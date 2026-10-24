"""WebSocket ``/research/ws`` for the app (SPEC §4.3, §4.3.1; exact messages in §13 "MR2b notes").

Lives on main.py's aiohttp app (port 8867) and therefore behind its
``api_auth`` middleware: the token is accepted ONLY in the ``X-UFOS-Token``
header of the upgrade request (no ``?token=``); a request without it gets 401
before this handler runs.

``ResearchWsSession`` is a hub ``Subscriber`` (kind ``view``) and is
independent of aiohttp (an adapter with ``receive()`` / ``send_str()`` /
``close()``), so it is tested without aiohttp.

- First client message ``hello`` within ``WS_HELLO_TIMEOUT_SECONDS`` else close 1008;
  malformed / unknown / oversized client messages → ``error bad_message``,
  ``WS_BAD_MESSAGE_LIMIT`` in a row → close 1008.
- Send queue ≤ ``WS_SEND_QUEUE_MAX``; candles, trades, symbol_status and
  feeds are coalesced by key (newest wins); a message that cannot be queued
  closes the connection 1013 (the client reconnects) — never a silent drop.
- One frame ≤ ``WS_MAX_SERVER_MESSAGE_BYTES``; a larger one is a server bug:
  logged, not sent, ``error message_too_large`` instead.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Optional

from strategy_platform.api.routes import dumps

from .. import limits
from .aio import within
from .demand import Subscriber, parse_topic

logger = logging.getLogger(__name__)

WS_PATH = "/research/ws"
CLOSE_NORMAL, CLOSE_POLICY, CLOSE_TOO_BIG, CLOSE_INTERNAL, CLOSE_OVERLOADED = 1000, 1008, 1009, 1011, 1013
OPS = {"hello": {"op", "visible"}, "visible": {"op", "visible"}, "subscribe": {"op", "topics"},
       "unsubscribe": {"op", "topics"}}


class _Closed(Exception):
    pass


class ResearchWsSession(Subscriber):
    def __init__(self, hub, ws, *, hello_timeout: float = limits.WS_HELLO_TIMEOUT_SECONDS,
                 queue_max: int = limits.WS_SEND_QUEUE_MAX):
        super().__init__("view")
        self.hub, self.ws = hub, ws
        self.hello_timeout, self.queue_max = hello_timeout, queue_max
        self._queue: deque = deque()          # entries: [key, msg]
        self._index = {}                      # key -> entry
        self._wake = asyncio.Event()
        self._closing_evt = asyncio.Event()
        self._closing: Optional[tuple] = None
        self._bad = 0
        self.helloed = False
        self.sent = 0

    # --- outgoing ---------------------------------------------------------------------------
    def deliver(self, msg: dict, key: Optional[str] = None) -> None:
        if self._closing is not None:
            return
        if key is not None and key in self._index:
            self._index[key][1] = msg         # coalesce: newest wins, position kept
            return
        if len(self._queue) >= self.queue_max:
            logger.warning("Research WebSocket: send queue full (%d) — closing 1013", self.queue_max)
            self._close_soon(CLOSE_OVERLOADED, "send queue full")
            return
        entry = [key, msg]
        self._queue.append(entry)
        if key is not None:
            self._index[key] = entry
        self._wake.set()

    def _close_soon(self, code: int, reason: str) -> None:
        if self._closing is None:
            self._closing = (code, reason)
            self._wake.set()
            self._closing_evt.set()

    def _error(self, code: str, message: str) -> None:
        self.deliver({"type": "error", "code": code, "message": limits.clip(message)})

    def _bad_message(self, message: str) -> None:
        self._bad += 1
        self._error("bad_message", message)
        if self._bad >= limits.WS_BAD_MESSAGE_LIMIT:
            self._close_soon(CLOSE_POLICY, "repeated bad messages")

    async def _writer(self) -> None:
        while True:
            if not self._queue:
                if self._closing is not None:
                    return
                await self._wake.wait()
                self._wake.clear()
                continue
            if self._closing is not None and self._closing[0] != CLOSE_POLICY:
                return                        # 1013/1011: stop sending at once
            key, msg = self._queue.popleft()
            if key is not None:
                self._index.pop(key, None)
            text = encode(msg)
            await self.ws.send_str(text)
            self.sent += 1

    # --- incoming ---------------------------------------------------------------------------
    def _handle(self, kind: str, data) -> None:
        if kind != "text":
            self._bad_message("only JSON text frames are accepted")
            return
        if not isinstance(data, str) or len(data.encode("utf-8")) > limits.WS_MAX_MESSAGE_BYTES:
            self._bad_message(f"message larger than {limits.WS_MAX_MESSAGE_BYTES} bytes")
            return
        try:
            msg = json.loads(data)
        except ValueError:
            self._bad_message("message is not valid JSON")
            return
        if not isinstance(msg, dict) or msg.get("op") not in OPS:
            self._bad_message("unknown op (hello, visible, subscribe, unsubscribe)")
            return
        op = msg["op"]
        if set(msg) != OPS[op]:
            self._bad_message(f"{op}: fields must be exactly {', '.join(sorted(OPS[op]))}")
            return
        if op != "hello" and not self.helloed:
            self._bad_message("hello must be the first message")
            return
        if op in ("hello", "visible"):
            if not isinstance(msg["visible"], bool):
                self._bad_message(f"{op}: visible must be true or false")
                return
            if op == "hello" and self.helloed:
                self._bad_message("hello was already sent")
                return
            self._bad = 0
            self.active = msg["visible"]
            if op == "hello":
                self.helloed = True
                self.hub.register(self)
                first = self.hub.feeds_message()
                if first is not None:
                    self.deliver(first, "feeds")
            else:
                self.hub.refresh()
            return
        topics = msg["topics"]
        if not isinstance(topics, list) or not topics or len(topics) > limits.WS_MAX_SUBSCRIPTIONS:
            self._bad_message(f"{op}: topics must be a list of 1–{limits.WS_MAX_SUBSCRIPTIONS} strings")
            return
        parsed = []
        for t in topics:
            try:
                parsed.append(parse_topic(t))
            except ValueError as ex:
                self._bad_message(f"{op}: invalid topic {str(t)[:70]!r}: {ex}")
                return
        self._bad = 0
        if op == "subscribe":
            new = [p for p in parsed if p.raw not in self.topics]
            if len(self.topics) + len({p.raw for p in new}) > limits.WS_MAX_SUBSCRIPTIONS:
                self._error("subscription_limit",
                            f"At most {limits.WS_MAX_SUBSCRIPTIONS} subscriptions per connection — nothing "
                            f"was subscribed.")
                return
            for p in new:
                self.topics[p.raw] = p
        else:
            for p in parsed:
                self.topics.pop(p.raw, None)
        self.hub.refresh()
        if op == "subscribe" and any(p.raw == "feeds" for p in new):
            first = self.hub.feeds_message()
            if first is not None:
                self.deliver(first, "feeds")

    async def _reader(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.hello_timeout
        while True:
            if not self.helloed:
                left = deadline - loop.time()
                if left <= 0:
                    self._close_soon(CLOSE_POLICY, "hello expected")
                    return
                try:
                    kind, data = await within(self.ws.receive(), left)
                except asyncio.TimeoutError:
                    self._close_soon(CLOSE_POLICY, "hello expected")
                    return
            else:
                kind, data = await self.ws.receive()
            if kind in ("close", "closed", "error"):
                return
            self._handle(kind, data)
            if self._closing is not None:
                return

    async def run(self) -> None:
        """Runs until the client closes, a policy/overload close, or an error. Never raises (except cancel)."""
        writer = asyncio.ensure_future(self._writer())
        reader = asyncio.ensure_future(self._reader())
        closing = asyncio.ensure_future(self._closing_evt.wait())
        try:
            await asyncio.wait({reader, writer, closing}, return_when=asyncio.FIRST_COMPLETED)
            if self._closing is not None and self._closing[0] == CLOSE_POLICY and not writer.done():
                # let the error message(s) out before the policy close (1013/1011 close at once)
                try:
                    await within(asyncio.shield(writer), 1.0)
                except (asyncio.TimeoutError, Exception):   # noqa: BLE001
                    pass
            for t in (reader, writer):
                if t.done() and not t.cancelled() and t.exception() is not None:
                    ex = t.exception()
                    if not isinstance(ex, (ConnectionError, _Closed)):
                        logger.error("Research WebSocket session failed: %r", ex)
                        self._close_soon(CLOSE_INTERNAL, "internal error")
        finally:
            for t in (reader, writer, closing):
                t.cancel()
            await asyncio.gather(reader, writer, closing, return_exceptions=True)
            self._closing = self._closing or (CLOSE_NORMAL, "")
            try:
                self.hub.unregister(self)
            except Exception:   # noqa: BLE001
                logger.exception("Research WebSocket: unregister failed")
            try:
                await self.ws.close(self._closing[0], self._closing[1])
            except Exception:   # noqa: BLE001 — already closed
                pass


def encode(msg: dict) -> str:
    """Strict JSON; a frame above WS_MAX_SERVER_MESSAGE_BYTES becomes ``error message_too_large`` (§4.3)."""
    text = dumps(msg)
    if len(text.encode("utf-8")) > limits.WS_MAX_SERVER_MESSAGE_BYTES:
        logger.error("Research WebSocket: %s message of %d bytes exceeds the frame cap — not sent",
                     msg.get("type"), len(text.encode("utf-8")))
        return dumps({"type": "error", "code": "message_too_large",
                      "message": f"A {str(msg.get('type'))[:20]} message was too large to send (server bug — "
                                 f"see backend.log)."})
    return text


class AiohttpAdapter:
    """aiohttp WebSocketResponse -> the session's adapter interface."""

    def __init__(self, ws):
        self._ws = ws

    async def receive(self):
        from aiohttp import WSMsgType
        msg = await self._ws.receive()
        if msg.type == WSMsgType.TEXT:
            return "text", msg.data
        if msg.type == WSMsgType.BINARY:
            return "binary", None
        if msg.type == WSMsgType.ERROR:
            return "error", None
        return "close", None

    async def send_str(self, text: str) -> None:
        await self._ws.send_str(text)

    async def close(self, code: int, message: str = "") -> None:
        if not self._ws.closed:
            await self._ws.close(code=code, message=message.encode("utf-8")[:120])


def make_handler(service):
    async def research_ws(request):
        from aiohttp import web
        hub = service.hub
        # aiohttp caps frames at 4× the client limit (memory guard, close 1009); the session itself
        # answers bad_message above WS_MAX_MESSAGE_BYTES
        ws = web.WebSocketResponse(heartbeat=limits.WS_HEARTBEAT_SECONDS,
                                   max_msg_size=4 * limits.WS_MAX_MESSAGE_BYTES)
        await ws.prepare(request)
        if hub.ws_clients >= limits.WS_MAX_CLIENTS:
            await ws.close(code=CLOSE_OVERLOADED, message=b"too many Research connections")
            return ws
        hub.ws_clients += 1
        try:
            await ResearchWsSession(hub, AiohttpAdapter(ws)).run()
        except asyncio.CancelledError:
            raise
        except Exception:   # noqa: BLE001 — never into aiohttp / the backend
            logger.exception("Research WebSocket handler failed")
        finally:
            hub.ws_clients -= 1
        return ws

    research_ws.__name__ = "research_ws"
    return research_ws


def register_ws(app, service) -> None:
    """Adds GET /research/ws (behind main.py's api_auth middleware: header token only)."""
    app.router.add_route("GET", WS_PATH, make_handler(service))
