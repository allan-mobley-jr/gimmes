"""Integration tests for SQLite database."""

import pytest

from gimmes.models.portfolio import PortfolioSnapshot, Position
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import (
    get_latest_snapshot,
    get_positions,
    get_trade_count,
    get_trades,
    insert_snapshot,
    insert_trade,
    upsert_position,
)


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    database = Database(db_path)
    await database.connect()
    yield database
    await database.close()


class TestDatabase:
    async def test_connect_and_schema(self, db: Database) -> None:
        # Base tables should exist after connect
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row[0] for row in await cursor.fetchall()]
        assert "trades" in tables
        assert "positions" in tables
        assert "snapshots" in tables
        assert "candidates" in tables

    async def test_migrations_run_on_connect(self, db: Database) -> None:
        # Migrated tables should exist after connect without explicit run_migrations()
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row[0] for row in await cursor.fetchall()]
        assert "activity_log" in tables

        # Schema version should reflect latest migration
        cursor = await db.conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = await cursor.fetchone()
        assert row[0] >= 2


class TestTradeQueries:
    async def test_insert_and_get(self, db: Database) -> None:
        trade = TradeDecision(
            ticker="KXTEST",
            action=TradeDecision.Action.OPEN,
            side="yes",
            count=10,
            price=0.70,
            model_probability=0.90,
            gimme_score=82,
            edge=0.20,
            rationale="Strong edge",
            agent="scout",
        )
        row_id = await insert_trade(db, trade)
        assert row_id > 0

        trades = await get_trades(db, ticker="KXTEST")
        assert len(trades) == 1
        assert trades[0]["ticker"] == "KXTEST"
        assert trades[0]["action"] == "open"

    async def test_trade_count(self, db: Database) -> None:
        trade = TradeDecision(
            ticker="KXTEST", action=TradeDecision.Action.OPEN,
            count=5, price=0.65,
        )
        await insert_trade(db, trade)
        count = await get_trade_count(db)
        assert count >= 1


class TestPositionQueries:
    async def test_upsert_and_get(self, db: Database) -> None:
        pos = Position(
            ticker="KXTEST", side="yes", count=10,
            avg_price=0.70, market_price=0.75,
        )
        await upsert_position(db, pos)

        positions = await get_positions(db)
        assert len(positions) == 1
        assert positions[0].ticker == "KXTEST"
        assert positions[0].count == 10

    async def test_upsert_updates(self, db: Database) -> None:
        pos1 = Position(ticker="KXTEST", side="yes", count=10, avg_price=0.70)
        await upsert_position(db, pos1)

        pos2 = Position(ticker="KXTEST", side="yes", count=15, avg_price=0.72)
        await upsert_position(db, pos2)

        positions = await get_positions(db)
        assert len(positions) == 1
        assert positions[0].count == 15


class TestSnapshotQueries:
    async def test_insert_and_get(self, db: Database) -> None:
        snap = PortfolioSnapshot(
            balance=5000, portfolio_value=3000,
            total_equity=8000, open_position_count=5,
            daily_pnl=150, total_pnl=800,
        )
        await insert_snapshot(db, snap)

        latest = await get_latest_snapshot(db)
        assert latest is not None
        assert latest["balance"] == 5000
        assert latest["total_equity"] == 8000
