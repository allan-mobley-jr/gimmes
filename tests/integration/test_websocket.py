"""Integration tests for WebSocket client (requires API credentials)."""


import pytest

from gimmes.config import load_config
from gimmes.kalshi.websocket import KalshiWebSocket

pytestmark = pytest.mark.integration


@pytest.fixture
def api_config():
    config = load_config()
    if not config.api_key or not config.private_key_path.exists():
        pytest.skip("API credentials not configured")
    return config


class TestWebSocket:
    async def test_connect_and_subscribe(self, api_config) -> None:
        """Connect, subscribe to ticker, receive at least one message."""
        async with KalshiWebSocket(api_config) as ws:
            await ws.subscribe(["ticker"])
            # Try to get one message within 5 seconds
            async for msg in ws.messages():
                assert isinstance(msg, dict)
                break  # Just need one message

    async def test_subscribe_unsubscribe(self, api_config) -> None:
        """Subscribe and unsubscribe from channels."""
        async with KalshiWebSocket(api_config) as ws:
            await ws.subscribe(["orderbook_delta", "ticker"])
            assert "orderbook_delta" in ws._subscriptions
            assert "ticker" in ws._subscriptions

            await ws.unsubscribe(["ticker"])
            assert "ticker" not in ws._subscriptions
            assert "orderbook_delta" in ws._subscriptions
