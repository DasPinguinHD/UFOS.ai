"""Retirement of the Council of AIs AND the old engine (SP §22.15; user decision 2026-11-06, contract
``UFOS.ai/docs/tasks/RETIRE_COUNCIL_TASK.md``). Reversible: nothing is deleted, no balance / ledger / position / order
history is touched, ``council.switchover`` stays ``done`` (the heartbeat, ``kill_all``, ``/fund/*``, the OMS and the
WPF branch on it — it is NOT the retirement marker).

Marker: hedgefund.db meta key ``retired.council_and_old_engine`` = ``{retired: bool, at, actor, reason, audit_id}``.
Absent → not retired. Unreadable (storage error, invalid JSON, no boolean ``retired``) → ``RetirementUnreadable``;
the callers fail closed (no Council cycle, no old engine at start-up, write routes refused, Council orders blocked) —
no fallback, no default.

``retire`` (POST /v2/council/retire): audit row first (refused when it cannot be written) → Council orders OFF +
working Council orders cancelled (``control.council_orders_off("retired")``) → the Council task stopped (unfinished
runs closed as interrupted) → marker written. ``unretire`` (POST /v2/council/unretire): audit row first → Council
orders forced OFF (the user turns them on) → marker cleared (``retired: false``, kept as a record) → the Council task
started. The old engine comes back only through the existing revert, after un-retiring. Both are idempotent and
refused in sandbox mode.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional, Tuple

from strategy_platform.storage import db

logger = logging.getLogger(__name__)

META_RETIRED = "retired.council_and_old_engine"
CODE = "retired"
REASON_MAX = 500
ALERT_KIND = "council_retirement_unreadable"
STOP_REASON = "Council retired"


class RetirementUnreadable(Exception):
    """The marker exists but cannot be read / understood (callers fail closed)."""


class RetireError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def payload(self) -> dict:
        return dict({"error": self.message, "code": self.code}, **self.extra)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state() -> dict:
    """{retired, at, actor, reason, audit_id}; raises RetirementUnreadable (never guesses)."""
    try:
        raw = db.meta_get(META_RETIRED)
    except Exception as ex:          # noqa: BLE001 — storage unavailable
        raise RetirementUnreadable(f"the retirement marker could not be read ({type(ex).__name__})") from ex
    if raw is None:
        return {"retired": False, "at": None, "actor": None, "reason": None, "audit_id": None}
    try:
        d = json.loads(raw)
    except (TypeError, ValueError) as ex:
        raise RetirementUnreadable("the retirement marker is not valid JSON") from ex
    if not isinstance(d, dict) or not isinstance(d.get("retired"), bool):
        raise RetirementUnreadable("the retirement marker has no retired flag")
    return {"retired": d["retired"], "at": d.get("at"), "actor": d.get("actor"), "reason": d.get("reason"),
            "audit_id": d.get("audit_id")}


def is_retired() -> bool:
    """True / False; raises RetirementUnreadable."""
    return state()["retired"]


def is_retired_if_stored() -> bool:
    """Like ``is_retired`` for the READERS (Overview, AI budgets, /fund/*): False when no platform database is
    configured at all (no marker can exist — the same rule as ``fund.api.switch_state``); raises
    RetirementUnreadable when the database is configured but the marker cannot be read."""
    if db.db_path() is None:
        return False
    return is_retired()


def blocked() -> bool:
    """Fail-closed gate for the Council's AI cycles and orders: retired OR unreadable → True."""
    try:
        return is_retired()
    except RetirementUnreadable:
        logger.error("retirement marker unreadable — Council cycles / orders blocked (fail closed)")
        return True


def status() -> Tuple[str, Optional[str]]:
    """("active" | "retired" | "unreadable", error text or None) — never raises."""
    try:
        return ("retired" if is_retired() else "active"), None
    except RetirementUnreadable as ex:
        return "unreadable", str(ex)


def read_fields() -> dict:
    """What the Council read routes carry: ``{retired: true, retired_at, reason}`` while retired, ``{retired: false}``
    otherwise, ``{retired: null, retired_error}`` when the marker is unreadable."""
    try:
        st = state()
    except RetirementUnreadable as ex:
        return {"retired": None, "retired_error": str(ex)}
    if st["retired"]:
        return {"retired": True, "retired_at": st["at"], "reason": st["reason"]}
    return {"retired": False}


def gone_message(at: Optional[str]) -> str:
    day = (at or "")[:10] or "an unknown date"
    return f"Retired by the user on {day} — un-retire first"


def write_refusal() -> Optional[Tuple[int, dict]]:
    """(status, payload) for a Council / old-engine WRITE route while retired (410) or while the marker is unreadable
    (503), else None."""
    try:
        st = state()
    except RetirementUnreadable as ex:
        return 503, {"error": f"The retirement state is unreadable ({ex}) — nothing was changed.",
                     "code": "retirement_unreadable"}
    if st["retired"]:
        return 410, {"error": gone_message(st["at"]), "code": CODE, "retired_at": st["at"]}
    return None


def _write(retired: bool, actor: str, reason: Optional[str], audit_id: Optional[int]) -> dict:
    value = {"retired": bool(retired), "at": _now_iso(), "actor": actor,
             "reason": (reason or None) and str(reason)[:REASON_MAX], "audit_id": audit_id}
    db.meta_set(META_RETIRED, json.dumps(value, ensure_ascii=False, sort_keys=True))
    return value


def _sandbox() -> bool:
    try:
        import sandbox_profile
        return bool(sandbox_profile.is_sandbox())
    except Exception:          # noqa: BLE001 — fail closed
        return True


def _switchover() -> str:
    from . import store
    try:
        return store.switchover()
    except Exception:          # noqa: BLE001
        return "unreadable"


_op_lock = threading.Lock()


class _Operation:
    """One retire / un-retire at a time in this process; a second request gets 409 instead of waiting."""

    def __enter__(self):
        if not _op_lock.acquire(blocking=False):
            raise RetireError(409, "busy", "A retire / un-retire is already running.")
        return self

    def __exit__(self, *exc):
        _op_lock.release()
        return False


async def retire(reason: str, actor: str = "user", svc=None) -> dict:
    """Retire the Council + the old engine (see the module docstring). Raises RetireError."""
    import asyncio
    from strategy_platform.execution import control
    from . import store
    with _Operation():
        if _sandbox():
            raise RetireError(409, "sandbox", "Sandbox mode — retiring is refused.")
        try:
            current = await asyncio.to_thread(state)
        except RetirementUnreadable:
            current = None                     # retiring is the safe direction: it repairs an unreadable marker
        if current is not None and current["retired"]:
            if svc is not None:                # idempotent: make sure nothing runs, no new audit row
                await svc.stop(STOP_REASON)
            return {"retired": True, "already": True, "retired_at": current["at"], "reason": current["reason"],
                    "audit_id": current["audit_id"], "orders_enabled": store.orders_enabled(), "cancelled_orders": 0,
                    "task_stopped": True}
        sw = await asyncio.to_thread(_switchover)
        if sw != "done":
            raise RetireError(409, "not_switched_over", "Retiring needs the finished Council switch-over (state: "
                                                        f"{sw}) — the old engine would keep running.")
        try:
            aid = await asyncio.to_thread(db.add_audit, actor, "council_retire", {"reason": reason or None})
        except Exception as ex:      # noqa: BLE001 — audit-first
            raise RetireError(503, "audit_unavailable", "audit log unavailable — the action was not carried out") \
                from ex
        n = await asyncio.to_thread(control.council_orders_off, "retired", actor)
        stopped = False
        if svc is not None:
            await svc.stop(STOP_REASON)        # a cycle in flight is recorded "cancelled (Council retired)"
            stopped = True
            try:
                await asyncio.to_thread(store.sweep_unfinished, svc.now())
            except Exception:      # noqa: BLE001 — shown as "running" until the next start's sweep
                logger.exception("retire: unfinished Council runs not swept")
        try:
            value = await asyncio.to_thread(_write, True, actor, reason, aid)
        except Exception as ex:      # noqa: BLE001 — said honestly: stopped now, but not persisted
            logger.exception("retire: marker not written")
            raise RetireError(503, "marker_unwritten",
                              "The Council was stopped and its orders switched off, but the retirement could not be "
                              "stored — it would start again at the next backend start. Try again.",
                              audit_id=aid, cancelled_orders=int(n or 0)) from ex
        logger.warning("Council of AIs + old engine RETIRED by %s (audit %s): %s", actor, aid, reason or "-")
        return {"retired": True, "already": False, "retired_at": value["at"], "reason": value["reason"],
                "audit_id": aid, "orders_enabled": store.orders_enabled(), "cancelled_orders": int(n or 0),
                "task_stopped": stopped}


async def unretire(reason: str, actor: str = "user", svc=None) -> dict:
    """Un-retire: the Council task runs again, its orders stay OFF. Raises RetireError."""
    import asyncio
    from strategy_platform.execution import control
    from . import store
    with _Operation():
        if _sandbox():
            raise RetireError(409, "sandbox", "Sandbox mode — un-retiring is refused.")
        try:
            current = await asyncio.to_thread(state)
        except RetirementUnreadable as ex:
            raise RetireError(503, "retirement_unreadable", f"The retirement state is unreadable ({ex}) — nothing was "
                                                            "changed (retire again to repair it).") from ex
        if not current["retired"]:
            return {"retired": False, "already": True, "orders_enabled": store.orders_enabled(),
                    "task_started": bool(svc is not None and svc.task_running())}
        try:
            aid = await asyncio.to_thread(db.add_audit, actor, "council_unretire", {"reason": reason or None})
        except Exception as ex:      # noqa: BLE001 — audit-first
            raise RetireError(503, "audit_unavailable", "audit log unavailable — the action was not carried out") \
                from ex
        await asyncio.to_thread(control.council_orders_off, "unretired", actor)     # stays OFF; the user turns it on
        try:
            await asyncio.to_thread(_write, False, actor, reason, aid)
        except Exception as ex:      # noqa: BLE001
            logger.exception("unretire: marker not cleared")
            raise RetireError(503, "marker_unwritten", "The retirement could not be cleared (database unavailable) — "
                                                       "the Council stays retired.", audit_id=aid) from ex
        started, start_error = False, None
        sw = await asyncio.to_thread(_switchover)
        if svc is None:
            start_error = "the Council service is not built in this backend"
        elif sw != "done":
            start_error = f"the Council is not switched over (state: {sw})"
        else:
            try:
                svc.start()
                started = True
            except Exception as ex:  # noqa: BLE001
                logger.exception("unretire: Council task not started")
                start_error = f"{type(ex).__name__}"
        logger.warning("Council of AIs UN-RETIRED by %s (audit %s); orders OFF; task started: %s", actor, aid, started)
        return {"retired": False, "already": False, "audit_id": aid, "orders_enabled": store.orders_enabled(),
                "task_started": started, "task_error": start_error}


def startup_check() -> str:
    """main.py at start-up: "active" | "retired" | "unreadable". Unreadable → a critical alert (fail closed: neither
    the Council task nor the old engine starts)."""
    st, err = status()
    if st == "unreadable":
        logger.critical("Retirement marker unreadable at start (%s) — neither the Council task nor the old engine "
                        "starts", err)
        try:
            from strategy_platform.monitoring import alerts
            alerts.emit(ALERT_KIND, "Council / old engine: retirement state unreadable",
                        f"The retirement marker ({META_RETIRED}) could not be read: {err}. Neither the Council task "
                        "nor the old engine was started. Retire again (Trading › Retired) to repair it.",
                        f"{ALERT_KIND}:{_now_iso()[:10]}")
        except Exception:      # noqa: BLE001 — the log line above stays
            logger.exception("retirement alert not stored")
    return st


def startup_plan(switchover_state: str, retire_state: str) -> dict:
    """main.py: what starts — ``{"old_engine": bool, "council_task": bool}``. The old engine only before the
    switch-over (none / reverted / unreadable switch-over state, as before) AND not retired; the Council task only
    after it AND not retired; an unreadable marker starts neither."""
    active = retire_state == "active"
    return {"old_engine": active and switchover_state not in ("done", "pending"),
            "council_task": active and switchover_state == "done"}
