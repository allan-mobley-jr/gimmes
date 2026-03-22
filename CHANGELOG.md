# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

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

[0.1.3]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.3
[0.1.2]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.2
[0.1.1]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.1
[0.1.0]: https://github.com/allan-mobley-jr/gimmes/releases/tag/v0.1.0
