"""Unit tests for risk limits."""

from gimmes.config import GimmesConfig
from gimmes.risk.limits import (
    check_daily_loss,
    check_position_count,
    check_position_size,
    check_session_spending,
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


class TestSessionSpending:
    def test_under_cap(self, config: GimmesConfig) -> None:
        # Default cap is 500; 100 spent + 200 trade = 300 < 500
        result = check_session_spending(100, 200, config)
        assert result.passed is True

    def test_over_cap(self, config: GimmesConfig) -> None:
        # 400 spent + 200 trade = 600 > 500
        result = check_session_spending(400, 200, config)
        assert result.passed is False
        assert "spending cap" in result.reason.lower()

    def test_exactly_at_cap(self, config: GimmesConfig) -> None:
        # 300 spent + 200 trade = 500 == 500 — should pass (> not >=)
        result = check_session_spending(300, 200, config)
        assert result.passed is True

    def test_zero_cap_disabled(self, config: GimmesConfig) -> None:
        config.risk.session_spending_cap = 0.0
        result = check_session_spending(9999, 9999, config)
        assert result.passed is True

    def test_zero_spending_under_cap(self, config: GimmesConfig) -> None:
        result = check_session_spending(0, 200, config)
        assert result.passed is True
