"""#768: in-cycle order gates.

Cycle 2212: the Closer's order was classifier-denied; 73 seconds later
the Caddie Master placed the order itself (733 NO @ $0.54, settled YES,
-$395.82). Two CLI gates close that path, both active only when
GIMMES_CYCLE is set so manual operator orders are untouched:

- identity gate: in-cycle `gimmes order` requires --agent closer;
- terminal gate: a recorded failed BUY attempt (activity_log marker,
  armed by the Closer's order_failed/order_canceled skip or by the
  order command's own failure exits) blocks same-ticker same-cycle
  retries for every agent.

Harness reuse follows test_churn_guard.py; gate asserts follow
TestOrderReopenGate (exit code, message, no broker call, audit row).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
from typer.testing import CliRunner

from gimmes import cli as cli_module
from gimmes.cli import app
from gimmes.store.database import Database
from gimmes.store.queries import (
    has_terminal_order_attempt,
    insert_activity,
    order_terminal_marker,
)
from tests.unit import test_order_error_handling as h


def _error_codes(insert_error) -> list[str]:
    return [
        c.args[1].error_code
        for c in insert_error.await_args_list
        if len(c.args) > 1
    ]


def _marker_calls(insert_activity_mock) -> list[dict]:
    return [
        c.kwargs
        for c in insert_activity_mock.await_args_list
        if c.kwargs.get("message", "").startswith("Order attempt terminal:")
    ]


class TestCloserOnlyGate:
    """In-cycle placement is restricted to --agent closer."""

    def _run(self, monkeypatch, *, cycle="42", extra_args=None,
             cli_args=None):
        if cycle is None:
            monkeypatch.delenv("GIMMES_CYCLE", raising=False)
        else:
            monkeypatch.setenv("GIMMES_CYCLE", cycle)
        broker = h._make_mock_broker()
        self._last_broker = broker
        result, console, insert_error = h._run_order_cli(
            broker, extra_args=extra_args, cli_args=cli_args,
        )
        return result, h._printed(console), insert_error

    def test_default_cli_agent_blocked_in_cycle(self, monkeypatch) -> None:
        result, out, insert_error = self._run(monkeypatch)
        assert result.exit_code == 1, out
        assert "Order agent gate (#768)" in out
        assert self._last_broker.create_order.await_count == 0
        assert "order_agent_denied" in _error_codes(insert_error)

    def test_caddie_master_blocked_in_cycle(self, monkeypatch) -> None:
        """The c2212 command shape: an in-cycle CM-attributed buy."""
        result, out, insert_error = self._run(
            monkeypatch, extra_args=["--agent", "caddie-master"],
        )
        assert result.exit_code == 1, out
        assert "Order agent gate (#768)" in out
        assert self._last_broker.create_order.await_count == 0

    def test_sell_also_blocked_in_cycle(self, monkeypatch) -> None:
        result, out, _ = self._run(
            monkeypatch,
            cli_args=[
                "order", "TEST-TICKER", "--action", "sell",
                "--side", "yes", "--count", "10", "--yes",
                "--agent", "caddie-master",
            ],
        )
        assert result.exit_code == 1, out
        assert "Order agent gate (#768)" in out
        assert self._last_broker.create_order.await_count == 0

    def test_closer_allowed_in_cycle(self, monkeypatch) -> None:
        result, out, _ = self._run(
            monkeypatch, extra_args=["--agent", "closer"],
        )
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1

    def test_inert_without_cycle_env(self, monkeypatch) -> None:
        result, out, _ = self._run(monkeypatch, cycle=None)
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1

    def test_inert_with_malformed_cycle_env(self, monkeypatch) -> None:
        result, out, _ = self._run(monkeypatch, cycle="not-a-number")
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1

    def test_audit_row_context_carries_cycle(self, monkeypatch) -> None:
        _, _, insert_error = self._run(monkeypatch, cycle="77")
        contexts = [
            json.loads(c.args[1].context)
            for c in insert_error.await_args_list
            if len(c.args) > 1
            and c.args[1].error_code == "order_agent_denied"
        ]
        assert len(contexts) == 1
        assert contexts[0]["cycle"] == 77


class TestOrderTerminalGate:
    """A recorded failed attempt is terminal for ticker+cycle — for
    every agent, the Closer included."""

    def _run(self, monkeypatch, *, terminal=True, lookup_effect=None,
             cycle="42", extra_args=None, cli_args=None):
        if cycle is None:
            monkeypatch.delenv("GIMMES_CYCLE", raising=False)
        else:
            monkeypatch.setenv("GIMMES_CYCLE", cycle)
        self._lookup = AsyncMock(
            return_value=terminal, side_effect=lookup_effect,
        )
        broker = h._make_mock_broker()
        self._last_broker = broker
        args = ["--agent", "closer"] + (extra_args or [])
        result, console, insert_error = h._run_order_cli(
            broker,
            extra_args=None if cli_args else args,
            cli_args=cli_args,
            terminal_attempt_mock=self._lookup,
        )
        return result, h._printed(console), insert_error

    def test_marker_blocks_in_cycle_buy(self, monkeypatch) -> None:
        result, out, insert_error = self._run(monkeypatch)
        assert result.exit_code == 1, out
        assert "Order terminal gate (#768)" in out
        assert self._last_broker.create_order.await_count == 0
        assert "order_terminal_retry_blocked" in _error_codes(insert_error)
        # The lookup must be keyed on THIS ticker and THIS cycle — a
        # hardcoded cycle or wrong ticker would survive pure
        # return-value mocking (review-found).
        assert self._lookup.await_args.args[1:] == ("TEST-TICKER", 42)

    def test_identity_gate_fires_before_terminal_gate(
        self, monkeypatch,
    ) -> None:
        """Both gates armed: the agent gate wins and the terminal
        lookup is never consulted."""
        result, out, _ = self._run(
            monkeypatch,
            cli_args=[
                "order", "TEST-TICKER",
                "--side", "yes", "--count", "10",
                "--price", "40", "--prob", "0.55", "--yes",
                "--agent", "caddie-master",
            ],
        )
        assert result.exit_code == 1, out
        assert "Order agent gate (#768)" in out
        assert "Order terminal gate (#768)" not in out
        assert self._lookup.await_count == 0

    def test_terminal_gate_fires_before_reopen_churn_gate(
        self, monkeypatch,
    ) -> None:
        """Terminal marker + fresh same-price close both armed: the
        terminal gate wins (it sits before any market fetch)."""
        monkeypatch.setenv("GIMMES_CYCLE", "42")
        self._lookup = AsyncMock(return_value=True)
        broker = h._make_mock_broker()
        result, console, _ = h._run_order_cli(
            broker, extra_args=["--agent", "closer"],
            terminal_attempt_mock=self._lookup,
            last_close={
                "price": 0.40, "timestamp": "2026-01-01T00:00:00+00:00",
                "agent": "closer",
            },
        )
        out = h._printed(console)
        assert result.exit_code == 1, out
        assert "Order terminal gate (#768)" in out
        assert "Reopen churn gate (#661)" not in out

    def test_force_does_not_bypass(self, monkeypatch) -> None:
        result, out, _ = self._run(monkeypatch, extra_args=["--force"])
        assert result.exit_code == 1, out
        assert self._last_broker.create_order.await_count == 0

    def test_force_reopen_does_not_bypass(self, monkeypatch) -> None:
        result, out, _ = self._run(
            monkeypatch, extra_args=["--force-reopen"],
        )
        assert result.exit_code == 1, out
        assert self._last_broker.create_order.await_count == 0

    def test_sell_not_gated(self, monkeypatch) -> None:
        """Terminal markers are entry-side only — a failed CLOSE must
        remain retryable (the close_failed backstop depends on it)."""
        result, out, _ = self._run(
            monkeypatch,
            cli_args=[
                "order", "TEST-TICKER", "--action", "sell",
                "--side", "yes", "--count", "10", "--yes",
                "--agent", "closer",
            ],
        )
        assert "Order terminal gate (#768)" not in out
        assert self._lookup.await_count == 0

    def test_inert_without_cycle_env(self, monkeypatch) -> None:
        result, out, _ = self._run(monkeypatch, cycle=None)
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1
        assert self._lookup.await_count == 0

    def test_lookup_failure_fails_open(self, monkeypatch) -> None:
        result, out, insert_error = self._run(
            monkeypatch,
            lookup_effect=sqlite3.OperationalError("locked"),
        )
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1
        assert "order_terminal_lookup_failed" in _error_codes(insert_error)


class TestTerminalMarkerWrites:
    """The order command's own BUY failure exits arm the gate."""

    def _run(self, monkeypatch, *, broker, cycle="42"):
        if cycle is None:
            monkeypatch.delenv("GIMMES_CYCLE", raising=False)
        else:
            monkeypatch.setenv("GIMMES_CYCLE", cycle)
        activity = AsyncMock(return_value=1)
        result, console, _ = h._run_order_cli(
            broker, extra_args=["--agent", "closer"],
            insert_activity_mock=activity,
        )
        return result, h._printed(console), activity

    def test_http_error_writes_marker(self, monkeypatch) -> None:
        broker = h._make_mock_broker(
            create_order_side_effect=httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "https://x"),
                response=h._make_response(500, text="server error"),
            ),
        )
        result, out, activity = self._run(monkeypatch, broker=broker)
        assert result.exit_code == 1, out
        markers = _marker_calls(activity)
        assert len(markers) == 1
        assert markers[0]["message"] == order_terminal_marker("TEST-TICKER")
        assert markers[0]["cycle"] == 42
        assert markers[0]["phase"] == "guard"

    def test_timeout_writes_marker(self, monkeypatch) -> None:
        broker = h._make_mock_broker(
            create_order_side_effect=httpx.TimeoutException("slow"),
        )
        result, out, activity = self._run(monkeypatch, broker=broker)
        assert result.exit_code == 1, out
        markers = _marker_calls(activity)
        assert len(markers) == 1
        assert markers[0]["message"] == order_terminal_marker("TEST-TICKER")
        assert json.loads(markers[0]["details"])["error_code"] == "timeout"

    def test_runtime_error_writes_marker(self, monkeypatch) -> None:
        broker = h._make_mock_broker(
            create_order_side_effect=RuntimeError("bad state"),
        )
        result, out, activity = self._run(monkeypatch, broker=broker)
        assert result.exit_code == 1, out
        markers = _marker_calls(activity)
        assert len(markers) == 1
        assert markers[0]["message"] == order_terminal_marker("TEST-TICKER")
        assert json.loads(markers[0]["details"])["error_code"] == (
            "runtime_error"
        )

    def test_canceled_order_writes_marker(self, monkeypatch) -> None:
        canceled = h._ok_order(status="canceled")
        canceled.remaining_count = 10
        broker = h._make_mock_broker()
        broker.create_order = AsyncMock(return_value=canceled)
        result, out, activity = self._run(monkeypatch, broker=broker)
        assert result.exit_code == 1, out
        markers = _marker_calls(activity)
        assert len(markers) == 1
        assert json.loads(markers[0]["details"])["error_code"] == (
            "order_canceled"
        )

    def test_no_marker_out_of_cycle(self, monkeypatch) -> None:
        broker = h._make_mock_broker(
            create_order_side_effect=RuntimeError("bad state"),
        )
        result, out, activity = self._run(
            monkeypatch, broker=broker, cycle=None,
        )
        assert result.exit_code == 1, out
        assert _marker_calls(activity) == []

    def test_marker_write_failure_keeps_original_exit(
        self, monkeypatch,
    ) -> None:
        broker = h._make_mock_broker(
            create_order_side_effect=RuntimeError("bad state"),
        )
        monkeypatch.setenv("GIMMES_CYCLE", "42")
        activity = AsyncMock(side_effect=sqlite3.OperationalError("locked"))
        result, console, _ = h._run_order_cli(
            broker, extra_args=["--agent", "closer"],
            insert_activity_mock=activity,
        )
        out = h._printed(console)
        assert result.exit_code == 1, out
        assert "Order FAILED" in out


runner = CliRunner()


def _db_run(db_path: Path, fn):
    async def _go():
        db = Database(db_path)
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.close()

    return asyncio.run(_go())


def _patch_config(monkeypatch, db_path: Path) -> None:
    cfg = MagicMock()
    cfg.db_path = db_path
    cfg.strategy.side = "no"
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)


class TestLogTradeTerminalMarker:
    """The Closer's order_failed skip is the ONLY trace a classifier
    denial leaves — log-trade machine-writes the marker from it."""

    def _log_skip(self, monkeypatch, tmp_path, *, reason, cycle="42"):
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, self._noop)
        _patch_config(monkeypatch, db_path)
        if cycle is None:
            monkeypatch.delenv("GIMMES_CYCLE", raising=False)
        else:
            monkeypatch.setenv("GIMMES_CYCLE", cycle)
        args = [
            "log-trade", "KXTEST-26AUG-T1", "--action", "skip",
            "--agent", "closer",
        ]
        if reason:
            args += ["--reason", reason]
        result = runner.invoke(app, args)
        return result, db_path

    @staticmethod
    async def _noop(db):
        pass

    def _markers(self, db_path: Path) -> list[dict]:
        async def _q(db):
            cursor = await db.conn.execute(
                "SELECT cycle, agent, phase, message FROM activity_log"
                " WHERE message LIKE 'Order attempt terminal:%'"
            )
            return [dict(r) for r in await cursor.fetchall()]

        return _db_run(db_path, _q)

    def test_order_failed_skip_writes_marker(
        self, monkeypatch, tmp_path,
    ) -> None:
        result, db_path = self._log_skip(
            monkeypatch, tmp_path, reason="order_failed",
        )
        assert result.exit_code == 0, result.output
        markers = self._markers(db_path)
        assert len(markers) == 1
        assert markers[0]["message"] == order_terminal_marker(
            "KXTEST-26AUG-T1"
        )
        assert markers[0]["cycle"] == 42
        assert markers[0]["agent"] == "closer"
        assert markers[0]["phase"] == "guard"

    def test_order_canceled_skip_writes_marker(
        self, monkeypatch, tmp_path,
    ) -> None:
        result, db_path = self._log_skip(
            monkeypatch, tmp_path, reason="order_canceled",
        )
        assert result.exit_code == 0, result.output
        assert len(self._markers(db_path)) == 1

    def test_other_reason_writes_no_marker(
        self, monkeypatch, tmp_path,
    ) -> None:
        result, db_path = self._log_skip(
            monkeypatch, tmp_path, reason="cooldown",
        )
        assert result.exit_code == 0, result.output
        assert self._markers(db_path) == []

    def test_no_cycle_env_writes_no_marker(
        self, monkeypatch, tmp_path,
    ) -> None:
        result, db_path = self._log_skip(
            monkeypatch, tmp_path, reason="order_failed", cycle=None,
        )
        assert result.exit_code == 0, result.output
        assert self._markers(db_path) == []

    def test_marker_failure_still_logs_skip(
        self, monkeypatch, tmp_path,
    ) -> None:
        """log-trade is the logger of last resort — the skip row must
        land and the command exit 0 even if the marker write fails."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, self._noop)
        _patch_config(monkeypatch, db_path)
        monkeypatch.setenv("GIMMES_CYCLE", "42")
        monkeypatch.setattr(
            "gimmes.store.queries.insert_activity",
            AsyncMock(side_effect=sqlite3.OperationalError("locked")),
        )
        result = runner.invoke(app, [
            "log-trade", "KXTEST-26AUG-T1", "--action", "skip",
            "--reason", "order_failed", "--agent", "closer",
        ])
        assert result.exit_code == 0, result.output

        async def _rows(db):
            cursor = await db.conn.execute(
                "SELECT ticker FROM trades WHERE action = 'skip'"
            )
            return [dict(r) for r in await cursor.fetchall()]

        assert len(_db_run(db_path, _rows)) == 1


class TestClassifierBlockSkip:
    """#636: a classifier_block skip is the only trace a permission
    denial leaves — log-trade machine-writes BOTH the error row
    (Groundskeeper's feed) and the #768 terminal marker."""

    @staticmethod
    async def _noop(db):
        pass

    def _log_skip(self, monkeypatch, tmp_path, *, cycle="42"):
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, self._noop)
        _patch_config(monkeypatch, db_path)
        if cycle is None:
            monkeypatch.delenv("GIMMES_CYCLE", raising=False)
        else:
            monkeypatch.setenv("GIMMES_CYCLE", cycle)
        result = runner.invoke(app, [
            "log-trade", "KXTEST-26AUG-T1", "--action", "skip",
            "--reason", "classifier_block", "--agent", "closer",
        ])
        return result, db_path

    def _errors(self, db_path: Path) -> list[dict]:
        async def _q(db):
            cursor = await db.conn.execute(
                "SELECT severity, category, error_code, cycle, context"
                " FROM error_log WHERE error_code ="
                " 'safety_classifier_block'"
            )
            return [dict(r) for r in await cursor.fetchall()]

        return _db_run(db_path, _q)

    def _markers(self, db_path: Path) -> list[dict]:
        async def _q(db):
            cursor = await db.conn.execute(
                "SELECT cycle, message FROM activity_log"
                " WHERE message LIKE 'Order attempt terminal:%'"
            )
            return [dict(r) for r in await cursor.fetchall()]

        return _db_run(db_path, _q)

    def test_writes_error_row_and_marker(
        self, monkeypatch, tmp_path,
    ) -> None:
        result, db_path = self._log_skip(monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output
        errors = self._errors(db_path)
        assert len(errors) == 1
        assert errors[0]["severity"] == "warning"
        assert errors[0]["category"] == "agent_failure"
        assert errors[0]["cycle"] == 42
        assert "KXTEST-26AUG-T1" in errors[0]["context"]
        markers = self._markers(db_path)
        assert len(markers) == 1
        assert markers[0]["cycle"] == 42

    def test_error_row_written_even_without_cycle(
        self, monkeypatch, tmp_path,
    ) -> None:
        """The error trail must survive manual/off-cycle use; only
        the marker (a cycle-scoped gate) requires GIMMES_CYCLE."""
        result, db_path = self._log_skip(
            monkeypatch, tmp_path, cycle=None,
        )
        assert result.exit_code == 0, result.output
        assert len(self._errors(db_path)) == 1
        assert self._markers(db_path) == []

    def test_error_write_failure_still_logs_skip(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Logger of last resort: the skip row lands and the command
        exits 0 even if the error write fails."""
        db_path = tmp_path / "gimmes.db"
        _db_run(db_path, self._noop)
        _patch_config(monkeypatch, db_path)
        monkeypatch.setenv("GIMMES_CYCLE", "42")
        monkeypatch.setattr(
            cli_module, "_log_cli_error",
            AsyncMock(side_effect=sqlite3.OperationalError("locked")),
        )
        result = runner.invoke(app, [
            "log-trade", "KXTEST-26AUG-T1", "--action", "skip",
            "--reason", "classifier_block", "--agent", "closer",
        ])
        assert result.exit_code == 0, result.output

        async def _rows(db):
            cursor = await db.conn.execute(
                "SELECT ticker FROM trades WHERE action = 'skip'"
            )
            return [dict(r) for r in await cursor.fetchall()]

        assert len(_db_run(db_path, _rows)) == 1

    def test_non_entry_semantics(self) -> None:
        """classifier_block joins the non-entry set — a proceed that
        died to tooling must not enter the missed-entry FNR (#636)."""
        from gimmes.strategy.advisor import NON_ENTRY_SKIP_REASONS

        assert "classifier_block" in NON_ENTRY_SKIP_REASONS


class TestHasTerminalOrderAttempt:
    """Round-trip on a real DB, including the 24h restart bound."""

    def test_round_trip(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"

        async def _go(db):
            await insert_activity(
                db, cycle=42, agent="closer", phase="guard",
                message=order_terminal_marker("KXTEST-26AUG-T1"),
            )
            hit = await has_terminal_order_attempt(
                db, "KXTEST-26AUG-T1", 42,
            )
            other_cycle = await has_terminal_order_attempt(
                db, "KXTEST-26AUG-T1", 43,
            )
            other_ticker = await has_terminal_order_attempt(
                db, "KXOTHER-26AUG-T1", 42,
            )
            return hit, other_cycle, other_ticker

        hit, other_cycle, other_ticker = _db_run(db_path, _go)
        assert hit is True
        assert other_cycle is False
        assert other_ticker is False

    def test_stale_marker_ignored(self, tmp_path) -> None:
        """A same-numbered cycle from an earlier loop run (fresh-DB
        restart) never matches — markers older than 24h are dead."""
        db_path = tmp_path / "gimmes.db"

        async def _go(db):
            await insert_activity(
                db, cycle=42, agent="closer", phase="guard",
                message=order_terminal_marker("KXTEST-26AUG-T1"),
            )
            await db.conn.execute(
                "UPDATE activity_log SET timestamp ="
                " datetime('now', '-2 days')"
            )
            await db.conn.commit()
            return await has_terminal_order_attempt(
                db, "KXTEST-26AUG-T1", 42,
            )

        assert _db_run(db_path, _go) is False


class TestC2212RoundTrip:
    """The incident shape, end-to-end on a real DB: the Closer's
    order_failed skip (the only trace a classifier denial leaves) must
    block a second same-cycle order attempt through the real
    marker-write -> marker-read wiring, with no #768 mocks."""

    def test_skip_then_order_blocked(self, monkeypatch, tmp_path) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import patch

        db_path = tmp_path / "gimmes.db"
        monkeypatch.setenv("GIMMES_CYCLE", "2212")

        cfg = MagicMock()
        cfg.is_championship = False
        cfg.db_path = db_path
        cfg.strategy.side = "no"

        monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

        # Step 1: the Closer logs the denial as an order_failed skip.
        result = runner.invoke(app, [
            "log-trade", "KXBTCD-26AUG1118-T63599.99",
            "--action", "skip", "--reason", "order_failed",
            "--agent", "closer",
        ])
        assert result.exit_code == 0, result.output

        # Step 2: a second placement attempt in the same cycle — even
        # a correctly-attributed Closer one — is refused by the real
        # activity_log marker.
        broker = h._make_mock_broker()
        mock_client = AsyncMock()

        @asynccontextmanager
        async def _real_db_ctx(config):
            db = Database(db_path)
            await db.connect()
            try:
                yield mock_client, broker, db
            finally:
                await db.close()

        mock_fees = MagicMock()
        mock_fees.taker_fee = 0.07
        mock_fees.maker_fee = 0.03

        async def _passthrough_mtm(b, c, **kw):
            return await b.get_positions()

        with (
            patch("gimmes.cli.trading_context", _real_db_ctx),
            patch("gimmes.cli._mode_banner"),
            patch("gimmes.cli._mark_positions_to_market", _passthrough_mtm),
            patch(
                "gimmes.kalshi.markets.get_market",
                AsyncMock(return_value=h._stub_market()),
            ),
            patch(
                "gimmes.strategy.fee_cache.get_multipliers",
                MagicMock(return_value=mock_fees),
            ),
        ):
            result = runner.invoke(app, [
                "order", "KXBTCD-26AUG1118-T63599.99",
                "--side", "no", "--count", "733",
                "--price", "54", "--prob", "0.70", "--yes",
                "--agent", "closer",
            ])

        assert result.exit_code == 1, result.output
        assert broker.create_order.await_count == 0
