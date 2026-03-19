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

    def test_equity_curve_from_snapshots(self) -> None:
        snapshots = [
            {"timestamp": "2026-03-18T10:00:00", "total_equity": 10000},
            {"timestamp": "2026-03-18T11:00:00", "total_equity": 10500},
        ]
        metrics = calculate_metrics([], snapshots, initial_bankroll=10000)
        assert len(metrics.equity_curve) == 2
        assert metrics.equity_curve[0]["equity"] == 10000
        assert metrics.equity_curve[1]["equity"] == 10500

    def test_equity_curve_fallback_from_trades(self) -> None:
        trades = [
            {"action": "open", "ticker": "A", "price": 0.60, "count": 10,
             "timestamp": "2026-03-18T10:00:00"},
            {"action": "close", "ticker": "A", "price": 0.80, "count": 10,
             "timestamp": "2026-03-18T11:00:00"},
        ]
        metrics = calculate_metrics(trades, [], initial_bankroll=100.0)
        assert len(metrics.equity_curve) == 2
        # After open: 100 - (10 * 0.60) = 94.0
        assert metrics.equity_curve[0]["equity"] == 94.0
        # After close: 94 + (10 * 0.80) = 102.0
        assert metrics.equity_curve[1]["equity"] == 102.0
        assert metrics.total_return == 2.0

    def test_equity_curve_fallback_with_size_up(self) -> None:
        trades = [
            {"action": "open", "ticker": "A", "price": 0.60, "count": 10,
             "timestamp": "2026-03-18T10:00:00"},
            {"action": "size_up", "ticker": "A", "price": 0.65, "count": 5,
             "timestamp": "2026-03-18T10:30:00"},
            {"action": "close", "ticker": "A", "price": 0.80, "count": 15,
             "timestamp": "2026-03-18T11:00:00"},
        ]
        metrics = calculate_metrics(trades, [], initial_bankroll=100.0)
        assert len(metrics.equity_curve) == 3
        # open: 100 - 6.0 = 94.0
        assert metrics.equity_curve[0]["equity"] == 94.0
        # size_up: 94 - 3.25 = 90.75
        assert metrics.equity_curve[1]["equity"] == 90.75
        # close: 90.75 + 12.0 = 102.75
        assert metrics.equity_curve[2]["equity"] == 102.75

    def test_equity_curve_empty_when_no_data(self) -> None:
        metrics = calculate_metrics([], [])
        assert metrics.equity_curve == []

    def test_equity_curve_fallback_skips_without_bankroll(self) -> None:
        trades = [
            {"action": "open", "ticker": "A", "price": 0.60, "count": 10,
             "timestamp": "2026-03-18T10:00:00"},
        ]
        metrics = calculate_metrics(trades, [], initial_bankroll=0)
        assert metrics.equity_curve == []
