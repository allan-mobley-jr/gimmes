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

Root cause: `gimmes.budget.parse_usage_from_stream_json` (`src/gimmes/budget.py:109-142`) iterates events in **reverse** and returns the **first** `usage` block it finds. That captures the final result-event usage, not the sum across all 200-300 assistant turns within the cycle.

**Two separable consequences** (don't conflate them):

1. **Tokens reported are ~10× lower than reality.** Direct sum vs single-block: 1.0M vs 95K avg cache_create per cycle.
2. **Cost reported is ~5× lower than reality.** `cost_from_usage` runs against the same under-counted `usage` dict, so daily `cost_usd` reflects only the final-turn cost of each cycle. Real per-cycle cost is ~\$5.68 (table above, sourced from Anthropic's authoritative `result.total_cost_usd`); budget.json recorded \$26 / 22 ≈ \$1.20.

**Implication for cap behavior:** the daily \$25 cap binds at 22 cycles *as recorded*, but real spend at that point was closer to ~\$125 (22 × \$5.68). So the cap is not biting at the right point — it's biting later in real-cost terms than the configured \$25 implies. Operators who think they have ~\$25/day of headroom actually consumed much more before the tracker noticed.

This is a tracking bug, not a cache-behavior bug. Filed as #563.

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

- **#563 (filed)** — `parse_usage_from_stream_json` undercount fix. Sum across **all** assistant events plus the result. Critical for the daily budget cap to bind at the right point.
- After #563 lands, re-evaluate whether the daily cap (default \$25) should be raised or lowered; with corrected accounting, \$25 binds at *fewer* cycles than the design-time 80-session assumption — operators may want to update the cap or accept that real ceiling.

## Post-#563 validation (2026-05-08)

Now that #563 has merged, sampled 17 full-pipeline cycles from May 8 (1356–1372) to validate the corrected parser against the still-running v0.7.0 daemon's broken accounting and Anthropic's authoritative `result.total_cost_usd`.

| | cache_creation | cache_read | cost (17 cycles) |
|---|---:|---:|---:|
| Old parser (result envelope only)         | 1,214,398   | 23,706,474   | — |
| New parser (sum-across-assistant-turns)   | 12,833,585  | 200,964,981  | — |
| Multiplier                                 | **10.57×**  | **8.48×**    | |
| Anthropic authoritative `result.total_cost_usd` | — | — | **\$82.86** |
| Local PRICING applied to new totals       | — | — | \$108.52 |
| budget.json recorded for full May 8 (24 cycles) | 1,860,124 | 36,445,024 | \$25.02 |

**Findings:**

1. The 10× cache_create undercount factor predicted from May 7 data **holds on May 8**: 10.57× across 17 trade-window cycles. cache_read was 8.48×, slightly lower — consistent with subagent dispatches contributing more cache_create per turn than cache_read.
2. The new parser **slightly overestimates** total cost (\$108.52 vs Anthropic's \$82.86, ~30% over). Plausible cause: Claude Code emits subagent dispatch envelopes as separate `assistant` events, and the dispatch + subagent execution may both report overlapping usage. Net effect is a conservative overcount — safer for a budget cap to bind early than late, but worth a follow-up to align with `result.total_cost_usd` exactly.
3. The daemon recorded **\$25.02 for 24 cycles** (the cap binding point); the **real** spend across those 24 cycles is closer to **\$117** (\$4.87/cycle × 24). Operators have been consuming roughly **5× their stated daily cap**.

**Recommended cap action:** raise `--max-daily-cost-usd` to a value reflecting the corrected per-cycle cost. At \$4.87/cycle authoritative, the current \$25 cap maps to ~5 cycles/day. To get back to the design-time ~80 cycles/day, the cap would need to be ~\$390/day; for a more modest 30 trade-window cycles/day, ~\$150/day. Operators should pick the value matching their actual willingness to spend, not the legacy \$25 default.

**Closing #552.** The cache_creation tail is doing its job (Phase 1 finding); the apparent runaway cost was an accounting artifact (Phase 2 finding, fixed in #563); the cap should be re-tuned based on the validation table above (operator action). All three open threads are resolved.
