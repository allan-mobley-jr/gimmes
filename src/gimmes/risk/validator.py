"""Pre-trade validation — all risk checks in one place."""

from __future__ import annotations

from dataclasses import dataclass, field

from gimmes.config import GimmesConfig
from gimmes.models.market import Market
from gimmes.risk.limits import (
    check_daily_loss,
    check_position_count,
    check_position_size,
)
from gimmes.risk.settlement import scan_settlement_rules
from gimmes.strategy.fees import edge_after_fees


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
    session_spent: float = 0.0,
) -> ValidationResult:
    """Run all pre-trade validation checks.

    Checks:
    1. Daily loss limit
    2. Position count limit
    3. Single position size limit
    4. Balance sufficient
    4b. Session spending cap (callers must track and pass session_spent)
    5. Edge after fees meets minimum (skipped when true_probability is None)
    6. Duplicate position check
    7. Settlement risk
    """
    checks: list[str] = []
    failures: list[str] = []

    # 1. Daily loss limit
    loss_check = check_daily_loss(daily_pnl, bankroll, config)
    if loss_check.passed:
        checks.append("Daily loss limit OK")
    else:
        failures.append(loss_check.reason)

    # 2. Position count
    count_check = check_position_count(open_position_count, config)
    if count_check.passed:
        checks.append(f"Position count OK ({open_position_count}/{config.risk.max_open_positions})")
    else:
        failures.append(count_check.reason)

    # 3. Position size
    size_check = check_position_size(trade_dollars, bankroll, config)
    if size_check.passed:
        checks.append(f"Position size OK (${trade_dollars:.2f})")
    else:
        failures.append(size_check.reason)

    # 4. Balance check
    if trade_dollars <= bankroll:
        checks.append(f"Balance sufficient (${bankroll:.2f})")
    else:
        failures.append(f"Insufficient balance: need ${trade_dollars:.2f}, have ${bankroll:.2f}")

    # 4b. Session spending cap (championship mode guard)
    cap = config.risk.session_spending_cap
    if cap > 0 and session_spent + trade_dollars > cap:
        failures.append(
            f"Session spending cap exceeded: "
            f"${session_spent:.2f} spent + ${trade_dollars:.2f} "
            f"> ${cap:.2f} cap"
        )
    elif cap > 0:
        checks.append(f"Session spending OK (${session_spent + trade_dollars:.2f}/${cap:.2f})")

    # 5. Edge after fees (skipped when probability is unknown)
    if true_probability is not None:
        price = market.midpoint if market.midpoint > 0 else market.last_price
        edge = edge_after_fees(price, true_probability, is_taker=is_taker)
        min_edge = config.strategy.min_edge_after_fees
        if edge >= min_edge:
            checks.append(f"Edge OK ({edge:.1%} >= {min_edge:.1%})")
        else:
            failures.append(f"Insufficient edge: {edge:.1%} < {min_edge:.1%} minimum")
    else:
        checks.append("Edge check skipped (no probability provided)")

    # 6. Duplicate check
    if market.ticker in existing_tickers:
        failures.append(f"Already have position in {market.ticker}")
    else:
        checks.append("No duplicate position")

    # 7. Settlement risk
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
