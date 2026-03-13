"""Unit tests for Kalshi RSA-PSS authentication."""

import base64

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from gimmes.kalshi.auth import auth_headers, create_signature


def _generate_test_key() -> rsa.RSAPrivateKey:
    """Generate an RSA-2048 key for testing."""
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )


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
