# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Autonomous loop commands (`start`, `driving_range`, `championship`) now default `--cycles` to 400 (~1 trading day worst-case at default 60s pause + 3600s monitor interval) to bound Claude API spend per run; pass `--cycles 0` (or the new `--max-cycles 0` alias) for the previous unbounded behavior, which now logs a startup warning (#543)
- Autonomous-loop agents (Caddie Master, Scout, Caddie, Closer, Monitor, Groundskeeper, Scorecard) now pin `model: claude-sonnet-4-6` in their `.claude/agents/*.md` frontmatter, dropping per-cycle Claude API cost ~10× from Opus. Override the Caddie Master subprocess globally with `gimmes config set model.default <id>` (e.g. `claude-opus-4-7`); for per-agent overrides (Scout, Caddie, etc.), edit the agent's frontmatter directly because Claude Code's sub-agent dispatch does not accept a runtime override from the parent (#544)

## [0.6.4] - 2026-04-21

### Added
- Caddie base-effect arithmetic primacy rules for CPI/inflation markets — the sanity-check extraordinary event handler now keeps mechanical threshold math instead of deferring to headline web forecasts, and the deep research framework requires web forecasts to validate MoM inputs rather than override threshold probabilities (#536)

### Fixed
- Stream idle timeout and partial response now recognized as transient API errors for retry (#538)

## [0.6.3] - 2026-04-18

### Fixed
- Autonomous loop hangs indefinitely between cycles after macOS system sleep/wake — replaced bare `time.sleep` with chunked `_resilient_sleep` using `time.monotonic()` to detect wall-clock jumps and resume promptly (#531)
- Monitor staleness alert fires false positives during normal monitor-only cycling — raised default threshold from 2h to 3h, configurable via `GIMMES_STALENESS_THRESHOLD` env var (#532)

## [0.6.2] - 2026-04-17

### Added
- `strategy.cm_min_edge_after_fees` (default 0.05) — explicit edge floor Caddie Master applies in Step 4c review. CM must cite the numeric threshold in every APPROVE/REJECT decision note; subjective descriptors like "thin edge" or "knife-edge" without a numeric citation are forbidden. Invariant: must be >= `strategy.min_edge_after_fees`. (#527)

### Fixed
- Driving range crashes on transient Anthropic API 5xx instead of retrying — `is_error: true` result envelopes are now treated as cycle failures and routed through the existing backoff + circuit breaker, recovering from API 5xx, overloads, timeouts, and connection resets (#526)
- `UnboundLocalError` in `validate`/`size`/`order` CLI commands caused by `probability` variable shadowing in nested async functions (#525)

## [0.6.1] - 2026-04-11

### Fixed
- Position sizing now uses backtested category base rates (80-90%) as a probability floor instead of unreliable LLM estimates (55-65%), increasing position sizes ~3.3x in gimme categories — only applied to NO-side trades, uses exact series matching to prevent prefix collisions (#517)

### Changed
- Updated README: documented Caddie sanity-check mode, base rate floor for sizing, simplified gimme criteria to reflect category-first approach

## [0.6.0] - 2026-04-11

### Added
- `gimmes monitor` command for local driving range health checks with iMessage alerts — runs hourly via cron during weekday trade windows, checks risk limits, cycle failures, and error logs (#511)
- Caddie sanity-check mode for gimme categories — 30-second 3-check fast path using category base rates instead of 5-minute deep research, ~80% token savings per candidate (#514)
- Clubhouse dashboard displays per-side config values (YES/NO) when in dual-side mode (#512)
- Exponential backoff (30/60/120/240s) on transient cycle failures instead of fixed sleep (#506)

### Changed
- Default scanner watchlist reduced from 51 series to 8 backtested gimme series — removes categories with negative backtested P&L; users can restore via `gimmes config set scanner.series` (#513)

## [0.5.1] - 2026-04-10

### Fixed
- Backtest engine now supports dual-side mode — runs per-side filter, score, and trade passes with correct price perspective instead of passing `side="both"` to the scanner (#501)
- Backtest series filter uses ticker prefix matching since `series_ticker` is empty on settled markets from the live API; also unions all per-side series for the initial fetch (#502)

## [0.5.0] - 2026-04-10

### Added
- Dual-side trading: set `strategy.side = "both"` to run YES and NO strategies simultaneously with independent price ranges, thresholds, probabilities, and series watchlists per side (#496, #497, #498)
- Per-side config via `SideOverrides` model — `strategy.yes_overrides.*` and `strategy.no_overrides.*` override flat defaults when in dual-side mode (#496)
- YES-side defaults to 9 equity index series (S&P 500 and Nasdaq-100 families including weekly, monthly, above/below, and up/down variants) where backtesting shows BUY YES is profitable at high prices (#497)
- Scanner runs per-side passes with deduplication and side tagging; formatter shows Side column in dual-side output (#498)
- Updated README with dual-side configuration guide, per-side gimme criteria, and strategy overview

## [0.4.2] - 2026-04-10

### Added
- Threshold ladder strategy: scan output groups candidates by event with sibling counts, Caddie researches the underlying event once and derives per-threshold probabilities, Caddie Master validates cross-threshold consistency (#493)
- 48-hour cooldown expiry: candidates rejected more than 48 hours ago are eligible for fresh re-evaluation regardless of prior score (#494)
- Updated README with position-aware windows, threshold ladder research, settlement dates in dashboard, and code staleness detection

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

[0.6.1]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.6.1
[0.6.0]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.6.0
[0.5.1]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.5.1
[0.5.0]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.5.0
[0.4.2]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.4.2
[0.4.1]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.4.1
[0.4.0]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.4.0
[0.3.0]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.3.0
[0.2.0]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.2.0
[0.1.3]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.3
[0.1.2]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.2
[0.1.1]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.1
[0.1.0]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.0
