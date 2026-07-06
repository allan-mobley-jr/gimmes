"""P&L calculation from trade history."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gimmes.strategy.fees import fee_for_order

_log = logging.getLogger(__name__)


@dataclass
class PnLSummary:
    """Profit and loss summary."""

    total_trades: int = 0
    open_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    scratch_trades: int = 0
    gross_pnl: float = 0.0
    total_fees: float = 0.0
    net_pnl: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    @property
    def win_rate(self) -> float:
        completed = self.winning_trades + self.losing_trades
        if completed == 0:
            return 0.0
        return self.winning_trades / completed


def calculate_pnl(trades: list[dict]) -> PnLSummary:  # type: ignore[type-arg]  # accepts TradeRecord dicts
    """Calculate P&L from a list of trade records.

    Groups by ``(ticker, side)`` and walks each group in timestamp-ascending
    order, maintaining a running weighted-average cost basis. ``size_up`` is
    treated as an additional open and rolls into the average. Each ``close``
    matches against the residual position at the running average cost.

    A close that exceeds the residual position is logged as an orphan and
    contributes \\$0 P&L for the unmatched portion (the matched portion still
    gets real P&L). This avoids the prior bug where empty ``open_list``
    defaulted ``open_price`` to 0.0 and inflated P&L by ``close_price * count``.

    Note: ``get_trades`` returns rows in ``timestamp DESC``; this function
    re-sorts ascending so the caller doesn't have to coordinate ordering.
    """
    summary = PnLSummary()

    # Group by (ticker, side); ignore non-actionable rows (skips).
    groups: dict[tuple[str, str], list[dict]] = {}  # type: ignore[type-arg]
    for t in trades:
        action = t.get("action", "")
        if action not in ("open", "close", "size_up"):
            continue
        key = (t.get("ticker", ""), t.get("side", "yes"))
        groups.setdefault(key, []).append(t)

    for (ticker, side), events in groups.items():
        events.sort(key=lambda e: str(e.get("timestamp", "")))
        remaining = 0
        avg_cost = 0.0

        # #653: resolution outcome propagated across the group — the
        # outcome is usually recorded on the OPEN row (Monitor's
        # log-outcome ran before any drift close existed), never on the
        # reconcile close itself.
        group_outcome = next(
            (
                e.get("resolved_outcome")
                for e in events
                if e.get("resolved_outcome") in ("yes", "no")
            ),
            None,
        )
        # #663: a settlement close in the group means settlements were
        # properly recorded for this position — any reconcile drift
        # row alongside it is genuinely NON-settlement drift (e.g. a
        # manual exit) and must keep its mark, not be repriced.
        group_has_settlement = any(
            e.get("agent") == "settlement" and e.get("action") == "close"
            for e in events
        )

        for e in events:
            action = e["action"]
            count = int(e.get("count", 0) or 0)
            price = float(e.get("price", 0.0) or 0.0)

            # #653: a reconcile drift close is priced at the last-known
            # mark, not the broker-confirmed outcome. When the market's
            # resolution is known, reprice at settlement value — this is
            # how championship-mode settlements (which arrive as drift)
            # enter the scorecard correctly. Without a known outcome the
            # mark stands (genuine non-settlement drift).
            if (
                action == "close"
                and e.get("agent") == "reconcile"
                and group_outcome is not None
                and not group_has_settlement
            ):
                price = 1.0 if side == group_outcome else 0.0

            if action in ("open", "size_up"):
                if count <= 0:
                    continue
                total = remaining + count
                avg_cost = (
                    (avg_cost * remaining + price * count) / total
                    if total
                    else 0.0
                )
                remaining = total
                continue

            # action == "close"
            if count <= 0:
                continue
            matched = min(count, remaining)
            orphan = count - matched
            pnl = (price - avg_cost) * matched if matched else 0.0
            if orphan:
                _log.warning(
                    "orphan close: ticker=%s side=%s count=%d remaining=%d",
                    ticker, side, count, remaining,
                )

            # Fees on matched volume for the open leg, full count for the close
            # leg (operator paid the close transaction in full regardless of
            # whether the open is on record).
            open_fee = (
                fee_for_order(matched, avg_cost) if matched and avg_cost > 0 else 0.0
            )
            close_fee = fee_for_order(count, price) if price > 0 else 0.0
            summary.total_fees += open_fee + close_fee
            summary.gross_pnl += pnl
            summary.total_trades += 1

            if pnl > 0:
                summary.winning_trades += 1
                summary.largest_win = max(summary.largest_win, pnl)
            elif pnl < 0:
                summary.losing_trades += 1
                summary.largest_loss = min(summary.largest_loss, pnl)
            else:
                summary.scratch_trades += 1

            remaining -= matched

        # Still-open residual: count once per (ticker, side) with carry, so
        # the prior open-only test (test_open_only_counted) keeps passing.
        # open_trades makes the summary internally consistent:
        # total = wins + losses + scratch + open (#653).
        if remaining > 0:
            summary.total_trades += 1
            summary.open_trades += 1

    summary.net_pnl = summary.gross_pnl - summary.total_fees
    return summary
