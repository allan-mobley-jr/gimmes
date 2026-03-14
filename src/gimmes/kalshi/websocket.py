"""Authenticated WebSocket client for Kalshi real-time data.

Channels:
- orderbook_delta: Real-time orderbook updates
- ticker: Price/volume updates
- fill: Fill notifications for your orders
- trade: Public trade feed
- market_lifecycle_v2: Market status changes
"""

from __future__ import annotations

import asyncio
import itertools
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
    "market_lifecycle_v2",
    "user_orders",
]

# Reconnection settings
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
_BACKOFF_FACTOR = 2.0
_MAX_RECONNECT_ATTEMPTS = 10


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
        self._subscription_tickers: dict[str, list[str] | None] = {}
        self._sid_map: dict[str, list[int]] = {}  # channel -> [subscription IDs]
        self._id_counter = itertools.count(1)
        self._last_seq: int | None = None

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
        self._running = True
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

        msg_id = next(self._id_counter)
        cmd: dict[str, Any] = {
            "id": msg_id,
            "cmd": "subscribe",
            "params": {
                "channels": channels,
            },
        }
        if tickers:
            cmd["params"]["market_tickers"] = tickers

        await self._connection.send(json.dumps(cmd))
        self._subscriptions.update(channels)
        for ch in channels:
            existing = self._subscription_tickers.get(ch)
            if existing is not None and tickers is not None:
                self._subscription_tickers[ch] = list(set(existing + tickers))
            else:
                self._subscription_tickers[ch] = tickers
        logger.info("Subscribed to channels: %s (msg_id=%d)", channels, msg_id)

    async def unsubscribe(self, channels: list[str]) -> None:
        """Unsubscribe from channels using subscription IDs."""
        if not self._connection:
            return

        sids: list[int] = []
        for ch in channels:
            sids.extend(self._sid_map.pop(ch, []))

        if sids:
            msg_id = next(self._id_counter)
            cmd: dict[str, Any] = {
                "id": msg_id,
                "cmd": "unsubscribe",
                "params": {
                    "sids": sids,
                },
            }
            await self._connection.send(json.dumps(cmd))
            logger.info(
                "Unsubscribed sids=%s for channels=%s (msg_id=%d)",
                sids,
                channels,
                msg_id,
            )
        else:
            logger.warning(
                "No subscription IDs found for channels: %s — sending channel-based unsubscribe",
                channels,
            )
            msg_id = next(self._id_counter)
            cmd = {
                "id": msg_id,
                "cmd": "unsubscribe",
                "params": {
                    "channels": channels,
                },
            }
            await self._connection.send(json.dumps(cmd))

        self._subscriptions -= set(channels)
        for ch in channels:
            self._subscription_tickers.pop(ch, None)
        logger.info("Unsubscribed from channels: %s", channels)

    def _process_message(self, message: dict[str, Any]) -> None:
        """Process control messages (subscription confirmations, seq tracking)."""
        # Track subscription IDs from subscribe confirmations
        if message.get("type") == "subscribed":
            sid = message.get("sid")
            channel = message.get("channel")
            if sid is not None and channel:
                self._sid_map.setdefault(channel, []).append(sid)
                logger.debug("Stored sid=%d for channel=%s", sid, channel)

        # Track sequence numbers for gap detection
        seq = message.get("seq")
        if seq is not None:
            if self._last_seq is not None and seq != self._last_seq + 1:
                logger.warning(
                    "Sequence gap detected: expected %d, got %d",
                    self._last_seq + 1,
                    seq,
                )
            self._last_seq = seq

    async def _resubscribe(self) -> None:
        """Re-subscribe to all tracked channels after reconnection."""
        channels_to_restore = list(self._subscriptions)
        self._subscriptions.clear()
        self._sid_map.clear()
        self._last_seq = None

        # Group by tickers to minimize subscribe calls
        ticker_groups: dict[tuple[str, ...] | None, list[str]] = {}
        for ch in channels_to_restore:
            tickers = self._subscription_tickers.get(ch)
            key = tuple(tickers) if tickers else None
            ticker_groups.setdefault(key, []).append(ch)

        for ticker_key, channels in ticker_groups.items():
            tickers = list(ticker_key) if ticker_key else None
            await self.subscribe(channels, tickers)
            logger.info("Re-subscribed to channels: %s", channels)

    async def _recv_loop(self) -> None:
        """Internal loop that reads messages with automatic reconnection."""
        backoff = _INITIAL_BACKOFF
        attempts = 0

        while self._running:
            if not self._connection:
                break

            try:
                async for raw_message in self._connection:
                    if not self._running:
                        return
                    try:
                        message = json.loads(raw_message)
                        self._process_message(message)
                        await self._message_queue.put(message)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Invalid JSON from WebSocket: %s", raw_message[:100]
                        )
                # Connection closed gracefully (iterator exhausted)
                if not self._running:
                    return
            except websockets.ConnectionClosed as e:
                logger.info("WebSocket connection closed: %s", e)
            except Exception:
                logger.exception("WebSocket receive error")

            # Attempt reconnection
            if not self._running:
                return

            attempts += 1
            if attempts > _MAX_RECONNECT_ATTEMPTS:
                logger.error(
                    "Max reconnection attempts (%d) reached, giving up",
                    _MAX_RECONNECT_ATTEMPTS,
                )
                self._running = False
                return

            logger.info(
                "Reconnecting in %.1fs (attempt %d/%d)...",
                backoff,
                attempts,
                _MAX_RECONNECT_ATTEMPTS,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * _BACKOFF_FACTOR, _MAX_BACKOFF)

            try:
                self._connection = None
                await self.connect()
                if not self._running:
                    return  # close() was called during reconnection
                await self._resubscribe()
                # Reset backoff on successful reconnection
                backoff = _INITIAL_BACKOFF
                attempts = 0
                logger.info("Reconnected and re-subscribed successfully")
            except Exception:
                logger.exception("Reconnection failed")

        self._running = False

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Async iterator that yields parsed WebSocket messages.

        Usage:
            async with KalshiWebSocket(config) as ws:
                await ws.subscribe(["ticker"], ["KXCPI-26MAR-T3.2"])
                async for msg in ws.messages():
                    print(msg)
        """
        # Ensure _running is set (in case connect() was called directly)
        self._running = True
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
