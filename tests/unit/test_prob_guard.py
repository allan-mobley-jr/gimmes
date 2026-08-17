"""#645: percent-form probabilities are rejected at parse time — the
Closer once passed --prob 85 and Kelly silently sized 0 contracts."""

from __future__ import annotations

import pytest
import typer
from typer.testing import CliRunner

from gimmes.cli import _PROB_RANGE_MSG, _prob_option_callback, app

runner = CliRunner()


def _flatten(output: str) -> str:
    """Rich draws the error in a box — strip borders AND rejoin the
    wrapped lines before asserting the full message."""
    return " ".join(
        output.replace("│", " ").replace("╭", " ")
        .replace("╰", " ").replace("─", " ").replace("╮", " ")
        .replace("╯", " ").split()
    )


class TestProbCallback:
    @pytest.mark.parametrize("value", [None, 0.0, 0.85, 1.0])
    def test_valid_values_pass_through(self, value) -> None:
        assert _prob_option_callback(value) == value

    @pytest.mark.parametrize(
        "value", [85.0, 1.01, -0.1, float("nan"), float("inf")],
    )
    def test_invalid_values_reject(self, value) -> None:
        with pytest.raises(typer.BadParameter) as exc:
            _prob_option_callback(value)
        assert _PROB_RANGE_MSG in str(exc.value)


class TestProbCliRejection:
    """Parse-time rejection: no config/mocks needed — the command body
    never runs. Rich wraps output, so assertions whitespace-normalize."""

    def test_order_rejects_percent_form(self) -> None:
        result = runner.invoke(
            app, ["order", "KXTEST-26AUG-T1", "--prob", "85", "--yes"],
        )
        assert result.exit_code != 0
        assert _PROB_RANGE_MSG in _flatten(result.output)

    def test_log_candidate_rejects_percent_form(self) -> None:
        result = runner.invoke(
            app, ["log-candidate", "KXTEST-26AUG-T1", "--prob", "85"],
        )
        assert result.exit_code != 0
        assert _PROB_RANGE_MSG in _flatten(result.output)

    @pytest.mark.parametrize("argv", [
        ["size", "KXTEST-26AUG-T1", "--prob", "85"],
        ["validate", "KXTEST-26AUG-T1", "--prob", "85"],
        ["log-trade", "KXTEST-26AUG-T1", "--action", "skip",
         "--prob", "85"],
    ])
    def test_other_commands_reject_percent_form(self, argv) -> None:
        """Wiring pin per command — removing the callback from any
        single option must fail a test (review-found survivors)."""
        result = runner.invoke(app, argv)
        assert result.exit_code != 0
        assert _PROB_RANGE_MSG in _flatten(result.output)
