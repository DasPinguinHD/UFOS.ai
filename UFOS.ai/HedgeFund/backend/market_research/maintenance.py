"""Supervised task "store_maintenance" (SPEC §10 MR1): opens market_research.db
in a Research pool thread (executor.py), re-tries every STORE_REOPEN_SECONDS while it is
unavailable, then prunes once a day (retention SPEC §6). The store's state is
read by the supervisor through ``store.status()``; a StoreUnavailable is a
state, not a crash. Any other exception escapes to the supervisor's error
boundary (logged, Research unavailable, bounded restart).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from . import limits
from .executor import run_blocking
from . import store as default_store

logger = logging.getLogger(__name__)


async def run(path: Optional[str] = None, *, store=default_store,
              sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    while True:
        try:
            if not store.is_open():
                await run_blocking(store.init, path)
            deleted = await run_blocking(store.prune)
            if any(deleted.values()):
                logger.info("market_research.db retention: %s", deleted)
            delay = limits.MAINTENANCE_INTERVAL_SECONDS
        except store.StoreUnavailable as ex:
            logger.warning("%s — next attempt in %d s", ex.user_message, limits.STORE_REOPEN_SECONDS)
            delay = limits.STORE_REOPEN_SECONDS
        await sleep(delay)
