"""Kalshi portfolio endpoints: balance, positions, settlements."""

from __future__ import annotations

from gimmes.kalshi.client import KalshiClient
from gimmes.models.portfolio import Position


def _parse_position(data: dict) -> Position:  # type: ignore[type-arg]
    """Parse a position from Kalshi API response."""
    # API returns dollar strings (e.g. "0.0000") and fp counts (e.g. "3.00")
    ticker = data.get("ticker", data.get("market_ticker", ""))
    count = int(float(data.get("position_fp", data.get("position", "0"))))
    # Positive = YES position, negative = NO position
    side = "yes" if count >= 0 else "no"
    abs_count = abs(count)

    market_value = float(data.get("market_exposure_dollars", "0"))
    realized_pnl = float(data.get("realized_pnl_dollars", "0"))
    total_traded = float(data.get("total_traded_dollars", "0"))
    fees_paid = float(data.get("fees_paid_dollars", "0"))

    # CAUTION (#674): total_traded_dollars and fees_paid_dollars are
    # CUMULATIVE lifetime figures for the market ("Total spent on this
    # market" per the API docs), and market_exposure_dollars is
    # doc-ambiguous ("cost of the aggregate market position" — used
    # here as market value, consistent with observed payloads). So
    # this cost_basis is only trustworthy while realized_pnl == 0: a
    # partial close inflates it, corrupting avg_price and the StopGate
    # denominator. The positions command flags such rows BASIS-SUSPECT.
    # TODO(#674): reconstruct open cost from GET /portfolio/fills
    # before scaling live capital.
    cost_basis = total_traded + fees_paid
    unrealized_pnl = (market_value - cost_basis) if abs_count > 0 else 0.0
    avg_price = cost_basis / abs_count if abs_count > 0 else 0.0
    market_price = market_value / abs_count if abs_count > 0 else 0.0

    return Position(
        ticker=ticker,
        side=side,
        count=abs_count,
        avg_price=avg_price,
        market_price=market_price,
        cost_basis=cost_basis,
        market_value=market_value,
        unrealized_pnl=unrealized_pnl,
        realized_pnl=realized_pnl,
    )


async def get_balance(client: KalshiClient) -> float:
    """Get current account balance in dollars."""
    data = await client.get("/portfolio/balance")
    # Balance is returned in cents
    return data.get("balance", 0) / 100


async def get_positions(
    client: KalshiClient,
    *,
    settlement_status: str = "unsettled",
    limit: int = 200,
    cursor: str | None = None,
) -> tuple[list[Position], str | None]:
    """Get current positions."""
    params: dict[str, str | int] = {
        "settlement_status": settlement_status,
        "limit": limit,
    }
    if cursor:
        params["cursor"] = cursor

    data = await client.get("/portfolio/positions", params=params)
    positions = [_parse_position(p) for p in data.get("market_positions", [])]
    next_cursor = data.get("cursor")
    return positions, next_cursor


async def get_all_positions(
    client: KalshiClient,
    settlement_status: str = "unsettled",
) -> list[Position]:
    """Fetch all positions, handling pagination."""
    all_positions: list[Position] = []
    cursor: str | None = None
    max_pages = 50

    for _ in range(max_pages):
        positions, cursor = await get_positions(
            client, settlement_status=settlement_status, cursor=cursor
        )
        all_positions.extend(positions)
        if not cursor or not positions:
            break
    else:
        import logging
        logging.getLogger(__name__).warning(
            "Pagination limit reached (%d pages, %d positions)",
            max_pages, len(all_positions),
        )

    return all_positions


async def get_settlements(
    client: KalshiClient,
    *,
    limit: int = 100,
    cursor: str | None = None,
) -> tuple[list[dict], str | None]:  # type: ignore[type-arg]
    """Get settlement history."""
    params: dict[str, str | int] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor

    data = await client.get("/portfolio/settlements", params=params)
    settlements = data.get("settlements", [])
    next_cursor = data.get("cursor")
    return settlements, next_cursor


async def get_settlements_for_tickers(
    client: KalshiClient,
    tickers: set[str],
    *,
    max_pages: int = 10,
    lookback_days: int = 30,
) -> dict[str, dict]:  # type: ignore[type-arg]
    """Newest settlement record per requested ticker (#663).

    The settlements endpoint returns account-lifetime records, newest
    first (live-probed schema: ticker, market_result 'yes'|'no',
    settled_time ISO-Z, yes/no_count_fp strings). Three stop
    conditions bound the walk: every requested ticker matched, a
    page's oldest settled_time older than the lookback window, or the
    max_pages cap (the get_all_positions defensive pattern). Newest
    record wins per ticker.
    """
    from datetime import UTC, datetime, timedelta

    if not tickers:
        return {}
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    found: dict[str, dict] = {}
    cursor: str | None = None
    for _ in range(max_pages):
        settlements, cursor = await get_settlements(
            client, limit=200, cursor=cursor,
        )
        if not settlements:
            break
        oldest_in_page: datetime | None = None
        for rec in settlements:
            ticker = rec.get("ticker", "")
            settled_raw = str(rec.get("settled_time", ""))
            try:
                settled = datetime.fromisoformat(
                    settled_raw.replace("Z", "+00:00"),
                )
            except ValueError:
                settled = None
            if settled is not None and (
                oldest_in_page is None or settled < oldest_in_page
            ):
                oldest_in_page = settled
            if ticker in tickers and ticker not in found:
                found[ticker] = rec
        if tickers <= found.keys():
            break
        if oldest_in_page is not None and oldest_in_page < cutoff:
            break
        if not cursor:
            break
    else:
        missing = tickers - found.keys()
        if missing:
            import logging
            logging.getLogger(__name__).warning(
                "settlements pagination cap (%d pages) hit with %d"
                " unmatched ticker(s): %s — they fall back to"
                " reconcile drift (#663)",
                max_pages, len(missing), sorted(missing),
            )
    return found
