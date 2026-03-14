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
    _clear_shell_history,
    _encrypt_private_key,
    _find_downloaded_key,
    _install_private_key,
    _secure_env_file,
    _update_env_var,
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


@pytest.fixture(scope="module")
def encrypted_pem(sample_pem: bytes) -> tuple[bytes, bytes]:
    """Encrypt sample_pem with a known password."""
    password = b"test-password-123"
    encrypted = _encrypt_private_key(sample_pem, password)
    return encrypted, password


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

    def test_encrypted_key_accepted_without_password(
        self, encrypted_pem: tuple[bytes, bytes]
    ) -> None:
        pem, _password = encrypted_pem
        assert _validate_pem_content(pem) is True

    def test_encrypted_key_with_correct_password(self, encrypted_pem: tuple[bytes, bytes]) -> None:
        pem, password = encrypted_pem
        assert _validate_pem_content(pem, password=password) is True

    def test_encrypted_key_with_wrong_password(self, encrypted_pem: tuple[bytes, bytes]) -> None:
        pem, _password = encrypted_pem
        assert _validate_pem_content(pem, password=b"wrong") is False


class TestEncryptPrivateKey:
    def test_encrypts_key(self, sample_pem: bytes) -> None:
        password = b"my-secret"
        encrypted = _encrypt_private_key(sample_pem, password)

        assert b"ENCRYPTED" in encrypted
        assert encrypted != sample_pem

    def test_encrypted_key_decrypts_correctly(self, sample_pem: bytes) -> None:
        password = b"roundtrip-test"
        encrypted = _encrypt_private_key(sample_pem, password)

        # Decrypt and verify the key material is equivalent
        original = serialization.load_pem_private_key(sample_pem, password=None)
        restored = serialization.load_pem_private_key(encrypted, password=password)

        assert original.private_numbers() == restored.private_numbers()


class TestInstallPrivateKey:
    def test_installs_and_encrypts_valid_key(self, tmp_path: Path, sample_pem: bytes) -> None:
        source = tmp_path / "gimmes.txt"
        source.write_bytes(sample_pem)
        password = b"install-test"

        with patch("gimmes.init.KEYS_DIR", tmp_path / "keys"):
            result = _install_private_key(source, password)

        assert result is not None
        assert result.exists()
        assert result.name == "kalshi_private.pem"
        # Installed key should be encrypted, not the original
        installed = result.read_bytes()
        assert b"ENCRYPTED" in installed
        assert installed != sample_pem
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
            result = _install_private_key(source, b"overwrite-test")

        assert result is not None
        installed = result.read_bytes()
        assert b"ENCRYPTED" in installed
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
            result = _install_private_key(source, b"decline-test")

        assert result is not None
        # Should still have old content
        existing.chmod(stat.S_IRUSR | stat.S_IWUSR)  # make readable for assertion
        assert existing.read_bytes() == b"old key content"

    def test_rejects_invalid_key(self, tmp_path: Path) -> None:
        source = tmp_path / "gimmes.txt"
        source.write_bytes(b"not a valid key")

        with patch("gimmes.init.KEYS_DIR", tmp_path / "keys"):
            result = _install_private_key(source, b"some-password")

        assert result is None

    def test_rejects_already_encrypted_key(
        self, tmp_path: Path, encrypted_pem: tuple[bytes, bytes]
    ) -> None:
        pem, _password = encrypted_pem
        source = tmp_path / "gimmes.txt"
        source.write_bytes(pem)

        with patch("gimmes.init.KEYS_DIR", tmp_path / "keys"):
            result = _install_private_key(source, b"new-password")

        assert result is None


class TestUpdateEnvVar:
    def test_updates_existing_var(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("KALSHI_PROD_API_KEY=old-value\n")

        with patch("gimmes.init.ENV_FILE", env_file):
            _update_env_var("KALSHI_PROD_API_KEY", "new-value")

        content = env_file.read_text()
        assert "KALSHI_PROD_API_KEY=new-value" in content
        assert "old-value" not in content

    def test_appends_missing_var(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("GIMMES_MODE=driving_range\n")

        with patch("gimmes.init.ENV_FILE", env_file):
            _update_env_var("KALSHI_PRIVATE_KEY_PASSWORD", "secret")

        content = env_file.read_text()
        assert "KALSHI_PRIVATE_KEY_PASSWORD=secret" in content

    def test_uncomments_commented_var(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("# KALSHI_PRIVATE_KEY_PASSWORD=\n")

        with patch("gimmes.init.ENV_FILE", env_file):
            _update_env_var("KALSHI_PRIVATE_KEY_PASSWORD", "secret")

        content = env_file.read_text()
        assert "KALSHI_PRIVATE_KEY_PASSWORD=secret" in content
        # Should not be commented out
        lines = [
            line for line in content.splitlines()
            if "KALSHI_PRIVATE_KEY_PASSWORD" in line
        ]
        assert len(lines) == 1
        assert not lines[0].startswith("#")

    def test_uncomments_no_space_after_hash(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("#KALSHI_PRIVATE_KEY_PASSWORD=\n")

        with patch("gimmes.init.ENV_FILE", env_file):
            _update_env_var("KALSHI_PRIVATE_KEY_PASSWORD", "secret")

        content = env_file.read_text()
        assert "KALSHI_PRIVATE_KEY_PASSWORD=secret" in content
        assert not content.strip().startswith("#")

    def test_does_not_match_similar_prefix(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("KALSHI_PROD_API_KEY_OLD=legacy\n")

        with patch("gimmes.init.ENV_FILE", env_file):
            _update_env_var("KALSHI_PROD_API_KEY", "new-value")

        content = env_file.read_text()
        # Should append, not replace the similar-prefix line
        assert "KALSHI_PROD_API_KEY_OLD=legacy" in content
        assert "KALSHI_PROD_API_KEY=new-value" in content

    def test_secures_env_file_after_update(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("FOO=bar\n")
        env_file.chmod(0o644)

        with patch("gimmes.init.ENV_FILE", env_file):
            _update_env_var("FOO", "baz")

        mode = env_file.stat().st_mode & 0o777
        assert mode == 0o600


class TestClearShellHistory:
    def test_clears_zsh_history_when_confirmed(self, tmp_path: Path) -> None:
        history = tmp_path / ".zsh_history"
        history.write_text("secret-api-key-12345\n")

        with (
            patch.dict(os.environ, {"SHELL": "/bin/zsh"}),
            patch("gimmes.init.Path.home", return_value=tmp_path),
            patch("gimmes.init.platform.system", return_value="Darwin"),
            patch("gimmes.init.typer.confirm", return_value=True),
        ):
            _clear_shell_history()

        assert history.read_text() == ""

    def test_clears_bash_history_when_confirmed(self, tmp_path: Path) -> None:
        history = tmp_path / ".bash_history"
        history.write_text("secret-api-key-12345\n")

        with (
            patch.dict(os.environ, {"SHELL": "/bin/bash"}),
            patch("gimmes.init.Path.home", return_value=tmp_path),
            patch("gimmes.init.platform.system", return_value="Linux"),
            patch("gimmes.init.typer.confirm", return_value=True),
        ):
            _clear_shell_history()

        assert history.read_text() == ""

    def test_skips_when_user_declines(self, tmp_path: Path) -> None:
        history = tmp_path / ".zsh_history"
        history.write_text("secret-api-key-12345\n")

        with (
            patch.dict(os.environ, {"SHELL": "/bin/zsh"}),
            patch("gimmes.init.Path.home", return_value=tmp_path),
            patch("gimmes.init.platform.system", return_value="Darwin"),
            patch("gimmes.init.typer.confirm", return_value=False),
        ):
            _clear_shell_history()

        # History should NOT be cleared
        assert history.read_text() == "secret-api-key-12345\n"

    def test_handles_missing_history_file(self, tmp_path: Path) -> None:
        """Should not error when history file doesn't exist."""
        with (
            patch.dict(os.environ, {"SHELL": "/bin/zsh"}),
            patch("gimmes.init.Path.home", return_value=tmp_path),
        ):
            _clear_shell_history()  # Should not raise
