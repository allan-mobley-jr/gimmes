"""#766: auto-sizing uses the worst-case (cap) price.

With a #743 approval-price cap, the worst-case fill cost is
count × cap (the broker reserves at the limit), but sizing at the live
mid clamped count × mid to the $-cap — so any cap above the mid
deterministically overshot the position/event caps at the order
command's validate_trade call while the Closer's standalone
`gimmes validate` (mid-based, no --price) had approved. Trades
31268/31431/31513. The fix sizes at max(mid, cap); the rejection path
also gains a durable validation_failed error row.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from gimmes.config import (
    GimmesConfig,
    Mode,
    PaperTradingConfig,
    RiskConfig,
    ScannerConfig,
    SizingConfig,
    StrategyConfig,
)
from gimmes.models.market import Market, MarketStatus
from gimmes.risk.validator import validate_trade
from gimmes.strategy.kelly import position_size
from tests.unit import test_order_error_handling as h


def _config() -> GimmesConfig:
    return GimmesConfig(
        mode=Mode.DRIVING_RANGE,
        strategy=StrategyConfig(
            side="no",
            min_true_probability=0.90,
            min_edge_after_fees=0.05,
        ),
        sizing=SizingConfig(kelly_fraction=0.25, max_position_pct=0.1),
        risk=RiskConfig(bankroll_paper=5000.0, max_event_exposure_pct=0.1),
        scanner=ScannerConfig(hourly_series=["KXBTCD"]),
        paper=PaperTradingConfig(starting_balance=5000.0),
    )


def _market(yes_mid: float) -> Market:
    spread = 0.02
    return Market(
        ticker="KXBTCD-26AUG0612-T64799.99",
        event_ticker="KXBTCD-26AUG0612",
        series_ticker="KXBTCD",
        title="BTC hourly",
        status=MarketStatus.ACTIVE,
        yes_bid=yes_mid - spread / 2,
        yes_ask=yes_mid + spread / 2,
        last_price=yes_mid,
        volume=5000,
        volume_24h=1200,
        open_interest=800,
        close_time=datetime(2026, 8, 6, 17, 0, tzinfo=UTC),
        rules_primary="Resolves YES if BTC is above the strike.",
    )


class TestWorstCaseSizing:
    """Real position_size + validate_trade — the incident replays."""

    def test_incident_31268_cap_above_mid_now_approves(self) -> None:
        """Mid $0.66, cap $0.85: sized at the cap, count × cap fits the
        $500 caps and validate_trade approves — the trade executes at a
        compliant size instead of being rejected."""
        cfg = _config()
        eff_mid, cap = 0.66, 0.85
        prob = 0.95
        contracts = position_size(
            5000.0, max(eff_mid, cap), prob,
            is_taker=True, fraction=cfg.sizing.kelly_fraction,
            max_position_pct=cfg.sizing.max_position_pct,
        )
        # A real, near-cap-utilization trade — not a token position
        assert contracts * cap >= 400.0
        assert contracts * cap <= 500.0 + 1e-9

        result = validate_trade(
            _market(1 - eff_mid), contracts * cap, prob, 5000.0,
            0.0, 0, [], cfg, is_taker=True,
        )
        assert result.approved, result.failures

    def test_cap_sized_dollars_hit_event_cap(self) -> None:
        """Cap-sized dollars flow into check_event_exposure: with $250
        already deployed on the event, ~$494 more rejects at the $500
        event cap."""
        cfg = _config()
        eff_mid, cap = 0.66, 0.85
        contracts = position_size(
            5000.0, max(eff_mid, cap), 0.95,
            is_taker=True, fraction=cfg.sizing.kelly_fraction,
            max_position_pct=cfg.sizing.max_position_pct,
        )
        result = validate_trade(
            _market(1 - eff_mid), contracts * cap, 0.95, 5000.0,
            0.0, 0, [], cfg, is_taker=True,
            event_exposure=250.0,
        )
        assert not result.approved
        assert any("Event exposure" in f for f in result.failures)

    def test_old_mid_sizing_overshot_the_cap(self) -> None:
        """The pre-fix arithmetic: sized at the mid, costed at the cap
        — deterministic overshoot (documents why the fix exists)."""
        eff_mid, cap = 0.66, 0.85
        contracts = position_size(
            5000.0, eff_mid, 0.95,
            is_taker=True, fraction=0.25, max_position_pct=0.1,
        )
        assert contracts * cap > 500.0

    def test_market_moved_shape_31431(self) -> None:
        """Mid moved to $0.70 with cap $0.85: still sized at the cap,
        still fits."""
        cfg = _config()
        eff_mid, cap = 0.70, 0.85
        contracts = position_size(
            5000.0, max(eff_mid, cap), 0.95,
            is_taker=True, fraction=cfg.sizing.kelly_fraction,
            max_position_pct=cfg.sizing.max_position_pct,
        )
        assert contracts > 0
        assert contracts * cap <= 500.0 + 1e-9

    def test_cap_below_mid_sizes_at_mid(self) -> None:
        """max() selects the mid; cost at the (lower) cap is smaller
        than the mid notional and passes trivially."""
        eff_mid, cap = 0.66, 0.60
        at_worst = position_size(
            5000.0, max(eff_mid, cap), 0.95,
            is_taker=True, fraction=0.25, max_position_pct=0.1,
        )
        at_mid = position_size(
            5000.0, eff_mid, 0.95,
            is_taker=True, fraction=0.25, max_position_pct=0.1,
        )
        assert at_worst == at_mid
        assert at_worst * cap <= 500.0 + 1e-9

    def test_negative_edge_at_cap_sizes_zero(self) -> None:
        """prob at/below cap + fee → zero contracts: a resting order
        that can only fill at negative edge should not exist."""
        assert (
            position_size(
                5000.0, 0.85, 0.80,
                is_taker=True, fraction=0.25, max_position_pct=0.1,
            )
            == 0
        )


class TestCliSizingWiring:
    """The order command passes the worst-case price to position_size."""

    def _run(self, cli_args):
        sized = MagicMock(return_value=100)
        broker = h._make_mock_broker()
        with patch("gimmes.strategy.kelly.position_size", sized):
            result, console, insert_error = h._run_order_cli(
                broker, cli_args=cli_args,
            )
        return result, sized, insert_error

    def test_cap_above_mid_sizes_at_cap(self) -> None:
        # Harness market: YES mid 0.40 → side yes eff price 0.40.
        _, sized, _ = self._run([
            "order", "TEST-TICKER",
            "--side", "yes", "--price", "85", "--prob", "0.95", "--yes",
        ])
        assert sized.call_args.args[1] == 0.85

    def test_no_cap_sizes_at_mid(self) -> None:
        _, sized, _ = self._run([
            "order", "TEST-TICKER",
            "--side", "yes", "--prob", "0.95", "--yes",
        ])
        assert sized.call_args.args[1] == 0.40

    def test_cap_below_mid_sizes_at_mid(self) -> None:
        _, sized, _ = self._run([
            "order", "TEST-TICKER",
            "--side", "yes", "--price", "20", "--prob", "0.95", "--yes",
        ])
        assert sized.call_args.args[1] == 0.40

    def test_size_up_with_cap_sizes_at_cap(self) -> None:
        _, sized, _ = self._run([
            "order", "TEST-TICKER",
            "--side", "yes", "--size-up", "--price", "85",
            "--prob", "0.95", "--yes",
        ])
        assert sized.call_args.args[1] == 0.85


class TestSizedZeroIsFailure:
    """#766 review-found: an approved candidate sizing to zero at the
    cap must fail LOUDLY (exit 1 + durable row), never exit-0 no-op —
    the Closer's protocol keys on the exit code."""

    def _run_zero(self, cli_args):
        broker = h._make_mock_broker()
        with patch(
            "gimmes.strategy.kelly.position_size",
            MagicMock(return_value=0),
        ):
            result, console, insert_error = h._run_order_cli(
                broker, cli_args=cli_args,
            )
        return result, h._printed(console), broker, insert_error

    def test_auto_sized_zero_exits_one_with_row(self) -> None:
        result, out, broker, insert_error = self._run_zero([
            "order", "TEST-TICKER",
            "--side", "yes", "--price", "85", "--prob", "0.95", "--yes",
        ])
        assert result.exit_code == 1, out
        assert "Sized to zero contracts" in out
        assert broker.create_order.await_count == 0
        rows = [
            c.args[1]
            for c in insert_error.await_args_list
            if len(c.args) > 1 and c.args[1].error_code == "sized_zero"
        ]
        assert len(rows) == 1
        ctx = json.loads(rows[0].context)
        assert ctx["cap_price"] == 0.85
        assert ctx["eff_price"] == 0.40

    def test_missing_inputs_keep_old_exit_zero(self) -> None:
        """No --prob and no --count is operator error, not a sized-zero
        rejection — the old message and exit stay."""
        result, out, broker, insert_error = self._run_zero([
            "order", "TEST-TICKER", "--side", "yes", "--yes",
        ])
        assert result.exit_code == 0, out
        assert "No contracts to order" in out
        codes = [
            c.args[1].error_code
            for c in insert_error.await_args_list
            if len(c.args) > 1
        ]
        assert "sized_zero" not in codes

    def test_degenerate_quote_does_not_size_from_cap(self) -> None:
        """Dead book (eff price 0): the cap alone must not produce a
        positive size — sizing sees the degenerate price and returns
        0 via the kelly guard, landing in the loud sized-zero exit."""
        market = h._stub_market()
        market.midpoint = 0.0
        market.last_price = 0.0
        broker = h._make_mock_broker()
        sized = MagicMock(return_value=0)
        with patch("gimmes.strategy.kelly.position_size", sized):
            result, console, _ = h._run_order_cli(
                broker, market=market,
                cli_args=[
                    "order", "TEST-TICKER",
                    "--side", "yes", "--price", "85",
                    "--prob", "0.95", "--yes",
                ],
            )
        assert sized.call_args.args[1] == 0.0
        assert result.exit_code == 1


class TestValidationFailedAuditRow:
    """An order-time validation rejection leaves a durable error row
    carrying both price bases; no #768 terminal marker is armed."""

    def _run_rejected(self, extra_args=None, monkeypatch=None):
        broker = h._make_mock_broker()
        activity = AsyncMock(return_value=1)
        validation = MagicMock(
            approved=False,
            failures=["Position $638.95 exceeds max $500.00"],
            summary="Validation failed with 1 check",
        )
        # Arm the #768 marker machinery for real (in-cycle + closer
        # agent) so the no-marker assertion is not vacuous.
        if monkeypatch is not None:
            monkeypatch.setenv("GIMMES_CYCLE", "42")
        args = list(extra_args or [])
        if monkeypatch is not None:
            args += ["--agent", "closer"]
        result, console, insert_error = h._run_order_cli(
            broker,
            extra_args=args,
            validation=validation,
            insert_activity_mock=activity,
        )
        return result, broker, insert_error, activity

    def test_rejection_writes_validation_failed_row(
        self, monkeypatch,
    ) -> None:
        result, broker, insert_error, activity = self._run_rejected(
            monkeypatch=monkeypatch,
        )
        assert broker.create_order.await_count == 0
        rows = [
            c.args[1]
            for c in insert_error.await_args_list
            if len(c.args) > 1
            and c.args[1].error_code == "validation_failed"
        ]
        assert len(rows) == 1
        ctx = json.loads(rows[0].context)
        assert ctx["ticker"] == "TEST-TICKER"
        assert ctx["count"] == 10
        assert ctx["eff_price"] == 0.40
        assert ctx["limit_price"] == 0.40
        assert ctx["trade_dollars"] == 4.0
        assert ctx["failures"] == [
            "Position $638.95 exceeds max $500.00"
        ]
        # No #768 terminal marker: next-cycle re-dispatch at fresh
        # prices must stay possible.
        markers = [
            c.kwargs
            for c in activity.await_args_list
            if c.kwargs.get("message", "").startswith(
                "Order attempt terminal:"
            )
        ]
        assert markers == []

    def test_context_carries_cap_price_basis(self) -> None:
        result, _, insert_error, _ = self._run_rejected(
            extra_args=["--price", "85"],
        )
        rows = [
            c.args[1]
            for c in insert_error.await_args_list
            if len(c.args) > 1
            and c.args[1].error_code == "validation_failed"
        ]
        assert len(rows) == 1
        ctx = json.loads(rows[0].context)
        assert ctx["eff_price"] == 0.40
        assert ctx["limit_price"] == 0.85
        assert ctx["trade_dollars"] == 8.5

    def test_force_overrides_without_error_row(self) -> None:
        result, broker, insert_error, _ = self._run_rejected(
            extra_args=["--force"],
        )
        assert broker.create_order.await_count == 1
        codes = [
            c.args[1].error_code
            for c in insert_error.await_args_list
            if len(c.args) > 1
        ]
        assert "validation_failed" not in codes
