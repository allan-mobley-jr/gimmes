"""Regression guard for issue #519.

The `size`, `validate`, and `order` CLI commands each define a nested async
function that calls `apply_base_rate_floor(probability, ...)`. If the result
is assigned back to `probability`, Python treats `probability` as a local
throughout the nested function, and reading it on the right-hand side raises
UnboundLocalError. This test enforces that the reassignment uses a different
name (e.g., `true_prob`).

Uses Python's `symtable` module so any binding mechanism is caught —
plain assignment, augmented assignment, annotated assignment, walrus,
tuple unpacking, `for` target, `with ... as probability`, etc. All of these
mark a name as local and all of them would reintroduce the bug.
"""

from __future__ import annotations

import symtable
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parents[2] / "src" / "gimmes" / "cli.py"
GUARDED_FUNCTIONS = ("_size", "_validate", "_order")


def _find_nested(parent: symtable.SymbolTable, name: str) -> symtable.Function | None:
    for child in parent.get_children():
        if child.get_name() == name and isinstance(child, symtable.Function):
            return child
        found = _find_nested(child, name)
        if found is not None:
            return found
    return None


@pytest.mark.parametrize("func_name", GUARDED_FUNCTIONS)
def test_probability_parameter_is_not_shadowed(func_name: str) -> None:
    table = symtable.symtable(CLI_PATH.read_text(), str(CLI_PATH), "exec")
    func = _find_nested(table, func_name)
    assert func is not None, f"{func_name} not found in cli.py"

    try:
        symbol = func.lookup("probability")
    except KeyError:
        return

    assert not symbol.is_local(), (
        f"{func_name} binds `probability` as a local, shadowing the outer "
        f"parameter. This causes UnboundLocalError (issue #519). Rename the "
        f"local to a distinct name (e.g., `true_prob`)."
    )
