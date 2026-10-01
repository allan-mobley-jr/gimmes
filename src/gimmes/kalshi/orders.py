"""Kalshi order management endpoints."""

from __future__ import annotations

import logging
import uuid

from gimmes.kalshi.client import KalshiClient
from gimmes.models.order import CreateOrderParams, Fill, Order, OrderAction, OrderSide

_log = logging.getLogger(__name__)


def _sum_dollar_fields(data: dict, *keys: str) -> float | None:  # type: ignore[type-arg]
    """Sum the named response fields that are present.

    None when none of them is present or any present one is unparseable
    — "not reported", never a fabricated zero.
    """
    present = [k for k in keys if data.get(k) is not None]
    if not present:
        return None
    try:
        return sum(float(data[k]) for k in present)
    except (TypeError, ValueError):
        return None


def _reported_fill_vwap(
    data: dict, limit: float, action: str,  # type: ignore[type-arg]
) -> float | None:
    """#834: VWAP = fill cost / fill count from Kalshi's order response.

    Missing, unparseable, or worse-than-limit (a cost quoted on the other
    side or in the wrong unit) → None; the ledger then falls back to the
    limit rather than book a fabricated price. SELLs are never derived:
    Kalshi's docs don't say which side a sell's cost is quoted on, and a
    wrong side would flip a close's sign — the limit is exact for the
    maker sells this book places (confirm against a live response first).
    """
    if action != "buy":
        return None
    fill_count = _sum_dollar_fields(data, "fill_count_fp")
    fill_cost = _sum_dollar_fields(
        data, "taker_fill_cost_dollars", "maker_fill_cost_dollars",
    )
    if not fill_count or not fill_cost or fill_count <= 0 or fill_cost <= 0:
        return None
    candidate = round(fill_cost / fill_count, 4)
    within_limit = candidate <= limit if action == "buy" else candidate >= limit
    if candidate <= 1.0 and within_limit:
        return candidate
    _log.warning(
        "order %s: reported fill VWAP %.4f is inconsistent with the %s"
        " limit %.4f (fill_count=%s, cost=%s) — not booked; the ledger"
        " falls back to the limit (#834)",
        data.get("order_id", ""), candidate, action, limit,
        fill_count, fill_cost,
    )
    return None


def _parse_order(data: dict) -> Order:  # type: ignore[type-arg]
    """Parse an order from Kalshi API response."""
    # API returns dollar strings (e.g. "0.5500") — keep as dollar floats
    yes_price = float(data.get("yes_price_dollars", "0"))
    no_price = float(data.get("no_price_dollars", "0"))
    count = int(round(float(data.get("initial_count_fp", "0"))))
    remaining = int(round(float(data.get("remaining_count_fp", "0"))))
    side = data.get("side", "yes")
    action = data.get("action", "buy")
    limit = yes_price if side == "yes" else no_price
    avg_fill_price = _reported_fill_vwap(data, limit, action)
    fill_fees = _sum_dollar_fields(data, "taker_fees_dollars", "maker_fees_dollars")
    if (
        count > remaining
        and action == "buy"
        and (avg_fill_price is None or fill_fees is None)
    ):
        # Something filled but the response carried no usable fill
        # data — the ledger books the limit / recomputes the fee, which
        # is the #834 drift re-emerging on the real path; leave a trace.
        _log.warning(
            "order %s: %d contracts filled but the response reported"
            " vwap=%s fees=%s; ledger falls back (#834)",
            data.get("order_id", ""), count - remaining,
            avg_fill_price, fill_fees,
        )
    return Order(
        order_id=data.get("order_id", ""),
        ticker=data.get("ticker", ""),
        action=OrderAction(action),
        side=OrderSide(side),
        status=data.get("status", ""),
        yes_price=yes_price,
        no_price=no_price,
        count=count,
        remaining_count=remaining,
        created_time=data.get("created_time"),
        client_order_id=data.get("client_order_id", ""),
        avg_fill_price=avg_fill_price,
        fill_fees=fill_fees,
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
    if params.time_in_force != "good_till_canceled":
        body["time_in_force"] = params.time_in_force
    if params.post_only:
        body["post_only"] = True
    if params.expiration_ts is not None:
        body["expiration_ts"] = params.expiration_ts

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
