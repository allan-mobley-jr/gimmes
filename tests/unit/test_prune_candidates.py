"""Tests for prune_candidates and get_position_tickers."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import (
    get_position_tickers,
    insert_candidate,
    insert_trade,
    prune_candidates,
    upsert_position,
)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """Create a temp database."""
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


async def _insert(db: Database, ticker: str, *, scanned_at: str | None = None) -> int:
    """Insert a minimal candidate, optionally overriding scanned_at."""
    row_id = await insert_candidate(
        db, ticker, f"Title {ticker}", 0.50, 0.80, 0.10, 70, "memo",
    )
    if scanned_at:
        await db.conn.execute(
            "UPDATE candidates SET scanned_at = ? WHERE id = ?",
            (scanned_at, row_id),
        )
        await db.conn.commit()
    return row_id


def _pos(ticker: str) -> Position:
    return Position(
        ticker=ticker, side="yes", count=10, avg_price=0.5,
        market_price=0.5, cost_basis=5.0,
    )


def _trade(ticker: str) -> TradeDecision:
    return TradeDecision(
        ticker=ticker, action=TradeDecision.Action.OPEN,
        side="yes", count=10, price=0.50,
    )


async def _create_paper_position(db: Database, ticker: str, count: int = 5) -> None:
    """Create the paper_positions table and insert a single row."""
    await db.conn.execute(
        """CREATE TABLE IF NOT EXISTS paper_positions (
            ticker TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'yes',
            count INTEGER NOT NULL DEFAULT 0,
            avg_price REAL NOT NULL DEFAULT 0,
            market_price REAL NOT NULL DEFAULT 0,
            cost_basis REAL NOT NULL DEFAULT 0,
            market_value REAL NOT NULL DEFAULT 0,
            unrealized_pnl REAL NOT NULL DEFAULT 0,
            realized_pnl REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (ticker, side)
        )"""
    )
    await db.conn.execute(
        "INSERT INTO paper_positions (ticker, count) VALUES (?, ?)",
        (ticker, count),
    )
    await db.conn.commit()


class TestPruneCandidates:
    async def test_prune_opened_positions(self, db):
        await _insert(db, "OPENED")
        await _insert(db, "REMAINING")

        counts = await prune_candidates(db, open_tickers={"OPENED"})

        assert counts["opened"] == 1
        cursor = await db.conn.execute("SELECT ticker FROM candidates")
        rows = await cursor.fetchall()
        assert {row["ticker"] for row in rows} == {"REMAINING"}

    async def test_prune_inactive_markets(self, db):
        await _insert(db, "CLOSED-MKT")
        await _insert(db, "ACTIVE-MKT")

        counts = await prune_candidates(
            db, open_tickers=set(), inactive_tickers={"CLOSED-MKT"},
        )

        assert counts["inactive"] == 1
        cursor = await db.conn.execute("SELECT ticker FROM candidates")
        rows = await cursor.fetchall()
        assert {row["ticker"] for row in rows} == {"ACTIVE-MKT"}

    async def test_prune_aged_out(self, db):
        await _insert(db, "OLD", scanned_at="2020-01-01 00:00:00")
        await _insert(db, "FRESH")

        counts = await prune_candidates(db, open_tickers=set())

        assert counts["aged_out"] == 1
        cursor = await db.conn.execute("SELECT ticker FROM candidates")
        rows = await cursor.fetchall()
        assert {row["ticker"] for row in rows} == {"FRESH"}

    async def test_prune_aged_out_custom_hours(self, db):
        # Fixed far-past timestamp for determinism — exceeds any max_age_hours
        await _insert(db, "RECENT", scanned_at="2020-06-01 00:00:00")

        counts = await prune_candidates(db, open_tickers=set(), max_age_hours=1)
        assert counts["aged_out"] == 1

    async def test_prune_stale_duplicates(self, db):
        await _insert(db, "DUP")
        id2 = await _insert(db, "DUP")

        counts = await prune_candidates(db, open_tickers=set())

        assert counts["duplicates"] == 1
        cursor = await db.conn.execute("SELECT id FROM candidates WHERE ticker = 'DUP'")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == id2  # newest survives

    async def test_prune_no_candidates(self, db):
        counts = await prune_candidates(db, open_tickers=set())
        assert all(v == 0 for v in counts.values())

    async def test_prune_no_matching_rules(self, db):
        await _insert(db, "SAFE")

        counts = await prune_candidates(db, open_tickers=set())

        assert all(v == 0 for v in counts.values())
        cursor = await db.conn.execute("SELECT COUNT(*) as cnt FROM candidates")
        row = await cursor.fetchone()
        assert row["cnt"] == 1

    async def test_prune_empty_open_tickers(self, db):
        await _insert(db, "A")
        counts = await prune_candidates(db, open_tickers=set())
        assert counts["opened"] == 0

    async def test_prune_inactive_tickers_none(self, db):
        await _insert(db, "A")
        counts = await prune_candidates(db, open_tickers=set(), inactive_tickers=None)
        assert counts["inactive"] == 0

    async def test_prune_counts_no_double_counting(self, db):
        """A ticker matching rule 1 (opened) should not also count as aged_out."""
        await _insert(db, "BOTH", scanned_at="2020-01-01 00:00:00")

        counts = await prune_candidates(db, open_tickers={"BOTH"})

        assert counts["opened"] == 1
        assert counts["aged_out"] == 0  # already deleted by rule 1

    async def test_prune_all_rules_combined(self, db):
        """All four rules in a single call with distinct candidates."""
        await _insert(db, "OPENED")
        await _insert(db, "INACTIVE")
        await _insert(db, "OLD", scanned_at="2020-01-01 00:00:00")
        await _insert(db, "DUP")
        await _insert(db, "DUP")  # duplicate
        await _insert(db, "SAFE")

        counts = await prune_candidates(
            db,
            open_tickers={"OPENED"},
            inactive_tickers={"INACTIVE"},
        )

        assert counts["opened"] == 1
        assert counts["inactive"] == 1
        assert counts["aged_out"] == 1
        assert counts["duplicates"] == 1
        cursor = await db.conn.execute("SELECT ticker FROM candidates")
        rows = await cursor.fetchall()
        assert {row["ticker"] for row in rows} == {"DUP", "SAFE"}

    async def test_prune_ticker_in_both_open_and_inactive(self, db):
        """A ticker in both open_tickers and inactive_tickers is only counted once."""
        await _insert(db, "OVERLAP")

        counts = await prune_candidates(
            db,
            open_tickers={"OVERLAP"},
            inactive_tickers={"OVERLAP"},
        )

        assert counts["opened"] == 1
        assert counts["inactive"] == 0  # already deleted by rule 1

    async def test_prune_opened_multiple_rows(self, db):
        """Multiple rows for the same opened ticker are all deleted."""
        await _insert(db, "MULTI")
        await _insert(db, "MULTI")
        await _insert(db, "MULTI")

        counts = await prune_candidates(db, open_tickers={"MULTI"})
        assert counts["opened"] == 3

    async def test_prune_invalid_max_age_raises(self, db):
        with pytest.raises(ValueError, match="max_age_hours must be >= 1"):
            await prune_candidates(db, open_tickers=set(), max_age_hours=0)


class TestGetPositionTickers:
    async def test_returns_open_tickers(self, db):
        await upsert_position(db, _pos("OPEN-1"))
        await upsert_position(db, _pos("OPEN-2"))

        result = await get_position_tickers(db)
        assert result == {"OPEN-1", "OPEN-2"}

    async def test_excludes_closed_positions(self, db):
        pos = Position(
            ticker="CLOSED", side="yes", count=0, avg_price=0.5,
            market_price=0.5, cost_basis=0,
        )
        await upsert_position(db, pos)

        result = await get_position_tickers(db)
        assert result == set()

    async def test_custom_table(self, db):
        await _create_paper_position(db, "PAPER-1")

        result = await get_position_tickers(db, table="paper_positions")
        assert result == {"PAPER-1"}

    async def test_invalid_table_raises(self, db):
        with pytest.raises(ValueError, match="Invalid position table"):
            await get_position_tickers(db, table="evil_table")


class TestGetPositionTickersStalenessWarning:
    async def test_warns_when_stale(self, db, caplog):
        """Warning when latest trade is newer than latest position update."""
        await upsert_position(db, _pos("TICK-1"))
        await insert_trade(db, _trade("TICK-1"))
        # Push trade timestamp ahead of position updated_at
        await db.conn.execute(
            "UPDATE trades SET timestamp = datetime('now', '+1 minute') "
            "WHERE ticker = 'TICK-1'"
        )
        await db.conn.commit()

        with caplog.at_level(logging.WARNING, logger="gimmes.store.queries"):
            result = await get_position_tickers(db)

        assert "stale" in caplog.text.lower()
        assert result == {"TICK-1"}

    async def test_no_warning_when_fresh(self, db, caplog):
        """No warning when positions are fresher than trades."""
        await insert_trade(db, _trade("TICK-1"))
        await upsert_position(db, _pos("TICK-1"))

        with caplog.at_level(logging.WARNING, logger="gimmes.store.queries"):
            result = await get_position_tickers(db)

        assert "stale" not in caplog.text.lower()
        assert result == {"TICK-1"}

    async def test_no_warning_for_paper_positions(self, db, caplog):
        """Staleness check is skipped for paper_positions table."""
        await _create_paper_position(db, "PAPER-1")

        with caplog.at_level(logging.WARNING, logger="gimmes.store.queries"):
            result = await get_position_tickers(db, table="paper_positions")

        assert "stale" not in caplog.text.lower()
        assert result == {"PAPER-1"}
