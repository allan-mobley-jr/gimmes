"""Fee multiplier cache -- fetches per-series multipliers from Kalshi API."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gimmes.strategy.fees import DEFAULT_FEE_MULTIPLIERS, FeeMultipliers

if TYPE_CHECKING:
    from gimmes.kalshi.client import KalshiClient

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 3600.0  # 1 hour


@dataclass
class _CacheEntry:
    multipliers: FeeMultipliers
    fetched_at: float  # time.monotonic()


# Module-level cache: series_ticker -> entry
_cache: dict[str, _CacheEntry] = {}


def _parse_scheduled_ts(value: object) -> datetime | None:
    """Parse an ISO 8601 timestamp, handling trailing 'Z' for Python 3.11."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def get_multipliers(series_ticker: str) -> FeeMultipliers:
    """Return fee multipliers from cache, or defaults if missing/expired."""
    entry = _cache.get(series_ticker)
    if entry and (time.monotonic() - entry.fetched_at) < _DEFAULT_TTL:
        return entry.multipliers
    return DEFAULT_FEE_MULTIPLIERS


async def refresh_fee_cache(client: KalshiClient) -> None:
    """Fetch fee changes from Kalshi and populate cache.

    Args:
        client: A KalshiClient instance.
    """
    from gimmes.kalshi.markets import get_series_fee_changes

    try:
        records = await get_series_fee_changes(client)
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("Failed to fetch fee multipliers: %s; using defaults", exc)
        return

    # Sort by scheduled_ts so the most recent effective record per series wins
    _epoch = datetime.min.replace(tzinfo=UTC)

    def _sort_key(item: dict) -> datetime:  # type: ignore[type-arg]
        ts = _parse_scheduled_ts(item.get("scheduled_ts", ""))
        return ts if ts is not None else _epoch

    records.sort(key=_sort_key)

    now_utc = datetime.now(UTC)
    now = time.monotonic()
    for item in records:
        series = item.get("series_ticker", "")
        if not series:
            continue

        # Skip future-scheduled changes that haven't taken effect yet
        scheduled = item.get("scheduled_ts")
        if scheduled:
            ts = _parse_scheduled_ts(scheduled)
            if ts is not None and ts > now_utc:
                continue

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
