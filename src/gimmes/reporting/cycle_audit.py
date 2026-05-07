"""Per-cycle audit of autonomous-loop ``cycle-NNNN.json`` logs.

Phase 0 of GitHub issue #546: extract Scout shortlist size, Caddie
threshold passes, and trade placements from each cycle log, cross-check
trades against the SQLite ``trades`` table, and render a Markdown report.

The audit is intentionally read-only and never modifies the live
``${GIMMES_HOME}/gimmes.db`` — it opens the database with
``mode=ro`` so a running loop is not perturbed.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("gimmes.cycle_audit")

ET = ZoneInfo("America/New_York")

# Scout shortlist: try variants in order; first match wins.
_SCOUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Scout\s+returned\s+(\d+)\s+candidates?", re.IGNORECASE),
    re.compile(
        r"Scout\s+(?:shortlisted|surfaced|found)\s+(\d+)\s+(?:markets?|candidates?|tickers?)",
        re.IGNORECASE,
    ),
    re.compile(r"shortlist(?:\s+size)?\s*[:=]\s*(\d+)", re.IGNORECASE),
)

_CADDIE_DISPATCH_RE = re.compile(
    r"Dispatching\s+Caddie|Step\s*4[a-z]?\s*[:.\-—]\s*Caddie",
    re.IGNORECASE,
)

# Caddie pass: count PROCEED tokens. Combined with REJECT for sanity.
_PROCEED_RE = re.compile(r"\bPROCEED\b")
_REJECT_RE = re.compile(r"\bREJECT(?:ED)?\b")

# Closer trade-placement text markers (cross-check; DB is source of truth).
_TRADE_TEXT_RE = re.compile(
    r"Order\s+(?:submitted|placed|filled)|Closer.*?placed",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CycleSummary:
    """One audited cycle's salient extracts."""

    cycle_id: int
    log_path: Path
    start_time: datetime | None
    end_time: datetime | None
    duration_seconds: float | None
    scout_shortlist_size: int | None
    caddie_dispatches: int | None
    caddie_threshold_passes: int | None
    trades_placed_db: int
    trades_placed_text: int | None
    cycle_type: str  # "full" | "monitor" | "errored" | "block"
    in_trade_window: bool
    parse_warnings: list[str] = field(default_factory=list)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Handle the trailing 'Z' some Claude Code events emit.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _iter_assistant_text(events: list[dict]) -> Iterable[str]:
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        for c in msg.get("content", []) or []:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text")
                if isinstance(t, str):
                    yield t


def _extract_scout_size(text_blocks: list[str]) -> tuple[int | None, str | None]:
    """Try each Scout pattern; return (size, warning) where warning notes
    multi-match cases."""
    matches: list[int] = []
    for pat in _SCOUT_PATTERNS:
        for block in text_blocks:
            for m in pat.finditer(block):
                matches.append(int(m.group(1)))
        if matches:
            # First pattern that hits wins; later patterns are fallbacks.
            break
    if not matches:
        return None, None
    if len(matches) > 1:
        return matches[-1], f"multiple Scout matches: {matches}"
    return matches[0], None


def _extract_caddie_passes(text_blocks: list[str]) -> int:
    proceed = 0
    for block in text_blocks:
        proceed += len(_PROCEED_RE.findall(block))
    return proceed


def _extract_caddie_dispatches(text_blocks: list[str]) -> int:
    n = 0
    for block in text_blocks:
        n += len(_CADDIE_DISPATCH_RE.findall(block))
    return n


def _extract_text_trade_count(text_blocks: list[str]) -> int:
    n = 0
    for block in text_blocks:
        n += len(_TRADE_TEXT_RE.findall(block))
    return n


def parse_cycle_log(
    path: Path,
    *,
    db_path: Path | None = None,
    in_trade_window_fn=None,
) -> CycleSummary:
    """Parse a single ``cycle-NNNN.json`` (or ``cycle-NNNN-block-*.json``)
    log file.

    Fully fail-open: regex misses, malformed JSON, missing fields, stray
    non-dict elements in the events list, and DB query failures all
    produce a :class:`CycleSummary` with ``None`` extraction cells and
    descriptive entries in :attr:`CycleSummary.parse_warnings`. The
    function does not raise for any of these conditions, so callers do
    not need to wrap invocations in ``try``/``except``. (Programming
    errors — e.g. wrong argument type — still raise normally.)
    """
    cycle_id = _cycle_id_from_path(path)
    warnings: list[str] = []

    if "-block-" in path.name:
        # Cap-block log: minimal JSON, never had a subprocess. Mark it.
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"unreadable block log: {exc}")
            payload = {}
        return CycleSummary(
            cycle_id=cycle_id,
            log_path=path,
            start_time=None,
            end_time=None,
            duration_seconds=None,
            scout_shortlist_size=None,
            caddie_dispatches=None,
            caddie_threshold_passes=None,
            trades_placed_db=0,
            trades_placed_text=None,
            cycle_type="block",
            in_trade_window=False,
            parse_warnings=warnings + [f"reason={payload.get('reason')}"],
        )

    try:
        events = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"unreadable: {exc}")
        return CycleSummary(
            cycle_id=cycle_id,
            log_path=path,
            start_time=None,
            end_time=None,
            duration_seconds=None,
            scout_shortlist_size=None,
            caddie_dispatches=None,
            caddie_threshold_passes=None,
            trades_placed_db=0,
            trades_placed_text=None,
            cycle_type="errored",
            in_trade_window=False,
            parse_warnings=warnings,
        )

    if not isinstance(events, list):
        warnings.append("log root is not a list")
        events = []
    # Filter out any non-dict elements before iterating — a malformed log
    # with a stray string/int in the events list must not raise here.
    events = [e for e in events if isinstance(e, dict)]

    # Timestamps: first and last with a non-empty value.
    first_ts = next(
        (_parse_iso(e.get("timestamp")) for e in events if e.get("timestamp")),
        None,
    )
    last_ts = next(
        (_parse_iso(e.get("timestamp")) for e in reversed(events) if e.get("timestamp")),
        None,
    )
    duration = (
        (last_ts - first_ts).total_seconds()
        if first_ts and last_ts and last_ts >= first_ts
        else None
    )

    # Cycle type: result event's is_error flag, or absence of assistant content.
    result_event = next((e for e in reversed(events) if e.get("type") == "result"), {})
    is_error = bool(result_event.get("is_error"))
    text_blocks = list(_iter_assistant_text(events))
    has_text = any(b.strip() for b in text_blocks)

    cycle_type: str
    if is_error:
        cycle_type = "errored"
    elif not has_text:
        cycle_type = "monitor"  # no assistant output ≈ monitor-only or no-op
    else:
        cycle_type = "full"

    scout_size, scout_warn = _extract_scout_size(text_blocks)
    if scout_warn:
        warnings.append(scout_warn)
    caddie_dispatches = _extract_caddie_dispatches(text_blocks) or None
    caddie_passes = _extract_caddie_passes(text_blocks) if has_text else None
    trades_text = _extract_text_trade_count(text_blocks) if has_text else None

    # Cross-check trades against the DB. Bubble any DB error into the
    # cycle's warnings so a swallowed sqlite failure (locked DB, missing
    # table, schema drift) is visible in the rendered report and doesn't
    # silently flip the H5 verdict.
    if db_path and first_ts and last_ts:
        trades_db, db_warning = _query_trades_in_window_with_warning(
            db_path, first_ts, last_ts,
        )
        if db_warning:
            warnings.append(db_warning)
    else:
        trades_db = 0

    if trades_text is not None and trades_db != trades_text:
        warnings.append(
            f"text/db trade count mismatch: text={trades_text} db={trades_db}"
        )

    in_window = (
        bool(in_trade_window_fn(first_ts)[0])
        if in_trade_window_fn and first_ts
        else False
    )

    return CycleSummary(
        cycle_id=cycle_id,
        log_path=path,
        start_time=first_ts,
        end_time=last_ts,
        duration_seconds=duration,
        scout_shortlist_size=scout_size,
        caddie_dispatches=caddie_dispatches,
        caddie_threshold_passes=caddie_passes,
        trades_placed_db=trades_db,
        trades_placed_text=trades_text,
        cycle_type=cycle_type,
        in_trade_window=in_window,
        parse_warnings=warnings,
    )


def _cycle_id_from_path(path: Path) -> int:
    """Extract the integer cycle id from ``cycle-NNNN.json`` /
    ``cycle-NNNN-block-*.json`` filenames; return -1 on parse failure."""
    m = re.match(r"cycle-(\d+)", path.stem)
    return int(m.group(1)) if m else -1


def query_trades_in_window(
    db_path: Path, start: datetime, end: datetime,
) -> int:
    """Read-only count of ``trades`` rows whose ``timestamp`` falls in
    ``[start, end]`` and whose ``action`` represents an actual order
    placement (excludes Scout/Caddie ``skip`` rows). Errors are swallowed
    and return 0 — callers that need to surface DB problems should use
    :func:`_query_trades_in_window_with_warning` instead."""
    count, _ = _query_trades_in_window_with_warning(db_path, start, end)
    return count


def _query_trades_in_window_with_warning(
    db_path: Path, start: datetime, end: datetime,
) -> tuple[int, str | None]:
    """Same as :func:`query_trades_in_window` but returns a warning string
    on error so the audit report can surface the failure inline."""
    uri = f"file:{db_path}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            cur = conn.execute(
                """
                SELECT COUNT(*) FROM trades
                WHERE timestamp >= ? AND timestamp <= ?
                  AND action != 'skip'
                """,
                (start.isoformat(), end.isoformat()),
            )
            return int(cur.fetchone()[0]), None
    except sqlite3.Error as exc:
        msg = f"trades-DB query failed: {exc}"
        logger.warning("audit: %s", msg)
        return 0, msg


def audit_date(
    log_dir: Path,
    db_path: Path | None,
    target_date: date,
    *,
    in_trade_window_fn=None,
    pre_buffer_hours: int = 12,
) -> list[CycleSummary]:
    """Audit cycle logs associated with ``target_date``'s trade window.

    A cycle is included if either:

    - its UTC ``start_time`` falls within
      ``[target_date 00:00, target_date+1 00:00)`` UTC, OR
    - its UTC ``start_time`` falls within
      ``[target_date 00:00 - pre_buffer_hours, target_date 00:00)`` UTC
      **AND** its UTC ``end_time`` lands on or after ``target_date 00:00``
      (i.e. the cycle started late on the prior evening but its work
      spilled into the target day).

    The default ``pre_buffer_hours=12`` is conservative for gimmes' longest
    trade window (Wed 6:30 PM ET → Thu 8:30 AM ET) — a cycle that opens at
    18:30 ET = 22:30 UTC EST / 23:30 UTC EDT lands within 12 h of UTC
    midnight, so 12 h covers every realistic spillover.

    Block-log siblings are paired with their parent cycle if the parent
    falls in the audit set; orphan blocks are silently dropped.
    """
    from datetime import timedelta as _td

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=UTC)
    day_end = day_start + _td(days=1)
    pre_window_start = day_start - _td(hours=pre_buffer_hours)

    summaries: list[CycleSummary] = []
    for log_path in sorted(log_dir.glob("cycle-*.json")):
        try:
            summary = parse_cycle_log(
                log_path,
                db_path=db_path,
                in_trade_window_fn=in_trade_window_fn,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("audit: failed to parse %s: %s", log_path, exc)
            continue
        if summary.start_time is None and summary.cycle_type != "block":
            # No timestamp; can't bucket. Skip.
            continue
        if summary.cycle_type == "block":
            sibling_match = any(
                s.cycle_id == summary.cycle_id for s in summaries
            )
            if sibling_match:
                summaries.append(summary)
            continue

        start = summary.start_time
        end = summary.end_time or start
        # Include if start is on the day, OR start is in the pre-buffer
        # AND the cycle's end straddles into the target day.
        on_day = day_start <= start < day_end
        spans_into_day = (
            pre_window_start <= start < day_start
            and end >= day_start
        )
        if on_day or spans_into_day:
            summaries.append(summary)

    summaries.sort(key=lambda s: (s.start_time or datetime.min.replace(tzinfo=UTC)))
    return summaries


def render_markdown(
    summaries: list[CycleSummary],
    *,
    target_date: date,
    hypothesis_id: str = "H5",
    hypothesis_text: str = (
        "overnight 8 PM EDT–2 AM EDT cycles produce no actionable signal"
    ),
    parent_issue: int = 546,
    deferred_phase1_issue: int = 553,
    deferred_phase23_issue: int = 554,
) -> str:
    """Render a deterministic Markdown report.

    Sorts ``summaries`` by ``(start_time, cycle_id)`` internally so the
    output is byte-identical regardless of caller-supplied order; cycles
    without a ``start_time`` (block logs) sort after timestamped cycles.
    """
    if not summaries:
        return (
            f"# {target_date.isoformat()} Cycle-Staleness Audit "
            f"(Phase 0 of #{parent_issue})\n\n"
            "No cycles found for this date.\n"
        )

    # Defensive in-place sort to make the contract independent of caller.
    summaries = sorted(
        summaries,
        key=lambda s: (
            s.start_time or datetime.max.replace(tzinfo=UTC),
            s.cycle_id,
        ),
    )

    full_cycles = [s for s in summaries if s.cycle_type == "full"]
    monitor_cycles = [s for s in summaries if s.cycle_type == "monitor"]
    errored_cycles = [s for s in summaries if s.cycle_type == "errored"]
    block_cycles = [s for s in summaries if s.cycle_type == "block"]

    total_trades = sum(s.trades_placed_db for s in summaries)
    cycles_with_trade = sum(1 for s in summaries if s.trades_placed_db > 0)

    # Hour-bucket aggregation (ET).
    by_hour: dict[int, list[CycleSummary]] = {}
    for s in full_cycles:
        if s.start_time is None:
            continue
        hour = s.start_time.astimezone(ET).hour
        by_hour.setdefault(hour, []).append(s)

    # H5 verdict.
    # Hour-bucket boundaries are upper-exclusive to match the prose:
    # "8 PM EDT–2 AM EDT" → hours {20, 21, 22, 23, 0, 1};
    # "5–9 AM EDT pre-release" → hours {5, 6, 7, 8}.
    overnight_hours = set(range(20, 24)) | set(range(0, 2))  # 8 PM–2 AM ET
    overnight_cycles = [
        s for s in full_cycles
        if s.start_time
        and s.start_time.astimezone(ET).hour in overnight_hours
    ]
    overnight_trades = sum(s.trades_placed_db for s in overnight_cycles)
    pre_release_hours = set(range(5, 9))  # 5–9 AM ET
    pre_release_cycles = [
        s for s in full_cycles
        if s.start_time
        and s.start_time.astimezone(ET).hour in pre_release_hours
    ]
    pre_release_trades = sum(s.trades_placed_db for s in pre_release_cycles)

    if not overnight_cycles and not pre_release_cycles:
        verdict = "INCONCLUSIVE"
        verdict_reason = "No cycles ran in either bucket."
    elif overnight_trades == 0 and pre_release_trades == 0:
        verdict = "INCONCLUSIVE"
        verdict_reason = (
            f"Zero trades in both buckets ({len(overnight_cycles)} overnight, "
            f"{len(pre_release_cycles)} pre-release) — too thin to call."
        )
    elif overnight_trades == 0 and pre_release_trades > 0:
        verdict = "ACCEPTED (one-day data)"
        verdict_reason = (
            f"{len(overnight_cycles)} overnight cycles produced 0 trades; "
            f"{pre_release_trades} trades came from {len(pre_release_cycles)} "
            "pre-release cycles."
        )
    elif overnight_trades > 0 and not pre_release_cycles:
        verdict = "REJECTED for overnight bucket; pre-release uninformed"
        verdict_reason = (
            f"{len(overnight_cycles)} overnight cycles produced "
            f"{overnight_trades} trades — overnight is NOT alpha-empty as "
            "H5 predicted. No cycles ran in the 5–9 AM ET pre-release "
            "bucket (e.g. cap-blocked, no scheduled window, or system "
            "down) so the comparison side is missing."
        )
    elif overnight_trades > 0 and pre_release_trades == 0 and pre_release_cycles:
        verdict = "REJECTED (one-day data)"
        on_word = "cycle" if len(overnight_cycles) == 1 else "cycles"
        on_trade = "trade" if overnight_trades == 1 else "trades"
        pr_word = "cycle" if len(pre_release_cycles) == 1 else "cycles"
        verdict_reason = (
            f"{len(overnight_cycles)} overnight {on_word} produced "
            f"{overnight_trades} {on_trade}; {len(pre_release_cycles)} "
            f"pre-release {pr_word} produced 0 trades. "
            "H5's prediction is inverted on this date."
        )
    else:
        verdict = "PARTIALLY REJECTED (one-day data)"
        verdict_reason = (
            f"Overnight {len(overnight_cycles)} cycles → "
            f"{overnight_trades} trades; pre-release "
            f"{len(pre_release_cycles)} cycles → {pre_release_trades} trades. "
            "Both buckets produced signal."
        )

    lines: list[str] = []
    lines.append(
        f"# {target_date.isoformat()} Cycle-Staleness Audit (Phase 0 of #{parent_issue})"
    )
    lines.append("")
    lines.append("## Executive summary")
    lines.append("")
    lines.append(f"- **{hypothesis_id}: {verdict}** — {verdict_reason}")
    lines.append(
        f"- {len(summaries)} cycles total: {len(full_cycles)} full, "
        f"{len(monitor_cycles)} monitor, {len(errored_cycles)} errored, "
        f"{len(block_cycles)} cap-blocked."
    )
    lines.append(
        f"- {total_trades} trades placed across {cycles_with_trade} cycles."
    )
    lines.append("")
    lines.append("## Hypothesis")
    lines.append("")
    lines.append(f"{hypothesis_id} (verbatim from #{parent_issue}): {hypothesis_text}")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append(
        "- Source: `${GIMMES_HOME}/logs/cycle-*.json` parsed via "
        "`gimmes.reporting.cycle_audit.parse_cycle_log`."
    )
    lines.append(
        "- Trade ground truth: read-only SQL on the `trades` table, "
        "windowed to each cycle's start/end timestamps and excluding "
        "Scout/Caddie `skip` bookkeeping rows."
    )
    lines.append(
        "- Hour-of-window bucketing converts each cycle's start time to "
        "America/New_York and groups by EDT hour."
    )
    lines.append(
        "- Scout shortlist size and Caddie pass count are extracted from "
        "assistant text via ordered regex patterns (see `_SCOUT_PATTERNS`); "
        "regex misses produce `unknown` cells, never exceptions."
    )
    lines.append("")
    lines.append("## Per-cycle audit")
    lines.append("")
    lines.append(
        "| cycle | start (EDT) | type | scout | caddie disp. | "
        "caddie pass | trades (db) | warnings |"
    )
    lines.append(
        "|------:|-------------|------|------:|-------------:|"
        "------------:|------------:|----------|"
    )
    for s in summaries:
        if s.start_time is None:
            edt = "—"
        else:
            edt = s.start_time.astimezone(ET).strftime("%H:%M")
        lines.append(
            "| {cid} | {edt} | {ct} | {scout} | {cd} | {cp} | {td} | {warn} |".format(
                cid=s.cycle_id,
                edt=edt,
                ct=s.cycle_type,
                scout=s.scout_shortlist_size if s.scout_shortlist_size is not None else "—",
                cd=s.caddie_dispatches if s.caddie_dispatches is not None else "—",
                cp=s.caddie_threshold_passes if s.caddie_threshold_passes is not None else "—",
                td=s.trades_placed_db,
                warn="; ".join(s.parse_warnings) if s.parse_warnings else "",
            )
        )
    lines.append("")
    lines.append("## Aggregate by hour-of-window (EDT)")
    lines.append("")
    lines.append(
        "| hour (EDT) | full cycles | trades placed | trades/cycle |"
    )
    lines.append("|-----------:|------------:|--------------:|-------------:|")
    for hour in sorted(by_hour.keys()):
        cycles = by_hour[hour]
        trades = sum(s.trades_placed_db for s in cycles)
        per = trades / len(cycles) if cycles else 0.0
        lines.append(
            f"| {hour:02d}:00 | {len(cycles)} | {trades} | {per:.2f} |"
        )
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- Single day, single trade-window (jobless claims). Not generalizable "
        "to CPI/GDP/PCE windows or to Friday/Wednesday calendars."
    )
    lines.append(
        "- The `trades` table lacks an explicit `cycle_id` column; rows are "
        "attributed to a cycle by timestamp window. Risk of misattribution "
        "exists if cycles overlap; none observed on this date."
    )
    lines.append(
        "- Hour-bucket boundaries: overnight = 8 PM–2 AM EDT (hours "
        "20, 21, 22, 23, 0, 1); pre-release = 5–9 AM EDT (hours 5, 6, 7, 8). "
        "When pre-release coverage is empty (e.g. cap-blocked sleep), any "
        "verdict on that bucket is uninformed by the audited date."
    )
    lines.append(
        "- Caddie threshold-pass count is the raw `PROCEED` token count in "
        "assistant text. A future-format wording change would silently zero this."
    )
    lines.append("")
    lines.append("## Deferred follow-up")
    lines.append("")
    lines.append(
        f"- **Phase 1** — 30-day backtest of pause and hour-of-window vs "
        f"realized PnL: #{deferred_phase1_issue}"
    )
    lines.append(
        f"- **Phase 2/3** — driving-range A/B + cycle-timing recommendation: "
        f"#{deferred_phase23_issue}"
    )
    lines.append("")
    return "\n".join(lines)
