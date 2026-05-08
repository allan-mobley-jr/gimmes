# Phase 1 backtest — research notes

**Status:** Scaffolding committed and runs against current data. Statistically inconclusive at today's N. Two structural blockers prevent the issue's full-fidelity analysis from being done with currently-persisted data.

**Issue:** #553 (parent #546)  
**Run command:** `uv run python tests/research/pause_and_hour_backtest.py`  
**As-of run date:** 2026-05-08

## What ran

30-day window cutoff: `2026-04-08`.

| Source | Rows |
|---|---:|
| `trades` events (open/close/size_up) | 28 |
| `candidates` scans | 317 |
| Closed positions (after FIFO weighted-average pairing per #561) | **6** |

## Output

### Table 1 — pause-length vs realized PnL

| pause_s | trades_surfaced | gross_pnl |
|---:|---:|---:|
| 60   | 0 | \$0.00      |
| 120  | 0 | \$0.00      |
| 300  | 2 | \$-258.81   |
| 600  | 2 | \$-258.81   |
| 900  | 2 | \$-258.81   |

### Table 2 — hour-of-window vs realized PnL

| hour_of_window | count | gross_pnl |
|---:|---:|---:|
| h+1 | 2 | \$-108.62 |
| h+6 | 1 | \$-13.00  |
| (out of window) | 3 | \$-240.69 |

## What this does NOT show

The numbers above are **not** a Phase 1 finding. They're the scaffolding's first run on insufficient data, recorded for traceability. Three problems:

### 1. N is too small (N=6 closed positions in 30 days)

A 5-bucket pause comparison needs at least N=30 per bucket to detect anything but a huge effect. AC's projected data-ready date (`~2026-06-06`) assumes ~5 closes/week steady-state; getting to N≥30 per bucket would take months at current trade rates. The scaffolding will run again automatically when more data accumulates.

### 2. Pause-length counterfactual is fundamentally limited

The script approximates "would this trade still surface under longer pause?" by checking the gap between the latest candidate scan and the actual open timestamp. That ignores the dominant cost: the Caddie Master subprocess itself runs for 15–30 minutes per cycle. The configurable `--pause` flag is the gap *between* subprocesses; for any pause in the 60s–900s range, real cycle frequency is dominated by subprocess runtime, not the pause. Changing pause from 60s to 900s adds at most ~14 minutes between cycles — small relative to the 15–30 min cycle itself.

The original issue framed pause as the lever for "how often we look at the market." The actual lever is sub-agent depth (which determines subprocess runtime). Phase 1's pause-vs-PnL question may simply be the wrong question against current architecture.

### 3. Counterfactual entry-price drift cannot be modeled

The issue itself flags this: "Requires reconstructing Kalshi historical mid-price trajectories for candidates we never traded — not data we currently persist." The `candidates` table snapshots `market_price` at scan time but not in between scans. A "missed by one cycle" candidate could have moved by any amount before the next scan; we don't know.

Two remediation paths:
- **Persist quote trajectories**: add a periodic Kalshi `/markets` poller that writes mid-price snapshots independent of cycle scans. ~5-min granularity is probably enough. New table `quote_history`. ~1 week of work.
- **Use Kalshi's historical-data API**: paid tier. Operationally simpler but adds an external dependency.

Neither is in scope for #553; both are prerequisites if Phase 1's pause analysis is to land in its full-fidelity form.

## Honest recommendation

**Defer Phase 1's pause analysis.** The pause lever is too weak against current architecture for a 30-day backtest to produce actionable signal. Pivot the parent #546 question to:

1. **Hour-of-window alpha localization** (Table 2 above) — the scaffolding handles this and produces meaningful output once N is sufficient. Likely actionable as a calendar-narrowing follow-up similar to #558.
2. **Sub-agent depth tuning** — what's the marginal P&L improvement from the 200th-300th turn vs cycles capped at 100 turns? This is the real lever for cost reduction (#552 found cycle cost is dominated by intra-cycle conversation building, not inter-cycle gaps). Would need new instrumentation.

Phase 1's literal pause-vs-PnL deliverable is feasible only after persistent quote logging lands. Until then, the scaffolding above runs on whatever data exists and the tables stay non-significant. Re-run periodically; Table 2 will become actionable first.

## Re-running

```bash
cd ~/gimmes
uv run python tests/research/pause_and_hour_backtest.py
```

Output writes to `tests/research/output/{pause_vs_pnl,hour_vs_pnl,closed_trades}.csv`. The script is idempotent and reads the live database; no fixtures or mocking.
