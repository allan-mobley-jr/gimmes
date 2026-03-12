---
name: tee-time
description: Full trading pipeline — Scout → Caddie → Closer in one cycle
user_invocable: true
---

# /tee-time — Full Pipeline

Run one complete trading cycle: Scout → Caddie → Closer.

## Instructions

Execute the full GIMMES pipeline:

1. **Scout** — Scan markets and produce a shortlist
   - Run `python -m gimmes scan`
   - Score top candidates
   - Select the best 3-5 candidates

2. **Caddie** — Research each candidate
   - Deep research with web search
   - Estimate true probabilities
   - Score each candidate (GimmeScore)
   - Filter to PROCEED recommendations only

3. **Closer** — Execute approved trades
   - Validate each trade
   - Size positions
   - Place orders
   - Log everything

Run each phase sequentially. If any phase produces no candidates, stop and report.

## Safety

- Always respect risk limits
- In Championship mode, pause for user confirmation before the Closer phase
- Log the entire cycle to the database
