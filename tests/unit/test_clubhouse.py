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
def _reset_config_cache(monkeypatch):
    """Reset the clubhouse data module's config cache and ensure driving_range mode."""
    import gimmes.clubhouse.data as data_mod
    monkeypatch.setenv("GIMMES_MODE", "driving_range")
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

    async def test_get_trades(self, db_path: Path) -> None:
        trades = await get_trades(db_path)
        assert len(trades) == 1
        assert trades[0].ticker == "TEST-YES"
        assert trades[0].action == "open"

    async def test_get_trades_excludes_yesterday(self, db_path: Path) -> None:
        """Daily-scoped query excludes trades from previous days (#256)."""
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
        assert len(trades) == 1
        assert trades[0].ticker == "TEST-YES"

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
            # Add a close trade with a loss: bought at 0.65, sold at 0.50
            await db.conn.execute(
                """INSERT INTO trades (ticker, action, side, count, price,
                   model_probability, gimme_score, edge, rationale, agent)
                   VALUES ('TEST-YES', 'close', 'yes', 10, 0.50,
                   0.92, 80, 0.27, 'test', 'closer')"""
            )
            # Update position to show negative unrealized on remaining
            await db.conn.execute(
                """UPDATE paper_positions SET unrealized_pnl = -2.0
                   WHERE ticker = 'TEST-YES'"""
            )
            await db.conn.commit()
        risk = await get_risk(db_path)
        # Realized: (0.50 - 0.65) * 10 = -1.50, Unrealized: -2.0
        # Total: -3.50
        assert risk.daily_pnl == pytest.approx(-3.50)
        # Loss pct = 3.50 / 500 (default bankroll) = 0.007
        assert risk.daily_loss_pct == pytest.approx(0.007)

    async def test_get_activity(self, db_path: Path) -> None:
        activity = await get_activity(db_path)
        assert len(activity) == 1
        assert activity[0].agent == "scout"
        assert activity[0].cycle == 1

    async def test_get_activity_excludes_yesterday(self, db_path: Path) -> None:
        """Daily-scoped query excludes activity from previous days."""
        async with Database(db_path) as db:
            await db.conn.execute(
                """INSERT INTO activity_log (cycle, agent, phase, message, timestamp)
                   VALUES (0, 'monitor', 'start', 'old entry', datetime('now', '-1 day'))"""
            )
            await db.conn.commit()
        activity = await get_activity(db_path)
        assert len(activity) == 1
        assert activity[0].message == "Found 3 candidates"

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
