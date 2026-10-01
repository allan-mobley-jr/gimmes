"""Unit tests for Kalshi order parsing and request body construction."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gimmes.kalshi.orders import _parse_fill, _parse_order, create_order
from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide


class TestParseOrder:
    def test_parses_dollar_string_prices(self) -> None:
        data = {
            "order_id": "ord-123",
            "ticker": "KXBTC-26MAR14-T100000",
            "action": "buy",
            "side": "yes",
            "status": "resting",
            "yes_price_dollars": "0.5500",
            "no_price_dollars": "0.4500",
            "initial_count_fp": "10.00",
            "remaining_count_fp": "5.00",
            "client_order_id": "uuid-abc",
        }
        order = _parse_order(data)
        assert order.order_id == "ord-123"
        assert order.yes_price == 0.55
        assert order.no_price == 0.45
        assert order.count == 10
        assert order.remaining_count == 5

    def test_handles_float_precision(self) -> None:
        """Dollar strings parse to standard float representation."""
        data = {
            "order_id": "ord-456",
            "ticker": "TEST",
            "action": "buy",
            "side": "yes",
            "yes_price_dollars": "0.2900",
            "no_price_dollars": "0.7100",
            "initial_count_fp": "1.00",
            "remaining_count_fp": "0.00",
        }
        order = _parse_order(data)
        assert order.yes_price == 0.29
        assert order.no_price == 0.71

    def test_handles_missing_fields(self) -> None:
        data = {"order_id": "ord-789", "ticker": "TEST"}
        order = _parse_order(data)
        assert order.yes_price == 0.0
        assert order.no_price == 0.0
        assert order.count == 0
        assert order.remaining_count == 0
        assert order.status == ""

    def test_parses_sell_action(self) -> None:
        data = {
            "order_id": "ord-sell",
            "ticker": "TEST",
            "action": "sell",
            "side": "no",
            "yes_price_dollars": "0.3000",
            "no_price_dollars": "0.7000",
            "initial_count_fp": "3.00",
            "remaining_count_fp": "0.00",
        }
        order = _parse_order(data)
        assert order.action == OrderAction.SELL
        assert order.side == OrderSide.NO


class TestParseFill:
    def test_parses_fill_with_dollar_strings(self) -> None:
        data = {
            "trade_id": "fill-123",
            "order_id": "ord-123",
            "ticker": "TEST",
            "action": "buy",
            "side": "yes",
            "count_fp": "5.00",
            "yes_price_dollars": "0.6500",
            "no_price_dollars": "0.3500",
            "is_taker": True,
        }
        fill = _parse_fill(data)
        assert fill.trade_id == "fill-123"
        assert fill.count == 5
        assert fill.yes_price == 0.65
        assert fill.no_price == 0.35
        assert fill.is_taker is True

    def test_handles_float_precision(self) -> None:
        data = {
            "trade_id": "fill-456",
            "order_id": "ord-456",
            "ticker": "TEST",
            "action": "buy",
            "side": "yes",
            "count_fp": "1.00",
            "yes_price_dollars": "0.1900",
            "no_price_dollars": "0.8100",
        }
        fill = _parse_fill(data)
        assert fill.yes_price == 0.19
        assert fill.no_price == 0.81

    def test_falls_back_to_count_field(self) -> None:
        """When count_fp is missing, fall back to count."""
        data = {
            "trade_id": "fill-789",
            "ticker": "TEST",
            "action": "buy",
            "side": "yes",
            "count": "3",
            "yes_price_dollars": "0.5000",
        }
        fill = _parse_fill(data)
        assert fill.count == 3


def _get_post_body(mock_client: AsyncMock) -> dict:
    """Extract the JSON body from the mock client's most recent POST call."""
    call = mock_client.post.call_args
    return call.kwargs.get("json") or call[1].get("json")


class TestCreateOrderRequestBody:
    async def test_sends_dollar_string_fields(self) -> None:
        """Verify request body uses yes_price_dollars and count_fp."""
        mock_client = AsyncMock()
        mock_client.post.return_value = {
            "order": {
                "order_id": "ord-new",
                "ticker": "TEST-TICKER",
                "action": "buy",
                "side": "yes",
                "status": "resting",
                "yes_price_dollars": "0.7000",
                "no_price_dollars": "0.3000",
                "initial_count_fp": "10.00",
                "remaining_count_fp": "10.00",
                "client_order_id": "test-uuid",
            }
        }

        params = CreateOrderParams(
            ticker="TEST-TICKER",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=10,
            yes_price=0.70,
            post_only=True,
        )

        order = await create_order(mock_client, params)

        call_args = mock_client.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")

        assert "yes_price_dollars" in body
        assert body["yes_price_dollars"] == "0.7000"
        assert "yes_price" not in body

        assert "count_fp" in body
        assert body["count_fp"] == "10.00"
        assert "count" not in body

        assert body["post_only"] is True
        assert order.order_id == "ord-new"
        assert order.yes_price == 0.70

    async def test_sends_no_price_dollars(self) -> None:
        mock_client = AsyncMock()
        mock_client.post.return_value = {
            "order": {
                "order_id": "ord-no",
                "ticker": "TEST",
                "action": "buy",
                "side": "no",
                "no_price_dollars": "0.4000",
                "initial_count_fp": "5.00",
                "remaining_count_fp": "5.00",
            }
        }

        params = CreateOrderParams(
            ticker="TEST",
            action=OrderAction.BUY,
            side=OrderSide.NO,
            count=5,
            no_price=0.40,
        )

        await create_order(mock_client, params)
        body = _get_post_body(mock_client)

        assert "no_price_dollars" in body
        assert body["no_price_dollars"] == "0.4000"
        assert "no_price" not in body

    async def test_omits_price_when_none(self) -> None:
        mock_client = AsyncMock()
        mock_client.post.return_value = {
            "order": {
                "order_id": "ord-mkt",
                "ticker": "TEST",
                "action": "buy",
                "side": "yes",
                "initial_count_fp": "1.00",
                "remaining_count_fp": "1.00",
            }
        }

        params = CreateOrderParams(
            ticker="TEST",
            action=OrderAction.BUY,
            side=OrderSide.YES,
            count=1,
        )

        await create_order(mock_client, params)
        body = _get_post_body(mock_client)

        assert "yes_price_dollars" not in body
        assert "no_price_dollars" not in body

    async def test_generates_client_order_id(self) -> None:
        mock_client = AsyncMock()
        mock_client.post.return_value = {
            "order": {
                "order_id": "ord-id",
                "ticker": "TEST",
                "action": "buy",
                "side": "yes",
                "initial_count_fp": "1.00",
                "remaining_count_fp": "1.00",
            }
        }

        params = CreateOrderParams(
            ticker="TEST", count=1, side=OrderSide.YES,
        )

        await create_order(mock_client, params)
        body = _get_post_body(mock_client)

        assert "client_order_id" in body
        assert len(body["client_order_id"]) == 36

    async def test_dollar_precision(self) -> None:
        """Verify dollar values convert to correct 4-decimal strings."""
        mock_client = AsyncMock()
        mock_client.post.return_value = {
            "order": {
                "order_id": "ord-prec",
                "ticker": "TEST",
                "action": "buy",
                "side": "yes",
                "yes_price_dollars": "0.0100",
                "initial_count_fp": "100.00",
                "remaining_count_fp": "100.00",
            }
        }

        test_cases = [
            (0.01, "0.0100"),
            (0.50, "0.5000"),
            (0.99, "0.9900"),
            (0.05, "0.0500"),
        ]

        for price_dollars, expected_str in test_cases:
            params = CreateOrderParams(
                ticker="TEST", count=100, yes_price=price_dollars,
            )
            await create_order(mock_client, params)
            body = _get_post_body(mock_client)
            actual = body["yes_price_dollars"]
            assert actual == expected_str, (
                f"price={price_dollars}: expected {expected_str},"
                f" got {actual}"
            )


class TestExpirationTs:
    _RESPONSE = {
        "order": {
            "order_id": "ord-exp",
            "ticker": "TEST",
            "action": "buy",
            "side": "no",
            "no_price_dollars": "0.4000",
            "initial_count_fp": "5.00",
            "remaining_count_fp": "5.00",
        }
    }

    async def test_sends_expiration_ts_when_set(self) -> None:
        mock_client = AsyncMock()
        mock_client.post.return_value = self._RESPONSE

        params = CreateOrderParams(
            ticker="TEST",
            action=OrderAction.BUY,
            side=OrderSide.NO,
            count=5,
            no_price=0.40,
            post_only=False,
            expiration_ts=1_800_000_000,
        )
        await create_order(mock_client, params)
        body = _get_post_body(mock_client)
        assert body["expiration_ts"] == 1_800_000_000

    async def test_omits_expiration_ts_by_default(self) -> None:
        mock_client = AsyncMock()
        mock_client.post.return_value = self._RESPONSE

        params = CreateOrderParams(
            ticker="TEST",
            action=OrderAction.BUY,
            side=OrderSide.NO,
            count=5,
            no_price=0.40,
        )
        await create_order(mock_client, params)
        body = _get_post_body(mock_client)
        assert "expiration_ts" not in body


class TestParseOrderAvgFillPrice:
    """#834: Kalshi reports fill cost + fill count; their ratio is the
    VWAP the ledger books. Anything missing or inconsistent → None."""

    _base = {
        "order_id": "ord-fill",
        "ticker": "TEST",
        "action": "buy",
        "side": "no",
        "status": "executed",
        "yes_price_dollars": "0.3000",
        "no_price_dollars": "0.7000",
        "initial_count_fp": "10.00",
        "remaining_count_fp": "0.00",
    }

    def test_taker_fill_cost_yields_vwap(self) -> None:
        data = {
            **self._base,
            "fill_count_fp": "10.00",
            "taker_fill_cost_dollars": "4.0000",  # filled at 0.40, cap 0.70
            "maker_fill_cost_dollars": "0.0000",
        }
        assert _parse_order(data).avg_fill_price == pytest.approx(0.40)

    def test_taker_and_maker_legs_combine(self) -> None:
        data = {
            **self._base,
            "fill_count_fp": "10.00",
            "taker_fill_cost_dollars": "2.4000",  # 6 @ 0.40
            "maker_fill_cost_dollars": "2.8000",  # 4 @ 0.70
        }
        assert _parse_order(data).avg_fill_price == pytest.approx(0.52)

    def test_no_fill_fields_means_none(self) -> None:
        assert _parse_order(dict(self._base)).avg_fill_price is None

    def test_zero_fill_count_means_none(self) -> None:
        data = {
            **self._base,
            "fill_count_fp": "0.00",
            "taker_fill_cost_dollars": "0.0000",
        }
        assert _parse_order(data).avg_fill_price is None

    def test_out_of_range_ratio_means_none(self) -> None:
        # A cost that implies > $1/contract is not a price — refuse it
        # rather than book a fabricated entry.
        data = {
            **self._base,
            "fill_count_fp": "10.00",
            "taker_fill_cost_dollars": "12.0000",
        }
        assert _parse_order(data).avg_fill_price is None

    def test_garbage_fill_fields_mean_none(self) -> None:
        data = {**self._base, "fill_count_fp": "n/a", "taker_fill_cost_dollars": "x"}
        assert _parse_order(data).avg_fill_price is None

    def test_fees_sum_taker_and_maker_legs(self) -> None:
        data = {
            **self._base,
            "taker_fees_dollars": "0.2100",
            "maker_fees_dollars": "0.0400",
        }
        assert _parse_order(data).fill_fees == pytest.approx(0.25)

    def test_single_fee_leg_counts_and_no_legs_means_none(self) -> None:
        data = {**self._base, "taker_fees_dollars": "0.2100"}
        assert _parse_order(data).fill_fees == pytest.approx(0.21)
        assert _parse_order(dict(self._base)).fill_fees is None

    def test_unparseable_fee_field_means_none(self) -> None:
        data = {**self._base, "taker_fees_dollars": "x", "maker_fees_dollars": "0.04"}
        assert _parse_order(data).fill_fees is None

    def test_filled_without_fill_data_warns(self, caplog) -> None:
        # count 10, remaining 0 → filled, but no cost/fee fields.
        import logging

        with caplog.at_level(logging.WARNING, logger="gimmes.kalshi.orders"):
            order = _parse_order(dict(self._base))
        assert order.avg_fill_price is None
        assert order.fill_price == pytest.approx(0.70)  # NO limit fallback
        assert any("#834" in r.message for r in caplog.records)

    def test_unfilled_without_fill_data_is_quiet(self, caplog) -> None:
        import logging

        data = {**self._base, "status": "resting", "remaining_count_fp": "10.00"}
        with caplog.at_level(logging.WARNING, logger="gimmes.kalshi.orders"):
            _parse_order(data)
        assert not caplog.records

    def test_buy_fill_above_limit_is_refused(self) -> None:
        # A BUY can't fill above its own limit — a cost implying it is a
        # cost quoted on the other side or in the wrong unit.
        data = {
            **self._base,
            "fill_count_fp": "10.00",
            "taker_fill_cost_dollars": "7.5000",  # 0.75 > NO limit 0.70
        }
        assert _parse_order(data).avg_fill_price is None

    @pytest.mark.parametrize("cost", ["8.5000", "2.0000"])
    def test_sell_never_derives_a_vwap(self, cost: str, caplog) -> None:
        # Kalshi doesn't document which side a sell's cost is quoted on;
        # the buy-NO-equivalent cost of a SELL YES @ 0.80 would be 0.20
        # and booking it would turn a winning exit into a recorded loss.
        # Until confirmed live, a sell books its limit — quietly.
        import logging

        data = {
            **self._base,
            "action": "sell",
            "side": "yes",
            "yes_price_dollars": "0.8000",
            "no_price_dollars": "0.2000",
            "fill_count_fp": "10.00",
            "taker_fill_cost_dollars": cost,
        }
        with caplog.at_level(logging.WARNING, logger="gimmes.kalshi.orders"):
            order = _parse_order(data)
        assert order.avg_fill_price is None
        assert order.fill_price == pytest.approx(0.80)
        assert not caplog.records
