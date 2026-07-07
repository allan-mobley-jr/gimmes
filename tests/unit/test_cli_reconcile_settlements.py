"""Championship-mode reconcile consumes the authoritative portfolio
settlements endpoint (#663).

Before #663, a position that vanished from the Kalshi API between
reconciles (i.e. it settled) got an `agent='reconcile'` drift close at
its stale local mark — excluded from daily P&L (#622) and repriced
only if Monitor's log-outcome happened to land first. Now reconcile
asks GET /portfolio/settlements for the removed tickers and writes a
proper `agent='settlement'` close at the true 1.0/0.0 value; the #653
dup-guard then suppresses the drift close for those tickers. Any
settlements failure degrades to the old drift behavior — reconcile is
never blocked.

Synchronous CLI tests (asyncio.run for setup) — CliRunner drives the
command's own asyncio.run, which cannot nest. Pagination tests for
`get_settlements_for_tickers` are plain async tests at the bottom.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gimmes import cli as cli_module
from gimmes.cli import app
from gimmes.kalshi import portfolio as portfolio_module
from gimmes.models.portfolio import Position
from gimmes.models.trade import TradeDecision
from gimmes.store.database import Database
from gimmes.store.queries import get_trades, insert_trade, upsert_position

runner = CliRunner()

TICKER = "KXCPI-26APR-T0.5"
SETTLED_TIME = "2026-06-20T14:00:00.123456Z"


def _settlement_record(
    ticker: str = TICKER, market_result: str = "no",
    settled_time: str = SETTLED_TIME,
) -> dict:
    """Shape live-probed from GET /portfolio/settlements (#663)."""
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "market_result": market_result,
        "settled_time": settled_time,
        "yes_count_fp": "0",
        "no_count_fp": "10000",
        "revenue": 10_000,
        "value": 100,
    }


def _seed(db_path: Path, *, close_count: int | None = None) -> None:
    """Seed the local state a settled championship position leaves
    behind: an open trade row and a positions-table row that the next
    API sync will find absent."""

    async def _s() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await insert_trade(db, TradeDecision(
                ticker=TICKER, action=TradeDecision.Action.OPEN,
                side="no", count=100, price=0.63,
            ))
            if close_count is not None:
                await insert_trade(db, TradeDecision(
                    ticker=TICKER, action=TradeDecision.Action.CLOSE,
                    side="no", count=close_count, price=0.9,
                ))
            await upsert_position(db, Position(
                ticker=TICKER,
                title="CPI April NO",
                side="no",
                count=100,
                avg_price=0.63,
                market_price=0.705,
                cost_basis=63.0,
                market_value=70.5,
                unrealized_pnl=7.5,
                realized_pnl=0.0,
            ))
        finally:
            await db.close()

    asyncio.run(_s())


def _wire_championship(
    monkeypatch: pytest.MonkeyPatch, db_path: Path,
) -> None:
    """Point the CLI at the temp DB and route reconcile down the
    championship branch (broker=None, mocked client) with the Kalshi
    API reporting no open positions."""
    cfg = MagicMock()
    cfg.db_path = db_path
    cfg.is_championship = True
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

    @asynccontextmanager
    async def _ctx(_config):  # type: ignore[no-untyped-def]
        db = Database(db_path)
        await db.connect()
        try:
            yield MagicMock(), None, db
        finally:
            await db.close()

    monkeypatch.setattr(cli_module, "trading_context", _ctx)

    async def _no_positions(_client):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr(
        portfolio_module, "get_all_positions", _no_positions,
    )


def _closes(db_path: Path) -> list[dict]:
    async def _r() -> list[dict]:
        db = Database(db_path)
        await db.connect()
        try:
            trades = await get_trades(db, ticker=TICKER)
            return [t for t in trades if t["action"] == "close"]
        finally:
            await db.close()

    return asyncio.run(_r())


def _mock_settlements(
    monkeypatch: pytest.MonkeyPatch, records: dict[str, dict],
) -> None:
    async def _fake(_client, _tickers, **_kw):  # type: ignore[no-untyped-def]
        return records

    monkeypatch.setattr(
        portfolio_module, "get_settlements_for_tickers", _fake,
    )


def test_removed_position_closed_at_settlement_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path: the settlement record says NO won → the NO
    position gets an agent='settlement' close at 1.0 stamped with the
    settlement time, and NO reconcile drift row is written."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(monkeypatch, {TICKER: _settlement_record()})

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert "matched settlement" in result.output

    closes = _closes(db_path)
    assert len(closes) == 1
    assert closes[0]["agent"] == "settlement"
    assert closes[0]["price"] == 1.0  # NO side, market_result 'no'
    assert closes[0]["count"] == 100
    assert closes[0]["resolved_outcome"] == "no"
    # #684: stamped NOW so the daily-loss trigger sees the loss on the
    # date it reads; the record's settled_time lives in the rationale.
    from datetime import UTC, datetime

    assert str(closes[0]["timestamp"]).startswith(
        datetime.now(UTC).date().isoformat(),
    )
    assert SETTLED_TIME in str(closes[0]["rationale"])


def test_losing_side_closed_at_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch, {TICKER: _settlement_record(market_result="yes")},
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output

    closes = _closes(db_path)
    assert len(closes) == 1
    assert closes[0]["agent"] == "settlement"
    assert closes[0]["price"] == 0.0  # NO side lost
    assert closes[0]["resolved_outcome"] == "yes"


def test_no_settlement_record_falls_back_to_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A removed position with no settlement record (e.g. transferred
    or endpoint lag) keeps today's behavior: a reconcile drift close
    at the stale mark."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(monkeypatch, {})

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output

    closes = _closes(db_path)
    assert len(closes) == 1
    assert closes[0]["agent"] == "reconcile"


def test_settlements_api_failure_degrades_to_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settlements-endpoint failure must never block reconcile —
    warn and fall back to the drift close."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)

    async def _boom(_client, _tickers, **_kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("settlements endpoint down")

    monkeypatch.setattr(
        portfolio_module, "get_settlements_for_tickers", _boom,
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert "settlements lookup failed" in result.output

    closes = _closes(db_path)
    assert len(closes) == 1
    assert closes[0]["agent"] == "reconcile"


def test_unsettleable_market_result_falls_back_to_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A voided/scalar-settled record (market_result outside yes/no)
    can't be priced 1.0/0.0 — leave it to the drift close."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch, {TICKER: _settlement_record(market_result="void")},
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output

    closes = _closes(db_path)
    assert len(closes) == 1
    assert closes[0]["agent"] == "reconcile"


def test_ledger_already_covered_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Residual <= 0: the ledger already closed the opens (e.g. a
    placement-time close row) — no settlement pre-write, and the #653
    dup-guard suppresses the drift close too. Also proves the
    pre-write is crash-idempotent: a reconcile retry lands here."""
    db_path = tmp_path / "test.db"
    _seed(db_path, close_count=100)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(monkeypatch, {TICKER: _settlement_record()})

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output

    closes = _closes(db_path)
    assert len(closes) == 1  # only the pre-existing close
    assert closes[0]["agent"] != "settlement"


def test_partial_residual_clamps_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ledger already closed part of the position — the settlement
    close covers only the residual, and the ledger-vs-broker mismatch
    is warned about."""
    import logging

    db_path = tmp_path / "test.db"
    _seed(db_path, close_count=40)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(monkeypatch, {TICKER: _settlement_record()})

    with caplog.at_level(logging.WARNING, logger="gimmes"):
        result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output

    closes = _closes(db_path)
    settlement = [c for c in closes if c["agent"] == "settlement"]
    assert len(settlement) == 1
    assert settlement[0]["count"] == 60  # ledger residual, not broker 100
    assert "settlement residual mismatch" in caplog.text


def test_unparseable_settled_time_still_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed settled_time must not demote the position to a
    drift close — the settlement is still written, just without the
    backdated timestamp."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch,
        {TICKER: _settlement_record(settled_time="not-a-date")},
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output

    closes = _closes(db_path)
    assert len(closes) == 1
    assert closes[0]["agent"] == "settlement"
    assert closes[0]["price"] == 1.0


def test_off_ledger_exit_splits_settlement_and_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#684: the record says only 60 contracts settled (operator sold
    40 on the Kalshi UI) — the settlement close is clamped to 60 and
    the 40-contract remainder falls to a drift close at the mark
    (excluded from daily P&L, correct for an exit we never saw)."""
    import logging

    db_path = tmp_path / "test.db"
    _seed(db_path)  # open 100, position count 100
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch,
        {TICKER: _settlement_record() | {"no_count_fp": "6000"}},
    )

    with caplog.at_level(logging.WARNING, logger="gimmes"):
        result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert "settlement record count mismatch" in caplog.text

    closes = _closes(db_path)
    settlement = [c for c in closes if c["agent"] == "settlement"]
    drift = [c for c in closes if c["agent"] == "reconcile"]
    assert len(settlement) == 1 and settlement[0]["count"] == 60
    assert settlement[0]["price"] == 1.0
    assert len(drift) == 1 and drift[0]["count"] == 40
    assert drift[0]["price"] == pytest.approx(0.705)  # last mark


def test_decimal_count_fp_form_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orders-style decimal fp ("60.00" = 60) parses too."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch,
        {TICKER: _settlement_record() | {"no_count_fp": "60.00"}},
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    settlement = [
        c for c in _closes(db_path) if c["agent"] == "settlement"
    ]
    assert settlement[0]["count"] == 60


def test_garbage_count_fp_keeps_ledger_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unusable record count → ledger-only behavior (pre-#684)."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch,
        {TICKER: _settlement_record() | {"no_count_fp": "garbage"}},
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    settlement = [
        c for c in _closes(db_path) if c["agent"] == "settlement"
    ]
    assert settlement[0]["count"] == 100


def test_settlement_loss_lands_in_todays_daily_pnl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#684 item 2: a record settled_time on a PRIOR UTC date must not
    hide the loss from the daily-loss trigger — the close is stamped
    now."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch,
        {TICKER: _settlement_record(
            market_result="yes",  # NO side LOSES → close at 0.0
            settled_time="2026-06-20T23:59:59.000000Z",
        )},
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output

    async def _pnl() -> float:
        from gimmes.store.queries import get_daily_pnl

        db = Database(db_path)
        await db.connect()
        try:
            return await get_daily_pnl(db)
        finally:
            await db.close()

    daily = asyncio.run(_pnl())
    assert daily == pytest.approx((0.0 - 0.63) * 100)


def test_crash_on_second_ticker_preserves_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#684 item 8: commit-per-ticker — a failure on the second ticker
    keeps the first ticker's settlement close, degrades the rest to
    drift, and never blocks reconcile."""
    db_path = tmp_path / "test.db"

    async def _s() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            for tk in (TICKER, "KXSECOND-26JUL-T1"):
                await insert_trade(db, TradeDecision(
                    ticker=tk, action=TradeDecision.Action.OPEN,
                    side="no", count=100, price=0.63,
                ))
                await upsert_position(db, Position(
                    ticker=tk, title=tk, side="no", count=100,
                    avg_price=0.63, market_price=0.705,
                    cost_basis=63.0, market_value=70.5,
                    unrealized_pnl=7.5, realized_pnl=0.0,
                ))
        finally:
            await db.close()

    asyncio.run(_s())
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(monkeypatch, {
        TICKER: _settlement_record(),
        "KXSECOND-26JUL-T1": _settlement_record(
            ticker="KXSECOND-26JUL-T1",
        ),
    })

    from gimmes.store import queries as queries_module

    real = queries_module.log_settlement_close
    calls = {"n": 0}

    async def _fail_second(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("write exploded")
        return await real(*args, **kwargs)

    monkeypatch.setattr(
        queries_module, "log_settlement_close", _fail_second,
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert "settlements lookup failed" in result.output

    all_closes = _closes(db_path) + [
        c for c in asyncio.run(_all_trades(db_path))
        if c["ticker"] == "KXSECOND-26JUL-T1" and c["action"] == "close"
    ]
    first = [
        c for c in all_closes
        if c["ticker"] == TICKER and c["agent"] == "settlement"
    ]
    second_drift = [
        c for c in all_closes
        if c["ticker"] == "KXSECOND-26JUL-T1"
        and c["agent"] == "reconcile"
    ]
    assert len(first) == 1  # commit-per-ticker held
    assert len(second_drift) == 1  # degraded to drift


def _all_trades(db_path: Path):  # type: ignore[no-untyped-def]
    async def _r():
        db = Database(db_path)
        await db.connect()
        try:
            return await get_trades(db, limit=1000)
        finally:
            await db.close()

    return _r()


def test_positions_command_consumes_settlements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#684 item 6: the positions command's championship branch also
    consumes settlements before its sync — a settled position gets a
    settlement close, not a drift row (previously reconcile-only)."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(monkeypatch, {TICKER: _settlement_record()})

    result = runner.invoke(app, ["positions"])
    assert result.exit_code == 0, result.output

    closes = _closes(db_path)
    assert len(closes) == 1
    assert closes[0]["agent"] == "settlement"
    assert closes[0]["price"] == 1.0


def test_risk_check_consumes_settlements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#684 item 6: risk-check's daily-loss reading must see the
    settlement loss, not a mark-priced drift row it excludes."""
    from unittest.mock import AsyncMock

    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch,
        {TICKER: _settlement_record(market_result="yes")},  # NO loses
    )
    monkeypatch.setattr(
        portfolio_module, "get_balance", AsyncMock(return_value=1000.0),
    )

    result = runner.invoke(app, ["risk-check"])
    # risk-check may exit nonzero on breached limits — the settlement
    # write is what we pin, not the verdict.
    closes = _closes(db_path)
    settlement = [c for c in closes if c["agent"] == "settlement"]
    assert len(settlement) == 1, result.output
    assert settlement[0]["price"] == 0.0


def test_zero_settled_record_writes_evidence_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#684 review: record says ZERO settled (full off-ledger exit) —
    a 0-count settlement evidence row protects the drift mark from
    later repricing while booking nothing."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch,
        {TICKER: _settlement_record(market_result="yes")
         | {"no_count_fp": "0"}},
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output

    closes = _closes(db_path)
    evidence = [
        c for c in closes
        if c["agent"] == "settlement" and c["count"] == 0
    ]
    drift = [c for c in closes if c["agent"] == "reconcile"]
    assert len(evidence) == 1
    assert len(drift) == 1 and drift[0]["count"] == 100
    # Repricing protection: the group has a settlement close, so the
    # drift row keeps its mark even with the outcome known.
    from gimmes.reporting.pnl import calculate_pnl

    async def _trades():
        db = Database(db_path)
        await db.connect()
        try:
            return await get_trades(db, ticker=TICKER, limit=100)
        finally:
            await db.close()

    summary = calculate_pnl(asyncio.run(_trades()))
    # Drift mark kept: (0.705-0.63)*100, NOT (0.0-0.63)*100.
    assert summary.gross_pnl == pytest.approx((0.705 - 0.63) * 100)


def test_wildly_off_record_distrusted_to_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#684 review: a plain-contracts "100" misparses to 1 under the
    centi heuristic — wildly off the residual → distrust, ledger
    behavior, ERROR naming the possible scaling misparse."""
    import logging

    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch,
        {TICKER: _settlement_record() | {"no_count_fp": "100"}},
    )

    with caplog.at_level(logging.ERROR, logger="gimmes"):
        result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert "possible fp-scaling misparse" in caplog.text

    settlement = [
        c for c in _closes(db_path) if c["agent"] == "settlement"
    ]
    assert settlement[0]["count"] == 100  # full ledger residual


def test_record_above_residual_uses_ledger_generic_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """In-band record > residual (phantom/extra ledger closes, not an
    off-ledger exit): the ledger residual stands, one settlement close
    for the full residual, no drift row, generic warning."""
    import logging

    db_path = tmp_path / "test.db"
    _seed(db_path)  # residual 100
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch,
        {TICKER: _settlement_record() | {"no_count_fp": "15000"}},
    )

    with caplog.at_level(logging.WARNING, logger="gimmes"):
        result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert "using the ledger (#684)" in caplog.text
    assert "exited off-ledger" not in caplog.text

    closes = _closes(db_path)
    assert len(closes) == 1
    assert closes[0]["agent"] == "settlement"
    assert closes[0]["count"] == 100


def test_unusable_fp_forms_fall_back_to_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nan/inf/fractional/negative/non-centi-integer forms all parse
    to None → ledger-only behavior."""
    for bad in ("nan", "inf", "100.5", "-6000", "60"):
        db_path = tmp_path / f"test-{bad}.db"
        _seed(db_path)
        _wire_championship(monkeypatch, db_path)
        _mock_settlements(
            monkeypatch,
            {TICKER: _settlement_record() | {"no_count_fp": bad}},
        )
        result = runner.invoke(app, ["reconcile"])
        assert result.exit_code == 0, (bad, result.output)
        settlement = [
            c for c in _closes(db_path) if c["agent"] == "settlement"
        ]
        assert settlement[0]["count"] == 100, bad


def test_second_run_does_not_double_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#684 review: the count clamp broke residual-based idempotency —
    a retry after a clamped write must NOT book the off-ledger drift
    remainder at settlement value."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch,
        {TICKER: _settlement_record() | {"no_count_fp": "6000"}},
    )

    # Run 1: clamped settlement close (60) — then simulate the crash
    # BEFORE sync by... running reconcile fully, then deleting the
    # drift row and re-inserting the position (the retry world).
    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output

    async def _retry_world() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await db.conn.execute(
                "DELETE FROM trades WHERE agent = 'reconcile'",
            )
            await db.conn.commit()
            await upsert_position(db, Position(
                ticker=TICKER, title="CPI April NO", side="no",
                count=100, avg_price=0.63, market_price=0.705,
                cost_basis=63.0, market_value=70.5,
                unrealized_pnl=7.5, realized_pnl=0.0,
            ))
        finally:
            await db.close()

    asyncio.run(_retry_world())

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    settlement = [
        c for c in _closes(db_path) if c["agent"] == "settlement"
    ]
    assert len(settlement) == 1  # no second settlement write
    assert settlement[0]["count"] == 60


def test_yes_side_record_keyed_by_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#684 review: the whole suite was NO-side — pin the side-keyed
    count_fp lookup and win math for a YES position."""
    db_path = tmp_path / "test.db"

    async def _s() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await insert_trade(db, TradeDecision(
                ticker=TICKER, action=TradeDecision.Action.OPEN,
                side="yes", count=100, price=0.37,
            ))
            await upsert_position(db, Position(
                ticker=TICKER, title="CPI April YES", side="yes",
                count=100, avg_price=0.37, market_price=0.295,
                cost_basis=37.0, market_value=29.5,
                unrealized_pnl=-7.5, realized_pnl=0.0,
            ))
        finally:
            await db.close()

    asyncio.run(_s())
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(
        monkeypatch,
        {TICKER: _settlement_record(market_result="yes")
         | {"yes_count_fp": "6000", "no_count_fp": "0"}},
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    closes = _closes(db_path)
    settlement = [
        c for c in closes
        if c["agent"] == "settlement" and c["count"] > 0
    ]
    assert len(settlement) == 1
    assert settlement[0]["count"] == 60  # yes_count_fp, not no_
    assert settlement[0]["price"] == 1.0  # YES won


def test_open_position_never_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#684 review: the fresh-exclusion — a position STILL OPEN on the
    API must not be force-closed even when a (stale/re-listed)
    settlement record exists for it."""
    db_path = tmp_path / "test.db"
    _seed(db_path)
    _wire_championship(monkeypatch, db_path)
    # The API still reports the position open.
    still_open = Position(
        ticker=TICKER, title="CPI April NO", side="no", count=100,
        avg_price=0.63, market_price=0.71, cost_basis=63.0,
        market_value=71.0, unrealized_pnl=8.0, realized_pnl=0.0,
    )

    async def _fresh(_client):  # type: ignore[no-untyped-def]
        return [still_open]

    monkeypatch.setattr(
        portfolio_module, "get_all_positions", _fresh,
    )
    requested: list[set] = []

    async def _spy(_client, tickers, **_kw):  # type: ignore[no-untyped-def]
        requested.append(set(tickers))
        return {TICKER: _settlement_record()}

    monkeypatch.setattr(
        portfolio_module, "get_settlements_for_tickers", _spy,
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert requested == []  # nothing removed → endpoint not asked
    assert _closes(db_path) == []  # no close rows of any kind


def test_mid_write_crash_rolls_back_open_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#684 review: the rollback is load-bearing — a failure AFTER a
    partial commit-less write leaves an open implicit transaction
    that would kill sync_positions' BEGIN IMMEDIATE."""
    db_path = tmp_path / "test.db"

    async def _s() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            for tk in (TICKER, "KXSECOND-26JUL-T1"):
                await insert_trade(db, TradeDecision(
                    ticker=tk, action=TradeDecision.Action.OPEN,
                    side="no", count=100, price=0.63,
                ))
                await upsert_position(db, Position(
                    ticker=tk, title=tk, side="no", count=100,
                    avg_price=0.63, market_price=0.705,
                    cost_basis=63.0, market_value=70.5,
                    unrealized_pnl=7.5, realized_pnl=0.0,
                ))
        finally:
            await db.close()

    asyncio.run(_s())
    _wire_championship(monkeypatch, db_path)
    _mock_settlements(monkeypatch, {
        TICKER: _settlement_record(),
        "KXSECOND-26JUL-T1": _settlement_record(
            ticker="KXSECOND-26JUL-T1",
        ),
    })

    from gimmes.store import queries as queries_module

    real = queries_module.log_settlement_close
    calls = {"n": 0}

    async def _write_then_fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        row_id = await real(*args, **kwargs)
        if calls["n"] >= 2:
            # The write happened, uncommitted — the exception leaves
            # an open implicit transaction.
            raise RuntimeError("post-write explosion")
        return row_id

    monkeypatch.setattr(
        queries_module, "log_settlement_close", _write_then_fail,
    )

    result = runner.invoke(app, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert "settlements lookup failed" in result.output

    async def _rows():
        db = Database(db_path)
        await db.connect()
        try:
            return await get_trades(db, limit=1000)
        finally:
            await db.close()

    rows = asyncio.run(_rows())
    first_settlement = [
        r for r in rows
        if r["ticker"] == TICKER and r["agent"] == "settlement"
    ]
    second_settlement = [
        r for r in rows
        if r["ticker"] == "KXSECOND-26JUL-T1"
        and r["agent"] == "settlement"
    ]
    second_drift = [
        r for r in rows
        if r["ticker"] == "KXSECOND-26JUL-T1"
        and r["agent"] == "reconcile"
    ]
    assert len(first_settlement) == 1  # commit-per-ticker held
    assert second_settlement == []  # uncommitted write rolled back
    assert len(second_drift) == 1  # degraded to drift


# ---------------------------------------------------------------------------
# get_settlements_for_tickers pagination
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TestGetSettlementsForTickers:
    """Bounded pagination over the account-lifetime settlements list."""

    def _pager(self, pages: list[tuple[list[dict], str | None]]):
        """Fake get_settlements yielding scripted (records, cursor)
        pages and counting calls."""
        calls = {"n": 0}

        async def _fake(_client, *, limit=200, cursor=None):  # type: ignore[no-untyped-def]
            page = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            return page

        return _fake, calls

    async def test_empty_tickers_no_api_call(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake, calls = self._pager([([], None)])
        monkeypatch.setattr(portfolio_module, "get_settlements", fake)
        assert await portfolio_module.get_settlements_for_tickers(
            MagicMock(), set(),
        ) == {}
        assert calls["n"] == 0

    async def test_stops_when_all_tickers_matched(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = datetime.now(UTC)
        fake, calls = self._pager([
            (
                [
                    _settlement_record("A", settled_time=_iso(now)),
                    _settlement_record("B", settled_time=_iso(now)),
                ],
                "next-cursor",  # more pages exist — must not be read
            ),
        ])
        monkeypatch.setattr(portfolio_module, "get_settlements", fake)
        found = await portfolio_module.get_settlements_for_tickers(
            MagicMock(), {"A", "B"},
        )
        assert set(found) == {"A", "B"}
        assert calls["n"] == 1

    async def test_newest_record_wins_per_ticker(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The endpoint returns newest-first — a re-listed ticker's
        first (newest) record is kept."""
        now = datetime.now(UTC)
        newest = _settlement_record(
            "A", market_result="yes", settled_time=_iso(now),
        )
        older = _settlement_record(
            "A", market_result="no",
            settled_time=_iso(now - timedelta(days=1)),
        )
        fake, _calls = self._pager([([newest, older], None)])
        monkeypatch.setattr(portfolio_module, "get_settlements", fake)
        found = await portfolio_module.get_settlements_for_tickers(
            MagicMock(), {"A"},
        )
        assert found["A"]["market_result"] == "yes"

    async def test_stops_at_lookback_window(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A page whose oldest record predates the lookback window ends
        the walk — an unmatched ticker older than that can only be
        stale local state, not a recent settlement."""
        old = datetime.now(UTC) - timedelta(days=45)
        fake, calls = self._pager([
            (
                [_settlement_record("OTHER", settled_time=_iso(old))],
                "next-cursor",
            ),
        ])
        monkeypatch.setattr(portfolio_module, "get_settlements", fake)
        found = await portfolio_module.get_settlements_for_tickers(
            MagicMock(), {"WANTED"}, lookback_days=30,
        )
        assert found == {}
        assert calls["n"] == 1

    async def test_stops_at_max_pages_and_warns(
        self, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Hitting the page cap with unmatched tickers is an operator
        signal — those tickers silently degrade to drift closes."""
        import logging

        now = datetime.now(UTC)
        fake, calls = self._pager([
            (
                [_settlement_record("OTHER", settled_time=_iso(now))],
                "more",
            ),
        ])
        monkeypatch.setattr(portfolio_module, "get_settlements", fake)
        with caplog.at_level(
            logging.WARNING, logger="gimmes.kalshi.portfolio",
        ):
            found = await portfolio_module.get_settlements_for_tickers(
                MagicMock(), {"WANTED"}, max_pages=3,
            )
        assert found == {}
        assert calls["n"] == 3
        assert "pagination cap" in caplog.text
        assert "WANTED" in caplog.text

    async def test_stops_when_cursor_exhausted(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """cursor=None with tickers still unmatched ends the walk —
        the account has no more history to read."""
        now = datetime.now(UTC)
        fake, calls = self._pager([
            ([_settlement_record("OTHER", settled_time=_iso(now))], None),
        ])
        monkeypatch.setattr(portfolio_module, "get_settlements", fake)
        found = await portfolio_module.get_settlements_for_tickers(
            MagicMock(), {"WANTED"},
        )
        assert found == {}
        assert calls["n"] == 1

    async def test_stops_on_empty_page(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty page ends the walk even if a cursor is present."""
        fake, calls = self._pager([([], "more")])
        monkeypatch.setattr(portfolio_module, "get_settlements", fake)
        found = await portfolio_module.get_settlements_for_tickers(
            MagicMock(), {"WANTED"},
        )
        assert found == {}
        assert calls["n"] == 1

    async def test_unparseable_settled_time_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A malformed timestamp neither crashes the walk nor
        satisfies the lookback stop."""
        rec = _settlement_record("A", settled_time="not-a-date")
        fake, _calls = self._pager([([rec], None)])
        monkeypatch.setattr(portfolio_module, "get_settlements", fake)
        found = await portfolio_module.get_settlements_for_tickers(
            MagicMock(), {"A"},
        )
        assert found["A"] is rec
