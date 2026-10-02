"""Robust WebSocket broadcast server.

This implementation replaces the previous minimal server with a clearer structure:
- supports per-connection lifecycle handling and graceful removal on errors
- uses periodic pings and configurable timeouts
- preserves the same public API: Broadcaster.start/stop/broadcast

Frontend expectations:
- listens on WS_HOST:WS_PORT (from config)
- receives JSON messages of form {"type": "<type>", "payload": { ... }}
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable, Optional, Set

import websockets
from websockets.legacy.server import WebSocketServerProtocol, WebSocketServer

from config import WS_HOST, WS_PORT, DEFAULT_ALLOWED_ORIGINS
import api_auth

logger = logging.getLogger(__name__)


class Broadcaster:
    """Manage connected websocket clients and fan out messages to them.

    Usage:
        b = Broadcaster()
        await b.start()
        await b.broadcast('market_update', payload)
        await b.stop()
    """

    def __init__(self) -> None:
        self._clients: Set[WebSocketServerProtocol] = set()
        self._server: WebSocketServer | None = None
        self._lock = asyncio.Lock()

        # Optional warm-start hook (2026-09-25): awaited for every new client; returns a
        # list of (event_type, payload) sent to that client only, e.g. the stored verdict history.
        self.on_connect: Optional[Callable[[], Awaitable[list]]] = None

        # tuning: allow large messages, send periodic pings
        self._ping_interval = 20
        self._ping_timeout = 20

    async def _register(self, ws: WebSocketServerProtocol) -> None:
        async with self._lock:
            self._clients.add(ws)
        logger.info("WS client connected: %s", ws.remote_address)

    async def _unregister(self, ws: WebSocketServerProtocol) -> None:
        async with self._lock:
            self._clients.discard(ws)
        logger.info("WS client disconnected: %s", ws.remote_address)

    async def _handler(self, websocket: WebSocketServerProtocol) -> None:
        # register client
        await self._register(websocket)
        await self._send_warm_start(websocket)
        try:
            # We don't expect inbound messages; keep the connection open and react to close
            async for _ in websocket:
                # ignore inbound messages; could be used for ping/pong or control
                pass
        except websockets.ConnectionClosedOK:
            logger.debug("Connection closed cleanly: %s", websocket.remote_address)
        except Exception as ex:
            logger.exception("WebSocket handler error: %s", ex)
        finally:
            await self._unregister(websocket)

    async def _send_warm_start(self, websocket: WebSocketServerProtocol) -> None:
        """Send the on_connect messages to one new client. Never raises."""
        if self.on_connect is None:
            return
        try:
            for event_type, payload in (await self.on_connect()) or []:
                await websocket.send(json.dumps({"type": event_type, "payload": payload}, default=str))
        except Exception as ex:
            logger.warning("WS warm start for %s failed: %s", websocket.remote_address, ex)

    async def start(self) -> None:
        # start the websockets server; allow large messages in case frontend sends bigger data
        self._server = await websockets.serve(
            self._handler,
            api_auth.bind_host(WS_HOST, "WebSocket server"),
            WS_PORT,
            # Session token + origin check before the handshake (api_auth).
            process_request=api_auth.ws_process_request(api_auth.allowed_origins(DEFAULT_ALLOWED_ORIGINS)),
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            max_size=2 ** 20,
        )
        logger.info("WebSocket broadcast server listening on port %s (token required)", WS_PORT)

    async def stop(self) -> None:
        # close server and all connected clients
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

        # close clients
        async with self._lock:
            clients = list(self._clients)
            self._clients.clear()

        if clients:
            await asyncio.gather(*(c.close(code=1001, reason="Server shutting down") for c in clients), return_exceptions=True)

    async def broadcast(self, event_type: str, payload: dict) -> None:
        """Broadcast a typed JSON event to all connected clients.

        Quietly removes clients that raised exceptions during send.
        """
        async with self._lock:
            if not self._clients:
                return
            clients = list(self._clients)

        message = json.dumps({"type": event_type, "payload": payload})

        # send concurrently and collect results
        results = await asyncio.gather(*(self._safe_send(c, message) for c in clients), return_exceptions=True)

        # remove failed clients
        for client, res in zip(clients, results):
            if isinstance(res, Exception):
                logger.warning("Removing WS client due to send error: %s (%s)", client.remote_address, res)
                async with self._lock:
                    self._clients.discard(client)

    async def _safe_send(self, client: WebSocketServerProtocol, message: str):
        try:
            await client.send(message)
        except Exception as ex:
            # propagate exception for caller to handle cleanup
            return ex
