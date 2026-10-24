"""HTTP gate: rate limit + bounded retries around every provider call (SPEC §3).

- ``TokenBucket``: thread-safe token bucket. Default for Alpaca: 150
  requests/min (the free plan allows 200/min) with a burst capacity of 10, so
  no 60-second window can exceed 150 + 10 = 160 requests. Fixed split with the
  Options Structure archive process (its own gate, 30/min — SPEC §21.1, lead
  decision 2026-10-23): 160 + 30 = 190 < 200, no cross-process limiter.
- ``HttpGate.call(fn, data_type=, classify=)``: takes a token, runs ``fn``.
  On an exception ``classify(ex, data_type)`` decides:
    * returns ``Retry(reason, retry_after)`` -> the SAME call to the SAME
      source is retried after a short backoff (only HTTP 429 / 5xx /
      timeouts), at most ``max_attempts`` attempts in total; exhausted ->
      ``DataSourceUnavailable`` naming the last reason;
    * returns a ``UserFacingError`` -> raised at once (auth, 4xx, network);
  ``UserFacingError`` raised by ``fn`` itself (e.g. SecretMissing) passes
  through unchanged. Never an alternative source, never a cached value.
- One gate per source (``gate_for(source)``), shared by all threads.
- Sources with hourly/daily allowances (Tiingo, SPEC §8.1) additionally get
  a ``WindowLimiter`` (``DEFAULT_WINDOWS``): at most N requests in any
  sliding window of the given length. Waiting for it can take up to an hour;
  ``call(..., progress=cb)`` then reports the wait to ``cb(event)`` every few
  seconds ("waiting for Tiingo rate limit (…)") — ``cb`` may raise to cancel.

- Concurrency (SPEC §10.1): a gate may carry a ``concurrency`` limiter
  (``network.AdaptiveLimiter``; ``gate_for`` attaches ``network.limiter_for``
  for alpaca/tiingo). Per attempt it takes a slot FIRST, then the rate-limit
  tokens above — concurrency never bypasses them — and releases it after the
  attempt (not during the backoff). Success / connection-type failure are
  reported to the limiter (AIMD). A ``DataSourceUnavailable`` raised because
  of a connection-type failure (``network.is_connection_error``: timeouts,
  DNS, refused/reset/unreachable, TLS) carries ``kind = "connection"``.

- Research priority (Market Research, ``market_research/SPEC.md`` §7.5,
  extension MR2 2026-10-16, review fixes M3/L6): inside ``with research_priority():``
  (a ``contextvars`` flag, copied into the Research thread pool) an attempt takes
  (1) a research permit: the gate's research bucket (60/min, capacity 5) AND a
  hard cap of 60 research SENDS (+ open permits) in any 60 s; (2) a SHARED
  token only while more than ``RESEARCH_RESERVE_TOKENS`` (3) remain and no
  normal caller waits for a token or a slot; (3) a concurrency slot only while
  no normal caller waits, holding at most max(1, limit − 1) slots (limit 1 →
  only while no normal request is active; ``AdaptiveLimiter.try_acquire``).
  It never waits while holding a slot; waits poll in steps ≤ 0.25 s; after
  ``RESEARCH_ACQUIRE_TIMEOUT_SECONDS`` → ``DataSourceUnavailable`` "Research
  waited too long for the … rate limit (shared with trading)". A send is counted
  when it is made: providers call ``note_send()`` before every HTTP request (the
  hardened alpaca-py session; one call can page into several requests) — under
  research priority every further page takes its own permit + shared token.
  ``research_yield_check(fn)``: while a research call waits, ``fn()`` True →
  ``ResearchYield`` (``is_cancellation``) so the caller can release a lock a
  normal caller waits for (the bar cache does, data/cache.py). Normal priority is
  unchanged (it only counts its callers that wait for a token or slot).
  ``research_stats(source)`` -> ``{"used_last_minute", "waiting_seconds"}``.

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time as _time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence

from ..core.errors import DataSourceUnavailable, UserFacingError
from . import network

logger = logging.getLogger(__name__)

DEFAULT_RATES_PER_MINUTE = {"alpaca": 150, "tiingo": 20}     # alpaca: 150 + burst 10, archive 30 (SPEC §21.1)
DEFAULT_CAPACITY = 10
DEFAULT_CAPACITIES = {"tiingo": 3}
# Tiingo free ("Starter") plan, https://www.tiingo.com/about/pricing (checked 2026-10-03):
# 50 requests/hour, 1,000 requests/day, 500 unique symbols/month, 1 GB/month. Configured
# conservatively at 80 %: 40/hour and 800/day. The process-wide gate (gate_for) persists the
# request times of these windows in hedgefund.db meta (``PersistedStamps``, user decision
# 2026-10-05), so a backend restart does not start them empty; HTTP 429 from Tiingo stops at once.
DEFAULT_WINDOWS = {"tiingo": ((40, 3600.0), (800, 86400.0))}
DEFAULT_ACQUIRE_TIMEOUTS = {"tiingo": 3900.0}      # an hourly window can need ~1 h of waiting
PROGRESS_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = (1.0, 3.0)
MAX_RETRY_AFTER_SECONDS = 10.0
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 120.0
# Market Research share (market_research/SPEC.md §7.5, Q3): the shared bucket (150/min since 2026-10-23); Research
# may use at most 60/min of it and always yields to normal-priority (trading / platform) callers.
RESEARCH_MAX_PER_MINUTE = 60
RESEARCH_CAPACITY = 5
RESEARCH_WINDOW_SECONDS = 60.0
RESEARCH_RESERVE_TOKENS = 3
RESEARCH_ACQUIRE_TIMEOUT_SECONDS = 30.0
RESEARCH_WAIT_STEP_SECONDS = 0.25

_RESEARCH = contextvars.ContextVar("ufos_http_gate_research_priority", default=False)


@contextmanager
def research_priority():
    """Every gate call made inside this block (also in threads that copy the context) runs with
    Research priority (see module docstring)."""
    token = _RESEARCH.set(True)
    try:
        yield
    finally:
        _RESEARCH.reset(token)


def is_research_priority() -> bool:
    return bool(_RESEARCH.get())


class ResearchYield(UserFacingError):
    """A research call gave way while waiting for the gate because a normal-priority caller needs what the
    research caller holds (e.g. the bar cache's per-series lock, ``research_yield_check``). The holder
    releases and retries; not a source failure (``is_cancellation``: the registry records no error)."""
    is_cancellation = True

    def __init__(self):
        super().__init__("Research gave way to a trading/platform request and retries.")


_YIELD_CHECK = contextvars.ContextVar("ufos_http_gate_research_yield_check", default=None)


@contextmanager
def research_yield_check(should_yield):
    """While a research call waits in the gate inside this block, ``should_yield()`` is polled every wait
    step; True -> ResearchYield is raised (the caller releases its lock and retries)."""
    token = _YIELD_CHECK.set(should_yield)
    try:
        yield
    finally:
        _YIELD_CHECK.reset(token)


@dataclass(frozen=True)
class Retry:
    """classify() result: the failure is transient (429 / 5xx / timeout)."""
    reason: str
    retry_after: Optional[float] = None


class TokenBucket:
    """``rate_per_minute`` tokens per minute, at most ``capacity`` stored."""

    def __init__(self, rate_per_minute: float, capacity: int = DEFAULT_CAPACITY,
                 monotonic: Callable[[], float] = _time.monotonic, sleep: Callable[[float], None] = _time.sleep):
        if rate_per_minute <= 0 or capacity < 1:
            raise ValueError("rate_per_minute must be > 0 and capacity >= 1")
        self.rate_per_minute = float(rate_per_minute)
        self.rate = float(rate_per_minute) / 60.0          # tokens per second
        self.capacity = float(capacity)
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = float(capacity)
        self._last = monotonic()

    def _refill(self) -> None:
        now = self._monotonic()
        if now > self._last:
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def try_acquire(self) -> float:
        """Takes a token and returns 0, or returns the seconds to wait."""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0 - 1e-9:          # tolerance: float rounding after an exact wait
                self._tokens = max(0.0, self._tokens - 1.0)
                return 0.0
            return (1.0 - self._tokens) / self.rate

    def try_acquire_above(self, reserve: float) -> float:
        """Takes a token only if more than ``reserve`` tokens are stored (so ``reserve`` stay for others);
        returns 0, or the seconds until that is possible (Research priority, SPEC §7.5)."""
        with self._lock:
            self._refill()
            need = float(reserve) + 1.0
            if self._tokens >= need - 1e-9:
                self._tokens = max(0.0, self._tokens - 1.0)
                return 0.0
            return (need - self._tokens) / self.rate

    def acquire(self, timeout: float = DEFAULT_ACQUIRE_TIMEOUT_SECONDS) -> bool:
        """Blocks until a token is taken (True) or ``timeout`` seconds passed (False)."""
        deadline = self._monotonic() + max(0.0, timeout)
        while True:
            wait = self.try_acquire()
            if wait <= 0:
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._sleep(min(wait, remaining))


class PersistedStamps:
    """Request times (UTC epoch seconds) of a WindowLimiter in hedgefund.db
    ``meta`` under ``key`` (JSON list). Failures raise ``StorageUnavailable``
    — the limiter then refuses the request: without its counters the
    allowance cannot be guaranteed (no silent reset)."""

    def __init__(self, key: str, store=None):
        self.key = key
        self._store = store

    def _db(self):
        if self._store is not None:
            return self._store
        from ..storage import db
        return db

    def load(self) -> list:
        import json
        text = self._db().meta_get(self.key)
        if text is None:
            return []
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"meta {self.key}: expected a list")
        return [float(x) for x in data]

    def save(self, stamps: list) -> None:
        import json
        self._db().meta_set(self.key, json.dumps([round(x, 3) for x in stamps]))


class WindowLimiter:
    """At most ``limit`` acquisitions in any sliding window of ``seconds``,
    for every (limit, seconds) pair. Thread-safe. With ``persist``
    (``PersistedStamps``) the request times survive a restart: loaded on first
    use (wall clock -> monotonic), saved after every acquisition."""

    def __init__(self, windows, monotonic: Callable[[], float] = _time.monotonic, *, persist=None,
                 wall: Callable[[], float] = _time.time):
        self.windows = tuple((int(n), float(sec)) for n, sec in windows)
        if not self.windows or any(n < 1 or sec <= 0 for n, sec in self.windows):
            raise ValueError("windows: (limit >= 1, seconds > 0) pairs")
        self._monotonic = monotonic
        self._wall = wall
        self._persist = persist
        self._loaded = persist is None
        self._lock = threading.Lock()
        self._stamps: list = []

    def _load(self, now: float) -> None:
        if self._loaded:
            return
        wall_now = self._wall()
        # a stamp "in the future" (wall clock moved back) counts as now: never fewer requests than made
        self._stamps = sorted(now - max(0.0, wall_now - w) for w in self._persist.load())
        self._loaded = True

    def _save(self, now: float) -> None:
        if self._persist is not None:
            wall_now = self._wall()
            self._persist.save([wall_now - (now - t) for t in self._stamps])

    def _purge(self, now: float) -> None:
        self._load(now)
        longest = max(sec for _n, sec in self.windows)
        self._stamps = [t for t in self._stamps if now - t < longest]

    def remaining(self) -> list:
        """[(limit, seconds, requests left in that window now)]."""
        with self._lock:
            now = self._monotonic()
            self._purge(now)
            return [(n, sec, max(0, n - sum(1 for t in self._stamps if now - t < sec))) for n, sec in self.windows]

    def try_acquire(self) -> float:
        """Records an acquisition and returns 0, or returns the seconds until one is possible."""
        with self._lock:
            now = self._monotonic()
            self._purge(now)
            wait = 0.0
            for n, sec in self.windows:
                inside = sorted(t for t in self._stamps if now - t < sec)
                if len(inside) >= n:
                    wait = max(wait, inside[len(inside) - n] + sec - now)
            if wait <= 0:
                self._stamps.append(now)
                try:
                    self._save(now)
                except Exception:
                    self._stamps.pop()        # not recorded -> not sent
                    raise
                return 0.0
            return wait


def _describe_windows(source: str, limiter: "WindowLimiter", wait: float) -> str:
    parts = []
    for n, sec, left in limiter.remaining():
        unit = "this hour" if sec == 3600 else ("today" if sec == 86400 else f"per {sec:g} s")
        parts.append(f"{left} requests left {unit}")
    minutes = max(1, int(round(wait / 60.0)))
    return f"waiting for {source.capitalize()} rate limit ({', '.join(parts)}; next slot in about {minutes} min)"


class HttpGate:
    def __init__(self, source: str, rate_per_minute: Optional[float] = None, *, capacity: int = DEFAULT_CAPACITY,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS, backoff_seconds: Sequence[float] = DEFAULT_BACKOFF_SECONDS,
                 acquire_timeout: Optional[float] = None, windows=None, persist=None,
                 monotonic: Callable[[], float] = _time.monotonic, sleep: Callable[[float], None] = _time.sleep,
                 wall: Callable[[], float] = _time.time, concurrency=None):
        self.source = source
        self.concurrency = concurrency          # network.AdaptiveLimiter or None (SPEC §10.1)
        self._count_lock = threading.Lock()
        rate = rate_per_minute if rate_per_minute is not None else DEFAULT_RATES_PER_MINUTE.get(source, 60)
        self.bucket = TokenBucket(rate, capacity, monotonic=monotonic, sleep=sleep)
        if windows is None:
            windows = DEFAULT_WINDOWS.get(source)
        self.limiter = WindowLimiter(windows, monotonic=monotonic, persist=persist, wall=wall) if windows else None
        if acquire_timeout is None:
            acquire_timeout = DEFAULT_ACQUIRE_TIMEOUTS.get(source, DEFAULT_ACQUIRE_TIMEOUT_SECONDS)
        self._monotonic = monotonic
        self.max_attempts = max(1, int(max_attempts))
        self.backoff = tuple(float(b) for b in backoff_seconds) or (1.0,)
        self.acquire_timeout = float(acquire_timeout)
        self._sleep = sleep
        self.calls = 0          # requests started (for status / tests)
        self.retries = 0
        # Research priority (SPEC §7.5 of market_research)
        self.research_bucket = TokenBucket(RESEARCH_MAX_PER_MINUTE, RESEARCH_CAPACITY, monotonic=monotonic,
                                           sleep=sleep)
        self.research_timeout = RESEARCH_ACQUIRE_TIMEOUT_SECONDS
        self._research_lock = threading.Lock()
        self._research_stamps: list = []        # send times of research requests (last 60 s)
        self._research_reserved = 0             # permits taken, not sent yet (count against the cap)
        self._research_waits: Dict[int, float] = {}
        self._research_in_flight = 0            # concurrency slots held by Research
        self._normal_waiting = 0                # normal callers waiting for a shared token
        self._normal_slot_waiting = 0           # normal callers waiting for a concurrency slot
        self._local = threading.local()          # per-attempt send counter (note_send)
        self.research_calls = 0                 # research HTTP sends

    # --- research priority ------------------------------------------------------------

    def _research_purge(self, now: float) -> None:
        self._research_stamps = [t for t in self._research_stamps if now - t < RESEARCH_WINDOW_SECONDS]

    def _normal_busy(self) -> bool:
        return self._normal_waiting > 0 or self._normal_slot_waiting > 0

    def _research_step(self, data_type: str, deadline: float, wait: float) -> None:
        """One wait step of a research acquisition: yield check, deadline, bounded sleep."""
        check = _YIELD_CHECK.get()
        if check is not None and check():
            raise ResearchYield()
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise DataSourceUnavailable(
                self.source, data_type,
                f"Research waited too long for the {self.source.capitalize()} rate limit (shared with "
                f"trading) — no slot within {self.research_timeout:g} s")
        self._sleep(max(0.001, min(wait, RESEARCH_WAIT_STEP_SECONDS, remaining)))

    def _research_permit(self, data_type: str, deadline: float) -> None:
        """Research bucket + hard cap (sends + open reservations < 60 per 60 s) -> one reservation."""
        while True:
            now = self._monotonic()
            with self._research_lock:
                self._research_purge(now)
                if len(self._research_stamps) + self._research_reserved >= RESEARCH_MAX_PER_MINUTE:
                    wait = (self._research_stamps[0] + RESEARCH_WINDOW_SECONDS - now) if self._research_stamps \
                        else RESEARCH_WAIT_STEP_SECONDS
                else:
                    wait = self.research_bucket.try_acquire()
                    if wait <= 0:
                        self._research_reserved += 1
                        return
            self._research_step(data_type, deadline, wait)

    def _research_shared_token(self, data_type: str, deadline: float) -> None:
        """A shared token only above the reserve and while no normal caller waits (token or slot)."""
        while True:
            with self._research_lock:
                wait = RESEARCH_WAIT_STEP_SECONDS
                if not self._normal_busy():
                    wait = self.bucket.try_acquire_above(RESEARCH_RESERVE_TOKENS)
                    if wait <= 0:
                        return
            self._research_step(data_type, deadline, wait)

    def _research_slot(self, lim, data_type: str, deadline: float) -> None:
        """A concurrency slot: Research holds at most max(1, limit − 1) slots, takes one only while no normal
        caller waits — with limit 1 only while no normal request is active."""
        while True:
            with self._research_lock:
                if not self._normal_busy() and self._research_in_flight < max(1, lim.limit - 1) \
                        and lim.try_acquire():
                    self._research_in_flight += 1
                    return
            self._research_step(data_type, deadline, RESEARCH_WAIT_STEP_SECONDS)

    def _research_sent(self) -> None:
        with self._research_lock:
            self._research_stamps.append(self._monotonic())     # counted at the real send time
            self.research_calls += 1

    def _acquire_research(self, data_type: str, lim=None) -> None:
        """Permit, shared token, then (``lim``) a slot — all bounded by ``research_timeout``; the request is
        counted when it is sent. Raises DataSourceUnavailable (timeout) or ResearchYield."""
        start = self._monotonic()
        deadline = start + self.research_timeout
        key = threading.get_ident()
        with self._research_lock:
            self._research_waits[key] = start
        try:
            self._research_permit(data_type, deadline)
            try:
                self._research_shared_token(data_type, deadline)
                if lim is not None:
                    self._research_slot(lim, data_type, deadline)
            finally:
                with self._research_lock:
                    self._research_reserved -= 1
        finally:
            with self._research_lock:
                self._research_waits.pop(key, None)

    def note_send(self) -> None:
        """Called by a provider for EVERY HTTP request it sends inside ``call`` (alpaca-py pages one call
        into several requests). The first send is covered by the attempt's tokens; under research priority
        every further send takes its own research permit + shared token first, so the 60/min cap counts
        actual sends. Normal priority: unchanged (counted only)."""
        n = getattr(self._local, "sends", None)
        if n is None:
            return
        self._local.sends = n + 1
        if n == 0 or not self._local.research:
            return
        start = self._monotonic()
        deadline = start + self.research_timeout
        self._research_permit(self._local.data_type, deadline)
        try:
            self._research_shared_token(self._local.data_type, deadline)
        finally:
            with self._research_lock:
                self._research_reserved -= 1
        self._research_sent()

    def research_stats(self) -> dict:
        """{"used_last_minute": research requests in the last 60 s, "waiting_seconds": longest current
        research wait or None}."""
        with self._research_lock:
            now = self._monotonic()
            self._research_purge(now)
            waits = [now - t for t in self._research_waits.values()]
            return {"used_last_minute": len(self._research_stamps),
                    "waiting_seconds": round(max(waits), 1) if waits else None}

    def _acquire_shared(self, data_type: str) -> None:
        """Normal priority: a shared token (callers waiting for one are counted, so Research yields)."""
        if self.bucket.try_acquire() <= 0:
            return
        with self._research_lock:
            self._normal_waiting += 1
        try:
            ok = self.bucket.acquire(min(self.acquire_timeout, DEFAULT_ACQUIRE_TIMEOUT_SECONDS))
        finally:
            with self._research_lock:
                self._normal_waiting -= 1
        if not ok:
            raise DataSourceUnavailable(
                self.source, data_type,
                f"the local request limit ({self.bucket.rate_per_minute:g}/min) did not free a slot within "
                f"{self.acquire_timeout:g} s")

    def _acquire_window(self, data_type: str, progress) -> None:
        """Blocks until the hourly/daily windows allow a request (reporting the
        wait to ``progress``), else DataSourceUnavailable after acquire_timeout."""
        if self.limiter is None:
            return
        deadline = self._monotonic() + self.acquire_timeout
        while True:
            wait = self.limiter.try_acquire()
            if wait <= 0:
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0 or wait > remaining + PROGRESS_INTERVAL_SECONDS:
                raise DataSourceUnavailable(
                    self.source, data_type,
                    f"its request allowance is used up for now (local limits "
                    f"{', '.join(f'{n}/{sec / 3600:g} h' for n, sec in self.limiter.windows)}); "
                    f"try again in about {max(1, int(round(wait / 60.0)))} min")
            if progress is not None:
                progress({"source": self.source, "waiting": True, "wait_seconds": round(wait, 1),
                          "message": _describe_windows(self.source, self.limiter, wait),
                          "remaining": [{"limit": n, "window_seconds": sec, "left": left}
                                        for n, sec, left in self.limiter.remaining()]})
            self._sleep(min(wait, PROGRESS_INTERVAL_SECONDS, max(remaining, 0.01)))

    def _attempt(self, fn: Callable[[], object], data_type: str, progress):
        """One attempt: concurrency slot (if any) -> window/bucket tokens -> fn().
        Returns (True, result) or (False, exception)."""
        lim = self.concurrency
        research = is_research_priority()
        if research:
            # Research: window, permit, shared token, slot — never waits while holding a slot (SPEC §7.5)
            self._acquire_window(data_type, progress)
            self._acquire_research(data_type, lim)
        elif lim is not None:
            with self._research_lock:
                self._normal_slot_waiting += 1
            try:
                lim.acquire(progress)
            finally:
                with self._research_lock:
                    self._normal_slot_waiting -= 1
        try:
            if not research:
                self._acquire_window(data_type, progress)
                self._acquire_shared(data_type)
            else:
                self._research_sent()
            with self._count_lock:
                self.calls += 1
            self._local.sends, self._local.research, self._local.data_type = 0, research, data_type
            try:
                result = fn()
            except BaseException as ex:  # noqa: BLE001 — reported to the limiter, re-raised by call()
                if lim is not None:
                    if network.is_connection_error(ex):
                        lim.record_connection_error()
                    else:
                        lim.record_failure()
                raise
            finally:
                self._local.sends = None
            if lim is not None:
                lim.record_success()
            return result
        finally:
            if lim is not None:
                lim.release()
                if research:
                    with self._research_lock:
                        self._research_in_flight -= 1

    def _raise_from(self, err: UserFacingError, ex: BaseException):
        if isinstance(err, DataSourceUnavailable) and network.is_connection_error(ex):
            network.mark_connection(err)
        raise err from ex

    def call(self, fn: Callable[[], object], *, data_type: str,
             classify: Callable[[BaseException, str], object], progress=None):
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._attempt(fn, data_type, progress)
            except UserFacingError:
                raise
            except Exception as ex:   # noqa: BLE001 — classified below, never swallowed
                verdict = classify(ex, data_type)
                if isinstance(verdict, Retry):
                    if attempt >= self.max_attempts:
                        logger.warning("%s %s failed after %d attempts: %s", self.source, data_type, attempt,
                                       verdict.reason)
                        self._raise_from(DataSourceUnavailable(
                            self.source, data_type, f"{verdict.reason} — gave up after {attempt} attempts"), ex)
                    delay = self.backoff[min(attempt - 1, len(self.backoff) - 1)]
                    if verdict.retry_after is not None:
                        delay = max(delay, min(float(verdict.retry_after), MAX_RETRY_AFTER_SECONDS))
                    with self._count_lock:
                        self.retries += 1
                    logger.info("%s %s: %s — retry %d in %.1f s", self.source, data_type, verdict.reason,
                                attempt, delay)
                    self._sleep(delay)
                    continue
                if isinstance(verdict, UserFacingError):
                    self._raise_from(verdict, ex)
                self._raise_from(DataSourceUnavailable(self.source, data_type,
                                                       f"unexpected error ({type(ex).__name__})"), ex)


_gates: Dict[str, HttpGate] = {}
_gates_lock = threading.Lock()


def gate_for(source: str) -> HttpGate:
    """The process-wide gate of ``source``."""
    with _gates_lock:
        gate = _gates.get(source)
        if gate is None:
            persist = PersistedStamps(f"http_gate/{source}/requests") if source in DEFAULT_WINDOWS else None
            concurrency = network.limiter_for(source) if source in network.SOURCES else None
            gate = _gates[source] = HttpGate(source, capacity=DEFAULT_CAPACITIES.get(source, DEFAULT_CAPACITY),
                                             persist=persist, concurrency=concurrency)
        return gate


def research_stats(source: str = "alpaca") -> dict:
    """Research budget counters of the process-wide gate of ``source`` (SPEC §7.5 of market_research)."""
    return gate_for(source).research_stats()


def set_gate(source: str, gate: Optional[HttpGate]) -> None:
    """Tests: replace (or with None reset) the gate of a source."""
    with _gates_lock:
        if gate is None:
            _gates.pop(source, None)
        else:
            _gates[source] = gate
