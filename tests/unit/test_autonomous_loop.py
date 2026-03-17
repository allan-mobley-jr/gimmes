"""Tests for gimmes autonomous loop commands (driving_range, championship)."""

from __future__ import annotations

import subprocess as _subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.exceptions import Exit as ClickExit
from typer.testing import CliRunner

from gimmes.cli import _autonomous_loop, _set_mode, app

runner = CliRunner()


def _mock_popen(returncode: int = 0, output: bytes = b"") -> MagicMock:
    """Return a mock Popen instance with the given returncode and stdout output."""
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (output, b"")
    mock_proc.returncode = returncode
    mock_proc.pid = 12345
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
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        cmd = mock_popen.call_args.args[0]
        assert cmd[0] == "/opt/bin/claude"
        agent_idx = cmd.index("--agent")
        assert cmd[agent_idx + 1] == "Caddie Master"
        idx = cmd.index("--allowedTools")
        allowed = cmd[idx + 1]
        assert "WebSearch" in allowed
        assert "WebFetch" in allowed
        assert mock_proc.communicate.call_args.kwargs["timeout"] == 2700

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
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_proc = _mock_popen()
            if call_count == 1:
                mock_proc.communicate.side_effect = _subprocess.TimeoutExpired(
                    cmd=args[0], timeout=2700,
                )
            return mock_proc

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=side_effect),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=2, pause_seconds=0)

        assert call_count == 2
        output = capsys.readouterr().out
        assert "timed out" in output

    def test_timeout_feeds_circuit_breaker(self, capsys) -> None:
        """Consecutive timeouts trip the circuit breaker."""
        def side_effect(*args, **kwargs):
            mock_proc = _mock_popen()
            mock_proc.communicate.side_effect = _subprocess.TimeoutExpired(
                cmd=args[0], timeout=2700,
            )
            return mock_proc

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=side_effect) as mock_popen,
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
        mock_proc.communicate.side_effect = _subprocess.TimeoutExpired(
            cmd="claude", timeout=2700,
        )

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
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
        mock_proc.communicate.side_effect = _subprocess.TimeoutExpired(
            cmd="claude", timeout=2700,
        )
        mock_proc.wait.side_effect = [
            _subprocess.TimeoutExpired(cmd="claude", timeout=5),
            None,
        ]

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
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
        mock_proc.communicate.side_effect = _subprocess.TimeoutExpired(
            cmd="claude", timeout=2700,
        )
        mock_proc.wait.return_value = None

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("os.killpg") as mock_killpg,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=1,
                max_consecutive_failures=1,
            )

        assert mock_killpg.call_count == 1
        mock_killpg.assert_called_once_with(12345, signal.SIGTERM)

    def test_creates_cycle_log_file(self, tmp_path) -> None:
        """Each cycle writes a log file under GIMMES_HOME/logs/."""
        mock_proc = _mock_popen(output=b"hello from claude\n")
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        log_file = tmp_path / "logs" / "cycle-001.log"
        assert log_file.exists()
        assert log_file.read_bytes() == b"hello from claude\n"

    def test_creates_sequential_log_files(self, tmp_path) -> None:
        """Multiple cycles produce cycle-001.log, cycle-002.log, etc."""
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=lambda *a, **kw: _mock_popen(output=b"output\n")),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=3, pause_seconds=0)

        for i in range(1, 4):
            assert (tmp_path / "logs" / f"cycle-{i:03d}.log").exists()


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
