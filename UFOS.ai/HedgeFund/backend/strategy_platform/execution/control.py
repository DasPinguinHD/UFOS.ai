"""Platform control (SPEC §17.1, §17.6, §17.7): the auto-trading switch, the ONE
heartbeat shared with the old engine (dead-man switch), the kill switch covering both
engines, per-account halts and resume.

- **Auto trading** (hedgefund.db meta ``oms.autotrade`` = {enabled, changed_at,
  changed_by}): forced OFF at every backend start; ON needs an audit row first and is
  refused while an account is halted; OFF always works (cancels the queued runtime
  orders, positions kept).
- **Heartbeat**: ``touch()`` (every authenticated platform GET) also touches the old
  engine (``engine.touch_client``); ``last_seen()`` = the newer of both. Timeout = the
  fund params' ``autopilot_heartbeat_minutes`` (one setting for both engines).
- **Kill** (``kill_platform``): every platform account: halt (kind ``kill``), auto
  trading off, cancel all working orders, close all positions with protective OMS
  orders (queued while the market is closed), sub-ledgers of a flat symbol crossed
  flat. ``kill_all`` (POST /v2/oms/kill) = platform kill + the old engine's kill
  (``engine.kill(propagate=False)``); each half reports its own failure.

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER TRADING ONLY.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..storage import db
from . import accounts
from .errors import TradingError

logger = logging.getLogger(__name__)

AUTOTRADE_KEY = "oms.autotrade"
_state_lock = threading.RLock()
_legacy_engine = None
_last_seen: Optional[datetime] = None
AUDIT_UNAVAILABLE = "audit log unavailable — the action was not carried out"


def set_legacy_engine(engine) -> None:
    global _legacy_engine
    _legacy_engine = engine


def legacy_engine():
    return _legacy_engine


# --- heartbeat ----------------------------------------------------------------------------------------

def touch(now: Optional[datetime] = None) -> None:
    global _last_seen
    _last_seen = (now or accounts.now()).astimezone(timezone.utc)
    eng = _legacy_engine
    if eng is not None:
        try:
            eng.touch_client(_last_seen)
        except Exception:      # noqa: BLE001
            logger.exception("touch_client failed")


def last_seen() -> Optional[datetime]:
    vals = [v for v in (_last_seen, getattr(_legacy_engine, "last_client_seen", None)) if v is not None]
    return max(vals) if vals else None


def heartbeat_minutes() -> float:
    from strategy_platform.core import params as fund_params
    lim = fund_params.LIMITS["autopilot_heartbeat_minutes"]
    v = None
    eng = _legacy_engine
    if eng is not None:
        try:
            _ver, p = eng.store.effective_params()
            v = p.get("autopilot_heartbeat_minutes")
        except Exception:      # noqa: BLE001
            v = None
    if v is None:
        v = fund_params.DEFAULTS["autopilot_heartbeat_minutes"]
    return max(float(lim["min"]), min(float(lim["max"]), float(v)))


# --- auto trading -------------------------------------------------------------------------------------

def autotrade() -> dict:
    v = db.meta_get(AUTOTRADE_KEY)
    try:
        d = json.loads(v) if v else {}
    except ValueError:
        d = {}
    enabled = bool(d.get("enabled"))
    seen = last_seen()
    hb = heartbeat_minutes()
    return {"enabled": enabled, "heartbeat_minutes": hb,
            "last_client_seen": seen.isoformat(timespec="seconds") if seen else None,
            "off_at": (seen + timedelta(minutes=hb)).isoformat(timespec="seconds") if (enabled and seen) else None,
            "changed_at": d.get("changed_at"), "changed_by": d.get("changed_by")}


def _write_autotrade(enabled: bool, actor: str) -> None:
    db.meta_set(AUTOTRADE_KEY, json.dumps({"enabled": bool(enabled), "changed_at": db.utc_now_iso(),
                                           "changed_by": actor}))


def _cancel_runtime_orders(reason: str) -> int:
    """Cancels the queued (working) runtime AND Council orders (SP §22.5) of every account — ONE account at a time
    (no thread ever holds two account trade locks, SP §22.5 D10); protective / manual / kill orders stay."""
    from . import oms
    n = 0
    for acc in accounts.all_accounts():
        try:
            n += len(oms.cancel_working(acc, reason, origins=("runtime", "council")))
        except Exception:     # noqa: BLE001
            logger.exception("cancelling the runtime orders of %s failed", acc.id)
    return n


def council_orders_off(cause: str, actor: str = "engine", acc_id: Optional[str] = None) -> Optional[int]:
    """Forces ``council.orders_enabled`` OFF (kill / drawdown kill / startup, SP §22.5) and cancels the queued
    Council orders; audit row ``council_orders_off``. Never raises (a kill must not fail on it). Returns the number of
    cancelled orders, or None when nothing was switched (the Council store is not there yet)."""
    from . import oms
    try:
        from strategy_platform.council import store as cl_store
    except Exception:          # noqa: BLE001
        return None
    n = 0
    try:
        was_on = cl_store.orders_enabled()
        cl_store.set_orders_enabled(False, actor, cause)
        if accounts.exists(accounts.COUNCIL) and (acc_id in (None, accounts.COUNCIL)):
            n = len(oms.cancel_working(accounts.get(accounts.COUNCIL), f"cancelled: Council orders off ({cause})",
                                       origins=("council",)))
        if was_on or n:
            db.add_audit(actor if actor in db.AUDIT_ACTORS else "engine", "council_orders_off",
                         {"cause": cause, "cancelled_orders": n})
    except Exception:          # noqa: BLE001
        logger.exception("forcing the Council orders off (%s) failed", cause)
    return n


def set_autotrade(enabled: bool, actor: str = "user") -> dict:
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    with _state_lock:
        if enabled:
            halted = [a.id for a in accounts.all_accounts() if a.halt()["active"]]
            if halted:
                raise TradingError(409, "halted", f"Account {', '.join(halted)} is halted — resume it before switching "
                                                  "auto trading on.")
            try:
                db.add_audit(actor, "autotrade", {"enabled": True})
            except Exception as ex:   # noqa: BLE001
                raise TradingError(503, "audit_unavailable", AUDIT_UNAVAILABLE) from ex
            _write_autotrade(True, actor)
            if actor == "user":
                touch()
            return autotrade()
        _write_autotrade(False, actor)
        n = _cancel_runtime_orders("cancelled: auto trading off")
        try:
            db.add_audit(actor, "autotrade", {"enabled": False, "cancelled_orders": n})
        except Exception:       # noqa: BLE001 — switching off always works
            logger.exception("audit row for auto trading off failed")
        return autotrade()


def force_off_at_start() -> None:
    try:
        if autotrade()["enabled"]:
            db.add_audit("system", "autotrade", {"enabled": False, "reason": "backend start"})
        _write_autotrade(False, "system")
    except Exception:          # noqa: BLE001
        logger.exception("could not reset the auto-trading flag")
    council_orders_off("startup", "system")      # SP §22.5: the user turns the Council orders on again


def check_heartbeat(now: Optional[datetime] = None) -> bool:
    """Dead-man switch: auto trading off when no client was seen for heartbeat_minutes."""
    try:
        st = autotrade()
    except Exception:          # noqa: BLE001
        logger.exception("heartbeat: auto-trading flag unreadable")
        return False
    if not st["enabled"]:
        return False
    now = (now or accounts.now()).astimezone(timezone.utc)
    seen = last_seen()
    if seen is not None and now - seen < timedelta(minutes=st["heartbeat_minutes"]):
        return False
    _write_autotrade(False, "engine")
    n = _cancel_runtime_orders("cancelled: auto trading off (no app heartbeat)")
    try:
        db.add_audit("engine", "autotrade_heartbeat_off", {"last_client_seen": st["last_client_seen"],
                                                           "timeout_minutes": st["heartbeat_minutes"],
                                                           "cancelled_orders": n})
    except Exception:          # noqa: BLE001
        logger.exception("audit row autotrade_heartbeat_off failed")
    logger.warning("platform auto trading switched off: no client heartbeat since %s", st["last_client_seen"])
    return True


# --- kill / resume --------------------------------------------------------------------------------------

def kill_account(acc, reason: str, kind: str, actor: str, prices: Optional[dict] = None) -> dict:
    """Halt + cancel all + close all on one account (protective OMS orders)."""
    from strategy_platform.core import params as fund_params
    from ..portfolio import ledger
    from . import oms
    origin = "kill" if kind == "kill" else "drawdown"
    if getattr(acc, "is_broker", False):
        why = accounts.sandbox_block_reason(acc.id)
        if why:      # sandbox profile: the kill switch acts only inside the sandbox — no broker call, not touched
            return {"account_id": acc.id, "cancelled": 0, "orders": [], "sandbox_blocked": why,
                    "broker": {"ok": True, "error": None, "cancelled_at_broker": 0, "note": why, "flat": None,
                               "pending": False, "not_flat": [], "untouched_orders": 0, "untouched_note": None}}
        # stage 8 broker account (SPEC §18.6): cancel ALL at the broker, close what the BROKER holds
        from .brokers import service
        r = service.kill(acc, reason, kind, actor)
        return {"account_id": acc.id, "cancelled": int(r["oms_cancelled"]) + int(r["cancelled_at_broker"]),
                "orders": r["orders"], "broker": {k: r[k] for k in ("ok", "error", "cancelled_at_broker", "note", "flat",
                                                                    "pending", "not_flat", "untouched_orders",
                                                                    "untouched_note")}}
    with oms.lock_for(acc.id):
        acc.set_halt(True, kind, reason[:300])
        cancelled = oms.cancel_working(acc, f"cancelled: {reason[:100]}")
        if acc.id == accounts.COUNCIL:          # SP §22.5 (N2): every kill forces the Council orders off
            council_orders_off("kill" if kind == "kill" else "drawdown", actor, acc.id)
        orders = []
        for p in acc.store.get_positions():
            side = "sell" if p["qty"] > 0 else "cover"
            try:
                o, _ = oms.place(acc, {"symbol": p["symbol"], "side": side, "qty": abs(p["qty"]), "origin": origin,
                                       "reason": reason[:200], "allow_reduce": True,
                                       "client_order_id": oms.new_client_id("kill" if kind == "kill" else "ddk")},
                                 actor=actor, prices=prices)
                orders.append(o)
            except Exception as ex:     # noqa: BLE001 — one failing symbol must not stop the others
                logger.exception("kill: closing %s failed", p["symbol"])
                orders.append({"symbol": p["symbol"], "state": "error", "reject_reason": str(ex)[:200]})
        try:
            ledger.attribute(acc)
            flatten_sources(acc, prices)
        except Exception:        # noqa: BLE001
            logger.exception("kill: sub-ledger attribution failed (retried next cycle)")
        try:
            acc.store.add_event("halt", "Kill switch: account halted" if kind == "kill" else "Drawdown kill: account halted",
                                None, f"{reason}. Auto trading off; all positions are being closed "
                                      f"({len(orders)} order(s)). Paper trading · simulated money.")
        except Exception:        # noqa: BLE001
            logger.exception("kill event failed")
    return {"account_id": acc.id, "cancelled": len(cancelled), "orders": orders}


def flatten_sources(acc, prices: Optional[dict] = None) -> int:
    """Symbols the ACCOUNT no longer holds but sources hold against each other (long vs short) are crossed
    flat at the current quote (internal, no order)."""
    from ..portfolio import ledger
    held = {p["symbol"]: p["qty"] for p in acc.store.get_positions()}
    hold = ledger.holdings(acc.id)
    syms = sorted({s for q in hold.values() for s in q} - set(held))
    rows = []
    for sym in syms:
        q = (prices or {}).get(sym)
        if q is None:
            try:
                q = accounts.quote_for(acc, sym)
            except Exception:      # noqa: BLE001
                q = None
        if q is None:
            continue
        px = float(q.price if hasattr(q, "price") else q)
        for src, qs in hold.items():
            v = qs.get(sym, 0.0)
            if abs(v) > ledger.EPS:
                rows.append({"source_id": src, "kind": "cross", "symbol": sym, "qty": -v, "price": px,
                             "amount": v * px, "ref": "flatten", "note": "flattened against other sources"})
    if rows:
        with db._connect() as conn:
            ledger.insert_entries(conn, acc.id, rows)
    return len(rows)


def kill_platform(reason: Optional[str] = None, actor: str = "user") -> dict:
    reason = (str(reason).strip() if reason else "") or "Kill switch pressed"
    out = {"ok": True, "error": None, "accounts": []}
    try:
        _write_autotrade(False, actor if actor in ("user", "engine", "system") else "user")
    except Exception as ex:      # noqa: BLE001
        logger.exception("kill: auto trading flag")
        out.update(ok=False, error=f"auto trading flag: {ex}"[:200])
    try:
        accs = accounts.all_accounts()
    except Exception as ex:      # noqa: BLE001
        logger.exception("kill: accounts unreadable")
        return {"ok": False, "error": f"platform accounts unreadable: {getattr(ex, 'user_message', ex)}"[:300],
                "accounts": []}
    for acc in accs:
        try:
            out["accounts"].append(kill_account(acc, f"Kill switch: {reason[:200]}", "kill", actor))
        except Exception as ex:  # noqa: BLE001
            logger.exception("kill of %s failed", acc.id)
            out["ok"] = False
            out["accounts"].append({"account_id": acc.id, "error": str(ex)[:200], "cancelled": 0, "orders": []})
    try:
        db.add_audit(actor if actor in db.AUDIT_ACTORS else "user", "platform_kill",
                     {"reason": reason, "accounts": [{"account_id": a["account_id"], "orders": len(a.get("orders") or [])}
                                                     for a in out["accounts"]]})
    except Exception:            # noqa: BLE001
        logger.exception("audit row platform_kill failed")
    return out


def kill_all(reason: Optional[str] = None, actor: str = "user") -> dict:
    """POST /v2/oms/kill: the platform kill AND the old engine's kill (sync; call in a worker thread)."""
    platform = kill_platform(reason, actor)
    legacy = {"ok": True, "error": None, "halted": None}
    eng = _legacy_engine
    if eng is None:
        legacy = {"ok": False, "error": "the old engine is not running", "halted": None}
    else:
        try:
            res = _run_coro(eng.kill((reason or "")[:200] or None, actor, propagate=False))
            legacy["halted"] = bool(res.get("halted"))
        except Exception as ex:  # noqa: BLE001
            logger.exception("kill (old engine half) failed")
            legacy = {"ok": False, "error": f"{type(ex).__name__}: {ex}"[:200], "halted": None}
    return {"platform": platform, "legacy": legacy}


_loop_ref = None


def set_loop(loop) -> None:
    """main.py: the backend's event loop (the old engine's kill is a coroutine)."""
    global _loop_ref
    _loop_ref = loop


def _run_coro(coro):
    loop = _loop_ref
    if loop is not None and loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=60)
    return asyncio.run(coro)


def resume(account_id: str, reason: str, actor: str = "user") -> dict:
    from strategy_platform.execution.paper_broker import StateUnavailable
    acc = accounts.get(account_id)
    if getattr(acc, "is_broker", False):
        from .brokers import mirror, service
        if mirror.reconcile_state(acc.store).get("halted"):
            raise TradingError(409, "reconcile_mismatch", f"{acc.id} is halted by a reconciliation mismatch — resolve "
                                                          "it in Settings → Brokers first.")
        if service.kill_pending(acc):
            raise TradingError(409, "kill_pending", f"The kill switch on {acc.id} is still waiting for the broker "
                                                    "(positions not closed yet).")
    pm = {}
    for p in acc.store.get_positions():
        try:
            pm[p["symbol"]] = accounts.quote_for(acc, p["symbol"])
        except Exception:        # noqa: BLE001
            pm[p["symbol"]] = None
    try:
        view = acc.broker.account(pm)
    except StateUnavailable as ex:
        raise TradingError(503, "valuation_paused", f"The account cannot be valued ({ex}) — cannot resume safely "
                                                    "(the drawdown peak could not be rebased). Try again later.")
    try:
        db.add_audit(actor, "platform_resume", {"account_id": acc.id, "was": acc.halt(), "reason": reason,
                                               "peak_equity_rebased_to": view.equity})
    except Exception as ex:      # noqa: BLE001
        raise TradingError(503, "audit_unavailable", AUDIT_UNAVAILABLE) from ex
    acc.set_halt(False)
    acc.store.set_peak_equity(view.equity)
    acc.store.add_event("resume", "Account resumed (auto trading stays off)", None,
                        f"Halt cleared{': ' + reason if reason else ''}. Switch auto trading on explicitly to trade "
                        "again.")
    return acc
