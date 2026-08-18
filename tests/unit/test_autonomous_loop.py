"""Tests for gimmes autonomous loop commands (driving_range, championship)."""

from __future__ import annotations

import json as _json
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
    _classify_timeout_outcome,
    _communicate_interruptible,
    _detect_api_error,
    _detect_rate_limit,
    _extract_terminal_text,
    _position_window_hit,
    _set_mode,
    _wrap_stream_json,
    app,
)
from gimmes.config import GimmesConfig, Mode, RiskConfig, ScannerConfig

runner = CliRunner()


def _make_future_dt() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def _mock_popen(returncode: int = 0, output: bytes = b"") -> MagicMock:
    """Return a mock Popen instance with the given returncode and stdout output."""
    mock_proc = MagicMock()
    # Chunked-read semantics (#761): first read returns the output,
    # the next returns b"" (EOF) — mirroring a real pipe.
    mock_proc.stdout.read.side_effect = [output, b""]
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
            # #788: _apply_failure_backoff sleeps 30*2^(n-1)s (cap
            # 240) for REAL between failures — this file alone was
            # 961s of every ~976s frozen gate, and the default-five
            # breaker test slept 450s, indistinguishable from a
            # hang. No-op at the fixture level; tests that need
            # sleep behavior override with their own inner patch.
            patch("gimmes.cli._resilient_sleep"),
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

    def test_loop_blocks_when_session_cap_already_reached(
        self, tmp_path: Path,
    ) -> None:
        """Pre-spawn budget check skips the cycle when session cap is hit (#545)."""
        # Freeze the tracker's clock so the seeded date and the loop's
        # observed date can't diverge across a UTC midnight boundary.
        _frozen = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        from gimmes.config import GIMMES_HOME
        budget_path = GIMMES_HOME / "budget.json"
        today = _frozen.date().isoformat()
        budget_path.parent.mkdir(parents=True, exist_ok=True)
        budget_path.write_text(_json.dumps({
            "version": 1,
            "days": {today: {"sessions": 5, "cost_usd": 0.0}},
            "caps": {"max_sessions": 5, "max_cost_usd": 25.0},
        }))

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen") as mock_popen,
            patch("gimmes.cli._resilient_sleep", side_effect=KeyboardInterrupt),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.budget._default_clock", lambda: _frozen),
        ):
            _autonomous_loop(
                "driving_range",
                max_cycles=1,
                pause_seconds=0,
                max_sessions_per_day=5,
            )

        # Loop should hit pre-spawn block, sleep, get interrupted — never spawn.
        mock_popen.assert_not_called()

    def test_loop_records_session_when_usage_unparseable(
        self, tmp_path: Path,
    ) -> None:
        """If the stream-json stdout has no parseable usage, the cycle
        still counts toward the session cap (Anthropic charged for it)."""
        _frozen = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        from gimmes.config import GIMMES_HOME
        budget_path = GIMMES_HOME / "budget.json"

        # stdout with no parseable JSON / no usage block.
        stream_json = b"random non-json terminal noise\n"
        mock_proc = _mock_popen(returncode=0, output=stream_json)

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch(
                "gimmes.cli._communicate_interruptible",
                return_value=stream_json,
            ),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.budget._default_clock", lambda: _frozen),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        assert budget_path.exists()
        data = _json.loads(budget_path.read_text())
        today = _frozen.date().isoformat()
        entry = data["days"][today]
        assert entry["sessions"] == 1
        assert entry["cost_usd"] == 0.0
        assert entry["input_tokens"] == 0

    def test_loop_writes_block_log_on_cap_hit(self, tmp_path: Path) -> None:
        """When the budget cap blocks the cycle, a cycle-NNN-block-*.json
        log is written for remote operators."""
        _frozen = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        from gimmes.config import GIMMES_HOME
        budget_path = GIMMES_HOME / "budget.json"
        logs_dir = GIMMES_HOME / "logs"
        today = _frozen.date().isoformat()
        budget_path.parent.mkdir(parents=True, exist_ok=True)
        budget_path.write_text(_json.dumps({
            "version": 1,
            "days": {today: {"sessions": 5, "cost_usd": 0.0}},
            "caps": {"max_sessions": 5, "max_cost_usd": 25.0},
        }))

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen") as mock_popen,
            patch("gimmes.cli._resilient_sleep", side_effect=KeyboardInterrupt),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.budget._default_clock", lambda: _frozen),
        ):
            _autonomous_loop(
                "driving_range",
                max_cycles=1,
                pause_seconds=0,
                max_sessions_per_day=5,
            )

        mock_popen.assert_not_called()
        # A block log should be written under logs/.
        block_logs = list(logs_dir.glob("cycle-*-block-*.json"))
        assert len(block_logs) == 1, (
            f"Expected one block log, found {block_logs}"
        )
        block = _json.loads(block_logs[0].read_text())
        assert block["type"] == "budget_cap_block"
        assert block["reason"] == "sessions"
        assert "seconds_until_reset" in block

    def test_loop_records_usage_after_successful_cycle(
        self, tmp_path: Path,
    ) -> None:
        """Loop parses usage from stream-json stdout and records to budget.json."""
        _frozen = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        from gimmes.config import GIMMES_HOME
        budget_path = GIMMES_HOME / "budget.json"

        # Build a stream-json stdout with a usage block.
        stream_json = (
            b'{"type":"system","subtype":"init"}\n'
            b'{"type":"result","is_error":false,"usage":'
            b'{"input_tokens":1000000,"output_tokens":0,'
            b'"cache_creation_input_tokens":0,'
            b'"cache_read_input_tokens":0}}'
        )
        mock_proc = _mock_popen(returncode=0, output=stream_json)

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc),
            patch(
                "gimmes.cli._communicate_interruptible",
                return_value=stream_json,
            ),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.budget._default_clock", lambda: _frozen),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        # 1M input tokens at Sonnet rate ($3/M) = $3 expected cost.
        assert budget_path.exists()
        data = _json.loads(budget_path.read_text())
        today = _frozen.date().isoformat()
        assert today in data["days"]
        entry = data["days"][today]
        assert entry["sessions"] == 1
        assert entry["cost_usd"] == pytest.approx(3.0, abs=1e-6)
        # Token totals must accumulate too — guards the integration against
        # a regression where cost is recorded but tokens are dropped.
        assert entry["input_tokens"] == 1_000_000
        assert entry["output_tokens"] == 0

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

        def comm_side_effect(proc, timeout, **kwargs):
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

    def test_timeout_writes_partial_cycle_log(self, tmp_path) -> None:
        """A clamp-killed cycle writes the bytes it streamed (#761)."""
        partial = b'{"step": "conferral"}'
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(
                    cmd="claude", timeout=2700, output=partial,
                ),
            ),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=1,
                max_consecutive_failures=1,
            )

        log_file = tmp_path / "logs" / "cycle-001.json"
        assert log_file.exists()
        assert log_file.read_bytes() == partial

    def test_timeout_after_complete_marker_resets_failures(
        self, tmp_path, capsys,
    ) -> None:
        """c2134 shape (#761): 'Cycle N complete' logged before the
        clamp — the kill is a process-exit race, not a failure."""
        _seed_activity_log(
            tmp_path / "gimmes.db", [(1, "Cycle 1 complete", _ts(300))],
        )
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(
                    cmd="claude", timeout=2700,
                ),
            ),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=1, pause_seconds=0,
                max_consecutive_failures=1,
            )

        output = capsys.readouterr().out
        assert "completed but overran" in output
        assert "Circuit breaker tripped" not in output

    def test_complete_classified_timeout_resets_accumulated_failures(
        self, tmp_path, capsys,
    ) -> None:
        """The reset is only observable with a NONZERO counter: cycles
        1 and 3 are plain failures, cycle 2 classifies complete — with
        max=2 the breaker never trips because the counter went
        1 → 0 → 1, not 1 → 1 → 2 (#761)."""
        _seed_activity_log(
            tmp_path / "gimmes.db", [(2, "Cycle 2 complete", _ts(300))],
        )
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(
                    cmd="claude", timeout=2700,
                ),
            ),
            patch("gimmes.cli._resilient_sleep"),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=3, pause_seconds=0,
                max_consecutive_failures=2,
            )

        output = capsys.readouterr().out
        assert output.count("failure 1/2") == 2
        assert "failure 2/2" not in output
        assert "Circuit breaker tripped" not in output

    def test_timeout_after_trade_path_not_counted(
        self, tmp_path, capsys,
    ) -> None:
        """Hourly clamp shape (#761): Closer concluded, clamp truncated
        only post-trade steps — breaker untouched."""
        _seed_activity_log(
            tmp_path / "gimmes.db",
            [(1, "Closer executed 1 trades", _ts(300))],
        )
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(
                    cmd="claude", timeout=1740,
                ),
            ),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=1, pause_seconds=0,
                max_consecutive_failures=1,
            )

        # Rich wraps console lines — normalize before phrase asserts
        output = " ".join(capsys.readouterr().out.split())
        assert "post-trade steps truncated" in output
        assert "not counted as a failure" in output
        assert "Circuit breaker tripped" not in output

    def test_trade_path_done_preserves_failure_counter(
        self, tmp_path, capsys,
    ) -> None:
        """trade_path_done must PRESERVE the counter, not reset it:
        failure (1/2) → trade-path kill (unchanged) → failure trips at
        2/2 (#761). Fails if the branch resets like 'complete'."""
        _seed_activity_log(
            tmp_path / "gimmes.db",
            [(2, "Closer executed 1 trades", _ts(300))],
        )
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(
                    cmd="claude", timeout=2700,
                ),
            ),
            patch("gimmes.cli._resilient_sleep"),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=3, pause_seconds=0,
                max_consecutive_failures=2,
            )

        output = capsys.readouterr().out
        assert "failure 2/2" in output
        assert "Circuit breaker tripped" in output

    def test_failure_breaks_trade_path_kill_run(
        self, tmp_path, capsys,
    ) -> None:
        """A real failure resets the unbroken-run counter: TPD, TPD,
        failure, TPD → never reaches 3 consecutive, no shed warning
        (Copilot-review-found on #763)."""
        _seed_activity_log(
            tmp_path / "gimmes.db",
            [
                (1, "Closer executed 1 trades", _ts(300)),
                (2, "Closer executed 1 trades", _ts(300)),
                (4, "Closer executed 1 trades", _ts(300)),
            ],
        )
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(
                    cmd="claude", timeout=2700,
                ),
            ),
            patch("gimmes.cli._resilient_sleep"),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=4, pause_seconds=0,
                max_consecutive_failures=5,
            )

        output = " ".join(capsys.readouterr().out.split())
        assert "consecutive clamp kills have truncated" not in output

    def test_three_consecutive_trade_path_kills_warn(
        self, tmp_path, capsys,
    ) -> None:
        """An UNBROKEN run of 3 trade-path kills prints the shed
        warning — post-trade surveillance shed every cycle is a
        capacity regression the operator must see (#761)."""
        _seed_activity_log(
            tmp_path / "gimmes.db",
            [
                (1, "Closer executed 1 trades", _ts(300)),
                (2, "Closer executed 1 trades", _ts(300)),
                (3, "Closer executed 1 trades", _ts(300)),
            ],
        )
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(
                    cmd="claude", timeout=2700,
                ),
            ),
            patch("gimmes.cli._resilient_sleep"),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=3, pause_seconds=0,
                max_consecutive_failures=5,
            )

        output = " ".join(capsys.readouterr().out.split())
        assert "3 consecutive clamp kills have truncated" in output

    def test_timeout_without_output_writes_empty_log(
        self, tmp_path,
    ) -> None:
        """Old-style TimeoutExpired (no output attached) still writes
        the log file — empty, not absent (#761)."""
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=_subprocess.TimeoutExpired(
                    cmd="claude", timeout=2700,
                ),
            ),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=1, pause_seconds=0,
                max_consecutive_failures=1,
            )

        log_file = tmp_path / "logs" / "cycle-001.json"
        assert log_file.exists()
        assert log_file.read_bytes() == b""

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
            # #788: the autouse fixture no-ops _resilient_sleep, so
            # the interrupt must come from the sleep seam itself.
            patch(
                "gimmes.cli._resilient_sleep",
                side_effect=KeyboardInterrupt,
            ),
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

        def comm_side_effect(proc, timeout, **kwargs):
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
        mock_proc.stdout.read = lambda *_: (block.wait(), b"")[-1]

        with pytest.raises(_subprocess.TimeoutExpired):
            _communicate_interruptible(mock_proc, timeout=0.1)

        block.set()  # Let daemon thread exit cleanly

    def test_timeout_attaches_partial_output(self) -> None:
        """Bytes streamed before the deadline ride the exception (#761)."""
        import threading

        block = threading.Event()
        chunks = [b'{"step": "research"}\n']

        def _read(*_):
            if chunks:
                return chunks.pop(0)
            block.wait()
            return b""

        mock_proc = MagicMock()
        mock_proc.args = ["claude"]
        mock_proc.stdout.read = _read

        with pytest.raises(_subprocess.TimeoutExpired) as excinfo:
            _communicate_interruptible(mock_proc, timeout=0.3)

        block.set()
        assert excinfo.value.output == b'{"step": "research"}\n'

    def test_timeout_accumulates_multiple_chunks(self) -> None:
        """Chunks must ACCUMULATE, not overwrite (#761)."""
        import threading

        block = threading.Event()
        chunks = [b'{"a": 1}\n', b'{"b": 2}\n']

        def _read(*_):
            if chunks:
                return chunks.pop(0)
            block.wait()
            return b""

        mock_proc = MagicMock()
        mock_proc.args = ["claude"]
        mock_proc.stdout.read = _read

        with pytest.raises(_subprocess.TimeoutExpired) as excinfo:
            _communicate_interruptible(mock_proc, timeout=0.3)

        block.set()
        assert excinfo.value.output == b'{"a": 1}\n{"b": 2}\n'

    def test_success_concatenates_chunks(self) -> None:
        mock_proc = _mock_popen()
        mock_proc.stdout.read.side_effect = [b"cycle ", b"output", b""]

        result = _communicate_interruptible(mock_proc, timeout=10)

        assert result == b"cycle output"

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
# _classify_timeout_outcome (#761)
# ---------------------------------------------------------------------------


def _seed_activity_log(
    db_path: Path, rows: list[tuple[int, str, str]],
) -> None:
    """Create a minimal activity_log and insert (cycle, message, ts) rows."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle INTEGER NOT NULL DEFAULT 0,
            agent TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            details TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.executemany(
        "INSERT INTO activity_log (cycle, message, timestamp)"
        " VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _ts(offset_seconds: int) -> str:
    """UTC activity_log timestamp string offset from now."""
    return (
        datetime.now(UTC) + timedelta(seconds=offset_seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")


class TestClassifyTimeoutOutcome:
    """#761: clamp kills are classified from the cycle's own
    activity_log trail — completed cycles and concluded trade paths
    must not feed the circuit breaker."""

    @pytest.fixture()
    def since(self) -> datetime:
        return datetime.now(UTC) - timedelta(seconds=1)

    def test_complete_marker(self, tmp_path, since) -> None:
        db = tmp_path / "gimmes.db"
        _seed_activity_log(
            db, [(7, "Cycle 7 complete [DEADLINE-SHED: Step 2]", _ts(60))],
        )
        assert _classify_timeout_outcome(db, 7, since) == "complete"

    @pytest.mark.parametrize("marker", [
        "Closer executed 1 trades",
        "Closer executed 0 trades",
    ])
    def test_closer_concluded_markers(
        self, tmp_path, since, marker,
    ) -> None:
        db = tmp_path / "gimmes.db"
        _seed_activity_log(db, [(7, marker, _ts(60))])
        assert (
            _classify_timeout_outcome(db, 7, since) == "trade_path_done"
        )

    def test_dispatch_marker_alone_is_failure(self, tmp_path, since) -> None:
        # "Hourly 4c: dispatching Closer" fires BEFORE the Closer runs
        # — a Closer hanging mid-order-placement must stay a failure,
        # or the breaker goes blind to a permanently hanging Closer
        # (review-found).
        db = tmp_path / "gimmes.db"
        _seed_activity_log(
            db, [(7, "Hourly 4c: dispatching Closer", _ts(60))],
        )
        assert _classify_timeout_outcome(db, 7, since) == "failure"

    def test_complete_outranks_trade_path(self, tmp_path, since) -> None:
        db = tmp_path / "gimmes.db"
        _seed_activity_log(db, [
            (7, "Closer executed 1 trades", _ts(50)),
            (7, "Cycle 7 complete", _ts(60)),
        ])
        assert _classify_timeout_outcome(db, 7, since) == "complete"

    def test_no_markers_is_failure(self, tmp_path, since) -> None:
        db = tmp_path / "gimmes.db"
        _seed_activity_log(
            db, [(7, "Monitor checking open positions", _ts(60))],
        )
        assert _classify_timeout_outcome(db, 7, since) == "failure"

    def test_other_cycles_markers_do_not_match(
        self, tmp_path, since,
    ) -> None:
        db = tmp_path / "gimmes.db"
        _seed_activity_log(db, [(6, "Cycle 6 complete", _ts(60))])
        assert _classify_timeout_outcome(db, 7, since) == "failure"

    def test_marker_from_before_this_run_is_ignored(
        self, tmp_path, since,
    ) -> None:
        # Fresh-DB restart guard: a same-numbered cycle from an earlier
        # loop run sits BEFORE since_utc and must not match.
        db = tmp_path / "gimmes.db"
        _seed_activity_log(db, [(7, "Cycle 7 complete", _ts(-3600))])
        assert _classify_timeout_outcome(db, 7, since) == "failure"

    def test_unreadable_db_is_failure(self, tmp_path, since) -> None:
        # Conservative default: telemetry problems never blind the
        # circuit breaker.
        missing = tmp_path / "nope.db"
        assert _classify_timeout_outcome(missing, 7, since) == "failure"


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
            max_sessions_per_day=None, max_daily_cost_usd=None,
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
            max_sessions_per_day=None, max_daily_cost_usd=None,
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
            max_sessions_per_day=None, max_daily_cost_usd=None,
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
            max_sessions_per_day=None, max_daily_cost_usd=None,
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
            max_sessions_per_day=None, max_daily_cost_usd=None,
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
            max_sessions_per_day=None, max_daily_cost_usd=None,
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
            max_sessions_per_day=None, max_daily_cost_usd=None,
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
            max_sessions_per_day=None, max_daily_cost_usd=None,
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
            max_sessions_per_day=None, max_daily_cost_usd=None,
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
            max_sessions_per_day=None, max_daily_cost_usd=None,
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
            max_sessions_per_day=None, max_daily_cost_usd=None,
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

    @pytest.fixture(autouse=True)
    def _isolate_loop(self, tmp_path, monkeypatch):
        """Isolate the loop from real DB, asyncio, and calendar so the
        retry/backoff path is deterministic."""
        monkeypatch.setenv("GIMMES_MODE", "driving_range")
        monkeypatch.setattr("gimmes.config.GIMMES_HOME", tmp_path)
        with (
            patch("gimmes.store.session.create_session", return_value=1),
            patch("gimmes.store.session.end_session"),
            patch("gimmes.store.session.mark_stale_sessions", return_value=0),
            patch("gimmes.store.session.update_session_cycle"),
            patch("gimmes.store.session.close_orphan_activities", return_value=0),
            patch("gimmes.store.session.get_max_global_cycle", return_value=0),
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
        ):
            yield

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
            patch("gimmes.cli._resilient_sleep") as mock_sleep,
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
            patch("gimmes.cli._resilient_sleep"),
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
            patch("gimmes.cli._resilient_sleep"),
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


class TestPositionWindowHit:
    """#723: the pure position-window seam — hourly tickers are excluded
    so a held sub-hour position can't trigger full cycles every 60s."""

    @staticmethod
    def _config(hourly_series: list[str]) -> GimmesConfig:
        return GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            scanner=ScannerConfig(hourly_series=hourly_series),
        )

    def test_excludes_hourly_ticker(self) -> None:
        now = datetime.now(UTC)
        close_times = [
            ("KXBTCD-26JUN23H14-T119999.99", now + timedelta(minutes=30)),
            ("KXINX-26JUN23-B5000", now + timedelta(hours=2)),
        ]
        hit, ticker = _position_window_hit(
            close_times, self._config(["KXBTCD"]), now,
        )
        assert hit is True
        assert ticker == "KXINX-26JUN23-B5000"

    def test_only_hourly_positions_no_hit(self) -> None:
        now = datetime.now(UTC)
        close_times = [
            ("KXBTCD-26JUN23H14-T119999.99", now + timedelta(minutes=30)),
        ]
        assert _position_window_hit(
            close_times, self._config(["KXBTCD"]), now,
        ) == (False, None)

    def test_inert_when_hourly_series_empty(self) -> None:
        # Without the hourly opt-in the old behavior is unchanged: a
        # KXBTCD position inside its 18h window IS a hit
        now = datetime.now(UTC)
        close_times = [
            ("KXBTCD-26JUN23H14-T119999.99", now + timedelta(minutes=30)),
        ]
        hit, ticker = _position_window_hit(
            close_times, self._config([]), now,
        )
        assert hit is True
        assert ticker == "KXBTCD-26JUN23H14-T119999.99"

    def test_outside_window_no_hit(self) -> None:
        now = datetime.now(UTC)
        close_times = [("KXINX-26JUN23-B5000", now + timedelta(hours=30))]
        assert _position_window_hit(
            close_times, self._config([]), now,
        ) == (False, None)


class TestHourlyLadder:
    """#723: the third 'hourly' cycle type — window gating, one cycle
    per window, timeout clamp, sleep integration, 24h cadence."""

    @pytest.fixture(autouse=True)
    def _patch_session_funcs(self, tmp_path, monkeypatch):
        """Same isolation as TestAutonomousLoop, but OUTSIDE any release
        window by default — the fixture's is_in_trade_window=True would
        mask every hourly path (release wins the precedence ladder)."""
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
                return_value=(False, None, None),
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

    @staticmethod
    def _hourly_load_config(**scanner_overrides):
        """load_config side_effect injecting hourly_series=['KXBTCD']."""
        from gimmes import cli as gimmes_cli

        original = gimmes_cli.load_config
        scanner_overrides.setdefault("hourly_series", ["KXBTCD"])

        def patched(*args, **kwargs):  # type: ignore[no-untyped-def]
            cfg = original(*args, **kwargs)
            return cfg.model_copy(update={
                "scanner": cfg.scanner.model_copy(update=scanner_overrides),
            })

        return patched

    @staticmethod
    def _dt_relative_window(remaining_seconds: int):
        """hourly_window stub: close at dt + remaining — cli computes
        _remaining from the same dt it passes, so the clamp is exact."""
        def _hw(dt=None, *, lead_minutes):  # type: ignore[no-untyped-def]
            return (
                dt - timedelta(minutes=4),
                dt + timedelta(seconds=remaining_seconds),
            )
        return _hw

    def test_hourly_prompt_and_env(self) -> None:
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli.load_config", side_effect=self._hourly_load_config()),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: True,
            ),
            patch(
                "gimmes.strategy.calendar.hourly_window",
                self._dt_relative_window(1500),
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        env = mock_popen.call_args.kwargs["env"]
        assert env["GIMMES_CYCLE_TYPE"] == "hourly"
        cmd = mock_popen.call_args.args[0]
        prompt = cmd[cmd.index("-p") + 1]
        assert "HOURLY-LADDER" in prompt
        assert "KXBTCD" in prompt
        # Step 2 (Monitor/stop-loss backstop) and 6.5 (Groundskeeper)
        # ride the hourly lane (#724); Step 2 runs AFTER the trade path
        # so a slow surveillance pass can never block the entry (#732)
        assert "0, 0.5, 1, 3, 4, 4c, 5, 2, 6.5, and 8" in prompt
        assert "AFTER Step 5" in prompt
        assert "Skip Scorecard and Pro" in prompt

    def test_hourly_disabled_when_series_empty(self) -> None:
        # Default config: the hourly gate is bool(hourly_series) — the
        # calendar helpers must never even be consulted. max_cycles=2 so
        # the post-cycle monitor recompute (which has its own
        # hourly_enabled guard) is also exercised: the loop breaks
        # BEFORE the sleep on the final cycle, so a single cycle would
        # never reach that block. The real seconds_until_next_hourly_open
        # delegates to the patched module-global hourly_window, so the
        # spies catch a dropped guard at either site transitively.
        hw_spy = MagicMock()
        in_hw_spy = MagicMock()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.Popen",
                side_effect=lambda *a, **kw: _mock_popen(),
            ) as mock_popen,
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli._resilient_sleep"),
            patch("gimmes.strategy.calendar.is_in_hourly_window", in_hw_spy),
            patch("gimmes.strategy.calendar.hourly_window", hw_spy),
        ):
            _autonomous_loop("driving_range", max_cycles=2, pause_seconds=0)

        env = mock_popen.call_args.kwargs["env"]
        assert env["GIMMES_CYCLE_TYPE"] == "monitor"
        in_hw_spy.assert_not_called()
        hw_spy.assert_not_called()

    def test_one_cycle_per_window(self) -> None:
        # A FIXED absolute window (same close instant every call → same
        # key): the second and third iterations must fall to monitor
        close = datetime.now(UTC) + timedelta(seconds=1500)
        window = (close - timedelta(minutes=29), close)
        types: list[str] = []

        def popen_side_effect(*args, **kwargs):  # type: ignore[no-untyped-def]
            types.append(kwargs["env"]["GIMMES_CYCLE_TYPE"])
            return _mock_popen()

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=popen_side_effect),
            patch(
                "gimmes.cli._communicate_interruptible", return_value=b"",
            ) as mock_comm,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli._resilient_sleep"),
            patch("gimmes.cli.load_config", side_effect=self._hourly_load_config()),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: True,
            ),
            patch(
                "gimmes.strategy.calendar.hourly_window",
                lambda dt=None, *, lead_minutes: window,
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_hourly_open",
                lambda dt=None, *, lead_minutes: 1860,
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=3, pause_seconds=0)

        assert types == ["hourly", "monitor", "monitor"]
        # The clamp must not leak into later cycles: cycle 1 is clamped
        # to the window remainder, cycles 2-3 (monitor) run unclamped
        timeouts = [c.kwargs["timeout"] for c in mock_comm.call_args_list]
        assert timeouts[0] <= 1500
        assert timeouts[1:] == [2700, 2700]

    def test_max_cycles_per_window_gt_one(self) -> None:
        close = datetime.now(UTC) + timedelta(seconds=1500)
        window = (close - timedelta(minutes=29), close)
        types: list[str] = []

        def popen_side_effect(*args, **kwargs):  # type: ignore[no-untyped-def]
            types.append(kwargs["env"]["GIMMES_CYCLE_TYPE"])
            return _mock_popen()

        sleep_spy = MagicMock()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=popen_side_effect),
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli._resilient_sleep", sleep_spy),
            patch(
                "gimmes.cli.load_config",
                side_effect=self._hourly_load_config(
                    hourly_max_cycles_per_window=2,
                ),
            ),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: True,
            ),
            patch(
                "gimmes.strategy.calendar.hourly_window",
                lambda dt=None, *, lead_minutes: window,
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_hourly_open",
                lambda dt=None, *, lead_minutes: 1860,
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=3, pause_seconds=0)

        assert types == ["hourly", "hourly", "monitor"]
        # Post-hourly with count < max sleeps pause_seconds (0), NOT the
        # to-next-open path — otherwise max>1 silently degrades to 1
        # effective cycle per window
        assert sleep_spy.call_args_list[0].args[0] == 0

    def test_release_precedence_over_hourly(self) -> None:
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli.load_config", side_effect=self._hourly_load_config()),
            patch(
                "gimmes.strategy.calendar.is_in_trade_window",
                return_value=(True, "Index contracts", 3600),
            ),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: True,
            ),
            patch(
                "gimmes.strategy.calendar.hourly_window",
                self._dt_relative_window(1500),
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        env = mock_popen.call_args.kwargs["env"]
        assert env["GIMMES_CYCLE_TYPE"] == "full"
        cmd = mock_popen.call_args.args[0]
        assert cmd[cmd.index("-p") + 1] == "Run one trading cycle."

    def test_hourly_timeout_clamped(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=_mock_popen()),
            patch(
                "gimmes.cli._communicate_interruptible", return_value=b"",
            ) as mock_comm,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli.load_config", side_effect=self._hourly_load_config()),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: True,
            ),
            patch(
                "gimmes.strategy.calendar.hourly_window",
                self._dt_relative_window(1500),
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        assert mock_comm.call_args.kwargs["timeout"] == 1500

    def test_hourly_timeout_not_clamped_when_window_long(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=_mock_popen()),
            patch(
                "gimmes.cli._communicate_interruptible", return_value=b"",
            ) as mock_comm,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli.load_config", side_effect=self._hourly_load_config()),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: True,
            ),
            patch(
                "gimmes.strategy.calendar.hourly_window",
                self._dt_relative_window(5000),
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        assert mock_comm.call_args.kwargs["timeout"] == 2700

    def test_hourly_skipped_when_too_late(self) -> None:
        # 60s remaining < HOURLY_MIN_CYCLE_SECONDS: fall through to
        # monitor, full timeout — no straggler cycle can order into a
        # settled market
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch(
                "gimmes.cli._communicate_interruptible", return_value=b"",
            ) as mock_comm,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli.load_config", side_effect=self._hourly_load_config()),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: True,
            ),
            patch(
                "gimmes.strategy.calendar.hourly_window",
                self._dt_relative_window(60),
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_hourly_open",
                lambda dt=None, *, lead_minutes: 3600,
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        env = mock_popen.call_args.kwargs["env"]
        assert env["GIMMES_CYCLE_TYPE"] == "monitor"
        assert mock_comm.call_args.kwargs["timeout"] == 2700

    def test_monitor_sleep_and_display_consider_hourly(self, capsys) -> None:
        sleep_spy = MagicMock()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=lambda *a, **kw: _mock_popen()),
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli._resilient_sleep", sleep_spy),
            patch("gimmes.cli.load_config", side_effect=self._hourly_load_config()),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: False,
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_window",
                return_value=7200,
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_hourly_open",
                lambda dt=None, *, lead_minutes: 900,
            ),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=2, pause_seconds=0,
                monitor_interval=3600,
            )

        # Both the decision-time sleep and the post-cycle recompute must
        # take the sooner hourly open (900 < 7200 release, < 3600 interval)
        assert sleep_spy.call_args_list[0].args[0] == 900
        assert "Hourly ladder" in capsys.readouterr().out

    def test_post_hourly_sleep_lands_at_next_open(self) -> None:
        # After an exhausted hourly window, sleep to the next open —
        # NOT clamped by monitor_interval, no intermediate monitor cycle
        sleep_spy = MagicMock()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=lambda *a, **kw: _mock_popen()),
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli._resilient_sleep", sleep_spy),
            patch("gimmes.cli.load_config", side_effect=self._hourly_load_config()),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: True,
            ),
            patch(
                "gimmes.strategy.calendar.hourly_window",
                self._dt_relative_window(1500),
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_window",
                return_value=7200,
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_hourly_open",
                lambda dt=None, *, lead_minutes: 1860,
            ),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=2, pause_seconds=0,
                monitor_interval=120,
            )

        assert sleep_spy.call_args_list[0].args[0] == 1860

    def test_24h_cadence_simulation(self) -> None:
        """The budget guardrail as a test: a full simulated day with
        hourly enabled and no release windows runs exactly 24 hourly
        cycles + 1 leading monitor cycle — no window slept through, no
        intermediate monitor sessions, sessions/day bounded at 25."""
        from zoneinfo import ZoneInfo

        from gimmes.strategy.calendar import (
            hourly_window as real_hw,
        )
        from gimmes.strategy.calendar import (
            is_in_hourly_window as real_in_hw,
        )
        from gimmes.strategy.calendar import (
            seconds_until_next_hourly_open as real_snho,
        )

        et = ZoneInfo("America/New_York")
        clock = {"now": datetime(2026, 4, 7, 0, 0, tzinfo=et)}  # a Tuesday
        end = clock["now"] + timedelta(hours=24)
        types: list[str] = []

        def fake_in_hw(dt=None, *, lead_minutes):  # type: ignore[no-untyped-def]
            return real_in_hw(clock["now"], lead_minutes=lead_minutes)

        def fake_hw(dt=None, *, lead_minutes):  # type: ignore[no-untyped-def]
            # Delegate to the real impl at the simulated instant, then
            # re-anchor to the caller's dt (the loop's real now) so its
            # `_remaining = close - now` arithmetic stays exact
            o, c = real_hw(clock["now"], lead_minutes=lead_minutes)
            rem_open = (o - clock["now"]).total_seconds()
            rem_close = (c - clock["now"]).total_seconds()
            base = dt if dt is not None else clock["now"]
            return (
                base + timedelta(seconds=rem_open),
                base + timedelta(seconds=rem_close),
            )

        def fake_snho(dt=None, *, lead_minutes):  # type: ignore[no-untyped-def]
            return real_snho(clock["now"], lead_minutes=lead_minutes)

        def fake_sleep(secs):  # type: ignore[no-untyped-def]
            clock["now"] += timedelta(seconds=secs)
            if clock["now"] >= end:
                raise KeyboardInterrupt

        def popen_side_effect(*args, **kwargs):  # type: ignore[no-untyped-def]
            types.append(kwargs["env"]["GIMMES_CYCLE_TYPE"])
            return _mock_popen()

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=popen_side_effect),
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli._resilient_sleep", side_effect=fake_sleep),
            patch("gimmes.cli.load_config", side_effect=self._hourly_load_config()),
            patch("gimmes.strategy.calendar.is_in_hourly_window", fake_in_hw),
            patch("gimmes.strategy.calendar.hourly_window", fake_hw),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_hourly_open",
                fake_snho,
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_window",
                return_value=10**6,  # no release windows all day
            ),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=100, pause_seconds=0,
                monitor_interval=3600,
            )

        assert types.count("hourly") == 24
        assert types.count("monitor") == 1
        assert len(types) == 25

    def test_post_hourly_sleep_takes_sooner_release_window(self) -> None:
        # The post-hourly min must consider the release calendar too —
        # a release window opening before the next hourly open wins
        sleep_spy = MagicMock()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=lambda *a, **kw: _mock_popen()),
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli._resilient_sleep", sleep_spy),
            patch("gimmes.cli.load_config", side_effect=self._hourly_load_config()),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: True,
            ),
            patch(
                "gimmes.strategy.calendar.hourly_window",
                self._dt_relative_window(1500),
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_window",
                return_value=600,
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_hourly_open",
                lambda dt=None, *, lead_minutes: 1860,
            ),
        ):
            _autonomous_loop(
                "driving_range", max_cycles=2, pause_seconds=0,
                monitor_interval=3600,
            )

        assert sleep_spy.call_args_list[0].args[0] == 600

    def test_startup_warning_lead_too_short_to_fire(self, capsys) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=lambda *a, **kw: _mock_popen()),
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch(
                "gimmes.cli.load_config",
                side_effect=self._hourly_load_config(hourly_lead_minutes=2),
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        # Rich wraps long console lines — normalize before matching
        out = " ".join(capsys.readouterr().out.split())
        assert "can NEVER fire" in out

    def test_startup_warning_lead_clamp_risk(self, capsys) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=lambda *a, **kw: _mock_popen()),
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch(
                "gimmes.cli.load_config",
                side_effect=self._hourly_load_config(hourly_lead_minutes=5),
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        out = " ".join(capsys.readouterr().out.split())
        assert "chronic timeouts" in out

    def test_no_startup_warning_at_default_lead(self, capsys) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=lambda *a, **kw: _mock_popen()),
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli.load_config", side_effect=self._hourly_load_config()),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: False,
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_hourly_open",
                lambda dt=None, *, lead_minutes: 1860,
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        out = " ".join(capsys.readouterr().out.split())
        assert "can NEVER fire" not in out
        assert "chronic timeouts" not in out

    def test_timeout_records_session_in_budget(self) -> None:
        # #545 intent: Anthropic charged for the killed subprocess — a
        # clamp-killed hourly straggler must still count a session
        _frozen = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
        from gimmes.config import GIMMES_HOME
        budget_path = GIMMES_HOME / "budget.json"

        def comm_side_effect(proc, timeout, **kwargs):  # type: ignore[no-untyped-def]
            raise _subprocess.TimeoutExpired(cmd=proc.args, timeout=timeout)

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", side_effect=lambda *a, **kw: _mock_popen()),
            patch(
                "gimmes.cli._communicate_interruptible",
                side_effect=comm_side_effect,
            ),
            patch("os.killpg"),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch("gimmes.cli._resilient_sleep"),
            patch("gimmes.budget._default_clock", lambda: _frozen),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        data = _json.loads(budget_path.read_text())
        today = _frozen.date().isoformat()
        assert data["days"][today]["sessions"] == 1


class TestCycleDeadlineEnv:
    """#746: the loop tells the subprocess when it will be killed."""

    @pytest.fixture(autouse=True)
    def _isolate_sessions(self, tmp_path, monkeypatch):
        """#638 review-found: this class ran the loop with the REAL
        load_config (it model_copies the real config to pin
        cycle_timeout) and NO session patches — every full-suite run
        wrote a real sessions row to the live DB (rows 175-178,
        2026-08-12), and the new session mutex correctly refuses that
        whenever the real loop is running. Session functions are
        patched; the real load_config stays (its model_copy is the
        point of the class). GIMMES_HOME is redirected too
        (review-found: the class was rewriting the LIVE budget.json
        and truncating the live cycle-001.json every suite run)."""
        monkeypatch.setenv("GIMMES_MODE", "driving_range")
        monkeypatch.setattr("gimmes.config.GIMMES_HOME", tmp_path)
        with (
            patch("gimmes.store.session.create_session", return_value=1),
            patch("gimmes.store.session.end_session"),
            patch(
                "gimmes.store.session.mark_stale_sessions",
                return_value=0,
            ),
            patch(
                "gimmes.store.session.close_orphan_activities",
                return_value=0,
            ),
            patch("gimmes.store.session.update_session_cycle"),
            patch(
                "gimmes.store.session.get_max_global_cycle",
                return_value=0,
            ),
        ):
            yield

    @staticmethod
    def _pinned_timeout_config():
        """load_config side_effect pinning strategy.cycle_timeout=2700
        (the machine's real config may differ)."""
        from gimmes import cli as gimmes_cli

        original = gimmes_cli.load_config

        def patched(*args, **kwargs):  # type: ignore[no-untyped-def]
            cfg = original(*args, **kwargs)
            return cfg.model_copy(update={
                "strategy": cfg.strategy.model_copy(
                    update={"cycle_timeout": 2700},
                ),
            })

        return patched

    def test_deadline_env_matches_effective_timeout(self) -> None:
        from datetime import UTC, datetime

        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
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
            patch("gimmes.cli._check_remote_staleness", return_value=None),
            patch(
                "gimmes.cli.load_config",
                side_effect=self._pinned_timeout_config(),
            ),
        ):
            before = datetime.now(UTC)
            _autonomous_loop("driving_range", max_cycles=1)
            after = datetime.now(UTC)

        env = mock_popen.call_args.kwargs["env"]
        assert "GIMMES_CYCLE_DEADLINE" in env
        deadline = datetime.strptime(
            env["GIMMES_CYCLE_DEADLINE"], "%Y-%m-%dT%H:%M:%SZ",
        ).replace(tzinfo=UTC)
        # Pinned cycle_timeout is 2700s; the deadline lands that far
        # from launch (loose bounds absorb test-runtime skew).
        low = (before - deadline).total_seconds()
        high = (after - deadline).total_seconds()
        assert -2760 <= low <= -2600, (low, high)


def _loop_isolation(tmp_path, monkeypatch, *, asyncio_run):  # type: ignore[no-untyped-def]
    """Shared loop-isolation stack: outside any release window, session
    store stubbed, staleness checks quiet. `asyncio_run` controls the
    position-window probe result (close the coro, return the hit).
    TestAutonomousLoop/TestHourlyLadder predate this helper and keep
    their own copies; new loop test classes should delegate here."""
    from contextlib import ExitStack

    monkeypatch.setenv("GIMMES_MODE", "driving_range")
    monkeypatch.setattr("gimmes.config.GIMMES_HOME", tmp_path)
    stack = ExitStack()
    for p in (
        patch("gimmes.store.session.create_session", return_value=1),
        patch("gimmes.store.session.end_session"),
        patch("gimmes.store.session.mark_stale_sessions", return_value=0),
        patch("gimmes.store.session.update_session_cycle"),
        patch("asyncio.run", side_effect=asyncio_run),
        patch(
            "gimmes.strategy.calendar.is_in_trade_window",
            return_value=(False, None, None),
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
        patch("gimmes.cli._check_remote_staleness", return_value=None),
    ):
        stack.enter_context(p)
    return stack


class TestPositionYieldsToHourly:
    """#755: precedence is release > hourly > position > monitor.
    Hourly cycles run full Step 2 surveillance for non-hourly positions
    (#659 backstop included), so a held position must never silence the
    hourly lane; position cycles interleave in the inter-window gaps,
    clamped so they can't eat the next hourly open."""

    @pytest.fixture(autouse=True)
    def _patch_session_funcs(self, tmp_path, monkeypatch):
        def _pos_hit(coro):  # type: ignore[no-untyped-def]
            coro.close()
            return (True, "KXGDP-26JUL30-T2.0")

        with _loop_isolation(tmp_path, monkeypatch, asyncio_run=_pos_hit):
            yield

    def test_hourly_outranks_position_window(self) -> None:
        # Both active: the hourly window fires; the position check is
        # never consulted (hourly Step 2 surveils the position anyway).
        mock_proc = _mock_popen()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("gimmes.cli._communicate_interruptible", return_value=b""),
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch(
                "gimmes.cli.load_config",
                side_effect=TestHourlyLadder._hourly_load_config(),
            ),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: True,
            ),
            patch(
                "gimmes.strategy.calendar.hourly_window",
                TestHourlyLadder._dt_relative_window(1500),
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        env = mock_popen.call_args.kwargs["env"]
        assert env["GIMMES_CYCLE_TYPE"] == "hourly"

    def test_position_cycle_clamped_to_next_hourly_open(self) -> None:
        # Gap case: no hourly window now, position cycle fires but its
        # timeout is clamped to the next hourly open.
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=_mock_popen()) as mock_popen,
            patch(
                "gimmes.cli._communicate_interruptible", return_value=b"",
            ) as mock_comm,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch(
                "gimmes.cli.load_config",
                side_effect=TestHourlyLadder._hourly_load_config(),
            ),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: False,
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_hourly_open",
                lambda dt=None, *, lead_minutes: 900,
            ),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        env = mock_popen.call_args.kwargs["env"]
        assert env["GIMMES_CYCLE_TYPE"] == "full"
        assert mock_comm.call_args.kwargs["timeout"] == 900

    def test_position_yields_sleep_when_gap_tiny(self) -> None:
        # Tail shorter than a useful cycle: no subprocess spawns; the
        # loop sleeps straight to the hourly open.
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen") as mock_popen,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
            patch(
                "gimmes.cli.load_config",
                side_effect=TestHourlyLadder._hourly_load_config(),
            ),
            patch(
                "gimmes.strategy.calendar.is_in_hourly_window",
                lambda dt=None, *, lead_minutes: False,
            ),
            patch(
                "gimmes.strategy.calendar.seconds_until_next_hourly_open",
                lambda dt=None, *, lead_minutes: 60,
            ),
            patch(
                "gimmes.cli._sleep_with_resting_sweep",
                side_effect=KeyboardInterrupt,
            ) as mock_sleep,
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        mock_popen.assert_not_called()
        assert mock_sleep.call_args.args[1] == 60

    def test_position_unclamped_when_hourly_disabled(self) -> None:
        # Stock install (empty hourly_series): position windows behave
        # exactly as before #755 — full cycle, full timeout.
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.Popen", return_value=_mock_popen()) as mock_popen,
            patch(
                "gimmes.cli._communicate_interruptible", return_value=b"",
            ) as mock_comm,
            patch("gimmes.clubhouse.server.start_background", return_value=None),
        ):
            _autonomous_loop("driving_range", max_cycles=1, pause_seconds=0)

        env = mock_popen.call_args.kwargs["env"]
        assert env["GIMMES_CYCLE_TYPE"] == "full"
        assert mock_comm.call_args.kwargs["timeout"] == 2700


class TestSessionMutexLoop:
    """#638: the loop refuses to start over a live session and cleans
    up on SIGTERM exactly as on SIGINT."""

    def test_refuses_startup_on_conflict(self, tmp_path, monkeypatch) -> None:
        from gimmes.store.session import SessionConflictError

        monkeypatch.setenv("GIMMES_MODE", "driving_range")
        monkeypatch.setattr("gimmes.config.GIMMES_HOME", tmp_path)
        conflict = SessionConflictError({
            "id": 7, "mode": "driving_range", "pid": 12345,
            "started_at": "2026-08-12 08:00:00",
        })
        popen = MagicMock()
        with (
            patch(
                "gimmes.store.session.create_session",
                side_effect=conflict,
            ),
            patch("gimmes.store.session.end_session") as end,
            patch(
                "gimmes.store.session.mark_stale_sessions",
                return_value=0,
            ),
            patch("gimmes.store.session.close_orphan_activities",
                  return_value=0),
            patch("subprocess.Popen", popen),
            patch("shutil.which", return_value="/usr/bin/claude"),
        ):
            with pytest.raises(ClickExit) as exc:
                _autonomous_loop("driving_range")
        assert exc.value.exit_code == 1
        popen.assert_not_called()
        end.assert_not_called()

    def test_refusal_output_names_pid_and_kill_command(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The refusal hints are operator-facing contract: the PID,
        the verification step, and the kill command — and NO
        kickstart advice (it SIGKILLs the pgroup and recreates the
        incident)."""
        from io import StringIO

        from rich.console import Console

        from gimmes.store.session import SessionConflictError

        monkeypatch.setattr("gimmes.config.GIMMES_HOME", tmp_path)
        conflict = SessionConflictError({
            "id": 7, "mode": "driving_range", "pid": 12345,
            "started_at": "2026-08-12 08:00:00",
        })
        buf = StringIO()
        with (
            patch(
                "gimmes.store.session.create_session",
                side_effect=conflict,
            ),
            patch("gimmes.store.session.mark_stale_sessions",
                  return_value=0),
            patch("gimmes.store.session.close_orphan_activities",
                  return_value=0),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("gimmes.cli.console", Console(file=buf, width=200)),
        ):
            with pytest.raises(ClickExit):
                _autonomous_loop("driving_range")
        out = buf.getvalue()
        assert "PID 12345" in out
        assert "kill -TERM 12345" in out
        assert "ps -o command= -p 12345" in out
        assert "kickstart" not in out

    def test_both_signals_share_the_shutdown_handler(
        self, tmp_path, monkeypatch,
    ) -> None:
        """SIGINT and SIGTERM must register the SAME handler, and both
        old handlers must be restored in the finally."""
        import signal as signal_mod

        monkeypatch.setenv("GIMMES_MODE", "driving_range")
        monkeypatch.setattr("gimmes.config.GIMMES_HOME", tmp_path)
        # asyncio.run registers its own transient SIGINT handler, so
        # record EVERY call and identify the loop's handler by name.
        calls: list[tuple[int, object]] = []
        originals = {
            signal_mod.SIGINT: object(),
            signal_mod.SIGTERM: object(),
        }

        def _fake_signal(signum, handler):
            calls.append((signum, handler))
            return originals.get(signum)

        with (
            patch("gimmes.store.session.create_session", return_value=1),
            patch("gimmes.store.session.end_session"),
            patch(
                "gimmes.store.session.mark_stale_sessions",
                return_value=0,
            ),
            patch(
                "gimmes.store.session.close_orphan_activities",
                return_value=0,
            ),
            # Review-found: without these, this test started a REAL
            # uvicorn dashboard and ran a REAL `git ls-remote`.
            patch(
                "gimmes.clubhouse.server.start_background",
                return_value=None,
            ),
            patch(
                "gimmes.cli._check_code_staleness",
                return_value=("abc123", False, None),
            ),
            patch("gimmes.cli._check_remote_staleness", return_value=None),
            patch("signal.signal", side_effect=_fake_signal),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "gimmes.strategy.calendar.is_in_trade_window",
                return_value=(False, "", 0),
            ),
            patch(
                "gimmes.strategy.calendar.next_trade_window",
                side_effect=KeyboardInterrupt,
            ),
        ):
            try:
                _autonomous_loop("driving_range")
            except (KeyboardInterrupt, ClickExit, Exception):
                pass

        def _shutdown_regs(signum):
            return [
                h for (sn, h) in calls
                if sn == signum
                and getattr(h, "__name__", "") == "_shutdown_handler"
            ]

        int_regs = _shutdown_regs(signal_mod.SIGINT)
        term_regs = _shutdown_regs(signal_mod.SIGTERM)
        assert int_regs, "no _shutdown_handler registered for SIGINT"
        assert term_regs, "no _shutdown_handler registered for SIGTERM"
        assert int_regs[0] is term_regs[0]
        # Both original handlers restored in the finally
        assert (signal_mod.SIGINT, originals[signal_mod.SIGINT]) in calls
        assert (signal_mod.SIGTERM, originals[signal_mod.SIGTERM]) in calls


class TestShutdownHandlerBody:
    """#638 review-found: the handler BODY was untested — signal
    delivery is synchronous on the main thread, so a real SIGTERM to
    self exercises it end-to-end. Standalone class (subclassing
    TestAutonomousLoop would inherit and re-run its ~40 tests) with
    its own isolation fixture."""

    @pytest.fixture(autouse=True)
    def _patch_session_funcs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIMMES_MODE", "driving_range")
        monkeypatch.setattr("gimmes.config.GIMMES_HOME", tmp_path)
        with (
            patch("gimmes.store.session.create_session", return_value=1),
            patch("gimmes.store.session.mark_stale_sessions", return_value=0),
            patch("gimmes.store.session.close_orphan_activities",
                  return_value=0),
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

    def test_sigterm_kills_agent_group_and_stops_cleanly(
        self,
    ) -> None:
        import os
        import signal as signal_mod

        def _comm(*args, **kwargs):
            os.kill(os.getpid(), signal_mod.SIGTERM)
            return b""

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
            patch("gimmes.cli._communicate_interruptible",
                  side_effect=_comm),
            patch("os.killpg") as mock_killpg,
            patch("gimmes.store.session.end_session") as end,
        ):
            _autonomous_loop("driving_range", pause_seconds=0)

        # The handler killpg'd the agent group with SIGTERM, and the
        # loop ended its session as 'stopped' — the full #638 cleanup.
        assert any(
            c.args == (12345, signal_mod.SIGTERM)
            for c in mock_killpg.call_args_list
        ), mock_killpg.call_args_list
        end.assert_called_once()
        assert end.call_args.args[2] == "stopped"

    def test_second_signal_does_not_reraise(self) -> None:
        """Idempotence: a second SIGTERM during the escalation wait
        must not raise a second KeyboardInterrupt (it would abort the
        TERM->KILL escalation)."""
        import signal as signal_mod

        captured: dict[int, object] = {}

        real_signal = signal_mod.signal

        def _capture(signum, handler):
            if getattr(handler, "__name__", "") == "_shutdown_handler":
                captured[signum] = handler
            return real_signal(signum, handler)

        def _comm(*args, **kwargs):
            handler = captured[signal_mod.SIGTERM]
            try:
                handler(signal_mod.SIGTERM, None)
            except KeyboardInterrupt:
                # First delivery raised; the second must NOT.
                handler(signal_mod.SIGTERM, None)
                raise
            raise AssertionError("first delivery did not raise")

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
            patch("signal.signal", side_effect=_capture),
            patch("gimmes.cli._communicate_interruptible",
                  side_effect=_comm),
            patch("os.killpg") as mock_killpg,
        ):
            _autonomous_loop("driving_range", pause_seconds=0)

        # Both deliveries killpg'd (the second still re-kills), plus
        # the except-block's escalation kill.
        assert mock_killpg.call_count >= 2

