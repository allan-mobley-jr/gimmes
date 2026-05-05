"""Tests for the `gimmes budget` CLI command (#545)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from gimmes.cli import app

runner = CliRunner()


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
        from datetime import UTC, datetime
        today = datetime.now(UTC).date().isoformat()
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
        from datetime import UTC, datetime
        today = datetime.now(UTC).date().isoformat()
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
        from datetime import UTC, datetime
        today = datetime.now(UTC).date().isoformat()
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
