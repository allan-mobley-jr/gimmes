"""#751/#720/#639/#635/#767: the position-lifecycle family.

Three root causes, one fix:
- RC-1: position-context treated a just-closed/settled position as a
  DATA_INTEGRITY fault — every position_not_found row in the live DB
  fired seconds-to-hours AFTER the ticker's own close trade.
- RC-2: only the `positions` command settled DETERMINED markets;
  risk-check and the order/validate position load kept counting a
  resolved position's cost basis and stale unrealized P&L.
- RC-3: `gimmes report` counts ledger residuals; one pre-#743 poisoned
  open row (523 logged vs 185 filled) made it report a phantom open
  position forever. Migration v19 repairs the row; the report gains a
  ledger-vs-positions consistency footnote + durable row.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from gimmes.cli import _mark_positions_to_market, app
from gimmes.models.market import MarketStatus
from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision
from gimmes.reporting.pnl import calculate_pnl
from gimmes.store.database import Database
from gimmes.store.migrations import run_migrations
from gimmes.store.queries import insert_candidate, insert_trade

runner = CliRunner()

TICKER = "KXBTCD-26AUG1118-T63599.99"


def _db_run(db_path: Path, fn):
    async def _go():
        db = Database(db_path)
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.close()

    return asyncio.run(_go())


def _config(db_path: Path) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = db_path
    cfg.strategy.side = "no"
    return cfg


def _read_errors(db_path: Path) -> list[dict]:
    async def _q(db):
        cursor = await db.conn.execute(
            "SELECT severity, error_code, category, component,"
            " message, context"
            " FROM error_log"
        )
        return [dict(r) for r in await cursor.fetchall()]

    return _db_run(db_path, _q)


async def _seed_closed_position(db) -> None:
    await insert_trade(db, TradeDecision(
        ticker=TICKER, action=TradeDecision.Action.OPEN,
        side="no", count=733, price=0.54,
        model_probability=0.70, agent="caddie-master",
        thesis="Shadow: WOULD-PASS | strike=$63599.99 ...",
    ))
    await insert_trade(db, TradeDecision(
        ticker=TICKER, action=TradeDecision.Action.CLOSE,
        side="no", count=733, price=0.0,
        model_probability=0.70, agent="settlement",
    ))


class TestClosedPositionContext:
    """RC-1: a settled position is a legitimate read."""

    def test_settled_position_renders_with_banner_no_error(
        self, tmp_path,
    ) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed_closed_position)
        with patch("gimmes.cli.load_config", return_value=_config(db_path)):
            result = runner.invoke(app, ["position-context", TICKER])
        assert result.exit_code == 0, result.output
        assert "POSITION CLOSED/SETTLED" in result.output
        assert "settlement" in result.output
        assert "--- OPEN TRADE ---" in result.output
        assert _read_errors(db_path) == []

    def test_settled_position_via_prefix(self, tmp_path) -> None:
        """The pass-2 traded resolution finds closed tickers by
        prefix — the exact Caddie Master step-2a read that produced
        every live position_not_found row."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed_closed_position)
        with patch("gimmes.cli.load_config", return_value=_config(db_path)):
            result = runner.invoke(
                app, ["position-context", "KXBTCD-26AUG1118"],
            )
        assert result.exit_code == 0, result.output
        assert "POSITION CLOSED/SETTLED" in result.output
        assert _read_errors(db_path) == []

    def test_sibling_candidate_and_skip_noise_does_not_ambiguate(
        self, tmp_path,
    ) -> None:
        """Review-found: pass 2 resolves against POSITION history only.
        Scout's sibling-strike candidate rows and Closer skip rows for
        the same event prefix must not explode the read into an
        ambiguous_ticker exit 1."""
        db_path = tmp_path / "gimmes.db"

        async def _seed(db):
            await _seed_closed_position(db)
            await insert_candidate(
                db, "KXBTCD-26AUG1118-T64099.99", "sibling", 0.4,
                0.7, 0.3, 60.0, "memo",
            )
            await insert_trade(db, TradeDecision(
                ticker="KXBTCD-26AUG1118-T64299.99",
                action=TradeDecision.Action.SKIP,
                side="no", count=0, price=0.2,
                model_probability=0.7, agent="caddie-master",
            ))

        _db_run(db_path, _seed)
        with patch("gimmes.cli.load_config", return_value=_config(db_path)):
            result = runner.invoke(
                app, ["position-context", "KXBTCD-26AUG1118"],
            )
        assert result.exit_code == 0, result.output
        assert "POSITION CLOSED/SETTLED" in result.output
        assert _read_errors(db_path) == []

    def test_candidates_only_ticker_still_logs_not_found(
        self, tmp_path,
    ) -> None:
        """Resolvable in pass 2 but never traded — the REAL
        position_not_found survives."""
        db_path = tmp_path / "gimmes.db"

        async def _seed(db):
            await insert_candidate(
                db, "KXNEVER-26AUG-T1", "never traded", 0.5, 0.7,
                0.2, 60.0, "memo",
            )

        _db_run(db_path, _seed)
        with patch("gimmes.cli.load_config", return_value=_config(db_path)):
            result = runner.invoke(
                app, ["position-context", "KXNEVER-26AUG-T1"],
            )
        assert result.exit_code == 0, result.output
        codes = [e["error_code"] for e in _read_errors(db_path)]
        assert codes == ["position_not_found"]

    def test_unknown_ticker_still_logs_not_found(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, _seed_closed_position)
        with patch("gimmes.cli.load_config", return_value=_config(db_path)):
            result = runner.invoke(app, ["position-context", "ZZZ"])
        assert result.exit_code == 0, result.output
        codes = [e["error_code"] for e in _read_errors(db_path)]
        assert codes == ["position_not_found"]


class TestMarkToMarketSettles:
    """RC-2: _mark_positions_to_market settles DETERMINED/FINALIZED
    markets instead of marking them at a stale mid."""

    @staticmethod
    def _market(status, result):
        market = MagicMock()
        market.status = status
        market.midpoint = 0.5
        market.last_price = 0.5
        market.close_time = None
        market.result = result
        return market

    def _run_mark(self, market):
        pos = Position(
            ticker=TICKER, side="no", count=733, avg_price=0.54,
            market_price=0.5, cost_basis=395.82, unrealized_pnl=0.0,
        )
        broker = AsyncMock()
        broker.get_positions = AsyncMock(side_effect=[[pos], []])
        client = AsyncMock()
        with patch(
            "gimmes.kalshi.markets.get_market",
            AsyncMock(return_value=market),
        ):
            asyncio.run(_mark_positions_to_market(broker, client))
        return broker

    def test_determined_market_settles_not_marks(self) -> None:
        broker = self._run_mark(
            self._market(MarketStatus.DETERMINED, "yes"),
        )
        broker.settle.assert_awaited_once_with(TICKER, "yes")
        broker.mark_to_market.assert_not_awaited()

    def test_finalized_market_settles(self) -> None:
        broker = self._run_mark(
            self._market(MarketStatus.FINALIZED, "no"),
        )
        broker.settle.assert_awaited_once_with(TICKER, "no")

    def test_determined_without_result_marks_not_settles(self) -> None:
        """An unsettleable result must never reach settle() — it would
        score every side as a loss."""
        broker = self._run_mark(
            self._market(MarketStatus.DETERMINED, ""),
        )
        broker.settle.assert_not_awaited()
        broker.mark_to_market.assert_awaited_once()

    def test_active_market_marks_not_settles(self) -> None:
        broker = self._run_mark(self._market(MarketStatus.ACTIVE, ""))
        broker.settle.assert_not_awaited()
        broker.mark_to_market.assert_awaited_once()


class TestMigrationV19:
    """RC-3: the poisoned pre-#743 ledger row is repaired in place."""

    POISONED_ORDER = "paper-da2137458555"

    def _seed_and_remigrate(self, db_path, *, count=523):
        async def _go(db):
            await insert_trade(db, TradeDecision(
                ticker="KXBTCD-26JUL1801-T63999.99",
                action=TradeDecision.Action.OPEN,
                side="no", count=count, price=0.945,
                model_probability=0.70, agent="closer",
                order_id=self.POISONED_ORDER,
                rationale="hourly entry",
            ))
            # Rewind the version stamp so the v19 block re-runs against
            # the newly-seeded row.
            await db.conn.execute(
                "DELETE FROM schema_version WHERE version = 19"
            )
            await db.conn.commit()
            await run_migrations(db)
            cursor = await db.conn.execute(
                "SELECT count, rationale FROM trades WHERE order_id = ?"
                " AND action = 'open'",
                (self.POISONED_ORDER,),
            )
            return dict(await cursor.fetchone())

        return _db_run(db_path, _go)

    def test_poisoned_row_corrected(self, tmp_path) -> None:
        row = self._seed_and_remigrate(tmp_path / "gimmes.db")
        assert row["count"] == 185
        assert "count corrected 523→185" in row["rationale"]

    def test_idempotent_and_fingerprinted(self, tmp_path) -> None:
        """A row with the same order_id but a different count (already
        corrected, or never poisoned) is untouched."""
        row = self._seed_and_remigrate(
            tmp_path / "gimmes.db", count=185,
        )
        assert row["count"] == 185
        assert "count corrected" not in row["rationale"]


class TestReportConsistencyFootnote:
    """RC-3: report names a ledger-vs-positions divergence and leaves a
    durable position_count_mismatch row."""

    def test_divergence_prints_footnote_and_row(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"

        async def _seed(db):
            # Ledger residual: open 523, close 185 — phantom open.
            await insert_trade(db, TradeDecision(
                ticker="KXGHOST-26JUL-T1",
                action=TradeDecision.Action.OPEN,
                side="no", count=523, price=0.9,
                model_probability=0.7, agent="closer",
            ))
            await insert_trade(db, TradeDecision(
                ticker="KXGHOST-26JUL-T1",
                action=TradeDecision.Action.CLOSE,
                side="no", count=185, price=1.0,
                model_probability=0.7, agent="settlement",
            ))

        _db_run(db_path, _seed)
        with patch("gimmes.cli.load_config", return_value=_config(db_path)):
            result = runner.invoke(app, ["report"])
        assert result.exit_code == 0, result.output
        assert "Open-count divergence (#767)" in result.output
        rows = [
            e for e in _read_errors(db_path)
            if e["error_code"] == "position_count_mismatch"
        ]
        assert len(rows) == 1
        ctx = json.loads(rows[0]["context"])
        assert ctx["ledger_only"] == ["KXGHOST-26JUL-T1"]
        assert ctx["positions_only"] == []
        assert ctx["count_drift"] == {}
        # Canonical serialization pin (second-review-found): the dedup
        # depends on a stable byte form.
        assert rows[0]["context"] == json.dumps(ctx, sort_keys=True)

        # Change-detection (review-found): an unchanged divergence on
        # the next report run prints the footnote but writes NO second
        # row — one Groundskeeper signal per state change, not per
        # cycle.
        with patch("gimmes.cli.load_config", return_value=_config(db_path)):
            result = runner.invoke(app, ["report"])
        assert "Open-count divergence (#767)" in result.output
        rows = [
            e for e in _read_errors(db_path)
            if e["error_code"] == "position_count_mismatch"
        ]
        assert len(rows) == 1

    def test_count_drift_detected_while_open(self, tmp_path) -> None:
        """Review-found: a live re-poisoning (ledger residual != broker
        count on the SAME ticker) is visible while the position is
        still open, not only after settlement."""
        db_path = tmp_path / "gimmes.db"

        async def _seed(db):
            await insert_trade(db, TradeDecision(
                ticker="KXDRIFT-26AUG-T1",
                action=TradeDecision.Action.OPEN,
                side="no", count=523, price=0.9,
                model_probability=0.7, agent="closer",
            ))
            await db.conn.execute(
                "INSERT INTO positions"
                " (ticker, side, count, avg_price, cost_basis)"
                " VALUES ('KXDRIFT-26AUG-T1', 'no', 185, 0.9, 166.5)"
            )
            await db.conn.commit()

        _db_run(db_path, _seed)
        with patch("gimmes.cli.load_config", return_value=_config(db_path)):
            result = runner.invoke(app, ["report"])
        assert result.exit_code == 0, result.output
        assert "count-drift" in result.output
        rows = [
            e for e in _read_errors(db_path)
            if e["error_code"] == "position_count_mismatch"
        ]
        assert len(rows) == 1
        ctx = json.loads(rows[0]["context"])
        assert ctx["count_drift"] == {
            "KXDRIFT-26AUG-T1": {"ledger": 523, "positions": 185}
        }

    def test_consistent_state_stays_quiet(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"

        async def _seed(db):
            await insert_trade(db, TradeDecision(
                ticker="KXCLEAN-26AUG-T1",
                action=TradeDecision.Action.OPEN,
                side="no", count=100, price=0.5,
                model_probability=0.7, agent="closer",
            ))
            await db.conn.execute(
                "INSERT INTO positions"
                " (ticker, side, count, avg_price, cost_basis)"
                " VALUES ('KXCLEAN-26AUG-T1', 'no', 100, 0.5, 50.0)"
            )
            await db.conn.commit()

        _db_run(db_path, _seed)
        with patch("gimmes.cli.load_config", return_value=_config(db_path)):
            result = runner.invoke(app, ["report"])
        assert result.exit_code == 0, result.output
        assert "Open-count divergence" not in result.output
        assert _read_errors(db_path) == []


class TestPnlOpenResiduals:
    def test_residual_populates_open_tickers(self) -> None:
        summary = calculate_pnl([
            {
                "ticker": "KXOPEN-26AUG-T1", "side": "no",
                "action": "open", "count": 100, "price": 0.5,
                "timestamp": "2026-08-01T10:00:00+00:00",
            },
        ])
        assert summary.open_trades == 1
        assert summary.open_residuals == {"KXOPEN-26AUG-T1": 100}

    def test_fully_closed_ticker_absent(self) -> None:
        summary = calculate_pnl([
            {
                "ticker": "KXDONE-26AUG-T1", "side": "no",
                "action": "open", "count": 100, "price": 0.5,
                "timestamp": "2026-08-01T10:00:00+00:00",
            },
            {
                "ticker": "KXDONE-26AUG-T1", "side": "no",
                "action": "close", "count": 100, "price": 1.0,
                "timestamp": "2026-08-01T11:00:00+00:00",
            },
        ])
        assert summary.open_trades == 0
        assert summary.open_residuals == {}


class TestLegacyAndOrdering:
    def test_close_only_history_renders(self, tmp_path) -> None:
        """Legacy shape: a close row with no open row still renders
        the closed context (not position_not_found)."""
        db_path = tmp_path / "gimmes.db"

        async def _seed(db):
            await insert_trade(db, TradeDecision(
                ticker=TICKER, action=TradeDecision.Action.CLOSE,
                side="no", count=100, price=0.5,
                model_probability=0.7, agent="reconcile",
            ))

        _db_run(db_path, _seed)
        with patch("gimmes.cli.load_config", return_value=_config(db_path)):
            result = runner.invoke(app, ["position-context", TICKER])
        assert result.exit_code == 0, result.output
        assert "POSITION CLOSED/SETTLED" in result.output
        assert "No open trade row recorded" in result.output
        assert _read_errors(db_path) == []

    def test_last_close_row_picks_newest(self, tmp_path) -> None:
        from gimmes.store.queries import get_last_close_row

        db_path = tmp_path / "gimmes.db"

        async def _go(db):
            await insert_trade(db, TradeDecision(
                ticker=TICKER, action=TradeDecision.Action.CLOSE,
                side="no", count=50, price=0.8,
                model_probability=0.7, agent="closer",
            ))
            await insert_trade(db, TradeDecision(
                ticker=TICKER, action=TradeDecision.Action.CLOSE,
                side="no", count=683, price=0.0,
                model_probability=0.7, agent="settlement",
            ))
            return await get_last_close_row(db, TICKER)

        row = _db_run(db_path, _go)
        assert row["agent"] == "settlement"
        assert row["count"] == 683


class TestOpenPositionMissingTradeLog:
    def test_open_position_without_trade_rows_renders(
        self, tmp_path,
    ) -> None:
        """Copilot review: a positions row with no trade log (imported/
        synced) must render context, not claim position_not_found."""
        db_path = tmp_path / "gimmes.db"

        async def _seed(db):
            await db.conn.execute(
                "INSERT INTO positions"
                " (ticker, side, count, avg_price, cost_basis)"
                " VALUES (?, 'no', 100, 0.5, 50.0)",
                (TICKER,),
            )
            await db.conn.commit()

        _db_run(db_path, _seed)
        with patch("gimmes.cli.load_config", return_value=_config(db_path)):
            result = runner.invoke(app, ["position-context", TICKER])
        assert result.exit_code == 0, result.output
        assert "Position Context:" in result.output
        assert "No open trade row recorded" in result.output
        assert "POSITION CLOSED/SETTLED" not in result.output
        assert _read_errors(db_path) == []


class TestKnownMarketsSettle:
    """#781 triage regression pin: a caller-supplied market for a held
    ticker gets the settle check — the old known_prices form skipped
    it, so ordering a ticker you already hold could size against a
    just-resolved position."""

    def test_known_market_settled_settles_not_marks(self) -> None:
        market = MagicMock()
        market.status = MarketStatus.DETERMINED
        market.result = "yes"
        market.midpoint = 0.5
        market.last_price = 0.5
        market.close_time = None
        pos = Position(
            ticker=TICKER, side="no", count=100, avg_price=0.5,
            market_price=0.5, cost_basis=50.0, unrealized_pnl=0.0,
        )
        broker = AsyncMock()
        broker.get_positions = AsyncMock(side_effect=[[pos], []])
        client = AsyncMock()
        get_market = AsyncMock(
            side_effect=AssertionError("must not fetch a known market"),
        )
        with patch("gimmes.kalshi.markets.get_market", get_market):
            asyncio.run(_mark_positions_to_market(
                broker, client, known_markets={TICKER: market},
            ))
        broker.settle.assert_awaited_once_with(TICKER, "yes")
        broker.mark_to_market.assert_not_awaited()
        get_market.assert_not_awaited()

    def test_known_market_determined_without_result_marks(self) -> None:
        """Fail-open pin (review-found): DETERMINED with an empty
        result must mark, not settle — a future 'settle on empty
        result' change has to be deliberate."""
        market = MagicMock()
        market.status = MarketStatus.DETERMINED
        market.result = ""
        market.midpoint = 0.5
        market.last_price = 0.5
        market.close_time = None
        pos = Position(
            ticker=TICKER, side="no", count=100, avg_price=0.5,
            market_price=0.5, cost_basis=50.0, unrealized_pnl=0.0,
        )
        broker = AsyncMock()
        broker.get_positions = AsyncMock(side_effect=[[pos], []])
        with patch(
            "gimmes.kalshi.markets.get_market",
            AsyncMock(side_effect=AssertionError("no fetch")),
        ):
            asyncio.run(_mark_positions_to_market(
                broker, AsyncMock(), known_markets={TICKER: market},
            ))
        broker.settle.assert_not_awaited()
        broker.mark_to_market.assert_awaited_once()


class TestPastClosePositions:
    """#783: open positions past market close without settlement get a
    console note + change-detected WARNING row. Reason separates
    'Kalshi is slow' (awaiting_determination) from actionable states."""

    @staticmethod
    def _market(status, *, close_minutes_ago=None, result=""):
        from datetime import UTC, datetime, timedelta

        m = MagicMock()
        m.status = status
        m.result = result
        m.midpoint = 0.5
        m.last_price = 0.5
        m.close_time = (
            datetime.now(UTC) - timedelta(minutes=close_minutes_ago)
            if close_minutes_ago is not None else None
        )
        return m

    def _pos(self):
        return Position(
            ticker=TICKER, side="no", count=100, avg_price=0.5,
            market_price=0.5, cost_basis=50.0, unrealized_pnl=0.0,
        )

    def _run_mark(self, db_path, market, *, threshold=30,
                  settle_effect=None):
        broker = AsyncMock()
        broker.get_positions = AsyncMock(
            side_effect=[[self._pos()], []],
        )
        if settle_effect is not None:
            broker.settle = AsyncMock(side_effect=settle_effect)

        async def _go():
            db = Database(db_path)
            await db.connect()
            try:
                with patch(
                    "gimmes.kalshi.markets.get_market",
                    AsyncMock(return_value=market),
                ):
                    await _mark_positions_to_market(
                        broker, AsyncMock(), db=db,
                        past_close_minutes=threshold,
                    )
            finally:
                await db.close()

        asyncio.run(_go())
        return broker

    def _rows(self, db_path):
        return [
            e for e in _read_errors(db_path)
            if e["error_code"] == "position_past_close"
        ]

    def test_past_close_flags_console_and_row(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        self._run_mark(
            db_path,
            self._market(MarketStatus.CLOSED, close_minutes_ago=120),
        )
        rows = self._rows(db_path)
        assert len(rows) == 1
        assert rows[0]["severity"] == "warning"
        ctx = json.loads(rows[0]["context"])
        assert ctx[TICKER]["reason"] == "awaiting_determination"
        # Canonical serialization pin (dedup depends on it)
        assert rows[0]["context"] == json.dumps(ctx, sort_keys=True)

    def test_threshold_boundary(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        self._run_mark(
            db_path,
            self._market(MarketStatus.CLOSED, close_minutes_ago=29),
        )
        assert self._rows(db_path) == []
        self._run_mark(
            db_path,
            self._market(MarketStatus.CLOSED, close_minutes_ago=31),
        )
        assert len(self._rows(db_path)) == 1

    def test_change_detected_across_two_runs(self, tmp_path) -> None:
        from datetime import timedelta

        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        market = self._market(
            MarketStatus.CLOSED, close_minutes_ago=50,
        )
        self._run_mark(db_path, market)
        # Mutant pin: minutes_past must be EXCLUDED from the dedup
        # payload — advance the clock within the same bucket and
        # assert no re-log.
        market.close_time -= timedelta(minutes=5)
        self._run_mark(db_path, market)
        assert len(self._rows(db_path)) == 1

    def test_state_change_relogs(self, tmp_path) -> None:
        """Mutant pin: change-detection must RE-log on a changed
        state, not suppress whenever any prior row exists."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        self._run_mark(
            db_path,
            self._market(MarketStatus.CLOSED, close_minutes_ago=50),
        )
        self._run_mark(
            db_path,
            self._market(
                MarketStatus.DETERMINED, close_minutes_ago=50,
                result="",
            ),
        )
        assert len(self._rows(db_path)) == 2

    def test_stuck_position_relogs_at_bucket_doubling(
        self, tmp_path,
    ) -> None:
        """#783 review: a position stuck for hours re-logs when the
        minutes-past bucket doubles (1x -> 2x threshold), so the
        Groundskeeper 3+/24h pattern rule is reachable."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        self._run_mark(
            db_path,
            self._market(MarketStatus.CLOSED, close_minutes_ago=40),
        )
        self._run_mark(
            db_path,
            self._market(MarketStatus.CLOSED, close_minutes_ago=70),
        )
        assert len(self._rows(db_path)) == 2

    def test_zero_threshold_suppresses_settle_failed_too(
        self, tmp_path,
    ) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        self._run_mark(
            db_path,
            self._market(
                MarketStatus.DETERMINED, close_minutes_ago=5,
                result="yes",
            ),
            threshold=0,
            settle_effect=RuntimeError("locked"),
        )
        assert self._rows(db_path) == []

    def test_settled_market_never_flags(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        broker = self._run_mark(
            db_path,
            self._market(
                MarketStatus.DETERMINED, close_minutes_ago=120,
                result="yes",
            ),
        )
        broker.settle.assert_awaited_once()
        assert self._rows(db_path) == []

    def test_settle_failure_flags_actionable(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        self._run_mark(
            db_path,
            self._market(
                MarketStatus.DETERMINED, close_minutes_ago=5,
                result="yes",
            ),
            settle_effect=RuntimeError("locked"),
        )
        rows = self._rows(db_path)
        assert len(rows) == 1
        ctx = json.loads(rows[0]["context"])
        assert ctx[TICKER]["reason"] == "settle_failed"

    def test_close_time_none_skipped(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        self._run_mark(
            db_path, self._market(MarketStatus.ACTIVE),
        )
        assert self._rows(db_path) == []

    def test_naive_close_time_coerced(self, tmp_path) -> None:
        from datetime import datetime, timedelta

        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        market = self._market(MarketStatus.CLOSED)
        # UTC-derived naive (review-found: local-naive is tz-dependent)
        from datetime import UTC

        market.close_time = (
            datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
        )
        self._run_mark(db_path, market)
        assert len(self._rows(db_path)) == 1

    def test_zero_threshold_disables(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        self._run_mark(
            db_path,
            self._market(MarketStatus.CLOSED, close_minutes_ago=999),
            threshold=0,
        )
        assert self._rows(db_path) == []

    def test_determined_without_result_reason(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, lambda db: asyncio.sleep(0))
        self._run_mark(
            db_path,
            self._market(
                MarketStatus.DETERMINED, close_minutes_ago=120,
                result="",
            ),
        )
        rows = self._rows(db_path)
        assert len(rows) == 1
        ctx = json.loads(rows[0]["context"])
        assert ctx[TICKER]["reason"] == "determined_no_result"
