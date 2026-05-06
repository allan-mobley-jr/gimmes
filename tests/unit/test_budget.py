"""Tests for gimmes.budget — daily Claude API budget guardrail (#545)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gimmes.budget import (
    DEFAULT_MAX_SESSIONS,
    DEFAULT_MAX_USD,
    PRICING,
    BudgetTracker,
    DaySummary,
    _warned_models,
    cost_from_usage,
    parse_usage_from_stream_json,
)


def _fixed_clock(when: datetime):
    return lambda: when


@pytest.fixture(autouse=True)
def _reset_warned_models():
    _warned_models.clear()
    yield
    _warned_models.clear()


# ---------------------------------------------------------------------------
# Pricing + cost helpers
# ---------------------------------------------------------------------------


class TestPricingTable:
    def test_known_models_present(self) -> None:
        assert "claude-sonnet-4-6" in PRICING
        assert "claude-opus-4-7" in PRICING
        assert "claude-haiku-4-5" in PRICING

    def test_each_pricing_entry_has_all_categories(self) -> None:
        required = {"input", "output", "cache_creation", "cache_read"}
        for model, rates in PRICING.items():
            assert required.issubset(rates.keys()), (
                f"{model} missing categories"
            )


class TestCostFromUsage:
    def test_sonnet_cost(self) -> None:
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 500_000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        # 1M * $3 + 0.5M * $15 = $3 + $7.5 = $10.5
        assert cost_from_usage(usage, "claude-sonnet-4-6") == pytest.approx(
            10.5, abs=1e-6,
        )

    def test_opus_cost_with_cache(self) -> None:
        usage = {
            "input_tokens": 100_000,
            "output_tokens": 50_000,
            "cache_creation_input_tokens": 200_000,
            "cache_read_input_tokens": 1_000_000,
        }
        # 0.1M*15 + 0.05M*75 + 0.2M*18.75 + 1M*1.5 = 1.5 + 3.75 + 3.75 + 1.5 = 10.5
        assert cost_from_usage(usage, "claude-opus-4-7") == pytest.approx(
            10.5, abs=1e-6,
        )

    def test_unknown_model_falls_back_to_sonnet(self, caplog) -> None:
        usage = {"input_tokens": 1_000_000, "output_tokens": 0}
        cost = cost_from_usage(usage, "claude-future-99")
        assert cost == pytest.approx(3.0, abs=1e-6)
        assert "claude-future-99" in _warned_models

    def test_unknown_model_warns_only_once(self, caplog) -> None:
        usage = {"input_tokens": 1_000_000}
        cost_from_usage(usage, "claude-future-99")
        cost_from_usage(usage, "claude-future-99")
        cost_from_usage(usage, "claude-future-99")
        # Set membership confirms only one entry across multiple calls.
        assert len([m for m in _warned_models if m == "claude-future-99"]) == 1

    def test_zero_usage_yields_zero_cost(self) -> None:
        assert cost_from_usage({}, "claude-sonnet-4-6") == 0.0


# ---------------------------------------------------------------------------
# Stream-JSON parser
# ---------------------------------------------------------------------------


class TestParseUsageFromStreamJson:
    def test_extracts_from_result_event(self) -> None:
        events = [
            b'{"type":"system","subtype":"init"}',
            b'{"type":"assistant","message":{"id":"x"}}',
            b'{"type":"result","is_error":false,"usage":{"input_tokens":1234,"output_tokens":99,"cache_creation_input_tokens":0,"cache_read_input_tokens":5000}}',
        ]
        usage = parse_usage_from_stream_json(b"\n".join(events))
        assert usage is not None
        assert usage["input_tokens"] == 1234
        assert usage["output_tokens"] == 99
        assert usage["cache_read_input_tokens"] == 5000

    def test_extracts_from_assistant_message_envelope(self) -> None:
        events = [
            b'{"type":"system","subtype":"init"}',
            b'{"type":"assistant","message":{"usage":{"input_tokens":42,"output_tokens":7}}}',
        ]
        usage = parse_usage_from_stream_json(b"\n".join(events))
        assert usage is not None
        assert usage["input_tokens"] == 42

    def test_returns_none_on_empty_stdout(self) -> None:
        assert parse_usage_from_stream_json(b"") is None

    def test_returns_none_on_only_malformed(self) -> None:
        assert parse_usage_from_stream_json(b"not json\n{also not json") is None

    def test_returns_none_on_invalid_utf8_bytes(self) -> None:
        """``json.loads`` raises ``UnicodeDecodeError`` on non-UTF-8 input —
        the parser must absorb that to keep its fail-open contract."""
        assert parse_usage_from_stream_json(b"\xff\xfe garbage\n") is None

    def test_returns_none_when_no_usage_in_any_event(self) -> None:
        events = [
            b'{"type":"system","subtype":"init"}',
            b'{"type":"result","is_error":false}',
        ]
        assert parse_usage_from_stream_json(b"\n".join(events)) is None

    def test_skips_malformed_lines_and_finds_later_usage(self) -> None:
        events = [
            b"corrupted line",
            b'{"type":"result","is_error":false,"usage":{"input_tokens":1,"output_tokens":2}}',
        ]
        usage = parse_usage_from_stream_json(b"\n".join(events))
        assert usage is not None
        assert usage["input_tokens"] == 1


# ---------------------------------------------------------------------------
# BudgetTracker — caps, persistence, rollover
# ---------------------------------------------------------------------------


class TestBudgetTracker:
    def test_under_cap_does_not_block(self, tmp_path: Path) -> None:
        tracker = BudgetTracker(path=tmp_path / "budget.json")
        blocked, reason = tracker.should_block()
        assert blocked is False
        assert reason is None

    def test_today_returns_zero_summary_when_no_state(
        self, tmp_path: Path,
    ) -> None:
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(path=tmp_path / "budget.json", clock=clock)
        s = tracker.today()
        assert s == DaySummary(date="2026-04-30")

    def test_record_cycle_persists_and_accumulates(
        self, tmp_path: Path,
    ) -> None:
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(path=tmp_path / "budget.json", clock=clock)
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        tracker.record_cycle(usage, "claude-sonnet-4-6")
        tracker.record_cycle(usage, "claude-sonnet-4-6")
        tracker.record_cycle(usage, "claude-sonnet-4-6")
        s = tracker.today()
        assert s.sessions == 3
        assert s.cost_usd == pytest.approx(9.0, abs=1e-6)
        assert s.input_tokens == 3_000_000

    def test_session_cap_blocks(self, tmp_path: Path) -> None:
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(
            path=tmp_path / "budget.json", max_sessions=2, clock=clock,
        )
        tracker.record_cycle({"input_tokens": 100}, "claude-sonnet-4-6")
        tracker.record_cycle({"input_tokens": 100}, "claude-sonnet-4-6")
        blocked, reason = tracker.should_block()
        assert blocked is True
        assert reason == "sessions"

    def test_cost_cap_blocks(self, tmp_path: Path) -> None:
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(
            path=tmp_path / "budget.json", max_cost_usd=5.0, clock=clock,
        )
        # 2M input tokens * $3/1M = $6 > $5 cap
        tracker.record_cycle(
            {"input_tokens": 2_000_000, "output_tokens": 0},
            "claude-sonnet-4-6",
        )
        blocked, reason = tracker.should_block()
        assert blocked is True
        assert reason == "cost"

    def test_zero_caps_are_treated_as_unlimited(self, tmp_path: Path) -> None:
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(
            path=tmp_path / "budget.json",
            max_sessions=0,
            max_cost_usd=0.0,
            clock=clock,
        )
        for _ in range(200):
            tracker.record_cycle(
                {"input_tokens": 10_000_000}, "claude-opus-4-7",
            )
        blocked, _ = tracker.should_block()
        assert blocked is False

    def test_midnight_rollover_resets_daily_totals(
        self, tmp_path: Path,
    ) -> None:
        # Pre-populate at cap on day 1.
        path = tmp_path / "budget.json"
        clock_d1 = _fixed_clock(datetime(2026, 4, 30, 23, 59, tzinfo=UTC))
        tracker_d1 = BudgetTracker(
            path=path, max_sessions=2, clock=clock_d1,
        )
        tracker_d1.record_cycle({"input_tokens": 1}, "claude-sonnet-4-6")
        tracker_d1.record_cycle({"input_tokens": 1}, "claude-sonnet-4-6")
        assert tracker_d1.should_block() == (True, "sessions")

        # Same file, advanced clock to next day.
        clock_d2 = _fixed_clock(datetime(2026, 5, 1, 0, 1, tzinfo=UTC))
        tracker_d2 = BudgetTracker(
            path=path, max_sessions=2, clock=clock_d2,
        )
        s = tracker_d2.today()
        assert s.date == "2026-05-01"
        assert s.sessions == 0
        blocked, reason = tracker_d2.should_block()
        assert blocked is False
        assert reason is None

    def test_secs_until_reset_at_22h(self, tmp_path: Path) -> None:
        clock = _fixed_clock(datetime(2026, 4, 30, 22, 0, tzinfo=UTC))
        tracker = BudgetTracker(path=tmp_path / "budget.json", clock=clock)
        # 22:00 → next midnight is 02:00 later = 7200 s.
        assert tracker.secs_until_reset() == 7200

    def test_corrupt_budget_json_archived_and_recovered(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "budget.json"
        path.write_text("{not valid json")
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(path=path, clock=clock)
        s = tracker.today()
        # Recovered to zeroed state.
        assert s.sessions == 0
        # Archive file present somewhere alongside.
        archives = list(tmp_path.glob("budget.corrupt.*"))
        assert len(archives) == 1

    def test_unknown_schema_version_treated_as_fresh(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "budget.json"
        path.write_text(json.dumps({"version": 999, "days": {"x": {}}}))
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(path=path, clock=clock)
        assert tracker.today().sessions == 0

    def test_pruning_drops_old_days(self, tmp_path: Path) -> None:
        path = tmp_path / "budget.json"
        # Pre-seed a 100-day-old entry.
        old_data = {
            "version": 1,
            "days": {
                "2026-01-01": {"sessions": 5, "cost_usd": 1.0},
                "2026-04-30": {"sessions": 3, "cost_usd": 0.5},
            },
        }
        path.write_text(json.dumps(old_data))
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(path=path, clock=clock)
        tracker.record_cycle({"input_tokens": 100}, "claude-sonnet-4-6")
        # Read back: old day pruned, today still present.
        data = json.loads(path.read_text())
        assert "2026-01-01" not in data["days"]
        assert "2026-04-30" in data["days"]

    def test_record_cycle_thread_safety(self, tmp_path: Path) -> None:
        """Concurrent ``record_cycle`` calls from threads in the same process
        must not lose updates — the in-process lock around the
        read-modify-write guarantees ``sessions == n_threads * per_thread``.
        """
        path = tmp_path / "budget.json"
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(path=path, clock=clock)
        n_threads = 4
        per_thread = 10

        def worker() -> None:
            for _ in range(per_thread):
                tracker.record_cycle(
                    {"input_tokens": 1000}, "claude-sonnet-4-6",
                )

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        s = tracker.today()
        assert s.sessions == n_threads * per_thread, (
            "In-process lock should prevent lost updates"
        )
        # File must be parseable.
        json.loads(path.read_text())

    def test_session_cap_blocks_when_jumped_past(
        self, tmp_path: Path,
    ) -> None:
        """If state already shows sessions > cap (e.g. cap reduced after
        prior records), ``should_block`` still returns True. Pins the ``>=``
        semantics."""
        path = tmp_path / "budget.json"
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        path.write_text(json.dumps({
            "version": 1,
            "days": {"2026-04-30": {"sessions": 81, "cost_usd": 0.0}},
        }))
        tracker = BudgetTracker(
            path=path, max_sessions=80, clock=clock,
        )
        blocked, reason = tracker.should_block()
        assert blocked is True
        assert reason == "sessions"

    def test_caps_in_effect_returns_persisted_caps(
        self, tmp_path: Path,
    ) -> None:
        """``caps_in_effect`` reads the caps the loop persisted to budget.json."""
        path = tmp_path / "budget.json"
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        # First tracker writes its caps via record_cycle.
        writer = BudgetTracker(
            path=path, max_sessions=200, max_cost_usd=99.0, clock=clock,
        )
        writer.record_cycle({"input_tokens": 1000}, "claude-sonnet-4-6")
        # Second tracker reads them back — its own constructor caps differ.
        reader = BudgetTracker(
            path=path, max_sessions=80, max_cost_usd=25.0, clock=clock,
        )
        caps = reader.caps_in_effect()
        assert caps == (200, 99.0)

    def test_caps_in_effect_falls_back_to_own_caps_before_first_record(
        self, tmp_path: Path,
    ) -> None:
        """Before any cycle is recorded, ``caps_in_effect`` returns the
        tracker's own configured caps."""
        path = tmp_path / "budget.json"
        tracker = BudgetTracker(
            path=path, max_sessions=42, max_cost_usd=7.5,
        )
        assert tracker.caps_in_effect() == (42, 7.5)

    def test_record_session_no_usage_increments_sessions_with_zero_cost(
        self, tmp_path: Path,
    ) -> None:
        """When parser returns no usage, the cycle still counts toward
        the session cap (Anthropic charged for it) at $0 attributed cost."""
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(path=tmp_path / "budget.json", clock=clock)
        tracker.record_session_no_usage()
        tracker.record_session_no_usage()
        s = tracker.today()
        assert s.sessions == 2
        assert s.cost_usd == 0.0
        assert s.input_tokens == 0
        assert s.output_tokens == 0

    def test_persist_caps_writes_caps_without_recording_session(
        self, tmp_path: Path,
    ) -> None:
        """``persist_caps()`` makes the caps visible to ``gimmes budget``
        before any cycle has run."""
        path = tmp_path / "budget.json"
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(
            path=path, max_sessions=99, max_cost_usd=42.5, clock=clock,
        )
        tracker.persist_caps()
        # File written, caps recorded, no session increment.
        data = json.loads(path.read_text())
        assert data["caps"]["max_sessions"] == 99
        assert data["caps"]["max_cost_usd"] == 42.5
        # No day entry created.
        assert "2026-04-30" not in data.get("days", {})

    def test_today_date_returns_iso_string(self, tmp_path: Path) -> None:
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(path=tmp_path / "budget.json", clock=clock)
        assert tracker.today_date() == "2026-04-30"


# ---------------------------------------------------------------------------
# Status formatters
# ---------------------------------------------------------------------------


class TestFormatters:
    def test_status_line_includes_caps_and_date(self, tmp_path: Path) -> None:
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(
            path=tmp_path / "budget.json",
            max_sessions=80,
            max_cost_usd=25.0,
            clock=clock,
        )
        line = tracker.format_status_line()
        assert "2026-04-30" in line
        assert "0/80" in line
        assert "$25.00" in line

    def test_status_line_renders_unlimited_caps(self, tmp_path: Path) -> None:
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(
            path=tmp_path / "budget.json",
            max_sessions=0,
            max_cost_usd=0.0,
            clock=clock,
        )
        line = tracker.format_status_line()
        assert "∞" in line

    def test_alert_message_session_reason(self, tmp_path: Path) -> None:
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(
            path=tmp_path / "budget.json",
            max_sessions=2,
            clock=clock,
        )
        tracker.record_cycle({"input_tokens": 1}, "claude-sonnet-4-6")
        tracker.record_cycle({"input_tokens": 1}, "claude-sonnet-4-6")
        msg = tracker.alert_message("sessions")
        assert "session cap" in msg
        assert "2/2" in msg

    def test_alert_message_cost_reason(self, tmp_path: Path) -> None:
        clock = _fixed_clock(datetime(2026, 4, 30, 12, 0, tzinfo=UTC))
        tracker = BudgetTracker(
            path=tmp_path / "budget.json",
            max_cost_usd=5.0,
            clock=clock,
        )
        tracker.record_cycle(
            {"input_tokens": 2_000_000}, "claude-sonnet-4-6",
        )
        msg = tracker.alert_message("cost")
        assert "cost cap" in msg
        assert "$5.00" in msg


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_session_cap_is_80(self) -> None:
        assert DEFAULT_MAX_SESSIONS == 80

    def test_default_cost_cap_is_25_usd(self) -> None:
        assert DEFAULT_MAX_USD == 25.0

    def test_tracker_uses_defaults_when_unspecified(
        self, tmp_path: Path,
    ) -> None:
        tracker = BudgetTracker(path=tmp_path / "budget.json")
        assert tracker.max_sessions == DEFAULT_MAX_SESSIONS
        assert tracker.max_cost_usd == DEFAULT_MAX_USD
