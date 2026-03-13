"""Pydantic response models for Clubhouse API endpoints."""

from __future__ import annotations

from pydantic import BaseModel


class StatusResponse(BaseModel):
    mode: str = "driving_range"
    loop_active: bool = False
    current_cycle: int = 0
    pause_seconds: int = 0


class PortfolioResponse(BaseModel):
    balance: float = 0.0
    portfolio_value: float = 0.0
    total_equity: float = 0.0
    unrealized_pnl: float = 0.0
    daily_pnl: float = 0.0
    total_pnl: float = 0.0


class PositionItem(BaseModel):
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
    ticker: str = ""
    title: str = ""
    market_price: float = 0.0
    model_probability: float = 0.0
    edge: float = 0.0
    gimme_score: float = 0.0
    research_memo: str = ""
    scanned_at: str = ""


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


class ActivityItem(BaseModel):
    id: int = 0
    cycle: int = 0
    agent: str = ""
    phase: str = ""
    message: str = ""
    details: str = ""
    timestamp: str = ""


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


class ConfigResponse(BaseModel):
    mode: str = "driving_range"
    strategy: dict = {}
    sizing: dict = {}
    risk: dict = {}
    orders: dict = {}
    scanner: dict = {}
    scoring: dict = {}
    paper: dict = {}
