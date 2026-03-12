"""Market scanner — filters markets for gimme candidates."""

from __future__ import annotations

from datetime import UTC, datetime

from gimmes.config import GimmesConfig
from gimmes.models.market import Market, MarketStatus


def days_until(dt: datetime | None) -> float | None:
    """Calculate days from now until a datetime."""
    if dt is None:
        return None
    now = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - now
    return delta.total_seconds() / 86400


def filter_markets(markets: list[Market], config: GimmesConfig) -> list[Market]:
    """Filter markets by gimme scanning criteria.

    Filters by:
    - Price range (min_market_price to max_market_price)
    - Minimum volume / open interest
    - Market status (open only)
    - Time to resolution
    - Category filter (if configured)

    Returns filtered markets sorted by volume (descending).
    """
    sc = config.scanner
    st = config.strategy
    candidates: list[Market] = []

    for m in markets:
        # Must be active/open
        if m.status not in (MarketStatus.ACTIVE, MarketStatus.OPEN):
            continue

        # Price range check (use midpoint or last_price)
        price = m.midpoint if m.midpoint > 0 else m.last_price
        if price < st.min_market_price or price > st.max_market_price:
            continue

        # Volume filter
        vol = m.volume_24h if m.volume_24h > 0 else m.volume
        if vol < sc.min_volume:
            continue

        # Open interest filter
        if m.open_interest < sc.min_open_interest:
            continue

        # Time to resolution
        days = days_until(m.close_time) or days_until(m.expiration_time)
        if days is not None:
            if days < sc.min_days_to_resolution:
                continue
            if days > sc.max_days_to_resolution:
                continue

        # Category filter (empty = all)
        if sc.categories and m.category:
            allowed = [c.lower() for c in sc.categories]
            if m.category.lower() not in allowed:
                continue

        candidates.append(m)

    # Sort by volume descending
    candidates.sort(key=lambda m: m.volume_24h or m.volume, reverse=True)
    return candidates
