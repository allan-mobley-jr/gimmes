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
from gimmes.paper.fill_simulator import (
    FillResult,
    SimulatedFill,
    has_opposing_liquidity,
    simulate_fill,
)
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

        # Rest-on-miss (#743): add expires_at to pre-existing
        # paper_orders tables (CREATE TABLE IF NOT EXISTS above skips
        # them). Same idempotent-ALTER idiom as store/migrations.py.
        try:
            await self._conn.execute(
                "ALTER TABLE paper_orders ADD COLUMN expires_at TEXT DEFAULT NULL"
            )
            await self._conn.commit()
        except aiosqlite.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise

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
                close_time TEXT DEFAULT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (ticker, side)
            );
            INSERT INTO paper_positions
                (ticker, side, count, avg_price, cost_basis, market_price,
                 unrealized_pnl, realized_pnl, updated_at)
                SELECT ticker, side, count, avg_price, cost_basis, market_price,
                       unrealized_pnl, realized_pnl, updated_at
                FROM _paper_positions_old;
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
                held = int(pos_row["count"]) if pos_row else 0
                return await self._reject_order(
                    order_id, params, now,
                    reason=(
                        f"insufficient position: have {held}, need"
                        f" {params.count}"
                    ),
                )

        # Run fill simulation
        result = simulate_fill(params, orderbook, fees=fees)

        # Paper mode: fill maker order remainder immediately at limit price.
        # Real markets have opposing flow that hits resting bids; paper mode
        # has none, so we fill at limit to enable same-cycle position
        # updates.  (#255)
        # #690: ONLY when the opposing side exists at all — an empty
        # counterparty side means no market, and filling there would
        # fabricate a fill nobody offered (the phantom ledger rows the
        # #663 settle clamp had to defend against). The order cancels
        # instead (status falls through to "canceled" below).
        opposing_liquidity = has_opposing_liquidity(params, orderbook)
        if (
            params.post_only and result.remaining_count > 0
            and opposing_liquidity
        ):
            remaining = result.remaining_count
            limit_price = params.price
            fill_fee = fee_for_order(
                remaining, limit_price, is_taker=False, fees=fees,
            )
            maker_fill = SimulatedFill(
                count=remaining, price=limit_price, fee=fill_fee, is_taker=False,
            )
            result = FillResult(
                fills=result.fills + [maker_fill],
                remaining_count=0,
                total_filled=params.count,
                total_notional=result.total_notional + remaining * limit_price,
                total_fees=result.total_fees + fill_fee,
            )

        # Determine status. Default: any fill counts as executed and a
        # partial taker fill abandons the remainder. With an expiration
        # set (#743 rest-on-miss), a BUY's unfilled remainder rests
        # until expiry instead — including the partial-fill case, so a
        # 1-of-10 taker fill doesn't silently undersize the position.
        # Resting is honest even against an empty book — the order
        # POSTS liquidity; only fabricating a FILL there is forbidden
        # (#690).
        if (
            params.action == OrderAction.BUY
            and params.expiration_ts is not None
            and result.remaining_count > 0
        ):
            status = "resting"
        elif result.total_filled > 0:
            status = "executed"
        else:
            status = "canceled"
        cancel_reason = ""
        if status == "canceled" and not opposing_liquidity:
            cancel_reason = (
                "no opposing liquidity — empty book, no counterparty"
                " at any price (#690)"
            )

        # Resting BUYs reserve the unfilled notional at the limit price
        # up front — cancel_order refunds remaining * stored cents on
        # that assumption, and fill_resting_orders debits only fees.
        # Reserve at the cents-rounded price so the round trip is exact.
        reserve = 0.0
        if status == "resting":
            reserve = (
                result.remaining_count * round(params.price * 100) / 100.0
            )

        # Pre-transaction balance validation
        if params.action == OrderAction.BUY and (
            result.total_filled > 0 or status == "resting"
        ):
            cost = result.total_notional + result.total_fees + reserve
            balance = await self.get_balance()
            if balance < cost:
                return await self._reject_order(
                    order_id, params, now,
                    reason=(
                        f"insufficient balance: cost ${cost:,.2f} >"
                        f" ${balance:,.2f}"
                    ),
                )

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
            if reserve > 0:
                await self._update_balance(-reserve)

            expires_at: str | None = None
            if params.expiration_ts is not None:
                expires_at = datetime.datetime.fromtimestamp(
                    params.expiration_ts, tz=datetime.UTC,
                ).isoformat()

            # Insert order record
            await self._conn.execute(
                """INSERT INTO paper_orders
                   (order_id, ticker, action, side, count, remaining_count,
                    yes_price, no_price, status, post_only, expires_at,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    expires_at,
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
            reason=cancel_reason,
        )

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a resting order and refund reserved balance.

        Resting rows come from the rest-on-miss path (zero-fill BUY
        with an expiration) and from legacy pre-#255 orders.
        """
        cursor = await self._conn.execute(
            "SELECT * FROM paper_orders WHERE order_id = ? AND status = 'resting'",
            (order_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return

        async with self._db.transaction():
            # Refund reserved balance for unfilled contracts — BUYS
            # only (#690 review): sells never reserve balance, so an
            # unconditional refund fabricated paper money on the
            # legacy-cleanup path this fix sanctions.
            remaining = int(row["remaining_count"])
            if str(row["action"]) == "buy":
                price_cents = max(
                    int(row["yes_price"]), int(row["no_price"]),
                )
                refund = remaining * price_cents / 100.0
                await self._update_balance(refund)

            await self._conn.execute(
                "UPDATE paper_orders SET status = 'canceled',"
                " updated_at = datetime('now') WHERE order_id = ?",
                (order_id,),
            )

            # #690 (#684 item 4): a NEVER-FILLED resting order's
            # placement-time trade rows describe a non-event — annul
            # them (action='skip', append-only ledger: no DELETE) so
            # daily P&L / scorecard / the #663 settle-clamp residual
            # math stop seeing an exit that never traded. A partially
            # filled legacy row keeps its trade rows (warned).
            if remaining == int(row["count"]):
                await self._conn.execute(
                    """UPDATE trades SET action = 'skip',
                       reason = 'order_canceled',
                       rationale = rationale || ?
                       WHERE order_id = ?""",
                    (
                        " [#690 annulment: resting order canceled"
                        " before any fill]",
                        order_id,
                    ),
                )
            else:
                import logging

                # #743: fill-time ledger rows cover only contracts that
                # actually filled, so a partial fill leaves nothing to
                # annul — the kept rows are real. (Pre-#743 legacy rows
                # logged the FULL count at placement; for those this
                # branch still overstates, hence the log line.)
                logging.getLogger("gimmes").info(
                    "canceled order %s was partially filled (%d/%d)"
                    " — fill-time trade rows kept (#690/#743)",
                    order_id, int(row["count"]) - remaining,
                    int(row["count"]),
                )

    async def expire_resting_orders(self) -> list[str]:
        """Cancel resting orders whose expiration has passed.

        Mirrors the exchange-side `expiration_ts` enforcement the real
        Kalshi API applies. Returns the canceled order ids. Refunds and
        trade-row annulment ride on cancel_order (#690).
        """
        now_iso = datetime.datetime.now(datetime.UTC).isoformat()
        # expires_at IS NULL rows are pre-#743 legacy resting orders:
        # under marketable-only fill semantics they could otherwise
        # neither fill nor expire, locking their reservation forever —
        # cancel them on sight (the #690 annulment was built for them).
        cursor = await self._conn.execute(
            "SELECT order_id FROM paper_orders WHERE status = 'resting'"
            " AND (expires_at IS NULL OR expires_at <= ?)",
            (now_iso,),
        )
        rows = await cursor.fetchall()
        expired: list[str] = []
        for row in rows:
            await self.cancel_order(row["order_id"])
            expired.append(row["order_id"])
        if expired:
            import logging

            logging.getLogger("gimmes").info(
                "expired %d resting paper order(s): %s",
                len(expired), ", ".join(expired),
            )
        return expired

    async def fill_resting_orders(
        self,
        orderbooks: dict[str, Orderbook],
        *,
        fees: FeeMultipliers = DEFAULT_FEE_MULTIPLIERS,
    ) -> list[tuple[Order, int]]:
        """Re-check resting orders against current orderbooks and fill any
        that have become marketable.

        Rest-on-miss orders (and legacy pre-#255 rows) fill ONLY when
        the market has come back to the limit — maker semantics at the
        limit price, up to the opposing depth at eligible levels. The
        pre-rework #215 behavior (fill at limit whenever ANY opposing
        depth exists, price-blind) overstated fills in exactly the thin
        sub-hour books the rest-on-miss lane targets.

        Expired orders are skipped here — expire_resting_orders()
        cancels them. Returns (order, newly_filled_count) pairs for
        orders that received at least one new fill — the count is THIS
        sweep's fill, which callers need for ledger rows (the Order's
        own count/remaining_count only give the lifetime total).
        """
        cursor = await self._conn.execute(
            "SELECT * FROM paper_orders WHERE status = 'resting'"
        )
        rows = await cursor.fetchall()

        filled_orders: list[tuple[Order, int]] = []
        now = datetime.datetime.now(datetime.UTC)

        for row in rows:
            ticker = row["ticker"]
            if ticker not in orderbooks:
                continue

            # Expired rows are expire_resting_orders()' job — never
            # fill past the deadline the order was placed under. (The
            # skip is defense in depth for a caller that fills without
            # expiring first.)
            expires_at = row["expires_at"]
            if expires_at and expires_at <= now.isoformat():
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

                # #690: same empty-book guard as create_order — a
                # legacy resting order against a dead book stays
                # resting instead of fabricating a fill.
                if not has_opposing_liquidity(params, orderbooks[ticker]):
                    import logging

                    logging.getLogger("gimmes").debug(
                        "resting order %s skipped — no opposing"
                        " liquidity on %s (#690)",
                        row["order_id"], ticker,
                    )
                    continue

                # Maker semantics at the limit: fill only when the
                # market has come back to the limit price, and only up
                # to the opposing depth at eligible levels. (Replaces
                # the price-blind #215 immediate fill, which overstated
                # fills in exactly the thin sub-hour books the
                # rest-on-miss lane targets.)
                result = simulate_fill(params, orderbooks[ticker], fees=fees)
                if result.total_filled <= 0:
                    continue

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

                filled_orders.append((
                    Order(
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
                    ),
                    result.total_filled,
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
        positions: list[Position] = []
        for row in rows:
            close_time_val = None
            try:
                ct_raw = row["close_time"]
                if ct_raw:
                    from datetime import datetime as _dt

                    close_time_val = _dt.fromisoformat(ct_raw)
            except (IndexError, KeyError, ValueError):
                pass
            positions.append(
                Position(
                    ticker=row["ticker"],
                    side=row["side"],
                    count=int(row["count"]),
                    avg_price=float(row["avg_price"]),
                    cost_basis=float(row["cost_basis"]),
                    market_price=float(row["market_price"]),
                    unrealized_pnl=float(row["unrealized_pnl"]),
                    realized_pnl=float(row["realized_pnl"]),
                    close_time=close_time_val,
                )
            )
        return positions

    async def mark_to_market(
        self,
        ticker: str,
        current_price: float,
        *,
        close_time: datetime.datetime | None = None,
    ) -> None:
        """Update unrealized P&L for a position based on current market price.

        Args:
            ticker: Market ticker.
            current_price: Current YES price in dollars.
            close_time: Optional datetime for the market's close/settlement time.
        """
        cursor = await self._conn.execute(
            "SELECT * FROM paper_positions WHERE ticker = ? AND count > 0",
            (ticker,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return

        close_time_str: str | None = None
        if close_time is not None:
            close_time_str = close_time.isoformat()

        async with self._db.transaction():
            for row in rows:
                count = int(row["count"])
                avg_price = float(row["avg_price"])
                side = row["side"]

                stored_price = current_price if side == "yes" else 1 - current_price
                unrealized = (stored_price - avg_price) * count
                if close_time_str is not None:
                    await self._conn.execute(
                        """UPDATE paper_positions
                           SET market_price = ?, unrealized_pnl = ?,
                               close_time = COALESCE(?, close_time),
                               updated_at = datetime('now')
                           WHERE ticker = ? AND side = ?""",
                        (stored_price, unrealized, close_time_str, ticker, side),
                    )
                else:
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
        from gimmes.store.queries import (
            count_opened_closed,
            fill_resolved_outcome,
            log_settlement_close,
            settlement_outcome,
        )

        async with self._db.transaction():
            # #751 review: read INSIDE the transaction (BEGIN
            # IMMEDIATE) — with settle now reachable from
            # risk-check/order/validate (not just `positions`), a
            # pre-transaction read was a TOCTOU window where a
            # concurrent settle of the same position could
            # double-credit the payout.
            cursor = await self._conn.execute(
                "SELECT * FROM paper_positions"
                " WHERE ticker = ? AND count > 0",
                (ticker,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return

            for row in rows:
                side = row["side"]
                count = int(row["count"])
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

                # #653: settlements are real outcomes — write the close
                # trade at settlement value so the lifetime scorecard
                # sees the W/L (previously only the balance knew).
                # #663: clamp to the LEDGER residual — a resting sell
                # logged its close row at placement without reducing
                # the paper position, so a full-count settlement row
                # would double-count the close in daily P&L (and the
                # daily-loss trigger). opened == 0 (no local trade
                # history: seeded/legacy positions) keeps the
                # full-count behavior.
                opened, closed = await count_opened_closed(
                    self._db, ticker, side,
                )
                ledger_count = (
                    count if opened <= 0
                    else min(count, opened - closed)
                )
                # (ledger_count < count implies opened > 0 — the
                # no-history branch sets ledger_count = count.)
                if ledger_count < count:
                    # Auditable divergence: the balance is credited
                    # for the full broker count while the ledger row
                    # covers less (a placement-time close row for a
                    # never-filled resting sell also lands here).
                    import logging

                    logging.getLogger("gimmes").warning(
                        "settlement close for %s %s clamped to ledger"
                        " residual: opened=%d closed=%d broker"
                        " count=%d -> row count=%d (#663)",
                        ticker, side, opened, closed, count,
                        max(ledger_count, 0),
                    )
                if ledger_count > 0:
                    await log_settlement_close(
                        self._db, ticker=ticker, side=side,
                        count=ledger_count, won=won,
                    )
                else:
                    # The ledger already covers the opens (e.g. a
                    # placement-time close row for a resting sell) —
                    # skip the trade row but keep the outcome
                    # authoritative.
                    await fill_resolved_outcome(
                        self._db, ticker,
                        settlement_outcome(side, won),
                    )

            # #653: remove the mirror row in the main positions table
            # inside the SAME transaction — otherwise the next
            # sync_positions sees the ticker vanish from the broker and
            # writes a duplicate mark-priced reconcile drift close.
            await self._conn.execute(
                "DELETE FROM positions WHERE ticker = ?", (ticker,)
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _reject_order(
        self,
        order_id: str,
        params: CreateOrderParams,
        now: datetime.datetime,
        reason: str = "",
    ) -> Order:
        """Record a canceled order and return it (#690: with a
        nameable cause for the CLI/agent contract)."""
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
            reason=reason,
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
            f.count * f.price for f in fill_result.fills
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
