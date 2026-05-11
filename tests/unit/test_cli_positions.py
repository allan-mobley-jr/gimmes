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
