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
from gimmes.models.portfolio import Position
from gimmes.store.database import Database
from gimmes.store.observation_validator import PLAYBOOK_SOURCES
from gimmes.store.queries import (
    get_position_notes,
    insert_candidate,
    insert_position_note,
    set_position_rules_snapshot,
    upsert_position,
)

C1407_STALE = (
    "No named major Wall Street bank has published April CPI MoM"
    " strictly above 0.5%"
)


def _patch_config(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    cfg = MagicMock()
    cfg.db_path = db_path
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)


def _seed_note(
    db_path: Path, ticker: str, body: str, *,
    cycle: int, agent: str, note_type: str,
) -> None:
    """Synchronously seed a position note via asyncio.run()."""

    async def _seed() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await insert_position_note(
                db,
                ticker=ticker,
                cycle=cycle,
                agent=agent,
                note_type=note_type,
                body=body,
            )
        finally:
            await db.close()

    asyncio.run(_seed())


def _seed_decision(db_path: Path, ticker: str, body: str) -> None:
    _seed_note(
        db_path, ticker, body,
        cycle=1391, agent="caddie-master", note_type="decision",
    )


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



# #643: economic-category observation writes now also require the
# playbook audit footer. A minimal conforming footer keeps these #614
# tests exercising their original read-back intent.
def _full_footer() -> str:
    return "\n".join(
        ["Playbook sources checked this cycle (#615):"]
        + [f"- {source}: no result this cycle" for source in PLAYBOOK_SOURCES]
    )


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
        "--body", C1407_STALE + "\n\n" + _full_footer(),
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
        "--body", C1407_STALE + "\n\n" + _full_footer(),
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
        "Barclays +0.55% confirmed this cycle (FXStreet, 2026-05-08)."
        "\n\n" + _full_footer(),
    ])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# #643: semantics guard + footer audit, end-to-end through the CLI
# ---------------------------------------------------------------------------

INCIDENT_RULES = (
    "If the Consumer Price Index (CPI) increases by more than -0.1%"
    " (single-decimal) in June 2026, the market resolves to Yes."
)


def _seed_position_with_rules(
    db_path: Path, ticker: str, rules_primary: str,
) -> None:
    """Seed a positions row and its settlement-language snapshot."""

    async def _seed() -> None:
        db = Database(db_path)
        await db.connect()
        try:
            await upsert_position(db, Position(
                ticker=ticker, side="no", count=100, avg_price=0.63,
                market_price=0.90, cost_basis=63.0,
            ))
            assert await set_position_rules_snapshot(
                db, ticker=ticker, rules_primary=rules_primary,
            )
        finally:
            await db.close()

    asyncio.run(_seed())


def _seed_observation(db_path: Path, ticker: str, body: str) -> None:
    _seed_note(
        db_path, ticker, body,
        cycle=1700, agent="monitor", note_type="observation",
    )


def _obs_with_semantics(semantics_line: str) -> str:
    return (
        "Delta since cycle 1700:\n"
        "Price: $0.90.\n"
        f"{semantics_line}\n"
        "Overall: no material change.\n\n"
        + _full_footer()
    )


def test_inverted_semantics_rejected_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #641 incident shape rejects at the CLI: position carries a
    rules snapshot; the observation restates the threshold with the
    inverted comparator."""
    db_path = tmp_path / "test.db"
    _seed_position_with_rules(db_path, "KXCPI-26JUN-T-0.1", INCIDENT_RULES)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26JUN-T-0.1",
        "--cycle", "1701", "--agent", "monitor", "--type", "observation",
        "--body", _obs_with_semantics(
            "Semantics: YES wins when CPI MoM <= -0.1%;"
            " NO wins when CPI MoM > -0.1%",
        ),
    ])
    assert result.exit_code == 1, result.output
    assert "INVERTED SEMANTICS" in result.output
    notes = _read_notes(
        db_path, "KXCPI-26JUN-T-0.1", note_type="observation",
    )
    assert notes == []


def test_correct_semantics_accepted_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_position_with_rules(db_path, "KXCPI-26JUN-T-0.1", INCIDENT_RULES)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26JUN-T-0.1",
        "--cycle", "1701", "--agent", "monitor", "--type", "observation",
        "--body", _obs_with_semantics(
            "Semantics: YES wins when CPI MoM > -0.1%;"
            " NO wins when CPI MoM <= -0.1%",
        ),
    ])
    assert result.exit_code == 0, result.output


def test_no_snapshot_passes_without_semantics_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No positions row / no snapshot → semantics guard dormant; the
    footer is still required."""
    db_path = tmp_path / "test.db"
    _seed_empty_db(db_path)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26JUN-T-0.1",
        "--cycle", "1701", "--agent", "monitor", "--type", "observation",
        "--body", "Delta: nothing new.\n\n" + _full_footer(),
    ])
    assert result.exit_code == 0, result.output


def test_missing_footer_rejected_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_empty_db(db_path)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26JUN-T-0.1",
        "--cycle", "1701", "--agent", "monitor", "--type", "observation",
        "--body", "Delta: nothing new this cycle.",
    ])
    assert result.exit_code == 1, result.output
    assert "Playbook sources checked this cycle" in result.output


def test_refound_stale_cite_rejected_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding-2 incident: prior cycle cited 2026-06-18; this cycle
    re-finds the same dated note and writes it as fresh."""
    db_path = tmp_path / "test.db"
    footer = _full_footer().replace(
        "- Bank of America: no result this cycle",
        "- Bank of America: +0.30% (Investing.com, 2026-06-18)",
    )
    _seed_observation(
        db_path, "KXCPI-26JUN-T-0.1", "Delta: baseline.\n\n" + footer,
    )
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26JUN-T-0.1",
        "--cycle", "1701", "--agent", "monitor", "--type", "observation",
        "--body", "Delta: re-checked banks.\n\n" + footer,
    ])
    assert result.exit_code == 1, result.output
    assert "NEWLY PUBLISHED" in result.output
    # Prior observation remains the only one on file.
    notes = _read_notes(
        db_path, "KXCPI-26JUN-T-0.1", note_type="observation",
    )
    assert len(notes) == 1


def test_warnings_print_but_do_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown footer source warns (yellow) but the write succeeds."""
    db_path = tmp_path / "test.db"
    _seed_empty_db(db_path)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26JUN-T-0.1",
        "--cycle", "1701", "--agent", "monitor", "--type", "observation",
        "--body", (
            "Delta: nothing new.\n\n" + _full_footer()
            + "\n- Nomura: +0.1% (FXStreet, 2026-07-01)"
        ),
    ])
    assert result.exit_code == 0, result.output
    assert "Nomura" in result.output  # warning surfaced


def test_multiple_errors_reported_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Semantics inversion AND a footer violation surface in ONE
    rejection so the agent fixes everything in a single rewrite."""
    db_path = tmp_path / "test.db"
    _seed_position_with_rules(db_path, "KXCPI-26JUN-T-0.1", INCIDENT_RULES)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26JUN-T-0.1",
        "--cycle", "1701", "--agent", "monitor", "--type", "observation",
        "--body", (
            "Delta since cycle 1700:\n"
            "Semantics: YES wins when CPI MoM <= -0.1%;"
            " NO wins when CPI MoM > -0.1%\n"
            "Overall: ok.\n"
            # no footer at all
        ),
    ])
    assert result.exit_code == 1, result.output
    assert "INVERTED SEMANTICS" in result.output
    assert "Playbook sources checked this cycle" in result.output


def test_force_bypasses_643_validators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "test.db"
    _seed_position_with_rules(db_path, "KXCPI-26JUN-T-0.1", INCIDENT_RULES)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26JUN-T-0.1",
        "--cycle", "1701", "--agent", "monitor", "--type", "observation",
        "--force",
        "--body", "Inverted and footerless — backfill only.",
    ])
    assert result.exit_code == 0, result.output
    assert "#643" in result.output  # audit line names both validators


def test_validator_messages_survive_rich_markup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejection messages embed settlement-language snippets with
    bracketed clauses — they must print with markup disabled or Rich
    eats them (#649 review). The incident rules contain
    '(single-decimal)' but the INVERTED SEMANTICS message quotes the
    raw rules text; use a bracketed rules variant to pin survival."""
    db_path = tmp_path / "test.db"
    rules = (
        "If the Consumer Price Index [as reported by the BLS]"
        " increases by more than -0.1% in June 2026, the market"
        " resolves to Yes."
    )
    _seed_position_with_rules(db_path, "KXCPI-26JUN-T-0.1", rules)
    _patch_config(monkeypatch, db_path)

    runner = CliRunner()
    result = runner.invoke(app, [
        "position-note", "KXCPI-26JUN-T-0.1",
        "--cycle", "1701", "--agent", "monitor", "--type", "observation",
        "--body", _obs_with_semantics(
            "Semantics: YES wins when CPI MoM <= -0.1%;"
            " NO wins when CPI MoM > -0.1%",
        ),
    ])
    assert result.exit_code == 1, result.output
    # The bracketed clause from the quoted rules must survive verbatim
    # in the rejection output.
    assert "[as reported" in result.output
