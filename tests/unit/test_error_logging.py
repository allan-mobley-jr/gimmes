"""Tests for the error logging system."""

from __future__ import annotations

from pathlib import Path

import pytest

from gimmes.models.error import ErrorCategory, ErrorLogEntry, ErrorSeverity
from gimmes.store.database import Database
from gimmes.store.queries import (
    get_error_summary,
    get_errors,
    insert_error,
    insert_error_if_changed,
    resolve_error,
)


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
        await insert_error(db, entry)

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


class TestInsertErrorIfChanged:
    """#819: the two dedupe policies the #783 / #767 call sites rely on."""

    @staticmethod
    def _entry(ctx: str, code: str = "X", component: str = "cli.mark") -> ErrorLogEntry:
        return ErrorLogEntry(
            severity=ErrorSeverity.WARNING,
            category=ErrorCategory.DATA_INTEGRITY,
            error_code=code, component=component,
            message="m", context=ctx,
        )

    @staticmethod
    async def _count(db: Database, code: str = "X") -> int:
        cur = await db.conn.execute(
            "SELECT COUNT(*) FROM error_log WHERE error_code = ?", (code,),
        )
        return (await cur.fetchone())[0]

    @pytest.mark.parametrize("policy", ["latest_unresolved", "latest_any"])
    async def test_empty_log_inserts(self, db: Database, policy: str) -> None:
        assert await insert_error_if_changed(db, self._entry("A"), dedupe=policy)
        assert await self._count(db) == 1

    @pytest.mark.parametrize("policy", ["latest_unresolved", "latest_any"])
    async def test_equal_context_skips(self, db: Database, policy: str) -> None:
        await insert_error_if_changed(db, self._entry("A"), dedupe=policy)
        assert not await insert_error_if_changed(db, self._entry("A"), dedupe=policy)
        assert await self._count(db) == 1

    @pytest.mark.parametrize("policy", ["latest_unresolved", "latest_any"])
    async def test_different_context_inserts(self, db: Database, policy: str) -> None:
        await insert_error_if_changed(db, self._entry("A"), dedupe=policy)
        assert await insert_error_if_changed(db, self._entry("B"), dedupe=policy)
        assert await self._count(db) == 2

    async def test_compares_latest_not_any(self, db: Database) -> None:
        for ctx in ("A", "B"):
            await insert_error_if_changed(db, self._entry(ctx), dedupe="latest_any")
        assert await insert_error_if_changed(db, self._entry("A"), dedupe="latest_any")

    async def test_resolved_latest_suppresses_under_latest_any(self, db: Database) -> None:
        row = await insert_error(db, self._entry("A"))
        await resolve_error(db, row)
        assert not await insert_error_if_changed(db, self._entry("A"), dedupe="latest_any")

    async def test_resolved_latest_rearms_under_latest_unresolved(self, db: Database) -> None:
        row = await insert_error(db, self._entry("A"))
        await resolve_error(db, row)
        assert await insert_error_if_changed(
            db, self._entry("A"), dedupe="latest_unresolved",
        )

    async def test_latest_unresolved_skips_resolved_newer_row(self, db: Database) -> None:
        await insert_error(db, self._entry("A"))
        newer = await insert_error(db, self._entry("B"))
        await resolve_error(db, newer)
        assert not await insert_error_if_changed(
            db, self._entry("A"), dedupe="latest_unresolved",
        )

    async def test_scoped_by_error_code(self, db: Database) -> None:
        await insert_error(db, self._entry("A", code="Y"))
        assert await insert_error_if_changed(db, self._entry("A"), dedupe="latest_any")

    async def test_component_not_in_key(self, db: Database) -> None:
        """One condition reported by cli.mark and cli.positions shares
        one dedupe chain — a second sweep must not double-log it."""
        await insert_error_if_changed(
            db, self._entry("A", component="cli.mark"), dedupe="latest_unresolved",
        )
        assert not await insert_error_if_changed(
            db, self._entry("A", component="cli.positions"),
            dedupe="latest_unresolved",
        )

    async def test_log_cli_error_dedupe_fails_open(self, db: Database) -> None:
        from unittest.mock import AsyncMock, patch

        from gimmes.cli import _log_cli_error

        with patch(
            "gimmes.store.queries.insert_error_if_changed",
            AsyncMock(side_effect=RuntimeError("db down")),
        ):
            await _log_cli_error(db, self._entry("A"), dedupe="latest_any")
        assert await self._count(db) == 0

    async def test_log_cli_error_without_dedupe_always_writes(self, db: Database) -> None:
        from gimmes.cli import _log_cli_error

        for _ in range(2):
            await _log_cli_error(db, self._entry("A"))
        assert await self._count(db) == 2

    async def test_unknown_policy_fails_loudly(self, db: Database) -> None:
        from gimmes.cli import _log_cli_error

        with pytest.raises(ValueError, match="unknown dedupe policy"):
            await _log_cli_error(db, self._entry("A"), dedupe="latest_uresolved")  # type: ignore[arg-type]
