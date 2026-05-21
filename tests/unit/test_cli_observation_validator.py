"""CLI integration tests for the observation read-back validator (#614).

Verifies that `gimmes position-note --type observation` rejects writes
that contradict cited evidence in the most-recent CM decision note.

These tests are intentionally SYNCHRONOUS — pytest-asyncio's "auto" mode
would wrap them in an event loop, but CliRunner.invoke() calls
position_note() which calls _run() which calls asyncio.run() — and
asyncio.run() refuses to run inside an already-running loop. So the
DB setup uses asyncio.run() at the start of each test (before CliRunner
fires) and the test itself stays sync.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gimmes import cli as cli_module
from gimmes.cli import app
from gimmes.store.database import Database
from gimmes.store.queries import (
    get_position_notes,
    insert_candidate,
    insert_position_note,
)

C1407_STALE = (
    "No named major Wall Street bank has published April CPI MoM"
    " strictly above 0.5%"
)


def _patch_config(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    cfg = MagicMock()
    cfg.db_path = db_path
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)


def _seed_decision(db_path: Path, ticker: str, body: str) -> None:
    """Synchronously seed a CM decision note via asyncio.run()."""

    async def _seed() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await insert_position_note(
                db,
                ticker=ticker,
                cycle=1391,
                agent="caddie-master",
                note_type="decision",
                body=body,
            )
        finally:
            await db.close()

    asyncio.run(_seed())


def _seed_empty_db(db_path: Path) -> None:
    """Synchronously initialize an empty DB (schema only)."""

    async def _init() -> None:
        db = Database(db_path)
        await db.connect()
        await db.close()

    asyncio.run(_init())


def _read_notes(
    db_path: Path, ticker: str, note_type: str | None = None,
) -> list[dict]:  # type: ignore[type-arg]
    async def _read() -> list[dict]:  # type: ignore[type-arg]
        db = Database(db_path)
        await db.connect()
        try:
            return await get_position_notes(
                db, ticker, note_type=note_type,
            )
        finally:
            await db.close()

    return asyncio.run(_read())


CM_DECISION_WITH_BARCLAYS = (
    "Decision: HOLD.\n"
    "Reasoning: thesis intact; bank forecasts confirm.\n"
    "Cited sources:\n"
    "- Barclays April headline CPI MoM +0.55% (FXStreet, 2026-05-08)"
)

CM_DECISION_SILENT_ON_SOURCES = (
    "Decision: HOLD.\n"
    "Reasoning: thesis intact; price unchanged.\n"
    "Cited sources:\n"
    "None — decision based on price + thesis only"
)


def test_observation_rejected_when_validator_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject case: CM cites Barclays +0.55%, observation contains
    c1407 stale-template phrase → exit 1, no row inserted (#614)."""
    db_path = tmp_path / "test.db"
    _seed_decision(db_path, "KXCPI-26APR-T0.5", CM_DECISION_WITH_BARCLAYS)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26APR-T0.5",
        "--cycle", "1407",
        "--agent", "monitor",
        "--type", "observation",
        "--body", C1407_STALE,
    ])
    assert result.exit_code == 1, result.output
    assert "Barclays" in result.output
    assert "#577" in result.output
    assert "#614" in result.output

    notes = _read_notes(db_path, "KXCPI-26APR-T0.5", note_type="observation")
    assert notes == []


def test_observation_allowed_when_cm_silent_on_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vacuous case: CM has no source citations → stale phrase passes (#614)."""
    db_path = tmp_path / "test.db"
    _seed_decision(
        db_path, "KXCPI-26APR-T0.5", CM_DECISION_SILENT_ON_SOURCES,
    )
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26APR-T0.5",
        "--cycle", "1407",
        "--agent", "monitor",
        "--type", "observation",
        "--body", C1407_STALE,
    ])
    assert result.exit_code == 0, result.output


def test_observation_allowed_when_no_prior_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No CM decision exists yet → validator is vacuously satisfied (#614)."""
    db_path = tmp_path / "test.db"
    _seed_empty_db(db_path)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26APR-T0.5",
        "--cycle", "1407",
        "--agent", "monitor",
        "--type", "observation",
        "--body", C1407_STALE,
    ])
    assert result.exit_code == 0, result.output


def test_observation_allowed_when_ticker_not_economic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equity-index tickers (KXSPX/KXINX/KXNASDAQ100) skip the
    validator entirely (#614)."""
    db_path = tmp_path / "test.db"
    _seed_decision(db_path, "KXSPX-26MAY-T5000", CM_DECISION_WITH_BARCLAYS)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXSPX-26MAY-T5000",
        "--cycle", "1407",
        "--agent", "monitor",
        "--type", "observation",
        "--body", C1407_STALE,
    ])
    assert result.exit_code == 0, result.output


def test_force_flag_bypasses_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--force allows backfill scripts to bypass the validator, with
    a yellow audit warning printed (#614)."""
    db_path = tmp_path / "test.db"
    _seed_decision(db_path, "KXCPI-26APR-T0.5", CM_DECISION_WITH_BARCLAYS)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26APR-T0.5",
        "--cycle", "1407",
        "--agent", "backfill",
        "--type", "observation",
        "--body", C1407_STALE,
        "--force",
    ])
    assert result.exit_code == 0, result.output
    assert "--force" in result.output
    assert "audit-visible" in result.output


def test_flag_type_skips_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validator is scoped to --type observation. A `flag` note with
    the same body content is unaffected — AND the row is actually
    inserted (catches a spurious-pass where the CLI no-ops on `flag`
    type without inserting anything) (#614)."""
    db_path = tmp_path / "test.db"
    _seed_decision(db_path, "KXCPI-26APR-T0.5", CM_DECISION_WITH_BARCLAYS)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26APR-T0.5",
        "--cycle", "1407",
        "--agent", "monitor",
        "--type", "flag",
        "--body", C1407_STALE,
    ])
    assert result.exit_code == 0, result.output

    # Verify the flag row was actually written — exit=0 alone could
    # mask a no-op CLI path.
    flag_notes = _read_notes(db_path, "KXCPI-26APR-T0.5", note_type="flag")
    assert len(flag_notes) == 1, flag_notes
    assert C1407_STALE in flag_notes[0]["body"]


def test_ambiguous_prefix_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous prefix tickers (e.g., "KXCPI" matching multiple
    contracts) are a hard error rather than silently writing under
    the un-resolved prefix. Mirrors position-notes / position-context
    behavior and prevents creating unreachable journal entries — the
    same class of bug the validator exists to prevent (#614)."""
    db_path = tmp_path / "test.db"
    # Seed two candidates under different canonical tickers that share
    # the prefix "KXCPI". resolve_ticker(source="known_markets") will
    # return both → ambiguous. (Seeding via candidates rather than
    # position_notes because known_markets reads from
    # positions/candidates/trades, not position_notes.)
    _seed_decision(db_path, "KXCPI-26APR-T0.5", CM_DECISION_WITH_BARCLAYS)
    _seed_decision(db_path, "KXCPI-26MAY-T0.5", CM_DECISION_WITH_BARCLAYS)

    async def _seed_candidates() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await insert_candidate(
                db, "KXCPI-26APR-T0.5", "April CPI", 0.5, 0.6, 0.1, 70, "",
            )
            await insert_candidate(
                db, "KXCPI-26MAY-T0.5", "May CPI", 0.5, 0.6, 0.1, 70, "",
            )
        finally:
            await db.close()

    asyncio.run(_seed_candidates())
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI",  # ambiguous prefix
        "--cycle", "1407",
        "--agent", "monitor",
        "--type", "observation",
        "--body", "Some observation body.",
    ])
    assert result.exit_code == 1, result.output
    assert "Ambiguous" in result.output or "ambiguous" in result.output

    # Verify no note was inserted under the prefix.
    notes = _read_notes(db_path, "KXCPI")
    assert notes == []


def test_observation_allowed_when_surfacing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Good observation that surfaces the CM-cited evidence passes,
    even with CM citing Barclays +0.55% (#614)."""
    db_path = tmp_path / "test.db"
    _seed_decision(db_path, "KXCPI-26APR-T0.5", CM_DECISION_WITH_BARCLAYS)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26APR-T0.5",
        "--cycle", "1407",
        "--agent", "monitor",
        "--type", "observation",
        "--body",
        "Barclays +0.55% confirmed this cycle (FXStreet, 2026-05-08).",
    ])
    assert result.exit_code == 0, result.output
