"""Unit tests for market scanner."""

from datetime import UTC, datetime, timedelta

from gimmes.config import GimmesConfig, Mode, ScannerConfig, StrategyConfig
from gimmes.models.market import Market, MarketStatus
from gimmes.strategy.scanner import filter_markets


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


class TestHourlyFilter:
    """#722: hourly-series tickers bypass the min-days floor and use the
    hourly price band; everything is inert while hourly_series is empty."""

    def test_hourly_bypasses_min_days_floor(self) -> None:
        m = _make_market(
            ticker="KXBTCD-26JUN23H14-T119999.99",
            close_time=datetime.now(UTC) + timedelta(minutes=29),
        )
        result = filter_markets([m], _hourly_config())
        assert [r.ticker for r in result] == [m.ticker]

    def test_inert_when_hourly_series_empty(self, config: GimmesConfig) -> None:
        # Identical market, default config: min-days floor rejects it
        m = _make_market(
            ticker="KXBTCD-26JUN23H14-T119999.99",
            close_time=datetime.now(UTC) + timedelta(minutes=29),
        )
        assert filter_markets([m], config) == []

    def test_hourly_respects_max_days(self) -> None:
        m = _make_market(
            ticker="KXBTCD-26DEC31H14-T119999.99",
            close_time=datetime.now(UTC) + timedelta(days=120),
        )
        assert filter_markets([m], _hourly_config()) == []

    def test_hourly_price_band_no_side(self) -> None:
        # Band is in effective (NO-side) terms: NO price = 1 - YES price
        cfg = _hourly_config(side="no")
        close = datetime.now(UTC) + timedelta(minutes=29)
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
        result = filter_markets(markets, cfg)
        assert [r.ticker for r in result] == ["KXBTCD-26JUN23H14-T2"]

    def test_hourly_max_band_applies_over_flat_max(self) -> None:
        # The hourly and flat max defaults collide at 0.85 — raise the
        # flat max so a mutation ignoring the hourly ceiling is caught
        cfg = _hourly_config(side="no", max_market_price=0.95)
        m = _make_market(
            ticker="KXBTCD-26JUN23H14-T1",
            # YES mid 0.10 -> NO 0.90: inside flat 0.95, above hourly 0.85
            yes_bid=0.08, yes_ask=0.12, last_price=0.10,
            close_time=datetime.now(UTC) + timedelta(minutes=29),
        )
        assert filter_markets([m], cfg) == []

    def test_hourly_band_boundaries_inclusive(self) -> None:
        # price < min or price > max: both bounds inclusive
        cfg = _hourly_config(side="no")
        close = datetime.now(UTC) + timedelta(minutes=29)
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
        result = filter_markets(markets, cfg)
        assert sorted(r.ticker for r in result) == [
            "KXBTCD-26JUN23H14-T1", "KXBTCD-26JUN23H14-T2",
        ]

    def test_non_hourly_band_unchanged_when_hourly_enabled(self) -> None:
        # A non-hourly ticker keeps the flat band + min-days floor even
        # with hourly_series populated
        cfg = _hourly_config()
        sub_day = _make_market(
            ticker="KXCPIYOY-26MAR-T3.5",
            close_time=datetime.now(UTC) + timedelta(minutes=29),
        )
        cheap = _make_market(
            ticker="KXCPIYOY-26MAR-T4.0",
            yes_bid=0.38, yes_ask=0.42, last_price=0.40,
        )
        assert filter_markets([sub_day, cheap], cfg) == []
