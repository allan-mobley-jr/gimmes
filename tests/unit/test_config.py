"""Tests for database-backed config loading and saving."""

from __future__ import annotations

import json
import sqlite3

import pytest

from gimmes.config import (
    DEFAULT_SERIES,
    GIMME_SERIES,
    SERIES_CATEGORIES,
    GimmesConfig,
    Mode,
    RiskConfig,
    ScannerConfig,
    SideOverrides,
    StrategyConfig,
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
        save_config_value("risk.bankroll_paper", 1000.0, db_path=db)

        result = config_keys_in_db(db)
        assert result == {"strategy.gimme_threshold", "risk.bankroll_paper"}


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
            "risk.bankroll_paper": 1000.0,
        }, db_path=db)

        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT key, value FROM config ORDER BY key").fetchall()
        conn.close()
        assert len(rows) == 2
        assert json.loads(rows[0][1]) == 1000.0  # risk.bankroll_paper
        assert json.loads(rows[1][1]) == 80  # strategy.gimme_threshold


class TestSaveAutoCreatesTable:
    def test_creates_table_when_missing(self, tmp_path):
        """save_config_values auto-creates the config table in a pre-migration DB."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.commit()
        conn.close()

        save_config_values({"strategy.gimme_threshold": 80}, db_path=db)

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT value FROM config WHERE key = ?",
            ("strategy.gimme_threshold",),
        ).fetchone()
        conn.close()
        assert json.loads(row[0]) == 80

    def test_creates_db_and_table_from_scratch(self, tmp_path):
        """save_config_values works when the DB file does not exist."""
        db = tmp_path / "subdir" / "new.db"

        save_config_values({"risk.bankroll_paper": 500.0}, db_path=db)

        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT value FROM config WHERE key = ?",
            ("risk.bankroll_paper",),
        ).fetchone()
        conn.close()
        assert json.loads(row[0]) == 500.0


class TestLoadConfig:
    def test_loads_defaults_when_no_db(self, tmp_path):
        config = load_config(db_path=tmp_path / "nonexistent.db")
        assert config.strategy.gimme_threshold == 75
        assert config.risk.bankroll_paper == 5_000.0

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


class TestCMMinEdgeAfterFees:
    """Tests for strategy.cm_min_edge_after_fees (issue #523)."""

    def test_default_is_five_pp(self) -> None:
        assert StrategyConfig().cm_min_edge_after_fees == 0.05

    @pytest.mark.parametrize("value", [-0.01, 0.51, 1.01])
    def test_bounds_reject_out_of_range(self, value: float) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            StrategyConfig(cm_min_edge_after_fees=value)

    def test_invariant_rejects_explicit_cm_below_validator(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            StrategyConfig(
                min_edge_after_fees=0.05,
                cm_min_edge_after_fees=0.01,
            )
        assert "must be >= strategy.min_edge_after_fees" in str(exc_info.value)

    def test_cm_auto_bumps_to_validator_when_not_set(self) -> None:
        # Backward compatibility: users who have raised min_edge_after_fees
        # above the cm default shouldn't error on upgrade. When cm isn't
        # explicitly set, it tracks the validator floor.
        s = StrategyConfig(min_edge_after_fees=0.08)
        assert s.cm_min_edge_after_fees == 0.08

    def test_invariant_allows_cm_equal_to_validator(self) -> None:
        s = StrategyConfig(
            min_edge_after_fees=0.05, cm_min_edge_after_fees=0.05,
        )
        assert s.cm_min_edge_after_fees == 0.05

    def test_invariant_allows_cm_above_validator(self) -> None:
        s = StrategyConfig(
            min_edge_after_fees=0.03, cm_min_edge_after_fees=0.08,
        )
        assert s.cm_min_edge_after_fees == 0.08

    def test_override_via_db(self, tmp_path) -> None:
        db = tmp_path / "test.db"
        _create_config_db(db)
        save_config_value("strategy.cm_min_edge_after_fees", 0.08, db_path=db)
        config = load_config(db_path=db)
        assert config.strategy.cm_min_edge_after_fees == 0.08


class TestDefaultSeries:
    def test_series_defaults_to_curated_list_when_no_db(self, tmp_path):
        config = load_config(db_path=tmp_path / "nonexistent.db")
        assert config.scanner.series == GIMME_SERIES

    def test_series_defaults_to_curated_list_with_empty_db(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        config = load_config(db_path=db)
        assert config.scanner.series == GIMME_SERIES

    def test_explicit_series_overrides_default(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        save_config_value("scanner.series", ["KXGDP"], db_path=db)
        config = load_config(db_path=db)
        assert config.scanner.series == ["KXGDP"]

    def test_explicit_empty_series_overrides_default(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        save_config_value("scanner.series", [], db_path=db)
        config = load_config(db_path=db)
        assert config.scanner.series == []

    def test_default_series_matches_categories(self):
        flat = [t for tickers in SERIES_CATEGORIES.values() for t in tickers]
        assert flat == DEFAULT_SERIES

    def test_series_categories_no_duplicates(self):
        all_tickers = [t for tickers in SERIES_CATEGORIES.values() for t in tickers]
        assert len(all_tickers) == len(set(all_tickers)), "Duplicate ticker in SERIES_CATEGORIES"


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


class TestPositionStopLoss:
    def test_default_is_015(self, tmp_path):
        config = load_config(db_path=tmp_path / "nonexistent.db")
        assert config.risk.position_stop_loss_pct == 0.15

    def test_loads_from_db(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        save_config_value("risk.position_stop_loss_pct", 0.25, db_path=db)

        config = load_config(db_path=db)
        assert config.risk.position_stop_loss_pct == 0.25


class TestPositionTakeProfit:
    def test_default_is_080(self, tmp_path):
        config = load_config(db_path=tmp_path / "nonexistent.db")
        assert config.risk.position_take_profit_pct == 0.80

    def test_loads_from_db(self, tmp_path):
        db = tmp_path / "test.db"
        _create_config_db(db)
        save_config_value("risk.position_take_profit_pct", 0.70, db_path=db)

        config = load_config(db_path=db)
        assert config.risk.position_take_profit_pct == 0.70


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


class TestBankrollProperty:
    def test_driving_range_returns_paper(self):
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            risk=RiskConfig(bankroll_paper=3000.0, bankroll_real=800.0),
        )
        assert config.bankroll == 3000.0

    def test_championship_returns_real(self):
        config = GimmesConfig(
            mode=Mode.CHAMPIONSHIP,
            risk=RiskConfig(bankroll_paper=3000.0, bankroll_real=800.0),
        )
        assert config.bankroll == 800.0

    def test_championship_default_is_zero(self):
        config = GimmesConfig(mode=Mode.CHAMPIONSHIP)
        assert config.bankroll == 0.0


class TestMigrationV14:
    """Migration v14: split risk.bankroll → bankroll_paper (bankroll_real stays 0)."""

    def _seed_v13_db(self, path, bankroll_value=None):
        """Create a DB at version 13 with optional risk.bankroll row."""
        import json
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, "
            "applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (13)")
        conn.execute(
            "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        if bankroll_value is not None:
            conn.execute(
                "INSERT INTO config (key, value) VALUES (?, ?)",
                ("risk.bankroll", json.dumps(bankroll_value)),
            )
        conn.commit()
        conn.close()

    @pytest.mark.asyncio
    async def test_migrates_old_bankroll_to_paper_only(self, tmp_path):
        from gimmes.store.database import Database
        from gimmes.store.migrations import run_migrations

        db_path = tmp_path / "v14.db"
        self._seed_v13_db(db_path, bankroll_value=750.0)

        async with Database(db_path) as db:
            version = await run_migrations(db)

        assert version >= 14
        conn = sqlite3.connect(str(db_path))
        # bankroll_paper should have the old value
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'risk.bankroll_paper'"
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == 750.0
        # bankroll_real should NOT exist (stays at default 0)
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'risk.bankroll_real'"
        ).fetchone()
        assert row is None
        # old key should be deleted
        row = conn.execute(
            "SELECT value FROM config WHERE key = 'risk.bankroll'"
        ).fetchone()
        assert row is None
        conn.close()

    @pytest.mark.asyncio
    async def test_no_old_key_still_bumps_version(self, tmp_path):
        from gimmes.store.database import Database
        from gimmes.store.migrations import run_migrations

        db_path = tmp_path / "v14_fresh.db"
        self._seed_v13_db(db_path)  # no bankroll row

        async with Database(db_path) as db:
            version = await run_migrations(db)

        assert version >= 14

    @pytest.mark.asyncio
    async def test_idempotent(self, tmp_path):
        from gimmes.store.database import Database
        from gimmes.store.migrations import run_migrations

        db_path = tmp_path / "v14_idem.db"
        self._seed_v13_db(db_path, bankroll_value=750.0)

        async with Database(db_path) as db:
            await run_migrations(db)
        async with Database(db_path) as db:
            version = await run_migrations(db)

        assert version >= 14


class TestSidesConfig:
    def test_sides_to_scan_single_yes(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="yes"),
        )
        assert config.sides_to_scan == ["yes"]

    def test_sides_to_scan_single_no(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="no"),
        )
        assert config.sides_to_scan == ["no"]

    def test_sides_to_scan_both(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="both"),
        )
        assert config.sides_to_scan == ["yes", "no"]

    def test_effective_config_single_side_returns_self(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="no"),
        )
        assert config.effective_config_for_side("no") is config

    def test_effective_config_both_applies_yes_overrides(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(
                side="both",
                min_market_price=0.40,
                yes_overrides=SideOverrides(
                    min_market_price=0.70,
                    min_true_probability=0.85,
                ),
            ),
        )
        yes_cfg = config.effective_config_for_side("yes")
        assert yes_cfg.strategy.side == "yes"
        assert yes_cfg.strategy.min_market_price == 0.70
        assert yes_cfg.strategy.min_true_probability == 0.85
        assert yes_cfg.strategy.max_market_price == config.strategy.max_market_price

    def test_effective_config_both_applies_no_overrides(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(
                side="both",
                min_market_price=0.55,
                no_overrides=SideOverrides(
                    min_market_price=0.40,
                    gimme_threshold=65,
                ),
            ),
        )
        no_cfg = config.effective_config_for_side("no")
        assert no_cfg.strategy.side == "no"
        assert no_cfg.strategy.min_market_price == 0.40
        assert no_cfg.strategy.gimme_threshold == 65

    def test_effective_config_both_applies_side_series(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="both"),
            scanner=ScannerConfig(
                series=["KXCPI", "KXGDP"],
                yes_series=["KXINX", "KXNASDAQ100"],
                no_series=["KXCPI", "KXGDP", "KXPAYROLLS"],
            ),
        )
        yes_cfg = config.effective_config_for_side("yes")
        assert yes_cfg.scanner.series == ["KXINX", "KXNASDAQ100"]

        no_cfg = config.effective_config_for_side("no")
        assert no_cfg.scanner.series == ["KXCPI", "KXGDP", "KXPAYROLLS"]

    def test_effective_config_preserves_other_sections(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(side="both"),
            risk=RiskConfig(bankroll_paper=8000.0),
        )
        yes_cfg = config.effective_config_for_side("yes")
        assert yes_cfg.risk.bankroll_paper == 8000.0
        assert yes_cfg.mode == Mode.DRIVING_RANGE


class TestHourlyConfig:
    """#722: hourly-ladder config plumbing — default-inert while
    scanner.hourly_series is empty."""

    def test_hourly_defaults(self) -> None:
        config = GimmesConfig(mode=Mode.DRIVING_RANGE)
        assert config.scanner.hourly_series == []
        assert config.scanner.hourly_lead_minutes == 29
        assert config.scanner.hourly_max_cycles_per_window == 1
        assert config.strategy.hourly_min_true_probability == 0.70
        assert config.strategy.hourly_min_market_price == 0.30
        assert config.strategy.hourly_max_market_price == 0.85

    def test_is_hourly_ticker_empty_series_always_false(self) -> None:
        config = GimmesConfig(mode=Mode.DRIVING_RANGE)
        assert config.is_hourly_ticker("KXBTCD-26JUN23H14-T119999.99") is False
        assert config.is_hourly_ticker("KXBTCD") is False

    def test_is_hourly_ticker_prefix_match(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            scanner=ScannerConfig(hourly_series=["KXBTCD"]),
        )
        assert config.is_hourly_ticker("KXBTCD-26JUN23H14-T119999.99") is True
        assert config.is_hourly_ticker("KXBTCD") is True
        # Exact series match, not a substring: KXBTCDX is a different series
        assert config.is_hourly_ticker("KXBTCDX-26JUN23H14-T1") is False
        assert config.is_hourly_ticker("KXCPIYOY-26MAR-T3.5") is False

    def test_hourly_fields_flow_through_side_split(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(
                side="both",
                no_overrides=SideOverrides(min_market_price=0.40),
            ),
            scanner=ScannerConfig(hourly_series=["KXBTCD"]),
        )
        no_cfg = config.effective_config_for_side("no")
        assert no_cfg.scanner.hourly_series == ["KXBTCD"]
        assert no_cfg.scanner.hourly_lead_minutes == 29
        assert no_cfg.strategy.hourly_min_true_probability == 0.70
        assert no_cfg.strategy.hourly_min_market_price == 0.30
        assert no_cfg.strategy.hourly_max_market_price == 0.85
        assert no_cfg.is_hourly_ticker("KXBTCD-26JUN23H14-T119999.99") is True

    def test_category_base_rate_kxbtcd(self) -> None:
        from gimmes.config import CATEGORY_BASE_RATES

        assert CATEGORY_BASE_RATES["KXBTCD"] == 0.70

    def test_hourly_series_entries_normalized(self) -> None:
        # A case/whitespace typo or a full market ticker would silently
        # never activate the hourly lane (#722 review) — entries are
        # normalized to bare uppercase series prefixes
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            scanner=ScannerConfig(hourly_series=[
                " kxbtcd ",
                "KXETHD-26JUN23H14-T2599.99",
                "",
                "  ",
            ]),
        )
        assert config.scanner.hourly_series == ["KXBTCD", "KXETHD"]
        assert config.is_hourly_ticker("KXBTCD-26JUN23H14-T119999.99") is True


class TestMonitorPlaybookSweepCadence:
    """#731: the sweep-cadence knob — bounded so the cap preserves
    #577's 48h staleness guarantee by construction."""

    def test_default(self) -> None:
        config = GimmesConfig(mode=Mode.DRIVING_RANGE)
        assert config.risk.monitor_playbook_sweep_hours == 6

    def test_zero_allowed_as_escape_hatch(self) -> None:
        config = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            risk=RiskConfig(monitor_playbook_sweep_hours=0),
        )
        assert config.risk.monitor_playbook_sweep_hours == 0

    def test_capped_at_48(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RiskConfig(monitor_playbook_sweep_hours=49)
