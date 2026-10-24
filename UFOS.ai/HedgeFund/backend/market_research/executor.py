"""The Research thread pool (SPEC §5.1).

ALL blocking Market Research work (SQLite, aggregation, Alpaca REST calls that
may wait in the shared http_gate) runs here — never on asyncio's default
executor (``asyncio.to_thread`` / ``run_in_executor(None, …)``), which the
paper engine uses for its heartbeat check and the kill switch
(``fund/engine.py``). A Research request stuck in the gate can therefore
occupy at most ``RESEARCH_EXECUTOR_WORKERS`` Research threads and never a
thread the engine needs. ``tests/test_mr_shell.py`` asserts that no module of
this package uses the default executor.

The calling context (``contextvars``, e.g. the http_gate research priority
from MR2) is copied into the worker thread.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from strategy_platform.core.errors import UserFacingError

from . import limits

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_executor: Optional[ThreadPoolExecutor] = None


def executor() -> ThreadPoolExecutor:
    """The process-wide Research pool (created on first use, again after shutdown())."""
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=limits.RESEARCH_EXECUTOR_WORKERS,
                                           thread_name_prefix="research")
        return _executor


async def run_blocking(fn, *args, **kwargs):
    """Runs ``fn(*args, **kwargs)`` in the Research pool (context copied) and awaits it."""
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, fn, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(executor(), call)


# --- the thumbnail pool (review M2, MR6) ---------------------------------------------------------------
# News thumbnail downloads (third-party hosts, up to NEWS_IMAGE_TOTAL_SECONDS each) run here — never on the
# Research pool above, so slow image hosts cannot starve bars / news / alert writes. Bounded: at most
# IMAGE_QUEUE_MAX jobs running + waiting; more → PoolBusy (the route answers 503).

class PoolBusy(UserFacingError):
    def __init__(self):
        super().__init__("Thumbnail loading is busy — try again shortly.")


_image_executor: Optional[ThreadPoolExecutor] = None
_image_pending = 0


def image_executor() -> ThreadPoolExecutor:
    global _image_executor
    with _lock:
        if _image_executor is None:
            _image_executor = ThreadPoolExecutor(max_workers=limits.IMAGE_EXECUTOR_WORKERS,
                                                 thread_name_prefix="research-img")
        return _image_executor


async def run_image(fn, *args, **kwargs):
    """Runs ``fn`` in the thumbnail pool; PoolBusy when IMAGE_QUEUE_MAX jobs are already running/waiting."""
    global _image_pending
    with _lock:
        if _image_pending >= limits.IMAGE_QUEUE_MAX:
            raise PoolBusy()
        _image_pending += 1
    try:
        ctx = contextvars.copy_context()
        call = functools.partial(ctx.run, fn, *args, **kwargs)
        return await asyncio.get_running_loop().run_in_executor(image_executor(), call)
    finally:
        with _lock:
            _image_pending -= 1


def shutdown() -> None:
    """Stops accepting work; queued items are cancelled, running ones finish on their own (never waits)."""
    global _executor
    global _image_executor
    with _lock:
        ex, _executor = _executor, None
        img, _image_executor = _image_executor, None
    if img is not None:
        try:
            img.shutdown(wait=False, cancel_futures=True)
        except Exception:   # noqa: BLE001 — shutdown must go on
            logger.exception("Research thumbnail executor shutdown failed")
    if ex is not None:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:   # noqa: BLE001 — shutdown must go on
            logger.exception("Research executor shutdown failed")
