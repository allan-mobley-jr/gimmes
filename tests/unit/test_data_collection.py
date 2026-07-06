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


class TestDetectCandidateFlip:
    """#660: the probability-flip detector. The canonical case is
    KXCPI-26JUN-T-0.2 rows 944 -> 947 (0.98 -> 0.02 on a 1c move)."""

    @staticmethod
    def _detect(
        prior_prob=0.98, prior_price=0.41,
        prior_scanned_at="2026-06-24 20:28:01",
        new_prob=0.02, new_price=0.40,
        now_iso="2026-06-24T22:55:00+00:00",
    ):
        from datetime import datetime

        from gimmes.store.observation_validator import (
            detect_candidate_flip,
        )

        return detect_candidate_flip(
            prior_prob=prior_prob, prior_price=prior_price,
            prior_scanned_at=prior_scanned_at,
            new_prob=new_prob, new_price=new_price,
            now=datetime.fromisoformat(now_iso),
        )

    def test_canonical_inversion_fires_with_signature(self) -> None:
        [w] = self._detect()
        assert "INVERSION SIGNATURE" in w
        assert "#660" in w and "#641" in w
        assert "Rules (primary)" in w

    def test_big_price_move_is_legit_reassessment(self) -> None:
        assert self._detect(
            prior_prob=0.90, prior_price=0.85,
            new_prob=0.30, new_price=0.30,
        ) == []

    def test_non_signature_flip_gets_generic_warning(self) -> None:
        [w] = self._detect(prior_prob=0.95, new_prob=0.30)
        assert "PROBABILITY INSTABILITY" in w
        assert "INVERSION SIGNATURE" not in w

    def test_stale_prior_skipped(self) -> None:
        assert self._detect(
            prior_scanned_at="2026-06-21 20:28:01",  # 73h+ old
        ) == []

    def test_degenerate_prior_skipped(self) -> None:
        assert self._detect(prior_prob=0.0) == []
        assert self._detect(prior_price=0.0) == []
        assert self._detect(new_prob=0.0) == []

    def test_unparseable_scanned_at_fails_open(self) -> None:
        assert self._detect(prior_scanned_at="not-a-time") == []

    def test_exactly_50pp_does_not_fire(self) -> None:
        assert self._detect(prior_prob=0.80, new_prob=0.30) == []
        assert self._detect(prior_prob=0.81, new_prob=0.30) != []

    def test_complement_price_does_not_satisfy_the_price_gate(
        self,
    ) -> None:
        """A confused agent logs the COMPLEMENT price alongside the
        inverted probability (observed live: $0.40 -> $0.63) — that
        is the inversion itself, not a market move (#660 review)."""
        [w] = self._detect(
            prior_prob=0.98, prior_price=0.40,
            new_prob=0.02, new_price=0.63,
        )
        assert "INVERSION SIGNATURE" in w

    def test_genuine_large_move_still_skips(self) -> None:
        # Non-complement large price move stays a legit re-assessment
        # (complement of 0.90 would be 0.10; 0.40 is 30c away from it)
        assert self._detect(
            prior_prob=0.90, prior_price=0.90,
            new_prob=0.30, new_price=0.40,
        ) == []

    def test_genuine_move_near_complement_not_flagged(self) -> None:
        """A real repricing that happens to land near the prior's
        complement must not fire the complement bypass unless the
        PROBABILITY also carries the inversion signature — else the
        message misstates facts (#660 review)."""
        # 25c genuine move; 0.60 sits 5c from complement 0.65, but
        # prob 0.90 -> 0.35 is not the inversion signature (1-0.90=0.10)
        assert self._detect(
            prior_prob=0.90, prior_price=0.35,
            new_prob=0.35, new_price=0.60,
        ) == []

    def test_complement_price_message_names_the_price_inversion(
        self,
    ) -> None:
        """In the complement-price bypass the raw delta is large —
        the message must not claim a small market move (#660
        Copilot review)."""
        [w] = self._detect(
            prior_prob=0.98, prior_price=0.40,
            new_prob=0.02, new_price=0.63,
        )
        assert "logged side-inverted" in w
        assert "moved only" not in w

    def test_banner_deduped_per_ticker(self, tmp_path) -> None:
        """Three flagged rows for one ticker -> one banner."""
        from unittest.mock import patch

        from typer.testing import CliRunner

        from gimmes.cli import app

        db_path = tmp_path / "gimmes.db"
        guard = TestLogCandidateFlipGuard
        guard._invoke(db_path, "0.41", "0.98")
        guard._invoke(db_path, "0.40", "0.02")
        guard._invoke(db_path, "0.41", "0.97")
        guard._invoke(db_path, "0.40", "0.03")

        with patch("gimmes.config.GIMMES_HOME", tmp_path):
            result = CliRunner().invoke(app, [
                "candidates", "--ticker", "KXCPI-26JUN-T-0.2",
                "--limit", "10",
            ])
        assert result.exit_code == 0, result.output
        assert result.output.count("FLIP-WARN:") == 1

    def test_zero_new_price_skipped(self) -> None:
        assert self._detect(new_price=0.0) == []

    def test_warning_avoids_scorer_red_flag_keywords(self) -> None:
        """scorer.py keyword-scans memos for settlement red flags —
        the prepended warning must not depress clarity scores."""
        for warning in (
            self._detect() + self._detect(prior_prob=0.95, new_prob=0.30)
        ):
            lowered = warning.lower()
            for keyword in (
                "carveout", "carve-out", "discretion", "subjective",
                "ambiguous", "unclear",
            ):
                assert keyword not in lowered, (keyword, warning)


class TestLogCandidateFlipGuard:
    """#660 end-to-end: log-candidate warns, annotates the memo, and
    the candidates command surfaces FLIP-WARN in Status."""

    @staticmethod
    def _invoke(db_path, price: str, prob: str):
        from unittest.mock import patch

        from typer.testing import CliRunner

        from gimmes.cli import app

        with patch("gimmes.cli.load_config") as mock_cfg:
            mock_cfg.return_value.db_path = db_path
            mock_cfg.return_value.strategy.side = "no"
            return CliRunner().invoke(app, [
                "log-candidate", "KXCPI-26JUN-T-0.2",
                "--price", price, "--prob", prob, "--score", "80",
                "--memo", "scoring memo",
            ])

    def test_flip_warns_and_annotates_memo(self, tmp_path) -> None:
        import asyncio

        from gimmes.store.queries import get_candidate_for_ticker

        db_path = tmp_path / "test.db"
        r1 = self._invoke(db_path, "0.41", "0.98")
        assert r1.exit_code == 0, r1.output
        assert "FLIP-WARNING" not in r1.output

        r2 = self._invoke(db_path, "0.40", "0.02")
        assert r2.exit_code == 0, r2.output
        # The BRACKETED literal is what caddie.md keys on — Rich must
        # render it intact (uppercase tags are not parsed as markup).
        assert "[FLIP-WARNING]" in r2.output
        assert "INVERSION SIGNATURE" in r2.output

        async def _rows():
            async with Database(db_path) as db:
                return await get_candidate_for_ticker(
                    db, "KXCPI-26JUN-T-0.2", limit=2,
                )

        rows = asyncio.run(_rows())
        latest = rows[0]
        assert latest["research_memo"].startswith("[FLIP-WARNING]")
        assert "scoring memo" in latest["research_memo"]
        # The first row stays unannotated
        assert rows[1]["research_memo"] == "scoring memo"

    def test_stable_rescore_unannotated(self, tmp_path) -> None:
        db_path = tmp_path / "test.db"
        self._invoke(db_path, "0.41", "0.98")
        r2 = self._invoke(db_path, "0.40", "0.95")
        assert r2.exit_code == 0, r2.output
        assert "FLIP-WARNING" not in r2.output

    def test_degenerate_row_does_not_reset_flip_baseline(
        self, tmp_path,
    ) -> None:
        """#676: caddie.md's mandated market-info-failure row
        (--price 0 --prob 0) is bookkeeping, not a scoring — it must
        not consume the flip baseline (at limit=1 it hid the real
        prior and the inversion sailed through)."""
        db_path = tmp_path / "test.db"
        self._invoke(db_path, "0.41", "0.98")
        r_deg = self._invoke(db_path, "0", "0")
        assert r_deg.exit_code == 0, r_deg.output
        r3 = self._invoke(db_path, "0.40", "0.02")
        assert r3.exit_code == 0, r3.output
        assert "[FLIP-WARNING]" in r3.output
        assert "INVERSION SIGNATURE" in r3.output

    def test_degenerate_row_then_stable_rescore_stays_clean(
        self, tmp_path,
    ) -> None:
        """The selection must not manufacture a false positive."""
        db_path = tmp_path / "test.db"
        self._invoke(db_path, "0.41", "0.98")
        self._invoke(db_path, "0", "0")
        r3 = self._invoke(db_path, "0.40", "0.95")
        assert r3.exit_code == 0, r3.output
        assert "FLIP-WARNING" not in r3.output

    def test_all_priors_degenerate_fails_open(self, tmp_path) -> None:
        """All-degenerate window → no usable baseline → warn-only
        path fails open (exit 0, no warning)."""
        db_path = tmp_path / "test.db"
        for _ in range(3):
            self._invoke(db_path, "0", "0")
        r = self._invoke(db_path, "0.40", "0.02")
        assert r.exit_code == 0, r.output
        assert "FLIP-WARNING" not in r.output

    def test_half_degenerate_row_does_not_reset_baseline(
        self, tmp_path,
    ) -> None:
        """A row with a real price but failed prob (or vice versa) is
        still bookkeeping — the scored filter requires BOTH."""
        db_path = tmp_path / "test.db"
        self._invoke(db_path, "0.41", "0.98")
        self._invoke(db_path, "0.41", "0")  # prob failed, price known
        r3 = self._invoke(db_path, "0.40", "0.02")
        assert r3.exit_code == 0, r3.output
        assert "[FLIP-WARNING]" in r3.output

    def test_two_consecutive_failure_rows_do_not_reset_baseline(
        self, tmp_path,
    ) -> None:
        """A multi-cycle market-info outage stacks several mandated
        failure rows — the SQL filter finds the real baseline no
        matter how many sit above it."""
        db_path = tmp_path / "test.db"
        self._invoke(db_path, "0.41", "0.98")
        self._invoke(db_path, "0", "0")
        self._invoke(db_path, "0", "0")
        r4 = self._invoke(db_path, "0.40", "0.02")
        assert r4.exit_code == 0, r4.output
        assert "[FLIP-WARNING]" in r4.output

    def test_newest_scored_prior_is_the_baseline(
        self, tmp_path,
    ) -> None:
        """Two usable priors that disagree: the NEWEST one is the
        baseline (an oldest-first mutant would fire a false flip
        against the ancient row)."""
        db_path = tmp_path / "test.db"
        self._invoke(db_path, "0.41", "0.02")   # ancient view
        self._invoke(db_path, "0.41", "0.98")   # newest scoring (warns; ignore)
        r3 = self._invoke(db_path, "0.40", "0.95")
        assert r3.exit_code == 0, r3.output
        assert "FLIP-WARNING" not in r3.output

    def test_candidates_status_shows_flip_warn(self, tmp_path) -> None:
        from unittest.mock import patch

        from typer.testing import CliRunner

        from gimmes.cli import app

        # ``candidates`` opens ``Database()`` bare -> GIMMES_HOME
        # fallback, so both the writes and the read must target the
        # same tmp home (the TestTradesCommand pattern).
        db_path = tmp_path / "gimmes.db"
        self._invoke(db_path, "0.41", "0.98")
        self._invoke(db_path, "0.40", "0.02")

        with patch("gimmes.config.GIMMES_HOME", tmp_path):
            result = CliRunner().invoke(app, [
                "candidates", "--ticker", "KXCPI-26JUN-T-0.2",
                "--limit", "5",
            ])
        assert result.exit_code == 0, result.output
        # The banner below the table carries the load-bearing string —
        # cells ellipsize at the width-80 non-TTY default (#659 lesson)
        assert "KXCPI-26JUN-T-0.2 FLIP-WARN:" in result.output


class TestCandidatesMemoPanel:
    """#676: single-ticker mode prints the newest row's research memo
    below the banners — caddie-master's 4c derivation rule reads the
    memo through this command, and the table has no memo column."""

    @staticmethod
    def _log(db_path, memo: str, prob: str = "0.9"):
        from unittest.mock import patch

        from typer.testing import CliRunner

        from gimmes.cli import app

        with patch("gimmes.cli.load_config") as mock_cfg:
            mock_cfg.return_value.db_path = db_path
            mock_cfg.return_value.strategy.side = "no"
            return CliRunner().invoke(app, [
                "log-candidate", "KXCPI-26JUN-T-0.2",
                "--price", "0.41", "--prob", prob, "--score", "80",
                "--memo", memo,
            ])

    @staticmethod
    def _candidates(tmp_path, *args):
        from unittest.mock import patch

        from typer.testing import CliRunner

        from gimmes.cli import app

        with patch("gimmes.config.GIMMES_HOME", tmp_path):
            return CliRunner().invoke(app, ["candidates", *args])

    def test_ticker_mode_prints_newest_memo_in_full(
        self, tmp_path,
    ) -> None:
        db_path = tmp_path / "gimmes.db"
        self._log(db_path, "old memo with OLDSENTINEL word")
        self._log(
            db_path,
            "Fresh research: CPI prints at 8:30 ET; consensus 0.2;"
            " sources reviewed; conclusion holds NEWSENTINEL",
            prob="0.88",
        )
        result = self._candidates(
            tmp_path, "--ticker", "KXCPI-26JUN-T-0.2", "--limit", "5",
        )
        assert result.exit_code == 0, result.output
        assert "RESEARCH MEMO" in result.output
        # Wrap-safe single-word sentinels (#659 width lesson).
        assert "NEWSENTINEL" in result.output
        assert "OLDSENTINEL" not in result.output

    def test_list_mode_prints_no_memo(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        self._log(db_path, "memo text")
        result = self._candidates(tmp_path)
        assert result.exit_code == 0, result.output
        assert "RESEARCH MEMO" not in result.output

    def test_empty_memo_placeholder(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        self._log(db_path, "")
        result = self._candidates(
            tmp_path, "--ticker", "KXCPI-26JUN-T-0.2",
        )
        assert result.exit_code == 0, result.output
        assert "No memo stored" in result.output

    def test_bookkeeping_row_does_not_hide_real_memo(
        self, tmp_path,
    ) -> None:
        """#676 review: a newest bookkeeping row with an empty memo
        must not hide the real research memo underneath it."""
        db_path = tmp_path / "gimmes.db"
        self._log(db_path, "real research REALSENTINEL here")
        self._log(db_path, "", prob="0")  # market-info-failure row
        result = self._candidates(
            tmp_path, "--ticker", "KXCPI-26JUN-T-0.2", "--limit", "5",
        )
        assert result.exit_code == 0, result.output
        assert "REALSENTINEL" in result.output
        assert "No memo stored" not in result.output

    def test_long_url_token_survives_unbroken(self, tmp_path) -> None:
        """soft_wrap: a source URL longer than width 80 must not be
        hard-wrapped into a dead link."""
        url = "https://www.bls.gov/news.release/archives/cpi_" + "x" * 60
        db_path = tmp_path / "gimmes.db"
        self._log(db_path, f"source: {url}")
        result = self._candidates(
            tmp_path, "--ticker", "KXCPI-26JUN-T-0.2",
        )
        assert result.exit_code == 0, result.output
        assert url in result.output

    def test_flip_annotated_memo_marker_renders_literally(
        self, tmp_path,
    ) -> None:
        """markup=False proof: the [FLIP-WARNING] annotation prints
        intact in the memo section."""
        db_path = tmp_path / "gimmes.db"
        self._log(db_path, "scoring memo", prob="0.98")
        self._log(db_path, "rescore memo", prob="0.02")
        result = self._candidates(
            tmp_path, "--ticker", "KXCPI-26JUN-T-0.2", "--limit", "5",
        )
        assert result.exit_code == 0, result.output
        assert "RESEARCH MEMO" in result.output
        assert "[FLIP-WARNING]" in result.output
