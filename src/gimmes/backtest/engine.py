"""Backtest engine — replays the gimme strategy on historical settled markets."""

from __future__ import annotations

import calendar
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from gimmes.backtest.candle_cache import CandleCache
from gimmes.config import GimmesConfig
from gimmes.kalshi.client import KalshiClient
from gimmes.kalshi.historical import Candle, get_candlesticks
from gimmes.kalshi.markets import list_all_markets
from gimmes.models.market import Market, MarketStatus
from gimmes.risk.limits import (
    check_event_exposure,
    check_series_exposure,
    compute_exposure_for_group,
)
from gimmes.strategy.fees import (
    DEFAULT_FEE_MULTIPLIERS,
    FeeMultipliers,
    edge_after_fees,
    fee_for_order,
)
from gimmes.strategy.kelly import apply_base_rate_floor, position_size
from gimmes.strategy.scanner import effective_price, filter_markets
from gimmes.strategy.scorer import quick_score

ENTRY_OFFSET_DAYS = 1.0
# The 3-day lookback fits the API cap at EVERY allowed period: worst
# case is period 1 -> 3*1440 = 4320 (+1 inclusive endpoint) < 5000
# (cap pinned by test; live-probed 2026-07-13, hard 400 beyond it).
CANDLE_LOOKBACK_DAYS = 3
ALLOWED_CANDLE_PERIODS = (1, 60, 1440)
# Kalshi candlesticks: max 5000 periods per request — a hard 400
# ("max candlesticks: 5000"), NOT truncation. Live-probed 2026-07-13
# by bisection at periods 1/60/1440 (#716, settles the #714 TODO).
MAX_CANDLES_PER_REQUEST = 5000


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
    # #682: conservative fill model — entries cross the spread and pay
    # the ask with taker fees, instead of filling at the midpoint as a
    # maker. Selection (filter/score) AND the model belief (true_prob
    # = midpoint + assumed_edge) stay anchored to the midpoint — the
    # flag models fill COST only, so it can never pass MORE markets
    # through the prob gate or size them larger than maker mode
    # (review: deriving true_prob from the fill price loosened the
    # gate). Edge and Kelly then shrink naturally when paying the ask.
    taker_fill: bool = False
    # #714: post-entry TP/SL walk. None = hold to settlement (today's
    # behavior — and NO post-entry candle fetch at all). 0.0 is a
    # legal threshold: every gate on these must use `is not None`.
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    # #713: enter each market this many days before close.
    entry_offset_days: float = ENTRY_OFFSET_DAYS
    # #716: candle granularity in minutes (1, 60, or 1440) for entry
    # pricing and the TP/SL walk. Sub-day periods make sub-day entry
    # offsets meaningful (daily candles are midnight-US/Eastern
    # aligned, so a <24h market has no daily candle before close) and
    # compute volume_24h as a trailing-24h sum. Each period is a
    # fresh cache namespace.
    candle_period_minutes: int = 1440


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
    # #714: how the position ended. "settled" preserves the pre-#714
    # shape; early exits carry the fill and its timestamp, and keep
    # `result` = the market's EVENTUAL settlement so did-the-exit-help
    # analysis stays possible.
    exit_reason: str = "settled"
    exit_price: float | None = None
    exit_time: datetime | None = None


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
    skipped_no_candle: int = 0
    skipped_one_sided: int = 0
    fetch_failures: int = 0
    skipped_entry_gates: int = 0
    # #682: markets PRICED (not skipped) from a candle >1 day older
    # than entry — visibility before policy; and pass-1 drops where
    # kelly sizing yielded zero contracts.
    stale_candles: int = 0
    skipped_zero_sizing: int = 0
    truncated_chunks: list[str] = field(default_factory=list)
    # #714 exit-reason counters (0 unless a TP/SL walk ran), plus
    # walk-fetch visibility: a failed post-entry fetch silently holds
    # to settlement, so without this counter a systemic API failure
    # would degrade a TP/SL backtest into hold-to-settlement numbers
    # wearing an "Exits:" header (#655 doctrine).
    exited_take_profit: int = 0
    exited_stop_loss: int = 0
    walk_fetch_failures: int = 0
    # #716: the period the walk actually used — coarser than the
    # configured candle_period_minutes when the offset span would
    # exceed the API's 5000-period cap; None when no walk ran.
    walk_candle_period: int | None = None


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
    # #714: filled by the walk pass when a TP/SL trigger is found.
    # walk_fetch_failed marks a trade whose post-entry candles could
    # not be fetched — counted at Pass-2 entry ACCEPTANCE, because a
    # failed walk only matters for positions that actually opened.
    exit_reason: str | None = None
    exit_price: float = 0.0
    exit_time: datetime | None = None
    walk_fetch_failed: bool = False


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

    def close(
        self,
        ticker: str,
        *,
        result: str,
        price: float,
        exit_fees: float,
        exit_time: datetime | None,
        reason: str,
        settle_time: datetime | None = None,
    ) -> BacktestTrade | None:
        """Close an open position at ``price`` (#714 early exit).

        Unlike ``settle``, this is a live SELL at a market price, so
        the exit leg pays its own order fee. ``result`` is the
        market's EVENTUAL settlement outcome, recorded so exited
        trades stay comparable with settled ones. The position is
        popped, which makes the trade's later settle event a natural
        no-op via the caller's positions guard.

        Note the maker-mode hybrid: exits always PRICE at the touch
        (a spread-crossing fill — deliberate conservatism), while the
        fee multiplier follows the run's fill model; only taker mode
        is fee-consistent with its price. Documented, not accidental.
        """
        pos = self.positions.pop(ticker, None)
        if pos is None:
            return None

        payout = pos.count * price - exit_fees
        pnl = payout - pos.cost_basis
        self.balance += payout

        trade = BacktestTrade(
            ticker=ticker,
            title=pos.title,
            side=pos.side,
            count=pos.count,
            entry_price=pos.entry_price,
            cost_basis=pos.cost_basis,
            # entry + exit legs — cost_basis embeds only the entry
            # fee; pnl = payout − cost_basis stays self-consistent
            # because the exit fee is already netted out of payout.
            fees=pos.fees + exit_fees,
            result=result,
            payout=payout,
            pnl=pnl,
            entry_time=pos.entry_time,
            # The time the market WOULD have settled — kept alongside
            # exit_time so exit-vs-hold analysis has both moments.
            settle_time=settle_time,
            exit_reason=reason,
            exit_price=price,
            exit_time=exit_time,
        )
        self.trades.append(trade)
        return trade

    def snapshot(self, timestamp: str) -> None:
        """Record an equity snapshot (balance only — all positions settle)."""
        self.equity_curve.append((timestamp, self.balance))


# ---------------------------------------------------------------------------
# Entry candle selection
# ---------------------------------------------------------------------------


def candle_midpoint(candle: Candle) -> float:
    """Bid/ask midpoint of a candle — the backtest analog of
    Market.midpoint. Returns 0.0 unless both quote sides are positive
    (a one-sided book has no priceable midpoint; the candle's
    trade-price OHLC can be stale on thin days, so it is deliberately
    NOT used as a fallback) (#655)."""
    if candle.yes_bid_close > 0 and candle.yes_ask_close > 0:
        return (candle.yes_bid_close + candle.yes_ask_close) / 2
    return 0.0


def _walk_exit(
    candles: list[Candle],
    *,
    side: str,
    count: int,
    entry_eff: float,
    cost_basis: float,
    entry_ts: int,
    settle_ts: int,
    tp_pct: float | None,
    sl_pct: float | None,
) -> tuple[str, float, int] | None:
    """Walk post-entry daily candles for the first TP/SL trigger (#714).

    Mirrors the LIVE definitions exactly: stop-loss fires when the
    unrealized loss reaches ``sl_pct`` of the fee-inclusive cost basis
    (reporting/formatter._stop_gate_pct); take-profit fires when the
    unrealized gain reaches ``tp_pct`` of the fee-FREE maximum profit
    ``count * (1 − side-effective entry)`` (monitor.md's worked
    example). The trigger mark is the side-effective bid/ask midpoint
    — what the live monitor sees — while the returned exit price is
    the conservative tradeable-side close (YES exits at the bid, NO
    at 1 − ask): the trigger looks at the mid, the fill crosses to
    the touch.

    Look-ahead discipline: candles ending at or before ``entry_ts``
    priced the entry; candles ending at or after ``settle_ts`` encode
    the settlement outcome — both are excluded, so the first evaluable
    candle is the day after entry. Quiet/one-sided candles (zero
    midpoint or a close at/over 1.0 — live data zero-defaults omitted
    quote groups) are SKIPPED, not treated as crashes: a zero-default
    close would otherwise fabricate a stop-loss on every quiet day.
    The price of that skip is that a GENUINE one-sided crash (bid
    collapses while the ask stands) is also skipped — the walk is
    optimistic there, unlike the live monitor, whose Market.midpoint
    falls back to last_price and can still mark down.
    Stop-loss is checked before take-profit — when both trigger on
    one candle (degenerate thresholds), the pessimistic read wins.

    Returns (reason, exit_price, exit_ts) or None to hold.
    """
    max_profit = count * (1.0 - entry_eff)
    for candle in candles:
        if candle.end_period_ts <= entry_ts:
            continue
        if candle.end_period_ts >= settle_ts:
            break
        mid = candle_midpoint(candle)
        if (
            mid <= 0
            or candle.yes_bid_close >= 1.0
            or candle.yes_ask_close >= 1.0
        ):
            continue
        mark = effective_price(mid, side)
        unrealized = count * mark - cost_basis
        # Conservative tradeable-side close: YES exits at the bid,
        # NO at 1 − ask (see docstring).
        exit_price = (
            candle.yes_bid_close if side == "yes"
            else 1.0 - candle.yes_ask_close
        )
        if sl_pct is not None and -unrealized >= sl_pct * cost_basis:
            return ("stop_loss", exit_price, candle.end_period_ts)
        if (
            tp_pct is not None
            and max_profit > 0
            and unrealized >= tp_pct * max_profit
        ):
            return ("take_profit", exit_price, candle.end_period_ts)
    return None


def entry_candle_at(candles: list[Candle], entry_ts: int) -> Candle | None:
    """Last candle ending at or before entry_ts — a fixed-offset rule,
    NOT a search for an in-range day. Its predecessor pick_entry_candle
    scanned history for the last in-range close, which conditions the
    entry day on knowing the price path — a look-ahead of its own
    (#655). Candles are sorted ascending by end_period_ts.
    """
    chosen: Candle | None = None
    for candle in candles:
        if candle.end_period_ts <= entry_ts:
            chosen = candle
        else:
            break
    return chosen


async def _fetch_entry_candle(
    client: KalshiClient,
    ticker: str,
    entry_ts: int,
    cache: dict[str, list[Candle]],
    failed: set[str],
    disk: CandleCache | None = None,
    period: int = 1440,
) -> Candle | None:
    """The ticker's entry-day candle, or None when no candle ends at
    or before entry_ts. The fetch window ends AT entry_ts, so future
    data structurally cannot leak into the engine (#655). Fetch
    failures degrade to None (debug-logged); classification of the
    unusable cases (no history vs one-sided quote) happens at the
    call site so the funnel can count them separately (#666).

    ``disk`` (#696): a CandleCache consulted between the in-memory
    miss and the API call, written through on SUCCESS only — a
    failure never reaches ``put``, structurally (it lives in the
    except branch), so the #655 fetch_failures visibility is intact
    on warm reruns. get_candlesticks raises on envelope/shape
    anomalies (#704), so a renamed field lands in ``failed`` too —
    counted, never cached.
    """
    if ticker not in cache:
        window = {
            "start_ts": entry_ts - CANDLE_LOOKBACK_DAYS * 86400,
            "end_ts": entry_ts,
            "period_interval": period,
        }
        cached = None
        if disk is not None:
            cached = await disk.get(ticker, **window)
        if cached is not None:
            cache[ticker] = cached
        else:
            try:
                candles = await get_candlesticks(
                    client, ticker, **window,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "candle fetch failed for %s: %s", ticker, exc,
                )
                cache[ticker] = []
                # A FAILED fetch is not empty history — the caller
                # keeps systemic API failures (the #655 endpoint 404
                # signature) out of the data-sparsity counters (#666).
                failed.add(ticker)
            else:
                cache[ticker] = candles
                if disk is not None:
                    await disk.put(ticker, candles=candles, **window)
    return entry_candle_at(cache[ticker], entry_ts)


def _trailing_24h_volume(
    candles: list[Candle], entry_ts: int,
) -> int:
    """Sum of per-period candle volumes in the trailing 24 hours
    ``(entry_ts - 86400, entry_ts]`` (#716). Candle volume is
    strictly per-period (live-verified: minute candles sum to the
    market total), and quiet periods are simply omitted from the
    series — omitted means zero volume, so the sum over present
    candles is exact."""
    return sum(
        c.volume for c in candles
        if entry_ts - 86_400 < c.end_period_ts <= entry_ts
    )


def _entry_day_view(
    market: Market, candle: Candle, close_time: datetime,
    volume_24h: int | None = None,
) -> Market:
    """A Market as it looked AT THE ENTRY TIMESTAMP, built from the
    entry candle (daily at period 1440, hourly/minute at sub-day
    periods, #716) — the selection lens the scanner/scorer see
    (#666).

    Field mapping (all price/liquidity data comes from the candle so
    filter_markets and quick_score run unchanged with no settlement
    leak):
    - yes_bid/yes_ask from the candle closes — Market.midpoint and
      Market.spread then reproduce candle_midpoint and the
      candle-derived spread with no new arithmetic.
    - last_price is 0.0: the midpoint must never fall back to stale
      trade prices (the same rationale candle_midpoint uses, #655).
    - volume AND volume_24h: at period 1440 both come from the
      candle's per-period volume — that IS the day's 24h volume. At
      sub-day periods the caller passes ``volume_24h`` = the
      trailing-24h SUM of per-period volumes, and both fields carry
      it (#716); the single per-period candle volume would understate
      24h volume up to 1440x and silently bias the scanner's
      min_volume gate and quick_score's volume tiers. Both fields are
      set to the same value so the scanner/scorer's
      `volume_24h or volume` branches read it either way. The 1440
      path deliberately keeps the SELECTED candle's volume (not the
      trailing sum): a stale entry candle >1 day old would sum to 0
      and silently drop markets the default path prices today.
    - open_interest at candle close.
    - status ACTIVE + close_time at now + the configured entry offset
      (``entry_offset_days``, default ENTRY_OFFSET_DAYS): the
      honest days-to-resolution at the backtest's fixed-offset entry
      is exactly the configured offset by construction (the +60s
      margin makes the min_days == entry_offset_days boundary
      deterministic).
    """
    vol = candle.volume if volume_24h is None else volume_24h
    return market.model_copy(update={
        "status": MarketStatus.ACTIVE,
        "yes_bid": candle.yes_bid_close,
        "yes_ask": candle.yes_ask_close,
        "last_price": 0.0,
        "volume": vol,
        "volume_24h": vol,
        "open_interest": candle.open_interest,
        "close_time": close_time,
        "expiration_time": close_time,
    })


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
    candle_cache: CandleCache | None = None,
) -> BacktestResult:
    """Run a backtest over historical settled markets.

    Algorithm (#666: selection happens through the entry-day lens):
    1. Fetch all settled historical markets
    2. Fetch the ENTRY-DAY candle for every settled market and build
       entry-day views (settlement data never reaches selection)
    3. Filter by scanner criteria and score with quick_score on the
       entry-day views, gate on gimme_threshold
    4. For each qualifying market, gate true-prob/edge and size at the
       entry-day price; settlement supplies only the payout
    5. Return aggregated results
    """
    gc = config.gimmes_config
    if not (
        math.isfinite(config.entry_offset_days)
        and config.entry_offset_days > 0
    ):
        # Fail fast — BEFORE the minutes-long market fetch pass — with
        # a clear message for programmatic callers: NaN/inf would
        # otherwise surface as an obscure timedelta conversion error
        # deep in the entry pass (Copilot, #713; placement
        # review-found).
        raise ValueError(
            f"entry_offset_days must be a positive finite number,"
            f" got {config.entry_offset_days!r}",
        )
    if config.candle_period_minutes not in ALLOWED_CANDLE_PERIODS:
        # Same fail-fast doctrine (#713): a bad period would surface
        # as a per-market API 400 deep in the entry pass.
        raise ValueError(
            f"candle_period_minutes must be one of"
            f" {ALLOWED_CANDLE_PERIODS}, got"
            f" {config.candle_period_minutes!r}",
        )
    # Union all series across sides — the fetch watchlist, also scoping
    # the hourly guard below
    all_series: set[str] = (
        set(gc.scanner.series)
        | set(gc.scanner.yes_series)
        | set(gc.scanner.no_series or [])
    )
    _hourly_overlap = set(gc.scanner.hourly_series) & all_series
    if _hourly_overlap:
        # Fail fast (#713 doctrine): entry-day views carry a SYNTHETIC
        # now-anchored close_time (now + entry_offset_days, see
        # _entry_day_view), so the hourly next-top-of-hour close bound
        # (#736) would reject every view — nondeterministically, by
        # the wall-clock minute the run starts. Hourly backtests need
        # a per-entry clock model; until then, fail loudly instead of
        # silently returning zero trades. Scoped to the OVERLAP with
        # the fetched series (review-found): hourly_series armed for
        # the LIVE loop but absent from the backtest watchlists cannot
        # affect any fetched view and must not block backtests.
        raise ValueError(
            f"hourly series {sorted(_hourly_overlap)!r} cannot be"
            " backtested through this engine: entry-day views use"
            " synthetic now-anchored close times that the hourly"
            " next-top-of-hour bound (#736) rejects. Remove the series"
            " from the backtest watchlist (scanner.series/yes_series/"
            "no_series) — do NOT clear scanner.hourly_series, which"
            " arms the live loop (hourly ladders are explored with"
            " min_days overrides instead, as the pre-#722 KXBTCD"
            " backtests did)",
        )
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

    # --- 2. Entry-day candle pass (#666) ---
    # Selection must see the market as it looked ON ENTRY DAY: the
    # settled snapshot's price/volume/OI/spread encode settlement-time
    # information (in-band-at-entry markets that drifted out by
    # settlement were invisible; end-of-life liquidity gated
    # optimistically). One candle fetch per settled market, behind the
    # client's token bucket (~3 min for a full multi-series run).
    original_by_ticker = {m.ticker: m for m in settled}
    memory_cache: dict[str, list[Candle]] = {}
    entry_candles: dict[str, Candle] = {}
    entry_times: dict[str, datetime] = {}
    skipped_no_candle = 0
    stale_candles = 0
    skipped_one_sided = 0
    fetch_failures = 0
    failed_fetches: set[str] = set()
    # Snapshot so the summary below logs THIS run's hits — a sweep
    # driver sharing one CandleCache across run_backtest calls would
    # otherwise see cumulative hits and negative "API fetches".
    disk_hits_before = candle_cache.hits if candle_cache is not None else 0
    if (
        gc.scanner.min_days_to_resolution > config.entry_offset_days
        or gc.scanner.max_days_to_resolution <= config.entry_offset_days
    ):
        logger.warning(
            "min_days_to_resolution (%.1f) / max_days_to_resolution"
            " (%.1f) exclude the backtest's %g-day entry offset"
            " (--entry-offset) — the days filter will select NOTHING"
            " (#666)",
            gc.scanner.min_days_to_resolution,
            gc.scanner.max_days_to_resolution, config.entry_offset_days,
        )
    if config.entry_offset_days < 1 and config.candle_period_minutes == 1440:
        # #716: sub-day offsets are only meaningful with sub-day
        # candles — daily candles are midnight-US/Eastern aligned, so
        # a <24h market has no daily candle before close.
        logger.warning(
            "--entry-offset %g is sub-day, but candles are DAILY"
            " (period 1440, midnight-US/Eastern boundaries): entries"
            " still price from the last candle ending at or before"
            " the offset, and markets living under a day mostly have"
            " none (skipped_no_candle) — pass --candle-period 60 (or"
            " 1) for sub-day granularity (#716)",
            config.entry_offset_days,
        )
        if (
            config.take_profit_pct is not None
            or config.stop_loss_pct is not None
        ):
            logger.warning(
                "TP/SL with a sub-day entry offset at DAILY"
                " granularity: the walk sees only daily candles"
                " strictly between entry and settlement — under a"
                " %g-day offset that is almost always ZERO candles,"
                " so exits silently degrade to hold-to-settlement;"
                " pass --candle-period 60 (or 1) (#714/#716)",
                config.entry_offset_days,
            )
    logger.info(
        "entry-candle pass: %d markets to price — one candle fetch"
        " each on a cold cache (#713)", len(settled),
    )
    for i, m in enumerate(settled):
        if m.close_time is None:
            continue
        if i and i % 250 == 0:
            logger.info(
                "entry candles: %d/%d processed", i, len(settled),
            )
        close_time = (
            m.close_time if m.close_time.tzinfo
            else m.close_time.replace(tzinfo=UTC)
        )
        entry_time = close_time - timedelta(days=config.entry_offset_days)
        entry_ts = int(entry_time.timestamp())
        candle = await _fetch_entry_candle(
            client, m.ticker, entry_ts, memory_cache,
            failed=failed_fetches, disk=candle_cache,
            period=config.candle_period_minutes,
        )
        if candle is None and m.ticker in failed_fetches:
            # Distinguish a FAILED fetch from genuinely-empty history:
            # a systemic failure (the #655 endpoint 404 produced
            # exactly this signature) must not masquerade as data
            # sparsity in the funnel.
            fetch_failures += 1
            if fetch_failures == 1:
                logger.warning(
                    "entry-candle fetch FAILED for %s — if this"
                    " repeats, the skip counts reflect an API"
                    " problem, not data sparsity (#666)", m.ticker,
                )
            continue
        if candle is None:
            skipped_no_candle += 1
            logger.debug(
                "no entry candle for %s: no candles <= entry_ts",
                m.ticker,
            )
            continue
        if (
            candle_midpoint(candle) <= 0
            or candle.yes_bid_close >= 1.0
            or candle.yes_ask_close >= 1.0
        ):
            # One-sided OR empty quote (either/both closes zero), or
            # an at/over-bound close (#682: as unpriceable as an
            # empty one — without the upper bound a 1.0 close passes
            # and pollutes the sizing/gate counters; taker mode reads
            # the raw close directly): no
            # priceable midpoint; trade-price OHLC stays deliberately
            # rejected as a fallback (stale on thin days, #655).
            # Counted separately because these skips can bias the
            # sample away from near-certain late-life contracts —
            # exactly the gimme population (#666).
            skipped_one_sided += 1
            logger.debug(
                "unusable entry quote for %s (bid=%s ask=%s)",
                m.ticker, candle.yes_bid_close, candle.yes_ask_close,
            )
            continue
        if entry_ts - candle.end_period_ts > 86400:
            stale_candles += 1
            logger.debug(
                "entry candle for %s is %.1f days older than"
                " entry_ts — stale-quote pricing (#666 residual)",
                m.ticker, (entry_ts - candle.end_period_ts) / 86400,
            )
        entry_candles[m.ticker] = candle
        entry_times[m.ticker] = entry_time

    # Build the views AFTER the fetch pass: the synthetic close must
    # be honest RELATIVE TO FILTER TIME — computing it before a
    # multi-minute fetch pass would leave days-to-resolution just
    # under the configured entry offset and silently zero out configs
    # with min_days_to_resolution == entry_offset_days (review-found).
    if candle_cache is not None:
        disk_hits = candle_cache.hits - disk_hits_before
        logger.info(
            "candle pass: %d unique tickers — %d disk-cache hits,"
            " %d API fetch attempts (#696)",
            len(memory_cache), disk_hits,
            len(memory_cache) - disk_hits,
        )
    synthetic_close = datetime.now(UTC) + timedelta(
        days=config.entry_offset_days, seconds=60,
    )
    entry_views: dict[str, Market] = {
        ticker: _entry_day_view(
            original_by_ticker[ticker], candle, synthetic_close,
            volume_24h=(
                _trailing_24h_volume(
                    memory_cache[ticker],
                    int(entry_times[ticker].timestamp()),
                )
                if config.candle_period_minutes < 1440 else None
            ),
        )
        for ticker, candle in entry_candles.items()
    }

    # --- 3-4. Filter, Score, Identify trades — per side ---
    pending: list[_PendingTrade] = []
    skipped_entry_gates = 0
    zero_sized_tickers: set[str] = set()
    seen_tickers: set[str] = set()
    total_passed = 0
    total_scored = 0

    for scan_side in gc.sides_to_scan:
        side_cfg = gc.effective_config_for_side(scan_side)
        side_threshold = side_cfg.strategy.gimme_threshold
        side_series = side_cfg.scanner.series

        # --- 3. Filter + Score on the entry-day views (#666) ---
        # series_ticker may be empty on settled markets, so match
        # by ticker prefix (e.g., "KXCPI" matches "KXCPI-26MAR-T1.3")
        side_prefixes = tuple(s + "-" for s in side_series) if side_series else ()
        side_views = [
            v for v in entry_views.values()
            if not side_prefixes or v.ticker.startswith(side_prefixes)
        ]
        passed = filter_markets(side_views, side_cfg)
        total_passed += len(passed)

        # Mirror live gating (validator.py:152-175): gimme_threshold +
        # min_true_probability + min_edge_after_fees (#592). The
        # price band already ran inside filter_markets on the SAME
        # entry-day midpoint, so pass 1 keeps only the gates the
        # filter doesn't apply: true-prob and edge-after-fees.
        min_true_prob = side_cfg.strategy.min_true_probability
        min_edge = side_cfg.strategy.min_edge_after_fees
        scored: list[tuple[Market, Market, float]] = []
        for m in passed:
            s = quick_score(m, side_cfg)
            if s < side_threshold:
                continue
            scored.append((m, original_by_ticker[m.ticker], s))
        total_scored += len(scored)

        scored.sort(
            key=lambda x: (
                x[1].close_time or datetime.min.replace(tzinfo=UTC)
            ),
        )

        # --- 4. Pass 1: identify qualifying trades ---
        # #655/#666: everything the engine decides is decided at the
        # entry-day price — settlement supplies only the payout.
        for view, orig_m, _score in scored:
            if orig_m.ticker in seen_tickers:
                continue
            if orig_m.close_time is None:
                continue

            ticker = orig_m.ticker
            entry_time = entry_times[ticker]
            mid = view.midpoint
            mid_eff = effective_price(mid, scan_side)
            is_taker = config.taker_fill
            if is_taker:
                # Conservative fill (#682): entry crosses the spread
                # and pays the ask. NO ask = 1 - YES bid.
                entry_eff = (
                    view.yes_ask if scan_side == "yes"
                    else effective_price(view.yes_bid, "no")
                )
            else:
                entry_eff = mid_eff
            # The model belief is anchored to the MIDPOINT, not the
            # fill price — otherwise taker mode would float true_prob
            # up with the ask and pass markets maker mode rejects.
            true_prob = min(mid_eff + config.assumed_edge, 0.99)
            true_prob = apply_base_rate_floor(
                true_prob, ticker, side=scan_side,
            )
            if true_prob < min_true_prob:
                skipped_entry_gates += 1
                continue
            if edge_after_fees(
                entry_eff, true_prob, is_taker=is_taker, fees=fees,
            ) < min_edge:
                skipped_entry_gates += 1
                continue

            count = position_size(
                config.starting_balance,
                entry_eff,
                true_prob,
                fraction=side_cfg.sizing.kelly_fraction,
                max_position_pct=side_cfg.sizing.max_position_pct,
                fees=fees,
                mode=side_cfg.sizing.mode,
                is_taker=is_taker,
            )
            if count <= 0:
                # Set, not counter (#682 review): under side='both' a
                # ticker can zero-size on both sides (one market, not
                # two), or zero-size on one and trade the other.
                zero_sized_tickers.add(ticker)
                continue

            trade_fees = fee_for_order(
                count, entry_eff, is_taker=is_taker, fees=fees,
            )

            seen_tickers.add(ticker)
            pending.append(_PendingTrade(
                ticker=ticker,
                title=orig_m.title,
                side=scan_side,
                count=count,
                vwap=entry_eff,
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

    # --- 4b. Post-entry TP/SL walk (#714) ---
    # Only when a threshold is configured (`is not None` — 0.0 is a
    # legal value): fetch each pending trade's post-entry daily
    # candles and find the first trigger. One fetch per pending trade
    # (tens; trades later skipped in Pass 2 by the balance or
    # concentration gates over-fetch slightly — accepted for
    # simplicity), through its own window-keyed path: the entry
    # pass's memory_cache is keyed by ticker for a DIFFERENT window
    # and must not be reused. Walk fetch failures hold to settlement
    # (debug-logged) and deliberately do NOT count in fetch_failures —
    # that counter's funnel arithmetic is entry-pass semantics.
    # Cap fact (settles the #714 TODO): Kalshi hard-400s beyond 5000
    # periods per request (live-probed 2026-07-13) — see
    # MAX_CANDLES_PER_REQUEST. The walk span is exactly the entry
    # offset (entry -> settle), so the cap condition is config-derived
    # and computed ONCE: fall back to the next coarser period until
    # the span fits (#716). Coarser exit resolution beats a hard 400,
    # and 1440 always fits (a 5000-day offset would be absurd).
    walk_fetch_failures = 0
    walk_candle_period: int | None = None
    if (
        config.take_profit_pct is not None
        or config.stop_loss_pct is not None
    ):
        walk_candle_period = config.candle_period_minutes
        while (
            walk_candle_period != 1440
            and config.entry_offset_days * 1440 / walk_candle_period
            >= MAX_CANDLES_PER_REQUEST
        ):
            coarser = 60 if walk_candle_period == 1 else 1440
            logger.warning(
                "--entry-offset %g spans %d periods at %d-min"
                " granularity — over the API's hard cap of %d periods"
                " per request; walking exits at period %d instead"
                " (coarser exit resolution) (#716)",
                config.entry_offset_days,
                int(config.entry_offset_days * 1440 / walk_candle_period),
                walk_candle_period, MAX_CANDLES_PER_REQUEST, coarser,
            )
            walk_candle_period = coarser
        for trade in pending:
            entry_ts = int(trade.entry_time.timestamp())
            settle_time = (
                trade.settle_time if trade.settle_time.tzinfo
                else trade.settle_time.replace(tzinfo=UTC)
            )
            settle_ts = int(settle_time.timestamp())
            walk_window = {
                "start_ts": entry_ts,
                "end_ts": settle_ts,
                "period_interval": walk_candle_period,
            }
            walk_candles = None
            if candle_cache is not None:
                walk_candles = await candle_cache.get(
                    trade.ticker, **walk_window,
                )
            if walk_candles is None:
                try:
                    walk_candles = await get_candlesticks(
                        client, trade.ticker, **walk_window,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Flag now, COUNT at Pass-2 entry acceptance — a
                    # failed walk only misleads for positions that
                    # actually open (Copilot: pre-admission counting
                    # overattributed silent holds).
                    trade.walk_fetch_failed = True
                    logger.debug(
                        "walk candle fetch failed for %s: %s —"
                        " holding to settlement", trade.ticker, exc,
                    )
                    continue
                else:
                    if candle_cache is not None:
                        await candle_cache.put(
                            trade.ticker, candles=walk_candles,
                            **walk_window,
                        )
            exit_hit = _walk_exit(
                walk_candles,
                side=trade.side,
                count=trade.count,
                entry_eff=trade.vwap,
                cost_basis=trade.count * trade.vwap + trade.fees,
                entry_ts=entry_ts,
                settle_ts=settle_ts,
                tp_pct=config.take_profit_pct,
                sl_pct=config.stop_loss_pct,
            )
            if exit_hit is not None:
                reason, exit_price, exit_ts = exit_hit
                trade.exit_reason = reason
                trade.exit_price = exit_price
                trade.exit_time = datetime.fromtimestamp(exit_ts, tz=UTC)

    # --- 5. Pass 2: process events chronologically ---
    events: list[tuple[str, datetime, _PendingTrade]] = []
    for trade in pending:
        events.append(("entry", trade.entry_time, trade))
        events.append(("settle", trade.settle_time, trade))
        if trade.exit_reason is not None and trade.exit_time is not None:
            events.append(("exit", trade.exit_time, trade))
    # Sort by time. For events at the same timestamp, process entries
    # before settlements so a trade's own entry always precedes its
    # settlement. This means capital freed by settlements is available
    # starting the next timestamp, not the same one.
    events.sort(key=lambda e: (e[1], 0 if e[0] == "entry" else 1))

    traded_count = 0
    skipped_concentration = 0
    skipped_balance = 0
    exited_take_profit = 0
    exited_stop_loss = 0
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
                if trade.walk_fetch_failed:
                    walk_fetch_failures += 1
                    if walk_fetch_failures == 1:
                        logger.warning(
                            "post-entry walk fetch FAILED for %s — if"
                            " this repeats, TP/SL exits are silently"
                            " degrading to hold-to-settlement (#714)",
                            trade.ticker,
                        )
            else:
                skipped_balance += 1
        elif event_type == "exit":
            # #714: TP/SL exit found by the walk. Entries skipped at
            # concentration/balance never opened a position, so the
            # guard makes their exit a no-op — and once closed, the
            # trade's later settle event no-ops the same way.
            if trade.ticker in ledger.positions:
                exit_fees = fee_for_order(
                    trade.count, trade.exit_price,
                    is_taker=config.taker_fill, fees=fees,
                )
                # Guarded at event construction: exit events are only
                # appended with a non-None reason — assert keeps a
                # future violation loud instead of mislabeled.
                assert trade.exit_reason is not None
                ledger.close(
                    trade.ticker,
                    result=trade.result,
                    price=trade.exit_price,
                    exit_fees=exit_fees,
                    exit_time=trade.exit_time,
                    reason=trade.exit_reason,
                    settle_time=trade.settle_time,
                )
                if trade.exit_reason == "take_profit":
                    exited_take_profit += 1
                elif trade.exit_reason == "stop_loss":
                    exited_stop_loss += 1
                else:  # pragma: no cover - _walk_exit emits only these
                    raise AssertionError(
                        f"unknown exit reason {trade.exit_reason!r}",
                    )
                ledger.snapshot(timestamp.isoformat())
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
        skipped_no_candle=skipped_no_candle,
        skipped_one_sided=skipped_one_sided,
        fetch_failures=fetch_failures,
        skipped_entry_gates=skipped_entry_gates,
        stale_candles=stale_candles,
        skipped_zero_sizing=len(zero_sized_tickers - seen_tickers),
        skipped_balance=skipped_balance,
        truncated_chunks=truncated_chunks,
        exited_take_profit=exited_take_profit,
        exited_stop_loss=exited_stop_loss,
        walk_fetch_failures=walk_fetch_failures,
        walk_candle_period=walk_candle_period,
    )
