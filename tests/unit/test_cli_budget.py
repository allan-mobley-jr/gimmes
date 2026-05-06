"""Tests for the `gimmes budget` CLI command (#545)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from gimmes.cli import app

runner = CliRunner()

# Frozen clock keeps the seeded date and the CLI-observed date in lockstep
# even if the test happens to run across a UTC midnight boundary.
_FROZEN_NOW = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
_FROZEN_TODAY = _FROZEN_NOW.date().isoformat()


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch):
    """Freeze the BudgetTracker's clock to keep the seeded date and the
    CLI-observed date in lockstep regardless of when the suite runs.
    The tracker resolves ``_default_clock`` lazily per-call, so this
    patch takes effect for any tracker constructed afterwards.
    """
    monkeypatch.setattr("gimmes.budget._default_clock", lambda: _FROZEN_NOW)
    yield


@pytest.fixture()
def gimmes_home(tmp_path: Path) -> Path:
    """Provide an isolated GIMMES_HOME and pre-create the dir."""
    home = tmp_path / ".gimmes"
    home.mkdir()
    return home


def _seed_budget(home: Path, days: dict, caps: dict | None = None) -> None:
    """Write a budget.json fixture into the given GIMMES_HOME."""
    payload = {"version": 1, "days": days}
    if caps is not None:
        payload["caps"] = caps
    (home / "budget.json").write_text(json.dumps(payload))


class TestBudgetCommand:
    def test_zero_state_today(self, gimmes_home: Path) -> None:
        """With no budget.json, the command prints zero totals at default caps."""
        with patch("gimmes.config.GIMMES_HOME", gimmes_home), \
             patch("gimmes.cli.GIMMES_HOME", gimmes_home, create=True):
            result = runner.invoke(app, ["budget"])

        assert result.exit_code == 0, result.output
        assert "Claude API Budget" in result.output
        assert "Sessions:  0 / 80" in result.output
        assert "Cost:      $0.00 / $25.00" in result.output
        assert "Resets in:" in result.output

    def test_populated_state_reflects_seeded_totals(
        self, gimmes_home: Path,
    ) -> None:
        """A pre-seeded budget.json shows the recorded totals."""
        today = _FROZEN_TODAY
        _seed_budget(
            gimmes_home,
            days={today: {
                "sessions": 12,
                "cost_usd": 7.5,
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 200,
            }},
            caps={"max_sessions": 80, "max_cost_usd": 25.0},
        )
        with patch("gimmes.config.GIMMES_HOME", gimmes_home), \
             patch("gimmes.cli.GIMMES_HOME", gimmes_home, create=True):
            result = runner.invoke(app, ["budget"])

        assert result.exit_code == 0, result.output
        assert "Sessions:  12 / 80" in result.output
        assert "Cost:      $7.50 / $25.00" in result.output

    def test_uses_caps_persisted_in_budget_json(
        self, gimmes_home: Path,
    ) -> None:
        """When the loop persisted custom caps, the diagnostic uses them."""
        today = _FROZEN_TODAY
        _seed_budget(
            gimmes_home,
            days={today: {"sessions": 25, "cost_usd": 30.0}},
            caps={"max_sessions": 200, "max_cost_usd": 75.0},
        )
        with patch("gimmes.config.GIMMES_HOME", gimmes_home), \
             patch("gimmes.cli.GIMMES_HOME", gimmes_home, create=True):
            result = runner.invoke(app, ["budget"])

        assert result.exit_code == 0
        assert "25 / 200" in result.output
        assert "$30.00 / $75.00" in result.output

    def test_json_output_is_parseable_list(self, gimmes_home: Path) -> None:
        """`--json` emits a JSON list of day summaries."""
        today = _FROZEN_TODAY
        _seed_budget(
            gimmes_home,
            days={today: {"sessions": 3, "cost_usd": 1.2345}},
        )
        with patch("gimmes.config.GIMMES_HOME", gimmes_home), \
             patch("gimmes.cli.GIMMES_HOME", gimmes_home, create=True):
            result = runner.invoke(app, ["budget", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["date"] == today
        assert parsed[0]["sessions"] == 3
        # Cost is rounded to 4 decimals per the CLI contract.
        assert parsed[0]["cost_usd"] == 1.2345

    def test_days_flag_returns_n_summaries(
        self, gimmes_home: Path,
    ) -> None:
        """`--days 3` produces 3 entries (today + 2 prior days)."""
        with patch("gimmes.config.GIMMES_HOME", gimmes_home), \
             patch("gimmes.cli.GIMMES_HOME", gimmes_home, create=True):
            result = runner.invoke(app, ["budget", "--days", "3", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert len(parsed) == 3

    def test_zero_caps_render_as_unlimited(self, gimmes_home: Path) -> None:
        """`0` caps in budget.json render as ``∞`` (matches the loop's
        ``0=unlimited`` semantics — the user shouldn't see ``0/0`` and
        think the budget is exhausted)."""
        _seed_budget(
            gimmes_home,
            days={_FROZEN_TODAY: {"sessions": 5, "cost_usd": 1.0}},
            caps={"max_sessions": 0, "max_cost_usd": 0.0},
        )
        with patch("gimmes.config.GIMMES_HOME", gimmes_home), \
             patch("gimmes.cli.GIMMES_HOME", gimmes_home, create=True):
            result = runner.invoke(app, ["budget"])

        assert result.exit_code == 0, result.output
        assert "∞" in result.output or "unlimited" in result.output.lower()
        assert "0 / 0" not in result.output

    def test_json_includes_caps_remaining_and_reset(
        self, gimmes_home: Path,
    ) -> None:
        """The `--json` schema includes the caps + remaining headroom +
        reset-seconds so automation can act on cap proximity."""
        _seed_budget(
            gimmes_home,
            days={_FROZEN_TODAY: {"sessions": 10, "cost_usd": 5.0}},
            caps={"max_sessions": 80, "max_cost_usd": 25.0},
        )
        with patch("gimmes.config.GIMMES_HOME", gimmes_home), \
             patch("gimmes.cli.GIMMES_HOME", gimmes_home, create=True):
            result = runner.invoke(app, ["budget", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        entry = parsed[0]
        assert entry["max_sessions"] == 80
        assert entry["max_cost_usd"] == 25.0
        assert entry["remaining_sessions"] == 70
        assert entry["remaining_cost_usd"] == 20.0
        assert "seconds_until_reset" in entry
        assert isinstance(entry["seconds_until_reset"], int)

    def test_json_under_unlimited_caps_shows_null(
        self, gimmes_home: Path,
    ) -> None:
        """Under `0` caps, the JSON cap fields are null (machine-friendly
        equivalent of the human-readable ``∞``)."""
        _seed_budget(
            gimmes_home,
            days={_FROZEN_TODAY: {"sessions": 1, "cost_usd": 0.5}},
            caps={"max_sessions": 0, "max_cost_usd": 0.0},
        )
        with patch("gimmes.config.GIMMES_HOME", gimmes_home), \
             patch("gimmes.cli.GIMMES_HOME", gimmes_home, create=True):
            result = runner.invoke(app, ["budget", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        entry = parsed[0]
        assert entry["max_sessions"] is None
        assert entry["max_cost_usd"] is None
        assert entry["remaining_sessions"] is None
        assert entry["remaining_cost_usd"] is None
