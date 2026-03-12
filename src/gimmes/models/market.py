"""Market and orderbook models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class MarketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"


class Market(BaseModel):
    """A Kalshi binary contract market."""

    ticker: str
    event_ticker: str = ""
    title: str = ""
    subtitle: str = ""
    status: MarketStatus = MarketStatus.OPEN
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0
    last_price: float = 0.0
    volume: int = 0
    volume_24h: int = 0
    open_interest: int = 0
    close_time: datetime | None = None
    expiration_time: datetime | None = None
    result: str = ""
    category: str = ""
    rules_primary: str = ""
    settlement_value: float | None = None

    @property
    def midpoint(self) -> float:
        if self.yes_bid > 0 and self.yes_ask > 0:
            return (self.yes_bid + self.yes_ask) / 2
        return self.last_price

    @property
    def spread(self) -> float:
        if self.yes_bid > 0 and self.yes_ask > 0:
            return self.yes_ask - self.yes_bid
        return 0.0


class OrderbookLevel(BaseModel):
    """A single price level in the orderbook."""

    price: float
    quantity: int


class Orderbook(BaseModel):
    """Orderbook snapshot for a market."""

    ticker: str
    yes_bids: list[OrderbookLevel] = Field(default_factory=list)
    no_bids: list[OrderbookLevel] = Field(default_factory=list)

    @property
    def best_yes_bid(self) -> float | None:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_yes_ask(self) -> float | None:
        # YES ask = 1 - best NO bid
        if self.no_bids:
            return round(1.0 - self.no_bids[0].price, 2)
        return None

    def depth_at_price(self, price: float, side: str = "yes") -> int:
        """Total contracts available at or better than the given price."""
        total = 0
        if side == "yes":
            for level in self.yes_bids:
                if level.price >= price:
                    total += level.quantity
        else:
            for level in self.no_bids:
                if level.price >= price:
                    total += level.quantity
        return total
