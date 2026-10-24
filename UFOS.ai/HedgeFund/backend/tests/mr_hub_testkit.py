"""Fakes for the Market Research hub tests (no network): a fake Alpaca stream server/transport, a fake
data service, a collecting subscriber and a settable clock.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
import asyncio
import json
import time
from datetime import datetime, timezone

from market_research.hub.alpaca_stream import HubConfig
from market_research.hub.demand import Subscriber, parse_topic
from market_research.hub.protocol import ENDPOINTS, StreamLost

UTC = timezone.utc
FAST = HubConfig(backoff=(0.02, 0.05, 0.05), linger=0.15, silence=0.3, ping_timeout=0.05, handshake_timeout=0.5,
                 stable=30, recheck_missing_keys=0.2, recheck_refused=0.3, tick=0.01, feeds_interval=0.05,
                 sip_refresh=3600, seed_retry=0.2)
_DROP = object()
_URL_TO_EP = {v: k for k, v in ENDPOINTS.items()}


def keys():
    return ("KEYID", "SECRET")


class Clock:
    def __init__(self, now=datetime(2026, 10, 15, 14, 0, tzinfo=UTC)):
        self.now = now

    def __call__(self):
        return self.now


class FakeConn:
    def __init__(self, server, endpoint):
        self.server, self.endpoint = server, endpoint
        self.q = asyncio.Queue()
        self.sent, self.subs, self.closed, self.pong = [], set(), False, True
        if server.mode.get(endpoint) == "406":
            self.push([{"T": "error", "code": 406, "msg": "connection limit exceeded"}])
        else:
            self.push([{"T": "success", "msg": "connected"}])

    def push(self, msgs):
        self.q.put_nowait(json.dumps(msgs))

    def drop(self):
        self.q.put_nowait(_DROP)

    async def send(self, text):
        if self.closed:
            raise StreamLost("closed")
        m = json.loads(text)
        self.sent.append(m)
        mode = self.server.mode.get(self.endpoint)
        if m["action"] == "auth":
            if mode == "auth_fail":
                self.push([{"T": "error", "code": 402, "msg": "auth failed"}])
            elif mode == "plan":
                self.push([{"T": "error", "code": 409, "msg": "insufficient subscription"}])
            else:
                self.push([{"T": "success", "msg": "authenticated"}])
            return
        key = "news" if self.endpoint == "news" else "trades"
        syms = set(m.get(key) or [])
        if m["action"] == "subscribe":
            limit = self.server.max_symbols
            if limit is not None and not self.server.partial_405 and len(self.subs | syms) > limit:
                # Alpaca docs: 405 rejects the whole request
                self.push([{"T": "error", "code": 405, "msg": "symbol limit exceeded"}])
                return
            over = False
            for s in sorted(syms):
                if limit is not None and len(self.subs) >= limit:
                    over = True
                    continue
                self.subs.add(s)
            self.push([{"T": "subscription", key: sorted(self.subs)}])
            if over:
                self.push([{"T": "error", "code": 405, "msg": "symbol limit exceeded"}])
        else:
            self.subs -= syms
            self.push([{"T": "subscription", key: sorted(self.subs)}])

    async def recv(self, timeout):
        if self.closed:
            raise StreamLost("closed")
        item = await (asyncio.wait_for(self.q.get(), timeout) if timeout is not None else self.q.get())
        if item is _DROP:
            self.closed = True
            raise StreamLost("dropped")
        return item

    async def ping(self, timeout):
        self.server.pings += 1
        return self.pong

    async def close(self):
        self.closed = True
        self.server.closes += 1


class FakeAlpaca:
    def __init__(self):
        self.mode, self.conns, self.connects = {}, [], []
        self.fail_connect, self.max_symbols = set(), None
        self.partial_405 = False            # True: accept up to the limit, answer, then 405 (unverified variant)
        self.closes = self.pings = 0

    async def connect(self, url):
        ep = _URL_TO_EP[url]
        self.connects.append(ep)
        if ep in self.fail_connect:
            raise StreamLost("connect refused")
        c = FakeConn(self, ep)
        self.conns.append(c)
        return c

    def live(self, ep):
        return [c for c in self.conns if c.endpoint == ep and not c.closed]


class _Loader:
    def __init__(self, owner):
        self.o = owner

    def equity_minutes(self, symbol, start, end, *, now=None, with_tail=True):
        self.o.calls.append(("equity_minutes", symbol, start, end, with_tail))
        if self.o.fail:
            raise self.o.fail
        return [m for m in self.o.minutes if start <= m["t"] < end]

    def crypto_minutes(self, symbol, start, end, *, now=None):
        self.o.calls.append(("crypto_minutes", symbol, start, end))
        if self.o.fail:
            raise self.o.fail
        return [m for m in self.o.minutes if start <= m["t"] < end]


class _Sources:
    def __init__(self, owner):
        self.o = owner

    def daily(self, symbol, start, end, **kw):
        self.o.calls.append(("daily", symbol, start, end))
        return list(self.o.daily_rows)

    def crypto_hourly(self, symbol, start, end):
        self.o.calls.append(("crypto_hourly", symbol, start, end))
        return list(self.o.daily_rows)


class FakeData:
    def __init__(self, minutes=(), daily_rows=()):
        self.minutes, self.daily_rows, self.calls, self.fail = list(minutes), list(daily_rows), [], None
        self.loader, self.sources = _Loader(self), _Sources(self)


def minute(t, o=100.0, h=None, lo=None, c=None, v=10.0, vwap=None, n=1, feed="sip"):
    c = o if c is None else c
    return {"t": t, "o": o, "h": max(o, c) if h is None else h, "l": min(o, c) if lo is None else lo, "c": c,
            "v": v, "vwap": o if vwap is None else vwap, "n": n, "feed": feed}


class Sink(Subscriber):
    def __init__(self, *raws, kind="view", active=True):
        super().__init__(kind)
        self.active = active
        self.msgs = []
        self.add(*raws)

    def add(self, *raws):
        for r in raws:
            t = parse_topic(r)
            self.topics[t.raw] = t

    def deliver(self, msg, key=None):
        self.msgs.append(msg)

    def of(self, typ, **match):
        return [m for m in self.msgs if m["type"] == typ and all(m.get(k) == v for k, v in match.items())]


def trade(sym, price, size, ts):
    return {"T": "t", "S": sym, "p": price, "s": size, "t": ts, "i": 1, "x": "V", "z": "C"}


async def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.005)
    return pred()


async def running(hub, body):
    task = asyncio.ensure_future(hub.run())
    try:
        await asyncio.sleep(0)
        return await body()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
