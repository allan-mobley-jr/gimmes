"""Tests for gimmes autonomous loop commands (driving_range, championship)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.exceptions import Exit as ClickExit
from typer.testing import CliRunner

from gimmes.cli import _autonomous_loop, app

runner = CliRunner()

# ---------------------------------------------------------------------------
# _autonomous_loop
# ---------------------------------------------------------------------------


class TestAutonomousLoop:
    @pytest.fixture(autouse=True)
    def _patch_session_funcs(self, monkeypatch):
        """Patch session DB functions so tests don't touch the real database."""
        # Preserve GIMMES_MODE so _autonomous_loop's os.environ write doesn't leak
        monkeypatch.setenv("GIMMES_MODE", "driving_range")
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
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run") as mock_run,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        env = mock_run.call_args.kwargs["env"]
        assert env["GIMMES_MODE"] == "driving_range"

    def test_sets_championship_mode_env(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run") as mock_run,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("championship", max_cycles=1)

        env = mock_run.call_args.kwargs["env"]
        assert env["GIMMES_MODE"] == "championship"

    def test_respects_max_cycles(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run") as mock_run,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=3, pause_seconds=0)

        assert mock_run.call_count == 3

    def test_passes_correct_claude_args(self) -> None:
        with (
            patch("shutil.which", return_value="/opt/bin/claude"),
            patch("subprocess.run") as mock_run,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        cmd = mock_run.call_args.args[0]
        assert cmd[0] == "/opt/bin/claude"
        assert "-p" in cmd
        assert "/caddy-shack" in cmd
        idx = cmd.index("--allowedTools")
        allowed = cmd[idx + 1]
        assert "WebSearch" in allowed
        assert "WebFetch" in allowed

    def test_warns_on_nonzero_exit(self, capsys) -> None:  # type: ignore[no-untyped-def]
        mock_result = MagicMock()
        mock_result.returncode = 2

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", return_value=mock_result),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        output = capsys.readouterr().out
        assert "exited with code 2" in output

    def test_keyboard_interrupt_stops_loop(self) -> None:
        call_count = 0
        ok = MagicMock(returncode=0)

        def side_effect(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise KeyboardInterrupt
            return ok

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", side_effect=side_effect),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", pause_seconds=0)

        assert call_count == 2

    def test_subprocess_failure_does_not_stop_loop(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", return_value=mock_result) as mock_run,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=2, pause_seconds=0)

        assert mock_run.call_count == 2

    def test_circuit_breaker_halts_after_consecutive_failures(
        self, capsys,
    ) -> None:  # type: ignore[no-untyped-def]
        mock_result = MagicMock()
        mock_result.returncode = 1

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", return_value=mock_result) as mock_run,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", pause_seconds=0,
                max_consecutive_failures=3,
            )

        assert mock_run.call_count == 3
        output = capsys.readouterr().out
        assert "Circuit breaker tripped" in output

    def test_circuit_breaker_resets_on_success(self) -> None:
        call_count = 0
        fail = MagicMock(returncode=1)
        ok = MagicMock(returncode=0)

        def alternate(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            # Fail twice, succeed once, fail twice, succeed once
            return fail if call_count % 3 != 0 else ok

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", side_effect=alternate) as mock_run,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=6, pause_seconds=0,
                max_consecutive_failures=3,
            )

        # Should complete all 6 cycles (never hits 3 consecutive)
        assert mock_run.call_count == 6

    def test_circuit_breaker_default_is_five(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", return_value=mock_result) as mock_run,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", pause_seconds=0)

        # Default max_consecutive_failures=5
        assert mock_run.call_count == 5


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
        with patch("gimmes.cli._set_mode"):
            result = runner.invoke(app, ["switch", "championship"], input="n\n")
            assert result.exit_code != 0

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
# Caddy-shack skill
# ---------------------------------------------------------------------------

_SKILL_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / ".claude" / "skills" / "caddy-shack" / "SKILL.md"
)


class TestCaddyShackSkill:
    def test_skill_file_exists(self) -> None:
        assert _SKILL_PATH.exists()

    def test_skill_has_frontmatter(self) -> None:
        content = _SKILL_PATH.read_text()
        assert "name: caddy-shack" in content
        assert "user_invocable: true" in content

    def test_skill_references_all_agents(self) -> None:
        content = _SKILL_PATH.read_text()
        for agent in ["Scout", "Caddie", "Closer", "Monitor", "Scorecard"]:
            assert agent in content, f"Skill should reference {agent} agent"
