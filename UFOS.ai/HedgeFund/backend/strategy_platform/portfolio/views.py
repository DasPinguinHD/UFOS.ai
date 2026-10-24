"""Read models of the "Portfolio & Trading" view (SPEC §17.11). Reads only the database
and the runtime's last quotes — no network in a GET: a held symbol without a current
quote is shown without a value and the account's valuation is "paused" (never valued
at a stored price).

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..execution import accounts, control, oms
from ..storage import db

from ..core import notices as _notices  # noqa: E402 — S10-H context notice (c), = Shared/AiContent.cs
NOTICE = _notices.PAPER_EXECUTION


def quotes_for(acc) -> Dict[str, Any]:
    from ..live import runtime
    pm = dict(runtime.default().last_quotes.get(acc.id) or {})
    for p in acc.store.get_positions():
        pm.setdefault(p["symbol"], None)
    return pm


def _marks(pm: Dict[str, Any]) -> Dict[str, Optional[float]]:
    return {s: (float(q.price) if q is not None else None) for s, q in pm.items()}


def half_spread_fn(acc):
    from strategy_platform.core import params as fund_params

    def h(symbol: str) -> float:
        return fund_params.fees_bps_for(symbol, acc.params) / 2.0 / 10000.0
    return h


def account_summary(acc, now: Optional[datetime] = None, pm: Optional[dict] = None) -> dict:
    from strategy_platform.execution.paper_broker import StateUnavailable
    from . import ledger, risk
    from . import settings as pf_settings
    now = now or accounts.now()
    pm = pm if pm is not None else quotes_for(acc)
    st = pf_settings.values(acc.id)
    view, valuation = None, {"ok": True, "message": None}
    try:
        view = acc.broker.account(pm)
    except StateUnavailable as ex:
        valuation = {"ok": False, "message": str(ex)[:300]}
    broker_link = reconciliation = None
    if getattr(acc, "is_broker", False):          # stage 8 (SPEC §18.10)
        from ..execution.brokers import mirror
        lk = acc.link.view()
        rs = mirror.reconcile_state(acc.store)
        broker_link = {"state": lk["state"], "message": lk["message"]}
        reconciliation = {"status": rs.get("status") or "never", "at": rs.get("at"),
                          "reason": rs.get("reason") if rs.get("halted") or rs.get("status") == "mismatch"
                          else (rs.get("error") if rs.get("status") == "unreachable" else None)}
    return {"id": acc.id, "label": acc.label, "broker": getattr(acc, "broker_name", "internal_paper"), "paper": True,
            "origin": acc.origin, "broker_link": broker_link, "reconciliation": reconciliation,
            "opened_at": acc.row["created_at"], "account": view.to_dict() if view is not None else None,
            "valuation": valuation, "halt": acc.halt(),
            "day_loss": risk.day_loss(acc, now, view.equity if view else None, st["daily_loss_pct"]),
            "drawdown": {"pct": view.drawdown_pct if view else None, "kill_pct": st["drawdown_kill_pct"]},
            "positions_count": len(acc.store.get_positions()),
            "working_orders": len(oms.list_orders(acc.id, states=oms.WORKING, limit=1000)),
            "sources": ledger.source_ids(acc.id)}


def positions(acc, pm: Optional[dict] = None) -> List[dict]:
    from . import ledger
    pm = pm if pm is not None else quotes_for(acc)
    try:
        equity = acc.broker.account(pm).equity
    except Exception:            # noqa: BLE001 — valuation paused: weights unknown
        equity = None
    hold = ledger.holdings(acc.id)
    out = []
    for p in acc.broker.positions(pm, True):
        mv = p.market_value
        out.append({"symbol": p.symbol, "qty": p.qty, "asset_class": p.asset_class, "instrument": p.instrument,
                    "side": p.side, "avg_price": p.avg_price, "last_price": p.last_price, "market_value": mv,
                    "weight": (mv / equity) if (mv is not None and equity and equity > 0) else None,
                    "unrealized_pnl": p.unrealized_pnl, "realized_pnl": p.realized_pnl, "opened_at": p.opened_at,
                    "borrow_rate_pct": p.borrow_rate_pct,
                    "by_source": [{"source_id": s, "qty": q[p.symbol]} for s, q in sorted(hold.items())
                                  if p.symbol in q]})
    return out


def _source_meta(acc) -> Dict[str, dict]:
    from ..live import incubation
    from . import allocator
    meta: Dict[str, dict] = {}
    last = allocator.latest(acc.id)
    weights = {w["source_id"]: w["weight"] for w in (last or {}).get("weights") or []}
    if (last or {}).get("status") == "no_allocation":     # review fix L2: no weight, not a silent 0
        weights = {k: None for k in weights}
    for r in incubation.run_rows():
        if r["account_id"] != acc.id or r["source_id"] in meta:
            continue
        row = db.strategy_get(r["strategy_id"])
        end = incubation.end_event(r["id"])
        meta[r["source_id"]] = {"kind": "strategy", "name": row["name"] if row else r["strategy_id"],
                                "strategy_id": r["strategy_id"], "version": r["version"],
                                "state": row["state"] if row else None,
                                "incubation_id": r["id"] if (end is None or end["event"] == "completed") else None,
                                "allocation_weight": weights.get(r["source_id"])}
    meta[accounts.UNALLOCATED] = {"kind": "unallocated", "name": "Unallocated cash", "strategy_id": None,
                                  "version": None, "state": None, "incubation_id": None, "allocation_weight": None}
    meta[accounts.COUNCIL_SOURCE] = {"kind": "council", "name": "Council of AIs", "strategy_id": None, "version": None,
                                     "state": None, "incubation_id": None, "allocation_weight": None}
    return meta


def source_summaries(acc, pm: Optional[dict] = None) -> List[dict]:
    from . import ledger
    pm = pm if pm is not None else quotes_for(acc)
    marks = _marks(pm)
    meta = _source_meta(acc)
    states = ledger.all_states(acc.id, marks, half_spread_fn(acc))
    override = {}
    if acc.id == accounts.COUNCIL:
        start = acc.store.get_starting_capital()
        if start is not None:
            override[accounts.COUNCIL_SOURCE] = float(start)
    out = []
    for sid, st in states.items():
        m = meta.get(sid) or {"kind": "strategy", "name": sid, "strategy_id": None, "version": None, "state": None,
                              "incubation_id": None, "allocation_weight": None}
        eq = st.get("equity")
        cap_in = override.get(sid, st["capital_in"])
        pos = []
        for s, v in sorted(st["positions"].items()):
            px = marks.get(s)
            pos.append({"symbol": s, "qty": v["qty"], "avg_price": v["avg_price"], "last_price": px,
                        "market_value": v["qty"] * px if px is not None else None,
                        "unrealized_pnl": v["qty"] * (px - v["avg_price"]) if px is not None else None})
        lmv, smv = st.get("long_mv"), st.get("short_mv")
        ok = bool(eq and eq > 0 and lmv is not None)
        out.append(dict(m, source_id=sid, capital_in=cap_in, cash=st["cash"], equity=eq,
                        valued=bool(st.get("valued", False)), positions=pos,
                        pnl={"realized": st["realized"], "unrealized": st.get("unrealized"),
                             "total": (eq - cap_in) if eq is not None else None},
                        costs=st["costs"],
                        exposure={"long_mv": lmv, "short_mv": smv,
                                  "gross_pct": ((lmv + smv) / eq * 100.0) if ok else None,
                                  "net_pct": ((lmv - smv) / eq * 100.0) if ok else None}))
    return out


def state(now: Optional[datetime] = None) -> dict:
    from ..execution import migration
    from ..live import runtime
    now = now or accounts.now()
    legacy = migration.legacy_state(control.legacy_engine())
    rt = runtime.default().status()
    return {"now": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "paper_only": True, "notice": NOTICE,
            "autotrade": control.autotrade(),
            "runtime": {k: rt[k] for k in ("running", "cycle_in_progress", "last_cycle_at", "next_cycle_at",
                                           "last_error")},
            "legacy_account": legacy, "accounts": [account_summary(a, now) for a in accounts.all_accounts()],
            "can": {"open_account": not accounts.exists(accounts.MAIN),
                    "migrate": legacy["status"] == "active" and not accounts.exists(accounts.COUNCIL)}}
