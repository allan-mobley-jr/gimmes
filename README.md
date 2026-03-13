<p align="center">
  <img src="https://raw.githubusercontent.com/allan-mobley-jr/gimmes/main/assets/gimmes-social-preview.svg" alt="GIMMES — We only play the gimmes" width="1280" />
</p>

# GIMMES ⛳

> *We only play the gimmes.*

An autonomous Claude Code agent team that trades Kalshi prediction markets by identifying **100 Percenters** — contracts priced well below their true probability of winning. Named after the golf term for a putt so close it's automatically conceded.

---

## What it does

GIMMES hunts for mispriced certainty. When a contract is trading at 70¢ but research, context, and converging signals say it should be 95¢+, that's a gimme. The system finds them, sizes them, watches them, and decides when to close or let ride.

**The core thesis:** Prediction markets systematically underprice near-certain outcomes. Human bettors anchor to headline odds without doing the underlying work. GIMMES does the work.

---

## Quick start

### Install

One command:

```bash
curl -fsSL https://raw.githubusercontent.com/allan-mobley-jr/gimmes/main/install.sh | bash
```

This clones the repo to `~/.gimmes/repo`, sets up a Python virtual environment, and creates a global `gimmes` command. Restart your terminal after install.

### Setup

Run the interactive setup wizard — it will create your config files, guide you through Kalshi API key creation, and verify your connection:

```bash
gimmes init
```

The wizard will:
1. Generate `~/.gimmes/.env` and `~/.gimmes/config/gimmes.toml` from example files
2. Walk you through creating a Kalshi API key (go to Account Settings → API Keys)
3. Find your downloaded private key, validate it, and install it securely
4. Verify your credentials work

After setup, confirm everything is connected:

```bash
gimmes mode
```

You should see "DRIVING RANGE — PAPER TRADING" with your paper balance.

### Start trading

Launch the autonomous trading loop in paper mode:

```bash
gimmes driving_range
```

That's it. The system will scan markets, research candidates, execute trades, and monitor positions — all with virtual money. A live dashboard auto-starts at `http://127.0.0.1:1919` — open it in your browser to watch the action.

Check on performance anytime:

```bash
gimmes report
```

Or launch the dashboard standalone (without the trading loop):

```bash
gimmes clubhouse
```

When you're ready for real money (after verifying your strategy on the driving range):

```bash
gimmes championship
```

Championship mode requires explicit confirmation at startup since it trades with real money autonomously.

### Update

```bash
gimmes update
```

### Help

```bash
gimmes help
```

---

## Two modes

| Mode | Command | Market data | Orders | Balance |
|---|---|---|---|---|
| **Driving Range** (default) | `gimmes driving_range` | Real (prod API) | Simulated locally | Virtual $10,000 |
| **Championship** | `gimmes championship` | Real (prod API) | Real (prod API) | Real money |

Both modes use the same prod API credentials for market data. The only difference is where portfolio operations are routed — the `PaperBroker` in driving range vs. Kalshi's API in championship. CLI commands and agents work identically in both modes.

**Always start in Driving Range.** Championship mode requires explicit confirmation at startup.

---

## Agent team

The autonomous loop is orchestrated by the **caddy-shack** skill, which dispatches five specialized Claude Code agents each cycle:

| Agent | Role | Tools | Responsibilities |
|---|---|---|---|
| **The Scout** | Opportunity discovery | Bash, Read, Glob, Grep | Scans Kalshi for markets above 55¢, scores each for gimme potential |
| **The Caddie** | Research & analysis | + WebSearch, WebFetch | Deep-dives shortlisted markets — news, social signals, historical patterns |
| **The Closer** | Trade execution | Bash, Read, Glob, Grep | Sizes positions using fractional Kelly, places maker limit orders |
| **The Monitor** | Position watching | + WebSearch, WebFetch | Monitors open contracts, flags early-close opportunities |
| **The Scorecard** | Reporting | Bash, Read, Glob, Grep | Tracks P&L, win rate, edge accuracy, and strategy performance |
| **The Groundskeeper** | Error escalation | Bash, Read, Glob, Grep | Reviews error logs, escalates critical/recurring errors to GitHub issues |
| **The Pro** | Strategy tuning | + WebSearch, WebFetch | Analyzes performance data, recommends parameter changes with evidence |

Agents communicate through the orchestrator's context — Scout's shortlist flows to Caddie, Caddie's approved candidates flow to Closer. Agents don't call the Kalshi API directly; they use CLI commands exclusively.

---

## How it works

Each `driving_range` or `championship` invocation runs a continuous loop of trading cycles. Each cycle:

1. **State check** — reads positions, daily P&L, and risk limits from SQLite
2. **Monitor** — reviews existing positions, recommends hold/close/size-up
3. **Scout** — scans Kalshi markets, filters by price/volume/time, produces a shortlist
4. **Caddie** — deep-researches each candidate with web search, estimates true probability
5. **Closer** — validates, sizes (quarter-Kelly), and executes approved trades
6. **Scorecard** — reports P&L, win rate, and strategy health
7. **Groundskeeper** — reviews error logs, escalates critical or recurring errors to GitHub issues
8. **The Pro** (every 10th cycle) — analyzes performance, recommends parameter changes with data

The loop pauses between cycles (default 30s, configurable with `--pause`) and can be stopped with Ctrl+C. If a cycle crashes, the loop re-invokes and the orchestrator picks up where it left off by reading database state.

```bash
gimmes driving_range                # Unlimited cycles, 30s pause
gimmes driving_range --cycles 5     # Run exactly 5 cycles
gimmes driving_range --pause 60     # 60s between cycles
```

---

## The Clubhouse

In golf, the clubhouse is where players check the leaderboard, review scores, and watch the action. The GIMMES Clubhouse is a local web dashboard that gives you a live view of everything the system is doing.

```bash
gimmes clubhouse    # Launch standalone at http://127.0.0.1:1919
```

The dashboard also **auto-starts** whenever you run `gimmes driving_range` or `gimmes championship` — just open your browser to the printed URL. Disable with `--no-dashboard` if you prefer headless operation.

### What you see

| Panel | What it shows |
|---|---|
| **KPI Cards** | Balance, total equity, daily P&L, open position count |
| **Positions Table** | Open positions with mark-to-market, unrealized P&L |
| **Risk Gauges** | Daily loss vs. limit, position count vs. max, largest position vs. cap |
| **Equity Curve** | Historical portfolio value chart (Chart.js) |
| **Performance Metrics** | Win rate, Sharpe ratio, max drawdown, total return |
| **Agent Activity Feed** | Live cycle events — which agent is running, what it found |
| **Error Log** | Recent errors with severity color-coding (hidden when no errors) |
| **Recent Trades** | Trade log with action, price, score, agent |
| **Candidate Pipeline** | Scout shortlist with scores, edge, and Caddie research memos |
| **Configuration** | Current strategy settings (collapsible, read-only) |

### How it works

- **FastAPI + Uvicorn** serves a single HTML page with Tailwind CSS and Chart.js (CDN, no build toolchain)
- **SSE (Server-Sent Events)** pushes updates to the browser every 2 seconds when data changes
- **Read-only** — the dashboard opens SQLite in read-only mode (`?mode=ro`) and never writes to the database
- **WAL mode** enables concurrent reads without blocking the autonomous loop's writes
- **Daemon thread** — when auto-started, the server runs in a background thread that dies when the main process exits
- **Port 1919** by default; on conflict, probes port+1 through port+10

### Loop activity detection

The dashboard determines if an autonomous loop is active by checking the `activity_log` table. If the latest entry is recent (within `pause_seconds + 60s`), the loop is considered active and the header shows a green connection indicator. When idle, it shows historical data with a "No active loop" message in the activity feed.

---

## CLI commands

### Autonomous trading
```bash
gimmes driving_range     # Start autonomous loop (paper trading)
gimmes championship      # Start autonomous loop (real money)
```

### Setup & configuration
```bash
gimmes init              # First-time setup wizard
gimmes config            # Interactive config wizard
gimmes mode              # Show mode + connection status
```

### Manual trading
```bash
gimmes scan              # Scan markets for gimme candidates
gimmes score TICKER      # Score a specific market
gimmes size TICKER -p P  # Calculate position size
gimmes validate TICKER   # Pre-trade validation
gimmes order TICKER      # Place an order (paper or real)
gimmes cancel ORDER_ID   # Cancel a resting order
```

### Dashboard
```bash
gimmes clubhouse         # Launch Clubhouse dashboard (see above)
```

### Monitoring & reporting
```bash
gimmes positions         # List open positions (with mark-to-market)
gimmes risk-check        # Check risk limits and daily P&L
gimmes report            # Performance scorecard
gimmes market-info TICKER # Detailed market info
gimmes log-trade TICKER  # Log a trade decision
gimmes discover CATEGORY # Discover series tickers in a category
gimmes errors            # View error logs (--severity, --category, --unresolved, --summary)
gimmes log-error         # Log a structured error (used by agents/system)
gimmes resolve-error ID  # Mark an error as resolved (--issue-url to link GitHub issue)
gimmes lesson            # Run strategy analysis (--analysis TYPE, --dry-run)
gimmes recommendations   # View past strategy recommendations (--status, --parameter)
```

---

## Gimme criteria

A market qualifies as a gimme when it clears all of the following:

- **Market price:** 55¢–85¢ (strong favorite, not yet near certainty)
- **Model probability:** ≥90% (genuine edge of ≥15 percentage points)
- **Confidence sources:** ≥2 independent confirming signals
- **Liquidity:** Sufficient depth to absorb the position without moving the market
- **Time horizon:** Contract resolves within a meaningful window (not years out)
- **Settlement clarity:** Unambiguous resolution criteria — no subjective carve-outs

---

## Strategy

### Phase 1 — Scan

The Scout polls the Kalshi API for all active markets, filters to the 55¢–85¢ price range, and scores each on:

- Volume and liquidity depth
- Time to resolution
- Category (economics, politics, sports, weather, crypto)
- Historical accuracy of similar markets

### Phase 2 — Research

For every shortlisted market, the Caddie runs a structured research pass:

- Current news and recent developments
- Social sentiment (X/Twitter, Reddit, relevant forums)
- Domain-specific data (polling averages, economic nowcasts, injury reports, weather forecasts)
- Cross-platform check (Polymarket, ForecastEx) for divergent pricing signals
- Historical base rates for comparable events

The Caddie produces a **Gimme Score** (0–100) and a structured memo summarizing the edge thesis.

### Phase 3 — Execute

The Closer reviews any market with a Gimme Score above threshold and:

1. Calculates true probability estimate
2. Applies fractional Kelly (0.25×) for position sizing
3. Places a maker limit order (preferred — 75% lower fees)
4. Logs the trade with full rationale

### Phase 4 — Monitor

The Monitor watches all open positions and triggers a review when:

- Market price moves significantly toward 100¢ (early close candidate)
- New material information emerges that changes the thesis
- Time to resolution drops below a configurable threshold
- Position approaches the daily loss limit

The Monitor recommends: **Hold**, **Close now**, or **Size up** (if additional edge confirmed).

---

## Position sizing

Uses fractional Kelly criterion with fees baked in:

```
effective_cost   = price + fee
effective_odds_b = (1 - price - fee) / (price + fee)
full_kelly       = (b × p_true - q) / b
position_size    = 0.25 × full_kelly × bankroll
```

Hard limits applied regardless of Kelly output:
- Max 5% of bankroll per position
- Max 15 open positions simultaneously
- 15% daily loss limit → full stop
- No positions in markets with ambiguous settlement language

---

## Fee awareness

Kalshi fees follow `round_up(0.07 × C × P × (1−P))` for takers; `round_up(0.0175 × C × P × (1−P))` for makers. GIMMES defaults to maker orders. At a 75¢ contract:

| Order type | Fee per contract | Break-even edge required |
|---|---|---|
| Taker | ~$0.013 | ~1.7% |
| Maker | ~$0.003 | ~0.4% |

Minimum required edge before any trade: **5 percentage points** after fees.

---

## Configuration

Strategy parameters live in `~/.gimmes/config/gimmes.toml`:

```toml
[strategy]
gimme_threshold = 75          # Minimum GimmeScore to execute (0-100)
min_market_price = 0.55       # Only scan markets above this price
max_market_price = 0.85       # Only scan markets below this price
min_true_probability = 0.90   # Model must see >=90% to qualify
min_edge_after_fees = 0.05    # 5pp minimum edge after fee math

[sizing]
kelly_fraction = 0.25         # Conservative quarter-Kelly
max_position_pct = 0.05       # Max 5% of bankroll per position

[risk]
max_open_positions = 15       # Concurrent position limit
daily_loss_limit_pct = 0.15   # Auto-stop at 15% daily drawdown

[orders]
preferred_order_type = "maker" # Limit orders; no takers by default

[paper]
starting_balance = 10000.00   # Virtual bankroll for driving range mode
```

---

## Project structure

```
~/.gimmes/                       # User data (created by gimmes init)
├── bin/gimmes                   # Global CLI command (symlink)
├── .env                         # API credentials
├── config/gimmes.toml           # Strategy parameters
├── keys/kalshi_private.pem      # RSA private key
├── gimmes.db                    # SQLite database
└── repo/                        # Cloned source code
    ├── src/gimmes/
    │   ├── cli.py               # Typer CLI entry point + trading_context routing
    │   ├── config.py            # Two-layer config (env vars + TOML)
    │   ├── clubhouse/           # Web dashboard (FastAPI + SSE)
    │   ├── templates/           # Jinja2 HTML template (Tailwind + Chart.js)
    │   ├── kalshi/              # HTTP client, auth, market/order/portfolio endpoints
    │   ├── paper/               # Paper trading engine (fill simulator, broker)
    │   ├── strategy/            # Scanner, scorer, Kelly sizing, fee calculator
    │   ├── risk/                # Limits, validator, settlement risk scanner
    │   ├── store/               # SQLite persistence (trades, positions, snapshots)
    │   ├── models/              # Pydantic models (market, order, portfolio, trade)
    │   └── reporting/           # P&L, metrics, Rich console formatting
    ├── bin/gimmes.sh            # CLI wrapper (symlink target)
    ├── install.sh               # One-liner installer
    ├── config/gimmes.example.toml
    ├── tests/
    └── pyproject.toml
```

---

## Developer setup

For contributors working directly in the repo (instead of the global install):

```bash
git clone https://github.com/allan-mobley-jr/gimmes.git
cd gimmes
uv sync
```

Set `GIMMES_HOME` to keep user data separate from the global install:

```bash
export GIMMES_HOME=./local
python -m gimmes init
```

## Running tests

```bash
uv run pytest tests/unit/                          # Unit tests (no API needed)
uv run pytest tests/integration/ -m integration    # Integration tests (needs API credentials)
uv run pytest                                      # All tests
```

---

## Tech stack

- **Runtime:** Claude Code (interactive session, Claude Max)
- **Platform:** Kalshi (CFTC-regulated DCM)
- **API:** Kalshi REST + WebSocket, RSA-PSS authentication
- **State:** SQLite (trades, positions, snapshots, error log, paper trading)
- **Language:** Python 3.11+
- **Dashboard:** FastAPI + Uvicorn + Jinja2 (Tailwind CSS + Chart.js via CDN)
- **Key dependencies:** `httpx`, `pydantic`, `typer`, `rich`, `aiosqlite`, `cryptography`, `websockets`, `fastapi`, `uvicorn`, `jinja2`
- **Dev tools:** `uv`, `pytest`, `ruff`, `mypy`

---

## Known risks

- **Settlement discretion:** Kalshi has invoked contract carve-outs (see: $54M Khamenei market, Jan 2026 NFL grading errors). Any trade must pass a "would Kalshi honor this?" sanity check.
- **Favorite-longshot bias weakening:** The documented edge (+2.6% for makers on ≥50¢ contracts) narrowed in 2025 data. Monitor continuously.
- **Thin liquidity:** Some markets cannot absorb meaningful size. Position sizing caps prevent market impact but also limit upside.
- **Platform regulatory risk:** Active class actions and state enforcement challenges. Monitor developments.
- **Model overconfidence:** LLM probability estimates are not calibrated forecasts. The Caddie's output is a signal, not a ground truth. Cross-validate with structured data sources.

---

## What this is not

- Not a high-frequency trading system
- Not a market making bot
- Not financial advice

---

*Built with Claude Code. Trades on Kalshi. Plays only the gimmes.*
