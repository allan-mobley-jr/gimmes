"""Tests for gimmes config set/get CLI subcommands."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from gimmes.cli import app

runner = CliRunner()


@pytest.fixture()
def db_file(tmp_path: Path) -> Path:
    """Create a temporary database with the config table."""
    db = tmp_path / "gimmes.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.commit()
    conn.close()
    return db


class TestConfigSet:
    def test_set_int_value(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "set", "strategy.gimme_threshold", "80"])

        assert result.exit_code == 0
        assert "80" in result.output

        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'strategy.gimme_threshold'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert json.loads(row[0]) == 80

    def test_set_float_value(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "set", "sizing.kelly_fraction", "0.30"])

        assert result.exit_code == 0
        assert "0.30" in result.output

    def test_set_invalid_key(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "set", "bad.key", "42"])

        assert result.exit_code == 1
        assert "Unknown config key" in result.output

    def test_set_out_of_range(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "set", "strategy.gimme_threshold", "200"])

        assert result.exit_code == 1
        assert "at most" in result.output

    def test_set_no_change(self, db_file: Path) -> None:
        # Pre-seed the default value
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("strategy.gimme_threshold", json.dumps(75)),
        )
        conn.commit()
        conn.close()

        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "set", "strategy.gimme_threshold", "75"])

        assert result.exit_code == 0
        assert "already" in result.output

    def test_set_list_value(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "set", "scanner.series", "KXCPI,KXGDP,KXFED"])

        assert result.exit_code == 0

        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'scanner.series'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert json.loads(row[0]) == ["KXCPI", "KXGDP", "KXFED"]

    def test_set_str_choice_value(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "set", "orders.preferred_order_type", "taker"])

        assert result.exit_code == 0
        assert "taker" in result.output

        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'orders.preferred_order_type'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert json.loads(row[0]) == "taker"

    def test_set_scoring_weight_warns_on_bad_sum(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(
                app, ["config", "set", "scoring.weights.edge_size", "0.90"]
            )

        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "sum to" in result.output

    def test_set_scoring_weight_no_warning_when_balanced(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            # Default edge_size is 0.30, setting it to 0.30 is no-change
            # Set to 0.29, then adjust signal_strength to 0.26 to keep sum at 1.0
            runner.invoke(
                app, ["config", "set", "scoring.weights.edge_size", "0.29"]
            )
            result = runner.invoke(
                app, ["config", "set", "scoring.weights.signal_strength", "0.26"]
            )

        assert result.exit_code == 0
        assert "Warning" not in result.output

    def test_set_exits_when_db_missing(self, tmp_path: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", tmp_path):
            result = runner.invoke(
                app, ["config", "set", "strategy.gimme_threshold", "80"]
            )

        assert result.exit_code == 1
        assert "Database not found" in result.output

    def test_set_shows_old_and_new(self, db_file: Path) -> None:
        # Seed old value
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("strategy.gimme_threshold", json.dumps(75)),
        )
        conn.commit()
        conn.close()

        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "set", "strategy.gimme_threshold", "80"])

        assert result.exit_code == 0
        # Should show old → new
        assert "75" in result.output
        assert "80" in result.output


class TestConfigGet:
    def test_get_single_key(self, db_file: Path) -> None:
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("strategy.gimme_threshold", json.dumps(80)),
        )
        conn.commit()
        conn.close()

        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "get", "strategy.gimme_threshold"])

        assert result.exit_code == 0
        assert "80" in result.output

    def test_get_invalid_key(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "get", "bad.key"])

        assert result.exit_code == 1
        assert "Unknown config key" in result.output

    def test_get_all_shows_table(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "get"])

        assert result.exit_code == 0
        # Table should contain section names
        assert "Strategy" in result.output
        assert "Risk" in result.output

    def test_get_shows_default_for_unset_key(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "get", "strategy.gimme_threshold"])

        assert result.exit_code == 0
        # Should show the Pydantic default (75)
        assert "75" in result.output

    def test_get_exits_when_db_missing(self, tmp_path: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", tmp_path):
            result = runner.invoke(app, ["config", "get", "strategy.gimme_threshold"])

        assert result.exit_code == 1
        assert "Database not found" in result.output
