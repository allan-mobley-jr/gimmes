"""Tests for Kalshi API client retry logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gimmes.kalshi.client import KalshiClient, RateLimiter


class FakeConfig:
    """Minimal config for testing KalshiClient."""

    api_key = "test-key"
    private_key_path = type("P", (), {"exists": lambda self: False})()
    base_url = "https://api.example.com/trade-api/v2"


@pytest.fixture
def client():
    c = KalshiClient(FakeConfig())  # type: ignore[arg-type]
    # Bypass auth — tests inject headers manually
    c._private_key = "fake"
    c._get_auth_headers = lambda *a, **kw: {"Authorization": "test"}  # type: ignore[method-assign]
    return c


class TestRetry:
    @pytest.mark.asyncio
    async def test_retries_on_429(self, client: KalshiClient) -> None:
        """429 triggers retry with exponential backoff."""
        resp_429 = httpx.Response(
            429, headers={"Retry-After": "0.01"},
            request=httpx.Request("GET", "https://x/test"),
        )
        resp_200 = httpx.Response(
            200, json={"ok": True},
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://x/test"),
        )

        with patch.object(
            client._client, "request",
            new_callable=AsyncMock,
            side_effect=[resp_429, resp_200],
        ):
            result = await client.get("/test")
            assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_retries_on_5xx_for_reads(
        self, client: KalshiClient
    ) -> None:
        """5xx on GET triggers retry."""
        resp_502 = httpx.Response(
            502,
            request=httpx.Request("GET", "https://x/test"),
        )
        resp_200 = httpx.Response(
            200, json={"ok": True},
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://x/test"),
        )

        with patch.object(
            client._client, "request",
            new_callable=AsyncMock,
            side_effect=[resp_502, resp_200],
        ):
            result = await client.get("/test")
            assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_no_retry_on_5xx_for_writes(
        self, client: KalshiClient
    ) -> None:
        """5xx on POST does NOT retry (non-idempotent)."""
        resp_500 = httpx.Response(
            500,
            request=httpx.Request("POST", "https://x/test"),
        )

        with patch.object(
            client._client, "request",
            new_callable=AsyncMock,
            return_value=resp_500,
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await client.post("/test")

    @pytest.mark.asyncio
    async def test_retries_on_connect_error(
        self, client: KalshiClient
    ) -> None:
        """Network errors trigger retry for reads."""
        resp_200 = httpx.Response(
            200, json={"ok": True},
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://x/test"),
        )

        with patch.object(
            client._client, "request",
            new_callable=AsyncMock,
            side_effect=[httpx.ConnectError("fail"), resp_200],
        ):
            result = await client.get("/test")
            assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_validates_content_type(
        self, client: KalshiClient
    ) -> None:
        """Non-JSON response raises ValueError."""
        resp = httpx.Response(
            200, text="<html>maintenance</html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", "https://x/test"),
        )

        with patch.object(
            client._client, "request",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            with pytest.raises(ValueError, match="Unexpected response"):
                await client.get("/test")

    @pytest.mark.asyncio
    async def test_no_unbound_error_with_zero_retries(
        self, client: KalshiClient
    ) -> None:
        """max_retries=0 doesn't cause UnboundLocalError."""
        with pytest.raises(httpx.ConnectError):
            await client._request("GET", "/test", max_retries=0)


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_consumes_token(self) -> None:
        rl = RateLimiter(reads_per_sec=2, writes_per_sec=1)
        await rl.acquire(is_write=False)
        # Should have consumed one token
        assert rl._read_tokens < 2

    @pytest.mark.asyncio
    async def test_write_uses_write_tokens(self) -> None:
        rl = RateLimiter(reads_per_sec=20, writes_per_sec=2)
        await rl.acquire(is_write=True)
        assert rl._write_tokens < 2
