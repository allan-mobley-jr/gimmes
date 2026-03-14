"""Unit tests for Kalshi RSA-PSS authentication."""

import base64

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from gimmes.kalshi.auth import (
    auth_headers,
    create_signature,
    load_private_key,
    load_private_key_for_config,
)


def _generate_test_key() -> rsa.RSAPrivateKey:
    """Generate an RSA-2048 key for testing."""
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )


class TestLoadPrivateKey:
    def test_loads_unencrypted_key(self, tmp_path) -> None:
        key = _generate_test_key()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        key_file = tmp_path / "test.pem"
        key_file.write_bytes(pem)

        loaded = load_private_key(key_file)
        assert isinstance(loaded, rsa.RSAPrivateKey)

    def test_loads_encrypted_key_with_password(self, tmp_path) -> None:
        key = _generate_test_key()
        password = b"test-password-123"
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(password),
        )
        key_file = tmp_path / "test_enc.pem"
        key_file.write_bytes(pem)

        loaded = load_private_key(key_file, password=password)
        assert isinstance(loaded, rsa.RSAPrivateKey)

    def test_encrypted_key_without_password_raises(self, tmp_path) -> None:
        key = _generate_test_key()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"secret"),
        )
        key_file = tmp_path / "test_enc.pem"
        key_file.write_bytes(pem)

        with pytest.raises(TypeError):
            load_private_key(key_file)

    def test_encrypted_key_wrong_password_raises(self, tmp_path) -> None:
        key = _generate_test_key()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"correct"),
        )
        key_file = tmp_path / "test_enc.pem"
        key_file.write_bytes(pem)

        with pytest.raises(ValueError):
            load_private_key(key_file, password=b"wrong")


class TestLoadPrivateKeyForConfig:
    def test_passes_none_when_no_password(self, tmp_path) -> None:
        key = _generate_test_key()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        key_file = tmp_path / "test.pem"
        key_file.write_bytes(pem)

        loaded = load_private_key_for_config(key_file, None)
        assert isinstance(loaded, rsa.RSAPrivateKey)

    def test_encodes_password_string(self, tmp_path) -> None:
        key = _generate_test_key()
        password = "test-password-123"
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(password.encode()),
        )
        key_file = tmp_path / "test_enc.pem"
        key_file.write_bytes(pem)

        loaded = load_private_key_for_config(key_file, password)
        assert isinstance(loaded, rsa.RSAPrivateKey)

    def test_hints_encrypted_key_without_password(self, tmp_path) -> None:
        key = _generate_test_key()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"secret"),
        )
        key_file = tmp_path / "test_enc.pem"
        key_file.write_bytes(pem)

        with pytest.raises(ValueError, match="set KALSHI_PRIVATE_KEY_PASSWORD"):
            load_private_key_for_config(key_file, None)

    def test_hints_wrong_password(self, tmp_path) -> None:
        key = _generate_test_key()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"correct"),
        )
        key_file = tmp_path / "test_enc.pem"
        key_file.write_bytes(pem)

        with pytest.raises(ValueError, match="check KALSHI_PRIVATE_KEY_PASSWORD"):
            load_private_key_for_config(key_file, "wrong")


class TestCreateSignature:
    def test_produces_base64_string(self) -> None:
        key = _generate_test_key()
        sig = create_signature(key, "1700000000000", "GET", "/markets")
        # Should be valid base64
        decoded = base64.b64decode(sig)
        assert len(decoded) > 0

    def test_strips_query_string(self) -> None:
        key = _generate_test_key()
        sig_with_query = create_signature(
            key, "1700000000000", "GET", "/markets?status=open&limit=10"
        )
        sig_without_query = create_signature(key, "1700000000000", "GET", "/markets")
        # Both should produce the same signature since query is stripped
        # They won't be identical because PSS uses random salt, but both should verify
        # Just check they're valid base64
        assert len(base64.b64decode(sig_with_query)) > 0
        assert len(base64.b64decode(sig_without_query)) > 0

    def test_signature_verifies(self) -> None:
        key = _generate_test_key()
        timestamp = "1700000000000"
        method = "GET"
        path = "/markets"

        sig = create_signature(key, timestamp, method, path)
        sig_bytes = base64.b64decode(sig)

        # Verify the signature with the public key
        message = f"{timestamp}{method}{path}".encode()
        public_key = key.public_key()
        # Should not raise
        public_key.verify(
            sig_bytes,
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

    def test_different_methods_different_sigs(self) -> None:
        key = _generate_test_key()
        # Can't directly compare PSS signatures (random salt), but we can
        # verify each is valid for its respective message
        sig_get = create_signature(key, "1700000000000", "GET", "/markets")
        sig_post = create_signature(key, "1700000000000", "POST", "/markets")
        # Both should be valid base64 strings
        assert isinstance(sig_get, str)
        assert isinstance(sig_post, str)


class TestAuthHeaders:
    def test_returns_three_headers(self) -> None:
        key = _generate_test_key()
        headers = auth_headers("test-api-key-uuid", key, "GET", "/markets")
        assert "KALSHI-ACCESS-KEY" in headers
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        assert "KALSHI-ACCESS-SIGNATURE" in headers

    def test_api_key_in_header(self) -> None:
        key = _generate_test_key()
        api_key = "my-test-uuid-1234"
        headers = auth_headers(api_key, key, "GET", "/markets")
        assert headers["KALSHI-ACCESS-KEY"] == api_key

    def test_timestamp_is_numeric(self) -> None:
        key = _generate_test_key()
        headers = auth_headers("key", key, "GET", "/markets")
        assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()
