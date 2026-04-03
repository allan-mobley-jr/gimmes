"""Unit tests for trade window calendar."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from gimmes.strategy.calendar import (
    is_in_trade_window,
    next_trade_window,
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
    def test_night_before_12th(self) -> None:
        # April 2026: 12th is a Sunday → nearest biz day is Monday 13th
        # So window opens Sunday 12th 6:30 PM, closes Monday 13th 8:30 AM
        dt = datetime(2026, 4, 12, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "CPI"

    def test_release_morning(self) -> None:
        # Monday April 13 at 8:00 AM
        dt = datetime(2026, 4, 13, 8, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "CPI"


class TestCorePCE:
    def test_night_before_last_friday(self) -> None:
        # April 2026: last Friday is April 24
        # Window opens Thursday April 23 6:30 PM
        dt = datetime(2026, 4, 23, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Core PCE"


class TestGDPAdvance:
    def test_january_has_window(self) -> None:
        # January 2026: ~28th, nearest biz day is Wed Jan 28
        # Window opens Tue Jan 27 6:30 PM
        dt = datetime(2026, 1, 27, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "GDP Advance"

    def test_february_has_no_window(self) -> None:
        # Feb is not a GDP month — check ~28th area
        dt = datetime(2026, 2, 27, 19, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        # Should not be GDP Advance
        assert not in_w or name != "GDP Advance"


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
        # Since March 31 is in March, April's _adp returns nothing
        # March's _adp should produce this window
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
