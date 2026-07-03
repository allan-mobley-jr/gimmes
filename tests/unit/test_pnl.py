"""Tests for P&L calculation."""

from __future__ import annotations

import pytest

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


class TestCalculatePnlWeightedAverage:
    """Regression coverage for #561 — running weighted-average cost basis.

    (Average-cost accounting at the position level — not lot-based FIFO.)
    """

    @staticmethod
    def _t(action: str, ticker: str, price: float, count: int, ts: str,
           side: str = "yes") -> dict:  # type: ignore[type-arg]
        return {
            "action": action, "ticker": ticker, "side": side,
            "price": price, "count": count, "timestamp": ts,
        }

    def test_two_opens_one_close_uses_weighted_average(self) -> None:
        trades = [
            self._t("open", "X", 0.40, 100, "2026-05-01T10:00:00"),
            self._t("open", "X", 0.60, 100, "2026-05-01T11:00:00"),
            self._t("close", "X", 0.70, 200, "2026-05-01T12:00:00"),
        ]
        # avg = 0.50; pnl = (0.70 - 0.50) * 200 = 40
        summary = calculate_pnl(trades)
        assert summary.gross_pnl == pytest.approx(40.0)
        assert summary.total_trades == 1
        assert summary.winning_trades == 1

    def test_one_open_two_partial_closes(self) -> None:
        trades = [
            self._t("open", "X", 0.50, 100, "2026-05-01T10:00:00"),
            self._t("close", "X", 0.60, 40, "2026-05-01T11:00:00"),
            self._t("close", "X", 0.70, 60, "2026-05-01T12:00:00"),
        ]
        # (0.60-0.50)*40 + (0.70-0.50)*60 = 4 + 12 = 16
        summary = calculate_pnl(trades)
        assert summary.gross_pnl == pytest.approx(16.0)
        assert summary.total_trades == 2
        assert summary.winning_trades == 2

    def test_size_up_rolls_into_average(self) -> None:
        trades = [
            self._t("open", "X", 0.50, 100, "2026-05-01T10:00:00"),
            self._t("size_up", "X", 0.60, 50, "2026-05-01T11:00:00"),
            self._t("close", "X", 0.70, 150, "2026-05-01T12:00:00"),
        ]
        # avg = (0.50*100 + 0.60*50)/150 = 0.5333...; pnl = (0.70-0.5333)*150 = 25
        summary = calculate_pnl(trades)
        assert summary.gross_pnl == pytest.approx(25.0, abs=0.01)
        assert summary.winning_trades == 1

    def test_orphan_close_no_inflation(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        trades = [self._t("close", "X", 0.80, 50, "2026-05-01T10:00:00")]
        with caplog.at_level("WARNING"):
            summary = calculate_pnl(trades)
        assert summary.gross_pnl == 0.0  # NOT 0.80 * 50 = 40
        assert summary.total_trades == 1
        assert "orphan close" in caplog.text

    def test_close_partially_orphan(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        trades = [
            self._t("open", "X", 0.50, 50, "2026-05-01T10:00:00"),
            self._t("close", "X", 0.70, 80, "2026-05-01T11:00:00"),
        ]
        with caplog.at_level("WARNING"):
            summary = calculate_pnl(trades)
        # 50 contracts matched at avg 0.50 → (0.70-0.50)*50 = 10; 30 orphan = 0
        assert summary.gross_pnl == pytest.approx(10.0)
        assert "orphan close" in caplog.text

    def test_yes_and_no_sides_isolated(self) -> None:
        trades = [
            self._t("open", "X", 0.40, 10, "t1", side="yes"),
            self._t("close", "X", 0.50, 10, "t2", side="yes"),  # +1.0
            self._t("open", "X", 0.30, 10, "t3", side="no"),
            self._t("close", "X", 0.20, 10, "t4", side="no"),   # -1.0
        ]
        summary = calculate_pnl(trades)
        assert summary.winning_trades == 1
        assert summary.losing_trades == 1
        assert summary.gross_pnl == pytest.approx(0.0)

    def test_handles_reverse_timestamp_input(self) -> None:
        # get_trades returns DESC; calculate_pnl must re-sort.
        trades = [
            self._t("close", "X", 0.70, 100, "2026-05-01T12:00:00"),
            self._t("open", "X", 0.50, 100, "2026-05-01T10:00:00"),
        ]
        summary = calculate_pnl(trades)
        assert summary.gross_pnl == pytest.approx(20.0)
        assert summary.winning_trades == 1

    def test_size_up_only_no_close_counts_residual_once(self) -> None:
        # Open + size_up with no close → 1 residual position, total_trades=1
        trades = [
            self._t("open", "X", 0.50, 100, "t1"),
            self._t("size_up", "X", 0.60, 50, "t2"),
        ]
        summary = calculate_pnl(trades)
        assert summary.total_trades == 1
        assert summary.gross_pnl == 0.0


class TestReconcileRepricing:
    """#653: reconcile drift closes reprice at settlement value when
    the group's resolution outcome is known (typically recorded on the
    OPEN row by Monitor's log-outcome, never on the drift close)."""

    def _group(self, *, close_agent, close_price, outcome_on_open):
        return [
            {
                "ticker": "KX1", "side": "no", "action": "open",
                "count": 100, "price": 0.63, "timestamp": "2026-04-20",
                "agent": "closer", "resolved_outcome": outcome_on_open,
            },
            {
                "ticker": "KX1", "side": "no", "action": "close",
                "count": 100, "price": close_price, "timestamp": "2026-04-25",
                "agent": close_agent, "resolved_outcome": None,
            },
        ]

    def test_reconcile_close_repriced_to_loss(self) -> None:
        # NO position; market resolved yes → NO lost → settlement 0.0,
        # even though the drift row carries mark 0.705.
        summary = calculate_pnl(self._group(
            close_agent="reconcile", close_price=0.705,
            outcome_on_open="yes",
        ))
        assert summary.losing_trades == 1
        assert summary.gross_pnl == pytest.approx((0.0 - 0.63) * 100)

    def test_reconcile_close_repriced_to_win(self) -> None:
        summary = calculate_pnl(self._group(
            close_agent="reconcile", close_price=0.705,
            outcome_on_open="no",
        ))
        assert summary.winning_trades == 1
        assert summary.gross_pnl == pytest.approx((1.0 - 0.63) * 100)

    def test_reconcile_close_without_outcome_stays_at_mark(self) -> None:
        """Genuine non-settlement drift (no known outcome) keeps the
        last-known mark."""
        summary = calculate_pnl(self._group(
            close_agent="reconcile", close_price=0.705,
            outcome_on_open=None,
        ))
        assert summary.gross_pnl == pytest.approx((0.705 - 0.63) * 100)

    def test_non_reconcile_close_never_repriced(self) -> None:
        """An intentional closer close keeps its actual fill price even
        when the outcome is known — repricing is drift-only."""
        summary = calculate_pnl(self._group(
            close_agent="closer", close_price=0.705,
            outcome_on_open="yes",
        ))
        assert summary.gross_pnl == pytest.approx((0.705 - 0.63) * 100)

    def test_settlement_price_close_has_no_close_fee(self) -> None:
        """Settlement closes at 1.0/0.0 are fee-free automatically via
        calculate_fee's price-range guard; open-leg fee remains."""
        summary = calculate_pnl(self._group(
            close_agent="reconcile", close_price=0.705,
            outcome_on_open="no",  # repriced to 1.0
        ))
        from gimmes.strategy.fees import fee_for_order

        assert summary.total_fees == pytest.approx(
            fee_for_order(100, 0.63),
        )


class TestOpenTradesField:
    def test_total_equals_closed_plus_open(self) -> None:
        trades = [
            {"ticker": "KX1", "side": "no", "action": "open",
             "count": 100, "price": 0.6, "timestamp": "1"},
            {"ticker": "KX1", "side": "no", "action": "close",
             "count": 100, "price": 0.8, "timestamp": "2"},
            {"ticker": "KX2", "side": "yes", "action": "open",
             "count": 50, "price": 0.4, "timestamp": "1"},
        ]
        summary = calculate_pnl(trades)
        assert summary.open_trades == 1
        closed = (
            summary.winning_trades + summary.losing_trades
            + summary.scratch_trades
        )
        assert summary.total_trades == closed + summary.open_trades
