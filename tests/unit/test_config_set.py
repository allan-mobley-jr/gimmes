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

    def test_get_specific_list_shows_full(self, db_file: Path) -> None:
        # Seed a list with more than 6 items
        items = [f"TICKER{i}" for i in range(10)]
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("scanner.series", json.dumps(items)),
        )
        conn.commit()
        conn.close()

        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "get", "scanner.series"])

        assert result.exit_code == 0
        # All items should be visible — no truncation
        for item in items:
            assert item in result.output
        # Should NOT show "[10 items]" truncation marker
        assert "[10 items]" not in result.output


class TestConfigAdd:
    def test_add_to_list(self, db_file: Path) -> None:
        # Seed a small list
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("scanner.series", json.dumps(["KXCPI", "KXGDP"])),
        )
        conn.commit()
        conn.close()

        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "add", "scanner.series", "KXFED"])

        assert result.exit_code == 0
        assert "Added" in result.output
        assert "KXFED" in result.output

        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'scanner.series'"
        ).fetchone()
        conn.close()
        assert json.loads(row[0]) == ["KXCPI", "KXGDP", "KXFED"]

    def test_add_duplicate_is_noop(self, db_file: Path) -> None:
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("scanner.series", json.dumps(["KXCPI", "KXGDP"])),
        )
        conn.commit()
        conn.close()

        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "add", "scanner.series", "KXCPI"])

        assert result.exit_code == 0
        assert "already in" in result.output

    def test_add_to_non_list_field_errors(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(
                app, ["config", "add", "strategy.gimme_threshold", "80"]
            )

        assert result.exit_code == 1
        assert "not a list field" in result.output

    def test_add_invalid_key_errors(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "add", "bad.key", "FOO"])

        assert result.exit_code == 1
        assert "Unknown config key" in result.output

    def test_add_exits_when_db_missing(self, tmp_path: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", tmp_path):
            result = runner.invoke(app, ["config", "add", "scanner.series", "KXFED"])

        assert result.exit_code == 1
        assert "Database not found" in result.output

    def test_add_corrupted_value_errors(self, db_file: Path) -> None:
        """If the stored value is not a list, error instead of silently overwriting."""
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("scanner.series", json.dumps("not-a-list")),
        )
        conn.commit()
        conn.close()

        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "add", "scanner.series", "KXFED"])

        assert result.exit_code == 1
        assert "corrupted" in result.output

    def test_add_to_default_list(self, db_file: Path) -> None:
        """Adding to an unset field should copy the default list and append."""
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "add", "scanner.series", "NEWTICKERXYZ"])

        assert result.exit_code == 0
        assert "Added" in result.output

        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'scanner.series'"
        ).fetchone()
        conn.close()
        saved = json.loads(row[0])
        assert "NEWTICKERXYZ" in saved
        assert len(saved) > 1  # Should include defaults plus new item


class TestConfigRemove:
    def test_remove_from_list(self, db_file: Path) -> None:
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("scanner.series", json.dumps(["KXCPI", "KXGDP", "KXFED"])),
        )
        conn.commit()
        conn.close()

        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "remove", "scanner.series", "KXGDP"])

        assert result.exit_code == 0
        assert "Removed" in result.output
        assert "KXGDP" in result.output

        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'scanner.series'"
        ).fetchone()
        conn.close()
        assert json.loads(row[0]) == ["KXCPI", "KXFED"]

    def test_remove_nonexistent_item_errors(self, db_file: Path) -> None:
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("scanner.series", json.dumps(["KXCPI", "KXGDP"])),
        )
        conn.commit()
        conn.close()

        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(
                app, ["config", "remove", "scanner.series", "NOTHERE"]
            )

        assert result.exit_code == 1
        assert "not found" in result.output

    def test_remove_from_non_list_field_errors(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(
                app, ["config", "remove", "strategy.gimme_threshold", "80"]
            )

        assert result.exit_code == 1
        assert "not a list field" in result.output

    def test_remove_invalid_key_errors(self, db_file: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "remove", "bad.key", "FOO"])

        assert result.exit_code == 1
        assert "Unknown config key" in result.output

    def test_remove_corrupted_value_errors(self, db_file: Path) -> None:
        """If the stored value is not a list, error instead of silently proceeding."""
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("scanner.series", json.dumps("not-a-list")),
        )
        conn.commit()
        conn.close()

        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "remove", "scanner.series", "KXFED"])

        assert result.exit_code == 1
        assert "corrupted" in result.output

    def test_remove_last_item_leaves_empty_list(self, db_file: Path) -> None:
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("scanner.series", json.dumps(["KXCPI"])),
        )
        conn.commit()
        conn.close()

        with patch("gimmes.config.GIMMES_HOME", db_file.parent):
            result = runner.invoke(app, ["config", "remove", "scanner.series", "KXCPI"])

        assert result.exit_code == 0
        assert "Removed" in result.output

        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'scanner.series'"
        ).fetchone()
        conn.close()
        assert json.loads(row[0]) == []

    def test_remove_exits_when_db_missing(self, tmp_path: Path) -> None:
        with patch("gimmes.config.GIMMES_HOME", tmp_path):
            result = runner.invoke(
                app, ["config", "remove", "scanner.series", "KXCPI"]
            )

        assert result.exit_code == 1
        assert "Database not found" in result.output
