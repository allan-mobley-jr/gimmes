"""Strategy advisor: data-backed analysis and parameter recommendations."""

from __future__ import annotations

import json

from gimmes.config import GimmesConfig
from gimmes.models.recommendation import (
    AnalysisType,
    Confidence,
    Recommendation,
)


def _open_trades(trades: list[dict]) -> list[dict]:  # type: ignore[type-arg]
    """Filter to open trades only."""
    return [t for t in trades if t.get("action") == "open"]


def _pair_closes(trades: list[dict]) -> list[dict]:  # type: ignore[type-arg]
    """Pair each close row to its opens and classify it won/lost.

    Close rows carry no reliable outcome signal of their own (#656):
    synthetic closes (settlement, reconcile) historically defaulted
    edge/score to 0, and even entry edge copied onto a close says
    nothing about how the trade resolved — opens are min_edge-gated,
    so close-row ``edge > 0`` would classify every trade a win.

    Mirrors the ``calculate_pnl`` walk (reporting/pnl.py): group
    ``open``/``size_up``/``close`` by (ticker, side), scan in timestamp
    order with a running weighted-average cost, and reprice reconcile
    drift closes at settlement value when the group's resolution is
    known (#653). ``won`` is resolution-first (side == resolved_outcome
    anywhere in the group), falling back to the realized return's sign.
    Orphan closes (no matched open on record) are dropped — they have
    no entry decision to learn from.

    Returns one record per matched close: ``{ticker, side, timestamp,
    won, realized_return, entry_score, entry_price}`` where
    ``realized_return`` is per-contract (close price − avg cost) and
    entry fields come from the most recent open/size_up before the
    close.
    """
    groups: dict[tuple[str, str], list[dict]] = {}  # type: ignore[type-arg]
    for t in trades:
        if t.get("action") not in ("open", "close", "size_up"):
            continue
        key = (t.get("ticker", ""), t.get("side", "yes"))
        groups.setdefault(key, []).append(t)

    paired: list[dict] = []  # type: ignore[type-arg]
    for (ticker, side), events in groups.items():
        events.sort(key=lambda e: str(e.get("timestamp", "")))
        group_outcome = next(
            (
                e.get("resolved_outcome")
                for e in events
                if e.get("resolved_outcome") in ("yes", "no")
            ),
            None,
        )
        # #663: mirror calculate_pnl — drift rows in groups that carry
        # a real settlement close keep their mark (manual exits).
        group_has_settlement = any(
            e.get("agent") == "settlement" and e.get("action") == "close"
            for e in events
        )
        remaining = 0
        avg_cost = 0.0
        entry_score = 0.0
        entry_price = 0.0
        for e in events:
            action = e.get("action")
            count = int(e.get("count", 0) or 0)
            price = float(e.get("price", 0.0) or 0.0)
            if count <= 0:
                continue
            if action in ("open", "size_up"):
                total = remaining + count
                avg_cost = (
                    (avg_cost * remaining + price * count) / total
                    if total
                    else 0.0
                )
                remaining = total
                entry_score = float(e.get("gimme_score", 0) or 0)
                entry_price = price
                continue
            # action == "close"
            matched = min(count, remaining)
            remaining -= matched
            if matched <= 0:
                continue
            if (
                e.get("agent") == "reconcile"
                and group_outcome is not None
                and not group_has_settlement
            ):
                price = 1.0 if side == group_outcome else 0.0
            realized = price - avg_cost
            won = (
                side == group_outcome
                if group_outcome is not None
                else realized > 0
            )
            paired.append({
                "ticker": ticker,
                "side": side,
                "timestamp": e.get("timestamp", ""),
                "won": won,
                "realized_return": realized,
                "entry_score": entry_score,
                "entry_price": entry_price,
            })
    return paired


# ---------------------------------------------------------------------------
# Analysis 1: Threshold Sweep
# ---------------------------------------------------------------------------

MIN_TRADES_THRESHOLD = 30


def analyze_threshold_sweep(
    trades: list[dict],  # type: ignore[type-arg]
    config: GimmesConfig,
) -> Recommendation | None:
    """Simulate different gimme_threshold values against historical trades.

    Returns a recommendation if a better threshold is found, else None.
    """
    opens = _open_trades(trades)
    paired = _pair_closes(trades)
    if len(paired) < MIN_TRADES_THRESHOLD:
        return None

    # Build outcome map: ticker -> won (True/False). Resolution-first
    # via _pair_closes — close-row edge is an entry artifact, not an
    # outcome (#656).
    outcomes: dict[str, bool] = {r["ticker"]: r["won"] for r in paired}

    # Build score map from opens
    scored: list[dict] = []  # type: ignore[type-arg]
    for t in opens:
        ticker = t.get("ticker", "")
        score = t.get("gimme_score", 0)
        if ticker in outcomes:
            scored.append({"score": score, "won": outcomes[ticker]})

    if len(scored) < MIN_TRADES_THRESHOLD:
        return None

    current_threshold = config.strategy.gimme_threshold
    best_threshold = current_threshold
    best_wr = -1.0
    best_count = 0
    sweep_data: list[dict] = []  # type: ignore[type-arg]

    for threshold in range(50, 96, 5):
        taken = [s for s in scored if s["score"] >= threshold]
        if len(taken) < 5:
            continue
        wins = sum(1 for s in taken if s["won"])
        win_rate = wins / len(taken)
        sweep_data.append({
            "threshold": threshold,
            "trades_taken": len(taken),
            "wins": wins,
            "win_rate": round(win_rate, 3),
        })
        # Maximize win rate, with tie-breaking by trade count
        if win_rate > best_wr or (win_rate == best_wr and len(taken) > best_count):
            best_wr = win_rate
            best_count = len(taken)
            best_threshold = threshold

    if best_threshold == current_threshold:
        return None

    # Determine confidence
    current_taken = [s for s in scored if s["score"] >= current_threshold]
    best_taken = [s for s in scored if s["score"] >= best_threshold]
    if not current_taken or not best_taken:
        return None

    current_wr = sum(1 for s in current_taken if s["won"]) / len(current_taken)
    best_wr = sum(1 for s in best_taken if s["won"]) / len(best_taken)
    improvement = best_wr - current_wr

    if improvement < 0.02:
        return None

    confidence = Confidence.LOW
    if len(best_taken) >= 20 and improvement >= 0.05:
        confidence = Confidence.HIGH
    elif len(best_taken) >= 10 and improvement >= 0.03:
        confidence = Confidence.MEDIUM

    return Recommendation(
        parameter_path="strategy.gimme_threshold",
        current_value=str(current_threshold),
        recommended_value=str(best_threshold),
        confidence=confidence,
        analysis_type=AnalysisType.THRESHOLD_SWEEP,
        rationale=(
            f"Threshold {best_threshold} would have captured {len(best_taken)} trades "
            f"with {best_wr:.0%} win rate vs current {current_threshold} "
            f"({len(current_taken)} trades, {current_wr:.0%} win rate)."
        ),
        supporting_data=json.dumps(sweep_data),
    )


# ---------------------------------------------------------------------------
# Analysis 2: Edge Decay Detection
# ---------------------------------------------------------------------------

MIN_TRADES_EDGE_DECAY = 30


def analyze_edge_decay(
    trades: list[dict],  # type: ignore[type-arg]
    config: GimmesConfig,
) -> Recommendation | None:
    """Detect if realized edge is shrinking over time.

    Compares the per-contract realized return of the most recent half
    of paired closes to the first half. Close-row `edge` is an entry
    artifact (zeroed on synthetic closes, min_edge-gated otherwise) and
    cannot express realized decay (#656).
    """
    paired = _pair_closes(trades)
    if len(paired) < MIN_TRADES_EDGE_DECAY:
        return None

    # Sort by timestamp ascending
    sorted_trades = sorted(paired, key=lambda r: str(r.get("timestamp", "")))
    edges = [r["realized_return"] for r in sorted_trades]

    mid = len(edges) // 2
    first_half = edges[:mid]
    second_half = edges[mid:]

    avg_first = sum(first_half) / len(first_half) if first_half else 0
    avg_second = sum(second_half) / len(second_half) if second_half else 0

    decay = avg_first - avg_second
    # Guard against false positives: need meaningful positive edge to compare
    if avg_first < 0.01:
        return None
    decay_pct = decay / avg_first

    if decay_pct < 0.15:  # Less than 15% decay — not significant
        return None

    confidence = Confidence.LOW
    if decay_pct >= 0.30 and len(paired) >= 50:
        confidence = Confidence.HIGH
    elif decay_pct >= 0.20:
        confidence = Confidence.MEDIUM

    return Recommendation(
        parameter_path="strategy.min_edge_after_fees",
        current_value=str(config.strategy.min_edge_after_fees),
        recommended_value=str(round(config.strategy.min_edge_after_fees + 0.02, 2)),
        confidence=confidence,
        analysis_type=AnalysisType.EDGE_DECAY,
        rationale=(
            f"Edge is decaying: first half avg {avg_first:.3f}, "
            f"second half avg {avg_second:.3f} ({decay_pct:.0%} decline). "
            f"Consider raising min_edge_after_fees to filter weaker opportunities."
        ),
        supporting_data=json.dumps({
            # Per-contract realized returns since #656 (were entry
            # edges) — keys named for what they now carry.
            "first_half_avg_return": round(avg_first, 4),
            "second_half_avg_return": round(avg_second, 4),
            "decay_pct": round(decay_pct, 3),
            "sample_size": len(paired),
        }),
    )


# ---------------------------------------------------------------------------
# Analysis 3: Scoring Weight Correlation (stub — needs component scores)
# ---------------------------------------------------------------------------

MIN_TRADES_SCORING = 50


def analyze_scoring_correlation(
    trades: list[dict],  # type: ignore[type-arg]
    candidates: list[dict],  # type: ignore[type-arg]
    config: GimmesConfig,
) -> Recommendation | None:
    """Correlate scoring components with trade outcomes.

    Requires individual component scores stored in candidates table
    (see issue #20). Returns None until that data is available.
    """
    # Check if candidates have component score data
    if not candidates:
        return None

    sample = candidates[0]
    has_components = any(
        k in sample for k in ("edge_size_score", "signal_strength_score")
    )
    if not has_components:
        return None  # Data collection enhancement (#20) not yet implemented

    # When component scores are available, compute point-biserial correlation
    # between each component and binary win/loss outcomes.
    # For now, this is a placeholder that returns None.
    return None


# ---------------------------------------------------------------------------
# Analysis 4: Kelly Fraction Optimization
# ---------------------------------------------------------------------------

MIN_TRADES_KELLY = 20


def analyze_kelly_optimization(
    trades: list[dict],  # type: ignore[type-arg]
    config: GimmesConfig,
) -> Recommendation | None:
    """Compute optimal Kelly fraction from realized win rate and payoffs.

    Wins/losses and payoff magnitudes come from per-contract realized
    returns via _pair_closes — entry edge is identical on both legs of
    a trade and would corrupt the payoff ratio (#656).
    """
    paired = _pair_closes(trades)
    if len(paired) < MIN_TRADES_KELLY:
        return None

    wins = [r for r in paired if r["realized_return"] > 0]
    losses = [r for r in paired if r["realized_return"] < 0]

    if not wins or not losses:
        return None

    win_rate = len(wins) / len(paired)
    avg_win = sum(r["realized_return"] for r in wins) / len(wins)
    avg_loss = sum(-r["realized_return"] for r in losses) / len(losses)

    if avg_loss == 0:
        return None

    b = avg_win / avg_loss  # payoff ratio
    p = win_rate
    q = 1 - p

    full_kelly = (b * p - q) / b if b > 0 else 0
    if full_kelly <= 0:
        return None

    # Recommend fractional Kelly (never more than half-Kelly)
    recommended = min(round(full_kelly * 0.5, 2), 0.50)
    current = config.sizing.kelly_fraction

    # Only recommend if meaningfully different (>= 0.05 change)
    if abs(recommended - current) < 0.05:
        return None

    confidence = Confidence.LOW
    if len(paired) >= 50 and abs(recommended - current) >= 0.10:
        confidence = Confidence.HIGH
    elif len(paired) >= 30:
        confidence = Confidence.MEDIUM

    return Recommendation(
        parameter_path="sizing.kelly_fraction",
        current_value=str(current),
        recommended_value=str(recommended),
        confidence=confidence,
        analysis_type=AnalysisType.KELLY_OPTIMIZATION,
        rationale=(
            f"Realized win rate: {win_rate:.0%}, payoff ratio: {b:.2f}:1. "
            f"Full Kelly: {full_kelly:.2f}, recommended half-Kelly: {recommended}. "
            f"Current fraction: {current}."
        ),
        supporting_data=json.dumps({
            "win_rate": round(win_rate, 3),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "payoff_ratio": round(b, 3),
            "full_kelly": round(full_kelly, 3),
            "recommended_fraction": recommended,
            "sample_size": len(paired),
        }),
    )


# ---------------------------------------------------------------------------
# Analysis 5: Scanner Parameter Review
# ---------------------------------------------------------------------------

MIN_TRADES_SCANNER = 30


def analyze_scanner_parameters(
    trades: list[dict],  # type: ignore[type-arg]
    config: GimmesConfig,
) -> Recommendation | None:
    """Analyze price distribution of winners vs losers for price range tuning."""
    opens = _open_trades(trades)
    paired = _pair_closes(trades)
    if len(paired) < MIN_TRADES_SCANNER:
        return None

    # Build outcome map — resolution-first via _pair_closes (#656)
    outcomes: dict[str, bool] = {r["ticker"]: r["won"] for r in paired}

    # Get prices from opens
    winner_prices: list[float] = []
    loser_prices: list[float] = []
    for t in opens:
        ticker = t.get("ticker", "")
        price = t.get("price", 0)
        if ticker in outcomes and price > 0:
            if outcomes[ticker]:
                winner_prices.append(price)
            else:
                loser_prices.append(price)

    if len(winner_prices) < 10 or len(loser_prices) < 5:
        return None

    avg_winner_price = sum(winner_prices) / len(winner_prices)
    avg_loser_price = sum(loser_prices) / len(loser_prices)

    current_min = config.strategy.min_market_price
    current_max = config.strategy.max_market_price

    # Check if winners cluster in a narrower range
    winner_min = min(winner_prices)
    winner_max = max(winner_prices)

    # Only recommend if winners clearly favor a different range
    rec_min = round(max(winner_min - 0.02, 0.50), 2)
    rec_max = round(min(winner_max + 0.02, 0.90), 2)

    if abs(rec_min - current_min) < 0.03 and abs(rec_max - current_max) < 0.03:
        return None

    # Pick the parameter with the bigger suggested change
    if abs(rec_min - current_min) >= abs(rec_max - current_max):
        param, current, recommended = "strategy.min_market_price", current_min, rec_min
    else:
        param, current, recommended = "strategy.max_market_price", current_max, rec_max

    return Recommendation(
        parameter_path=param,
        current_value=str(current),
        recommended_value=str(recommended),
        confidence=Confidence.MEDIUM if len(paired) >= 50 else Confidence.LOW,
        analysis_type=AnalysisType.SCANNER_REVIEW,
        rationale=(
            f"Winners avg price: {avg_winner_price:.2f} (n={len(winner_prices)}), "
            f"losers avg price: {avg_loser_price:.2f} (n={len(loser_prices)}). "
            f"Winner range: {winner_min:.2f}–{winner_max:.2f}."
        ),
        supporting_data=json.dumps({
            "avg_winner_price": round(avg_winner_price, 3),
            "avg_loser_price": round(avg_loser_price, 3),
            "winner_count": len(winner_prices),
            "loser_count": len(loser_prices),
            "winner_range": [round(winner_min, 3), round(winner_max, 3)],
        }),
    )


# ---------------------------------------------------------------------------
# Analysis 6: Missed Opportunity Audit (stub — needs skip data)
# ---------------------------------------------------------------------------

MIN_SKIPS_AUDIT = 20


def analyze_missed_opportunities(
    trades: list[dict],  # type: ignore[type-arg]
    config: GimmesConfig,
) -> Recommendation | None:
    """Check skipped candidates that resolved favorably.

    Requires skip logging to be in place (see issue #20).
    """
    # #657: exclude degenerate skip rows (no probability AND no price
    # recorded) — they carry no signal but inflate the denominator of
    # false_negative_rate and the MIN_SKIPS_AUDIT gate, suppressing
    # legitimate recommendations. Close-workflow skips (no_position,
    # close_failed — cli._NON_ENTRY_REASONS) are excluded outright:
    # a failed close is never a missed ENTRY, whatever analytics the
    # row carries. Both exclusion counts are surfaced in
    # supporting_data so an audit reconciles against the raw table.
    all_skips = [t for t in trades if t.get("action") == "skip"]
    entry_skips = [
        t for t in all_skips
        if t.get("reason", "") not in ("no_position", "close_failed")
    ]
    skips = [
        t for t in entry_skips
        if t.get("model_probability", 0) > 0 or t.get("price", 0) > 0
    ]
    excluded_non_entry = len(all_skips) - len(entry_skips)
    excluded_degenerate = len(entry_skips) - len(skips)
    if len(skips) < MIN_SKIPS_AUDIT:
        return None

    # Count skips that had positive edge (would have won)
    missed_wins = [s for s in skips if s.get("edge", 0) > 0]
    if not missed_wins:
        return None

    false_negative_rate = len(missed_wins) / len(skips)

    if false_negative_rate < 0.20:  # Less than 20% false negatives — acceptable
        return None

    # Check if missed wins had scores just below threshold
    threshold = config.strategy.gimme_threshold
    near_misses = [
        s for s in missed_wins
        if 0 < s.get("gimme_score", 0) < threshold
    ]

    if not near_misses:
        return None

    avg_missed_score = sum(s.get("gimme_score", 0) for s in near_misses) / len(near_misses)
    recommended = max(int(avg_missed_score - 5), 50)

    if recommended >= threshold:
        return None

    return Recommendation(
        parameter_path="strategy.gimme_threshold",
        current_value=str(threshold),
        recommended_value=str(recommended),
        confidence=Confidence.MEDIUM if len(skips) >= 50 else Confidence.LOW,
        analysis_type=AnalysisType.MISSED_OPPORTUNITY,
        rationale=(
            f"False negative rate: {false_negative_rate:.0%} ({len(missed_wins)}/{len(skips)} "
            f"skipped trades would have won). {len(near_misses)} near-misses averaged "
            f"score {avg_missed_score:.0f} (threshold: {threshold})."
        ),
        supporting_data=json.dumps({
            "false_negative_rate": round(false_negative_rate, 3),
            "missed_wins": len(missed_wins),
            "total_skips": len(skips),
            "excluded_degenerate": excluded_degenerate,
            "excluded_non_entry": excluded_non_entry,
            "near_misses": len(near_misses),
            "avg_missed_score": round(avg_missed_score, 1),
        }),
    )


# ---------------------------------------------------------------------------
# Run all analyses
# ---------------------------------------------------------------------------


def run_all_analyses(
    trades: list[dict],  # type: ignore[type-arg]
    candidates: list[dict],  # type: ignore[type-arg]
    config: GimmesConfig,
) -> list[Recommendation]:
    """Run all applicable analyses and return recommendations."""
    results: list[Recommendation] = []

    analyses = [
        lambda: analyze_threshold_sweep(trades, config),
        lambda: analyze_edge_decay(trades, config),
        lambda: analyze_scoring_correlation(trades, candidates, config),
        lambda: analyze_kelly_optimization(trades, config),
        lambda: analyze_scanner_parameters(trades, config),
        lambda: analyze_missed_opportunities(trades, config),
    ]

    for analysis in analyses:
        rec = analysis()
        if rec is not None:
            results.append(rec)

    return results
