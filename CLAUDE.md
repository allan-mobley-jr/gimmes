# GIMMES — Claude Code Project Guide

## What is this?

GIMMES is an autonomous trading system for Kalshi prediction markets. It identifies "gimmes" — contracts priced well below their true probability — and executes trades via a team of Claude Code agents.

## Architecture

```
CLI (python -m gimmes)  ←→  Kalshi REST API
     ↑                          ↑
  Agents                   RSA-PSS Auth
(Scout/Caddie/Closer/Monitor/Scorecard/Groundskeeper)
     ↑
  SQLite DB (gimmes.db)
```

**Key modules:**
- `src/gimmes/config.py` — Two-layer config (env vars + TOML)
- `src/gimmes/kalshi/` — HTTP client, auth, market/order/portfolio endpoints
- `src/gimmes/paper/` — Paper trading engine (fill simulator, broker, schema)
- `src/gimmes/strategy/` — Scanner, scorer, Kelly sizing, fee calculator, advisor
- `src/gimmes/risk/` — Limits, validator, settlement risk scanner
- `src/gimmes/store/` — SQLite persistence (trades, positions, snapshots, error log, recommendations)
- `src/gimmes/reporting/` — P&L, metrics, Rich console formatting
- `src/gimmes/cli.py` — Typer CLI entry point

## Two Modes

- **Driving Range** (`GIMMES_MODE=driving_range`): Paper trading. Reads **real prod market data** but simulates order execution locally via `PaperBroker`. Virtual bankroll (default $10,000). Default.
- **Championship** (`GIMMES_MODE=championship`): Production API at `api.elections.kalshi.com`. Real money. Requires explicit confirmation.

Both modes use the same prod API credentials for market data. The only difference is where portfolio operations (orders, balance, positions) are routed — `PaperBroker` in driving range vs. Kalshi API in championship. The `trading_context()` helper in `cli.py` handles this routing transparently.

**Always default to Driving Range. Never switch to Championship without explicit user approval.**

## CLI Commands

```bash
# Autonomous trading loop
python -m gimmes driving_range     # Autonomous loop — paper trading (default)
python -m gimmes championship      # Autonomous loop — real money (requires confirmation)

# Setup & config
python -m gimmes init              # First-time setup wizard
python -m gimmes config            # Interactive config wizard (--section to jump)
python -m gimmes mode              # Show mode + connection status

# Manual trading
python -m gimmes scan              # Scan markets (Scout pipeline)
python -m gimmes score TICKER      # Score a specific market
python -m gimmes size TICKER -p P  # Calculate position size
python -m gimmes validate TICKER   # Pre-trade validation
python -m gimmes order TICKER      # Place an order
python -m gimmes cancel ORDER_ID   # Cancel an order
python -m gimmes positions         # List open positions
python -m gimmes risk-check        # Check risk limits
python -m gimmes report            # Performance scorecard
python -m gimmes market-info TICKER # Detailed market info
python -m gimmes log-trade TICKER  # Log a trade decision
python -m gimmes errors            # View error logs (--severity, --category, --unresolved, --summary)
python -m gimmes log-error         # Log a structured error (used by agents/system)
python -m gimmes resolve-error ID  # Mark an error resolved (--issue-url to link issue)
python -m gimmes lesson            # Strategy analysis and recommendations (--analysis TYPE, --dry-run)
python -m gimmes recommendations   # View past strategy recommendations (--status, --parameter)
python -m gimmes tune              # Interactively apply pending recommendations to gimmes.toml
```

## Autonomous Loop

`driving_range` and `championship` run the full trading pipeline in a loop via `claude -p "/caddy-shack"`. Each cycle: State Check → Monitor → Scout → Caddie → Closer → Scorecard → Groundskeeper → The Pro (every 10th cycle). The orchestrator reads SQLite state at the start of each cycle for crash recovery. Options: `--cycles N` (bounded runs), `--pause S` (seconds between cycles).

## Running Tests

```bash
uv run pytest tests/unit/             # Unit tests (no API needed)
uv run pytest tests/integration/ -m integration  # Integration tests (needs API credentials)
uv run pytest                          # All tests
```

## Safety Rules

1. **Never trade in Championship mode without explicit user confirmation**
2. **Never exceed risk limits**: 15% daily loss, 15 positions, 5% per position
3. **Always validate before ordering**: Run `validate` before `order`
4. **Settlement risk**: Skip markets with high settlement ambiguity
5. **Minimum edge**: 5pp after fees or no trade
6. **Maker orders only**: Use post_only=True by default
7. **Log everything**: Every trade decision gets logged to SQLite

## Conventions

- Python 3.11+, Pydantic v2 models, async/await throughout
- `uv` for package management
- Prices in dollars (0.00-1.00) internally; Kalshi API uses dollar-string format (`yes_price_dollars`, `initial_count_fp`)
- All Kalshi API interactions through the CLI — agents don't call the API directly
- Tests with pytest, formatting with ruff
