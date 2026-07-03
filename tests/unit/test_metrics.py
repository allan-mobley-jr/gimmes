"""Tests for performance metrics calculations."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from gimmes.reporting.metrics import (
    calculate_max_drawdown,
    calculate_metrics,
    calculate_sharpe_from_curve,
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


def _daily_curve(values: list[float]) -> list[tuple[str, float]]:
    """Timestamped daily curve for Sharpe tests."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        ((start + timedelta(days=i)).isoformat(), v)
        for i, v in enumerate(values)
    ]


class TestSharpeFromCurve:
    def test_steady_growth_positive(self) -> None:
        # Compounding growth with slight alternation so std > 0.
        values, v = [], 1000.0
        for i in range(20):
            v *= 1.02 if i % 2 == 0 else 1.005
            values.append(v)
        sharpe = calculate_sharpe_from_curve(_daily_curve([1000.0, *values]))
        assert sharpe > 0

    def test_variance_drag_shape_is_negative(self) -> None:
        """The #654 headline regression pin: alternating +30%/-25%
        compounds to a LOSS (1.3 * 0.75 = 0.975 per pair) while the
        arithmetic mean of simple returns is +2.5% — the old
        simple-returns Sharpe reported this POSITIVE."""
        values, v = [1000.0], 1000.0
        for i in range(30):
            v *= 1.30 if i % 2 == 0 else 0.75
            values.append(v)
        assert values[-1] < values[0]  # compounded loss
        sharpe = calculate_sharpe_from_curve(_daily_curve(values))
        assert sharpe < 0

    def test_annualization_uses_observed_frequency(self) -> None:
        """The core #654 defect pin: identical per-step returns spaced
        DAILY vs WEEKLY must annualize differently — by sqrt(7) — since
        the weekly series has 7x fewer periods per year. The old code
        applied sqrt(252) to both, treating a sparse settlement curve
        as if it were daily and inflating its Sharpe."""
        values, v = [1000.0], 1000.0
        for i in range(20):
            v *= 1.02 if i % 2 == 0 else 1.005
            values.append(v)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        daily = _daily_curve(values)
        weekly = [
            ((start + timedelta(days=7 * i)).isoformat(), val)
            for i, val in enumerate(values)
        ]
        s_daily = calculate_sharpe_from_curve(daily)
        s_weekly = calculate_sharpe_from_curve(weekly)
        assert s_daily > 0 and s_weekly > 0
        assert s_daily / s_weekly == pytest.approx(math.sqrt(7), rel=1e-9)

    def test_hand_computed_value(self) -> None:
        """Exactly two log returns, hand-computable end to end.
        Asymmetric so the mean is nonzero — a symmetric curve pins
        nothing (mean 0 zeroes out annualization and variance terms;
        #654 review found the first version let a ddof mutation
        survive)."""
        curve = _daily_curve([1000.0, 1100.0, 1050.0])  # 3 points, 2 days
        r1, r2 = math.log(1.1), math.log(1050.0 / 1100.0)
        mean = (r1 + r2) / 2
        var = ((r1 - mean) ** 2 + (r2 - mean) ** 2) / 1
        periods_per_year = 2 / (2 / 365.25)
        expected = mean / math.sqrt(var) * math.sqrt(periods_per_year)
        assert calculate_sharpe_from_curve(curve) == pytest.approx(expected)

    def test_empty_and_single_point(self) -> None:
        assert calculate_sharpe_from_curve([]) == 0.0
        assert calculate_sharpe_from_curve(
            [("2026-01-01T00:00:00+00:00", 1000.0)],
        ) == 0.0

    def test_constant_equity_zero_variance(self) -> None:
        assert calculate_sharpe_from_curve(
            _daily_curve([1000.0] * 5),
        ) == 0.0

    def test_zero_equity_points_skipped(self) -> None:
        """Pairs touching a 0.0 equity point are dropped; the two
        surviving returns (1000->1010, 1010->1000) compute normally."""
        curve = _daily_curve([1000.0, 0.0, 1000.0, 1010.0, 1000.0])
        r1, r2 = math.log(1.01), math.log(1000.0 / 1010.0)
        mean = (r1 + r2) / 2
        var = ((r1 - mean) ** 2 + (r2 - mean) ** 2) / 1
        periods_per_year = 2 / (4 / 365.25)
        expected = mean / math.sqrt(var) * math.sqrt(periods_per_year)
        assert calculate_sharpe_from_curve(curve) == pytest.approx(expected)

    def test_unparseable_timestamps_return_zero(self) -> None:
        curve = [("", 1000.0), ("", 1100.0), ("not-a-ts", 1050.0)]
        assert calculate_sharpe_from_curve(curve) == 0.0

    def test_zero_time_span_returns_zero(self) -> None:
        ts = "2026-01-01T00:00:00+00:00"
        curve = [(ts, 1000.0), (ts, 1100.0), (ts, 1050.0)]
        assert calculate_sharpe_from_curve(curve) == 0.0


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
