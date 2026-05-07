# 2026-05-07 Cycle-Staleness Audit (Phase 0 of #546)

## Executive summary

- **H5: REJECTED (one-day data)** — 12 overnight cycles produced 1 trade; 1 pre-release cycle produced 0 trades. H5's prediction is inverted on this date.
- 22 cycles total: 22 full, 0 monitor, 0 errored, 0 cap-blocked.
- 2 trades placed across 2 cycles.

## Hypothesis

H5 (verbatim from #546): overnight 8 PM EDT–2 AM EDT cycles produce no actionable signal

## Methodology

- Source: `${GIMMES_HOME}/logs/cycle-*.json` parsed via `gimmes.reporting.cycle_audit.parse_cycle_log`.
- Trade ground truth: read-only SQL on the `trades` table, windowed to each cycle's start/end timestamps and excluding Scout/Caddie `skip` bookkeeping rows.
- Hour-of-window bucketing converts each cycle's start time to America/New_York and groups by EDT hour.
- Scout shortlist size and Caddie pass count are extracted from assistant text via ordered regex patterns (see `_SCOUT_PATTERNS`); regex misses produce `unknown` cells, never exceptions.

## Per-cycle audit

| cycle | start (EDT) | type | scout | caddie disp. | caddie pass | trades (db) | warnings |
|------:|-------------|------|------:|-------------:|------------:|------------:|----------|
| 1327 | 19:50 | full | 7 | 1 | 3 | 1 | text/db trade count mismatch: text=0 db=1 |
| 1328 | 20:22 | full | 6 | 2 | 5 | 0 |  |
| 1329 | 20:54 | full | 9 | 1 | 3 | 0 |  |
| 1330 | 21:23 | full | 15 | 1 | 9 | 0 |  |
| 1331 | 22:07 | full | — | 2 | 3 | 0 |  |
| 1332 | 22:42 | full | — | — | 2 | 1 |  |
| 1333 | 23:11 | full | 9 | 1 | 1 | 0 |  |
| 1334 | 23:39 | full | 3 | — | 0 | 0 |  |
| 1335 | 00:15 | full | 5 | — | 0 | 0 |  |
| 1336 | 00:43 | full | 7 | — | 0 | 0 | multiple Scout matches: [7, 7] |
| 1337 | 01:06 | full | 5 | 1 | 0 | 0 |  |
| 1338 | 01:25 | full | 13 | 3 | 2 | 0 |  |
| 1339 | 01:51 | full | 14 | — | 0 | 0 |  |
| 1340 | 02:24 | full | 6 | 1 | 0 | 0 |  |
| 1341 | 02:55 | full | 8 | — | 0 | 0 |  |
| 1342 | 03:10 | full | — | 1 | 0 | 0 |  |
| 1343 | 03:24 | full | 12 | 1 | 0 | 0 |  |
| 1344 | 03:41 | full | 10 | 1 | 0 | 0 |  |
| 1345 | 04:02 | full | 12 | — | 0 | 0 |  |
| 1346 | 04:20 | full | 12 | — | 0 | 0 |  |
| 1347 | 04:40 | full | — | 2 | 0 | 0 |  |
| 1348 | 05:08 | full | — | 3 | 3 | 0 |  |

## Aggregate by hour-of-window (EDT)

| hour (EDT) | full cycles | trades placed | trades/cycle |
|-----------:|------------:|--------------:|-------------:|
| 00:00 | 2 | 0 | 0.00 |
| 01:00 | 3 | 0 | 0.00 |
| 02:00 | 2 | 0 | 0.00 |
| 03:00 | 3 | 0 | 0.00 |
| 04:00 | 3 | 0 | 0.00 |
| 05:00 | 1 | 0 | 0.00 |
| 19:00 | 1 | 1 | 1.00 |
| 20:00 | 2 | 0 | 0.00 |
| 21:00 | 1 | 0 | 0.00 |
| 22:00 | 2 | 1 | 0.50 |
| 23:00 | 2 | 0 | 0.00 |

## Caveats

- Single day, single trade-window (jobless claims). Not generalizable to CPI/GDP/PCE windows or to Friday/Wednesday calendars.
- The `trades` table lacks an explicit `cycle_id` column; rows are attributed to a cycle by timestamp window. Risk of misattribution exists if cycles overlap; none observed on this date.
- Hour-bucket boundaries: overnight = 8 PM–2 AM EDT (hours 20, 21, 22, 23, 0, 1); pre-release = 5–9 AM EDT (hours 5, 6, 7, 8). When pre-release coverage is empty (e.g. cap-blocked sleep), any verdict on that bucket is uninformed by the audited date.
- Caddie threshold-pass count is the raw `PROCEED` token count in assistant text. A future-format wording change would silently zero this.

## Deferred follow-up

- **Phase 1** — 30-day backtest of pause and hour-of-window vs realized PnL: #553
- **Phase 2/3** — driving-range A/B + cycle-timing recommendation: #554
