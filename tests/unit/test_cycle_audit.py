"""Tests for gimmes.reporting.cycle_audit (#546 Phase 0)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from gimmes.reporting.cycle_audit import (
    CycleSummary,
    audit_date,
    parse_cycle_log,
    query_trades_in_window,
    render_markdown,
)


def _write_cycle(path: Path, events: list[dict]) -> None:
    path.write_text(json.dumps(events))


def _assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": "2026-05-07T00:30:00.000Z",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _user_event(ts: str = "2026-05-07T00:00:00.000Z") -> dict:
    return {"type": "user", "timestamp": ts}


def _result_event(is_error: bool = False) -> dict:
    return {"type": "result", "is_error": is_error}


class TestParseCycleLog:
    def test_full_cycle_with_scout_caddie_proceed(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-1327.json"
        _write_cycle(log, [
            _user_event("2026-05-06T23:50:09.582Z"),
            _assistant_text(
                "Scout returned 7 candidates. Step 4: Caddie. PROCEED PROCEED PROCEED"
            ),
            {"type": "user", "timestamp": "2026-05-07T00:21:00.053Z"},
            _result_event(),
        ])
        s = parse_cycle_log(log)
        assert s.cycle_id == 1327
        assert s.cycle_type == "full"
        assert s.scout_shortlist_size == 7
        assert s.caddie_threshold_passes == 3
        # Caddie dispatch detected via "Step 4" pattern.
        assert s.caddie_dispatches == 1
        assert s.start_time == datetime(2026, 5, 6, 23, 50, 9, 582_000, tzinfo=UTC)
        assert s.end_time == datetime(2026, 5, 7, 0, 21, 0, 53_000, tzinfo=UTC)

    def test_monitor_cycle_no_assistant_text(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-1.json"
        _write_cycle(log, [_user_event(), _result_event()])
        s = parse_cycle_log(log)
        assert s.cycle_type == "monitor"
        assert s.scout_shortlist_size is None
        assert s.caddie_threshold_passes is None

    def test_errored_cycle_marked(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-2.json"
        _write_cycle(log, [
            _user_event(),
            _assistant_text("partial output"),
            _result_event(is_error=True),
        ])
        s = parse_cycle_log(log)
        assert s.cycle_type == "errored"

    def test_unreadable_file_returns_errored_summary(
        self, tmp_path: Path,
    ) -> None:
        log = tmp_path / "cycle-3.json"
        log.write_text("{not valid json")
        s = parse_cycle_log(log)
        assert s.cycle_type == "errored"
        assert any("unreadable" in w for w in s.parse_warnings)

    def test_scout_wording_drift_fallback(self, tmp_path: Path) -> None:
        """When the primary 'Scout returned N candidates' wording isn't
        used, the fallback patterns should still extract the count."""
        log = tmp_path / "cycle-4.json"
        _write_cycle(log, [
            _user_event(),
            _assistant_text("Scout shortlisted 9 markets. PROCEED"),
            _result_event(),
        ])
        s = parse_cycle_log(log)
        assert s.scout_shortlist_size == 9

    def test_unknown_when_no_scout_marker(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-5.json"
        _write_cycle(log, [
            _user_event(),
            _assistant_text("nothing matching the regex here"),
            _result_event(),
        ])
        s = parse_cycle_log(log)
        assert s.scout_shortlist_size is None

    def test_handles_non_dict_events_in_list(self, tmp_path: Path) -> None:
        """A malformed log with a stray non-dict element in the events list
        must not raise — the parser is contracted to fail-open."""
        log = tmp_path / "cycle-7.json"
        events = [
            "stray string",
            123,
            None,
            _user_event(),
            _assistant_text("Scout returned 4 candidates"),
            _result_event(),
        ]
        log.write_text(json.dumps(events))
        s = parse_cycle_log(log)  # must not raise
        assert s.cycle_type == "full"
        assert s.scout_shortlist_size == 4

    def test_block_log_marked_as_block_type(self, tmp_path: Path) -> None:
        block = tmp_path / "cycle-1348-block-1778146600.json"
        block.write_text(json.dumps({
            "type": "budget_cap_block",
            "reason": "cost",
            "message": "Daily cost cap reached",
            "seconds_until_reset": 51799,
        }))
        s = parse_cycle_log(block)
        assert s.cycle_type == "block"
        assert s.cycle_id == 1348


class TestQueryTradesInWindow:
    def _seed_trades_db(self, tmp_path: Path) -> Path:
        db = tmp_path / "gimmes.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                side TEXT NOT NULL DEFAULT 'yes',
                count INTEGER NOT NULL DEFAULT 0,
                price REAL NOT NULL DEFAULT 0,
                model_probability REAL NOT NULL DEFAULT 0,
                gimme_score REAL NOT NULL DEFAULT 0,
                edge REAL NOT NULL DEFAULT 0,
                kelly_fraction REAL NOT NULL DEFAULT 0,
                rationale TEXT NOT NULL DEFAULT '',
                agent TEXT NOT NULL DEFAULT '',
                order_id TEXT NOT NULL DEFAULT '',
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                resolved_outcome TEXT,
                thesis TEXT NOT NULL DEFAULT ''
            )
        """)
        rows = [
            ("KX1", "open", "2026-05-07T00:18:00+00:00", "closer"),
            ("KX2", "skip", "2026-05-07T01:00:00+00:00", "scout"),
            ("KX3", "open", "2026-05-07T03:07:00+00:00", "closer"),
            ("KX4", "open", "2026-05-07T13:00:00+00:00", "closer"),  # outside
        ]
        for ticker, action, ts, agent in rows:
            conn.execute(
                "INSERT INTO trades (ticker, action, timestamp, agent) "
                "VALUES (?, ?, ?, ?)",
                (ticker, action, ts, agent),
            )
        conn.commit()
        conn.close()
        return db

    def test_counts_only_placed_orders_in_window(self, tmp_path: Path) -> None:
        db = self._seed_trades_db(tmp_path)
        n = query_trades_in_window(
            db,
            datetime(2026, 5, 7, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
        )
        # KX1 + KX3 → 2; KX2 (skip) excluded; KX4 (outside) excluded.
        assert n == 2

    def test_returns_zero_when_db_missing(self, tmp_path: Path) -> None:
        n = query_trades_in_window(
            tmp_path / "does-not-exist.db",
            datetime(2026, 5, 7, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 7, 12, 0, tzinfo=UTC),
        )
        assert n == 0

    def test_db_error_surfaces_in_cycle_warnings(self, tmp_path: Path) -> None:
        """A missing/locked DB must surface as a warning on the cycle, not
        silently flip the H5 verdict to ACCEPTED via a bogus zero count."""
        log = tmp_path / "cycle-1.json"
        _write_cycle(log, [
            {"type": "user", "timestamp": "2026-05-07T00:00:00.000Z"},
            _assistant_text("Scout returned 5 candidates"),
            {"type": "user", "timestamp": "2026-05-07T00:30:00.000Z"},
            _result_event(),
        ])
        s = parse_cycle_log(log, db_path=tmp_path / "missing.db")
        assert s.trades_placed_db == 0
        assert any("trades-DB query failed" in w for w in s.parse_warnings)


class TestAuditDate:
    def test_includes_prior_evening_cycle_that_spans_into_target_day(
        self, tmp_path: Path,
    ) -> None:
        """A cycle starting on 2026-05-06 23:50 UTC and ending 2026-05-07
        00:21 UTC must be included in a 2026-05-07 audit because its trade
        window spilled into the target date."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        # Cycle that spans the boundary.
        _write_cycle(log_dir / "cycle-1327.json", [
            {"type": "user", "timestamp": "2026-05-06T23:50:00.000Z"},
            _assistant_text("Scout returned 7 candidates. PROCEED"),
            {"type": "user", "timestamp": "2026-05-07T00:21:00.000Z"},
            _result_event(),
        ])
        # Cycle clearly on prior day, NOT spanning.
        _write_cycle(log_dir / "cycle-1326.json", [
            {"type": "user", "timestamp": "2026-05-06T18:00:00.000Z"},
            _assistant_text("Scout returned 3 candidates"),
            {"type": "user", "timestamp": "2026-05-06T18:30:00.000Z"},
            _result_event(),
        ])
        # Cycle clearly on target day.
        _write_cycle(log_dir / "cycle-1335.json", [
            {"type": "user", "timestamp": "2026-05-07T04:00:00.000Z"},
            _assistant_text("Scout returned 5 candidates"),
            {"type": "user", "timestamp": "2026-05-07T04:30:00.000Z"},
            _result_event(),
        ])

        result = audit_date(
            log_dir=log_dir, db_path=None, target_date=date(2026, 5, 7),
        )
        ids = [s.cycle_id for s in result]
        assert 1327 in ids, "boundary-spanning cycle must be included"
        assert 1335 in ids
        assert 1326 not in ids, (
            "cycle that fully ended before the target day must not be included"
        )


class TestRenderMarkdown:
    def test_h5_rejected_when_overnight_has_trades_and_pre_release_does_not(
        self, tmp_path: Path,
    ) -> None:
        # Two overnight cycles (22:00 EDT, 23:00 EDT — both in 8 PM-2 AM EDT
        # window): one with a trade, one without. One pre-release cycle
        # (05:00 EDT, in 5-8 AM EDT window): no trade.
        summaries = [
            CycleSummary(
                cycle_id=1, log_path=tmp_path / "x", cycle_type="full",
                start_time=datetime(2026, 5, 7, 2, 0, tzinfo=UTC),  # 22:00 EDT
                end_time=datetime(2026, 5, 7, 2, 30, tzinfo=UTC),
                duration_seconds=1800,
                scout_shortlist_size=7, caddie_dispatches=1,
                caddie_threshold_passes=3, trades_placed_db=1,
                trades_placed_text=0, in_trade_window=True,
            ),
            CycleSummary(
                cycle_id=2, log_path=tmp_path / "x",  cycle_type="full",
                start_time=datetime(2026, 5, 7, 3, 0, tzinfo=UTC),  # 23:00 EDT
                end_time=datetime(2026, 5, 7, 3, 30, tzinfo=UTC),
                duration_seconds=1800,
                scout_shortlist_size=5, caddie_dispatches=1,
                caddie_threshold_passes=0, trades_placed_db=0,
                trades_placed_text=0, in_trade_window=True,
            ),
            CycleSummary(
                cycle_id=3, log_path=tmp_path / "x",  cycle_type="full",
                start_time=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),  # 05:00 EDT
                end_time=datetime(2026, 5, 7, 9, 30, tzinfo=UTC),
                duration_seconds=1800,
                scout_shortlist_size=2, caddie_dispatches=1,
                caddie_threshold_passes=0, trades_placed_db=0,
                trades_placed_text=0, in_trade_window=True,
            ),
        ]
        md = render_markdown(summaries=summaries, target_date=date(2026, 5, 7))
        assert "H5: REJECTED" in md
        assert "Phase 0 of #546" in md
        assert "## Per-cycle audit" in md
        assert "## Aggregate by hour-of-window (EDT)" in md
        assert "## Deferred follow-up" in md
        assert "#553" in md  # Phase 1 deferred issue
        assert "#554" in md  # Phase 2/3 deferred issue

    def test_render_is_deterministic(self, tmp_path: Path) -> None:
        summaries = [
            CycleSummary(
                cycle_id=1, log_path=tmp_path / "x", cycle_type="full",
                start_time=datetime(2026, 5, 7, 0, 0, tzinfo=UTC),
                end_time=datetime(2026, 5, 7, 0, 30, tzinfo=UTC),
                duration_seconds=1800,
                scout_shortlist_size=5, caddie_dispatches=1,
                caddie_threshold_passes=2, trades_placed_db=0,
                trades_placed_text=None, in_trade_window=False,
            ),
        ]
        a = render_markdown(summaries=summaries, target_date=date(2026, 5, 7))
        b = render_markdown(summaries=summaries, target_date=date(2026, 5, 7))
        assert a == b

    def test_inconclusive_when_zero_cycles(self) -> None:
        md = render_markdown(summaries=[], target_date=date(2026, 5, 7))
        assert "No cycles found" in md

    @pytest.mark.parametrize(
        "overnight_trades,pre_release_trades,has_pre_release_cycles,"
        "expected_substr",
        [
            # Both empty → INCONCLUSIVE-zero
            (0, 0, True, "INCONCLUSIVE"),
            # Only pre-release has trades → ACCEPTED
            (0, 1, True, "ACCEPTED"),
            # Only overnight has trades, no pre-release coverage → REJECTED
            (1, 0, False, "REJECTED for overnight bucket; pre-release uninformed"),
            # Both ran but only overnight produced → REJECTED one-day data
            (1, 0, True, "REJECTED (one-day data)"),
            # Both produced → PARTIALLY REJECTED
            (1, 1, True, "PARTIALLY REJECTED"),
        ],
    )
    def test_h5_verdict_branches(
        self, tmp_path: Path,
        overnight_trades: int,
        pre_release_trades: int,
        has_pre_release_cycles: bool,
        expected_substr: str,
    ) -> None:
        """All five non-empty H5 verdict branches must produce the expected
        verdict substring. Guards against typos in any branch's wording."""
        # Always seed at least one overnight cycle so we exercise the
        # non-empty bucket path. Trade count is on the first overnight cycle.
        summaries = [
            CycleSummary(
                cycle_id=1, log_path=tmp_path / "x", cycle_type="full",
                start_time=datetime(2026, 5, 7, 2, 0, tzinfo=UTC),  # 22:00 EDT
                end_time=datetime(2026, 5, 7, 2, 30, tzinfo=UTC),
                duration_seconds=1800,
                scout_shortlist_size=1, caddie_dispatches=1,
                caddie_threshold_passes=overnight_trades,
                trades_placed_db=overnight_trades, trades_placed_text=None,
                in_trade_window=True,
            ),
        ]
        if has_pre_release_cycles:
            summaries.append(
                CycleSummary(
                    cycle_id=2, log_path=tmp_path / "x", cycle_type="full",
                    start_time=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),  # 5 AM EDT
                    end_time=datetime(2026, 5, 7, 9, 30, tzinfo=UTC),
                    duration_seconds=1800,
                    scout_shortlist_size=1, caddie_dispatches=1,
                    caddie_threshold_passes=0,
                    trades_placed_db=pre_release_trades,
                    trades_placed_text=None, in_trade_window=True,
                ),
            )
        md = render_markdown(summaries=summaries, target_date=date(2026, 5, 7))
        assert expected_substr in md, md.split("\n")[3]

    def test_render_invariant_under_input_reordering(
        self, tmp_path: Path,
    ) -> None:
        """Reversing the input summaries must produce the same Markdown
        byte-for-byte — including the per-cycle table, which depends on
        iteration order. Catches a regression that drops the internal
        sort in ``render_markdown``."""
        summaries = [
            CycleSummary(
                cycle_id=1, log_path=tmp_path / "x", cycle_type="full",
                start_time=datetime(2026, 5, 7, 2, 0, tzinfo=UTC),
                end_time=datetime(2026, 5, 7, 2, 30, tzinfo=UTC),
                duration_seconds=1800,
                scout_shortlist_size=5, caddie_dispatches=1,
                caddie_threshold_passes=2, trades_placed_db=1,
                trades_placed_text=None, in_trade_window=True,
            ),
            CycleSummary(
                cycle_id=2, log_path=tmp_path / "x", cycle_type="full",
                start_time=datetime(2026, 5, 7, 3, 0, tzinfo=UTC),
                end_time=datetime(2026, 5, 7, 3, 30, tzinfo=UTC),
                duration_seconds=1800,
                scout_shortlist_size=3, caddie_dispatches=1,
                caddie_threshold_passes=0, trades_placed_db=0,
                trades_placed_text=None, in_trade_window=True,
            ),
        ]
        a = render_markdown(summaries=summaries, target_date=date(2026, 5, 7))
        b = render_markdown(
            summaries=list(reversed(summaries)),
            target_date=date(2026, 5, 7),
        )
        assert a == b, (
            "render_markdown must produce byte-identical output regardless "
            "of caller-supplied order"
        )

    def test_aggregate_hour_bucket_math(self, tmp_path: Path) -> None:
        """Hour aggregate row must show count, trades, and trades/cycle
        correctly. Catches typos that would swap the columns."""
        summaries = [
            CycleSummary(
                cycle_id=1, log_path=tmp_path / "x", cycle_type="full",
                start_time=datetime(2026, 5, 7, 2, 0, tzinfo=UTC),  # 22 EDT
                end_time=datetime(2026, 5, 7, 2, 30, tzinfo=UTC),
                duration_seconds=1800,
                scout_shortlist_size=1, caddie_dispatches=1,
                caddie_threshold_passes=1, trades_placed_db=1,
                trades_placed_text=None, in_trade_window=True,
            ),
            CycleSummary(
                cycle_id=2, log_path=tmp_path / "x", cycle_type="full",
                start_time=datetime(2026, 5, 7, 2, 30, tzinfo=UTC),  # 22 EDT
                end_time=datetime(2026, 5, 7, 3, 0, tzinfo=UTC),
                duration_seconds=1800,
                scout_shortlist_size=1, caddie_dispatches=1,
                caddie_threshold_passes=0, trades_placed_db=0,
                trades_placed_text=None, in_trade_window=True,
            ),
        ]
        md = render_markdown(summaries=summaries, target_date=date(2026, 5, 7))
        # Hour 22 EDT row: 2 cycles, 1 trade, 0.50 trades/cycle.
        assert "| 22:00 | 2 | 1 | 0.50 |" in md


class TestCliAuditCycles:
    def test_invalid_date_exits_with_error(
        self, tmp_path: Path,
    ) -> None:
        from typer.testing import CliRunner

        from gimmes.cli import app
        runner = CliRunner()
        result = runner.invoke(
            app, ["audit-cycles", "--date", "not-a-date", "--output", "-"],
        )
        assert result.exit_code == 1
        assert "Invalid --date" in result.output

    def test_missing_logs_dir_exits_with_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typer.testing import CliRunner

        from gimmes.cli import app
        gimmes_home = tmp_path / "empty"
        gimmes_home.mkdir()
        # No logs/ subdir under gimmes_home → command should error out cleanly.
        monkeypatch.setattr("gimmes.config.GIMMES_HOME", gimmes_home)
        runner = CliRunner()
        result = runner.invoke(
            app, ["audit-cycles", "--date", "2026-05-07", "--output", "-"],
        )
        assert result.exit_code == 1
        assert "No logs directory" in result.output

    def test_command_writes_report_to_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from typer.testing import CliRunner

        from gimmes.cli import app

        gimmes_home = tmp_path / "gimmes_home"
        (gimmes_home / "logs").mkdir(parents=True)
        _write_cycle(gimmes_home / "logs" / "cycle-100.json", [
            {"type": "user", "timestamp": "2026-05-07T01:00:00.000Z"},
            _assistant_text("Scout returned 4 candidates. PROCEED"),
            {"type": "user", "timestamp": "2026-05-07T01:30:00.000Z"},
            _result_event(),
        ])
        monkeypatch.setattr("gimmes.config.GIMMES_HOME", gimmes_home)
        monkeypatch.setattr("gimmes.cli.GIMMES_HOME", gimmes_home, raising=False)

        out_file = tmp_path / "report.md"
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["audit-cycles", "--date", "2026-05-07", "--output", str(out_file)],
        )
        assert result.exit_code == 0, result.output
        assert out_file.exists()
        content = out_file.read_text()
        assert "# 2026-05-07 Cycle-Staleness Audit" in content
        assert "cycle-100" not in content  # we render cycle ids, not paths
        assert "100" in content
