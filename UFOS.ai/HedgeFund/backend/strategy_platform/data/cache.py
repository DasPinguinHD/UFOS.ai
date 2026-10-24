"""Bar cache in hedgefund.db (table ``bars``) — SPEC §3.

``BarCache.get_bars(symbol, timeframe, start, end=None, *,
require_fresh_through=None) -> [bar dict]`` (oldest first, providers/base.py
shape, only bars with ``known_at <= now`` — an unfinished bar is never
stored or returned).

- Cached bars of closed periods are history, not a fallback: they are
  served from hedgefund.db without a request.
- Freshness is explicit: ``require_fresh_through=T`` means "the bar that
  ends at or just before T must be included". Daily equities: the bar of
  the last session closing at or before T (``known_at >= that close`` —
  sessions differ in length, early closes 13:00 NY; pass e.g.
  ``calendars.last_close_before(now)``); fixed-length bars (crypto daily,
  intraday): the newest bar's ``known_at`` is later than ``T - one period``
  (crypto: pass ``now``). If the cache does not satisfy it, the missing tail
  is fetched from the configured source; if that fetch fails or the source
  has no such bar yet, ``DataSourceUnavailable`` is raised — stale bars are
  never returned as if current.
- Without ``require_fresh_through`` a range whose end lies clearly after the
  newest cached bar (more than one period + ``TAIL_SLACK``) is completed
  from the source the same way (failure -> the error, no partial answer).
- Adjusted prices (``adjustment="all"``) change after every new dividend or
  split. Each tail fetch re-reads the newest cached bar; if its prices
  differ (relative > ``ADJUSTMENT_TOLERANCE``), the whole stored history of
  the symbol is fetched again and replaced.
- A range that starts clearly before the first cached bar
  (> ``HEAD_SLACK``) is fetched once; where the source's history starts
  later (IPO, plan limit), that start is remembered in memory.
- hedgefund.db failures raise ``StorageUnavailable`` (storage.db) — the
  cache does not silently run without its store.
- Two price series (SPEC §8.1): ``adjustment="all"`` (default — every
  stage-1 caller) and ``adjustment="split"`` (splits only; equities only).
  Each series is cached, freshness-checked and adjustment-change-checked on
  its own key (symbol, timeframe, source, adjustment); a series is never
  completed with rows of another series or source.

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ..core.errors import DataSourceUnavailable, RequestTooLarge
from ..core.instruments import instrument
from . import http_gate
from .providers.base import TIMEFRAMES, bars_data_type, iso, parse_ts

logger = logging.getLogger(__name__)

HEAD_SLACK = {"1Day": timedelta(days=5), "1Hour": timedelta(days=3), "1Min": timedelta(days=3)}
TAIL_SLACK = {"1Day": timedelta(days=5), "1Hour": timedelta(days=3), "1Min": timedelta(days=3)}
ADJUSTMENT_TOLERANCE = 1e-6
# One provider call covers at most this time window, so one SDK call stays
# around one Alpaca page (≤ 10 000 bars, 24/7 crypto) — i.e. about one token of
# the HTTP gate's rate limit. Longer ranges are fetched window by window.
FETCH_WINDOW = {"1Day": timedelta(days=3660), "1Hour": timedelta(days=400), "1Min": timedelta(days=6)}
# Largest range one get_bars request may need from the source (else RequestTooLarge).
MAX_REQUEST_RANGE = {"1Day": timedelta(days=366 * 40), "1Hour": timedelta(days=366 * 5),
                     "1Min": timedelta(days=31)}
SAVE_CHUNK_ROWS = 10000          # storage.db.upsert_bars accepts at most 50 000 rows per call
ADJUSTMENTS = ("all", "split")   # = storage.db.ADJUSTMENTS (SPEC §8.1)
HISTORY_DATA_TYPES = ("equity_bars_history",)   # Alpaca SIP daily history (user decision 2026-10-06)
# Market Research (its SPEC §7.5, MR2a review M3b): a research-priority caller that waits in the gate
# while holding a series lock gives way when a normal caller waits for that lock, then retries.
MAX_RESEARCH_YIELDS = 50


def _now_fn(clock):
    if clock is None:
        return lambda: datetime.now(timezone.utc)
    return clock.now_utc if hasattr(clock, "now_utc") else clock


def required_known_at(require_fresh_through, timeframe: str, asset_class: str) -> Optional[datetime]:
    """Daily EQUITY bars: the close of the last session at or before T — the
    bar of that session itself must be there (``known_at >= close``). Session
    lengths differ (early closes at 13:00 NY), so "T minus one period" would
    accept the previous session's bar on/after an early-close day. None for
    fixed-length bars (crypto daily, intraday): there ``known_at > T - period``."""
    if timeframe == "1Day" and asset_class == "equity":
        from ..core import calendars
        return calendars.last_close_before(parse_ts(require_fresh_through))
    return None


def is_fresh(rows: List[dict], require_fresh_through, timeframe: str, asset_class: Optional[str] = None) -> bool:
    """True when the newest row satisfies ``require_fresh_through`` (see
    ``required_known_at``). ``asset_class`` defaults to the rows' symbol's."""
    if not rows:
        return False
    if asset_class is None:
        asset_class = instrument(rows[-1]["symbol"]).asset_class
    last_known = parse_ts(rows[-1]["known_at"])
    need = required_known_at(require_fresh_through, timeframe, asset_class)
    if need is not None:
        return last_known >= need
    return last_known > parse_ts(require_fresh_through) - TIMEFRAMES[timeframe]


class BarCache:
    def __init__(self, store=None, fetch=None, configured_source=None, clock=None):
        self._store = store                      # storage.db module or compatible (tests)
        self._fetch_fn = fetch                   # registry.fetch-like (data_type, op, *args)
        self._configured_fn = configured_source  # registry.configured_source-like
        self._now = _now_fn(clock)
        self._locks: Dict[Tuple[str, str, str], threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._waiters: Dict[Tuple[str, str, str], int] = {}   # normal callers waiting for a series lock
        self._history_start: Dict[Tuple[str, str, str], Tuple[datetime, datetime]] = {}
        self.provider_calls = 0

    # --- dependencies ----------------------------------------------------------------------

    def _db(self):
        if self._store is not None:
            return self._store
        from ..storage import db
        return db

    def _fetch(self, data_type, op, *args, **kw):
        if self._fetch_fn is not None:
            return self._fetch_fn(data_type, op, *args, **kw)
        from . import registry
        return registry.fetch(data_type, op, *args, **kw)

    def _configured_source(self, data_type: str) -> str:
        if self._configured_fn is not None:
            return self._configured_fn(data_type)
        from . import registry
        return registry.configured_source(data_type)

    def _lock_for(self, key) -> threading.Lock:
        with self._locks_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock

    @contextmanager
    def _held(self, key):
        """The series lock. Normal callers are counted while they wait for it; a research-priority holder
        polls that count in every gate wait step and raises ``http_gate.ResearchYield`` (released here) —
        so Research never keeps a platform request waiting on a series lock while IT waits for the gate."""
        lock = self._lock_for(key)
        if not http_gate.is_research_priority():
            with self._locks_lock:
                self._waiters[key] = self._waiters.get(key, 0) + 1
            try:
                lock.acquire()
            finally:
                with self._locks_lock:
                    self._waiters[key] -= 1
            try:
                yield
            finally:
                lock.release()
            return
        lock.acquire()
        try:
            with http_gate.research_yield_check(lambda: self._waiters.get(key, 0) > 0):
                yield
        finally:
            lock.release()

    # --- helpers -----------------------------------------------------------------------------

    def _provider_bars(self, data_type, symbol, timeframe, start, end, now, adjustment="all") -> List[dict]:
        """Bars from the source, window by window (FETCH_WINDOW); a range
        longer than MAX_REQUEST_RANGE raises RequestTooLarge (nothing fetched)."""
        start, end = parse_ts(start), parse_ts(end)
        if end - start > MAX_REQUEST_RANGE[timeframe]:
            days = MAX_REQUEST_RANGE[timeframe].days
            raise RequestTooLarge(f"The requested range is too large for {timeframe} bars of {symbol} "
                                  f"(at most {days} days per request) — choose a shorter period.")
        by_ts: Dict[str, dict] = {}
        lo = start
        while True:
            hi = min(lo + FETCH_WINDOW[timeframe], end)
            self.provider_calls += 1
            # stage-1 call shape for "all"; the split series asks the provider for it explicitly
            kw = {} if adjustment == "all" else {"adjustment": adjustment}
            for r in self._fetch(data_type, "get_bars", symbol, timeframe, lo, hi, **kw) or []:
                r = dict(r)
                r["adjustment"] = adjustment
                by_ts[r["ts"]] = r
            if hi >= end:
                break
            lo = hi
        complete = [r for r in by_ts.values() if parse_ts(r["known_at"]) <= now]  # never store an unfinished bar
        complete.sort(key=lambda r: r["ts"])
        return complete

    def _save(self, rows: List[dict]) -> None:
        for i in range(0, len(rows or []), SAVE_CHUNK_ROWS):
            self._db().upsert_bars(rows[i:i + SAVE_CHUNK_ROWS])

    @staticmethod
    def _adjustment_changed(cached_last: dict, fetched: List[dict]) -> bool:
        same = next((r for r in fetched if r["ts"] == cached_last["ts"]), None)
        if same is None:
            return False
        for f in ("open", "high", "low", "close"):
            a, b = float(cached_last[f]), float(same[f])
            if abs(a - b) > ADJUSTMENT_TOLERANCE * max(abs(a), abs(b), 1e-12):
                return True
        return False

    def _history_known_from(self, key, start: datetime) -> bool:
        hs = self._history_start.get(key)
        return hs is not None and hs[0] <= start

    def _note_history_start(self, key, asked_from: datetime, rows: List[dict], timeframe: str) -> None:
        first = parse_ts(rows[0]["ts"]) if rows else None
        if first is None or first - asked_from > HEAD_SLACK[timeframe]:
            prev = self._history_start.get(key)
            if prev is None or asked_from < prev[0]:
                self._history_start[key] = (asked_from, first or asked_from)

    # --- API ------------------------------------------------------------------------------------

    def get_bars(self, symbol: str, timeframe: str, start, end=None, *, require_fresh_through=None,
                 adjustment: str = "all", data_type: Optional[str] = None) -> List[dict]:
        if not http_gate.is_research_priority():
            return self._get_bars(symbol, timeframe, start, end, require_fresh_through=require_fresh_through,
                                  adjustment=adjustment, data_type=data_type)
        for _ in range(MAX_RESEARCH_YIELDS):
            try:
                return self._get_bars(symbol, timeframe, start, end, require_fresh_through=require_fresh_through,
                                      adjustment=adjustment, data_type=data_type)
            except http_gate.ResearchYield:
                continue                   # the lock was released for a platform request; try again
        raise DataSourceUnavailable("alpaca", data_type or "bars",
                                    "Research gave way to trading/platform requests for this series too often — "
                                    "try again in a moment")

    def _get_bars(self, symbol: str, timeframe: str, start, end=None, *, require_fresh_through=None,
                  adjustment: str = "all", data_type: Optional[str] = None) -> List[dict]:
        """``data_type`` None = the asset class's live bar type (equity_bars = Alpaca IEX,
        crypto_bars); ``"equity_bars_history"`` = Alpaca SIP daily history (user decision
        2026-10-06, backtests). Each type has its own source label, so IEX and SIP rows
        are never mixed in one cached series."""
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {timeframe!r}")
        if adjustment not in ADJUSTMENTS:
            raise ValueError(f"adjustment must be one of {', '.join(ADJUSTMENTS)}")
        inst = instrument(symbol)
        if adjustment != "all" and inst.asset_class != "equity":
            raise ValueError(f"adjustment {adjustment!r} applies to equities only ({inst.symbol} has no "
                             "corporate actions)")
        if data_type is None:
            data_type = bars_data_type(inst.asset_class)
        elif data_type not in HISTORY_DATA_TYPES or inst.asset_class != "equity":
            raise ValueError(f"data_type {data_type!r} is not a bar history type for {inst.symbol}")
        now = parse_ts(self._now())
        start = parse_ts(start)
        end = min(parse_ts(end), now) if end is not None else now
        if start >= end:
            raise ValueError("start must lie before end (and before now)")
        fresh = parse_ts(require_fresh_through) if require_fresh_through is not None else None
        if fresh is not None and fresh > now:
            raise ValueError("require_fresh_through lies in the future")
        period = TIMEFRAMES[timeframe]
        source = self._configured_source(data_type)
        key = (inst.symbol, timeframe, source, adjustment)
        adj = adjustment
        db = self._db()
        with self._held(key):
            cached = db.query_bars(inst.symbol, timeframe, iso(start), iso(end), source, known_by=iso(now),
                                   adjustment=adj)
            merged = {r["ts"]: r for r in cached}
            full = False
            if not cached:
                rows = self._provider_bars(data_type, inst.symbol, timeframe, start, end, now, adj)
                self._save(rows)
                self._note_history_start(key, start, rows, timeframe)
                merged = {r["ts"]: r for r in rows}
                full = True
            else:
                last_known = parse_ts(cached[-1]["known_at"])
                tail_missing = (not is_fresh(cached, fresh, timeframe, inst.asset_class)) if fresh is not None else \
                    last_known < end - period - TAIL_SLACK[timeframe]
                if tail_missing:
                    rows = self._provider_bars(data_type, inst.symbol, timeframe, parse_ts(cached[-1]["ts"]), end,
                                               now, adj)
                    if self._adjustment_changed(cached[-1], rows):
                        stored = db.query_bars(inst.symbol, timeframe, None, None, source, adjustment=adj)
                        first = min(parse_ts(stored[0]["ts"]), start) if stored else start
                        logger.info("%s %s: adjusted prices changed (dividend/split) — refetching from %s",
                                    inst.symbol, timeframe, iso(first)[:10])
                        rows = self._provider_bars(data_type, inst.symbol, timeframe, first, end, now, adj)
                        merged = {}
                        full = True
                    self._save(rows)
                    for r in rows:
                        merged[r["ts"]] = r
                first_ts = parse_ts(min(merged)) if merged else None
                if not full and first_ts is not None and first_ts - start > HEAD_SLACK[timeframe] \
                        and not self._history_known_from(key, start):
                    # the head fetch includes the first cached bar: if its adjusted prices
                    # changed (dividend/split since caching) the whole history is refetched,
                    # never older bars on a different adjustment basis next to cached ones
                    head = self._provider_bars(data_type, inst.symbol, timeframe, start, first_ts, now, adj)
                    if self._adjustment_changed(merged[min(merged)], head):
                        logger.info("%s %s: adjusted prices changed (dividend/split) — refetching from %s",
                                    inst.symbol, timeframe, iso(start)[:10])
                        head = self._provider_bars(data_type, inst.symbol, timeframe, start, end, now, adj)
                        merged = {}
                    self._save(head)
                    self._note_history_start(key, start, head, timeframe)
                    for r in head:
                        merged[r["ts"]] = r
            s_iso, e_iso = iso(start), iso(end)
            result = [merged[t] for t in sorted(merged)
                      if s_iso <= merged[t]["ts"] <= e_iso and parse_ts(merged[t]["known_at"]) <= now]
        if fresh is not None and not is_fresh(result, fresh, timeframe, inst.asset_class):
            latest = result[-1]["ts"] if result else "none"
            raise DataSourceUnavailable(source, data_type,
                                        f"no {timeframe} bar for {inst.symbol} through {iso(fresh)} yet "
                                        f"(newest available bar starts {latest})")
        return result


_default: Optional[BarCache] = None
_default_lock = threading.Lock()


def default_cache() -> BarCache:
    global _default
    with _default_lock:
        if _default is None:
            _default = BarCache()
        return _default


def set_default_cache(cache: Optional[BarCache]) -> None:
    global _default
    with _default_lock:
        _default = cache


def get_bars(symbol: str, timeframe: str, start, end=None, *, require_fresh_through=None,
             adjustment: str = "all", data_type: Optional[str] = None) -> List[dict]:
    if data_type is not None:
        return default_cache().get_bars(symbol, timeframe, start, end, require_fresh_through=require_fresh_through,
                                        adjustment=adjustment, data_type=data_type)
    if adjustment == "all":           # stage-1 call shape (fakes in tests take no adjustment keyword)
        return default_cache().get_bars(symbol, timeframe, start, end, require_fresh_through=require_fresh_through)
    return default_cache().get_bars(symbol, timeframe, start, end, require_fresh_through=require_fresh_through,
                                    adjustment=adjustment)
