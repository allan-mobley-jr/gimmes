"""Tests for clear_all_candidates (cooldown reset)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from gimmes.store.database import Database
from gimmes.store.queries import clear_all_candidates, insert_candidate


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """Create a temp database."""
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


async def _insert(db: Database, ticker: str) -> int:
    return await insert_candidate(
        db, ticker, f"Title {ticker}", 0.50, 0.80, 0.10, 70, "memo",
    )


class TestClearAllCandidates:
    @pytest.mark.asyncio
    async def test_clears_and_returns_count(self, db: Database) -> None:
        await _insert(db, "A")
        await _insert(db, "B")
        await _insert(db, "C")

        count = await clear_all_candidates(db)
        assert count == 3

        # Verify table is empty
        cursor = await db.conn.execute("SELECT COUNT(*) FROM candidates")
        row = await cursor.fetchone()
        assert row[0] == 0

    @pytest.mark.asyncio
    async def test_empty_table_returns_zero(self, db: Database) -> None:
        count = await clear_all_candidates(db)
        assert count == 0

    @pytest.mark.asyncio
    async def test_idempotent(self, db: Database) -> None:
        await _insert(db, "A")
        first = await clear_all_candidates(db)
        second = await clear_all_candidates(db)
        assert first == 1
        assert second == 0
