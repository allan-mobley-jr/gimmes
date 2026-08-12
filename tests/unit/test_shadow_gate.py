"""#769: the hourly distance gate governs.

Two 2026-08-11 losses (−$482.16, −$395.82) were both Shadow WOULD-PASS
entries priced by the floored 0.70 probability; filled WOULD-PASS
entries ran 2W-4L, −$1,244 — the entire in-band deficit. The verdict
now gates at three layers: Caddie recommendation, CM mechanical review
(prompt drift guards), and this CLI gate — an in-cycle hourly BUY whose
newest research memo carries `Shadow: WOULD-PASS` is hard-blocked.

Harness reuse follows test_order_agent_gate.py (#768); gate asserts
follow TestOrderReopenGate (exit code, message, no broker call, audit
row).
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from gimmes import cli as cli_module
from gimmes.cli import app
from gimmes.store.database import Database
from gimmes.store.observation_validator import (
    FLIP_WARNING_MARKER,
    parse_shadow_verdict,
)
from gimmes.store.queries import get_shadow_verdict_for_ticker, insert_candidate
from tests.unit import test_order_error_handling as h

WOULD_PASS_LINE = (
    "Shadow: WOULD-PASS | strike=$64100 spot=$64027 distance=$73 move30m=$123"
)
WOULD_PROCEED_LINE = (
    "Shadow: WOULD-PROCEED | strike=$64100 spot=$63500 distance=$600"
    " move30m=$123"
)


class TestParseShadowVerdict:
    def test_would_pass(self) -> None:
        assert parse_shadow_verdict(WOULD_PASS_LINE) == "WOULD-PASS"

    def test_would_proceed(self) -> None:
        assert parse_shadow_verdict(WOULD_PROCEED_LINE) == "WOULD-PROCEED"

    def test_unavailable_form(self) -> None:
        assert (
            parse_shadow_verdict("Shadow: UNAVAILABLE | reason=spot fetch"
                                 " failed")
            == "UNAVAILABLE"
        )

    def test_flip_warning_prefixed_memo(self) -> None:
        """The [FLIP-WARNING] marker is PREPENDED to memos — the Shadow
        line is line 2 there and must still be found."""
        memo = f"{FLIP_WARNING_MARKER} flip context\n{WOULD_PASS_LINE}\nmore"
        assert parse_shadow_verdict(memo) == "WOULD-PASS"

    def test_multiline_memo_with_preload(self) -> None:
        memo = (
            f"{WOULD_PASS_LINE}\n\nConferral preload:\n"
            "- Contrary scenario: BTC drifts up\n- Timing: 26 minutes"
        )
        assert parse_shadow_verdict(memo) == "WOULD-PASS"

    def test_missing_shadow_line(self) -> None:
        assert parse_shadow_verdict("Plain macro memo, no shadow.") is None

    def test_empty_and_none_like(self) -> None:
        assert parse_shadow_verdict("") is None

    def test_malformed_token(self) -> None:
        assert parse_shadow_verdict("Shadow: MAYBE | strike=$1") is None

    def test_mid_line_shadow_substring_does_not_match(self) -> None:
        # startswith on the stripped line: a line that begins with the
        # prefix matches even when indented; embedded mentions on lines
        # starting with other text do not.
        assert (
            parse_shadow_verdict(
                "The memo mentions Shadow: WOULD-PASS | inside prose"
            )
            is None
        )

    def test_indented_shadow_line_matches(self) -> None:
        assert parse_shadow_verdict(f"   {WOULD_PASS_LINE}") == "WOULD-PASS"

    def test_first_shadow_line_wins(self) -> None:
        memo = f"{WOULD_PROCEED_LINE}\n{WOULD_PASS_LINE}"
        assert parse_shadow_verdict(memo) == "WOULD-PROCEED"

    def test_no_space_after_colon_still_matches(self) -> None:
        """Liberal matching: a parse miss fails the gate OPEN, so
        plausible format drift must still be recognized."""
        assert (
            parse_shadow_verdict("Shadow:WOULD-PASS | strike=$1")
            == "WOULD-PASS"
        )

    def test_no_pipe_separator_still_matches(self) -> None:
        assert parse_shadow_verdict("Shadow: WOULD-PASS") == "WOULD-PASS"

    def test_malformed_line_does_not_mask_valid_later_line(self) -> None:
        memo = f"Shadow: MAYBE | garbage\n{WOULD_PASS_LINE}"
        assert parse_shadow_verdict(memo) == "WOULD-PASS"

    def test_crlf_line_endings(self) -> None:
        memo = f"header\r\n{WOULD_PASS_LINE}\r\nmore"
        assert parse_shadow_verdict(memo) == "WOULD-PASS"

    def test_bare_prefix_only(self) -> None:
        assert parse_shadow_verdict("Shadow: ") is None


def _hourly_config():
    c = h._stub_config()
    c.is_hourly_ticker = MagicMock(return_value=True)
    return c


def _non_hourly_config():
    c = h._stub_config()
    c.is_hourly_ticker = MagicMock(return_value=False)
    return c


def _error_codes(insert_error) -> list[str]:
    return [
        c.args[1].error_code
        for c in insert_error.await_args_list
        if len(c.args) > 1
    ]


class TestShadowGateCli:
    """In-cycle hourly BUY blocked on WOULD-PASS; everything else flows."""

    def _run(self, monkeypatch, *, verdict="WOULD-PASS", lookup_effect=None,
             cycle="42", config=None, extra_args=None, cli_args=None):
        if cycle is None:
            monkeypatch.delenv("GIMMES_CYCLE", raising=False)
        else:
            monkeypatch.setenv("GIMMES_CYCLE", cycle)
        self._lookup = AsyncMock(
            return_value=(verdict, 0 if verdict is None else 1),
            side_effect=lookup_effect,
        )
        broker = h._make_mock_broker()
        self._last_broker = broker
        args = ["--agent", "closer"] + (extra_args or [])
        result, console, insert_error = h._run_order_cli(
            broker,
            extra_args=None if cli_args else args,
            cli_args=cli_args,
            config=config if config is not None else _hourly_config(),
            shadow_verdict_mock=self._lookup,
        )
        return result, h._printed(console), insert_error

    def test_would_pass_blocked(self, monkeypatch) -> None:
        import json

        from gimmes.models.error import ErrorCategory, ErrorSeverity

        result, out, insert_error = self._run(monkeypatch)
        assert result.exit_code == 1, out
        assert "Hourly shadow gate (#769)" in out
        assert self._last_broker.create_order.await_count == 0
        assert self._lookup.await_args.args[1] == "TEST-TICKER"
        blocked = [
            c.args[1]
            for c in insert_error.await_args_list
            if len(c.args) > 1
            and c.args[1].error_code == "shadow_gate_blocked"
        ]
        assert len(blocked) == 1
        entry = blocked[0]
        assert entry.severity == ErrorSeverity.ERROR
        assert entry.category == ErrorCategory.RISK_BREACH
        assert entry.cycle == 42
        assert json.loads(entry.context) == {
            "ticker": "TEST-TICKER",
            "side": "yes",
            "cycle": 42,
            "verdict": "WOULD-PASS",
        }

    def test_would_proceed_allowed(self, monkeypatch) -> None:
        result, out, _ = self._run(monkeypatch, verdict="WOULD-PROCEED")
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1

    def test_unavailable_allowed(self, monkeypatch) -> None:
        result, out, _ = self._run(monkeypatch, verdict="UNAVAILABLE")
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1

    def test_no_verdict_allowed_with_audit_row(self, monkeypatch) -> None:
        result, out, insert_error = self._run(monkeypatch, verdict=None)
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1
        assert "shadow_gate_no_verdict" in _error_codes(insert_error)

    def test_non_hourly_unaffected(self, monkeypatch) -> None:
        result, out, insert_error = self._run(
            monkeypatch, config=_non_hourly_config(),
        )
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1
        assert self._lookup.await_count == 0
        assert "shadow_gate_no_verdict" not in _error_codes(insert_error)

    def test_sell_unaffected(self, monkeypatch) -> None:
        result, out, _ = self._run(
            monkeypatch,
            cli_args=[
                "order", "TEST-TICKER", "--action", "sell",
                "--side", "yes", "--count", "10", "--yes",
                "--agent", "closer",
            ],
        )
        assert "Hourly shadow gate (#769)" not in out
        assert self._lookup.await_count == 0

    def test_inert_without_cycle_env(self, monkeypatch) -> None:
        result, out, _ = self._run(monkeypatch, cycle=None)
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1
        assert self._lookup.await_count == 0

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

    def test_lookup_failure_fails_open(self, monkeypatch) -> None:
        result, out, insert_error = self._run(
            monkeypatch,
            lookup_effect=sqlite3.OperationalError("locked"),
        )
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1
        assert "shadow_gate_lookup_failed" in _error_codes(insert_error)
        # The failure path must not ALSO claim a missing verdict
        assert "shadow_gate_no_verdict" not in _error_codes(insert_error)

    def test_identity_gate_fires_before_shadow_gate(
        self, monkeypatch,
    ) -> None:
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
        assert "Hourly shadow gate (#769)" not in out
        assert self._lookup.await_count == 0

    def test_terminal_gate_fires_before_shadow_gate(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv("GIMMES_CYCLE", "42")
        shadow = AsyncMock(return_value=("WOULD-PASS", 1))
        broker = h._make_mock_broker()
        result, console, _ = h._run_order_cli(
            broker, extra_args=["--agent", "closer"],
            config=_hourly_config(),
            terminal_attempt_mock=AsyncMock(return_value=True),
            shadow_verdict_mock=shadow,
        )
        out = h._printed(console)
        assert result.exit_code == 1, out
        assert "Order terminal gate (#768)" in out
        assert shadow.await_count == 0


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


class TestGetShadowVerdictForTicker:
    def test_newest_row_wins(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"

        async def _go(db):
            await insert_candidate(
                db, "KXBTCD-26AUG1210-T64099.99", "BTC hourly", 0.41,
                0.70, 0.29, 71.0, WOULD_PROCEED_LINE,
                recommendation="proceed",
            )
            await insert_candidate(
                db, "KXBTCD-26AUG1210-T64099.99", "BTC hourly", 0.41,
                0.70, 0.29, 71.0, WOULD_PASS_LINE,
                recommendation="pass",
            )
            return await get_shadow_verdict_for_ticker(
                db, "KXBTCD-26AUG1210-T64099.99",
            )

        assert _db_run(db_path, _go) == ("WOULD-PASS", 2)

    def test_no_candidate_returns_none_zero_rows(self, tmp_path) -> None:
        db_path = tmp_path / "gimmes.db"

        async def _go(db):
            return await get_shadow_verdict_for_ticker(db, "KXNOPE-1")

        assert _db_run(db_path, _go) == (None, 0)

    def test_bookkeeping_row_does_not_shadow_verdict(self, tmp_path) -> None:
        """A #676-style bookkeeping row (no memo) logged AFTER research
        must not disarm the gate — the scan skips to the researched
        row's verdict."""
        db_path = tmp_path / "gimmes.db"

        async def _go(db):
            await insert_candidate(
                db, "KXBTCD-26AUG1210-T64099.99", "BTC hourly", 0.41,
                0.70, 0.29, 71.0, WOULD_PASS_LINE,
                recommendation="pass",
            )
            await insert_candidate(
                db, "KXBTCD-26AUG1210-T64099.99", "BTC hourly", 0.0,
                0.0, 0.0, 0.0, "market-info failed — bookkeeping row",
            )
            return await get_shadow_verdict_for_ticker(
                db, "KXBTCD-26AUG1210-T64099.99",
            )

        assert _db_run(db_path, _go) == ("WOULD-PASS", 2)

    def test_unparseable_rows_report_row_count(self, tmp_path) -> None:
        """Rows exist but none parses: (None, N) — the audit trail's
        drift-vs-no-candidate discriminator."""
        db_path = tmp_path / "gimmes.db"

        async def _go(db):
            await insert_candidate(
                db, "KXBTCD-26AUG1210-T64099.99", "BTC hourly", 0.41,
                0.70, 0.29, 71.0, "memo without a shadow line",
            )
            return await get_shadow_verdict_for_ticker(
                db, "KXBTCD-26AUG1210-T64099.99",
            )

        assert _db_run(db_path, _go) == (None, 1)


class TestC2199RoundTrip:
    """The incident shape end-to-end on a real DB: a WOULD-PASS memo
    logged at research time blocks the in-cycle order with no #769
    mocks; re-logging WOULD-PROCEED unblocks."""

    def _order_attempt(self, db_path, ticker):
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
                "order", ticker,
                "--side", "no", "--count", "100",
                "--price", "41", "--prob", "0.70", "--yes",
                "--agent", "closer",
            ])
        return result, broker

    def test_would_pass_memo_blocks_then_proceed_unblocks(
        self, monkeypatch, tmp_path,
    ) -> None:
        db_path = tmp_path / "gimmes.db"
        ticker = "KXBTCD-26AUG1210-T64099.99"
        monkeypatch.setenv("GIMMES_CYCLE", "2199")

        cfg = MagicMock()
        cfg.is_championship = False
        cfg.db_path = db_path
        cfg.strategy.side = "no"
        cfg.is_hourly_ticker = MagicMock(return_value=True)
        monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

        memo_file = tmp_path / "memo.txt"
        memo_file.write_text(f"{WOULD_PASS_LINE}\nrationale text\n")
        result = runner.invoke(app, [
            "log-candidate", ticker, "--title", "BTC hourly",
            "--price", "0.41", "--prob", "0.70", "--score", "71",
            "--memo-file", str(memo_file),
            "--recommendation", "pass",
        ])
        assert result.exit_code == 0, result.output

        result, broker = self._order_attempt(db_path, ticker)
        assert result.exit_code == 1, result.output
        assert broker.create_order.await_count == 0
        assert "Hourly shadow gate (#769)" in result.output

        memo_file.write_text(f"{WOULD_PROCEED_LINE}\nrationale text\n")
        result = runner.invoke(app, [
            "log-candidate", ticker, "--title", "BTC hourly",
            "--price", "0.41", "--prob", "0.70", "--score", "71",
            "--memo-file", str(memo_file),
            "--recommendation", "proceed",
        ])
        assert result.exit_code == 0, result.output

        # The unblocked attempt proceeds past the gate into the real
        # order flow, which this stub-config harness can't complete —
        # the assertion that matters is that the #769 gate no longer
        # fires on the WOULD-PROCEED memo.
        result, broker = self._order_attempt(db_path, ticker)
        assert "Hourly shadow gate (#769)" not in result.output
