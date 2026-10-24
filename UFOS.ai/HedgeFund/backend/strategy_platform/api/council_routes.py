"""/v2/council/* (SP §22.11) — the Council of AIs as a platform strategy: state, runs, "Run now", params, the
paper-council broker-params override (D9), the Council orders switch and (R2) the switch-over: GET
``/v2/council/switchover`` (dry run + blockers), POST ``/v2/council/switchover`` (the user's dialog — the ONLY way the
real switch-over happens) and POST ``/v2/council/switchover/revert``. Council GETs do NOT touch the client heartbeat
(SP §22.5). Errors ``{error, code}``, never a stack trace."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Mapping

from .trading_routes import _bad, _body, _note, _unknown

logger = logging.getLogger(__name__)


def _err(status: int, code: str, message: str):
    return status, {"error": message, "code": code}


def _svc():
    from strategy_platform.council import service
    return service.default()


def _symbol_cards(detail: dict) -> list:
    if not detail:
        return []
    sig = {}
    for x in detail.get("signals") or []:
        sig.setdefault(x["symbol"], []).append({"agent": x["agent"], "score": x["score"],
                                                "confidence": x["confidence"], "summary": x["summary"],
                                                "source": x["source"], "ai_generated": bool(x["ai_generated"])})
    ex = {x["decision_id"]: x for x in detail.get("executions") or []}
    pos = {p["symbol"]: p for p in detail.get("positions") or []}
    out = []
    for d in detail.get("decisions") or []:
        e = ex.get(d["id"])
        state = None
        if e and e.get("client_order_id"):
            from strategy_platform.execution import oms
            o = oms.get(e["client_order_id"])
            state = o["state"] if o else None
        out.append({"symbol": d["symbol"], "action": d["action"], "confidence": d["confidence"],
                    "judge_source": d["judge_source"], "rationale": d["rationale"],
                    "ai_generated": bool(d["ai_generated"]), "provenance": d.get("provenance"),
                    "agents": sig.get(d["symbol"], []), "target_weight": d["target_weight"],
                    "position": {"qty": (pos.get(d["symbol"]) or {}).get("qty"),
                                 "weight": (pos.get(d["symbol"]) or {}).get("weight")},
                    "execution": None if e is None else {"outcome": e["outcome"], "reason": e["reason"],
                                                         "client_order_id": e["client_order_id"],
                                                         "order_state": state},
                    "unavailable_reason": d["unavailable_reason"]})
    return out


def state_payload() -> dict:
    from strategy_platform.council import service, spend, store
    svc = service.default()
    sw_error = None
    try:
        sw = store.switchover()
        if sw == "pending":                 # R2 review M2: an unfinished attempt is visible with its error
            from strategy_platform.council import switchover as sw_mod
            sw_error = (sw_mod.state_info() or {}).get("last_error")
    except Exception:          # noqa: BLE001
        sw = "unreadable"
    params = None
    try:
        params = store.latest_params()
    except Exception:          # noqa: BLE001
        logger.exception("Council params unreadable")
    now = svc.now() if svc else datetime.now(timezone.utc)
    sandbox = svc.sandbox() if svc else False
    if svc is None or sw != "done":
        task = "not_active"
    elif sandbox:
        task = "sandbox"
    else:
        task = "running" if svc.cycle_in_progress else "idle"
    limit = float(params[1]["ai_daily_budget_usd"]) if params else None
    spent = spend.spent_today(now)
    if limit is None:
        bstatus = None
    elif limit <= 0:
        bstatus = "off"
    elif spent is None:
        bstatus = "unreadable"
    else:
        bstatus = "reached" if spent >= limit else "ok"
    runs = store.runs(200)
    live = [r for r in runs if not r["legacy"]]
    since = live[-1]["started_at"] if live else None
    days = None
    if since:
        days = max(0, (now - datetime.fromisoformat(since.replace("Z", "+00:00"))).days)
    last = runs[0] if runs else None
    return {"active": sw == "done", "switchover": sw, "switchover_error": sw_error, "task": task,
            "orders_enabled": store.orders_enabled(),
            "label": service.LABEL, "record": {"since": since, "days": days,
                                                "text": (f"forward paper record, {days} days, not validated"
                                                         if days is not None else None)},
            "next_due_at": (svc.next_due_at.isoformat(timespec="seconds") if svc and svc.next_due_at else None),
            "budget": {"limit_usd": limit, "spent_usd": spent, "status": bstatus},
            "last_run": last, "symbols": _symbol_cards(store.run_detail(last["id"]) if last else None)}


class CouncilRoutesMixin:
    """Handlers of the /v2/council/* routes (SP §22.11)."""

    async def council_state(self, query: Mapping, body, match: Mapping):
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        try:
            return 200, await asyncio.to_thread(state_payload)
        except Exception as ex:      # noqa: BLE001
            logger.exception("GET /v2/council/state failed")
            return _err(503, "unavailable", f"The Council state could not be read ({type(ex).__name__}).")

    async def council_runs(self, query: Mapping, body, match: Mapping):
        from strategy_platform.council import store
        errors = {k: "unknown parameter" for k in query if k != "limit"}
        limit = 50
        if "limit" in query:
            try:
                limit = int(query["limit"])
                if not 1 <= limit <= 200:
                    raise ValueError
            except ValueError:
                errors["limit"] = "1..200"
        if errors:
            return _bad(errors)
        return 200, {"runs": await asyncio.to_thread(store.runs, limit)}

    async def council_run_detail(self, query: Mapping, body, match: Mapping):
        from strategy_platform.council import store
        try:
            rid = int(match.get("id") or "")
        except ValueError:
            return _bad({"id": "a run id"})
        d = await asyncio.to_thread(store.run_detail, rid)
        if d is None:
            return _err(404, "not_found", "No such Council run.")
        return 200, d

    async def council_run(self, query: Mapping, body, match: Mapping):
        from strategy_platform.council import service
        b = _body(body)
        if b is None or set(query) or (b and set(b)):
            return _bad({"_": "no parameters"})
        svc = _svc()
        if svc is None:
            return _err(503, "not_active", "The Council service is not running.")
        try:
            out = await asyncio.to_thread(svc.request_run)
        except service.CouncilError as ex:
            return _err(ex.status, ex.code, ex.message)
        return 202, out

    async def council_params_get(self, query: Mapping, body, match: Mapping):
        from strategy_platform.council import params as cp
        from strategy_platform.council import store
        got = await asyncio.to_thread(store.latest_params)
        return 200, {"version": got[0] if got else None, "params": got[1] if got else None, "limits": cp.limits(),
                     "active": got is not None}

    async def council_params_put(self, query: Mapping, body, match: Mapping):
        from strategy_platform.council import params as cp
        from strategy_platform.council import store
        from strategy_platform.storage import db
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"})
        errors: dict = {}
        reason = _note(b, errors)
        payload = {k: v for k, v in b.items() if k != "reason"}
        got = await asyncio.to_thread(store.latest_params)
        if got is None:
            return _err(409, "not_active", "The Council has no parameters before the switch-over.")
        clean, errs = cp.validate(payload, got[1])
        errors.update(errs)
        if errors:
            return _bad(errors)

        def write():
            merged = cp.merge(got[1], clean)
            with db._connect() as conn:
                conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, 'user', "
                             "'council_params', ?)", (db.utc_now_iso(), db._dumps({"changes": clean,
                                                                                    "reason": reason})))
                v = store.add_params(merged, "user", reason or None, "user", conn=conn)
            return {"version": v, "params": merged}
        try:
            return 200, await asyncio.to_thread(write)
        except Exception as ex:      # noqa: BLE001 — audit-first: nothing written
            logger.exception("PUT /v2/council/params failed")
            return _err(503, "audit_unavailable", "The change was not stored (database unavailable).")

    async def council_broker_params_get(self, query: Mapping, body, match: Mapping):
        from strategy_platform.council import params as cp
        from strategy_platform.council import store
        from strategy_platform.execution import accounts

        def read():
            if not accounts.exists(accounts.COUNCIL):
                return None
            acc = accounts.get(accounts.COUNCIL)
            ov = store.latest_broker_override()
            return {"version": ov[0] if ov else None, "migrated": {k: acc.params.get(k) for k in cp.BROKER_KEYS},
                    "override": ov[1] if ov else None,
                    "effective": cp.effective_broker(acc.params, ov[1] if ov else None), "limits": cp.broker_limits()}
        out = await asyncio.to_thread(read)
        if out is None:
            return _err(404, "not_found", "The account paper-council does not exist (migration not done).")
        return 200, out

    async def council_broker_params_put(self, query: Mapping, body, match: Mapping):
        from strategy_platform.council import params as cp
        from strategy_platform.council import store
        from strategy_platform.execution import accounts
        from strategy_platform.storage import db
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"})
        errors: dict = {}
        reason = _note(b, errors)
        payload = {k: v for k, v in b.items() if k != "reason"}

        def write():
            if not accounts.exists(accounts.COUNCIL):
                return _err(404, "not_found", "The account paper-council does not exist (migration not done).")
            acc = accounts.get(accounts.COUNCIL)
            ov = store.latest_broker_override()
            eff = cp.effective_broker(acc.params, ov[1] if ov else None)
            clean, errs = cp.validate_broker(payload, eff)
            errs.update(errors)
            if errs:
                return _bad(errs)
            new_ov = dict(ov[1] if ov else {}, **clean)
            with db._connect() as conn:
                conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, 'user', "
                             "'council_broker_params', ?)", (db.utc_now_iso(), db._dumps({"changes": clean,
                                                                                           "reason": reason})))
                v = store.add_broker_override(new_ov, "user", reason or None, conn=conn)
            accounts.reset_cache()
            return 200, {"version": v, "override": new_ov, "effective": cp.effective_broker(acc.params, new_ov)}
        try:
            return await asyncio.to_thread(write)
        except Exception:      # noqa: BLE001
            logger.exception("PUT /v2/council/broker-params failed")
            return _err(503, "audit_unavailable", "The change was not stored (database unavailable).")

    async def council_orders(self, query: Mapping, body, match: Mapping):
        from strategy_platform.council import store
        from strategy_platform.execution import accounts, control
        from strategy_platform.storage import db
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object {enabled, reason?}"})
        errors = _unknown(b, ("enabled", "reason"))
        reason = _note(b, errors)
        enabled = b.get("enabled")
        if not isinstance(enabled, bool):
            errors["enabled"] = "true or false"
        if errors:
            return _bad(errors)

        def go():
            if not enabled:          # turning OFF always works (audit failure logged)
                n = control.council_orders_off("user", "user") or 0
                return 200, {"orders_enabled": False, "cancelled_orders": n}
            if store.switchover() != "done":
                return _err(409, "not_active", "The Council is not switched over yet.")
            if accounts.exists(accounts.COUNCIL) and accounts.get(accounts.COUNCIL).halt()["active"]:
                return _err(409, "halted", "paper-council is halted — resume it first.")
            try:
                aid = db.add_audit("user", "council_orders_on", {"reason": reason})
            except Exception:      # noqa: BLE001 — audit-first
                return _err(503, "audit_unavailable", "audit log unavailable — the action was not carried out")
            store.set_orders_enabled(True, "user", "user")
            return 200, {"orders_enabled": True, "audit_id": aid}
        return await asyncio.to_thread(go)

    # --- R2 (SP §22.8) ------------------------------------------------------------------------------------------
    @staticmethod
    def _reason(b: dict, errors: dict) -> str:
        errors.update(_unknown(b, ("reason",)))
        return _note(b, errors)

    async def council_switchover_get(self, query: Mapping, body, match: Mapping):
        from strategy_platform.council import switchover
        from strategy_platform.execution import control
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        try:
            return 200, await asyncio.to_thread(switchover.dry_run, control.legacy_engine())
        except switchover.SwitchoverError as ex:
            return ex.status, ex.payload()

    async def council_switchover_post(self, query: Mapping, body, match: Mapping):
        from strategy_platform.council import switchover
        from strategy_platform.execution import control
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object {reason?}"})
        errors: dict = {}
        reason = self._reason(b, errors)
        if errors:
            return _bad(errors)
        try:
            return 200, await switchover.run(reason, "user", control.legacy_engine())
        except switchover.SwitchoverError as ex:
            return ex.status, ex.payload()

    async def _switchover_op(self, query, body, op_name: str):
        from strategy_platform.council import switchover
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object {reason?}"})
        errors: dict = {}
        reason = self._reason(b, errors)
        if errors:
            return _bad(errors)
        try:
            return 200, await getattr(switchover, op_name)(reason, "user")
        except switchover.SwitchoverError as ex:
            return ex.status, ex.payload()

    async def council_switchover_revert(self, query: Mapping, body, match: Mapping):
        return await self._switchover_op(query, body, "revert")

    async def council_switchover_retry(self, query: Mapping, body, match: Mapping):
        """R2 review M2: the SAME unfinished attempt again (scratchpad/mr/SWITCHOVER_FIX_API.md §3)."""
        return await self._switchover_op(query, body, "retry")

    async def council_switchover_abort(self, query: Mapping, body, match: Mapping):
        """R2 review M2: give up the unfinished attempt, back to the old engine (SWITCHOVER_FIX_API.md §5)."""
        return await self._switchover_op(query, body, "abort")


COUNCIL_ROUTES = (
    ("GET", "/v2/council/state", "council_state", False),
    ("GET", "/v2/council/runs", "council_runs", False),
    ("GET", "/v2/council/runs/{id}", "council_run_detail", False),
    ("POST", "/v2/council/run", "council_run", True),
    ("GET", "/v2/council/params", "council_params_get", False),
    ("PUT", "/v2/council/params", "council_params_put", True),
    ("GET", "/v2/council/broker-params", "council_broker_params_get", False),
    ("PUT", "/v2/council/broker-params", "council_broker_params_put", True),
    ("PUT", "/v2/council/orders", "council_orders", True),
    ("GET", "/v2/council/switchover", "council_switchover_get", False),
    ("POST", "/v2/council/switchover", "council_switchover_post", True),
    ("POST", "/v2/council/switchover/revert", "council_switchover_revert", True),
    ("POST", "/v2/council/switchover/retry", "council_switchover_retry", True),
    ("POST", "/v2/council/switchover/abort", "council_switchover_abort", True),
)
