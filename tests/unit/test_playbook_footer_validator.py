"""Unit tests for the #643 playbook footer audit (enforcing #641
Finding 2 — stale forecasts marked "freshly confirmed" when re-found).

Covers the full reject/warn matrix: enumeration, dateless fresh
claims, date monotonicity vs the prior cycle's cite, and SUPERSEDED
stickiness. Prior-cycle rows parse best-effort (pre-#615 prose skips
per-source); the CURRENT write must conform.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gimmes.store.observation_validator import (
    AGGREGATORS,
    NAMED_BANKS,
    PLAYBOOK_SOURCES,
    parse_playbook_footer,
    pub_date_floor,
    ticker_period,
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
    # #731: sweep-cycle body — the Sweep: marker keeps the missing-
    # marker warning out of the warnings == [] assertions
    body = (
        "Delta since cycle 12:\nSweep: full (#731)\n"
        "Price: $0.63.\nOverall: ok.\n"
    )
    if footer is not None:
        body += "\n" + footer + "\n"
    return body


def _recent_anchor(hours_ago: float = 2.0) -> str:
    """An anchor inside the #731 48h age ceiling."""
    return (
        datetime.now(UTC) - timedelta(hours=hours_ago)
    ).strftime("%Y-%m-%d %H:%M:%S")


NOT_SEARCHED = "not searched (cadence — last full sweep 2026-07-10: no result)"


def _not_searched_rows() -> dict[str, str]:
    """make_footer overrides marking every source not-searched."""
    return {s.replace(" ", "_"): NOT_SEARCHED for s in PLAYBOOK_SOURCES}


def make_skipped_observation(
    footer: str | None,
    anchor: str | None = None,
) -> str:
    """#731 non-sweep-cycle body carrying the sweep anchor."""
    if anchor is None:
        anchor = _recent_anchor()
    body = (
        "Delta since cycle 12:\n"
        f"Sweep: skipped (cadence #731 — last full sweep {anchor})\n"
        "Price: $0.63.\nOverall: ok.\n"
    )
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


class TestPublicationDateWindow:
    """#874: a bare fresh row dated before the ticker's hard floor (first
    of the month three months before the period month) is a prior-year
    article — the 2016-11-04 FXStreet NFP preview that closed
    KXPAYROLLS-26OCT-T70000 had no prior cite and passed the #641
    first-time-find rule as "fresh by definition"."""

    PAYROLLS = "KXPAYROLLS-26OCT-T70000"
    NOW = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)

    def _run(self, ticker: str, **rows: str) -> tuple[list[str], list[str]]:
        return validate_playbook_footer(
            ticker=ticker,
            observation_body=make_observation(make_footer(**rows)),
            prior_observation_body=None,
            now=self.NOW,
        )

    def test_prior_year_first_time_fresh_row_rejected(self) -> None:
        errors, _ = self._run(
            self.PAYROLLS,
            FXStreet="Oct NFP consensus 170k-245k (FXStreet, 2016-11-04)",
        )
        assert len(errors) == 1
        for needle in (
            "FXStreet", "2016-11-04", "2026-07-01", "2026-10", "#874",
        ):
            assert needle in errors[0]

    @pytest.mark.parametrize(
        ("ticker", "source", "row"),
        [
            # in-window fresh cite
            (PAYROLLS, "FXStreet", "60k-90k (FXStreet, 2026-10-03)"),
            # floor is inclusive
            (PAYROLLS, "Reuters", "+70k (Reuters, 2026-07-01)"),
            # January market accepts a prior-year November forecast
            ("KXCPI-26JAN-T0.3", "Reuters", "+0.3% (Reuters, 2025-11-20)"),
            # no YYMON token: window check skipped
            ("KXGDP-26Q1-T2.5", "Reuters", "+2.1% (Reuters, 2016-11-04)"),
            # same-day cite is not "future"
            (PAYROLLS, "Reuters", "+70k (Reuters, 2026-10-22)"),
            # inherited row without a date is silent
            (PAYROLLS, "Citi", "inherited: +0.2% (FXStreet)"),
        ],
    )
    def test_clean_row_is_silent(
        self, ticker: str, source: str, row: str,
    ) -> None:
        errors, warnings = self._run(ticker, **{source: row})
        assert errors == []
        assert warnings == []

    @pytest.mark.parametrize(
        ("ticker", "bad_date", "floor"),
        [
            ("KXPAYROLLS-26OCT-T70000", "2026-06-30", "2026-07-01"),
            ("KXCPI-26JAN-T0.3", "2025-09-30", "2025-10-01"),
        ],
    )
    def test_day_before_floor_rejected(
        self, ticker: str, bad_date: str, floor: str,
    ) -> None:
        errors, _ = self._run(ticker, Reuters=f"+1 (Reuters, {bad_date})")
        assert any("#874" in e and bad_date in e and floor in e for e in errors)

    def test_future_dated_fresh_row_beyond_one_day_is_error(self) -> None:
        errors, warnings = self._run(
            self.PAYROLLS, Reuters="+70k (Reuters, 2099-01-01)",
        )
        assert warnings == []
        assert len(errors) == 1
        assert "#874" in errors[0] and "future" in errors[0]
        assert "not strictly newer" in errors[0]

    def test_future_check_runs_without_a_parseable_period(self) -> None:
        errors, _ = self._run(
            "KXGDP-26Q1-T2.5", Reuters="+2.1% (Reuters, 2099-01-01)",
        )
        assert any("#874" in e and "future" in e for e in errors)

    def test_fresh_row_dated_tomorrow_warns_only(self) -> None:
        errors, warnings = self._run(
            self.PAYROLLS, Reuters="+70k (Reuters, 2026-10-23)",
        )
        assert errors == []
        assert len(warnings) == 1
        assert "#874" in warnings[0] and "timezone slack" in warnings[0]

    def test_inherited_out_of_window_warns_only(self) -> None:
        errors, warnings = self._run(
            self.PAYROLLS, Citi="inherited: +0.2% (FXStreet, 2016-11-04)",
        )
        assert errors == []
        assert any("#874" in w and "Citi" in w for w in warnings)

    def test_window_error_coexists_with_monotonicity(self) -> None:
        prior = make_observation(make_footer(
            FXStreet="+70k (FXStreet, 2026-10-01)",
        ))
        errors, _ = validate_playbook_footer(
            ticker=self.PAYROLLS,
            observation_body=make_observation(make_footer(
                FXStreet="170k-245k (FXStreet, 2016-11-04)",
            )),
            prior_observation_body=prior,
            now=self.NOW,
        )
        assert any("#874" in e for e in errors)
        assert any("NEWLY PUBLISHED" in e for e in errors)

    def test_skipped_cycle_out_of_window_fresh_row_reports_731_only(
        self,
    ) -> None:
        """On a skipped cycle the #731 branch already rejects every
        fresh row; a second #874 error with a contradictory remedy
        ('write no result this cycle', itself forbidden on skipped
        cycles) must not pile on."""
        errors, _ = validate_playbook_footer(
            ticker=self.PAYROLLS,
            observation_body=make_skipped_observation(make_footer(
                FXStreet="170k-245k (FXStreet, 2016-11-04)",
            )),
            prior_observation_body=make_observation(make_footer()),
            now=self.NOW,
        )
        assert any("#731" in e and "FXStreet" in e for e in errors)
        assert not any("#874" in e for e in errors)

    def test_inherited_out_of_window_on_skipped_cycle_says_carry(
        self,
    ) -> None:
        cite = "inherited: +0.2% (FXStreet, 2016-11-04)"
        rows = _not_searched_rows()
        rows["Citi"] = cite
        errors, warnings = validate_playbook_footer(
            ticker=self.PAYROLLS,
            observation_body=make_skipped_observation(make_footer(**rows)),
            prior_observation_body=make_observation(make_footer(Citi=cite)),
            now=self.NOW,
        )
        assert errors == []
        w = [x for x in warnings if "#874" in x and "Citi" in x]
        assert len(w) == 1
        assert "carry it this cycle" in w[0]
        assert "next full sweep" in w[0]

    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("KXPAYROLLS-26OCT-T70000", (2026, 10)),
            ("KXGDP-26OCT30-T4.0", (2026, 10)),
            ("KXJOBLESSCLAIMS-26MAY14-210000", (2026, 5)),
            ("KXCPI-26JUN-T-0.1", (2026, 6)),
            ("KXUE-CAN26APR-6.6", (2026, 4)),
            ("KXCPI-26MARB-T0.3", (2026, 3)),
            ("kxcpi-26dec-t0.3", (2026, 12)),
            ("KXGDP-26Q1-T2.5", None),
            ("KXCPI-TEST", None),
        ],
    )
    def test_ticker_period_shapes(
        self, ticker: str, expected: tuple[int, int] | None,
    ) -> None:
        assert ticker_period(ticker) == expected

    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("KXCPI-26JAN-T0.3", "2025-10-01"),
            ("KXCPI-26FEB-T0.3", "2025-11-01"),
            ("KXCPI-26MAR-T0.3", "2025-12-01"),
            ("KXCPI-26APR-T0.3", "2026-01-01"),
            ("KXPAYROLLS-26OCT-T70000", "2026-07-01"),
        ],
    )
    def test_pub_date_floor_year_rollover(
        self, ticker: str, expected: str,
    ) -> None:
        floor = pub_date_floor(ticker)
        assert floor is not None
        assert floor.isoformat() == expected


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

    def test_citi_alias_duplicate_rows_warn(self) -> None:
        """`- Citi:` plus `- Citigroup:` normalize to the same parsed
        row (last silently wins) — the duplicate warning must count
        aliases together (#649 review)."""
        footer = (
            make_footer()
            + "\n- Citigroup: +0.2% (Reuters, 2026-07-01)"
        )
        errors, warnings = validate_playbook_footer(
            ticker=TICKER, observation_body=make_observation(footer),
            prior_observation_body=None,
        )
        assert any(
            "Citi" in w and "2 times" in w for w in warnings
        ), warnings


class TestSweepCadence731:
    """#731: the sweep-marker chain and the five-outcome grammar."""

    NOT_SEARCHED = NOT_SEARCHED

    def _prior_full(self) -> str:
        return make_observation(make_footer())

    def _not_searched_rows(self) -> dict[str, str]:
        return _not_searched_rows()

    def test_not_searched_row_classifies_without_dates(self) -> None:
        footer = parse_playbook_footer(
            make_skipped_observation(
                make_footer(Goldman_Sachs=self.NOT_SEARCHED),
            ),
        )
        row = footer["Goldman Sachs"]
        assert row.kind == "not_searched"
        # The last-sweep date must NOT leak into the freshness audit
        assert row.pub_date is None
        assert row.event_date is None

    def test_skip_mode_fresh_row_rejected(self) -> None:
        rows = self._not_searched_rows()
        rows["Barclays"] = "April CPI MoM +0.55% (FXStreet, 2026-07-15)"
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(make_footer(**rows)),
            prior_observation_body=self._prior_full(),
            prior_observation_timestamp=_recent_anchor(3.0),
        )
        assert any("claims a search ran" in e for e in errors)

    def test_skip_mode_no_result_row_rejected(self) -> None:
        rows = self._not_searched_rows()
        rows["Citi"] = "no result this cycle"
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(make_footer(**rows)),
            prior_observation_body=self._prior_full(),
            prior_observation_timestamp=_recent_anchor(3.0),
        )
        assert any("claims a search ran" in e for e in errors)

    def test_full_mode_not_searched_row_rejected(self) -> None:
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_observation(
                make_footer(UBS=self.NOT_SEARCHED),
            ),
            prior_observation_body=None,
        )
        assert any("a full sweep must search every source" in e for e in errors)

    def test_skip_anchor_matches_prior_full_timestamp_passes(self) -> None:
        anchor = _recent_anchor(3.0)
        rows = self._not_searched_rows()
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor=anchor,
            ),
            prior_observation_body=self._prior_full(),
            prior_observation_timestamp=anchor,
        )
        assert errors == []

    def test_forged_anchor_rejected(self) -> None:
        rows = self._not_searched_rows()
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor=_recent_anchor(1.0),
            ),
            prior_observation_body=self._prior_full(),
            prior_observation_timestamp=_recent_anchor(9.0),
        )
        assert any("not yours to refresh" in e for e in errors)

    def test_skipped_after_skipped_chain_carries_anchor(self) -> None:
        anchor = _recent_anchor(5.0)
        rows = self._not_searched_rows()
        prior = make_skipped_observation(make_footer(**rows), anchor=anchor)
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor=anchor,
            ),
            prior_observation_body=prior,
            prior_observation_timestamp=_recent_anchor(4.0),
        )
        assert errors == []

    def test_skipped_after_skipped_anchor_drift_rejected(self) -> None:
        rows = self._not_searched_rows()
        prior = make_skipped_observation(
            make_footer(**rows), anchor=_recent_anchor(5.0),
        )
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor=_recent_anchor(3.5),
            ),
            prior_observation_body=prior,
            prior_observation_timestamp=_recent_anchor(4.0),
        )
        assert any("copy the anchor VERBATIM" in e for e in errors)

    def test_skip_with_no_prior_marker_rejected(self) -> None:
        rows = self._not_searched_rows()
        # Prior observation predates #731 — no Sweep: line
        prior = (
            "Delta since cycle 11:\nPrice: $0.60.\n\n"
            + make_footer() + "\n"
        )
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(make_footer(**rows)),
            prior_observation_body=prior,
            prior_observation_timestamp="2026-07-16 09:00:00",
        )
        assert any("no sweep anchor exists on record" in e for e in errors)

    def test_skipped_marker_missing_timestamp_rejected(self) -> None:
        rows = self._not_searched_rows()
        body = (
            "Delta since cycle 12:\nSweep: skipped (cadence #731)\n"
            "Price: $0.63.\n\n" + make_footer(**rows) + "\n"
        )
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=body,
            prior_observation_body=self._prior_full(),
            prior_observation_timestamp="2026-07-16 09:00:00",
        )
        assert any("no parseable" in e for e in errors)

    def test_missing_sweep_marker_warns_only(self) -> None:
        body = (
            "Delta since cycle 12:\nPrice: $0.63.\n\n"
            + make_footer() + "\n"
        )
        errors, warnings = validate_playbook_footer(
            ticker=TICKER,
            observation_body=body,
            prior_observation_body=None,
        )
        assert errors == []
        assert any("no `Sweep:` marker" in w for w in warnings)

    def _skipped_vs_prior(self, prior_footer_overrides: dict) -> tuple:
        """Run a default all-not-searched skipped observation against a
        prior FULL sweep with the given row overrides."""
        anchor = _recent_anchor(3.0)
        prior = make_observation(make_footer(**prior_footer_overrides))
        rows = self._not_searched_rows()
        return validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor=anchor,
            ),
            prior_observation_body=prior,
            prior_observation_timestamp=anchor,
        )

    def test_cite_to_not_searched_is_error_on_skip(self) -> None:
        # ONLY-clause: a cited source MUST inherit on non-sweep cycles —
        # not_searched drops the date chain and launders #641
        errors, _ = self._skipped_vs_prior({
            "Barclays": "April CPI MoM +0.55% (FXStreet, 2026-07-08)",
        })
        assert any(
            "MUST use `inherited" in e and "Barclays" in e for e in errors
        )

    def test_inherited_cite_to_not_searched_is_error_on_skip(self) -> None:
        # The prior-inherited variant (M5 kill)
        errors, _ = self._skipped_vs_prior({
            "Citi": "inherited: +0.2% (FXStreet, 2026-06-18)",
        })
        assert any(
            "MUST use `inherited" in e and "Citi" in e for e in errors
        )

    def test_superseded_to_not_searched_is_error_on_skip(self) -> None:
        errors, _ = self._skipped_vs_prior({
            "Wells_Fargo": (
                "SUPERSEDED (pre-Hormuz-reopening, 2026-06-11)"
                " — refresh required"
            ),
        })
        assert any(
            "repeat the" in e and "SUPERSEDED" in e for e in errors
        )

    def test_no_result_to_inherited_is_error_on_skip(self) -> None:
        # Inheriting a citation that never existed is the same lie as
        # a fake no-result row
        anchor = _recent_anchor(3.0)
        rows = self._not_searched_rows()
        rows["JPMorgan"] = "inherited: +0.1% (Reuters, 2026-07-01)"
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor=anchor,
            ),
            prior_observation_body=self._prior_full(),  # all no_result
            prior_observation_timestamp=anchor,
        )
        assert any("nothing to" in e and "JPMorgan" in e for e in errors)

    def test_new_superseded_on_skip_is_error(self) -> None:
        # Recognizing a regime-change event IS the escalation trigger
        anchor = _recent_anchor(3.0)
        rows = self._not_searched_rows()
        rows["UBS"] = (
            "SUPERSEDED (pre-OPEC-shock, 2026-07-15) — refresh required"
        )
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor=anchor,
            ),
            prior_observation_body=self._prior_full(),
            prior_observation_timestamp=anchor,
        )
        assert any("escalation trigger" in e for e in errors)

    def test_cite_to_not_searched_warns_without_marker(self) -> None:
        # Marker-less bodies keep the old warning-grade behavior
        prior = make_observation(make_footer(
            Barclays="April CPI MoM +0.55% (FXStreet, 2026-07-08)",
        ))
        rows = self._not_searched_rows()
        body = (
            "Delta since cycle 12:\nPrice: $0.63.\n\n"
            + make_footer(**rows) + "\n"
        )
        errors, warnings = validate_playbook_footer(
            ticker=TICKER,
            observation_body=body,
            prior_observation_body=prior,
        )
        assert errors == []
        assert any("prior cite is lost" in w for w in warnings)

    def test_full_sweep_cite_to_no_result_warns(self) -> None:
        # The quiet-forgery tripwire: a full sweep dropping citations
        # to no-result is the lazy path of least resistance
        prior = make_observation(make_footer(
            Barclays="April CPI MoM +0.55% (FXStreet, 2026-07-08)",
        ))
        errors, warnings = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_observation(make_footer()),
            prior_observation_body=prior,
        )
        assert errors == []
        assert any("quiet forgery" in w for w in warnings)

    def test_anchor_older_than_48h_rejected(self) -> None:
        # The machine floor on sweep frequency (#577 by construction)
        rows = self._not_searched_rows()
        stale = _recent_anchor(72.0)
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor=stale,
            ),
            prior_observation_body=make_skipped_observation(
                make_footer(**rows), anchor=stale,
            ),
        )
        assert any("hard" in e and "ceiling" in e for e in errors)

    def test_multiple_sweep_lines_warn(self) -> None:
        anchor = _recent_anchor(3.0)
        rows = self._not_searched_rows()
        body = (
            "Delta since cycle 12:\n"
            "Sweep: full (#731)\n"
            f"Sweep: skipped (cadence #731 — last full sweep {anchor})\n"
            "\n" + make_footer(**rows) + "\n"
        )
        _, warnings = validate_playbook_footer(
            ticker=TICKER,
            observation_body=body,
            prior_observation_body=None,
        )
        assert any("only the FIRST is parsed" in w for w in warnings)

    def test_anchor_regex_scoped_to_marker_line(self) -> None:
        # M1 kill: a full timestamp in body PROSE must not satisfy the
        # skipped marker's anchor requirement
        rows = self._not_searched_rows()
        body = (
            "Delta since cycle 12:\n"
            "Sweep: skipped (cadence #731)\n"
            f"Note: last full sweep {_recent_anchor(3.0)} per history.\n"
            "\n" + make_footer(**rows) + "\n"
        )
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=body,
            prior_observation_body=self._prior_full(),
            prior_observation_timestamp=_recent_anchor(3.0),
        )
        assert any("no parseable" in e for e in errors)

    def test_prior_timestamp_with_microseconds_matches(self) -> None:
        # M2 kill: the [:19] slice tolerates microsecond-bearing rows
        anchor = _recent_anchor(3.0)
        rows = self._not_searched_rows()
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor=anchor,
            ),
            prior_observation_body=self._prior_full(),
            prior_observation_timestamp=anchor + ".123456",
        )
        assert errors == []

    def test_prior_timestamp_iso_t_form_matches(self) -> None:
        # M3 kill: ISO-T prior timestamps normalize to the space form
        anchor = _recent_anchor(3.0)
        rows = self._not_searched_rows()
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor=anchor,
            ),
            prior_observation_body=self._prior_full(),
            prior_observation_timestamp=anchor.replace(" ", "T"),
        )
        assert errors == []

    def test_body_anchor_iso_t_form_matches(self) -> None:
        # M4 kill: a T-form anchor in the marker normalizes too
        anchor = _recent_anchor(3.0)
        rows = self._not_searched_rows()
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor=anchor.replace(" ", "T"),
            ),
            prior_observation_body=self._prior_full(),
            prior_observation_timestamp=anchor,
        )
        assert errors == []

    def test_not_searched_to_fresh_next_sweep_passes(self) -> None:
        rows = self._not_searched_rows()
        prior = make_skipped_observation(make_footer(**rows))
        # Next sweep finds a note published BEFORE the last sweep date —
        # must not be blocked (the degenerate-cite poisoning trap)
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_observation(make_footer(
                Goldman_Sachs="June CPI nowcast -0.1% (Reuters, 2026-07-05)",
            )),
            prior_observation_body=prior,
        )
        assert errors == []


class TestInvalidAnchorDatetime731:
    def test_calendar_invalid_anchor_rejected(self) -> None:
        # Regex-valid but not a real datetime — must not bypass the
        # age ceiling (Copilot review on #731)
        rows = {
            s.replace(" ", "_"): (
                "not searched (cadence — last full sweep 2026-07-10:"
                " no result)"
            )
            for s in PLAYBOOK_SOURCES
        }
        errors, _ = validate_playbook_footer(
            ticker=TICKER,
            observation_body=make_skipped_observation(
                make_footer(**rows), anchor="2026-99-99 99:99:99",
            ),
            prior_observation_body=make_observation(make_footer()),
            prior_observation_timestamp="2026-99-99 99:99:99",
        )
        assert any("not a valid datetime" in e for e in errors)
