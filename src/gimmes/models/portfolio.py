"""Portfolio and position models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Position(BaseModel):
    """An open position in a market."""

    ticker: str
    title: str = ""
    side: Literal["yes", "no"] = "yes"
    count: int = 0
    avg_price: float = 0.0  # Average entry price in dollars
    market_price: float = 0.0  # Current market price
    cost_basis: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0

    @property
    def total_pnl(self) -> float:
        return self.unrealized_pnl + self.realized_pnl


class PortfolioSnapshot(BaseModel):
    """Point-in-time snapshot of the portfolio."""

    timestamp: datetime = Field(default_factory=datetime.now)
    balance: float = 0.0
    portfolio_value: float = 0.0
    total_equity: float = 0.0
    positions: list[Position] = Field(default_factory=list)
    open_position_count: int = 0
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
