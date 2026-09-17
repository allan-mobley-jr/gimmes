"""`gimmes activity` — the read side of the activity log (#829).

Caddie Master's Step 7 staleness anchor is the newest `pro`/`complete`
row, read via `gimmes activity --agent pro --phase complete --limit 1`.
Before #829 nothing could ask that question: `log-activity` is
write-only and `get_recent_activity` had no caller, so the Pro's
schedule was a `% 10` cycle slot that forfeited every miss.

Real seeded DB + CliRunner, following the test_cli_skip_analytics
pattern.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from gimmes.cli import app
from gimmes.store.database import Database
from gimmes.store.queries import insert_activity

runner = CliRunner()


def _db_run(db_path: Path, fn):
    async def _go():
        db = Database(db_path)
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.close()

    return asyncio.run(_go())


def _config(db_path: Path) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = db_path
    return cfg


def _invoke(db_path: Path, *args: str):
    # COLUMNS keeps Rich from wrapping table cells mid-assertion.
    with patch("gimmes.cli.load_config", return_value=_config(db_path)), \
            patch.dict(os.environ, {"COLUMNS": "200"}):
        return runner.invoke(app, ["activity", *args])


async def _seed(db) -> None:
    await insert_activity(
        db, cycle=10, agent="pro", phase="start",
        message="Pro starting strategy analysis",
    )
    await insert_activity(
        db, cycle=10, agent="pro", phase="complete",
        message="Pro: 4 analyses run",
    )
    await insert_activity(
        db, cycle=11, agent="monitor", phase="complete",
        message="Monitor reviewed 2 positions",
    )


def test_filters_by_agent_and_phase(tmp_path) -> None:
    """The exact call Step 7 makes returns only the Pro completion."""
    db_path = tmp_path / "gimmes.db"
    _db_run(db_path, _seed)
    result = _invoke(
        db_path, "--agent", "pro", "--phase", "complete", "--limit", "1",
    )
    assert result.exit_code == 0, result.output
    assert "Pro: 4 analyses run" in result.output
    assert "Pro starting strategy analysis" not in result.output
    assert "Monitor reviewed" not in result.output


def test_no_rows_prints_no_activity_found(tmp_path) -> None:
    """#829: Step 7's missing-anchor branch keys on this exact string —
    a Pro that has never completed means RUN, never skip."""
    db_path = tmp_path / "gimmes.db"
    _db_run(db_path, lambda db: asyncio.sleep(0))
    result = _invoke(db_path, "--agent", "pro", "--phase", "complete")
    assert result.exit_code == 0, result.output
    assert "No activity found" in result.output


def test_timestamp_is_utc_not_local(tmp_path, monkeypatch) -> None:
    """#829: the anchor is compared against `date -u`. A future swap to
    format_local_timestamp would silently skew the cadence by the UTC
    offset — the #731 trap in a new place."""
    db_path = tmp_path / "gimmes.db"
    _db_run(db_path, _seed)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE activity_log SET timestamp = '2026-09-02 08:15:00'"
        " WHERE agent = 'pro' AND phase = 'complete'",
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("TZ", "America/New_York")
    if hasattr(time, "tzset"):
        time.tzset()
    result = _invoke(
        db_path, "--agent", "pro", "--phase", "complete", "--limit", "1",
    )
    assert result.exit_code == 0, result.output
    assert "2026-09-02" in result.output
    assert "08:15:00" in result.output


def test_limit_returns_newest_first(tmp_path) -> None:
    db_path = tmp_path / "gimmes.db"

    async def _three(db) -> None:
        for n in (1, 2, 3):
            await insert_activity(
                db, cycle=n, agent="pro", phase="complete",
                message=f"Pro run {n}",
            )

    _db_run(db_path, _three)
    result = _invoke(
        db_path, "--agent", "pro", "--phase", "complete", "--limit", "1",
    )
    assert result.exit_code == 0, result.output
    assert "Pro run 3" in result.output
    assert "Pro run 1" not in result.output


@pytest.mark.parametrize("extra", [(), ("--agent", "pro")])
def test_unfiltered_and_agent_only_both_work(tmp_path, extra) -> None:
    """The filters are optional and compose — an operator debugging a
    silent agent uses the same command without --phase."""
    db_path = tmp_path / "gimmes.db"
    _db_run(db_path, _seed)
    result = _invoke(db_path, *extra)
    assert result.exit_code == 0, result.output
    assert "Pro: 4 analyses run" in result.output
