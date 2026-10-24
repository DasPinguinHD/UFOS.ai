"""Migration of the old paper account (SPEC §17.9) — the HARD SWITCH of the
coexistence rule (§17.1): the old engine's account (newsroom.db) is copied 1:1 into the
platform account ``paper-council`` (source "Council of AIs"), after which the old engine
never trades it again (advisory only).

Order (crash-safe):
1. blockers checked (old autopilot on, a cycle running, queued orders, unreadable old
   store, ``paper-council`` existing, an earlier migration);
2. FREEZE: newsroom.db meta ``fund.migrated_to_platform`` = migrating (under the old
   engine's trade lock — no old order can be placed in between);
3. the old account is read directly from newsroom.db (read-only connection, one read
   transaction; a read error raises — the fund.store "empty on error" guard is NOT used);
4. ONE hedgefund.db transaction: ``pf_accounts`` row, orders / cash ledger / positions /
   equity curve / broker meta copied (ids of the old rows kept in ``legacy_id``), the
   sub-ledger of ``council-of-ais`` (``migration`` entries: the cash and each position at
   its average price), settings (drawdown kill = the old ``drawdown_stop_pct``, daily loss
   2 %), cursors (no re-attribution / re-import of copied rows), ``pf_migrations`` row,
   audit;
5. newsroom flag = migrated.
``recover()`` at startup: flag "migrating" + a pf_migrations row → step 5; without → the
flag is cleared (nothing moved). Tests run this only on temp copies.

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER TRADING ONLY.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from typing import List, Optional

from ..storage import db
from . import accounts
from .errors import TradingError

logger = logging.getLogger(__name__)

CONFIRM = "MIGRATE"
NOTE = ("The old engine keeps analysing (agents, AI judge, recommendations) but no longer trades this account; the "
        "positions are held by the source \"Council of AIs\" in the platform account paper-council until the Council "
        "becomes a platform strategy (stage 10). The kill switch and the dead-man switch cover both.")
BROKER_META = ("peak_equity", "reset_at", "starting_capital", "last_accrual_date", "last_dividend_date",
               "last_funding_at")


def _legacy_path() -> Optional[str]:
    from newsroom import db as newsroom_db
    return newsroom_db.db_path()


def _ro(path: str) -> sqlite3.Connection:
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def read_legacy(path: Optional[str] = None) -> dict:
    """The old account, read in ONE read transaction (raises TradingError 503 when unreadable)."""
    path = path or _legacy_path()
    if not path or not os.path.exists(path):
        raise TradingError(503, "legacy_unreadable", "The old paper account's database (newsroom.db) is not open.")
    try:
        conn = _ro(path)
        try:
            conn.execute("BEGIN")
            orders = [dict(r) for r in conn.execute("SELECT * FROM orders ORDER BY id").fetchall()]
            ledger = [dict(r) for r in conn.execute("SELECT * FROM fund_cash_ledger ORDER BY id").fetchall()]
            positions = [dict(r) for r in conn.execute("SELECT * FROM positions WHERE qty != 0 ORDER BY symbol").fetchall()]
            equity = [dict(r) for r in conn.execute("SELECT * FROM equity_curve WHERE mode = 'paper' ORDER BY ts").fetchall()]
            meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'fund.%'")}
            prow = conn.execute("SELECT version, params_json FROM fund_params ORDER BY version DESC LIMIT 1").fetchone()
            conn.execute("COMMIT")
        finally:
            conn.close()
    except sqlite3.Error as ex:
        raise TradingError(503, "legacy_unreadable", f"The old paper account could not be read ({ex}).") from ex
    from strategy_platform.core import params as fund_params
    stored = json.loads(prow["params_json"]) if prow else None
    params = fund_params.merge(stored, None) if stored else fund_params.defaults()

    def jl(v):
        try:
            return json.loads(v) if v else None
        except ValueError:
            return None
    return {"path": path, "orders": orders, "ledger": ledger, "positions": positions, "equity": equity,
            "params": params, "params_version": prow["version"] if prow else 0,
            "meta": {k: meta.get(f"fund.{k}") for k in BROKER_META},
            "autopilot": meta.get("fund.autopilot") == "1", "halted": jl(meta.get("fund.halted")) or {"active": False},
            "migration": jl(meta.get("fund.migrated_to_platform"))}


def _migration_row() -> Optional[dict]:
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM pf_migrations ORDER BY created_at DESC LIMIT 1").fetchone()
    if r is None:
        return None
    d = dict(r)
    d["summary"] = json.loads(d.pop("summary_json"))
    return d


def legacy_state(engine=None) -> dict:
    """{status: active|migrating|migrated, owner, migrated_at, migration_id}."""
    st = None
    from fund import store as fund_store
    try:
        st = fund_store.get_migration_state()
    except Exception:            # noqa: BLE001
        st = None
    if st is None:
        try:
            m = _migration_row()
        except Exception:        # noqa: BLE001
            m = None
        if m is not None:
            st = {"status": "migrated", "migration_id": m["id"], "at": m["created_at"]}
    if st is None:
        return {"status": "active", "owner": "old engine", "migrated_at": None, "migration_id": None}
    return {"status": st["status"], "owner": "platform", "migrated_at": st.get("at"),
            "migration_id": st.get("migration_id")}


def platform_migrated() -> Optional[dict]:
    """For the old engine (FundEngine.platform_migrated_fn): the platform's record of a migration."""
    m = _migration_row()
    return None if m is None else {"status": "migrated", "migration_id": m["id"], "at": m["created_at"]}


def _blockers(legacy: Optional[dict], engine=None) -> List[dict]:
    out = []
    if legacy is not None:
        if legacy["autopilot"]:
            out.append({"code": "autopilot_on", "message": "Switch the old autopilot off first."})
        pend = [o for o in legacy["orders"] if o["status"] == "pending"]
        if pend:
            out.append({"code": "pending_orders", "message": f"{len(pend)} queued order(s) in the old account — wait "
                                                             "until they are filled or cancel them first."})
        if (legacy.get("migration") or {}).get("status") in ("migrating", "migrated"):
            out.append({"code": "already_migrated", "message": "The old paper account was already migrated."})
    if engine is not None and getattr(engine, "cycle_in_progress", False):
        out.append({"code": "cycle_running", "message": "An old-engine cycle is running — try again in a minute."})
    if accounts.exists(accounts.COUNCIL):
        out.append({"code": "account_exists", "message": "The platform account paper-council already exists."})
    if _migration_row() is not None:
        out.append({"code": "already_migrated", "message": "A migration was already carried out."})
    seen, uniq = set(), []
    for b in out:
        if b["code"] not in seen:
            seen.add(b["code"])
            uniq.append(b)
    return uniq


def _settings_for(legacy: dict) -> dict:
    from ..portfolio import settings as pf_settings
    s = pf_settings.defaults()
    dd = float(legacy["params"].get("drawdown_stop_pct") or s["drawdown_kill_pct"])
    lo, hi = pf_settings.SPEC["drawdown_kill_pct"][1], pf_settings.SPEC["drawdown_kill_pct"][2]
    s["drawdown_kill_pct"] = min(hi, max(lo, dd))
    return s


def _cash(legacy: dict) -> float:
    import math
    return float(math.fsum(float(r["amount"]) for r in legacy["ledger"]))


def dry_run(actor: str = "user", engine=None, path: Optional[str] = None) -> dict:
    blockers = []
    legacy = None
    try:
        legacy = read_legacy(path)
    except TradingError as ex:
        blockers.append({"code": ex.code, "message": ex.user_message})
    blockers += _blockers(legacy, engine)
    out = {"ok": not blockers, "blockers": blockers, "target_account_id": accounts.COUNCIL,
           "source_id": accounts.COUNCIL_SOURCE, "note": NOTE, "legacy": None, "settings": None, "broker_params": None}
    if legacy is not None:
        st = _settings_for(legacy)
        out["legacy"] = {"orders": len(legacy["orders"]), "ledger_rows": len(legacy["ledger"]),
                         "equity_points": len(legacy["equity"]), "cash": _cash(legacy),
                         "starting_capital": _f(legacy["meta"]["starting_capital"]),
                         "peak_equity": _f(legacy["meta"]["peak_equity"]), "reset_at": legacy["meta"]["reset_at"],
                         "halted": bool((legacy["halted"] or {}).get("active")), "autopilot": legacy["autopilot"],
                         "pending_orders": sum(1 for o in legacy["orders"] if o["status"] == "pending"),
                         "positions": [{"symbol": p["symbol"], "qty": p["qty"], "avg_price": p["avg_price"]}
                                       for p in legacy["positions"]]}
        out["settings"] = {"drawdown_kill_pct": st["drawdown_kill_pct"], "daily_loss_pct": st["daily_loss_pct"]}
        out["broker_params"] = legacy["params"]
    try:
        db.add_audit(actor, "migration_dry_run", {"ok": out["ok"], "blockers": [b["code"] for b in blockers]})
    except Exception:            # noqa: BLE001
        logger.exception("audit row migration_dry_run failed")
    return out


def _f(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except ValueError:
        return None


_run_lock = threading.Lock()      # review fix M2 (2026-10-18): one migration request at a time


def _done_blockers() -> List[dict]:
    """Blockers that mean "already migrated" — re-checked AFTER the freeze (M2)."""
    out = []
    if accounts.exists(accounts.COUNCIL):
        out.append({"code": "account_exists", "message": "The platform account paper-council already exists."})
    if _migration_row() is not None:
        out.append({"code": "already_migrated", "message": "A migration was already carried out."})
    return out


def _blocked(blockers: List[dict]) -> TradingError:
    return TradingError(409, "migration_blocked", "The migration is blocked: " + "; ".join(b["message"] for b in
                                                                                         blockers), blockers=blockers)


def run(confirm, reason: str, actor: str = "user", engine=None, path: Optional[str] = None) -> dict:
    """The hard switch (see the module docstring). Review fix M2: serialised by a module lock (a concurrent second
    request → 409 ``migration_blocked`` / ``migration_running``), the "already migrated" blockers are checked again
    after the freeze, the freeze is never undone once a migration record exists, and a failed copy is a 503
    ``migration_failed`` (nothing moved, the old account active again) instead of a generic error."""
    # fix1023: no typed "MIGRATE" any more (``confirm`` accepted and ignored; the dialog shows the summary)
    if not _run_lock.acquire(blocking=False):
        raise _blocked([{"code": "migration_running", "message": "A migration is already running — wait for it."}])
    try:
        return _run_locked(reason, actor, engine, path)
    finally:
        _run_lock.release()


def _run_locked(reason: str, actor: str, engine, path: Optional[str]) -> dict:
    from fund import store as fund_store
    legacy = read_legacy(path)
    blockers = _blockers(legacy, engine)
    if blockers:
        raise _blocked(blockers)
    mid = f"mig-{uuid.uuid4().hex[:12]}"
    at = db.utc_now_iso()
    lock = getattr(engine, "_trade_lock", None)
    if lock is not None:
        lock.acquire()
    try:
        if not fund_store.set_migration_state({"status": "migrating", "migration_id": mid, "at": at}):
            raise TradingError(503, "legacy_unwritable", "The old paper account could not be frozen (newsroom.db not "
                                                         "writable) — nothing was migrated.")
        if engine is not None:
            engine._migrated_seen = {"status": "migrating", "migration_id": mid, "at": at}
        try:
            if engine is not None and getattr(engine, "cycle_in_progress", False):
                # a cycle that started before the freeze may still book accruals: try again (every cycle that
                # starts after the freeze skips the trading steps)
                raise TradingError(409, "migration_blocked", "An old-engine cycle is running — try again in a minute.",
                                   blockers=[{"code": "cycle_running", "message": "an old-engine cycle is running"}])
            done = _done_blockers()             # again, AFTER the freeze (M2)
            if done:
                raise _blocked(done)
            legacy = read_legacy(path)          # again, AFTER the freeze
            if any(o["status"] == "pending" for o in legacy["orders"]):
                raise TradingError(409, "migration_blocked", "Queued orders appeared in the old account.",
                                   blockers=[{"code": "pending_orders", "message": "queued orders in the old account"}])
            try:
                summary = _copy(legacy, mid, reason, actor)
            except TradingError:
                raise
            except Exception as ex:             # noqa: BLE001 — mapped below, never a generic error
                done = _done_blockers()
                if done:
                    raise _blocked(done) from ex
                logger.exception("migration %s: the copy failed", mid)
                raise TradingError(503, "migration_failed", "The migration failed while copying "
                                   f"({getattr(ex, 'user_message', None) or type(ex).__name__}) — nothing was moved; "
                                   "the old paper account is active again.") from ex
        except Exception:
            m = None
            try:
                m = _migration_row()
            except Exception:                   # noqa: BLE001 — unreadable: keep the freeze (safe side)
                logger.exception("migration %s: pf_migrations unreadable — the old account stays frozen", mid)
                m = {"id": None, "created_at": at}
            if m is None:
                fund_store.set_migration_state(None)
                if engine is not None:
                    engine._migrated_seen = None
            elif m.get("id"):
                # a migration record exists (another request won): the account IS migrated — never un-freeze it
                st = {"status": "migrated", "migration_id": m["id"], "at": m["created_at"]}
                fund_store.set_migration_state(st)
                if engine is not None:
                    engine._migrated_seen = st
            raise
    finally:
        if lock is not None:
            lock.release()
    if not fund_store.set_migration_state({"status": "migrated", "migration_id": mid, "at": at}):
        logger.error("migration %s: the newsroom flag could not be set to migrated (recovered at the next start)", mid)
    if engine is not None:
        engine._migrated_seen = {"status": "migrated", "migration_id": mid, "at": at}
    accounts.reset_cache()
    return {"migration": {"id": mid, "created_at": at, "account_id": accounts.COUNCIL, "summary": summary},
            "account": accounts.get(accounts.COUNCIL)}


def _copy(legacy: dict, mid: str, reason: str, actor: str) -> dict:
    from ..portfolio import ledger as sl
    from ..portfolio import settings as pf_settings
    from .paper_store import PlatformPaperStore
    params = dict(legacy["params"])
    if legacy["meta"]["starting_capital"] is not None:
        params["starting_capital"] = float(legacy["meta"]["starting_capital"])
    cash = _cash(legacy)
    summary = {"orders": len(legacy["orders"]), "ledger_rows": len(legacy["ledger"]),
               "positions": [{"symbol": p["symbol"], "qty": p["qty"], "avg_price": p["avg_price"]}
                             for p in legacy["positions"]],
               "equity_points": len(legacy["equity"]), "cash": cash, "params_version": legacy["params_version"],
               "legacy_path": os.path.basename(legacy["path"]), "reason": reason[:500],
               "halted": bool((legacy["halted"] or {}).get("active"))}
    with db._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        accounts.insert_account(conn, accounts.COUNCIL, "migrated", params, actor, migration_id=mid)
        store = PlatformPaperStore(accounts.COUNCIL, params)
        store._tl.conn = conn
        try:
            max_order = 0
            for o in legacy["orders"]:
                prov = json.loads(o["provenance_json"]) if o.get("provenance_json") else None
                oid = store.insert_order(o["ts"], o["symbol"], o["side"], o["qty"], o["status"], o.get("origin") or "manual",
                                         run_id=o.get("run_id"), recommendation_id=o.get("recommendation_id"),
                                         rationale=o.get("rationale"), notional=o.get("notional"),
                                         fill_price=o.get("fill_price"), fees=o.get("fees"), filled_at=o.get("filled_at"),
                                         reject_reason=o.get("reject_reason"), provenance=prov, legacy_id=o["id"])
                max_order = max(max_order, oid)
            max_ledger = 0
            for r in legacy["ledger"]:
                lid = store.add_cash_entry(r["ts"], r["kind"], r["amount"], symbol=r.get("symbol"), note=r.get("note"),
                                           legacy_id=r["id"])
                max_ledger = max(max_ledger, lid)
            for p in legacy["positions"]:
                store.upsert_position(p["symbol"], p["qty"], p["avg_price"], updated_at=p.get("updated_at"),
                                      opened_at=p.get("opened_at"), realized_pnl=p.get("realized_pnl") or 0.0,
                                      borrow_rate_pct=p.get("borrow_rate_pct"), last_price=p.get("last_price"))
            for e in legacy["equity"]:
                store.add_equity_point(e["ts"], e["equity"], e["cash"] if e.get("cash") is not None else 0.0,
                                       e.get("long_mv") or 0.0, e.get("short_mv") or 0.0, e.get("benchmark"),
                                       e.get("drawdown_pct"))
            for k in BROKER_META:
                v = legacy["meta"][k]
                if v is not None:
                    store.meta_set(k, v)
            store.meta_set(sl.CURSOR_KEY, str(max_ledger))
            from .oms import IMPORT_CURSOR
            store.meta_set(IMPORT_CURSOR, str(max_order))
            if summary["halted"]:
                store.meta_set("halt", json.dumps({"active": True, "kind": "kill", "reason": "migrated while the old "
                                                   "engine was halted", "at": db.utc_now_iso()}))
        finally:
            store._tl.conn = None
        rows = [{"source_id": accounts.COUNCIL_SOURCE, "kind": "migration", "amount": cash, "ref": mid,
                 "note": "cash of the old paper account"}]
        for p in legacy["positions"]:
            rows.append({"source_id": accounts.COUNCIL_SOURCE, "kind": "migration", "symbol": p["symbol"],
                         "qty": float(p["qty"]), "price": float(p["avg_price"] or 0.0), "amount": 0.0, "ref": mid,
                         "note": "position of the old paper account"})
        sl.insert_entries(conn, accounts.COUNCIL, rows)
        pf_settings.write(accounts.COUNCIL, _settings_for(legacy), "system",
                          "migrated: drawdown kill = the old drawdown stop", conn=conn)
        conn.execute("INSERT INTO pf_migrations (id, created_at, account_id, legacy_path, summary_json) VALUES"
                     " (?, ?, ?, ?, ?)", (mid, db.utc_now_iso(), accounts.COUNCIL, os.path.basename(legacy["path"]),
                                          json.dumps(summary, default=str)))
        conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'migration', ?)",
                     (db.utc_now_iso(), actor, json.dumps({"migration_id": mid, **summary}, default=str)))
    return summary


def recover() -> Optional[str]:
    """Startup: completes / undoes an interrupted migration (see the module docstring)."""
    from fund import store as fund_store
    st = fund_store.get_migration_state()
    if st is None or st["status"] != "migrating":
        return None
    m = _migration_row()
    if m is not None:      # any migration record (M2: never un-freeze once one exists, whichever request wrote it)
        fund_store.set_migration_state({"status": "migrated", "migration_id": m["id"], "at": m["created_at"]})
        return "completed"
    fund_store.set_migration_state(None)
    return "undone"
