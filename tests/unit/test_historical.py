"""Tests for Kalshi historical API wrappers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gimmes.kalshi.historical import (
    Candle,
    _parse_candle,
    get_candlesticks,
    list_all_historical_markets,
    list_historical_markets,
)


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


class TestListAllHistoricalMarketsFiltering:
    @pytest.mark.asyncio
    async def test_filters_by_series_tickers(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {
            "markets": [
                {
                    "ticker": "KXCPI-26MAR-T3.0",
                    "series_ticker": "KXCPI",
                    "status": "finalized",
                    "result": "yes",
                },
                {
                    "ticker": "KXFED-26MAR-T5.0",
                    "series_ticker": "KXFED",
                    "status": "finalized",
                    "result": "no",
                },
            ],
            "cursor": None,
        }

        markets = await list_all_historical_markets(
            mock_client, series_tickers={"KXCPI"},
        )

        assert len(markets) == 1
        assert markets[0].ticker == "KXCPI-26MAR-T3.0"

    @pytest.mark.asyncio
    async def test_filters_by_date_range(self, mock_client: AsyncMock) -> None:
        from datetime import UTC, datetime

        mock_client.get.return_value = {
            "markets": [
                {
                    "ticker": "EARLY",
                    "status": "finalized",
                    "close_time": "2024-01-15T00:00:00Z",
                },
                {
                    "ticker": "INRANGE",
                    "status": "finalized",
                    "close_time": "2025-06-15T00:00:00Z",
                },
                {
                    "ticker": "LATE",
                    "status": "finalized",
                    "close_time": "2026-12-15T00:00:00Z",
                },
            ],
            "cursor": None,
        }

        markets = await list_all_historical_markets(
            mock_client,
            min_close_time=datetime(2025, 1, 1, tzinfo=UTC),
            max_close_time=datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
        )

        assert len(markets) == 1
        assert markets[0].ticker == "INRANGE"

    @pytest.mark.asyncio
    async def test_no_filters_returns_all(self, mock_client: AsyncMock) -> None:
        mock_client.get.return_value = {
            "markets": [
                {"ticker": "A", "status": "finalized"},
                {"ticker": "B", "status": "finalized"},
            ],
            "cursor": None,
        }

        markets = await list_all_historical_markets(mock_client)

        assert len(markets) == 2


class TestParseCandleLiveShape:
    """#655: the live API serves nested `*_dollars` string OHLC and
    `*_fp` counts (verified against the real endpoint 2026-07-03) —
    the parser previously read only legacy plain keys and would have
    produced all-zero candles."""

    def test_dollars_fields_parsed(self) -> None:
        c = _parse_candle({
            "end_period_ts": 1776168000,
            "yes_bid": {"open_dollars": "0.3400", "high_dollars": "0.3600",
                        "low_dollars": "0.2900", "close_dollars": "0.3200"},
            "yes_ask": {"open_dollars": "0.3600", "high_dollars": "0.3800",
                        "low_dollars": "0.3100", "close_dollars": "0.3300"},
            "price": {"open_dollars": "0.3500", "close_dollars": "0.3250"},
            "volume_fp": "1200",
            "open_interest_fp": "800",
        })
        assert c.yes_bid_close == 0.32
        assert c.yes_ask_close == 0.33
        assert c.price_close == 0.325
        assert c.volume == 1200
        assert c.open_interest == 800

    def test_legacy_plain_keys_still_parse(self) -> None:
        c = _parse_candle({
            "end_period_ts": 1,
            "yes_bid": {"close": "0.63"},
            "yes_ask": {"close": "0.70"},
            "price": {},
            "volume": 10,
            "open_interest": 5,
        })
        assert c.yes_bid_close == 0.63
        assert c.volume == 10

    def test_legacy_int_cents_fallback(self) -> None:
        """Plain-key INT values are legacy cents per the markets.py
        convention — 34 means $0.34 (#655 review: a silent 100x
        sizing error otherwise)."""
        c = _parse_candle({
            "end_period_ts": 1,
            "yes_bid": {"close": 34},
            "yes_ask": {"close": 36},
            "price": {},
        })
        assert c.yes_bid_close == 0.34
        assert c.yes_ask_close == 0.36
