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
# Sanity-check gimme-category inclusion (#590)
# ---------------------------------------------------------------------------


def test_caddie_jobless_claims_in_gimme_category_list(caddie_text: str) -> None:
    # KXJOBLESSCLAIMS must remain in the sanity-check fast-track list —
    # removing it would re-introduce the live-vs-backtest market-mix
    # bias documented in #590 (live picker overweighted CPI losers
    # while JOBLESS, a backtest winner, fell through to deep research
    # and got under-approved).
    import re

    # The opening sentence of the Sanity-Check Mode section enumerates
    # the gimme categories in parentheses. Pin JOBLESS there.
    sanity_check_section = re.search(
        r"For candidates in backtested gimme categories \(([^)]+)\),",
        caddie_text,
    )
    assert sanity_check_section is not None, (
        "Caddie must have a 'For candidates in backtested gimme categories"
        " (...)' opening sentence in the Sanity-Check Mode section."
    )
    series_list = sanity_check_section.group(1)
    assert "KXJOBLESSCLAIMS" in series_list, (
        "KXJOBLESSCLAIMS must remain in the Sanity-Check gimme list (#590)."
        f" Current list: {series_list}"
    )


def test_caddie_jobless_claims_has_base_rate_matching_payrolls(
    caddie_text: str,
) -> None:
    # The base-rate table must include KXJOBLESSCLAIMS with the same
    # probability (0.85) as its peer employment series KXPAYROLLS/KXADP.
    # Pin the exact number — a silent drop to a different rate (e.g.
    # 0.50) would still produce "a numeric probability" and would not
    # be caught by a loose `0\.\d+` regex, but it would violate the AC
    # ("same treatment as KXPAYROLLS") and re-introduce the bias #590
    # was filed to fix.
    import re

    row_match = re.search(
        r"\|[^|]*KXJOBLESSCLAIMS[^|]*\|[^|]*\|[^|]*0\.85[^|]*\|",
        caddie_text,
    )
    assert row_match is not None, (
        "Caddie sanity-check base-rate table must include a row for"
        " KXJOBLESSCLAIMS with probability 0.85 (peer-matched with"
        " KXPAYROLLS/KXADP) so the fast-track path has a defined --prob"
        " consistent with the employment family (#590)."
    )


# ---------------------------------------------------------------------------
# Stop-loss override rule (#586)
# ---------------------------------------------------------------------------


def test_caddie_master_stop_loss_safety_valve_rule(
    caddie_master_text: str,
) -> None:
    # The new step 2c stop-loss rule must remain in the prompt — if it
    # drifts, the close-and-reopen anti-pattern reappears (see #586).
    # Scope assertions to the literal stop-loss subsection so they can't
    # pass vacuously by matching strings elsewhere in the prompt.
    import re

    sl_match = re.search(
        r"When reviewing a \*\*stop-loss flag\*\*:(.*?)Canonical anti-pattern to avoid:",
        caddie_master_text,
        flags=re.DOTALL,
    )
    assert sl_match is not None, (
        "Step 2c must contain a 'When reviewing a **stop-loss flag**:' block"
        " ending at the 'Canonical anti-pattern to avoid:' line."
    )
    sl_block = sl_match.group(1)

    assert "stop-loss is a safety valve" in sl_block, (
        "Step 2c stop-loss block must contain the 'safety valve' framing"
        " preventing automatic CLOSE on thesis-intact breaches."
    )
    # The asymmetric rule MUST encode both branches. Match the literal
    # mapping inside the block so a future edit that flips either branch
    # fails the test.
    assert re.search(r"`Thesis: degraded`[^.]*CLOSE", sl_block), (
        "Stop-loss block must map `Thesis: degraded` -> CLOSE in the same"
        " sentence."
    )
    assert re.search(r"`Thesis: intact`[^.]*imminent[^.]*HOLD", sl_block), (
        "Stop-loss block must map `Thesis: intact` + imminent settlement"
        " -> HOLD in the same sentence."
    )
    assert "Missing or malformed thesis fallback" in sl_block, (
        "Stop-loss block must define behavior when Monitor's `Thesis:`"
        " line is absent or malformed (conservative default = CLOSE)."
    )
    # The CLOSE-marker requirement so step 4c lockout can match. Without
    # this line, the lockout has no deterministic anchor.
    assert "Trigger: Stop-loss breach" in sl_block, (
        "Stop-loss block must require the literal `Trigger: Stop-loss"
        " breach` line in the decision note body so step 4c lockout"
        " has a deterministic match anchor."
    )
    # The no-immediate-reopen rule must have a concrete enforcement point
    # in Step 4c, not just float in step 2c. (#586 close-and-reopen).
    assert "Stop-loss reopen lockout" in caddie_master_text, (
        "Step 4c REJECT criteria must include a 'Stop-loss reopen lockout'"
        " rule so the prohibition has a concrete enforcement point."
    )


def test_caddie_master_4c_lockout_requires_both_markers(
    caddie_master_text: str,
) -> None:
    # Step 4c's REJECT criterion must require BOTH `Decision: CLOSE` AND
    # `Trigger: Stop-loss breach` in the same decision note. Without the
    # AND, a future edit could weaken the rule to match on `Decision:
    # CLOSE` alone — locking out tickers for any close, not just
    # stop-loss closes — or to match on `Trigger: Stop-loss breach`
    # alone — locking out tickers from a flag that didn't actually
    # result in a close. Both halves must appear in the lockout text.
    import re

    lockout_match = re.search(
        r"\*\*Stop-loss reopen lockout\*\*[^\n]+",
        caddie_master_text,
    )
    assert lockout_match is not None, (
        "Step 4c must contain a 'Stop-loss reopen lockout' REJECT bullet."
    )
    lockout_text = lockout_match.group(0)
    assert "Decision: CLOSE" in lockout_text, (
        "Step 4c lockout must reference `Decision: CLOSE` so a non-close"
        " note can't trigger the lockout."
    )
    assert "Trigger: Stop-loss breach" in lockout_text, (
        "Step 4c lockout must reference `Trigger: Stop-loss breach` so"
        " non-stop-loss closes (e.g. new-information CLOSEs) don't"
        " trigger the lockout."
    )


def test_monitor_template_body_carries_conditional_fields(
    monitor_text: str,
) -> None:
    # The "Writing Flags" template body MUST contain the new conditional
    # field lines (Thesis:, Price:, TimeToResolution:) — otherwise a
    # Monitor copy-pasting the template would silently regress to the
    # pre-#586 format while the trigger-description test still passes.
    import re

    template_match = re.search(
        r"\*\*Template\*\*[^`]+```bash(.*?)```",
        monitor_text,
        flags=re.DOTALL,
    )
    assert template_match is not None, (
        "Writing Flags section must contain a `bash` code block under"
        " the **Template** label."
    )
    template_body = template_match.group(1)
    for field in ("Thesis:", "Price:", "TimeToResolution:"):
        assert field in template_body, (
            f"Monitor flag template body must include `{field}` so the"
            f" copy-paste path doesn't silently drop the field (#586)."
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

    for field in ("Thesis:", "Price:", "TimeToResolution:"):
        assert field in stop_loss_section, (
            f"Stop-loss bullet must name `{field}` field by reference so"
            f" the requirement surfaces at the trigger description (the"
            f" formatting rules live in the Writing Flags field table)."
        )
    # The bullet must also pin the exact trigger-name spelling so step
    # 4c's literal `Trigger: Stop-loss breach` lockout match works.
    assert "exact spelling" in stop_loss_section, (
        "Stop-loss bullet must instruct Monitor to use the exact"
        " spelling `Stop-loss breach` for the Trigger value, else step"
        " 4c's literal lockout match can miss case/spacing variants."
    )
