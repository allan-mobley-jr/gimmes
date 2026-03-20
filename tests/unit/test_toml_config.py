"""Tests for database-backed config loading and saving."""

from __future__ import annotations

import json
import sqlite3

from gimmes.config import (
    _load_config_from_db,
    config_keys_in_db,
    load_config,
    save_config_value,
    save_config_values,
)


def _create_config_db(path):
    """Create a minimal DB with the config table for testing."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.commit()
    conn.close()


class TestLoadConfigFromDb:
    def test_returns_empty_when_db_missing(self, tmp_path):
        result = _load_config_from_db(tmp_path / "nonexistent.db")
        assert result == {}

    def test_returns_empty_when_table_missing(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.close()
        result = _load_config_from_db(db)
        assert result == {}

    def test_loads_simple_values(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO config (key, value) VALUES (?, ?)",
                     ("strategy.gimme_threshold", json.dumps(80)))
        conn.commit()
        conn.close()

        result = _load_config_from_db(db)
        assert result == {"strategy": {"gimme_threshold": 80}}

    def test_loads_nested_values(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO config (key, value) VALUES (?, ?)",
                     ("scoring.weights.edge_size", json.dumps(0.40)))
        conn.commit()
        conn.close()

        result = _load_config_from_db(db)
        assert result == {"scoring": {"weights": {"edge_size": 0.40}}}

    def test_loads_list_values(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO config (key, value) VALUES (?, ?)",
                     ("scanner.series", json.dumps(["KXCPI", "KXGDP"])))
        conn.commit()
        conn.close()

        result = _load_config_from_db(db)
        assert result["scanner"]["series"] == ["KXCPI", "KXGDP"]


class TestConfigKeysInDb:
    def test_returns_empty_when_db_missing(self, tmp_path):
        result = config_keys_in_db(tmp_path / "nonexistent.db")
        assert result == set()

    def test_returns_keys(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        save_config_value("strategy.gimme_threshold", 80, db_path=db)
        save_config_value("risk.bankroll", 1000.0, db_path=db)

        result = config_keys_in_db(db)
        assert result == {"strategy.gimme_threshold", "risk.bankroll"}


class TestSaveConfigValue:
    def test_inserts_new_value(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)

        save_config_value("strategy.gimme_threshold", 80, db_path=db)

        conn = sqlite3.connect(str(db))
        row = conn.execute("SELECT value FROM config WHERE key = ?",
                           ("strategy.gimme_threshold",)).fetchone()
        conn.close()
        assert json.loads(row[0]) == 80

    def test_replaces_existing_value(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)

        save_config_value("strategy.gimme_threshold", 80, db_path=db)
        save_config_value("strategy.gimme_threshold", 90, db_path=db)

        conn = sqlite3.connect(str(db))
        row = conn.execute("SELECT value FROM config WHERE key = ?",
                           ("strategy.gimme_threshold",)).fetchone()
        conn.close()
        assert json.loads(row[0]) == 90


class TestSaveConfigValues:
    def test_saves_multiple_values(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)

        save_config_values({
            "strategy.gimme_threshold": 80,
            "risk.bankroll": 1000.0,
        }, db_path=db)

        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT key, value FROM config ORDER BY key").fetchall()
        conn.close()
        assert len(rows) == 2
        assert json.loads(rows[0][1]) == 1000.0  # risk.bankroll
        assert json.loads(rows[1][1]) == 80  # strategy.gimme_threshold


class TestLoadConfig:
    def test_loads_defaults_when_no_db(self, tmp_path):
        config = load_config(db_path=tmp_path / "nonexistent.db")
        assert config.strategy.gimme_threshold == 75
        assert config.risk.bankroll == 500.0

    def test_loads_overrides_from_db(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        save_config_value("strategy.min_edge_after_fees", 0.08, db_path=db)

        config = load_config(db_path=db)
        assert config.strategy.min_edge_after_fees == 0.08

    def test_non_overridden_fields_use_defaults(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        save_config_value("strategy.gimme_threshold", 80, db_path=db)

        config = load_config(db_path=db)
        assert config.strategy.gimme_threshold == 80
        assert config.strategy.min_market_price == 0.55  # default


class TestMonitorPriceTrigger:
    def test_default_is_10(self, tmp_path):
        config = load_config(db_path=tmp_path / "nonexistent.db")
        assert config.risk.monitor_price_trigger_pp == 10

    def test_loads_from_db(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        save_config_value("risk.monitor_price_trigger_pp", 15, db_path=db)

        config = load_config(db_path=db)
        assert config.risk.monitor_price_trigger_pp == 15


class TestPrivateKeyPasswordConfig:
    def test_reads_password_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PASSWORD", "my-secret")
        config = load_config(db_path=tmp_path / "nonexistent.db")
        assert config.private_key_password == "my-secret"

    def test_password_none_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_PASSWORD", raising=False)
        config = load_config(db_path=tmp_path / "nonexistent.db")
        assert config.private_key_password is None

    def test_empty_password_treated_as_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PASSWORD", "")
        config = load_config(db_path=tmp_path / "nonexistent.db")
        assert config.private_key_password is None
