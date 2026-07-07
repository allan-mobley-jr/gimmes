"""On-disk candle cache for backtest reruns (#696).

Settled-market candle history is immutable, so cached windows never
invalidate: keys are (ticker, start_ts, end_ts, period_interval), and
each window derives from the market's fixed close_time — parameter
sweeps hit 100% warm. Successful fetches are cached INCLUDING empty
lists (negative caching is what makes reruns fast); failures are never
cached, preserving the #655 fetch_failures visibility (a systemic 404
must keep failing loudly, not become a cached miss).

Failure doctrine: any cache error degrades to fetch-through with ONE
warning per run and never aborts the backtest. A corrupt cache file is
recoverable by deleting ~/.gimmes/backtest_cache.db.
"""

from __future__ import annotations

import json
import logging
import time
import zlib
from dataclasses import asdict
from pathlib import Path

import aiosqlite

from gimmes.kalshi.historical import Candle

logger = logging.getLogger(__name__)

def _schema_fingerprint() -> int:
    """31-bit fingerprint of the Candle field set, stamped into
    ``PRAGMA user_version``. Any Candle evolution changes it, and a
    mismatched cache is dropped wholesale on open — otherwise a field
    added WITH a default would deserialize old rows cleanly with
    stale/default values (silently diverging cached runs from
    --no-cache runs), and a removed field would degrade every future
    run until the user manually deleted the DB (review-found)."""
    names = ",".join(sorted(Candle.__dataclass_fields__))
    return zlib.crc32(names.encode()) & 0x7FFFFFFF


_SCHEMA = """CREATE TABLE IF NOT EXISTS candles (
    ticker TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    period_interval INTEGER NOT NULL,
    candles_json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (ticker, start_ts, end_ts, period_interval)
)"""


class CandleCache:
    """Async SQLite-backed cache; ``get`` returns None on MISS and a
    list (possibly empty — a legitimate hit) on HIT."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._disabled = False
        self.hits = 0

    async def open(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(self.db_path)
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA busy_timeout=5000")
            fingerprint = _schema_fingerprint()
            async with self._conn.execute(
                "PRAGMA user_version",
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or row[0] != fingerprint:
                # Candle changed shape (or the DB is brand new):
                # settled history is refetchable, so drop everything
                # rather than risk misreading old rows.
                await self._conn.execute("DROP TABLE IF EXISTS candles")
                await self._conn.execute(
                    f"PRAGMA user_version = {fingerprint}",
                )
            await self._conn.execute(_SCHEMA)
            await self._conn.commit()
        except Exception as exc:  # noqa: BLE001 — degrade, never abort
            self._degrade(exc)

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    async def __aenter__(self) -> CandleCache:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def get(
        self, ticker: str, *, start_ts: int, end_ts: int,
        period_interval: int,
    ) -> list[Candle] | None:
        """Cached candles for the window, or None on miss/disabled."""
        if self._disabled or self._conn is None:
            return None
        try:
            async with self._conn.execute(
                """SELECT candles_json FROM candles
                   WHERE ticker = ? AND start_ts = ? AND end_ts = ?
                     AND period_interval = ?""",
                (ticker, start_ts, end_ts, period_interval),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            candles = [Candle(**d) for d in json.loads(row[0])]
        except Exception as exc:  # noqa: BLE001 — corrupt row/file
            self._degrade(exc)
            return None
        self.hits += 1
        return candles

    async def put(
        self, ticker: str, *, start_ts: int, end_ts: int,
        period_interval: int, candles: list[Candle],
    ) -> None:
        """Write-through a SUCCESSFUL fetch (empty lists included —
        immutable negative caching). Callers must never route
        failures here."""
        if self._disabled or self._conn is None:
            return
        try:
            await self._conn.execute(
                """INSERT OR REPLACE INTO candles
                   (ticker, start_ts, end_ts, period_interval,
                    candles_json, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    ticker, start_ts, end_ts, period_interval,
                    json.dumps([asdict(c) for c in candles]),
                    int(time.time()),
                ),
            )
            await self._conn.commit()
        except Exception as exc:  # noqa: BLE001
            self._degrade(exc)

    def _degrade(self, exc: Exception) -> None:
        if not self._disabled:
            logger.warning(
                "candle cache disabled (%s: %s) — fetching through"
                " for the rest of this run (#696)",
                type(exc).__name__, exc,
            )
        self._disabled = True
