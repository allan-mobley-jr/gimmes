"""Unit tests for Kelly criterion sizing."""

from gimmes.strategy.fees import FeeMultipliers, fee_for_order
from gimmes.strategy.kelly import kelly_fraction, position_size


class TestKellyFraction:
    def test_strong_edge(self) -> None:
        # Market at 0.60, true prob 0.90, quarter Kelly
        kf = kelly_fraction(0.60, 0.90, fraction=0.25)
        assert kf > 0
        assert kf < 0.5  # Should be reasonable

    def test_no_edge(self) -> None:
        # Market at 0.70, true prob 0.70 -- no edge
        kf = kelly_fraction(0.70, 0.70)
        assert kf == 0.0

    def test_negative_edge(self) -> None:
        # Market at 0.80, true prob 0.70 -- negative edge
        kf = kelly_fraction(0.80, 0.70)
        assert kf == 0.0

    def test_full_kelly_vs_quarter(self) -> None:
        full = kelly_fraction(0.60, 0.85, fraction=1.0)
        quarter = kelly_fraction(0.60, 0.85, fraction=0.25)
        assert abs(quarter - full * 0.25) < 1e-10

    def test_extreme_edge(self) -> None:
        # Near certainty
        kf = kelly_fraction(0.55, 0.99, fraction=0.25)
        assert kf > 0

    def test_invalid_inputs(self) -> None:
        assert kelly_fraction(0, 0.50) == 0.0
        assert kelly_fraction(1.0, 0.50) == 0.0
        assert kelly_fraction(0.50, 0) == 0.0
        assert kelly_fraction(0.50, 1.5) == 0.0

    def test_taker_reduces_bet(self) -> None:
        maker = kelly_fraction(0.65, 0.85, is_taker=False)
        taker = kelly_fraction(0.65, 0.85, is_taker=True)
        assert maker > taker  # Higher fees reduce bet size

    def test_custom_multiplier_reduces_bet(self) -> None:
        default_kf = kelly_fraction(0.65, 0.85)
        high_fee_kf = kelly_fraction(0.65, 0.85, fees=FeeMultipliers(maker=0.10))
        assert high_fee_kf < default_kf


class TestPositionSize:
    def test_basic_sizing(self) -> None:
        contracts = position_size(10000, 0.65, 0.90)
        assert contracts > 0

    def test_max_position_pct(self) -> None:
        # Max 5% of $10k = $500, cost per contract = price + fee
        contracts = position_size(10000, 0.65, 0.99, max_position_pct=0.05)
        fee = fee_for_order(1, 0.65)
        max_contracts = int(0.05 * 10000 / (0.65 + fee))
        assert contracts <= max_contracts

    def test_zero_bankroll(self) -> None:
        assert position_size(0, 0.65, 0.90) == 0

    def test_no_edge(self) -> None:
        assert position_size(10000, 0.70, 0.70) == 0

    def test_max_dollar_cap(self) -> None:
        contracts = position_size(
            100000, 0.65, 0.95,
            max_position_dollars=500,
        )
        fee = fee_for_order(1, 0.65)
        assert contracts * (0.65 + fee) <= 500

    def test_custom_multiplier_reduces_contracts(self) -> None:
        default_contracts = position_size(10000, 0.65, 0.90)
        high_fee_contracts = position_size(
            10000, 0.65, 0.90, fees=FeeMultipliers(maker=0.10),
        )
        assert high_fee_contracts <= default_contracts
