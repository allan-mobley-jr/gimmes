"""Unit tests for config sync."""

from __future__ import annotations

from pathlib import Path

import tomlkit

from gimmes.config_sync import (
    _flatten_keys,
    _remove_nested,
    new_keys,
    sync_config,
)


def _write_toml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestFlattenKeys:
    def test_simple(self) -> None:
        doc = tomlkit.parse("[strategy]\ngimme_threshold = 75\n")
        assert _flatten_keys(doc) == {"strategy.gimme_threshold"}

    def test_nested(self) -> None:
        doc = tomlkit.parse("[scoring.weights]\nedge_size = 0.30\n")
        assert _flatten_keys(doc) == {"scoring.weights.edge_size"}

    def test_empty(self) -> None:
        doc = tomlkit.document()
        assert _flatten_keys(doc) == set()

    def test_multiple_sections(self) -> None:
        doc = tomlkit.parse("[a]\nx = 1\n[b]\ny = 2\n")
        assert _flatten_keys(doc) == {"a.x", "b.y"}


class TestRemoveNested:
    def test_removes_leaf(self) -> None:
        doc = tomlkit.parse("[strategy]\na = 1\nb = 2\n")
        _remove_nested(doc, "strategy.b")
        assert "b" not in doc["strategy"]
        assert doc["strategy"]["a"] == 1

    def test_prunes_empty_parent(self) -> None:
        doc = tomlkit.parse("[old_section]\nkey = 1\n")
        _remove_nested(doc, "old_section.key")
        assert "old_section" not in doc

    def test_no_op_for_missing_key(self) -> None:
        doc = tomlkit.parse("[a]\nx = 1\n")
        _remove_nested(doc, "a.nonexistent")
        assert doc["a"]["x"] == 1


class TestSyncConfig:
    def test_no_changes(self, tmp_path: Path) -> None:
        content = "[strategy]\ngimme_threshold = 75\n"
        _write_toml(tmp_path / "user.toml", content)
        _write_toml(tmp_path / "example.toml", content)

        result = sync_config(tmp_path / "user.toml", tmp_path / "example.toml")
        assert result.added == []
        assert result.removed == []

    def test_adds_new_key(self, tmp_path: Path) -> None:
        _write_toml(tmp_path / "user.toml", "[strategy]\na = 1\n")
        _write_toml(tmp_path / "example.toml", "[strategy]\na = 1\nb = 2\n")

        result = sync_config(tmp_path / "user.toml", tmp_path / "example.toml")
        assert result.added == ["strategy.b"]
        assert result.removed == []

        # Verify the file was updated
        doc = tomlkit.parse((tmp_path / "user.toml").read_text())
        assert doc["strategy"]["b"] == 2

    def test_removes_deprecated_key(self, tmp_path: Path) -> None:
        _write_toml(tmp_path / "user.toml", "[strategy]\na = 1\nold = 99\n")
        _write_toml(tmp_path / "example.toml", "[strategy]\na = 1\n")

        result = sync_config(tmp_path / "user.toml", tmp_path / "example.toml")
        assert result.added == []
        assert result.removed == ["strategy.old"]

        doc = tomlkit.parse((tmp_path / "user.toml").read_text())
        assert "old" not in doc["strategy"]

    def test_preserves_existing_values(self, tmp_path: Path) -> None:
        _write_toml(tmp_path / "user.toml", "[strategy]\na = 99\n")
        _write_toml(tmp_path / "example.toml", "[strategy]\na = 1\nb = 2\n")

        sync_config(tmp_path / "user.toml", tmp_path / "example.toml")

        doc = tomlkit.parse((tmp_path / "user.toml").read_text())
        assert doc["strategy"]["a"] == 99  # user's value preserved
        assert doc["strategy"]["b"] == 2  # new key added

    def test_both_add_and_remove(self, tmp_path: Path) -> None:
        _write_toml(tmp_path / "user.toml", "[strategy]\na = 1\nold = 99\n")
        _write_toml(tmp_path / "example.toml", "[strategy]\na = 1\nnew = 42\n")

        result = sync_config(tmp_path / "user.toml", tmp_path / "example.toml")
        assert result.added == ["strategy.new"]
        assert result.removed == ["strategy.old"]


class TestNewKeys:
    def test_returns_keys_at_default(self, tmp_path: Path) -> None:
        _write_toml(tmp_path / "user.toml", "[strategy]\na = 1\n")
        _write_toml(tmp_path / "example.toml", "[strategy]\na = 1\n")

        result = new_keys(tmp_path / "user.toml", tmp_path / "example.toml")
        assert "strategy.a" in result

    def test_excludes_customized_keys(self, tmp_path: Path) -> None:
        _write_toml(tmp_path / "user.toml", "[strategy]\na = 99\n")
        _write_toml(tmp_path / "example.toml", "[strategy]\na = 1\n")

        result = new_keys(tmp_path / "user.toml", tmp_path / "example.toml")
        assert "strategy.a" not in result

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = new_keys(tmp_path / "nope.toml", tmp_path / "also_nope.toml")
        assert result == set()
