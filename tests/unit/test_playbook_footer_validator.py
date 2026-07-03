"""Unit tests for the #643 playbook footer audit (enforcing #641
Finding 2 — stale forecasts marked "freshly confirmed" when re-found).

Covers the full reject/warn matrix: enumeration, dateless fresh
claims, date monotonicity vs the prior cycle's cite, and SUPERSEDED
stickiness. Prior-cycle rows parse best-effort (pre-#615 prose skips
per-source); the CURRENT write must conform.
"""

from __future__ import annotations

from gimmes.store.observation_validator import (
    AGGREGATORS,
    NAMED_BANKS,
    PLAYBOOK_SOURCES,
    parse_playbook_footer,
    validate_playbook_footer,
)

TICKER = "KXCPI-26JUN-T-0.1"

_DEFAULT_ROW = "no result this cycle"


def make_footer(**overrides: str) -> str:
    """Build a full 13-source footer; override rows by kwargs keyed on
    the source name with spaces replaced by underscores."""
    lines = ["Playbook sources checked this cycle (#615):"]
    for source in PLAYBOOK_SOURCES:
        key = source.replace(" ", "_")
        lines.append(f"- {source}: {overrides.get(key, _DEFAULT_ROW)}")
    return "\n".join(lines)


def make_observation(footer: str | None) -> str:
    body = "Delta since cycle 12:\nPrice: $0.63.\nOverall: ok.\n"
    if footer is not None:
        body += "\n" + footer + "\n"
    return body


class TestParsePlaybookFooter:
    def test_no_header_returns_none(self) -> None:
        assert parse_playbook_footer("Price: $0.63. No footer here.") is None

    def test_parses_all_thirteen_rows(self) -> None:
        footer = parse_playbook_footer(make_observation(make_footer()))
        assert footer is not None
        assert set(footer) == set(NAMED_BANKS) | set(AGGREGATORS)

    def test_real_prose_row_first_date_extraction(self) -> None:
        """The real KXCPI-26JUN row carries the publication date AND a
        later search-confirmation date — the template puts the citation
        date first, so textual-first extraction picks the publication."""
        row = (
            "GS preliminary June 2026 CPI MoM = -0.13%"
            " (Investing.com, 2026-06-23 — freshly confirmed in"
            " search results 2026-07-01)"
        )
        footer = parse_playbook_footer(
            make_observation(make_footer(Goldman_Sachs=row))
        )
        assert footer is not None
        parsed = footer["Goldman Sachs"]
        assert parsed.kind == "fresh"
        assert parsed.pub_date == "2026-06-23"

    def test_superseded_row_extracts_event_date(self) -> None:
        row = "SUPERSEDED (pre-Hormuz-reopening, 2026-06-11) — refresh required"
        footer = parse_playbook_footer(
            make_observation(make_footer(Wells_Fargo=row))
        )
        assert footer is not None
        parsed = footer["Wells Fargo"]
        assert parsed.kind == "superseded"
        assert parsed.event_date == "2026-06-11"

    def test_continuation_lines_join_previous_row(self) -> None:
        body = make_observation(make_footer()).replace(
            "- Reuters: no result this cycle",
            "- Reuters: +0.2% consensus (Reuters,\n  2026-07-01)",
        )
        footer = parse_playbook_footer(body)
        assert footer is not None
        assert footer["Reuters"].kind == "fresh"
        assert footer["Reuters"].pub_date == "2026-07-01"

    def test_inherited_row_classified(self) -> None:
        row = "inherited: +0.30% (Investing.com, 2026-06-18)"
        footer = parse_playbook_footer(
            make_observation(make_footer(Bank_of_America=row))
        )
        assert footer is not None
        parsed = footer["Bank of America"]
        assert parsed.kind == "inherited"
        assert parsed.pub_date == "2026-06-18"


class TestValidatePlaybookFooter:
    def test_non_economic_ticker_skipped_entirely(self) -> None:
        errors, warnings = validate_playbook_footer(
            ticker="KXINX-26JUL-T5000",
            observation_body=make_observation(None),
            prior_observation_body=None,
        )
        assert errors == [] and warnings == []

    def test_missing_footer_rejected(self) -> None:
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_observation(None),
            prior_observation_body=None,
        )
        assert len(errors) == 1
        assert "Missing `Playbook sources checked this cycle`" in errors[0]
        assert "#615" in errors[0] and "#643" in errors[0]

    def test_full_footer_no_prior_passes(self) -> None:
        errors, warnings = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_observation(make_footer()),
            prior_observation_body=None,
        )
        assert errors == [] and warnings == []

    def test_missing_source_rejected(self) -> None:
        footer = make_footer()
        footer = footer.replace("- Barclays: no result this cycle\n", "")
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_observation(footer),
            prior_observation_body=None,
        )
        assert any("Barclays" in e and "missing source" in e for e in errors)

    def test_unknown_source_warns(self) -> None:
        footer = make_footer() + "\n- Nomura: +0.1% (FXStreet, 2026-07-01)"
        errors, warnings = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_observation(footer),
            prior_observation_body=None,
        )
        assert errors == []
        assert any("Nomura" in w and "unrecognized" in w for w in warnings)

    def test_fresh_without_date_rejected(self) -> None:
        footer = make_footer(Goldman_Sachs="GS says -0.13% per recent note")
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_observation(footer),
            prior_observation_body=None,
        )
        assert any(
            "Goldman Sachs" in e and "no YYYY-MM-DD" in e for e in errors
        )

    # --- date monotonicity (the Finding-2 incident) ---

    def test_refound_same_date_rejected(self) -> None:
        """Prior cycle cited 2026-06-18; this cycle re-finds the SAME
        note and writes it as fresh — the exact #641 miscount."""
        prior = make_observation(make_footer(
            Bank_of_America="+0.30% (Investing.com, 2026-06-18)",
        ))
        current = make_observation(make_footer(
            Bank_of_America="+0.30% (Investing.com, 2026-06-18)",
        ))
        errors, _ = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert any(
            "Bank of America" in e and "NEWLY PUBLISHED" in e
            for e in errors
        )

    def test_strictly_newer_date_passes(self) -> None:
        prior = make_observation(make_footer(
            Bank_of_America="+0.30% (Investing.com, 2026-06-18)",
        ))
        current = make_observation(make_footer(
            Bank_of_America="+0.46% (Investing.com, 2026-06-25)",
        ))
        errors, warnings = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert errors == [] and warnings == []

    def test_refound_older_than_inherited_cite_rejected(self) -> None:
        prior = make_observation(make_footer(
            Wells_Fargo="inherited: +0.25% (FXStreet, 2026-06-11)",
        ))
        current = make_observation(make_footer(
            Wells_Fargo="+0.25% (FXStreet, 2026-06-11)",
        ))
        errors, _ = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert any("Wells Fargo" in e for e in errors)

    def test_first_cite_passes_without_prior(self) -> None:
        prior = make_observation(make_footer())  # all "no result"
        current = make_observation(make_footer(
            Citi="+0.2% (Reuters, 2026-07-01)",
        ))
        errors, _ = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert errors == []

    def test_prior_prose_row_skips_cross_check(self) -> None:
        """Historical pre-#615 prose that classifies as dateless fresh
        must not block the current write's dated fresh row."""
        prior = make_observation(make_footer(
            UBS="checked, nothing relevant found lately",
        ))
        current = make_observation(make_footer(
            UBS="+0.15% (Bloomberg, 2026-07-02)",
        ))
        errors, _ = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert errors == []

    # --- SUPERSEDED stickiness ---

    SUPERSEDED_ROW = (
        "SUPERSEDED (pre-Hormuz-reopening, 2026-06-24) — refresh required"
    )

    def test_superseded_to_inherited_rejected(self) -> None:
        prior = make_observation(make_footer(Wells_Fargo=self.SUPERSEDED_ROW))
        current = make_observation(make_footer(
            Wells_Fargo="inherited: +0.25% (FXStreet, 2026-06-11)",
        ))
        errors, _ = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert any(
            "Wells Fargo" in e and "sticky" in e for e in errors
        )

    def test_superseded_to_stale_fresh_rejected(self) -> None:
        prior = make_observation(make_footer(Wells_Fargo=self.SUPERSEDED_ROW))
        current = make_observation(make_footer(
            Wells_Fargo="+0.25% (FXStreet, 2026-06-11)",
        ))
        errors, _ = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert any(
            "Wells Fargo" in e and "strictly newer than the event"
            in e for e in errors
        )

    def test_superseded_to_newer_fresh_passes(self) -> None:
        prior = make_observation(make_footer(Wells_Fargo=self.SUPERSEDED_ROW))
        current = make_observation(make_footer(
            Wells_Fargo="-0.05% (Bloomberg, 2026-07-02)",
        ))
        errors, warnings = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert errors == [] and warnings == []

    def test_superseded_to_superseded_passes(self) -> None:
        prior = make_observation(make_footer(Wells_Fargo=self.SUPERSEDED_ROW))
        current = make_observation(make_footer(
            Wells_Fargo=self.SUPERSEDED_ROW,
        ))
        errors, warnings = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert errors == [] and warnings == []

    def test_superseded_to_no_result_warns(self) -> None:
        prior = make_observation(make_footer(Wells_Fargo=self.SUPERSEDED_ROW))
        current = make_observation(make_footer())
        errors, warnings = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert errors == []
        assert any(
            "Wells Fargo" in w and "SUPERSEDED marker" in w
            for w in warnings
        )

    def test_superseded_unparseable_event_date_warns_on_fresh(self) -> None:
        prior = make_observation(make_footer(
            Wells_Fargo="SUPERSEDED (pre-Hormuz-reopening) — refresh required",
        ))
        current = make_observation(make_footer(
            Wells_Fargo="+0.25% (FXStreet, 2026-06-11)",
        ))
        errors, warnings = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert errors == []
        assert any("not verifiable" in w for w in warnings)


class TestFirstDateExtractionAndRowForms:
    """#643 review regressions: min()-date extraction hard-failed
    diligent-refresh rows; en-dash rows were absorbed; Citi aliases
    were rejected; impossible dates leaked through."""

    def test_revising_prior_row_uses_citation_date(self) -> None:
        """A fresh row that references the estimate it replaces must
        use the citation date (first), not the older replaced date."""
        prior = make_observation(make_footer(
            Goldman_Sachs="+0.27% (GS note, 2026-06-15)",
        ))
        current = make_observation(make_footer(
            Goldman_Sachs=(
                "+0.30% (Goldman note, 2026-07-01, revising prior"
                " 2026-06-15 estimate)"
            ),
        ))
        errors, warnings = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert errors == [] and warnings == []

    def test_post_event_refresh_citing_event_date_passes(self) -> None:
        """The supersession recovery path: a fresh publication strictly
        newer than the event, whose row also names the event date."""
        prior = make_observation(make_footer(
            Wells_Fargo=(
                "SUPERSEDED (pre-Hormuz-reopening, 2026-06-11)"
                " — refresh required"
            ),
        ))
        current = make_observation(make_footer(
            Wells_Fargo=(
                "+0.2% (Wells Fargo, 2026-07-01, post-Hormuz-reopening"
                " 2026-06-11 refresh)"
            ),
        ))
        errors, warnings = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert errors == [] and warnings == []

    def test_en_dash_row_not_absorbed(self) -> None:
        """An en-dash bullet must parse as its own row, not merge into
        the previous row and poison its date extraction."""
        footer = make_footer().replace(
            "- Goldman Sachs: no result this cycle",
            "\u2013 Goldman Sachs: +0.3% (Reuters, 2026-07-01)",
        )
        rows = parse_playbook_footer(make_observation(footer))
        assert rows is not None
        assert rows["Goldman Sachs"].kind == "fresh"
        assert rows["Goldman Sachs"].pub_date == "2026-07-01"

    def test_citi_aliases_normalized(self) -> None:
        footer = make_footer().replace(
            "- Citi: no result this cycle",
            "- Citigroup: no result this cycle",
        )
        errors, warnings = validate_playbook_footer(
            ticker=TICKER, observation_body=make_observation(footer),
            prior_observation_body=None,
        )
        assert errors == [] and warnings == []

    def test_impossible_date_treated_as_dateless(self) -> None:
        """2026-13-45 matches the date regex shape but is not a date —
        a fresh row carrying only an impossible date must hit the
        dateless-fresh reject, not silently pass freshness."""
        footer = make_footer(
            Goldman_Sachs="+0.3% (Reuters, 2026-13-45)",
        )
        errors, _ = validate_playbook_footer(
            ticker=TICKER, observation_body=make_observation(footer),
            prior_observation_body=None,
        )
        assert any(
            "Goldman Sachs" in e and "no YYYY-MM-DD" in e for e in errors
        )

    def test_impossible_prior_date_does_not_block_forever(self) -> None:
        """Once an impossible date sits in the PRIOR footer it must be
        ignored — not lexically compared, which would hard-block every
        legitimate fresh claim until 2027."""
        prior = make_observation(make_footer(
            Goldman_Sachs="+0.3% (Reuters, 2026-13-45)",
        ))
        current = make_observation(make_footer(
            Goldman_Sachs="+0.35% (Reuters, 2026-07-02)",
        ))
        errors, _ = validate_playbook_footer(
            ticker=TICKER, observation_body=current,
            prior_observation_body=prior,
        )
        assert errors == []

    def test_duplicate_source_rows_warn(self) -> None:
        footer = (
            make_footer()
            + "\n- Goldman Sachs: +0.3% (Reuters, 2026-07-01)"
        )
        errors, warnings = validate_playbook_footer(
            ticker=TICKER, observation_body=make_observation(footer),
            prior_observation_body=None,
        )
        assert any(
            "Goldman Sachs" in w and "2 times" in w for w in warnings
        )

    def test_flush_left_prose_after_footer_ends_it(self) -> None:
        """Trailing flush-left prose (no blank line) must NOT join the
        last row and poison its date extraction."""
        body = make_observation(make_footer(
            Bloomberg="+0.25% (Bloomberg consensus, 2026-07-01)",
        )).rstrip() + "\nNext CPI release is 2026-06-10; will re-check then.\n"
        rows = parse_playbook_footer(body)
        assert rows is not None
        assert rows["Bloomberg"].pub_date == "2026-07-01"
