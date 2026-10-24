"""Network settings, adaptive loading concurrency and connection-error kinds (SPEC §10.1).

Reason (user, 2026-10-09): on a weak Wi-Fi connection validations failed with
Alpaca timeouts. Concurrency changes SPEED only — never data, source or
results; it never bypasses the HTTP gate's rate limits (``http_gate``).

- **Settings** (hedgefund.db ``meta``): ``network.adaptive`` ("true"/"false",
  default true) and ``network.fixed`` (1–8, default 4).
  ``get_settings() -> {adaptive, fixed, current: {alpaca, tiingo}, min, max}``
  (+ ``stored_invalid``, ``error``, ``stored`` when a stored value is invalid)
  and ``set_settings(adaptive, fixed=None)`` (validated -> ``InvalidNetworkSettings``,
  an ``InvalidRequest`` and ``ValueError``; the caller audits; works on an
  invalid store = the repair path). A missing key is the documented default
  (configuration, not a fallback); an invalid stored value makes every data
  request raise ``StoredSettingsInvalid`` ("… save them again in Hedge Fund →
  Keys & data → Network.") until saved again — no default is used instead. Until hedgefund.db is open (tests, start-up)
  the limiters run on the documented defaults and load the stored settings
  at the first request after it is open (speed only).
- **Adaptive limiter** (``AdaptiveLimiter``, one per source in ``SOURCES``,
  ``limiter_for(source)``): at most ``limit`` requests of that source in
  flight. Adaptive (AIMD): starts at ``START_CONCURRENCY`` (4); a
  connection-type failure (``is_connection_error``) halves it (floor
  ``MIN_CONCURRENCY`` 1); ``INCREASE_AFTER_SUCCESSES`` (20) consecutive
  successful requests raise it by 1 (max ``MAX_CONCURRENCY`` 8); other
  failures (HTTP errors, auth, bad data) change nothing. Fixed mode: the
  limit is ``fixed``, constant. It sits IN FRONT of the gate: ``HttpGate.call``
  takes a slot first, then the gate's rate-limit tokens, per attempt.
- **Error kind**: a ``DataSourceUnavailable`` caused by a connection-type
  failure (timeouts, DNS, refused/unreachable/reset, TLS handshake) carries
  the instance attribute ``kind = "connection"`` (``CONNECTION``); all other
  errors have no ``kind`` attribute (``getattr(ex, "kind", None)`` is None).
  ``error_kind(ex)`` returns that value (or "connection" for a raw transport
  exception). Set by ``http_gate`` and ``registry`` via ``mark_connection``.
- **Probe** ``probe(source) -> bool``: Alpaca — ONE latest SPY trade
  (``registry.fetch("equity_quotes", "latest_trade", "SPY")``, through gate +
  limiter); Tiingo — no request (its allowance is 40/h; the next real request
  is the probe), returns True. Never more often than every
  ``PROBE_MIN_INTERVAL_SECONDS`` (30 s) per source: an earlier call sleeps
  until the interval has passed (``next_probe_in(source)`` tells how long, so
  a caller can wait cancellably). True = the request succeeded, False = it
  failed with a connection-type error; any other error (keys rejected,
  missing key, …) is raised — waiting would not fix it.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import errno
import logging
import socket
import ssl
import threading
import time as _time
import urllib.error
from typing import Callable, Dict, Optional

from ..core.errors import InvalidRequest, UserFacingError

logger = logging.getLogger(__name__)

SOURCES = ("alpaca", "tiingo")
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 8
START_CONCURRENCY = 4
INCREASE_AFTER_SUCCESSES = 20
DEFAULT_ADAPTIVE = True
DEFAULT_FIXED = 4
META_ADAPTIVE = "network.adaptive"
META_FIXED = "network.fixed"
SLOT_PROGRESS_INTERVAL_SECONDS = 5.0       # a waiting thread calls progress (cancel check) this often
PROBE_MIN_INTERVAL_SECONDS = 30.0
PROBE_SYMBOL = "SPY"
CONNECTION = "connection"

# errno values of connection failures (POSIX names + the Windows WSA codes, which Windows reports as errno)
_CONN_ERRNOS = frozenset([v for v in (getattr(errno, n, None) for n in (
    "ECONNREFUSED", "ECONNRESET", "ECONNABORTED", "ENETUNREACH", "ENETDOWN", "ENETRESET", "EHOSTUNREACH",
    "EHOSTDOWN", "ETIMEDOUT", "EPIPE")) if v is not None] +
    [10050, 10051, 10052, 10053, 10054, 10060, 10061, 10064, 10065, 11001, 11002, 11004])
# class-name markers of third-party transport errors (requests, urllib3, aiohttp, http.client, asyncio)
_NAME_MARKERS = ("Timeout", "ConnectionError", "ConnectorError", "ServerDisconnected", "NewConnectionError",
                 "NameResolution", "SSLError", "ProxyError", "RemoteDisconnected", "ChunkedEncodingError",
                 "IncompleteRead", "ClientOSError")


class InvalidNetworkSettings(InvalidRequest, ValueError):
    """set_settings() got an invalid value (400-style)."""


class StoredSettingsInvalid(UserFacingError):
    """The stored network settings cannot be read back."""


# --- connection-error classification ------------------------------------------------------------

def _has_http_status(ex: BaseException) -> bool:
    """An HTTP response arrived (any status): the connection itself worked."""
    if isinstance(ex, urllib.error.HTTPError):
        return True
    for holder in (ex, getattr(ex, "response", None)):
        if holder is None:
            continue
        try:
            v = getattr(holder, "status_code", None)
        except Exception:  # noqa: BLE001 — a property of a foreign object
            v = None
        if isinstance(v, int) and not isinstance(v, bool):
            return True
    return False


def _direct(ex: BaseException) -> bool:
    if isinstance(ex, (TimeoutError, ConnectionError, socket.gaierror, socket.herror, ssl.SSLError)):
        return True
    if isinstance(ex, OSError) and ex.errno in _CONN_ERRNOS:
        return True
    names = [c.__name__ for c in type(ex).__mro__]
    return any(m in n for n in names for m in _NAME_MARKERS)


def is_connection_error(ex: Optional[BaseException]) -> bool:
    """True for a transport failure: timeout (connect/read), DNS failure,
    connection refused/reset/aborted, network/host unreachable, TLS handshake
    (``ssl.SSLError``), proxy failure, connection broken mid-response —
    including requests/urllib3/aiohttp wrappers (by class name) and the
    ``reason`` / ``__cause__`` / exception args they wrap. An exception that
    carries an HTTP status (a response arrived) is never one; a
    ``UserFacingError`` is one only when marked ``kind == "connection"``."""
    stack = [ex]
    seen = set()
    while stack and len(seen) < 16:
        e = stack.pop(0)
        if not isinstance(e, BaseException) or id(e) in seen:
            continue
        seen.add(id(e))
        if getattr(e, "kind", None) == CONNECTION:
            return True
        if isinstance(e, UserFacingError) or _has_http_status(e):
            continue
        if _direct(e):
            return True
        stack.append(getattr(e, "reason", None))
        stack.append(e.__cause__)
        stack.extend(a for a in e.args if isinstance(a, BaseException))
    return False


def mark_connection(err: BaseException) -> BaseException:
    """Sets ``err.kind = "connection"`` and returns ``err``."""
    err.kind = CONNECTION
    return err


def error_kind(ex: Optional[BaseException]) -> Optional[str]:
    """"connection" for a marked error (attribute ``kind``) or a raw transport
    exception, else None. A ``UserFacingError`` is judged by its attribute only."""
    if ex is None:
        return None
    kind = getattr(ex, "kind", None)
    if kind is not None:
        return kind
    if isinstance(ex, UserFacingError):
        return None
    return CONNECTION if is_connection_error(ex) else None


# --- adaptive limiter -------------------------------------------------------------------------------

class AdaptiveLimiter:
    """At most ``limit`` concurrent requests of one source (thread-safe). See module docstring."""

    def __init__(self, source: str, *, adaptive: bool = DEFAULT_ADAPTIVE, fixed: int = DEFAULT_FIXED,
                 start: int = START_CONCURRENCY, minimum: int = MIN_CONCURRENCY, maximum: int = MAX_CONCURRENCY,
                 increase_after: int = INCREASE_AFTER_SUCCESSES,
                 before_acquire: Optional[Callable[[], None]] = None):
        if not 1 <= minimum <= start <= maximum or not minimum <= fixed <= maximum or increase_after < 1:
            raise ValueError("need 1 <= minimum <= start, fixed <= maximum and increase_after >= 1")
        self.source = source
        self.start = int(start)
        self.minimum = int(minimum)
        self.maximum = int(maximum)
        self.increase_after = int(increase_after)
        self._before_acquire = before_acquire
        self._cond = threading.Condition()
        self.adaptive = bool(adaptive)
        self.fixed = int(fixed)
        self._limit = self.start if self.adaptive else self.fixed
        self._streak = 0
        self.in_flight = 0
        self.peak = 0                   # highest in_flight seen (status / tests)

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    def configure(self, adaptive: bool, fixed: int) -> None:
        """Switching to adaptive restarts at ``start``; fixed mode = ``fixed``."""
        with self._cond:
            if adaptive:
                if not self.adaptive:
                    self._limit = self.start
                    self._streak = 0
            else:
                self._limit = int(fixed)
                self._streak = 0
            self.adaptive = bool(adaptive)
            self.fixed = int(fixed)
            self._cond.notify_all()

    def _take(self) -> bool:
        if self.in_flight < self._limit:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            return True
        return False

    def acquire(self, progress: Optional[Callable[[dict], None]] = None) -> None:
        """Blocks until a slot is free. With ``progress`` it is called every
        ``SLOT_PROGRESS_INTERVAL_SECONDS`` while waiting (an event without
        "message"; it may raise to cancel). Waiting is bounded by the
        requests in flight (each has a timeout and a bounded gate wait)."""
        if self._before_acquire is not None:
            self._before_acquire()
        while True:
            with self._cond:
                if self._take():
                    return
                self._cond.wait(None if progress is None else SLOT_PROGRESS_INTERVAL_SECONDS)
                if self._take():
                    return
                limit = self._limit
            if progress is not None:
                progress({"source": self.source, "waiting": True, "concurrency": limit})

    def try_acquire(self) -> bool:
        """Takes a free slot without waiting (True) or returns False (Market Research priority, its SPEC §7.5)."""
        if self._before_acquire is not None:
            self._before_acquire()
        with self._cond:
            return self._take()

    def release(self) -> None:
        with self._cond:
            if self.in_flight > 0:
                self.in_flight -= 1
            self._cond.notify()

    def record_success(self) -> None:
        with self._cond:
            if not self.adaptive:
                return
            self._streak += 1
            if self._streak >= self.increase_after:
                self._streak = 0
                if self._limit < self.maximum:
                    self._limit += 1
                    logger.info("%s: loading concurrency raised to %d", self.source, self._limit)
                    self._cond.notify_all()

    def record_connection_error(self) -> None:
        with self._cond:
            if not self.adaptive:
                return
            self._streak = 0
            new = max(self.minimum, self._limit // 2)
            if new != self._limit:
                logger.info("%s: connection problem — loading concurrency lowered %d -> %d", self.source,
                            self._limit, new)
            self._limit = new

    def record_failure(self) -> None:
        """Any other failure (HTTP error, auth, bad data): no change."""


# --- settings ---------------------------------------------------------------------------------------

_lock = threading.RLock()
_limiters: Dict[str, AdaptiveLimiter] = {}
_settings = {"adaptive": DEFAULT_ADAPTIVE, "fixed": DEFAULT_FIXED}
_loaded = False
_store_override = None


def _store():
    if _store_override is not None:
        return _store_override
    from ..storage import db
    return db


def _validate(adaptive, fixed) -> dict:
    if not isinstance(adaptive, bool):
        raise InvalidNetworkSettings("adaptive must be true or false.")
    if isinstance(fixed, bool) or not isinstance(fixed, int) or not MIN_CONCURRENCY <= fixed <= MAX_CONCURRENCY:
        raise InvalidNetworkSettings(f"fixed must be a whole number from {MIN_CONCURRENCY} to {MAX_CONCURRENCY}.")
    return {"adaptive": adaptive, "fixed": fixed}


STORED_INVALID_MESSAGE = ("The stored network settings are invalid — save them again in Hedge Fund → Keys & data → "
                          "Network.")


def _describe_raw(value: Optional[str]) -> Optional[str]:
    """A stored meta value for display: printable characters only, at most 40."""
    if value is None:
        return None
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return text[:40] + ("…" if len(text) > 40 else "")


def _stored(store) -> dict:
    """{adaptive, fixed (each None when its stored value is invalid), invalid, raw}."""
    a = store.meta_get(META_ADAPTIVE)
    f = store.meta_get(META_FIXED)
    adaptive = DEFAULT_ADAPTIVE if a is None else {"true": True, "false": False}.get(a)
    fixed = DEFAULT_FIXED
    if f is not None:
        try:
            fixed = int(f)
        except (TypeError, ValueError):
            fixed = None
        if fixed is not None and not MIN_CONCURRENCY <= fixed <= MAX_CONCURRENCY:
            fixed = None
    return {"adaptive": adaptive, "fixed": fixed, "invalid": adaptive is None or fixed is None,
            "raw": {META_ADAPTIVE: _describe_raw(a), META_FIXED: _describe_raw(f)}}


def _read(store) -> dict:
    """The stored settings; StoredSettingsInvalid when a stored value is invalid
    (data requests then stop with that message — no default is used instead)."""
    st = _stored(store)
    if st["invalid"]:
        raise StoredSettingsInvalid(STORED_INVALID_MESSAGE,
                                    detail=f"{META_ADAPTIVE}={st['raw'][META_ADAPTIVE]!r} "
                                           f"{META_FIXED}={st['raw'][META_FIXED]!r}")
    return {"adaptive": st["adaptive"], "fixed": st["fixed"]}


def _apply(settings: dict) -> None:
    global _settings
    with _lock:
        _settings = dict(settings)
        for lim in _limiters.values():
            lim.configure(settings["adaptive"], settings["fixed"])


def ensure_loaded() -> None:
    """Loads the stored settings once hedgefund.db is open (no-op afterwards)."""
    global _loaded
    if _loaded:
        return
    store = _store()
    is_open = getattr(store, "is_open", None)
    if callable(is_open) and not is_open():
        return                          # documented defaults until the DB is open (speed only)
    with _lock:
        if _loaded:
            return
        _apply(_read(store))
        _loaded = True


def limiter_for(source: str) -> AdaptiveLimiter:
    """The process-wide limiter of ``source`` (one of SOURCES)."""
    if source not in SOURCES:
        raise ValueError(f"no concurrency limiter for source {source!r}")
    with _lock:
        lim = _limiters.get(source)
        if lim is None:
            lim = _limiters[source] = AdaptiveLimiter(source, adaptive=_settings["adaptive"],
                                                      fixed=_settings["fixed"], before_acquire=ensure_loaded)
        return lim


def _describe() -> dict:
    with _lock:
        return {"adaptive": _settings["adaptive"], "fixed": _settings["fixed"],
                "current": {s: limiter_for(s).limit for s in SOURCES},
                "min": MIN_CONCURRENCY, "max": MAX_CONCURRENCY}


def get_settings() -> dict:
    """{adaptive, fixed, current: {alpaca, tiingo}, min, max} — read from
    hedgefund.db (StorageUnavailable when it cannot be read). When a stored
    value is invalid it does NOT raise (so the UI can show it and PUT can
    repair it): the invalid field is None and three fields are added —
    ``stored_invalid: True``, ``error`` (STORED_INVALID_MESSAGE) and
    ``stored`` {"network.adaptive", "network.fixed"} (the raw stored values,
    printable, <= 40 chars). Data requests keep failing with
    ``StoredSettingsInvalid`` until the settings are saved again."""
    global _loaded
    st = _stored(_store())
    with _lock:
        if st["invalid"]:
            _loaded = False                 # requests re-read -> StoredSettingsInvalid (no silent default)
            out = _describe()
            out.update({"adaptive": st["adaptive"], "fixed": st["fixed"], "stored_invalid": True,
                        "error": STORED_INVALID_MESSAGE, "stored": st["raw"]})
            return out
        _apply({"adaptive": st["adaptive"], "fixed": st["fixed"]})
        _loaded = True
        return _describe()


def set_settings(adaptive, fixed=None) -> dict:
    """Validates ONLY the new values, stores and applies them; returns
    ``get_settings()``'s shape. Works when the stored values are invalid (the
    repair path). ``fixed`` None keeps the stored value — refused when that is
    invalid. The caller (API route) writes the audit entry."""
    global _loaded
    store = _store()
    if fixed is None:
        if isinstance(adaptive, bool):
            fixed = _stored(store)["fixed"]
            if fixed is None:
                raise InvalidNetworkSettings("fixed is required — the stored value is invalid.")
    settings = _validate(adaptive, fixed)
    store.meta_set(META_ADAPTIVE, "true" if settings["adaptive"] else "false")
    store.meta_set(META_FIXED, str(settings["fixed"]))
    with _lock:
        _apply(settings)
        _loaded = True
        return _describe()


def pool_size() -> int:
    """Worker threads for a data loader: MAX_CONCURRENCY in adaptive mode
    (the per-source limiters bound the requests in flight), else ``fixed``."""
    ensure_loaded()
    with _lock:
        return MAX_CONCURRENCY if _settings["adaptive"] else int(_settings["fixed"])


def reset(store=None) -> None:
    """Tests: forget limiters and loaded settings; ``store`` replaces storage.db."""
    global _settings, _loaded, _store_override, _default_prober
    with _lock:
        _limiters.clear()
        _settings = {"adaptive": DEFAULT_ADAPTIVE, "fixed": DEFAULT_FIXED}
        _loaded = False
        _store_override = store
    with _prober_lock:
        _default_prober = None


# --- connectivity probe -----------------------------------------------------------------------------

def _default_probe_fetch(source: str) -> None:
    from . import registry
    registry.fetch("equity_quotes", "latest_trade", PROBE_SYMBOL)


class Prober:
    """``probe(source)`` with at most one probe per ``interval`` seconds per source."""

    def __init__(self, fetch: Optional[Callable[[str], object]] = None, *,
                 interval: float = PROBE_MIN_INTERVAL_SECONDS,
                 monotonic: Callable[[], float] = _time.monotonic, sleep: Callable[[float], None] = _time.sleep):
        self._fetch = fetch if fetch is not None else _default_probe_fetch
        self.interval = float(interval)
        self._monotonic = monotonic
        self._sleep = sleep
        self._locks = {s: threading.Lock() for s in SOURCES}
        self._last: Dict[str, float] = {}
        self.requests = 0

    def next_probe_in(self, source: str) -> float:
        """Seconds until ``probe(source)`` may run without sleeping (0 = now)."""
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        last = self._last.get(source)
        return 0.0 if last is None else max(0.0, last + self.interval - self._monotonic())

    def probe(self, source: str) -> bool:
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        with self._locks[source]:
            wait = self.next_probe_in(source)
            while wait > 0:
                self._sleep(wait)
                wait = self.next_probe_in(source)
            self._last[source] = self._monotonic()
            if source == "tiingo":
                return True             # not probed (40 requests/h): the next real request is the probe
            self.requests += 1
            try:
                self._fetch(source)
            except Exception as ex:  # noqa: BLE001 — connection kinds -> False, everything else raised
                if error_kind(ex) == CONNECTION:
                    logger.info("probe %s: still unreachable (%s)", source, type(ex).__name__)
                    return False
                raise
            return True


_prober_lock = threading.Lock()
_default_prober: Optional[Prober] = None


def default_prober() -> Prober:
    global _default_prober
    with _prober_lock:
        if _default_prober is None:
            _default_prober = Prober()
        return _default_prober


def set_prober(prober: Optional[Prober]) -> None:
    """Tests: replace (None = reset) the process-wide prober."""
    global _default_prober
    with _prober_lock:
        _default_prober = prober


def next_probe_in(source: str) -> float:
    return default_prober().next_probe_in(source)


def probe(source: str) -> bool:
    """See the module docstring (SPEC §10.1)."""
    return default_prober().probe(source)
