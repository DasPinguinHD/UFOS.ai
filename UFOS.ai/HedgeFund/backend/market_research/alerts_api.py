"""MR7 REST routes (SPEC §4.2, exact JSON in §13 "MR7 backend notes") — mixed into ``api.ResearchApi``:

``GET /research/alerts``, ``POST /research/alerts``, ``PATCH /research/alerts/{id}``,
``DELETE /research/alerts/{id}``, ``GET /research/alerts/log?limit=&alert_id=``.

Unknown query parameters / body fields → 400 ``{error: "invalid input", errors}``; unknown id → 404; limit /
duplicate / linked line conflicts → 409 ``{error}``; body > ``MAX_ALERT_BODY_BYTES`` → 400. Store work runs on
the Research pool (inside research priority: a new symbol may refresh the asset list). Every answer's alert
objects carry the engine's live ``status`` (computed on the event loop, no I/O).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import json

from strategy_platform.api.routes import NO_BODY

from . import alerts as al
from . import limits
from .data_api import _int, _one, _research, _unknown
from .executor import run_blocking


def _bad(errors: dict, message: str = "invalid input"):
    return 400, {"error": message, "errors": errors}


def _size_ok(body) -> bool:
    try:
        return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= limits.MAX_ALERT_BODY_BYTES
    except (TypeError, ValueError):
        return False


class AlertRoutes:
    """Mixin: needs ``self.service.alerts`` (``alerts.AlertEngine``) and ``self.service.data``."""

    def _alert_obj(self, row: dict) -> dict:
        out = {k: row[k] for k in ("id", "symbol", "kind", "params", "cooldown_seconds", "enabled", "drawing_id",
                                   "last_triggered_at", "disabled_reason", "created_at", "updated_at")}
        out["status"] = self.service.alerts.status(row)
        return out

    async def _al_guard(self, coro_fn):
        try:
            return await self._guard(coro_fn)
        except al.AlertInvalid as ex:
            return _bad(ex.errors)
        except al.AlertConflict as ex:
            return 409, {"error": ex.user_message}
        except al.AlertNotFound as ex:
            return 404, {"error": ex.user_message}

    def _limits(self) -> dict:
        lo, hi = limits.ALERT_COOLDOWN_RANGE
        return {"max_alerts": limits.MAX_ALERTS, "cooldown_default": limits.ALERT_COOLDOWN_SECONDS,
                "cooldown_min": lo, "cooldown_max": hi, "day_move_pct_min": limits.ALERT_DAY_MOVE_PCT[0],
                "day_move_pct_max": limits.ALERT_DAY_MOVE_PCT[1]}

    # GET /research/alerts
    async def alerts_list(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            rows = await run_blocking(self.service.alerts.store.alerts_list)
            return 200, {"alerts": [self._alert_obj(r) for r in rows], "limits": self._limits(),
                         "runtime": self.service.alerts.runtime()}
        return await self._al_guard(go)

    # POST /research/alerts {symbol, kind, params, cooldown_seconds?, enabled?}
    async def alert_create(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            if body is NO_BODY or not isinstance(body, dict):
                return _bad({"_": "a JSON object body is required"})
            if not _size_ok(body):
                return _bad({"_": f"body larger than {limits.MAX_ALERT_BODY_BYTES} bytes"})
            eng = self.service.alerts
            row = await _research(lambda: al.create(self.service.data, body, store=eng.store, now=eng._now()))
            eng.poke()
            return 201, self._alert_obj(row)
        return await self._al_guard(go)

    # PATCH /research/alerts/{id} {params?, cooldown_seconds?, enabled?}
    async def alert_update(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            if body is NO_BODY or not isinstance(body, dict):
                return _bad({"_": "a JSON object body is required"})
            if not _size_ok(body):
                return _bad({"_": f"body larger than {limits.MAX_ALERT_BODY_BYTES} bytes"})
            eng = self.service.alerts
            row = await run_blocking(lambda: al.update(str(match.get("id", "")), body, store=eng.store, now=eng._now()))
            eng.poke()
            return 200, self._alert_obj(row)
        return await self._al_guard(go)

    # DELETE /research/alerts/{id}
    async def alert_delete(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            eng = self.service.alerts
            aid = str(match.get("id", ""))
            await run_blocking(lambda: al.delete(aid, store=eng.store))
            eng.poke()
            return 200, {"deleted": aid}
        return await self._al_guard(go)

    # GET /research/alerts/log?limit=&alert_id=
    async def alerts_log(self, query, body, match):
        async def go():
            bad = _unknown(query, ("limit", "alert_id"))
            if bad:
                return _bad(bad)
            limit = _int(query, "limit", 1, limits.ALERT_LOG_LIMIT_MAX) or limits.ALERT_LOG_LIMIT_DEFAULT
            aid = _one(query, "alert_id")
            if aid is not None and not al.ID_RE.match(aid):
                return _bad({"alert_id": "must be a 32-character lowercase hex id"})
            items = await run_blocking(self.service.alerts.store.alert_log_list, limit, aid)
            return 200, {"items": items, "limit": limit, "retention_days": limits.RETENTION_DAYS["alert_log"]}
        return await self._al_guard(go)


ALERT_ROUTES = (
    ("GET", "/research/alerts", "alerts_list", False),
    ("POST", "/research/alerts", "alert_create", True),
    ("GET", "/research/alerts/log", "alerts_log", False),
    ("PATCH", "/research/alerts/{id}", "alert_update", True),
    ("DELETE", "/research/alerts/{id}", "alert_delete", False),
)
