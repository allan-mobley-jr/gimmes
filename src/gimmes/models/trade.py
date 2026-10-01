"""Trade decision and outcome models."""

from __future__ import annotations

from datetime import UTC, datetime
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
    thesis: str = ""
    # Structured skip cause (#657) — machine-queryable, unlike the
    # prose rationale. Empty for non-skip rows and legacy data.
    reason: str = ""
    agent: str = ""  # Which agent made the decision
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )
    order_id: str = ""
    # #834: the fee actually paid for `count` contracts, so the scorecard
    # sums what the venue charged instead of recomputing. None on legacy
    # rows and on rows that never traded at a venue (settlement, reconcile
    # drift) — those fall back to a maker-rate recompute. Note `price` is
    # the fill VWAP while `edge` stays measured against the approved cap
    # (#766), so `model_probability - price` need not equal `edge`.
    fee: float | None = None
