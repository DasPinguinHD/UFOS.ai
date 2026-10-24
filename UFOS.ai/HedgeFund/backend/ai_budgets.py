"""``GET/PUT /ai/budgets`` of the HedgeFund backend — all AI budgets of the module (task B, 2026-10-17).

Contract: ``UFOS.ai/docs/ai-budgets-spec.md`` §4.3/§4.4. Aggregates (authoritative values, no copies):

- ``fund``                      fund param ``ai_daily_budget_usd`` (sentiment + judge, Council of AIs);
                                PUT = ``engine.update_params`` (audited ``params_update``, applies next cycle)
- ``explain``                   ``strategy_platform.backtest.explain`` settings (audited)
- ``ideation_month``/``_day``   ``strategy_platform.research.studio`` settings (audited)
- ``loop``                      the stage-6 discovery loop via the hook ``PROVIDER_MODULES`` (§4.4)
- ``reports``                   stage 9 report / Shadow Account commentary (``ai_caps.DailyCaps``, SPEC §19.4)
- ``newsroom_analysis``, ``newsroom_insider_summary``  NEW daily caps (``ai_caps.DailyCaps`` over the
                                hedgefund.db meta table, audited ``ai_budget_changed``), enforced in
                                main.py before each Newsroom AI call (402 ``budget``).

The existing routes (/fund/params, /v2/settings/explain, /v2/settings/ideation) keep working; this is a
second door to the same stored values. Handlers are framework-free ``(status, payload)`` (testable without
aiohttp); ``register(app, engine)`` adds the aiohttp routes behind main.py's api_auth middleware.
Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
import asyncio
import importlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import ai_caps

logger = logging.getLogger(__name__)

MODULE_ID = "hedgefund"
MODULE_LABEL = "Hedge Fund"
COST_MODULE = "HedgeFund"
# Stage-6 hook (§4.4): dotted module paths exposing ai_budget_entries(now) / ai_budget_put(key, v, actor).
PROVIDER_MODULES = ("strategy_platform.research.loop",)
READ_FAILED = "This budget could not be read — see backend.log."

NEWSROOM_GROUP = "Financial Newsroom"
NEWSROOM_CAPS = (
    ai_caps.CapDef("newsroom_analysis", "Newsroom · Analysis & Reasoning", 5.0, 0.02, max_usd=50.0,
                   group=NEWSROOM_GROUP, cost_log=((COST_MODULE, "analysis"),),
                   notes="Checked before every \"Let AI analyse this\" run (estimate $0.02 standard, "
                         "$0.30 premium)."),
    ai_caps.CapDef("newsroom_insider_summary", "Newsroom · Insider summary", 1.0, 0.02, max_usd=50.0,
                   group=NEWSROOM_GROUP, cost_log=((COST_MODULE, "insider-summary"),),
                   notes="Checked before every \"Sum up most notable\" (Insider Trades) run."),
)
ANALYSIS_ESTIMATES = {"standard": 0.02, "premium": 0.30}

# Stage 9 (strategy_platform SPEC §19.4): AI commentary of monitoring reports and of the Shadow Account report —
# one daily cap (key ``reports``), default — user confirm.
REPORTS_CAPS = (
    ai_caps.CapDef("reports", "Hedge Fund · Report commentary", 0.50, 0.02, max_usd=20.0,
                   group="Strategy Platform", cost_log=((COST_MODULE, "sp-report"),),
                   notes="Checked before every AI commentary of a monitoring report or a Shadow Account report "
                         "(numbers only; estimate $0.02 standard, $0.20 premium)."),
)
_reports = None
_reports_lock = threading.Lock()

_newsroom = None
_newsroom_lock = threading.Lock()


class PutRefused(Exception):
    """A PUT the source refused: HTTP ``status`` + payload."""

    def __init__(self, status: int, payload: dict):
        super().__init__(payload.get("error"))
        self.status, self.payload = int(status), payload


def _sp_db():
    from strategy_platform.storage import db
    return db


def newsroom_caps() -> ai_caps.DailyCaps:
    """The Newsroom's daily caps (singleton) over hedgefund.db meta (strict: raises StorageUnavailable)."""
    global _newsroom
    with _newsroom_lock:
        if _newsroom is None:
            _newsroom = ai_caps.DailyCaps(
                MODULE_ID, MODULE_LABEL, NEWSROOM_CAPS,
                kv_get=lambda k: _sp_db().meta_get(k),
                kv_set=lambda k, v: _sp_db().meta_set(k, v),
                audit=lambda change: _sp_db().add_audit("user", "ai_budget_changed", change),
                logger=logger)
        return _newsroom


def reports_caps() -> ai_caps.DailyCaps:
    """The report-commentary cap (singleton) over hedgefund.db meta (strict: raises StorageUnavailable)."""
    global _reports
    with _reports_lock:
        if _reports is None:
            _reports = ai_caps.DailyCaps(
                MODULE_ID, MODULE_LABEL, REPORTS_CAPS,
                kv_get=lambda k: _sp_db().meta_get(k),
                kv_set=lambda k, v: _sp_db().meta_set(k, v),
                audit=lambda change: _sp_db().add_audit("user", "ai_budget_changed", change),
                logger=logger)
        return _reports


def analysis_estimate(tier: str) -> float:
    return ANALYSIS_ESTIMATES.get(tier, ANALYSIS_ESTIMATES["premium"])


async def guarded_newsroom_call(key: str, estimate, factory):
    """A Newsroom AI call under its daily cap (raises ai_caps.BudgetBlocked BEFORE the call)."""
    return await ai_caps.guarded_call(newsroom_caps(), key, estimate, factory)


# --- entry helpers ---------------------------------------------------------------------------

def _r6(v):
    return None if v is None else round(float(v), 6)


def _entry(key, label, group, period, period_id, limit, default, lo, hi, step, spent, notes, cost_log,
           applies="immediately", status=None, message=None):
    remaining = None if limit is None or spent is None else max(0.0, float(limit) - float(spent))
    if status is None:
        status = "ok"
        if limit is not None and float(limit) <= 0:
            status = "blocked"
        elif remaining is not None and remaining <= 0:
            status = "blocked"
    return {"key": key, "label": label, "group": group, "period": period, "period_id": period_id,
            "limit_usd": _r6(limit), "default_usd": _r6(default), "min": _r6(lo), "max": _r6(hi), "step": step,
            "spent_usd": _r6(spent), "remaining_usd": _r6(remaining), "enabled": bool(limit and float(limit) > 0),
            "status": status, "message": message, "editable": True, "applies": applies, "notes": notes,
            "cost_log": [{"module": m, "feature": f} for m, f in cost_log]}


def _error_entry(key, label, group, period, period_id, lo, hi, step, default, notes, cost_log, message, applies):
    return _entry(key, label, group, period, period_id, None, default, lo, hi, step, None, notes, cost_log,
                  applies=applies, status="error", message=message)


def _safe_message(ex: BaseException) -> str:
    """A user-facing text for a read failure (UserFacingError texts are written for users; others not)."""
    msg = getattr(ex, "user_message", None)
    return str(msg)[:ai_caps.TEXT_MAX] if msg else READ_FAILED


# --- sources ---------------------------------------------------------------------------------

class FundSource:
    KEY = "fund"
    LABEL = "Fund AI — sentiment + judge (Council of AIs)"
    GROUP = "Fund (paper trading)"
    COST = ((COST_MODULE, "fund-sentiment"), (COST_MODULE, "fund-judge"))
    NOTES = ("Daily budget of the paper engine's sentiment agent and judge (fund parameter "
             "ai_daily_budget_usd). A change creates a new parameter version.")

    def __init__(self, engine):
        self.engine = engine

    def keys(self):
        return (self.KEY,)

    def _meta(self):
        from strategy_platform.core import params as fund_params
        lim = fund_params.LIMITS["ai_daily_budget_usd"]
        return (float(fund_params.DEFAULTS["ai_daily_budget_usd"]), float(lim["min"]), float(lim["max"]),
                float(lim.get("step", 0.1)))

    @staticmethod
    def _council_state() -> str:
        """SP §22.2: from the Council switch-over on the limit is ``cl_params.ai_daily_budget_usd`` and the spend the
        Council's ONE counter (``council.ai_spend``)."""
        try:
            from strategy_platform.council import store as cl_store
            return cl_store.switchover()
        except Exception:     # noqa: BLE001
            return "unreadable"

    def _council_entries(self, now, default, lo, hi, step, day):
        from strategy_platform.core import llm
        from strategy_platform.council import spend
        from strategy_platform.council import store as cl_store
        try:
            got = cl_store.latest_params()
            if got is None:
                raise RuntimeError("the Council has no parameters")
            limit = float(got[1]["ai_daily_budget_usd"])
        except Exception as ex:     # noqa: BLE001
            logger.error("ai budgets: Council params unreadable", exc_info=True)
            return [_error_entry(self.KEY, self.LABEL, self.GROUP, "day", day, lo, hi, step, default, self.NOTES,
                                 self.COST, _safe_message(ex), "next_cycle")]
        spent = spend.spent_today(now)
        if spent is None:
            return [_entry(self.KEY, self.LABEL, self.GROUP, "day", day, limit, default, lo, hi, step, None,
                           self.NOTES, self.COST, applies="next_cycle", status="error",
                           message=llm.SPEND_UNREADABLE_MESSAGE)]
        e = _entry(self.KEY, self.LABEL, self.GROUP, "day", day, limit, default, lo, hi, step, float(spent),
                   self.NOTES, self.COST, applies="next_cycle")
        if e["status"] == "blocked":
            e["message"] = llm.AI_DISABLED_MESSAGE if limit <= 0 else llm.BUDGET_REACHED_MESSAGE
        return [e]

    def _council_put(self, value) -> None:
        from strategy_platform.council import params as cp
        from strategy_platform.council import store as cl_store
        from strategy_platform.storage import db as sp_db
        got = cl_store.latest_params()
        if got is None:
            raise PutRefused(409, {"error": "The Council has no parameters.", "kind": "NotActive"})
        clean, errs = cp.validate({"ai_daily_budget_usd": value}, got[1])
        if errs:
            raise PutRefused(400, {"error": "invalid input", "errors": {"limit_usd": "; ".join(errs.values())}})
        merged = cp.merge(got[1], clean)
        try:
            with sp_db._connect() as conn:
                conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, 'user', 'council_params', "
                             "?)", (sp_db.utc_now_iso(), sp_db._dumps({"changes": clean, "via": "/ai/budgets"})))
                cl_store.add_params(merged, "user", "AI budget (Settings → AI budgets)", "user", conn=conn)
        except Exception as ex:     # noqa: BLE001 — audit-first: nothing written
            raise PutRefused(503, {"error": "audit log unavailable — the action was not carried out",
                                   "kind": "AuditUnavailable"}) from ex

    def entries(self, now):
        default, lo, hi, step = self._meta()
        day = now.date().isoformat()
        state = self._council_state()
        if state in ("pending", "done"):
            return self._council_entries(now, default, lo, hi, step, day)
        if self.engine is None:
            return [_error_entry(self.KEY, self.LABEL, self.GROUP, "day", day, lo, hi, step, default, self.NOTES,
                                 self.COST, "The fund engine is not running.", "next_cycle")]
        try:
            info = self.engine.get_params()
            limit = float(info["params"]["ai_daily_budget_usd"])
        except Exception as ex:     # noqa: BLE001
            logger.error("ai budgets: fund params unreadable", exc_info=True)
            return [_error_entry(self.KEY, self.LABEL, self.GROUP, "day", day, lo, hi, step, default, self.NOTES,
                                 self.COST, _safe_message(ex), "next_cycle")]
        if not info.get("version"):
            # version 0 = the built-in defaults: nothing stored yet, OR the store is disabled/unreadable
            # (fund.store.effective_params) — never show the defaults as if they were stored (review L6)
            problem = self._store_problem()
            if problem:
                return [_error_entry(self.KEY, self.LABEL, self.GROUP, "day", day, lo, hi, step, default,
                                     self.NOTES, self.COST, problem, "next_cycle")]
        spent = None
        try:
            spent = self.engine.store.ai_spend_today()
        except Exception:     # noqa: BLE001
            logger.error("ai budgets: fund AI spend unreadable", exc_info=True)
        if spent is None:
            from strategy_platform.core import llm
            e = _entry(self.KEY, self.LABEL, self.GROUP, "day", day, limit, default, lo, hi, step, None, self.NOTES,
                       self.COST, applies="next_cycle", status="error", message=llm.SPEND_UNREADABLE_MESSAGE)
            return [e]
        e = _entry(self.KEY, self.LABEL, self.GROUP, "day", day, limit, default, lo, hi, step, float(spent),
                   self.NOTES, self.COST, applies="next_cycle")
        if e["status"] == "blocked":
            from strategy_platform.core import llm
            e["message"] = llm.AI_DISABLED_MESSAGE if limit <= 0 else llm.BUDGET_REACHED_MESSAGE
        return [e]

    @staticmethod
    def _store_problem():
        """Why the fund store (newsroom.db) cannot hold the params, or None when it is readable."""
        from newsroom import db as ndb
        if not ndb.is_enabled():
            return ("The fund store (newsroom.db) is disabled — the fund parameters cannot be read or saved "
                    "(see backend.log).")
        try:
            with ndb._connect() as conn:
                conn.execute("SELECT 1 FROM fund_params LIMIT 1").fetchall()
        except Exception:     # noqa: BLE001
            logger.error("ai budgets: fund params table unreadable", exc_info=True)
            return "The fund parameters could not be read (newsroom.db) — see backend.log."
        return None

    def put(self, key, value):
        if self._council_state() in ("pending", "done"):
            return self._council_put(value)
        if self.engine is None:
            raise PutRefused(503, {"error": "The fund engine is not running.", "kind": "EngineUnavailable"})
        from fund.engine import EngineError
        try:
            self.engine.update_params({"ai_daily_budget_usd": float(value)}, "user")
        except EngineError as ex:
            payload = {"error": ex.message}
            if getattr(ex, "errors", None):
                payload["errors"] = {"limit_usd": "; ".join(str(v) for v in ex.errors.values())}
            raise PutRefused(ex.status, payload)


class ExplainSource:
    KEY = "explain"
    LABEL = "Strategy explanations (AI)"
    GROUP = "Strategy Platform"
    COST = ((COST_MODULE, "sp-explain"),)
    NOTES = "Daily budget of the AI explanations of backtest/validation runs."

    def keys(self):
        return (self.KEY,)

    def entries(self, now):
        from strategy_platform.backtest import explain
        day = now.date().isoformat()
        args = (explain.DEFAULT_DAILY_BUDGET_USD, 0.0, explain.MAX_DAILY_BUDGET_USD)
        try:
            s = explain.get_settings(now)
        except Exception as ex:     # noqa: BLE001
            logger.error("ai budgets: explain settings unreadable", exc_info=True)
            return [_error_entry(self.KEY, self.LABEL, self.GROUP, "day", day, args[1], args[2], 0.1, args[0],
                                 self.NOTES, self.COST, _safe_message(ex), "immediately")]
        e = _entry(self.KEY, self.LABEL, self.GROUP, "day", s["day"], s["daily_budget_usd"], args[0], args[1],
                   args[2], 0.1, s["spent_today_usd"], self.NOTES, self.COST)
        if s.get("paused_reason"):
            e.update(status="paused", message=s["paused_reason"])
        elif e["status"] == "blocked":
            e["message"] = explain.BUDGET_OFF_MESSAGE if s["daily_budget_usd"] <= 0 else explain.BUDGET_REACHED_MESSAGE
        return [e]

    def put(self, key, value):
        from strategy_platform.backtest import explain
        body = {"daily_budget_usd": float(value)}
        errors = explain.check_settings(body)
        if errors:
            raise PutRefused(400, {"error": "invalid input", "errors": {"limit_usd": errors.get("daily_budget_usd")
                                                                     or "invalid"}})
        _call_settings(explain.put_settings, body)


class IdeationSource:
    GROUP = "Strategy Platform"
    COST = ((COST_MODULE, "sp-studio"), (COST_MODULE, "sp-paper"), (COST_MODULE, "sp-committee"))
    NOTES = "Shared by the Strategy Studio, paper import and the investment committee."
    LABELS = {"ideation_month": "AI ideation — monthly budget", "ideation_day": "AI ideation — daily cap"}

    def keys(self):
        return tuple(self.LABELS)

    def entries(self, now):
        from strategy_platform.research import studio
        day, month = now.date().isoformat(), now.strftime("%Y-%m")
        hi = studio.MAX_BUDGET_USD
        try:
            s = studio.get_settings(now)
        except Exception as ex:     # noqa: BLE001
            logger.error("ai budgets: ideation settings unreadable", exc_info=True)
            msg = _safe_message(ex)
            return [_error_entry("ideation_month", self.LABELS["ideation_month"], self.GROUP, "month", month, 0.0, hi,
                                 0.5, studio.DEFAULT_MONTHLY_BUDGET_USD, self.NOTES, self.COST, msg, "immediately"),
                    _error_entry("ideation_day", self.LABELS["ideation_day"], self.GROUP, "day", day, 0.0, hi, 0.1,
                                 studio.DEFAULT_DAILY_CAP_USD, self.NOTES, self.COST, msg, "immediately")]
        m = _entry("ideation_month", self.LABELS["ideation_month"], self.GROUP, "month", s["month"],
                   s["monthly_budget_usd"], studio.DEFAULT_MONTHLY_BUDGET_USD, 0.0, hi, 0.5, s["spent_month_usd"],
                   self.NOTES, self.COST)
        d = _entry("ideation_day", self.LABELS["ideation_day"], self.GROUP, "day", s["day"], s["daily_cap_usd"],
                   studio.DEFAULT_DAILY_CAP_USD, 0.0, hi, 0.1, s["spent_today_usd"], self.NOTES, self.COST)
        for e, off, reached in ((m, studio.OFF_MESSAGE, studio.MONTH_REACHED_MESSAGE),
                                (d, studio.DAY_OFF_MESSAGE, studio.DAY_REACHED_MESSAGE)):
            if s.get("paused_reason"):
                e.update(status="paused", message=s["paused_reason"])
            elif e["status"] == "blocked":
                e["message"] = off if (e["limit_usd"] or 0) <= 0 else reached
        return [m, d]

    def put(self, key, value):
        from strategy_platform.research import studio
        try:
            monthly, daily = studio.limits()
        except Exception as ex:     # noqa: BLE001 — an invalid stored value: both must be set in Settings
            raise PutRefused(503, {"error": _safe_message(ex) + " (set both ideation values in Hedge Fund → "
                                   "Settings)", "kind": type(ex).__name__})
        body = {"monthly_budget_usd": monthly, "daily_cap_usd": daily}
        body["monthly_budget_usd" if key == "ideation_month" else "daily_cap_usd"] = float(value)
        errors = studio.check_settings(body)
        if errors:
            raise PutRefused(400, {"error": "invalid input", "errors": {"limit_usd": "; ".join(errors.values())}})
        _call_settings(studio.put_settings, body)


class NewsroomSource:
    def keys(self):
        return tuple(c.key for c in NEWSROOM_CAPS)

    def entries(self, now):
        return newsroom_caps().entries(now)

    def put(self, key, value):
        try:
            newsroom_caps().put(key, float(value), actor="user")
        except ai_caps.StoreUnavailable:
            logger.error("ai budgets: newsroom cap %s could not be changed", key, exc_info=True)
            raise PutRefused(503, {"error": "The budget could not be stored (hedgefund.db or its audit is "
                                            "unavailable) — see backend.log.", "kind": "StoreUnavailable"})


class ReportsSource:
    """Stage 9: the report commentary cap (strategy_platform SPEC §19.4)."""

    def keys(self):
        return tuple(c.key for c in REPORTS_CAPS)

    def entries(self, now):
        return reports_caps().entries(now)

    def put(self, key, value):
        try:
            reports_caps().put(key, float(value), actor="user")
        except ai_caps.StoreUnavailable:
            logger.error("ai budgets: reports cap %s could not be changed", key, exc_info=True)
            raise PutRefused(503, {"error": "The budget could not be stored (hedgefund.db or its audit is "
                                            "unavailable) — see backend.log.", "kind": "StoreUnavailable"})


class HookSource:
    """A stage-6 style provider module (§4.4)."""

    def __init__(self, dotted: str):
        self.dotted = dotted
        self._keys = ("loop",)

    def _module(self):
        try:
            return importlib.import_module(self.dotted)
        except ModuleNotFoundError as ex:
            # development-order guard (the provider is not built yet) — NOT a data fallback
            if ex.name and (ex.name == self.dotted or self.dotted.startswith(ex.name + ".")):
                return None
            raise

    def keys(self):
        return self._keys

    def entries(self, now):
        mod = self._module()
        if mod is None or not hasattr(mod, "ai_budget_entries"):
            return []
        out = []
        for e in list(mod.ai_budget_entries(now) or [])[:16]:
            if not isinstance(e, dict) or not str(e.get("key", "")).startswith("loop"):
                raise ValueError(f"{self.dotted}: budget entries must be dicts with keys starting with 'loop'")
            out.append(e)
        self._keys = tuple(e["key"] for e in out) or ("loop",)
        return out

    def put(self, key, value):
        mod = self._module()
        if mod is None or not hasattr(mod, "ai_budget_put"):
            raise PutRefused(400, {"error": "invalid input", "errors": {"key": "unknown budget key"}})
        try:
            mod.ai_budget_put(key, float(value), "user")
        except ValueError as ex:
            raise PutRefused(400, {"error": "invalid input", "errors": {"limit_usd": str(ex)[:200] or "invalid"}})
        except PutRefused:
            raise
        except Exception as ex:     # noqa: BLE001
            _raise_user_error(ex)


def _raise_user_error(ex: BaseException):
    """ExplainError/StudioError (status + payload), other UserFacingError -> 503, anything else -> 500."""
    status = getattr(ex, "status", None)
    if callable(getattr(ex, "payload", None)) and isinstance(status, int):
        raise PutRefused(status, ex.payload())
    msg = getattr(ex, "user_message", None)
    if msg:
        raise PutRefused(503, {"error": str(msg), "kind": type(ex).__name__})
    logger.error("ai budgets: PUT failed", exc_info=(type(ex), ex, ex.__traceback__))
    raise PutRefused(500, {"error": "internal error while storing the budget (see backend.log)"})


def _call_settings(fn, body):
    try:
        fn(body, "user")
    except Exception as ex:     # noqa: BLE001
        _raise_user_error(ex)


# --- aggregator --------------------------------------------------------------------------------

class HedgeFundBudgets:
    def __init__(self, engine=None, provider_modules=PROVIDER_MODULES, clock=None):
        self.sources = [FundSource(engine), ExplainSource(), IdeationSource()]
        self.sources += [HookSource(m) for m in provider_modules]
        self.sources.append(ReportsSource())          # stage 9 (strategy_platform SPEC §19.4)
        self.sources.append(NewsroomSource())
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        return self._clock().astimezone(timezone.utc)

    def entries(self, now=None):
        now = now or self._now()
        out, seen = [], set()
        for src in self.sources:
            try:
                got = src.entries(now)
            except Exception as ex:     # noqa: BLE001 — one source failing never hides the others
                logger.error("ai budgets: source %s failed", type(src).__name__, exc_info=True)
                got = [{"key": k, "label": k, "group": None, "period": "day", "period_id": now.date().isoformat(),
                        "limit_usd": None, "default_usd": None, "min": 0.0, "max": 0.0, "step": 0.1,
                        "spent_usd": None, "remaining_usd": None, "enabled": False, "status": "error",
                        "message": _safe_message(ex), "editable": False, "applies": "immediately", "notes": "",
                        "cost_log": []} for k in src.keys()]
            for e in got:
                if e.get("key") in seen:
                    logger.error("ai budgets: duplicate key %s ignored", e.get("key"))
                    continue
                seen.add(e.get("key"))
                e["_source"] = src
                out.append(e)
        return out

    def payload(self, now=None, entries=None):
        now = now or self._now()
        entries = self.entries(now) if entries is None else entries
        return ai_caps.module_payload(MODULE_ID, MODULE_LABEL,
                                      [{k: v for k, v in e.items() if k != "_source"} for e in entries], now)

    def handle_get(self, query=None):
        if query:
            return 400, {"error": "invalid input", "errors": {str(k): "unknown parameter" for k in query}}
        return 200, self.payload()

    def handle_put(self, body, origin=None, query=None):
        refused = ai_caps.refuse_put(origin, query)
        if refused:
            return refused
        entries = self.entries()
        by_key = {e["key"]: e for e in entries}

        def bounds(key):
            e = by_key.get(key)
            if not e or not e.get("editable", True) or e.get("min") is None or e.get("max") is None:
                return None
            return float(e["min"]), float(e["max"])

        errors = ai_caps.check_put_body(body, bounds)
        if errors:
            return 400, {"error": "invalid input", "errors": errors}
        key, value = body["key"], round(float(body["limit_usd"]), 6)
        entry = by_key[key]
        old = entry.get("limit_usd")
        try:
            entry["_source"].put(key, value)
        except PutRefused as ex:
            return ex.status, ex.payload
        out = self.payload()
        out["changed"] = {"key": key, "old": old, "new": value}
        logger.info("AI budget changed via /ai/budgets: %s %s -> %s", key, old, value)
        return 200, out


def register(app, engine=None) -> HedgeFundBudgets:
    """Adds GET/PUT /ai/budgets to main.py's aiohttp app (behind its api_auth middleware)."""
    from aiohttp import web  # lazy: importable without aiohttp

    budgets = HedgeFundBudgets(engine)

    def _json(status, payload):
        return web.json_response(payload, status=status,
                                 dumps=lambda o: json.dumps(o, allow_nan=False, ensure_ascii=False, default=str))

    async def _get(request):
        try:
            status, payload = await asyncio.to_thread(budgets.handle_get, dict(request.query))
        except Exception:     # noqa: BLE001
            logger.exception("GET /ai/budgets failed")
            status, payload = 500, {"error": "internal error (see backend.log)"}
        return _json(status, payload)

    async def _put(request):
        origin = request.headers.get("Origin")
        if origin is not None:
            return _json(403, {"error": ai_caps.ORIGIN_REFUSED})
        body, refused = await ai_caps.read_json_body(request)
        if refused:
            return _json(*refused)
        try:
            status, payload = await asyncio.to_thread(budgets.handle_put, body, None, dict(request.query))
        except Exception:     # noqa: BLE001
            logger.exception("PUT /ai/budgets failed")
            status, payload = 500, {"error": "internal error (see backend.log)"}
        return _json(status, payload)

    app.router.add_get("/ai/budgets", _get)
    app.router.add_put("/ai/budgets", _put)
    return budgets
