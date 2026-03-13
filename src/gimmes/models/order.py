"""Order and fill models."""

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
    yes_price: int | None = None  # Price in cents (1-99)
    no_price: int | None = None
    client_order_id: str = ""
    time_in_force: str = "gtc"  # gtc, fok, ioc
    post_only: bool = True  # Maker guarantee

    @property
    def price_cents(self) -> int:
        """Effective price in cents."""
        if self.yes_price is not None:
            return self.yes_price
        if self.no_price is not None:
            return self.no_price
        return 0


class Order(BaseModel):
    """A resting or filled order."""

    order_id: str
    ticker: str
    action: OrderAction
    side: OrderSide
    status: str = ""  # resting, canceled, executed
    yes_price: int = 0
    no_price: int = 0
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
    yes_price: int = 0
    no_price: int = 0
    created_time: datetime | None = None
    is_taker: bool = False
