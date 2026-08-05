"""Same-ticker reopen churn guards (#661).

The KXGDP-26JUL30-T3.0 case: opened/closed/reopened/closed within 33
minutes, all at $0.71 — the reopen executed a candidate scanned before
the close, 21 seconds after Caddie Master's own cooldown note. The
reopen gate is a HARD CLI rejection (prompt cooldowns demonstrably
failed); the round-trip warning never blocks a close.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from gimmes.cli import app
from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision
from gimmes.risk.churn import (
    REOPEN_LOCKOUT_MINUTES,
    REOPEN_PRICE_DELTA,
    check_reopen_churn,
    check_roundtrip_churn,
)
from gimmes.store.database import Database
from gimmes.store.queries import (
    get_last_close_trade,
    insert_candidate,
    insert_trade,
)
from tests.unit import test_order_error_handling as h

NOW = datetime(2026, 6, 17, 19, 0, 21, tzinfo=UTC)


def _iso_ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def _error_codes(insert_error) -> list[str]:
    return [
        c.args[1].error_code for c in insert_error.await_args_list
        if len(c.args) > 1
    ]


def _close_trade(
    ticker: str, *, timestamp: datetime | None = None, agent: str = "closer",
    price: float = 0.71, rationale: str = "close",
) -> TradeDecision:
    trade = TradeDecision(
        ticker=ticker, action=TradeDecision.Action.CLOSE,
        side="no", count=10, price=price,
        rationale=rationale, agent=agent,
    )
    if timestamp is not None:
        trade.timestamp = timestamp
    return trade


def _run_db(db_path, work) -> None:
    """Run async ``work(db)`` against a real connected Database."""
    async def _s() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await work(db)
        finally:
            await db.close()

    asyncio.run(_s())


class TestCheckReopenChurn:
    @staticmethod
    def _check(
        close_price=0.71,
        close_timestamp="2026-06-17T18:53:36+00:00",
        close_agent="closer",
        entry_price=0.71,
        close_side=None,
        entry_side=None,
    ):
        return check_reopen_churn(
            close_price=close_price,
            close_timestamp=close_timestamp,
            close_agent=close_agent,
            entry_price=entry_price,
            close_side=close_side,
            entry_side=entry_side,
            now=NOW,
        )

    def test_canonical_kxgdp_case_rejected(self) -> None:
        # closed 7 minutes ago at the identical price
        msg = self._check()
        assert msg is not None
        assert "#661" in msg
        assert "--force-reopen" in msg

    def test_material_price_move_allowed(self) -> None:
        assert self._check(entry_price=0.61) is None
        assert self._check(entry_price=0.81) is None

    def test_old_close_allowed(self) -> None:
        assert self._check(
            close_timestamp="2026-06-17T17:53:36+00:00",  # 67m ago
        ) is None

    def test_reconcile_close_never_arms_the_gate(self) -> None:
        assert self._check(close_agent="reconcile") is None

    def test_unparseable_timestamp_fails_open(self) -> None:
        assert self._check(close_timestamp="not-a-time") is None

    def test_naive_sqlite_format_parsed_as_utc(self) -> None:
        # schema default datetime('now') writes naive space-separated
        msg = self._check(close_timestamp="2026-06-17 18:53:36")
        assert msg is not None

    def test_exact_boundaries(self) -> None:
        # exactly 60 minutes ago -> still gated (strict > on age)
        assert REOPEN_LOCKOUT_MINUTES == 60
        assert self._check(
            close_timestamp="2026-06-17T18:00:21+00:00",
        ) is not None
        # exactly at the price delta -> allowed (strict <)
        assert self._check(
            entry_price=0.71 + REOPEN_PRICE_DELTA,
        ) is None

    def test_future_timestamp_fails_open(self) -> None:
        assert self._check(
            close_timestamp="2026-06-17T19:30:00+00:00",
        ) is None


class TestSideAwareReopenGate:
    """#678: prices are side-effective — the gate flips the close
    price into the entry's denomination before the band check."""

    _check = staticmethod(TestCheckReopenChurn._check)

    def test_opposite_side_same_number_allowed(self) -> None:
        """Close NO $0.71 → buy YES $0.71 is a 42-cent real move
        (previously false-blocked)."""
        msg = self._check(
            close_price=0.71, entry_price=0.71,
            close_side="no", entry_side="yes",
        )
        assert msg is None

    def test_opposite_side_complement_price_rejected(self) -> None:
        """Close NO $0.71 → buy YES $0.29 is the SAME price point
        (previously missed churn)."""
        msg = self._check(
            close_price=0.71, entry_price=0.29,
            close_side="no", entry_side="yes",
        )
        assert msg is not None
        assert "#661" in msg
        assert "--force-reopen" in msg
        # The judged price and both denominations are auditable.
        assert "$0.29" in msg
        assert "$0.71" in msg

    def test_same_side_explicit_unchanged(self) -> None:
        msg = self._check(
            close_price=0.71, entry_price=0.71,
            close_side="no", entry_side="no",
        )
        assert msg is not None

    def test_flipped_exact_boundary_allowed(self) -> None:
        """Exactly at the delta in flipped terms → allowed, same as
        the side-blind boundary (the round(...,4) is load-bearing:
        without it 1−0.71 carries a float residue that flips the
        boundary verdict)."""
        msg = self._check(
            close_price=0.71, entry_price=0.34,
            close_side="no", entry_side="yes",
        )
        assert msg is None

    def test_missing_or_junk_side_falls_back_side_blind(self) -> None:
        """Fail toward the pre-#678 behavior — this is a fail-open
        guard, not a ledger."""
        for close_side in (None, "", "maybe"):
            msg = self._check(
                close_price=0.71, entry_price=0.71,
                close_side=close_side, entry_side="yes",
            )
            assert msg is not None, f"close_side={close_side!r}"


class TestCheckRoundtripChurn:
    def test_fast_roundtrip_warns(self) -> None:
        msg = check_roundtrip_churn(
            open_timestamp="2026-06-17T18:33:06+00:00", now=NOW,
        )
        assert msg is not None
        assert "#661" in msg

    def test_old_open_silent(self) -> None:
        assert check_roundtrip_churn(
            open_timestamp="2026-06-17T17:00:00+00:00", now=NOW,
        ) is None

    def test_unparseable_silent(self) -> None:
        assert check_roundtrip_churn(
            open_timestamp="garbage", now=NOW,
        ) is None


class TestOrderReopenGate:
    """#661 end-to-end: the buy path hard-rejects a same-price reopen
    of a freshly closed ticker; --force-reopen (and ONLY that flag)
    bypasses with an audit row."""

    @staticmethod
    def _run(extra_args=None, last_close=None, last_close_effect=None):
        broker = h._make_mock_broker()
        TestOrderReopenGate._last_broker = broker
        captured = {}

        async def _sync(db, positions, trade):
            captured["trade"] = trade

        with (
            patch(
                "gimmes.store.queries.get_thesis_for_ticker",
                AsyncMock(return_value=""),
            ),
            patch(
                "gimmes.store.queries.get_open_trade_for_ticker",
                AsyncMock(return_value=None),
            ),
        ):
            result, console, insert_error = h._run_order_cli(
                broker, sync_side_effect=_sync,
                extra_args=extra_args or [],
                last_close=last_close,
                last_close_effect=last_close_effect,
            )
        return result, h._printed(console), captured, insert_error

    @staticmethod
    def _fresh_close(price=0.40, agent="closer"):
        return {
            "price": price, "timestamp": _iso_ago(minutes=7),
            "agent": agent,
        }

    def test_opposite_side_complement_rejected_end_to_end(self) -> None:
        """#678: harness buys YES at eff 0.40; a fresh NO close at
        0.60 is the same price point in YES terms — hard reject."""
        result, out, captured, _ = self._run(
            last_close={
                "price": 0.60, "timestamp": _iso_ago(minutes=7),
                "agent": "closer", "side": "no",
            },
        )
        assert result.exit_code == 1, out
        assert not captured  # no order reached the broker

    def test_opposite_side_same_number_allowed_end_to_end(self) -> None:
        """A fresh NO close at 0.40 is YES 0.60 — a 20-cent move from
        the 0.40 entry; the order proceeds."""
        result, out, captured, _ = self._run(
            last_close={
                "price": 0.40, "timestamp": _iso_ago(minutes=7),
                "agent": "closer", "side": "no",
            },
        )
        assert result.exit_code == 0, out
        assert len(captured) == 1

    def test_same_price_fresh_close_rejected(self) -> None:
        # order harness buys at eff price 0.40; close 7m ago at 0.40
        result, out, captured, _ = self._run(
            last_close=self._fresh_close(price=0.40),
        )
        assert result.exit_code == 1, out
        assert "Reopen churn gate (#661)" in out
        assert "trade" not in captured  # no ledger row
        # The load-bearing assert: no BROKER call ever happened — a
        # gate that fires after placement would still pass the two
        # asserts above (surviving mutant from review).
        assert self._last_broker.create_order.await_count == 0

    def test_price_moved_allowed(self) -> None:
        result, out, captured, _ = self._run(
            last_close=self._fresh_close(price=0.60),
        )
        assert result.exit_code == 0, out
        assert "trade" in captured

    def test_old_close_allowed(self) -> None:
        old = {
            "price": 0.40, "timestamp": _iso_ago(hours=3),
            "agent": "closer",
        }
        result, out, captured, _ = self._run(last_close=old)
        assert result.exit_code == 0, out
        assert "trade" in captured

    def test_reconcile_close_allowed(self) -> None:
        result, out, captured, _ = self._run(
            last_close=self._fresh_close(agent="reconcile"),
        )
        assert result.exit_code == 0, out
        assert "trade" in captured

    def test_override_context_carries_close_side(self) -> None:
        """#678: the override audit row records the close row's side
        so mixed-denomination overrides are auditable."""
        result, out, captured, insert_error = self._run(
            extra_args=["--force-reopen"],
            last_close={
                "price": 0.40, "timestamp": _iso_ago(minutes=7),
                "agent": "closer", "side": "yes",
            },
        )
        assert result.exit_code == 0, out
        contexts = [
            json.loads(c.args[1].context)
            for c in insert_error.await_args_list
            if len(c.args) > 1
            and c.args[1].error_code == "reopen_gate_overridden"
        ]
        assert len(contexts) == 1
        assert contexts[0]["close_side"] == "yes"

    def test_force_reopen_bypasses_with_audit_row(self) -> None:
        result, out, captured, insert_error = self._run(
            extra_args=["--force-reopen"],
            last_close=self._fresh_close(price=0.40),
        )
        assert result.exit_code == 0, out
        assert "trade" in captured
        assert "--force-reopen override" in out
        assert "reopen_gate_overridden" in _error_codes(insert_error)

    def test_plain_force_does_not_bypass(self) -> None:
        result, out, captured, _ = self._run(
            extra_args=["--force"],
            last_close=self._fresh_close(price=0.40),
        )
        assert result.exit_code == 1, out
        assert "trade" not in captured
        assert self._last_broker.create_order.await_count == 0

    def test_lookup_failure_fails_open(self) -> None:
        result, out, captured, _ = self._run(
            last_close_effect=sqlite3.OperationalError("locked"),
        )
        assert result.exit_code == 0, out
        assert "trade" in captured


class TestSellRoundtripWarning:
    """#661: closing is never blocked; a sub-hour round trip warns and
    records an error row."""

    @staticmethod
    def _run_sell(open_row):
        pos = Position(
            ticker="TEST-TICKER", side="yes", count=100,
            avg_price=0.40, market_price=0.40, cost_basis=40.0,
        )
        broker = h._make_mock_broker(
            get_positions_side_effect=lambda: [pos],
        )
        captured = {}

        async def _sync(db, positions, trade):
            captured["trade"] = trade

        with (
            patch(
                "gimmes.store.queries.get_entry_analytics",
                AsyncMock(return_value=None),
            ),
            patch(
                "gimmes.store.queries.get_last_close_trade",
                AsyncMock(return_value=None),
            ),
        ):
            # last_entry rides the harness param — an outer
            # get_last_entry_trade patch would be overridden by the
            # harness's own inner patch (#762 harness change).
            result, console, insert_error = h._run_order_cli(
                broker, sync_side_effect=_sync,
                last_entry=open_row,
                cli_args=[
                    "order", "TEST-TICKER", "--action", "sell",
                    "--side", "yes", "--count", "10",
                    "--price", "40", "--yes",
                ],
            )
        return result, h._printed(console), captured, insert_error

    def test_fast_roundtrip_warns_but_closes(self) -> None:
        open_row = {"timestamp": _iso_ago(minutes=20), "price": 0.38}
        result, out, captured, insert_error = self._run_sell(open_row)
        assert result.exit_code == 0, out
        assert "trade" in captured  # the close went through
        assert "Round-trip churn (#661)" in out
        assert "churn_roundtrip" in _error_codes(insert_error)
        # #678: the anchor entry is the documented grouping key Pro
        # uses to link multi-leg round trips — pin the contract.
        contexts = [
            json.loads(c.args[1].context)
            for c in insert_error.await_args_list
            if len(c.args) > 1
            and c.args[1].error_code == "churn_roundtrip"
        ]
        assert len(contexts) == 1
        assert contexts[0]["entry_timestamp"] == open_row["timestamp"]
        assert contexts[0]["entry_price"] == 0.38

    def test_old_open_no_warning(self) -> None:
        open_row = {"timestamp": _iso_ago(hours=5)}
        result, out, captured, _ = self._run_sell(open_row)
        assert result.exit_code == 0, out
        assert "trade" in captured
        assert "Round-trip churn" not in out


class TestCandidatesStaleCloseFlag:
    """#661: research scanned before the ticker's most recent close is
    stale by construction — flagged in Status and a banner below the
    table (the surface Caddie Master and Closer key on)."""

    TICKER = "KXGDP-26JUL30-T3.0"

    def _seed(self, db_path, *, close_after_scan: bool, agent="closer"):
        async def _work(db: Database) -> None:
            await insert_candidate(
                db, self.TICKER, "GDP", 0.27, 0.85, 0.12, 72.0,
                "memo", recommendation="proceed",
            )
            # candidates.scanned_at is datetime('now') — place the
            # close clearly after or before it
            year = 2027 if close_after_scan else 2020
            await insert_trade(db, _close_trade(
                self.TICKER, agent=agent,
                timestamp=datetime(year, 1, 1, tzinfo=UTC),
            ))

        _run_db(db_path, _work)

    def _invoke(self, tmp_path):
        with patch("gimmes.config.GIMMES_HOME", tmp_path):
            return CliRunner().invoke(app, [
                "candidates", "--ticker", self.TICKER, "--limit", "5",
            ])

    def test_pre_close_research_flagged(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        self._seed(db_path, close_after_scan=True)
        result = self._invoke(tmp_path)
        assert result.exit_code == 0, result.output
        assert f"{self.TICKER} STALE-CLOSE:" in result.output

    def test_post_close_research_clean(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        self._seed(db_path, close_after_scan=False)
        result = self._invoke(tmp_path)
        assert result.exit_code == 0, result.output
        assert "STALE-CLOSE" not in result.output

    def test_fresh_research_above_stale_rows_no_banner(
        self, tmp_path,
    ) -> None:
        """The recovery path (#661 review): fresh post-close research
        must clear the banner even while older stale rows remain in
        the listing — otherwise the fix deadlocks its own documented
        escape (fresh research would be rejected forever)."""
        db_path = tmp_path / "gimmes.db"

        async def _work(db: Database) -> None:
            # stale research (scanned now)
            await insert_candidate(
                db, self.TICKER, "GDP", 0.27, 0.85, 0.12, 72.0,
                "old memo",
            )
            # close AFTER it (2027)
            await insert_trade(db, _close_trade(
                self.TICKER,
                timestamp=datetime(2027, 1, 1, tzinfo=UTC),
            ))
            # fresh research AFTER the close
            await db.conn.execute(
                "INSERT INTO candidates (ticker, title,"
                " market_price, model_probability, edge,"
                " gimme_score, research_memo, scanned_at)"
                " VALUES (?, ?, 0.30, 0.80, 0.10, 70,"
                " 'fresh memo', '2027-06-01 12:00:00')",
                (self.TICKER, "GDP"),
            )
            await db.conn.commit()

        _run_db(db_path, _work)
        result = self._invoke(tmp_path)
        assert result.exit_code == 0, result.output
        # cell flag on the stale row survives; the banner does not
        assert "STALE-CLOSE:" not in result.output
        assert "STALE" in result.output

    def test_reconcile_close_does_not_flag(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"
        self._seed(db_path, close_after_scan=True, agent="reconcile")
        result = self._invoke(tmp_path)
        assert result.exit_code == 0, result.output
        assert "STALE-CLOSE" not in result.output


class TestLastCloseQueries:
    """Real-DB pins for the #661 query helpers."""

    def test_exact_match_ignores_sibling_thresholds(
        self, tmp_path,
    ) -> None:
        """A KXGDP-26JUL30-T3.05 close must not arm the gate for
        T3.0 — prefix matching would (#661 review)."""
        async def _work(db: Database) -> None:
            await insert_trade(db, _close_trade(
                "KXGDP-26JUL30-T3.05", rationale="sibling",
            ))
            assert await get_last_close_trade(
                db, "KXGDP-26JUL30-T3.0",
            ) is None
            row = await get_last_close_trade(db, "KXGDP-26JUL30-T3.05")
            assert row is not None and row["price"] == 0.71

        _run_db(tmp_path / "test.db", _work)

    def test_reconcile_shadow_does_not_disarm(self, tmp_path) -> None:
        """A trailing reconcile drift close must not shadow the fresh
        decision close underneath it (#661 review)."""
        async def _work(db: Database) -> None:
            await insert_trade(db, _close_trade(
                "T", rationale="decision close",
                timestamp=datetime(2026, 6, 17, 18, 53, tzinfo=UTC),
            ))
            await insert_trade(db, _close_trade(
                "T", agent="reconcile", price=0.70, rationale="drift",
                timestamp=datetime(2026, 6, 17, 18, 58, tzinfo=UTC),
            ))

            row = await get_last_close_trade(db, "T")
            assert row is not None
            assert row["agent"] == "closer"  # decision, not drift

        _run_db(tmp_path / "test.db", _work)


def test_get_last_close_trade_returns_side(tmp_path) -> None:
    """#678: the gate needs the close row's side to normalize
    denominations."""
    from gimmes.store.queries import get_last_close_trade, insert_trade

    db_path = tmp_path / "test.db"

    async def _seed_and_check(db) -> None:
        await insert_trade(db, _close_trade("KXGDP-26JUL30-T3.0"))
        row = await get_last_close_trade(db, "KXGDP-26JUL30-T3.0")
        assert row is not None
        assert row["side"] == "no"

    _run_db(db_path, _seed_and_check)


def test_get_last_entry_trade_side_scoped(tmp_path) -> None:
    """#678: under both-side holdings the round-trip anchor must be
    the leg being closed, not the ticker's most recent entry."""
    from gimmes.store.queries import get_last_entry_trade, insert_trade

    db_path = tmp_path / "test.db"

    async def _work(db) -> None:
        no_entry = TradeDecision(
            ticker="KXBOTH-26JUL-T1", action=TradeDecision.Action.OPEN,
            side="no", count=10, price=0.60,
        )
        no_entry.timestamp = datetime.now(UTC) - timedelta(hours=2)
        await insert_trade(db, no_entry)
        yes_entry = TradeDecision(
            ticker="KXBOTH-26JUL-T1", action=TradeDecision.Action.OPEN,
            side="yes", count=10, price=0.40,
        )
        await insert_trade(db, yes_entry)

        row = await get_last_entry_trade(db, "KXBOTH-26JUL-T1", side="no")
        assert row is not None
        assert row["price"] == 0.60  # the NO leg, not the newer YES

        unscoped = await get_last_entry_trade(db, "KXBOTH-26JUL-T1")
        assert unscoped is not None
        assert unscoped["price"] == 0.40  # legacy behavior preserved

    _run_db(db_path, _work)


def test_order_requires_explicit_side_under_both_config() -> None:
    """#678: under strategy.side='both' an order without --side must
    fail loud and early — the reopen gate would otherwise run
    silently side-blind (the exact gap the denomination flip closes)
    and OrderSide('both') would crash after the gate anyway."""
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    from gimmes.cli import app

    cfg = MagicMock()
    cfg.strategy.side = "both"
    with patch("gimmes.cli.load_config", return_value=cfg):
        result = CliRunner().invoke(app, [
            "order", "KXTEST", "--action", "buy", "--count", "10",
            "--price", "40", "--yes",
        ])
    assert result.exit_code == 1
    assert "explicit side" in result.output
