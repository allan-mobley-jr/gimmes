"""Tests for gimmes autonomous loop commands (driving_range, championship)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.exceptions import Exit as ClickExit

from gimmes.cli import _autonomous_loop, app

# ---------------------------------------------------------------------------
# _autonomous_loop
# ---------------------------------------------------------------------------


class TestAutonomousLoop:
    def test_exits_when_claude_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            with pytest.raises(ClickExit):
                _autonomous_loop("driving_range")

    def test_sets_gimmes_mode_env(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run") as mock_run,
        ):
            _autonomous_loop("driving_range", max_cycles=1)

        env = mock_run.call_args.kwargs["env"]
        assert env["GIMMES_MODE"] == "driving_range"

    def test_sets_championship_mode_env(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run") as mock_run,
        ):
            _autonomous_loop("championship", max_cycles=1)

        env = mock_run.call_args.kwargs["env"]
        assert env["GIMMES_MODE"] == "championship"

    def test_respects_max_cycles(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run") as mock_run,
        ):
            _autonomous_loop("driving_range", max_cycles=3, pause_seconds=0)

        assert mock_run.call_count == 3

    def test_passes_correct_claude_args(self) -> None:
        with (
            patch("shutil.which", return_value="/opt/bin/claude"),
            patch("subprocess.run") as mock_run,
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
        ):
            _autonomous_loop("driving_range", pause_seconds=0)

        assert call_count == 2

    def test_subprocess_failure_does_not_stop_loop(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", return_value=mock_result) as mock_run,
        ):
            _autonomous_loop("driving_range", max_cycles=2, pause_seconds=0)

        assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


class TestDrivingRangeCommand:
    def test_command_exists(self) -> None:
        commands = {cmd.name for cmd in app.registered_commands}
        assert "driving_range" in commands

    def test_invokes_loop_with_driving_range_mode(self) -> None:
        with patch("gimmes.cli._autonomous_loop") as mock_loop:
            from typer.testing import CliRunner

            runner = CliRunner()
            runner.invoke(app, ["driving_range", "--cycles", "1"])

        mock_loop.assert_called_once_with(
            "driving_range", max_cycles=1, pause_seconds=0,
        )


class TestChampionshipCommand:
    def test_command_exists(self) -> None:
        commands = {cmd.name for cmd in app.registered_commands}
        assert "championship" in commands

    def test_aborts_without_confirmation(self) -> None:
        with patch("gimmes.cli._autonomous_loop") as mock_loop:
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(app, ["championship"], input="n\n")
            assert result.exit_code != 0
            mock_loop.assert_not_called()

    def test_invokes_loop_with_championship_mode(self) -> None:
        with patch("gimmes.cli._autonomous_loop") as mock_loop:
            from typer.testing import CliRunner

            runner = CliRunner()
            runner.invoke(app, ["championship", "--cycles", "1"], input="y\n")

        mock_loop.assert_called_once_with(
            "championship", max_cycles=1, pause_seconds=0,
        )


class TestOrderYesFlag:
    def test_order_command_has_yes_option(self) -> None:
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, ["order", "--help"])
        assert "--yes" in result.output


# ---------------------------------------------------------------------------
# Caddy-shack skill
# ---------------------------------------------------------------------------


class TestCaddyShackSkill:
    def test_skill_file_exists(self) -> None:
        from pathlib import Path

        skill_path = (
            Path(__file__).resolve().parent.parent.parent
            / ".claude" / "skills" / "caddy-shack" / "SKILL.md"
        )
        assert skill_path.exists()

    def test_skill_has_frontmatter(self) -> None:
        from pathlib import Path

        skill_path = (
            Path(__file__).resolve().parent.parent.parent
            / ".claude" / "skills" / "caddy-shack" / "SKILL.md"
        )
        content = skill_path.read_text()
        assert "name: caddy-shack" in content
        assert "user_invocable: true" in content

    def test_skill_references_all_agents(self) -> None:
        from pathlib import Path

        skill_path = (
            Path(__file__).resolve().parent.parent.parent
            / ".claude" / "skills" / "caddy-shack" / "SKILL.md"
        )
        content = skill_path.read_text()
        for agent in ["Scout", "Caddie", "Closer", "Monitor", "Scorecard"]:
            assert agent in content, f"Skill should reference {agent} agent"
