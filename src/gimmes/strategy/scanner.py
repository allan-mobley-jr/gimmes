"""Market scanner — filters markets for gimme candidates."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from gimmes.config import GimmesConfig
from gimmes.models.market import Market, MarketStatus
from gimmes.strategy.calendar import next_hour_top


def effective_price(yes_price: float, side: str) -> float:
    """Return the buy price for the given side.

    YES side: returns the YES price as-is.
    NO side: returns 1 - YES price (the NO price).
    """
    if side == "no":
        return round(1.0 - yes_price, 4)
    return yes_price


# Kalshi's cent tick. A side priced within one tick of a bound is
# untradeable in practice (at $0.00 an order isn't even placeable).
# Near-bound-but-placeable prices (eff $0.02-$0.05) are deliberately
# NOT clamped: the arithmetic stands, and execution is gated by the
# validator's price-at-bound rejection and the scan price band
# (#672 decision, deferred from #658). Historical degenerate candidate
# rows are not repaired — resurfacing tickers self-heal.
BOUND_TICK = 0.01


def price_at_bound(price: float) -> bool:
    """True when a price sits at or within one tick of $0.00 / $1.00."""
    return price <= BOUND_TICK or price >= 1.0 - BOUND_TICK


def tradeable_edge(prob: float, yes_price: float, side: str) -> float:
    """Edge on the side actually being bought, 0.0 at the price bounds.

    ``prob - effective_price`` degenerates when the tradeable side sits
    at or within one tick of a bound: at YES $1.00 the NO side costs
    $0.00 and the formula collapses to ``prob`` — a meaningless +88%
    "edge" on an unfillable order that inflates every aggregate edge
    statistic (#658). Bound-priced markets have no realizable edge.
    """
    eff = effective_price(yes_price, side)
    if price_at_bound(eff):
        logging.getLogger(__name__).debug(
            "edge clamped to 0: %s side priced at bound"
            " (yes %.2f -> eff %.2f) (#658)", side, yes_price, eff,
        )
        return 0.0
    return prob - eff


# Hourly ladders settle exactly at the top of the hour; the tolerance
# absorbs second-level stamping jitter in API close_times without ever
# admitting the following hour's ladder (anything < 1h cannot) (#736).
HOURLY_CLOSE_TOLERANCE = timedelta(seconds=60)


def days_until(
    dt: datetime | None, *, now: datetime | None = None,
) -> float | None:
    """Calculate days from *now* (default: wall clock) until a datetime."""
    if dt is None:
        return None
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)  # same coercion as dt below
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - now
    return delta.total_seconds() / 86400


def filter_markets(
    markets: list[Market],
    config: GimmesConfig,
    *,
    exclude_tickers: set[str] | None = None,
    now: datetime | None = None,
) -> list[Market]:
    """Filter markets by gimme scanning criteria.

    Filters by:
    - Excluded tickers (e.g. open positions)
    - Price range (min_market_price to max_market_price; hourly-series
      tickers use hourly_min/max_market_price instead)
    - Minimum volume / open interest
    - Market status (active only)
    - Time to resolution (hourly-series tickers must settle at the
      NEXT top of hour — close_time in (now, next_hour_top + 60s] —
      instead of the min-days floor, #736)

    *now* anchors every time comparison (tests inject it; live callers
    omit it for the wall clock). Naive values coerce to UTC.

    Category filtering is handled upstream by fetching markets per series.
    Returns filtered markets sorted by volume (descending).
    """
    sc = config.scanner
    st = config.strategy
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        # Coerce like days_until does for dt — a naive now would
        # otherwise be interpreted as SYSTEM-LOCAL time by
        # next_hour_top's astimezone(), silently skewing the bound
        now = now.replace(tzinfo=UTC)
    hourly_close_bound = next_hour_top(now) + HOURLY_CLOSE_TOLERANCE
    candidates: list[Market] = []

    for m in markets:
        # Skip tickers with open positions
        if exclude_tickers and m.ticker in exclude_tickers:
            continue

        # Must be active
        if m.status != MarketStatus.ACTIVE:
            continue

        hourly = config.is_hourly_ticker(m.ticker)

        # Price range check (from the configured side's perspective)
        raw_price = m.midpoint if m.midpoint > 0 else m.last_price
        price = effective_price(raw_price, st.side)
        min_price = st.hourly_min_market_price if hourly else st.min_market_price
        max_price = st.hourly_max_market_price if hourly else st.max_market_price
        if price < min_price or price > max_price:
            continue

        # Volume filter
        vol = m.volume_24h if m.volume_24h > 0 else m.volume
        if vol < sc.min_volume:
            continue

        # Open interest filter
        if m.open_interest < sc.min_open_interest:
            continue

        # Time to resolution — reject markets with no time info
        resolve_dt = (
            m.close_time if m.close_time is not None else m.expiration_time
        )
        if resolve_dt is None:
            continue  # Perpetual or unknown — skip
        if resolve_dt.tzinfo is None:
            resolve_dt = resolve_dt.replace(tzinfo=UTC)
        days = days_until(resolve_dt, now=now)
        # Hourly ladders settle at the NEXT top of hour (#736): the
        # #721 min-days bypass had no bound of its own, so far-hour
        # strikes (and stale past-close stragglers) leaked through.
        # (now, next_top + tolerance] rejects both; max_days below is
        # implied but kept as defense in depth.
        if hourly and not (now < resolve_dt <= hourly_close_bound):
            # Observability (#736 review): a mis-stamped ladder would
            # otherwise silently yield 0 candidates every hour
            logging.getLogger(__name__).debug(
                "hourly ticker %s rejected by close bound:"
                " close=%s not in (%s, %s]",
                m.ticker, resolve_dt, now, hourly_close_bound,
            )
            continue
        if not hourly and days < sc.min_days_to_resolution:
            continue
        if days > sc.max_days_to_resolution:
            continue

        candidates.append(m)

    # Sort by volume descending
    candidates.sort(key=lambda m: m.volume_24h or m.volume, reverse=True)
    return candidates
