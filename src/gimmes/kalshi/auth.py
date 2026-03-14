"""RSA-PSS authentication for Kalshi API.

Signs each request with: timestamp_ms + METHOD + path_without_query
using RSA-PSS (SHA-256, MAX_LENGTH salt).
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


def load_private_key(key_path: Path, password: bytes | None = None) -> RSAPrivateKey:
    """Load an RSA private key from a PEM file."""
    key_data = key_path.read_bytes()
    private_key = serialization.load_pem_private_key(key_data, password=password)
    if not isinstance(private_key, RSAPrivateKey):
        raise TypeError(f"Expected RSA private key, got {type(private_key).__name__}")
    return private_key


def load_private_key_for_config(
    key_path: Path, password_str: str | None
) -> RSAPrivateKey:
    """Load a private key using config-style password, with user-friendly errors.

    Wraps load_private_key with clear error messages that guide the user
    to set or check KALSHI_PRIVATE_KEY_PASSWORD.
    """
    password = password_str.encode() if password_str else None
    try:
        return load_private_key(key_path, password=password)
    except TypeError as e:
        # Distinguish "encrypted but no password" from "wrong key type"
        if "encrypt" in str(e).lower():
            raise ValueError(
                f"Failed to load private key from {key_path}: {e} "
                "(key appears encrypted — set "
                "KALSHI_PRIVATE_KEY_PASSWORD)"
            ) from e
        raise ValueError(
            f"Failed to load private key from {key_path}: {e}"
        ) from e
    except ValueError as e:
        msg = f"Failed to load private key from {key_path}: {e}"
        if password is not None:
            msg += " (check KALSHI_PRIVATE_KEY_PASSWORD)"
        raise ValueError(msg) from e
    except Exception as e:
        raise ValueError(
            f"Failed to load private key from {key_path}: {e}"
        ) from e


def create_signature(private_key: RSAPrivateKey, timestamp_ms: str, method: str, path: str) -> str:
    """Create a Kalshi API signature.

    Args:
        private_key: RSA private key.
        timestamp_ms: Millisecond timestamp as string.
        method: HTTP method (uppercase).
        path: Request path (query string stripped internally).

    Returns:
        Base64-encoded signature string.
    """
    path_without_query = path.split("?")[0]
    message = f"{timestamp_ms}{method}{path_without_query}"
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def auth_headers(
    api_key: str, private_key: RSAPrivateKey, method: str, path: str
) -> dict[str, str]:
    """Generate the three Kalshi authentication headers.

    Returns:
        Dict with KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE.
    """
    timestamp_ms = str(int(time.time() * 1000))
    signature = create_signature(private_key, timestamp_ms, method.upper(), path)
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": signature,
    }
