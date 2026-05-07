# Cache creation diff — investigation for #552

**Status:** Phase 1 complete. Phase 2 (controlled --pause comparison) not run — Phase 1 findings reframed the question and made Phase 2 less informative than originally planned. See "Reframing" below.

## Sample

7 cycles from May 7 2026 (UTC), spanning 22:06 ET May 6 → 05:35 ET May 7. All cycles ran the full Caddie Master pipeline against the live database; inter-cycle gap averaged 25–30 minutes (well past the 5-minute ephemeral cache TTL).

| Cycle | Wall time (ET) | Turns | cache_create (cycle total) | cache_read (cycle total) | First-turn cache_create | First-turn cache_read | Cost |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1330 | May 6 22:06 | 346 | 1,146,085 | 18,776,389 | **0** | 15,095 | $7.95 |
| 1331 | May 6 22:41 | 316 | 1,228,648 | 14,784,912 | **0** | 15,095 | $7.13 |
| 1332 | May 6 23:09 | 274 | 1,272,159 | 10,560,264 | **0** | 15,095 | $4.75 |
| 1333 | May 6 23:38 | 308 | 1,014,172 | 14,437,716 | **0** | 15,095 | $6.01 |
| 1340 | May 7 02:54 | 275 | 702,035 | 11,658,636 | **0** | 15,095 | $4.61 |
| 1345 | May 7 04:19 | 199 | 882,765 | 8,642,297 | **0** | 15,095 | $4.00 |
| 1348 | May 7 05:35 | 261 | 880,387 | 11,502,502 | **0** | 15,095 | $5.34 |
| **avg** | | **283** | **1,018,036** | **12,908,959** | **0** | **15,095** | **$5.68** |

`cr:cc` ratio: **12.7:1** (cache works well overall).

## Diff against the issue's hypothesis

The issue body framed the problem as "~95K tokens/cycle of fresh cache writes despite 5-min TTL gap." Two findings break that framing:

### 1. The system-prompt prefix DOES survive across cycles

Every cycle's first assistant turn shows `cache_create=0, cache_read=15,095`. The 15K Claude Code system-prompt scaffolding is hitting cache cleanly across the 25–30-minute inter-cycle gap. **The 5-minute ephemeral TTL is not the binding constraint** — Claude Code's prefix appears to use the longer-TTL cache slot, and the prefix string is byte-identical across cycles.

### 2. The cache_create totals are ~10× higher than the budget tracker reports

Issue body cites `budget.json` totals: 2.085M cache_create / 22 sessions = **~95K avg/cycle**. Direct stream-json analysis of 7 cycles shows **~1M avg/cycle**. The tracker is recording roughly 1/10th of the real cache_create.

Root cause: `gimmes.budget.parse_usage_from_stream_json` (budget.py:109-142) iterates events in **reverse** and returns the **first** `usage` block it finds. That captures the final result-event usage, not the sum across all 200-300 assistant turns within the cycle. **All cycle accounting since #545/v0.7.0 has been undercounting cache_creation by ~10×**, which is why the cap was thought to bind at 22 cycles — it actually binds where it should given the real cycle cost (~$5/cycle in trade windows, $26/22 ≈ $1.20 if ~1/4 of cycles were full-pipeline trade-window runs and the rest monitor-only).

This is a tracking bug, not a cache-behavior bug. **Filed as a follow-up issue.**

## What the cache_create actually is

The 1M tokens/cycle of cache_create is **intra-cycle**: spread across 200-346 assistant turns per cycle. Each subagent dispatch (Monitor, Scout, Caddie, Closer, Scorecard) and tool-result return adds new content that gets cached for downstream turns within the same cycle. The 12M cache_read/cycle confirms the intra-cycle reuse — every turn after the first reads back what earlier turns cached.

This is not a category 2 ("Claude Code metadata bleed") or category 3 ("prompt section ordering drift") problem from the issue's hypothesis list. It's category 1: **legitimately dynamic intra-cycle conversation building**. Different markets each cycle → different Bash output, Read content, subagent results → new content to cache.

## Reframing — Phase 2 not run

The issue's Phase 2 plan was to compare `--pause 0` vs `--pause 600` and check whether the cache_create/cache_read ratio differs. With Phase 1 showing first-turn `cache_create=0` at 25–30 min gaps, the prefix is already cache-stable; running shorter `--pause` would not meaningfully reduce intra-cycle cache_create (which is what dominates the cost). Phase 2 would mostly confirm what Phase 1 already established.

If a future investigation wants to validate the inter-cycle prefix TTL at longer gaps (60+ min), Phase 2 is still useful, but it isn't the lever to pull for the original cost concern.

## Recommendation — option D (document as known floor)

The cache_creation tail is doing its job. The ~$5/cycle full-pipeline cost is a function of agentic conversation depth (200-300 turns × ~3K cache-create tokens each), not cache misconfiguration. Mitigations from the issue's option list:

- **A. ephemeral_1h cache** — would not help. The 1M cache_create/cycle is intra-cycle, not inter-cycle. Longer TTL doesn't reduce intra-cycle writes.
- **B. Long-running daemon** — would eliminate per-cycle subprocess startup cost (the 15K-token prefix re-read), but that's already $0.0023 per cycle (15,095 tokens × $0.30/M cache_read for Sonnet) — total ~$0.05/day across 22 cycles. Not worth the rewrite.
- **C. Strip Claude Code metadata** — first turn `cache_create=0` proves no metadata bleed. Nothing to strip.
- **D. Document as known floor** — recommended. Real lever is **agent depth**, not cache: capping subagent fanout or pruning long tool results would reduce cache_create proportionally. That's a strategy/agent-design conversation, not a cache-tuning conversation.

## Follow-up

- File issue: `parse_usage_from_stream_json` returns first-found usage block, undercounting cache_create by ~10×. Fix: sum `cache_creation_input_tokens` and `cache_read_input_tokens` across **all** assistant events plus the result. Apply to `BudgetTracker.record_cycle` so daily totals reflect reality. Critical for the daily budget cap to bind at the right point.
- After the tracker fix lands, re-evaluate whether the daily cap (default $25) should be raised; with corrected accounting, $25 may bind at far fewer cycles than the design-time 80-session assumption.
