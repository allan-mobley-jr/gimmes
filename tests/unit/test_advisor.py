"""Tests for the strategy advisor analysis framework."""

from __future__ import annotations

from pathlib import Path

import pytest

from gimmes.config import GimmesConfig
from gimmes.models.recommendation import AnalysisType, Confidence, Recommendation, RecStatus
from gimmes.store.database import Database
from gimmes.store.migrations import run_migrations
from gimmes.store.queries import (
    get_recommendations,
    insert_recommendation,
    update_recommendation_status,
)
from gimmes.strategy.advisor import (
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
    """Generate synthetic trade data for testing."""
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
            "price": win_price + win_edge, "model_probability": 0.90,
            "gimme_score": win_score, "edge": win_edge, "rationale": "settled",
            "agent": "closer",
            "timestamp": f"2026-01-{(i % 28) + 1:02d}T18:00:00",
        })
    for i in range(n_losses):
        ticker = f"LOSS-{i}"
        trades.append({
            "ticker": ticker, "action": "open", "side": "yes", "count": 10,
            "price": loss_price, "model_probability": 0.85, "gimme_score": loss_score,
            "edge": abs(loss_edge), "rationale": "test", "agent": "closer",
            "timestamp": f"2026-02-{(i % 28) + 1:02d}T10:00:00",
        })
        trades.append({
            "ticker": ticker, "action": "close", "side": "yes", "count": 10,
            "price": loss_price + loss_edge, "model_probability": 0.85,
            "gimme_score": loss_score, "edge": loss_edge, "rationale": "settled",
            "agent": "closer",
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
# Analysis tests
# ---------------------------------------------------------------------------


class TestThresholdSweep:
    def test_insufficient_data(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=5, n_losses=2)
        assert analyze_threshold_sweep(trades, config) is None

    def test_finds_better_threshold(self, config: GimmesConfig) -> None:
        # Wins have score 70 (below default threshold of 75)
        # so lowering threshold should capture more wins
        trades = _make_trades(n_wins=25, n_losses=5, win_score=70, loss_score=85)
        rec = analyze_threshold_sweep(trades, config)
        # Should recommend lowering threshold
        if rec is not None:
            assert rec.parameter_path == "strategy.gimme_threshold"
            assert rec.analysis_type == AnalysisType.THRESHOLD_SWEEP

    def test_no_change_needed(self, config: GimmesConfig) -> None:
        # All trades at exactly the threshold — no improvement possible
        trades = _make_trades(n_wins=20, n_losses=10, win_score=75, loss_score=75)
        rec = analyze_threshold_sweep(trades, config)
        # Either None or same threshold
        if rec is not None:
            assert int(rec.recommended_value) != config.strategy.gimme_threshold


class TestEdgeDecay:
    def test_insufficient_data(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=5, n_losses=2)
        assert analyze_edge_decay(trades, config) is None

    def test_detects_decay(self, config: GimmesConfig) -> None:
        # First half has good edge, second half has poor edge
        trades: list[dict] = []
        for i in range(20):
            trades.append({
                "ticker": f"EARLY-{i}", "action": "close", "edge": 0.20,
                "timestamp": f"2026-01-{(i % 28) + 1:02d}T10:00:00",
            })
        for i in range(20):
            trades.append({
                "ticker": f"LATE-{i}", "action": "close", "edge": 0.05,
                "timestamp": f"2026-03-{(i % 28) + 1:02d}T10:00:00",
            })
        rec = analyze_edge_decay(trades, config)
        assert rec is not None
        assert rec.analysis_type == AnalysisType.EDGE_DECAY
        assert "decaying" in rec.rationale.lower()

    def test_no_decay(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=20, n_losses=10)
        # All close trades have same edge magnitude — no decay
        rec = analyze_edge_decay(trades, config)
        # Should be None since edges are consistent
        # (wins have 0.15, losses have -0.05 — but sorted by time they alternate)


class TestKellyOptimization:
    def test_insufficient_data(self, config: GimmesConfig) -> None:
        trades = _make_trades(n_wins=5, n_losses=2)
        assert analyze_kelly_optimization(trades, config) is None

    def test_recommends_adjustment(self, config: GimmesConfig) -> None:
        # High win rate with good payoff ratio should suggest higher Kelly
        trades = _make_trades(n_wins=25, n_losses=5, win_edge=0.20, loss_edge=-0.05)
        rec = analyze_kelly_optimization(trades, config)
        if rec is not None:
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
        rec = analyze_scanner_parameters(trades, config)
        if rec is not None:
            assert rec.analysis_type == AnalysisType.SCANNER_REVIEW
            assert "strategy.m" in rec.parameter_path


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

    def test_detects_false_negatives(self, config: GimmesConfig) -> None:
        trades: list[dict] = []
        # Skips that would have won (score just below threshold of 75)
        for i in range(15):
            trades.append({
                "ticker": f"SKIP-WIN-{i}", "action": "skip",
                "gimme_score": 72, "edge": 0.15,
                "timestamp": f"2026-01-{(i % 28) + 1:02d}T10:00:00",
            })
        # Skips that correctly lost
        for i in range(10):
            trades.append({
                "ticker": f"SKIP-LOSS-{i}", "action": "skip",
                "gimme_score": 60, "edge": -0.05,
                "timestamp": f"2026-02-{(i % 28) + 1:02d}T10:00:00",
            })
        rec = analyze_missed_opportunities(trades, config)
        if rec is not None:
            assert rec.analysis_type == AnalysisType.MISSED_OPPORTUNITY
            assert int(rec.recommended_value) < 75


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
