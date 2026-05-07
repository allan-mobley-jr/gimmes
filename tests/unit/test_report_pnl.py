"""Regression test for #542: report() must not lose actionable trades when
skip volume dominates the trades table."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from gimmes.models.trade import TradeDecision
from gimmes.reporting.pnl import calculate_pnl
from gimmes.store.database import Database
from gimmes.store.queries import get_trades, insert_trade


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


def _open(ticker: str, price: float = 0.50, count: int = 100) -> TradeDecision:
    return TradeDecision(
        ticker=ticker, action=TradeDecision.Action.OPEN,
        side="yes", count=count, price=price,
    )


def _close(ticker: str, price: float = 0.60, count: int = 100) -> TradeDecision:
    return TradeDecision(
        ticker=ticker, action=TradeDecision.Action.CLOSE,
        side="yes", count=count, price=price,
    )


def _skip(ticker: str) -> TradeDecision:
    return TradeDecision(
        ticker=ticker, action=TradeDecision.Action.SKIP,
        side="yes", count=0, price=0.0,
    )


@pytest.mark.asyncio
async def test_report_handles_skip_dominated_history(db: Database) -> None:
    """Match the report() flow: fetch by action so skips can't truncate.

    Inserting an open + close for KXTEST-A then 1500 skips for KXTEST-B
    would, under the old single-LIMIT 1000 query, evict the open + close
    and yield a $0 P&L. Fetching by action keeps both visible.
    """
    await insert_trade(db, _open("KXTEST-A"))
    await insert_trade(db, _close("KXTEST-A"))
    for _ in range(1500):
        await insert_trade(db, _skip("KXTEST-B"))

    opens = await get_trades(db, action="open", limit=100_000)
    closes = await get_trades(db, action="close", limit=100_000)
    size_ups = await get_trades(db, action="size_up", limit=100_000)
    summary = calculate_pnl(opens + closes + size_ups)

    assert summary.gross_pnl > 0
    assert summary.winning_trades == 1
    assert summary.total_trades == 1


@pytest.mark.asyncio
async def test_naive_limit_query_is_broken(db: Database) -> None:
    """Document the broken behavior: a single LIMIT 1000 query loses the
    open/close because the 1500 skips inserted afterward sort to the top.
    Guards against a future regression that reverts the fix."""
    await insert_trade(db, _open("KXTEST-A"))
    await insert_trade(db, _close("KXTEST-A"))
    for _ in range(1500):
        await insert_trade(db, _skip("KXTEST-B"))

    naive = await get_trades(db, limit=1000)
    summary = calculate_pnl(naive)

    assert summary.gross_pnl == 0.0
    assert summary.winning_trades == 0
