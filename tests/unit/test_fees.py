"""Unit tests for fee calculator."""

import pytest

from gimmes.strategy.fees import (
    break_even_probability,
    edge_after_fees,
    maker_fee,
    taker_fee,
)


class TestTakerFee:
    def test_single_contract_50c(self) -> None:
        # 0.07 * 1 * 0.50 * 0.50 = 0.0175 -> rounds up to 0.02
        fee = taker_fee(1, 0.50)
        assert fee == 0.02

    def test_single_contract_75c(self) -> None:
        # 0.07 * 1 * 0.75 * 0.25 = 0.013125 -> rounds up to 0.02
        fee = taker_fee(1, 0.75)
        assert fee == 0.02

    def test_single_contract_95c(self) -> None:
        # 0.07 * 1 * 0.95 * 0.05 = 0.003325 -> rounds up to 0.01
        fee = taker_fee(1, 0.95)
        assert fee == 0.01

    def test_ten_contracts_70c(self) -> None:
        # 0.07 * 10 * 0.70 * 0.30 = 0.147 -> rounds up to 0.15
        fee = taker_fee(10, 0.70)
        assert fee == 0.15

    def test_zero_contracts(self) -> None:
        assert taker_fee(0, 0.50) == 0.0

    def test_invalid_price(self) -> None:
        assert taker_fee(1, 0.0) == 0.0
        assert taker_fee(1, 1.0) == 0.0
        assert taker_fee(1, -0.5) == 0.0


class TestMakerFee:
    def test_single_contract_50c(self) -> None:
        # 0.0175 * 1 * 0.50 * 0.50 = 0.004375 -> rounds up to 0.01
        fee = maker_fee(1, 0.50)
        assert fee == 0.01

    def test_single_contract_75c(self) -> None:
        # 0.0175 * 1 * 0.75 * 0.25 = 0.00328125 -> rounds up to 0.01
        fee = maker_fee(1, 0.75)
        assert fee == 0.01

    def test_ten_contracts_75c(self) -> None:
        # 0.0175 * 10 * 0.75 * 0.25 = 0.0328125 -> rounds up to 0.04
        fee = maker_fee(10, 0.75)
        assert fee == 0.04

    def test_rounding_trap(self) -> None:
        # Single contract at 0.02 (extreme): 0.0175 * 1 * 0.02 * 0.98 = 0.000343
        # Rounds up to 0.01 — effectively a 50% fee!
        fee = maker_fee(1, 0.02)
        assert fee == 0.01


class TestEdgeAfterFees:
    def test_positive_edge(self) -> None:
        # Market at 0.70, true prob 0.90, maker
        edge = edge_after_fees(0.70, 0.90)
        # Fee for 1 contract at 0.70: maker_fee(1, 0.70) = ceil(0.0175*0.7*0.3*100)/100
        # = ceil(0.3675)/100 = 0.01
        # Edge = 0.90 - (0.70 + 0.01) = 0.19
        assert edge == pytest.approx(0.19, abs=0.01)

    def test_negative_edge(self) -> None:
        # Market at 0.70, true prob 0.72
        edge = edge_after_fees(0.70, 0.72)
        assert edge < 0.05  # Below min edge threshold

    def test_taker_vs_maker_edge(self) -> None:
        maker_edge = edge_after_fees(0.70, 0.85, is_taker=False)
        taker_edge = edge_after_fees(0.70, 0.85, is_taker=True)
        assert maker_edge > taker_edge


class TestBreakEven:
    def test_break_even_at_75c(self) -> None:
        be = break_even_probability(0.75, is_taker=False)
        # Price + maker_fee(1, 0.75) = 0.75 + 0.01 = 0.76
        assert be == pytest.approx(0.76, abs=0.01)

    def test_taker_break_even_higher(self) -> None:
        be_maker = break_even_probability(0.60, is_taker=False)
        be_taker = break_even_probability(0.60, is_taker=True)
        assert be_taker > be_maker
