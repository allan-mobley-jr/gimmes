"""Fractional Kelly criterion with fee adjustment for position sizing."""

from __future__ import annotations

from typing import Literal

from gimmes.config import CATEGORY_BASE_RATES
from gimmes.strategy.fees import DEFAULT_FEE_MULTIPLIERS, FeeMultipliers, fee_for_order


def apply_base_rate_floor(
    true_probability: float,
    ticker: str,
    base_rates: dict[str, float] | None = None,
) -> float:
    """Apply category base rate as a floor to the probability estimate.

    Matches the ticker against series prefixes in *base_rates* (longest
    prefix wins).  If the base rate exceeds *true_probability*, returns
    the base rate.  Otherwise returns *true_probability* unchanged.
    """
    if base_rates is None:
        base_rates = CATEGORY_BASE_RATES

    best_rate = 0.0
    best_len = 0
    for prefix, rate in base_rates.items():
        if ticker.startswith(prefix) and len(prefix) > best_len:
            best_rate = rate
            best_len = len(prefix)

    if best_len == 0:
        return true_probability
    return max(true_probability, best_rate)


def kelly_fraction(
    market_price: float,
    true_probability: float,
    *,
    is_taker: bool = False,
    fraction: float = 0.25,
    fees: FeeMultipliers = DEFAULT_FEE_MULTIPLIERS,
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
        fees: Fee multipliers for this series.

    Returns:
        Fraction of bankroll to bet (0 to ~1). Negative means no bet.
    """
    if not (0 < market_price < 1) or not (0 < true_probability <= 1):
        return 0.0

    fee = fee_for_order(1, market_price, is_taker=is_taker, fees=fees)
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


def ev_fraction(
    market_price: float,
    true_probability: float,
    *,
    is_taker: bool = False,
    fraction: float = 0.25,
    fees: FeeMultipliers = DEFAULT_FEE_MULTIPLIERS,
) -> float:
    """EV-based sizing fraction for variance plays.

    Sizes proportionally to the edge-to-cost ratio, scaled by fraction.
    Better than Kelly for moderate-probability variance plays (30-60%)
    where expected value is positive but Kelly produces tiny sizes.

    Returns:
        Fraction of bankroll to bet (>= 0). Returns 0 if EV is non-positive.
    """
    if not (0 < market_price < 1) or not (0 < true_probability <= 1):
        return 0.0

    fee = fee_for_order(1, market_price, is_taker=is_taker, fees=fees)
    cost_per_contract = market_price + fee

    if cost_per_contract >= 1.0:
        return 0.0

    edge_dollars = true_probability - cost_per_contract
    if edge_dollars <= 0:
        return 0.0

    ratio = edge_dollars / cost_per_contract
    return fraction * ratio


def position_size(
    bankroll: float,
    market_price: float,
    true_probability: float,
    *,
    is_taker: bool = False,
    fraction: float = 0.25,
    max_position_pct: float = 0.05,
    max_position_dollars: float | None = None,
    fees: FeeMultipliers = DEFAULT_FEE_MULTIPLIERS,
    mode: Literal["kelly", "ev"] = "kelly",
) -> int:
    """Calculate number of contracts to buy.

    Applies Kelly or EV sizing clamped by risk limits.

    Args:
        mode: "kelly" for Kelly Criterion, "ev" for expected-value sizing.

    Returns:
        Number of contracts (integer, minimum 0).
    """
    if bankroll <= 0:
        return 0

    if mode == "ev":
        frac = ev_fraction(
            market_price, true_probability,
            is_taker=is_taker, fraction=fraction, fees=fees,
        )
    elif mode == "kelly":
        frac = kelly_fraction(
            market_price, true_probability,
            is_taker=is_taker, fraction=fraction, fees=fees,
        )
    else:
        msg = f"Unknown sizing mode: {mode!r}. Expected 'kelly' or 'ev'."
        raise ValueError(msg)
    if frac <= 0:
        return 0

    # Dollar amount from sizing formula
    sized_dollars = frac * bankroll

    # Clamp by max position percent
    max_from_pct = max_position_pct * bankroll

    # Clamp by absolute dollar limit
    max_dollars = min(sized_dollars, max_from_pct)
    if max_position_dollars is not None:
        max_dollars = min(max_dollars, max_position_dollars)

    # Convert to contracts using effective cost (price + fee)
    fee_per = fee_for_order(1, market_price, is_taker=is_taker, fees=fees)
    cost_per_contract = market_price + fee_per
    if cost_per_contract <= 0:
        return 0
    contracts = int(max_dollars / cost_per_contract)
    return max(contracts, 0)
