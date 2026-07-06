"""Unit tests for daily P&L calculation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import get_daily_pnl, insert_trade


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

    async def test_multi_cycle_same_ticker(self, db: Database) -> None:
        """Two open/close cycles on the same ticker use correct entries."""
        # Fixed reference point far from midnight to avoid cross-day issues
        now = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        # Cycle 1: open at 0.50, close at 0.70
        await insert_trade(db, _trade(
            action="open", price=0.50, count=5,
            timestamp=now - timedelta(hours=3),
        ))
        await insert_trade(db, _trade(
            action="close", price=0.70, count=5,
            timestamp=now - timedelta(hours=2),
        ))
        # Cycle 2: re-open at 0.60, close at 0.65
        await insert_trade(db, _trade(
            action="open", price=0.60, count=5,
            timestamp=now - timedelta(hours=1),
        ))
        await insert_trade(db, _trade(
            action="close", price=0.65, count=5,
            timestamp=now,
        ))
        pnl = await get_daily_pnl(db, today=now.strftime("%Y-%m-%d"))
        # Cycle 1: (0.70 - 0.50) * 5 = 1.0
        # Cycle 2: (0.65 - 0.60) * 5 = 0.25
        # Total: 1.25
        assert pnl == pytest.approx(1.25)


class TestGetDailyPnlExcludesReconcileCloses:
    """Synthetic reconcile-divergence closes (#609) are broker-side
    drift, not intentional trading P&L. Including them in daily P&L
    would distort the autonomous-loop's daily-loss-limit trigger —
    the operator would see a "loss" they did not actually take
    (#622)."""

    async def test_reconcile_close_excluded_from_pnl(
        self, db: Database,
    ) -> None:
        """A reconcile-driven close trade must NOT contribute to
        daily P&L. Without the agent filter, the synthetic close at
        last-known mark would show up as realized P&L."""
        now = datetime.now(UTC)
        # Open at 0.40
        await insert_trade(db, _trade(
            action="open", price=0.40, count=10, timestamp=now,
        ))
        # Reconcile drops the position — synthetic close at the last
        # mark of 0.60, agent='reconcile'. Without the filter, this
        # would show as 0.20 * 10 = $2 of realized P&L.
        reconcile_trade = TradeDecision(
            ticker="KXTEST",
            action=TradeDecision.Action.CLOSE,
            price=0.60,
            count=10,
            edge=0.0,
            agent="reconcile",
            rationale="reconcile drift — broker removed position (#609)",
            timestamp=now,
        )
        await insert_trade(db, reconcile_trade)

        pnl = await get_daily_pnl(db, today=now.strftime("%Y-%m-%d"))
        assert pnl == 0.0, (
            f"Expected 0.0 (reconcile close excluded), got {pnl}"
        )

    async def test_real_close_still_included_alongside_reconcile(
        self, db: Database,
    ) -> None:
        """A real Closer-driven close in the same day as a reconcile
        close should still contribute to daily P&L — only reconcile
        rows are filtered."""
        now = datetime.now(UTC)

        # Real trade pair: open + closer-driven close → +$1 P&L
        await insert_trade(db, _trade(
            ticker="REAL-TICKER", action="open", price=0.40,
            count=10, timestamp=now,
        ))
        await insert_trade(db, _trade(
            ticker="REAL-TICKER", action="close", price=0.50,
            count=10, timestamp=now,
        ))

        # Reconcile drift on a different ticker — must NOT contribute.
        await insert_trade(db, _trade(
            ticker="DRIFT-TICKER", action="open", price=0.40,
            count=10, timestamp=now,
        ))
        reconcile_trade = TradeDecision(
            ticker="DRIFT-TICKER",
            action=TradeDecision.Action.CLOSE,
            price=0.99,  # large drift "gain" if not filtered
            count=10,
            edge=0.0,
            agent="reconcile",
            timestamp=now,
        )
        await insert_trade(db, reconcile_trade)

        pnl = await get_daily_pnl(db, today=now.strftime("%Y-%m-%d"))
        # Only the real close counts: (0.50 - 0.40) * 10 = 1.0
        # If the filter were broken, the DRIFT-TICKER reconcile would
        # add (0.99 - 0.40) * 10 = 5.9 for a misleading total of 6.9.
        assert pnl == pytest.approx(1.0), (
            f"Expected 1.0 (real close only), got {pnl}"
        )

    async def test_multiple_reconcile_closes_all_excluded(
        self, db: Database,
    ) -> None:
        """Multi-ticker reconcile drift in one day — all synthetic
        rows excluded from P&L."""
        now = datetime.now(UTC)
        for ticker in ("A", "B", "C"):
            await insert_trade(db, _trade(
                ticker=ticker, action="open", price=0.30,
                count=5, timestamp=now,
            ))
            await insert_trade(db, TradeDecision(
                ticker=ticker,
                action=TradeDecision.Action.CLOSE,
                price=0.80,
                count=5,
                edge=0.0,
                agent="reconcile",
                timestamp=now,
            ))

        pnl = await get_daily_pnl(db, today=now.strftime("%Y-%m-%d"))
        assert pnl == 0.0


class TestGetDailyPnlIncludesSettlementCloses:
    """#653: settlement closes (agent='settlement') ARE included in
    daily P&L — a settlement loss is real realized money lost that day
    and the daily-loss trigger should see it. Only reconcile DRIFT
    (#622) is excluded."""

    async def test_settlement_close_included_in_pnl(
        self, db: Database,
    ) -> None:
        now = datetime.now(UTC)
        await insert_trade(db, _trade(
            action="open", price=0.40, count=10, timestamp=now,
        ))
        settlement_trade = TradeDecision(
            ticker="KXTEST",
            action=TradeDecision.Action.CLOSE,
            price=0.0,  # settled as a loss
            count=10,
            edge=0.0,
            agent="settlement",
            rationale="market settled (#653)",
            timestamp=now,
        )
        await insert_trade(db, settlement_trade)

        pnl = await get_daily_pnl(db, today=now.strftime("%Y-%m-%d"))
        # (0.0 - 0.40) * 10 = -$4.00 — the settlement loss IS visible.
        assert pnl == pytest.approx(-4.0)


class TestRestingSellNoDoubleCount:
    """#663: a resting sell's close trade is logged at placement; the
    later settlement must not add a second close for the same
    contracts, or daily P&L (and the daily-loss trigger) counts the
    exit twice. End-to-end through PaperBroker.settle()."""

    async def test_settlement_after_placement_close_counts_once(
        self, db: Database, tmp_path,
    ) -> None:
        from unittest.mock import MagicMock

        from gimmes.paper.broker import PaperBroker

        paper_cfg = MagicMock()
        paper_cfg.starting_balance = 10_000.0
        broker = PaperBroker(db, paper_cfg)
        await broker.initialize()

        # Ledger: the agent opened 10 @ 0.70 and its resting sell
        # logged the close 10 @ 0.90 at placement (today).
        await insert_trade(db, _trade(action="open", price=0.70))
        await insert_trade(db, _trade(action="close", price=0.90))
        # Broker state: the paper position was never reduced by the
        # resting sell — it still holds all 10 contracts at settle.
        await db.conn.execute(
            """INSERT INTO paper_positions
               (ticker, side, count, avg_price, market_price,
                cost_basis, unrealized_pnl, realized_pnl, updated_at)
               VALUES ('KXTEST', 'yes', 10, 0.70, 0.90,
                       7.0, 2.0, 0.0, datetime('now'))""",
        )
        await db.conn.commit()

        await broker.settle("KXTEST", "yes")

        # Exactly the placement-time close counts — no settlement row
        # doubled it.
        assert await get_daily_pnl(db) == pytest.approx(
            (0.90 - 0.70) * 10,
        )
