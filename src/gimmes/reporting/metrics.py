"""Performance metrics: win rate, edge accuracy, max drawdown, Sharpe."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime


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
    equity_curve: list[dict] = field(default_factory=list)  # type: ignore[type-arg]


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


def calculate_sharpe_from_curve(
    curve: list[tuple[str, float]],
    risk_free_rate: float = 0.0,
) -> float:
    """Annualized Sharpe ratio from a timestamped equity curve (#654).

    Computed on LOG returns of consecutive equity points, annualized by
    the OBSERVED frequency (periods per year derived from the curve's
    actual time span) — never an assumed 252 trading days. Two
    properties this buys over the old daily-assuming simple-returns
    version:

    - The sign matches the compounded total return (mean log return
      telescopes to ln(final/initial)/N), so a strategy that lost
      money can never report a positive Sharpe via variance drag —
      the exact #654 symptom (+1.11 on a -14% backtest). Caveat: the
      telescoping (and hence the sign identity) only holds when every
      consecutive pair of equity points is positive; pairs touching a
      non-positive value are dropped, not bridged, so a curve that
      dips to zero or below can break the identity.
    - Sampling frequency doesn't inflate the value: per-settlement
      curves (dozens of events over months) annualize by their real
      cadence, not sqrt(252).

    Irregular event spacing is approximated as evenly spaced at the
    span's average frequency; a daily-resampled upgrade path exists if
    that ever matters.

    Args:
        curve: (ISO timestamp, equity) points, chronological.
        risk_free_rate: ANNUAL risk-free rate (converted per-period
            internally as a simple division; exact treatment would use
            ln(1+rf) — immaterial at realistic rates, zero by default).

    Returns 0.0 when undefined: fewer than 2 usable log returns,
    zero variance, non-positive equity pairs, unparseable timestamps,
    or a non-positive time span.
    """
    if len(curve) < 2:
        return 0.0

    log_returns: list[float] = []
    for (_, prev), (_, cur) in zip(curve, curve[1:], strict=False):
        if prev > 0 and cur > 0:
            log_returns.append(math.log(cur / prev))
    if len(log_returns) < 2:
        return 0.0

    try:
        t0 = datetime.fromisoformat(curve[0][0])
        t1 = datetime.fromisoformat(curve[-1][0])
        # Normalize naive timestamps to UTC — a naive/aware mix would
        # TypeError on subtraction (#654 review; Market.close_time can
        # arrive naive in principle).
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=UTC)
        if t1.tzinfo is None:
            t1 = t1.replace(tzinfo=UTC)
        years = (t1 - t0).total_seconds() / (365.25 * 24 * 3600)
    except (ValueError, TypeError):
        return 0.0
    if years <= 0:
        return 0.0
    periods_per_year = len(log_returns) / years

    rf_per_period = risk_free_rate / periods_per_year
    excess = [r - rf_per_period for r in log_returns]
    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    if variance <= 0:
        return 0.0

    return (mean / math.sqrt(variance)) * math.sqrt(periods_per_year)


def _equity_curve_from_trades(
    trades: list[dict],  # type: ignore[type-arg]
    initial_bankroll: float,
) -> list[dict]:  # type: ignore[type-arg]
    """Build a cash-balance equity curve from trade history."""
    cash = initial_bankroll
    curve: list[dict] = []  # type: ignore[type-arg]
    for t in sorted(trades, key=lambda x: x.get("timestamp", "")):
        action = t.get("action", "")
        cost = t.get("count", 0) * t.get("price", 0.0)
        if action in ("open", "size_up"):
            cash -= cost
        elif action == "close":
            cash += cost
        else:
            continue
        curve.append({"timestamp": t.get("timestamp", ""), "equity": cash})
    return curve


def _apply_equity_curve(
    metrics: PerformanceMetrics,
    curve: list[dict],  # type: ignore[type-arg]
    initial_bankroll: float,
) -> None:
    """Populate drawdown, return, Sharpe, and equity_curve on *metrics*."""
    equity_values = [pt["equity"] for pt in curve]
    metrics.max_drawdown, metrics.max_drawdown_pct = calculate_max_drawdown(
        equity_values
    )
    if initial_bankroll > 0:
        metrics.total_return = equity_values[-1] - initial_bankroll
        metrics.total_return_pct = metrics.total_return / initial_bankroll
    if len(equity_values) >= 2:
        # #654: time-aware Sharpe — snapshot cadence, not assumed daily.
        metrics.sharpe_ratio = calculate_sharpe_from_curve([
            (str(pt.get("timestamp", "")), float(pt["equity"]))
            for pt in curve
        ])
    metrics.equity_curve = curve


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

    # Equity curve — prefer snapshots, fall back to trade-derived curve
    if snapshots:
        curve = [
            {"timestamp": s.get("timestamp", ""), "equity": s.get("total_equity", 0)}
            for s in snapshots
        ]
        _apply_equity_curve(metrics, curve, initial_bankroll)
    elif trades and initial_bankroll > 0:
        curve = _equity_curve_from_trades(trades, initial_bankroll)
        if curve:
            _apply_equity_curve(metrics, curve, initial_bankroll)

    return metrics
