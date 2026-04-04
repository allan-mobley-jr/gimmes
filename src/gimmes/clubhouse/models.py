"""Pydantic response models for Clubhouse API endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class StatusResponse(BaseModel):
    mode: str = "driving_range"
    loop_active: bool = False
    current_cycle: int = 0
    pause_seconds: int = 0
    session_pid: int | None = None
    session_started_at: str | None = None


class PortfolioResponse(BaseModel):
    balance: float = 0.0
    portfolio_value: float = 0.0
    total_equity: float = 0.0
    unrealized_pnl: float = 0.0
    daily_pnl: float = 0.0
    total_pnl: float = 0.0


class PositionItem(BaseModel):
    id: int = 0
    ticker: str = ""
    title: str = ""
    side: str = "yes"
    count: int = 0
    avg_price: float = 0.0
    market_price: float = 0.0
    cost_basis: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    close_time: str | None = None
    updated_at: str = ""


class TradeItem(BaseModel):
    id: int = 0
    ticker: str = ""
    action: str = ""
    side: str = "yes"
    count: int = 0
    price: float = 0.0
    model_probability: float = 0.0
    gimme_score: float = 0.0
    edge: float = 0.0
    rationale: str = ""
    agent: str = ""
    timestamp: str = ""


class CandidateItem(BaseModel):
    id: int = 0
    ticker: str = ""
    title: str = ""
    market_price: float = 0.0
    model_probability: float = 0.0
    edge: float = 0.0
    gimme_score: float = 0.0
    research_memo: str = ""
    scanned_at: str = ""
    cap_blocked: bool = False


class MetricsResponse(BaseModel):
    win_rate: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    total_return: float = 0.0
    total_return_pct: float = 0.0
    equity_curve: list[dict] = []


class RiskResponse(BaseModel):
    daily_loss_pct: float = 0.0
    daily_loss_limit_pct: float = 0.15
    daily_pnl: float = 0.0
    position_count: int = 0
    max_positions: int = 15
    largest_position_pct: float = 0.0
    max_position_pct: float = 0.05
    bankroll: float = 0.0
    deployed_cost_basis: float = 0.0


class ActivityItem(BaseModel):
    id: int = 0
    cycle: int = 0
    agent: str = ""
    phase: str = ""
    message: str = ""
    details: str = ""
    timestamp: str = ""
    session_id: int | None = None


class ErrorItem(BaseModel):
    id: int = 0
    timestamp: str = ""
    severity: str = ""
    category: str = ""
    error_code: str = ""
    component: str = ""
    agent: str = ""
    cycle: int = 0
    message: str = ""
    resolved: bool = False
    github_issue_url: str = ""


class RecommendationItem(BaseModel):
    id: int = 0
    timestamp: str = ""
    parameter_path: str = ""
    current_value: str = ""
    recommended_value: str = ""
    confidence: str = ""
    analysis_type: str = ""
    rationale: str = ""
    status: str = "pending"


class MarketDetailResponse(BaseModel):
    ticker: str = ""
    title: str = ""
    subtitle: str = ""
    status: str = ""
    close_time: str | None = None
    volume: int = 0
    volume_24h: int = 0
    open_interest: int = 0
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    last_price: float = 0.0


class ConfigResponse(BaseModel):
    mode: str = "driving_range"
    strategy: dict = {}
    sizing: dict = {}
    risk: dict = {}
    orders: dict = {}
    scanner: dict = {}
    scoring: dict = {}
    paper: dict = {}
