"""Backtest engine — replays the gimme strategy on historical settled markets."""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from gimmes.config import GimmesConfig
from gimmes.kalshi.client import KalshiClient
from gimmes.kalshi.historical import Candle
from gimmes.kalshi.markets import list_all_markets
from gimmes.models.market import Market, MarketStatus, Orderbook, OrderbookLevel
from gimmes.risk.limits import (
    check_event_exposure,
    check_series_exposure,
    compute_exposure_for_group,
)
from gimmes.strategy.fees import DEFAULT_FEE_MULTIPLIERS, FeeMultipliers, fee_for_order
from gimmes.strategy.kelly import apply_base_rate_floor, position_size
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
    skipped_concentration: int = 0
    skipped_balance: int = 0
    truncated_chunks: list[str] = field(default_factory=list)


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
    event_ticker: str = ""
    series_ticker: str = ""


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
    event_ticker: str = ""
    series_ticker: str = ""


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
        event_ticker: str = "",
        series_ticker: str = "",
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
            event_ticker=event_ticker,
            series_ticker=series_ticker,
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
# Date chunking
# ---------------------------------------------------------------------------


def monthly_chunks(start: date, end: date) -> list[tuple[int, int]]:
    """Split a date range into per-month ``(min_ts, max_ts)`` pairs.

    Each chunk covers midnight UTC on the first day through 23:59:59 UTC
    on the last day of the month (or *end*, whichever comes first).
    """
    if start > end:
        return []
    chunks: list[tuple[int, int]] = []
    current = start
    while current <= end:
        last_day = calendar.monthrange(current.year, current.month)[1]
        chunk_end = min(date(current.year, current.month, last_day), end)
        min_ts = int(datetime(current.year, current.month, current.day,
                              tzinfo=UTC).timestamp())
        max_ts = int(datetime(chunk_end.year, chunk_end.month, chunk_end.day,
                              23, 59, 59, tzinfo=UTC).timestamp())
        chunks.append((min_ts, max_ts))
        # Advance to the 1st of the next month
        current = chunk_end + timedelta(days=1)
    return chunks


def weekly_chunks(min_ts: int, max_ts: int) -> list[tuple[int, int]]:
    """Split a timestamp range into ~7-day ``(min_ts, max_ts)`` pairs."""
    chunks: list[tuple[int, int]] = []
    current = min_ts
    week = 7 * 24 * 60 * 60  # 7 days in seconds
    while current <= max_ts:
        chunk_end = min(current + week - 1, max_ts)
        chunks.append((current, chunk_end))
        current = chunk_end + 1
    return chunks


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
    # Union all series across sides to ensure we fetch everything needed
    all_series: set[str] = set(gc.scanner.series or [])
    if gc.scanner.yes_series:
        all_series.update(gc.scanner.yes_series)
    if gc.scanner.no_series:
        all_series.update(gc.scanner.no_series)
    series_list = sorted(all_series)
    chunks = monthly_chunks(config.start_date, config.end_date)
    logger.info(
        "Fetching settled markets for %d series × %d monthly chunks via live API...",
        len(series_list), len(chunks),
    )
    max_pages = 200
    page_size = 200  # Kalshi default per-page limit
    pagination_cap = max_pages * page_size

    all_markets: list[Market] = []
    truncated_chunks: list[str] = []
    for series in series_list:
        for min_ts, max_ts in chunks:
            try:
                markets = await list_all_markets(
                    client, status="settled", series_ticker=series,
                    max_pages=max_pages,
                    min_close_ts=min_ts,
                    max_close_ts=max_ts,
                )
                if len(markets) >= pagination_cap:
                    # Monthly chunk too large — re-fetch in weekly slices
                    label = datetime.fromtimestamp(min_ts, tz=UTC).strftime("%Y-%m")
                    logger.info(
                        "Re-fetching %s %s in weekly chunks (monthly hit %d cap)",
                        series, label, len(markets),
                    )
                    markets = []
                    still_truncated = False
                    for wk_min, wk_max in weekly_chunks(min_ts, max_ts):
                        wk_markets = await list_all_markets(
                            client, status="settled", series_ticker=series,
                            max_pages=max_pages,
                            min_close_ts=wk_min,
                            max_close_ts=wk_max,
                        )
                        if len(wk_markets) >= pagination_cap:
                            still_truncated = True
                        markets.extend(wk_markets)
                    if still_truncated:
                        truncated_chunks.append(f"{series} ({label})")
                        logger.warning(
                            "Pagination limit hit for %s in %s"
                            " even with weekly chunks (%d markets)",
                            series, label, len(markets),
                        )
                all_markets.extend(markets)
            except Exception:
                label = datetime.fromtimestamp(min_ts, tz=UTC).strftime("%Y-%m")
                logger.warning(
                    "Failed to fetch series %s in %s", series, label,
                    exc_info=True,
                )

    # Deduplicate markets that may appear in adjacent chunks
    seen_tickers: set[str] = set()
    deduped: list[Market] = []
    for m in all_markets:
        if m.ticker not in seen_tickers:
            seen_tickers.add(m.ticker)
            deduped.append(m)
    all_markets = deduped

    # Safety-net date filter (server-side filtering handles most cases)
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

    # --- 2-4. Filter, Score, Identify trades — per side ---
    adapted = _adapt_for_filter(
        settled, max_days=gc.scanner.max_days_to_resolution,
    )
    original_by_ticker = {m.ticker: m for m in settled}
    pending: list[_PendingTrade] = []
    seen_tickers: set[str] = set()
    total_passed = 0
    total_scored = 0

    for scan_side in gc.sides_to_scan:
        side_cfg = gc.effective_config_for_side(scan_side)
        side_threshold = side_cfg.strategy.gimme_threshold
        side_series = side_cfg.scanner.series

        # --- 2. Filter (restrict to this side's series) ---
        # series_ticker may be empty on settled markets, so match
        # by ticker prefix (e.g., "KXCPI" matches "KXCPI-26MAR-T1.3")
        side_prefixes = tuple(s + "-" for s in side_series) if side_series else ()
        side_adapted = [
            m for m in adapted
            if not side_prefixes or m.ticker.startswith(side_prefixes)
        ]
        passed = filter_markets(side_adapted, side_cfg)
        total_passed += len(passed)

        # --- 3. Score ---
        scored: list[tuple[Market, Market, float]] = []
        for m in passed:
            s = quick_score(m, side_cfg)
            if s >= side_threshold:
                orig = original_by_ticker[m.ticker]
                scored.append((m, orig, s))
        total_scored += len(scored)

        scored.sort(
            key=lambda x: (
                x[1].close_time or datetime.min.replace(tzinfo=UTC)
            ),
        )

        # --- 4. Pass 1: identify qualifying trades ---
        for _, orig_m, _score in scored:
            if orig_m.ticker in seen_tickers:
                continue
            if orig_m.close_time is None:
                continue

            raw_price = (
                orig_m.midpoint
                if orig_m.midpoint > 0
                else orig_m.last_price
            )
            if raw_price <= 0:
                continue
            eff_price = effective_price(raw_price, scan_side)
            true_prob = min(eff_price + config.assumed_edge, 0.99)
            true_prob = apply_base_rate_floor(true_prob, orig_m.ticker)
            count = position_size(
                config.starting_balance,
                eff_price,
                true_prob,
                fraction=side_cfg.sizing.kelly_fraction,
                max_position_pct=side_cfg.sizing.max_position_pct,
                fees=fees,
                mode=side_cfg.sizing.mode,
            )
            if count <= 0:
                continue

            trade_fees = fee_for_order(
                count, eff_price, is_taker=False, fees=fees,
            )
            entry_time = orig_m.close_time - timedelta(days=1)

            seen_tickers.add(orig_m.ticker)
            pending.append(_PendingTrade(
                ticker=orig_m.ticker,
                title=orig_m.title,
                side=scan_side,
                count=count,
                vwap=eff_price,
                fees=trade_fees,
                entry_time=entry_time,
                settle_time=orig_m.close_time,
                result=orig_m.result,
                event_ticker=orig_m.event_ticker,
                series_ticker=orig_m.series_ticker,
            ))

    logger.info(
        "Filter: %d passed, %d scored above threshold",
        total_passed, total_scored,
    )

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
    skipped_concentration = 0
    skipped_balance = 0
    for event_type, timestamp, trade in events:
        if event_type == "entry":
            trade_dollars = trade.count * trade.vwap + trade.fees
            positions = list(ledger.positions.values())

            # Event concentration check
            skip = False
            if trade.event_ticker:
                evt_exp = compute_exposure_for_group(
                    positions, trade.event_ticker,
                )
                evt_chk = check_event_exposure(
                    evt_exp, trade_dollars,
                    config.starting_balance, gc,
                )
                if not evt_chk.passed:
                    skip = True

            # Series concentration check
            if not skip and trade.series_ticker:
                ser_exp = compute_exposure_for_group(
                    positions, trade.series_ticker,
                )
                ser_chk = check_series_exposure(
                    ser_exp, trade_dollars,
                    config.starting_balance, gc,
                )
                if not ser_chk.passed:
                    skip = True

            if skip:
                skipped_concentration += 1
                continue

            bought = ledger.buy(
                ticker=trade.ticker,
                title=trade.title,
                side=trade.side,
                count=trade.count,
                price=trade.vwap,
                fees=trade.fees,
                entry_time=trade.entry_time,
                event_ticker=trade.event_ticker,
                series_ticker=trade.series_ticker,
            )
            if bought:
                traded_count += 1
            else:
                skipped_balance += 1
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
        markets_passed_filter=total_passed,
        markets_scored=total_scored,
        markets_traded=traded_count,
        skipped_concentration=skipped_concentration,
        skipped_balance=skipped_balance,
        truncated_chunks=truncated_chunks,
    )
