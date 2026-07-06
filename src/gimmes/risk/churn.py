"""Same-ticker reopen churn guards (#661).

KXGDP-26JUL30-T3.0 was opened, closed on genuinely new information,
and REOPENED 21 seconds after Caddie Master's own cooldown note — the
Closer executed a candidate scanned 12 minutes before the close, so
the reopen was priced on data the close had already invalidated. Two
scratch round-trips in 33 minutes, all at $0.71, ~$18 in fees.

Prompt-level cooldowns demonstrably failed here, so the reopen gate is
a HARD CLI rejection: an open against a ticker whose most recent
non-reconcile close is under an hour old at roughly the same price is
a fee-burning round trip by construction. Constants are deliberately
NOT configurable (#659 anti-rule-lawyering precedent). The escape
hatch is the dedicated ``--force-reopen`` flag — never plain
``--force`` — so agent prompts can forbid it cleanly.
"""

from __future__ import annotations

import datetime

REOPEN_LOCKOUT_MINUTES = 60
REOPEN_PRICE_DELTA = 0.05
ROUNDTRIP_WARN_MINUTES = 60


def _parse_trade_timestamp(value: str) -> datetime.datetime | None:
    """Parse a trades.timestamp value; naive values are UTC.

    Trades rows carry tz-aware ISO strings from TradeDecision, but the
    schema default (``datetime('now')``) is naive SQLite UTC — accept
    both. None on failure (callers fail open: this is a churn guard,
    not a ledger).
    """
    text = str(value).strip().replace(" ", "T", 1)
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _minutes_since(
    timestamp: str, now: datetime.datetime | None,
) -> float | None:
    parsed = _parse_trade_timestamp(timestamp)
    if parsed is None:
        return None
    current = (
        now if now is not None
        else datetime.datetime.now(datetime.UTC)
    )
    return (current - parsed).total_seconds() / 60


def check_reopen_churn(
    *,
    close_price: float,
    close_timestamp: str,
    close_agent: str,
    entry_price: float,
    close_side: str | None = None,
    entry_side: str | None = None,
    now: datetime.datetime | None = None,
) -> str | None:
    """Rejection message when an open repeats a fresh close, else None.

    Fires when the ticker's most recent close is under
    REOPEN_LOCKOUT_MINUTES old AND the new entry price is within
    REOPEN_PRICE_DELTA of the close price — a same-price round trip
    is pure fee loss by construction (#661). Reconcile closes are
    broker drift, not decisions, and never arm the gate (#586/#609
    semantics). ``now`` must be timezone-aware.

    Prices are side-effective, so when both sides are known and
    differ, the close price is flipped into the entry's denomination
    before the band check (#678): closing NO at $0.71 then buying YES
    at $0.29 is the SAME price point (churn), while YES at $0.71
    after that close is a 42-cent real move (legit). Missing or junk
    side values fall back to the side-blind comparison — this is a
    fail-open guard.
    """
    if close_agent == "reconcile":
        return None
    age_minutes = _minutes_since(close_timestamp, now)
    if age_minutes is None or age_minutes < 0:
        return None
    if age_minutes > REOPEN_LOCKOUT_MINUTES:
        return None
    effective_close = close_price
    flipped = False
    # The set equality does all the guarding: equal sides build a
    # size-1 set, and missing/junk values can never assemble exactly
    # {"yes", "no"} — both fall through to the side-blind comparison.
    if {close_side, entry_side} == {"yes", "no"}:
        effective_close = round(1.0 - close_price, 4)
        flipped = True
    if abs(entry_price - effective_close) >= REOPEN_PRICE_DELTA:
        return None
    denomination = (
        f" ({entry_side.upper()} terms; ${close_price:.2f}"
        f" {close_side.upper()})" if flipped else ""
    )
    return (
        f"Reopen churn gate (#661): this ticker was closed"
        f" {age_minutes:.0f}m ago at ${effective_close:.2f}"
        f"{denomination} and this order"
        f" would re-enter at ${entry_price:.2f} — a same-price round"
        f" trip repeats the KXGDP-26JUL30-T3.0 anti-pattern (fees for"
        f" zero gross). Re-entry within {REOPEN_LOCKOUT_MINUTES}m"
        f" requires a material price move or fresh post-close"
        f" research; pass --force-reopen ONLY with explicit"
        f" justification."
    )


def check_roundtrip_churn(
    *,
    open_timestamp: str,
    now: datetime.datetime | None = None,
) -> str | None:
    """Warning when a close completes a round trip under the churn
    window, else None. Closing is NEVER blocked — this only makes the
    churn visible (#661). ``now`` must be timezone-aware.
    """
    age_minutes = _minutes_since(open_timestamp, now)
    if age_minutes is None or age_minutes < 0:
        return None
    if age_minutes > ROUNDTRIP_WARN_MINUTES:
        return None
    return (
        f"Round-trip churn (#661): closing a position opened only"
        f" {age_minutes:.0f}m ago — open/close inside"
        f" {ROUNDTRIP_WARN_MINUTES}m burns fees for little movement."
        f" Recorded for the Pro agent's churn audit."
    )
