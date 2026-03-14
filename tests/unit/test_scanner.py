"""Unit tests for market scanner."""

from datetime import UTC, datetime, timedelta

from gimmes.config import GimmesConfig
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
