"""Kalshi fee calculator.

Fee formula: round_up(multiplier * contracts * price * (1 - price))
- Taker multiplier: 0.07
- Maker multiplier: 0.0175 (75% cheaper)

Prices are in dollars (0.01 to 0.99). Fees are in dollars.
"""

from __future__ import annotations

import math

TAKER_MULTIPLIER = 0.07
MAKER_MULTIPLIER = 0.0175


def _round_up_cents(value: float) -> float:
    """Round up to the nearest cent."""
    return math.ceil(value * 100) / 100


def taker_fee(contracts: int, price: float, multiplier: float = TAKER_MULTIPLIER) -> float:
    """Calculate taker fee for a trade.

    Args:
        contracts: Number of contracts.
        price: Contract price in dollars (0.01-0.99).
        multiplier: Fee multiplier (default 0.07, use 0.035 for index markets).

    Returns:
        Fee in dollars, rounded up to nearest cent.
    """
    if contracts <= 0 or not (0.0 < price < 1.0):
        return 0.0
    raw = multiplier * contracts * price * (1 - price)
    return _round_up_cents(raw)


def maker_fee(contracts: int, price: float) -> float:
    """Calculate maker fee for a trade.

    Args:
        contracts: Number of contracts.
        price: Contract price in dollars (0.01-0.99).

    Returns:
        Fee in dollars, rounded up to nearest cent.
    """
    if contracts <= 0 or not (0.0 < price < 1.0):
        return 0.0
    raw = MAKER_MULTIPLIER * contracts * price * (1 - price)
    return _round_up_cents(raw)


def fee_for_order(
    contracts: int, price: float, is_taker: bool = False
) -> float:
    """Calculate fee based on order type."""
    if is_taker:
        return taker_fee(contracts, price)
    return maker_fee(contracts, price)


def edge_after_fees(
    market_price: float,
    true_probability: float,
    contracts: int = 1,
    is_taker: bool = False,
) -> float:
    """Calculate the effective edge after fees.

    Returns:
        Edge in percentage points (e.g., 0.10 = 10pp).
    """
    fee = fee_for_order(contracts, market_price, is_taker=is_taker)
    fee_per_contract = fee / contracts if contracts > 0 else 0
    effective_cost = market_price + fee_per_contract
    # Edge = true_probability - effective_cost
    return true_probability - effective_cost


def break_even_probability(price: float, is_taker: bool = False) -> float:
    """Minimum true probability needed to break even at this price."""
    fee = fee_for_order(1, price, is_taker=is_taker)
    return price + fee
