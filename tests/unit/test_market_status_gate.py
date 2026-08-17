"""#784: the market-status gate — orders on non-ACTIVE markets are
refused explicitly instead of dying as incidental cancels/4xxs."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gimmes.models.market import (
    SETTLED_STATUSES,
    UNTRADEABLE_STATUSES,
    MarketStatus,
)
from tests.unit import test_order_error_handling as h


def _gate_rows(insert_error):
    return [
        c.args[1]
        for c in insert_error.await_args_list
        if len(c.args) > 1
        and c.args[1].error_code == "market_not_active"
    ]


class TestMarketStatusGate:
    def _run(self, *, status, cli_args=None, extra_args=None,
             insert_activity_mock=None, monkeypatch=None, cycle=None):
        if monkeypatch is not None and cycle is not None:
            monkeypatch.setenv("GIMMES_CYCLE", cycle)
        market = h._stub_market()
        market.status = status
        broker = h._make_mock_broker()
        self._last_broker = broker
        result, console, insert_error = h._run_order_cli(
            broker, market=market, cli_args=cli_args,
            extra_args=extra_args,
            insert_activity_mock=insert_activity_mock,
        )
        return result, h._printed(console), insert_error

    @pytest.mark.parametrize(
        "status", sorted(UNTRADEABLE_STATUSES, key=str),
    )
    def test_buy_refused_on_every_non_active_status(
        self, status,
    ) -> None:
        result, out, insert_error = self._run(status=status)
        assert result.exit_code == 1, out
        assert "Market status gate (#784)" in out
        assert self._last_broker.create_order.await_count == 0
        rows = _gate_rows(insert_error)
        assert len(rows) == 1
        assert json.loads(rows[0].context)["status"] == str(status)

    def test_string_active_stub_proceeds(self) -> None:
        """StrEnum/str equivalence pin: the harness's plain 'active'
        string must keep passing the gate."""
        result, out, _ = self._run(status="active")
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1

    def test_magicmock_status_proceeds(self) -> None:
        """Blast-radius pin: membership (not != ACTIVE) means an
        unset MagicMock status fails OPEN."""
        result, out, _ = self._run(status=MagicMock())
        assert result.exit_code == 0, out
        assert self._last_broker.create_order.await_count == 1

    def test_sell_on_determined_names_settlement(self) -> None:
        result, out, _ = self._run(
            status=MarketStatus.DETERMINED,
            cli_args=[
                "order", "TEST-TICKER", "--action", "sell",
                "--side", "yes", "--count", "10", "--yes",
            ],
        )
        assert result.exit_code == 1, out
        assert "settlement supersedes" in out
        assert self._last_broker.create_order.await_count == 0
        # Settle sweep ran BEFORE the refusal (#782 ordering)
        assert self._last_broker.get_positions.await_count == 1

    def test_buy_on_determined_names_resolution(self) -> None:
        result, out, _ = self._run(status=MarketStatus.DETERMINED)
        assert result.exit_code == 1, out
        assert "RESOLVED" in out

    def test_sell_on_closed_does_not_claim_settlement(self) -> None:
        """#781 pin: closed-not-yet-settled is a real transient — the
        message must not claim the market resolved."""
        result, out, _ = self._run(
            status=MarketStatus.CLOSED,
            cli_args=[
                "order", "TEST-TICKER", "--action", "sell",
                "--side", "yes", "--count", "10", "--yes",
            ],
        )
        assert result.exit_code == 1, out
        assert "settlement supersedes" not in out
        assert "not active" in out

    def test_in_cycle_refusal_stamps_and_arms(
        self, monkeypatch,
    ) -> None:
        activity = AsyncMock(return_value=1)
        result, out, insert_error = self._run(
            status=MarketStatus.DETERMINED,
            extra_args=["--agent", "closer"],
            insert_activity_mock=activity,
            monkeypatch=monkeypatch, cycle="42",
        )
        assert result.exit_code == 1, out
        rows = _gate_rows(insert_error)
        assert rows[0].cycle == 42
        markers = [
            c.kwargs
            for c in activity.await_args_list
            if c.kwargs.get("message", "").startswith(
                "Order attempt terminal:"
            )
        ]
        assert len(markers) == 1

    def test_out_of_cycle_refusal_does_not_arm(self) -> None:
        activity = AsyncMock(return_value=1)
        result, out, insert_error = self._run(
            status=MarketStatus.DETERMINED,
            insert_activity_mock=activity,
        )
        assert result.exit_code == 1, out
        rows = _gate_rows(insert_error)
        assert rows[0].cycle == 0
        assert not any(
            c.kwargs.get("message", "").startswith(
                "Order attempt terminal:"
            )
            for c in activity.await_args_list
        )

    def test_settled_statuses_subset(self) -> None:
        assert SETTLED_STATUSES < UNTRADEABLE_STATUSES
        assert MarketStatus.ACTIVE not in UNTRADEABLE_STATUSES
