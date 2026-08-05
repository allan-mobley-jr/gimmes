"""Tests for Orderbook.depth_snapshot — displayed depth at placement (#762).

Depth is judged in the ORDER's own side terms: a NO buyer's asks are
implied from YES bids (1 - bid) and vice versa. Volume/OI say nothing
about instantaneous depth at the limit — the 2026-08-04 fill sized 686
against a 1-contract touch.
"""

from __future__ import annotations

from collections.abc import Sequence

from gimmes.models.market import Orderbook, OrderbookLevel


def _book(
    yes_bids: Sequence[tuple[float, int]] = (),
    no_bids: Sequence[tuple[float, int]] = (),
) -> Orderbook:
    return Orderbook(
        ticker="KXTEST-MKT",
        yes_bids=[
            OrderbookLevel(price=p, quantity=q) for p, q in yes_bids
        ],
        no_bids=[
            OrderbookLevel(price=p, quantity=q) for p, q in no_bids
        ],
    )


class TestDepthSnapshot:
    def test_no_buyer_reads_yes_bids(self) -> None:
        # yes_bid 0.42 -> implied NO ask 0.58 (touch, 1 contract);
        # yes_bid 0.40 -> implied NO ask 0.60 (outside a 0.58 limit).
        book = _book(yes_bids=[(0.42, 1), (0.40, 500)])
        touch, executable, best_ask = book.depth_snapshot("no", 0.58)
        assert touch == 1
        assert executable == 1
        assert best_ask == 0.58

    def test_limit_above_second_level_includes_it(self) -> None:
        book = _book(yes_bids=[(0.42, 1), (0.40, 500)])
        touch, executable, best_ask = book.depth_snapshot("no", 0.60)
        assert touch == 1
        assert executable == 501
        assert best_ask == 0.58

    def test_yes_buyer_reads_no_bids(self) -> None:
        # no_bid 0.55 -> implied YES ask 0.45 (touch, 30 contracts)
        book = _book(
            yes_bids=[(0.40, 99)], no_bids=[(0.55, 30), (0.50, 70)],
        )
        touch, executable, best_ask = book.depth_snapshot("yes", 0.45)
        assert touch == 30
        assert executable == 30
        assert best_ask == 0.45

    def test_touch_found_by_price_not_array_position(self) -> None:
        # The parser copies API arrays verbatim with no ordering
        # contract — a best-LAST (ascending) book must still report
        # the true touch, not the far end of the ladder
        # (review-found: [0] would report 5000 @ $0.99 here).
        book = _book(yes_bids=[(0.01, 5000), (0.40, 500), (0.42, 1)])
        touch, executable, best_ask = book.depth_snapshot("no", 0.58)
        assert touch == 1
        assert executable == 1
        assert best_ask == 0.58

    def test_one_sided_book_is_zero_depth(self) -> None:
        # NO buyer with no YES bids anywhere: nothing to take.
        book = _book(no_bids=[(0.55, 30)])
        touch, executable, best_ask = book.depth_snapshot("no", 0.58)
        assert touch == 0
        assert executable == 0
        assert best_ask is None

    def test_limit_below_best_ask_is_zero_executable(self) -> None:
        # Book displayed but the limit misses it — the rest-on-miss
        # shape: touch is visible, executable is zero.
        book = _book(yes_bids=[(0.42, 100)])
        touch, executable, best_ask = book.depth_snapshot("no", 0.50)
        assert touch == 100
        assert executable == 0
        assert best_ask == 0.58

    def test_zero_quantity_touch_level(self) -> None:
        # The parser does not filter zero-quantity levels: touch
        # reports the DISPLAYED top level (possibly a stale zero)
        # while executable counts the real depth behind it.
        book = _book(yes_bids=[(0.42, 0), (0.40, 500)])
        touch, executable, best_ask = book.depth_snapshot("no", 0.60)
        assert touch == 0
        assert executable == 500
        assert best_ask == 0.58
