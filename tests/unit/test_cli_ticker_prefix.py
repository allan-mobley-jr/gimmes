"""CLI integration tests for ticker prefix resolution (#582).

Verifies the three lookup commands (``position-context``,
``trades``, ``market-info``) accept prefix arguments and surface the
expected ambiguous/no-match/exact-match branches.

Each test seeds a temp SQLite DB, points ``load_config`` at it, mocks
the Kalshi client for ``market-info``, and invokes the CLI through
``typer.testing.CliRunner``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from gimmes.cli import app
from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import insert_trade, upsert_position

runner = CliRunner()


def _pos(ticker: str, *, count: int = 100) -> Position:
    return Position(
        ticker=ticker, side="yes", count=count, avg_price=0.50,
        market_price=0.50, cost_basis=50.0,
    )


def _trade(ticker: str) -> TradeDecision:
    return TradeDecision(
        ticker=ticker,
        action=TradeDecision.Action.OPEN,
        side="yes",
        count=10,
        price=0.5,
        model_probability=0.6,
        gimme_score=70.0,
        edge=0.1,
        kelly_fraction=0.02,
        rationale="test",
        thesis="A multi-line thesis stored at open time.",
        agent="test-agent",
        order_id="o1",
    )


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """Create a temp DB with three open positions (one with a long
    ticker and two sharing a ``KXCPI`` prefix) plus matching trades.

    Sync fixture so the CLI tests can call ``CliRunner.invoke`` without
    being inside a running asyncio event loop (the CLI's ``_run``
    helper itself drives ``asyncio.run`` and cannot nest).
    """
    db_path = tmp_path / "gimmes.db"

    async def _seed() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            for t in (
                "KXJOBLESSCLAIMS-26MAY14-210000",
                "KXCPI-26APR-T0.5",
                "KXCPI-26MAY-T0.6",
            ):
                await upsert_position(db, _pos(t))
                await insert_trade(db, _trade(t))
        finally:
            await db.close()

    asyncio.run(_seed())
    return db_path


def _config(db_path: Path) -> MagicMock:
    c = MagicMock()
    c.db_path = db_path
    return c


def _column_text(output: str, index: int) -> str:
    """Concatenate one column's content across every body row of a
    Rich table, including fold-continuation rows. Lets tests assert a
    cell value appears even when Rich wraps it across rows (#567)."""
    parts: list[str] = []
    for line in output.splitlines():
        if not line.startswith("│"):
            continue
        segments = line.split("│")
        if len(segments) <= index + 1:
            continue
        parts.append(segments[index].strip())
    return "".join(parts)


def _ticker_column_text(output: str) -> str:
    """First (Ticker) column's concatenated content."""
    return _column_text(output, 1)


class TestPositionContext:
    def test_unique_prefix_resolves(self, seeded_db: Path) -> None:
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)):
            result = runner.invoke(app, ["position-context", "KXJOB"])
        assert result.exit_code == 0, result.output
        assert "KXJOBLESSCLAIMS-26MAY14-210000" in result.output
        assert "Position Context" in result.output

    def test_ambiguous_prefix_lists_candidates_and_exits_1(
        self, seeded_db: Path,
    ) -> None:
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)):
            result = runner.invoke(app, ["position-context", "KXCPI"])
        assert result.exit_code == 1
        assert "Ambiguous ticker prefix 'KXCPI'" in result.output
        assert "KXCPI-26APR-T0.5" in result.output
        assert "KXCPI-26MAY-T0.6" in result.output

    def test_no_match_yellow_message(self, seeded_db: Path) -> None:
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)):
            result = runner.invoke(app, ["position-context", "ZZZ"])
        assert result.exit_code == 0
        assert "No open position found for ZZZ" in result.output

    def test_exact_full_ticker_backcompat(
        self, seeded_db: Path,
    ) -> None:
        # The pre-#582 happy path: full exact ticker still works.
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)):
            result = runner.invoke(
                app, ["position-context", "KXJOBLESSCLAIMS-26MAY14-210000"],
            )
        assert result.exit_code == 0, result.output
        assert "KXJOBLESSCLAIMS-26MAY14-210000" in result.output


class TestPositionNotes:
    def test_unique_prefix_resolves(self, seeded_db: Path) -> None:
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)):
            result = runner.invoke(app, ["position-notes", "KXJOB"])
        # No notes seeded, but the resolver should fire and the
        # message should reference the FULL resolved ticker, not the
        # prefix the user typed.
        assert result.exit_code == 0, result.output
        assert "KXJOBLESSCLAIMS-26MAY14-210000" in result.output

    def test_ambiguous_prefix_lists_candidates_and_exits_1(
        self, seeded_db: Path,
    ) -> None:
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)):
            result = runner.invoke(app, ["position-notes", "KXCPI"])
        assert result.exit_code == 1
        assert "Ambiguous ticker prefix 'KXCPI'" in result.output
        assert "Specify a longer prefix" in result.output

    def test_resolves_closed_position_ticker(self, seeded_db: Path) -> None:
        # The seeded fixture inserts trades for KXCPI-26APR-T0.5,
        # KXCPI-26MAY-T0.6, and KXJOBLESSCLAIMS-26MAY14-210000.
        # Insert a position note for a CLOSED ticker (one that exists
        # in trades but NOT in open positions) and verify position-notes
        # can still surface it. Required for #586's Step 4c "Stop-loss
        # reopen lockout" — after Closer closes a position, the ticker
        # drops out of `positions` but its decision notes must remain
        # reachable so Caddie Master can check the lockout.
        from gimmes.models.trade import TradeDecision
        from gimmes.store.queries import insert_position_note, insert_trade

        async def _seed_closed_note() -> None:
            db = Database(seeded_db)
            await db.connect()
            try:
                # Insert a close trade for a ticker NOT in open positions.
                closed_ticker = "KXADP-26APR-T125000"
                await insert_trade(
                    db,
                    TradeDecision(
                        ticker=closed_ticker,
                        action=TradeDecision.Action.CLOSE,
                        side="no",
                        count=100,
                        price=0.85,
                        model_probability=0.85,
                        gimme_score=0,
                        edge=0.0,
                        kelly_fraction=0.0,
                        rationale="closed",
                        agent="closer",
                        order_id="o-closed",
                    ),
                )
                await insert_position_note(
                    db,
                    ticker=closed_ticker,
                    cycle=1199,
                    agent="caddie-master",
                    note_type="decision",
                    body=(
                        "Decision: CLOSE.\n"
                        "Reasoning: Stop-loss BREACHED.\n"
                        "Trigger: Stop-loss breach"
                    ),
                )
            finally:
                await db.close()
        asyncio.run(_seed_closed_note())

        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)):
            result = runner.invoke(
                app, ["position-notes", "KXADP-26APR-T125000"],
            )
        assert result.exit_code == 0, result.output
        # The CLOSE decision note must be reachable so the lockout
        # check actually has something to match against.
        assert "Trigger: Stop-loss breach" in result.output
        assert "Decision: CLOSE" in result.output


class TestTradesCommand:
    # ``gimmes trades`` opens ``Database()`` with no path arg, so it
    # falls back to ``GIMMES_HOME / "gimmes.db"``. Patching
    # ``gimmes.config.GIMMES_HOME`` to ``tmp_path`` redirects the
    # default to our seeded DB. ``Database.__init__`` re-imports the
    # constant lazily, so this patch takes effect for new instances.
    def test_prefix_filter_includes_multiple_tickers(
        self, seeded_db: Path,
    ) -> None:
        # Three positions ⇒ three trades. ``trades --ticker KXCPI``
        # should return both KXCPI-* trades (prefix filter), not error
        # on ambiguity.
        with patch("gimmes.config.GIMMES_HOME", seeded_db.parent):
            result = runner.invoke(
                app, ["trades", "--ticker", "KXCPI", "-n", "10"],
            )
        assert result.exit_code == 0, result.output
        ticker_col = _ticker_column_text(result.output)
        assert "KXCPI-26APR-T0.5" in ticker_col
        assert "KXCPI-26MAY-T0.6" in ticker_col
        assert "KXJOBLESSCLAIMS" not in ticker_col

    def test_exact_full_ticker_backcompat(
        self, seeded_db: Path,
    ) -> None:
        with patch("gimmes.config.GIMMES_HOME", seeded_db.parent):
            result = runner.invoke(
                app, ["trades", "--ticker", "KXCPI-26APR-T0.5", "-n", "10"],
            )
        assert result.exit_code == 0, result.output
        ticker_col = _ticker_column_text(result.output)
        assert "KXCPI-26APR-T0.5" in ticker_col
        # Sister ticker must NOT appear — exact ticker that's also a
        # prefix shouldn't accidentally pull the other in.
        # (KXCPI-26APR-T0.5 is NOT a prefix of KXCPI-26MAY-T0.6, so
        #  the natural behavior excludes it.)
        assert "KXCPI-26MAY-T0.6" not in ticker_col

    def test_prob_and_outcome_columns(self, seeded_db: Path) -> None:
        """#656: the trades table exposes the entry probability and
        the market's resolution so calibration audits (entry prob vs
        resolved outcome) work through the CLI."""

        async def _resolve() -> None:
            db = Database(seeded_db)
            await db.connect()
            try:
                # side is 'yes' in the fixture — 'no' means it lost.
                # 'no' is also unambiguous in the rendered table (the
                # Side cells all say 'yes'; 'yes' would be vacuous).
                await db.conn.execute(
                    "UPDATE trades SET resolved_outcome = 'no'"
                    " WHERE ticker = 'KXCPI-26APR-T0.5'",
                )
                await db.conn.commit()
            finally:
                await db.close()

        asyncio.run(_resolve())
        # 10 columns don't fit an 80-col terminal — headers get
        # crushed to nothing. Widen so every header renders.
        with patch("gimmes.config.GIMMES_HOME", seeded_db.parent), \
                patch.dict(os.environ, {"COLUMNS": "200"}):
            result = runner.invoke(app, ["trades", "-n", "10"])
        assert result.exit_code == 0, result.output
        assert "Prob" in result.output
        assert "Outcome" in result.output
        # Fixture trades carry model_probability=0.6 → rendered 60.0%
        assert "60.0%" in result.output
        outcome_col = _column_text(result.output, 9)
        assert "no" in outcome_col
        # Unresolved rows render a blank Outcome, not "None"
        assert "None" not in outcome_col

    def test_wildcard_in_input_is_rejected(self, seeded_db: Path) -> None:
        # ``trades`` must apply the same wildcard guard as the other
        # lookup commands (``%`` and ``_`` are SQL LIKE wildcards that
        # would silently widen the filter to all trades).
        with patch("gimmes.config.GIMMES_HOME", seeded_db.parent):
            result = runner.invoke(
                app, ["trades", "--ticker", "%", "-n", "10"],
            )
        assert result.exit_code != 0
        # _run formats ValueError through Rich; assert the actionable
        # error class is present without depending on Rich's exact
        # formatting.
        assert "[A-Z0-9.-]" in result.output

    def test_whitespace_and_lowercase_input_normalized(
        self, seeded_db: Path,
    ) -> None:
        # Copy-paste from another tool can carry surrounding whitespace
        # or lowercase. Both must normalize before matching so the
        # prefix filter behaves the same as the canonical-uppercase
        # input would.
        with patch("gimmes.config.GIMMES_HOME", seeded_db.parent):
            result = runner.invoke(
                app, ["trades", "--ticker", "  kxcpi  ", "-n", "10"],
            )
        assert result.exit_code == 0, result.output
        ticker_col = _ticker_column_text(result.output)
        assert "KXCPI-26APR-T0.5" in ticker_col
        assert "KXCPI-26MAY-T0.6" in ticker_col

    def test_exact_match_short_ticker_does_not_pull_in_longer(
        self, seeded_db: Path,
    ) -> None:
        # Critical backward-compat test: if a user types ``KXCPI``
        # (full short ticker) and the DB has both ``KXCPI`` and
        # ``KXCPI-26APR-T0.5``, the trades command must NOT include
        # the longer sister. The CLI uses the resolver's exact-match
        # shortcut to preserve this.
        db = Database(seeded_db)
        asyncio.run(_add_extra(db))
        with patch("gimmes.config.GIMMES_HOME", seeded_db.parent):
            result = runner.invoke(
                app, ["trades", "--ticker", "KXCPI", "-n", "10"],
            )
        assert result.exit_code == 0, result.output
        ticker_col = _ticker_column_text(result.output)
        assert "KXCPI" in ticker_col
        # The KXCPI-26* sisters MUST NOT appear because KXCPI is itself
        # an exact match in the DB.
        assert "KXCPI-26APR-T0.5" not in ticker_col
        assert "KXCPI-26MAY-T0.6" not in ticker_col


async def _add_extra(db: Database) -> None:
    """Helper: seed a bare ``KXCPI`` position + trade on top of the
    standard fixture so the exact-match-short-ticker test has
    something to disambiguate against."""
    await db.connect()
    try:
        await upsert_position(db, _pos("KXCPI"))
        await insert_trade(db, _trade("KXCPI"))
    finally:
        await db.close()


async def _add_position_only(db: Database, ticker: str) -> None:
    """Helper: seed a position (no trade row) so the trades-table
    exact-match probe can be verified against positions-only inputs."""
    await db.connect()
    try:
        await upsert_position(db, _pos(ticker))
    finally:
        await db.close()


class TestTradesExactMatchProbeIsTradesTableOnly:
    def test_input_only_in_positions_still_uses_prefix_filter(
        self, seeded_db: Path,
    ) -> None:
        # Seed a ticker into ``positions`` only (no matching trade).
        # If the exact-match probe consulted ``known_markets`` (which
        # includes positions), it would disable prefix mode and the
        # user would get an empty trades result. The probe must be
        # trades-only so the user still sees the trade family that
        # shares the prefix.
        asyncio.run(_add_position_only(
            Database(seeded_db), "KXCPI",
        ))
        with patch("gimmes.config.GIMMES_HOME", seeded_db.parent):
            result = runner.invoke(
                app, ["trades", "--ticker", "KXCPI", "-n", "10"],
            )
        assert result.exit_code == 0, result.output
        ticker_col = _ticker_column_text(result.output)
        # ``KXCPI`` itself was only seeded into positions; ``trades``
        # rows are only the ``KXCPI-*`` sisters from the fixture, so
        # prefix mode must surface them.
        assert "KXCPI-26APR-T0.5" in ticker_col
        assert "KXCPI-26MAY-T0.6" in ticker_col


def _stub_market(ticker: str) -> MagicMock:
    m = MagicMock()
    m.ticker = ticker
    m.event_ticker = "EVT"
    m.title = f"Market {ticker}"
    m.status = MagicMock()
    m.status.value = "active"
    m.yes_bid = 0.50
    m.yes_ask = 0.55
    m.last_price = 0.52
    m.spread = 0.05
    m.volume = 100
    m.volume_24h = 50
    m.open_interest = 30
    m.close_time = "2026-05-14"
    m.subtitle = ""
    m.rules_primary = ""
    return m


def _stub_orderbook() -> MagicMock:
    ob = MagicMock()
    ob.best_yes_bid = 0.50
    ob.best_yes_ask = 0.55
    ob.yes_bids = [MagicMock()]
    return ob


class TestMarketInfo:
    def test_unique_prefix_resolves_then_calls_kalshi(
        self, seeded_db: Path,
    ) -> None:
        get_market = AsyncMock(
            return_value=_stub_market("KXJOBLESSCLAIMS-26MAY14-210000"),
        )
        get_orderbook = AsyncMock(return_value=_stub_orderbook())
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.markets.get_orderbook", get_orderbook), \
             patch("gimmes.kalshi.client.KalshiClient"):
            result = runner.invoke(app, ["market-info", "KXJOB"])
        assert result.exit_code == 0, result.output
        # The resolver should have substituted the full ticker before
        # the Kalshi call; verify get_market was called with the full
        # ticker rather than the prefix.
        assert get_market.await_args is not None
        called_ticker = get_market.await_args.args[1]
        assert called_ticker == "KXJOBLESSCLAIMS-26MAY14-210000"

    def test_ambiguous_prefix_skips_kalshi_and_exits_1(
        self, seeded_db: Path,
    ) -> None:
        get_market = AsyncMock()  # must NOT be called
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.client.KalshiClient"):
            result = runner.invoke(app, ["market-info", "KXCPI"])
        assert result.exit_code == 1
        assert "Ambiguous ticker prefix 'KXCPI'" in result.output
        get_market.assert_not_called()

    def test_no_local_match_falls_through_to_kalshi(
        self, seeded_db: Path,
    ) -> None:
        # Prefix matches nothing local — pass through to Kalshi with
        # the literal input. Preserves first-time-lookup behavior.
        get_market = AsyncMock(return_value=_stub_market("KXBRANDNEW-26MAY-T1.0"))
        get_orderbook = AsyncMock(return_value=_stub_orderbook())
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.markets.get_orderbook", get_orderbook), \
             patch("gimmes.kalshi.client.KalshiClient"):
            result = runner.invoke(app, ["market-info", "KXBRANDNEW-26MAY-T1.0"])
        assert result.exit_code == 0, result.output
        assert get_market.await_args is not None
        assert get_market.await_args.args[1] == "KXBRANDNEW-26MAY-T1.0"

    def test_exact_full_ticker_backcompat(
        self, seeded_db: Path,
    ) -> None:
        get_market = AsyncMock(
            return_value=_stub_market("KXCPI-26APR-T0.5"),
        )
        get_orderbook = AsyncMock(return_value=_stub_orderbook())
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.markets.get_orderbook", get_orderbook), \
             patch("gimmes.kalshi.client.KalshiClient"):
            result = runner.invoke(app, ["market-info", "KXCPI-26APR-T0.5"])
        assert result.exit_code == 0, result.output
        assert get_market.await_args.args[1] == "KXCPI-26APR-T0.5"

    def test_market_info_displays_settlement_rules(
        self, seeded_db: Path,
    ) -> None:
        """market-info must show the verbatim settlement sentence and
        subtitle so agents can ground YES/NO threshold semantics —
        without these rows the #641 semantics-grounding prompt rules
        are unimplementable (agents have no other way to read
        rules_primary)."""
        market = _stub_market("KXCPI-26APR-T0.5")
        market.subtitle = "0.5% or above"
        # Bracketed clause included deliberately: Rich parses [word
        # groups] as style tags and silently deletes them unless the
        # value is markup-escaped — the settlement sentence must
        # survive verbatim (#641).
        market.rules_primary = (
            "If the Consumer Price Index [as reported by the BLS]"
            " increases by more than 0.5%"
            " in April 2026, the market resolves to Yes."
        )
        get_market = AsyncMock(return_value=market)
        get_orderbook = AsyncMock(return_value=_stub_orderbook())
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.markets.get_orderbook", get_orderbook), \
             patch("gimmes.kalshi.client.KalshiClient"):
            result = runner.invoke(app, ["market-info", "KXCPI-26APR-T0.5"])
        assert result.exit_code == 0, result.output
        assert "Rules (primary)" in result.output
        assert "Subtitle" in result.output
        assert "0.5% or above" in result.output
        # The settlement sentence must appear (Rich may wrap it across
        # lines, so assert on fragments short enough to survive
        # wrapping).
        assert "resolves to" in result.output
        # The bracketed clause must survive — without markup escaping,
        # Rich eats "[as reported by the BLS]" as a style tag (#641).
        assert "as reported" in result.output

    def test_market_info_settlement_risk_flags_survive_markup(
        self, seeded_db: Path,
    ) -> None:
        """With red flags present, SettlementRisk.summary reads
        `found [discretion, ...]` — the bracketed flag list must be
        markup-escaped inside the color tags or Rich eats it exactly
        when the warning matters most (#641 Copilot review)."""
        market = _stub_market("KXCPI-26APR-T0.5")
        # Bracketed lowercase segment in the title: Rich parses table
        # titles for markup too, so unescaped titles lose it (#641).
        market.title = "CPI [preliminary] April 2026"
        market.rules_primary = (
            "The market may be settled at the sole discretion of the"
            " exchange."
        )
        get_market = AsyncMock(return_value=market)
        get_orderbook = AsyncMock(return_value=_stub_orderbook())
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.markets.get_orderbook", get_orderbook), \
             patch("gimmes.kalshi.client.KalshiClient"):
            result = runner.invoke(app, ["market-info", "KXCPI-26APR-T0.5"])
        assert result.exit_code == 0, result.output
        # Assert on bracket-adjacent fragments: the bare word
        # "discretion" also appears in the verbatim Rules (primary)
        # row, so it cannot distinguish the two rows — only the
        # Settlement Risk flag list contains "[sole" / "discretion]",
        # and both vanish if the escape is reverted (mutation-verified
        # in review). The fragments have no internal spaces, so Rich
        # word-wrap cannot split them.
        assert "[sole" in result.output, (
            "The bracketed red-flag list must render verbatim — without"
            " escaping, Rich eats '[sole discretion]' as a style tag"
            " and the Settlement Risk row silently truncates at"
            " 'found' (#641)."
        )
        assert "discretion]" in result.output
        assert "[preliminary]" in result.output, (
            "The market title must render verbatim as the table title —"
            " Rich markup-parses string titles, eating lowercase-start"
            " bracketed segments unless escaped (#641)."
        )
        # #644: title escaping moved INTO format_kv_table — a caller
        # that pre-escapes now double-escapes, rendering a literal
        # backslash that the positive assertion above cannot see.
        assert "\\[preliminary]" not in result.output

    def test_market_info_em_dash_fallback_for_missing_fields(
        self, seeded_db: Path,
    ) -> None:
        """Missing subtitle/rules must render the em-dash fallback,
        never `None` — an agent reading `Rules (primary): None` is the
        precise semantics-grounding failure #641 exists to stop."""
        market = _stub_market("KXCPI-26APR-T0.5")
        market.subtitle = None
        market.rules_primary = ""
        get_market = AsyncMock(return_value=market)
        get_orderbook = AsyncMock(return_value=_stub_orderbook())
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.markets.get_orderbook", get_orderbook), \
             patch("gimmes.kalshi.client.KalshiClient"):
            result = runner.invoke(app, ["market-info", "KXCPI-26APR-T0.5"])
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert any("Subtitle" in ln and "—" in ln for ln in lines), (
            "Subtitle row must fall back to em-dash when the field is"
            " None (#641)."
        )
        assert any("Rules (primary)" in ln and "—" in ln for ln in lines), (
            "Rules (primary) row must fall back to em-dash when the"
            " field is empty (#641)."
        )
        assert "None" not in result.output


def _read_error_log(db_path: Path) -> list[dict]:
    """Synchronous sqlite3 read of error_log rows, returned newest first.
    Used by CLI failure-path tests to assert that the failing command
    landed a structured row for Groundskeeper to surface."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT severity, category, error_code, component, message,"
            " context"
            " FROM error_log ORDER BY id DESC",
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


class TestErrorLogging:
    """Verify that the failure paths in market-info and position-context
    (#588) write to the error_log table, so Groundskeeper sees them
    instead of silently passing through to the Python logger only."""

    def test_market_info_ambiguous_logs_error(self, seeded_db: Path) -> None:
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)):
            result = runner.invoke(app, ["market-info", "KXCPI"])
        assert result.exit_code == 1
        errors = _read_error_log(seeded_db)
        assert any(
            e["category"] == "config_error"
            and e["error_code"] == "ambiguous_ticker"
            and e["component"] == "cli.market-info"
            and "KXCPI" in e["message"]
            for e in errors
        ), errors

    def test_market_info_kalshi_http_error_logs_error(
        self, seeded_db: Path,
    ) -> None:
        response = MagicMock(status_code=404, text="not found")
        get_market = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "404 Client Error",
                request=MagicMock(),
                response=response,
            ),
        )
        get_orderbook = AsyncMock(return_value=_stub_orderbook())
        # #778: pin the empty-recovery branch deterministically — an
        # unpatched list_markets against the mocked client degrades
        # through the recovery helper's except by accident.
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.markets.get_orderbook", get_orderbook), \
             patch("gimmes.kalshi.markets.list_markets",
                   AsyncMock(return_value=([], None))), \
             patch("gimmes.kalshi.client.KalshiClient"):
            result = runner.invoke(app, ["market-info", "KXBRANDNEW-26MAY-T1.0"])
        assert result.exit_code == 1, result.output
        errors = _read_error_log(seeded_db)
        assert any(
            e["category"] == "api_error"
            and e["error_code"] == "http_status_error"
            and e["component"] == "cli.market-info"
            and "404" in e["message"]
            for e in errors
        ), errors

    def test_position_context_no_match_logs_error(self, seeded_db: Path) -> None:
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)):
            result = runner.invoke(app, ["position-context", "ZZZ"])
        assert result.exit_code == 0
        errors = _read_error_log(seeded_db)
        assert any(
            e["category"] == "data_integrity"
            and e["error_code"] == "position_not_found"
            and e["component"] == "cli.position-context"
            and "ZZZ" in e["message"]
            for e in errors
        ), errors

    def test_position_context_ambiguous_logs_error(
        self, seeded_db: Path,
    ) -> None:
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)):
            result = runner.invoke(app, ["position-context", "KXCPI"])
        assert result.exit_code == 1
        errors = _read_error_log(seeded_db)
        assert any(
            e["category"] == "config_error"
            and e["error_code"] == "ambiguous_ticker"
            and e["component"] == "cli.position-context"
            and "KXCPI" in e["message"]
            for e in errors
        ), errors

    def test_position_context_race_renders_closed_context(
        self, seeded_db: Path,
    ) -> None:
        # #751: the old race branch (position closed between resolver
        # read and trade lookup) now renders the closed-position
        # context — history exists, so this is a legitimate read, not
        # a data-integrity fault. No error row of any kind.
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch(
                "gimmes.store.queries.has_open_position",
                AsyncMock(return_value=False),
             ):
            result = runner.invoke(
                app,
                ["position-context", "KXJOBLESSCLAIMS-26MAY14-210000"],
            )
        assert result.exit_code == 0, result.output
        assert "POSITION CLOSED/SETTLED" in result.output
        assert "Position Context:" in result.output
        errors = _read_error_log(seeded_db)
        assert not any(
            e["error_code"]
            in ("position_closed_during_lookup", "position_not_found")
            for e in errors
        ), errors

    def test_ambiguous_context_caps_matches_to_20(
        self, seeded_db: Path,
    ) -> None:
        # Seed 25 sibling positions sharing prefix ``KXBLOAT``. The
        # logged error_log.context must truncate ``matches`` to 20 and
        # carry the full count in ``matches_total`` so a broad prefix
        # can't bloat the row. The terminal display already truncates
        # at the same limit (#588 Copilot review).
        import json as _json

        async def _seed_bloat() -> None:
            db = Database(seeded_db)
            await db.connect()
            try:
                for i in range(25):
                    await upsert_position(db, _pos(f"KXBLOAT-{i:02d}"))
            finally:
                await db.close()
        asyncio.run(_seed_bloat())

        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)):
            result = runner.invoke(app, ["position-context", "KXBLOAT"])
        assert result.exit_code == 1, result.output

        import sqlite3
        conn = sqlite3.connect(seeded_db)
        try:
            row = conn.execute(
                "SELECT context FROM error_log"
                " WHERE error_code='ambiguous_ticker'"
                " ORDER BY id DESC LIMIT 1",
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        ctx = _json.loads(row[0])
        assert len(ctx["matches"]) == 20
        assert ctx["matches_total"] == 25

    def test_market_info_network_error_logs_error(
        self, seeded_db: Path,
    ) -> None:
        # httpx.RequestError covers TimeoutException, ConnectError,
        # etc. — distinct from HTTPStatusError. The catch must fire
        # and log under category=network_error.
        get_market = AsyncMock(
            side_effect=httpx.ConnectError("connection refused"),
        )
        get_orderbook = AsyncMock(return_value=_stub_orderbook())
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.markets.get_orderbook", get_orderbook), \
             patch("gimmes.kalshi.client.KalshiClient"):
            result = runner.invoke(app, ["market-info", "KXBRANDNEW-26MAY-T1.0"])
        assert result.exit_code == 1, result.output
        errors = _read_error_log(seeded_db)
        assert any(
            e["category"] == "network_error"
            and e["error_code"] == "request_error"
            and e["component"] == "cli.market-info"
            for e in errors
        ), errors


class TestMarketInfo404Recovery:
    """#778: a 404 on a strike-shaped ticker triggers event-listing
    recovery — real tickers printed, distinct WARNING error code —
    while genuine unknowns keep the ERROR http_status_error path."""

    @staticmethod
    def _mk(ticker):
        m = MagicMock()
        m.ticker = ticker
        return m

    @staticmethod
    def _run(seeded_db, ticker, *, list_markets=None,
             list_markets_effect=None, status=404):
        response = MagicMock(status_code=status, text="err")
        get_market = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "err", request=MagicMock(), response=response,
            ),
        )
        lm = AsyncMock(
            return_value=(list_markets or [], None),
            side_effect=list_markets_effect,
        )
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch("gimmes.kalshi.markets.get_market", get_market), \
             patch("gimmes.kalshi.markets.get_orderbook",
                   AsyncMock(return_value=_stub_orderbook())), \
             patch("gimmes.kalshi.markets.list_markets", lm), \
             patch("gimmes.kalshi.client.KalshiClient"):
            result = runner.invoke(app, ["market-info", ticker])
        return result, lm

    def test_404_with_event_recovery_suggests_real_tickers(
        self, seeded_db: Path,
    ) -> None:
        result, _ = self._run(
            seeded_db, "KXBTCD-26AUG1313-T63399",
            list_markets=[
                self._mk("KXBTCD-26AUG1313-T63899.99"),
                self._mk("KXBTCD-26AUG1313-T63399.99"),
            ],
        )
        assert result.exit_code == 1
        assert "The event KXBTCD-26AUG1313 EXISTS" in result.output
        assert "KXBTCD-26AUG1313-T63399.99" in result.output
        assert "NEVER guess format variants" in result.output
        errors = _read_error_log(seeded_db)
        row = [e for e in errors if e["error_code"] == "ticker_not_found"]
        assert len(row) == 1
        assert row[0]["severity"] == "warning"
        assert row[0]["category"] == "config_error"
        import json

        ctx = json.loads(row[0]["context"])
        assert ctx["event_ticker"] == "KXBTCD-26AUG1313"
        assert ctx["suggestions_total"] == 2
        # Longest-common-prefix first: the intended correction leads
        assert ctx["suggestions"][0] == "KXBTCD-26AUG1313-T63399.99"
        assert not any(
            e["error_code"] == "http_status_error"
            and "KXBTCD" in (e["context"] or "")
            for e in errors
        )

    def test_404_recovery_empty_falls_back_to_error(
        self, seeded_db: Path,
    ) -> None:
        result, _ = self._run(seeded_db, "KXBTCD-26AUG1313-T63399")
        assert result.exit_code == 1
        assert "EXISTS" not in result.output
        errors = _read_error_log(seeded_db)
        assert any(
            e["error_code"] == "http_status_error" for e in errors
        )
        assert not any(
            e["error_code"] == "ticker_not_found" for e in errors
        )

    def test_404_recovery_failure_degrades(self, seeded_db: Path) -> None:
        result, _ = self._run(
            seeded_db, "KXBTCD-26AUG1313-T63399",
            list_markets_effect=RuntimeError("api down"),
        )
        assert result.exit_code == 1
        errors = _read_error_log(seeded_db)
        assert any(
            e["error_code"] == "http_status_error" for e in errors
        )

    def test_404_without_dash_t_skips_recovery(
        self, seeded_db: Path,
    ) -> None:
        result, lm = self._run(seeded_db, "KXINX-26AUG14H1600-B7737")
        assert result.exit_code == 1
        lm.assert_not_awaited()
        errors = _read_error_log(seeded_db)
        assert any(
            e["error_code"] == "http_status_error" for e in errors
        )

    def test_non_404_skips_recovery(self, seeded_db: Path) -> None:
        result, lm = self._run(
            seeded_db, "KXBTCD-26AUG1313-T63399", status=500,
        )
        assert result.exit_code == 1
        lm.assert_not_awaited()

    def test_suggestions_capped_and_prefix_sorted(
        self, seeded_db: Path,
    ) -> None:
        markets = [
            self._mk(f"KXBTCD-26AUG1313-T{60000 + i * 100}.99")
            for i in range(24)
        ] + [self._mk("KXBTCD-26AUG1313-T63399.99")]
        result, _ = self._run(
            seeded_db, "KXBTCD-26AUG1313-T63399",
            list_markets=markets,
        )
        assert result.exit_code == 1
        assert "... and 5 more" in result.output
        # The correction shares the longest prefix — it must lead the
        # list and survive the cap.
        lines = [ln.strip() for ln in result.output.splitlines()]
        first = next(
            ln for ln in lines if ln.startswith("KXBTCD-26AUG1313-T")
        )
        assert first == "KXBTCD-26AUG1313-T63399.99"
        # The stored context is capped too — unbounded ladders must
        # not bloat error_log rows (review-found)
        import json

        ctx = json.loads(_read_error_log(seeded_db)[0]["context"])
        assert len(ctx["suggestions"]) == 20
        assert ctx["suggestions_total"] == 25

    def test_negative_threshold_event_derivation(
        self, seeded_db: Path,
    ) -> None:
        """rsplit on the LAST '-T' keeps negative strikes correct —
        KXCPI-26JUN-T-0.2 derives event KXCPI-26JUN (review-found
        pin: a regex or split() refactor would silently break this)."""
        result, _ = self._run(
            seeded_db, "KXCPI-26JUN-T-0.2",
            list_markets=[self._mk("KXCPI-26JUN-T-0.1")],
        )
        assert "The event KXCPI-26JUN EXISTS" in result.output

    def test_orderbook_404_not_misclassified(
        self, seeded_db: Path,
    ) -> None:
        """A 404 whose failing request is the ORDERBOOK endpoint is a
        real API partial failure, never an unknown ticker
        (review-found)."""
        request = MagicMock()
        request.url = "https://api.example.com/markets/X/orderbook"
        response = MagicMock(status_code=404, text="not found")
        lm = AsyncMock(return_value=([self._mk("X-T1.99")], None))
        with patch("gimmes.cli.load_config", return_value=_config(seeded_db)), \
             patch("gimmes.kalshi.markets.get_market",
                   AsyncMock(
                       return_value=_stub_market(
                           "KXBTCD-26AUG1313-T63399.99",
                       ),
                   )), \
             patch(
                 "gimmes.kalshi.markets.get_orderbook",
                 AsyncMock(side_effect=httpx.HTTPStatusError(
                     "404", request=request, response=response,
                 )),
             ), \
             patch("gimmes.kalshi.markets.list_markets", lm), \
             patch("gimmes.kalshi.client.KalshiClient"):
            result = runner.invoke(
                app, ["market-info", "KXBTCD-26AUG1313-T63399.99"],
            )
        assert result.exit_code == 1
        lm.assert_not_awaited()
        assert "EXISTS" not in result.output
        errors = _read_error_log(seeded_db)
        assert any(
            e["error_code"] == "http_status_error" for e in errors
        )


class TestMarketInfo404SettledEvent:
    """#782: a 404 on a SETTLED event's ticker gets the wrong-hour
    signal — with ZERO settled tickers listed (a ladder-probing agent
    must not be handed the data it was fishing for)."""

    def test_settled_event_warns_without_suggestions(
        self, seeded_db: Path,
    ) -> None:
        settled = MagicMock()
        settled.ticker = "KXBTCD-26AUG1709-T63399.99"
        result, lm = TestMarketInfo404Recovery._run(
            seeded_db,
            "KXBTCD-26AUG1709-T63424.99",
            list_markets_effect=[([], None), ([settled], None)],
        )
        assert result.exit_code == 1
        assert "ALREADY SETTLED" in result.output
        assert "NEVER probe settled" in result.output
        assert "KXBTCD-26AUG1709-T63399.99" not in result.output
        assert lm.await_count == 2
        assert lm.await_args_list[1].kwargs["status"] == "settled"
        errors = _read_error_log(seeded_db)
        row = [e for e in errors if e["error_code"] == "ticker_not_found"]
        assert len(row) == 1
        assert row[0]["severity"] == "warning"
        import json

        ctx = json.loads(row[0]["context"])
        assert ctx["event_state"] == "settled"
        assert ctx["suggestions"] == []
        # Same key as the open branch — dashboards must not need two
        # queries for one concept (review-found)
        assert ctx["suggestions_total"] == 1
        assert not any(
            e["error_code"] == "http_status_error" for e in errors
        )

    def test_settled_lookup_failure_degrades(
        self, seeded_db: Path,
    ) -> None:
        result, _ = TestMarketInfo404Recovery._run(
            seeded_db,
            "KXBTCD-26AUG1709-T63424.99",
            list_markets_effect=[([], None), RuntimeError("api down")],
        )
        assert result.exit_code == 1
        errors = _read_error_log(seeded_db)
        assert any(
            e["error_code"] == "http_status_error" for e in errors
        )

    def test_both_lookups_empty_falls_back_to_error(
        self, seeded_db: Path,
    ) -> None:
        result, lm = TestMarketInfo404Recovery._run(
            seeded_db,
            "KXBTCD-26AUG1709-T63424.99",
        )
        assert result.exit_code == 1
        assert lm.await_count == 2
        errors = _read_error_log(seeded_db)
        assert any(
            e["error_code"] == "http_status_error" for e in errors
        )

    def test_open_branch_never_makes_second_call(
        self, seeded_db: Path,
    ) -> None:
        mk = MagicMock()
        mk.ticker = "KXBTCD-26AUG1710-T63499.99"
        result, lm = TestMarketInfo404Recovery._run(
            seeded_db,
            "KXBTCD-26AUG1710-T63499",
            list_markets=[mk],
        )
        assert result.exit_code == 1
        assert "EXISTS" in result.output
        assert lm.await_count == 1
