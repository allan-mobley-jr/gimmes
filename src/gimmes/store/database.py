"""SQLite database connection and schema management."""

from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'yes',
    count INTEGER NOT NULL DEFAULT 0,
    price REAL NOT NULL DEFAULT 0,
    model_probability REAL NOT NULL DEFAULT 0,
    gimme_score REAL NOT NULL DEFAULT 0,
    edge REAL NOT NULL DEFAULT 0,
    kelly_fraction REAL NOT NULL DEFAULT 0,
    rationale TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL DEFAULT 'yes',
    count INTEGER NOT NULL DEFAULT 0,
    avg_price REAL NOT NULL DEFAULT 0,
    market_price REAL NOT NULL DEFAULT 0,
    cost_basis REAL NOT NULL DEFAULT 0,
    market_value REAL NOT NULL DEFAULT 0,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    balance REAL NOT NULL DEFAULT 0,
    portfolio_value REAL NOT NULL DEFAULT 0,
    total_equity REAL NOT NULL DEFAULT 0,
    open_position_count INTEGER NOT NULL DEFAULT 0,
    daily_pnl REAL NOT NULL DEFAULT 0,
    total_pnl REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    market_price REAL NOT NULL DEFAULT 0,
    model_probability REAL NOT NULL DEFAULT 0,
    edge REAL NOT NULL DEFAULT 0,
    gimme_score REAL NOT NULL DEFAULT 0,
    research_memo TEXT NOT NULL DEFAULT '',
    scanned_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            from gimmes.config import GIMMES_HOME
            db_path = GIMMES_HOME / "gimmes.db"
        self.db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open database connection and initialize schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    async def __aenter__(self) -> Database:
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
