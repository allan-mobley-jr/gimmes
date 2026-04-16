"""GIMMES CLI — Typer-based command interface for Kalshi trading."""

from __future__ import annotations

import asyncio
import functools
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import click
import typer
from rich.console import Console

from gimmes.config import GIMMES_HOME, GimmesConfig, load_config

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


def _extract_ticker_from_url(exc) -> str | None:  # type: ignore[no-untyped-def]
    """Try to extract a market ticker from the request URL in a 404 error."""
    try:
        parts = str(exc.request.url.path).split("/")
        idx = parts.index("markets")
        ticker = parts[idx + 1]
    except (AttributeError, ValueError, IndexError):
        return None
    return ticker or None


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
        if e.response.status_code == 404:
            ticker = _extract_ticker_from_url(e)
            if ticker:
                console.print(
                    f"[red]Market ticker '{ticker}' not found."
                    " Check the ticker and try again.[/red]"
                )
                raise typer.Exit(1)
        detail = _api_error_detail(e)
        console.print(f"[red]API error ({e.response.status_code}): {detail}[/red]")
        raise typer.Exit(1)
    except httpx.TimeoutException:
        logger.debug("Timeout error", exc_info=True)
        console.print(
            "[red]Request timed out.[/red] "
            "Kalshi may be slow or unreachable. "
            "Check your connection and try again."
        )
        raise typer.Exit(1)
    except httpx.TransportError:
        logger.debug("Transport error", exc_info=True)
        console.print(
            "[red]Connection error.[/red] "
            "Could not reach Kalshi. "
            "Check your internet connection and try again."
        )
        raise typer.Exit(1)
    except sqlite3.Error as e:
        logger.warning("Database error: %s: %s", type(e).__name__, e, exc_info=True)
        from gimmes.config import GIMMES_HOME

        db_path = GIMMES_HOME / "gimmes.db"
        if not db_path.exists():
            console.print(
                "[red]Database not found.[/red] "
                "Run [bold]gimmes init[/bold] to set up GIMMES."
            )
        else:
            console.print(
                f"[red]Database error: {e}.[/red] "
                "Try [bold]gimmes reconcile[/bold] or reinstall with "
                "[bold]gimmes update[/bold]."
            )
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

    markets: dict = {}
    for pos in positions:
        try:
            if pos.ticker not in prices:
                market = await get_market(client, pos.ticker)
                prices[pos.ticker] = market.midpoint or market.last_price
                markets[pos.ticker] = market
            await broker.mark_to_market(
                pos.ticker,
                prices[pos.ticker],
                close_time=getattr(markets.get(pos.ticker), "close_time", None),
            )
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
        has_credentials = (
            bool(config.api_key)
            and str(config.private_key_path) != "."
            and config.private_key_path.exists()
        )

        if has_credentials:
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

        if not has_credentials:
            console.print(
                "\n[yellow]Not configured yet."
                " Run [bold]gimmes init[/bold] to set up your API credentials.[/yellow]"
            )

        if active:
            console.print(
                f"\n[green]Active session:[/green] "
                f"PID {active['pid']}, "
                f"cycle {active['cycle_count']}, "
                f"started {active['started_at']}"
            )

    _run(_check())


@app.command(rich_help_panel="Market Research")
def scan(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of top candidates to show"),
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
        import logging
        import sqlite3

        from gimmes.kalshi.client import KalshiClient
        from gimmes.kalshi.markets import list_all_markets
        from gimmes.reporting.formatter import format_scan_results
        from gimmes.store.database import Database
        from gimmes.store.queries import get_position_tickers
        from gimmes.strategy.scanner import filter_markets
        from gimmes.strategy.scorer import quick_score

        logger = logging.getLogger("gimmes.cli")

        # Exclude tickers with open positions
        exclude_tickers: set[str] = set()
        try:
            async with Database(config.db_path) as db:
                exclude_tickers = await get_position_tickers(db, table=config.position_table)
            if exclude_tickers:
                console.print(
                    f"Excluding {len(exclude_tickers)} ticker(s) with open positions"
                )
        except sqlite3.OperationalError:
            logger.debug("Could not load position tickers", exc_info=True)

        async with KalshiClient(config) as client:
            console.print("[cyan]Scanning markets...[/cyan]")

            all_scored: list[dict] = []  # type: ignore[type-arg]
            seen_tickers: set[str] = set()

            for scan_side in config.sides_to_scan:
                side_cfg = config.effective_config_for_side(scan_side)
                side_series = (
                    series
                    or side_cfg.scanner.series
                )

                if side_series and not all_markets:
                    markets = []
                    for st in side_series:
                        batch = await list_all_markets(
                            client, series_ticker=st,
                        )
                        markets.extend(batch)
                else:
                    markets = await list_all_markets(client)

                candidates = filter_markets(
                    markets, side_cfg,
                    exclude_tickers=exclude_tickers,
                )

                # Staleness filtering
                staleness_threshold = side_cfg.scanner.staleness_cycles
                if staleness_threshold > 0:
                    from gimmes.strategy.staleness import (
                        check_staleness,
                        load_staleness,
                        save_staleness,
                    )

                    staleness_path = GIMMES_HOME / "scan_staleness.json"
                    staleness_data = load_staleness(staleness_path)
                    candidates, stale_skipped, staleness_data = (
                        check_staleness(
                            candidates, staleness_data,
                            threshold=staleness_threshold,
                        )
                    )
                    save_staleness(staleness_data, staleness_path)
                    if stale_skipped:
                        console.print(
                            f"[dim]Skipped {len(stale_skipped)}"
                            f" stale market(s)[/dim]"
                        )

                side_label = (
                    f" [{scan_side.upper()}]"
                    if config.strategy.side == "both"
                    else ""
                )
                console.print(
                    f"Fetched {len(markets)} markets,"
                    f" {len(candidates)} candidates{side_label}"
                )

                for m in candidates:
                    if m.ticker in seen_tickers:
                        continue
                    seen_tickers.add(m.ticker)
                    qs = quick_score(m, side_cfg)
                    all_scored.append({
                        "ticker": m.ticker,
                        "event_ticker": m.event_ticker,
                        "title": m.title,
                        "price": m.midpoint or m.last_price,
                        "volume_24h": m.volume_24h or m.volume,
                        "open_interest": m.open_interest,
                        "score": qs,
                        "side": scan_side,
                    })

            all_scored.sort(key=lambda r: r["score"], reverse=True)
            format_scan_results(all_scored[:limit])

    _run(_scan())


@app.command(rich_help_panel="Market Research")
def score(
    ticker: str = typer.Argument(..., help="Market ticker to score"),
) -> None:
    """Score a specific market for gimme potential."""
    config = load_config()

    async def _score() -> None:
        from gimmes.kalshi.client import KalshiClient
        from gimmes.kalshi.markets import get_market, get_orderbook
        from gimmes.reporting.formatter import format_kv_table
        from gimmes.strategy.scorer import quick_score

        async with KalshiClient(config) as client:
            market = await get_market(client, ticker)
            orderbook = await get_orderbook(client, ticker)
            qs = quick_score(market, config)

            table = format_kv_table(market.title, [
                ("Ticker", market.ticker),
                ("Price", f"${market.midpoint:.2f}"),
                ("Volume 24h", str(market.volume_24h)),
                ("Open Interest", str(market.open_interest)),
                ("Spread", f"${market.spread:.2f}"),
                ("Best YES Bid", str(orderbook.best_yes_bid)),
                ("Best YES Ask", str(orderbook.best_yes_ask)),
                ("Quick Score", f"[bold]{qs:.0f}[/bold]/100"),
            ])
            console.print(table)

    _run(_score())


@app.command(rich_help_panel="Trading")
def size(
    ticker: str = typer.Argument(..., help="Market ticker"),
    probability: float = typer.Option(
        ..., "--prob", "-p", help="True probability for configured side",
    ),
) -> None:
    """Calculate position size for a market."""
    config = load_config()

    async def _size() -> None:
        from gimmes.kalshi.markets import get_market
        from gimmes.reporting.formatter import format_kv_table
        from gimmes.strategy.fee_cache import get_multipliers
        from gimmes.strategy.fees import edge_after_fees, fee_for_order
        from gimmes.strategy.kelly import apply_base_rate_floor, kelly_fraction, position_size

        async with trading_context(config) as (client, broker, _db):
            market = await get_market(client, ticker)

            if broker:
                balance = await broker.get_balance()
            else:
                from gimmes.kalshi.portfolio import get_balance
                balance = await get_balance(client)

            from gimmes.strategy.scanner import effective_price

            raw_price = market.midpoint or market.last_price
            price = effective_price(raw_price, config.strategy.side)
            fees = get_multipliers(market.series_ticker)

            bankroll = config.bankroll
            true_prob = apply_base_rate_floor(probability, ticker, side=config.strategy.side)
            kf = kelly_fraction(
                price, true_prob,
                fraction=config.sizing.kelly_fraction, fees=fees,
            )
            contracts = position_size(
                bankroll, price, true_prob,
                fraction=config.sizing.kelly_fraction,
                max_position_pct=config.sizing.max_position_pct, fees=fees,
                mode=config.sizing.mode,
            )
            fee = fee_for_order(contracts, price, is_taker=False, fees=fees)
            edge = edge_after_fees(price, true_prob, fees=fees)
            cost = contracts * price + fee

            table = format_kv_table(f"Position Sizing: {ticker}", [
                ("Market Price", f"${price:.2f}"),
                ("True Probability", f"{true_prob:.1%}"),
                ("Edge After Fees", f"{edge:.1%}"),
                ("Kelly Fraction", f"{kf:.4f}"),
                ("Bankroll", f"${bankroll:,.2f}"),
                ("Balance", f"${balance:,.2f}"),
                ("Contracts", f"[bold]{contracts}[/bold]"),
                ("Est. Cost", f"${cost:,.2f}"),
                ("Est. Fee", f"${fee:,.2f}"),
            ])
            console.print(table)

    _run(_size())


@app.command(rich_help_panel="Trading")
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
        from gimmes.store.queries import get_daily_pnl, get_deployed_cost_basis
        from gimmes.strategy.fee_cache import get_multipliers
        from gimmes.strategy.kelly import apply_base_rate_floor, position_size
        from gimmes.strategy.scanner import effective_price

        async with trading_context(config) as (client, broker, db):
            market = await get_market(client, ticker)

            raw_price = market.midpoint or market.last_price
            price = effective_price(raw_price, config.strategy.side)
            bankroll = config.bankroll

            if broker:
                positions = await _mark_positions_to_market(
                    broker, client, known_prices={ticker: raw_price},
                )
            else:
                from gimmes.kalshi.portfolio import get_all_positions
                from gimmes.store.queries import sync_positions
                positions = await get_all_positions(client)
                await sync_positions(db, positions)

            fees = get_multipliers(market.series_ticker)
            true_prob = apply_base_rate_floor(probability, ticker, side=config.strategy.side)
            if dollars <= 0:
                contracts = position_size(
                    bankroll, price, true_prob,
                    fraction=config.sizing.kelly_fraction,
                    max_position_pct=config.sizing.max_position_pct, fees=fees,
                    mode=config.sizing.mode,
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

            # Deployed cost basis for bankroll check
            try:
                deployed = await get_deployed_cost_basis(db)
            except Exception as exc:
                console.print(
                    f"[red bold]VALIDATION FAILED: Could not query"
                    f" deployed cost basis — {exc}[/red bold]"
                )
                console.print(
                    "[red]Refusing to validate with unknown deployed"
                    " capital (bankroll limit may be breached)[/red]"
                )
                raise typer.Exit(1)

            unrealized_pnl = sum(p.unrealized_pnl for p in positions)
            total_daily_pnl = daily_pnl + unrealized_pnl

            existing_cost_basis = 0.0
            if size_up:
                match = next((p for p in positions if p.ticker == ticker), None)
                if match:
                    existing_cost_basis = match.cost_basis

            from gimmes.risk.limits import compute_exposure_for_group

            existing_tickers = [p.ticker for p in positions]
            event_exp = compute_exposure_for_group(positions, market.event_ticker)
            series_exp = compute_exposure_for_group(positions, market.series_ticker)
            result = validate_trade(
                market, trade_dollars, true_prob, bankroll,
                total_daily_pnl, len(positions), existing_tickers, config,
                fees=fees, deployed_cost_basis=deployed, size_up=size_up,
                existing_cost_basis=existing_cost_basis,
                event_exposure=event_exp,
                series_exposure=series_exp,
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


@app.command(rich_help_panel="Trading")
def order(
    ticker: str = typer.Argument(..., help="Market ticker"),
    action: str = typer.Option(
        "buy", "--action", "-a", help="Order action (buy/sell)",
        click_type=click.Choice(["buy", "sell"], case_sensitive=False),
    ),
    side: str = typer.Option("", "--side", "-s", help="Order side (yes/no, default from config)"),
    count: int = typer.Option(0, "--count", "-c", help="Number of contracts (0=auto-size)"),
    price: int = typer.Option(
        0, "--price", help="Limit price in cents, e.g. 70 for $0.70 (0=market)"
    ),
    probability: float | None = typer.Option(
        None, "--prob", "-p",
        help="True probability for configured side (buy only: sizing/edge)",
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
    agent: str = typer.Option(
        "cli", "--agent", help="Agent identifier for trade/error logging",
    ),
) -> None:
    """Place an order on Kalshi (runs pre-trade validation first)."""
    config = load_config()

    async def _order() -> None:
        nonlocal side
        if not side:
            side = config.strategy.side

        import json
        import logging
        import sqlite3
        import traceback

        import httpx

        from gimmes.kalshi.markets import get_market, get_orderbook
        from gimmes.models.error import ErrorCategory, ErrorLogEntry, ErrorSeverity
        from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide
        from gimmes.risk.validator import validate_trade
        from gimmes.store.queries import get_daily_pnl, get_deployed_cost_basis, insert_error
        from gimmes.strategy.fee_cache import get_multipliers
        from gimmes.strategy.fees import fee_for_order
        from gimmes.strategy.kelly import apply_base_rate_floor, position_size

        logger = logging.getLogger("gimmes.cli")

        async with trading_context(config) as (client, broker, db):
            from gimmes.strategy.scanner import effective_price

            market = await get_market(client, ticker)
            raw_price = market.midpoint or market.last_price
            eff_price = effective_price(raw_price, side)
            fees = get_multipliers(market.series_ticker)

            # Get positions for validation
            if broker:
                positions = await _mark_positions_to_market(
                    broker, client, known_prices={ticker: raw_price},
                )
            else:
                from gimmes.kalshi.portfolio import get_all_positions
                from gimmes.store.queries import sync_positions
                positions = await get_all_positions(client)
                await sync_positions(db, positions)

            order_action = OrderAction(action.lower())
            is_buy = order_action == OrderAction.BUY
            is_taker = config.orders.preferred_order_type != "maker"

            bankroll = config.bankroll
            true_prob = probability
            if is_buy and count <= 0 and probability is not None:
                true_prob = apply_base_rate_floor(probability, ticker, side=config.strategy.side)
                final_count = position_size(
                    bankroll, eff_price, true_prob,
                    fraction=config.sizing.kelly_fraction,
                    max_position_pct=config.sizing.max_position_pct, fees=fees,
                    mode=config.sizing.mode,
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

            final_price = price / 100.0 if price > 0 else eff_price
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

                # Deployed cost basis for bankroll check
                try:
                    deployed = await get_deployed_cost_basis(db)
                except Exception as exc:
                    if force:
                        deployed = 0.0
                        console.print(
                            f"[yellow]Warning: Could not query deployed"
                            f" cost basis ({exc}) — using 0.0 (--force)"
                            f"[/yellow]"
                        )
                    else:
                        console.print(
                            f"[red bold]Cannot query deployed cost"
                            f" basis: {exc}[/red bold]"
                        )
                        console.print(
                            "[red]Refusing to order with unknown"
                            " deployed capital (bankroll limit may"
                            " be breached). Use --force to"
                            " override.[/red]"
                        )
                        return

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

                from gimmes.risk.limits import compute_exposure_for_group

                existing_tickers = [p.ticker for p in positions]
                evt_exp = compute_exposure_for_group(positions, market.event_ticker)
                ser_exp = compute_exposure_for_group(positions, market.series_ticker)
                validation = validate_trade(
                    market, trade_dollars, true_prob, bankroll,
                    total_daily_pnl, len(positions), existing_tickers,
                    config, is_taker=is_taker, fees=fees,
                    deployed_cost_basis=deployed, size_up=size_up,
                    existing_cost_basis=existing_cost_basis,
                    event_exposure=evt_exp,
                    series_exposure=ser_exp,
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
                        component="cli.order", agent=agent,
                        message=f"Order placement failed ({exc.response.status_code}): {detail}",
                        stack_trace=traceback.format_exc(),
                        context=json.dumps({"ticker": ticker, "side": side,
                                            "count": final_count, "price": final_price,
                                            "status_code": exc.response.status_code}),
                    ))
                except Exception:
                    logger.error("Failed to log error to DB", exc_info=True)
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
                        component="cli.order", agent=agent,
                        message=f"Order placement timed out: {exc}",
                        stack_trace=traceback.format_exc(),
                        context=json.dumps({"ticker": ticker, "side": side,
                                            "count": final_count, "price": final_price}),
                    ))
                except Exception:
                    logger.error("Failed to log error to DB", exc_info=True)
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
                        component="cli.order", agent=agent,
                        message=f"Order placement failed: {exc}",
                        stack_trace=traceback.format_exc(),
                        context=json.dumps({"ticker": ticker, "side": side,
                                            "count": final_count, "price": final_price}),
                    ))
                except Exception:
                    logger.error("Failed to log error to DB", exc_info=True)
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
                        get_open_trade_for_ticker,
                        get_thesis_for_ticker,
                        sync_positions_with_trade,
                    )

                    if is_buy:
                        trade_action = (
                            TradeDecision.Action.SIZE_UP
                            if size_up
                            else TradeDecision.Action.OPEN
                        )
                        try:
                            thesis = ""
                            if size_up:
                                open_trade = await get_open_trade_for_ticker(
                                    db, ticker,
                                )
                                if open_trade:
                                    thesis = open_trade.get("thesis", "")
                                if not thesis:
                                    reason = (
                                        "open trade has empty thesis"
                                        if open_trade
                                        else "no open trade found"
                                    )
                                    logger.warning(
                                        "Size-up thesis fallback for %s "
                                        "(%s); using candidate thesis",
                                        ticker, reason,
                                    )
                            if not thesis:
                                thesis = await get_thesis_for_ticker(
                                    db, ticker,
                                )
                        except sqlite3.Error as exc:
                            logger.error(
                                "Failed to fetch thesis for %s; "
                                "recording trade with empty thesis: %s",
                                ticker, exc, exc_info=True,
                            )
                            try:
                                await insert_error(db, ErrorLogEntry(
                                    severity=ErrorSeverity.WARNING,
                                    category=ErrorCategory.DATA_INTEGRITY,
                                    error_code="thesis_fetch_failed",
                                    component="cli.order", agent=agent,
                                    message=(
                                        f"Thesis fetch failed for {ticker}; "
                                        f"trade recorded with empty thesis: {exc}"
                                    ),
                                    stack_trace=traceback.format_exc(),
                                    context=json.dumps({
                                        "ticker": ticker, "side": side,
                                        "count": final_count,
                                        "price": final_price,
                                    }),
                                ))
                            except Exception:
                                logger.error(
                                    "Failed to log error to DB", exc_info=True,
                                )
                            thesis = ""
                    else:
                        trade_action = TradeDecision.Action.CLOSE
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
                        rationale=thesis or f"{agent} order",
                        thesis=thesis,
                        agent=agent,
                        order_id=result.order_id,
                    )
                    await sync_positions_with_trade(
                        db, positions_for_sync, trade
                    )
                else:
                    from gimmes.store.queries import sync_positions

                    await sync_positions(db, positions_for_sync)
            except sqlite3.Error as exc:
                logger.warning(
                    "Position sync failed (database): %s", exc, exc_info=True,
                )
                try:
                    await insert_error(db, ErrorLogEntry(
                        severity=ErrorSeverity.WARNING,
                        category=ErrorCategory.DATA_INTEGRITY,
                        error_code="position_sync_db_error",
                        component="cli.order", agent=agent,
                        message=f"Position sync failed after order {result.order_id}: {exc}",
                        stack_trace=traceback.format_exc(),
                        context=json.dumps({"ticker": ticker, "side": side,
                                            "count": final_count, "price": final_price,
                                            "order_id": result.order_id}),
                    ))
                except Exception:
                    logger.error("Failed to log error to DB", exc_info=True)
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
                logger.warning(
                    "Position sync failed: %s", exc, exc_info=True,
                )
                try:
                    await insert_error(db, ErrorLogEntry(
                        severity=ErrorSeverity.WARNING,
                        category=ErrorCategory.DATA_INTEGRITY,
                        error_code="position_sync_failed",
                        component="cli.order", agent=agent,
                        message=f"Position sync failed after order {result.order_id}: {exc}",
                        stack_trace=traceback.format_exc(),
                        context=json.dumps({"ticker": ticker, "side": side,
                                            "count": final_count, "price": final_price,
                                            "order_id": result.order_id}),
                    ))
                except Exception:
                    logger.error("Failed to log error to DB", exc_info=True)
                console.print(
                    f"[red bold]Warning: Order was placed successfully"
                    f" ({result.order_id}) but position sync"
                    f" failed: {exc}[/red bold]"
                )
                console.print(_RECONCILE_HINT)

    _run(_order())


@app.command(rich_help_panel="Trading")
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


@app.command(rich_help_panel="Trading")
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

        from gimmes.reporting.formatter import format_local_timestamp
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
                format_local_timestamp(str(t.get("timestamp", ""))),
            )

        console.print(table)

    _run(_trades())


@app.command(rich_help_panel="Trading")
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

        from gimmes.reporting.formatter import format_local_timestamp
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
        table.add_column("Rec")
        table.add_column("Scanned")

        for c in records:
            status = "[yellow]CAP BLOCKED[/yellow]" if c.get("cap_blocked") else ""
            rec = str(c.get("recommendation", ""))
            table.add_row(
                str(c.get("ticker", "")),
                f"{c.get('gimme_score', 0):.0f}",
                f"${c.get('market_price', 0):.2f}",
                f"{c.get('model_probability', 0):.1%}",
                f"{c.get('edge', 0):+.1%}",
                status,
                rec,
                format_local_timestamp(str(c.get("scanned_at", ""))),
            )

        console.print(table)

    _run(_candidates())


@app.command(name="prune-candidates", hidden=True)
def prune_candidates_cmd(
    max_age: int = typer.Option(72, "--max-age", help="Max age in hours before pruning"),
    check_markets: bool = typer.Option(
        False, "--check-markets", help="Check market status via Kalshi API",
    ),
) -> None:
    """Remove candidates that have exited the pipeline."""
    config = load_config()

    async def _prune() -> None:
        import logging
        import sqlite3

        from gimmes.store.database import Database
        from gimmes.store.queries import get_position_tickers, prune_candidates

        logger = logging.getLogger("gimmes.cli")
        inactive_tickers: set[str] | None = None

        async with Database(config.db_path) as db:
            try:
                open_tickers = await get_position_tickers(
                    db, table=config.position_table,
                )
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    logger.debug("Position table not found", exc_info=True)
                    open_tickers = set()
                else:
                    raise

            if check_markets:
                from gimmes.kalshi.client import KalshiClient
                from gimmes.kalshi.markets import get_market
                from gimmes.models.market import MarketStatus

                # Collect unique candidate tickers still in the table
                cursor = await db.conn.execute(
                    "SELECT DISTINCT ticker FROM candidates"
                )
                rows = await cursor.fetchall()
                candidate_tickers = {
                    row["ticker"] for row in rows
                } - open_tickers  # no need to check tickers already being pruned

                inactive_tickers = set()
                inactive_statuses = {
                    MarketStatus.CLOSED,
                    MarketStatus.DETERMINED,
                    MarketStatus.FINALIZED,
                }
                skipped = 0
                async with KalshiClient(config) as client:
                    for t in candidate_tickers:
                        try:
                            market = await get_market(client, t)
                            if market.status in inactive_statuses:
                                inactive_tickers.add(t)
                        except Exception:
                            logger.debug("Could not check market %s", t, exc_info=True)
                            skipped += 1
                if skipped:
                    console.print(
                        f"[yellow]Could not check {skipped} ticker(s)"
                        " via API — they will be pruned by age only[/yellow]"
                    )

            counts = await prune_candidates(
                db,
                open_tickers=open_tickers,
                inactive_tickers=inactive_tickers,
                max_age_hours=max_age,
            )

        total = sum(counts.values())
        if total:
            console.print(
                f"[green]Pruned {total} candidates[/green]"
                f" ({counts['opened']} opened, {counts['inactive']} inactive,"
                f" {counts['aged_out']} aged out, {counts['duplicates']} duplicates)"
            )
        else:
            console.print("[dim]No candidates to prune[/dim]")

    _run(_prune())


@app.command(name="mark-cap-blocked", hidden=True)
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


@app.command(name="reset-cooldown", rich_help_panel="Trading")
def reset_cooldown(
    force: bool = typer.Option(
        False, "--force", "-f", help="Skip confirmation prompt",
    ),
) -> None:
    """Clear all cached candidate scores to reset the cooldown system.

    Use this after changing strategy parameters (side, price range, etc.)
    so the system re-evaluates all markets with the new strategy.
    """

    async def _reset() -> None:
        from gimmes.store.database import Database
        from gimmes.store.queries import clear_all_candidates

        if not force:
            typer.confirm(
                "Clear all cached candidates? This resets cooldown for every market.",
                abort=True,
            )

        async with Database() as db:
            count = await clear_all_candidates(db)

        if count > 0:
            console.print(
                f"[green]Cleared {count} cached candidate(s)"
                f" — cooldown reset[/green]"
            )
        else:
            console.print("[dim]No cached candidates to clear[/dim]")

    _run(_reset())


@app.command(rich_help_panel="Portfolio")
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


@app.command(name="risk-check", rich_help_panel="Portfolio")
def risk_check() -> None:
    """Check risk limits and daily P&L."""
    config = load_config()

    async def _check() -> None:
        from gimmes.reporting.formatter import format_kv_table
        from gimmes.risk.limits import (
            check_bankroll,
            check_daily_loss,
            check_position_count,
        )
        from gimmes.store.queries import get_daily_pnl, get_deployed_cost_basis

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

            try:
                deployed = await get_deployed_cost_basis(db)
            except Exception as exc:
                console.print(
                    f"[red bold]RISK CHECK FAILED: Could not query"
                    f" deployed cost basis — {exc}[/red bold]"
                )
                console.print(
                    "[red]Cannot verify risk limits with unknown"
                    " deployed capital[/red]"
                )
                raise typer.Exit(1)
            bankroll = config.bankroll

            unrealized_pnl = sum(p.unrealized_pnl for p in pos)
            total_daily_pnl = daily_pnl + unrealized_pnl

            table = format_kv_table("Risk Check", [
                ("Balance", f"${balance:,.2f}"),
                ("Bankroll", f"${bankroll:,.2f}"),
                ("Deployed Capital", f"${deployed:,.2f} / ${bankroll:,.2f}"),
                ("Open Positions", f"{len(pos)}/{config.risk.max_open_positions}"),
                ("Daily Realized P&L", f"${daily_pnl:,.2f}"),
                ("Unrealized P&L", f"${unrealized_pnl:,.2f}"),
                ("Total Daily P&L", f"${total_daily_pnl:,.2f}"),
                ("Price Trigger", f"{config.risk.monitor_price_trigger_pp}pp"),
                ("Position Stop-Loss", f"{config.risk.position_stop_loss_pct:.0%}"),
                ("Position Take-Profit", f"{config.risk.position_take_profit_pct:.0%}"),
            ])
            console.print(table)

            loss = check_daily_loss(total_daily_pnl, bankroll, config)
            count = check_position_count(len(pos), config)
            bankroll_chk = check_bankroll(deployed, 0, config)

            for check, label in [
                (loss, "Daily Loss"),
                (count, "Position Count"),
                (bankroll_chk, "Bankroll"),
            ]:
                if check.passed:
                    console.print(f"  [green]✓[/green] {label}: OK")
                else:
                    console.print(f"  [red]✗[/red] {label}: {check.reason}")

    _run(_check())


@app.command(rich_help_panel="Portfolio")
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


@app.command(rich_help_panel="Portfolio")
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


@app.command(name="market-info", rich_help_panel="Market Research")
def market_info(
    ticker: str = typer.Argument(..., help="Market ticker"),
) -> None:
    """Show detailed market information."""
    config = load_config()

    async def _info() -> None:
        from gimmes.kalshi.client import KalshiClient
        from gimmes.kalshi.markets import get_market, get_orderbook
        from gimmes.reporting.formatter import format_kv_table
        from gimmes.risk.settlement import scan_settlement_rules

        async with KalshiClient(config) as client:
            market = await get_market(client, ticker)
            orderbook = await get_orderbook(client, ticker)
            settlement = scan_settlement_rules(market.rules_primary)

            risk_color = {"low": "green", "medium": "yellow", "high": "red"}.get(
                settlement.risk_level, "white"
            )
            table = format_kv_table(market.title, [
                ("Ticker", market.ticker),
                ("Event", market.event_ticker),
                ("Status", market.status.value),
                ("YES Bid", f"${market.yes_bid:.2f}"),
                ("YES Ask", f"${market.yes_ask:.2f}"),
                ("Last Price", f"${market.last_price:.2f}"),
                ("Spread", f"${market.spread:.2f}"),
                ("Volume", str(market.volume)),
                ("Volume 24h", str(market.volume_24h)),
                ("Open Interest", str(market.open_interest)),
                ("Close Time", str(market.close_time)),
                ("Best YES Bid", str(orderbook.best_yes_bid)),
                ("Best YES Ask", str(orderbook.best_yes_ask)),
                ("Depth (YES bids)", f"{len(orderbook.yes_bids)} levels"),
                ("Settlement Risk", f"[{risk_color}]{settlement.summary}[/{risk_color}]"),
            ])
            console.print(table)

    _run(_info())


@app.command(name="log-trade", hidden=True)
def log_trade(
    ticker: str = typer.Argument(..., help="Market ticker"),
    action: str = typer.Option(..., "--action", "-a", help="open/close/skip"),
    side: str = typer.Option("", "--side", "-s"),
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
        from gimmes.strategy.scanner import effective_price

        resolved_side = side if side else config.strategy.side
        eff = effective_price(price_val, resolved_side)
        trade = TradeDecision(
            ticker=ticker,
            action=TradeDecision.Action(action),
            side=resolved_side,
            count=count,
            price=price_val,
            model_probability=0.0 if prob is None else prob,
            gimme_score=score_val,
            edge=(prob - eff) if prob is not None else 0.0,
            rationale=rationale,
            agent=agent,
        )

        async with Database(config.db_path) as db:
            row_id = await insert_trade(db, trade)
            console.print(f"[green]Logged trade #{row_id}: {action} {ticker}[/green]")

    _run(_log())


@app.command(name="log-candidate", hidden=True)
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
    recommendation: str = typer.Option(
        "", "--recommendation", help="Caddie recommendation (proceed/pass/needs_more_research)",
    ),
) -> None:
    """Log a scanned candidate to the candidates table."""
    config = load_config()

    async def _log() -> None:
        from gimmes.store.database import Database
        from gimmes.store.queries import insert_candidate as _insert
        from gimmes.strategy.scanner import effective_price

        eff = effective_price(price_val, config.strategy.side)
        edge = prob - eff

        async with Database(config.db_path) as db:
            row_id = await _insert(
                db, ticker, title, price_val, prob, edge, score_val, memo,
                edge_size_score=edge_size,
                signal_strength_score=signal_strength,
                liquidity_depth_score=liquidity_depth,
                settlement_clarity_score=settlement_clarity,
                time_to_resolution_score=time_to_resolution,
                recommendation=recommendation,
            )
            console.print(f"[green]Logged candidate #{row_id}: {ticker}[/green]")

    _run(_log())


@app.command(name="log-outcome", hidden=True)
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


@app.command(name="position-context", rich_help_panel="Portfolio")
def position_context(
    ticker: str = typer.Argument(..., help="Market ticker"),
) -> None:
    """Show the full thesis and note history for an open position."""
    config = load_config()

    async def _ctx() -> None:
        from gimmes.reporting.formatter import format_local_timestamp
        from gimmes.store.database import Database
        from gimmes.store.queries import (
            get_open_trade_for_ticker,
            get_position_notes,
            has_open_position,
        )

        async with Database(config.db_path) as db:
            trade = await get_open_trade_for_ticker(db, ticker)
            is_open = await has_open_position(db, ticker)
            notes = await get_position_notes(db, ticker, limit=20)

        if not trade or not is_open:
            console.print(f"[yellow]No open position found for {ticker}[/yellow]")
            return

        console.print(f"\n[bold]Position Context: {ticker}[/bold]\n")
        console.print("[bold]--- OPEN TRADE ---[/bold]")
        console.print(f"Opened:           {format_local_timestamp(str(trade['timestamp']))}")
        console.print(
            f"Side:             {trade['side'].upper()}"
            f"  Count: {trade['count']}  Entry: ${trade['price']:.2f}"
        )
        console.print(
            f"Model Prob:       {trade['model_probability']:.1%}"
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


@app.command(name="position-note", hidden=True)
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
        from gimmes.store.database import Database
        from gimmes.store.queries import insert_position_note

        async with Database(config.db_path) as db:
            row_id = await insert_position_note(
                db, ticker=ticker, cycle=cycle, agent=agent,
                note_type=note_type, body=body,
            )
        console.print(
            f"[green]Logged position note #{row_id}"
            f" ({note_type}) for {ticker}[/green]"
        )

    _run(_note())


@app.command(name="position-notes", rich_help_panel="Portfolio")
def position_notes(
    ticker: str = typer.Argument(..., help="Market ticker"),
    limit: int = typer.Option(50, "--limit", "-n", help="Max notes to return"),
) -> None:
    """Show the position journal for a ticker."""
    config = load_config()

    async def _notes() -> None:
        from gimmes.store.database import Database
        from gimmes.store.queries import get_position_notes

        async with Database(config.db_path) as db:
            notes = await get_position_notes(db, ticker, limit=limit)

        if not notes:
            console.print(f"[yellow]No notes found for {ticker}[/yellow]")
            return

        console.print(f"\n[bold]Position Notes: {ticker} ({len(notes)} notes)[/bold]\n")
        for n in reversed(notes):
            _print_note(n)
            console.print()

    _run(_notes())


@app.command(name="log-activity", hidden=True)
def log_activity(
    cycle: int = typer.Option(0, "--cycle", "-c", help="Cycle number"),
    agent: str = typer.Option("", "--agent", "-a", help="Agent name"),
    phase: str = typer.Option("", "--phase", help="Phase (start/complete/info/error)"),
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


@app.command(name="log-error", hidden=True)
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


@app.command(name="errors", rich_help_panel="Diagnostics")
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

        from gimmes.reporting.formatter import format_local_timestamp
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
                        format_local_timestamp(str(row["timestamp"])),
                        f"[{sev_color}]{sev}[/{sev_color}]",
                        row["category"],
                        row.get("error_code", ""),
                        row["message"][:50],
                        resolved,
                    )
                console.print(table)

    _run(_errors())


@app.command(name="resolve-error", hidden=True)
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


@app.command(rich_help_panel="Strategy")
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

        from gimmes.reporting.formatter import format_local_timestamp
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
                        format_local_timestamp(row["timestamp"], date_only=True),
                    )
                console.print(table)

    _run(_lesson())


@app.command(rich_help_panel="Strategy")
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

        from gimmes.reporting.formatter import format_local_timestamp
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
                    format_local_timestamp(row["timestamp"], date_only=True),
                    row["parameter_path"],
                    row["current_value"],
                    row["recommended_value"],
                    f"[{conf_color}]{conf}[/{conf_color}]",
                    row["analysis_type"],
                    f"[{status_color}]{row['status']}[/{status_color}]",
                )
            console.print(table)

    _run(_recs())


def _parse_recommendation_value(raw: str) -> object:
    """Convert a recommendation string value to the appropriate Python type."""
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        if raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        return raw


@app.command(rich_help_panel="Strategy")
def tune() -> None:
    """Interactively apply pending strategy recommendations."""
    config = load_config()

    async def _tune() -> None:
        from gimmes.config import save_config_value
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
                    typed_value = _parse_recommendation_value(row["recommended_value"])
                    save_config_value(
                        row["parameter_path"],
                        typed_value,
                        db_path=config.db_path,
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
                    f"\n[green]Applied {applied} change(s) to database[/green]"
                )
                console.print("[dim]Restart the trading loop for changes to take effect[/dim]")

    _run(_tune())


@app.command(rich_help_panel="Strategy")
def backtest(
    from_date: str = typer.Option(
        ..., "--from", help="Start date (YYYY-MM-DD)",
    ),
    to_date: str = typer.Option(
        ..., "--to", help="End date (YYYY-MM-DD)",
    ),
    balance: float = typer.Option(
        10_000, "--balance", "-b", help="Starting balance in dollars",
    ),
    edge: float = typer.Option(
        0.10, "--edge", "-e", help="Assumed edge over market price for Kelly sizing",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output results as JSON",
    ),
) -> None:
    """Backtest the gimme strategy on historical settled markets."""
    config = load_config()

    async def _backtest() -> None:
        import json
        from datetime import date

        from gimmes.backtest.engine import BacktestConfig, run_backtest
        from gimmes.backtest.report import backtest_result_to_json, format_backtest_report
        from gimmes.kalshi.client import KalshiClient

        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
        if start >= end:
            console.print("[red]--from must be before --to[/red]")
            raise typer.Exit(1)
        if balance <= 0:
            console.print("[red]--balance must be positive[/red]")
            raise typer.Exit(1)
        if edge <= 0:
            console.print("[red]--edge must be positive[/red]")
            raise typer.Exit(1)
        bt_config = BacktestConfig(
            start_date=start,
            end_date=end,
            starting_balance=balance,
            gimmes_config=config,
            assumed_edge=edge,
        )
        console.print(
            f"[dim]Running backtest: {start} to {end}, "
            f"${balance:,.0f} starting balance...[/dim]"
        )
        async with KalshiClient(config) as client:
            result = await run_backtest(client, bt_config)

        if json_output:
            console.print_json(json.dumps(backtest_result_to_json(result)))
        else:
            format_backtest_report(result, console)

    _run(_backtest())


@app.command(rich_help_panel="Market Research")
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


config_app = typer.Typer(
    name="config",
    help="Configuration — interactive wizard, get/set individual values.",
    invoke_without_command=True,
)
app.add_typer(config_app, rich_help_panel="Setup & Config")


@config_app.callback(invoke_without_command=True)
def config_callback(
    ctx: typer.Context,
    section: str | None = typer.Option(
        None, "--section", "-s",
        help="Jump to a specific section (paper, strategy, sizing, risk, orders, scanner, scoring)",
    ),
    new_only: bool = typer.Option(
        False, "--new-only",
        help="Only prompt for settings not yet saved in the database",
    ),
) -> None:
    """Interactive configuration wizard — walk through every setting."""
    if ctx.invoked_subcommand is None:
        from gimmes.config_wizard import run_config_wizard

        run_config_wizard(section_filter=section, new_only=new_only)


def _require_db() -> Path:
    """Return the database path, or exit with an error if it doesn't exist."""
    from gimmes.config import GIMMES_HOME

    db_path = GIMMES_HOME / "gimmes.db"
    if not db_path.exists():
        console.print(
            f"[red]Database not found at {db_path}[/red]\n"
            "Run [bold]gimmes init[/bold] first to create it."
        )
        raise typer.Exit(1)
    return db_path


@config_app.command(name="set")
def config_set(
    key: str = typer.Argument(help="Dotted config key (e.g. strategy.gimme_threshold)"),
    value: str = typer.Argument(help="New value"),
) -> None:
    """Set a single configuration value."""
    from gimmes.config import (
        _load_config_from_db,
        config_keys_in_db,
        save_config_value,
    )
    from gimmes.config_wizard import (
        _format_current,
        _get_current_value,
        _scoring_weights_total,
        validate_config_value,
    )

    db_path = _require_db()

    try:
        setting, parsed = validate_config_value(key, value)
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None

    overrides = _load_config_from_db(db_path)
    current = _get_current_value(overrides, key, setting.default)
    already_in_db = key in config_keys_in_db(db_path)

    # Always persist to DB so the override is pinned and won't revert
    # to a code default if defaults change in a future release.
    save_config_value(key, parsed, db_path=db_path)

    if parsed == current and already_in_db:
        console.print(f"[dim]{key} is already {_format_current(current, setting)}[/dim]")
        raise typer.Exit(0)

    if parsed == current:
        # Value matches but was not pinned in DB — now it is.
        console.print(
            f"[cyan]{key}[/cyan]: pinned at "
            f"[bold]{_format_current(parsed, setting)}[/bold]"
            f" [dim](was using code default)[/dim]"
        )
    else:
        console.print(
            f"[cyan]{key}[/cyan]: {_format_current(current, setting)} → "
            f"[bold]{_format_current(parsed, setting)}[/bold]"
        )

    # Auto-clear cached candidates when strategy params change
    if key.startswith("strategy."):
        import asyncio

        from gimmes.store.database import Database
        from gimmes.store.queries import clear_all_candidates

        async def _auto_clear() -> int:
            async with Database(db_path) as db:
                return await clear_all_candidates(db)

        cleared = asyncio.run(_auto_clear())
        if cleared:
            console.print(
                f"[yellow]Cleared {cleared} cached candidate(s)"
                f" — scores from prior strategy no longer apply[/yellow]"
            )

    # Warn if scoring weights no longer sum to 1.0
    if key.startswith("scoring.weights."):
        parts = key.split(".")
        d: dict = overrides
        for part in parts[:-1]:
            d = d.setdefault(part, {})
        d[parts[-1]] = parsed
        total = _scoring_weights_total(overrides)
        if abs(total - 1.0) > 0.01:
            console.print(
                f"[yellow]Warning: Scoring weights now sum to {total:.2f} "
                f"instead of 1.00.[/yellow]"
            )


@config_app.command(name="get")
def config_get(
    key: str | None = typer.Argument(None, help="Dotted config key (omit to show all)"),
) -> None:
    """Show current configuration value(s)."""
    from rich.table import Table

    from gimmes.config import SERIES_CATEGORIES, _load_config_from_db
    from gimmes.config_wizard import (
        _format_current,
        _get_current_value,
        _iter_sections,
        resolve_setting,
    )

    db_path = _require_db()
    overrides = _load_config_from_db(db_path)

    if key is not None:
        try:
            setting = resolve_setting(key)
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            raise typer.Exit(1) from None

        current = _get_current_value(overrides, key, setting.default)
        categories = SERIES_CATEGORIES if key == "scanner.series" else None
        formatted = _format_current(current, setting, full=True, categories=categories)
        console.print(f"[cyan]{key}[/cyan]: [bold]{formatted}[/bold]")
        console.print(f"[dim]Default: {_format_current(setting.default, setting)}[/dim]")
        console.print(f"[dim]{setting.description}[/dim]")
        return

    # Show all settings grouped by section
    table = Table(title="GIMMES Configuration", show_lines=True)
    table.add_column("Section", style="bold blue")
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="bold")
    table.add_column("Default", style="dim")

    for _section_key, section_name, _section_desc, settings in _iter_sections():
        for i, setting in enumerate(settings):
            current = _get_current_value(overrides, setting.key, setting.default)
            display = _format_current(current, setting)
            default_display = _format_current(setting.default, setting)
            section_label = section_name if i == 0 else ""
            table.add_row(section_label, setting.key, display, default_display)

    console.print(table)


def _resolve_list_field(key: str) -> tuple[Path, list]:
    """Resolve a config key as a list field and return (db_path, current_list).

    Exits with an error if the key is invalid or not a list type.
    """
    from gimmes.config import _load_config_from_db
    from gimmes.config_wizard import _get_current_value, resolve_setting

    db_path = _require_db()

    try:
        setting = resolve_setting(key)
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from None

    if setting.type != "list":
        console.print(f"[red]Error: {key} is not a list field. Use 'config set' instead.[/red]")
        raise typer.Exit(1)

    overrides = _load_config_from_db(db_path)
    current = _get_current_value(overrides, key, setting.default)
    if not isinstance(current, list):
        console.print(
            f"[red]Error: Stored value for {key!r} is not a list."
            " The configuration may be corrupted.[/red]"
        )
        console.print(
            f"[yellow]Reset it with 'gimmes config set {key} ...' "
            "before using add/remove.[/yellow]"
        )
        raise typer.Exit(1)
    return db_path, list(current)


@config_app.command(name="add")
def config_add(
    key: str = typer.Argument(help="Dotted config key for a list field (e.g. scanner.series)"),
    value: str = typer.Argument(help="Value to append"),
) -> None:
    """Add a value to a list-type configuration field."""
    from gimmes.config import save_config_value

    db_path, current_list = _resolve_list_field(key)

    if value in current_list:
        console.print(f"[dim]{value} is already in {key}[/dim]")
        raise typer.Exit(0)

    new_list = current_list + [value]
    save_config_value(key, new_list, db_path=db_path)
    console.print(f"Added [bold]{value}[/bold] to [cyan]{key}[/cyan] ({len(new_list)} items)")


@config_app.command(name="remove")
def config_remove(
    key: str = typer.Argument(help="Dotted config key for a list field (e.g. scanner.series)"),
    value: str = typer.Argument(help="Value to remove"),
) -> None:
    """Remove a value from a list-type configuration field."""
    from gimmes.config import save_config_value

    db_path, current_list = _resolve_list_field(key)

    if value not in current_list:
        console.print(f"[red]Error: {value} not found in {key}[/red]")
        raise typer.Exit(1)

    new_list = [item for item in current_list if item != value]
    save_config_value(key, new_list, db_path=db_path)
    console.print(f"Removed [bold]{value}[/bold] from [cyan]{key}[/cyan] ({len(new_list)} items)")


@app.command(rich_help_panel="Setup & Config")
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


@app.command(rich_help_panel="Dashboard")
def clubhouse(
    port: int = typer.Option(1919, "--port", "-p", help="Port number"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't auto-open browser"),
) -> None:
    """Launch the Clubhouse web dashboard (standalone)."""
    from gimmes.clubhouse.server import run_standalone

    config = load_config()
    run_standalone(port=port, db_path=config.db_path, open_browser=not no_browser)


# ---------------------------------------------------------------------------
# Claude agent sessions (tour guide, caddie shop)
# ---------------------------------------------------------------------------


def _get_username() -> str:
    """Return the OS username, or ``'there'`` as a safe fallback."""
    import getpass

    try:
        return getpass.getuser()
    except Exception:
        return "there"


def _launch_claude_agent(
    agent: str,
    session_name: str,
    *,
    opening_message: str,
    closing_message: str,
    interrupt_message: str,
    allowed_tools: list[str],
    initial_prompt: str | None = None,
) -> None:
    """Find the 'claude' CLI and launch a named agent session.

    *allowed_tools* lists tool names to pre-approve via ``--allowedTools``
    so the agent can run without permission prompts.  Must match the
    agent's frontmatter ``tools:`` list.

    If *initial_prompt* is provided it is passed as a positional argument
    so the agent session opens in interactive mode with pre-filled input.

    Handles missing binary, KeyboardInterrupt, OSError, and non-zero exit.
    """
    import shutil

    claude_path = shutil.which("claude")
    if not claude_path:
        console.print(
            "[red]Error: 'claude' CLI not found. Install Claude Code first.[/red]"
        )
        raise typer.Exit(1)

    project_root = Path(__file__).resolve().parent.parent.parent
    console.print(opening_message)

    cmd = [claude_path]
    if initial_prompt is not None:
        cmd.append(initial_prompt)
    cmd.extend([
        "--agent", agent,
        "--name", session_name,
        "--allowedTools", ",".join(allowed_tools),
    ])

    try:
        result = subprocess.run(
            cmd,
            cwd=project_root,
            check=False,
        )
    except KeyboardInterrupt:
        console.print(interrupt_message)
        raise typer.Exit(130)
    except OSError as exc:
        console.print(
            f"[red]Failed to launch Claude Code: {exc}[/red]\n"
            "[yellow]Ensure 'claude' is installed and executable.[/yellow]"
        )
        raise typer.Exit(1)

    if result.returncode != 0:
        console.print(
            f"[red]{session_name} exited with an error (code {result.returncode}).[/red]"
        )
        raise typer.Exit(1)

    console.print(closing_message)


@app.command(name="tour_guide", rich_help_panel="Setup & Config")
def tour_guide() -> None:
    """Launch The Starter — an interactive GIMMES product tour."""
    _launch_claude_agent(
        "Starter", "GIMMES Tour",
        opening_message=(
            "\n[bold green]Starting the GIMMES tour...[/bold green]\n"
            "[dim]The Starter will guide you through the system.[/dim]\n"
        ),
        closing_message="\n[yellow]Tour ended. Happy trading![/yellow]",
        interrupt_message="\n[dim]Tour interrupted.[/dim]",
        allowed_tools=["Bash", "Read", "Glob", "Grep", "WebSearch", "WebFetch"],
        initial_prompt=f"Hi, I am {_get_username()}",
    )


@app.command(name="caddie_shop", rich_help_panel="Setup & Config")
def caddie_shop() -> None:
    """Launch The Caddie Shop — conversational configuration advisor."""
    _launch_claude_agent(
        "Caddie Shop", "GIMMES Caddie Shop",
        opening_message=(
            "\n[bold green]Opening The Caddie Shop...[/bold green]\n"
            "[dim]The Caddie Shop attendant will help tune your settings.[/dim]\n"
        ),
        closing_message="\n[yellow]Caddie Shop session ended.[/yellow]",
        interrupt_message="\n[dim]Caddie Shop closed.[/dim]",
        allowed_tools=["Bash", "Read", "Glob", "Grep"],
        initial_prompt=f"Hi, I am {_get_username()}",
    )


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
        _championship_gate(config)

    _set_mode(target)

    # Show updated banner
    new_config = load_config()
    _mode_banner(new_config)
    console.print(f"\nSwitched from [bold]{current}[/bold] → [bold]{target}[/bold]")


# ---------------------------------------------------------------------------
# Autonomous loop commands
# ---------------------------------------------------------------------------


def _parse_dollars(raw: str) -> float | None:
    """Parse a user-entered dollar string like '$1,500' into a float, or None on failure."""
    try:
        return float(raw.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _championship_gate(config: GimmesConfig) -> None:
    """Championship entry gate: confirm real-money risk, then set/confirm bankroll.

    All championship entry points must call this before proceeding.
    Aborts on decline.
    """
    from gimmes.config import save_config_value

    console.print("\n[red bold]⚠  CHAMPIONSHIP MODE — REAL MONEY ⚠[/red bold]")
    console.print(
        "This will trade with real money on Kalshi autonomously.\n"
        "The system will scan markets, research candidates, and execute trades\n"
        "without asking for confirmation on each order.\n"
    )
    if not typer.confirm("Are you sure you want to trade with real money?"):
        raise typer.Abort()

    current_bankroll = config.risk.bankroll_real
    if current_bankroll <= 0:
        console.print(
            "\n[bold]How much capital are you willing to have deployed?[/bold]"
        )
        console.print(
            "[dim]This is the maximum total cost basis across all open positions.[/dim]"
        )
        while True:
            raw = typer.prompt("  Championship bankroll ($)", default="", show_default=False)
            if raw.strip() == "":
                console.print("  [red]You must set a bankroll before trading.[/red]")
                continue
            value = _parse_dollars(raw)
            if value is None:
                console.print("  [red]Enter a number (e.g. 500).[/red]")
                continue
            if value <= 0:
                console.print("  [red]Bankroll must be greater than $0.[/red]")
                continue
            if value > 1_000_000:
                console.print("  [red]Bankroll cannot exceed $1,000,000.[/red]")
                continue
            break
    else:
        console.print(
            f"\n[bold]Your championship bankroll is ${current_bankroll:,.2f}[/bold]"
        )
        raw = typer.prompt(
            "  Keep this or enter new amount ($)",
            default=str(current_bankroll),
        )
        value = _parse_dollars(raw)
        if value is None:
            console.print("[red]Invalid amount.[/red]")
            raise typer.Abort()
        if value <= 0:
            console.print("[red]Bankroll must be greater than $0.[/red]")
            raise typer.Abort()
        if value > 1_000_000:
            console.print("[red]Bankroll cannot exceed $1,000,000.[/red]")
            raise typer.Abort()

    if value != current_bankroll:
        try:
            save_config_value("risk.bankroll_real", value, db_path=config.db_path)
        except Exception as exc:
            console.print(
                f"[red]Error: Could not save championship bankroll: {exc}[/red]"
            )
            raise typer.Exit(1)
        console.print(f"  [green]Championship bankroll set to ${value:,.2f}[/green]")


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
        _championship_gate(config)

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


def _detect_rate_limit(output: bytes) -> tuple[bool, int]:
    """Check cycle output for rate limit errors and parse reset time.

    Returns ``(is_rate_limited, pause_seconds)`` where *pause_seconds* is
    the number of seconds to wait until the reset time, or a fallback of
    30 minutes if the reset time cannot be parsed.
    """
    import re
    from datetime import datetime

    try:
        text = output.decode("utf-8", errors="replace")
    except Exception:
        return False, 0

    # Match messages like "You've hit your limit · resets 5pm (America/New_York)"
    # or "you've hit your limit" anywhere in the output
    pattern = re.compile(
        r"you'?ve hit your limit(?:.*?resets?\s+(\d{1,2}(?::\d{2})?\s*[ap]m)"
        r"(?:\s*\(([^)]+)\))?)?",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        # Also detect HTTP 429 messages from the CLI
        if re.search(r"\b429\b.*(?:rate.limit|too.many.requests)", text, re.IGNORECASE):
            return True, 1800  # 30 min fallback
        return False, 0

    reset_time_str = match.group(1)
    tz_name = match.group(2)

    if not reset_time_str:
        return True, 1800  # 30 min fallback

    # Parse the reset time
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("America/New_York")
        now = datetime.now(tz)

        # Parse "5pm" or "5:00pm" style times
        reset_time_str = reset_time_str.strip()
        for fmt in ("%I:%M%p", "%I%p"):
            try:
                parsed = datetime.strptime(reset_time_str.upper(), fmt)
                reset_dt = now.replace(
                    hour=parsed.hour, minute=parsed.minute,
                    second=0, microsecond=0,
                )
                if reset_dt <= now:
                    # Reset time is tomorrow
                    from datetime import timedelta
                    reset_dt += timedelta(days=1)
                pause = int((reset_dt - now).total_seconds()) + 60  # 1 min buffer
                return True, max(pause, 60)
            except ValueError:
                continue
    except Exception:
        pass

    return True, 1800  # 30 min fallback


@functools.cache
def _transient_api_patterns() -> tuple[re.Pattern[str], ...]:
    sources = (
        r"API Error:\s*5\d{2}\b",
        r"\boverloaded_error\b",
        r"\bOverloaded\b",
        r"\btimed?[\s_-]?out\b",
        r"\bconnection\s+(?:error|reset|refused|aborted)\b",
        r"\bECONNRESET\b|\bETIMEDOUT\b",
        r"\b(?:read|write)\s+timeout\b",
    )
    return tuple(re.compile(p, re.IGNORECASE) for p in sources)


def _detect_api_error(output: bytes) -> tuple[bool, bool, str]:
    """Classify the Claude SDK result envelope for API-level errors.

    Returns ``(had_error, is_transient, detail)`` where:
    - ``had_error`` is True when the terminal ``type: result`` event has
      ``is_error: true`` — the cycle failed inside the SDK even when the
      subprocess returncode is 0.
    - ``is_transient`` is True when the error string matches a known
      retryable pattern (5xx, overloaded, timeout, connection reset).
      Callers should still treat non-transient errors as failures — the
      circuit breaker bounds retry on persistent/permanent errors.
    - ``detail`` is the serialized error content for logging.
    """
    import json as _json

    if not output or not output.strip():
        return False, False, ""

    event: dict[str, object] | None = None
    lines = output.strip().split(b"\n")
    if len(lines) > 1:
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = _json.loads(line)
            except (_json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(parsed, dict) and parsed.get("type") == "result":
                event = parsed
                break
    else:
        try:
            parsed = _json.loads(output.strip())
        except (_json.JSONDecodeError, UnicodeDecodeError):
            return False, False, ""
        if isinstance(parsed, dict) and parsed.get("type") == "result":
            event = parsed

    if event is None or not event.get("is_error"):
        return False, False, ""

    raw = event.get("result")
    if raw is None:
        detail = str(event.get("subtype") or "unknown")
    elif isinstance(raw, str):
        detail = raw
    else:
        try:
            detail = _json.dumps(raw)
        except (TypeError, ValueError):
            detail = str(raw)

    is_transient = any(p.search(detail) for p in _transient_api_patterns())
    return True, is_transient, detail


def _apply_failure_backoff(
    consecutive_failures: int,
    max_consecutive_failures: int,
) -> bool:
    """Check circuit breaker after a cycle failure; apply backoff if not.

    Returns True if the caller should halt the loop (breaker tripped),
    False if the caller should ``continue`` after the backoff sleep.
    Callers increment ``consecutive_failures`` and print their own
    branch-specific failure message before invoking this helper.
    """
    import time as _time

    if (max_consecutive_failures > 0
            and consecutive_failures >= max_consecutive_failures):
        console.print(
            f"[red bold]Circuit breaker tripped:"
            f" {max_consecutive_failures} consecutive"
            f" failures. Halting autonomous loop.[/red bold]"
        )
        return True
    backoff = min(30 * 2 ** (consecutive_failures - 1), 240)
    console.print(
        f"[dim]Backoff: retrying in {backoff}s...[/dim]"
    )
    _time.sleep(backoff)
    return False


def _check_code_staleness(
    project_root: Path,
    startup_commit: str | None,
) -> tuple[str, bool, str | None]:
    """Check if the running code has changed since startup.

    Returns ``(current_commit, is_stale, message)``.  Fails open —
    any git error returns ``("", False, None)``.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return "", False, None
        current = result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "", False, None

    if startup_commit is None:
        return current, False, None

    if current != startup_commit:
        return current, True, (
            f"Installed code changed: startup={startup_commit[:8]}"
            f" current={current[:8]}."
            f" Restart to pick up changes."
        )
    return current, False, None


def _check_remote_staleness(
    project_root: Path,
    current_commit: str,
) -> str | None:
    """Check if remote has commits ahead of *current_commit*.

    Returns a warning message or ``None`` if up to date.
    Uses ``git ls-remote`` (read-only, no fetch).
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "origin", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        remote_sha = (
            result.stdout.split()[0] if result.stdout.strip() else None
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    if remote_sha and remote_sha != current_commit:
        return (
            f"Remote differs from local:"
            f" local={current_commit[:8]}"
            f" remote={remote_sha[:8]}."
            f" Run `gimmes update` and restart."
        )
    return None


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
    monitor_interval: int = 3600,
    no_dashboard: bool = False,
    max_consecutive_failures: int = 5,
) -> None:
    """Run the Caddie Master orchestrator agent via claude --agent in a loop.

    Each cycle checks a trade window calendar to determine cycle type:

    - **Full cycle** (in trade window): runs the complete pipeline
      (Monitor → Scout → Caddie → Closer → Scorecard), sleeps
      ``pause_seconds`` after.
    - **Monitor-only cycle** (outside trade window): runs only Monitor
      and Groundskeeper, sleeps until the next trade window or
      ``monitor_interval``, whichever is sooner.

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
    console.print(f"Pause between cycles (in trade window): {pause_seconds}s")
    console.print(f"Monitor interval (outside windows): {monitor_interval}s")
    if max_cycles > 0:
        console.print(f"Max cycles: {max_cycles}")
    console.print("Press Ctrl+C to stop\n")

    cycle = get_max_global_cycle(config.db_path)
    cycles_run = 0
    consecutive_failures = 0
    session_status = "stopped"

    # Code staleness detection
    _startup_commit: str | None = None
    _last_remote_check: float = 0.0
    _staleness_warned = False

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
        from gimmes.strategy.calendar import (
            ET,
            is_in_trade_window,
            next_trade_window,
            position_window,
            seconds_until_next_window,
        )

        async def _check_position_windows() -> tuple[bool, str | None]:
            """Check if any open position is within a settlement window.

            Returns (in_position_window, ticker) if now falls within an
            ad-hoc position window for any held position with a known
            close_time.  When positions lack a cached close_time, a
            short-lived API client fetches and caches it.
            """
            import logging
            from datetime import datetime as _dt

            from gimmes.store.database import Database
            from gimmes.store.queries import (
                get_position_close_times,
                get_position_tickers,
            )

            _log = logging.getLogger("gimmes.position_window")
            pos_table = config.position_table

            now = _dt.now(ET)
            async with Database(config.db_path) as db:
                close_times = await get_position_close_times(
                    db, table=pos_table,
                )
                cached_tickers = {t for t, _ in close_times}
                all_tickers = await get_position_tickers(
                    db, table=pos_table,
                )
                missing = all_tickers - cached_tickers

            # Backfill close_times for positions that lack them
            if missing:
                try:
                    from gimmes.kalshi.client import KalshiClient
                    from gimmes.kalshi.markets import get_market

                    async with KalshiClient(config) as client:
                        async with Database(config.db_path) as db:
                            for ticker in missing:
                                try:
                                    mkt = await get_market(client, ticker)
                                    if mkt.close_time:
                                        await db.conn.execute(
                                            f"UPDATE {pos_table}"  # noqa: S608
                                            " SET close_time = ?"
                                            " WHERE ticker = ?",
                                            (mkt.close_time.isoformat(),
                                             ticker),
                                        )
                                        close_times.append(
                                            (ticker, mkt.close_time)
                                        )
                                except Exception:
                                    _log.warning(
                                        "Failed to backfill close_time"
                                        " for %s",
                                        ticker, exc_info=True,
                                    )
                                    continue
                            await db.conn.commit()
                except Exception:
                    _log.warning(
                        "Position window backfill failed",
                        exc_info=True,
                    )

            for ticker, ct in close_times:
                pw_open, pw_close = position_window(ct)
                if pw_open <= now < pw_close:
                    return True, ticker
            return False, None

        while max_cycles == 0 or cycles_run < max_cycles:
            cycle += 1
            cycles_run += 1

            # Code staleness check
            _cur, _stale, _stale_msg = _check_code_staleness(
                project_root, _startup_commit,
            )
            if _startup_commit is None and _cur:
                _startup_commit = _cur
            if _stale and _stale_msg:
                console.print(
                    f"[red bold]CODE STALE: {_stale_msg}[/red bold]"
                )
            elif _cur:
                if _staleness_warned:
                    # Re-print cached warning each cycle
                    console.print(
                        "[yellow bold]UPDATE AVAILABLE:"
                        " Run `gimmes update` and restart."
                        "[/yellow bold]"
                    )
                else:
                    # Throttled remote check — skip during trade
                    # windows to avoid blocking time-sensitive cycles
                    _now_ts = time.time()
                    _in_tw = is_in_trade_window()[0]
                    if (
                        not _in_tw
                        and _now_ts - _last_remote_check
                        >= monitor_interval
                    ):
                        _remote_msg = _check_remote_staleness(
                            project_root, _cur,
                        )
                        _last_remote_check = _now_ts
                        if _remote_msg:
                            console.print(
                                f"[yellow bold]UPDATE AVAILABLE:"
                                f" {_remote_msg}[/yellow bold]"
                            )
                            _staleness_warned = True

            # Determine cycle type based on trade window calendar
            in_window, release_name, _secs_to_close = is_in_trade_window()
            if in_window:
                cycle_type = "full"
                cycle_prompt = "Run one trading cycle."
                post_sleep = pause_seconds
                console.print(
                    f"[cyan]--- Cycle {cycle} ---[/cyan]"
                    f" [green bold][TRADE WINDOW: {release_name}][/green bold]"
                )
            else:
                # Check if any held position is near settlement
                _pw_result = asyncio.run(
                    _check_position_windows()
                )
                in_pos_window, pos_ticker = _pw_result or (False, None)
                if in_pos_window:
                    cycle_type = "full"
                    cycle_prompt = "Run one trading cycle."
                    post_sleep = pause_seconds
                    console.print(
                        f"[cyan]--- Cycle {cycle} ---[/cyan]"
                        f" [magenta bold][POSITION WINDOW:"
                        f" {pos_ticker}][/magenta bold]"
                    )
                else:
                    cycle_type = "monitor"
                    _, next_name = next_trade_window()
                    secs_to_next = seconds_until_next_window()
                    post_sleep = min(secs_to_next, monitor_interval)
                    h, remainder = divmod(secs_to_next, 3600)
                    m, _ = divmod(remainder, 60)
                    console.print(
                        f"[cyan]--- Cycle {cycle} ---[/cyan]"
                        f" [yellow][MONITOR ONLY — next window:"
                        f" {next_name} in {h}h {m:02d}m][/yellow]"
                    )
                    cycle_prompt = (
                        "Run a MONITOR-ONLY cycle. Only run Steps 0, 0.5, 1, 2,"
                        " 6.5, and 8. Skip Scout, Caddie, Closer, Scorecard,"
                        " and Pro."
                    )

            update_session_cycle(config.db_path, session_id, cycle)

            env["GIMMES_CYCLE"] = str(cycle)
            env["GIMMES_CYCLE_TYPE"] = cycle_type
            log_path = logs_dir / f"cycle-{cycle:03d}.json"
            try:
                proc = subprocess.Popen(
                    [
                        claude_path,
                        "--agent", "Caddie Master",
                        "-p", cycle_prompt,
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

                # --- Rate limit detection ---
                is_rate_limited, rl_pause = _detect_rate_limit(
                    stdout_bytes,
                )
                if is_rate_limited:
                    h, remainder = divmod(rl_pause, 3600)
                    m, _ = divmod(remainder, 60)
                    console.print(
                        f"[red bold]Rate limit detected on cycle"
                        f" {cycle}. Pausing for"
                        f" {h}h {m:02d}m until reset."
                        f"[/red bold]"
                    )
                    time.sleep(rl_pause)
                    consecutive_failures = 0
                    continue

                # --- Anthropic API error detection ---
                # SDK returns is_error=true with returncode=0 for API errors.
                # Route every such case through the failure/backoff path so
                # the circuit breaker bounds retry on persistent errors.
                had_api_error, is_transient, api_detail = _detect_api_error(
                    stdout_bytes,
                )
                if had_api_error:
                    consecutive_failures += 1
                    kind = "transient API error" if is_transient else "API error"
                    snippet = api_detail[:200].replace("\n", " ")
                    console.print(
                        f"[yellow]Cycle {cycle} hit {kind}:"
                        f" {snippet}"
                        f" (failure {consecutive_failures}"
                        f"/{max_consecutive_failures})[/yellow]"
                    )
                    if _apply_failure_backoff(
                        consecutive_failures, max_consecutive_failures,
                    ):
                        session_status = "crashed"
                        break
                    continue

            except subprocess.TimeoutExpired:
                _kill_process_group(proc)
                consecutive_failures += 1
                console.print(
                    f"[yellow]Cycle {cycle} timed out after"
                    f" {config.strategy.cycle_timeout}s"
                    f" (failure {consecutive_failures}"
                    f"/{max_consecutive_failures})[/yellow]"
                )
                if _apply_failure_backoff(
                    consecutive_failures, max_consecutive_failures,
                ):
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
                if _apply_failure_backoff(
                    consecutive_failures, max_consecutive_failures,
                ):
                    session_status = "crashed"
                    break
                continue
            else:
                consecutive_failures = 0

            if max_cycles > 0 and cycles_run >= max_cycles:
                break

            # Recalculate sleep after cycle completes — a trade window
            # may have opened during the cycle execution.
            if cycle_type == "monitor":
                fresh_secs = seconds_until_next_window()
                post_sleep = min(fresh_secs, monitor_interval)
            console.print(f"[dim]Next cycle in {post_sleep}s...[/dim]")
            time.sleep(post_sleep)
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
    monitor_interval: int = typer.Option(
        3600, "--monitor-interval", min=60,
        help="Seconds between monitor-only cycles outside trade windows (default 3600)",
    ),
    no_dashboard: bool = typer.Option(
        False, "--no-dashboard", help="Disable auto-start of Clubhouse dashboard",
    ),
) -> None:
    """Switch to Driving Range mode and start autonomous trading loop (paper trading)."""
    _set_mode("driving_range")
    _autonomous_loop("driving_range", max_cycles=cycles, pause_seconds=pause,
                     monitor_interval=monitor_interval, no_dashboard=no_dashboard)


@app.command(name="championship", rich_help_panel="Autonomous Trading")
def championship(
    cycles: int = typer.Option(
        0, "--cycles", "-n", min=0, help="Max cycles to run (0=unlimited)",
    ),
    pause: int = typer.Option(60, "--pause", min=0, help="Seconds between cycles (default 60)"),
    monitor_interval: int = typer.Option(
        3600, "--monitor-interval", min=60,
        help="Seconds between monitor-only cycles outside trade windows (default 3600)",
    ),
    no_dashboard: bool = typer.Option(
        False, "--no-dashboard", help="Disable auto-start of Clubhouse dashboard",
    ),
) -> None:
    """Switch to Championship mode and start autonomous trading loop (REAL MONEY)."""
    config = load_config()
    _championship_gate(config)
    _set_mode("championship")
    _autonomous_loop("championship", max_cycles=cycles, pause_seconds=pause,
                     monitor_interval=monitor_interval, no_dashboard=no_dashboard)


@app.command(name="monitor", rich_help_panel="Diagnostics")
def monitor_cmd(
    action: str = typer.Argument(
        "status",
        help="on | off | status | run | quiet | notify",
    ),
) -> None:
    """Manage the hourly driving range health monitor.

    \b
    Actions:
      on      Enable the cron job (weekday trade window hours)
      off     Disable the cron job
      status  Show if monitor is active
      run     Run a single check now
      quiet   Disable iMessage alerts (log only)
      notify  Re-enable iMessage alerts
    """
    import subprocess

    project_root = Path(__file__).resolve().parent.parent.parent
    script = project_root / "bin" / "monitor.sh"
    config_file = GIMMES_HOME / "monitor.conf"
    cron_tag = "# GIMMES_MONITOR"

    # Cron schedule: every hour 11 AM - 6 PM PT (6 PM - 1 AM UTC) weekdays
    cron_line = (
        f"0 18,19,20,21,22,23,0,1 * * 1-5"
        f" {script} {cron_tag}"
    )

    if action == "on":
        # Remove existing entry, add fresh
        try:
            result = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True,
            )
            existing = result.stdout if result.returncode == 0 else ""
        except FileNotFoundError:
            existing = ""

        lines = [
            l for l in existing.splitlines()
            if cron_tag not in l
        ]
        lines.append(cron_line)
        subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            text=True, check=True,
        )
        console.print("[green]Monitor enabled.[/green] Runs hourly during weekday trade windows.")
        console.print(f"[dim]Script: {script}[/dim]")

    elif action == "off":
        try:
            result = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True,
            )
            if result.returncode != 0:
                console.print("[yellow]No crontab found.[/yellow]")
                return
            lines = [
                l for l in result.stdout.splitlines()
                if cron_tag not in l
            ]
            subprocess.run(
                ["crontab", "-"],
                input="\n".join(lines) + "\n",
                text=True, check=True,
            )
        except FileNotFoundError:
            pass
        console.print("[yellow]Monitor disabled.[/yellow]")

    elif action == "status":
        try:
            result = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True,
            )
            active = (
                result.returncode == 0
                and cron_tag in result.stdout
            )
        except FileNotFoundError:
            active = False

        quiet = config_file.exists() and "quiet" in config_file.read_text()

        if active:
            mode = "quiet (log only)" if quiet else "notify (iMessage alerts)"
            console.print(f"[green]Monitor is ON[/green] — {mode}")
        else:
            console.print("[yellow]Monitor is OFF[/yellow]")

    elif action == "run":
        quiet_flag = (
            ["--quiet"]
            if config_file.exists() and "quiet" in config_file.read_text()
            else []
        )
        result = subprocess.run(
            [str(script)] + quiet_flag, check=False,
        )
        if result.returncode == 0:
            console.print("[green]All checks passed.[/green]")
        else:
            console.print("[yellow]Issues found — check monitor log.[/yellow]")

    elif action == "quiet":
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("quiet\n")
        console.print("[yellow]iMessage alerts disabled.[/yellow] Monitor will log only.")

    elif action == "notify":
        if config_file.exists():
            config_file.write_text("notify\n")
        console.print("[green]iMessage alerts enabled.[/green]")

    else:
        console.print(f"[red]Unknown action: {action}[/red]")
        console.print("Use: on | off | status | run | quiet | notify")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
