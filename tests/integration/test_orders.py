"""Integration tests for order placement (requires API credentials)."""

import pytest

from gimmes.config import load_config
from gimmes.kalshi.client import KalshiClient
from gimmes.kalshi.markets import list_markets
from gimmes.kalshi.orders import cancel_order, create_order, list_orders
from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide

pytestmark = pytest.mark.integration


@pytest.fixture
def api_config():
    config = load_config()
    if not config.api_key or not config.private_key_path.exists():
        pytest.skip("API credentials not configured")
    return config


class TestOrders:
    async def test_place_and_cancel_order(self, api_config) -> None:
        """Place a limit order at a low price, then cancel it."""
        async with KalshiClient(api_config) as client:
            # Find an open market
            markets, _ = await list_markets(client, limit=5)
            if not markets:
                pytest.skip("No open markets found")

            market = markets[0]
            # Place at a very low price (unlikely to fill)
            params = CreateOrderParams(
                ticker=market.ticker,
                action=OrderAction.BUY,
                side=OrderSide.YES,
                count=1,
                yes_price=1,  # 1 cent — won't fill
                post_only=True,
            )

            order = await create_order(client, params)
            assert order.order_id
            assert order.ticker == market.ticker

            # Cancel it
            await cancel_order(client, order.order_id)

    async def test_list_orders(self, api_config) -> None:
        async with KalshiClient(api_config) as client:
            orders, cursor = await list_orders(client, limit=5)
            assert isinstance(orders, list)
