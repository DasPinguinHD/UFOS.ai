"""Stateful side of the allocator (SPEC §17.4): which sources are managed, when a
rebalance is due, and the capital transfers between ``_unallocated`` and the sources
(virtual ``capital`` sub-ledger entries; cash only moves INSIDE the account).

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..execution import accounts
from ..execution.errors import TradingError
from ..storage import db
from . import allocator, ledger

logger = logging.getLogger(__name__)

FIXED_SOURCES = (accounts.UNALLOCATED, accounts.COUNCIL_SOURCE)


def managed_sources(account) -> List[dict]:
    from ..live import incubation
    out = []
    for r in incubation.active_runs():
        if r["account_id"] != account.id:
            continue
        row = db.strategy_get(r["strategy_id"])
        exp = json.loads(r["expected_json"]) if r.get("expected_json") else {}
        out.append({"source_id": r["source_id"], "strategy_id": r["strategy_id"], "version": r["version"],
                    "name": row["name"] if row else r["strategy_id"], "calendar": r["calendar"], "run_id": r["id"],
                    "holdout_returns": [(d, math.exp(lr) - 1.0) for d, lr in (exp.get("returns") or [])]})
    return sorted(out, key=lambda s: s["source_id"])


def marks_for(account, symbols) -> Dict[str, Optional[float]]:
    out = {}
    for s in sorted(set(symbols)):
        try:
            q = accounts.quote_for(account, s)
        except Exception:        # noqa: BLE001 — unvalued: the caller refuses to act on it
            q = None
        out[s] = float(q.price) if q is not None else None
    return out


def params(account) -> dict:
    from . import settings as pf_settings
    s = pf_settings.values(account.id)
    return {k: s[k] for k in ("alloc_max_weight", "alloc_min_weight", "alloc_max_total", "alloc_lambda",
                              "alloc_drift_band")}


def due(account, now: Optional[datetime] = None) -> Optional[str]:
    now = (now or accounts.now()).astimezone(timezone.utc)
    managed = managed_sources(account)
    last = allocator.latest(account.id)
    ids = {s["source_id"] for s in managed}
    if managed and last is None:
        return "initial allocation"
    if last is not None and {w["source_id"] for w in last.get("weights") or []} != ids:
        return "strategy set changed"
    if managed and last is not None and last["created_at"][:7] != now.isoformat()[:7]:
        return "monthly rebalance"
    states = ledger.all_states(account.id)
    for sid, st in states.items():
        if sid not in ids and sid not in FIXED_SOURCES and not st["positions"] and abs(st["cash"]) > 1e-6:
            return "release capital of a strategy that left incubation"
    return None


def rebalance(account, reason: str, actor: str = "engine", now: Optional[datetime] = None) -> dict:
    """Computes the weights, moves capital, stores the allocation (+ audit). Returns the allocation payload."""
    now = (now or accounts.now()).astimezone(timezone.utc)
    ledger.attribute(account)
    managed = managed_sources(account)
    ids = [s["source_id"] for s in managed]
    held = {sym for st in ledger.all_states(account.id).values() for sym in st["positions"]}
    marks = marks_for(account, held)
    states = ledger.all_states(account.id, marks)
    unvalued = sorted(sym for sym, m in marks.items() if m is None)
    if unvalued:
        raise TradingError(503, "valuation_paused", f"No current quote for {', '.join(unvalued)} — the allocation "
                                                    "waits (no stored-price valuation).")
    un = states.get(accounts.UNALLOCATED) or {"cash": 0.0, "equity": 0.0, "positions": {}}
    eq = {sid: float((states.get(sid) or {}).get("equity") or 0.0) for sid in ids}
    pool = float(un.get("equity") or 0.0) + math.fsum(eq.values())
    p = params(account)
    inputs = [{"source_id": s["source_id"], "name": s["name"], "calendar": s["calendar"],
               "ledger_returns": ledger.daily_returns(account.id, s["source_id"]),
               "holdout_returns": s["holdout_returns"]} for s in managed]
    wres = allocator.weights_result(inputs, p)
    w = wres["weights"]
    targets = {r["source_id"]: r["weight"] * pool for r in w}
    for r in w:
        r["target_capital"] = targets[r["source_id"]]
    if wres["status"] == "ok":
        moves = allocator.plan_transfers(pool, targets, eq, float(un.get("cash") or 0.0), p["alloc_drift_band"])
    else:
        # review fix L2: no allocation — no capital is moved to or from the managed strategies (the reason is stored
        # and shown); only the release of strategies that left incubation below still happens
        moves = []
        for r in w:
            r["target_capital"] = None
    # strategies that left the managed set and are flat give their cash back
    for sid, st in states.items():
        if sid not in ids and sid not in FIXED_SOURCES and not st["positions"] and abs(st["cash"]) > 1e-6:
            moves.insert(0, (sid, -st["cash"]))
    ts = now.isoformat(timespec="seconds")
    rows = []
    for sid, amt in moves:
        rows.append({"source_id": sid, "kind": "capital", "amount": amt, "ref": "allocation", "ts": ts,
                     "note": f"allocation: {reason}" if reason else "allocation"})
        rows.append({"source_id": accounts.UNALLOCATED, "kind": "capital", "amount": -amt, "ref": "allocation",
                     "ts": ts, "note": f"allocation to {sid}: {reason}" if reason else f"allocation to {sid}"})
    result = {"pool": pool, "weights": w,
              "unallocated_weight": (max(0.0, 1.0 - math.fsum(r["weight"] for r in w)) if wres["status"] == "ok"
                                     else None),
              "transfers": [{"source_id": s, "amount": a} for s, a in moves], "params": p,
              "status": wres["status"], "status_reason": wres["reason"]}
    with account.broker._lock:
        with db._connect() as conn:
            ledger.insert_entries(conn, account.id, rows, ts)
            conn.execute("INSERT INTO pf_allocations (account_id, created_at, actor, reason, result_json) VALUES"
                         " (?, ?, ?, ?, ?)", (account.id, ts, actor, reason[:500], json.dumps(result, default=str)))
            conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'allocation', ?)",
                         (ts, actor, json.dumps({"account_id": account.id, "reason": reason, "transfers":
                                                 result["transfers"]}, default=str)))
    return payload(account, now)


def payload(account, now: Optional[datetime] = None) -> dict:
    now = (now or accounts.now()).astimezone(timezone.utc)
    last = allocator.latest(account.id)
    p = params(account)
    nxt_year, nxt_month = (now.year + (1 if now.month == 12 else 0)), (1 if now.month == 12 else now.month + 1)
    alloc = None
    if last is not None:
        alloc = {"id": last["id"], "created_at": last["created_at"], "reason": last["reason"], "actor": last["actor"],
                 "pool": last.get("pool"), "unallocated_weight": last.get("unallocated_weight"),
                 "weights": last.get("weights") or [], "transfers": last.get("transfers") or [],
                 "status": last.get("status") or "ok", "status_reason": last.get("status_reason")}
    return {"account_id": account.id, "allocation": alloc,
            "params": {"vol_window": allocator.VOL_WINDOW, "vol_min": allocator.VOL_MIN,
                       "corr_window": allocator.CORR_WINDOW, "corr_min": allocator.CORR_MIN,
                       "lambda": p["alloc_lambda"], "max_weight": p["alloc_max_weight"],
                       "min_weight": p["alloc_min_weight"], "max_total": p["alloc_max_total"],
                       "drift_band": p["alloc_drift_band"], "rebalance": "monthly"},
            "next_rebalance": f"{nxt_year:04d}-{nxt_month:02d}"}
