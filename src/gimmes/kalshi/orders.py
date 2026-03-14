"""Kalshi order management endpoints."""

from __future__ import annotations

import uuid

from gimmes.kalshi.client import KalshiClient
from gimmes.models.order import CreateOrderParams, Fill, Order, OrderAction, OrderSide


def _parse_order(data: dict) -> Order:  # type: ignore[type-arg]
    """Parse an order from Kalshi API response."""
    # API returns dollar strings (e.g. "0.5500") — keep as dollar floats
    yes_price = float(data.get("yes_price_dollars", "0"))
    no_price = float(data.get("no_price_dollars", "0"))
    count = int(round(float(data.get("initial_count_fp", "0"))))
    remaining = int(round(float(data.get("remaining_count_fp", "0"))))
    return Order(
        order_id=data.get("order_id", ""),
        ticker=data.get("ticker", ""),
        action=OrderAction(data.get("action", "buy")),
        side=OrderSide(data.get("side", "yes")),
        status=data.get("status", ""),
        yes_price=yes_price,
        no_price=no_price,
        count=count,
        remaining_count=remaining,
        created_time=data.get("created_time"),
        client_order_id=data.get("client_order_id", ""),
    )


def _parse_fill(data: dict) -> Fill:  # type: ignore[type-arg]
    """Parse a fill from Kalshi API response."""
    yes_price = float(data.get("yes_price_dollars", "0"))
    no_price = float(data.get("no_price_dollars", "0"))
    count = int(round(float(data.get("count_fp", data.get("count", "0")))))
    return Fill(
        trade_id=data.get("trade_id", ""),
        order_id=data.get("order_id", ""),
        ticker=data.get("ticker", ""),
        action=OrderAction(data.get("action", "buy")),
        side=OrderSide(data.get("side", "yes")),
        count=count,
        yes_price=yes_price,
        no_price=no_price,
        created_time=data.get("created_time"),
        is_taker=data.get("is_taker", False),
    )


async def create_order(client: KalshiClient, params: CreateOrderParams) -> Order:
    """Place a new order."""
    body: dict[str, object] = {
        "ticker": params.ticker,
        "action": params.action.value,
        "side": params.side.value,
        "count_fp": f"{params.count:.2f}",
    }
    if params.yes_price is not None:
        body["yes_price_dollars"] = f"{params.yes_price:.4f}"
    if params.no_price is not None:
        body["no_price_dollars"] = f"{params.no_price:.4f}"
    if params.client_order_id:
        body["client_order_id"] = params.client_order_id
    else:
        body["client_order_id"] = str(uuid.uuid4())
    if params.time_in_force != "gtc":
        body["time_in_force"] = params.time_in_force
    if params.post_only:
        body["post_only"] = True

    data = await client.post("/portfolio/orders", json=body)  # type: ignore[arg-type]
    return _parse_order(data.get("order", data))


async def cancel_order(client: KalshiClient, order_id: str) -> dict:  # type: ignore[type-arg]
    """Cancel a resting order."""
    return await client.delete(f"/portfolio/orders/{order_id}")


async def list_orders(
    client: KalshiClient,
    *,
    ticker: str | None = None,
    status: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> tuple[list[Order], str | None]:
    """List orders with optional filters."""
    params: dict[str, str | int] = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    if status:
        params["status"] = status
    if cursor:
        params["cursor"] = cursor

    data = await client.get("/portfolio/orders", params=params)
    orders = [_parse_order(o) for o in data.get("orders", [])]
    next_cursor = data.get("cursor")
    return orders, next_cursor


async def list_fills(
    client: KalshiClient,
    *,
    ticker: str | None = None,
    order_id: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> tuple[list[Fill], str | None]:
    """List fill history."""
    params: dict[str, str | int] = {"limit": limit}
    if ticker:
        params["ticker"] = ticker
    if order_id:
        params["order_id"] = order_id
    if cursor:
        params["cursor"] = cursor

    data = await client.get("/portfolio/fills", params=params)
    fills = [_parse_fill(f) for f in data.get("fills", [])]
    next_cursor = data.get("cursor")
    return fills, next_cursor
