"""Tests for gimmes.reporting.formatter."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from io import StringIO

import pytest
from rich.console import Console
from rich.table import Table

from gimmes.reporting.formatter import format_kv_table, format_local_timestamp


def _render_table(table: Table) -> str:
    """Render a table to text at a fixed 100-col width — wide enough
    that Rich wraps titles at the TABLE width, not the console width
    (a narrow console would split the asserted fragments)."""
    buf = StringIO()
    Console(file=buf, width=100).print(table)
    return buf.getvalue()


class TestFormatKvTable:
    def test_returns_table_with_correct_rows(self) -> None:
        rows = [("Key1", "Val1"), ("Key2", "Val2"), ("Key3", "Val3")]
        table = format_kv_table("My Title", rows)
        assert table.title == "My Title"
        assert table.row_count == 3

    def test_empty_rows(self) -> None:
        table = format_kv_table("Empty", [])
        assert table.row_count == 0

    def test_no_header(self) -> None:
        table = format_kv_table("T", [("k", "v")])
        assert table.show_header is False

    def test_title_with_brackets_survives_render(self) -> None:
        """Bracketed external text in the title must render verbatim
        (#644): the positive fragment catches markup-eating, the
        negative catches double-escaping (a pre-escaping caller would
        render a literal backslash — invisible to the positive check).
        """
        # Wide row: Rich wraps the title to the TABLE width, not
        # the console width — a narrow table would split the fragment.
        table = format_kv_table(
            "CPI [preliminary] April 2026",
            [("key", "value " * 8)],
        )
        out = _render_table(table)
        assert "[preliminary]" in out
        assert "\\[preliminary]" not in out

    def test_values_still_support_markup(self) -> None:
        """Row VALUES stay markup-parsed by contract (#644) — callers
        pass deliberate color tags (e.g. the Settlement Risk cell). A
        future blanket value-escape must fail here loudly."""
        table = format_kv_table("T", [("k", "[bold]42[/bold]")])
        out = _render_table(table)
        assert "42" in out
        assert "[bold]" not in out


@pytest.fixture()
def _tz_eastern() -> Iterator[None]:
    """Pin the local timezone to US/Eastern for deterministic tests."""
    orig = os.environ.get("TZ")
    os.environ["TZ"] = "US/Eastern"
    time.tzset()
    yield
    if orig is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = orig
    time.tzset()


@pytest.mark.usefixtures("_tz_eastern")
class TestFormatLocalTimestamp:
    def test_naive_utc_string(self) -> None:
        result = format_local_timestamp("2026-03-15 14:30:00")
        assert result == "2026-03-15 10:30:00"  # EDT is UTC-4

    def test_iso_with_utc_offset(self) -> None:
        result = format_local_timestamp("2026-03-15T14:30:00+00:00")
        assert result == "2026-03-15 10:30:00"

    def test_iso_with_z_suffix(self) -> None:
        result = format_local_timestamp("2026-03-15T14:30:00Z")
        assert result == "2026-03-15 10:30:00"

    def test_date_only(self) -> None:
        result = format_local_timestamp("2026-03-15 14:30:00", date_only=True)
        assert result == "2026-03-15"

    def test_date_boundary_crossing(self) -> None:
        # 2026-03-15 02:00 UTC -> 2026-03-14 22:00 EDT (previous day)
        result = format_local_timestamp("2026-03-15 02:00:00", date_only=True)
        assert result == "2026-03-14"

    def test_empty_string(self) -> None:
        assert format_local_timestamp("") == "--"

    def test_string_none_fallback(self) -> None:
        # str(None) == "None" -- a real scenario from dict.get() wrapping
        result = format_local_timestamp("None")
        assert result == "None"  # falls back to raw[:19]

    def test_garbage_input_fallback(self) -> None:
        result = format_local_timestamp("not-a-timestamp-at-all")
        assert result == "not-a-timestamp-at-"  # falls back to [:19]

    def test_winter_offset(self) -> None:
        # January -> EST (UTC-5), not EDT
        result = format_local_timestamp("2026-01-15 14:30:00")
        assert result == "2026-01-15 09:30:00"
