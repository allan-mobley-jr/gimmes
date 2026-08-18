"""Unit tests for the observation read-back validator (#614).

These are pure-function tests — no DB, no CLI. The CLI integration tests
live in test_cli_observation_validator.py.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gimmes.store.observation_validator import (
    AGGREGATORS,
    ECONOMIC_CATEGORIES,
    NAMED_BANKS,
    PLAYBOOK_SOURCES,
    STALE_TEMPLATE_PHRASE,
    contains_stale_template,
    extract_cited_evidence,
    parse_playbook_footer,
    ticker_in_economic_category,
    validate,
    validate_playbook_footer,
    validate_semantics,
)


def _monitor_md() -> str:
    """monitor.md, read fresh — the drift-guard classes below pin the
    validator's hard-coded constants and grammar against it."""
    path = (
        Path(__file__).resolve().parents[2]
        / ".claude"
        / "agents"
        / "monitor.md"
    )
    return path.read_text()


C1407_STALE = (
    "No named major Wall Street bank has published April CPI MoM"
    " strictly above 0.5%"
)

CM_DECISION_WITH_BARCLAYS = (
    "Decision: HOLD.\n"
    "Reasoning: thesis intact; price unchanged.\n"
    "Cited sources:\n"
    "- Barclays April headline CPI MoM +0.55% (FXStreet, 2026-05-08)\n"
    "- Wells Fargo April headline CPI MoM +0.63% (FXStreet, 2026-05-08)"
)

CM_DECISION_NO_BANKS = (
    "Decision: HOLD.\n"
    "Reasoning: thesis intact; price moved within noise band.\n"
    "Cited sources:\n"
    "None — decision based on price + thesis only"
)


class TestTickerInEconomicCategory:
    def test_kxcpi_matches(self) -> None:
        assert ticker_in_economic_category("KXCPI-26APR-T0.5")

    def test_kxpayrolls_matches(self) -> None:
        assert ticker_in_economic_category("KXPAYROLLS-26APR-T100000")

    def test_kxjoblessclaims_matches(self) -> None:
        assert ticker_in_economic_category(
            "KXJOBLESSCLAIMS-26MAY14-210000",
        )

    def test_kxspx_does_not_match(self) -> None:
        # Equity-index tickers are out of scope.
        assert not ticker_in_economic_category("KXSPX-26MAY-T5000")

    def test_kxinx_does_not_match(self) -> None:
        assert not ticker_in_economic_category(
            "KXINX-26APR27H1600-B7162",
        )

    def test_kxnasdaq100_does_not_match(self) -> None:
        assert not ticker_in_economic_category(
            "KXNASDAQ100-26APR24H1600-B27350",
        )

    def test_case_insensitive(self) -> None:
        assert ticker_in_economic_category("kxcpi-26apr-t0.5")


class TestContainsStaleTemplate:
    def test_canonical_phrase_present(self) -> None:
        assert contains_stale_template(C1407_STALE)

    def test_case_insensitive_match(self) -> None:
        upper = C1407_STALE.upper()
        assert contains_stale_template(upper)

    def test_phrase_absent(self) -> None:
        assert not contains_stale_template(
            "Barclays +0.55% confirmed; thesis intact.",
        )

    def test_empty_body(self) -> None:
        assert not contains_stale_template("")


class TestExtractCitedEvidence:
    def test_extracts_barclays_with_value(self) -> None:
        pairs = extract_cited_evidence(CM_DECISION_WITH_BARCLAYS)
        # Should find both Barclays and Wells Fargo on their respective
        # lines, each paired with the inline numeric value.
        sources = [s for s, _ in pairs]
        values = [v for _, v in pairs]
        assert "Barclays" in sources
        assert "Wells Fargo" in sources
        assert "+0.55%" in values
        assert "+0.63%" in values

    def test_no_pairs_when_silent(self) -> None:
        assert extract_cited_evidence(CM_DECISION_NO_BANKS) == []

    def test_no_pairs_when_bank_without_value(self) -> None:
        body = (
            "Decision: HOLD.\n"
            "Reasoning: Barclays research suggests interest in this print"
            " but no quantitative claim made."
        )
        # No `%` on the Barclays line.
        assert extract_cited_evidence(body) == []

    def test_same_line_proximity_enforced(self) -> None:
        # "Barclays said April CPI is interesting" → no `%`.
        # Next paragraph: "+0.55% was the print".
        # Even though `+0.55%` appears in the body, it's not on the
        # Barclays line, so no pair.
        body = (
            "Barclays said April CPI is interesting.\n"
            "\n"
            "+0.55% was the print.\n"
        )
        assert extract_cited_evidence(body) == []

    def test_citi_aliases_match_citibank_and_citigroup(self) -> None:
        # Pre-#614 the regex was strict `\bCiti\b`, which silently let
        # "Citibank +0.42%" through as a non-citation. The silent-
        # failure-hunter on PR #614 surfaced this as a real silent-pass
        # path (CM prose freely writes Citibank/Citigroup for the same
        # institution). The validator now treats both as Citi citations.
        body = "Citibank research +0.42% on April CPI MoM."
        pairs = extract_cited_evidence(body)
        assert len(pairs) == 1
        assert pairs[0][0].startswith("Citi")
        assert pairs[0][1] == "+0.42%"

    def test_citi_matches_when_standalone(self) -> None:
        body = "Citi +0.42% on April CPI MoM."
        pairs = extract_cited_evidence(body)
        assert pairs == [("Citi", "+0.42%")]

    def test_citibank_matches_as_citi_alias(self) -> None:
        # CM prose often writes "Citibank analysts forecast +0.42%" or
        # "Citigroup +0.42%" — these are the same institution as "Citi"
        # in the playbook list. The validator must catch all three so a
        # CM citation under any form triggers the read-back rule.
        body = "Citibank analysts forecast +0.42% on April CPI MoM."
        pairs = extract_cited_evidence(body)
        assert len(pairs) == 1
        # The canonical-form returned is "Citibank" (the literal match);
        # downstream consumers only need to know SOME cited evidence
        # exists.
        source, value = pairs[0]
        assert source.startswith("Citi")
        assert value == "+0.42%"

    def test_citigroup_matches_as_citi_alias(self) -> None:
        body = "Citigroup +0.50% on April CPI MoM."
        pairs = extract_cited_evidence(body)
        assert len(pairs) == 1
        source, value = pairs[0]
        assert source.startswith("Citi")
        assert value == "+0.50%"

    def test_aggregator_match_fxstreet(self) -> None:
        body = "FXStreet aggregate +0.55% on April headline CPI MoM."
        pairs = extract_cited_evidence(body)
        assert pairs == [("FXStreet", "+0.55%")]

    def test_jpmorgan_full_name(self) -> None:
        body = "JPMorgan estimates +0.47% on April CPI MoM."
        pairs = extract_cited_evidence(body)
        assert pairs == [("JPMorgan", "+0.47%")]


class TestValidate:
    def test_reject_cited_bank_with_value_and_stale_template(self) -> None:
        ok, err = validate(
            ticker="KXCPI-26APR-T0.5",
            observation_body=C1407_STALE,
            decision_body=CM_DECISION_WITH_BARCLAYS,
        )
        assert ok is False
        assert err is not None
        assert "Barclays" in err or "Wells Fargo" in err
        # Error message must reference both issue numbers for
        # actionability.
        assert "#577" in err
        assert "#614" in err

    def test_allow_cm_silent_on_banks(self) -> None:
        # Vacuous case: CM cites no banks → nothing to contradict.
        ok, err = validate(
            ticker="KXCPI-26APR-T0.5",
            observation_body=C1407_STALE,
            decision_body=CM_DECISION_NO_BANKS,
        )
        assert ok is True
        assert err is None

    def test_allow_ticker_not_in_economic_category(self) -> None:
        # KXSPX is an equity index — out of validator scope.
        ok, err = validate(
            ticker="KXSPX-26MAY-T5000",
            observation_body=C1407_STALE,
            decision_body=CM_DECISION_WITH_BARCLAYS,
        )
        assert ok is True
        assert err is None

    def test_allow_no_prior_decision(self) -> None:
        # Position with no prior CM decision note yet.
        ok, err = validate(
            ticker="KXCPI-26APR-T0.5",
            observation_body=C1407_STALE,
            decision_body=None,
        )
        assert ok is True
        assert err is None

    def test_allow_bank_named_without_numeric_value(self) -> None:
        body = (
            "Decision: HOLD.\n"
            "Reasoning: Barclays research mentioned but no forecast"
            " quantified."
        )
        ok, err = validate(
            ticker="KXCPI-26APR-T0.5",
            observation_body=C1407_STALE,
            decision_body=body,
        )
        # No quantitative claim → nothing to contradict.
        assert ok is True
        assert err is None

    def test_allow_observation_without_stale_phrase(self) -> None:
        # Observation surfaces Barclays correctly — passes regardless of
        # what CM cited.
        obs = "Barclays +0.55% confirmed this cycle (FXStreet, 2026-05-08)."
        ok, err = validate(
            ticker="KXCPI-26APR-T0.5",
            observation_body=obs,
            decision_body=CM_DECISION_WITH_BARCLAYS,
        )
        assert ok is True
        assert err is None

    def test_case_insensitive_stale_phrase_still_rejects(self) -> None:
        ok, _ = validate(
            ticker="KXCPI-26APR-T0.5",
            observation_body=C1407_STALE.upper(),
            decision_body=CM_DECISION_WITH_BARCLAYS,
        )
        assert ok is False


class TestConstantsSyncWithMonitorMd:
    """Drift-guard: hard-coded constants in observation_validator.py
    must stay in sync with .claude/agents/monitor.md's playbook lists.
    Validator works without parsing monitor.md at runtime (avoids
    install-path fragility), but this test catches drift."""

    def test_banks_match_monitor_playbook(self) -> None:
        text = _monitor_md()
        for bank in NAMED_BANKS:
            assert bank in text, (
                f"NAMED_BANKS contains {bank!r} but monitor.md does not"
                f" mention it. Constants drift — sync the playbook."
            )

    def test_aggregators_match_monitor_playbook(self) -> None:
        text = _monitor_md()
        for source in AGGREGATORS:
            assert source in text, (
                f"AGGREGATORS contains {source!r} but monitor.md does"
                f" not mention it. Constants drift."
            )

    def test_economic_categories_match_monitor_playbook(self) -> None:
        text = _monitor_md()
        for prefix in ECONOMIC_CATEGORIES:
            # Word-boundary check so KXCPI doesn't accidentally match
            # KXCPICORE (which is also in the list — both must appear).
            assert re.search(rf"\b{re.escape(prefix)}\b", text), (
                f"ECONOMIC_CATEGORIES contains {prefix!r} but"
                f" monitor.md's playbook does not list it. Constants"
                f" drift."
            )

    def test_stale_template_phrase_is_lowercase(self) -> None:
        # Documented contract: the phrase is stored lowercase so the
        # matcher can compare with `body.lower()` (case-insensitive).
        assert STALE_TEMPLATE_PHRASE == STALE_TEMPLATE_PHRASE.lower()

    def test_stale_template_phrase_appears_in_monitor_md(self) -> None:
        """If monitor.md ever changes the canonical stale-template
        phrase, the validator silently misses every future c1407-class
        regression. Pin the phrase against monitor.md so a rename in
        the prompt forces a corresponding validator update."""
        text = _monitor_md().lower()
        assert STALE_TEMPLATE_PHRASE in text, (
            f"STALE_TEMPLATE_PHRASE {STALE_TEMPLATE_PHRASE!r} is not"
            f" present in monitor.md. The validator must pin the same"
            f" canonical phrase that monitor.md's FORBIDDEN clause"
            f" forbids. Update one or the other to re-align (#614)."
        )


class TestSyncWith643Rules:
    """Drift-guard for the #643 validators: the grammar literals the
    footer audit parses and the enforcement paragraphs must exist in
    monitor.md, and the footer template must enumerate exactly the
    PLAYBOOK_SOURCES the validator requires — in the same order."""

    def _footer_template_block(self) -> str:
        """The heredoc footer template in monitor.md, header through
        the GIMMES_EOF terminator."""
        match = re.search(
            r"Playbook sources checked this cycle.*?GIMMES_EOF",
            _monitor_md(),
            flags=re.DOTALL,
        )
        assert match is not None
        return match.group(0)

    def test_outcome_grammar_literals_in_monitor_md(self) -> None:
        text = _monitor_md()
        for literal in (
            "no result this cycle",
            "inherited: <prior cite>",
            "not searched (cadence — last full sweep",  # #731
            "Sweep: full (#731)",
            "Sweep: skipped (cadence #731",
            "SUPERSEDED (pre-<event>, <date>) — refresh required",
            "Semantics:",
        ):
            assert literal in text, (
                f"Footer/semantics grammar literal {literal!r} missing"
                f" from monitor.md — the #643 validator parses notes"
                f" written to that template; sync them."
            )

    def test_footer_template_enumerates_playbook_sources_in_order(
        self,
    ) -> None:
        footer = self._footer_template_block()
        positions = [footer.find(f"- {s}:") for s in PLAYBOOK_SOURCES]
        assert all(p != -1 for p in positions), (
            "Footer template missing a PLAYBOOK_SOURCES entry —"
            " the #643 enumeration check would reject every"
            " template-conformant write."
        )
        assert positions == sorted(positions), (
            "Footer template source order differs from"
            " PLAYBOOK_SOURCES — keep them aligned."
        )

    def test_enforcement_paragraphs_reference_643(self) -> None:
        text = _monitor_md()
        count = text.count("Runtime enforcement (#643)")
        assert count >= 2, (
            f"monitor.md must carry the two `Runtime enforcement"
            f" (#643)` paragraphs (semantics + footer); found {count}."
        )

    def test_template_footer_round_trips_through_validator(self) -> None:
        """Render monitor.md's own heredoc footer with each of the
        sweep-legal canonical outcome forms and prove the validator
        accepts it — the template and the parser must never drift
        apart (#643). The body carries a `Sweep: full` marker (#731)
        so the missing-marker warning stays out of warnings == []."""
        block = self._footer_template_block().rsplit("GIMMES_EOF", 1)[0]
        forms = {
            "Goldman Sachs": "+0.3% (Reuters, 2026-07-01)",
            "JPMorgan": "no result this cycle",
            "Citi": "inherited: +0.2% (FXStreet, 2026-06-18)",
            "Wells Fargo": (
                "SUPERSEDED (pre-Hormuz-reopening, 2026-06-11)"
                " — refresh required"
            ),
        }
        rendered = "Sweep: full (#731)\n" + re.sub(
            r"^- ([^:]+): \[.*$",
            lambda m: (
                f"- {m.group(1)}:"
                f" {forms.get(m.group(1), 'no result this cycle')}"
            ),
            block,
            flags=re.M,
        )
        errors, warnings = validate_playbook_footer(
            ticker="KXCPI-26JUN-T-0.1",
            observation_body=rendered,
            prior_observation_body=None,
        )
        assert errors == [], errors
        assert warnings == [], warnings
        rows = parse_playbook_footer(rendered)
        assert rows is not None
        assert {
            rows["Goldman Sachs"].kind, rows["JPMorgan"].kind,
            rows["Citi"].kind, rows["Wells Fargo"].kind,
        } == {"fresh", "no_result", "inherited", "superseded"}

    def test_template_footer_round_trips_skipped_mode(self) -> None:
        """#731: the non-sweep-cycle form — skipped marker with carried
        anchor, inherited/not-searched/SUPERSEDED-verbatim rows — must
        round-trip cleanly when the anchor matches the prior full
        sweep's timestamp."""
        block = self._footer_template_block().rsplit("GIMMES_EOF", 1)[0]
        not_searched = (
            "not searched (cadence — last full sweep 2026-07-10:"
            " no result)"
        )
        forms = {
            "Citi": "inherited: +0.2% (FXStreet, 2026-06-18)",
            "Wells Fargo": (
                "SUPERSEDED (pre-Hormuz-reopening, 2026-06-11)"
                " — refresh required"
            ),
        }
        def render(header: str, default: str) -> str:
            return header + re.sub(
                r"^- ([^:]+): \[.*$",
                lambda m: f"- {m.group(1)}: {forms.get(m.group(1), default)}",
                block,
                flags=re.M,
            )

        anchor = (
            datetime.now(UTC) - timedelta(hours=3)
        ).strftime("%Y-%m-%d %H:%M:%S")
        prior = render("Sweep: full (#731)\n", "no result this cycle")
        skipped = render(
            f"Sweep: skipped (cadence #731 — last full sweep {anchor})\n",
            not_searched,
        )
        errors, _ = validate_playbook_footer(
            ticker="KXCPI-26JUN-T-0.1",
            observation_body=skipped,
            prior_observation_body=prior,
            prior_observation_timestamp=anchor,
        )
        assert errors == [], errors

    def test_monitor_md_canonical_semantics_example_passes(self) -> None:
        """The worked example in monitor.md's #641 grounding rule must
        itself pass the semantics guard against the incident rules."""
        body = (
            "Semantics: YES wins when CPI MoM > -0.1%;"
            " NO wins when CPI MoM <= -0.1%"
        )
        rules = (
            "If the Consumer Price Index (CPI) increases by more than"
            " -0.1% (single-decimal) in June 2026, the market resolves"
            " to Yes."
        )
        errors, warnings = validate_semantics(
            ticker="KXCPI-26JUN-T-0.1",
            observation_body=body,
            rules_primary=rules,
        )
        assert errors == [] and warnings == []


class TestThresholdParseInconclusive:
    """#646: coverage telemetry for the semantics guard's silent
    blind spot — threshold-style ticker, non-empty snapshot, parse
    miss."""

    def test_unparseable_rules_on_threshold_ticker(self) -> None:
        from gimmes.store.observation_validator import (
            threshold_parse_inconclusive,
        )

        assert threshold_parse_inconclusive(
            "KXCPI-26APR-T0.5", "Settles per the committee's judgment.",
        )

    def test_parseable_rules_are_conclusive(self) -> None:
        from gimmes.store.observation_validator import (
            parse_rules_threshold,
            threshold_parse_inconclusive,
        )

        rules = "Resolves YES if the value is above 0.5 percent."
        assert parse_rules_threshold(rules) is not None
        assert not threshold_parse_inconclusive(
            "KXCPI-26APR-T0.5", rules,
        )

    def test_empty_snapshot_is_not_inconclusive(self) -> None:
        """Empty snapshots are the #647 backfill's job."""
        from gimmes.store.observation_validator import (
            threshold_parse_inconclusive,
        )

        assert not threshold_parse_inconclusive("KXCPI-26APR-T0.5", "")
        assert not threshold_parse_inconclusive("KXCPI-26APR-T0.5", None)

    def test_non_threshold_ticker_ignored(self) -> None:
        from gimmes.store.observation_validator import (
            threshold_parse_inconclusive,
        )

        assert not threshold_parse_inconclusive(
            "KXINX-26APR", "Settles per the committee's judgment.",
        )

    def test_negative_and_decimal_strikes_match(self) -> None:
        from gimmes.store.observation_validator import (
            threshold_parse_inconclusive,
        )

        for t in ("KXCPI-26JUN-T-0.1", "KXU3-26JUN-T4.3",
                  "KXPAYROLLS-26MAY-T80000"):
            assert threshold_parse_inconclusive(t, "opaque wording")


class TestFooterOnNonPlaybookTicker:
    """#648 item 2: a footer on a non-playbook ticker violates the
    Footer-omission rule — warn, never block."""

    def test_footer_on_equity_ticker_warns(self) -> None:
        from gimmes.store.observation_validator import (
            PLAYBOOK_SOURCES,
            validate_playbook_footer,
        )

        body = "Delta: none.\n\nPlaybook sources checked this cycle (#615):\n" + "\n".join(
            f"- {s}: no result this cycle" for s in PLAYBOOK_SOURCES
        )
        errors, warnings = validate_playbook_footer(
            ticker="KXINX-26APR-T5000",
            observation_body=body,
            prior_observation_body=None,
        )
        assert errors == []
        assert len(warnings) == 1
        assert "NON-playbook ticker" in warnings[0]

    def test_no_footer_on_equity_ticker_silent(self) -> None:
        from gimmes.store.observation_validator import (
            validate_playbook_footer,
        )

        errors, warnings = validate_playbook_footer(
            ticker="KXINX-26APR-T5000",
            observation_body="Delta: none.",
            prior_observation_body=None,
        )
        assert (errors, warnings) == ([], [])
