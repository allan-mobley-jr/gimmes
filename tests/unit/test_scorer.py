"""Unit tests for gimme scorer."""

from gimmes.config import GimmesConfig
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
