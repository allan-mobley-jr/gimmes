"""Unit tests for gimme scorer."""

from datetime import UTC, datetime, timedelta

from gimmes.config import GimmesConfig, Mode, ScannerConfig, StrategyConfig
from gimmes.models.gimme import ConfidenceSignal, GimmeCandidate
from gimmes.models.market import Market, Orderbook, OrderbookLevel
from gimmes.strategy.scorer import full_score, quick_score


class TestQuickScore:
    def test_high_quality_market(self, sample_market: Market, config: GimmesConfig) -> None:
        score = quick_score(sample_market, config)
        assert score > 0
        assert score <= 100

    def test_low_volume_market(self, config: GimmesConfig) -> None:
        market = Market(ticker="X", last_price=0.70, volume=10, volume_24h=5, open_interest=3)
        score = quick_score(market, config)
        assert score < 50  # Low volume/OI/spread but gets price + settlement points

    def test_excellent_market(self, config: GimmesConfig) -> None:
        market = Market(
            ticker="X", yes_bid=0.69, yes_ask=0.71, last_price=0.70,
            volume=50000, volume_24h=15000, open_interest=8000,
        )
        score = quick_score(market, config)
        assert score >= 60


class TestFullScore:
    def test_strong_candidate(self, config: GimmesConfig) -> None:
        candidate = GimmeCandidate(
            ticker="X",
            market_price=0.65,
            model_probability=0.92,
            edge=0.27,
            signals=[
                ConfidenceSignal(source="news", description="Strong signal", strength=0.9),
                ConfidenceSignal(source="data", description="Data confirms", strength=0.85),
                ConfidenceSignal(source="cross", description="Cross-platform", strength=0.8),
            ],
            research_memo="Clear settlement rules. No red flags.",
        )
        orderbook = Orderbook(
            ticker="X",
            yes_bids=[OrderbookLevel(price=0.65, quantity=300)],
        )
        score = full_score(candidate, orderbook, config)
        assert score.total > 50

    def test_weak_candidate(self, config: GimmesConfig) -> None:
        candidate = GimmeCandidate(
            ticker="X",
            market_price=0.70,
            model_probability=0.73,
            edge=0.03,
        )
        score = full_score(candidate, None, config)
        assert score.total < 50

    def test_settlement_red_flags(self, config: GimmesConfig) -> None:
        candidate = GimmeCandidate(
            ticker="X",
            market_price=0.65,
            model_probability=0.92,
            edge=0.27,
            signals=[
                ConfidenceSignal(source="news", description="Signal", strength=0.9),
                ConfidenceSignal(source="data", description="Data", strength=0.85),
            ],
            research_memo="Sole discretion clause. Carveout for death. Subjective determination.",
        )
        score = full_score(candidate, None, config)
        # Settlement penalty should lower the score
        assert score.settlement_clarity_score <= 30

    def test_bound_priced_no_side_scores_edge_zero(self) -> None:
        """#672: YES $1.00 → NO effective $0.00 — an unfillable order.
        edge_after_fees would say +88pp; the edge component must be 0
        (this exact shape scored 100 pre-fix, re-triggering Caddie
        research every cycle)."""
        no_config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="no"),
        )
        candidate = GimmeCandidate(
            ticker="KXCPIYOY", market_price=1.00,
            model_probability=0.88,
        )
        score = full_score(candidate, None, no_config)
        assert score.edge_size_score == 0.0

    def test_one_tick_inside_bound_scores_edge_zero(self) -> None:
        """YES $0.99 → NO effective $0.01 — within one tick, still
        untradeable (matches tradeable_edge and the validator)."""
        no_config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="no"),
        )
        candidate = GimmeCandidate(
            ticker="X", market_price=0.99, model_probability=0.88,
        )
        score = full_score(candidate, None, no_config)
        assert score.edge_size_score == 0.0

    def test_bound_determination_carried_in_memo(self) -> None:
        """#672: a zeroed-at-bound edge must stay distinguishable from
        a genuinely dead thesis — the memo carries the marker."""
        no_config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="no"),
        )
        bound = GimmeCandidate(
            ticker="X", market_price=1.00, model_probability=0.88,
            research_memo="thesis text",
        )
        score = full_score(bound, None, no_config)
        assert score.memo.startswith("[at-bound:")
        assert "thesis text" in score.memo

        normal = GimmeCandidate(
            ticker="X", market_price=0.30, model_probability=0.88,
            research_memo="thesis text",
        )
        score = full_score(normal, None, no_config)
        assert score.memo == "thesis text"

    def test_yes_side_floor_bound_scores_edge_zero(self) -> None:
        candidate = GimmeCandidate(
            ticker="X", market_price=0.01, model_probability=0.90,
        )
        config = GimmesConfig(mode=Mode.DRIVING_RANGE)
        score = full_score(candidate, None, config)
        assert score.edge_size_score == 0.0

    def test_no_side_uses_effective_price(self) -> None:
        """full_score should convert YES-denominated market_price for NO side."""
        no_config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="no"),
        )
        # YES price 0.30 → NO price 0.70; model_probability from NO perspective
        candidate = GimmeCandidate(
            ticker="X",
            market_price=0.30,
            model_probability=0.92,
            signals=[
                ConfidenceSignal(source="data", description="Signal", strength=0.9),
            ],
            research_memo="Clear rules.",
        )
        # YES bids at 0.30 → implied NO ask = 1 - 0.30 = 0.70
        orderbook = Orderbook(
            ticker="X",
            yes_bids=[OrderbookLevel(price=0.30, quantity=200)],
        )
        score = full_score(candidate, orderbook, no_config)
        # Edge: 0.92 - 0.71 (0.70 + maker fees) ≈ 0.21 → edge_score = 80.0 bucket
        assert score.edge_size_score == 80.0
        # Depth at NO price 0.70: YES bids at 0.30, implied ask = 0.70
        # depth_at_price(0.70, "no") checks YES bids where 1 - bid <= 0.70
        # → 1 - 0.30 = 0.70 <= 0.70 ✓ → depth = 200 → liq_score = 80.0
        assert score.liquidity_depth_score == 80.0


class TestHourlyTimeScore:
    """#722: hourly-series markets closing in <1 day score 70 (the
    designed entry window), not 20; everything else is unchanged."""

    @staticmethod
    def _candidate(ticker: str) -> GimmeCandidate:
        return GimmeCandidate(
            ticker=ticker,
            market_price=0.65,
            model_probability=0.80,
            edge=0.15,
        )

    @staticmethod
    def _hourly_config() -> GimmesConfig:
        return GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="no"),
            scanner=ScannerConfig(hourly_series=["KXBTCD"]),
        )

    def test_hourly_sub_day_time_score_70(self) -> None:
        ticker = "KXBTCD-26JUN23H14-T119999.99"
        market = Market(
            ticker=ticker,
            last_price=0.35,
            close_time=datetime.now(UTC) + timedelta(minutes=29),
        )
        score = full_score(
            self._candidate(ticker), None, self._hourly_config(), market=market,
        )
        assert score.time_to_resolution_score == 70.0

    def test_non_hourly_sub_day_time_score_20_unchanged(self, config: GimmesConfig) -> None:
        ticker = "KXBTCD-26JUN23H14-T119999.99"
        market = Market(
            ticker=ticker,
            last_price=0.35,
            close_time=datetime.now(UTC) + timedelta(minutes=29),
        )
        # Default config: hourly_series empty, the <1-day branch is inert
        score = full_score(self._candidate(ticker), None, config, market=market)
        assert score.time_to_resolution_score == 20.0

    def test_hourly_other_time_branches_unchanged(self) -> None:
        ticker = "KXBTCD-26JUN30H14-T119999.99"
        market = Market(
            ticker=ticker,
            last_price=0.35,
            close_time=datetime.now(UTC) + timedelta(days=7),
        )
        score = full_score(
            self._candidate(ticker), None, self._hourly_config(), market=market,
        )
        assert score.time_to_resolution_score == 100.0
