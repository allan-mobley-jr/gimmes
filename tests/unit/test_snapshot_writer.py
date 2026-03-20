"""Unit tests for periodic snapshot writer."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import gimmes.clubhouse.server as server_mod
from gimmes.clubhouse.models import PortfolioResponse, RiskResponse
from gimmes.clubhouse.server import _SNAPSHOT_INTERVAL, _maybe_write_snapshot
from gimmes.store.database import Database
from gimmes.store.migrations import run_migrations


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    """Create a temporary database with schema."""
    path = tmp_path / "test.db"
    async with Database(path) as db:
        await run_migrations(db)
    return path


@pytest.fixture(autouse=True)
def _reset_snapshot_state():
    """Reset module-level snapshot state between tests."""
    server_mod._last_snapshot_time = None
    yield
    server_mod._last_snapshot_time = None


def _portfolio(balance: float = 9500.0) -> PortfolioResponse:
    return PortfolioResponse(
        balance=balance,
        portfolio_value=500.0,
        total_equity=10000.0,
        daily_pnl=50.0,
        total_pnl=100.0,
    )


def _risk(position_count: int = 3) -> RiskResponse:
    return RiskResponse(position_count=position_count)


class TestMaybeWriteSnapshot:
    async def test_writes_snapshot_on_first_call(self, db_path: Path) -> None:
        await _maybe_write_snapshot(
            db_path, _portfolio(), _risk(), logging.getLogger("test"),
        )

        async with Database(db_path) as db:
            cursor = await db.conn.execute("SELECT COUNT(*) FROM snapshots")
            row = await cursor.fetchone()
            assert row[0] == 1

    async def test_skips_before_interval(self, db_path: Path) -> None:
        server_mod._last_snapshot_time = datetime.now() - timedelta(minutes=1)

        await _maybe_write_snapshot(
            db_path, _portfolio(), _risk(), logging.getLogger("test"),
        )

        async with Database(db_path) as db:
            cursor = await db.conn.execute("SELECT COUNT(*) FROM snapshots")
            row = await cursor.fetchone()
            assert row[0] == 0

    async def test_writes_after_interval(self, db_path: Path) -> None:
        server_mod._last_snapshot_time = (
            datetime.now() - _SNAPSHOT_INTERVAL - timedelta(seconds=1)
        )

        await _maybe_write_snapshot(
            db_path, _portfolio(), _risk(), logging.getLogger("test"),
        )

        async with Database(db_path) as db:
            cursor = await db.conn.execute("SELECT COUNT(*) FROM snapshots")
            row = await cursor.fetchone()
            assert row[0] == 1

    async def test_skips_zero_balance(self, db_path: Path) -> None:
        await _maybe_write_snapshot(
            db_path, _portfolio(balance=0), _risk(), logging.getLogger("test"),
        )

        async with Database(db_path) as db:
            cursor = await db.conn.execute("SELECT COUNT(*) FROM snapshots")
            row = await cursor.fetchone()
            assert row[0] == 0

    async def test_snapshot_values_match_portfolio(self, db_path: Path) -> None:
        portfolio = _portfolio()
        risk = _risk(position_count=5)

        await _maybe_write_snapshot(
            db_path, portfolio, risk, logging.getLogger("test"),
        )

        async with Database(db_path) as db:
            cursor = await db.conn.execute("SELECT * FROM snapshots LIMIT 1")
            row = await cursor.fetchone()
            assert row["balance"] == pytest.approx(9500.0)
            assert row["portfolio_value"] == pytest.approx(500.0)
            assert row["total_equity"] == pytest.approx(10000.0)
            assert row["open_position_count"] == 5
            assert row["daily_pnl"] == pytest.approx(50.0)
            assert row["total_pnl"] == pytest.approx(100.0)
