"""Tests for gimmes mode command."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from gimmes.cli import app
from gimmes.config import GimmesConfig, Mode

runner = CliRunner()


@asynccontextmanager
async def _fake_trading_context(config):
    """Async context manager that mimics trading_context without network."""
    mock_broker = AsyncMock()
    mock_broker.get_balance.return_value = 100.0
    yield MagicMock(), mock_broker, MagicMock()


class TestModeInitHint:
    """The mode command should guide unconfigured users to run 'gimmes init'."""

    def test_shows_init_hint_when_unconfigured(self, tmp_path: Path) -> None:
        """When api_key is empty, show the init hint."""
        unconfigured = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            db_path=tmp_path / "gimmes.db",
        )
        with (
            patch("gimmes.cli.load_config", return_value=unconfigured),
            patch("gimmes.store.session.get_active_session", return_value=None),
        ):
            result = runner.invoke(app, ["mode"])

        assert result.exit_code == 0
        assert "gimmes init" in result.output

    def test_no_hint_when_configured(self, tmp_path: Path) -> None:
        """When credentials are present, do not show the init hint."""
        key_file = tmp_path / "key.pem"
        key_file.write_text("fake-key")
        configured = GimmesConfig(
            mode=Mode.DRIVING_RANGE,
            api_key="test-key",
            private_key_path=key_file,
            db_path=tmp_path / "gimmes.db",
        )
        with (
            patch("gimmes.cli.load_config", return_value=configured),
            patch("gimmes.store.session.get_active_session", return_value=None),
            patch("gimmes.cli.trading_context", side_effect=_fake_trading_context),
        ):
            result = runner.invoke(app, ["mode"])

        assert result.exit_code == 0
        assert "gimmes init" not in result.output
