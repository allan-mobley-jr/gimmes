"""Skip rows inherit candidate analytics and carry a structured
reason (#657).

Degenerate skips (prob 0 → edge = price − 100%, $0.00 prices) came
from agent templates passing zeros or omitting args; `log-trade` now
backfills missing/zeroed skip analytics from the latest candidate row
(the #656 CLOSE-inheritance precedent), and `--reason` persists a
machine-queryable cause. Real seeded DB + CliRunner, following the
test_cli_close_analytics pattern.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gimmes import cli as cli_module
from gimmes.cli import app
from gimmes.store.database import Database
from gimmes.store.queries import get_trades, insert_candidate
from gimmes.strategy.scanner import effective_price

runner = CliRunner()

TICKER = "KXCPI-26APR-T0.5"

# Candidate row seeded at scan time: price 0.70, prob 0.85, score 82.
CAND_PRICE = 0.70
CAND_PROB = 0.85
CAND_SCORE = 82.0


def _patch_config(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, side: str = "no",
) -> None:
    cfg = MagicMock()
    cfg.db_path = db_path
    cfg.strategy.side = side
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)


def _db_run(db_path: Path, fn: Callable[[Database], Awaitable[Any]]) -> Any:
    """Open (creating/migrating) the DB, run `fn(db)`, close."""
    async def _go() -> Any:
        db = Database(db_path)
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.close()

    return asyncio.run(_go())


def _seed_candidate(
    db_path: Path, side: str = "no", prob: float = CAND_PROB,
) -> None:
    async def _insert(db: Database) -> None:
        edge = prob - effective_price(CAND_PRICE, side) if prob > 0 else 0.0
        await insert_candidate(
            db, TICKER, "CPI April", CAND_PRICE, prob, edge,
            CAND_SCORE, "memo", recommendation="proceed",
        )

    _db_run(db_path, _insert)


def _bare_db(db_path: Path) -> None:
    """Create an empty (candidate-free) migrated database."""
    async def _noop(db: Database) -> None:
        pass

    _db_run(db_path, _noop)


def _trade_rows(db_path: Path, action: str = "skip") -> list[dict]:
    async def _query(db: Database) -> list[dict]:
        trades = await get_trades(db, ticker=TICKER)
        return [t for t in trades if t["action"] == action]

    return _db_run(db_path, _query)


def _skip_row(db_path: Path) -> dict:
    [s] = _trade_rows(db_path)
    return s


def _invoke_skip(*extra: str) -> object:
    return runner.invoke(app, [
        "log-trade", TICKER, "--action", "skip",
        "--rationale", "test skip",
        *extra,
    ])


def test_skip_without_args_inherits_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip()
    assert result.exit_code == 0, result.output

    s = _skip_row(db_path)
    assert s["model_probability"] == CAND_PROB
    assert s["price"] == CAND_PRICE
    assert s["gimme_score"] == CAND_SCORE
    assert s["edge"] == pytest.approx(
        CAND_PROB - effective_price(CAND_PRICE, "no"),
    )


def test_explicit_zeros_backfilled_like_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact degenerate template form (`--price 0 --prob 0
    --score 0`) that produced edge = price − 100% batches."""
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip(
        "--price", "0", "--prob", "0", "--score", "0",
    )
    assert result.exit_code == 0, result.output

    s = _skip_row(db_path)
    assert s["model_probability"] == CAND_PROB
    assert s["price"] == CAND_PRICE
    assert s["gimme_score"] == CAND_SCORE
    # The #657 symptom: NO-side edge must not be price - 100%
    assert s["edge"] != pytest.approx(CAND_PRICE - 1.0)
    assert s["edge"] > 0


def test_explicit_nonzero_args_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip(
        "--price", "0.55", "--prob", "0.75", "--score", "64",
    )
    assert result.exit_code == 0, result.output

    s = _skip_row(db_path)
    assert s["model_probability"] == 0.75
    assert s["price"] == 0.55
    assert s["gimme_score"] == 64.0
    assert s["edge"] == pytest.approx(
        0.75 - effective_price(0.55, "no"),
    )


def test_mixed_explicit_prob_backfilled_price_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip("--prob", "0.8")
    assert result.exit_code == 0, result.output

    s = _skip_row(db_path)
    assert s["model_probability"] == 0.8
    assert s["price"] == CAND_PRICE
    assert s["gimme_score"] == CAND_SCORE
    # Edge recomputed from the mixed final values
    assert s["edge"] == pytest.approx(
        0.8 - effective_price(CAND_PRICE, "no"),
    )


def test_no_candidate_row_keeps_zeros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    db_path = tmp_path / "test.db"
    _bare_db(db_path)
    _patch_config(monkeypatch, db_path)

    with caplog.at_level(logging.DEBUG, logger="gimmes.cli"):
        result = _invoke_skip()
    assert result.exit_code == 0, result.output
    # The genuine no-row path — the fallback debug names the true
    # cause (guards against inverting the cause_logged flag, #670).
    assert "found no candidate row" in caplog.text

    s = _skip_row(db_path)
    assert s["model_probability"] == 0.0
    assert s["price"] == 0.0
    assert s["edge"] == 0.0


def test_yes_side_edge_uses_effective_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path, side="yes")
    _patch_config(monkeypatch, db_path, side="yes")

    result = _invoke_skip()
    assert result.exit_code == 0, result.output

    s = _skip_row(db_path)
    assert s["edge"] == pytest.approx(
        CAND_PROB - effective_price(CAND_PRICE, "yes"),
    )


def test_reason_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip("--reason", "validation_failed")
    assert result.exit_code == 0, result.output
    assert _skip_row(db_path)["reason"] == "validation_failed"


def test_reason_defaults_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip()
    assert result.exit_code == 0, result.output
    assert _skip_row(db_path)["reason"] == ""


def test_unknown_reason_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip("--reason", "vibes")
    assert result.exit_code != 0


def test_reason_rejected_on_non_skip_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--reason is a skip cause — an open/close carrying one would be
    semantically invalid data (#657 review)."""
    db_path = tmp_path / "test.db"
    _bare_db(db_path)
    _patch_config(monkeypatch, db_path)

    for action in ("open", "close"):
        result = runner.invoke(app, [
            "log-trade", TICKER, "--action", action,
            "--price", "0.6", "--prob", "0.8",
            "--rationale", "test", "--reason", "cooldown",
        ])
        assert result.exit_code != 0, action
    assert _trade_rows(db_path, "open") == []
    assert _trade_rows(db_path, "close") == []


def test_open_with_zero_args_does_not_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#180 guard rail: prob<=0-as-unknown is a SKIP-only rule. An
    open logged with zeros must keep them — a zero probability
    bypassing validation gates was the original #180 bug."""
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, [
        "log-trade", TICKER, "--action", "open",
        "--price", "0", "--prob", "0", "--score", "0",
        "--rationale", "test open",
    ])
    assert result.exit_code == 0, result.output

    [o] = _trade_rows(db_path, action="open")
    assert o["model_probability"] == 0.0
    assert o["price"] == 0.0
    assert o["gimme_score"] == 0.0


def test_zero_prob_candidate_does_not_persist_degenerate_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The residual corner (#657 review): explicit --prob 0 --price 0
    with a candidate row whose prob is also 0 must not keep the
    constructor's edge = -effective_price(0) = -100%. Unknown
    probability -> edge 0, not a fabricated one."""
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path, prob=0.0)
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip("--price", "0", "--prob", "0")
    assert result.exit_code == 0, result.output

    s = _skip_row(db_path)
    assert s["price"] == CAND_PRICE  # backfilled
    assert s["model_probability"] == 0.0  # candidate had none
    assert s["edge"] == 0.0  # NOT -1.0


def test_scout_shape_prob_zero_real_price_no_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scout logs skips before any candidate row exists and passes
    --prob 0 when it has no estimate. The recorded edge must be 0,
    not -effective_price(price) (the price - 100% signature)."""
    db_path = tmp_path / "test.db"
    _bare_db(db_path)
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip("--price", "0.65", "--prob", "0")
    assert result.exit_code == 0, result.output

    s = _skip_row(db_path)
    assert s["price"] == 0.65
    assert s["model_probability"] == 0.0
    assert s["edge"] == 0.0


def test_non_entry_reason_skips_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-entry skips (no_position, close_failed, plus #670's
    infra_failed and already_traded) carry no entry decision — the
    candidate row must NOT be fabricated onto them, else the
    missed-opportunity audit counts a failed close, a tooling
    casualty, or a held position as a missed entry."""
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _patch_config(monkeypatch, db_path)

    from gimmes.strategy.advisor import NON_ENTRY_SKIP_REASONS

    for reason in sorted(NON_ENTRY_SKIP_REASONS):
        result = _invoke_skip("--reason", reason)
        assert result.exit_code == 0, result.output

    rows = _trade_rows(db_path)
    assert len(rows) == len(NON_ENTRY_SKIP_REASONS)
    for s in rows:
        assert s["model_probability"] == 0.0
        assert s["price"] == 0.0
        assert s["edge"] == 0.0


def test_migration_v18_adds_reason_column(tmp_path: Path) -> None:
    async def _check(db: Database) -> tuple[int, list[str]]:
        cursor = await db.conn.execute(
            "SELECT MAX(version) FROM schema_version",
        )
        version = (await cursor.fetchone())[0]
        cursor = await db.conn.execute("PRAGMA table_info(trades)")
        cols = [row[1] for row in await cursor.fetchall()]
        return version, cols

    version, cols = _db_run(tmp_path / "test.db", _check)
    assert version >= 18
    assert "reason" in cols


def test_bound_price_candidate_backfills_zero_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#658: a candidate stuck at YES $1.00 (NO side costs $0.00)
    backfills its price onto the skip, but the edge clamps to 0 —
    prob - effective_price would fabricate edge = prob there."""
    db_path = tmp_path / "test.db"

    async def _insert(db: Database) -> None:
        await insert_candidate(
            db, TICKER, "CPI YoY", 1.00, 0.88, 0.88, 80.0, "memo",
        )

    _db_run(db_path, _insert)
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip()
    assert result.exit_code == 0, result.output

    s = _skip_row(db_path)
    assert s["price"] == 1.00
    assert s["model_probability"] == 0.88
    assert s["edge"] == 0.0


def test_bound_price_open_records_zero_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """log-trade opens at a bound price record edge 0 too (#658)."""
    db_path = tmp_path / "test.db"
    _bare_db(db_path)
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, [
        "log-trade", TICKER, "--action", "open",
        "--price", "1.00", "--prob", "0.88",
        "--rationale", "bound open",
    ])
    assert result.exit_code == 0, result.output

    [o] = _trade_rows(db_path, "open")
    assert o["model_probability"] == 0.88  # explicit prob untouched
    assert o["edge"] == 0.0


def test_bound_price_close_with_explicit_prob_records_zero_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented behavior change (#658): a manually logged close at a
    bound price with explicit --prob records edge 0, not prob - eff —
    a settlement-priced close has no fabricated edge either. (No
    production writer takes this path; settlement/reconcile closes
    copy entry analytics in queries.py.)"""
    db_path = tmp_path / "test.db"
    _bare_db(db_path)
    _patch_config(monkeypatch, db_path)

    result = runner.invoke(app, [
        "log-trade", TICKER, "--action", "close",
        "--price", "1.00", "--prob", "0.9",
        "--rationale", "bound close",
    ])
    assert result.exit_code == 0, result.output

    [c] = _trade_rows(db_path, "close")
    assert c["model_probability"] == 0.9
    assert c["edge"] == 0.0


def _age_candidate(db_path: Path, sql_age: str) -> None:
    async def _age(db: Database) -> None:
        await db.conn.execute(
            "UPDATE candidates SET scanned_at = " + sql_age,
        )
        await db.conn.commit()

    _db_run(db_path, _age)


def test_stale_candidate_not_backfilled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#670: a candidate scanned >48h ago is a different market — the
    skip keeps honest zeros instead of inheriting stale analytics
    (mirrors caddie-master's 48h research-void rule)."""
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _age_candidate(db_path, "datetime('now', '-49 hours')")
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip()
    assert result.exit_code == 0, result.output
    s = _skip_row(db_path)
    assert s["model_probability"] == 0.0
    assert s["price"] == 0.0
    assert s["edge"] == 0.0


def test_candidate_just_inside_bound_backfills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """47h old is within the bound — full backfill (pins the
    comparison direction)."""
    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _age_candidate(db_path, "datetime('now', '-47 hours')")
    _patch_config(monkeypatch, db_path)

    result = _invoke_skip()
    assert result.exit_code == 0, result.output
    s = _skip_row(db_path)
    assert s["model_probability"] == CAND_PROB
    assert s["price"] == CAND_PRICE


def test_unparseable_scanned_at_not_backfilled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A corrupt scanned_at is unknown-age data — treated as stale
    (zeros) rather than trusted, and WARNED about (data integrity —
    Groundskeeper triages logs post-cycle)."""
    import logging

    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _age_candidate(db_path, "'garbage'")
    _patch_config(monkeypatch, db_path)

    with caplog.at_level(logging.WARNING, logger="gimmes.cli"):
        result = _invoke_skip()
    assert result.exit_code == 0, result.output
    assert "unparseable" in caplog.text
    s = _skip_row(db_path)
    assert s["model_probability"] == 0.0
    assert s["price"] == 0.0


def test_stale_candidate_logs_staleness_not_no_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#670 review: the stale path logs its own cause (INFO) and must
    NOT also emit the misleading 'found no candidate row' fallback
    (DEBUG capture is required — at INFO the absence assertion would
    be vacuous)."""
    import logging

    db_path = tmp_path / "test.db"
    _seed_candidate(db_path)
    _age_candidate(db_path, "datetime('now', '-49 hours')")
    _patch_config(monkeypatch, db_path)

    with caplog.at_level(logging.DEBUG, logger="gimmes.cli"):
        result = _invoke_skip()
    assert result.exit_code == 0, result.output
    assert "too stale to backfill" in caplog.text
    assert "found no candidate row" not in caplog.text


def test_lookup_error_degrades_with_own_cause_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A candidate-lookup failure degrades to zeros, logs its own
    cause at ERROR, and does not misreport 'no candidate row'."""
    import logging
    import sqlite3

    db_path = tmp_path / "test.db"
    _bare_db(db_path)
    _patch_config(monkeypatch, db_path)

    async def _boom(_db, _ticker):  # type: ignore[no-untyped-def]
        raise sqlite3.Error("lookup exploded")

    monkeypatch.setattr(
        "gimmes.store.queries.get_candidate_for_ticker", _boom,
    )

    with caplog.at_level(logging.DEBUG, logger="gimmes.cli"):
        result = _invoke_skip()
    assert result.exit_code == 0, result.output
    assert "candidate lookup failed" in caplog.text
    assert "found no candidate row" not in caplog.text

    s = _skip_row(db_path)
    assert s["model_probability"] == 0.0
    assert s["price"] == 0.0
