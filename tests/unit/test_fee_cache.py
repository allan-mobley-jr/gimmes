"""Unit tests for fee multiplier cache."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from gimmes.strategy.fee_cache import (
    _DEFAULT_TTL,
    _cache,
    _CacheEntry,
    clear_cache,
    get_multipliers,
    refresh_fee_cache,
)
from gimmes.strategy.fees import DEFAULT_FEE_MULTIPLIERS, FeeMultipliers


@pytest.fixture(autouse=True)
def _clean_cache():
    """Ensure cache is clean before and after each test."""
    clear_cache()
    yield
    clear_cache()


class TestGetMultipliers:
    def test_returns_defaults_when_empty(self) -> None:
        result = get_multipliers("UNKNOWN")
        assert result == DEFAULT_FEE_MULTIPLIERS

    def test_returns_cached_values(self) -> None:
        _cache["KXCPI"] = _CacheEntry(
            multipliers=FeeMultipliers(taker=0.10, maker=0.025),
            fetched_at=time.monotonic(),
        )
        result = get_multipliers("KXCPI")
        assert result.taker == 0.10
        assert result.maker == 0.025

    def test_returns_defaults_when_expired(self) -> None:
        _cache["KXCPI"] = _CacheEntry(
            multipliers=FeeMultipliers(taker=0.10, maker=0.025),
            fetched_at=time.monotonic() - _DEFAULT_TTL - 1,
        )
        result = get_multipliers("KXCPI")
        assert result == DEFAULT_FEE_MULTIPLIERS

    def test_different_series_independent(self) -> None:
        now = time.monotonic()
        _cache["KXCPI"] = _CacheEntry(FeeMultipliers(0.10, 0.025), now)
        _cache["KXGDP"] = _CacheEntry(FeeMultipliers(0.05, 0.0), now)

        r1 = get_multipliers("KXCPI")
        r2 = get_multipliers("KXGDP")
        assert r1.taker == 0.10
        assert r1.maker == 0.025
        assert r2.taker == 0.05
        assert r2.maker == 0.0


class TestRefreshFeeCache:
    @pytest.mark.asyncio
    async def test_populates_cache_quadratic_with_maker(self) -> None:
        client = AsyncMock()
        with patch("gimmes.kalshi.markets.get_series_fee_changes") as mock:
            mock.return_value = [
                {
                    "id": "1",
                    "series_ticker": "KXCPI",
                    "fee_type": "quadratic_with_maker_fees",
                    "fee_multiplier": 0.10,
                    "scheduled_ts": "2026-01-01T00:00:00Z",
                },
            ]
            await refresh_fee_cache(client)

        result = get_multipliers("KXCPI")
        assert result.taker == 0.10
        assert result.maker == pytest.approx(0.025)

    @pytest.mark.asyncio
    async def test_populates_cache_quadratic_no_maker(self) -> None:
        client = AsyncMock()
        with patch("gimmes.kalshi.markets.get_series_fee_changes") as mock:
            mock.return_value = [
                {
                    "id": "2",
                    "series_ticker": "KXGDP",
                    "fee_type": "quadratic",
                    "fee_multiplier": 0.05,
                    "scheduled_ts": "2026-01-01T00:00:00Z",
                },
            ]
            await refresh_fee_cache(client)

        result = get_multipliers("KXGDP")
        assert result.taker == 0.05
        assert result.maker == 0.0

    @pytest.mark.asyncio
    async def test_populates_cache_flat(self) -> None:
        client = AsyncMock()
        with patch("gimmes.kalshi.markets.get_series_fee_changes") as mock:
            mock.return_value = [
                {
                    "id": "3",
                    "series_ticker": "KXSPORTS",
                    "fee_type": "flat",
                    "fee_multiplier": 0.02,
                    "scheduled_ts": "2026-01-01T00:00:00Z",
                },
            ]
            await refresh_fee_cache(client)

        result = get_multipliers("KXSPORTS")
        assert result.taker == 0.02
        assert result.maker == 0.02

    @pytest.mark.asyncio
    async def test_unknown_fee_type_uses_quarter_ratio(self) -> None:
        client = AsyncMock()
        with patch("gimmes.kalshi.markets.get_series_fee_changes") as mock:
            mock.return_value = [
                {
                    "id": "5",
                    "series_ticker": "KXNEW",
                    "fee_type": "some_future_type",
                    "fee_multiplier": 0.08,
                },
            ]
            await refresh_fee_cache(client)

        result = get_multipliers("KXNEW")
        assert result.taker == 0.08
        assert result.maker == pytest.approx(0.02)  # 0.08 * 0.25

    @pytest.mark.asyncio
    async def test_handles_network_failure_gracefully(self) -> None:
        client = AsyncMock()
        with patch("gimmes.kalshi.markets.get_series_fee_changes") as mock:
            mock.side_effect = OSError("Connection refused")
            await refresh_fee_cache(client)

        # Should still return defaults
        result = get_multipliers("KXCPI")
        assert result == DEFAULT_FEE_MULTIPLIERS

    @pytest.mark.asyncio
    async def test_skips_entries_without_series_ticker(self) -> None:
        client = AsyncMock()
        with patch("gimmes.kalshi.markets.get_series_fee_changes") as mock:
            mock.return_value = [
                {
                    "id": "4",
                    "series_ticker": "",
                    "fee_type": "quadratic",
                    "fee_multiplier": 0.05,
                },
            ]
            await refresh_fee_cache(client)

        assert len(_cache) == 0

    @pytest.mark.asyncio
    async def test_multiple_series(self) -> None:
        client = AsyncMock()
        with patch("gimmes.kalshi.markets.get_series_fee_changes") as mock:
            mock.return_value = [
                {
                    "id": "1",
                    "series_ticker": "KXCPI",
                    "fee_type": "quadratic_with_maker_fees",
                    "fee_multiplier": 0.10,
                },
                {
                    "id": "2",
                    "series_ticker": "KXGDP",
                    "fee_type": "quadratic",
                    "fee_multiplier": 0.05,
                },
            ]
            await refresh_fee_cache(client)

        assert len(_cache) == 2
        r1 = get_multipliers("KXCPI")
        assert r1.taker == 0.10
        r2 = get_multipliers("KXGDP")
        assert r2.taker == 0.05


class TestRefreshEdgeCases:
    @pytest.mark.asyncio
    async def test_skips_invalid_multiplier_value(self) -> None:
        client = AsyncMock()
        with patch("gimmes.kalshi.markets.get_series_fee_changes") as mock:
            mock.return_value = [
                {
                    "id": "1",
                    "series_ticker": "KXBAD",
                    "fee_type": "quadratic",
                    "fee_multiplier": "pending",
                },
            ]
            await refresh_fee_cache(client)

        assert len(_cache) == 0

    @pytest.mark.asyncio
    async def test_skips_out_of_range_multiplier(self) -> None:
        client = AsyncMock()
        with patch("gimmes.kalshi.markets.get_series_fee_changes") as mock:
            mock.return_value = [
                {
                    "id": "1",
                    "series_ticker": "KXBAD",
                    "fee_type": "quadratic",
                    "fee_multiplier": 5.0,
                },
            ]
            await refresh_fee_cache(client)

        assert len(_cache) == 0

    @pytest.mark.asyncio
    async def test_skips_future_scheduled_changes(self) -> None:
        client = AsyncMock()
        with patch("gimmes.kalshi.markets.get_series_fee_changes") as mock:
            mock.return_value = [
                {
                    "id": "1",
                    "series_ticker": "KXCPI",
                    "fee_type": "quadratic_with_maker_fees",
                    "fee_multiplier": 0.10,
                    "scheduled_ts": "2020-01-01T00:00:00Z",  # past
                },
                {
                    "id": "2",
                    "series_ticker": "KXFUT",
                    "fee_type": "quadratic",
                    "fee_multiplier": 0.05,
                    "scheduled_ts": "2099-01-01T00:00:00Z",  # future
                },
            ]
            await refresh_fee_cache(client)

        assert "KXCPI" in _cache
        assert "KXFUT" not in _cache


class TestClearCache:
    def test_clears_all_entries(self) -> None:
        _cache["KXCPI"] = _CacheEntry(FeeMultipliers(0.10, 0.025), time.monotonic())
        assert len(_cache) == 1
        clear_cache()
        assert len(_cache) == 0
