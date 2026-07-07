"""Verify #609's reconcile-drift fix covers paper mode (#623).

The silent-failure-hunter on PR #624 (#609) claimed paper-mode reconcile
bypasses `sync_positions` and therefore doesn't get the synthetic close
trade. Reading the code, `cli.reconcile()` actually DOES route paper
mode through `sync_positions(db, broker.get_positions())`, so #609's
fix should cover paper mode automatically.

These tests prove it from two angles:
- Unit-level: settle a paper position via `PaperBroker.settle()` (the
  most common path that zeros `paper_positions.count` outside reconcile),
  then directly call `sync_positions(db, broker.get_positions())` and
  assert the synthetic close + reconcile-divergence note are written.
- CLI-level: invoke `gimmes reconcile` via the Typer CliRunner against
  a real DB + real PaperBroker, so a future refactor that diverged the
  CLI's paper-mode path from `sync_positions` would fail loudly.

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

        # #653: settle() itself wrote the ONE close — at settlement
        # value, agent='settlement' — and deleted the positions mirror
        # row, so reconcile must NOT write a duplicate mark-priced
        # drift close (that was how phantom rows were born).
        trades = await get_trades(db)
        closes = [t for t in trades if t["action"] == "close"]
        assert len(closes) == 1, (
            f"Expected exactly 1 close after settle+reconcile,"
            f" got {closes}"
        )
        t = closes[0]
        assert t["agent"] == "settlement"
        assert t["ticker"] == "KXCPI-26APR-T0.5"
        # YES position + result 'no' → lost → settlement value 0.0.
        assert t["price"] == 0.0
        assert t["resolved_outcome"] == "no"

        # 2. Decision note with Trigger: Settlement (NOT
        #    Trigger: Stop-loss breach).
        notes = await get_position_notes(
            db, "KXCPI-26APR-T0.5", note_type="decision",
        )
        assert len(notes) == 1
        body = notes[0]["body"]
        assert "Decision: CLOSE" in body
        assert "Trigger: Settlement" in body
        assert "Trigger: Stop-loss breach" not in body
        assert notes[0]["agent"] == "settlement"

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

        # #653: exactly one close for the settled ticker — written by
        # settle() at settlement value — and no reconcile duplicate.
        trades = await get_trades(db)
        closes = [t for t in trades if t["action"] == "close"]
        assert len(closes) == 1
        assert closes[0]["ticker"] == "KXCPI-26APR-T0.5"
        assert closes[0]["agent"] == "settlement"
        assert not any(t.get("agent") == "reconcile" for t in trades)

        # The other two positions are unchanged.
        from gimmes.store.queries import get_positions
        remaining = await get_positions(db)
        remaining_tickers = {p.ticker for p in remaining}
        assert "KXCPI-26APR-T0.5" not in remaining_tickers
        assert "KXPAYROLLS-26APR" in remaining_tickers
        assert "KXFED-26MAY" in remaining_tickers


def test_cli_reconcile_paper_mode_writes_synthetic_close(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch,
) -> None:
    """End-to-end through the actual `cli.reconcile()` Typer command —
    catches a future refactor that diverged paper-mode's reconcile path
    from championship's. Without this, the unit tests above would still
    pass even if `cli.reconcile()` stopped calling sync_positions in
    paper mode (the exact regression #623 was guarding against).

    Synchronous test using asyncio.run() for setup so CliRunner's
    asyncio.run() inside the Typer command doesn't nest event loops.
    """
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    from typer.testing import CliRunner

    from gimmes import cli as cli_module
    from gimmes.cli import app

    db_path = tmp_path / "test.db"

    async def _setup() -> None:
        db = Database(db_path)
        await db.connect()
        broker = PaperBroker(db, _paper_config().paper)
        await broker.initialize()
        await _seed_position(
            db, broker, ticker="KXCPI-26APR-T0.5",
        )
        await broker.settle("KXCPI-26APR-T0.5", result="no")
        await db.close()

    asyncio.run(_setup())

    # Patch load_config to point at our temp DB and force paper mode.
    cfg = MagicMock()
    cfg.db_path = db_path
    cfg.is_championship = False
    cfg.paper = _paper_config().paper
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

    # Patch trading_context to skip KalshiClient (no network) and yield
    # a real PaperBroker + real DB so the CLI exercises the actual
    # reconcile branch (paper-mode: broker.get_positions() →
    # sync_positions). A divergence between the CLI and our unit tests
    # would surface as a test failure here.
    @asynccontextmanager
    async def _ctx(_config):  # type: ignore[no-untyped-def]
        db = Database(db_path)
        await db.connect()
        try:
            broker = PaperBroker(db, _paper_config().paper)
            await broker.initialize()
            yield None, broker, db
        finally:
            await db.close()

    monkeypatch.setattr(cli_module, "trading_context", _ctx)

    runner = CliRunner()
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output

    # Verify the actual CLI path: settle() already wrote the ONE
    # settlement close; the CLI reconcile must not add a duplicate
    # drift close (#653).
    async def _check() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            trades = await get_trades(db)
            closes = [t for t in trades if t["action"] == "close"]
            assert len(closes) == 1, (
                f"Expected exactly one close after CLI reconcile,"
                f" got {len(closes)}: {closes}"
            )
            assert closes[0]["ticker"] == "KXCPI-26APR-T0.5"
            assert closes[0]["agent"] == "settlement"

            notes = await get_position_notes(
                db, "KXCPI-26APR-T0.5", note_type="decision",
            )
            assert len(notes) == 1
            body = notes[0]["body"]
            assert "Trigger: Settlement" in body
            assert "Trigger: Stop-loss breach" not in body
        finally:
            await db.close()

    asyncio.run(_check())


def test_paper_reconcile_never_touches_settlements_endpoint(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch,
) -> None:
    """#684 item 8: settlements consumption is championship-only — a
    paper-mode reconcile with a removed position must never call the
    settlements endpoint (the paper broker maintains its own truth)."""
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    from typer.testing import CliRunner

    from gimmes import cli as cli_module
    from gimmes.cli import app
    from gimmes.kalshi import portfolio as portfolio_module

    db_path = tmp_path / "test.db"

    async def _setup() -> None:
        db = Database(db_path)
        await db.connect()
        broker = PaperBroker(db, _paper_config().paper)
        await broker.initialize()
        await _seed_position(db, broker, ticker="KXCPI-26APR-T0.5")
        # Remove from the broker WITHOUT settling — the reconcile
        # will see it as removed and write a drift close.
        await db.conn.execute(
            "DELETE FROM paper_positions WHERE ticker = ?",
            ("KXCPI-26APR-T0.5",),
        )
        await db.conn.commit()
        await db.close()

    asyncio.run(_setup())

    cfg = MagicMock()
    cfg.db_path = db_path
    cfg.is_championship = False
    cfg.paper = _paper_config().paper
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

    @asynccontextmanager
    async def _ctx(_config):  # type: ignore[no-untyped-def]
        db = Database(db_path)
        await db.connect()
        try:
            broker = PaperBroker(db, _paper_config().paper)
            await broker.initialize()
            yield None, broker, db
        finally:
            await db.close()

    monkeypatch.setattr(cli_module, "trading_context", _ctx)

    calls: list[set] = []

    async def _spy(_client, tickers, **_kw):  # type: ignore[no-untyped-def]
        calls.append(set(tickers))
        return {}

    monkeypatch.setattr(
        portfolio_module, "get_settlements_for_tickers", _spy,
    )

    result = CliRunner().invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert calls == []  # endpoint never touched in paper mode
