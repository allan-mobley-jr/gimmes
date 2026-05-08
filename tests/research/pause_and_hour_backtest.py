"""Phase 1 backtest scaffolding for #553 (parent: #546).

Computes two stratified tables from the live database and cycle logs:
  1. Pause-length vs realized PnL — for each closed trade, simulates whether
     it would still have been surfaced under longer cycle-pause settings.
  2. Hour-of-window vs realized PnL — buckets each closed trade by hours
     elapsed since the trade window opened.

This is a scaffolding deliverable: AC requires 30 days of post-#549 cycle
logs (~ready 2026-06-06). Today's run executes against whatever data
exists and prints HONEST caveats about statistical sufficiency. Re-run
the same command after more data accumulates; the tables will fill in.

Run from the repo root:

    uv run python tests/research/pause_and_hour_backtest.py

Writes summary to stdout and detailed CSV to ``tests/research/output/``.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from gimmes.strategy.calendar import ET, is_in_trade_window  # noqa: E402

DB_PATH = Path.home() / ".gimmes" / "gimmes.db"
LOOKBACK_DAYS = 30
PAUSE_BUCKETS_S = (60, 120, 300, 600, 900)
OUT_DIR = Path(__file__).resolve().parent / "output"


def _load_trades(db_path: Path, since: datetime) -> list[dict]:
    """Return open/close/size_up rows newer than ``since``, oldest first."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT timestamp, ticker, action, side, count, price
        FROM trades
        WHERE action IN ('open', 'close', 'size_up')
          AND timestamp >= ?
        ORDER BY timestamp
        """,
        (since.isoformat(),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _load_candidates(db_path: Path, since: datetime) -> list[dict]:
    """Return candidate scans newer than ``since``, oldest first."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT scanned_at, ticker, market_price, edge, gimme_score
        FROM candidates
        WHERE scanned_at >= ?
        ORDER BY scanned_at
        """,
        (since.isoformat(),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _parse_dt(s: str) -> datetime:
    """Parse ISO timestamp; ensure timezone-aware (assume UTC if naive)."""
    from datetime import UTC
    s = s.replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _pair_trades(trades: list[dict]) -> list[dict]:
    """Match closes to their preceding opens per (ticker, side); compute realized PnL.

    Uses the same weighted-average logic as ``calculate_pnl`` (#561) so the
    backtest's PnL numbers reconcile with ``gimmes report``.
    """
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        by_key[(t["ticker"], t["side"])].append(t)

    closed = []
    for (ticker, side), events in by_key.items():
        events.sort(key=lambda e: e["timestamp"])
        remaining = 0
        avg_cost = 0.0
        first_open_ts: str | None = None
        for e in events:
            count = int(e["count"] or 0)
            price = float(e["price"] or 0.0)
            if e["action"] in ("open", "size_up"):
                if count <= 0:
                    continue
                if remaining == 0:
                    first_open_ts = e["timestamp"]
                total = remaining + count
                avg_cost = (
                    (avg_cost * remaining + price * count) / total
                    if total
                    else 0.0
                )
                remaining = total
            elif e["action"] == "close":
                if count <= 0 or remaining <= 0:
                    continue
                matched = min(count, remaining)
                pnl = (price - avg_cost) * matched
                closed.append({
                    "ticker": ticker,
                    "side": side,
                    "open_ts": first_open_ts,
                    "close_ts": e["timestamp"],
                    "open_price": avg_cost,
                    "close_price": price,
                    "count": matched,
                    "pnl": pnl,
                })
                remaining -= matched
                if remaining == 0:
                    first_open_ts = None
    return closed


def _candidate_first_seen(
    candidates: list[dict], ticker: str, before: datetime,
) -> datetime | None:
    """Earliest scan timestamp that surfaced ``ticker`` before ``before``."""
    earliest: datetime | None = None
    for c in candidates:
        if c["ticker"] != ticker:
            continue
        ts = _parse_dt(c["scanned_at"])
        if ts > before:
            continue
        if earliest is None or ts < earliest:
            earliest = ts
    return earliest


def _hour_of_window(open_ts: str) -> int | None:
    """Hours since the active trade window opened, or None if not in window."""
    dt = _parse_dt(open_ts).astimezone(ET)
    in_w, _name, _secs = is_in_trade_window(dt)
    if not in_w:
        return None
    # Walk back hour by hour to find when the window opened.
    for h in range(0, 24):
        check = dt - timedelta(hours=h + 1)
        in_check, _, _ = is_in_trade_window(check)
        if not in_check:
            return h
    return None


def _pause_bucket_for_trade(
    closed: dict, candidates: list[dict], pause_s: int,
) -> bool:
    """Would this trade still have been surfaced under ``pause_s`` between cycles?

    Approximation: the trade was placed at ``open_ts``. Find the latest
    candidate scan for this ticker before ``open_ts``. If the gap from that
    scan to ``open_ts`` exceeds ``pause_s``, the simulated longer pause
    would not have surfaced the candidate in time. (Counterfactual
    price-trajectory data is not persisted, so we can't model "missed by
    one cycle" entry-price drift — see research blockers.)
    """
    open_dt = _parse_dt(closed["open_ts"])
    seen = _candidate_first_seen(candidates, closed["ticker"], open_dt)
    if seen is None:
        return True  # No scan record — assume the trade still happens
    gap = (open_dt - seen).total_seconds()
    return gap <= pause_s


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cutoff = datetime.now().astimezone() - timedelta(days=LOOKBACK_DAYS)
    cutoff_iso = cutoff.isoformat()

    trades = _load_trades(DB_PATH, cutoff)
    candidates = _load_candidates(DB_PATH, cutoff)
    closed = _pair_trades(trades)

    print(f"=== Phase 1 backtest — {LOOKBACK_DAYS}-day window ===")
    print(f"Cutoff: {cutoff_iso}")
    print(f"Trade events in window: {len(trades)}")
    print(f"Candidate scans in window: {len(candidates)}")
    print(f"Closed positions in window: {len(closed)}")
    print()

    # ----- Table 1: pause-length vs realized PnL -----
    print("--- Table 1: pause-length vs realized PnL ---")
    print(f"{'pause_s':<10} {'trades_surfaced':<18} {'gross_pnl':<12}")
    table1: list[dict] = []
    for pause_s in PAUSE_BUCKETS_S:
        surfaced = [c for c in closed if _pause_bucket_for_trade(c, candidates, pause_s)]
        pnl = sum(c["pnl"] for c in surfaced)
        print(f"{pause_s:<10} {len(surfaced):<18} ${pnl:<11.2f}")
        table1.append({
            "pause_s": pause_s,
            "trades_surfaced": len(surfaced),
            "gross_pnl": pnl,
        })
    print()

    # ----- Table 2: hour-of-window vs realized PnL -----
    print("--- Table 2: hour-of-window vs realized PnL ---")
    by_hour: dict[int, list[dict]] = defaultdict(list)
    out_of_window: list[dict] = []
    for c in closed:
        h = _hour_of_window(c["open_ts"])
        if h is None:
            out_of_window.append(c)
        else:
            by_hour[h].append(c)
    print(f"{'hour_of_window':<16} {'count':<8} {'gross_pnl':<12}")
    table2: list[dict] = []
    for h in sorted(by_hour):
        cnt = len(by_hour[h])
        pnl = sum(c["pnl"] for c in by_hour[h])
        print(f"h+{h:<14} {cnt:<8} ${pnl:<11.2f}")
        table2.append({"hour_of_window": h, "count": cnt, "gross_pnl": pnl})
    if out_of_window:
        oow_pnl = sum(c["pnl"] for c in out_of_window)
        print(f"{'(out of window)':<16} {len(out_of_window):<8} ${oow_pnl:.2f}")
    print()

    # ----- Persist CSVs -----
    import csv
    with (OUT_DIR / "pause_vs_pnl.csv").open("w") as f:
        w = csv.DictWriter(f, fieldnames=["pause_s", "trades_surfaced", "gross_pnl"])
        w.writeheader()
        w.writerows(table1)
    with (OUT_DIR / "hour_vs_pnl.csv").open("w") as f:
        w = csv.DictWriter(f, fieldnames=["hour_of_window", "count", "gross_pnl"])
        w.writeheader()
        w.writerows(table2)
    with (OUT_DIR / "closed_trades.csv").open("w") as f:
        w = csv.DictWriter(f, fieldnames=[
            "ticker", "side", "open_ts", "close_ts",
            "open_price", "close_price", "count", "pnl",
        ])
        w.writeheader()
        w.writerows(closed)

    # ----- Sufficiency caveats -----
    print("--- Sufficiency caveats ---")
    if len(closed) < 30:
        print(
            f"  N={len(closed)} closes is statistically insufficient for"
            f" pause-bucket inference. Re-run after additional data accrues."
        )
    if len(closed) > 0 and len(closed) < 8:
        print("  Single-trade buckets dominate; treat tables as illustrative.")
    print(
        "  Pause-vs-PnL counterfactual cannot model entry-price drift —"
        " mid-price trajectories between cycles are not persisted."
    )
    print(
        "  Hour-of-window alpha localization needs N>=30 per bucket to be"
        " meaningful; some buckets will show zero coverage indefinitely if"
        " trade windows happen to be sparse during those hours."
    )


if __name__ == "__main__":
    main()
