"""Kalshi market discovery and data endpoints."""

from __future__ import annotations

from gimmes.kalshi.client import KalshiClient
from gimmes.models.market import Market, MarketStatus, Orderbook, OrderbookLevel


def _dollars_field(data: dict, key: str) -> float:  # type: ignore[type-arg]
    """Read a dollar-string field (e.g. '0.2500') as float. Falls back to int cents."""
    # API v2 returns dollar strings like "yes_bid_dollars": "0.5500"
    dollars_key = f"{key}_dollars"
    if dollars_key in data:
        return float(data[dollars_key])
    val = data.get(key, 0)
    return val / 100 if isinstance(val, int) else float(val)


def _fp_field(data: dict, key: str) -> int:
    """Read a fractional-precision field (e.g. '150.00') as int."""
    fp_key = f"{key}_fp"
    if fp_key in data:
        return int(float(data[fp_key]))
    return int(data.get(key, 0))


def _parse_market(data: dict) -> Market:  # type: ignore[type-arg]
    """Parse a market from Kalshi API response."""
    return Market(
        ticker=data.get("ticker", ""),
        event_ticker=data.get("event_ticker", ""),
        title=data.get("title", ""),
        subtitle=data.get("subtitle", ""),
        status=MarketStatus(data.get("status", "active")),
        yes_bid=_dollars_field(data, "yes_bid"),
        yes_ask=_dollars_field(data, "yes_ask"),
        no_bid=_dollars_field(data, "no_bid"),
        no_ask=_dollars_field(data, "no_ask"),
        last_price=_dollars_field(data, "last_price"),
        volume=_fp_field(data, "volume"),
        volume_24h=_fp_field(data, "volume_24h"),
        open_interest=_fp_field(data, "open_interest"),
        close_time=data.get("close_time"),
        expiration_time=data.get("expiration_time"),
        result=data.get("result", ""),
        rules_primary=data.get("rules_primary", ""),
    )


def _parse_orderbook(ticker: str, data: dict) -> Orderbook:  # type: ignore[type-arg]
    """Parse orderbook from Kalshi API response."""
    # API v2 returns orderbook_fp with dollar-string levels: [["0.5500", "10.00"], ...]
    # Falls back to legacy integer-cents format: [[55, 10], ...]
    fp = data.get("orderbook_fp", {})
    yes_raw = fp.get("yes_dollars", data.get("yes", []))
    no_raw = fp.get("no_dollars", data.get("no", []))

    yes_bids = [
        OrderbookLevel(price=float(level[0]), quantity=int(float(level[1])))
        for level in yes_raw
    ]
    no_bids = [
        OrderbookLevel(price=float(level[0]), quantity=int(float(level[1])))
        for level in no_raw
    ]
    return Orderbook(ticker=ticker, yes_bids=yes_bids, no_bids=no_bids)


async def list_markets(
    client: KalshiClient,
    *,
    status: str = "open",
    limit: int = 200,
    cursor: str | None = None,
    event_ticker: str | None = None,
    series_ticker: str | None = None,
) -> tuple[list[Market], str | None]:
    """List markets with pagination.

    Returns:
        Tuple of (markets, next_cursor). next_cursor is None when no more pages.
    """
    params: dict[str, str | int] = {"status": status, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    if event_ticker:
        params["event_ticker"] = event_ticker
    if series_ticker:
        params["series_ticker"] = series_ticker

    data = await client.get("/markets", params=params)
    markets = [_parse_market(m) for m in data.get("markets", [])]
    next_cursor = data.get("cursor")
    return markets, next_cursor


async def list_all_markets(
    client: KalshiClient,
    *,
    status: str = "open",
    event_ticker: str | None = None,
    series_ticker: str | None = None,
) -> list[Market]:
    """Fetch all markets, handling pagination automatically."""
    all_markets: list[Market] = []
    cursor: str | None = None
    max_pages = 50

    for _ in range(max_pages):
        markets, cursor = await list_markets(
            client,
            status=status,
            cursor=cursor,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
        )
        all_markets.extend(markets)
        if not cursor or not markets:
            break
    else:
        import logging
        logging.getLogger(__name__).warning(
            "Pagination limit reached (%d pages, %d markets)",
            max_pages, len(all_markets),
        )

    return all_markets


async def get_market(client: KalshiClient, ticker: str) -> Market:
    """Get a single market by ticker."""
    data = await client.get(f"/markets/{ticker}")
    return _parse_market(data.get("market", data))


async def list_series(
    client: KalshiClient,
    *,
    category: str | None = None,
) -> list[dict]:  # type: ignore[type-arg]
    """List series, optionally filtered by category.

    Categories match Kalshi's top-level groupings:
    Economics, Politics, Financials, Sports, Crypto, etc.
    """
    params: dict[str, str] = {}
    if category:
        params["category"] = category
    data = await client.get("/series", params=params)
    return data.get("series", [])


async def get_event(client: KalshiClient, event_ticker: str) -> dict:  # type: ignore[type-arg]
    """Get event details."""
    return await client.get(f"/events/{event_ticker}")


async def get_orderbook(client: KalshiClient, ticker: str, depth: int = 10) -> Orderbook:
    """Get orderbook for a market."""
    data = await client.get(f"/markets/{ticker}/orderbook", params={"depth": depth})
    return _parse_orderbook(ticker, data)
