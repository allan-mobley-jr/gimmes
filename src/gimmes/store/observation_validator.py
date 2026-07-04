"""Observation validator — CLI-side runtime enforcement of Monitor's
read-back assertion (#577).

The read-back assertion in `.claude/agents/monitor.md` requires Monitor to
surface CM-cited named-bank / aggregator sources in its observation. The
rule is LLM-self-enforced; the c1407 regression demonstrated that LLM
compliance with prose rules is unreliable. This module provides a
deterministic runtime check: when a `--type observation` write contains
the canonical c1407 stale-template phrase ("No named major Wall Street
bank has published") AND the most-recent CM `decision` note for the same
ticker cites a named bank or aggregator with a numeric percentage value,
the write is rejected.

Scope: fundamental-economic-trigger tickers only (CPI/PCE/payrolls/etc.).
Equity-index tickers (KXSPX/KXINX/KXNASDAQ100) skip the validator —
they have no bank-forecast vocabulary to contradict.

Constants are hard-coded rather than parsed from monitor.md so the
validator works regardless of install path / worktree state. A
drift-guard test in `tests/unit/test_observation_validator.py` keeps
the constants in sync with monitor.md's playbook.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import NamedTuple

NAMED_BANKS: tuple[str, ...] = (
    "Goldman Sachs",
    "JPMorgan",
    "Morgan Stanley",
    "Bank of America",
    "Citi",
    "Barclays",
    "Wells Fargo",
    "Deutsche Bank",
    "UBS",
)

AGGREGATORS: tuple[str, ...] = (
    "FXStreet",
    "MarketWatch",
    "Reuters",
    "Bloomberg",
)

# Verbatim phrase from the c1407 regression. Case-insensitive match.
# Narrowly pinned — broader patterns risk rejecting legitimate
# "no result this cycle" entries that the playbook explicitly requires.
STALE_TEMPLATE_PHRASE: str = "no named major wall street bank has published"

# Fundamental-economic-trigger Kalshi category prefixes. Mirrors
# monitor.md's `## Fundamental-Economic-Trigger Source Playbook` list.
# Drift-guard test pins both lists in sync.
ECONOMIC_CATEGORIES: tuple[str, ...] = (
    "KXCPI",
    "KXCPICORE",
    "KXCPIYOY",
    "KXCPICOREYOY",
    "KXPCECORE",
    "KXPAYROLLS",
    "KXADP",
    "KXJOBLESSCLAIMS",
    "KXUE",
    "KXU3",
    "KXGDP",
    "KXGDPNOM",
    "KXFED",
    "KXFEDDECISION",
    "KXFEDCOMBO",
    "KXRATECUTCOUNT",
    "KXISMPMI",
)


_NUMERIC_VALUE_RE = re.compile(r"[-+]?\d+(?:\.\d+)?\s?%")


# Citi's institutional aliases — used by both the evidence extractor and
# the footer duplicate-row counter; keep in one place so they can't drift.
_CITI_ALIAS = "Citi(?:bank|group)?"


def _named_source_regex() -> re.Pattern[str]:
    # Citi is special-cased: CM prose frequently writes "Citibank" or
    # "Citigroup" as the same institution. Treating those as Citi
    # citations avoids a silent-pass where "Citibank analysts forecast
    # +0.42%" wouldn't trigger the validator. Other banks don't have
    # comparable widely-used aliases that conflict with the simple
    # word-boundary form.
    citi_alias = _CITI_ALIAS
    other_banks = [b for b in NAMED_BANKS if b != "Citi"]
    sources = other_banks + list(AGGREGATORS)
    # Sort by length desc so longer names match before substrings
    # (e.g., "Bank of America" before any subset).
    sources_sorted = sorted(sources, key=len, reverse=True)
    escaped = [re.escape(s) for s in sources_sorted]
    pattern = r"\b(?:" + "|".join([citi_alias, *escaped]) + r")\b"
    return re.compile(pattern)


_NAMED_SOURCE_RE = _named_source_regex()


def ticker_in_economic_category(ticker: str) -> bool:
    """Return True if `ticker` matches a fundamental-economic-trigger
    Kalshi prefix from the Monitor playbook."""
    upper = ticker.upper()
    for prefix in ECONOMIC_CATEGORIES:
        if upper.startswith(prefix):
            return True
    return False


def extract_cited_evidence(decision_body: str) -> list[tuple[str, str]]:
    """Return [(source_name, numeric_value)] for each named bank /
    aggregator in `decision_body` that has a numeric percentage value
    on the same line. Same-line proximity rather than character-window
    matches Monitor's surfacing format (bank + value on one line)."""
    if not decision_body:
        return []
    pairs: list[tuple[str, str]] = []
    for line in decision_body.splitlines():
        for source_match in _NAMED_SOURCE_RE.finditer(line):
            value_match = _NUMERIC_VALUE_RE.search(line)
            if value_match:
                pairs.append((source_match.group(0), value_match.group(0)))
                # One pair per line is enough — multiple matches on the
                # same line would be duplicates of the same evidence.
                break
    return pairs


def contains_stale_template(observation_body: str) -> bool:
    """Case-insensitive plain substring match for the c1407 stale-
    template phrase. The phrase must appear with its exact internal
    spacing — variants with embedded newlines or collapsed whitespace
    will NOT match. This is a tight pin against the verbatim c1407
    regression; broader matching risks rejecting legitimate
    "no result this cycle" entries that the playbook explicitly
    requires."""
    if not observation_body:
        return False
    return STALE_TEMPLATE_PHRASE in observation_body.lower()


def validate(
    *,
    ticker: str,
    observation_body: str,
    decision_body: str | None,
) -> tuple[bool, str | None]:
    """Validate an observation-write against the most-recent CM decision.

    Returns (ok, error_message). When `ok` is False, the caller MUST
    reject the write with exit 1 and surface `error_message`.

    Returns (True, None) when:
    - ticker is not in a fundamental-economic-trigger category
    - decision_body is None (no prior decision to contradict)
    - decision_body has no cited bank/aggregator with a numeric value
    - observation_body does not contain the stale-template phrase
    """
    if not ticker_in_economic_category(ticker):
        return (True, None)
    if decision_body is None:
        return (True, None)
    if not contains_stale_template(observation_body):
        return (True, None)
    cited = extract_cited_evidence(decision_body)
    if not cited:
        return (True, None)
    # Stale template AND CM cites at least one bank/aggregator with a
    # numeric value → reject.
    source, value = cited[0]
    err = (
        f"Observation contradicts cited evidence in the most-recent CM"
        f" decision for {ticker} (#577, #614). The CM decision cites"
        f" {source} {value}, but this observation contains the"
        f" stale-template phrase \"{STALE_TEMPLATE_PHRASE}\"."
        f" Re-write the observation to either reference a freshly"
        f" searched result, explicitly inherit the prior CM-cited"
        f" finding with citation, or — if a regime-change event"
        f" postdates that finding — mark it SUPERSEDED with the event"
        f" and date (#641). Do NOT use --force to bypass — that"
        f" is reserved for backfill scripts."
    )
    return (False, err)


# ---------------------------------------------------------------------------
# #643 runtime enforcement of the #641 rules: semantics guard + footer audit
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")


def _valid_iso_dates(text: str) -> list[str]:
    """ISO dates in textual order, dropping impossible ones (2026-13-45
    matches the regex but would silently pass lexicographic freshness
    checks now and permanently hard-block them once it becomes the
    prior cite)."""
    out: list[str] = []
    for candidate in _ISO_DATE_RE.findall(text):
        try:
            datetime.date.fromisoformat(candidate)
        except ValueError:
            continue
        out.append(candidate)
    return out

# Comparator library for Kalshi settlement sentences and Semantics lines.
# Deliberately small: any wording outside it makes the parse inconclusive
# and the semantics guard silently passes (#643 — never reject on parser
# uncertainty). Coverage telemetry / expansion is tracked in #646.
_CMP_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:increase|rise|grow)s?\s+by\s+more\s+than", re.I), "GT"),
    (re.compile(
        r"(?:is|are|be)?\s*(?:strictly\s+)?"
        r"(?:above|greater\s+than|more\s+than|exceeds?)", re.I,
    ), "GT"),
    (re.compile(r"at\s+or\s+above|at\s+least|or\s+higher|or\s+above", re.I), "GE"),
    (re.compile(r"(?:is|are|be)?\s*(?:strictly\s+)?(?:below|less\s+than|under)", re.I), "LT"),
    (re.compile(r"at\s+or\s+below|at\s+most|or\s+lower|or\s+below", re.I), "LE"),
)

_CMP_SYMBOLS: dict[str, str] = {
    ">=": "GE", "≥": "GE", "<=": "LE", "≤": "LE", ">": "GT", "<": "LT",
}
# Longest-first alternation so ">=" matches before its ">" prefix;
# finditer's non-overlapping matching skips the ">" inside a matched ">=".
_CMP_SYMBOL_RE = re.compile("|".join(re.escape(s) for s in _CMP_SYMBOLS))

# Threshold immediately after a comparator: at most a few filler
# characters (whitespace / opening paren / tilde), then a signed number.
_THRESHOLD_RE = re.compile(r"[ (~]{0,24}([-+]?\d+(?:\.\d+)?)")

_RESOLVES_RE = re.compile(r"resolves?\s+(?:to\s+)?['\"]?(yes|no)\b", re.I)

_COMPLEMENT: dict[str, str] = {"GT": "LE", "GE": "LT", "LT": "GE", "LE": "GT"}


def _direction_family(direction: str) -> str:
    """GT/GE are the 'up' family; LT/LE the 'down' family. Boundary
    nuance (GT vs GE) is deliberately NOT validated — only inversions."""
    return "up" if direction in ("GT", "GE") else "down"


def _find_comparator(text: str) -> list[tuple[int, int, str]]:
    """Return merged [(start, end, direction)] comparator hits.

    Overlapping hits with the SAME direction are merged into one span —
    "increases by more than" matches both the increase-form and the
    bare "more than" pattern, and counting that as two hits would trip
    the single-comparator gate on the exact sentences the guard exists
    for. Overlapping hits with DIFFERENT directions are kept separate
    (genuinely ambiguous → gate stays closed).
    """
    raw: list[tuple[int, int, str]] = []
    for pattern, direction in _CMP_PATTERNS:
        for m in pattern.finditer(text):
            raw.append((m.start(), m.end(), direction))
    for m in _CMP_SYMBOL_RE.finditer(text):
        raw.append((m.start(), m.end(), _CMP_SYMBOLS[m.group(0)]))
    # Specificity: a hit fully contained inside a strictly longer hit
    # is dropped regardless of direction — "at or above" (GE) contains
    # "above" (GT), and the containing phrase is the real comparator.
    kept = [
        (start, end, direction)
        for start, end, direction in raw
        if not any(
            s2 <= start and end <= e2 and (e2 - s2) > (end - start)
            for s2, e2, _ in raw
        )
    ]
    kept.sort()
    merged: list[tuple[int, int, str]] = []
    for start, end, direction in kept:
        if (
            merged
            and start < merged[-1][1]
            and direction == merged[-1][2]
        ):
            merged[-1] = (
                merged[-1][0], max(end, merged[-1][1]), direction,
            )
        else:
            merged.append((start, end, direction))
    return merged


def _number_after(text: str, pos: int) -> float | None:
    """Parse the numeric threshold immediately following a comparator.
    'Immediately' = within a few characters (whitespace / opening
    paren), so unrelated numbers later in the sentence (years, strike
    counts) can't be mistaken for the threshold."""
    m = _THRESHOLD_RE.match(text, pos)
    return float(m.group(1)) if m else None


def parse_rules_threshold(rules_primary: str) -> tuple[str, float] | None:
    """Parse a Kalshi settlement sentence into (yes_direction, threshold).

    High-confidence gate (#643): returns a result ONLY when the text
    contains exactly one comparator hit, a number immediately after it,
    and an unambiguous resolves-to anchor (all 'resolves to' mentions
    agree). Anything else returns None and the semantics guard stays
    dormant — a mis-parse that rejects a correct note is worse than no
    check.
    """
    if not rules_primary:
        return None
    hits = _find_comparator(rules_primary)
    if len(hits) != 1:
        return None
    _, end, direction = hits[0]
    threshold = _number_after(rules_primary, end)
    if threshold is None:
        return None
    anchors = {m.group(1).lower() for m in _RESOLVES_RE.finditer(rules_primary)}
    if len(anchors) != 1:
        return None
    if anchors == {"no"}:
        direction = _COMPLEMENT[direction]
    return (direction, threshold)


_NEGATION_RE = re.compile(
    r"\b(?:not|never|no|fails?\s+to|without|doesn't|does\s+not|isn't"
    r"|is\s+not|won't|will\s+not)\b|n't\b",
    re.I,
)


def _parse_claim(clause: str) -> tuple[str, float] | None:
    """Parse a 'YES wins when …' clause into (direction, threshold).
    First comparator hit wins; number must follow it immediately.

    Negated comparators ("NO wins when CPI is not above -0.1%") are a
    correct way to state a complement but would parse as the
    un-negated direction and hard-fail a correct note — any negation
    token before the comparator makes the clause unparseable instead
    (warning path, never reject)."""
    hits = _find_comparator(clause)
    if not hits:
        return None
    if _NEGATION_RE.search(clause[:hits[0][0]]):
        return None
    _, end, direction = hits[0]
    threshold = _number_after(clause, end)
    if threshold is None:
        return None
    return (direction, threshold)


_SEMANTICS_LINE_RE = re.compile(r"(?im)^Semantics:\s*(?P<rest>.+)$")
_YES_CLAUSE_RE = re.compile(r"YES\s+wins\s+(?:when|if)\s+(?P<c>.*?)(?:;|$)", re.I)
_NO_CLAUSE_RE = re.compile(r"NO\s+wins\s+(?:when|if)\s+(?P<c>.*)$", re.I)


def validate_semantics(
    *,
    ticker: str,
    observation_body: str,
    rules_primary: str | None,
) -> tuple[list[str], list[str]]:
    """Semantics guard (#643, enforcing #641 Finding 1).

    Returns (errors, warnings). Active only when the position's
    settlement-language snapshot parses at high confidence; otherwise
    silent. Hard-rejects only the swapped-semantics incident shape:
    the note restates the market's own threshold with an inverted
    comparator direction.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not rules_primary:
        return (errors, warnings)
    parsed = parse_rules_threshold(rules_primary)
    if parsed is None:
        return (errors, warnings)
    rules_dir, rules_threshold = parsed

    sem_match = _SEMANTICS_LINE_RE.search(observation_body or "")
    if sem_match is None:
        errors.append(
            f"Missing `Semantics:` line for threshold market {ticker}"
            f" (#641, #643). The settlement language is verifiable"
            f" (YES wins when value {rules_dir} {rules_threshold}) —"
            f" add a line in exactly this shape: `Semantics: YES wins"
            f" when <metric> <comparator> <threshold>; NO wins when"
            f" <complement>` per monitor.md."
        )
        return (errors, warnings)

    line = sem_match.group("rest")
    yes_m = _YES_CLAUSE_RE.search(line)
    no_m = _NO_CLAUSE_RE.search(line)
    yes_claim = _parse_claim(yes_m.group("c")) if yes_m else None
    no_claim = _parse_claim(no_m.group("c")) if no_m else None

    if yes_claim is None and no_claim is None:
        warnings.append(
            f"`Semantics:` line for {ticker} could not be parsed for"
            f" comparator/threshold — semantics cross-check skipped"
            f" (#643). Use the template form: YES wins when <metric>"
            f" <comparator> <threshold>; NO wins when <complement>."
        )
        return (errors, warnings)

    def _close(a: float, b: float) -> bool:
        return abs(a - b) < 1e-9

    if (
        yes_claim is not None and no_claim is not None
        and _close(yes_claim[1], no_claim[1])
        and _direction_family(yes_claim[0]) == _direction_family(no_claim[0])
    ):
        errors.append(
            f"`Semantics:` line for {ticker} is not internally"
            f" complementary: YES and NO both claim the"
            f" {_direction_family(yes_claim[0])} side of"
            f" {yes_claim[1]} (#641, #643). One side must be the"
            f" complement of the other."
        )
        return (errors, warnings)

    for label, claim, expected_dir in (
        ("YES", yes_claim, rules_dir),
        ("NO", no_claim, _COMPLEMENT[rules_dir]),
    ):
        if claim is None:
            continue
        claim_dir, claim_threshold = claim
        if not _close(claim_threshold, rules_threshold):
            warnings.append(
                f"`Semantics:` {label} clause for {ticker} uses"
                f" threshold {claim_threshold} but the settlement"
                f" language says {rules_threshold} (#643) — verify"
                f" units/rounding. Cross-check skipped for this clause."
            )
            continue
        if _direction_family(claim_dir) != _direction_family(expected_dir):
            errors.append(
                f"INVERTED SEMANTICS for {ticker} (#641, #643): the"
                f" `Semantics:` line claims {label} wins on the"
                f" {_direction_family(claim_dir)} side of"
                f" {claim_threshold}, but the settlement language"
                f" (\"{rules_primary.strip()[:120]}...\") puts {label}"
                f" on the {_direction_family(expected_dir)} side."
                f" This is the exact KXCPI-26JUN-T-0.1 inversion —"
                f" re-derive YES/NO from the Rules (primary) row of"
                f" market-info and re-write."
            )
    return (errors, warnings)

# --- Playbook footer audit (#643, enforcing #641 Finding 2) ---------------

_FOOTER_HEADER_RE = re.compile(r"(?m)^Playbook sources checked this cycle")
_FOOTER_ROW_RE = re.compile(r"^[-\u2013\u2014\u2022*]\s*(?P<source>[^:]+):\s*(?P<outcome>.+)$")

PLAYBOOK_SOURCES: tuple[str, ...] = NAMED_BANKS + AGGREGATORS


class _FooterRow(NamedTuple):
    """One parsed footer row. `kind` is one of: 'fresh', 'inherited',
    'no_result', 'superseded'."""

    kind: str
    pub_date: str | None
    event_date: str | None
    text: str


def _classify_row(outcome: str) -> _FooterRow:
    """Classify a footer row's outcome per the #642 four-outcome
    grammar, leniently (real rows carry trailing prose).

    Publication date extraction: the FIRST ISO date in textual order.
    The template grammar puts the citation date immediately after the
    publisher ("value (publisher, YYYY-MM-DD)"), so the first date is
    the publication date even when the row carries later search dates
    ("(Investing.com, 2026-06-23 — confirmed in search 2026-07-01)")
    or references to the date it replaces ("(GS, 2026-07-01, revising
    prior 2026-06-15 estimate)"). min() would mis-pick the replaced
    date and hard-fail exactly the diligent-refresh rows the #641
    rules encourage.
    """
    stripped = outcome.strip()
    lowered = stripped.lower()
    dates = _valid_iso_dates(stripped)
    first_date = dates[0] if dates else None
    if lowered.startswith("superseded"):
        return _FooterRow(
            "superseded", pub_date=None, event_date=first_date, text=stripped,
        )
    if lowered.startswith("inherited"):
        return _FooterRow(
            "inherited", pub_date=first_date, event_date=None, text=stripped,
        )
    if lowered.startswith("no result this cycle"):
        return _FooterRow("no_result", None, None, stripped)
    # Anything else is a bare fresh claim.
    return _FooterRow(
        "fresh", pub_date=first_date, event_date=None, text=stripped,
    )


def parse_playbook_footer(body: str) -> dict[str, _FooterRow] | None:
    """Extract the playbook audit footer from an observation body.

    Returns {source_name: row} or None when no footer header exists.
    Lines after the header that don't start a new `- Source:` row are
    treated as continuations of the previous row (agents wrap prose);
    a blank line ends the footer.
    """
    if not body:
        return None
    header = _FOOTER_HEADER_RE.search(body)
    if header is None:
        return None
    # Start AFTER the header line — its trailing text ("(#615 — OMIT
    # ...):") is not a row and must not terminate the footer scan.
    line_end = body.find("\n", header.end())
    if line_end == -1:
        return {}
    raw: dict[str, str] = {}
    current: str | None = None
    for line in body[line_end + 1:].splitlines():
        if not line.strip():
            if raw:
                break  # blank line after rows ends the footer
            continue  # blank line(s) between header and first row
        m = _FOOTER_ROW_RE.match(line.strip())
        if m:
            source = m.group("source").strip()
            # Same aliasing the #614 evidence extractor applies:
            # Citibank/Citigroup are the playbook's "Citi".
            if source in ("Citibank", "Citigroup"):
                source = "Citi"
            current = source
            raw[current] = m.group("outcome")
        elif current is not None and line[:1].isspace():
            # Indented lines are wrapped continuations of the previous
            # row. Flush-left prose after the rows ("Next CPI release
            # is 2026-06-10...") ends the footer instead of poisoning
            # the last row's date extraction.
            raw[current] += " " + line.strip()
        else:
            break
    return {source: _classify_row(outcome) for source, outcome in raw.items()}


def validate_playbook_footer(
    *,
    ticker: str,
    observation_body: str,
    prior_observation_body: str | None,
) -> tuple[list[str], list[str]]:
    """Footer audit (#643): four-outcome grammar, 13-source
    enumeration, date monotonicity for bare fresh rows, SUPERSEDED
    stickiness. Prior-cycle rows parse best-effort — a prior row that
    doesn't classify cleanly skips its cross-cycle checks (historical
    pre-#615 prose), but the CURRENT write must conform.

    Returns (errors, warnings).
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not ticker_in_economic_category(ticker):
        return (errors, warnings)

    footer = parse_playbook_footer(observation_body)
    if footer is None:
        errors.append(
            f"Missing `Playbook sources checked this cycle` footer for"
            f" economic-category ticker {ticker} (#615, #643). Every"
            f" observation on a playbook ticker MUST end with the"
            f" 13-source audit footer — see monitor.md."
        )
        return (errors, warnings)

    missing = [s for s in PLAYBOOK_SOURCES if s not in footer]
    if missing:
        errors.append(
            f"Playbook footer for {ticker} is missing source(s):"
            f" {', '.join(missing)} (#615, #643). All 13 sources"
            f" (9 banks + 4 aggregators) must be enumerated —"
            f" partial enumeration lets sources drop silently."
            f" (A present-but-malformed row also counts as missing:"
            f" each row must be `- Source: outcome`.)"
        )
    # Duplicate rows: dict semantics keep only the last, silently
    # discarding the first — surface it so a dated fresh claim can't
    # vanish behind a later duplicate.
    for source in PLAYBOOK_SOURCES:
        # Citi aliases normalize to the same parsed row, so count them
        # together — `- Citi:` plus `- Citigroup:` is a duplicate too
        # (#649 review).
        name_pattern = (
            _CITI_ALIAS if source == "Citi" else re.escape(source)
        )
        row_count = len(re.findall(
            rf"(?m)^[-\u2013\u2014\u2022*]\s*{name_pattern}\s*:",
            observation_body,
        ))
        if row_count > 1:
            warnings.append(
                f"Playbook footer for {ticker} lists `{source}`"
                f" {row_count} times — only the last row is used;"
                f" consolidate to one row per source (#643)."
            )
    unknown = [s for s in footer if s not in PLAYBOOK_SOURCES]
    if unknown:
        warnings.append(
            f"Playbook footer for {ticker} has unrecognized source(s):"
            f" {', '.join(unknown)} (#643) — not in the monitor.md"
            f" playbook list."
        )

    for source in PLAYBOOK_SOURCES:
        row = footer.get(source)
        if row is None:
            continue
        if row.kind == "fresh" and row.pub_date is None:
            hint = ""
            if "no result" in row.text.lower():
                hint = (
                    " (This row looks like a near-miss of the exact"
                    " phrase `no result this cycle` — use it verbatim.)"
                )
            errors.append(
                f"Footer row `{source}` for {ticker} reads as a fresh"
                f" result but carries no YYYY-MM-DD publication date"
                f" (#641, #643). A fresh claim without a date bypasses"
                f" the freshness rule — cite it as `value (publisher,"
                f" YYYY-MM-DD)`, or write `inherited: <prior cite>` /"
                f" `no result this cycle`.{hint}"
            )

    prior = (
        parse_playbook_footer(prior_observation_body)
        if prior_observation_body
        else None
    )
    if prior is None:
        return (errors, warnings)

    for source in PLAYBOOK_SOURCES:
        cur = footer.get(source)
        prev = prior.get(source)
        if cur is None or prev is None:
            continue
        if prev.kind in ("fresh", "inherited") and cur.kind == "fresh":
            if (
                cur.pub_date is not None
                and prev.pub_date is not None
                and cur.pub_date <= prev.pub_date
            ):
                errors.append(
                    f"Footer row `{source}` for {ticker} claims a fresh"
                    f" result dated {cur.pub_date}, but the prior cycle"
                    f" already cited {prev.pub_date} (#641, #643)."
                    f" Fresh means NEWLY PUBLISHED — re-finding the"
                    f" same dated note is `inherited: <prior cite>`."
                )
        elif prev.kind == "superseded":
            if cur.kind == "inherited":
                errors.append(
                    f"Footer row `{source}` for {ticker} reverted from"
                    f" SUPERSEDED to inherited (#641, #643)."
                    f" Supersession is sticky: it stays SUPERSEDED"
                    f" until a publication strictly newer than the"
                    f" regime-change event date is found."
                )
            elif cur.kind == "fresh":
                if prev.event_date is None:
                    warnings.append(
                        f"Footer row `{source}` for {ticker} goes"
                        f" SUPERSEDED -> fresh but the prior event date"
                        f" was unparseable (#643) — stickiness not"
                        f" verifiable this cycle."
                    )
                elif (
                    cur.pub_date is not None
                    and cur.pub_date <= prev.event_date
                ):
                    errors.append(
                        f"Footer row `{source}` for {ticker} claims a"
                        f" fresh result dated {cur.pub_date}, but the"
                        f" source was SUPERSEDED by an event dated"
                        f" {prev.event_date} (#641, #643) — only a"
                        f" publication strictly newer than the event"
                        f" clears supersession."
                    )
            elif cur.kind == "no_result":
                warnings.append(
                    f"Footer row `{source}` for {ticker} dropped a"
                    f" SUPERSEDED marker to `no result this cycle`"
                    f" (#643) — the supersession context is lost;"
                    f" prefer keeping the SUPERSEDED marker until"
                    f" refreshed."
                )
    return (errors, warnings)


def validate_observation(
    *,
    ticker: str,
    observation_body: str,
    decision_body: str | None,
    prior_observation_body: str | None,
    rules_primary: str | None,
) -> tuple[bool, list[str], list[str]]:
    """Combined observation-write validation (#614 + #643).

    Runs the read-back check (#614), the semantics guard, and the
    playbook footer audit, accumulating ALL errors and warnings so the
    agent can fix everything in one rewrite instead of ping-ponging.

    Returns (ok, errors, warnings). Callers reject the write (exit 1)
    when ok is False; warnings print but never block.
    """
    errors: list[str] = []
    warnings: list[str] = []

    ok, err = validate(
        ticker=ticker,
        observation_body=observation_body,
        decision_body=decision_body,
    )
    if not ok and err is not None:
        errors.append(err)

    sem_errors, sem_warnings = validate_semantics(
        ticker=ticker,
        observation_body=observation_body,
        rules_primary=rules_primary,
    )
    errors.extend(sem_errors)
    warnings.extend(sem_warnings)

    footer_errors, footer_warnings = validate_playbook_footer(
        ticker=ticker,
        observation_body=observation_body,
        prior_observation_body=prior_observation_body,
    )
    errors.extend(footer_errors)
    warnings.extend(footer_warnings)

    return (not errors, errors, warnings)


# ---------------------------------------------------------------------------
# #660: candidate probability flip detection
# ---------------------------------------------------------------------------
#
# KXCPI-26JUN-T-0.2 was scored NO-prob 0.98 (PROCEED, score 88) and
# 0.02 (PASS) 2.5 hours apart on IDENTICAL market facts — a negative-
# threshold side-convention inversion (the #641 class), with the
# inversion signature new_prob == 1 - prior_prob holding exactly. The
# flip was the CORRECTION, so this detector only WARNS — a hard
# reject would have blocked the correct row. The warning text must
# avoid scorer.py's red-flag keywords (carveout, discretion,
# subjective, ambiguous, unclear) or it would silently depress
# settlement-clarity scores when prepended to the memo.

FLIP_PROB_DELTA = 0.50
FLIP_PRICE_DELTA = 0.10
INVERSION_TOLERANCE = 0.05
FLIP_STALENESS_HOURS = 48
# The marker MUST stay uppercase: Rich only parses tags starting
# [a-z#/@], so [FLIP-WARNING] renders literally while a lowercase
# [flip-warning] would be silently swallowed as markup (#644 class).
FLIP_WARNING_MARKER = "[FLIP-WARNING]"


def _parse_scanned_at(value: str) -> datetime.datetime | None:
    """Parse a candidates.scanned_at timestamp (UTC), None on failure."""
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.datetime.strptime(
                text[:19], fmt,
            ).replace(tzinfo=datetime.UTC)
        except ValueError:
            continue
    return None


def detect_candidate_flip(
    *,
    prior_prob: float,
    prior_price: float,
    prior_scanned_at: str,
    new_prob: float,
    new_price: float,
    now: datetime.datetime | None = None,
) -> list[str]:
    """Warnings when a candidate's probability flips without a price move.

    Fires when |new_prob - prior_prob| > FLIP_PROB_DELTA while the
    YES-denominated market price moved <= FLIP_PRICE_DELTA — a move
    that market facts cannot explain (#660). When the flip also
    matches the inversion signature (new ~= 1 - prior within
    INVERSION_TOLERANCE), the message names the #641 side-convention
    class specifically. Skips degenerate priors (prob/price <= 0,
    pre-#657 rows) and priors older than FLIP_STALENESS_HOURS (a
    days-old scoring reflects a different market state). Comparison
    assumes both rows were scored under the same configured side —
    candidates carry no side column, so a mid-window strategy.side
    change could produce one spurious warning (accepted residual;
    this detector never blocks). ``now`` must be timezone-aware.
    """
    log = logging.getLogger(__name__)
    if (
        prior_prob <= 0 or prior_price <= 0
        or new_prob <= 0 or new_price <= 0
    ):
        log.debug("flip check skipped: degenerate prob/price (#660)")
        return []
    scanned = _parse_scanned_at(prior_scanned_at)
    if scanned is None:
        log.debug(
            "flip check skipped: unparseable scanned_at %r (#660)",
            prior_scanned_at,
        )
        return []
    current = now if now is not None else datetime.datetime.now(datetime.UTC)
    age_hours = (current - scanned).total_seconds() / 3600
    if age_hours > FLIP_STALENESS_HOURS:
        log.debug(
            "flip check skipped: prior scoring %.0fh old (#660)",
            age_hours,
        )
        return []
    prob_delta = abs(new_prob - prior_prob)
    price_delta = abs(new_price - prior_price)
    prob_inverted = (
        abs(new_prob - (1.0 - prior_prob)) <= INVERSION_TOLERANCE
    )
    # A confused agent logs the COMPLEMENT price alongside the
    # inverted probability (observed live: $0.40 -> $0.63, sum 1.03) —
    # that is itself the inversion, not a market move, so it must not
    # satisfy the price gate.
    # The complement-price bypass applies ONLY when the probability
    # also carries the inversion signature — a genuine repricing that
    # happens to land near the prior's complement must not fire a
    # "market facts cannot explain this" message (#660 review).
    price_inverted = (
        prob_inverted
        and abs(new_price - (1.0 - prior_price)) <= FLIP_PRICE_DELTA
    )
    if prob_delta <= FLIP_PROB_DELTA or (
        price_delta > FLIP_PRICE_DELTA and not price_inverted
    ):
        return []
    if prob_inverted:
        return [
            f"INVERSION SIGNATURE (#660/#641): new probability"
            f" {new_prob:.0%} is the complement of the prior"
            f" {prior_prob:.0%} scored {age_hours:.0f}h ago while the"
            f" market moved only {price_delta * 100:.0f}c — this is"
            f" the negative-threshold side-convention flip class."
            f" Re-derive YES/NO from Rules (primary) and state in the"
            f" memo which convention is correct and why the prior"
            f" scoring was wrong."
        ]
    return [
        f"PROBABILITY INSTABILITY (#660): {prob_delta * 100:.0f}pp"
        f" move from the prior scoring {age_hours:.0f}h ago on a"
        f" {price_delta * 100:.0f}c price move — market facts cannot"
        f" explain this. State in the memo what changed versus the"
        f" prior scoring."
    ]
