"""Tests for Kalshi API client retry logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from gimmes.kalshi.client import KalshiClient, RateLimiter


class FakeConfig:
    """Minimal config for testing KalshiClient."""

    api_key = "test-key"
    private_key_path = type("P", (), {
        "exists": lambda self: True,
        "__str__": lambda self: "/fake/key.pem",
    })()
    base_url = "https://api.example.com/trade-api/v2"


@pytest.fixture
def client():
    with patch("gimmes.kalshi.client.load_private_key", return_value="fake"):
        c = KalshiClient(FakeConfig())  # type: ignore[arg-type]
    c._get_auth_headers = lambda *a, **kw: {"Authorization": "test"}  # type: ignore[method-assign]
    return c


def _json_response(status=200, json_data=None, headers=None):
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    return httpx.Response(
        status, json=json_data or {"ok": True}, headers=h,
        request=httpx.Request("GET", "https://x/test"),
    )


def _error_response(status, headers=None):
    return httpx.Response(
        status, headers=headers or {},
        request=httpx.Request("GET", "https://x/test"),
    )


class TestRetry:
    @pytest.mark.asyncio
    async def test_retries_on_429(self, client: KalshiClient) -> None:
        with (
            patch.object(
                client._client, "request",
                new_callable=AsyncMock,
                side_effect=[
                    _error_response(429, {"Retry-After": "0.01"}),
                    _json_response(),
                ],
            ),
            patch("gimmes.kalshi.client.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await client.get("/test")
            assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_retries_on_5xx_for_reads(
        self, client: KalshiClient
    ) -> None:
        with (
            patch.object(
                client._client, "request",
                new_callable=AsyncMock,
                side_effect=[_error_response(502), _json_response()],
            ),
            patch("gimmes.kalshi.client.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await client.get("/test")
            assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_no_retry_on_5xx_for_writes(
        self, client: KalshiClient
    ) -> None:
        with patch.object(
            client._client, "request",
            new_callable=AsyncMock,
            return_value=_error_response(500),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await client.post("/test")

    @pytest.mark.asyncio
    async def test_retries_on_connect_error(
        self, client: KalshiClient
    ) -> None:
        with (
            patch.object(
                client._client, "request",
                new_callable=AsyncMock,
                side_effect=[httpx.ConnectError("fail"), _json_response()],
            ),
            patch("gimmes.kalshi.client.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await client.get("/test")
            assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_no_retry_on_network_error_for_writes(
        self, client: KalshiClient
    ) -> None:
        """Writes never retry on network errors."""
        mock_req = AsyncMock(side_effect=httpx.ConnectError("fail"))
        with patch.object(client._client, "request", mock_req):
            with pytest.raises(httpx.ConnectError):
                await client.post("/test")
        assert mock_req.call_count == 1

    @pytest.mark.asyncio
    async def test_validates_content_type(
        self, client: KalshiClient
    ) -> None:
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
        with pytest.raises(httpx.ConnectError):
            await client._request("GET", "/test", max_retries=0)

    @pytest.mark.asyncio
    async def test_reraises_original_exception(
        self, client: KalshiClient
    ) -> None:
        """All retries fail — re-raises the original exception type."""
        with (
            patch.object(
                client._client, "request",
                new_callable=AsyncMock,
                side_effect=httpx.ReadTimeout("timed out"),
            ),
            patch("gimmes.kalshi.client.asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(httpx.ReadTimeout):
                await client.get("/test")

    @pytest.mark.asyncio
    async def test_non_numeric_retry_after(
        self, client: KalshiClient
    ) -> None:
        """Non-numeric Retry-After falls back to 1s delay."""
        with (
            patch.object(
                client._client, "request",
                new_callable=AsyncMock,
                side_effect=[
                    _error_response(
                        429, {"Retry-After": "Thu, 01 Jan 2099 00:00:00 GMT"}
                    ),
                    _json_response(),
                ],
            ),
            patch(
                "gimmes.kalshi.client.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            await client.get("/test")
            # Should have used fallback delay (1.0 * 2^0 = 1.0)
            mock_sleep.assert_called()


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_consumes_token(self) -> None:
        rl = RateLimiter(reads_per_sec=2, writes_per_sec=1)
        await rl.acquire(is_write=False)
        assert rl._read_tokens < 2

    @pytest.mark.asyncio
    async def test_write_uses_write_tokens(self) -> None:
        rl = RateLimiter(reads_per_sec=20, writes_per_sec=2)
        await rl.acquire(is_write=True)
        assert rl._write_tokens < 2
