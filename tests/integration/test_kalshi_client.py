"""Integration tests for Kalshi API client (requires API credentials)."""

import pytest

from gimmes.config import load_config
from gimmes.kalshi.client import KalshiClient
from gimmes.kalshi.markets import get_orderbook, list_markets
from gimmes.kalshi.portfolio import get_balance

pytestmark = pytest.mark.integration


@pytest.fixture
def api_config():
    config = load_config()
    if not config.api_key or not config.private_key_path.exists():
        pytest.skip("API credentials not configured")
    return config


class TestKalshiClient:
    async def test_list_markets(self, api_config) -> None:
        async with KalshiClient(api_config) as client:
            markets, cursor = await list_markets(client, limit=5)
            assert len(markets) > 0
            assert markets[0].ticker

    async def test_get_balance(self, api_config) -> None:
        async with KalshiClient(api_config) as client:
            balance = await get_balance(client)
            assert isinstance(balance, float)
            assert balance >= 0

    async def test_get_orderbook(self, api_config) -> None:
        async with KalshiClient(api_config) as client:
            markets, _ = await list_markets(client, limit=1)
            if markets:
                ob = await get_orderbook(client, markets[0].ticker)
                assert ob.ticker == markets[0].ticker
