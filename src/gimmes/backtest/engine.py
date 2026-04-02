"""Backtest engine — replays the gimme strategy on historical settled markets."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from gimmes.config import GimmesConfig
from gimmes.kalshi.client import KalshiClient
from gimmes.kalshi.historical import Candle, get_candlesticks
from gimmes.kalshi.markets import list_all_markets
from gimmes.models.market import Market, MarketStatus, Orderbook, OrderbookLevel
from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide
from gimmes.paper.fill_simulator import simulate_fill
from gimmes.strategy.fees import DEFAULT_FEE_MULTIPLIERS, FeeMultipliers
from gimmes.strategy.kelly import position_size
from gimmes.strategy.scanner import effective_price, filter_markets
from gimmes.strategy.scorer import quick_score

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BacktestConfig:
    """Configuration for a backtest run."""

    start_date: date
    end_date: date
    starting_balance: float
    gimmes_config: GimmesConfig
    assumed_edge: float = 0.10  # Edge premium over market price for Kelly sizing


@dataclass
class BacktestTrade:
    """A completed trade in the backtest."""

    ticker: str
    title: str
    side: str
    count: int
    entry_price: float
    cost_basis: float
    fees: float
    result: str
    payout: float
    pnl: float
    entry_time: datetime | None = None
    settle_time: datetime | None = None


@dataclass
class BacktestResult:
    """Full result of a backtest run."""

    config: BacktestConfig
    trades: list[BacktestTrade]
    final_balance: float
    equity_curve: list[tuple[str, float]]  # (ISO timestamp, equity)
    markets_scanned: int
    markets_passed_filter: int
    markets_scored: int
    markets_traded: int


@dataclass
class _PendingTrade:
    """A trade identified in Pass 1, to be executed chronologically in Pass 2."""

    ticker: str
    title: str
    side: str
    count: int
    vwap: float
    fees: float
    entry_time: datetime
    settle_time: datetime
    result: str


# ---------------------------------------------------------------------------
# Backtest ledger — in-memory position/balance tracker
# ---------------------------------------------------------------------------


@dataclass
class _OpenPosition:
    ticker: str
    title: str
    side: str
    count: int
    entry_price: float
    cost_basis: float
    fees: float
    entry_time: datetime | None = None


class BacktestLedger:
    """Lightweight in-memory ledger for backtest state."""

    def __init__(self, starting_balance: float) -> None:
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.positions: dict[str, _OpenPosition] = {}
        self.trades: list[BacktestTrade] = []
        self.equity_curve: list[tuple[str, float]] = []

    def buy(
        self,
        ticker: str,
        title: str,
        side: str,
        count: int,
        price: float,
        fees: float,
        entry_time: datetime | None = None,
    ) -> bool:
        """Open a position. Returns False if insufficient balance."""
        cost = count * price + fees
        if cost > self.balance:
            return False
        self.balance -= cost
        self.positions[ticker] = _OpenPosition(
            ticker=ticker,
            title=title,
            side=side,
            count=count,
            entry_price=price,
            cost_basis=cost,
            fees=fees,
            entry_time=entry_time,
        )
        return True

    def settle(
        self,
        ticker: str,
        result: str,
        settle_time: datetime | None = None,
    ) -> BacktestTrade | None:
        """Settle an open position. Returns the trade record or None."""
        pos = self.positions.pop(ticker, None)
        if pos is None:
            return None

        won = pos.side == result
        payout = pos.count * 1.0 if won else 0.0
        pnl = payout - pos.cost_basis
        self.balance += payout

        trade = BacktestTrade(
            ticker=ticker,
            title=pos.title,
            side=pos.side,
            count=pos.count,
            entry_price=pos.entry_price,
            cost_basis=pos.cost_basis,
            fees=pos.fees,
            result=result,
            payout=payout,
            pnl=pnl,
            entry_time=pos.entry_time,
            settle_time=settle_time,
        )
        self.trades.append(trade)
        return trade

    def snapshot(self, timestamp: str) -> None:
        """Record an equity snapshot (balance only — all positions settle)."""
        self.equity_curve.append((timestamp, self.balance))


# ---------------------------------------------------------------------------
# Orderbook synthesis
# ---------------------------------------------------------------------------


def synthesize_orderbook(ticker: str, candle: Candle, depth: int = 100) -> Orderbook:
    """Build a minimal Orderbook from candlestick bid/ask close prices.

    Uses the candle's yes_bid_close as a YES bid level and derives a NO bid
    level from yes_ask_close (NO bid = 1 - YES ask). Assigns synthetic depth.
    """
    yes_bid_price = candle.yes_bid_close
    yes_ask_price = candle.yes_ask_close

    yes_bids = []
    no_bids = []

    if yes_bid_price > 0:
        yes_bids.append(OrderbookLevel(price=yes_bid_price, quantity=depth))

    if yes_ask_price > 0:
        # NO bid = 1 - YES ask
        no_bid_price = round(1.0 - yes_ask_price, 2)
        if no_bid_price > 0:
            no_bids.append(OrderbookLevel(price=no_bid_price, quantity=depth))

    return Orderbook(ticker=ticker, yes_bids=yes_bids, no_bids=no_bids)


# ---------------------------------------------------------------------------
# Entry candle selection
# ---------------------------------------------------------------------------


def pick_entry_candle(
    candles: list[Candle],
    min_price: float,
    max_price: float,
) -> Candle | None:
    """Pick the last daily candle where the price falls in the target range.

    This simulates "we would have entered the market on this day."
    """
    for candle in reversed(candles):
        price = candle.price_close
        if min_price <= price <= max_price:
            return candle
    return None


# ---------------------------------------------------------------------------
# Adapt historical markets for filter_markets()
# ---------------------------------------------------------------------------


def _adapt_for_filter(
    markets: list[Market],
    max_days: float = 90.0,
) -> list[Market]:
    """Adapt settled markets so they pass through filter_markets().

    Sets status to ACTIVE and close_time to a synthetic future value
    within the configured max_days_to_resolution window.
    """
    days = max(1.0, min(max_days - 1, 30.0))
    future = datetime.now(UTC) + timedelta(days=days)
    adapted = []
    for m in markets:
        adapted.append(m.model_copy(update={
            "status": MarketStatus.ACTIVE,
            "close_time": future,
            "expiration_time": future,
        }))
    return adapted


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_backtest(
    client: KalshiClient,
    config: BacktestConfig,
    *,
    fees: FeeMultipliers = DEFAULT_FEE_MULTIPLIERS,
) -> BacktestResult:
    """Run a backtest over historical settled markets.

    Algorithm:
    1. Fetch all settled historical markets
    2. Filter by scanner criteria (price, volume, OI)
    3. Score with quick_score, gate on gimme_threshold
    4. For each qualifying market, fetch candles, simulate entry, settle
    5. Return aggregated results
    """
    gc = config.gimmes_config
    threshold = gc.strategy.gimme_threshold
    ledger = BacktestLedger(config.starting_balance)

    # --- 1. Fetch settled markets per series via live API ---
    start_dt = datetime(
        config.start_date.year, config.start_date.month,
        config.start_date.day, tzinfo=UTC,
    )
    end_dt = datetime(
        config.end_date.year, config.end_date.month,
        config.end_date.day, 23, 59, 59, tzinfo=UTC,
    )
    series_list = gc.scanner.series or []
    logger.info(
        "Fetching settled markets for %d series via live API...",
        len(series_list),
    )
    all_markets: list[Market] = []
    for series in series_list:
        try:
            markets = await list_all_markets(
                client, status="settled", series_ticker=series,
            )
            all_markets.extend(markets)
        except Exception:
            logger.warning("Failed to fetch series %s", series, exc_info=True)

    # Filter to date range and known result
    def _in_range(m: Market) -> bool:
        if m.result not in ("yes", "no") or m.close_time is None:
            return False
        ct = m.close_time if m.close_time.tzinfo else m.close_time.replace(tzinfo=UTC)
        return start_dt <= ct <= end_dt

    settled = [m for m in all_markets if _in_range(m)]
    logger.info(
        "Fetched %d markets, %d settled in date range",
        len(all_markets), len(settled),
    )

    # --- 2. Filter ---
    adapted = _adapt_for_filter(settled, max_days=gc.scanner.max_days_to_resolution)
    passed = filter_markets(adapted, gc)

    # Build a lookup from adapted ticker back to original market (with result)
    original_by_ticker = {m.ticker: m for m in settled}

    # --- 3. Score ---
    scored: list[tuple[Market, Market, float]] = []  # (adapted, original, score)
    for m in passed:
        s = quick_score(m, gc)
        if s >= threshold:
            orig = original_by_ticker[m.ticker]
            scored.append((m, orig, s))

    # Sort by close_time (use original) for chronological processing
    scored.sort(key=lambda x: x[1].close_time or datetime.min.replace(tzinfo=UTC))

    logger.info(
        "Filter: %d passed, %d scored above threshold (%.0f)",
        len(passed), len(scored), threshold,
    )

    # --- 4. Pass 1: identify qualifying trades (no balance changes) ---
    pending: list[_PendingTrade] = []
    side = gc.strategy.side
    if side == "no":
        min_cp = round(1 - gc.strategy.max_market_price, 4)
        max_cp = round(1 - gc.strategy.min_market_price, 4)
    else:
        min_cp = gc.strategy.min_market_price
        max_cp = gc.strategy.max_market_price

    for _, orig_m, _score in scored:
        close_ts = int(orig_m.close_time.timestamp()) if orig_m.close_time else 0
        if close_ts == 0:
            continue

        start_ts = close_ts - (90 * 86400)
        try:
            candles = await get_candlesticks(
                client, orig_m.ticker,
                start_ts=start_ts, end_ts=close_ts,
                period_interval=1440,
            )
        except Exception:
            logger.debug("Failed to fetch candles for %s", orig_m.ticker, exc_info=True)
            continue

        if not candles:
            continue

        entry_candle = pick_entry_candle(candles, min_cp, max_cp)
        if entry_candle is None:
            continue

        entry_price = entry_candle.price_close
        eff_price = effective_price(entry_price, side)
        true_prob = min(eff_price + config.assumed_edge, 0.99)
        count = position_size(
            config.starting_balance,
            eff_price,
            true_prob,
            fraction=gc.sizing.kelly_fraction,
            max_position_pct=gc.sizing.max_position_pct,
            fees=fees,
            mode=gc.sizing.mode,
        )
        if count <= 0:
            continue

        orderbook = synthesize_orderbook(orig_m.ticker, entry_candle)
        order_side = OrderSide.NO if side == "no" else OrderSide.YES
        order_params = CreateOrderParams(
            ticker=orig_m.ticker,
            action=OrderAction.BUY,
            side=order_side,
            count=count,
            yes_price=entry_price if side != "no" else None,
            no_price=eff_price if side == "no" else None,
            post_only=False,
        )
        fill_result = simulate_fill(order_params, orderbook, fees=fees)

        if fill_result.total_filled <= 0:
            continue

        filled = fill_result.total_filled
        vwap = fill_result.total_notional / filled
        entry_time = datetime.fromtimestamp(entry_candle.end_period_ts, tz=UTC)
        settle_time = orig_m.close_time or entry_time

        pending.append(_PendingTrade(
            ticker=orig_m.ticker,
            title=orig_m.title,
            side=side,
            count=filled,
            vwap=vwap,
            fees=fill_result.total_fees,
            entry_time=entry_time,
            settle_time=settle_time,
            result=orig_m.result,
        ))

    # --- 5. Pass 2: process events chronologically ---
    events: list[tuple[str, datetime, _PendingTrade]] = []
    for trade in pending:
        events.append(("entry", trade.entry_time, trade))
        events.append(("settle", trade.settle_time, trade))
    # Sort by time. For events at the same timestamp, process entries
    # before settlements so a trade's own entry always precedes its
    # settlement. This means capital freed by settlements is available
    # starting the next timestamp, not the same one.
    events.sort(key=lambda e: (e[1], 0 if e[0] == "entry" else 1))

    traded_count = 0
    for event_type, timestamp, trade in events:
        if event_type == "entry":
            bought = ledger.buy(
                ticker=trade.ticker,
                title=trade.title,
                side=trade.side,
                count=trade.count,
                price=trade.vwap,
                fees=trade.fees,
                entry_time=trade.entry_time,
            )
            if bought:
                traded_count += 1
        elif event_type == "settle":
            if trade.ticker in ledger.positions:
                ledger.settle(
                    trade.ticker, trade.result,
                    settle_time=trade.settle_time,
                )
                ledger.snapshot(timestamp.isoformat())

    return BacktestResult(
        config=config,
        trades=ledger.trades,
        final_balance=ledger.balance,
        equity_curve=ledger.equity_curve,
        markets_scanned=len(settled),
        markets_passed_filter=len(passed),
        markets_scored=len(scored),
        markets_traded=traded_count,
    )
