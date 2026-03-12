"""Risk limits: daily loss, position count, position size."""

from __future__ import annotations

from dataclasses import dataclass

from gimmes.config import GimmesConfig


@dataclass
class RiskLimitCheck:
    """Result of a risk limit check."""

    passed: bool
    reason: str = ""


def check_daily_loss(daily_pnl: float, bankroll: float, config: GimmesConfig) -> RiskLimitCheck:
    """Check if daily loss limit has been breached.

    Limit: 15% of bankroll (configurable).
    """
    limit = config.risk.daily_loss_limit_pct * bankroll
    if daily_pnl < 0 and abs(daily_pnl) >= limit:
        return RiskLimitCheck(
            passed=False,
            reason=f"Daily loss ${abs(daily_pnl):.2f} exceeds limit ${limit:.2f} "
            f"({config.risk.daily_loss_limit_pct:.0%} of ${bankroll:.2f})",
        )
    return RiskLimitCheck(passed=True)


def check_position_count(current_count: int, config: GimmesConfig) -> RiskLimitCheck:
    """Check if max open positions limit would be exceeded."""
    if current_count >= config.risk.max_open_positions:
        return RiskLimitCheck(
            passed=False,
            reason=f"Already at max positions ({current_count}/{config.risk.max_open_positions})",
        )
    return RiskLimitCheck(passed=True)


def check_position_size(
    trade_dollars: float, bankroll: float, config: GimmesConfig
) -> RiskLimitCheck:
    """Check if a single position exceeds max size."""
    max_dollars = config.sizing.max_position_pct * bankroll
    if trade_dollars > max_dollars:
        return RiskLimitCheck(
            passed=False,
            reason=f"Position ${trade_dollars:.2f} exceeds max ${max_dollars:.2f} "
            f"({config.sizing.max_position_pct:.0%} of ${bankroll:.2f})",
        )
    return RiskLimitCheck(passed=True)
