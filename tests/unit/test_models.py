"""Unit tests for Pydantic models."""

from gimmes.models.gimme import GimmeCandidate, GimmeScore
from gimmes.models.market import Market, MarketStatus, Orderbook
from gimmes.models.order import CreateOrderParams, Order, OrderAction, OrderSide
from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision


class TestMarket:
    def test_midpoint(self, sample_market: Market) -> None:
        assert sample_market.midpoint == 0.70

    def test_spread(self, sample_market: Market) -> None:
        assert abs(sample_market.spread - 0.04) < 1e-10

    def test_midpoint_fallback(self) -> None:
        m = Market(ticker="X", last_price=0.50)
        assert m.midpoint == 0.50

    def test_market_status_enum(self) -> None:
        m = Market(ticker="X", status=MarketStatus.FINALIZED)
        assert m.status == MarketStatus.FINALIZED

    def test_title_strips_bold_markdown(self) -> None:
        m = Market(ticker="X", title="Will **real GDP** exceed **3%**?")
        assert m.title == "Will real GDP exceed 3%?"

    def test_title_strips_italic_markdown(self) -> None:
        m = Market(ticker="X", title="Will *GDP* exceed 3%?")
        assert m.title == "Will GDP exceed 3%?"

    def test_title_no_markdown_unchanged(self) -> None:
        m = Market(ticker="X", title="CPI YoY > 3.2%")
        assert m.title == "CPI YoY > 3.2%"

    def test_title_strips_bold_italic(self) -> None:
        m = Market(ticker="X", title="***bold-italic***")
        assert m.title == "bold-italic"

    def test_subtitle_strips_markdown(self) -> None:
        m = Market(ticker="X", subtitle="**March 2026**")
        assert m.subtitle == "March 2026"


class TestOrderbook:
    def test_best_yes_bid(self, sample_orderbook: Orderbook) -> None:
        assert sample_orderbook.best_yes_bid == 0.68

    def test_best_yes_ask(self, sample_orderbook: Orderbook) -> None:
        # YES ask = 1 - best NO bid = 1 - 0.30 = 0.70
        assert sample_orderbook.best_yes_ask == 0.70

    def test_depth_at_price(self, sample_orderbook: Orderbook) -> None:
        # YES buyer at 0.72: opposing NO bids at 0.30 (ask=0.70) and 0.29 (ask=0.71)
        depth = sample_orderbook.depth_at_price(0.72, "yes")
        assert depth == 430  # 180 + 250

    def test_depth_at_price_exact(self, sample_orderbook: Orderbook) -> None:
        # YES buyer at 0.70: only NO bid at 0.30 (ask=0.70) eligible
        depth = sample_orderbook.depth_at_price(0.70, "yes")
        assert depth == 180

    def test_empty_orderbook(self) -> None:
        ob = Orderbook(ticker="X")
        assert ob.best_yes_bid is None
        assert ob.best_yes_ask is None


class TestGimmeScore:
    def test_qualifies_default_threshold(self) -> None:
        score = GimmeScore(total=80)
        assert score.qualifies() is True

    def test_does_not_qualify_default_threshold(self) -> None:
        score = GimmeScore(total=50)
        assert score.qualifies() is False

    def test_qualifies_custom_threshold(self) -> None:
        score = GimmeScore(total=80)
        assert score.qualifies(threshold=85) is False
        assert score.qualifies(threshold=75) is True


class TestOrder:
    def test_create_order_params(self) -> None:
        params = CreateOrderParams(
            ticker="KXTEST",
            count=10,
            yes_price=0.70,
        )
        assert params.price == 0.70

    def test_order_is_open(self) -> None:
        o = Order(order_id="abc", ticker="X", action=OrderAction.BUY,
                  side=OrderSide.YES, status="resting")
        assert o.is_open is True

    def test_order_is_not_open(self) -> None:
        o = Order(order_id="abc", ticker="X", action=OrderAction.BUY,
                  side=OrderSide.YES, status="canceled")
        assert o.is_open is False


class TestGimmeCandidate:
    def test_title_strips_markdown(self) -> None:
        gc = GimmeCandidate(ticker="X", market_price=0.5, title="**Bold title**")
        assert gc.title == "Bold title"


class TestPosition:
    def test_total_pnl(self) -> None:
        pos = Position(ticker="X", unrealized_pnl=10.0, realized_pnl=5.0)
        assert pos.total_pnl == 15.0

    def test_title_strips_markdown(self) -> None:
        pos = Position(ticker="X", title="**Bold position**")
        assert pos.title == "Bold position"


class TestTradeDecision:
    def test_actions(self) -> None:
        td = TradeDecision(ticker="X", action=TradeDecision.Action.OPEN)
        assert td.action == TradeDecision.Action.OPEN

    def test_skip(self) -> None:
        td = TradeDecision(ticker="X", action=TradeDecision.Action.SKIP, rationale="No edge")
        assert td.rationale == "No edge"
