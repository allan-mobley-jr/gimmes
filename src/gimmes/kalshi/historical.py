"""Kalshi historical data endpoints for settled markets and candlesticks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from gimmes.kalshi.client import KalshiClient
from gimmes.kalshi.markets import parse_market
from gimmes.models.market import Market

logger = logging.getLogger(__name__)


@dataclass
class Candle:
    """A single candlestick from historical market data."""

    end_period_ts: int
    yes_bid_open: float
    yes_bid_high: float
    yes_bid_low: float
    yes_bid_close: float
    yes_ask_open: float
    yes_ask_high: float
    yes_ask_low: float
    yes_ask_close: float
    price_open: float
    price_high: float
    price_low: float
    price_close: float
    volume: int
    open_interest: int


def _ohlc(group: dict, field: str) -> float:  # type: ignore[type-arg]
    """Read an OHLC component, preferring the live API's `*_dollars`
    string fields (e.g. `close_dollars: "0.3200"`, verified 2026-07-03)
    over the legacy plain keys used by older fixtures."""
    value = group.get(f"{field}_dollars", group.get(field, 0))
    try:
        # House convention (markets.py:_dollars_field): a plain-key INT
        # is legacy integer cents — 34 means $0.34, not $34.00.
        if isinstance(value, int) and not isinstance(value, bool):
            return value / 100
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _quote_group(data: dict, name: str) -> dict:  # type: ignore[type-arg]
    """Normalize a candle's quote group. Absent or JSON-null is
    treated as equivalent to the VERIFIED group-omission case (quiet
    periods — keeps the legacy zero-default path; null is the classic
    nil-without-omitempty serialization of the same state); any other
    non-dict value is a shape anomaly
    that must raise the documented ValueError, not leak an
    AttributeError/TypeError out of _ohlc (#704)."""
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            f"candle {name} group is not an object:"
            f" {type(value).__name__}",
        )
    return value


def _require_close(group: dict, name: str) -> None:  # type: ignore[type-arg]
    """A non-empty quote group must carry a NUMERIC close in a known
    spelling. A missing key means an API rename (#655: close ->
    close_dollars) and a non-coercible value means a value-shape
    change — either way the zero-default would mint an all-zero
    candle that parses, gets disk-cached (#696), and miscounts as a
    one-sided quote (#704). A PRESENT close key with value 0 is a
    legal 0.00 quote and passes; empty/missing groups keep the legacy
    zero-default behavior (live data verifiably omits whole groups,
    and the `price` group is verifiably partial)."""
    if not group:
        return
    if "close_dollars" not in group and "close" not in group:
        raise ValueError(
            f"candle {name} group has no close field: {sorted(group)}",
        )
    value = group.get("close_dollars", group.get("close"))
    try:
        float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(
            f"candle {name} close is not numeric: {value!r}",
        ) from None


def _parse_candle(data: dict) -> Candle:  # type: ignore[type-arg]
    """Parse a candlestick from the Kalshi API response."""
    if "end_period_ts" not in data:
        # The engine selects the entry candle and staleness-checks by
        # this timestamp — a renamed key would zero-default, price
        # every market from an arbitrary candle, and disk-cache the
        # result (#704).
        raise ValueError(
            f"candle has no end_period_ts field: {sorted(data)}",
        )
    try:
        end_period_ts = int(data["end_period_ts"])
    except (TypeError, ValueError):
        raise ValueError(
            f"candle end_period_ts is not numeric:"
            f" {data['end_period_ts']!r}",
        ) from None
    yes_bid = _quote_group(data, "yes_bid")
    yes_ask = _quote_group(data, "yes_ask")
    price = _quote_group(data, "price")
    # Close guard only on the groups the backtest consumes — the live
    # `price` group is verifiably partial (open/close only).
    _require_close(yes_bid, "yes_bid")
    _require_close(yes_ask, "yes_ask")
    return Candle(
        end_period_ts=end_period_ts,
        yes_bid_open=_ohlc(yes_bid, "open"),
        yes_bid_high=_ohlc(yes_bid, "high"),
        yes_bid_low=_ohlc(yes_bid, "low"),
        yes_bid_close=_ohlc(yes_bid, "close"),
        yes_ask_open=_ohlc(yes_ask, "open"),
        yes_ask_high=_ohlc(yes_ask, "high"),
        yes_ask_low=_ohlc(yes_ask, "low"),
        yes_ask_close=_ohlc(yes_ask, "close"),
        price_open=_ohlc(price, "open"),
        price_high=_ohlc(price, "high"),
        price_low=_ohlc(price, "low"),
        price_close=_ohlc(price, "close"),
        volume=int(float(data.get("volume_fp", data.get("volume", 0)))),
        open_interest=int(float(
            data.get("open_interest_fp", data.get("open_interest", 0)),
        )),
    )


async def list_historical_markets(
    client: KalshiClient,
    *,
    limit: int = 1000,
    cursor: str | None = None,
    event_ticker: str | None = None,
) -> tuple[list[Market], str | None]:
    """List settled historical markets with pagination.

    Returns:
        Tuple of (markets, next_cursor). next_cursor is None when no more pages.
    """
    params: dict[str, str | int] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    if event_ticker:
        params["event_ticker"] = event_ticker

    data = await client.get("/historical/markets", params=params)
    markets = [parse_market(m) for m in data.get("markets", [])]
    next_cursor = data.get("cursor")
    return markets, next_cursor


async def list_all_historical_markets(
    client: KalshiClient,
    *,
    event_ticker: str | None = None,
    max_pages: int = 100,
    series_tickers: set[str] | None = None,
    min_close_time: datetime | None = None,
    max_close_time: datetime | None = None,
) -> list[Market]:
    """Fetch settled historical markets with optional per-page filtering.

    Filters are applied per-page to reduce memory usage. Markets that
    don't match are discarded immediately rather than accumulated.

    Args:
        series_tickers: Keep only markets in these series (by series_ticker).
        min_close_time: Keep only markets with close_time >= this value.
        max_close_time: Keep only markets with close_time <= this value.
    """
    def _in_window(m: Market) -> bool:
        ct = m.close_time
        if ct is None:
            return False
        if ct.tzinfo is None:
            ct = ct.replace(tzinfo=UTC)
        if min_close_time and ct < min_close_time:
            return False
        if max_close_time and ct > max_close_time:
            return False
        return True

    all_markets: list[Market] = []
    cursor: str | None = None
    has_date_filter = min_close_time is not None or max_close_time is not None

    for page in range(max_pages):
        markets, cursor = await list_historical_markets(
            client,
            cursor=cursor,
            event_ticker=event_ticker,
        )

        # Per-page filtering to reduce memory
        filtered = markets
        if series_tickers is not None:
            filtered = [m for m in filtered if m.series_ticker in series_tickers]
        if has_date_filter:
            filtered = [m for m in filtered if _in_window(m)]

        all_markets.extend(filtered)

        if page % 10 == 9:
            logger.info(
                "Historical fetch: page %d, %d markets matched so far",
                page + 1, len(all_markets),
            )

        if not cursor or not markets:
            break
    else:
        logger.warning(
            "Pagination limit reached (%d pages, %d markets)",
            max_pages, len(all_markets),
        )

    return all_markets


async def get_candlesticks(
    client: KalshiClient,
    ticker: str,
    *,
    start_ts: int,
    end_ts: int,
    period_interval: int = 1440,
) -> list[Candle]:
    """Fetch candlestick data for a historical market.

    Args:
        ticker: Market ticker.
        start_ts: Unix timestamp — candles ending on or after this time.
        end_ts: Unix timestamp — candles ending on or before this time.
        period_interval: Candle duration in minutes (1, 60, or 1440).

    Returns:
        List of Candle objects sorted by end_period_ts ascending.

    Raises:
        ValueError: when the response body is not a dict containing a
            ``candlesticks`` key, or a candle's quote group lacks any
            recognized close field — a shape anomaly must land in the
            backtest's fetch_failures counter, never in the disk
            cache's permanent negative entries (#704).
    """
    params: dict[str, str | int] = {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": period_interval,
    }
    series_ticker = ticker.split("-")[0]
    data = await client.get(
        f"/series/{series_ticker}/markets/{ticker}/candlesticks",
        params=params,
    )
    candles_raw = data.get("candlesticks") if isinstance(data, dict) else None
    if not isinstance(candles_raw, list):
        # Missing/non-list value != present-but-empty list: a shape
        # change (field rename, error-in-200 body, null value) must
        # surface as a fetch failure the engine can count, not parse
        # as empty history the disk cache would negative-cache
        # permanently (#704).
        raise ValueError(
            f"Unexpected candlesticks response for {ticker}: no"
            f" 'candlesticks' list (got"
            f" {sorted(data) if isinstance(data, dict) else type(data).__name__})",
        )
    candles = [_parse_candle(c) for c in candles_raw]
    candles.sort(key=lambda c: c.end_period_ts)
    return candles
