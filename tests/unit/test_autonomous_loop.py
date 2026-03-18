"""Tests for gimmes autonomous loop commands (driving_range, championship)."""

from __future__ import annotations

import subprocess as _subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.exceptions import Exit as ClickExit
from typer.testing import CliRunner

from gimmes.cli import (
    _autonomous_loop,
    _communicate_interruptible,
    _extract_terminal_text,
    _set_mode,
    _wrap_stream_json,
    app,
)

runner = CliRunner()


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
            patch("asyncio.run"),
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
            no_dashboard=False,
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
            patch("gimmes.cli._set_mode"),
            patch("gimmes.cli._autonomous_loop") as mock_loop,
        ):
            runner.invoke(app, ["championship", "--cycles", "1"], input="y\n")

        mock_loop.assert_called_once_with(
            "championship", max_cycles=1, pause_seconds=60,
            no_dashboard=False,
        )


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
        with patch("gimmes.cli._set_mode") as mock_set:
            runner.invoke(app, ["switch", "championship"], input="y\n")

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
        with patch("gimmes.cli._set_mode") as mock_set:
            runner.invoke(app, ["switch"], input="y\n")

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

    def test_start_championship_requires_confirmation(self, monkeypatch) -> None:
        monkeypatch.setenv("GIMMES_MODE", "championship")
        with patch("gimmes.cli._autonomous_loop") as mock_loop:
            result = runner.invoke(app, ["start"], input="n\n")
            assert result.exit_code != 0
            mock_loop.assert_not_called()

    def test_start_championship_with_confirmation(self, monkeypatch) -> None:
        monkeypatch.setenv("GIMMES_MODE", "championship")
        with (
            patch("gimmes.cli._autonomous_loop") as mock_loop,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            runner.invoke(app, ["start", "--cycles", "1"], input="y\n")

        mock_loop.assert_called_once_with(
            "championship", max_cycles=1, pause_seconds=60,
            no_dashboard=False,
        )


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


class TestCaddieMasterAgent:
    def test_agent_file_exists(self) -> None:
        assert _CADDIE_MASTER_PATH.exists()

    def test_agent_has_frontmatter(self) -> None:
        content = _CADDIE_MASTER_PATH.read_text()
        assert "name: Caddie Master" in content
        assert "tools:" in content
        assert "Agent" in content
