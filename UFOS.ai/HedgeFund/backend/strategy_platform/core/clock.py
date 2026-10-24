"""Injectable clock (SPEC §1/§5): code that needs "now" takes a ``Clock`` so
tests can pin time with ``FixedClock`` instead of patching datetime.

All times are timezone-aware UTC datetimes; ``now_iso()`` gives the platform's
UTC ISO-8601 string (``timespec="seconds"``).
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Union


def to_utc(value: Union[datetime, str]) -> datetime:
    """datetime (naive = UTC) or ISO-8601 string (``Z`` allowed) -> aware UTC datetime.
    Raises ValueError/TypeError for anything else (no guessing)."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError(f"expected a datetime or ISO string, got {type(value).__name__}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """UTC ISO-8601 with seconds precision, e.g. '2026-10-02T14:00:00+00:00'."""
    return to_utc(dt).isoformat(timespec="seconds")


class Clock:
    """The real clock. Subclass / replace in tests."""

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return iso(self.now_utc())


class FixedClock(Clock):
    """A clock that only moves when told to (tests)."""

    def __init__(self, now: Union[datetime, str, None] = None):
        self._lock = threading.Lock()
        self._now = to_utc(now) if now is not None else datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)

    def now_utc(self) -> datetime:
        with self._lock:
            return self._now

    def set(self, now: Union[datetime, str]) -> None:
        with self._lock:
            self._now = to_utc(now)

    def advance(self, seconds: float = 0.0, *, minutes: float = 0.0, hours: float = 0.0,
                days: float = 0.0) -> datetime:
        with self._lock:
            self._now = self._now + timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
            return self._now


SYSTEM_CLOCK = Clock()


def resolve(clock: Optional[Clock]) -> Clock:
    """``clock`` or the system clock (for optional constructor arguments)."""
    return clock if clock is not None else SYSTEM_CLOCK
