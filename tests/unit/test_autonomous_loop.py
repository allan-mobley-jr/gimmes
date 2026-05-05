"""Tests for gimmes autonomous loop commands (driving_range, championship)."""

from __future__ import annotations

import subprocess as _subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.exceptions import Exit as ClickExit
from typer.testing import CliRunner

from gimmes.cli import (
    _autonomous_loop,
    _check_code_staleness,
    _check_remote_staleness,
    _communicate_interruptible,
    _detect_api_error,
    _detect_rate_limit,
    _extract_terminal_text,
    _set_mode,
    _wrap_stream_json,
    app,
)
from gimmes.config import GimmesConfig, Mode, RiskConfig

runner = CliRunner()


def _make_future_dt() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def _mock_popen(returncode: int = 0, output: bytes = b"") -> MagicMock:
    """Return a mock Popen instance with the given returncode and stdout output."""
    mock_proc = MagicMock()
    mock_proc.stdout.read.return_value = output
    mock_proc.returncode = returncode
    mock_proc.pid = 12345
    mock_proc.args = ["claude"]
    mock_proc.wait.return_value = returncode
    return mock_proc


# ---------------------------------------------------------------------------
# _set_mode
# ---------------------------------------------------------------------------


class TestSetMode:
    def test_raises_when_env_missing(self, tmp_path, monkeypatch) -> None:
        """_set_mode raises typer.Exit when .env doesn't exist."""
        monkeypatch.setattr("gimmes.init.ENV_FILE", tmp_path / "nonexistent" / ".env")
        with pytest.raises(ClickExit):
            _set_mode("driving_range")

    def test_writes_mode_and_reloads(self, tmp_path, monkeypatch) -> None:
        """_set_mode writes to .env and reloads so os.environ reflects the change."""
        import os

        env_file = tmp_path / ".env"
        env_file.write_text("GIMMES_MODE=driving_range\n")
        monkeypatch.setattr("gimmes.init.ENV_FILE", env_file)
        monkeypatch.setenv("GIMMES_MODE", "driving_range")

        _set_mode("championship")

        assert os.environ["GIMMES_MODE"] == "championship"
        assert "championship" in env_file.read_text()

    def test_raises_on_write_error(self, tmp_path, monkeypatch) -> None:
        """_set_mode raises typer.Exit when _update_env_var fails with OSError."""
        env_file = tmp_path / ".env"
        env_file.write_text("GIMMES_MODE=driving_range\n")
        monkeypatch.setattr("gimmes.init.ENV_FILE", env_file)

        with patch("gimmes.init._update_env_var", side_effect=OSError("disk full")):
            with pytest.raises(ClickExit):
                _set_mode("championship")

    def test_raises_on_verification_failure(self, tmp_path, monkeypatch) -> None:
        """_set_mode raises typer.Exit when post-write verification fails."""
        env_file = tmp_path / ".env"
        env_file.write_text("GIMMES_MODE=driving_range\n")
        monkeypatch.setattr("gimmes.init.ENV_FILE", env_file)
        monkeypatch.setenv("GIMMES_MODE", "driving_range")

        # Patch load_dotenv to be a no-op so env var stays stale
        with patch("dotenv.load_dotenv"):
            with pytest.raises(ClickExit):
                _set_mode("championship")


# ---------------------------------------------------------------------------
# _autonomous_loop
# ---------------------------------------------------------------------------


class TestAutonomousLoop:
    @pytest.fixture(autouse=True)
    def _patch_session_funcs(self, tmp_path, monkeypatch):
        """Patch session DB functions so tests don't touch the real database."""
        # Preserve GIMMES_MODE so _autonomous_loop's os.environ write doesn't leak
        monkeypatch.setenv("GIMMES_MODE", "driving_range")
        monkeypatch.setattr("gimmes.config.GIMMES_HOME", tmp_path)
        with (
            patch("gimmes.store.session.create_session", return_value=1),
            patch("gimmes.store.session.end_session"),
            patch("gimmes.store.session.mark_stale_sessions", return_value=0),
            patch("gimmes.store.session.update_session_cycle"),
            patch("asyncio.run", side_effect=lambda coro: coro.close()),
            patch(
                "gimmes.strategy.calendar.is_in_trade_window",
                return_value=(True, "Index contracts", 3600),
            ),
            patch(
                "gimmes.strategy.calendar.next_trade_window",
                return_value=(_make_future_dt(), "Index contracts"),
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_window",
                return_value=3600,
            ),
            patch(
                "gimmes.cli._check_code_staleness",
                return_value=("abc123", False, None),
            ),
            patch(
                "gimmes.cli._check_remote_staleness",
                return_value=None,
            ),
        ):
            yield

    def test_exits_when_claude_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            with pytest.raises(ClickExit):
                _autonomous_loop("driving_range")

    def test_sets_gimmes_mode_env(self) -> None:
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        env = mock_popen.call_args.kwargs["env"]
        assert env["GIMMES_MODE"] == "driving_range"

    def test_sets_championship_mode_env(self) -> None:
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("championship", max_cycles=1)

        env = mock_popen.call_args.kwargs["env"]
        assert env["GIMMES_MODE"] == "championship"

    def test_respects_max_cycles(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=lambda *a, **kw: _mock_popen()) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=3, pause_seconds=0)

        assert mock_popen.call_count == 3

    def test_warns_when_max_cycles_zero(self, capsys) -> None:  # type: ignore[no-untyped-def]
        """max_cycles=0 (unbounded) prints a startup warning."""
        def side_effect(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=side_effect),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=0, pause_seconds=0)

        output = capsys.readouterr().out
        # Flatten rich's terminal-width line wrapping before substring checks.
        flat = " ".join(output.split())
        assert "Unbounded run" in flat
        assert "Pass --max-cycles" in flat

    def test_no_warning_when_max_cycles_positive(self, capsys) -> None:  # type: ignore[no-untyped-def]
        """max_cycles>0 (bounded) does not print the unbounded-run warning."""
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=lambda *a, **kw: _mock_popen()),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=2, pause_seconds=0)

        output = capsys.readouterr().out
        assert "Unbounded run" not in output

    def test_no_model_flag_when_default_unset(self) -> None:
        """When config.model.default is None, --model is not passed to claude."""
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch(
                "gimmes.cli._communicate_interruptible", return_value=b"",
            ),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        cmd = mock_popen.call_args.args[0]
        assert "--model" not in cmd

    def test_model_flag_passed_when_default_set(self) -> None:
        """When config.model.default is set, --model <id> is passed to claude."""
        from gimmes import cli as gimmes_cli
        from gimmes.config import ModelConfig

        original_load_config = gimmes_cli.load_config

        def patched_load_config(*args, **kwargs):  # type: ignore[no-untyped-def]
            cfg = original_load_config(*args, **kwargs)
            return cfg.model_copy(
                update={"model": ModelConfig(default="claude-opus-4-7")},
            )

        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch(
                "gimmes.cli._communicate_interruptible", return_value=b"",
            ),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli.load_config", side_effect=patched_load_config),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        cmd = mock_popen.call_args.args[0]
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-7"

    def test_passes_correct_claude_args(self) -> None:
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/opt/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch(
                "gimmes.cli._communicate_interruptible", return_value=b"",
            ) as mock_comm,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        cmd = mock_popen.call_args.args[0]
        assert cmd[0] == "/opt/bin/claude"
        agent_idx = cmd.index("--agent")
        assert cmd[agent_idx + 1] == "Caddie Master"
        fmt_idx = cmd.index("--output-format")
        assert cmd[fmt_idx + 1] == "stream-json"
        assert "--verbose" in cmd
        idx = cmd.index("--allowedTools")
        allowed = cmd[idx + 1]
        assert "WebSearch" in allowed
        assert "WebFetch" in allowed
        assert mock_comm.call_args.kwargs["timeout"] == 2700

    def test_warns_on_nonzero_exit(self, capsys) -> None:  # type: ignore[no-untyped-def]
        mock_proc = _mock_popen(returncode=2)
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        output = capsys.readouterr().out
        assert "exited with code 2" in output

    def test_keyboard_interrupt_stops_loop(self) -> None:
        call_count = 0

        def side_effect(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise KeyboardInterrupt
            return _mock_popen()

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=side_effect),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", pause_seconds=0)

        assert call_count == 2

    def test_subprocess_failure_does_not_stop_loop(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(returncode=1),
            ) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=2, pause_seconds=0)

        assert mock_popen.call_count == 2

    def test_circuit_breaker_halts_after_consecutive_failures(
        self, capsys,
    ) -> None:  # type: ignore[no-untyped-def]
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(returncode=1),
            ) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", pause_seconds=0,
                max_consecutive_failures=3,
            )

        assert mock_popen.call_count == 3
        output = capsys.readouterr().out
        assert "Circuit breaker tripped" in output

    def test_circuit_breaker_resets_on_success(self) -> None:
        call_count = 0

        def alternate(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            # Fail twice, succeed once, fail twice, succeed once
            return _mock_popen(returncode=1 if call_count % 3 != 0 else 0)

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=alternate) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=6, pause_seconds=0,
                max_consecutive_failures=3,
            )

        # Should complete all 6 cycles (never hits 3 consecutive)
        assert mock_popen.call_count == 6

    def test_circuit_breaker_default_is_five(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(returncode=1),
            ) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", pause_seconds=0)

        # Default max_consecutive_failures=5
        assert mock_popen.call_count == 5

    def test_timeout_increments_failures_and_continues(self, capsys) -> None:
        """A TimeoutExpired cycle counts as a failure but the loop continues."""
        comm_count = 0

        def comm_side_effect(proc, timeout):
            nonlocal comm_count
            comm_count += 1
            if comm_count == 1:
                raise _subprocess.TimeoutExpired(cmd=proc.args, timeout=timeout)
            return b""

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ) as mock_popen,
            patch("gimmes.cli._communicate_interruptible", side_effect=comm_side_effect),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=2, pause_seconds=0)

        assert mock_popen.call_count == 2
        output = capsys.readouterr().out
        assert "timed out" in output

    def test_timeout_feeds_circuit_breaker(self, capsys) -> None:
        """Consecutive timeouts trip the circuit breaker."""
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ) as mock_popen,
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(cmd="claude", timeout=2700),
            ),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", pause_seconds=0,
                max_consecutive_failures=3,
            )

        assert mock_popen.call_count == 3
        output = capsys.readouterr().out
        assert "Circuit breaker tripped" in output

    def test_popen_starts_new_session(self) -> None:
        """Popen is called with start_new_session=True for process group cleanup."""
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        assert mock_popen.call_args.kwargs["start_new_session"] is True

    def test_timeout_sends_sigterm_to_process_group(self) -> None:
        """On timeout, SIGTERM is sent to the entire process group."""
        import signal

        mock_proc = _mock_popen()

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(cmd="claude", timeout=2700),
            ),
            patch("os.killpg") as mock_killpg,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=1,
                max_consecutive_failures=1,
            )

        mock_killpg.assert_any_call(12345, signal.SIGTERM)

    def test_timeout_escalates_to_sigkill_on_stuck_process(self) -> None:
        """If process doesn't exit after SIGTERM within 5s, SIGKILL is sent."""
        import signal

        mock_proc = _mock_popen()
        mock_proc.wait.side_effect = [
            _subprocess.TimeoutExpired(cmd="claude", timeout=5),
            None,
        ]

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(cmd="claude", timeout=2700),
            ),
            patch("os.killpg") as mock_killpg,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=1,
                max_consecutive_failures=1,
            )

        calls = mock_killpg.call_args_list
        assert calls[0] == ((12345, signal.SIGTERM),)
        assert calls[1] == ((12345, signal.SIGKILL),)

    def test_timeout_no_sigkill_when_sigterm_sufficient(self) -> None:
        """If process exits after SIGTERM, SIGKILL is never sent."""
        import signal

        mock_proc = _mock_popen()
        mock_proc.wait.return_value = None

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(cmd="claude", timeout=2700),
            ),
            patch("os.killpg") as mock_killpg,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=1,
                max_consecutive_failures=1,
            )

        assert mock_killpg.call_count == 1
        mock_killpg.assert_called_once_with(12345, signal.SIGTERM)

    def test_keyboard_interrupt_kills_subprocess_group(self) -> None:
        """Ctrl+C during subprocess I/O kills the subprocess process group."""
        import signal

        mock_proc = _mock_popen()
        mock_proc.poll.return_value = None  # subprocess still running

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("gimmes.cli._communicate_interruptible", side_effect=KeyboardInterrupt),
            patch("os.killpg") as mock_killpg,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        mock_killpg.assert_called_once_with(12345, signal.SIGTERM)

    def test_keyboard_interrupt_escalates_to_sigkill(self) -> None:
        """Ctrl+C escalates to SIGKILL if subprocess doesn't exit after SIGTERM."""
        import signal

        mock_proc = _mock_popen()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = [
            _subprocess.TimeoutExpired(cmd="claude", timeout=5),
            None,
        ]

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("gimmes.cli._communicate_interruptible", side_effect=KeyboardInterrupt),
            patch("os.killpg") as mock_killpg,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        calls = mock_killpg.call_args_list
        assert calls[0] == ((12345, signal.SIGTERM),)
        assert calls[1] == ((12345, signal.SIGKILL),)

    def test_keyboard_interrupt_during_sleep_no_kill(self) -> None:
        """Ctrl+C during sleep between cycles doesn't try to kill exited process."""
        mock_proc = _mock_popen()
        mock_proc.poll.return_value = 0  # subprocess already exited

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("os.killpg") as mock_killpg,
            patch("time.sleep", side_effect=KeyboardInterrupt),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", pause_seconds=60)

        mock_killpg.assert_not_called()

    def test_keyboard_interrupt_before_first_popen_no_kill(self) -> None:
        """Ctrl+C before first Popen call doesn't crash (proc is None)."""
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=KeyboardInterrupt),
            patch("os.killpg") as mock_killpg,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range")

        mock_killpg.assert_not_called()

    def test_keyboard_interrupt_handles_process_lookup_error(self) -> None:
        """Ctrl+C doesn't crash if process exits between poll() and killpg()."""
        mock_proc = _mock_popen()
        mock_proc.poll.return_value = None  # subprocess appears running

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("gimmes.cli._communicate_interruptible", side_effect=KeyboardInterrupt),
            patch("os.killpg", side_effect=ProcessLookupError),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

    def test_sigint_handler_installed_during_loop(self) -> None:
        """A custom SIGINT handler is active during the cycle loop."""
        import signal

        captured = []

        def comm_side_effect(proc, timeout):
            captured.append(signal.getsignal(signal.SIGINT))
            return b""

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=comm_side_effect,
            ),
            patch(
                "gimmes.clubhouse.server.start_background",
                return_value=None,
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        assert len(captured) == 1
        original = signal.getsignal(signal.SIGINT)
        # Handler during loop should differ from the current (restored) one
        assert captured[0] is not original
        assert callable(captured[0])

    def test_sigint_handler_restored_after_loop(self) -> None:
        """Original SIGINT handler is restored when the loop exits."""
        import signal

        original = signal.getsignal(signal.SIGINT)

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ),
            patch(
                "gimmes.clubhouse.server.start_background",
                return_value=None,
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        assert signal.getsignal(signal.SIGINT) is original

    def test_sigint_handler_restored_after_interrupt(self) -> None:
        """Original SIGINT handler is restored even on KeyboardInterrupt."""
        import signal

        original = signal.getsignal(signal.SIGINT)

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=KeyboardInterrupt,
            ),
            patch("os.killpg"),
            patch(
                "gimmes.clubhouse.server.start_background",
                return_value=None,
            ),
        ):
            _autonomous_loop("driving_range")

        assert signal.getsignal(signal.SIGINT) is original

    def test_creates_cycle_log_file(self, tmp_path) -> None:
        """Each cycle writes a JSON log file under GIMMES_HOME/logs/."""
        import json

        json_output = json.dumps({"result": "hello from claude"}).encode()
        mock_proc = _mock_popen(output=json_output)
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        log_file = tmp_path / "logs" / "cycle-001.json"
        assert log_file.exists()
        assert log_file.read_bytes() == json_output

    def test_creates_sequential_log_files(self, tmp_path) -> None:
        """Multiple cycles produce cycle-001.json, cycle-002.json, etc."""
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=lambda *a, **kw: _mock_popen(output=b"{}\n")),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=3, pause_seconds=0)

        for i in range(1, 4):
            assert (tmp_path / "logs" / f"cycle-{i:03d}.json").exists()

    def test_monitor_only_prompt_outside_window(self) -> None:
        """Outside a trade window, prompt should be MONITOR-ONLY."""
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch(
                "gimmes.strategy.calendar.is_in_trade_window",
                return_value=(False, None, None),
            ),
            patch(
                "gimmes.strategy.calendar.next_trade_window",
                return_value=(_make_future_dt(), "Jobless claims"),
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_window",
                return_value=7200,
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        cmd = mock_popen.call_args.args[0]
        prompt_idx = cmd.index("-p")
        assert "MONITOR-ONLY" in cmd[prompt_idx + 1]

    def test_cycle_type_env_set_full(self) -> None:
        """In a trade window, GIMMES_CYCLE_TYPE should be 'full'."""
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        env = mock_popen.call_args.kwargs["env"]
        assert env["GIMMES_CYCLE_TYPE"] == "full"

    def test_cycle_type_env_set_monitor(self) -> None:
        """Outside a trade window, GIMMES_CYCLE_TYPE should be 'monitor'."""
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch(
                "gimmes.strategy.calendar.is_in_trade_window",
                return_value=(False, None, None),
            ),
            patch(
                "gimmes.strategy.calendar.next_trade_window",
                return_value=(_make_future_dt(), "CPI"),
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_window",
                return_value=1800,
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        env = mock_popen.call_args.kwargs["env"]
        assert env["GIMMES_CYCLE_TYPE"] == "monitor"


# ---------------------------------------------------------------------------
# _communicate_interruptible
# ---------------------------------------------------------------------------


class TestCommunicateInterruptible:
    def test_returns_stdout_on_success(self) -> None:
        mock_proc = _mock_popen(output=b"hello world")

        result = _communicate_interruptible(mock_proc, timeout=10)

        assert result == b"hello world"
        mock_proc.wait.assert_called_once()

    def test_returns_empty_bytes_on_no_output(self) -> None:
        mock_proc = _mock_popen()

        result = _communicate_interruptible(mock_proc, timeout=10)

        assert result == b""
        mock_proc.wait.assert_called_once()

    def test_raises_timeout_expired_when_reader_hangs(self) -> None:
        import threading

        block = threading.Event()
        mock_proc = MagicMock()
        mock_proc.args = ["claude"]
        mock_proc.stdout.read = lambda: (block.wait(), b"")[-1]

        with pytest.raises(_subprocess.TimeoutExpired):
            _communicate_interruptible(mock_proc, timeout=0.1)

        block.set()  # Let daemon thread exit cleanly

    def test_raises_when_reader_hits_os_error(self) -> None:
        """Exceptions from proc.stdout.read() propagate to the caller."""
        mock_proc = MagicMock()
        mock_proc.args = ["claude"]
        mock_proc.stdout.read.side_effect = OSError("Broken pipe")

        with pytest.raises(OSError, match="Broken pipe"):
            _communicate_interruptible(mock_proc, timeout=10)

    def test_raises_value_error_when_stdout_is_none(self) -> None:
        """Calling with proc.stdout=None raises ValueError immediately."""
        mock_proc = MagicMock()
        mock_proc.stdout = None
        mock_proc.args = ["claude"]

        with pytest.raises(ValueError, match="requires stdout=PIPE"):
            _communicate_interruptible(mock_proc, timeout=10)

    def test_returns_output_when_proc_wait_hangs(self) -> None:
        """If proc.wait() times out after stdout EOF, output is still returned."""
        mock_proc = _mock_popen(output=b"cycle output")
        mock_proc.wait.side_effect = [
            _subprocess.TimeoutExpired(cmd=mock_proc.args, timeout=5),
            None,  # _kill_process_group's proc.wait(timeout=5) succeeds
        ]

        with patch("os.killpg"):
            result = _communicate_interruptible(mock_proc, timeout=2700)

        assert result == b"cycle output"


# ---------------------------------------------------------------------------
# _extract_terminal_text
# ---------------------------------------------------------------------------


class TestExtractTerminalText:
    def test_extracts_result_from_json(self) -> None:
        import json

        data = json.dumps({"result": "cycle summary"}).encode()
        assert _extract_terminal_text(data) == b"cycle summary\n"

    def test_falls_back_on_invalid_json(self) -> None:
        raw = b"not json at all"
        assert _extract_terminal_text(raw) == raw

    def test_returns_empty_on_missing_result(self) -> None:
        import json

        data = json.dumps({"messages": []}).encode()
        assert _extract_terminal_text(data) == b""

    def test_returns_error_message_on_claude_error(self) -> None:
        import json

        data = json.dumps({"is_error": True, "result": "", "subtype": "rate_limit"}).encode()
        assert _extract_terminal_text(data) == b"[Claude error: rate_limit]\n"

    def test_returns_empty_on_empty_result(self) -> None:
        import json

        data = json.dumps({"result": ""}).encode()
        assert _extract_terminal_text(data) == b""

    def test_returns_empty_on_empty_bytes(self) -> None:
        assert _extract_terminal_text(b"") == b""

    def test_returns_empty_on_whitespace_bytes(self) -> None:
        assert _extract_terminal_text(b"   \n  ") == b""

    def test_error_with_nonempty_result(self) -> None:
        import json

        data = json.dumps({
            "is_error": True, "result": "Rate limit exceeded",
            "subtype": "rate_limit",
        }).encode()
        assert _extract_terminal_text(data) == b"[Claude error: Rate limit exceeded]\n"

    def test_extracts_result_from_stream_json(self) -> None:
        import json

        lines = [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"text": "working..."}}),
            json.dumps({"type": "result", "result": "cycle done"}),
        ]
        data = "\n".join(lines).encode()
        assert _extract_terminal_text(data) == b"cycle done\n"

    def test_stream_json_no_result_returns_empty(self, caplog) -> None:
        import json
        import logging

        lines = [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"text": "working..."}}),
        ]
        data = "\n".join(lines).encode()
        with caplog.at_level(logging.WARNING, logger="gimmes"):
            result = _extract_terminal_text(data)
        assert result == b""
        assert "no result event" in caplog.text

    def test_stream_json_error_event(self) -> None:
        import json

        lines = [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "result", "is_error": True, "subtype": "rate_limit"}),
        ]
        data = "\n".join(lines).encode()
        assert _extract_terminal_text(data) == b"[Claude error: rate_limit]\n"


# ---------------------------------------------------------------------------
# _wrap_stream_json
# ---------------------------------------------------------------------------


class TestWrapStreamJson:
    def test_single_object_unchanged(self) -> None:
        import json

        raw = json.dumps({"result": "hello"}).encode()
        assert _wrap_stream_json(raw) == raw

    def test_ndjson_produces_array(self) -> None:
        import json

        lines = [
            json.dumps({"type": "system"}),
            json.dumps({"type": "result", "result": "done"}),
        ]
        raw = "\n".join(lines).encode()
        result = json.loads(_wrap_stream_json(raw))
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["type"] == "system"
        assert result[1]["type"] == "result"

    def test_skips_empty_lines(self) -> None:
        import json

        raw = b'{"type":"a"}\n\n{"type":"b"}\n'
        result = json.loads(_wrap_stream_json(raw))
        assert len(result) == 2


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


class TestDrivingRangeCommand:
    def test_command_exists(self) -> None:
        commands = {cmd.name for cmd in app.registered_commands}
        assert "driving_range" in commands

    def test_invokes_loop_with_driving_range_mode(self) -> None:
        with (
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop") as mock_loop,
        ):
            runner.invoke(app, ["driving_range", "--cycles", "1"])

        mock_loop.assert_called_once_with(
            "driving_range", max_cycles=1, pause_seconds=60,
            monitor_interval=3600, no_dashboard=False,
        )

    def test_default_cycles_is_400(self) -> None:
        """Without --cycles, the new bounded default (400) is passed through."""
        with (
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop") as mock_loop,
        ):
            result = runner.invoke(app, ["driving_range"])

        assert result.exit_code == 0, result.output
        mock_loop.assert_called_once_with(
            "driving_range", max_cycles=400, pause_seconds=60,
            monitor_interval=3600, no_dashboard=False,
        )

    def test_max_cycles_alias_works(self) -> None:
        """The --max-cycles alias is accepted and passes through to max_cycles."""
        with (
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop") as mock_loop,
        ):
            result = runner.invoke(app, ["driving_range", "--max-cycles", "7"])

        assert result.exit_code == 0, result.output
        mock_loop.assert_called_once_with(
            "driving_range", max_cycles=7, pause_seconds=60,
            monitor_interval=3600, no_dashboard=False,
        )

    def test_help_documents_new_default_and_alias(self) -> None:
        """`gimmes driving_range --help` mentions --max-cycles alias and default 400."""
        result = runner.invoke(app, ["driving_range", "--help"])
        assert result.exit_code == 0
        assert "--max-cycles" in result.output
        assert "[default: 400]" in result.output

    def test_cycles_and_max_cycles_last_wins(self) -> None:
        """When both --cycles and --max-cycles are passed, the last value wins."""
        with (
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop") as mock_loop,
        ):
            result = runner.invoke(
                app, ["driving_range", "--cycles", "5", "--max-cycles", "9"],
            )

        assert result.exit_code == 0, result.output
        mock_loop.assert_called_once_with(
            "driving_range", max_cycles=9, pause_seconds=60,
            monitor_interval=3600, no_dashboard=False,
        )


class TestChampionshipCommand:
    def test_command_exists(self) -> None:
        commands = {cmd.name for cmd in app.registered_commands}
        assert "championship" in commands

    def test_aborts_without_confirmation(self) -> None:
        with (
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop") as mock_loop,
        ):
            result = runner.invoke(app, ["championship"], input="n\n")
            assert result.exit_code != 0
            mock_loop.assert_not_called()

    def test_invokes_loop_with_championship_mode(self) -> None:
        with (
            patch("gimmes.cli._championship_gate"),
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop") as mock_loop,
        ):
            runner.invoke(app, ["championship", "--cycles", "1"])

        mock_loop.assert_called_once_with(
            "championship", max_cycles=1, pause_seconds=60,
            monitor_interval=3600, no_dashboard=False,
        )

    def test_default_cycles_is_400(self) -> None:
        """Without --cycles, the new bounded default (400) is passed through."""
        with (
            patch("gimmes.cli._championship_gate"),
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop") as mock_loop,
        ):
            result = runner.invoke(app, ["championship"])

        assert result.exit_code == 0, result.output
        mock_loop.assert_called_once_with(
            "championship", max_cycles=400, pause_seconds=60,
            monitor_interval=3600, no_dashboard=False,
        )

    def test_max_cycles_alias_works(self) -> None:
        """The --max-cycles alias is accepted and passes through to max_cycles."""
        with (
            patch("gimmes.cli._championship_gate"),
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop") as mock_loop,
        ):
            result = runner.invoke(app, ["championship", "--max-cycles", "7"])

        assert result.exit_code == 0, result.output
        mock_loop.assert_called_once_with(
            "championship", max_cycles=7, pause_seconds=60,
            monitor_interval=3600, no_dashboard=False,
        )

    def test_help_documents_new_default_and_alias(self) -> None:
        """`gimmes championship --help` mentions --max-cycles alias and default 400."""
        result = runner.invoke(app, ["championship", "--help"])
        assert result.exit_code == 0
        assert "--max-cycles" in result.output
        assert "[default: 400]" in result.output


class TestSwitchCommand:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.setenv("GIMMES_MODE", "driving_range")

    def test_command_exists(self) -> None:
        commands = {cmd.name for cmd in app.registered_commands}
        assert "switch" in commands

    def test_switch_to_championship_requires_confirmation(self) -> None:
        with patch("gimmes.cli._set_mode") as mock_set:
            result = runner.invoke(app, ["switch", "championship"], input="n\n")
            assert result.exit_code != 0
            mock_set.assert_not_called()

    def test_switch_to_championship_with_confirmation(self) -> None:
        with (
            patch("gimmes.cli._championship_gate"),
            patch("gimmes.cli._set_mode") as mock_set,
        ):
            runner.invoke(app, ["switch", "championship"])

        mock_set.assert_called_once_with("championship")

    def test_switch_to_driving_range_no_confirmation(self, monkeypatch) -> None:
        monkeypatch.setenv("GIMMES_MODE", "championship")
        with patch("gimmes.cli._set_mode") as mock_set:
            runner.invoke(app, ["switch", "driving_range"])

        mock_set.assert_called_once_with("driving_range")

    def test_switch_invalid_mode(self) -> None:
        result = runner.invoke(app, ["switch", "invalid_mode"])
        assert result.exit_code != 0

    def test_toggle_from_driving_range(self) -> None:
        """Omitting target toggles from driving_range to championship."""
        with (
            patch("gimmes.cli._championship_gate"),
            patch("gimmes.cli._set_mode") as mock_set,
        ):
            runner.invoke(app, ["switch"])

        mock_set.assert_called_once_with("championship")

    def test_toggle_from_championship(self, monkeypatch) -> None:
        """Omitting target toggles from championship to driving_range."""
        monkeypatch.setenv("GIMMES_MODE", "championship")
        with patch("gimmes.cli._set_mode") as mock_set:
            runner.invoke(app, ["switch"])

        mock_set.assert_called_once_with("driving_range")

    def test_already_same_mode_no_op(self) -> None:
        """Switching to the current mode prints a message and does not call _set_mode."""
        with patch("gimmes.cli._set_mode") as mock_set:
            result = runner.invoke(app, ["switch", "driving_range"])

        mock_set.assert_not_called()
        assert "Already in" in result.output


class TestStartCommand:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.setenv("GIMMES_MODE", "driving_range")

    def test_command_exists(self) -> None:
        commands = {cmd.name for cmd in app.registered_commands}
        assert "start" in commands

    def test_start_invokes_loop_with_current_mode(self) -> None:
        with (
            patch("gimmes.cli._autonomous_loop") as mock_loop,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            runner.invoke(app, ["start", "--cycles", "1"])

        mock_loop.assert_called_once_with(
            "driving_range", max_cycles=1, pause_seconds=60,
            no_dashboard=False,
        )

    def test_default_cycles_is_400(self) -> None:
        """Without --cycles, the new bounded default (400) is passed through."""
        with (
            patch("gimmes.cli._autonomous_loop") as mock_loop,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            result = runner.invoke(app, ["start"])

        assert result.exit_code == 0, result.output
        mock_loop.assert_called_once_with(
            "driving_range", max_cycles=400, pause_seconds=60,
            no_dashboard=False,
        )

    def test_max_cycles_alias_works(self) -> None:
        """The --max-cycles alias is accepted and passes through to max_cycles."""
        with (
            patch("gimmes.cli._autonomous_loop") as mock_loop,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            result = runner.invoke(app, ["start", "--max-cycles", "7"])

        assert result.exit_code == 0, result.output
        mock_loop.assert_called_once_with(
            "driving_range", max_cycles=7, pause_seconds=60,
            no_dashboard=False,
        )

    def test_help_documents_new_default_and_alias(self) -> None:
        """`gimmes start --help` mentions --max-cycles alias and default 400."""
        result = runner.invoke(app, ["start", "--help"])
        assert result.exit_code == 0
        assert "--max-cycles" in result.output
        assert "[default: 400]" in result.output

    def test_start_championship_requires_confirmation(self, monkeypatch) -> None:
        monkeypatch.setenv("GIMMES_MODE", "championship")
        with patch("gimmes.cli._autonomous_loop") as mock_loop:
            result = runner.invoke(app, ["start"], input="n\n")
            assert result.exit_code != 0
            mock_loop.assert_not_called()

    def test_start_championship_with_confirmation(self, monkeypatch) -> None:
        monkeypatch.setenv("GIMMES_MODE", "championship")
        with (
            patch("gimmes.cli._championship_gate"),
            patch("gimmes.cli._autonomous_loop") as mock_loop,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            runner.invoke(app, ["start", "--cycles", "1"])

        mock_loop.assert_called_once_with(
            "championship", max_cycles=1, pause_seconds=60,
            no_dashboard=False,
        )


class TestChampionshipGate:
    """Tests for _championship_gate: real-money confirmation + bankroll prompt."""

    def test_abort_on_decline(self) -> None:
        with (
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop") as mock_loop,
        ):
            result = runner.invoke(app, ["championship"], input="n\n")
        assert result.exit_code != 0
        mock_loop.assert_not_called()

    def test_bankroll_unset_prompts_for_value(self) -> None:
        with (
            patch("gimmes.config.save_config_value") as mock_save,
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop"),
        ):
            runner.invoke(
                app, ["championship", "--cycles", "1"],
                input="y\n750\n",
            )

        mock_save.assert_called_once()
        args, kwargs = mock_save.call_args
        assert args == ("risk.bankroll_real", 750.0)
        assert "db_path" in kwargs

    def test_bankroll_set_shows_confirm_prompt(self) -> None:
        """When bankroll_real is already set, user can keep it by pressing Enter."""
        with (
            patch("gimmes.cli.load_config") as mock_load,
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop"),
            patch("gimmes.config.save_config_value") as mock_save,
        ):
            mock_load.return_value = GimmesConfig(
                mode=Mode.CHAMPIONSHIP,
                risk=RiskConfig(bankroll_real=500.0),
            )
            # "y" confirms real money, Enter keeps the current bankroll
            runner.invoke(
                app, ["championship", "--cycles", "1"],
                input="y\n\n",
            )

        # No save needed when keeping existing value
        mock_save.assert_not_called()

    def test_bankroll_zero_rejects_empty_input(self) -> None:
        """When bankroll_real is 0, empty input is rejected — must enter a number."""
        with (
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop"),
            patch("gimmes.config.save_config_value") as mock_save,
        ):
            # "y" confirms, empty rejected, then "500" accepted
            result = runner.invoke(
                app, ["championship", "--cycles", "1"],
                input="y\n\n500\n",
            )

        mock_save.assert_called_once()
        args, kwargs = mock_save.call_args
        assert args == ("risk.bankroll_real", 500.0)
        assert "db_path" in kwargs
        assert "must set a bankroll" in result.output.lower()


    def test_bankroll_rejects_over_million(self) -> None:
        """When bankroll_real is 0, values over $1M are rejected."""
        with (
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop"),
            patch("gimmes.config.save_config_value") as mock_save,
        ):
            # "y" confirms, 2M rejected, then "500" accepted
            result = runner.invoke(
                app, ["championship", "--cycles", "1"],
                input="y\n2000000\n500\n",
            )

        mock_save.assert_called_once()
        args, kwargs = mock_save.call_args
        assert args == ("risk.bankroll_real", 500.0)
        assert "db_path" in kwargs
        assert "exceed" in result.output.lower()


class TestOrderYesFlag:
    def test_order_command_has_yes_option(self) -> None:
        result = runner.invoke(app, ["order", "--help"])
        assert "--yes" in result.output

    def test_order_command_has_force_option(self) -> None:
        result = runner.invoke(app, ["order", "--help"])
        assert "--force" in result.output


# ---------------------------------------------------------------------------
# Caddie Master agent
# ---------------------------------------------------------------------------

_CADDIE_MASTER_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / ".claude" / "agents" / "caddie-master.md"
)


class TestDetectRateLimit:
    """Tests for _detect_rate_limit helper."""

    def test_no_rate_limit_in_normal_output(self) -> None:
        output = b"Cycle completed successfully. All steps passed."
        is_limited, pause = _detect_rate_limit(output)
        assert is_limited is False
        assert pause == 0

    def test_detects_rate_limit_message(self) -> None:
        output = b"You've hit your limit \xc2\xb7 resets 5pm (America/New_York)"
        is_limited, pause = _detect_rate_limit(output)
        assert is_limited is True
        assert pause > 0

    def test_detects_rate_limit_without_reset_time(self) -> None:
        output = b"You've hit your limit"
        is_limited, pause = _detect_rate_limit(output)
        assert is_limited is True
        assert pause == 1800  # 30 min fallback

    def test_detects_429_rate_limit(self) -> None:
        output = b"Error: 429 Too Many Requests - rate limit exceeded"
        is_limited, pause = _detect_rate_limit(output)
        assert is_limited is True
        assert pause == 1800  # 30 min fallback

    def test_empty_output(self) -> None:
        is_limited, pause = _detect_rate_limit(b"")
        assert is_limited is False
        assert pause == 0

    def test_rate_limit_embedded_in_json(self) -> None:
        output = (
            b'{"type":"result","result":"You\'ve hit your limit'
            b' \\xc2\\xb7 resets 5pm (America/New_York)"}'
        )
        is_limited, pause = _detect_rate_limit(output)
        assert is_limited is True


def _stream_json_result(is_error: bool, result: object) -> bytes:
    import json

    envelope = {"type": "result", "is_error": is_error, "result": result}
    return (b'{"type":"assistant"}\n' + json.dumps(envelope).encode() + b"\n")


class TestDetectApiError:
    """Tests for _detect_api_error helper (issue #522)."""

    def test_detects_api_500(self) -> None:
        output = _stream_json_result(
            True,
            'API Error: 500 {"type":"error","error":{"type":"api_error",'
            '"message":"Internal server error"}} · check status.claude.com',
        )
        had_error, is_transient, detail = _detect_api_error(output)
        assert had_error is True
        assert is_transient is True
        assert "500" in detail

    @pytest.mark.parametrize("code", [502, 503, 504, 529, 599])
    def test_detects_other_5xx(self, code: int) -> None:
        output = _stream_json_result(True, f"API Error: {code} Upstream failure")
        had_error, is_transient, _ = _detect_api_error(output)
        assert had_error is True
        assert is_transient is True

    def test_detects_overloaded_error(self) -> None:
        output = _stream_json_result(
            True, '{"type":"overloaded_error","message":"Overloaded"}',
        )
        had_error, is_transient, _ = _detect_api_error(output)
        assert had_error is True
        assert is_transient is True

    def test_detects_timeout(self) -> None:
        output = _stream_json_result(True, "Request timed out after 600s")
        had_error, is_transient, _ = _detect_api_error(output)
        assert had_error is True
        assert is_transient is True

    def test_detects_connection_reset(self) -> None:
        output = _stream_json_result(True, "connection reset by peer (ECONNRESET)")
        had_error, is_transient, _ = _detect_api_error(output)
        assert had_error is True
        assert is_transient is True

    def test_permanent_4xx_auth_error_is_error_but_not_transient(self) -> None:
        output = _stream_json_result(
            True, "API Error: 401 authentication_error: invalid x-api-key",
        )
        had_error, is_transient, detail = _detect_api_error(output)
        assert had_error is True
        assert is_transient is False
        assert "401" in detail

    def test_permanent_invalid_request_is_error_but_not_transient(self) -> None:
        output = _stream_json_result(True, "invalid_request_error: tool not found")
        had_error, is_transient, _ = _detect_api_error(output)
        assert had_error is True
        assert is_transient is False

    def test_success_envelope_is_not_error(self) -> None:
        output = _stream_json_result(
            False, "Cycle complete. All steps passed. No timeout.",
        )
        had_error, is_transient, _ = _detect_api_error(output)
        assert had_error is False
        assert is_transient is False

    def test_empty_output(self) -> None:
        assert _detect_api_error(b"") == (False, False, "")
        assert _detect_api_error(b"   \n") == (False, False, "")

    def test_no_result_event(self) -> None:
        output = b'{"type":"assistant"}\n{"type":"tool_use"}\n'
        assert _detect_api_error(output) == (False, False, "")

    def test_malformed_json_lines_are_skipped(self) -> None:
        output = (
            b"not-json\n"
            + _stream_json_result(True, "API Error: 500 bad")
        )
        had_error, is_transient, _ = _detect_api_error(output)
        assert had_error is True
        assert is_transient is True

    def test_detects_stream_idle_timeout(self) -> None:
        output = _stream_json_result(
            True,
            "API Error: Stream idle timeout - partial response received",
        )
        had_error, is_transient, detail = _detect_api_error(output)
        assert had_error is True
        assert is_transient is True
        assert "Stream idle timeout" in detail

    def test_null_result_uses_subtype_as_detail(self) -> None:
        output = (
            b'{"type":"result","is_error":true,"result":null,'
            b'"subtype":"error_during_execution"}\n'
        )
        had_error, is_transient, detail = _detect_api_error(output)
        assert had_error is True
        assert is_transient is False
        assert detail == "error_during_execution"

    def test_mixed_stream_picks_terminal_result_event(self) -> None:
        # assistant + tool_use events before and after the result envelope
        import json

        msgs = [
            b'{"type":"system","subtype":"init"}',
            b'{"type":"assistant","message":"reasoning"}',
            b'{"type":"tool_use","name":"Bash"}',
            json.dumps({
                "type": "result", "is_error": True,
                "result": "API Error: 503 Service Unavailable",
            }).encode(),
        ]
        output = b"\n".join(msgs) + b"\n"
        had_error, is_transient, detail = _detect_api_error(output)
        assert had_error is True
        assert is_transient is True
        assert "503" in detail

    def test_utf8_non_ascii_bytes_in_detail(self) -> None:
        # The real-world error string includes U+00B7 middle dot (\xc2\xb7)
        import json

        envelope = {
            "type": "result", "is_error": True,
            "result": "API Error: 500 Internal server error \u00b7 status.claude.com",
        }
        output = json.dumps(envelope, ensure_ascii=False).encode("utf-8") + b"\n"
        had_error, is_transient, detail = _detect_api_error(output)
        assert had_error is True
        assert is_transient is True
        assert "\u00b7" in detail

    def test_dict_result_is_serialized_to_json(self) -> None:
        output = _stream_json_result(
            True, {"type": "overloaded_error", "message": "Overloaded"},
        )
        had_error, is_transient, detail = _detect_api_error(output)
        assert had_error is True
        assert is_transient is True
        assert '"overloaded_error"' in detail


class TestApiErrorLoopIntegration:
    """Loop-level integration for #522 — API errors retry via backoff."""

    def test_transient_api_error_triggers_backoff_and_retries(
        self, capsys,  # type: ignore[no-untyped-def]
    ) -> None:
        transient_output = _stream_json_result(
            True, "API Error: 500 Internal server error",
        )
        ok_output = _stream_json_result(False, "ok")
        call_count = 0

        def popen_side_effect(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_popen(returncode=0, output=transient_output)
            return _mock_popen(returncode=0, output=ok_output)

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=popen_side_effect),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli._check_code_staleness", return_value=("", False, None)),
            patch("gimmes.cli._check_remote_staleness", return_value=None),
            patch("time.sleep") as mock_sleep,
        ):
            _autonomous_loop("driving_range", max_cycles=2, pause_seconds=0)

        out = capsys.readouterr().out
        assert "transient API error" in out
        mock_sleep.assert_any_call(30)
        assert call_count == 2

    def test_permanent_api_error_still_counts_as_failure(
        self, capsys,  # type: ignore[no-untyped-def]
    ) -> None:
        # A 401 auth failure is not transient but must still be counted as
        # a cycle failure. Otherwise consecutive_failures resets and the
        # loop burns forever on a broken API key — the original #522 bug
        # class, just for permanent instead of transient errors.
        permanent_output = _stream_json_result(
            True, "API Error: 401 authentication_error: invalid x-api-key",
        )
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(
                    returncode=0, output=permanent_output,
                ),
            ) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli._check_code_staleness", return_value=("", False, None)),
            patch("gimmes.cli._check_remote_staleness", return_value=None),
            patch("time.sleep"),
        ):
            _autonomous_loop(
                "driving_range", pause_seconds=0,
                max_consecutive_failures=2,
            )

        out = capsys.readouterr().out
        assert mock_popen.call_count == 2
        assert "API error" in out
        assert "transient API error" not in out
        assert "Circuit breaker tripped" in out

    def test_api_error_trips_circuit_breaker(
        self, capsys,  # type: ignore[no-untyped-def]
    ) -> None:
        transient_output = _stream_json_result(
            True, "API Error: 503 Service Unavailable",
        )
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(
                    returncode=0, output=transient_output,
                ),
            ) as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli._check_code_staleness", return_value=("", False, None)),
            patch("gimmes.cli._check_remote_staleness", return_value=None),
            patch("time.sleep"),
        ):
            _autonomous_loop(
                "driving_range", pause_seconds=0,
                max_consecutive_failures=3,
            )

        assert mock_popen.call_count == 3
        out = capsys.readouterr().out
        assert "Circuit breaker tripped" in out


class TestCaddieMasterAgent:
    def test_agent_file_exists(self) -> None:
        assert _CADDIE_MASTER_PATH.exists()

    def test_agent_has_frontmatter(self) -> None:
        content = _CADDIE_MASTER_PATH.read_text()
        assert "name: Caddie Master" in content
        assert "tools:" in content
        assert "Agent" in content


class TestAutonomousLoopAgentModels:
    """All 7 autonomous-loop agents pin Sonnet 4.6 in their frontmatter (#544)."""

    AGENTS_DIR = (
        Path(__file__).resolve().parent.parent.parent / ".claude" / "agents"
    )
    EXPECTED_MODEL = "claude-sonnet-4-6"

    @pytest.mark.parametrize(
        "agent_file",
        [
            "caddie-master.md",
            "scout.md",
            "caddie.md",
            "closer.md",
            "monitor.md",
            "groundskeeper.md",
            "scorecard.md",
        ],
    )
    def test_agent_pins_sonnet_4_6(self, agent_file: str) -> None:
        # Match the frontmatter `model:` field exactly (whitespace-tolerant,
        # value-anchored). A loose substring check would silently pass on
        # `model: sonnet-4-6-old` or break on `model:  sonnet-4-6` — neither
        # is what we want for a value Claude CLI parses.
        import re

        text = (self.AGENTS_DIR / agent_file).read_text()
        assert text.startswith("---\n"), f"{agent_file}: missing frontmatter"
        end = text.index("\n---", 4)
        frontmatter = text[4:end]
        match = re.search(r"^model:\s+(\S+)\s*$", frontmatter, re.MULTILINE)
        assert match is not None, f"{agent_file}: no `model:` field"
        assert match.group(1) == self.EXPECTED_MODEL


class TestCheckCodeStaleness:
    def test_first_call_returns_commit(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="abc123def\n",
            )
            commit, stale, msg = _check_code_staleness(tmp_path, None)
            assert commit == "abc123def"
            assert stale is False
            assert msg is None

    def test_same_commit_not_stale(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="abc123def\n",
            )
            _, stale, msg = _check_code_staleness(
                tmp_path, "abc123def",
            )
            assert stale is False
            assert msg is None

    def test_different_commit_is_stale(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="new456ghi\n",
            )
            _, stale, msg = _check_code_staleness(
                tmp_path, "old123abc",
            )
            assert stale is True
            assert "old123ab" in msg
            assert "new456gh" in msg

    def test_git_failure_returns_empty(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128, stdout="",
            )
            commit, stale, msg = _check_code_staleness(
                tmp_path, "abc",
            )
            assert commit == ""
            assert stale is False

    def test_git_not_found(self, tmp_path: Path) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            commit, stale, msg = _check_code_staleness(
                tmp_path, "abc",
            )
            assert commit == ""
            assert stale is False


class TestCheckRemoteStaleness:
    def test_same_commit_returns_none(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="abc123def\tHEAD\n",
            )
            msg = _check_remote_staleness(tmp_path, "abc123def")
            assert msg is None

    def test_different_commit_returns_warning(
        self, tmp_path: Path,
    ) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="remote99\tHEAD\n",
            )
            msg = _check_remote_staleness(tmp_path, "local123")
            assert msg is not None
            assert "remote99" in msg
            assert "local123" in msg
            assert "differs" in msg.lower()

    def test_network_failure_returns_none(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _subprocess.TimeoutExpired(
                cmd="git", timeout=10,
            )
            msg = _check_remote_staleness(tmp_path, "abc")
            assert msg is None
