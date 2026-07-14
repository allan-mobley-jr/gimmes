"""End-to-end pipeline tests exercising the full trade lifecycle."""

from __future__ import annotations

import time
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
from gimmes.models.trade import TradeDecision
from gimmes.paper.broker import PaperBroker
from gimmes.risk.validator import validate_trade
from gimmes.store.database import Database
from gimmes.store.queries import get_daily_pnl, insert_trade, sync_positions
from gimmes.strategy.fees import edge_after_fees, fee_for_order
from gimmes.strategy.kelly import apply_base_rate_floor, position_size
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


# ---------------------------------------------------------------------------
# #725: hourly-ladder lifecycle (KXBTCD paper experiment, parts A-C of #721)
# ---------------------------------------------------------------------------

HOURLY_TICKER = "KXBTCD-26JUN23H14-T119999.99"


def _hourly_config(series: list[str] | None = None) -> GimmesConfig:
    return GimmesConfig(
        mode=Mode.DRIVING_RANGE,
        strategy=StrategyConfig(
            side="no",
            gimme_threshold=75,
            min_true_probability=0.90,
            min_edge_after_fees=0.05,
        ),
        # hourly_* stay at defaults: floor 0.70, band 0.30-0.85
        sizing=SizingConfig(kelly_fraction=0.25, max_position_pct=0.05),
        risk=RiskConfig(bankroll_paper=10_000.0),
        scanner=ScannerConfig(
            min_volume=100, min_open_interest=50,
            hourly_series=series if series is not None else ["KXBTCD"],
        ),
        paper=PaperTradingConfig(starting_balance=10_000.0),
    )


def _hourly_market() -> Market:
    return Market(
        ticker=HOURLY_TICKER,
        event_ticker="KXBTCD-26JUN23H14",
        series_ticker="KXBTCD",
        title="BTC above $119,999.99 at 2pm EDT?",
        status=MarketStatus.ACTIVE,
        yes_bid=0.53,
        yes_ask=0.57,
        last_price=0.55,  # YES mid 0.55 -> NO effective 0.45
        volume=5000,
        volume_24h=1200,
        open_interest=800,
        close_time=datetime.now(UTC) + timedelta(minutes=29),
        rules_primary="Resolves YES if BTC price is above the strike at the hour close.",
    )


def _ob_hourly() -> Orderbook:
    # Taker liquidity for BUY NO: implied NO ask = 1 - yes_bid = 0.47
    return Orderbook(
        ticker=HOURLY_TICKER,
        yes_bids=[OrderbookLevel(price=0.53, quantity=2000)],
        no_bids=[OrderbookLevel(price=0.43, quantity=2000)],
    )


def _ob_hourly_no_counterparty() -> Orderbook:
    # Empty opposing side for a BUY NO — the #690 honest no-fill book
    return Orderbook(
        ticker=HOURLY_TICKER,
        yes_bids=[],
        no_bids=[OrderbookLevel(price=0.43, quantity=2000)],
    )


@pytest.fixture
async def hourly_db_broker(
    tmp_path: Path,
) -> AsyncIterator[tuple[Database, PaperBroker]]:
    cfg = _hourly_config()
    db = Database(tmp_path / "hourly.db")
    await db.connect()
    broker = PaperBroker(db, cfg.paper)
    await broker.initialize()
    yield db, broker
    await db.close()


async def _enter_hourly_position(
    db: Database, broker: PaperBroker,
) -> int:
    """Run the mechanical entry flow the live hourly cycle drives:
    filter -> score -> base-rate floor -> size (taker) -> validate
    (hourly floor) -> log open trade -> taker order. Returns count."""
    cfg = _hourly_config()
    market = _hourly_market()

    # Scanner admission: min-days bypass + hourly band (NO 0.45 in
    # 0.30-0.85; the flat 0.55 floor would reject it)
    passed = filter_markets([market], cfg)
    assert [m.ticker for m in passed] == [HOURLY_TICKER]

    # quick_score under-scores hourly NO honestly (~55 < threshold 75):
    # the price-position component is keyed to the classic 0.55-0.85
    # band. Scanner admission is the assertion; the hourly-aware time
    # branch lives in full_score (Caddie's rubric), not quick_score.
    assert quick_score(market, cfg) > 0

    # The KXBTCD base-rate floor promotes a low NO-side estimate to
    # exactly the hourly gate (floor==gate by design, #722)
    floored = apply_base_rate_floor(0.60, market.ticker, side="no")
    assert floored == 0.70

    # Production auto-sizing prices at the side-effective MID and
    # passes the same floored prob to both position_size and
    # validate_trade (cli.py order command); the ORDER then crosses at
    # the NO ask
    eff_mid = 0.45  # effective_price(0.55, "no")
    no_price = 0.47  # the implied NO ask — takers pay the touch
    contracts = position_size(
        10_000, eff_mid, floored,
        is_taker=True, fraction=0.25, max_position_pct=0.05,
    )
    assert contracts > 0

    result = validate_trade(
        market, contracts * eff_mid, floored, 10_000,
        0.0, 0, [], cfg, is_taker=True,
    )
    assert result.approved
    assert any("hourly floor" in c for c in result.checks)

    # The open row the live Closer's order command writes — settle()
    # clamps the settlement close to the ledger residual (#663), so the
    # ledger must show the real opened count
    await insert_trade(db, TradeDecision(
        ticker=HOURLY_TICKER, action=TradeDecision.Action.OPEN,
        side="no", count=contracts, price=no_price,
        model_probability=0.72, agent="closer",
    ))

    # post_only=False is what `gimmes order --taker` wires (#722)
    params = CreateOrderParams(
        ticker=HOURLY_TICKER, action=OrderAction.BUY,
        side=OrderSide.NO, count=contracts, no_price=no_price,
        post_only=False,
    )
    order = await broker.create_order(params, _ob_hourly())
    assert order.status == "executed"
    fills = await broker.list_fills(HOURLY_TICKER)
    assert fills and all(f.is_taker for f in fills)
    return contracts


async def _settlement_closes(db: Database) -> list:
    """All close rows for the hourly ticker, as written by settle()."""
    cursor = await db.conn.execute(
        "SELECT agent, price, count FROM trades"
        " WHERE ticker = ? AND action = 'close'",
        (HOURLY_TICKER,),
    )
    return await cursor.fetchall()


class TestHourlyNoLifecycle:
    @pytest.mark.asyncio
    async def test_win_path(
        self, hourly_db_broker: tuple[Database, PaperBroker],
    ) -> None:
        db, broker = hourly_db_broker
        contracts = await _enter_hourly_position(db, broker)

        # Top-of-hour settlement: NO wins
        await broker.settle(HOURLY_TICKER, "no")

        positions = await broker.get_positions()
        assert positions == []

        # Exact payout math: entry cost + taker fee out, $1/contract in
        entry_fee = fee_for_order(contracts, 0.47, is_taker=True)
        assert await broker.get_balance() == pytest.approx(
            10_000 - contracts * 0.47 - entry_fee + contracts * 1.0,
        )

        # Next-cycle reconcile is a no-op: the settlement close already
        # exists, sync must not duplicate it (#628)
        await sync_positions(db, await broker.get_positions())

        # #622 semantics: the settlement close is a single
        # agent='settlement' row at price 1.0 and COUNTS toward daily P&L
        closes = await _settlement_closes(db)
        assert len(closes) == 1
        settlement = closes[0]
        assert settlement["agent"] == "settlement"
        assert settlement["price"] == 1.0
        assert settlement["count"] == contracts
        assert await get_daily_pnl(db) == pytest.approx(
            (1.0 - 0.47) * contracts,
        )

    @pytest.mark.asyncio
    async def test_loss_path(
        self, hourly_db_broker: tuple[Database, PaperBroker],
    ) -> None:
        db, broker = hourly_db_broker
        contracts = await _enter_hourly_position(db, broker)

        # BTC closes above the strike: NO loses
        await broker.settle(HOURLY_TICKER, "yes")

        assert await broker.get_positions() == []
        entry_fee = fee_for_order(contracts, 0.47, is_taker=True)
        assert await broker.get_balance() == pytest.approx(
            10_000 - contracts * 0.47 - entry_fee,
        )

        closes = await _settlement_closes(db)
        assert len(closes) == 1
        settlement = closes[0]
        assert settlement["agent"] == "settlement"
        assert settlement["price"] == 0.0
        # A real realized loss the daily-loss trigger must see
        assert await get_daily_pnl(db) == pytest.approx(
            (0.0 - 0.47) * contracts,
        )

    def test_hourly_gates_are_load_bearing(self) -> None:
        # Negative control: with hourly_series empty, the relaxed gates
        # vanish — each proven in isolation
        cfg = _hourly_config(series=[])

        # (a) min-days floor: a 29-minute market whose NO price (0.60)
        # sits INSIDE the flat band, so the days check is the gate that
        # actually rejects it
        in_band = _hourly_market().model_copy(update={
            "yes_bid": 0.38, "yes_ask": 0.42, "last_price": 0.40,
        })
        assert filter_markets([in_band], cfg) == []

        # (b) flat price band: the standard fixture's NO 0.45 falls
        # below the flat 0.55 floor (the hourly band admitted it)
        assert filter_markets([_hourly_market()], cfg) == []

        # (c) the global 0.90 probability floor rejects 0.72
        result = validate_trade(
            _hourly_market(), 500.0, 0.72, 10_000,
            0.0, 0, [], cfg, is_taker=True,
        )
        assert not result.approved
        assert any("90% minimum" in f for f in result.failures)


class TestHourlyMakerHonesty:
    @pytest.mark.asyncio
    async def test_post_only_no_counterparty_cancels(
        self, hourly_db_broker: tuple[Database, PaperBroker],
    ) -> None:
        # The #690 honest no-fill that the --taker design encodes: a
        # maker order with no opposing liquidity cancels, deploys
        # nothing, and leaves no position
        _, broker = hourly_db_broker
        params = CreateOrderParams(
            ticker=HOURLY_TICKER, action=OrderAction.BUY,
            side=OrderSide.NO, count=100, no_price=0.47,
            post_only=True,
        )
        order = await broker.create_order(
            params, _ob_hourly_no_counterparty(),
        )
        assert order.status == "canceled"
        assert "no opposing liquidity" in (order.reason or "")
        assert await broker.get_positions() == []
        assert await broker.get_balance() == 10_000

    @pytest.mark.asyncio
    async def test_post_only_liquid_book_fills_at_limit_as_maker(
        self, hourly_db_broker: tuple[Database, PaperBroker],
    ) -> None:
        # Paper-mode maker fills are OPTIMISTIC (#255): the remainder
        # fills at your limit without a counterparty actually crossing.
        # This is exactly why the hourly experiment runs the taker
        # lane — the +466% maker backtest twin cannot be honestly
        # simulated on paper.
        _, broker = hourly_db_broker
        params = CreateOrderParams(
            ticker=HOURLY_TICKER, action=OrderAction.BUY,
            side=OrderSide.NO, count=100, no_price=0.45,
            post_only=True,
        )
        order = await broker.create_order(params, _ob_hourly())
        assert order.status == "executed"
        fills = await broker.list_fills(HOURLY_TICKER)
        assert fills and all(not f.is_taker for f in fills)


class TestHourlyTimingBudget:
    @pytest.mark.asyncio
    async def test_mechanical_path_fits_clamp(
        self, hourly_db_broker: tuple[Database, PaperBroker],
    ) -> None:
        """#725 timing budget: the mechanical scan->order->settle path
        must consume a negligible slice of the hourly window clamp.

        The loop clamps every hourly cycle to the window remainder —
        at the default lead of 29 minutes that is at most
        hourly_lead_minutes * 60 = 1740 seconds for the whole
        Scout -> Caddie -> Closer pass. A unit test cannot measure the
        live LLM agents' wall-clock; what it CAN pin is that the
        CLI/broker machinery is nowhere near the budget, so a blown
        clamp in production is LLM wall-clock by elimination — and
        scanner.hourly_lead_minutes is the tuning knob (see the README
        recipe for log-based live measurement).
        """
        db, broker = hourly_db_broker
        clamp = _hourly_config().scanner.hourly_lead_minutes * 60
        assert clamp == 1740

        start = time.perf_counter()
        await _enter_hourly_position(db, broker)
        await broker.settle(HOURLY_TICKER, "no")
        elapsed = time.perf_counter() - start

        # Generous CI margin; the real number is milliseconds. <2% of
        # the clamp means the mechanical path can never be the reason
        # an hourly cycle times out. min() keeps the budget meaningful
        # if the default lead ever shrinks.
        assert elapsed < min(30.0, clamp * 0.02)
