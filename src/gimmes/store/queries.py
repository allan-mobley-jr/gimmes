"""Named database queries for trades, positions, snapshots, errors."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import TypedDict

from pydantic import ValidationError

from gimmes.models.error import (
    ErrorCategory,
    ErrorLogEntry,
    ErrorSeverity,
)
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
            gimme_score, edge, kelly_fraction, rationale, thesis, reason,
            agent, order_id, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            trade.reason,
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
    ticker_prefix: bool = False,
    since: str | None = None,
) -> list[TradeRecord]:
    """Query trade decisions with optional filters.

    ``ticker_prefix=True`` switches the ticker filter from exact match
    to prefix match (``ticker LIKE <bound_ticker> || '%'``) for the
    CLI's ``gimmes trades --ticker`` command — programmatic callers
    (P&L reports, etc.) default to exact match to preserve their
    semantics. ``since`` (#686) bounds rows to ``timestamp >=`` the
    given ISO instant, normalized via ``datetime()`` so legacy
    space-format rows compare correctly (the #680 lesson).
    """
    query = "SELECT * FROM trades WHERE 1=1"
    params: list[object] = []

    if ticker:
        if ticker_prefix:
            query += " AND ticker LIKE ? || '%'"
        else:
            query += " AND ticker = ?"
        params.append(ticker)
    if action:
        query += " AND action = ?"
        params.append(action)
    if since:
        query += " AND datetime(timestamp) >= datetime(?)"
        params.append(since)

    # space->T normalization + id tie-break (#661/#680 pattern):
    # mixed legacy/ISO formats mis-order raw string comparison, and a
    # wrong order here silently drops the NEWEST rows at the LIMIT.
    query += " ORDER BY replace(timestamp, ' ', 'T') DESC, id DESC LIMIT ?"
    params.append(limit)

    cursor = await db.conn.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def update_trade_outcome(db: Database, ticker: str, outcome: str) -> int:
    """Set resolved_outcome for all trades matching a ticker.

    #760: delegates to the conflict-correcting fill_resolved_outcome —
    a later AUTHORITATIVE outcome now corrects a wrong earlier one
    instead of silently updating 0 rows (the old IS NULL clause left
    KXPCECORE-26JUL-T0.3 split-brained: 138 premature rows that a
    correct post-settlement log-outcome could never fix).

    Returns:
        Number of rows updated (NULL-fills plus corrections).
    """
    updated = await fill_resolved_outcome(db, ticker, outcome)
    await db.conn.commit()
    return updated


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


_UPSERT_POSITION_SQL = """INSERT INTO positions
    (ticker, title, side, count, avg_price, market_price,
     cost_basis, market_value, unrealized_pnl, realized_pnl, close_time)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(ticker) DO UPDATE SET
     title=excluded.title, side=excluded.side, count=excluded.count,
     avg_price=excluded.avg_price, market_price=excluded.market_price,
     cost_basis=excluded.cost_basis, market_value=excluded.market_value,
     unrealized_pnl=excluded.unrealized_pnl, realized_pnl=excluded.realized_pnl,
     close_time=COALESCE(excluded.close_time, close_time),
     updated_at=datetime('now')"""


def _position_params(pos: Position) -> tuple:
    close_time_str = (
        pos.close_time.isoformat() if pos.close_time is not None else None
    )
    return (
        pos.ticker, pos.title, pos.side, pos.count,
        pos.avg_price, pos.market_price, pos.cost_basis,
        pos.market_value, pos.unrealized_pnl, pos.realized_pnl,
        close_time_str,
    )


async def upsert_position(db: Database, pos: Position) -> None:
    """Insert or update a position."""
    await db.conn.execute(_UPSERT_POSITION_SQL, _position_params(pos))
    await db.conn.commit()


async def set_position_rules_snapshot(
    db: Database, *, ticker: str, rules_primary: str,
) -> bool:
    """Persist the market's settlement language on an existing position
    row (#643). UPDATE-only by design: never inserts a stub row (a
    count=0 stub would be swept by the next position sync and generate
    a bogus synthetic close via reconcile), and the column is not part
    of the position upsert, so syncs without market data can't wipe it.

    Returns True when a row was updated; False when ``rules_primary``
    is empty or no position row exists (e.g. a resting order that
    hasn't filled).
    """
    if not rules_primary:
        return False
    cursor = await db.conn.execute(
        "UPDATE positions SET rules_primary = ? WHERE ticker = ?",
        (rules_primary, ticker),
    )
    await db.conn.commit()
    return cursor.rowcount > 0


async def get_position_rules_snapshot(db: Database, ticker: str) -> str | None:
    """Read the settlement-language snapshot for a position (#643).

    Returns None when no position row exists; empty string when the
    row exists but no snapshot was captured.
    """
    cursor = await db.conn.execute(
        "SELECT rules_primary FROM positions WHERE ticker = ?", (ticker,),
    )
    row = await cursor.fetchone()
    return None if row is None else row["rules_primary"]


async def _sync_positions_rows(
    db: Database, positions: list[Position],
) -> list[Position]:
    """Core position sync SQL. Caller manages the transaction.

    Returns the list of `Position` objects that were REMOVED from the
    DB (positions present in the DB but absent from the new sync set).
    Callers use this to log synthetic close trades for reconcile drift
    (#609) — without that, the removed ticker drops out of
    `known_markets` and Caddie Master's #586 lockout query passes
    silently with no lockout in effect.
    """
    current_tickers = {p.ticker for p in positions}
    cursor = await db.conn.execute("SELECT * FROM positions")
    rows = await cursor.fetchall()
    removed: list[Position] = []
    for row in rows:
        if row["ticker"] not in current_tickers:
            close_time_val = (
                row["close_time"] if "close_time" in row.keys() else None
            )
            close_time_dt = None
            if close_time_val:
                try:
                    close_time_dt = datetime.fromisoformat(close_time_val)
                except (ValueError, TypeError):
                    pass
            removed.append(
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
                    close_time=close_time_dt,
                )
            )
            await db.conn.execute(
                "DELETE FROM positions WHERE ticker = ?", (row["ticker"],)
            )
    for pos in positions:
        await db.conn.execute(_UPSERT_POSITION_SQL, _position_params(pos))
    return removed


async def _log_reconcile_closes(
    db: Database,
    removed: list[Position],
    *,
    exclude_ticker: str | None = None,
) -> list[ErrorLogEntry]:
    """Write a synthetic close trade + decision note for each removed
    position (#609 — reconcile-driven drift).

    Without these synthetic rows, a position closed off-CLI (manual
    Kalshi UI close, broker liquidation, settlement, API divergence
    corrected by reconcile) drops out of `known_markets` and Caddie
    Master's #586 stop-loss reopen lockout cannot resolve the ticker —
    legitimate stop-loss closes would be silently un-locked.

    The synthetic decision note uses `Trigger: Reconcile-divergence`
    (NOT `Trigger: Stop-loss breach`), so the #586 lockout query
    correctly does NOT fire on reconcile-driven closes — allowing
    legitimate re-entry after broker drift.

    Caller MUST invoke inside an active transaction. The synthetic
    rows use commit-less helpers so they participate in the caller's
    transaction.
    """
    cycle = _cycle_from_env()
    corrupt: list[ErrorLogEntry] = []

    for pos in removed:
        if exclude_ticker and pos.ticker == exclude_ticker:
            continue
        # #653 guard: if the ticker/side's closes already cover its
        # opens (residual <= 0 — e.g. the paper broker just wrote a
        # settlement close in this same sync window), do NOT write a
        # duplicate drift close — that was how mark-priced phantom
        # rows were born. Quantity-based, not timestamp-based: a
        # partial close must NOT suppress the drift close for the
        # remaining contracts (#653 review).
        opened, closed = await count_opened_closed(db, pos.ticker, pos.side)
        if opened > 0 and closed >= opened:
            # Recorded closes already cover the recorded opens — a
            # position with NO trade history (opened == 0) still gets
            # its drift close (pre-#609 DBs, seeded positions).
            continue
        # #684: the drift close covers the LEDGER residual, not
        # pos.count — a clamped settlement close (record count <
        # residual, an off-ledger exit) leaves a remainder that must
        # not be over-closed, and a stale pos.count must not inflate
        # the group either. opened == 0 keeps the pos.count behavior.
        drift_count = (opened - closed) if opened > 0 else pos.count
        # Use last-known mark (market_price or avg_price) as the close
        # price rather than 0.0 — keeps `get_daily_pnl`'s realized-P&L
        # math honest. Documented in the rationale so audit can see
        # this isn't a broker-confirmed fill.
        close_price = pos.market_price if pos.market_price else pos.avg_price
        # #656: carry the entry decision's analytics onto the synthetic
        # close so the trades table isn't blind to its own reasoning.
        entry = await get_entry_analytics(db, pos.ticker, pos.side) or {}

        def _build(analytics: dict) -> TradeDecision:  # type: ignore[type-arg]
            return TradeDecision(
                ticker=pos.ticker,
                action=TradeDecision.Action.CLOSE,
                side=pos.side,
                count=drift_count,
                price=close_price,
                model_probability=analytics.get("model_probability", 0.0),
                gimme_score=analytics.get("gimme_score", 0.0),
                edge=analytics.get("edge", 0.0),
                kelly_fraction=analytics.get("kelly_fraction", 0.0),
                rationale=(
                    "reconcile drift — broker removed position without"
                    " local close; price is last-known mark, not"
                    " broker-confirmed fill (#609)"
                ),
                agent="reconcile",
            )

        try:
            synth = _build(entry)
        except ValidationError as exc:
            # #668: same guard as log_settlement_close — corrupt
            # analytics must not abort the sync_positions transaction
            # every reconcile cycle. Retry before warning; if the
            # zeroed retry ALSO raises, the culprit is a caller value
            # (count/close_price from the positions row — the
            # Position model is unconstrained, and bounding it is
            # wrong: fee-inclusive avg_price legitimately exceeds
            # 1.0). #686: skip-and-escalate that single position
            # instead of aborting the whole sync — no lying row is
            # written, the healthy positions still get their closes,
            # and the error_log row is the triage breadcrumb
            # (insert_error commits, so callers write it AFTER the
            # transaction).
            try:
                synth = _build({})
            except ValidationError as caller_exc:
                fields = ", ".join(
                    str(e.get("loc", ("?",))[0])
                    for e in caller_exc.errors()
                )
                logging.getLogger(__name__).error(
                    "corrupt position values for %s/%s (%s) — drift"
                    " close SKIPPED for this position (#686)",
                    pos.ticker, pos.side, fields,
                )
                corrupt.append(ErrorLogEntry(
                    severity=ErrorSeverity.ERROR,
                    category=ErrorCategory.DATA_INTEGRITY,
                    error_code="corrupt_position_skipped",
                    component="store.queries",
                    agent="reconcile",
                    cycle=cycle,
                    message=(
                        f"Corrupt position values for {pos.ticker}/"
                        f"{pos.side} ({fields}) — reconcile drift"
                        f" close skipped (#686)"
                    ),
                    context=json.dumps({
                        "ticker": pos.ticker, "side": pos.side,
                        "count": pos.count,
                        "market_price": pos.market_price,
                        "fields": fields,
                    }),
                ))
                continue
            logging.getLogger(__name__).warning(
                "entry analytics for %s/%s failed validation (%s) —"
                " reconcile close recorded with zeroed analytics"
                " (#668)", pos.ticker, pos.side,
                ", ".join(
                    str(e.get("loc", ("?",))[0]) for e in exc.errors()
                ),
            )
        await _insert_trade_row(db, synth)

        # IMPORTANT: this body MUST NOT contain the literal string
        # `Trigger: Stop-loss breach` anywhere — not even in explanatory
        # text — because Caddie Master's #586 lockout query is a
        # substring match. Quoting the forbidden phrase here would
        # silently lock out legitimate re-entry after reconcile drift.
        body = (
            f"Decision: CLOSE\n"
            f"Trigger: Reconcile-divergence\n"
            f"Side: {pos.side}\n"
            f"Count: {drift_count}\n"
            f"Last-known mark: {close_price}\n"
            f"Rationale: broker reported position absent during reconcile;"
            f" local DB had it open. This is broker-divergence drift,"
            f" not an adverse price event. The #586 reopen lockout does"
            f" NOT apply to this close — re-entry is allowed on the next"
            f" cycle."
        )
        await db.conn.execute(
            "INSERT INTO position_notes"
            " (ticker, cycle, agent, note_type, body)"
            " VALUES (?, ?, ?, ?, ?)",
            (pos.ticker, cycle, "reconcile", "decision", body),
        )
    return corrupt


def _cycle_from_env() -> int:
    """Current cycle number from GIMMES_CYCLE (0 if unset or invalid)."""
    try:
        return int(os.environ.get("GIMMES_CYCLE", "0") or 0)
    except ValueError:
        return 0


def _session_id_from_env() -> int | None:
    """Session ID from GIMMES_SESSION_ID (None if unset or invalid)."""
    try:
        return int(os.environ["GIMMES_SESSION_ID"])
    except (KeyError, ValueError):
        return None


def settlement_outcome(side: str, won: bool) -> str:
    """Resolution outcome implied by a settlement result (#653).

    A winning YES position means the market resolved yes; a losing
    YES position means it resolved no; and vice versa for NO.
    """
    if won:
        return side
    return "no" if side == "yes" else "yes"


async def get_close_order_ledger(db: Database) -> list[dict]:  # type: ignore[type-arg]
    """Per-order close ledger for the championship true-up (#698).

    Groups non-settlement ``action='close'`` rows that carry an
    order_id, restricted to the repair window: tickers that still
    have a positions row, or rows younger than 7 days. Settled
    history beyond that is owned by the settlement path.
    """
    cursor = await db.conn.execute(
        """SELECT order_id,
                  MAX(ticker) AS ticker,
                  MAX(side) AS side,
                  MAX(agent) AS agent,
                  SUM(count) AS ledger_count
           FROM trades
           WHERE action = 'close' AND order_id != ''
             AND agent != 'settlement'
             AND (
               ticker IN (SELECT ticker FROM positions)
               OR datetime(timestamp) >= datetime('now', '-7 days')
             )
           GROUP BY order_id
           ORDER BY MAX(id) DESC"""
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def annul_close_rows(db: Database, order_id: str, marker: str) -> int:
    """#698 (reusing the #690 shape): annul every close row of a
    terminal never-filled order — append-only ledger, no DELETE.
    ``reason='order_canceled'`` keeps the rows out of residual math
    (count_opened_closed sums open/size_up/close only) and out of
    the missed-entry FNR (NON_ENTRY_SKIP_REASONS)."""
    cursor = await db.conn.execute(
        """UPDATE trades SET action = 'skip',
           reason = 'order_canceled',
           rationale = rationale || ?
           WHERE order_id = ? AND action = 'close'""",
        (marker, order_id),
    )
    await db.conn.commit()
    return cursor.rowcount


async def shrink_newest_close_row(
    db: Database, order_id: str, excess: int, marker: str,
) -> bool:
    """#698: shrink the newest close row of a partially-filled
    terminal order by the unfilled excess. Returns False when the
    newest row is smaller than the excess (multi-row overstatement —
    warned by the caller, left for manual repair)."""
    cursor = await db.conn.execute(
        """SELECT id, count FROM trades
           WHERE order_id = ? AND action = 'close'
           ORDER BY id DESC LIMIT 1""",
        (order_id,),
    )
    row = await cursor.fetchone()
    if row is None or int(row["count"]) < excess:
        return False
    if int(row["count"]) == excess:
        await db.conn.execute(
            """UPDATE trades SET action = 'skip',
               reason = 'order_canceled',
               rationale = rationale || ?
               WHERE id = ?""",
            (marker, row["id"]),
        )
    else:
        await db.conn.execute(
            """UPDATE trades SET count = count - ?,
               rationale = rationale || ?
               WHERE id = ?""",
            (excess, marker, row["id"]),
        )
    await db.conn.commit()
    return True


async def has_settlement_close(
    db: Database, ticker: str, side: str
) -> bool:
    """True when an agent='settlement' close exists for ticker/side.

    A market settles once — used by the #684 consumption idempotency
    guard (the count clamp broke the old residual-based idempotency:
    a retry after a clamped write would book the off-ledger drift
    remainder at settlement value).
    """
    cursor = await db.conn.execute(
        """SELECT 1 FROM trades
           WHERE ticker = ? AND side = ? AND action = 'close'
             AND agent = 'settlement' LIMIT 1""",
        (ticker, side),
    )
    return await cursor.fetchone() is not None


async def count_opened_closed(
    db: Database, ticker: str, side: str
) -> tuple[int, int]:
    """Contract counts (opened, closed) for a ticker/side in the trades log.

    Opened sums `open` + `size_up` rows; closed sums `close` rows.
    Used by the #653 reconcile dup-guard and the backfill residual.
    """
    cursor = await db.conn.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN action IN ('open','size_up')
               THEN count END), 0) AS opened,
             COALESCE(SUM(CASE WHEN action = 'close'
               THEN count END), 0) AS closed
           FROM trades WHERE ticker = ? AND side = ?
             AND action IN ('open','size_up','close')""",
        (ticker, side),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0, 0
    return int(row["opened"] or 0), int(row["closed"] or 0)


async def fill_resolved_outcome(
    db: Database, ticker: str, outcome: str
) -> int:
    """Set `resolved_outcome` on a ticker's trade rows — filling NULLs
    AND overwriting conflicting values.

    Settlement is the authoritative resolution source: an already-
    populated but WRONG outcome (Monitor's log-outcome was proven
    wrong at least once — the KXUE-UK26FEB-5.1 case) must be corrected,
    or read-time repricing would trust the stale outcome over the
    settlement truth. Idempotent when already correct (#653 review).

    Commit-less — the caller manages the transaction.
    """
    cursor = await db.conn.execute(
        "UPDATE trades SET resolved_outcome = ?"
        " WHERE ticker = ?"
        " AND (resolved_outcome IS NULL OR resolved_outcome != ?)",
        (outcome, ticker, outcome),
    )
    return cursor.rowcount


async def log_settlement_close(
    db: Database,
    *,
    ticker: str,
    side: str,
    count: int,
    won: bool,
    timestamp: datetime | None = None,
    rationale: str | None = None,
) -> int:
    """Write a settlement close trade + decision note (#653).

    Settlements are broker-confirmed outcomes — priced at exactly 1.0
    (win) or 0.0 (loss), fee-free (Kalshi charges no settlement fee;
    `calculate_fee` returns 0 outside 0<price<1 automatically), and
    tagged `agent='settlement'` so they are distinguishable from both
    intentional agent closes and reconcile drift. Settlement closes
    COUNT toward daily P&L (unlike `agent='reconcile'` drift, #622): a
    settlement loss is real realized money lost that day.

    Also sets `resolved_outcome` on the ticker's trade rows — filling
    NULLs AND correcting conflicting values, since settlement IS the
    authoritative resolution event (Monitor's log-outcome has been
    wrong before) and the scorecard must not depend on its timing.

    Commit-less: caller manages the transaction. Returns the trade
    row id.
    """
    cycle = _cycle_from_env()

    price = 1.0 if won else 0.0
    # #656: carry the entry decision's analytics onto the settlement
    # close so calibration audits can read entry vs outcome per row.
    entry = await get_entry_analytics(db, ticker, side)
    if entry is None:
        # No entry on record (pre-#653 data, seeded positions) — the
        # close keeps honest zeros, but log it so a calibration gap is
        # traceable to missing history rather than a broken writer.
        logging.getLogger(__name__).debug(
            "settlement close for %s/%s found no entry row —"
            " analytics default to 0 (#656)", ticker, side,
        )
        entry = {}

    def _build(analytics: dict) -> TradeDecision:  # type: ignore[type-arg]
        return TradeDecision(
            ticker=ticker,
            action=TradeDecision.Action.CLOSE,
            side=side,
            count=count,
            price=price,
            model_probability=analytics.get("model_probability", 0.0),
            gimme_score=analytics.get("gimme_score", 0.0),
            edge=analytics.get("edge", 0.0),
            kelly_fraction=analytics.get("kelly_fraction", 0.0),
            rationale=rationale or (
                "market settled — broker-confirmed outcome; close at"
                " settlement value (#653)"
            ),
            agent="settlement",
        )

    try:
        synth = _build(entry)
    except ValidationError as exc:
        # #668: corrupt stored analytics (manual SQL / legacy rows)
        # must not abort the settlement transaction — that would
        # re-fail every settle cycle. Honest zeros over fabricated
        # in-range values. Retry BEFORE warning: caller-supplied
        # fields (count, side) are validated too, and if one of those
        # is the culprit the zeroed retry re-raises — fail-loud is
        # correct there, and the log must not claim a row was written.
        synth = _build({})
        logging.getLogger(__name__).warning(
            "entry analytics for %s/%s failed validation (%s) —"
            " settlement close recorded with zeroed analytics (#668)",
            ticker, side,
            ", ".join(
                str(e.get("loc", ("?",))[0]) for e in exc.errors()
            ),
        )
    if timestamp is not None:
        synth.timestamp = timestamp
    row_id = await _insert_trade_row(db, synth)

    # resolved_outcome is derivable from the settlement itself:
    # a winning YES position means the market resolved yes, etc.
    outcome = settlement_outcome(side, won)
    await fill_resolved_outcome(db, ticker, outcome)

    # IMPORTANT: this body MUST NOT contain the literal string
    # `Trigger: Stop-loss breach` (the #586 lockout query is a
    # substring match — see _log_reconcile_closes above).
    body = (
        f"Decision: CLOSE\n"
        f"Trigger: Settlement\n"
        f"Side: {side}\n"
        f"Count: {count}\n"
        f"Settlement value: {price}\n"
        f"Rationale: market resolved {outcome}; position settled by the"
        f" broker at {price}. This is a settlement outcome, not an"
        f" adverse price event — the #586 reopen lockout does NOT apply."
    )
    await db.conn.execute(
        "INSERT INTO position_notes"
        " (ticker, cycle, agent, note_type, body)"
        " VALUES (?, ?, ?, ?, ?)",
        (ticker, cycle, "settlement", "decision", body),
    )
    return row_id


async def sync_positions(db: Database, positions: list[Position]) -> None:
    """Replace all positions with the given list.

    Runs as a single atomic transaction — clears stale positions and upserts
    current ones so the local DB always reflects the API/broker state.
    Removed positions get a synthetic close trade + reconcile-divergence
    decision note so they remain resolvable via `known_markets` (#609).
    """
    async with db.transaction():
        removed = await _sync_positions_rows(db, positions)
        corrupt = await _log_reconcile_closes(db, removed)
    # #686: insert_error commits, so corrupt-position escalations are
    # written AFTER the transaction — if the sync rolled back, the
    # exception propagates first and no orphan error rows describe a
    # sync that never committed.
    for entry in corrupt:
        await insert_error(db, entry)


async def sync_positions_with_trade(
    db: Database, positions: list[Position], trade: TradeDecision
) -> int:
    """Atomically sync positions and log a trade decision.

    Ensures position state and trade log are always consistent — if either
    operation fails, both are rolled back.  Returns the trade row ID.

    If OTHER tickers are also being removed (multi-ticker drift in the
    same sync), they get synthetic close trades + reconcile-divergence
    decision notes via `_log_reconcile_closes`. The caller's trade
    ticker is excluded — it gets logged explicitly below.
    """
    async with db.transaction():
        removed = await _sync_positions_rows(db, positions)
        corrupt = await _log_reconcile_closes(
            db, removed, exclude_ticker=trade.ticker,
        )
        row_id = await _insert_trade_row(db, trade)
    # #686: see sync_positions — error escalations post-transaction.
    for entry in corrupt:
        await insert_error(db, entry)
    return row_id


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


async def get_tickers_missing_rules(db: Database) -> list[str]:
    """Open positions with no settlement-language snapshot (#647):
    resting fills, pre-v17 rows, and sync DELETE/re-add all leave
    rules_primary empty, degrading the semantics guard to silent
    pass."""
    cursor = await db.conn.execute(
        """SELECT ticker FROM positions
           WHERE (rules_primary IS NULL OR rules_primary = '')
             AND count > 0
           ORDER BY ticker"""
    )
    rows = await cursor.fetchall()
    return [r["ticker"] for r in rows]


async def get_positions(db: Database) -> list[Position]:
    """Get all stored positions.

    Logs a warning if positions are stale (updated_at older than the most
    recent trade timestamp), which can happen after a crash between trade
    insertion and position sync.
    """
    await _check_position_staleness(db, "positions")

    cursor = await db.conn.execute("SELECT * FROM positions WHERE count > 0")
    rows = await cursor.fetchall()
    positions = []
    for row in rows:
        close_time_val = row["close_time"] if "close_time" in row.keys() else None
        close_time_dt = None
        if close_time_val:
            try:
                close_time_dt = datetime.fromisoformat(close_time_val)
            except (ValueError, TypeError):
                pass
        positions.append(
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
                close_time=close_time_dt,
            )
        )
    return positions


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
    recommendation: str = "",
) -> int:
    """Insert a scanned gimme candidate with optional component scores.

    Returns the row ID of the inserted candidate.
    """
    cursor = await db.conn.execute(
        """INSERT INTO candidates
           (ticker, title, market_price, model_probability, edge, gimme_score,
            research_memo, cap_blocked, edge_size_score, signal_strength_score,
            liquidity_depth_score, settlement_clarity_score,
            time_to_resolution_score, recommendation)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, title, market_price, model_prob, edge, score, memo,
         int(cap_blocked), edge_size_score, signal_strength_score,
         liquidity_depth_score, settlement_clarity_score,
         time_to_resolution_score, recommendation),
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


# Canonical daily-P&L SQL (#622: reconcile drift excluded; #653:
# settlement closes included). Shared with the read-only clubhouse
# (clubhouse/data.py get_risk) so the dashboard's risk panel can never
# drift from the daily-loss-limit trigger the loop reads (#680).
# #695: side-scoped open matching + replace(' ','T') timestamp
# normalization in BOTH the WHERE and the ORDER BY — the raw string
# compare let a LATER space-format open match an EARLIER ISO close
# (' ' < 'T'), and the raw sort ranked every ISO row above every
# space-format row, picking the wrong "most recent" open.
DAILY_PNL_SQL = """SELECT COALESCE(SUM(
            (c.price - COALESCE(
                (SELECT price FROM trades o
                 WHERE o.ticker = c.ticker
                   AND o.side = c.side
                   AND o.action = 'open'
                   AND replace(o.timestamp, ' ', 'T')
                       <= replace(c.timestamp, ' ', 'T')
                 ORDER BY replace(o.timestamp, ' ', 'T') DESC,
                          o.id DESC
                 LIMIT 1),
            0)) * c.count
        ), 0) as daily_pnl
        FROM trades c
        WHERE c.action = 'close'
          AND c.agent != 'reconcile'
          AND date(c.timestamp) = date(?)"""


async def get_daily_pnl(db: Database, *, today: str | None = None) -> float:
    """Calculate realized P&L from close trades for a given date (defaults to today).

    For each close trade on the target date, finds the most recent open trade
    on the same ticker AND SIDE that occurred before the close (#695 —
    sides are independent positions; timestamps compare after
    replace(' ', 'T') normalization so legacy space-format rows order
    chronologically, the #661/#680 pattern), then computes:
    (close_price - open_price) * count.

    Intentional approximation (#695): the entry price is the MOST
    RECENT open's price, not a size-weighted average across scale-ins
    (size_up rows are excluded from the subquery). After a scale-in,
    per-contract P&L is measured against the last full open, not true
    average cost. Deliberate — this value feeds the real-capital
    daily-loss trigger (check_daily_loss) and the clubhouse risk panel
    via the shared constant (#680), and both must move together; do
    not change the averaging here without revisiting the trigger
    semantics.

    Synthetic reconcile-divergence close trades (agent='reconcile', written
    by `_log_reconcile_closes` per #609) are EXCLUDED from daily P&L because
    they represent broker-side drift, not intentional realized trading P&L.
    Settlement closes (agent='settlement', #653) ARE included: a settlement
    loss is real realized money lost that day and the daily-loss trigger
    should see it.
    Including them distorts the daily-loss-limit trigger that the autonomous
    loop reads from this value — a reconcile-driven close at the last-known
    mark would show as realized loss/gain the operator did not actually
    take (#622).

    Args:
        db: Database connection.
        today: Optional date string (YYYY-MM-DD) to use instead of 'now'.
    """
    cursor = await db.conn.execute(
        DAILY_PNL_SQL,
        ("now" if today is None else today,),
    )
    row = await cursor.fetchone()
    return float(row["daily_pnl"]) if row else 0.0


async def get_deployed_cost_basis(db: Database) -> float:
    """Total cost basis of all open positions.

    #743: resting rest-on-miss BUYs count as deployed — their notional
    is reserved out of the balance and fills without a re-check, so a
    bankroll gate that ignored them could be stacked past the cap by
    successive resting opens.
    """
    cursor = await db.conn.execute(
        "SELECT COALESCE(SUM(cost_basis), 0) AS deployed"
        " FROM positions WHERE count > 0"
    )
    row = await cursor.fetchone()
    deployed = float(row["deployed"]) if row else 0.0

    try:
        cursor = await db.conn.execute(
            "SELECT COALESCE(SUM(remaining_count"
            " * MAX(yes_price, no_price)), 0) AS reserved"
            " FROM paper_orders"
            " WHERE status = 'resting' AND action = 'buy'"
        )
        row = await cursor.fetchone()
        if row:
            deployed += float(row["reserved"]) / 100.0  # cents -> dollars
    except Exception:
        # Championship DBs have no paper tables — resting exposure is
        # a paper-mode concept (#743 scopes rest-on-miss to paper).
        pass

    return deployed


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


# #768: a failed BUY attempt is terminal for its candidate for the rest of
# the cycle — for every agent session, not just the one that failed. The
# marker lives in activity_log because trades has no cycle column.
ORDER_TERMINAL_MARKER_PREFIX = "Order attempt terminal:"


def order_terminal_marker(ticker: str) -> str:
    """Activity-log message marking a terminal order attempt (#768)."""
    return f"{ORDER_TERMINAL_MARKER_PREFIX} {ticker}"


async def has_terminal_order_attempt(db: Database, ticker: str, cycle: int) -> bool:
    """True if a terminal order-attempt marker exists for ticker+cycle (#768).

    Bounded to 24h so a same-numbered cycle from an earlier loop run
    (fresh-DB restart) can never match — same bounding as #761.
    """
    cursor = await db.conn.execute(
        "SELECT 1 FROM activity_log"
        " WHERE cycle = ? AND message = ?"
        " AND timestamp >= datetime('now', '-1 day') LIMIT 1",
        (cycle, order_terminal_marker(ticker)),
    )
    return await cursor.fetchone() is not None


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
        "SELECT * FROM candidates ORDER BY scanned_at DESC, id DESC LIMIT ?", (limit,)
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


async def get_shadow_verdict_for_ticker(
    db: Database, ticker: str,
) -> tuple[str | None, int]:
    """The shadow distance verdict from the ticker's memos (#769).

    Returns ``(verdict, candidate_rows)``. Scans the newest candidate
    rows (not just the newest — a #676-style bookkeeping row logged
    after research must not shadow a researched memo's verdict) and
    returns the first recognizable verdict. Hourly tickers embed
    date+hour, so same-ticker rows are same-window by construction.
    ``candidate_rows`` is the number of recent rows fetched (up to 10,
    regardless of where the verdict was found) — it lets the caller's
    audit trail distinguish "no candidate at all" (0) from "candidates
    exist but none parseable".
    """
    from gimmes.store.observation_validator import parse_shadow_verdict

    rows = await get_candidate_for_ticker(db, ticker, limit=10)
    for row in rows:
        verdict = parse_shadow_verdict(row["research_memo"] or "")
        if verdict is not None:
            return verdict, len(rows)
    return None, len(rows)


async def get_candidate_for_ticker(
    db: Database, ticker: str, *, limit: int = 1,
    scored_only: bool = False,
) -> list[dict]:
    """Return the most recent candidate row(s) for a specific ticker.

    ``scored_only`` (#676) skips bookkeeping rows (prob/price 0 — the
    market-info-failure rows caddie.md/scout.md mandate) so the true
    newest SCORING is found no matter how many failure rows stack
    above it. The predicate mirrors detect_candidate_flip's degenerate
    guard, which remains the policy backstop.
    """
    query = "SELECT * FROM candidates WHERE ticker = ?"
    if scored_only:
        query += " AND model_probability > 0 AND market_price > 0"
    query += " ORDER BY scanned_at DESC, id DESC LIMIT ?"
    cursor = await db.conn.execute(query, (ticker, limit))
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_open_trade_for_ticker(
    db: Database, ticker: str, side: str | None = None,
) -> dict | None:  # type: ignore[type-arg]
    """Return the most recent open trade record for a ticker, or None.

    #695: normalized ordering (the #661 pattern) so legacy space-
    format rows rank chronologically, and an optional ``side`` scope
    for side='both' holdings (the size_up thesis-inheritance caller
    must not read the other leg's thesis).
    """
    query = "SELECT * FROM trades WHERE ticker = ? AND action = 'open'"
    params: list[object] = [ticker]
    if side:
        query += " AND side = ?"
        params.append(side)
    query += " ORDER BY replace(timestamp, ' ', 'T') DESC, id DESC LIMIT 1"
    cursor = await db.conn.execute(query, params)
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_entry_analytics(
    db: Database, ticker: str, side: str,
) -> dict | None:  # type: ignore[type-arg]
    """Return the latest open/size_up row's analytics for a ticker/side.

    Carries entry-time analytics (model probability, score, edge, kelly
    fraction) onto close rows so `gimmes trades` and calibration audits
    see the entry decision that produced the close (#656). Synthetic
    closes otherwise inherit TradeDecision's 0.0 defaults, which left
    the trades table blind to its own entry reasoning.

    Commit-less: safe inside a caller's transaction.
    """
    cursor = await db.conn.execute(
        "SELECT model_probability, gimme_score, edge, kelly_fraction"
        " FROM trades WHERE ticker = ? AND side = ?"
        " AND action IN ('open', 'size_up')"
        " ORDER BY replace(timestamp, ' ', 'T') DESC, id DESC LIMIT 1",
        (ticker, side),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_last_entry_trade(
    db: Database, ticker: str, side: str | None = None,
) -> dict | None:  # type: ignore[type-arg]
    """The ticker's most recent open OR size_up row, or None (#661).

    The round-trip churn anchor: a position sized up 10 minutes ago
    round-trips most of its capital even if the original open is
    hours old, so size_up rows count as entries. ``side`` scopes the
    anchor to the leg being closed (#678): under side='both' holdings
    the ticker's most recent entry may belong to the OTHER side, and
    the anchor is persisted as the round-trip grouping key in the
    churn audit rows.
    """
    query = (
        "SELECT price, timestamp FROM trades"
        " WHERE ticker = ? AND action IN ('open', 'size_up')"
    )
    params: list[object] = [ticker]
    if side:
        query += " AND side = ?"
        params.append(side)
    query += " ORDER BY replace(timestamp, ' ', 'T') DESC, id DESC LIMIT 1"
    cursor = await db.conn.execute(query, params)
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_last_close_row(
    db: Database, ticker: str,
) -> dict | None:  # type: ignore[type-arg]
    """The ticker's most recent close row from ANY agent, or None.

    Unlike get_last_close_trade (#661, decision closes only), this
    includes settlement and reconcile closes — it answers "did this
    position ever close?" for the closed-position context read (#751).
    """
    cursor = await db.conn.execute(
        "SELECT price, timestamp, count, agent, side FROM trades"
        " WHERE ticker = ? AND action = 'close'"
        " ORDER BY replace(timestamp, ' ', 'T') DESC, id DESC LIMIT 1",
        (ticker,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_last_close_trade(
    db: Database, ticker: str,
) -> dict | None:  # type: ignore[type-arg]
    """The ticker's most recent DECISION close row, or None (#661).

    Exact ticker match — a prefix match would false-arm the reopen
    churn gate for sibling thresholds (T3.0 vs T3.05). Reconcile
    drift closes are excluded in SQL: a trailing drift row after a
    fresh decision close must not shadow it and disarm the gate
    (churn.py keeps its own agent check as defense in depth).
    Timestamps are normalized space->T in the ORDER BY so a legacy
    schema-default row can never outrank a newer ISO row; ``id DESC``
    breaks exact ties. ``side`` is returned so the gate can normalize
    denominations (#678): prices in the row remain side-effective, and
    check_reopen_churn flips the close price into the entry's terms
    before applying the band.
    """
    cursor = await db.conn.execute(
        "SELECT price, timestamp, agent, side FROM trades"
        " WHERE ticker = ? AND action = 'close'"
        " AND agent != 'reconcile'"
        " ORDER BY replace(timestamp, ' ', 'T') DESC, id DESC LIMIT 1",
        (ticker,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_last_close_times(
    db: Database, tickers: list[str],
) -> dict[str, str]:
    """Most recent non-reconcile close timestamp per ticker (#661).

    One grouped query for the candidates-table render. Reconcile
    closes are broker drift, not decisions — they never mark research
    stale.
    """
    if not tickers:
        return {}
    placeholders = ",".join("?" for _ in tickers)
    cursor = await db.conn.execute(
        f"SELECT ticker,"  # noqa: S608
        f" MAX(replace(timestamp, ' ', 'T')) AS last_close"
        f" FROM trades"
        f" WHERE action = 'close' AND agent != 'reconcile'"
        f" AND ticker IN ({placeholders})"
        f" GROUP BY ticker",
        tuple(tickers),
    )
    rows = await cursor.fetchall()
    return {row["ticker"]: row["last_close"] for row in rows}


async def get_position_close_times(
    db: Database, *, table: str = "positions",
) -> list[tuple[str, datetime]]:
    """Return (ticker, close_time) pairs for open positions with a known close_time."""
    if table not in _ALLOWED_POSITION_TABLES:
        raise ValueError(f"Invalid position table: {table}")

    from zoneinfo import ZoneInfo

    _utc = ZoneInfo("UTC")

    cursor = await db.conn.execute(
        f"SELECT ticker, close_time FROM {table}"  # noqa: S608
        " WHERE count > 0 AND close_time IS NOT NULL"
    )
    rows = await cursor.fetchall()
    results: list[tuple[str, datetime]] = []
    for row in rows:
        try:
            ct = datetime.fromisoformat(row["close_time"])
            # Normalize naive datetimes to UTC (Kalshi API returns UTC)
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=_utc)
            results.append((row["ticker"], ct))
        except (ValueError, TypeError):
            continue
    return results


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
    note_type: str | None = None,
) -> list[dict]:  # type: ignore[type-arg]
    """Return position notes for a ticker, newest first.

    When ``note_type`` is set, the type filter is applied before
    ``limit`` so chatty notes of other types can't evict matches (#580).
    """
    if note_type is None:
        sql = "SELECT * FROM position_notes WHERE ticker = ? ORDER BY id DESC LIMIT ?"
        params: tuple = (ticker, limit)
    else:
        sql = (
            "SELECT * FROM position_notes WHERE ticker = ? AND note_type = ?"
            " ORDER BY id DESC LIMIT ?"
        )
        params = (ticker, note_type, limit)
    cursor = await db.conn.execute(sql, params)
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
