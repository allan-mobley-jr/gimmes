"""Sync session helpers for non-async contexts.

Uses stdlib sqlite3 (not aiosqlite) so these functions can be called from
synchronous code like CLI preambles.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Literal

logger = logging.getLogger("gimmes.store.session")


def pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # process exists but we lack permission
    except OSError:
        return False


def get_active_session(db_path: Path) -> dict | None:
    """Get the active trading session, if any.

    Performs a PID liveness check: if the session's process is dead,
    marks it as 'crashed' and returns None.
    """
    if not db_path.exists():
        return None

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                "SELECT * FROM sessions WHERE status = 'active' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if row is None:
                return None

            session = dict(row)

            if not pid_alive(session["pid"]):
                conn.close()
                try:
                    wconn = sqlite3.connect(str(db_path))
                    try:
                        wconn.execute(
                            "UPDATE sessions SET status = 'crashed', "
                            "ended_at = datetime('now') WHERE id = ?",
                            (session["id"],),
                        )
                        wconn.commit()
                    finally:
                        wconn.close()
                except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                    logger.warning(
                        "Failed to mark stale session %d as crashed: %s",
                        session["id"], exc,
                    )
                return None

            return session
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            logger.warning("get_active_session failed: %s", exc)
        return None
    except sqlite3.DatabaseError as exc:
        logger.error("get_active_session: database error: %s", exc)
        return None


def get_latest_session(db_path: Path) -> dict | None:
    """Get the most recent session regardless of status."""
    if not db_path.exists():
        return None

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            logger.warning("get_latest_session failed: %s", exc)
        return None
    except sqlite3.DatabaseError as exc:
        logger.error("get_latest_session: database error: %s", exc)
        return None


def create_session(db_path: Path, mode: str, pid: int) -> int:
    """Create a new active session. Returns the session ID."""
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            "INSERT INTO sessions (mode, pid) VALUES (?, ?)",
            (mode, pid),
        )
        conn.commit()
        return cursor.lastrowid or 0
    finally:
        conn.close()


def update_session_cycle(
    db_path: Path, session_id: int, cycle_count: int
) -> None:
    """Update the cycle count for a session."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE sessions SET cycle_count = ? WHERE id = ?",
                (cycle_count, session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        logger.warning(
            "Failed to update session %d cycle to %d: %s",
            session_id, cycle_count, exc,
        )


def end_session(
    db_path: Path, session_id: int, status: Literal["stopped", "crashed"]
) -> None:
    """Mark a session as stopped or crashed.

    This function is safe to call from exception handlers — it catches
    its own errors to avoid masking the original exception.
    """
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE sessions SET status = ?, ended_at = datetime('now') "
                "WHERE id = ?",
                (status, session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        logger.error(
            "Failed to end session %d as %s: %s",
            session_id, status, exc,
        )


def mark_stale_sessions(db_path: Path) -> int:
    """Mark active sessions with dead PIDs as crashed. Returns count."""
    if not db_path.exists():
        return 0

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        logger.warning("mark_stale_sessions: failed to connect: %s", exc)
        return 0

    try:
        cursor = conn.execute(
            "SELECT id, pid FROM sessions WHERE status = 'active'"
        )
        rows = cursor.fetchall()

        count = 0
        for row in rows:
            if not pid_alive(row["pid"]):
                conn.execute(
                    "UPDATE sessions SET status = 'crashed', "
                    "ended_at = datetime('now') WHERE id = ?",
                    (row["id"],),
                )
                count += 1

        if count:
            conn.commit()
        return count
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            logger.warning("mark_stale_sessions failed: %s", exc)
        return 0
    finally:
        conn.close()
