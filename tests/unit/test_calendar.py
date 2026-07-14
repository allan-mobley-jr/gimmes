"""Unit tests for trade window calendar."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from gimmes.strategy.calendar import (
    _cpi,
    _gdp_advance,
    hourly_window,
    is_in_hourly_window,
    is_in_trade_window,
    next_trade_window,
    position_window,
    seconds_until_next_hourly_open,
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
    def test_wednesday_evening_not_in_window(self) -> None:
        # Post-#558: night-before pre-roll dropped. Wed April 1 7:00 PM is
        # in the dead zone — assert no window is active at all (not just
        # that Jobless claims is silent).
        dt = datetime(2026, 4, 1, 19, 0, tzinfo=ET)
        in_w, _, _ = is_in_trade_window(dt)
        assert in_w is False

    def test_thursday_4am_in_window(self) -> None:
        # Thursday April 2, 4:00 AM ET — new release-day window opens
        dt = datetime(2026, 4, 2, 4, 0, tzinfo=ET)
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
    def test_tuesday_midnight_not_in_window(self) -> None:
        # Post-#558: Tue 23:30 ET no longer opens the Wed Treasury window
        # and falls in the dead zone — assert no window is active.
        dt = datetime(2026, 4, 7, 23, 30, tzinfo=ET)
        in_w, _, _ = is_in_trade_window(dt)
        assert in_w is False

    def test_wednesday_4am_in_window(self) -> None:
        # Wednesday April 8, 4:00 AM ET — new release-day window opens
        dt = datetime(2026, 4, 8, 4, 0, tzinfo=ET)
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
    def test_thursday_before_first_friday_not_in_window(self) -> None:
        # Post-#558: Thursday evening before NFP is in the dead zone.
        dt = datetime(2026, 4, 2, 19, 0, tzinfo=ET)
        in_w, _, _ = is_in_trade_window(dt)
        assert in_w is False

    def test_first_friday_4am_in_window(self) -> None:
        # Friday April 3, 4:00 AM ET — new release-day window opens
        dt = datetime(2026, 4, 3, 4, 0, tzinfo=ET)
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
    def test_thursday_evening_not_in_window(self) -> None:
        # Post-#558: night before CPI is in the dead zone.
        dt = datetime(2026, 4, 9, 19, 0, tzinfo=ET)
        in_w, _, _ = is_in_trade_window(dt)
        assert in_w is False

    def test_april_2026_release_4am(self) -> None:
        # Friday April 10 at 4:00 AM — new release-day window opens
        dt = datetime(2026, 4, 10, 4, 0, tzinfo=ET)
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
    def test_night_before_last_friday_not_in_window(self) -> None:
        # Post-#558: Thursday evening before PCE is in the dead zone.
        dt = datetime(2026, 4, 23, 19, 0, tzinfo=ET)
        in_w, _, _ = is_in_trade_window(dt)
        assert in_w is False

    def test_last_friday_4am_in_window(self) -> None:
        # April 2026: last Friday is April 24, 4:00 AM ET window opens
        dt = datetime(2026, 4, 24, 4, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Core PCE"


class TestGDPAdvance:
    def test_january_2026_evening_before_not_in_window(self) -> None:
        # Post-#558: Wed Jan 28 19:00 ET is in the dead zone.
        dt = datetime(2026, 1, 28, 19, 0, tzinfo=ET)
        in_w, _, _ = is_in_trade_window(dt)
        assert in_w is False

    def test_january_2026_release_4am(self) -> None:
        # Thu Jan 29 04:00 ET — new release-day window opens
        dt = datetime(2026, 1, 29, 4, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "GDP Advance"

    def test_april_2026_release_4am(self) -> None:
        # Thu Apr 30 04:00 ET — new release-day window opens
        dt = datetime(2026, 4, 30, 4, 0, tzinfo=ET)
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
    def test_evening_before_not_in_window(self) -> None:
        # Post-#558: Tue Mar 31 20:00 ET is in the dead zone.
        dt = datetime(2026, 3, 31, 20, 0, tzinfo=ET)
        in_w, _, _ = is_in_trade_window(dt)
        assert in_w is False

    def test_first_business_day_4am_in_window(self) -> None:
        # March 2026: Mar 1 is Sunday → first biz day Mon Mar 2. ADP is
        # Wed Mar 4 (before NFP Fri Mar 6); NFP is Fri Mar 6. No collisions
        # with ISM on Mon Mar 2 04:00 ET.
        dt = datetime(2026, 3, 2, 4, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "ISM PMI"

    def test_first_business_day_morning(self) -> None:
        # April 2026: 1st is Wednesday (business day), 9:00 AM still in window
        dt = datetime(2026, 4, 1, 9, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "ISM PMI"


class TestADP:
    def test_tuesday_evening_not_in_window(self) -> None:
        # Post-#558: Tue Mar 31 18:30 ET is in the dead zone.
        dt = datetime(2026, 3, 31, 18, 30, tzinfo=ET)
        in_w, _, _ = is_in_trade_window(dt)
        assert in_w is False

    def test_wednesday_4am_in_window(self) -> None:
        # Wed Apr 1 04:00 ET — new release-day ADP window opens
        dt = datetime(2026, 4, 1, 4, 0, tzinfo=ET)
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

    def test_after_index_close_next_is_claims_thursday_morning(self) -> None:
        # Wednesday April 1 at 4:01 PM — just after index close
        dt = datetime(2026, 4, 1, 16, 1, tzinfo=ET)
        start, name = next_trade_window(dt)
        # Post-#558: jobless claims now opens Thu 04:00 ET (release day),
        # not Wed 18:30 ET — sleep increases from 1.5h to ~12h.
        assert name == "Jobless claims"
        assert start.weekday() == 3  # Thursday
        assert start.hour == 4
        assert start.minute == 0


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
        # March 8 2026 is spring forward day in US.
        # Post-#558: jobless claims opens 04:00 ET on Thursday release day.
        # Thursday March 5 2026 is the release; 04:00 ET is safely past
        # spring-forward's 02:00→03:00 jump (which falls on Sunday Mar 8).
        dt = datetime(2026, 3, 5, 4, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Jobless claims"

    def test_fall_back(self) -> None:
        # November 1 2026 is fall back day.
        # Thursday Oct 29 2026 is the jobless claims release day.
        dt = datetime(2026, 10, 29, 4, 0, tzinfo=ET)
        in_w, name, _ = is_in_trade_window(dt)
        assert in_w is True
        assert name == "Jobless claims"


class TestDeadZone:
    """Regression: 00:00–04:00 ET on release day must stay out of any window.

    #557 data showed 0 trades in 116 cycles across the 00:00–07:00 ET block;
    #558 trims night-before windows to 04:00 ET on release day. This test
    guards against future drift back into the dead zone.
    """

    def test_thursday_3am_not_in_any_window(self) -> None:
        # Thursday April 2 2026 at 3:00 AM ET — pre-04:00, post-midnight
        dt = datetime(2026, 4, 2, 3, 0, tzinfo=ET)
        in_w, _, _ = is_in_trade_window(dt)
        assert in_w is False

    def test_friday_3am_cpi_release_day_not_in_window(self) -> None:
        # CPI release day Apr 10 (Fri) at 3:00 AM ET — pre-04:00
        dt = datetime(2026, 4, 10, 3, 0, tzinfo=ET)
        in_w, _, _ = is_in_trade_window(dt)
        assert in_w is False


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


class TestHourlyWindow:
    """#723: ad-hoc scan windows for hourly-settled series."""

    def test_basic(self) -> None:
        dt = datetime(2026, 4, 7, 14, 10, tzinfo=ET)
        open_dt, close_dt = hourly_window(dt, lead_minutes=29)
        assert open_dt == datetime(2026, 4, 7, 14, 31, tzinfo=ET)
        assert close_dt == datetime(2026, 4, 7, 15, 0, tzinfo=ET)

    def test_exact_top_of_hour_belongs_to_next(self) -> None:
        at_top = datetime(2026, 4, 7, 14, 0, 0, tzinfo=ET)
        open_dt, close_dt = hourly_window(at_top, lead_minutes=29)
        assert close_dt == datetime(2026, 4, 7, 15, 0, tzinfo=ET)
        assert open_dt == datetime(2026, 4, 7, 14, 31, tzinfo=ET)

        just_before = datetime(2026, 4, 7, 13, 59, 59, 999999, tzinfo=ET)
        _, close_dt = hourly_window(just_before, lead_minutes=29)
        assert close_dt == datetime(2026, 4, 7, 14, 0, tzinfo=ET)

    def test_day_boundary(self) -> None:
        dt = datetime(2026, 4, 7, 23, 45, tzinfo=ET)
        open_dt, close_dt = hourly_window(dt, lead_minutes=29)
        assert close_dt == datetime(2026, 4, 8, 0, 0, tzinfo=ET)
        assert open_dt == datetime(2026, 4, 7, 23, 31, tzinfo=ET)

    def test_custom_lead(self) -> None:
        dt = datetime(2026, 4, 7, 14, 55, tzinfo=ET)
        open_dt, close_dt = hourly_window(dt, lead_minutes=10)
        assert close_dt - open_dt == timedelta(minutes=10)

    def test_spring_forward_no_2am_window(self) -> None:
        # 2026-03-08: 2 AM ET does not exist. From 01:45 EST the next
        # real hour-top is 07:00 UTC (= 03:00 EDT); UTC arithmetic gets
        # this right where wall-clock replace() would invent 02:00.
        dt = datetime(2026, 3, 8, 1, 45, tzinfo=ET)
        open_dt, close_dt = hourly_window(dt, lead_minutes=29)
        assert close_dt.astimezone(UTC) == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
        assert is_in_hourly_window(dt, lead_minutes=29) is True

    def test_fall_back_repeated_hour_gets_two_windows(self) -> None:
        # 2026-11-01: the 1 AM ET wall hour repeats. Both instants must
        # get their own settlement window (KXBTCD settles every real
        # hour) — pins the UTC-arithmetic design via ZoneInfo fold.
        first = datetime(2026, 11, 1, 1, 30, tzinfo=ET)  # fold=0, EDT
        second = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=ET)  # EST
        _, close1 = hourly_window(first, lead_minutes=29)
        _, close2 = hourly_window(second, lead_minutes=29)
        assert close1.astimezone(UTC) == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)
        assert close2.astimezone(UTC) == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
        assert close2.astimezone(UTC) - close1.astimezone(UTC) == timedelta(hours=1)


class TestIsInHourlyWindow:
    def test_open_boundary_inclusive(self) -> None:
        at_open = datetime(2026, 4, 7, 14, 31, tzinfo=ET)
        assert is_in_hourly_window(at_open, lead_minutes=29) is True

    def test_just_before_open(self) -> None:
        dt = datetime(2026, 4, 7, 14, 30, 59, tzinfo=ET)
        assert is_in_hourly_window(dt, lead_minutes=29) is False

    def test_top_of_hour_not_in_window(self) -> None:
        # At exactly 14:00 the relevant window is the 15:00 one, whose
        # open is 14:31 — close-exclusivity via the next-window rule
        dt = datetime(2026, 4, 7, 14, 0, tzinfo=ET)
        assert is_in_hourly_window(dt, lead_minutes=29) is False


class TestSecondsUntilNextHourlyOpen:
    def test_before_open(self) -> None:
        dt = datetime(2026, 4, 7, 14, 10, tzinfo=ET)
        assert seconds_until_next_hourly_open(dt, lead_minutes=29) == 1260

    def test_exactly_at_open_returns_following(self) -> None:
        dt = datetime(2026, 4, 7, 14, 31, tzinfo=ET)
        assert seconds_until_next_hourly_open(dt, lead_minutes=29) == 3600

    def test_inside_window(self) -> None:
        dt = datetime(2026, 4, 7, 14, 45, tzinfo=ET)
        assert seconds_until_next_hourly_open(dt, lead_minutes=29) == 2760

    def test_ceil_and_minimum(self) -> None:
        dt = datetime(2026, 4, 7, 14, 30, 59, 900000, tzinfo=ET)
        assert seconds_until_next_hourly_open(dt, lead_minutes=29) == 1

    def test_ceil_not_truncation(self) -> None:
        # 60.5s to the open must round UP (61) — int() truncation (60)
        # would wake the loop a second early, outside the window
        dt = datetime(2026, 4, 7, 14, 29, 59, 500000, tzinfo=ET)
        assert seconds_until_next_hourly_open(dt, lead_minutes=29) == 61

    def test_fall_back_counts_toward_first_settlement(self) -> None:
        dt = datetime(2026, 11, 1, 1, 15, tzinfo=ET)  # fold=0, EDT
        # Next open is 01:31 EDT (toward the 06:00 UTC settlement)
        assert seconds_until_next_hourly_open(dt, lead_minutes=29) == 960
