"""`log-trade` close rows inherit entry analytics (#656).

A close logged without --prob carries the entry decision's
model_probability/edge/kelly (and score, unless --score was given)
instead of TradeDecision's 0.0 defaults. Real seeded DB + CliRunner,
following the test_backfill_settlements pattern (synchronous seeding;
the CLI drives its own asyncio.run).
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
    cfg.strategy.side = "yes"
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)


def _seed_open(db_path: Path) -> None:
    async def _s() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            t = TradeDecision(
                ticker=TICKER,
                action=TradeDecision.Action.OPEN,
                side="yes",
                count=10,
                price=0.60,
                model_probability=0.9,
                gimme_score=82.0,
                edge=0.25,
                kelly_fraction=0.03,
                rationale="entry",
                agent="closer",
            )
            t.timestamp = datetime(2026, 4, 20, 12, tzinfo=UTC)
            await insert_trade(db, t)
        finally:
            await db.close()

    asyncio.run(_s())


def _close_row(db_path: Path) -> dict:
    async def _r() -> dict:
        db = Database(db_path)
        await db.connect()
        try:
            trades = await get_trades(db, ticker=TICKER)
            [c] = [t for t in trades if t["action"] == "close"]
            return c
        finally:
            await db.close()

    return asyncio.run(_r())


def _invoke_close(*extra: str) -> object:
    return runner.invoke(app, [
        "log-trade", TICKER, "--action", "close",
        "--side", "yes", "--count", "10", "--price", "0.80",
        *extra,
    ])


def test_close_without_prob_inherits_entry_analytics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_open(db_path)
    _patch_config(monkeypatch, db_path)

    result = _invoke_close()
    assert result.exit_code == 0, result.output

    c = _close_row(db_path)
    assert c["model_probability"] == 0.9
    assert c["gimme_score"] == 82.0
    assert c["edge"] == 0.25
    assert c["kelly_fraction"] == 0.03


def test_close_with_explicit_prob_keeps_close_time_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gimmes.strategy.scanner import effective_price

    db_path = tmp_path / "test.db"
    _seed_open(db_path)
    _patch_config(monkeypatch, db_path)

    result = _invoke_close("--prob", "0.5")
    assert result.exit_code == 0, result.output

    c = _close_row(db_path)
    assert c["model_probability"] == 0.5
    assert c["edge"] == pytest.approx(0.5 - effective_price(0.80, "yes"))
    assert c["gimme_score"] == 0.0
    assert c["kelly_fraction"] == 0.0


def test_explicit_score_survives_inheritance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_open(db_path)
    _patch_config(monkeypatch, db_path)

    result = _invoke_close("--score", "55")
    assert result.exit_code == 0, result.output

    c = _close_row(db_path)
    assert c["gimme_score"] == 55.0
    # prob/edge/kelly still inherited from the entry
    assert c["model_probability"] == 0.9
    assert c["edge"] == 0.25
    assert c["kelly_fraction"] == 0.03


def test_close_without_entry_keeps_zeros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"

    async def _bare() -> None:
        db = Database(db_path)
        await db.connect()
        await db.close()

    asyncio.run(_bare())
    _patch_config(monkeypatch, db_path)

    result = _invoke_close()
    assert result.exit_code == 0, result.output

    c = _close_row(db_path)
    assert c["model_probability"] == 0.0
    assert c["edge"] == 0.0
    assert c["gimme_score"] == 0.0
