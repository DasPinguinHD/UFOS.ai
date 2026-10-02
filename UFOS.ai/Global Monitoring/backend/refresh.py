"""Stale-while-revalidate wake-ups for the collectors (2026-09-25).

Requests (GET /events, a new WebSocket client) are always answered from
memory right away. If a source's last poll is older than its minimum
interval, request_refresh() wakes that collector early instead of waiting
for its regular poll interval: each collector ends its cycle with
``await wait_next_cycle(source, poll_seconds)`` instead of a plain sleep,
and that wait returns as soon as the source's asyncio.Event is set.

Minimum intervals (config.REFRESH_MIN_SECONDS) protect the upstream
services: a refresh is only requested when the last attempt (success or
failure) is at least that old, so reloading the page doesn't hammer a feed
— and OpenSky's anonymous quota is never exceeded (its minimum equals its
poll interval).

The timestamps live in memory (note_attempt/note_success, called by
main.py's report_status); main.py seeds them from storage.source_state at
startup. Pure stdlib + asyncio.
"""
import asyncio
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_events: Dict[str, asyncio.Event] = {}
_last_attempt: Dict[str, float] = {}
_last_success: Dict[str, float] = {}


def _event(source: str) -> asyncio.Event:
    ev = _events.get(source)
    if ev is None:
        ev = asyncio.Event()
        _events[source] = ev
    return ev


def note_attempt(source: str, ts: Optional[float] = None) -> None:
    _last_attempt[source] = time.time() if ts is None else ts


def note_success(source: str, ts: Optional[float] = None) -> None:
    stamp = time.time() if ts is None else ts
    _last_success[source] = stamp
    _last_attempt[source] = max(_last_attempt.get(source, 0.0), stamp)


def last_success(source: str) -> Optional[float]:
    return _last_success.get(source)


def is_stale(source: str, min_interval_s: float, now: Optional[float] = None) -> bool:
    """True if the source hasn't been polled (successfully or not) for at
    least min_interval_s. A source never tried in this process is not stale
    — its collector is doing its first poll right now."""
    last = _last_attempt.get(source)
    if last is None:
        return False
    now = time.time() if now is None else now
    return (now - last) >= min_interval_s


def request_refresh(source: str, min_interval_s: float, now: Optional[float] = None) -> bool:
    """Wakes the source's collector early if its data is stale. Returns True
    if a wake-up was requested (or is already pending)."""
    ev = _event(source)
    if ev.is_set():
        return True
    if not is_stale(source, min_interval_s, now):
        return False
    logger.info("Refresh requested for %s (last poll older than %ss)", source, min_interval_s)
    ev.set()
    return True


async def wait_next_cycle(source: str, poll_seconds: float) -> bool:
    """Sleeps up to poll_seconds; returns early (True) when a refresh was
    requested for source. A request made while the collector was polling is
    dropped here on purpose — the poll that just finished already refreshed
    the data."""
    ev = _event(source)
    ev.clear()
    try:
        await asyncio.wait_for(ev.wait(), timeout=poll_seconds)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        ev.clear()


def reset() -> None:
    """Tests only."""
    _events.clear()
    _last_attempt.clear()
    _last_success.clear()
