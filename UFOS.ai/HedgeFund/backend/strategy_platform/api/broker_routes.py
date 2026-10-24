"""Stage 8 routes (SPEC §18.9, payloads §18.10): ``/v2/brokers/*`` — a mixin of ``routes.V2Api`` (same handler
shape and strict input as the stage-7 routes; refused requests ``{error, code, …}``; never a stack trace). Every GET
touches the ONE heartbeat. PAPER / DEMO TRADING ONLY — the live guard always refuses.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Mapping

from .trading_routes import STRAT_RE, _bad, _body, _note, _reason, _unknown

logger = logging.getLogger(__name__)

BROKER_RE = re.compile(r"^(alpaca-paper|binance-demo|kraken-demo)$")      # kraken-demo: retired (409, §18.13)
SYMBOL_HINT = "a ticker symbol (e.g. SPY, BTC-USD)"
NOT_FOUND = {"error": "No such broker account (alpaca-paper | binance-demo).", "code": "not_found"}
ASSET_RE = re.compile(r"^[A-Z0-9]{1,20}$")
MAX_BALANCES = 500


def _starting_balances(b, errors):
    """binance-demo connect: ``starting_balances {ASSET: total}`` (SPEC §18.14 item 7) — optional here, the service
    requires it on the first connect."""
    import math
    if "starting_balances" not in b:
        return None
    v = b["starting_balances"]
    if not isinstance(v, dict) or len(v) > MAX_BALANCES:
        errors["starting_balances"] = f"an object {{ASSET: total}} with at most {MAX_BALANCES} entries"
        return None
    for k, x in v.items():
        if not isinstance(k, str) or not ASSET_RE.fullmatch(k):
            errors["starting_balances"] = "asset names: A-Z / 0-9, 1..20 characters"
            return None
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)) or float(x) < 0:
            errors["starting_balances"] = f"{k}: a finite number ≥ 0"
            return None
    return {k: float(x) for k, x in v.items()}


class BrokerRoutesMixin:
    """Handlers of the stage-8 routes."""

    def _broker_id(self, match: Mapping):
        bid = match.get("id") or ""
        return bid if BROKER_RE.fullmatch(bid) else None

    @staticmethod
    def _no_query(query: Mapping):
        return _bad({k: "unknown parameter" for k in query}) if set(query) else None

    async def brokers_list(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import service
        if set(query):
            return self._no_query(query)
        self._touch()
        err, payload = await self._trading(service.list_status)
        return err or (200, payload)

    async def broker_get(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import service
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND
        if set(query):
            return self._no_query(query)
        self._touch()
        err, payload = await self._trading(service.detail, bid)
        return err or (200, payload)

    async def _post(self, body, query, allowed, fn):
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"} if b is None else {k: "unknown parameter" for k in query})
        errors = _unknown(b, allowed)
        if errors:
            return _bad(errors)
        return await fn(b)

    async def broker_test(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import service
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND

        async def go(b):
            err, payload = await self._trading(service.test, bid, "user")
            return err or (200, payload)
        return await self._post(body, query, (), go)

    async def broker_connect(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import service
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND

        async def go(b):
            errors = {}
            reason = _note(b, errors)
            conf = b.get("confirm")                  # accepted and ignored (fix1023)
            sb = _starting_balances(b, errors)
            if "starting_balances" in b and bid != "binance-demo":
                errors["starting_balances"] = "only for binance-demo"
            if errors:
                return _bad(errors)
            err, payload = await self._trading(service.connect, bid, conf, reason, "user", sb)
            return err or (200, payload)
        return await self._post(body, query, ("confirm", "reason", "starting_balances"), go)

    async def broker_disconnect(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import service
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND

        async def go(b):
            errors = {}
            reason = _note(b, errors)
            if errors:
                return _bad(errors)
            err, payload = await self._trading(service.disconnect, bid, reason, "user")
            return err or (200, payload)
        return await self._post(body, query, ("reason",), go)

    async def broker_reconcile(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import service
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND

        async def go(b):
            err, payload = await self._trading(service.reconcile_now, bid)
            return err or (200, payload)
        return await self._post(body, query, (), go)

    async def broker_reconciliations(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import reconcile
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND
        errors = {k: "unknown parameter" for k in query if k != "limit"}
        limit = 20
        if "limit" in query:
            try:
                limit = int(query["limit"])
                if not 1 <= limit <= 200:
                    raise ValueError
            except ValueError:
                errors["limit"] = "1..200"
        if errors:
            return _bad(errors)
        self._touch()
        err, payload = await self._trading(lambda: {"reconciliations": reconcile.history(bid, limit)})
        return err or (200, payload)

    async def broker_resolve(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        from ..execution.brokers import reconcile, service
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND

        async def go(b):
            errors = {}
            action = b.get("action")
            if action not in reconcile.ACTIONS:
                errors["action"] = " | ".join(reconcile.ACTIONS)
            reason = _note(b, errors)
            conf = b.get("confirm")                  # accepted and ignored (fix1023)
            if errors:
                return _bad(errors)

            def run():
                service.require_active(bid)
                if service.connection(bid)["status"] != "connected":
                    from ..execution.errors import TradingError
                    raise TradingError(409, "not_connected", f"{bid} is not connected.")
                out = reconcile.resolve(accounts.get(bid), action, reason, conf, "user")
                out["broker"] = service.broker_status(bid)
                return out
            err, payload = await self._trading(run)
            return err or (200, payload)
        return await self._post(body, query, ("action", "reason", "confirm"), go)

    async def broker_kill(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        from ..execution.brokers import service
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND
        from ..execution import accounts as _accounts
        why = _accounts.retired_reason(bid)
        if why:                          # retired: nothing to kill, no broker call (SPEC §18.13)
            return 409, {"error": f"{bid}: {why}", "code": "retired", "sources": list(_accounts.RETIRED_SOURCES)}
        why = _accounts.sandbox_block_reason(bid)
        if why:                          # sandbox profile: nothing is sent to the broker
            return 409, {"error": f"{bid}: {why}", "code": "sandbox_blocked"}
        b = _body(body) or {}
        reason = b.get("reason") if isinstance(b.get("reason"), str) else None
        reason = (reason or "").strip()[:200] or "Kill switch pressed (broker)"

        def run():
            if service.connection(bid)["status"] != "connected":
                return {"account_id": bid, "ok": False, "error": f"{bid} is not connected", "cancelled_at_broker": 0,
                        "oms_cancelled": 0, "orders": [], "halt": None, "note": None}
            return service.kill(accounts.get(bid), f"Kill switch: {reason}", "kill", "user")
        try:
            payload = await asyncio.to_thread(run)
        except Exception as ex:   # noqa: BLE001 — the kill switch always answers
            logger.exception("POST /v2/brokers/%s/kill failed", bid)
            payload = {"account_id": bid, "ok": False, "error": type(ex).__name__, "cancelled_at_broker": 0,
                       "oms_cancelled": 0, "orders": [], "halt": None, "note": None}
        return 200, payload

    async def broker_instrument(self, query: Mapping, body, match: Mapping):
        from strategy_platform.core import params as fund_params
        from ..execution.brokers import service
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND
        errors = {k: "unknown parameter" for k in query if k != "symbol"}
        sym = fund_params.normalise_symbol(query.get("symbol")) if isinstance(query.get("symbol"), str) else None
        if sym is None:
            errors["symbol"] = SYMBOL_HINT
        if errors:
            return _bad(errors)
        self._touch()
        err, payload = await self._trading(service.instrument, bid, sym)
        return err or (200, payload)

    async def broker_live_guard_get(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import live_guard
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND
        if set(query):
            return self._no_query(query)
        self._touch()
        err, payload = await self._trading(live_guard.status, bid)
        return err or (200, payload)

    async def broker_live_guard_put(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import live_guard
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND

        async def go(b):
            errors = {}
            flag = b.get("live_flag")
            if not isinstance(flag, bool):
                errors["live_flag"] = "true | false"
            cap = b.get("capital_cap_usd")
            try:
                live_guard.validate_cap(cap)
            except ValueError as ex:
                errors["capital_cap_usd"] = str(ex)
            reason = _reason(b, errors)
            if errors:
                return _bad(errors)

            def run():
                live_guard.set_settings(bid, flag, cap, reason, "user")
                return live_guard.status(bid)
            err, payload = await self._trading(run)
            return err or (200, payload)
        return await self._post(body, query, ("live_flag", "capital_cap_usd", "reason"), go)

    async def broker_live_guard_check(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import live_guard
        bid = self._broker_id(match)
        if bid is None:
            return 404, NOT_FOUND

        async def go(b):
            errors = {}
            sid = b.get("strategy_id")
            if sid is not None and (not isinstance(sid, str) or not STRAT_RE.fullmatch(sid)):
                errors["strategy_id"] = "a strategy id"
            conf = b.get("confirm")
            if conf is not None and (not isinstance(conf, str) or len(conf) > 200):
                errors["confirm"] = "text (≤ 200 characters)"
            if errors:
                return _bad(errors)
            err, payload = await self._trading(lambda: live_guard.check(bid, strategy_id=sid, confirm=conf,
                                                                        actor="user"))
            return err or (200, payload)
        return await self._post(body, query, ("strategy_id", "confirm"), go)

    async def brokers_routing_get(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import service
        if set(query):
            return self._no_query(query)
        self._touch()
        err, payload = await self._trading(service.routing_list)
        return err or (200, payload)

    async def brokers_routing_post(self, query: Mapping, body, match: Mapping):
        from ..execution.brokers import service

        async def go(b):
            errors = {}
            sid = b.get("strategy_id")
            if not isinstance(sid, str) or not STRAT_RE.fullmatch(sid):
                errors["strategy_id"] = "required (a strategy id)"
            aid = b.get("account_id")
            if aid not in ("paper-main", "alpaca-paper", "binance-demo", "kraken-demo"):
                errors["account_id"] = "paper-main | alpaca-paper | binance-demo"
            reason = _note(b, errors)
            conf = b.get("confirm")                  # accepted and ignored (fix1023)
            if errors:
                return _bad(errors)
            err, payload = await self._trading(service.set_route, sid, aid, conf, reason, "user")
            return err or (200, payload)
        return await self._post(body, query, ("strategy_id", "account_id", "confirm", "reason"), go)


BROKER_ROUTES = (
    ("GET", "/v2/brokers", "brokers_list", False),
    ("GET", "/v2/brokers/routing", "brokers_routing_get", False),
    ("POST", "/v2/brokers/routing", "brokers_routing_post", True),
    ("GET", "/v2/brokers/{id}", "broker_get", False),
    ("POST", "/v2/brokers/{id}/test", "broker_test", True),
    ("POST", "/v2/brokers/{id}/connect", "broker_connect", True),
    ("POST", "/v2/brokers/{id}/disconnect", "broker_disconnect", True),
    ("POST", "/v2/brokers/{id}/reconcile", "broker_reconcile", True),
    ("GET", "/v2/brokers/{id}/reconciliations", "broker_reconciliations", False),
    ("POST", "/v2/brokers/{id}/resolve", "broker_resolve", True),
    ("POST", "/v2/brokers/{id}/kill", "broker_kill", True),
    ("GET", "/v2/brokers/{id}/instrument", "broker_instrument", False),
    ("GET", "/v2/brokers/{id}/live-guard", "broker_live_guard_get", False),
    ("PUT", "/v2/brokers/{id}/live-guard", "broker_live_guard_put", True),
    ("POST", "/v2/brokers/{id}/live-guard/check", "broker_live_guard_check", True),
)
