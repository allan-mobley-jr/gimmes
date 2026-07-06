"""Market scanner — filters markets for gimme candidates."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from gimmes.config import GimmesConfig
from gimmes.models.market import Market, MarketStatus


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


def days_until(dt: datetime | None) -> float | None:
    """Calculate days from now until a datetime."""
    if dt is None:
        return None
    now = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - now
    return delta.total_seconds() / 86400


def filter_markets(
    markets: list[Market],
    config: GimmesConfig,
    *,
    exclude_tickers: set[str] | None = None,
) -> list[Market]:
    """Filter markets by gimme scanning criteria.

    Filters by:
    - Excluded tickers (e.g. open positions)
    - Price range (min_market_price to max_market_price)
    - Minimum volume / open interest
    - Market status (active only)
    - Time to resolution

    Category filtering is handled upstream by fetching markets per series.
    Returns filtered markets sorted by volume (descending).
    """
    sc = config.scanner
    st = config.strategy
    candidates: list[Market] = []

    for m in markets:
        # Skip tickers with open positions
        if exclude_tickers and m.ticker in exclude_tickers:
            continue

        # Must be active
        if m.status != MarketStatus.ACTIVE:
            continue

        # Price range check (from the configured side's perspective)
        raw_price = m.midpoint if m.midpoint > 0 else m.last_price
        price = effective_price(raw_price, st.side)
        if price < st.min_market_price or price > st.max_market_price:
            continue

        # Volume filter
        vol = m.volume_24h if m.volume_24h > 0 else m.volume
        if vol < sc.min_volume:
            continue

        # Open interest filter
        if m.open_interest < sc.min_open_interest:
            continue

        # Time to resolution — reject markets with no time info
        days = days_until(m.close_time)
        if days is None:
            days = days_until(m.expiration_time)
        if days is None:
            continue  # Perpetual or unknown — skip
        if days < sc.min_days_to_resolution:
            continue
        if days > sc.max_days_to_resolution:
            continue

        candidates.append(m)

    # Sort by volume descending
    candidates.sort(key=lambda m: m.volume_24h or m.volume, reverse=True)
    return candidates
