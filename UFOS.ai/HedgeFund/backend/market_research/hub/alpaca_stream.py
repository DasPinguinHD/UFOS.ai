"""One Alpaca stream connection per endpoint (stocks IEX, crypto, news) — SPEC §7.6.

``StreamConnection.run()`` runs for the hub's lifetime (inside the hub's
supervised task ``market_data_hub``) and drives the feed state of §1.2:

    off ──(demand + symbols)──▶ connecting ──auth ok──▶ live
     ▲                              │  │                 │ lost / silent 90 s + no pong
     └──(no demand: linger 60 s)────┘  │                 ▼
                                       └──lost──▶ waiting (attempt n, backoff 2/5/10/30/60 s;
                                                  from attempt 2: network.probe("alpaca") first)
    406 / keys rejected / plan refused / keys missing ──▶ unavailable (re-check 300 s / 60 s,
                                                          and once at every demand change)

Keys are read at every connect (Credential Manager, Research pool thread),
sent only in the auth message, never logged. The connection object is
closed whenever the loop leaves a session (also on cancel).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

from strategy_platform.core.errors import SecretMissing, UserFacingError

from .. import limits
from ..executor import run_blocking
from . import protocol
from .aio import within
from .protocol import StreamLost, StreamRefused

logger = logging.getLogger(__name__)

LABELS = {"stocks": ("Stocks", "IEX · real time", limits.STOCK_STREAM_SYMBOL_LIMIT),
          "crypto": ("Crypto", "Alpaca crypto · real time", None),
          "news": ("News", "Alpaca · Benzinga", None)}


@dataclass(frozen=True)
class HubConfig:
    backoff: tuple = limits.RECONNECT_BACKOFF_SECONDS
    linger: float = limits.HUB_LINGER_SECONDS
    silence: float = limits.STREAM_SILENCE_SECONDS
    ping_timeout: float = limits.STREAM_PING_TIMEOUT_SECONDS
    handshake_timeout: float = limits.STREAM_HANDSHAKE_TIMEOUT_SECONDS
    stable: float = limits.HUB_STABLE_SECONDS
    recheck_missing_keys: float = limits.RECHECK_MISSING_KEYS_SECONDS
    recheck_refused: float = limits.RECHECK_REFUSED_SECONDS
    tick: float = 0.05                       # hub flush loop (throttled candles / trades, bucket closes)
    feeds_interval: float = 1.0 / limits.FEEDS_MAX_PER_SECOND
    sip_refresh: float = limits.SIP_TAIL_REFRESH_SECONDS
    seed_retry: float = limits.SEED_RETRY_SECONDS


def default_keys():
    from strategy_platform import secrets
    from strategy_platform.data.providers.alpaca import KEY_ID_SECRET, SECRET_KEY_SECRET
    return secrets.require_secret(KEY_ID_SECRET), secrets.require_secret(SECRET_KEY_SECRET)


class StreamConnection:
    def __init__(self, endpoint: str, hub, *, transport, keys: Callable[[], tuple],
                 probe: Callable[[], Awaitable[bool]], next_probe_in: Callable[[], float],
                 config: HubConfig, monotonic: Callable[[], float], now):
        if endpoint not in protocol.ENDPOINT_IDS:
            raise ValueError("endpoint")
        self.endpoint, self.url = endpoint, protocol.ENDPOINTS[endpoint]
        self.label, self.feed_label, self.symbol_limit = LABELS[endpoint]
        self._hub, self._transport, self._keys = hub, transport, keys
        self._probe, self._next_probe_in = probe, next_probe_in
        self.config, self._mono, self._now = config, monotonic, now
        self.state = "off"
        self.reason: Optional[str] = None          # unavailable: the message; waiting: stable short text
        self.attempt = 0
        self.connected_since = None                 # datetime while live
        self.live_since_mono: Optional[float] = None
        self.last_message_at = None
        self.next_try_mono: Optional[float] = None
        self.desired: List[str] = []                # platform symbols (already limited by the hub)
        self.wanted = False
        self.subscribed: Dict[str, str] = {}        # wire symbol -> platform symbol (confirmed by Alpaca)
        self.pending: Dict[str, str] = {}           # wire symbol -> platform symbol (subscribe sent, no answer yet)
        self.confirmed: set = set()                 # wire symbols in Alpaca's last "subscription" answer
        self.rejected: Dict[str, str] = {}          # platform symbol -> reason (refused: 405 / not confirmed)
        self._requests: deque = deque()             # (action, [wire symbols]) awaiting Alpaca's answer, in order
        self._last_unconfirmed: List[str] = []      # platform symbols of the last answer that omitted them
        self._retry_rejected = False
        self.connects = 0                           # transport.connect calls (tests / diagnostics)
        self._failures = 0
        self._recheck_mono = 0.0
        self._demand_changed = False
        self._subs_dirty = False
        self._wake: Optional[asyncio.Event] = None

    # --- control from the hub ---------------------------------------------------------------
    def update(self, symbols: List[str], wanted: bool, demand_changed: bool = False) -> None:
        symbols = list(symbols)
        if symbols != self.desired:
            self.desired = symbols
            self._subs_dirty = True
        self.wanted = bool(wanted)
        if demand_changed:
            self._demand_changed = True
            if self.rejected:                       # retry refused symbols at every demand change (review L5)
                self._retry_rejected = self._subs_dirty = True
        if self._wake is not None:
            self._wake.set()

    def is_wanted(self) -> bool:
        return self.wanted and bool(self.desired)

    def is_live(self) -> bool:
        return self.state == "live"

    def symbol_reason(self) -> Optional[str]:
        """Why this feed's symbols are not live (short, stable text) — None while live."""
        if self.state == "live":
            return None
        if self.state == "unavailable":
            return self.reason
        if self.state == "waiting":
            return f"{self.label} stream: waiting for connection (attempt {self.attempt})."
        if self.state == "connecting":
            return f"{self.label} stream: connecting."
        return f"{self.label} stream is off."

    def message(self) -> str:
        if self.state == "live":
            n = len(self.desired)
            return f"Live · {n} symbol{'s' if n != 1 else ''}"
        if self.state == "waiting":
            left = 0 if self.next_try_mono is None else max(0, math.ceil(self.next_try_mono - self._mono()))
            tail = f"next try in {left} s" if left > 0 else "checking the connection"
            return f"Waiting for connection (attempt {self.attempt}) — {tail}"
        if self.state == "connecting":
            return f"Connecting to Alpaca ({self.feed_label})…"
        if self.state == "unavailable":
            return limits.clip(self.reason or "unavailable")
        if not self.wanted:
            return "Off — live streams run only while a Research view is visible or an alert is active."
        what = "news subscription" if self.endpoint == "news" else f"{self.label.lower()} symbol"
        return f"Off — no {what} is open."

    # --- state ------------------------------------------------------------------------------
    def _set(self, state: str, reason: Optional[str] = None) -> None:
        changed = (state, reason) != (self.state, self.reason)
        self.state, self.reason = state, reason
        if state != "waiting":
            self.attempt = 0
        if changed:
            self._hub._on_conn_change(self)

    def _unavailable(self, message: str, recheck: float) -> None:
        self._failures = 0
        self._recheck_mono = self._mono() + recheck
        self._demand_changed = False
        logger.warning("Research %s stream unavailable: %s (re-check in %.0f s)", self.endpoint, message, recheck)
        self._set("unavailable", limits.clip(message))

    async def _wait_wake(self, timeout: Optional[float]) -> None:
        try:
            if timeout is None:
                await self._wake.wait()
            else:
                await within(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _sleep_while_wanted(self, delay: float) -> bool:
        end = self._mono() + delay
        while True:
            left = end - self._mono()
            if left <= 0:
                return True
            await self._wait_wake(left)
            if not self.is_wanted():
                return False

    # --- main loop --------------------------------------------------------------------------
    async def run(self) -> None:
        self._wake = asyncio.Event()
        try:
            while True:
                if not self.is_wanted():
                    self._failures = 0
                    self._demand_changed = False
                    self._set("off")
                    await self._wait_wake(None)
                    continue
                if self.state == "unavailable":
                    left = self._recheck_mono - self._mono()
                    if left > 0 and not self._demand_changed:
                        await self._wait_wake(left)
                        continue
                    self._demand_changed = False
                elif self.state == "waiting":
                    if not await self._backoff():
                        continue
                try:
                    await self._session()
                except StreamRefused as ex:
                    self._unavailable(ex.user_message, self.config.recheck_missing_keys if ex.kind == "keys"
                                      else self.config.recheck_refused)
                except StreamLost as ex:
                    logger.info("Research %s stream lost: %s", self.endpoint, ex)
                    self.state = "waiting"          # _backoff() publishes it with the attempt number
        finally:
            self.live_since_mono = self.connected_since = None
            self.state, self.reason, self.attempt = "off", None, 0

    async def _backoff(self) -> bool:
        """One waiting step. True = try to connect now; False = loop again (still waiting / off / unavailable)."""
        self._failures += 1
        delays = tuple(self.config.backoff) or (1.0,)
        delay = float(delays[min(self._failures - 1, len(delays) - 1)])
        self.next_try_mono = self._mono() + delay
        self.attempt = self._failures
        self._set("waiting", f"attempt {self._failures}")
        if not await self._sleep_while_wanted(delay):
            return False
        if self.attempt >= 2:
            wait = float(self._next_probe_in() or 0.0)
            if wait > 0:                            # network.probe spacing (≥ 30 s): wait here, not in a thread
                self.next_try_mono = self._mono() + wait
                if not await self._sleep_while_wanted(wait):
                    return False
            self.next_try_mono = None
            try:
                ok = await self._probe()
            except UserFacingError as ex:
                self._unavailable(ex.user_message, self.config.recheck_refused)
                return False
            except asyncio.CancelledError:
                raise
            except Exception:   # noqa: BLE001 — a probe bug keeps the feed waiting, logged
                logger.exception("Research %s stream: connection probe failed", self.endpoint)
                ok = False
            if not ok:
                logger.info("Research %s stream: Alpaca still unreachable (attempt %d)", self.endpoint, self.attempt)
                return False
        return True

    async def _read_keys(self):
        try:
            return await run_blocking(self._keys)
        except SecretMissing as ex:
            raise StreamRefused(ex.user_message, "keys") from None
        except UserFacingError as ex:          # Credential Manager unreadable
            raise StreamRefused(ex.user_message, "keys") from None

    async def _session(self) -> None:
        self._set("connecting")
        keys = await self._read_keys()
        self.connects += 1
        conn = await self._transport.connect(self.url)
        try:
            await self._expect(conn, "connected")
            await conn.send(protocol.auth_message(*keys))
            keys = None
            await self._expect(conn, "authenticated")
            self._reset_subscriptions()
            self.rejected = {}
            self.connected_since = self._now()
            self.live_since_mono = self._mono()
            self._subs_dirty = True
            self._set("live")
            await self._stream(conn)
        finally:
            keys = None
            if self.live_since_mono is not None and self._mono() - self.live_since_mono >= self.config.stable:
                self._failures = 0
            self.live_since_mono = self.connected_since = None
            self._reset_subscriptions()
            await conn.close()

    def _reset_subscriptions(self) -> None:
        self.subscribed, self.pending, self.confirmed = {}, {}, set()
        self._requests.clear()
        self._last_unconfirmed, self._retry_rejected = [], False

    async def _expect(self, conn, what: str) -> None:
        end = self._mono() + self.config.handshake_timeout
        while True:
            left = end - self._mono()
            if left <= 0:
                raise StreamLost(f"no '{what}' answer within {self.config.handshake_timeout:.0f} s")
            try:
                text = await within(conn.recv(None), left)
            except asyncio.TimeoutError:
                raise StreamLost(f"no '{what}' answer within {self.config.handshake_timeout:.0f} s") from None
            try:
                msgs = protocol.parse_frame(text)
            except (ValueError, UnicodeDecodeError):
                raise StreamLost("malformed frame during the handshake") from None
            for m in msgs:
                if m.get("T") == "error":
                    self._error(m)
                elif m.get("T") == "success" and m.get("msg") == what:
                    return

    def _error(self, m: dict) -> None:
        code = m.get("code")
        kind = protocol.classify(code)
        logger.warning("Research %s stream: Alpaca error %s (%s)", self.endpoint, code,
                       limits.clip(m.get("msg"), 120))
        if kind == "refused":
            raise protocol.refused_error(code)
        if kind == "symbol_limit" or code == protocol.INVALID_SYNTAX:
            self._request_failed(code)
            return
        if kind == "lost":
            raise StreamLost(f"Alpaca error {code}")

    # --- subscriptions (review L5) ------------------------------------------------------------
    # Alpaca answers every subscribe/unsubscribe in order with a "subscription" message (everything
    # subscribed afterwards) or an error; 405 (symbol limit) rejects the WHOLE request. Refused symbols are
    # not on the wire (not in ``subscribed``): they carry a reason and are retried one symbol per request
    # right after a refused batch of several, when a slot frees (an unsubscribe) and at every demand change.
    async def _sync_subscriptions(self, conn) -> None:
        self._subs_dirty = False
        want = {protocol.wire_symbol(self.endpoint, s): s for s in self.desired}
        before = dict(self.rejected)
        self.rejected = {p: r for p, r in self.rejected.items() if p in want.values()}
        on_wire = list(self.subscribed) + [w for w in self.pending if w not in self.subscribed]
        remove = [w for w in on_wire if w not in want]
        retry = self._retry_rejected or bool(remove)
        self._retry_rejected = False
        add = [w for w in want if w not in on_wire and want[w] not in self.rejected]
        again = [w for w in want if w not in on_wire and want[w] in self.rejected] if retry else []
        if remove:
            for w in remove:
                self.subscribed.pop(w, None)
                self.pending.pop(w, None)
            await self._request(conn, "unsubscribe", remove, want)
        if add:
            await self._request(conn, "subscribe", add, want)
        for w in again:
            await self._request(conn, "subscribe", [w], want)
        if remove or add or again or before != self.rejected:
            self._hub._on_conn_change(self)

    async def _request(self, conn, action: str, wires: List[str], want: Dict[str, str]) -> None:
        if action == "subscribe":
            for w in wires:
                self.pending[w] = want[w]
        self._requests.append((action, list(wires)))
        await conn.send(protocol.subscribe_message(self.endpoint, action, wires))

    def _subscription_answer(self, listed: List[str]) -> None:
        self.confirmed = set(listed)
        req = self._requests.popleft() if self._requests else None
        self._last_unconfirmed = []
        if req is not None and req[0] == "subscribe":
            for w in req[1]:
                p = self.pending.pop(w, None)
                if p is None:
                    continue                        # unsubscribed meanwhile (its unsubscribe answer follows)
                if w in self.confirmed:
                    self.subscribed[w] = p
                    self.rejected.pop(p, None)
                else:
                    self.rejected[p] = protocol.UNCONFIRMED_REASON.format(symbol=p)
                    self._last_unconfirmed.append(p)
        self._hub._on_conn_change(self)

    def _request_failed(self, code) -> None:
        """405 / 400 answer the oldest open request: a refused subscribe leaves its symbols off the wire."""
        reason = protocol.ALPACA_SYMBOL_LIMIT_REASON if code == protocol.SYMBOL_LIMIT \
            else protocol.UNCONFIRMED_REASON
        req = self._requests.popleft() if self._requests else None
        if req is not None and req[0] == "subscribe":
            refused = [self.pending.pop(w) for w in req[1] if w in self.pending]
            for p in refused:
                self.rejected[p] = reason.format(symbol=p)
            if len(req[1]) > 1 and refused:
                self._retry_rejected = self._subs_dirty = True     # find out which ones fit, one by one
        elif req is None:
            # the answer already came as a "subscription" without them (partial accept): give the reason
            for p in self._last_unconfirmed:
                if p in self.rejected:
                    self.rejected[p] = reason.format(symbol=p)
        self._last_unconfirmed = []
        self._hub._on_conn_change(self)

    async def _stream(self, conn) -> None:
        recv_task = wake_task = None
        last_frame = self._mono()
        linger_until = None
        try:
            while True:
                if self._subs_dirty:
                    await self._sync_subscriptions(conn)
                if not self.is_wanted():
                    if linger_until is None:
                        linger_until = self._mono() + self.config.linger
                    elif self._mono() >= linger_until:
                        logger.info("Research %s stream closed (no demand for %.0f s)", self.endpoint,
                                    self.config.linger)
                        return
                else:
                    linger_until = None
                timeout = self.config.silence - (self._mono() - last_frame)
                if linger_until is not None:
                    timeout = min(timeout, linger_until - self._mono())
                if recv_task is None:
                    recv_task = asyncio.ensure_future(conn.recv(None))
                wake_task = asyncio.ensure_future(self._wake.wait())
                done, _ = await asyncio.wait({recv_task, wake_task}, timeout=max(0.0, timeout),
                                             return_when=asyncio.FIRST_COMPLETED)
                if wake_task.done():
                    self._wake.clear()
                else:
                    wake_task.cancel()
                if recv_task in done:
                    task, recv_task = recv_task, None
                    text = task.result()          # StreamLost on a closed connection
                    last_frame = self._mono()
                    self.last_message_at = self._now()
                    self._frame(text)
                elif self._mono() - last_frame >= self.config.silence:
                    if not await conn.ping(self.config.ping_timeout):
                        raise StreamLost(f"no frame for {self.config.silence:.0f} s and no pong")
                    last_frame = self._mono()
        finally:
            if wake_task is not None and not wake_task.done():
                wake_task.cancel()                # also on cancel: no pending task left behind
            if recv_task is not None:
                recv_task.cancel()
                with contextlib.suppress(BaseException):
                    await recv_task

    def _frame(self, text) -> None:
        try:
            msgs = protocol.parse_frame(text)
        except (ValueError, UnicodeDecodeError):
            logger.warning("Research %s stream: malformed frame ignored", self.endpoint)
            return
        hub = self._hub
        for m in msgs:
            t = m.get("T")
            if t == "t" and self.endpoint != "news":
                tr = protocol.parse_trade(m, self.endpoint)
                if tr is not None:
                    hub.on_trade(self.endpoint, tr)
            elif t == "b" and self.endpoint != "news":
                bar = protocol.parse_bar(m)
                if bar is not None:
                    hub.on_bar(self.endpoint, bar)
            elif t == "n" and self.endpoint == "news":
                item = protocol.parse_news(m)
                if item is not None:
                    hub.on_news(item, [s for s in (m.get("symbols") or []) if isinstance(s, str)])
            elif t == "subscription":
                key = "news" if self.endpoint == "news" else "trades"
                self._subscription_answer([s for s in (m.get(key) or []) if isinstance(s, str)])
            elif t == "error":
                self._error(m)
