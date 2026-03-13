"""Tests for gimmes config wizard module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit
from click.exceptions import Exit as ClickExit

from gimmes.config_wizard import (
    SECTION_KEYS,
    SETTINGS,
    Setting,
    _format_current,
    _get_nested,
    _load_toml,
    _parse_input,
    _save_toml,
    _set_nested,
    _validate_scoring_weights,
    run_config_wizard,
)

# ---------------------------------------------------------------------------
# Setting metadata tests
# ---------------------------------------------------------------------------


class TestSettingMetadata:
    def test_all_settings_have_valid_section(self) -> None:
        for s in SETTINGS:
            assert s.section in SECTION_KEYS, f"{s.key} has unknown section {s.section}"

    def test_all_settings_have_description(self) -> None:
        for s in SETTINGS:
            assert len(s.description) > 20, f"{s.key} has a too-short description"

    def test_all_settings_have_valid_type(self) -> None:
        for s in SETTINGS:
            assert s.type in ("int", "float", "str", "list"), f"{s.key} has invalid type {s.type}"

    def test_scoring_weights_have_five_entries(self) -> None:
        weights = [s for s in SETTINGS if s.key.startswith("scoring.weights.")]
        assert len(weights) == 5

    def test_scoring_weight_defaults_sum_to_one(self) -> None:
        weights = [s for s in SETTINGS if s.key.startswith("scoring.weights.")]
        total = sum(s.default for s in weights)  # type: ignore[arg-type]
        assert abs(total - 1.0) < 0.01


# ---------------------------------------------------------------------------
# TOML helpers
# ---------------------------------------------------------------------------


class TestTomlHelpers:
    def test_load_toml_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "test.toml"
        f.write_text('[strategy]\ngimme_threshold = 80\n')
        doc = _load_toml(f)
        assert doc["strategy"]["gimme_threshold"] == 80  # type: ignore[index]

    def test_load_toml_missing_file(self, tmp_path: Path) -> None:
        doc = _load_toml(tmp_path / "missing.toml")
        assert len(doc) == 0

    def test_get_nested_simple(self) -> None:
        doc = tomlkit.parse("[strategy]\ngimme_threshold = 75\n")
        assert _get_nested(doc, "strategy.gimme_threshold") == 75

    def test_get_nested_deep(self) -> None:
        doc = tomlkit.parse("[scoring.weights]\nedge_size = 0.30\n")
        assert _get_nested(doc, "scoring.weights.edge_size") == 0.30

    def test_get_nested_missing(self) -> None:
        doc = tomlkit.parse("")
        assert _get_nested(doc, "strategy.gimme_threshold") is None

    def test_set_nested_creates_tables(self) -> None:
        doc = tomlkit.document()
        _set_nested(doc, "strategy.gimme_threshold", 80)
        assert doc["strategy"]["gimme_threshold"] == 80  # type: ignore[index]

    def test_set_nested_deep(self) -> None:
        doc = tomlkit.document()
        _set_nested(doc, "scoring.weights.edge_size", 0.40)
        assert doc["scoring"]["weights"]["edge_size"] == 0.40  # type: ignore[index]

    def test_save_toml_roundtrip(self, tmp_path: Path) -> None:
        f = tmp_path / "config" / "test.toml"
        doc = tomlkit.document()
        _set_nested(doc, "strategy.gimme_threshold", 80)
        _save_toml(doc, f)
        assert f.exists()
        loaded = _load_toml(f)
        assert _get_nested(loaded, "strategy.gimme_threshold") == 80

    def test_save_toml_preserves_comments(self, tmp_path: Path) -> None:
        f = tmp_path / "test.toml"
        original = '[strategy]\ngimme_threshold = 75  # Minimum score\n'
        f.write_text(original)
        doc = _load_toml(f)
        doc["strategy"]["gimme_threshold"] = 80  # type: ignore[index]
        _save_toml(doc, f)
        content = f.read_text()
        assert "# Minimum score" in content
        assert "80" in content


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


class TestParseInput:
    def test_parse_int(self) -> None:
        s = Setting(
            key="a.b", name="", description="", type="int",
            default=0, min_val=0, max_val=100,
        )
        assert _parse_input("42", s) == 42

    def test_parse_int_below_min(self) -> None:
        s = Setting(
            key="a.b", name="", description="", type="int",
            default=0, min_val=10, max_val=100,
        )
        with pytest.raises(ValueError, match="at least"):
            _parse_input("5", s)

    def test_parse_int_above_max(self) -> None:
        s = Setting(
            key="a.b", name="", description="", type="int",
            default=0, min_val=0, max_val=100,
        )
        with pytest.raises(ValueError, match="at most"):
            _parse_input("200", s)

    def test_parse_float(self) -> None:
        s = Setting(
            key="a.b", name="", description="", type="float",
            default=0.0, min_val=0.0, max_val=1.0,
        )
        assert _parse_input("0.25", s) == 0.25

    def test_parse_float_below_min(self) -> None:
        s = Setting(
            key="a.b", name="", description="", type="float",
            default=0.0, min_val=0.01, max_val=1.0,
        )
        with pytest.raises(ValueError, match="at least"):
            _parse_input("0.001", s)

    def test_parse_str_valid_choice(self) -> None:
        s = Setting(key="a.b", name="", description="", type="str", default="a", choices=["a", "b"])
        assert _parse_input("b", s) == "b"

    def test_parse_str_invalid_choice(self) -> None:
        s = Setting(key="a.b", name="", description="", type="str", default="a", choices=["a", "b"])
        with pytest.raises(ValueError, match="Must be one of"):
            _parse_input("c", s)

    def test_parse_list(self) -> None:
        s = Setting(key="a.b", name="", description="", type="list", default=[])
        result = _parse_input("KXCPI, KXGDP, KXFED", s)
        assert result == ["KXCPI", "KXGDP", "KXFED"]

    def test_parse_list_strips_whitespace(self) -> None:
        s = Setting(key="a.b", name="", description="", type="list", default=[])
        result = _parse_input("  A ,  B  , C  ", s)
        assert result == ["A", "B", "C"]

    def test_parse_invalid_int(self) -> None:
        s = Setting(key="a.b", name="", description="", type="int", default=0)
        with pytest.raises(ValueError):
            _parse_input("not_a_number", s)


# ---------------------------------------------------------------------------
# Format display
# ---------------------------------------------------------------------------


class TestFormatCurrent:
    def test_format_float_small(self) -> None:
        s = Setting(key="a.b", name="", description="", type="float", default=0.0)
        assert _format_current(0.25, s) == "0.25"

    def test_format_float_large(self) -> None:
        s = Setting(key="a.b", name="", description="", type="float", default=0.0)
        assert _format_current(10000.0, s) == "10,000.00"

    def test_format_list_short(self) -> None:
        s = Setting(key="a.b", name="", description="", type="list", default=[])
        assert _format_current(["A", "B", "C"], s) == "A, B, C"

    def test_format_list_long(self) -> None:
        s = Setting(key="a.b", name="", description="", type="list", default=[])
        items = [f"ITEM{i}" for i in range(10)]
        result = _format_current(items, s)
        assert "[10 items]" in result

    def test_format_int(self) -> None:
        s = Setting(key="a.b", name="", description="", type="int", default=0)
        assert _format_current(75, s) == "75"


# ---------------------------------------------------------------------------
# Scoring weight validation
# ---------------------------------------------------------------------------


class TestValidateScoringWeights:
    def test_valid_weights(self) -> None:
        doc = tomlkit.parse(
            "[scoring.weights]\n"
            "edge_size = 0.30\n"
            "signal_strength = 0.25\n"
            "liquidity_depth = 0.15\n"
            "settlement_clarity = 0.15\n"
            "time_to_resolution = 0.15\n"
        )
        assert _validate_scoring_weights(doc) is True

    def test_invalid_weights(self) -> None:
        doc = tomlkit.parse(
            "[scoring.weights]\n"
            "edge_size = 0.50\n"
            "signal_strength = 0.50\n"
            "liquidity_depth = 0.15\n"
            "settlement_clarity = 0.15\n"
            "time_to_resolution = 0.15\n"
        )
        assert _validate_scoring_weights(doc) is False

    def test_missing_weights_treated_as_zero(self) -> None:
        doc = tomlkit.parse("[scoring.weights]\nedge_size = 0.30\n")
        assert _validate_scoring_weights(doc) is False


# ---------------------------------------------------------------------------
# Integration: run_config_wizard
# ---------------------------------------------------------------------------


class TestRunConfigWizard:
    def test_exits_when_toml_missing(self, tmp_path: Path) -> None:
        with patch("gimmes.config_wizard.TOML_FILE", tmp_path / "missing.toml"):
            with pytest.raises(ClickExit):
                run_config_wizard()

    def test_invalid_section_exits(self, tmp_path: Path) -> None:
        f = tmp_path / "gimmes.toml"
        f.write_text("[strategy]\ngimme_threshold = 75\n")
        with patch("gimmes.config_wizard.TOML_FILE", f):
            with pytest.raises(ClickExit):
                run_config_wizard(section_filter="nonexistent")

    def test_no_changes_when_all_defaults_kept(self, tmp_path: Path) -> None:
        f = tmp_path / "gimmes.toml"
        f.write_text("[paper]\nstarting_balance = 10000.00\n")

        with (
            patch("gimmes.config_wizard.TOML_FILE", f),
            patch("gimmes.config_wizard._prompt_setting", return_value=10000.0),
            patch("gimmes.config_wizard._save_toml") as mock_save,
        ):
            run_config_wizard(section_filter="paper")

        mock_save.assert_not_called()

    def test_saves_changed_value(self, tmp_path: Path) -> None:
        f = tmp_path / "gimmes.toml"
        f.write_text("[paper]\nstarting_balance = 10000.00\n")

        with (
            patch("gimmes.config_wizard.TOML_FILE", f),
            patch("gimmes.config_wizard._prompt_setting", return_value=5000.0),
        ):
            run_config_wizard(section_filter="paper")

        doc = tomlkit.parse(f.read_text())
        assert doc["paper"]["starting_balance"] == 5000.0  # type: ignore[index]

    def test_section_filter_limits_settings(self, tmp_path: Path) -> None:
        f = tmp_path / "gimmes.toml"
        f.write_text(
            "[paper]\nstarting_balance = 10000.00\n"
            "[strategy]\ngimme_threshold = 75\n"
        )

        prompted_keys: list[str] = []

        def fake_prompt(setting: Setting, current: object) -> object:
            prompted_keys.append(setting.key)
            return current

        with (
            patch("gimmes.config_wizard.TOML_FILE", f),
            patch("gimmes.config_wizard._prompt_setting", side_effect=fake_prompt),
        ):
            run_config_wizard(section_filter="paper")

        # Only paper settings should have been prompted
        assert all(k.startswith("paper.") for k in prompted_keys)
        assert len(prompted_keys) == 1
