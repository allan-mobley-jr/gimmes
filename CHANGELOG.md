# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.8.12] - 2026-07-16

The hourly strike-ladder feature (#721): the autonomous loop can now paper-trade KXBTCD hourly strike ladders — scan windows before each top-of-hour settlement, a third "hourly" cycle type, taker execution, and hold-to-settlement semantics, validated end to end on the paper stack. Everything is default-inert while `scanner.hourly_series` is empty (a stock install behaves identically), with three exceptions to know: **(1) timed-out cycles now count toward the daily session budget** — Anthropic charges for the killed subprocess's tokens, so the session cap reflects them (previously they were silently uncounted); **(2) `CATEGORY_BASE_RATES` gains KXBTCD at 0.70** — `gimmes size`/`gimmes validate`/auto-sized NO-side orders on KXBTCD tickers now floor the estimate at 0.70 regardless of the hourly switch (with hourly off the global 0.90 validator floor still blocks the order itself, so the visible surface is sizing/validation output and `--force`d orders); **(3) Caddie's time rubric is aligned to the scorer** (>60 days now scores 15, was 20; `<1 day → 20` is now explicit) — a pre-existing prompt/scorer drift fix that marginally affects full-cycle scoring. **Restart `driving_range` after `gimmes update`** — the loop code and all five agent prompts changed. To enable the experiment, see the README's "Hourly ladders (KXBTCD paper experiment)" recipe: `strategy.side` must be `"no"`, `gimmes config add scanner.hourly_series KXBTCD`, and raise `budget.max_sessions_per_day` (~120) — hourly adds up to ~24-25 sessions/day.

### Added
- **Hourly strike-ladder paper trading** (#721; parts #722-#725, PRs #726-#729): `scanner.hourly_series` is the switch (with `hourly_lead_minutes` and `hourly_max_cycles_per_window`); hourly tickers get their own price band (`strategy.hourly_min/max_market_price`, 0.30-0.85) and probability floor (`strategy.hourly_min_true_probability`, 0.70 — deliberately equal to the KXBTCD base rate; the edge-after-fees check and Caddie's sanity checks are the binding gates), bypass the scanner min-days floor, score time 70 (not 20) on the sub-day branch of full_score, and print an HOURLY tag in scan output. The loop wakes `hourly_lead_minutes` before each top-of-hour settlement (DST-immune UTC hour-top arithmetic — the fall-back day genuinely has 25 windows), fires at most `hourly_max_cycles_per_window` cycles per window, clamps the subprocess timeout to the window close (a straggler forfeits the cycle, never orders into a settled market), skips dispatch under 120s remaining, sleeps to land exactly at the next open, and warns at startup on unusable leads (≤2 minutes can never fire; <10 risks chronic clamp timeouts). Precedence: release windows > held-position windows > hourly > monitor — a held non-hourly position keeps its surveillance cycles, while hourly-series positions are excluded from position windows (hold-to-settlement; their settlement reconciles on the next hourly cycle). Agent prompts gain the fast lane: Caddie Master runs a trimmed step list with ONE batched conferral (a named, paper-only relaxation — every reject criterion and per-candidate decision note still applies), Caddie fast-tracks KXBTCD with crypto-shaped sanity checks (spot-distance vs ~30-min realized move) and exempts hourly ladders from cheapest-sibling selection (the backtest entered every in-band rung), Closer sends EVERY hourly order as taker (a maker stop-loss exit in a sub-hour book never fills), Monitor never fires time-decay on hourly positions, and 13 drift guards pin the load-bearing sentences. End-to-end paper validation covers the full NO lifecycle with exact payout math, settlement P&L semantics (#622/#628), and the maker-honesty pair (#690/#255); the README carries the operator recipe with the `strategy.side="no"` prerequisite and the timing-budget guidance (`scanner.hourly_lead_minutes` is the knob).
- **`gimmes order --taker`** (#722): per-order taker override (post_only=false) wired through validation, fees, and Kelly auto-sizing; the global `orders.preferred_order_type` default is untouched.

### Changed
- **Timed-out cycles count toward the daily session budget** (#723): the #545 intent ("always increment the session count once the subprocess has spawned") now covers the TimeoutExpired path — the hourly clamp makes timeouts a routine outcome rather than an anomaly, but the accounting fix applies to all cycle types.

## [0.8.11] - 2026-07-13

The backtest engine trilogy: exit modeling, a configurable entry offset, and sub-day candle granularity — together they make the 807k-market short-lived universe (hourly crypto/FX ladders) backtestable end to end. All three default to current behavior — TP/SL exits run only when values are passed, `--entry-offset` defaults to 1 day, `--candle-period` to 1440 (daily) — so a bare `gimmes backtest` produces the same numbers as v0.8.10 (the report header gains three always-on lines — Entry offset, Candle period, and Exits — plus one INFO progress line before the candle pass, and the JSON gains config fields, exit counters in the funnel, and `exit_reason`/`exit_price`/`exit_time` on every trade row, so anything parsing report output sees new lines/keys). **The 0.8.10 note still applies: the pre-0.8.10 `strategy.gimme_threshold` recommendation (60 -> 50, computed with the old fee-free metric) is STILL pending — reject it via `gimmes tune` (answer n, confirm "Mark as rejected?") before re-running `gimmes lesson`, which skips regenerating a parameter while a pending recommendation for it exists.**

### Added
- **TP/SL exit modeling** (#714): `--take-profit 0.8` / `--stop-loss 0.15` (fractions of max profit / cost basis) walk each entered position's post-entry candles and exit at the first trigger, mirroring the live formulas exactly — stop-loss on the fee-inclusive cost basis, take-profit on the fee-free max profit, marked at the side-effective midpoint the live monitor sees, with conservative touch fills and the exit leg paying its own fee under the run's fill model (one `taker_fill` flag governs both legs). Exits free capital chronologically inside the replay; exited trades keep both the eventual settlement result AND the would-have-settled time, so exit-vs-hold analysis has everything it needs; walk fetch failures are counted (entered positions only) and warned, never silently absorbed. First product: an exploratory exit sweep (single-quarter sample, ~30 trades, Apr-Jun 2026) in which every mechanical SL-15 stop fired on an eventual winner — hold-to-settlement +75% vs live-mirror (TP80/SL15) +25.2% — early support for Caddie Master's thesis-first hold discretion and the #659 backstop placement; re-run as settled history accrues.
- **Configurable entry offset** (#713): `--entry-offset DAYS` (float) governs the entry window, candle selection, synthetic close, days-filter warning, and the #714 walk boundary — one source of truth. Each offset value is a clean cache namespace; NaN/inf are rejected at both layers (CLI red-exit, engine fail-fast ValueError before any fetch); larger offsets give the exit walk real candle density.
- **Sub-day candle granularity** (#716): `--candle-period 1|60|1440`. Live-probed facts now pinned in code: the candlesticks API cap is exactly 5000 periods per request (a hard 400, not truncation); daily candles are midnight-US/Eastern aligned, so a <24h market has no daily candle before close (sub-day offsets need sub-day candles); candle volume is strictly per-period. At sub-day periods `volume_24h` is the trailing-24h sum of per-period volumes — the single-candle value would understate 24h volume by up to 1440x and silently bias the scanner's volume gates; the period-1440 default deliberately keeps the selected candle's volume so stale-candle markets keep pricing exactly as before. A walk-period fallback ladder (1 -> 60 -> 1440) respects the cap, warns per rung, and surfaces in the report header ("exits walked at N min — capped fallback") and JSON.

## [0.8.10] - 2026-07-10

Two follow-ups to the v0.8.9 advisor/analytics stack. Restart `driving_range` after `gimmes update` so the autonomous loop loads the new Scout/Caddie prompts. **One behavior change to know: the missed-opportunity audit now scores skips by EV after fees and recommends less often. Any `strategy.gimme_threshold` recommendation still pending from before this release was computed with the old fee-free metric — reject it via `gimmes tune` (answer n, then confirm "Mark as rejected?"; tune only ever applies on an explicit y) BEFORE re-running `gimmes lesson`, because lesson skips regenerating a parameter while a pending recommendation for it exists.** Rationale text and `supporting_data` keys change (`correct_skip_rate`, `fn_cost_total` added), so anything parsing recommendation rows sees new fields.

### Fixed
- **Missed-opportunity audit scores skips by EV after fees** (#707): a "missed win" now requires positive expected value at the recorded skip price — side-effective price (`effective_price`, NO-side conversion) plus maker fee via `edge_after_fees`, with the #658 `price_at_bound` guard keeping bound-priced skips in the denominator as correct skips. The old numerator was fee-free ex-ante edge sign, which the docstring mislabeled as settlement-based and which fabricated missed wins from the fee margin, generating systematic threshold-loosening pressure. The cited near-miss cohort is now restricted to the score band the recommended threshold would actually admit — the recommendation is derived once from the full sub-threshold cohort, then the citation refilters to `[recommended, threshold)`, and an empty admitted band yields no recommendation (previously a floor-50 recommendation could be justified entirely by score-45 skips it wouldn't take). The rationale surfaces the tradeoff, not just the loosening pressure: EV-based FN rate, correct-skip rate, and total foregone EV at 1 contract/skip. Deferred follow-up: #708 (`log-trade` skips fail entirely in dual-side mode, starving this audit's input).
- **Liquidity skips carry `--reason liquidity`** (#710): Scout and Caddie were logging book-emptiness skips with prose-only rationales and empty structured reasons (116+ rows since 2026-07-08), leaving the #657 skip analytics unable to classify the skip class that dominates wide-band scanning. Both prompts gain mandate blocks — book-emptiness skips MUST carry `--reason liquidity`, with the trigger phrased in terms of each agent's observable output (Scout reads `gimmes score`'s "Best YES Bid = None"; Caddie reads `market-info`'s "$0.00"), all other skips omit `--reason`, and thin-but-two-sided books are a research PASS, not a liquidity skip. `gimmes log-trade` now prints a yellow warning whenever any skip is logged without `--reason` — printed after the insert succeeds, never rejecting (rejection would silently drop agent rows under the no-retry rule). **The warning is expected and benign for non-liquidity skips** — the row was logged; agents are instructed not to re-log or tag just to silence it. Drift guards pin both prompts' mandates, and a vocabulary sync guard ties every `--reason` value in any agent prompt to `cli._SKIP_REASONS`.

## [0.8.9] - 2026-07-07

The full 26-issue engineering queue from the July session (#653–#704): settlement P&L lands in the scorecard, the backtest loses its look-ahead bias and gains an on-disk candle cache, live positions get a hard loss backstop and reopen-churn gates, and `gimmes lesson` produces its first real recommendations. Restart `driving_range` — and any running `gimmes clubhouse` — after `gimmes update` so the loop loads the new agent prompts and the dashboard picks up the corrected daily-P&L math. **Five behavior changes to know before comparing numbers or resuming the loop:** (1) positions at ≥200% stop-gate consumption now carry a `MANDATORY-CLOSE` banner and Caddie Master is mandated to close them unconditionally — positions previously held through gate loopholes will be closed; (2) `gimmes order` now hard-blocks same-ticker reopens through the churn gate (`--force-reopen` is the audited escape hatch); (3) paper-mode orders that can't fill against the book now cancel and **exit 1** where they previously fabricated a fill and exited 0 — scripts checking `gimmes order`'s exit code see a new failure mode; (4) backtest results change materially for identical inputs because the look-ahead bias is gone — honest numbers look worse than pre-0.8.9 runs; (5) reported P&L, win rate, and Sharpe all change because settlements now reach the scorecard, win rate uses the weighted cost basis, and Sharpe is time-aware.

### Added
- **On-disk candle cache for backtests** (#696): settled-market candle history is immutable, so windows are cached at `${GIMMES_HOME}/backtest_cache.db` keyed by ticker and window — reruns and parameter sweeps skip the multi-minute fetch pass entirely. Empty history is negative-cached; failures are never cached (they retry loudly every run); corruption degrades to fetch-through with one warning; a `Candle`-schema fingerprint drops the cache wholesale on shape drift. `--no-cache` bypasses it. Companion hardening (#704): candlestick responses with a missing/renamed `candlesticks` key, non-dict bodies, renamed close/timestamp fields, or non-numeric values now raise into the backtest's `fetch_failures` counter instead of parsing as empty/zero data the cache would poison permanently.
- **`--taker-fill` backtest flag** (#682): conservative fill model that prices entries at the ask with taker fees — the pessimistic bound to pair with the default maker model. Also from #682: stale-candle counts and zero-sizing outcomes are surfaced in the report instead of silently shaping the sample.
- **Hard loss backstop** (#659): `gimmes positions` shows per-position stop-gate consumption; at ≥200% a red `MANDATORY-CLOSE` banner appears and Caddie Master closes unconditionally — no thesis, no re-eval condition, no exception. **STALE / BASIS-SUSPECT StopGate flags** (#674): stale marks and corrupted live cost basis stop rendering as confident percentages.
- **`gimmes lesson` produces recommendations** (#656): closes are paired to entries lifecycle-by-lifecycle, so the strategy advisor finally has outcomes to learn from (previously it produced nothing). The analysis window is bounded by the new `strategy.lesson_window_days` config (default 90; 0 = all-time), with lifecycle-aware outcome keys and corrupt-position skip-and-escalate (#686, #668).
- **Reopen-churn gate** (#661): `gimmes order` hard-blocks reopening a ticker recently closed — the audited failure fired 21 seconds after a close. Side-aware with check-first ordering (#678); `--force-reopen` overrides with an audit trail.
- **Probability-flip warnings** (#660): a candidate whose probability estimate flips against its own recent scoring gets flagged before capital moves; the flip baseline is robust against bookkeeping rows, and Caddie Master gets a memo panel for review (#676).
- **Skip-row analytics** (#657): non-entry skips backfill candidate analytics with structured reasons, bounded by a staleness rule so ancient skips don't masquerade as current signal (#670).

### Changed
- **Backtest selects, prices, gates, and sizes through the entry-day lens** (#655, #666): entries are priced on entry-day candles and candidates are selected as the market looked on entry day — settlement-time snapshots leaked future information into selection, pricing, and sizing. **Results for identical inputs change materially; the new, worse numbers are the honest ones.** New funnel counters distinguish data sparsity, one-sided quotes, stale candles, and fetch failures.
- **Time-aware log-return Sharpe** (#654) replaces the daily-assuming sqrt(252) computation — irregular snapshot intervals no longer inflate the ratio.

### Fixed
- **Settlement P&L reaches the scorecard** (#653): `settle()` writes close trades, historical settlements backfill, and drift marks reprice. Settlement source is now authoritative with a resting-sell clamp and manual-close repricing (#663). Championship syncs consume settlements at every call site, with settled-count parsing hardened against fp-scaling misparses, plus count clamping, idempotency guards, and evidence rows (#684); the settlement writer itself is hardened against partial data (#668).
- Win rate uses the weighted cost basis via `calculate_pnl` instead of a per-trade approximation (#662).
- Scorer and sizer stop fabricating edge at price bounds — a candidate at the bound has zero edge, and the validator bound check runs unconditionally (#658, #672).
- Daily P&L: the canonical SQL is shared between CLI and dashboard (#680), and open-trade matching is side-scoped with mixed-format timestamps normalized in both WHERE and ORDER BY — a same-second reopen or a legacy space-separated timestamp can no longer attribute the wrong entry price (#695). Dashboard window truncation and orphan render noise fixed, with a schema check (#680).
- Paper broker: maker orders against an empty opposing book **cancel instead of fabricating fills** — canceled orders exit 1, write an error row, annul the trade record, and refund BUY costs; rejects carry structured reasons (#690).

## [0.8.8] - 2026-07-03

Runtime enforcement of the #641 note-quality rules plus the close-out of the Rich markup-eating bug class. Restart `driving_range` after `gimmes update` so the autonomous loop loads the new agent prompts.

### Added
- Runtime validators enforcing the #641 rules at the `position-note` write path (#643): a **semantics guard** cross-checks YES/NO directional claims in observation notes against the market's settlement language (snapshotted at position-open into `positions.rules_primary`, migration v17 — additive and idempotent), and a **playbook footer audit** enforces the four-outcome footer grammar, 13-source enumeration, publication-date freshness (fresh means newly published, not re-found), and SUPERSEDED stickiness. **Observation writes that violate these rules now exit 1 where they previously succeeded**; `--force` remains the audit-visible bypass (prints a warning naming #614 and #643) and is reserved for backfill scripts. Warnings (unparseable clauses, threshold mismatches, duplicate footer rows) print but never block, and parser uncertainty always passes silently — the guard hard-rejects only high-confidence violations. Validated observations now insert under the canonical resolved ticker so next-cycle prior-footer lookups can't silently miss. Follow-ups: #646 (coverage telemetry), #647 (reconcile-time snapshot backfill), #648 (strictness escalations).
- `market-info` now displays the market Subtitle and verbatim settlement language (`Rules (primary)`) so agents can ground threshold semantics; an empty rules row means UNVERIFIABLE — Monitor flags, Caddie PASSes, Caddie Master REJECTs rather than falling back to title-derived semantics (#641).

### Fixed
- Rich markup no longer eats bracketed text from external data across the CLI: market titles, settlement rules, validation failure lists, discover/candidates tables, validator rejection messages, and the errors table — where a stray closing tag in logged error text could previously crash the render. Title escaping is now central in `format_kv_table` (callers must not pre-escape); remaining low-priority sites tracked in #650. (#641, #643, #644)
- Kalshi API explicit nulls for `subtitle`/`rules_primary` no longer crash `market-info` with an unlogged traceback (#641).
- Agent prompts: threshold-semantics grounding ("YES wins when X / NO wins when Y" derived from settlement language, never the title — negative thresholds are the documented trap), forecast freshness/supersession rules for the playbook audit footer, and empirically-validated web-search cache-bust DOs and DON'Ts (#618, #641).

## [0.8.7] - 2026-05-22

Three follow-ups to the v0.8.5 stack (#577 / #609). Restart `driving_range` after `gimmes update` so the autonomous loop loads the new Monitor prompt.

### Fixed
- `get_daily_pnl` now excludes synthetic reconcile-divergence close trades (`agent='reconcile'`) from the daily realized P&L aggregate. Without the filter, the synthetic closes written by `_log_reconcile_closes` (introduced in #609) would distort the autonomous-loop's daily-loss-limit trigger — an operator could see a "realized loss" from broker-side drift that they did not actually take. Three new tests in `TestGetDailyPnlExcludesReconcileCloses` lock in the invariant: pure-reconcile day → 0 P&L; real close + reconcile close → only the real close counts; multi-ticker reconcile drift → all excluded. (#622)

### Changed
- Monitor's observation template now requires a `Playbook sources checked this cycle:` audit footer for fundamental-economic-trigger tickers, enumerating every named bank (Goldman Sachs, JPMorgan, Morgan Stanley, BofA, Citi, Barclays, Wells Fargo, Deutsche Bank, UBS) and aggregator (FXStreet, MarketWatch, Reuters, Bloomberg) with one of three explicit outcomes per source: a freshly-searched value, an inherited prior result with citation, or `no result this cycle`. Without this footer, an operator auditing `gimmes position-notes` couldn't distinguish "Monitor ran the playbook, found no signal" from "Monitor skipped the playbook entirely" — the silent-failure path the 48h staleness rule (#577) was designed to defend against. For tickers NOT in the playbook category list (equity indices), the footer is OMITTED entirely via an inline annotation on the header line so an agent copy-pasting the template sees the omission rule immediately. Four drift-guard tests pin the footer block, full source enumeration (so partial-row drop-out is detected via count), the three allowed per-source outcomes, and the OMIT annotation on the header line specifically. (#615)

### Tests
- New `tests/unit/test_paper_reconcile_drift.py` (3 tests, no code change) — confirms paper-mode reconcile is already covered by #609's fix. Reviewer concern on PR #624 was based on misreading the reconcile path; `cli.reconcile()` routes paper mode through `sync_positions` (which #609 fixed), so the synthetic close + reconcile-divergence decision note are written uniformly across paper and championship modes. Tests exercise the canonical scenario (settle paper position → next reconcile writes synthetic close), multi-ticker drift, and an end-to-end CLI-level invocation via Typer's `CliRunner`. The CLI-level test would catch a future refactor that diverged paper-mode's reconcile path from championship's. (#623)

## [0.8.6] - 2026-05-22

### Added
- Budget caps (`budget.max_daily_cost_usd`, `budget.max_sessions_per_day`) are now config-settable and persist across loop restarts via `gimmes config set budget.max_daily_cost_usd 50`. Previously the only override was the `--max-daily-cost-usd` CLI flag at startup, which had to be remembered every time. CLI flags still win when both are present; if neither is set, the hardcoded defaults ($25 / 80) apply. `0` (from either source) means "unlimited"; `None` means "no override at this layer." Config changes do NOT live-apply to a running loop — the loop reads config once at startup and constructs `BudgetTracker` with fixed caps. Restart `driving_range` / `championship` after `gimmes config set budget.*` to pick up new values. (#626)

## [0.8.5] - 2026-05-21

Four fixes that close stale-template regressions in Monitor's observation writes (#577, #617, #614) and the reconcile-driven silent-bypass of the #586 stop-loss reopen lockout (#609). Restart `driving_range` after `gimmes update` so the autonomous loop loads the new agent prompts and CLI helpers.

### Fixed
- Monitor no longer misses Wall Street bank CPI forecasts across cycles, the c1391–c1407 failure mode that allowed Caddie Master to HOLD a losing position past its MUST-CLOSE re-eval condition (-$133.67 realized loss on KXCPI-26APR-T0.5). New `## Fundamental-Economic-Trigger Source Playbook` section in `monitor.md` mandates per-bank individual searches (Goldman Sachs, JPMorgan, Morgan Stanley, Bank of America, Citi, Barclays, Wells Fargo, Deutsche Bank, UBS) and per-aggregator queries (FXStreet, MarketWatch, Reuters, Bloomberg) for 17 fundamental-economic Kalshi categories. A Read-back assertion at the top of `Writing Observations` requires Monitor to surface every CM-cited bank/aggregator from the most-recent `decision` note with either a freshly searched result OR an explicit inherited citation; the FORBIDDEN clause rejects template assertions that contradict cited evidence. A 48-hour CM-decision staleness rule forces a full playbook re-search regardless of "no material change" so old re-eval conditions get a fresh check. 11 drift-guard tests pin the bank list, aggregator list, category list, the FORBIDDEN clause, the (a)/(b) inheritance enumeration, query-phrasing variation, surfacing format, no-result logging, and Caddie/Monitor category-list sync. (#577)
- Caddie Master decision-note templates now require a `Cited sources:` field on all 4 decision types (HOLD/CLOSE, SIZE UP, APPROVE, REJECT). The field accepts two forms: Form A (a bulleted list of `Source — metric value (publisher, YYYY-MM-DD)` matching Monitor's surfacing format) or Form B (the literal `None — decision based on price + thesis only` with U+2014 em-dash). A derivation rule forbids fabricated citations — a source is only allowed if it appears verbatim in the input CM actually consulted this cycle (Monitor's flag body for Step 2, Caddie's memo or market-info for Step 4c). Cross-references Monitor's `Fundamental-Economic-Trigger Source Playbook` by name so future bank/aggregator additions are naturally covered. Closes the gap that defanged Monitor's read-back assertion: pre-this-fix, most CM decisions were silent on sources and the read-back was vacuously satisfied on every observation. 7 drift-guard tests including per-template regex extraction, format match with Monitor surfacing, derivation-rule presence, and cross-file Playbook reference. (#617)
- Runtime forcing function for Monitor's read-back: `gimmes position-note --type observation` now rejects writes that contain the canonical c1407 stale-template phrase ("No named major Wall Street bank has published") when the most-recent CM `decision` note for the same ticker cites a named bank or aggregator with a numeric percentage value. Scope: fundamental-economic-trigger tickers only — equity-index tickers (KXSPX/KXINX/KXNASDAQ100) skip the validator. Bank-name matching uses word-boundary regex with explicit Citi aliases (Citibank, Citigroup); same-line proximity for source-with-value matching mirrors Monitor's playbook surfacing format. Ticker canonicalization via `resolve_ticker(source="known_markets")` runs before the decision lookup so canonical-form drift (`KXCPI-26APR-T0.5` vs `KXCPI-26APR-T0.50`) can't silently defeat the check; ambiguous prefixes are a hard error. Hidden `--force` flag for backfill scripts prints an audit-visible yellow warning when used; autonomous Monitor cycles MUST re-write the observation rather than bypass. 32 unit tests on the pure functions plus 8 CLI integration tests. (#614)
- Reconcile-driven position closes now write a synthetic `close` trade row and a `Trigger: Reconcile-divergence` decision-type position-note so the closed ticker remains resolvable via `known_markets` (positions ∪ candidates ∪ trades) and Caddie Master's #586 stop-loss reopen lockout query can resolve closed-ticker history. The decision-note body is carefully constructed to never mention the literal `Trigger: Stop-loss breach` phrase — even in explanatory text — because the lockout query is a substring match; including the phrase anywhere would silently lock out legitimate re-entry after broker drift. The synthetic close uses `pos.market_price or pos.avg_price` as the close price (not 0.0) so daily P&L math stays honest. `sync_positions_with_trade` excludes the caller's trade ticker so a Closer-driven close isn't double-logged; multi-ticker drift in the same sync still gets synthetic closes for the non-excluded tickers. A cross-file drift-guard test pins the trigger-name literals on both sides (`caddie-master.md` and `queries.py`). Paper-mode reconcile is out of scope for this fix and tracked in a follow-up (#623). (#609)

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
