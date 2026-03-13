"""Tests for data collection enhancements (issue #20).

Covers: migration v5, resolved_outcome on trades, component scores on candidates,
and the update_trade_outcome query.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gimmes.store.database import Database
from gimmes.store.queries import (
    get_recent_candidates,
    get_trades,
    insert_candidate,
    insert_trade,
    update_trade_outcome,
)


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

        for action in (TradeDecision.Action.OPEN, TradeDecision.Action.CLOSE, TradeDecision.Action.SKIP):
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

        with pytest.raises(Exception, match="CHECK"):
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

    async def test_backward_compatible_insert(self, db: Database) -> None:
        """Old-style inserts without component scores still work."""
        await insert_candidate(db, "OLD-TEST", "Old Style", 0.60, 0.80, 0.20, 72, "memo")
        rows = await get_recent_candidates(db)
        assert len(rows) == 1
        assert rows[0]["ticker"] == "OLD-TEST"
        assert rows[0]["gimme_score"] == 72
