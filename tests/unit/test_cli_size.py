"""`gimmes size` must not fabricate sizing rows for bound-priced
markets (#672, deferred from #658).

kelly_fraction's `0 < price < 1` guard misses the one-tick-inside case
(effective $0.01), so pre-fix the table showed a fabricated Edge After
Fees AND nonzero Contracts/Cost for an order that cannot exist. The
command is pure display (no DB writes, no orders) — the fix zeroes the
whole row set behind the same `price_at_bound` gate the validator and
the stored-edge writers use.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

from rich.console import Console
from typer.testing import CliRunner

from gimmes.cli import app
from gimmes.config import GimmesConfig, Mode, StrategyConfig

runner = CliRunner()


def _fake_trading_context(broker):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def _ctx(config):  # type: ignore[no-untyped-def]
        yield AsyncMock(), broker, AsyncMock()

    return _ctx


def _stub_market(yes_price: float):  # type: ignore[no-untyped-def]
    m = MagicMock()
    m.midpoint = yes_price
    m.last_price = yes_price
    m.series_ticker = "TEST"
    return m


def _run_size(yes_price: float, side: str = "no") -> str:
    config = GimmesConfig(
        mode=Mode.DRIVING_RANGE,
        strategy=StrategyConfig(side=side),
    )
    broker = MagicMock()
    broker.get_balance = AsyncMock(return_value=10_000.0)
    buf = StringIO()
    # Wide console: cell content must not ellipsize (the #618 lesson).
    real_console = Console(file=buf, width=200)
    from gimmes.strategy.fee_cache import DEFAULT_FEE_MULTIPLIERS
    mock_fees = DEFAULT_FEE_MULTIPLIERS

    with (
        patch("gimmes.cli.load_config", return_value=config),
        patch("gimmes.cli.trading_context", _fake_trading_context(broker)),
        patch("gimmes.cli.console", real_console),
        patch(
            "gimmes.kalshi.markets.get_market",
            AsyncMock(return_value=_stub_market(yes_price)),
        ),
        patch(
            "gimmes.strategy.fee_cache.get_multipliers",
            MagicMock(return_value=mock_fees),
        ),
    ):
        result = runner.invoke(app, ["size", "KXTEST", "--prob", "0.88"])
    assert result.exit_code == 0, result.output
    return buf.getvalue()


def test_exact_bound_shows_untradeable_and_zero_contracts() -> None:
    # YES $1.00 → NO effective $0.00
    out = _run_size(1.00, side="no")
    assert "price at bound" in out
    contracts_line = next(
        line for line in out.splitlines() if "Contracts" in line
    )
    assert " 0 " in contracts_line
    assert "(at bound)" in contracts_line


def test_one_tick_inside_bound_zeroes_the_table() -> None:
    """YES $0.99 → NO effective $0.01 — the case kelly does NOT guard;
    pre-fix this fabricated nonzero contracts."""
    out = _run_size(0.99, side="no")
    assert "price at bound" in out
    contracts_line = next(
        line for line in out.splitlines() if "Contracts" in line
    )
    assert " 0 " in contracts_line
    kelly_line = next(
        line for line in out.splitlines() if "Kelly Fraction" in line
    )
    assert "0.0000" in kelly_line


def test_mid_range_market_unchanged() -> None:
    """Control: a normally-priced market keeps real sizing output."""
    import re

    out = _run_size(0.70, side="no")  # NO effective $0.30
    assert "price at bound" not in out
    contracts_line = next(
        line for line in out.splitlines() if "Contracts" in line
    )
    # 0.88 prob at $0.30 effective sizes a real position.
    contracts = int(re.search(r"(\d+)", contracts_line.split("Contracts")[1]).group(1))
    assert contracts > 0
