# GIMMES ⛳

> *We only play the gimmes.*

An autonomous Claude Code agent team that trades Kalshi prediction markets by identifying **100 Percenters** — contracts priced well below their true probability of winning. Named after the golf term for a putt so close it's automatically conceded.

---

## What it does

GIMMES hunts for mispriced certainty. When a contract is trading at 70¢ but research, context, and converging signals say it should be 95¢+, that's a gimme. The system finds them, sizes them, watches them, and decides when to close or let ride.

**The core thesis:** Prediction markets systematically underprice near-certain outcomes. Human bettors anchor to headline odds without doing the underlying work. GIMMES does the work.

---

## Agent team

| Agent | Role | Responsibilities |
|---|---|---|
| **The Scout** | Opportunity discovery | Scans Kalshi for markets above 55¢, scores each for gimme potential |
| **The Caddie** | Research & analysis | Deep-dives shortlisted markets — news, social signals, historical patterns |
| **The Closer** | Trade execution | Sizes positions using fractional Kelly, places maker limit orders |
| **The Caddie Monitor** | Position watching | Monitors open contracts, flags early-close opportunities |
| **The Scorecard** | Reporting | Tracks P&L, win rate, edge accuracy, and strategy performance |

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

## Tech stack

- **Runtime:** Claude Code (interactive session, Claude Max)
- **Platform:** Kalshi (CFTC-regulated DCM)
- **API:** Kalshi REST + WebSocket, RSA-PSS authentication
- **State:** GitHub Issues (trade log, open positions, agent decisions)
- **Language:** Python 3.11+
- **Key dependencies:** `kalshi-python-async`, `anthropic`, `httpx`, `pydantic`

---

## Project structure

```
gimmes/
├── agents/
│   ├── scout.py          # Market scanning and initial scoring
│   ├── caddie.py         # Research and Gimme Score generation
│   ├── closer.py         # Trade execution and order management
│   ├── monitor.py        # Open position watching
│   └── scorecard.py      # P&L and performance reporting
├── kalshi/
│   ├── client.py         # Authenticated API client (REST + WebSocket)
│   ├── orders.py         # Order placement and management
│   └── markets.py        # Market discovery and data
├── strategies/
│   └── gimme.py          # Gimme scoring logic and thresholds
├── sizing/
│   └── kelly.py          # Fractional Kelly with fee adjustment
├── config.py             # Thresholds, limits, agent parameters
├── AGENTS.md             # Agent instructions for Claude Code
└── README.md
```

---

## Configuration

```python
# config.py
GIMME_THRESHOLD       = 75       # Minimum Gimme Score to execute
MIN_MARKET_PRICE      = 0.55     # Only scan markets above this
MAX_MARKET_PRICE      = 0.85     # Only scan markets below this
MIN_TRUE_PROBABILITY  = 0.90     # Model must see ≥90% to qualify
KELLY_FRACTION        = 0.25     # Conservative fractional Kelly
MAX_POSITION_PCT      = 0.05     # Max 5% of bankroll per trade
MAX_OPEN_POSITIONS    = 15       # Concurrent position limit
DAILY_LOSS_LIMIT_PCT  = 0.15     # Auto-stop at 15% daily drawdown
MIN_EDGE_AFTER_FEES   = 0.05     # 5pp minimum edge after fee math
PREFERRED_ORDER_TYPE  = "maker"  # Limit orders only; no takers by default
```

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

## Status

`[ ] Scaffolding` → `[ ] Scout agent` → `[ ] Kalshi client` → `[ ] Caddie research loop` → `[ ] Closer + sizing` → `[ ] Monitor` → `[ ] Paper trading` → `[ ] Live`

---

*Built with Claude Code. Trades on Kalshi. Plays only the gimmes.*
