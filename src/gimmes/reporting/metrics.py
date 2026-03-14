"""Performance metrics: win rate, edge accuracy, max drawdown, Sharpe."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class PerformanceMetrics:
    """Trading performance metrics."""

    win_rate: float = 0.0
    avg_edge_predicted: float = 0.0
    avg_edge_realized: float = 0.0
    edge_accuracy: float = 0.0  # realized / predicted
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    total_return: float = 0.0
    total_return_pct: float = 0.0


def calculate_max_drawdown(equity_curve: list[float]) -> tuple[float, float]:
    """Calculate maximum drawdown from an equity curve.

    Returns:
        Tuple of (max_drawdown_dollars, max_drawdown_pct).
    """
    if len(equity_curve) < 2:
        return 0.0, 0.0

    peak = equity_curve[0]
    max_dd = 0.0
    max_dd_pct = 0.0

    for value in equity_curve:
        if value > peak:
            peak = value
        dd = peak - value
        dd_pct = dd / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        max_dd_pct = max(max_dd_pct, dd_pct)

    return max_dd, max_dd_pct


def calculate_sharpe(returns: list[float], risk_free_rate: float = 0.0) -> float:
    """Calculate annualized Sharpe ratio from daily returns.

    Args:
        returns: List of daily returns (e.g., [0.01, -0.005, 0.02]).
        risk_free_rate: Daily risk-free rate.
    """
    if len(returns) < 2:
        return 0.0

    excess = [r - risk_free_rate for r in returns]
    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0

    if std == 0:
        return 0.0

    # Annualize (assuming ~252 trading days)
    return (mean / std) * math.sqrt(252)


def calculate_metrics(
    trades: list[dict],  # type: ignore[type-arg]
    snapshots: list[dict],  # type: ignore[type-arg]
    initial_bankroll: float = 0.0,
) -> PerformanceMetrics:
    """Calculate performance metrics from trades and snapshots."""
    metrics = PerformanceMetrics()

    # Win rate — based on realized P&L (matching pnl.py definition)
    # Group opens by ticker for P&L calculation
    open_prices: dict[str, float] = {}
    for t in trades:
        if t.get("action") == "open":
            open_prices.setdefault(t.get("ticker", ""), t.get("price", 0.0))

    wins = 0
    losses = 0
    for t in trades:
        if t.get("action") != "close":
            continue
        close_price = t.get("price", 0.0)
        open_price = open_prices.get(t.get("ticker", ""), 0.0)
        pnl = (close_price - open_price) * t.get("count", 0)
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    total = wins + losses
    metrics.win_rate = wins / total if total > 0 else 0.0

    # Edge accuracy
    predicted_edges = [t.get("edge", 0) for t in trades if t.get("action") == "open"]
    if predicted_edges:
        metrics.avg_edge_predicted = sum(predicted_edges) / len(predicted_edges)

    # Equity curve from snapshots
    if snapshots:
        equity_curve = [s.get("total_equity", 0) for s in snapshots]
        if equity_curve:
            metrics.max_drawdown, metrics.max_drawdown_pct = calculate_max_drawdown(equity_curve)

            if initial_bankroll > 0 and equity_curve:
                metrics.total_return = equity_curve[-1] - initial_bankroll
                metrics.total_return_pct = metrics.total_return / initial_bankroll

            # Daily returns for Sharpe
            if len(equity_curve) >= 2:
                daily_returns = [
                    (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
                    for i in range(1, len(equity_curve))
                    if equity_curve[i - 1] > 0
                ]
                metrics.sharpe_ratio = calculate_sharpe(daily_returns)

    return metrics
