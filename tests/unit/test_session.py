"""Tests for session tracking (store/session.py)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from gimmes.store.session import (
    close_orphan_activities,
    create_session,
    end_session,
    get_active_session,
    get_latest_session,
    get_max_global_cycle,
    mark_stale_sessions,
    pid_alive,
    update_session_cycle,
)


def _init_db(db_path: Path) -> None:
    """Create a minimal DB with just the sessions table."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL
                CHECK (mode IN ('driving_range', 'championship')),
            pid INTEGER NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at TEXT DEFAULT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'stopped', 'crashed')),
            cycle_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
    """)
    conn.close()


class TestPidAlive:
    def test_current_process_is_alive(self) -> None:
        assert pid_alive(os.getpid()) is True

    def test_dead_pid(self) -> None:
        assert pid_alive(999999) is False


class TestCreateSession:
    def test_creates_active_session(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        sid = create_session(db_path, "driving_range", os.getpid())
        assert sid > 0

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        conn.close()

        assert row["mode"] == "driving_range"
        assert row["pid"] == os.getpid()
        assert row["status"] == "active"
        assert row["cycle_count"] == 0

    def test_championship_session(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        sid = create_session(db_path, "championship", 12345)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        conn.close()

        assert row["mode"] == "championship"
        assert row["pid"] == 12345


class TestGetActiveSession:
    def test_returns_active_session(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        create_session(db_path, "driving_range", os.getpid())
        active = get_active_session(db_path)

        assert active is not None
        assert active["mode"] == "driving_range"
        assert active["pid"] == os.getpid()
        assert active["status"] == "active"

    def test_returns_none_when_no_sessions(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        assert get_active_session(db_path) is None

    def test_returns_none_for_nonexistent_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nonexistent.db"
        assert get_active_session(db_path) is None

    def test_marks_dead_pid_as_crashed(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        # Create session with a PID that doesn't exist
        create_session(db_path, "driving_range", 999999)
        active = get_active_session(db_path)
        assert active is None

        # Verify it was marked crashed
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()

        assert row["status"] == "crashed"
        assert row["ended_at"] is not None

    def test_handles_missing_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        # Create DB without sessions table
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE dummy (id INTEGER)")
        conn.close()

        assert get_active_session(db_path) is None


class TestGetLatestSession:
    def test_returns_most_recent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        create_session(db_path, "driving_range", os.getpid())
        sid2 = create_session(db_path, "championship", os.getpid())

        latest = get_latest_session(db_path)
        assert latest is not None
        assert latest["id"] == sid2
        assert latest["mode"] == "championship"

    def test_returns_none_when_empty(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        assert get_latest_session(db_path) is None


class TestUpdateSessionCycle:
    def test_updates_cycle_count(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        sid = create_session(db_path, "driving_range", os.getpid())
        update_session_cycle(db_path, sid, 5)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        conn.close()

        assert row["cycle_count"] == 5


class TestEndSession:
    def test_stopped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        sid = create_session(db_path, "driving_range", os.getpid())
        end_session(db_path, sid, "stopped")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        conn.close()

        assert row["status"] == "stopped"
        assert row["ended_at"] is not None

    def test_crashed(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        sid = create_session(db_path, "driving_range", os.getpid())
        end_session(db_path, sid, "crashed")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        conn.close()

        assert row["status"] == "crashed"


class TestMarkStaleSessions:
    def test_marks_dead_pids(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        # One alive, one dead
        create_session(db_path, "driving_range", os.getpid())
        create_session(db_path, "championship", 999999)

        count = mark_stale_sessions(db_path)
        assert count == 1

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()
        conn.close()

        assert rows[0]["status"] == "active"  # current PID still alive
        assert rows[1]["status"] == "crashed"  # dead PID marked

    def test_returns_zero_when_all_alive(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)

        create_session(db_path, "driving_range", os.getpid())
        assert mark_stale_sessions(db_path) == 0

    def test_returns_zero_for_nonexistent_db(self, tmp_path: Path) -> None:
        assert mark_stale_sessions(tmp_path / "nonexistent.db") == 0


class TestGetMaxGlobalCycle:
    def test_returns_zero_for_nonexistent_db(self, tmp_path: Path) -> None:
        assert get_max_global_cycle(tmp_path / "nonexistent.db") == 0

    def test_returns_zero_when_no_sessions(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        assert get_max_global_cycle(db_path) == 0

    def test_returns_max_across_sessions(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db(db_path)
        sid1 = create_session(db_path, "driving_range", os.getpid())
        update_session_cycle(db_path, sid1, 5)
        sid2 = create_session(db_path, "driving_range", os.getpid())
        update_session_cycle(db_path, sid2, 12)
        assert get_max_global_cycle(db_path) == 12


def _init_db_with_activity(db_path: Path) -> None:
    """Create DB with sessions + activity_log tables."""
    _init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle INTEGER NOT NULL DEFAULT 0,
            agent TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            session_id INTEGER DEFAULT NULL
        );
    """)
    conn.close()


class TestCloseOrphanActivities:
    def test_returns_zero_for_nonexistent_db(self, tmp_path: Path) -> None:
        assert close_orphan_activities(tmp_path / "nonexistent.db") == 0

    def test_returns_zero_when_no_orphans(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db_with_activity(db_path)
        sid = create_session(db_path, "driving_range", os.getpid())
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE sessions SET status = 'crashed' WHERE id = ?", (sid,)
        )
        # Insert start + complete pair — not orphaned
        conn.execute(
            "INSERT INTO activity_log (cycle, agent, phase, session_id) "
            "VALUES (1, 'scout', 'start', ?)", (sid,)
        )
        conn.execute(
            "INSERT INTO activity_log (cycle, agent, phase, session_id) "
            "VALUES (1, 'scout', 'complete', ?)", (sid,)
        )
        conn.commit()
        conn.close()
        assert close_orphan_activities(db_path) == 0

    def test_closes_orphan_starts(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _init_db_with_activity(db_path)
        sid = create_session(db_path, "driving_range", os.getpid())
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE sessions SET status = 'crashed' WHERE id = ?", (sid,)
        )
        # Insert start with no matching complete — orphaned
        conn.execute(
            "INSERT INTO activity_log (cycle, agent, phase, session_id) "
            "VALUES (1, 'scout', 'start', ?)", (sid,)
        )
        conn.commit()
        conn.close()

        assert close_orphan_activities(db_path) == 1

        # Verify crash_recovery entry was created
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM activity_log WHERE phase = 'crash_recovery'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["agent"] == "scout"
        assert row["cycle"] == 1
        assert row["session_id"] == sid
