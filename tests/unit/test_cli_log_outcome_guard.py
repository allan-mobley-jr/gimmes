"""#760: log-outcome verifies settlement against the live API — a
Monitor once stamped a JUNE data release onto the still-ACTIVE JULY
market, corrupting 138 rows with no error trail."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from gimmes.cli import app
from gimmes.models.market import MarketStatus
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import insert_trade

runner = CliRunner()
TICKER = "KXPCECORE-26JUL-T0.3"
# #815: the guard classifies refusals by close_time — a past close is
# settlement lag, a future close is a premature (Monitor) call.
_PAST = "2020-01-01T00:00:00+00:00"
_FUTURE = (datetime.now(UTC) + timedelta(days=30)).isoformat()


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


def _market(status, result="", close_time=_PAST):
    m = MagicMock()
    m.status = status
    m.result = result
    m.close_time = close_time
    return m


def _flat(text: str) -> str:
    """Rich wraps console output at 80 cols — compare on one line."""
    return " ".join(text.split())


def _rows(db_path, code):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT severity, error_code, message, context FROM error_log"
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
             extra=(), outcome="no"):
        get_market = AsyncMock(
            return_value=market, side_effect=fetch_effect,
        )
        with patch("gimmes.cli.load_config",
                   return_value=_config(db_path)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.client.KalshiClient"):
            return runner.invoke(app, [
                "log-outcome", TICKER, "--outcome", outcome, *extra,
            ])

    def _refused(self, db_path, market):
        """Shared shape of every not-settled refusal (#815): exit 1,
        the #760 message plus the protocol note, outcome untouched,
        and exactly one INFO trace row."""
        result = self._run(db_path, market=market)
        assert result.exit_code == 1, result.output
        assert "Refused (#760)" in result.output
        assert "Monitor protocol error (#815)" in _flat(result.output)
        assert _outcomes(db_path) == [None]
        rows = _rows(db_path, "outcome_market_not_settled")
        assert len(rows) == 1
        assert rows[0]["severity"] == "info"
        assert rows[0]["message"].startswith("Monitor protocol error (#815)")
        return rows[0]

    def test_active_market_refused_with_info_row(self, tmp_path) -> None:
        """#815: a refusal is the guard working — INFO, never ERROR, so
        Groundskeeper's generic info-suppress rule absorbs it."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        row = self._refused(
            db_path, _market(MarketStatus.ACTIVE, close_time=_PAST),
        )
        assert "(future)" not in row["message"]
        assert json.loads(row["context"])["premature"] is False

    def test_future_close_flagged_premature(self, tmp_path) -> None:
        """A close_time still ahead is recorded as premature in the
        trace (aware datetime — the real Market.close_time type)."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        aware_future = datetime.now(UTC) + timedelta(days=30)
        row = self._refused(
            db_path, _market(MarketStatus.ACTIVE, close_time=aware_future),
        )
        assert "(future)" in row["message"]
        assert json.loads(row["context"])["premature"] is True

    def test_unknown_status_refused_as_info(self, tmp_path) -> None:
        """#787 UNKNOWN status is not settled and rides the same path."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        row = self._refused(
            db_path, _market(MarketStatus.UNKNOWN, close_time=_FUTURE),
        )
        assert json.loads(row["context"])["premature"] is True

    def test_close_time_none_not_premature(self, tmp_path) -> None:
        """Unknown close_time still traces; premature stays False."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        row = self._refused(
            db_path, _market(MarketStatus.ACTIVE, close_time=None),
        )
        assert json.loads(row["context"])["premature"] is False

    def test_every_attempt_traced(self, tmp_path) -> None:
        """No dedupe: each premature call is its own protocol breach."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        market = _market(MarketStatus.CLOSED, close_time=_PAST)
        for _ in range(2):
            result = self._run(db_path, market=market)
            assert result.exit_code == 1, result.output
        assert _outcomes(db_path) == [None]
        rows = _rows(db_path, "outcome_market_not_settled")
        assert [r["severity"] for r in rows] == ["info", "info"]
        # Canonical serialization pin for operators grepping context.
        assert rows[0]["context"] == json.dumps(
            json.loads(rows[0]["context"]), sort_keys=True,
        )

    def test_error_log_failure_still_refuses(self, tmp_path) -> None:
        """Observability is best-effort: a broken error_log store must
        not eclipse the refusal or let the outcome through."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        with patch(
            "gimmes.store.database.Database",
            side_effect=RuntimeError("database is locked"),
        ):
            result = self._run(
                db_path, market=_market(MarketStatus.CLOSED, close_time=_PAST),
            )
        assert result.exit_code == 1, result.output
        assert "Refused (#760)" in result.output
        assert "Database error" not in result.output
        assert _outcomes(db_path) == [None]
        assert _rows(db_path, "outcome_market_not_settled") == []

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

    def test_result_conflict_refused_with_error_row(
        self, tmp_path,
    ) -> None:
        """A published result that contradicts --outcome wins: with
        overwrite semantics a wrong --outcome would clobber a
        correct row."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        result = self._run(
            db_path,
            market=_market(MarketStatus.FINALIZED, result="yes"),
        )
        assert result.exit_code == 1, result.output
        assert "contradicts" in result.output
        assert _outcomes(db_path) == [None]
        rows = _rows(db_path, "outcome_conflicts_with_result")
        assert len(rows) == 1
        assert rows[0]["severity"] == "error"

    def test_void_result_refused(self, tmp_path) -> None:
        """A voided market resolves neither yes nor no — no
        --outcome value is stampable onto it."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        result = self._run(
            db_path,
            market=_market(MarketStatus.FINALIZED, result="void"),
        )
        assert result.exit_code == 1, result.output
        assert _outcomes(db_path) == [None]
        assert len(_rows(db_path, "outcome_conflicts_with_result")) == 1

    def test_uppercase_matching_result_writes(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        result = self._run(
            db_path,
            market=_market(MarketStatus.FINALIZED, result="NO"),
        )
        assert result.exit_code == 0, result.output
        assert _outcomes(db_path) == ["no"]

    def test_matching_result_writes(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed)
        result = self._run(
            db_path,
            market=_market(MarketStatus.FINALIZED, result="no"),
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


@pytest.mark.parametrize(
    ("value", "expect"),
    [
        (None, None),
        ("not-a-date", None),
        ("", None),
        (MagicMock(), None),
        (12345, None),
        ("2020-01-01T00:00:00+00:00", datetime(2020, 1, 1, tzinfo=UTC)),
        ("2020-01-01T00:00:00Z", datetime(2020, 1, 1, tzinfo=UTC)),
        (datetime(2020, 1, 1), datetime(2020, 1, 1, tzinfo=UTC)),
        (datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 1, 1, tzinfo=UTC)),
    ],
)
def test_close_time_utc_normalization(value, expect) -> None:
    """#815: every close_time shape the guard can meet normalizes to
    aware UTC or None — never raises."""
    from gimmes.cli import _close_time_utc

    assert _close_time_utc(value) == expect


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
                "DELETE FROM schema_version WHERE version >= 20"
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
                "DELETE FROM schema_version WHERE version >= 20"
            )
            await db.conn.commit()
            await run_migrations(db)

        _db_run(db_path, _go)
        assert set(_outcomes(db_path)) == {"no"}


def test_market_info_renders_result_row(tmp_path) -> None:
    """#760: the Result row is the checkable half of the field test."""
    m = MagicMock()
    m.ticker = "KX-26AUG-T1"
    m.status.value = "finalized"
    m.status = MarketStatus.FINALIZED
    m.result = "yes"
    m.spread = 0.2
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
    from io import StringIO

    from rich.console import Console

    from gimmes.models.market import Orderbook

    buf = StringIO()
    with patch("gimmes.cli.load_config",
               return_value=_config(tmp_path / "gimmes.db")), \
         patch("gimmes.kalshi.markets.get_market",
               AsyncMock(return_value=m)), \
         patch("gimmes.kalshi.markets.get_orderbook",
               AsyncMock(return_value=Orderbook(ticker="KX-26AUG-T1"))), \
         patch("gimmes.kalshi.client.KalshiClient"), \
         patch("gimmes.cli.console", Console(file=buf, width=200)), \
         patch(
             "gimmes.reporting.formatter.console",
             Console(file=buf, width=200),
         ):
        result = runner.invoke(app, ["market-info", "KX-26AUG-T1"])
    out = " ".join(buf.getvalue().split())
    assert result.exit_code == 0, buf.getvalue()
    assert "Result" in out
    assert "yes" in out
