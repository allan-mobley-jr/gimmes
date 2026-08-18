"""#760: log-outcome verifies settlement against the live API — a
Monitor once stamped a JUNE data release onto the still-ACTIVE JULY
market, corrupting 138 rows with no error trail."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from typer.testing import CliRunner

from gimmes.cli import app
from gimmes.models.market import MarketStatus
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import insert_trade

runner = CliRunner()
TICKER = "KXPCECORE-26JUL-T0.3"


def _db_run(db_path: Path, fn):
    async def _go():
        db = Database(db_path)
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.close()

    return asyncio.run(_go())


async def _seed(db):
    await insert_trade(db, TradeDecision(
        ticker=TICKER, action=TradeDecision.Action.OPEN,
        side="no", count=100, price=0.5,
        model_probability=0.7, agent="closer",
    ))


def _config(db_path):
    cfg = MagicMock()
    cfg.db_path = db_path
    return cfg


def _market(status, result=""):
    m = MagicMock()
    m.status = status
    m.result = result
    m.close_time = "2026-08-26T12:25:00+00:00"
    return m


def _rows(db_path, code):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT severity, error_code, context FROM error_log"
        " WHERE error_code = ?", (code,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _outcomes(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT resolved_outcome FROM trades WHERE ticker = ?",
        (TICKER,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


class TestLogOutcomeGuard:
    def _run(self, db_path, *, market=None, fetch_effect=None,
             extra=()):
        get_market = AsyncMock(
            return_value=market, side_effect=fetch_effect,
        )
        with patch("gimmes.cli.load_config",
                   return_value=_config(db_path)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.client.KalshiClient"):
            return runner.invoke(app, [
                "log-outcome", TICKER, "--outcome", "no", *extra,
            ])

    def test_active_market_refused_with_error_row(
        self, tmp_path,
    ) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        result = self._run(
            db_path, market=_market(MarketStatus.ACTIVE),
        )
        assert result.exit_code == 1, result.output
        assert "Refused (#760)" in result.output
        assert _outcomes(db_path) == [None]
        rows = _rows(db_path, "outcome_market_not_settled")
        assert len(rows) == 1
        assert rows[0]["severity"] == "error"

    def test_finalized_market_writes(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        result = self._run(
            db_path, market=_market(MarketStatus.FINALIZED),
        )
        assert result.exit_code == 0, result.output
        assert _outcomes(db_path) == ["no"]

    def test_closed_with_result_permitted(self, tmp_path) -> None:
        """The hourly case: status closed but result published."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        result = self._run(
            db_path,
            market=_market(MarketStatus.CLOSED, result="no"),
        )
        assert result.exit_code == 0, result.output
        assert _outcomes(db_path) == ["no"]

    def test_closed_without_result_refused(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        result = self._run(
            db_path, market=_market(MarketStatus.CLOSED),
        )
        assert result.exit_code == 1, result.output
        assert _outcomes(db_path) == [None]

    def test_fetch_failure_names_override(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        result = self._run(
            db_path,
            fetch_effect=httpx.RequestError("gone"),
        )
        assert result.exit_code == 1, result.output
        assert "--override" in result.output
        assert _outcomes(db_path) == [None]

    def test_fetch_failure_with_override_writes_warning(
        self, tmp_path,
    ) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        result = self._run(
            db_path,
            fetch_effect=httpx.RequestError("gone"),
            extra=("--override", "delisted market"),
        )
        assert result.exit_code == 0, result.output
        assert _outcomes(db_path) == ["no"]
        rows = _rows(db_path, "outcome_override_used")
        assert len(rows) == 1
        assert rows[0]["severity"] == "warning"

    def test_active_with_override_still_refused(self, tmp_path) -> None:
        """Override never bypasses a live not-settled answer."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        result = self._run(
            db_path,
            market=_market(MarketStatus.ACTIVE),
            extra=("--override", "trust me"),
        )
        assert result.exit_code == 1, result.output
        assert _outcomes(db_path) == [None]

    def test_overwrite_corrects_wrong_outcome(self, tmp_path) -> None:
        """#760 split-brain defense: an authoritative log-outcome
        CORRECTS a wrong earlier stamp."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)

        async def _stamp_wrong(db):
            await db.conn.execute(
                "UPDATE trades SET resolved_outcome = 'yes'"
                " WHERE ticker = ?", (TICKER,),
            )
            await db.conn.commit()

        _db_run(db_path, _stamp_wrong)
        result = self._run(
            db_path, market=_market(MarketStatus.FINALIZED),
        )
        assert result.exit_code == 0, result.output
        assert _outcomes(db_path) == ["no"]
        assert "1 trade(s)" in result.output


class TestMigrationV20:
    def test_premature_rows_nulled(self, tmp_path) -> None:
        from gimmes.store.migrations import run_migrations

        db_path = tmp_path / "gimmes.db"

        async def _go(db):
            await insert_trade(db, TradeDecision(
                ticker=TICKER, action=TradeDecision.Action.OPEN,
                side="no", count=100, price=0.5,
                model_probability=0.7, agent="closer",
            ))
            await db.conn.execute(
                "UPDATE trades SET resolved_outcome = 'no'"
                " WHERE ticker = ?", (TICKER,),
            )
            await db.conn.execute(
                "DELETE FROM schema_version WHERE version = 20"
            )
            await db.conn.commit()
            await run_migrations(db)

        _db_run(db_path, _go)
        assert _outcomes(db_path) == [None]

    def test_settled_ticker_untouched(self, tmp_path) -> None:
        """The NOT EXISTS guard: a genuinely settled DB keeps the
        authoritative outcome."""
        from gimmes.store.migrations import run_migrations

        db_path = tmp_path / "gimmes.db"

        async def _go(db):
            await insert_trade(db, TradeDecision(
                ticker=TICKER, action=TradeDecision.Action.OPEN,
                side="no", count=100, price=0.5,
                model_probability=0.7, agent="closer",
            ))
            await insert_trade(db, TradeDecision(
                ticker=TICKER, action=TradeDecision.Action.CLOSE,
                side="no", count=100, price=1.0,
                model_probability=0.7, agent="settlement",
            ))
            await db.conn.execute(
                "UPDATE trades SET resolved_outcome = 'no'"
                " WHERE ticker = ?", (TICKER,),
            )
            await db.conn.execute(
                "DELETE FROM schema_version WHERE version = 20"
            )
            await db.conn.commit()
            await run_migrations(db)

        _db_run(db_path, _go)
        assert set(_outcomes(db_path)) == {"no"}


def test_market_info_renders_result_row(tmp_path) -> None:
    """#760: the Result row is the checkable half of the field test."""
    m = MagicMock()
    m.status.value = "finalized"
    m.status = MarketStatus.FINALIZED
    m.result = "yes"
    m.midpoint = 0.5
    m.last_price = 0.5
    m.yes_bid = 0.4
    m.yes_ask = 0.6
    m.title = "t"
    m.subtitle = ""
    m.volume = 1
    m.volume_24h = 1
    m.open_interest = 1
    m.close_time = None
    m.rules_primary = "Resolves YES if X."
    m.series_ticker = "KX"
    m.event_ticker = "KX-26AUG"
    from gimmes.models.market import Orderbook

    with patch("gimmes.cli.load_config",
               return_value=_config(tmp_path / "gimmes.db")), \
         patch("gimmes.kalshi.markets.get_market",
               AsyncMock(return_value=m)), \
         patch("gimmes.kalshi.markets.get_orderbook",
               AsyncMock(return_value=Orderbook(ticker="KX-26AUG-T1"))), \
         patch("gimmes.kalshi.client.KalshiClient"):
        result = runner.invoke(app, ["market-info", "KX-26AUG-T1"])
    out = " ".join(result.output.split())
    assert "Result" in out
    assert "yes" in out
