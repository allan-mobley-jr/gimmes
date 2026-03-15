"""Unit tests for Kalshi API position parsing."""

from __future__ import annotations

import pytest

from gimmes.kalshi.portfolio import _parse_position


class TestParsePosition:
    def test_yes_position_with_unrealized_gain(self) -> None:
        data = {
            "ticker": "KXTEST",
            "position_fp": "50.00",
            "market_exposure_dollars": "32.50",
            "realized_pnl_dollars": "0.00",
            "total_traded_dollars": "25.00",
            "fees_paid_dollars": "1.50",
        }
        pos = _parse_position(data)
        assert pos.ticker == "KXTEST"
        assert pos.side == "yes"
        assert pos.count == 50
        assert pos.cost_basis == pytest.approx(26.50)  # 25.00 + 1.50
        assert pos.market_value == pytest.approx(32.50)
        assert pos.unrealized_pnl == pytest.approx(6.00)  # 32.50 - 26.50
        assert pos.avg_price == pytest.approx(0.53)  # 26.50 / 50
        assert pos.market_price == pytest.approx(0.65)  # 32.50 / 50

    def test_no_position(self) -> None:
        data = {
            "ticker": "KXFED",
            "position_fp": "-30.00",
            "market_exposure_dollars": "21.00",
            "realized_pnl_dollars": "1.50",
            "total_traded_dollars": "18.00",
            "fees_paid_dollars": "0.90",
        }
        pos = _parse_position(data)
        assert pos.side == "no"
        assert pos.count == 30
        assert pos.cost_basis == pytest.approx(18.90)
        assert pos.unrealized_pnl == pytest.approx(2.10)  # 21.00 - 18.90

    def test_zero_count_no_division_error(self) -> None:
        data = {
            "ticker": "KXZERO",
            "position_fp": "0.00",
            "market_exposure_dollars": "0.00",
            "total_traded_dollars": "10.00",
            "fees_paid_dollars": "0.50",
        }
        pos = _parse_position(data)
        assert pos.count == 0
        assert pos.unrealized_pnl == 0.0
        assert pos.avg_price == 0.0
        assert pos.market_price == 0.0

    def test_unrealized_loss(self) -> None:
        data = {
            "ticker": "KXLOSS",
            "position_fp": "10.00",
            "market_exposure_dollars": "3.00",
            "total_traded_dollars": "5.00",
            "fees_paid_dollars": "0.30",
        }
        pos = _parse_position(data)
        # unrealized = 3.00 - (5.00 + 0.30) = -2.30
        assert pos.unrealized_pnl == pytest.approx(-2.30)

    def test_legacy_format_fallback(self) -> None:
        """Old API format without _dollars/_fp suffixes still parses."""
        data = {
            "market_ticker": "KXOLD",
            "position": 20,
        }
        pos = _parse_position(data)
        assert pos.ticker == "KXOLD"
        assert pos.count == 20
        assert pos.side == "yes"
        # No cost data available — cost_basis defaults to 0
        assert pos.cost_basis == 0.0

    def test_realized_pnl_preserved(self) -> None:
        data = {
            "ticker": "KXREAL",
            "position_fp": "5.00",
            "market_exposure_dollars": "2.50",
            "realized_pnl_dollars": "3.75",
            "total_traded_dollars": "2.00",
            "fees_paid_dollars": "0.10",
        }
        pos = _parse_position(data)
        assert pos.realized_pnl == pytest.approx(3.75)
        assert pos.unrealized_pnl == pytest.approx(0.40)  # 2.50 - 2.10
