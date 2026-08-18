"""Championship reconcile trues up per-order close rows (#698).

Two drift directions, one convergence target (ledger sum == Kalshi
fill sum per order_id): a canceled/never-filled resting sell leaves
close rows for an exit that never traded (annul/shrink, #690
semantics); fills landing AFTER placement are never logged (#744
logs only the placement-time fill) — append the delta at the
fill-weighted price. A ticker/side with an agent='settlement' close
is never touched, and a still-resting order is never annulled.

Synchronous CLI tests (asyncio.run for setup) — CliRunner drives the
command's own asyncio.run, which cannot nest (#663 pattern).
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gimmes import cli as cli_module
from gimmes.cli import app
from gimmes.kalshi import orders as orders_module
from gimmes.kalshi import portfolio as portfolio_module
from gimmes.models.order import Fill, Order, OrderAction, OrderSide
from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import insert_trade, upsert_position

runner = CliRunner()

TICKER = "KXCPI-26APR-T0.5"
OID = "ord-698-1"


def _fill(count: int, no_price: float, created: str) -> Fill:
    return Fill(
        trade_id=f"t-{created}", order_id=OID, ticker=TICKER,
        action=OrderAction.SELL, side=OrderSide.NO, count=count,
        yes_price=round(1 - no_price, 4), no_price=no_price,
        created_time=created, is_taker=False,
    )


def _resting_order(remaining: int = 40) -> Order:
    return Order(
        order_id=OID, ticker=TICKER, action=OrderAction.SELL,
        side=OrderSide.NO, status="resting", yes_price=0.1,
        no_price=0.9, count=100, remaining_count=remaining,
        created_time="2026-08-17T10:00:00Z", client_order_id="c1",
    )


def _seed(
    db_path: Path, *, close_count: int | None = 100,
    close_agent: str = "closer", keep_position: bool = True,
    settlement_close: bool = False,
) -> None:
    async def _s() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await insert_trade(db, TradeDecision(
                ticker=TICKER, action=TradeDecision.Action.OPEN,
                side="no", count=100, price=0.63,
                model_probability=0.8, agent="closer",
            ))
            if close_count is not None:
                await insert_trade(db, TradeDecision(
                    ticker=TICKER, action=TradeDecision.Action.CLOSE,
                    side="no", count=close_count, price=0.9,
                    agent=close_agent, order_id=OID,
                ))
            if settlement_close:
                await insert_trade(db, TradeDecision(
                    ticker=TICKER, action=TradeDecision.Action.CLOSE,
                    side="no", count=100, price=1.0,
                    agent="settlement",
                ))
            if keep_position:
                await upsert_position(db, Position(
                    ticker=TICKER, title="CPI", side="no", count=100,
                    avg_price=0.63, market_price=0.7,
                    cost_basis=63.0, market_value=70.0,
                    unrealized_pnl=7.0, realized_pnl=0.0,
                ))
        finally:
            await db.close()

    asyncio.run(_s())


def _wire(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, *,
    resting: list[Order] = [], fills: list[Fill] = [],
    fills_error: bool = False, fills_cursor: str | None = None,
) -> None:
    cfg = MagicMock()
    cfg.db_path = db_path
    cfg.is_championship = True
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

    @asynccontextmanager
    async def _ctx(_config):  # type: ignore[no-untyped-def]
        db = Database(db_path)
        await db.connect()
        try:
            yield MagicMock(), None, db
        finally:
            await db.close()

    monkeypatch.setattr(cli_module, "trading_context", _ctx)

    async def _no_positions(_client):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr(
        portfolio_module, "get_all_positions", _no_positions,
    )

    async def _no_settlements(_client, _tickers, **_kw):  # type: ignore[no-untyped-def]
        return {}

    monkeypatch.setattr(
        portfolio_module, "get_settlements_for_tickers",
        _no_settlements,
    )

    async def _list_orders(_client, **_kw):  # type: ignore[no-untyped-def]
        # Kalshi returns "" (not a missing field) on the last page —
        # the completeness check must treat both as complete (#698
        # review: `cursor is None` would have disabled shrink/annul
        # on every production run).
        return resting, ""

    monkeypatch.setattr(orders_module, "list_orders", _list_orders)

    async def _list_fills(_client, **_kw):  # type: ignore[no-untyped-def]
        if fills_error:
            raise RuntimeError("fills endpoint down")
        return fills, fills_cursor

    monkeypatch.setattr(orders_module, "list_fills", _list_fills)


def _rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT action, reason, count, price, agent, order_id,"
        " rationale FROM trades WHERE ticker = ? ORDER BY id",
        (TICKER,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _errors(db_path: Path, code: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT error_code, context FROM error_log"
        " WHERE error_code = ?", (code,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_canceled_never_filled_annulled(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "g.db"
    _seed(db_path, close_count=100)
    _wire(monkeypatch, db_path, resting=[], fills=[])
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    closes = [r for r in _rows(db_path) if r["order_id"] == OID]
    assert len(closes) == 1
    assert closes[0]["action"] == "skip"
    assert closes[0]["reason"] == "order_canceled"
    assert "#698 repair" in closes[0]["rationale"]
    assert len(_errors(db_path, "close_row_repaired")) == 1


def test_partial_fill_shrinks_newest_row(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "g.db"
    _seed(db_path, close_count=100)
    _wire(
        monkeypatch, db_path, resting=[],
        fills=[_fill(60, 0.9, "2026-08-17T10:01:00Z")],
    )
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    closes = [
        r for r in _rows(db_path)
        if r["order_id"] == OID and r["action"] == "close"
    ]
    assert len(closes) == 1
    assert closes[0]["count"] == 60
    assert closes[0]["price"] == 0.9


def test_post_placement_fills_appended(monkeypatch, tmp_path) -> None:
    """Ledger logged 40 at placement but 100 filled: the 60
    post-placement contracts get a delta row at the fill-weighted
    price, inheriting entry analytics (#656 pattern)."""
    db_path = tmp_path / "g.db"
    _seed(db_path, close_count=40)
    _wire(
        monkeypatch, db_path, resting=[],
        fills=[
            _fill(40, 0.9, "2026-08-17T10:00:01Z"),
            _fill(40, 0.91, "2026-08-17T10:05:00Z"),
            _fill(20, 0.92, "2026-08-17T10:06:00Z"),
        ],
    )
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    closes = [
        r for r in _rows(db_path)
        if r["order_id"] == OID and r["action"] == "close"
    ]
    assert len(closes) == 2
    delta = closes[1]
    assert delta["count"] == 60
    # newest-first attribution: 20 @ 0.92 + 40 @ 0.91
    assert delta["price"] == round((20 * 0.92 + 40 * 0.91) / 60, 4)
    assert delta["agent"] == "closer"
    assert "#698" in delta["rationale"]


def test_still_resting_never_annulled(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "g.db"
    _seed(db_path, close_count=100)
    _wire(monkeypatch, db_path, resting=[_resting_order()], fills=[])
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    closes = [r for r in _rows(db_path) if r["order_id"] == OID]
    assert closes[0]["action"] == "close"
    assert closes[0]["count"] == 100


def test_settlement_close_never_touched(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "g.db"
    _seed(db_path, close_count=100, settlement_close=True)
    _wire(monkeypatch, db_path, resting=[], fills=[])
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    closes = [r for r in _rows(db_path) if r["order_id"] == OID]
    assert closes[0]["action"] == "close"
    assert len(_errors(db_path, "close_repair_settlement_owns")) == 1


def test_idempotent_second_run(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "g.db"
    _seed(db_path, close_count=40)
    fills = [
        _fill(40, 0.9, "2026-08-17T10:00:01Z"),
        _fill(60, 0.91, "2026-08-17T10:05:00Z"),
    ]
    _wire(monkeypatch, db_path, resting=[], fills=fills)
    assert runner.invoke(app, ["reconcile"]).exit_code == 0
    first = _rows(db_path)
    assert runner.invoke(app, ["reconcile"]).exit_code == 0
    assert _rows(db_path) == first
    assert len(_errors(db_path, "close_row_repaired")) == 1


def test_fills_failure_degrades(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "g.db"
    _seed(db_path, close_count=100)
    _wire(monkeypatch, db_path, resting=[], fills=[], fills_error=True)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    closes = [r for r in _rows(db_path) if r["order_id"] == OID]
    assert closes[0]["action"] == "close"  # untouched, not blocked


def test_paper_mode_repair_not_called(monkeypatch, tmp_path) -> None:
    """broker set -> the repair pass must never run (#255/#690 own
    the paper side)."""
    called = False

    async def _spy(client, db):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True

    monkeypatch.setattr(
        cli_module, "_repair_championship_close_rows", _spy,
    )
    db_path = tmp_path / "g.db"
    _seed(db_path, close_count=None)
    cfg = MagicMock()
    cfg.db_path = db_path
    cfg.is_championship = False
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

    @asynccontextmanager
    async def _ctx(_config):  # type: ignore[no-untyped-def]
        db = Database(db_path)
        await db.connect()
        try:
            broker = MagicMock()

            async def _pos():
                return []

            broker.get_positions = _pos
            yield MagicMock(), broker, db
        finally:
            await db.close()

    monkeypatch.setattr(cli_module, "trading_context", _ctx)

    async def _sweep(*a, **kw):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(
        cli_module, "_sweep_resting_paper_orders", _sweep,
    )
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert called is False


def test_truncated_fills_never_repair(monkeypatch, tmp_path) -> None:
    """A fills read that still has a cursor is a floor, not a total —
    shrinking on it would delete genuine close history."""
    db_path = tmp_path / "g.db"
    _seed(db_path, close_count=100)
    _wire(
        monkeypatch, db_path, resting=[],
        fills=[_fill(60, 0.9, "2026-08-17T10:01:00Z")],
        fills_cursor="more",
    )
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    closes = [r for r in _rows(db_path) if r["order_id"] == OID]
    assert closes[0]["action"] == "close"
    assert closes[0]["count"] == 100
    assert len(_errors(db_path, "close_repair_manual")) >= 1


def test_append_while_still_resting(monkeypatch, tmp_path) -> None:
    """Partial fills on a LIVE resting order append immediately —
    only shrink/annul waits for the order to go terminal."""
    db_path = tmp_path / "g.db"
    _seed(db_path, close_count=None)
    _wire(
        monkeypatch, db_path, resting=[_resting_order(remaining=60)],
        fills=[_fill(40, 0.9, "2026-08-17T10:01:00Z")],
    )
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    closes = [
        r for r in _rows(db_path)
        if r["order_id"] == OID and r["action"] == "close"
    ]
    assert len(closes) == 1
    assert closes[0]["count"] == 40
    assert closes[0]["price"] == 0.9
