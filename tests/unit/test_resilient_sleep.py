"""Tests for _resilient_sleep (issue #530).

Verifies that the chunked sleep helper survives macOS system sleep/wake
by using time.monotonic() to track wall-clock progress.
"""

from __future__ import annotations

from unittest.mock import patch

from gimmes.cli import _resilient_sleep


class TestResilientSleep:

    def test_short_sleep_calls_once(self) -> None:
        """A sleep shorter than 60s should call time.sleep once."""
        with (
            patch("time.monotonic", side_effect=[0.0, 0.0, 30.0]),
            patch("time.sleep") as mock_sleep,
        ):
            _resilient_sleep(30)

        mock_sleep.assert_called_once_with(30.0)

    def test_long_sleep_chunks_at_60s(self) -> None:
        """A 150s sleep should chunk into 60+60+30."""
        # Call sequence: set deadline (0), check remaining (0→sleep 60),
        # check remaining (60→sleep 60), check remaining (120→sleep 30),
        # check remaining (150→break)
        times = [0.0, 0.0, 60.0, 120.0, 150.0]
        with (
            patch("time.monotonic", side_effect=times),
            patch("time.sleep") as mock_sleep,
        ):
            _resilient_sleep(150)

        assert mock_sleep.call_count == 3
        mock_sleep.assert_any_call(60)
        mock_sleep.assert_any_call(30.0)

    def test_wake_from_sleep_exits_immediately(self) -> None:
        """Simulates system wake: monotonic jumps past the deadline."""
        times = [0.0, 0.0, 7200.0]
        with (
            patch("time.monotonic", side_effect=times),
            patch("time.sleep") as mock_sleep,
        ):
            _resilient_sleep(3600)

        mock_sleep.assert_called_once_with(60)

    def test_zero_duration_returns_immediately(self) -> None:
        with (
            patch("time.monotonic", side_effect=[0.0, 0.0]),
            patch("time.sleep") as mock_sleep,
        ):
            _resilient_sleep(0)

        mock_sleep.assert_not_called()

    def test_negative_duration_returns_immediately(self) -> None:
        with (
            patch("time.monotonic", side_effect=[100.0, 100.0]),
            patch("time.sleep") as mock_sleep,
        ):
            _resilient_sleep(-5)

        mock_sleep.assert_not_called()

    def test_sleep_values_never_exceed_60(self) -> None:
        """Even for very long sleeps, individual chunks cap at 60s."""
        times = []
        for i in range(0, 7260, 60):
            times.extend([float(i), float(i)])
        with (
            patch("time.monotonic", side_effect=times),
            patch("time.sleep") as mock_sleep,
        ):
            _resilient_sleep(7200)

        for c in mock_sleep.call_args_list:
            assert c[0][0] <= 60.0
