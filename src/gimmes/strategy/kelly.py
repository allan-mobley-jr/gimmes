"""Fractional Kelly criterion with fee adjustment for position sizing."""

from __future__ import annotations

from gimmes.strategy.fees import fee_for_order


def kelly_fraction(
    market_price: float,
    true_probability: float,
    *,
    is_taker: bool = False,
    fraction: float = 0.25,
) -> float:
    """Calculate fractional Kelly bet size as fraction of bankroll.

    Formula:
        effective_cost = price + fee_per_contract
        effective_odds_b = (1 - price - fee) / (price + fee)
        full_kelly = (b * p_true - q) / b
        position = fraction * full_kelly

    Args:
        market_price: Current YES price (0-1).
        true_probability: Our estimated true probability (0-1).
        is_taker: Whether this is a taker order.
        fraction: Kelly fraction (default 0.25 = quarter Kelly).

    Returns:
        Fraction of bankroll to bet (0 to ~1). Negative means no bet.
    """
    if not (0 < market_price < 1) or not (0 < true_probability <= 1):
        return 0.0

    fee = fee_for_order(1, market_price, is_taker=is_taker)
    effective_cost = market_price + fee

    if effective_cost >= 1.0:
        return 0.0

    # Effective odds (b in Kelly formula)
    b = (1.0 - effective_cost) / effective_cost
    if b <= 0:
        return 0.0

    p = true_probability
    q = 1.0 - p

    full_kelly = (b * p - q) / b
    if full_kelly <= 0:
        return 0.0

    return fraction * full_kelly


def position_size(
    bankroll: float,
    market_price: float,
    true_probability: float,
    *,
    is_taker: bool = False,
    fraction: float = 0.25,
    max_position_pct: float = 0.05,
    max_position_dollars: float | None = None,
) -> int:
    """Calculate number of contracts to buy.

    Applies Kelly sizing clamped by risk limits.

    Returns:
        Number of contracts (integer, minimum 0).
    """
    if bankroll <= 0:
        return 0

    kelly = kelly_fraction(market_price, true_probability, is_taker=is_taker, fraction=fraction)
    if kelly <= 0:
        return 0

    # Dollar amount from Kelly
    kelly_dollars = kelly * bankroll

    # Clamp by max position percent
    max_from_pct = max_position_pct * bankroll

    # Clamp by absolute dollar limit
    max_dollars = min(kelly_dollars, max_from_pct)
    if max_position_dollars is not None:
        max_dollars = min(max_dollars, max_position_dollars)

    # Convert to contracts
    if market_price <= 0:
        return 0
    contracts = int(max_dollars / market_price)
    return max(contracts, 0)
