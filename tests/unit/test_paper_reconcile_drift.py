"""Verify #609's reconcile-drift fix covers paper mode (#623).

The silent-failure-hunter on PR #624 (#609) claimed paper-mode reconcile
bypasses `sync_positions` and therefore doesn't get the synthetic close
trade. Reading the code, `cli.reconcile` actually DOES route paper mode
through `sync_positions(db, broker.get_positions())` at cli.py:1742-1750,
so #609's fix should cover paper mode automatically.

These tests prove it: settle a paper position via `PaperBroker.settle()`
(the most common path that zeros out `paper_positions.count` outside of
reconcile), then run the reconcile flow and assert the synthetic close +
reconcile-divergence decision note are written.

If these tests pass without any code change, #623 is resolved by #609.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gimmes.config import GimmesConfig, Mode, PaperTradingConfig
from gimmes.paper.broker import PaperBroker
from gimmes.store.database import Database
from gimmes.store.queries import (
    get_position_notes,
    get_trades,
    sync_positions,
    upsert_position,
)
from gimmes.store.ticker_resolver import resolve_ticker


def _paper_config() -> GimmesConfig:
    return GimmesConfig(
        mode=Mode.DRIVING_RANGE,
        paper=PaperTradingConfig(starting_balance=10_000.0),
    )


@pytest.fixture
async def db_and_broker(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "test.db")
    await db.connect()
    broker = PaperBroker(db, _paper_config().paper)
    await broker.initialize()
    try:
        yield db, broker
    finally:
        await db.close()


async def _seed_position(
    db: Database, broker: PaperBroker, ticker: str = "KXCPI-26APR-T0.5",
) -> None:
    """Seed both paper_positions (broker state) and positions
    (sync_positions target). Mirrors what a real cycle would have
    produced via an Open trade + reconcile."""
    from gimmes.models.portfolio import Position

    # Insert paper_positions row directly so the broker sees the
    # position. Using raw SQL because the broker's order-path is
    # complex and not relevant to this test.
    await db.conn.execute(
        """INSERT INTO paper_positions
           (ticker, side, count, avg_price, market_price,
            cost_basis, unrealized_pnl, realized_pnl,
            updated_at)
           VALUES (?, 'yes', 10, 0.40, 0.42,
                   4.0, 0.2, 0.0,
                   datetime('now'))""",
        (ticker,),
    )
    await db.conn.commit()

    # Also seed positions table so we have something for sync_positions
    # to remove on reconcile.
    pos = Position(
        ticker=ticker,
        title="CPI April YES",
        side="yes",
        count=10,
        avg_price=0.40,
        market_price=0.42,
        cost_basis=4.0,
        market_value=4.2,
        unrealized_pnl=0.2,
        realized_pnl=0.0,
    )
    await upsert_position(db, pos)


class TestPaperReconcileDriftCoveredBy609:
    async def test_settle_then_reconcile_writes_synthetic_close(
        self, db_and_broker,  # type: ignore[no-untyped-def]
    ) -> None:
        """The smoking-gun scenario: paper market settles → broker
        zeros out paper_positions internally → next reconcile sees the
        position absent from broker.get_positions() → sync_positions
        removes from positions → #609 fires → synthetic close trade +
        reconcile-divergence decision note (#623)."""
        db, broker = db_and_broker
        await _seed_position(db, broker, ticker="KXCPI-26APR-T0.5")

        # Sanity: broker reports the position.
        positions_before = await broker.get_positions()
        assert any(
            p.ticker == "KXCPI-26APR-T0.5" for p in positions_before
        )

        # Settlement: paper-broker zeros paper_positions.count
        # WITHOUT going through reconcile. This is the path the
        # silent-failure-hunter feared would bypass #609.
        await broker.settle("KXCPI-26APR-T0.5", result="no")

        # After settle, broker.get_positions() (filtered by count>0)
        # no longer includes the settled ticker.
        positions_after = await broker.get_positions()
        assert not any(
            p.ticker == "KXCPI-26APR-T0.5" for p in positions_after
        )

        # The reconcile flow: pass broker's fresh positions to
        # sync_positions. This is what cli.py:1750 does.
        await sync_positions(db, positions_after)

        # ASSERT #609 fired in paper mode:
        # 1. Synthetic close trade with agent='reconcile'.
        trades = await get_trades(db)
        reconcile_trades = [
            t for t in trades if t.get("agent") == "reconcile"
        ]
        assert len(reconcile_trades) == 1, (
            f"Expected 1 reconcile-agent trade, got {reconcile_trades}"
        )
        t = reconcile_trades[0]
        assert t["ticker"] == "KXCPI-26APR-T0.5"
        assert t["action"] == "close"

        # 2. Decision note with Trigger: Reconcile-divergence (NOT
        #    Trigger: Stop-loss breach).
        notes = await get_position_notes(
            db, "KXCPI-26APR-T0.5", note_type="decision",
        )
        assert len(notes) == 1
        body = notes[0]["body"]
        assert "Decision: CLOSE" in body
        assert "Trigger: Reconcile-divergence" in body
        assert "Trigger: Stop-loss breach" not in body
        assert notes[0]["agent"] == "reconcile"

        # 3. known_markets resolver finds the closed ticker (closes
        #    the #586 lockout-resolver gap that #623 was filed to fix).
        matches = await resolve_ticker(
            db, "KXCPI-26APR-T0.5", source="known_markets",
        )
        assert matches == ["KXCPI-26APR-T0.5"]

    async def test_paper_reconcile_flow_mirrors_championship(
        self, db_and_broker,  # type: ignore[no-untyped-def]
    ) -> None:
        """The whole point of #623: paper mode should behave identically
        to championship mode under reconcile drift. Both call
        sync_positions with the fresh set; #609's logic handles both."""
        db, broker = db_and_broker
        # Three positions; settle one of them.
        for tick in ("KXCPI-26APR-T0.5", "KXPAYROLLS-26APR", "KXFED-26MAY"):
            await _seed_position(db, broker, ticker=tick)
        await broker.settle("KXCPI-26APR-T0.5", result="no")

        fresh = await broker.get_positions()
        await sync_positions(db, fresh)

        # Exactly one synthetic close, for the settled ticker.
        trades = await get_trades(db)
        reconcile_trades = [
            t for t in trades if t.get("agent") == "reconcile"
        ]
        assert len(reconcile_trades) == 1
        assert reconcile_trades[0]["ticker"] == "KXCPI-26APR-T0.5"

        # The other two positions are unchanged.
        from gimmes.store.queries import get_positions
        remaining = await get_positions(db)
        remaining_tickers = {p.ticker for p in remaining}
        assert "KXCPI-26APR-T0.5" not in remaining_tickers
        assert "KXPAYROLLS-26APR" in remaining_tickers
        assert "KXFED-26MAY" in remaining_tickers
