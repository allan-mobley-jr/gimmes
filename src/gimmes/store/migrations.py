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
            severity TEXT NOT NULL CHECK (
                severity IN ('debug', 'info', 'warning', 'error', 'critical')
            ),
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
    # Versions > 5 must go after the v5 block in run_migrations().
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

# ALTER TABLE ADD COLUMN statements for v8: thesis snapshot on trades.
_V8_COLUMNS: list[str] = [
    "ALTER TABLE trades ADD COLUMN thesis TEXT NOT NULL DEFAULT ''",
]

# ALTER TABLE ADD COLUMN statements for v10: session_id on activity_log.
_V10_COLUMNS: list[str] = [
    "ALTER TABLE activity_log ADD COLUMN session_id INTEGER DEFAULT NULL",
]

# ALTER TABLE ADD COLUMN statements for v12: cap_blocked flag on candidates.
_V12_COLUMNS: list[str] = [
    "ALTER TABLE candidates ADD COLUMN cap_blocked INTEGER NOT NULL DEFAULT 0",
]

# ALTER TABLE ADD COLUMN statements for v16: close_time on position tables.
_V16_COLUMNS: list[str] = [
    "ALTER TABLE positions ADD COLUMN close_time TEXT DEFAULT NULL",
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

    # Version 6: indexes on high-query tables
    if current < 6:
        await db.conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_action ON trades(action);
            CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON snapshots(timestamp);
            CREATE INDEX IF NOT EXISTS idx_candidates_scanned ON candidates(scanned_at);
        """)
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (6,)
        )
        await db.conn.commit()
        current = 6

    # Version 7: sessions table for mode tracking
    if current < 7:
        await db.conn.executescript("""
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
            CREATE INDEX IF NOT EXISTS idx_sessions_status
                ON sessions(status);
            CREATE INDEX IF NOT EXISTS idx_sessions_started
                ON sessions(started_at);
        """)
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (7,)
        )
        await db.conn.commit()
        current = 7

    # Version 8: thesis snapshot column on trades (ALTER TABLE, idempotent)
    if current < 8:
        await _run_alter_columns(db, _V8_COLUMNS)
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (8,)
        )
        await db.conn.commit()
        current = 8

    # Version 9: position_notes journal table
    if current < 9:
        await db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS position_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                cycle INTEGER NOT NULL DEFAULT 0,
                agent TEXT NOT NULL DEFAULT '',
                note_type TEXT NOT NULL DEFAULT 'observation'
                    CHECK (note_type IN ('observation', 'flag', 'decision', 'context')),
                body TEXT NOT NULL DEFAULT '',
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_position_notes_ticker
                ON position_notes(ticker);
            CREATE INDEX IF NOT EXISTS idx_position_notes_cycle
                ON position_notes(cycle);
        """)
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (9,)
        )
        await db.conn.commit()
        current = 9

    # Version 10: session_id column on activity_log (ALTER TABLE, idempotent)
    if current < 10:
        await _run_alter_columns(db, _V10_COLUMNS)
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (10,)
        )
        await db.conn.commit()
        current = 10

    # Version 11: index on candidates.ticker for per-ticker lookups (#275)
    if current < 11:
        await db.conn.executescript(
            "CREATE INDEX IF NOT EXISTS idx_candidates_ticker"
            " ON candidates(ticker);"
        )
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (11,)
        )
        await db.conn.commit()
        current = 11

    # Version 12: cap_blocked flag on candidates (#276)
    if current < 12:
        await _run_alter_columns(db, _V12_COLUMNS)
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (12,)
        )
        await db.conn.commit()
        current = 12

    # Version 13: config key-value table (#292)
    if current < 13:
        await db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (13,)
        )
        await db.conn.commit()
        current = 13

    # Version 14: split risk.bankroll → bankroll_paper + bankroll_real (#299)
    # Only migrate old value to bankroll_paper. bankroll_real stays at default
    # (0) so the championship gate forces explicit confirmation on first entry.
    if current < 14:
        cursor = await db.conn.execute(
            "SELECT value FROM config WHERE key = 'risk.bankroll'"
        )
        row = await cursor.fetchone()
        if row:
            old_value = row[0]
            await db.conn.execute(
                "INSERT OR IGNORE INTO config (key, value, updated_at) "
                "VALUES ('risk.bankroll_paper', ?, datetime('now'))",
                (old_value,),
            )
            await db.conn.execute(
                "DELETE FROM config WHERE key = 'risk.bankroll'"
            )
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (14,)
        )
        await db.conn.commit()
        current = 14

    if current < 15:
        _v15 = [
            "ALTER TABLE candidates ADD COLUMN recommendation"
            " TEXT NOT NULL DEFAULT ''",
        ]
        await _run_alter_columns(db, _v15)
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (15,)
        )
        await db.conn.commit()
        current = 15

    # Version 16: close_time column on positions and paper_positions
    if current < 16:
        await _run_alter_columns(db, _V16_COLUMNS)
        # paper_positions is created by PaperBroker, not the main schema,
        # so it may not exist yet. Only alter if present.
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name='paper_positions'"
        )
        if await cursor.fetchone():
            await _run_alter_columns(db, [
                "ALTER TABLE paper_positions ADD COLUMN close_time"
                " TEXT DEFAULT NULL",
            ])
        await db.conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (16,)
        )
        await db.conn.commit()
        current = 16

    return current
