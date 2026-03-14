"""P&L calculation from trade history."""

from __future__ import annotations

from dataclasses import dataclass

from gimmes.strategy.fees import fee_for_order


@dataclass
class PnLSummary:
    """Profit and loss summary."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    scratch_trades: int = 0
    gross_pnl: float = 0.0
    total_fees: float = 0.0
    net_pnl: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    @property
    def win_rate(self) -> float:
        completed = self.winning_trades + self.losing_trades
        if completed == 0:
            return 0.0
        return self.winning_trades / completed


def calculate_pnl(trades: list[dict]) -> PnLSummary:  # type: ignore[type-arg]  # accepts TradeRecord dicts
    """Calculate P&L from a list of trade records.

    Args:
        trades: List of trade dicts from the database.
    """
    summary = PnLSummary()

    # Group trades by ticker to match opens with closes
    opens: dict[str, list[dict]] = {}  # type: ignore[type-arg]
    closes: dict[str, list[dict]] = {}  # type: ignore[type-arg]

    for t in trades:
        action = t.get("action", "")
        ticker = t.get("ticker", "")
        if action == "open":
            opens.setdefault(ticker, []).append(t)
        elif action == "close":
            closes.setdefault(ticker, []).append(t)

    for ticker, close_list in closes.items():
        open_list = opens.get(ticker, [])
        for close in close_list:
            # Find matching open
            open_price = open_list[0]["price"] if open_list else 0.0
            close_price = close.get("price", 0.0)
            count = close.get("count", 0)

            pnl = (close_price - open_price) * count
            # Estimate fees for both legs (open + close)
            open_fee = fee_for_order(count, open_price) if open_price > 0 else 0.0
            close_fee = fee_for_order(count, close_price) if close_price > 0 else 0.0
            summary.total_fees += open_fee + close_fee
            summary.total_trades += 1
            summary.gross_pnl += pnl

            if pnl > 0:
                summary.winning_trades += 1
                summary.largest_win = max(summary.largest_win, pnl)
            elif pnl < 0:
                summary.losing_trades += 1
                summary.largest_loss = min(summary.largest_loss, pnl)
            else:
                summary.scratch_trades += 1

    # Count open-only trades (skips and still-open)
    for ticker, open_list in opens.items():
        if ticker not in closes:
            for _ in open_list:
                summary.total_trades += 1

    summary.net_pnl = summary.gross_pnl - summary.total_fees
    return summary
