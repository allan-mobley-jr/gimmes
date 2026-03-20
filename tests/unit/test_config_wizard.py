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
    run_config_wizard,
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
        assert "risk.bankroll" in keys

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
