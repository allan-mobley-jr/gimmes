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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


def _ticker_column_text(output: str) -> str:
    """Concatenate the first (Ticker) column's content across every
    body row of a Rich table, including fold-continuation rows. Lets
    tests assert a full ticker appears even when Rich wraps it across
    cell rows (#567)."""
    parts: list[str] = []
    for line in output.splitlines():
        if not line.startswith("│"):
            continue
        segments = line.split("│")
        if len(segments) < 3:
            continue
        parts.append(segments[1].strip())
    return "".join(parts)


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
