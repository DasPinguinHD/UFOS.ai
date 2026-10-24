"""Live candles from stream trades (SPEC §4.3 "candle", §7.1, §7.2).

One ``LiveCandle`` per topic ``candle:<SYM>:<tf>:<session>`` (shared by all
subscribers — the hub multiplexes). Bucket rules = SPEC §7.2 (the same as
``bars/aggregate.py`` of the REST side, test-checked): equities in
America/New_York, ``rth`` anchored 09:30 (session end 16:00 / 13:00 early
close), ``eth`` anchored 04:00 (end 20:00 / 17:00); the last bucket is cut at
the session end; trades outside the session window are ignored. ``1D`` t =
00:00 New York of the session date, ``1W`` t = Monday 00:00 New York.
Crypto (session ``utc``): UTC-aligned floor(t, tf), 1D = UTC midnight, 1W =
Monday 00:00 UTC.

A candle is built from per-minute parts (trades of the minute; a stream
minute bar REPLACES its minute — it is Alpaca's authoritative IEX bar), plus
a seed: the REST 1m bars (1W: daily bars for the finished days) of the part
of the current bucket that elapsed before the topic started or the stream
reconnected — without a seed the candle is NOT sent (a partial candle would
show wrong open/high/low/volume; no stand-in values). Equity candles carry
``feed: "iex_tail", final: false`` (also when the bucket ends — SIP replaces
them later, ``sip_replace``); crypto candles ``feed: "crypto"``, the closing
message has ``final: true``. A trade or stream minute bar of the bucket closed last (the hub closes buckets
by its own clock; the minute bar always arrives after its minute) never restarts that bucket: within
``LATE_TRADE_GRACE_SECONDS`` after the bucket end it is merged and the corrected bar (same ``t``) is sent
again; later ones are dropped and counted (``late_dropped``).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import time as _time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, Optional

from strategy_platform.core import calendars

from .. import limits
from ..bars import aggregate as agg

UTC = timezone.utc
NY = calendars.NY
TIMEFRAMES = agg.TIMEFRAMES
INTRADAY_MINUTES = agg.TF_MINUTES
EQUITY_SESSIONS = agg.SESSIONS
CRYPTO_SESSION = "utc"

# t = bucket start (the bar's t); open = first instant a trade can fall into it; end = exclusive end
Bucket = namedtuple("Bucket", "t open end")


def floor_minute(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(second=0, microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def bucket_for(ts: datetime, tf: str, session: str, asset_class: str) -> Optional[Bucket]:
    """The bucket containing ``ts`` or None (equity trade outside the session window / non-trading day).
    Intraday (and crypto 1D) buckets come from ``bars/aggregate.py`` (the REST side's rules)."""
    ts = ts.astimezone(UTC)
    if tf not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {tf!r}")
    if asset_class == "crypto":
        if tf == "1W":
            monday = agg.week_start(ts, "crypto")
            return Bucket(monday, monday, monday + timedelta(days=7))
        start = agg.bucket_start(ts, tf, "rth", "crypto")
        return Bucket(start, start, agg.bucket_end(start, tf, "rth", "crypto"))
    if tf in INTRADAY_MINUTES:
        start = agg.bucket_start(ts, tf, session, "equity")
        if start is None:
            return None
        return Bucket(start, start, agg.bucket_end(start, tf, session, "equity"))
    d = ts.astimezone(NY).date()
    win = agg.session_window(d, session)
    if win is None or not (win[0] <= ts < win[1]):
        return None
    if tf == "1D":
        return Bucket(agg.ny_midnight(d), win[0], win[1])
    monday = d - timedelta(days=d.weekday())
    first = last = None
    for i in range(5):
        w = agg.session_window(monday + timedelta(days=i), session)
        if w is not None:
            first = first or w
            last = w
    return Bucket(agg.ny_midnight(monday), first[0], last[1])


class _Part:
    __slots__ = ("o", "h", "l", "c", "v", "pv", "n", "first", "last", "vwap_ok", "src")

    def __init__(self, o, h, lo, c, v, pv, n, first, last, vwap_ok=True, src="trade"):
        self.o, self.h, self.l, self.c, self.v, self.pv, self.n = o, h, lo, c, v, pv, n
        self.first, self.last, self.vwap_ok, self.src = first, last, vwap_ok, src

    @classmethod
    def from_bar(cls, b: dict, t: datetime, end: datetime):
        v = float(b.get("v") or 0.0)
        vwap = b.get("vwap")
        return cls(b["o"], b["h"], b["l"], b["c"], v, (vwap * v) if vwap is not None else 0.0,
                   int(b.get("n") or 0), t, end, vwap is not None, "bar")

    def add_trade(self, ts, price, size):
        if ts < self.first:
            self.first, self.o = ts, price
        if ts >= self.last:
            self.last, self.c = ts, price
        self.h, self.l = max(self.h, price), min(self.l, price)
        self.v += size
        self.pv += price * size
        self.n += 1


class LiveCandle:
    def __init__(self, symbol: str, tf: str, session: str, asset_class: str):
        if tf not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {tf!r}")
        self.symbol, self.tf, self.session, self.asset_class = symbol, tf, session, asset_class
        self.feed = "crypto" if asset_class == "crypto" else "iex_tail"
        self.streaming_since: Optional[datetime] = None    # set by the hub while the symbol streams
        self.late_dropped = 0                              # late trades / bars beyond the grace window (diagnostic)
        self.reset()

    # --- state ------------------------------------------------------------------------------
    def reset(self) -> None:
        """Drops the current bucket (stream gap): nothing is sent until the next bucket or a new seed."""
        self.bucket: Optional[Bucket] = None
        self.parts: Dict[object, _Part] = {}
        self.ready = False
        self.needs_seed = False
        self.seed_until: Optional[datetime] = None
        self.dirty = False
        # the bucket closed last: its t (a later trade/bar of it never restarts it) and, when its bar was sent,
        # (bucket, parts, seed_until, last sent bar) for late corrections within LATE_TRADE_GRACE_SECONDS
        self.closed_t: Optional[datetime] = None
        self.prev = None

    def _start(self, b: Bucket, live_since: Optional[datetime]) -> None:
        self.bucket, self.parts, self.dirty = b, {}, False
        self.ready = live_since is not None and live_since <= b.open
        self.needs_seed = not self.ready
        self.seed_until = None

    def bucket_for(self, ts: datetime) -> Optional[Bucket]:
        return bucket_for(ts, self.tf, self.session, self.asset_class)

    def begin_at(self, now: datetime) -> bool:
        """Topic start / reconnect: open the bucket of ``now`` (if any) and ask for a seed. True = seed needed."""
        self.reset()
        b = self.bucket_for(now)
        if b is None:
            return False
        self._start(b, None)
        return True

    # --- input ------------------------------------------------------------------------------
    def apply_trade(self, ts: datetime, price: float, size: float, live_since: Optional[datetime],
                    now: Optional[datetime] = None) -> Optional[dict]:
        """Adds a trade. Returns the CLOSED previous bar when this trade opened a new bucket, the CORRECTED
        closed bar for a late trade of the bucket closed last (within LATE_TRADE_GRACE_SECONDS after its end,
        by ``now``), else None. A late trade never restarts a closed bucket (review H1)."""
        b = self.bucket_for(ts)
        if b is None:
            return None
        if self._is_late(b):
            return self._late(b, now, lambda parts, seed_until: _add_trade(parts, ts, price, size, seed_until))
        closed = None
        if self.bucket is None or b.t > self.bucket.t:
            if self.bucket is not None:
                closed = self.close()
            self._start(b, live_since)
        if _add_trade(self.parts, ts, price, size, self.seed_until):
            self.dirty = True                # (a trade before seed_until is already in the seed)
        return closed

    def _is_late(self, b: Bucket) -> bool:
        if self.bucket is not None:
            return b.t < self.bucket.t
        return self.closed_t is not None and b.t <= self.closed_t

    def _late(self, b: Bucket, now: Optional[datetime], merge) -> Optional[dict]:
        """A trade / minute bar of an already closed bucket: merged into the closed bar and the CORRECTED bar
        returned (same t; crypto final true) while within the grace window — never a candle of its own."""
        prev = self.prev
        if prev is None or prev[0].t != b.t or \
                (now is not None and (now - prev[0].end).total_seconds() > limits.LATE_TRADE_GRACE_SECONDS):
            self.late_dropped += 1
            return None
        bucket, parts, seed_until, sent = prev
        if not merge(parts, seed_until):
            return None
        bar = _make_bar(bucket, parts, self.feed, self.asset_class == "crypto",
                        agg.session_flag(bucket.t, self.asset_class, self.tf))
        if bar == sent:
            return None
        self.prev = (bucket, parts, seed_until, bar)
        return bar

    def apply_bar(self, bar: dict, now: Optional[datetime] = None) -> Optional[dict]:
        """A stream minute bar replaces its minute (authoritative IEX / crypto bar). For a minute of the bucket
        closed last (the bar arrives after the minute ends) it returns the corrected closed bar (see _late)."""
        t = bar["t"]
        b = self.bucket_for(t)
        if b is None:
            return None
        if self.bucket is not None and b.t == self.bucket.t:
            self.parts[t] = _Part.from_bar(bar, t, t + timedelta(seconds=59))
            self.dirty = True
            return None
        if self._is_late(b):
            def merge(parts, _seed_until):
                parts[t] = _Part.from_bar(bar, t, t + timedelta(seconds=59))
                return True
            return self._late(b, now, merge)
        return None

    def seed_window(self, now: datetime):
        """(start, until) of the REST seed for the current bucket: [bucket open, floor_minute(now))."""
        if self.bucket is None:
            return None
        until = max(self.bucket.open, floor_minute(now))
        self.seed_until = until
        return self.bucket.open, until

    def apply_seed(self, minutes: Iterable[dict], days: Iterable[dict] = ()) -> None:
        """Seed parts (REST): ``minutes`` 1m bars ({"t": datetime, o,h,l,c,v,vwap,n}) before seed_until,
        ``days`` daily bars of finished days (1W only). A stream minute bar that arrived meanwhile wins;
        a trades-only minute (partial) is replaced."""
        if self.bucket is None:
            return
        until = self.seed_until or self.bucket.open
        # finished parts of a week: equity daily bars (t = 00:00 NY), crypto HOURLY bars (UTC) — each part's
        # last instant is inside its own period, so the close comes from the latest part (review M2)
        span = timedelta(hours=1) if self.asset_class == "crypto" else timedelta(hours=23)
        for d in days:
            key = ("d", d["t"])
            if key not in self.parts:
                self.parts[key] = _Part.from_bar(d, d["t"], d["t"] + span - timedelta(seconds=1))
        for m in minutes:
            t = m["t"]
            if t >= until or (t in self.parts and self.parts[t].src == "bar"):
                continue                     # a stream minute bar is as good; a trades-only minute is partial
            b = self.bucket_for(t)
            if b is None or b.t != self.bucket.t:
                continue
            self.parts[t] = _Part.from_bar(m, t, t + timedelta(seconds=59))
        self.ready, self.needs_seed, self.dirty = True, False, True

    # --- output -----------------------------------------------------------------------------
    def due(self, now: datetime) -> bool:
        return self.bucket is not None and now >= self.bucket.end

    def close(self) -> Optional[dict]:
        """Ends the current bucket; returns its last bar (crypto: final true) when it was complete."""
        bar = self.bar(final=self.asset_class == "crypto") if self.ready and self.parts else None
        if self.bucket is not None:
            self.closed_t = self.bucket.t
            self.prev = (self.bucket, self.parts, self.seed_until, bar) if bar is not None else None
        self.bucket, self.parts, self.ready, self.needs_seed, self.seed_until, self.dirty = \
            None, {}, False, False, None, False
        return bar

    def bar(self, final: bool = False) -> Optional[dict]:
        if self.bucket is None or not self.parts:
            return None
        return _make_bar(self.bucket, self.parts, self.feed, final,
                         agg.session_flag(self.bucket.t, self.asset_class, self.tf))


def _add_trade(parts: Dict[object, _Part], ts: datetime, price: float, size: float,
               seed_until: Optional[datetime]) -> bool:
    """Adds a trade to its minute part. False when the minute is already covered by the REST seed."""
    if seed_until is not None and ts < seed_until:
        return False
    key = floor_minute(ts)
    part = parts.get(key)
    if part is None:
        parts[key] = _Part(price, price, price, price, size, price * size, 1, ts, ts)
    else:
        part.add_trade(ts, price, size)
    return True


def _make_bar(bucket: Bucket, parts_map: Dict[object, _Part], feed: str, final: bool,
              s: Optional[str] = None) -> dict:
    """``s`` = session flag of an intraday equity candle (``aggregate.session_flag``; MR2a follow-up)."""
    parts = list(parts_map.values())
    first = min(parts, key=lambda p: p.first)
    last = max(parts, key=lambda p: p.last)
    v = sum(p.v for p in parts)
    vwap = (sum(p.pv for p in parts) / v) if v > 0 and all(p.vwap_ok for p in parts) else None
    bar = {"t": iso(bucket.t), "o": first.o, "h": max(p.h for p in parts), "l": min(p.l for p in parts),
           "c": last.c, "v": v, "vwap": vwap, "n": sum(p.n for p in parts), "feed": feed, "final": bool(final)}
    if s is not None:
        bar["s"] = s
    return bar


class RateLimiter:
    """At most ``per_second`` sends per key (minimum spacing 1/per_second)."""

    def __init__(self, per_second: float, monotonic: Callable[[], float] = _time.monotonic):
        self.interval = 1.0 / float(per_second)
        self._monotonic = monotonic
        self._last: Dict[object, float] = {}

    def ready(self, key) -> bool:
        last = self._last.get(key)
        return last is None or self._monotonic() - last >= self.interval

    def mark(self, key) -> None:
        self._last[key] = self._monotonic()

    def forget(self, key) -> None:
        self._last.pop(key, None)
