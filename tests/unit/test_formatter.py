"""Tests for gimmes.reporting.formatter."""

from gimmes.reporting.formatter import format_kv_table


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
