"""Rich console output and markdown formatting for reports."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console
from rich.markup import escape as rich_escape
from rich.panel import Panel
from rich.table import Table

from gimmes.reporting.metrics import PerformanceMetrics
from gimmes.reporting.pnl import PnLSummary

console = Console()


def format_mode_status(mode: str, connected: bool, balance: float | None = None) -> None:
    """Display current mode and connection status."""
    mode_color = "red bold" if mode == "championship" else "green bold"
    status = "[green]Connected[/green]" if connected else "[red]Disconnected[/red]"

    if mode == "championship":
        mode_display = "CHAMPIONSHIP"
    else:
        mode_display = "DRIVING RANGE — PAPER TRADING"

    lines = [
        f"Mode: [{mode_color}]{mode_display}[/{mode_color}]",
        f"Status: {status}",
    ]
    if balance is not None:
        label = "Paper Balance" if mode != "championship" else "Balance"
        lines.append(f"{label}: ${balance:,.2f}")

    if mode == "championship":
        lines.append("\n[red bold]WARNING: REAL MONEY MODE[/red bold]")
    else:
        lines.append("\n[dim]Market data from prod API — orders simulated locally[/dim]")

    console.print(Panel("\n".join(lines), title="GIMMES", border_style="blue"))


def format_pnl_summary(summary: PnLSummary) -> None:
    """Display P&L summary as a Rich table."""
    table = Table(title="P&L Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white", justify="right")

    table.add_row("Total Trades", str(summary.total_trades))
    # #653: total = closed (W+L+scratch) + open — show the split so the
    # table is internally consistent instead of leaving readers to
    # wonder where total - W - L - S trades went.
    closed = (
        summary.winning_trades + summary.losing_trades
        + summary.scratch_trades
    )
    table.add_row("Closed", str(closed))
    table.add_row("Open Positions", str(summary.open_trades))
    table.add_row("Winning", str(summary.winning_trades))
    table.add_row("Losing", str(summary.losing_trades))
    table.add_row("Scratch", str(summary.scratch_trades))
    table.add_row("Win Rate", f"{summary.win_rate:.1%}")
    table.add_row("Gross P&L", f"${summary.gross_pnl:,.2f}")
    table.add_row("Fees", f"${summary.total_fees:,.2f}")

    pnl_color = "green" if summary.net_pnl >= 0 else "red"
    table.add_row("Net P&L", f"[{pnl_color}]${summary.net_pnl:,.2f}[/{pnl_color}]")
    table.add_row("Largest Win", f"${summary.largest_win:,.2f}")
    table.add_row("Largest Loss", f"${summary.largest_loss:,.2f}")

    console.print(table)


def format_performance(metrics: PerformanceMetrics) -> None:
    """Display performance metrics as a Rich table."""
    table = Table(title="Performance Scorecard")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white", justify="right")

    table.add_row("Win Rate", f"{metrics.win_rate:.1%}")
    table.add_row("Avg Edge Predicted", f"{metrics.avg_edge_predicted:.1%}")
    table.add_row("Avg Edge Realized", f"{metrics.avg_edge_realized:.1%}")
    table.add_row("Max Drawdown", f"${metrics.max_drawdown:,.2f}")
    table.add_row("Max Drawdown %", f"{metrics.max_drawdown_pct:.1%}")
    table.add_row("Sharpe Ratio", f"{metrics.sharpe_ratio:.2f}")

    ret_color = "green" if metrics.total_return >= 0 else "red"
    table.add_row("Total Return", f"[{ret_color}]${metrics.total_return:,.2f}[/{ret_color}]")
    table.add_row("Total Return %", f"[{ret_color}]{metrics.total_return_pct:.1%}[/{ret_color}]")

    console.print(table)


def _stop_gate_pct(
    pnl: float, cost_basis: float, stop_loss_pct: float,
) -> int | None:
    """Stop-gate consumption for one position, rounded percent (#659).

    Computed HERE so agents read a number instead of doing arithmetic
    (a known failure mode). Rounded BEFORE thresholding so the display
    and the 200% backstop marker can never disagree (199.6 -> 200 ->
    marker). None for winners and zero-basis rows.
    """
    if cost_basis <= 0 or pnl >= 0:
        return None
    return round(-pnl / (stop_loss_pct * cost_basis) * 100)


def _stop_cell(
    pnl: float, cost_basis: float, ticker: str, stop_loss_pct: float,
) -> tuple[str, str | None]:
    """``Stop`` cell text and optional banner line for one position."""
    pct = _stop_gate_pct(pnl, cost_basis, stop_loss_pct)
    if pct is None:
        if pnl < 0 and cost_basis <= 0:
            # A losing position with no cost basis is a data
            # bug — surface it loudly rather than exempting
            # the position from the backstop with a dash.
            # Banner kept minimal so it stays one line at width
            # 80 for ANY real ticker (#659).
            return (
                "[red]ERR[/red]",
                f"[red bold]{ticker} StopGate: DATA-ERROR[/red bold]",
            )
        return "—", None
    if pct >= 200:
        return (
            f"[red bold]{pct}%[/red bold]",
            f"[red bold]{ticker} StopGate: {pct}%"
            f" MANDATORY-CLOSE[/red bold]",
        )
    if pct >= 100:
        return f"[red]{pct}%[/red]", None
    return f"{pct}%", None


def format_positions(
    positions: list[dict],  # type: ignore[type-arg]
    stop_loss_pct: float | None = None,
) -> None:
    """Display positions as a Rich table.

    With ``stop_loss_pct``, adds a ``Stop`` column showing stop-gate
    consumption per losing position (#659) and prints a
    ``StopGate: N% MANDATORY-CLOSE`` banner line BELOW the table for
    each position at >= 200%. The banner — not the table cell —
    carries the load-bearing literal: at the width-80 non-TTY default
    agents see, table cells wrap and ellipsize long content, but a
    plain sub-80-char line always survives intact. Monitor copies the
    banner into flags; Caddie Master's hard backstop keys on it.
    """
    if stop_loss_pct is not None and stop_loss_pct <= 0:
        raise ValueError(
            f"stop_loss_pct must be positive, got {stop_loss_pct}"
        )
    table = Table(title="Open Positions")
    # ``overflow="fold"`` keeps the full ticker visible by wrapping to
    # the next line when the terminal can't fit it; Rich's default
    # ``"ellipsis"`` produced truncated tickers that broke downstream
    # commands relying on exact-match lookup (issue #567).
    table.add_column("Ticker", style="cyan", overflow="fold")
    table.add_column("Side")
    table.add_column("Qty", justify="right")
    table.add_column("Avg Price", justify="right")
    table.add_column("Mkt Price", justify="right")
    table.add_column("P&L", justify="right")
    if stop_loss_pct is not None:
        table.add_column("Stop", justify="right")

    banners: list[str] = []
    for p in positions:
        pnl = p.get("unrealized_pnl", 0)
        pnl_color = "green" if pnl >= 0 else "red"
        row = [
            p.get("ticker", ""),
            p.get("side", ""),
            str(p.get("count", 0)),
            f"${p.get('avg_price', 0):.2f}",
            f"${p.get('market_price', 0):.2f}",
            f"[{pnl_color}]${pnl:,.2f}[/{pnl_color}]",
        ]
        if stop_loss_pct is not None:
            cell, banner = _stop_cell(
                pnl, p.get("cost_basis", 0), p.get("ticker", ""),
                stop_loss_pct,
            )
            row.append(cell)
            if banner is not None:
                banners.append(banner)
        table.add_row(*row)

    console.print(table)
    for banner in banners:
        console.print(banner)


def format_scan_results(markets: list[dict], title: str = "Scan Results") -> None:  # type: ignore[type-arg]
    """Display scanned markets as a Rich table."""
    # Count siblings per event for the annotation
    from collections import Counter

    event_counts: Counter[str] = Counter(
        m.get("event_ticker", "") for m in markets
        if m.get("event_ticker")
    )

    has_side = any(m.get("side") for m in markets)

    table = Table(title=rich_escape(title))  # #644: title param is open to callers
    table.add_column("Ticker", style="cyan", overflow="fold")
    if has_side:
        table.add_column("Side", style="bold")
    table.add_column("Event", style="dim", max_width=25)
    table.add_column("Title", max_width=20)
    table.add_column("Price", justify="right")
    table.add_column("Vol 24h", justify="right")
    table.add_column("OI", justify="right")
    table.add_column("Score", justify="right")

    for m in markets:
        evt = m.get("event_ticker", "")
        count = event_counts.get(evt, 0)
        evt_label = f"{evt} ({count})" if count > 1 else evt
        row: list[str] = [m.get("ticker", "")]
        if has_side:
            row.append(m.get("side", "").upper())
        row.extend([
            evt_label,
            rich_escape(m.get("title", "")[:20]),  # #644
            f"${m.get('price', 0):.2f}",
            str(m.get("volume_24h", 0)),
            str(m.get("open_interest", 0)),
            f"{m.get('score', 0):.0f}",
        ])
        table.add_row(*row)

    console.print(table)


def format_kv_table(title: str, rows: list[tuple[str, str]]) -> Table:
    """Build a two-column Rich table for key-value display.

    The title is markup-escaped here — callers must NOT pre-escape it
    (double-escaping renders a literal backslash). Row VALUES are still
    markup-parsed: wrap external text in rich_escape() at the call
    site, or pass deliberate markup like color tags (#641/#644). If a
    styled title is ever genuinely needed, add an escape hatch then —
    today every caller passes plain text.
    """
    table = Table(title=rich_escape(title), show_header=False)
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="white", justify="right")
    for key, value in rows:
        table.add_row(key, value)
    return table


def format_local_timestamp(raw: str, *, date_only: bool = False) -> str:
    """Convert a UTC database timestamp to a local-timezone display string.

    Handles both ``YYYY-MM-DD HH:MM:SS`` (SQLite ``datetime('now')``) and ISO
    8601 strings with a ``+00:00`` suffix.  Returns ``--`` for falsy input and
    falls back to a raw truncation on parse errors.
    """
    if not raw:
        return "--"
    try:
        s = str(raw).strip()
        if "T" in s or "+" in s[10:]:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        local_dt = dt.astimezone()
        if date_only:
            return local_dt.strftime("%Y-%m-%d")
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError):
        return str(raw)[:19] if raw else "--"


def pnl_to_markdown(summary: PnLSummary) -> str:
    """Format P&L summary as markdown."""
    sign = "+" if summary.net_pnl >= 0 else ""
    return f"""## P&L Summary

| Metric | Value |
|--------|-------|
| Total Trades | {summary.total_trades} |
| Win Rate | {summary.win_rate:.1%} |
| Net P&L | {sign}${summary.net_pnl:,.2f} |
| Largest Win | ${summary.largest_win:,.2f} |
| Largest Loss | ${summary.largest_loss:,.2f} |
"""
