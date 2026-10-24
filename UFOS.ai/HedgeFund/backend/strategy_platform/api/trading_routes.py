"""Stage 7 routes (SPEC §17.10, payloads §17.11): ``/v2/portfolio/*``, ``/v2/oms/*``,
``/v2/incubation*`` — a mixin of ``routes.V2Api`` (same ``(query, body, match) ->
(status, payload)`` handlers, same ``register``). Strict input: unknown fields /
parameters → 400 ``{error: "invalid input", errors}``; refused requests → ``{error,
code, …}`` (``TradingError``); never a stack trace. Every GET touches the ONE heartbeat
shared with the old engine (dead-man switch, §17.1). PAPER TRADING ONLY.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import re
from typing import Mapping, Optional

from ..core.errors import UserFacingError

logger = logging.getLogger(__name__)

ACCOUNT_RE = re.compile(r"^(paper-main|paper-council|alpaca-paper|binance-demo|kraken-demo)$")  # + broker accounts
ACCOUNTS_TEXT = "paper-main | paper-council | alpaca-paper | binance-demo"       # (kraken-demo: retired, §18.13)
SOURCE_RE = re.compile(r"^(_unallocated|council-of-ais|s-[A-Za-z0-9_.:-]{1,64})$")
CID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,80}$")          # stored ids (GET/cancel paths; runtime ids carry symbols)
# client-supplied ids (POST /v2/oms/orders, SPEC §17.7): the contract pattern; the engine's own prefixes are
# reserved so a manual id can never pre-empt a runtime / kill / drawdown / margin order id (audit 2026-10-18)
MANUAL_CID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
RESERVED_CID_PREFIXES = ("rt-", "run-", "kill-", "kil-", "ddk-", "dra-", "mar-")
RUN_RE = re.compile(r"^inc-[0-9a-f]{12}$")
STRAT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)?)?$")
STATES = ("new", "submitted", "accepted", "partially_filled", "filled", "cancelled", "rejected", "expired")


def _bad(errors: dict):
    return 400, {"error": "invalid input", "errors": errors}


def _body(body) -> Optional[dict]:
    from .routes import NO_BODY
    if body is NO_BODY or body is None:
        return {}
    return body if isinstance(body, dict) else None


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _reason(body: dict, errors: dict, required: bool = True, lo: int = 3, hi: int = 500) -> Optional[str]:
    v = body.get("reason")
    if v is None:
        if required:
            errors["reason"] = f"required ({lo}..{hi} characters)"
        return None
    if not isinstance(v, str) or not lo <= len(v.strip()) <= hi:
        errors["reason"] = f"{lo}..{hi} characters"
        return None
    return v.strip()


def _note(body: dict, errors: dict, hi: int = 500) -> str:
    """fix1023 (confirmation style, user 2026-10-23): an OPTIONAL audit note — missing / null / "" -> "" (stored as
    given). Only evidence overrides and the live guard still require a reason (``_reason``)."""
    v = body.get("reason")
    if v is None:
        return ""
    if not isinstance(v, str) or len(v.strip()) > hi:
        errors["reason"] = f"optional note, at most {hi} characters"
        return ""
    return v.strip()


def _unknown(body: dict, allowed) -> dict:
    return {str(k)[:60]: "unknown field" for k in body if k not in allowed}


def _order_type_fields(b: dict, errors: dict, aid, sym, notional):
    """Stage 8 (SPEC §18.4): type / prices / bracket / tif of a manual order + the venue rule (400)."""
    from strategy_platform.core import params as fund_params
    from ..execution import oms
    otype = b.get("type", "market")
    extra: dict = {}
    if otype not in oms.ORDER_TYPES:
        errors["type"] = " | ".join(oms.ORDER_TYPES)
        return "market", extra
    if otype != "market" and aid in ("paper-main", "paper-council"):
        errors["type"] = "the internal paper broker supports market orders only"
    for key in ("limit_price", "stop_price"):
        v = b.get(key)
        if v is not None:
            if not _num(v) or not 0 < float(v) <= 10_000_000:
                errors[key] = "a positive price"
            else:
                extra[key] = float(v)
    if otype in ("limit", "stop_limit") and "limit_price" not in extra and "limit_price" not in errors:
        errors["limit_price"] = f"required for a {otype} order"
    if otype in ("stop", "stop_limit") and "stop_price" not in extra and "stop_price" not in errors:
        errors["stop_price"] = f"required for a {otype} order"
    tp, sl = b.get("take_profit"), b.get("stop_loss")
    if otype == "bracket":
        if not isinstance(tp, dict) or set(tp) - {"limit_price"} or not _num(tp.get("limit_price")) \
                or float(tp["limit_price"]) <= 0:
            errors["take_profit"] = "{limit_price: positive number}"
        else:
            extra["take_profit"] = {"limit_price": float(tp["limit_price"])}
        if not isinstance(sl, dict) or set(sl) - {"stop_price", "limit_price"} or not _num(sl.get("stop_price")) \
                or float(sl["stop_price"]) <= 0 or (sl.get("limit_price") is not None and (not _num(sl["limit_price"])
                                                                                          or float(sl["limit_price"]) <= 0)):
            errors["stop_loss"] = "{stop_price: positive number, limit_price?: positive number}"
        else:
            extra["stop_loss"] = {k: float(v) for k, v in sl.items() if v is not None}
        if b.get("side") not in ("buy", "short"):
            errors["side"] = "a bracket order opens a position (buy | short)"
    elif tp is not None or sl is not None:
        errors["take_profit" if tp is not None else "stop_loss"] = "only for type bracket"
    tif = b.get("tif")
    if tif is not None:
        if tif not in ("day", "gtc"):
            errors["tif"] = "day | gtc"
        else:
            extra["tif"] = tif
    if notional is not None and otype != "market":
        errors["notional"] = "notional orders are market orders"
    if sym is not None and aid == "alpaca-paper" and fund_params.is_crypto(sym):
        errors["symbol"] = "crypto is traded on binance-demo, not on alpaca-paper"
    if sym is not None and aid == "binance-demo" and not fund_params.is_crypto(sym):
        errors["symbol"] = f"stocks/ETFs are traded on alpaca-paper, not on {aid}"
    for key in ("limit_price", "stop_price"):
        if key in extra and otype not in (("limit", "stop_limit", "bracket") if key == "limit_price"
                                          else ("stop", "stop_limit")):
            errors[key] = f"not used by a {otype} order"
    return otype, extra


class TradingRoutesMixin:
    """Handlers of the stage-7 routes."""

    async def _trading(self, fn, *args):
        from ..execution.errors import TradingError
        try:
            if inspect.iscoroutinefunction(fn):
                return None, await fn(*args)
            return None, await asyncio.to_thread(fn, *args)
        except TradingError as ex:
            return (ex.status, ex.payload()), None
        except ValueError as ex:
            return _bad({"_": str(ex)[:300]}), None
        except UserFacingError as ex:
            from .routes import user_error
            logger.warning("/v2 trading: %s", ex)
            return user_error(ex), None

    @staticmethod
    def _touch():
        from ..execution import control
        try:
            control.touch()
        except Exception:        # noqa: BLE001
            logger.exception("heartbeat touch failed")

    @staticmethod
    def _account_param(query: Mapping, errors: dict, required: bool = True, default: Optional[str] = None):
        v = query.get("account_id", default)
        if v is None:
            if required:
                errors["account_id"] = f"required ({ACCOUNTS_TEXT})"
            return None
        if not isinstance(v, str) or not ACCOUNT_RE.fullmatch(v):
            errors["account_id"] = ACCOUNTS_TEXT
            return None
        return v

    # ---- /v2/portfolio ---------------------------------------------------------------------------

    async def portfolio_state(self, query: Mapping, body, match: Mapping):
        from ..portfolio import views
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        self._touch()
        err, payload = await self._trading(views.state)
        return err or (200, payload)

    async def portfolio_account_open(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        from ..portfolio import views
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"} if b is None else {k: "unknown parameter" for k in query})
        errors = _unknown(b, ("starting_capital", "label"))
        cap = b.get("starting_capital")
        if cap is not None and (not _num(cap) or not accounts.CAPITAL_MIN <= float(cap) <= accounts.CAPITAL_MAX):
            errors["starting_capital"] = f"a number {accounts.CAPITAL_MIN:,.0f}..{accounts.CAPITAL_MAX:,.0f}"
        label = b.get("label")
        if label is not None and (not isinstance(label, str) or not 1 <= len(label.strip()) <= 60):
            errors["label"] = "1..60 characters"
        if errors:
            return _bad(errors)

        def run():
            acc = accounts.open_main(cap, label.strip() if isinstance(label, str) else None, "user")
            return {"account": views.account_summary(acc)}
        err, payload = await self._trading(run)
        return err or (201, payload)

    async def portfolio_account_get(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts, oms
        from ..portfolio import settings as pf_settings
        from ..portfolio import views
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        aid = match.get("id") or ""
        if not ACCOUNT_RE.fullmatch(aid):
            return 404, {"error": accounts.NOT_FOUND, "code": "not_found"}
        self._touch()

        def run():
            acc = accounts.get(aid)
            pm = views.quotes_for(acc)
            return {"account": views.account_summary(acc, pm=pm), "positions": views.positions(acc, pm),
                    "working_orders": oms.list_orders(acc.id, states=oms.WORKING, limit=500),
                    "sources": views.source_summaries(acc, pm), "settings": pf_settings.get(acc.id)}
        err, payload = await self._trading(run)
        return err or (200, payload)

    async def portfolio_account_equity(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        errors = {k: "unknown parameter" for k in query if k != "since"}
        since = query.get("since")
        if since is not None and not ISO_RE.fullmatch(since):
            errors["since"] = "an ISO date/time"
        aid = match.get("id") or ""
        if errors:
            return _bad(errors)
        if not ACCOUNT_RE.fullmatch(aid):
            return 404, {"error": accounts.NOT_FOUND, "code": "not_found"}
        self._touch()

        def run():
            acc = accounts.get(aid)
            pts = acc.store.equity_points(since=since, limit=5000)
            return {"account_id": aid, "starting_capital": acc.store.get_starting_capital(),
                    "reset_at": acc.store.get_reset_at(),
                    "points": [{k: p[k] for k in ("ts", "equity", "cash", "long_mv", "short_mv", "drawdown_pct")}
                               for p in pts]}
        err, payload = await self._trading(run)
        return err or (200, payload)

    async def portfolio_sources(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        from ..portfolio import views
        errors = {k: "unknown parameter" for k in query if k != "account_id"}
        aid = self._account_param(query, errors)
        if errors:
            return _bad(errors)
        self._touch()

        def run():
            acc = accounts.get(aid)
            return {"account_id": aid, "sources": views.source_summaries(acc)}
        err, payload = await self._trading(run)
        return err or (200, payload)

    async def portfolio_source_ledger(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        from ..portfolio import ledger
        errors = {k: "unknown parameter" for k in query if k not in ("account_id", "limit")}
        aid = self._account_param(query, errors)
        limit, lerr = self._limit_param(query, 200, 1000)
        if lerr:
            errors.update(lerr)
        sid = match.get("source_id") or ""
        if not SOURCE_RE.fullmatch(sid):
            errors["source_id"] = "a source id"
        if errors:
            return _bad(errors)
        self._touch()

        def run():
            accounts.get(aid)
            rows = ledger.entries(aid, sid, limit=limit, newest_first=True)
            return {"account_id": aid, "source_id": sid,
                    "entries": [{k: r[k] for k in ("id", "ts", "kind", "symbol", "qty", "price", "amount", "ref", "note")}
                                for r in rows]}
        err, payload = await self._trading(run)
        return err or (200, payload)

    async def portfolio_allocation(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        from ..portfolio import allocation
        errors = {k: "unknown parameter" for k in query if k != "account_id"}
        aid = self._account_param(query, errors)
        if errors:
            return _bad(errors)
        self._touch()
        err, payload = await self._trading(lambda: allocation.payload(accounts.get(aid)))
        return err or (200, payload)

    async def portfolio_allocation_rebalance(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        from ..portfolio import allocation
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"} if b is None else {k: "unknown parameter" for k in query})
        errors = _unknown(b, ("account_id", "reason"))
        aid = self._account_param(b, errors)
        reason = _note(b, errors)
        if errors:
            return _bad(errors)
        why = f"user: {reason}" if reason else "user"
        err, payload = await self._trading(lambda: allocation.rebalance(accounts.get(aid), why, "user"))
        return err or (200, payload)

    async def portfolio_settings_get(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        from ..portfolio import settings as pf_settings
        errors = {k: "unknown parameter" for k in query if k != "account_id"}
        aid = self._account_param(query, errors)
        if errors:
            return _bad(errors)
        self._touch()

        def run():
            accounts.get(aid)
            return pf_settings.get(aid)
        err, payload = await self._trading(run)
        return err or (200, payload)

    async def portfolio_settings_put(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        from ..portfolio import settings as pf_settings
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"} if b is None else {k: "unknown parameter" for k in query})
        errors = _unknown(b, ("account_id", "reason", "changes"))
        aid = self._account_param(b, errors)
        reason = _note(b, errors)
        clean, cerr = pf_settings.validate(b.get("changes"), aid)
        errors.update(cerr)
        if errors:
            return _bad(errors)

        def run():
            accounts.get(aid)
            current = pf_settings.values(aid)
            merged = dict(current, **clean)
            if merged["alloc_min_weight"] > merged["alloc_max_weight"]:
                raise ValueError("alloc_min_weight must not exceed alloc_max_weight")
            return pf_settings.put(aid, clean, reason, "user")
        err, payload = await self._trading(run)
        return err or (200, payload)

    async def portfolio_migration_get(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts, control, migration
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        self._touch()

        def run():
            st = migration.legacy_state(control.legacy_engine())
            m = migration._migration_row()
            return {"legacy_account": st,
                    "migration": None if m is None else {"id": m["id"], "created_at": m["created_at"],
                                                         "account_id": m["account_id"], "summary": m["summary"]},
                    "can_migrate": st["status"] == "active" and not accounts.exists(accounts.COUNCIL)}
        err, payload = await self._trading(run)
        return err or (200, payload)

    async def portfolio_migration_dry_run(self, query: Mapping, body, match: Mapping):
        from ..execution import control, migration
        b = _body(body)
        if b is None or set(query) or b:
            return _bad({"_": "expected an empty JSON object {}"})
        err, payload = await self._trading(lambda: migration.dry_run("user", control.legacy_engine()))
        return err or (200, payload)

    async def portfolio_migration_run(self, query: Mapping, body, match: Mapping):
        from ..execution import control, migration
        from ..portfolio import views
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"} if b is None else {k: "unknown parameter" for k in query})
        errors = _unknown(b, ("confirm", "reason"))      # confirm: accepted and ignored (fix1023)
        reason = _note(b, errors)
        if errors:
            return _bad(errors)

        def run():
            out = migration.run(b.get("confirm"), reason, "user", control.legacy_engine())
            return {"migration": out["migration"], "account": views.account_summary(out["account"])}
        err, payload = await self._trading(run)
        return err or (200, payload)

    # ---- /v2/oms -----------------------------------------------------------------------------------

    async def oms_orders(self, query: Mapping, body, match: Mapping):
        from ..execution import oms
        errors = {k: "unknown parameter" for k in query if k not in ("account_id", "state", "limit")}
        aid = self._account_param(query, errors, required=False)
        state = query.get("state")
        if state is not None and state not in STATES:
            errors["state"] = "one of " + ", ".join(STATES)
        limit, lerr = self._limit_param(query, 100, 500)
        if lerr:
            errors.update(lerr)
        if errors:
            return _bad(errors)
        self._touch()
        err, payload = await self._trading(lambda: {"orders": oms.list_orders(aid, state, limit)})
        return err or (200, payload)

    async def oms_order_get(self, query: Mapping, body, match: Mapping):
        from ..execution import oms
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        cid = match.get("client_order_id") or ""
        if not CID_RE.fullmatch(cid):
            return 404, {"error": "No such order.", "code": "not_found"}
        self._touch()

        def run():
            o = oms.get(cid)
            if o is None:
                from ..execution.errors import TradingError
                raise TradingError(404, "not_found", "No such order.")
            return {"order": o, "events": oms.events(cid)}
        err, payload = await self._trading(run)
        return err or (200, payload)

    async def oms_order_create(self, query: Mapping, body, match: Mapping):
        from strategy_platform.core import params as fund_params
        from ..execution import accounts, oms
        from ..execution.errors import TradingError
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"} if b is None else {k: "unknown parameter" for k in query})
        errors = _unknown(b, ("account_id", "symbol", "side", "qty", "notional", "source_id", "reason",
                              "client_order_id", "type", "limit_price", "stop_price", "take_profit", "stop_loss",
                              "tif"))
        aid = self._account_param(b, errors)
        sym = fund_params.normalise_symbol(b.get("symbol")) if isinstance(b.get("symbol"), str) else None
        if sym is None:
            errors["symbol"] = "a ticker symbol (e.g. SPY, BTC-USD)"
        if b.get("side") not in oms.SIDES:
            errors["side"] = "buy | sell | short | cover"
        qty, notional = b.get("qty"), b.get("notional")
        if (qty is None) == (notional is None):
            errors["qty"] = "give exactly one of qty or notional"
        elif qty is not None:
            if not _num(qty) or float(qty) <= 0 or float(qty) > 10_000_000:
                errors["qty"] = "a positive number up to 10,000,000"
            elif sym is not None and not fund_params.is_crypto(sym) and not float(qty).is_integer():
                errors["qty"] = "fractional quantities are only allowed for crypto — equities trade in whole shares"
        elif not _num(notional) or not 0 < float(notional) <= 1_000_000_000:
            errors["notional"] = "a positive USD amount"
        src = b.get("source_id")
        if src is not None and (not isinstance(src, str) or not SOURCE_RE.fullmatch(src)):
            errors["source_id"] = "a source id (_unallocated | council-of-ais | s-<strategy id>)"
        cid = b.get("client_order_id")
        if cid is not None and (not isinstance(cid, str) or not MANUAL_CID_RE.fullmatch(cid)):
            errors["client_order_id"] = "8..64 characters [A-Za-z0-9_-] (e.g. a GUID)"
        elif cid is not None and cid.lower().startswith(RESERVED_CID_PREFIXES):
            errors["client_order_id"] = "the prefixes rt-, run-, kill-, kil-, ddk-, dra-, mar- are reserved for the engine"
        reason = _reason(b, errors, required=False, lo=1, hi=500)
        otype, extra = _order_type_fields(b, errors, aid, sym, notional)
        if errors:
            return _bad(errors)

        if src is not None and not errors:
            if aid == accounts.COUNCIL and src != accounts.COUNCIL_SOURCE:
                errors["source_id"] = "paper-council has one source: council-of-ais"
            elif aid != accounts.COUNCIL and src == accounts.COUNCIL_SOURCE:
                errors["source_id"] = "council-of-ais lives in paper-council"
            if errors:
                return _bad(errors)

        def run():
            acc = accounts.get(aid)
            known = oms.get(cid) if cid else None
            if known is not None:
                return 200, {"order": known}
            o, created = oms.place(acc, {"symbol": sym, "side": b["side"], "qty": qty, "notional": notional,
                                         "origin": "manual", "reason": reason or "manual order (user)",
                                         "source_id": src or acc.default_source(), "client_order_id": cid,
                                         "type": otype, **extra},
                                   actor="user")
            from ..portfolio import ledger
            ledger.attribute(acc)
            return (201 if created else 200), {"order": oms.get(o["client_order_id"])}
        err, payload = await self._trading(run)
        return err or payload

    async def oms_order_cancel(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts, oms
        from ..execution.errors import TradingError
        b = _body(body)
        if b is None or set(query) or b:
            return _bad({"_": "expected an empty JSON object {}"})
        cid = match.get("client_order_id") or ""
        if not CID_RE.fullmatch(cid):
            return 404, {"error": "No such order.", "code": "not_found"}

        def run():
            o = oms.get(cid)
            if o is None:
                raise TradingError(404, "not_found", "No such order.")
            return {"order": oms.cancel(accounts.get(o["account_id"]), cid, "cancelled by the user", "user")}
        err, payload = await self._trading(run)
        return err or (200, payload)

    async def oms_fills(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        errors = {k: "unknown parameter" for k in query if k not in ("account_id", "after", "limit")}
        aid = self._account_param(query, errors)
        after = query.get("after", "0")
        if not after.isdigit() or len(after) > 12:
            errors["after"] = "a fill id (integer)"
        limit, lerr = self._limit_param(query, 200, 500)
        if lerr:
            errors.update(lerr)
        if errors:
            return _bad(errors)
        self._touch()

        def run():
            acc = accounts.get(aid)
            fills = acc.adapter.fills(int(after), limit)
            return {"fills": [{k: f[k] for k in ("id", "ts", "client_order_id", "symbol", "side", "qty", "price", "fees",
                                                 "notional")} for f in fills],
                    "next_after": fills[-1]["id"] if fills else int(after)}
        err, payload = await self._trading(run)
        return err or (200, payload)

    async def oms_autotrade(self, query: Mapping, body, match: Mapping):
        from ..execution import control
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"} if b is None else {k: "unknown parameter" for k in query})
        errors = _unknown(b, ("enabled",))
        if not isinstance(b.get("enabled"), bool):
            errors["enabled"] = "true or false"
        if errors:
            return _bad(errors)
        err, payload = await self._trading(lambda: {"autotrade": control.set_autotrade(b["enabled"], "user")})
        return err or (200, payload)

    async def oms_kill(self, query: Mapping, body, match: Mapping):
        from ..execution import control
        b = _body(body)
        if b is None:
            b = {}
        reason = b.get("reason") if isinstance(b.get("reason"), str) else None
        try:
            payload = await asyncio.to_thread(control.kill_all, (reason or "")[:200] or None, "user")
        except Exception as ex:   # noqa: BLE001 — the kill switch always answers
            logger.exception("POST /v2/oms/kill failed")
            payload = {"platform": {"ok": False, "error": f"{type(ex).__name__}", "accounts": []},
                       "legacy": {"ok": False, "error": "not attempted", "halted": None}}
        return 200, payload

    async def oms_resume(self, query: Mapping, body, match: Mapping):
        from ..execution import control
        from ..portfolio import views
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"} if b is None else {k: "unknown parameter" for k in query})
        errors = _unknown(b, ("account_id", "reason"))
        aid = self._account_param(b, errors)
        reason = _note(b, errors)
        if errors:
            return _bad(errors)
        err, payload = await self._trading(lambda: {"account": views.account_summary(control.resume(aid, reason,
                                                                                                    "user"))})
        return err or (200, payload)

    async def oms_runtime(self, query: Mapping, body, match: Mapping):
        from ..live import runtime
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        self._touch()
        return 200, runtime.default().status()

    async def oms_runtime_run(self, query: Mapping, body, match: Mapping):
        from ..live import runtime
        from ..storage import db
        b = _body(body)
        if b is None or set(query) or b:
            return _bad({"_": "expected an empty JSON object {}"})
        try:
            await asyncio.to_thread(db.add_audit, "user", "runtime_run_now", None)
        except Exception:        # noqa: BLE001
            return 503, {"error": "audit log unavailable — the action was not carried out", "code": "audit_unavailable"}
        return 202, runtime.default().request_run()

    # ---- /v2/incubation ------------------------------------------------------------------------------

    async def incubation_list(self, query: Mapping, body, match: Mapping):
        from ..live import incubation
        errors = {k: "unknown parameter" for k in query if k not in ("strategy_id", "started_after")}
        sid = query.get("strategy_id")
        if sid is not None and not STRAT_RE.fullmatch(sid):
            errors["strategy_id"] = "a strategy id"
        after = None
        if query.get("started_after") is not None:      # fix1023 round 3: "did my start arrive?" after a timeout
            try:
                from datetime import datetime as _dt, timezone as _tz
                t = _dt.fromisoformat(str(query["started_after"]).strip().replace("Z", "+00:00"))
                if t.tzinfo is None:
                    raise ValueError("no offset")
                after = t.astimezone(_tz.utc).replace(microsecond=0).isoformat()
            except ValueError:
                errors["started_after"] = "an ISO time with offset, e.g. 2026-10-23T21:43:40Z"
        if errors:
            return _bad(errors)
        self._touch()
        err, payload = await self._trading(lambda: {"runs": [incubation.status(r) for r in incubation.run_rows(sid)
                                                             if after is None or r["created_at"] >= after],
                                                    "defaults": incubation.defaults(),
                                                    **({"started_after": after} if after else {})})
        return err or (200, payload)

    async def incubation_eligibility(self, query: Mapping, body, match: Mapping):
        from ..live import incubation
        errors = {k: "unknown parameter" for k in query if k != "strategy_id"}
        sid = query.get("strategy_id")
        if sid is None or not STRAT_RE.fullmatch(sid):
            errors["strategy_id"] = "required (a strategy id)"
        if errors:
            return _bad(errors)
        self._touch()
        err, payload = await self._trading(lambda: {k: v for k, v in incubation.eligibility(sid).items()
                                                    if k != "code"})
        return err or (200, payload)

    async def incubation_create(self, query: Mapping, body, match: Mapping):
        from ..execution import accounts
        from ..live import incubation
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"} if b is None else {k: "unknown parameter" for k in query})
        errors = _unknown(b, ("strategy_id", "length_sessions", "reason", "account_id"))
        sid = b.get("strategy_id")
        if not isinstance(sid, str) or not STRAT_RE.fullmatch(sid):
            errors["strategy_id"] = "required (a strategy id)"
        n = b.get("length_sessions")
        if isinstance(n, bool) or not isinstance(n, int) or not incubation.MIN_LENGTH <= n <= incubation.MAX_LENGTH:
            errors["length_sessions"] = f"a whole number {incubation.MIN_LENGTH}..{incubation.MAX_LENGTH}"
        reason = _note(b, errors)
        aid = b.get("account_id")
        if aid is not None and aid not in ("paper-main", "alpaca-paper", "binance-demo", "kraken-demo"):
            errors["account_id"] = "paper-main | alpaca-paper | binance-demo (default: the strategy's route)"
        if errors:
            return _bad(errors)

        def start():
            from ..execution.brokers import service
            from ..execution.errors import TradingError
            route = service.route_for(sid)
            if aid is not None and aid != route:
                raise TradingError(409, "route_mismatch", f"The strategy is routed to {route} — switch the route "
                                                          "(Settings → Brokers → Routing) to start it on " + str(aid) + ".")
            return incubation.start(sid, n, reason, route, "user")
        err, payload = await self._trading(start)
        return err or (201, payload)

    async def incubation_get(self, query: Mapping, body, match: Mapping):
        from ..live import incubation
        if set(query):
            return _bad({k: "unknown parameter" for k in query})
        rid = match.get("id") or ""
        if not RUN_RE.fullmatch(rid):
            return 404, {"error": incubation.NOT_FOUND, "code": "not_found"}
        self._touch()

        def run():
            r = incubation.run_row(rid)
            if r is None:
                from ..execution.errors import TradingError
                raise TradingError(404, "not_found", incubation.NOT_FOUND)
            from ..monitoring import decay
            return {"run": incubation.status(r), "events": incubation.run_events(rid, 300),
                    "decisions": decisions_of(rid),
                    "decay_events": decay.run_decisions(rid)}       # fix1023 round 2 (B3): also after a demote
        err, payload = await self._trading(run)
        return err or (200, payload)

    async def incubation_abort(self, query: Mapping, body, match: Mapping):
        from ..live import incubation
        b = _body(body)
        if b is None or set(query):
            return _bad({"_": "expected a JSON object"} if b is None else {k: "unknown parameter" for k in query})
        errors = _unknown(b, ("reason",))
        reason = _note(b, errors)
        if errors:
            return _bad(errors)
        rid = match.get("id") or ""
        if not RUN_RE.fullmatch(rid):
            return 404, {"error": incubation.NOT_FOUND, "code": "not_found"}
        err, payload = await self._trading(lambda: incubation.abort(rid, reason, "user"))
        return err or (200, payload)


def decisions_of(run_id: str, limit: int = 100) -> list:
    """Decisions of an incubation run with their execution status (§17.11)."""
    import json
    from ..live import runtime
    from ..storage import db
    from ..live import incubation
    end = incubation.end_event(run_id)
    aborted_at = end["ts"] if end is not None and end["event"] == "aborted" else None
    with db._connect() as conn:
        rows = conn.execute("SELECT * FROM rt_decisions WHERE incubation_id = ? ORDER BY bar_date DESC, created_at DESC"
                            " LIMIT ?", (run_id, int(limit))).fetchall()
        execs = conn.execute("SELECT decision_ids_json, result_json FROM rt_executions").fetchall()
    st = {}
    for e in execs:
        res = json.loads(e["result_json"])
        for d in json.loads(e["decision_ids_json"]):
            st[d] = (res.get("status"), res.get("reason"))
    out = []
    newest_ok = None
    for r in rows:
        res = json.loads(r["result_json"])
        if r["status"] == "ok" and newest_ok is None:
            newest_ok = r["id"]
        s, why = st.get(r["id"], (None, None))
        cancelled = None
        if s is None:
            s = "failed" if r["status"] == "failed" else ("pending" if r["id"] == newest_ok else "missed")
            if s == "pending" and aborted_at is not None:
                # fix1023 round 3: the run was aborted — the runtime never executes it (active runs only)
                s, cancelled = "cancelled", aborted_at
        d = {"id": r["id"], "bar_date": r["bar_date"], "created_at": r["created_at"], "status": s,
             "reason": why or res.get("reason"), "targets": res.get("targets"), "force": res.get("force") or [],
             "error": res.get("error")}
        if cancelled:
            d.update(cancelled_at=cancelled, cancelled_note="run aborted — this decision is never executed")
        out.append(d)
    err = runtime.default().decision_errors.get(run_id)
    if err:
        out.insert(0, {"id": None, "bar_date": None, "created_at": err["at"], "status": "failed", "reason": None,
                       "targets": None, "force": [], "error": err["error"]})
    return out


TRADING_ROUTES = (
    ("GET", "/v2/portfolio/state", "portfolio_state", False),
    ("POST", "/v2/portfolio/accounts", "portfolio_account_open", True),
    ("GET", "/v2/portfolio/accounts/{id}", "portfolio_account_get", False),
    ("GET", "/v2/portfolio/accounts/{id}/equity", "portfolio_account_equity", False),
    ("GET", "/v2/portfolio/sources", "portfolio_sources", False),
    ("GET", "/v2/portfolio/sources/{source_id}/ledger", "portfolio_source_ledger", False),
    ("GET", "/v2/portfolio/allocation", "portfolio_allocation", False),
    ("POST", "/v2/portfolio/allocation/rebalance", "portfolio_allocation_rebalance", True),
    ("GET", "/v2/portfolio/settings", "portfolio_settings_get", False),
    ("PUT", "/v2/portfolio/settings", "portfolio_settings_put", True),
    ("GET", "/v2/portfolio/migration", "portfolio_migration_get", False),
    ("POST", "/v2/portfolio/migration/dry-run", "portfolio_migration_dry_run", True),
    ("POST", "/v2/portfolio/migration", "portfolio_migration_run", True),
    ("GET", "/v2/oms/orders", "oms_orders", False),
    ("GET", "/v2/oms/orders/{client_order_id}", "oms_order_get", False),
    ("POST", "/v2/oms/orders", "oms_order_create", True),
    ("POST", "/v2/oms/orders/{client_order_id}/cancel", "oms_order_cancel", True),
    ("GET", "/v2/oms/fills", "oms_fills", False),
    ("POST", "/v2/oms/autotrade", "oms_autotrade", True),
    ("POST", "/v2/oms/kill", "oms_kill", True),
    ("POST", "/v2/oms/resume", "oms_resume", True),
    ("GET", "/v2/oms/runtime", "oms_runtime", False),
    ("POST", "/v2/oms/runtime/run", "oms_runtime_run", True),
    ("GET", "/v2/incubation", "incubation_list", False),
    ("GET", "/v2/incubation/eligibility", "incubation_eligibility", False),
    ("POST", "/v2/incubation", "incubation_create", True),
    ("GET", "/v2/incubation/{id}", "incubation_get", False),
    ("POST", "/v2/incubation/{id}/abort", "incubation_abort", True),
)
