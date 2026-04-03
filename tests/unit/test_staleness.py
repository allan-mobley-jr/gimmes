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


def _market(ticker: str = "T1", price: float = 0.65, volume: int = 500) -> Market:
    return Market(
        ticker=ticker,
        status=MarketStatus.ACTIVE,
        yes_bid=price - 0.01,
        yes_ask=price + 0.01,
        last_price=price,
        volume=volume,
        volume_24h=volume,
    )


class TestCheckStaleness:
    def test_fresh_market_not_skipped(self) -> None:
        data: dict[str, StalenessEntry] = {}
        kept, skipped, updated = check_staleness([_market()], data, threshold=5)
        assert len(kept) == 1
        assert skipped == []
        assert updated["T1"]["count"] == 0

    def test_unchanged_price_increments_count(self) -> None:
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=500, count=2,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        kept, skipped, updated = check_staleness([_market()], data, threshold=5)
        assert len(kept) == 1
        assert updated["T1"]["count"] == 3

    def test_count_reaches_threshold_skips(self) -> None:
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=500, count=4,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        kept, skipped, updated = check_staleness([_market()], data, threshold=5)
        assert len(kept) == 0
        assert skipped == ["T1"]
        assert updated["T1"]["count"] == 5

    def test_price_change_resets_count(self) -> None:
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.60, volume=500, count=4,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        # Market at 0.65, prior was 0.60 — 5¢ change
        kept, skipped, updated = check_staleness([_market()], data, threshold=5)
        assert len(kept) == 1
        assert updated["T1"]["count"] == 0

    def test_volume_spike_resets_count(self) -> None:
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=200, count=4,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        # Same price but volume 500 > 200 * 2.0 = 400
        kept, skipped, updated = check_staleness(
            [_market(volume=500)], data, threshold=5,
        )
        assert len(kept) == 1
        assert updated["T1"]["count"] == 0

    def test_threshold_zero_disables(self) -> None:
        data: dict[str, StalenessEntry] = {
            "T1": StalenessEntry(
                price=0.65, volume=500, count=99,
                last_cycle=datetime.now(UTC).isoformat(),
            ),
        }
        kept, skipped, updated = check_staleness([_market()], data, threshold=0)
        assert len(kept) == 1
        assert skipped == []

    def test_stale_entries_pruned(self) -> None:
        old_time = (datetime.now(UTC) - timedelta(hours=49)).isoformat()
        data: dict[str, StalenessEntry] = {
            "OLD": StalenessEntry(
                price=0.50, volume=100, count=3, last_cycle=old_time,
            ),
            "T1": StalenessEntry(
                price=0.65, volume=500, count=0,
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
                price=0.65, volume=500, count=3,
                last_cycle="2026-04-03T12:00:00+00:00",
            ),
        }
        save_staleness(data, path)
        loaded = load_staleness(path)
        assert loaded == data
