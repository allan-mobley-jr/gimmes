"""Tests for gimmes tour_guide command (The Starter)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.exceptions import Exit as ClickExit

from gimmes.cli import app, tour_guide

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


class TestTourGuideCommand:
    def test_command_exists(self) -> None:
        commands = {cmd.name for cmd in app.registered_commands}
        assert "tour_guide" in commands

    def test_exits_when_claude_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            with pytest.raises(ClickExit) as exc_info:
                tour_guide()
            assert exc_info.value.exit_code == 1

    def test_passes_correct_subprocess_args(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
            patch("getpass.getuser", return_value="testplayer"),
        ):
            tour_guide()

        cmd = mock_run.call_args.args[0]
        assert cmd[0] == "/usr/bin/claude"
        assert "--agent" in cmd
        assert "Starter" in cmd
        assert "--name" in cmd
        assert "GIMMES Tour" in cmd
        # Initial prompt is a positional arg (not -p which is non-interactive)
        assert cmd[-1] == "Hi, I am testplayer"
        assert "-p" not in cmd
        # Tools pre-approved so agent runs without permission prompts
        allowed = set(cmd[cmd.index("--allowedTools") + 1].split(","))
        assert {"Bash", "WebSearch", "WebFetch"} <= allowed
        assert mock_run.call_args.kwargs["cwd"] is not None

    def test_username_fallback_on_getuser_failure(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
            patch("getpass.getuser", side_effect=KeyError("getpwuid")),
        ):
            tour_guide()

        cmd = mock_run.call_args.args[0]
        assert cmd[-1] == "Hi, I am there"

    def test_reports_nonzero_exit(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", return_value=MagicMock(returncode=2)),
        ):
            with pytest.raises(ClickExit) as exc_info:
                tour_guide()
            assert exc_info.value.exit_code == 1

    def test_keyboard_interrupt_exits_130(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", side_effect=KeyboardInterrupt),
        ):
            with pytest.raises(ClickExit) as exc_info:
                tour_guide()
            assert exc_info.value.exit_code == 130

    def test_os_error_exits_with_message(self, capsys) -> None:  # type: ignore[no-untyped-def]
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", side_effect=OSError("Permission denied")),
        ):
            with pytest.raises(ClickExit) as exc_info:
                tour_guide()
            assert exc_info.value.exit_code == 1

        output = capsys.readouterr().out
        assert "Permission denied" in output


# ---------------------------------------------------------------------------
# Agent files
# ---------------------------------------------------------------------------


class TestStarterAgent:
    _agent_path = _PROJECT_ROOT / ".claude" / "agents" / "starter.md"

    def test_agent_file_exists(self) -> None:
        assert self._agent_path.exists()

    def test_agent_has_frontmatter(self) -> None:
        content = self._agent_path.read_text()
        assert "name: Starter" in content
        assert "tools:" in content


