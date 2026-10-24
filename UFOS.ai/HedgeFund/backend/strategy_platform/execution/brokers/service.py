"""Broker service (SPEC §18.1, §18.2, §18.6, §18.9): connection (test / connect / disconnect), status views,
routing of strategies to accounts, the kill switch per broker, the 15 s poller (fills of working orders, fresh
reconciliations, pending kills) and the start-up reconciliation.

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER / DEMO TRADING ONLY.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ...core.errors import UserFacingError
from ...storage import db
from .. import accounts
from ..errors import BrokerUnavailable, TradingError
from . import common, live_guard, mirror
from .common import Link, classify_failure, fnum

logger = logging.getLogger(__name__)

MIN_OPENING_BALANCE = 1_000.0
POLL_SECONDS = 15.0
RECONCILE_EVERY = timedelta(minutes=5)
from ...core import notices as _notices  # noqa: E402 — S10-H context notice (c), = Shared/AiContent.cs
NOTICE = _notices.PAPER_EXECUTION


def _meta(account_id: str) -> dict:
    if account_id == "alpaca-paper":
        from . import alpaca_paper as m
        return {"broker": "alpaca_paper", "label": mirror.LABELS[account_id], "venue": "Alpaca — paper trading",
                "instruments": "US stocks and ETFs", "keys": [m.KEY_ID_SECRET, m.SECRET_KEY_SECRET], "module": m,
                "chip": "PAPER · SIMULATED FUNDS", "currency": "USD", "currency_note": None}
    if account_id in accounts.RETIRED:
        # S10-K: a retired broker has no adapter module any more — the record of an old row only (blocked, 409)
        return {"broker": mirror.BROKER_OF[account_id], "label": mirror.LABELS[account_id],
                "venue": "Kraken Futures — demo (retired 2026-10-19)", "instruments": "none (retired)", "keys": [],
                "module": None, "chip": "RETIRED", "currency": "USD", "currency_note": None}
    if account_id == "binance-demo":
        from . import binance_demo as m
        return {"broker": "binance_demo", "label": mirror.LABELS[account_id], "venue": "Binance Spot — Demo Mode",
                "instruments": "Crypto spot BASE-USD → BASE/USDT (long only)", "keys": [m.KEY_SECRET, m.SECRET_SECRET],
                "module": m, "chip": m.CHIP, "currency": m.QUOTE, "currency_note": m.CURRENCY_NOTE}
    raise TradingError(404, "not_found", "No such broker account (alpaca-paper | binance-demo).")


def require_active(account_id: str) -> None:
    """409 ``retired`` for a retired broker account (SPEC §18.13 "Kraken demo retired") — no broker call follows."""
    why = accounts.retired_reason(account_id)
    if why:
        raise TradingError(409, "retired", f"{account_id}: {why}", sources=list(accounts.RETIRED_SOURCES))
    why = accounts.sandbox_block_reason(account_id)
    if why:                              # sandbox profile: no broker call at all (SPEC "Sandbox profile")
        raise TradingError(409, "sandbox_blocked", f"{account_id}: {why}")


RETIRED_NOTE = ("Retired 2026-10-19: Kraken decommissioned its Futures demo environment (Kraken support article "
                "360024809011 — \"decommissioned at 13 UTC on Monday, 14 July\"; demo-futures.kraken.com now redirects "
                "to pro.kraken.com/app). Not connectable or routable; the keys can be removed in Keys & data. The crypto "
                "broker is now binance-demo (Binance Spot demo, long only).")


def confirm_phrase(account_id: str) -> str:
    return f"CONNECT {account_id.upper()}"


def connection(account_id: str) -> dict:
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM br_connections WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                         (account_id,)).fetchone()
    if r is None:
        return {"status": "never_connected", "since": None, "by": None, "reason": None}
    return {"status": r["status"], "since": r["ts"], "by": r["actor"], "reason": r["reason"]}


def _remote(account_id: str):
    """The connected account's adapter (MirroredAdapter) or, before the first connect, a plain adapter wrapped with
    the broker's link bookkeeping."""
    if accounts.exists(account_id) and connection(account_id)["status"] == "connected":
        acc = accounts.get(account_id)
        return acc.adapter, acc
    return _Standalone(account_id), None


class _Standalone:
    """remote_call() for a broker account that is not connected (test / first connect)."""

    def __init__(self, account_id: str):
        self.account_id = account_id
        self.remote = mirror.remote_adapter(account_id)
        self.link: Link = mirror.link(account_id)

    def remote_call(self, fn_name: str, *args, user: bool = True, **kw):
        try:
            out = getattr(self.remote, fn_name)(*args, **kw)
        except Exception as ex:      # noqa: BLE001
            self.link.fail(classify_failure(ex), getattr(ex, "user_message", None) or type(ex).__name__)
            raise
        self.link.ok()
        return out


def _sdk_info(account_id: str, remote) -> Optional[dict]:
    if _meta(account_id)["module"] is None:
        return None                      # retired (S10-K): no SDK involved
    fn = getattr(remote, "sdk_info", None)
    if callable(fn):
        return fn()
    return _meta(account_id)["module"].sdk_info()


def _keys(account_id: str) -> List[dict]:
    from ... import secrets
    out = []
    for name in _meta(account_id)["keys"]:
        e = {"name": name, "label": secrets.SECRETS[name], "present": None, "error": None}
        try:
            e["present"] = bool(secrets.get_secret(name))
        except Exception as ex:      # noqa: BLE001 — status only
            e["error"] = getattr(ex, "user_message", None) or "The Credential Manager could not be read."
        out.append(e)
    return out


# --- status views -------------------------------------------------------------------------------------------

def _snapshot_meta(acc) -> Optional[dict]:
    if acc is None:
        return None
    v = acc.store.meta_get(mirror.SNAPSHOT_KEY)
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


def _routes_to(account_id: str) -> List[dict]:
    out = []
    for sid, aid in latest_routes().items():
        if aid == account_id:
            row = db.strategy_get(sid)
            out.append({"strategy_id": sid, "name": (row or {}).get("name")})
    return out


def broker_status(account_id: str) -> dict:
    from .. import oms
    meta = _meta(account_id)
    conn = connection(account_id)
    acc = accounts.get(account_id) if accounts.exists(account_id) else None
    remote = acc.remote if acc is not None else mirror.remote_adapter(account_id)
    lk = mirror.link(account_id)
    rs = mirror.reconcile_state(acc.store) if acc is not None else {"status": "never", "id": None, "at": None,
                                                                   "trigger": None, "reason": None}
    last = None
    if acc is not None and rs.get("id"):
        from . import reconcile
        last = reconcile.latest(account_id)
    snap = _snapshot_meta(acc)
    connected = conn["status"] == "connected"
    halted = acc.halt() if acc is not None else {"active": False, "kind": None, "reason": None, "at": None}
    working = len(oms.list_orders(account_id, states=oms.WORKING, limit=1000)) if acc is not None else 0
    reconcile_halt = halted["active"] and halted["kind"] == "reconcile"
    retired = accounts.retired_reason(account_id)
    mod = meta["module"]                     # None for a retired broker (S10-K: its adapter is gone)
    notes = ([RETIRED_NOTE] if retired else []) + (list(mod.NOTES) if mod is not None else [])
    bl_missing = baseline_missing(account_id)
    can = {"test": True, "connect": not connected or bl_missing, "disconnect": connected,
           "reconcile": connected, "resolve": connected and (reconcile_halt or rs.get("status") == "mismatch"),
           "kill": connected}
    if retired:
        can = {k: False for k in can}
        can["disconnect"] = connected            # only the record can be closed (no broker call)
    sandboxed = accounts.sandbox_block_reason(account_id)
    if sandboxed:
        can = {k: False for k in can}            # sandbox profile: nothing is sent to the broker
    out = {"id": account_id, "broker": meta["broker"], "label": meta["label"], "venue": meta["venue"],
            "state": "retired" if retired else ("blocked_sandbox" if sandboxed else "active"),
            "retired": {"reason": retired, "since": "2026-10-19", "sources": list(accounts.RETIRED_SOURCES)}
            if retired else None,
            "instruments": meta["instruments"], "paper": True, "live": False, "chip": meta["chip"],
            "currency": meta["currency"], "currency_note": meta["currency_note"],
            # binance-demo (§18.14): the confirmed starting balances; missing → blocked, "Confirm starting balances…"
            "baseline": ({"required": True, "set": acc is not None and not bl_missing, "missing": bl_missing,
                          "balances": mirror.baseline(acc.store) if acc is not None else None,
                          "message": BASELINE_MISSING if bl_missing else None}
                         if meta["broker"] == "binance_demo" else None),
            "endpoint": live_guard.paper_endpoint(meta["broker"]) if mod is not None else None,
            "sdk": _sdk_info(account_id, remote),
            "keys": _keys(account_id), "connection": conn, "link": lk.view(),
            "reconciliation": {"status": rs.get("status") or "never", "id": rs.get("id"), "at": rs.get("at"),
                               "trigger": rs.get("trigger"), "reason": rs.get("reason") if rs.get("halted") or
                               rs.get("status") == "mismatch" else (rs.get("error") if rs.get("status") ==
                                                                    "unreachable" else None),
                               "mismatches": (last or {}).get("mismatches") or []},
            "halt": halted,
            "snapshot": {"at": (snap or {}).get("at"), "account": (snap or {}).get("account"),
                         "positions_count": len((snap or {}).get("positions") or []),
                         "open_orders_count": len((snap or {}).get("open_orders") or [])},
            "kill": _kill_view(acc),
            "working_orders": working, "routes": _routes_to(account_id),
            "capabilities": getattr(remote, "capabilities", mod.CAPABILITIES if mod is not None else {}),
            "notes": notes, "terms": dict(mod.TERMS) if mod is not None else None,
            "live_guard": {"build_allows_live": live_guard.BUILD_ALLOWS_LIVE, "message": live_guard.LIVE_DISABLED},
            "can": can}
    if sandboxed:                                # additive, sandbox mode only
        out["sandbox_blocked"] = {"label": "blocked in sandbox", "reason": sandboxed}
        out["notes"] = [sandboxed] + out["notes"]
    return out


LAST_KILL_KEY = "last_kill"


def _kill_view(acc) -> dict:
    st = kill_state(acc) if acc is not None else None
    last = {}
    if acc is not None:
        try:
            last = json.loads(acc.store.meta_get(LAST_KILL_KEY) or "{}") or {}
        except ValueError:
            last = {}
    return {"pending": st is not None, "at": (st or {}).get("at") or last.get("at"),
            "not_flat": (st or {}).get("not_flat") or [],
            "untouched_orders": int(last.get("untouched_orders") or 0), "untouched_note": last.get("untouched_note")}


def _retired_row_present(account_id: str) -> bool:
    """S10-K: a retired broker is listed only while hedgefund.db still holds a row of it (account, connection record
    or a strategy routed there) — that row stays visible as RETIRED / blocked until the user cleans it up."""
    return (accounts.exists(account_id) or connection(account_id)["status"] != "never_connected"
            or account_id in latest_routes().values())


def list_status() -> dict:
    ids = [a for a in mirror.BROKER_IDS if a not in accounts.RETIRED or _retired_row_present(a)]
    return {"brokers": [broker_status(a) for a in ids], "paper_only": True, "notice": NOTICE,
            "live_trading": {"enabled": False, "message": live_guard.LIVE_DISABLED}}


def detail(account_id: str) -> dict:
    from . import reconcile
    st = broker_status(account_id)
    acc = accounts.get(account_id) if accounts.exists(account_id) else None
    snap = _snapshot_meta(acc) or {}
    events = []
    if acc is not None:
        events = [{"id": e["id"], "ts": e["ts"], "kind": e["kind"], "symbol": e["symbol"], "title": e["title"],
                   "body": e["body"]} for e in acc.store.all_events()[-50:]][::-1]
    return {"broker": st, "positions": snap.get("positions") or [], "open_orders": snap.get("open_orders") or [],
            "reconciliations": reconcile.history(account_id, 20) if acc is not None else [], "events": events}


# --- test / connect / disconnect ----------------------------------------------------------------------------

def test(account_id: str, actor: str = "user") -> dict:
    meta = _meta(account_id)
    require_active(account_id)
    rem, acc = _remote(account_id)
    remote = rem.acc.remote if acc is not None else rem.remote
    checks: List[dict] = []
    stop: Optional[str] = None
    out = {"ok": False, "endpoint": live_guard.paper_endpoint(meta["broker"]), "paper": True, "checks": checks,
           "flat": None, "cash_basis": None}

    def add(name: str, fn):
        nonlocal stop
        if stop is not None:
            checks.append({"name": name, "ok": False, "message": f"Skipped — {stop}"})
            return None
        try:
            v = fn()
            checks.append({"name": name, "ok": True, "message": None})
            return v
        except Exception as ex:      # noqa: BLE001 — each check names its reason
            msg = getattr(ex, "user_message", None) or f"{type(ex).__name__}"
            checks.append({"name": name, "ok": False, "message": msg[:500]})   # Binance code + hint fit (2026-10-23)
            stop = msg[:120]
            return None

    def sdk():
        info = _sdk_info(account_id, remote)
        if not info.get("installed"):
            raise UserFacingError(info.get("message") or f"{info.get('name')} is not installed")
        return info

    def keys():
        ks = _keys(account_id)
        bad = [k for k in ks if not k["present"]]
        if bad:
            raise UserFacingError("missing: " + ", ".join(k["label"] for k in bad) +
                                  " — enter them in Hedge Fund → Settings → Keys")
        return ks

    add("SDK installed", sdk)
    add("Keys present", keys)
    add("Paper/demo endpoint", lambda: rem.remote_call("check_endpoint", user=True))
    if meta["broker"] == "binance_demo":           # SPEC §18.14 item 8 (read-only)
        st = add("Server time", lambda: rem.remote_call("server_time", user=True))
        if st is not None:
            out["server_time"] = st
        add("Markets loaded (exchangeInfo)", lambda: _binance_markets_check(rem))
    acct = add("Account readable", lambda: rem.remote_call("account", user=True))
    if meta["broker"] == "binance_demo" and baseline_missing(account_id):
        # no baseline → the platform's positions are unknown (never assumed empty); the other reads still run
        checks.append({"name": "Positions readable", "ok": False, "message": BASELINE_MISSING})
        pos = None
    else:
        pos = add("Positions readable", lambda: rem.remote_call("positions", user=True))
    oo = add("Open orders readable", lambda: rem.remote_call("open_orders", user=True))
    out["ok"] = all(c["ok"] for c in checks)
    if acct is not None:
        out["cash_basis"] = fnum(acct.get("cash_basis"))
    if pos is not None and oo is not None:
        out["flat"] = not pos and not oo
    out["positions"], out["open_orders"] = pos or [], oo or []
    out["currency"] = meta["currency"]
    if meta["broker"] == "binance_demo":
        out["balances"] = (acct or {}).get("balances") or []
        out["platform_open_orders"] = len(oo or [])
        out["foreign_open_orders"] = int(getattr(remote, "last_foreign_open", 0) or 0) if oo is not None else None
        out["currency_note"] = meta["currency_note"]
    try:
        db.add_audit(actor, "broker_test", {"account_id": account_id, "ok": out["ok"],
                                            "failed": [c["name"] for c in checks if not c["ok"]]})
    except Exception:            # noqa: BLE001 — a test is read-only
        logger.exception("audit row broker_test failed")
    return out


def _binance_markets_check(rem) -> int:
    n = rem.remote_call("usdt_markets", user=True)
    if not n:
        raise UserFacingError("no trading BASE/USDT spot markets found on the demo")
    return n


BALANCE_TOL = 1e-8


def balances_differ(confirmed, current: List[dict]) -> Optional[str]:
    """None when the confirmed ``{ASSET: total}`` equals the fresh non-zero balances, else what differs."""
    if not isinstance(confirmed, dict):
        return "starting_balances must be an object {ASSET: total}"
    cur = {b["asset"]: float(b["total"]) for b in current}
    conf = {}
    for k, v in confirmed.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return f"starting_balances.{k} must be a number"
        if abs(float(v)) > 1e-12:
            conf[str(k).upper()] = float(v)
    diff = []
    for a in sorted(set(cur) | set(conf)):
        c, n = conf.get(a, 0.0), cur.get(a, 0.0)
        if abs(c - n) > max(BALANCE_TOL, 1e-9 * abs(n)):
            diff.append(f"{a} {c:g} confirmed vs {n:g} now")
    return "; ".join(diff)[:300] or None


BASELINE_MISSING = ("baseline missing — confirm the starting balances (Settings → Brokers → Confirm starting "
                    "balances…); never assumed empty")


def baseline_missing(account_id: str) -> bool:
    """binance-demo (SPEC §18.14): the account exists but its confirmed baseline is not stored — it is blocked until the
    user confirms the starting balances again (never assumed empty)."""
    if account_id != "binance-demo" or not accounts.exists(account_id):
        return False
    return mirror.baseline(accounts.get(account_id).store) is None


def connect(account_id: str, confirm: str, reason: str, actor: str = "user", starting_balances=None) -> dict:
    """Test → preconditions → ONE transaction (account row + settings on the first connect, the connection row, the
    binance-demo baseline, the audit row) → mirror deposit → connect reconciliation. binance-demo: also the way to
    confirm a MISSING baseline again while connected (SPEC §18.14 item 7)."""
    from ...portfolio import ledger
    from ...portfolio import settings as pf_settings
    from . import reconcile
    meta = _meta(account_id)
    require_active(account_id)
    # fix1023: no typed confirm phrase any more (``confirm`` accepted and ignored)
    binance = meta["broker"] == "binance_demo"
    connected = connection(account_id)["status"] == "connected"
    first = not accounts.exists(account_id)
    rebase = binance and not first and baseline_missing(account_id)
    if connected and not rebase:
        raise TradingError(409, "already_connected", f"{account_id} is already connected.")
    t = test(account_id, actor)
    failed = [c for c in t["checks"] if not c["ok"] and not (rebase and c["name"] == "Positions readable")]
    if failed:
        raise TradingError(409, "test_failed", "The connection test failed — see the checks.", checks=t["checks"])
    ts = db.utc_now_iso()
    baseline = None
    if binance and (first or rebase):
        # SPEC §18.14 item 7: NO flat rule (the demo is pre-funded) — no open UFOS_ orders, and the user confirms the
        # starting balances, which become the baseline (P&L and positions are the changes since then)
        if t.get("platform_open_orders"):
            raise TradingError(409, "platform_orders_open", f"The {meta['label']} holds {t['platform_open_orders']} "
                                                            "open order(s) with the platform's UFOS_ prefix — cancel "
                                                            "them at the broker first.", open_orders=t["open_orders"])
        if starting_balances is None:
            raise ValueError("starting_balances: confirm the starting balances shown by Test connection "
                             "({ASSET: total})")
        diff = balances_differ(starting_balances, t.get("balances") or [])
        if diff:
            raise TradingError(409, "balances_changed", f"The balances changed since you confirmed them ({diff}) — "
                                                        "check them again and confirm.", balances=t.get("balances"))
        baseline = {b["asset"]: float(b["total"]) for b in t.get("balances") or []}
    basis = t["cash_basis"]
    if first:
        if not binance and not t["flat"]:
            raise TradingError(409, "not_flat", f"The {meta['label']} at the broker holds positions or open orders — "
                                                "close / cancel them at the broker (or reset the paper/demo account) "
                                                "first; the platform never imports foreign positions.",
                               positions=t["positions"], open_orders=t["open_orders"])
        if basis is None or basis < MIN_OPENING_BALANCE:
            raise TradingError(409, "too_small", f"The broker account has {basis or 0:,.2f} {meta['currency']} — at "
                                                 f"least {MIN_OPENING_BALANCE:,.0f} {meta['currency']} are needed.")
    with db._connect() as conn:          # ONE transaction (review fix 2026-10-19): account, connection, baseline, audit
        if first:
            params = accounts.broker_params(float(basis))
            conn.execute("INSERT INTO br_accounts (id, broker, label, params_json, created_at, created_by) VALUES"
                         " (?, ?, ?, ?, ?, ?)", (account_id, meta["broker"], meta["label"],
                                                 json.dumps(params, sort_keys=True), ts, actor))
            pf_settings.write(account_id, pf_settings.defaults(), "system", "broker account connected (defaults)",
                              conn=conn)
        if not connected:
            conn.execute("INSERT INTO br_connections (account_id, ts, status, actor, reason, detail_json) VALUES"
                         " (?, ?, 'connected', ?, ?, ?)", (account_id, ts, actor, reason,
                                                           json.dumps({"first": first, "cash_basis": basis,
                                                                       "currency": meta["currency"],
                                                                       "baseline": baseline})))
        if baseline is not None:
            conn.execute("INSERT INTO pa_meta (account_id, key, value) VALUES (?, ?, ?) ON CONFLICT(account_id, key)"
                         " DO UPDATE SET value = excluded.value",
                         (account_id, mirror.BASELINE_KEY, json.dumps(baseline, sort_keys=True)))
        conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'broker_connect', ?)",
                     (ts, actor, json.dumps({"account_id": account_id, "first": first, "reason": reason,
                                             "opening_balance": basis if first else None,
                                             "currency": meta["currency"], "baseline": baseline,
                                             "baseline_reconfirmed": bool(rebase)})))
    accounts.reset_cache()
    acc = accounts.get(account_id)
    if first:
        with acc.broker._lock:
            with acc.store.atomic():
                acc.broker.ensure_initialized(accounts.now(), acc.params)
        ledger.attribute(acc)
    res = reconcile.run(acc, "connect", user=True)
    return {"broker": broker_status(account_id), "reconciliation": res}

def disconnect(account_id: str, reason: str, actor: str = "user") -> dict:
    from ...live import incubation
    from .. import oms
    _meta(account_id)
    why = accounts.sandbox_block_reason(account_id)
    if why:                              # sandbox profile: the record of the real connection is left alone
        raise TradingError(409, "sandbox_blocked", f"{account_id}: {why}")
    if connection(account_id)["status"] != "connected":
        raise TradingError(409, "not_connected", f"{account_id} is not connected.")
    if accounts.retired_reason(account_id):       # retired: only the record changes, the broker is not contacted
        ts = db.utc_now_iso()
        with db._connect() as conn:
            conn.execute("INSERT INTO br_connections (account_id, ts, status, actor, reason) VALUES"
                         " (?, ?, 'disconnected', ?, ?)", (account_id, ts, actor, reason))
            conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'broker_disconnect', ?)",
                         (ts, actor, json.dumps({"account_id": account_id, "reason": reason, "retired": True})))
        accounts.reset_cache()
        return {"broker": broker_status(account_id)}
    acc = accounts.get(account_id)
    if oms.list_orders(account_id, states=oms.WORKING, limit=1):
        raise TradingError(409, "working_orders", "Orders are still working on this broker — cancel them first.")
    if any(r["account_id"] == account_id for r in incubation.active_runs()):
        raise TradingError(409, "incubating", "An incubation run trades on this broker — abort it first.")
    if _routes_to(account_id):
        raise TradingError(409, "routed", "Strategies are routed to this broker — route them elsewhere first.")
    if acc.store.get_positions():
        raise TradingError(409, "not_flat", "The platform still holds positions on this broker — close them first.")
    try:
        snap = acc.adapter.snapshot(user=True)
    except BrokerUnavailable as ex:
        raise TradingError(409, "unreachable", f"The broker could not be read: {ex.user_message}") from ex
    if snap.get("positions") or snap.get("open_orders"):
        raise TradingError(409, "not_flat", "The broker account still holds positions or open orders.")
    ts = db.utc_now_iso()
    with db._connect() as conn:
        conn.execute("INSERT INTO br_connections (account_id, ts, status, actor, reason) VALUES"
                     " (?, ?, 'disconnected', ?, ?)", (account_id, ts, actor, reason))
        conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'broker_disconnect', ?)",
                     (ts, actor, json.dumps({"account_id": account_id, "reason": reason})))
    accounts.reset_cache()
    return {"broker": broker_status(account_id)}


def reconcile_now(account_id: str) -> dict:
    from . import reconcile
    _meta(account_id)
    require_active(account_id)
    if connection(account_id)["status"] != "connected":
        raise TradingError(409, "not_connected", f"{account_id} is not connected.")
    acc = accounts.get(account_id)
    res = reconcile.run(acc, "manual", user=True)
    return {"reconciliation": res, "broker": broker_status(account_id)}


def instrument(account_id: str, symbol: str) -> dict:
    from strategy_platform.core import params as fund_params
    meta = _meta(account_id)
    require_active(account_id)
    crypto = fund_params.is_crypto(symbol)
    if meta["broker"] == "alpaca_paper" and crypto:
        raise ValueError(f"crypto is traded on {mirror.CRYPTO_VENUE}, not on alpaca-paper")
    if meta["broker"] == "binance_demo" and not crypto:
        raise ValueError(f"stocks/ETFs are traded on alpaca-paper, not on {account_id}")
    rem, _acc = _remote(account_id)
    try:
        return rem.remote_call("instrument", symbol, user=True)
    except BrokerUnavailable as ex:
        raise TradingError(503, "unreachable", ex.user_message) from ex
    except UserFacingError as ex:
        raise TradingError(503, "unreachable", ex.user_message) from ex


# --- kill switch per broker (SPEC §18.6, review fix H 2026-10-18 §18.13) -----------------------------------------

KILL_CANCEL_WAIT_SECONDS = 10.0     # bound: wait until the broker reports no open orders (pending cancels release qty)
KILL_SETTLE_SECONDS = 10.0          # bound per attempt: wait for the closing orders to fill
KILL_POLL_SECONDS = 0.5
KILL_CLOSE_ATTEMPTS = 3             # rejected closes are re-sent at most this often (new ids, never two at once)
NOT_FLAT_NOTE = ("The broker still holds positions (see not_flat) — the account stays halted and the kill retries "
                 "automatically every 15 s until the broker is flat.")
UNREADABLE_NOTE = ("The broker's positions could not be read — the account is halted and the positions are closed "
                   "automatically as soon as the broker answers.")


def kill(acc, reason: str, kind: str = "kill", actor: str = "user") -> dict:
    """Halt, cancel everything (OMS + broker), wait (bounded) until the broker holds no open orders, close every
    position the BROKER reports (rejected closes retried, bounded), then VERIFY the broker is flat. Not flat /
    unreadable → ``ok: false``, ``pending: true``, per-symbol ``not_flat``; the poller completes it. Never a
    silent success."""
    require_active(acc.id)
    from .. import oms
    out = {"account_id": acc.id, "ok": True, "error": None, "cancelled_at_broker": 0, "oms_cancelled": 0,
           "orders": [], "halt": None, "note": None, "flat": None, "not_flat": [], "open_orders_left": None,
           "attempts": 0, "pending": False, "untouched_orders": 0, "untouched_note": None, "refused": []}
    errors: list = []
    with oms.lock_for(acc.id), acc.adapter.user_attempt():
        acc.set_halt(True, "kill" if kind == "kill" else "drawdown", reason[:300])
        try:
            out["oms_cancelled"] = len(oms.cancel_working(acc, f"cancelled: {reason[:100]}"))
        except Exception as ex:      # noqa: BLE001 — every step of a kill is tried
            errors.append(f"cancel OMS orders: {getattr(ex, 'user_message', ex)}")
        try:
            out["cancelled_at_broker"] = acc.adapter.cancel_all(user=True)
        except Exception as ex:      # noqa: BLE001
            errors.append(f"cancel at the broker: {getattr(ex, 'user_message', ex)}")
        # Binance: orders without the UFOS_ prefix — not the platform's (left untouched, counted)
        out["untouched_orders"] = int(getattr(acc.remote, "last_untouched", 0) or 0)
        # cancels the broker refused (binance-demo, review fix 2026-10-19): counted with the reasons, never silent
        out["refused"] = list(getattr(acc.remote, "last_refused", None) or [])
        if out["refused"]:
            errors.append(f"{len(out['refused'])} cancel(s) refused by the broker: "
                          + "; ".join(f"{r.get('client_order_id') or r.get('broker_order_id')}: {r.get('reason')}"
                                      for r in out["refused"][:5]))
        if out["untouched_orders"]:
            out["untouched_note"] = (f"{out['untouched_orders']} order(s) without the platform's UFOS_ prefix left "
                                     "untouched (not the platform's).")
        acc.store.meta_set(LAST_KILL_KEY, json.dumps({"at": accounts.now().isoformat(timespec="seconds"),
                                                       "untouched_orders": out["untouched_orders"],
                                                       "untouched_note": out["untouched_note"]}))
        res = _flatten_verified(acc, reason, kind, actor, errors, cancel_wait=KILL_CANCEL_WAIT_SECONDS,
                                settle=KILL_SETTLE_SECONDS, attempts=KILL_CLOSE_ATTEMPTS)
        out.update(res)
        pending = res["flat"] is not True
        out["pending"] = pending
        if pending:
            acc.store.meta_set(mirror.KILL_PENDING_KEY, json.dumps({
                "reason": reason[:300], "kind": kind, "actor": actor, "at": accounts.now().isoformat(timespec="seconds"),
                "not_flat": res["not_flat"]}, default=str))
            out["note"] = UNREADABLE_NOTE if res["flat"] is None else NOT_FLAT_NOTE
        else:
            acc.store.meta_set(mirror.KILL_PENDING_KEY, "")
        out["halt"] = acc.halt()
    if errors or out["pending"]:
        out["ok"] = False
        out["error"] = ("; ".join(errors) or "the broker is not flat yet")[:500]
    try:
        acc.store.add_event("halt", f"Kill switch: {acc.id} halted", None,
                            f"{reason}. Open orders cancelled at the broker; {len(out['orders'])} closing order(s); "
                            + ("the broker is flat." if out["flat"] else "the broker is NOT flat yet.")
                            + (f" {out['note']}" if out["note"] else "") + " Paper/demo trading · simulated money.")
        db.add_audit(actor if actor in db.AUDIT_ACTORS else "user", "broker_kill",
                     {"account_id": acc.id, "reason": reason, "kind": kind, "orders": len(out["orders"]),
                      "flat": out["flat"], "pending": out["pending"], "not_flat": out["not_flat"],
                      "error": out["error"]})
    except Exception:            # noqa: BLE001
        logger.exception("kill event / audit failed")
    return out


def _broker_positions(acc, errors: list) -> Optional[list]:
    try:
        return acc.adapter.remote_call("positions", user=True)
    except Exception as ex:      # noqa: BLE001
        errors.append(f"read positions: {getattr(ex, 'user_message', ex)}")
        return None


def _closing_working(acc) -> Dict[str, dict]:
    from .. import oms
    return {o["symbol"]: o for o in oms.list_orders(acc.id, states=oms.WORKING, limit=1000)
            if o["origin"] in ("kill", "drawdown")}


def _wait_no_open_orders(acc, seconds: float, errors: list) -> Optional[int]:
    """Bounded wait until the broker reports no open orders except the platform's own working closing orders
    (cancels are asynchronous: until they are done the broker still holds the quantity). Returns how many are
    still open (None: unreadable)."""
    from .. import oms
    polls = max(1, int(seconds / KILL_POLL_SECONDS) + 1) if seconds > 0 else 1
    left = None
    for k in range(polls):
        try:
            oo = acc.adapter.remote_call("open_orders", user=True)
        except Exception as ex:  # noqa: BLE001
            errors.append(f"read open orders: {getattr(ex, 'user_message', ex)}")
            return None
        own = {o.get("broker_order_id") for o in _closing_working(acc).values()}
        left = sum(1 for o in oo if str(o.get("broker_order_id")) not in {str(x) for x in own if x})
        if left == 0:
            break
        if k + 1 < polls:
            common.sleep(KILL_POLL_SECONDS)
    try:
        oms.sync(acc)
    except Exception as ex:      # noqa: BLE001 — the closes below still run
        errors.append(f"sync: {getattr(ex, 'user_message', ex)}")
    if left:
        errors.append(f"{left} order(s) still open at the broker after {seconds:g} s (cancel pending)")
    return left


def _settle(acc, seconds: float, res: Optional[dict] = None) -> bool:
    """Bounded wait for the closing orders: True when the broker is flat."""
    from .. import oms
    polls = max(1, int(seconds / KILL_POLL_SECONDS)) if seconds > 0 else 1
    for k in range(polls):
        try:
            oms.sync(acc)
            pos = _closable(acc.adapter.remote_call("positions", user=True), res)
        except Exception:        # noqa: BLE001 — reported by the final verification
            return False
        if not pos:
            return True
        if not _closing_working(acc):
            return False         # every close is done (filled / rejected) but positions remain -> next attempt
        if k + 1 < polls:
            common.sleep(KILL_POLL_SECONDS)
    return False


def _closable(positions: Optional[list], res: Optional[dict]) -> Optional[list]:
    """The positions the kill closes. A position the broker adapter marks ``kill_skip`` (binance-demo: dust below
    LOT_SIZE / NOTIONAL, below the starting balance, an asset the platform does not trade — SPEC §18.14 item 9) is
    not sold, counts as flat for the kill and is listed in ``skipped`` with the reason."""
    if positions is None:
        return None
    out = []
    for p in positions:
        why = p.get("kill_skip")
        if why:
            if res is not None and not any(x["symbol"] == p["symbol"] for x in res["skipped"]):
                res["skipped"].append({"symbol": p["symbol"], "qty": float(p["qty"]), "reason": why})
            continue
        out.append(p)
    return out


def _flatten_verified(acc, reason: str, kind: str, actor: str, errors: list, *, cancel_wait: float,
                      settle: float, attempts: int) -> dict:
    from .. import oms
    res = {"orders": [], "flat": None, "not_flat": [], "open_orders_left": None, "attempts": 0, "skipped": []}
    res["open_orders_left"] = _wait_no_open_orders(acc, cancel_wait, errors)
    origin = "kill" if kind == "kill" else "drawdown"
    last: Dict[str, dict] = {}
    for k in range(max(1, attempts)):
        positions = _closable(_broker_positions(acc, errors), res)
        if positions is None:
            return res
        if not positions:
            res["flat"] = True
            return res
        res["attempts"] = k + 1
        working = _closing_working(acc)
        for p in positions:
            sym, q = p["symbol"], float(p["qty"])
            if sym.startswith("UNMAPPED:"):
                if k == 0:
                    res["orders"].append({"symbol": sym, "state": "error",
                                          "reject_reason": "a position the platform cannot map — close it at the "
                                                           "broker"})
                continue
            if sym in working:           # a closing order already works at the broker — never a second one
                last[sym] = working[sym]
                continue
            try:
                o, _ = oms.place(acc, {"symbol": sym, "side": "sell" if q > 0 else "cover", "qty": abs(q),
                                       "origin": origin, "reason": reason[:200], "allow_reduce": True,
                                       "client_order_id": oms.new_client_id("kill" if kind == "kill" else "ddk")},
                                 actor=actor)
                res["orders"].append(o)
                last[sym] = o
            except Exception as ex:  # noqa: BLE001 — one symbol never stops the others
                logger.exception("%s kill: closing %s failed", acc.id, sym)
                err = {"symbol": sym, "state": "error", "reject_reason": str(getattr(ex, "user_message", ex))[:200]}
                res["orders"].append(err)
                last[sym] = err
        if _settle(acc, settle, res):
            res["flat"] = True
            return res
        if _closing_working(acc):
            break                        # queued at the broker (e.g. market closed): wait, never duplicate
        if k + 1 < attempts:
            common.sleep(KILL_POLL_SECONDS)
    positions = _closable(_broker_positions(acc, errors), res)
    if positions is None:
        return res
    if not positions:
        res["flat"] = True
        return res
    res["flat"] = False
    for p in positions:
        o = oms.get(last[p["symbol"]]["client_order_id"]) if p["symbol"] in last and \
            last[p["symbol"]].get("client_order_id") else last.get(p["symbol"])
        res["not_flat"].append({"symbol": p["symbol"], "broker_qty": float(p["qty"]),
                                "close_order": (o or {}).get("client_order_id"),
                                "close_state": (o or {}).get("state"),
                                "reject_reason": (o or {}).get("reject_reason")})
    return res


def complete_pending_kills() -> int:
    """Poller: a kill that is not complete (broker unreadable or not flat) is continued — one bounded step per
    tick, closing orders only where none works yet; the pending flag clears only when the broker is flat."""
    n = 0
    for acc in _connected_accounts():
        v = acc.store.meta_get(mirror.KILL_PENDING_KEY)
        if not v:
            continue
        try:
            d = json.loads(v)
        except ValueError:
            continue
        if acc.link.blocked():
            continue
        from .. import oms
        errors: list = []
        with oms.lock_for(acc.id):
            res = _flatten_verified(acc, d.get("reason") or "Kill switch", d.get("kind") or "kill",
                                    d.get("actor") or "user", errors, cancel_wait=0, settle=0, attempts=1)
            if res["flat"] is True:
                acc.store.meta_set(mirror.KILL_PENDING_KEY, "")
                acc.store.add_event("halt", f"Kill switch completed on {acc.id}", None,
                                    f"The broker is flat now ({len(res['orders'])} further closing order(s)).")
                n += 1
            else:
                d["not_flat"] = res["not_flat"]
                acc.store.meta_set(mirror.KILL_PENDING_KEY, json.dumps(d, default=str))
    return n


def kill_pending(acc) -> bool:
    return bool(acc.store.meta_get(mirror.KILL_PENDING_KEY))


def kill_state(acc) -> Optional[dict]:
    """The pending kill (``{reason, kind, actor, at, not_flat}``) or None."""
    v = acc.store.meta_get(mirror.KILL_PENDING_KEY)
    try:
        return json.loads(v) if v else None
    except ValueError:
        return None


# --- routing (SPEC §18.2) -------------------------------------------------------------------------------------

def latest_routes() -> Dict[str, str]:
    with db._connect() as conn:
        rows = conn.execute("SELECT strategy_id, account_id FROM br_routes ORDER BY id").fetchall()
    out: Dict[str, str] = {}
    for r in rows:
        out[r["strategy_id"]] = r["account_id"]
    return out


def route_row(strategy_id: str) -> Optional[dict]:
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM br_routes WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
                         (strategy_id,)).fetchone()
    return dict(r) if r else None


def route_for(strategy_id: str) -> str:
    r = route_row(strategy_id)
    return r["account_id"] if r else accounts.MAIN


def universe_class(strategy_id: str) -> Optional[str]:
    from ...core.instruments import instrument
    from ...lifecycle import registry
    from ...strategy import schema, universe
    try:
        row = registry.get(strategy_id)
        spec = registry.get_version(strategy_id, int(row["current_version"]))["spec"]
        full = schema.with_defaults(spec)
        classes = {instrument(s).asset_class for s in universe.resolve(full["universe"])}
    except Exception:            # noqa: BLE001 — shown as "unknown"
        return None
    if classes == {"crypto"}:
        return "crypto"
    if classes and "crypto" not in classes:
        return "equity"
    return "mixed" if classes else None


def strategy_mode(strategy_id: str) -> Optional[str]:
    """The current version's ``portfolio.mode`` (long_only | short_only | long_short), None when unreadable."""
    from ...lifecycle import registry
    from ...strategy import schema
    try:
        row = registry.get(strategy_id)
        spec = registry.get_version(strategy_id, int(row["current_version"]))["spec"]
        return schema.with_defaults(spec)["portfolio"]["mode"]
    except Exception:            # noqa: BLE001 — shown as "unknown"
        return None


def long_only_reason(strategy_id: str, account_id: str) -> Optional[str]:
    """binance-demo is spot, long only (SPEC §18.14 item 5): a strategy that needs shorts cannot be routed there."""
    if account_id != "binance-demo":
        return None
    mode = strategy_mode(strategy_id)
    if mode == "long_only":
        return None
    return (f"{account_id} is spot, long only — this strategy's portfolio mode is {mode or 'unknown'} (its signals "
            "need shorts); only long_only strategies can run there")


def allowed_accounts(cls: Optional[str]) -> List[str]:
    """Route targets by universe — a RETIRED broker is never offered (crypto → paper-main | binance-demo since
    2026-10-19; binance-demo additionally needs a long_only strategy, ``long_only_reason``)."""
    if cls == "equity":
        out = [accounts.MAIN, "alpaca-paper"]
    elif cls == "crypto":
        out = [accounts.MAIN, "binance-demo"]
    else:
        out = [accounts.MAIN]
    return [a for a in out if not accounts.no_call_reason(a)]      # retired / blocked in sandbox


def _route_entry(strategy_id: str, row: Optional[dict] = None) -> dict:
    from ...live import incubation
    row = row or db.strategy_get(strategy_id) or {}
    r = route_row(strategy_id)
    cls = universe_class(strategy_id)
    run = incubation.running_for(strategy_id)
    aid = r["account_id"] if r else accounts.MAIN
    why = accounts.no_call_reason(aid)                # retired, or blocked in sandbox
    blocked_reason = f"{aid}: {why} — re-route the strategy" if why else None
    refused = []
    allowed = []
    for a in allowed_accounts(cls):
        lw = long_only_reason(strategy_id, a)
        if lw:
            refused.append({"account_id": a, "code": "long_only", "reason": lw})
        else:
            allowed.append(a)
    if blocked_reason is None:
        lw = long_only_reason(strategy_id, aid)     # routed earlier, the strategy needs shorts now: shown, not moved
        if lw:
            blocked_reason = f"{lw} — re-route the strategy"
    return {"strategy_id": strategy_id, "name": row.get("name"), "state": row.get("state"), "universe": cls,
            "account_id": aid, "since": r["ts"] if r else None,
            "by": r["actor"] if r else None, "reason": r["reason"] if r else None,
            "allowed_accounts": allowed, "refused_accounts": refused,
            "running_incubation": run["id"] if run else None,
            # a route to a retired broker is NOT moved silently: shown blocked, the user re-routes (§18.13)
            "blocked": blocked_reason is not None, "blocked_reason": blocked_reason}


def routing_list() -> dict:
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM strategies WHERE state IN ('validated', 'incubation',"
                                              " 'live') ORDER BY id").fetchall()]
    ids = {r["id"] for r in rows}
    routed = [sid for sid in latest_routes() if sid not in ids]
    out = [_route_entry(r["id"], r) for r in rows]
    for sid in routed:
        out.append(_route_entry(sid))
    return {"default_account_id": accounts.MAIN, "routes": out}


def route_phrase(account_id: str) -> str:
    return f"ROUTE TO {account_id.upper()}"


def set_route(strategy_id: str, account_id: str, confirm: str, reason: str, actor: str = "user") -> dict:
    from ...live import incubation
    from ...portfolio import ledger
    if account_id not in accounts.ACCOUNT_IDS or account_id == accounts.COUNCIL:
        raise ValueError("account_id must be paper-main, alpaca-paper or binance-demo")
    # fix1023: no typed confirm phrase any more (``confirm`` accepted and ignored)
    row = db.strategy_get(strategy_id)
    if row is None:
        raise TradingError(404, "not_found", "No such strategy.")
    require_active(account_id)
    cls = universe_class(strategy_id)
    if account_id not in allowed_accounts(cls):
        raise TradingError(409, "wrong_venue", f"A {cls or 'unknown'} universe cannot run on {account_id} (stocks/ETFs "
                                               "→ alpaca-paper, crypto → binance-demo, mixed → paper-main only).")
    why_long = long_only_reason(strategy_id, account_id)
    if why_long:
        raise TradingError(409, "long_only", why_long)
    if mirror.is_broker_id(account_id):
        if connection(account_id)["status"] != "connected":
            raise TradingError(409, "not_connected", f"{account_id} is not connected — connect it first.")
        acc = accounts.get(account_id)
        if acc.halt()["active"]:
            raise TradingError(409, "halted", f"{account_id} is halted — resolve / resume it first.")
        if mirror.reconcile_state(acc.store).get("status") != "ok":
            raise TradingError(409, "not_reconciled", f"{account_id}'s last reconciliation is not ok — reconcile it "
                                                      "first.")
    elif not accounts.exists(account_id):
        raise TradingError(409, "not_connected", accounts.NOT_OPEN)
    if any(r["strategy_id"] == strategy_id for r in incubation.active_runs()):
        raise TradingError(409, "incubating", "The strategy has an active incubation run — its account is fixed; abort "
                                              "the run first (the next run starts on the new route).")
    for r in accounts.rows(include_disconnected=True):
        if ledger.holdings(r["id"]).get(strategy_id):
            raise TradingError(409, "not_flat", f"The strategy still holds positions in {r['id']} — close them first.")
    ts = db.utc_now_iso()
    prev = route_for(strategy_id)
    with db._connect() as conn:
        conn.execute("INSERT INTO br_routes (strategy_id, account_id, ts, actor, reason) VALUES (?, ?, ?, ?, ?)",
                     (strategy_id, account_id, ts, actor, reason))
        conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'broker_route', ?)",
                     (ts, actor, json.dumps({"strategy_id": strategy_id, "from": prev, "to": account_id,
                                             "reason": reason})))
    return {"route": _route_entry(strategy_id)}


# --- poller + start-up (SPEC §18.6) ----------------------------------------------------------------------------

def _connected_accounts() -> list:
    out = []
    try:
        rows = accounts.rows()
    except Exception:            # noqa: BLE001 — hedgefund.db unavailable: nothing to poll
        return out
    for r in rows:
        if mirror.is_broker_id(r["id"]):
            try:
                out.append(accounts.get(r["id"]))
            except Exception:    # noqa: BLE001
                logger.exception("broker account %s unavailable", r["id"])
    return out


def poll_once(now: Optional[datetime] = None) -> dict:
    """Fills of working orders, fresh reconciliations (≥ 5 min old), pending kills — per connected broker."""
    from ...portfolio import ledger
    from .. import oms
    from . import reconcile
    now = now or accounts.now()
    out = {"synced": 0, "reconciled": 0, "errors": {}}
    for acc in _connected_accounts():
        if acc.link.blocked():
            continue
        try:
            if oms.list_orders(acc.id, states=oms.WORKING, limit=1):
                oms.sync(acc, now)
                ledger.attribute(acc)
                out["synced"] += 1
            rs = mirror.reconcile_state(acc.store)
            at = rs.get("at")
            stale = True
            if at:
                try:
                    stale = now - datetime.fromisoformat(at) >= RECONCILE_EVERY
                except ValueError:
                    stale = True
            if stale:
                reconcile.run(acc, "poll", now)
                out["reconciled"] += 1
        except Exception as ex:  # noqa: BLE001 — the poller never stops
            out["errors"][acc.id] = (getattr(ex, "user_message", None) or type(ex).__name__)[:200]
    try:
        complete_pending_kills()
    except Exception:            # noqa: BLE001
        logger.exception("pending kills")
    return out


def startup() -> None:
    """Start-up reconciliation of every connected broker account (before the runtime trades)."""
    from . import reconcile
    for acc in _connected_accounts():
        try:
            reconcile.run(acc, "start")
        except Exception:        # noqa: BLE001
            logger.exception("start-up reconciliation of %s failed", acc.id)


async def run_forever(stop_event: Optional[asyncio.Event] = None) -> None:
    await asyncio.to_thread(startup)
    while stop_event is None or not stop_event.is_set():
        try:
            await asyncio.sleep(POLL_SECONDS)
            await asyncio.to_thread(poll_once)
        except asyncio.CancelledError:
            raise
        except Exception:        # noqa: BLE001
            logger.exception("broker poller failed")
