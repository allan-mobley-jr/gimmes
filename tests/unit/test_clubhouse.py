"""Tests for the Clubhouse dashboard server and data layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from gimmes.clubhouse.data import (
    get_activity,
    get_candidates,
    get_change_fingerprint,
    get_config_data,
    get_errors_data,
    get_metrics,
    get_portfolio,
    get_positions,
    get_recommendations_data,
    get_risk,
    get_status,
    get_trades,
)
from gimmes.clubhouse.models import (
    ConfigResponse,
    MarketDetailResponse,
    MetricsResponse,
    PortfolioResponse,
    RecommendationItem,
    StatusResponse,
)
from gimmes.clubhouse.server import _find_port, app, run_standalone
from gimmes.paper.schema import PAPER_SCHEMA_SQL
from gimmes.store.database import Database
from gimmes.store.migrations import run_migrations


@pytest.fixture(autouse=True)
def _reset_config_cache(monkeypatch, tmp_path):
    """Reset the clubhouse data module's config cache and use isolated config."""
    import gimmes.clubhouse.data as data_mod
    import gimmes.config as config_mod
    monkeypatch.setenv("GIMMES_MODE", "driving_range")
    monkeypatch.setattr(config_mod, "GIMMES_HOME", tmp_path)
    data_mod._cached_config = None
    data_mod._kalshi_client = None
    yield
    data_mod._cached_config = None
    data_mod._kalshi_client = None


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    """Create a temporary database with schema + migrations applied."""
    path = tmp_path / "test.db"
    async with Database(path) as db:
        await run_migrations(db)
        await db.conn.executescript(PAPER_SCHEMA_SQL)
        # Insert some test data
        await db.conn.execute(
            """INSERT INTO trades (ticker, action, side, count, price, model_probability,
               gimme_score, edge, rationale, agent) VALUES
               ('TEST-YES', 'open', 'yes', 10, 0.65, 0.92, 80, 0.27, 'test', 'closer')"""
        )
        await db.conn.execute(
            """INSERT INTO snapshots (balance, portfolio_value, total_equity,
               open_position_count, daily_pnl, total_pnl) VALUES
               (9500, 500, 10000, 1, 50, 100)"""
        )
        await db.conn.execute(
            """INSERT INTO candidates (ticker, title, market_price, model_probability,
               edge, gimme_score, research_memo) VALUES
               ('CAND-YES', 'Test Candidate', 0.70, 0.95, 0.25, 85, 'Looks good')"""
        )
        await db.conn.execute(
            """INSERT INTO activity_log (cycle, agent, phase, message) VALUES
               (1, 'scout', 'complete', 'Found 3 candidates')"""
        )
        await db.conn.execute(
            """INSERT INTO error_log (severity, category, error_code, component,
               agent, cycle, message) VALUES
               ('error', 'api_error', 'KALSHI_500', 'kalshi.client', 'scout', 1,
                'Internal server error from /markets endpoint')"""
        )
        await db.conn.execute(
            """INSERT INTO paper_balance (id, balance, starting_balance) VALUES
               (1, 9500, 10000)"""
        )
        await db.conn.execute(
            """INSERT INTO paper_positions (ticker, side, count, avg_price, cost_basis,
               market_price, unrealized_pnl, realized_pnl) VALUES
               ('TEST-YES', 'yes', 10, 0.65, 6.50, 0.70, 0.50, 0.0)"""
        )
        await db.conn.commit()
    return path


_INSERT_CANDIDATE_SQL = """INSERT INTO candidates (ticker, title, market_price,
    model_probability, edge, gimme_score, research_memo)
    VALUES (?, ?, ?, ?, ?, ?, ?)"""

_INSERT_POSITION_SQL = """INSERT INTO paper_positions
    (ticker, side, count, avg_price, cost_basis,
     market_price, unrealized_pnl, realized_pnl)
    VALUES (?, 'yes', 5, 0.50, 2.50, 0.55, 0.25, 0.0)"""


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    def test_status_defaults(self) -> None:
        s = StatusResponse()
        assert s.mode == "driving_range"
        assert not s.loop_active

    def test_portfolio_defaults(self) -> None:
        p = PortfolioResponse()
        assert p.balance == 0.0

    def test_config_response(self) -> None:
        c = ConfigResponse(mode="championship")
        assert c.mode == "championship"


# ---------------------------------------------------------------------------
# Data layer tests
# ---------------------------------------------------------------------------


class TestDataLayer:
    async def test_get_status(self, db_path: Path) -> None:
        status = await get_status(db_path)
        assert status.mode == "driving_range"
        assert isinstance(status, StatusResponse)

    async def test_get_portfolio(self, db_path: Path) -> None:
        portfolio = await get_portfolio(db_path)
        assert portfolio.balance == 9500.0
        assert portfolio.portfolio_value == pytest.approx(7.0)  # 10 * 0.70
        assert portfolio.total_equity == pytest.approx(9507.0)  # 9500 + 7.0

    async def test_get_positions(self, db_path: Path) -> None:
        positions = await get_positions(db_path)
        assert len(positions) == 1
        assert positions[0].ticker == "TEST-YES"
        assert positions[0].count == 10
        assert positions[0].market_value == pytest.approx(7.0)  # 10 * 0.70

    async def test_get_positions_returns_id(self, db_path: Path) -> None:
        """Position items include an id field for pagination."""
        positions = await get_positions(db_path)
        assert len(positions) == 1
        assert positions[0].id > 0

    async def test_get_positions_respects_limit(self, db_path: Path) -> None:
        """Limit parameter caps the number of returned positions."""
        async with Database(db_path) as db:
            for i in range(5):
                await db.conn.execute(_INSERT_POSITION_SQL, (f"POS-{i}",))
            await db.conn.commit()

        # 5 new + 1 from fixture = 6 total
        all_pos = await get_positions(db_path, limit=200)
        assert len(all_pos) == 6

        page = await get_positions(db_path, limit=3)
        assert len(page) == 3

    async def test_get_positions_before_id(self, db_path: Path) -> None:
        """before_id paginates through positions."""
        async with Database(db_path) as db:
            for i in range(5):
                await db.conn.execute(_INSERT_POSITION_SQL, (f"POS-{i}",))
            await db.conn.commit()

        page1 = await get_positions(db_path, limit=3)
        assert len(page1) == 3

        page2 = await get_positions(db_path, limit=3, before_id=page1[-1].id)
        assert len(page2) == 3

        # No overlap
        page1_ids = {p.id for p in page1}
        page2_ids = {p.id for p in page2}
        assert page1_ids.isdisjoint(page2_ids)

        # page2 ids are all smaller than page1's smallest
        assert all(p2_id < min(page1_ids) for p2_id in page2_ids)

    async def test_get_trades(self, db_path: Path) -> None:
        trades = await get_trades(db_path)
        assert len(trades) == 1
        assert trades[0].ticker == "TEST-YES"
        assert trades[0].action == "open"

    async def test_get_trades_includes_historical(self, db_path: Path) -> None:
        """Trades from previous days are included (no daily scope)."""
        async with Database(db_path) as db:
            await db.conn.execute(
                """INSERT INTO trades (ticker, action, side, count, price,
                   model_probability, gimme_score, edge, rationale, agent,
                   timestamp) VALUES
                   ('OLD-MKT', 'open', 'yes', 5, 0.50, 0.80, 70, 0.10,
                    'old', 'closer', datetime('now', '-1 day'))"""
            )
            await db.conn.commit()
        trades = await get_trades(db_path)
        tickers = [t.ticker for t in trades]
        assert "TEST-YES" in tickers
        assert "OLD-MKT" in tickers

    async def test_get_trades_before_id(self, db_path: Path) -> None:
        """before_id paginates through results in DESC order."""
        async with Database(db_path) as db:
            for i in range(4):
                await db.conn.execute(
                    f"""INSERT INTO trades (ticker, action, side, count, price,
                       model_probability, gimme_score, edge, rationale, agent)
                       VALUES ('PAGE-{i}', 'open', 'yes', 1, 0.50, 0.80, 70, 0.10,
                        'test', 'closer')"""
                )
            await db.conn.commit()
        # 5 total trades: TEST-YES + PAGE-0..3
        page1 = await get_trades(db_path, limit=3)
        assert len(page1) == 3
        assert page1[0].id > page1[1].id > page1[2].id
        page2 = await get_trades(db_path, limit=3, before_id=page1[-1].id)
        assert len(page2) == 2  # 5 - 3 = 2 remaining
        assert page2[0].id < page1[-1].id

    async def test_get_trades_since_id(self, db_path: Path) -> None:
        """since_id returns newly inserted items."""
        trades = await get_trades(db_path)
        max_id = trades[0].id
        newer = await get_trades(db_path, since_id=max_id)
        assert newer == []
        async with Database(db_path) as db:
            await db.conn.execute(
                """INSERT INTO trades (ticker, action, side, count, price,
                   model_probability, gimme_score, edge, rationale, agent)
                   VALUES ('NEW-MKT', 'open', 'yes', 1, 0.50, 0.80, 70, 0.10,
                    'new', 'closer')"""
            )
            await db.conn.commit()
        newer = await get_trades(db_path, since_id=max_id)
        assert len(newer) == 1
        assert newer[0].ticker == "NEW-MKT"
        assert newer[0].id > max_id

    async def test_get_trades_since_id_ignores_limit(self, db_path: Path) -> None:
        """since_id returns all new items regardless of limit."""
        trades = await get_trades(db_path)
        max_id = trades[0].id
        async with Database(db_path) as db:
            for i in range(5):
                await db.conn.execute(
                    f"""INSERT INTO trades (ticker, action, side, count, price,
                       model_probability, gimme_score, edge, rationale, agent)
                       VALUES ('DELTA-{i}', 'open', 'yes', 1, 0.50, 0.80, 70, 0.10,
                        'test', 'closer')"""
                )
            await db.conn.commit()
        newer = await get_trades(db_path, since_id=max_id, limit=2)
        assert len(newer) == 5

    async def test_get_trades_respects_limit(self, db_path: Path) -> None:
        """Limit parameter caps the result set."""
        async with Database(db_path) as db:
            for i in range(5):
                await db.conn.execute(
                    f"""INSERT INTO trades (ticker, action, side, count, price,
                       model_probability, gimme_score, edge, rationale, agent)
                       VALUES ('BULK-{i}', 'open', 'yes', 1, 0.50, 0.80, 70, 0.10,
                        'test', 'closer')"""
                )
            await db.conn.commit()
        trades = await get_trades(db_path, limit=3)
        assert len(trades) == 3

    async def test_get_candidates(self, db_path: Path) -> None:
        candidates = await get_candidates(db_path)
        assert len(candidates) == 1
        assert candidates[0].ticker == "CAND-YES"
        assert candidates[0].gimme_score == 85

    async def test_get_candidates_deduplicates_by_ticker(self, db_path: Path) -> None:
        """Only the most recent assessment per ticker is returned (#257)."""
        async with Database(db_path) as db:
            await db.conn.execute(
                """INSERT INTO candidates (ticker, title, market_price,
                   model_probability, edge, gimme_score, research_memo)
                   VALUES ('CAND-YES', 'Updated', 0.75, 0.98, 0.23, 92, 'New memo')"""
            )
            await db.conn.commit()
        candidates = await get_candidates(db_path)
        # Two rows for CAND-YES exist, but only the latest should be returned
        cand_yes = [c for c in candidates if c.ticker == "CAND-YES"]
        assert len(cand_yes) == 1
        assert cand_yes[0].gimme_score == 92
        assert cand_yes[0].title == "Updated"

    async def test_get_candidates_includes_cap_blocked(self, db_path: Path) -> None:
        """Cap-blocked flag is surfaced in CandidateItem (#276)."""
        async with Database(db_path) as db:
            await db.conn.execute(
                """INSERT INTO candidates (ticker, title, market_price,
                   model_probability, edge, gimme_score, research_memo,
                   cap_blocked)
                   VALUES ('CAP-MKT', 'Blocked', 0.70, 0.90, 0.20, 82, 'memo', 1)"""
            )
            await db.conn.commit()
        candidates = await get_candidates(db_path)
        cap_mkt = [c for c in candidates if c.ticker == "CAP-MKT"]
        assert len(cap_mkt) == 1
        assert cap_mkt[0].cap_blocked is True

    async def test_get_candidates_returns_id(self, db_path: Path) -> None:
        """CandidateItem exposes the row id (#355)."""
        candidates = await get_candidates(db_path)
        assert len(candidates) >= 1
        assert candidates[0].id > 0

    async def test_get_candidates_before_id(self, db_path: Path) -> None:
        """before_id paginates through deduplicated candidates (#355)."""
        async with Database(db_path) as db:
            for i in range(4):
                await db.conn.execute(
                    f"""INSERT INTO candidates (ticker, title, market_price,
                       model_probability, edge, gimme_score, research_memo,
                       scanned_at)
                       VALUES ('PAGE-{i}', 'Page {i}', 0.50, 0.80, 0.10, 70,
                       'memo', '2026-01-01 00:0{i + 1}:00')"""
                )
            await db.conn.commit()
        # 5 total unique tickers: CAND-YES + PAGE-0..3
        page1 = await get_candidates(db_path, limit=3)
        assert len(page1) == 3
        page2 = await get_candidates(db_path, limit=3, before_id=page1[-1].id)
        assert len(page2) == 2  # 5 - 3 = 2 remaining
        assert page2[0].id < page1[-1].id
        # No overlap
        page1_tickers = {c.ticker for c in page1}
        page2_tickers = {c.ticker for c in page2}
        assert page1_tickers.isdisjoint(page2_tickers)

    async def test_get_candidates_before_id_with_dedup(self, db_path: Path) -> None:
        """Cursor pagination still deduplicates by ticker (#355)."""
        async with Database(db_path) as db:
            # Insert two rows for the same ticker
            await db.conn.execute(
                """INSERT INTO candidates (ticker, title, market_price,
                   model_probability, edge, gimme_score, research_memo)
                   VALUES ('DUP-MKT', 'Old', 0.50, 0.80, 0.10, 70, 'old')"""
            )
            await db.conn.execute(
                """INSERT INTO candidates (ticker, title, market_price,
                   model_probability, edge, gimme_score, research_memo)
                   VALUES ('DUP-MKT', 'New', 0.55, 0.85, 0.15, 75, 'new')"""
            )
            await db.conn.commit()
        # Should still only have one entry for DUP-MKT across all pages
        all_items = await get_candidates(db_path, limit=100)
        dup_items = [c for c in all_items if c.ticker == "DUP-MKT"]
        assert len(dup_items) == 1
        assert dup_items[0].title == "New"

    async def test_get_candidates_excludes_open_positions(self, db_path: Path) -> None:
        """Candidates with an open paper position are hidden (#356)."""
        # TEST-YES already has a paper_position (from fixture). Add it as a candidate.
        async with Database(db_path) as db:
            await db.conn.execute(
                _INSERT_CANDIDATE_SQL,
                ("TEST-YES", "Positioned", 0.70, 0.95, 0.25, 85, "memo"),
            )
            await db.conn.commit()
        candidates = await get_candidates(db_path)
        tickers = {c.ticker for c in candidates}
        assert "TEST-YES" not in tickers  # excluded: has open position
        assert "CAND-YES" in tickers      # retained: no position

    async def test_get_candidates_includes_settled_position(self, db_path: Path) -> None:
        """A candidate whose position is settled (count=0) still appears (#356)."""
        async with Database(db_path) as db:
            # Insert a settled (count=0) paper position and a candidate for it
            await db.conn.execute(
                """INSERT INTO paper_positions (ticker, side, count, avg_price, cost_basis,
                   market_price, unrealized_pnl, realized_pnl)
                   VALUES ('SETTLED', 'yes', 0, 0.65, 0.0, 0.0, 0.0, 1.50)"""
            )
            await db.conn.execute(
                _INSERT_CANDIDATE_SQL,
                ("SETTLED", "Settled market", 0.99, 0.99, 0.01, 50, "memo"),
            )
            await db.conn.commit()
        candidates = await get_candidates(db_path)
        tickers = {c.ticker for c in candidates}
        assert "SETTLED" in tickers  # count=0, should NOT be excluded

    async def test_get_candidates_pagination_with_position_filter(
        self, db_path: Path,
    ) -> None:
        """Cursor pagination works correctly when positions are filtered (#356)."""
        async with Database(db_path) as db:
            for i in range(3):
                await db.conn.execute(
                    _INSERT_CANDIDATE_SQL,
                    (f"NOPOS-{i}", f"No Pos {i}", 0.50, 0.80, 0.10, 70, "memo"),
                )
            # Add a positioned candidate (TEST-YES already has a paper position)
            await db.conn.execute(
                _INSERT_CANDIDATE_SQL,
                ("TEST-YES", "Positioned", 0.70, 0.95, 0.25, 85, "memo"),
            )
            await db.conn.commit()
        # 4 unique candidate tickers (CAND-YES + NOPOS-0..2), TEST-YES excluded
        page1 = await get_candidates(db_path, limit=2)
        assert len(page1) == 2
        page2 = await get_candidates(db_path, limit=10, before_id=page1[-1].id)
        assert len(page2) == 2  # 4 - 2 = 2
        all_tickers = {c.ticker for c in page1} | {c.ticker for c in page2}
        assert "TEST-YES" not in all_tickers

    async def test_get_metrics(self, db_path: Path) -> None:
        metrics = await get_metrics(db_path)
        assert isinstance(metrics, MetricsResponse)

    async def test_get_risk(self, db_path: Path) -> None:
        risk = await get_risk(db_path)
        assert risk.position_count == 1
        assert risk.max_positions == 15
        # Daily P&L includes unrealized (no close trades, so realized = 0)
        assert risk.daily_pnl == pytest.approx(0.50)  # unrealized only
        # Positive P&L → loss pct stays 0
        assert risk.daily_loss_pct == 0.0

    async def test_get_risk_negative_pnl(self, db_path: Path) -> None:
        """Negative combined P&L triggers daily_loss_pct using bankroll."""
        async with Database(db_path) as db:
            # Partial close: sell 5 of 10 contracts at a loss (0.65→0.50)
            await db.conn.execute(
                """INSERT INTO trades (ticker, action, side, count, price,
                   model_probability, gimme_score, edge, rationale, agent)
                   VALUES ('TEST-YES', 'close', 'yes', 5, 0.50,
                   0.92, 80, 0.27, 'test', 'closer')"""
            )
            # Update position: 5 remaining with negative unrealized
            await db.conn.execute(
                """UPDATE paper_positions SET count = 5, unrealized_pnl = -1.0
                   WHERE ticker = 'TEST-YES'"""
            )
            await db.conn.commit()
        risk = await get_risk(db_path)
        # Realized: (0.50 - 0.65) * 5 = -0.75, Unrealized: -1.0
        # Total: -1.75
        assert risk.daily_pnl == pytest.approx(-1.75)
        # Loss pct = 1.75 / 5000 (default paper bankroll) = 0.00035
        assert risk.daily_loss_pct == pytest.approx(0.00035)

    async def test_get_activity(self, db_path: Path) -> None:
        activity = await get_activity(db_path)
        assert len(activity) == 1
        assert activity[0].agent == "scout"
        assert activity[0].cycle == 1

    async def test_get_activity_includes_historical(self, db_path: Path) -> None:
        """Activity from previous days is included (no daily scope)."""
        async with Database(db_path) as db:
            await db.conn.execute(
                """INSERT INTO activity_log (cycle, agent, phase, message, timestamp)
                   VALUES (0, 'monitor', 'start', 'old entry', datetime('now', '-1 day'))"""
            )
            await db.conn.commit()
        activity = await get_activity(db_path)
        assert len(activity) == 2
        messages = [a.message for a in activity]
        assert "Found 3 candidates" in messages
        assert "old entry" in messages

    async def test_get_activity_before_id(self, db_path: Path) -> None:
        """before_id paginates through results in DESC order."""
        async with Database(db_path) as db:
            for i in range(4):
                await db.conn.execute(
                    f"""INSERT INTO activity_log (cycle, agent, phase, message)
                       VALUES ({i}, 'scout', 'start', 'Page entry {i}')"""
                )
            await db.conn.commit()
        # 5 total: fixture + 4 new
        page1 = await get_activity(db_path, limit=3)
        assert len(page1) == 3
        assert page1[0].id > page1[1].id > page1[2].id
        page2 = await get_activity(db_path, limit=3, before_id=page1[-1].id)
        assert len(page2) == 2
        assert page2[0].id < page1[-1].id

    async def test_get_activity_since_id(self, db_path: Path) -> None:
        """since_id returns newly inserted items."""
        activity = await get_activity(db_path)
        max_id = activity[0].id
        newer = await get_activity(db_path, since_id=max_id)
        assert newer == []
        async with Database(db_path) as db:
            await db.conn.execute(
                """INSERT INTO activity_log (cycle, agent, phase, message)
                   VALUES (2, 'closer', 'complete', 'New activity')"""
            )
            await db.conn.commit()
        newer = await get_activity(db_path, since_id=max_id)
        assert len(newer) == 1
        assert newer[0].message == "New activity"
        assert newer[0].id > max_id

    async def test_get_activity_respects_limit(self, db_path: Path) -> None:
        """Limit parameter caps the result set."""
        async with Database(db_path) as db:
            for i in range(5):
                await db.conn.execute(
                    f"""INSERT INTO activity_log (cycle, agent, phase, message)
                       VALUES ({i}, 'scout', 'start', 'Entry {i}')"""
                )
            await db.conn.commit()
        activity = await get_activity(db_path, limit=3)
        assert len(activity) == 3

    async def test_get_errors_data(self, db_path: Path) -> None:
        errors = await get_errors_data(db_path)
        assert len(errors) == 1
        assert errors[0].severity == "error"
        assert errors[0].category == "api_error"
        assert errors[0].error_code == "KALSHI_500"
        assert errors[0].agent == "scout"
        assert not errors[0].resolved

    async def test_get_recommendations_data(self, db_path: Path) -> None:
        # Insert a pending recommendation
        async with Database(db_path) as db:
            await db.conn.execute(
                """INSERT INTO recommendations
                   (parameter_path, current_value, recommended_value,
                    confidence, analysis_type, rationale)
                   VALUES ('strategy.gimme_threshold', '75', '70',
                           'high', 'threshold_sweep', 'Better win rate at 70')"""
            )
            await db.conn.commit()

        recs = await get_recommendations_data(db_path)
        assert len(recs) == 1
        assert recs[0].parameter_path == "strategy.gimme_threshold"
        assert recs[0].current_value == "75"
        assert recs[0].recommended_value == "70"
        assert recs[0].confidence == "high"
        assert recs[0].status == "pending"

    async def test_get_recommendations_filters_non_pending(self, db_path: Path) -> None:
        async with Database(db_path) as db:
            await db.conn.execute(
                """INSERT INTO recommendations
                   (parameter_path, current_value, recommended_value,
                    confidence, analysis_type, rationale, status)
                   VALUES ('strategy.min_edge', '0.05', '0.04',
                           'medium', 'edge_decay', 'Edge shrinking', 'implemented')"""
            )
            await db.conn.commit()

        recs = await get_recommendations_data(db_path)
        assert len(recs) == 0

    async def test_get_recommendations_nonexistent_db(self, tmp_path: Path) -> None:
        bad_path = tmp_path / "nonexistent.db"
        recs = await get_recommendations_data(bad_path)
        assert recs == []

    async def test_get_errors_nonexistent_db(self, tmp_path: Path) -> None:
        bad_path = tmp_path / "nonexistent.db"
        errors = await get_errors_data(bad_path)
        assert errors == []

    async def test_get_config_data(self) -> None:
        config = await get_config_data()
        assert config.mode in ("driving_range", "championship")
        assert "gimme_threshold" in config.strategy

    async def test_get_change_fingerprint(self, db_path: Path) -> None:
        fp = await get_change_fingerprint(db_path)
        assert "|" in fp
        # Should be consistent
        fp2 = await get_change_fingerprint(db_path)
        assert fp == fp2

    async def test_nonexistent_db_returns_defaults(self, tmp_path: Path) -> None:
        bad_path = tmp_path / "nonexistent.db"
        status = await get_status(bad_path)
        assert status.mode == "driving_range"
        assert not status.loop_active

        positions = await get_positions(bad_path)
        assert positions == []


# ---------------------------------------------------------------------------
# Server helpers
# ---------------------------------------------------------------------------


class TestServerHelpers:
    def test_find_port_returns_available(self) -> None:
        port = _find_port(start=19191)
        assert port is not None
        assert port >= 19191

    def test_find_port_returns_none_when_all_taken(self) -> None:
        import socket

        # Bind a port to make it unavailable
        socks = []
        base = 19291
        for i in range(11):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", base + i))
            socks.append(s)

        try:
            result = _find_port(start=base, max_tries=11)
            assert result is None
        finally:
            for s in socks:
                s.close()


# ---------------------------------------------------------------------------
# FastAPI app tests
# ---------------------------------------------------------------------------


class TestRecommendationModel:
    def test_defaults(self) -> None:
        r = RecommendationItem()
        assert r.status == "pending"
        assert r.parameter_path == ""
        assert r.confidence == ""

    def test_full_item(self) -> None:
        r = RecommendationItem(
            id=1,
            parameter_path="strategy.gimme_threshold",
            current_value="75",
            recommended_value="70",
            confidence="high",
            analysis_type="threshold_sweep",
            rationale="Better win rate",
            status="pending",
        )
        assert r.parameter_path == "strategy.gimme_threshold"
        assert r.confidence == "high"


class TestRunStandalone:
    @pytest.fixture()
    def stub(self, monkeypatch):
        """Stub uvicorn so run_standalone returns immediately."""
        captured = {"timers": [], "server": None}

        class FakeTimer:
            def __init__(self, delay, fn, args=None, kwargs=None):
                self.delay = delay
                self.fn = fn
                self.daemon = False
                captured["timers"].append(self)

            def start(self):
                pass

        class FakeServer:
            def __init__(self, config):
                self.config = config
                self.handle_exit = None

            def run(self):
                captured["server"] = self

        monkeypatch.setattr("gimmes.clubhouse.server.threading.Timer", FakeTimer)
        monkeypatch.setattr("uvicorn.Config", lambda *a, **kw: None)
        monkeypatch.setattr("uvicorn.Server", FakeServer)
        return captured

    def test_opens_browser_by_default(self, stub) -> None:
        run_standalone(port=19391, open_browser=True)

        assert len(stub["timers"]) == 1
        assert stub["timers"][0].delay == 1.0
        assert stub["timers"][0].daemon is True

    def test_no_browser_flag(self, stub) -> None:
        run_standalone(port=19392, open_browser=False)

        assert stub["timers"] == []

    def test_handle_exit_overridden(self, stub) -> None:
        run_standalone(port=19393, open_browser=False)

        assert stub["server"] is not None
        assert stub["server"].handle_exit is not None

    def test_handle_exit_calls_os_exit(self, monkeypatch, stub) -> None:
        run_standalone(port=19394, open_browser=False)

        handler = stub["server"].handle_exit
        exit_codes: list[int] = []
        monkeypatch.setattr("os._exit", lambda code: exit_codes.append(code))
        import signal
        handler(signal.SIGINT, None)
        assert exit_codes == [0]


class TestFastAPIApp:
    def test_app_has_routes(self) -> None:
        routes = {r.path for r in app.routes}
        assert "/" in routes
        assert "/api/status" in routes
        assert "/api/portfolio" in routes
        assert "/api/positions" in routes
        assert "/api/trades" in routes
        assert "/api/candidates" in routes
        assert "/api/metrics" in routes
        assert "/api/risk" in routes
        assert "/api/activity" in routes
        assert "/api/errors" in routes
        assert "/api/recommendations" in routes
        assert "/api/config" in routes
        assert "/api/market/{ticker}" in routes
        assert "/api/stream" in routes


class TestMarketDetailResponse:
    def test_defaults(self) -> None:
        resp = MarketDetailResponse()
        assert resp.ticker == ""
        assert resp.close_time is None
        assert resp.volume == 0
        assert resp.open_interest == 0
        assert resp.subtitle == ""

    def test_construction(self) -> None:
        resp = MarketDetailResponse(
            ticker="TEST-YES",
            title="Will it rain?",
            subtitle="Weather",
            status="active",
            close_time="2026-04-01T12:00:00",
            volume=5000,
            volume_24h=1200,
            open_interest=800,
            yes_bid=0.65,
            yes_ask=0.67,
            last_price=0.66,
        )
        assert resp.ticker == "TEST-YES"
        assert resp.volume == 5000
        assert resp.open_interest == 800
        assert resp.close_time == "2026-04-01T12:00:00"


class TestGetMarketDetail:
    @pytest.fixture()
    def patch_kalshi(self, monkeypatch):
        """Patch the Kalshi client and get_market to return a configurable market."""
        import gimmes.clubhouse.data as data_mod

        captured_tickers: list[str] = []
        market_to_return: list = []  # single-element list used as mutable holder

        async def fake_get_market(_client, ticker):
            captured_tickers.append(ticker)
            return market_to_return[0]

        monkeypatch.setattr(data_mod, "_kalshi_client", object())
        monkeypatch.setattr("gimmes.kalshi.markets.get_market", fake_get_market)
        return {"set_market": lambda m: market_to_return.append(m) or None,
                "captured_tickers": captured_tickers,
                "data_mod": data_mod}

    @pytest.mark.asyncio
    async def test_get_market_detail(self, patch_kalshi) -> None:
        from datetime import UTC, datetime

        from gimmes.models.market import Market, MarketStatus

        patch_kalshi["set_market"](Market(
            ticker="TEST-YES",
            title="Will it rain?",
            subtitle="Weather",
            status=MarketStatus.ACTIVE,
            volume=5000,
            volume_24h=1200,
            open_interest=800,
            close_time=datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC),
            yes_bid=0.65,
            yes_ask=0.67,
            last_price=0.66,
        ))

        result = await patch_kalshi["data_mod"].get_market_detail("TEST-YES")
        assert result.ticker == "TEST-YES"
        assert result.volume == 5000
        assert result.open_interest == 800
        assert result.status == "active"
        assert result.close_time == "2026-04-01T12:00:00+00:00"
        assert result.subtitle == "Weather"
        assert patch_kalshi["captured_tickers"] == ["TEST-YES"]

    @pytest.mark.asyncio
    async def test_get_market_detail_no_close_time(self, patch_kalshi) -> None:
        from gimmes.models.market import Market, MarketStatus

        patch_kalshi["set_market"](Market(
            ticker="TEST-YES",
            status=MarketStatus.ACTIVE,
            close_time=None,
        ))

        result = await patch_kalshi["data_mod"].get_market_detail("TEST-YES")
        assert result.close_time is None

    @pytest.mark.asyncio
    async def test_get_market_detail_no_credentials(self, monkeypatch) -> None:
        import gimmes.clubhouse.data as data_mod

        monkeypatch.delenv("KALSHI_PROD_API_KEY", raising=False)
        monkeypatch.delenv("KALSHI_PROD_PRIVATE_KEY_PATH", raising=False)
        data_mod._cached_config = None

        with pytest.raises(ValueError, match="API key not set"):
            await data_mod.get_market_detail("TEST-YES")


class TestDashboardTradeWindow:
    """#680: a single 1000-row window across all actions let skip
    volume push the newest closes out — win_rate silently went stale.
    Per-action fetching (the #542 pattern) is immune."""

    @pytest.mark.asyncio
    async def test_get_metrics_survives_skip_flood(
        self, db_path: Path,
    ) -> None:
        async with Database(db_path) as db:
            await db.conn.executemany(
                """INSERT INTO trades (ticker, action, side, count,
                   price, model_probability, gimme_score, edge,
                   kelly_fraction, rationale, agent, timestamp)
                   VALUES (?, 'skip', 'no', 0, 0, 0, 0, 0, 0, 's',
                           'scout', ?)""",
                [
                    (f"SKIP-{i}", f"2026-01-01T{i % 24:02d}:00:00")
                    for i in range(1001)
                ],
            )
            await db.conn.execute(
                """INSERT INTO trades (ticker, action, side, count,
                   price, model_probability, gimme_score, edge,
                   kelly_fraction, rationale, agent, timestamp)
                   VALUES ('KXWIN', 'open', 'no', 10, 0.50, 0.8, 70,
                           0.1, 0.02, 'entry', 'closer',
                           '2026-06-01T10:00:00')""",
            )
            await db.conn.execute(
                """INSERT INTO trades (ticker, action, side, count,
                   price, model_probability, gimme_score, edge,
                   kelly_fraction, rationale, agent, timestamp)
                   VALUES ('KXWIN', 'close', 'no', 10, 0.90, 0, 0, 0,
                           0, 'win', 'closer', '2026-06-02T10:00:00')""",
            )
            await db.conn.commit()

        metrics = await get_metrics(db_path)
        # Pre-fix the window held only skips → win_rate 0.
        assert metrics.win_rate == 1.0


class TestSnapshotWindow:
    """#680 review: the snapshots fetch keeps the NEWEST 500 — the
    old ASC window froze equity/total_return at the oldest 500 after
    ~2 days of SSE uptime."""

    @pytest.mark.asyncio
    async def test_equity_reflects_newest_snapshot_past_window(
        self, db_path: Path,
    ) -> None:
        async with Database(db_path) as db:
            await db.conn.executemany(
                """INSERT INTO snapshots (balance, portfolio_value,
                   total_equity, timestamp)
                   VALUES (?, 0, ?, ?)""",
                [
                    (10_000.0, 10_000.0, f"2026-01-01T{i // 60:02d}:{i % 60:02d}:00")
                    for i in range(501)
                ],
            )
            # The single NEWEST snapshot carries a distinct equity.
            await db.conn.execute(
                """INSERT INTO snapshots (balance, portfolio_value,
                   total_equity, timestamp)
                   VALUES (99999.0, 0, 99999.0,
                           '2027-01-01T00:00:00')""",
            )
            await db.conn.commit()

        metrics = await get_metrics(db_path)
        # Pre-fix the ASC window dropped the newest row → stale curve.
        assert metrics.equity_curve[-1]["equity"] == pytest.approx(
            99999.0,
        )


class TestMixedTimestampFormats:
    """#680 review: legacy space-format rows must not outrank newer
    ISO rows (or vice versa) in the newest-N windows."""

    @pytest.mark.asyncio
    async def test_legacy_row_ordered_by_time_not_separator(
        self, db_path: Path,
    ) -> None:
        async with Database(db_path) as db:
            # Legacy-format snapshot NEWER than every ISO row: the
            # raw DESC sort would rank it LAST (' ' < 'T'); the
            # normalized sort keeps it as the curve's newest point.
            await db.conn.execute(
                """INSERT INTO snapshots (balance, portfolio_value,
                   total_equity, timestamp)
                   VALUES (77777.0, 0, 77777.0, '2027-06-01 00:00:00')""",
            )
            await db.conn.commit()

        metrics = await get_metrics(db_path)
        assert metrics.equity_curve[-1]["equity"] == pytest.approx(
            77777.0,
        )


class TestOrphanWarningQuiet:
    """#680: get_metrics runs per SSE client per fingerprint change —
    orphan-close warnings are demoted to debug on that path only."""

    @pytest.mark.asyncio
    async def test_get_metrics_silences_orphan_warnings(
        self, db_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        async with Database(db_path) as db:
            await db.conn.execute(
                """INSERT INTO trades (ticker, action, side, count,
                   price, model_probability, gimme_score, edge,
                   kelly_fraction, rationale, agent, timestamp)
                   VALUES ('KXORPHAN', 'close', 'no', 10, 0.90, 0, 0,
                           0, 0, 'orphan', 'closer',
                           '2026-06-02T10:00:00')""",
            )
            await db.conn.commit()

        with caplog.at_level(
            logging.DEBUG, logger="gimmes.reporting.pnl",
        ):
            await get_metrics(db_path)
        warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and "orphan close" in r.message
        ]
        assert warnings == []
        # Forensic trail kept at debug.
        assert any("orphan close" in r.message for r in caplog.records)


class TestRiskExcludesReconcileDrift:
    """#680: get_risk shares the canonical DAILY_PNL_SQL — reconcile
    drift is excluded from the dashboard's daily P&L (#622), matching
    the daily-loss trigger the loop reads."""

    @pytest.mark.asyncio
    async def test_get_risk_excludes_reconcile_drift(
        self, db_path: Path,
    ) -> None:
        from datetime import UTC, datetime

        from gimmes.store.queries import get_daily_pnl

        today = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        async with Database(db_path) as db:
            await db.conn.execute(
                f"""INSERT INTO trades (ticker, action, side, count,
                   price, model_probability, gimme_score, edge,
                   kelly_fraction, rationale, agent, timestamp)
                   VALUES ('KXDRIFT', 'open', 'no', 100, 0.50, 0.8,
                           70, 0.1, 0.02, 'entry', 'closer',
                           '{today}')""",
            )
            await db.conn.execute(
                f"""INSERT INTO trades (ticker, action, side, count,
                   price, model_probability, gimme_score, edge,
                   kelly_fraction, rationale, agent, timestamp)
                   VALUES ('KXDRIFT', 'close', 'no', 100, 0.10, 0, 0,
                           0, 0, 'drift', 'reconcile', '{today}')""",
            )
            # A real close with known P&L defeats the default-0
            # escape: get_risk swallows exceptions into daily_pnl=0,
            # which would vacuously equal an all-zero expectation.
            await db.conn.execute(
                f"""INSERT INTO trades (ticker, action, side, count,
                   price, model_probability, gimme_score, edge,
                   kelly_fraction, rationale, agent, timestamp)
                   VALUES ('KXREAL', 'open', 'no', 10, 0.50, 0.8, 70,
                           0.1, 0.02, 'entry', 'closer', '{today}')""",
            )
            await db.conn.execute(
                f"""INSERT INTO trades (ticker, action, side, count,
                   price, model_probability, gimme_score, edge,
                   kelly_fraction, rationale, agent, timestamp)
                   VALUES ('KXREAL', 'close', 'no', 10, 0.90, 0, 0, 0,
                           0, 'win', 'closer', '{today}')""",
            )
            await db.conn.commit()
            canonical = await get_daily_pnl(db)
            assert canonical == pytest.approx(4.0)  # (0.9-0.5)*10
            cursor = await db.conn.execute(
                # paper mode: get_risk reads paper_positions
                "SELECT COALESCE(SUM(unrealized_pnl), 0) AS u"
                " FROM paper_positions WHERE count > 0"
            )
            unrealized = float((await cursor.fetchone())["u"])

        risk = await get_risk(db_path)
        # Parity with the canonical query: realized component matches
        # get_daily_pnl exactly (drift excluded on both paths); the
        # dashboard adds unrealized on top.
        assert risk.daily_pnl == pytest.approx(canonical + unrealized)
        # Pre-fix the -$40 phantom drift loss landed here.
        assert risk.daily_pnl > -1.0


class TestSchemaVersionCheck:
    """#680: the read-only dashboard warns (never refuses) on a DB
    behind the writer's schema version."""

    def test_warning_on_old_db(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        import sqlite3

        from gimmes.clubhouse.server import _check_schema_version

        old_db = tmp_path / "old.db"
        conn = sqlite3.connect(old_db)
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (5)")
        conn.commit()
        conn.close()

        with caplog.at_level(logging.WARNING, logger="gimmes.clubhouse"):
            msg = _check_schema_version(old_db)
        assert msg is not None
        assert "v5" in msg
        assert "read-only" in msg
        # start_background discards the return — the LOG line is the
        # whole feature on that path.
        assert any(
            "read-only" in r.message and r.levelno >= logging.WARNING
            for r in caplog.records
        )

    def test_foreign_file_warns_not_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A file that OPENS but isn't a GIMMES DB gets one clear
        startup line, not silence (#680 review)."""
        import logging
        import sqlite3

        from gimmes.clubhouse.server import _check_schema_version

        foreign = tmp_path / "foreign.db"
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()

        with caplog.at_level(logging.WARNING, logger="gimmes.clubhouse"):
            assert _check_schema_version(foreign) is None
        assert any(
            "schema check failed" in r.message for r in caplog.records
        )

    def test_check_wired_into_both_entry_points(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Deleting the call sites must fail a test — the helper runs
        before _find_port in both entry points, so stubbing the port
        lookup short-circuits before uvicorn."""
        from gimmes.clubhouse import server as server_mod

        calls: list[str] = []
        monkeypatch.setattr(
            server_mod, "_check_schema_version",
            lambda p: calls.append(str(p)) or None,
        )
        monkeypatch.setattr(server_mod, "_find_port", lambda p: None)

        assert server_mod.start_background() is None
        assert len(calls) == 1

        with pytest.raises(RuntimeError):
            server_mod.run_standalone(open_browser=False)
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_silent_on_current_db(self, db_path: Path) -> None:
        from gimmes.clubhouse.server import _check_schema_version

        assert _check_schema_version(db_path) is None

    def test_silent_on_missing_db(self, tmp_path: Path) -> None:
        from gimmes.clubhouse.server import _check_schema_version

        assert _check_schema_version(tmp_path / "nope.db") is None


def test_latest_schema_version_constant_matches_migrations(
    tmp_path: Path,
) -> None:
    """Drift guard for the #680 constant: a new migration must bump
    LATEST_SCHEMA_VERSION."""
    import asyncio

    from gimmes.store.migrations import (
        LATEST_SCHEMA_VERSION,
        run_migrations,
    )

    async def _version() -> int:
        async with Database(tmp_path / "fresh.db") as db:
            return await run_migrations(db)

    assert asyncio.run(_version()) == LATEST_SCHEMA_VERSION
