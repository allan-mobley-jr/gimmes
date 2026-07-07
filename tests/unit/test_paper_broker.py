"""Tests for paper trading broker."""

from __future__ import annotations

import pytest

from gimmes.config import PaperTradingConfig
from gimmes.models.market import Orderbook, OrderbookLevel
from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide
from gimmes.paper.broker import PaperBroker
from gimmes.store.database import Database
from gimmes.strategy.fees import fee_for_order


@pytest.fixture
async def broker(tmp_path) -> PaperBroker:
    """Create a PaperBroker with in-memory-like temp DB."""
    db_path = tmp_path / "test_paper.db"
    db = Database(db_path)
    await db.connect()
    config = PaperTradingConfig(starting_balance=10_000.00)
    b = PaperBroker(db, config)
    await b.initialize()
    yield b  # type: ignore[misc]
    await db.close()


@pytest.fixture
def orderbook() -> Orderbook:
    """Orderbook where YES best ask = 70c, YES best bid = 68c."""
    return Orderbook(
        ticker="TEST-MKT",
        yes_bids=[
            OrderbookLevel(price=0.68, quantity=200),
            OrderbookLevel(price=0.67, quantity=150),
        ],
        no_bids=[
            OrderbookLevel(price=0.30, quantity=500),  # YES ask = 0.70
        ],
    )


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


class TestBalance:
    @pytest.mark.asyncio
    async def test_initial_balance(self, broker: PaperBroker) -> None:
        balance = await broker.get_balance()
        assert balance == 10_000.00

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self, broker: PaperBroker) -> None:
        """Calling initialize again doesn't reset balance."""
        await broker.initialize()
        balance = await broker.get_balance()
        assert balance == 10_000.00


# ---------------------------------------------------------------------------
# Order creation
# ---------------------------------------------------------------------------


class TestCreateOrder:
    @pytest.mark.asyncio
    async def test_buy_yes_maker_fills(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Buy 10 YES at 70c — fills immediately, balance deducted."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        order = await broker.create_order(params, orderbook)
        assert order.status == "executed"
        assert order.remaining_count == 0
        assert order.order_id.startswith("paper-")

        # Balance should be reduced by cost + fees
        balance = await broker.get_balance()
        assert balance < 10_000.00

    @pytest.mark.asyncio
    async def test_buy_yes_creates_position(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Filled order creates a paper position."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

        positions = await broker.get_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert pos.ticker == "TEST-MKT"
        assert pos.side == "yes"
        assert pos.count == 10

    @pytest.mark.asyncio
    async def test_non_marketable_maker_fills_immediately(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Non-marketable maker order fills immediately at limit price (#255)."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,  # Below ask — fills immediately in paper mode
            post_only=True,
        )
        order = await broker.create_order(params, orderbook)
        assert order.status == "executed"
        assert order.remaining_count == 0

        # Balance reduced by notional + maker fee
        notional = 10 * 0.65
        fee = fee_for_order(10, 0.65, is_taker=False)
        balance = await broker.get_balance()
        assert balance == pytest.approx(10_000.00 - notional - fee)

        # Position created in same call
        positions = await broker.get_positions()
        assert len(positions) == 1
        assert positions[0].count == 10

    @pytest.mark.asyncio
    async def test_order_records_fills(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Fills are recorded in paper_fills."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        order = await broker.create_order(params, orderbook)

        fills = await broker.list_fills(ticker="TEST-MKT")
        assert len(fills) == 1
        assert fills[0].order_id == order.order_id
        assert fills[0].count == 10

    @pytest.mark.asyncio
    async def test_multiple_buys_accumulate_position(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Two buys on same ticker accumulate into one position."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=5,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(params, orderbook)
        await broker.create_order(params, orderbook)

        positions = await broker.get_positions()
        assert len(positions) == 1
        assert positions[0].count == 10


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_nonexistent_is_noop(self, broker: PaperBroker) -> None:
        """Canceling an unknown order ID does nothing."""
        await broker.cancel_order("nonexistent-order-id")
        balance = await broker.get_balance()
        assert balance == 10_000.00


# ---------------------------------------------------------------------------
# Mark to market
# ---------------------------------------------------------------------------


class TestMarkToMarket:
    @pytest.mark.asyncio
    async def test_mark_to_market_updates_unrealized(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Mark-to-market updates unrealized P&L."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

        # Price goes up
        await broker.mark_to_market("TEST-MKT", 0.80)
        positions = await broker.get_positions()
        pos = positions[0]
        assert pos.market_price == 0.80
        # avg_price includes fees, so unrealized = (0.80 - avg_price) * 10
        assert pos.unrealized_pnl > 0

    @pytest.mark.asyncio
    async def test_mark_to_market_price_drop(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Price drop shows negative unrealized P&L."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

        await broker.mark_to_market("TEST-MKT", 0.50)
        positions = await broker.get_positions()
        assert positions[0].unrealized_pnl < 0

    @pytest.mark.asyncio
    async def test_mark_to_market_nonexistent_ticker(
        self, broker: PaperBroker
    ) -> None:
        """Mark-to-market on nonexistent ticker is a no-op."""
        await broker.mark_to_market("NONEXISTENT", 0.50)  # Should not raise

    @pytest.mark.asyncio
    async def test_mark_to_market_no_side_stores_no_price(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """NO-side positions store 1 - yes_price as market_price."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.NO,
            count=10,
            no_price=0.32,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

        await broker.mark_to_market("TEST-MKT", 0.60)
        positions = await broker.get_positions()
        no_pos = [p for p in positions if p.side == "no"][0]
        assert no_pos.market_price == pytest.approx(0.40)  # 1 - 0.60

    @pytest.mark.asyncio
    async def test_mark_to_market_no_side_unrealized_pnl(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """NO-side unrealized P&L uses NO-price space, not YES-price."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.NO,
            count=10,
            no_price=0.32,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

        # YES midpoint = 0.60 → NO price = 0.40 > entry ~0.32 → profit
        await broker.mark_to_market("TEST-MKT", 0.60)
        positions = await broker.get_positions()
        no_pos = [p for p in positions if p.side == "no"][0]
        assert no_pos.unrealized_pnl > 0

    @pytest.mark.asyncio
    async def test_mark_to_market_no_side_price_drop(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """NO-side shows negative unrealized P&L when NO price drops."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.NO,
            count=10,
            no_price=0.32,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

        # YES midpoint = 0.90 → NO price = 0.10 < entry ~0.32 → loss
        await broker.mark_to_market("TEST-MKT", 0.90)
        positions = await broker.get_positions()
        no_pos = [p for p in positions if p.side == "no"][0]
        assert no_pos.unrealized_pnl < 0


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


class TestSettlement:
    @pytest.mark.asyncio
    async def test_settle_yes_wins(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """YES position settles YES → $1/contract payout."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(params, orderbook)
        balance_after_buy = await broker.get_balance()

        await broker.settle("TEST-MKT", "yes")

        balance_after_settle = await broker.get_balance()
        # Should receive $10 (10 * $1.00)
        assert balance_after_settle == pytest.approx(balance_after_buy + 10.0)

        # Position should be zeroed out
        positions = await broker.get_positions()
        assert len(positions) == 0  # count = 0 filtered out

    @pytest.mark.asyncio
    async def test_settle_yes_loses(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """YES position settles NO → $0 payout."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(params, orderbook)
        balance_after_buy = await broker.get_balance()

        await broker.settle("TEST-MKT", "no")

        balance_after_settle = await broker.get_balance()
        # No payout
        assert balance_after_settle == pytest.approx(balance_after_buy)

    @pytest.mark.asyncio
    async def test_settle_nonexistent_is_noop(self, broker: PaperBroker) -> None:
        """Settling a nonexistent position does nothing."""
        await broker.settle("NONEXISTENT", "yes")
        balance = await broker.get_balance()
        assert balance == 10_000.00


# ---------------------------------------------------------------------------
# List orders
# ---------------------------------------------------------------------------


class TestListOrders:
    @pytest.mark.asyncio
    async def test_list_orders_by_status(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Can filter orders by status."""
        # Both maker orders fill immediately in paper mode (#255)
        params_a = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=5,
            yes_price=0.65,
            post_only=True,
        )
        await broker.create_order(params_a, orderbook)

        params_b = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=5,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(params_b, orderbook)

        resting = await broker.list_orders(status="resting")
        assert len(resting) == 0

        executed = await broker.list_orders(status="executed")
        assert len(executed) == 2


# ---------------------------------------------------------------------------
# SELL orders
# ---------------------------------------------------------------------------


class TestSellOrder:
    @pytest.mark.asyncio
    async def test_sell_credits_balance(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Buy 10 YES, sell 5 YES — balance increases by proceeds minus fees."""
        buy_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(buy_params, orderbook)
        balance_after_buy = await broker.get_balance()

        sell_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=5,
            yes_price=0.68,  # At YES best bid
            post_only=True,
        )
        await broker.create_order(sell_params, orderbook)
        balance_after_sell = await broker.get_balance()

        # SELL should credit balance (proceeds - fee > 0)
        assert balance_after_sell > balance_after_buy

        # Verify exact credit: 5 * $0.68 - maker_fee(5, 0.68)
        sell_notional = 5 * 0.68
        sell_fee = fee_for_order(5, 0.68, is_taker=False)
        expected_credit = sell_notional - sell_fee
        assert balance_after_sell == pytest.approx(balance_after_buy + expected_credit)

    @pytest.mark.asyncio
    async def test_sell_reduces_position_and_cost_basis(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Buy 10, sell 5 → position count=5, cost_basis halved."""
        buy_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(buy_params, orderbook)

        positions = await broker.get_positions()
        original_cost = positions[0].cost_basis

        sell_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=5,
            yes_price=0.68,
            post_only=True,
        )
        await broker.create_order(sell_params, orderbook)

        positions = await broker.get_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert pos.count == 5
        assert pos.cost_basis == pytest.approx(original_cost / 2)

    @pytest.mark.asyncio
    async def test_settlement_after_partial_sell(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Buy 10 at 70c, sell 5 at 68c, settle YES. Verify final balance."""
        buy_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(buy_params, orderbook)

        sell_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=5,
            yes_price=0.68,
            post_only=True,
        )
        await broker.create_order(sell_params, orderbook)
        balance_before_settle = await broker.get_balance()

        await broker.settle("TEST-MKT", "yes")
        balance_after_settle = await broker.get_balance()

        # 5 remaining contracts settle at $1 each
        assert balance_after_settle == pytest.approx(balance_before_settle + 5.0)

    @pytest.mark.asyncio
    async def test_sell_without_position_is_noop(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """SELL with no position: no crash, no ghost position, no balance change."""
        balance_before = await broker.get_balance()

        sell_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=5,
            yes_price=0.68,
            post_only=True,
        )
        order = await broker.create_order(sell_params, orderbook)

        # Order should be canceled
        assert order.status == "canceled"
        assert order.remaining_count == 5

        # No position should be created
        positions = await broker.get_positions()
        assert len(positions) == 0

        # Balance must be unchanged (no free money)
        balance_after = await broker.get_balance()
        assert balance_after == balance_before

    @pytest.mark.asyncio
    async def test_sell_wrong_side_is_rejected(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """SELL NO on a ticker with only a YES position is rejected."""
        buy_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(buy_params, orderbook)
        balance_after_buy = await broker.get_balance()

        sell_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.NO,
            count=5,
            no_price=0.30,
            post_only=True,
        )
        order = await broker.create_order(sell_params, orderbook)
        assert order.status == "canceled"

        # Balance unchanged
        balance_after = await broker.get_balance()
        assert balance_after == balance_after_buy

    @pytest.mark.asyncio
    async def test_sell_more_than_held_is_rejected(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """SELL more contracts than held position is rejected."""
        buy_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=5,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(buy_params, orderbook)
        balance_after_buy = await broker.get_balance()

        sell_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=10,  # More than the 5 held
            yes_price=0.68,
            post_only=True,
        )
        order = await broker.create_order(sell_params, orderbook)
        assert order.status == "canceled"

        balance_after = await broker.get_balance()
        assert balance_after == balance_after_buy


# ---------------------------------------------------------------------------
# Fill resting orders
# ---------------------------------------------------------------------------


class TestMakerImmediateFill:
    """Paper mode fills maker orders immediately at limit price (#255)."""

    @pytest.mark.asyncio
    async def test_non_marketable_buy_fills_immediately(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """BUY below ask fills immediately, creates position in same call."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,  # Below ask (0.70)
            post_only=True,
        )
        order = await broker.create_order(params, orderbook)
        assert order.status == "executed"
        assert order.remaining_count == 0

        positions = await broker.get_positions()
        assert len(positions) == 1
        assert positions[0].count == 10

    @pytest.mark.asyncio
    async def test_immediate_fill_balance_accounting(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Non-marketable maker fill debits notional + maker fees."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

        notional = 10 * 0.65
        fees = fee_for_order(10, 0.65, is_taker=False)
        balance = await broker.get_balance()
        assert balance == pytest.approx(10_000.00 - notional - fees)

    @pytest.mark.asyncio
    async def test_immediate_fill_uses_maker_fees(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Non-marketable maker fill uses maker fees, not taker fees."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

        fills = await broker.list_fills(ticker="TEST-MKT")
        assert len(fills) == 1
        assert fills[0].is_taker is False

    @pytest.mark.asyncio
    async def test_maker_bid_below_ask_fills_immediately(
        self, broker: PaperBroker,
    ) -> None:
        """Maker BUY at 73c fills immediately even with ask at 74c (#255)."""
        ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.68, quantity=200)],
            no_bids=[OrderbookLevel(price=0.26, quantity=100)],
        )
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.73,
            post_only=True,
        )
        order = await broker.create_order(params, ob)
        assert order.status == "executed"
        assert order.remaining_count == 0

        positions = await broker.get_positions()
        assert positions[0].count == 10

    @pytest.mark.asyncio
    async def test_sell_above_bid_fills_immediately(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """SELL above best bid fills immediately, reduces position."""
        # Create position first
        buy_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(buy_params, orderbook)

        # SELL at 0.72 — above best bid (0.68)
        sell_ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.68, quantity=200)],
            no_bids=[OrderbookLevel(price=0.30, quantity=500)],
        )
        sell_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=5,
            yes_price=0.72,
            post_only=True,
        )
        order = await broker.create_order(sell_params, sell_ob)
        assert order.status == "executed"
        assert order.remaining_count == 0

        # Position reduced from 10 to 5
        positions = await broker.get_positions()
        assert positions[0].count == 5

        # Balance credited (proceeds - fees)
        proceeds = 5 * 0.72
        fees = fee_for_order(5, 0.72, is_taker=False)
        balance = await broker.get_balance()
        buy_cost = 10 * 0.70 + fee_for_order(10, 0.70, is_taker=False)
        assert balance == pytest.approx(10_000.00 - buy_cost + proceeds - fees)

    @pytest.mark.asyncio
    async def test_fill_records_persisted(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Fill records are written to paper_fills at creation time."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,
            post_only=True,
        )
        order = await broker.create_order(params, orderbook)

        fills = await broker.list_fills(ticker="TEST-MKT")
        assert len(fills) == 1
        assert fills[0].order_id == order.order_id
        assert fills[0].count == 10
        assert fills[0].is_taker is False

    @pytest.mark.asyncio
    async def test_partial_depth_fills_remainder_at_limit(
        self, broker: PaperBroker,
    ) -> None:
        """Maker BUY with partial depth: fills at depth + synthetic remainder."""
        # Only 180 contracts on opposing side at YES ask 0.70
        limited_ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.68, quantity=200)],
            no_bids=[OrderbookLevel(price=0.30, quantity=180)],
        )
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=500,
            yes_price=0.70,
            post_only=True,
        )
        order = await broker.create_order(params, limited_ob)
        assert order.status == "executed"
        assert order.remaining_count == 0

        # Two fills: 180 from depth + 320 synthetic
        fills = await broker.list_fills(ticker="TEST-MKT")
        assert len(fills) == 2
        counts = sorted(f.count for f in fills)
        assert counts == [180, 320]
        assert all(not f.is_taker for f in fills)

        # Full position created
        positions = await broker.get_positions()
        assert positions[0].count == 500

        # Balance = starting - (500 * 0.70) - maker_fee(180, 0.70) - maker_fee(320, 0.70)
        notional = 500 * 0.70
        fees = fee_for_order(180, 0.70, is_taker=False) + fee_for_order(320, 0.70, is_taker=False)
        balance = await broker.get_balance()
        assert balance == pytest.approx(10_000.00 - notional - fees)

    @pytest.mark.asyncio
    async def test_no_side_non_marketable_fills_immediately(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """NO-side non-marketable maker order fills immediately at limit price."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.NO,
            count=10,
            no_price=0.25,  # Below NO ask (1 - 0.68 = 0.32)
            post_only=True,
        )
        order = await broker.create_order(params, orderbook)
        assert order.status == "executed"
        assert order.remaining_count == 0

        positions = await broker.get_positions()
        no_pos = [p for p in positions if p.side == "no"]
        assert len(no_pos) == 1
        assert no_pos[0].count == 10

        notional = 10 * 0.25
        fees = fee_for_order(10, 0.25, is_taker=False)
        balance = await broker.get_balance()
        assert balance == pytest.approx(10_000.00 - notional - fees)

    @pytest.mark.asyncio
    async def test_fill_resting_orders_is_noop(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """fill_resting_orders returns empty when no orders are resting."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

        filled = await broker.fill_resting_orders({"TEST-MKT": orderbook})
        assert len(filled) == 0


# ---------------------------------------------------------------------------
# Taker partial fills
# ---------------------------------------------------------------------------


class TestTakerPartialFill:
    @pytest.mark.asyncio
    async def test_taker_partial_fill_no_balance_reservation(
        self, broker: PaperBroker
    ) -> None:
        """Taker BUY 500 YES (only 180 available) — no reservation for unfilled."""
        limited_ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.68, quantity=200)],
            no_bids=[OrderbookLevel(price=0.30, quantity=180)],  # YES ask = 0.70
        )
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=500,
            yes_price=0.70,
            post_only=False,  # taker order
        )
        order = await broker.create_order(params, limited_ob)

        # Only 180 should fill
        assert order.remaining_count == 320

        # Balance should only be debited for 180 filled contracts (notional + fees)
        # NOT an additional reservation for 320 unfilled
        notional = 180 * 0.70
        fee = fee_for_order(180, 0.70, is_taker=True)
        expected_balance = 10_000.00 - notional - fee
        balance = await broker.get_balance()
        assert balance == pytest.approx(expected_balance)


# ---------------------------------------------------------------------------
# Negative balance guard
# ---------------------------------------------------------------------------


class TestNegativeBalanceGuard:
    @pytest.mark.asyncio
    async def test_buy_exceeding_balance_is_canceled(self, broker: PaperBroker) -> None:
        """BUY that would exceed available balance is rejected."""
        # Starting balance is $10,000. Try to buy 20,000 contracts at $0.70 = $14,000
        ob = Orderbook(
            ticker="EXPENSIVE",
            yes_bids=[],
            no_bids=[OrderbookLevel(price=0.30, quantity=20_000)],
        )
        params = CreateOrderParams(
            ticker="EXPENSIVE",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=20_000,
            yes_price=0.70,
            post_only=False,
        )
        order = await broker.create_order(params, ob)
        assert order.status == "canceled"

        # Balance unchanged
        balance = await broker.get_balance()
        assert balance == 10_000.00

    @pytest.mark.asyncio
    async def test_maker_order_exceeding_balance_is_canceled(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Maker BUY that would exceed available balance is rejected."""
        # #690: give the book opposing depth so the BALANCE guard is
        # what trips — an empty book now cancels for its own reason
        # and would silently stop pinning the balance check.
        ob = Orderbook(
            ticker="EXPENSIVE",
            yes_bids=[],
            no_bids=[OrderbookLevel(price=0.30, quantity=20_000)],
        )
        params = CreateOrderParams(
            ticker="EXPENSIVE",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=20_000,
            yes_price=0.70,
            post_only=True,
        )
        order = await broker.create_order(params, ob)
        assert order.status == "canceled"
        # Pin WHICH cancel fired (#690): the balance guard, not the
        # empty-book refusal.
        assert "insufficient balance" in order.reason

        balance = await broker.get_balance()
        assert balance == 10_000.00


class TestSettlementCloseTrade:
    """#653: settle() writes the settlement close trade and removes the
    positions mirror row so reconcile can't write a duplicate."""

    async def _buy(self, broker, orderbook, side, count=10) -> None:
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=side,
            count=count,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

    @pytest.mark.asyncio
    async def test_settle_writes_settlement_close_win(
        self, broker: PaperBroker, orderbook: Orderbook,
    ) -> None:
        from gimmes.store.queries import get_trades

        await self._buy(broker, orderbook, OrderSide.YES)
        await broker.settle("TEST-MKT", "yes")  # YES won

        trades = await get_trades(broker._db, ticker="TEST-MKT")
        closes = [t for t in trades if t["action"] == "close"]
        assert len(closes) == 1
        assert closes[0]["agent"] == "settlement"
        assert closes[0]["price"] == 1.0
        assert closes[0]["count"] == 10
        assert closes[0]["resolved_outcome"] == "yes"

    @pytest.mark.asyncio
    async def test_settle_writes_settlement_close_loss(
        self, broker: PaperBroker, orderbook: Orderbook,
    ) -> None:
        from gimmes.store.queries import get_trades

        await self._buy(broker, orderbook, OrderSide.YES)
        await broker.settle("TEST-MKT", "no")  # YES lost

        trades = await get_trades(broker._db, ticker="TEST-MKT")
        closes = [t for t in trades if t["action"] == "close"]
        assert len(closes) == 1
        assert closes[0]["price"] == 0.0
        assert closes[0]["resolved_outcome"] == "no"

    @pytest.mark.asyncio
    async def test_settle_deletes_positions_mirror_row(
        self, broker: PaperBroker, orderbook: Orderbook,
    ) -> None:
        from gimmes.models.portfolio import Position
        from gimmes.store.queries import get_positions, upsert_position

        await self._buy(broker, orderbook, OrderSide.YES)
        # Mirror the position into the main positions table the way a
        # cycle's sync would.
        await upsert_position(broker._db, Position(
            ticker="TEST-MKT", side="yes", count=10, avg_price=0.70,
        ))
        await broker.settle("TEST-MKT", "yes")

        remaining = {p.ticker for p in await get_positions(broker._db)}
        assert "TEST-MKT" not in remaining

    @pytest.mark.asyncio
    async def test_settle_nonexistent_writes_no_trade(
        self, broker: PaperBroker,
    ) -> None:
        from gimmes.store.queries import get_trades

        await broker.settle("KXT-GHOST", "yes")
        assert await get_trades(broker._db, ticker="KXT-GHOST") == []


class TestPartialSellThenSettleCloseRow:
    """#653 gap 4: settle() after a partial sell must write the
    settlement close for the RESIDUAL count only."""

    @pytest.mark.asyncio
    async def test_settlement_close_counts_residual_only(
        self, broker: PaperBroker, orderbook: Orderbook,
    ) -> None:
        from gimmes.store.queries import get_trades

        buy = CreateOrderParams(
            ticker="TEST-MKT", action=OrderAction.BUY,
            side=OrderSide.YES, count=10, yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(buy, orderbook)
        sell = CreateOrderParams(
            ticker="TEST-MKT", action=OrderAction.SELL,
            side=OrderSide.YES, count=5, yes_price=0.68,
            post_only=True,
        )
        await broker.create_order(sell, orderbook)
        await broker.settle("TEST-MKT", "yes")

        trades = await get_trades(broker._db, ticker="TEST-MKT")
        settlement = [
            t for t in trades if t.get("agent") == "settlement"
        ]
        assert len(settlement) == 1
        assert settlement[0]["count"] == 5
        assert settlement[0]["price"] == 1.0


class TestSettlementOverridesWrongOutcome:
    """#664 review: settlement is authoritative — a pre-existing WRONG
    resolved_outcome (bad log-outcome) must be corrected at settle
    time, or read-time repricing trusts the stale outcome."""

    @pytest.mark.asyncio
    async def test_settle_corrects_conflicting_outcome(
        self, broker: PaperBroker, orderbook: Orderbook,
    ) -> None:
        from gimmes.store.queries import get_trades

        params = CreateOrderParams(
            ticker="TEST-MKT", action=OrderAction.BUY,
            side=OrderSide.YES, count=10, yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(params, orderbook)
        # The paper broker itself writes no trades rows — seed the open
        # row an agent's decision logging would have written, carrying
        # a WRONG outcome from an earlier bad log-outcome (the KXUE
        # case). Without a real row here the wrong-outcome setup is a
        # no-op and the test is vacuous (mutation-proven in review).
        from gimmes.models.trade import TradeDecision
        from gimmes.store.queries import insert_trade

        await insert_trade(broker._db, TradeDecision(
            ticker="TEST-MKT", action=TradeDecision.Action.OPEN,
            side="yes", count=10, price=0.70,
        ))
        await broker._conn.execute(
            "UPDATE trades SET resolved_outcome = 'no'"
            " WHERE ticker = 'TEST-MKT'",
        )
        await broker._db.conn.commit()

        await broker.settle("TEST-MKT", "yes")  # market actually: yes

        trades = await get_trades(broker._db, ticker="TEST-MKT")
        assert all(
            t["resolved_outcome"] == "yes" for t in trades
        ), trades


class TestSettleLedgerClamp:
    """#663: a resting sell logs its close trade at placement without
    reducing the paper position, so settle() writing a FULL-count
    settlement row would double-count the exit in daily P&L (and the
    daily-loss trigger). The settlement row is clamped to the ledger
    residual; balance and paper_positions updates stay full-count
    because the broker's cash accounting is separate from the ledger.
    """

    async def _buy(
        self, broker: PaperBroker, orderbook: Orderbook, count: int = 10,
    ) -> None:
        params = CreateOrderParams(
            ticker="TEST-MKT", action=OrderAction.BUY,
            side=OrderSide.YES, count=count, yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

    async def _seed_ledger(
        self, broker: PaperBroker, *, opened: int = 0, closed: int = 0,
    ) -> None:
        from gimmes.models.trade import TradeDecision
        from gimmes.store.queries import insert_trade

        if opened:
            await insert_trade(broker._db, TradeDecision(
                ticker="TEST-MKT", action=TradeDecision.Action.OPEN,
                side="yes", count=opened, price=0.70,
            ))
        if closed:
            await insert_trade(broker._db, TradeDecision(
                ticker="TEST-MKT", action=TradeDecision.Action.CLOSE,
                side="yes", count=closed, price=0.90,
            ))

    @pytest.mark.asyncio
    async def test_settle_skips_trade_row_when_ledger_covered(
        self, broker: PaperBroker, orderbook: Orderbook,
    ) -> None:
        """Placement-time close row already covers the opens — settle
        writes NO second close, but the payout still lands in the
        balance and the outcome is still recorded."""
        from gimmes.store.queries import get_trades

        await self._buy(broker, orderbook)
        await self._seed_ledger(broker, opened=10, closed=10)
        balance_before = await broker.get_balance()

        await broker.settle("TEST-MKT", "yes")  # YES won → $1/contract

        trades = await get_trades(broker._db, ticker="TEST-MKT")
        assert [
            t for t in trades if t.get("agent") == "settlement"
        ] == []
        assert all(
            t["resolved_outcome"] == "yes" for t in trades
        ), trades
        assert await broker.get_balance() == pytest.approx(
            balance_before + 10 * 1.0,
        )

    @pytest.mark.asyncio
    async def test_settle_clamps_to_ledger_residual(
        self, broker: PaperBroker, orderbook: Orderbook,
    ) -> None:
        """A partial placement-time close: the settlement row covers
        only the contracts the ledger hasn't closed yet."""
        from gimmes.store.queries import get_trades

        await self._buy(broker, orderbook)
        await self._seed_ledger(broker, opened=10, closed=4)

        await broker.settle("TEST-MKT", "yes")

        trades = await get_trades(broker._db, ticker="TEST-MKT")
        settlement = [
            t for t in trades if t.get("agent") == "settlement"
        ]
        assert len(settlement) == 1
        assert settlement[0]["count"] == 6
        assert settlement[0]["price"] == 1.0

    @pytest.mark.asyncio
    async def test_settle_full_count_without_trade_history(
        self, broker: PaperBroker, orderbook: Orderbook,
    ) -> None:
        """opened == 0 (seeded/legacy position with no local trades)
        keeps the pre-#663 full-count settlement row — a zero ledger
        must not be mistaken for 'already covered'."""
        from gimmes.store.queries import get_trades

        await self._buy(broker, orderbook)  # broker writes no trades rows

        await broker.settle("TEST-MKT", "no")  # YES lost

        trades = await get_trades(broker._db, ticker="TEST-MKT")
        closes = [t for t in trades if t["action"] == "close"]
        assert len(closes) == 1
        assert closes[0]["agent"] == "settlement"
        assert closes[0]["count"] == 10
        assert closes[0]["price"] == 0.0

    @pytest.mark.asyncio
    async def test_settle_clamp_capped_at_broker_count(
        self, broker: PaperBroker, orderbook: Orderbook,
    ) -> None:
        """A ledger residual LARGER than the broker position (size-ups
        in trades, partially reduced paper position) must not inflate
        the settlement row past the contracts that actually settled."""
        from gimmes.store.queries import get_trades

        await self._buy(broker, orderbook)
        await self._seed_ledger(broker, opened=20, closed=0)

        await broker.settle("TEST-MKT", "yes")

        trades = await get_trades(broker._db, ticker="TEST-MKT")
        settlement = [
            t for t in trades if t.get("agent") == "settlement"
        ]
        assert len(settlement) == 1
        assert settlement[0]["count"] == 10  # broker count, not 20


class TestEmptyBookMakerRefusal:
    """#690: a maker order whose OPPOSING side is completely empty
    cancels instead of fabricating a fill at its own limit — there is
    no counterparty at any price. Non-marketable makers with a real
    book keep the #255 immediate fill."""

    @staticmethod
    def _empty_book(ticker: str = "KXDEAD") -> Orderbook:
        return Orderbook(ticker=ticker, yes_bids=[], no_bids=[])

    @pytest.mark.asyncio
    async def test_buy_yes_empty_book_cancels(
        self, broker: PaperBroker,
    ) -> None:
        params = CreateOrderParams(
            ticker="KXDEAD", action=OrderAction.BUY,
            side=OrderSide.YES, count=10, yes_price=0.70,
            post_only=True,
        )
        order = await broker.create_order(params, self._empty_book())
        assert order.status == "canceled"
        assert order.remaining_count == 10
        assert "no opposing liquidity" in order.reason
        assert await broker.get_balance() == 10_000.00
        assert await broker.get_positions() == []

    @pytest.mark.asyncio
    async def test_buy_no_empty_book_cancels(
        self, broker: PaperBroker,
    ) -> None:
        params = CreateOrderParams(
            ticker="KXDEAD", action=OrderAction.BUY,
            side=OrderSide.NO, count=10, no_price=0.40,
            post_only=True,
        )
        order = await broker.create_order(params, self._empty_book())
        assert order.status == "canceled"
        assert await broker.get_balance() == 10_000.00

    @pytest.mark.asyncio
    async def test_sell_yes_empty_book_cancels(
        self, broker: PaperBroker, orderbook: Orderbook,
    ) -> None:
        """Seed via a liquid book, then try to close on a dead one."""
        buy = CreateOrderParams(
            ticker="TEST-MKT", action=OrderAction.BUY,
            side=OrderSide.YES, count=10, yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(buy, orderbook)
        balance_after_buy = await broker.get_balance()

        sell = CreateOrderParams(
            ticker="TEST-MKT", action=OrderAction.SELL,
            side=OrderSide.YES, count=10, yes_price=0.68,
            post_only=True,
        )
        order = await broker.create_order(
            sell, self._empty_book("TEST-MKT"),
        )
        assert order.status == "canceled"
        assert "no opposing liquidity" in order.reason
        # Position intact, no proceeds credited.
        [pos] = await broker.get_positions()
        assert pos.count == 10
        assert await broker.get_balance() == balance_after_buy

    @pytest.mark.asyncio
    async def test_sell_no_empty_book_cancels(
        self, broker: PaperBroker,
    ) -> None:
        """Fourth direction: SELL NO against empty no_bids."""
        buy_book = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.68, quantity=200)],
            no_bids=[OrderbookLevel(price=0.30, quantity=500)],
        )
        buy = CreateOrderParams(
            ticker="TEST-MKT", action=OrderAction.BUY,
            side=OrderSide.NO, count=10, no_price=0.32,
            post_only=True,
        )
        await broker.create_order(buy, buy_book)

        sell = CreateOrderParams(
            ticker="TEST-MKT", action=OrderAction.SELL,
            side=OrderSide.NO, count=10, no_price=0.35,
            post_only=True,
        )
        order = await broker.create_order(
            sell, self._empty_book("TEST-MKT"),
        )
        assert order.status == "canceled"
        assert "no opposing liquidity" in order.reason

    @pytest.mark.asyncio
    async def test_canceled_order_row_kept_for_audit(
        self, broker: PaperBroker,
    ) -> None:
        params = CreateOrderParams(
            ticker="KXDEAD", action=OrderAction.BUY,
            side=OrderSide.YES, count=10, yes_price=0.70,
            post_only=True,
        )
        order = await broker.create_order(params, self._empty_book())
        cursor = await broker._conn.execute(
            "SELECT status FROM paper_orders WHERE order_id = ?",
            (order.order_id,),
        )
        row = await cursor.fetchone()
        assert row is not None and row["status"] == "canceled"

    @pytest.mark.asyncio
    async def test_resting_order_stays_resting_on_dead_book(
        self, broker: PaperBroker,
    ) -> None:
        """fill_resting_orders must not fabricate either — a legacy
        resting order against a dead book stays resting."""
        await broker._conn.execute(
            """INSERT INTO paper_orders
               (order_id, ticker, action, side, count, remaining_count,
                yes_price, no_price, status, post_only, created_at,
                updated_at)
               VALUES ('legacy-1', 'KXDEAD', 'buy', 'yes', 10, 10,
                       70, 0, 'resting', 1, datetime('now'),
                       datetime('now'))""",
        )
        await broker._db.conn.commit()

        filled = await broker.fill_resting_orders(
            {"KXDEAD": self._empty_book()},
        )
        assert filled == []
        cursor = await broker._conn.execute(
            "SELECT status FROM paper_orders WHERE order_id = 'legacy-1'",
        )
        assert (await cursor.fetchone())["status"] == "resting"


class TestCancelOrderAnnulment:
    """#690 (#684 item 4): canceling a never-filled resting order
    annuls its placement-time trade rows (action='skip') so the
    ledger stops seeing an exit that never traded."""

    async def _seed_resting_with_close_row(
        self, broker: PaperBroker, *, remaining: int = 10,
    ) -> None:
        from gimmes.models.trade import TradeDecision
        from gimmes.store.queries import insert_trade

        await broker._conn.execute(
            """INSERT INTO paper_orders
               (order_id, ticker, action, side, count, remaining_count,
                yes_price, no_price, status, post_only, created_at,
                updated_at)
               VALUES ('legacy-2', 'KXOLD', 'sell', 'yes', 10, ?,
                       68, 0, 'resting', 1, datetime('now'),
                       datetime('now'))""",
            (remaining,),
        )
        await broker._db.conn.commit()
        await insert_trade(broker._db, TradeDecision(
            ticker="KXOLD", action=TradeDecision.Action.CLOSE,
            side="yes", count=10, price=0.68,
            rationale="placement-time close",
            order_id="legacy-2",
        ))

    @pytest.mark.asyncio
    async def test_never_filled_cancel_annuls_trade_rows(
        self, broker: PaperBroker,
    ) -> None:
        from gimmes.store.queries import get_daily_pnl, get_trades

        await self._seed_resting_with_close_row(broker)
        await broker.cancel_order("legacy-2")

        rows = await get_trades(broker._db, ticker="KXOLD")
        assert len(rows) == 1
        assert rows[0]["action"] == "skip"
        assert rows[0]["reason"] == "order_canceled"
        assert "#690 annulment" in rows[0]["rationale"]
        # The phantom exit no longer reaches daily P&L.
        assert await get_daily_pnl(broker._db) == 0.0
        # #690 review: SELLs never reserved balance — canceling one
        # must not fabricate a refund.
        assert await broker.get_balance() == 10_000.00

    @pytest.mark.asyncio
    async def test_partially_filled_cancel_keeps_trade_rows(
        self, broker: PaperBroker, caplog,
    ) -> None:
        import logging

        from gimmes.store.queries import get_trades

        await self._seed_resting_with_close_row(broker, remaining=4)
        with caplog.at_level(logging.WARNING, logger="gimmes"):
            await broker.cancel_order("legacy-2")

        rows = await get_trades(broker._db, ticker="KXOLD")
        assert rows[0]["action"] == "close"  # kept
        assert "partially filled" in caplog.text
