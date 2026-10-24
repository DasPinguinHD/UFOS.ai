"""Order management system (SPEC §17.7) — persisted state machine on ``oms_orders``
(+ an insert-only ``oms_order_events`` row per transition).

States: new → submitted → accepted → partially_filled → filled, plus cancelled,
rejected, expired (terminal: filled, cancelled, rejected, expired). Transitions are
checked in SQL (``UPDATE … WHERE state IN (allowed predecessors)``), so two threads can
never move one order twice.

Rules:
- **Idempotent client order ids** — a repeated id returns the stored order (nothing is
  sent twice; the adapter is idempotent on the id as well).
- **Stored before sent** — the order is written as ``new`` (with its pre-trade check
  results) BEFORE the adapter call; then ``submitted``; then the adapter's ack.
- **Retry policy** — a transient adapter error (``BrokerUnavailable``): 3 attempts
  (1 s, 3 s) in the call; still failing → stays ``submitted`` and the next ``sync``
  retries; after ``MAX_ATTEMPTS`` (5) → ``rejected`` "broker unreachable".
- **Crash between store and send** (``sync``, every cycle and at startup): ``new``
  older than ``STALE_NEW_SECONDS`` (30) → ``cancelled`` "not sent before a restart";
  ``submitted`` → ``adapter.order(id)``: known → synced; unknown and younger than
  ``RESEND_WINDOW_SECONDS`` (300) → re-sent with the SAME id; older → ``rejected``.
- **Partial fills** — a fill below the ordered quantity → ``partially_filled``, and as
  the broker is done with it → ``cancelled`` (``filled_qty > 0``, "rest cancelled …").
- **TIF** — runtime orders ``day``: an equity order still queued after its session's
  close → cancelled at the broker → ``expired``; protective orders ``gtc``.
- Broker-initiated orders (margin calls / liquidations of the PaperBroker) are imported
  as OMS orders ``brk-<account>-<broker id>`` (origin ``margin``).

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER TRADING ONLY.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ..storage import db
from . import accounts
from .errors import BrokerUnavailable, TradingError

logger = logging.getLogger(__name__)

STATES = ("new", "submitted", "accepted", "partially_filled", "filled", "cancelled", "rejected", "expired")
TERMINAL = ("filled", "cancelled", "rejected", "expired")
WORKING = ("new", "submitted", "accepted", "partially_filled")
ALLOWED = {
    "submitted": ("new", "submitted"),
    "accepted": ("submitted",),
    "partially_filled": ("submitted", "accepted", "partially_filled"),   # self: a broker's growing partial fill
    "filled": ("submitted", "accepted", "partially_filled"),
    "cancelled": ("new", "submitted", "accepted", "partially_filled"),   # submitted: review fix H1 (2026-10-18)
    "rejected": ("new", "submitted", "accepted"),
    "expired": ("accepted", "partially_filled"),
}
ORIGINS = ("runtime", "manual", "kill", "drawdown", "margin", "council")     # council: SP §22.5 (prefix cl-)
ORDER_TYPES = ("market", "limit", "stop", "stop_limit", "bracket")
PROTECTIVE = ("kill", "drawdown", "margin")
SIDES = ("buy", "sell", "short", "cover")
MAX_ATTEMPTS = 5
RETRY_DELAYS = (1.0, 3.0)
STALE_NEW_SECONDS = 30
RESEND_WINDOW_SECONDS = 300
CID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,80}$")
IMPORT_CURSOR = "oms_import_cursor"
EPS = 1e-9

_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_inflight: set = set()
_sleep = _time.sleep            # tests replace it


def lock_for(account_id: str) -> threading.RLock:
    with _locks_guard:
        lk = _locks.get(account_id)
        if lk is None:
            lk = _locks[account_id] = threading.RLock()
        return lk


def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or accounts.now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _out(r) -> dict:
    from strategy_platform.core import params as fund_params
    d = dict(r)
    d["allocations"] = [{"source_id": a["source_id"], "qty": a["qty"]} for a in json.loads(d.pop("allocations_json") or "[]")]
    d["checks"] = json.loads(d.pop("checks_json") or "[]")
    d["terminal"] = d["state"] in TERMINAL
    d["asset_class"] = "crypto" if fund_params.is_crypto(d["symbol"]) else "equity"
    d["allow_reduce"] = bool(d["allow_reduce"])
    spec = json.loads(d.pop("legs_json") or "null") if "legs_json" in d else None
    d.setdefault("order_type", "market")
    d["bracket"] = spec if isinstance(spec, dict) else None          # SPEC §18.4: {take_profit, stop_loss}
    d["legs"] = [f"{d['client_order_id']}.tp", f"{d['client_order_id']}.sl"] if d["order_type"] == "bracket" else []
    return d


def get(client_order_id: str) -> Optional[dict]:
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM oms_orders WHERE client_order_id = ?", (client_order_id,)).fetchone()
    return _out(r) if r else None


def list_orders(account_id: Optional[str] = None, state: Optional[str] = None, limit: int = 100,
                states: Optional[Tuple[str, ...]] = None) -> List[dict]:
    q, args = "SELECT * FROM oms_orders WHERE 1 = 1", []
    if account_id:
        q += " AND account_id = ?"
        args.append(account_id)
    if state:
        q += " AND state = ?"
        args.append(state)
    if states:
        q += f" AND state IN ({','.join('?' * len(states))})"
        args.extend(states)
    q += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    args.append(max(1, min(int(limit), 5000)))
    with db._connect() as conn:
        return [_out(r) for r in conn.execute(q, args).fetchall()]


def events(client_order_id: str) -> List[dict]:
    with db._connect() as conn:
        rows = conn.execute("SELECT id, ts, from_state, to_state, note, data_json FROM oms_order_events WHERE"
                            " client_order_id = ? ORDER BY id", (client_order_id,)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "from_state": r["from_state"], "to_state": r["to_state"], "note": r["note"],
             "data": json.loads(r["data_json"]) if r["data_json"] else None} for r in rows]


def _event(conn, cid: str, frm: Optional[str], to: str, note: Optional[str], data=None, ts: Optional[str] = None):
    conn.execute("INSERT INTO oms_order_events (client_order_id, ts, from_state, to_state, note, data_json) VALUES"
                 " (?, ?, ?, ?, ?, ?)", (cid, ts or _now_iso(), frm, to, (note or None) and str(note)[:500],
                                          json.dumps(data, default=str) if data is not None else None))


def transition(cid: str, to: str, *, note: Optional[str] = None, fields: Optional[dict] = None, data=None) -> bool:
    """Moves an order to ``to`` when its current state allows it (SQL-checked); False otherwise."""
    allowed = ALLOWED[to]
    fields = dict(fields or {})
    with db._connect() as conn:
        r = conn.execute("SELECT state FROM oms_orders WHERE client_order_id = ?", (cid,)).fetchone()
        if r is None or r["state"] not in allowed:
            return False
        frm = r["state"]
        ts = _now_iso()
        fields["state"] = to
        fields["updated_at"] = ts
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = conn.execute(f"UPDATE oms_orders SET {cols} WHERE client_order_id = ? AND state = ?",
                           tuple(fields.values()) + (cid, frm))
        if cur.rowcount != 1:
            return False
        _event(conn, cid, frm, to, note, data, ts)
        return True


def new_client_id(prefix: str = "man") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _insert(conn, row: dict) -> None:
    cols = ("client_order_id", "account_id", "created_at", "updated_at", "symbol", "side", "qty", "notional_req",
            "origin", "tif", "reason", "ref_price", "state", "broker_order_id", "filled_qty", "avg_fill_price", "fees",
            "notional", "reject_reason", "attempts", "allow_reduce", "allocations_json", "checks_json", "execution_id",
            "order_type", "limit_price", "stop_price", "legs_json", "parent_client_order_id", "broker_status")
    row = dict(row)
    row["order_type"] = row.get("order_type") or "market"
    conn.execute(f"INSERT INTO oms_orders ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(row.get(c) for c in cols))
    _event(conn, row["client_order_id"], None, row["state"], "order stored before sending", None, row["created_at"])


def _class_day_start(now: datetime, crypto: bool) -> datetime:
    from ..core import calendars
    if crypto:
        return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    ny = now.astimezone(calendars.NY)
    return datetime(ny.year, ny.month, ny.day, tzinfo=calendars.NY).astimezone(timezone.utc)


def _orders_today(account_id: str, now: datetime, crypto: bool) -> int:
    from strategy_platform.core import params as fund_params
    start = _class_day_start(now, crypto).isoformat(timespec="seconds")
    with db._connect() as conn:
        rows = conn.execute("SELECT symbol FROM oms_orders WHERE account_id = ? AND created_at >= ? AND state != 'rejected'"
                            f" AND origin NOT IN ({','.join('?' * len(PROTECTIVE))})",
                            (account_id, start) + PROTECTIVE).fetchall()
    return sum(1 for r in rows if fund_params.is_crypto(r["symbol"]) == crypto)


def _valued(account, prices) -> Tuple[Optional[object], Optional[str]]:
    from strategy_platform.execution.paper_broker import StateUnavailable
    try:
        return account.broker.account(prices), None
    except StateUnavailable as ex:
        return None, str(ex)[:300]


def place(account, intent: dict, *, actor: str = "user", prices: Optional[dict] = None) -> Tuple[dict, bool]:
    """Pre-trade checks → stored ``new`` → sent. Returns (order, created). ``intent``: {symbol, side, qty |
    notional, origin, tif?, reason?, ref_price?, allocations?: [(source, qty)], execution_id?, allow_reduce?,
    client_order_id?, source_id?}. A failed check → the order is stored ``rejected`` (created True)."""
    from strategy_platform.core import params as fund_params
    from strategy_platform.execution.paper_broker import round_qty
    from ..portfolio import checks, risk
    from ..portfolio import settings as pf_settings
    sym = fund_params.normalise_symbol(intent.get("symbol"))
    side, origin = intent.get("side"), intent.get("origin")
    if sym is None or side not in SIDES or origin not in ORIGINS:
        raise ValueError("symbol, side and origin must be valid")
    otype = intent.get("type") or "market"
    if otype not in ORDER_TYPES:
        raise ValueError(f"type must be one of {', '.join(ORDER_TYPES)}")
    is_broker = bool(getattr(account, "is_broker", False))
    if otype != "market" and not is_broker:
        raise ValueError("the internal paper broker supports market orders only")
    if otype != "market" and origin != "manual":
        raise ValueError("only manual orders may use limit / stop / bracket types")
    cid = intent.get("client_order_id") or new_client_id({"manual": "man", "council": "cl"}.get(origin, origin[:3]))
    with lock_for(account.id):
        known = get(cid)
        if known is not None:
            if known["account_id"] != account.id:
                raise TradingError(409, "client_order_id_in_use", "This client_order_id belongs to another account.")
            return known, False
        now = accounts.now()
        pm = dict(prices or {})
        # retest 2026-10-23: every quote this order needs (the traded symbol, each held one without a quote, and
        # the collar's fresh read) is fetched IN PARALLEL with ONE deadline — a flaky network gives a clear
        # "no current quote" instead of a long wait (each read alone may take up to ~64 s through the gate)
        need = [] if sym in pm else [sym]
        need += [p["symbol"] for p in account.store.get_positions() if p["symbol"] not in pm and p["symbol"] != sym]
        got, why_q, fresh = _quotes_within(account, need, ORDER_QUOTE_DEADLINE_SECONDS, fresh_for=sym)
        pm.update(got)
        q = pm.get(sym)
        qty = intent.get("qty")
        notional_req = intent.get("notional")
        if qty is None and notional_req is not None and q is not None:
            qty = round_qty(sym, float(notional_req) / float(q.price))
        qty = float(qty) if qty is not None else 0.0
        if is_broker and qty > 0:
            qty = float(account.round_qty(sym, qty))      # the broker's size step, SPEC §18.4
        acct_view, why = _valued(account, pm)
        settings = pf_settings.values(account.id)
        allocations = [{"source_id": s, "qty": float(v)} for s, v in (intent.get("allocations") or [])]
        if not allocations and intent.get("source_id") and qty > 0:
            allocations = [{"source_id": intent["source_id"], "qty": qty}]
        working = [o for o in list_orders(account.id, states=WORKING, limit=500)]
        window = int(settings["duplicate_window_seconds"])
        since = (now - timedelta(seconds=window)).astimezone(timezone.utc).isoformat(timespec="seconds")
        recent = [dict(o, source_id=(o["allocations"][0]["source_id"] if len(o["allocations"]) == 1 else None))
                  for o in list_orders(account.id, limit=200) if o["created_at"] >= since and o["state"] != "rejected"]
        crypto = fund_params.is_crypto(sym)
        tif = intent.get("tif") or (getattr(account, "default_tif", lambda _o: None)(origin) if is_broker else None) \
            or ("gtc" if origin in PROTECTIVE else "day")
        dl_now = risk.day_loss(account, now, acct_view.equity if acct_view else None, settings["daily_loss_pct"])
        results = checks.run(account, {"symbol": sym, "side": side, "qty": qty, "origin": origin, "type": otype,
                                       "tif": tif,
                                       "ref_price": intent.get("ref_price"), "allow_reduce": intent.get("allow_reduce"),
                                       "fresh_quote_price": fresh, "quote_error": why_q.get(sym),
                                       "source_id": intent.get("source_id") or (allocations[0]["source_id"]
                                                                                if len(allocations) == 1 else None)},
                             now=now, settings=settings, quote=q, acct_view=acct_view, valuation_error=why,
                             working=working, recent=recent, orders_today=_orders_today(account.id, now, crypto),
                             day_loss=dl_now)
        for _cls, _v in dl_now.items():         # review fix M1: a block seen here is latched for the class day too
            if _v["blocked"] and not _v.get("latched"):
                try:
                    risk.record_block(account, _cls, _v, now, "engine")
                except Exception:      # noqa: BLE001 — the check result above already blocks this order
                    logger.exception("latching the daily-loss block failed")
        ts = _now_iso(now)
        row = {"client_order_id": cid, "account_id": account.id, "created_at": ts, "updated_at": ts, "symbol": sym,
               "side": side, "qty": qty if qty > 0 else None, "notional_req": notional_req, "origin": origin,
               "tif": tif, "order_type": otype, "limit_price": intent.get("limit_price"),
               "stop_price": intent.get("stop_price"),
               "legs_json": json.dumps({"take_profit": intent.get("take_profit"), "stop_loss": intent.get("stop_loss")})
               if otype == "bracket" else None,
               "reason": (intent.get("reason") or None) and str(intent["reason"])[:500],
               "ref_price": intent.get("ref_price"), "state": "new", "filled_qty": 0.0, "fees": 0.0, "attempts": 0,
               "allow_reduce": 1 if intent.get("allow_reduce") else 0,
               "allocations_json": json.dumps(allocations), "checks_json": json.dumps(results),
               "execution_id": intent.get("execution_id")}
        _inflight.add(cid)
        try:
            with db._connect() as conn:
                _insert(conn, row)
                if origin == "manual":
                    conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'oms_order', ?)",
                                 (ts, actor, json.dumps({"client_order_id": cid, "account_id": account.id,
                                                         "symbol": sym, "side": side, "qty": qty,
                                                         "notional": notional_req, "reason": row["reason"]})))
            bad = checks.failed(results)
            if bad is not None:
                transition(cid, "rejected", note=f"pre-trade check {bad['check']}", fields={"reject_reason": bad["message"]})
                return get(cid), True
            if q is None and side not in checks.REDUCING:
                transition(cid, "rejected", note="no quote", fields={"reject_reason": f"no current quote for {sym} — "
                                                                                      "order not sent"})
                return get(cid), True
            _send(account, cid, pm, now)
        finally:
            _inflight.discard(cid)
            _remember_quotes(account, pm)
        return get(cid), True


ORDER_QUOTE_DEADLINE_SECONDS = 8.0          # all quote reads of ONE order together (retest 2026-10-23)
_quote_pool = None
_quote_pool_lock = threading.Lock()


def _pool():
    global _quote_pool
    with _quote_pool_lock:
        if _quote_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            _quote_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="oms-quote")
        return _quote_pool


def _quotes_within(account, symbols, deadline: float, fresh_for: Optional[str] = None):
    """({symbol: Quote | None}, {symbol: why missing}, fresh price of ``fresh_for`` | None): the account's quotes
    read in parallel, waiting at most ``deadline`` seconds for all of them together. A read still running then is
    abandoned (its result is never used) — no stand-in, the symbol is simply without a quote."""
    from concurrent.futures import wait
    syms = list(dict.fromkeys(symbols))
    jobs = {_pool().submit(accounts.quote_for, account, s): ("q", s) for s in syms}
    if fresh_for is not None:          # the price collar's own, independent read of the latest quote
        jobs[_pool().submit(accounts.quote_for, account, fresh_for)] = ("fresh", fresh_for)
    done, _late = wait(list(jobs), timeout=deadline)
    got, why, fresh = {s: None for s in syms}, {}, None
    for f, (kind, s) in jobs.items():
        if f not in done:
            if kind == "q":
                why[s] = f"no quote within {deadline:g} s"
                logger.info("OMS: no quote for %s within %g s — not waited for", s, deadline)
            continue
        try:
            qv = f.result()
        except Exception as ex:      # noqa: BLE001 — the reason is shown as the quote check
            if kind == "q":
                why[s] = str(getattr(ex, "user_message", None) or ex)[:200]
                logger.info("OMS: no quote for %s (%s)", s, why[s])
            continue
        if qv is None or not (getattr(qv, "price", 0) or 0) > 0:
            continue
        if kind == "q":
            got[s] = qv
        else:
            fresh = float(qv.price)
    return got, why, fresh


def _remember_quotes(account, pm: dict) -> None:
    """The quotes this order fetched (the traded symbol + every held one) become the account's last quotes — a fill
    between two runtime cycles (e.g. 24/7 crypto) is valued at once instead of "valuation paused" until the next
    cycle (retest 2026-10-23). GETs still never fetch (§17.11)."""
    try:
        from ..live import runtime
        runtime.default().remember_quotes(account.id, pm)
    except Exception:      # noqa: BLE001 — a cache update never fails the order
        logger.exception("OMS: could not remember the quotes")


def _resend_blocker(account, o: dict, now: Optional[datetime]) -> Optional[str]:
    """Why a non-protective order still ``submitted`` (a transient broker error) must NOT be re-sent (review fix
    H1): the account was halted / killed meanwhile, auto trading went off (runtime orders), or the class day's
    daily-loss block is latched (risk-increasing orders). None = it may be re-sent."""
    from ..portfolio import checks, risk
    if o["origin"] in PROTECTIVE:
        return None
    h = account.halt()
    if h["active"]:
        return f"not re-sent: the account was halted meanwhile ({h['reason'] or h['kind']})"
    if o["origin"] in ("runtime", "council"):
        from . import control
        if not control.autotrade()["enabled"]:
            return "not re-sent: auto trading was switched off meanwhile"
    if o["origin"] == "council":             # SP §22.5 (N2): the Council's own switches + switch-over + sandbox
        why = _council_blocker()
        if why:
            return f"not re-sent: {why}"
    if checks.increases_risk(o["side"]):
        from strategy_platform.core import params as fund_params
        cls = "crypto" if fund_params.is_crypto(o["symbol"]) else "equity"
        at = risk.latched(account, cls, now or accounts.now())
        if at:
            return f"not re-sent: the daily loss limit was reached meanwhile (at {at})"
    return None


def _council_blocker() -> Optional[str]:
    """Why a Council order may not be (re-)sent now, or None (SP §22.5): Council orders switched off, switch-over not
    done, sandbox mode. Unreadable state → blocked (fail closed)."""
    try:
        from strategy_platform.council import store as cl_store
        if cl_store.switchover() != "done":
            return "the Council is not switched over"
        if not cl_store.orders_enabled():
            return "Council orders were switched off meanwhile"
    except Exception:          # noqa: BLE001 — fail closed
        logger.exception("Council order gate unreadable")
        return "the Council order gate could not be read"
    try:
        import sandbox_profile
        if sandbox_profile.is_sandbox():
            return "sandbox mode — no Council orders"
    except Exception:          # noqa: BLE001
        return "the sandbox state could not be read"
    return None


def _send(account, cid: str, prices: Optional[dict], now: Optional[datetime] = None) -> None:
    o = get(cid)
    if o is None or o["state"] not in ("new", "submitted"):
        return
    if o["state"] == "submitted":           # a re-send (sync / next cycle): the situation may have changed (H1)
        why = _resend_blocker(account, o, now)
        if why is not None:
            transition(cid, "cancelled", note=why, fields={"reject_reason": why})
            return
    if not transition(cid, "submitted", note=f"attempt {o['attempts'] + 1}", fields={"attempts": o["attempts"] + 1}):
        return
    payload = {"client_order_id": cid, "symbol": o["symbol"], "side": o["side"], "qty": o["qty"],
               "notional": o["notional_req"] if o["qty"] is None else None,
               "origin": o["origin"], "rationale": f"{o['origin']}: {o['reason'] or ''}"[:200],
               "allow_reduce": o["allow_reduce"], "prices": prices, "now": now,
               "type": o.get("order_type") or "market", "tif": o["tif"], "limit_price": o.get("limit_price"),
               "stop_price": o.get("stop_price")}
    if payload["type"] == "bracket":
        spec = o.get("bracket") or {}
        payload["take_profit"], payload["stop_loss"] = spec.get("take_profit"), spec.get("stop_loss")
    ack, err = None, None
    for k in range(len(RETRY_DELAYS) + 1):
        try:
            ack = account.adapter.submit(payload)
            break
        except BrokerUnavailable as ex:
            err = ex
            if k < len(RETRY_DELAYS):
                _sleep(RETRY_DELAYS[k])
    if ack is None:
        o = get(cid)
        if o["attempts"] >= MAX_ATTEMPTS:
            transition(cid, "rejected", note="retries exhausted",
                       fields={"reject_reason": f"broker unreachable — gave up after {MAX_ATTEMPTS} attempts "
                                                f"({err.user_message if err else 'unknown'})"})
        else:
            with db._connect() as conn:
                _event(conn, cid, "submitted", "submitted", f"transient broker error — retried at the next cycle "
                                                            f"({err.user_message if err else ''})"[:300])
        return
    apply_ack(cid, ack)


def _broker_account(account_id: str) -> bool:
    try:
        return bool(getattr(accounts.get(account_id), "is_broker", False))
    except TradingError:
        return False


def apply_ack(cid: str, ack: dict) -> None:
    o = get(cid)
    if o is None:
        return
    broker = _broker_account(o["account_id"])
    if broker:
        # SPEC §18.4: book the reported fills into the mirror FIRST (idempotent via br_fill_marks) — also for an
        # order the OMS already closed (a late fill / a bracket leg of a filled parent)
        from .brokers import mirror
        mirror.book_ack(o, ack)
    if o["terminal"]:
        return
    st = ack.get("status")
    fields = {"broker_order_id": ack.get("broker_order_id")}
    if ack.get("broker_status") is not None:
        fields["broker_status"] = str(ack["broker_status"])[:40]
    if o["notional_req"] is not None and ack.get("qty"):
        fields["qty"] = float(ack["qty"])
    filled_now = float(ack.get("filled_qty") or 0.0)
    fill_fields = dict(fields, filled_qty=filled_now, avg_fill_price=ack.get("avg_price"), fees=ack.get("fees") or 0.0,
                       notional=ack.get("notional"))
    if broker and st == "partially_filled":
        if o["state"] == "submitted":
            transition(cid, "accepted", note="accepted at the broker", fields=fields)
        if filled_now > float(o["filled_qty"] or 0.0) + EPS:
            transition(cid, "partially_filled", note=f"filled {filled_now:g} so far", fields=fill_fields)
        return
    if broker and st in ("cancelled", "expired") and filled_now > EPS:
        if o["state"] == "submitted":
            transition(cid, "accepted", note="accepted at the broker", fields=fields)
        if filled_now > float(o["filled_qty"] or 0.0) + EPS:
            transition(cid, "partially_filled", note=f"filled {filled_now:g}", fields=fill_fields)
        transition(cid, "cancelled" if st == "cancelled" else "expired", note=f"rest {st} at the broker",
                   fields={"reject_reason": f"rest {st} at the broker after {filled_now:g} filled"})
        return
    if broker and st == "expired":
        if o["state"] == "submitted":
            transition(cid, "accepted", note="accepted at the broker", fields=fields)
        transition(cid, "expired", note="expired at the broker", fields={"reject_reason": ack.get("reject_reason")
                                                                         or "expired at the broker"})
        return
    if st == "accepted":
        if o["state"] == "submitted":
            transition(cid, "accepted", note="queued at the broker", fields=fields)
        return
    if st == "rejected":
        transition(cid, "rejected", note="rejected by the broker", fields=dict(fields, reject_reason=ack.get("reject_reason")))
        return
    if st == "cancelled":
        if o["state"] == "submitted":
            transition(cid, "accepted", note="queued at the broker", fields=fields)
        transition(cid, "cancelled", note="cancelled at the broker", fields={"reject_reason": ack.get("reject_reason")})
        return
    if st == "filled":
        planned = float(fields.get("qty") or o["qty"] or 0.0)
        filled = float(ack.get("filled_qty") or 0.0)
        fill_fields = dict(fields, filled_qty=filled, avg_fill_price=ack.get("avg_price"), fees=ack.get("fees") or 0.0,
                           notional=ack.get("notional"))
        if planned > 0 and filled < planned - EPS:
            transition(cid, "partially_filled", note=f"filled {filled:g} of {planned:g}", fields=fill_fields)
            transition(cid, "cancelled", note="rest cancelled",
                       fields={"reject_reason": f"rest cancelled: the broker filled {filled:g} of {planned:g} (reduced "
                                                "to buying power / the position)"})
        else:
            transition(cid, "filled", note="filled", fields=fill_fields)


def _cancel_submitted(account, cid: str, reason: str) -> None:
    """Review fix H1: an order still ``submitted`` (the send hit a transient broker error, or a crash came between
    send and ack). Ask the adapter: it knows the order → state synced, then a still-working order is cancelled AT
    THE ADAPTER; it does not know it → ``cancelled`` here (it is never re-sent). Adapter unreachable → a best-effort
    adapter cancel, then ``cancelled`` here with that note (never re-sent; for a real broker a late appearance is a
    reconciliation mismatch, SPEC §18)."""
    try:
        ack = account.adapter.order(cid)
        known = True
    except Exception as ex:     # noqa: BLE001 — BrokerUnavailable, store locked, …
        ack, known = None, False
        err = getattr(ex, "user_message", None) or type(ex).__name__
        try:
            account.adapter.cancel(cid, reason)
        except Exception:       # noqa: BLE001
            pass
        transition(cid, "cancelled", note=f"{reason} (broker unreachable: {err}) — never re-sent"[:300],
                   fields={"reject_reason": f"{reason} — cancelled before the broker confirmed it (broker "
                                            f"unreachable); it is never re-sent"[:300]})
        return
    if known and ack is None:
        transition(cid, "cancelled", note=f"{reason} (not at the broker — never re-sent)"[:300],
                   fields={"reject_reason": f"{reason} — cancelled before it reached the broker"[:300]})
        return
    apply_ack(cid, ack)
    o = get(cid)
    if o["terminal"]:
        return
    if o["state"] == "submitted":           # the adapter's state is not a working one we map: cancel there + here
        try:
            account.adapter.cancel(cid, reason)
        except Exception:       # noqa: BLE001
            logger.exception("adapter cancel of %s failed", cid)
        transition(cid, "cancelled", note=f"{reason} (broker state {ack.get('status')!r})"[:300],
                   fields={"reject_reason": reason})
        return
    ok = account.adapter.cancel(cid, reason)
    if getattr(account, "is_broker", False):
        ack2 = account.adapter.order(cid)
        if ack2 is not None:
            apply_ack(cid, ack2)
        o = get(cid)
        if not o["terminal"]:
            if not ok:
                raise TradingError(409, "not_cancellable", "The broker could not cancel this order.")
            with db._connect() as conn:
                _event(conn, cid, o["state"], o["state"], f"cancel requested at the broker ({reason})"[:300])
        return
    if ok:
        transition(cid, "cancelled", note=reason, fields={"reject_reason": reason})
    else:
        ack2 = account.adapter.order(cid)
        if ack2 is not None:
            apply_ack(cid, ack2)
        if not get(cid)["terminal"]:
            raise TradingError(409, "not_cancellable", "The broker could not cancel this order.")


def cancel(account, cid: str, reason: str = "cancelled by the user", actor: str = "user") -> dict:
    with lock_for(account.id):
        o = get(cid)
        if o is None or o["account_id"] != account.id:
            raise TradingError(404, "not_found", "No such order.")
        if o["state"] == "new":
            transition(cid, "cancelled", note=reason, fields={"reject_reason": reason})
        elif o["state"] == "submitted":
            _cancel_submitted(account, cid, reason)
        elif o["state"] in ("accepted", "partially_filled") and getattr(account, "is_broker", False):
            # a real broker cancels asynchronously (and may fill meanwhile): request, then read the state back;
            # still working -> "cancel requested", the poller / next sync completes it (SPEC §18.4)
            ok = account.adapter.cancel(cid, reason)
            ack = account.adapter.order(cid)
            if ack is not None:
                apply_ack(cid, ack)
            o = get(cid)
            if not o["terminal"]:
                if not ok:
                    raise TradingError(409, "not_cancellable", "The broker could not cancel this order.")
                with db._connect() as conn:
                    _event(conn, cid, o["state"], o["state"], f"cancel requested at the broker ({reason})"[:300])
        elif o["state"] in ("accepted", "partially_filled"):
            ok = account.adapter.cancel(cid, reason)
            if ok:
                transition(cid, "cancelled", note=reason, fields={"reject_reason": reason})
            else:
                ack = account.adapter.order(cid)
                if ack is not None:
                    apply_ack(cid, ack)
                o = get(cid)
                if not o["terminal"]:
                    raise TradingError(409, "not_cancellable", "The broker could not cancel this order.")
        else:
            raise TradingError(409, "not_cancellable", f"An order in state {o['state']} cannot be cancelled.")
        try:
            db.add_audit(actor, "oms_cancel", {"client_order_id": cid, "account_id": account.id, "reason": reason})
        except Exception:     # noqa: BLE001 — the cancel itself is a safety action
            logger.exception("audit row for the cancel failed")
        return get(cid)


def cancel_working(account, reason: str, origins: Optional[Tuple[str, ...]] = None) -> List[dict]:
    """Cancels every working (not filled) order of the account (optionally only some origins)."""
    out = []
    with lock_for(account.id):
        for o in list_orders(account.id, states=WORKING, limit=1000):
            if origins is not None and o["origin"] not in origins:
                continue
            try:
                out.append(cancel(account, o["client_order_id"], reason, actor="engine"))
            except Exception as ex:      # noqa: BLE001 — one order must not stop cancelling the others (H1)
                logger.warning("cancel of %s (%s) failed: %s", o["client_order_id"], o["state"],
                               getattr(ex, "user_message", None) or ex)
                continue
        # broker rows the OMS does not know (never left there) — cancel at the broker too
        for p in account.store.pending_orders():
            if p.get("client_order_id") is None and (origins is None or "margin" in origins):
                account.store.update_order(p["id"], status="cancelled", reject_reason=reason[:200])
    return out


def _session_close(day) -> Optional[datetime]:
    from ..core import calendars
    try:
        b = calendars.session_bounds(day)
    except Exception:       # noqa: BLE001
        return None
    return b[1] if b else None


def sync(account, now: Optional[datetime] = None) -> dict:
    """Reconciles the account's working orders with the broker (see the module docstring)."""
    from strategy_platform.core import params as fund_params
    from ..core import calendars
    now = now or accounts.now()
    out = {"synced": 0, "expired": 0, "imported": 0, "resent": 0, "lost": 0, "stale_new": 0}
    with lock_for(account.id):
        for o in list_orders(account.id, states=WORKING, limit=2000):
            cid = o["client_order_id"]
            if cid in _inflight:
                continue
            age = (now - datetime.fromisoformat(o["created_at"])).total_seconds()
            if o["state"] == "new":
                if age >= STALE_NEW_SECONDS:
                    transition(cid, "cancelled", note="not sent before a restart",
                               fields={"reject_reason": "not sent before a restart (the intent is stale)"})
                    out["stale_new"] += 1
                continue
            ack = account.adapter.order(cid)
            if o["state"] == "submitted" and ack is None:
                if age < RESEND_WINDOW_SECONDS and o["attempts"] < MAX_ATTEMPTS:
                    _send(account, cid, None, now)      # re-checks halt / auto trading / daily loss first (H1)
                    if get(cid)["state"] == "cancelled":
                        out["not_resent"] = out.get("not_resent", 0) + 1
                    else:
                        out["resent"] += 1
                else:
                    transition(cid, "rejected", note="lost", fields={"reject_reason": "lost before reaching the broker"})
                    out["lost"] += 1
                continue
            if ack is not None:
                apply_ack(cid, ack)
                out["synced"] += 1
            o2 = get(cid)
            if o2["state"] in ("accepted", "partially_filled") and o2["tif"] == "day" \
                    and not fund_params.is_crypto(o2["symbol"]) and not getattr(account, "is_broker", False):
                created = datetime.fromisoformat(o2["created_at"])
                close = _session_close(calendars.ny_date(created)) if calendars.is_trading_day(
                    calendars.ny_date(created)) else None
                if close is None:
                    try:
                        nxt = calendars.next_trading_day(calendars.ny_date(created))
                        close = _session_close(nxt)
                    except Exception:      # noqa: BLE001
                        close = None
                if close is not None and now >= close and account.adapter.cancel(cid, "expired: day order"):
                    transition(cid, "expired", note="day order expired at the session close",
                               fields={"reject_reason": "expired: not filled before the session close"})
                    out["expired"] += 1
        # broker-initiated orders (margin calls / liquidations) -> OMS rows
        cur = int(account.store.meta_get(IMPORT_CURSOR) or 0)
        last = cur
        for r in account.store.orders_after(cur, 1000):
            last = r["id"]
            if r.get("client_order_id") is not None or r.get("legacy_id") is not None:
                continue
            from .brokers.paper import ack_of
            ack = ack_of(r)
            cid = f"brk-{account.id}-{r['id']}"
            if get(cid) is not None:
                continue
            ts = r["ts"]
            row = {"client_order_id": cid, "account_id": account.id, "created_at": ts, "updated_at": ts,
                   "symbol": r["symbol"], "side": r["side"], "qty": float(r["qty"]), "origin": "margin",
                   "tif": "gtc", "reason": (r.get("rationale") or "broker margin action")[:500], "state": "submitted",
                   "filled_qty": 0.0, "fees": 0.0, "attempts": 1, "allow_reduce": 1, "allocations_json": "[]",
                   "checks_json": "[]", "broker_order_id": str(r["id"])}
            with db._connect() as conn:
                _insert(conn, row)
            _link_client_id(account, r["id"], cid)
            apply_ack(cid, dict(ack, client_order_id=cid))
            out["imported"] += 1
        if last != cur:
            account.store.meta_set(IMPORT_CURSOR, str(last))
    return out


def _link_client_id(account, broker_order_id: int, cid: str) -> None:
    with account.store._c() as conn:
        conn.execute("UPDATE pa_orders SET client_order_id = ? WHERE id = ? AND account_id = ? AND client_order_id IS NULL",
                     (cid, int(broker_order_id), account.id))
