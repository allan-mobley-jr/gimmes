"""Tests for thesis anchoring and position notes journal (issue #221).

Covers: migration v8 (thesis column on trades), migration v9 (position_notes table),
get_thesis_for_ticker, get_open_trade_for_ticker, insert_position_note, get_position_notes,
and thesis round-trip through _insert_trade_row.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import (
    get_open_trade_for_ticker,
    get_position_notes,
    get_thesis_for_ticker,
    get_trades,
    insert_candidate,
    insert_position_note,
    insert_trade,
)


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    """Temporary database with schema + all migrations applied."""
    database = Database(tmp_path / "test.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


def _trade(ticker: str = "TEST-TICKER", **kwargs: object) -> TradeDecision:
    defaults = {
        "ticker": ticker,
        "action": TradeDecision.Action.OPEN,
        "side": "yes",
        "count": 10,
        "price": 0.73,
        "model_probability": 0.92,
        "gimme_score": 82,
        "edge": 0.19,
        "rationale": "test",
        "agent": "closer",
    }
    return TradeDecision(**(defaults | kwargs))


# ---------------------------------------------------------------------------
# Migration v8 — thesis column on trades
# ---------------------------------------------------------------------------


class TestMigrationV8:
    async def test_schema_version_is_at_least_8(self, db: Database) -> None:
        cursor = await db.conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row[0] >= 8

    async def test_trades_has_thesis_column(self, db: Database) -> None:
        cursor = await db.conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "thesis" in columns

    async def test_migration_idempotent(self, tmp_path: Path) -> None:
        db1 = Database(tmp_path / "idem8.db")
        await db1.connect()
        await db1.close()
        db2 = Database(tmp_path / "idem8.db")
        await db2.connect()
        cursor = await db2.conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row[0] >= 8
        await db2.close()

    async def test_thesis_defaults_to_empty_string(self, db: Database) -> None:
        await insert_trade(db, _trade(thesis=""))
        rows = await get_trades(db, ticker="TEST-TICKER")
        assert rows[0]["thesis"] == ""

    async def test_thesis_round_trips(self, db: Database) -> None:
        memo = "Iran war energy shock pushes CPI above 0.7%."
        await insert_trade(db, _trade(thesis=memo))
        rows = await get_trades(db, ticker="TEST-TICKER")
        assert rows[0]["thesis"] == memo


# ---------------------------------------------------------------------------
# Migration v9 — position_notes table
# ---------------------------------------------------------------------------


class TestMigrationV9:
    async def test_schema_version_is_at_least_9(self, db: Database) -> None:
        cursor = await db.conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row[0] >= 9

    async def test_position_notes_table_exists(self, db: Database) -> None:
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='position_notes'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_position_notes_has_expected_columns(self, db: Database) -> None:
        cursor = await db.conn.execute("PRAGMA table_info(position_notes)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert columns >= {"id", "ticker", "cycle", "agent", "note_type", "body", "timestamp"}

    async def test_indexes_exist(self, db: Database) -> None:
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name IN ('idx_position_notes_ticker', 'idx_position_notes_cycle')"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 2

    async def test_invalid_note_type_rejected(self, db: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            await db.conn.execute(
                "INSERT INTO position_notes (ticker, note_type, body)"
                " VALUES (?, ?, ?)",
                ("TEST", "invalid_type", "body"),
            )

    async def test_migration_idempotent(self, tmp_path: Path) -> None:
        db1 = Database(tmp_path / "idem9.db")
        await db1.connect()
        await db1.close()
        db2 = Database(tmp_path / "idem9.db")
        await db2.connect()
        cursor = await db2.conn.execute("SELECT MAX(version) FROM schema_version")
        row = await cursor.fetchone()
        assert row[0] >= 9
        await db2.close()


# ---------------------------------------------------------------------------
# get_thesis_for_ticker
# ---------------------------------------------------------------------------


class TestGetThesisForTicker:
    async def test_returns_empty_string_when_no_candidate(self, db: Database) -> None:
        result = await get_thesis_for_ticker(db, "NONEXISTENT-TICKER")
        assert result == ""

    async def test_returns_research_memo(self, db: Database) -> None:
        await insert_candidate(
            db, "KXCPI-TEST", "CPI Test", 0.73, 0.92, 0.19, 82,
            "Iran war energy shock thesis.",
        )
        result = await get_thesis_for_ticker(db, "KXCPI-TEST")
        assert result == "Iran war energy shock thesis."

    async def test_returns_most_recent_when_multiple_candidates(
        self, db: Database
    ) -> None:
        await insert_candidate(db, "KXCPI-MULTI", "CPI", 0.70, 0.88, 0.18, 80, "old memo")
        await insert_candidate(db, "KXCPI-MULTI", "CPI", 0.73, 0.92, 0.19, 82, "new memo")
        result = await get_thesis_for_ticker(db, "KXCPI-MULTI")
        assert result == "new memo"


# ---------------------------------------------------------------------------
# get_open_trade_for_ticker
# ---------------------------------------------------------------------------


class TestGetOpenTradeForTicker:
    async def test_returns_none_when_no_trades(self, db: Database) -> None:
        result = await get_open_trade_for_ticker(db, "NONE-TICKER")
        assert result is None

    async def test_returns_none_for_close_only(self, db: Database) -> None:
        await insert_trade(db, _trade("CLOSE-ONLY", action=TradeDecision.Action.CLOSE))
        result = await get_open_trade_for_ticker(db, "CLOSE-ONLY")
        assert result is None

    async def test_returns_dict_for_open_trade(self, db: Database) -> None:
        await insert_trade(db, _trade("OPEN-TICKER"))
        result = await get_open_trade_for_ticker(db, "OPEN-TICKER")
        assert result is not None
        assert result["ticker"] == "OPEN-TICKER"
        assert result["action"] == "open"

    async def test_returned_dict_includes_thesis(self, db: Database) -> None:
        await insert_trade(db, _trade("THESIS-TICKER", thesis="my thesis"))
        result = await get_open_trade_for_ticker(db, "THESIS-TICKER")
        assert result is not None
        assert result["thesis"] == "my thesis"

    async def test_returns_most_recent_open(self, db: Database) -> None:
        await insert_trade(db, _trade("MULTI-OPEN", thesis="first"))
        await insert_trade(db, _trade("MULTI-OPEN", thesis="second",
                                      action=TradeDecision.Action.OPEN))
        result = await get_open_trade_for_ticker(db, "MULTI-OPEN")
        assert result is not None
        assert result["thesis"] == "second"


# ---------------------------------------------------------------------------
# insert_position_note and get_position_notes
# ---------------------------------------------------------------------------


class TestPositionNotes:
    async def test_insert_returns_positive_row_id(self, db: Database) -> None:
        row_id = await insert_position_note(
            db, ticker="TEST", body="price moved -2pp"
        )
        assert row_id > 0

    async def test_get_returns_empty_list_when_no_notes(self, db: Database) -> None:
        notes = await get_position_notes(db, "EMPTY-TICKER")
        assert notes == []

    async def test_roundtrip_all_fields(self, db: Database) -> None:
        await insert_position_note(
            db, ticker="RT-TICKER", cycle=5, agent="monitor",
            note_type="flag", body="thesis check failed",
        )
        notes = await get_position_notes(db, "RT-TICKER")
        assert len(notes) == 1
        n = notes[0]
        assert n["ticker"] == "RT-TICKER"
        assert n["cycle"] == 5
        assert n["agent"] == "monitor"
        assert n["note_type"] == "flag"
        assert n["body"] == "thesis check failed"

    async def test_returns_newest_first(self, db: Database) -> None:
        for i in range(3):
            await insert_position_note(db, ticker="ORDER-TICKER", body=f"note {i}")
        notes = await get_position_notes(db, "ORDER-TICKER")
        assert notes[0]["body"] == "note 2"
        assert notes[2]["body"] == "note 0"

    async def test_limit_is_respected(self, db: Database) -> None:
        for i in range(10):
            await insert_position_note(db, ticker="LIMIT-TICKER", body=f"note {i}")
        notes = await get_position_notes(db, "LIMIT-TICKER", limit=3)
        assert len(notes) == 3

    async def test_notes_are_isolated_by_ticker(self, db: Database) -> None:
        await insert_position_note(db, ticker="TICKER-A", body="A note")
        await insert_position_note(db, ticker="TICKER-B", body="B note")
        notes_a = await get_position_notes(db, "TICKER-A")
        assert len(notes_a) == 1
        assert notes_a[0]["body"] == "A note"

    async def test_all_valid_note_types_accepted(self, db: Database) -> None:
        for note_type in ("observation", "flag", "decision", "context"):
            row_id = await insert_position_note(
                db, ticker="TYPES-TICKER", note_type=note_type, body=f"{note_type} body"
            )
            assert row_id > 0
