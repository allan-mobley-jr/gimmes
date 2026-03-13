---
name: caddy-shack
description: Autonomous trading cycle orchestrator — runs one complete Monitor → Scout → Caddie → Closer → Scorecard pipeline cycle
user_invocable: true
---

# /caddy-shack — Autonomous Trading Cycle

Run one complete autonomous trading cycle. This skill is invoked by the `driving_range` and `championship` CLI commands in a loop. Each invocation is one cycle — the CLI handles re-invocation.

**Do NOT ask the user any questions.** Make all decisions based on config parameters, market data, and database state. Operate fully autonomously.

## Cycle Steps

### Step 0: Log Cycle Start

Log the beginning of this cycle to the activity feed. The cycle number is passed via the `GIMMES_CYCLE` env var (default to 0 if not set):

```bash
python -m gimmes log-activity --cycle $GIMMES_CYCLE --agent orchestrator --phase start --message "Cycle $GIMMES_CYCLE started"
```

### Step 1: State Check

Before doing anything, assess the current state:

```bash
python -m gimmes risk-check
python -m gimmes positions
```

**Decision gates:**
- If daily loss limit is breached → skip to Step 6 (Scorecard only)
- If position count is at maximum → run Step 2 (Monitor) then skip to Step 6 (Scorecard) — no new trades
- Otherwise → proceed with full cycle

### Step 2: Monitor (if positions exist)

If there are open positions, dispatch the **Monitor** agent to review them.

Launch the Monitor agent (`monitor.md`) to:
1. Review all open positions with mark-to-market data
2. Check for material news or price movements
3. Recommend HOLD, CLOSE, or SIZE UP for each position

**If Monitor recommends CLOSE on any position:**
Dispatch the **Closer** agent to execute the close. Run:
```bash
python -m gimmes cancel ORDER_ID  # For resting orders to close
```

Log all close decisions to the database.

### Step 3: Scout

Dispatch the **Scout** agent to scan for new gimme candidates.

Launch the Scout agent (`scout.md`) to:
1. Run `python -m gimmes scan` to fetch and filter markets
2. Score the top candidates
3. Return a ranked shortlist

Log Scout completion:
```bash
python -m gimmes log-activity --cycle $GIMMES_CYCLE --agent scout --phase complete --message "Scout found N candidates"
```

**If Scout finds no candidates**, skip to Step 6.

### Step 4: Caddie

For each candidate from the Scout's shortlist, dispatch the **Caddie** agent for deep research.

Launch the Caddie agent (`caddie.md`) to:
1. Research each candidate's underlying event
2. Gather at least 2 independent confirming signals
3. Estimate true probability
4. Produce a GimmeScore and research memo
5. Recommend PROCEED, PASS, or NEEDS MORE RESEARCH

Log Caddie completion:
```bash
python -m gimmes log-activity --cycle $GIMMES_CYCLE --agent caddie --phase complete --message "Caddie reviewed N candidates, M approved"
```

**If no candidates receive PROCEED**, skip to Step 6.

### Step 5: Closer

For each approved candidate (GimmeScore >= 75, recommendation = PROCEED), dispatch the **Closer** agent.

Launch the Closer agent (`closer.md`) to:
1. Run `python -m gimmes validate TICKER --prob P` for each candidate
2. If validation passes, run `python -m gimmes size TICKER --prob P`
3. Place the order: `python -m gimmes order TICKER --prob P --yes`
4. Log the trade: `python -m gimmes log-trade TICKER --action open --prob P --score S --rationale "..."`

Log Closer completion:
```bash
python -m gimmes log-activity --cycle $GIMMES_CYCLE --agent closer --phase complete --message "Closer executed N trades"
```

**Safety**: The Closer must pass all 7 validation checks before any trade. Never override risk limits.

### Step 6: Scorecard

Dispatch the **Scorecard** agent for end-of-cycle reporting.

Launch the Scorecard agent (`scorecard.md`) to:
1. Generate P&L summary
2. Report performance metrics
3. Assess strategy health

Log cycle completion:
```bash
python -m gimmes log-activity --cycle $GIMMES_CYCLE --agent orchestrator --phase complete --message "Cycle $GIMMES_CYCLE complete"
```

## Parallelism

- **Steps 2 + 3 can run in parallel** — Monitor reviews existing positions while Scout scans for new candidates. These are independent operations.
- **Steps 4, 5, 6 must be sequential** — Caddie needs Scout output, Closer needs Caddie output, Scorecard reports on the full cycle.

## Recovery

Each cycle reads database state fresh at Step 1. If the previous cycle crashed mid-execution:
- Partially filled orders are visible in `positions`
- The risk check will account for current exposure
- The Scout won't duplicate positions (validator catches duplicates)

No special recovery logic needed — the state machine is the database.

## Rules

- Never ask the user questions — operate autonomously
- Never modify source code
- All market interaction through CLI commands only
- Respect all risk limits unconditionally
- Log every decision (trades, skips, closes) to the database
