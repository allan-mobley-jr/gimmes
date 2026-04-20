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


@pytest.fixture(scope="module")
def caddie_master_text() -> str:
    return CADDIE_MASTER.read_text()


@pytest.fixture(scope="module")
def caddie_text() -> str:
    return CADDIE.read_text()


def test_caddie_master_reads_cm_min_edge_after_fees(caddie_master_text: str) -> None:
    assert "gimmes config get strategy.cm_min_edge_after_fees" in caddie_master_text, (
        "Caddie Master must look up strategy.cm_min_edge_after_fees from config "
        "at the start of Step 4c review."
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
        r"(?is)threshold.arithmetic primacy.*NEVER to override the threshold probability",
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
