"""WebSocket broadcast server — same pattern/API as LiveStreamAgent/backend/broadcast.py.

Frontend (globe.html) connects to ws://127.0.0.1:<WS_PORT> and receives JSON
messages of the form {"type": "<type>", "payload": {...}}. Message types used
in this module:

- "snapshot"       payload: list[event]   — sent once, right after a client connects
- "event"          payload: event         — one new/updated event from a collector
- "source_status"  payload: {source, ok, message, timestamp} — collector health,
                    drives the "Ampel pro Feed" traffic-light list in the left sidebar
"""
import asyncio
import json
import logging
from typing import Set

import websockets
from websockets.legacy.server import WebSocketServerProtocol, WebSocketServer

from config import WS_HOST, WS_PORT

logger = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self) -> None:
        self._clients: Set[WebSocketServerProtocol] = set()
        self._server: WebSocketServer | None = None
        self._lock = asyncio.Lock()
        self._ping_interval = 20
        self._ping_timeout = 20
        self.on_connect = None  # optional async callback(ws) -> sends initial state

    async def _register(self, ws: WebSocketServerProtocol) -> None:
        async with self._lock:
            self._clients.add(ws)
        logger.info("WS client connected: %s", ws.remote_address)
        if self.on_connect:
            try:
                await self.on_connect(ws)
            except Exception:
                logger.exception("on_connect handler failed")

    async def _unregister(self, ws: WebSocketServerProtocol) -> None:
        async with self._lock:
            self._clients.discard(ws)
        logger.info("WS client disconnected: %s", ws.remote_address)

    async def _handler(self, websocket: WebSocketServerProtocol) -> None:
        await self._register(websocket)
        try:
            async for _ in websocket:
                pass
        except websockets.ConnectionClosedOK:
            logger.debug("Connection closed cleanly: %s", websocket.remote_address)
        except Exception as ex:
            logger.exception("WebSocket handler error: %s", ex)
        finally:
            await self._unregister(websocket)

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handler, WS_HOST, WS_PORT,
            ping_interval=self._ping_interval, ping_timeout=self._ping_timeout,
            max_size=2 ** 20,
        )
        logger.info("WebSocket broadcast server listening on ws://%s:%s", WS_HOST, WS_PORT)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        async with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        if clients:
            await asyncio.gather(*(c.close(code=1001, reason="Server shutting down") for c in clients), return_exceptions=True)

    async def send_to(self, ws: WebSocketServerProtocol, event_type: str, payload) -> None:
        try:
            await ws.send(json.dumps({"type": event_type, "payload": payload}))
        except Exception:
            logger.debug("send_to failed for %s", ws.remote_address)

    async def broadcast(self, event_type: str, payload) -> None:
        async with self._lock:
            if not self._clients:
                return
            clients = list(self._clients)
        message = json.dumps({"type": event_type, "payload": payload})
        results = await asyncio.gather(*(self._safe_send(c, message) for c in clients), return_exceptions=True)
        for client, res in zip(clients, results):
            if isinstance(res, Exception):
                logger.warning("Removing WS client due to send error: %s (%s)", client.remote_address, res)
                async with self._lock:
                    self._clients.discard(client)

    async def _safe_send(self, client: WebSocketServerProtocol, message: str):
        try:
            await client.send(message)
        except Exception as ex:
            return ex
