"""Fee multiplier cache -- fetches per-series multipliers from Kalshi API."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from gimmes.strategy.fees import DEFAULT_FEE_MULTIPLIERS, FeeMultipliers

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 3600.0  # 1 hour


@dataclass
class _CacheEntry:
    multipliers: FeeMultipliers
    fetched_at: float  # time.monotonic()


# Module-level cache: series_ticker -> entry
_cache: dict[str, _CacheEntry] = {}


def get_multipliers(series_ticker: str) -> FeeMultipliers:
    """Return fee multipliers from cache, or defaults if missing/expired."""
    entry = _cache.get(series_ticker)
    if entry and (time.monotonic() - entry.fetched_at) < _DEFAULT_TTL:
        return entry.multipliers
    return DEFAULT_FEE_MULTIPLIERS


async def refresh_fee_cache(client: object) -> None:
    """Fetch fee changes from Kalshi and populate cache.

    Args:
        client: A KalshiClient instance.
    """
    from gimmes.kalshi.markets import get_series_fee_changes

    try:
        records = await get_series_fee_changes(client)  # type: ignore[arg-type]
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("Failed to fetch fee multipliers: %s; using defaults", exc)
        return

    now_utc = datetime.now(UTC)
    now = time.monotonic()
    for item in records:
        series = item.get("series_ticker", "")
        if not series:
            continue

        # Skip future-scheduled changes that haven't taken effect yet
        scheduled = item.get("scheduled_ts")
        if scheduled:
            try:
                ts = datetime.fromisoformat(scheduled)
                if ts > now_utc:
                    continue
            except (ValueError, TypeError):
                pass  # If we can't parse the timestamp, use the record

        fee_type = item.get("fee_type", "")
        try:
            multiplier = float(item.get("fee_multiplier", 0))
        except (ValueError, TypeError):
            logger.warning(
                "Invalid fee_multiplier for series %s: %r; skipping",
                series, item.get("fee_multiplier"),
            )
            continue

        if not (0.0 <= multiplier <= 1.0):
            logger.warning(
                "Fee multiplier %.4f for series %s outside [0, 1]; skipping",
                multiplier, series,
            )
            continue

        if fee_type == "quadratic_with_maker_fees":
            taker = multiplier
            maker = multiplier * 0.25
        elif fee_type == "quadratic":
            taker = multiplier
            maker = 0.0
        elif fee_type == "flat":
            taker = multiplier
            maker = multiplier
        else:
            logger.warning(
                "Unknown fee_type %r for series %s; defaulting to 0.25x maker ratio",
                fee_type, series,
            )
            taker = multiplier
            maker = multiplier * 0.25

        _cache[series] = _CacheEntry(
            multipliers=FeeMultipliers(taker=taker, maker=maker),
            fetched_at=now,
        )

    if _cache:
        logger.debug("Fee cache refreshed: %d series", len(_cache))


def clear_cache() -> None:
    """Clear the fee cache (for testing)."""
    _cache.clear()
