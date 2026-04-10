# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.4.1] - 2026-04-10

### Added
- Position-aware trade windows in autonomous loop: full pipeline cycles automatically run near position settlement dates, not just during scheduled data release windows (#484)
- Code staleness detection: autonomous loop warns when installed code changes or remote has newer commits available (#490)
- Settlement date column in Clubhouse dashboard open positions table (#485)

### Fixed
- CPI and GDP trade windows now use actual BLS/BEA release dates instead of hardcoded approximations — includes 2025-2026 lookup tables with fallback heuristic for future years (#482)
- Recalculate sleep after monitor cycles to catch newly opened trade windows (#491)
- `gimmes update` no longer fails when new releases add files that exist as untracked in the repo (#483)

## [0.4.0] - 2026-04-03

### Added
- Event-driven scheduling with trade window calendar — the autonomous loop now sleeps between data release windows instead of cycling every 60s, reducing token usage ~80-90% with zero missed opportunities. Nine windows cover all profitable settlement times: equity index close (daily), jobless claims and treasury notes (weekly), CPI/NFP/ADP/ISM/PCE (monthly), GDP advance (quarterly). New `--monitor-interval` flag controls monitor-only cycle frequency outside windows (#474)
- Concentration limits enforced in backtest engine for more realistic historical simulations (#466)

### Fixed
- Config overrides no longer silently revert to code defaults — `config set` now always persists values to the database, and the wizard pins all visited settings (#476)
- Rate limit errors now pause the autonomous loop until the advertised reset time (30-minute fallback) instead of burning cycles retrying every 60 seconds (#475)
- Staleness filter no longer removes actively-traded markets with stable prices — now uses volume activity and open interest changes instead of price-only, preventing 77 of 83 eligible markets from being incorrectly filtered (#468)

## [0.3.0] - 2026-04-03

> **Note:** The backtest subsystem received 4 fixes in this release. Results from v0.2.0 backtests should be re-run for accuracy.

### Added
- Event-level and series-level concentration limits to prevent over-exposure (#458)
- Profit-taking trigger for Monitor to lock in gains on winning positions (#457)
- Scout market staleness tracking to reduce redundant scanning (#451)
- Skip reason tracking via recommendation column in candidates table (#449)
- Cross-cycle agent memory with delta observations and decision expiry (#448)
- Configurable max_pages and date filtering for market API (#443)

### Fixed
- Edge calculation and side defaulting for BUY NO strategy — NO-side candidates were showing negative edge and getting skipped (#455)
- Hardcoded 90% probability gate in Caddie and Closer that blocked all variance play trades (#442)
- Backtest accuracy: switched from historical API to live API per-series (#436), use market-level prices instead of candlesticks (#438)
- Backtest fill simulation removed in favor of direct market price (#439)

### Changed
- All agent definitions now read configured values instead of hardcoding defaults (#445)
- Backtest fetches chunked by month to avoid 40K pagination truncation (#461)

## [0.2.0] - 2026-04-02

**Breaking:** Default `strategy.side` changed from `"yes"` to `"no"`. Existing users who prefer BUY YES must run `gimmes config set strategy.side yes`.

### Added
- Backtest mode: `gimmes backtest --from --to --balance [--edge] [--json]` validates strategies against historical settled Kalshi markets with win rate, P&L, ROI, max drawdown, and Sharpe ratio
- BUY NO (contrarian) strategy support via `strategy.side` config — scanner, scorer, validator, sizer, backtest, and CLI all evaluate from the configured side's perspective
- Expected-value position sizing mode (`sizing.mode = "ev"`) for variance plays where probability is moderate but expected value is positive
- Per-position stop-loss trigger for Monitor (`risk.position_stop_loss_pct`, default 15%)
- CLOSE execution procedure for the Closer agent — positions can now be exited autonomously
- SIZE UP opportunity flagging: Monitor notes thesis intactness on adverse price moves; Caddie Master gains SIZE UP as a third decision option alongside HOLD and CLOSE
- SIZE UP bias rule: when thesis is intact and bankroll is under 50% deployed, SIZE UP is the presumptive action
- Domain playbooks for Caddie with category-specific research sources across 8 market categories
- `gimmes reset-cooldown` command to clear cached candidate scores
- Auto-clear candidates when strategy config changes via `gimmes config set`
- Concurrent position tracking in backtest with chronological entry/settlement events
- Per-page series and date filtering for historical market fetch to reduce memory

### Changed
- Default strategy pivoted from BUY YES (55-85¢) to BUY NO — backtesting showed BUY YES had -54% ROI while BUY NO is consistently profitable
- Price range descriptions updated to be side-agnostic

### Fixed
- `config set scanner.series` no longer double-encodes JSON array values
- Cooldown system no longer blocks all candidates after a strategy change
- `full_score` now uses side-appropriate price for NO-side edge and depth calculations
- `test_get_risk` no longer reads user config — uses isolated test defaults
- `install.sh` uses `$HOME` instead of hardcoded path in shell RC export

## [0.1.3] - 2026-03-22

### Fixed
- Fix initial_prompt argument order in `_launch_claude_agent()` so agent sessions auto-start with the greeting prompt

## [0.1.2] - 2026-03-22

### Added
- Auto-start greeting and exit hint for Caddie Shop agent sessions
- Agent sessions (Starter, Caddie Shop) now run without tool-permission prompts via `--allowedTools` enforcement

### Fixed
- Starter agent tour UX: auto-start greeting, complete team roster, curated help output, and exit hint

## [0.1.1] - 2026-03-22

### Added
- Candidate lifecycle management with pruning and position filtering —
  candidates exit the pipeline when opened as a position, market inactive,
  aged out, or stale duplicates; new `prune-candidates` CLI command
- `gimmes uninstall` command
- Scroll-to-load pagination for Candidate Pipeline and Open Positions
  dashboard panels
- Local timezone display with dates for user-facing timestamps
- Activity logging when Caddie Master skips all candidates
- Skip-logging for Step 1 risk-limit decision gates
- Short-circuit `gimmes update` when already on latest version
- Staleness check for position tickers
- Caddie thesis carried forward as trade rationale

### Fixed
- `size_up` trades now source thesis from the open trade record
- Install script checks out latest release tag instead of staying on main HEAD
- Trade detail modal title backfilled from market API when not in cache
- Open Positions panel no longer grows unboundedly, pushing down dashboard layout
- KalshiClient resource leak in market-checking path

### Changed
- Agent definitions use `gimmes` CLI instead of `python -m gimmes`
- Post-placement error handling log levels upgraded

## [0.1.0] - 2026-03-21

First public release.

GIMMES is an autonomous trading system that finds mispriced contracts
on Kalshi prediction markets using a team of Claude Code agents.

### Added
- Kalshi API client with RSA key authentication, WebSocket streaming
  for real-time market data, and retry with exponential backoff on
  network errors
- GimmeScore scoring engine with configurable weights, Kelly criterion
  position sizing, and category-aware market scanning with curated
  series watchlist
- Autonomous agent team: Scout (scanning), Caddie (research), Caddie
  Master (orchestration), Closer (execution), Monitor (surveillance),
  Scorecard (reporting), Groundskeeper (error escalation), Pro (strategy
  tuning), Caddie Shop (config advisor), and Starter (product tour)
- Autonomous trading loop with per-cycle logging, crash recovery,
  configurable timeouts, and circuit breaker
- Caddie Master review gate before dispatching trades and close
  authority for open positions
- Paper trading mode (driving range) with simulated fills against real
  market data
- Championship mode for live trading with real capital
- CLI with commands for scanning, ordering, position management, trade
  logging, candidate tracking, and system administration
- Pre-order summary with cost display and confirmation pause
- Market discovery command for exploring Kalshi series by category
- Strategy analysis workflow: lesson extraction, data-backed
  recommendations, and interactive tuning
- Position journal for attaching observations, flags, and decisions to
  open positions
- Clubhouse: local web dashboard with live SSE streaming for trades,
  positions, P&L, equity curve, agent activity, and candidate pipeline
- SQLite-backed configuration with interactive wizard, Pydantic
  validation, headless mode, and auto-migrations on connect
- Risk management: configurable bankroll, pre-trade validation, daily
  loss limits, mark-to-market P&L, and session spending controls
- Structured error logging with Groundskeeper escalation to GitHub
  issues
- Mark-to-market snapshots from the SSE stream for portfolio valuation
- Curl-to-shell install script with global CLI wrapper, Fish shell
  support, and post-install guidance
- Self-update command with stale-code protection and tag-based version
  checks

[0.4.0]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.4.0
[0.3.0]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.3.0
[0.2.0]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.2.0
[0.1.3]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.3
[0.1.2]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.2
[0.1.1]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.1
[0.1.0]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.0
