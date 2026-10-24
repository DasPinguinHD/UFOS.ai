"""Strategy Studio (SPEC §13.1, stage 5a): an AI "Researcher" drafts StrategySpecs in a conversation.

- One session = one strategy family: the first VALID draft creates a strategy (origin ``studio``, state
  draft, ``ai_generated: true`` + AI provenance in the envelope); later valid drafts add versions.
- Each user message -> ``core.llm.chat_json`` (feature ``sp-studio``); the answer's spec goes through the
  existing validator; invalid -> ONE automatic repair round with the validator's errors (budget-checked
  and charged like the first call); still invalid -> shown with the errors, never backtested.
- Every valid new version is backtested automatically through the existing JobManager (default range,
  ends before the holdout window). When a job is running the draft waits for the slot (persisted queue,
  FIFO, ≤ 3 waiting drafts per session, cancellable); ``on_slot_free`` (called by ``backtest.jobs``
  after every job) and every Studio request dispatch it.
- Own ideation budget (meta keys, per UTC month AND day; default 10 USD / 2 USD; 0 = off), checked
  BEFORE every call with the ``core.llm`` estimate; failed attempted calls are charged; a failed spend
  write pauses the Studio until restart (fail closed). No stand-in text, no default model.
- Operator requests are stored with the message and listed — text only, never executed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from ..backtest import explain
from ..core.errors import BudgetExceeded, UserFacingError
from ..storage import db
from . import memory, prompts, topic_guard

logger = logging.getLogger(__name__)

FEATURE = "sp-studio"
FEATURE_LABEL = "Hedge Fund · Strategy Studio"
TEMPERATURE = 0.3
MAX_TOKENS = 4000
TIMEOUT_SECONDS = 90
TIERS = ("standard", "premium")
QUEUE_MAX_PER_SESSION = 3
HISTORY_MESSAGES = 10
HISTORY_TEXT_MAX = 2000
TITLE_MAX = 120

DEFAULT_MONTHLY_BUDGET_USD = 10.0          # user decision 2026-10-17 (setting defaults, not data fallbacks)
DEFAULT_DAILY_CAP_USD = 2.0
MAX_BUDGET_USD = 200.0
OFF_MESSAGE = "AI ideation is switched off (monthly budget is 0) — raise it in Settings."
DAY_OFF_MESSAGE = "AI ideation is switched off (daily cap is 0) — raise it in Settings."
MONTH_REACHED_MESSAGE = "Monthly ideation budget reached — raise it in Settings."
DAY_REACHED_MESSAGE = "Daily ideation cap reached — try again tomorrow (UTC) or raise it in Settings."
SPEND_UNREADABLE_MESSAGE = ("The ideation spend could not be read — the Studio is paused until it can be read "
                            "again (see backend.log).")
SPEND_UNRECORDED_MESSAGE = "Ideation spend could not be recorded — the Studio is paused until the backend restarts."
BUSY_MESSAGE = "The Studio is already answering — wait until it is finished."
NOT_FOUND_MESSAGE = "Studio session not found."
QUEUE_FULL_MESSAGE = (f"Backtest queue full ({QUEUE_MAX_PER_SESSION} drafts waiting) — start this version from "
                      "the Library.")
WAITING_MESSAGE = "Waiting for the backtest slot"
OPERATOR_REQUEST_LABEL = "unverified AI suggestion"
SCOPE_NOTE = ("The Strategy Studio only helps design and refine trading strategies in the rule language. Describe "
              "a trading idea (market, signal, rebalancing) and the Studio drafts a strategy.")
AI_INTERACTION_NOTICE = ("You are interacting with an AI system. Its answers are AI-generated and not verified. "
                         "AI-generated research output for your own decision. Not a personal recommendation by a "
                         "licensed advisor; you decide and carry the risk. Backtests are hypothetical.")
# EU AI Act Art. 50(1); S10-H: + context notice (b) (core/notices.RESEARCH)
DISCARDED_TEXT_MAX = 4000
DRAFT_INVALID_NOTE = "The AI draft did not pass the validator — its text is not shown; the validator's findings are below."
AUTO_SAVE_STATES = ("idea", "draft", "backtested")      # review M1: later states need a confirmation
NOT_CONFIRMABLE_MESSAGE = "This draft does not need a confirmation."
REPAIR_TOO_LARGE_MESSAGE = "No repair round: the repair request would exceed the prompt size limit."

_busy = threading.Lock()
_dispatch_lock = threading.Lock()
_reserve_lock = threading.Lock()     # review L4: estimates of calls in flight (Studio, papers, committee)
_reserved = 0.0
_spend_unrecorded = False


class StudioError(explain.ExplainError):
    """A Studio request that cannot be served (HTTP status, code, extra payload fields)."""


class StudioBudgetBlocked(StudioError, BudgetExceeded):
    """The ideation budget blocks the call (402-style)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _audit_quiet(action: str, detail: dict) -> None:
    try:
        db.add_audit("user", action, detail)
    except Exception:     # noqa: BLE001
        logger.warning("studio: audit row %s could not be written", action, exc_info=True)


# --- settings + budget -----------------------------------------------------------------------------

def _value(raw, default: float, what: str) -> float:
    if raw is None:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError, OverflowError):
        v = float("nan")
    if not math.isfinite(v) or not 0 <= v <= MAX_BUDGET_USD:
        raise StudioError(503, "settings_invalid", f"The stored ideation {what} is invalid — set it again in Settings.")
    return v


def limits() -> tuple:
    return (_value(db.meta_get(db.IDEATION_MONTHLY_KEY), DEFAULT_MONTHLY_BUDGET_USD, "monthly budget"),
            _value(db.meta_get(db.IDEATION_DAILY_KEY), DEFAULT_DAILY_CAP_USD, "daily cap"))


def paused_reason() -> Optional[str]:
    return SPEND_UNRECORDED_MESSAGE if _spend_unrecorded else None


def get_settings(now: Optional[datetime] = None) -> dict:
    now = now or utc_now()
    month, day = now.strftime("%Y-%m"), now.date().isoformat()
    monthly, daily = limits()
    sm, sd = db.ideation_spend_get(month, day)
    return {"monthly_budget_usd": monthly, "daily_cap_usd": daily, "spent_month_usd": round(sm, 6),
            "spent_today_usd": round(sd, 6), "remaining_month_usd": round(max(0.0, monthly - sm), 6),
            "remaining_today_usd": round(max(0.0, min(daily - sd, monthly - sm)), 6), "month": month, "day": day,
            "enabled": monthly > 0 and daily > 0, "default_monthly_budget_usd": DEFAULT_MONTHLY_BUDGET_USD,
            "default_daily_cap_usd": DEFAULT_DAILY_CAP_USD, "max_monthly_budget_usd": MAX_BUDGET_USD,
            "max_daily_cap_usd": MAX_BUDGET_USD, "paused_reason": paused_reason()}


def check_settings(body) -> dict:
    keys = ("monthly_budget_usd", "daily_cap_usd")
    if not isinstance(body, dict):
        return {"_": "expected a JSON object {monthly_budget_usd, daily_cap_usd}"}
    errors = {str(k): "unknown field" for k in body if k not in keys}
    for k in keys:
        if k not in body:
            errors[k] = "required"
            continue
        v = body[k]
        try:
            ok = (not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(float(v))
                  and 0 <= float(v) <= MAX_BUDGET_USD)
        except (OverflowError, ValueError):
            ok = False
        if not ok:
            errors[k] = f"a number 0..{MAX_BUDGET_USD:g} (0 = off)"
    return errors


def put_settings(body: dict, actor: str = "user") -> dict:
    try:
        old = dict(zip(("monthly_budget_usd", "daily_cap_usd"), limits()))
    except StudioError:
        old = None                       # repair path: invalid stored values are replaced
    new = {"monthly_budget_usd": float(body["monthly_budget_usd"]), "daily_cap_usd": float(body["daily_cap_usd"])}
    db.meta_set(db.IDEATION_MONTHLY_KEY, repr(new["monthly_budget_usd"]))
    db.meta_set(db.IDEATION_DAILY_KEY, repr(new["daily_cap_usd"]))
    db.add_audit(actor, "ideation_settings_changed", {"old": old, "new": new})
    return get_settings()


class IdeationBudget:
    """Month AND day limits for one request; duck-types ``core.llm.Budget`` for ``charge_failed_call``
    (``add``). Fail closed: unreadable spend -> no call; a failed spend write pauses the Studio."""

    def __init__(self, now: Optional[datetime] = None):
        now = now or utc_now()
        self.month, self.day = now.strftime("%Y-%m"), now.date().isoformat()
        self.monthly, self.daily = limits()
        self.limit_usd = min(self.monthly, self.daily)
        self.unreadable = False
        self.run_spent = 0.0
        self._base = None               # stored (month, day) spend at the first read of this request

    def spent(self):
        """(month, day) spend: max(stored, stored at the start + what this request booked) — a booking that
        failed to reach the store still counts (review L3)."""
        try:
            sm, sd = db.ideation_spend_get(self.month, self.day)
            self.unreadable = False
            if self._base is None:
                self._base = (sm - self.run_spent, sd - self.run_spent) if self.run_spent else (sm, sd)
            return max(sm, self._base[0] + self.run_spent), max(sd, self._base[1] + self.run_spent)
        except Exception:     # noqa: BLE001
            self.unreadable = True
            logger.error("ideation budget: spend unreadable — treating the budget as exhausted (fail closed)")
            return None

    def check(self, estimate: float, reserve: bool = False) -> Optional[str]:
        """None when a call with ``estimate`` fits (counting the estimates of calls in flight), else the user
        message. ``reserve``: the estimate is held until ``release`` (one process-wide lock)."""
        global _reserved
        with _reserve_lock:
            msg = self._check(estimate + _reserved)
            if msg is None and reserve:
                _reserved += estimate
            return msg

    def release(self, estimate: float) -> None:
        global _reserved
        with _reserve_lock:
            _reserved = max(0.0, _reserved - estimate)

    def _check(self, estimate: float) -> Optional[str]:
        if _spend_unrecorded:
            return SPEND_UNRECORDED_MESSAGE
        if self.monthly <= 0:
            return OFF_MESSAGE
        if self.daily <= 0:
            return DAY_OFF_MESSAGE
        s = self.spent()
        if s is None:
            return SPEND_UNREADABLE_MESSAGE
        sm, sd = s
        if sm + estimate > self.monthly:
            return MONTH_REACHED_MESSAGE
        if sd + estimate > self.daily:
            return DAY_REACHED_MESSAGE
        return None

    def fields(self, estimate: float) -> dict:
        s = None if self.unreadable else self.spent()
        return {"estimate_usd": round(estimate, 6), "monthly_budget_usd": self.monthly, "daily_cap_usd": self.daily,
                "spent_month_usd": None if s is None else round(s[0], 6),
                "spent_today_usd": None if s is None else round(s[1], 6)}

    def add(self, usd) -> float:
        global _spend_unrecorded
        try:
            usd = max(0.0, float(usd or 0.0))
        except (TypeError, ValueError):
            usd = 0.0
        self.run_spent += usd
        try:
            db.ideation_spend_add(self.month, self.day, usd)
        except Exception:     # noqa: BLE001 — fail closed until restart (review L6 pattern, §11.6)
            _spend_unrecorded = True
            logger.error("ideation spend of %.6f USD could not be recorded — Studio paused", usd, exc_info=True)
            _audit_quiet("ideation_spend_unrecorded", {"month": self.month, "day": self.day, "usd": round(usd, 6),
                                                       "reason": SPEND_UNRECORDED_MESSAGE})
        return self.run_spent


# --- sessions ----------------------------------------------------------------------------------------

def _session_row(session_id) -> dict:
    if not isinstance(session_id, str) or not session_id.startswith("ss-") or len(session_id) > 64:
        raise StudioError(404, "not_found", NOT_FOUND_MESSAGE)
    row = db.studio_session_get(session_id)
    if row is None:
        raise StudioError(404, "not_found", NOT_FOUND_MESSAGE)
    return row


def _family(strategy_id: Optional[str]):
    if not strategy_id:
        return None, None, 0
    from ..validation import families
    row = db.strategy_get(strategy_id)
    fam = (row or {}).get("family_id")
    return row, fam, (families.trial_count(fam) if fam else 0)


def session_out(row: dict, stats: Optional[dict] = None) -> dict:
    _srow, fam, trials = _family(row.get("strategy_id"))
    if stats is None:
        msgs = db.studio_messages_list(row["id"])
        stats = {"message_count": len(msgs), "last_message_at": msgs[-1]["created_at"] if msgs else None}
    return {"id": row["id"], "title": row.get("title"), "created_at": row["created_at"],
            "strategy_id": row.get("strategy_id"), "family_id": fam, "trial_count": trials,
            "message_count": int(stats.get("message_count") or 0), "last_message_at": stats.get("last_message_at")}


def create_session(title=None) -> dict:
    sid = "ss-" + uuid.uuid4().hex[:12]
    row = db.studio_session_insert({"id": sid, "title": title}, ("user", "studio_session_created",
                                                                  {"session_id": sid, "title": title}))
    return session_out(row, {"message_count": 0, "last_message_at": None})


def list_sessions(limit: int = 50) -> dict:
    dispatch_queue()
    return {"sessions": [session_out(r, r) for r in db.studio_sessions_list(limit)],
            "ai_interaction_notice": AI_INTERACTION_NOTICE}


# --- draft backtests (persisted queue) ----------------------------------------------------------------

def _latest_events(session_id: Optional[str] = None) -> dict:
    """message_id -> (latest event row, first waiting event id or None)."""
    out = {}
    for e in db.studio_backtest_events(session_id):
        prev = out.get(e["message_id"])
        first_wait = prev[1] if prev else None
        if e["event"] == "waiting" and first_wait is None:
            first_wait = e["id"]
        out[e["message_id"]] = (e, first_wait)
    return out


def _waiting(latest: dict) -> List[dict]:
    """Waiting entries, FIFO (by their waiting event)."""
    items = [(fw, e) for e, fw in latest.values() if e["event"] == "waiting"]
    return [e for _fw, e in sorted(items, key=lambda t: t[0])]


def draft_backtest(message_id: int, latest: Optional[dict] = None) -> Optional[dict]:
    from ..backtest import jobs, metrics as metrics_mod, runcard
    latest = latest if latest is not None else _latest_events()
    item = latest.get(message_id)
    if item is None:
        return None
    e = item[0]
    out = {"message_id": message_id, "state": e["event"], "position": None, "run_id": e.get("run_id"),
           "run_status": None, "key_metrics": None, "message": e.get("note")}
    if e["event"] == "waiting":
        ids = [w["message_id"] for w in _waiting(latest)]
        out["position"] = ids.index(message_id) + 1 if message_id in ids else None
        out["message"] = WAITING_MESSAGE
    if e.get("run_id"):
        card = db.backtest_run_get(e["run_id"])
        if card is not None:
            status = card["status"]
            if status == "running" and jobs.manager().is_waiting(e["run_id"]):
                status = "waiting"
            out["run_status"] = status
            if card.get("metrics_json"):
                out["key_metrics"] = metrics_mod.summary(json.loads(card["metrics_json"]))
            if card.get("error") and status in ("failed", "cancelled"):
                out["message"] = card["error"]
    return out


def _start_or_queue(session_id: str, message_id: int, strategy_id: str, version: int) -> List[dict]:
    """Submits the draft's backtest, or queues it (≤ 3 waiting per session) -> system notes to store."""
    from ..backtest import jobs
    base = {"session_id": session_id, "message_id": message_id, "strategy_id": strategy_id, "version": version}
    latest = _latest_events(session_id)
    mgr = jobs.manager()
    if mgr.active_run_id() is None and not _waiting(_latest_events()):
        res = _submit(base)
        if res is not None:
            return res
    if len(_waiting(latest)) >= QUEUE_MAX_PER_SESSION:
        db.studio_backtest_event_insert(dict(base, event="not_queued", note=QUEUE_FULL_MESSAGE))
        return [{"text": f"Version {version}: {QUEUE_FULL_MESSAGE}"}]
    db.studio_backtest_event_insert(dict(base, event="waiting"),
                                    ("user", "studio_draft_waiting", dict(base)))
    dispatch_queue()
    return []


def _submit(entry: dict) -> Optional[List[dict]]:
    """One submit attempt -> None when the slot is busy (stays/gets queued), else system notes ([] on
    success). Records the started / start_failed event."""
    from ..backtest import jobs, runcard
    from ..lifecycle import registry as lc
    base = {k: entry[k] for k in ("session_id", "message_id", "strategy_id", "version")}
    try:
        run_id = jobs.manager().submit(entry["strategy_id"], int(entry["version"]), {})
    except jobs.JobBusy:
        return None
    except (runcard.NotFound, lc.NotFound, ValueError, UserFacingError) as ex:
        msg = ex.user_message if isinstance(ex, UserFacingError) else str(ex)[:300]
        db.studio_backtest_event_insert(dict(base, event="start_failed", note=msg),
                                        ("user", "studio_draft_start_failed", dict(base, error=msg)))
        return [{"text": f"Version {entry['version']}: the backtest could not start — {msg}"}]
    db.studio_backtest_event_insert(dict(base, event="started", run_id=run_id),
                                    ("user", "studio_draft_started", dict(base, run_id=run_id)))
    return []


_dispatch_dirty = False


def dispatch_queue() -> None:
    """Starts the oldest waiting draft when the job slot is free (FIFO over all sessions). Wake-ups are
    never lost (review L2): a caller that cannot take the lock leaves the "dirty" flag, and the holder
    runs another round after releasing it."""
    global _dispatch_dirty
    _dispatch_dirty = True
    while True:
        if not db.is_open() or not _dispatch_lock.acquire(blocking=False):
            return
        try:
            _dispatch_dirty = False
            _dispatch_once()
        finally:
            _dispatch_lock.release()
        if not _dispatch_dirty:
            return


def _dispatch_once() -> None:
    from ..backtest import jobs
    if jobs.manager().active_run_id() is not None:
        return
    for entry in _waiting(_latest_events()):
        res = _submit(entry)
        if res is None:
            return                       # busy again
        if res:                          # failed to start: note it, try the next one
            db.studio_messages_insert([{"session_id": entry["session_id"], "role": "system_note",
                                        "text": res[0]["text"]}])
            continue
        return


def on_slot_free() -> None:
    """Called by ``backtest.jobs`` after every job."""
    if db.is_open():
        dispatch_queue()


def cancel_draft(session_id: str, message_id) -> dict:
    _session_row(session_id)
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        raise StudioError(404, "not_found", "Draft not found.") from None
    with _dispatch_lock:
        latest = _latest_events(session_id)
        item = latest.get(mid)
        if item is None:
            raise StudioError(404, "not_found", "Draft not found.")
        e = item[0]
        if e["event"] != "waiting":
            raise StudioError(409, "not_waiting", "This draft is not waiting for the backtest slot.")
        base = {k: e[k] for k in ("session_id", "message_id", "strategy_id", "version")}
        db.studio_backtest_event_insert(dict(base, event="cancelled", note="Cancelled while waiting for the "
                                                                           "backtest slot."),
                                        ("user", "studio_draft_cancelled", dict(base)))
        db.studio_messages_insert([{"session_id": session_id, "role": "system_note",
                                    "text": f"Version {e['version']}: the waiting backtest was cancelled."}])
    return draft_backtest(mid)


# --- messages ----------------------------------------------------------------------------------------

def message_out(m: dict, latest: dict, saves: Optional[dict] = None) -> dict:
    d = m.get("detail") or {}
    draft = d.get("draft")
    run_id = m.get("run_id")
    spec_version = m.get("spec_version")
    if draft is not None:
        draft = {k: v for k, v in draft.items() if k != "ai_provenance"}
        for k, default in (("requires_confirmation", None), ("not_saved", False), ("not_saved_reason", None),
                           ("saved_by_confirmation", False), ("base_version", None)):
            draft.setdefault(k, default)
        saved = (saves if saves is not None else db.studio_draft_saves(m["session_id"])).get(m["id"])
        if saved is not None:
            draft.update(strategy_id=saved["strategy_id"], version=saved["version"], requires_confirmation=None,
                         saved_by_confirmation=True)
            spec_version = saved["version"]
        draft["backtest"] = draft_backtest(m["id"], latest)
        if draft["backtest"] and draft["backtest"].get("run_id"):
            run_id = draft["backtest"]["run_id"]
    prov = m.get("provenance")
    discarded = bool(d.get("discarded"))
    return {"id": m["id"], "session_id": m["session_id"], "role": m["role"], "text": "" if discarded else m["text"],
            "kind": d.get("kind") if m["role"] == "assistant" else None, "discarded": discarded,
            "discarded_reason": d.get("discarded_reason") if discarded else None,
            "topic_check": d.get("topic_check") if m["role"] == "assistant" else None,
            "created_at": m["created_at"], "spec_version": spec_version, "run_id": run_id,
            "operator_requests": m.get("operator_requests") or [], "assumptions": d.get("assumptions") or [],
            "draft": draft, "memory_used": d.get("memory_used") or [],
            "model": (prov or {}).get("model"), "model_tier": (prov or {}).get("model_tier"),
            "provenance": prov, "usage": m.get("usage"), "cost_usd": m.get("cost_usd")}


def get_session(session_id: str) -> dict:
    from ..backtest import runcard
    row = _session_row(session_id)
    _recover_link(row)
    dispatch_queue()
    msgs = db.studio_messages_list(session_id)
    latest = _latest_events()
    saves = db.studio_draft_saves(session_id)
    versions, runs = [], []
    by_version = {m.get("spec_version"): m["id"] for m in msgs if m.get("spec_version")}
    by_version.update({sv["version"]: mid for mid, sv in saves.items()})
    if row.get("strategy_id"):
        for v in db.strategy_versions_list(row["strategy_id"]):
            versions.append({"version": v["version"], "created_at": v["created_at"],
                             "content_hash": v["content_hash"], "message_id": by_version.get(v["version"])})
        for r in runcard.list_runs(row["strategy_id"], limit=200):
            runs.append({"run_id": r["id"], "version": r["version"], "kind": r.get("kind"), "status": r["status"],
                         "created_at": r["created_at"], "finished_at": r.get("finished_at"),
                         "key_metrics": r.get("key_metrics")})
    queue = [draft_backtest(e["message_id"], latest) for e in _waiting(latest) if e["session_id"] == session_id]
    out = {"session": session_out(row, {"message_count": len(msgs),
                                        "last_message_at": msgs[-1]["created_at"] if msgs else None}),
           "messages": [message_out(m, latest, saves) for m in msgs], "versions": versions, "runs": runs,
           "trial_count": None, "queue": queue, "estimate_usd": None, "budget": get_settings(),
           "ai_interaction_notice": AI_INTERACTION_NOTICE}
    out["trial_count"] = out["session"]["trial_count"]
    newest_fact = db.memory_facts_list(limit=1)
    srow = db.strategy_get(row["strategy_id"]) if row.get("strategy_id") else None
    fingerprint = (msgs[-1]["id"] if msgs else 0, row.get("strategy_id"), (srow or {}).get("current_version"),
                   tuple((r["run_id"], r["status"]) for r in runs[:3]),
                   newest_fact[0]["id"] if newest_fact else 0)
    cached = _ESTIMATES.get(session_id)
    if cached is not None and cached[0] == fingerprint:
        out["estimate_usd"] = dict(cached[1])
        return out
    try:
        ctx = _context(row)
        msgs_est = prompts.build_messages("x" * prompts.USER_TEXT_MAX, **ctx["prompt"])
        from strategy_platform.core import llm
        out["estimate_usd"] = {t: round(llm.estimate_cost(msgs_est, MAX_TOKENS, t), 6) for t in TIERS}
        _ESTIMATES[session_id] = (fingerprint, dict(out["estimate_usd"]))
    except UserFacingError as ex:
        out["estimate_unavailable"] = ex.user_message
    return out


# per-session estimate cache (review L8): recomputed only when messages / versions / runs / memory change
_ESTIMATES: dict = {}


def _context(row: dict) -> dict:
    """Current spec, last run facts (no holdout), memory block, history -> {prompt: kwargs, …}."""
    from ..backtest import runcard
    strategy_id = row.get("strategy_id")
    srow, fam, _trials = _family(strategy_id)
    current_spec, current_version, last_run = None, None, None
    if srow is not None:
        current_version = int(srow["current_version"])
        v = db.strategy_version_get(strategy_id, current_version)
        current_spec = json.loads(v["spec_json"]) if v else None
        for r in runcard.list_runs(strategy_id, status="done", limit=20):
            if r.get("kind") not in ("backtest", "validation"):
                continue                              # never holdout runs
            card = runcard.get(r["id"])
            facts = None
            if card is not None and card.get("attribution") is not None:
                facts = explain.build_context(card)["facts"]
            last_run = {"run_id": r["id"], "kind": r.get("kind"), "version": r["version"],
                        "range": {k: ((card or {}).get("range") or {}).get(k) for k in ("start", "end")},
                        "key_metrics": r.get("key_metrics"), **prompts.last_run_facts(facts)}
            break
    mem = memory.select_for_prompt(fam, current_spec)
    hist = []
    for m in db.studio_messages_list(row["id"])[-HISTORY_MESSAGES:]:
        hist.append({"role": m["role"], "text": m["text"][:HISTORY_TEXT_MAX]})
    return {"srow": srow, "family_id": fam, "current_version": current_version, "memory": mem,
            "messages": db.studio_messages_list(row["id"]),
            "prompt": {"current_spec": current_spec, "last_run": last_run, "memory_facts": mem, "history": hist}}


async def _call(messages, model: str, tier: str, budget: IdeationBudget, audit: dict, *, feature: str = FEATURE,
                feature_label: str = FEATURE_LABEL, max_tokens: int = MAX_TOKENS,
                temperature: float = TEMPERATURE):
    """One budget-checked LLM call -> (parsed, raw, used_model, usage, estimate, cost). Shared by the paper
    and committee features (§14), which pass their own feature id/label."""
    from strategy_platform.core import llm
    estimate = llm.estimate_cost(messages, max_tokens, tier)
    msg = await asyncio.to_thread(budget.check, estimate, True)
    if msg is not None:
        _audit_quiet("studio_call_blocked", dict(audit, reason=msg, estimate_usd=round(estimate, 6)))
        if msg == SPEND_UNRECORDED_MESSAGE:
            raise StudioBudgetBlocked(503, "spend_unrecorded", msg)
        raise StudioBudgetBlocked(402, "budget", msg, **budget.fields(estimate))
    try:
        parsed, raw, used, usage = await asyncio.wait_for(
            llm.chat_json(messages, model, max_tokens, feature, feature_label, temperature=temperature),
            timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        budget.release(estimate)
        ex = llm.LlmUnavailable("error", f"no answer within {TIMEOUT_SECONDS} s", attempted=True)
        await asyncio.to_thread(llm.charge_failed_call, budget, ex, estimate)
        _audit_quiet("studio_call_failed", dict(audit, reason="timeout", cost_usd=round(budget.run_spent, 6)))
        raise StudioError(504, "timeout", f"The AI did not answer within {TIMEOUT_SECONDS} s — nothing is shown "
                                          f"(charged as attempted: ${budget.run_spent:.4f}).",
                          cost_usd=round(budget.run_spent, 6))
    except llm.LlmUnavailable as ex:
        budget.release(estimate)
        await asyncio.to_thread(llm.charge_failed_call, budget, ex, estimate)
        _audit_quiet("studio_call_failed", dict(audit, reason=ex.reason, cost_usd=round(budget.run_spent, 6)))
        raise explain._llm_error(ex, budget.run_spent) from None
    except BaseException:
        budget.release(estimate)
        raise
    cost = llm.usage_cost(usage, estimate)
    try:
        await asyncio.to_thread(budget.add, cost)
    finally:
        budget.release(estimate)
    return parsed, raw, used or model, dict(usage or {}), estimate, cost


def _validated(parsed, budget: IdeationBudget, audit: dict) -> dict:
    try:
        return prompts.validate_answer(parsed)
    except ValueError as ve:
        _audit_quiet("studio_call_failed", dict(audit, reason="bad_json", detail=str(ve)[:200],
                                                cost_usd=round(budget.run_spent, 6)))
        raise StudioError(502, "bad_json", f"The AI answer did not have the required format ({ve}) — nothing is "
                                           f"shown (charged: ${budget.run_spent:.4f}).",
                          cost_usd=round(budget.run_spent, 6)) from None


def _clean_spec(spec: dict) -> dict:
    return {k: v for k, v in spec.items() if k != "provenance"}


def _validate_spec(spec: dict) -> dict:
    """The validator + the AI-draft parameter rule (click-test 2026-10-22): every parameter of an AI draft needs
    value, min, max AND step (the core validator does not require step; the robustness gate does). Missing keys
    are errors named exactly — never filled in."""
    from ..lifecycle import registry as lc
    v = lc.validate_spec(_clean_spec(spec))
    errors = list(v.get("errors") or [])
    have = {e.get("path") for e in errors}
    params = spec.get("parameters") if isinstance(spec, dict) else None
    if isinstance(params, dict):
        for name, p in sorted(params.items()):
            if isinstance(p, dict) and "step" not in p:
                path = f"$.parameters.{name}.step"
                if path not in have:
                    errors.append({"path": path, "code": "missing_key",
                                   "message": "Missing key 'step' — every parameter of a draft needs value, min, "
                                              "max and step (the robustness checks vary it)."})
    return {"ok": bool(v.get("ok")) and not errors, "errors": errors,
            "warnings": list(v.get("warnings") or [])}


async def send_message(session_id: str, text: str, tier: str) -> dict:
    """POST …/messages -> {user_message, assistant_message, notes}. Raises StudioError."""
    from strategy_platform.core import llm
    from ..lifecycle import registry as lc
    if tier not in TIERS or not isinstance(text, str):
        raise ValueError("tier/text")
    if _spend_unrecorded:
        _audit_quiet("studio_call_blocked", {"session_id": session_id, "reason": SPEND_UNRECORDED_MESSAGE})
        raise StudioBudgetBlocked(503, "spend_unrecorded", SPEND_UNRECORDED_MESSAGE)
    if not _busy.acquire(blocking=False):
        raise StudioError(409, "busy", BUSY_MESSAGE)
    try:
        row = await asyncio.to_thread(_session_row, session_id)
        await asyncio.to_thread(_recover_link, row)
        await asyncio.to_thread(dispatch_queue)
        ctx = await asyncio.to_thread(_context, row)
        messages = prompts.build_messages(text, **ctx["prompt"])
        if prompts.size(messages) > prompts.PROMPT_MAX_CHARS:
            raise StudioError(409, "prompt_too_large", f"The Studio context is too large (more than "
                                                       f"{prompts.PROMPT_MAX_CHARS:,} characters).")
        model = explain._model_id(tier)
        budget = await asyncio.to_thread(IdeationBudget)
        audit = {"session_id": session_id, "model_tier": tier, "model": model}
        p_hash = prompts.prompt_hash(messages)
        parsed, raw, used, usage, est, _cost = await _call(messages, model, tier, budget, audit)
        reason = prompts.scope_guard(parsed, _clarify_in_a_row(ctx["messages"]))
        if reason is not None:
            return await asyncio.to_thread(_store_discarded, session_id, text, raw, parsed, reason, used, tier,
                                           p_hash, usage, est, budget, audit)
        ans = _validated(parsed, budget, audit)
        calls, usages, estimates = 1, [usage], [est]
        draft = None
        if ans["spec"] is not None:
            validation = await asyncio.to_thread(_validate_spec, ans["spec"])
            draft = {"spec": _clean_spec(ans["spec"]), "valid": validation["ok"], "validation": validation,
                     "repaired": False, "repair_errors": None, "repair_skipped": None, "strategy_id": None,
                     "version": None, "unchanged": False, "requires_confirmation": None, "not_saved": False,
                     "not_saved_reason": None, "saved_by_confirmation": False,
                     "base_version": ctx["current_version"]}
            if not validation["ok"]:
                rmsgs, _trimmed = prompts.capped_repair(text, ctx["prompt"], raw, validation["errors"])
                try:
                    if rmsgs is None:
                        raise StudioBudgetBlocked(409, "prompt_too_large", REPAIR_TOO_LARGE_MESSAGE)
                    parsed2, _raw2, used, usage2, est2, _c2 = await _call(rmsgs, model, tier, budget,
                                                                         dict(audit, repair=True))
                except StudioBudgetBlocked as ex:
                    draft["repair_skipped"] = ex.user_message
                else:
                    calls += 1
                    usages.append(usage2)
                    estimates.append(est2)
                    reason2 = prompts.scope_guard(parsed2, 0)
                    if reason2 is not None or parsed2.get("kind") != "draft":
                        draft["repair_skipped"] = (f"The repair answer was discarded "
                                                   f"({reason2 or 'not a draft'}).")
                        ans2 = None
                    else:
                        ans2 = _validated(parsed2, budget, dict(audit, repair=True))
                    if ans2 is None:
                        pass
                    elif ans2["spec"] is None:
                        draft = dict(draft, spec=None, repaired=True, repair_errors=validation["errors"],
                                     valid=False)
                    else:
                        ans = ans2
                        v2 = await asyncio.to_thread(_validate_spec, ans2["spec"])
                        draft = dict(draft, spec=_clean_spec(ans2["spec"]), valid=v2["ok"], validation=v2,
                                     repaired=True, repair_errors=validation["errors"])
        # defence in depth (review H1): an invalid final draft shows only the validator card; then the
        # independent topic check sees every displayable AI text (never the user's) before anything is shown
        discard_reason, pre_notes = None, []
        raw_reply = ans["reply"]
        if draft is not None and not draft["valid"]:
            discard_reason = "draft_invalid"
            ans = dict(ans, reply="", assumptions=[], operator_requests=[])
            pre_notes.append(DRAFT_INVALID_NOTE)
        topic = await topic_guard.check(prompts.displayable_texts(ans, draft), budget, audit)
        calls += topic["calls"]
        if topic["usage"]:
            usages.append(topic["usage"])
        if not topic["on_topic"]:
            discard_reason = "topic_check_failed"
            ans = dict(ans, reply="", assumptions=[], operator_requests=[])
            draft = None
            pre_notes = [SCOPE_NOTE]
        prov = dict(llm.provenance(FEATURE, used))
        prov.update({"model_tier": tier, "prompt_hash": p_hash, "session_id": session_id})
        notes: List[str] = list(pre_notes)
        spec_version = None
        if draft is not None and draft["valid"] and draft["spec"] is not None:
            ai_prov = {"feature": FEATURE, "model": used, "model_tier": tier, "prompt_hash": p_hash,
                       "session_id": session_id, "generated_at": prov.get("generated_at")}
            draft["ai_provenance"] = ai_prov
            state = (ctx["srow"] or {}).get("state")
            if ctx["srow"] is not None and state not in AUTO_SAVE_STATES:
                draft["requires_confirmation"] = f"demotes {state} to draft"
                notes.append(f"Not saved automatically: saving this draft demotes the strategy from {state} to "
                             "draft (and its backtest counts as a trial) — confirm with \"Save draft\" to save "
                             "and backtest it.")
        if draft is not None and draft["valid"] and draft["spec"] is not None \
                and not draft["requires_confirmation"]:
            try:
                res = await asyncio.to_thread(_save_version, row, ctx, draft["spec"], draft["ai_provenance"])
                draft.update(strategy_id=res["id"], version=res["version"], unchanged=bool(res.get("unchanged")))
                spec_version = None if res.get("unchanged") else res["version"]
                if res.get("unchanged"):
                    notes.append(f"The draft is identical to version {res['version']} — no new version, no "
                                 "new backtest.")
            except lc.SpecInvalid as ex:
                draft.update(valid=False, validation={"ok": False, "errors": list(ex.validation.get("errors") or []),
                                                      "warnings": list(ex.validation.get("warnings") or [])})
            except lc.Conflict as ex:
                draft.update(not_saved=True, not_saved_reason=ex.message)       # still a valid spec (L5)
                notes.append(f"The draft was not saved: {ex.message}")
        total_cost = round(budget.run_spent, 6)
        usage_out = {"cost_usd": total_cost, "calls": calls, "estimate_usd": round(sum(estimates), 6),
                     "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usages),
                     "completion_tokens": sum(int(u.get("completion_tokens") or 0) for u in usages),
                     "cost_estimated": any(u.get("cost_usd") is None for u in usages)}
        mem_short = [memory.short(f) for f in ctx["memory"]]
        detail = {"draft": draft, "assumptions": ans["assumptions"], "memory_used": mem_short, "kind": ans["kind"],
                  "topic_check": topic["status"], "topic_check_reason": topic["reason"]}
        if discard_reason:
            detail.update(discarded=True, discarded_reason=discard_reason, discarded_text=raw_reply[:DISCARDED_TEXT_MAX])
        rows = [{"session_id": session_id, "role": "user", "text": text},
                {"session_id": session_id, "role": "assistant", "text": ans["reply"], "spec_version": spec_version,
                 "operator_requests_json": json.dumps(ans["operator_requests"], ensure_ascii=False),
                 "provenance_json": json.dumps(prov, ensure_ascii=False, sort_keys=True),
                 "usage_json": json.dumps(usage_out, ensure_ascii=False, sort_keys=True),
                 "cost_usd": total_cost, "detail_json": json.dumps(detail, ensure_ascii=False, default=str)}]
        rows += [{"session_id": session_id, "role": "system_note", "text": n} for n in notes]
        ids = await asyncio.to_thread(db.studio_messages_insert, rows,
                                      ("user", "studio_message", dict(audit, model=used, cost_usd=total_cost,
                                                                      calls=calls, spec_version=spec_version,
                                                                      valid=bool(draft and draft["valid"]),
                                                                      operator_requests=len(ans["operator_requests"]))))
        note_ids = ids[2:]
        if spec_version is not None:
            extra = await asyncio.to_thread(_start_or_queue, session_id, ids[1], draft["strategy_id"], spec_version)
            if extra:
                note_ids += await asyncio.to_thread(db.studio_messages_insert,
                                                    [{"session_id": session_id, "role": "system_note",
                                                      "text": n["text"]} for n in extra])
        stored = {m["id"]: m for m in await asyncio.to_thread(db.studio_messages_list, session_id)}
        latest = await asyncio.to_thread(_latest_events)
        return {"user_message": message_out(stored[ids[0]], latest),
                "assistant_message": await asyncio.to_thread(message_out, stored[ids[1]], latest),
                "notes": [message_out(stored[i], latest) for i in note_ids if i in stored]}
    finally:
        _busy.release()


def _recover_link(row: dict) -> Optional[str]:
    """Idempotent session -> strategy link (review L6): a Studio strategy whose version-1 envelope names
    this session but was never linked (backend stopped between the two writes) is linked now instead of
    creating a second strategy/family. -> the linked strategy id or None."""
    if row.get("strategy_id"):
        return None
    for s in db.strategies_list(origin="studio"):
        v1 = db.strategy_version_get(s["id"], 1)
        try:
            env = json.loads(v1["envelope_json"]) if v1 else {}
        except (TypeError, ValueError):
            continue
        if ((env or {}).get("ai_provenance") or {}).get("session_id") == row["id"]:
            if db.studio_session_set_strategy(row["id"], s["id"]):
                _audit_quiet("studio_session_relinked", {"session_id": row["id"], "strategy_id": s["id"]})
            row["strategy_id"] = db.studio_session_get(row["id"])["strategy_id"]
            return row["strategy_id"]
    return None


def save_draft(session_id: str, message_id: int) -> dict:
    """POST …/drafts/{message_id}/save {confirm: true}: saves a draft that ``requires_confirmation``
    (demotes the strategy to draft) and starts/queues its backtest -> {draft, notes}."""
    from ..lifecycle import registry as lc
    if not _busy.acquire(blocking=False):
        raise StudioError(409, "busy", BUSY_MESSAGE)
    try:
        row = _session_row(session_id)
        found = db.studio_messages_list(session_id, ids=[int(message_id)])
        if not found or found[0]["role"] != "assistant":
            raise StudioError(404, "not_found", "Draft not found.")
        m = found[0]
        draft = (m.get("detail") or {}).get("draft")
        saves = db.studio_draft_saves(session_id)
        if (not draft or not draft.get("requires_confirmation") or not draft.get("valid")
                or m["id"] in saves or not row.get("strategy_id")):
            raise StudioError(409, "not_confirmable", NOT_CONFIRMABLE_MESSAGE)
        before = db.strategy_get(row["strategy_id"])
        try:
            res = lc.new_studio_version(row["strategy_id"], draft["spec"], int(draft["base_version"]),
                                        draft.get("ai_provenance") or {"feature": FEATURE, "session_id": session_id})
        except lc.Conflict as ex:
            cur = (ex.extra or {}).get("current_version")
            raise StudioError(409, "stale", f"The strategy was changed meanwhile (current version {cur}) — ask the "
                                            "Studio for a new draft.") from None
        except lc.SpecInvalid:
            raise StudioError(409, "not_confirmable", "The draft no longer validates — ask the Studio for a new "
                                                      "draft.") from None
        db.studio_draft_save_insert({"session_id": session_id, "message_id": m["id"], "strategy_id": res["id"],
                                     "version": res["version"]},
                                    ("user", "studio_draft_saved", {"session_id": session_id, "message_id": m["id"],
                                                                     "strategy_id": res["id"], "version": res["version"],
                                                                     "from_state": (before or {}).get("state"),
                                                                     "unchanged": bool(res.get("unchanged"))}))
        notes = []
        if res.get("unchanged"):
            notes.append(f"The draft is identical to version {res['version']} — no new version, no new backtest.")
        else:
            notes.append(f"Version {res['version']} saved after confirmation (the strategy went from "
                         f"{(before or {}).get('state')} to draft).")
            notes += [n["text"] for n in _start_or_queue(session_id, m["id"], res["id"], res["version"])]
        _ESTIMATES.pop(session_id, None)
        ids = db.studio_messages_insert([{"session_id": session_id, "role": "system_note", "text": n} for n in notes])
        latest = _latest_events()
        msgs = {x["id"]: x for x in db.studio_messages_list(session_id)}
        return {"draft": message_out(msgs[m["id"]], latest)["draft"],
                "notes": [message_out(msgs[i], latest) for i in ids if i in msgs]}
    finally:
        _busy.release()


def _clarify_in_a_row(msgs: List[dict]) -> int:
    """Consecutive (not discarded) assistant "clarify" answers at the end of the conversation."""
    n = 0
    for m in reversed(msgs):
        if m["role"] != "assistant":
            continue
        d = m.get("detail") or {}
        if d.get("kind") == "clarify" and not d.get("discarded"):
            n += 1
            continue
        break
    return n


def _store_discarded(session_id, text, raw, parsed, reason, used, tier, p_hash, usage, est, budget, audit) -> dict:
    """An off-topic / rule-breaking answer: its text is NOT shown (kept for the audit only); the user gets the
    fixed non-AI SCOPE_NOTE; the cost is booked and shown on the (empty) assistant message."""
    from strategy_platform.core import llm
    prov = dict(llm.provenance(FEATURE, used))
    prov.update({"model_tier": tier, "prompt_hash": p_hash, "session_id": session_id})
    total = round(budget.run_spent, 6)
    usage_out = {"cost_usd": total, "calls": 1, "estimate_usd": round(est, 6),
                 "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                 "completion_tokens": int(usage.get("completion_tokens") or 0),
                 "cost_estimated": usage.get("cost_usd") is None}
    kind = parsed.get("kind") if isinstance(parsed, dict) and isinstance(parsed.get("kind"), str) else None
    detail = {"draft": None, "assumptions": [], "memory_used": [], "kind": kind, "discarded": True,
              "discarded_reason": reason, "discarded_text": str(raw or "")[:DISCARDED_TEXT_MAX]}
    rows = [{"session_id": session_id, "role": "user", "text": text},
            {"session_id": session_id, "role": "assistant", "text": "", "operator_requests_json": "[]",
             "provenance_json": json.dumps(prov, ensure_ascii=False, sort_keys=True),
             "usage_json": json.dumps(usage_out, ensure_ascii=False, sort_keys=True), "cost_usd": total,
             "detail_json": json.dumps(detail, ensure_ascii=False, default=str)},
            {"session_id": session_id, "role": "system_note", "text": SCOPE_NOTE}]
    ids = db.studio_messages_insert(rows, ("user", "studio_message", dict(audit, model=used, cost_usd=total, calls=1,
                                                                          discarded_reason=reason)))
    stored = {m["id"]: m for m in db.studio_messages_list(session_id, ids=ids)}
    latest = _latest_events()
    return {"user_message": message_out(stored[ids[0]], latest), "assistant_message": message_out(stored[ids[1]], latest),
            "notes": [message_out(stored[ids[2]], latest)]}


def _save_version(row: dict, ctx: dict, spec: dict, ai_prov: dict) -> dict:
    from ..lifecycle import registry as lc
    if row.get("strategy_id"):
        return lc.new_studio_version(row["strategy_id"], spec, ctx["current_version"], ai_prov)
    res = lc.create_from_studio(spec, ai_prov)
    if not db.studio_session_set_strategy(row["id"], res["id"]):
        raise db.StorageUnavailable("the Studio session already has a strategy", detail=row["id"])
    row["strategy_id"] = res["id"]
    return dict(res, unchanged=False)


def operator_requests(session_id: Optional[str] = None) -> dict:
    if session_id is not None:
        _session_row(session_id)
    out = []
    for m in db.studio_messages_list(session_id, with_operator_requests=True):
        for i, r in enumerate(m.get("operator_requests") or []):
            out.append({"id": f"{m['id']}-{i + 1}", "session_id": m["session_id"], "message_id": m["id"],
                        "name": r.get("name"), "purpose": r.get("purpose"), "signature": r.get("signature"),
                        "created_at": m["created_at"], "label": OPERATOR_REQUEST_LABEL})
    out.reverse()
    return {"operator_requests": out}


def memory_list(family_id=None, strategy_id=None, limit: int = 200) -> dict:
    return {"facts": db.memory_facts_list(family_id, strategy_id, limit)}
