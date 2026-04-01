"""Tests for Kalshi historical API wrappers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gimmes.kalshi.historical import Candle, get_candlesticks, list_historical_markets


@pytest.fixture
def mock_client() -> AsyncMock:
    return AsyncMock()


class TestListHistoricalMarkets:
    @pytest.mark.asyncio
    async def test_parses_markets_and_cursor(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {
            "markets": [
                {
                    "ticker": "KXCPI-26MAR-T3.0",
                    "event_ticker": "KXCPI-26MAR",
                    "series_ticker": "KXCPI",
                    "title": "CPI above 3.0%?",
                    "status": "finalized",
                    "yes_bid_dollars": "0.70",
                    "yes_ask_dollars": "0.75",
                    "last_price_dollars": "0.72",
                    "volume_fp": "5000.00",
                    "volume_24h_fp": "1200.00",
                    "open_interest_fp": "800.00",
                    "result": "yes",
                },
            ],
            "cursor": "next_page_token",
        }

        markets, cursor = await list_historical_markets(mock_client)

        assert len(markets) == 1
        assert markets[0].ticker == "KXCPI-26MAR-T3.0"
        assert markets[0].result == "yes"
        assert markets[0].yes_bid == 0.70
        assert cursor == "next_page_token"
        mock_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_response(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"markets": []}

        markets, cursor = await list_historical_markets(mock_client)

        assert markets == []
        assert cursor is None

    @pytest.mark.asyncio
    async def test_passes_event_ticker_param(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"markets": []}

        await list_historical_markets(mock_client, event_ticker="KXCPI-26MAR")

        call_args = mock_client.get.call_args
        assert call_args[1]["params"]["event_ticker"] == "KXCPI-26MAR"


class TestGetCandlesticks:
    @pytest.mark.asyncio
    async def test_parses_candles(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {
            "candlesticks": [
                {
                    "end_period_ts": 1700000000,
                    "yes_bid": {"open": "0.60", "high": "0.65", "low": "0.58", "close": "0.63"},
                    "yes_ask": {"open": "0.62", "high": "0.67", "low": "0.60", "close": "0.65"},
                    "price": {"open": "0.61", "high": "0.66", "low": "0.59", "close": "0.64"},
                    "volume": "500.00",
                    "open_interest": "200.00",
                },
            ],
        }

        candles = await get_candlesticks(
            mock_client, "KXTEST", start_ts=1699900000, end_ts=1700100000,
        )

        assert len(candles) == 1
        c = candles[0]
        assert isinstance(c, Candle)
        assert c.end_period_ts == 1700000000
        assert c.yes_bid_close == 0.63
        assert c.yes_ask_close == 0.65
        assert c.price_close == 0.64
        assert c.volume == 500
        assert c.open_interest == 200

    @pytest.mark.asyncio
    async def test_sorts_by_timestamp(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {
            "candlesticks": [
                {
                    "end_period_ts": 1700200000,
                    "yes_bid": {"open": "0", "high": "0", "low": "0", "close": "0.50"},
                    "yes_ask": {"open": "0", "high": "0", "low": "0", "close": "0.55"},
                    "price": {"open": "0", "high": "0", "low": "0", "close": "0.52"},
                    "volume": "100", "open_interest": "50",
                },
                {
                    "end_period_ts": 1700100000,
                    "yes_bid": {"open": "0", "high": "0", "low": "0", "close": "0.48"},
                    "yes_ask": {"open": "0", "high": "0", "low": "0", "close": "0.53"},
                    "price": {"open": "0", "high": "0", "low": "0", "close": "0.50"},
                    "volume": "80", "open_interest": "40",
                },
            ],
        }

        candles = await get_candlesticks(
            mock_client, "KXTEST", start_ts=1700000000, end_ts=1700300000,
        )

        assert candles[0].end_period_ts == 1700100000
        assert candles[1].end_period_ts == 1700200000

    @pytest.mark.asyncio
    async def test_empty_candles(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {"candlesticks": []}

        candles = await get_candlesticks(
            mock_client, "KXTEST", start_ts=1700000000, end_ts=1700100000,
        )

        assert candles == []
