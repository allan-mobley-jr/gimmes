"""Tests for P&L calculation."""

from __future__ import annotations

from gimmes.reporting.pnl import calculate_pnl


class TestCalculatePnl:
    def test_simple_win(self) -> None:
        trades = [
            {"action": "open", "ticker": "WIN", "price": 0.60, "count": 10},
            {"action": "close", "ticker": "WIN", "price": 0.80, "count": 10},
        ]
        summary = calculate_pnl(trades)
        assert summary.winning_trades == 1
        assert summary.losing_trades == 0
        assert summary.gross_pnl > 0
        assert summary.win_rate == 1.0

    def test_simple_loss(self) -> None:
        trades = [
            {"action": "open", "ticker": "LOSS", "price": 0.70, "count": 5},
            {"action": "close", "ticker": "LOSS", "price": 0.50, "count": 5},
        ]
        summary = calculate_pnl(trades)
        assert summary.losing_trades == 1
        assert summary.gross_pnl < 0
        assert summary.win_rate == 0.0

    def test_fees_estimated(self) -> None:
        trades = [
            {"action": "open", "ticker": "FEE", "price": 0.65, "count": 10},
            {"action": "close", "ticker": "FEE", "price": 0.80, "count": 10},
        ]
        summary = calculate_pnl(trades)
        assert summary.total_fees > 0
        assert summary.net_pnl < summary.gross_pnl

    def test_scratch_trade(self) -> None:
        trades = [
            {"action": "open", "ticker": "SCR", "price": 0.70, "count": 10},
            {"action": "close", "ticker": "SCR", "price": 0.70, "count": 10},
        ]
        summary = calculate_pnl(trades)
        assert summary.scratch_trades == 1
        assert summary.gross_pnl == 0.0

    def test_open_only_counted(self) -> None:
        trades = [
            {"action": "open", "ticker": "OPEN", "price": 0.60, "count": 5},
        ]
        summary = calculate_pnl(trades)
        assert summary.total_trades == 1
        assert summary.winning_trades == 0

    def test_no_trades(self) -> None:
        summary = calculate_pnl([])
        assert summary.total_trades == 0
        assert summary.win_rate == 0.0
        assert summary.net_pnl == 0.0

    def test_largest_win_and_loss(self) -> None:
        trades = [
            {"action": "open", "ticker": "BIG", "price": 0.50, "count": 20},
            {"action": "close", "ticker": "BIG", "price": 0.80, "count": 20},
            {"action": "open", "ticker": "BAD", "price": 0.70, "count": 10},
            {"action": "close", "ticker": "BAD", "price": 0.40, "count": 10},
        ]
        summary = calculate_pnl(trades)
        assert summary.largest_win > 0
        assert summary.largest_loss < 0

    def test_multiple_tickers(self) -> None:
        trades = [
            {"action": "open", "ticker": "A", "price": 0.60, "count": 10},
            {"action": "open", "ticker": "B", "price": 0.70, "count": 5},
            {"action": "close", "ticker": "A", "price": 0.80, "count": 10},
            {"action": "close", "ticker": "B", "price": 0.50, "count": 5},
        ]
        summary = calculate_pnl(trades)
        assert summary.total_trades == 2  # 2 completed trades
        assert summary.winning_trades == 1
        assert summary.losing_trades == 1
