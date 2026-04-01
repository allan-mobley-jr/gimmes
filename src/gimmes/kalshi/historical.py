"""Kalshi historical data endpoints for settled markets and candlesticks."""

from __future__ import annotations

import logging
from dataclasses import dataclass

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


def _parse_candle(data: dict) -> Candle:  # type: ignore[type-arg]
    """Parse a candlestick from the Kalshi API response."""
    yes_bid = data.get("yes_bid", {})
    yes_ask = data.get("yes_ask", {})
    price = data.get("price", {})
    return Candle(
        end_period_ts=int(data.get("end_period_ts", 0)),
        yes_bid_open=float(yes_bid.get("open", 0)),
        yes_bid_high=float(yes_bid.get("high", 0)),
        yes_bid_low=float(yes_bid.get("low", 0)),
        yes_bid_close=float(yes_bid.get("close", 0)),
        yes_ask_open=float(yes_ask.get("open", 0)),
        yes_ask_high=float(yes_ask.get("high", 0)),
        yes_ask_low=float(yes_ask.get("low", 0)),
        yes_ask_close=float(yes_ask.get("close", 0)),
        price_open=float(price.get("open", 0)),
        price_high=float(price.get("high", 0)),
        price_low=float(price.get("low", 0)),
        price_close=float(price.get("close", 0)),
        volume=int(float(data.get("volume", 0))),
        open_interest=int(float(data.get("open_interest", 0))),
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
) -> list[Market]:
    """Fetch all settled historical markets, handling pagination automatically."""
    all_markets: list[Market] = []
    cursor: str | None = None

    for _ in range(max_pages):
        markets, cursor = await list_historical_markets(
            client,
            cursor=cursor,
            event_ticker=event_ticker,
        )
        all_markets.extend(markets)
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
    """
    params: dict[str, str | int] = {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "period_interval": period_interval,
    }
    data = await client.get(
        f"/historical/markets/{ticker}/candlesticks", params=params,
    )
    candles = [_parse_candle(c) for c in data.get("candlesticks", [])]
    candles.sort(key=lambda c: c.end_period_ts)
    return candles
