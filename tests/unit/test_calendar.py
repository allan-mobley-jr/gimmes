"""Unit tests for trade window calendar."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from gimmes.strategy.calendar import (
    _cpi,
    _gdp_advance,
    is_in_trade_window,
    next_trade_window,
    position_window,
    seconds_until_next_window,
)

ET = ZoneInfo("America/New_York")


class TestIndexContracts:
    def test_weekday_3pm_in_window(self) -> None:
        # Wednesday 3:00 PM ET
        dt = datetime(2026, 4, 1, 15, 0, tzinfo=ET)
        in_w, name, secs = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Index contracts"
        assert secs == 3600  # 1 hour until 4pm

    def test_weekday_1pm_not_in_window(self) -> None:
        dt = datetime(2026, 4, 1, 13, 0, tzinfo=ET)
        in_w, _, _ = is_in_trade_window(dt)
        # Could be in another window, but check index doesn't trigger at 1pm
        # by checking a day with no other windows
        dt = datetime(2026, 4, 6, 13, 0, tzinfo=ET)  # Monday, no other windows
        in_w, name, _ = is_in_trade_window(dt)
        assert not in_w or name != "Index contracts"

    def test_saturday_3pm_not_in_window(self) -> None:
        # Saturday 3:00 PM ET — no index window
        dt = datetime(2026, 4, 4, 15, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert not in_w or name != "Index contracts"

    def test_boundary_2pm_in(self) -> None:
        dt = datetime(2026, 4, 1, 14, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Index contracts"

    def test_boundary_4pm_out(self) -> None:
        # 4:00 PM is the close — should be out
        dt = datetime(2026, 4, 6, 16, 0, tzinfo=ET)  # Monday
        in_w, name, _ = is_in_trade_window(dt)
        assert not in_w or name != "Index contracts"


class TestJoblessClaims:
    def test_wednesday_evening_in_window(self) -> None:
        # Wednesday April 1 2026, 7:00 PM ET
        dt = datetime(2026, 4, 1, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Jobless claims"

    def test_thursday_morning_in_window(self) -> None:
        # Thursday April 2, 8:00 AM ET
        dt = datetime(2026, 4, 2, 8, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Jobless claims"

    def test_thursday_after_release_out(self) -> None:
        # Thursday April 2, 9:00 AM ET — after release
        dt = datetime(2026, 4, 2, 9, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert not in_w or name != "Jobless claims"


class TestTreasuryNotes:
    def test_tuesday_midnight_in_window(self) -> None:
        # Tuesday April 7 2026 at 11:30 PM ET (no ADP overlap)
        dt = datetime(2026, 4, 7, 23, 30, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Treasury notes"

    def test_wednesday_noon_in_window(self) -> None:
        # Wednesday April 1, 12:00 PM ET
        dt = datetime(2026, 4, 1, 12, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Treasury notes"


class TestNFP:
    def test_thursday_before_first_friday(self) -> None:
        # April 2026: first Friday is April 3
        # Thursday April 2, 7:00 PM ET
        dt = datetime(2026, 4, 2, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Non-Farm Payrolls"

    def test_first_friday_morning(self) -> None:
        # Friday April 3, 8:00 AM ET
        dt = datetime(2026, 4, 3, 8, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Non-Farm Payrolls"


class TestCPI:
    def test_april_2026_actual_date(self) -> None:
        # April 2026 CPI releases on April 10 (Friday)
        # Window: Thursday April 9 6:30 PM → Friday April 10 8:30 AM
        dt = datetime(2026, 4, 9, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "CPI"

    def test_april_2026_release_morning(self) -> None:
        # Friday April 10 at 8:00 AM
        dt = datetime(2026, 4, 10, 8, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "CPI"

    def test_old_12th_not_cpi_for_april_2026(self) -> None:
        # The old hardcoded 12th should NOT be a CPI window for April 2026
        dt = datetime(2026, 4, 12, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert not in_w or name != "CPI"

    def test_fallback_for_unknown_year(self) -> None:
        # 2028 is not in lookup table — falls back to ~12th heuristic
        windows = _cpi(2028, 3)
        assert len(windows) == 1
        open_dt, close_dt = windows[0]
        assert 10 <= open_dt.day <= 14  # near 12th


class TestCorePCE:
    def test_night_before_last_friday(self) -> None:
        # April 2026: last Friday is April 24
        # Window opens Thursday April 23 6:30 PM
        dt = datetime(2026, 4, 23, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Core PCE"


class TestGDPAdvance:
    def test_january_2026_actual_date(self) -> None:
        # January 2026 GDP releases on Jan 29 (Thursday)
        # Window: Wed Jan 28 6:30 PM → Thu Jan 29 8:30 AM
        dt = datetime(2026, 1, 28, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "GDP Advance"

    def test_april_2026_actual_date(self) -> None:
        # April 2026 GDP releases on Apr 30 (Thursday)
        # Window: Wed Apr 29 6:30 PM → Thu Apr 30 8:30 AM
        dt = datetime(2026, 4, 29, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "GDP Advance"

    def test_february_has_no_window(self) -> None:
        dt = datetime(2026, 2, 27, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert not in_w or name != "GDP Advance"

    def test_fallback_for_unknown_year(self) -> None:
        # 2028 is not in lookup table — falls back to ~28th
        windows = _gdp_advance(2028, 4)
        assert len(windows) == 1
        open_dt, close_dt = windows[0]
        assert 26 <= open_dt.day <= 29  # near 28th


class TestISMPMI:
    def test_first_business_day_morning(self) -> None:
        # April 2026: 1st is Wednesday (business day)
        # Window opens Tue March 31 8:00 PM, closes Wed April 1 10:00 AM
        dt = datetime(2026, 4, 1, 9, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "ISM PMI"


class TestADP:
    def test_tuesday_before_nfp(self) -> None:
        # April 2026: NFP Friday is April 3, ADP Wednesday is April 1
        # ADP Tuesday is March 31 — opens 6:15 PM
        # The ADP window for that April release opens on March 31;
        # _adp(2026, 4) returns a window whose start falls in the prior month
        dt = datetime(2026, 3, 31, 18, 30, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "ADP"


class TestNextTradeWindow:
    def test_sunday_next_is_monday_index(self) -> None:
        # Sunday April 5 2026 at noon
        dt = datetime(2026, 4, 5, 12, 0, tzinfo=ET)
        start, name = next_trade_window(dt)
        assert name == "Index contracts"
        assert start.weekday() == 0  # Monday
        assert start.hour == 14

    def test_after_index_close_next_is_claims_or_tomorrow(self) -> None:
        # Wednesday April 1 at 4:01 PM — just after index close
        dt = datetime(2026, 4, 1, 16, 1, tzinfo=ET)
        start, name = next_trade_window(dt)
        # Next should be jobless claims (Wed 6:30 PM) — same day
        assert name == "Jobless claims"
        assert start.hour == 18
        assert start.minute == 30


class TestSecondsUntilNextWindow:
    def test_basic_arithmetic(self) -> None:
        # Sunday April 5 at noon → Monday index at 2pm = 26 hours = 93600s
        dt = datetime(2026, 4, 5, 12, 0, tzinfo=ET)
        secs = seconds_until_next_window(dt)
        assert secs == 26 * 3600  # 26 hours

    def test_minimum_one_second(self) -> None:
        # Even when next window is imminent, returns at least 1
        # Monday April 6 at 1:59:59.9 PM → Index at 2:00 PM = ~0.1s → ceil to 1
        dt = datetime(2026, 4, 6, 13, 59, 59, 900000, tzinfo=ET)
        secs = seconds_until_next_window(dt)
        assert secs >= 1


class TestDST:
    def test_spring_forward(self) -> None:
        # March 8 2026 is spring forward day in US
        # A Wednesday evening window should still work
        # March 4 2026 is a Wednesday — jobless claims window
        dt = datetime(2026, 3, 4, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Jobless claims"

    def test_fall_back(self) -> None:
        # November 1 2026 is fall back day
        # Oct 28 2026 is a Wednesday — jobless claims
        dt = datetime(2026, 10, 28, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Jobless claims"


class TestPositionWindow:
    def test_basic_window(self) -> None:
        from datetime import timedelta

        close = datetime(2026, 4, 15, 12, 0, tzinfo=ET)
        open_dt, close_dt = position_window(close)
        assert close_dt == close.astimezone(ET)
        assert open_dt == close.astimezone(ET) - timedelta(hours=18)

    def test_custom_hours(self) -> None:
        from datetime import timedelta

        close = datetime(2026, 4, 15, 12, 0, tzinfo=ET)
        open_dt, _ = position_window(close, hours_before=6.0)
        assert open_dt == close.astimezone(ET) - timedelta(hours=6)
