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
    APPROVE, REJECT) MUST end with a `Cited sources:` line. Without it,
    Monitor's read-back assertion (#577) is vacuously satisfied on most
    decisions and the structural defense against stale-template
    regressions is defanged (#617)."""
    occurrences = caddie_master_text.count("Cited sources:")
    assert occurrences >= 5, (
        f"Expected >= 5 occurrences of 'Cited sources:' in"
        f" caddie-master.md (4 templates + 1 in the shared rule"
        f" block), found {occurrences}. Every decision template MUST"
        " end with the Cited sources field (#617)."
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
