"""HTTP API of the HedgeFund loop: /fund/* routes (fund/SPEC.md §11).

PAPER TRADING ONLY — every order is simulated; nothing is investment advice.
All routes sit behind main.py's ``api_auth`` middleware (session token +
origin check). Every state-changing call writes a ``fund_audit`` row (actor
"user") — the engine methods do that; when the audit log is unavailable they
answer 503 "audit log unavailable" (the kill switch always works). GET
/fund/state and GET /fund/events count as client heartbeat
(``engine.touch_client()``, dead-man switch of the autopilot). Responses never contain NaN/Infinity
(``json_safe`` + ``allow_nan=False``; the .NET client rejects them).

Two layers so the handlers can be tested without aiohttp:
- ``FundApi(engine)``: one ``async`` method per route taking
  ``(query: Mapping, body, match: Mapping)`` and returning
  ``(status, payload)`` or ``(status, CsvBody)``;
- ``register(app, engine)``: adds the aiohttp routes (aiohttp imported
  lazily) that parse the request, call the handler and serialise.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple

from . import export
from .engine import EngineError, json_safe

logger = logging.getLogger(__name__)

_NO_BODY = object()


class CsvBody:
    def __init__(self, filename: str, text: str):
        self.filename = filename
        self.text = text


def dumps(obj) -> str:
    """json.dumps for web.json_response: sanitised, strict (no NaN)."""
    return json.dumps(json_safe(obj), allow_nan=False, ensure_ascii=False)


def _bad(errors: dict, message: str = "invalid input") -> Tuple[int, dict]:
    return 400, {"error": message, "errors": errors}


def _int_query(query: Mapping, key: str, default: int, lo: int, hi: int):
    raw = query.get(key) if query is not None else None
    if raw in (None, ""):
        return default, None
    try:
        v = int(str(raw))
    except ValueError:
        return None, {key: "must be an integer"}
    if not lo <= v <= hi:
        return None, {key: f"must be between {lo} and {hi}"}
    return v, None


def _body_dict(body, allow_empty: bool = True):
    if body is _NO_BODY or body is None:
        return ({} if allow_empty else None), None
    if not isinstance(body, dict):
        return None, {"_": "expected a JSON object"}
    return body, None


class FundApi:
    """Route handlers (see module docstring)."""

    def __init__(self, engine):
        self.engine = engine

    async def _call(self, fn, *args, thread: bool = False):
        try:
            if thread:
                result = await asyncio.to_thread(fn, *args)
            else:
                result = fn(*args)
                if asyncio.iscoroutine(result):
                    result = await result
            return 200, result
        except EngineError as ex:
            payload = {"error": ex.message}
            if ex.errors:
                payload["errors"] = ex.errors
            return ex.status, payload
        except Exception as ex:
            # broker.StateUnavailable (cash ledger / positions unreadable) -> 503, not a generic 500
            if type(ex).__name__ == "StateUnavailable":
                logger.error("fund route refused: paper account state unreadable (%s)", ex)
                return 503, {"error": "The paper account state is unreadable (database) — try again later."}
            raise

    def _touch(self) -> None:
        """Client heartbeat (the route is only reached with a valid session
        token — main.py's api_auth middleware runs first)."""
        touch = getattr(self.engine, "touch_client", None)
        if touch is not None:
            try:
                touch()
            except Exception:
                logger.exception("touch_client failed")

    # --- state / params / control -----------------------------------------------

    async def state(self, query, body, match):
        self._touch()
        return await self._call(self.engine.state, thread=True)

    async def params_get(self, query, body, match):
        return await self._call(self.engine.get_params, thread=True)

    async def params_put(self, query, body, match):
        b, err = _body_dict(body, allow_empty=False)
        if err or b is None:
            return _bad(err or {"_": "expected a JSON object with parameters"})
        return await self._call(self.engine.update_params, b, "user", thread=True)

    async def run(self, query, body, match):
        return await self._call(self.engine.request_run, "user")

    async def autopilot(self, query, body, match):
        b, err = _body_dict(body, allow_empty=False)
        if err or b is None or not isinstance(b.get("enabled"), bool):
            return _bad(err or {"enabled": "required: true or false"})
        return await self._call(self.engine.set_autopilot, b["enabled"], "user", thread=True)

    async def kill(self, query, body, match):
        b, err = _body_dict(body)
        if err:
            return _bad(err)
        reason = b.get("reason")
        if reason is not None and not isinstance(reason, str):
            return _bad({"reason": "must be a string"})
        return await self._call(self.engine.kill, (reason or "")[:200] or None, "user")

    async def resume(self, query, body, match):
        return await self._call(self.engine.resume, "user", thread=True)

    async def reset(self, query, body, match):
        b, err = _body_dict(body)
        if err:
            return _bad(err)
        return await self._call(self.engine.reset, b.get("starting_capital"), "user")

    # --- history -------------------------------------------------------------------

    async def runs(self, query, body, match):
        limit, err = _int_query(query, "limit", 50, 1, 500)
        if err:
            return _bad(err)
        return 200, await asyncio.to_thread(self.engine.store.list_runs, limit)

    async def run_detail(self, query, body, match):
        try:
            rid = int(match.get("id"))
        except (TypeError, ValueError):
            return _bad({"id": "must be an integer"})
        data = await asyncio.to_thread(self.engine.store.get_run, rid)
        if data is None:
            return 404, {"error": f"Run #{rid} not found."}
        return 200, data

    async def orders(self, query, body, match):
        limit, err = _int_query(query, "limit", 100, 1, 5000)
        if err:
            return _bad(err)
        return 200, await asyncio.to_thread(self.engine.store.list_orders, limit)

    async def orders_post(self, query, body, match):
        b, err = _body_dict(body, allow_empty=False)
        if err or b is None:
            return _bad(err or {"_": "expected {symbol, side, qty|notional}"})
        return await self._call(self.engine.manual_order, b, "user")

    async def execute(self, query, body, match):
        return await self._call(self.engine.execute_recommendation, match.get("id"), "user")

    async def equity(self, query, body, match):
        since = (query or {}).get("since")
        if since:
            try:
                dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
            except ValueError:
                return _bad({"since": "must be an ISO-8601 timestamp"})
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            since = dt
        return await self._call(self.engine.equity, since or None, thread=True)

    async def events(self, query, body, match):
        after, err = _int_query(query, "after_id", 0, 0, 2 ** 62)
        if err:
            return _bad(err)
        self._touch()
        return 200, await asyncio.to_thread(self.engine.store.events_after, after, 50)

    async def audit(self, query, body, match):
        limit, err = _int_query(query, "limit", 100, 1, 1000)
        if err:
            return _bad(err)
        return 200, await asyncio.to_thread(self.engine.store.list_audit, limit)

    # --- backtests / exports -------------------------------------------------------------

    async def backtest(self, query, body, match):
        b, err = _body_dict(body)
        if err:
            return _bad(err)
        return await self._call(self.engine.backtest, b, "user")

    async def backtests(self, query, body, match):
        limit, err = _int_query(query, "limit", 10, 1, 100)
        if err:
            return _bad(err)
        return 200, await asyncio.to_thread(self.engine.store.list_backtests, limit)

    async def backtest_detail(self, query, body, match):
        try:
            bid = int(match.get("id"))
        except (TypeError, ValueError):
            return _bad({"id": "must be an integer"})
        data = await asyncio.to_thread(self.engine.store.get_backtest, bid)
        if data is None:
            return 404, {"error": f"Backtest #{bid} not found."}
        return 200, data

    async def export_orders(self, query, body, match):
        text = await asyncio.to_thread(export.orders_csv, self.engine.store)
        return 200, CsvBody("orders.csv", text)

    async def export_decisions(self, query, body, match):
        text = await asyncio.to_thread(export.decisions_csv, self.engine.store)
        return 200, CsvBody("decisions.csv", text)


# (method, path, handler name, reads a JSON body)
ROUTES = (
    ("GET", "/fund/state", "state", False),
    ("GET", "/fund/params", "params_get", False),
    ("PUT", "/fund/params", "params_put", True),
    ("POST", "/fund/run", "run", True),
    ("POST", "/fund/autopilot", "autopilot", True),
    ("POST", "/fund/kill", "kill", True),
    ("POST", "/fund/resume", "resume", True),
    ("POST", "/fund/reset", "reset", True),
    ("GET", "/fund/runs", "runs", False),
    ("GET", "/fund/runs/{id}", "run_detail", False),
    ("GET", "/fund/orders", "orders", False),
    ("POST", "/fund/orders", "orders_post", True),
    ("POST", "/fund/recommendations/{id}/execute", "execute", True),
    ("GET", "/fund/equity", "equity", False),
    ("GET", "/fund/events", "events", False),
    ("GET", "/fund/audit", "audit", False),
    ("POST", "/fund/backtest", "backtest", True),
    ("GET", "/fund/backtests", "backtests", False),
    ("GET", "/fund/backtests/{id}", "backtest_detail", False),
    ("GET", "/fund/export/orders.csv", "export_orders", False),
    ("GET", "/fund/export/decisions.csv", "export_decisions", False),
)


async def _read_body(request):
    """Parsed JSON body, _NO_BODY when empty; raises ValueError on bad JSON."""
    if not request.can_read_body:
        return _NO_BODY
    text = await request.text()
    if not text.strip():
        return _NO_BODY
    return json.loads(text)


def to_response(web, status: int, payload):
    if isinstance(payload, CsvBody):
        return web.Response(text=payload.text, status=status, content_type="text/csv", charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{payload.filename}"',
                                     "Cache-Control": "no-store"})
    return web.json_response(payload, status=status, dumps=dumps)


MODE_PATHS = frozenset({"/fund/state"})        # sandbox profile: the main state payload carries "mode"


def register(app, engine) -> FundApi:
    """Adds the /fund/* routes to an aiohttp app (behind its middlewares)."""
    from aiohttp import web  # lazy: fund/* stays importable without aiohttp

    api = FundApi(engine)
    import sandbox_profile

    def make(name: str, reads_body: bool, path: str = ""):
        handler = getattr(api, name)

        async def route(request):
            body = _NO_BODY
            if reads_body:
                try:
                    body = await _read_body(request)
                except ValueError:
                    return web.json_response({"error": "invalid JSON body", "errors": {"_": "invalid JSON body"}},
                                             status=400)
            try:
                status, payload = await handler(request.query, body, request.match_info)
            except Exception:
                logger.exception("/fund route %s failed", name)
                return web.json_response({"error": "internal error in the fund engine (see backend.log)"},
                                         status=500)
            if path in MODE_PATHS and status == 200 and isinstance(payload, dict):
                payload = sandbox_profile.stamp(payload)       # "mode": normal | sandbox (SPEC "Sandbox profile")
            return to_response(web, status, payload)

        route.__name__ = f"fund_{name}"
        return route

    for method, path, name, reads_body in ROUTES:
        app.router.add_route(method, path, make(name, reads_body, path))
    return api
