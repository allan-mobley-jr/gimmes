"""Unit tests for market scanner."""

from datetime import UTC, datetime, timedelta

from gimmes.config import GimmesConfig, Mode, ScannerConfig, StrategyConfig
from gimmes.models.market import Market, MarketStatus
from gimmes.strategy.scanner import days_until, filter_markets


def _make_market(**kwargs) -> Market:  # type: ignore[no-untyped-def]
    defaults = {
        "ticker": "TEST",
        "status": MarketStatus.ACTIVE,
        "yes_bid": 0.68,
        "yes_ask": 0.72,
        "last_price": 0.70,
        "volume": 1000,
        "volume_24h": 500,
        "open_interest": 200,
        "close_time": datetime.now(UTC) + timedelta(days=7),
    }
    defaults.update(kwargs)
    return Market(**defaults)


class TestFilterMarkets:
    def test_price_range_filter(self, config: GimmesConfig) -> None:
        markets = [
            _make_market(ticker="LOW", yes_bid=0.20, yes_ask=0.25, last_price=0.22),
            _make_market(ticker="MID", yes_bid=0.68, yes_ask=0.72, last_price=0.70),
            _make_market(ticker="HIGH", yes_bid=0.90, yes_ask=0.95, last_price=0.92),
        ]
        result = filter_markets(markets, config)
        assert len(result) == 1
        assert result[0].ticker == "MID"

    def test_closed_markets_excluded(self, config: GimmesConfig) -> None:
        markets = [
            _make_market(ticker="OPEN"),
            _make_market(ticker="CLOSED", status=MarketStatus.CLOSED),
        ]
        result = filter_markets(markets, config)
        assert len(result) == 1
        assert result[0].ticker == "OPEN"

    def test_low_volume_excluded(self, config: GimmesConfig) -> None:
        markets = [
            _make_market(ticker="GOOD", volume_24h=500),
            _make_market(ticker="LOW", volume=50, volume_24h=0),
        ]
        result = filter_markets(markets, config)
        assert len(result) == 1
        assert result[0].ticker == "GOOD"

    def test_low_open_interest_excluded(self, config: GimmesConfig) -> None:
        markets = [
            _make_market(ticker="GOOD", open_interest=200),
            _make_market(ticker="LOW", open_interest=10),
        ]
        result = filter_markets(markets, config)
        assert len(result) == 1
        assert result[0].ticker == "GOOD"

    def test_sorted_by_volume(self, config: GimmesConfig) -> None:
        markets = [
            _make_market(ticker="LOW_VOL", volume_24h=200),
            _make_market(ticker="HIGH_VOL", volume_24h=5000),
            _make_market(ticker="MED_VOL", volume_24h=1000),
        ]
        result = filter_markets(markets, config)
        assert [m.ticker for m in result] == ["HIGH_VOL", "MED_VOL", "LOW_VOL"]

    def test_empty_list(self, config: GimmesConfig) -> None:
        assert filter_markets([], config) == []

    def test_exclude_tickers_filtered(self, config: GimmesConfig) -> None:
        markets = [
            _make_market(ticker="A"),
            _make_market(ticker="B"),
            _make_market(ticker="C"),
        ]
        result = filter_markets(markets, config, exclude_tickers={"A", "C"})
        assert len(result) == 1
        assert result[0].ticker == "B"

    def test_exclude_tickers_none_passes_all(self, config: GimmesConfig) -> None:
        markets = [
            _make_market(ticker="A"),
            _make_market(ticker="B"),
        ]
        result = filter_markets(markets, config, exclude_tickers=None)
        assert len(result) == 2

    def test_exclude_tickers_empty_set_passes_all(self, config: GimmesConfig) -> None:
        markets = [
            _make_market(ticker="A"),
            _make_market(ticker="B"),
        ]
        result = filter_markets(markets, config, exclude_tickers=set())
        assert len(result) == 2


def _hourly_config(**strategy_kwargs) -> GimmesConfig:
    strategy_kwargs.setdefault("side", "yes")
    return GimmesConfig(
        mode=Mode.DRIVING_RANGE,
        strategy=StrategyConfig(**strategy_kwargs),
        scanner=ScannerConfig(hourly_series=["KXBTCD"]),
    )


# Fixed clock for the hourly tests (#736): the next-top-of-hour bound
# makes wall-clock fixtures flaky (now+29min crosses the hour boundary
# for roughly half of every hour), so every hourly test injects now=.
_HOURLY_NOW = datetime(2026, 6, 23, 13, 30, tzinfo=UTC)  # next top 14:00 UTC
_NEXT_TOP = datetime(2026, 6, 23, 14, 0, tzinfo=UTC)


class TestHourlyFilter:
    """#722/#736: hourly-series tickers use the hourly price band and
    must settle at the NEXT top of hour; everything is inert while
    hourly_series is empty."""

    def test_hourly_bypasses_min_days_floor(self) -> None:
        m = _make_market(
            ticker="KXBTCD-26JUN23H14-T119999.99",
            close_time=_HOURLY_NOW + timedelta(minutes=29),
        )
        result = filter_markets([m], _hourly_config(), now=_HOURLY_NOW)
        assert [r.ticker for r in result] == [m.ticker]

    def test_inert_when_hourly_series_empty(self, config: GimmesConfig) -> None:
        # Identical market, default config: min-days floor rejects it
        m = _make_market(
            ticker="KXBTCD-26JUN23H14-T119999.99",
            close_time=_HOURLY_NOW + timedelta(minutes=29),
        )
        assert filter_markets([m], config, now=_HOURLY_NOW) == []

    def test_hourly_respects_max_days(self) -> None:
        # Now doubly rejected: the next-hour bound (#736) fires first,
        # max_days remains defense in depth
        m = _make_market(
            ticker="KXBTCD-26DEC31H14-T119999.99",
            close_time=_HOURLY_NOW + timedelta(days=120),
        )
        assert filter_markets([m], _hourly_config(), now=_HOURLY_NOW) == []

    def test_hourly_far_hour_rejected(self) -> None:
        # The #736 headline bug: a ticker settling at the hour AFTER
        # next (90 min out) must not reach the shortlist
        m = _make_market(
            ticker="KXBTCD-26JUN23H15-T119999.99",
            close_time=_NEXT_TOP + timedelta(hours=1),
        )
        assert filter_markets([m], _hourly_config(), now=_HOURLY_NOW) == []

    def test_hourly_past_close_rejected(self) -> None:
        # The latent #736 bug: the min-days bypass also skipped the
        # negative-days rejection, so a stale straggler passed
        m = _make_market(
            ticker="KXBTCD-26JUN23H13-T119999.99",
            close_time=_HOURLY_NOW - timedelta(minutes=5),
        )
        assert filter_markets([m], _hourly_config(), now=_HOURLY_NOW) == []

    def test_hourly_close_at_now_rejected(self) -> None:
        # Strict lower bound: settling this instant is not tradeable
        m = _make_market(
            ticker="KXBTCD-26JUN23H13-T119999.99",
            close_time=_HOURLY_NOW,
        )
        assert filter_markets([m], _hourly_config(), now=_HOURLY_NOW) == []

    def test_hourly_exactly_at_next_top_passes(self) -> None:
        # The normal case: hourly markets close exactly at the top
        m = _make_market(
            ticker="KXBTCD-26JUN23H14-T119999.99",
            close_time=_NEXT_TOP,
        )
        result = filter_markets([m], _hourly_config(), now=_HOURLY_NOW)
        assert [r.ticker for r in result] == [m.ticker]

    def test_hourly_tolerance_edge(self) -> None:
        inside = _make_market(
            ticker="KXBTCD-26JUN23H14-T1",
            close_time=_NEXT_TOP + timedelta(seconds=60),
        )
        outside = _make_market(
            ticker="KXBTCD-26JUN23H14-T2",
            close_time=_NEXT_TOP + timedelta(seconds=61),
        )
        result = filter_markets(
            [inside, outside], _hourly_config(), now=_HOURLY_NOW,
        )
        assert [r.ticker for r in result] == ["KXBTCD-26JUN23H14-T1"]

    def test_hourly_naive_close_time_passes(self) -> None:
        # tz-safety: naive close_times coerce to UTC (test fixtures)
        m = _make_market(
            ticker="KXBTCD-26JUN23H14-T119999.99",
            close_time=datetime(2026, 6, 23, 13, 59),
        )
        result = filter_markets([m], _hourly_config(), now=_HOURLY_NOW)
        assert [r.ticker for r in result] == [m.ticker]

    def test_hourly_dst_fall_back_sanity(self) -> None:
        # Inside the repeated 1 AM ET hour: pure-UTC arithmetic is
        # unaffected by the fold
        now = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # 01:30 EDT
        this_hour = _make_market(
            ticker="KXBTCD-26NOV01H01-T1",
            close_time=datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
        )
        next_hour = _make_market(
            ticker="KXBTCD-26NOV01H01-T2",
            close_time=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        )
        result = filter_markets(
            [this_hour, next_hour], _hourly_config(), now=now,
        )
        assert [r.ticker for r in result] == ["KXBTCD-26NOV01H01-T1"]

    def test_inert_far_hour_rejected_by_min_days(self) -> None:
        # Inertness of the new bound: with hourly_series empty, a
        # far-hour KXBTCD ticker is still rejected — by the min-days
        # floor, exactly as before #736
        m = _make_market(
            ticker="KXBTCD-26JUN23H15-T119999.99",
            close_time=_HOURLY_NOW + timedelta(minutes=90),
        )
        cfg = GimmesConfig(mode=Mode.DRIVING_RANGE)
        assert filter_markets([m], cfg, now=_HOURLY_NOW) == []

    def test_hourly_expiration_fallback(self) -> None:
        # The resolve_dt refactor's fallback: close_time None, bound
        # applies to expiration_time (kills the fallback-drop mutation)
        passes = _make_market(
            ticker="KXBTCD-26JUN23H14-T1",
            close_time=None, expiration_time=_NEXT_TOP,
        )
        rejected = _make_market(
            ticker="KXBTCD-26JUN23H15-T2",
            close_time=None,
            expiration_time=_NEXT_TOP + timedelta(hours=1),
        )
        result = filter_markets(
            [passes, rejected], _hourly_config(), now=_HOURLY_NOW,
        )
        assert [r.ticker for r in result] == ["KXBTCD-26JUN23H14-T1"]

    def test_hourly_just_after_now_passes(self) -> None:
        # Strict lower bound from the passing side
        m = _make_market(
            ticker="KXBTCD-26JUN23H13-T1",
            close_time=_HOURLY_NOW + timedelta(seconds=1),
        )
        result = filter_markets([m], _hourly_config(), now=_HOURLY_NOW)
        assert [r.ticker for r in result] == [m.ticker]

    def test_hourly_dst_spring_forward_sanity(self) -> None:
        # 2026-03-08 06:45 UTC = 01:45 EST, spring-forward night
        now = datetime(2026, 3, 8, 6, 45, tzinfo=UTC)
        this_hour = _make_market(
            ticker="KXBTCD-26MAR08H02-T1",
            close_time=datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
        )
        next_hour = _make_market(
            ticker="KXBTCD-26MAR08H03-T2",
            close_time=datetime(2026, 3, 8, 8, 0, tzinfo=UTC),
        )
        result = filter_markets(
            [this_hour, next_hour], _hourly_config(), now=now,
        )
        assert [r.ticker for r in result] == ["KXBTCD-26MAR08H02-T1"]

    def test_days_until_now_param(self) -> None:
        # The injected clock is new public API of #736
        assert days_until(_NEXT_TOP, now=_HOURLY_NOW) == 0.5 / 24

    def test_hourly_price_band_no_side(self) -> None:
        # Band is in effective (NO-side) terms: NO price = 1 - YES price
        cfg = _hourly_config(side="no")
        close = _HOURLY_NOW + timedelta(minutes=29)
        markets = [
            # YES mid 0.90 -> NO 0.10 < 0.30 floor: rejected
            _make_market(
                ticker="KXBTCD-26JUN23H14-T1",
                yes_bid=0.88, yes_ask=0.92, close_time=close,
            ),
            # YES mid 0.50 -> NO 0.50: within 0.30-0.85
            _make_market(
                ticker="KXBTCD-26JUN23H14-T2",
                yes_bid=0.48, yes_ask=0.52, close_time=close,
            ),
            # YES mid 0.05 -> NO 0.95 > 0.85 ceiling: rejected
            _make_market(
                ticker="KXBTCD-26JUN23H14-T3",
                yes_bid=0.03, yes_ask=0.07, last_price=0.05, close_time=close,
            ),
        ]
        result = filter_markets(markets, cfg, now=_HOURLY_NOW)
        assert [r.ticker for r in result] == ["KXBTCD-26JUN23H14-T2"]

    def test_hourly_max_band_applies_over_flat_max(self) -> None:
        # The hourly and flat max defaults collide at 0.85 — raise the
        # flat max so a mutation ignoring the hourly ceiling is caught
        cfg = _hourly_config(side="no", max_market_price=0.95)
        m = _make_market(
            ticker="KXBTCD-26JUN23H14-T1",
            # YES mid 0.10 -> NO 0.90: inside flat 0.95, above hourly 0.85
            yes_bid=0.08, yes_ask=0.12, last_price=0.10,
            close_time=_HOURLY_NOW + timedelta(minutes=29),
        )
        assert filter_markets([m], cfg, now=_HOURLY_NOW) == []

    def test_hourly_band_boundaries_inclusive(self) -> None:
        # price < min or price > max: both bounds inclusive
        cfg = _hourly_config(side="no")
        close = _HOURLY_NOW + timedelta(minutes=29)
        markets = [
            # YES mid 0.70 -> NO 0.30: exactly the floor, passes
            _make_market(
                ticker="KXBTCD-26JUN23H14-T1",
                yes_bid=0.68, yes_ask=0.72, close_time=close,
            ),
            # YES mid 0.15 -> NO 0.85: exactly the ceiling, passes
            _make_market(
                ticker="KXBTCD-26JUN23H14-T2",
                yes_bid=0.13, yes_ask=0.17, last_price=0.15, close_time=close,
            ),
        ]
        result = filter_markets(markets, cfg, now=_HOURLY_NOW)
        assert sorted(r.ticker for r in result) == [
            "KXBTCD-26JUN23H14-T1", "KXBTCD-26JUN23H14-T2",
        ]

    def test_non_hourly_band_unchanged_when_hourly_enabled(self) -> None:
        # A non-hourly ticker keeps the flat band + min-days floor even
        # with hourly_series populated
        cfg = _hourly_config()
        sub_day = _make_market(
            ticker="KXCPIYOY-26MAR-T3.5",
            close_time=_HOURLY_NOW + timedelta(minutes=29),
        )
        cheap = _make_market(
            ticker="KXCPIYOY-26MAR-T4.0",
            yes_bid=0.38, yes_ask=0.42, last_price=0.40,
        )
        assert filter_markets([sub_day, cheap], cfg, now=_HOURLY_NOW) == []
