"""Disk candle cache (#696): immutable settled-market windows, empty
lists as legitimate hits (negative caching), failures never cached,
and corruption degrading to fetch-through with one warning."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from gimmes.backtest.candle_cache import CandleCache
from gimmes.kalshi.historical import Candle


@pytest.fixture(autouse=True)
def _propagate_to_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backtest CLI sets propagate=False on the gimmes.backtest
    logger (it owns its handler's output); caplog captures at ROOT,
    so any CLI test running first would strand this module's warning
    assertions. Restore propagation here — order-independence, not a
    behavior change (monkeypatch undoes it per test)."""
    monkeypatch.setattr(
        logging.getLogger("gimmes.backtest"), "propagate", True,
    )


def _candle(ts: int = 1_750_000_000, price: float = 0.42) -> Candle:
    return Candle(
        end_period_ts=ts,
        yes_bid_open=price - 0.01, yes_bid_high=price,
        yes_bid_low=price - 0.03, yes_bid_close=price - 0.01,
        yes_ask_open=price + 0.01, yes_ask_high=price + 0.03,
        yes_ask_low=price, yes_ask_close=price + 0.01,
        price_open=price, price_high=price + 0.02,
        price_low=price - 0.02, price_close=price,
        volume=123,
        open_interest=456,
    )


_WINDOW = {"start_ts": 100, "end_ts": 400, "period_interval": 1440}


async def test_miss_put_hit_roundtrips_all_fields(tmp_path: Path) -> None:
    async with CandleCache(tmp_path / "c.db") as cache:
        assert await cache.get("KX1", **_WINDOW) is None
        original = [_candle(), _candle(ts=1_750_086_400, price=0.55)]
        await cache.put("KX1", candles=original, **_WINDOW)
        got = await cache.get("KX1", **_WINDOW)
    assert got == original  # dataclass equality — all 15 fields
    assert cache.hits == 1


async def test_empty_list_is_a_hit_not_a_miss(tmp_path: Path) -> None:
    """Negative caching: empty settled history is immutable and valid
    — None means miss, [] means hit."""
    async with CandleCache(tmp_path / "c.db") as cache:
        await cache.put("KXEMPTY", candles=[], **_WINDOW)
        got = await cache.get("KXEMPTY", **_WINDOW)
    assert got == []
    assert got is not None
    assert cache.hits == 1


async def test_key_discrimination(tmp_path: Path) -> None:
    async with CandleCache(tmp_path / "c.db") as cache:
        await cache.put("KX1", candles=[_candle()], **_WINDOW)
        assert await cache.get(
            "KX1", start_ts=999, end_ts=400, period_interval=1440,
        ) is None
        assert await cache.get(
            "KX1", start_ts=100, end_ts=400, period_interval=60,
        ) is None
        assert await cache.get("KX2", **_WINDOW) is None


async def test_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "c.db"
    async with CandleCache(path) as cache:
        await cache.put("KX1", candles=[_candle()], **_WINDOW)
    async with CandleCache(path) as fresh:
        got = await fresh.get("KX1", **_WINDOW)
    assert got == [_candle()]


async def test_schema_drift_drops_and_self_heals(tmp_path: Path) -> None:
    """A Candle-shape change (stale user_version fingerprint) drops
    the whole cache on open instead of misreading old rows — and the
    cache repopulates rather than degrading forever."""
    path = tmp_path / "c.db"
    async with CandleCache(path) as cache:
        await cache.put("KX1", candles=[_candle()], **_WINDOW)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 1")  # stale fingerprint
    conn.commit()
    conn.close()

    async with CandleCache(path) as fresh:
        assert await fresh.get("KX1", **_WINDOW) is None  # dropped
        await fresh.put("KX1", candles=[_candle()], **_WINDOW)
        assert await fresh.get("KX1", **_WINDOW) == [_candle()]


async def test_put_failure_degrades_not_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A mid-run write failure (disk full, dropped connection) must
    not abort the backtest — the engine calls put unguarded."""
    async with CandleCache(tmp_path / "c.db") as cache:
        async def _boom(*args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        cache._conn.execute = _boom  # type: ignore[method-assign]
        with caplog.at_level(
            logging.WARNING, logger="gimmes.backtest.candle_cache",
        ):
            await cache.put("KX1", candles=[_candle()], **_WINDOW)
            assert await cache.get("KX1", **_WINDOW) is None
    warnings = [
        r for r in caplog.records if "candle cache disabled" in r.message
    ]
    assert len(warnings) == 1


async def test_corrupt_file_degrades_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "c.db"
    path.write_bytes(b"this is not a sqlite database at all........")

    with caplog.at_level(
        logging.WARNING, logger="gimmes.backtest.candle_cache",
    ):
        async with CandleCache(path) as cache:
            assert await cache.get("KX1", **_WINDOW) is None
            await cache.put("KX1", candles=[_candle()], **_WINDOW)
            assert await cache.get("KX1", **_WINDOW) is None
    warnings = [
        r for r in caplog.records if "candle cache disabled" in r.message
    ]
    assert len(warnings) == 1  # exactly one, not per-op spam


async def test_corrupt_row_degrades_not_crashes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "c.db"
    async with CandleCache(path) as cache:
        await cache.put("KX1", candles=[_candle()], **_WINDOW)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE candles SET candles_json = 'not json' WHERE ticker='KX1'",
    )
    conn.commit()
    conn.close()

    with caplog.at_level(
        logging.WARNING, logger="gimmes.backtest.candle_cache",
    ):
        async with CandleCache(path) as fresh:
            assert await fresh.get("KX1", **_WINDOW) is None
    assert any(
        "candle cache disabled" in r.message for r in caplog.records
    )
