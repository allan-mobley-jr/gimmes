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


def _release_day_window(
    release_dt: datetime,
    close_time: time,
    open_time: time = time(4, 0),
) -> tuple[datetime, datetime]:
    """Build a release-day window opening at 04:00 ET by default.

    Per #558, all formerly-overnight windows now open at 04:00 ET on the
    release day (eliminating the 18:00–04:00 ET dead zone where 12 days of
    data showed ~0.009 trades/cycle).  Caller passes the close time on the
    release day.  Note: 04:00 ET is safely past the spring-forward 02:00→03:00
    skip; callers parameterizing ``open_time`` to 02:00–03:00 on the DST
    transition day risk a non-existent local time from ``replace()``.
    """
    open_dt = release_dt.replace(
        hour=open_time.hour, minute=open_time.minute, second=0, microsecond=0,
    )
    close_dt = release_dt.replace(
        hour=close_time.hour, minute=close_time.minute, second=0, microsecond=0,
    )
    return open_dt, close_dt


def _month_add(year: int, month: int, delta: int) -> tuple[int, int]:
    """Add *delta* months to (year, month)."""
    m = month - 1 + delta
    return year + m // 12, m % 12 + 1


# ---------------------------------------------------------------------------
# Actual release date lookup tables
# Update annually from BLS (bls.gov/schedule/news_release/cpi.htm) and
# BEA (bea.gov/news/schedule) schedules.
# ---------------------------------------------------------------------------

# CPI release dates: (year, month) -> day-of-month when CPI is released.
# Note: CPI for month M is released in month M+1, but this table is keyed
# by the month the release *falls in* (i.e., the calendar month to generate
# a window for).
_CPI_DATES: dict[tuple[int, int], int] = {
    # 2025
    (2025, 1): 15, (2025, 2): 12, (2025, 3): 12, (2025, 4): 10,
    (2025, 5): 13, (2025, 6): 11, (2025, 7): 15, (2025, 8): 12,
    (2025, 9): 10, (2025, 10): 14, (2025, 11): 12, (2025, 12): 10,
    # 2026
    (2026, 1): 13, (2026, 2): 11, (2026, 3): 11, (2026, 4): 10,
    (2026, 5): 12, (2026, 6): 10, (2026, 7): 14, (2026, 8): 12,
    (2026, 9): 16, (2026, 10): 13, (2026, 11): 10, (2026, 12): 9,
}

# GDP Advance Estimate dates: (year, month) -> day-of-month.
# Only Jan/Apr/Jul/Oct have GDP releases.
_GDP_ADVANCE_DATES: dict[tuple[int, int], int] = {
    # 2025
    (2025, 1): 30, (2025, 4): 30, (2025, 7): 30, (2025, 10): 29,
    # 2026
    (2026, 1): 29, (2026, 4): 30, (2026, 7): 30, (2026, 10): 28,
}


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
    """Weekly: Wed 4:00 AM → Wed 1:00 PM ET (release day, post-#558)."""
    windows: list[tuple[datetime, datetime]] = []
    last_day = _cal.monthrange(year, month)[1]
    for day in range(1, last_day + 1):
        dt = datetime(year, month, day, tzinfo=ET)
        if dt.weekday() == 2:  # Wednesday
            windows.append(_release_day_window(dt, time(13, 0)))
    return windows


def _jobless_claims(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Weekly: Thu 4:00 AM → Thu 8:30 AM ET (release day, post-#558)."""
    windows: list[tuple[datetime, datetime]] = []
    last_day = _cal.monthrange(year, month)[1]
    for day in range(1, last_day + 1):
        dt = datetime(year, month, day, tzinfo=ET)
        if dt.weekday() == 3:  # Thursday
            windows.append(_release_day_window(dt, time(8, 30)))
    return windows


def _adp(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Monthly: Wed before NFP 4:00 AM → 8:15 AM ET (release day, post-#558)."""
    nfp_friday = _first_friday(year, month)
    adp_wednesday = nfp_friday - timedelta(days=2)
    return [_release_day_window(adp_wednesday, time(8, 15))]


def _first_business_day(year: int, month: int) -> datetime:
    """Return the first Mon-Fri on or after the 1st of the month."""
    dt = datetime(year, month, 1, tzinfo=ET)
    while dt.weekday() >= 5:  # Skip Sat/Sun
        dt += timedelta(days=1)
    return dt


def _ism_pmi(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Monthly: 1st biz day 4:00 AM → 10:00 AM ET (release day, post-#558)."""
    biz_day = _first_business_day(year, month)
    return [_release_day_window(biz_day, time(10, 0))]


def _nfp(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Monthly: 1st Friday 4:00 AM → 8:30 AM ET (release day, post-#558)."""
    friday = _first_friday(year, month)
    return [_release_day_window(friday, time(8, 30))]


def _cpi(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Monthly: CPI release day 4:00 AM → 8:30 AM ET (post-#558)."""
    day = _CPI_DATES.get((year, month))
    if day is None:
        # Fallback: ~12th snapped to nearest business day
        target = datetime(year, month, 12, tzinfo=ET)
        release = _nearest_business_day(target)
    else:
        release = datetime(year, month, day, tzinfo=ET)
    return [_release_day_window(release, time(8, 30))]


def _core_pce(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Monthly: last Friday 4:00 AM → 8:30 AM ET (release day, post-#558)."""
    friday = _last_friday(year, month)
    return [_release_day_window(friday, time(8, 30))]


def _gdp_advance(year: int, month: int) -> list[tuple[datetime, datetime]]:
    """Quarterly (Jan/Apr/Jul/Oct): release day 4:00 AM → 8:30 AM ET (post-#558)."""
    if month not in (1, 4, 7, 10):
        return []
    day = _GDP_ADVANCE_DATES.get((year, month))
    if day is None:
        # Fallback: ~28th snapped to nearest business day
        target = datetime(year, month, 28, tzinfo=ET)
        release = _nearest_business_day(target)
    else:
        release = datetime(year, month, day, tzinfo=ET)
    return [_release_day_window(release, time(8, 30))]


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


def position_window(
    close_time: datetime,
    hours_before: float = 18.0,
) -> tuple[datetime, datetime]:
    """Build an ad-hoc trade window ending at *close_time*.

    Opens *hours_before* hours prior to close_time.  Used for
    position-aware windows when settlement doesn't fall within
    any scheduled release window.
    """
    close_et = close_time.astimezone(ET)
    open_et = close_et - timedelta(hours=hours_before)
    return open_et, close_et
