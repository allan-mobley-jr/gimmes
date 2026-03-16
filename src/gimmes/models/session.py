"""Trading session model."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class SessionStatus(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"
    CRASHED = "crashed"


class Session(BaseModel):
    """A trading session record."""

    id: int = 0
    mode: str = "driving_range"
    pid: int = 0
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC)
    )
    ended_at: datetime | None = None
    status: SessionStatus = SessionStatus.ACTIVE
    cycle_count: int = 0
