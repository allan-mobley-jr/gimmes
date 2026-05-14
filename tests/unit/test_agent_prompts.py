"""Static checks on agent prompt files (issue #523).

These guards prevent silent drift in the Caddie Master prompt that
would reintroduce subjective edge-based rejections untied to config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "agents"
CADDIE_MASTER = AGENTS_DIR / "caddie-master.md"
CADDIE = AGENTS_DIR / "caddie.md"
MONITOR = AGENTS_DIR / "monitor.md"


@pytest.fixture(scope="module")
def caddie_master_text() -> str:
    return CADDIE_MASTER.read_text()


@pytest.fixture(scope="module")
def caddie_text() -> str:
    return CADDIE.read_text()


@pytest.fixture(scope="module")
def monitor_text() -> str:
    return MONITOR.read_text()


def test_caddie_master_reads_cm_min_edge_after_fees(caddie_master_text: str) -> None:
    assert "gimmes config get strategy.cm_min_edge_after_fees" in caddie_master_text, (
        "Caddie Master must look up strategy.cm_min_edge_after_fees from config "
        "at the start of Step 4c review."
    )


def test_caddie_master_reads_strategy_side(caddie_master_text: str) -> None:
    assert "gimmes config get strategy.side" in caddie_master_text, (
        "Caddie Master Step 4c must read strategy.side from config "
        "to enforce the side constraint rule (#540)."
    )


def test_caddie_master_side_constraint_reject_rule(caddie_master_text: str) -> None:
    assert "Side constraint" in caddie_master_text, (
        "Step 4c REJECT criteria must include a 'Side constraint' rule "
        "that rejects candidates mismatched with strategy.side (#540)."
    )


def test_caddie_master_cites_cm_floor_in_reject_criteria(
    caddie_master_text: str,
) -> None:
    # Expect the config key to appear in the REJECT criteria list (not just the lookup).
    assert caddie_master_text.count("cm_min_edge_after_fees") >= 3, (
        "Expected cm_min_edge_after_fees to appear in config lookup, REJECT "
        "criteria, and the decision-note template."
    )


def test_caddie_master_forbids_subjective_descriptors(
    caddie_master_text: str,
) -> None:
    import re

    forbidden_phrases = ["thin edge", "knife-edge", "marginal edge", "coin flip"]
    for phrase in forbidden_phrases:
        assert phrase in caddie_master_text, (
            f"Step 4c must mention '{phrase}' as an example of a "
            f"forbidden subjective descriptor."
        )

    forbidden_clause = re.search(
        r"(?is)MUST\s+NOT\s+use.*subjective.*edge.*descriptor",
        caddie_master_text,
    )
    assert forbidden_clause is not None, (
        "Step 4c must contain a 'MUST NOT use subjective edge descriptors' "
        "clause (any reasonable wording) so rejections are auditable."
    )


# ---------------------------------------------------------------------------
# Caddie prompt guards (issue #535)
# ---------------------------------------------------------------------------


def test_caddie_cpi_extraordinary_event_keeps_arithmetic(
    caddie_text: str,
) -> None:
    import re

    assert re.search(
        r"(?is)extraordinary event.*inflation.*base-effect arithmetic",
        caddie_text,
    ), (
        "Caddie's sanity-check extraordinary event handler must instruct "
        "the agent to keep base-effect arithmetic for CPI/inflation markets "
        "instead of abandoning it."
    )


def test_caddie_threshold_arithmetic_primacy_rule(caddie_text: str) -> None:
    import re

    assert re.search(
        r"(?is)threshold.arithmetic primacy.*NEVER.*override.*threshold probability",
        caddie_text,
    ), (
        "Caddie's deep research framework must contain the "
        "'threshold-arithmetic primacy rule' preventing web forecasts "
        "from overriding mechanical threshold calculations."
    )


def test_caddie_point_estimate_not_fifty_percent(caddie_text: str) -> None:
    assert "does NOT mean P(" in caddie_text, (
        "Caddie must explicitly state that a consensus point forecast "
        "near a threshold does NOT mean ~50% probability of exceeding it."
    )


# ---------------------------------------------------------------------------
# Stop-loss override rule (#586)
# ---------------------------------------------------------------------------


def test_caddie_master_stop_loss_safety_valve_rule(
    caddie_master_text: str,
) -> None:
    # The new step 2c stop-loss rule must remain in the prompt — if it
    # drifts, the close-and-reopen anti-pattern reappears (see #586).
    assert "stop-loss is a safety valve" in caddie_master_text, (
        "Step 2c must contain the 'stop-loss is a safety valve' rule "
        "preventing automatic CLOSE on thesis-intact stop-loss breaches."
    )
    # The asymmetric rule shape MUST be present: thesis-degraded forces
    # CLOSE, thesis-intact + imminent settlement forces HOLD. Asserting
    # both halves prevents a future edit from deleting either branch
    # while keeping the preamble.
    assert "`Thesis: degraded`" in caddie_master_text and "CLOSE" in caddie_master_text, (
        "The stop-loss rule must map `Thesis: degraded` -> CLOSE."
    )
    assert "`Thesis: intact`" in caddie_master_text and "imminent" in caddie_master_text, (
        "The stop-loss rule must map `Thesis: intact` + imminent settlement"
        " -> HOLD."
    )
    # Missing-field fallback: ambiguous thesis must default to CLOSE.
    assert "Missing or malformed thesis fallback" in caddie_master_text, (
        "Must define behavior when Monitor's `Thesis:` line is absent or"
        " malformed (conservative default = CLOSE)."
    )
    # The no-immediate-reopen rule must have a concrete enforcement point
    # in Step 4c, not just float in step 2c. (#586 close-and-reopen).
    assert "Stop-loss reopen lockout" in caddie_master_text, (
        "Step 4c REJECT criteria must include a 'Stop-loss reopen lockout'"
        " rule so the prohibition has a concrete enforcement point."
    )


def test_monitor_stop_loss_flag_carries_thesis_and_time(
    monitor_text: str,
) -> None:
    # Caddie Master's stop-loss rule reads Monitor's `Thesis:` and
    # `TimeToResolution:` fields from the flag body. If Monitor stops
    # writing them, CM's rule silently degrades to the old behavior.
    import re

    # Match the stop-loss bullet block by regex anchored to bullet
    # boundaries — robust against future re-ordering of the bullets
    # around it. Captures up to the next top-level bullet `\n- **`.
    match = re.search(
        r"- \*\*Stop-loss breach\*\*:(.*?)(?=\n- \*\*|\Z)",
        monitor_text,
        flags=re.DOTALL,
    )
    assert match is not None, "monitor.md must contain a Stop-loss breach bullet."
    stop_loss_section = match.group(1)

    assert "Thesis: intact" in stop_loss_section, (
        "Stop-loss flag body must specify the `Thesis: intact|degraded`"
        " line so Caddie Master can apply the asymmetric rule (#586)."
    )
    assert "TimeToResolution:" in stop_loss_section, (
        "Stop-loss flag body must include `TimeToResolution:` so Caddie"
        " Master can identify the <24h imminent-settlement HOLD path"
        " without inferring it."
    )
