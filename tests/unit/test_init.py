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
    _copy_example_file,
    _find_downloaded_key,
    _install_private_key,
    _validate_pem_content,
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


class TestCopyExampleFile:
    def test_copies_when_target_missing(self, tmp_path: Path) -> None:
        example = tmp_path / "example.txt"
        example.write_text("content")
        target = tmp_path / "target.txt"

        result = _copy_example_file(example, target, "test file")

        assert result is True
        assert target.read_text() == "content"

    def test_skips_when_target_exists_and_user_declines(self, tmp_path: Path) -> None:
        example = tmp_path / "example.txt"
        example.write_text("new content")
        target = tmp_path / "target.txt"
        target.write_text("old content")

        with patch("gimmes.init.typer.confirm", return_value=False):
            result = _copy_example_file(example, target, "test file")

        assert result is False
        assert target.read_text() == "old content"

    def test_overwrites_when_target_exists_and_user_confirms(self, tmp_path: Path) -> None:
        example = tmp_path / "example.txt"
        example.write_text("new content")
        target = tmp_path / "target.txt"
        target.write_text("old content")

        with patch("gimmes.init.typer.confirm", return_value=True):
            result = _copy_example_file(example, target, "test file")

        assert result is True
        assert target.read_text() == "new content"


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

    def test_rejects_invalid_key(self, tmp_path: Path) -> None:
        source = tmp_path / "gimmes.txt"
        source.write_bytes(b"not a valid key")

        with patch("gimmes.init.KEYS_DIR", tmp_path / "keys"):
            result = _install_private_key(source)

        assert result is None
