"""Trade window calendar — schedule cycles around data releases."""

from __future__ import annotations

import calendar as _cal
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _first_friday(year: int, month: int) -> datetime:
    """Return the first Friday of the given month in ET."""
    for day in range(1, 8):
        dt = datetime(year, month, day, tzinfo=ET)
        if dt.weekday() == 4:  # Friday
            return dt
    raise AssertionError("unreachable")


def _last_friday(year: int, month: int) -> datetime:
    """Return the last Friday of the given month in ET."""
    last_day = _cal.monthrange(year, month)[1]
    for day in range(last_day, last_day - 7, -1):
        dt = datetime(year, month, day, tzinfo=ET)
        if dt.weekday() == 4:
            return dt
    raise AssertionError("unreachable")


def _nearest_business_day(dt: datetime) -> datetime:
    """Snap to nearest business day: Sat→Fri, Sun→Mon."""
    wd = dt.weekday()
    if wd == 5:  # Saturday
        return dt - timedelta(days=1)
    if wd == 6:  # Sunday
        return dt + timedelta(days=1)
    return dt


def _overnight_window(
    release_date: datetime,
    open_time: time,
    close_time: time,
) -> tuple[datetime, datetime]:
    """Build an overnight window: open evening before, close on release day."""
    day_before = release_date - timedelta(days=1)
    open_dt = day_before.replace(
        hour=open_time.hour, minute=open_time.minute, second=0, microsecond=0,
    )
    close_dt = release_date.replace(
        hour=close_time.hour, minute=close_time.minute, second=0, microsecond=0,
    )
    return open_dt, close_dt


def _month_add(year: int, month: int, delta: int) -> tuple[int, int]:
    """Add *delta* months to (year, month)."""
    m = month - 1 + delta
    return year + m // 12, m % 12 + 1


# ---------------------------------------------------------------------------
# Window compute functions — each returns [(open_dt, close_dt), ...] for a
# given (year, month).  All datetimes are timezone-aware in US/Eastern.
# ---------------------------------------------------------------------------

def _index_contracts(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Daily equity index window: 2:00–4:00 PM ET on weekdays."""
    windows: list[tuple[datetime, datetime]] = []
    last_day = _cal.monthrange(year, month)[1]
    for day in range(1, last_day + 1):
        dt = datetime(year, month, day, tzinfo=ET)
        if dt.weekday() < 5:  # Mon-Fri
            windows.append((
                dt.replace(hour=14, minute=0, second=0, microsecond=0),
                dt.replace(hour=16, minute=0, second=0, microsecond=0),
            ))
    return windows


def _treasury_notes(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Weekly: Tue 11:00 PM → Wed 1:00 PM ET."""
    windows: list[tuple[datetime, datetime]] = []
    last_day = _cal.monthrange(year, month)[1]
    for day in range(1, last_day + 1):
        dt = datetime(year, month, day, tzinfo=ET)
        if dt.weekday() == 1:  # Tuesday
            open_dt = dt.replace(hour=23, minute=0, second=0, microsecond=0)
            close_dt = (dt + timedelta(days=1)).replace(
                hour=13, minute=0, second=0, microsecond=0,
            )
            windows.append((open_dt, close_dt))
    return windows


def _jobless_claims(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Weekly: Wed 6:30 PM → Thu 8:30 AM ET."""
    windows: list[tuple[datetime, datetime]] = []
    last_day = _cal.monthrange(year, month)[1]
    for day in range(1, last_day + 1):
        dt = datetime(year, month, day, tzinfo=ET)
        if dt.weekday() == 2:  # Wednesday
            open_dt = dt.replace(hour=18, minute=30, second=0, microsecond=0)
            close_dt = (dt + timedelta(days=1)).replace(
                hour=8, minute=30, second=0, microsecond=0,
            )
            windows.append((open_dt, close_dt))
    return windows


def _adp(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Monthly: Tue before NFP 6:15 PM → Wed 8:15 AM ET."""
    nfp_friday = _first_friday(year, month)
    adp_wednesday = nfp_friday - timedelta(days=2)  # Wednesday before NFP
    adp_tuesday = adp_wednesday - timedelta(days=1)
    open_dt = adp_tuesday.replace(hour=18, minute=15, second=0, microsecond=0)
    close_dt = adp_wednesday.replace(hour=8, minute=15, second=0, microsecond=0)
    return [(open_dt, close_dt)]


def _first_business_day(year: int, month: int) -> datetime:
    """Return the first Mon-Fri on or after the 1st of the month."""
    dt = datetime(year, month, 1, tzinfo=ET)
    while dt.weekday() >= 5:  # Skip Sat/Sun
        dt += timedelta(days=1)
    return dt


def _ism_pmi(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Monthly: night before 1st business day 8:00 PM → 1st biz day 10:00 AM."""
    biz_day = _first_business_day(year, month)
    day_before = biz_day - timedelta(days=1)
    open_dt = day_before.replace(hour=20, minute=0, second=0, microsecond=0)
    close_dt = biz_day.replace(hour=10, minute=0, second=0, microsecond=0)
    return [(open_dt, close_dt)]


def _nfp(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Monthly: Thu 6:30 PM → 1st Friday 8:30 AM ET."""
    friday = _first_friday(year, month)
    return [_overnight_window(friday, time(18, 30), time(8, 30))]


def _cpi(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Monthly: night before ~12th 6:30 PM → ~12th 8:30 AM ET."""
    target = datetime(year, month, 12, tzinfo=ET)
    release = _nearest_business_day(target)
    return [_overnight_window(release, time(18, 30), time(8, 30))]


def _core_pce(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Monthly: night before last Friday 6:30 PM → last Fri 8:30 AM ET."""
    friday = _last_friday(year, month)
    return [_overnight_window(friday, time(18, 30), time(8, 30))]


def _gdp_advance(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Quarterly (Jan/Apr/Jul/Oct): night before ~28th 6:30 PM → ~28th 8:30 AM."""
    if month not in (1, 4, 7, 10):
        return []
    target = datetime(year, month, 28, tzinfo=ET)
    release = _nearest_business_day(target)
    return [_overnight_window(release, time(18, 30), time(8, 30))]


# ---------------------------------------------------------------------------
# Window registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TradeWindow:
    name: str
    compute: Callable[[int, int], list[tuple[datetime, datetime]]]


WINDOWS: tuple[TradeWindow, ...] = (
    TradeWindow("Index contracts", _index_contracts),
    TradeWindow("Treasury notes", _treasury_notes),
    TradeWindow("Jobless claims", _jobless_claims),
    TradeWindow("ADP", _adp),
    TradeWindow("ISM PMI", _ism_pmi),
    TradeWindow("Non-Farm Payrolls", _nfp),
    TradeWindow("CPI", _cpi),
    TradeWindow("Core PCE", _core_pce),
    TradeWindow("GDP Advance", _gdp_advance),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _generate_windows(dt: datetime) -> list[tuple[datetime, datetime, str]]:
    """Generate all trade windows across a 3-month span around *dt*."""
    seen: set[tuple[str, str]] = set()
    results: list[tuple[datetime, datetime, str]] = []
    dt_et = dt.astimezone(ET)
    y, m = dt_et.year, dt_et.month
    for delta in (-1, 0, 1):
        ym = _month_add(y, m, delta)
        for w in WINDOWS:
            for open_dt, close_dt in w.compute(*ym):
                key = (w.name, open_dt.isoformat())
                if key not in seen:
                    seen.add(key)
                    results.append((open_dt, close_dt, w.name))
    return results


def is_in_trade_window(
    dt: datetime | None = None,
) -> tuple[bool, str | None, int | None]:
    """Check if *dt* falls inside any trade window.

    Returns:
        (in_window, release_name, seconds_until_close)
        If not in window: (False, None, None)
    """
    if dt is None:
        dt = datetime.now(ET)

    active: list[tuple[int, str]] = []
    for open_dt, close_dt, name in _generate_windows(dt):
        if open_dt <= dt < close_dt:
            secs = int((close_dt - dt).total_seconds())
            active.append((secs, name))

    if not active:
        return False, None, None

    # Return window closing soonest
    active.sort()
    return True, active[0][1], active[0][0]


def next_trade_window(
    dt: datetime | None = None,
) -> tuple[datetime, str]:
    """Return (window_start, release_name) for the next upcoming window."""
    if dt is None:
        dt = datetime.now(ET)

    upcoming: list[tuple[datetime, str]] = []
    for open_dt, _close_dt, name in _generate_windows(dt):
        if open_dt > dt:
            upcoming.append((open_dt, name))

    if not upcoming:
        # Extend search to +2 months
        y, m = _month_add(dt.year, dt.month, 2)
        for w in WINDOWS:
            for open_dt, _close_dt in w.compute(y, m):
                if open_dt > dt:
                    upcoming.append((open_dt, w.name))

    if not upcoming:
        # Fallback: next Monday index window
        dt_et = dt.astimezone(ET)
        days_ahead = (7 - dt_et.weekday()) % 7 or 7
        monday = dt_et + timedelta(days=days_ahead)
        return (
            monday.replace(hour=14, minute=0, second=0, microsecond=0),
            "Index contracts",
        )
    upcoming.sort()
    return upcoming[0]


def seconds_until_next_window(dt: datetime | None = None) -> int:
    """Seconds from *dt* until the next trade window opens (minimum 1)."""
    if dt is None:
        dt = datetime.now(ET)
    start, _name = next_trade_window(dt)
    return max(1, math.ceil((start - dt).total_seconds()))
