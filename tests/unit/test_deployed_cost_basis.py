"""Unit tests for deployed cost basis calculation."""

from __future__ import annotations

import pytest

from gimmes.models.portfolio import Position
from gimmes.store.database import Database
from gimmes.store.queries import get_deployed_cost_basis, upsert_position


@pytest.fixture
async def db(tmp_path):
    """Create a temporary database for testing."""
    db_path = tmp_path / "test.db"
    async with Database(db_path) as database:
        yield database


def _position(
    ticker: str = "KXTEST",
    count: int = 10,
    cost_basis: float = 7.0,
) -> Position:
    return Position(
        ticker=ticker,
        count=count,
        avg_price=0.70,
        cost_basis=cost_basis,
    )


class TestGetDeployedCostBasis:
    async def test_no_positions_returns_zero(self, db: Database) -> None:
        deployed = await get_deployed_cost_basis(db)
        assert deployed == 0.0

    async def test_sums_open_positions(self, db: Database) -> None:
        await upsert_position(db, _position("TICK1", count=10, cost_basis=5.0))
        await upsert_position(db, _position("TICK2", count=20, cost_basis=12.0))
        deployed = await get_deployed_cost_basis(db)
        assert deployed == pytest.approx(17.0)

    async def test_ignores_zero_count_positions(self, db: Database) -> None:
        """Closed positions (count=0) should not be summed."""
        await upsert_position(db, _position("OPEN", count=10, cost_basis=5.0))
        await upsert_position(db, _position("CLOSED", count=0, cost_basis=3.0))
        deployed = await get_deployed_cost_basis(db)
        assert deployed == pytest.approx(5.0)

    async def test_single_position(self, db: Database) -> None:
        await upsert_position(db, _position(cost_basis=7.0))
        deployed = await get_deployed_cost_basis(db)
        assert deployed == pytest.approx(7.0)
