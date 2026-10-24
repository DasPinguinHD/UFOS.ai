"""MR4 REST routes of the symbol workspace (SPEC §4.2, exact JSON in §13 "MR4 backend notes") — mixed into
``api.ResearchApi`` (handler shape ``(query, body, match) -> (status, payload)``, same error wrapper).

Watchlists (CRUD, ordering, items, live quotes), session restore, recent symbols and the Ctrl+K palette.
Strict validation: unknown query parameters / body fields, wrong types → 400; bodies must be JSON objects ≤
``MAX_WORKSPACE_BODY_BYTES``; ids 32-char hex (else 404). Blocking work (SQLite, an asset-list refresh, the
prev-close lookups) runs on the Research pool inside ``http_gate.research_priority()``; after every watchlist
change the hub's ``watchlist:<id>`` expansion is updated on the loop (``watchlists.Workspace``).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from strategy_platform.api.routes import NO_BODY, dumps

from . import limits
from . import session as session_mod
from . import watchlists as wl
from .assets import QUERY_RE, SymbolNotCovered
from .data_api import _int, _one, _research, _unknown
from .bars.series import BadParam
from .stats import quote as quote_mod

PATCH_KEYS = {"name": str, "collapsed": bool, "position": int}


def _bad(errors: dict, message: str = "invalid input"):
    return 400, {"error": message, "errors": errors}


def _type_ok(value, typ) -> bool:
    if typ is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, typ)


def _object_body(body, allowed: dict, required=()):
    """Checks a JSON object body: exactly known keys of the right type (raises a 400 WorkspaceError)."""
    if body is NO_BODY or not isinstance(body, dict):
        raise wl.bad("_", "a JSON object body is required")
    if len(dumps(body).encode("utf-8")) > limits.MAX_WORKSPACE_BODY_BYTES:
        raise wl.bad("_", f"body larger than {limits.MAX_WORKSPACE_BODY_BYTES} bytes")
    errors = {str(k)[:40]: "unknown field" for k in list(body) if k not in allowed}
    for k in required:
        if k not in body:
            errors[k] = "required"
    for k, typ in allowed.items():
        if k in body and typ is not None and not _type_ok(body[k], typ):
            errors[k] = {str: "must be a string", bool: "must be true or false", int: "must be an integer",
                         list: "must be a list"}.get(typ, "wrong type")
    if errors:
        raise wl.WorkspaceError(400, {"error": "invalid input", "errors": dict(list(errors.items())[:20])})
    return body


class WorkspaceRoutes:
    """Mixin: needs ``self.service.data`` (``DataService``), ``self.service.hub`` and ``self.service.workspace``."""

    async def _ws_guard(self, coro_fn):
        try:
            return await coro_fn()
        except wl.WorkspaceError as ex:
            return ex.status, ex.payload
        except BadParam as ex:
            return _bad({ex.field: ex.user_message})
        except SymbolNotCovered as ex:
            return 404, {"error": ex.user_message, "kind": "SymbolNotCovered"}

    def _require(self):
        data = self.service.data
        return data.asset                      # assets.require with the current sources (404 / 503)

    def _workspace(self):
        return getattr(self.service, "workspace", None)

    def _applied(self, seq, lists, extra=None):
        ws = self._workspace()
        if ws is not None:
            ws.apply(seq, lists)
        out = wl.payload(lists)
        if extra:
            out = dict(extra, **out)
        return out

    @staticmethod
    def _no_query(query):
        bad = _unknown(query, ())
        return _bad(bad) if bad else None

    # GET /research/watchlists
    async def watchlists_list(self, query: Mapping, body, match: Mapping):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            seq, lists = await _research(wl.list_all)
            return 200, self._applied(seq, lists)
        return await self._ws_guard(go)

    # POST /research/watchlists {name}
    async def watchlist_create(self, query, body, match):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            b = _object_body(body, {"name": str}, required=("name",))
            wl.check_name(b["name"])
            new_id, seq, lists = await _research(wl.create, b["name"])
            return 201, self._applied(seq, lists, {"id": new_id})
        return await self._ws_guard(go)

    # PATCH /research/watchlists/{id} {name?, collapsed?, position?}
    async def watchlist_patch(self, query, body, match):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            list_id = wl.check_id(match.get("id"))
            b = _object_body(body, PATCH_KEYS)
            if not b:
                raise wl.bad("_", "give at least one of name, collapsed, position")
            if "name" in b:
                wl.check_name(b["name"])
            seq, lists = await _research(wl.patch, list_id, dict(b))
            return 200, self._applied(seq, lists)
        return await self._ws_guard(go)

    # DELETE /research/watchlists/{id}
    async def watchlist_delete(self, query, body, match):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            list_id = wl.check_id(match.get("id"))
            seq, lists = await _research(wl.delete, list_id)
            return 200, self._applied(seq, lists)
        return await self._ws_guard(go)

    # PUT /research/watchlists/{id}/items {symbols}
    async def watchlist_items_put(self, query, body, match):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            list_id = wl.check_id(match.get("id"))
            b = _object_body(body, {"symbols": list}, required=("symbols",))
            symbols = wl.normalise_symbols(b["symbols"])
            seq, lists = await _research(wl.replace_items, list_id, symbols, self._require())
            return 200, self._applied(seq, lists)
        return await self._ws_guard(go)

    # POST /research/watchlists/{id}/items {symbol, position?}
    async def watchlist_item_add(self, query, body, match):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            list_id = wl.check_id(match.get("id"))
            b = _object_body(body, {"symbol": None, "position": int}, required=("symbol",))
            sym, _ac = wl.check_symbol(b["symbol"])
            seq, lists = await _research(wl.add_item, list_id, sym, b.get("position"), self._require())
            return 200, self._applied(seq, lists)
        return await self._ws_guard(go)

    # DELETE /research/watchlists/{id}/items/{symbol}
    async def watchlist_item_remove(self, query, body, match):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            list_id = wl.check_id(match.get("id"))
            sym, _ac = wl.check_symbol(match.get("symbol"))
            seq, lists = await _research(wl.remove_item, list_id, sym)
            return 200, self._applied(seq, lists)
        return await self._ws_guard(go)

    # GET /research/watchlists/{id}/quotes
    async def watchlist_quotes(self, query, body, match):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            list_id = wl.check_id(match.get("id"))
            wlist = await _research(wl.get_list, list_id)
            hub = self.service.hub
            states, wanted = {}, []
            for it in wlist["items"]:
                sym = it["symbol"]
                if not it["covered"]:
                    from .assets import not_covered
                    states[sym] = {"live": False, "limited": False, "last": None,
                                   "reason": not_covered(sym, it["asset_class"] or "equity").user_message}
                    continue
                st = hub.quote_state(sym)
                states[sym] = st
                if not st["limited"]:
                    wanted.append((sym, it["asset_class"]))
            data = self.service.data
            prevs, complete = {}, True
            if wanted:
                prevs, complete = await _research(quote_mod.prev_closes, wanted, data.loader, data.sources,
                                                  data.now(), self._prev_cache())
            items = [quote_mod.item(it, states[it["symbol"]], prevs.get(it["symbol"])) for it in wlist["items"]]
            return 200, {"id": list_id,
                         "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "complete": complete,
                         "retry_after_seconds": None if complete else limits.QUOTE_RETRY_SECONDS,
                         "stream_limit": limits.STOCK_STREAM_SYMBOL_LIMIT, "note": quote_mod.NOTE, "items": items}
        return await self._ws_guard(go)

    def _prev_cache(self) -> quote_mod.PrevCloseCache:
        cache = getattr(self.service, "prev_close_cache", None)
        if cache is None:
            cache = quote_mod.PrevCloseCache()
            self.service.prev_close_cache = cache
        return cache

    # GET /research/session
    async def session_get(self, query, body, match):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            return 200, await _research(session_mod.get)
        return await self._ws_guard(go)

    # PUT /research/session {tabs, active, sidebar}
    async def session_put(self, query, body, match):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            b = _object_body(body, {"tabs": None, "active": None, "sidebar": None})
            state = session_mod.check_state(b)
            return 200, await _research(session_mod.put, state)
        return await self._ws_guard(go)

    # POST /research/recent {symbol}
    async def recent_add(self, query, body, match):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            b = _object_body(body, {"symbol": None}, required=("symbol",))
            sym, _ac = wl.check_symbol(b["symbol"])
            return 200, {"recent": await _research(session_mod.add_recent, sym, self._require())}
        return await self._ws_guard(go)

    # DELETE /research/recent
    async def recent_clear(self, query, body, match):
        async def go():
            bad = self._no_query(query)
            if bad:
                return bad
            return 200, {"recent": await _research(session_mod.clear_recent)}
        return await self._ws_guard(go)

    # GET /research/palette?q=&limit=
    async def palette(self, query, body, match):
        async def go():
            bad = _unknown(query, ("q", "limit"))
            if bad:
                return _bad(bad)
            q = _one(query, "q")
            if q is not None and q.strip() and not QUERY_RE.match(q):
                raise BadParam("q", f"0–{limits.ASSET_QUERY_MAX_CHARS} characters: letters, digits, space, . - /")
            if q is not None and len(q) > limits.ASSET_QUERY_MAX_CHARS:
                raise BadParam("q", f"0–{limits.ASSET_QUERY_MAX_CHARS} characters: letters, digits, space, . - /")
            limit = _int(query, "limit", 1, limits.ASSET_LIMIT_MAX) or limits.ASSET_LIMIT_DEFAULT
            data = self.service.data
            now = data.now()
            return 200, await _research(lambda: session_mod.palette(q, limit, data.sources, now=now))
        return await self._ws_guard(go)


WORKSPACE_ROUTES = (
    ("GET", "/research/watchlists", "watchlists_list", False),
    ("POST", "/research/watchlists", "watchlist_create", True),
    ("PATCH", "/research/watchlists/{id}", "watchlist_patch", True),
    ("DELETE", "/research/watchlists/{id}", "watchlist_delete", False),
    ("PUT", "/research/watchlists/{id}/items", "watchlist_items_put", True),
    ("POST", "/research/watchlists/{id}/items", "watchlist_item_add", True),
    ("DELETE", "/research/watchlists/{id}/items/{symbol}", "watchlist_item_remove", False),
    ("GET", "/research/watchlists/{id}/quotes", "watchlist_quotes", False),
    ("GET", "/research/session", "session_get", False),
    ("PUT", "/research/session", "session_put", True),
    ("POST", "/research/recent", "recent_add", True),
    ("DELETE", "/research/recent", "recent_clear", False),
    ("GET", "/research/palette", "palette", False),
)
