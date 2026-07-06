"""Tests for backtest report formatting."""

from __future__ import annotations

from datetime import UTC, date, datetime

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
        assert "Usable entry-day views" in output  # #666 funnel row

    def test_handles_no_trades(self) -> None:
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        test_console = Console(file=buf, force_terminal=True, width=120)
        result = _make_result(trades=[])
        format_backtest_report(result, test_console)
        output = buf.getvalue()
        assert "Performance Summary" in output

    def test_truncation_warning_displayed(self) -> None:
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        test_console = Console(file=buf, force_terminal=True, width=120)
        result = _make_result()
        result.truncated_chunks = ["KXINX (2025-01)", "KXNASDAQ100 (2025-02)"]
        format_backtest_report(result, test_console)
        output = buf.getvalue()
        assert "pagination limit reached" in output.lower()
        assert "KXINX" in output

    def test_no_truncation_warning_when_empty(self) -> None:
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        test_console = Console(file=buf, force_terminal=True, width=120)
        result = _make_result()
        format_backtest_report(result, test_console)
        output = buf.getvalue()
        assert "pagination limit" not in output.lower()


class TestTruncationInJson:
    def test_truncated_chunks_in_json(self) -> None:
        result = _make_result()
        result.truncated_chunks = ["KXINX (2025-03)"]
        data = backtest_result_to_json(result)
        assert data["funnel"]["truncated_chunks"] == ["KXINX (2025-03)"]

    def test_empty_truncated_chunks_in_json(self) -> None:
        result = _make_result()
        data = backtest_result_to_json(result)
        assert data["funnel"]["truncated_chunks"] == []


class TestSharpeSignMatchesRoi:
    """#654: under log-return Sharpe, sign(sharpe) == sign(total
    compounded return) is an identity — a losing backtest can never
    again report a positive Sharpe (the +1.11-on-minus-14% headline)."""

    def test_losing_backtest_negative_sharpe(self) -> None:
        result = _make_result()  # final 9,996.70 < 10,000 start
        data = backtest_result_to_json(result)
        assert data["summary"]["net_pnl"] < 0
        assert data["summary"]["sharpe"] < 0

    def test_winning_backtest_positive_sharpe(self) -> None:
        result = _make_result()
        result.final_balance = 10_006.50
        result.equity_curve = [
            ("2025-03-15T00:00:00+00:00", 9_996.80),
            ("2025-03-20T00:00:00+00:00", 10_006.50),
        ]
        data = backtest_result_to_json(result)
        assert data["summary"]["sharpe"] > 0


class TestSkipCountersInReport:
    """#655: the new funnel counters must reach the JSON output and the
    coverage warning must fire when candle history is mostly missing."""

    def test_json_carries_skip_counters(self) -> None:
        result = _make_result()
        result.skipped_no_candle = 3
        result.skipped_one_sided = 2
        result.skipped_entry_gates = 5
        data = backtest_result_to_json(result)
        assert data["funnel"]["skipped_no_candle"] == 3
        assert data["funnel"]["skipped_one_sided"] == 2
        assert data["funnel"]["skipped_entry_gates"] == 5

    def test_fetch_failures_render_red_warning(self) -> None:
        """#666: FAILED fetches are an API-problem signal (the #655
        endpoint 404 signature), never silent data sparsity."""
        from io import StringIO

        from rich.console import Console

        result = _make_result()
        result.fetch_failures = 7
        buf = StringIO()
        format_backtest_report(result, Console(file=buf, width=120))
        out = buf.getvalue()
        assert "FAILED" in out
        assert "API problem" in out

    def test_json_carries_fetch_failures(self) -> None:
        result = _make_result()
        result.fetch_failures = 3
        data = backtest_result_to_json(result)
        assert data["funnel"]["fetch_failures"] == 3

    def test_zero_passed_note_names_the_lens(self) -> None:
        """#666: passed=0 with usable views must explain itself —
        entry-day values are typically lower than settlement."""
        from io import StringIO

        from rich.console import Console

        result = _make_result()
        result.markets_passed_filter = 0
        buf = StringIO()
        format_backtest_report(result, Console(file=buf, width=120))
        assert "ENTRY-DAY values" in buf.getvalue()

    def test_one_sided_funnel_row_renders(self) -> None:
        from io import StringIO

        from rich.console import Console

        result = _make_result()
        result.skipped_one_sided = 4
        buf = StringIO()
        format_backtest_report(result, Console(file=buf, width=120))
        assert "one-sided" in buf.getvalue()

    def test_coverage_warning_fires_above_half(self) -> None:
        from io import StringIO

        from rich.console import Console

        # #666: the caution denominator is the SCANNED universe now —
        # candle skips happen before scoring.
        result = _make_result()
        result.markets_scanned = 10
        result.skipped_no_candle = 4
        result.skipped_one_sided = 2
        buf = StringIO()
        format_backtest_report(result, Console(file=buf, width=120))
        assert "Caution" in buf.getvalue()

    def test_no_warning_at_or_below_half(self) -> None:
        from io import StringIO

        from rich.console import Console

        result = _make_result()
        result.markets_scanned = 10
        result.skipped_no_candle = 3
        result.skipped_one_sided = 2
        buf = StringIO()
        format_backtest_report(result, Console(file=buf, width=120))
        assert "Caution" not in buf.getvalue()
