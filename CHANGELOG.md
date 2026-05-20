# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.8.4] - 2026-05-20

Two data-quality fixes that close storage-time corruption and noise-filing leaks. Restart `driving_range` after `gimmes update` so the autonomous loop loads the new agent prompts and CLI.

### Fixed
- Stored prose CLI arguments no longer get corrupted by shell expansion. Agent-emitted commands containing `$0.41`, `$VAR`, or backticks had the `$0` portion expanded to `/bin/zsh` by the agent's bash subprocess before reaching the CLI, so a memo of `Market prices YES at $0.41` was stored verbatim as `Market prices YES at /bin/zsh.41`. New `--memo-file`/`--rationale-file`/`--body-file` options on `log-candidate`/`log-trade`/`position-note` take a path instead of inline text, bypassing argv entirely. Inline `--memo`/`--rationale`/`--body` options still accept text identically to before, but specifying both the inline and file variant in the same invocation is now a hard error (mutex) — surfaces a conflicting double-emit rather than silently picking one. Agents (caddie, caddie-master, closer, monitor, scout) now use the file-input pattern exclusively via a `mktemp` + single-quoted heredoc. `position-note --body` is now optional at the Typer level — the in-function check enforces "at least one of `--body` or `--body-file`" with whitespace stripping; the missing-arg error message changed format slightly but the CLI surface still accepts `--body "text"` identically to before. A drift-guard test (`test_no_agent_uses_inline_memo_body_rationale_for_prose`) prevents future agent prompts from reintroducing the inline pattern. An end-to-end regression test invokes the real CLI through `/bin/sh -c` and asserts `$0.41` survives to the DB. Existing corrupted rows are not repaired — the digit between `$` and `.` is unrecoverably lost (`$0.41` and `$5.41` both collapsed to `/bin/zsh.41`). (#589)
- Groundskeeper no longer files near-duplicate GitHub issues for recurring `(error_code, component)` tuples. Canonical anti-pattern: the same error code produced #597 → #598 → #599 within four hours because the local `github_issue_url` field was only set after filing, and newly-arriving rows tripped the threshold in isolation. A new Step 2.5 pre-flight runs `gh issue list --state all --label bug --search 'in:title "ERROR_CODE" in:body "COMPONENT"' --limit 100` before every filing. OPEN matches receive a single consolidated recurrence comment after the local rows are resolved against the existing issue URL; CLOSED matches within 24h are suppressed (the fix is presumed still propagating); CLOSED 24h-30d file a new issue citing the prior closure; CLOSED >30d treat as no match. CRITICAL severity and risk_breach category errors keep their "MUST file in current cycle" safety guarantee — they take a separate dedup branch that comments on an OPEN match but never suppresses under the 24h cooldown. When the `gh issue list` query itself fails, behavior is fail-open (file the possible duplicate) so a broken dedup query can never silently drop a real escalation. (#600)

## [0.8.3] - 2026-05-17

Three trading-logic prompt fixes that close the execution-layer leaks identified in the May 11 backtest analysis. Scorecard flagged the strategy as DEGRADED (18.2% win rate vs backtest 65-72%) and explicitly diagnosed "core edge predictions are not being realized" — these three fixes target the agent-decision biases driving that gap. Restart `driving_range` after `gimmes update` so the autonomous loop loads the new prompts.

### Fixed
- Caddie Master no longer treats a Monitor `Stop-loss breach` flag as forcing an automatic CLOSE. Step 2c now applies an asymmetric rule based on Monitor's `Thesis:` field: `Thesis: degraded` → CLOSE; `Thesis: intact` AND resolution imminent (<24h per `TimeToResolution:`) → HOLD; `Thesis: intact` AND not imminent → conditional CLOSE unless a tighter re-eval condition is articulated. Missing or malformed `Thesis:` defaults conservatively to CLOSE (this couples Caddie Master to Monitor's prompt — both agents need the v0.8.3 prompts loaded; running a partial update would silently bias toward closes). A new Step 4c REJECT criterion ("Stop-loss reopen lockout") prevents the close-and-immediately-reopen anti-pattern by rejecting candidates matching a stop-loss CLOSE in the current or prior cycle (lockout window is two cycles only — a cycle+2 reopen is not caught). Monitor's stop-loss flag template now requires `Thesis:`, `Price:`, and `TimeToResolution:` fields with pinned vocabulary (exact strings `intact`/`degraded` and integer hours `Nh`). Canonical case cited inline: KXGDP-26APR30-T2.5 cycles 1199-1200, where a thesis-intact position was force-closed on a 104%-of-stop-loss breach and re-opened 26 minutes later at a worse cost basis. (#586)
- `KXJOBLESSCLAIMS` added to Caddie's Sanity-Check Mode gimme-category fast-track at the same 0.85 base rate as peer employment series KXPAYROLLS/KXADP. Previously absent from the list, KXJOBLESSCLAIMS fell through to the deep-research path with ~56% approval despite a 6/6 backtest win rate, while KXCPI/KXCPIYOY (live losers) sat in the gimme list at 85-90% approval. Sanity-check still catches extraordinary events (government shutdowns, methodology changes, staleness); the deep-research playbook remains the fallback when Monitor flags a position or Caddie Master overrides. (#590)
- Caddie's Sanity-Check Mode now applies a per-event sibling-strike Kelly rule (check #4). When 2+ same-event candidates pass the existing three checks on the configured `trading_side` and share the same gimme-category base rate, only the candidate with the LOWEST price on `trading_side` is PROCEED'd; higher-priced siblings are PASS'd with rationale citing the dominant sibling. Closes the leak where Caddie picked higher-priced (worse Kelly) strikes within the same event — canonical case: live took KXADP-26APR-T100000 at \$0.71 (lost) when KXADP-26APR-T125000 at \$0.48 was a same-day winner. Guardrails included: `trading_side="both"` carveout (rule doesn't fire — sides aren't directly comparable), tied prices within \$0.01 (PROCEED all and defer to Caddie Master's concentration limit), CPI extraordinary-event arithmetic exception (each sibling needs CM review individually), and a cross-cycle limitation (rule applies within one review batch — cross-cycle siblings fall back on `max_event_exposure_pct`). When sibling prices violate monotonicity (a looser strike priced CHEAPER than a tighter sibling on the same side), the rule treats this as the gimme signal and PROCEEDs both — this exception path is net trade-increasing, so deployments with loose concentration limits may see more fills per event under that condition. (#591)
- `gimmes position-notes TICKER` now resolves tickers from positions ∪ candidates ∪ trades (`known_markets` source) rather than only currently-open positions. Required for the #586 Stop-loss reopen lockout to actually function: after Closer closes a stop-loss position, the ticker drops out of `open_positions`, and the prior resolver would silently return "No notes found" — making Caddie Master's lockout check unenforceable. The new resolver path surfaces decision notes for closed tickers so the `Trigger: Stop-loss breach` match can fire as designed. Also makes `position-notes` more generally useful for post-mortem review of closed positions.

## [0.8.2] - 2026-05-13

### Fixed
- `gimmes discover <category>` no longer crashes with `TypeError: object of type 'NoneType' has no len()` when Kalshi returns `{"series": null}` for an empty category. `dict.get(key, default)` returns the explicit None value, not the default — coercing with `or []` in `list_series`, `list_markets`, and `get_series_fee_changes` keeps all three honest to their `list[dict]` return-type contract. (#574)
- `gimmes position-context` no longer displays stale Caddie Master decision cycles for chatty positions. The "CADDIE MASTER DECISIONS" panel previously filtered for `note_type == "decision"` *after* applying a 20-row mixed-type limit on `get_position_notes`; on positions with many recent observation/flag notes (e.g. KXCPIYOY-26APR-T3.7, which showed c1403 governing despite the DB having c1409), recent decisions were silently evicted from the display window. `get_position_notes` now accepts an optional `note_type` kwarg, and the CLI issues a second query with `limit=25, note_type="decision"` so the full governance trail renders regardless of interleaved note density. (#580)

## [0.8.1] - 2026-05-12

### Fixed
- `gimmes market-info` and `gimmes position-context` now write to the `error_log` table on failure paths that previously logged only to Python logging: ambiguous prefix, no-match, race-condition close during lookup, Kalshi `HTTPStatusError`, and `httpx.RequestError`. Groundskeeper now sees what monitor-only cycles previously hid — the gap that let #581 run 60+ cycles without escalation. Extracts the `try / except log-only` envelope into a module-scope `_log_cli_error` helper; the ambiguous-prefix `context` payload caps `matches` at 20 with a `matches_total` count to bound row size. (#588)
- `gimmes backtest` now applies the full strategy filter set in its scoring loop. The engine previously honored only `strategy.gimme_threshold`, silently ignoring `strategy.min_true_probability` and `strategy.min_edge_after_fees` — parameter sweeps over either returned identical results. Logic mirrors `risk/validator.py`: `true_prob = effective_price + assumed_edge + base_rate_floor`, reject below `min_true_probability`; `edge_after_fees` rejected below `min_edge_after_fees`. The computed `(eff_price, true_prob)` ride forward in the `scored` tuple so Pass 1 doesn't recompute. Regression-safe at default live-config values. (#592)

## [0.8.0] - 2026-05-11

### Added
- `gimmes audit-cycles --date YYYY-MM-DD [--output FILE]` — audits a day's autonomous-loop cycle logs and produces a Markdown report. Parses every `${GIMMES_HOME}/logs/cycle-NNNN.json` whose UTC start_time falls on (or pre-buffer-spills into) the target UTC date, extracts Scout shortlist size, Caddie dispatch count, and trade events, cross-checks trade counts against the `trades` SQLite table, and renders a deterministic Markdown report with hours bucketed in America/New_York for readability. Phase 0 deliverable for #546. (#555)
- `gimmes pause-backtest [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--output FILE] [--json]` — backtests `pause_seconds` against trade-coincidence plus hour-of-window aggregation across all on-disk cycle logs. Range defaults to the earliest `candidates.scanned_at` date in `gimmes.db` through today UTC. Emits Markdown by default; `--json` emits the same structure as JSON. (#557)

### Fixed
- `gimmes market-info`, `gimmes position-context`, `gimmes position-notes`, and `gimmes trades --ticker` now accept a ticker prefix in addition to a full Kalshi ticker. A unique prefix (e.g. `KXJOBLESS`) is resolved to the canonical ticker before the existing exact-match lookups run; an ambiguous prefix prints a candidate list and exits non-zero. `market-info` additionally falls through to a literal Kalshi API call when the prefix matches no known local row, preserving the first-time-lookup UX for unseen markets. Closes the failure mode where agents reading a wrapped ticker in `gimmes positions` guessed a partial ticker and got `ticker not found` for an open position. (#582)
- `gimmes positions`, `gimmes trades`, `gimmes candidates`, `gimmes discover`, and backtest report tables now wrap long Kalshi tickers across multiple lines (`overflow="fold"`) instead of truncating with an ellipsis. Operators and agents can read the full ticker without referring back to the database. (#567)
- `BudgetTracker` no longer undercounts per-cycle Claude API consumption by ~10×. `parse_usage_from_stream_json` previously returned the first `usage` block found (the `result` event for the parent agent only), missing the 200-300 sub-agent assistant turns dispatched per Caddie Master cycle. The parser now sums `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens` across every `assistant` event in the stream, falling back to `result.usage` only when no assistant events carried usage. Dollar cost computed by `cost_from_usage` is now ~5× more accurate. Operators relying on `gimmes budget` for capacity decisions should re-evaluate the daily $25 cap against true per-cycle cost (~\$5/cycle in trade windows). (#563)
- `calculate_pnl` no longer mispairs multiple closes against the first open per ticker. Group key is now `(ticker, side)`; events sort by timestamp ascending; opens and `size_up` events roll into a running weighted-average cost basis; closes match against residual position at average cost. Orphan closes (close with no matching open, or close exceeding remaining contracts) log a warning and contribute \$0 P&L for the unmatched portion instead of inflating P&L by `close_price * count`. (#561)
- `gimmes report` was silently zero-output when skip volume dominated the trades table — `get_trades(db, limit=1000)` returns the most-recent 1000 rows ordered by timestamp, and 17k+ skip records evicted the actionable opens/closes the P&L calculator needed. Report now fetches by action (`open`, `close`, `size_up`) separately so skip volume can't truncate. (#542)

### Changed
- All formerly overnight release windows now open at 04:00 ET on the release day instead of the prior evening (formerly 18:30 ET, 20:00 ET, 23:00 ET, etc.). Window-close times unchanged. `_index_contracts` (release-day-only 14:00–16:00 ET) is unaffected. Eliminates the empirically dead 18:00–04:00 ET block where 12 days of cycle data showed ~0.009 trades/cycle (1 trade in 116 full cycles), while preserving the 5–8 AM EDT pre-positioning window where smart money activity concentrates. Affects `_jobless_claims`, `_treasury_notes`, `_adp`, `_ism_pmi`, `_nfp`, `_cpi`, `_core_pce`, and `_gdp_advance`. (#558)

## [0.7.0] - 2026-05-06

### Added
- Daily Claude API budget guardrail for the autonomous loop. Two caps apply per UTC day: `--max-sessions-per-day N` (default 80) and `--max-daily-cost-usd X` (default $25). On cap hit the loop logs a warning, sends an iMessage push (when `GIMMES_NOTIFY_PHONE` is set), and sleeps until the next UTC midnight, then resumes automatically. Pass `0` for either flag to disable that cap. State is persisted to `${GIMMES_HOME}/budget.json`; new `gimmes budget` CLI command (with `--days N` and `--json`) shows running totals and remaining headroom. (#550)

### Changed
- Autonomous loop commands (`start`, `driving_range`, `championship`) now default `--cycles` to 400 (~1 trading day worst-case) to bound Claude API spend per run. Pass `--cycles 0` (or the new `--max-cycles 0` alias) for the previous unbounded behavior, which now logs a startup warning. (#548)
- Autonomous-loop agents (Caddie Master, Scout, Caddie, Closer, Monitor, Groundskeeper, Scorecard) now pin `model: claude-sonnet-4-6` in their `.claude/agents/*.md` frontmatter, dropping per-cycle Claude API cost ~10× from Opus. To override only the Caddie Master subprocess at runtime: `gimmes config set model.default claude-opus-4-7`. Sub-agents continue to read their own frontmatter (edit those files for per-agent overrides). (#549)

### Fixed
- Caddie Master Step 4c now reads `strategy.side` from config and rejects any candidate whose side does not match the configured side. The structural edge is side-specific; CM was previously overriding the configured side during extraordinary events, which caused the KXCPI-26MAR-T0.8 YES trade loss. (#541)

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
