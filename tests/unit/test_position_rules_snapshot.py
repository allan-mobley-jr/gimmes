"""Tests for the #643 settlement-language snapshot: migration v17 and
the set/get query helpers.

The snapshot column is deliberately NOT part of the position upsert —
these tests pin the wipe-proof property: a later sync/upsert of the
same ticker must preserve a previously written snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gimmes.models.portfolio import Position
from gimmes.store.database import Database
from gimmes.store.migrations import get_schema_version
from gimmes.store.queries import (
    get_position_rules_snapshot,
    set_position_rules_snapshot,
    sync_positions,
    upsert_position,
)

RULES = (
    "If the Consumer Price Index (CPI) increases by more than -0.1%"
    " (single-decimal) in June 2026, the market resolves to Yes."
)


def _pos(ticker: str, count: int = 100) -> Position:
    return Position(
        ticker=ticker, side="no", count=count, avg_price=0.63,
        market_price=0.90, cost_basis=63.0,
    )


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


class TestMigrationV17:
    async def test_schema_reaches_v17_with_rules_column(
        self, db: Database,
    ) -> None:
        assert await get_schema_version(db) >= 17
        cursor = await db.conn.execute("PRAGMA table_info(positions)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert "rules_primary" in columns

    async def test_migration_idempotent_on_reconnect(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "re.db"
        for _ in range(2):
            database = Database(path)
            await database.connect()
            try:
                assert await get_schema_version(database) >= 17
            finally:
                await database.close()


class TestRulesSnapshotHelpers:
    async def test_roundtrip(self, db: Database) -> None:
        await upsert_position(db, _pos("KXCPI-26JUN-T-0.1"))
        assert await set_position_rules_snapshot(
            db, ticker="KXCPI-26JUN-T-0.1", rules_primary=RULES,
        )
        assert await get_position_rules_snapshot(
            db, "KXCPI-26JUN-T-0.1",
        ) == RULES

    async def test_empty_rules_is_noop_false(self, db: Database) -> None:
        await upsert_position(db, _pos("KXCPI-26JUN-T-0.1"))
        assert not await set_position_rules_snapshot(
            db, ticker="KXCPI-26JUN-T-0.1", rules_primary="",
        )
        assert await get_position_rules_snapshot(
            db, "KXCPI-26JUN-T-0.1",
        ) == ""

    async def test_no_position_row_returns_false_and_inserts_nothing(
        self, db: Database,
    ) -> None:
        """UPDATE-only by design: a stub row would be swept by the next
        position sync and generate a bogus synthetic close (#643)."""
        assert not await set_position_rules_snapshot(
            db, ticker="KXNOPOS-26JUL", rules_primary=RULES,
        )
        cursor = await db.conn.execute(
            "SELECT COUNT(*) AS n FROM positions WHERE ticker = ?",
            ("KXNOPOS-26JUL",),
        )
        row = await cursor.fetchone()
        assert row["n"] == 0
        assert await get_position_rules_snapshot(db, "KXNOPOS-26JUL") is None

    async def test_get_returns_none_for_missing_row(
        self, db: Database,
    ) -> None:
        assert await get_position_rules_snapshot(db, "KXGHOST") is None

    async def test_upsert_preserves_snapshot(self, db: Database) -> None:
        """The wipe-proof property: position upserts (broker/API data
        without rules) must not clear an existing snapshot."""
        await upsert_position(db, _pos("KXCPI-26JUN-T-0.1"))
        await set_position_rules_snapshot(
            db, ticker="KXCPI-26JUN-T-0.1", rules_primary=RULES,
        )
        await upsert_position(db, _pos("KXCPI-26JUN-T-0.1", count=200))
        assert await get_position_rules_snapshot(
            db, "KXCPI-26JUN-T-0.1",
        ) == RULES

    async def test_sync_preserves_snapshot(self, db: Database) -> None:
        await upsert_position(db, _pos("KXCPI-26JUN-T-0.1"))
        await set_position_rules_snapshot(
            db, ticker="KXCPI-26JUN-T-0.1", rules_primary=RULES,
        )
        await sync_positions(db, [_pos("KXCPI-26JUN-T-0.1", count=150)])
        assert await get_position_rules_snapshot(
            db, "KXCPI-26JUN-T-0.1",
        ) == RULES

    async def test_overwrite_with_newer_rules(self, db: Database) -> None:
        await upsert_position(db, _pos("KXCPI-26JUN-T-0.1"))
        await set_position_rules_snapshot(
            db, ticker="KXCPI-26JUN-T-0.1", rules_primary="old text",
        )
        await set_position_rules_snapshot(
            db, ticker="KXCPI-26JUN-T-0.1", rules_primary=RULES,
        )
        assert await get_position_rules_snapshot(
            db, "KXCPI-26JUN-T-0.1",
        ) == RULES
