"""Tests for sync_positions function."""

from __future__ import annotations

import pytest

from gimmes.models.portfolio import Position
from gimmes.store.database import Database
from gimmes.store.queries import get_positions, sync_positions, upsert_position


@pytest.fixture
async def db(tmp_path):
    """Create a temp database."""
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


def _pos(ticker: str, count: int = 10, price: float = 0.5) -> Position:
    return Position(
        ticker=ticker, side="yes", count=count, avg_price=price,
        market_price=price, cost_basis=count * price,
    )


class TestSyncPositions:
    async def test_inserts_new_positions(self, db):
        positions = [_pos("AAPL"), _pos("GOOG")]
        await sync_positions(db, positions)

        stored = await get_positions(db)
        tickers = {p.ticker for p in stored}
        assert tickers == {"AAPL", "GOOG"}

    async def test_removes_stale_positions(self, db):
        # Seed an old position
        await upsert_position(db, _pos("OLD-TICKER"))
        stored = await get_positions(db)
        assert len(stored) == 1

        # Sync with different positions
        await sync_positions(db, [_pos("NEW-TICKER")])

        stored = await get_positions(db)
        tickers = {p.ticker for p in stored}
        assert "OLD-TICKER" not in tickers
        assert "NEW-TICKER" in tickers

    async def test_updates_existing_positions(self, db):
        await upsert_position(db, _pos("AAPL", count=5, price=0.3))

        # Sync with updated count
        await sync_positions(db, [_pos("AAPL", count=15, price=0.7)])

        stored = await get_positions(db)
        assert len(stored) == 1
        assert stored[0].count == 15
        assert stored[0].avg_price == 0.7

    async def test_empty_sync_clears_all(self, db):
        await upsert_position(db, _pos("A"))
        await upsert_position(db, _pos("B"))

        await sync_positions(db, [])

        stored = await get_positions(db)
        assert len(stored) == 0

    async def test_atomic_transaction(self, db):
        """All changes should happen in one transaction."""
        await upsert_position(db, _pos("KEEP"))
        await upsert_position(db, _pos("REMOVE"))

        await sync_positions(db, [_pos("KEEP", count=20), _pos("ADD")])

        stored = await get_positions(db)
        tickers = {p.ticker for p in stored}
        assert tickers == {"KEEP", "ADD"}
        keep = next(p for p in stored if p.ticker == "KEEP")
        assert keep.count == 20
