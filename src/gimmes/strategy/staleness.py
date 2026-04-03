"""Market staleness tracking — skip markets with no trading activity."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from gimmes.models.market import Market

logger = logging.getLogger(__name__)


class StalenessEntry(TypedDict):
    price: float
    volume: int
    open_interest: int
    count: int
    last_cycle: str


def load_staleness(path: Path) -> dict[str, StalenessEntry]:
    """Load staleness data from JSON file. Returns empty dict on error."""
    try:
        if path.exists():
            return json.loads(path.read_text())  # type: ignore[return-value]
    except (json.JSONDecodeError, OSError):
        logger.debug("Failed to load staleness file %s", path, exc_info=True)
    return {}


def save_staleness(data: dict[str, StalenessEntry], path: Path) -> None:
    """Save staleness data atomically via temp file + rename."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.rename(path)
    except OSError:
        logger.debug("Failed to save staleness file %s", path, exc_info=True)


def check_staleness(
    candidates: list[Market],
    staleness_data: dict[str, StalenessEntry],
    *,
    threshold: int = 5,
    price_tolerance: float = 0.01,
    volume_floor: int = 10,
) -> tuple[list[Market], list[str], dict[str, StalenessEntry]]:
    """Filter out stale markets and update staleness tracking.

    A market is considered stale when it has no trading activity: price
    unchanged, volume below the floor, and open interest frozen.  Markets
    with stable prices but active volume are NOT stale — for a NO variance
    strategy, a persistent mispricing with ongoing volume is the opportunity.

    Returns:
        (kept_candidates, skipped_tickers, updated_staleness_data)
    """
    if threshold <= 0:
        return candidates, [], staleness_data

    kept: list[Market] = []
    skipped: list[str] = []
    now = datetime.now(UTC).isoformat()

    for m in candidates:
        price = m.midpoint if m.midpoint > 0 else m.last_price
        volume = m.volume_24h if m.volume_24h > 0 else m.volume
        oi = m.open_interest
        ticker = m.ticker

        entry = staleness_data.get(ticker)
        if entry is not None:
            price_changed = abs(price - entry["price"]) > price_tolerance
            has_volume = volume >= volume_floor
            oi_changed = oi != entry.get("open_interest", 0)

            if price_changed or has_volume or oi_changed:
                new_count = 0
            else:
                new_count = entry["count"] + 1
        else:
            new_count = 0

        staleness_data[ticker] = StalenessEntry(
            price=price,
            volume=volume,
            open_interest=oi,
            count=new_count,
            last_cycle=now,
        )

        if new_count >= threshold:
            skipped.append(ticker)
        else:
            kept.append(m)

    # Prune entries older than 48h
    cutoff = datetime.now(UTC) - timedelta(hours=48)
    to_remove = []
    for t, e in staleness_data.items():
        try:
            if datetime.fromisoformat(e["last_cycle"]) < cutoff:
                to_remove.append(t)
        except (ValueError, KeyError):
            to_remove.append(t)
    for t in to_remove:
        del staleness_data[t]

    return kept, skipped, staleness_data
