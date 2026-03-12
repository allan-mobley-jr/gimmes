"""Unit tests for Pydantic models."""

from gimmes.models.gimme import GimmeScore
from gimmes.models.market import Market, MarketStatus, Orderbook
from gimmes.models.order import CreateOrderParams, Order, OrderAction, OrderSide
from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision, TradeOutcome


class TestMarket:
    def test_midpoint(self, sample_market: Market) -> None:
        assert sample_market.midpoint == 0.70

    def test_spread(self, sample_market: Market) -> None:
        assert abs(sample_market.spread - 0.04) < 1e-10

    def test_midpoint_fallback(self) -> None:
        m = Market(ticker="X", last_price=0.50)
        assert m.midpoint == 0.50

    def test_market_status_enum(self) -> None:
        m = Market(ticker="X", status=MarketStatus.SETTLED)
        assert m.status == MarketStatus.SETTLED


class TestOrderbook:
    def test_best_yes_bid(self, sample_orderbook: Orderbook) -> None:
        assert sample_orderbook.best_yes_bid == 0.68

    def test_best_yes_ask(self, sample_orderbook: Orderbook) -> None:
        # YES ask = 1 - best NO bid = 1 - 0.30 = 0.70
        assert sample_orderbook.best_yes_ask == 0.70

    def test_depth_at_price(self, sample_orderbook: Orderbook) -> None:
        # All levels at or above 0.65
        depth = sample_orderbook.depth_at_price(0.65, "yes")
        assert depth == 650  # 200 + 150 + 300

    def test_empty_orderbook(self) -> None:
        ob = Orderbook(ticker="X")
        assert ob.best_yes_bid is None
        assert ob.best_yes_ask is None


class TestGimmeScore:
    def test_qualifies(self) -> None:
        score = GimmeScore(total=80)
        assert score.qualifies is True

    def test_does_not_qualify(self) -> None:
        score = GimmeScore(total=50)
        assert score.qualifies is False


class TestOrder:
    def test_create_order_params(self) -> None:
        params = CreateOrderParams(
            ticker="KXTEST",
            count=10,
            yes_price=70,
        )
        assert params.price_cents == 70

    def test_order_is_open(self) -> None:
        o = Order(order_id="abc", ticker="X", action=OrderAction.BUY,
                  side=OrderSide.YES, status="resting")
        assert o.is_open is True

    def test_order_is_not_open(self) -> None:
        o = Order(order_id="abc", ticker="X", action=OrderAction.BUY,
                  side=OrderSide.YES, status="canceled")
        assert o.is_open is False


class TestPosition:
    def test_total_pnl(self) -> None:
        pos = Position(ticker="X", unrealized_pnl=10.0, realized_pnl=5.0)
        assert pos.total_pnl == 15.0


class TestTradeDecision:
    def test_actions(self) -> None:
        td = TradeDecision(ticker="X", action=TradeDecision.Action.OPEN)
        assert td.action == TradeDecision.Action.OPEN

    def test_skip(self) -> None:
        td = TradeDecision(ticker="X", action=TradeDecision.Action.SKIP, rationale="No edge")
        assert td.rationale == "No edge"


class TestTradeOutcome:
    def test_win(self) -> None:
        to = TradeOutcome(ticker="X", result=TradeOutcome.Result.WIN,
                          entry_price=0.70, exit_price=1.0, contracts=10,
                          gross_pnl=3.0, net_pnl=2.90)
        assert to.result == TradeOutcome.Result.WIN
