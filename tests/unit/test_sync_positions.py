"""Tests for sync_positions and sync_positions_with_trade functions."""

from __future__ import annotations

import pytest

from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import (
    get_positions,
    get_trades,
    insert_trade,
    sync_positions,
    sync_positions_with_trade,
    upsert_position,
)


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


def _trade(ticker: str = "KXTEST", price: float = 0.60) -> TradeDecision:
    return TradeDecision(
        ticker=ticker, action=TradeDecision.Action.OPEN,
        side="yes", count=10, price=price,
    )


class TestSyncPositionsWithTrade:
    async def test_syncs_positions_and_inserts_trade(self, db):
        """Both positions and trade should be written."""
        positions = [_pos("AAPL")]
        trade = _trade("AAPL")

        row_id = await sync_positions_with_trade(db, positions, trade)

        stored_pos = await get_positions(db)
        assert len(stored_pos) == 1
        assert stored_pos[0].ticker == "AAPL"

        trades = await get_trades(db, ticker="AAPL")
        assert len(trades) == 1
        assert row_id > 0

    async def test_rolls_back_on_trade_failure(self, db):
        """If the trade insert fails, positions should not be synced."""
        await upsert_position(db, _pos("OLD"))

        # Create a trade with an invalid action value that will fail on insert
        trade = _trade("NEW")
        # Monkey-patch to force a DB error during insert
        original_execute = db.conn.execute

        call_count = 0

        async def failing_execute(sql, params=None):
            nonlocal call_count
            call_count += 1
            # Let position sync SQL through, fail on the trade INSERT
            if "INSERT INTO trades" in str(sql):
                raise RuntimeError("simulated insert failure")
            if params:
                return await original_execute(sql, params)
            return await original_execute(sql)

        db._conn.execute = failing_execute  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="simulated insert failure"):
            await sync_positions_with_trade(db, [_pos("NEW")], trade)

        # Restore original execute for assertions
        db._conn.execute = original_execute  # type: ignore[assignment]

        # Positions should be unchanged (rolled back)
        stored = await get_positions(db)
        tickers = {p.ticker for p in stored}
        assert "OLD" in tickers
        assert "NEW" not in tickers

    async def test_removes_stale_positions_with_trade(self, db):
        """Old positions should be removed when syncing with trade."""
        await upsert_position(db, _pos("STALE"))

        await sync_positions_with_trade(
            db, [_pos("FRESH")], _trade("FRESH")
        )

        stored = await get_positions(db)
        tickers = {p.ticker for p in stored}
        assert "STALE" not in tickers
        assert "FRESH" in tickers


class TestGetPositionsStalenessWarning:
    async def test_no_warning_when_in_sync(self, db, caplog):
        """No warning when positions are fresher than trades."""
        # Insert trade then sync positions (normal flow)
        await insert_trade(db, _trade("AAPL"))
        await sync_positions(db, [_pos("AAPL")])

        import logging
        with caplog.at_level(logging.WARNING, logger="gimmes.store.queries"):
            await get_positions(db)

        assert "stale" not in caplog.text.lower()

    async def test_no_warning_when_empty(self, db, caplog):
        """No warning when there are no trades or positions."""
        import logging
        with caplog.at_level(logging.WARNING, logger="gimmes.store.queries"):
            await get_positions(db)

        assert "stale" not in caplog.text.lower()
