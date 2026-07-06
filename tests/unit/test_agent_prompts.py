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
GROUNDSKEEPER = AGENTS_DIR / "groundskeeper.md"


@pytest.fixture(scope="module")
def caddie_master_text() -> str:
    return CADDIE_MASTER.read_text()


@pytest.fixture(scope="module")
def caddie_text() -> str:
    return CADDIE.read_text()


@pytest.fixture(scope="module")
def monitor_text() -> str:
    return MONITOR.read_text()


@pytest.fixture(scope="module")
def groundskeeper_text() -> str:
    return GROUNDSKEEPER.read_text()


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


def test_caddie_sibling_strike_selection_rule(caddie_text: str) -> None:
    # Within an event, Caddie must PASS higher-priced sibling strikes
    # when a cheaper sibling shares the same gimme-category base rate.
    # Without this rule, the per-strike scoring picks higher-priced
    # (worse Kelly) strikes — the canonical case from #591.
    #
    # Scope assertions to the Sanity-Check Mode block so the rule can't
    # silently drift into an unrelated section of the prompt while
    # keeping the keyword in the doc.
    import re

    sanity_check_match = re.search(
        r"## Sanity-Check Mode.*?(?=\n## )",
        caddie_text,
        flags=re.DOTALL,
    )
    assert sanity_check_match is not None, (
        "caddie.md must contain a '## Sanity-Check Mode' section."
    )
    sanity_block = sanity_check_match.group(0)

    assert "Sibling-strike selection" in sanity_block, (
        "Sanity-Check Mode must contain the per-event sibling-strike"
        " rule (#591)."
    )
    # The rule must pin the cheapest-on-the-trading-side as the
    # PROCEED winner. Accept either explicit "LOWEST" or "lowest"
    # paired with a price-on-trading-side reference — the load-bearing
    # claim is that the rule selects on PRICE, not score or threshold.
    assert "LOWEST price" in sanity_block or "LOWEST NO price" in sanity_block, (
        "Sibling-strike rule must pin 'LOWEST price on trading_side' (or"
        " 'LOWEST NO price' if NO-side hardcoded) — a softening to 'lower'"
        " or 'preferred' would reintroduce the bias #591 fixes."
    )
    assert "same event_ticker" in sanity_block or "SAME event_ticker" in sanity_block, (
        "Sibling-strike rule must scope to same event_ticker so it"
        " doesn't accidentally compare strikes across different events."
    )
    assert "KXADP-26APR" in sanity_block, (
        "Sibling-strike rule must cite the canonical KXADP-26APR"
        " T100000-vs-T125000 anti-pattern so a future drift can't"
        " quietly remove the evidence."
    )
    # Three load-bearing sub-rules from the diff must remain pinned:
    assert "PASS rationale MUST cite the dominant sibling" in sanity_block, (
        "Sibling-strike rule must require PASS rationale to cite the"
        " dominant sibling — auditability anchor."
    )
    assert "extraordinary-event exception" in sanity_block, (
        "Sibling-strike rule must defer to the CPI extraordinary-event"
        " carveout — if that exception fires for any sibling, each"
        " sibling needs Caddie Master review individually."
    )
    assert "monotonicity" in sanity_block.lower(), (
        "Sibling-strike rule must address sibling-price monotonicity —"
        " a looser-strike priced below tighter is the gimme signal, not"
        " a reason to collapse to the cheapest."
    )
    assert "trading_side" in sanity_block, (
        "Sibling-strike rule must reference `trading_side` (or strategy"
        " side) so it works on YES, NO, and 'both' configurations."
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


# ---------------------------------------------------------------------------
# Caddie Master: Cited sources field (#617 — closes the gap that defangs
# Monitor's read-back assertion from #577)
# ---------------------------------------------------------------------------


def test_caddie_master_decision_templates_all_require_cited_sources(
    caddie_master_text: str,
) -> None:
    """Each of the 4 decision-note templates (HOLD/CLOSE, SIZE UP,
    APPROVE, REJECT) MUST end with a `Cited sources:` line inside its
    own heredoc body. Scoped per-template rather than substring-counted
    so a future field-rename can't satisfy the count spuriously (#617)."""
    import re

    # Each decision-note template lives inside a `<<'GIMMES_EOF' ...
    # GIMMES_EOF` block whose body contains `Decision:`. Require the
    # opening delimiter to be followed by a newline (i.e. a REAL heredoc
    # opener) so inline backtick mentions of the delimiter literal in
    # explanatory prose don't false-match (`<<'GIMMES_EOF'\n` is a real
    # opener; `<<'GIMMES_EOF'` inside backticks is prose).
    heredoc_pattern = re.compile(
        r"<<'GIMMES_EOF'\n(.*?)\n\s*GIMMES_EOF\b",
        flags=re.DOTALL,
    )
    decision_heredocs = [
        m.group(1) for m in heredoc_pattern.finditer(caddie_master_text)
        if "Decision:" in m.group(1)
    ]
    assert len(decision_heredocs) >= 4, (
        f"Expected >= 4 decision-note heredoc blocks in caddie-master.md"
        f" (HOLD/CLOSE, SIZE UP, APPROVE, REJECT); found"
        f" {len(decision_heredocs)}. A template-block dropout would"
        " silently bypass the cited-sources contract (#617)."
    )
    missing = [
        i for i, body in enumerate(decision_heredocs)
        if "Cited sources:" not in body
    ]
    assert not missing, (
        f"Decision-note heredoc blocks at index {missing} are missing"
        " the `Cited sources:` field. Every decision template MUST"
        " end with the field — Monitor's read-back assertion (#577)"
        " is defanged otherwise (#617)."
    )
    # Order check: `Cited sources:` MUST appear AFTER `Decision:` in
    # each body — i.e., it's positioned as the closing field, not
    # interleaved with the leading fields. Catches drift where the
    # field is moved earlier and other fields drop below it.
    out_of_order = [
        i for i, body in enumerate(decision_heredocs)
        if body.find("Cited sources:") <= body.find("Decision:")
    ]
    assert not out_of_order, (
        f"Decision-note heredoc blocks at index {out_of_order} have"
        " `Cited sources:` appearing BEFORE the `Decision:` line."
        " The field must be the closing audit footer, not"
        " interleaved (#617)."
    )


def test_caddie_master_edge_pre_filter_reject_path_uses_form_b(
    caddie_master_text: str,
) -> None:
    """Step 4c's edge-pre-filter REJECT branch skips Caddie conferral but
    DOES read `gimmes candidates` and `gimmes market-info`. The rule must
    explicitly explain how to populate Cited sources in this branch —
    without that guidance, agents either default to Form B silently
    (losing valid citations) or fabricate sources (#617)."""
    assert "Step 4c edge-pre-filter REJECT path" in caddie_master_text, (
        "Cited-sources rule MUST explicitly call out the Step 4c"
        " edge-pre-filter REJECT branch — it's a third context (along"
        " with Step 2 and Step 4c regular APPROVE/REJECT) and skipping"
        " it leaves the immediate-reject branch with no citation"
        " guidance (#617)."
    )
    # The carve-out must mention BOTH candidates output and market-info
    # are still available, even though Caddie conferral memo isn't.
    assert "gimmes candidates" in caddie_master_text, (
        "Edge-pre-filter REJECT rule must reference `gimmes candidates`"
        " as a still-available source of citations in this branch."
    )
    assert "gimmes market-info" in caddie_master_text, (
        "Edge-pre-filter REJECT rule must reference `gimmes market-info`"
        " as a still-available source of citations in this branch."
    )


def test_caddie_master_cited_sources_allows_none_carveout(
    caddie_master_text: str,
) -> None:
    """When a decision turns purely on price + thesis (no named-source
    input), Form B 'None — decision based on price + thesis only' is
    the allowed empty case. The em-dash is U+2014, not a hyphen-minus
    — pin the exact byte (#617)."""
    assert "None — decision based on price + thesis only" in caddie_master_text, (
        "Cited sources rule must include the literal 'None — decision"
        " based on price + thesis only' carve-out (Form B). Em-dash"
        " is U+2014, not a hyphen-minus — this exact byte sequence is"
        " what the drift-guard pins."
    )


def test_caddie_master_cited_sources_format_matches_monitor_surfacing(
    caddie_master_text: str,
) -> None:
    """The example bullet format must match Monitor's playbook surfacing
    format (#577) so a single regex can parse citations from both CM
    decisions and Monitor observations (#617)."""
    # Monitor's exemplar (monitor.md:78-79):
    # `Barclays April headline CPI MoM +0.55% (FXStreet, 2026-05-08)`
    # CM's exemplar must follow the same shape — pin the bracketed
    # `(publisher, YYYY-MM-DD)` portion specifically since that's the
    # parser-relevant structure.
    assert "(FXStreet, 2026-05-08)" in caddie_master_text, (
        "Cited sources rule must show an example bullet using the"
        " same `(publisher, YYYY-MM-DD)` format as Monitor's"
        " surfacing rule. Mismatched formats break read-back"
        " parseability (#617)."
    )
    assert "Barclays April headline CPI MoM +0.55%" in caddie_master_text, (
        "Cited sources example must reproduce Monitor's exemplar"
        " verbatim so the two prompts can't drift on format (#617)."
    )


def test_caddie_master_forbids_uncited_source_fabrication(
    caddie_master_text: str,
) -> None:
    """The derivation rule prevents agents from satisfying the Cited
    sources field by fabricating citations. A source is only allowed
    if it appears in the input CM actually consulted this cycle (#617)."""
    derivation_marker = (
        "Derivation rule (REQUIRED — guards against fabricated citations)"
    )
    assert derivation_marker in caddie_master_text, (
        "Cited sources section MUST include a derivation rule that"
        " forbids citing sources not present in the input consulted"
        " this cycle. Without it, an agent can satisfy the field with"
        " plausible-looking fabrications (#617)."
    )
    assert "MUST appear in Monitor's flag body" in caddie_master_text, (
        "Derivation rule MUST anchor Step 2 citations on Monitor's"
        " flag body — that's where the bank/aggregator forecasts CM"
        " relied on are written (#617)."
    )
    assert (
        "MUST appear in Caddie's research memo" in caddie_master_text
        or "appear in Caddie's research memo" in caddie_master_text
    ), (
        "Derivation rule MUST anchor Step 4c citations on Caddie's"
        " research memo or market-info output — that's where the"
        " sources CM relied on for APPROVE/REJECT are written (#617)."
    )


def test_caddie_master_cited_sources_references_monitor_playbook(
    caddie_master_text: str,
    monitor_text: str,
) -> None:
    """Cross-file invariant: CM's cited-sources rule must reference
    Monitor's `Fundamental-Economic-Trigger Source Playbook` by name
    so future playbook additions (new bank, new aggregator) are
    naturally covered by CM's derivation rule (#617)."""
    assert "Fundamental-Economic-Trigger Source Playbook" in caddie_master_text, (
        "Caddie Master's cited-sources rule MUST reference Monitor's"
        " `Fundamental-Economic-Trigger Source Playbook` section by"
        " name. Without the cross-reference, a future addition to"
        " Monitor's bank list (e.g., HSBC) would require a separate"
        " CM edit — guaranteed drift (#617)."
    )
    # And the playbook section itself must still exist in monitor.md
    # (regression pin against accidentally deleting it from monitor).
    assert "## Fundamental-Economic-Trigger Source Playbook" in monitor_text, (
        "monitor.md MUST still have the playbook section that CM's"
        " cross-reference points at (#577)."
    )


def test_monitor_readback_vacuous_clause_still_present(
    monitor_text: str,
) -> None:
    """Regression pin: #617 must NOT inadvertently break Monitor's
    backward-compatibility clause for pre-existing decision notes that
    lack a Cited sources field. The 'vacuously satisfied' clause covers
    pre-#617 decisions during the migration window (#617)."""
    assert "vacuously satisfied" in monitor_text, (
        "monitor.md's read-back assertion must retain the 'vacuously"
        " satisfied' clause so pre-#617 decision notes (which lack"
        " Cited sources) don't trip the FORBIDDEN rule during the"
        " migration window (#577 + #617)."
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


# ---------------------------------------------------------------------------
# Monitor playbook audit footer (#615)
# ---------------------------------------------------------------------------


def test_monitor_observation_template_includes_playbook_audit_footer(
    monitor_text: str,
) -> None:
    """The observation template must include a `Playbook sources
    checked this cycle:` block so an operator auditing position-notes
    can distinguish 'Monitor ran the playbook' from 'Monitor skipped
    the playbook entirely' — the silent-failure path the 48h
    staleness rule was added to defend against (#615)."""
    import re

    obs_match = re.search(
        r"^## Writing Observations.*?(?=\n## )",
        monitor_text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert obs_match is not None, "Writing Observations section must exist"
    block = obs_match.group(0)
    assert "Playbook sources checked this cycle" in block, (
        "Observation template MUST include a `Playbook sources checked"
        " this cycle:` audit footer for fundamental-economic-trigger"
        " tickers (#615). Without it, operators can't tell whether"
        " Monitor actually ran the playbook this cycle."
    )
    assert "#615" in block, (
        "Audit footer must cite #615 inline so the rationale is"
        " preserved when the prompt is read in isolation."
    )


def test_monitor_audit_footer_enumerates_full_playbook_list(
    monitor_text: str,
) -> None:
    """The footer template must enumerate every named bank AND every
    aggregator from the playbook list — partial enumeration would
    let Monitor silently drop sources from cycle to cycle (#615)."""
    import re

    obs_match = re.search(
        r"^## Writing Observations.*?(?=\n## )",
        monitor_text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert obs_match is not None
    block = obs_match.group(0)

    # Locate just the footer template (between "Playbook sources
    # checked this cycle" header and the GIMMES_EOF terminator).
    footer_match = re.search(
        r"Playbook sources checked this cycle.*?GIMMES_EOF",
        block,
        flags=re.DOTALL,
    )
    assert footer_match is not None
    footer = footer_match.group(0)

    required = [
        "Goldman Sachs", "JPMorgan", "Morgan Stanley", "Bank of America",
        "Citi", "Barclays", "Wells Fargo", "Deutsche Bank", "UBS",
        "FXStreet", "MarketWatch", "Reuters", "Bloomberg",
    ]
    missing = [name for name in required if name not in footer]
    assert not missing, (
        f"Audit footer template missing playbook sources: {missing}."
        f" Every bank and aggregator in the playbook MUST be enumerated"
        f" in the footer template so partial enumeration can't drop"
        f" sources silently (#615)."
    )


def test_monitor_audit_footer_allows_no_result_and_inheritance(
    monitor_text: str,
) -> None:
    """Each source row MUST carry the explicit four-outcome grammar
    inline. Without per-row grammar, agents may fill the `[...]`
    placeholders inconsistently and the audit value erodes — Copilot's
    review of #615 caught this exact vacuous-coverage path (#615).
    The fourth outcome (SUPERSEDED) was added by #641."""
    import re

    obs_match = re.search(
        r"^## Writing Observations.*?(?=\n## )",
        monitor_text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert obs_match is not None
    block = obs_match.group(0)
    # Find the footer template specifically.
    footer_match = re.search(
        r"Playbook sources checked this cycle.*?GIMMES_EOF",
        block,
        flags=re.DOTALL,
    )
    assert footer_match is not None
    footer = footer_match.group(0)

    # Every enumerated source line MUST carry the full grammar.
    # Counting occurrences of the grammar string against the count
    # of source bullets ensures partial-row drift is caught.
    grammar = (
        "[value (publisher, YYYY-MM-DD) OR 'no result this cycle'"
        " OR 'inherited: <prior cite>'"
        " OR 'SUPERSEDED (pre-<event>, <date>) — refresh required']"
    )
    grammar_count = footer.count(grammar)
    assert grammar_count >= 13, (
        f"Footer template must repeat the full four-outcome grammar"
        f" on every source row (13 sources: 9 banks + 4 aggregators)."
        f" Found grammar on {grammar_count} rows. Bare `[...]`"
        f" placeholders let agents fill inconsistently and erode"
        f" audit value (#615, #641)."
    )


def test_monitor_audit_footer_omitted_for_non_economic_tickers(
    monitor_text: str,
) -> None:
    """The footer MUST be explicitly scoped to fundamental-economic-
    trigger tickers — equity indices (KXINX, KXNASDAQ100, KXSPX) have
    no bank-forecast vocabulary; synthesizing a footer for them would
    mislead audit (#615)."""
    import re

    obs_match = re.search(
        r"^## Writing Observations.*?(?=\n## )",
        monitor_text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert obs_match is not None
    block = obs_match.group(0)
    # The "Footer-omission rule" callout must exist and explicitly
    # name equity-index categories OR the broader exclusion.
    assert "Footer-omission rule" in block, (
        "Observation template must contain a `Footer-omission rule`"
        " callout that scopes the footer requirement to fundamental-"
        " economic-trigger tickers (#615)."
    )
    assert "OMIT" in block, (
        "Footer-omission rule must use the uppercase token `OMIT`"
        " (matching the existing prompt's emphasis convention) so"
        " agents reading the rule treat it as a hard instruction."
    )
    # The omission rule must be inline on the footer header itself
    # (not just in surrounding prose) so an agent copy-pasting the
    # template sees the rule immediately. Copilot's review of #615
    # flagged the unconditional-template / omission-rule mismatch.
    footer_header_match = re.search(
        r"Playbook sources checked this cycle[^\n]*",
        block,
    )
    assert footer_header_match is not None
    footer_header = footer_header_match.group(0)
    assert "OMIT" in footer_header, (
        "The `Playbook sources checked this cycle:` header line MUST"
        " contain an inline omission annotation (e.g.,"
        " `OMIT this entire block for non-playbook tickers`) so an"
        " agent copy-pasting the template sees the omission rule"
        " right there, not just in surrounding prose (#615)."
    )


# ---------------------------------------------------------------------------
# Threshold semantics + forecast supersession (#641)
# ---------------------------------------------------------------------------


def _writing_observations_block(monitor_text: str) -> str:
    """Extract monitor.md's `## Writing Observations` section (same
    anchoring convention as `_playbook_block` below)."""
    import re

    match = re.search(
        r"^## Writing Observations.*?(?=\n## )",
        monitor_text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert match is not None, "Writing Observations section must exist"
    return match.group(0)


def _observation_rule(monitor_text: str, name: str) -> str:
    """Extract the bold `**<name>...**` rule paragraph from the Writing
    Observations section, asserting the rule exists."""
    import re

    match = re.search(
        rf"\*\*{re.escape(name)}.*?(?=\n\n)",
        _writing_observations_block(monitor_text),
        flags=re.DOTALL,
    )
    assert match is not None, (
        f"Writing Observations must contain the `{name}` paragraph"
        f" (#641)."
    )
    return match.group(0)


def test_monitor_threshold_semantics_rule_pinned(monitor_text: str) -> None:
    """Monitor MUST ground YES/NO semantics in the settlement sentence
    (`Rules (primary)` row of market-info), never the title's
    directional wording — the KXCPI-26JUN-T-0.1 chain described a
    negative-threshold market backwards in every note (#641)."""
    import re

    rule = _observation_rule(monitor_text, "Threshold-semantics grounding")
    for needle in (
        "YES wins when",
        "NO wins when",
        "Rules (primary)",
        "KXCPI-26JUN-T-0.1",
        "#641",
    ):
        assert needle in rule, (
            f"Threshold-semantics grounding rule must contain"
            f" {needle!r} — the rule loses its teeth without the"
            f" verbatim-quote requirement and the canonical trap"
            f" exemplar (#641)."
        )
    assert re.search(r"negative threshold", rule, flags=re.IGNORECASE), (
        "The rule must call out negative thresholds (double negative)"
        " as the known trap (#641)."
    )


def test_monitor_observation_template_carries_semantics_line(
    monitor_text: str,
) -> None:
    """The observation heredoc must carry a `Semantics:` line for
    threshold markets with an inline OMIT annotation for non-threshold
    markets — same inline-annotation convention as the #615 footer
    header (#641)."""
    import re

    block = _writing_observations_block(monitor_text)
    sem_match = re.search(r"^Semantics: \[[^\n]*", block, flags=re.MULTILINE)
    assert sem_match is not None, (
        "Observation template must include a `Semantics:` line so"
        " threshold win conditions are stated in every observation"
        " (#641)."
    )
    sem_line = sem_match.group(0)
    for needle in ("YES wins", "NO wins", "OMIT", "#641"):
        assert needle in sem_line, (
            f"`Semantics:` template line must carry {needle!r} inline"
            f" — the annotation must live on the line itself so a"
            f" copy-pasting agent sees it (#641)."
        )


def test_monitor_footer_freshness_rule_pinned(monitor_text: str) -> None:
    """`fresh` means newly published, not re-found: re-discovering the
    same dated note must be written as inherited, and describing it as
    'freshly confirmed' is FORBIDDEN. This is the exact miscount that
    rode Jun 11-18 bank notes through Jul 1 cycles on
    KXCPI-26JUN-T-0.1 (#641)."""
    rule = _observation_rule(monitor_text, "Freshness rule")
    for needle in (
        "strictly newer",
        "FORBIDDEN",
        "freshly confirmed",
        "inherited: <prior cite>",
        "#641",
    ):
        assert needle in rule, (
            f"Freshness rule must contain {needle!r} — without the"
            f" strict-date requirement and the FORBIDDEN phrase, a"
            f" re-found stale note can still masquerade as fresh"
            f" (#641)."
        )


def test_monitor_footer_supersession_rule_pinned(monitor_text: str) -> None:
    """A forecast predating a regime-change event must be marked
    SUPERSEDED — not inherited — and cannot support HOLD continuation
    on its own (#641)."""
    rule = _observation_rule(monitor_text, "Supersession rule")
    for needle in (
        "regime-change",
        "SUPERSEDED (pre-<event>, <date>) — refresh required",
        "Hormuz",
        "HOLD",
        "#641",
        "MUST NOT revert to",
    ):
        assert needle in rule, (
            f"Supersession rule must contain {needle!r} — the exact"
            f" SUPERSEDED grammar (em-dash included), the exemplar,"
            f" the HOLD prohibition, and stickiness across cycles are"
            f" all load-bearing (#641)."
        )


def test_monitor_footer_spec_declares_four_outcomes(
    monitor_text: str,
) -> None:
    """The footer spec paragraph must agree with the row grammar: four
    outcomes, including superseded. Prose reverting to 'three
    outcomes' would contradict the 13 pinned rows (#641)."""
    assert "one of four outcomes" in monitor_text, (
        "Footer spec paragraph must declare `one of four outcomes`"
        " (#641 added SUPERSEDED as the fourth)."
    )
    assert "one of three outcomes" not in monitor_text, (
        "Stale `one of three outcomes` prose contradicts the 4-outcome"
        " row grammar (#641)."
    )
    assert "superseded" in monitor_text.lower(), (
        "Footer spec must mention the superseded outcome (#641)."
    )


def test_caddie_threshold_semantics_in_arithmetic_primacy(
    caddie_text: str,
) -> None:
    """Caddie must state YES/NO win conditions from `Rules (primary)`
    before deriving any probability (deep research), and in
    Sanity-Check Mode's settlement clarity check (fast-track) —
    covering both research paths (#641)."""
    import re

    # Scope to the grounding bullet itself so relocation out of the
    # arithmetic-primacy block fails loudly.
    grounding_match = re.search(
        r"\*\*Threshold-semantics grounding.*?(?=\n- |\n\n)",
        caddie_text,
        flags=re.DOTALL,
    )
    assert grounding_match is not None, (
        "Caddie's threshold-arithmetic primacy block must contain the"
        " `Threshold-semantics grounding` bullet (#641)."
    )
    grounding = grounding_match.group(0)
    for needle in (
        "YES wins when",
        "NO wins when",
        "Rules (primary)",
        "KXCPI-26JUN-T-0.1",
    ):
        assert needle in grounding, (
            f"Caddie semantics grounding bullet must contain"
            f" {needle!r} (#641)."
        )
    # Fast-track path: the settlement clarity check must also state
    # win conditions, since gimme-category candidates skip deep
    # research entirely.
    clarity_match = re.search(
        r"\*\*Settlement clarity check\*\*.*?(?=\n\n|\n3\.)",
        caddie_text,
        flags=re.DOTALL,
    )
    assert clarity_match is not None
    clarity = clarity_match.group(0)
    assert "Rules (primary)" in clarity and "#641" in clarity, (
        "Sanity-Check Mode's settlement clarity check must require"
        " stating YES/NO win conditions from `Rules (primary)` —"
        " fast-track candidates never reach the deep-research rule"
        " (#641)."
    )


def test_caddie_master_verifies_semantics_and_superseded(
    caddie_master_text: str,
) -> None:
    """Caddie Master is the last line of defense: it must verify
    Monitor's/Caddie's YES/NO descriptions against `Rules (primary)`
    at both decision points (Step 2c flag review, Step 4c candidate
    review), and must not renew a HOLD on SUPERSEDED sources (#641).
    Each duty is anchored to its own section so neither clause can be
    deleted and compensated for by a mention elsewhere."""
    import re

    # Step 2c: the flag-review sub-step itself must carry the check.
    flag_review_match = re.search(
        r"Review Monitor's flag note[^\n]*", caddie_master_text
    )
    assert flag_review_match is not None
    flag_review = flag_review_match.group(0)
    assert "Rules (primary)" in flag_review and "#641" in flag_review, (
        "Step 2c's `Review Monitor's flag note` line must require"
        " verifying Monitor's YES/NO description against"
        " `Rules (primary)` (#641)."
    )
    # Step 4c: the candidate-review verification must live between the
    # independent-research read and the Caddie conferral.
    research_idx = caddie_master_text.find("Read the research independently")
    confer_idx = caddie_master_text.find("Confer with Caddie using SendMessage")
    assert research_idx != -1 and confer_idx != -1
    candidate_review = caddie_master_text[research_idx:confer_idx]
    assert "Rules (primary)" in candidate_review, (
        "Step 4c must verify YES/NO win conditions against"
        " `Rules (primary)` before APPROVE/REJECT (#641)."
    )
    assert "directional description" in candidate_review, (
        "Step 4c must forbid accepting Caddie's directional"
        " description of the contract unverified (#641)."
    )
    # HOLD bullet: SUPERSEDED prohibition.
    assert "SUPERSEDED" in caddie_master_text, (
        "Caddie Master's HOLD rule must reference SUPERSEDED sources"
        " (#641)."
    )
    hold_idx = caddie_master_text.find("MUST NOT rest on sources marked")
    assert hold_idx != -1, (
        "HOLD bullet must contain the SUPERSEDED prohibition: a HOLD"
        " `MUST NOT rest on sources marked` SUPERSEDED (#641)."
    )
    assert caddie_master_text.count("#641") >= 3, (
        "Each #641 verification duty must cite the issue inline"
        " (2c review, HOLD bullet, 4c review)."
    )


def test_threshold_semantics_exemplar_shared_across_agents(
    monitor_text: str, caddie_text: str,
) -> None:
    """The canonical negative-threshold trap exemplar
    (KXCPI-26JUN-T-0.1) must be pinned in BOTH monitor.md and
    caddie.md so the two agents' semantics rules stay anchored to the
    same incident — same cross-file convention as the economic-
    category list sync test (#641)."""
    for text, name in ((monitor_text, "monitor.md"), (caddie_text, "caddie.md")):
        assert "KXCPI-26JUN-T-0.1" in text, (
            f"{name} must pin the KXCPI-26JUN-T-0.1 exemplar (#641)."
        )
        assert 'Will CPI rise more than -0.1%?' in text, (
            f"{name} must spell out the double-negative trap title"
            f" verbatim so the worked example survives prompt edits"
            f" (#641)."
        )


# ---------------------------------------------------------------------------
# Monitor fundamental-economic-trigger source playbook (#577)
# ---------------------------------------------------------------------------


def _playbook_block(monitor_text: str) -> str:
    import re

    # Anchor on line-start so inline cross-references (which contain the
    # literal `## Fundamental-Economic-Trigger Source Playbook` inside a
    # backtick span) don't false-match. The real section header is the
    # only line that starts with `## `.
    match = re.search(
        r"^## Fundamental-Economic-Trigger Source Playbook.*?(?=\n## )",
        monitor_text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert match is not None, (
        "Monitor MUST have a `## Fundamental-Economic-Trigger Source"
        " Playbook` top-level section between Trigger Conditions and"
        " Writing Observations (#577)."
    )
    return match.group(0)


def test_monitor_source_playbook_pins_named_banks(monitor_text: str) -> None:
    """The 9 named banks are the audit vocabulary for #577. They MUST appear
    verbatim in the playbook AND the playbook MUST require individual
    queries (not batched), which is the precise #577 root-cause fix."""
    block = _playbook_block(monitor_text)
    required_banks = [
        "Goldman Sachs",
        "JPMorgan",
        "Morgan Stanley",
        "Bank of America",
        "Citi",
        "Barclays",
        "Wells Fargo",
        "Deutsche Bank",
        "UBS",
    ]
    missing = [b for b in required_banks if b not in block]
    assert not missing, (
        f"Playbook missing named banks: {missing}. All 9 must appear so"
        " Monitor enumerates each individually rather than batching into"
        " a single 'Wall Street CPI' query (#577)."
    )
    # The batching prohibition IS the #577 fix — pin it explicitly so a
    # future edit that kept the names but deleted the instruction would
    # fail the test.
    assert "Search EACH of these banks individually" in block, (
        "Playbook MUST instruct Monitor to search EACH bank"
        " individually. Without this, an LLM batching all 9 banks into"
        " one search is what produced #577 c1391-c1405."
    )
    assert "Do NOT batch them" in block, (
        "Playbook MUST explicitly forbid batching the bank list into a"
        " single 'Wall Street CPI forecasts' query (#577)."
    )


def test_monitor_source_playbook_pins_aggregator_sources(
    monitor_text: str,
) -> None:
    """FXStreet was the aggregator Monitor missed in the c1391-c1405 window
    (#577). Pin the 4 aggregator sources verbatim AND the per-aggregator
    enumeration instruction."""
    block = _playbook_block(monitor_text)
    required_sources = ["FXStreet", "MarketWatch", "Reuters", "Bloomberg"]
    missing = [s for s in required_sources if s not in block]
    assert not missing, (
        f"Playbook missing aggregator sources: {missing}. FXStreet"
        " specifically was the missed source in #577."
    )
    assert "Query EACH of these aggregator sources by name" in block, (
        "Playbook MUST instruct Monitor to query EACH aggregator by"
        " name in search terms. A general 'check aggregators'"
        " instruction is what allowed FXStreet to be missed (#577)."
    )


def test_monitor_source_playbook_lists_economic_categories(
    monitor_text: str,
) -> None:
    """The playbook must list the Kalshi category prefixes that trigger
    bank/aggregator enumeration. Pin the minimal subset overlapping with
    Caddie's Sanity-Check Mode list (caddie.md:55)."""
    block = _playbook_block(monitor_text)
    required_categories = [
        "KXCPI",
        "KXCPICORE",
        "KXCPIYOY",
        "KXPAYROLLS",
        "KXJOBLESSCLAIMS",
        "KXADP",
        "KXGDP",
    ]
    missing = [c for c in required_categories if c not in block]
    assert not missing, (
        f"Playbook missing economic categories: {missing}. These overlap"
        " with caddie.md's Sanity-Check Mode list and MUST trigger"
        " Monitor's source-enumeration playbook (#577)."
    )


def test_monitor_48h_staleness_rule_pinned(monitor_text: str) -> None:
    """The 48-hour CM-decision staleness rule is the core defense against
    a stale baseline observation persisting across cycles (#577)."""
    import re

    dedup_match = re.search(
        r"\*\*Flag deduplication rules.*?(?=\n## )",
        monitor_text,
        flags=re.DOTALL,
    )
    assert dedup_match is not None, "Flag deduplication block must exist"
    block = dedup_match.group(0)
    assert "48 hours" in block, (
        "Dedup block must include the exact phrase `48 hours` so the"
        " staleness threshold is unambiguous (#577)."
    )
    assert "most recent CM `decision`-type note" in block, (
        "Staleness rule must anchor on the CM decision-note timestamp,"
        " not the prior observation timestamp (which Monitor controls"
        " and can refresh by writing a stale-template observation)"
        " (#577)."
    )
    assert "48h forces a re-search, NOT a flag" in block, (
        "The 48h rule must clarify that re-search is required but flag"
        " suppression still applies if the re-search confirms no change."
    )


def test_monitor_48h_does_not_bypass_no_material_change_rule(
    monitor_text: str,
) -> None:
    """The existing 'No material change → no flag' bullet must remain
    AFTER the 48h staleness bullet, so 48h forces a re-search but doesn't
    cause spurious flags when the re-search confirms no change (#577)."""
    idx_48h = monitor_text.find(
        "48-hour staleness re-search rule (REQUIRED — #577)",
    )
    idx_no_change = monitor_text.find(
        'If the delta observation says "No material change," do NOT write',
    )
    assert idx_48h != -1, "48h staleness bullet not found (#577)"
    assert idx_no_change != -1, "No-material-change bullet not found (#577)"
    assert idx_48h < idx_no_change, (
        "48h staleness bullet must come BEFORE the No-material-change"
        " bullet so the dedup ordering is: check staleness first, then"
        " skip flag if no material change. Inverting this ordering"
        " breaks the dedup contract."
    )


def test_monitor_read_back_assertion_in_observation_template(
    monitor_text: str,
) -> None:
    """The read-back assertion closes the c1407 regression where Monitor
    reverted to a stale template even after CM cited the missing data
    (#577)."""
    import re

    obs_match = re.search(
        r"## Writing Observations.*?(?=\n## )",
        monitor_text,
        flags=re.DOTALL,
    )
    assert obs_match is not None, "Writing Observations section must exist"
    block = obs_match.group(0)
    assert "Read-back assertion" in block, (
        "Writing Observations must open with a `Read-back assertion`"
        " block that requires Monitor to surface CM-cited sources in"
        " its observation (#577)."
    )
    assert "FORBIDDEN" in block, (
        "Read-back assertion must use the uppercase token `FORBIDDEN`"
        " to flag template assertions that contradict cited evidence."
    )
    assert "contradict cited evidence" in block, (
        "FORBIDDEN clause must reference `contradict cited evidence` so"
        " future prompts can't soften the contract via paraphrase."
    )
    assert "#577" in block, (
        "Read-back assertion must cite #577 inline so the rationale is"
        " preserved when the prompt is read in isolation."
    )
    # The (a) freshly searched OR (b) inherited enumeration is the
    # behaviorally specific piece of the rule. Without it, a watered-down
    # version of the read-back ("just check the CM decision") still
    # passes — but doesn't force surfaceable per-source evidence.
    assert "freshly search" in block.lower(), (
        "Read-back must require option (a): freshly searched this cycle"
        " for each CM-cited source (#577)."
    )
    assert "inherit" in block.lower() and "citation" in block.lower(), (
        "Read-back must require option (b): explicitly inherit the prior"
        " observation's finding with citation. Without (b) Monitor has"
        " no audit-friendly way to handle sources it can't re-search"
        " this cycle (#577)."
    )


def test_monitor_playbook_positioned_between_triggers_and_observations(
    monitor_text: str,
) -> None:
    """Playbook section MUST sit between `## What You Look For (Trigger
    Conditions)` and `## Writing Observations` — the read-back assertion
    references the playbook by name, so the playbook has to load first.
    Reordering would break the implicit forward-reference (#577).

    Anchored on `\\n## ` (line-start) so inline cross-references inside
    backtick spans (which contain the literal heading text) don't
    false-match before the real section header."""
    triggers_idx = monitor_text.find(
        "\n## What You Look For (Trigger Conditions)",
    )
    playbook_idx = monitor_text.find(
        "\n## Fundamental-Economic-Trigger Source Playbook",
    )
    observations_idx = monitor_text.find("\n## Writing Observations")
    assert triggers_idx != -1, "Trigger Conditions section must exist"
    assert playbook_idx != -1, "Playbook section must exist"
    assert observations_idx != -1, "Writing Observations section must exist"
    assert triggers_idx < playbook_idx < observations_idx, (
        "Playbook MUST be positioned between Trigger Conditions and"
        " Writing Observations sections. The read-back assertion"
        " forward-references the playbook by name (#577)."
    )


def test_monitor_playbook_pins_query_phrasing_variation(
    monitor_text: str,
) -> None:
    """The cache-mitigation rule (playbook §3) is one of three numbered
    MUSTs in the playbook. Without it, tool-level caching could re-create
    the c1391-c1405 stuck-result pattern (#577)."""
    block = _playbook_block(monitor_text)
    assert "Query-phrasing variation" in block, (
        "Playbook MUST contain a `Query-phrasing variation` rule against"
        " tool-level caching of identical search queries (#577)."
    )
    assert "Do NOT repeat" in block, (
        "Query-phrasing variation rule MUST explicitly forbid repeating"
        " the exact query string used in the prior observation (#577)."
    )


def test_monitor_playbook_cache_bust_dos_and_donts_pinned(
    monitor_text: str,
) -> None:
    """The empirically-validated cache-bust DOs and DON'Ts (#618) must
    appear in the playbook. The #618 investigation proved that
    appending a date suffix to an otherwise-identical query does NOT
    bypass the cache — the backend normalizes the date token away.
    Token-level rewording IS effective. The agent must be explicitly
    warned against ineffective cache-bust patterns so it doesn't
    waste cycles on strategies that look defensive but aren't."""
    block = _playbook_block(monitor_text)
    assert "Cache-bust DOs and DON'Ts" in block, (
        "Playbook §3 MUST contain a `Cache-bust DOs and DON'Ts` block"
        " documenting the empirically-validated guidance (#618)."
    )
    assert "#618" in block, (
        "Cache-bust block must cite #618 inline so the rationale is"
        " preserved when the prompt is read in isolation."
    )
    # The ineffective patterns MUST be explicitly forbidden — agents
    # would otherwise reach for them as obvious cache-busts.
    assert "DON'T" in block and "date suffix" in block, (
        "Cache-bust DON'Ts MUST explicitly call out `date suffix` as"
        " ineffective. The #618 investigation tested this directly:"
        " appending `2026-05-22` returned the IDENTICAL cached result"
        " set."
    )
    assert "random salt" in block, (
        "Cache-bust DON'Ts MUST forbid random-salt suffixes too. Same"
        " failure mode as the date-suffix attempt (#618)."
    )
    # The effective patterns MUST be explicitly endorsed.
    assert "DO" in block and "content tokens" in block, (
        "Cache-bust DOs MUST endorse content-token substitution"
        " (synonyms, alternate forms, descriptive terms) — the"
        " empirically-effective cache-bust method (#618)."
    )
    # Pin the synonym examples so a future edit can't drop them.
    assert "synonyms" in block, (
        "Cache-bust DOs MUST cite synonyms as the concrete mechanism"
        " agents should reach for. Without a worked example, the"
        " rule devolves into vague advice (#618)."
    )


def test_monitor_playbook_pins_surfacing_format(monitor_text: str) -> None:
    """When a bank/aggregator forecast is found, the observation MUST
    surface it with bank name, forecast value, source, and publication
    date — the four fields needed for CM audit. Vague surfacing is what
    let c1407 revert to a stale template (#577)."""
    block = _playbook_block(monitor_text)
    assert "bank name" in block, (
        "Surfacing rule MUST require the bank name field (#577)."
    )
    assert "forecast value" in block, (
        "Surfacing rule MUST require the forecast value field (#577)."
    )
    assert "source" in block and "publication date" in block, (
        "Surfacing rule MUST require source and publication date so CM"
        " can audit which aggregator surfaced the forecast and when"
        " (#577)."
    )


def test_monitor_playbook_pins_no_result_logging(monitor_text: str) -> None:
    """Silent omission (no log entry when a bank search returned empty)
    was the c1391-c1405 failure mode — Monitor never said 'Barclays:
    not found' so CM couldn't tell whether the search ran (#577)."""
    block = _playbook_block(monitor_text)
    assert "no" in block.lower() and "found this cycle" in block, (
        "Playbook MUST require Monitor to explicitly log when a bank"
        " returned no result in its search this cycle. Silent omission"
        " is what produced #577."
    )


def test_caddie_and_monitor_economic_category_lists_stay_in_sync(
    monitor_text: str,
    caddie_text: str,
) -> None:
    """Monitor's playbook must contain every fundamental-economic-trigger
    category from Caddie's Sanity-Check Mode list. Equity-index categories
    (KXINX S&P 500, KXNASDAQ100) are tracked by Caddie but legitimately
    out of scope for Monitor's bank-enumeration playbook — there are no
    "Goldman April S&P 500 forecast" sources to enumerate. The test pins
    the economic overlap and explicitly excludes index categories so
    drift in either direction (Caddie or Monitor) is caught (#577)."""
    import re

    block = _playbook_block(monitor_text)
    sanity_match = re.search(
        r"backtested gimme categories \(([^)]+)\)",
        caddie_text,
    )
    assert sanity_match is not None, (
        "caddie.md must contain the `backtested gimme categories"
        " (...)` line for sanity-check fast-track (this test depends"
        " on it)."
    )
    caddie_cats = [c.strip() for c in sanity_match.group(1).split(",")]
    # Equity indices use intraday price moves, not economist forecasts;
    # bank enumeration does not apply.
    non_economic = {"KXINX", "KXNASDAQ100"}
    economic_caddie_cats = [
        c for c in caddie_cats if c and c not in non_economic
    ]
    missing_from_monitor = [
        c for c in economic_caddie_cats if c not in block
    ]
    assert not missing_from_monitor, (
        f"Caddie's fundamental-economic sanity-check categories not in"
        f" Monitor's playbook: {missing_from_monitor}. Monitor MUST"
        " cover everything Caddie fast-tracks in the economic-trigger"
        " space (#577)."
    )


# ---------------------------------------------------------------------------
# Groundskeeper GitHub dedup pre-flight (#600)
# ---------------------------------------------------------------------------


def test_groundskeeper_has_github_dedup_preflight(
    groundskeeper_text: str,
) -> None:
    # Without the pre-flight, Groundskeeper re-files the same
    # (error_code, component) pattern every time new rows trip the
    # threshold — the #597 → #598 → #599 cascade on 2026-05-12.
    assert "Step 2.5" in groundskeeper_text, (
        "groundskeeper.md must contain a 'Step 2.5' GitHub dedup"
        " pre-flight section between Step 2 and Step 3 (#600)."
    )
    assert "gh issue list --state all --label bug --search" in groundskeeper_text, (
        "Step 2.5 must use `gh issue list --state all --label bug"
        " --search ...` to find both open and closed matching issues."
    )
    # JSON fields: must request createdAt (for OPEN selection rule),
    # url (for resolve-error sync), closedAt (for CLOSED branches).
    # Missing any of these makes a downstream branch unimplementable.
    for field in ("number", "title", "state", "createdAt", "closedAt", "url"):
        assert field in groundskeeper_text, (
            f"Step 2.5 --json must include `{field}` field — without"
            f" it, the agent can't apply a downstream branch correctly."
        )
    assert "(error_code, component)" in groundskeeper_text, (
        "Step 2.5 must name the dedup key as the tuple"
        " `(error_code, component)`, not error_code alone or category"
        " alone."
    )


def test_groundskeeper_dedup_branches_all_three_states(
    groundskeeper_text: str,
) -> None:
    # The Step 2.5 block must explicitly handle all three branches:
    # OPEN match → comment, CLOSED-recent → suppress, CLOSED-stale →
    # file new with citation. Scope assertions to the Step 2.5 block
    # so a future edit can't pass vacuously by mentioning these
    # keywords in an unrelated section.
    import re

    block_match = re.search(
        r"### Step 2\.5:.*?(?=\n### |\n## )",
        groundskeeper_text,
        flags=re.DOTALL,
    )
    assert block_match is not None, (
        "Step 2.5 section must exist as a level-3 heading between"
        " other level-3 headings or before the next level-2 heading."
    )
    block = block_match.group(0)

    assert "state: OPEN" in block and "gh issue comment" in block, (
        "OPEN-match branch must instruct Groundskeeper to comment on"
        " the existing issue rather than file a new one."
    )
    assert "state: CLOSED" in block and "within last 24h" in block, (
        "CLOSED-within-24h branch must suppress (allow fix to"
        " propagate)."
    )
    assert "older than 24h" in block and "cooldown" in block, (
        "CLOSED-older-than-24h branch must file new with cooldown"
        " framing so recurrence after fix is operationally"
        " distinguishable from initial discovery."
    )


def test_groundskeeper_forbids_dedup_on_error_code_alone(
    groundskeeper_text: str,
) -> None:
    # error_code alone over-suppresses unrelated components (e.g.,
    # `position_not_found` from cli.position-context vs from the
    # autonomous-loop order path are different bugs but share the
    # error_code). Tuple-keyed dedup is load-bearing.
    assert "NEVER dedup on `error_code` alone" in groundskeeper_text, (
        "Step 2.5 must explicitly forbid error_code-alone dedup."
    )


def test_groundskeeper_rules_pin_preflight_requirement(
    groundskeeper_text: str,
) -> None:
    # If the Rules section doesn't pin the requirement, a future edit
    # could quietly drop the Step 2.5 invocation from the Step 3 flow
    # while leaving the Step 2.5 description intact — silently
    # reverting #600. Scope to the Rules block so we catch removals
    # from there even if the phrase appears elsewhere in the prompt.
    import re

    rules_block = re.search(
        r"## Rules\s*\n(.*?)(?=\n## |\Z)",
        groundskeeper_text,
        flags=re.DOTALL,
    )
    assert rules_block is not None, (
        "groundskeeper.md must contain a `## Rules` section."
    )
    rules_body = rules_block.group(1)
    assert re.search(
        r"MUST run Step 2\.5.*before every `gh issue create`",
        rules_body,
    ), (
        "Rules section must contain a 'MUST run Step 2.5 ... before"
        " every gh issue create' line to enforce the pre-flight check."
    )


def test_groundskeeper_permits_commenting_on_existing_issues(
    groundskeeper_text: str,
) -> None:
    # The old rule "NEVER close or modify existing GitHub issues" was
    # too broad — it forbade the Step 2.5 commenting path. The new
    # rule must explicitly permit comments as the only sanctioned
    # modification. Scope to the Rules block.
    import re

    rules_block = re.search(
        r"## Rules\s*\n(.*?)(?=\n## |\Z)",
        groundskeeper_text,
        flags=re.DOTALL,
    )
    assert rules_block is not None, (
        "groundskeeper.md must contain a `## Rules` section."
    )
    rules_body = rules_block.group(1)
    assert "MAY add comments to existing open issues" in rules_body, (
        "Rules section must permit commenting on open issues as the"
        " only sanctioned modification (#600 Step 2.5 enforcement)."
    )


def test_groundskeeper_search_query_uses_quoted_terms(
    groundskeeper_text: str,
) -> None:
    # GitHub search tokenizes on `_`, `-`, `.` — unquoted error_codes
    # like `position_not_found` split into substring tokens and
    # false-match unrelated issues. The search MUST use quoted terms
    # restricted to `in:title` to suppress this fuzz.
    import re

    block = re.search(
        r"### Step 2\.5:.*?(?=\n### |\n## )",
        groundskeeper_text,
        flags=re.DOTALL,
    ).group(0)
    # ERROR_CODE lives in the title (Step 3 title template), COMPONENT
    # lives in the body. Both must be quoted so GitHub search doesn't
    # tokenize on `_`/`-`/`.`.
    assert '\'in:title "ERROR_CODE" in:body "COMPONENT"\'' in block, (
        "Step 2.5 search must use quoted terms with `in:title` for"
        " ERROR_CODE and `in:body` for COMPONENT — the title template"
        " puts the error_code in the title and the component in the"
        " body, so the search needs both scopes."
    )


def test_groundskeeper_selection_rule_prefers_open_over_closed(
    groundskeeper_text: str,
) -> None:
    # If a stale CLOSED match is newer than an OPEN match, the agent
    # must still pick the OPEN one (and comment), not the CLOSED one
    # (and re-file). Without this rule, a reopen-then-close history
    # silently breaks the dedup.
    import re

    block = re.search(
        r"### Step 2\.5:.*?(?=\n### |\n## )",
        groundskeeper_text,
        flags=re.DOTALL,
    ).group(0)
    assert "Match selection rule" in block, (
        "Step 2.5 must define a 'Match selection rule' for the"
        " multiple-match case."
    )
    assert (
        "ANY match has `state: OPEN`" in block
        and "take the most recently-created OPEN match" in block
    ), (
        "Selection rule must give OPEN matches priority over CLOSED"
        " regardless of which was more recently created/closed."
    )


def test_groundskeeper_open_match_resolves_before_commenting(
    groundskeeper_text: str,
) -> None:
    # If `gh issue comment` runs before `gimmes resolve-error` and the
    # resolve fails, the next cycle re-trips the threshold (because
    # the row is still unresolved locally), finds the same open
    # issue, and posts a duplicate comment. Idempotency requires
    # resolve-error FIRST, then comment only after success.
    import re

    block = re.search(
        r"### Step 2\.5:.*?(?=\n### |\n## )",
        groundskeeper_text,
        flags=re.DOTALL,
    ).group(0)
    assert "FIRST run `gimmes resolve-error" in block, (
        "OPEN-match branch must run `gimmes resolve-error` FIRST"
        " before posting the comment, so the local field is synced"
        " before the comment lands."
    )
    assert (
        "SKIP the comment for that row" in block
        or "never comment without successful resolve" in block
    ), (
        "OPEN-match branch must explicitly skip the comment if"
        " resolve-error fails — otherwise the next cycle re-comments"
        " indefinitely."
    )


def test_groundskeeper_closed_recency_cap_at_30_days(
    groundskeeper_text: str,
) -> None:
    # Without a recency cap, the "CLOSED older than 24h" branch fires
    # on months-old unrelated issues that happen to token-match. New
    # issue body would cite a stale unrelated issue as "previously
    # resolved" — misleading audit trail. Cap at 30 days.
    import re

    block = re.search(
        r"### Step 2\.5:.*?(?=\n### |\n## )",
        groundskeeper_text,
        flags=re.DOTALL,
    ).group(0)
    assert "older than 30 days" in block, (
        "Step 2.5 must define a stale-CLOSED branch (older than 30"
        " days) that treats the match as no-match — otherwise stale"
        " unrelated issues get cited as 'previously resolved'."
    )


def test_groundskeeper_critical_handling_preserves_safety_rule(
    groundskeeper_text: str,
) -> None:
    # Step 2's hard rule says critical + risk_breach MUST file in the
    # current cycle. The CLOSED-within-24h suppress branch would
    # violate that for recurring critical incidents. Step 2.5 must
    # explicitly handle critical/risk_breach: comment-on-existing-
    # OPEN to avoid duplicates, but always file when CLOSED (any
    # age) — never suppress.
    import re

    block = re.search(
        r"### Step 2\.5:.*?(?=\n### |\n## )",
        groundskeeper_text,
        flags=re.DOTALL,
    ).group(0)
    assert "CRITICAL handling" in block, (
        "Step 2.5 must contain a 'CRITICAL handling' block that"
        " preserves the file-in-current-cycle safety rule for"
        " critical/risk_breach."
    )
    assert "critical" in block.lower() and "risk_breach" in block, (
        "CRITICAL handling must name both `critical` severity and"
        " `risk_breach` category so the safety rule can't be evaded."
    )
    assert "never suppress a fresh critical/risk_breach" in block, (
        "CRITICAL handling must explicitly forbid suppressing fresh"
        " critical/risk_breach — closing a prior issue should not"
        " allow a 24h suppression window on the next critical."
    )


def test_groundskeeper_fail_open_behavior_pinned(
    groundskeeper_text: str,
) -> None:
    # The fail-open posture (file possible duplicate on `gh issue
    # list` failure rather than silently suppress) is load-bearing.
    # A future edit changing it to fail-closed would silently drop
    # escalations during GitHub outages.
    import re

    block = re.search(
        r"### Step 2\.5:.*?(?=\n### |\n## )",
        groundskeeper_text,
        flags=re.DOTALL,
    ).group(0)
    assert "fail-open" in block, (
        "Step 2.5 must explicitly name the fail-open posture so a"
        " future edit can't quietly flip to fail-closed."
    )
    assert (
        "proceed to Step 3" in block or "file a possible duplicate" in block.lower()
    ), (
        "Fail-open behavior must instruct the agent to proceed to"
        " Step 3 (file new) when `gh issue list` fails — silent"
        " suppression is worse than possible-duplicate."
    )


def test_groundskeeper_title_template_includes_error_code(
    groundskeeper_text: str,
) -> None:
    # Step 2.5's `in:title "ERROR_CODE"` search relies on the title
    # actually carrying the error code. Step 3's title template must
    # match the exact form `[SEVERITY] Error: ERROR_CODE — ...` so
    # the literal-substring search hits.
    import re

    # Pin the full title-template shape so a future edit that
    # restructures the title (e.g. "Error in COMPONENT: ERROR_CODE")
    # but keeps the ERROR_CODE substring somewhere doesn't pass
    # vacuously.
    assert re.search(
        r"\[SEVERITY\] Error: ERROR_CODE",
        groundskeeper_text,
    ), (
        "Step 3 issue-title template must use the exact form"
        " `[SEVERITY] Error: ERROR_CODE — ...` so Step 2.5's literal"
        " `in:title \"ERROR_CODE\"` search can match"
        " Groundskeeper-filed issues."
    )


def test_groundskeeper_critical_open_match_comments_not_files(
    groundskeeper_text: str,
) -> None:
    # Without this rule, a sustained risk_breach over multiple cycles
    # would re-file every cycle — recreating the #597 → #598 → #599
    # pattern scoped to criticals. Critical+OPEN must comment, not
    # file new.
    import re

    block = re.search(
        r"### Step 2\.5:.*?(?=\n### |\n## )",
        groundskeeper_text,
        flags=re.DOTALL,
    ).group(0)
    # Must explicitly cover the "critical + matching issue OPEN" case
    # and route it to the OPEN-match comment branch.
    assert "matching issue is OPEN" in block and "comment on it" in block, (
        "CRITICAL handling must route critical/risk_breach with an"
        " OPEN matching issue to the comment branch, not to file-new"
        " — otherwise sustained critical incidents spawn duplicates."
    )


def test_groundskeeper_comment_failure_handling_pinned(
    groundskeeper_text: str,
) -> None:
    # The "rows stay resolved + log phase=warn" comment-failure
    # handling prevents re-comment loops AND prevents silent loss of
    # the recurrence notification by surfacing it to operator audit.
    # Drop either half and the failure mode reappears.
    import re

    block = re.search(
        r"### Step 2\.5:.*?(?=\n### |\n## )",
        groundskeeper_text,
        flags=re.DOTALL,
    ).group(0)
    assert "If the comment fails after the resolves succeeded" in block, (
        "Step 2.5 must explicitly handle the case where comment fails"
        " after resolves succeeded — without it, the recurrence is"
        " silently dropped."
    )
    assert "phase=warn" in block, (
        "Comment-failure handling must log to activity_log with"
        " `phase=warn` so operators can audit unfiled recurrences."
    )
    assert "operator audit required" in block, (
        "Comment-failure handling must surface the audit requirement"
        " in the warn message itself."
    )


def test_no_agent_uses_inline_memo_body_rationale_for_prose() -> None:
    """Drift-guard for #589: agent prompts must use the `--*-file` variant
    for the three prose-bearing gimmes CLI args (log-candidate --memo,
    log-trade --rationale, position-note --body). Inline string variants
    are vulnerable to shell expansion of $0, $VAR, backticks — corrupts
    stored agent text at storage time.

    Each forbidden pattern requires the gimmes subcommand to appear in
    the same logical command before the inline arg — that way ``gh issue
    create --body "..."`` (which writes to GitHub, not the gimmes DB) and
    doc text that quotes the forbidden form as a warning don't trip the
    test. Multi-line bash commands with backslash continuation are
    collapsed to one line before searching.
    """
    import re

    # The `[^`\n]{0,400}?` window bounds the match within a single logical
    # bash command so a `gimmes log-candidate` in one code block can't
    # falsely pair with a `--memo "..."` later in the file.
    #
    # Each forbidden arg is matched by `--<arg>\b(?!-file)` — the negative
    # lookahead lets `--memo-file` / `--rationale-file` / `--body-file`
    # through while forbidding bare `--memo` / `--rationale` / `--body` in
    # any form: double-quoted (`--memo "x"`), single-quoted (`--memo 'x'`),
    # equals (`--memo=x`), or unquoted (`--memo x`). Single-quoted bash is
    # technically $-safe, but the file-input variant is the only sanctioned
    # path — anything else can regress under future quoting refactors.
    forbidden = [
        (
            re.compile(
                r"gimmes\s+log-candidate\b[^`\n]{0,400}?--memo\b(?!-file)",
            ),
            "log-candidate --memo (use --memo-file)",
        ),
        (
            re.compile(
                r"gimmes\s+log-trade\b[^`\n]{0,400}?--rationale\b(?!-file)",
            ),
            "log-trade --rationale (use --rationale-file)",
        ),
        (
            re.compile(
                r"gimmes\s+position-note\b[^`\n]{0,400}?--body\b(?!-file)",
            ),
            "position-note --body (use --body-file)",
        ),
    ]
    offenders: list[str] = []
    for agent_file in sorted(AGENTS_DIR.glob("*.md")):
        text = agent_file.read_text()
        collapsed = re.sub(r"\\\n\s*", " ", text)
        for pattern, label in forbidden:
            if pattern.search(collapsed):
                offenders.append(f"{agent_file.name}: {label}")
    assert not offenders, (
        "Inline prose CLI args are vulnerable to shell expansion of $N"
        " tokens (#589). Use the *-file variant via a single-quoted"
        " heredoc instead. Offending sites:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# #657: skip templates must not write degenerate analytics
# ---------------------------------------------------------------------------

_CLOSER = AGENTS_DIR / "closer.md"
_SCOUT = AGENTS_DIR / "scout.md"


def test_no_zeroed_skip_analytics_in_templates(
    caddie_master_text: str,
) -> None:
    """The degenerate batches (#657) came from templates passing
    explicit zeros. Skips now omit analytics flags — the CLI backfills
    from the candidates table."""
    import re

    zero_flag = re.compile(r"--(?:prob|price|score) 0(?:\s|\\|$)")
    closer_text = _CLOSER.read_text()
    for text, name in (
        (caddie_master_text, "caddie-master.md"),
        (closer_text, "closer.md"),
    ):
        hits = [
            line.strip() for line in text.splitlines()
            if zero_flag.search(line)
        ]
        assert not hits, (
            f"{name} passes explicit zero analytics to log-trade —"
            f" that produced edge = price - 100% skip batches (#657):"
            f" {hits}"
        )


def test_skip_templates_carry_structured_reason(
    caddie_master_text: str,
) -> None:
    """Every skip template names a --reason so proceed→no-trade
    conversions are queryable (#657)."""
    closer_text = _CLOSER.read_text()
    for fragment, text, name in (
        ("--action skip --reason cooldown", caddie_master_text,
         "caddie-master.md"),
        ("--action skip --reason research_failed", caddie_master_text,
         "caddie-master.md"),
        ("--action skip --reason review_reject", caddie_master_text,
         "caddie-master.md"),
        # #670: already_traded template (criterion 4) and the
        # infra_failed decision-note-failure instruction.
        ("--action skip --reason already_traded", caddie_master_text,
         "caddie-master.md"),
        ("--reason infra_failed", caddie_master_text,
         "caddie-master.md"),
        ("--action skip --reason validation_failed", closer_text,
         "closer.md"),
        ("--action skip --reason order_failed", closer_text,
         "closer.md"),
        ("--action skip --reason close_failed", closer_text,
         "closer.md"),
        ("--action skip --reason no_position", closer_text,
         "closer.md"),
    ):
        assert fragment in text, f"{name} lost the template: {fragment}"


def test_scout_and_caddie_skip_templates_keep_real_analytics(
    caddie_text: str,
) -> None:
    """Scout/Caddie skips happen at scan/research time when the agent
    HAS the real values — their templates must keep passing them
    (the CLI backfill is the fallback, not the primary path)."""
    scout_text = _SCOUT.read_text()
    for text, name in ((scout_text, "scout.md"), (caddie_text, "caddie.md")):
        assert "--price 0.XX --prob 0.XX --score NN" in text, (
            f"{name} skip template no longer passes real analytics"
        )


def test_caddie_untradeable_at_bound_rule(caddie_text: str) -> None:
    """#658: Caddie must recognize bound-priced markets as untradeable
    and log them with recommendation pass (record feeds cooldown)."""
    assert "Untradeable at the bound" in caddie_text
    assert "within one tick of a bound" in caddie_text
    assert "--recommendation pass" in caddie_text
    assert "sibling-strike" in caddie_text  # bound strikes don't dominate
    # #672: the numeric score must not undo the pass recommendation.
    assert "edge-size component 0" in caddie_text


# ---------------------------------------------------------------------------
# #659: hard loss backstop and HOLD-loop tightening
# ---------------------------------------------------------------------------


def test_caddie_master_hard_loss_backstop_rule(
    caddie_master_text: str,
) -> None:
    """At >= 200% of the stop gate the CLOSE is unconditional — every
    carve-out the audited failures stretched must be enumerated as
    non-applicable, and the lockout literal must apply."""
    assert "Hard loss backstop" in caddie_master_text
    start = caddie_master_text.index("Hard loss backstop")
    block = caddie_master_text[start:start + 1600]
    assert "200%" in block
    assert "MANDATORY-CLOSE" in block
    assert "unconditionally" in block
    for carve_out in (
        "thesis intact", "imminent settlement",
        "re-evaluation condition", "data release",
        "trigger type", "governance refresh",
    ):
        assert carve_out in block, f"missing non-applicable: {carve_out}"
    assert "Trigger: Stop-loss breach" in block


def test_caddie_master_loss_thesis_rule_not_scoped_to_flag_type(
    caddie_master_text: str,
) -> None:
    """The KXPAYROLLS-26JUN failure: degraded thesis + losing position
    must CLOSE whatever the flag's trigger type says."""
    assert "Loss-position thesis rule" in caddie_master_text
    start = caddie_master_text.index("Loss-position thesis rule")
    block = caddie_master_text[start:start + 1000]
    assert "POSITION STATE, not flag type" in block
    assert "thesis-INTACT positions only" in block
    assert "FORBIDDEN" in block
    assert "KXPAYROLLS-26JUN-T125000" in block


def test_caddie_master_scheduled_release_hold_rule(
    caddie_master_text: str,
) -> None:
    """Governance refreshes must decide hold-through vs exit-before a
    scheduled release (the KXCPIYOY-26MAY failure)."""
    assert "Scheduled-release HOLD rule" in caddie_master_text
    start = caddie_master_text.index("Scheduled-release HOLD rule")
    block = caddie_master_text[start:start + 1200]
    assert "hold-through-release" in block
    assert "exit-before-release" in block
    assert "FORBIDDEN as the sole re-evaluation condition" in block


def test_caddie_master_2a_backstop_sweep(caddie_master_text: str) -> None:
    """Losses gap between cycles (the KXCPIYOY-26JUN case) — the first
    positions call of the cycle must sweep the StopGate banners, and
    the sweep repeats after Monitor returns (mid-cycle gaps)."""
    assert "Hard-backstop sweep" in caddie_master_text
    start = caddie_master_text.index("Hard-backstop sweep")
    block = caddie_master_text[start:start + 700]
    assert "MANDATORY-CLOSE" in block
    assert "even if Monitor writes no flag" in block
    assert "TOP of step 2c" in block


def test_caddie_master_backstop_rereads_positions_at_review(
    caddie_master_text: str,
) -> None:
    """The 2a read can be stale by review time — the backstop must
    re-run positions and fire on EITHER source (#659 review)."""
    start = caddie_master_text.index("Hard loss backstop")
    block = caddie_master_text[start:start + 1700]
    assert "re-run `gimmes positions` NOW" in block
    assert "EITHER the fresh output OR Monitor" in block


def test_caddie_master_size_up_gate_dilution_rule(
    caddie_master_text: str,
) -> None:
    """Sizing up a losing position dilutes the Stop percentage and
    defers the backstop — forbidden at >= 100% (#659 review)."""
    assert "SIZE UP gate-dilution rule" in caddie_master_text
    start = caddie_master_text.index("SIZE UP gate-dilution rule")
    block = caddie_master_text[start:start + 700]
    assert "FORBIDDEN" in block
    assert "100% or more" in block
    assert "pre-add StopGate" in block


def test_monitor_dedup_exempts_mandatory_close(monitor_text: str) -> None:
    """The hard backstop outranks flag deduplication — a banner always
    re-flags (#659 review)."""
    assert "the hard loss backstop outranks flag deduplication" in monitor_text


def test_monitor_stopgate_field_on_losing_positions(
    monitor_text: str,
) -> None:
    assert "StopGate:" in monitor_text
    # Field table row: required for every trigger when losing, copied
    # verbatim from the CLI — never hand-computed.
    assert "EVERY trigger type" in monitor_text
    assert "never hand-computed" in monitor_text
    # Template carries the line with an omit rule for winners.
    assert "OMIT this line if the position is not losing" in monitor_text


def test_stop_column_literal_shared_across_code_and_prompts(
    caddie_master_text: str, monitor_text: str,
) -> None:
    """Drift guard: the MANDATORY-CLOSE literal rendered by the
    formatter must be the string both prompts key on (#659)."""
    from io import StringIO
    from unittest.mock import patch

    from rich.console import Console

    from gimmes.reporting import formatter

    # Behavioral, not getsource: render a breached position and check
    # the emitted banner (a docstring mention would fool getsource).
    buf = StringIO()
    with patch(
        "gimmes.reporting.formatter.console",
        Console(file=buf, width=80),
    ):
        formatter.format_positions([{
            "ticker": "KXTEST", "side": "no", "count": 10,
            "avg_price": 0.55, "market_price": 0.40,
            "unrealized_pnl": -32.10, "cost_basis": 100.0,
        }], stop_loss_pct=0.15)
    assert "MANDATORY-CLOSE" in buf.getvalue()
    assert "MANDATORY-CLOSE" in caddie_master_text
    assert "MANDATORY-CLOSE" in monitor_text


# ---------------------------------------------------------------------------
# #660: probability-flip guard
# ---------------------------------------------------------------------------


def test_caddie_flip_acknowledgment_rule(caddie_text: str) -> None:
    """An unexplained >50pp flip against the ticker's own prior
    scoring must be acknowledged in the memo (#660)."""
    assert "Flip acknowledgment (REQUIRED — #660)" in caddie_text
    start = caddie_text.index("Flip acknowledgment")
    block = caddie_text[start:start + 700]
    assert "[FLIP-WARNING]" in block
    assert "prior probability" in block
    assert "convention correction" in block


def test_caddie_master_flip_reject_criterion(
    caddie_master_text: str,
) -> None:
    """FLIP-WARN candidates are never approved on score alone (#660:
    four PROCEEDs at ~88 on the side later assessed at 2%)."""
    assert "Probability flip unresolved" in caddie_master_text
    start = caddie_master_text.index("Probability flip unresolved")
    block = caddie_master_text[start:start + 800]
    assert "FLIP-WARN" in block
    assert "REJECT or confer" in block
    assert "NEVER approve a FLIP-WARN candidate on score alone" in block


def test_flip_marker_literal_shared_across_code_and_prompts(
    caddie_text: str,
) -> None:
    """Drift guard: the marker the CLI prints is the string the
    caddie prompt keys on (#660)."""
    from gimmes.store.observation_validator import FLIP_WARNING_MARKER

    assert FLIP_WARNING_MARKER == "[FLIP-WARNING]"
    assert FLIP_WARNING_MARKER in caddie_text


# ---------------------------------------------------------------------------
# #661: reopen churn gate and stale-post-close research
# ---------------------------------------------------------------------------


def test_caddie_master_4a_stale_close_rule(caddie_master_text: str) -> None:
    assert "Prior research flagged STALE-CLOSE" in caddie_master_text
    start = caddie_master_text.index("Prior research flagged STALE-CLOSE")
    block = caddie_master_text[start:start + 600]
    assert "NO valid prior research" in block
    assert "#661" in block


def test_caddie_master_4c_stale_close_reject(
    caddie_master_text: str,
) -> None:
    assert "Post-close stale research" in caddie_master_text
    start = caddie_master_text.index("Post-close stale research")
    block = caddie_master_text[start:start + 900]
    assert "STALE-CLOSE" in block
    assert "postdates the close" in block
    # #586 stays the stricter rule
    assert "#586" in block


def test_closer_reopen_gate_rules() -> None:
    closer_text = _CLOSER.read_text()
    assert "Staleness gate (#661)" in closer_text
    assert "Reopen churn gate (#661)" in closer_text
    assert "NEVER pass `--force-reopen`" in closer_text


def test_586_lockout_untouched(caddie_master_text: str) -> None:
    """The generalized #661 rule must not have weakened the stricter
    stop-loss lockout literals."""
    assert "Trigger: Stop-loss breach" in caddie_master_text
    assert "Stop-loss reopen lockout" in caddie_master_text


def test_groundskeeper_churn_carveout() -> None:
    """#661: churn_roundtrip audit rows must not spam GitHub issues;
    forced-bypass rows still escalate."""
    gk_text = (AGENTS_DIR / "groundskeeper.md").read_text()
    assert "churn_roundtrip" in gk_text
    assert "do NOT file issues" in gk_text
    assert "reopen_gate_overridden" in gk_text


# ---------------------------------------------------------------------------
# #674: STALE / BASIS-SUSPECT StopGate values + DATA-ERROR interplay pins
# ---------------------------------------------------------------------------


def test_monitor_copies_all_stopgate_banner_values(
    monitor_text: str,
) -> None:
    """#674 (and the previously unpinned #659 side): Monitor's field
    table and dedup exception must enumerate every banner value the
    formatter can emit, and prefer MANDATORY-CLOSE when several
    banners exist."""
    assert "`MANDATORY-CLOSE`, `DATA-ERROR`, `STALE`, or `BASIS-SUSPECT`" in monitor_text
    assert "copy the `MANDATORY-CLOSE` one" in monitor_text
    # A stale WINNER must still surface — the OMIT clause is scoped.
    assert (
        "OMIT this line if the position is not losing AND no StopGate"
        " banner exists" in monitor_text
    )


def test_caddie_master_non_numeric_stopgate_rule(
    caddie_master_text: str,
) -> None:
    """#674 (pins the previously untested #659 interplay): a
    non-numeric StopGate trips the conservative CLOSE path, and the
    new values are named."""
    assert "NON-NUMERIC StopGate" in caddie_master_text
    assert "do NOT HOLD on unquantified risk" in caddie_master_text
    assert "`DATA-ERROR`" in caddie_master_text
    assert "`STALE`" in caddie_master_text
    assert "`BASIS-SUSPECT`" in caddie_master_text
    # The 2a sweep covers the new banners too.
    assert (
        "Sweep `StopGate: STALE`, `StopGate: BASIS-SUSPECT`, and"
        " `StopGate: DATA-ERROR`" in caddie_master_text
    )


def test_stale_literal_shared_across_code_and_prompts(
    caddie_master_text: str, monitor_text: str,
) -> None:
    """Drift guard (#674, mirrors the MANDATORY-CLOSE guard):
    behavioral render of a stale and a suspect position — the emitted
    literals must be the strings both prompts key on."""
    from io import StringIO
    from unittest.mock import patch

    from rich.console import Console

    from gimmes.reporting import formatter

    buf = StringIO()
    with patch(
        "gimmes.reporting.formatter.console",
        Console(file=buf, width=80),
    ):
        formatter.format_positions([{
            "ticker": "KXTEST", "side": "no", "count": 10,
            "avg_price": 0.55, "market_price": 0.40,
            "unrealized_pnl": -7.0, "cost_basis": 100.0,
        }], stop_loss_pct=0.15,
            stale_tickers={"KXTEST"}, suspect_tickers={"KXTEST"})
    out = buf.getvalue()
    for literal in ("StopGate: STALE", "StopGate: BASIS-SUSPECT"):
        assert literal in out
    # "StopGate: STALE" disambiguates against the pre-existing #661
    # STALE-CLOSE candidates-gate literal (review: a bare "STALE"
    # check was vacuous).
    assert "StopGate: STALE" in caddie_master_text
    assert "BASIS-SUSPECT" in caddie_master_text
    for literal in ("`STALE`", "`BASIS-SUSPECT`"):
        assert literal in monitor_text


def test_data_error_literal_shared_across_code_and_prompts(
    caddie_master_text: str, monitor_text: str,
) -> None:
    """#674 item 4: the DATA-ERROR banner ↔ conservative-path
    interplay, previously only pinned formatter-side."""
    from io import StringIO
    from unittest.mock import patch

    from rich.console import Console

    from gimmes.reporting import formatter

    buf = StringIO()
    with patch(
        "gimmes.reporting.formatter.console",
        Console(file=buf, width=80),
    ):
        formatter.format_positions([{
            "ticker": "KXTEST", "side": "no", "count": 10,
            "avg_price": 0.55, "market_price": 0.40,
            "unrealized_pnl": -7.0, "cost_basis": 0.0,
        }], stop_loss_pct=0.15)
    assert "StopGate: DATA-ERROR" in buf.getvalue()
    assert "DATA-ERROR" in caddie_master_text
    assert "DATA-ERROR" in monitor_text
