"""Early-refresh signalling for polled data sources (stale-while-revalidate, 2026-09-25).

A poller waits for its next cycle with ``await wait_next_cycle(source, seconds)``
instead of ``asyncio.sleep``; anyone who notices that the source's cached data is
stale (e.g. a WebSocket client connects while the last market snapshot is old)
calls ``request_refresh(source)`` to wake the poller early. Callers never block:
they are served the cached data immediately and get fresh data with the next
broadcast. One asyncio.Event per source; use only from the event loop thread.
"""
import asyncio

_events: "dict[str, asyncio.Event]" = {}


def _event(source: str) -> asyncio.Event:
    ev = _events.get(source)
    if ev is None:
        ev = _events[source] = asyncio.Event()
    return ev


def request_refresh(source: str) -> None:
    """Ask the poller of `source` to run its next cycle now (no-op if it is already running)."""
    _event(source).set()


async def wait_next_cycle(source: str, seconds: float) -> bool:
    """Sleep up to `seconds`; return True if woken early by request_refresh()."""
    ev = _event(source)
    try:
        await asyncio.wait_for(ev.wait(), timeout=max(0.0, float(seconds)))
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        ev.clear()
