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

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (package manager)
- A [Kalshi](https://kalshi.com) account with API access

### Installation

```bash
git clone https://github.com/allan-mobley-jr/gimmes.git
cd gimmes
uv sync
```

### Setup

Run the interactive setup wizard — it will create your config files, guide you through Kalshi API key creation, and verify your connection:

```bash
python -m gimmes init
```

The wizard will:
1. Generate `.env` and `config/gimmes.toml` from their example files
2. Walk you through creating a Kalshi API key (go to Account Settings → API Keys)
3. Find your downloaded private key, validate it, and install it securely
4. Verify your credentials work

After setup, confirm everything is connected:

```bash
python -m gimmes mode
```

You should see "DRIVING RANGE — PAPER TRADING" with your paper balance.

---

## Two modes

| Mode | Env var | Market data | Orders | Balance |
|---|---|---|---|---|
| **Driving Range** (default) | `driving_range` | Real (prod API) | Simulated locally | Virtual $10,000 |
| **Championship** | `championship` | Real (prod API) | Real (prod API) | Real money |

Both modes use the same prod API credentials for market data. The only difference is where portfolio operations are routed — the `PaperBroker` in driving range vs. Kalshi's API in championship. CLI commands and agents work identically in both modes.

**Always start in Driving Range.** Championship mode requires explicit confirmation before every order.

---

## Agent team

| Agent | Role | Responsibilities |
|---|---|---|
| **The Scout** | Opportunity discovery | Scans Kalshi for markets above 55¢, scores each for gimme potential |
| **The Caddie** | Research & analysis | Deep-dives shortlisted markets — news, social signals, historical patterns |
| **The Closer** | Trade execution | Sizes positions using fractional Kelly, places maker limit orders |
| **The Monitor** | Position watching | Monitors open contracts, flags early-close opportunities |
| **The Scorecard** | Reporting | Tracks P&L, win rate, edge accuracy, and strategy performance |

---

## CLI commands

```bash
python -m gimmes init              # First-time setup wizard
python -m gimmes config            # Interactive config wizard
python -m gimmes mode              # Show mode + connection status
python -m gimmes scan              # Scan markets for gimme candidates
python -m gimmes score TICKER      # Score a specific market
python -m gimmes size TICKER -p P  # Calculate position size
python -m gimmes validate TICKER   # Pre-trade validation
python -m gimmes order TICKER      # Place an order (paper or real)
python -m gimmes cancel ORDER_ID   # Cancel a resting order
python -m gimmes positions         # List open positions (with mark-to-market)
python -m gimmes risk-check        # Check risk limits and daily P&L
python -m gimmes report            # Performance scorecard
python -m gimmes market-info TICKER # Detailed market info
python -m gimmes log-trade TICKER  # Log a trade decision
python -m gimmes discover CATEGORY # Discover series tickers in a category
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

Strategy parameters live in `config/gimmes.toml`:

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
gimmes/
├── src/gimmes/
│   ├── cli.py              # Typer CLI entry point + trading_context routing
│   ├── config.py           # Two-layer config (env vars + TOML)
│   ├── kalshi/
│   │   ├── client.py       # Authenticated HTTP client (RSA-PSS)
│   │   ├── auth.py         # RSA-PSS signature generation
│   │   ├── markets.py      # Market discovery and data endpoints
│   │   ├── orders.py       # Order placement and management
│   │   ├── portfolio.py    # Balance, positions, settlements
│   │   └── websocket.py    # WebSocket client for real-time data
│   ├── paper/
│   │   ├── broker.py       # PaperBroker — local order simulation
│   │   ├── fill_simulator.py # Pure fill logic against orderbook
│   │   └── schema.py       # SQLite DDL for paper trading tables
│   ├── strategy/
│   │   ├── scanner.py      # Market filtering pipeline
│   │   ├── scorer.py       # Gimme scoring logic
│   │   ├── kelly.py        # Fractional Kelly sizing
│   │   └── fees.py         # Kalshi fee calculator
│   ├── risk/
│   │   ├── limits.py       # Daily loss, position count checks
│   │   ├── validator.py    # Pre-trade validation
│   │   └── settlement.py   # Settlement risk scanner
│   ├── store/
│   │   ├── database.py     # Async SQLite wrapper + schema
│   │   └── queries.py      # Named queries (trades, positions, snapshots)
│   ├── models/             # Pydantic models (market, order, portfolio, trade)
│   └── reporting/
│       ├── formatter.py    # Rich console output
│       ├── pnl.py          # P&L calculation
│       └── metrics.py      # Performance metrics
├── config/
│   ├── gimmes.toml         # Strategy parameters
│   └── gimmes.example.toml # Example config
├── tests/
│   ├── unit/               # Unit tests (no API needed)
│   └── integration/        # Integration tests (needs API credentials)
└── pyproject.toml
```

---

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
- **State:** SQLite (trades, positions, snapshots, paper trading)
- **Language:** Python 3.11+
- **Key dependencies:** `httpx`, `pydantic`, `typer`, `rich`, `aiosqlite`, `cryptography`, `websockets`
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
