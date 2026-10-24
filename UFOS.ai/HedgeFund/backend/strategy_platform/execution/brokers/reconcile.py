"""Reconciliation of a broker account (SPEC §18.5): broker = truth, mirror = expectation; a mismatch HALTS that
broker account (no new orders at all — only the broker kill switch), alerts with a clear reason, and stays halted
until the user resolves it (``resolve``: recheck | cancel_unknown_orders | accept_broker). Never auto-corrected.

``run(acc, trigger)``: OMS sync (fills booked) → ONE broker snapshot → OMS sync again; if
the second sync changed anything (a fill arrived meanwhile) the round is repeated (≤ 3) so a fill racing the
snapshot is not reported as a mismatch. Then positions (exact, to the broker's step), the cash basis (tolerance)
and the open orders (both directions) are compared. Results are stored insert-only (``br_reconciliations``); the
state (status / halted) lives in the account's ``pa_meta`` key ``reconcile``.

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER / DEMO TRADING ONLY.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ...core.errors import UserFacingError
from ...storage import db
from .. import accounts
from ..errors import BrokerUnavailable, TradingError
from . import mirror
from .common import EPS, fnum

logger = logging.getLogger(__name__)

TRIGGERS = ("start", "cycle", "connect", "manual", "resolve", "poll")
# (minimum in the account's currency, fraction of equity — of the cash basis when the broker reports no equity)
TOLERANCE = {"alpaca_paper": (1.00, 0.0001), "binance_demo": (1.00, 0.0005)}
QTY_TOL = 1e-7
ACCEPT_CONFIRM = "ACCEPT BROKER VALUES"
ACTIONS = ("recheck", "cancel_unknown_orders", "accept_broker")
STABLE_ROUNDS = 3


def _working(acc) -> List[dict]:
    from .. import oms
    return oms.list_orders(acc.id, states=oms.WORKING, limit=2000)


def _signature(acc) -> tuple:
    w = tuple(sorted((o["client_order_id"], o["state"], round(float(o["filled_qty"] or 0), 8)) for o in _working(acc)))
    return (mirror.fills_signature(acc.id), w)


def expected_basis(acc) -> float:
    return float(acc.store.cash_balance() or 0.0)


def tolerance(acc, equity: Optional[float]) -> float:
    lo, frac = TOLERANCE.get(acc.broker_name, (1.0, 0.0001))
    return max(lo, frac * abs(float(equity or 0.0)))


def _snapshot_stable(acc, now, user: bool) -> dict:
    from .. import oms
    last = None
    for _k in range(STABLE_ROUNDS):
        oms.sync(acc, now)
        sig = _signature(acc)
        snap = acc.adapter.snapshot(user=user)
        oms.sync(acc, now)
        if _signature(acc) == sig:
            return snap
        last = snap
    raise BrokerUnavailable("the broker's state kept changing during the check (fills arriving) — retried at the next "
                            "cycle")


def compare(acc, snap: dict) -> dict:
    from strategy_platform.core import params as fund_params
    mirror_pos = {p["symbol"]: float(p["qty"]) for p in acc.store.get_positions()}
    broker_pos: Dict[str, float] = {}
    for p in snap.get("positions") or []:
        broker_pos[p["symbol"]] = broker_pos.get(p["symbol"], 0.0) + float(p["qty"])
    positions, mismatches = [], []
    for sym in sorted(set(mirror_pos) | set(broker_pos)):
        e, b = mirror_pos.get(sym, 0.0), broker_pos.get(sym, 0.0)
        ok = abs(e - b) <= QTY_TOL
        positions.append({"symbol": sym, "expected": e, "broker": b, "ok": ok})
        if not ok:
            hint = ""
            if acc.broker_name == "binance_demo":
                hint = (" — the balance changed outside the platform (traded, transferred or reset in the demo UI, or a "
                        "fee paid in another asset)") if not sym.startswith("UNMAPPED:") else \
                    " — an asset the platform does not trade changed (e.g. a fee paid in BNB)"
            mismatches.append({"kind": "position", "symbol": sym,
                               "message": f"{sym} {b:g} at the broker vs {e:g} expected{hint}"})
    acct = snap.get("account") or {}
    broker_basis = fnum(acct.get("cash_basis"))
    exp = expected_basis(acc)
    tol = tolerance(acc, acct.get("equity") if acct.get("equity") is not None else broker_basis)
    cash_ok = broker_basis is not None and abs(broker_basis - exp) <= tol
    cash = {"basis": "cash", "expected": exp,
            "currency": acct.get("currency") or "USD",
            "broker": broker_basis, "diff": (broker_basis - exp) if broker_basis is not None else None,
            "tolerance": tol, "ok": cash_ok}
    if not cash_ok:
        what = "cash (USDT — not converted)" if acc.broker_name == "binance_demo" else "cash"
        shown = f"{broker_basis:,.2f}" if broker_basis is not None else "not reported"
        mismatches.append({"kind": "cash", "symbol": None,
                           "message": f"{what} {shown} at the broker vs {exp:,.2f} expected (tolerance {tol:,.2f}) — "
                                      "e.g. a dividend, fee or funding payment the platform has not booked"})
    working = _working(acc)
    w_cids = {o["client_order_id"] for o in working}
    w_boids = {str(o["broker_order_id"]) for o in working if o.get("broker_order_id")}
    unknown, seen_cids, seen_boids = [], set(), set()
    for bo in snap.get("open_orders") or []:
        cid, boid = bo.get("client_order_id"), str(bo.get("broker_order_id"))
        known = (cid in w_cids) or (boid in w_boids)
        bo["known"] = known
        seen_cids.add(cid)
        seen_boids.add(boid)
        if not known:
            unknown.append(bo)
            mismatches.append({"kind": "unknown_order", "symbol": bo.get("symbol"),
                               "message": f"an open {bo.get('side')} order for {bo.get('qty')} {bo.get('symbol')} at "
                                          f"the broker is unknown to the platform (broker id {boid})"})
    missing = []
    for o in working:
        if o["state"] not in ("accepted", "partially_filled"):
            continue
        if o["client_order_id"] in seen_cids or (o.get("broker_order_id") and str(o["broker_order_id"]) in seen_boids):
            continue
        missing.append({"client_order_id": o["client_order_id"], "state": o["state"]})
        mismatches.append({"kind": "missing_order", "symbol": o["symbol"],
                           "message": f"order {o['client_order_id']} ({o['state']}) is not open at the broker"})
    return {"positions": positions, "cash": cash,
            "open_orders": {"unknown_at_broker": unknown, "missing_at_broker": missing,
                            "ok": not unknown and not missing},
            "mismatches": mismatches}


def _now_iso() -> str:
    return accounts.now().astimezone(timezone.utc).isoformat(timespec="seconds")


def _store_result(acc, trigger: str, status: str, res: dict) -> dict:
    ts = _now_iso()
    with db._connect() as conn:
        cur = conn.execute("INSERT INTO br_reconciliations (account_id, ts, trigger, status, result_json) VALUES"
                           " (?, ?, ?, ?, ?)", (acc.id, ts, trigger, status, json.dumps(res, default=str)))
        rid = int(cur.lastrowid)
    res.update(id=rid, account_id=acc.id, at=ts, trigger=trigger, status=status)
    return res


def run(acc, trigger: str = "cycle", now: Optional[datetime] = None, user: bool = False) -> dict:
    """One reconciliation (see the module docstring). Never raises for broker failures (``unreachable``)."""
    from ...portfolio import ledger
    from .. import oms
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {', '.join(TRIGGERS)}")
    now = now or accounts.now()
    with oms.lock_for(acc.id):
        try:
            if user:                     # a user action makes ONE real attempt even while the link backs off
                with acc.adapter.user_attempt():
                    snap = _snapshot_stable(acc, now, True)
            else:
                snap = _snapshot_stable(acc, now, False)
        except (BrokerUnavailable, UserFacingError) as ex:
            if isinstance(ex, TradingError):
                raise
            msg = getattr(ex, "user_message", str(ex))[:300]
            res = _store_result(acc, trigger, "unreachable", {"positions": [], "cash": None,
                                                              "open_orders": {"unknown_at_broker": [],
                                                                              "missing_at_broker": [], "ok": None},
                                                              "mismatches": [], "reason": None, "error": msg})
            st = mirror.set_reconcile_state(acc.store, status="unreachable", id=res["id"], at=res["at"], trigger=trigger,
                                            error=msg)
            res["halted"] = bool(st.get("halted"))
            return res
        try:
            ledger.attribute(acc)
        except Exception:        # noqa: BLE001 — attribution retries next cycle; reconciliation is independent
            logger.exception("%s: attribution before reconciliation failed", acc.id)
        cmp_ = compare(acc, snap)
        status = "ok" if not cmp_["mismatches"] else "mismatch"
        reason = None
        if status == "mismatch":
            reason = "Reconciliation mismatch: " + "; ".join(m["message"] for m in cmp_["mismatches"])[:600]
        res = _store_result(acc, trigger, status, dict(cmp_, reason=reason, error=None))
        acc.store.meta_set(mirror.SNAPSHOT_KEY, json.dumps({"at": snap.get("at") or res["at"],
                                                            "account": snap.get("account"),
                                                            "positions": snap.get("positions") or [],
                                                            "open_orders": snap.get("open_orders") or []},
                                                           default=str))
        prev = mirror.reconcile_state(acc.store)
        fields = {"status": status, "id": res["id"], "at": res["at"], "trigger": trigger, "error": None}
        if status == "mismatch":
            fields["reason"] = reason
            if not prev.get("halted"):
                fields.update(halted=True, halted_at=res["at"], mismatch_id=res["id"])
                _alert(acc, reason, res["id"])
        elif trigger in ("resolve", "connect"):
            fields.update(halted=False, reason=None, halted_at=None, mismatch_id=None)
        elif not prev.get("halted"):
            fields["reason"] = None
        st = mirror.set_reconcile_state(acc.store, **fields)
        res["halted"] = bool(st.get("halted"))
        return res


def _alert(acc, reason: str, rid: int) -> None:
    try:
        acc.store.add_event("halt", f"Broker halted: reconciliation mismatch ({acc.id})", None,
                            f"{reason}. No new orders on {acc.id} until you resolve it in Settings → Brokers "
                            "(the kill switch still works). Paper/demo trading · simulated money.")
    except Exception:            # noqa: BLE001
        logger.exception("mismatch event failed")
    try:
        db.add_audit("engine", "broker_reconcile_mismatch", {"account_id": acc.id, "reconciliation_id": rid,
                                                             "reason": reason})
    except Exception:            # noqa: BLE001
        logger.exception("audit row broker_reconcile_mismatch failed")
    logger.warning("%s halted: %s", acc.id, reason)


def latest(account_id: str) -> Optional[dict]:
    rows = history(account_id, 1)
    return rows[0] if rows else None


def history(account_id: str, limit: int = 20) -> List[dict]:
    with db._connect() as conn:
        rows = conn.execute("SELECT * FROM br_reconciliations WHERE account_id = ? ORDER BY id DESC LIMIT ?",
                            (account_id, max(1, min(int(limit), 200)))).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["result_json"])
        d.update(id=r["id"], account_id=r["account_id"], at=r["ts"], trigger=r["trigger"], status=r["status"])
        out.append(d)
    return out


# --- resolve ---------------------------------------------------------------------------------------------

def _holders(acc, sym: str, direction: float) -> List[tuple]:
    from ...portfolio import ledger
    hold = ledger.holdings(acc.id)
    return [(s, q[sym]) for s, q in sorted(hold.items()) if sym in q and (q[sym] > 0) == (direction > 0)]


def _accept_broker(acc, snap: dict, reason: str) -> dict:
    from ...portfolio import ledger
    from .. import oms
    detail = {"positions": [], "cash_adjustment": 0.0, "orders_cancelled": []}
    broker_pos = {p["symbol"]: p for p in snap.get("positions") or []}
    ts = _now_iso()
    rebase = {}
    if acc.broker_name == "binance_demo":
        # long-only spot (SPEC §18.14): a change BELOW the starting balance or of an asset the platform does not trade
        # is not a platform position — accepting it moves that asset's baseline (recorded), never a short in the mirror
        for sym in list(broker_pos):
            p = broker_pos[sym]
            if sym.startswith("UNMAPPED:") or float(p.get("qty") or 0.0) < 0:
                rebase[str(p.get("asset") or sym.split(":")[-1])] = float(p.get("qty") or 0.0)
                del broker_pos[sym]
    with acc.broker._lock:
        with acc.store.atomic() as conn:
            if rebase:
                base = dict(mirror.baseline(acc.store) or {})
                for asset, q in rebase.items():
                    base[asset] = float(base.get(asset, 0.0)) + q
                acc.store.meta_set(mirror.BASELINE_KEY, json.dumps(base, sort_keys=True))
                detail["baseline_changes"] = [{"asset": k, "change": v} for k, v in sorted(rebase.items())]
            mirror_pos = {p["symbol"]: p for p in acc.store.get_positions()}
            rows = []
            for sym in sorted(set(mirror_pos) | set(broker_pos)):
                e = float((mirror_pos.get(sym) or {}).get("qty") or 0.0)
                bp = broker_pos.get(sym) or {}
                b = float(bp.get("qty") or 0.0)
                if abs(e - b) <= QTY_TOL:
                    continue
                px = fnum(bp.get("avg_price")) or fnum((mirror_pos.get(sym) or {}).get("avg_price")) \
                    or fnum(bp.get("mark_price")) or 0.0
                delta = b - e
                if abs(b) <= QTY_TOL:
                    acc.store.upsert_position(sym, 0.0, None, updated_at=ts, opened_at=None,
                                              realized_pnl=float((mirror_pos.get(sym) or {}).get("realized_pnl") or 0),
                                              borrow_rate_pct=None, last_price=fnum(bp.get("mark_price")))
                else:
                    acc.store.upsert_position(sym, b, px, updated_at=ts,
                                              opened_at=(mirror_pos.get(sym) or {}).get("opened_at") or ts,
                                              realized_pnl=float((mirror_pos.get(sym) or {}).get("realized_pnl") or 0),
                                              borrow_rate_pct=None, last_price=fnum(bp.get("mark_price")))
                ref = f"resolve:{ts}"
                note = f"reconciliation accepted by the user: {sym} {e:g} -> {b:g} ({reason})"[:300]
                # shrink the holders pro rata (as if they sold to _unallocated at px); extra goes to _unallocated
                same_dir = _holders(acc, sym, e) if abs(e) > QTY_TOL else []
                if same_dir and (abs(b) < abs(e) or (b > 0) != (e > 0)):
                    take = min(abs(delta), abs(e)) * (1.0 if delta > 0 else -1.0)
                    parts = ledger.split_qty(sym, abs(take), [(s, abs(q)) for s, q in same_dir])
                    for s, q in parts:
                        q_signed = q if take > 0 else -q
                        rows.append({"source_id": s, "kind": "cross", "symbol": sym, "qty": q_signed, "price": px,
                                     "amount": -q_signed * px, "ref": ref, "note": note})
                        rows.append({"source_id": accounts.UNALLOCATED, "kind": "cross", "symbol": None, "qty": 0.0,
                                     "price": None, "amount": q_signed * px, "ref": ref, "note": note})
                    rest = delta - take
                else:
                    rest = delta
                if abs(rest) > QTY_TOL:
                    rows.append({"source_id": accounts.UNALLOCATED, "kind": "cross", "symbol": sym, "qty": rest,
                                 "price": px, "amount": 0.0, "ref": ref, "note": note})
                detail["positions"].append({"symbol": sym, "from": e, "to": b, "price": px})
            ledger.insert_entries(conn, acc.id, rows, ts)
            broker_basis = fnum((snap.get("account") or {}).get("cash_basis"))
            if broker_basis is not None:
                adj = broker_basis - expected_basis(acc)
                if abs(adj) > 1e-6:
                    acc.store.add_cash_entry(ts, "broker_adjustment", adj, note=(f"reconciliation accepted by the "
                                                                                 f"user ({reason})")[:300])
                    detail["cash_adjustment"] = adj
    open_ids = {str(o.get("broker_order_id")) for o in snap.get("open_orders") or []}
    open_cids = {o.get("client_order_id") for o in snap.get("open_orders") or []}
    for o in _working(acc):
        if o["state"] in ("accepted", "partially_filled") and o["client_order_id"] not in open_cids and \
                str(o.get("broker_order_id")) not in open_ids:
            if oms.transition(o["client_order_id"], "cancelled", note="resolved by the user",
                              fields={"reject_reason": "not open at the broker (resolved by the user)"}):
                detail["orders_cancelled"].append(o["client_order_id"])
    ledger.attribute(acc)
    return detail


def resolve(acc, action: str, reason: str, confirm: Optional[str] = None, actor: str = "user") -> dict:
    from .. import oms
    from . import service
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    service.require_active(acc.id)
    # fix1023: no typed confirm phrase for accept_broker any more (``confirm`` accepted and ignored)
    if service.connection(acc.id)["status"] != "connected":
        raise TradingError(409, "not_connected", f"{acc.id} is not connected.")
    st = mirror.reconcile_state(acc.store)
    if action == "accept_broker" and not st.get("halted"):
        # review fix L-b: the mirror is only overwritten to clear a reconcile halt, never on a healthy account
        raise TradingError(409, "nothing_to_resolve", f"{acc.id} is not halted by a reconciliation mismatch — "
                                                      "accepting the broker's values is only possible to resolve one.")
    if not st.get("halted") and st.get("status") == "ok" and action != "recheck":
        raise TradingError(409, "nothing_to_resolve", "The last reconciliation is ok — nothing to resolve.")
    detail: dict = {}
    with oms.lock_for(acc.id), acc.adapter.user_attempt():
        if action in ("cancel_unknown_orders", "accept_broker"):
            try:
                snap = _snapshot_stable(acc, accounts.now(), True)
            except (BrokerUnavailable, UserFacingError) as ex:
                raise TradingError(409, "unreachable", f"The broker could not be read: "
                                                       f"{getattr(ex, 'user_message', ex)}") from ex
            if action == "cancel_unknown_orders":
                cmp_ = compare(acc, snap)
                done = []
                for bo in cmp_["open_orders"]["unknown_at_broker"]:
                    try:
                        ok = acc.adapter.remote_call("cancel", bo.get("client_order_id") or "", "resolve: unknown order",
                                                     broker_order_id=str(bo.get("broker_order_id")), user=True)
                    except BrokerUnavailable as ex:
                        raise TradingError(409, "unreachable", ex.user_message) from ex
                    done.append({"broker_order_id": bo.get("broker_order_id"), "cancelled": bool(ok)})
                detail = {"cancelled": done}
            else:
                detail = _accept_broker(acc, snap, reason)
        res = run(acc, "resolve", user=True)
    ts = _now_iso()
    with db._connect() as conn:
        cur = conn.execute("INSERT INTO br_resolutions (account_id, ts, action, actor, reason, reconciliation_id,"
                           " detail_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (acc.id, ts, action, actor, reason, res.get("id"), json.dumps(detail, default=str)))
        rid = int(cur.lastrowid)
        conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'broker_resolve', ?)",
                     (ts, actor, json.dumps({"account_id": acc.id, "action": action, "reason": reason,
                                             "reconciliation_id": res.get("id"), "status": res.get("status"),
                                             "detail": detail}, default=str)))
    if res.get("status") == "ok":
        acc.store.add_event("resume", f"Broker reconciliation resolved ({acc.id})", None,
                            f"{action}{': ' + reason if reason else ''}. New orders are possible again (auto trading "
                            "is unchanged).")
    return {"resolution": {"id": rid, "ts": ts, "action": action, "reason": reason, "detail": detail},
            "reconciliation": res}
