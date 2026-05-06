<p align="center">
  <img src="https://raw.githubusercontent.com/allan-mobley-jr/gimmes/main/assets/gimmes-social-preview.svg" alt="GIMMES — We only play the gimmes" width="1280" />
</p>

# GIMMES ⛳

> *We only play the gimmes.*

An autonomous Claude Code agent team that trades Kalshi prediction markets by finding **gimmes** — mispriced contracts where the math is on your side. Supports dual-side trading: BUY NO on economic data threshold variance plays AND BUY YES on high-conviction equity index contracts — simultaneously, with independent parameters per side. Named after the golf term for a putt so close it's automatically conceded.

---

## What it does

GIMMES hunts for mispriced contracts on both sides of the market:

- **BUY NO** (default) — targets threshold variance plays on economic data. "Will X be above Y?" contracts where the market overprices YES near consensus. When the data says 2.0% but the market prices "above 2.5%" at 60¢ YES, the NO side at 40¢ is the gimme.
- **BUY YES** — targets high-conviction equity index contracts (S&P 500, Nasdaq-100) where the market underprices directional moves at ≥70¢.
- **BOTH** — runs both strategies simultaneously with independent price ranges, thresholds, and series per side.

**The core thesis:** Economic data has natural variance around consensus. Markets systematically overprice continuation at nearby thresholds, creating a persistent edge on the NO side. Equity index contracts at high prices offer a complementary YES-side edge. GIMMES finds these mispricings, sizes them, watches them, and decides when to close or add.

---

## Requirements

- **Python 3.11+**
- **Git**
- **Claude Code** — required for autonomous trading mode (Claude Max recommended, Claude Pro also works)
- **Kalshi account** with API access

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
1. Generate `~/.gimmes/.env` and initialize the config database
2. Walk you through creating a Kalshi API key (go to Account Settings → API Keys)
3. Find your downloaded private key, validate it, and install it securely
4. Verify your credentials work

After setup, confirm everything is connected:

```bash
gimmes mode
```

You should see "DRIVING RANGE — PAPER TRADING" with your paper balance.

### Take a tour

New to GIMMES? The Starter will show you around:

```bash
gimmes tour_guide
```

The Starter is an interactive guide who walks you through what GIMMES does, how the agent team works, and how to get the most out of the system. Take the guided tour or just ask questions — your call.

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

### Uninstall

```bash
gimmes uninstall              # Remove everything (~/.gimmes)
gimmes uninstall --keep-data  # Remove code but keep config, database, keys, logs
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

`GIMMES_MODE` in `.env` is the single source of truth. Use `gimmes switch` to toggle, or `gimmes driving_range`/`gimmes championship` to switch and start in one step. Both modes use the same prod API credentials for market data. The only difference is where portfolio operations are routed — the `PaperBroker` in driving range vs. Kalshi's API in championship. CLI commands and agents work identically in both modes.

**Always start in Driving Range.** Championship mode requires explicit confirmation.

---

## Playing with a Caddie

Hand the bag to the agent team and let them play the round.

```bash
gimmes driving_range    # Paper money
gimmes championship     # Real money (requires confirmation)
```

The **Caddie Master** dispatches agents each cycle. During trade windows (around data releases), the full pipeline runs: Monitor → Scout → Caddie → Closer → Scorecard. Outside trade windows, only Monitor runs — the system sleeps until the next window. See [How it works](#how-it-works) for the full cycle breakdown.

The [Clubhouse](#the-clubhouse) dashboard auto-launches at `http://127.0.0.1:1919` (or the next available port) — check the printed URL and open your browser to watch live.

Press **Ctrl+C** to stop. Run the command again to resume — the loop reads database state, so it picks up where it left off. Use `--cycles N` (alias `--max-cycles N`, default 400 ≈ one trading day) to run a bounded number of cycles, `--pause N` to adjust seconds between full cycles, `--monitor-interval N` for seconds between monitor-only checks. Pass `--cycles 0` for unbounded runs (a startup warning will print).

---

## Walking the Course Solo

Skip the agents and play each shot yourself. Every CLI command respects your configured strategy parameters.

```bash
# 1. Spot candidates on the course
gimmes scan

# 2. Quick-read on a specific market
gimmes score TICKER

# 3. Do your own research
#    This is the one shot the CLI can't take for you. In autonomous mode,
#    the Caddie handles deep research — news, sentiment, cross-platform
#    pricing, base rates. Walking solo, that research is yours.

# 4. Pre-shot checklist
gimmes validate TICKER --prob 0.92

# 5. Calculate your wager
gimmes size TICKER --prob 0.92

# 6. Take the shot
gimmes order TICKER --prob 0.92

# 7. Check the leaderboard
gimmes positions

# 8. Review your scorecard
gimmes report
```

Both modes share the same SQLite database. Trades you place manually appear in the [Clubhouse](#the-clubhouse) and in `gimmes report`. If you later start the autonomous loop, the Monitor will pick up your manually-placed positions and manage them alongside its own.

See [CLI commands](#cli-commands) for the full command reference including options and flags.

---

## Agent team

The autonomous loop is orchestrated by the **Caddie Master**, which dispatches this agent team each cycle:

| Agent | Role | Responsibilities |
|---|---|---|
| **The Caddie Master** | Orchestration | Dispatches agents, manages cycle flow, handles errors |
| **The Scout** | Opportunity discovery | Scans Kalshi for markets in the configured price range, scores each for gimme potential, groups candidates by event for threshold ladder evaluation |
| **The Caddie** | Research & analysis | Deep-dives shortlisted markets — news, social signals, historical patterns. Groups multi-threshold candidates by event for efficient research |
| **The Closer** | Trade execution | Sizes positions (Kelly or EV mode), places orders, executes closes |
| **The Monitor** | Position watching | Monitors open contracts, flags price moves, stop-loss breaches, and SIZE UP opportunities |
| **The Scorecard** | Reporting | Tracks P&L, win rate, edge accuracy, and strategy performance |
| **The Groundskeeper** | Error escalation | Reviews error logs, escalates critical/recurring errors to GitHub issues |
| **The Pro** | Strategy tuning | Analyzes performance data, recommends parameter changes with evidence |
| **The Starter** | Product tour guide | Welcomes new users, explains features, answers questions — on-demand via `gimmes tour_guide` |
| **The Caddie Shop** | Configuration advisor | Conversational config tuning — explains trade-offs, suggests parameter packages, sets values — on-demand via `gimmes caddie_shop` |

Agents communicate through the orchestrator's context — Scout's shortlist flows to Caddie, Caddie's approved candidates flow to Closer. Agents don't call the Kalshi API directly; they use CLI commands exclusively.

---

## How it works

Each `start`, `driving_range`, or `championship` invocation runs a **calendar-aware trading loop** that schedules cycles around data release windows instead of cycling continuously.

### Trade windows

The loop checks a built-in calendar of 9 scheduled trade windows plus dynamic position settlement windows:

| Window | Schedule | Frequency |
|--------|----------|-----------|
| Equity index close (S&P 500, Nasdaq) | 2:00–4:00 PM ET | Every weekday |
| Treasury notes | Tue 11 PM – Wed 1 PM ET | Weekly |
| Jobless claims | Wed 6:30 PM – Thu 8:30 AM ET | Weekly |
| ADP private payrolls | Tue before NFP 6:15 PM – Wed 8:15 AM ET | Monthly |
| ISM Manufacturing PMI | Night before 1st biz day 8 PM – 10 AM ET | Monthly |
| Non-Farm Payrolls | Thu 6:30 PM – 1st Fri 8:30 AM ET | Monthly |
| CPI | Night before release 6:30 PM – 8:30 AM ET | Monthly |
| Core PCE | Night before last Fri 6:30 PM – 8:30 AM ET | Monthly |
| GDP Advance Estimate | Night before release 6:30 PM – 8:30 AM ET | Quarterly |
| Position settlement | 18 hours before close_time | Per-position |

CPI and GDP windows use actual BLS/BEA release dates (lookup table for 2025-2026, fallback heuristic for future years). Position settlement windows are created automatically when a held position's close_time doesn't fall within any scheduled window.

### Cycle types

- **Full cycle** (inside a trade window or position settlement window): runs the complete pipeline — Monitor → Scout → Caddie → Closer → Scorecard. Pauses `--pause` seconds (default 60) between cycles.
- **Monitor-only cycle** (outside all windows): runs only Monitor and Groundskeeper, then sleeps until the next trade window or `--monitor-interval` (default 1 hour). Sleep is recalculated after each cycle to catch windows that open mid-cycle.

This reduces token usage ~80-90% compared to continuous cycling while being *more* responsive during data releases. Code staleness detection warns when the installed code has changed or the remote has newer commits — restart to pick up fixes.

### Full cycle pipeline

1. **State check** — reads positions, daily P&L, and risk limits from SQLite
2. **Monitor** — reviews existing positions, recommends hold/close/size-up
3. **Scout** — scans Kalshi markets, filters by price/volume/time, produces a shortlist
4. **Caddie** — deep-researches each candidate with web search, estimates true probability
5. **Closer** — validates, sizes (quarter-Kelly), and executes approved trades
6. **Scorecard** — reports P&L, win rate, and strategy health
7. **Groundskeeper** — reviews error logs, escalates critical or recurring errors to GitHub issues
8. **The Pro** (every 10th cycle) — analyzes performance, recommends parameter changes with data

The loop can be stopped with Ctrl+C. If a cycle crashes, the loop re-invokes and the orchestrator picks up where it left off by reading database state. Rate limit errors are detected automatically and pause the loop until reset.

```bash
gimmes start                              # Use current mode from .env
gimmes driving_range                      # Switch to driving_range + start
gimmes driving_range --cycles 5           # Run exactly 5 cycles
gimmes driving_range --pause 60           # 60s between cycles (in trade window)
gimmes driving_range --monitor-interval 1800  # Check positions every 30 min outside windows
```

---

## The Clubhouse

In golf, the clubhouse is where players check the leaderboard, review scores, and watch the action. The GIMMES Clubhouse is a local web dashboard that gives you a live view of everything the system is doing.

```bash
gimmes clubhouse    # Launch standalone at http://127.0.0.1:1919
```

The dashboard also **auto-starts** whenever you run `gimmes start`, `gimmes driving_range`, or `gimmes championship` — just open your browser to the printed URL. Disable with `--no-dashboard` if you prefer headless operation.

### What you see

| Panel | What it shows |
|---|---|
| **KPI Cards** | Balance, total equity, daily P&L, open position count |
| **Positions Table** | Open positions with mark-to-market, unrealized P&L, and settlement date |
| **Risk Gauges** | Daily loss vs. limit, position count vs. max, largest position vs. cap |
| **Equity Curve** | Historical portfolio value chart (Chart.js) |
| **Performance Metrics** | Win rate, Sharpe ratio, max drawdown, total return |
| **Agent Activity Feed** | Live cycle events — which agent is running, what it found |
| **Error Log** | Recent errors with severity color-coding (hidden when no errors) |
| **Strategy Recommendations** | Pending parameter change recommendations from The Pro (hidden when none) |
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

The dashboard determines if an autonomous loop is active by checking the `sessions` table for a live session (PID liveness check). When active, the header shows a green connection indicator with cycle count. Mode always comes from `.env` via `load_config()`. When idle, it shows historical data with a "No active loop" message in the activity feed.

---

## CLI commands

Run `gimmes help` for the full grouped reference. See [Walking the Course Solo](#walking-the-course-solo) for a workflow walkthrough.

### Setup & Config
```bash
gimmes init              # First-time setup (API credentials, config)
gimmes config            # Interactive configuration wizard
gimmes config set K V    # Set a single config value directly
gimmes config get [K]    # Show config value(s)
gimmes tour_guide        # Interactive product tour (The Starter)
gimmes caddie_shop       # Conversational config advisor (The Caddie Shop)
gimmes update            # Pull latest code and reinstall
gimmes uninstall         # Remove gimmes (--keep-data to preserve config/db)
gimmes version           # Show version and check for updates
```

For CI/Docker/automation, use `gimmes init --headless`. Requires env vars: `KALSHI_PROD_API_KEY`, `KALSHI_PROD_PRIVATE_KEY_PATH` (path to an unencrypted PEM), and `KALSHI_PRIVATE_KEY_PASSWORD`.

### Mode & Status
```bash
gimmes mode              # Show current mode and connection status
gimmes switch [MODE]     # Switch trading mode (omit MODE to toggle)
```

### Market Research
```bash
gimmes discover CAT        # Explore series in a Kalshi category
gimmes scan                # Scan markets for gimme candidates
gimmes score TICKER        # Score a specific market
gimmes market-info TICKER  # Detailed market info + orderbook
```

### Trading
```bash
gimmes size TICKER -p P      # Calculate position size
gimmes validate TICKER -p P  # Pre-trade validation
gimmes order TICKER -p P     # Place an order
gimmes cancel ORDER_ID       # Cancel a resting order
gimmes trades                # List trade records (--ticker, --action)
gimmes candidates            # List scored candidates (--ticker)
gimmes reset-cooldown        # Clear cached candidate scores (--force)
```

### Portfolio
```bash
gimmes positions                # List open positions
gimmes reconcile                # Sync positions with broker/API
gimmes risk-check               # Check risk limits and daily P&L
gimmes report                   # Performance scorecard
gimmes position-context TICKER  # Full thesis + note history for a position
gimmes position-notes TICKER    # Position journal entries
```

### Diagnostics
```bash
gimmes errors            # View error logs (--severity, --category, --unresolved)
```

### Strategy
```bash
gimmes backtest --from D --to D  # Backtest strategy on historical markets (--balance, --edge, --json)
                                 # Supports dual-side mode — runs per-side passes when strategy.side=both
gimmes lesson                    # Run strategy analysis and recommendations
gimmes recommendations           # View past strategy recommendations
gimmes tune                      # Apply pending strategy recommendations
```

### Dashboard
```bash
gimmes clubhouse         # Launch web dashboard (http://127.0.0.1:1919)
```

### Autonomous Trading
```bash
gimmes start             # Autonomous loop using current mode from .env
gimmes driving_range     # Autonomous loop -- paper trading (auto-starts dashboard)
gimmes championship      # Autonomous loop -- real money (auto-starts dashboard)
```

All three accept `--cycles N` (alias `--max-cycles N`; default 400 ≈ one trading day, pass `0` for unbounded with a startup warning), `--pause N` (seconds between full cycles, default 60), `--monitor-interval N` (seconds between monitor-only cycles outside trade windows, default 3600), and `--no-dashboard`.

### Daily Claude API budget cap

The autonomous loop enforces a UTC-day budget cap on Claude API usage to keep
runs from blowing through Anthropic's rate plan. Two cap types apply
simultaneously:

- `--max-sessions-per-day N` (default 80) — number of `claude` subprocesses spawned per UTC day.
- `--max-daily-cost-usd X` (default $25) — estimated dollar cost per UTC day, computed per cycle from the `usage` block in each subprocess's stream-json output multiplied by the model's per-million-token rate.

State is persisted to `${GIMMES_HOME}/budget.json` so it survives loop
restarts. When either cap is hit, the loop prints a red `BUDGET CAP HIT`
banner, sends an iMessage push when `GIMMES_NOTIFY_PHONE` is set in the
environment, writes a `cycle-NNN-block.json` entry to `${GIMMES_HOME}/logs/`,
and sleeps until the next UTC midnight, at which point the day's counters
roll over and trading resumes automatically. Pass `0` for either flag to
disable that cap. Inspect today's totals and remaining headroom with
`gimmes budget` (`gimmes budget --json` for machine-readable output, which
also includes the active caps and seconds until reset).

---

## Gimme criteria

A market qualifies as a gimme when it clears all of the following:

- **Category:** Must be in a backtested gimme category from the default watchlist (`KXINX`, `KXNASDAQ100`, `KXPAYROLLS`, `KXCPIYOY`, `KXCPICORE`, `KXCPICOREYOY`, `KXGDP`, `KXADP`). Additional series such as `KXISMPMI` can be enabled via `scanner.series`
- **Buy price:** NO side defaults to 40¢–75¢ (`strategy.min_market_price` / `strategy.max_market_price`)
- **Liquidity:** Sufficient volume and open interest to absorb the position
- **Time horizon:** Contract resolves within 0.5–90 days (configurable)
- **Concentration limits:** Max 15% per event, 30% per series — prevents over-exposure
- **Sanity check:** No exceptional circumstances that invalidate the structural edge
- **Settlement clarity:** Unambiguous resolution criteria — no subjective carve-outs

---

## Strategy

### Phase 1 — Scan

The Scout polls the Kalshi API for active markets in the configured series watchlist. In dual-side mode (`strategy.side = "both"`), the scanner runs two passes — one per side with independent price ranges, thresholds, and series (NO uses the main scanner watchlist, YES defaults to 9 equity index series). Each candidate is tagged with its scan side. The Scout filters by price range, volume, open interest, and time to resolution, then scores each on:

- Volume and liquidity depth
- Time to resolution
- Spread tightness and price position
- Market staleness (skips truly dead markets with no volume, OI, or price changes)

### Phase 2 — Research

For candidates in backtested gimme categories, the Caddie runs a **sanity check** — a quick verification that no exceptional circumstance invalidates the structural edge (government shutdown, data revision, one-time event). This replaces the previous deep research mode for proven categories, saving ~80% of tokens per candidate.

For non-gimme or new categories, the Caddie runs a full structured research pass: news, sentiment, domain-specific data, cross-platform pricing, and historical base rates.

**Threshold ladder research:** When multiple candidates share the same event (e.g., KXCPI-26APR at T0.3, T0.5, T0.8), the Caddie researches the underlying event once and derives per-threshold probabilities — one research pass covers all thresholds.

Candidates are eligible for re-research after 48 hours regardless of prior score, ensuring stale rejections from a different macro environment don't block fresh evaluation.

The Caddie produces a **Gimme Score** (0–100) and a structured memo summarizing the edge thesis.

### Phase 3 — Execute

The Closer reviews any market with a Gimme Score above threshold and:

1. Calculates true probability estimate
2. Applies fractional Kelly (0.25×) for position sizing
3. Places a maker limit order (preferred — 75% lower fees)
4. Logs the trade with full rationale

### Phase 4 — Monitor

The Monitor watches all open positions and triggers a review when:

- Market price moves significantly from entry (configurable price trigger, default 10pp)
- New material information emerges that changes the thesis
- Time to resolution drops below 24 hours and position isn't profitable
- Position approaches the daily loss limit
- Per-position stop-loss threshold is breached (default 15% of cost basis)

The Caddie Master reviews each flag and decides: **Hold**, **Close** (dispatches Closer to sell), or **Size up** (if edge has improved on an intact thesis). A SIZE UP bias rule defaults to adding when the thesis is intact and bankroll is underutilized.

---

## Position sizing

Two sizing modes (`sizing.mode` config):

**Kelly mode** (default) — fractional Kelly criterion with fees:
```
effective_cost   = price + fee
effective_odds_b = (1 - price - fee) / (price + fee)
full_kelly       = (b × p_true - q) / b
position_size    = 0.25 × full_kelly × bankroll
```

**EV mode** — expected-value sizing for variance plays with moderate probability (30-60%):
```
edge_dollars     = (true_probability × $1.00) - price - fees
ratio            = edge_dollars / cost_per_contract
position_size    = 0.25 × ratio × bankroll
```

**Base rate floor:** For gimme categories with backtested win rates, the probability input is floored at the category base rate (80-90%) to prevent undersizing from conservative LLM estimates. If the Caddie estimates higher than the base rate, the higher estimate is used. This ensures positions are sized to the known structural edge, not the per-trade LLM noise.

Hard limits applied regardless of sizing mode:
- Max 10% of bankroll per position (configurable)
- Max 50 open positions simultaneously (configurable)
- 15% daily loss limit → full stop
- 15% per-position stop-loss trigger
- 80% per-position take-profit trigger
- 15% max exposure per event, 30% max per series
- No positions in markets with ambiguous settlement language

In dual-side mode, these limits apply independently per side — each side's positions are sized and validated using the side-specific config from `effective_config_for_side()`. Concentration limits aggregate across both sides.

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

Strategy parameters are stored in the SQLite database (`~/.gimmes/gimmes.db`) and managed three ways:

```bash
gimmes config                         # Walk through all settings interactively
gimmes config --section risk          # Jump to a specific section
gimmes config set risk.bankroll_paper 2000  # Set a single value directly
gimmes config get                     # Show all current values
gimmes config get strategy.gimme_threshold  # Show a single value
gimmes caddie_shop                    # Conversational config advisor (Claude Code)
gimmes tune                           # Apply pending strategy recommendations
```

**Direct set** validates the value against the field's constraints (type, min/max, choices) and shows old → new. **The Caddie Shop** is a conversational agent that explains how parameters interact, suggests coherent multi-parameter adjustments based on your goals ("make the system more aggressive"), and sets values on your behalf — all through the CLI.

The Pydantic config models in `config.py` are the single source of truth — each field's type, default, constraints, and wizard metadata are defined in one place. Adding a new config parameter automatically makes it appear in the wizard, `config set`, `config get`, and The Caddie Shop.

### Dual-side configuration

To enable dual-side trading:

```bash
gimmes config set strategy.side both

# YES-side overrides (equity index contracts)
gimmes config set strategy.yes_overrides.min_market_price 0.70
gimmes config set strategy.yes_overrides.max_market_price 0.85
gimmes config set strategy.yes_overrides.min_true_probability 0.85
gimmes config set strategy.yes_overrides.gimme_threshold 75

# NO-side overrides (economic data)
gimmes config set strategy.no_overrides.min_market_price 0.40
gimmes config set strategy.no_overrides.max_market_price 0.75
gimmes config set strategy.no_overrides.min_true_probability 0.50
gimmes config set strategy.no_overrides.gimme_threshold 65

# Per-side series (optional — defaults are set automatically)
gimmes config set scanner.yes_series "KXINX,KXINXW,KXNASDAQ100,KXNASDAQ100W"
```

When `side = "yes"` or `"no"`, the flat strategy fields are used directly. Per-side overrides only apply when `side = "both"`.

---

## Project structure

```
~/.gimmes/                       # User data (created by gimmes init)
├── bin/gimmes                   # Global CLI command (symlink)
├── .env                         # API credentials
├── keys/kalshi_private.pem      # RSA private key
├── gimmes.db                    # SQLite database (config + trade data)
└── repo/                        # Cloned source code
    ├── src/gimmes/
    │   ├── cli.py               # Typer CLI entry point + trading_context routing
    │   ├── config.py            # Two-layer config (env vars + SQLite)
    │   ├── clubhouse/           # Web dashboard (FastAPI + SSE)
    │   ├── templates/           # Jinja2 HTML template (Tailwind + Chart.js)
    │   ├── kalshi/              # HTTP client, auth, market/order/portfolio endpoints
    │   ├── paper/               # Paper trading engine (fill simulator, broker)
    │   ├── backtest/            # Backtest engine, report formatter
    │   ├── strategy/            # Scanner, scorer, Kelly/EV sizing, fee calculator, trade window calendar
    │   ├── risk/                # Limits, validator, settlement risk scanner
    │   ├── store/               # SQLite persistence (trades, positions, snapshots)
    │   ├── models/              # Pydantic models (market, order, portfolio, trade)
    │   └── reporting/           # P&L, metrics, Rich console formatting
    ├── bin/gimmes.sh            # CLI wrapper (symlink target)
    ├── install.sh               # One-liner installer
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
