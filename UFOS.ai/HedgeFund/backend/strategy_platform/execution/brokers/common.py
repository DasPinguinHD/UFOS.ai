"""Shared pieces of the real-broker adapters (SPEC §18.3, §18.6): the link state (weak
connections: adaptive backoff, visible "waiting for broker (attempt n)"), ack helpers and the
mapping of transport/HTTP failures. Part of UFOS.ai; PAPER / DEMO TRADING ONLY.
"""
from __future__ import annotations

import math
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from ...core.errors import UserFacingError
from ...data import network
from ..errors import BrokerUnavailable

BACKOFF_BASE_SECONDS = 15.0
BACKOFF_MAX_SECONDS = 300.0
AUTH_RECHECK_SECONDS = 300.0
LINK_STATES = ("idle", "ok", "waiting", "auth_failed", "keys_missing", "not_installed", "error")
ACK_STATUSES = ("accepted", "partially_filled", "filled", "cancelled", "rejected", "expired")
EPS = 1e-9


class BrokerAuthRejected(UserFacingError):
    """The broker refused the keys (HTTP 401/403 on auth) — waiting does not fix it."""


class BrokerNotInstalled(UserFacingError):
    """The broker SDK (alpaca-py / ccxt) is not installed."""


class BrokerNotRetryable(UserFacingError):
    """A broker answer that waiting will not fix (unexpected format, missing wallet …): the link goes to ``error``
    with ``retry: "manual"`` — no automatic retries, no "waiting for broker" (fix 2026-10-18, live retest)."""


class BrokerFormatError(BrokerNotRetryable):
    """The broker answered in an unexpected format — not a connection problem."""


class BrokerSetupError(BrokerNotRetryable):
    """The broker account is not set up the way the adapter needs (e.g. no multi-collateral wallet)."""


def shape_of(value) -> str:
    """A SAFE description of a response's shape for logs and messages — the type plus the top-level keys (sanitised,
    ≤ 10) or the length; never values or ids. A text says only its first character class (e.g. '<' = HTML?)."""
    import re as _re
    if isinstance(value, dict):
        keys = [_re.sub(r"[^A-Za-z0-9_]", "?", str(k))[:30] for k in list(value)[:10]]
        more = f" (+{len(value) - 10} more)" if len(value) > 10 else ""
        return f"dict, keys: {', '.join(keys) or '(none)'}{more}"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__}, length {len(value)}"
    if isinstance(value, (str, bytes, bytearray)):
        text = value.decode("utf-8", "replace") if not isinstance(value, str) else value
        first = text.lstrip()[:1]
        hint = {"<": " (HTML page?)", "": " (empty)"}.get(first, "")
        show = first if first in "{}[]<\"'" and first else ("letter/digit" if first.isalnum() else "other")
        return f"{type(value).__name__}, length {len(value)}, starts with {show!s}{hint}"
    return type(value).__name__


def utc(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def fnum(v) -> Optional[float]:
    """float or None (never NaN / inf)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def unavailable(message: str, ex: Optional[BaseException] = None, connection: Optional[bool] = None) -> BrokerUnavailable:
    err = BrokerUnavailable(message[:300], detail=(f"{type(ex).__name__}" if ex is not None else ""))
    if connection or (connection is None and ex is not None and network.is_connection_error(ex)):
        network.mark_connection(err)
    return err


class Link:
    """Connection state of ONE broker (thread-safe). ``fail_connection`` → ``waiting`` with backoff
    min(15 s · 2^(n−1), 300 s); ``ok`` resets; ``auth_failed`` re-checked every 5 min; callers that
    are not the user skip the broker while ``blocked()``."""

    def __init__(self, name: str, clock: Optional[Callable[[], datetime]] = None):
        self.name = name
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self.state = "idle"
        self.attempt = 0
        self.next_retry_at: Optional[datetime] = None
        self.last_ok_at: Optional[datetime] = None
        self.last_error: Optional[str] = None

    def now(self) -> datetime:
        return utc(self._clock())

    manual_only = False

    def ok(self) -> None:
        with self._lock:
            self.state, self.attempt, self.next_retry_at, self.last_error = "ok", 0, None, None
            self.manual_only = False
            self.last_ok_at = self.now()

    def fail(self, state: str, message: str) -> None:
        with self._lock:
            if state == "manual":        # BrokerNotRetryable: no automatic retry until a user action succeeds
                self.state, self.manual_only, self.next_retry_at = "error", True, None
                self.last_error = (message or "")[:500] or None
                return
            self.manual_only = False
            if state == "waiting":
                self.attempt += 1
                delay = min(BACKOFF_BASE_SECONDS * (2 ** (self.attempt - 1)), BACKOFF_MAX_SECONDS)
            elif state == "auth_failed":
                delay = AUTH_RECHECK_SECONDS
            else:
                delay = BACKOFF_BASE_SECONDS
            self.state = state if state in LINK_STATES else "error"
            self.next_retry_at = self.now() + timedelta(seconds=delay)
            self.last_error = (message or "")[:500] or None

    def blocked(self) -> Optional[str]:
        """A message while automatic calls must wait, else None."""
        with self._lock:
            if self.manual_only and self.state == "error":
                return self.message_locked()
            if self.next_retry_at is not None and self.now() < self.next_retry_at and \
                    self.state in ("waiting", "auth_failed", "error", "keys_missing", "not_installed"):
                return self.message_locked()
        return None

    def message_locked(self) -> str:
        nxt = self.next_retry_at.strftime("%H:%M:%S UTC") if self.next_retry_at else "soon"
        if self.state == "waiting":
            return f"Waiting for broker (attempt {self.attempt}) — next try {nxt}" + \
                (f" · {self.last_error}" if self.last_error else "")
        if self.state == "ok":
            t = self.last_ok_at.strftime("%H:%M:%S UTC") if self.last_ok_at else "?"
            return f"Connected · last answer {t}"
        if self.state == "idle":
            return "Not contacted yet"
        if self.manual_only:
            return (self.last_error or self.state) + " — not retried automatically (Test connection / Reconcile now " \
                                                    "tries again)"
        return self.last_error or self.state

    def message(self) -> str:
        with self._lock:
            return self.message_locked()

    def view(self) -> dict:
        with self._lock:
            return {"state": self.state, "attempt": self.attempt, "next_retry_at": iso(self.next_retry_at),
                    "last_ok_at": iso(self.last_ok_at), "last_error": self.last_error,
                    "message": self.message_locked(), "retry": "manual" if self.manual_only else "auto"}


def classify_failure(ex: BaseException) -> str:
    """Link state for a failure: waiting (connection / transient) | auth_failed | keys_missing |
    not_installed | error."""
    from ...core.errors import SecretMissing, SecretStoreUnavailable
    if isinstance(ex, BrokerAuthRejected):
        return "auth_failed"
    if isinstance(ex, SecretMissing):
        return "keys_missing"
    if isinstance(ex, SecretStoreUnavailable):
        return "error"
    if isinstance(ex, BrokerNotInstalled):
        return "not_installed"
    if isinstance(ex, BrokerNotRetryable):
        return "manual"
    if isinstance(ex, BrokerUnavailable):
        return "waiting"
    if network.is_connection_error(ex):
        return "waiting"
    return "error"


def rejected_ack(order: dict, reason: str, broker_status: Optional[str] = None) -> dict:
    return {"client_order_id": order.get("client_order_id"), "broker_order_id": None, "status": "rejected",
            "qty": fnum(order.get("qty")), "filled_qty": 0.0, "avg_price": None, "fees": 0.0, "notional": None,
            "reject_reason": (reason or "rejected by the broker")[:500], "ts": None, "origin": order.get("origin"),
            "symbol": order.get("symbol"), "side": order.get("side"), "legs": [], "broker_status": broker_status}


ORDER_TYPES = ("market", "limit", "stop", "stop_limit", "bracket")
TIFS = ("day", "gtc")


def validate_order_shape(order: dict, *, types: tuple, tifs: tuple) -> Optional[str]:
    """A refusal reason for an order the adapter cannot express, else None."""
    t = order.get("type") or "market"
    if t not in types:
        return f"order type {t} is not supported here (supported: {', '.join(types)})"
    tif = order.get("tif") or "day"
    if tif not in tifs:
        return f"time in force {tif} is not supported here (supported: {', '.join(tifs)})"
    if t in ("limit", "stop_limit") and not (fnum(order.get("limit_price")) or 0) > 0:
        return "a limit price is required"
    if t in ("stop", "stop_limit") and not (fnum(order.get("stop_price")) or 0) > 0:
        return "a stop price is required"
    if t == "bracket":
        tp = (order.get("take_profit") or {}).get("limit_price")
        sl = (order.get("stop_loss") or {}).get("stop_price")
        if not (fnum(tp) or 0) > 0 or not (fnum(sl) or 0) > 0:
            return "a bracket needs take_profit.limit_price and stop_loss.stop_price"
    return None


def sleep(seconds: float) -> None:      # tests replace it
    _time.sleep(seconds)
