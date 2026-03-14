"""Unit tests for WebSocket client."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gimmes.kalshi.websocket import (
    _INITIAL_BACKOFF,
    _MAX_RECONNECT_ATTEMPTS,
    CHANNELS,
    KalshiWebSocket,
)


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.ws_url = "wss://test.example.com/trade-api/ws/v2"
    config.api_key = "test-key"
    config.private_key_path.exists.return_value = True
    return config


@pytest.fixture
def ws(mock_config):
    with patch("gimmes.kalshi.websocket.load_private_key", return_value=MagicMock()):
        return KalshiWebSocket(mock_config)


class TestChannelNames:
    def test_market_lifecycle_v2(self):
        """Channel should be market_lifecycle_v2, not market_lifecycle."""
        assert "market_lifecycle_v2" in CHANNELS
        assert "market_lifecycle" not in CHANNELS

    def test_no_user_fills(self):
        """user_fills is not a valid channel — fill is the correct one."""
        assert "user_fills" not in CHANNELS
        assert "fill" in CHANNELS


class TestMessageIds:
    async def test_subscribe_uses_incrementing_ids(self, ws):
        """Each subscribe call should use a different message ID."""
        ws._connection = AsyncMock()
        ws._running = True

        await ws.subscribe(["ticker"])
        call1 = json.loads(ws._connection.send.call_args_list[0][0][0])

        await ws.subscribe(["trade"])
        call2 = json.loads(ws._connection.send.call_args_list[1][0][0])

        assert call1["id"] != call2["id"]
        assert call1["id"] < call2["id"]

    async def test_unsubscribe_uses_incrementing_ids(self, ws):
        """Unsubscribe should also use incrementing IDs."""
        ws._connection = AsyncMock()
        ws._running = True

        await ws.subscribe(["ticker"])
        await ws.unsubscribe(["ticker"])

        call1 = json.loads(ws._connection.send.call_args_list[0][0][0])
        call2 = json.loads(ws._connection.send.call_args_list[1][0][0])
        assert call1["id"] < call2["id"]


class TestSubscriptionIds:
    def test_stores_sid_from_subscribed_message(self, ws):
        """Should store subscription IDs from 'subscribed' confirmations."""
        ws._process_message({"type": "subscribed", "sid": 42, "channel": "ticker"})
        assert ws._sid_map == {"ticker": [42]}

    async def test_unsubscribe_sends_sids(self, ws):
        """Unsubscribe should send sids, not channel names."""
        ws._connection = AsyncMock()
        ws._running = True
        ws._subscriptions = {"ticker"}
        ws._sid_map = {"ticker": [42, 43]}

        await ws.unsubscribe(["ticker"])

        sent = json.loads(ws._connection.send.call_args[0][0])
        assert sent["cmd"] == "unsubscribe"
        assert sent["params"]["sids"] == [42, 43]
        assert "channels" not in sent["params"]

    async def test_unsubscribe_falls_back_to_channels_when_no_sids(self, ws):
        """When no sids are stored, fall back to channel-based unsubscribe."""
        ws._connection = AsyncMock()
        ws._running = True
        ws._subscriptions = {"ticker"}

        await ws.unsubscribe(["ticker"])

        sent = json.loads(ws._connection.send.call_args[0][0])
        assert sent["cmd"] == "unsubscribe"
        assert sent["params"]["channels"] == ["ticker"]


class TestSequenceTracking:
    def test_tracks_sequence_numbers(self, ws):
        """Should track the last sequence number."""
        ws._process_message({"seq": 1, "data": "test"})
        assert ws._last_seq == 1

        ws._process_message({"seq": 2, "data": "test"})
        assert ws._last_seq == 2

    def test_detects_sequence_gap(self, ws, caplog):
        """Should log a warning when a gap is detected."""
        with caplog.at_level(logging.WARNING):
            ws._process_message({"seq": 1})
            ws._process_message({"seq": 5})  # gap: expected 2
        assert "Sequence gap detected" in caplog.text
        assert "expected 2, got 5" in caplog.text

    def test_no_gap_warning_for_first_message(self, ws, caplog):
        """First message should not trigger a gap warning."""
        with caplog.at_level(logging.WARNING):
            ws._process_message({"seq": 100})
        assert "Sequence gap" not in caplog.text


class TestReconnection:
    def test_recv_loop_constants(self):
        """Verify reconnection constants are sensible."""
        assert _INITIAL_BACKOFF == 1.0
        assert _MAX_RECONNECT_ATTEMPTS == 10

    async def test_resubscribe_restores_channels(self, ws):
        """Resubscribe should restore all tracked subscriptions."""
        ws._connection = AsyncMock()
        ws._running = True
        ws._subscriptions = {"ticker", "trade"}
        ws._subscription_tickers = {"ticker": ["AAPL"], "trade": None}
        ws._sid_map = {"ticker": [1]}

        await ws._resubscribe()

        # All channels should be resubscribed
        assert "ticker" in ws._subscriptions
        assert "trade" in ws._subscriptions
        # Old sids should be cleared
        assert ws._last_seq is None


class TestTickerAccumulation:
    async def test_subscribe_accumulates_tickers(self, ws):
        """Subscribing to same channel with different tickers should merge them."""
        ws._connection = AsyncMock()
        ws._running = True

        await ws.subscribe(["ticker"], ["AAPL"])
        await ws.subscribe(["ticker"], ["GOOG"])

        tickers = ws._subscription_tickers["ticker"]
        assert set(tickers) == {"AAPL", "GOOG"}

    async def test_subscribe_none_tickers_not_overwritten(self, ws):
        """Subscribing with None tickers keeps None."""
        ws._connection = AsyncMock()
        ws._running = True

        await ws.subscribe(["ticker"])
        assert ws._subscription_tickers["ticker"] is None

    async def test_none_tickers_not_narrowed_by_specific_subscribe(self, ws):
        """Once subscribed with None (all tickers), don't narrow on later calls."""
        ws._connection = AsyncMock()
        ws._running = True

        await ws.subscribe(["ticker"])  # None = all tickers
        await ws.subscribe(["ticker"], ["AAPL"])  # Should not narrow

        assert ws._subscription_tickers["ticker"] is None


class TestUnsubscribeWhileDisconnected:
    async def test_updates_tracking_when_disconnected(self, ws):
        """Unsubscribe should update local state even if not connected."""
        ws._connection = None
        ws._subscriptions = {"ticker", "trade"}
        ws._subscription_tickers = {"ticker": None, "trade": None}

        await ws.unsubscribe(["ticker"])

        assert "ticker" not in ws._subscriptions
        assert "ticker" not in ws._subscription_tickers
        assert "trade" in ws._subscriptions
