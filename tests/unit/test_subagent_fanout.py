"""Tests for gimmes.reporting.subagent_fanout (#571 Phase 1)."""

from __future__ import annotations

import json
from pathlib import Path

from gimmes.reporting.subagent_fanout import (
    CADDIE_MASTER,
    UNKNOWN,
    build_summary,
    parse_cycle_log,
    render_markdown,
)


def _write_cycle(path: Path, events: list[dict]) -> None:
    path.write_text(json.dumps(events))


def _assistant_event(usage: dict, content: list[dict] | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "content": content or [{"type": "text", "text": "..."}],
            "usage": usage,
        },
    }


def _agent_dispatch(tool_use_id: str, subagent_type: str | None) -> dict:
    return {
        "type": "tool_use",
        "id": tool_use_id,
        "name": "Agent",
        "input": {
            "subagent_type": subagent_type,
            "description": f"dispatch-{subagent_type}",
        },
    }


def _tool_result(tool_use_id: str) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"},
            ],
        },
    }


def _basic_usage(out_tok: int = 100) -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": out_tok,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


class TestParseCycleLog:
    def test_zero_dispatches_all_caddie_master(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-0001.json"
        _write_cycle(log, [
            _assistant_event(_basic_usage(50)),
            _assistant_event(_basic_usage(50)),
            {"type": "result", "total_cost_usd": 0.0015},
        ])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert cyc.cycle_id == 1
        assert cyc.dispatches == ()
        # Both turns should land in caddie_master.
        buckets = {b.bucket: b for b in cyc.buckets}
        assert set(buckets.keys()) == {CADDIE_MASTER}
        assert buckets[CADDIE_MASTER].turn_count == 2
        assert buckets[CADDIE_MASTER].output_tokens == 100

    def test_single_dispatch_attribution(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-0042.json"
        _write_cycle(log, [
            # caddie_master pre-dispatch turn
            _assistant_event(_basic_usage(10)),
            # dispatch envelope (still caddie_master)
            _assistant_event(_basic_usage(20), [
                _agent_dispatch("tu-1", "Monitor"),
            ]),
            # subagent's internal turns
            _assistant_event(_basic_usage(30)),
            _assistant_event(_basic_usage(40)),
            # tool_result returns control to caddie_master
            _tool_result("tu-1"),
            # caddie_master post-dispatch turn
            _assistant_event(_basic_usage(50)),
            {"type": "result", "total_cost_usd": 0.001},
        ])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert cyc.cycle_id == 42
        assert len(cyc.dispatches) == 1
        d = cyc.dispatches[0]
        assert d.subagent_type == "monitor"  # normalized lowercase
        assert d.tool_use_id == "tu-1"

        buckets = {b.bucket: b for b in cyc.buckets}
        # Caddie master: pre-dispatch (10) + envelope (20) + post-dispatch (50) = 80
        assert buckets[CADDIE_MASTER].output_tokens == 80
        assert buckets[CADDIE_MASTER].turn_count == 3
        # Monitor: the two internal turns 30 + 40 = 70
        assert buckets["monitor"].output_tokens == 70
        assert buckets["monitor"].turn_count == 2

    def test_subagent_type_case_normalized(self, tmp_path: Path) -> None:
        # Real logs include both "Monitor" and "monitor" (different code
        # paths). They must collapse into one bucket.
        log = tmp_path / "cycle-0050.json"
        _write_cycle(log, [
            _assistant_event(_basic_usage(0), [_agent_dispatch("tu-A", "Monitor")]),
            _assistant_event(_basic_usage(10)),
            _tool_result("tu-A"),
            _assistant_event(_basic_usage(0), [_agent_dispatch("tu-B", "monitor")]),
            _assistant_event(_basic_usage(20)),
            _tool_result("tu-B"),
            {"type": "result", "total_cost_usd": 0.0},
        ])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert len(cyc.dispatches) == 2
        assert all(d.subagent_type == "monitor" for d in cyc.dispatches)
        # Single bucket "monitor" with 30 output tokens combined.
        buckets = {b.bucket: b for b in cyc.buckets}
        assert buckets["monitor"].output_tokens == 30
        assert buckets["monitor"].turn_count == 2

    def test_unknown_subagent_type(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-0060.json"
        _write_cycle(log, [
            _assistant_event(_basic_usage(0), [_agent_dispatch("tu-X", None)]),
            _assistant_event(_basic_usage(15)),
            _tool_result("tu-X"),
            {"type": "result", "total_cost_usd": 0.0},
        ])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        d = cyc.dispatches[0]
        assert d.subagent_type == UNKNOWN
        buckets = {b.bucket: b for b in cyc.buckets}
        assert UNKNOWN in buckets
        assert buckets[UNKNOWN].output_tokens == 15

    def test_reconciliation_sum_equals_aggregate_usage(self, tmp_path: Path) -> None:
        # Sum of buckets' raw token totals must equal what
        # parse_usage_from_stream_json would return — the two are
        # different views of the same cycle and must reconcile exactly.
        log = tmp_path / "cycle-0070.json"
        usages = [
            {"input_tokens": 1, "output_tokens": 2,
             "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000},
            {"input_tokens": 3, "output_tokens": 4,
             "cache_creation_input_tokens": 200, "cache_read_input_tokens": 2000},
            {"input_tokens": 5, "output_tokens": 6,
             "cache_creation_input_tokens": 300, "cache_read_input_tokens": 3000},
        ]
        _write_cycle(log, [
            _assistant_event(usages[0]),
            _assistant_event(usages[1], [_agent_dispatch("tu-1", "Scout")]),
            _assistant_event(usages[2]),
            _tool_result("tu-1"),
            {"type": "result", "total_cost_usd": 0.0},
        ])
        cyc = parse_cycle_log(log)
        assert cyc is not None

        def total(field: str) -> int:
            return sum(getattr(b, field) for b in cyc.buckets)

        assert total("input_tokens") == 1 + 3 + 5
        assert total("output_tokens") == 2 + 4 + 6
        assert total("cache_creation_input_tokens") == 100 + 200 + 300
        assert total("cache_read_input_tokens") == 1000 + 2000 + 3000

    def test_malformed_log_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-0080.json"
        log.write_text("{not valid json")
        assert parse_cycle_log(log) is None

    def test_non_array_log_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-0081.json"
        log.write_text(json.dumps({"events": []}))
        assert parse_cycle_log(log) is None

    def test_missing_log_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-9999.json"
        # File never created.
        assert parse_cycle_log(log) is None

    def test_dispatch_still_in_flight_at_eof_warns(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-0090.json"
        _write_cycle(log, [
            _assistant_event(_basic_usage(0), [_agent_dispatch("tu-1", "Caddie")]),
            _assistant_event(_basic_usage(10)),
            # No tool_result for tu-1 — cycle truncated.
            {"type": "result", "total_cost_usd": 0.0},
        ])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert any("still in flight" in w for w in cyc.warnings)
        # Trailing turn still attributed to caddie even though result missing.
        buckets = {b.bucket: b for b in cyc.buckets}
        assert buckets["caddie"].output_tokens == 10

    def test_multiple_dispatches_same_envelope_uses_last(self, tmp_path: Path) -> None:
        # Real logs serialize dispatches one per envelope, but the walker
        # tolerates two-in-one: it records both, flips active to the
        # last, and emits a warning so the silent mis-attribution is
        # observable.
        log = tmp_path / "cycle-0100.json"
        _write_cycle(log, [
            _assistant_event(_basic_usage(0), [
                _agent_dispatch("tu-A", "Monitor"),
                _agent_dispatch("tu-B", "Scout"),
            ]),
            _assistant_event(_basic_usage(50)),
            _tool_result("tu-B"),
            {"type": "result", "total_cost_usd": 0.0},
        ])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert len(cyc.dispatches) == 2
        assert any("Agent blocks in one envelope" in w for w in cyc.warnings)
        # The 50-token assistant turn lands in the last subagent (Scout).
        buckets = {b.bucket: b for b in cyc.buckets}
        assert buckets["scout"].output_tokens == 50
        assert "monitor" not in buckets

    def test_new_dispatch_in_flight_warns(self, tmp_path: Path) -> None:
        # Dispatch B fires before A's tool_result returns. Single-pointer
        # model can't represent the overlap; the walker warns.
        log = tmp_path / "cycle-0110.json"
        _write_cycle(log, [
            _assistant_event(_basic_usage(0), [_agent_dispatch("tu-A", "Monitor")]),
            _assistant_event(_basic_usage(0), [_agent_dispatch("tu-B", "Scout")]),
            _tool_result("tu-A"),
            _tool_result("tu-B"),
            {"type": "result", "total_cost_usd": 0.0},
        ])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert any(
            "new dispatch while another in flight" in w
            for w in cyc.warnings
        )

    def test_unmatched_tool_result_for_known_dispatch_warns(
        self, tmp_path: Path,
    ) -> None:
        # tu-A is known but the active pointer is on tu-B (last dispatch).
        # When tu-A's tool_result arrives it doesn't match the active id;
        # the walker should warn rather than silently no-op so the
        # nested/interleaved case is observable.
        log = tmp_path / "cycle-0120.json"
        _write_cycle(log, [
            _assistant_event(_basic_usage(0), [
                _agent_dispatch("tu-A", "Monitor"),
                _agent_dispatch("tu-B", "Scout"),
            ]),
            _tool_result("tu-A"),
            _tool_result("tu-B"),
            {"type": "result", "total_cost_usd": 0.0},
        ])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert any(
            "nested/interleaved dispatch suspected" in w
            for w in cyc.warnings
        )

    def test_missing_model_falls_back_to_sonnet(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-0130.json"
        # No `model` field on any assistant event.
        ev = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "..."}],
                "usage": _basic_usage(10),
            },
        }
        _write_cycle(log, [ev, {"type": "result", "total_cost_usd": 0.0}])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert cyc.model == "claude-sonnet-4-6"

    def test_unknown_model_appends_warning(self, tmp_path: Path) -> None:
        log = tmp_path / "cycle-0140.json"
        ev = {
            "type": "assistant",
            "message": {
                "model": "claude-future-1-0",
                "content": [{"type": "text", "text": "..."}],
                "usage": _basic_usage(10),
            },
        }
        _write_cycle(log, [ev, {"type": "result", "total_cost_usd": 0.0}])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert any("unknown model" in w for w in cyc.warnings)

    def test_empty_array_returns_empty_cyclefanout(self, tmp_path: Path) -> None:
        # Truncated / killed-subprocess shape: valid JSON, empty array.
        log = tmp_path / "cycle-0150.json"
        log.write_text("[]")
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert cyc.dispatches == ()
        assert cyc.buckets == ()
        assert cyc.total_events == 0
        assert cyc.total_cost_usd_reported is None

    def test_result_missing_total_cost_keeps_reported_none(
        self, tmp_path: Path,
    ) -> None:
        log = tmp_path / "cycle-0160.json"
        _write_cycle(log, [
            _assistant_event(_basic_usage(10)),
            {"type": "result"},  # no total_cost_usd key
        ])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert cyc.total_cost_usd_reported is None

    def test_result_non_numeric_total_cost_keeps_reported_none(
        self, tmp_path: Path,
    ) -> None:
        log = tmp_path / "cycle-0161.json"
        _write_cycle(log, [
            _assistant_event(_basic_usage(10)),
            {"type": "result", "total_cost_usd": "0.001"},  # string, not number
        ])
        cyc = parse_cycle_log(log)
        assert cyc is not None
        assert cyc.total_cost_usd_reported is None


class TestBuildSummary:
    def test_aggregates_across_cycles(self, tmp_path: Path) -> None:
        c1 = tmp_path / "cycle-0001.json"
        c2 = tmp_path / "cycle-0002.json"
        _write_cycle(c1, [
            _assistant_event(_basic_usage(0), [_agent_dispatch("a", "Monitor")]),
            _assistant_event(_basic_usage(10)),
            _tool_result("a"),
            {"type": "result", "total_cost_usd": 0.001},
        ])
        _write_cycle(c2, [
            _assistant_event(_basic_usage(0), [_agent_dispatch("b", "Monitor")]),
            _assistant_event(_basic_usage(20)),
            _tool_result("b"),
            _assistant_event(_basic_usage(0), [_agent_dispatch("c", "Scout")]),
            _assistant_event(_basic_usage(5)),
            _tool_result("c"),
            {"type": "result", "total_cost_usd": 0.002},
        ])
        cycles = [parse_cycle_log(c1), parse_cycle_log(c2)]
        summary = build_summary([c for c in cycles if c is not None])
        assert summary.cycles_audited == 2
        # Reported cost preserved.
        assert summary.total_cost_usd_reported == 0.003
        # Monitor: 10 + 20 = 30; Scout: 5
        totals = {b.bucket: b for b in summary.totals_by_bucket}
        assert totals["monitor"].output_tokens == 30
        assert totals["scout"].output_tokens == 5
        # Dispatch counts
        assert summary.dispatch_counts_by_type == {"monitor": 2, "scout": 1}

    def test_highest_cost_bucket_excludes_caddie_master(self, tmp_path: Path) -> None:
        # Pin output_tokens-only usage so cost is monotone in tokens.
        c1 = tmp_path / "cycle-0001.json"
        # caddie_master accumulates a lot via top-level turns; monitor a
        # smaller amount. highest_cost_bucket must still return monitor
        # because caddie_master is excluded from the "subagent path" view.
        _write_cycle(c1, [
            _assistant_event(_basic_usage(1000)),
            _assistant_event(_basic_usage(0), [_agent_dispatch("a", "Monitor")]),
            _assistant_event(_basic_usage(50)),
            _tool_result("a"),
            {"type": "result", "total_cost_usd": 0.0},
        ])
        summary = build_summary([parse_cycle_log(c1)])
        highest = summary.highest_cost_bucket()
        assert highest is not None
        assert highest.bucket == "monitor"


class TestRenderMarkdown:
    def test_includes_all_three_tables(self, tmp_path: Path) -> None:
        c1 = tmp_path / "cycle-0001.json"
        _write_cycle(c1, [
            _assistant_event(_basic_usage(0), [_agent_dispatch("a", "Monitor")]),
            _assistant_event(_basic_usage(10)),
            _tool_result("a"),
            {"type": "result", "total_cost_usd": 0.001},
        ])
        summary = build_summary([parse_cycle_log(c1)])
        md = render_markdown(summary)
        assert "Table 1" in md
        assert "Table 2" in md
        assert "Table 3" in md
        assert "Highest-cost subagent path" in md
        assert "monitor" in md.lower()

    def test_render_markdown_deterministic(self, tmp_path: Path) -> None:
        # The deliverable file should be byte-identical for the same
        # inputs so two runs over the same cycles diff cleanly.
        c1 = tmp_path / "cycle-0001.json"
        _write_cycle(c1, [
            _assistant_event(_basic_usage(0), [_agent_dispatch("a", "Monitor")]),
            _assistant_event(_basic_usage(10)),
            _tool_result("a"),
            _assistant_event(_basic_usage(0), [_agent_dispatch("b", "Scout")]),
            _assistant_event(_basic_usage(20)),
            _tool_result("b"),
            {"type": "result", "total_cost_usd": 0.001},
        ])
        s1 = build_summary([parse_cycle_log(c1)])
        s2 = build_summary([parse_cycle_log(c1)])
        assert render_markdown(s1) == render_markdown(s2)

    def test_render_markdown_table2_column_order(self, tmp_path: Path) -> None:
        # Pin the Table 2 header to keep Phase 2's diff readable across
        # runs even if the ordering of the underlying dict changes.
        c1 = tmp_path / "cycle-0001.json"
        _write_cycle(c1, [
            _assistant_event(_basic_usage(0), [_agent_dispatch("a", "Monitor")]),
            _assistant_event(_basic_usage(10)),
            _tool_result("a"),
            {"type": "result", "total_cost_usd": 0.0},
        ])
        md = render_markdown(build_summary([parse_cycle_log(c1)]))
        expected_header = (
            "| bucket | turns | input_tok | output_tok |"
            " cache_creation_tok | cache_read_tok | cost_usd |"
            " % of total |"
        )
        assert expected_header in md

    def test_render_markdown_zero_cost_does_not_divide(self, tmp_path: Path) -> None:
        # Defensive: a cycle whose every assistant event has zero usage
        # produces total_cost==0; the percentage column must not blow up.
        c1 = tmp_path / "cycle-0001.json"
        _write_cycle(c1, [
            _assistant_event({k: 0 for k in (
                "input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens",
            )}),
            {"type": "result", "total_cost_usd": 0.0},
        ])
        md = render_markdown(build_summary([parse_cycle_log(c1)]))
        assert "0.0%" in md  # all rows should report 0.0%
        assert "Highest-cost" not in md  # no non-master buckets present


class TestBuildSummaryMixedModels:
    def test_mixed_model_cost_summed_per_cycle(self, tmp_path: Path) -> None:
        # Two cycles on different models. Build_summary must sum each
        # cycle's per-bucket cost rather than re-pricing aggregated tokens
        # at one model's rate (the previous bug). With Opus rates ~5×
        # Sonnet, an Opus cycle's bucket cost should be reflected in the
        # aggregate even when the first cycle is Sonnet.
        sonnet = tmp_path / "cycle-0001.json"
        opus = tmp_path / "cycle-0002.json"
        sonnet_event = {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "..."}],
                "usage": _basic_usage(1000),
            },
        }
        opus_event = {
            "type": "assistant",
            "message": {
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "..."}],
                "usage": _basic_usage(1000),
            },
        }
        _write_cycle(sonnet, [
            sonnet_event,
            {"type": "result", "total_cost_usd": 0.0},
        ])
        _write_cycle(opus, [
            opus_event,
            {"type": "result", "total_cost_usd": 0.0},
        ])
        cs = parse_cycle_log(sonnet)
        co = parse_cycle_log(opus)
        assert cs is not None and co is not None
        # Verify per-cycle bucket cost reflects each cycle's model.
        sonnet_cost = cs.cost_by_bucket()[CADDIE_MASTER]
        opus_cost = co.cost_by_bucket()[CADDIE_MASTER]
        assert opus_cost > sonnet_cost  # opus output rate is 5× sonnet's
        # Aggregate must equal the sum of per-cycle costs (not a re-price
        # at cycle 0's rate).
        summary = build_summary([cs, co])
        agg = {b.bucket: b.cost_usd for b in summary.totals_by_bucket}
        assert agg[CADDIE_MASTER] == sonnet_cost + opus_cost
