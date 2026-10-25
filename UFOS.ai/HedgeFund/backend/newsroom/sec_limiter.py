"""One process-wide limiter for every request to sec.gov / data.sec.gov (SEC fair-access policy, < 10 req/s).

The live Form 4 collector, the Disclosures daily job (issuer SIC) and the 2-year backfill all reserve their next
request slot here, so their SUM never exceeds one request per ``interval``. The interval is the user setting
``sec_edgar_min_request_interval_seconds`` (data/settings.json, default 0.5 s); while the backfill's EDGAR steps run
it is lowered to 0.2 s (5 req/s, decision 2026-10-24 §11.4) via ``override(0.2)`` and returns to the setting after.

Thread-safe (``wait`` for worker threads) and asyncio-friendly (``wait_async`` sleeps without blocking the loop).

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

BACKFILL_INTERVAL_SECONDS = 0.2
MIN_INTERVAL_SECONDS = 0.1          # never faster than 10 req/s, whatever is configured


def _configured() -> float:
    from config import SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS
    return float(SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS)


class SecLimiter:
    def __init__(self, interval_fn: Callable[[], float] = _configured, clock=time.monotonic):
        self._interval_fn, self._clock = interval_fn, clock
        self._lock = threading.Lock()
        self._next = 0.0
        self._override: Optional[float] = None

    def interval(self) -> float:
        value = self._override if self._override is not None else self._interval_fn()
        return max(MIN_INTERVAL_SECONDS, float(value))

    def reserve(self) -> float:
        """Books the next request slot; returns how long the caller has to wait before sending."""
        with self._lock:
            now = self._clock()
            at = max(now, self._next)
            self._next = at + self.interval()
            return at - now

    def wait(self) -> None:
        delay = self.reserve()
        if delay > 0:
            time.sleep(delay)

    async def wait_async(self) -> None:
        delay = self.reserve()
        if delay > 0:
            await asyncio.sleep(delay)

    @contextmanager
    def override(self, seconds: float):
        """Temporarily use ``seconds`` (the backfill's 0.2 s); restored afterwards even on errors."""
        with self._lock:
            previous, self._override = self._override, float(seconds)
        try:
            yield self
        finally:
            with self._lock:
                self._override = previous

    @property
    def overridden(self) -> bool:
        return self._override is not None


LIMITER = SecLimiter()
