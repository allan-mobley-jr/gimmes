"""Tests for the strategy advisor analysis framework."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gimmes.config import GimmesConfig
from gimmes.models.recommendation import AnalysisType, Confidence, Recommendation, RecStatus
from gimmes.store.database import Database
from gimmes.store.queries import (
    get_recommendations,
    insert_recommendation,
    update_recommendation_status,
)
from gimmes.strategy.advisor import (
    _pair_closes,
    analyze_edge_decay,
    analyze_kelly_optimization,
    analyze_missed_opportunities,
    analyze_scanner_parameters,
    analyze_scoring_correlation,
    analyze_threshold_sweep,
    run_all_analyses,
)


@pytest.fixture
def config() -> GimmesConfig:
    return GimmesConfig()


def _make_trades(
    n_wins: int = 20,
    n_losses: int = 10,
    win_score: float = 80,
    loss_score: float = 75,
    win_edge: float = 0.15,
    loss_edge: float = -0.05,
    win_price: float = 0.70,
    loss_price: float = 0.65,
) -> list[dict]:
    """Generate production-shaped trade data.

    Close rows carry ZERO analytics — exactly how synthetic closes
    (settlement, reconcile) were written historically (#656). Outcome
    is expressed only through prices: win closes above the open price,
    loss closes below. Analyses must derive wins by pairing, never
    from close-row edge.
    """
    trades: list[dict] = []
    for i in range(n_wins):
        ticker = f"WIN-{i}"
        trades.append({
            "ticker": ticker, "action": "open", "side": "yes", "count": 10,
            "price": win_price, "model_probability": 0.90, "gimme_score": win_score,
            "edge": win_edge, "rationale": "test", "agent": "closer",
            "timestamp": f"2026-01-{(i % 28) + 1:02d}T10:00:00",
        })
        trades.append({
            "ticker": ticker, "action": "close", "side": "yes", "count": 10,
            "price": win_price + win_edge, "model_probability": 0.0,
            "gimme_score": 0.0, "edge": 0.0, "rationale": "settled",
            "agent": "settlement",
            "timestamp": f"2026-01-{(i % 28) + 1:02d}T18:00:00",
        })
    for i in range(n_losses):
        ticker = f"LOSS-{i}"
        trades.append({
            "ticker": ticker, "action": "open", "side": "yes", "count": 10,
            "price": loss_price, "model_probability": 0.85, "gimme_score": loss_score,
            "edge": loss_edge, "rationale": "test", "agent": "closer",
            "timestamp": f"2026-02-{(i % 28) + 1:02d}T10:00:00",
        })
        trades.append({
            "ticker": ticker, "action": "close", "side": "yes", "count": 10,
            "price": loss_price + loss_edge, "model_probability": 0.0,
            "gimme_score": 0.0, "edge": 0.0, "rationale": "settled",
            "agent": "settlement",
            "timestamp": f"2026-02-{(i % 28) + 1:02d}T18:00:00",
        })
    return trades


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestRecommendationModel:
    def test_defaults(self) -> None:
        rec = Recommendation(
            parameter_path="strategy.gimme_threshold",
            current_value="75",
            recommended_value="70",
        )
        assert rec.confidence == Confidence.MEDIUM
        assert rec.analysis_type == AnalysisType.THRESHOLD_SWEEP
        assert rec.supporting_data == "{}"

    def test_all_confidence_levels(self) -> None:
        for conf in Confidence:
            rec = Recommendation(
                parameter_path="test", current_value="1",
                recommended_value="2", confidence=conf,
            )
            assert rec.confidence == conf

    def test_all_analysis_types(self) -> None:
        for at in AnalysisType:
            rec = Recommendation(
                parameter_path="test", current_value="1",
                recommended_value="2", analysis_type=at,
            )
            assert rec.analysis_type == at

    def test_all_statuses(self) -> None:
        for status in RecStatus:
            assert isinstance(status.value, str)


# ---------------------------------------------------------------------------
# Pairing tests (#656)
# ---------------------------------------------------------------------------


def _pair_group(
    *,
    ticker: str = "KXCPI-26APR-T0.5",
    side: str = "yes",
    open_price: float = 0.60,
    close_price: float = 0.75,
    agent: str = "closer",
    resolved_outcome: str | None = None,
    score: float = 80.0,
) -> list[dict]:
    """One open + one close with optional resolution on the OPEN row
    (where Monitor's log-outcome lands, per pnl.py)."""
    return [
        {
            "ticker": ticker, "action": "open", "side": side, "count": 10,
            "price": open_price, "gimme_score": score, "edge": 0.15,
            "resolved_outcome": resolved_outcome, "agent": "closer",
            "timestamp": "2026-01-01T10:00:00",
        },
        {
            "ticker": ticker, "action": "close", "side": side, "count": 10,
            "price": close_price, "gimme_score": 0.0, "edge": 0.0,
            "resolved_outcome": None, "agent": agent,
            "timestamp": "2026-01-02T10:00:00",
        },
    ]


class TestPairCloses:
    def test_pnl_fallback_win_and_loss(self) -> None:
        trades = (
            _pair_group(ticker="A", open_price=0.60, close_price=0.75)
            + _pair_group(ticker="B", open_price=0.60, close_price=0.40)
        )
        paired = {r["ticker"]: r for r in _pair_closes(trades)}
        assert paired["A"]["won"] is True
        assert paired["A"]["realized_return"] == pytest.approx(0.15)
        assert paired["B"]["won"] is False
        assert paired["B"]["realized_return"] == pytest.approx(-0.20)

    def test_resolution_beats_realized_sign(self) -> None:
        """A stop-loss close at a paper loss on a market that resolved
        our way is still a WON prediction — resolution-first."""
        trades = _pair_group(
            open_price=0.60, close_price=0.40, resolved_outcome="yes",
        )
        [r] = _pair_closes(trades)
        assert r["won"] is True
        assert r["realized_return"] == pytest.approx(-0.20)

    def test_resolution_against_us_beats_positive_entry_edge(self) -> None:
        """Anti-inversion (#656): the close row carrying copied POSITIVE
        entry edge must not classify as a win when the market resolved
        against the side."""
        trades = _pair_group(
            open_price=0.60, close_price=0.75, resolved_outcome="no",
        )
        trades[1]["edge"] = 0.15  # entry edge copied onto the close
        [r] = _pair_closes(trades)
        assert r["won"] is False

    def test_reconcile_close_repriced_at_settlement(self) -> None:
        """Reconcile drift priced at a stale mark is repriced 1.0/0.0
        when the group's resolution is known (#653 semantics)."""
        trades = _pair_group(
            open_price=0.63, close_price=0.705, agent="reconcile",
            resolved_outcome="no",  # yes side lost
        )
        [r] = _pair_closes(trades)
        assert r["won"] is False
        assert r["realized_return"] == pytest.approx(-0.63)

    def test_drift_keeps_mark_when_group_has_settlement(self) -> None:
        """#663 mirror of calculate_pnl: a settlement close in the
        group means the reconcile drift row is a manual exit — it
        keeps its mark instead of being repriced to settlement."""
        trades = _pair_group(
            open_price=0.63, close_price=0.705, agent="reconcile",
            resolved_outcome="no",  # yes side lost
        )
        trades[1]["count"] = 4
        trades.append({
            "ticker": trades[0]["ticker"], "action": "close",
            "side": "yes", "count": 6, "price": 0.0,
            "gimme_score": 0.0, "edge": 0.0,
            "resolved_outcome": "no", "agent": "settlement",
            "timestamp": "2026-01-03T10:00:00",
        })
        results = _pair_closes(trades)
        drift = [
            r for r in results
            if r["timestamp"] == "2026-01-02T10:00:00"
        ]
        assert len(drift) == 1
        # 0.705 mark kept — NOT repriced to the 0.0 settlement value.
        assert drift[0]["realized_return"] == pytest.approx(0.705 - 0.63)

    def test_orphan_close_dropped(self) -> None:
        trades = [{
            "ticker": "GHOST", "action": "close", "side": "yes",
            "count": 10, "price": 0.9, "timestamp": "2026-01-01T10:00:00",
        }]
        assert _pair_closes(trades) == []

    def test_size_up_rolls_into_avg_cost(self) -> None:
        trades = [
            {
                "ticker": "S", "action": "open", "side": "yes", "count": 10,
                "price": 0.50, "gimme_score": 70.0,
                "timestamp": "2026-01-01T10:00:00",
            },
            {
                "ticker": "S", "action": "size_up", "side": "yes",
                "count": 10, "price": 0.70, "gimme_score": 72.0,
                "timestamp": "2026-01-02T10:00:00",
            },
            {
                "ticker": "S", "action": "close", "side": "yes", "count": 20,
                "price": 0.55, "timestamp": "2026-01-03T10:00:00",
            },
        ]
        [r] = _pair_closes(trades)
        # avg cost (0.50*10 + 0.70*10)/20 = 0.60 → realized −0.05
        assert r["realized_return"] == pytest.approx(-0.05)
        assert r["won"] is False
        assert r["entry_score"] == 72.0

    def test_entry_analytics_captured(self) -> None:
        [r] = _pair_closes(_pair_group(score=85.0, open_price=0.62))
        assert r["entry_score"] == 85.0
        assert r["entry_price"] == 0.62


class TestLessonNotBlind:
    """The dead-loop regression (#656): production-shaped data — close
    rows with zeroed analytics — must still produce recommendations."""

    def test_run_all_analyses_produces_a_recommendation(
        self, config: GimmesConfig,
    ) -> None:
        # Wins score 70 (below default threshold 75), losses score 85:
        # the sweep must discover the better threshold from PAIRED
        # outcomes despite every close row carrying edge=0/score=0.
        trades = _make_trades(
            n_wins=25, n_losses=10, win_score=70, loss_score=85,
        )
        recs = run_all_analyses(trades, [], config)
        assert len(recs) >= 1
        # Pin the specific analyses (all four fire on this data) so a
        # single-analysis regression can't hide behind the others.
        types = {r.analysis_type for r in recs}
        assert AnalysisType.THRESHOLD_SWEEP in types
        assert AnalysisType.KELLY_OPTIMIZATION in types
        assert AnalysisType.SCANNER_REVIEW in types

    def test_not_all_losses_under_zeroed_close_edge(
        self, config: GimmesConfig,
    ) -> None:
        """The original bug: close-row `edge > 0` classified every
        trade a loss. Pairing must see the actual mix."""
        paired = _pair_closes(_make_trades(n_wins=20, n_losses=10))
        wins = sum(1 for r in paired if r["won"])
        assert wins == 20
        assert len(paired) - wins == 10


# ---------------------------------------------------------------------------
# Analysis tests
# ---------------------------------------------------------------------------


class TestThresholdSweep:
    def test_insufficient_data(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=5, n_losses=2)
        assert analyze_threshold_sweep(trades, config) is None

    def test_finds_better_threshold(self, config: GimmesConfig) -> None:
        # Wins have score 70 (below default threshold of 75)
        # so lowering threshold should capture more wins. Hard assert:
        # an `if rec is not None` guard let a per-analysis revert to
        # close-row edge classification survive (#656 review).
        trades = _make_trades(n_wins=25, n_losses=5, win_score=70, loss_score=85)
        rec = analyze_threshold_sweep(trades, config)
        assert rec is not None
        assert rec.parameter_path == "strategy.gimme_threshold"
        assert rec.analysis_type == AnalysisType.THRESHOLD_SWEEP

    def test_no_change_needed(self, config: GimmesConfig) -> None:
        # All trades at exactly the threshold — no improvement possible
        trades = _make_trades(n_wins=20, n_losses=10, win_score=75, loss_score=75)
        rec = analyze_threshold_sweep(trades, config)
        # Same score for wins and losses means no threshold can improve win rate
        assert rec is None


class TestEdgeDecay:
    def test_insufficient_data(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=5, n_losses=2)
        assert analyze_edge_decay(trades, config) is None

    @staticmethod
    def _paired_trades(
        prefix: str, n: int, month: int, realized: float,
    ) -> list[dict]:
        """n open/close pairs whose per-contract realized return is
        `realized` — decay must be visible from PAIRED returns, not
        close-row edge (which is zeroed in production, #656)."""
        trades: list[dict] = []
        for i in range(n):
            day = (i % 28) + 1
            trades.append({
                "ticker": f"{prefix}-{i}", "action": "open", "side": "yes",
                "count": 10, "price": 0.50, "gimme_score": 80.0,
                "timestamp": f"2026-{month:02d}-{day:02d}T10:00:00",
            })
            trades.append({
                "ticker": f"{prefix}-{i}", "action": "close", "side": "yes",
                "count": 10, "price": 0.50 + realized, "edge": 0.0,
                "timestamp": f"2026-{month:02d}-{day:02d}T18:00:00",
            })
        return trades

    def test_detects_decay(self, config: GimmesConfig) -> None:
        # First half realizes +0.20/contract, second half +0.05
        trades = (
            self._paired_trades("EARLY", 20, 1, 0.20)
            + self._paired_trades("LATE", 20, 3, 0.05)
        )
        rec = analyze_edge_decay(trades, config)
        assert rec is not None
        assert rec.analysis_type == AnalysisType.EDGE_DECAY
        assert "decaying" in rec.rationale.lower()

    def test_no_decay(self, config: GimmesConfig) -> None:
        # Consistent realized return across both halves — no decay
        trades = (
            self._paired_trades("CONSISTENT", 20, 1, 0.15)
            + self._paired_trades("STEADY", 20, 2, 0.15)
        )
        rec = analyze_edge_decay(trades, config)
        assert rec is None


class TestKellyOptimization:
    def test_insufficient_data(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=5, n_losses=2)
        assert analyze_kelly_optimization(trades, config) is None

    def test_recommends_adjustment(self, config: GimmesConfig) -> None:
        # High win rate with good payoff ratio should suggest higher
        # Kelly. Hard assert — see TestThresholdSweep note (#656).
        trades = _make_trades(n_wins=25, n_losses=5, win_edge=0.20, loss_edge=-0.05)
        rec = analyze_kelly_optimization(trades, config)
        assert rec is not None
        assert rec.parameter_path == "sizing.kelly_fraction"
        assert rec.analysis_type == AnalysisType.KELLY_OPTIMIZATION
        assert float(rec.recommended_value) > 0
        assert float(rec.recommended_value) <= 0.50

    def test_no_wins(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=0, n_losses=25)
        assert analyze_kelly_optimization(trades, config) is None


class TestScannerParameters:
    def test_insufficient_data(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=5, n_losses=2)
        assert analyze_scanner_parameters(trades, config) is None

    def test_with_enough_data(self, config: GimmesConfig) -> None:
        trades = _make_trades(
            n_wins=20, n_losses=15,
            win_price=0.72, loss_price=0.58,
        )
        # Hard assert — see TestThresholdSweep note (#656).
        rec = analyze_scanner_parameters(trades, config)
        assert rec is not None
        assert rec.analysis_type == AnalysisType.SCANNER_REVIEW
        assert "strategy.m" in rec.parameter_path


class TestSideAwareOutcomes:
    """#668: outcome maps key by (ticker, side) — both sides of a
    ticker stay distinct, and multiple partial closes of one position
    aggregate any-loss = loss instead of last-wins."""

    def _both_sides(self, ticker: str = "BOTH") -> list[dict]:
        """A yes-side WIN then a no-side LOSS on the same ticker.
        Ticker-only last-wins would mark BOTH opens as losses."""
        return [
            {
                "ticker": ticker, "action": "open", "side": "yes",
                "count": 10, "price": 0.72, "gimme_score": 85.0,
                "edge": 0.15, "agent": "closer",
                "timestamp": "2026-03-01T10:00:00",
            },
            {
                "ticker": ticker, "action": "close", "side": "yes",
                "count": 10, "price": 0.90, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-03-01T18:00:00",
            },
            {
                "ticker": ticker, "action": "open", "side": "no",
                "count": 10, "price": 0.58, "gimme_score": 85.0,
                "edge": 0.15, "agent": "closer",
                "timestamp": "2026-03-02T10:00:00",
            },
            {
                "ticker": ticker, "action": "close", "side": "no",
                "count": 10, "price": 0.30, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-03-02T18:00:00",
            },
        ]

    def test_threshold_sweep_keeps_sides_distinct(
        self, config: GimmesConfig,
    ) -> None:
        trades = _make_trades(
            n_wins=25, n_losses=5, win_score=70, loss_score=85,
        ) + self._both_sides()
        rec = analyze_threshold_sweep(trades, config)
        assert rec is not None
        sweep = {
            row["threshold"]: row
            for row in json.loads(rec.supporting_data)
        }
        # At threshold 85: the 5 baseline losses + BOTH's two opens.
        # The yes-side win must survive the no-side loss.
        assert sweep[85]["trades_taken"] == 7
        assert sweep[85]["wins"] == 1

    def test_threshold_sweep_partial_closes_any_loss(
        self, config: GimmesConfig,
    ) -> None:
        """Loss tranche first, win tranche last — last-wins would call
        the position a win; any-loss keeps it a loss."""
        trades = _make_trades(
            n_wins=25, n_losses=5, win_score=70, loss_score=85,
        ) + [
            {
                "ticker": "PARTIAL", "action": "open", "side": "yes",
                "count": 10, "price": 0.50, "gimme_score": 85.0,
                "edge": 0.15, "agent": "closer",
                "timestamp": "2026-03-01T10:00:00",
            },
            {
                "ticker": "PARTIAL", "action": "close", "side": "yes",
                "count": 5, "price": 0.20, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-03-01T12:00:00",
            },
            {
                "ticker": "PARTIAL", "action": "close", "side": "yes",
                "count": 5, "price": 0.70, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-03-01T18:00:00",
            },
        ]
        rec = analyze_threshold_sweep(trades, config)
        assert rec is not None
        sweep = {
            row["threshold"]: row
            for row in json.loads(rec.supporting_data)
        }
        # 5 baseline losses + PARTIAL at threshold 85 — no wins.
        assert sweep[85]["trades_taken"] == 6
        assert sweep[85]["wins"] == 0

    def test_scanner_parameters_partial_closes_any_loss(
        self, config: GimmesConfig,
    ) -> None:
        """Any-loss must hold in the scanner too: the PARTIAL open's
        price lands in the loser bucket even though its last (winning)
        tranche would put it in the winners under last-wins."""
        trades = _make_trades(
            n_wins=20, n_losses=15, win_price=0.72, loss_price=0.58,
        ) + [
            {
                "ticker": "PARTIAL", "action": "open", "side": "yes",
                "count": 10, "price": 0.50, "gimme_score": 85.0,
                "edge": 0.15, "agent": "closer",
                "timestamp": "2026-03-01T10:00:00",
            },
            {
                "ticker": "PARTIAL", "action": "close", "side": "yes",
                "count": 5, "price": 0.20, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-03-01T12:00:00",
            },
            {
                "ticker": "PARTIAL", "action": "close", "side": "yes",
                "count": 5, "price": 0.70, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-03-01T18:00:00",
            },
        ]
        rec = analyze_scanner_parameters(trades, config)
        assert rec is not None
        # 20 baseline winners; 15 baseline losers + PARTIAL.
        assert "n=20" in rec.rationale
        assert "n=16" in rec.rationale

    def test_scanner_parameters_keep_sides_distinct(
        self, config: GimmesConfig,
    ) -> None:
        trades = _make_trades(
            n_wins=20, n_losses=15, win_price=0.72, loss_price=0.58,
        ) + self._both_sides()
        rec = analyze_scanner_parameters(trades, config)
        assert rec is not None
        # 20 baseline winners + BOTH's yes win; 15 losers + no loss.
        assert "n=21" in rec.rationale
        assert "n=16" in rec.rationale


class TestScoringCorrelation:
    def test_returns_none_without_component_data(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=30, n_losses=20)
        candidates = [{"ticker": "TEST", "gimme_score": 80}]
        assert analyze_scoring_correlation(trades, candidates, config) is None

    def test_returns_none_with_no_candidates(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=30, n_losses=20)
        assert analyze_scoring_correlation(trades, [], config) is None


class TestMissedOpportunities:
    def test_insufficient_skips(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=20, n_losses=10)
        assert analyze_missed_opportunities(trades, config) is None

    @staticmethod
    def _real_skips(n_wins: int = 15, n_losses: int = 10) -> list[dict]:
        """Production-shaped skips: price and prob recorded (#657)."""
        trades: list[dict] = []
        # Skips that would have won (score just below threshold of 75)
        for i in range(n_wins):
            trades.append({
                "ticker": f"SKIP-WIN-{i}", "action": "skip",
                "gimme_score": 72, "edge": 0.15, "price": 0.65,
                "model_probability": 0.85,
                "timestamp": f"2026-01-{(i % 28) + 1:02d}T10:00:00",
            })
        # Skips that correctly lost
        for i in range(n_losses):
            trades.append({
                "ticker": f"SKIP-LOSS-{i}", "action": "skip",
                "gimme_score": 60, "edge": -0.05, "price": 0.60,
                "model_probability": 0.55,
                "timestamp": f"2026-02-{(i % 28) + 1:02d}T10:00:00",
            })
        return trades

    @staticmethod
    def _degenerate_skips(n: int) -> list[dict]:
        """The #657 rows: no probability, no price recorded."""
        return [{
            "ticker": f"DEGEN-{i}", "action": "skip",
            "gimme_score": 0, "edge": 0.0, "price": 0.0,
            "model_probability": 0.0,
            "timestamp": f"2026-03-{(i % 28) + 1:02d}T10:00:00",
        } for i in range(n)]

    def test_detects_false_negatives(self, config: GimmesConfig) -> None:
        # Hard assert — this data fires the analysis (25 skips, 60%
        # false-negative rate, near-misses averaging 72).
        rec = analyze_missed_opportunities(self._real_skips(), config)
        assert rec is not None
        assert rec.analysis_type == AnalysisType.MISSED_OPPORTUNITY
        assert int(rec.recommended_value) < 75

    def test_degenerate_skips_excluded_from_denominator(
        self, config: GimmesConfig,
    ) -> None:
        """#657: zero-prob/zero-price rows must not dilute the
        false-negative rate or the sample gate — the analysis output
        is identical with or without 40 degenerate rows mixed in,
        and the exclusion is visible in supporting_data."""
        clean = analyze_missed_opportunities(self._real_skips(), config)
        polluted = analyze_missed_opportunities(
            self._real_skips() + self._degenerate_skips(40), config,
        )
        assert clean is not None
        assert polluted is not None
        assert polluted.recommended_value == clean.recommended_value
        clean_data = json.loads(clean.supporting_data)
        polluted_data = json.loads(polluted.supporting_data)
        # The 40 junk rows dilute the old code to fnr 15/65 = 0.231;
        # excluded, the rate stays 0.6.
        assert polluted_data["false_negative_rate"] == pytest.approx(0.6)
        assert polluted_data["total_skips"] == clean_data["total_skips"]
        # The exclusion is auditable, not silent (#668 lesson).
        assert clean_data["excluded_degenerate"] == 0
        assert polluted_data["excluded_degenerate"] == 40

    def test_half_degenerate_rows_excluded_both_ways(
        self, config: GimmesConfig,
    ) -> None:
        """#670: a skip missing EITHER probability or price persists
        with edge = 0 (log-trade's normalization), so it can NEVER
        classify as a missed win — and a legacy prob-only row's
        fabricated constructor edge must not inflate the numerator.
        Both half-degenerate shapes are excluded."""
        prob_only = [{
            "ticker": f"PROB-{i}", "action": "skip", "gimme_score": 72,
            "edge": 0.85, "price": 0.0, "model_probability": 0.85,
            "timestamp": f"2026-04-{(i % 28) + 1:02d}T10:00:00",
        } for i in range(3)]
        price_only = [{
            "ticker": f"PRICE-{i}", "action": "skip", "gimme_score": 60,
            "edge": -0.05, "price": 0.60, "model_probability": 0.0,
            "timestamp": f"2026-05-{(i % 28) + 1:02d}T10:00:00",
        } for i in range(3)]
        rec = analyze_missed_opportunities(
            self._real_skips() + prob_only + price_only, config,
        )
        assert rec is not None
        data = json.loads(rec.supporting_data)
        assert data["total_skips"] == 25  # the real rows only
        assert data["excluded_degenerate"] == 6
        # The legacy fabricated edges (0.85) did not reach the rate.
        assert data["false_negative_rate"] == pytest.approx(0.6)

    def test_price_only_rows_do_not_dilute_the_rate(
        self, config: GimmesConfig,
    ) -> None:
        """#670 mirror of the degenerate-dilution test: N price-only
        rows leave the recommendation and rate untouched."""
        price_only = [{
            "ticker": f"PRICE-{i}", "action": "skip", "gimme_score": 60,
            "edge": 0.0, "price": 0.60, "model_probability": 0.0,
            "timestamp": f"2026-05-{(i % 28) + 1:02d}T10:00:00",
        } for i in range(15)]
        clean = analyze_missed_opportunities(self._real_skips(), config)
        polluted = analyze_missed_opportunities(
            self._real_skips() + price_only, config,
        )
        assert clean is not None
        assert polluted is not None
        assert polluted.recommended_value == clean.recommended_value
        data = json.loads(polluted.supporting_data)
        assert data["false_negative_rate"] == pytest.approx(0.6)
        assert data["excluded_degenerate"] == 15

    def test_degenerate_rows_do_not_satisfy_the_gate(
        self, config: GimmesConfig,
    ) -> None:
        """19 real skips + 40 degenerate: the old code passed the
        MIN_SKIPS_AUDIT gate on the raw count (59 >= 20); after
        exclusion the real sample (19) is honestly insufficient."""
        rec = analyze_missed_opportunities(
            self._real_skips(n_wins=12, n_losses=7)
            + self._degenerate_skips(40),
            config,
        )
        assert rec is None

    def test_non_entry_reason_skips_excluded_despite_analytics(
        self, config: GimmesConfig,
    ) -> None:
        """A failed close, a tooling casualty, or a held position is
        never a missed ENTRY — every non-entry reason stays out of
        the audit even when the row carries full analytics
        (#657 review, #670)."""
        from gimmes.strategy.advisor import NON_ENTRY_SKIP_REASONS

        reasons = tuple(sorted(NON_ENTRY_SKIP_REASONS))
        non_entry = [{
            "ticker": f"NONENTRY-{i}", "action": "skip",
            "gimme_score": 90, "edge": 0.30, "price": 0.65,
            "model_probability": 0.95,
            "reason": reasons[i % len(reasons)],
            "timestamp": f"2026-06-{(i % 28) + 1:02d}T10:00:00",
        } for i in range(12)]
        clean = analyze_missed_opportunities(self._real_skips(), config)
        polluted = analyze_missed_opportunities(
            self._real_skips() + non_entry, config,
        )
        assert clean is not None
        assert polluted is not None
        data = json.loads(polluted.supporting_data)
        # The phantom "missed entries" (edge 0.30) did not inflate
        # missed_wins or the denominator.
        assert data["false_negative_rate"] == pytest.approx(0.6)
        assert data["excluded_non_entry"] == 12

    def test_degenerate_skips_alone_are_insufficient(
        self, config: GimmesConfig,
    ) -> None:
        """40 degenerate rows carry no signal — below the audit gate
        after exclusion."""
        rec = analyze_missed_opportunities(
            self._degenerate_skips(40), config,
        )
        assert rec is None


class TestRunAllAnalyses:
    def test_returns_list(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=25, n_losses=10)
        recs = run_all_analyses(trades, [], config)
        assert isinstance(recs, list)
        for rec in recs:
            assert isinstance(rec, Recommendation)

    def test_empty_trades(self, config: GimmesConfig) -> None:
        recs = run_all_analyses([], [], config)
        assert recs == []


# ---------------------------------------------------------------------------
# Database query tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    """Create a temporary database with schema + migrations."""
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


class TestRecommendationQueries:
    async def test_insert_and_get(self, db: Database) -> None:
        rec = Recommendation(
            parameter_path="strategy.gimme_threshold",
            current_value="75",
            recommended_value="70",
            confidence=Confidence.HIGH,
            analysis_type=AnalysisType.THRESHOLD_SWEEP,
            rationale="Test recommendation",
        )
        row_id = await insert_recommendation(db, rec)
        assert row_id > 0

        rows = await get_recommendations(db)
        assert len(rows) == 1
        assert rows[0]["parameter_path"] == "strategy.gimme_threshold"
        assert rows[0]["confidence"] == "high"
        assert rows[0]["status"] == "pending"

    async def test_filter_by_status(self, db: Database) -> None:
        rec = Recommendation(
            parameter_path="test.param",
            current_value="1",
            recommended_value="2",
        )
        row_id = await insert_recommendation(db, rec)
        await update_recommendation_status(db, row_id, "implemented")

        pending = await get_recommendations(db, status="pending")
        assert len(pending) == 0

        implemented = await get_recommendations(db, status="implemented")
        assert len(implemented) == 1

    async def test_filter_by_parameter(self, db: Database) -> None:
        for param in ["strategy.gimme_threshold", "sizing.kelly_fraction"]:
            await insert_recommendation(db, Recommendation(
                parameter_path=param,
                current_value="1",
                recommended_value="2",
            ))

        rows = await get_recommendations(db, parameter="sizing.kelly_fraction")
        assert len(rows) == 1
        assert rows[0]["parameter_path"] == "sizing.kelly_fraction"

    async def test_update_status_with_outcome(self, db: Database) -> None:
        rec = Recommendation(
            parameter_path="test.param",
            current_value="1",
            recommended_value="2",
        )
        row_id = await insert_recommendation(db, rec)
        await update_recommendation_status(
            db, row_id, "implemented",
            outcome="Win rate improved by 3pp",
        )

        rows = await get_recommendations(db)
        assert rows[0]["status"] == "implemented"
        assert rows[0]["outcome"] == "Win rate improved by 3pp"
        assert rows[0]["outcome_measured_at"] != ""

    async def test_update_with_github_url(self, db: Database) -> None:
        rec = Recommendation(
            parameter_path="test.param",
            current_value="1",
            recommended_value="2",
        )
        row_id = await insert_recommendation(db, rec)
        await update_recommendation_status(
            db, row_id, "pending",
            github_issue_url="https://github.com/example/issues/1",
        )

        rows = await get_recommendations(db)
        assert rows[0]["github_issue_url"] == "https://github.com/example/issues/1"


class TestMigrationV4:
    async def test_recommendations_table_exists(self, db: Database) -> None:
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='recommendations'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_schema_version_is_4(self, db: Database) -> None:
        cursor = await db.conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = await cursor.fetchone()
        assert row[0] >= 4


def test_non_entry_reasons_single_source_of_truth() -> None:
    """#670 review: the CLI's --reason gate and the advisor's audit
    exclusion must never drift — the CLI aliases the advisor set."""
    from gimmes import cli
    from gimmes.strategy.advisor import NON_ENTRY_SKIP_REASONS

    assert cli._NON_ENTRY_REASONS is NON_ENTRY_SKIP_REASONS
    assert NON_ENTRY_SKIP_REASONS <= cli._SKIP_REASONS


class TestLifecycleOutcomes:
    """#686: flat-book re-entries are separate lifecycles — an old
    losing round trip can never ratchet a later winning re-entry to a
    loss, and a still-open re-entry inherits nothing."""

    def _lifecycle_trades(self) -> list[dict]:
        """Lifecycle 0: open/close at a loss. Lifecycle 1 (same
        ticker/side, flat book): open/close at a win."""
        return [
            {
                "ticker": "KXRETRY", "action": "open", "side": "no",
                "count": 10, "price": 0.60, "gimme_score": 85.0,
                "edge": 0.1, "agent": "closer",
                "timestamp": "2026-03-01T10:00:00",
            },
            {
                "ticker": "KXRETRY", "action": "close", "side": "no",
                "count": 10, "price": 0.30, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-03-01T18:00:00",
            },
            {
                "ticker": "KXRETRY", "action": "open", "side": "no",
                "count": 10, "price": 0.55, "gimme_score": 85.0,
                "edge": 0.1, "agent": "closer",
                "timestamp": "2026-04-01T10:00:00",
            },
            {
                "ticker": "KXRETRY", "action": "close", "side": "no",
                "count": 10, "price": 0.90, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-04-01T18:00:00",
            },
        ]

    def test_pair_closes_emits_lifecycle_indices(self) -> None:
        paired = _pair_closes(self._lifecycle_trades())
        assert [r["lifecycle"] for r in paired] == [0, 1]
        assert [r["won"] for r in paired] == [False, True]

    def test_partial_close_does_not_increment_lifecycle(self) -> None:
        """A partial close (book never flat) then more entries stays
        one lifecycle."""
        trades = [
            {
                "ticker": "KXP", "action": "open", "side": "no",
                "count": 10, "price": 0.50, "gimme_score": 80.0,
                "edge": 0.1, "agent": "closer",
                "timestamp": "2026-03-01T10:00:00",
            },
            {
                "ticker": "KXP", "action": "close", "side": "no",
                "count": 4, "price": 0.70, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-03-01T12:00:00",
            },
            {
                "ticker": "KXP", "action": "size_up", "side": "no",
                "count": 5, "price": 0.55, "gimme_score": 82.0,
                "edge": 0.1, "agent": "closer",
                "timestamp": "2026-03-01T14:00:00",
            },
            {
                "ticker": "KXP", "action": "close", "side": "no",
                "count": 11, "price": 0.80, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-03-01T18:00:00",
            },
        ]
        paired = _pair_closes(trades)
        assert [r["lifecycle"] for r in paired] == [0, 0]

    def test_threshold_sweep_counts_lifecycles_independently(
        self, config: GimmesConfig,
    ) -> None:
        """The ratchet fix: lifecycle 0 loses, lifecycle 1 wins — the
        sweep must count one loss AND one win (pre-#686: two losses)."""
        trades = _make_trades(
            n_wins=25, n_losses=5, win_score=70, loss_score=85,
        ) + self._lifecycle_trades()
        rec = analyze_threshold_sweep(trades, config)
        assert rec is not None
        sweep = {
            row["threshold"]: row
            for row in json.loads(rec.supporting_data)
        }
        # At threshold 85: 5 baseline losses + KXRETRY's TWO
        # lifecycles (one loss, one win).
        assert sweep[85]["trades_taken"] == 7
        assert sweep[85]["wins"] == 1

    def test_still_open_reentry_inherits_nothing(
        self, config: GimmesConfig,
    ) -> None:
        """The inheritance fix: a closed losing lifecycle plus a
        currently-open re-entry (no close) yields exactly ONE scored
        entry — the dangling open contributes nothing."""
        trades = _make_trades(
            n_wins=25, n_losses=5, win_score=70, loss_score=85,
        ) + self._lifecycle_trades()[:2] + [{
            "ticker": "KXRETRY", "action": "open", "side": "no",
            "count": 10, "price": 0.55, "gimme_score": 85.0,
            "edge": 0.1, "agent": "closer",
            "timestamp": "2026-05-01T10:00:00",  # still open
        }]
        rec = analyze_threshold_sweep(trades, config)
        assert rec is not None
        sweep = {
            row["threshold"]: row
            for row in json.loads(rec.supporting_data)
        }
        # 5 baseline losses + ONE closed KXRETRY lifecycle; the open
        # re-entry is not scored (pre-#686 it inherited the loss).
        assert sweep[85]["trades_taken"] == 6
        assert sweep[85]["wins"] == 0

    def test_scanner_counts_lifecycles_independently(
        self, config: GimmesConfig,
    ) -> None:
        trades = _make_trades(
            n_wins=20, n_losses=15, win_price=0.72, loss_price=0.58,
        ) + self._lifecycle_trades()
        rec = analyze_scanner_parameters(trades, config)
        assert rec is not None
        # 20 winners + lifecycle 1's win; 15 losers + lifecycle 0's
        # loss.
        assert "n=21" in rec.rationale
        assert "n=16" in rec.rationale


class TestAnalysisWindow:
    """#686: the since cutoff drops paired closes and skips before it,
    post-pairing — an in-window close whose open predates the window
    still prices correctly."""

    def test_pair_closes_since_filters_post_walk(self) -> None:
        trades = [
            {
                "ticker": "KXW", "action": "open", "side": "no",
                "count": 10, "price": 0.50, "gimme_score": 80.0,
                "edge": 0.1, "agent": "closer",
                "timestamp": "2026-01-01T10:00:00",  # out of window
            },
            {
                "ticker": "KXW", "action": "close", "side": "no",
                "count": 10, "price": 0.90, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-06-01T10:00:00",  # in window
            },
        ]
        paired = _pair_closes(trades, since="2026-05-01T00:00:00")
        assert len(paired) == 1
        # Anti-orphan: priced against the OUT-OF-WINDOW open.
        assert paired[0]["realized_return"] == pytest.approx(0.40)
        assert paired[0]["entry_price"] == pytest.approx(0.50)

    def test_out_of_window_closes_dropped(self) -> None:
        trades = [
            {
                "ticker": "KXOLD", "action": "open", "side": "no",
                "count": 10, "price": 0.50, "gimme_score": 80.0,
                "edge": 0.1, "agent": "closer",
                "timestamp": "2026-01-01T10:00:00",
            },
            {
                "ticker": "KXOLD", "action": "close", "side": "no",
                "count": 10, "price": 0.90, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-01-02T10:00:00",
            },
        ]
        assert _pair_closes(trades, since="2026-05-01T00:00:00") == []
        assert len(_pair_closes(trades)) == 1  # default: all-time

    def test_missed_opportunities_windows_skips(
        self, config: GimmesConfig,
    ) -> None:
        """Skips before the cutoff drop out of the FNR denominator."""
        recent = analyze_missed_opportunities(
            TestMissedOpportunities._real_skips(), config,
        )
        windowed = analyze_missed_opportunities(
            TestMissedOpportunities._real_skips(), config,
            since="2099-01-01T00:00:00",
        )
        assert recent is not None
        assert windowed is None  # everything out of window → gated


class TestLifecycleEntryAttribution:
    """#686 review: the sweep/scanner bucket a lifecycle by its
    OPENING entry, not the most recent size_up before the close."""

    def test_lifecycle_entry_fields_are_the_opening_entry(self) -> None:
        from gimmes.strategy.advisor import _lifecycle_outcomes

        trades = [
            {
                "ticker": "KXS", "action": "open", "side": "no",
                "count": 10, "price": 0.50, "gimme_score": 80.0,
                "edge": 0.1, "agent": "closer",
                "timestamp": "2026-03-01T10:00:00",
            },
            {
                "ticker": "KXS", "action": "size_up", "side": "no",
                "count": 5, "price": 0.55, "gimme_score": 92.0,
                "edge": 0.1, "agent": "closer",
                "timestamp": "2026-03-01T12:00:00",
            },
            {
                "ticker": "KXS", "action": "close", "side": "no",
                "count": 15, "price": 0.80, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-03-01T18:00:00",
            },
        ]
        outcomes = _lifecycle_outcomes(_pair_closes(trades))
        o = outcomes[("KXS", "no", 0)]
        # The OPEN's score/price, not the size_up's (92.0/0.55).
        assert o["entry_score"] == 80.0
        assert o["entry_price"] == pytest.approx(0.50)

    def test_straddling_lifecycle_windows_whole_not_tranches(
        self,
    ) -> None:
        """#686 review: a lifecycle with a pre-cutoff losing tranche
        and an in-window winning final close must keep BOTH tranches
        (whole-lifecycle windowing) — per-tranche filtering would
        strip the loss from the any-loss AND, biasing win rates UP."""
        from gimmes.strategy.advisor import _lifecycle_outcomes

        trades = [
            {
                "ticker": "KXSTR", "action": "open", "side": "no",
                "count": 10, "price": 0.50, "gimme_score": 80.0,
                "edge": 0.1, "agent": "closer",
                "timestamp": "2026-01-01T10:00:00",
            },
            {  # losing partial, BEFORE the cutoff
                "ticker": "KXSTR", "action": "close", "side": "no",
                "count": 5, "price": 0.20, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-01-02T10:00:00",
            },
            {  # winning final close, AFTER the cutoff
                "ticker": "KXSTR", "action": "close", "side": "no",
                "count": 5, "price": 0.90, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-06-01T10:00:00",
            },
        ]
        paired = _pair_closes(trades, since="2026-05-01T00:00:00")
        assert len(paired) == 2  # whole lifecycle survives
        outcomes = _lifecycle_outcomes(paired)
        assert outcomes[("KXSTR", "no", 0)]["won"] is False  # any-loss

    def test_same_day_mixed_formats_pair_correctly(self) -> None:
        """PR #700 review: the pairing sort normalizes space->T — a
        same-day legacy close after an ISO open must walk AFTER it
        (raw string sort puts ' 10:00' before 'T09:00', orphaning the
        close and silently shrinking the analysis sample)."""
        trades = [
            {
                "ticker": "KXMIX", "action": "open", "side": "no",
                "count": 10, "price": 0.50, "gimme_score": 80.0,
                "edge": 0.1, "agent": "closer",
                "timestamp": "2026-06-01T09:00:00",
            },
            {
                "ticker": "KXMIX", "action": "close", "side": "no",
                "count": 10, "price": 0.90, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-06-01 10:00:00",  # legacy format
            },
        ]
        paired = _pair_closes(trades)
        assert len(paired) == 1
        assert paired[0]["realized_return"] == pytest.approx(0.40)

    def test_space_format_close_retained_by_window(self) -> None:
        """#680 lesson applied to the window filter: legacy
        space-format timestamps normalize before comparison."""
        trades = [
            {
                "ticker": "KXSP", "action": "open", "side": "no",
                "count": 10, "price": 0.50, "gimme_score": 80.0,
                "edge": 0.1, "agent": "closer",
                "timestamp": "2026-05-20T10:00:00",
            },
            {
                "ticker": "KXSP", "action": "close", "side": "no",
                "count": 10, "price": 0.90, "gimme_score": 0.0,
                "edge": 0.0, "agent": "closer",
                "timestamp": "2026-06-01 10:00:00",  # legacy format
            },
        ]
        paired = _pair_closes(trades, since="2026-05-01T00:00:00")
        assert len(paired) == 1

    def test_run_all_analyses_threads_since(
        self, config: GimmesConfig,
    ) -> None:
        """#686: a far-future cutoff must yield NO recommendations
        (everything windowed out) while the unwindowed call
        recommends — pins the since threading through every lambda."""
        from gimmes.strategy.advisor import run_all_analyses

        trades = _make_trades(
            n_wins=25, n_losses=5, win_score=70, loss_score=85,
        )
        unwindowed = run_all_analyses(trades, [], config)
        windowed = run_all_analyses(
            trades, [], config, since="2099-01-01T00:00:00",
        )
        assert unwindowed  # sanity: data produces recommendations
        assert windowed == []
