"""``within(awaitable, timeout)`` — ``asyncio.wait_for`` without its Python ≤ 3.11 flaw.

On Python 3.10/3.11 ``asyncio.wait_for`` can swallow a cancellation when the
inner awaitable completes at the same moment (bpo-42130 / gh-86296): a hub
task would then ignore ``supervisor.stop()`` / a restart and run on. This
helper uses ``asyncio.wait`` (never swallows the caller's cancellation) and
cancels the inner task on timeout or cancel. Used for every timed wait in the
hub package.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import asyncio
from typing import Optional


async def within(aw, timeout: Optional[float]):
    """Result of ``aw``; ``asyncio.TimeoutError`` after ``timeout`` s (None = no limit)."""
    task = asyncio.ensure_future(aw)
    try:
        done, _ = await asyncio.wait({task}, timeout=None if timeout is None else max(0.0, timeout))
    except BaseException:
        task.cancel()
        raise
    if not done:
        task.cancel()
        raise asyncio.TimeoutError
    return task.result()
