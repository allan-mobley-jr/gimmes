"""Pure fill simulation logic — no DB, no side effects.

Simulates how Kalshi would fill an order given the current orderbook.
"""

from __future__ import annotations

from dataclasses import dataclass

from gimmes.models.market import Orderbook
from gimmes.models.order import CreateOrderParams, OrderAction, OrderSide
from gimmes.strategy.fees import fee_for_order


@dataclass
class SimulatedFill:
    """Result of a simulated fill against the orderbook."""

    count: int
    price_cents: int  # Price in cents
    fee: float  # Fee in dollars
    is_taker: bool


@dataclass
class FillResult:
    """Full result of simulating an order."""

    fills: list[SimulatedFill]
    remaining_count: int  # Unfilled contracts that rest on the book
    total_filled: int
    total_notional: float  # Sum of count * price across fills (always positive)
    total_fees: float  # Sum of fees across fills (always positive)


def simulate_fill(params: CreateOrderParams, orderbook: Orderbook) -> FillResult:
    """Simulate filling an order against the current orderbook.

    For maker orders (post_only=True):
        - If limit price is at or better than best opposing level, the order
          fills immediately at the limit price (price improvement).
        - Otherwise the order rests unfilled.

    For taker orders:
        - Walks the orderbook, filling against available liquidity up to
          the limit price.
    """
    price_cents = params.price_cents
    price_dollars = price_cents / 100.0

    if params.post_only:
        return _simulate_maker_fill(params, orderbook, price_cents, price_dollars)
    return _simulate_taker_fill(params, orderbook, price_cents, price_dollars)


def _simulate_maker_fill(
    params: CreateOrderParams,
    orderbook: Orderbook,
    price_cents: int,
    price_dollars: float,
) -> FillResult:
    """Maker order: fills at limit price if marketable, otherwise rests."""
    # Determine which side of the book we'd match against
    if params.action == OrderAction.BUY:
        if params.side == OrderSide.YES:
            # Buying YES: matches against NO bids (which represent YES asks)
            # YES ask = 1 - NO bid price
            best_ask = orderbook.best_yes_ask
            marketable = best_ask is not None and price_dollars >= best_ask
        else:
            # Buying NO: matches against YES bids (which represent NO asks)
            # NO ask = 1 - YES bid price
            best_no_ask = (
                round(1.0 - orderbook.yes_bids[0].price, 2)
                if orderbook.yes_bids
                else None
            )
            marketable = best_no_ask is not None and price_dollars >= best_no_ask
    else:
        # Selling: matches against bids on the same side
        if params.side == OrderSide.YES:
            best_bid = orderbook.best_yes_bid
            marketable = best_bid is not None and price_dollars <= best_bid
        else:
            best_no_bid = orderbook.no_bids[0].price if orderbook.no_bids else None
            marketable = best_no_bid is not None and price_dollars <= best_no_bid

    if not marketable:
        # Order rests on the book
        return FillResult(
            fills=[], remaining_count=params.count, total_filled=0,
            total_notional=0.0, total_fees=0.0,
        )

    # Maker fill at limit price (not taker even though marketable — post_only guarantee)
    fee = fee_for_order(params.count, price_dollars, is_taker=False)
    fill = SimulatedFill(
        count=params.count,
        price_cents=price_cents,
        fee=fee,
        is_taker=False,
    )
    notional = params.count * price_dollars
    return FillResult(
        fills=[fill], remaining_count=0, total_filled=params.count,
        total_notional=notional, total_fees=fee,
    )


def _simulate_taker_fill(
    params: CreateOrderParams,
    orderbook: Orderbook,
    price_cents: int,
    price_dollars: float,
) -> FillResult:
    """Taker order: walks the book until filled or limit price exceeded."""
    fills: list[SimulatedFill] = []
    remaining = params.count
    total_notional = 0.0
    total_fees = 0.0

    # Get the levels to walk
    if params.action == OrderAction.BUY:
        if params.side == OrderSide.YES:
            # Walk NO bids (converted to YES ask prices)
            levels = [
                (round(1.0 - lvl.price, 2), lvl.quantity)
                for lvl in orderbook.no_bids
            ]
            # Sort ascending — cheapest ask first
            levels.sort(key=lambda x: x[0])
        else:
            # Buying NO: walk YES bids (converted to NO ask prices)
            levels = [
                (round(1.0 - lvl.price, 2), lvl.quantity)
                for lvl in orderbook.yes_bids
            ]
            levels.sort(key=lambda x: x[0])
    else:
        # Selling: walk bids on the same side
        if params.side == OrderSide.YES:
            levels = [(lvl.price, lvl.quantity) for lvl in orderbook.yes_bids]
            # Sort descending — best bid first
            levels.sort(key=lambda x: x[0], reverse=True)
        else:
            levels = [(lvl.price, lvl.quantity) for lvl in orderbook.no_bids]
            levels.sort(key=lambda x: x[0], reverse=True)

    for level_price, level_qty in levels:
        if remaining <= 0:
            break

        # Check limit: for buys, level price must be <= limit; for sells, >= limit
        if params.action == OrderAction.BUY and level_price > price_dollars:
            break
        if params.action == OrderAction.SELL and level_price < price_dollars:
            break

        fill_count = min(remaining, level_qty)
        fill_price_cents = int(round(level_price * 100))
        fee = fee_for_order(fill_count, level_price, is_taker=True)

        fills.append(
            SimulatedFill(
                count=fill_count,
                price_cents=fill_price_cents,
                fee=fee,
                is_taker=True,
            )
        )
        total_notional += fill_count * level_price
        total_fees += fee
        remaining -= fill_count

    total_filled = params.count - remaining
    return FillResult(
        fills=fills,
        remaining_count=remaining,
        total_filled=total_filled,
        total_notional=total_notional,
        total_fees=total_fees,
    )
