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
    (3, """
        CREATE TABLE IF NOT EXISTS error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            severity TEXT NOT NULL CHECK (severity IN ('debug', 'info', 'warning', 'error', 'critical')),
            category TEXT NOT NULL CHECK (category IN (
                'api_error', 'auth_failure', 'data_integrity', 'agent_failure',
                'order_failure', 'risk_breach', 'config_error', 'network_error', 'paper_broker'
            )),
            error_code TEXT NOT NULL DEFAULT '',
            component TEXT NOT NULL DEFAULT '',
            agent TEXT NOT NULL DEFAULT '',
            cycle INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL,
            stack_trace TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '{}',
            resolved INTEGER NOT NULL DEFAULT 0,
            github_issue_url TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_error_severity ON error_log(severity);
        CREATE INDEX IF NOT EXISTS idx_error_category ON error_log(category);
        CREATE INDEX IF NOT EXISTS idx_error_timestamp ON error_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_error_resolved ON error_log(resolved);
    """),
    (4, """
        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            parameter_path TEXT NOT NULL,
            current_value TEXT NOT NULL,
            recommended_value TEXT NOT NULL,
            confidence TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
            analysis_type TEXT NOT NULL,
            rationale TEXT NOT NULL,
            supporting_data TEXT NOT NULL DEFAULT '{}',
            github_issue_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'implemented', 'rejected', 'superseded')),
            outcome TEXT NOT NULL DEFAULT '',
            outcome_measured_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_rec_status ON recommendations(status);
        CREATE INDEX IF NOT EXISTS idx_rec_parameter ON recommendations(parameter_path);
    """),
    # Version 5 uses _run_alter_columns below (ALTER TABLE is not idempotent).
]

# ALTER TABLE ADD COLUMN statements for v5. Each is run individually so that
# a partial failure on a previous attempt doesn't leave the schema stuck.
_V5_COLUMNS: list[str] = [
    ("ALTER TABLE trades ADD COLUMN resolved_outcome TEXT DEFAULT NULL"
     " CHECK (resolved_outcome IN ('yes', 'no'))"),
    "ALTER TABLE candidates ADD COLUMN edge_size_score REAL NOT NULL DEFAULT 0",
    "ALTER TABLE candidates ADD COLUMN signal_strength_score REAL NOT NULL DEFAULT 0",
    "ALTER TABLE candidates ADD COLUMN liquidity_depth_score REAL NOT NULL DEFAULT 0",
    "ALTER TABLE candidates ADD COLUMN settlement_clarity_score REAL NOT NULL DEFAULT 0",
    "ALTER TABLE candidates ADD COLUMN time_to_resolution_score REAL NOT NULL DEFAULT 0",
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


async def _run_alter_columns(db: Database, statements: list[str]) -> None:
    """Run ALTER TABLE statements, ignoring 'duplicate column name' errors."""
    for stmt in statements:
        try:
            await db.conn.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            if "duplicate column name" in str(exc).lower():
                continue
            raise


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

    # Version 5: ALTER TABLE columns (handled separately for idempotency)
    if current < 5:
        await _run_alter_columns(db, _V5_COLUMNS)
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (5,)
        )
        await db.conn.commit()
        current = 5

    return current
