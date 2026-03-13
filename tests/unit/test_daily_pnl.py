"""Unit tests for daily P&L calculation."""

from __future__ import annotations

from datetime import datetime

import pytest

from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import get_daily_pnl, insert_trade


@pytest.fixture
async def db(tmp_path):
    """Create an in-memory database for testing."""
    db_path = tmp_path / "test.db"
    async with Database(db_path) as database:
        yield database


def _trade(
    ticker: str = "KXTEST",
    action: str = "open",
    price: float = 0.70,
    count: int = 10,
    edge: float = 0.15,
    timestamp: datetime | None = None,
) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        action=TradeDecision.Action(action),
        price=price,
        count=count,
        edge=edge,
        timestamp=timestamp or datetime.now(),
    )


class TestGetDailyPnl:
    async def test_no_trades_returns_zero(self, db: Database) -> None:
        pnl = await get_daily_pnl(db)
        assert pnl == 0.0

    async def test_only_opens_returns_zero(self, db: Database) -> None:
        """Open trades without closes should produce zero P&L."""
        await insert_trade(db, _trade(action="open", price=0.60))
        await insert_trade(db, _trade(action="open", price=0.70, ticker="OTHER"))
        pnl = await get_daily_pnl(db)
        assert pnl == 0.0

    async def test_winning_close(self, db: Database) -> None:
        """Close at higher price than open = positive P&L."""
        await insert_trade(db, _trade(action="open", price=0.60, count=10))
        await insert_trade(db, _trade(action="close", price=0.80, count=10))
        pnl = await get_daily_pnl(db)
        # P&L = (0.80 - 0.60) * 10 = 2.0
        assert pnl == pytest.approx(2.0)

    async def test_losing_close(self, db: Database) -> None:
        """Close at lower price than open = negative P&L."""
        await insert_trade(db, _trade(action="open", price=0.70, count=5))
        await insert_trade(db, _trade(action="close", price=0.50, count=5))
        pnl = await get_daily_pnl(db)
        # P&L = (0.50 - 0.70) * 5 = -1.0
        assert pnl == pytest.approx(-1.0)

    async def test_multiple_tickers(self, db: Database) -> None:
        """P&L across multiple tickers sums correctly."""
        # Ticker A: win
        await insert_trade(db, _trade(
            ticker="A", action="open", price=0.50, count=10,
        ))
        await insert_trade(db, _trade(
            ticker="A", action="close", price=0.80, count=10,
        ))
        # Ticker B: loss
        await insert_trade(db, _trade(
            ticker="B", action="open", price=0.60, count=10,
        ))
        await insert_trade(db, _trade(
            ticker="B", action="close", price=0.40, count=10,
        ))
        pnl = await get_daily_pnl(db)
        # A: (0.80 - 0.50) * 10 = 3.0
        # B: (0.40 - 0.60) * 10 = -2.0
        # Total: 1.0
        assert pnl == pytest.approx(1.0)

    async def test_close_without_open_uses_zero_entry(
        self, db: Database,
    ) -> None:
        """Orphaned close (no matching open) uses 0 as entry price."""
        await insert_trade(db, _trade(action="close", price=0.80, count=5))
        pnl = await get_daily_pnl(db)
        # P&L = (0.80 - 0) * 5 = 4.0
        assert pnl == pytest.approx(4.0)

    async def test_edge_field_not_used_in_calculation(
        self, db: Database,
    ) -> None:
        """The old bug used (price - edge) * count. Verify edge is ignored."""
        await insert_trade(db, _trade(
            action="open", price=0.60, count=10, edge=0.20,
        ))
        await insert_trade(db, _trade(
            action="close", price=0.80, count=10, edge=0.15,
        ))
        pnl = await get_daily_pnl(db)
        # Correct: (0.80 - 0.60) * 10 = 2.0
        # Old bug: (0.80 - 0.15) * 10 = 6.5
        assert pnl == pytest.approx(2.0)
        assert pnl != pytest.approx(6.5)

    async def test_skip_trades_ignored(self, db: Database) -> None:
        """Skip trades should not affect P&L."""
        await insert_trade(db, _trade(action="open", price=0.60, count=10))
        await insert_trade(db, _trade(action="skip", price=0.70, count=0))
        await insert_trade(db, _trade(action="close", price=0.80, count=10))
        pnl = await get_daily_pnl(db)
        assert pnl == pytest.approx(2.0)

    async def test_break_even_is_zero(self, db: Database) -> None:
        """Same open and close price = zero P&L."""
        await insert_trade(db, _trade(action="open", price=0.70, count=10))
        await insert_trade(db, _trade(action="close", price=0.70, count=10))
        pnl = await get_daily_pnl(db)
        assert pnl == pytest.approx(0.0)
