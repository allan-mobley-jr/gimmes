"""#787: one enum-drift market must degrade, not abort the scan.

parse_market maps unknown API status strings to the UNKNOWN
sentinel, which the set-difference construction of
UNTRADEABLE_STATUSES absorbs automatically — everything downstream
fails closed (#784 order gate refuses; log-outcome guard refuses).
"""

from __future__ import annotations

import logging

from gimmes.kalshi.markets import _parse_status, _warned_statuses, parse_market
from gimmes.models.market import (
    SETTLED_STATUSES,
    UNTRADEABLE_STATUSES,
    MarketStatus,
)


def test_unknown_status_parses_to_sentinel() -> None:
    m = parse_market({"ticker": "KXTEST-26AUG-T1", "status": "paused"})
    assert m.status is MarketStatus.UNKNOWN


def test_null_and_nonstring_status_degrade() -> None:
    """#787 review: an explicit JSON null (data.get returns None) and
    any non-string garbage take the same ValueError path — enum
    lookup raises ValueError for every non-member value."""
    assert parse_market({"ticker": "T", "status": None}).status is (
        MarketStatus.UNKNOWN
    )
    assert _parse_status(42) is MarketStatus.UNKNOWN
    assert _parse_status(["active"]) is MarketStatus.UNKNOWN


def test_known_statuses_still_parse() -> None:
    for member in MarketStatus:
        if member is MarketStatus.UNKNOWN:
            continue
        assert _parse_status(member.value) is member


def test_unknown_fails_closed_everywhere() -> None:
    assert MarketStatus.UNKNOWN in UNTRADEABLE_STATUSES
    assert MarketStatus.UNKNOWN not in SETTLED_STATUSES


def test_warns_once_per_status_string(caplog) -> None:
    _warned_statuses.discard(repr("halted"))
    with caplog.at_level(logging.WARNING, logger="gimmes.kalshi.markets"):
        _parse_status("halted")
        _parse_status("halted")
    hits = [r for r in caplog.records if "halted" in r.getMessage()]
    assert len(hits) == 1
    assert "#787" in hits[0].getMessage()
