"""Tests for paper trading fill simulator."""

from __future__ import annotations

import pytest

from gimmes.models.market import Orderbook, OrderbookLevel
from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide
from gimmes.paper.fill_simulator import simulate_fill


@pytest.fixture
def orderbook() -> Orderbook:
    """Orderbook with YES bids and NO bids (which imply YES asks)."""
    return Orderbook(
        ticker="TEST-MKT",
        yes_bids=[
            OrderbookLevel(price=0.68, quantity=200),
            OrderbookLevel(price=0.67, quantity=150),
            OrderbookLevel(price=0.65, quantity=300),
        ],
        no_bids=[
            OrderbookLevel(price=0.30, quantity=180),  # YES ask = 0.70
            OrderbookLevel(price=0.28, quantity=250),  # YES ask = 0.72
        ],
    )


# ---------------------------------------------------------------------------
# Maker fill tests
# ---------------------------------------------------------------------------


class TestMakerFill:
    def test_maker_buy_yes_marketable_fills(self, orderbook: Orderbook) -> None:
        """Maker buy YES at 70c fills immediately (best ask is 70c)."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=70,
            post_only=True,
        )
        result = simulate_fill(params, orderbook)
        assert result.total_filled == 10
        assert result.remaining_count == 0
        assert len(result.fills) == 1
        assert result.fills[0].price_cents == 70
        assert result.fills[0].is_taker is False
        assert result.total_cost > 0

    def test_maker_buy_yes_above_ask_fills(self, orderbook: Orderbook) -> None:
        """Maker buy YES at 75c (above best ask 70c) fills at limit price."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=5,
            yes_price=75,
            post_only=True,
        )
        result = simulate_fill(params, orderbook)
        assert result.total_filled == 5
        assert result.fills[0].price_cents == 75

    def test_maker_buy_yes_below_ask_rests(self, orderbook: Orderbook) -> None:
        """Maker buy YES at 65c (below best ask 70c) rests on book."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=65,
            post_only=True,
        )
        result = simulate_fill(params, orderbook)
        assert result.total_filled == 0
        assert result.remaining_count == 10
        assert len(result.fills) == 0
        assert result.total_cost == 0.0

    def test_maker_sell_yes_at_bid_fills(self, orderbook: Orderbook) -> None:
        """Maker sell YES at 68c (at best bid) fills."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=10,
            yes_price=68,
            post_only=True,
        )
        result = simulate_fill(params, orderbook)
        assert result.total_filled == 10

    def test_maker_sell_yes_above_bid_rests(self, orderbook: Orderbook) -> None:
        """Maker sell YES at 72c (above best bid 68c) rests."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=10,
            yes_price=72,
            post_only=True,
        )
        result = simulate_fill(params, orderbook)
        assert result.total_filled == 0
        assert result.remaining_count == 10

    def test_maker_fee_is_maker_rate(self, orderbook: Orderbook) -> None:
        """Maker fills use the maker fee multiplier (0.0175)."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=100,
            yes_price=70,
            post_only=True,
        )
        result = simulate_fill(params, orderbook)
        assert result.total_filled == 100
        fill = result.fills[0]
        assert fill.is_taker is False
        # maker fee for 100 contracts at $0.70: ceil(0.0175 * 100 * 0.70 * 0.30 * 100) / 100
        assert fill.fee > 0


# ---------------------------------------------------------------------------
# Taker fill tests
# ---------------------------------------------------------------------------


class TestTakerFill:
    def test_taker_buy_yes_full_fill(self, orderbook: Orderbook) -> None:
        """Taker buy YES walks NO bids (converted to YES asks)."""
        # NO bids: 0.30 (qty=180, ask=0.70), 0.28 (qty=250, ask=0.72)
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=100,
            yes_price=72,
            post_only=False,
        )
        result = simulate_fill(params, orderbook)
        assert result.total_filled == 100
        assert result.remaining_count == 0
        # Should fill at 70c first (cheapest ask)
        assert result.fills[0].price_cents == 70

    def test_taker_buy_yes_partial_fill(self, orderbook: Orderbook) -> None:
        """Taker buy YES 200 contracts but only 180 available at 70c."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=200,
            yes_price=70,  # Only willing to pay up to 70c
            post_only=False,
        )
        result = simulate_fill(params, orderbook)
        # Only 180 available at 70c (NO bid at 0.30 → YES ask 0.70)
        assert result.total_filled == 180
        assert result.remaining_count == 20

    def test_taker_buy_yes_walks_multiple_levels(self, orderbook: Orderbook) -> None:
        """Taker buy YES 300 contracts walks through both levels."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=300,
            yes_price=75,  # Willing to pay up to 75c
            post_only=False,
        )
        result = simulate_fill(params, orderbook)
        # Level 1: 180 @ 70c, Level 2: 120 @ 72c
        assert result.total_filled == 300
        assert len(result.fills) == 2
        assert result.fills[0].count == 180
        assert result.fills[0].price_cents == 70
        assert result.fills[1].count == 120
        assert result.fills[1].price_cents == 72

    def test_taker_buy_yes_no_fill_price_too_low(self, orderbook: Orderbook) -> None:
        """Taker buy YES at 60c — no liquidity available below that."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=60,
            post_only=False,
        )
        result = simulate_fill(params, orderbook)
        assert result.total_filled == 0
        assert result.remaining_count == 10

    def test_taker_sell_yes_walks_bids(self, orderbook: Orderbook) -> None:
        """Taker sell YES walks YES bids (best bid first)."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=250,
            yes_price=65,  # Willing to sell down to 65c
            post_only=False,
        )
        result = simulate_fill(params, orderbook)
        # Level 1: 200 @ 68c, Level 2: 50 @ 67c (of 150 available)
        assert result.total_filled == 250
        assert len(result.fills) == 2
        assert result.fills[0].price_cents == 68
        assert result.fills[1].price_cents == 67

    def test_taker_fills_have_taker_fees(self, orderbook: Orderbook) -> None:
        """Taker fills use the taker fee multiplier."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=72,
            post_only=False,
        )
        result = simulate_fill(params, orderbook)
        assert result.total_filled == 10
        assert all(f.is_taker for f in result.fills)
        assert all(f.fee > 0 for f in result.fills)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_orderbook(self) -> None:
        """No fills when orderbook is empty."""
        empty_ob = Orderbook(ticker="EMPTY", yes_bids=[], no_bids=[])
        params = CreateOrderParams(
            ticker="EMPTY",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=70,
            post_only=False,
        )
        result = simulate_fill(params, empty_ob)
        assert result.total_filled == 0

    def test_maker_empty_orderbook_rests(self) -> None:
        """Maker order rests on empty orderbook."""
        empty_ob = Orderbook(ticker="EMPTY", yes_bids=[], no_bids=[])
        params = CreateOrderParams(
            ticker="EMPTY",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=70,
            post_only=True,
        )
        result = simulate_fill(params, empty_ob)
        assert result.total_filled == 0
        assert result.remaining_count == 10

    def test_total_cost_includes_fees(self, orderbook: Orderbook) -> None:
        """total_cost = sum of (count * price + fee) across fills."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=72,
            post_only=False,
        )
        result = simulate_fill(params, orderbook)
        expected_cost = sum(
            f.count * (f.price_cents / 100.0) + f.fee for f in result.fills
        )
        assert abs(result.total_cost - expected_cost) < 0.001
