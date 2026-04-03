"""End-to-end pipeline tests exercising the full trade lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gimmes.config import (
    GimmesConfig,
    Mode,
    PaperTradingConfig,
    RiskConfig,
    ScannerConfig,
    SizingConfig,
    StrategyConfig,
)
from gimmes.models.market import Market, MarketStatus, Orderbook, OrderbookLevel
from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide
from gimmes.paper.broker import PaperBroker
from gimmes.risk.validator import validate_trade
from gimmes.store.database import Database
from gimmes.strategy.fees import edge_after_fees
from gimmes.strategy.kelly import position_size
from gimmes.strategy.scanner import effective_price, filter_markets
from gimmes.strategy.scorer import quick_score

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(side: str = "yes") -> GimmesConfig:
    return GimmesConfig(
        mode=Mode.DRIVING_RANGE,
        strategy=StrategyConfig(
            side=side,
            min_market_price=0.55,
            max_market_price=0.85,
            gimme_threshold=75,
            min_true_probability=0.90,
            min_edge_after_fees=0.05,
        ),
        sizing=SizingConfig(kelly_fraction=0.25, max_position_pct=0.05),
        risk=RiskConfig(
            bankroll_paper=10_000.0,
            position_stop_loss_pct=0.15,
            position_take_profit_pct=0.80,
        ),
        scanner=ScannerConfig(min_volume=100, min_open_interest=50),
        paper=PaperTradingConfig(starting_balance=10_000.0),
    )


def _market_yes() -> Market:
    return Market(
        ticker="PIPE-YES-T70",
        event_ticker="PIPE-YES",
        series_ticker="KXTEST",
        title="Pipeline YES test",
        status=MarketStatus.ACTIVE,
        yes_bid=0.68,
        yes_ask=0.72,
        last_price=0.70,
        volume=5000,
        volume_24h=1200,
        open_interest=800,
        close_time=datetime.now(UTC) + timedelta(days=7),
        rules_primary="Resolves YES if X happens.",
    )


def _market_no() -> Market:
    return Market(
        ticker="PIPE-NO-T25",
        event_ticker="PIPE-NO",
        series_ticker="KXTEST",
        title="Pipeline NO test",
        status=MarketStatus.ACTIVE,
        yes_bid=0.23,
        yes_ask=0.27,
        last_price=0.25,
        volume=5000,
        volume_24h=1200,
        open_interest=800,
        close_time=datetime.now(UTC) + timedelta(days=7),
        rules_primary="Resolves NO if X does not happen.",
    )


def _ob_yes() -> Orderbook:
    return Orderbook(
        ticker="PIPE-YES-T70",
        yes_bids=[OrderbookLevel(price=0.68, quantity=500)],
        no_bids=[OrderbookLevel(price=0.30, quantity=500)],
    )


def _ob_no() -> Orderbook:
    return Orderbook(
        ticker="PIPE-NO-T25",
        yes_bids=[OrderbookLevel(price=0.25, quantity=500)],
        no_bids=[OrderbookLevel(price=0.73, quantity=500)],
    )


@pytest.fixture
async def broker_yes(tmp_path: Path) -> AsyncIterator[PaperBroker]:
    cfg = _config("yes")
    db = Database(tmp_path / "yes.db")
    await db.connect()
    broker = PaperBroker(db, cfg.paper)
    await broker.initialize()
    yield broker
    await db.close()


@pytest.fixture
async def broker_no(tmp_path: Path) -> AsyncIterator[PaperBroker]:
    cfg = _config("no")
    db = Database(tmp_path / "no.db")
    await db.connect()
    broker = PaperBroker(db, cfg.paper)
    await broker.initialize()
    yield broker
    await db.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBuyYesLifecycle:
    @pytest.mark.asyncio
    async def test_full_pipeline(self, broker_yes: PaperBroker) -> None:
        cfg = _config("yes")
        market = _market_yes()
        ob = _ob_yes()

        # Scan + filter
        passed = filter_markets([market], cfg)
        assert len(passed) == 1

        # Score
        score = quick_score(market, cfg)
        assert score > 0

        # Size
        contracts = position_size(
            10_000, 0.70, 0.95,
            fraction=0.25, max_position_pct=0.05,
        )
        assert contracts > 0

        # Validate
        trade_dollars = contracts * 0.70
        result = validate_trade(
            market, trade_dollars, 0.95, 10_000,
            0.0, 0, [], cfg,
        )
        assert result.approved

        # Order
        params = CreateOrderParams(
            ticker=market.ticker, action=OrderAction.BUY,
            side=OrderSide.YES, count=contracts, yes_price=0.70,
        )
        order = await broker_yes.create_order(params, ob)
        assert order.status == "executed"

        # Settle YES = win
        await broker_yes.settle(market.ticker, "yes")
        balance = await broker_yes.get_balance()
        assert balance > 10_000


class TestBuyNoLifecycle:
    @pytest.mark.asyncio
    async def test_full_pipeline(self, broker_no: PaperBroker) -> None:
        cfg = _config("no")
        market = _market_no()
        ob = _ob_no()

        # Filter (NO price 0.75 in [0.55, 0.85])
        passed = filter_markets([market], cfg)
        assert len(passed) == 1

        # Score from NO perspective
        score = quick_score(market, cfg)
        assert score > 0

        # Size using NO price
        no_price = effective_price(0.25, "no")  # 0.75
        contracts = position_size(
            10_000, no_price, 0.95,
            fraction=0.25, max_position_pct=0.05,
        )
        assert contracts > 0

        # Validate
        trade_dollars = contracts * no_price
        result = validate_trade(
            market, trade_dollars, 0.95, 10_000,
            0.0, 0, [], cfg,
        )
        assert result.approved

        # Order — BUY NO
        params = CreateOrderParams(
            ticker=market.ticker, action=OrderAction.BUY,
            side=OrderSide.NO, count=contracts, no_price=no_price,
        )
        order = await broker_no.create_order(params, ob)
        assert order.status == "executed"

        # Settle NO = win
        await broker_no.settle(market.ticker, "no")
        balance = await broker_no.get_balance()
        assert balance > 10_000


class TestSideFlipEdgeConsistency:
    def test_edges_are_complementary(self) -> None:
        yes_price = 0.30
        true_prob_yes = 0.25  # We think YES is unlikely

        # YES side: buy YES at 0.30, prob 0.25 → negative edge
        eff_yes = effective_price(yes_price, "yes")
        edge_yes = edge_after_fees(eff_yes, true_prob_yes)
        assert edge_yes < 0

        # NO side: NO price 0.70, NO prob 0.75 → positive edge
        eff_no = effective_price(yes_price, "no")
        true_prob_no = 1 - true_prob_yes
        edge_no = edge_after_fees(eff_no, true_prob_no)
        assert edge_no > 0


class TestStopLossTrigger:
    @pytest.mark.asyncio
    async def test_unrealized_loss_exceeds_threshold(
        self, broker_yes: PaperBroker,
    ) -> None:
        cfg = _config("yes")
        market = _market_yes()
        ob = _ob_yes()

        params = CreateOrderParams(
            ticker=market.ticker, action=OrderAction.BUY,
            side=OrderSide.YES, count=100, yes_price=0.70,
        )
        await broker_yes.create_order(params, ob)

        # Price drops to 0.55
        await broker_yes.mark_to_market(market.ticker, 0.55)

        positions = await broker_yes.get_positions()
        pos = positions[0]
        loss_pct = abs(pos.unrealized_pnl) / pos.cost_basis
        assert loss_pct > cfg.risk.position_stop_loss_pct


class TestTakeProfitTrigger:
    @pytest.mark.asyncio
    async def test_unrealized_gain_exceeds_threshold(
        self, broker_yes: PaperBroker,
    ) -> None:
        cfg = _config("yes")
        market = _market_yes()
        ob = _ob_yes()

        params = CreateOrderParams(
            ticker=market.ticker, action=OrderAction.BUY,
            side=OrderSide.YES, count=100, yes_price=0.70,
        )
        await broker_yes.create_order(params, ob)

        # Price rises to 0.95
        await broker_yes.mark_to_market(market.ticker, 0.95)

        positions = await broker_yes.get_positions()
        pos = positions[0]
        max_profit = pos.count * (1.0 - pos.avg_price)
        gain_ratio = pos.unrealized_pnl / max_profit if max_profit > 0 else 0
        assert gain_ratio > cfg.risk.position_take_profit_pct


class TestSettlement:
    @pytest.mark.asyncio
    async def test_yes_position_settles_yes(
        self, broker_yes: PaperBroker,
    ) -> None:
        params = CreateOrderParams(
            ticker="SETTLE-YES", action=OrderAction.BUY,
            side=OrderSide.YES, count=50, yes_price=0.70,
        )
        await broker_yes.create_order(params, _ob_yes())
        await broker_yes.settle("SETTLE-YES", "yes")

        positions = await broker_yes.get_positions()
        assert len(positions) == 0  # Settled — no open position
        balance = await broker_yes.get_balance()
        assert balance > 10_000  # Profitable: payout $50, cost ~$35 + fees

    @pytest.mark.asyncio
    async def test_no_position_settles_no(
        self, broker_no: PaperBroker,
    ) -> None:
        params = CreateOrderParams(
            ticker="SETTLE-NO", action=OrderAction.BUY,
            side=OrderSide.NO, count=50, no_price=0.75,
        )
        await broker_no.create_order(params, _ob_no())
        await broker_no.settle("SETTLE-NO", "no")

        positions = await broker_no.get_positions()
        assert len(positions) == 0
        balance = await broker_no.get_balance()
        assert balance > 10_000
