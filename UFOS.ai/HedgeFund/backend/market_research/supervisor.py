"""Research supervisor: separately supervised asyncio tasks (SPEC §1.4, §5).

Every Market Research background task runs inside its own error boundary:

- an exception is logged (backend.log, with the stack), recorded as the
  task's ``last_error`` (a safe one-line text: the ``user_message`` of a
  ``UserFacingError``, else "<ExceptionType> — see backend.log") and turns
  Research "unavailable" with the reason — it NEVER propagates to main.py,
  the paper engine, the kill switch / dead-man switch, the newsroom
  collectors or Strategy Platform jobs;
- a task that returns instead of looping counts as a crash;
- restarts are bounded and visible: delays ``RESTART_DELAYS_SECONDS``;
  after ``MAX_RESTARTS`` restarts within ``RESTART_WINDOW_SECONDS`` the next
  crash marks the task "failed" (no more restarts); a run that lasted
  ``HEALTHY_AFTER_SECONDS`` clears the crash history.

``start()`` only creates tasks (sync, never raises); ``stop()`` cancels and
waits at most ``timeout`` seconds (never raises).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time as _time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Deque, Dict, List, Optional

from strategy_platform.core.errors import UserFacingError

from . import limits

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
TASK_STATES = ("pending", "running", "restarting", "failed", "stopped")
RESEARCH_STATES = ("starting", "ok", "unavailable", "stopped")
STORE_STATES = ("ok", "unavailable", "not_opened")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt is not None else None


def describe_error(ex: BaseException) -> str:
    """Safe one-line reason for the UI (no stack trace; raw exception texts can carry internals)."""
    if isinstance(ex, UserFacingError):
        return limits.clip(ex.user_message)
    return limits.clip(f"{type(ex).__name__} — see backend.log")


@dataclass(frozen=True)
class RestartPolicy:
    delays: tuple = limits.RESTART_DELAYS_SECONDS
    max_restarts: int = limits.MAX_RESTARTS
    window_seconds: float = limits.RESTART_WINDOW_SECONDS
    healthy_after_seconds: float = limits.HEALTHY_AFTER_SECONDS

    def delay(self, crash_number: int) -> float:
        """Delay before restart number ``crash_number`` (1-based)."""
        d = tuple(self.delays) or (1.0,)
        return float(d[min(max(crash_number, 1) - 1, len(d) - 1)])


@dataclass
class _Task:
    name: str
    label: str
    factory: Callable[[], Awaitable[None]]
    state: str = "pending"
    restarts: int = 0
    crashes: Deque[float] = field(default_factory=deque)
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None
    next_restart_at: Optional[datetime] = None
    handle: Optional[asyncio.Task] = None


def _default_store_status() -> dict:
    from . import store
    return store.status()


class ResearchSupervisor:
    def __init__(self, policy: Optional[RestartPolicy] = None, *,
                 store_status: Optional[Callable[[], dict]] = None,
                 monotonic: Callable[[], float] = _time.monotonic,
                 now: Callable[[], datetime] = utc_now,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.policy = policy or RestartPolicy()
        self._store_status = store_status or _default_store_status
        self._monotonic = monotonic
        self._now = now
        self._sleep = sleep
        self._tasks: Dict[str, _Task] = {}
        self._started = False
        self._stopping = False
        self._start_error: Optional[str] = None
        self._state = "starting"
        self._since = now()

    # --- registration / lifecycle ---------------------------------------------------------
    def add(self, name: str, label: str, factory: Callable[[], Awaitable[None]]) -> None:
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError("task name must match [a-z][a-z0-9_]{0,39}")
        if name in self._tasks:
            raise ValueError(f"task {name!r} already added")
        if not callable(factory):
            raise ValueError("factory must be callable")
        t = _Task(name, limits.clip(label, 80), factory)
        self._tasks[name] = t
        if self._started and not self._stopping:
            self._spawn(t)

    def start(self) -> None:
        """Creates the supervised tasks (needs the running loop). Idempotent; never raises."""
        if self._started or self._stopping:
            return
        try:
            asyncio.get_running_loop()
            self._started = True
            for t in self._tasks.values():
                self._spawn(t)
            logger.info("Market Research supervisor started (%d task(s): %s)", len(self._tasks),
                        ", ".join(self._tasks) or "-")
        except Exception as ex:   # noqa: BLE001 — Research must never break the backend start
            self._started = True
            self._start_error = limits.clip(f"Research could not start ({type(ex).__name__}) — see backend.log. "
                                            f"{limits.TRADING_UNAFFECTED}")
            logger.exception("Market Research supervisor could not start")
        self._refresh()

    async def stop(self, timeout: float = limits.SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """Cancels every task and waits at most ``timeout`` seconds. Never raises."""
        self._stopping = True
        try:
            handles = [t.handle for t in self._tasks.values() if t.handle is not None and not t.handle.done()]
            for h in handles:
                h.cancel()
            if handles:
                _done, pending = await asyncio.wait(handles, timeout=max(0.0, float(timeout)))
                if pending:
                    logger.warning("Market Research: %d task(s) did not stop within %.1f s — left behind",
                                   len(pending), timeout)
            for t in self._tasks.values():
                if t.state != "failed":
                    t.state = "stopped"
        except BaseException:   # noqa: BLE001 — shutdown must go on (also when the caller is cancelled)
            logger.exception("Market Research: stopping the tasks failed")
        from . import executor
        executor.shutdown()
        self._refresh()

    def _spawn(self, t: _Task) -> None:
        t.handle = asyncio.get_running_loop().create_task(self._run(t), name=f"research:{t.name}")
        t.handle.add_done_callback(self._done_callback(t))

    def _done_callback(self, t: _Task):
        def _cb(fut: asyncio.Future) -> None:
            try:
                if fut.cancelled():
                    return
                ex = fut.exception()
                if ex is not None:   # _run catches everything — this is a last line of defence
                    logger.error("Market Research task %s ended with an escaped error: %r", t.name, ex)
                    t.state = "failed"
                    t.last_error = describe_error(ex)
                    t.last_error_at = self._now()
                    self._refresh()
            except BaseException:   # noqa: BLE001 — a callback must never raise into the loop
                pass
        return _cb

    # --- the error boundary ---------------------------------------------------------------
    async def _run(self, t: _Task) -> None:
        while True:
            started = self._monotonic()
            t.state = "running"
            t.next_restart_at = None
            self._refresh()
            try:
                await t.factory()
                reason = "ended unexpectedly"
                logger.error("Market Research task %s returned instead of running until stopped", t.name)
            except asyncio.CancelledError:
                t.state = "stopped"
                self._refresh()
                raise
            except KeyboardInterrupt:
                # Ctrl+C of a console run reaches whatever coroutine runs at that moment — that is the
                # process's own stop signal, not a Research failure, so it is passed on (SPEC §5).
                raise
            except BaseException as ex:   # noqa: BLE001 — the boundary (incl. SystemExit from Research code)
                if self._stopping:
                    logger.info("Market Research task %s ended during shutdown (%s)", t.name, type(ex).__name__)
                    reason = None
                else:
                    logger.exception("Market Research task %s crashed", t.name)
                    reason = describe_error(ex)
            if self._stopping:            # shutting down: never restart, whatever the task did
                t.state = "stopped"
                self._refresh()
                return
            try:
                if not await self._after_crash(t, started, reason):
                    return
            except asyncio.CancelledError:
                t.state = "stopped"
                self._refresh()
                raise
            except BaseException:   # noqa: BLE001 — bookkeeping failed: stop this task, keep the rest
                logger.exception("Market Research task %s: restart bookkeeping failed", t.name)
                t.state = "failed"
                self._refresh()
                return

    async def _after_crash(self, t: _Task, started: float, reason: str) -> bool:
        """Records a crash; sleeps the restart delay. False = budget used up (task failed)."""
        now = self._monotonic()
        if now - started >= self.policy.healthy_after_seconds:
            t.crashes.clear()
        t.crashes.append(now)
        while t.crashes and now - t.crashes[0] > self.policy.window_seconds:
            t.crashes.popleft()
        t.last_error = reason
        t.last_error_at = self._now()
        if len(t.crashes) > self.policy.max_restarts:
            t.state = "failed"
            t.next_restart_at = None
            logger.error("Market Research task %s failed: %d crashes within %.0f s — no more restarts",
                         t.name, len(t.crashes), self.policy.window_seconds)
            self._refresh()
            return False
        t.restarts += 1
        delay = self.policy.delay(len(t.crashes))
        t.state = "restarting"
        t.next_restart_at = self._now() + timedelta(seconds=delay)
        self._refresh()
        await self._sleep(delay)
        return True

    # --- status ---------------------------------------------------------------------------
    def _store(self) -> dict:
        try:
            s = dict(self._store_status() or {})
        except Exception as ex:   # noqa: BLE001 — status must always answer
            logger.exception("Market Research: store status failed")
            s = {"state": "unavailable", "reason": describe_error(ex)}
        state = s.get("state") if s.get("state") in STORE_STATES else "unavailable"
        return {"state": state,
                "reason": None if state == "ok" else limits.clip(s.get("reason") or "unknown reason"),
                "file": s.get("file") or limits.DB_FILENAME,
                "schema_version": s.get("schema_version") if state == "ok" else None}

    def _task_reason(self, t: _Task) -> str:
        if t.state == "failed":
            return limits.clip(f'Research task "{t.name}" stopped after {len(t.crashes) or 1} crash(es) '
                               f"({t.last_error}) — restart the HedgeFund backend. {limits.TRADING_UNAFFECTED}")
        left = 0
        if t.next_restart_at is not None:
            left = max(0, math.ceil((t.next_restart_at - self._now()).total_seconds()))
        return limits.clip(f'Research task "{t.name}" crashed ({t.last_error}) — restarting in {left} s '
                           f"(attempt {len(t.crashes)} of {self.policy.max_restarts}). {limits.TRADING_UNAFFECTED}")

    def _compute(self, store: dict):
        if self._stopping:
            return "stopped", None
        if not self._started:
            return "starting", None
        if self._start_error:
            return "unavailable", self._start_error
        if store["state"] == "unavailable":
            return "unavailable", limits.clip(f"{store['reason']}")
        for t in self._tasks.values():
            if t.state in ("restarting", "failed"):
                return "unavailable", self._task_reason(t)
        if store["state"] == "not_opened":
            return "starting", None
        return "ok", None

    def _refresh(self, store: Optional[dict] = None) -> tuple:
        state, reason = self._compute(store if store is not None else self._store())
        if state != self._state:
            if state == "unavailable":
                logger.warning("Market Research unavailable: %s", reason)
            elif self._state == "unavailable":
                logger.info("Market Research is %s again", state)
            self._state = state
            self._since = self._now()
        return state, reason

    @property
    def state(self) -> str:
        return self._refresh()[0]

    def tasks_status(self) -> List[dict]:
        return [{"name": t.name, "label": t.label, "state": t.state, "restarts": t.restarts,
                 "max_restarts": self.policy.max_restarts, "last_error": t.last_error,
                 "last_error_at": iso(t.last_error_at), "next_restart_at": iso(t.next_restart_at)}
                for t in self._tasks.values()]

    def status(self) -> dict:
        """{"research": {state, reason, since}, "store": {...}, "tasks": [...]} (SPEC §1.2). Never raises."""
        store = self._store()
        state, reason = self._refresh(store)
        return {"research": {"state": state, "reason": reason, "since": iso(self._since)},
                "store": store, "tasks": self.tasks_status()}
