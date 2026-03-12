"""Async Kalshi API client with rate limiting and auth."""

from __future__ import annotations

import asyncio
import time
from types import TracebackType

import httpx

from gimmes.config import GimmesConfig
from gimmes.kalshi.auth import auth_headers, load_private_key


class RateLimiter:
    """Token-bucket rate limiter for Kalshi API tiers."""

    def __init__(self, reads_per_sec: int = 20, writes_per_sec: int = 10) -> None:
        self._read_tokens = float(reads_per_sec)
        self._write_tokens = float(writes_per_sec)
        self._max_read = reads_per_sec
        self._max_write = writes_per_sec
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, is_write: bool = False) -> None:
        async with self._lock:
            self._refill()
            if is_write:
                while self._write_tokens < 1:
                    await asyncio.sleep(0.05)
                    self._refill()
                self._write_tokens -= 1
            else:
                while self._read_tokens < 1:
                    await asyncio.sleep(0.05)
                    self._refill()
                self._read_tokens -= 1

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._read_tokens = min(self._max_read, self._read_tokens + elapsed * self._max_read)
        self._write_tokens = min(self._max_write, self._write_tokens + elapsed * self._max_write)
        self._last_refill = now


class KalshiClient:
    """Async HTTP client for the Kalshi REST API."""

    def __init__(self, config: GimmesConfig) -> None:
        self.config = config
        self._api_key = config.api_key
        self._private_key = (
            load_private_key(config.private_key_path)
            if config.private_key_path.exists()
            else None
        )
        self._rate_limiter = RateLimiter()
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _get_auth_headers(self, method: str, path: str) -> dict[str, str]:
        if self._private_key is None:
            raise RuntimeError(
                "Private key not loaded. Check KALSHI_*_PRIVATE_KEY_PATH in .env"
            )
        return auth_headers(self._api_key, self._private_key, method, path)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,  # type: ignore[type-arg]
        json: dict | None = None,  # type: ignore[type-arg]
        max_retries: int = 3,
    ) -> dict:  # type: ignore[type-arg]
        """Make an authenticated request with rate limiting and retry on 429."""
        is_write = method.upper() in ("POST", "PUT", "DELETE", "PATCH")
        await self._rate_limiter.acquire(is_write=is_write)

        headers = self._get_auth_headers(method.upper(), path)

        for attempt in range(max_retries):
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                headers=headers,
            )

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "1"))
                await asyncio.sleep(retry_after * (attempt + 1))
                # Re-sign with fresh timestamp
                headers = self._get_auth_headers(method.upper(), path)
                continue

            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]

        raise httpx.HTTPStatusError(
            "Rate limited after max retries",
            request=response.request,  # type: ignore[possibly-undefined]
            response=response,  # type: ignore[possibly-undefined]
        )

    async def get(self, path: str, params: dict | None = None) -> dict:  # type: ignore[type-arg]
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: dict | None = None) -> dict:  # type: ignore[type-arg]
        return await self._request("POST", path, json=json)

    async def put(self, path: str, json: dict | None = None) -> dict:  # type: ignore[type-arg]
        return await self._request("PUT", path, json=json)

    async def delete(self, path: str) -> dict:  # type: ignore[type-arg]
        return await self._request("DELETE", path)
