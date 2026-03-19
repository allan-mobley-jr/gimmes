"""Unit tests for pre-trade validator."""

from gimmes.config import GimmesConfig
from gimmes.models.market import Market, MarketStatus
from gimmes.risk.validator import validate_trade


def _make_market(**kwargs) -> Market:  # type: ignore[no-untyped-def]
    defaults = {
        "ticker": "KXTEST",
        "status": MarketStatus.ACTIVE,
        "yes_bid": 0.68,
        "yes_ask": 0.72,
        "last_price": 0.70,
        "rules_primary": "This market resolves YES if X happens.",
    }
    defaults.update(kwargs)
    return Market(**defaults)


class TestValidateTrade:
    def test_all_checks_pass(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is True
        assert len(result.failures) == 0

    def test_daily_loss_exceeded(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=-2000,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("Daily loss" in f for f in result.failures)

    def test_max_positions(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=15,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("max positions" in f.lower() for f in result.failures)

    def test_insufficient_edge(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.72,  # Only 2pp edge, below 5pp min
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("edge" in f.lower() for f in result.failures)

    def test_true_probability_at_boundary_passes(self, config: GimmesConfig) -> None:
        """Trade approved when true probability equals min_true_probability (>= not >)."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,  # Exactly at 0.90 default threshold
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is True
        assert any("true probability ok" in c.lower() for c in result.checks)

    def test_true_probability_too_low(self, config: GimmesConfig) -> None:
        """Trade rejected when true probability is below min_true_probability."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.85,  # Below 0.90 default min
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("true probability" in f.lower() for f in result.failures)

    def test_duplicate_position(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=["KXTEST"],
            config=config,
        )
        assert result.approved is False
        assert any("duplicate" in f.lower() or "already" in f.lower() for f in result.failures)

    def test_size_up_bypasses_duplicate_check(self, config: GimmesConfig) -> None:
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=["KXTEST"],
            config=config,
            size_up=True,
        )
        assert result.approved is True
        assert any("size up" in c.lower() for c in result.checks)

    def test_size_up_still_enforces_other_checks(self, config: GimmesConfig) -> None:
        """SIZE UP bypasses duplicate check but other checks still apply."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=-2000,  # Exceeds 15% daily loss on 10k bankroll
            open_position_count=3,
            existing_tickers=["KXTEST"],
            config=config,
            size_up=True,
        )
        assert result.approved is False
        assert any("daily" in f.lower() or "loss" in f.lower() for f in result.failures)
        assert not any("duplicate" in f.lower() or "already" in f.lower() for f in result.failures)

    def test_size_up_rejects_when_no_existing_position(self, config: GimmesConfig) -> None:
        """SIZE UP must fail if there's no existing position to add to."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],  # No existing position
            config=config,
            size_up=True,
        )
        assert result.approved is False
        assert any("no existing position" in f.lower() for f in result.failures)

    def test_size_up_skips_position_count_check(self, config: GimmesConfig) -> None:
        """SIZE UP should not be blocked by position count at max."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=15,  # At max
            existing_tickers=["KXTEST"],
            config=config,
            size_up=True,
        )
        assert result.approved is True
        assert any("position count" in c.lower() and "skipped" in c.lower() for c in result.checks)

    def test_settlement_risk_high(self, config: GimmesConfig) -> None:
        market = _make_market(
            rules_primary="Kalshi reserves the right to cancel at sole discretion. "
                          "Death carveout applies. Subjective determination may apply."
        )
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("settlement" in f.lower() for f in result.failures)

    def test_none_probability_skips_true_prob_check(self, config: GimmesConfig) -> None:
        """When true_probability is None, the min probability check is skipped."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=None,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is True
        assert any("true probability check skipped" in c.lower() for c in result.checks)

    def test_none_probability_skips_edge_check(self, config: GimmesConfig) -> None:
        """When true_probability is None, edge check is skipped."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=None,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is True
        assert any("skipped" in c.lower() for c in result.checks)
        assert not any("edge" in f.lower() for f in result.failures)

    def test_none_probability_still_enforces_other_checks(
        self, config: GimmesConfig,
    ) -> None:
        """Even without probability, other checks still apply."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=None,
            bankroll=10000,
            daily_pnl=-2000,  # Exceeds 15% daily loss
            open_position_count=3,
            existing_tickers=[],
            config=config,
        )
        assert result.approved is False
        assert any("daily loss" in f.lower() for f in result.failures)

    def test_session_spending_disabled_when_cap_zero(self, config: GimmesConfig) -> None:
        """When cap is 0, spending check is skipped entirely."""
        config.risk.session_spending_cap = 0.0
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
            session_spent=9999,
        )
        assert result.approved is True
        assert not any("spending" in c.lower() for c in result.checks)
        assert not any("spending" in f.lower() for f in result.failures)

    def test_session_spending_cap_exceeded(self, config: GimmesConfig) -> None:
        """Trade rejected when session_spent + trade_dollars > cap."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
            session_spent=400,  # 400 + 200 = 600 > 500 cap
        )
        assert result.approved is False
        assert any("spending cap" in f.lower() for f in result.failures)

    def test_session_spending_within_limit(self, config: GimmesConfig) -> None:
        """Trade approved when session_spent + trade_dollars <= cap."""
        market = _make_market()
        result = validate_trade(
            market=market,
            trade_dollars=200,
            true_probability=0.90,
            bankroll=10000,
            daily_pnl=0,
            open_position_count=3,
            existing_tickers=[],
            config=config,
            session_spent=100,  # 100 + 200 = 300 < 500 cap
        )
        assert result.approved is True
        assert any("spending ok" in c.lower() for c in result.checks)
