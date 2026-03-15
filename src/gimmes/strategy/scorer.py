"""Gimme scorer — evaluates markets for gimme potential."""

from __future__ import annotations

from gimmes.config import GimmesConfig
from gimmes.models.gimme import GimmeCandidate, GimmeScore
from gimmes.models.market import Market, Orderbook
from gimmes.strategy.fees import DEFAULT_FEE_MULTIPLIERS, FeeMultipliers, edge_after_fees
from gimmes.strategy.scanner import days_until


def quick_score(market: Market, config: GimmesConfig) -> float:
    """Quick score for Scout scanning (0-100). No research inputs needed.

    Evaluates based on readily available market data:
    - Liquidity depth (volume, open interest, spread)
    - Volume trend (24h volume vs total)
    - Price position in target range (closer to center = better)
    """
    score = 0.0
    price = market.midpoint if market.midpoint > 0 else market.last_price

    # Volume score (0-30): higher volume = better
    vol = market.volume_24h if market.volume_24h > 0 else market.volume
    if vol >= 10000:
        score += 30
    elif vol >= 1000:
        score += 20
    elif vol >= 500:
        score += 15
    elif vol >= 100:
        score += 10

    # Open interest score (0-20)
    oi = market.open_interest
    if oi >= 5000:
        score += 20
    elif oi >= 1000:
        score += 15
    elif oi >= 200:
        score += 10
    elif oi >= 50:
        score += 5

    # Spread score (0-20): tighter spread = better
    spread = market.spread
    if 0 < spread <= 0.02:
        score += 20
    elif spread <= 0.05:
        score += 15
    elif spread <= 0.10:
        score += 10

    # Price position score (0-15): sweet spot 60-80c
    if 0.60 <= price <= 0.80:
        score += 15
    elif 0.55 <= price <= 0.85:
        score += 10

    # Settlement clarity placeholder (0-15): defaults to neutral
    score += 10

    return min(score, 100.0)


def full_score(
    candidate: GimmeCandidate,
    orderbook: Orderbook | None,
    config: GimmesConfig,
    *,
    market: Market | None = None,
    fees: FeeMultipliers = DEFAULT_FEE_MULTIPLIERS,
) -> GimmeScore:
    """Full gimme scoring with research inputs (0-100).

    Requires model probability, confidence signals, and research memo from Caddie.

    Scoring weights from config:
    - Edge size (30%)
    - Signal strength (25%)
    - Liquidity depth (15%)
    - Settlement clarity (15%)
    - Time to resolution (15%)
    """
    weights = config.scoring.weights
    max_per = 100.0  # Each component scored 0-100, then weighted

    # Edge size score (0-100)
    edge = edge_after_fees(candidate.market_price, candidate.model_probability, fees=fees)
    if edge >= 0.25:
        edge_score = 100.0
    elif edge >= 0.15:
        edge_score = 80.0
    elif edge >= 0.10:
        edge_score = 60.0
    elif edge >= 0.05:
        edge_score = 40.0
    elif edge > 0:
        edge_score = 20.0
    else:
        edge_score = 0.0

    # Signal strength score (0-100)
    signals = candidate.signals
    if len(signals) >= 4:
        signal_score = 90.0
    elif len(signals) >= 3:
        signal_score = 70.0
    elif len(signals) >= 2:
        signal_score = 50.0
    elif len(signals) >= 1:
        signal_score = 25.0
    else:
        signal_score = 0.0
    # Adjust by average signal strength
    if signals:
        avg_strength = sum(s.strength for s in signals) / len(signals)
        signal_score *= avg_strength

    # Liquidity depth score (0-100)
    liq_score = 50.0  # Default neutral
    if orderbook:
        depth = orderbook.depth_at_price(candidate.market_price, "yes")
        if depth >= 500:
            liq_score = 100.0
        elif depth >= 200:
            liq_score = 80.0
        elif depth >= 50:
            liq_score = 60.0
        elif depth >= 10:
            liq_score = 30.0
        else:
            liq_score = 10.0

    # Settlement clarity score (0-100)
    # Penalize if research memo flags settlement concerns
    settlement_score = 80.0  # Default: assume clear
    memo_lower = candidate.research_memo.lower()
    red_flags = ["carveout", "carve-out", "discretion", "subjective", "ambiguous", "unclear"]
    flag_count = sum(1 for flag in red_flags if flag in memo_lower)
    if flag_count >= 3:
        settlement_score = 10.0
    elif flag_count >= 2:
        settlement_score = 30.0
    elif flag_count >= 1:
        settlement_score = 50.0

    # Time to resolution score (0-100) — sweet spot is 1-14 days
    time_score = 30.0  # Conservative default when no time info
    if market:
        days = days_until(market.close_time)
        if days is None:
            days = days_until(market.expiration_time)
        if days is not None:
            if days < 1:
                time_score = 20.0  # Too soon — limited time to enter/exit
            elif days <= 14:
                time_score = 100.0  # Sweet spot
            elif days <= 30:
                time_score = 70.0
            elif days <= 60:
                time_score = 40.0
            else:
                time_score = 15.0  # Very long-dated

    # Calculate weighted total
    total = (
        edge_score * weights.edge_size
        + signal_score * weights.signal_strength
        + liq_score * weights.liquidity_depth
        + settlement_score * weights.settlement_clarity
        + time_score * weights.time_to_resolution
    )

    return GimmeScore(
        total=min(total, max_per),
        edge_size_score=edge_score,
        signal_strength_score=signal_score,
        liquidity_depth_score=liq_score,
        settlement_clarity_score=settlement_score,
        time_to_resolution_score=time_score,
        memo=candidate.research_memo,
    )
