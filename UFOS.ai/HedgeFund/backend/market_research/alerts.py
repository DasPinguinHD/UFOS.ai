"""MR7 alerts (SPEC §10 MR7, §13 "MR7 backend notes"): rules, validation, CRUD (blocking, Research pool) and
the ``AlertEngine`` that evaluates them on the market-data hub's live data.

Rules (``kind`` + ``params``):
- ``price_cross {level, direction: up|down}`` — the last trade crosses ``level``;
- ``line_cross {drawing_id, direction?: up|down|any}`` — crosses the price of an ``alertLine`` drawing (MR5);
  the level is re-read from the drawing (moved / split-rescaled lines apply within ``ALERT_RELOAD_SECONDS``);
  the drawing's ``alert_id`` is set / cleared by the backend in the SAME transaction as the alert write
  (create / switch on → linked, switch off / delete / auto-disable → cleared);
  a deleted drawing switches the alert off with a reason (+ audit row);
- ``day_move {pct 0.1–50, direction: up|down|any}`` — the move today vs the previous close (§7.4 definition,
  ``stats.quote.prev_close``) reaches ±pct;
- ``news {}`` — a new news item for the symbol arrives on the news stream (MR6).

Evaluation (event loop, arithmetic only): every stream trade of a streaming symbol (``hub.trade_hook``, not the
coalesced ``trade`` messages) and every news message. A crossing needs a PREVIOUS live trade: ``up`` =
prev < level ≤ last, ``down`` = prev > level ≥ last — so a jump over the level between two trades (a gap)
fires, the first trade after a start / reconnect / restart only sets the baseline (never fires). Late (older)
trades are ignored. De-dup: one trigger per rule per ``cooldown_seconds`` (persisted ``last_triggered_at``);
``day_move`` at most once per day and side; ``news`` once per news id. Triggers are written (``alert_log`` +
rule state, one transaction, Research pool) BEFORE the WebSocket ``alert`` message goes out — a restart never
repeats one.

Demand: every ENABLED rule is a hub subscriber of kind ``alert`` (topic ``trades:<SYM>`` or ``news:<SYM>``),
so the streams run while ≥ 1 alert is active; priority after the views (30-symbol stock limit, Q10): a rule
whose symbol is beyond it is ``not_live`` with the limit text and cannot fire (no snapshot, no polling).
Alerts work only while UFOS.ai runs (no background service).

Part of UFOS.ai, a proof-of-concept portfolio project; research only, not investment advice.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from strategy_platform.core.errors import UserFacingError
from strategy_platform.core.instruments import instrument

from . import limits
from . import store as default_store
from .executor import run_blocking
from .hub.demand import Subscriber, parse_topic

logger = logging.getLogger(__name__)

ID_RE = re.compile(r"^[0-9a-f]{32}$")
RUNTIME_NOTE = ("Alerts work only while UFOS.ai runs — there is no background service; nothing is checked while "
                "the app is closed.")
LINE_DELETED = "The linked alert line was deleted — this alert is switched off."
NOT_RUNNING = "The alert task is not running (see the Research status). " + RUNTIME_NOTE
BASELINE_REASON = "Armed with the next live trade — a crossing needs a previous price."
CLOSED_NOTE = " Market closed — no trades until the next session."
NOT_LIVE_SUFFIX = " This alert cannot fire while the symbol is not live."
DIRECTIONS = ("up", "down")
ANY_DIRECTIONS = ("up", "down", "any")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(ts) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt(price: float) -> str:
    return f"{price:,.2f}" if abs(price) >= 1 else f"{price:.6f}".rstrip("0").rstrip(".")


class AlertInvalid(UserFacingError):
    """400 {error: "invalid input", errors}."""

    def __init__(self, errors: dict):
        super().__init__("invalid input")
        self.errors = errors


class AlertConflict(UserFacingError):
    """409 {error}."""


class AlertNotFound(UserFacingError):
    def __init__(self):
        super().__init__("Alert not found.")


# --- validation ----------------------------------------------------------------------------------

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_params(kind: str, params) -> dict:
    """Normalised params of ``kind`` — exactly the allowed keys, else AlertInvalid."""
    if not isinstance(params, dict):
        raise AlertInvalid({"params": "must be an object"})
    allowed = {"price_cross": {"level", "direction"}, "line_cross": {"drawing_id", "direction"},
               "day_move": {"pct", "direction"}, "news": set()}[kind]
    extra = [k for k in params if k not in allowed]
    if extra:
        raise AlertInvalid({f"params.{str(k)[:30]}": "unknown field" for k in extra[:10]})
    if kind == "price_cross":
        lv, d = params.get("level"), params.get("direction")
        if not _num(lv) or not 0 < lv <= limits.ALERT_MAX_LEVEL:
            raise AlertInvalid({"params.level": f"must be a number > 0 and ≤ {limits.ALERT_MAX_LEVEL:g}"})
        if d not in DIRECTIONS:
            raise AlertInvalid({"params.direction": "must be up or down"})
        return {"level": float(lv), "direction": d}
    if kind == "line_cross":
        did, d = params.get("drawing_id"), params.get("direction", "any")
        if not isinstance(did, str) or not ID_RE.match(did):
            raise AlertInvalid({"params.drawing_id": "must be a 32-character lowercase hex drawing id"})
        if d not in ANY_DIRECTIONS:
            raise AlertInvalid({"params.direction": "must be up, down or any"})
        return {"drawing_id": did, "direction": d}
    if kind == "day_move":
        pct, d = params.get("pct"), params.get("direction")
        lo, hi = limits.ALERT_DAY_MOVE_PCT
        if not _num(pct) or not lo <= pct <= hi:
            raise AlertInvalid({"params.pct": f"must be a number {lo:g}–{hi:g} (percent)"})
        if d not in ANY_DIRECTIONS:
            raise AlertInvalid({"params.direction": "must be up, down or any"})
        return {"pct": float(pct), "direction": d}
    return {}


def _cooldown(v) -> int:
    lo, hi = limits.ALERT_COOLDOWN_RANGE
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise AlertInvalid({"cooldown_seconds": f"must be an integer {lo}–{hi}"})
    return v


def check_new(body) -> dict:
    if not isinstance(body, dict):
        raise AlertInvalid({"_": "a JSON object body is required"})
    extra = [k for k in body if k not in ("symbol", "kind", "params", "cooldown_seconds", "enabled")]
    if extra:
        raise AlertInvalid({str(k)[:40]: "unknown field" for k in extra[:20]})
    sym = body.get("symbol")
    if not isinstance(sym, str) or not sym or len(sym) > 20:
        raise AlertInvalid({"symbol": "required (stocks/ETFs like NVDA, BRK.B; crypto like BTC-USD)"})
    try:
        inst = instrument(sym.strip())
    except ValueError:
        raise AlertInvalid({"symbol": "not a supported symbol (stocks/ETFs like NVDA, BRK.B; crypto like BTC-USD)"})
    kind = body.get("kind")
    if kind not in limits.ALERT_KINDS:
        raise AlertInvalid({"kind": "must be one of " + ", ".join(limits.ALERT_KINDS)})
    if "params" not in body:
        raise AlertInvalid({"params": "required"})
    params = check_params(kind, body["params"])
    cooldown = _cooldown(body["cooldown_seconds"]) if "cooldown_seconds" in body else limits.ALERT_COOLDOWN_SECONDS
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AlertInvalid({"enabled": "must be true or false"})
    return {"symbol": inst.symbol, "asset_class": inst.asset_class, "kind": kind, "params": params,
            "cooldown_seconds": cooldown, "enabled": enabled}


def check_patch(body, kind: str) -> dict:
    if not isinstance(body, dict) or not body:
        raise AlertInvalid({"_": "a JSON object with at least one of params, cooldown_seconds, enabled"})
    extra = [k for k in body if k not in ("params", "cooldown_seconds", "enabled")]
    if extra:
        raise AlertInvalid({str(k)[:40]: ("cannot be changed — create a new alert" if k in ("symbol", "kind", "id")
                                          else "unknown field") for k in extra[:20]})
    out = {}
    if "params" in body:
        out["params"] = check_params(kind, body["params"])
    if "cooldown_seconds" in body:
        out["cooldown_seconds"] = _cooldown(body["cooldown_seconds"])
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise AlertInvalid({"enabled": "must be true or false"})
        out["enabled"] = body["enabled"]
    return out


# --- CRUD (BLOCKING: Research pool; the routes add research priority for the asset lookup) ---------------

def _line_drawing(store, drawing_id: str, symbol: str) -> dict:
    d = store.drawing_get(drawing_id)
    if d is None:
        raise AlertInvalid({"params.drawing_id": "no such drawing"})
    if d["tool"] != "alertLine":
        raise AlertInvalid({"params.drawing_id": "the drawing is not an alert line (tool alertLine)"})
    if d["symbol"] != symbol:
        raise AlertInvalid({"params.drawing_id": f"the drawing belongs to {d['symbol']}, not {symbol}"})
    return d


def create(data, body, *, store=default_store, now: Optional[datetime] = None) -> dict:
    a = check_new(body)
    data.asset(a["symbol"])                        # SymbolNotCovered → 404
    if a["kind"] == "line_cross":
        _line_drawing(store, a["params"]["drawing_id"], a["symbol"])
    ts = _iso(now or utc_now())
    row = {"id": uuid.uuid4().hex, "symbol": a["symbol"], "kind": a["kind"], "params": a["params"],
           "cooldown_seconds": a["cooldown_seconds"], "enabled": a["enabled"],
           "drawing_id": a["params"].get("drawing_id"), "created_at": ts, "updated_at": ts}
    res = store.alert_insert(row, limits.MAX_ALERTS)       # links the line (drawings.alert_id) atomically
    _link_error(res)
    if res == "full":
        raise AlertConflict(f"At most {limits.MAX_ALERTS} alerts — delete one first.")
    if res == "duplicate":
        raise AlertConflict("An identical alert for this symbol already exists.")
    if res == "drawing":
        raise AlertConflict("This alert line already has an alert.")
    store.add_audit("user", "alert_created", {"id": row["id"], "symbol": row["symbol"], "kind": row["kind"]})
    return store.alert_get(row["id"])


def update(alert_id: str, body, *, store=default_store, now: Optional[datetime] = None) -> dict:
    if not ID_RE.match(str(alert_id)):
        raise AlertNotFound()
    cur = store.alert_get(alert_id)
    if cur is None:
        raise AlertNotFound()
    changes = check_patch(body, cur["kind"])
    if "params" in changes and cur["kind"] == "line_cross" and \
            changes["params"]["drawing_id"] != cur["params"]["drawing_id"]:
        raise AlertInvalid({"params.drawing_id": "cannot be changed — create a new alert for another line"})
    if changes.get("enabled") and cur["kind"] == "line_cross" and store.drawing_get(cur["drawing_id"]) is None:
        raise AlertConflict("The linked alert line was deleted — this alert cannot be switched on again.")
    if changes.get("enabled"):
        changes["disabled_reason"] = None
    elif changes.get("enabled") is False:
        changes["disabled_reason"] = None          # switched off by the user: no system reason
    res = store.alert_update(alert_id, changes, _iso(now or utc_now()))   # (un)links the line atomically
    _link_error(res)
    if res == "missing":
        raise AlertNotFound()
    if res == "duplicate":
        raise AlertConflict("An identical alert for this symbol already exists.")
    store.add_audit("user", "alert_updated", {"id": alert_id, "fields": sorted(k for k in changes if k != "disabled_reason")})
    return store.alert_get(alert_id)


def _link_error(res: str) -> None:
    """The line checks repeated inside the store transaction (the drawing may change between check and write)."""
    if res == "no_drawing":
        raise AlertConflict("The linked alert line no longer exists.")
    if res == "not_line":
        raise AlertInvalid({"params.drawing_id": "the drawing is not an alert line (tool alertLine)"})
    if res == "wrong_symbol":
        raise AlertInvalid({"params.drawing_id": "the drawing belongs to another symbol"})
    if res == "linked":
        raise AlertConflict("This alert line already has an alert.")


def delete(alert_id: str, *, store=default_store) -> None:
    if not ID_RE.match(str(alert_id)) or not store.alert_delete(alert_id):
        raise AlertNotFound()
    store.add_audit("user", "alert_deleted", {"id": alert_id})


# --- engine --------------------------------------------------------------------------------------------

class AlertSub(Subscriber):
    """Hub subscriber of kind ``alert`` (demand + stream priority after the views) for ONE rule."""

    def __init__(self, engine: "AlertEngine", rule_id: str, topic: str):
        super().__init__("alert")
        self.engine, self.rule_id = engine, rule_id
        t = parse_topic(topic)
        self.topics[t.raw] = t

    def deliver(self, msg: dict, key=None) -> None:
        self.engine._deliver(self, msg)


class Rule:
    def __init__(self, row: dict):
        self.id, self.symbol, self.kind = row["id"], row["symbol"], row["kind"]
        self.asset_class = instrument(row["symbol"]).asset_class
        self.update(row)
        self.level: Optional[float] = row["params"].get("level")
        self.level_reason: Optional[str] = None
        self.suppressed = 0

    def update(self, row: dict) -> None:
        self.params, self.cooldown = row["params"], int(row["cooldown_seconds"])
        self.enabled, self.drawing_id = row["enabled"], row.get("drawing_id")
        self.created_at = row["created_at"]
        self.last_triggered = _parse(row["last_triggered_at"]) if row.get("last_triggered_at") else None
        self.state = dict(row.get("state") or {})

    @property
    def topic(self) -> str:
        return f"{'news' if self.kind == 'news' else 'trades'}:{self.symbol}"

    def cooling(self, now: datetime) -> Optional[datetime]:
        if self.last_triggered is None:
            return None
        until = self.last_triggered + timedelta(seconds=self.cooldown)
        return until if now < until else None


def default_day(asset_class: str, now: datetime) -> str:
    if asset_class == "crypto":
        return now.astimezone(timezone.utc).date().isoformat()
    from .stats.context import equity_reference_days
    return equity_reference_days(now)[0].isoformat()


class AlertEngine:
    def __init__(self, hub, data_fn: Callable[[], object], *, store=default_store,
                 now: Callable[[], datetime] = utc_now, reload_seconds: float = limits.ALERT_RELOAD_SECONDS,
                 prev_close_fn: Optional[Callable[[str, str, datetime], dict]] = None,
                 day_fn: Callable[[str, datetime], str] = default_day):
        self.hub, self._data_fn, self.store = hub, data_fn, store
        self._now, self.reload_seconds = now, reload_seconds
        self._prev_close_fn = prev_close_fn or self._default_prev_close
        self._day_fn = day_fn
        self.running = False
        self.rules: Dict[str, Rule] = {}
        self.subs: Dict[str, AlertSub] = {}
        self._by_symbol: Dict[str, List[Rule]] = {}
        self.last: Dict[str, tuple] = {}             # symbol -> (price, ts iso) of the last live trade
        self.day_ctx: Dict[str, tuple] = {}          # symbol -> (day key, prev close Num)
        self._prev_retry: Dict[str, tuple] = {}      # symbol -> (day key, monotonic-free retry time, reason)
        self._fires: deque = deque()
        self._wake: Optional[asyncio.Event] = None
        self.store_error: Optional[str] = None
        self.loaded = False
        self.dropped = 0
        if getattr(hub, "trade_hook", None) is None:
            hub.trade_hook = self.on_trade

    # --- lifecycle (supervised task "alert_engine") -----------------------------------------------
    async def run(self) -> None:
        from .hub.aio import within
        self._wake = asyncio.Event()
        self.running = True
        prev_task = asyncio.ensure_future(self._prev_loop())    # review M3: never in the trigger path
        try:
            while True:
                if prev_task.done() and not prev_task.cancelled() and prev_task.exception() is not None:
                    raise prev_task.exception()      # a crash of the previous-close loop = task crash
                await self.sync(prev=False)
                self._wake.clear()
                try:
                    await within(self._wake.wait(), self.reload_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            prev_task.cancel()
            self.running = False
            for sub in list(self.subs.values()):
                self.hub.unregister(sub)
            self.subs.clear()
            self.rules.clear()
            self._by_symbol.clear()
            self.last.clear()
            self.loaded = False

    def poke(self) -> None:
        """After a CRUD change: reload at once."""
        if self._wake is not None:
            self._wake.set()

    async def sync(self, prev: bool = True) -> None:
        """Flush pending triggers FIRST (review M3), then reload the rules (local SQLite), flush again.
        ``prev`` = also refresh the previous closes here (tests); the task does that in ``_prev_loop``."""
        await self._flush()
        try:
            await self._reload()
            self.store_error = None
        except self.store.StoreUnavailable as ex:
            self.store_error = ex.user_message
            logger.warning("Research alerts: %s", ex.user_message)
        await self._flush()
        if prev:
            await self._prev_closes()

    async def _prev_loop(self) -> None:
        """Previous closes for day_move rules on their own schedule (Alpaca may be slow / failing)."""
        while True:
            try:
                await self._prev_closes()
            except self.store.StoreUnavailable:
                pass
            await asyncio.sleep(self.reload_seconds)

    def _load(self):
        rows = self.store.alerts_list()
        drawings = {}
        for r in rows:
            if r["kind"] == "line_cross" and r["enabled"] and r["drawing_id"]:
                drawings[r["drawing_id"]] = self.store.drawing_get(r["drawing_id"])
        return rows, drawings

    async def _reload(self) -> None:
        rows, drawings = await run_blocking(self._load)
        now = self._now()
        for r in rows:
            if r["kind"] == "line_cross" and r["enabled"] and drawings.get(r["drawing_id"]) is None:
                await run_blocking(self.store.alert_disable, r["id"], LINE_DELETED, _iso(now))
                r["enabled"], r["disabled_reason"] = False, LINE_DELETED
                logger.info("Research alert %s switched off: its alert line was deleted", r["id"])
        wanted = {r["id"]: r for r in rows if r["enabled"]}
        for rid in [k for k in self.rules if k not in wanted]:
            self._drop(rid)
        changed = False
        for rid, row in wanted.items():
            rule = self.rules.get(rid)
            if rule is None:
                rule = self.rules[rid] = Rule(row)
                changed = True
            else:
                mem_last, mem_state = rule.last_triggered, rule.state
                rule.update(row)
                rule.state = mem_state                  # memory is ahead of the store (written only at a trigger)
                if mem_last is not None and (rule.last_triggered is None or mem_last > rule.last_triggered):
                    rule.last_triggered = mem_last      # a trigger not written yet
            if rule.kind == "price_cross":
                rule.level, rule.level_reason = rule.params["level"], None
            elif rule.kind == "line_cross":
                rule.level, rule.level_reason = self._line_level(drawings.get(rule.drawing_id), rule.symbol)
            if rid not in self.subs:
                sub = self.subs[rid] = AlertSub(self, rid, rule.topic)
                self.hub.register(sub)
        self._by_symbol = {}
        for rule in sorted(self.rules.values(), key=lambda x: x.created_at):
            if rule.kind != "news":
                self._by_symbol.setdefault(rule.symbol, []).append(rule)
        for sym in [s for s in self.last if s not in self._by_symbol]:
            del self.last[sym]
        self.loaded = True

    @staticmethod
    def _line_level(drawing: Optional[dict], symbol: str):
        if drawing is None:
            return None, LINE_DELETED
        if drawing.get("tool") != "alertLine" or drawing.get("symbol") != symbol:
            return None, "The linked drawing is no longer an alert line of " + symbol + "."
        pts = drawing.get("points") or []
        p = pts[0].get("p") if pts and isinstance(pts[0], dict) else None
        if not _num(p) or p <= 0:
            return None, "The alert line has no usable price."
        return float(p), None

    def _drop(self, rid: str) -> None:
        self.rules.pop(rid, None)
        sub = self.subs.pop(rid, None)
        if sub is not None:
            self.hub.unregister(sub)

    # --- previous close for day_move (Research pool, research priority) ------------------------------
    def _default_prev_close(self, symbol: str, asset_class: str, now: datetime) -> dict:
        from .stats import quote
        data = self._data_fn()
        return quote.prev_close(data.loader, data.sources, symbol, asset_class, now)

    def _fetch_prev(self, symbol: str, asset_class: str, now: datetime) -> dict:
        from strategy_platform.data import http_gate
        with http_gate.research_priority():
            return self._prev_close_fn(symbol, asset_class, now)

    async def _prev_closes(self) -> None:
        now = self._now()
        syms = {r.symbol: r.asset_class for r in self.rules.values() if r.kind == "day_move"}
        for sym in [s for s in self.day_ctx if s not in syms]:
            del self.day_ctx[sym]
        for sym, ac in syms.items():
            try:
                day = self._day_fn(ac, now)
            except UserFacingError as ex:
                self.day_ctx.pop(sym, None)
                self._prev_retry[sym] = (None, now, ex.user_message)
                continue
            ctx = self.day_ctx.get(sym)
            if ctx is not None and ctx[0] == day:
                continue
            retry = self._prev_retry.get(sym)
            if retry is not None and retry[0] == day and now < retry[1]:
                continue
            try:
                value = await run_blocking(self._fetch_prev, sym, ac, now)
            except UserFacingError as ex:
                value = {"value": None, "reason": ex.user_message}
            if value.get("value") is None:
                self.day_ctx.pop(sym, None)
                self._prev_retry[sym] = (day, now + timedelta(seconds=limits.ALERT_PREV_CLOSE_RETRY_SECONDS),
                                         "Previous close unavailable: " + str(value.get("reason") or "no value"))
            else:
                self.day_ctx[sym] = (day, value)
                self._prev_retry.pop(sym, None)

    # --- evaluation (event loop; arithmetic only) -----------------------------------------------------
    def on_trade(self, endpoint: str, tr: dict) -> None:
        if not self.running:
            return
        sym = tr.get("symbol")
        rules = self._by_symbol.get(sym)
        if not rules:
            return
        price = tr.get("price")
        if not _num(price) or price <= 0:
            return
        ts = tr["ts"] if isinstance(tr["ts"], str) else _iso(tr["ts"]) if isinstance(tr["ts"], datetime) else None
        if ts is None:
            return
        prev = self.last.get(sym)
        if prev is not None and ts < prev[1]:
            return                                  # a late trade never moves the baseline back
        self.last[sym] = (float(price), ts)
        if prev is None:
            return                                  # baseline only: no previous price, no crossing
        for rule in rules:
            try:
                self._evaluate(rule, prev[0], float(price), ts)
            except Exception:   # noqa: BLE001 — one broken rule never stops the others
                logger.exception("Research alert %s: evaluation failed", rule.id)

    @staticmethod
    def _crossed(p0: float, p1: float, level: float, direction: str) -> Optional[str]:
        if p0 < level <= p1 and direction in ("up", "any"):
            return "up"
        if p0 > level >= p1 and direction in ("down", "any"):
            return "down"
        return None

    def _evaluate(self, rule: Rule, p0: float, p1: float, ts: str) -> None:
        if rule.kind in ("price_cross", "line_cross"):
            if rule.level is None:
                return
            side = self._crossed(p0, p1, rule.level, rule.params.get("direction", "any"))
            if side is None:
                return
            if rule.kind == "price_cross":
                msg = f"{rule.symbol} crossed {'above' if side == 'up' else 'below'} {fmt(rule.level)} — last trade {fmt(p1)}."
            else:
                msg = (f"{rule.symbol} crossed the alert line at {fmt(rule.level)} "
                       f"({'upward' if side == 'up' else 'downward'}) — last trade {fmt(p1)}.")
            self._fire(rule, msg, p1, ts, rule.level, {"side": side, "previous_price": p0})
        elif rule.kind == "day_move":
            ctx = self.day_ctx.get(rule.symbol)
            if ctx is None:
                return
            day, num = ctx
            pc = num["value"]
            x = rule.params["pct"]
            pct0, pct1 = (p0 - pc) / pc * 100.0, (p1 - pc) / pc * 100.0
            side = self._crossed(pct0, pct1, x, "up") if rule.params["direction"] in ("up", "any") else None
            if side is None and rule.params["direction"] in ("down", "any"):
                side = self._crossed(pct0, pct1, -x, "down")
            if side is None:
                return
            key = f"{day}:{side}"
            if key in (rule.state.get("days") or []):
                return
            when = "today" if rule.asset_class != "crypto" else "today (UTC day)"
            msg = (f"{rule.symbol} is {'up' if side == 'up' else 'down'} {abs(pct1):.2f} % {when} — last trade "
                   f"{fmt(p1)} vs previous close {fmt(pc)}.")
            level = pc * (1 + (x if side == "up" else -x) / 100.0)
            self._fire(rule, msg, p1, ts, level, {"side": side, "pct": round(pct1, 4), "prev_close": pc, "day": day},
                       day_key=key)

    def _fire(self, rule: Rule, message: str, price: Optional[float], ts: str, level: Optional[float], detail: dict,
              *, day_key: Optional[str] = None, news_id: Optional[int] = None) -> bool:
        now = self._now()
        if rule.cooling(now) is not None:
            rule.suppressed += 1
            return False
        if len(self._fires) >= limits.ALERT_FIRE_QUEUE_MAX:
            self.dropped += 1
            logger.warning("Research alerts: trigger queue full — alert %s not fired", rule.id)
            return False
        rule.last_triggered = now
        state = dict(rule.state)
        if day_key is not None:
            state["days"] = ([d for d in state.get("days") or [] if d.split(":")[0] == day_key.split(":")[0]]
                             + [day_key])[-4:]
        rule.state = state
        self._fires.append({"rule": rule.id, "symbol": rule.symbol, "kind": rule.kind, "ts": _iso(now),
                            "trade_ts": ts, "message": limits.clip(message), "price": price, "level": level,
                            "detail": detail, "state": dict(state)})
        if self._wake is not None:
            self._wake.set()
        return True

    def _deliver(self, sub: AlertSub, msg: dict) -> None:
        t = msg.get("type")
        rule = self.rules.get(sub.rule_id)
        if rule is None or not self.running:
            return
        if t == "symbol_status" and not msg.get("live"):
            self.last.pop(msg.get("symbol"), None)    # stream gap: the next trade is a new baseline
        elif t == "news" and rule.kind == "news":
            item = msg.get("item") or {}
            nid = item.get("id")
            ids = list(rule.state.get("news_ids") or [])
            if nid is None or nid in ids:
                return                               # an update / repeat of a news item already handled
            rule.state = dict(rule.state, news_ids=(ids + [nid])[-limits.ALERT_NEWS_IDS_KEPT:])
            head = limits.clip(item.get("headline") or "", 200)
            self._fire(rule, f"News for {rule.symbol}: {head}", None, item.get("created_at") or _iso(self._now()),
                       None, {"news_id": nid, "url": item.get("url"), "source_label": limits.NEWS_SOURCE_LABEL})

    async def _flush(self) -> None:
        while self._fires:
            f = self._fires.popleft()
            log = {"symbol": f["symbol"], "kind": f["kind"], "message": f["message"], "price": f["price"],
                   "detail": dict(f["detail"], level=f["level"], trade_ts=f["trade_ts"])}
            logged = True
            try:
                log_id = await run_blocking(self.store.alert_fire, f["rule"], f["ts"], f["state"], log)
                if log_id is None:
                    continue                          # deleted meanwhile: nothing sent
            except self.store.StoreUnavailable as ex:
                log_id, logged = None, False
                logger.warning("Research alert %s fired but could not be logged: %s", f["rule"], ex.user_message)
            msg = {"type": "alert", "alert_id": f["rule"], "symbol": f["symbol"], "kind": f["kind"],
                   "title": f"Research alert · {f['symbol']}", "message": f["message"], "ts": f["ts"],
                   "price": f["price"], "level": f["level"], "log_id": log_id, "logged": logged}
            self.hub._send(self.hub._subs_with("alerts"), msg)

    # --- status (event loop, no I/O) --------------------------------------------------------------------
    def _live(self, rule_kind: str, symbol: str):
        if rule_kind == "news":
            c = self.hub.conns["news"]
            if not self.hub.running:
                from .hub.hub import NOT_RUNNING_MESSAGE
                return False, NOT_RUNNING_MESSAGE
            if not c.is_live():
                return False, c.symbol_reason()
            return (symbol in c.desired), (None if symbol in c.desired else "Starting the news stream…")
        live, reason = self.hub.symbol_live(symbol)
        if live and symbol not in self.hub._conn_for(symbol).desired:
            return False, "Starting the live stream…"
        return live, reason

    def status(self, a: dict) -> dict:
        now = self._now()
        out = {"state": "armed", "reason": None, "live": False, "level": None, "last_price": None,
               "prev_close": None, "cooldown_until": None}
        if not a["enabled"]:
            return dict(out, state="disabled", reason=a.get("disabled_reason") or "Switched off.")
        if not self.running:
            return dict(out, state="not_running", reason=NOT_RUNNING)
        rule = self.rules.get(a["id"])
        if rule is None:
            return dict(out, state="waiting", reason="Loading the alert…")
        live, reason = self._live(rule.kind, rule.symbol)
        last = self.last.get(rule.symbol)
        out.update(live=live, level=rule.level,
                   last_price={"price": last[0], "ts": last[1]} if last and rule.kind != "news" else None)
        if rule.kind == "day_move":
            ctx = self.day_ctx.get(rule.symbol)
            if ctx is not None:
                pc = ctx[1]["value"]
                out["prev_close"] = pc
                out["level"] = None
                out["levels"] = {"up": pc * (1 + rule.params["pct"] / 100.0), "down": pc * (1 - rule.params["pct"] / 100.0)}
        closed = ""
        if rule.asset_class == "equity":
            try:
                from . import market
                if market.status(now)["equities"]["state"] in ("closed", "holiday"):
                    closed = CLOSED_NOTE
            except UserFacingError:
                closed = ""
        if not live:
            return dict(out, state="not_live", reason=limits.clip((reason or "Not live.") + NOT_LIVE_SUFFIX, 400))
        if rule.kind in ("price_cross", "line_cross") and rule.level is None:
            return dict(out, state="waiting", reason=rule.level_reason)
        if rule.kind == "day_move" and self.day_ctx.get(rule.symbol) is None:
            retry = self._prev_retry.get(rule.symbol)
            return dict(out, state="waiting", reason=retry[2] if retry else "Loading the previous close…")
        until = rule.cooling(now)
        if until is not None:
            return dict(out, state="cooldown", cooldown_until=_iso(until),
                        reason=f"Fired at {_iso(rule.last_triggered)} — quiet until {_iso(until)}.")
        if rule.kind != "news" and last is None:
            return dict(out, state="waiting", reason=BASELINE_REASON + closed)
        return dict(out, reason=closed.strip() or None)

    def runtime(self) -> dict:
        return {"running": self.running, "note": RUNTIME_NOTE, "store_error": self.store_error,
                "active": len(self.subs)}
