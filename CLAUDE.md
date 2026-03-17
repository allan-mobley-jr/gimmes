# GIMMES

GIMMES is an autonomous trading system that finds gimmes — mispriced contracts on Kalshi prediction markets.

## Agent Rules

The following rules apply exclusively to named agents defined in `.claude/agents/`.

- You have a specific role. Follow your agent definition exclusively — it contains everything you need.
- Never modify source code, agent definitions, or configuration to fix a problem. Log it and continue your work.
- Only interact with the trading system through the `gimmes` CLI. Never directly access the database or call the Kalshi API.
