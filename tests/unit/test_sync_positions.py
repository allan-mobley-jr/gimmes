"""Tests for sync_positions and sync_positions_with_trade functions."""

from __future__ import annotations

from datetime import datetime

import pytest

from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import (
    get_entry_analytics,
    get_positions,
    get_trades,
    insert_trade,
    sync_positions,
    sync_positions_with_trade,
    upsert_position,
)


@pytest.fixture
async def db(tmp_path):
    """Create a temp database."""
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


def _pos(ticker: str, count: int = 10, price: float = 0.5) -> Position:
    return Position(
        ticker=ticker, side="yes", count=count, avg_price=price,
        market_price=price, cost_basis=count * price,
    )


class TestSyncPositions:
    async def test_inserts_new_positions(self, db):
        positions = [_pos("AAPL"), _pos("GOOG")]
        await sync_positions(db, positions)

        stored = await get_positions(db)
        tickers = {p.ticker for p in stored}
        assert tickers == {"AAPL", "GOOG"}

    async def test_removes_stale_positions(self, db):
        # Seed an old position
        await upsert_position(db, _pos("OLD-TICKER"))
        stored = await get_positions(db)
        assert len(stored) == 1

        # Sync with different positions
        await sync_positions(db, [_pos("NEW-TICKER")])

        stored = await get_positions(db)
        tickers = {p.ticker for p in stored}
        assert "OLD-TICKER" not in tickers
        assert "NEW-TICKER" in tickers

    async def test_updates_existing_positions(self, db):
        await upsert_position(db, _pos("AAPL", count=5, price=0.3))

        # Sync with updated count
        await sync_positions(db, [_pos("AAPL", count=15, price=0.7)])

        stored = await get_positions(db)
        assert len(stored) == 1
        assert stored[0].count == 15
        assert stored[0].avg_price == 0.7

    async def test_empty_sync_clears_all(self, db):
        await upsert_position(db, _pos("A"))
        await upsert_position(db, _pos("B"))

        await sync_positions(db, [])

        stored = await get_positions(db)
        assert len(stored) == 0

    async def test_atomic_transaction(self, db):
        """All changes should happen in one transaction."""
        await upsert_position(db, _pos("KEEP"))
        await upsert_position(db, _pos("REMOVE"))

        await sync_positions(db, [_pos("KEEP", count=20), _pos("ADD")])

        stored = await get_positions(db)
        tickers = {p.ticker for p in stored}
        assert tickers == {"KEEP", "ADD"}
        keep = next(p for p in stored if p.ticker == "KEEP")
        assert keep.count == 20


def _trade(ticker: str = "KXTEST", price: float = 0.60) -> TradeDecision:
    return TradeDecision(
        ticker=ticker, action=TradeDecision.Action.OPEN,
        side="yes", count=10, price=price,
    )


class TestSyncPositionsWithTrade:
    async def test_syncs_positions_and_inserts_trade(self, db):
        """Both positions and trade should be written."""
        positions = [_pos("AAPL")]
        trade = _trade("AAPL")

        row_id = await sync_positions_with_trade(db, positions, trade)

        stored_pos = await get_positions(db)
        assert len(stored_pos) == 1
        assert stored_pos[0].ticker == "AAPL"

        trades = await get_trades(db, ticker="AAPL")
        assert len(trades) == 1
        assert row_id > 0

    async def test_rolls_back_on_trade_failure(self, db):
        """If the trade insert fails, positions should not be synced."""
        await upsert_position(db, _pos("OLD"))

        # Create a trade with an invalid action value that will fail on insert
        trade = _trade("NEW")
        # Monkey-patch to force a DB error during insert
        original_execute = db.conn.execute

        call_count = 0

        async def failing_execute(sql, params=None):
            nonlocal call_count
            call_count += 1
            # Let position sync SQL through, fail on the trade INSERT
            if "INSERT INTO trades" in str(sql):
                raise RuntimeError("simulated insert failure")
            if params:
                return await original_execute(sql, params)
            return await original_execute(sql)

        db._conn.execute = failing_execute  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="simulated insert failure"):
            await sync_positions_with_trade(db, [_pos("NEW")], trade)

        # Restore original execute for assertions
        db._conn.execute = original_execute  # type: ignore[assignment]

        # Positions should be unchanged (rolled back)
        stored = await get_positions(db)
        tickers = {p.ticker for p in stored}
        assert "OLD" in tickers
        assert "NEW" not in tickers

    async def test_removes_stale_positions_with_trade(self, db):
        """Old positions should be removed when syncing with trade."""
        await upsert_position(db, _pos("STALE"))

        await sync_positions_with_trade(
            db, [_pos("FRESH")], _trade("FRESH")
        )

        stored = await get_positions(db)
        tickers = {p.ticker for p in stored}
        assert "STALE" not in tickers
        assert "FRESH" in tickers


class TestGetPositionsStalenessWarning:
    async def test_no_warning_when_in_sync(self, db, caplog):
        """No warning when positions are fresher than trades."""
        # Insert trade then sync positions (normal flow)
        await insert_trade(db, _trade("AAPL"))
        await sync_positions(db, [_pos("AAPL")])

        import logging
        with caplog.at_level(logging.WARNING, logger="gimmes.store.queries"):
            await get_positions(db)

        assert "stale" not in caplog.text.lower()

    async def test_no_warning_when_empty(self, db, caplog):
        """No warning when there are no trades or positions."""
        import logging
        with caplog.at_level(logging.WARNING, logger="gimmes.store.queries"):
            await get_positions(db)

        assert "stale" not in caplog.text.lower()


# ---------------------------------------------------------------------------
# Reconcile-driven synthetic close (#609)
# ---------------------------------------------------------------------------


class TestReconcileDriftLogsSyntheticClose:
    async def test_removed_ticker_logs_synthetic_close_trade(self, db):
        """Pre-existing position absent from new sync → trades table
        gets a close row with agent='reconcile' and the last-known
        mark as the close price (#609)."""
        await upsert_position(db, _pos("KXCPI-26APR-T0.5", count=10, price=0.42))

        await sync_positions(db, [])

        trades = await get_trades(db)
        assert len(trades) == 1, trades
        t = trades[0]
        assert t["ticker"] == "KXCPI-26APR-T0.5"
        assert t["action"] == "close"
        assert t["agent"] == "reconcile"
        assert t["count"] == 10
        # Price falls back to market_price or avg_price — both 0.42
        # in the fixture. Verify it's NOT 0.0 (which would corrupt
        # daily P&L math).
        assert t["price"] == 0.42
        assert "reconcile drift" in t["rationale"]
        assert "#609" in t["rationale"]

    async def test_removed_ticker_logs_reconcile_divergence_decision_note(
        self, db,
    ):
        """The synthetic close also writes a decision note with
        Trigger: Reconcile-divergence so the #586 lockout query does
        NOT match — legitimate re-entry is allowed after reconcile
        drift (#609)."""
        from gimmes.store.queries import get_position_notes

        await upsert_position(db, _pos("KXCPI-26APR-T0.5"))
        await sync_positions(db, [])

        notes = await get_position_notes(
            db, "KXCPI-26APR-T0.5", note_type="decision",
        )
        assert len(notes) == 1, notes
        body = notes[0]["body"]
        assert "Decision: CLOSE" in body
        assert "Trigger: Reconcile-divergence" in body
        # CRITICAL invariant — the body MUST NOT contain
        # `Trigger: Stop-loss breach`. If it did, Caddie Master's
        # #586 lockout query would treat reconcile-driven drift as
        # a stop-loss event and silently block legitimate re-entry.
        assert "Trigger: Stop-loss breach" not in body
        assert notes[0]["agent"] == "reconcile"

    async def test_known_markets_resolves_reconciled_close(self, db):
        """After a reconcile-driven close, `resolve_ticker` finds the
        ticker via the trades table — closing the #586 lockout's
        `known_markets` resolver gap that #609 was filed to fix."""
        from gimmes.store.ticker_resolver import resolve_ticker

        await upsert_position(db, _pos("KXCPI-26APR-T0.5"))
        await sync_positions(db, [])

        # positions table no longer has the ticker (count=0)
        stored = await get_positions(db)
        assert all(p.ticker != "KXCPI-26APR-T0.5" for p in stored)

        # but known_markets (positions ∪ candidates ∪ trades) does,
        # because the synthetic close wrote a trades row.
        matches = await resolve_ticker(
            db, "KXCPI-26APR-T0.5", source="known_markets",
        )
        assert matches == ["KXCPI-26APR-T0.5"]

    async def test_sync_with_trade_excludes_closer_ticker(self, db):
        """When sync_positions_with_trade is called by the Closer,
        the caller's trade ticker is excluded from synthetic-close
        logging (the caller logs it explicitly). Other removed tickers
        — multi-ticker drift in the same sync — still get synthetic
        closes (#609)."""
        await upsert_position(db, _pos("CLOSER-TICKER"))
        await upsert_position(db, _pos("DRIFT-TICKER"))

        trade = TradeDecision(
            ticker="CLOSER-TICKER",
            action=TradeDecision.Action.CLOSE,
            side="yes", count=10, price=0.55,
            rationale="Closer sold full position",
            agent="closer",
        )
        await sync_positions_with_trade(db, [], trade)

        trades = await get_trades(db)
        by_agent = {t["agent"]: t["ticker"] for t in trades}
        # Closer's trade is logged (the caller's explicit log).
        assert by_agent.get("closer") == "CLOSER-TICKER"
        # DRIFT-TICKER gets a synthetic reconcile close.
        assert by_agent.get("reconcile") == "DRIFT-TICKER"

    async def test_reconcile_synthetic_close_is_idempotent(self, db):
        """Running sync_positions twice with the same empty set does
        not write duplicate synthetic closes — second call sees the
        position already absent and produces no new trades (#609)."""
        await upsert_position(db, _pos("KXCPI-26APR-T0.5"))
        await sync_positions(db, [])
        await sync_positions(db, [])

        trades = await get_trades(db)
        assert len(trades) == 1, trades

    async def test_zero_market_price_falls_back_to_avg_price(self, db):
        """If market_price is 0/null (e.g., position never marked),
        the synthetic close uses avg_price as the close mark — keeps
        daily P&L math honest. Falling back to 0.0 would corrupt P&L
        reports."""
        pos = Position(
            ticker="ZERO-MARK", side="yes", count=10,
            avg_price=0.42, market_price=0.0,
            cost_basis=4.2,
        )
        await upsert_position(db, pos)
        await sync_positions(db, [])

        trades = await get_trades(db)
        assert len(trades) == 1
        assert trades[0]["price"] == 0.42

    async def test_both_zero_marks_produce_zero_close_price(self, db):
        """Edge case: position with market_price=0 AND avg_price=0
        (newly-opened during a Kalshi outage, never marked). The
        fallback chain returns 0.0 — synthetic close at 0 yields a
        synthetic realized P&L of (0 - 0) * count = 0, which is
        mathematically neutral. Not a corruption — just a record
        of the close happening at the only price we have on file
        (the entry, which itself was zero)."""
        pos = Position(
            ticker="DOUBLE-ZERO", side="yes", count=10,
            avg_price=0.0, market_price=0.0,
            cost_basis=0.0,
        )
        await upsert_position(db, pos)
        await sync_positions(db, [])

        trades = await get_trades(db)
        assert len(trades) == 1
        assert trades[0]["price"] == 0.0
        assert trades[0]["agent"] == "reconcile"


def _open_trade(
    ticker: str,
    *,
    action: str = "open",
    prob: float = 0.9,
    score: float = 82.0,
    edge: float = 0.25,
    kelly: float = 0.03,
    ts: str = "2026-04-20T12:00:00+00:00",
) -> TradeDecision:
    t = TradeDecision(
        ticker=ticker,
        action=TradeDecision.Action(action),
        side="yes",
        count=10,
        price=0.60,
        model_probability=prob,
        gimme_score=score,
        edge=edge,
        kelly_fraction=kelly,
        rationale="entry",
        agent="closer",
    )
    t.timestamp = datetime.fromisoformat(ts)
    return t


class TestReconcileCloseCarriesEntryAnalytics:
    """#656: synthetic drift closes inherit the entry decision's
    analytics instead of TradeDecision's 0.0 defaults."""

    async def test_drift_close_inherits_entry_analytics(self, db):
        await insert_trade(db, _open_trade("KXCPI-26APR-T0.5"))
        await upsert_position(
            db, _pos("KXCPI-26APR-T0.5", count=10, price=0.42),
        )

        await sync_positions(db, [])

        closes = [
            t for t in await get_trades(db) if t["action"] == "close"
        ]
        assert len(closes) == 1
        c = closes[0]
        assert c["agent"] == "reconcile"
        assert c["model_probability"] == 0.9
        assert c["gimme_score"] == 82.0
        assert c["edge"] == 0.25
        assert c["kelly_fraction"] == 0.03

    async def test_drift_close_without_open_keeps_zeros(self, db):
        """No entry on record (pre-#609 DBs, seeded positions) — the
        drift close still writes, with honest zeros."""
        await upsert_position(db, _pos("NO-HISTORY", count=10, price=0.42))

        await sync_positions(db, [])

        [c] = await get_trades(db)
        assert c["action"] == "close"
        assert c["model_probability"] == 0
        assert c["gimme_score"] == 0
        assert c["edge"] == 0


class TestGetEntryAnalytics:
    async def test_returns_latest_entry_row(self, db):
        await insert_trade(db, _open_trade(
            "T", prob=0.7, score=60.0, ts="2026-04-01T12:00:00+00:00",
        ))
        await insert_trade(db, _open_trade(
            "T", action="size_up", prob=0.85, score=75.0, edge=0.18,
            kelly=0.04, ts="2026-04-05T12:00:00+00:00",
        ))

        entry = await get_entry_analytics(db, "T", "yes")
        assert entry == {
            "model_probability": 0.85,
            "gimme_score": 75.0,
            "edge": 0.18,
            "kelly_fraction": 0.04,
        }

    async def test_ignores_close_rows(self, db):
        close = _open_trade("T", prob=0.0, score=0.0, edge=0.0, kelly=0.0,
                            ts="2026-04-09T12:00:00+00:00")
        close.action = TradeDecision.Action.CLOSE
        await insert_trade(db, _open_trade(
            "T", prob=0.9, ts="2026-04-01T12:00:00+00:00",
        ))
        await insert_trade(db, close)

        entry = await get_entry_analytics(db, "T", "yes")
        assert entry is not None
        assert entry["model_probability"] == 0.9

    async def test_none_when_no_entry(self, db):
        assert await get_entry_analytics(db, "MISSING", "yes") is None

    async def test_side_must_match(self, db):
        await insert_trade(db, _open_trade("T"))  # side yes
        assert await get_entry_analytics(db, "T", "no") is None


def test_caddie_master_lockout_query_does_not_match_reconcile_body() -> None:
    """Cross-file drift guard (#609): the literal trigger strings
    `Trigger: Stop-loss breach` (in caddie-master.md's #586 lockout
    query) and `Trigger: Reconcile-divergence` (in queries.py's
    synthetic decision-note body) are coupled across files. A rename
    on either side breaks the lockout semantics with no other test
    failure. This test pins both literals and asserts the body
    generated by `_log_reconcile_closes` cannot match the lockout
    query."""
    from pathlib import Path

    cm_md = (
        Path(__file__).resolve().parents[2]
        / ".claude" / "agents" / "caddie-master.md"
    ).read_text()
    queries_py = (
        Path(__file__).resolve().parents[2]
        / "src" / "gimmes" / "store" / "queries.py"
    ).read_text()

    # Lockout query string must still match the body Reconcile-divergence
    # was designed NOT to satisfy.
    assert "Trigger: Stop-loss breach" in cm_md, (
        "caddie-master.md #586 lockout must still reference"
        " `Trigger: Stop-loss breach` literal. Rename here without"
        " a corresponding rename in queries.py's synthetic-close"
        " logic would break lockout semantics (#609)."
    )

    # Reconcile body uses Reconcile-divergence.
    assert "Trigger: Reconcile-divergence" in queries_py, (
        "queries.py's _log_reconcile_closes must still write"
        " `Trigger: Reconcile-divergence` in the decision body."
        " Renaming this without updating caddie-master.md would"
        " be silently fine (lockout still doesn't match) — but"
        " auditors looking for the marker would be confused (#609)."
    )

    # And queries.py's RUNTIME body string must not contain the
    # stop-loss breach literal. (The literal appears in docstrings
    # / comments inside queries.py for documentation, but the
    # actual `body = ( ... )` string passed to insert must not.)

    # Pull out the `body = (\n ... )\n` block inside
    # _log_reconcile_closes. The function name is unique in the file
    # so a simple find-from-anchor works regardless of preceding
    # docstring backticks. Use paren-depth tracking (not a naive
    # str.find for the next `)`) so a future edit that introduces
    # a `)` inside the body string can't terminate the slice early
    # and silently weaken the guard.
    func_idx = queries_py.find("def _log_reconcile_closes")
    assert func_idx != -1, (
        "_log_reconcile_closes function not found (drift-guard"
        " scaffolding broken)."
    )
    body_assign_idx = queries_py.find("body = (", func_idx)
    assert body_assign_idx != -1, (
        "_log_reconcile_closes must have a `body = (...)` string"
        " assignment (drift-guard scaffolding broken)."
    )
    # Start tracking AFTER the opening `(`.
    open_paren_idx = body_assign_idx + len("body = (") - 1
    depth = 0
    body_close_idx = -1
    for i, ch in enumerate(queries_py[open_paren_idx:], start=open_paren_idx):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                body_close_idx = i
                break
    assert body_close_idx != -1, (
        "Could not find matching `)` for the `body = (` in"
        " _log_reconcile_closes (drift-guard scaffolding broken)."
    )
    body_template = queries_py[body_assign_idx:body_close_idx]
    assert "Trigger: Stop-loss breach" not in body_template, (
        "queries.py's _log_reconcile_closes body template MUST NOT"
        " contain the literal `Trigger: Stop-loss breach` —"
        " Caddie Master's #586 lockout query is a substring match,"
        " so including the phrase anywhere in the body (even in"
        " explanatory rationale text) would silently lock out"
        " legitimate re-entry after reconcile drift (#609)."
    )


def _guard_trade(
    ticker: str, action: str, count: int, price: float,
    agent: str = "closer",
) -> TradeDecision:
    return TradeDecision(
        ticker=ticker, action=TradeDecision.Action(action),
        side="yes", count=count, price=price, agent=agent,
    )


class TestReconcileDuplicateGuard:
    """#653: the drift-close duplicate guard in _log_reconcile_closes —
    mutation testing showed it previously had zero kill coverage."""

    async def test_guard_suppresses_when_closes_cover_opens(self, db):
        """Settlement close exists but the mirror row survived (the
        race the guard exists for): sync must NOT write a duplicate
        reconcile drift close."""
        await insert_trade(db, _guard_trade("KX1", "open", 10, 0.6))
        await insert_trade(db, _guard_trade(
            "KX1", "close", 10, 1.0, agent="settlement",
        ))
        await upsert_position(db, _pos("KX1", count=10))

        await sync_positions(db, [])  # ticker removed

        trades = await get_trades(db, ticker="KX1")
        closes = [t for t in trades if t["action"] == "close"]
        assert len(closes) == 1
        assert closes[0]["agent"] == "settlement"

    async def test_guard_allows_drift_close_after_reopen(self, db):
        """close -> reopen -> drift: the reopened contracts are not
        covered by the old close, so the drift close MUST be written
        (a timestamp-based guard would wrongly suppress it)."""
        await insert_trade(db, _guard_trade("KX2", "open", 100, 0.6))
        await insert_trade(db, _guard_trade("KX2", "close", 100, 0.8))
        await insert_trade(db, _guard_trade("KX2", "open", 100, 0.55))
        await upsert_position(db, _pos("KX2", count=100))

        await sync_positions(db, [])

        trades = await get_trades(db, ticker="KX2")
        reconcile = [t for t in trades if t.get("agent") == "reconcile"]
        assert len(reconcile) == 1

    async def test_guard_allows_drift_close_after_partial_close(self, db):
        """open 10, close 4, drift removes the rest: the residual 6
        must still get a drift close."""
        await insert_trade(db, _guard_trade("KX3", "open", 10, 0.6))
        await insert_trade(db, _guard_trade("KX3", "close", 4, 0.7))
        await upsert_position(db, _pos("KX3", count=6))

        await sync_positions(db, [])

        trades = await get_trades(db, ticker="KX3")
        reconcile = [t for t in trades if t.get("agent") == "reconcile"]
        assert len(reconcile) == 1
        assert reconcile[0]["count"] == 6

    async def test_guard_allows_drift_close_with_no_trade_history(
        self, db,
    ):
        """A position with no recorded opens (pre-#609 DB, seeded)
        still gets its drift close."""
        await upsert_position(db, _pos("KX4", count=5))

        await sync_positions(db, [])

        trades = await get_trades(db, ticker="KX4")
        reconcile = [t for t in trades if t.get("agent") == "reconcile"]
        assert len(reconcile) == 1


class TestCorruptAnalyticsGuard:
    """#668: corrupt stored entry analytics (manual SQL / legacy rows)
    must not abort the settlement or reconcile transaction — the write
    degrades to honest zeros with a warning instead of re-failing
    every cycle."""

    async def test_settlement_close_survives_corrupt_analytics(
        self, db, caplog,
    ):
        import logging

        from gimmes.store.queries import log_settlement_close

        await insert_trade(db, _open_trade("KXCPI-26APR-T0.5"))
        # Out-of-range probability can only arrive via manual SQL —
        # insert_trade validates, so corrupt the row underneath it.
        await db.conn.execute(
            "UPDATE trades SET model_probability = 1.7"
            " WHERE ticker = 'KXCPI-26APR-T0.5'",
        )
        await db.conn.commit()

        with caplog.at_level(logging.WARNING, logger="gimmes.store.queries"):
            async with db.transaction():
                row_id = await log_settlement_close(
                    db, ticker="KXCPI-26APR-T0.5", side="yes",
                    count=10, won=True,
                )
        assert row_id > 0
        assert "failed validation" in caplog.text

        closes = [
            t for t in await get_trades(db) if t["action"] == "close"
        ]
        assert len(closes) == 1
        assert closes[0]["agent"] == "settlement"
        assert closes[0]["price"] == 1.0
        assert closes[0]["model_probability"] == 0.0
        assert closes[0]["gimme_score"] == 0.0

    async def test_drift_close_survives_corrupt_analytics(
        self, db, caplog,
    ):
        import logging

        await insert_trade(db, _open_trade("KXCPI-26APR-T0.5"))
        await db.conn.execute(
            "UPDATE trades SET gimme_score = 250.0"
            " WHERE ticker = 'KXCPI-26APR-T0.5'",
        )
        await db.conn.commit()
        await upsert_position(
            db, _pos("KXCPI-26APR-T0.5", count=10, price=0.42),
        )

        with caplog.at_level(logging.WARNING, logger="gimmes.store.queries"):
            await sync_positions(db, [])
        assert "failed validation" in caplog.text

        closes = [
            t for t in await get_trades(db) if t["action"] == "close"
        ]
        assert len(closes) == 1
        assert closes[0]["agent"] == "reconcile"
        assert closes[0]["gimme_score"] == 0.0
        assert closes[0]["model_probability"] == 0.0

    async def test_corrupt_caller_value_still_fails_loud(
        self, db, caplog,
    ):
        """A corrupt POSITIONS row (mark > 1.0) is not an analytics
        problem — the zeroed retry re-raises, and the guard must not
        log a 'recorded with zeroed analytics' line for a row that was
        rolled back."""
        import logging

        from pydantic import ValidationError

        await insert_trade(db, _open_trade("KXCPI-26APR-T0.5"))
        await upsert_position(
            db, _pos("KXCPI-26APR-T0.5", count=10, price=1.7),
        )

        with caplog.at_level(logging.WARNING, logger="gimmes.store.queries"):
            with pytest.raises(ValidationError):
                await sync_positions(db, [])
        assert "recorded with zeroed analytics" not in caplog.text


class TestDriftCountClamp:
    """#684: the drift close covers the LEDGER residual, not
    pos.count — a clamped settlement close (off-ledger exit) leaves a
    remainder that must not be over-closed."""

    async def test_drift_close_uses_residual_after_partial_settlement(
        self, db,
    ):
        from gimmes.store.queries import log_settlement_close

        await insert_trade(db, _open_trade("KXCPI-26APR-T0.5"))
        # A clamped settlement close covered 6 of the 10 opens.
        async with db.transaction():
            await log_settlement_close(
                db, ticker="KXCPI-26APR-T0.5", side="yes",
                count=6, won=True,
            )
        await upsert_position(
            db, _pos("KXCPI-26APR-T0.5", count=10, price=0.42),
        )

        await sync_positions(db, [])

        closes = [
            t for t in await get_trades(db) if t["action"] == "close"
        ]
        drift = [c for c in closes if c["agent"] == "reconcile"]
        assert len(drift) == 1
        # 10 opened − 6 settled = 4, NOT pos.count 10.
        assert drift[0]["count"] == 4

    async def test_no_history_position_keeps_pos_count(self, db):
        await upsert_position(db, _pos("NO-HISTORY", count=10, price=0.42))
        await sync_positions(db, [])
        [c] = await get_trades(db)
        assert c["action"] == "close"
        assert c["count"] == 10
