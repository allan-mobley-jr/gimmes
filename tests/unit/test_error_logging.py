"""Tests for the error logging system."""

from __future__ import annotations

from pathlib import Path

import pytest

from gimmes.models.error import ErrorCategory, ErrorLogEntry, ErrorSeverity
from gimmes.store.database import Database
from gimmes.store.queries import get_error_summary, get_errors, insert_error, resolve_error


@pytest.fixture
async def db(tmp_path: Path) -> Database:
    """Create a temporary database with schema + migrations."""
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


class TestErrorModel:
    def test_defaults(self) -> None:
        entry = ErrorLogEntry(message="test")
        assert entry.severity == ErrorSeverity.ERROR
        assert entry.category == ErrorCategory.API_ERROR
        assert entry.resolved is False
        assert entry.context == "{}"

    def test_all_severities(self) -> None:
        for sev in ErrorSeverity:
            entry = ErrorLogEntry(severity=sev, message="test")
            assert entry.severity == sev

    def test_all_categories(self) -> None:
        for cat in ErrorCategory:
            entry = ErrorLogEntry(category=cat, message="test")
            assert entry.category == cat


class TestErrorQueries:
    async def test_insert_and_get(self, db: Database) -> None:
        entry = ErrorLogEntry(
            severity=ErrorSeverity.ERROR,
            category=ErrorCategory.API_ERROR,
            error_code="KALSHI_500",
            component="kalshi.client",
            agent="scout",
            cycle=1,
            message="Internal server error",
        )
        row_id = await insert_error(db, entry)
        assert row_id > 0

        errors = await get_errors(db)
        assert len(errors) == 1
        assert errors[0]["severity"] == "error"
        assert errors[0]["category"] == "api_error"
        assert errors[0]["error_code"] == "KALSHI_500"
        assert errors[0]["message"] == "Internal server error"
        assert errors[0]["resolved"] == 0

    async def test_filter_by_severity(self, db: Database) -> None:
        await insert_error(db, ErrorLogEntry(
            severity=ErrorSeverity.ERROR, message="err",
        ))
        await insert_error(db, ErrorLogEntry(
            severity=ErrorSeverity.WARNING, message="warn",
        ))

        errors = await get_errors(db, severity="error")
        assert len(errors) == 1
        assert errors[0]["severity"] == "error"

    async def test_filter_by_category(self, db: Database) -> None:
        await insert_error(db, ErrorLogEntry(
            category=ErrorCategory.API_ERROR, message="api",
        ))
        await insert_error(db, ErrorLogEntry(
            category=ErrorCategory.AUTH_FAILURE, message="auth",
        ))

        errors = await get_errors(db, category="auth_failure")
        assert len(errors) == 1
        assert errors[0]["category"] == "auth_failure"

    async def test_filter_unresolved(self, db: Database) -> None:
        entry = ErrorLogEntry(message="unresolved")
        row_id = await insert_error(db, entry)

        resolved_entry = ErrorLogEntry(message="resolved", resolved=True)
        await insert_error(db, resolved_entry)

        errors = await get_errors(db, unresolved=True)
        assert len(errors) == 1
        assert errors[0]["message"] == "unresolved"

    async def test_resolve_error(self, db: Database) -> None:
        entry = ErrorLogEntry(message="to resolve")
        row_id = await insert_error(db, entry)

        await resolve_error(db, row_id, "https://github.com/example/issues/1")

        errors = await get_errors(db)
        assert errors[0]["resolved"] == 1
        assert errors[0]["github_issue_url"] == "https://github.com/example/issues/1"

    async def test_error_summary(self, db: Database) -> None:
        await insert_error(db, ErrorLogEntry(
            severity=ErrorSeverity.ERROR,
            category=ErrorCategory.API_ERROR,
            message="err1",
        ))
        await insert_error(db, ErrorLogEntry(
            severity=ErrorSeverity.ERROR,
            category=ErrorCategory.API_ERROR,
            message="err2",
        ))
        await insert_error(db, ErrorLogEntry(
            severity=ErrorSeverity.WARNING,
            category=ErrorCategory.NETWORK_ERROR,
            message="warn1",
        ))

        summary = await get_error_summary(db)
        assert len(summary) == 2

        # API errors should have count=2
        api_row = next(r for r in summary if r["category"] == "api_error")
        assert api_row["count"] == 2
        assert api_row["unresolved"] == 2

    async def test_limit(self, db: Database) -> None:
        for i in range(10):
            await insert_error(db, ErrorLogEntry(message=f"error {i}"))

        errors = await get_errors(db, limit=3)
        assert len(errors) == 3


class TestMigrationV3:
    async def test_error_log_table_exists_after_connect(self, db: Database) -> None:
        cursor = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='error_log'"
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_schema_version_is_3(self, db: Database) -> None:
        cursor = await db.conn.execute(
            "SELECT MAX(version) FROM schema_version"
        )
        row = await cursor.fetchone()
        assert row[0] >= 3
