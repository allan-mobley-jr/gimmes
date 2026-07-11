"""Backtest result reporting — Rich tables and JSON output."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from gimmes.backtest.engine import BacktestResult
from gimmes.reporting.metrics import (
    calculate_max_drawdown,
    calculate_sharpe_from_curve,
)


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
    # #654: time-aware Sharpe on the timestamped curve — the starting
    # balance is prepended at the configured start date so the span
    # (and therefore the annualization frequency) is real.
    start_ts = datetime.combine(
        result.config.start_date, time.min, tzinfo=UTC,
    ).isoformat()
    sharpe = calculate_sharpe_from_curve(
        [(start_ts, starting), *result.equity_curve],
    )

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
    exit_parts = []
    if cfg.take_profit_pct is not None:
        exit_parts.append(f"TP {cfg.take_profit_pct:.0%}")
    if cfg.stop_loss_pct is not None:
        exit_parts.append(f"SL {cfg.stop_loss_pct:.0%}")
    header_lines = [
        f"Period: {cfg.start_date} to {cfg.end_date}",
        f"Starting balance: ${cfg.starting_balance:,.2f}",
        f"Price range: {cfg.gimmes_config.strategy.min_market_price:.2f}"
        f" – {cfg.gimmes_config.strategy.max_market_price:.2f}",
        f"Gimme threshold: {cfg.gimmes_config.strategy.gimme_threshold:.0f}",
        f"Assumed edge: {cfg.assumed_edge:.0%}",
        "Fill model: "
        + ("taker (pays the ask)" if cfg.taker_fill else "maker (midpoint)"),
        "Exits: "
        + (" / ".join(exit_parts) if exit_parts else "hold to settlement"),
    ]
    console.print(Panel(
        "\n".join(header_lines), title="Backtest Config", border_style="blue",
    ))

    # --- Funnel ---
    funnel = Table(title="Market Funnel", show_header=False, box=None)
    funnel.add_column("Label", style="cyan")
    funnel.add_column("Count", justify="right")
    funnel.add_row("Settled markets fetched", str(result.markets_scanned))
    usable_views = (
        result.markets_scanned - result.skipped_no_candle
        - result.skipped_one_sided - result.fetch_failures
    )
    funnel.add_row("Usable entry-day views", str(usable_views))
    funnel.add_row("Passed entry-day filters", str(result.markets_passed_filter))
    funnel.add_row("Scored above threshold", str(result.markets_scored))
    funnel.add_row("Traded", str(result.markets_traded))
    if result.skipped_concentration > 0:
        funnel.add_row(
            "Skipped (concentration)",
            str(result.skipped_concentration),
        )
    if result.skipped_balance > 0:
        funnel.add_row(
            "Skipped (balance)",
            str(result.skipped_balance),
        )
    if result.skipped_no_candle > 0:
        funnel.add_row(
            "Skipped (no entry-day candle history)",
            str(result.skipped_no_candle),
        )
    if result.skipped_one_sided > 0:
        funnel.add_row(
            "Skipped (one-sided/empty entry-day quote)",
            str(result.skipped_one_sided),
        )
    if result.fetch_failures > 0:
        funnel.add_row(
            "Candle fetch FAILURES (API problem)",
            str(result.fetch_failures),
        )
    if result.stale_candles > 0:
        # PRICED, not skipped — these views carry a quote up to 3
        # days old (they may still be filtered out downstream; #682
        # visibility, policy deferred).
        funnel.add_row(
            "Priced from stale candle (>1 day old)",
            str(result.stale_candles),
        )
    if result.skipped_zero_sizing > 0:
        funnel.add_row(
            "Skipped (zero position size)",
            str(result.skipped_zero_sizing),
        )
    if result.skipped_entry_gates > 0:
        funnel.add_row(
            "Skipped (entry-day prob/edge gates)",
            str(result.skipped_entry_gates),
        )
    if result.exited_take_profit > 0:
        funnel.add_row(
            "Exited (take-profit)", str(result.exited_take_profit),
        )
    if result.exited_stop_loss > 0:
        funnel.add_row(
            "Exited (stop-loss)", str(result.exited_stop_loss),
        )
    console.print(funnel)
    if result.fetch_failures > 0:
        console.print(
            f"[red]Warning: {result.fetch_failures} candle fetches"
            f" FAILED — the skip counts may reflect an API problem,"
            f" not data sparsity (#666). The #655 endpoint regression"
            f" produced exactly this signature.[/red]"
        )
    if result.walk_fetch_failures > 0:
        console.print(
            f"[yellow]Warning: {result.walk_fetch_failures} post-entry"
            f" walk fetches FAILED — those positions silently held to"
            f" settlement, so the TP/SL exit numbers above understate"
            f" what the exit rule would have done (#714).[/yellow]"
        )
    if result.markets_passed_filter == 0 and usable_views > 0:
        console.print(
            f"[yellow]Note: all {usable_views} entry-day views failed"
            f" the scanner filters — check min_volume /"
            f" min_open_interest / days-to-resolution against"
            f" ENTRY-DAY values (they are typically lower than"
            f" settlement-time values) (#666).[/yellow]"
        )
    candle_skips = (
        result.skipped_no_candle + result.skipped_one_sided
        + result.fetch_failures
    )
    if (
        result.markets_scanned > 0
        and candle_skips > 0.5 * result.markets_scanned
    ):
        console.print(
            f"[yellow]Caution: the selection replay skipped"
            f" {candle_skips} of {result.markets_scanned} scanned"
            f" markets for missing, unusable, or fetch-failed entry"
            f" candles —"
            f" results cover a subset of the scanned universe, and"
            f" one-sided skips can under-represent near-certain"
            f" late-life contracts (#666).[/yellow]"
        )
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
        trades_table.add_column("Ticker", style="cyan", overflow="fold")
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
            "exit_reason": t.exit_reason,
            "exit_price": (
                round(t.exit_price, 4) if t.exit_price is not None else None
            ),
            "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        }
        for t in result.trades
    ]

    return {
        "config": {
            "start_date": str(result.config.start_date),
            "end_date": str(result.config.end_date),
            "starting_balance": result.config.starting_balance,
            "assumed_edge": result.config.assumed_edge,
            "taker_fill": result.config.taker_fill,
            "take_profit_pct": result.config.take_profit_pct,
            "stop_loss_pct": result.config.stop_loss_pct,
        },
        "funnel": {
            "markets_scanned": result.markets_scanned,
            "markets_passed_filter": result.markets_passed_filter,
            "markets_scored": result.markets_scored,
            "markets_traded": result.markets_traded,
            "skipped_no_candle": result.skipped_no_candle,
            "skipped_one_sided": result.skipped_one_sided,
            "fetch_failures": result.fetch_failures,
            "skipped_entry_gates": result.skipped_entry_gates,
            "stale_candles": result.stale_candles,
            "skipped_zero_sizing": result.skipped_zero_sizing,
            "truncated_chunks": result.truncated_chunks,
            "exited_take_profit": result.exited_take_profit,
            "exited_stop_loss": result.exited_stop_loss,
            "walk_fetch_failures": result.walk_fetch_failures,
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
