"""Tests for the rest-on-miss between-cycle sweep (#743).

Exercises _sweep_resting_paper_orders end-to-end against a real paper
broker and store: expiry, marketable fills, and the log-on-fill ledger
rows.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from gimmes.config import PaperTradingConfig
from gimmes.models.market import Orderbook, OrderbookLevel
from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide
from gimmes.paper.broker import PaperBroker
from gimmes.store.database import Database
from gimmes.store.queries import get_deployed_cost_basis
from gimmes.store.session import has_active_resting_paper_orders


@pytest.fixture
async def db(tmp_path) -> Database:
    database = Database(tmp_path / "sweep_test.db")
    await database.connect()
    yield database  # type: ignore[misc]
    await database.close()


@pytest.fixture
async def broker(db: Database) -> PaperBroker:
    b = PaperBroker(db, PaperTradingConfig(starting_balance=10_000.00))
    await b.initialize()
    return b


def _miss_book() -> Orderbook:
    """Implied NO ask 0.32 — a NO bid at 0.25 misses."""
    return Orderbook(
        ticker="KXTEST-MKT",
        yes_bids=[OrderbookLevel(price=0.68, quantity=200)],
        no_bids=[],
    )


def _return_book() -> Orderbook:
    """Implied NO ask 0.24 — a resting NO bid at 0.25 fills."""
    return Orderbook(
        ticker="KXTEST-MKT",
        yes_bids=[OrderbookLevel(price=0.76, quantity=200)],
        no_bids=[],
    )


async def _place_resting(broker: PaperBroker, *, expires_in: int = 3600):
    params = CreateOrderParams(
        ticker="KXTEST-MKT",
        action=OrderAction.BUY,
        side=OrderSide.NO,
        count=10,
        no_price=0.25,
        post_only=False,
        expiration_ts=int(time.time()) + expires_in,
    )
    order = await broker.create_order(params, _miss_book())
    assert order.status == "resting"
    return order


class TestSweep:
    @pytest.mark.asyncio
    async def test_sweep_fills_and_logs_ledger_row(
        self, broker: PaperBroker, db: Database
    ) -> None:
        from gimmes.cli import _sweep_resting_paper_orders

        order = await _place_resting(broker)

        client = AsyncMock()
        with patch(
            "gimmes.kalshi.markets.get_orderbook",
            AsyncMock(return_value=_return_book()),
        ):
            filled = await _sweep_resting_paper_orders(
                broker, client, db, quiet=True,
            )

        assert len(filled) == 1
        forder, n = filled[0]
        assert forder.order_id == order.order_id
        assert n == 10

        # #743 log-on-fill: the ledger row lands at fill time with the
        # filled count and the sweep agent.
        cursor = await db.conn.execute(
            "SELECT * FROM trades WHERE ticker = 'KXTEST-MKT'"
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        assert len(rows) == 1
        assert rows[0]["action"] == "open"
        assert rows[0]["count"] == 10
        assert rows[0]["agent"] == "sweep"
        assert rows[0]["order_id"] == order.order_id

        # Position mirrored into the main positions table by the sync
        cursor = await db.conn.execute(
            "SELECT count FROM positions WHERE ticker = 'KXTEST-MKT'"
        )
        pos = await cursor.fetchone()
        assert pos is not None and int(pos["count"]) == 10

    @pytest.mark.asyncio
    async def test_sweep_expires_overdue_order_with_no_ledger_row(
        self, broker: PaperBroker, db: Database
    ) -> None:
        from gimmes.cli import _sweep_resting_paper_orders

        await _place_resting(broker, expires_in=-100)

        client = AsyncMock()
        filled = await _sweep_resting_paper_orders(
            broker, client, db, quiet=True,
        )

        assert filled == []
        assert await broker.get_balance() == 10_000.00
        # Log-on-fill: nothing was logged at placement, nothing to annul
        cursor = await db.conn.execute("SELECT COUNT(*) AS n FROM trades")
        row = await cursor.fetchone()
        assert int(row["n"]) == 0

    @pytest.mark.asyncio
    async def test_resting_reservation_counts_as_deployed(
        self, broker: PaperBroker, db: Database
    ) -> None:
        """#743: the bankroll gate sees resting reservations."""
        assert await get_deployed_cost_basis(db) == 0.0
        await _place_resting(broker)
        assert await get_deployed_cost_basis(db) == pytest.approx(2.50)

    @pytest.mark.asyncio
    async def test_session_helper_sees_resting_orders(
        self, broker: PaperBroker, db: Database
    ) -> None:
        assert has_active_resting_paper_orders(db.db_path) is False
        await _place_resting(broker)
        assert has_active_resting_paper_orders(db.db_path) is True

    @pytest.mark.asyncio
    async def test_session_helper_false_on_missing_db(self, tmp_path) -> None:
        assert (
            has_active_resting_paper_orders(tmp_path / "nope.db") is False
        )
