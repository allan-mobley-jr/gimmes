"""Tests for performance metrics calculations."""

from __future__ import annotations

from gimmes.reporting.metrics import (
    calculate_max_drawdown,
    calculate_metrics,
    calculate_sharpe,
)


class TestMaxDrawdown:
    def test_simple_drawdown(self) -> None:
        # 1000 → 1200 → 900 → 1100
        curve = [1000.0, 1200.0, 900.0, 1100.0]
        dd, dd_pct = calculate_max_drawdown(curve)
        assert dd == 300.0  # 1200 → 900
        assert abs(dd_pct - 0.25) < 0.001  # 300/1200

    def test_no_drawdown(self) -> None:
        curve = [100.0, 200.0, 300.0, 400.0]
        dd, dd_pct = calculate_max_drawdown(curve)
        assert dd == 0.0
        assert dd_pct == 0.0

    def test_single_point(self) -> None:
        dd, dd_pct = calculate_max_drawdown([100.0])
        assert dd == 0.0

    def test_empty_curve(self) -> None:
        dd, dd_pct = calculate_max_drawdown([])
        assert dd == 0.0

    def test_monotonic_decline(self) -> None:
        curve = [1000.0, 800.0, 600.0, 400.0]
        dd, dd_pct = calculate_max_drawdown(curve)
        assert dd == 600.0
        assert abs(dd_pct - 0.6) < 0.001


class TestSharpe:
    def test_positive_returns(self) -> None:
        returns = [0.01, 0.02, 0.01, 0.015, 0.005]
        sharpe = calculate_sharpe(returns)
        assert sharpe > 0

    def test_zero_returns(self) -> None:
        returns = [0.0, 0.0, 0.0]
        sharpe = calculate_sharpe(returns)
        assert sharpe == 0.0

    def test_single_return(self) -> None:
        sharpe = calculate_sharpe([0.05])
        assert sharpe == 0.0

    def test_empty_returns(self) -> None:
        sharpe = calculate_sharpe([])
        assert sharpe == 0.0

    def test_negative_returns(self) -> None:
        returns = [-0.01, -0.02, -0.01, -0.015]
        sharpe = calculate_sharpe(returns)
        assert sharpe < 0


class TestCalculateMetrics:
    def test_win_rate_from_pnl(self) -> None:
        trades = [
            {"action": "open", "ticker": "A", "price": 0.60, "count": 10, "edge": 0.1},
            {"action": "close", "ticker": "A", "price": 0.80, "count": 10},
            {"action": "open", "ticker": "B", "price": 0.70, "count": 5, "edge": 0.1},
            {"action": "close", "ticker": "B", "price": 0.50, "count": 5},
        ]
        metrics = calculate_metrics(trades, [])
        assert abs(metrics.win_rate - 0.5) < 0.001  # 1 win, 1 loss

    def test_no_trades(self) -> None:
        metrics = calculate_metrics([], [])
        assert metrics.win_rate == 0.0

    def test_avg_edge_predicted(self) -> None:
        trades = [
            {"action": "open", "ticker": "A", "edge": 0.10},
            {"action": "open", "ticker": "B", "edge": 0.20},
        ]
        metrics = calculate_metrics(trades, [])
        assert abs(metrics.avg_edge_predicted - 0.15) < 0.001

    def test_total_return(self) -> None:
        snapshots = [
            {"total_equity": 10000},
            {"total_equity": 10500},
            {"total_equity": 11000},
        ]
        metrics = calculate_metrics([], snapshots, initial_bankroll=10000)
        assert metrics.total_return == 1000.0
        assert abs(metrics.total_return_pct - 0.10) < 0.001

    def test_drawdown_from_snapshots(self) -> None:
        snapshots = [
            {"total_equity": 10000},
            {"total_equity": 12000},
            {"total_equity": 9000},
            {"total_equity": 11000},
        ]
        metrics = calculate_metrics([], snapshots)
        assert metrics.max_drawdown == 3000.0
