"""Tests for market staleness tracking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from gimmes.models.market import Market, MarketStatus
from gimmes.strategy.staleness import (
    StalenessEntry,
    check_staleness,
    load_staleness,
    save_staleness,
)


def _market(
    ticker: str = "T1",
    price: float = 0.65,
    volume: int = 500,
    open_interest: int = 1000,
) -> Market:
    return Market(
        ticker=ticker,
        status=MarketStatus.ACTIVE,
        yes_bid=price - 0.01,
        yes_ask=price + 0.01,
        last_price=price,
        volume=volume,
        volume_24h=volume,
        open_interest=open_interest,
    )


class TestCheckStaleness:
    def test_fresh_market_not_skipped(self) -> None:
        data: dict[str, StalenessEntry] = {}
        kept, skipped, updated = check_staleness([_market()], data, threshold=5)
        assert len(kept) == 1
        assert skipped == []
        assert updated["T1"]["count"] == 0

    def test_inactive_market_increments_count(self) -> None:
        """No price change, no volume, no OI change → stale count increments."""
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=0, open_interest=1000, count=2,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        kept, skipped, updated = check_staleness(
            [_market(volume=0)], data, threshold=5,
        )
        assert len(kept) == 1
        assert updated["T1"]["count"] == 3

    def test_count_reaches_threshold_skips(self) -> None:
        """Inactive market reaching threshold gets skipped."""
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=0, open_interest=1000, count=4,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        kept, skipped, updated = check_staleness(
            [_market(volume=0)], data, threshold=5,
        )
        assert len(kept) == 0
        assert skipped == ["T1"]
        assert updated["T1"]["count"] == 5

    def test_price_change_resets_count(self) -> None:
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.60, volume=0, open_interest=1000, count=4,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        # Market at 0.65, prior was 0.60 — 5¢ change
        kept, skipped, updated = check_staleness(
            [_market(volume=0)], data, threshold=5,
        )
        assert len(kept) == 1
        assert updated["T1"]["count"] == 0

    def test_stable_price_with_volume_not_stale(self) -> None:
        """Same price but active volume → NOT stale. This is the key fix."""
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=500, open_interest=1000, count=4,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        kept, skipped, updated = check_staleness(
            [_market(volume=500)], data, threshold=5,
        )
        assert len(kept) == 1
        assert updated["T1"]["count"] == 0

    def test_oi_change_resets_count(self) -> None:
        """Same price, no volume, but OI changed → not stale."""
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=0, open_interest=800, count=4,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        kept, skipped, updated = check_staleness(
            [_market(volume=0, open_interest=1000)], data, threshold=5,
        )
        assert len(kept) == 1
        assert updated["T1"]["count"] == 0

    def test_no_volume_no_oi_no_price_increments(self) -> None:
        """Truly dead market: no price move, no volume, same OI → stale."""
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=0, open_interest=1000, count=0,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        kept, skipped, updated = check_staleness(
            [_market(volume=0)], data, threshold=5,
        )
        assert len(kept) == 1
        assert updated["T1"]["count"] == 1

    def test_volume_at_floor_not_stale(self) -> None:
        """Volume exactly at floor → active, count resets."""
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=10, open_interest=1000, count=4,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        kept, skipped, updated = check_staleness(
            [_market(volume=10)], data, threshold=5, volume_floor=10,
        )
        assert len(kept) == 1
        assert updated["T1"]["count"] == 0

    def test_volume_below_floor_stale(self) -> None:
        """Volume just below floor → inactive, count increments."""
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=9, open_interest=1000, count=3,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        kept, skipped, updated = check_staleness(
            [_market(volume=9)], data, threshold=5, volume_floor=10,
        )
        assert len(kept) == 1
        assert updated["T1"]["count"] == 4

    def test_old_entry_missing_open_interest(self) -> None:
        """Backward compat: old entries without open_interest don't crash."""
        data: dict[str, StalenessEntry] = {
            "T1": {  # type: ignore[typeddict-item]
                "price": 0.65,
                "volume": 0,
                "count": 3,
                "last_cycle": datetime.now(UTC).isoformat(),
            },
        }
        # OI defaults to 0, market has OI=1000, so oi_changed=True → reset
        kept, skipped, updated = check_staleness(
            [_market(volume=0)], data, threshold=5,
        )
        assert len(kept) == 1
        assert updated["T1"]["count"] == 0
        assert updated["T1"]["open_interest"] == 1000

    def test_threshold_zero_disables(self) -> None:
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=0, open_interest=1000, count=99,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        kept, skipped, updated = check_staleness(
            [_market(volume=0)], data, threshold=0,
        )
        assert len(kept) == 1
        assert skipped == []

    def test_stale_entries_pruned(self) -> None:
        old_time = (datetime.now(UTC) - timedelta(hours=49)).isoformat()
        data: dict[str, StalenessEntry] = {
            "OLD": StalenessEntry(
                price=0.50, volume=100, open_interest=500, count=3,
                last_cycle=old_time,
            ),
            "T1": StalenessEntry(
                price=0.65, volume=500, open_interest=1000, count=0,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        _, _, updated = check_staleness([_market()], data, threshold=5)
        assert "OLD" not in updated
        assert "T1" in updated


class TestLoadSave:
    def test_load_missing_file(self, tmp_path: Path) -> None:
        result = load_staleness(tmp_path / "nonexistent.json")
        assert result == {}

    def test_load_corrupt_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json{{{")
        result = load_staleness(bad)
        assert result == {}

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "staleness.json"
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=500, open_interest=1000, count=3,
                last_cycle="2026-04-03T12:00:00+00:00",
            ),
        }
        save_staleness(data, path)
        loaded = load_staleness(path)
        assert loaded == data
