---
name: trade
description: Execute approved trades using the Closer agent
user_invocable: true
---

# /trade — Trade Execution

Dispatch the Closer agent to validate, size, and execute approved trades.

## Instructions

Launch the Closer agent (`closer.md`) to:
1. Take the Caddie's approved candidates (PROCEED recommendations)
2. Validate each trade (all 7 risk checks)
3. Calculate position size using Kelly criterion
4. Place maker limit orders
5. Log all decisions to the database

**Safety**: The Closer will never skip validation or exceed risk limits.
In Championship mode, it will ask for explicit user confirmation before every order.
