"""Tests for the backtest engine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gimmes.backtest.engine import (
    BacktestConfig,
    BacktestLedger,
    BacktestResult,
    pick_entry_candle,
    synthesize_orderbook,
)
from gimmes.kalshi.historical import Candle
from gimmes.models.market import OrderbookLevel


def _make_candle(
    *,
    ts: int = 1700000000,
    yes_bid_close: float = 0.65,
    yes_ask_close: float = 0.70,
    price_close: float = 0.68,
    volume: int = 500,
    open_interest: int = 200,
) -> Candle:
    return Candle(
        end_period_ts=ts,
        yes_bid_open=yes_bid_close,
        yes_bid_high=yes_bid_close,
        yes_bid_low=yes_bid_close,
        yes_bid_close=yes_bid_close,
        yes_ask_open=yes_ask_close,
        yes_ask_high=yes_ask_close,
        yes_ask_low=yes_ask_close,
        yes_ask_close=yes_ask_close,
        price_open=price_close,
        price_high=price_close,
        price_low=price_close,
        price_close=price_close,
        volume=volume,
        open_interest=open_interest,
    )


class TestBacktestLedger:
    def test_initial_state(self) -> None:
        ledger = BacktestLedger(10_000.0)
        assert ledger.balance == 10_000.0
        assert ledger.positions == {}
        assert ledger.trades == []

    def test_buy_deducts_cost(self) -> None:
        ledger = BacktestLedger(1000.0)
        result = ledger.buy("T1", "Test", "yes", 10, 0.65, 0.50)
        assert result is True
        assert ledger.balance == pytest.approx(1000.0 - 10 * 0.65 - 0.50)
        assert "T1" in ledger.positions

    def test_buy_insufficient_balance(self) -> None:
        ledger = BacktestLedger(5.0)
        result = ledger.buy("T1", "Test", "yes", 100, 0.65, 1.0)
        assert result is False
        assert ledger.balance == 5.0
        assert "T1" not in ledger.positions

    def test_settle_win(self) -> None:
        ledger = BacktestLedger(1000.0)
        ledger.buy("T1", "Test", "yes", 10, 0.65, 0.50)
        balance_after_buy = ledger.balance

        trade = ledger.settle("T1", "yes")
        assert trade is not None
        assert trade.pnl == pytest.approx(10 * 1.0 - (10 * 0.65 + 0.50))
        assert trade.payout == 10.0
        assert ledger.balance == pytest.approx(balance_after_buy + 10.0)
        assert "T1" not in ledger.positions

    def test_settle_loss(self) -> None:
        ledger = BacktestLedger(1000.0)
        ledger.buy("T1", "Test", "yes", 10, 0.65, 0.50)
        balance_after_buy = ledger.balance

        trade = ledger.settle("T1", "no")
        assert trade is not None
        assert trade.payout == 0.0
        assert trade.pnl == pytest.approx(-(10 * 0.65 + 0.50))
        assert ledger.balance == balance_after_buy  # No payout

    def test_settle_unknown_ticker(self) -> None:
        ledger = BacktestLedger(1000.0)
        assert ledger.settle("UNKNOWN", "yes") is None

    def test_snapshot(self) -> None:
        ledger = BacktestLedger(1000.0)
        ledger.snapshot("2025-01-01T00:00:00")
        assert len(ledger.equity_curve) == 1
        assert ledger.equity_curve[0] == ("2025-01-01T00:00:00", 1000.0)

    def test_multiple_trades(self) -> None:
        ledger = BacktestLedger(1000.0)

        # Trade 1: win
        ledger.buy("T1", "Win Market", "yes", 5, 0.60, 0.25)
        ledger.settle("T1", "yes")

        # Trade 2: loss
        ledger.buy("T2", "Loss Market", "yes", 5, 0.70, 0.30)
        ledger.settle("T2", "no")

        assert len(ledger.trades) == 2
        assert ledger.trades[0].pnl > 0
        assert ledger.trades[1].pnl < 0


class TestSynthesizeOrderbook:
    def test_basic_orderbook(self) -> None:
        candle = _make_candle(yes_bid_close=0.65, yes_ask_close=0.70)
        ob = synthesize_orderbook("T1", candle)

        assert ob.ticker == "T1"
        assert len(ob.yes_bids) == 1
        assert ob.yes_bids[0].price == 0.65
        assert ob.yes_bids[0].quantity == 100

        assert len(ob.no_bids) == 1
        assert ob.no_bids[0].price == 0.30  # 1 - 0.70
        assert ob.no_bids[0].quantity == 100

    def test_custom_depth(self) -> None:
        candle = _make_candle()
        ob = synthesize_orderbook("T1", candle, depth=50)
        assert ob.yes_bids[0].quantity == 50

    def test_zero_bid_produces_empty(self) -> None:
        candle = _make_candle(yes_bid_close=0.0, yes_ask_close=0.0)
        ob = synthesize_orderbook("T1", candle)
        assert ob.yes_bids == []
        assert ob.no_bids == []

    def test_best_yes_ask_derived(self) -> None:
        candle = _make_candle(yes_ask_close=0.72)
        ob = synthesize_orderbook("T1", candle)
        # NO bid = 1 - 0.72 = 0.28, so best YES ask = 1 - 0.28 = 0.72
        assert ob.best_yes_ask == 0.72


class TestPickEntryCandle:
    def test_picks_last_in_range(self) -> None:
        candles = [
            _make_candle(ts=1, price_close=0.50),
            _make_candle(ts=2, price_close=0.65),
            _make_candle(ts=3, price_close=0.70),
            _make_candle(ts=4, price_close=0.90),  # Out of range
        ]
        result = pick_entry_candle(candles, 0.55, 0.85)
        assert result is not None
        assert result.end_period_ts == 3

    def test_none_when_no_candle_in_range(self) -> None:
        candles = [
            _make_candle(ts=1, price_close=0.10),
            _make_candle(ts=2, price_close=0.95),
        ]
        result = pick_entry_candle(candles, 0.55, 0.85)
        assert result is None

    def test_empty_candles(self) -> None:
        assert pick_entry_candle([], 0.55, 0.85) is None

    def test_single_candle_in_range(self) -> None:
        candles = [_make_candle(ts=1, price_close=0.70)]
        result = pick_entry_candle(candles, 0.55, 0.85)
        assert result is not None
        assert result.price_close == 0.70
