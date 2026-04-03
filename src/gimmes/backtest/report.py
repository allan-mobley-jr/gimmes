"""Backtest result reporting — Rich tables and JSON output."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from gimmes.backtest.engine import BacktestResult
from gimmes.reporting.metrics import calculate_max_drawdown, calculate_sharpe


@dataclass
class _Summary:
    wins: int
    losses: int
    total: int
    win_rate: float
    net_pnl: float
    total_fees: float
    roi: float
    max_dd: float
    max_dd_pct: float
    sharpe: float


def _compute_summary(result: BacktestResult) -> _Summary:
    """Compute aggregate metrics from a backtest result."""
    wins = sum(1 for t in result.trades if t.pnl > 0)
    total = len(result.trades)
    losses = total - wins
    win_rate = wins / total if total > 0 else 0.0
    net_pnl = sum(t.pnl for t in result.trades)
    total_fees = sum(t.fees for t in result.trades)
    starting = result.config.starting_balance
    roi = net_pnl / starting if starting > 0 else 0.0

    equity_values = [starting] + [e for _, e in result.equity_curve]
    max_dd, max_dd_pct = calculate_max_drawdown(equity_values)
    returns = []
    for i in range(1, len(equity_values)):
        if equity_values[i - 1] > 0:
            returns.append(
                (equity_values[i] - equity_values[i - 1])
                / equity_values[i - 1]
            )
    sharpe = calculate_sharpe(returns)

    return _Summary(
        wins=wins, losses=losses, total=total, win_rate=win_rate,
        net_pnl=net_pnl, total_fees=total_fees, roi=roi,
        max_dd=max_dd, max_dd_pct=max_dd_pct, sharpe=sharpe,
    )


def _pnl_color(value: float) -> str:
    if value > 0:
        return "green"
    if value < 0:
        return "red"
    return "white"


def format_backtest_report(result: BacktestResult, console: Console) -> None:
    """Print a Rich-formatted backtest report to the console."""
    cfg = result.config
    s = _compute_summary(result)

    # --- Header ---
    header_lines = [
        f"Period: {cfg.start_date} to {cfg.end_date}",
        f"Starting balance: ${cfg.starting_balance:,.2f}",
        f"Price range: {cfg.gimmes_config.strategy.min_market_price:.2f}"
        f" – {cfg.gimmes_config.strategy.max_market_price:.2f}",
        f"Gimme threshold: {cfg.gimmes_config.strategy.gimme_threshold:.0f}",
        f"Assumed edge: {cfg.assumed_edge:.0%}",
    ]
    console.print(Panel(
        "\n".join(header_lines), title="Backtest Config", border_style="blue",
    ))

    # --- Funnel ---
    funnel = Table(title="Market Funnel", show_header=False, box=None)
    funnel.add_column("Label", style="cyan")
    funnel.add_column("Count", justify="right")
    funnel.add_row("Settled markets fetched", str(result.markets_scanned))
    funnel.add_row("Passed scanner filters", str(result.markets_passed_filter))
    funnel.add_row("Scored above threshold", str(result.markets_scored))
    funnel.add_row("Traded", str(result.markets_traded))
    console.print(funnel)
    if result.truncated_chunks:
        console.print(
            f"[yellow]Warning: pagination limit reached for "
            f"{len(result.truncated_chunks)} chunk(s): "
            f"{', '.join(result.truncated_chunks)}. "
            f"Results may be incomplete.[/yellow]"
        )
    console.print()

    # --- Trade log ---
    if result.trades:
        trades_table = Table(title="Trade Log")
        trades_table.add_column("Ticker", style="cyan", max_width=30)
        trades_table.add_column("Side", style="white")
        trades_table.add_column("Qty", justify="right")
        trades_table.add_column("Entry", justify="right")
        trades_table.add_column("Fees", justify="right")
        trades_table.add_column("Result", style="white")
        trades_table.add_column("P&L", justify="right")

        display_trades = result.trades[:50]
        for t in display_trades:
            color = _pnl_color(t.pnl)
            win = "[green]WIN[/green]" if t.pnl > 0 else "[red]LOSS[/red]"
            trades_table.add_row(
                t.ticker, t.side.upper(), str(t.count),
                f"${t.entry_price:.2f}", f"${t.fees:.2f}", win,
                f"[{color}]${t.pnl:+,.2f}[/{color}]",
            )
        if len(result.trades) > 50:
            trades_table.add_row(
                f"... {len(result.trades) - 50} more (use --json)",
                "", "", "", "", "", "",
            )
        console.print(trades_table)
        console.print()

    # --- Summary metrics ---
    summary = Table(title="Performance Summary")
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", justify="right")
    summary.add_row("Total Trades", str(s.total))
    summary.add_row("Wins / Losses", f"{s.wins} / {s.losses}")
    summary.add_row("Win Rate", f"{s.win_rate:.1%}")
    summary.add_row("Total Fees", f"${s.total_fees:,.2f}")

    pnl_c = _pnl_color(s.net_pnl)
    summary.add_row("Net P&L", f"[{pnl_c}]${s.net_pnl:+,.2f}[/{pnl_c}]")
    roi_c = _pnl_color(s.roi)
    summary.add_row("ROI", f"[{roi_c}]{s.roi:+.1%}[/{roi_c}]")
    summary.add_row(
        "Max Drawdown", f"${s.max_dd:,.2f} ({s.max_dd_pct:.1%})",
    )
    summary.add_row("Sharpe Ratio", f"{s.sharpe:.2f}")
    console.print(summary)
    console.print()

    # --- Bottom line ---
    fc = _pnl_color(result.final_balance - cfg.starting_balance)
    console.print(
        f"[bold]${cfg.starting_balance:,.2f}[/bold] → "
        f"[bold {fc}]${result.final_balance:,.2f}[/bold {fc}]"
        f"  ([{fc}]{s.roi:+.1%}[/{fc}])"
    )


def backtest_result_to_json(result: BacktestResult) -> dict:  # type: ignore[type-arg]
    """Convert a BacktestResult to a JSON-serializable dict."""
    s = _compute_summary(result)
    trades = [
        {
            "ticker": t.ticker,
            "title": t.title,
            "side": t.side,
            "count": t.count,
            "entry_price": t.entry_price,
            "cost_basis": round(t.cost_basis, 4),
            "fees": round(t.fees, 4),
            "result": t.result,
            "payout": round(t.payout, 4),
            "pnl": round(t.pnl, 4),
            "entry_time": t.entry_time.isoformat() if t.entry_time else None,
            "settle_time": t.settle_time.isoformat() if t.settle_time else None,
        }
        for t in result.trades
    ]

    return {
        "config": {
            "start_date": str(result.config.start_date),
            "end_date": str(result.config.end_date),
            "starting_balance": result.config.starting_balance,
            "assumed_edge": result.config.assumed_edge,
        },
        "funnel": {
            "markets_scanned": result.markets_scanned,
            "markets_passed_filter": result.markets_passed_filter,
            "markets_scored": result.markets_scored,
            "markets_traded": result.markets_traded,
            "truncated_chunks": result.truncated_chunks,
        },
        "summary": {
            "total_trades": s.total,
            "wins": s.wins,
            "losses": s.losses,
            "win_rate": round(s.win_rate, 4),
            "total_fees": round(s.total_fees, 2),
            "net_pnl": round(s.net_pnl, 2),
            "roi": round(s.roi, 4),
            "final_balance": round(result.final_balance, 2),
            "max_drawdown": round(s.max_dd, 2),
            "max_drawdown_pct": round(s.max_dd_pct, 4),
            "sharpe": round(s.sharpe, 2),
        },
        "trades": trades,
        "equity_curve": [
            {"timestamp": ts, "equity": round(eq, 2)}
            for ts, eq in result.equity_curve
        ],
    }
