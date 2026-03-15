"""Tests for order command error handling around placement and position sync."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from gimmes.models.error import ErrorCategory, ErrorSeverity
from gimmes.models.order import Order, OrderAction, OrderSide

_ORDER_CLI_ARGS = [
    "order", "TEST-TICKER",
    "--side", "yes", "--count", "10",
    "--price", "40", "--prob", "0.55", "--yes",
]


def _make_response(status: int, *, json_data=None, text="") -> httpx.Response:
    request = httpx.Request("POST", "https://api.example.com/portfolio/orders")
    if json_data is not None:
        return httpx.Response(status, json=json_data, request=request)
    return httpx.Response(status, text=text, request=request)


def _ok_order(order_id: str = "order-123", status: str = "executed") -> Order:
    return Order(
        order_id=order_id,
        ticker="TEST-TICKER",
        action=OrderAction.BUY,
        side=OrderSide.YES,
        status=status,
        yes_price=0.40,
        count=10,
    )


def _make_mock_broker(*, create_order_side_effect=None, get_positions_side_effect=None):
    broker = AsyncMock()
    broker.get_balance = AsyncMock(return_value=1000.0)

    if create_order_side_effect is not None:
        broker.create_order = AsyncMock(side_effect=create_order_side_effect)
    else:
        broker.create_order = AsyncMock(return_value=_ok_order())

    if get_positions_side_effect is not None:
        broker.get_positions = AsyncMock(side_effect=get_positions_side_effect)
    else:
        broker.get_positions = AsyncMock(return_value=[])

    return broker


def _fake_trading_context(broker):
    """Return a patched trading_context that yields mock client + given broker."""
    mock_client = AsyncMock()
    mock_db = AsyncMock()

    @asynccontextmanager
    async def _ctx(config):
        yield mock_client, broker, mock_db

    return _ctx


def _stub_market():
    m = MagicMock()
    m.midpoint = 0.40
    m.last_price = 0.40
    m.series_ticker = "TEST"
    m.close_time = None
    m.title = "Test Market"
    m.subtitle = ""
    m.status = "active"
    return m


def _stub_config():
    c = MagicMock()
    c.is_championship = False
    c.db_path = ":memory:"
    c.paper = MagicMock()
    return c


def _run_order_cli(
    broker, *, sync_side_effect=None, championship_create_order=None,
    insert_error_side_effect=None,
):
    """Invoke the order CLI command with a mocked broker."""
    mock_console = MagicMock()
    mock_fees = MagicMock()
    mock_fees.taker_fee = 0.07
    mock_fees.maker_fee = 0.03
    mock_insert_error = AsyncMock(
        return_value=1, side_effect=insert_error_side_effect,
    )

    patches = [
        patch("gimmes.cli.load_config", return_value=_stub_config()),
        patch("gimmes.cli.trading_context", _fake_trading_context(broker)),
        patch("gimmes.cli.console", mock_console),
        patch("gimmes.cli._championship_warning"),
        patch("gimmes.kalshi.markets.get_market", AsyncMock(return_value=_stub_market())),
        patch("gimmes.kalshi.markets.get_orderbook", AsyncMock(return_value=MagicMock())),
        patch("gimmes.strategy.fee_cache.get_multipliers", MagicMock(return_value=mock_fees)),
        patch(
            "gimmes.risk.validator.validate_trade",
            MagicMock(return_value=MagicMock(approved=True, failures=[])),
        ),
        patch("gimmes.store.queries.get_daily_pnl", AsyncMock(return_value=0.0)),
        patch("gimmes.store.queries.insert_error", mock_insert_error),
        patch("gimmes.strategy.fees.fee_for_order", MagicMock(return_value=0.03)),
    ]

    # Championship mode (broker=None) needs patched portfolio and order funcs
    if broker is None:
        patches.extend([
            patch(
                "gimmes.kalshi.portfolio.get_balance",
                AsyncMock(return_value=1000.0),
            ),
            patch(
                "gimmes.kalshi.portfolio.get_all_positions",
                AsyncMock(return_value=[]),
            ),
            patch("gimmes.store.queries.sync_positions", AsyncMock()),
        ])
        if championship_create_order is not None:
            patches.append(
                patch(
                    "gimmes.kalshi.orders.create_order",
                    championship_create_order,
                )
            )

    if sync_side_effect is not None:
        patches.append(
            patch(
                "gimmes.store.queries.sync_positions_with_trade",
                AsyncMock(side_effect=sync_side_effect),
            )
        )

    for p in patches:
        p.start()

    try:
        from typer.testing import CliRunner

        from gimmes.cli import app

        runner = CliRunner()
        result = runner.invoke(app, _ORDER_CLI_ARGS)
    finally:
        for p in patches:
            p.stop()

    return result, mock_console, mock_insert_error


def _printed(mock_console) -> str:
    return " ".join(str(c) for c in mock_console.print.call_args_list)


# ---------------------------------------------------------------------------
# Order placement error tests
# ---------------------------------------------------------------------------


class TestOrderPlacementErrors:
    def test_api_error_shows_status_and_message(self) -> None:
        resp = _make_response(400, json_data={"message": "Insufficient balance"})
        exc = httpx.HTTPStatusError("error", request=resp.request, response=resp)
        broker = _make_mock_broker(create_order_side_effect=exc)

        result, mock_console, _ = _run_order_cli(broker)

        assert result.exit_code == 1
        out = _printed(mock_console)
        assert "Order FAILED" in out
        assert "Insufficient balance" in out

    def test_timeout_warns_about_reconcile(self) -> None:
        exc = httpx.ReadTimeout("Connection read timed out")
        broker = _make_mock_broker(create_order_side_effect=exc)

        result, mock_console, _ = _run_order_cli(broker)

        assert result.exit_code == 1
        out = _printed(mock_console)
        assert "Order FAILED" in out
        assert "timed out" in out
        assert "reconcile" in out

    def test_championship_timeout_warns_order_may_have_been_accepted(self) -> None:
        exc = httpx.ReadTimeout("Connection read timed out")
        mock_create = AsyncMock(side_effect=exc)

        result, mock_console, _ = _run_order_cli(
            None, championship_create_order=mock_create
        )

        assert result.exit_code == 1
        out = _printed(mock_console)
        assert "Order FAILED" in out
        assert "timed out" in out
        assert "may have been accepted" in out
        assert "reconcile" in out

    def test_generic_error_shows_message(self) -> None:
        exc = RuntimeError("Paper DB locked")
        broker = _make_mock_broker(create_order_side_effect=exc)

        result, mock_console, _ = _run_order_cli(broker)

        assert result.exit_code == 1
        out = _printed(mock_console)
        assert "Order FAILED" in out
        assert "Paper DB locked" in out

    def test_placement_error_skips_post_order_sync(self) -> None:
        exc = RuntimeError("create failed")
        broker = _make_mock_broker(create_order_side_effect=exc)

        result, _, _ = _run_order_cli(broker)

        assert result.exit_code == 1
        # get_positions is called once for pre-order validation (line 395),
        # but should NOT be called a second time for post-order sync
        assert broker.get_positions.call_count == 1


# ---------------------------------------------------------------------------
# Position sync error tests
# ---------------------------------------------------------------------------


def _broker_with_post_order_fetch_failure(error: Exception):
    """Create a broker where get_positions succeeds first (pre-order) then fails (post-order)."""
    broker = _make_mock_broker()
    broker.get_positions = AsyncMock(side_effect=[[], error])
    return broker


class TestPositionSyncErrors:
    def test_position_fetch_failure_warns_and_suggests_reconcile(self) -> None:
        broker = _broker_with_post_order_fetch_failure(
            RuntimeError("DB connection lost")
        )

        _, mock_console, _ = _run_order_cli(broker)

        out = _printed(mock_console)
        assert "Order was placed successfully" in out
        assert "position sync failed" in out
        assert "reconcile" in out

    def test_db_write_failure_warns_and_suggests_reconcile(self) -> None:
        broker = _make_mock_broker()

        _, mock_console, _ = _run_order_cli(
            broker,
            sync_side_effect=RuntimeError("disk full"),
        )

        out = _printed(mock_console)
        assert "Order was placed successfully" in out
        assert "position sync failed" in out
        assert "reconcile" in out

    def test_sync_failure_shows_order_id(self) -> None:
        broker = _broker_with_post_order_fetch_failure(
            RuntimeError("fetch failed")
        )

        _, mock_console, _ = _run_order_cli(broker)

        out = _printed(mock_console)
        assert "order-123" in out

    def test_sync_failure_does_not_crash(self) -> None:
        broker = _broker_with_post_order_fetch_failure(
            RuntimeError("fetch failed")
        )

        _, mock_console, _ = _run_order_cli(broker)

        out = _printed(mock_console)
        assert "Order placed:" in out


# ---------------------------------------------------------------------------
# Structured error logging tests
# ---------------------------------------------------------------------------


def _error_entry(mock_insert_error):
    """Extract the ErrorLogEntry from the first insert_error call."""
    assert mock_insert_error.call_count >= 1
    return mock_insert_error.call_args_list[0].args[1]


class TestOrderErrorLogging:
    def test_api_error_logs_order_failure(self) -> None:
        resp = _make_response(400, json_data={"message": "Insufficient balance"})
        exc = httpx.HTTPStatusError("error", request=resp.request, response=resp)
        broker = _make_mock_broker(create_order_side_effect=exc)

        _, _, mock_insert = _run_order_cli(broker)

        entry = _error_entry(mock_insert)
        assert entry.severity == ErrorSeverity.ERROR
        assert entry.category == ErrorCategory.ORDER_FAILURE
        assert entry.error_code == "http_status_error"
        assert entry.component == "cli.order"
        ctx = json.loads(entry.context)
        assert ctx["ticker"] == "TEST-TICKER"
        assert ctx["status_code"] == 400

    def test_timeout_logs_order_failure(self) -> None:
        exc = httpx.ReadTimeout("Connection read timed out")
        broker = _make_mock_broker(create_order_side_effect=exc)

        _, _, mock_insert = _run_order_cli(broker)

        entry = _error_entry(mock_insert)
        assert entry.severity == ErrorSeverity.ERROR
        assert entry.category == ErrorCategory.ORDER_FAILURE
        assert entry.error_code == "timeout"
        assert "timed out" in entry.message
        assert "Connection read timed out" in entry.message

    def test_generic_error_logs_order_failure(self) -> None:
        exc = RuntimeError("Paper DB locked")
        broker = _make_mock_broker(create_order_side_effect=exc)

        _, _, mock_insert = _run_order_cli(broker)

        entry = _error_entry(mock_insert)
        assert entry.severity == ErrorSeverity.ERROR
        assert entry.category == ErrorCategory.ORDER_FAILURE
        assert entry.error_code == "unexpected"
        assert "Paper DB locked" in entry.message

    def test_position_sync_failure_logs_data_integrity(self) -> None:
        broker = _broker_with_post_order_fetch_failure(
            RuntimeError("DB connection lost")
        )

        _, _, mock_insert = _run_order_cli(broker)

        entry = _error_entry(mock_insert)
        assert entry.severity == ErrorSeverity.WARNING
        assert entry.category == ErrorCategory.DATA_INTEGRITY
        assert entry.error_code == "position_sync_failed"
        ctx = json.loads(entry.context)
        assert ctx["order_id"] == "order-123"

    def test_error_context_includes_trade_details(self) -> None:
        exc = httpx.ReadTimeout("timeout")
        broker = _make_mock_broker(create_order_side_effect=exc)

        _, _, mock_insert = _run_order_cli(broker)

        entry = _error_entry(mock_insert)
        ctx = json.loads(entry.context)
        assert ctx["ticker"] == "TEST-TICKER"
        assert ctx["side"] == "yes"
        assert ctx["count"] == 10
        assert ctx["price"] == 0.40


class TestErrorLoggingResilience:
    """Verify that insert_error failures never mask the original error."""

    def test_db_failure_does_not_mask_placement_error(self) -> None:
        resp = _make_response(400, json_data={"message": "Insufficient balance"})
        exc = httpx.HTTPStatusError("error", request=resp.request, response=resp)
        broker = _make_mock_broker(create_order_side_effect=exc)

        result, mock_console, _ = _run_order_cli(
            broker, insert_error_side_effect=RuntimeError("DB broken"),
        )

        assert result.exit_code == 1
        out = _printed(mock_console)
        assert "Order FAILED" in out
        assert "Insufficient balance" in out

    def test_db_failure_does_not_mask_sync_error(self) -> None:
        broker = _broker_with_post_order_fetch_failure(
            RuntimeError("fetch failed")
        )

        _, mock_console, _ = _run_order_cli(
            broker, insert_error_side_effect=RuntimeError("DB broken"),
        )

        out = _printed(mock_console)
        assert "Order was placed successfully" in out
        assert "position sync failed" in out
