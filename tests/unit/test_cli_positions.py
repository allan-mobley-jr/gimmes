"""Tests for ticker rendering in CLI display commands (#567).

The bug: Rich's default ``Column.overflow == "ellipsis"`` truncated long
tickers (>~29 chars at 80-col terminal with several rows competing for
column width) with a ``…``, producing a display string that wouldn't
round-trip through downstream commands like ``gimmes position-context``.
The fix sets ``overflow="fold"`` on every Ticker column so the full
ticker is preserved (wrapped to the next line when the terminal is
narrow).

Test strategy: a Rich Table with a single short ticker auto-sizes its
columns and the bug doesn't trigger — so the regression tests below
either (a) build multi-row pressure into the table so Rich is forced to
shrink the Ticker column under the bug case, or (b) inspect the
relevant function's source to confirm ``overflow="fold"`` is wired up.

Strategy (a) covers the two callable formatter functions
(``format_positions`` and ``format_scan_results``). Strategy (b) covers
the inline ``Table`` constructions inside async CLI command bodies
(``trades``, ``candidates``, ``discover``, and the backtest trade log),
where invoking the full CliRunner stack requires mocking the async DB
layer with non-trivial fixture setup.
"""

from __future__ import annotations

import inspect
import re
from io import StringIO

import pytest
from rich.console import Console
from rich.table import Table

LONG_TICKER = "KXJOBLESSCLAIMS-26MAY14-210000"  # 30 chars
SHORTER_TICKER = "KXCPI-26APR-T0.5"
ELLIPSIS = "…"


@pytest.fixture()
def narrow_console(monkeypatch):
    """Patch the formatter's Console to a fixed 80-col buffer with no
    color/terminal effects. The 80-col width matches what Rich sees
    when stdout is captured by ``subprocess.Popen`` (the autonomous
    loop's actual environment)."""
    buf = StringIO()
    console = Console(
        file=buf, width=80, force_terminal=True, color_system=None,
    )
    monkeypatch.setattr("gimmes.reporting.formatter.console", console)
    return buf


def _ticker_column_text(text: str) -> str:
    """Concatenate the first (Ticker) column's content across every
    body row, including fold-continuation rows.

    Rich draws body rows starting with ``│``; the Ticker column lies
    between the leading ``│`` and the next inner ``│``. Continuation
    rows share that structure with whitespace padding in other columns.
    """
    parts: list[str] = []
    for line in text.splitlines():
        if not line.startswith("│"):
            continue
        segments = line.split("│")
        if len(segments) < 3:
            continue
        parts.append(segments[1].strip())
    return "".join(parts)


def _assert_overflow_fold_in_source(func) -> None:
    """Assert the function's source contains an
    ``add_column("Ticker", ..., overflow="fold", ...)`` call.

    Inline-Table CLI commands construct their tables imperatively
    rather than through a shared helper; the most-direct regression
    fence is to verify the source string at the call site. Source
    inspection is brittle in general but is the right tool when the
    alternative is mocking the entire async DB layer just to verify a
    single Rich kwarg.

    Accepts both single- and double-quoted string literals so a
    cosmetic quote-style change doesn't break the test.
    """
    src = inspect.getsource(func)
    # Match: add_column("Ticker", ...) or add_column('Ticker', ...);
    # the overflow kwarg must appear inside the same parenthesized call
    # with either quote style for the value.
    pattern = re.compile(
        r'''add_column\(\s*["']Ticker["'][^)]*?overflow\s*=\s*["']fold["']''',
        re.DOTALL,
    )
    assert pattern.search(src), (
        f"{func.__qualname__} does not declare overflow='fold' on its"
        f" Ticker column. Source excerpt:\n{src[:500]}"
    )


def _real_positions_fixture() -> list[dict]:
    """7-row positions list matching the exact shape from a real
    ``gimmes positions`` run that demonstrates the bug. Under default
    Rich behavior at 80 cols this layout truncates ``LONG_TICKER`` to
    ``KXJOBLESSCLAIMS-26MAY14-21…``; under ``overflow="fold"`` the full
    ticker survives. The test for ``test_default_truncates_at_80_cols``
    below verifies the bug condition holds without the fix.
    """
    return [
        {"ticker": "KXCPICORE-26APR-T0.3", "side": "no", "count": 990,
         "avg_price": 0.50, "market_price": 0.50, "unrealized_pnl": 0.61},
        {"ticker": "KXCPI-26APR-T0.5", "side": "no", "count": 1075,
         "avg_price": 0.46, "market_price": 0.36, "unrealized_pnl": -101.42},
        {"ticker": "KXPCECORE-26APR-T0.3", "side": "no", "count": 705,
         "avg_price": 0.60, "market_price": 0.60, "unrealized_pnl": 0.55},
        {"ticker": "KXCPIYOY-26APR-T3.7", "side": "no", "count": 594,
         "avg_price": 0.67, "market_price": 0.68, "unrealized_pnl": 0.67},
        {"ticker": "KXCPIYOY-26MAY-T4.2", "side": "no", "count": 813,
         "avg_price": 0.61, "market_price": 0.59, "unrealized_pnl": -11.54},
        {"ticker": LONG_TICKER, "side": "no", "count": 324,
         "avg_price": 0.60, "market_price": 0.65, "unrealized_pnl": 14.83},
        {"ticker": "KXCPI-26MAY-T0.6", "side": "no", "count": 273,
         "avg_price": 0.78, "market_price": 0.68, "unrealized_pnl": -28.14},
    ]


class TestFormatPositions:
    def test_long_ticker_under_multirow_pressure_is_not_truncated(
        self, narrow_console: StringIO,
    ) -> None:
        from gimmes.reporting.formatter import format_positions
        format_positions(_real_positions_fixture())
        out = narrow_console.getvalue()
        ticker_col = _ticker_column_text(out)
        assert LONG_TICKER in ticker_col, (
            f"long ticker missing from Ticker column. Got: {ticker_col!r}\n"
            f"Full output:\n{out}"
        )
        assert ELLIPSIS not in ticker_col


class TestFormatScanResults:
    def test_long_ticker_under_multicolumn_pressure_is_not_truncated(
        self, narrow_console: StringIO,
    ) -> None:
        from gimmes.reporting.formatter import format_scan_results
        # ``format_scan_results`` has 8 columns including two with
        # ``max_width`` truncation — at 80 cols this is naturally
        # tight enough that a single 30-char ticker would have
        # truncated pre-fix.
        format_scan_results([{
            "ticker": LONG_TICKER,
            "event_ticker": "KXJOBLESSCLAIMS-26MAY14",
            "title": "Initial claims",
            "price": 0.65,
            "volume_24h": 100,
            "open_interest": 50,
            "score": 80,
        }])
        out = narrow_console.getvalue()
        ticker_col = _ticker_column_text(out)
        assert LONG_TICKER in ticker_col
        assert ELLIPSIS not in ticker_col  # Event/Title may ellipsize;
        # this checks only the Ticker column slice above.


class TestCliInlineTableSites:
    """Source-level fences for the inline ``Table`` constructions
    inside async CLI command bodies. Each call site must declare
    ``overflow="fold"`` on its Ticker column; this is the regression
    contract the fix promises.
    """

    def test_trades_command_uses_overflow_fold(self) -> None:
        from gimmes.cli import trades
        _assert_overflow_fold_in_source(trades)

    def test_candidates_command_uses_overflow_fold(self) -> None:
        from gimmes.cli import candidates
        _assert_overflow_fold_in_source(candidates)

    def test_discover_command_uses_overflow_fold(self) -> None:
        # The ``discover`` CLI command renders a per-category series
        # table. Series tickers are typically short, but the
        # ``overflow="fold"`` invariant must hold so future long
        # category-specific tickers don't reintroduce the bug.
        from gimmes.cli import discover
        _assert_overflow_fold_in_source(discover)

    def test_backtest_report_uses_overflow_fold(self) -> None:
        from gimmes.backtest.report import format_backtest_report
        _assert_overflow_fold_in_source(format_backtest_report)


class TestRichDefaultBehaviorDocumentation:
    """Negative-direction tests: verify that *without* the fix, Rich
    would in fact truncate. These pin the bug's empirical conditions so
    a future "is the fix still needed?" question is answerable from
    the test suite alone.
    """

    def _build_positions_table(self, overflow: str | None) -> str:
        """Render the bug-reproducing positions Table under the given
        overflow setting and return the captured stdout."""
        buf = StringIO()
        console = Console(
            file=buf, width=80, force_terminal=True, color_system=None,
        )
        table = Table(title="Open Positions")
        kwargs: dict = {"style": "cyan"}
        if overflow is not None:
            kwargs["overflow"] = overflow
        table.add_column("Ticker", **kwargs)
        table.add_column("Side")
        table.add_column("Qty", justify="right")
        table.add_column("Avg Price", justify="right")
        table.add_column("Mkt Price", justify="right")
        table.add_column("P&L", justify="right")
        for p in _real_positions_fixture():
            pnl = p["unrealized_pnl"]
            color = "green" if pnl >= 0 else "red"
            table.add_row(
                p["ticker"], p["side"], str(p["count"]),
                f"${p['avg_price']:.2f}", f"${p['market_price']:.2f}",
                f"[{color}]${pnl:,.2f}[/{color}]",
            )
        console.print(table)
        return buf.getvalue()

    def test_default_overflow_truncates_long_ticker_at_80_cols(self) -> None:
        # Pre-fix shape: default ``"ellipsis"`` overflow on the Ticker
        # column. The 30-char ticker MUST be truncated for the rest of
        # this test file's regression fences to be meaningful.
        ticker_col = _ticker_column_text(
            self._build_positions_table(overflow=None),
        )
        assert ELLIPSIS in ticker_col
        assert LONG_TICKER not in ticker_col

    def test_overflow_fold_preserves_long_ticker_at_80_cols(self) -> None:
        # Positive companion: with ``overflow="fold"``, the same data
        # at the same width keeps the long ticker visible.
        ticker_col = _ticker_column_text(
            self._build_positions_table(overflow="fold"),
        )
        assert LONG_TICKER in ticker_col
        assert ELLIPSIS not in ticker_col


class TestScanResultsMarkupEscape:
    @staticmethod
    def _market(title: str) -> dict:
        return {
            "ticker": SHORTER_TICKER,
            "event_ticker": "KXCPI-26APR",
            "title": title,
            "price": 0.65,
            "volume_24h": 100,
            "open_interest": 50,
            "score": 80,
        }

    def test_bracketed_title_cell_survives_markup(
        self, narrow_console: StringIO,
    ) -> None:
        """Market titles from the Kalshi API can carry bracketed
        segments — the Title cell must render them verbatim (#644).
        The fragment fits inside the column's 20-char cap."""
        from gimmes.reporting.formatter import format_scan_results

        format_scan_results([self._market("CPI [prelim] Apr")])
        out = narrow_console.getvalue()
        assert "[prelim]" in out

    def test_bracketed_title_param_survives_markup(
        self, narrow_console: StringIO,
    ) -> None:
        """The title parameter is public — bracketed callers must
        render verbatim (#644)."""
        from gimmes.reporting.formatter import format_scan_results

        format_scan_results([self._market("CPI")], title="Scan [draft]")
        out = narrow_console.getvalue()
        assert "[draft]" in out


class TestPositionsStopColumn:
    """#659 end-to-end: the positions COMMAND plumbs the configured
    stop through to the Stop column and StopGate banner (the review
    found the config plumbing was the only untested link)."""

    def test_command_renders_stop_and_banner(self) -> None:
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock, patch

        from typer.testing import CliRunner

        from gimmes.cli import app
        from gimmes.models.portfolio import Position

        losing = Position(
            ticker="KXJOBLESSCLAIMS-26MAY14-210000", side="no",
            count=100, avg_price=0.55, market_price=0.23,
            cost_basis=100.0, unrealized_pnl=-32.10,
        )
        broker = AsyncMock()
        broker.get_positions = AsyncMock(return_value=[losing])

        mock_client = AsyncMock()
        mock_db = AsyncMock()

        @asynccontextmanager
        async def _ctx(config):
            yield mock_client, broker, mock_db

        cfg = MagicMock()
        cfg.risk.position_stop_loss_pct = 0.15
        from gimmes.models.market import MarketStatus

        market = MagicMock()
        market.midpoint = 0.23
        market.last_price = 0.23
        market.status = MarketStatus.ACTIVE
        # #674: explicit floats — the dead-book check compares > 0,
        # and a bare MagicMock comparison raises TypeError which the
        # try/except would swallow into a spurious STALE.
        market.yes_bid = 0.22
        market.yes_ask = 0.24

        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        with (
            patch("gimmes.cli.load_config", return_value=cfg),
            patch("gimmes.cli.trading_context", _ctx),
            patch(
                "gimmes.kalshi.markets.get_market",
                AsyncMock(return_value=market),
            ),
            patch(
                "gimmes.reporting.formatter.console",
                Console(file=buf, width=80),
            ),
        ):
            result = CliRunner().invoke(app, ["positions"])

        assert result.exit_code == 0, result.output
        out = buf.getvalue()
        # 214% of the $15 gate — banner intact at width 80
        assert (
            "KXJOBLESSCLAIMS-26MAY14-210000 StopGate:"
            " 214% MANDATORY-CLOSE" in out
        )
        assert "Stop" in out
        assert "STALE" not in out  # live quote present — no flag


class TestPositionsStaleAndSuspect:
    """#674: mark failures and dead books flag STALE; live positions
    with prior partial closes flag BASIS-SUSPECT."""

    def _run(self, *, market=None, get_market_effect=None, broker=None,
             live_positions=None):
        from contextlib import ExitStack, asynccontextmanager
        from io import StringIO
        from unittest.mock import AsyncMock, MagicMock, patch

        from rich.console import Console
        from typer.testing import CliRunner

        from gimmes.cli import app

        mock_client = AsyncMock()
        mock_db = AsyncMock()

        @asynccontextmanager
        async def _ctx(config):
            yield mock_client, broker, mock_db

        cfg = MagicMock()
        cfg.risk.position_stop_loss_pct = 0.15

        buf = StringIO()
        patches = [
            patch("gimmes.cli.load_config", return_value=cfg),
            patch("gimmes.cli.trading_context", _ctx),
            patch(
                "gimmes.kalshi.markets.get_market",
                AsyncMock(
                    return_value=market, side_effect=get_market_effect,
                ),
            ),
            patch(
                "gimmes.reporting.formatter.console",
                Console(file=buf, width=80),
            ),
            patch(
                "gimmes.cli.console",
                Console(file=buf, width=80),
            ),
        ]
        if live_positions is not None:
            patches.extend([
                patch(
                    "gimmes.kalshi.portfolio.get_all_positions",
                    AsyncMock(return_value=live_positions),
                ),
                patch(
                    "gimmes.store.queries.sync_positions",
                    AsyncMock(),
                ),
                # #684: the settlements pre-consumption reads the old
                # positions; empty → no removed tickers → no-op.
                patch(
                    "gimmes.store.queries.get_positions",
                    AsyncMock(return_value=[]),
                ),
            ])

        with ExitStack() as stack:
            for pt in patches:
                stack.enter_context(pt)
            result = CliRunner().invoke(app, ["positions"])
        assert result.exit_code == 0, result.output
        return buf.getvalue()

    @staticmethod
    def _market(*, status=None, midpoint=0.23, last_price=0.23,
                yes_bid=0.0, yes_ask=0.0, result=None):
        """A MagicMock market — dead book (bid/ask 0.0) by default,
        ``status`` defaulting to ACTIVE. Quote fields are explicit
        floats: the dead-book check compares > 0, and a bare MagicMock
        comparison raises TypeError which the command's try/except
        would swallow into a spurious STALE."""
        from unittest.mock import MagicMock

        from gimmes.models.market import MarketStatus

        market = MagicMock()
        market.status = MarketStatus.ACTIVE if status is None else status
        market.midpoint = midpoint
        market.last_price = last_price
        market.yes_bid = yes_bid
        market.yes_ask = yes_ask
        market.result = result
        return market

    def _losing_position(self):
        from gimmes.models.portfolio import Position

        return Position(
            ticker="KXCPIYOY-26MAY-T2.5", side="no",
            count=100, avg_price=0.55, market_price=0.23,
            cost_basis=100.0, unrealized_pnl=-32.10,
        )

    def _paper_broker(self):
        from unittest.mock import AsyncMock

        broker = AsyncMock()
        broker.get_positions = AsyncMock(
            return_value=[self._losing_position()],
        )
        return broker

    def test_mark_failure_flags_stale(self) -> None:
        out = self._run(
            broker=self._paper_broker(),
            get_market_effect=RuntimeError("api down"),
        )
        assert "KXCPIYOY-26MAY-T2.5 StopGate: STALE" in out
        # The breach banner from the last-good mark is NOT suppressed.
        assert "MANDATORY-CLOSE" in out

    def test_dead_book_flags_stale(self) -> None:
        out = self._run(broker=self._paper_broker(), market=self._market())
        assert "KXCPIYOY-26MAY-T2.5 StopGate: STALE" in out
        assert "no live quote" in out

    def test_live_partial_close_flags_basis_suspect(self) -> None:
        from gimmes.models.portfolio import Position

        pos = Position(
            ticker="KXCPIYOY-26MAY-T2.5", side="no",
            count=50, avg_price=0.73, market_price=0.23,
            cost_basis=51.0, unrealized_pnl=-25.0,
            realized_pnl=6.0,  # prior partial close
        )
        out = self._run(broker=None, live_positions=[pos])
        assert "KXCPIYOY-26MAY-T2.5 StopGate: BASIS-SUSPECT" in out

    def test_dead_book_mark_still_applied_when_price_known(self) -> None:
        """The flag, not mark suppression, is the fix — a frozen
        last_price > 0 is the best available and must still mark."""
        broker = self._paper_broker()
        self._run(broker=broker, market=self._market())
        broker.mark_to_market.assert_awaited_once_with(
            "KXCPIYOY-26MAY-T2.5", 0.23,
        )

    def test_never_traded_dead_book_does_not_corrupt_mark(self) -> None:
        """#674 review: current_price 0.0 (empty book, no trades ever)
        must NOT mark — a $0 mark would fabricate a total loss and a
        bogus MANDATORY-CLOSE from the staleness itself."""
        broker = self._paper_broker()
        out = self._run(
            broker=broker,
            market=self._market(midpoint=0.0, last_price=0.0),
        )
        broker.mark_to_market.assert_not_awaited()
        assert "KXCPIYOY-26MAY-T2.5 StopGate: STALE" in out

    def test_settling_market_with_empty_book_not_flagged(self) -> None:
        """An empty book is the NORMAL state at settlement — a
        DETERMINED market must not spam STALE (it auto-settles)."""
        from gimmes.models.market import MarketStatus

        market = self._market(
            status=MarketStatus.DETERMINED,
            midpoint=0.0, last_price=1.0, result="no",
        )
        out = self._run(broker=self._paper_broker(), market=market)
        assert "StopGate: STALE" not in out
        assert "no live quote" not in out

    def test_paused_market_with_empty_book_flags_stale(self) -> None:
        """INACTIVE (trading paused) freezes marks the same way ACTIVE
        dead books do — same anchoring risk, same flag."""
        from gimmes.models.market import MarketStatus

        market = self._market(status=MarketStatus.INACTIVE)
        out = self._run(broker=self._paper_broker(), market=market)
        assert "KXCPIYOY-26MAY-T2.5 StopGate: STALE" in out

    def test_one_sided_book_flags_stale(self) -> None:
        """A one-sided book also forces the midpoint fallback — the
        inner condition is AND, not OR."""
        market = self._market(yes_ask=0.24)
        out = self._run(broker=self._paper_broker(), market=market)
        assert "KXCPIYOY-26MAY-T2.5 StopGate: STALE" in out

    def test_live_partial_close_at_loss_also_suspect(self) -> None:
        """realized_pnl != 0, not > 0 — a prior partial close at a
        LOSS corrupts the cumulative cost_basis identically."""
        from gimmes.models.portfolio import Position

        pos = Position(
            ticker="KXCPIYOY-26MAY-T2.5", side="no",
            count=50, avg_price=0.73, market_price=0.23,
            cost_basis=51.0, unrealized_pnl=-25.0,
            realized_pnl=-3.0,
        )
        out = self._run(broker=None, live_positions=[pos])
        assert "KXCPIYOY-26MAY-T2.5 StopGate: BASIS-SUSPECT" in out

    def test_closed_live_position_not_suspect(self) -> None:
        """count = 0 rows (closed positions the API still returns)
        must not be flagged — they hold no capital."""
        from gimmes.models.portfolio import Position

        closed = Position(
            ticker="KXOLD-26APR-T1", side="no",
            count=0, avg_price=0.55, market_price=0.0,
            cost_basis=0.0, unrealized_pnl=0.0,
            realized_pnl=12.0,
        )
        open_clean = Position(
            ticker="KXCPIYOY-26MAY-T2.5", side="no",
            count=100, avg_price=0.55, market_price=0.60,
            cost_basis=55.0, unrealized_pnl=5.0,
            realized_pnl=0.0,
        )
        out = self._run(
            broker=None, live_positions=[closed, open_clean],
        )
        assert "BASIS-SUSPECT" not in out

    def test_live_untouched_position_not_suspect(self) -> None:
        from gimmes.models.portfolio import Position

        pos = Position(
            ticker="KXCPIYOY-26MAY-T2.5", side="no",
            count=100, avg_price=0.55, market_price=0.60,
            cost_basis=55.0, unrealized_pnl=5.0,
            realized_pnl=0.0,
        )
        out = self._run(broker=None, live_positions=[pos])
        assert "BASIS-SUSPECT" not in out


class TestHourlyScanColumn:
    """#722: the Hourly column appears only when at least one scanned
    row is an hourly-series market — output is byte-identical otherwise."""

    @staticmethod
    def _row(**overrides):
        row = {
            "ticker": "KXBTCD-26JUN23H14-T119999.99",
            "event_ticker": "KXBTCD-26JUN23H14",
            "title": "BTC above 119,999.99",
            "price": 0.65,
            "volume_24h": 100,
            "open_interest": 50,
            "score": 80,
        }
        row.update(overrides)
        return row

    def test_hourly_tag_shown_when_any_row_hourly(
        self, narrow_console: StringIO,
    ) -> None:
        from gimmes.reporting.formatter import format_scan_results
        format_scan_results([
            self._row(hourly=True),
            self._row(ticker="KXCPI-26APR-T0.5", event_ticker="", hourly=False),
        ])
        out = narrow_console.getvalue()
        # Exactly one cell carries the tag — the non-hourly row's cell
        # is empty (the header is "Hourly", different case)
        assert out.count("HOURLY") == 1

    def test_no_hourly_column_when_absent(self, narrow_console: StringIO) -> None:
        from gimmes.reporting.formatter import format_scan_results
        format_scan_results([self._row()])  # no "hourly" key at all
        out = narrow_console.getvalue()
        assert "HOURLY" not in out
        assert "Hourly" not in out
