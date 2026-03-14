"""Gimme scoring models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ConfidenceSignal(BaseModel):
    """An independent signal supporting a probability estimate."""

    source: str  # e.g., "cleveland_fed_nowcast", "news_consensus", "cross_platform"
    description: str
    strength: float = Field(ge=0.0, le=1.0)  # 0 = weak, 1 = very strong
    timestamp: datetime = Field(default_factory=datetime.now)


class GimmeScore(BaseModel):
    """Composite score for a gimme candidate (0-100)."""

    total: float = Field(ge=0.0, le=100.0)
    edge_size_score: float = 0.0
    signal_strength_score: float = 0.0
    liquidity_depth_score: float = 0.0
    settlement_clarity_score: float = 0.0
    time_to_resolution_score: float = 0.0
    memo: str = ""

    def qualifies(self, threshold: float = 75) -> bool:
        """Check if this score meets the given threshold."""
        return self.total >= threshold


class GimmeCandidate(BaseModel):
    """A market identified as a potential gimme."""

    ticker: str
    title: str = ""
    market_price: float  # Current YES price
    model_probability: float = 0.0  # Our estimated true probability
    edge: float = 0.0  # model_probability - market_price
    signals: list[ConfidenceSignal] = Field(default_factory=list)
    score: GimmeScore | None = None
    scanned_at: datetime = Field(default_factory=datetime.now)
    research_memo: str = ""
