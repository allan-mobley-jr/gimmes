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

import pytest
from typer.testing import CliRunner

from gimmes import cli as cli_module
from gimmes.cli import app
from gimmes.store import queries as queries_module

runner = CliRunner()


def _real_config(tmp_path: Path):  # type: ignore[no-untyped-def]
    from gimmes.config import GimmesConfig, Mode

    cfg = GimmesConfig(mode=Mode.DRIVING_RANGE)
    # db_path is derived — override via object.__setattr__-safe route
    cfg2 = cfg.model_copy(update={"db_path": tmp_path / "test.db"})
    return cfg2


def test_lesson_fetches_per_action_with_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real config (#686 review): a MagicMock fabricates attribute
    # paths — the lesson_window_days placement bug was invisible
    # until the real model was exercised.
    cfg = _real_config(tmp_path)
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
    # #686: the window bounds SKIPS at the DB (volume lives there);
    # open/close/size_up stay unbounded — the pairing walk needs full
    # history, the cutoff applies post-pairing.
    for c in calls:
        if c.get("action") == "skip":
            assert c.get("since") is not None
        else:
            assert c.get("since") is None


def test_lesson_feeds_all_actions_to_analyses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The four fetches must actually reach run_all_analyses —
    dropping skips from the concatenation would starve
    analyze_missed_opportunities."""
    from gimmes.strategy import advisor as advisor_module

    cfg = _real_config(tmp_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

    async def _sentinels(_db, **kwargs):  # type: ignore[no-untyped-def]
        return [{"action": kwargs["action"], "ticker": f"S-{kwargs['action']}"}]

    monkeypatch.setattr(queries_module, "get_trades", _sentinels)

    captured: list[list] = []

    windows: list = []

    def _capture(trades, _candidates, _config, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(trades)
        windows.append(kwargs.get("since"))
        return []

    monkeypatch.setattr(advisor_module, "run_all_analyses", _capture)

    result = runner.invoke(app, ["lesson"])
    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    fed_actions = {t["action"] for t in captured[0]}
    assert fed_actions == {"open", "close", "size_up", "skip"}
    # #686: the window cutoff must actually reach the analyses —
    # default config implies a non-None ISO cutoff.
    assert len(windows) == 1
    assert windows[0] is not None
    assert "T" in str(windows[0])


def test_zero_window_means_all_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#686: lesson_window_days = 0 disables the window — every fetch
    unbounded (the documented escape hatch for sparse deployments)."""
    cfg = _real_config(tmp_path)
    cfg.strategy.lesson_window_days = 0
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

    calls: list[dict] = []

    async def _spy(_db, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return []

    monkeypatch.setattr(queries_module, "get_trades", _spy)

    result = runner.invoke(app, ["lesson"])
    assert result.exit_code == 0, result.output
    assert all(c.get("since") is None for c in calls)


def test_truncation_warning_at_exact_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#686: a fetch returning exactly the 100k cap warns (no silent
    caps); one row under stays silent."""
    cfg = _real_config(tmp_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: cfg)

    async def _spy(_db, **kwargs):  # type: ignore[no-untyped-def]
        n = 100_000 if kwargs.get("action") == "skip" else 99_999
        return [
            {"action": kwargs["action"], "ticker": "X",
             "timestamp": "2026-06-01T10:00:00"}
        ] * n

    monkeypatch.setattr(queries_module, "get_trades", _spy)

    result = runner.invoke(app, ["lesson"])
    assert result.exit_code == 0, result.output
    assert result.output.count("hit the 100000-row limit") == 1
    assert "'skip'" in result.output
