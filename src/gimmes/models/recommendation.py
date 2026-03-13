"""Recommendation model for strategy tuning advisor."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecStatus(StrEnum):
    PENDING = "pending"
    IMPLEMENTED = "implemented"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class AnalysisType(StrEnum):
    THRESHOLD_SWEEP = "threshold_sweep"
    EDGE_DECAY = "edge_decay"
    SCORING_CORRELATION = "scoring_correlation"
    KELLY_OPTIMIZATION = "kelly_optimization"
    SCANNER_REVIEW = "scanner_review"
    MISSED_OPPORTUNITY = "missed_opportunity"


class Recommendation(BaseModel):
    """A data-backed parameter change recommendation."""

    parameter_path: str  # e.g. 'strategy.gimme_threshold'
    current_value: str
    recommended_value: str
    confidence: Confidence = Confidence.MEDIUM
    analysis_type: AnalysisType = AnalysisType.THRESHOLD_SWEEP
    rationale: str = ""
    supporting_data: str = Field(default="{}")  # JSON blob
