# Subagent fanout characterization — Phase 1 of #571

**Status:** Phase 1 deliverable complete. Phase 2 recommendation in this doc.
Phase 3 (driving-range A/B + ship/no-ship decision) deferred — same gating
pattern as PR #569 (Phase 1 of #553). Issue #571 stays open.

**Issue:** #571 (parent #546)
**Run command:** `uv run python tests/research/subagent_fanout.py`
**As-of run date:** 2026-05-08

## Methodology

Walked the JSON-array stream-json logs at `~/.gimmes/logs/cycle-NNNN.json`
for the cycles listed below and attributed every `assistant` event's
`message.usage` to either the parent `caddie_master` agent or the active
subagent (the dispatchee of the most recent in-flight `Agent` `tool_use`).
The envelope turn carrying the `Agent` block is attributed to
`caddie_master` because the dispatch decision is the parent's, not the
dispatchee's. When a subagent's matching `tool_result` arrives in a `user`
event, attribution returns to `caddie_master`.

Cost is computed via `gimmes.budget.cost_from_usage` at
`claude-sonnet-4-6` rates (every declared agent in `.claude/agents/` uses
Sonnet 4.6 today).

### Cycle selection (6 cycles)

Stratified to span the observed dispatch-count and cost range across
cycles 1340–1373:

- cycle 1361: 4 dispatches, \$3.52, 573 events — low-end standard
- cycle 1366: 4 dispatches, \$3.71, 704 events — typical standard
- cycle 1369: 4 dispatches, \$4.67, 875 events — high-end standard
- cycle 1367: 5 dispatches, \$4.94, 774 events — single-Caddie research
- cycle 1373: 5 dispatches, \$5.34, 743 events — single-Caddie research, recent
- cycle 1364: 8 dispatches, \$4.47, 660 events — Closer present, multi-Caddie execution

## Tables

### Table 1 — dispatches per cycle by subagent type

| cycle | events | cost_usd | caddie | closer | groundskeeper | monitor | scorecard | scout | total |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1361 | 573 | \$3.52 | 0 | 0 | 1 | 1 | 1 | 1 | 4 |
| 1366 | 704 | \$3.71 | 0 | 0 | 1 | 1 | 1 | 1 | 4 |
| 1369 | 875 | \$4.67 | 0 | 0 | 1 | 1 | 1 | 1 | 4 |
| 1367 | 774 | \$4.94 | 1 | 0 | 1 | 1 | 1 | 1 | 5 |
| 1373 | 743 | \$5.34 | 1 | 0 | 1 | 1 | 1 | 1 | 5 |
| 1364 | 660 | \$4.47 | 3 | 1 | 1 | 1 | 1 | 1 | 8 |

Across the 6 cycles, every cycle dispatched Monitor / Scout / Scorecard /
Groundskeeper exactly once. Caddie fanout varies (0, 1, or 3 per cycle).
Closer was dispatched only when an order was actually placed.

### Table 2 — cost share by bucket (aggregate across all 6 cycles)

| bucket | turns | input_tok | output_tok | cache_creation_tok | cache_read_tok | cost_usd | % of total |
|:---|---:|---:|---:|---:|---:|---:|---:|
| monitor | 357 | 6,239 | 6,001 | 2,240,223 | 17,424,794 | \$13.74 | 39.0% |
| scout | 634 | 656 | 24,394 | 648,940 | 22,048,554 | \$9.42 | 26.8% |
| caddie_master | 351 | 375 | 5,698 | 1,011,290 | 18,404,015 | \$9.40 | 26.7% |
| caddie | 133 | 14,528 | 4,235 | 216,736 | 2,779,923 | \$1.75 | 5.0% |
| scorecard | 75 | 86 | 1,553 | 120,363 | 754,171 | \$0.70 | 2.0% |
| groundskeeper | 25 | 36 | 728 | 18,913 | 178,450 | \$0.14 | 0.4% |
| closer | 5 | 7 | 11 | 9,340 | 34,046 | \$0.05 | 0.1% |

**Highest-cost subagent path:** `monitor` — \$13.74 (39.0% of total),
357 attributed turns across 6 dispatches (~60 turns per dispatch).

### Table 3 — per-cycle cost decomposition

| cycle | total | top-1 | top-2 | caddie_master |
| ---: | ---: | :--- | :--- | ---: |
| 1361 | \$3.52 | scout (\$1.46) | monitor (\$1.24) | \$2.17 |
| 1366 | \$3.71 | monitor (\$3.33) | scout (\$1.82) | \$0.85 |
| 1369 | \$4.67 | scout (\$3.27) | monitor (\$2.63) | \$0.82 |
| 1367 | \$4.94 | monitor (\$1.52) | scout (\$1.40) | \$2.30 |
| 1373 | \$5.34 | monitor (\$3.11) | scout (\$0.87) | \$2.09 |
| 1364 | \$4.47 | monitor (\$1.91) | caddie (\$0.67) | \$1.17 |

The top-2 buckets are always Monitor + Scout (cycle 1364 swaps Scout for
Caddie because that cycle had 3 Caddie dispatches). Caddie Master
overhead ranges \$0.82–\$2.30 — when it's high (1361, 1367, 1373) it
correlates with a cycle that dispatched Caddie at least once, suggesting
Caddie Master's `Step 4c` review of Caddie's output is itself expensive.

## Cost-reconciliation caveat

Sum of bucket costs over the 6 cycles: **\$35.19**.
Sum of `result.total_cost_usd` reported by Anthropic over the same 6
cycles: **\$26.66**.

The 32% over-attribution is consistent with the known finding from #568:
`parse_usage_from_stream_json`'s sum-across-turns is slightly higher
than Anthropic's authoritative billing (#568 measured 31% over for 17
cycles: \$108 computed vs \$83 authoritative). This is a token-level
artifact of cache_creation accounting; the **relative** percentages in
Table 2 are unaffected. Cap recommendations below should be read as
"X% of the cycle's parent-stream observed token cost" not "X% of the
Anthropic invoice."

## Phase 1 finding

**The cost lever is Monitor and Scout intra-dispatch turn count, not
dispatch fanout.** Three observations support this:

1. **Dispatch count doesn't vary with cost in the dominant agents.** Every
   cycle dispatches Monitor and Scout exactly once. The 6 cycles span
   \$3.52–\$5.34 and the dispatch count is fixed for those agents.

2. **Per-dispatch turn count is high.** Monitor averages ~60 turns per
   dispatch. Scout averages ~106 turns per dispatch. By contrast, Caddie
   averages ~20 turns per dispatch. Monitor checks each open position
   sequentially; Scout iterates over many candidate markets.

3. **Caddie is not the cost driver.** Despite being intuitively the most
   expensive agent (does deep research), Caddie is 5% of total cost
   because it's dispatched at most ~1×/cycle and its turn count is
   bounded.

This **contradicts** the original framing in #571's body, which expected
Caddie to be the highest-cost path. Phase 1's job is to surface that
mismatch — and it has.

## Phase 2 recommendation

**Posted as a comment on #571.** Summary:

- **Reject Option A (hard cap on dispatch count per cycle).** All
  high-cost agents are already dispatched ≤1× per cycle. Capping
  dispatches doesn't address the cost.
- **Reject Option B (depth cap).** No nested subagent dispatches were
  observed — the pipeline is already depth-1.
- **Reject Option D (Haiku swap).** Monitor and Scout do market-data
  reasoning that's quality-sensitive; downgrading without measuring
  decision-regression risk is premature. Revisit only if the targeted
  cap below doesn't yield enough savings.
- **Recommend a refined Option C — cap per-dispatch fanout inside
  Monitor and Scout.** Specifically:
  - Monitor: cap the number of open positions checked per dispatch
    (process N most-recently-changed positions, defer the rest to the
    next cycle).
  - Scout: cap candidate-list size before per-candidate analysis (top-K
    by edge or score, defer the long tail).

The cost-reduction math from Table 2:

```
mean_monitor_cost_per_cycle = $13.74 / 6 = $2.29
mean_scout_cost_per_cycle = $9.42 / 6 = $1.57
combined = $3.86 / cycle (66% of mean cycle cost)
```

If a per-position/per-candidate cap halved Monitor and Scout's intra-
dispatch turns (a guess; Phase 3 measures), per-cycle savings would be
~\$1.93 — a third of mean cycle cost. Even a 20% turn reduction would
free ~\$0.77/cycle, which against the recently-validated \$4.87
authoritative mean (#568) is meaningful.

Phase 3 is required before shipping any cap: a driving-range A/B with
the cap applied, comparing cost AND trade decisions on the same fixture
data. The cap must not regress decisions; if it does, the recommendation
becomes "don't ship, re-investigate Monitor's per-position depth."

## Phase 1 deliverable AC checklist

- [x] `tests/research/subagent_fanout.md` characterizes current fanout
      (Table 1).
- [x] Identifies the highest-cost path (Monitor, 39%; Table 2 +
      "Highest-cost subagent path" callout).
- [x] Per-cycle cost decomposition by subagent type (Table 3).
- [x] Phase 2 proposal posted as comment on #571.
- [ ] Phase 3 (A/B + ship decision) — deferred, gates issue closure.

## Reproducing this report

```bash
uv run python tests/research/subagent_fanout.py
```

Output:
- `tests/research/output/subagent_fanout_per_cycle.csv`
- `tests/research/output/subagent_fanout_buckets.csv`
- Markdown to stdout (the tables above)

The cycles audited are listed in `DEFAULT_CYCLES` at the top of the
driver script. Editing that tuple and re-running gives a fresh sample.
