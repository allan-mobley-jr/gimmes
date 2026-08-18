"""#647: reconcile backfills empty positions.rules_primary snapshots.

The snapshot is written on the BUY path — resting fills, pre-v17
positions, and sync-recreated rows all leave it empty, and the
semantics guard silently passes them. Reconcile has a live client
in both modes; the backfill is best-effort and capped per run.
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
from gimmes.kalshi import markets as markets_module
from gimmes.models.portfolio import Position
from gimmes.store.database import Database
from gimmes.store.queries import upsert_position

runner = CliRunner()

RULES = "Resolves YES if the value exceeds 0.5 [preliminary]."


def _seed(db_path: Path, tickers: list[str]) -> list[Position]:
    positions = [
        Position(
            ticker=t, title=t, side="no", count=10, avg_price=0.5,
            market_price=0.5, cost_basis=5.0, market_value=5.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
        )
        for t in tickers
    ]

    async def _s() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            for p in positions:
                await upsert_position(db, p)
        finally:
            await db.close()

    asyncio.run(_s())
    return positions


def _wire(
    monkeypatch: pytest.MonkeyPatch, db_path: Path,
    positions: list[Position], *, fetch_error: bool = False,
) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = db_path
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

    @asynccontextmanager
    async def _ctx(_config):  # type: ignore[no-untyped-def]
        db = Database(db_path)
        await db.connect()
        try:
            broker = MagicMock()

            async def _pos():
                return positions

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

    fetches = MagicMock()

    async def _get_market(_client, ticker):  # type: ignore[no-untyped-def]
        fetches(ticker)
        if fetch_error:
            raise RuntimeError("api down")
        m = MagicMock()
        m.rules_primary = RULES
        return m

    monkeypatch.setattr(markets_module, "get_market", _get_market)
    return fetches


def _rules(db_path: Path) -> dict[str, str | None]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT ticker, rules_primary FROM positions"
    ).fetchall()
    conn.close()
    return dict(rows)


def test_empty_snapshot_backfilled(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "g.db"
    positions = _seed(db_path, ["KX-A", "KX-B"])
    _wire(monkeypatch, db_path, positions)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert _rules(db_path) == {"KX-A": RULES, "KX-B": RULES}
    assert "Backfilled settlement-rules snapshots for 2" in (
        result.output
    )


def test_existing_snapshot_not_refetched(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "g.db"
    positions = _seed(db_path, ["KX-A"])
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE positions SET rules_primary = 'original'"
        " WHERE ticker = 'KX-A'"
    )
    conn.commit()
    conn.close()
    fetches = _wire(monkeypatch, db_path, positions)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert _rules(db_path)["KX-A"] == "original"
    fetches.assert_not_called()


def test_fetch_failure_degrades(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "g.db"
    positions = _seed(db_path, ["KX-A"])
    _wire(monkeypatch, db_path, positions, fetch_error=True)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert _rules(db_path)["KX-A"] in (None, "")


def test_backfill_capped_per_run(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "g.db"
    tickers = [f"KX-{i:02d}" for i in range(12)]
    positions = _seed(db_path, tickers)
    fetches = _wire(monkeypatch, db_path, positions)
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert fetches.call_count == 10
    filled = [t for t, r in _rules(db_path).items() if r == RULES]
    assert len(filled) == 10
