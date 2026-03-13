"""Read-only database queries for the Clubhouse dashboard."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path

import aiosqlite

from gimmes.clubhouse.models import (
    ActivityItem,
    CandidateItem,
    ConfigResponse,
    MetricsResponse,
    PortfolioResponse,
    PositionItem,
    RiskResponse,
    StatusResponse,
    TradeItem,
)
from gimmes.config import GimmesConfig, load_config
from gimmes.reporting.metrics import calculate_metrics


def _config() -> GimmesConfig:
    return load_config()


@asynccontextmanager
async def _connect(db_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Open a read-only SQLite connection as a context manager."""
    conn = await aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


def _table_exists_sql(table: str) -> str:
    return f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"


async def get_status(db_path: Path, pause_seconds: int = 0) -> StatusResponse:
    """Get system status: mode, loop active, current cycle."""
    config = _config()
    resp = StatusResponse(
        mode=config.mode.value,
        pause_seconds=pause_seconds,
    )

    try:
        async with _connect(db_path) as conn:
            # Check if activity_log table exists
            cursor = await conn.execute(_table_exists_sql("activity_log"))
            if not await cursor.fetchone():
                return resp

            cursor = await conn.execute(
                "SELECT cycle, timestamp FROM activity_log ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                resp.current_cycle = row["cycle"]
                # Consider loop active if last activity was within pause_seconds + 60
                from datetime import datetime

                try:
                    ts = datetime.fromisoformat(row["timestamp"]).replace(tzinfo=UTC)
                    now = datetime.now(UTC)
                    elapsed = (now - ts).total_seconds()
                    threshold = max(pause_seconds, 30) + 60
                    resp.loop_active = elapsed < threshold
                except (ValueError, TypeError):
                    pass
    except Exception:
        pass

    return resp


async def get_portfolio(db_path: Path) -> PortfolioResponse:
    """Get portfolio summary from latest snapshot + paper balance."""
    config = _config()
    resp = PortfolioResponse()

    try:
        async with _connect(db_path) as conn:
            # Get balance from paper_balance if driving range
            if not config.is_championship:
                cursor = await conn.execute(
                    "SELECT balance FROM paper_balance WHERE id = 1"
                )
                row = await cursor.fetchone()
                if row:
                    resp.balance = row["balance"]

            # Get latest snapshot for portfolio value + P&L
            cursor = await conn.execute(
                "SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT 1"
            )
            snap = await cursor.fetchone()
            if snap:
                resp.portfolio_value = snap["portfolio_value"]
                resp.total_equity = snap["total_equity"]
                resp.daily_pnl = snap["daily_pnl"]
                resp.total_pnl = snap["total_pnl"]
                # If we didn't get balance from paper, use snapshot
                if resp.balance == 0:
                    resp.balance = snap["balance"]

            # Calculate unrealized P&L from positions
            table = "paper_positions" if not config.is_championship else "positions"
            cursor = await conn.execute(
                f"SELECT COALESCE(SUM(unrealized_pnl), 0) as total FROM {table} WHERE count > 0"
            )
            row = await cursor.fetchone()
            if row:
                resp.unrealized_pnl = row["total"]

            # Recalculate total equity if we have live balance
            if resp.balance > 0:
                resp.total_equity = resp.balance + resp.unrealized_pnl
    except Exception:
        pass

    return resp


def _row_get(row: aiosqlite.Row, key: str, default: object = None) -> object:
    """Safely get a column from a Row, returning default if missing."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


async def get_positions(db_path: Path) -> list[PositionItem]:
    """Get open positions."""
    config = _config()
    table = "paper_positions" if not config.is_championship else "positions"
    items: list[PositionItem] = []

    try:
        async with _connect(db_path) as conn:
            cursor = await conn.execute(
                f"SELECT * FROM {table} WHERE count > 0 ORDER BY ticker"
            )
            rows = await cursor.fetchall()
            for row in rows:
                items.append(PositionItem(
                    ticker=row["ticker"],
                    title=str(_row_get(row, "title", "") or ""),
                    side=row["side"],
                    count=row["count"],
                    avg_price=row["avg_price"],
                    market_price=row["market_price"],
                    cost_basis=row["cost_basis"],
                    market_value=float(_row_get(row, "market_value", 0) or 0),
                    unrealized_pnl=row["unrealized_pnl"],
                    realized_pnl=row["realized_pnl"],
                ))
    except Exception:
        pass

    return items


async def get_trades(db_path: Path, limit: int = 50) -> list[TradeItem]:
    """Get recent trade decisions."""
    items: list[TradeItem] = []

    try:
        async with _connect(db_path) as conn:
            cursor = await conn.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            for row in rows:
                items.append(TradeItem(
                    id=row["id"],
                    ticker=row["ticker"],
                    action=row["action"],
                    side=row["side"],
                    count=row["count"],
                    price=row["price"],
                    model_probability=row["model_probability"],
                    gimme_score=row["gimme_score"],
                    edge=row["edge"],
                    rationale=row["rationale"],
                    agent=row["agent"],
                    timestamp=row["timestamp"],
                ))
    except Exception:
        pass

    return items


async def get_candidates(db_path: Path, limit: int = 20) -> list[CandidateItem]:
    """Get recent scanned candidates."""
    items: list[CandidateItem] = []

    try:
        async with _connect(db_path) as conn:
            cursor = await conn.execute(
                "SELECT * FROM candidates ORDER BY scanned_at DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            for row in rows:
                items.append(CandidateItem(
                    ticker=row["ticker"],
                    title=row["title"],
                    market_price=row["market_price"],
                    model_probability=row["model_probability"],
                    edge=row["edge"],
                    gimme_score=row["gimme_score"],
                    research_memo=row["research_memo"],
                    scanned_at=row["scanned_at"],
                ))
    except Exception:
        pass

    return items


async def get_metrics(db_path: Path) -> MetricsResponse:
    """Calculate performance metrics from trades and snapshots."""
    resp = MetricsResponse()
    config = _config()

    try:
        async with _connect(db_path) as conn:
            # Get trades
            cursor = await conn.execute(
                "SELECT * FROM trades ORDER BY timestamp ASC LIMIT 1000"
            )
            trades = [dict(row) for row in await cursor.fetchall()]

            # Get snapshots
            cursor = await conn.execute(
                "SELECT * FROM snapshots ORDER BY timestamp ASC LIMIT 500"
            )
            snapshots = [dict(row) for row in await cursor.fetchall()]

            initial = config.paper.starting_balance if not config.is_championship else 0
            metrics = calculate_metrics(trades, snapshots, initial)

            resp.win_rate = metrics.win_rate
            resp.sharpe_ratio = metrics.sharpe_ratio
            resp.max_drawdown = metrics.max_drawdown
            resp.max_drawdown_pct = metrics.max_drawdown_pct
            resp.total_return = metrics.total_return
            resp.total_return_pct = metrics.total_return_pct

            # Equity curve data for charting
            resp.equity_curve = [
                {"timestamp": s.get("timestamp", ""), "equity": s.get("total_equity", 0)}
                for s in snapshots
            ]
    except Exception:
        pass

    return resp


async def get_risk(db_path: Path) -> RiskResponse:
    """Get current risk limit usage."""
    config = _config()
    resp = RiskResponse(
        daily_loss_limit_pct=config.risk.daily_loss_limit_pct,
        max_positions=config.risk.max_open_positions,
        max_position_pct=config.sizing.max_position_pct,
    )

    try:
        async with _connect(db_path) as conn:
            # Daily P&L
            cursor = await conn.execute(
                """SELECT COALESCE(SUM(
                    CASE WHEN action = 'close' THEN (price - edge) * count ELSE 0 END
                ), 0) as daily_pnl
                FROM trades WHERE date(timestamp) = date('now')"""
            )
            row = await cursor.fetchone()
            if row:
                resp.daily_pnl = row["daily_pnl"]

            # Balance for percentage calculation
            balance = config.paper.starting_balance
            if not config.is_championship:
                cursor = await conn.execute(
                    "SELECT balance FROM paper_balance WHERE id = 1"
                )
                row = await cursor.fetchone()
                if row:
                    balance = row["balance"]

            if balance > 0:
                resp.daily_loss_pct = abs(resp.daily_pnl) / balance

            # Position count + largest position
            table = "paper_positions" if not config.is_championship else "positions"
            cursor = await conn.execute(
                f"SELECT COUNT(*) as cnt FROM {table} WHERE count > 0"
            )
            row = await cursor.fetchone()
            if row:
                resp.position_count = row["cnt"]

            cursor = await conn.execute(
                f"SELECT MAX(cost_basis) as largest FROM {table} WHERE count > 0"
            )
            row = await cursor.fetchone()
            if row and row["largest"] and balance > 0:
                resp.largest_position_pct = row["largest"] / balance
    except Exception:
        pass

    return resp


async def get_activity(db_path: Path, limit: int = 50) -> list[ActivityItem]:
    """Get recent activity log entries."""
    items: list[ActivityItem] = []

    try:
        async with _connect(db_path) as conn:
            cursor = await conn.execute(_table_exists_sql("activity_log"))
            if not await cursor.fetchone():
                return items

            cursor = await conn.execute(
                "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            for row in rows:
                items.append(ActivityItem(
                    id=row["id"],
                    cycle=row["cycle"],
                    agent=row["agent"],
                    phase=row["phase"],
                    message=row["message"],
                    details=row["details"],
                    timestamp=row["timestamp"],
                ))
    except Exception:
        pass

    return items


async def get_config_data() -> ConfigResponse:
    """Get current configuration (read-only)."""
    config = _config()
    return ConfigResponse(
        mode=config.mode.value,
        strategy=config.strategy.model_dump(),
        sizing=config.sizing.model_dump(),
        risk=config.risk.model_dump(),
        orders=config.orders.model_dump(),
        scanner=config.scanner.model_dump(),
        scoring=config.scoring.model_dump(),
        paper=config.paper.model_dump(),
    )


async def get_change_fingerprint(db_path: Path) -> str:
    """Get a fingerprint of current data state for SSE change detection.

    Returns a string of max IDs from key tables. When this changes,
    the dashboard should push an update.
    """
    parts: list[str] = []

    try:
        async with _connect(db_path) as conn:
            for table in ("trades", "positions", "snapshots", "candidates"):
                cursor = await conn.execute(f"SELECT MAX(id) as m FROM {table}")
                row = await cursor.fetchone()
                parts.append(str(row["m"] if row and row["m"] else 0))

            # activity_log may not exist yet
            cursor = await conn.execute(_table_exists_sql("activity_log"))
            if await cursor.fetchone():
                cursor = await conn.execute("SELECT MAX(id) as m FROM activity_log")
                row = await cursor.fetchone()
                parts.append(str(row["m"] if row and row["m"] else 0))
            else:
                parts.append("0")

            # Paper balance changes
            try:
                cursor = await conn.execute(
                    "SELECT balance FROM paper_balance WHERE id = 1"
                )
                row = await cursor.fetchone()
                parts.append(f"{row['balance']:.2f}" if row else "0")
            except Exception:
                parts.append("0")
    except Exception:
        parts = ["err"]

    return "|".join(parts)
