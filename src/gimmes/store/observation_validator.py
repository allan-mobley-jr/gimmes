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

import re

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


def _named_source_regex() -> re.Pattern[str]:
    # Citi is special-cased: CM prose frequently writes "Citibank" or
    # "Citigroup" as the same institution. Treating those as Citi
    # citations avoids a silent-pass where "Citibank analysts forecast
    # +0.42%" wouldn't trigger the validator. Other banks don't have
    # comparable widely-used aliases that conflict with the simple
    # word-boundary form.
    citi_alias = "Citi(?:bank|group)?"
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
    """Case-insensitive substring match for the c1407 stale-template
    phrase. Whitespace inside the phrase is tolerant (the phrase has
    spaces that real prose preserves)."""
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
        f" searched result OR explicitly inherit the prior CM-cited"
        f" finding with citation. Do NOT use --force to bypass — that"
        f" is reserved for backfill scripts."
    )
    return (False, err)
