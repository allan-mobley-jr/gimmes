"""Tests for BUY NO (contrarian) strategy support."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gimmes.config import GimmesConfig, Mode, StrategyConfig
from gimmes.models.market import Market, MarketStatus
from gimmes.strategy.scanner import effective_price, filter_markets
from gimmes.strategy.scorer import quick_score

# ---------------------------------------------------------------------------
# effective_price helper
# ---------------------------------------------------------------------------


class TestEffectivePrice:
    def test_yes_side_returns_unchanged(self) -> None:
        assert effective_price(0.70, "yes") == 0.70

    def test_no_side_returns_complement(self) -> None:
        assert effective_price(0.70, "no") == 0.30

    def test_no_side_boundary_high(self) -> None:
        assert effective_price(0.01, "no") == 0.99

    def test_no_side_boundary_low(self) -> None:
        assert effective_price(0.99, "no") == 0.01

    def test_no_side_midpoint(self) -> None:
        assert effective_price(0.50, "no") == 0.50

    def test_rounding(self) -> None:
        # 1 - 0.333 = 0.667 — should round to 4 decimal places
        result = effective_price(0.333, "no")
        assert result == 0.667


# ---------------------------------------------------------------------------
# Scanner with side="no"
# ---------------------------------------------------------------------------


def _make_config(side: str = "yes") -> GimmesConfig:
    return GimmesConfig(
        mode=Mode.DRIVING_RANGE,
        strategy=StrategyConfig(side=side),
    )


def _make_market(**kwargs) -> Market:  # type: ignore[no-untyped-def]
    defaults = {
        "ticker": "TEST",
        "status": MarketStatus.ACTIVE,
        "yes_bid": 0.68,
        "yes_ask": 0.72,
        "last_price": 0.70,
        "volume": 1000,
        "volume_24h": 500,
        "open_interest": 200,
        "close_time": datetime.now(UTC) + timedelta(days=7),
    }
    defaults.update(kwargs)
    return Market(**defaults)


class TestFilterMarketsNoSide:
    def test_no_side_includes_low_yes_price(self) -> None:
        """A market at YES price 0.25 has NO price 0.75 — should pass default range."""
        config = _make_config(side="no")
        markets = [
            _make_market(ticker="LOWYES", yes_bid=0.23, yes_ask=0.27, last_price=0.25),
        ]
        result = filter_markets(markets, config)
        assert len(result) == 1
        assert result[0].ticker == "LOWYES"

    def test_no_side_excludes_high_yes_price(self) -> None:
        """A market at YES price 0.70 has NO price 0.30 — below min range, excluded."""
        config = _make_config(side="no")
        markets = [
            _make_market(ticker="HIGHYES", yes_bid=0.68, yes_ask=0.72, last_price=0.70),
        ]
        result = filter_markets(markets, config)
        assert len(result) == 0

    def test_yes_side_excludes_low_yes_price(self) -> None:
        """Confirm the same low-YES market is excluded when side=yes."""
        config = _make_config(side="yes")
        markets = [
            _make_market(ticker="LOWYES", yes_bid=0.23, yes_ask=0.27, last_price=0.25),
        ]
        result = filter_markets(markets, config)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Scorer with side="no"
# ---------------------------------------------------------------------------


class TestQuickScoreNoSide:
    def test_no_side_scores_from_no_perspective(self) -> None:
        """A market at YES 0.25 (NO 0.75) should get sweet spot points when side=no."""
        config = _make_config(side="no")
        market = Market(
            ticker="X", yes_bid=0.23, yes_ask=0.27, last_price=0.25,
            volume=5000, volume_24h=2000, open_interest=500,
        )
        score = quick_score(market, config)
        # NO price 0.75 is in [0.60, 0.80] sweet spot — should get 15 points
        assert score >= 50

    def test_yes_side_low_price_loses_sweet_spot_points(self) -> None:
        """Same market with side=yes — YES price 0.25 is out of sweet spot."""
        config_no = _make_config(side="no")
        config_yes = _make_config(side="yes")
        market = Market(
            ticker="X", yes_bid=0.23, yes_ask=0.27, last_price=0.25,
            volume=5000, volume_24h=2000, open_interest=500,
        )
        score_no = quick_score(market, config_no)
        score_yes = quick_score(market, config_yes)
        # NO side gets sweet spot points (NO price 0.75 in [0.60, 0.80])
        # YES side does not (YES price 0.25 not in [0.55, 0.85])
        assert score_no > score_yes


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BacktestLedger with side="no"
# ---------------------------------------------------------------------------


class TestLedgerNoSide:
    def test_settle_no_wins_when_result_no(self) -> None:
        from gimmes.backtest.engine import BacktestLedger

        ledger = BacktestLedger(1000.0)
        ledger.buy("T1", "Test", "no", 10, 0.35, 0.50)
        trade = ledger.settle("T1", "no")
        assert trade is not None
        assert trade.pnl > 0  # Won: side="no" matches result="no"
        assert trade.payout == 10.0

    def test_settle_no_loses_when_result_yes(self) -> None:
        from gimmes.backtest.engine import BacktestLedger

        ledger = BacktestLedger(1000.0)
        ledger.buy("T1", "Test", "no", 10, 0.35, 0.50)
        trade = ledger.settle("T1", "yes")
        assert trade is not None
        assert trade.pnl < 0  # Lost: side="no" doesn't match result="yes"
        assert trade.payout == 0.0


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


class TestStrategyConfigSide:
    def test_default_side_is_no(self) -> None:
        config = GimmesConfig(mode=Mode.DRIVING_RANGE)
        assert config.strategy.side == "no"

    def test_side_yes(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="yes"),
        )
        assert config.strategy.side == "yes"
