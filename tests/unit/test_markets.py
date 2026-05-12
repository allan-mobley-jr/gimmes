"""Tests for Kalshi live market API wrappers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gimmes.kalshi.markets import (
    get_series_fee_changes,
    list_all_markets,
    list_markets,
    list_series,
)


@pytest.fixture
def mock_client() -> AsyncMock:
    return AsyncMock()


class TestListMarketsDateFiltering:
    @pytest.mark.asyncio
    async def test_passes_close_ts_params(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"markets": [], "cursor": None}

        await list_markets(
            mock_client,
            min_close_ts=1700000000,
            max_close_ts=1710000000,
        )

        call_args = mock_client.get.call_args
        params = call_args[1]["params"]
        assert params["min_close_ts"] == 1700000000
        assert params["max_close_ts"] == 1710000000

    @pytest.mark.asyncio
    async def test_omits_close_ts_when_none(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"markets": [], "cursor": None}

        await list_markets(mock_client)

        call_args = mock_client.get.call_args
        params = call_args[1]["params"]
        assert "min_close_ts" not in params
        assert "max_close_ts" not in params

    @pytest.mark.asyncio
    async def test_null_markets_value_coerces_to_empty(
        self, mock_client: AsyncMock,
    ) -> None:
        mock_client.get.return_value = {"markets": None, "cursor": None}
        markets, cursor = await list_markets(mock_client)
        assert markets == []
        assert cursor is None


class TestListAllMarketsParams:
    @pytest.mark.asyncio
    async def test_respects_max_pages(self, mock_client: AsyncMock) -> None:
        """max_pages limits pagination even when cursor keeps coming."""
        mock_client.get.return_value = {
            "markets": [{"ticker": "T", "status": "finalized"}],
            "cursor": "next",
        }

        markets = await list_all_markets(mock_client, max_pages=3)

        assert mock_client.get.call_count == 3
        assert len(markets) == 3

    @pytest.mark.asyncio
    async def test_threads_timestamps(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"markets": [], "cursor": None}

        await list_all_markets(
            mock_client,
            min_close_ts=1700000000,
            max_close_ts=1710000000,
        )

        call_args = mock_client.get.call_args
        params = call_args[1]["params"]
        assert params["min_close_ts"] == 1700000000
        assert params["max_close_ts"] == 1710000000

    @pytest.mark.asyncio
    async def test_default_max_pages_is_50(self, mock_client: AsyncMock) -> None:
        """Without max_pages, should use default of 50."""
        mock_client.get.return_value = {
            "markets": [{"ticker": "T", "status": "finalized"}],
            "cursor": "next",
        }

        await list_all_markets(mock_client)

        assert mock_client.get.call_count == 50


class TestListSeries:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response,expected",
        [
            ({"series": None}, []),
            ({"series": []}, []),
            ({}, []),
            ({"series": [{"ticker": "X"}]}, [{"ticker": "X"}]),
        ],
    )
    async def test_normalizes_null_and_missing_series_to_empty_list(
        self,
        mock_client: AsyncMock,
        response: dict,
        expected: list,
    ) -> None:
        mock_client.get.return_value = response
        result = await list_series(mock_client)
        assert result == expected

    @pytest.mark.asyncio
    async def test_threads_category_param(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"series": []}
        await list_series(mock_client, category="Economics")
        params = mock_client.get.call_args[1]["params"]
        assert params["category"] == "Economics"


class TestGetSeriesFeeChanges:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response,expected",
        [
            ({"series_fee_change_arr": None}, []),
            ({"series_fee_change_arr": []}, []),
            ({}, []),
            (
                {"series_fee_change_arr": [{"id": 1}]},
                [{"id": 1}],
            ),
        ],
    )
    async def test_normalizes_null_and_missing_to_empty_list(
        self,
        mock_client: AsyncMock,
        response: dict,
        expected: list,
    ) -> None:
        mock_client.get.return_value = response
        result = await get_series_fee_changes(mock_client)
        assert result == expected

    @pytest.mark.asyncio
    async def test_threads_series_ticker_param(
        self, mock_client: AsyncMock,
    ) -> None:
        mock_client.get.return_value = {"series_fee_change_arr": []}
        await get_series_fee_changes(mock_client, series_ticker="KXCPI")
        params = mock_client.get.call_args[1]["params"]
        assert params["series_ticker"] == "KXCPI"
