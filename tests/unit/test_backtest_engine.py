"""Tests for the backtest engine."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from gimmes.backtest.engine import (
    BacktestLedger,
    monthly_chunks,
    pick_entry_candle,
    synthesize_orderbook,
    weekly_chunks,
)
from gimmes.kalshi.historical import Candle


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


def _settled_market(
    ticker: str, *, yes_bid: float, yes_ask: float, result: str = "no",
    days_ago: int = 7,
):
    """Synthetic settled Market — enough fields for backtest to score it."""
    from datetime import timedelta

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
        close_time=datetime.now(UTC) - timedelta(days=days_ago),
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
        from gimmes.backtest.engine import run_backtest
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
        from gimmes.backtest.engine import run_backtest
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
        from gimmes.backtest.engine import run_backtest
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
