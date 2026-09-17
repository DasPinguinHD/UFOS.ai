"""Local WebSocket server that fans out live events to connected frontend clients."""
import asyncio
import json
import logging

import websockets
from websockets.server import WebSocketServerProtocol

from config import WS_HOST, WS_PORT

logger = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self) -> None:
        self._clients: set[WebSocketServerProtocol] = set()
        self._server: websockets.WebSocketServer | None = None

    async def _handler(self, websocket: WebSocketServerProtocol) -> None:
        self._clients.add(websocket)
        try:
            async for _ in websocket:
                pass  # this server is publish-only; inbound messages are ignored
        finally:
            self._clients.discard(websocket)

    async def start(self) -> None:
        self._server = await websockets.serve(self._handler, WS_HOST, WS_PORT)
        logger.info("WebSocket broadcast server listening on ws://%s:%s", WS_HOST, WS_PORT)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def broadcast(self, event_type: str, payload: dict) -> None:
        if not self._clients:
            return
        message = json.dumps({"type": event_type, "payload": payload})
        results = await asyncio.gather(
            *(client.send(message) for client in list(self._clients)),
            return_exceptions=True,
        )
        for client, result in zip(list(self._clients), results):
            if isinstance(result, Exception):
                self._clients.discard(client)
