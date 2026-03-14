"""Tests for TOML config parsing and _apply_toml_change."""

from __future__ import annotations

import pytest

from gimmes.cli import _apply_toml_change
from gimmes.config import load_config


class TestLoadConfigMalformedToml:
    def test_raises_on_malformed_toml(self, tmp_path):
        """load_config should raise ValueError on invalid TOML."""
        bad_toml = tmp_path / "gimmes.toml"
        bad_toml.write_text("[broken\nkey = !!!")

        with pytest.raises(ValueError, match="Failed to parse"):
            load_config(config_path=bad_toml)

    def test_loads_valid_toml(self, tmp_path):
        """load_config should work with valid TOML."""
        good_toml = tmp_path / "gimmes.toml"
        good_toml.write_text("[strategy]\nmin_edge_after_fees = 0.08\n")

        config = load_config(config_path=good_toml)
        assert config.strategy.min_edge_after_fees == 0.08

    def test_loads_defaults_when_no_file(self, tmp_path):
        """load_config should use defaults when file doesn't exist."""
        config = load_config(config_path=tmp_path / "nonexistent.toml")
        assert config.strategy is not None


class TestPrivateKeyPasswordConfig:
    def test_reads_password_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PASSWORD", "my-secret")
        config = load_config(config_path=tmp_path / "nonexistent.toml")
        assert config.private_key_password == "my-secret"

    def test_password_none_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KALSHI_PRIVATE_KEY_PASSWORD", raising=False)
        config = load_config(config_path=tmp_path / "nonexistent.toml")
        assert config.private_key_password is None

    def test_empty_password_treated_as_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PASSWORD", "")
        config = load_config(config_path=tmp_path / "nonexistent.toml")
        assert config.private_key_password is None


class TestApplyTomlChange:
    def test_simple_key(self, tmp_path):
        """Should update a simple section.key path."""
        toml = tmp_path / "gimmes.toml"
        toml.write_text("[strategy]\nmin_edge_after_fees = 0.05\n")

        _apply_toml_change(toml, "strategy.min_edge_after_fees", "0.08")

        import tomllib
        with open(toml, "rb") as f:
            data = tomllib.load(f)
        assert data["strategy"]["min_edge_after_fees"] == 0.08

    def test_nested_key(self, tmp_path):
        """Should handle 3-level nested paths like scoring.weights.edge_size."""
        toml = tmp_path / "gimmes.toml"
        toml.write_text(
            "[scoring.weights]\nedge_size = 25\n"
        )

        _apply_toml_change(toml, "scoring.weights.edge_size", "30")

        import tomllib
        with open(toml, "rb") as f:
            data = tomllib.load(f)
        assert data["scoring"]["weights"]["edge_size"] == 30

    def test_creates_missing_section(self, tmp_path):
        """Should create missing sections when path doesn't exist."""
        toml = tmp_path / "gimmes.toml"
        toml.write_text("")

        _apply_toml_change(toml, "new_section.new_key", "42")

        import tomllib
        with open(toml, "rb") as f:
            data = tomllib.load(f)
        assert data["new_section"]["new_key"] == 42

    def test_creates_file_when_missing(self, tmp_path):
        """Should create the file if it doesn't exist."""
        toml = tmp_path / "gimmes.toml"

        _apply_toml_change(toml, "strategy.threshold", "85")

        assert toml.exists()
        import tomllib
        with open(toml, "rb") as f:
            data = tomllib.load(f)
        assert data["strategy"]["threshold"] == 85

    def test_creates_backup(self, tmp_path):
        """Should create a .bak backup before writing."""
        toml = tmp_path / "gimmes.toml"
        toml.write_text("[strategy]\nthreshold = 75\n")

        _apply_toml_change(toml, "strategy.threshold", "85")

        backup = tmp_path / "gimmes.toml.bak"
        assert backup.exists()
        assert "threshold = 75" in backup.read_text()

    def test_preserves_comments(self, tmp_path):
        """Should preserve TOML comments."""
        toml = tmp_path / "gimmes.toml"
        toml.write_text("# Important config\n[strategy]\nthreshold = 75\n")

        _apply_toml_change(toml, "strategy.threshold", "85")

        content = toml.read_text()
        assert "# Important config" in content

    def test_boolean_value(self, tmp_path):
        """Should handle boolean values."""
        toml = tmp_path / "gimmes.toml"
        toml.write_text("[orders]\npost_only = false\n")

        _apply_toml_change(toml, "orders.post_only", "true")

        import tomllib
        with open(toml, "rb") as f:
            data = tomllib.load(f)
        assert data["orders"]["post_only"] is True

    def test_string_value(self, tmp_path):
        """Should handle non-numeric string values."""
        toml = tmp_path / "gimmes.toml"
        toml.write_text("[strategy]\nmode = \"conservative\"\n")

        _apply_toml_change(toml, "strategy.mode", "aggressive")

        import tomllib
        with open(toml, "rb") as f:
            data = tomllib.load(f)
        assert data["strategy"]["mode"] == "aggressive"

    def test_raises_on_scalar_intermediate(self, tmp_path):
        """Should raise ValueError if intermediate path is a scalar."""
        toml = tmp_path / "gimmes.toml"
        toml.write_text("scoring = 42\n")

        with pytest.raises(ValueError, match="is a scalar, not a table"):
            _apply_toml_change(toml, "scoring.weights.edge", "30")

    def test_creates_parent_dir(self, tmp_path):
        """Should create parent directories if they don't exist."""
        toml = tmp_path / "subdir" / "gimmes.toml"

        _apply_toml_change(toml, "strategy.threshold", "85")

        assert toml.exists()
