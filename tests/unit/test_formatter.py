"""Tests for gimmes.reporting.formatter."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from io import StringIO

import pytest
from rich.console import Console
from rich.table import Table

from gimmes.reporting.formatter import format_kv_table, format_local_timestamp


def _render_table(table: Table) -> str:
    """Render a table to text at a fixed 100-col width — wide enough
    that Rich wraps titles at the TABLE width, not the console width
    (a narrow console would split the asserted fragments)."""
    buf = StringIO()
    Console(file=buf, width=100).print(table)
    return buf.getvalue()


class TestFormatKvTable:
    def test_returns_table_with_correct_rows(self) -> None:
        rows = [("Key1", "Val1"), ("Key2", "Val2"), ("Key3", "Val3")]
        table = format_kv_table("My Title", rows)
        assert table.title == "My Title"
        assert table.row_count == 3

    def test_empty_rows(self) -> None:
        table = format_kv_table("Empty", [])
        assert table.row_count == 0

    def test_no_header(self) -> None:
        table = format_kv_table("T", [("k", "v")])
        assert table.show_header is False

    def test_title_with_brackets_survives_render(self) -> None:
        """Bracketed external text in the title must render verbatim
        (#644): the positive fragment catches markup-eating, the
        negative catches double-escaping (a pre-escaping caller would
        render a literal backslash — invisible to the positive check).
        """
        # Wide row: Rich wraps the title to the TABLE width, not
        # the console width — a narrow table would split the fragment.
        table = format_kv_table(
            "CPI [preliminary] April 2026",
            [("key", "value " * 8)],
        )
        out = _render_table(table)
        assert "[preliminary]" in out
        assert "\\[preliminary]" not in out

    def test_values_still_support_markup(self) -> None:
        """Row VALUES stay markup-parsed by contract (#644) — callers
        pass deliberate color tags (e.g. the Settlement Risk cell). A
        future blanket value-escape must fail here loudly."""
        table = format_kv_table("T", [("k", "[bold]42[/bold]")])
        out = _render_table(table)
        assert "42" in out
        assert "[bold]" not in out


@pytest.fixture()
def _tz_eastern() -> Iterator[None]:
    """Pin the local timezone to US/Eastern for deterministic tests."""
    orig = os.environ.get("TZ")
    os.environ["TZ"] = "US/Eastern"
    time.tzset()
    yield
    if orig is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = orig
    time.tzset()


@pytest.mark.usefixtures("_tz_eastern")
class TestFormatLocalTimestamp:
    def test_naive_utc_string(self) -> None:
        result = format_local_timestamp("2026-03-15 14:30:00")
        assert result == "2026-03-15 10:30:00"  # EDT is UTC-4

    def test_iso_with_utc_offset(self) -> None:
        result = format_local_timestamp("2026-03-15T14:30:00+00:00")
        assert result == "2026-03-15 10:30:00"

    def test_iso_with_z_suffix(self) -> None:
        result = format_local_timestamp("2026-03-15T14:30:00Z")
        assert result == "2026-03-15 10:30:00"

    def test_date_only(self) -> None:
        result = format_local_timestamp("2026-03-15 14:30:00", date_only=True)
        assert result == "2026-03-15"

    def test_date_boundary_crossing(self) -> None:
        # 2026-03-15 02:00 UTC -> 2026-03-14 22:00 EDT (previous day)
        result = format_local_timestamp("2026-03-15 02:00:00", date_only=True)
        assert result == "2026-03-14"

    def test_empty_string(self) -> None:
        assert format_local_timestamp("") == "--"

    def test_string_none_fallback(self) -> None:
        # str(None) == "None" -- a real scenario from dict.get() wrapping
        result = format_local_timestamp("None")
        assert result == "None"  # falls back to raw[:19]

    def test_garbage_input_fallback(self) -> None:
        result = format_local_timestamp("not-a-timestamp-at-all")
        assert result == "not-a-timestamp-at-"  # falls back to [:19]

    def test_winter_offset(self) -> None:
        # January -> EST (UTC-5), not EDT
        result = format_local_timestamp("2026-01-15 14:30:00")
        assert result == "2026-01-15 09:30:00"


class TestFormatPnlSummary:
    """#653/#663: the Close Events/Open Positions rows make the P&L table
    internally consistent — pin the arithmetic."""

    def test_closed_and_open_rows(self) -> None:
        from io import StringIO
        from unittest.mock import patch

        from rich.console import Console

        from gimmes.reporting.formatter import format_pnl_summary
        from gimmes.reporting.pnl import PnLSummary

        summary = PnLSummary(
            total_trades=5, open_trades=1, winning_trades=2,
            losing_trades=1, scratch_trades=1,
        )
        buf = StringIO()
        with patch(
            "gimmes.reporting.formatter.console",
            Console(file=buf, width=100),
        ):
            format_pnl_summary(summary)
        out = buf.getvalue()
        assert "Close Events" in out and "Open Positions" in out
        # Close Events = W+L+S = 4; Open = 1; Total = 5.
        lines = {
            line.split("│")[1].strip(): line.split("│")[2].strip()
            for line in out.splitlines()
            if line.count("│") >= 3
        }
        assert lines.get("Close Events") == "4"
        assert lines.get("Open Positions") == "1"
        assert lines.get("Total Trades") == "5"


class TestStopColumn:
    """#659: the Stop column computes stop-gate consumption so agents
    read a number instead of doing arithmetic; at >= 200% a
    `StopGate: N% MANDATORY-CLOSE` banner prints BELOW the table —
    banners, unlike table cells, survive the width-80 non-TTY default
    agents see (cells wrap and ellipsize long content)."""

    @staticmethod
    def _render(
        positions: list[dict], stop_loss_pct=0.15, width=160, **kwargs,
    ) -> str:
        from io import StringIO
        from unittest.mock import patch

        from rich.console import Console

        from gimmes.reporting.formatter import format_positions

        buf = StringIO()
        with patch(
            "gimmes.reporting.formatter.console",
            Console(file=buf, width=width),
        ):
            format_positions(
                positions, stop_loss_pct=stop_loss_pct, **kwargs,
            )
        return buf.getvalue()

    @staticmethod
    def _pos(pnl: float, cost_basis: float = 100.0, ticker="KXTEST") -> dict:
        return {
            "ticker": ticker, "side": "no", "count": 10,
            "avg_price": 0.55, "market_price": 0.40,
            "unrealized_pnl": pnl, "cost_basis": cost_basis,
        }

    def test_below_gate_renders_percentage(self) -> None:
        # loss $7 on $100 basis at 15% stop -> 47% of gate
        out = self._render([self._pos(-7.0)])
        assert "47%" in out
        assert "MANDATORY-CLOSE" not in out

    def test_breached_gate_renders_over_100(self) -> None:
        # loss $19.80 -> 132% of the $15 gate
        out = self._render([self._pos(-19.80)])
        assert "132%" in out
        assert "MANDATORY-CLOSE" not in out

    def test_double_gate_prints_banner(self) -> None:
        # loss $32.10 -> 214% of the $15 gate
        out = self._render([self._pos(-32.10)])
        assert "214%" in out
        assert "KXTEST StopGate: 214% MANDATORY-CLOSE" in out

    def test_exactly_200_prints_banner(self) -> None:
        out = self._render([self._pos(-30.0)])
        assert "KXTEST StopGate: 200% MANDATORY-CLOSE" in out

    def test_rounding_cannot_split_display_from_marker(self) -> None:
        # 199.6% of gate rounds to 200 -> the banner MUST fire (the
        # display and the threshold share the rounded value; a raw
        # comparison would render 200% with no marker).
        out = self._render([self._pos(-29.94)])
        assert "200%" in out
        assert "MANDATORY-CLOSE" in out

    def test_banner_survives_width_80_with_long_ticker(self) -> None:
        """THE load-bearing property (#659 review): at the width-80
        non-TTY default agents get, table cells ellipsize — the
        banner line must carry the literal intact."""
        ticker = "KXJOBLESSCLAIMS-26MAY14-210000"
        out = self._render(
            [self._pos(-32.10, ticker=ticker)], width=80,
        )
        assert f"{ticker} StopGate: 214% MANDATORY-CLOSE" in out

    def test_profitable_position_renders_dash(self) -> None:
        out = self._render([self._pos(12.0)])
        assert "MANDATORY-CLOSE" not in out
        assert "—" in out

    def test_losing_zero_basis_is_loud_not_dash(self) -> None:
        """A losing position with no cost basis is a data bug — it
        must not silently exempt itself from the backstop (#659
        review)."""
        out = self._render([self._pos(-5.0, cost_basis=0.0)])
        assert "ERR" in out
        assert "DATA-ERROR" in out

    def test_none_stop_pct_keeps_legacy_table(self) -> None:
        out = self._render([self._pos(-19.80)], stop_loss_pct=None)
        assert "Stop" not in out
        assert "MANDATORY-CLOSE" not in out

    def test_nonpositive_stop_pct_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="stop_loss_pct"):
            self._render([self._pos(-5.0)], stop_loss_pct=0.0)

    def test_data_error_banner_survives_width_80(self) -> None:
        ticker = "KXJOBLESSCLAIMS-26MAY14-210000"
        out = self._render(
            [self._pos(-5.0, cost_basis=0.0, ticker=ticker)], width=80,
        )
        assert f"{ticker} StopGate: DATA-ERROR" in out


class TestStaleAndSuspectMarkers:
    """#674: STALE (mark failed / dead book) and BASIS-SUSPECT
    (partial-close-corrupted live cost basis) ride the same banner
    machinery as MANDATORY-CLOSE/DATA-ERROR — non-numeric values that
    trip Caddie Master's conservative rule."""

    _render = staticmethod(TestStopColumn._render)
    _pos = staticmethod(TestStopColumn._pos)

    def test_stale_losing_position(self) -> None:
        out = self._render(
            [self._pos(-7.0)], stale_tickers={"KXTEST"},
        )
        assert "STALE" in out
        assert "KXTEST StopGate: STALE" in out
        assert "47%" not in out  # frozen pct must not invite anchoring

    def test_stale_does_not_suppress_breach_banner(self) -> None:
        """Fail-safe: the last-good mark showed >=200% — staleness
        cannot rescind a breach."""
        out = self._render(
            [self._pos(-32.10)], stale_tickers={"KXTEST"},
        )
        assert "KXTEST StopGate: STALE" in out
        assert "214% MANDATORY-CLOSE" in out

    def test_stale_winner_still_flagged(self) -> None:
        """A stale 'winning' mark can hide a loss."""
        out = self._render(
            [self._pos(5.0)], stale_tickers={"KXTEST"},
        )
        assert "KXTEST StopGate: STALE" in out

    def test_suspect_position(self) -> None:
        out = self._render(
            [self._pos(-7.0)], suspect_tickers={"KXTEST"},
        )
        assert "SUSP" in out
        assert "KXTEST StopGate: BASIS-SUSPECT" in out

    def test_stale_wins_cell_both_banners_emitted(self) -> None:
        out = self._render(
            [self._pos(-7.0)],
            stale_tickers={"KXTEST"}, suspect_tickers={"KXTEST"},
        )
        assert "KXTEST StopGate: STALE" in out
        assert "KXTEST StopGate: BASIS-SUSPECT" in out
        # Cell-level precedence: STALE occupies the Stop cell (mark
        # trust precedes basis trust). Banners print below the table,
        # so split there; BASIS-SUSPECT contains "SUSP", hence the
        # table-portion check.
        table_part = out.split("StopGate")[0]
        assert "STALE" in table_part
        assert "SUSP" not in table_part

    def test_banners_survive_width_80_with_long_ticker(self) -> None:
        long_ticker = "KXJOBLESSCLAIMS-26MAY14-210000"
        out = self._render(
            [self._pos(-7.0, ticker=long_ticker)],
            width=80,
            stale_tickers={long_ticker},
            suspect_tickers={long_ticker},
        )
        assert f"{long_ticker} StopGate: STALE" in out
        assert f"{long_ticker} StopGate: BASIS-SUSPECT" in out

    def test_defaults_leave_legacy_output_unchanged(self) -> None:
        plain = self._render([self._pos(-7.0)])
        explicit = self._render(
            [self._pos(-7.0)],
            stale_tickers=set(), suspect_tickers=set(),
        )
        assert plain == explicit
        assert "STALE" not in plain
