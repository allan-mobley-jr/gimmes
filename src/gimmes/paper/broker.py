"""Paper trading broker — simulates order execution locally.

Returns the same model types (Order, Fill, Position) as the real Kalshi API
functions, making the routing transparent to CLI commands and agents.
"""

from __future__ import annotations

import datetime
import uuid

import aiosqlite

from gimmes.config import PaperTradingConfig
from gimmes.models.market import Orderbook
from gimmes.models.order import (
    CreateOrderParams,
    Fill,
    Order,
    OrderAction,
    OrderSide,
)
from gimmes.models.portfolio import Position
from gimmes.paper.fill_simulator import FillResult, SimulatedFill, simulate_fill
from gimmes.paper.schema import PAPER_SCHEMA_SQL
from gimmes.store.database import Database
from gimmes.strategy.fees import DEFAULT_FEE_MULTIPLIERS, FeeMultipliers, fee_for_order


class PaperBroker:
    """Local paper trading broker backed by SQLite."""

    def __init__(self, db: Database, config: PaperTradingConfig) -> None:
        self._db = db
        self._config = config

    @property
    def _conn(self) -> aiosqlite.Connection:
        return self._db.conn

    async def initialize(self) -> None:
        """Create paper tables and seed starting balance if needed."""
        # Migrate paper_positions from old single-column PK to (ticker, side)
        await self._migrate_positions_pk()

        await self._conn.executescript(PAPER_SCHEMA_SQL)
        await self._conn.commit()

        # Seed balance on first run
        cursor = await self._conn.execute("SELECT balance FROM paper_balance WHERE id = 1")
        row = await cursor.fetchone()
        if row is None:
            await self._conn.execute(
                "INSERT INTO paper_balance (id, balance, starting_balance) VALUES (1, ?, ?)",
                (self._config.starting_balance, self._config.starting_balance),
            )
            await self._conn.commit()

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    async def get_balance(self) -> float:
        cursor = await self._conn.execute("SELECT balance FROM paper_balance WHERE id = 1")
        row = await cursor.fetchone()
        return float(row["balance"]) if row else 0.0

    async def _update_balance(self, delta: float) -> None:
        """Adjust balance by delta (positive = credit, negative = debit)."""
        await self._conn.execute(
            "UPDATE paper_balance SET balance = balance + ?,"
            " updated_at = datetime('now') WHERE id = 1",
            (delta,),
        )

    async def _migrate_positions_pk(self) -> None:
        """Migrate paper_positions from single-column PK to (ticker, side)."""
        cursor = await self._conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name='paper_positions'"
        )
        if await cursor.fetchone() is None:
            return  # Table doesn't exist yet; schema will create it

        # Check if PK already includes side
        info = await self._conn.execute("PRAGMA table_info(paper_positions)")
        columns = await info.fetchall()
        pk_cols = [c for c in columns if int(c["pk"]) > 0]
        if len(pk_cols) > 1:
            return  # Already migrated

        # Rebuild table with new composite PK
        await self._conn.executescript("""
            ALTER TABLE paper_positions RENAME TO _paper_positions_old;
            CREATE TABLE paper_positions (
                ticker TEXT NOT NULL,
                side TEXT NOT NULL DEFAULT 'yes',
                count INTEGER NOT NULL DEFAULT 0,
                avg_price REAL NOT NULL DEFAULT 0,
                cost_basis REAL NOT NULL DEFAULT 0,
                market_price REAL NOT NULL DEFAULT 0,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (ticker, side)
            );
            INSERT INTO paper_positions
                SELECT * FROM _paper_positions_old;
            DROP TABLE _paper_positions_old;
        """)
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def create_order(
        self,
        params: CreateOrderParams,
        orderbook: Orderbook,
        *,
        fees: FeeMultipliers = DEFAULT_FEE_MULTIPLIERS,
    ) -> Order:
        """Simulate placing an order. Fills immediately if marketable.

        All balance, order, fill, and position writes are wrapped in a single
        transaction so a crash can never leave partial state.
        """
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        now = datetime.datetime.now(datetime.UTC)

        # SELL orders require a backing position with enough contracts
        if params.action == OrderAction.SELL:
            cursor = await self._conn.execute(
                "SELECT count FROM paper_positions"
                " WHERE ticker = ? AND side = ? AND count > 0",
                (params.ticker, params.side.value),
            )
            pos_row = await cursor.fetchone()
            if pos_row is None or int(pos_row["count"]) < params.count:
                return await self._reject_order(order_id, params, now)

        # Run fill simulation
        result = simulate_fill(params, orderbook, fees=fees)

        # Determine status
        if result.total_filled == params.count:
            status = "executed"
        elif result.total_filled > 0:
            status = "executed"  # Partial taker fill — remainder abandoned
        elif params.post_only:
            status = "resting"  # Maker order rests on book
        else:
            status = "canceled"  # Taker found nothing

        # Pre-transaction balance validation
        if result.total_filled > 0 and params.action == OrderAction.BUY:
            cost = result.total_notional + result.total_fees
            resting_cost = 0.0
            if result.remaining_count > 0 and params.post_only:
                resting_cost = result.remaining_count * (
                    params.price
                )
            balance = await self.get_balance()
            if balance < cost + resting_cost:
                return await self._reject_order(order_id, params, now)
        elif (
            params.action == OrderAction.BUY
            and result.remaining_count > 0
            and params.post_only
            and result.total_filled == 0
        ):
            resting_cost = result.remaining_count * (
                params.price
            )
            balance = await self.get_balance()
            if balance < resting_cost:
                return await self._reject_order(order_id, params, now)

        # All writes in one atomic transaction
        async with self._db.transaction():
            # Balance delta for filled portion
            if result.total_filled > 0:
                if params.action == OrderAction.BUY:
                    cost = result.total_notional + result.total_fees
                    await self._update_balance(-cost)
                else:  # SELL — credit proceeds minus fees
                    await self._update_balance(
                        result.total_notional - result.total_fees
                    )

            # Reserve balance only for resting BUY maker orders
            if (
                result.remaining_count > 0
                and params.post_only
                and params.action == OrderAction.BUY
            ):
                resting_price = params.price
                await self._update_balance(
                    -(result.remaining_count * resting_price)
                )

            # Insert order record
            await self._conn.execute(
                """INSERT INTO paper_orders
                   (order_id, ticker, action, side, count, remaining_count,
                    yes_price, no_price, status, post_only, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    params.ticker,
                    params.action.value,
                    params.side.value,
                    params.count,
                    result.remaining_count,
                    int(round((params.yes_price or 0) * 100)),
                    int(round((params.no_price or 0) * 100)),
                    status,
                    1 if params.post_only else 0,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )

            # Insert fills and update positions
            for fill in result.fills:
                trade_id = f"paper-fill-{uuid.uuid4().hex[:12]}"
                fill_cents = int(round(fill.price * 100))
                await self._conn.execute(
                    """INSERT INTO paper_fills
                       (trade_id, order_id, ticker, action, side, count,
                        yes_price, no_price, fee, is_taker, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        trade_id,
                        order_id,
                        params.ticker,
                        params.action.value,
                        params.side.value,
                        fill.count,
                        fill_cents if params.side == OrderSide.YES else 0,
                        fill_cents if params.side == OrderSide.NO else 0,
                        fill.fee,
                        1 if fill.is_taker else 0,
                        now.isoformat(),
                    ),
                )

            # Update position if any fills occurred
            if result.total_filled > 0:
                await self._update_position_from_fills(params, result)

        return Order(
            order_id=order_id,
            ticker=params.ticker,
            action=params.action,
            side=params.side,
            status=status,
            yes_price=params.yes_price or 0.0,
            no_price=params.no_price or 0.0,
            count=params.count,
            remaining_count=result.remaining_count,
            created_time=now,
        )

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a resting order and refund reserved balance."""
        cursor = await self._conn.execute(
            "SELECT * FROM paper_orders WHERE order_id = ? AND status = 'resting'",
            (order_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return

        async with self._db.transaction():
            # Refund reserved balance for unfilled contracts
            remaining = int(row["remaining_count"])
            price_cents = max(int(row["yes_price"]), int(row["no_price"]))
            refund = remaining * price_cents / 100.0
            await self._update_balance(refund)

            await self._conn.execute(
                "UPDATE paper_orders SET status = 'canceled',"
                " updated_at = datetime('now') WHERE order_id = ?",
                (order_id,),
            )

    async def fill_resting_orders(
        self,
        orderbooks: dict[str, Orderbook],
        *,
        fees: FeeMultipliers = DEFAULT_FEE_MULTIPLIERS,
    ) -> list[Order]:
        """Re-check resting orders against current orderbooks and fill any
        that have become marketable.

        For BUY orders the notional was already reserved at creation, so
        filling only debits the additional fee.  For SELL orders the proceeds
        are credited normally.

        Returns orders that received at least one new fill.
        """
        cursor = await self._conn.execute(
            "SELECT * FROM paper_orders WHERE status = 'resting'"
        )
        rows = await cursor.fetchall()

        filled_orders: list[Order] = []
        now = datetime.datetime.now(datetime.UTC)

        for row in rows:
            ticker = row["ticker"]
            if ticker not in orderbooks:
                continue

            try:
                remaining = int(row["remaining_count"])
                side = OrderSide(row["side"])
                action = OrderAction(row["action"])
                price_cents = max(int(row["yes_price"]), int(row["no_price"]))
                price = price_cents / 100.0

                # SELL orders require a backing position
                fillable = remaining
                if action == OrderAction.SELL:
                    pos = await self._conn.execute(
                        "SELECT count FROM paper_positions"
                        " WHERE ticker = ? AND side = ? AND count > 0",
                        (ticker, side.value),
                    )
                    pos_row = await pos.fetchone()
                    held = int(pos_row["count"]) if pos_row else 0
                    fillable = min(remaining, held)
                    if fillable <= 0:
                        import logging

                        logging.getLogger("gimmes").warning(
                            "Resting SELL order %s for %s %s has no backing"
                            " position (held=%d); skipping",
                            row["order_id"], ticker, side.value, held,
                        )
                        continue

                params = CreateOrderParams(
                    ticker=ticker,
                    action=action,
                    side=side,
                    count=fillable,
                    yes_price=price if side == OrderSide.YES else None,
                    no_price=price if side == OrderSide.NO else None,
                    post_only=True,
                )

                # Paper mode: fill at limit price unconditionally.
                # Real markets have opposing flow that hits resting bids;
                # paper mode has none, so we simulate immediate fill to
                # enable full position lifecycle testing.  (#215)
                fill_fee = fee_for_order(fillable, price, is_taker=False, fees=fees)
                fill = SimulatedFill(
                    count=fillable, price=price, fee=fill_fee, is_taker=False,
                )
                result = FillResult(
                    fills=[fill],
                    remaining_count=0,
                    total_filled=fillable,
                    total_notional=fillable * price,
                    total_fees=fill_fee,
                )

                async with self._db.transaction():
                    # Balance: BUY reservation already covers notional, just debit fees.
                    # SELL has no reservation — credit proceeds minus fees.
                    if action == OrderAction.BUY:
                        await self._update_balance(-result.total_fees)
                    else:
                        await self._update_balance(
                            result.total_notional - result.total_fees
                        )

                    # Record fills
                    for fill in result.fills:
                        trade_id = f"paper-fill-{uuid.uuid4().hex[:12]}"
                        fill_cents = int(round(fill.price * 100))
                        await self._conn.execute(
                            """INSERT INTO paper_fills
                               (trade_id, order_id, ticker, action, side, count,
                                yes_price, no_price, fee, is_taker, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                trade_id,
                                row["order_id"],
                                ticker,
                                action.value,
                                side.value,
                                fill.count,
                                fill_cents if side == OrderSide.YES else 0,
                                fill_cents if side == OrderSide.NO else 0,
                                fill.fee,
                                0,  # maker fill
                                now.isoformat(),
                            ),
                        )

                    # Update position
                    await self._update_position_from_fills(params, result)

                    # Update order record
                    new_remaining = remaining - result.total_filled
                    new_status = "executed" if new_remaining == 0 else "resting"
                    await self._conn.execute(
                        """UPDATE paper_orders
                           SET remaining_count = ?, status = ?,
                               updated_at = datetime('now')
                           WHERE order_id = ?""",
                        (new_remaining, new_status, row["order_id"]),
                    )

                filled_orders.append(Order(
                    order_id=row["order_id"],
                    ticker=ticker,
                    action=action,
                    side=side,
                    status=new_status,
                    yes_price=int(row["yes_price"]) / 100.0,
                    no_price=int(row["no_price"]) / 100.0,
                    count=int(row["count"]),
                    remaining_count=new_remaining,
                    created_time=row["created_at"],
                ))
            except Exception:
                import logging

                logging.getLogger("gimmes").error(
                    "Failed to process resting order %s; skipping",
                    row["order_id"],
                    exc_info=True,
                )

        return filled_orders

    async def list_orders(
        self,
        ticker: str | None = None,
        status: str | None = None,
    ) -> list[Order]:
        query = "SELECT * FROM paper_orders WHERE 1=1"
        params: list[object] = []
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [
            Order(
                order_id=row["order_id"],
                ticker=row["ticker"],
                action=OrderAction(row["action"]),
                side=OrderSide(row["side"]),
                status=row["status"],
                yes_price=int(row["yes_price"]) / 100.0,
                no_price=int(row["no_price"]) / 100.0,
                count=int(row["count"]),
                remaining_count=int(row["remaining_count"]),
                created_time=row["created_at"],
            )
            for row in rows
        ]

    async def list_fills(self, ticker: str | None = None) -> list[Fill]:
        query = "SELECT * FROM paper_fills WHERE 1=1"
        params: list[object] = []
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        query += " ORDER BY created_at DESC"

        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [
            Fill(
                trade_id=row["trade_id"],
                order_id=row["order_id"],
                ticker=row["ticker"],
                action=OrderAction(row["action"]),
                side=OrderSide(row["side"]),
                count=int(row["count"]),
                yes_price=int(row["yes_price"]) / 100.0,
                no_price=int(row["no_price"]) / 100.0,
                is_taker=bool(row["is_taker"]),
                created_time=row["created_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def get_positions(self) -> list[Position]:
        cursor = await self._conn.execute(
            "SELECT * FROM paper_positions WHERE count > 0"
        )
        rows = await cursor.fetchall()
        return [
            Position(
                ticker=row["ticker"],
                side=row["side"],
                count=int(row["count"]),
                avg_price=float(row["avg_price"]),
                cost_basis=float(row["cost_basis"]),
                market_price=float(row["market_price"]),
                unrealized_pnl=float(row["unrealized_pnl"]),
                realized_pnl=float(row["realized_pnl"]),
            )
            for row in rows
        ]

    async def mark_to_market(self, ticker: str, current_price: float) -> None:
        """Update unrealized P&L for a position based on current market price."""
        cursor = await self._conn.execute(
            "SELECT * FROM paper_positions WHERE ticker = ? AND count > 0",
            (ticker,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return

        async with self._db.transaction():
            for row in rows:
                count = int(row["count"])
                avg_price = float(row["avg_price"])
                side = row["side"]

                if side == "yes":
                    unrealized = (current_price - avg_price) * count
                else:
                    unrealized = (avg_price - current_price) * count

                stored_price = current_price if side == "yes" else 1 - current_price
                await self._conn.execute(
                    """UPDATE paper_positions
                       SET market_price = ?, unrealized_pnl = ?,
                           updated_at = datetime('now')
                       WHERE ticker = ? AND side = ?""",
                    (stored_price, unrealized, ticker, side),
                )

    async def settle(self, ticker: str, result: str) -> None:
        """Settle a resolved market. result is 'yes' or 'no'.

        YES position + YES result → pays $1/contract
        YES position + NO result → pays $0
        NO position + NO result → pays $1/contract
        NO position + YES result → pays $0
        """
        cursor = await self._conn.execute(
            "SELECT * FROM paper_positions WHERE ticker = ? AND count > 0",
            (ticker,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return

        async with self._db.transaction():
            for row in rows:
                count = int(row["count"])
                side = row["side"]
                cost_basis = float(row["cost_basis"])

                won = (side == result)
                payout = count * 1.0 if won else 0.0
                realized_pnl = payout - cost_basis + float(row["realized_pnl"])

                await self._update_balance(payout)

                await self._conn.execute(
                    """UPDATE paper_positions
                       SET count = 0, market_price = ?, unrealized_pnl = 0,
                           realized_pnl = ?, updated_at = datetime('now')
                       WHERE ticker = ? AND side = ?""",
                    (1.0 if won else 0.0, realized_pnl, ticker, side),
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _reject_order(
        self,
        order_id: str,
        params: CreateOrderParams,
        now: datetime.datetime,
    ) -> Order:
        """Record a canceled order and return it."""
        await self._conn.execute(
            """INSERT INTO paper_orders
               (order_id, ticker, action, side, count, remaining_count,
                yes_price, no_price, status, post_only,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'canceled', ?, ?, ?)""",
            (
                order_id,
                params.ticker,
                params.action.value,
                params.side.value,
                params.count,
                params.count,
                int(round((params.yes_price or 0) * 100)),
                int(round((params.no_price or 0) * 100)),
                1 if params.post_only else 0,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        await self._conn.commit()
        return Order(
            order_id=order_id,
            ticker=params.ticker,
            action=params.action,
            side=params.side,
            status="canceled",
            yes_price=params.yes_price or 0.0,
            no_price=params.no_price or 0.0,
            count=params.count,
            remaining_count=params.count,
            created_time=now,
        )

    async def _update_position_from_fills(
        self, params: CreateOrderParams, fill_result: FillResult
    ) -> None:
        """Update paper_positions after fills."""

        ticker = params.ticker
        side = params.side.value

        cursor = await self._conn.execute(
            "SELECT * FROM paper_positions WHERE ticker = ? AND side = ?", (ticker, side)
        )
        existing = await cursor.fetchone()

        # Calculate weighted average fill price
        total_fill_cost = sum(
            f.count * (f.price) for f in fill_result.fills
        )
        total_fees = sum(f.fee for f in fill_result.fills)
        filled = fill_result.total_filled

        if params.action == OrderAction.BUY:
            if existing and int(existing["count"]) > 0:
                # Add to existing position (side already validated by query)
                old_count = int(existing["count"])
                old_cost = float(existing["cost_basis"])
                new_count = old_count + filled
                new_cost = old_cost + total_fill_cost + total_fees
                new_avg = new_cost / new_count if new_count > 0 else 0.0

                await self._conn.execute(
                    """UPDATE paper_positions
                       SET count = ?, avg_price = ?, cost_basis = ?, updated_at = datetime('now')
                       WHERE ticker = ? AND side = ?""",
                    (new_count, new_avg, new_cost, ticker, side),
                )
            else:
                # New position
                cost_basis = total_fill_cost + total_fees
                avg_price = cost_basis / filled if filled > 0 else 0.0
                fill_price = total_fill_cost / filled if filled > 0 else 0.0

                await self._conn.execute(
                    """INSERT INTO paper_positions
                       (ticker, side, count, avg_price, cost_basis, market_price)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(ticker, side) DO UPDATE SET
                        count=excluded.count,
                        avg_price=excluded.avg_price, cost_basis=excluded.cost_basis,
                        market_price=excluded.market_price, updated_at=datetime('now')""",
                    (ticker, side, filled, avg_price, cost_basis, fill_price),
                )
        else:
            # SELL — reduce position, realize P&L, reduce cost_basis proportionally
            if existing and int(existing["count"]) > 0:
                old_count = int(existing["count"])
                old_avg = float(existing["avg_price"])
                old_cost = float(existing["cost_basis"])
                sell_count = min(filled, old_count)
                sell_proceeds = total_fill_cost - total_fees
                realized = sell_proceeds - (old_avg * sell_count)

                new_count = old_count - sell_count
                # Reduce cost_basis proportionally to contracts sold
                new_cost = old_cost * (new_count / old_count) if old_count > 0 else 0.0
                new_avg = new_cost / new_count if new_count > 0 else 0.0
                old_realized = float(existing["realized_pnl"])

                await self._conn.execute(
                    """UPDATE paper_positions
                       SET count = ?, avg_price = ?, cost_basis = ?, realized_pnl = ?,
                           updated_at = datetime('now')
                       WHERE ticker = ? AND side = ?""",
                    (new_count, new_avg, new_cost, old_realized + realized, ticker, side),
                )
