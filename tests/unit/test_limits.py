"""Unit tests for risk limits."""

from gimmes.config import GimmesConfig
from gimmes.risk.limits import check_daily_loss, check_position_count, check_position_size


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
