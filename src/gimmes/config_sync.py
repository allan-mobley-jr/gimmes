"""Config sync — keep user's gimmes.toml aligned with the example template."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

from gimmes.config_wizard import _get_nested, _load_toml, _save_toml, _set_nested

# Example config lives in the repo checkout
EXAMPLE_TOML_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "gimmes.example.toml"


@dataclass
class SyncResult:
    """Summary of config sync changes."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _flatten_keys(doc: tomlkit.TOMLDocument, prefix: str = "") -> set[str]:
    """Recursively collect all leaf dotted keys from a TOML document."""
    keys: set[str] = set()
    for key, value in doc.items():
        dotted = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            keys.update(_flatten_keys(value, dotted))
        else:
            keys.add(dotted)
    return keys


def _remove_nested(doc: tomlkit.TOMLDocument, dotted_key: str) -> None:
    """Remove a leaf key from a TOML document, pruning empty parent tables."""
    parts = dotted_key.split(".")
    # Navigate to the parent
    parents: list[tuple[dict, str]] = []
    current: dict = doc  # type: ignore[assignment]
    for part in parts[:-1]:
        if part not in current:
            return
        parents.append((current, part))
        current = current[part]
    # Remove the leaf
    leaf = parts[-1]
    if leaf in current:
        del current[leaf]
    # Prune empty parent tables bottom-up
    for parent, key in reversed(parents):
        child = parent[key]
        if isinstance(child, dict) and len(child) == 0:
            del parent[key]


def sync_config(
    user_path: Path,
    example_path: Path | None = None,
) -> SyncResult:
    """Sync user config with the example template.

    Adds new keys from the example, removes deprecated keys not in the
    example. Never modifies existing user values.
    """
    example_path = example_path or EXAMPLE_TOML_PATH
    if not example_path.exists():
        raise FileNotFoundError(f"Example config not found: {example_path}")
    if not user_path.exists():
        raise FileNotFoundError(f"User config not found: {user_path}")

    example_doc = _load_toml(example_path)
    user_doc = _load_toml(user_path)

    example_keys = _flatten_keys(example_doc)
    if not example_keys:
        raise ValueError("Example config has no keys — refusing to sync")

    user_keys = _flatten_keys(user_doc)

    to_add = sorted(example_keys - user_keys)
    to_remove = sorted(user_keys - example_keys)

    for key in to_remove:
        _remove_nested(user_doc, key)

    for key in to_add:
        value = _get_nested(example_doc, key)
        _set_nested(user_doc, key, value)

    if to_add or to_remove:
        _save_toml(user_doc, user_path)

    return SyncResult(added=to_add, removed=to_remove)


def new_keys(
    user_path: Path,
    example_path: Path | None = None,
) -> set[str]:
    """Return dotted keys in user config that still have their example default value."""
    example_path = example_path or EXAMPLE_TOML_PATH
    if not user_path.exists() or not example_path.exists():
        return set()
    example_doc = _load_toml(example_path)
    user_doc = _load_toml(user_path)
    result: set[str] = set()
    for key in _flatten_keys(example_doc):
        user_val = _get_nested(user_doc, key)
        example_val = _get_nested(example_doc, key)
        if user_val is not None and user_val == example_val:
            result.add(key)
    return result
