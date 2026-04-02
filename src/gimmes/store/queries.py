"""Named database queries for trades, positions, snapshots, errors."""

from __future__ import annotations

import logging
from typing import TypedDict

from gimmes.models.error import ErrorLogEntry
from gimmes.models.portfolio import PortfolioSnapshot, Position
from gimmes.models.recommendation import Recommendation
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database


class TradeRecord(TypedDict, total=False):
    """Typed dict for trade records from the database."""

    id: int
    ticker: str
    action: str
    side: str
    count: int
    price: float
    model_probability: float
    gimme_score: float
    edge: float
    kelly_fraction: float
    rationale: str
    thesis: str
    agent: str
    order_id: str
    timestamp: str
    resolved_outcome: str | None

# ---------------------------------------------------------------------------
# Trade decisions
# ---------------------------------------------------------------------------


async def _insert_trade_row(db: Database, trade: TradeDecision) -> int:
    """Core trade insert SQL. Caller manages the transaction."""
    cursor = await db.conn.execute(
        """INSERT INTO trades
           (ticker, action, side, count, price, model_probability,
            gimme_score, edge, kelly_fraction, rationale, thesis, agent, order_id, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trade.ticker,
            trade.action.value,
            trade.side,
            trade.count,
            trade.price,
            trade.model_probability,
            trade.gimme_score,
            trade.edge,
            trade.kelly_fraction,
            trade.rationale,
            trade.thesis,
            trade.agent,
            trade.order_id,
            trade.timestamp.isoformat(),
        ),
    )
    return cursor.lastrowid or 0


async def insert_trade(db: Database, trade: TradeDecision) -> int:
    """Insert a trade decision record. Returns the row ID."""
    row_id = await _insert_trade_row(db, trade)
    await db.conn.commit()
    return row_id


async def get_trades(
    db: Database,
    *,
    ticker: str | None = None,
    action: str | None = None,
    limit: int = 50,
) -> list[TradeRecord]:
    """Query trade decisions with optional filters."""
    query = "SELECT * FROM trades WHERE 1=1"
    params: list[object] = []

    if ticker:
        query += " AND ticker = ?"
        params.append(ticker)
    if action:
        query += " AND action = ?"
        params.append(action)

    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    cursor = await db.conn.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def update_trade_outcome(db: Database, ticker: str, outcome: str) -> int:
    """Set resolved_outcome for all trades matching a ticker.

    Args:
        ticker: Market ticker.
        outcome: Resolution result ('yes' or 'no').

    Returns:
        Number of rows updated.
    """
    cursor = await db.conn.execute(
        "UPDATE trades SET resolved_outcome = ? WHERE ticker = ? AND resolved_outcome IS NULL",
        (outcome, ticker),
    )
    await db.conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


_UPSERT_POSITION_SQL = """INSERT INTO positions
    (ticker, title, side, count, avg_price, market_price,
     cost_basis, market_value, unrealized_pnl, realized_pnl)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(ticker) DO UPDATE SET
     title=excluded.title, side=excluded.side, count=excluded.count,
     avg_price=excluded.avg_price, market_price=excluded.market_price,
     cost_basis=excluded.cost_basis, market_value=excluded.market_value,
     unrealized_pnl=excluded.unrealized_pnl, realized_pnl=excluded.realized_pnl,
     updated_at=datetime('now')"""


def _position_params(pos: Position) -> tuple:
    return (
        pos.ticker, pos.title, pos.side, pos.count,
        pos.avg_price, pos.market_price, pos.cost_basis,
        pos.market_value, pos.unrealized_pnl, pos.realized_pnl,
    )


async def upsert_position(db: Database, pos: Position) -> None:
    """Insert or update a position."""
    await db.conn.execute(_UPSERT_POSITION_SQL, _position_params(pos))
    await db.conn.commit()


async def _sync_positions_rows(db: Database, positions: list[Position]) -> None:
    """Core position sync SQL. Caller manages the transaction."""
    current_tickers = {p.ticker for p in positions}
    # Remove positions that no longer exist
    cursor = await db.conn.execute("SELECT ticker FROM positions")
    for row in await cursor.fetchall():
        if row["ticker"] not in current_tickers:
            await db.conn.execute(
                "DELETE FROM positions WHERE ticker = ?", (row["ticker"],)
            )
    # Upsert current positions
    for pos in positions:
        await db.conn.execute(_UPSERT_POSITION_SQL, _position_params(pos))


async def sync_positions(db: Database, positions: list[Position]) -> None:
    """Replace all positions with the given list.

    Runs as a single atomic transaction — clears stale positions and upserts
    current ones so the local DB always reflects the API/broker state.
    """
    async with db.transaction():
        await _sync_positions_rows(db, positions)


async def sync_positions_with_trade(
    db: Database, positions: list[Position], trade: TradeDecision
) -> int:
    """Atomically sync positions and log a trade decision.

    Ensures position state and trade log are always consistent — if either
    operation fails, both are rolled back.  Returns the trade row ID.
    """
    async with db.transaction():
        await _sync_positions_rows(db, positions)
        return await _insert_trade_row(db, trade)


async def _check_position_staleness(db: Database, table: str) -> None:
    """Log a warning if position data may be stale.

    Only applies to the ``positions`` table — paper_positions are updated
    inline by the paper broker and cannot become stale in this way.
    """
    if table != "positions":
        return
    logger = logging.getLogger(__name__)
    # Normalize timestamps with datetime() since trades use ISO format
    # (T separator) while positions use SQLite datetime() format (space).
    stale_cursor = await db.conn.execute(
        """SELECT
            datetime((SELECT MAX(timestamp) FROM trades)) AS latest_trade,
            datetime((SELECT MAX(updated_at) FROM positions WHERE count > 0)) AS latest_pos
        """
    )
    stale_row = await stale_cursor.fetchone()
    if stale_row and stale_row["latest_trade"] and stale_row["latest_pos"]:
        if stale_row["latest_trade"] > stale_row["latest_pos"]:
            logger.warning(
                "Position data may be stale: latest trade at %s but latest "
                "position update at %s — run 'reconcile' to sync",
                stale_row["latest_trade"],
                stale_row["latest_pos"],
            )


async def get_positions(db: Database) -> list[Position]:
    """Get all stored positions.

    Logs a warning if positions are stale (updated_at older than the most
    recent trade timestamp), which can happen after a crash between trade
    insertion and position sync.
    """
    await _check_position_staleness(db, "positions")

    cursor = await db.conn.execute("SELECT * FROM positions WHERE count > 0")
    rows = await cursor.fetchall()
    return [
        Position(
            ticker=row["ticker"],
            title=row["title"],
            side=row["side"],
            count=row["count"],
            avg_price=row["avg_price"],
            market_price=row["market_price"],
            cost_basis=row["cost_basis"],
            market_value=row["market_value"],
            unrealized_pnl=row["unrealized_pnl"],
            realized_pnl=row["realized_pnl"],
        )
        for row in rows
    ]


_ALLOWED_POSITION_TABLES = frozenset({"positions", "paper_positions"})


async def get_position_tickers(
    db: Database, *, table: str = "positions"
) -> set[str]:
    """Return tickers of all open positions (count > 0)."""
    if table not in _ALLOWED_POSITION_TABLES:
        raise ValueError(f"Invalid position table: {table}")
    await _check_position_staleness(db, table)
    cursor = await db.conn.execute(
        f"SELECT ticker FROM {table} WHERE count > 0"  # noqa: S608
    )
    rows = await cursor.fetchall()
    return {row["ticker"] for row in rows}


async def delete_position(db: Database, ticker: str) -> None:
    """Remove a position (e.g., after settlement)."""
    await db.conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
    await db.conn.commit()


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


async def insert_snapshot(db: Database, snap: PortfolioSnapshot) -> None:
    """Insert a portfolio snapshot."""
    await db.conn.execute(
        """INSERT INTO snapshots
           (timestamp, balance, portfolio_value, total_equity,
            open_position_count, daily_pnl, total_pnl)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            snap.timestamp.isoformat(),
            snap.balance,
            snap.portfolio_value,
            snap.total_equity,
            snap.open_position_count,
            snap.daily_pnl,
            snap.total_pnl,
        ),
    )
    await db.conn.commit()


async def get_latest_snapshot(db: Database) -> dict | None:  # type: ignore[type-arg]
    """Get the most recent portfolio snapshot."""
    cursor = await db.conn.execute(
        "SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


async def insert_candidate(
    db: Database,
    ticker: str,
    title: str,
    market_price: float,
    model_prob: float,
    edge: float,
    score: float,
    memo: str,
    *,
    cap_blocked: bool = False,
    edge_size_score: float = 0,
    signal_strength_score: float = 0,
    liquidity_depth_score: float = 0,
    settlement_clarity_score: float = 0,
    time_to_resolution_score: float = 0,
) -> int:
    """Insert a scanned gimme candidate with optional component scores.

    Returns the row ID of the inserted candidate.
    """
    cursor = await db.conn.execute(
        """INSERT INTO candidates
           (ticker, title, market_price, model_probability, edge, gimme_score,
            research_memo, cap_blocked, edge_size_score, signal_strength_score,
            liquidity_depth_score, settlement_clarity_score, time_to_resolution_score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, title, market_price, model_prob, edge, score, memo,
         int(cap_blocked), edge_size_score, signal_strength_score,
         liquidity_depth_score, settlement_clarity_score,
         time_to_resolution_score),
    )
    await db.conn.commit()
    return cursor.lastrowid or 0


async def _delete_candidates_by_tickers(
    db: Database, tickers: set[str],
) -> int:
    """Delete candidates matching a set of tickers. Returns rows deleted."""
    if not tickers:
        return 0
    placeholders = ",".join("?" for _ in tickers)
    cursor = await db.conn.execute(
        f"DELETE FROM candidates WHERE ticker IN ({placeholders})",
        tuple(tickers),
    )
    return cursor.rowcount


async def prune_candidates(
    db: Database,
    *,
    open_tickers: set[str],
    inactive_tickers: set[str] | None = None,
    max_age_hours: int = 72,
) -> dict[str, int]:
    """Remove candidates that have exited the pipeline.

    Pruning rules (applied in order within a single transaction):
    1. Opened as a position — ticker has an open position.
    2. Market inactive — ticker's market is determined, finalized, or closed.
    3. Aged out — scanned_at older than max_age_hours.
    4. Stale duplicates — keep only the most recent row per ticker.

    Returns counts per pruning reason.
    """
    if max_age_hours < 1:
        raise ValueError(f"max_age_hours must be >= 1, got {max_age_hours}")

    counts: dict[str, int] = {"opened": 0, "inactive": 0, "aged_out": 0, "duplicates": 0}

    async with db.transaction():
        # 1. Opened as a position
        if open_tickers:
            counts["opened"] = await _delete_candidates_by_tickers(db, open_tickers)

        # 2. Market inactive
        if inactive_tickers:
            counts["inactive"] = await _delete_candidates_by_tickers(db, inactive_tickers)

        # 3. Aged out
        cursor = await db.conn.execute(
            "DELETE FROM candidates WHERE scanned_at < datetime('now', ?)",
            (f"-{max_age_hours} hours",),
        )
        counts["aged_out"] = cursor.rowcount

        # 4. Stale duplicates — keep only the newest row per ticker
        cursor = await db.conn.execute(
            "DELETE FROM candidates WHERE id NOT IN"
            " (SELECT MAX(id) FROM candidates GROUP BY ticker)"
        )
        counts["duplicates"] = cursor.rowcount

    return counts


async def mark_cap_blocked(db: Database, ticker: str) -> bool:
    """Mark the most recent candidate for a ticker as cap-blocked.

    Returns True if a row was updated, False otherwise.
    """
    cursor = await db.conn.execute(
        "UPDATE candidates SET cap_blocked = 1"
        " WHERE id = (SELECT MAX(id) FROM candidates WHERE ticker = ?)",
        (ticker,),
    )
    await db.conn.commit()
    return cursor.rowcount > 0


async def clear_all_candidates(db: Database) -> int:
    """Delete all cached candidates. Returns the number of rows deleted."""
    cursor = await db.conn.execute("DELETE FROM candidates")
    await db.conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# P&L queries
# ---------------------------------------------------------------------------


async def get_daily_pnl(db: Database, *, today: str | None = None) -> float:
    """Calculate realized P&L from close trades for a given date (defaults to today).

    For each close trade on the target date, finds the most recent open trade
    on the same ticker that occurred before the close, then computes:
    (close_price - open_price) * count.

    Args:
        db: Database connection.
        today: Optional date string (YYYY-MM-DD) to use instead of 'now'.
    """
    cursor = await db.conn.execute(
        """SELECT COALESCE(SUM(
            (c.price - COALESCE(
                (SELECT price FROM trades o
                 WHERE o.ticker = c.ticker
                   AND o.action = 'open'
                   AND o.timestamp <= c.timestamp
                 ORDER BY o.timestamp DESC
                 LIMIT 1),
            0)) * c.count
        ), 0) as daily_pnl
        FROM trades c
        WHERE c.action = 'close'
          AND date(c.timestamp) = date(?)""",
        ("now" if today is None else today,),
    )
    row = await cursor.fetchone()
    return float(row["daily_pnl"]) if row else 0.0


async def get_deployed_cost_basis(db: Database) -> float:
    """Total cost basis of all open positions."""
    cursor = await db.conn.execute(
        "SELECT COALESCE(SUM(cost_basis), 0) AS deployed"
        " FROM positions WHERE count > 0"
    )
    row = await cursor.fetchone()
    return float(row["deployed"]) if row else 0.0


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


async def insert_activity(
    db: Database,
    *,
    cycle: int = 0,
    agent: str = "",
    phase: str = "",
    message: str = "",
    details: str = "",
    session_id: int | None = None,
) -> int:
    """Insert an activity log entry. Returns the row ID."""
    cursor = await db.conn.execute(
        """INSERT INTO activity_log
           (cycle, agent, phase, message, details, session_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (cycle, agent, phase, message, details, session_id),
    )
    await db.conn.commit()
    return cursor.lastrowid or 0


async def get_recent_activity(db: Database, limit: int = 50) -> list[dict]:
    """Get recent activity log entries, newest first."""
    cursor = await db.conn.execute(
        "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Snapshots — range queries
# ---------------------------------------------------------------------------


async def get_snapshots(db: Database, limit: int = 500) -> list[dict]:
    """Get portfolio snapshots, oldest first (for equity curve)."""
    cursor = await db.conn.execute(
        "SELECT * FROM snapshots ORDER BY timestamp ASC LIMIT ?", (limit,)
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


async def get_recent_candidates(db: Database, limit: int = 20) -> list[dict]:
    """Get recent scanned candidates, newest first."""
    cursor = await db.conn.execute(
        "SELECT * FROM candidates ORDER BY scanned_at DESC LIMIT ?", (limit,)
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_thesis_for_ticker(db: Database, ticker: str) -> str:
    """Return the most recent research_memo from candidates for a ticker.

    Used by the order command to snapshot the Caddie's thesis at open time.
    Returns an empty string if no candidate record exists.
    """
    cursor = await db.conn.execute(
        "SELECT research_memo FROM candidates WHERE ticker = ?"
        " ORDER BY scanned_at DESC, id DESC LIMIT 1",
        (ticker,),
    )
    row = await cursor.fetchone()
    return row["research_memo"] if row else ""


async def get_candidate_for_ticker(
    db: Database, ticker: str, *, limit: int = 1,
) -> list[dict]:
    """Return the most recent candidate row(s) for a specific ticker."""
    cursor = await db.conn.execute(
        "SELECT * FROM candidates WHERE ticker = ?"
        " ORDER BY scanned_at DESC, id DESC LIMIT ?",
        (ticker, limit),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_open_trade_for_ticker(db: Database, ticker: str) -> dict | None:  # type: ignore[type-arg]
    """Return the most recent open trade record for a ticker, or None."""
    cursor = await db.conn.execute(
        "SELECT * FROM trades WHERE ticker = ? AND action = 'open'"
        " ORDER BY timestamp DESC LIMIT 1",
        (ticker,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def has_open_position(db: Database, ticker: str) -> bool:
    """Return True if the ticker has a position with count > 0."""
    cursor = await db.conn.execute(
        "SELECT 1 FROM positions WHERE ticker = ? AND count > 0",
        (ticker,),
    )
    return await cursor.fetchone() is not None


# ---------------------------------------------------------------------------
# Position notes (thesis journal)
# ---------------------------------------------------------------------------


async def insert_position_note(
    db: Database,
    *,
    ticker: str,
    cycle: int = 0,
    agent: str = "",
    note_type: str = "observation",
    body: str = "",
) -> int:
    """Append a note to the position_notes journal. Returns the row ID."""
    cursor = await db.conn.execute(
        """INSERT INTO position_notes (ticker, cycle, agent, note_type, body)
           VALUES (?, ?, ?, ?, ?)""",
        (ticker, cycle, agent, note_type, body),
    )
    await db.conn.commit()
    return cursor.lastrowid or 0


async def get_position_notes(
    db: Database,
    ticker: str,
    *,
    limit: int = 50,
) -> list[dict]:  # type: ignore[type-arg]
    """Return position notes for a ticker, newest first."""
    cursor = await db.conn.execute(
        "SELECT * FROM position_notes WHERE ticker = ? ORDER BY id DESC LIMIT ?",
        (ticker, limit),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------


async def get_trade_count(db: Database, action: str | None = None) -> int:
    """Count trade records."""
    if action:
        cursor = await db.conn.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE action = ?", (action,)
        )
    else:
        cursor = await db.conn.execute("SELECT COUNT(*) as cnt FROM trades")
    row = await cursor.fetchone()
    return int(row["cnt"]) if row else 0


# ---------------------------------------------------------------------------
# Error log
# ---------------------------------------------------------------------------


async def insert_error(db: Database, entry: ErrorLogEntry) -> int:
    """Insert an error log entry. Returns the row ID."""
    cursor = await db.conn.execute(
        """INSERT INTO error_log
           (severity, category, error_code, component, agent, cycle,
            message, stack_trace, context, resolved, github_issue_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry.severity.value,
            entry.category.value,
            entry.error_code,
            entry.component,
            entry.agent,
            entry.cycle,
            entry.message,
            entry.stack_trace,
            entry.context,
            int(entry.resolved),
            entry.github_issue_url,
        ),
    )
    await db.conn.commit()
    return cursor.lastrowid or 0


async def get_errors(
    db: Database,
    *,
    severity: str | None = None,
    category: str | None = None,
    unresolved: bool = False,
    limit: int = 50,
) -> list[dict]:  # type: ignore[type-arg]
    """Query error log entries with optional filters."""
    query = "SELECT * FROM error_log WHERE 1=1"
    params: list[object] = []

    if severity:
        query += " AND severity = ?"
        params.append(severity)
    if category:
        query += " AND category = ?"
        params.append(category)
    if unresolved:
        query += " AND resolved = 0"

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    cursor = await db.conn.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_error_summary(db: Database) -> list[dict]:  # type: ignore[type-arg]
    """Get error counts grouped by severity and category."""
    cursor = await db.conn.execute(
        """SELECT severity, category, COUNT(*) as count,
                  SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END) as unresolved
           FROM error_log
           GROUP BY severity, category
           ORDER BY count DESC"""
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def resolve_error(db: Database, error_id: int, github_issue_url: str = "") -> None:
    """Mark an error as resolved, optionally linking a GitHub issue."""
    await db.conn.execute(
        "UPDATE error_log SET resolved = 1, github_issue_url = ? WHERE id = ?",
        (github_issue_url, error_id),
    )
    await db.conn.commit()


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


async def insert_recommendation(db: Database, rec: Recommendation) -> int:
    """Insert a parameter recommendation. Returns the row ID."""
    cursor = await db.conn.execute(
        """INSERT INTO recommendations
           (parameter_path, current_value, recommended_value, confidence,
            analysis_type, rationale, supporting_data)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            rec.parameter_path,
            rec.current_value,
            rec.recommended_value,
            rec.confidence.value,
            rec.analysis_type.value,
            rec.rationale,
            rec.supporting_data,
        ),
    )
    await db.conn.commit()
    return cursor.lastrowid or 0


async def get_recommendations(
    db: Database,
    *,
    status: str | None = None,
    parameter: str | None = None,
    limit: int = 50,
) -> list[dict]:  # type: ignore[type-arg]
    """Query recommendations with optional filters."""
    query = "SELECT * FROM recommendations WHERE 1=1"
    params: list[object] = []

    if status:
        query += " AND status = ?"
        params.append(status)
    if parameter:
        query += " AND parameter_path = ?"
        params.append(parameter)

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    cursor = await db.conn.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def update_recommendation_status(
    db: Database,
    rec_id: int,
    status: str,
    *,
    github_issue_url: str = "",
    outcome: str = "",
) -> None:
    """Update a recommendation's status and optional fields."""
    fields = ["status = ?"]
    params: list[object] = [status]

    if github_issue_url:
        fields.append("github_issue_url = ?")
        params.append(github_issue_url)
    if outcome:
        fields.append("outcome = ?")
        params.append(outcome)
        fields.append("outcome_measured_at = datetime('now')")

    params.append(rec_id)
    await db.conn.execute(
        f"UPDATE recommendations SET {', '.join(fields)} WHERE id = ?",
        params,
    )
    await db.conn.commit()
