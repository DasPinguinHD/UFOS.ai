"""MR5 REST routes (SPEC §4.2, exact JSON in §13 "MR5 backend notes") — mixed into ``api.ResearchApi``
(handler shape ``(query, body, match) -> (status, payload)``, same error wrapper in ``api.register``):

- drawings CRUD, delete-all, export/import (``drawings.py``),
- chart settings (``chart_settings.py``),
- indicator series ``/research/symbols/{symbol}/indicators`` (``indicators.py``, DSL definitions — Q8).

Unknown query parameters / body fields → 400; ids 32-char hex (else 404); bodies are size-checked
(compact JSON). Blocking work runs on the Research pool (``executor.run_blocking``); the indicator route
(it may fetch bars from Alpaca) inside ``http_gate.research_priority()``.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

from typing import Mapping

from strategy_platform.api.routes import NO_BODY
from strategy_platform.core.clock import to_utc

from . import chart_settings as cs
from . import drawings as dr
from . import indicators as ind
from . import limits
from .assets import SymbolNotCovered
from .bars import aggregate as agg
from .bars.series import BadParam, max_limit
from .data_api import _int, _one, _research, _symbol, _unknown
from .executor import run_blocking

BODY_SLACK = 2048       # POST/PATCH body may carry a little more than the drawing content (symbol, flags)


def _bad(errors: dict, message: str = "invalid input"):
    return 400, {"error": message, "errors": errors}


def _sym_param(query) -> str:
    s = _one(query, "symbol")
    if s is None:
        raise BadParam("symbol", "required")
    try:
        return dr.symbol_of(s).symbol
    except ValueError:
        raise BadParam("symbol", "not a supported symbol (stocks/ETFs like NVDA, BRK.B; crypto like BTC-USD)")


def _body(body, max_bytes: int):
    if body is NO_BODY or not isinstance(body, dict):
        raise dr.DrawingInvalid({"_": "a JSON object body is required"})
    n = dr.compact_size(body)
    if n > max_bytes:
        raise dr.DrawingInvalid({"_": f"body larger than {max_bytes} bytes"})
    return body


class DrawingRoutes:
    """Mixin: needs ``self.service.data`` (``DataService``)."""

    async def _dr_guard(self, coro_fn):
        try:
            return await coro_fn()
        except dr.DrawingInvalid as ex:
            return _bad(ex.errors, ex.user_message)
        except BadParam as ex:
            return _bad({ex.field: ex.user_message})
        except dr.DrawingNotFound as ex:
            return 404, {"error": str(ex) or dr.NOT_FOUND}
        except dr.DrawingConflict as ex:
            return 409, {"error": ex.message}
        except SymbolNotCovered as ex:
            return 404, {"error": ex.user_message, "kind": "SymbolNotCovered"}

    # GET /research/drawings?symbol=
    async def drawings_list(self, query, body, match):
        async def go():
            bad = _unknown(query, ("symbol",))
            if bad:
                return _bad(bad)
            res = await run_blocking(dr.list_drawings, _sym_param(query))
            if dr.needs_check(res["split_check"]):
                dr.request_check()             # review L9: not checked today yet → the supervised task checks now
            return 200, res
        return await self._dr_guard(go)

    # POST /research/drawings
    async def drawing_create(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            b = _body(body, limits.MAX_DRAWING_JSON_BYTES + BODY_SLACK)
            created, d = await run_blocking(dr.create_ex, b)
            return (201 if created else 200), d        # 200 = a retried POST with the same client_id
        return await self._dr_guard(go)

    # PATCH /research/drawings/{id}
    async def drawing_patch(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            b = _body(body, limits.MAX_DRAWING_JSON_BYTES + BODY_SLACK)
            return 200, await run_blocking(dr.patch, match.get("id"), b)
        return await self._dr_guard(go)

    # DELETE /research/drawings/{id}
    async def drawing_delete(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            return 200, await run_blocking(dr.delete, match.get("id"))
        return await self._dr_guard(go)

    # DELETE /research/drawings?symbol=&confirm=true
    async def drawings_delete_all(self, query, body, match):
        async def go():
            bad = _unknown(query, ("symbol", "confirm"))
            if bad:
                return _bad(bad)
            sym = _sym_param(query)
            # fix1023: ``confirm`` accepted and ignored (the app's dialog summary is the confirmation)
            return 200, await run_blocking(dr.delete_all, sym)
        return await self._dr_guard(go)

    # GET /research/drawings/export?symbol=
    async def drawings_export(self, query, body, match):
        async def go():
            bad = _unknown(query, ("symbol",))
            if bad:
                return _bad(bad)
            return 200, await run_blocking(dr.export, _sym_param(query))
        return await self._dr_guard(go)

    # POST /research/drawings/import?symbol=&mode=append|replace
    async def drawings_import(self, query, body, match):
        async def go():
            bad = _unknown(query, ("symbol", "mode", "client_id"))
            if bad:
                return _bad(bad)
            sym = _sym_param(query)
            mode = _one(query, "mode") or "append"
            if mode not in ("append", "replace"):
                raise BadParam("mode", "append or replace")
            if body is NO_BODY or not isinstance(body, dict):
                raise dr.DrawingInvalid({"_": "the drawings file (a JSON object) is required"},
                                        "Not a UFOS.ai drawings file.")
            cid = _one(query, "client_id")
            res = await run_blocking(lambda: dr.import_file(body, sym, mode, client_id=cid))
            if not res.get("repeated"):
                dr.request_check()             # a split since the export is applied at once
            return 200, res
        return await self._dr_guard(go)

    # GET /research/chart-settings?symbol=
    async def chart_settings_get(self, query, body, match):
        async def go():
            bad = _unknown(query, ("symbol",))
            if bad:
                return _bad(bad)
            return 200, await run_blocking(cs.get, _sym_param(query))
        return await self._dr_guard(go)

    # PUT /research/chart-settings?symbol=
    async def chart_settings_put(self, query, body, match):
        async def go():
            bad = _unknown(query, ("symbol",))
            if bad:
                return _bad(bad)
            sym = _sym_param(query)
            b = _body(body, 4096)
            return 200, await run_blocking(cs.put, sym, b)
        return await self._dr_guard(go)

    # GET /research/symbols/{symbol}/indicators?tf=&session=&before=&limit=&ind=
    async def symbol_indicators(self, query, body, match):
        async def go():
            bad = _unknown(query, ("tf", "session", "before", "limit", "ind", "cmp_base"))
            if bad:
                return _bad(bad)
            inst = _symbol(match)
            tf = _one(query, "tf")
            if tf not in agg.TIMEFRAMES:
                raise BadParam("tf", "must be one of " + " ".join(agg.TIMEFRAMES))
            session = _one(query, "session")
            if session is not None:
                if inst.asset_class == "crypto":
                    raise BadParam("session", "applies to stocks/ETFs only (crypto bars are UTC aligned)")
                if session not in agg.SESSIONS:
                    raise BadParam("session", "must be rth or eth")
            before = _one(query, "before")
            if before is not None:
                if len(before) > 40:
                    raise BadParam("before", "not an ISO-8601 time")
                try:
                    before = to_utc(before)
                except (ValueError, TypeError):
                    raise BadParam("before", "not an ISO-8601 time")
            limit = _int(query, "limit", 1, max_limit(tf))
            spec_text = _one(query, "ind")
            if spec_text is None:
                raise BadParam("ind", "required: comma-separated indicator specs (e.g. sma:20,rsi:14)")
            specs = ind.parse_list(spec_text, inst.symbol)
            cmp_base = _one(query, "cmp_base")
            if cmp_base is not None:
                if not any(s.kind == "cmp" for s in specs):
                    raise BadParam("cmp_base", "only together with a cmp:<SYMBOL> indicator")
                if len(cmp_base) > 40:
                    raise BadParam("cmp_base", "not an ISO-8601 time")
                try:
                    cmp_base = to_utc(cmp_base)
                except (ValueError, TypeError):
                    raise BadParam("cmp_base", "not an ISO-8601 time")
            return 200, await _research(ind.compute, self.service.data, inst.symbol, tf, session, before, limit,
                                        specs, cmp_base)

        async def guarded():
            try:
                return await go()
            except BadParam as ex:
                return _bad({ex.field: ex.user_message},
                            ex.user_message if ex.field == "before" else "invalid input")
        return await self._dr_guard(guarded)


DRAWING_ROUTES = (
    ("GET", "/research/drawings/export", "drawings_export", False),
    ("POST", "/research/drawings/import", "drawings_import", True),
    ("GET", "/research/drawings", "drawings_list", False),
    ("POST", "/research/drawings", "drawing_create", True),
    ("DELETE", "/research/drawings", "drawings_delete_all", False),
    ("PATCH", "/research/drawings/{id}", "drawing_patch", True),
    ("DELETE", "/research/drawings/{id}", "drawing_delete", False),
    ("GET", "/research/chart-settings", "chart_settings_get", False),
    ("PUT", "/research/chart-settings", "chart_settings_put", True),
    ("GET", "/research/symbols/{symbol}/indicators", "symbol_indicators", False),
)
