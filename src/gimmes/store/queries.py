"""Named database queries for trades, positions, snapshots, errors."""

from __future__ import annotations

from gimmes.models.error import ErrorLogEntry
from gimmes.models.portfolio import PortfolioSnapshot, Position
from gimmes.models.recommendation import Recommendation
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database

# ---------------------------------------------------------------------------
# Trade decisions
# ---------------------------------------------------------------------------


async def insert_trade(db: Database, trade: TradeDecision) -> int:
    """Insert a trade decision record. Returns the row ID."""
    cursor = await db.conn.execute(
        """INSERT INTO trades
           (ticker, action, side, count, price, model_probability,
            gimme_score, edge, kelly_fraction, rationale, agent, order_id, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            trade.agent,
            trade.order_id,
            trade.timestamp.isoformat(),
        ),
    )
    await db.conn.commit()
    return cursor.lastrowid or 0


async def get_trades(
    db: Database,
    *,
    ticker: str | None = None,
    action: str | None = None,
    limit: int = 50,
) -> list[dict]:  # type: ignore[type-arg]
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


async def upsert_position(db: Database, pos: Position) -> None:
    """Insert or update a position."""
    await db.conn.execute(
        """INSERT INTO positions
           (ticker, title, side, count, avg_price, market_price,
            cost_basis, market_value, unrealized_pnl, realized_pnl)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(ticker) DO UPDATE SET
            title=excluded.title, side=excluded.side, count=excluded.count,
            avg_price=excluded.avg_price, market_price=excluded.market_price,
            cost_basis=excluded.cost_basis, market_value=excluded.market_value,
            unrealized_pnl=excluded.unrealized_pnl, realized_pnl=excluded.realized_pnl,
            updated_at=datetime('now')""",
        (
            pos.ticker,
            pos.title,
            pos.side,
            pos.count,
            pos.avg_price,
            pos.market_price,
            pos.cost_basis,
            pos.market_value,
            pos.unrealized_pnl,
            pos.realized_pnl,
        ),
    )
    await db.conn.commit()


async def get_positions(db: Database) -> list[Position]:
    """Get all stored positions."""
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
    edge_size_score: float = 0,
    signal_strength_score: float = 0,
    liquidity_depth_score: float = 0,
    settlement_clarity_score: float = 0,
    time_to_resolution_score: float = 0,
) -> None:
    """Insert a scanned gimme candidate with optional component scores."""
    await db.conn.execute(
        """INSERT INTO candidates
           (ticker, title, market_price, model_probability, edge, gimme_score,
            research_memo, edge_size_score, signal_strength_score,
            liquidity_depth_score, settlement_clarity_score, time_to_resolution_score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, title, market_price, model_prob, edge, score, memo,
         edge_size_score, signal_strength_score, liquidity_depth_score,
         settlement_clarity_score, time_to_resolution_score),
    )
    await db.conn.commit()


# ---------------------------------------------------------------------------
# P&L queries
# ---------------------------------------------------------------------------


async def get_daily_pnl(db: Database) -> float:
    """Calculate today's realized P&L from trade records."""
    cursor = await db.conn.execute(
        """SELECT COALESCE(SUM(
            CASE WHEN action = 'close' THEN (price - edge) * count
            ELSE 0 END
        ), 0) as daily_pnl
        FROM trades
        WHERE date(timestamp) = date('now')"""
    )
    row = await cursor.fetchone()
    return float(row["daily_pnl"]) if row else 0.0


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
) -> int:
    """Insert an activity log entry. Returns the row ID."""
    cursor = await db.conn.execute(
        """INSERT INTO activity_log (cycle, agent, phase, message, details)
           VALUES (?, ?, ?, ?, ?)""",
        (cycle, agent, phase, message, details),
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
# Candidates — recent
# ---------------------------------------------------------------------------


async def get_recent_candidates(db: Database, limit: int = 20) -> list[dict]:
    """Get recent scanned candidates, newest first."""
    cursor = await db.conn.execute(
        "SELECT * FROM candidates ORDER BY scanned_at DESC LIMIT ?", (limit,)
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
