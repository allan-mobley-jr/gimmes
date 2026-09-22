"""Order and fill models.

All prices are in dollars (0.00–1.00). Cents conversion happens only at
the API boundary (kalshi/orders.py) and paper DB boundary (paper/broker.py).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class OrderSide(StrEnum):
    YES = "yes"
    NO = "no"


class OrderAction(StrEnum):
    BUY = "buy"
    SELL = "sell"


class CreateOrderParams(BaseModel):
    """Parameters for creating a new order on Kalshi."""

    ticker: str
    action: OrderAction = OrderAction.BUY
    side: OrderSide = OrderSide.YES
    count: int = Field(gt=0)
    yes_price: float | None = Field(default=None, ge=0.0, le=1.0)
    no_price: float | None = Field(default=None, ge=0.0, le=1.0)
    client_order_id: str = ""
    time_in_force: str = "good_till_canceled"
    post_only: bool = True  # Maker guarantee
    # Unix seconds after which an unfilled order self-cancels. On the
    # real exchange Kalshi enforces this server-side; the paper broker
    # rests unfilled BUYs until expiry instead of canceling them at
    # placement (hourly rest-on-miss lane).
    expiration_ts: int | None = Field(default=None, gt=0)

    @property
    def price(self) -> float:
        """Effective price in dollars."""
        if self.yes_price is not None:
            return self.yes_price
        if self.no_price is not None:
            return self.no_price
        return 0.0


class Order(BaseModel):
    """A resting or filled order."""

    order_id: str
    ticker: str
    action: OrderAction
    side: OrderSide
    status: str = ""  # resting, canceled, executed
    yes_price: float = 0.0
    no_price: float = 0.0
    count: int = 0
    remaining_count: int = 0
    created_time: datetime | None = None
    client_order_id: str = ""
    # #690: why a canceled order canceled (paper broker sets it; the
    # real Kalshi path leaves it empty). Agents need a nameable cause.
    reason: str = ""
    # #834: what the contracts that filled at placement actually cost —
    # the fill-weighted average price (side-relative like yes_price /
    # no_price) and the fees charged. None when nothing filled or the
    # venue's response carried no fill data. Unbounded on purpose: the
    # producers range-guard, and a constraint here could only raise
    # AFTER the paper transaction committed the fills.
    avg_fill_price: float | None = None
    fill_fees: float | None = None

    @property
    def is_open(self) -> bool:
        return self.status == "resting"

    @property
    def fill_price(self) -> float:
        """The price the ledger books for this order's fills (#834).

        The reported VWAP when the venue gave one; otherwise the
        side-relative limit, which is exact for a maker fill.
        """
        if self.avg_fill_price is not None:
            return self.avg_fill_price
        return self.yes_price if self.side == OrderSide.YES else self.no_price


class Fill(BaseModel):
    """A fill event from a matched order."""

    trade_id: str = ""
    order_id: str = ""
    ticker: str = ""
    action: OrderAction = OrderAction.BUY
    side: OrderSide = OrderSide.YES
    count: int = 0
    yes_price: float = 0.0
    no_price: float = 0.0
    created_time: datetime | None = None
    is_taker: bool = False
