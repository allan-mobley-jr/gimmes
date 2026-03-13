"""Simple sequential migration runner for SQLite schema changes."""

from __future__ import annotations

from gimmes.store.database import Database

# Migrations are applied sequentially. Each is a tuple of (version, sql).
MIGRATIONS: list[tuple[int, str]] = [
    # Version 1 is the initial schema (handled by database.py SCHEMA_SQL).
    (2, """
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle INTEGER NOT NULL DEFAULT 0,
            agent TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """),
]


async def get_schema_version(db: Database) -> int:
    """Get the current schema version."""
    cursor = await db.conn.execute(
        "SELECT MAX(version) FROM schema_version"
    )
    row = await cursor.fetchone()
    if row and row[0] is not None:
        return int(row[0])
    # Initialize to version 1 (base schema)
    await db.conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (1)")
    await db.conn.commit()
    return 1


async def run_migrations(db: Database) -> int:
    """Run any pending migrations. Returns final schema version."""
    current = await get_schema_version(db)

    for version, sql in MIGRATIONS:
        if version > current:
            await db.conn.executescript(sql)
            await db.conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (version,)
            )
            await db.conn.commit()
            current = version

    return current
