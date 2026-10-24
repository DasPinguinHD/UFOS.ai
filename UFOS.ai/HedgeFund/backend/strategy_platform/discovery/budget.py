"""Loop AI budget (SPEC §16.6, key ``loop``): own monthly budget, daily cap and per-run cap, separate from the
fund's, the explanations' and the ideation budget. Checked BEFORE every loop call (all three limits, with the
estimates of calls in flight reserved under one lock), failed attempted calls charged (``core.llm
.charge_failed_call``), a failed spend write pauses the loop until the backend restarts (fail closed — never
read as 0). Duck-types the budget object ``research.studio._call`` / ``topic_guard.check`` expect
(``check(estimate, reserve)``, ``release``, ``add``, ``fields``, ``run_spent``).

The central AI budget window (SPEC §15 B) reads/writes this budget through ``descriptor()`` / ``apply()``
(shape documented in SPEC §16.12).
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Optional

from ..storage import db

logger = logging.getLogger(__name__)

KEY = "loop"
LABEL = "Hedge Fund · Discovery loop"
# setting defaults (SPEC §16.6 — chosen while the user was away 2026-10-17, "default — user confirm"); not
# data fallbacks: an unset meta key means "the user has not changed the default"
DEFAULT_MONTHLY_USD = 10.0
DEFAULT_DAILY_USD = 2.0
DEFAULT_RUN_CAP_USD = 1.0
MAX_USD = 200.0
LIMIT_KEYS = ("monthly_usd", "daily_usd", "per_run_usd")

OFF_MESSAGE = "The discovery loop's AI is switched off (monthly budget is 0) — raise it in the AI budget settings."
DAY_OFF_MESSAGE = "The discovery loop's AI is switched off (daily cap is 0) — raise it in the AI budget settings."
RUN_OFF_MESSAGE = "The discovery loop's AI is switched off (per-run cap is 0) — raise it in the AI budget settings."
MONTH_REACHED_MESSAGE = "Monthly loop AI budget reached — raise it in the AI budget settings, then resume the loop."
DAY_REACHED_MESSAGE = ("Daily loop AI cap reached — resume the loop tomorrow (UTC) or raise the cap in the AI "
                       "budget settings.")
RUN_REACHED_MESSAGE = "Per-run AI cap of this loop reached — the loop stops creating new variants."
SPEND_UNREADABLE_MESSAGE = ("The loop AI spend could not be read — the loop is paused until it can be read again "
                            "(see backend.log).")
SPEND_UNRECORDED_MESSAGE = ("Loop AI spend could not be recorded — the discovery loop is paused until the backend "
                            "restarts.")
STOP_MESSAGES = (MONTH_REACHED_MESSAGE, DAY_REACHED_MESSAGE, OFF_MESSAGE, DAY_OFF_MESSAGE, SPEND_UNREADABLE_MESSAGE,
                 SPEND_UNRECORDED_MESSAGE)        # -> paused_budget (the run cap ends the loop early instead)

_reserve_lock = threading.Lock()
_reserved = 0.0
_spend_unrecorded = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SettingsInvalid(Exception):
    """A stored loop setting is unreadable (-> 503, set it again)."""


def _value(raw, default: float, what: str) -> float:
    if raw is None:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError, OverflowError):
        v = float("nan")
    if not math.isfinite(v) or not 0 <= v <= MAX_USD:
        raise SettingsInvalid(f"The stored loop AI {what} is invalid — set it again in the AI budget settings.")
    return v


def limits() -> dict:
    return {"monthly_usd": _value(db.meta_get(db.LOOP_MONTHLY_KEY), DEFAULT_MONTHLY_USD, "monthly budget"),
            "daily_usd": _value(db.meta_get(db.LOOP_DAILY_KEY), DEFAULT_DAILY_USD, "daily cap"),
            "per_run_usd": _value(db.meta_get(db.LOOP_RUN_CAP_KEY), DEFAULT_RUN_CAP_USD, "per-run cap")}


def paused_reason() -> Optional[str]:
    return SPEND_UNRECORDED_MESSAGE if _spend_unrecorded else None


def autonomous() -> bool:
    raw = db.meta_get(db.LOOP_AUTONOMOUS_KEY)
    if raw is None:
        return False                       # default off (finalists are proposed for the user's approval)
    if raw not in ("0", "1"):
        raise SettingsInvalid("The stored 'fully autonomous' loop setting is invalid — set it again in Settings.")
    return raw == "1"


def descriptor(now: Optional[datetime] = None) -> dict:
    """The loop budget for the central AI budget window (SPEC §16.12)."""
    now = now or utc_now()
    month, day = now.strftime("%Y-%m"), now.date().isoformat()
    lim = limits()
    sm, sd, _ = db.loop_spend_get(month, day)
    return {"key": KEY, "module": "hedgefund", "label": "Discovery loop", "cost_log_label": LABEL,
            "features": ["sp-loop", "sp-loop-diagnosis", "sp-loop-seed", "sp-studio-guard"],
            "limits": [
                {"key": "daily_usd", "label": "Daily cap (USD)", "value": lim["daily_usd"],
                 "default": DEFAULT_DAILY_USD, "min": 0.0, "max": MAX_USD, "period": "day"},
                {"key": "monthly_usd", "label": "Monthly budget (USD)", "value": lim["monthly_usd"],
                 "default": DEFAULT_MONTHLY_USD, "min": 0.0, "max": MAX_USD, "period": "month"},
                {"key": "per_run_usd", "label": "Per loop run (USD)", "value": lim["per_run_usd"],
                 "default": DEFAULT_RUN_CAP_USD, "min": 0.0, "max": MAX_USD, "period": "run"}],
            "spent": {"today_usd": round(sd, 6), "month_usd": round(sm, 6)}, "day": day, "month": month,
            "enabled": all(v > 0 for v in lim.values()), "zero_means": "off", "paused_reason": paused_reason()}


def check_limits(body, *, partial: bool) -> dict:
    """{field: message} for {monthly_usd, daily_usd, per_run_usd} (all required unless ``partial``)."""
    if not isinstance(body, dict):
        return {"_": "expected a JSON object {" + ", ".join(LIMIT_KEYS) + "}"}
    errors = {str(k): "unknown field" for k in body if k not in LIMIT_KEYS}
    for k in LIMIT_KEYS:
        if k not in body:
            if not partial:
                errors[k] = "required"
            continue
        v = body[k]
        try:
            ok = (not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(float(v))
                  and 0 <= float(v) <= MAX_USD)
        except (OverflowError, ValueError):
            ok = False
        if not ok:
            errors[k] = f"a number 0..{MAX_USD:g} (0 = off)"
    if partial and not errors and not any(k in body for k in LIMIT_KEYS):
        errors["_"] = "at least one of " + ", ".join(LIMIT_KEYS)
    return errors


def apply(values: dict, actor: str = "user") -> dict:
    """Stores the given limits (validated by ``check_limits(…, partial=True)``) -> the new descriptor.
    Audited ``loop_budget_changed {old, new}``."""
    try:
        old = limits()
    except SettingsInvalid:
        old = None                       # repair path: invalid stored values are replaced
    keymap = {"monthly_usd": db.LOOP_MONTHLY_KEY, "daily_usd": db.LOOP_DAILY_KEY, "per_run_usd": db.LOOP_RUN_CAP_KEY}
    new = dict(old or {})
    for k, mk in keymap.items():
        if k in values:
            db.meta_set(mk, repr(float(values[k])))
            new[k] = float(values[k])
    db.add_audit(actor, "loop_budget_changed", {"old": old, "new": new})
    return descriptor()


class LoopBudget:
    """Month, day AND per-run limits of one loop run."""

    def __init__(self, loop_id: str, now: Optional[datetime] = None):
        self.loop_id = loop_id
        self._period(now)
        lim = limits()
        self.monthly, self.daily, self.run_cap = lim["monthly_usd"], lim["daily_usd"], lim["per_run_usd"]
        self.limit_usd = min(self.monthly, self.daily, self.run_cap)
        self.unreadable = False
        self.run_spent = 0.0              # booked by THIS budget object (one orchestrator session)
        self.last_block: Optional[str] = None

    def _period(self, now: Optional[datetime] = None) -> None:
        """The CURRENT UTC month/day — re-read on every check and booking (review M2: a loop running over
        midnight checks and books the new day)."""
        now = now or utc_now()
        self.month, self.day = now.strftime("%Y-%m"), now.date().isoformat()

    def spent(self):
        """(month, day, run) as stored for the current period. A booking that failed to reach the store
        pauses the loop (``_spend_unrecorded``), so stored values are complete while calls are allowed."""
        self._period()
        try:
            sm, sd, sr = db.loop_spend_get(self.month, self.day, self.loop_id)
            self.unreadable = False
            return sm, sd, sr
        except Exception:     # noqa: BLE001
            self.unreadable = True
            logger.error("loop budget: spend unreadable — treating the budget as exhausted (fail closed)")
            return None

    def check(self, estimate: float, reserve: bool = False) -> Optional[str]:
        global _reserved
        with _reserve_lock:
            msg = self._check(estimate + _reserved)
            if msg is None and reserve:
                _reserved += estimate
            self.last_block = msg
            return msg

    def release(self, estimate: float) -> None:
        global _reserved
        with _reserve_lock:
            _reserved = max(0.0, _reserved - estimate)

    def _check(self, estimate: float) -> Optional[str]:
        if _spend_unrecorded:
            return SPEND_UNRECORDED_MESSAGE
        try:                                       # limits changed in the budget window apply immediately
            lim = limits()
            self.monthly, self.daily, self.run_cap = lim["monthly_usd"], lim["daily_usd"], lim["per_run_usd"]
            self.limit_usd = min(self.monthly, self.daily, self.run_cap)
        except SettingsInvalid as ex:
            return str(ex)
        except Exception:     # noqa: BLE001 — unreadable settings: fail closed
            return SPEND_UNREADABLE_MESSAGE
        if self.monthly <= 0:
            return OFF_MESSAGE
        if self.daily <= 0:
            return DAY_OFF_MESSAGE
        if self.run_cap <= 0:
            return RUN_OFF_MESSAGE
        s = self.spent()
        if s is None:
            return SPEND_UNREADABLE_MESSAGE
        sm, sd, sr = s
        if sm + estimate > self.monthly:
            return MONTH_REACHED_MESSAGE
        if sd + estimate > self.daily:
            return DAY_REACHED_MESSAGE
        if sr + estimate > self.run_cap:
            return RUN_REACHED_MESSAGE
        return None

    def fields(self, estimate: float) -> dict:
        s = None if self.unreadable else self.spent()
        return {"estimate_usd": round(estimate, 6), "monthly_budget_usd": self.monthly, "daily_cap_usd": self.daily,
                "per_run_cap_usd": self.run_cap,
                "spent_month_usd": None if s is None else round(s[0], 6),
                "spent_today_usd": None if s is None else round(s[1], 6),
                "spent_run_usd": None if s is None else round(s[2], 6)}

    def add(self, usd) -> float:
        global _spend_unrecorded
        try:
            usd = max(0.0, float(usd or 0.0))
        except (TypeError, ValueError):
            usd = 0.0
        self.run_spent += usd
        self._period()
        try:
            db.loop_spend_add(self.month, self.day, self.loop_id, usd)
        except Exception:     # noqa: BLE001 — fail closed until restart (as §11.6 / §13.5)
            _spend_unrecorded = True
            logger.error("loop AI spend of %.6f USD could not be recorded — discovery loop paused", usd, exc_info=True)
            try:
                db.add_audit("system", "loop_spend_unrecorded", {"loop_id": self.loop_id, "usd": round(usd, 6),
                                                                 "reason": SPEND_UNRECORDED_MESSAGE})
            except Exception:     # noqa: BLE001
                pass
        return self.run_spent


def run_spent(loop_id: str, now: Optional[datetime] = None) -> Optional[float]:
    now = now or utc_now()
    try:
        return round(db.loop_spend_get(now.strftime("%Y-%m"), now.date().isoformat(), loop_id)[2], 6)
    except Exception:     # noqa: BLE001 — shown as unknown (null), never as 0
        return None


# --- central AI budget window (UFOS.ai/docs/ai-budgets-spec.md §4.4, schema 1 entries) ---------------------------

ENTRY_KEYS = {"loop": ("daily_usd", "day", "Discovery loop · daily cap", DEFAULT_DAILY_USD),
              "loop_month": ("monthly_usd", "month", "Discovery loop · monthly budget", DEFAULT_MONTHLY_USD),
              "loop_run": ("per_run_usd", "run", "Discovery loop · per loop run", DEFAULT_RUN_CAP_USD)}
COST_LOG = [{"module": "HedgeFund", "feature": f} for f in ("sp-loop", "sp-loop-diagnosis", "sp-loop-seed")]


def _active_loop_id() -> Optional[str]:
    try:
        from . import loop
        with loop._lock:
            for lid, f in loop._active.items():
                if f.thread is not None and f.thread.is_alive():
                    return lid
    except Exception:     # noqa: BLE001
        return None
    return None


def ai_budget_entries(now: Optional[datetime] = None) -> list:
    """Entries for ``GET /ai/budgets`` (schema 1, §4.2): ``loop`` (day), ``loop_month`` (month), ``loop_run``
    (per loop run; spent = the running loop's spend, 0 when none runs). Raises on unreadable storage (the
    aggregator turns that into one ``status: error`` entry)."""
    now = now or utc_now()
    month, day = now.strftime("%Y-%m"), now.date().isoformat()
    lim = limits()
    active = _active_loop_id()
    sm, sd, sr = db.loop_spend_get(month, day, active)
    spent = {"loop": sd, "loop_month": sm, "loop_run": sr if active else 0.0}
    paused = paused_reason()
    out = []
    for key, (lk, period, label, default) in ENTRY_KEYS.items():
        limit = lim[lk]
        sp = spent[key]
        status, message = "ok", None
        if paused:
            status, message = "paused", paused
        elif limit <= 0:
            status, message = "blocked", {"loop": DAY_OFF_MESSAGE, "loop_month": OFF_MESSAGE,
                                          "loop_run": RUN_OFF_MESSAGE}[key]
        elif sp >= limit and key != "loop_run":
            status, message = "blocked", DAY_REACHED_MESSAGE if key == "loop" else MONTH_REACHED_MESSAGE
        elif key == "loop_run" and active and sp >= limit:
            status, message = "blocked", RUN_REACHED_MESSAGE
        out.append({"key": key, "label": label, "group": "Strategy Platform", "period": period,
                    "period_id": {"day": day, "month": month, "run": active}[period],
                    "limit_usd": round(limit, 6), "default_usd": default, "min": 0.0, "max": MAX_USD, "step": 0.1,
                    "spent_usd": round(sp, 6), "remaining_usd": round(max(0.0, limit - sp), 6),
                    "enabled": limit > 0, "status": status, "message": message, "editable": True,
                    "applies": "immediately",
                    "notes": {"loop": "Checked before every discovery-loop AI call (diagnosis, refinement, topic check).",
                              "loop_month": "All discovery-loop AI calls of the UTC month.",
                              "loop_run": ("Per loop run: when reached, the loop stops creating new variants and "
                                           "finishes with what it has. spent = the running loop (0 when none runs).")
                              }[key],
                    "cost_log": list(COST_LOG)})
    return out


def ai_budget_put(key: str, limit_usd: float, actor: str = "user") -> None:
    """``PUT /ai/budgets`` for a loop key: ValueError("<field message>") -> 400; stores like PUT
    /v2/settings/loop (audited ``loop_budget_changed``)."""
    if key not in ENTRY_KEYS:
        raise ValueError("unknown budget key")
    errors = check_limits({ENTRY_KEYS[key][0]: limit_usd}, partial=True)
    if errors:
        raise ValueError(f"a number 0..{MAX_USD:g} (0 = off)")
    apply({ENTRY_KEYS[key][0]: float(limit_usd)}, actor)
