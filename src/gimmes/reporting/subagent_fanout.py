"""Phase 1 characterization of intra-cycle subagent fanout (issue #571).

Walks one or more ``cycle-NNNN.json`` stream-json logs, tallies ``Agent``
dispatches by ``subagent_type``, and attributes per-turn token usage and
dollar cost to either the parent ``caddie_master`` agent or the active
subagent. Closes #569's recommendation that "sub-agent depth tuning is
the real cost lever" by giving Phase 2 a numeric basis for picking which
subagent to cap.

## Attribution model

A cycle's stream-json is a JSON **array** of events from the parent
(Caddie Master) subprocess. Subagent dispatches surface as ``assistant``
events containing a ``tool_use`` block with ``name == "Agent"``. The
matching ``tool_result`` arrives in a ``user`` event some events later.
Subagent internal turns surface as nested ``assistant`` events between
dispatch and return — those carry their own ``message.usage``.

The walker maintains an "active dispatch" pointer:

- Before any dispatch: every ``assistant`` event's usage is attributed to
  ``caddie_master``.
- Once a dispatch fires, the *envelope* turn (the one containing the
  ``Agent`` ``tool_use`` block) stays attributed to ``caddie_master`` —
  the dispatch decision is Caddie Master's, not the dispatchee's.
- Subsequent ``assistant`` events until the matching ``tool_result``
  attribute to the dispatchee's ``subagent_type``.
- After the result, attribution returns to ``caddie_master`` until the
  next dispatch.

Nested dispatches (subagent calling subagent) would require a stack;
the production gimmes pipeline doesn't emit them, so this module uses a
single active-dispatch pointer and emits a warning if a tool_use_id
arrives mid-flight from an unexpected source.

## Cost model

All declared agents in ``.claude/agents/*.md`` use ``claude-sonnet-4-6``;
the cycle log's first ``assistant.message.model`` is read as the cycle's
billed model. Falls back to Sonnet pricing on unknown models, matching
``budget.cost_from_usage`` policy.

Read-only: this module never writes to ``~/.gimmes`` or ``gimmes.db``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from gimmes.budget import PRICING, cost_from_usage

logger = logging.getLogger("gimmes.subagent_fanout")

CADDIE_MASTER = "caddie_master"
UNKNOWN = "unknown"

# Token kinds that ``parse_usage_from_stream_json`` and
# ``budget.cost_from_usage`` already use; mirror them here so the bucket
# totals reconcile against the cycle-level totals.
TOKEN_KINDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


@dataclass(frozen=True)
class Dispatch:
    """One ``Agent`` tool_use observed in a cycle log."""

    tool_use_id: str
    subagent_type: str
    description: str
    event_idx: int


@dataclass(frozen=True)
class BucketUsage:
    """Aggregate token usage and dollar cost attributed to one bucket
    (parent ``caddie_master`` or a subagent type)."""

    bucket: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cost_usd: float
    turn_count: int


@dataclass(frozen=True)
class CycleFanout:
    """Per-cycle fanout characterization."""

    cycle_id: int
    log_path: Path
    model: str
    total_cost_usd_reported: float | None
    total_events: int
    dispatches: tuple[Dispatch, ...]
    buckets: tuple[BucketUsage, ...]
    warnings: tuple[str, ...] = ()

    def cost_by_bucket(self) -> dict[str, float]:
        return {b.bucket: b.cost_usd for b in self.buckets}

    def dispatch_count_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.dispatches:
            counts[d.subagent_type] = counts.get(d.subagent_type, 0) + 1
        return counts

    def computed_total_cost_usd(self) -> float:
        return sum(b.cost_usd for b in self.buckets)


@dataclass(frozen=True)
class FanoutSummary:
    """Aggregate over multiple cycles, suitable for the deliverable markdown."""

    cycles: tuple[CycleFanout, ...]
    totals_by_bucket: tuple[BucketUsage, ...]
    dispatch_counts_by_type: dict[str, int]
    warnings: tuple[str, ...] = field(default=())

    @property
    def cycles_audited(self) -> int:
        return len(self.cycles)

    @property
    def cycles_with_reported_cost(self) -> int:
        return sum(1 for c in self.cycles if c.total_cost_usd_reported is not None)

    @property
    def total_cost_usd_reported(self) -> float | None:
        # Returns None when *no* audited cycle has a reported cost — that
        # avoids rendering a misleading $0.00 sum. When some cycles
        # reported and others didn't, returns the partial sum; the
        # render layer surfaces "(N of M cycles reported)" so the
        # coverage gap is visible rather than silently understated.
        reported = [c.total_cost_usd_reported for c in self.cycles
                    if c.total_cost_usd_reported is not None]
        if not reported:
            return None
        return sum(reported)

    @property
    def total_cost_usd_computed(self) -> float:
        return sum(b.cost_usd for b in self.totals_by_bucket)

    def highest_cost_bucket(self) -> BucketUsage | None:
        non_master = [b for b in self.totals_by_bucket if b.bucket != CADDIE_MASTER]
        return max(non_master, key=lambda b: b.cost_usd, default=None)


def _normalize_subagent(name: object) -> str:
    """Lowercase + ``str()`` the dispatchee name. Real logs include a
    ``"monitor"`` (lowercase) variant alongside ``"Monitor"`` from a
    different code path; treating them as one bucket avoids splitting
    the same agent's cost across two rows."""
    if name is None:
        return UNKNOWN
    text = str(name).strip()
    if not text:
        return UNKNOWN
    return text.lower()


def _iter_content_blocks(event: dict) -> Iterable[dict]:
    msg = event.get("message")
    if not isinstance(msg, dict):
        return ()
    content = msg.get("content")
    if not isinstance(content, list):
        return ()
    return (b for b in content if isinstance(b, dict))


def _add(usage: dict[str, int], delta: dict | None) -> None:
    if not isinstance(delta, dict):
        return
    for k in TOKEN_KINDS:
        v = delta.get(k, 0) or 0
        try:
            usage[k] += int(v)
        except (TypeError, ValueError):
            continue


def parse_cycle_log(path: Path) -> CycleFanout | None:
    """Walk one cycle-NNNN.json and return per-bucket attribution.

    Fail-open on parse errors — returns ``None`` so a sweep over many
    cycles can skip malformed files without crashing the report."""
    try:
        with path.open() as f:
            arr = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("subagent_fanout: skipping %s: %s", path, exc)
        return None
    if not isinstance(arr, list):
        logger.warning("subagent_fanout: %s is not a JSON array", path)
        return None

    # Cycle id from filename: ``cycle-NNNN.json`` -> ``NNNN``
    try:
        cycle_id = int(path.stem.split("-", 1)[1])
    except (IndexError, ValueError):
        cycle_id = -1

    bucket_usage: dict[str, dict[str, int]] = {}
    bucket_turns: dict[str, int] = {}
    dispatches: list[Dispatch] = []
    warnings: list[str] = []

    # Active dispatch pointer: tool_use_id of the in-flight subagent, or
    # None when attribution should go to the parent.
    active_tool_use_id: str | None = None
    active_bucket: str = CADDIE_MASTER

    # Resolve dispatch -> subagent_type lookups when the matching
    # tool_result arrives; the result event itself doesn't echo the type.
    dispatch_by_id: dict[str, str] = {}

    model: str | None = None
    total_cost_usd_reported: float | None = None

    def attribute(bucket: str, usage: dict | None) -> None:
        if not isinstance(usage, dict):
            return
        slot = bucket_usage.setdefault(
            bucket, dict.fromkeys(TOKEN_KINDS, 0),
        )
        _add(slot, usage)
        bucket_turns[bucket] = bucket_turns.get(bucket, 0) + 1

    for idx, ev in enumerate(arr):
        if not isinstance(ev, dict):
            continue
        ev_type = ev.get("type")

        if ev_type == "assistant":
            msg = ev.get("message")
            if not isinstance(msg, dict):
                continue
            if model is None:
                m = msg.get("model")
                if isinstance(m, str) and m:
                    model = m

            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None

            # Detect Agent tool_use blocks (subagent dispatches).
            new_dispatches: list[Dispatch] = []
            for blk in _iter_content_blocks(ev):
                if blk.get("type") != "tool_use" or blk.get("name") != "Agent":
                    continue
                inp = blk.get("input") if isinstance(blk.get("input"), dict) else {}
                tool_use_id = blk.get("id")
                if not isinstance(tool_use_id, str):
                    continue
                subtype = _normalize_subagent(inp.get("subagent_type"))
                desc = str(inp.get("description") or "")[:200]
                d = Dispatch(
                    tool_use_id=tool_use_id,
                    subagent_type=subtype,
                    description=desc,
                    event_idx=idx,
                )
                new_dispatches.append(d)
                dispatch_by_id[tool_use_id] = subtype

            # Attribute THIS turn before flipping the active pointer:
            # the envelope turn carrying the Agent tool_use is a
            # caddie_master decision, not the dispatchee's work.
            attribute(active_bucket, usage)

            # If this turn dispatched one or more subagents, flip the
            # active pointer to the LAST one (real logs serialize them).
            if new_dispatches:
                if active_tool_use_id is not None:
                    warnings.append(
                        f"event {idx}: new dispatch while another in"
                        f" flight ({active_tool_use_id}); attribution"
                        f" may be inaccurate",
                    )
                if len(new_dispatches) > 1:
                    warnings.append(
                        f"event {idx}: {len(new_dispatches)} Agent blocks"
                        f" in one envelope; only the last subagent_type is"
                        f" tracked as active",
                    )
                last = new_dispatches[-1]
                active_tool_use_id = last.tool_use_id
                active_bucket = last.subagent_type
                dispatches.extend(new_dispatches)

        elif ev_type == "user":
            for blk in _iter_content_blocks(ev):
                if blk.get("type") != "tool_result":
                    continue
                tool_use_id = blk.get("tool_use_id")
                if not isinstance(tool_use_id, str):
                    continue
                if tool_use_id == active_tool_use_id:
                    active_tool_use_id = None
                    active_bucket = CADDIE_MASTER
                elif tool_use_id in dispatch_by_id:
                    # Result for a known-but-not-active dispatch — implies
                    # nesting or interleaving the single-pointer model
                    # doesn't capture. Emit a warning; don't attempt to
                    # re-attribute.
                    warnings.append(
                        f"event {idx}: tool_result for {tool_use_id}"
                        f" ({dispatch_by_id[tool_use_id]}) but active is"
                        f" {active_tool_use_id};"
                        f" nested/interleaved dispatch suspected",
                    )
                # Non-Agent tool_results (Bash, Read, etc.) carry no
                # usage and don't change attribution.

        elif ev_type == "result":
            cost = ev.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                total_cost_usd_reported = float(cost)
            # Result event's own usage is parent-only; the per-turn
            # attribution above already captured the cycle total.

        # ``system`` and ``rate_limit_event`` events carry no usage.

    if active_tool_use_id is not None:
        warnings.append(
            f"cycle ended with dispatch {active_tool_use_id}"
            f" ({dispatch_by_id.get(active_tool_use_id, '?')}) still"
            f" in flight; trailing turns attributed to its bucket",
        )

    bill_model = model or "claude-sonnet-4-6"
    if bill_model not in PRICING:
        warnings.append(
            f"unknown model {bill_model!r}; bucket costs computed at"
            f" Sonnet rates (update PRICING in src/gimmes/budget.py)",
        )
    buckets: list[BucketUsage] = []
    for name, usage in bucket_usage.items():
        cost = cost_from_usage(usage, bill_model)
        buckets.append(BucketUsage(
            bucket=name,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_creation_input_tokens=usage["cache_creation_input_tokens"],
            cache_read_input_tokens=usage["cache_read_input_tokens"],
            cost_usd=cost,
            turn_count=bucket_turns.get(name, 0),
        ))
    buckets.sort(key=lambda b: (-b.cost_usd, b.bucket))

    return CycleFanout(
        cycle_id=cycle_id,
        log_path=path,
        model=bill_model,
        total_cost_usd_reported=total_cost_usd_reported,
        total_events=len(arr),
        dispatches=tuple(dispatches),
        buckets=tuple(buckets),
        warnings=tuple(warnings),
    )


def build_summary(cycles: Iterable[CycleFanout]) -> FanoutSummary:
    """Roll cycle attributions up across multiple cycles.

    Costs are summed from each cycle's per-bucket cost (which was already
    computed at that cycle's billed model). This is mathematically equal
    to re-pricing aggregated tokens at one rate when every cycle uses the
    same model — and remains correct when cycles span multiple models
    (e.g. an Opus → Sonnet rotation), where re-pricing aggregated tokens
    at one rate would silently misprice the others.
    """
    cycles_t = tuple(cycles)
    totals: dict[str, dict[str, int]] = {}
    turns: dict[str, int] = {}
    cost_by_bucket: dict[str, float] = {}
    dispatch_counts: dict[str, int] = {}
    warnings: list[str] = []

    for cyc in cycles_t:
        for b in cyc.buckets:
            slot = totals.setdefault(
                b.bucket, dict.fromkeys(TOKEN_KINDS, 0),
            )
            slot["input_tokens"] += b.input_tokens
            slot["output_tokens"] += b.output_tokens
            slot["cache_creation_input_tokens"] += b.cache_creation_input_tokens
            slot["cache_read_input_tokens"] += b.cache_read_input_tokens
            turns[b.bucket] = turns.get(b.bucket, 0) + b.turn_count
            cost_by_bucket[b.bucket] = cost_by_bucket.get(b.bucket, 0.0) + b.cost_usd
        for d in cyc.dispatches:
            dispatch_counts[d.subagent_type] = (
                dispatch_counts.get(d.subagent_type, 0) + 1
            )
        warnings.extend(f"cycle {cyc.cycle_id}: {w}" for w in cyc.warnings)

    bucket_totals = [
        BucketUsage(
            bucket=name,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_creation_input_tokens=usage["cache_creation_input_tokens"],
            cache_read_input_tokens=usage["cache_read_input_tokens"],
            cost_usd=cost_by_bucket.get(name, 0.0),
            turn_count=turns.get(name, 0),
        )
        for name, usage in totals.items()
    ]
    bucket_totals.sort(key=lambda b: (-b.cost_usd, b.bucket))

    return FanoutSummary(
        cycles=cycles_t,
        totals_by_bucket=tuple(bucket_totals),
        dispatch_counts_by_type=dispatch_counts,
        warnings=tuple(warnings),
    )


def render_markdown(summary: FanoutSummary) -> str:
    """Render the deliverable markdown for ``tests/research/subagent_fanout.md``.

    The output is deterministic given the same inputs (sorted bucket order,
    fixed table columns), so the file can be regenerated and diffed."""
    lines: list[str] = []
    lines.append("# Subagent fanout characterization (Phase 1 of #571)")
    lines.append("")
    if summary.total_cost_usd_reported is None:
        reported_str = "**?** (no cycle reported a total)"
    else:
        coverage = ""
        if summary.cycles_with_reported_cost < summary.cycles_audited:
            coverage = (
                f" ({summary.cycles_with_reported_cost} of"
                f" {summary.cycles_audited} cycles reported)"
            )
        reported_str = f"**${summary.total_cost_usd_reported:.2f}**{coverage}"
    lines.append(
        f"Audited **{summary.cycles_audited}** cycles.  "
        f"Sum of reported `result.total_cost_usd`: {reported_str}.  "
        f"Sum of bucket costs (this report): "
        f"**${summary.total_cost_usd_computed:.2f}**.",
    )
    lines.append("")

    # Table 1 — dispatches per cycle by type
    all_subagents = sorted({
        d.subagent_type
        for c in summary.cycles
        for d in c.dispatches
    })
    lines.append("## Table 1 — Dispatches per cycle by subagent type")
    lines.append("")
    header = ["cycle", "events", "cost_usd"] + all_subagents + ["total"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---:"] * len(header)) + "|")
    for c in summary.cycles:
        counts = c.dispatch_count_by_type()
        cost = (
            f"${c.total_cost_usd_reported:.2f}"
            if c.total_cost_usd_reported is not None
            else "?"
        )
        row = [str(c.cycle_id), str(c.total_events), cost]
        row.extend(str(counts.get(s, 0)) for s in all_subagents)
        row.append(str(len(c.dispatches)))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Table 2 — cost share by bucket (aggregate)
    lines.append("## Table 2 — Cost share by bucket (aggregate)")
    lines.append("")
    total_cost = summary.total_cost_usd_computed
    header2 = [
        "bucket", "turns", "input_tok", "output_tok",
        "cache_creation_tok", "cache_read_tok", "cost_usd", "% of total",
    ]
    lines.append("| " + " | ".join(header2) + " |")
    lines.append("|" + "|".join([":---"] + ["---:"] * (len(header2) - 1)) + "|")
    for b in summary.totals_by_bucket:
        pct = (100.0 * b.cost_usd / total_cost) if total_cost > 0 else 0.0
        lines.append(
            f"| {b.bucket} | {b.turn_count} | {b.input_tokens:,} |"
            f" {b.output_tokens:,} | {b.cache_creation_input_tokens:,} |"
            f" {b.cache_read_input_tokens:,} | ${b.cost_usd:.2f} | {pct:.1f}% |",
        )
    lines.append("")

    highest = summary.highest_cost_bucket()
    if highest is not None:
        pct = (
            (100.0 * highest.cost_usd / total_cost) if total_cost > 0 else 0.0
        )
        lines.append(
            f"**Highest-cost subagent path:** `{highest.bucket}` —"
            f" ${highest.cost_usd:.2f} ({pct:.1f}% of total)."
            f" {highest.turn_count} attributed turns across"
            f" {summary.dispatch_counts_by_type.get(highest.bucket, 0)} dispatches.",
        )
    lines.append("")

    # Table 3 — per-cycle decomposition
    lines.append("## Table 3 — Per-cycle cost decomposition")
    lines.append("")
    lines.append(
        "| cycle | total | top-1 | top-2 | caddie_master |",
    )
    lines.append("| ---: | ---: | :--- | :--- | ---: |")
    for c in summary.cycles:
        cm = c.cost_by_bucket().get(CADDIE_MASTER, 0.0)
        non_master = [b for b in c.buckets if b.bucket != CADDIE_MASTER]
        top1 = non_master[0] if len(non_master) >= 1 else None
        top2 = non_master[1] if len(non_master) >= 2 else None
        cost = (
            f"${c.total_cost_usd_reported:.2f}"
            if c.total_cost_usd_reported is not None
            else "?"
        )

        def fmt(b: BucketUsage | None) -> str:
            if b is None:
                return "—"
            return f"{b.bucket} (${b.cost_usd:.2f})"

        lines.append(
            f"| {c.cycle_id} | {cost} | {fmt(top1)} | {fmt(top2)} |"
            f" ${cm:.2f} |",
        )
    lines.append("")

    if summary.warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in summary.warnings:
            lines.append(f"- {w}")
        lines.append("")

    return "\n".join(lines)
