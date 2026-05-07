# Phase 1a Pause Backtest (#556, parent #546)

## Executive summary

- **Recommendation: WAIT FOR #553** — 12% of placed trades have gaps under 300s — too borderline to recommend a change without the PnL counterfactual from #553. Hold pause_seconds at the current default.
- 17 placed trades audited (2026-04-13 → 2026-05-07); 494 cycle logs surveyed.

## Methodology

- Data: `~/.gimmes/logs/cycle-*.json` parsed via `gimmes.reporting.cycle_audit.parse_cycle_log`; `~/.gimmes/gimmes.db` opened read-only.
- Trade selection: `action='open'` only (entries, not exits). Excludes `skip` bookkeeping rows.
- First-seen lookup: `MIN(scanned_at)` from `candidates` for the trade's ticker, bounded above by the trade's `timestamp`.
- Hour bucketing converts UTC `start_time` to America/New_York; EDT hour-of-day is the bucket key.
- Gap buckets: lower-inclusive, upper-exclusive. `30min+` is open-ended.

## Hour-of-window

| hour (EDT) | days observed | full cycles | trades placed | trades/cycle |
|-----------:|--------------:|------------:|--------------:|-------------:|
| 00:00 | 11 | 20 | 0 | 0.00 |
| 01:00 | 7 | 18 | 0 | 0.00 |
| 02:00 | 10 | 18 | 1 | 0.06 |
| 03:00 | 12 | 24 | 0 | 0.00 |
| 04:00 | 8 | 20 | 0 | 0.00 |
| 05:00 | 6 | 15 | 0 | 0.00 |
| 06:00 | 8 | 17 | 0 | 0.00 |
| 07:00 | 7 | 17 | 0 | 0.00 |
| 08:00 | 7 | 14 | 2 | 0.14 |
| 09:00 | 10 | 12 | 2 | 0.17 |
| 10:00 | 14 | 24 | 3 | 0.12 |
| 11:00 | 9 | 17 | 1 | 0.06 |
| 12:00 | 11 | 21 | 0 | 0.00 |
| 13:00 | 11 | 12 | 1 | 0.08 |
| 14:00 | 11 | 33 | 4 | 0.12 |
| 15:00 | 12 | 44 | 3 | 0.07 |
| 16:00 | 12 | 25 | 1 | 0.04 |
| 17:00 | 14 | 27 | 2 | 0.07 |
| 18:00 | 12 | 26 | 1 | 0.04 |
| 19:00 | 10 | 21 | 1 | 0.05 |
| 20:00 | 11 | 20 | 1 | 0.05 |
| 21:00 | 10 | 16 | 1 | 0.06 |
| 22:00 | 7 | 16 | 1 | 0.06 |
| 23:00 | 9 | 17 | 1 | 0.06 |

## Gap distribution (first-seen → trade-placed)

Out of 17 placed trades with a known first-seen time:

| bucket | trades | % of placed |
|--------|------:|------------:|
| 0-60s | 0 | 0.0% |
| 60-300s | 2 | 11.8% |
| 5-10min | 0 | 0.0% |
| 10-30min | 3 | 17.6% |
| 30min+ | 12 | 70.6% |

Interpretation: a trade whose first-seen-to-placed gap is **under X seconds** would have been **missed** by a re-scan cadence of X seconds — the candidate appeared and was traded between scans. The real loop's effective cadence is dominated by cycle wall time (15–30 min per cycle), so `pause_seconds` is a lower bound on the actual rescan interval.

## Per-trade detail

| ticker | trade time (EDT) | first seen (EDT) | gap | hour (EDT) | window | gimme | edge |
|--------|------------------|------------------|----:|-----------:|--------|------:|-----:|
| KXCPIYOY-26MAY-T3.8 | 04-16 15:08 | 04-16 07:25 | 27799s | 15:00 | Index contracts | 80 | 0.29 |
| KXCPICORE-26APR-T0.3 | 04-21 14:13 | 04-13 17:09 | 680681s | 14:00 | Index contracts | 68 | 0.36 |
| KXJOBLESSCLAIMS-26APR23-215000 | 04-21 14:40 | 04-20 23:23 | 55000s | 14:00 | Index contracts | 75 | 0.07 |
| KXADP-26APR-T100000 | 04-21 15:21 | 04-14 14:35 | 607556s | 15:00 | Index contracts | 73 | 0.09 |
| KXCPI-26APR-T0.5 | 04-22 10:30 | 04-14 14:14 | 677722s | 10:00 | Treasury notes | 58 | 0.13 |
| KXINX-26APR24H1600-B7137 | 04-22 22:54 | 04-22 22:20 | 2058s | 22:00 | Jobless claims | 75 | 0.14 |
| KXUE-CAN26APR-6.6 | 04-23 02:16 | 04-23 01:58 | 1112s | 02:00 | Jobless claims | 67 | 0.14 |
| KXADP-26APR-T100000 | 04-24 15:26 | 04-14 14:35 | 867043s | 15:00 | Index contracts | 73 | 0.09 |
| KXPCECORE-26APR-T0.3 | 04-28 15:22 | 04-20 13:40 | 697361s | 15:00 | Index contracts | 48 | -0.22 |
| KXCPIYOY-26MAY-T4.1 | 04-28 18:27 | 04-28 18:24 | 168s | 18:00 | ADP | 84 | 0.27 |
| KXGDP-26APR30-T2.5 | 04-29 09:13 | 04-29 08:54 | 1149s | 09:00 | Treasury notes | 70 | 0.06 |
| KXGDP-26APR30-T3.0 | 04-29 09:29 | 04-29 08:53 | 2110s | 09:00 | Treasury notes | 80 | 0.07 |
| KXCPIYOY-26APR-T3.7 | 04-29 11:55 | 04-14 14:14 | 1287654s | 11:00 | Treasury notes | 68 | 0.16 |
| KXCPIYOY-26JUN-T3.9 | 04-29 16:44 | 04-29 16:42 | 158s | 16:00 | outside | 72 | 0.24 |
| KXCPIYOY-26MAY-T4.2 | 04-29 21:53 | 04-29 21:36 | 1048s | 21:00 | GDP Advance | 81 | 0.23 |
| KXCPI-26APR-T0.5 | 05-06 20:18 | 04-14 14:14 | 1922627s | 20:00 | Jobless claims | 58 | 0.13 |
| KXU3-26APR-T4.3 | 05-06 23:07 | 04-23 15:51 | 1149321s | 23:00 | Jobless claims | 72 | 0.05 |

## By trade window

| release window | trades |
|----------------|------:|
| ADP | 1 |
| GDP Advance | 1 |
| Index contracts | 6 |
| Jobless claims | 4 |
| Treasury notes | 4 |
| outside | 1 |

## Caveats

- **Cross-model basis**: Apr cycles ran on Opus 4.7; May cycles on Sonnet 4.6 (post-#549). Trades placed are objectively the same, but the cost-per-cycle differs — interpret budget-related conclusions with care.
- **Cycle cadence vs `pause_seconds`**: each cycle takes ~15-30 min wall time; `pause_seconds` only adds inter-cycle delay. The gap distribution measures first-seen to trade-placed wall time regardless of where that time was spent.
- **Small-N for some hour buckets**: a single overnight EDT hour may have only one or two trades. Treat 0/1-trade rows as anecdote, not signal.
- **No PnL**: this analysis says nothing about *whether the trades we placed were profitable*, only about *how a longer pause would have affected which trades got placed*. PnL counterfactual is #553 (Phase 1b).

## Recommendation

12% of placed trades have gaps under 300s — too borderline to recommend a change without the PnL counterfactual from #553. Hold pause_seconds at the current default.

PnL counterfactual deferred to #553 (needs Kalshi historical orderbook data not currently persisted).
