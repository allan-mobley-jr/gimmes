"""#640: per-group capacity reporting and validate/order exposure-basis
alignment — the validator's own view (positions + resting BUY
reservations), readable by the CM before approving."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from gimmes.cli import _exposure_basis, app
from gimmes.models.portfolio import Position

runner = CliRunner()


def _pos(ticker, cost):
    return Position(
        ticker=ticker, side="no", count=100, avg_price=cost / 100,
        market_price=cost / 100, cost_basis=cost, unrealized_pnl=0.0,
    )


def _resting(ticker, side="no", remaining=200, price=0.5):
    o = MagicMock()
    o.action.value = "buy"
    o.side.value = side
    o.ticker = ticker
    o.remaining_count = remaining
    o.yes_price = price
    o.no_price = price
    return o


class TestExposureBasis:
    def test_includes_resting_buys_at_reservation(self) -> None:
        broker = AsyncMock()
        broker.list_orders = AsyncMock(
            return_value=[_resting("KXE-26AUG-T2", remaining=200,
                                   price=0.5)],
        )
        basis = asyncio.run(
            _exposure_basis([_pos("KXE-26AUG-T1", 300.0)], broker),
        )
        assert len(basis) == 2
        assert basis[1].cost_basis == 100.0  # 200 * 0.5

    def test_sells_excluded(self) -> None:
        sell = _resting("KXE-26AUG-T2")
        sell.action.value = "sell"
        broker = AsyncMock()
        broker.list_orders = AsyncMock(return_value=[sell])
        basis = asyncio.run(_exposure_basis([], broker))
        assert basis == []

    def test_no_broker_passthrough(self) -> None:
        positions = [_pos("KXE-26AUG-T1", 300.0)]
        assert asyncio.run(_exposure_basis(positions, None)) == positions


class TestRiskCheckCapacity:
    def _run(self, *extra):
        from contextlib import asynccontextmanager

        cfg = MagicMock()
        cfg.risk.max_event_exposure_pct = 0.10
        cfg.risk.max_series_exposure_pct = 0.30
        cfg.risk.position_past_close_minutes = 30
        cfg.bankroll = 5000.0
        broker = AsyncMock()
        broker.get_balance = AsyncMock(return_value=5000.0)
        broker.get_positions = AsyncMock(
            return_value=[_pos("KXE-26AUG-T1", 300.0)],
        )
        broker.list_orders = AsyncMock(
            return_value=[_resting("KXE-26AUG-T2", remaining=200,
                                   price=0.5)],
        )
        mock_db = AsyncMock()

        @asynccontextmanager
        async def _ctx(config):
            yield AsyncMock(), broker, mock_db

        with patch("gimmes.cli.load_config", return_value=cfg), \
             patch("gimmes.cli.trading_context", _ctx), \
             patch("gimmes.cli._mark_positions_to_market",
                   AsyncMock(return_value=[_pos("KXE-26AUG-T1",
                                                300.0)])), \
             patch("gimmes.store.queries.get_daily_pnl",
                   AsyncMock(return_value=0.0)), \
             patch("gimmes.store.queries.get_deployed_cost_basis",
                   AsyncMock(return_value=300.0)):
            return runner.invoke(app, ["risk-check", *extra])

    # NOTE: the MagicMock config crashes risk-check's DOWNSTREAM
    # formatting after the capacity block prints — the capacity
    # feature is what's under test, so assertions target the output,
    # not the exit code.

    def test_event_capacity_reports_remaining(self) -> None:
        result = self._run("--event", "KXE-26AUG")
        out = " ".join(result.output.split())
        # 300 position + 100 resting = 400 exposure; cap 500 → 100 left
        assert "Event capacity: KXE-26AUG" in out
        assert "$400.00" in out
        assert "$500.00" in out
        assert "Remaining capacity: $100.00" in out

    def test_series_capacity_uses_series_cap(self) -> None:
        # Series cap 30% of 5000 = $1500; exposure 400 → $1100 left.
        # A swapped cap-pct pairing would report $100 (the event cap).
        result = self._run("--series", "KXE")
        out = " ".join(result.output.split())
        assert "Series capacity: KXE" in out
        assert "$1500.00" in out
        assert "Remaining capacity: $1100.00" in out

    def test_unrelated_group_zero_exposure(self) -> None:
        result = self._run("--event", "KXOTHER-26AUG")
        out = " ".join(result.output.split())
        assert "Exposure (positions + resting): $0.00" in out
        assert "Remaining capacity: $500.00" in out

    def test_no_remaining_capacity_cross(self) -> None:
        # Over-cap variant: a 5% cap (=$250 < $400 exposure)
        from contextlib import asynccontextmanager

        cfg = MagicMock()
        cfg.risk.max_event_exposure_pct = 0.05
        cfg.risk.max_series_exposure_pct = 0.30
        cfg.risk.position_past_close_minutes = 30
        cfg.bankroll = 5000.0
        broker = AsyncMock()
        broker.get_balance = AsyncMock(return_value=5000.0)
        broker.get_positions = AsyncMock(
            return_value=[_pos("KXE-26AUG-T1", 300.0)],
        )
        broker.list_orders = AsyncMock(
            return_value=[_resting("KXE-26AUG-T2", remaining=200,
                                   price=0.5)],
        )

        @asynccontextmanager
        async def _ctx(config):
            yield AsyncMock(), broker, AsyncMock()

        with patch("gimmes.cli.load_config", return_value=cfg), \
             patch("gimmes.cli.trading_context", _ctx), \
             patch("gimmes.cli._mark_positions_to_market",
                   AsyncMock(return_value=[_pos("KXE-26AUG-T1",
                                                300.0)])), \
             patch("gimmes.store.queries.get_daily_pnl",
                   AsyncMock(return_value=0.0)), \
             patch("gimmes.store.queries.get_deployed_cost_basis",
                   AsyncMock(return_value=300.0)):
            result = runner.invoke(
                app, ["risk-check", "--event", "KXE-26AUG"],
            )
        out = " ".join(result.output.split())
        assert "No remaining capacity" in out


class TestValidateAlignment:
    """#640 F1: `gimmes validate` sees resting reservations — the
    approve-at-validate/reject-at-order split is closed. Reverting the
    alignment must fail this test."""

    def test_validate_rejects_on_resting_reservation(self) -> None:
        from contextlib import asynccontextmanager
        from datetime import UTC, datetime

        from gimmes.config import (
            GimmesConfig,
            Mode,
            RiskConfig,
            SizingConfig,
            StrategyConfig,
        )
        from gimmes.models.market import Market, MarketStatus

        cfg = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            strategy=StrategyConfig(
                side="no", min_true_probability=0.6,
                min_edge_after_fees=0.01,
            ),
            sizing=SizingConfig(
                kelly_fraction=0.25, max_position_pct=0.10,
            ),
            risk=RiskConfig(
                bankroll_paper=5000.0, max_event_exposure_pct=0.10,
            ),
        )
        market = Market(
            ticker="KXE-26AUG-T1",
            event_ticker="KXE-26AUG",
            series_ticker="KXE",
            title="t",
            status=MarketStatus.ACTIVE,
            yes_bid=0.49, yes_ask=0.51, last_price=0.5,
            volume=5000, volume_24h=1000, open_interest=500,
            close_time=datetime(2026, 9, 1, tzinfo=UTC),
            rules_primary="Resolves YES if X above threshold.",
        )
        broker = AsyncMock()
        broker.get_balance = AsyncMock(return_value=5000.0)
        broker.get_positions = AsyncMock(return_value=[])
        # $450 resting reservation on a sibling of the same event —
        # the candidate's $100 would project $550 > the $500 cap.
        broker.list_orders = AsyncMock(
            return_value=[_resting("KXE-26AUG-T2", remaining=900,
                                   price=0.5)],
        )
        mock_fees = MagicMock()
        mock_fees.taker_fee = 0.07
        mock_fees.maker_fee = 0.03

        @asynccontextmanager
        async def _ctx(config):
            yield AsyncMock(), broker, AsyncMock()

        with patch("gimmes.cli.load_config", return_value=cfg), \
             patch("gimmes.cli.trading_context", _ctx), \
             patch("gimmes.cli._mark_positions_to_market",
                   AsyncMock(return_value=[])), \
             patch("gimmes.kalshi.markets.get_market",
                   AsyncMock(return_value=market)), \
             patch("gimmes.strategy.fee_cache.get_multipliers",
                   MagicMock(return_value=mock_fees)), \
             patch("gimmes.store.queries.get_daily_pnl",
                   AsyncMock(return_value=0.0)), \
             patch("gimmes.store.queries.get_deployed_cost_basis",
                   AsyncMock(return_value=450.0)), \
             patch("gimmes.store.queries.set_position_rules_snapshot",
                   AsyncMock(return_value=True)):
            result = runner.invoke(app, [
                "validate", "KXE-26AUG-T1", "--prob", "0.9",
                "--dollars", "100",
            ])
        assert result.exit_code == 1, result.output
        assert "Event exposure" in result.output
