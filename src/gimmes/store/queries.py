"""Named database queries for trades, positions, snapshots."""

from __future__ import annotations

from gimmes.models.portfolio import PortfolioSnapshot, Position
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


async def insert_candidate(db: Database, ticker: str, title: str, market_price: float,
                           model_prob: float, edge: float, score: float, memo: str) -> None:
    """Insert a scanned gimme candidate."""
    await db.conn.execute(
        """INSERT INTO candidates
           (ticker, title, market_price, model_probability, edge, gimme_score, research_memo)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticker, title, market_price, model_prob, edge, score, memo),
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
