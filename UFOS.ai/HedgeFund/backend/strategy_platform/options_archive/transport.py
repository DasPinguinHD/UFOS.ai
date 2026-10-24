"""GET-only HTTP for the Options Structure layer (SP §21.1 / §21.4 / §21.9) — shared by the OS1 archive and the OS0
check tool ``tools/verify_alpaca_options.py``.

- Allow-list of (method, host, path): only the read endpoints of §21.1; anything else is refused BEFORE sending.
  https only, default port; the Alpaca paper host comes from ``live_guard.paper_endpoint`` (never a live host).
- Redirects are never followed (a 3xx comes back as that status); answers > ``MAX_BODY_BYTES`` stop (not truncated);
  TLS through ``strategy_platform/core/tls.py`` (verification always on).
- ``Http``: one request at a time; an optional own rate gate (sliding 60 s window — at most N requests in ANY 60 s);
  per-endpoint timeouts; ONE bounded retry after a timeout / connection reset (and, if enabled, an HTTP 5xx) — every
  attempt counted, each retry recorded in ``retries``; the second failure stops (``Stop``); HTTP 429 → ``RateLimited``
  at once (never retried); network reasons are SANITIZED (``sanitized_reason``: root-cause type + text with every URL,
  query and key=… removed — a FRED URL carries the key in its query).

Part of UFOS.ai, a proof-of-concept portfolio project; data collection only.
"""
from __future__ import annotations

import collections
import json
import re
import time
from typing import Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

from strategy_platform.execution.brokers import live_guard

TRADING = live_guard.paper_endpoint("alpaca_paper")          # https://paper-api.alpaca.markets (paper only)
DATA = "https://data.alpaca.markets"
FRED = "https://api.stlouisfed.org"
ALLOWED = (
    ("GET", urlparse(TRADING).hostname, r"/v2/options/contracts"),
    ("GET", urlparse(TRADING).hostname, r"/v2/clock"),
    ("GET", "data.alpaca.markets", r"/v1beta1/options/snapshots/(SPY|QQQ)"),
    ("GET", "data.alpaca.markets", r"/v1beta1/options/bars"),
    ("GET", "data.alpaca.markets", r"/v2/stocks/(SPY|QQQ)/trades/latest"),
    ("GET", "api.stlouisfed.org", r"/fred/series"),
    ("GET", "api.stlouisfed.org", r"/fred/series/observations"),
)
MAX_BODY_BYTES = 20_000_000
DEFAULT_TIMEOUT_SECONDS = 20.0


class Refused(Exception):
    """A request outside the allow-list — never sent."""


class RateLimited(Exception):
    """HTTP 429: the caller stops (no retry)."""


class Stop(Exception):
    """The run cannot continue safely (network after the retry, oversized answer, keys rejected …)."""

    def __init__(self, message: str, code: int = 3):
        super().__init__(message)
        self.code = code


def allowed(method: str, url: str) -> bool:
    u = urlparse(url)
    try:
        port = u.port
    except ValueError:
        return False
    if u.scheme != "https" or u.username or u.password or port not in (None, 443):
        return False
    return any(method == m and (u.hostname or "") == h and re.fullmatch(p, u.path or "") for m, h, p in ALLOWED)


def where(url: str) -> str:
    """host + path of a URL — never its query (a FRED URL carries the key)."""
    u = urlparse(url)
    return f"{u.hostname}{u.path}"


import urllib.error as _ue  # noqa: E402
import urllib.request as _ur  # noqa: E402


class NoRedirect(_ur.HTTPRedirectHandler):
    """Redirects are never followed: a 3xx answer comes back as that status (not 200)."""

    def redirect_request(self, *a, **kw):
        return None


def build_opener(context=None):
    handlers = [NoRedirect()]
    if context is not None:
        handlers.insert(0, _ur.HTTPSHandler(context=context))
    return _ur.build_opener(*handlers)


def fetch_with(opener, method: str, url: str, headers: Dict[str, str], timeout: float, max_bytes: int = None):
    """(status, headers, body) — a body larger than ``max_bytes`` raises ``Stop`` (never truncated)."""
    limit = MAX_BODY_BYTES if max_bytes is None else max_bytes
    req = _ur.Request(url, method=method, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as r:
            status, hdrs = r.status, dict(r.headers)
            body = r.read(limit + 1)
    except _ue.HTTPError as ex:
        status, hdrs = ex.code, dict(ex.headers or {})
        body = ex.read(limit + 1) if ex.fp else b""
    if len(body) > limit:
        raise Stop(f"an answer from {where(url)} was larger than {limit // 1_000_000} MB — refused (not truncated)")
    return status, hdrs, body


def urllib_transport(method: str, url: str, headers: Dict[str, str], timeout: float):
    """GET over urllib with the backend's verifying TLS context; redirects are NOT followed."""
    from strategy_platform.core import tls
    return fetch_with(build_opener(tls.client_context()), method, url, headers, timeout)


REASON_MAX = 200
_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I)
_QUERY_RE = re.compile(r"\?\S*")
_KEYLIKE_RE = re.compile(r"(api[_-]?key|apikey|key|token|secret)\s*[=:]\s*\S+", re.I)
_RETRYABLE = (TimeoutError, ConnectionResetError, ConnectionAbortedError)


def _root_cause(ex: BaseException) -> BaseException:
    reason = getattr(ex, "reason", None)            # urllib.error.URLError wraps the socket / SSL error
    return reason if isinstance(reason, BaseException) else ex


def sanitized_reason(ex: BaseException) -> str:
    """``Type: message`` for URLError / OSError (the root cause's type, e.g. gaierror, SSLCertVerificationError,
    ConnectionRefusedError, TimeoutError) with every URL, query string and key=… redacted, ≤ REASON_MAX characters;
    the type only for anything else (its text could carry anything)."""
    if not isinstance(ex, OSError):                 # URLError and the socket / SSL errors are OSErrors
        return type(ex).__name__
    root = _root_cause(ex)
    text = str(root)
    text = _URL_RE.sub("[url]", text)
    text = _QUERY_RE.sub("", text)
    text = _KEYLIKE_RE.sub("[redacted]", text)
    text = " ".join(text.split())
    name = type(root).__name__
    return (f"{name}: {text}" if text and text != name else name)[:REASON_MAX]


def retryable(ex: BaseException) -> bool:
    import socket
    root = _root_cause(ex)
    return isinstance(root, _RETRYABLE + (socket.timeout,)) or type(root).__name__ == "RemoteDisconnected"


class RateGate:
    """At most ``per_minute`` requests in ANY 60 s window (sliding window over the request times)."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, window: float = 60.0):
        self.per_minute, self.clock, self.sleep, self.window = int(per_minute), clock, sleep, window
        self.stamps: Deque[float] = collections.deque()
        self.waited = 0.0

    def acquire(self) -> None:
        while True:
            now = self.clock()
            while self.stamps and now - self.stamps[0] >= self.window:
                self.stamps.popleft()
            if len(self.stamps) < self.per_minute:
                self.stamps.append(now)
                return
            wait = self.window - (now - self.stamps[0]) + 0.001
            self.waited += wait
            self.sleep(wait)


class Http:
    """One request at a time through the allow-list; counts requests (EVERY attempt) and latency per label. A timeout /
    connection reset (and with ``retry_on_5xx`` an HTTP 5xx) is retried ONCE — counted, listed in ``retries`` as
    ``{endpoint, path, reason}``; the second failure stops. ``timeouts``: a dict by label or a callable
    ``(path, label) -> seconds``. ``progress``: optional per-page / retry lines (the OS0 tool prints them)."""

    def __init__(self, transport=None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, *, gate: Optional[RateGate] = None, retries: int = 1,
                 retry_sleep: float = 2.0, retry_on_5xx: bool = False, timeouts=None,
                 default_timeout: float = DEFAULT_TIMEOUT_SECONDS, page_sleep: float = 0.5, max_pages: int = 40,
                 progress: Optional[Callable[[str], None]] = None, wall: Optional[Callable[[], float]] = None):
        self.transport = transport or urllib_transport
        self.wall = wall                    # epoch seconds; with it, every answer's Date header → ``last_skew_ms``
        self.last_skew_ms: Optional[int] = None
        self.sleep, self.clock = sleep, clock
        self.gate, self.max_retries, self.retry_sleep, self.retry_on_5xx = gate, int(retries), retry_sleep, retry_on_5xx
        self.timeouts, self.default_timeout = timeouts, default_timeout
        self.page_sleep, self.max_pages, self.progress = page_sleep, max_pages, progress
        self.count: Dict[str, int] = {}
        self.latency_ms: Dict[str, List[int]] = {}
        self.retries: List[dict] = []

    def timeout_for(self, path: str, label: str) -> float:
        if callable(self.timeouts):
            return float(self.timeouts(path))
        return float((self.timeouts or {}).get(label, self.default_timeout))

    def get(self, base: str, path: str, params: Optional[dict] = None, headers: Optional[dict] = None,
            label: str = "") -> Tuple[int, object]:
        url = base + path + ("?" + urlencode({k: v for k, v in (params or {}).items() if v is not None})
                             if params else "")
        if not allowed("GET", url):
            raise Refused(f"refused before sending: GET {where(url)}")
        key = label or path
        timeout = self.timeout_for(path, key)
        attempt = 0
        while True:
            attempt += 1
            if self.gate is not None:
                self.gate.acquire()
            t0 = self.clock()
            self.count[key] = self.count.get(key, 0) + 1          # every attempt counts in the budget
            try:
                status, hdrs, body = self.transport("GET", url, dict(headers or {}), timeout)
            except Stop:
                raise
            except Exception as ex:  # noqa: BLE001 — sanitized reason only (a URL in the text may carry the FRED key)
                why = sanitized_reason(ex)
                if attempt <= self.max_retries and retryable(ex):
                    self._retry(key, url, why)
                    continue
                raise Stop(f"network error on {where(url)}: {why}" + (" (after 1 retry)" if attempt > 1 else "")) \
                    from None
            if self.retry_on_5xx and isinstance(status, int) and 500 <= status <= 599 and attempt <= self.max_retries:
                self._retry(key, url, f"HTTP {status}")
                continue
            break
        self.latency_ms.setdefault(key, []).append(int((self.clock() - t0) * 1000))
        if self.wall is not None:
            self._skew(hdrs)
        if status == 429:
            raise RateLimited(f"HTTP 429 (rate limit) on {urlparse(url).path} — stopped; wait a minute and run again")
        try:
            data = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            data = None
        return status, data

    def _skew(self, hdrs) -> None:
        """Server ``Date`` − this PC's clock (ms; 1 s resolution + latency) — the archive stops on a wrong clock."""
        try:
            raw = next((v for k, v in dict(hdrs or {}).items() if str(k).lower() == "date"), None)
            if raw:
                from email.utils import parsedate_to_datetime
                self.last_skew_ms = int(parsedate_to_datetime(str(raw)).timestamp() * 1000 - self.wall() * 1000)
        except (TypeError, ValueError, IndexError, OverflowError):
            pass                                   # an unreadable Date header measures nothing (skew stays unknown)

    def _retry(self, key: str, url: str, why: str) -> None:
        self.retries.append({"endpoint": key, "path": urlparse(url).path, "reason": why})
        if self.progress:
            self.progress(f"  retrying {where(url)} once in {self.retry_sleep:g} s ({why})")
        self.sleep(self.retry_sleep)

    def pages(self, base: str, path: str, params: dict, headers: dict, list_key: str, label: str,
              on_page: Optional[Callable[[object], None]] = None):
        """All pages (next_page_token) → (items merged, pages, status of the last request, capped?). ``on_page``
        sees every page's raw payload (e.g. to stamp the receive time)."""
        items = {} if list_key in ("snapshots", "bars") else []
        token, n, status, capped = None, 0, 0, False
        while True:
            p = dict(params, page_token=token) if token else dict(params)
            status, data = self.get(base, path, p, headers, label)
            n += 1
            if status != 200 or not isinstance(data, dict):
                return items, n, status, capped
            if on_page is not None:
                on_page(data)
            part = data.get(list_key)
            if isinstance(items, dict) and isinstance(part, dict):
                for k, v in part.items():
                    if isinstance(items.get(k), list) and isinstance(v, list):
                        items[k].extend(v)
                    else:
                        items[k] = v
            elif isinstance(items, list) and isinstance(part, list):
                items.extend(part)
            token = data.get("next_page_token")
            if self.progress:
                got = sum(len(v) if isinstance(v, list) else 1 for v in items.values()) if isinstance(items, dict) \
                    else len(items)
                self.progress(f"  {label} {params.get('underlying_symbols') or path.rsplit('/', 1)[-1]}: page {n} — "
                              f"{got} rows so far" + ("" if token else " (last page)"))
            if not token:
                return items, n, status, capped
            if n >= self.max_pages:
                return items, n, status, True
            self.sleep(self.page_sleep)
