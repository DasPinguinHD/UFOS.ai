"""``GET /v2/portfolio/summary`` (SPEC §17.15, contract UFOS.ai/docs/tasks/PORTFOLIO_SUMMARY_API.md) — the cheap,
stored-values-only summary for the MainWindow Hedge Fund tile (30 s poll).

Reads ONLY hedgefund.db (pf_accounts / br_accounts, pa_equity snapshots, pa_positions, pa_meta, oms_orders, meta) and
the registered old engine's own store (before the switch-over). No Account / broker object is built, no broker call,
no market data, no quote, no valuation: equity and day P&L come from the stored equity snapshots the runtime writes;
a value that is not stored is null WITH a reason (no stand-in). It does NOT touch the client heartbeat (a tile must
not keep the dead-man switch alive). Amounts stay in the account's own currency (binance-demo: USDT, never summed
with USD).

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..core import notices as _notices
from ..storage import db

logger = logging.getLogger(__name__)

SCHEMA = 1
SPARK_POINTS = 30                       # newest stored equity points per account (oldest first)
NO_SNAPSHOT = "no equity snapshot stored yet — the runtime writes one every 5–30 min while the backend runs"
NO_REFERENCE = "no equity snapshot before {mid} (start of the {cls} day) — day P&L not available"
NOTICE = _notices.PAPER_EXECUTION


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(v) -> Optional[dict]:
    try:
        d = json.loads(v) if v else None
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _day_start(cls: str, now: datetime) -> datetime:
    """Start of the current class day: 00:00 New York (equity / mixed accounts) or 00:00 UTC (crypto-only venue)."""
    from ..core import calendars
    if cls == "crypto":
        return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    ny = now.astimezone(calendars.NY)
    return datetime(ny.year, ny.month, ny.day, tzinfo=calendars.NY)


def _halt(store, broker: bool) -> dict:
    if broker:
        from ..execution.brokers import mirror
        rs = mirror.reconcile_state(store)
        if rs.get("halted"):
            return {"active": True, "kind": "reconcile", "reason": rs.get("reason") or "reconciliation mismatch",
                    "at": rs.get("halted_at") or rs.get("at")}
    h = _json(store.meta_get("halt"))
    if not h or not h.get("active"):
        return {"active": False, "kind": None, "reason": None, "at": None}
    return {"active": True, "kind": h.get("kind"), "reason": h.get("reason"), "at": h.get("at")}


def _account(row: dict, broker: bool, now: datetime) -> dict:
    from ..execution import oms
    from ..execution.paper_store import PlatformPaperStore
    store = PlatformPaperStore(row["id"])
    currency = "USDT" if row.get("broker") == "binance_demo" else "USD"
    cls = "crypto" if row.get("broker") == "binance_demo" else "equity"
    pts = store.equity_points(limit=SPARK_POINTS)
    last = pts[-1] if pts else None
    mid = _day_start(cls, now)
    ref = store.equity_point_before(mid)
    equity = float(last["equity"]) if last else None
    day_pnl = day_pct = None
    reason = None
    if last is None:
        reason = NO_SNAPSHOT
    elif ref is None or not ref.get("equity"):
        reason = NO_REFERENCE.format(mid=_iso(mid), cls="New York" if cls == "equity" else "UTC")
    else:
        day_pnl = equity - float(ref["equity"])
        day_pct = day_pnl / float(ref["equity"]) * 100.0
    kill_pending = bool(store.meta_get("kill_pending")) if broker else False
    return {"id": row["id"], "label": row.get("label"), "broker": row.get("broker") or "internal_paper",
            "paper": True, "currency": currency,
            "equity": equity, "as_of": last["ts"] if last else None,
            "day_pnl": day_pnl, "day_pnl_pct": day_pct,
            "day_reference": {"ts": ref["ts"], "equity": float(ref["equity"])} if ref else None,
            "day_starts_at": _iso(mid), "reason": reason,
            "positions_count": len(store.get_positions()),
            "working_orders": len(oms.list_orders(row["id"], states=oms.WORKING, limit=1000)),
            "halt": _halt(store, broker), "kill_pending": kill_pending,
            "spark": [{"ts": p["ts"], "equity": float(p["equity"])} for p in pts]}


def _autotrade() -> dict:
    from ..execution import control
    d = _json(db.meta_get(control.AUTOTRADE_KEY)) or {}
    return {"enabled": bool(d.get("enabled")), "changed_at": d.get("changed_at"), "changed_by": d.get("changed_by")}


def _council() -> dict:
    from ..council import store as cl_store
    try:
        sw = cl_store.switchover()
        err = None
    except Exception as ex:      # noqa: BLE001 — shown, never guessed
        sw, err = None, f"the Council switch-over state is unreadable ({type(ex).__name__})"
    return {"switchover": sw, "error": err, "orders_enabled": cl_store.orders_enabled(),
            "label": "not validated — AI strategy"}


def _old_engine(switchover: Optional[str]) -> dict:
    """Before the switch-over: the registered old engine's own stored flags (its store, no fund import)."""
    from ..execution import control
    if switchover in ("pending", "done"):
        return {"applicable": False, "halted": None, "autopilot": None, "reason": "switched over to the Council"}
    eng = control.legacy_engine()
    if eng is None:
        return {"applicable": True, "halted": None, "autopilot": None, "reason": "the old engine is not running"}
    try:
        h = eng.store.get_halted()
        ap = bool(eng.store.get_autopilot())
    except Exception as ex:      # noqa: BLE001
        return {"applicable": True, "halted": None, "autopilot": None,
                "reason": f"the old engine's state is unreadable ({type(ex).__name__})"}
    return {"applicable": True, "halted": bool(h.get("active")), "autopilot": ap, "reason": None}


def summary(now: Optional[datetime] = None) -> dict:
    from ..execution import accounts
    now = (now or accounts.now()).astimezone(timezone.utc)
    out_accounts: List[dict] = []
    for row in accounts.rows():
        out_accounts.append(_account(row, accounts.is_broker(row["id"]), now))
    totals: Dict[str, Any] = {}
    for cur in sorted({a["currency"] for a in out_accounts}):
        group = [a for a in out_accounts if a["currency"] == cur]
        missing = [a["id"] for a in group if a["equity"] is None]
        no_day = [a["id"] for a in group if a["day_pnl"] is None]
        totals[cur] = {"accounts": len(group),
                       "equity": None if missing else sum(a["equity"] for a in group),
                       "day_pnl": None if no_day else sum(a["day_pnl"] for a in group),
                       "missing": sorted(set(missing) | set(no_day))}
    halted = [a["id"] for a in out_accounts if a["halt"]["active"]]
    killed = [a["id"] for a in out_accounts if a["halt"]["active"] and a["halt"]["kind"] == "kill"]
    council = _council()
    return {"schema": SCHEMA, "generated_at": _iso(now), "paper_only": True, "notice": NOTICE,
            "stored_values_only": True,
            "accounts": out_accounts, "totals": totals,
            "positions_count": sum(a["positions_count"] for a in out_accounts),
            "autotrade": _autotrade(),
            "halts": {"any": bool(halted), "accounts": halted},
            "kill": {"active": bool(killed), "accounts": killed,
                     "pending": [a["id"] for a in out_accounts if a["kill_pending"]]},
            "council": council, "old_engine": _old_engine(council["switchover"])}
