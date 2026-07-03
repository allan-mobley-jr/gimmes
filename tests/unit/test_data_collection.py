"""Tests for data collection enhancements (issue #20).

Covers: migration v5, resolved_outcome on trades, component scores on candidates,
and the update_trade_outcome query.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from gimmes.store.database import Database
from gimmes.store.queries import (
    get_recent_candidates,
    get_trades,
    insert_candidate,
    insert_trade,
    update_trade_outcome,
)

if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from click.testing import Result


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    """Create a temporary database with schema + migrations."""
    database = Database(tmp_path / "test.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# Migration v5 — schema changes
# ---------------------------------------------------------------------------


class TestMigrationV5:
    async def test_schema_version_is_5(self, db: Database) -> None:
        cursor = await db.conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = await cursor.fetchone()
        assert row[0] >= 5

    async def test_trades_has_resolved_outcome(self, db: Database) -> None:
        cursor = await db.conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "resolved_outcome" in columns

    async def test_migration_idempotent(self, tmp_path: Path) -> None:
        """Running migrations twice doesn't fail (torn-state recovery)."""
        db1 = Database(tmp_path / "idem.db")
        await db1.connect()
        await db1.close()
        # Second connect re-runs migrations on the same DB
        db2 = Database(tmp_path / "idem.db")
        await db2.connect()
        cursor = await db2.conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row[0] >= 5
        await db2.close()

    async def test_candidates_has_component_scores(self, db: Database) -> None:
        cursor = await db.conn.execute("PRAGMA table_info(candidates)")
        columns = {row[1] for row in await cursor.fetchall()}
        for col in (
            "edge_size_score",
            "signal_strength_score",
            "liquidity_depth_score",
            "settlement_clarity_score",
            "time_to_resolution_score",
        ):
            assert col in columns


# ---------------------------------------------------------------------------
# Resolved outcome
# ---------------------------------------------------------------------------


class TestResolvedOutcome:
    async def test_update_trade_outcome(self, db: Database) -> None:
        from gimmes.models.trade import TradeDecision

        trade = TradeDecision(
            ticker="OUTCOME-TEST",
            action=TradeDecision.Action.OPEN,
            side="yes",
            count=10,
            price=0.65,
            model_probability=0.90,
            gimme_score=80,
            edge=0.25,
            rationale="test",
            agent="closer",
        )
        await insert_trade(db, trade)

        updated = await update_trade_outcome(db, "OUTCOME-TEST", "yes")
        assert updated == 1

        rows = await get_trades(db, ticker="OUTCOME-TEST")
        assert rows[0]["resolved_outcome"] == "yes"

    async def test_update_outcome_idempotent(self, db: Database) -> None:
        from gimmes.models.trade import TradeDecision

        trade = TradeDecision(
            ticker="IDEM-TEST",
            action=TradeDecision.Action.OPEN,
            side="yes",
            count=5,
            price=0.70,
            model_probability=0.85,
            gimme_score=75,
            edge=0.15,
            rationale="test",
            agent="closer",
        )
        await insert_trade(db, trade)

        await update_trade_outcome(db, "IDEM-TEST", "yes")
        # Second call should not update (already set)
        updated = await update_trade_outcome(db, "IDEM-TEST", "no")
        assert updated == 0

        rows = await get_trades(db, ticker="IDEM-TEST")
        assert rows[0]["resolved_outcome"] == "yes"  # unchanged

    async def test_update_outcome_no_match(self, db: Database) -> None:
        updated = await update_trade_outcome(db, "NONEXISTENT", "yes")
        assert updated == 0

    async def test_outcome_updates_all_actions(self, db: Database) -> None:
        """Outcome is recorded on all trade actions (open, close, skip)."""
        from gimmes.models.trade import TradeDecision

        actions = (TradeDecision.Action.OPEN, TradeDecision.Action.CLOSE, TradeDecision.Action.SKIP)
        for action in actions:
            await insert_trade(db, TradeDecision(
                ticker="MIXED-TEST",
                action=action,
                side="yes",
                count=10,
                price=0.65,
                model_probability=0.90,
                gimme_score=80,
                edge=0.25,
                rationale="test",
                agent="closer",
            ))

        updated = await update_trade_outcome(db, "MIXED-TEST", "yes")
        assert updated == 3

    async def test_invalid_outcome_rejected_by_check(self, db: Database) -> None:
        """CHECK constraint prevents invalid outcome values."""
        from gimmes.models.trade import TradeDecision

        await insert_trade(db, TradeDecision(
            ticker="CHECK-TEST",
            action=TradeDecision.Action.OPEN,
            side="yes",
            count=1,
            price=0.60,
            model_probability=0.80,
            gimme_score=70,
            edge=0.20,
            rationale="test",
            agent="closer",
        ))

        with pytest.raises(sqlite3.IntegrityError):
            await update_trade_outcome(db, "CHECK-TEST", "invalid")

    async def test_default_outcome_is_null(self, db: Database) -> None:
        from gimmes.models.trade import TradeDecision

        trade = TradeDecision(
            ticker="NULL-TEST",
            action=TradeDecision.Action.OPEN,
            side="yes",
            count=1,
            price=0.60,
            model_probability=0.80,
            gimme_score=70,
            edge=0.20,
            rationale="test",
            agent="closer",
        )
        await insert_trade(db, trade)

        rows = await get_trades(db, ticker="NULL-TEST")
        assert rows[0]["resolved_outcome"] is None


# ---------------------------------------------------------------------------
# Component scores
# ---------------------------------------------------------------------------


class TestComponentScores:
    async def test_insert_candidate_with_scores(self, db: Database) -> None:
        await insert_candidate(
            db, "COMP-TEST", "Component Test", 0.70, 0.90, 0.20, 85, "memo",
            edge_size_score=80.0,
            signal_strength_score=70.0,
            liquidity_depth_score=60.0,
            settlement_clarity_score=50.0,
            time_to_resolution_score=40.0,
        )

        rows = await get_recent_candidates(db)
        assert len(rows) == 1
        assert rows[0]["edge_size_score"] == 80.0
        assert rows[0]["signal_strength_score"] == 70.0
        assert rows[0]["liquidity_depth_score"] == 60.0
        assert rows[0]["settlement_clarity_score"] == 50.0
        assert rows[0]["time_to_resolution_score"] == 40.0

    async def test_insert_candidate_defaults_zero(self, db: Database) -> None:
        await insert_candidate(db, "DEF-TEST", "Default Test", 0.65, 0.85, 0.20, 78, "memo")

        rows = await get_recent_candidates(db)
        assert len(rows) == 1
        assert rows[0]["edge_size_score"] == 0
        assert rows[0]["signal_strength_score"] == 0

    async def test_insert_candidate_returns_row_id(self, db: Database) -> None:
        row_id = await insert_candidate(
            db, "ID-TEST", "Row ID Test", 0.60, 0.80, 0.20, 72, "memo",
        )
        assert isinstance(row_id, int)
        assert row_id > 0
        cursor = await db.conn.execute(
            "SELECT ticker FROM candidates WHERE id = ?", (row_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "ID-TEST"

    async def test_backward_compatible_insert(self, db: Database) -> None:
        """Old-style inserts without component scores still work."""
        await insert_candidate(db, "OLD-TEST", "Old Style", 0.60, 0.80, 0.20, 72, "memo")
        rows = await get_recent_candidates(db)
        assert len(rows) == 1
        assert rows[0]["ticker"] == "OLD-TEST"
        assert rows[0]["gimme_score"] == 72


# ---------------------------------------------------------------------------
# log-candidate CLI command
# ---------------------------------------------------------------------------


class TestLogCandidateCommand:
    def test_command_exists(self) -> None:
        from gimmes.cli import app

        commands = {cmd.name for cmd in app.registered_commands}
        assert "log-candidate" in commands

    def test_edge_calculation(self, tmp_path: Path) -> None:
        """Edge is computed as prob - price."""
        from unittest.mock import AsyncMock, patch

        from typer.testing import CliRunner

        from gimmes.cli import app

        mock_insert = AsyncMock(return_value=1)
        runner = CliRunner()

        with (
            patch("gimmes.cli.load_config") as mock_cfg,
            patch("gimmes.store.queries.insert_candidate", mock_insert),
        ):
            mock_cfg.return_value.db_path = tmp_path / "test.db"
            result = runner.invoke(app, [
                "log-candidate", "EDGE-TEST",
                "--price", "0.70", "--prob", "0.90", "--score", "85",
                "--memo", "test memo",
            ])

        assert result.exit_code == 0
        _, ticker, title, price, prob, edge, score, memo = mock_insert.call_args[0]
        assert ticker == "EDGE-TEST"
        assert abs(edge - 0.20) < 1e-9

    @staticmethod
    def _invoke_no_side(
        tmp_path: Path, argv: list[str],
    ) -> tuple[Result, AsyncMock]:
        """Run a log-candidate argv with the strategy side pinned to a
        real 'no' literal and insert_candidate mocked (#658 harness)."""
        from unittest.mock import AsyncMock, patch

        from typer.testing import CliRunner

        from gimmes.cli import app

        mock_insert = AsyncMock(return_value=1)
        runner = CliRunner()

        with (
            patch("gimmes.cli.load_config") as mock_cfg,
            patch("gimmes.store.queries.insert_candidate", mock_insert),
        ):
            mock_cfg.return_value.db_path = tmp_path / "test.db"
            mock_cfg.return_value.strategy.side = "no"
            result = runner.invoke(app, argv)
        return result, mock_insert

    def test_bound_price_clamps_edge_to_zero(self, tmp_path: Path) -> None:
        """#658: YES $1.00 on a BUY NO strategy is untradeable (NO
        costs $0.00) — the stored edge must be 0, not prob - 0 = +88%.
        A zero edge auto-fails Caddie Master's cm_min_edge_after_fees
        pre-filter."""
        result, mock_insert = self._invoke_no_side(tmp_path, [
            "log-candidate", "KXCPIYOY-26JUL-T3.7",
            "--price", "1.00", "--prob", "0.88", "--score", "80",
            "--memo", "bound test",
        ])

        assert result.exit_code == 0
        _, _, _, price, prob, edge, _, _ = mock_insert.call_args[0]
        assert price == 1.00
        assert prob == 0.88
        assert edge == 0.0

    def test_no_side_mid_range_edge(self, tmp_path: Path) -> None:
        """Side plumbing through log-candidate with a real 'no'
        literal at a NON-bound price: edge = prob - (1 - price).
        (The pre-existing test's MagicMock side pins neither literal;
        a double-conversion mutant survived it — #658 review.)"""
        result, mock_insert = self._invoke_no_side(tmp_path, [
            "log-candidate", "MIDRANGE-NO",
            "--price", "0.70", "--prob", "0.85", "--score", "80",
            "--memo", "mid-range no side",
        ])

        assert result.exit_code == 0
        edge = mock_insert.call_args[0][5]
        assert abs(edge - 0.55) < 1e-9  # 0.85 - (1 - 0.70)

    def test_memo_file_preserves_dollar_zero(self, tmp_path: Path) -> None:
        """Regression test for #589: --memo-file content with `$0.41` reaches
        the DB literally, not shell-expanded to `/bin/zsh.41`."""
        from unittest.mock import AsyncMock, patch

        from typer.testing import CliRunner

        from gimmes.cli import app

        memo_file = tmp_path / "memo.txt"
        memo_file.write_text(
            "Market prices YES at $0.41 even though base rate is 38%.\n"
            "Includes $VAR and `cmd` characters that should survive.",
        )

        mock_insert = AsyncMock(return_value=1)
        runner = CliRunner()
        with (
            patch("gimmes.cli.load_config") as mock_cfg,
            patch("gimmes.store.queries.insert_candidate", mock_insert),
        ):
            mock_cfg.return_value.db_path = tmp_path / "test.db"
            result = runner.invoke(app, [
                "log-candidate", "REPRO-589",
                "--price", "0.41", "--prob", "0.50", "--score", "78",
                "--memo-file", str(memo_file),
            ])

        assert result.exit_code == 0, result.output
        _, _ticker, _title, _price, _prob, _edge, _score, memo = (
            mock_insert.call_args[0]
        )
        assert "$0.41" in memo
        assert "$VAR" in memo
        assert "`cmd`" in memo
        assert "/bin/zsh" not in memo

    def test_memo_and_memo_file_mutex(self, tmp_path: Path) -> None:
        """Specifying both --memo and --memo-file is a hard error (#589)."""
        from typer.testing import CliRunner

        from gimmes.cli import app

        memo_file = tmp_path / "memo.txt"
        memo_file.write_text("file content")
        runner = CliRunner()
        result = runner.invoke(app, [
            "log-candidate", "MUTEX-TEST",
            "--price", "0.50", "--prob", "0.60", "--score", "70",
            "--memo", "inline content",
            "--memo-file", str(memo_file),
        ])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output.lower()

    def test_memo_file_not_found(self, tmp_path: Path) -> None:
        """Missing --memo-file path raises a clear error (#589)."""
        from typer.testing import CliRunner

        from gimmes.cli import app

        runner = CliRunner()
        result = runner.invoke(app, [
            "log-candidate", "MISSING-FILE",
            "--price", "0.50", "--prob", "0.60", "--score", "70",
            "--memo-file", str(tmp_path / "does-not-exist.txt"),
        ])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_memo_file_empty_rejected(self, tmp_path: Path) -> None:
        """An empty --memo-file is a contract violation (#589)."""
        from typer.testing import CliRunner

        from gimmes.cli import app

        memo_file = tmp_path / "empty.txt"
        memo_file.write_text("")
        runner = CliRunner()
        result = runner.invoke(app, [
            "log-candidate", "EMPTY-FILE",
            "--price", "0.50", "--prob", "0.60", "--score", "70",
            "--memo-file", str(memo_file),
        ])
        assert result.exit_code == 1
        assert "empty" in result.output.lower()


# ---------------------------------------------------------------------------
# log-trade --rationale-file (mirror tests for #589 — same risk surface as
# --memo-file but distinct wiring in log_trade)
# ---------------------------------------------------------------------------


class TestLogTradeRationaleFile:
    def test_rationale_file_preserves_dollar_zero(self, tmp_path: Path) -> None:
        """Regression for #589: --rationale-file content with `$0.41` reaches
        the trades.rationale column literally."""
        from unittest.mock import AsyncMock, patch

        from typer.testing import CliRunner

        from gimmes.cli import app

        rationale_file = tmp_path / "rationale.txt"
        rationale_file.write_text(
            "Close order failed: error at $0.41 with `cmd` substitution\n"
            "Includes $VAR reference that should survive.",
        )

        mock_insert = AsyncMock(return_value=1)
        runner = CliRunner()
        with (
            patch("gimmes.cli.load_config") as mock_cfg,
            patch("gimmes.store.queries.insert_trade", mock_insert),
        ):
            mock_cfg.return_value.db_path = tmp_path / "test.db"
            mock_cfg.return_value.strategy.side = "no"
            result = runner.invoke(app, [
                "log-trade", "REPRO-589",
                "--action", "skip",
                "--price", "0.41", "--prob", "0.50", "--score", "78",
                "--rationale-file", str(rationale_file),
            ])

        assert result.exit_code == 0, result.output
        trade_arg = mock_insert.call_args[0][1]
        assert "$0.41" in trade_arg.rationale
        assert "$VAR" in trade_arg.rationale
        assert "`cmd`" in trade_arg.rationale
        assert "/bin/zsh" not in trade_arg.rationale

    def test_rationale_and_rationale_file_mutex(self, tmp_path: Path) -> None:
        """Both --rationale and --rationale-file → exit 1 (#589)."""
        from typer.testing import CliRunner

        from gimmes.cli import app

        rationale_file = tmp_path / "rationale.txt"
        rationale_file.write_text("file content")
        runner = CliRunner()
        result = runner.invoke(app, [
            "log-trade", "MUTEX",
            "--action", "skip",
            "--price", "0.50", "--prob", "0.60", "--score", "70",
            "--rationale", "inline content",
            "--rationale-file", str(rationale_file),
        ])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output.lower()


# ---------------------------------------------------------------------------
# position-note --body-file (mirror tests for #589 — distinct wiring in
# position_note including the relaxed-required check)
# ---------------------------------------------------------------------------


class TestPositionNoteBodyFile:
    def test_body_file_preserves_dollar_zero(self, tmp_path: Path) -> None:
        """Regression for #589: --body-file content with `$0.41` reaches
        the position_notes.body column literally."""
        from unittest.mock import AsyncMock, patch

        from typer.testing import CliRunner

        from gimmes.cli import app

        body_file = tmp_path / "body.txt"
        body_file.write_text(
            "Decision: HOLD.\n"
            "Reasoning: price moved to $0.41 but thesis intact.\n"
            "Includes $VAR and `cmd` characters that should survive.",
        )

        mock_insert = AsyncMock(return_value=1)
        runner = CliRunner()
        with (
            patch("gimmes.cli.load_config") as mock_cfg,
            patch("gimmes.store.queries.insert_position_note", mock_insert),
        ):
            mock_cfg.return_value.db_path = tmp_path / "test.db"
            result = runner.invoke(app, [
                "position-note", "REPRO-589",
                "--cycle", "1",
                "--agent", "caddie-master",
                "--type", "decision",
                "--body-file", str(body_file),
            ])

        assert result.exit_code == 0, result.output
        body_arg = mock_insert.call_args.kwargs["body"]
        assert "$0.41" in body_arg
        assert "$VAR" in body_arg
        assert "`cmd`" in body_arg
        assert "/bin/zsh" not in body_arg

    def test_body_and_body_file_mutex(self, tmp_path: Path) -> None:
        """Both --body and --body-file → exit 1 (#589)."""
        from typer.testing import CliRunner

        from gimmes.cli import app

        body_file = tmp_path / "body.txt"
        body_file.write_text("file content")
        runner = CliRunner()
        result = runner.invoke(app, [
            "position-note", "MUTEX",
            "--cycle", "1",
            "--body", "inline content",
            "--body-file", str(body_file),
        ])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output.lower()

    def test_neither_body_nor_body_file_required(self, tmp_path: Path) -> None:
        """When neither --body nor --body-file is given, exit 1 with a
        clear error (#589 — replaces the old required-Option contract)."""
        from typer.testing import CliRunner

        from gimmes.cli import app

        runner = CliRunner()
        result = runner.invoke(app, [
            "position-note", "NEITHER",
            "--cycle", "1",
        ])
        assert result.exit_code == 1
        assert "required" in result.output.lower()

    def test_whitespace_only_body_rejected(self, tmp_path: Path) -> None:
        """`--body "   "` (whitespace-only) must NOT silently write — the
        in-function check strips before checking (#589)."""
        from typer.testing import CliRunner

        from gimmes.cli import app

        runner = CliRunner()
        result = runner.invoke(app, [
            "position-note", "WHITESPACE",
            "--cycle", "1",
            "--body", "   \n  ",
        ])
        assert result.exit_code == 1
        assert "required" in result.output.lower()


# ---------------------------------------------------------------------------
# End-to-end shell-tokenization regression (#589 — the strongest possible
# proof: invoke the actual CLI binary through a real shell with the value
# inside double quotes, verify $0 expansion does NOT corrupt the stored
# memo).
# ---------------------------------------------------------------------------


class TestShellTokenizationE2E:
    def test_memo_file_survives_real_shell_with_dollar_zero(
        self, tmp_path: Path,
    ) -> None:
        """Invoke `gimmes log-candidate --memo-file ...` through /bin/sh so
        argv tokenization happens in a real shell — the exact failure mode
        from #589. Asserts the stored memo contains literal `$0.41`.

        Strongest possible regression: if some future change re-routes the
        memo through a shell command line, this test would catch the
        corruption because the value is read from the production CLI binary,
        not the in-process CliRunner."""
        import os
        import sqlite3
        import subprocess
        import sys

        memo_file = tmp_path / "memo.txt"
        memo_file.write_text("Market prices YES at $0.41")

        # GIMMES_HOME steers the default db_path to tmp_path/gimmes.db
        # (see config.py:18 + 975). The CLI's `Database(...)` context
        # manager auto-creates the schema on first connect.
        env = os.environ.copy()
        env["GIMMES_HOME"] = str(tmp_path)
        env.pop("GIMMES_CONFIG", None)

        # Invoke via `sys.executable -m gimmes` rather than `uv run gimmes`:
        # avoids nested uv overhead, no external binary dependency, and
        # still goes through a real /bin/sh so argv tokenization happens
        # in a real shell — the exact failure mode from #589.
        cmd = (
            f'"{sys.executable}" -m gimmes log-candidate REPRO-589-E2E '
            f"--price 0.41 --prob 0.50 --score 78 "
            f'--memo-file "{memo_file}"'
        )
        result = subprocess.run(
            ["/bin/sh", "-c", cmd],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"CLI failed: stdout={result.stdout}\nstderr={result.stderr}"
        )

        db_path = tmp_path / "gimmes.db"
        assert db_path.exists(), f"DB not created at {db_path}"
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT research_memo FROM candidates WHERE ticker = ?",
                ("REPRO-589-E2E",),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, "candidate row not written"
        stored_memo = row[0]
        assert "$0.41" in stored_memo, f"got: {stored_memo!r}"
        assert "/bin/zsh" not in stored_memo, f"got: {stored_memo!r}"
