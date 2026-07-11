"""Tests for the backtest engine."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from gimmes.backtest.engine import (
    DEFAULT_FEE_MULTIPLIERS,
    ENTRY_OFFSET_DAYS,
    BacktestLedger,
    _walk_exit,
    candle_midpoint,
    entry_candle_at,
    monthly_chunks,
    run_backtest,
    weekly_chunks,
)
from gimmes.kalshi.historical import Candle
from gimmes.strategy.fees import fee_for_order
from gimmes.strategy.kelly import position_size


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

    def test_close_gain(self) -> None:
        """#714: an early exit is a SELL at price minus the exit fee."""
        ledger = BacktestLedger(1000.0)
        ledger.buy("T1", "Test", "yes", 10, 0.65, 0.50)  # cost 7.00
        trade = ledger.close(
            "T1", result="yes", price=0.94, exit_fees=0.06,
            exit_time=None, reason="take_profit",
        )
        assert trade is not None
        assert trade.payout == pytest.approx(10 * 0.94 - 0.06)  # 9.34
        assert trade.pnl == pytest.approx(9.34 - 7.00)  # +2.34
        assert ledger.balance == pytest.approx(1000.0 - 7.00 + 9.34)
        assert "T1" not in ledger.positions
        assert trade.exit_reason == "take_profit"
        assert trade.exit_price == 0.94
        assert trade.settle_time is None
        assert trade.fees == pytest.approx(0.56)  # entry + exit legs

    def test_close_loss(self) -> None:
        ledger = BacktestLedger(1000.0)
        ledger.buy("T1", "Test", "yes", 10, 0.65, 0.50)
        trade = ledger.close(
            "T1", result="no", price=0.40, exit_fees=0.29,
            exit_time=None, reason="stop_loss",
        )
        assert trade is not None
        assert trade.payout == pytest.approx(10 * 0.40 - 0.29)  # 3.71
        assert trade.pnl == pytest.approx(3.71 - 7.00)  # -3.29
        assert ledger.balance == pytest.approx(1000.0 - 7.00 + 3.71)

    def test_close_unknown_ticker(self) -> None:
        ledger = BacktestLedger(1000.0)
        assert ledger.close(
            "UNKNOWN", result="yes", price=0.5, exit_fees=0.0,
            exit_time=None, reason="stop_loss",
        ) is None
        assert ledger.balance == 1000.0

    def test_settle_after_close_noop(self) -> None:
        """#714: the exit pops the position, so the trade's later
        settle event no-ops — no double-count."""
        ledger = BacktestLedger(1000.0)
        ledger.buy("T1", "Test", "yes", 10, 0.65, 0.50)
        ledger.close(
            "T1", result="yes", price=0.94, exit_fees=0.06,
            exit_time=None, reason="take_profit",
        )
        assert ledger.settle("T1", "yes") is None
        assert len(ledger.trades) == 1

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



class TestConcurrentPositions:
    def test_capital_locked_between_entry_and_settle(self) -> None:
        """Two overlapping positions should lock capital concurrently."""
        ledger = BacktestLedger(100.0)

        # Position 1: buy at t=1, costs 50 + 1 fee = 51
        ledger.buy("T1", "Market 1", "yes", 50, 1.0, 1.0)
        assert ledger.balance == pytest.approx(100.0 - 51.0)  # 49 left

        # Position 2: buy at t=2 — only 49 available, can't afford 60
        bought = ledger.buy("T2", "Market 2", "yes", 60, 1.0, 1.0)
        assert bought is False  # Capital locked in T1
        assert "T2" not in ledger.positions

        # Settle T1 at t=3 — frees capital
        ledger.settle("T1", "yes")
        assert ledger.balance == pytest.approx(49.0 + 50.0)  # Payout

        # Now T2 can be bought with freed capital
        bought = ledger.buy("T2", "Market 2", "yes", 60, 1.0, 1.0)
        assert bought is True

    def test_multiple_concurrent_positions(self) -> None:
        """Multiple positions can be open simultaneously."""
        ledger = BacktestLedger(1000.0)

        ledger.buy("T1", "A", "yes", 10, 0.50, 0.10)
        ledger.buy("T2", "B", "yes", 10, 0.60, 0.10)
        ledger.buy("T3", "C", "yes", 10, 0.70, 0.10)

        assert len(ledger.positions) == 3
        assert ledger.balance == pytest.approx(
            1000.0 - (10 * 0.50 + 0.10) - (10 * 0.60 + 0.10) - (10 * 0.70 + 0.10)
        )

        # Settle in different order
        ledger.settle("T2", "yes")
        assert len(ledger.positions) == 2
        ledger.settle("T1", "no")
        assert len(ledger.positions) == 1
        ledger.settle("T3", "yes")
        assert len(ledger.positions) == 0


def _ts(d: date) -> int:
    """Convert a date to midnight UTC timestamp."""
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def _ts_end(d: date) -> int:
    """Convert a date to 23:59:59 UTC timestamp."""
    return int(datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC).timestamp())


class TestMonthlyChunks:
    def test_single_month(self) -> None:
        chunks = monthly_chunks(date(2025, 1, 10), date(2025, 1, 20))
        assert len(chunks) == 1
        assert chunks[0] == (_ts(date(2025, 1, 10)), _ts_end(date(2025, 1, 20)))

    def test_multi_month(self) -> None:
        chunks = monthly_chunks(date(2025, 1, 15), date(2025, 3, 10))
        assert len(chunks) == 3
        assert chunks[0] == (_ts(date(2025, 1, 15)), _ts_end(date(2025, 1, 31)))
        assert chunks[1] == (_ts(date(2025, 2, 1)), _ts_end(date(2025, 2, 28)))
        assert chunks[2] == (_ts(date(2025, 3, 1)), _ts_end(date(2025, 3, 10)))

    def test_single_day(self) -> None:
        chunks = monthly_chunks(date(2025, 6, 15), date(2025, 6, 15))
        assert len(chunks) == 1
        assert chunks[0] == (_ts(date(2025, 6, 15)), _ts_end(date(2025, 6, 15)))

    def test_full_month(self) -> None:
        chunks = monthly_chunks(date(2025, 2, 1), date(2025, 2, 28))
        assert len(chunks) == 1
        assert chunks[0] == (_ts(date(2025, 2, 1)), _ts_end(date(2025, 2, 28)))

    def test_cross_year_boundary(self) -> None:
        chunks = monthly_chunks(date(2025, 11, 15), date(2026, 2, 10))
        assert len(chunks) == 4
        assert chunks[0] == (_ts(date(2025, 11, 15)), _ts_end(date(2025, 11, 30)))
        assert chunks[1] == (_ts(date(2025, 12, 1)), _ts_end(date(2025, 12, 31)))
        assert chunks[2] == (_ts(date(2026, 1, 1)), _ts_end(date(2026, 1, 31)))
        assert chunks[3] == (_ts(date(2026, 2, 1)), _ts_end(date(2026, 2, 10)))

    def test_leap_year(self) -> None:
        chunks = monthly_chunks(date(2024, 2, 1), date(2024, 2, 29))
        assert len(chunks) == 1
        assert chunks[0] == (_ts(date(2024, 2, 1)), _ts_end(date(2024, 2, 29)))

    def test_start_last_day_of_month(self) -> None:
        chunks = monthly_chunks(date(2025, 1, 31), date(2025, 2, 15))
        assert len(chunks) == 2
        assert chunks[0] == (_ts(date(2025, 1, 31)), _ts_end(date(2025, 1, 31)))
        assert chunks[1] == (_ts(date(2025, 2, 1)), _ts_end(date(2025, 2, 15)))

    def test_empty_range(self) -> None:
        assert monthly_chunks(date(2025, 3, 1), date(2025, 2, 1)) == []


class TestWeeklyChunks:
    def test_single_week(self) -> None:
        start = _ts(date(2025, 1, 1))
        end = _ts_end(date(2025, 1, 5))
        chunks = weekly_chunks(start, end)
        assert len(chunks) == 1
        assert chunks[0] == (start, end)

    def test_two_weeks(self) -> None:
        start = _ts(date(2025, 1, 1))
        end = _ts_end(date(2025, 1, 14))
        chunks = weekly_chunks(start, end)
        assert len(chunks) == 2
        # First chunk: Jan 1 00:00:00 to Jan 7 23:59:59
        assert chunks[0][0] == start
        # Second chunk starts right after first ends
        assert chunks[1][1] == end

    def test_full_month_produces_about_5_chunks(self) -> None:
        start = _ts(date(2025, 1, 1))
        end = _ts_end(date(2025, 1, 31))
        chunks = weekly_chunks(start, end)
        assert 4 <= len(chunks) <= 5

    def test_single_day(self) -> None:
        ts = _ts(date(2025, 6, 15))
        chunks = weekly_chunks(ts, ts)
        assert len(chunks) == 1
        assert chunks[0] == (ts, ts)


class TestConcentrationLimits:
    def test_event_exposure_tracked_for_single_trade(self) -> None:
        """Single-trade event exposure is computed correctly."""
        ledger = BacktestLedger(1000.0)
        # Cost = 10 * 0.50 + 0.10 = $5.10
        ledger.buy(
            "KXGDP-26APR30-T2.5", "GDP 2.5%", "no", 10, 0.50, 0.10,
            event_ticker="KXGDP-26APR30",
            series_ticker="KXGDP",
        )
        from gimmes.risk.limits import compute_exposure_for_group
        positions = list(ledger.positions.values())
        exp = compute_exposure_for_group(positions, "KXGDP-26APR30")
        assert exp == pytest.approx(5.10)

    def test_series_exposure_tracked(self) -> None:
        """Series exposure sums across different events."""
        ledger = BacktestLedger(1000.0)
        ledger.buy(
            "KXGDP-26APR30-T2.5", "GDP 2.5%", "no", 10, 0.50, 0.10,
            event_ticker="KXGDP-26APR30",
            series_ticker="KXGDP",
        )
        ledger.buy(
            "KXGDP-26JUL30-T3.0", "GDP 3.0%", "no", 10, 0.60, 0.10,
            event_ticker="KXGDP-26JUL30",
            series_ticker="KXGDP",
        )
        from gimmes.risk.limits import compute_exposure_for_group
        positions = list(ledger.positions.values())
        # Series exposure covers both events
        series_exp = compute_exposure_for_group(positions, "KXGDP")
        assert series_exp == pytest.approx(5.10 + 6.10)

    def test_different_event_not_blocked(self) -> None:
        """Trades in different events don't affect each other."""
        ledger = BacktestLedger(1000.0)
        ledger.buy(
            "KXGDP-26APR30-T2.5", "GDP", "no", 10, 0.50, 0.10,
            event_ticker="KXGDP-26APR30",
            series_ticker="KXGDP",
        )
        from gimmes.risk.limits import compute_exposure_for_group
        positions = list(ledger.positions.values())
        # CPI event exposure should be zero
        cpi_exp = compute_exposure_for_group(positions, "KXCPI-26APR")
        assert cpi_exp == 0.0

    def test_event_limit_blocks_when_exceeded(self) -> None:
        """check_event_exposure rejects when limit exceeded."""
        from gimmes.config import GimmesConfig, Mode, RiskConfig
        from gimmes.risk.limits import (
            check_event_exposure,
            compute_exposure_for_group,
        )

        gc = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            risk=RiskConfig(
                max_event_exposure_pct=0.10,
                bankroll_paper=1000.0,
            ),
        )
        ledger = BacktestLedger(1000.0)
        # First trade costs $80 — within 10% of $1000 ($100 limit)
        ledger.buy(
            "KXGDP-26APR30-T2.5", "GDP", "no", 100, 0.75, 5.0,
            event_ticker="KXGDP-26APR30",
        )
        positions = list(ledger.positions.values())
        evt_exp = compute_exposure_for_group(positions, "KXGDP-26APR30")
        # Second trade would cost $55 — total $135 > $100 limit
        chk = check_event_exposure(evt_exp, 55.0, 1000.0, gc)
        assert not chk.passed

    def test_settlement_frees_event_capacity(self) -> None:
        """After settling, event exposure decreases."""
        ledger = BacktestLedger(1000.0)
        ledger.buy(
            "KXGDP-26APR30-T2.5", "GDP", "no", 10, 0.50, 0.10,
            event_ticker="KXGDP-26APR30",
            series_ticker="KXGDP",
        )
        from gimmes.risk.limits import compute_exposure_for_group
        assert compute_exposure_for_group(
            list(ledger.positions.values()), "KXGDP-26APR30",
        ) > 0
        ledger.settle("KXGDP-26APR30-T2.5", "no")
        assert compute_exposure_for_group(
            list(ledger.positions.values()), "KXGDP-26APR30",
        ) == 0.0


# ---------------------------------------------------------------------------
# Strategy filters in the scoring loop (#592)
# ---------------------------------------------------------------------------


# Fixed close_time inside the BacktestConfig window (Mar 1 — May 11
# 2026). Hard-coded so tests are deterministic instead of drifting
# with wall-clock as `datetime.now()` would (#592 / Copilot review).
_FIXED_CLOSE_TIME = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)


def _stub_settlement_candles(monkeypatch, markets) -> None:
    """Patch the candle fetcher to return one entry-day candle per
    ticker whose quotes MIRROR the market's settlement quotes — the
    #655 entry-day pricing then equals the pre-#655 settlement pricing,
    preserving each legacy test's intent unchanged."""
    by_ticker = {m.ticker: m for m in markets}

    async def _fake_candles(client, ticker, *, start_ts, end_ts, **kwargs):
        m = by_ticker[ticker]
        entry_ts = int(
            (m.close_time - timedelta(days=ENTRY_OFFSET_DAYS)).timestamp(),
        )
        return [_make_candle(
            ts=entry_ts,
            yes_bid_close=m.yes_bid,
            yes_ask_close=m.yes_ask,
            price_close=(m.yes_bid + m.yes_ask) / 2,
        )]

    monkeypatch.setattr(
        "gimmes.backtest.engine.get_candlesticks", _fake_candles,
    )


def _settled_market(
    ticker: str, *, yes_bid: float, yes_ask: float, result: str = "no",
    close_time: datetime = _FIXED_CLOSE_TIME,
):
    """Synthetic settled Market — enough fields for backtest to score it."""
    from gimmes.models.market import Market, MarketStatus
    return Market(
        ticker=ticker,
        event_ticker=ticker.rsplit("-", 1)[0],
        series_ticker=ticker.split("-")[0],
        title=ticker,
        status=MarketStatus.FINALIZED,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=1.0 - yes_ask,
        no_ask=1.0 - yes_bid,
        last_price=(yes_bid + yes_ask) / 2,
        volume=10_000,
        volume_24h=1_000,
        open_interest=5_000,
        close_time=close_time,
        result=result,
    )


def _backtest_config_with_overrides(**overrides):
    """BacktestConfig with `gimme_threshold=0` so the new filters can
    be tested in isolation; pass-through kwargs override strategy
    fields. side='no' keeps effective_config_for_side from consulting
    no_overrides, so the flat values are what the engine reads."""
    import copy

    from gimmes.backtest.engine import BacktestConfig
    from gimmes.config import load_config

    base = load_config()
    cfg = copy.deepcopy(base)
    cfg.strategy.gimme_threshold = 0
    for key, value in overrides.items():
        setattr(cfg.strategy, key, value)
    cfg.strategy.side = "no"
    cfg.scanner.series = ["KXCPI"]
    cfg.scanner.no_series = []
    cfg.scanner.yes_series = []
    return BacktestConfig(
        start_date=date(2026, 3, 1),
        end_date=date(2026, 5, 11),
        starting_balance=10_000.0,
        gimmes_config=cfg,
        assumed_edge=0.10,
    )


class TestStrategyFilters:
    """Verify backtest engine honors min_true_probability and
    min_edge_after_fees in addition to gimme_threshold (#592)."""

    @pytest.mark.asyncio
    # Three NO-side midpoints inside the scanner price band
    # (min=0.40, max=0.75); varying input catches effective_price
    # inversion and threshold-edge bugs.
    @pytest.mark.parametrize(
        "yes_bid,yes_ask",
        [(0.25, 0.35), (0.40, 0.50), (0.50, 0.60)],
    )
    async def test_min_true_probability_filter_reduces_trades(
        self, monkeypatch, yes_bid: float, yes_ask: float,
    ) -> None:
        markets = [
            _settled_market(
                f"KXCPI-26MAR-T0.{i}", yes_bid=yes_bid, yes_ask=yes_ask,
            )
            for i in range(5)
        ]

        async def _fake_list(*args, **kwargs):
            return markets

        monkeypatch.setattr(
            "gimmes.backtest.engine.list_all_markets", _fake_list,
        )
        _stub_settlement_candles(monkeypatch, markets)

        permissive = await run_backtest(
            client=None,  # fetcher is stubbed
            config=_backtest_config_with_overrides(
                min_true_probability=0.0,
                min_edge_after_fees=-1.0,
            ),
        )
        strict = await run_backtest(
            client=None,
            config=_backtest_config_with_overrides(
                min_true_probability=0.95,
                min_edge_after_fees=-1.0,
            ),
        )
        assert len(permissive.trades) > 0
        assert len(strict.trades) < len(permissive.trades)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "yes_bid,yes_ask",
        [(0.25, 0.35), (0.40, 0.50), (0.50, 0.60)],
    )
    async def test_min_edge_after_fees_filter_reduces_trades(
        self, monkeypatch, yes_bid: float, yes_ask: float,
    ) -> None:
        markets = [
            _settled_market(
                f"KXCPI-26MAR-T0.{i}", yes_bid=yes_bid, yes_ask=yes_ask,
            )
            for i in range(5)
        ]

        async def _fake_list(*args, **kwargs):
            return markets

        monkeypatch.setattr(
            "gimmes.backtest.engine.list_all_markets", _fake_list,
        )
        _stub_settlement_candles(monkeypatch, markets)

        permissive = await run_backtest(
            client=None,
            config=_backtest_config_with_overrides(
                min_true_probability=0.0,
                min_edge_after_fees=0.0,
            ),
        )
        strict = await run_backtest(
            client=None,
            config=_backtest_config_with_overrides(
                min_true_probability=0.0,
                min_edge_after_fees=0.99,
            ),
        )
        assert len(permissive.trades) > 0
        assert len(strict.trades) < len(permissive.trades)

    @pytest.mark.asyncio
    async def test_live_default_thresholds_dont_reject_typical_markets(
        self, monkeypatch,
    ) -> None:
        # Regression-safety (#592 AC3): at live values
        # (min_true_probability=0.5, min_edge_after_fees=0.01), the
        # filter rejects nothing in the live-trade universe.
        markets = [
            _settled_market(
                f"KXCPI-26MAR-T0.{i}", yes_bid=0.50, yes_ask=0.55,
            )
            for i in range(5)
        ]

        async def _fake_list(*args, **kwargs):
            return markets

        monkeypatch.setattr(
            "gimmes.backtest.engine.list_all_markets", _fake_list,
        )
        _stub_settlement_candles(monkeypatch, markets)

        live_defaults = await run_backtest(
            client=None,
            config=_backtest_config_with_overrides(
                min_true_probability=0.5,
                min_edge_after_fees=0.01,
            ),
        )
        permissive = await run_backtest(
            client=None,
            config=_backtest_config_with_overrides(
                min_true_probability=0.0,
                min_edge_after_fees=-1.0,
            ),
        )
        assert len(live_defaults.trades) == len(permissive.trades)


class TestWalkExit:
    """#714: the pure walk — trigger math mirrors the LIVE formulas
    (SL on fee-inclusive cost basis; TP on fee-free max profit; mark
    = side-effective midpoint; conservative tradeable-side fill)."""

    ENTRY_TS = 1_700_000_000
    SETTLE_TS = ENTRY_TS + 86_400

    def _walk(self, candles, **kw):
        args = dict(
            side="yes", count=10, entry_eff=0.65, cost_basis=6.5,
            entry_ts=self.ENTRY_TS, settle_ts=self.SETTLE_TS,
            tp_pct=None, sl_pct=None,
        )
        args.update(kw)
        return _walk_exit(candles, **args)

    def _mid_candle(self, bid, ask, offset=43_200):
        return _make_candle(
            ts=self.ENTRY_TS + offset, yes_bid_close=bid,
            yes_ask_close=ask,
        )

    def test_stop_first_when_both_trigger(self) -> None:
        """Degenerate 0.0 thresholds co-trigger at break-even — the
        pessimistic stop wins (and 0.0 is a LEGAL threshold)."""
        hit = self._walk(
            [self._mid_candle(0.60, 0.70)], tp_pct=0.0, sl_pct=0.0,
        )
        assert hit == ("stop_loss", 0.60, self.ENTRY_TS + 43_200)

    def test_take_profit_fill_is_bid_for_yes(self) -> None:
        # mark 0.97, gain 10*0.97-6.5 = 3.2 >= 0.8*3.5 = 2.8
        hit = self._walk(
            [self._mid_candle(0.96, 0.98)], tp_pct=0.8,
        )
        assert hit == ("take_profit", 0.96, self.ENTRY_TS + 43_200)

    def test_no_side_fill_is_one_minus_ask(self) -> None:
        # NO mark = 1-0.55 = 0.45; loss 6.5-4.5 = 2.0 >= 0.15*6.5
        hit = self._walk(
            [self._mid_candle(0.50, 0.60)], side="no", sl_pct=0.15,
        )
        assert hit is not None
        assert hit[0] == "stop_loss"
        assert hit[1] == pytest.approx(0.40)  # 1 - yes_ask_close

    def test_quiet_candle_skipped(self) -> None:
        """A zero-default candle computes mark 0 — treating it as a
        crash would fabricate a stop on every quiet day."""
        hit = self._walk(
            [self._mid_candle(0.0, 0.0)], sl_pct=0.15,
        )
        assert hit is None

    def test_degenerate_candle_skipped(self) -> None:
        hit = self._walk(
            [self._mid_candle(0.99, 1.0)], tp_pct=0.5,
        )
        assert hit is None

    def test_entry_and_settlement_candles_excluded(self) -> None:
        """Look-ahead discipline: the entry candle priced the entry;
        the settlement candle encodes the outcome."""
        crash = [
            self._mid_candle(0.10, 0.20, offset=0),        # == entry_ts
            self._mid_candle(0.10, 0.20, offset=86_400),   # == settle_ts
        ]
        assert self._walk(crash, sl_pct=0.15) is None

    def test_tp_denominator_is_fee_free(self) -> None:
        """Mutation pin: max_profit = count*(1-eff) WITHOUT fees. A
        fee-inclusive denominator (count - cost_basis) is smaller and
        would fire here; the fee-free threshold holds. count=10,
        eff=0.65, cost_basis=7.0 (fee 0.50): mark 0.96 -> unrealized
        2.60 < fee-free 0.8*3.5=2.80 (hold) but >= mutant 2.40."""
        hit = self._walk(
            [self._mid_candle(0.95, 0.97)], cost_basis=7.0, tp_pct=0.8,
        )
        assert hit is None

    def test_sl_basis_is_fee_inclusive(self) -> None:
        """Mutation pin: the SL denominator is the fee-INCLUSIVE cost
        basis (live _stop_gate_pct). count=10, eff=0.65,
        cost_basis=7.0: mark 0.595 -> loss 1.05 >= 0.15*7.0 = 1.05
        (fires); a fee-free basis gives loss 0.55 < 0.975 (holds)."""
        hit = self._walk(
            [self._mid_candle(0.59, 0.60)], cost_basis=7.0, sl_pct=0.15,
        )
        assert hit is not None
        assert hit[0] == "stop_loss"

    def test_tp_none_means_no_tp(self) -> None:
        hit = self._walk(
            [self._mid_candle(0.96, 0.98)], tp_pct=None, sl_pct=0.15,
        )
        assert hit is None

    def test_sl_none_means_no_sl(self) -> None:
        hit = self._walk(
            [self._mid_candle(0.10, 0.20)], tp_pct=0.8, sl_pct=None,
        )
        assert hit is None


class _EntryDayHarness:
    """Shared pricing-path harness (no test methods — subclassing a
    Test class re-collects its tests, review #682)."""

    def _markets(self):
        # Settlement quotes 0.25/0.35 → NO eff at settlement = 0.70.
        # Since #666 selection ALSO runs on the entry-day candle, the
        # settlement quotes are payout-only context here.
        return [_settled_market("KXCPI-26MAR-T0.5",
                                yes_bid=0.25, yes_ask=0.35)]

    def _entry_ts(self, m, offset: float = ENTRY_OFFSET_DAYS) -> int:
        return int(
            (m.close_time - timedelta(days=offset)).timestamp(),
        )

    def _stub(self, monkeypatch, markets, candles_by_ticker):
        async def _fake_list(*args, **kwargs):
            return markets

        calls: list[dict] = []

        async def _fake_candles(client, ticker, *, start_ts, end_ts, **kw):
            calls.append({"ticker": ticker, "start_ts": start_ts,
                          "end_ts": end_ts})
            result = candles_by_ticker.get(ticker, [])
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(
            "gimmes.backtest.engine.list_all_markets", _fake_list,
        )
        monkeypatch.setattr(
            "gimmes.backtest.engine.get_candlesticks", _fake_candles,
        )
        return calls

    def _permissive_config(self):
        """Prob/edge gates neutralized so each test isolates the
        entry-day pricing path."""
        return _backtest_config_with_overrides(
            min_true_probability=0.0, min_edge_after_fees=-1.0,
        )

    def _taker_config(self):
        """Permissive config with the #682 taker fill model on."""
        cfg = self._permissive_config()
        cfg.taker_fill = True
        return cfg

    def _stub_entry_candle(
        self, monkeypatch, *, yes_bid, yes_ask, ts_offset=0,
    ):
        """Stub the single settled market with one entry-day candle
        (ts_offset shifts the candle timestamp, e.g. negative for a
        stale quote)."""
        markets = self._markets()
        m = markets[0]
        candles = {m.ticker: [_make_candle(
            ts=self._entry_ts(m) + ts_offset,
            yes_bid_close=yes_bid, yes_ask_close=yes_ask,
        )]}
        return self._stub(monkeypatch, markets, candles)

    async def _run(self, config=None):
        return await run_backtest(
            client=None, config=config or self._permissive_config(),
        )

    def _stub_windowed(
        self, monkeypatch, markets, entry_candles, walk_candles,
    ):
        """Window-aware stub (#714): entry windows serve the entry
        candle, walk windows (end_ts == settle_ts) the walk series."""
        async def _fake_list(*args, **kwargs):
            return markets

        calls: list[dict] = []
        settle_ts = {
            m.ticker: int(m.close_time.timestamp()) for m in markets
        }

        async def _fake_candles(client, ticker, *, start_ts, end_ts, **kw):
            calls.append({"ticker": ticker, "start_ts": start_ts,
                          "end_ts": end_ts})
            if end_ts == settle_ts[ticker] and start_ts != end_ts:
                return walk_candles.get(ticker, [])
            return entry_candles.get(ticker, [])

        monkeypatch.setattr(
            "gimmes.backtest.engine.list_all_markets", _fake_list,
        )
        monkeypatch.setattr(
            "gimmes.backtest.engine.get_candlesticks", _fake_candles,
        )
        return calls

    def _exit_config(self, tp=None, sl=None):
        cfg = self._permissive_config()
        cfg.take_profit_pct = tp
        cfg.stop_loss_pct = sl
        return cfg


class TestEntryDayPricing(_EntryDayHarness):
    """#655 regression suite: entries are priced, gated, and sized on
    the ENTRY-DAY candle — settlement data reaches only the payout."""

    @pytest.mark.asyncio
    async def test_entry_priced_from_candle_not_settlement(
        self, monkeypatch,
    ) -> None:
        markets = self._markets()
        m = markets[0]
        entry_ts = self._entry_ts(m)
        # Entry-day candle 0.30/0.40 → NO eff at entry = 0.65.
        candles = {m.ticker: [_make_candle(
            ts=entry_ts, yes_bid_close=0.30, yes_ask_close=0.40,
        )]}
        calls = self._stub(monkeypatch, markets, candles)

        result = await self._run()
        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.entry_price == pytest.approx(0.65)
        # Settlement-time NO eff was 0.70 — must NOT be the fill.
        assert t.entry_price != pytest.approx(0.70)
        # The fetch window must end AT entry time — future candles are
        # structurally unreachable.
        assert calls and all(c["end_ts"] == entry_ts for c in calls)

    @pytest.mark.asyncio
    async def test_sizing_uses_entry_day_price(self, monkeypatch) -> None:
        markets = self._markets()
        m = markets[0]
        candles = {m.ticker: [_make_candle(
            ts=self._entry_ts(m), yes_bid_close=0.30, yes_ask_close=0.40,
        )]}
        self._stub(monkeypatch, markets, candles)

        config = self._permissive_config()
        result = await self._run(config)
        assert len(result.trades) == 1
        side_cfg = config.gimmes_config.effective_config_for_side("no")
        expected = position_size(
            config.starting_balance, 0.65,
            min(0.65 + config.assumed_edge, 0.99),
            fraction=side_cfg.sizing.kelly_fraction,
            max_position_pct=side_cfg.sizing.max_position_pct,
            fees=DEFAULT_FEE_MULTIPLIERS,
            mode=side_cfg.sizing.mode,
        )
        assert result.trades[0].count == expected

    @pytest.mark.asyncio
    async def test_out_of_range_on_entry_day_dropped(
        self, monkeypatch,
    ) -> None:
        markets = self._markets()
        m = markets[0]
        # Entry-day NO eff = 0.05 — far below min_market_price.
        candles = {m.ticker: [_make_candle(
            ts=self._entry_ts(m), yes_bid_close=0.90, yes_ask_close=1.00,
        )]}
        self._stub(monkeypatch, markets, candles)

        result = await self._run()
        assert result.trades == []
        # #666: the price band now runs inside filter_markets on the
        # entry-day view — the market never passes the filter, so it
        # never reaches the pass-1 prob/edge gates.
        assert result.markets_passed_filter == 0
        assert result.skipped_entry_gates == 0

    @pytest.mark.asyncio
    async def test_no_candle_dropped(self, monkeypatch) -> None:
        markets = self._markets()
        self._stub(monkeypatch, markets, {markets[0].ticker: []})

        result = await self._run()
        assert result.trades == []
        assert result.skipped_no_candle == 1

    @pytest.mark.asyncio
    async def test_fetch_error_dropped_not_crashed(
        self, monkeypatch,
    ) -> None:
        markets = self._markets()
        self._stub(
            monkeypatch, markets,
            {markets[0].ticker: RuntimeError("api down")},
        )

        result = await self._run()
        assert result.trades == []
        # #666: a FAILED fetch is counted apart from empty history —
        # a systemic API failure must not masquerade as data sparsity.
        assert result.fetch_failures == 1
        assert result.skipped_no_candle == 0

    @pytest.mark.asyncio
    async def test_degenerate_quote_dropped(self, monkeypatch) -> None:
        markets = self._markets()
        m = markets[0]
        candles = {m.ticker: [_make_candle(
            ts=self._entry_ts(m), yes_bid_close=0.30, yes_ask_close=0.0,
        )]}
        self._stub(monkeypatch, markets, candles)

        result = await self._run()
        assert result.trades == []
        # #666: one-sided quotes get their own counter — they can
        # bias the sample away from near-certain late-life contracts
        # and must be visible separately from missing history.
        assert result.skipped_one_sided == 1
        assert result.skipped_no_candle == 0

    @pytest.mark.asyncio
    async def test_future_candle_not_used(self, monkeypatch) -> None:
        """A candle ending after entry_ts must be invisible even if the
        (stubbed) fetcher leaks it."""
        markets = self._markets()
        m = markets[0]
        candles = {m.ticker: [_make_candle(
            ts=self._entry_ts(m) + 3600,
            yes_bid_close=0.30, yes_ask_close=0.40,
        )]}
        self._stub(monkeypatch, markets, candles)

        result = await self._run()
        assert result.trades == []
        assert result.skipped_no_candle == 1


class TestEntryCandleHelpers:
    def test_candle_midpoint(self) -> None:
        assert candle_midpoint(
            _make_candle(yes_bid_close=0.30, yes_ask_close=0.40),
        ) == pytest.approx(0.35)
        assert candle_midpoint(
            _make_candle(yes_bid_close=0.0, yes_ask_close=0.40),
        ) == 0.0

    def test_entry_candle_at_boundary_and_order(self) -> None:
        c1 = _make_candle(ts=100)
        c2 = _make_candle(ts=200)
        c3 = _make_candle(ts=300)
        assert entry_candle_at([c1, c2, c3], 200) is c2  # boundary ==
        assert entry_candle_at([c1, c2, c3], 250) is c2
        assert entry_candle_at([c1, c2, c3], 50) is None
        assert entry_candle_at([], 100) is None


class TestEntryDaySelection:
    """#666 regression suite: the candidate SET is selected through
    the entry-day lens — settlement snapshots never reach
    filter_markets/quick_score."""

    _pricing = TestEntryDayPricing()

    def _stub(self, monkeypatch, markets, candles_by_ticker):
        return self._pricing._stub(monkeypatch, markets, candles_by_ticker)

    async def _run(self, config=None):
        return await self._pricing._run(config)

    @pytest.mark.asyncio
    async def test_pessimistic_omission_fixed(self, monkeypatch) -> None:
        """A market OUT of band at settlement but IN band on entry day
        must now be selected and traded — the settlement lens made it
        invisible."""
        m = _settled_market(
            "KXCPI-26MAR-T0.5", yes_bid=0.01, yes_ask=0.03,
        )  # settlement NO eff ~0.98 — outside the band
        candles = {m.ticker: [_make_candle(
            ts=self._pricing._entry_ts(m),
            yes_bid_close=0.30, yes_ask_close=0.40,  # entry NO eff 0.65
        )]}
        self._stub(monkeypatch, [m], candles)

        result = await self._run()
        assert len(result.trades) == 1
        assert result.trades[0].entry_price == pytest.approx(0.65)

    @pytest.mark.asyncio
    async def test_optimistic_volume_gating_fixed(self, monkeypatch) -> None:
        """Settlement volume passes the min but the ENTRY-DAY candle
        volume doesn't — the market must be filtered out (end-of-life
        liquidity was gating optimistically)."""
        m = _settled_market(
            "KXCPI-26MAR-T0.5", yes_bid=0.25, yes_ask=0.35,
        )  # settlement volume_24h=1000 (helper default)
        candles = {m.ticker: [_make_candle(
            ts=self._pricing._entry_ts(m),
            yes_bid_close=0.25, yes_ask_close=0.35,
            volume=0,  # no entry-day liquidity
        )]}
        self._stub(monkeypatch, [m], candles)

        result = await self._run()
        assert result.trades == []
        assert result.markets_passed_filter == 0

    @pytest.mark.asyncio
    async def test_optimistic_oi_gating_fixed(self, monkeypatch) -> None:
        m = _settled_market(
            "KXCPI-26MAR-T0.5", yes_bid=0.25, yes_ask=0.35,
        )  # settlement OI=5000
        candles = {m.ticker: [_make_candle(
            ts=self._pricing._entry_ts(m),
            yes_bid_close=0.25, yes_ask_close=0.35,
            open_interest=0,
        )]}
        self._stub(monkeypatch, [m], candles)

        result = await self._run()
        assert result.trades == []
        assert result.markets_passed_filter == 0

    @pytest.mark.asyncio
    async def test_counter_split_mixed_population(self, monkeypatch) -> None:
        close = _FIXED_CLOSE_TIME
        healthy = _settled_market(
            "KXCPI-26MAR-T0.5", yes_bid=0.25, yes_ask=0.35,
            close_time=close,
        )
        one_sided = _settled_market(
            "KXCPI-26MAR-T0.6", yes_bid=0.25, yes_ask=0.35,
            close_time=close,
        )
        no_history = _settled_market(
            "KXCPI-26MAR-T0.7", yes_bid=0.25, yes_ask=0.35,
            close_time=close,
        )
        entry_ts = self._pricing._entry_ts(healthy)
        candles = {
            healthy.ticker: [_make_candle(
                ts=entry_ts, yes_bid_close=0.25, yes_ask_close=0.35,
            )],
            one_sided.ticker: [_make_candle(
                ts=entry_ts, yes_bid_close=0.30, yes_ask_close=0.0,
            )],
            no_history.ticker: [],
        }
        self._stub(monkeypatch, [healthy, one_sided, no_history], candles)

        result = await self._run()
        assert result.skipped_one_sided == 1
        assert result.skipped_no_candle == 1
        assert len(result.trades) == 1

    @pytest.mark.asyncio
    async def test_one_fetch_per_unique_ticker(self, monkeypatch) -> None:
        """The selection replay fetches each ticker's candles exactly
        once — the cache absorbs the side loop and pass 1."""
        markets = [
            _settled_market(
                f"KXCPI-26MAR-T0.{i}", yes_bid=0.25, yes_ask=0.35,
            )
            for i in range(1, 4)
        ]
        entry_ts = self._pricing._entry_ts(markets[0])
        candles = {
            m.ticker: [_make_candle(
                ts=entry_ts, yes_bid_close=0.25, yes_ask_close=0.35,
            )]
            for m in markets
        }
        calls = self._stub(monkeypatch, markets, candles)

        await self._run()
        fetched = [c["ticker"] for c in calls]
        assert sorted(fetched) == sorted(m.ticker for m in markets)
        assert len(fetched) == len(set(fetched))

    @pytest.mark.asyncio
    async def test_score_reads_candle_liquidity(self, monkeypatch) -> None:
        """quick_score must consume the ENTRY-DAY view, not the
        settlement snapshot: with the threshold pinned between the
        settlement score (~rich liquidity) and the entry-day score
        (thin volume/OI, wide spread), a settlement-lens regression
        would trade; the entry-day lens must not (#666 review — the
        one-token-away mutant feeding original_by_ticker into
        quick_score)."""
        m = _settled_market(
            "KXCPI-26MAR-T0.5", yes_bid=0.25, yes_ask=0.35,
        )  # settlement: volume_24h=1000, OI=5000, spread 0.10
        candles = {m.ticker: [_make_candle(
            ts=self._pricing._entry_ts(m),
            yes_bid_close=0.20, yes_ask_close=0.40,  # wide spread
            volume=120, open_interest=60,  # passes filter mins, thin
        )]}
        self._stub(monkeypatch, [m], candles)

        config = _backtest_config_with_overrides(
            min_true_probability=0.0, min_edge_after_fees=-1.0,
        )
        from gimmes.strategy.scorer import quick_score

        # Pin the threshold strictly between the two lens scores.
        settlement_score = quick_score(m, config.gimmes_config)
        entry_view_score = quick_score(
            m.model_copy(update={
                "yes_bid": 0.20, "yes_ask": 0.40, "last_price": 0.0,
                "volume": 120, "volume_24h": 120, "open_interest": 60,
            }),
            config.gimmes_config,
        )
        assert entry_view_score < settlement_score
        config.gimmes_config.strategy.gimme_threshold = (
            entry_view_score + settlement_score
        ) / 2

        result = await self._run(config)
        assert result.trades == []
        assert result.markets_scored == 0
        assert result.markets_passed_filter == 1  # filter passed; score gated

    @pytest.mark.asyncio
    async def test_min_days_above_offset_warns_and_selects_nothing(
        self, monkeypatch, caplog,
    ) -> None:
        """The honest synthetic close (now + ENTRY_OFFSET_DAYS) means
        a min_days_to_resolution above the fixed offset selects
        nothing — that must be loud, not a silent data outage."""
        m = _settled_market("KXCPI-26MAR-T0.5", yes_bid=0.25, yes_ask=0.35)
        candles = {m.ticker: [_make_candle(
            ts=self._pricing._entry_ts(m),
            yes_bid_close=0.25, yes_ask_close=0.35,
        )]}
        self._stub(monkeypatch, [m], candles)

        config = _backtest_config_with_overrides(
            min_true_probability=0.0, min_edge_after_fees=-1.0,
        )
        config.gimmes_config.scanner.min_days_to_resolution = 2.0
        with caplog.at_level("WARNING", logger="gimmes.backtest.engine"):
            result = await self._run(config)
        assert result.trades == []
        assert result.markets_passed_filter == 0
        assert any(
            "min_days_to_resolution" in r.message for r in caplog.records
        )


class TestEntryDayView:
    """Unit pins for the #666 synthetic entry-day Market."""

    def test_field_mapping(self) -> None:
        from gimmes.backtest.engine import _entry_day_view

        m = _settled_market("KXCPI-26MAR-T0.5", yes_bid=0.10, yes_ask=0.20)
        candle = _make_candle(
            yes_bid_close=0.30, yes_ask_close=0.42,
            volume=77, open_interest=88,
        )
        close = datetime(2027, 1, 1, tzinfo=UTC)
        view = _entry_day_view(m, candle, close)

        assert view.yes_bid == 0.30
        assert view.yes_ask == 0.42
        assert view.midpoint == pytest.approx(0.36)
        assert view.spread == pytest.approx(0.12)
        assert view.last_price == 0.0  # never fall back to stale trades
        assert view.volume == 77
        assert view.volume_24h == 77
        assert view.open_interest == 88
        assert view.close_time == close
        assert view.status.value == "active"
        # the settled original is untouched
        assert m.yes_bid == 0.10


class TestTakerFill(_EntryDayHarness):
    """#682: --taker-fill prices entries at the ask with taker fees;
    selection still runs on the midpoint. Inherits the pricing
    harness; overrides nothing that changes the base tests."""

    @pytest.mark.asyncio
    async def test_no_side_entry_pays_the_ask(self, monkeypatch) -> None:
        """Candle 0.30/0.40: NO mid-fill = 0.65 (pinned by the base
        suite); NO taker ask = 1 − yes_bid = 0.70. Kills a bid/ask
        inversion mutant (0.70 ≠ 0.65 ≠ 0.60)."""
        self._stub_entry_candle(monkeypatch, yes_bid=0.30, yes_ask=0.40)

        result = await self._run(config=self._taker_config())
        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.entry_price == pytest.approx(0.70)

    @pytest.mark.asyncio
    async def test_taker_fees_charged(self, monkeypatch) -> None:
        from gimmes.strategy.fees import fee_for_order

        self._stub_entry_candle(monkeypatch, yes_bid=0.30, yes_ask=0.40)

        result = await self._run(config=self._taker_config())
        [t] = result.trades
        assert t.fees == pytest.approx(
            fee_for_order(t.count, 0.70, is_taker=True),
        )
        # Sanity: taker fees exceed the maker fee at the same size.
        assert t.fees > fee_for_order(t.count, 0.70, is_taker=False)

    @pytest.mark.asyncio
    async def test_json_config_reports_fill_model(self, monkeypatch) -> None:
        from gimmes.backtest.report import backtest_result_to_json

        self._stub_entry_candle(monkeypatch, yes_bid=0.30, yes_ask=0.40)
        result = await self._run(config=self._taker_config())
        assert backtest_result_to_json(result)["config"]["taker_fill"] is True


class TestStaleCandleCounter(_EntryDayHarness):
    """#682: a candle >1 day older than entry_ts prices the view — the
    counter makes it visible; the market still trades (count-only,
    policy deferred)."""

    @pytest.mark.asyncio
    async def test_stale_candle_counted_and_still_traded(
        self, monkeypatch,
    ) -> None:
        self._stub_entry_candle(
            monkeypatch, yes_bid=0.30, yes_ask=0.40,
            ts_offset=-2 * 86400,  # 2 days stale
        )

        result = await self._run()
        assert result.stale_candles == 1
        # Count-only: a future tightening must consciously break this.
        assert len(result.trades) == 1

    @pytest.mark.asyncio
    async def test_fresh_candle_not_counted(self, monkeypatch) -> None:
        self._stub_entry_candle(monkeypatch, yes_bid=0.30, yes_ask=0.40)

        result = await self._run()
        assert result.stale_candles == 0

    @pytest.mark.asyncio
    async def test_exactly_one_day_old_not_stale(self, monkeypatch) -> None:
        """Strict >: an exactly-one-day-old daily candle is the normal
        case, not stale."""
        self._stub_entry_candle(
            monkeypatch, yes_bid=0.30, yes_ask=0.40, ts_offset=-86400,
        )

        result = await self._run()
        assert result.stale_candles == 0


class TestZeroSizingCounter(_EntryDayHarness):
    """#682: pass-1 kelly drops (count <= 0) are counted, not silent."""

    @pytest.mark.asyncio
    async def test_tiny_bankroll_counts_zero_sizing(
        self, monkeypatch,
    ) -> None:
        from gimmes.backtest.engine import BacktestConfig

        self._stub_entry_candle(monkeypatch, yes_bid=0.30, yes_ask=0.40)

        base = self._permissive_config()
        config = BacktestConfig(
            start_date=base.start_date,
            end_date=base.end_date,
            starting_balance=1.0,  # $1 buys zero contracts at ~$0.65
            gimmes_config=base.gimmes_config,
            assumed_edge=base.assumed_edge,
        )
        result = await self._run(config=config)
        assert result.skipped_zero_sizing == 1
        assert result.trades == []
        # Counted at the SIZING gate, not upstream.
        assert result.skipped_entry_gates == 0


class TestBothSidesDedup(_EntryDayHarness):
    """#682: pass 1's seen_tickers guard must yield exactly one trade
    when a ticker qualifies on BOTH sides under side='both'."""

    @pytest.mark.asyncio
    async def test_ticker_traded_once_across_sides(
        self, monkeypatch,
    ) -> None:
        # Candle 0.45/0.55 → mid 0.50: yes eff 0.50, no eff 0.50 —
        # both sides in an explicit wide band.
        self._stub_entry_candle(monkeypatch, yes_bid=0.45, yes_ask=0.55)

        # effective_config_for_side RE-VALIDATES StrategyConfig, so
        # the overrides must be constructible (min edge > 0).
        config = _backtest_config_with_overrides(
            min_true_probability=0.01, min_edge_after_fees=0.0001,
            min_market_price=0.10, max_market_price=0.90,
        )
        config.gimmes_config.strategy.side = "both"
        # The operator's live config carries per-side overrides that
        # would clobber the flat test values — reset them to empty
        # (all-None fields inherit the flat values above).
        from gimmes.config import SideOverrides

        config.gimmes_config.strategy.yes_overrides = SideOverrides()
        config.gimmes_config.strategy.no_overrides = SideOverrides()

        result = await self._run(config=config)
        # Both sides genuinely qualified — without this the test can
        # pass vacuously.
        assert result.markets_scored == 2
        # The guard: exactly one trade for the ticker. markets_traded
        # is the killing assertion (without the guard, pass 2 buys
        # twice); the risk-limit asserts prove the dedup path — not a
        # concentration/balance backstop — produced the single trade.
        assert result.markets_traded == 1
        assert len(result.trades) == 1
        assert result.skipped_concentration == 0
        assert result.skipped_balance == 0


class TestTakerFillReviewPins(_EntryDayHarness):
    """#682 review: yes-side branch, is_taker threading at every call
    site, edge-gate interaction, guard bound, and the CLI wiring."""

    @pytest.mark.asyncio
    async def test_yes_side_entry_pays_the_ask(self, monkeypatch) -> None:
        """Candle 0.45/0.55, side='yes': taker entry = ask 0.55
        (mid-fill would be 0.50, bid 0.45 — three-way
        discrimination)."""
        from gimmes.config import SideOverrides

        self._stub_entry_candle(monkeypatch, yes_bid=0.45, yes_ask=0.55)

        config = _backtest_config_with_overrides(
            min_true_probability=0.01, min_edge_after_fees=0.0001,
            min_market_price=0.10, max_market_price=0.90,
        )
        config.gimmes_config.strategy.side = "yes"
        config.gimmes_config.strategy.yes_overrides = SideOverrides()
        config.taker_fill = True

        result = await self._run(config=config)
        assert len(result.trades) == 1
        assert result.trades[0].entry_price == pytest.approx(0.55)

    @pytest.mark.asyncio
    async def test_sizing_uses_taker_price_and_fees(
        self, monkeypatch,
    ) -> None:
        """Pins is_taker at the position_size call site — the fees
        test alone is self-consistent with any count."""
        from gimmes.strategy.fee_cache import DEFAULT_FEE_MULTIPLIERS
        from gimmes.strategy.kelly import position_size

        self._stub_entry_candle(monkeypatch, yes_bid=0.30, yes_ask=0.40)

        config = self._taker_config()
        result = await self._run(config=config)
        [t] = result.trades
        side_cfg = config.gimmes_config.effective_config_for_side("no")
        # true_prob anchored to the MIDPOINT (0.65 + 0.10), entry at
        # the taker ask (0.70).
        expected = position_size(
            10_000.0, 0.70, 0.75,
            is_taker=True,
            fraction=side_cfg.sizing.kelly_fraction,
            max_position_pct=side_cfg.sizing.max_position_pct,
            fees=DEFAULT_FEE_MULTIPLIERS,
            mode=side_cfg.sizing.mode,
        )
        assert t.count == expected
        # And the maker-sized mutant is genuinely different.
        assert expected != position_size(
            10_000.0, 0.70, 0.75,
            is_taker=False,
            fraction=side_cfg.sizing.kelly_fraction,
            max_position_pct=side_cfg.sizing.max_position_pct,
            fees=DEFAULT_FEE_MULTIPLIERS,
            mode=side_cfg.sizing.mode,
        )

    @pytest.mark.asyncio
    async def test_taker_mode_cannot_pass_more_markets(
        self, monkeypatch,
    ) -> None:
        """The review fix: true_prob stays midpoint-anchored, so a
        prob gate that rejects the maker fill also rejects the taker
        fill (pre-fix taker floated true_prob up and PASSED)."""
        self._stub_entry_candle(monkeypatch, yes_bid=0.30, yes_ask=0.40)

        # mid_eff = 0.65 → true_prob 0.75; taker entry 0.70 would give
        # 0.80 under the pre-fix float-up.
        config = _backtest_config_with_overrides(
            min_true_probability=0.78, min_edge_after_fees=0.0001,
        )
        config.taker_fill = True
        result = await self._run(config=config)
        assert result.trades == []
        assert result.skipped_entry_gates == 1

    @pytest.mark.asyncio
    async def test_taker_edge_gate_tighter_than_maker(
        self, monkeypatch,
    ) -> None:
        """Pins is_taker at the edge_after_fees gate: a threshold
        inside the (taker_edge, maker_edge) window rejects under
        taker and would pass under the is_taker=False mutant."""
        from gimmes.strategy.fees import edge_after_fees

        self._stub_entry_candle(monkeypatch, yes_bid=0.30, yes_ask=0.40)

        taker_edge = edge_after_fees(0.70, 0.75, is_taker=True)
        maker_edge = edge_after_fees(0.70, 0.75, is_taker=False)
        assert taker_edge < maker_edge  # window exists
        threshold = (taker_edge + maker_edge) / 2

        config = _backtest_config_with_overrides(
            min_true_probability=0.0,
            min_edge_after_fees=threshold,
        )
        config.taker_fill = True
        result = await self._run(config=config)
        assert result.trades == []
        assert result.skipped_entry_gates == 1

    @pytest.mark.asyncio
    async def test_at_bound_close_counted_one_sided(
        self, monkeypatch,
    ) -> None:
        """#682 review: an at/over-bound close (ask 1.0) is as
        unpriceable as an empty one — it must land in the one-sided
        counter, not pollute the sizing/gate counters."""
        self._stub_entry_candle(monkeypatch, yes_bid=0.40, yes_ask=1.0)

        result = await self._run(config=self._taker_config())
        assert result.skipped_one_sided == 1
        assert result.skipped_zero_sizing == 0
        assert result.trades == []

    @pytest.mark.asyncio
    async def test_zero_sizing_counts_markets_not_side_attempts(
        self, monkeypatch,
    ) -> None:
        """#682 review: under side='both' a ticker zero-sizing on both
        sides is ONE market, not two."""
        from gimmes.backtest.engine import BacktestConfig
        from gimmes.config import SideOverrides

        self._stub_entry_candle(monkeypatch, yes_bid=0.45, yes_ask=0.55)

        base = _backtest_config_with_overrides(
            min_true_probability=0.01, min_edge_after_fees=0.0001,
            min_market_price=0.10, max_market_price=0.90,
        )
        base.gimmes_config.strategy.side = "both"
        base.gimmes_config.strategy.yes_overrides = SideOverrides()
        base.gimmes_config.strategy.no_overrides = SideOverrides()
        config = BacktestConfig(
            start_date=base.start_date,
            end_date=base.end_date,
            starting_balance=1.0,  # zero-sizes on BOTH sides
            gimmes_config=base.gimmes_config,
            assumed_edge=base.assumed_edge,
        )
        result = await self._run(config=config)
        assert result.markets_scored == 2  # both sides qualified
        assert result.skipped_zero_sizing == 1  # one market


def _invoke_backtest_cli(tmp_path, *extra_args):
    """Run the backtest CLI with run_backtest faked out; returns
    (result, captured) where captured records the config and the
    candle_cache kwarg the CLI passed."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from typer.testing import CliRunner

    from gimmes.backtest.engine import BacktestResult
    from gimmes.cli import app

    captured: dict = {}

    async def _fake_run(client, config, **kwargs):
        captured["config"] = config
        captured["cache"] = kwargs.get("candle_cache")
        return BacktestResult(
            config=config, trades=[], final_balance=10_000.0,
            equity_curve=[], markets_scanned=0,
            markets_passed_filter=0, markets_scored=0, markets_traded=0,
        )

    class _FakeClient:
        def __init__(self, _config):
            pass

        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *exc):
            return False

    cfg = MagicMock()
    with (
        patch("gimmes.cli.load_config", return_value=cfg),
        patch("gimmes.backtest.engine.run_backtest", _fake_run),
        # the backtest command opens its own KalshiClient
        patch("gimmes.kalshi.client.KalshiClient", _FakeClient),
        # #696 review: the default path builds the cache at
        # GIMMES_HOME — patched to tmp_path so the test never
        # writes into the user's real ~/.gimmes (the CLI imports
        # GIMMES_HOME lazily, so the module attribute is live).
        patch("gimmes.config.GIMMES_HOME", tmp_path),
    ):
        result = CliRunner().invoke(app, [
            "backtest", "--from", "2026-05-01", "--to", "2026-05-10",
            "--json", *extra_args,
        ])
    return result, captured


def test_cli_taker_fill_flag_wired(tmp_path) -> None:
    """#682: --taker-fill reaches BacktestConfig through the CLI."""
    result, captured = _invoke_backtest_cli(tmp_path, "--taker-fill")
    assert result.exit_code == 0, result.output
    assert captured["config"].taker_fill is True
    # #696: the default path constructs a real CandleCache on disk.
    assert captured["cache"] is not None
    assert (tmp_path / "backtest_cache.db").exists()


def test_cli_no_cache_flag_bypasses_disk(tmp_path) -> None:
    """#696: --no-cache never constructs a cache or touches disk."""
    result, captured = _invoke_backtest_cli(tmp_path, "--no-cache")
    assert result.exit_code == 0, result.output
    assert captured["cache"] is None
    assert not (tmp_path / "backtest_cache.db").exists()


def test_cli_tp_sl_flags_wired(tmp_path) -> None:
    """#714: --take-profit/--stop-loss reach BacktestConfig; defaults
    stay None (hold to settlement)."""
    result, captured = _invoke_backtest_cli(
        tmp_path, "--take-profit", "0.8", "--stop-loss", "0.15",
    )
    assert result.exit_code == 0, result.output
    assert captured["config"].take_profit_pct == 0.8
    assert captured["config"].stop_loss_pct == 0.15

    result, captured = _invoke_backtest_cli(tmp_path)
    assert result.exit_code == 0, result.output
    assert captured["config"].take_profit_pct is None
    assert captured["config"].stop_loss_pct is None


def test_cli_tp_sl_out_of_range_rejected(tmp_path) -> None:
    """#714: validation runs before any engine work."""
    result, _ = _invoke_backtest_cli(tmp_path, "--take-profit", "1.5")
    assert result.exit_code == 1
    result, _ = _invoke_backtest_cli(tmp_path, "--stop-loss", "-0.1")
    assert result.exit_code == 1


def test_cli_entry_offset_wired(tmp_path) -> None:
    """#713: --entry-offset reaches BacktestConfig; the CLI default
    equals the engine constant."""
    result, captured = _invoke_backtest_cli(
        tmp_path, "--entry-offset", "2.5",
    )
    assert result.exit_code == 0, result.output
    assert captured["config"].entry_offset_days == 2.5

    result, captured = _invoke_backtest_cli(tmp_path)
    assert result.exit_code == 0, result.output
    assert captured["config"].entry_offset_days == ENTRY_OFFSET_DAYS


def test_cli_entry_offset_nonpositive_rejected(tmp_path) -> None:
    result, _ = _invoke_backtest_cli(tmp_path, "--entry-offset", "0")
    assert result.exit_code == 1
    result, _ = _invoke_backtest_cli(tmp_path, "--entry-offset", "-1")
    assert result.exit_code == 1


class TestEntryOffset(_EntryDayHarness):
    """#713: the entry offset is configurable; the fetch window,
    candle selection, synthetic close, walk boundary, and warnings
    all key off the configured value."""

    def _offset_config(self, offset, tp=None, sl=None):
        cfg = self._exit_config(tp=tp, sl=sl)
        cfg.entry_offset_days = offset
        return cfg

    @pytest.mark.asyncio
    async def test_offset_shifts_entry_window_and_candle_selection(
        self, monkeypatch,
    ) -> None:
        """The discriminating fixture: with offset 2.0 the window
        ends at close-2d, so the close-1d candle (which the default
        offset would price from) must be unreachable — both the
        window bound AND the selected candle prove the offset
        governs."""
        markets = self._markets()
        m = markets[0]
        close_ts = int(m.close_time.timestamp())
        candles = {m.ticker: [
            _make_candle(ts=close_ts - 172_800,
                         yes_bid_close=0.30, yes_ask_close=0.40),
            _make_candle(ts=close_ts - 86_400,
                         yes_bid_close=0.10, yes_ask_close=0.20),
        ]}
        calls = self._stub(monkeypatch, markets, candles)
        config = self._offset_config(2.0)
        result = await run_backtest(client=None, config=config)

        assert len(result.trades) == 1
        t = result.trades[0]
        # NO eff of the -2d candle (mid 0.35 -> 0.65), NOT the -1d
        # candle's 0.85.
        assert t.entry_price == pytest.approx(0.65)
        assert t.entry_time == m.close_time - timedelta(days=2)
        assert all(c["end_ts"] == close_ts - 172_800 for c in calls)
        assert all(
            c["start_ts"] == close_ts - 172_800 - 3 * 86_400
            for c in calls
        )

    @pytest.mark.asyncio
    async def test_larger_offset_walk_gains_candles(
        self, monkeypatch,
    ) -> None:
        """#714 synergy: a 3-day offset gives the walk interior
        candles the default offset never sees — the TP fires at
        close-2d."""
        markets = self._markets()
        m = markets[0]
        close_ts = int(m.close_time.timestamp())
        entry_ts3 = close_ts - 259_200
        entry = {m.ticker: [_make_candle(
            ts=entry_ts3, yes_bid_close=0.30, yes_ask_close=0.40,
        )]}
        walk = {m.ticker: [
            _make_candle(ts=entry_ts3 + 86_400,
                         yes_bid_close=0.02, yes_ask_close=0.06),
            _make_candle(ts=entry_ts3 + 172_800,
                         yes_bid_close=0.50, yes_ask_close=0.60),
        ]}
        calls = self._stub_windowed(monkeypatch, markets, entry, walk)
        config = self._offset_config(3.0, tp=0.8)
        result = await run_backtest(client=None, config=config)

        assert len(result.trades) == 1
        t = result.trades[0]
        assert t.exit_reason == "take_profit"
        assert t.exit_time == datetime.fromtimestamp(
            entry_ts3 + 86_400, tz=UTC,
        )
        assert result.exited_take_profit == 1
        walk_calls = [c for c in calls if c["end_ts"] == close_ts]
        assert walk_calls
        assert all(c["start_ts"] == entry_ts3 for c in walk_calls)

    @pytest.mark.asyncio
    async def test_subday_offset_warns(
        self, monkeypatch, caplog,
    ) -> None:
        import logging

        # CLI tests set propagate=False on gimmes.backtest (#696
        # lesson) — caplog captures at root; restore for this test.
        monkeypatch.setattr(
            logging.getLogger("gimmes.backtest"), "propagate", True,
        )
        markets = self._markets()
        m = markets[0]
        close_ts = int(m.close_time.timestamp())
        candles = {m.ticker: [_make_candle(
            ts=close_ts - 43_200, yes_bid_close=0.30, yes_ask_close=0.40,
        )]}
        self._stub(monkeypatch, markets, candles)
        config = self._offset_config(0.5)
        config.gimmes_config.scanner.min_days_to_resolution = 0.0

        with caplog.at_level(
            logging.WARNING, logger="gimmes.backtest.engine",
        ):
            await run_backtest(client=None, config=config)
        warnings = [r.message for r in caplog.records]
        assert any("sub-day" in w for w in warnings)
        assert not any("hold-to-settlement" in w for w in warnings)

        # Control arm (mutation pin: `< 1` must not become `<= 1`):
        # the default 1.0 offset never triggers the sub-day warning.
        caplog.clear()
        config = self._offset_config(1.0)
        config.gimmes_config.scanner.min_days_to_resolution = 0.0
        with caplog.at_level(
            logging.WARNING, logger="gimmes.backtest.engine",
        ):
            await run_backtest(client=None, config=config)
        assert not any(
            "sub-day" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_subday_offset_with_tpsl_warns_walk_degradation(
        self, monkeypatch, caplog,
    ) -> None:
        import logging

        # CLI tests set propagate=False on gimmes.backtest (#696
        # lesson) — caplog captures at root; restore for this test.
        monkeypatch.setattr(
            logging.getLogger("gimmes.backtest"), "propagate", True,
        )
        markets = self._markets()
        m = markets[0]
        close_ts = int(m.close_time.timestamp())
        candles = {m.ticker: [_make_candle(
            ts=close_ts - 43_200, yes_bid_close=0.30, yes_ask_close=0.40,
        )]}
        self._stub(monkeypatch, markets, candles)
        config = self._offset_config(0.5, tp=0.8)
        config.gimmes_config.scanner.min_days_to_resolution = 0.0

        with caplog.at_level(
            logging.WARNING, logger="gimmes.backtest.engine",
        ):
            await run_backtest(client=None, config=config)
        warnings = [r.message for r in caplog.records]
        assert any("sub-day" in w for w in warnings)
        assert any("hold-to-settlement" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_days_filter_warning_keys_off_configured_offset(
        self, monkeypatch, caplog,
    ) -> None:
        """min_days 1.5: a 2.0 offset passes (and trades — proving
        synthetic_close honors the offset); the default 1.0 offset
        warns and selects nothing."""
        import logging

        monkeypatch.setattr(
            logging.getLogger("gimmes.backtest"), "propagate", True,
        )
        markets = self._markets()
        m = markets[0]

        def _cfg(offset):
            candles = {m.ticker: [_make_candle(
                ts=int((m.close_time
                        - timedelta(days=offset)).timestamp()),
                yes_bid_close=0.30, yes_ask_close=0.40,
            )]}
            self._stub(monkeypatch, markets, candles)
            config = self._offset_config(offset)
            config.gimmes_config.scanner.min_days_to_resolution = 1.5
            config.gimmes_config.scanner.max_days_to_resolution = 90.0
            return config

        config = _cfg(2.0)
        with caplog.at_level(
            logging.WARNING, logger="gimmes.backtest.engine",
        ):
            result = await run_backtest(client=None, config=config)
        assert not any(
            "min_days_to_resolution" in r.message for r in caplog.records
        )
        assert len(result.trades) == 1

        caplog.clear()
        config = _cfg(1.0)
        with caplog.at_level(
            logging.WARNING, logger="gimmes.backtest.engine",
        ):
            result = await run_backtest(client=None, config=config)
        assert any(
            "min_days_to_resolution" in r.message for r in caplog.records
        )
        assert result.trades == []


class TestPostEntryExits(_EntryDayHarness):
    """#714: end-to-end TP/SL walk through run_backtest. The stub is
    window-aware: entry window (end_ts == entry_ts) serves the entry
    candle, walk window (end_ts == settle_ts) serves the walk series."""

    def _entry_setup(self, monkeypatch, walk_bid, walk_ask):
        """One market, entry candle 0.30/0.40 (NO eff 0.65), one
        strictly-interior walk candle at entry_ts + 12h."""
        markets = self._markets()
        m = markets[0]
        entry_ts = self._entry_ts(m)
        entry = {m.ticker: [_make_candle(
            ts=entry_ts, yes_bid_close=0.30, yes_ask_close=0.40,
        )]}
        walk = {m.ticker: [_make_candle(
            ts=entry_ts + 43_200, yes_bid_close=walk_bid,
            yes_ask_close=walk_ask,
        )]}
        calls = self._stub_windowed(monkeypatch, markets, entry, walk)
        return m, entry_ts, calls

    def _expected_entry(self, config):
        """Recompute count / entry fee / cost basis the way the
        engine does (NO eff 0.65), so tests don't hard-wire the
        operator's sizing config."""
        side_cfg = config.gimmes_config.effective_config_for_side("no")
        count = position_size(
            config.starting_balance, 0.65,
            min(0.65 + config.assumed_edge, 0.99),
            fraction=side_cfg.sizing.kelly_fraction,
            max_position_pct=side_cfg.sizing.max_position_pct,
            fees=DEFAULT_FEE_MULTIPLIERS,
            mode=side_cfg.sizing.mode,
        )
        entry_fee = fee_for_order(count, 0.65, is_taker=False)
        return count, entry_fee, count * 0.65 + entry_fee

    @pytest.mark.asyncio
    async def test_take_profit_exit(self, monkeypatch) -> None:
        # Walk candle 0.02/0.06: NO mark 0.96 — deep in profit.
        _, _, _ = self._entry_setup(monkeypatch, 0.02, 0.06)
        config = self._exit_config(tp=0.8)
        result = await run_backtest(client=None, config=config)

        assert len(result.trades) == 1
        t = result.trades[0]
        count, _, cost_basis = self._expected_entry(config)
        exit_price = 1.0 - 0.06  # NO fill = 1 - yes_ask_close
        exit_fee = fee_for_order(count, exit_price, is_taker=False)
        assert t.exit_reason == "take_profit"
        assert t.exit_price == pytest.approx(exit_price)
        assert t.payout == pytest.approx(count * exit_price - exit_fee)
        assert t.pnl == pytest.approx(t.payout - cost_basis)
        assert t.result == "no"  # eventual settlement preserved
        # settle_time keeps the WOULD-HAVE-settled moment (review:
        # exit-vs-hold analysis needs both timestamps).
        assert t.settle_time == _FIXED_CLOSE_TIME
        assert t.exit_time is not None and t.exit_time < t.settle_time
        assert result.exited_take_profit == 1
        assert result.exited_stop_loss == 0
        # The exit snapshots equity at exit time (drawdown/sharpe
        # inputs — review-found: deleting the snapshot survived).
        assert any(
            eq == pytest.approx(
                10_000.0 - cost_basis + t.payout,
            ) for _, eq in result.equity_curve
        )

    @pytest.mark.asyncio
    async def test_stop_loss_exit(self, monkeypatch) -> None:
        # Walk candle 0.50/0.60: NO mark 0.45 vs entry eff 0.65 —
        # a >15% drawdown on cost basis. The market EVENTUALLY
        # settles "no" (a win) — the stop costs money vs holding:
        # realism, pinned.
        self._entry_setup(monkeypatch, 0.50, 0.60)
        config = self._exit_config(sl=0.15)
        result = await run_backtest(client=None, config=config)

        assert len(result.trades) == 1
        t = result.trades[0]
        count, _, cost_basis = self._expected_entry(config)
        exit_price = 1.0 - 0.60
        exit_fee = fee_for_order(count, exit_price, is_taker=False)
        assert t.exit_reason == "stop_loss"
        assert t.payout == pytest.approx(count * exit_price - exit_fee)
        assert t.pnl == pytest.approx(t.payout - cost_basis)
        assert t.pnl < 0 < count * 1.0 - cost_basis  # stop lost, hold won
        assert result.exited_stop_loss == 1

    @pytest.mark.asyncio
    async def test_no_trigger_holds_to_settlement(self, monkeypatch) -> None:
        # Walk candle 0.28/0.38: NO mark 0.67 — inside both bands.
        self._entry_setup(monkeypatch, 0.28, 0.38)
        config = self._exit_config(tp=0.8, sl=0.15)
        result = await run_backtest(client=None, config=config)

        assert len(result.trades) == 1
        t = result.trades[0]
        count, _, cost_basis = self._expected_entry(config)
        assert t.exit_reason == "settled"
        assert t.exit_price is None
        assert t.payout == pytest.approx(count * 1.0)  # NO won
        assert result.exited_take_profit == 0
        assert result.exited_stop_loss == 0

    @pytest.mark.asyncio
    async def test_defaults_no_post_entry_fetch(self, monkeypatch) -> None:
        """None thresholds mean NO walk fetch at all — every candle
        call is the entry window (complements the #666 pin)."""
        m, entry_ts, calls = self._entry_setup(monkeypatch, 0.02, 0.06)
        result = await run_backtest(
            client=None, config=self._permissive_config(),
        )
        assert len(result.trades) == 1
        assert result.trades[0].exit_reason == "settled"
        assert all(c["end_ts"] == entry_ts for c in calls)

    @pytest.mark.asyncio
    async def test_zero_threshold_is_not_none(self, monkeypatch) -> None:
        """tp=0.0 is a legal threshold — a falsy gate would silently
        disable it (review guard)."""
        self._entry_setup(monkeypatch, 0.28, 0.38)  # tiny gain
        config = self._exit_config(tp=0.0)
        result = await run_backtest(client=None, config=config)
        assert result.exited_take_profit == 1

    @pytest.mark.asyncio
    async def test_walk_window_bounds(self, monkeypatch) -> None:
        """The walk fetch spans (entry_ts, settle_ts, 1440)."""
        m, entry_ts, calls = self._entry_setup(monkeypatch, 0.02, 0.06)
        settle_ts = int(m.close_time.timestamp())
        await run_backtest(client=None, config=self._exit_config(tp=0.8))
        walk_calls = [c for c in calls if c["end_ts"] == settle_ts]
        assert walk_calls, "no walk fetch recorded"
        assert all(c["start_ts"] == entry_ts for c in walk_calls)

    @pytest.mark.asyncio
    async def test_taker_fill_exit_fee(self, monkeypatch) -> None:
        """One flag governs both legs: taker entries pay taker exits."""
        self._entry_setup(monkeypatch, 0.02, 0.06)
        config = self._exit_config(tp=0.8)
        config.taker_fill = True
        result = await run_backtest(client=None, config=config)

        assert len(result.trades) == 1
        t = result.trades[0]
        # Taker entry: NO pays 1 - yes_bid = 0.70.
        side_cfg = config.gimmes_config.effective_config_for_side("no")
        count = position_size(
            config.starting_balance, 0.70,
            min(0.65 + config.assumed_edge, 0.99),
            fraction=side_cfg.sizing.kelly_fraction,
            max_position_pct=side_cfg.sizing.max_position_pct,
            fees=DEFAULT_FEE_MULTIPLIERS,
            mode=side_cfg.sizing.mode,
            is_taker=True,
        )
        entry_fee = fee_for_order(count, 0.70, is_taker=True)
        exit_fee = fee_for_order(count, t.exit_price, is_taker=True)
        assert t.fees == pytest.approx(entry_fee + exit_fee)

    @pytest.mark.asyncio
    async def test_exit_frees_balance_for_later_entry(
        self, monkeypatch,
    ) -> None:
        """Exits release capital chronologically inside Pass 2: with
        stops on, positions closed before a later entry make room
        for it (without them, the fourth entry hits the balance
        gate)."""
        close_a = _FIXED_CLOSE_TIME
        close_b = _FIXED_CLOSE_TIME + timedelta(hours=18)
        markets = [
            _settled_market(f"KXCPI-26MAR-A{i}", yes_bid=0.25,
                            yes_ask=0.35, close_time=close_a)
            for i in range(3)
        ] + [
            _settled_market("KXCPI-26MAR-B0", yes_bid=0.25,
                            yes_ask=0.35, close_time=close_b),
        ]
        entry = {}
        walk = {}
        for m in markets:
            ts = self._entry_ts(m)
            entry[m.ticker] = [_make_candle(
                ts=ts, yes_bid_close=0.30, yes_ask_close=0.40,
            )]
            # Crash candle 6h after entry: NO mark 0.45 -> stop.
            walk[m.ticker] = [_make_candle(
                ts=ts + 21_600, yes_bid_close=0.50, yes_ask_close=0.60,
            )]
        self._stub_windowed(monkeypatch, markets, entry, walk)

        def _cfg(sl=None):
            cfg = self._exit_config(sl=sl)
            gc = cfg.gimmes_config
            gc.sizing.kelly_fraction = 1.0  # ~26.5% of bankroll each
            gc.sizing.max_position_pct = 1.0
            gc.risk.max_event_exposure_pct = 1.0
            gc.risk.max_series_exposure_pct = 1.0
            return cfg

        baseline = await run_backtest(client=None, config=_cfg())
        # B can't fit: the three open A positions consume ~79% of
        # bankroll, so B trips the balance gate or (sharing the
        # KXCPI-26MAR event) the concentration gate — either way it
        # never trades without exits.
        assert len(baseline.trades) == 3
        assert (
            baseline.skipped_balance + baseline.skipped_concentration == 1
        )

        stopped = await run_backtest(client=None, config=_cfg(sl=0.15))
        # A-exits freed capital AND exposure before B's entry.
        assert len(stopped.trades) == 4
        assert stopped.skipped_balance == 0
        assert stopped.skipped_concentration == 0
        assert stopped.exited_stop_loss == 4

    @pytest.mark.asyncio
    async def test_exit_at_same_instant_does_not_fund_entry(
        self, monkeypatch,
    ) -> None:
        """Sort-key pin: exits free capital for the NEXT timestamp —
        an exit sharing a timestamp with another trade's entry must
        not fund it (entries sort before non-entries)."""
        close_a = _FIXED_CLOSE_TIME
        close_b = _FIXED_CLOSE_TIME + timedelta(hours=18)
        m_a = _settled_market("KXCPI-26MARA-A0", yes_bid=0.25,
                              yes_ask=0.35, close_time=close_a)
        m_b = _settled_market("KXCPI-26MARB-B0", yes_bid=0.25,
                              yes_ask=0.35, close_time=close_b)
        markets = [m_a, m_b]
        entry = {}
        for m in markets:
            entry[m.ticker] = [_make_candle(
                ts=self._entry_ts(m), yes_bid_close=0.30,
                yes_ask_close=0.40,
            )]
        # A's crash candle ends EXACTLY at B's entry_ts (A entry +6h).
        walk = {m_a.ticker: [_make_candle(
            ts=self._entry_ts(m_b), yes_bid_close=0.50,
            yes_ask_close=0.60,
        )], m_b.ticker: []}
        self._stub_windowed(monkeypatch, markets, entry, walk)

        cfg = self._exit_config(sl=0.15)
        gc = cfg.gimmes_config
        gc.sizing.kelly_fraction = 1.0
        gc.sizing.max_position_pct = 1.0
        gc.risk.max_event_exposure_pct = 1.0
        gc.risk.max_series_exposure_pct = 1.0
        # Geometry that distinguishes the sort-key mutant: with
        # assumed_edge 0.214, full Kelly sizes each position to ~60%
        # of the 3k balance. Without A's exit cash B cannot fit
        # (~1200 available < ~1800); WITH it (~1073 freed) B would
        # fit — so exit-before-entry ordering is the only thing that
        # makes B skip.
        cfg.assumed_edge = 0.214
        cfg.starting_balance = 3_000.0

        result = await run_backtest(client=None, config=cfg)
        # A entered; A's exit shares B's entry timestamp — B's entry
        # processes FIRST (entries sort key 0) and fails on the
        # balance or shared-series concentration gate while A is
        # still open; A's exit frees capital/exposure one instant too
        # late. An exit-first mutant would clear BOTH gates and trade
        # B (len == 2).
        assert len(result.trades) == 1
        assert (
            result.skipped_balance + result.skipped_concentration == 1
        )
        assert result.exited_stop_loss == 1

    @pytest.mark.asyncio
    async def test_walk_windows_disk_cached(
        self, monkeypatch, tmp_path,
    ) -> None:
        """#714 x #696: walk windows ride the disk cache — a warm
        rerun makes ZERO fetches (entry AND walk) and reproduces the
        same exit (review-found: the walk's cache path was dead code
        under test)."""
        from gimmes.backtest.candle_cache import CandleCache

        markets = self._markets()
        m = markets[0]
        entry_ts = self._entry_ts(m)
        entry = {m.ticker: [_make_candle(
            ts=entry_ts, yes_bid_close=0.30, yes_ask_close=0.40,
        )]}
        walk = {m.ticker: [_make_candle(
            ts=entry_ts + 43_200, yes_bid_close=0.02,
            yes_ask_close=0.06,
        )]}
        calls = self._stub_windowed(monkeypatch, markets, entry, walk)
        config = self._exit_config(tp=0.8)

        async with CandleCache(tmp_path / "c.db") as cache:
            first = await run_backtest(
                client=None, config=config, candle_cache=cache,
            )
        cold_calls = len(calls)
        assert first.exited_take_profit == 1
        assert cold_calls >= 2  # entry window + walk window

        async with CandleCache(tmp_path / "c.db") as cache:
            second = await run_backtest(
                client=None, config=config, candle_cache=cache,
            )
        assert len(calls) == cold_calls  # fully warm
        assert second.exited_take_profit == 1
        assert second.trades[0].exit_price == pytest.approx(
            first.trades[0].exit_price,
        )

    @pytest.mark.asyncio
    async def test_walk_failure_on_skipped_entry_not_counted(
        self, monkeypatch,
    ) -> None:
        """#714 Copilot semantics pin: walk_fetch_failures counts only
        ENTERED positions. B's walk fetch fails AND B is skipped at
        entry (balance/concentration) — the failure must not count
        (a pre-admission-counting revert reports 1 and fails)."""
        close_a = _FIXED_CLOSE_TIME
        close_b = _FIXED_CLOSE_TIME + timedelta(hours=18)
        m_a = _settled_market("KXCPI-26MARA-A0", yes_bid=0.25,
                              yes_ask=0.35, close_time=close_a)
        m_b = _settled_market("KXCPI-26MARB-B0", yes_bid=0.25,
                              yes_ask=0.35, close_time=close_b)
        markets = [m_a, m_b]
        settle_ts = {
            m.ticker: int(m.close_time.timestamp()) for m in markets
        }
        entry_candles = {
            m.ticker: [_make_candle(
                ts=self._entry_ts(m), yes_bid_close=0.30,
                yes_ask_close=0.40,
            )] for m in markets
        }
        a_walk = [_make_candle(
            ts=self._entry_ts(m_b), yes_bid_close=0.50,
            yes_ask_close=0.60,
        )]

        async def _fake_list(*args, **kwargs):
            return markets

        async def _fake_candles(client, ticker, *, start_ts, end_ts, **kw):
            if end_ts == settle_ts[ticker] and start_ts != end_ts:
                if ticker == m_b.ticker:
                    raise RuntimeError("walk endpoint 404")
                return a_walk
            return entry_candles[ticker]

        monkeypatch.setattr(
            "gimmes.backtest.engine.list_all_markets", _fake_list,
        )
        monkeypatch.setattr(
            "gimmes.backtest.engine.get_candlesticks", _fake_candles,
        )

        cfg = self._exit_config(sl=0.15)
        gc = cfg.gimmes_config
        gc.sizing.kelly_fraction = 1.0
        gc.sizing.max_position_pct = 1.0
        gc.risk.max_event_exposure_pct = 1.0
        gc.risk.max_series_exposure_pct = 1.0
        cfg.assumed_edge = 0.214
        cfg.starting_balance = 3_000.0

        result = await run_backtest(client=None, config=cfg)
        assert len(result.trades) == 1
        assert (
            result.skipped_balance + result.skipped_concentration == 1
        )
        assert result.exited_stop_loss == 1
        assert result.fetch_failures == 0
        # The pin: B never entered, so its failed walk is not counted.
        assert result.walk_fetch_failures == 0

    @pytest.mark.asyncio
    async def test_walk_fetch_failure_holds_and_counts(
        self, monkeypatch,
    ) -> None:
        """A failed walk fetch holds to settlement, counts in
        walk_fetch_failures, and never touches the entry-pass
        fetch_failures funnel (review-found: was silent AND
        untested)."""
        markets = self._markets()
        m = markets[0]
        entry_ts = self._entry_ts(m)
        settle_ts = int(m.close_time.timestamp())
        entry_candles = [_make_candle(
            ts=entry_ts, yes_bid_close=0.30, yes_ask_close=0.40,
        )]

        async def _fake_list(*args, **kwargs):
            return markets

        async def _fake_candles(client, ticker, *, start_ts, end_ts, **kw):
            if end_ts == settle_ts and start_ts != end_ts:
                raise RuntimeError("walk endpoint 404")
            return entry_candles

        monkeypatch.setattr(
            "gimmes.backtest.engine.list_all_markets", _fake_list,
        )
        monkeypatch.setattr(
            "gimmes.backtest.engine.get_candlesticks", _fake_candles,
        )
        result = await run_backtest(
            client=None, config=self._exit_config(sl=0.15),
        )
        assert len(result.trades) == 1
        assert result.trades[0].exit_reason == "settled"
        assert result.walk_fetch_failures == 1
        assert result.fetch_failures == 0


class TestDiskCandleCache(_EntryDayHarness):
    """#696: warm reruns make zero API calls with identical results;
    failures are never cached (the #655 visibility holds warm)."""

    @pytest.mark.asyncio
    async def test_cold_then_warm_rerun_zero_fetches(
        self, monkeypatch, tmp_path,
    ) -> None:
        from gimmes.backtest.candle_cache import CandleCache

        markets = self._markets()
        m = markets[0]
        candles = {m.ticker: [_make_candle(
            ts=self._entry_ts(m), yes_bid_close=0.30, yes_ask_close=0.40,
        )]}
        calls = self._stub(monkeypatch, markets, candles)
        config = self._permissive_config()

        async with CandleCache(tmp_path / "c.db") as cache:
            first = await run_backtest(
                client=None, config=config, candle_cache=cache,
            )
        cold_calls = len(calls)
        assert cold_calls >= 1

        async with CandleCache(tmp_path / "c.db") as cache:
            second = await run_backtest(
                client=None, config=config, candle_cache=cache,
            )
        assert len(calls) == cold_calls  # ZERO new API calls warm
        assert cache.hits >= 1
        assert len(second.trades) == len(first.trades)
        assert second.trades[0].entry_price == pytest.approx(
            first.trades[0].entry_price,
        )

    @pytest.mark.asyncio
    async def test_empty_history_negative_cached(
        self, monkeypatch, tmp_path,
    ) -> None:
        from gimmes.backtest.candle_cache import CandleCache

        markets = self._markets()
        m = markets[0]
        calls = self._stub(monkeypatch, markets, {m.ticker: []})
        config = self._permissive_config()

        async with CandleCache(tmp_path / "c.db") as cache:
            first = await run_backtest(
                client=None, config=config, candle_cache=cache,
            )
        cold_calls = len(calls)
        assert first.skipped_no_candle == 1

        async with CandleCache(tmp_path / "c.db") as cache:
            second = await run_backtest(
                client=None, config=config, candle_cache=cache,
            )
        assert len(calls) == cold_calls  # negative cache hit
        # The funnel is undistorted warm.
        assert second.skipped_no_candle == 1

    @pytest.mark.asyncio
    async def test_failures_never_cached(
        self, monkeypatch, tmp_path,
    ) -> None:
        from gimmes.backtest.candle_cache import CandleCache

        markets = self._markets()
        m = markets[0]
        calls = self._stub(
            monkeypatch, markets,
            {m.ticker: RuntimeError("endpoint 404")},
        )
        config = self._permissive_config()

        async with CandleCache(tmp_path / "c.db") as cache:
            first = await run_backtest(
                client=None, config=config, candle_cache=cache,
            )
        cold_calls = len(calls)
        assert first.fetch_failures == 1

        async with CandleCache(tmp_path / "c.db") as cache:
            second = await run_backtest(
                client=None, config=config, candle_cache=cache,
            )
        # The fetch RETRIES warm — a failure is not empty history.
        assert len(calls) == cold_calls * 2
        assert second.fetch_failures == 1

    @pytest.mark.asyncio
    async def test_engine_uses_the_documented_window_keys(
        self, monkeypatch,
    ) -> None:
        """A recording fake pins the cache-key contract: the window
        derives from close_time, making sweep keys stable."""
        from gimmes.backtest.engine import (
            CANDLE_LOOKBACK_DAYS,
        )

        markets = self._markets()
        m = markets[0]
        entry_ts = self._entry_ts(m)
        candles = {m.ticker: [_make_candle(
            ts=entry_ts, yes_bid_close=0.30, yes_ask_close=0.40,
        )]}
        self._stub(monkeypatch, markets, candles)

        recorded: list[dict] = []

        class _FakeCache:
            hits = 0

            async def get(self, ticker, **kw):
                recorded.append({"op": "get", "ticker": ticker, **kw})
                return None

            async def put(self, ticker, *, candles, **kw):
                recorded.append({"op": "put", "ticker": ticker, **kw})

        await run_backtest(
            client=None, config=self._permissive_config(),
            candle_cache=_FakeCache(),
        )
        gets = [r for r in recorded if r["op"] == "get"]
        puts = [r for r in recorded if r["op"] == "put"]
        assert gets and puts
        for r in gets + puts:
            assert r["end_ts"] == entry_ts
            assert r["start_ts"] == entry_ts - CANDLE_LOOKBACK_DAYS * 86400
            assert r["period_interval"] == 1440
