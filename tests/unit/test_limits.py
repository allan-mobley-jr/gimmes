"""Unit tests for risk limits."""

from gimmes.config import GimmesConfig, Mode, RiskConfig
from gimmes.risk.limits import (
    check_bankroll,
    check_daily_loss,
    check_position_count,
    check_position_size,
)


class TestDailyLoss:
    def test_within_limit(self, config: GimmesConfig) -> None:
        result = check_daily_loss(-100, 10000, config)
        assert result.passed is True

    def test_at_limit(self, config: GimmesConfig) -> None:
        # 15% of 10000 = 1500
        result = check_daily_loss(-1500, 10000, config)
        assert result.passed is False

    def test_over_limit(self, config: GimmesConfig) -> None:
        result = check_daily_loss(-2000, 10000, config)
        assert result.passed is False

    def test_positive_pnl(self, config: GimmesConfig) -> None:
        result = check_daily_loss(500, 10000, config)
        assert result.passed is True


class TestPositionCount:
    def test_under_limit(self, config: GimmesConfig) -> None:
        result = check_position_count(5, config)
        assert result.passed is True

    def test_at_limit(self, config: GimmesConfig) -> None:
        result = check_position_count(15, config)
        assert result.passed is False

    def test_over_limit(self, config: GimmesConfig) -> None:
        result = check_position_count(20, config)
        assert result.passed is False


class TestPositionSize:
    def test_within_limit(self, config: GimmesConfig) -> None:
        # 5% of 10000 = 500
        result = check_position_size(300, 10000, config)
        assert result.passed is True

    def test_over_limit(self, config: GimmesConfig) -> None:
        result = check_position_size(600, 10000, config)
        assert result.passed is False


class TestBankroll:
    def test_under_limit(self, config: GimmesConfig) -> None:
        # 200 deployed + 100 trade = 300 < 500 bankroll
        result = check_bankroll(200, 100, config)
        assert result.passed is True

    def test_over_limit(self, config: GimmesConfig) -> None:
        # 400 deployed + 200 trade = 600 > 500
        result = check_bankroll(400, 200, config)
        assert result.passed is False
        assert "bankroll" in result.reason.lower()

    def test_exactly_at_limit(self, config: GimmesConfig) -> None:
        # 300 + 200 = 500 == 500 — should pass (> not >=)
        result = check_bankroll(300, 200, config)
        assert result.passed is True

    def test_zero_deployed(self, config: GimmesConfig) -> None:
        result = check_bankroll(0, 200, config)
        assert result.passed is True

    def test_championship_mode_uses_real_bankroll(self) -> None:
        champ_config = GimmesConfig(
            mode=Mode.CHAMPIONSHIP,
            risk=RiskConfig(bankroll_paper=5000.0, bankroll_real=500.0),
        )
        # 300 + 200 = 500 == bankroll_real → passes (> not >=)
        result = check_bankroll(300, 200, champ_config)
        assert result.passed is True
        # 400 + 200 = 600 > 500 bankroll_real → fails
        result = check_bankroll(400, 200, champ_config)
        assert result.passed is False

    def test_championship_zero_bankroll_blocks_trading(self) -> None:
        champ_config = GimmesConfig(mode=Mode.CHAMPIONSHIP)
        # bankroll_real defaults to 0; any positive trade should fail
        result = check_bankroll(0, 1, champ_config)
        assert result.passed is False
