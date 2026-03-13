"""Authenticated WebSocket client for Kalshi real-time data.

Channels:
- orderbook_delta: Real-time orderbook updates
- ticker: Price/volume updates
- fill: Fill notifications for your orders
- trade: Public trade feed
- market_lifecycle: Market status changes
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from gimmes.config import GimmesConfig
from gimmes.kalshi.auth import auth_headers, load_private_key

logger = logging.getLogger(__name__)

# Kalshi WebSocket channels
CHANNELS = [
    "orderbook_delta",
    "ticker",
    "trade",
    "fill",
    "market_lifecycle",
    "user_orders",
    "user_fills",
]


class KalshiWebSocket:
    """Authenticated WebSocket client for Kalshi real-time data."""

    def __init__(self, config: GimmesConfig) -> None:
        self.config = config
        self._ws_url = config.ws_url
        self._api_key = config.api_key
        self._private_key = (
            load_private_key(config.private_key_path)
            if config.private_key_path.exists()
            else None
        )
        self._connection: ClientConnection | None = None
        self._running = False
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscriptions: set[str] = set()

    async def connect(self) -> None:
        """Establish authenticated WebSocket connection."""
        if self._private_key is None:
            raise RuntimeError("Private key not loaded. Check credentials in .env")

        # Auth headers for WebSocket handshake
        headers = auth_headers(
            self._api_key, self._private_key, "GET", "/trade-api/ws/v2"
        )

        self._connection = await websockets.connect(
            self._ws_url,
            additional_headers=headers,
        )
        self._running = True
        logger.info("WebSocket connected to %s", self._ws_url)

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._running = False
        if self._connection:
            await self._connection.close()
            self._connection = None
        logger.info("WebSocket disconnected")

    async def __aenter__(self) -> KalshiWebSocket:
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def subscribe(
        self, channels: list[str], tickers: list[str] | None = None
    ) -> None:
        """Subscribe to one or more channels.

        Args:
            channels: List of channel names (e.g., ["orderbook_delta", "ticker"]).
            tickers: Optional list of market tickers to filter on.
        """
        if not self._connection:
            raise RuntimeError("Not connected. Call connect() first.")

        cmd: dict[str, Any] = {
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": channels,
            },
        }
        if tickers:
            cmd["params"]["market_tickers"] = tickers

        await self._connection.send(json.dumps(cmd))
        self._subscriptions.update(channels)
        logger.info("Subscribed to channels: %s", channels)

    async def unsubscribe(self, channels: list[str]) -> None:
        """Unsubscribe from channels."""
        if not self._connection:
            return

        cmd: dict[str, Any] = {
            "id": 2,
            "cmd": "unsubscribe",
            "params": {
                "channels": channels,
            },
        }
        await self._connection.send(json.dumps(cmd))
        self._subscriptions -= set(channels)
        logger.info("Unsubscribed from channels: %s", channels)

    async def _recv_loop(self) -> None:
        """Internal loop that reads messages and puts them on the queue."""
        if not self._connection:
            return

        try:
            async for raw_message in self._connection:
                if not self._running:
                    break
                try:
                    message = json.loads(raw_message)
                    await self._message_queue.put(message)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from WebSocket: %s", raw_message[:100])
        except websockets.ConnectionClosed as e:
            logger.info("WebSocket connection closed: %s", e)
        except Exception:
            logger.exception("WebSocket receive error")
        finally:
            self._running = False

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Async iterator that yields parsed WebSocket messages.

        Usage:
            async with KalshiWebSocket(config) as ws:
                await ws.subscribe(["ticker"], ["KXCPI-26MAR-T3.2"])
                async for msg in ws.messages():
                    print(msg)
        """
        # Start the receive loop as a background task
        recv_task = asyncio.create_task(self._recv_loop())

        try:
            while self._running:
                try:
                    message = await asyncio.wait_for(
                        self._message_queue.get(), timeout=1.0
                    )
                    yield message
                except TimeoutError:
                    continue
        finally:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass

    async def ping(self) -> None:
        """Send a ping to keep the connection alive."""
        if self._connection:
            await self._connection.ping()
