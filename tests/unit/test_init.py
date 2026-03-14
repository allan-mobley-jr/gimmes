"""Tests for gimmes init module."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gimmes.init import (
    _find_downloaded_key,
    _install_private_key,
    _secure_env_file,
    _validate_pem_content,
    _write_default_file,
)


@pytest.fixture(scope="module")
def sample_pem() -> bytes:
    """Generate a real RSA private key in PEM format for testing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


class TestWriteDefaultFile:
    def test_writes_when_target_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"

        result = _write_default_file(target, "content", "test file")

        assert result is True
        assert target.read_text() == "content"

    def test_skips_when_target_exists_and_user_declines(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("old content")

        with patch("gimmes.init.typer.confirm", return_value=False):
            result = _write_default_file(target, "new content", "test file")

        assert result is False
        assert target.read_text() == "old content"

    def test_overwrites_when_target_exists_and_user_confirms(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("old content")

        with patch("gimmes.init.typer.confirm", return_value=True):
            result = _write_default_file(target, "new content", "test file")

        assert result is True
        assert target.read_text() == "new content"


class TestSecureEnvFile:
    def test_sets_600_permissions(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("KALSHI_PROD_API_KEY=secret")
        env_file.chmod(0o644)  # World-readable

        with patch("gimmes.init.ENV_FILE", env_file):
            _secure_env_file()

        mode = env_file.stat().st_mode & 0o777
        assert mode == 0o600

    def test_write_default_secures_env_file(self, tmp_path: Path) -> None:
        target = tmp_path / ".env"

        with patch("gimmes.init.ENV_FILE", target):
            _write_default_file(target, "KALSHI_PROD_API_KEY=placeholder", ".env")

        mode = target.stat().st_mode & 0o777
        assert mode == 0o600


class TestFindDownloadedKey:
    def test_finds_lowercase_gimmes_txt(self, tmp_path: Path, sample_pem: bytes) -> None:
        downloads = tmp_path / "Downloads"
        downloads.mkdir()
        key_file = downloads / "gimmes.txt"
        key_file.write_bytes(sample_pem)

        with patch("gimmes.init.Path.home", return_value=tmp_path):
            result = _find_downloaded_key()

        assert result == key_file

    def test_finds_capitalized_gimmes_txt(self, tmp_path: Path, sample_pem: bytes) -> None:
        downloads = tmp_path / "Downloads"
        downloads.mkdir()
        key_file = downloads / "Gimmes.txt"
        key_file.write_bytes(sample_pem)

        with patch("gimmes.init.Path.home", return_value=tmp_path):
            result = _find_downloaded_key()

        assert result == key_file

    def test_returns_none_when_no_match(self, tmp_path: Path, sample_pem: bytes) -> None:
        downloads = tmp_path / "Downloads"
        downloads.mkdir()
        # Wrong filename
        (downloads / "other_key.txt").write_bytes(sample_pem)

        with patch("gimmes.init.Path.home", return_value=tmp_path):
            result = _find_downloaded_key()

        assert result is None

    def test_returns_none_when_no_downloads_dir(self, tmp_path: Path) -> None:
        with patch("gimmes.init.Path.home", return_value=tmp_path):
            result = _find_downloaded_key()

        assert result is None

    def test_returns_most_recent_match(self, tmp_path: Path, sample_pem: bytes) -> None:
        downloads = tmp_path / "Downloads"
        downloads.mkdir()

        old = downloads / "gimmes.txt"
        old.write_bytes(sample_pem)

        new = downloads / "gimmes_2.txt"
        new.write_bytes(sample_pem)
        # Ensure different mtime
        os.utime(new, (old.stat().st_mtime + 10, old.stat().st_mtime + 10))

        with patch("gimmes.init.Path.home", return_value=tmp_path):
            result = _find_downloaded_key()

        assert result == new


class TestValidatePemContent:
    def test_valid_rsa_key(self, sample_pem: bytes) -> None:
        assert _validate_pem_content(sample_pem) is True

    def test_invalid_content(self) -> None:
        assert _validate_pem_content(b"not a key") is False

    def test_empty_content(self) -> None:
        assert _validate_pem_content(b"") is False

    def test_truncated_pem(self) -> None:
        assert _validate_pem_content(b"-----BEGIN RSA PRIVATE KEY-----\nfoo\n") is False


class TestInstallPrivateKey:
    def test_installs_valid_key(self, tmp_path: Path, sample_pem: bytes) -> None:
        source = tmp_path / "gimmes.txt"
        source.write_bytes(sample_pem)

        with patch("gimmes.init.KEYS_DIR", tmp_path / "keys"):
            result = _install_private_key(source)

        assert result is not None
        assert result.exists()
        assert result.name == "kalshi_private.pem"
        assert result.read_bytes() == sample_pem
        # Check permissions are restrictive
        mode = result.stat().st_mode
        assert mode & stat.S_IRUSR  # Owner can read
        assert not (mode & stat.S_IWUSR)  # Owner can't write
        assert not (mode & stat.S_IRGRP)  # Group can't read
        assert not (mode & stat.S_IROTH)  # Others can't read

    def test_overwrites_existing_key_when_confirmed(
        self, tmp_path: Path, sample_pem: bytes
    ) -> None:
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        existing = keys_dir / "kalshi_private.pem"
        existing.write_bytes(b"old key content")
        existing.chmod(stat.S_IRUSR)  # 0400, like a previous install

        source = tmp_path / "gimmes.txt"
        source.write_bytes(sample_pem)

        with patch("gimmes.init.KEYS_DIR", keys_dir), patch(
            "gimmes.init.typer.confirm", return_value=True
        ):
            result = _install_private_key(source)

        assert result is not None
        assert result.read_bytes() == sample_pem
        mode = result.stat().st_mode
        assert mode & stat.S_IRUSR
        assert not (mode & stat.S_IWUSR)

    def test_keeps_existing_key_when_declined(
        self, tmp_path: Path, sample_pem: bytes
    ) -> None:
        keys_dir = tmp_path / "keys"
        keys_dir.mkdir()
        existing = keys_dir / "kalshi_private.pem"
        existing.write_bytes(b"old key content")
        existing.chmod(stat.S_IRUSR)

        source = tmp_path / "gimmes.txt"
        source.write_bytes(sample_pem)

        with patch("gimmes.init.KEYS_DIR", keys_dir), patch(
            "gimmes.init.typer.confirm", return_value=False
        ):
            result = _install_private_key(source)

        assert result is not None
        # Should still have old content
        existing.chmod(stat.S_IRUSR | stat.S_IWUSR)  # make readable for assertion
        assert existing.read_bytes() == b"old key content"

    def test_rejects_invalid_key(self, tmp_path: Path) -> None:
        source = tmp_path / "gimmes.txt"
        source.write_bytes(b"not a valid key")

        with patch("gimmes.init.KEYS_DIR", tmp_path / "keys"):
            result = _install_private_key(source)

        assert result is None
