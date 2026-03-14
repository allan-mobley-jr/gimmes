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

    @property
    def is_open(self) -> bool:
        return self.status == "resting"


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
