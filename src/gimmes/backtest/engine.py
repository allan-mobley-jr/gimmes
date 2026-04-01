"""Backtest engine — replays the gimme strategy on historical settled markets."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from gimmes.config import GimmesConfig
from gimmes.kalshi.client import KalshiClient
from gimmes.kalshi.historical import Candle, get_candlesticks, list_all_historical_markets
from gimmes.models.market import Market, MarketStatus, Orderbook, OrderbookLevel
from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide
from gimmes.paper.fill_simulator import simulate_fill
from gimmes.strategy.fees import DEFAULT_FEE_MULTIPLIERS, FeeMultipliers
from gimmes.strategy.kelly import position_size
from gimmes.strategy.scanner import filter_markets
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


def _adapt_for_filter(markets: list[Market]) -> list[Market]:
    """Adapt historical markets so they pass through filter_markets().

    Historical markets have status=finalized and close_time in the past.
    We set status to ACTIVE and push close_time into the future so the
    time-to-resolution filter passes. The price/volume/OI filters remain.
    """
    future = datetime(2099, 1, 1, tzinfo=UTC)
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

    # --- 1. Fetch historical markets ---
    logger.info("Fetching historical markets...")
    all_markets = await list_all_historical_markets(client)

    # Keep only markets with a known result within the date range
    start_dt = datetime(
        config.start_date.year, config.start_date.month,
        config.start_date.day, tzinfo=UTC,
    )
    end_dt = datetime(
        config.end_date.year, config.end_date.month,
        config.end_date.day, 23, 59, 59, tzinfo=UTC,
    )

    def _in_range(m: Market) -> bool:
        if m.result not in ("yes", "no") or m.close_time is None:
            return False
        ct = m.close_time if m.close_time.tzinfo else m.close_time.replace(tzinfo=UTC)
        return start_dt <= ct <= end_dt

    settled = [m for m in all_markets if _in_range(m)]
    logger.info("Fetched %d markets, %d settled in date range", len(all_markets), len(settled))

    # --- 2. Filter ---
    adapted = _adapt_for_filter(settled)
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

    # --- 4. Iterate markets, fetch candles, simulate ---
    traded_count = 0
    for _adapted_m, orig_m, score in scored:
        # Fetch daily candlesticks
        close_ts = int(orig_m.close_time.timestamp()) if orig_m.close_time else 0
        if close_ts == 0:
            continue

        # Look back 90 days for entry opportunities
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

        # Pick entry candle
        entry_candle = pick_entry_candle(
            candles, gc.strategy.min_market_price, gc.strategy.max_market_price,
        )
        if entry_candle is None:
            continue

        entry_price = entry_candle.price_close

        # Size the position
        true_prob = min(entry_price + config.assumed_edge, 0.99)
        count = position_size(
            ledger.balance,
            entry_price,
            true_prob,
            fraction=gc.sizing.kelly_fraction,
            max_position_pct=gc.sizing.max_position_pct,
            fees=fees,
        )
        if count <= 0:
            continue

        # Synthesize orderbook and simulate fill
        orderbook = synthesize_orderbook(orig_m.ticker, entry_candle)
        order_params = CreateOrderParams(
            ticker=orig_m.ticker,
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=count,
            yes_price=entry_price,
            post_only=False,  # Taker for backtest (conservative fee assumption)
        )
        fill_result = simulate_fill(order_params, orderbook, fees=fees)

        if fill_result.total_filled <= 0:
            continue

        # Record the fill
        entry_time = datetime.fromtimestamp(entry_candle.end_period_ts, tz=UTC)
        bought = ledger.buy(
            ticker=orig_m.ticker,
            title=orig_m.title,
            side="yes",
            count=fill_result.total_filled,
            price=entry_price,
            fees=fill_result.total_fees,
            entry_time=entry_time,
        )
        if not bought:
            continue

        # Settle immediately (we know the outcome)
        settle_time = orig_m.close_time
        ledger.settle(orig_m.ticker, orig_m.result, settle_time=settle_time)
        ledger.snapshot(
            settle_time.isoformat() if settle_time else entry_time.isoformat(),
        )
        traded_count += 1

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
