"""GIMMES CLI — Typer-based command interface for Kalshi trading."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import click
import typer
from rich.console import Console

from gimmes.config import GimmesConfig, load_config

app = typer.Typer(
    name="gimmes",
    help="GIMMES — We only play the gimmes. Kalshi prediction market trading CLI.",
    no_args_is_help=True,
)
console = Console()


def _run(coro):  # type: ignore[no-untyped-def]
    """Run an async coroutine from sync CLI context with error handling."""
    import logging

    logger = logging.getLogger("gimmes.cli")
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        raise typer.Exit(130)
    except (ConnectionError, ValueError, RuntimeError) as e:
        logger.debug("CLI error", exc_info=True)
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


def _championship_warning(config) -> None:  # type: ignore[no-untyped-def]
    """Show warning when in Championship mode."""
    if config.is_championship:
        console.print(
            "\n[red bold]⚠  CHAMPIONSHIP MODE — REAL MONEY ⚠[/red bold]\n",
            highlight=False,
        )


@asynccontextmanager
async def trading_context(config: GimmesConfig):
    """Yields (client, broker, db). broker is None in championship mode.

    Both modes use the prod API client for real market data.
    In driving range, a PaperBroker handles portfolio operations locally.
    Both modes open a Database for position syncing and snapshots.
    """
    from gimmes.kalshi.client import KalshiClient
    from gimmes.store.database import Database

    async with KalshiClient(config) as client:
        async with Database(config.db_path) as db:
            if config.is_championship:
                yield client, None, db
            else:
                from gimmes.paper.broker import PaperBroker

                broker = PaperBroker(db, config.paper)
                await broker.initialize()
                yield client, broker, db


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def mode() -> None:
    """Show current mode and connection status."""
    config = load_config()
    _championship_warning(config)

    async def _check() -> None:
        from gimmes.reporting.formatter import format_mode_status

        connected = False
        balance = None

        if config.api_key and config.private_key_path.exists():
            try:
                async with trading_context(config) as (client, broker, _db):
                    if broker:
                        balance = await broker.get_balance()
                    else:
                        from gimmes.kalshi.portfolio import get_balance
                        balance = await get_balance(client)
                    connected = True
            except Exception as exc:
                import logging
                logging.getLogger("gimmes").debug("mode: connection check failed: %s", exc)

        format_mode_status(config.mode.value, connected, balance)

    _run(_check())


@app.command()
def scan(
    top_n: int = typer.Option(20, "--top", "-n", help="Number of top candidates to show"),
    series: list[str] = typer.Option(
        None, "--series", "-s",
        help="Override series tickers to scan (e.g. -s KXCPI -s KXGDP)",
    ),
    all_markets: bool = typer.Option(
        False, "--all", help="Scan all markets (ignore series filter)",
    ),
) -> None:
    """Scan markets for gimme candidates (Scout pipeline)."""
    config = load_config()
    _championship_warning(config)

    async def _scan() -> None:
        from gimmes.kalshi.client import KalshiClient
        from gimmes.kalshi.markets import list_all_markets
        from gimmes.reporting.formatter import format_scan_results
        from gimmes.strategy.scanner import filter_markets
        from gimmes.strategy.scorer import quick_score

        series_tickers = series or config.scanner.series

        async with KalshiClient(config) as client:
            console.print("[cyan]Scanning markets...[/cyan]")

            if series_tickers and not all_markets:
                # Fetch markets only for curated series — fast and focused
                markets = []
                for st in series_tickers:
                    batch = await list_all_markets(client, series_ticker=st)
                    markets.extend(batch)
                console.print(f"Fetched {len(markets)} markets from {len(series_tickers)} series")
            else:
                markets = await list_all_markets(client)
                console.print(f"Fetched {len(markets)} markets (all)")

            candidates = filter_markets(markets, config)
            console.print(f"Filtered to {len(candidates)} candidates")

            scored = []
            for m in candidates:
                qs = quick_score(m, config)
                scored.append({
                    "ticker": m.ticker,
                    "title": m.title,
                    "price": m.midpoint or m.last_price,
                    "volume_24h": m.volume_24h or m.volume,
                    "open_interest": m.open_interest,
                    "score": qs,
                })

            scored.sort(key=lambda r: r["score"], reverse=True)
            format_scan_results(scored[:top_n])

    _run(_scan())


@app.command()
def score(
    ticker: str = typer.Argument(..., help="Market ticker to score"),
) -> None:
    """Score a specific market for gimme potential."""
    config = load_config()

    async def _score() -> None:
        from gimmes.kalshi.client import KalshiClient
        from gimmes.kalshi.markets import get_market, get_orderbook
        from gimmes.strategy.scorer import quick_score

        async with KalshiClient(config) as client:
            market = await get_market(client, ticker)
            orderbook = await get_orderbook(client, ticker)
            qs = quick_score(market, config)

            console.print(f"\n[bold]{market.title}[/bold]")
            console.print(f"Ticker: {market.ticker}")
            console.print(f"Price: ${market.midpoint or market.last_price:.2f}")
            console.print(f"Volume 24h: {market.volume_24h}")
            console.print(f"Open Interest: {market.open_interest}")
            console.print(f"Spread: ${market.spread:.2f}")
            console.print(f"Best YES Bid: {orderbook.best_yes_bid}")
            console.print(f"Best YES Ask: {orderbook.best_yes_ask}")
            console.print(f"Quick Score: [bold]{qs:.0f}[/bold]/100")

    _run(_score())


@app.command()
def size(
    ticker: str = typer.Argument(..., help="Market ticker"),
    probability: float = typer.Option(..., "--prob", "-p", help="Estimated true probability"),
) -> None:
    """Calculate position size for a market."""
    config = load_config()

    async def _size() -> None:
        from gimmes.kalshi.markets import get_market
        from gimmes.strategy.fees import edge_after_fees, fee_for_order
        from gimmes.strategy.kelly import kelly_fraction, position_size

        async with trading_context(config) as (client, broker, _db):
            market = await get_market(client, ticker)

            if broker:
                balance = await broker.get_balance()
            else:
                from gimmes.kalshi.portfolio import get_balance
                balance = await get_balance(client)

            price = market.midpoint or market.last_price

            kf = kelly_fraction(price, probability, fraction=config.sizing.kelly_fraction)
            contracts = position_size(
                balance, price, probability,
                fraction=config.sizing.kelly_fraction,
                max_position_pct=config.sizing.max_position_pct,
            )
            fee = fee_for_order(contracts, price, is_taker=False)
            edge = edge_after_fees(price, probability)
            cost = contracts * price + fee

            console.print(f"\n[bold]Position Sizing: {ticker}[/bold]")
            console.print(f"Market Price: ${price:.2f}")
            console.print(f"True Probability: {probability:.1%}")
            console.print(f"Edge After Fees: {edge:.1%}")
            console.print(f"Kelly Fraction: {kf:.4f}")
            console.print(f"Bankroll: ${balance:,.2f}")
            console.print(f"Contracts: [bold]{contracts}[/bold]")
            console.print(f"Est. Cost: ${cost:,.2f}")
            console.print(f"Est. Fee: ${fee:,.2f}")

    _run(_size())


@app.command()
def validate(
    ticker: str = typer.Argument(..., help="Market ticker"),
    probability: float = typer.Option(..., "--prob", "-p", help="Estimated true probability"),
    dollars: float = typer.Option(0, "--dollars", "-d", help="Trade size in dollars (0=auto-size)"),
) -> None:
    """Pre-trade validation for a market."""
    config = load_config()
    _championship_warning(config)

    async def _validate() -> None:
        from gimmes.kalshi.markets import get_market
        from gimmes.risk.validator import validate_trade
        from gimmes.store.queries import get_daily_pnl
        from gimmes.strategy.kelly import position_size

        async with trading_context(config) as (client, broker, db):
            market = await get_market(client, ticker)

            if broker:
                balance = await broker.get_balance()
                positions = await broker.get_positions()
            else:
                from gimmes.kalshi.portfolio import get_all_positions, get_balance
                from gimmes.store.queries import sync_positions
                balance = await get_balance(client)
                positions = await get_all_positions(client)
                await sync_positions(db, positions)

            price = market.midpoint or market.last_price
            if dollars <= 0:
                contracts = position_size(
                    balance, price, probability,
                    fraction=config.sizing.kelly_fraction,
                    max_position_pct=config.sizing.max_position_pct,
                )
                trade_dollars = contracts * price
            else:
                trade_dollars = dollars

            # Get daily P&L from local DB — MUST succeed for safe validation
            try:
                daily_pnl = await get_daily_pnl(db)
            except Exception as exc:
                console.print(
                    f"[red bold]VALIDATION FAILED: Could not query daily P&L — {exc}[/red bold]"
                )
                console.print(
                    "[red]Refusing to validate with unknown P&L "
                    "(daily loss limit may be breached)[/red]"
                )
                raise typer.Exit(1)

            existing_tickers = [p.ticker for p in positions]
            result = validate_trade(
                market, trade_dollars, probability, balance,
                daily_pnl, len(positions), existing_tickers, config,
            )

            console.print(f"\n[bold]Validation: {ticker}[/bold]")
            if result.approved:
                console.print(f"[green bold]{result.summary}[/green bold]")
            else:
                console.print(f"[red bold]{result.summary}[/red bold]")

            for check in result.checks:
                console.print(f"  [green]✓[/green] {check}")
            for fail in result.failures:
                console.print(f"  [red]✗[/red] {fail}")

    _run(_validate())


@app.command()
def order(
    ticker: str = typer.Argument(..., help="Market ticker"),
    action: str = typer.Option(
        "buy", "--action", "-a", help="Order action (buy/sell)",
        click_type=click.Choice(["buy", "sell"], case_sensitive=False),
    ),
    side: str = typer.Option("yes", "--side", "-s", help="Order side (yes/no)"),
    count: int = typer.Option(0, "--count", "-c", help="Number of contracts (0=auto-size)"),
    price: int = typer.Option(
        0, "--price", help="Limit price in cents, e.g. 70 for $0.70 (0=market)"
    ),
    probability: float = typer.Option(
        0, "--prob", "-p", help="True probability (buy only: auto-sizing and edge check)",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation (for autonomous mode)",
    ),
    force: bool = typer.Option(
        False, "--force", help="Override validation failures (use with caution)",
    ),
) -> None:
    """Place an order on Kalshi (runs pre-trade validation first)."""
    config = load_config()
    _championship_warning(config)

    if config.is_championship and not yes:
        confirm = typer.confirm("You are in CHAMPIONSHIP mode. Place a REAL MONEY order?")
        if not confirm:
            raise typer.Abort()

    async def _order() -> None:
        from gimmes.kalshi.markets import get_market, get_orderbook
        from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide
        from gimmes.risk.validator import validate_trade
        from gimmes.store.queries import get_daily_pnl
        from gimmes.strategy.kelly import position_size

        async with trading_context(config) as (client, broker, db):
            market = await get_market(client, ticker)
            mkt_price = market.midpoint or market.last_price

            # Get balance and positions for both sizing and validation
            if broker:
                balance = await broker.get_balance()
                positions = await broker.get_positions()
            else:
                from gimmes.kalshi.portfolio import get_all_positions, get_balance
                from gimmes.store.queries import sync_positions
                balance = await get_balance(client)
                positions = await get_all_positions(client)
                await sync_positions(db, positions)

            order_action = OrderAction(action.lower())
            is_buy = order_action == OrderAction.BUY

            if is_buy and count <= 0 and probability > 0:
                final_count = position_size(
                    balance, mkt_price, probability,
                    fraction=config.sizing.kelly_fraction,
                    max_position_pct=config.sizing.max_position_pct,
                )
            else:
                final_count = count

            if final_count <= 0:
                hint = (
                    " Provide --count N or --prob P for auto-sizing."
                    if is_buy else " Provide --count N."
                )
                console.print(f"[red]No contracts to order (count=0).{hint}[/red]")
                return

            final_price = price / 100.0 if price > 0 else mkt_price
            trade_dollars = final_count * final_price

            # --- Sell validation: check position exists and count ---
            if not is_buy:
                matching = [
                    p for p in positions
                    if p.ticker == ticker and p.side == side
                ]
                if not matching:
                    console.print(
                        f"[red]No {side.upper()} position in"
                        f" {ticker} to sell[/red]"
                    )
                    return
                held = matching[0].count
                if final_count > held:
                    console.print(
                        f"[red]Cannot sell {final_count} contracts"
                        f" — only {held} held[/red]"
                    )
                    return

            # --- Pre-trade validation (buy orders only) ---
            if is_buy:
                try:
                    daily_pnl = await get_daily_pnl(db)
                except Exception as exc:
                    if force:
                        daily_pnl = 0.0
                        console.print(
                            f"[yellow]Warning: Could not query daily"
                            f" P&L ({exc}) — using 0.0 (--force)"
                            f"[/yellow]"
                        )
                    else:
                        console.print(
                            f"[red bold]Cannot query daily P&L:"
                            f" {exc}[/red bold]"
                        )
                        console.print(
                            "[red]Refusing to order with unknown"
                            " P&L (daily loss limit may be"
                            " breached). Use --force to"
                            " override.[/red]"
                        )
                        return

                true_prob = probability if probability > 0 else None
                existing_tickers = [p.ticker for p in positions]
                is_taker = (
                    config.orders.preferred_order_type != "maker"
                )
                validation = validate_trade(
                    market, trade_dollars, true_prob, balance,
                    daily_pnl, len(positions), existing_tickers,
                    config, is_taker=is_taker,
                )

                if not validation.approved:
                    console.print(
                        f"\n[red bold]{validation.summary}"
                        f"[/red bold]"
                    )
                    for fail in validation.failures:
                        console.print(f"  [red]✗[/red] {fail}")
                    if force:
                        console.print(
                            "[yellow bold]--force: Overriding"
                            " validation failures!"
                            "[/yellow bold]"
                        )
                    else:
                        console.print(
                            "[dim]Use --force to override"
                            " (not recommended)[/dim]"
                        )
                        return
                else:
                    for check in validation.checks:
                        console.print(
                            f"  [green]✓[/green] {check}"
                        )

            # --- Place the order ---
            params = CreateOrderParams(
                ticker=ticker,
                action=order_action,
                side=OrderSide(side),
                count=final_count,
                yes_price=final_price if side == "yes" else None,
                no_price=final_price if side == "no" else None,
                post_only=(config.orders.preferred_order_type == "maker"),
            )

            msg = (
                f"Placing order: {action.upper()} {final_count}x"
                f" {ticker} {side.upper()} @ {int(round(final_price * 100))}¢"
            )
            console.print(msg)

            if broker:
                orderbook = await get_orderbook(client, ticker)
                result = await broker.create_order(params, orderbook)
                label = "[yellow]PAPER[/yellow] "
            else:
                from gimmes.kalshi.orders import create_order
                from gimmes.kalshi.portfolio import get_all_positions as refresh_pos
                from gimmes.store.queries import sync_positions
                result = await create_order(client, params)
                label = ""
                # Sync positions to local DB after championship order
                pos_list = await refresh_pos(client)
                await sync_positions(db, pos_list)

            console.print(
                f"[green]{label}Order placed:[/green] {result.order_id}"
                f" (status: {result.status})"
            )

    _run(_order())


@app.command()
def cancel(
    order_id: str = typer.Argument(..., help="Order ID to cancel"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation",
    ),
) -> None:
    """Cancel a resting order."""
    config = load_config()

    if config.is_championship and not yes:
        confirm = typer.confirm(
            f"Cancel order {order_id} in CHAMPIONSHIP mode?"
        )
        if not confirm:
            raise typer.Abort()

    async def _cancel() -> None:
        async with trading_context(config) as (client, broker, _db):
            if broker:
                await broker.cancel_order(order_id)
            else:
                from gimmes.kalshi.orders import cancel_order
                await cancel_order(client, order_id)
            console.print(f"[green]Canceled order {order_id}[/green]")

    _run(_cancel())


@app.command()
def trades(
    ticker: str | None = typer.Option(
        None, "--ticker", "-t", help="Filter by ticker",
    ),
    action: str | None = typer.Option(
        None, "--action", "-a", help="Filter by action (open/close/skip)",
    ),
    limit: int = typer.Option(
        20, "--limit", "-n", help="Number of records to show",
    ),
) -> None:
    """List individual trade records from the database."""

    async def _trades() -> None:
        from rich.table import Table

        from gimmes.store.database import Database
        from gimmes.store.queries import get_trades

        async with Database() as db:
            records = await get_trades(
                db, ticker=ticker, action=action, limit=limit,
            )

        if not records:
            console.print("[dim]No trade records found[/dim]")
            return

        table = Table(title=f"Trade History (last {limit})")
        table.add_column("Ticker")
        table.add_column("Action")
        table.add_column("Side")
        table.add_column("Count", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("Edge", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("Timestamp")

        for t in records:
            table.add_row(
                str(t.get("ticker", "")),
                str(t.get("action", "")),
                str(t.get("side", "")),
                str(t.get("count", 0)),
                f"${t.get('price', 0):.2f}",
                f"{t.get('edge', 0):.1%}",
                f"{t.get('gimme_score', 0):.0f}",
                str(t.get("timestamp", ""))[:19],
            )

        console.print(table)

    _run(_trades())


@app.command()
def positions() -> None:
    """List open positions."""
    config = load_config()

    async def _positions() -> None:
        from gimmes.kalshi.markets import get_market
        from gimmes.models.market import MarketStatus
        from gimmes.reporting.formatter import format_positions

        async with trading_context(config) as (client, broker, db):
            if broker:
                pos_list = await broker.get_positions()
                # Mark-to-market + auto-settle with real prices
                for pos in pos_list:
                    try:
                        market = await get_market(client, pos.ticker)
                        current_price = market.midpoint or market.last_price
                        await broker.mark_to_market(pos.ticker, current_price)
                        # Auto-settle if market resolved
                        if market.status in (MarketStatus.DETERMINED, MarketStatus.FINALIZED):
                            await broker.settle(pos.ticker, market.result)
                    except Exception as exc:
                        console.print(
                            f"[yellow]Warning: could not update {pos.ticker}: {exc}[/yellow]"
                        )
                # Re-fetch after mark-to-market
                pos_list = await broker.get_positions()
            else:
                from gimmes.kalshi.portfolio import get_all_positions
                from gimmes.store.queries import sync_positions
                pos_list = await get_all_positions(client)
                await sync_positions(db, pos_list)

            if not pos_list:
                console.print("[dim]No open positions[/dim]")
                return
            format_positions([p.model_dump() for p in pos_list])

    _run(_positions())


@app.command(name="risk-check")
def risk_check() -> None:
    """Check risk limits and daily P&L."""
    config = load_config()

    async def _check() -> None:
        from gimmes.risk.limits import check_daily_loss, check_position_count
        from gimmes.store.queries import get_daily_pnl

        async with trading_context(config) as (client, broker, db):
            if broker:
                balance = await broker.get_balance()
                pos = await broker.get_positions()
            else:
                from gimmes.kalshi.portfolio import get_all_positions, get_balance
                from gimmes.store.queries import sync_positions
                balance = await get_balance(client)
                pos = await get_all_positions(client)
                await sync_positions(db, pos)

            try:
                daily_pnl = await get_daily_pnl(db)
            except Exception as exc:
                console.print(
                    f"[red bold]RISK CHECK FAILED: Could not query daily P&L — {exc}[/red bold]"
                )
                console.print(
                    "[red]Cannot verify risk limits with unknown P&L[/red]"
                )
                raise typer.Exit(1)

            console.print("\n[bold]Risk Check[/bold]")
            console.print(f"Balance: ${balance:,.2f}")
            console.print(f"Open Positions: {len(pos)}/{config.risk.max_open_positions}")
            console.print(f"Daily P&L: ${daily_pnl:,.2f}")

            loss = check_daily_loss(daily_pnl, balance, config)
            count = check_position_count(len(pos), config)

            for check, label in [(loss, "Daily Loss"), (count, "Position Count")]:
                if check.passed:
                    console.print(f"  [green]✓[/green] {label}: OK")
                else:
                    console.print(f"  [red]✗[/red] {label}: {check.reason}")

    _run(_check())


@app.command()
def report() -> None:
    """Generate performance scorecard."""
    config = load_config()

    async def _report() -> None:
        from gimmes.reporting.formatter import format_pnl_summary
        from gimmes.reporting.pnl import PnLSummary, calculate_pnl
        from gimmes.store.database import Database
        from gimmes.store.queries import get_trades

        try:
            async with Database(config.db_path) as db:
                trades = await get_trades(db, limit=1000)
                summary = calculate_pnl(trades)
                format_pnl_summary(summary)
        except Exception as exc:
            import logging
            logging.getLogger("gimmes").warning("report: failed to load trades: %s", exc)
            format_pnl_summary(PnLSummary())
            console.print("[dim]No trade data yet[/dim]")

    _run(_report())


@app.command(name="market-info")
def market_info(
    ticker: str = typer.Argument(..., help="Market ticker"),
) -> None:
    """Show detailed market information."""
    config = load_config()

    async def _info() -> None:
        from gimmes.kalshi.client import KalshiClient
        from gimmes.kalshi.markets import get_market, get_orderbook
        from gimmes.risk.settlement import scan_settlement_rules

        async with KalshiClient(config) as client:
            market = await get_market(client, ticker)
            orderbook = await get_orderbook(client, ticker)
            settlement = scan_settlement_rules(market.rules_primary)

            console.print(f"\n[bold]{market.title}[/bold]")
            console.print(f"Ticker: {market.ticker}")
            console.print(f"Event: {market.event_ticker}")
            console.print(f"Status: {market.status.value}")
            console.print(f"\nYES Bid: ${market.yes_bid:.2f}  |  YES Ask: ${market.yes_ask:.2f}")
            console.print(f"Last Price: ${market.last_price:.2f}  |  Spread: ${market.spread:.2f}")
            console.print(f"Volume: {market.volume}  |  24h Vol: {market.volume_24h}")
            console.print(f"Open Interest: {market.open_interest}")
            console.print(f"Close Time: {market.close_time}")

            console.print("\nOrderbook:")
            console.print(f"  Best YES Bid: {orderbook.best_yes_bid}")
            console.print(f"  Best YES Ask: {orderbook.best_yes_ask}")
            console.print(f"  Depth (YES bids): {len(orderbook.yes_bids)} levels")

            risk_color = {"low": "green", "medium": "yellow", "high": "red"}.get(
                settlement.risk_level, "white"
            )
            console.print(
                f"\nSettlement Risk: [{risk_color}]{settlement.summary}[/{risk_color}]"
            )

    _run(_info())


@app.command(name="log-trade")
def log_trade(
    ticker: str = typer.Argument(..., help="Market ticker"),
    action: str = typer.Option(..., "--action", "-a", help="open/close/skip"),
    side: str = typer.Option("yes", "--side", "-s"),
    count: int = typer.Option(0, "--count", "-c"),
    price_val: float = typer.Option(0, "--price"),
    prob: float = typer.Option(0, "--prob", "-p"),
    score_val: float = typer.Option(0, "--score"),
    rationale: str = typer.Option("", "--rationale", "-r"),
    agent: str = typer.Option("manual", "--agent"),
) -> None:
    """Log a trade decision to the database."""
    config = load_config()

    async def _log() -> None:
        from gimmes.models.trade import TradeDecision
        from gimmes.store.database import Database
        from gimmes.store.queries import insert_trade

        trade = TradeDecision(
            ticker=ticker,
            action=TradeDecision.Action(action),
            side=side,
            count=count,
            price=price_val,
            model_probability=prob,
            gimme_score=score_val,
            edge=prob - price_val if prob > 0 else 0,
            rationale=rationale,
            agent=agent,
        )

        async with Database(config.db_path) as db:
            row_id = await insert_trade(db, trade)
            console.print(f"[green]Logged trade #{row_id}: {action} {ticker}[/green]")

    _run(_log())


@app.command(name="log-outcome")
def log_outcome(
    ticker: str = typer.Argument(..., help="Market ticker"),
    outcome: str = typer.Option(..., "--outcome", "-o", help="Resolution outcome (yes/no)"),
) -> None:
    """Record a market's resolution outcome for trades on that ticker."""
    if outcome not in ("yes", "no"):
        console.print(f"[red]Invalid outcome '{outcome}': must be 'yes' or 'no'[/red]")
        raise typer.Exit(1)

    config = load_config()

    async def _log() -> None:
        from gimmes.store.database import Database
        from gimmes.store.queries import update_trade_outcome

        async with Database(config.db_path) as db:
            updated = await update_trade_outcome(db, ticker, outcome)
            if updated:
                console.print(
                    f"[green]Recorded outcome '{outcome}' for"
                    f" {updated} trade(s) on {ticker}[/green]"
                )
            else:
                console.print(
                    f"[yellow]No trades found for {ticker}"
                    " (or already recorded)[/yellow]"
                )

    _run(_log())


@app.command(name="log-activity")
def log_activity(
    cycle: int = typer.Option(0, "--cycle", "-c", help="Cycle number"),
    agent: str = typer.Option("", "--agent", "-a", help="Agent name"),
    phase: str = typer.Option("", "--phase", help="Phase (start/complete/error)"),
    message: str = typer.Option("", "--message", "-m", help="Activity message"),
    details: str = typer.Option("", "--details", "-d", help="Additional details"),
) -> None:
    """Log agent activity to the activity_log table."""
    config = load_config()

    async def _log() -> None:
        from gimmes.store.database import Database
        from gimmes.store.queries import insert_activity

        async with Database(config.db_path) as db:
            row_id = await insert_activity(
                db, cycle=cycle, agent=agent, phase=phase,
                message=message, details=details,
            )
            console.print(f"[green]Logged activity #{row_id}[/green]")

    _run(_log())


@app.command(name="log-error")
def log_error(
    severity: str = typer.Option(
        "error", "--severity", "-s", help="Severity level"
    ),
    category: str = typer.Option("api_error", "--category", help="Error category"),
    code: str = typer.Option("", "--code", help="Error code identifier"),
    component: str = typer.Option("", "--component", help="Component that raised the error"),
    agent: str = typer.Option("", "--agent", "-a", help="Agent name"),
    cycle: int = typer.Option(0, "--cycle", "-c", help="Cycle number"),
    message: str = typer.Option("", "--message", "-m", help="Error message"),
    stack_trace: str = typer.Option("", "--stack-trace", help="Stack trace"),
    context: str = typer.Option("{}", "--context", help="JSON context blob"),
) -> None:
    """Log a structured error to the error_log table."""
    config = load_config()

    async def _log() -> None:
        from gimmes.models.error import ErrorCategory, ErrorLogEntry, ErrorSeverity
        from gimmes.store.database import Database
        from gimmes.store.queries import insert_error

        entry = ErrorLogEntry(
            severity=ErrorSeverity(severity),
            category=ErrorCategory(category),
            error_code=code,
            component=component,
            agent=agent,
            cycle=cycle,
            message=message,
            stack_trace=stack_trace,
            context=context,
        )

        async with Database(config.db_path) as db:
            row_id = await insert_error(db, entry)
            console.print(
                f"[red]Logged error #{row_id}:[/red] [{severity}] {category} — {message}"
            )

    _run(_log())


@app.command(name="errors")
def errors(
    severity: str | None = typer.Option(None, "--severity", "-s", help="Filter by severity"),
    category: str | None = typer.Option(None, "--category", help="Filter by category"),
    unresolved: bool = typer.Option(False, "--unresolved", "-u", help="Only unresolved errors"),
    summary: bool = typer.Option(False, "--summary", help="Aggregate summary view"),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of entries to show"),
) -> None:
    """View error logs with optional filters."""
    config = load_config()

    async def _errors() -> None:
        from rich.table import Table

        from gimmes.store.database import Database
        from gimmes.store.queries import get_error_summary, get_errors

        async with Database(config.db_path) as db:
            if summary:
                rows = await get_error_summary(db)
                if not rows:
                    console.print("[dim]No errors logged[/dim]")
                    return

                table = Table(title="Error Summary")
                table.add_column("Severity", style="bold")
                table.add_column("Category")
                table.add_column("Total", justify="right")
                table.add_column("Unresolved", justify="right")

                for row in rows:
                    sev = row["severity"]
                    sev_color = {
                        "critical": "red bold",
                        "error": "red",
                        "warning": "yellow",
                        "info": "blue",
                        "debug": "dim",
                    }.get(sev, "white")
                    table.add_row(
                        f"[{sev_color}]{sev}[/{sev_color}]",
                        row["category"],
                        str(row["count"]),
                        str(row["unresolved"]),
                    )
                console.print(table)
            else:
                rows = await get_errors(
                    db, severity=severity, category=category,
                    unresolved=unresolved, limit=limit,
                )
                if not rows:
                    console.print("[dim]No errors found[/dim]")
                    return

                table = Table(title="Error Log")
                table.add_column("ID", justify="right")
                table.add_column("Time")
                table.add_column("Severity", style="bold")
                table.add_column("Category")
                table.add_column("Code")
                table.add_column("Message", max_width=50)
                table.add_column("Resolved")

                for row in rows:
                    sev = row["severity"]
                    sev_color = {
                        "critical": "red bold",
                        "error": "red",
                        "warning": "yellow",
                        "info": "blue",
                        "debug": "dim",
                    }.get(sev, "white")
                    resolved = "[green]Yes[/green]" if row["resolved"] else "[red]No[/red]"
                    table.add_row(
                        str(row["id"]),
                        row["timestamp"],
                        f"[{sev_color}]{sev}[/{sev_color}]",
                        row["category"],
                        row.get("error_code", ""),
                        row["message"][:50],
                        resolved,
                    )
                console.print(table)

    _run(_errors())


@app.command(name="resolve-error")
def resolve_error_cmd(
    error_id: int = typer.Argument(..., help="Error ID to mark as resolved"),
    issue_url: str = typer.Option("", "--issue-url", "-u", help="GitHub issue URL"),
) -> None:
    """Mark an error log entry as resolved."""
    config = load_config()

    async def _resolve() -> None:
        from gimmes.store.database import Database
        from gimmes.store.queries import resolve_error

        async with Database(config.db_path) as db:
            await resolve_error(db, error_id, issue_url)
            console.print(f"[green]Resolved error #{error_id}[/green]")
            if issue_url:
                console.print(f"  Linked to: {issue_url}")

    _run(_resolve())


@app.command()
def lesson(
    analysis: str | None = typer.Option(
        None, "--analysis", "-a", help="Analysis type to run",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show without persisting",
    ),
) -> None:
    """Run strategy analysis and show parameter recommendations."""
    config = load_config()

    async def _lesson() -> None:
        from rich.table import Table

        from gimmes.store.database import Database
        from gimmes.store.queries import get_recommendations, get_trades, insert_recommendation
        from gimmes.strategy.advisor import run_all_analyses

        async with Database(config.db_path) as db:
            all_trades = await get_trades(db, limit=1000)
            # Candidates not yet used (scoring correlation needs #20)
            candidates: list[dict] = []  # type: ignore[type-arg]

            recs = run_all_analyses(all_trades, candidates, config)

            if not recs:
                console.print(
                    "[dim]No recommendations — insufficient data"
                    " or current parameters are optimal[/dim]"
                )
                return

            # Filter to specific analysis type if requested
            if analysis:
                recs = [r for r in recs if analysis in r.analysis_type.value]
                if not recs:
                    console.print(f"[dim]No recommendations from {analysis} analysis[/dim]")
                    return

            # Print The Lesson report
            console.print("\n[bold]═══════════════════════════════════════════════[/bold]")
            console.print("[bold]                  THE LESSON[/bold]")
            console.print("[bold]═══════════════════════════════════════════════[/bold]\n")

            console.print("[bold]Recommendations[/bold]")
            console.print("─" * 46)

            for rec in recs:
                color = {"high": "red bold", "medium": "yellow", "low": "dim"}.get(
                    rec.confidence.value, "white"
                )
                console.print(
                    f"[{color}][{rec.confidence.value.upper()}][/{color}] "
                    f"{rec.parameter_path}: {rec.current_value} → {rec.recommended_value}"
                )
                console.print(f"  {rec.rationale}\n")

            # Persist recommendations (skip if pending rec already exists for same parameter)
            if not dry_run:
                existing = await get_recommendations(db, status="pending", limit=100)
                existing_params = {r["parameter_path"] for r in existing}
                new_recs = [r for r in recs if r.parameter_path not in existing_params]
                for rec in new_recs:
                    await insert_recommendation(db, rec)
                if new_recs:
                    console.print(
                        f"[green]Saved {len(new_recs)}"
                        " recommendation(s) to database[/green]"
                    )
                skipped = len(recs) - len(new_recs)
                if skipped:
                    console.print(
                        f"[dim]Skipped {skipped} duplicate(s)"
                        " (pending recs already exist)[/dim]"
                    )

            # Show past recommendations
            past = await get_recommendations(db, status="pending", limit=10)
            if past:
                console.print("\n[bold]Past Pending Recommendations[/bold]")
                console.print("─" * 46)
                table = Table()
                table.add_column("ID", justify="right")
                table.add_column("Parameter")
                table.add_column("Change")
                table.add_column("Confidence")
                table.add_column("Date")
                for row in past:
                    table.add_row(
                        str(row["id"]),
                        row["parameter_path"],
                        f"{row['current_value']} → {row['recommended_value']}",
                        row["confidence"],
                        row["timestamp"][:10],
                    )
                console.print(table)

    _run(_lesson())


@app.command()
def recommendations(
    status: str | None = typer.Option(
        None, "--status", "-s", help="Filter by status",
    ),
    parameter: str | None = typer.Option(
        None, "--parameter", "-p", help="Filter by parameter path",
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of entries to show"),
) -> None:
    """View past strategy recommendations."""
    config = load_config()

    async def _recs() -> None:
        from rich.table import Table

        from gimmes.store.database import Database
        from gimmes.store.queries import get_recommendations

        async with Database(config.db_path) as db:
            rows = await get_recommendations(db, status=status, parameter=parameter, limit=limit)
            if not rows:
                console.print("[dim]No recommendations found[/dim]")
                return

            table = Table(title="Strategy Recommendations")
            table.add_column("ID", justify="right")
            table.add_column("Date")
            table.add_column("Parameter")
            table.add_column("Current")
            table.add_column("Recommended")
            table.add_column("Confidence", style="bold")
            table.add_column("Analysis")
            table.add_column("Status")

            for row in rows:
                conf = row["confidence"]
                conf_color = {"high": "red", "medium": "yellow", "low": "dim"}.get(conf, "white")
                status_color = {
                    "pending": "yellow", "implemented": "green",
                    "rejected": "red", "superseded": "dim",
                }.get(row["status"], "white")
                table.add_row(
                    str(row["id"]),
                    row["timestamp"][:10],
                    row["parameter_path"],
                    row["current_value"],
                    row["recommended_value"],
                    f"[{conf_color}]{conf}[/{conf_color}]",
                    row["analysis_type"],
                    f"[{status_color}]{row['status']}[/{status_color}]",
                )
            console.print(table)

    _run(_recs())


@app.command()
def tune() -> None:
    """Interactively apply pending strategy recommendations to gimmes.toml."""
    config = load_config()

    async def _tune() -> None:
        from gimmes.config import DEFAULT_CONFIG_PATH
        from gimmes.store.database import Database
        from gimmes.store.queries import get_recommendations, update_recommendation_status

        async with Database(config.db_path) as db:
            rows = await get_recommendations(db, status="pending", limit=50)
            if not rows:
                console.print("[dim]No pending recommendations[/dim]")
                return

            applied = 0
            for row in rows:
                conf = row["confidence"]
                conf_color = {"high": "red", "medium": "yellow", "low": "dim"}.get(conf, "white")
                console.print(
                    f"\n[{conf_color}][{conf.upper()}][/{conf_color}] "
                    f"[cyan]{row['parameter_path']}[/cyan]: "
                    f"{row['current_value']} → [bold]{row['recommended_value']}[/bold]"
                )
                console.print(f"  {row['rationale']}")
                console.print(f"  [dim]Analysis: {row['analysis_type']}[/dim]")

                answer = typer.prompt("  Apply? [y/n/q]", default="n").strip().lower()
                if answer == "q":
                    break
                if answer == "y":
                    _apply_toml_change(
                        DEFAULT_CONFIG_PATH,
                        row["parameter_path"],
                        row["recommended_value"],
                    )
                    await update_recommendation_status(db, row["id"], "implemented")
                    console.print("  [green]Applied and marked as implemented[/green]")
                    applied += 1
                else:
                    reject = typer.confirm("  Mark as rejected?", default=False)
                    if reject:
                        await update_recommendation_status(db, row["id"], "rejected")
                        console.print("  [dim]Marked as rejected[/dim]")

            if applied:
                console.print(
                    f"\n[green]Applied {applied} change(s)"
                    f" to {DEFAULT_CONFIG_PATH}[/green]"
                )
                console.print("[dim]Restart the trading loop for changes to take effect[/dim]")

    _run(_tune())


def _apply_toml_change(
    toml_path: Path, parameter_path: str, new_value: str
) -> None:
    """Update a single value in gimmes.toml using tomlkit for safe editing.

    Supports arbitrary nesting depth (e.g., "scoring.weights.edge_size").
    Preserves comments, formatting, and creates missing sections as needed.
    Writes to a temp file first, validates the result, then replaces the original.
    """
    import shutil
    import tempfile
    import tomllib

    import tomlkit

    path = Path(toml_path)
    if path.exists():
        doc = tomlkit.parse(path.read_text())
    else:
        doc = tomlkit.document()

    # Convert value to the appropriate type
    try:
        if "." in new_value:
            typed_value: object = float(new_value)
        else:
            typed_value = int(new_value)
    except ValueError:
        if new_value.lower() in ("true", "false"):
            typed_value = new_value.lower() == "true"
        else:
            typed_value = new_value

    # Set the value using dotted path, creating tables as needed
    parts = parameter_path.split(".")
    current: dict = doc  # type: ignore[assignment]
    for part in parts[:-1]:
        if part not in current:
            current[part] = tomlkit.table()
        elif not isinstance(current[part], dict):
            raise ValueError(
                f"Cannot set '{parameter_path}': "
                f"'{part}' is a scalar, not a table"
            )
        current = current[part]
    current[parts[-1]] = typed_value

    # Write to temp file, validate, then replace
    new_text = tomlkit.dumps(doc)
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Generated invalid TOML: {e}") from e

    # Ensure parent directory exists
    path.parent.mkdir(parents=True, exist_ok=True)

    # Backup original if it exists
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)

    # Atomic write via temp file
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".toml")
    try:
        with open(fd, "w") as f:
            f.write(new_text)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@app.command()
def discover(
    category: str = typer.Argument(
        ..., help="Category to explore (Economics, Politics, Financials, etc.)",
    ),
) -> None:
    """Discover series tickers in a Kalshi category."""
    config = load_config()

    async def _discover() -> None:
        from rich.table import Table

        from gimmes.kalshi.client import KalshiClient
        from gimmes.kalshi.markets import list_series

        async with KalshiClient(config) as client:
            series_list = await list_series(client, category=category)
            console.print(f"Found {len(series_list)} series in [bold]{category}[/bold]")

            table = Table(title=f"{category} Series")
            table.add_column("Ticker", style="cyan")
            table.add_column("Title")

            for s in sorted(series_list, key=lambda x: x.get("ticker", "")):
                table.add_row(s.get("ticker", ""), s.get("title", ""))

            console.print(table)

    _run(_discover())


@app.command()
def config(
    section: str | None = typer.Option(
        None, "--section", "-s",
        help="Jump to a specific section (paper, strategy, sizing, risk, orders, scanner, scoring)",
    ),
) -> None:
    """Interactive configuration wizard — walk through every setting."""
    from gimmes.config_wizard import run_config_wizard

    run_config_wizard(section_filter=section)


@app.command()
def init() -> None:
    """Set up gimmes for first-time use (config files, API credentials)."""
    from gimmes.init import run_init

    run_init()


# ---------------------------------------------------------------------------
# Clubhouse dashboard
# ---------------------------------------------------------------------------


@app.command()
def clubhouse(
    port: int = typer.Option(1919, "--port", "-p", help="Port number"),
) -> None:
    """Launch the Clubhouse web dashboard (standalone)."""
    from gimmes.clubhouse.server import run_standalone

    config = load_config()
    run_standalone(port=port, db_path=config.db_path)


# ---------------------------------------------------------------------------
# Autonomous loop commands
# ---------------------------------------------------------------------------


def _autonomous_loop(
    mode: str,
    *,
    max_cycles: int = 0,
    pause_seconds: int = 30,
    no_dashboard: bool = False,
    max_consecutive_failures: int = 5,
) -> None:
    """Run the caddy-shack orchestrator skill via claude -p in a loop.

    Each cycle invokes one complete trading pipeline (Monitor → Scout →
    Caddie → Closer → Scorecard). On exit or crash, the loop re-invokes
    and the orchestrator picks up where it left off by reading SQLite state.

    A circuit breaker halts the loop after ``max_consecutive_failures``
    successive non-zero exits to prevent runaway retries when the system
    is in a broken state (e.g., expired credentials, API outage).
    """
    import os
    import shutil
    import subprocess
    import time
    from pathlib import Path

    claude_path = shutil.which("claude")
    if not claude_path:
        console.print("[red]Error: 'claude' CLI not found. Install Claude Code first.[/red]")
        raise typer.Exit(1)

    project_root = Path(__file__).resolve().parent.parent.parent
    config = load_config()

    # Set mode in process env so the in-process dashboard reads the correct mode
    os.environ["GIMMES_MODE"] = mode

    env = os.environ.copy()

    # Auto-start Clubhouse dashboard
    if not no_dashboard:
        from gimmes.clubhouse.server import start_background

        port = start_background(
            db_path=config.db_path, pause_seconds=pause_seconds,
        )
        if port:
            console.print(
                f"[green]Clubhouse dashboard:[/green] http://127.0.0.1:{port}"
            )
        else:
            console.print(
                "[yellow]Could not start Clubhouse dashboard (port unavailable)[/yellow]"
            )

    mode_label = "DRIVING RANGE" if mode == "driving_range" else "CHAMPIONSHIP"
    console.print(f"\n[bold]{mode_label}[/bold] — autonomous trading loop started")
    console.print(f"Pause between cycles: {pause_seconds}s")
    if max_cycles > 0:
        console.print(f"Max cycles: {max_cycles}")
    console.print("Press Ctrl+C to stop\n")

    cycle = 0
    consecutive_failures = 0
    try:
        while max_cycles == 0 or cycle < max_cycles:
            cycle += 1
            console.print(f"[cyan]--- Cycle {cycle} ---[/cyan]")

            env["GIMMES_CYCLE"] = str(cycle)
            result = subprocess.run(
                [
                    claude_path, "-p", "/caddy-shack",
                    "--allowedTools",
                    "Bash,Read,Glob,Grep,Agent,WebSearch,WebFetch",
                ],
                env=env,
                cwd=project_root,
                check=False,
            )
            if result.returncode != 0:
                consecutive_failures += 1
                console.print(
                    f"[yellow]Cycle {cycle} exited with code"
                    f" {result.returncode}"
                    f" (failure {consecutive_failures}"
                    f"/{max_consecutive_failures})[/yellow]"
                )
                if (max_consecutive_failures > 0
                        and consecutive_failures >= max_consecutive_failures):
                    console.print(
                        f"[red bold]Circuit breaker tripped:"
                        f" {max_consecutive_failures} consecutive"
                        f" failures. Halting autonomous loop.[/red bold]"
                    )
                    break
            else:
                consecutive_failures = 0

            if max_cycles > 0 and cycle >= max_cycles:
                break

            console.print(f"[dim]Next cycle in {pause_seconds}s...[/dim]")
            time.sleep(pause_seconds)
    except KeyboardInterrupt:
        pass

    console.print("\n[yellow]Autonomous loop stopped.[/yellow]")


@app.command(name="driving_range")
def driving_range(
    cycles: int = typer.Option(
        0, "--cycles", "-n", min=0, help="Max cycles to run (0=unlimited)",
    ),
    pause: int = typer.Option(0, "--pause", min=0, help="Seconds between cycles"),
    no_dashboard: bool = typer.Option(
        False, "--no-dashboard", help="Disable auto-start of Clubhouse dashboard",
    ),
) -> None:
    """Start autonomous trading loop in Driving Range mode (paper trading)."""
    _autonomous_loop("driving_range", max_cycles=cycles, pause_seconds=pause,
                     no_dashboard=no_dashboard)


@app.command(name="championship")
def championship(
    cycles: int = typer.Option(
        0, "--cycles", "-n", min=0, help="Max cycles to run (0=unlimited)",
    ),
    pause: int = typer.Option(0, "--pause", min=0, help="Seconds between cycles"),
    no_dashboard: bool = typer.Option(
        False, "--no-dashboard", help="Disable auto-start of Clubhouse dashboard",
    ),
) -> None:
    """Start autonomous trading loop in Championship mode (REAL MONEY)."""
    console.print("\n[red bold]⚠  CHAMPIONSHIP MODE — REAL MONEY ⚠[/red bold]")
    console.print(
        "This will trade with real money on Kalshi autonomously.\n"
        "The system will scan markets, research candidates, and execute trades\n"
        "without asking for confirmation on each order.\n"
    )
    if not typer.confirm("Are you sure you want to start autonomous trading with real money?"):
        raise typer.Abort()
    _autonomous_loop("championship", max_cycles=cycles, pause_seconds=pause,
                     no_dashboard=no_dashboard)


if __name__ == "__main__":
    app()
