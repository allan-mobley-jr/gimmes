"""Tests for the budget-cap config precedence (CLI > config > default)."""

from __future__ import annotations

import pytest

from gimmes.budget import DEFAULT_MAX_SESSIONS, DEFAULT_MAX_USD
from gimmes.config import BudgetConfig, GimmesConfig


class TestBudgetConfigDefaults:
    def test_defaults_are_none(self) -> None:
        """Unset config means the loop falls through to hardcoded
        DEFAULT_MAX_* constants. None signals 'no override'."""
        cfg = BudgetConfig()
        assert cfg.max_daily_cost_usd is None
        assert cfg.max_sessions_per_day is None

    def test_zero_is_explicit_unlimited(self) -> None:
        """0 is a valid value — matches the CLI flag semantics
        (`--max-daily-cost-usd 0` = unlimited). Distinct from None
        (use hardcoded default)."""
        cfg = BudgetConfig(max_daily_cost_usd=0.0, max_sessions_per_day=0)
        assert cfg.max_daily_cost_usd == 0.0
        assert cfg.max_sessions_per_day == 0

    def test_negative_rejected(self) -> None:
        """Negative caps make no sense — Pydantic ge=0 enforces this."""
        with pytest.raises(ValueError):
            BudgetConfig(max_daily_cost_usd=-1.0)
        with pytest.raises(ValueError):
            BudgetConfig(max_sessions_per_day=-1)

    def test_typical_override(self) -> None:
        """The common case: operator sets a higher cap to keep the
        loop running longer per day."""
        cfg = BudgetConfig(max_daily_cost_usd=50.0, max_sessions_per_day=120)
        assert cfg.max_daily_cost_usd == 50.0
        assert cfg.max_sessions_per_day == 120


class TestBudgetConfigInGimmesConfig:
    def test_budget_is_attached_to_main_config(self) -> None:
        """GimmesConfig must expose `budget` as a sub-config so
        `gimmes config set budget.max_daily_cost_usd 50` resolves."""
        cfg = GimmesConfig()
        assert hasattr(cfg, "budget")
        assert isinstance(cfg.budget, BudgetConfig)

    def test_budget_field_extras_marked_forbid(self) -> None:
        """BudgetConfig must reject unknown fields so typos in config
        get a clear error rather than silently producing a no-op."""
        with pytest.raises(ValueError):
            BudgetConfig(unknown_field=42)


class TestBudgetDefaultsConstants:
    """Pin the hardcoded defaults the config falls back to. If these
    change, the docstring/test should change in lockstep."""

    def test_default_max_usd(self) -> None:
        assert DEFAULT_MAX_USD == 25.0

    def test_default_max_sessions(self) -> None:
        assert DEFAULT_MAX_SESSIONS == 80


class TestResolveBudgetCap:
    """Precedence: CLI flag wins; if absent, fall back to config; if
    both absent, use the hardcoded default. 0 (from either CLI or
    config) is preserved as 'unlimited', NOT collapsed to default."""

    def test_cli_value_wins_over_config(self) -> None:
        from gimmes.cli import _resolve_budget_cap

        result = _resolve_budget_cap(
            cli_value=100.0, config_value=50.0, default=25.0,
        )
        assert result == 100.0

    def test_cli_value_wins_over_default(self) -> None:
        from gimmes.cli import _resolve_budget_cap

        result = _resolve_budget_cap(
            cli_value=100.0, config_value=None, default=25.0,
        )
        assert result == 100.0

    def test_config_wins_when_cli_absent(self) -> None:
        from gimmes.cli import _resolve_budget_cap

        result = _resolve_budget_cap(
            cli_value=None, config_value=50.0, default=25.0,
        )
        assert result == 50.0

    def test_default_when_both_absent(self) -> None:
        from gimmes.cli import _resolve_budget_cap

        result = _resolve_budget_cap(
            cli_value=None, config_value=None, default=25.0,
        )
        assert result == 25.0

    def test_cli_zero_means_unlimited_not_default(self) -> None:
        """0 from the CLI is the documented 'unlimited' value. It MUST
        be preserved — collapsing it to the default would silently
        re-enforce the cap the operator was trying to remove."""
        from gimmes.cli import _resolve_budget_cap

        result = _resolve_budget_cap(
            cli_value=0.0, config_value=50.0, default=25.0,
        )
        assert result == 0.0

    def test_config_zero_means_unlimited_not_default(self) -> None:
        from gimmes.cli import _resolve_budget_cap

        result = _resolve_budget_cap(
            cli_value=None, config_value=0.0, default=25.0,
        )
        assert result == 0.0

    def test_works_with_int_session_caps(self) -> None:
        """max_sessions_per_day is an int, max_daily_cost_usd is a
        float — the helper is generic and handles both."""
        from gimmes.cli import _resolve_budget_cap

        assert _resolve_budget_cap(
            cli_value=None, config_value=120, default=80,
        ) == 120
        assert _resolve_budget_cap(
            cli_value=None, config_value=None, default=80,
        ) == 80


class TestBudgetConfigCLIIntegration:
    """End-to-end: the `gimmes config set budget.*` path must actually
    work. Without CONFIG_SECTIONS registration, `resolve_setting` would
    reject these keys and the headline feature would be unusable."""

    def test_max_daily_cost_usd_resolves_via_wizard(self) -> None:
        from gimmes.config_wizard import resolve_setting

        setting = resolve_setting("budget.max_daily_cost_usd")
        assert setting.key == "budget.max_daily_cost_usd"
        assert setting.type == "float"  # NOT "str" — Optional unwrapped
        assert setting.default is None

    def test_max_sessions_per_day_resolves_via_wizard(self) -> None:
        from gimmes.config_wizard import resolve_setting

        setting = resolve_setting("budget.max_sessions_per_day")
        assert setting.key == "budget.max_sessions_per_day"
        assert setting.type == "int"  # NOT "str" — Optional unwrapped
        assert setting.default is None

    def test_budget_section_listed_in_config_sections(self) -> None:
        """CONFIG_SECTIONS drives the wizard walkthrough order AND
        `gimmes config get/set` key resolution. Missing entry means
        the entire BudgetConfig is invisible to the CLI (#28 ship-
        blocker found in review pipeline)."""
        from gimmes.config import CONFIG_SECTIONS, BudgetConfig

        section_names = [name for name, _ in CONFIG_SECTIONS]
        assert "budget" in section_names
        section_models = {name: cls for name, cls in CONFIG_SECTIONS}
        assert section_models["budget"] is BudgetConfig

    def test_field_type_str_unwraps_optional_float(self) -> None:
        """Optional[T] / T | None annotations must yield T's type
        string, not 'str'. Without this fix the wizard would store
        user input as the literal string and skip ge=0 validation."""
        from gimmes.config import BudgetConfig
        from gimmes.config_wizard import _field_type_str

        cost_field = BudgetConfig.model_fields["max_daily_cost_usd"]
        sessions_field = BudgetConfig.model_fields["max_sessions_per_day"]
        assert _field_type_str(cost_field) == "float"
        assert _field_type_str(sessions_field) == "int"

    def test_load_config_wires_budget_sub_config(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """load_config() MUST pass `budget=BudgetConfig(...)` when
        constructing GimmesConfig — otherwise values saved via
        `gimmes config set budget.*` are persisted to the DB but
        never reach the runtime config object, and _autonomous_loop
        always sees defaults. Found by Copilot review on PR #626."""
        import sqlite3

        from gimmes.config import load_config
        from gimmes.store.database import Database

        # Build a DB with a budget override in the config table.
        db_path = tmp_path / "test.db"
        import asyncio

        async def _setup() -> None:
            db = Database(db_path)
            await db.connect()
            await db.close()

        asyncio.run(_setup())

        # Insert the budget override directly (schema is created by
        # Database.connect via migrations).
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                ("budget.max_daily_cost_usd", "50.0"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                ("budget.max_sessions_per_day", "120"),
            )
            conn.commit()
        finally:
            conn.close()

        cfg = load_config(db_path=db_path)
        # If load_config doesn't wire budget, both values would be
        # None (BudgetConfig's default) regardless of what's in the DB.
        assert cfg.budget.max_daily_cost_usd == 50.0
        assert cfg.budget.max_sessions_per_day == 120

    def test_min_val_in_json_schema_extra(self) -> None:
        """Pydantic's `ge=0.0` rejects negatives at model construction
        time, but `gimmes config set` validates ranges using
        `json_schema_extra['min_val']`. Without `min_val: 0`, a user
        could save `-1` to the DB and only see the error later when
        the loaded config tried to construct BudgetConfig — a
        confusing UX. Found by Copilot review on PR #626."""
        from gimmes.config import BudgetConfig

        cost_extra = (
            BudgetConfig.model_fields["max_daily_cost_usd"].json_schema_extra
            or {}
        )
        sessions_extra = (
            BudgetConfig.model_fields["max_sessions_per_day"].json_schema_extra
            or {}
        )
        assert cost_extra.get("min_val") == 0.0
        assert sessions_extra.get("min_val") == 0
