"""Pre-trade validation — all risk checks in one place."""

from __future__ import annotations

from dataclasses import dataclass, field

from gimmes.config import GimmesConfig
from gimmes.models.market import Market
from gimmes.risk.limits import (
    check_bankroll,
    check_daily_loss,
    check_event_exposure,
    check_position_count,
    check_position_size,
    check_series_exposure,
)
from gimmes.risk.settlement import scan_settlement_rules
from gimmes.strategy.fees import DEFAULT_FEE_MULTIPLIERS, FeeMultipliers, edge_after_fees
from gimmes.strategy.scanner import effective_price, price_at_bound


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
    event_exposure: float = 0.0,
    series_exposure: float = 0.0,
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

    # 4c. Event concentration limit
    if market.event_ticker:
        evt_check = check_event_exposure(
            event_exposure, trade_dollars, bankroll, config,
        )
        if evt_check.passed:
            checks.append(
                f"Event exposure OK "
                f"(${event_exposure + trade_dollars:.2f}"
                f"/${config.risk.max_event_exposure_pct * bankroll:.2f})"
            )
        else:
            failures.append(evt_check.reason)

    # 4d. Series concentration limit
    if market.series_ticker:
        ser_check = check_series_exposure(
            series_exposure, trade_dollars, bankroll, config,
        )
        if ser_check.passed:
            checks.append(
                f"Series exposure OK "
                f"(${series_exposure + trade_dollars:.2f}"
                f"/${config.risk.max_series_exposure_pct * bankroll:.2f})"
            )
        else:
            failures.append(ser_check.reason)

    # 5. Minimum true probability gate (skipped when probability is unknown).
    #    Hourly tickers are gated on their OWN backtested floor (#721) —
    #    scoped so the global floor is untouched for everything else.
    if true_probability is not None:
        if config.is_hourly_ticker(market.ticker):
            min_prob = config.strategy.hourly_min_true_probability
            ok_label, fail_label = " hourly floor", " hourly minimum"
        else:
            min_prob = config.strategy.min_true_probability
            ok_label, fail_label = "", " minimum"
        if true_probability >= min_prob:
            checks.append(
                f"True probability OK ({true_probability:.0%} >= {min_prob:.0%}{ok_label})"
            )
        else:
            failures.append(
                f"True probability too low: "
                f"{true_probability:.0%} < {min_prob:.0%}{fail_label}"
            )
    else:
        checks.append("True probability check skipped (no probability provided)")

    # 6. Price at bound + edge after fees. The bound rejection is
    # NOT probability-gated (#672 review): the price is known
    # regardless, and a count-only manual order at eff $0.01 is just
    # as unfillable as a probability-carrying one.
    raw_price = market.midpoint if market.midpoint > 0 else market.last_price
    price = effective_price(raw_price, config.strategy.side)
    # #658: within one tick of a bound the edge formula collapses
    # to `prob - 0` — a fabricated "Edge OK (88%)" would wave an
    # unfillable order through the last pre-capital gate. The
    # price can drift to the bound between Caddie research and
    # Closer execution, so the live check must catch it.
    if price_at_bound(price):
        failures.append(
            f"Price at bound: effective price ${price:.2f} is"
            f" untradeable — no realizable edge (#658)"
        )
    elif true_probability is not None:
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
