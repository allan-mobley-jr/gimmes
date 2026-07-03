"""Tests for `gimmes backfill-settlements` (#653) — the one-time
correction that writes missing settlement close trades for
paper-settled positions and repairs mark-priced reconcile drift rows.

Synchronous tests (asyncio.run for setup) — CliRunner's invocation
drives the CLI's own asyncio.run, which cannot nest.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gimmes import cli as cli_module
from gimmes.cli import app
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import get_trades, insert_trade

runner = CliRunner()

TICKER = "KXCPI-26APR-T0.5"


def _patch_config(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    cfg = MagicMock()
    cfg.db_path = db_path
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)


def _trade(
    ticker: str, action: str, count: int, price: float,
    agent: str = "closer", ts: str | None = None,
) -> TradeDecision:
    t = TradeDecision(
        ticker=ticker,
        action=TradeDecision.Action(action),
        side="no",
        count=count,
        price=price,
        model_probability=0.8,
        gimme_score=70.0,
        edge=0.1,
        kelly_fraction=0.02,
        rationale="test",
        agent=agent,
        order_id=f"o-{ticker}-{action}",
    )
    if ts is not None:
        t.timestamp = datetime.fromisoformat(ts).replace(tzinfo=UTC)
    return t


def _seed(
    db_path: Path, *,
    open_count: int = 100,
    close_count: int | None = None,
    close_agent: str = "closer",
    close_price: float = 0.9,
    paper_market_price: float = 1.0,
    paper_count: int = 0,
) -> None:
    """Seed an open trade, an optional close, and a paper_positions row."""

    async def _s() -> None:
        from gimmes.paper.broker import PaperBroker

        db = Database(db_path)
        await db.connect()
        try:
            # paper_positions is created by PaperBroker.initialize().
            paper_cfg = MagicMock()
            paper_cfg.starting_balance = 10_000.0
            broker = PaperBroker(db, paper_cfg)
            await broker.initialize()
            await insert_trade(db, _trade(
                TICKER, "open", open_count, 0.63, ts="2026-04-20T12:00:00",
            ))
            if close_count is not None:
                await insert_trade(db, _trade(
                    TICKER, "close", close_count, close_price,
                    agent=close_agent, ts="2026-04-25T12:00:00",
                ))
            await db.conn.execute(
                """INSERT INTO paper_positions
                   (ticker, side, count, avg_price, market_price,
                    cost_basis, realized_pnl, updated_at)
                   VALUES (?, 'no', ?, 0.63, ?, ?, ?,
                           '2026-04-24 12:00:00')""",
                (
                    TICKER, paper_count, paper_market_price,
                    open_count * 0.63,
                    (open_count * 1.0 if paper_market_price == 1.0
                     else 0.0) - open_count * 0.63,
                ),
            )
            await db.conn.commit()
        finally:
            await db.close()

    asyncio.run(_s())


def _closes(db_path: Path) -> list[dict]:
    async def _r() -> list[dict]:
        db = Database(db_path)
        await db.connect()
        try:
            trades = await get_trades(db, ticker=TICKER)
            return [t for t in trades if t["action"] == "close"]
        finally:
            await db.close()

    return asyncio.run(_r())


def test_ghost_settlement_gets_close_at_historical_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path)  # open, no close, paper settled as win (mp=1.0)
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, ["backfill-settlements"])
    assert result.exit_code == 0, result.output

    closes = _closes(db_path)
    assert len(closes) == 1
    c = closes[0]
    assert c["agent"] == "settlement"
    assert c["price"] == 1.0
    assert c["count"] == 100
    assert c["resolved_outcome"] == "no"  # NO side won → resolved no
    # Historical timestamp from paper_positions.updated_at — keeps the
    # backfilled P&L out of today's daily-loss trigger.
    assert c["timestamp"].startswith("2026-04-24")


def test_backfill_close_carries_entry_analytics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#656: the settlement close inherits the open row's analytics
    (seeded prob 0.8 / score 70 / edge 0.1 / kelly 0.02) instead of
    TradeDecision's 0.0 defaults — calibration audits read entry vs
    outcome off the close row directly."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, ["backfill-settlements"])
    assert result.exit_code == 0, result.output

    [c] = _closes(db_path)
    assert c["model_probability"] == 0.8
    assert c["gimme_score"] == 70.0
    assert c["edge"] == 0.1
    assert c["kelly_fraction"] == 0.02


def test_second_run_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _patch_config(monkeypatch, db_path)

    runner.invoke(app, ["backfill-settlements"])
    result = runner.invoke(app, ["backfill-settlements"])
    assert result.exit_code == 0, result.output
    assert len(_closes(db_path)) == 1  # no duplicate


def test_dry_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, ["backfill-settlements", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert "Would insert 1" in result.output
    assert _closes(db_path) == []


def test_drift_row_repaired_to_settlement_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconcile drift close at mark 0.705 whose market settled at
    0.0 (loss) must be repriced — the KXJOBLESSCLAIMS case."""
    db_path = tmp_path / "test.db"
    _seed(
        db_path, close_count=100, close_agent="reconcile",
        close_price=0.705, paper_market_price=0.0,
    )
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, ["backfill-settlements"])
    assert result.exit_code == 0, result.output

    closes = _closes(db_path)
    assert len(closes) == 1  # residual 0 → no ghost insert
    c = closes[0]
    assert c["agent"] == "reconcile"  # agent unchanged (#622 semantics)
    assert c["price"] == 0.0
    assert "corrected to settlement value" in c["rationale"]
    assert c["resolved_outcome"] == "yes"  # NO side lost → resolved yes


def test_open_positions_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """paper_positions rows with count > 0 (still open) are skipped."""
    db_path = tmp_path / "test.db"
    _seed(db_path, paper_count=100, paper_market_price=0.9)
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, ["backfill-settlements"])
    assert result.exit_code == 0, result.output
    assert _closes(db_path) == []


def test_fully_closed_position_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settled row whose closes already cover the opens (a real
    closer close exists) must not get a backfill close."""
    db_path = tmp_path / "test.db"
    _seed(db_path, close_count=100, close_agent="closer",
          close_price=0.9, paper_market_price=1.0)
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, ["backfill-settlements"])
    assert result.exit_code == 0, result.output
    closes = _closes(db_path)
    assert len(closes) == 1
    assert closes[0]["agent"] == "closer"


def test_resolved_outcome_conflict_prefers_paper_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The KXUE-UK26FEB-5.1 case: log-outcome recorded 'no' (which
    would mean the NO side won) but the broker settled it at 0.0 —
    paper truth wins, loudly."""
    db_path = tmp_path / "test.db"
    _seed(db_path, paper_market_price=0.0)  # NO side lost → truth 'yes'

    async def _set_wrong_outcome() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await db.conn.execute(
                "UPDATE trades SET resolved_outcome = 'no'"
                " WHERE ticker = ?", (TICKER,),
            )
            await db.conn.commit()
        finally:
            await db.close()

    asyncio.run(_set_wrong_outcome())
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, ["backfill-settlements"])
    assert result.exit_code == 0, result.output
    assert "conflicted with paper truth" in result.output

    closes = _closes(db_path)
    assert len(closes) == 1
    assert closes[0]["price"] == 0.0
    assert closes[0]["resolved_outcome"] == "yes"


def test_backfill_isolated_from_todays_daily_pnl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The historical timestamp keeps the correction out of today's
    daily-loss trigger — the property that makes the backfill safe to
    run on a live system (#653)."""
    db_path = tmp_path / "test.db"
    _seed(db_path, paper_market_price=0.0)  # settled loss
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, ["backfill-settlements"])
    assert result.exit_code == 0, result.output

    async def _daily() -> float:
        from datetime import UTC
        from datetime import datetime as dt

        from gimmes.store.queries import get_daily_pnl

        db = Database(db_path)
        await db.connect()
        try:
            return await get_daily_pnl(
                db, today=dt.now(UTC).strftime("%Y-%m-%d"),
            )
        finally:
            await db.close()

    assert asyncio.run(_daily()) == 0.0


def test_backfill_null_timestamp_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable settle timestamp → the close stamps NOW and the
    command warns that it will count toward today's loss trigger
    (updated_at is NOT NULL, so malformed strings are the real
    fallback trigger)."""
    db_path = tmp_path / "test.db"
    _seed(db_path)

    async def _null_ts() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await db.conn.execute(
                "UPDATE paper_positions SET updated_at ="
                " 'not-a-timestamp' WHERE ticker = ?", (TICKER,),
            )
            await db.conn.commit()
        finally:
            await db.close()

    asyncio.run(_null_ts())
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, ["backfill-settlements"])
    assert result.exit_code == 0, result.output
    assert "no usable settle timestamp" in result.output.lower()
    assert len(_closes(db_path)) == 1


def test_backfill_without_paper_positions_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Championship-only DBs have no paper_positions — graceful no-op."""
    db_path = tmp_path / "test.db"

    async def _bare() -> None:
        db = Database(db_path)
        await db.connect()
        await db.close()

    asyncio.run(_bare())
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, ["backfill-settlements"])
    assert result.exit_code == 0, result.output
    assert "No paper_positions table" in result.output
