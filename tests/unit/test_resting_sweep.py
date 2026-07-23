"""Tests for the rest-on-miss between-cycle sweep (#743).

Exercises _sweep_resting_paper_orders end-to-end against a real paper
broker and store: expiry, marketable fills, and the log-on-fill ledger
rows.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from gimmes.config import (
    GimmesConfig,
    Mode,
    PaperTradingConfig,
    ScannerConfig,
    StrategyConfig,
)
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


def _plain_config() -> GimmesConfig:
    """Hermetic config: hourly_series empty, no #750 band filtering."""
    return GimmesConfig(mode=Mode.DRIVING_RANGE)


def _hourly_config() -> GimmesConfig:
    """KXTEST is an hourly series: the #750 band filter applies."""
    return GimmesConfig(
        mode=Mode.DRIVING_RANGE,
        strategy=StrategyConfig(side="no"),
        scanner=ScannerConfig(hourly_series=["KXTEST"]),
    )


async def _place_resting(
    broker: PaperBroker, *, expires_in: int = 3600,
    no_price: float = 0.25, book: Orderbook | None = None,
):
    params = CreateOrderParams(
        ticker="KXTEST-MKT",
        action=OrderAction.BUY,
        side=OrderSide.NO,
        count=10,
        no_price=no_price,
        post_only=False,
        expiration_ts=int(time.time()) + expires_in,
    )
    order = await broker.create_order(params, book or _miss_book())
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
                broker, client, db, config=_plain_config(), quiet=True,
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
            broker, client, db, config=_plain_config(), quiet=True,
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


class TestSweepHourlyBand:
    """#750: an hourly resting order advances only when the effective
    mid is back inside the validated band — the book touching the
    limit while the mid sits below the floor is the adverse-repricing
    case the band excludes. One-sided books skip conservatively."""

    @staticmethod
    def _hourly_miss_book() -> Orderbook:
        # Implied NO ask 0.45 — a NO bid at 0.40 misses and rests
        return Orderbook(
            ticker="KXTEST-MKT",
            yes_bids=[OrderbookLevel(price=0.55, quantity=200)],
            no_bids=[OrderbookLevel(price=0.43, quantity=200)],
        )

    @pytest.mark.asyncio
    async def test_in_band_return_fills(
        self, broker: PaperBroker, db: Database
    ) -> None:
        from gimmes.cli import _sweep_resting_paper_orders

        order = await _place_resting(
            broker, no_price=0.40, book=self._hourly_miss_book(),
        )
        # NO ask hits the 0.40 limit; yes mid 0.61 -> NO mid 0.39,
        # inside 0.30-0.85: the fill advances.
        in_band_book = Orderbook(
            ticker="KXTEST-MKT",
            yes_bids=[OrderbookLevel(price=0.60, quantity=200)],
            no_bids=[OrderbookLevel(price=0.38, quantity=200)],
        )
        client = AsyncMock()
        with patch(
            "gimmes.kalshi.markets.get_orderbook",
            AsyncMock(return_value=in_band_book),
        ):
            filled = await _sweep_resting_paper_orders(
                broker, client, db, config=_hourly_config(), quiet=True,
            )
        assert len(filled) == 1
        assert filled[0][0].order_id == order.order_id

    @pytest.mark.asyncio
    async def test_out_of_band_return_skips_fill(
        self, broker: PaperBroker, db: Database
    ) -> None:
        from gimmes.cli import _sweep_resting_paper_orders

        order = await _place_resting(
            broker, no_price=0.40, book=self._hourly_miss_book(),
        )
        # NO ask 0.20 crosses the 0.40 limit (price-wise fillable),
        # but yes mid 0.825 -> NO mid 0.175 is below the 0.30 floor:
        # the c1989 loss shape. No fill, order stays resting.
        crashed_book = Orderbook(
            ticker="KXTEST-MKT",
            yes_bids=[OrderbookLevel(price=0.80, quantity=200)],
            no_bids=[OrderbookLevel(price=0.15, quantity=200)],
        )
        client = AsyncMock()
        with patch(
            "gimmes.kalshi.markets.get_orderbook",
            AsyncMock(return_value=crashed_book),
        ):
            filled = await _sweep_resting_paper_orders(
                broker, client, db, config=_hourly_config(), quiet=True,
            )
        assert filled == []
        resting = await broker.list_orders(status="resting")
        assert [o.order_id for o in resting] == [order.order_id]
        # Log-on-fill: no ledger row for the skipped fill
        cursor = await db.conn.execute("SELECT COUNT(*) AS n FROM trades")
        assert int((await cursor.fetchone())["n"]) == 0

    @pytest.mark.asyncio
    async def test_one_sided_book_skips_conservatively(
        self, broker: PaperBroker, db: Database
    ) -> None:
        from gimmes.cli import _sweep_resting_paper_orders

        order = await _place_resting(
            broker, no_price=0.40, book=self._hourly_miss_book(),
        )
        # yes side only: implied NO ask 0.25 would fill price-wise,
        # but the mid is undeterminable -> conservative skip.
        one_sided = Orderbook(
            ticker="KXTEST-MKT",
            yes_bids=[OrderbookLevel(price=0.75, quantity=200)],
            no_bids=[],
        )
        client = AsyncMock()
        with patch(
            "gimmes.kalshi.markets.get_orderbook",
            AsyncMock(return_value=one_sided),
        ):
            filled = await _sweep_resting_paper_orders(
                broker, client, db, config=_hourly_config(), quiet=True,
            )
        assert filled == []
        assert [
            o.order_id for o in await broker.list_orders(status="resting")
        ] == [order.order_id]

    @pytest.mark.asyncio
    async def test_non_hourly_ticker_unaffected(
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
                broker, client, db, config=_plain_config(), quiet=True,
            )
        assert len(filled) == 1
        assert filled[0][0].order_id == order.order_id

    @pytest.mark.asyncio
    async def test_band_judged_in_order_side_terms(
        self, broker: PaperBroker, db: Database
    ) -> None:
        from gimmes.cli import _sweep_resting_paper_orders

        # Review-found: config side can be flipped after placement.
        # Config says "yes" (YES mid 0.825 IS in band) but the resting
        # order is NO (NO mid 0.175 is below the floor) — the order's
        # own side terms govern, so the fill must be skipped.
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="yes"),
            scanner=ScannerConfig(hourly_series=["KXTEST"]),
        )
        order = await _place_resting(
            broker, no_price=0.40, book=self._hourly_miss_book(),
        )
        crashed_book = Orderbook(
            ticker="KXTEST-MKT",
            yes_bids=[OrderbookLevel(price=0.80, quantity=200)],
            no_bids=[OrderbookLevel(price=0.15, quantity=200)],
        )
        client = AsyncMock()
        with patch(
            "gimmes.kalshi.markets.get_orderbook",
            AsyncMock(return_value=crashed_book),
        ):
            filled = await _sweep_resting_paper_orders(
                broker, client, db, config=config, quiet=True,
            )
        assert filled == []
        assert [
            o.order_id for o in await broker.list_orders(status="resting")
        ] == [order.order_id]
