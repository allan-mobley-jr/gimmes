"""Unit tests for the #643 semantics guard (enforcing #641 Finding 1).

Pure-function tests: parse_rules_threshold() and validate_semantics().
The guard's contract is precision-first — it hard-rejects ONLY the
swapped-semantics incident shape (note restates the market's own
threshold with an inverted comparator) and silently passes whenever a
parse is inconclusive. Every reject asserted here is paired with a
nearby pass proving the gate's conservatism.
"""

from __future__ import annotations

from gimmes.store.observation_validator import (
    parse_rules_threshold,
    validate_semantics,
)

# The literal #641 incident market.
INCIDENT_RULES = (
    "If the Consumer Price Index (CPI) increases by more than -0.1%"
    " (single-decimal) in June 2026, the market resolves to Yes."
)

TICKER = "KXCPI-26JUN-T-0.1"


class TestParseRulesThreshold:
    def test_incident_sentence_parses_gt(self) -> None:
        assert parse_rules_threshold(INCIDENT_RULES) == ("GT", -0.1)

    def test_positive_threshold(self) -> None:
        rules = (
            "If the CPI increases by more than 0.5% in April 2026,"
            " the market resolves to Yes."
        )
        assert parse_rules_threshold(rules) == ("GT", 0.5)

    def test_at_or_above_parses_ge(self) -> None:
        rules = (
            "If the unemployment rate is at or above 4.6% for July"
            " 2026, the market resolves to Yes."
        )
        assert parse_rules_threshold(rules) == ("GE", 4.6)

    def test_resolves_to_no_inverts_direction(self) -> None:
        rules = (
            "If initial jobless claims exceed 250000 for the week,"
            " the market resolves to No."
        )
        # YES direction is the complement of the stated condition.
        assert parse_rules_threshold(rules) == ("LE", 250000.0)

    def test_two_comparators_is_inconclusive(self) -> None:
        rules = (
            "If the CPI is above 0.1% and below 0.5%, the market"
            " resolves to Yes."
        )
        assert parse_rules_threshold(rules) is None

    def test_no_resolves_anchor_is_inconclusive(self) -> None:
        assert parse_rules_threshold("CPI rises by more than 0.5%.") is None

    def test_conflicting_anchors_is_inconclusive(self) -> None:
        rules = (
            "If the CPI increases by more than 0.5%, the market"
            " resolves to Yes. Otherwise the market resolves to No."
        )
        assert parse_rules_threshold(rules) is None

    def test_no_number_after_comparator_is_inconclusive(self) -> None:
        rules = (
            "If the CPI increases by more than the threshold shown,"
            " the market resolves to Yes. Threshold: 0.5%."
        )
        assert parse_rules_threshold(rules) is None

    def test_empty_rules_is_inconclusive(self) -> None:
        assert parse_rules_threshold("") is None

    def test_year_not_mistaken_for_threshold(self) -> None:
        # The number must IMMEDIATELY follow the comparator — "June
        # 2026" later in the sentence must not be picked up.
        rules = (
            "If the CPI increases by more than [redacted] in June"
            " 2026, the market resolves to Yes."
        )
        assert parse_rules_threshold(rules) is None


def _sem(line: str) -> str:
    return f"Delta since cycle 12:\nPrice: $0.63.\n{line}\nOverall: ok.\n"


class TestValidateSemantics:
    def test_incident_inversion_rejected(self) -> None:
        """The exact #641 failure: YES described as the deflation side
        on a market whose rules put YES on the flat-or-positive side."""
        body = _sem(
            "Semantics: YES wins when CPI MoM <= -0.1%;"
            " NO wins when CPI MoM > -0.1%"
        )
        errors, warnings = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        assert errors, "swapped semantics must reject"
        assert any("INVERTED SEMANTICS" in e for e in errors)
        assert any("#641" in e and "#643" in e for e in errors)

    def test_correct_semantics_passes_clean(self) -> None:
        body = _sem(
            "Semantics: YES wins when CPI MoM > -0.1%;"
            " NO wins when CPI MoM <= -0.1%"
        )
        errors, warnings = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        assert errors == []
        assert warnings == []

    def test_word_comparators_accepted(self) -> None:
        body = _sem(
            "Semantics: YES wins when CPI MoM is above -0.1%;"
            " NO wins when CPI MoM is at or below -0.1%"
        )
        errors, warnings = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        # warnings must ALSO be empty: a regression demoting these to
        # the unparseable-warning path would otherwise pass silently
        # (mutation-verified in review).
        assert errors == [] and warnings == []

    def test_missing_semantics_line_rejected_when_rules_parse(self) -> None:
        body = "Delta since cycle 12:\nPrice: $0.63.\nOverall: ok.\n"
        errors, _ = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        assert len(errors) == 1
        assert "Missing `Semantics:` line" in errors[0]

    def test_missing_semantics_line_passes_without_rules(self) -> None:
        body = "Delta since cycle 12:\nPrice: $0.63.\nOverall: ok.\n"
        for rules in (None, ""):
            errors, warnings = validate_semantics(
                ticker=TICKER, observation_body=body, rules_primary=rules,
            )
            assert errors == [] and warnings == []

    def test_missing_semantics_line_passes_on_inconclusive_rules(
        self,
    ) -> None:
        """Parser uncertainty must NEVER reject (#643)."""
        body = "Delta since cycle 12:\nPrice: $0.63.\nOverall: ok.\n"
        errors, warnings = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary="Settlement at the exchange's discretion.",
        )
        assert errors == [] and warnings == []

    def test_unparseable_clauses_warn_not_reject(self) -> None:
        body = _sem(
            "Semantics: YES wins when the print lands in deflation"
            " territory; NO wins otherwise"
        )
        errors, warnings = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        assert errors == []
        assert len(warnings) == 1
        assert "could not be parsed" in warnings[0]

    def test_non_complementary_clauses_rejected(self) -> None:
        body = _sem(
            "Semantics: YES wins when CPI MoM > -0.1%;"
            " NO wins when CPI MoM > -0.1%"
        )
        errors, _ = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        assert len(errors) == 1
        assert "not internally complementary" in errors[0]

    def test_threshold_mismatch_warns_not_rejects(self) -> None:
        """A different threshold value is legitimate (units, rounding,
        annualization) — warn and skip the cross-check."""
        body = _sem(
            "Semantics: YES wins when CPI MoM > 0.4%;"
            " NO wins when CPI MoM <= 0.4%"
        )
        rules = (
            "If the CPI increases by more than 0.5% in April 2026,"
            " the market resolves to Yes."
        )
        errors, warnings = validate_semantics(
            ticker="KXCPI-26APR-T0.5", observation_body=body,
            rules_primary=rules,
        )
        assert errors == []
        assert len(warnings) == 2  # one per clause
        assert all("verify units/rounding" in w for w in warnings)

    def test_boundary_nuance_gt_vs_ge_passes(self) -> None:
        """GT vs GE is deliberately not validated — only inversions."""
        body = _sem(
            "Semantics: YES wins when CPI MoM >= -0.1%;"
            " NO wins when CPI MoM < -0.1%"
        )
        errors, warnings = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        assert errors == [] and warnings == []

    def test_single_clause_inversion_still_caught(self) -> None:
        """A Semantics line with only a YES clause still cross-checks."""
        body = _sem("Semantics: YES wins when CPI MoM < -0.1%")
        errors, _ = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        assert len(errors) == 1
        assert "INVERTED SEMANTICS" in errors[0]

    def test_resolves_to_no_market_cross_checks_correctly(self) -> None:
        rules = (
            "If initial jobless claims exceed 250000 for the week,"
            " the market resolves to No."
        )
        # YES = claims <= 250000; a note claiming YES on the up side
        # of 250000 is inverted.
        body = _sem(
            "Semantics: YES wins when claims > 250000;"
            " NO wins when claims <= 250000"
        )
        errors, _ = validate_semantics(
            ticker="KXJOBLESSCLAIMS-26JUL-250000",
            observation_body=body, rules_primary=rules,
        )
        assert errors
        assert any("INVERTED SEMANTICS" in e for e in errors)


    def test_no_only_clause_inversion_caught(self) -> None:
        """NO described on the up side of the threshold (the rules put
        NO on the down side) — mutation testing showed this branch was
        previously unpinned."""
        body = _sem("Semantics: NO wins when CPI MoM > -0.1%")
        errors, _ = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        assert len(errors) == 1
        assert "INVERTED SEMANTICS" in errors[0]

    def test_no_only_clause_correct_passes(self) -> None:
        body = _sem("Semantics: NO wins when CPI MoM <= -0.1%")
        errors, warnings = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        assert errors == [] and warnings == []

    def test_negated_complement_warns_not_rejects(self) -> None:
        """'NO wins when CPI is not above X' is a CORRECT complement —
        negation-blind parsing would read it as the un-negated
        direction and hard-fail a correct note (#643 review)."""
        body = _sem(
            "Semantics: YES wins when CPI MoM is above -0.1%;"
            " NO wins when CPI MoM is not above -0.1%"
        )
        errors, warnings = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        assert errors == []

    def test_fails_to_exceed_negation_warns_not_rejects(self) -> None:
        body = _sem(
            "Semantics: YES wins when CPI MoM exceeds -0.1%;"
            " NO wins when CPI MoM fails to exceed -0.1%"
        )
        errors, warnings = validate_semantics(
            ticker=TICKER, observation_body=body,
            rules_primary=INCIDENT_RULES,
        )
        assert errors == []


class TestResolvesAnchorForms:
    def test_resolves_yes_if_form_parses(self) -> None:
        """Kalshi rules also use 'resolves YES if' (no 'to') — the
        repo's own market fixtures carry this form (#643 review)."""
        rules = (
            "This market resolves YES if the CPI YoY exceeds 3.2%"
            " for June 2026."
        )
        assert parse_rules_threshold(rules) == ("GT", 3.2)
