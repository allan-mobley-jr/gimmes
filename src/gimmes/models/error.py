"""Error log entry model."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ErrorSeverity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ErrorCategory(StrEnum):
    API_ERROR = "api_error"
    AUTH_FAILURE = "auth_failure"
    DATA_INTEGRITY = "data_integrity"
    AGENT_FAILURE = "agent_failure"
    ORDER_FAILURE = "order_failure"
    RISK_BREACH = "risk_breach"
    CONFIG_ERROR = "config_error"
    NETWORK_ERROR = "network_error"
    PAPER_BROKER = "paper_broker"


class ErrorLogEntry(BaseModel):
    """A structured error log entry."""

    severity: ErrorSeverity = ErrorSeverity.ERROR
    category: ErrorCategory = ErrorCategory.API_ERROR
    error_code: str = ""
    component: str = ""
    agent: str = ""
    cycle: int = 0
    message: str = ""
    stack_trace: str = ""
    context: str = Field(default="{}")  # JSON blob
    resolved: bool = False
    github_issue_url: str = ""
