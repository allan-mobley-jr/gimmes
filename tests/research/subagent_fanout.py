"""Phase 1 driver for #571: characterize current subagent fanout.

Walks a stratified sample of recent ``cycle-NNNN.json`` logs from
``${GIMMES_HOME}/logs`` and emits:

- ``tests/research/output/subagent_fanout_per_cycle.csv`` — Table 1 data.
- ``tests/research/output/subagent_fanout_buckets.csv`` — Table 2 data.
- Markdown tables to stdout (the deliverable
  ``tests/research/subagent_fanout.md`` is hand-curated and embeds these
  tables — same handoff pattern as ``pause_and_hour_backtest.py`` →
  ``pause_and_hour_backtest.md``).

Re-run as more data accumulates; cycle picks are listed in
``DEFAULT_CYCLES`` and reflected in the markdown's Methodology section so
the tables are reproducible.

Run from the repo root:

    uv run python tests/research/subagent_fanout.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from gimmes.reporting.subagent_fanout import (  # noqa: E402
    build_summary,
    parse_cycle_log,
    render_markdown,
)

LOGS_DIR = Path.home() / ".gimmes" / "logs"
OUT_DIR = Path(__file__).resolve().parent / "output"

# Stratified sample: 3 standard outside-window cycles (4 dispatches each),
# 2 research cycles (Caddie present, no trade), 1 trade-execution cycle
# (Closer present). Picked from a survey of cycles 1340-1373 to span the
# observed cost range ($3.10-$7.64) and dispatch-count range (4-8).
DEFAULT_CYCLES: tuple[int, ...] = (
    1361,  # 4 dispatches, $3.52 — low end of standard
    1366,  # 4 dispatches, $3.71 — typical standard
    1369,  # 4 dispatches, $4.67 — high end of standard
    1367,  # 5 dispatches, $4.94 — single-Caddie research
    1373,  # 5 dispatches, $5.34 — single-Caddie research, recent
    1364,  # 8 dispatches, $4.47 — Closer present, multi-Caddie execution
)


def _resolve_cycles(ids: tuple[int, ...]) -> list[Path]:
    paths = []
    for n in ids:
        p = LOGS_DIR / f"cycle-{n}.json"
        if not p.exists():
            print(f"warn: {p} not found, skipping", file=sys.stderr)
            continue
        paths.append(p)
    return paths


def _write_csvs(summary, paths: list[Path]) -> None:
    """Write per-cycle and per-bucket CSVs in **long form**.

    Long form (one row per ``(cycle, subagent_type)``) keeps the schema
    constant across runs even when different cycle samples include
    different subagent types. Wide form (one column per subagent type)
    would scramble columns between runs and make CSV diffs noisy.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with (OUT_DIR / "subagent_fanout_per_cycle.csv").open("w") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "cycle_id", "events", "cost_usd_reported",
                "cost_usd_computed", "subagent_type", "dispatch_count",
            ],
        )
        w.writeheader()
        for c in summary.cycles:
            counts = c.dispatch_count_by_type()
            base = {
                "cycle_id": c.cycle_id,
                "events": c.total_events,
                "cost_usd_reported": c.total_cost_usd_reported,
                "cost_usd_computed": round(c.computed_total_cost_usd(), 6),
            }
            # Emit one row per dispatched subagent_type. Sorted so two
            # runs over the same cycles produce byte-identical output.
            for subtype in sorted(counts):
                w.writerow({
                    **base,
                    "subagent_type": subtype,
                    "dispatch_count": counts[subtype],
                })

    with (OUT_DIR / "subagent_fanout_buckets.csv").open("w") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "bucket", "turns",
                "input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens",
                "cost_usd",
            ],
        )
        w.writeheader()
        for b in summary.totals_by_bucket:
            w.writerow({
                "bucket": b.bucket,
                "turns": b.turn_count,
                "input_tokens": b.input_tokens,
                "output_tokens": b.output_tokens,
                "cache_creation_input_tokens": b.cache_creation_input_tokens,
                "cache_read_input_tokens": b.cache_read_input_tokens,
                "cost_usd": round(b.cost_usd, 6),
            })


def main() -> None:
    paths = _resolve_cycles(DEFAULT_CYCLES)
    if not paths:
        print(
            f"error: no cycle logs found under {LOGS_DIR}; cannot render"
            f" deliverable. Run a Caddie Master cycle first.",
            file=sys.stderr,
        )
        sys.exit(1)

    cycles = [c for c in (parse_cycle_log(p) for p in paths) if c is not None]
    if not cycles:
        print(
            "error: every selected cycle log failed to parse; check"
            f" {LOGS_DIR} for JSON validity.",
            file=sys.stderr,
        )
        sys.exit(2)

    summary = build_summary(cycles)
    _write_csvs(summary, paths)

    print(render_markdown(summary))
    print(
        f"\nWrote {(OUT_DIR / 'subagent_fanout_per_cycle.csv').relative_to(ROOT)},"
        f" {(OUT_DIR / 'subagent_fanout_buckets.csv').relative_to(ROOT)}",
    )


if __name__ == "__main__":
    main()
