"""CLI regression tests for Rich markup escaping of external text (#644).

Kalshi titles, CLI args, and settlement text can contain bracketed
segments (`[preliminary]`, `[sole discretion, ...]`) that Rich eats as
style tags unless escaped. Each test pins a bracket fragment surviving
in the rendered output — fragments are chosen with no internal spaces
so Rich word-wrap can't split them (the #641/#642 pattern), and
bracket segments start lowercase because uppercase-start segments
aren't parsed as tags and wouldn't reproduce the bug.

The `size` command's title is covered by the central format_kv_table
escape (test_formatter.py) — no per-command harness here per the #644
plan (its 7-patch mock stack isn't worth a CLI-arg-only title).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from gimmes.cli import app

runner = CliRunner()


def _config(tmp_path: Path) -> MagicMock:
    c = MagicMock()
    c.db_path = tmp_path / "test.db"
    return c


def _stub_market() -> MagicMock:
    m = MagicMock()
    m.ticker = "KXCPI-26APR-T0.5"
    m.event_ticker = "KXCPI-26APR"
    m.series_ticker = "KXCPI"
    m.title = "CPI [preliminary] April 2026"
    m.subtitle = ""
    m.status = MagicMock()
    m.status.value = "active"
    m.yes_bid = 0.50
    m.yes_ask = 0.55
    m.no_bid = 0.45
    m.no_ask = 0.50
    m.last_price = 0.52
    m.midpoint = 0.52
    m.spread = 0.05
    m.volume = 100
    m.volume_24h = 50
    m.open_interest = 30
    m.close_time = None
    m.rules_primary = ""
    return m


def test_score_title_brackets(tmp_path: Path) -> None:
    with patch("gimmes.cli.load_config", return_value=_config(tmp_path)), \
         patch("gimmes.kalshi.client.KalshiClient"), \
         patch(
             "gimmes.kalshi.markets.get_market",
             AsyncMock(return_value=_stub_market()),
         ), \
         patch(
             "gimmes.kalshi.markets.get_orderbook",
             AsyncMock(return_value=MagicMock(yes_bids=[], yes_asks=[])),
         ), \
         patch(
             "gimmes.strategy.scorer.quick_score",
             MagicMock(return_value=85.0),
         ):
        result = runner.invoke(app, ["score", "KXCPI-26APR-T0.5"])
    assert result.exit_code == 0, result.output
    assert "[preliminary]" in result.output
    assert "\\[preliminary]" not in result.output


def test_candidates_title_brackets(tmp_path: Path) -> None:
    row = {
        "ticker": "KXCPI-26APR-T0.5", "gimme_score": 70.0,
        "market_price": 0.5, "model_probability": 0.6, "edge": 0.1,
        "cap_blocked": 0, "recommendation": "proceed",
        "scanned_at": "2026-07-01 12:00:00",
    }
    with patch("gimmes.store.database.Database", MagicMock()), \
         patch(
             "gimmes.store.queries.get_candidate_for_ticker",
             AsyncMock(return_value=[row]),
         ), patch("gimmes.cli.load_config", return_value=_config(tmp_path)):
        result = runner.invoke(
            app, ["candidates", "--ticker", "kxcpi [draft]"],
        )
    assert "[draft]" in result.output, result.output


def test_candidates_memo_panel_renders_markup_literally(tmp_path) -> None:
    """#676: the memo panel prints with markup=False — bracketed memo
    text must render literally, not vanish as Rich markup (#644)."""
    row = {
        "ticker": "KXCPI-26APR-T0.5", "gimme_score": 70.0,
        "market_price": 0.5, "model_probability": 0.6, "edge": 0.1,
        "cap_blocked": 0, "recommendation": "proceed",
        "scanned_at": "2026-07-01 12:00:00",
        "research_memo": "sources say [red]hot[/red] print likely",
    }
    with patch("gimmes.store.database.Database", MagicMock()), \
         patch(
             "gimmes.store.queries.get_candidate_for_ticker",
             AsyncMock(return_value=[row]),
         ), patch("gimmes.cli.load_config", return_value=_config(tmp_path)):
        result = runner.invoke(
            app, ["candidates", "--ticker", "KXCPI-26APR-T0.5"],
        )
    assert "[red]hot[/red]" in result.output, result.output


def test_discover_category_brackets(tmp_path: Path) -> None:
    series = [{"ticker": "KXCPI", "title": "CPI [preliminary] index"}]
    with patch("gimmes.cli.load_config", return_value=_config(tmp_path)), \
         patch("gimmes.kalshi.client.KalshiClient"), \
         patch(
             "gimmes.kalshi.markets.list_series",
             AsyncMock(return_value=series),
         ):
        result = runner.invoke(app, ["discover", "econ [test]"])
    assert result.exit_code == 0, result.output
    # The category appears on BOTH the found-line and the table title —
    # count >= 2 so reverting either escape is caught.
    assert result.output.count("[test]") >= 2, result.output
    assert "[preliminary]" in result.output


def test_validate_failure_messages_brackets(tmp_path: Path) -> None:
    """Settlement red-flag lists render as literal bracketed text in
    validation failures — they must survive the styled output lines."""
    from gimmes.risk.validator import ValidationResult

    cfg = _config(tmp_path)
    cfg.bankroll = 1000.0
    cfg.strategy.side = "yes"
    vr = ValidationResult(
        approved=False,
        checks=["Edge OK [margin ok]"],
        failures=[
            "Settlement risk HIGH: Settlement risk (high):"
            " found [sole discretion, may determine]"
        ],
    )
    with patch("gimmes.cli.load_config", return_value=cfg), \
         patch("gimmes.kalshi.client.KalshiClient"), \
         patch("gimmes.strategy.fee_cache.refresh_fee_cache", AsyncMock()), \
         patch(
             "gimmes.kalshi.markets.get_market",
             AsyncMock(return_value=_stub_market()),
         ), \
         patch(
             "gimmes.kalshi.portfolio.get_all_positions",
             AsyncMock(return_value=[]),
         ), \
         patch("gimmes.store.queries.get_daily_pnl", AsyncMock(return_value=0.0)), \
         patch(
             "gimmes.store.queries.get_deployed_cost_basis",
             AsyncMock(return_value=0.0),
         ), \
         patch(
             "gimmes.risk.validator.validate_trade",
             MagicMock(return_value=vr),
         ):
        result = runner.invoke(app, [
            "validate", "KXCPI-26APR-T0.5",
            "--prob", "0.9", "--dollars", "10",
        ])
    assert result.exit_code == 1, result.output
    # The fragment appears on BOTH the summary line and the per-failure
    # line — count >= 2 so reverting either escape is caught (the
    # summary embeds the same failure text, making a single presence
    # check blind to the per-line escape).
    assert result.output.count("[sole") >= 2, result.output
    assert "determine]" in result.output
    # F6 (#644): the checks line and header ticker are escaped too.
    assert "[margin" in result.output
