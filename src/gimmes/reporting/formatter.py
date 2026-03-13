"""Rich console output and markdown formatting for reports."""

from __future__ import annotations

from rich.console import Console
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


def format_positions(positions: list[dict]) -> None:  # type: ignore[type-arg]
    """Display positions as a Rich table."""
    table = Table(title="Open Positions")
    table.add_column("Ticker", style="cyan")
    table.add_column("Side")
    table.add_column("Qty", justify="right")
    table.add_column("Avg Price", justify="right")
    table.add_column("Mkt Price", justify="right")
    table.add_column("P&L", justify="right")

    for p in positions:
        pnl = p.get("unrealized_pnl", 0)
        pnl_color = "green" if pnl >= 0 else "red"
        table.add_row(
            p.get("ticker", ""),
            p.get("side", ""),
            str(p.get("count", 0)),
            f"${p.get('avg_price', 0):.2f}",
            f"${p.get('market_price', 0):.2f}",
            f"[{pnl_color}]${pnl:,.2f}[/{pnl_color}]",
        )

    console.print(table)


def format_scan_results(markets: list[dict], title: str = "Scan Results") -> None:  # type: ignore[type-arg]
    """Display scanned markets as a Rich table."""
    table = Table(title=title)
    table.add_column("Ticker", style="cyan")
    table.add_column("Title", max_width=40)
    table.add_column("Price", justify="right")
    table.add_column("Vol 24h", justify="right")
    table.add_column("OI", justify="right")
    table.add_column("Score", justify="right")

    for m in markets:
        table.add_row(
            m.get("ticker", ""),
            m.get("title", "")[:40],
            f"${m.get('price', 0):.2f}",
            str(m.get("volume_24h", 0)),
            str(m.get("open_interest", 0)),
            f"{m.get('score', 0):.0f}",
        )

    console.print(table)


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
