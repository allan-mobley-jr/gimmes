"""Tests for order command error handling around placement and position sync."""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gimmes.models.error import ErrorCategory, ErrorSeverity
from gimmes.models.order import Order, OrderAction, OrderSide
from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision

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
    insert_error_side_effect=None, extra_args=None, market=None,
    snapshot_mock=None, validation=None, cli_args=None,
    last_close=None, last_close_effect=None,
):
    """Invoke the order CLI command with a mocked broker.

    `market` overrides the stub market; `snapshot_mock` overrides the
    #643 rules-snapshot patch (defaults to AsyncMock(return_value=True)
    so the real helper never runs against the mocked DB).
    """
    mock_console = MagicMock()
    mock_fees = MagicMock()
    mock_fees.taker_fee = 0.07
    mock_fees.maker_fee = 0.03
    mock_insert_error = AsyncMock(
        return_value=1, side_effect=insert_error_side_effect,
    )

    # Bypass mark-to-market — these tests focus on order error handling
    async def _passthrough_mtm(b, c, **kw):
        return await b.get_positions()

    patches = [
        patch("gimmes.cli.load_config", return_value=_stub_config()),
        patch("gimmes.cli.trading_context", _fake_trading_context(broker)),
        patch("gimmes.cli.console", mock_console),
        patch("gimmes.cli._mode_banner"),
        patch("gimmes.cli._mark_positions_to_market", _passthrough_mtm),
        patch(
            "gimmes.kalshi.markets.get_market",
            AsyncMock(return_value=market if market is not None else _stub_market()),
        ),
        # #643: the snapshot helper would otherwise run for real against
        # the mocked DB (MagicMock rowcount comparisons).
        patch(
            "gimmes.store.queries.set_position_rules_snapshot",
            snapshot_mock if snapshot_mock is not None
            else AsyncMock(return_value=True),
        ),
        patch("gimmes.kalshi.markets.get_orderbook", AsyncMock(return_value=MagicMock())),
        patch("gimmes.strategy.fee_cache.get_multipliers", MagicMock(return_value=mock_fees)),
        patch(
            "gimmes.risk.validator.validate_trade",
            MagicMock(
                return_value=validation if validation is not None
                else MagicMock(approved=True, failures=[]),
            ),
        ),
        patch("gimmes.store.queries.get_daily_pnl", AsyncMock(return_value=0.0)),
        patch("gimmes.store.queries.get_deployed_cost_basis", AsyncMock(return_value=0.0)),
        patch("gimmes.store.queries.insert_error", mock_insert_error),
        patch("gimmes.strategy.fees.fee_for_order", MagicMock(return_value=0.03)),
        # #661: the buy path consults the ticker's last close; default
        # to none so the reopen gate stays out of unrelated tests.
        patch(
            "gimmes.store.queries.get_last_close_trade",
            AsyncMock(
                return_value=last_close, side_effect=last_close_effect,
            ),
        ),
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
        if cli_args is None:
            cli_args = _ORDER_CLI_ARGS + (extra_args or [])
        result = runner.invoke(app, cli_args)
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
            sqlite3.OperationalError("DB connection lost")
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
            sync_side_effect=sqlite3.OperationalError("disk full"),
        )

        out = _printed(mock_console)
        assert "Order was placed successfully" in out
        assert "position sync failed" in out
        assert "reconcile" in out

    def test_sync_failure_shows_order_id(self) -> None:
        broker = _broker_with_post_order_fetch_failure(
            sqlite3.OperationalError("fetch failed")
        )

        _, mock_console, _ = _run_order_cli(broker)

        out = _printed(mock_console)
        assert "order-123" in out

    def test_sync_failure_does_not_crash(self) -> None:
        broker = _broker_with_post_order_fetch_failure(
            sqlite3.OperationalError("fetch failed")
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
        assert entry.error_code == "runtime_error"
        assert "Paper DB locked" in entry.message

    def test_position_sync_failure_logs_data_integrity(self) -> None:
        broker = _broker_with_post_order_fetch_failure(
            sqlite3.OperationalError("DB connection lost")
        )

        _, _, mock_insert = _run_order_cli(broker)

        entry = _error_entry(mock_insert)
        assert entry.severity == ErrorSeverity.WARNING
        assert entry.category == ErrorCategory.DATA_INTEGRITY
        assert entry.error_code == "position_sync_db_error"
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

    def test_agent_flag_propagates_to_error_log(self) -> None:
        """The --agent flag value flows through to error log entries."""
        exc = httpx.ReadTimeout("timeout")
        broker = _make_mock_broker(create_order_side_effect=exc)

        _, _, mock_insert = _run_order_cli(
            broker, extra_args=["--agent", "closer"],
        )

        entry = _error_entry(mock_insert)
        assert entry.agent == "closer"

    def test_agent_defaults_to_cli(self) -> None:
        """Without --agent, error logs default to 'cli'."""
        exc = httpx.ReadTimeout("timeout")
        broker = _make_mock_broker(create_order_side_effect=exc)

        _, _, mock_insert = _run_order_cli(broker)

        entry = _error_entry(mock_insert)
        assert entry.agent == "cli"


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
            sqlite3.OperationalError("fetch failed")
        )

        _, mock_console, _ = _run_order_cli(
            broker, insert_error_side_effect=RuntimeError("DB broken"),
        )

        out = _printed(mock_console)
        assert "Order was placed successfully" in out
        assert "position sync failed" in out


# ---------------------------------------------------------------------------
# Narrowed exception type tests
# ---------------------------------------------------------------------------


class TestNarrowedExceptionTypes:
    def test_sqlite_error_caught_in_placement(self) -> None:
        exc = sqlite3.OperationalError("database is locked")
        broker = _make_mock_broker(create_order_side_effect=exc)

        result, mock_console, mock_insert = _run_order_cli(broker)

        assert result.exit_code == 1
        out = _printed(mock_console)
        assert "Order FAILED" in out
        entry = _error_entry(mock_insert)
        assert entry.error_code == "db_error"

    def test_value_error_caught_in_placement(self) -> None:
        exc = ValueError("invalid price format")
        broker = _make_mock_broker(create_order_side_effect=exc)

        result, mock_console, mock_insert = _run_order_cli(broker)

        assert result.exit_code == 1
        out = _printed(mock_console)
        assert "Order FAILED" in out
        entry = _error_entry(mock_insert)
        assert entry.error_code == "value_error"

    def test_programming_bug_propagates_from_placement(self) -> None:
        """TypeError should NOT be caught — it propagates as an unhandled exception."""
        exc = TypeError("unsupported operand")
        broker = _make_mock_broker(create_order_side_effect=exc)

        result, _, _ = _run_order_cli(broker)

        assert result.exit_code == 1
        assert isinstance(result.exception, TypeError)

    def test_sqlite_sync_failure_suggests_db_health(self) -> None:
        broker = _broker_with_post_order_fetch_failure(
            sqlite3.OperationalError("database is locked")
        )

        _, mock_console, _ = _run_order_cli(broker)

        out = _printed(mock_console)
        assert "Order was placed successfully" in out
        assert "database" in out.lower()
        assert "health" in out.lower()

    def test_httpx_sync_failure_warns_reconcile(self) -> None:
        broker = _broker_with_post_order_fetch_failure(
            httpx.ConnectError("connection refused")
        )

        _, mock_console, mock_insert = _run_order_cli(broker)

        out = _printed(mock_console)
        assert "Order was placed successfully" in out
        assert "reconcile" in out
        entry = _error_entry(mock_insert)
        assert entry.error_code == "position_sync_failed"

    def test_value_error_sync_failure_warns_reconcile(self) -> None:
        broker = _broker_with_post_order_fetch_failure(
            ValueError("bad position data")
        )

        _, mock_console, mock_insert = _run_order_cli(broker)

        out = _printed(mock_console)
        assert "Order was placed successfully" in out
        assert "reconcile" in out
        entry = _error_entry(mock_insert)
        assert entry.error_code == "position_sync_failed"

    def test_programming_bug_propagates_from_sync(self) -> None:
        """TypeError in sync should NOT be caught — it propagates."""
        broker = _broker_with_post_order_fetch_failure(
            TypeError("unexpected type")
        )

        result, _, _ = _run_order_cli(broker)

        assert result.exit_code == 1
        assert isinstance(result.exception, TypeError)


# ---------------------------------------------------------------------------
# Trade rationale propagation tests
# ---------------------------------------------------------------------------


class TestTradeRationalePropagation:
    """Verify the trade rationale carries the Caddie thesis, not a placeholder."""

    def _run_with_thesis(
        self, thesis_text: str, *, extra_args: list[str] | None = None,
    ):
        """Run the order CLI with a patched thesis and capture the TradeDecision."""
        broker = _make_mock_broker()
        sync_spy = AsyncMock()

        with patch(
            "gimmes.store.queries.get_thesis_for_ticker",
            AsyncMock(return_value=thesis_text),
        ):
            result, _, _ = _run_order_cli(
                broker, sync_side_effect=sync_spy, extra_args=extra_args,
            )

        trade = sync_spy.call_args.args[2] if sync_spy.call_count else None
        return result, trade

    def test_rationale_uses_thesis_when_available(self) -> None:
        """When a Caddie thesis exists, it becomes the trade rationale."""
        thesis = "Threshold extremely low; weekly claims signal stable labor market"
        result, trade = self._run_with_thesis(thesis)

        assert result.exit_code == 0
        assert trade is not None
        assert trade.rationale == thesis
        assert trade.thesis == thesis

    def test_rationale_falls_back_when_thesis_empty(self) -> None:
        """When no thesis exists, rationale defaults to '{agent} order'."""
        result, trade = self._run_with_thesis("")

        assert result.exit_code == 0
        assert trade is not None
        assert trade.rationale == "cli order"

    def test_rationale_falls_back_with_custom_agent(self) -> None:
        """The fallback rationale uses the --agent flag value."""
        result, trade = self._run_with_thesis(
            "", extra_args=["--agent", "closer"],
        )

        assert result.exit_code == 0
        assert trade is not None
        assert trade.rationale == "closer order"


# ---------------------------------------------------------------------------
# Size-up thesis anchoring tests
# ---------------------------------------------------------------------------


def _position_for_size_up() -> Position:
    return Position(
        ticker="TEST-TICKER", side="yes", count=5,
        avg_price=0.40, cost_basis=2.0,
    )


class TestSizeUpThesisAnchoring:
    """Verify size_up trades source thesis from the open trade, not candidates."""

    def _run_with_size_up(
        self,
        *,
        open_trade_return: dict | None,
        candidate_thesis: str = "diverged candidate thesis",
        extra_args: list[str] | None = None,
    ):
        """Run the order CLI with patched thesis sources and capture the trade."""
        broker = _make_mock_broker(
            get_positions_side_effect=AsyncMock(
                return_value=[_position_for_size_up()],
            ),
        )
        sync_spy = AsyncMock()

        with (
            patch(
                "gimmes.store.queries.get_open_trade_for_ticker",
                AsyncMock(return_value=open_trade_return),
            ) as open_trade_mock,
            patch(
                "gimmes.store.queries.get_thesis_for_ticker",
                AsyncMock(return_value=candidate_thesis),
            ) as candidate_mock,
        ):
            result, _, _ = _run_order_cli(
                broker, sync_side_effect=sync_spy,
                extra_args=["--size-up"] + (extra_args or []),
            )

        trade = sync_spy.call_args.args[2] if sync_spy.call_count else None
        return result, trade, open_trade_mock, candidate_mock

    def test_size_up_uses_thesis_from_open_trade(self) -> None:
        """Size-up should carry the original open trade's thesis."""
        result, trade, _, candidate_mock = self._run_with_size_up(
            open_trade_return={"thesis": "original open thesis"},
        )

        assert result.exit_code == 0
        assert trade is not None
        assert trade.thesis == "original open thesis"
        assert trade.rationale == "original open thesis"
        candidate_mock.assert_not_called()

    def test_size_up_falls_back_when_no_open_trade(self) -> None:
        """When no open trade exists, size-up falls back to candidate thesis."""
        result, trade, _, _ = self._run_with_size_up(
            open_trade_return=None,
            candidate_thesis="candidate thesis",
        )

        assert result.exit_code == 0
        assert trade is not None
        assert trade.thesis == "candidate thesis"

    def test_size_up_falls_back_when_open_trade_thesis_empty(self) -> None:
        """When open trade thesis is empty, size-up falls back to candidates."""
        result, trade, _, _ = self._run_with_size_up(
            open_trade_return={"thesis": ""},
            candidate_thesis="candidate thesis",
        )

        assert result.exit_code == 0
        assert trade is not None
        assert trade.thesis == "candidate thesis"

    def test_open_trade_does_not_use_open_trade_lookup(self) -> None:
        """Normal open trades should NOT call get_open_trade_for_ticker."""
        broker = _make_mock_broker()
        sync_spy = AsyncMock()

        with (
            patch(
                "gimmes.store.queries.get_open_trade_for_ticker",
                AsyncMock(return_value=None),
            ) as open_trade_mock,
            patch(
                "gimmes.store.queries.get_thesis_for_ticker",
                AsyncMock(return_value="candidate thesis"),
            ),
        ):
            result, _, _ = _run_order_cli(
                broker, sync_side_effect=sync_spy,
            )

        trade = sync_spy.call_args.args[2] if sync_spy.call_count else None
        assert result.exit_code == 0
        assert trade is not None
        assert trade.thesis == "candidate thesis"
        open_trade_mock.assert_not_called()

    def test_size_up_handles_db_error_on_open_trade_lookup(self) -> None:
        """sqlite3.Error during open trade lookup yields empty thesis."""
        broker = _make_mock_broker(
            get_positions_side_effect=AsyncMock(
                return_value=[_position_for_size_up()],
            ),
        )
        sync_spy = AsyncMock()

        with (
            patch(
                "gimmes.store.queries.get_open_trade_for_ticker",
                AsyncMock(side_effect=sqlite3.OperationalError("db locked")),
            ),
            patch(
                "gimmes.store.queries.get_thesis_for_ticker",
                AsyncMock(return_value="should not reach"),
            ) as candidate_mock,
        ):
            result, _, _ = _run_order_cli(
                broker, sync_side_effect=sync_spy,
                extra_args=["--size-up"],
            )

        trade = sync_spy.call_args.args[2] if sync_spy.call_count else None
        assert result.exit_code == 0
        assert trade is not None
        assert trade.thesis == ""
        candidate_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Thesis fetch structured error logging tests
# ---------------------------------------------------------------------------


def _assert_thesis_fetch_error(mock_insert) -> None:
    """Verify the first logged error is a thesis_fetch_failed DATA_INTEGRITY entry."""
    entry = _error_entry(mock_insert)
    assert entry.severity == ErrorSeverity.WARNING
    assert entry.category == ErrorCategory.DATA_INTEGRITY
    assert entry.error_code == "thesis_fetch_failed"
    assert "TEST-TICKER" in entry.message
    ctx = json.loads(entry.context)
    assert ctx == {
        "ticker": "TEST-TICKER", "side": "yes",
        "count": 10, "price": 0.4,
    }


class TestThesisFetchErrorLogging:
    """Verify thesis fetch failures create structured DATA_INTEGRITY errors."""

    def test_thesis_fetch_db_error_creates_structured_error(self) -> None:
        """sqlite3.Error during thesis fetch records a DATA_INTEGRITY error."""
        broker = _make_mock_broker()
        sync_spy = AsyncMock()

        with patch(
            "gimmes.store.queries.get_thesis_for_ticker",
            AsyncMock(side_effect=sqlite3.OperationalError("db locked")),
        ):
            result, _, mock_insert = _run_order_cli(
                broker, sync_side_effect=sync_spy,
            )

        assert result.exit_code == 0
        _assert_thesis_fetch_error(mock_insert)

    def test_size_up_thesis_fetch_db_error_creates_structured_error(self) -> None:
        """sqlite3.Error during size-up thesis fetch records a DATA_INTEGRITY error."""
        broker = _make_mock_broker(
            get_positions_side_effect=AsyncMock(
                return_value=[_position_for_size_up()],
            ),
        )
        sync_spy = AsyncMock()

        with (
            patch(
                "gimmes.store.queries.get_open_trade_for_ticker",
                AsyncMock(side_effect=sqlite3.OperationalError("db locked")),
            ),
            patch(
                "gimmes.store.queries.get_thesis_for_ticker",
                AsyncMock(return_value="should not reach"),
            ),
        ):
            result, _, mock_insert = _run_order_cli(
                broker, sync_side_effect=sync_spy,
                extra_args=["--size-up"],
            )

        assert result.exit_code == 0
        _assert_thesis_fetch_error(mock_insert)


# ---------------------------------------------------------------------------
# #643: settlement-language snapshot wiring on BUY orders
# ---------------------------------------------------------------------------


class TestRulesSnapshotWiring:
    """The order command must snapshot market.rules_primary onto the
    positions row after a successful BUY sync — if this wiring breaks,
    the #643 semantics guard goes permanently dormant for every new
    position (silent degradation)."""

    RULES = (
        "If the Consumer Price Index increases by more than 0.5% in"
        " April 2026, the market resolves to Yes."
    )

    def _run_with_snapshot_spy(self, *, extra_args=None, snap_return=True):
        broker = _make_mock_broker()
        snap_spy = AsyncMock(return_value=snap_return)
        market = _stub_market()
        market.rules_primary = self.RULES

        with patch(
            "gimmes.store.queries.get_thesis_for_ticker",
            AsyncMock(return_value="test thesis"),
        ):
            result, _, _ = _run_order_cli(
                broker, extra_args=extra_args, market=market,
                snapshot_mock=snap_spy, sync_side_effect=AsyncMock(),
            )
        return result, snap_spy

    def test_buy_snapshots_rules_primary(self) -> None:
        result, snap_spy = self._run_with_snapshot_spy()
        assert result.exit_code == 0, result.output
        snap_spy.assert_awaited_once()
        kwargs = snap_spy.await_args.kwargs
        assert kwargs["ticker"] == "TEST-TICKER"
        assert kwargs["rules_primary"] == self.RULES

    def test_sell_does_not_snapshot(self) -> None:
        result, snap_spy = self._run_with_snapshot_spy(
            extra_args=["--action", "sell"],
        )
        snap_spy.assert_not_awaited()

    def test_snapshot_false_does_not_fail_order(self) -> None:
        """A resting order (no position row yet) returns False from the
        snapshot helper — the order must still succeed; backfill is
        #647."""
        result, snap_spy = self._run_with_snapshot_spy(snap_return=False)
        assert result.exit_code == 0, result.output
        snap_spy.assert_awaited_once()


class TestOrderValidationMarkupEscape:
    def test_rejection_display_escapes_bracketed_failures(self) -> None:
        """Order-command rejection summary and per-failure lines embed
        settlement red-flag lists — the escapes at both sites must
        survive (console is mocked, so we pin the pre-render markup
        directly: the escaped form contains a backslash-bracket)
        (#644)."""
        from gimmes.risk.validator import ValidationResult

        broker = _make_mock_broker()
        vr = ValidationResult(
            approved=False,
            checks=[],
            failures=[
                "Settlement risk HIGH: found [sole discretion]",
            ],
        )
        result, mock_console, _ = _run_order_cli(broker, validation=vr)
        printed = " ".join(
            str(c.args[0]) for c in mock_console.print.call_args_list
            if c.args
        )
        # Escaped markup contains the literal backslash-bracket form on
        # BOTH the summary line and the per-failure line.
        assert printed.count("\\[sole") >= 2, printed


# ---------------------------------------------------------------------------
# #656: order-close entry-analytics inheritance
# ---------------------------------------------------------------------------

_SELL_CLI_ARGS = [
    "order", "TEST-TICKER", "--action", "sell",
    "--side", "yes", "--count", "10", "--price", "40", "--yes",
]

_ENTRY = {
    "model_probability": 0.9, "gimme_score": 82.0,
    "edge": 0.25, "kelly_fraction": 0.03,
}


class TestCloseInheritsEntryAnalytics:
    """#656: the capital-deployment close path. Without an explicit
    --prob, the recorded close row inherits the entry decision's
    analytics; with one, close-time semantics are preserved."""

    @staticmethod
    def _broker_with_position():
        """Sell orders require an existing position to close."""
        pos = Position(
            ticker="TEST-TICKER", side="yes", count=100,
            avg_price=0.60, market_price=0.60, cost_basis=60.0,
        )
        return _make_mock_broker(get_positions_side_effect=lambda: [pos])

    @staticmethod
    def _capture():
        captured = {}

        async def _sync(db, positions, trade):
            captured["trade"] = trade

        return captured, _sync

    def test_close_without_prob_inherits_entry_analytics(self) -> None:
        broker = self._broker_with_position()
        captured, sync = self._capture()
        entry_mock = AsyncMock(return_value=dict(_ENTRY))
        with patch("gimmes.store.queries.get_entry_analytics", entry_mock):
            result, mock_console, _ = _run_order_cli(
                broker, sync_side_effect=sync, cli_args=_SELL_CLI_ARGS,
            )
        assert result.exit_code == 0, _printed(mock_console)
        t = captured["trade"]
        assert t.action is TradeDecision.Action.CLOSE
        assert t.model_probability == 0.9
        assert t.gimme_score == 82.0
        assert t.edge == 0.25
        assert t.kelly_fraction == 0.03
        entry_mock.assert_awaited_once()

    def test_close_with_explicit_prob_keeps_close_time_semantics(
        self,
    ) -> None:
        broker = self._broker_with_position()
        captured, sync = self._capture()
        entry_mock = AsyncMock(return_value=dict(_ENTRY))
        with patch("gimmes.store.queries.get_entry_analytics", entry_mock):
            result, mock_console, _ = _run_order_cli(
                broker, sync_side_effect=sync,
                cli_args=[*_SELL_CLI_ARGS, "--prob", "0.55"],
            )
        assert result.exit_code == 0, _printed(mock_console)
        t = captured["trade"]
        # Close-time prob/edge, no inherited analytics
        assert t.model_probability == 0.55
        assert t.edge == pytest.approx(0.55 - t.price)
        assert t.gimme_score == 0.0
        assert t.kelly_fraction == 0.0
        entry_mock.assert_not_awaited()

    def test_open_path_unchanged_no_entry_fetch(self) -> None:
        broker = _make_mock_broker()
        captured, sync = self._capture()
        entry_mock = AsyncMock(return_value=dict(_ENTRY))
        # The buy path fetches a thesis before building the trade —
        # stub those DB lookups (they'd run against the mock DB).
        with patch("gimmes.store.queries.get_entry_analytics", entry_mock), \
                patch(
                    "gimmes.store.queries.get_thesis_for_ticker",
                    AsyncMock(return_value=""),
                ), \
                patch(
                    "gimmes.store.queries.get_open_trade_for_ticker",
                    AsyncMock(return_value=None),
                ):
            result, mock_console, _ = _run_order_cli(
                broker, sync_side_effect=sync,
            )
        assert result.exit_code == 0, _printed(mock_console)
        t = captured["trade"]
        assert t.action is TradeDecision.Action.OPEN
        # _ORDER_CLI_ARGS pass --prob 0.55: open semantics untouched
        assert t.model_probability == 0.55
        assert t.gimme_score == 0.0
        entry_mock.assert_not_awaited()

    def test_entry_fetch_failure_still_records_close(self) -> None:
        """A transient DB error in the best-effort analytics fetch must
        not become 'position sync failed' — the close records with
        zeroed analytics (#656 Copilot review)."""
        broker = self._broker_with_position()
        captured, sync = self._capture()
        entry_mock = AsyncMock(side_effect=sqlite3.OperationalError("locked"))
        with patch("gimmes.store.queries.get_entry_analytics", entry_mock):
            result, mock_console, _ = _run_order_cli(
                broker, sync_side_effect=sync, cli_args=_SELL_CLI_ARGS,
            )
        assert result.exit_code == 0, _printed(mock_console)
        t = captured["trade"]
        assert t.action is TradeDecision.Action.CLOSE
        assert t.model_probability == 0.0
        assert t.edge == 0.0
        assert "position sync failed" not in _printed(mock_console).lower()
