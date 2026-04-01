"""Pre-trade validation — all risk checks in one place."""

from __future__ import annotations

from dataclasses import dataclass, field

from gimmes.config import GimmesConfig
from gimmes.models.market import Market
from gimmes.risk.limits import (
    check_bankroll,
    check_daily_loss,
    check_position_count,
    check_position_size,
)
from gimmes.risk.settlement import scan_settlement_rules
from gimmes.strategy.fees import DEFAULT_FEE_MULTIPLIERS, FeeMultipliers, edge_after_fees
from gimmes.strategy.scanner import effective_price


@dataclass
class ValidationResult:
    """Result of pre-trade validation."""

    approved: bool
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.approved:
            return f"APPROVED — {len(self.checks)} checks passed"
        return f"REJECTED — {len(self.failures)} failure(s): {'; '.join(self.failures)}"


def validate_trade(
    market: Market,
    trade_dollars: float,
    true_probability: float | None,
    bankroll: float,
    daily_pnl: float,
    open_position_count: int,
    existing_tickers: list[str],
    config: GimmesConfig,
    *,
    is_taker: bool = False,
    deployed_cost_basis: float = 0.0,
    fees: FeeMultipliers = DEFAULT_FEE_MULTIPLIERS,
    size_up: bool = False,
    existing_cost_basis: float = 0.0,
) -> ValidationResult:
    """Run all pre-trade validation checks.

    Checks:
    1. Daily loss limit
    2. Position count limit
    3. Single position size limit
    4. Balance sufficient
    4b. Bankroll cap (callers must track and pass deployed_cost_basis)
    5. Minimum true probability gate (skipped when true_probability is None)
    6. Edge after fees meets minimum (skipped when true_probability is None)
    7. Duplicate position check
    8. Settlement risk
    """
    checks: list[str] = []
    failures: list[str] = []

    # 1. Daily loss limit
    loss_check = check_daily_loss(daily_pnl, bankroll, config)
    if loss_check.passed:
        checks.append("Daily loss limit OK")
    else:
        failures.append(loss_check.reason)

    # 2. Position count (skipped for SIZE UP — not adding a new position)
    if size_up:
        checks.append("Position count check skipped (SIZE UP, no new position)")
    else:
        count_check = check_position_count(open_position_count, config)
        if count_check.passed:
            max_pos = config.risk.max_open_positions
            checks.append(f"Position count OK ({open_position_count}/{max_pos})")
        else:
            failures.append(count_check.reason)

    # 3. Position size (aggregate for SIZE UP)
    cost_basis = max(0.0, existing_cost_basis)
    size_dollars = trade_dollars + cost_basis if size_up else trade_dollars
    size_check = check_position_size(size_dollars, bankroll, config)
    if size_check.passed:
        if size_up and cost_basis > 0:
            checks.append(
                f"Position size OK (${trade_dollars:.2f} new"
                f" + ${cost_basis:.2f} existing"
                f" = ${size_dollars:.2f})"
            )
        else:
            checks.append(f"Position size OK (${trade_dollars:.2f})")
    else:
        reason = size_check.reason
        if size_up and cost_basis > 0:
            reason += (
                f" — ${trade_dollars:.2f} new"
                f" + ${cost_basis:.2f} existing"
            )
        failures.append(reason)

    # 4. Balance check
    if trade_dollars <= bankroll:
        checks.append(f"Balance sufficient (${bankroll:.2f})")
    else:
        failures.append(f"Insufficient balance: need ${trade_dollars:.2f}, have ${bankroll:.2f}")

    # 4b. Bankroll check (total cost basis + proposed trade vs bankroll)
    bankroll_check = check_bankroll(deployed_cost_basis, trade_dollars, config)
    if bankroll_check.passed:
        checks.append(f"Bankroll OK (${deployed_cost_basis + trade_dollars:.2f}/${bankroll:.2f})")
    else:
        failures.append(bankroll_check.reason)

    # 5. Minimum true probability gate (skipped when probability is unknown)
    if true_probability is not None:
        min_prob = config.strategy.min_true_probability
        if true_probability >= min_prob:
            checks.append(f"True probability OK ({true_probability:.0%} >= {min_prob:.0%})")
        else:
            failures.append(
                f"True probability too low: {true_probability:.0%} < {min_prob:.0%} minimum"
            )
    else:
        checks.append("True probability check skipped (no probability provided)")

    # 6. Edge after fees (skipped when probability is unknown)
    if true_probability is not None:
        raw_price = market.midpoint if market.midpoint > 0 else market.last_price
        price = effective_price(raw_price, config.strategy.side)
        edge = edge_after_fees(price, true_probability, is_taker=is_taker, fees=fees)
        min_edge = config.strategy.min_edge_after_fees
        if edge >= min_edge:
            checks.append(f"Edge OK ({edge:.1%} >= {min_edge:.1%})")
        else:
            failures.append(f"Insufficient edge: {edge:.1%} < {min_edge:.1%} minimum")
    else:
        checks.append("Edge check skipped (no probability provided)")

    # 7. Duplicate check (skipped for SIZE UP — adding to existing position)
    if market.ticker in existing_tickers:
        if size_up:
            checks.append(f"Duplicate check skipped (SIZE UP for {market.ticker})")
        else:
            failures.append(f"Already have position in {market.ticker}")
    else:
        if size_up:
            failures.append(f"SIZE UP requested but no existing position in {market.ticker}")
        else:
            checks.append("No duplicate position")

    # 8. Settlement risk
    settlement = scan_settlement_rules(market.rules_primary)
    if settlement.is_clear:
        checks.append("Settlement rules clear")
    elif settlement.risk_level == "high":
        failures.append(f"Settlement risk HIGH: {settlement.summary}")
    else:
        checks.append(f"Settlement risk {settlement.risk_level} (proceed with caution)")

    return ValidationResult(
        approved=len(failures) == 0,
        checks=checks,
        failures=failures,
    )
