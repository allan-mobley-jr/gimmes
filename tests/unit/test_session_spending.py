"""Unit tests for session spending calculation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import get_session_spending, insert_trade


@pytest.fixture
async def db(tmp_path):
    """Create a temporary database for testing."""
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
        timestamp=timestamp or datetime.now(UTC),
    )


class TestGetSessionSpending:
    async def test_no_trades_returns_zero(self, db: Database) -> None:
        spent = await get_session_spending(db)
        assert spent == 0.0

    async def test_sums_open_trades_only(self, db: Database) -> None:
        """Close and skip trades should not count toward spending."""
        await insert_trade(db, _trade(action="open", price=0.50, count=10))
        await insert_trade(db, _trade(action="close", price=0.80, count=10))
        await insert_trade(db, _trade(action="skip", price=0.60, count=0))
        spent = await get_session_spending(db)
        # Only the open: 0.50 * 10 = 5.0
        assert spent == pytest.approx(5.0)

    async def test_multiple_open_trades_sum(self, db: Database) -> None:
        await insert_trade(db, _trade(action="open", price=0.50, count=10))
        await insert_trade(db, _trade(
            ticker="OTHER", action="open", price=0.60, count=20,
        ))
        spent = await get_session_spending(db)
        # 0.50*10 + 0.60*20 = 5.0 + 12.0 = 17.0
        assert spent == pytest.approx(17.0)

    async def test_since_filters_correctly(self, db: Database) -> None:
        """Only trades on or after 'since' should be counted."""
        now = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        # Trade before the cutoff
        await insert_trade(db, _trade(
            action="open", price=0.50, count=10,
            timestamp=now - timedelta(hours=5),
        ))
        # Trade after the cutoff
        await insert_trade(db, _trade(
            action="open", price=0.60, count=10,
            timestamp=now - timedelta(hours=1),
        ))

        cutoff = (now - timedelta(hours=3)).isoformat()
        spent = await get_session_spending(db, since=cutoff)
        # Only the second trade: 0.60 * 10 = 6.0
        assert spent == pytest.approx(6.0)

    async def test_since_includes_exact_boundary(self, db: Database) -> None:
        """A trade at exactly the 'since' timestamp should be counted."""
        cutoff = datetime(2026, 3, 15, 10, 0, 0, tzinfo=UTC)
        await insert_trade(db, _trade(
            action="open", price=0.50, count=10, timestamp=cutoff,
        ))
        spent = await get_session_spending(db, since=cutoff.isoformat())
        assert spent == pytest.approx(5.0)

    async def test_since_none_uses_today(self, db: Database) -> None:
        """Without 'since', only today's trades should be counted."""
        yesterday = datetime.now(UTC) - timedelta(days=1)
        await insert_trade(db, _trade(
            action="open", price=0.50, count=10, timestamp=yesterday,
        ))
        await insert_trade(db, _trade(action="open", price=0.60, count=10))
        spent = await get_session_spending(db)
        # Only today's trade: 0.60 * 10 = 6.0
        assert spent == pytest.approx(6.0)
