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
    async def test_resting_order_reserves_balance(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Order that doesn't fill reserves balance for resting portion."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,  # Below ask — rests
            post_only=True,
        )
        order = await broker.create_order(params, orderbook)
        assert order.status == "resting"
        assert order.remaining_count == 10

        # Balance reduced by reserved amount (10 * $0.65 = $6.50)
        balance = await broker.get_balance()
        assert balance == pytest.approx(10_000.00 - 6.50)

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
    async def test_cancel_resting_refunds_balance(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Canceling a resting order refunds the reserved balance."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,  # Rests
            post_only=True,
        )
        order = await broker.create_order(params, orderbook)
        assert order.status == "resting"

        balance_before = await broker.get_balance()
        await broker.cancel_order(order.order_id)
        balance_after = await broker.get_balance()

        # Should refund 10 * $0.65 = $6.50
        assert balance_after == pytest.approx(balance_before + 6.50)

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
        # Create a resting order
        resting_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=5,
            yes_price=0.65,
            post_only=True,
        )
        await broker.create_order(resting_params, orderbook)

        # Create a filled order
        filled_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=5,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(filled_params, orderbook)

        resting = await broker.list_orders(status="resting")
        assert len(resting) == 1
        assert resting[0].status == "resting"

        executed = await broker.list_orders(status="executed")
        assert len(executed) == 1
        assert executed[0].status == "executed"


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


class TestFillRestingOrders:
    @pytest.mark.asyncio
    async def test_fill_resting_buy_when_marketable(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Resting BUY becomes marketable → fills, creates position."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,  # Below ask (0.70) — rests
            post_only=True,
        )
        order = await broker.create_order(params, orderbook)
        assert order.status == "resting"

        # New orderbook: ask has dropped to 0.65 (NO bid = 0.35)
        new_ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.63, quantity=200)],
            no_bids=[OrderbookLevel(price=0.35, quantity=100)],  # YES ask = 0.65
        )
        filled = await broker.fill_resting_orders({"TEST-MKT": new_ob})

        assert len(filled) == 1
        assert filled[0].status == "executed"
        assert filled[0].remaining_count == 0

        positions = await broker.get_positions()
        assert len(positions) == 1
        assert positions[0].count == 10

    @pytest.mark.asyncio
    async def test_fill_resting_buy_balance_accounting(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Filling a resting BUY only debits fees (reservation covers notional)."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,
            post_only=True,
        )
        await broker.create_order(params, orderbook)
        balance_after_rest = await broker.get_balance()
        # Reservation: 10 * 0.65 = $6.50, so balance = $9,993.50

        new_ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.63, quantity=200)],
            no_bids=[OrderbookLevel(price=0.35, quantity=100)],
        )
        await broker.fill_resting_orders({"TEST-MKT": new_ob})

        balance_after_fill = await broker.get_balance()
        # Only fees deducted from the reserved balance
        fees = fee_for_order(10, 0.65, is_taker=False)
        assert balance_after_fill == pytest.approx(balance_after_rest - fees)

    @pytest.mark.asyncio
    async def test_resting_fills_at_limit_price(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Resting order fills at limit price even if ask hasn't moved (#215)."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,
            post_only=True,
        )
        order = await broker.create_order(params, orderbook)
        assert order.status == "resting"

        # Same orderbook — ask still at $0.70, above limit $0.65
        filled = await broker.fill_resting_orders({"TEST-MKT": orderbook})

        assert len(filled) == 1
        assert filled[0].status == "executed"
        assert filled[0].remaining_count == 0

        positions = await broker.get_positions()
        assert len(positions) == 1
        assert positions[0].count == 10

    @pytest.mark.asyncio
    async def test_resting_fill_uses_maker_fees(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Resting order fill uses maker fees, not taker fees."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,
            post_only=True,
        )
        await broker.create_order(params, orderbook)
        balance_after_rest = await broker.get_balance()

        await broker.fill_resting_orders({"TEST-MKT": orderbook})

        balance_after_fill = await broker.get_balance()
        fees = fee_for_order(10, 0.65, is_taker=False)
        assert balance_after_fill == pytest.approx(balance_after_rest - fees)

    @pytest.mark.asyncio
    async def test_resting_maker_bid_below_ask_fills(
        self, broker: PaperBroker,
    ) -> None:
        """Maker BUY at 73c fills on reconcile even with ask at 74c (#215)."""
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
        assert order.status == "resting"

        filled = await broker.fill_resting_orders({"TEST-MKT": ob})
        assert len(filled) == 1
        assert filled[0].status == "executed"

        positions = await broker.get_positions()
        assert positions[0].count == 10

    @pytest.mark.asyncio
    async def test_fill_resting_skips_missing_orderbook(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Resting order for a ticker not in orderbooks dict is skipped."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,
            post_only=True,
        )
        await broker.create_order(params, orderbook)

        filled = await broker.fill_resting_orders({})  # Empty dict
        assert len(filled) == 0

    @pytest.mark.asyncio
    async def test_fill_records_persisted(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Fill records are written to paper_fills with correct order_id."""
        params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.65,
            post_only=True,
        )
        order = await broker.create_order(params, orderbook)
        assert order.status == "resting"

        new_ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.63, quantity=200)],
            no_bids=[OrderbookLevel(price=0.35, quantity=100)],
        )
        await broker.fill_resting_orders({"TEST-MKT": new_ob})

        fills = await broker.list_fills(ticker="TEST-MKT")
        assert len(fills) == 1
        assert fills[0].order_id == order.order_id
        assert fills[0].count == 10
        assert fills[0].is_taker is False

    @pytest.mark.asyncio
    async def test_fill_resting_sell_order(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Resting SELL becomes marketable → fills, reduces position."""
        # First create a position by buying
        buy_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(buy_params, orderbook)

        # Place a SELL at 0.72 — above best bid (0.68), so it rests
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
        assert order.status == "resting"
        balance_after_rest = await broker.get_balance()

        # New orderbook: bid rises to 0.73
        new_ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.73, quantity=200)],
            no_bids=[OrderbookLevel(price=0.27, quantity=500)],
        )
        filled = await broker.fill_resting_orders({"TEST-MKT": new_ob})

        assert len(filled) == 1
        assert filled[0].status == "executed"

        # Position reduced from 10 to 5
        positions = await broker.get_positions()
        assert positions[0].count == 5

        # Balance credited (proceeds - fees)
        balance_after_fill = await broker.get_balance()
        proceeds = 5 * 0.72
        fees = fee_for_order(5, 0.72, is_taker=False)
        assert balance_after_fill == pytest.approx(balance_after_rest + proceeds - fees)


    @pytest.mark.asyncio
    async def test_fill_resting_sell_no_position_skipped(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Resting SELL with no backing position is skipped (no free money)."""
        # Buy 10 — creates position
        buy_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(buy_params, orderbook)

        # Place resting SELL for 5 (above bid, rests while position exists)
        sell_ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.68, quantity=200)],
            no_bids=[OrderbookLevel(price=0.30, quantity=500)],
        )
        rest_sell = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=5,
            yes_price=0.72,
            post_only=True,
        )
        order = await broker.create_order(rest_sell, sell_ob)
        assert order.status == "resting"

        # Now sell ALL 10 contracts, zeroing the position
        sell_all = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=10,
            yes_price=0.68,
            post_only=True,
        )
        await broker.create_order(sell_all, orderbook)
        positions = await broker.get_positions()
        assert len(positions) == 0  # Position fully liquidated

        balance_before = await broker.get_balance()

        # Market moves — bid rises above sell price
        new_ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.73, quantity=200)],
            no_bids=[OrderbookLevel(price=0.27, quantity=500)],
        )
        filled = await broker.fill_resting_orders({"TEST-MKT": new_ob})

        assert len(filled) == 0  # Skipped — no backing position
        assert await broker.get_balance() == balance_before

    @pytest.mark.asyncio
    async def test_fill_resting_sell_capped_to_held(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Resting SELL capped to actually held contracts."""
        # Buy 10
        buy_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )
        await broker.create_order(buy_params, orderbook)

        # Place resting SELL for 10 (above bid, rests)
        sell_ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.68, quantity=200)],
            no_bids=[OrderbookLevel(price=0.30, quantity=500)],
        )
        sell_params = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=10,
            yes_price=0.72,
            post_only=True,
        )
        await broker.create_order(sell_params, sell_ob)

        # Sell 7 through another order, leaving only 3 held
        sell_some = CreateOrderParams(
            ticker="TEST-MKT",
            action=OrderAction.SELL,
            side=OrderSide.YES,
            count=7,
            yes_price=0.68,
            post_only=True,
        )
        await broker.create_order(sell_some, orderbook)

        positions = await broker.get_positions()
        assert positions[0].count == 3

        # Now fill the resting SELL — should cap at 3
        new_ob = Orderbook(
            ticker="TEST-MKT",
            yes_bids=[OrderbookLevel(price=0.73, quantity=200)],
            no_bids=[OrderbookLevel(price=0.27, quantity=500)],
        )
        filled = await broker.fill_resting_orders({"TEST-MKT": new_ob})

        assert len(filled) == 1
        # Only 3 filled (capped), 7 still resting
        assert filled[0].remaining_count == 7

        positions = await broker.get_positions()
        assert len(positions) == 0  # All 3 remaining sold


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
    async def test_resting_order_exceeding_balance_is_canceled(
        self, broker: PaperBroker, orderbook: Orderbook
    ) -> None:
        """Resting BUY that would reserve more than available balance is rejected."""
        ob = Orderbook(
            ticker="EXPENSIVE",
            yes_bids=[],
            no_bids=[],  # Nothing to fill, will rest
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

        balance = await broker.get_balance()
        assert balance == 10_000.00
