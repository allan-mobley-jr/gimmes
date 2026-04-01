"""Tests for backtest report formatting."""

from __future__ import annotations

from datetime import date, datetime, UTC

import pytest

from gimmes.backtest.engine import BacktestConfig, BacktestResult, BacktestTrade
from gimmes.backtest.report import backtest_result_to_json, format_backtest_report
from gimmes.config import GimmesConfig, Mode


def _make_config() -> BacktestConfig:
    return BacktestConfig(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        starting_balance=10_000.0,
        gimmes_config=GimmesConfig(mode=Mode.DRIVING_RANGE),
    )


def _make_trade(
    *,
    ticker: str = "KXTEST",
    pnl: float = 1.50,
    result: str = "yes",
) -> BacktestTrade:
    return BacktestTrade(
        ticker=ticker,
        title="Test Market",
        side="yes",
        count=10,
        entry_price=0.65,
        cost_basis=6.50,
        fees=0.30,
        result=result,
        payout=10.0 if pnl > 0 else 0.0,
        pnl=pnl,
        entry_time=datetime(2025, 3, 15, tzinfo=UTC),
        settle_time=datetime(2025, 3, 20, tzinfo=UTC),
    )


def _make_result(
    trades: list[BacktestTrade] | None = None,
) -> BacktestResult:
    if trades is None:
        trades = [_make_trade(pnl=3.20), _make_trade(ticker="KXTEST2", pnl=-6.50, result="no")]
    return BacktestResult(
        config=_make_config(),
        trades=trades,
        final_balance=9_996.70,
        equity_curve=[
            ("2025-03-15T00:00:00+00:00", 10_003.20),
            ("2025-03-20T00:00:00+00:00", 9_996.70),
        ],
        markets_scanned=500,
        markets_passed_filter=50,
        markets_scored=10,
        markets_traded=2,
    )


class TestBacktestResultToJson:
    def test_basic_structure(self) -> None:
        result = _make_result()
        data = backtest_result_to_json(result)

        assert "config" in data
        assert "funnel" in data
        assert "summary" in data
        assert "trades" in data
        assert "equity_curve" in data

    def test_config_section(self) -> None:
        data = backtest_result_to_json(_make_result())
        assert data["config"]["start_date"] == "2025-01-01"
        assert data["config"]["end_date"] == "2025-12-31"
        assert data["config"]["starting_balance"] == 10_000.0

    def test_funnel_counts(self) -> None:
        data = backtest_result_to_json(_make_result())
        assert data["funnel"]["markets_scanned"] == 500
        assert data["funnel"]["markets_traded"] == 2

    def test_summary_metrics(self) -> None:
        data = backtest_result_to_json(_make_result())
        s = data["summary"]
        assert s["total_trades"] == 2
        assert s["wins"] == 1
        assert s["losses"] == 1
        assert 0 < s["win_rate"] < 1

    def test_trades_serialized(self) -> None:
        data = backtest_result_to_json(_make_result())
        assert len(data["trades"]) == 2
        t = data["trades"][0]
        assert "ticker" in t
        assert "pnl" in t
        assert "entry_time" in t

    def test_empty_trades(self) -> None:
        result = _make_result(trades=[])
        data = backtest_result_to_json(result)
        assert data["summary"]["total_trades"] == 0
        assert data["summary"]["win_rate"] == 0.0


class TestFormatBacktestReport:
    def test_prints_without_error(self) -> None:
        """Smoke test — just verify it doesn't crash."""
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        test_console = Console(file=buf, force_terminal=True, width=120)
        result = _make_result()
        format_backtest_report(result, test_console)
        output = buf.getvalue()
        assert "Backtest Config" in output
        assert "Performance Summary" in output

    def test_handles_no_trades(self) -> None:
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        test_console = Console(file=buf, force_terminal=True, width=120)
        result = _make_result(trades=[])
        format_backtest_report(result, test_console)
        output = buf.getvalue()
        assert "Performance Summary" in output
