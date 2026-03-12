"""Kalshi portfolio endpoints: balance, positions, settlements."""

from __future__ import annotations

from gimmes.kalshi.client import KalshiClient
from gimmes.models.portfolio import Position


def _parse_position(data: dict) -> Position:  # type: ignore[type-arg]
    """Parse a position from Kalshi API response."""
    # Kalshi returns market_exposure, resting_orders_count, etc.
    ticker = data.get("ticker", data.get("market_ticker", ""))
    count = data.get("position", 0)
    # Positive = YES position, negative = NO position
    side = "yes" if count >= 0 else "no"
    abs_count = abs(count)

    return Position(
        ticker=ticker,
        side=side,
        count=abs_count,
        market_value=data.get("market_exposure", 0) / 100 if data.get("market_exposure") else 0,
        realized_pnl=data.get("realized_pnl", 0) / 100 if data.get("realized_pnl") else 0,
    )


async def get_balance(client: KalshiClient) -> float:
    """Get current account balance in dollars."""
    data = await client.get("/portfolio/balance")
    # Balance is returned in cents
    return data.get("balance", 0) / 100


async def get_positions(
    client: KalshiClient,
    *,
    settlement_status: str = "unsettled",
    limit: int = 200,
    cursor: str | None = None,
) -> tuple[list[Position], str | None]:
    """Get current positions."""
    params: dict[str, str | int] = {
        "settlement_status": settlement_status,
        "limit": limit,
    }
    if cursor:
        params["cursor"] = cursor

    data = await client.get("/portfolio/positions", params=params)
    positions = [_parse_position(p) for p in data.get("market_positions", [])]
    next_cursor = data.get("cursor")
    return positions, next_cursor


async def get_all_positions(
    client: KalshiClient,
    settlement_status: str = "unsettled",
) -> list[Position]:
    """Fetch all positions, handling pagination."""
    all_positions: list[Position] = []
    cursor: str | None = None

    while True:
        positions, cursor = await get_positions(
            client, settlement_status=settlement_status, cursor=cursor
        )
        all_positions.extend(positions)
        if not cursor or not positions:
            break

    return all_positions


async def get_settlements(
    client: KalshiClient,
    *,
    limit: int = 100,
    cursor: str | None = None,
) -> tuple[list[dict], str | None]:  # type: ignore[type-arg]
    """Get settlement history."""
    params: dict[str, str | int] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor

    data = await client.get("/portfolio/settlements", params=params)
    settlements = data.get("settlements", [])
    next_cursor = data.get("cursor")
    return settlements, next_cursor
