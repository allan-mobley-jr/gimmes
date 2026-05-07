"""Phase 1a backtest of pause-vs-trade-coincidence and hour-of-window
distribution across all available cycle logs and the SQLite ``trades``
table.

GitHub issue #556 (parent: #546). The autonomous-loop's ``pause_seconds``
default has been suspected of being either too aggressive (burns budget)
or too lax (misses gimmes). Phase 0 (#555) established the audit
infrastructure for one day; this module extends the analysis across
every day with cycle logs on disk, joins to the ``candidates`` table to
find each placed-trade's first sighting, and bucketises the
first-seen-to-trade gap to answer the practical question: *how many
placed trades would a longer pause have missed?*

PnL counterfactuals (would a longer pause have *changed which trades got
placed*) require Kalshi historical orderbook data that this repo does
not persist; deferred to #553 (Phase 1b).

Read-only: opens ``${GIMMES_HOME}/gimmes.db`` with ``mode=ro`` so the
running autonomous loop is not perturbed.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("gimmes.pause_backtest")

ET = ZoneInfo("America/New_York")

# Gap buckets, lower-inclusive, upper-exclusive. ``upper=None`` is open-ended.
GAP_BUCKETS: tuple[tuple[str, float, float | None], ...] = (
    ("0-60s", 0.0, 60.0),
    ("60-300s", 60.0, 300.0),
    ("5-10min", 300.0, 600.0),
    ("10-30min", 600.0, 1800.0),
    ("30min+", 1800.0, None),
)

# Recommendation thresholds for the render_markdown verdict tree.
# Tunable — not specified in #546 or #556; chosen as conservative defaults
# until the Phase 1b PnL counterfactual (#553) can confirm or refute.
FAST_GAP_DO_NOT_RAISE_PCT = 30.0
FAST_GAP_LIKELY_SAFE_PCT = 5.0


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a possibly-suffixed ISO 8601 string to UTC datetime, or None."""
    if not value:
        return None
    try:
        # candidates.scanned_at uses the SQLite "datetime('now')" form
        # (e.g. "2026-05-07 09:26:51") with no tz; trades.timestamp uses
        # ISO with timezone. Normalize both.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        if "T" not in value and " " in value and "+" not in value:
            value = value.replace(" ", "T") + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class TradeBacktest:
    """One placed trade plus its first-sighting and timing metadata."""

    trade_id: int
    ticker: str
    action: str
    side: str
    trade_time: datetime
    first_seen_time: datetime | None
    gap_seconds: float | None
    hour_of_window_edt: int
    trade_window_name: str
    gimme_score: float | None
    edge: float | None
    cap_blocked_at_first_seen: bool


@dataclass(frozen=True)
class HourBucket:
    """Aggregate per EDT hour-of-day across all observed days."""

    hour_edt: int
    cycles_observed: int
    days_observed: int
    trades_placed: int
    trades_per_cycle: float


@dataclass(frozen=True)
class GapBucket:
    label: str
    lower_seconds: float
    upper_seconds: float | None
    count: int
    pct_of_total: float


@dataclass(frozen=True)
class BacktestSummary:
    trades: list[TradeBacktest]
    hour_buckets: list[HourBucket]
    gap_buckets: list[GapBucket]
    by_window_name: dict[str, int]
    date_from: date
    date_to: date
    cycles_audited: int
    trades_with_no_candidate: int
    warnings: list[str] = field(default_factory=list)


def collect_trades(
    db_path: Path,
    *,
    date_from: date,
    date_to: date,
    actions: tuple[str, ...] = ("open",),
    in_trade_window_fn=None,
) -> tuple[list[TradeBacktest], list[str]]:
    """Read placed trades from ``db_path`` and join each to its first
    candidate sighting. Returns ``(trades, warnings)``.

    Opens a single read-only SQLite connection for the entire backtest
    (one trades query plus one first-seen query per trade) so per-trade
    file-descriptor churn is bounded.
    """
    warnings: list[str] = []
    if not db_path.exists():
        return [], [f"DB not found: {db_path}"]

    start = datetime.combine(date_from, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(
        date_to + timedelta(days=1), datetime.min.time(), tzinfo=UTC,
    )

    placeholders = ", ".join("?" for _ in actions)
    out: list[TradeBacktest] = []
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            cur = conn.execute(
                f"""
                SELECT id, ticker, action, side, timestamp
                FROM trades
                WHERE timestamp >= ? AND timestamp < ?
                  AND action IN ({placeholders})
                ORDER BY timestamp
                """,
                (start.isoformat(), end.isoformat(), *actions),
            )
            rows = cur.fetchall()

            for trade_id, ticker, action, side, ts_str in rows:
                trade_time = _parse_iso(ts_str)
                if trade_time is None:
                    warnings.append(
                        f"trade #{trade_id} has unparseable timestamp"
                    )
                    continue

                first_seen, gimme_score, edge, cap_blocked = _first_seen(
                    conn, ticker, trade_time,
                )
                gap = (
                    (trade_time - first_seen).total_seconds()
                    if first_seen is not None
                    else None
                )

                if in_trade_window_fn is not None:
                    try:
                        in_w, name, _ = in_trade_window_fn(trade_time)
                    except Exception as exc:  # pragma: no cover - defensive
                        warnings.append(f"calendar lookup failed: {exc}")
                        in_w, name = False, None
                else:
                    in_w, name = False, None

                out.append(TradeBacktest(
                    trade_id=int(trade_id),
                    ticker=str(ticker),
                    action=str(action),
                    side=str(side),
                    trade_time=trade_time,
                    first_seen_time=first_seen,
                    gap_seconds=gap,
                    hour_of_window_edt=trade_time.astimezone(ET).hour,
                    # `is_in_trade_window` is contracted to return a non-None
                    # name when in_w=True, but the `or "outside"` is paranoia.
                    trade_window_name=(name or "outside") if in_w else "outside",
                    gimme_score=gimme_score,
                    edge=edge,
                    cap_blocked_at_first_seen=bool(cap_blocked),
                ))
    except sqlite3.Error as exc:
        return [], [f"trades query failed: {exc}"]

    return out, warnings


def _first_seen(
    conn: sqlite3.Connection, ticker: str, before: datetime,
) -> tuple[datetime | None, float | None, float | None, bool]:
    """Return the candidate's earliest sighting at or before ``before``,
    plus its gimme_score/edge/cap_blocked at that sighting. Caller owns
    the connection (must be opened in ``mode=ro``)."""
    try:
        cur = conn.execute(
            """
            SELECT scanned_at, gimme_score, edge, cap_blocked
            FROM candidates
            WHERE ticker = ? AND scanned_at <= ?
            ORDER BY scanned_at ASC
            LIMIT 1
            """,
            (ticker, before.isoformat()),
        )
        row = cur.fetchone()
    except sqlite3.Error as exc:
        logger.warning("backtest: first-seen lookup failed for %s: %s", ticker, exc)
        return None, None, None, False
    if row is None:
        return None, None, None, False
    scanned_at, gimme_score, edge, cap_blocked = row
    return _parse_iso(scanned_at), gimme_score, edge, bool(cap_blocked)


def bucketize_gaps(trades: Iterable[TradeBacktest]) -> list[GapBucket]:
    """Distribute trades across the predefined gap buckets.

    Trades with ``gap_seconds is None`` (no candidate row) are excluded
    from the percent-of-total denominator.
    """
    placed = [t for t in trades if t.gap_seconds is not None]
    total = len(placed)
    out: list[GapBucket] = []
    for label, lower, upper in GAP_BUCKETS:
        count = 0
        for t in placed:
            assert t.gap_seconds is not None  # narrowed by filter
            if t.gap_seconds < lower:
                continue
            if upper is not None and t.gap_seconds >= upper:
                continue
            count += 1
        pct = (count / total * 100.0) if total else 0.0
        out.append(GapBucket(
            label=label,
            lower_seconds=lower,
            upper_seconds=upper,
            count=count,
            pct_of_total=round(pct, 2),
        ))
    return out


def aggregate_hours(
    log_dir: Path,
    db_path: Path | None,
    *,
    date_from: date,
    date_to: date,
) -> tuple[list[HourBucket], int]:
    """Bucket every cycle log on disk whose UTC start_time falls within
    ``[date_from, date_to]`` by EDT hour-of-day. Returns
    ``(buckets, total_cycles_audited)``."""
    from gimmes.reporting.cycle_audit import parse_cycle_log

    by_hour: dict[int, list] = {}
    days_by_hour: dict[int, set] = {}
    cycles = 0
    for log_path in sorted(log_dir.glob("cycle-*.json")):
        if "-block-" in log_path.name:
            continue
        summary = parse_cycle_log(log_path, db_path=db_path)
        if summary.start_time is None:
            continue
        start_utc = summary.start_time
        d_utc = start_utc.date()
        if not (date_from <= d_utc <= date_to):
            continue
        cycles += 1
        hour_edt = start_utc.astimezone(ET).hour
        by_hour.setdefault(hour_edt, []).append(summary)
        days_by_hour.setdefault(hour_edt, set()).add(
            start_utc.astimezone(ET).date(),
        )

    out: list[HourBucket] = []
    for hour in sorted(by_hour.keys()):
        group = by_hour[hour]
        trades = sum(s.trades_placed_db for s in group)
        out.append(HourBucket(
            hour_edt=hour,
            cycles_observed=len(group),
            days_observed=len(days_by_hour[hour]),
            trades_placed=trades,
            trades_per_cycle=trades / len(group) if group else 0.0,
        ))
    return out, cycles


def build_summary(
    log_dir: Path,
    db_path: Path,
    *,
    date_from: date,
    date_to: date,
    actions: tuple[str, ...] = ("open",),
    in_trade_window_fn=None,
) -> BacktestSummary:
    """End-to-end: collect trades, bucket gaps, aggregate hours, count
    by trade-window-name, return :class:`BacktestSummary`."""
    trades, trade_warnings = collect_trades(
        db_path,
        date_from=date_from,
        date_to=date_to,
        actions=actions,
        in_trade_window_fn=in_trade_window_fn,
    )
    hour_buckets, cycles_audited = aggregate_hours(
        log_dir, db_path, date_from=date_from, date_to=date_to,
    )
    gap_buckets = bucketize_gaps(trades)
    by_window = dict(Counter(t.trade_window_name for t in trades))
    no_candidate = sum(1 for t in trades if t.first_seen_time is None)
    return BacktestSummary(
        trades=sorted(trades, key=lambda t: t.trade_time),
        hour_buckets=hour_buckets,
        gap_buckets=gap_buckets,
        by_window_name=by_window,
        date_from=date_from,
        date_to=date_to,
        cycles_audited=cycles_audited,
        trades_with_no_candidate=no_candidate,
        warnings=trade_warnings,
    )


def _recommendation(summary: BacktestSummary) -> tuple[str, str]:
    """Pick one of three recommendations based on the gap distribution."""
    placed_total = sum(b.count for b in summary.gap_buckets)
    if placed_total == 0:
        return ("INCONCLUSIVE", (
            "No trades with measurable gaps in the audited range. "
            "Cannot recommend a pause change without #553 PnL counterfactual."
        ))
    fast_pct = sum(
        b.pct_of_total for b in summary.gap_buckets
        if b.upper_seconds is not None and b.upper_seconds <= 300
    )
    if fast_pct >= FAST_GAP_DO_NOT_RAISE_PCT:
        return ("DO NOT RAISE", (
            f"{fast_pct:.0f}% of placed trades have first-seen-to-trade gaps "
            "under 300s. Raising `pause_seconds` to 300 risks missing those "
            "entries. Wait for #553 PnL counterfactual before changing the "
            "default."
        ))
    if fast_pct <= FAST_GAP_LIKELY_SAFE_PCT:
        return ("LIKELY SAFE TO RAISE", (
            f"Only {fast_pct:.0f}% of placed trades have first-seen-to-trade "
            "gaps under 300s. Raising `pause_seconds` to 300 would likely "
            "miss few entries — but confirm with #553's PnL counterfactual "
            "before changing the default."
        ))
    return ("WAIT FOR #553", (
        f"{fast_pct:.0f}% of placed trades have gaps under 300s — too "
        "borderline to recommend a change without the PnL counterfactual "
        "from #553. Hold pause_seconds at the current default."
    ))


def render_markdown(
    summary: BacktestSummary,
    *,
    parent_issue: int = 546,
    self_issue: int = 556,
    deferred_phase1b_issue: int = 553,
) -> str:
    """Render a deterministic Markdown report of the backtest summary."""
    placed_total = sum(b.count for b in summary.gap_buckets)
    total_trades = len(summary.trades)
    rec_label, rec_text = _recommendation(summary)

    lines: list[str] = []
    lines.append(
        f"# Phase 1a Pause Backtest (#{self_issue}, parent #{parent_issue})"
    )
    lines.append("")
    lines.append("## Executive summary")
    lines.append("")
    lines.append(
        f"- **Recommendation: {rec_label}** — {rec_text}"
    )
    lines.append(
        f"- {total_trades} placed trades audited "
        f"({summary.date_from.isoformat()} → {summary.date_to.isoformat()}); "
        f"{summary.cycles_audited} cycle logs surveyed."
    )
    if summary.trades_with_no_candidate:
        lines.append(
            f"- {summary.trades_with_no_candidate} trade(s) had no matching "
            "candidate row (manual log or Scout bypass) — counted in totals "
            "but excluded from the gap distribution denominator."
        )
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append(
        "- Data: `~/.gimmes/logs/cycle-*.json` parsed via "
        "`gimmes.reporting.cycle_audit.parse_cycle_log`; "
        "`~/.gimmes/gimmes.db` opened read-only."
    )
    lines.append(
        "- Trade selection: `action='open'` only (entries, not exits). "
        "Excludes `skip` bookkeeping rows."
    )
    lines.append(
        "- First-seen lookup: `MIN(scanned_at)` from `candidates` for the "
        "trade's ticker, bounded above by the trade's `timestamp`."
    )
    lines.append(
        "- Hour bucketing converts UTC `start_time` to America/New_York; "
        "EDT hour-of-day is the bucket key."
    )
    lines.append(
        "- Gap buckets: lower-inclusive, upper-exclusive. "
        "`30min+` is open-ended."
    )
    lines.append("")
    lines.append("## Hour-of-window")
    lines.append("")
    lines.append(
        "| hour (EDT) | days observed | full cycles | trades placed | trades/cycle |"
    )
    lines.append(
        "|-----------:|--------------:|------------:|--------------:|-------------:|"
    )
    for b in summary.hour_buckets:
        lines.append(
            f"| {b.hour_edt:02d}:00 | {b.days_observed} | "
            f"{b.cycles_observed} | {b.trades_placed} | "
            f"{b.trades_per_cycle:.2f} |"
        )
    lines.append("")
    lines.append("## Gap distribution (first-seen → trade-placed)")
    lines.append("")
    lines.append(
        f"Out of {placed_total} placed trades with a known first-seen time:"
    )
    lines.append("")
    lines.append("| bucket | trades | % of placed |")
    lines.append("|--------|------:|------------:|")
    for b in summary.gap_buckets:
        lines.append(
            f"| {b.label} | {b.count} | {b.pct_of_total:.1f}% |"
        )
    lines.append("")
    lines.append(
        "Interpretation: a trade whose first-seen-to-placed gap is **under "
        "X seconds** would have been **missed** by a re-scan cadence of X "
        "seconds — the candidate appeared and was traded between scans. The "
        "real loop's effective cadence is dominated by cycle wall time "
        "(15–30 min per cycle), so `pause_seconds` is a lower bound on the "
        "actual rescan interval."
    )
    lines.append("")
    lines.append("## Per-trade detail")
    lines.append("")
    lines.append(
        "| ticker | trade time (EDT) | first seen (EDT) | gap | hour (EDT) | "
        "window | gimme | edge |"
    )
    lines.append(
        "|--------|------------------|------------------|----:|-----------:|"
        "--------|------:|-----:|"
    )
    for t in summary.trades:
        trade_edt = t.trade_time.astimezone(ET).strftime("%m-%d %H:%M")
        first_edt = (
            t.first_seen_time.astimezone(ET).strftime("%m-%d %H:%M")
            if t.first_seen_time is not None
            else "—"
        )
        gap = (
            f"{int(t.gap_seconds)}s"
            if t.gap_seconds is not None
            else "—"
        )
        gimme = (
            f"{t.gimme_score:.0f}"
            if t.gimme_score is not None
            else "—"
        )
        edge_str = (
            f"{t.edge:.2f}"
            if t.edge is not None
            else "—"
        )
        cap_flag = "*" if t.cap_blocked_at_first_seen else ""
        lines.append(
            f"| {t.ticker}{cap_flag} | {trade_edt} | {first_edt} | "
            f"{gap} | {t.hour_of_window_edt:02d}:00 | "
            f"{t.trade_window_name} | {gimme} | {edge_str} |"
        )
    lines.append("")
    if any(t.cap_blocked_at_first_seen for t in summary.trades):
        lines.append(
            "`*` = ticker was `cap_blocked` at its first sighting "
            "(Scout flagged it before any Caddie/Closer evaluation)."
        )
        lines.append("")
    lines.append("## By trade window")
    lines.append("")
    lines.append("| release window | trades |")
    lines.append("|----------------|------:|")
    for name in sorted(summary.by_window_name.keys()):
        lines.append(f"| {name} | {summary.by_window_name[name]} |")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- **Cross-model basis**: Apr cycles ran on Opus 4.7; May cycles on "
        "Sonnet 4.6 (post-#549). Trades placed are objectively the same, "
        "but the cost-per-cycle differs — interpret budget-related "
        "conclusions with care."
    )
    lines.append(
        "- **Cycle cadence vs `pause_seconds`**: each cycle takes ~15-30 "
        "min wall time; `pause_seconds` only adds inter-cycle delay. The "
        "gap distribution measures first-seen to trade-placed wall time "
        "regardless of where that time was spent."
    )
    lines.append(
        "- **Small-N for some hour buckets**: a single overnight EDT hour "
        "may have only one or two trades. Treat 0/1-trade rows as anecdote, "
        "not signal."
    )
    lines.append(
        "- **No PnL**: this analysis says nothing about *whether the trades "
        "we placed were profitable*, only about *how a longer pause would "
        f"have affected which trades got placed*. PnL counterfactual is "
        f"#{deferred_phase1b_issue} (Phase 1b)."
    )
    if summary.warnings:
        lines.append("")
        lines.append("### Parser warnings")
        lines.append("")
        for w in summary.warnings:
            lines.append(f"- {w}")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append(rec_text)
    lines.append("")
    lines.append(
        f"PnL counterfactual deferred to #{deferred_phase1b_issue} "
        "(needs Kalshi historical orderbook data not currently persisted)."
    )
    lines.append("")
    return "\n".join(lines)
