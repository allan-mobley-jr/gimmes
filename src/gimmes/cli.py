"""GIMMES CLI — Typer-based command interface for Kalshi trading."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

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
    """Run an async coroutine from sync CLI context."""
    return asyncio.run(coro)


def _championship_warning(config) -> None:  # type: ignore[no-untyped-def]
    """Show warning when in Championship mode."""
    if config.is_championship:
        console.print(
            "\n[red bold]⚠  CHAMPIONSHIP MODE — REAL MONEY ⚠[/red bold]\n",
            highlight=False,
        )


@asynccontextmanager
async def trading_context(config: GimmesConfig):
    """Yields (client, broker). broker is None in championship mode.

    Both modes use the prod API client for real market data.
    In driving range, a PaperBroker handles portfolio operations locally.
    """
    from gimmes.kalshi.client import KalshiClient

    async with KalshiClient(config) as client:
        if config.is_championship:
            yield client, None
        else:
            from gimmes.paper.broker import PaperBroker
            from gimmes.store.database import Database

            async with Database(config.db_path) as db:
                broker = PaperBroker(db, config.paper)
                await broker.initialize()
                yield client, broker


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
                async with trading_context(config) as (client, broker):
                    if broker:
                        balance = await broker.get_balance()
                    else:
                        from gimmes.kalshi.portfolio import get_balance
                        balance = await get_balance(client)
                    connected = True
            except Exception:
                pass

        format_mode_status(config.mode.value, connected, balance)

    _run(_check())


@app.command()
def scan(
    top_n: int = typer.Option(20, "--top", "-n", help="Number of top candidates to show"),
    series: list[str] = typer.Option(
        None, "--series", "-s",
        help="Override series tickers to scan (e.g. -s KXCPI -s KXGDP)",
    ),
    all_markets: bool = typer.Option(False, "--all", help="Scan all markets (ignore series filter)"),
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

            results = []
            for m in candidates[:top_n]:
                qs = quick_score(m, config)
                results.append({
                    "ticker": m.ticker,
                    "title": m.title,
                    "price": m.midpoint or m.last_price,
                    "volume_24h": m.volume_24h or m.volume,
                    "open_interest": m.open_interest,
                    "score": qs,
                })

            format_scan_results(results)

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

        async with trading_context(config) as (client, broker):
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
        from gimmes.store.database import Database
        from gimmes.store.queries import get_daily_pnl
        from gimmes.strategy.kelly import position_size

        async with trading_context(config) as (client, broker):
            market = await get_market(client, ticker)

            if broker:
                balance = await broker.get_balance()
                positions = await broker.get_positions()
            else:
                from gimmes.kalshi.portfolio import get_all_positions, get_balance
                balance = await get_balance(client)
                positions = await get_all_positions(client)

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

            # Get daily P&L from local DB
            daily_pnl = 0.0
            try:
                async with Database(config.db_path) as db:
                    daily_pnl = await get_daily_pnl(db)
            except Exception:
                pass

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
    side: str = typer.Option("yes", "--side", "-s", help="Order side (yes/no)"),
    count: int = typer.Option(0, "--count", "-c", help="Number of contracts (0=auto-size)"),
    price: int = typer.Option(0, "--price", help="Price in cents (0=use market price)"),
    probability: float = typer.Option(0, "--prob", "-p", help="True probability (for auto-sizing)"),
) -> None:
    """Place an order on Kalshi."""
    config = load_config()
    _championship_warning(config)

    if config.is_championship:
        confirm = typer.confirm("You are in CHAMPIONSHIP mode. Place a REAL MONEY order?")
        if not confirm:
            raise typer.Abort()

    async def _order() -> None:
        from gimmes.kalshi.markets import get_market, get_orderbook
        from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide
        from gimmes.strategy.kelly import position_size

        async with trading_context(config) as (client, broker):
            market = await get_market(client, ticker)
            mkt_price = market.midpoint or market.last_price

            if count <= 0 and probability > 0:
                if broker:
                    balance = await broker.get_balance()
                else:
                    from gimmes.kalshi.portfolio import get_balance
                    balance = await get_balance(client)
                final_count = position_size(
                    balance, mkt_price, probability,
                    fraction=config.sizing.kelly_fraction,
                    max_position_pct=config.sizing.max_position_pct,
                )
            else:
                final_count = count

            if final_count <= 0:
                console.print("[red]No contracts to order (count=0)[/red]")
                return

            final_price = price if price > 0 else int(mkt_price * 100)

            params = CreateOrderParams(
                ticker=ticker,
                action=OrderAction.BUY,
                side=OrderSide(side),
                count=final_count,
                yes_price=final_price if side == "yes" else None,
                no_price=final_price if side == "no" else None,
                post_only=(config.orders.preferred_order_type == "maker"),
            )

            msg = f"Placing order: {final_count}x {ticker} {side.upper()} @ {final_price}¢"
            console.print(msg)

            if broker:
                orderbook = await get_orderbook(client, ticker)
                result = await broker.create_order(params, orderbook)
                label = "[yellow]PAPER[/yellow] "
            else:
                from gimmes.kalshi.orders import create_order
                result = await create_order(client, params)
                label = ""

            console.print(
                f"[green]{label}Order placed:[/green] {result.order_id}"
                f" (status: {result.status})"
            )

    _run(_order())


@app.command()
def cancel(
    order_id: str = typer.Argument(..., help="Order ID to cancel"),
) -> None:
    """Cancel a resting order."""
    config = load_config()

    async def _cancel() -> None:
        async with trading_context(config) as (client, broker):
            if broker:
                await broker.cancel_order(order_id)
            else:
                from gimmes.kalshi.orders import cancel_order
                await cancel_order(client, order_id)
            console.print(f"[green]Canceled order {order_id}[/green]")

    _run(_cancel())


@app.command()
def positions() -> None:
    """List open positions."""
    config = load_config()

    async def _positions() -> None:
        from gimmes.kalshi.markets import get_market
        from gimmes.models.market import MarketStatus
        from gimmes.reporting.formatter import format_positions

        async with trading_context(config) as (client, broker):
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
                    except Exception:
                        pass
                # Re-fetch after mark-to-market
                pos_list = await broker.get_positions()
            else:
                from gimmes.kalshi.portfolio import get_all_positions
                pos_list = await get_all_positions(client)

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
        from gimmes.store.database import Database
        from gimmes.store.queries import get_daily_pnl

        async with trading_context(config) as (client, broker):
            if broker:
                balance = await broker.get_balance()
                pos = await broker.get_positions()
            else:
                from gimmes.kalshi.portfolio import get_all_positions, get_balance
                balance = await get_balance(client)
                pos = await get_all_positions(client)

            daily_pnl = 0.0
            try:
                async with Database(config.db_path) as db:
                    daily_pnl = await get_daily_pnl(db)
            except Exception:
                pass

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
        except Exception:
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


@app.command()
def discover(
    category: str = typer.Argument(..., help="Category to explore (Economics, Politics, Financials, etc.)"),
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


if __name__ == "__main__":
    app()
