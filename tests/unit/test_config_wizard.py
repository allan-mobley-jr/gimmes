"""Tests for gimmes config wizard module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.exceptions import Exit as ClickExit

from gimmes.config import (
    CONFIG_SECTIONS,
    GimmesConfig,
)
from gimmes.config_wizard import (
    Setting,
    _format_current,
    _iter_section_settings,
    _iter_sections,
    _parse_input,
    _scoring_weights_total,
    resolve_setting,
    run_config_wizard,
    validate_config_value,
)

# ---------------------------------------------------------------------------
# Model introspection tests
# ---------------------------------------------------------------------------


class TestModelMetadata:
    def test_all_sections_have_metadata(self) -> None:
        for field_name, model_cls in CONFIG_SECTIONS:
            extra = model_cls.model_config.get("json_schema_extra", {})
            assert "section_name" in extra, f"{field_name} missing section_name"
            assert "section_description" in extra, f"{field_name} missing section_description"

    def test_all_fields_have_display_name(self) -> None:
        for field_name, model_cls in CONFIG_SECTIONS:
            settings = _iter_section_settings(field_name, model_cls)
            for s in settings:
                assert s.name, f"{s.key} has empty display_name"

    def test_all_fields_have_description(self) -> None:
        for field_name, model_cls in CONFIG_SECTIONS:
            settings = _iter_section_settings(field_name, model_cls)
            for s in settings:
                assert len(s.description) > 20, f"{s.key} has too-short description"

    def test_all_fields_have_valid_type(self) -> None:
        for field_name, model_cls in CONFIG_SECTIONS:
            settings = _iter_section_settings(field_name, model_cls)
            for s in settings:
                valid = ("int", "float", "str", "list")
                assert s.type in valid, f"{s.key} has invalid type {s.type}"

    def test_scoring_weights_have_five_entries(self) -> None:
        scoring_cls = type(GimmesConfig.model_fields["scoring"].default_factory())
        settings = _iter_section_settings("scoring", scoring_cls)
        weight_settings = [s for s in settings if "weights" in s.key]
        assert len(weight_settings) == 5

    def test_scoring_weight_defaults_sum_to_one(self) -> None:
        scoring_cls = type(GimmesConfig.model_fields["scoring"].default_factory())
        settings = _iter_section_settings("scoring", scoring_cls)
        weight_settings = [s for s in settings if "weights" in s.key]
        total = sum(s.default for s in weight_settings)
        assert abs(total - 1.0) < 0.01

    def test_iter_sections_yields_all(self) -> None:
        sections = list(_iter_sections())
        keys = [s[0] for s in sections]
        assert "paper" in keys
        assert "strategy" in keys
        assert "risk" in keys
        assert "scoring" in keys

    def test_bankroll_is_in_risk_section(self) -> None:
        """Regression: bankroll must appear in the wizard (issue #292)."""
        from gimmes.config import RiskConfig
        settings = _iter_section_settings("risk", RiskConfig)
        keys = [s.key for s in settings]
        assert "risk.bankroll_paper" in keys
        assert "risk.bankroll_real" in keys

    def test_monitor_price_trigger_is_in_risk_section(self) -> None:
        """Regression: all risk fields must appear in the wizard."""
        from gimmes.config import RiskConfig
        settings = _iter_section_settings("risk", RiskConfig)
        keys = [s.key for s in settings]
        assert "risk.monitor_price_trigger_pp" in keys

    def test_cycle_timeout_is_in_strategy_section(self) -> None:
        """Regression: cycle_timeout was missing from the wizard."""
        from gimmes.config import StrategyConfig
        settings = _iter_section_settings("strategy", StrategyConfig)
        keys = [s.key for s in settings]
        assert "strategy.cycle_timeout" in keys


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
        with pytest.raises(ValueError, match="between"):
            _parse_input("5", s)

    def test_parse_int_above_max(self) -> None:
        s = Setting(
            key="a.b", name="", description="", type="int",
            default=0, min_val=0, max_val=100,
        )
        with pytest.raises(ValueError, match="between"):
            _parse_input("200", s)

    def test_parse_int_below_min_only(self) -> None:
        s = Setting(
            key="a.b", name="", description="", type="int",
            default=0, min_val=10,
        )
        with pytest.raises(ValueError, match="at least"):
            _parse_input("5", s)

    def test_parse_int_above_max_only(self) -> None:
        s = Setting(
            key="a.b", name="", description="", type="int",
            default=0, max_val=100,
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
        with pytest.raises(ValueError, match="between"):
            _parse_input("0.001", s)

    def test_parse_float_above_max(self) -> None:
        s = Setting(
            key="a.b", name="", description="", type="float",
            default=0.0, min_val=0.0, max_val=1.0,
        )
        with pytest.raises(ValueError, match="between"):
            _parse_input("1.5", s)

    def test_parse_float_below_min_only(self) -> None:
        s = Setting(
            key="a.b", name="", description="", type="float",
            default=0.0, min_val=0.01,
        )
        with pytest.raises(ValueError, match="at least"):
            _parse_input("0.001", s)

    def test_parse_float_above_max_only(self) -> None:
        s = Setting(
            key="a.b", name="", description="", type="float",
            default=0.0, max_val=1.0,
        )
        with pytest.raises(ValueError, match="at most"):
            _parse_input("1.5", s)

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

    def test_format_list_full_shows_all_items(self) -> None:
        s = Setting(key="a.b", name="", description="", type="list", default=[])
        items = [f"ITEM{i}" for i in range(10)]
        result = _format_current(items, s, full=True)
        for item in items:
            assert item in result
        assert "[10 items]" not in result

    def test_format_list_full_one_per_line(self) -> None:
        s = Setting(key="a.b", name="", description="", type="list", default=[])
        result = _format_current(["A", "B", "C"], s, full=True)
        # Every item should be on its own indented line (leading newline)
        assert result.startswith("\n")
        assert "A" in result
        assert "B" in result
        assert "C" in result

    def test_format_int(self) -> None:
        s = Setting(key="a.b", name="", description="", type="int", default=0)
        assert _format_current(75, s) == "75"


# ---------------------------------------------------------------------------
# Scoring weight validation
# ---------------------------------------------------------------------------


class TestScoringWeightsTotal:
    def test_valid_weights_sum_to_one(self) -> None:
        overrides = {
            "scoring": {"weights": {
                "edge_size": 0.30,
                "signal_strength": 0.25,
                "liquidity_depth": 0.15,
                "settlement_clarity": 0.15,
                "time_to_resolution": 0.15,
            }},
        }
        assert abs(_scoring_weights_total(overrides) - 1.0) < 0.01

    def test_invalid_weights_exceed_one(self) -> None:
        overrides = {
            "scoring": {"weights": {
                "edge_size": 0.50,
                "signal_strength": 0.50,
                "liquidity_depth": 0.15,
                "settlement_clarity": 0.15,
                "time_to_resolution": 0.15,
            }},
        }
        assert abs(_scoring_weights_total(overrides) - 1.0) > 0.01

    def test_missing_weights_uses_defaults(self) -> None:
        assert abs(_scoring_weights_total({}) - 1.0) < 0.01


# ---------------------------------------------------------------------------
# Integration: run_config_wizard
# ---------------------------------------------------------------------------


class TestRunConfigWizard:
    @pytest.fixture()
    def db_file(self, tmp_path: Path) -> Path:
        """Create a temporary database with the config table."""
        import sqlite3
        db = tmp_path / "gimmes.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.commit()
        conn.close()
        return db

    def test_exits_when_db_missing(self, tmp_path: Path) -> None:
        with patch("gimmes.config_wizard.DB_PATH", tmp_path / "missing.db"):
            with pytest.raises(ClickExit):
                run_config_wizard()

    def test_invalid_section_exits(self, db_file: Path) -> None:
        with patch("gimmes.config_wizard.DB_PATH", db_file):
            with pytest.raises(ClickExit):
                run_config_wizard(section_filter="nonexistent")

    def test_no_changes_when_all_defaults_kept(self, db_file: Path) -> None:
        with (
            patch("gimmes.config_wizard.DB_PATH", db_file),
            patch("gimmes.config_wizard._prompt_setting", return_value=10000.0),
            patch("gimmes.config_wizard.save_config_value") as mock_save,
        ):
            run_config_wizard(section_filter="paper")

        mock_save.assert_not_called()

    def test_saves_changed_value(self, db_file: Path) -> None:
        with (
            patch("gimmes.config_wizard.DB_PATH", db_file),
            patch("gimmes.config_wizard._prompt_setting", return_value=5000.0),
        ):
            run_config_wizard(section_filter="paper")

        import json
        import sqlite3
        conn = sqlite3.connect(str(db_file))
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'paper.starting_balance'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert json.loads(row[0]) == 5000.0

    def test_section_filter_limits_settings(self, db_file: Path) -> None:
        prompted_keys: list[str] = []

        def fake_prompt(setting: Setting, current: object, **kwargs: object) -> object:
            prompted_keys.append(setting.key)
            return current

        with (
            patch("gimmes.config_wizard.DB_PATH", db_file),
            patch("gimmes.config_wizard._prompt_setting", side_effect=fake_prompt),
        ):
            run_config_wizard(section_filter="paper")

        # Only paper settings should have been prompted
        assert all(k.startswith("paper.") for k in prompted_keys)
        assert len(prompted_keys) == 1

    def test_new_only_skips_existing_settings(self, db_file: Path) -> None:
        import json
        import sqlite3

        # Pre-populate paper.starting_balance so it's not "new"
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("paper.starting_balance", json.dumps(10000.0)),
        )
        conn.commit()
        conn.close()

        prompted_keys: list[str] = []

        def fake_prompt(setting: Setting, current: object, **kwargs: object) -> object:
            prompted_keys.append(setting.key)
            return current

        with (
            patch("gimmes.config_wizard.DB_PATH", db_file),
            patch("gimmes.config_wizard._prompt_setting", side_effect=fake_prompt),
        ):
            run_config_wizard(new_only=True)

        # paper.starting_balance was in DB, so it should NOT have been prompted
        assert "paper.starting_balance" not in prompted_keys
        # But other settings should have been prompted (they are all "new")
        assert len(prompted_keys) > 0

    def test_new_only_no_new_settings_exits_early(self, db_file: Path) -> None:
        import json
        import sqlite3

        # Pre-populate ALL settings so none are "new"
        all_keys = [
            s.key
            for _, _, _, settings in _iter_sections()
            for s in settings
        ]
        conn = sqlite3.connect(str(db_file))
        for key in all_keys:
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, json.dumps(0)),
            )
        conn.commit()
        conn.close()

        with (
            patch("gimmes.config_wizard.DB_PATH", db_file),
            patch("gimmes.config_wizard._prompt_setting") as mock_prompt,
        ):
            run_config_wizard(new_only=True)

        mock_prompt.assert_not_called()

    def test_new_only_with_section_filter_exits_when_section_configured(
        self, db_file: Path,
    ) -> None:
        import json
        import sqlite3

        # Pre-populate only the paper section's settings
        paper_keys = [
            s.key
            for _, _, _, settings in _iter_sections()
            for s in settings
            if s.key.startswith("paper.")
        ]
        conn = sqlite3.connect(str(db_file))
        for key in paper_keys:
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, json.dumps(0)),
            )
        conn.commit()
        conn.close()

        with (
            patch("gimmes.config_wizard.DB_PATH", db_file),
            patch("gimmes.config_wizard._prompt_setting") as mock_prompt,
        ):
            run_config_wizard(section_filter="paper", new_only=True)

        # Paper is fully configured, so nothing should be prompted
        # even though other sections have new settings
        mock_prompt.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_setting
# ---------------------------------------------------------------------------


class TestResolveSetting:
    def test_resolves_strategy_key(self) -> None:
        s = resolve_setting("strategy.gimme_threshold")
        assert s.key == "strategy.gimme_threshold"
        assert s.type == "int"

    def test_resolves_nested_scoring_weight(self) -> None:
        s = resolve_setting("scoring.weights.edge_size")
        assert s.key == "scoring.weights.edge_size"
        assert s.type == "float"

    def test_resolves_list_key(self) -> None:
        s = resolve_setting("scanner.series")
        assert s.key == "scanner.series"
        assert s.type == "list"

    def test_raises_on_unknown_key(self) -> None:
        with pytest.raises(ValueError, match="Unknown config key"):
            resolve_setting("nonexistent.key")

    def test_error_lists_valid_keys(self) -> None:
        with pytest.raises(ValueError, match="strategy.gimme_threshold"):
            resolve_setting("bad.key")


# ---------------------------------------------------------------------------
# validate_config_value
# ---------------------------------------------------------------------------


class TestValidateConfigValue:
    def test_validates_int_in_range(self) -> None:
        setting, parsed = validate_config_value("strategy.gimme_threshold", "80")
        assert parsed == 80
        assert setting.type == "int"

    def test_validates_float(self) -> None:
        setting, parsed = validate_config_value("sizing.kelly_fraction", "0.30")
        assert parsed == 0.30

    def test_validates_str_choice(self) -> None:
        setting, parsed = validate_config_value("orders.preferred_order_type", "taker")
        assert parsed == "taker"

    def test_validates_list(self) -> None:
        setting, parsed = validate_config_value("scanner.series", "KXCPI,KXGDP")
        assert parsed == ["KXCPI", "KXGDP"]

    def test_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="between"):
            validate_config_value("strategy.gimme_threshold", "200")

    def test_rejects_invalid_choice(self) -> None:
        with pytest.raises(ValueError, match="Must be one of"):
            validate_config_value("orders.preferred_order_type", "invalid")

    def test_rejects_unknown_key(self) -> None:
        with pytest.raises(ValueError, match="Unknown config key"):
            validate_config_value("bad.key", "42")
