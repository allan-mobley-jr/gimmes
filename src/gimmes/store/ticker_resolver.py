"""Prefix-match ticker resolution for CLI lookup commands (issue #582).

The CLI display layer wraps long tickers across multiple lines (#567's
fix), making partial copies easy. This module accepts a (possibly
partial) ticker prefix and returns the matching full ticker(s) from
local DB state so downstream lookup commands like ``position-context``
and ``market-info`` can keep using exact-match queries internally.

Behavior:
- Empty result: the prefix matches no known ticker. Caller decides
  whether to error or fall through (``market-info`` falls through to
  Kalshi with the literal input; ``position-context`` errors).
- Single result: the prefix uniquely resolves; caller substitutes the
  full ticker for its lookup.
- Multiple results: the prefix is ambiguous; caller renders a candidate
  list and exits non-zero.

Exact-match shortcut: when the caller's input is itself a known
ticker AND it happens to be a prefix of other tickers (e.g.
``KXCPI`` is a prefix of ``KXCPICORE-26APR-T0.3``), the resolver
returns ``[input]`` alone rather than the longer list. This preserves
backward compatibility for callers who type the full short ticker.
"""

from __future__ import annotations

import re
from typing import Literal

from gimmes.store.database import Database

TickerSource = Literal["open_positions", "known_markets"]


# Kalshi tickers are canonical uppercase alphanumerics with ``-`` and
# ``.`` separators (e.g. ``KXCPIYOY-26APR-T3.7``). Rejecting anything
# outside this charset prevents SQL ``LIKE`` wildcards (``%``, ``_``)
# in user input from silently matching unintended rows, and gives the
# resolver a tight contract: input is a literal Kalshi ticker prefix.
_TICKER_CHARS = re.compile(r"^[A-Z0-9.\-]+$")


_OPEN_POSITIONS_SQL = (
    "SELECT ticker FROM positions "
    "WHERE ticker LIKE ? || '%' AND count > 0 "
    "ORDER BY ticker"
)

_KNOWN_MARKETS_SQL = (
    "SELECT ticker FROM positions "
    "WHERE count > 0 AND ticker LIKE ? || '%' "
    "UNION SELECT ticker FROM candidates "
    "WHERE ticker LIKE ? || '%' "
    "UNION SELECT ticker FROM trades "
    "WHERE ticker LIKE ? || '%' "
    "ORDER BY ticker"
)


async def resolve_ticker(
    db: Database, prefix: str, *, source: TickerSource,
) -> list[str]:
    """Return tickers from ``source`` whose name starts with ``prefix``.

    Args:
        db: Async SQLite connection wrapper.
        prefix: User-supplied ticker or ticker fragment. Stripped of
            surrounding whitespace and uppercased before matching;
            Kalshi tickers are canonically uppercase. Raises
            ``ValueError`` if empty/whitespace-only or if any
            character falls outside ``[A-Z0-9.-]`` (this rejects SQL
            ``LIKE`` wildcards in user input).
        source:
            - ``"open_positions"``: only positions with ``count > 0``.
              Use for ``gimmes position-context`` and
              ``gimmes position-notes``.
            - ``"known_markets"``: distinct tickers from any of
              ``positions``, ``candidates``, ``trades``. Use for
              ``gimmes market-info``.

    Returns:
        Sorted list of matching tickers (deduplicated). If the cleaned
        prefix exactly equals one of the matches, the list is truncated
        to ``[match]`` so a full exact ticker that is also a prefix of
        others does not trigger an ambiguity error.
    """
    cleaned = prefix.strip().upper()
    if not cleaned:
        raise ValueError("ticker prefix must be non-empty")
    if not _TICKER_CHARS.match(cleaned):
        raise ValueError(
            f"ticker prefix {prefix!r} contains characters outside"
            f" [A-Z0-9.-]",
        )

    if source == "open_positions":
        cursor = await db.conn.execute(_OPEN_POSITIONS_SQL, (cleaned,))
    else:
        cursor = await db.conn.execute(
            _KNOWN_MARKETS_SQL, (cleaned, cleaned, cleaned),
        )
    rows = await cursor.fetchall()
    matches = [row[0] for row in rows]

    # Exact-match shortcut: if the cleaned input is itself in the match
    # set, return just that — sidesteps the ambiguous-error branch for
    # callers who happen to type a short ticker that's a prefix of
    # longer ones.
    if cleaned in matches:
        return [cleaned]

    return matches
