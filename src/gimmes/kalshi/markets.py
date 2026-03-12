"""Kalshi market discovery and data endpoints."""

from __future__ import annotations

from gimmes.kalshi.client import KalshiClient
from gimmes.models.market import Market, MarketStatus, Orderbook, OrderbookLevel


def _cents_to_dollars(data: dict, key: str) -> float:  # type: ignore[type-arg]
    """Convert a field from cents (int) to dollars, or pass through floats."""
    val = data.get(key, 0)
    return val / 100 if isinstance(val, int) else val


def _parse_market(data: dict) -> Market:  # type: ignore[type-arg]
    """Parse a market from Kalshi API response."""
    return Market(
        ticker=data.get("ticker", ""),
        event_ticker=data.get("event_ticker", ""),
        title=data.get("title", ""),
        subtitle=data.get("subtitle", ""),
        status=MarketStatus(data.get("status", "active")),
        yes_bid=_cents_to_dollars(data, "yes_bid"),
        yes_ask=_cents_to_dollars(data, "yes_ask"),
        no_bid=_cents_to_dollars(data, "no_bid"),
        no_ask=_cents_to_dollars(data, "no_ask"),
        last_price=_cents_to_dollars(data, "last_price"),
        volume=data.get("volume", 0),
        volume_24h=data.get("volume_24h", 0),
        open_interest=data.get("open_interest", 0),
        close_time=data.get("close_time"),
        expiration_time=data.get("expiration_time"),
        result=data.get("result", ""),
        category=data.get("category", ""),
        rules_primary=data.get("rules_primary", ""),
    )


def _parse_orderbook(ticker: str, data: dict) -> Orderbook:  # type: ignore[type-arg]
    """Parse orderbook from Kalshi API response."""
    yes_bids = [
        OrderbookLevel(price=level[0] / 100, quantity=level[1])
        for level in data.get("yes", [])
    ]
    no_bids = [
        OrderbookLevel(price=level[0] / 100, quantity=level[1])
        for level in data.get("no", [])
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

    while True:
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

    return all_markets


async def get_market(client: KalshiClient, ticker: str) -> Market:
    """Get a single market by ticker."""
    data = await client.get(f"/markets/{ticker}")
    return _parse_market(data.get("market", data))


async def get_event(client: KalshiClient, event_ticker: str) -> dict:  # type: ignore[type-arg]
    """Get event details."""
    return await client.get(f"/events/{event_ticker}")


async def get_orderbook(client: KalshiClient, ticker: str, depth: int = 10) -> Orderbook:
    """Get orderbook for a market."""
    data = await client.get(f"/markets/{ticker}/orderbook", params={"depth": depth})
    return _parse_orderbook(ticker, data.get("orderbook", data))
