"""WebSocket client transport for the Alpaca streams (SPEC §7.6).

The ``websockets`` package (already a backend dependency; imported lazily so
the package and its tests load without it). TLS is verified (default
``ssl`` context of ``wss://``); proxies from the environment are honoured by
websockets itself. Every failure is mapped to ``protocol.StreamLost`` (the
hub then waits with backoff); a refused upgrade with HTTP 401/403 is
``StreamRefused`` (keys rejected), 406 the connection-limit message.

Interface used by the hub (tests use a fake with the same methods):

    conn = await transport.connect(url)
    await conn.send(text);  text = await conn.recv(timeout);  ok = await conn.ping(timeout);  await conn.close()

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import asyncio
import logging

from .. import limits
from .aio import within
from .protocol import CONNECTION_LIMIT, StreamLost, refused_error

logger = logging.getLogger(__name__)


class _Conn:
    def __init__(self, ws, websockets_mod):
        self._ws = ws
        self._mod = websockets_mod

    async def send(self, text: str) -> None:
        try:
            await self._ws.send(text)
        except Exception as ex:   # noqa: BLE001 — any send failure = connection lost
            raise StreamLost(f"send failed ({type(ex).__name__})") from None

    async def recv(self, timeout: float):
        try:
            return await within(self._ws.recv(), timeout)
        except asyncio.TimeoutError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as ex:   # noqa: BLE001 — ConnectionClosed and friends
            code = getattr(getattr(ex, "rcvd", None), "code", None)
            raise StreamLost(f"connection closed ({type(ex).__name__}{f' {code}' if code else ''})") from None

    async def ping(self, timeout: float) -> bool:
        try:
            waiter = await self._ws.ping()
            await within(waiter, timeout)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:   # noqa: BLE001 — no pong = dead
            return False

    async def close(self) -> None:
        try:
            await within(self._ws.close(), 2.0)
        except asyncio.CancelledError:
            raise
        except Exception:   # noqa: BLE001 — closing a dead socket
            pass


class WebsocketsTransport:
    async def connect(self, url: str):
        import websockets   # lazy: not installed on the offline test VM
        from websockets.asyncio.client import connect
        try:
            ws = await connect(url, open_timeout=limits.STREAM_CONNECT_TIMEOUT_SECONDS, close_timeout=2,
                               max_size=limits.STREAM_MAX_FRAME_BYTES, ping_interval=20, ping_timeout=20,
                               user_agent_header="UFOS.ai-HedgeFund-Research")
        except asyncio.CancelledError:
            raise
        except Exception as ex:   # noqa: BLE001 — handshake / network / TLS
            status = getattr(getattr(ex, "response", None), "status_code", None)
            if status in (401, 403):
                raise refused_error(401) from None
            if status == 406:
                raise refused_error(CONNECTION_LIMIT) from None
            raise StreamLost(f"connect failed ({type(ex).__name__}{f' HTTP {status}' if status else ''})") from None
        return _Conn(ws, websockets)
