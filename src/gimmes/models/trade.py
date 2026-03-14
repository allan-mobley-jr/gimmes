"""Trade decision and outcome models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

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
    side: Literal["yes", "no"] = "yes"
    count: int = Field(default=0, ge=0)
    price: float = Field(default=0.0, ge=0.0, le=1.0)
    model_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    gimme_score: float = Field(default=0.0, ge=0.0, le=100.0)
    edge: float = 0.0
    kelly_fraction: float = 0.0
    rationale: str = ""
    agent: str = ""  # Which agent made the decision
    timestamp: datetime = Field(default_factory=datetime.now)
    order_id: str = ""
