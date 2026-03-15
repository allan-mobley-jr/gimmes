"""Tests for the _mark_positions_to_market CLI helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gimmes.models.portfolio import Position


def _pos(ticker: str = "KXTEST", unrealized_pnl: float = 0.0) -> Position:
    return Position(ticker=ticker, count=10, avg_price=0.50, unrealized_pnl=unrealized_pnl)


def _market(midpoint: float = 0.65) -> MagicMock:
    m = MagicMock()
    m.midpoint = midpoint
    m.last_price = midpoint
    return m


class TestMarkPositionsToMarket:
    async def test_marks_each_position(self) -> None:
        broker = AsyncMock()
        broker.get_positions = AsyncMock(return_value=[_pos("A"), _pos("B")])
        broker.mark_to_market = AsyncMock()
        client = AsyncMock()

        with patch("gimmes.kalshi.markets.get_market", AsyncMock(return_value=_market(0.70))):
            from gimmes.cli import _mark_positions_to_market

            await _mark_positions_to_market(broker, client)

        assert broker.mark_to_market.call_count == 2
        broker.mark_to_market.assert_any_call("A", 0.70)
        broker.mark_to_market.assert_any_call("B", 0.70)

    async def test_known_prices_skip_api_call(self) -> None:
        broker = AsyncMock()
        broker.get_positions = AsyncMock(return_value=[_pos("A"), _pos("B")])
        broker.mark_to_market = AsyncMock()
        client = AsyncMock()
        mock_get_market = AsyncMock(return_value=_market(0.80))

        with patch("gimmes.kalshi.markets.get_market", mock_get_market):
            from gimmes.cli import _mark_positions_to_market

            await _mark_positions_to_market(
                broker, client, known_prices={"A": 0.65},
            )

        # A uses known price, B fetches from API
        broker.mark_to_market.assert_any_call("A", 0.65)
        broker.mark_to_market.assert_any_call("B", 0.80)
        # get_market called only for B
        assert mock_get_market.call_count == 1

    async def test_error_does_not_abort_loop(self) -> None:
        broker = AsyncMock()
        broker.get_positions = AsyncMock(return_value=[_pos("A"), _pos("BAD"), _pos("C")])
        broker.mark_to_market = AsyncMock()
        client = AsyncMock()

        def _market_or_fail(client, ticker):
            if ticker == "BAD":
                raise httpx.ConnectError("connection refused")
            return _market(0.70)

        mock_console = MagicMock()
        with (
            patch("gimmes.kalshi.markets.get_market", AsyncMock(side_effect=_market_or_fail)),
            patch("gimmes.cli.console", mock_console),
        ):
            from gimmes.cli import _mark_positions_to_market

            await _mark_positions_to_market(broker, client)

        # A and C were marked, BAD was skipped
        assert broker.mark_to_market.call_count == 2
        broker.mark_to_market.assert_any_call("A", 0.70)
        broker.mark_to_market.assert_any_call("C", 0.70)
        # Warning printed for BAD
        warning_text = str(mock_console.print.call_args_list)
        assert "BAD" in warning_text

    async def test_empty_positions(self) -> None:
        broker = AsyncMock()
        broker.get_positions = AsyncMock(return_value=[])
        broker.mark_to_market = AsyncMock()
        client = AsyncMock()

        with patch("gimmes.kalshi.markets.get_market", AsyncMock()) as mock_gm:
            from gimmes.cli import _mark_positions_to_market

            result = await _mark_positions_to_market(broker, client)

        assert result == []
        mock_gm.assert_not_called()
        broker.mark_to_market.assert_not_called()

    async def test_returns_refreshed_positions(self) -> None:
        """After marking, the function re-fetches to get updated P&L."""
        stale = [_pos("A", unrealized_pnl=0.0)]
        fresh = [_pos("A", unrealized_pnl=1.50)]
        broker = AsyncMock()
        broker.get_positions = AsyncMock(side_effect=[stale, fresh])
        broker.mark_to_market = AsyncMock()
        client = AsyncMock()

        with patch("gimmes.kalshi.markets.get_market", AsyncMock(return_value=_market(0.65))):
            from gimmes.cli import _mark_positions_to_market

            result = await _mark_positions_to_market(broker, client)

        # Returns the second (refreshed) fetch
        assert result[0].unrealized_pnl == pytest.approx(1.50)
        assert broker.get_positions.call_count == 2
