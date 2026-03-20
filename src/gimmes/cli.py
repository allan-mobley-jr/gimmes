"""GIMMES CLI — Typer-based command interface for Kalshi trading."""

from __future__ import annotations

import asyncio
import subprocess
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


_RECONCILE_HINT = (
    "[yellow]Run 'gimmes reconcile'"
    " to sync positions with the broker.[/yellow]"
)


def _api_error_detail(e) -> str:  # type: ignore[no-untyped-def]
    """Extract a human-readable message from an httpx.HTTPStatusError."""
    fallback = e.response.text[:200] if e.response.text else str(e)
    try:
        body = e.response.json()
    except (ValueError, UnicodeDecodeError):
        return fallback
    if not isinstance(body, dict):
        return fallback
    return str(body.get("message") or body.get("error") or fallback)


def _run(coro):  # type: ignore[no-untyped-def]
    """Run an async coroutine from sync CLI context with error handling."""
    import logging
    import sqlite3

    import httpx

    logger = logging.getLogger("gimmes.cli")
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        raise typer.Exit(130)
    except httpx.HTTPStatusError as e:
        logger.debug("API error", exc_info=True)
        detail = _api_error_detail(e)
        console.print(f"[red]API error ({e.response.status_code}): {detail}[/red]")
        raise typer.Exit(1)
    except httpx.TimeoutException as e:
        logger.debug("Timeout error", exc_info=True)
        console.print(f"[red]Request timed out: {e}[/red]")
        raise typer.Exit(1)
    except httpx.TransportError as e:
        logger.debug("Transport error", exc_info=True)
        console.print(f"[red]Connection error: {e}[/red]")
        raise typer.Exit(1)
    except sqlite3.Error as e:
        logger.debug("Database error", exc_info=True)
        console.print(f"[red]Database error: {e}[/red]")
        raise typer.Exit(1)
    except typer.Exit:
        raise
    except (ConnectionError, ValueError, RuntimeError) as e:
        logger.debug("CLI error", exc_info=True)
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)


def _mode_banner(config: GimmesConfig) -> None:
    """Show a compact mode indicator so the user always knows which mode is active."""
    if config.is_championship:
        console.print(
            "[red bold]⚠  CHAMPIONSHIP MODE — REAL MONEY ⚠[/red bold]",
            highlight=False,
        )
    else:
        console.print(
            "[green]● Driving Range[/green] [dim](paper trading)[/dim]",
            highlight=False,
        )


async def _mark_positions_to_market(
    broker,   # PaperBroker
    client,   # KalshiClient
    *,
    known_prices: dict[str, float] | None = None,
) -> list:
    """Mark all paper positions to market and return refreshed list."""
    from gimmes.kalshi.markets import get_market

    positions = await broker.get_positions()
    prices = dict(known_prices or {})

    for pos in positions:
        try:
            if pos.ticker not in prices:
                market = await get_market(client, pos.ticker)
                prices[pos.ticker] = market.midpoint or market.last_price
            await broker.mark_to_market(pos.ticker, prices[pos.ticker])
        except Exception as exc:
            console.print(
                f"[yellow]Warning: could not mark {pos.ticker}"
                f" to market: {exc}[/yellow]"
            )

    return await broker.get_positions()


@asynccontextmanager
async def trading_context(config: GimmesConfig):
    """Yields (client, broker, db). broker is None in championship mode.

    Both modes use the prod API client for real market data.
    In driving range, a PaperBroker handles portfolio operations locally.
    Both modes open a Database for position syncing and snapshots.
    Refreshes the fee multiplier cache on entry.
    """
    from gimmes.kalshi.client import KalshiClient
    from gimmes.store.database import Database
    from gimmes.strategy.fee_cache import refresh_fee_cache

    async with KalshiClient(config) as client:
        await refresh_fee_cache(client)
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


@app.command(rich_help_panel="Mode & Status")
def mode() -> None:
    """Show current mode and connection status."""
    from gimmes.store.session import get_active_session

    config = load_config()
    active = get_active_session(config.db_path)
    _mode_banner(config)

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

        if active:
            console.print(
                f"\n[green]Active session:[/green] "
                f"PID {active['pid']}, "
                f"cycle {active['cycle_count']}, "
                f"started {active['started_at']}"
            )

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
        from gimmes.strategy.fee_cache import get_multipliers
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
            fees = get_multipliers(market.series_ticker)

            kf = kelly_fraction(
                price, probability,
                fraction=config.sizing.kelly_fraction, fees=fees,
            )
            contracts = position_size(
                balance, price, probability,
                fraction=config.sizing.kelly_fraction,
                max_position_pct=config.sizing.max_position_pct, fees=fees,
            )
            fee = fee_for_order(contracts, price, is_taker=False, fees=fees)
            edge = edge_after_fees(price, probability, fees=fees)
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
    size_up: bool = typer.Option(False, "--size-up", help="Allow adding to existing position"),
) -> None:
    """Pre-trade validation for a market."""
    config = load_config()

    async def _validate() -> None:
        from gimmes.kalshi.markets import get_market
        from gimmes.risk.validator import validate_trade
        from gimmes.store.queries import get_daily_pnl, get_session_spending
        from gimmes.store.session import get_active_session
        from gimmes.strategy.fee_cache import get_multipliers
        from gimmes.strategy.kelly import position_size

        async with trading_context(config) as (client, broker, db):
            market = await get_market(client, ticker)

            price = market.midpoint or market.last_price

            if broker:
                balance = await broker.get_balance()
                positions = await _mark_positions_to_market(
                    broker, client, known_prices={ticker: price},
                )
            else:
                from gimmes.kalshi.portfolio import get_all_positions, get_balance
                from gimmes.store.queries import sync_positions
                balance = await get_balance(client)
                positions = await get_all_positions(client)
                await sync_positions(db, positions)

            fees = get_multipliers(market.series_ticker)
            if dollars <= 0:
                contracts = position_size(
                    balance, price, probability,
                    fraction=config.sizing.kelly_fraction,
                    max_position_pct=config.sizing.max_position_pct, fees=fees,
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

            # Session spending: use active session boundary or fall back to today
            active = get_active_session(config.db_path)
            since = active["started_at"] if active else None
            try:
                session_spent = await get_session_spending(db, since=since)
            except Exception as exc:
                console.print(
                    f"[red bold]VALIDATION FAILED: Could not query"
                    f" session spending — {exc}[/red bold]"
                )
                console.print(
                    "[red]Refusing to validate with unknown spending "
                    "(session spending cap may be breached)[/red]"
                )
                raise typer.Exit(1)

            unrealized_pnl = sum(p.unrealized_pnl for p in positions)
            total_daily_pnl = daily_pnl + unrealized_pnl

            existing_cost_basis = 0.0
            if size_up:
                match = next((p for p in positions if p.ticker == ticker), None)
                if match:
                    existing_cost_basis = match.cost_basis

            existing_tickers = [p.ticker for p in positions]
            result = validate_trade(
                market, trade_dollars, probability, balance,
                total_daily_pnl, len(positions), existing_tickers, config,
                fees=fees, session_spent=session_spent, size_up=size_up,
                existing_cost_basis=existing_cost_basis,
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

            if not result.approved:
                raise typer.Exit(1)

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
    probability: float | None = typer.Option(
        None, "--prob", "-p", help="True probability (buy only: auto-sizing and edge check)",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation (for autonomous mode)",
    ),
    force: bool = typer.Option(
        False, "--force", help="Override validation failures (use with caution)",
    ),
    size_up: bool = typer.Option(
        False, "--size-up", help="Allow adding to existing position (SIZE UP)",
    ),
) -> None:
    """Place an order on Kalshi (runs pre-trade validation first)."""
    config = load_config()

    async def _order() -> None:
        import json
        import logging
        import sqlite3
        import traceback

        import httpx

        from gimmes.kalshi.markets import get_market, get_orderbook
        from gimmes.models.error import ErrorCategory, ErrorLogEntry, ErrorSeverity
        from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide
        from gimmes.risk.validator import validate_trade
        from gimmes.store.queries import get_daily_pnl, get_session_spending, insert_error
        from gimmes.store.session import get_active_session
        from gimmes.strategy.fee_cache import get_multipliers
        from gimmes.strategy.fees import fee_for_order
        from gimmes.strategy.kelly import position_size

        logger = logging.getLogger("gimmes.cli")

        async with trading_context(config) as (client, broker, db):
            market = await get_market(client, ticker)
            mkt_price = market.midpoint or market.last_price
            fees = get_multipliers(market.series_ticker)

            # Get balance and positions for both sizing and validation
            if broker:
                balance = await broker.get_balance()
                positions = await _mark_positions_to_market(
                    broker, client, known_prices={ticker: mkt_price},
                )
            else:
                from gimmes.kalshi.portfolio import get_all_positions, get_balance
                from gimmes.store.queries import sync_positions
                balance = await get_balance(client)
                positions = await get_all_positions(client)
                await sync_positions(db, positions)

            order_action = OrderAction(action.lower())
            is_buy = order_action == OrderAction.BUY
            is_taker = config.orders.preferred_order_type != "maker"

            if is_buy and count <= 0 and probability is not None:
                final_count = position_size(
                    balance, mkt_price, probability,
                    fraction=config.sizing.kelly_fraction,
                    max_position_pct=config.sizing.max_position_pct, fees=fees,
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

                unrealized_pnl = sum(p.unrealized_pnl for p in positions)
                total_daily_pnl = daily_pnl + unrealized_pnl

                # Session spending: use active session boundary or today
                active = get_active_session(config.db_path)
                since = active["started_at"] if active else None
                try:
                    session_spent = await get_session_spending(db, since=since)
                except Exception as exc:
                    if force:
                        session_spent = 0.0
                        console.print(
                            f"[yellow]Warning: Could not query session"
                            f" spending ({exc}) — using 0.0 (--force)"
                            f"[/yellow]"
                        )
                    else:
                        console.print(
                            f"[red bold]Cannot query session spending:"
                            f" {exc}[/red bold]"
                        )
                        console.print(
                            "[red]Refusing to order with unknown"
                            " spending (session cap may be"
                            " breached). Use --force to"
                            " override.[/red]"
                        )
                        return

                true_prob = probability

                existing_cost_basis = 0.0
                if size_up:
                    match = next(
                        (p for p in positions
                         if p.ticker == ticker and p.side == side),
                        None,
                    )
                    if not match:
                        console.print(
                            f"[red]SIZE UP rejected: no {side.upper()}"
                            f" position in {ticker}[/red]"
                        )
                        return
                    existing_cost_basis = match.cost_basis

                existing_tickers = [p.ticker for p in positions]
                validation = validate_trade(
                    market, trade_dollars, true_prob, balance,
                    total_daily_pnl, len(positions), existing_tickers,
                    config, is_taker=is_taker, fees=fees,
                    session_spent=session_spent, size_up=size_up,
                    existing_cost_basis=existing_cost_basis,
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

            # --- Pre-order summary ---
            est_fee = fee_for_order(
                final_count, final_price,
                is_taker=is_taker, fees=fees,
            )
            if is_buy:
                total = trade_dollars + est_fee
            else:
                total = trade_dollars - est_fee
            console.print(
                f"\n  Action:     {action.upper()} {side.upper()}"
                f"\n  Ticker:     {ticker}"
                f"\n  Contracts:  {final_count}"
                f"\n  Price:      {int(round(final_price * 100))}¢"
                f"  (${final_price:.2f})"
                f"\n  Subtotal:   ${trade_dollars:.2f}"
                f"\n  Est. fees:  ${est_fee:.2f}"
                f"\n  Total:      ${total:.2f}"
            )

            if config.is_championship and not yes:
                if not typer.confirm(
                    "\nCHAMPIONSHIP MODE — place this REAL MONEY order?"
                ):
                    raise typer.Abort()

            # --- Place the order ---
            params = CreateOrderParams(
                ticker=ticker,
                action=order_action,
                side=OrderSide(side),
                count=final_count,
                yes_price=final_price if side == "yes" else None,
                no_price=final_price if side == "no" else None,
                post_only=not is_taker,
            )

            try:
                if broker:
                    orderbook = await get_orderbook(client, ticker)
                    result = await broker.create_order(params, orderbook, fees=fees)
                    label = "[yellow]PAPER[/yellow] "
                else:
                    from gimmes.kalshi.orders import create_order

                    result = await create_order(client, params)
                    label = ""
            except httpx.HTTPStatusError as exc:
                logger.debug("Order placement failed", exc_info=True)
                detail = _api_error_detail(exc)
                try:
                    await insert_error(db, ErrorLogEntry(
                        severity=ErrorSeverity.ERROR,
                        category=ErrorCategory.ORDER_FAILURE,
                        error_code="http_status_error",
                        component="cli.order", agent="cli",
                        message=f"Order placement failed ({exc.response.status_code}): {detail}",
                        stack_trace=traceback.format_exc(),
                        context=json.dumps({"ticker": ticker, "side": side,
                                            "count": final_count, "price": final_price,
                                            "status_code": exc.response.status_code}),
                    ))
                except Exception:
                    logger.warning("Failed to log error to DB", exc_info=True)
                console.print(
                    f"[red bold]Order FAILED"
                    f" ({exc.response.status_code}): {detail}[/red bold]"
                )
                raise typer.Exit(1)
            except httpx.TimeoutException as exc:
                logger.debug("Order placement timed out", exc_info=True)
                try:
                    await insert_error(db, ErrorLogEntry(
                        severity=ErrorSeverity.ERROR,
                        category=ErrorCategory.ORDER_FAILURE,
                        error_code="timeout",
                        component="cli.order", agent="cli",
                        message=f"Order placement timed out: {exc}",
                        stack_trace=traceback.format_exc(),
                        context=json.dumps({"ticker": ticker, "side": side,
                                            "count": final_count, "price": final_price}),
                    ))
                except Exception:
                    logger.warning("Failed to log error to DB", exc_info=True)
                console.print(
                    "[red bold]Order FAILED: request timed out[/red bold]"
                )
                if not broker:
                    console.print(
                        "[red]WARNING: The order may have been accepted"
                        " by Kalshi before the timeout.[/red]"
                    )
                console.print(_RECONCILE_HINT)
                raise typer.Exit(1)
            except (sqlite3.Error, ValueError, RuntimeError) as exc:
                logger.debug("Order placement failed", exc_info=True)
                if isinstance(exc, sqlite3.Error):
                    error_code = "db_error"
                elif isinstance(exc, ValueError):
                    error_code = "value_error"
                else:
                    error_code = "runtime_error"
                try:
                    await insert_error(db, ErrorLogEntry(
                        severity=ErrorSeverity.ERROR,
                        category=ErrorCategory.ORDER_FAILURE,
                        error_code=error_code,
                        component="cli.order", agent="cli",
                        message=f"Order placement failed: {exc}",
                        stack_trace=traceback.format_exc(),
                        context=json.dumps({"ticker": ticker, "side": side,
                                            "count": final_count, "price": final_price}),
                    ))
                except Exception:
                    logger.warning("Failed to log error to DB", exc_info=True)
                console.print(f"[red bold]Order FAILED: {exc}[/red bold]")
                raise typer.Exit(1)

            console.print(
                f"[green]{label}Order placed:[/green] {result.order_id}"
                f" (status: {result.status})"
            )

            # Sync positions + log trade atomically so a crash can't
            # leave positions stale while a trade is recorded (or vice versa)
            try:
                if broker:
                    positions_for_sync = await broker.get_positions()
                else:
                    from gimmes.kalshi.portfolio import (
                        get_all_positions as refresh_pos,
                    )

                    positions_for_sync = await refresh_pos(client)

                if result.status in ("executed", "resting"):
                    from gimmes.models.trade import TradeDecision
                    from gimmes.store.queries import (
                        get_thesis_for_ticker,
                        sync_positions_with_trade,
                    )

                    if is_buy:
                        trade_action = (
                            TradeDecision.Action.SIZE_UP
                            if size_up
                            else TradeDecision.Action.OPEN
                        )
                    else:
                        trade_action = TradeDecision.Action.CLOSE
                    if is_buy:
                        try:
                            thesis = await get_thesis_for_ticker(db, ticker)
                        except sqlite3.Error:
                            logger.warning(
                                "Failed to fetch thesis for %s; "
                                "recording trade with empty thesis",
                                ticker, exc_info=True,
                            )
                            thesis = ""
                    else:
                        thesis = ""
                    trade = TradeDecision(
                        ticker=ticker,
                        action=trade_action,
                        side=side,
                        count=final_count,
                        price=final_price,
                        model_probability=0.0 if probability is None else probability,
                        edge=(
                            probability - final_price
                            if probability is not None
                            else 0.0
                        ),
                        rationale="CLI order",
                        thesis=thesis,
                        agent="cli",
                        order_id=result.order_id,
                    )
                    await sync_positions_with_trade(
                        db, positions_for_sync, trade
                    )
                else:
                    from gimmes.store.queries import sync_positions

                    await sync_positions(db, positions_for_sync)
            except sqlite3.Error as exc:
                logger.debug("Position sync failed (database)", exc_info=True)
                try:
                    await insert_error(db, ErrorLogEntry(
                        severity=ErrorSeverity.WARNING,
                        category=ErrorCategory.DATA_INTEGRITY,
                        error_code="position_sync_db_error",
                        component="cli.order", agent="cli",
                        message=f"Position sync failed after order {result.order_id}: {exc}",
                        stack_trace=traceback.format_exc(),
                        context=json.dumps({"ticker": ticker, "side": side,
                                            "count": final_count, "price": final_price,
                                            "order_id": result.order_id}),
                    ))
                except Exception:
                    logger.warning("Failed to log error to DB", exc_info=True)
                console.print(
                    f"[red bold]Warning: Order was placed successfully"
                    f" ({result.order_id}) but position sync"
                    f" failed: {exc}[/red bold]"
                )
                console.print(
                    "[yellow]Database error — check database health"
                    " (disk space, permissions, corruption).[/yellow]"
                )
                console.print(_RECONCILE_HINT)
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                logger.debug("Position sync failed", exc_info=True)
                try:
                    await insert_error(db, ErrorLogEntry(
                        severity=ErrorSeverity.WARNING,
                        category=ErrorCategory.DATA_INTEGRITY,
                        error_code="position_sync_failed",
                        component="cli.order", agent="cli",
                        message=f"Position sync failed after order {result.order_id}: {exc}",
                        stack_trace=traceback.format_exc(),
                        context=json.dumps({"ticker": ticker, "side": side,
                                            "count": final_count, "price": final_price,
                                            "order_id": result.order_id}),
                    ))
                except Exception:
                    logger.warning("Failed to log error to DB", exc_info=True)
                console.print(
                    f"[red bold]Warning: Order was placed successfully"
                    f" ({result.order_id}) but position sync"
                    f" failed: {exc}[/red bold]"
                )
                console.print(_RECONCILE_HINT)

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
def candidates(
    ticker: str | None = typer.Option(
        None, "--ticker", "-t", help="Filter by ticker",
    ),
    limit: int = typer.Option(
        20, "--limit", "-n", help="Number of records to show",
    ),
) -> None:
    """List scanned candidate records from the database."""

    async def _candidates() -> None:
        from rich.table import Table

        from gimmes.store.database import Database
        from gimmes.store.queries import (
            get_candidate_for_ticker,
            get_recent_candidates,
        )

        async with Database() as db:
            if ticker:
                records = await get_candidate_for_ticker(
                    db, ticker, limit=limit,
                )
            else:
                records = await get_recent_candidates(db, limit=limit)

        if not records:
            console.print("[dim]No candidate records found[/dim]")
            return

        title = f"Candidates for {ticker}" if ticker else f"Candidates (last {limit})"
        table = Table(title=title)
        table.add_column("Ticker")
        table.add_column("Score", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("Prob", justify="right")
        table.add_column("Edge", justify="right")
        table.add_column("Status")
        table.add_column("Scanned")

        for c in records:
            status = "[yellow]CAP BLOCKED[/yellow]" if c.get("cap_blocked") else ""
            table.add_row(
                str(c.get("ticker", "")),
                f"{c.get('gimme_score', 0):.0f}",
                f"${c.get('market_price', 0):.2f}",
                f"{c.get('model_probability', 0):.0%}",
                f"{c.get('edge', 0):+.1%}",
                status,
                str(c.get("scanned_at", ""))[:19],
            )

        console.print(table)

    _run(_candidates())


@app.command(name="mark-cap-blocked")
def mark_cap_blocked_cmd(
    ticker: str = typer.Argument(..., help="Market ticker"),
) -> None:
    """Mark the most recent candidate for a ticker as cap-blocked."""

    async def _mark() -> None:
        from gimmes.store.database import Database
        from gimmes.store.queries import mark_cap_blocked

        async with Database() as db:
            updated = await mark_cap_blocked(db, ticker)
        if updated:
            console.print(f"[yellow]Marked {ticker} as cap-blocked[/yellow]")
        else:
            console.print(f"[red]No candidate found for {ticker}[/red]")
            raise typer.Exit(1)

    _run(_mark())


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
        from gimmes.risk.limits import (
            check_daily_loss,
            check_position_count,
            check_session_spending,
        )
        from gimmes.store.queries import get_daily_pnl, get_session_spending
        from gimmes.store.session import get_active_session

        async with trading_context(config) as (client, broker, db):
            if broker:
                balance = await broker.get_balance()
                pos = await _mark_positions_to_market(broker, client)
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

            # Session spending
            active = get_active_session(config.db_path)
            since = active["started_at"] if active else None
            try:
                session_spent = await get_session_spending(db, since=since)
            except Exception as exc:
                console.print(
                    f"[red bold]RISK CHECK FAILED: Could not query"
                    f" session spending — {exc}[/red bold]"
                )
                console.print(
                    "[red]Cannot verify risk limits with unknown spending[/red]"
                )
                raise typer.Exit(1)
            cap = config.risk.session_spending_cap

            unrealized_pnl = sum(p.unrealized_pnl for p in pos)
            total_daily_pnl = daily_pnl + unrealized_pnl

            console.print("\n[bold]Risk Check[/bold]")
            console.print(f"Balance: ${balance:,.2f}")
            console.print(f"Open Positions: {len(pos)}/{config.risk.max_open_positions}")
            console.print(f"Daily Realized P&L: ${daily_pnl:,.2f}")
            console.print(f"Unrealized P&L:     ${unrealized_pnl:,.2f}")
            console.print(f"Total Daily P&L:    ${total_daily_pnl:,.2f}")
            console.print(f"Session Spending:   ${session_spent:,.2f} / ${cap:,.2f}")
            console.print(f"Price Trigger:      {config.risk.monitor_price_trigger_pp}pp")

            loss = check_daily_loss(total_daily_pnl, balance, config)
            count = check_position_count(len(pos), config)
            spending = check_session_spending(session_spent, 0, config)

            for check, label in [
                (loss, "Daily Loss"),
                (count, "Position Count"),
                (spending, "Session Spending"),
            ]:
                if check.passed:
                    console.print(f"  [green]✓[/green] {label}: OK")
                else:
                    console.print(f"  [red]✗[/red] {label}: {check.reason}")

    _run(_check())


@app.command()
def reconcile() -> None:
    """Sync local position data with the authoritative source.

    In driving range mode, copies paper_positions to the main positions table.
    In championship mode, fetches positions from the Kalshi API.
    Reports any differences found.
    """
    config = load_config()

    async def _reconcile() -> None:
        from gimmes.store.queries import get_positions, sync_positions

        async with trading_context(config) as (client, broker, db):
            # Fill resting paper orders against current market data
            if broker:
                resting = await broker.list_orders(status="resting")
                if resting:
                    from gimmes.kalshi.markets import get_orderbook

                    tickers = {o.ticker for o in resting}
                    orderbooks = {}
                    for t in tickers:
                        try:
                            orderbooks[t] = await get_orderbook(client, t)
                        except Exception as exc:
                            import logging

                            logging.getLogger("gimmes").warning(
                                "Could not fetch orderbook for %s", t,
                                exc_info=True,
                            )
                            console.print(
                                f"  [yellow]Warning: could not fetch"
                                f" orderbook for {t}: {exc}[/yellow]"
                            )
                    try:
                        filled = await broker.fill_resting_orders(orderbooks)
                    except Exception as exc:
                        import logging

                        logging.getLogger("gimmes").warning(
                            "Failed to process resting orders: %s", exc,
                            exc_info=True,
                        )
                        console.print(
                            f"  [yellow]Warning: could not process"
                            f" resting orders: {exc}[/yellow]"
                        )
                        filled = []
                    for o in filled:
                        n = o.count - o.remaining_count
                        console.print(
                            f"  [green]Filled resting order {o.order_id}:"
                            f" {o.action.value.upper()} {n}"
                            f" {o.side.value.upper()} {o.ticker}[/green]"
                        )

            old_positions = await get_positions(db)
            old_tickers = {p.ticker: p for p in old_positions}

            if broker:
                fresh = await broker.get_positions()
                source = "paper broker"
            else:
                from gimmes.kalshi.portfolio import get_all_positions
                fresh = await get_all_positions(client)
                source = "Kalshi API"

            await sync_positions(db, fresh)

            fresh_tickers = {p.ticker: p for p in fresh}

            added = fresh_tickers.keys() - old_tickers.keys()
            removed = old_tickers.keys() - fresh_tickers.keys()
            common = old_tickers.keys() & fresh_tickers.keys()
            changed = [
                t for t in common
                if (old_tickers[t].count != fresh_tickers[t].count
                    or old_tickers[t].side != fresh_tickers[t].side)
            ]

            if not added and not removed and not changed:
                console.print(
                    f"[green]Positions in sync with {source}"
                    f" ({len(fresh)} positions)[/green]"
                )
            else:
                console.print(f"[bold]Reconciled with {source}:[/bold]")
                for t in added:
                    console.print(f"  [green]+[/green] {t} ({fresh_tickers[t].count} contracts)")
                for t in removed:
                    console.print(f"  [red]-[/red] {t} (removed)")
                for t in changed:
                    console.print(
                        f"  [yellow]~[/yellow] {t}: "
                        f"{old_tickers[t].count} → {fresh_tickers[t].count} contracts"
                    )

    _run(_reconcile())


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
    prob: float | None = typer.Option(None, "--prob", "-p"),
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
            model_probability=0.0 if prob is None else prob,
            gimme_score=score_val,
            edge=prob - price_val if prob is not None else 0.0,
            rationale=rationale,
            agent=agent,
        )

        async with Database(config.db_path) as db:
            row_id = await insert_trade(db, trade)
            console.print(f"[green]Logged trade #{row_id}: {action} {ticker}[/green]")

    _run(_log())


@app.command(name="log-candidate")
def log_candidate(
    ticker: str = typer.Argument(..., help="Market ticker"),
    title: str = typer.Option("", "--title", "-t", help="Event title"),
    price_val: float = typer.Option(0, "--price", help="Market price"),
    prob: float = typer.Option(0, "--prob", "-p", help="Model probability estimate"),
    score_val: float = typer.Option(0, "--score", help="GimmeScore (0-100)"),
    memo: str = typer.Option("", "--memo", "-m", help="Research memo summary"),
    edge_size: float = typer.Option(0, "--edge-size", help="Edge size score"),
    signal_strength: float = typer.Option(0, "--signal-strength", help="Signal strength score"),
    liquidity_depth: float = typer.Option(0, "--liquidity-depth", help="Liquidity depth score"),
    settlement_clarity: float = typer.Option(
        0, "--settlement-clarity", help="Settlement clarity score",
    ),
    time_to_resolution: float = typer.Option(
        0, "--time-to-resolution", help="Time to resolution score",
    ),
) -> None:
    """Log a scanned candidate to the candidates table."""
    config = load_config()

    async def _log() -> None:
        from gimmes.store.database import Database
        from gimmes.store.queries import insert_candidate as _insert

        edge = prob - price_val

        async with Database(config.db_path) as db:
            row_id = await _insert(
                db, ticker, title, price_val, prob, edge, score_val, memo,
                edge_size_score=edge_size,
                signal_strength_score=signal_strength,
                liquidity_depth_score=liquidity_depth,
                settlement_clarity_score=settlement_clarity,
                time_to_resolution_score=time_to_resolution,
            )
            console.print(f"[green]Logged candidate #{row_id}: {ticker}[/green]")

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


def _print_note(note: dict) -> None:  # type: ignore[type-arg]
    """Print a single position note with its metadata header."""
    console.print(
        f"[dim][#{note['id']}] {note['timestamp']} | cycle={note['cycle']}"
        f" | {note['agent']} | {note['note_type']}[/dim]"
    )
    console.print(note["body"], markup=False)


@app.command(name="position-context")
def position_context(
    ticker: str = typer.Argument(..., help="Market ticker"),
) -> None:
    """Show the full thesis and note history for an open position."""
    config = load_config()

    async def _ctx() -> None:
        import sqlite3

        from gimmes.store.database import Database
        from gimmes.store.queries import (
            get_open_trade_for_ticker,
            get_position_notes,
            has_open_position,
        )

        try:
            async with Database(config.db_path) as db:
                trade = await get_open_trade_for_ticker(db, ticker)
                is_open = await has_open_position(db, ticker)
                notes = await get_position_notes(db, ticker, limit=20)
        except sqlite3.Error as exc:
            console.print(f"[red]Database error: {exc}[/red]")
            raise typer.Exit(1) from exc

        if not trade or not is_open:
            console.print(f"[yellow]No open position found for {ticker}[/yellow]")
            return

        console.print(f"\n[bold]Position Context: {ticker}[/bold]\n")
        console.print("[bold]--- OPEN TRADE ---[/bold]")
        console.print(f"Opened:           {trade['timestamp']}")
        console.print(
            f"Side:             {trade['side'].upper()}"
            f"  Count: {trade['count']}  Entry: ${trade['price']:.2f}"
        )
        console.print(
            f"Model Prob:       {trade['model_probability']:.0%}"
            f"  Edge: {trade['edge']:+.1%}"
            f"  GimmeScore: {trade['gimme_score']:.0f}"
        )
        console.print(f"Agent:            {trade['agent']}  Order ID: {trade['order_id']}")

        console.print("\n[bold]--- ORIGINAL THESIS ---[/bold]")
        thesis = trade.get("thesis", "")
        if thesis:
            console.print(thesis, markup=False)
        else:
            console.print("[dim][No thesis stored — position predates v8 migration][/dim]")

        console.print("\n[bold]--- POSITION NOTES (last 20) ---[/bold]")
        if notes:
            for n in reversed(notes):
                console.print()
                _print_note(n)
        else:
            console.print("[dim]No notes yet.[/dim]")

        decisions = [n for n in notes if n["note_type"] == "decision"]
        if decisions:
            console.print("\n[bold yellow]--- CADDIE MASTER DECISIONS ---[/bold yellow]")
            for n in decisions:
                console.print(f"[#{n['id']}] cycle={n['cycle']} —")
                console.print(n["body"][:120] + "...", markup=False)

    _run(_ctx())


@app.command(name="position-note")
def position_note(
    ticker: str = typer.Argument(..., help="Market ticker"),
    cycle: int = typer.Option(0, "--cycle", "-c", help="Cycle number"),
    agent: str = typer.Option("manual", "--agent", "-a", help="Agent name"),
    note_type: str = typer.Option(
        "observation", "--type", "-t",
        help="Note type: observation, flag, decision, context",
    ),
    body: str = typer.Option(..., "--body", "-b", help="Note content"),
) -> None:
    """Append a note to the position journal."""
    valid_types = ("observation", "flag", "decision", "context")
    if note_type not in valid_types:
        console.print(f"[red]Invalid type '{note_type}': must be one of {valid_types}[/red]")
        raise typer.Exit(1)

    config = load_config()

    async def _note() -> None:
        import sqlite3

        from gimmes.store.database import Database
        from gimmes.store.queries import insert_position_note

        try:
            async with Database(config.db_path) as db:
                row_id = await insert_position_note(
                    db, ticker=ticker, cycle=cycle, agent=agent,
                    note_type=note_type, body=body,
                )
        except sqlite3.Error as exc:
            console.print(f"[red]Database error: {exc}[/red]")
            raise typer.Exit(1) from exc
        console.print(
            f"[green]Logged position note #{row_id}"
            f" ({note_type}) for {ticker}[/green]"
        )

    _run(_note())


@app.command(name="position-notes")
def position_notes(
    ticker: str = typer.Argument(..., help="Market ticker"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max notes to return"),
) -> None:
    """Show the position journal for a ticker."""
    config = load_config()

    async def _notes() -> None:
        import sqlite3

        from gimmes.store.database import Database
        from gimmes.store.queries import get_position_notes

        try:
            async with Database(config.db_path) as db:
                notes = await get_position_notes(db, ticker, limit=limit)
        except sqlite3.Error as exc:
            console.print(f"[red]Database error: {exc}[/red]")
            raise typer.Exit(1) from exc

        if not notes:
            console.print(f"[yellow]No notes found for {ticker}[/yellow]")
            return

        console.print(f"\n[bold]Position Notes: {ticker} ({len(notes)} notes)[/bold]\n")
        for n in reversed(notes):
            _print_note(n)
            console.print()

    _run(_notes())


@app.command(name="log-activity")
def log_activity(
    cycle: int = typer.Option(0, "--cycle", "-c", help="Cycle number"),
    agent: str = typer.Option("", "--agent", "-a", help="Agent name"),
    phase: str = typer.Option("", "--phase", help="Phase (start/complete/error)"),
    message: str = typer.Option("", "--message", "-m", help="Activity message"),
    details: str = typer.Option("", "--details", "-d", help="Additional details"),
    session_id: int = typer.Option(0, "--session-id", help="Session ID"),
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
                session_id=session_id or None,
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
def init(
    headless: bool = typer.Option(
        False,
        "--headless",
        help="Non-interactive mode (requires env vars: KALSHI_PROD_API_KEY, "
        "KALSHI_PROD_PRIVATE_KEY_PATH, KALSHI_PRIVATE_KEY_PASSWORD)",
    ),
) -> None:
    """Set up gimmes for first-time use (config files, API credentials)."""
    from gimmes.init import run_init

    run_init(headless=headless)


# ---------------------------------------------------------------------------
# Clubhouse dashboard
# ---------------------------------------------------------------------------


@app.command()
def clubhouse(
    port: int = typer.Option(1919, "--port", "-p", help="Port number"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't auto-open browser"),
) -> None:
    """Launch the Clubhouse web dashboard (standalone)."""
    from gimmes.clubhouse.server import run_standalone

    config = load_config()
    run_standalone(port=port, db_path=config.db_path, open_browser=not no_browser)


# ---------------------------------------------------------------------------
# Product tour
# ---------------------------------------------------------------------------


@app.command(name="tour_guide")
def tour_guide() -> None:
    """Launch The Starter — an interactive GIMMES product tour."""
    import shutil
    import subprocess

    claude_path = shutil.which("claude")
    if not claude_path:
        console.print(
            "[red]Error: 'claude' CLI not found. Install Claude Code first.[/red]"
        )
        raise typer.Exit(1)

    project_root = Path(__file__).resolve().parent.parent.parent

    console.print(
        "\n[bold green]Starting the GIMMES tour...[/bold green]\n"
        "[dim]The Starter will guide you through the system.[/dim]\n"
    )

    try:
        result = subprocess.run(
            [
                claude_path,
                "--agent", "Starter",
                "--name", "GIMMES Tour",
            ],
            cwd=project_root,
            check=False,
        )
    except KeyboardInterrupt:
        console.print("\n[dim]Tour interrupted.[/dim]")
        raise typer.Exit(130)
    except OSError as exc:
        console.print(
            f"[red]Failed to launch Claude Code: {exc}[/red]\n"
            "[yellow]Ensure 'claude' is installed and executable.[/yellow]"
        )
        raise typer.Exit(1)

    if result.returncode != 0:
        console.print(
            f"[red]Tour exited with an error (code {result.returncode}).[/red]"
        )
        raise typer.Exit(1)

    console.print("\n[yellow]Tour ended. Happy trading![/yellow]")


# ---------------------------------------------------------------------------
# Mode switching
# ---------------------------------------------------------------------------


def _set_mode(target: str) -> None:
    """Write GIMMES_MODE to .env and reload dotenv so the process picks it up."""
    import os

    from gimmes.init import ENV_FILE, _update_env_var

    if not ENV_FILE.exists():
        console.print(
            f"[red]Error: .env not found at {ENV_FILE}. "
            "Run 'gimmes init' first.[/red]"
        )
        raise typer.Exit(1)

    try:
        _update_env_var("GIMMES_MODE", target)
    except OSError as e:
        console.print(f"[red]Error: Could not write to {ENV_FILE}: {e}[/red]")
        raise typer.Exit(1)

    # Reload .env so load_config() in this process sees the change
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=ENV_FILE, override=True)

    # Verify the write took effect
    actual = os.environ.get("GIMMES_MODE", "")
    if actual != target:
        console.print(
            f"[red]Error: Mode switch did not take effect. "
            f"Expected '{target}' but got '{actual}'.[/red]"
        )
        raise typer.Exit(1)


@app.command(name="switch", rich_help_panel="Mode & Status")
def switch(
    target: str | None = typer.Argument(
        None,
        help="Target mode: driving_range or championship (omit to toggle)",
    ),
) -> None:
    """Switch trading mode (persisted in .env)."""
    from gimmes.config import Mode

    config = load_config()
    current = config.mode.value

    if target is None:
        # Toggle
        target = (
            Mode.CHAMPIONSHIP.value
            if current == Mode.DRIVING_RANGE.value
            else Mode.DRIVING_RANGE.value
        )
    else:
        target = target.lower()
        if target not in (Mode.DRIVING_RANGE.value, Mode.CHAMPIONSHIP.value):
            console.print(
                f"[red]Invalid mode '{target}'. "
                f"Use 'driving_range' or 'championship'.[/red]"
            )
            raise typer.Exit(1)

    if target == current:
        console.print(f"Already in [bold]{current}[/bold] mode.")
        return

    if target == Mode.CHAMPIONSHIP.value:
        console.print("\n[red bold]⚠  CHAMPIONSHIP MODE — REAL MONEY ⚠[/red bold]")
        console.print(
            "Switching to championship mode means all trades use real money.\n"
        )
        if not typer.confirm("Switch to championship mode?"):
            raise typer.Abort()

    _set_mode(target)

    # Show updated banner
    new_config = load_config()
    _mode_banner(new_config)
    console.print(f"\nSwitched from [bold]{current}[/bold] → [bold]{target}[/bold]")


# ---------------------------------------------------------------------------
# Autonomous loop commands
# ---------------------------------------------------------------------------


def _confirm_championship() -> None:
    """Show championship warning and prompt for confirmation. Aborts on decline."""
    console.print(
        "This will trade with real money on Kalshi autonomously.\n"
        "The system will scan markets, research candidates, and execute trades\n"
        "without asking for confirmation on each order.\n"
    )
    if not typer.confirm("Are you sure you want to start autonomous trading with real money?"):
        raise typer.Abort()


@app.command(name="start", rich_help_panel="Autonomous Trading")
def start(
    cycles: int = typer.Option(
        0, "--cycles", "-n", min=0, help="Max cycles to run (0=unlimited)",
    ),
    pause: int = typer.Option(60, "--pause", min=0, help="Seconds between cycles (default 60)"),
    no_dashboard: bool = typer.Option(
        False, "--no-dashboard", help="Disable auto-start of Clubhouse dashboard",
    ),
) -> None:
    """Start autonomous trading loop using the current mode from .env."""
    config = load_config()
    mode_val = config.mode.value
    _mode_banner(config)

    if config.is_championship:
        _confirm_championship()

    _autonomous_loop(mode_val, max_cycles=cycles, pause_seconds=pause,
                     no_dashboard=no_dashboard)


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    """Send SIGTERM to the process group, escalating to SIGKILL if needed.

    Handles ProcessLookupError at each step in case the process exits
    between our check and the signal delivery.
    """
    import os
    import signal

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return  # Already exited

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # Exited between SIGTERM and SIGKILL
        proc.wait()


def _communicate_interruptible(
    proc: subprocess.Popen[bytes],
    timeout: float,
) -> bytes:
    """Read subprocess stdout in a background thread so the main thread
    remains interruptible by KeyboardInterrupt.

    ``proc.communicate()`` blocks the main thread in a C-level read that
    cannot be interrupted by SIGINT on macOS.  This function moves the
    blocking read to a daemon thread and polls with short
    ``Thread.join()`` intervals so the main thread can process pending
    signals between iterations (CPython bug bpo-45274 prevents a single
    blocking ``join()`` from being interrupted).

    The caller is responsible for killing the subprocess on
    ``TimeoutExpired`` or ``KeyboardInterrupt``; killing the process
    closes the pipe and unblocks the daemon reader thread.

    Returns the captured stdout bytes.  Raises ``subprocess.TimeoutExpired``
    if the subprocess has not finished within *timeout* seconds.
    """
    import threading
    import time

    if proc.stdout is None:
        raise ValueError(
            "_communicate_interruptible requires stdout=PIPE"
        )

    stdout = proc.stdout  # narrowed to IO[bytes] for the closure

    output: list[bytes] = []
    error: list[BaseException] = []

    def _reader() -> None:
        try:
            data = stdout.read()
            if data:
                output.append(data)
        except BaseException as exc:
            error.append(exc)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    # Poll with short intervals so pending SIGINT can be delivered
    # between iterations.  A single join(timeout=N) blocks signal
    # delivery on macOS due to SA_RESTART on pthread_cond_timedwait.
    poll_interval = 0.5
    deadline = time.monotonic() + timeout
    while reader.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        reader.join(timeout=min(poll_interval, remaining))

    if reader.is_alive():
        raise subprocess.TimeoutExpired(cmd=proc.args, timeout=timeout)

    if error:
        raise error[0]

    # Bounded wait to reap the child — stdout is already at EOF so the
    # process should have exited (or be exiting imminently).
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        import logging

        logging.getLogger("gimmes").warning(
            "Subprocess did not exit within 5s after stdout EOF; "
            "killing process group",
        )
        _kill_process_group(proc)
    return b"".join(output)


def _result_dict_to_text(data: dict[str, object]) -> bytes:
    """Extract terminal text from a Claude result dict."""
    if data.get("is_error"):
        detail = data.get("result") or data.get("subtype", "unknown")
        return f"[Claude error: {detail}]\n".encode()
    result = data.get("result")
    if result:
        return (result + "\n").encode("utf-8")
    return b""


def _extract_terminal_text(json_bytes: bytes) -> bytes:
    """Extract human-readable assistant text from Claude JSON output.

    Handles both single-object JSON (``--output-format json``) and
    newline-delimited stream-json (``--output-format stream-json``).
    Falls back to raw bytes if JSON parsing fails.
    """
    import json as _json
    import logging

    if not json_bytes or not json_bytes.strip():
        logging.getLogger("gimmes").warning(
            "Claude subprocess produced no output for this cycle",
        )
        return b""

    # Stream-json: multiple newline-delimited JSON objects
    lines = json_bytes.strip().split(b"\n")
    if len(lines) > 1:
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                event = _json.loads(line)
            except (_json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                return _result_dict_to_text(event)
        # Stream-json with no result event (e.g. killed mid-session)
        logging.getLogger("gimmes").warning(
            "Stream-json output contained no result event",
        )
        return b""

    # Single JSON object (--output-format json)
    try:
        data = _json.loads(json_bytes)
    except (_json.JSONDecodeError, UnicodeDecodeError):
        logging.getLogger("gimmes").warning(
            "Failed to parse Claude JSON output; displaying raw output",
        )
        return json_bytes

    if isinstance(data, dict):
        return _result_dict_to_text(data)

    return json_bytes


def _wrap_stream_json(raw: bytes) -> bytes:
    """Wrap newline-delimited JSON events into a JSON array.

    If the input is a single JSON object, returns it unchanged.
    """
    import json as _json

    lines = raw.strip().split(b"\n")
    if len(lines) <= 1:
        return raw

    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(_json.loads(line))
        except (_json.JSONDecodeError, UnicodeDecodeError):
            events.append(line.decode("utf-8", errors="replace"))
    return _json.dumps(events, separators=(",", ":")).encode("utf-8")


def _autonomous_loop(
    mode: str,
    *,
    max_cycles: int = 0,
    pause_seconds: int = 60,
    no_dashboard: bool = False,
    max_consecutive_failures: int = 5,
) -> None:
    """Run the Caddie Master orchestrator agent via claude --agent in a loop.

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
    import sys
    import time

    from gimmes.config import GIMMES_HOME

    claude_path = shutil.which("claude")
    if not claude_path:
        console.print("[red]Error: 'claude' CLI not found. Install Claude Code first.[/red]")
        raise typer.Exit(1)

    project_root = Path(__file__).resolve().parent.parent.parent
    config = load_config()

    # --- Session management ---
    from gimmes.store.session import (
        close_orphan_activities,
        create_session,
        end_session,
        get_max_global_cycle,
        mark_stale_sessions,
        update_session_cycle,
    )

    # Ensure DB + migrations are up to date
    async def _ensure_db() -> None:
        from gimmes.store.database import Database

        async with Database(config.db_path):
            pass  # connect() runs migrations automatically

    asyncio.run(_ensure_db())

    # Clean up any stale sessions from prior crashes
    stale = mark_stale_sessions(config.db_path)
    if stale:
        console.print(
            f"[dim]Cleaned up {stale} stale session(s) from prior crash[/dim]"
        )
    orphans = close_orphan_activities(config.db_path)
    if orphans:
        console.print(
            f"[dim]Closed {orphans} orphan activity entries[/dim]"
        )

    session_id = create_session(config.db_path, mode, os.getpid())

    # Set mode and session ID in process env for subprocesses
    os.environ["GIMMES_MODE"] = mode
    os.environ["GIMMES_SESSION_ID"] = str(session_id)

    env = os.environ.copy()

    # Cycle log directory
    logs_dir = GIMMES_HOME / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

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

    cycle = get_max_global_cycle(config.db_path)
    cycles_run = 0
    consecutive_failures = 0
    session_status = "stopped"

    # Custom SIGINT handler — Python's default SIGINT → KeyboardInterrupt
    # delivery is blocked by Thread.join() on macOS (CPython bpo-45274).
    # This handler fires at the C level and sends SIGTERM to the subprocess
    # process group (non-blocking), then raises KeyboardInterrupt.  The
    # actual process reaping happens in the except KeyboardInterrupt block.
    import signal

    # Accessed from the signal handler, which runs on the main thread in
    # CPython.  Assignment is atomic under the GIL.
    _active_proc: subprocess.Popen[bytes] | None = None

    def _sigint_handler(signum: int, frame: object) -> None:
        p = _active_proc
        if p is not None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        raise KeyboardInterrupt

    proc = None
    old_handler = signal.signal(signal.SIGINT, _sigint_handler)
    try:
        while max_cycles == 0 or cycles_run < max_cycles:
            cycle += 1
            cycles_run += 1
            console.print(f"[cyan]--- Cycle {cycle} ---[/cyan]")

            update_session_cycle(config.db_path, session_id, cycle)

            env["GIMMES_CYCLE"] = str(cycle)
            log_path = logs_dir / f"cycle-{cycle:03d}.json"
            try:
                proc = subprocess.Popen(
                    [
                        claude_path,
                        "--agent", "Caddie Master",
                        "-p", "Run one trading cycle.",
                        "--verbose",
                        "--output-format", "stream-json",
                        "--allowedTools",
                        "Bash,Read,Glob,Grep,Agent,WebSearch,WebFetch",
                    ],
                    env=env,
                    cwd=project_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                _active_proc = proc
                try:
                    stdout_bytes = _communicate_interruptible(
                        proc, timeout=config.strategy.cycle_timeout,
                    )
                finally:
                    _active_proc = None
                try:
                    with open(log_path, "wb") as log_file:
                        log_file.write(_wrap_stream_json(stdout_bytes))
                except OSError:
                    import logging
                    logging.getLogger("gimmes").warning(
                        "Failed to write cycle log to %s", log_path,
                        exc_info=True,
                    )
                    console.print(
                        f"[yellow]Warning: could not write log"
                        f" {log_path}[/yellow]"
                    )
                terminal_text = _extract_terminal_text(stdout_bytes)
                sys.stdout.buffer.write(terminal_text)
                sys.stdout.buffer.flush()
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                consecutive_failures += 1
                console.print(
                    f"[yellow]Cycle {cycle} timed out after"
                    f" {config.strategy.cycle_timeout}s"
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
                    session_status = "crashed"
                    break
                continue

            if returncode != 0:
                consecutive_failures += 1
                console.print(
                    f"[yellow]Cycle {cycle} exited with code"
                    f" {returncode}"
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
                    session_status = "crashed"
                    break
            else:
                consecutive_failures = 0

            if max_cycles > 0 and cycles_run >= max_cycles:
                break

            console.print(f"[dim]Next cycle in {pause_seconds}s...[/dim]")
            time.sleep(pause_seconds)
    except KeyboardInterrupt:
        if proc is not None and proc.poll() is None:
            _kill_process_group(proc)
    except Exception:
        session_status = "crashed"
        raise
    finally:
        signal.signal(signal.SIGINT, old_handler)
        end_session(config.db_path, session_id, session_status)

    console.print("\n[yellow]Autonomous loop stopped.[/yellow]")


@app.command(name="driving_range", rich_help_panel="Autonomous Trading")
def driving_range(
    cycles: int = typer.Option(
        0, "--cycles", "-n", min=0, help="Max cycles to run (0=unlimited)",
    ),
    pause: int = typer.Option(60, "--pause", min=0, help="Seconds between cycles (default 60)"),
    no_dashboard: bool = typer.Option(
        False, "--no-dashboard", help="Disable auto-start of Clubhouse dashboard",
    ),
) -> None:
    """Switch to Driving Range mode and start autonomous trading loop (paper trading)."""
    _set_mode("driving_range")
    _autonomous_loop("driving_range", max_cycles=cycles, pause_seconds=pause,
                     no_dashboard=no_dashboard)


@app.command(name="championship", rich_help_panel="Autonomous Trading")
def championship(
    cycles: int = typer.Option(
        0, "--cycles", "-n", min=0, help="Max cycles to run (0=unlimited)",
    ),
    pause: int = typer.Option(60, "--pause", min=0, help="Seconds between cycles (default 60)"),
    no_dashboard: bool = typer.Option(
        False, "--no-dashboard", help="Disable auto-start of Clubhouse dashboard",
    ),
) -> None:
    """Switch to Championship mode and start autonomous trading loop (REAL MONEY)."""
    console.print("\n[red bold]⚠  CHAMPIONSHIP MODE — REAL MONEY ⚠[/red bold]")
    _confirm_championship()
    _set_mode("championship")
    _autonomous_loop("championship", max_cycles=cycles, pause_seconds=pause,
                     no_dashboard=no_dashboard)


if __name__ == "__main__":
    app()
