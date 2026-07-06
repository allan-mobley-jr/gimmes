"""`gimmes lesson` fetches trades per-action, not through a shared
display limit (#668).

The old single `get_trades(limit=1000)` window (timestamp DESC) let
skip volume push the oldest opens out from under their closes once the
trades table passed 1000 rows — `_pair_closes` then dropped those
closes as orphans and the analyses silently degraded to "insufficient
data". The #542 per-action pattern (already used by `report`) fetches
each action under its own generous limit; `lesson` additionally keeps
`skip` rows because `analyze_missed_opportunities` consumes them.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from gimmes import cli as cli_module
from gimmes.cli import app
from gimmes.store import queries as queries_module

runner = CliRunner()


def test_lesson_fetches_per_action_with_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = MagicMock()
    cfg.db_path = tmp_path / "test.db"
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

    calls: list[dict] = []

    async def _spy(_db, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return []

    monkeypatch.setattr(queries_module, "get_trades", _spy)

    result = runner.invoke(app, ["lesson"])
    assert result.exit_code == 0, result.output

    actions = {c.get("action") for c in calls}
    assert actions == {"open", "close", "size_up", "skip"}
    # Per-action limits generous enough that display-style caps can't
    # orphan old opens; no un-scoped legacy fetch remains.
    assert all(c.get("limit") == 100_000 for c in calls)
    assert not any(c.get("action") is None for c in calls)


def test_lesson_feeds_all_actions_to_analyses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The four fetches must actually reach run_all_analyses —
    dropping skips from the concatenation would starve
    analyze_missed_opportunities."""
    from gimmes.strategy import advisor as advisor_module

    cfg = MagicMock()
    cfg.db_path = tmp_path / "test.db"
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

    async def _sentinels(_db, **kwargs):  # type: ignore[no-untyped-def]
        return [{"action": kwargs["action"], "ticker": f"S-{kwargs['action']}"}]

    monkeypatch.setattr(queries_module, "get_trades", _sentinels)

    captured: list[list] = []

    def _capture(trades, _candidates, _config):  # type: ignore[no-untyped-def]
        captured.append(trades)
        return []

    monkeypatch.setattr(advisor_module, "run_all_analyses", _capture)

    result = runner.invoke(app, ["lesson"])
    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    fed_actions = {t["action"] for t in captured[0]}
    assert fed_actions == {"open", "close", "size_up", "skip"}
