"""Trade decision and outcome models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class TradeDecision(BaseModel):
    """Record of a trade decision (open, close, or skip)."""

    class Action(StrEnum):
        OPEN = "open"
        CLOSE = "close"
        SKIP = "skip"
        SIZE_UP = "size_up"

    ticker: str
    action: Action
    side: str = "yes"
    count: int = 0
    price: float = 0.0
    model_probability: float = 0.0
    gimme_score: float = 0.0
    edge: float = 0.0
    kelly_fraction: float = 0.0
    rationale: str = ""
    agent: str = ""  # Which agent made the decision
    timestamp: datetime = Field(default_factory=datetime.now)
    order_id: str = ""


class TradeOutcome(BaseModel):
    """Final outcome of a completed trade."""

    class Result(StrEnum):
        WIN = "win"
        LOSS = "loss"
        SCRATCH = "scratch"  # Broke even or canceled

    ticker: str
    result: Result
    entry_price: float = 0.0
    exit_price: float = 0.0
    contracts: int = 0
    gross_pnl: float = 0.0
    fees_paid: float = 0.0
    net_pnl: float = 0.0
    predicted_edge: float = 0.0
    realized_edge: float = 0.0
    hold_duration_hours: float = 0.0
    opened_at: datetime | None = None
    closed_at: datetime | None = None
