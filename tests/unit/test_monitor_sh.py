"""Tests for bin/monitor.sh staleness threshold (issue #529)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

MONITOR_SH = Path(__file__).parents[2] / "bin" / "monitor.sh"


@pytest.fixture()
def monitor_env(tmp_path: Path) -> dict[str, str]:
    """Set up a minimal environment for monitor.sh."""
    logs = tmp_path / "logs"
    logs.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".venv" / "bin").mkdir(parents=True)
    # Stub out gimmes CLI — risk-check and errors succeed silently
    gimmes_stub = repo / ".venv" / "bin" / "python"
    gimmes_stub.write_text("#!/bin/sh\nexit 0\n")
    gimmes_stub.chmod(0o755)

    env = {
        "GIMMES_HOME": str(tmp_path),
        "PATH": str(repo / ".venv" / "bin") + ":" + os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    return env


class TestStalenessThreshold:

    def test_fresh_cycle_log_passes(self, tmp_path: Path, monitor_env: dict) -> None:
        """A cycle log created just now should not trigger an alert."""
        logs = tmp_path / "logs"
        (logs / "cycle-1.json").write_text("{}")

        result = subprocess.run(
            ["bash", str(MONITOR_SH), "--quiet"],
            env=monitor_env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_stale_cycle_log_alerts(self, tmp_path: Path, monitor_env: dict) -> None:
        """A cycle log older than the threshold should trigger an alert."""
        logs = tmp_path / "logs"
        log_file = logs / "cycle-1.json"
        log_file.write_text("{}")
        # Set mtime to 4 hours ago (> default 3h threshold)
        old_time = time.time() - 14400
        os.utime(log_file, (old_time, old_time))

        result = subprocess.run(
            ["bash", str(MONITOR_SH), "--quiet"],
            env=monitor_env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1

    def test_env_var_overrides_threshold(
        self, tmp_path: Path, monitor_env: dict,
    ) -> None:
        """GIMMES_STALENESS_THRESHOLD env var should override the default."""
        logs = tmp_path / "logs"
        log_file = logs / "cycle-1.json"
        log_file.write_text("{}")
        # Set mtime to 2 hours ago
        old_time = time.time() - 7200
        os.utime(log_file, (old_time, old_time))

        # Default threshold is 3h (10800s) — 2h-old file should pass
        result = subprocess.run(
            ["bash", str(MONITOR_SH), "--quiet"],
            env=monitor_env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, "2h-old file should pass 3h threshold"

        # Override threshold to 1h (3600s) — 2h-old file should alert
        monitor_env["GIMMES_STALENESS_THRESHOLD"] = "3600"
        result = subprocess.run(
            ["bash", str(MONITOR_SH), "--quiet"],
            env=monitor_env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1, "2h-old file should fail 1h threshold"

    def test_default_threshold_is_three_hours(
        self, tmp_path: Path, monitor_env: dict,
    ) -> None:
        """With no override, default 3h threshold passes 2h-old but fails 4h-old."""
        logs = tmp_path / "logs"
        log_file = logs / "cycle-1.json"
        log_file.write_text("{}")

        # 2h-old should pass 3h default
        os.utime(log_file, (time.time() - 7200,) * 2)
        result = subprocess.run(
            ["bash", str(MONITOR_SH), "--quiet"],
            env=monitor_env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, "2h-old file should pass 3h default"

        # 4h-old should fail 3h default
        os.utime(log_file, (time.time() - 14400,) * 2)
        result = subprocess.run(
            ["bash", str(MONITOR_SH), "--quiet"],
            env=monitor_env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1, "4h-old file should fail 3h default"
