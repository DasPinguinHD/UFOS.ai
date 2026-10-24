"""R2 — the switch-over from the old engine to the Council strategy, and the revert (SP §22.8, §22.8a, §22.2, §22.4).

ONLY the user triggers it (the app's dialog → ``POST /v2/council/switchover``); agents and tests run it on temp copies.

Run (``run``): refused in sandbox (409 ``sandbox``) and while a blocker exists (409 ``switchover_blocked`` + list):
the old paper account must be migrated (§17.9 ``pf_migrations``), ``paper-council``'s ``_unallocated`` empty, the
old autopilot off, the pre-v25 backup present (or the database born at v25+), the parity gate accepted (user approval
2026-10-24 of the deliberate differences, ``PARITY_ACCEPTED``). Then:
(1) audit (audit-first) + meta ``council.switchover = pending``;
(2) the running ``FundEngine`` is stopped IN-PROCESS (a running cycle is awaited, the scheduler task cancelled, the
    autopilot forced off under its ``_trade_lock``) and unregistered from ``control``;
(3) ONE hedgefund.db transaction (``apply``): the Council params copied from the latest old ``fund_params`` version
    (§22.4), ``autotrade.heartbeat_minutes`` from the old ``autopilot_heartbeat_minutes``, the spend counter seeded
    with max(old meta ``fund.ai_spend`` today, Σ cost log ``fund-sentiment`` + ``fund-judge`` today) (§22.2), the
    legacy import above the high-water mark ``council.legacy_hwm`` (§22.8a) and the transaction marker
    ``council.r2_tx`` carrying the attempt id of step (1) — idempotent: a marker with the SAME attempt id means the
    transaction already happened (a later switch-over after a revert has a new id);
(4) meta ``council.switchover = done`` + audit; the Council orders stay OFF (the user turns them on); the Council task
    is started.
A crash between (1) and (4) leaves ``pending``: ``recover`` (backend start) re-runs (3) idempotently, then (4). Any
failure after (1) keeps ``pending`` with ``last_error`` (+ time, audit, critical alert ``council_switchover_failed``);
the user then chooses ``retry`` (the SAME attempt) or ``abort`` (back to ``none`` / ``reverted``, the old engine
re-created with autopilot off). One operation at a time (``_op_lock``, 409 ``switchover_running``); a new run while
``pending`` is blocked (review M1 / M2; API: scratchpad/mr/SWITCHOVER_FIX_API.md).

Revert (``revert``, before R4 only): Council orders off + its working orders cancelled, the Council task stopped, the
old engine's spend counter (newsroom.db meta ``fund.ai_spend``) seeded with max(old meta, cost log today), meta
``council.switchover = reverted``, then the old engine is RE-CONSTRUCTED by the factory ``main.py`` registered
(``configure``), started and handed to ``control`` / the ``/fund`` API / the AI-budget source. The ``cl_*`` rows stay
(insert-only); a later run imports only old ids above the high-water mark.

The old store is read through ``newsroom.db`` directly (no ``fund`` import — the S10-C guard).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from strategy_platform.core import params as P
from strategy_platform.storage import db

from . import params as cparams
from . import spend, store

logger = logging.getLogger(__name__)

PARITY_ACCEPTED = "2026-10-24"        # SP §22.10 / §22.13: parity report + deliberate differences approved by the user
HEARTBEAT_KEY = "autotrade.heartbeat_minutes"
META_R2TX = "council.r2_tx"
OLD_SPEND_KEY = "fund.ai_spend"
OLD_AUTOPILOT_KEY = "fund.autopilot"
COST_FEATURES = ("fund-sentiment", "fund-judge")
IMPORT_TRIGGERS = {"schedule": "scheduled", "manual": "manual", "startup": "startup"}
ENGINE_STOP_WAIT_S = 120.0

_engine_factory: Optional[Callable[[], Any]] = None
_on_engine: Optional[Callable[[Any], None]] = None


class SwitchoverError(Exception):
    def __init__(self, status: int, code: str, message: str, blockers: Optional[List[dict]] = None, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.blockers = status, code, message, blockers or []
        self.extra = extra

    def payload(self) -> dict:
        out = {"error": self.message, "code": self.code}
        if self.blockers:
            out["blockers"] = self.blockers
        out.update(self.extra)
        return out


# ONE switch-over operation at a time in this process (run / retry / abort / revert / recover — review M1); a second
# request gets 409 ``switchover_running`` instead of waiting
_op_lock = threading.Lock()


class _Operation:
    def __enter__(self):
        if not _op_lock.acquire(blocking=False):
            raise SwitchoverError(409, "switchover_running",
                                  "A switch-over operation is already running — wait for it.")
        return self

    def __exit__(self, *exc):
        _op_lock.release()
        return False


def configure(engine_factory: Optional[Callable[[], Any]], on_engine: Optional[Callable[[Any], None]]) -> None:
    """main.py: how to RE-CONSTRUCT the old engine on a revert, and who must learn about it (control, /fund API,
    AI budgets)."""
    global _engine_factory, _on_engine
    _engine_factory, _on_engine = engine_factory, on_engine


def _now() -> datetime:
    from strategy_platform.execution import accounts
    return accounts.now().astimezone(timezone.utc)


# --- the old store (newsroom.db), read-only except the revert's spend seed --------------------------------------

def _ndb():
    from newsroom import db as ndb
    if not ndb.is_enabled():
        raise SwitchoverError(503, "legacy_unavailable", "The old engine's store (newsroom.db) is not available.")
    return ndb


def _old_meta(key: str) -> Optional[str]:
    with _ndb()._connect() as c:
        r = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def old_params() -> Tuple[int, Dict[str, Any]]:
    """(version, full params) of the latest old ``fund_params`` version merged over the defaults (as
    ``fund.store.effective_params``); version 0 = nothing stored (the defaults)."""
    with _ndb()._connect() as c:
        r = c.execute("SELECT version, params_json FROM fund_params ORDER BY version DESC LIMIT 1").fetchone()
    if r is None:
        return 0, P.defaults()
    try:
        stored = json.loads(r["params_json"])
    except (TypeError, ValueError) as ex:
        raise SwitchoverError(503, "legacy_unreadable", f"Old params v{r['version']} are unreadable.") from ex
    if not isinstance(stored, dict):
        raise SwitchoverError(503, "legacy_unreadable", f"Old params v{r['version']} are unreadable.")
    return int(r["version"]), P.merge(stored, None)


def _old_autopilot() -> bool:
    return _old_meta(OLD_AUTOPILOT_KEY) == "1"


def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


def old_spend_today(now: datetime) -> Optional[float]:
    """The old counter for today; None = unreadable (callers fail closed)."""
    v = _old_meta(OLD_SPEND_KEY)
    if v is None:
        return 0.0
    try:
        d = json.loads(v)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    if d.get("day") != _day(now):
        return 0.0
    try:
        usd = float(d.get("usd") or 0.0)
    except (TypeError, ValueError):
        return None
    return usd if usd == usd and usd >= 0 else None


def cost_log_today(now: datetime) -> Optional[float]:
    """Σ cost_usd of today's (UTC) ``fund-sentiment`` / ``fund-judge`` lines of module HedgeFund in the shared AI
    cost log; missing file = 0; unreadable = None (fail closed). Malformed single lines are skipped (logged)."""
    from newsroom import cost_ledger
    path = cost_ledger.ledger_path()
    if not os.path.exists(path):
        return 0.0
    day, total, bad = _day(now), 0.0, 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(e, dict) or e.get("module") != cost_ledger.MODULE \
                        or e.get("feature") not in COST_FEATURES or str(e.get("ts") or "")[:10] != day:
                    continue
                try:
                    total += max(0.0, float(e.get("cost_usd") or 0.0))
                except (TypeError, ValueError):
                    bad += 1
    except OSError:
        logger.exception("AI cost log unreadable")
        return None
    if bad:
        logger.warning("AI cost log: %d malformed line(s) skipped", bad)
    return total


def spend_seed(now: datetime) -> float:
    """max(old meta today, cost log today) — refuses (503) when either is unreadable (fail closed: no guessed 0)."""
    a, b = old_spend_today(now), cost_log_today(now)
    if a is None or b is None:
        raise SwitchoverError(503, "spend_unreadable", "Today's AI spend could not be read (old counter or cost "
                                                       "log) — nothing was switched over.")
    return max(a, b)


def _hwm() -> Dict[str, int]:
    v = db.meta_get(store.META_HWM)
    if not v:
        return {"runs": 0, "recommendations": 0}
    d = json.loads(v)
    return {"runs": int(d.get("runs") or 0), "recommendations": int(d.get("recommendations") or 0)}


def _is_risk_watch(run: dict) -> bool:
    """The old risk watch also ran with trigger "startup"; its runs carry ``summary.risk_watch`` (no decisions)."""
    try:
        s = json.loads(run.get("summary_json") or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(s, dict) and s.get("risk_watch") is True


def legacy_rows(hwm: Dict[str, int]) -> Dict[str, list]:
    """The old rows above the high-water mark: full-cycle runs (risk-watch runs are not imported), their signals,
    recommendations and the params versions they reference."""
    with _ndb()._connect() as c:
        runs = [dict(r) for r in c.execute("SELECT * FROM fund_runs WHERE id > ? ORDER BY id", (hwm["runs"],))]
        max_run = max([r["id"] for r in runs], default=hwm["runs"])
        runs = [r for r in runs if r["trigger"] in IMPORT_TRIGGERS and not _is_risk_watch(r)]
        ids = [r["id"] for r in runs]
        sigs, recs = [], []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            sigs += [dict(r) for r in c.execute(f"SELECT * FROM fund_agent_scores WHERE run_id IN ({q}) "
                                                "ORDER BY run_id, symbol, agent", chunk)]
            recs += [dict(r) for r in c.execute(f"SELECT * FROM fund_recommendations WHERE run_id IN ({q}) AND "
                                                "id > ? ORDER BY id", chunk + [hwm["recommendations"]])]
        max_rec = c.execute("SELECT COALESCE(MAX(id), 0) FROM fund_recommendations").fetchone()[0]
        versions = sorted({r["params_version"] for r in runs})
        params = {}
        for v in versions:
            p = c.execute("SELECT params_json FROM fund_params WHERE version = ?", (v,)).fetchone()
            params[v] = json.loads(p["params_json"]) if p else None
    return {"runs": runs, "signals": sigs, "recommendations": recs, "params": params,
            "hwm": {"runs": int(max_run), "recommendations": max(int(max_rec), hwm["recommendations"])}}


# --- preconditions / state ----------------------------------------------------------------------------------------

def _backup_problem() -> Optional[str]:
    path = db.db_path()
    if not path or path == ":memory:":
        return None
    folder = os.path.join(os.path.dirname(os.path.abspath(path)), "backups")
    prefix = f"{os.path.basename(path)}.pre-v25-"
    if os.path.isdir(folder) and any(n.startswith(prefix) and not n.endswith(".tmp") for n in os.listdir(folder)):
        return None
    try:
        born = int(db.meta_get(db.META_CREATED_VERSION) or 0)
    except (TypeError, ValueError):
        born = 0
    if born >= 25:
        return None                      # created at v25+: nothing before v25 to keep
    return "no pre-v25 backup of hedgefund.db found (data/backups/hedgefund.db.pre-v25-*)"


LEGACY_UNIQUE_TABLES = ("cl_runs", "cl_decisions")


def legacy_duplicates() -> Dict[str, int]:
    """{table: surplus rows} of legacy rows sharing an ``old_id`` (a double import from before the v26 unique
    indexes — the index could not be created on such a database). Empty = clean."""
    out: Dict[str, int] = {}
    with db._connect() as conn:
        for t in LEGACY_UNIQUE_TABLES:
            n = conn.execute(f"SELECT COUNT(*) - COUNT(DISTINCT old_id) FROM {t} WHERE legacy = 1 "
                             "AND old_id IS NOT NULL").fetchone()[0]
            if n:
                out[t] = int(n)
    return out


def check_legacy_duplicates() -> Dict[str, int]:
    """Backend start + every precondition check: duplicates → a CRITICAL alert (deduped per table and count) — never
    a silent degraded mode; the switch-over is blocked by ``legacy_duplicates`` until they are resolved."""
    dup = legacy_duplicates()
    for t, n in dup.items():
        _alert("council_legacy_duplicates", "Council history imported twice",
               f"{n} duplicate legacy row(s) in {t} (same old id) — the unique index of schema v26 could not be "
               f"created; the switch-over stays blocked. See SPEC §22.14.", f"council-legacy-dup-{t}-{n}")
    return dup


def state_info() -> dict:
    """The raw switch-over meta: {state, tx, at, actor, previous, last_error, error_at} (state "none" when unset);
    raises StorageUnavailable when unreadable (callers fail closed)."""
    st = store.switchover()
    try:
        raw = json.loads(db.meta_get(store.META_SWITCHOVER) or "{}")
    except (TypeError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    return {"state": st, "tx": raw.get("tx"), "at": raw.get("at"), "actor": raw.get("actor"),
            "previous": raw.get("previous") if raw.get("previous") in ("none", "reverted") else "none",
            "last_error": raw.get("last_error"), "error_at": raw.get("error_at")}


def pending_info() -> Optional[dict]:
    """While ``pending``: the attempt, whether its transaction already committed, and its last error (review M2)."""
    info = state_info()
    if info["state"] != "pending":
        return None
    applied = bool(info["tx"]) and _marker_tx(db.meta_get(META_R2TX)) == info["tx"]
    return {"tx": info["tx"], "at": info["at"], "actor": info["actor"], "previous": info["previous"],
            "applied": applied, "last_error": info["last_error"], "error_at": info["error_at"]}


def blockers(engine=None) -> List[dict]:
    from strategy_platform.execution import accounts, migration
    from . import service
    from .service import _unallocated_problem
    out: List[dict] = []
    try:
        st = store.switchover()
    except Exception:          # noqa: BLE001
        return [{"code": "state_unreadable", "message": "the Council switch-over state is unreadable"}]
    if st == "done":
        out.append({"code": "already_switched_over", "message": "the Council is already switched over"})
    if st == "pending":
        out.append({"code": "switchover_pending",
                    "message": "a switch-over attempt is unfinished — retry or abort it"})
    if service.default() is None:
        out.append({"code": "council_unavailable",
                    "message": "the Council service could not be built at backend start — see backend.log"})
    if not PARITY_ACCEPTED:
        out.append({"code": "parity_not_accepted", "message": "the parity report is not accepted yet"})
    try:
        if not migration.platform_migrated():
            out.append({"code": "not_migrated", "message": "the old paper account is not migrated to paper-council "
                                                           "(Portfolio & Trading → migration first)"})
    except Exception:          # noqa: BLE001
        out.append({"code": "migration_unreadable", "message": "the migration record is unreadable"})
    if not accounts.exists(accounts.COUNCIL):
        out.append({"code": "no_account", "message": "the account paper-council does not exist"})
    else:
        problem = _unallocated_problem(accounts.get(accounts.COUNCIL))
        if problem:
            out.append({"code": "unallocated", "message": problem})
    try:
        if _old_autopilot():
            out.append({"code": "autopilot_on", "message": "the old engine's autopilot is on — switch it off first"})
    except SwitchoverError as ex:
        out.append({"code": ex.code, "message": ex.message})
    bp = _backup_problem()
    if bp:
        out.append({"code": "no_backup", "message": bp})
    try:
        dup = check_legacy_duplicates()
    except Exception:          # noqa: BLE001 — unreadable = blocked (fail closed)
        logger.exception("legacy duplicate check failed")
        dup = {"cl_runs / cl_decisions": -1}
    for t, n in dup.items():
        out.append({"code": "legacy_duplicates", "table": t, "rows": n,
                    "message": (f"{n} duplicate imported history row(s) in {t} (same old id) — resolve them before a "
                                "switch-over" if n >= 0 else "the imported history could not be checked")})
    return out


def _sandbox() -> bool:
    try:
        import sandbox_profile
        return bool(sandbox_profile.is_sandbox())
    except Exception:          # noqa: BLE001 — fail closed
        return True


def _refuse_sandbox() -> None:
    if _sandbox():
        raise SwitchoverError(409, "sandbox", "Sandbox mode — the switch-over is refused.")


def dry_run(engine=None) -> dict:
    """What a switch-over would do (writes nothing) + the unfinished attempt and the dialog's actions (review M2)."""
    now = _now()
    version, old = old_params()
    council, notes = cparams.from_old(old)
    hb = old.get("autopilot_heartbeat_minutes")
    try:
        seed = spend_seed(now)
        seed_err = None
    except SwitchoverError as ex:
        seed, seed_err = None, ex.message
    hwm = _hwm()
    rows = legacy_rows(hwm)
    b = blockers(engine)
    if seed_err:
        b.append({"code": "spend_unreadable", "message": seed_err})
    sandbox = _sandbox()
    if sandbox:
        b.insert(0, {"code": "sandbox", "message": "sandbox mode — the switch-over is refused"})
    state = store.switchover()
    return {"ok": not b, "blockers": b, "state": state, "parity_accepted": PARITY_ACCEPTED,
            "pending": pending_info(),
            "actions": {"run": state in ("none", "reverted") and not b,
                        "retry": state == "pending" and not sandbox, "abort": state == "pending" and not sandbox,
                        "revert": state == "done" and not sandbox},
            "params": {"from_version": version, "council": council, "notes": notes},
            "heartbeat_minutes": hb, "spend_seed_usd": seed,
            "legacy": {"runs": len(rows["runs"]), "signals": len(rows["signals"]),
                       "recommendations": len(rows["recommendations"]), "hwm_before": hwm, "hwm_after": rows["hwm"]}}


# --- the transaction ----------------------------------------------------------------------------------------------

def _import_legacy(conn, rows: dict, now: datetime) -> Dict[str, int]:
    pv: Dict[int, int] = {}
    for v, p in rows["params"].items():
        if p is None:
            continue
        pv[v] = store.add_params(p, "system", "legacy import (R2)", f"legacy fund_params v{v}", legacy=True,
                                 now=now, conn=conn)
    run_map: Dict[int, int] = {}
    for r in rows["runs"]:
        rid = store.create_run(IMPORT_TRIGGERS[r["trigger"]], r["started_at"], pv.get(r["params_version"]),
                               "open" if r["market_open"] else "closed", None, None, old_id=r["id"], legacy=True,
                               conn=conn)
        run_map[r["id"]] = rid
        try:
            summary = json.loads(r["summary_json"]) if r["summary_json"] else {}
        except ValueError:
            summary = {"raw": str(r["summary_json"])[:500]}
        if not isinstance(summary, dict):
            summary = {"value": summary}
        status = r["status"]
        if status == "running":
            status, summary["error"] = "error", "never finished (old engine)"
        elif status == "skipped":
            status, summary["note"] = "partial", "skipped (old engine)"
        elif status not in ("ok", "partial", "error"):
            status, summary["note"] = "error", f"old status {status!r}"
        if r.get("error"):
            summary.setdefault("error", r["error"])
        if r.get("models"):
            summary.setdefault("models", r["models"])
        store.finish_run(rid, r["finished_at"] or r["started_at"], status, r["judge_mode"], r["cost_usd"] or 0.0,
                         summary, conn=conn)
    sig_rows: Dict[int, list] = {}
    for s in rows["signals"]:
        ai = s["source"] in ("llm", "cached")
        try:
            details = json.loads(s["details_json"]) if s["details_json"] else {}
        except ValueError:
            details = {}
        try:
            prov = json.loads(s["provenance_json"]) if (ai and s["provenance_json"]) else None
        except ValueError:
            prov = None
        sig_rows.setdefault(run_map[s["run_id"]], []).append(
            {"symbol": s["symbol"], "agent": s["agent"], "score": s["score"], "confidence": s["confidence"],
             "summary": s["summary"], "details": details, "source": s["source"], "ai_generated": ai,
             "provenance": prov})
    for rid, lst in sig_rows.items():
        store.save_signals(rid, lst, legacy=True, conn=conn)
    for rec in rows["recommendations"]:
        src = rec["source"]
        ai = src in ("llm", "carried")
        try:
            prov = json.loads(rec["provenance_json"]) if (ai and rec["provenance_json"]) else None
        except ValueError:
            prov = None
        try:
            usage = json.loads(rec["usage_json"]) if rec["usage_json"] else None
        except ValueError:
            usage = None
        cost = None
        if isinstance(usage, dict):
            for k in ("cost", "cost_usd"):
                if usage.get(k) is not None:
                    try:
                        cost = float(usage[k])
                    except (TypeError, ValueError):
                        cost = None
                    break
        did = store.add_decision({"old_id": rec["id"], "run_id": run_map[rec["run_id"]], "symbol": rec["symbol"],
                                  "rule_score": rec["rule_score"], "judge_source": src, "action": rec["action"],
                                  "confidence": rec["confidence"], "rationale": rec["rationale"],
                                  "model": rec["model"], "cost_share_usd": cost,
                                  "target_weight": rec["target_weight"], "ai_generated": ai and prov is not None,
                                  "provenance": prov, "legacy": True}, conn=conn)
        if rec["status"] == "executed":
            store.add_execution(did, "submitted", f"old engine order #{rec['order_id']}", rec["ts"], conn=conn)
        elif rec["status"] == "skipped":
            store.add_execution(did, "skipped", rec["skip_reason"], rec["ts"], conn=conn)
    return {"runs": len(rows["runs"]), "signals": len(rows["signals"]), "recommendations": len(rows["recommendations"]),
            "params_versions": len(pv)}


def _meta_put(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value = excluded.value, updated_at = excluded.updated_at", (key, value, db.utc_now_iso()))


def _marker_tx(raw: Optional[str]) -> Optional[str]:
    try:
        return json.loads(raw).get("tx") if raw else None
    except (ValueError, AttributeError):
        return None


def pending_tx() -> str:
    """The attempt id stored with ``council.switchover = pending`` ("" for an old state row without one)."""
    try:
        return str(json.loads(db.meta_get(store.META_SWITCHOVER) or "{}").get("tx") or "")
    except (ValueError, AttributeError):
        return ""


def apply(actor: str = "user", reason: Optional[str] = None, tx: Optional[str] = None) -> dict:
    """Step 3 — ONE hedgefund.db transaction; idempotent per attempt (``council.r2_tx`` with the same attempt id
    ``tx`` → nothing is written again). The high-water mark is re-read INSIDE ``BEGIN IMMEDIATE``: when another
    import moved it meanwhile, nothing is written (409 ``concurrent_import``, review M1)."""
    now = _now()
    tx = pending_tx() if tx is None else tx
    marker = db.meta_get(META_R2TX)
    if marker and _marker_tx(marker) == tx:
        return {"already_applied": True, **json.loads(marker)}
    version, old = old_params()
    council, notes = cparams.from_old(old)
    hb_lim = P.LIMITS["autopilot_heartbeat_minutes"]
    hb = float(old.get("autopilot_heartbeat_minutes", P.DEFAULTS["autopilot_heartbeat_minutes"]))
    hb = max(5.0, min(240.0, max(float(hb_lim["min"]), min(float(hb_lim["max"]), hb))))
    seed = spend_seed(now)
    hwm = _hwm()
    rows = legacy_rows(hwm)
    with db._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_R2TX,)).fetchone()
        if row and _marker_tx(row[0]) == tx:
            return {"already_applied": True, **json.loads(row[0])}
        cur = conn.execute("SELECT value FROM meta WHERE key = ?", (store.META_HWM,)).fetchone()
        now_hwm = json.loads(cur[0]) if cur else {"runs": 0, "recommendations": 0}
        if {k: int(now_hwm.get(k) or 0) for k in ("runs", "recommendations")} != hwm:
            raise SwitchoverError(409, "concurrent_import", "The legacy history was imported by another operation "
                                                            "meanwhile — nothing was written; reload and retry.")
        imported = _import_legacy(conn, rows, now)
        src = f"copied from fund_params v{version}" + (" (defaults)" if version == 0 else "")
        pver = store.add_params(council, actor, (reason or None) and "; ".join([reason] + notes) or
                                ("; ".join(notes) or None), src, now=now, conn=conn)
        _meta_put(conn, HEARTBEAT_KEY, json.dumps({"minutes": hb, "source": f"fund_params v{version}"}))
        spend.seed(conn, now, seed)
        _meta_put(conn, store.META_HWM, json.dumps(rows["hwm"]))
        summary = {"tx": tx, "at": now.isoformat(timespec="seconds"), "params_version": pver,
                   "from_fund_params": version,
                   "notes": notes, "heartbeat_minutes": hb, "spend_seed_usd": seed, "legacy": imported,
                   "hwm": rows["hwm"]}
        _meta_put(conn, META_R2TX, json.dumps(summary))
    return summary


def _set_state(state: str, actor: str, extra: Optional[dict] = None) -> None:
    db.meta_set(store.META_SWITCHOVER, json.dumps({"state": state, "at": _now().isoformat(timespec="seconds"),
                                                    "actor": actor, **(extra or {})}))


def _alert(kind: str, title: str, body: str, key: str) -> None:
    try:
        from strategy_platform.monitoring import alerts
        alerts.emit(kind, title, body, key)
    except Exception:          # noqa: BLE001 — an alert never blocks the switch-over path
        logger.exception("alert %s failed", kind)


def _record_failure(tx: str, message: str) -> None:
    """A failure after ``pending`` (review M2): the state stays ``pending`` with ``last_error`` + time (shown by GET
    /v2/council/switchover and /v2/council/state), audit + a critical alert; the user retries or aborts."""
    info = state_info()
    at = _now().isoformat(timespec="seconds")
    _set_state("pending", info["actor"] or "system", {"tx": tx, "previous": info["previous"],
                                                     "last_error": message[:500], "error_at": at})
    try:
        db.add_audit("system", "council_switchover", {"stage": "failed", "tx": tx, "error": message[:500]})
    except Exception:          # noqa: BLE001
        logger.exception("audit row council_switchover (failed) failed")
    _alert("council_switchover_failed", "Council switch-over did not finish",
           f"{message[:400]} — open the Council view and choose Retry or Abort. The old engine stays stopped until "
           f"then.", f"council-switchover-failed-{tx}-{at}")
    logger.error("Council switch-over attempt %s did not finish: %s", tx, message)


async def _stop_engine(engine) -> None:
    """Stops the old engine in-process: no new cycle (``_stopping`` first), waits for a running cycle, cancels the
    scheduler, autopilot off under its trade lock, unregistered from control (SP §22.8, re-review N3)."""
    from strategy_platform.execution import control
    if engine is None:
        return
    engine._switchover_refused = True             # FundEngine.start refuses from now on (also reads the state)
    engine._stopping = True                       # review minor 1: the scheduler starts no new cycle while we wait
    waited = 0.0
    while getattr(engine, "cycle_in_progress", False) and waited < ENGINE_STOP_WAIT_S:
        await asyncio.sleep(0.25)
        waited += 0.25
    await engine.stop()
    lock = getattr(engine, "_trade_lock", None)

    def off():
        if lock is not None:
            with lock:
                engine.store.set_autopilot(False)
        else:
            engine.store.set_autopilot(False)
    await asyncio.to_thread(off)
    if control.legacy_engine() is engine:
        control.set_legacy_engine(None)


def _start_council() -> Tuple[bool, Optional[str]]:
    from . import service
    svc = service.default()
    if svc is None:
        return False, "the Council service is not available (it could not be built at backend start)"
    try:
        svc.start()
        return True, None
    except Exception as ex:      # noqa: BLE001 — reported in the answer + alert, never swallowed (review minor 3)
        msg = f"{type(ex).__name__}: {ex}"[:300]
        _alert("council_run_failed", "Council task did not start", msg, f"council-task-start-{_now().isoformat()}")
        return False, msg


async def _complete(actor: str, reason: str, tx: str, engine) -> dict:
    """Steps 2–4 of one attempt (also the retry). A failure keeps ``pending`` + ``last_error`` (503)."""
    try:
        await _stop_engine(engine)
        summary = await asyncio.to_thread(apply, actor, reason, tx)
        await asyncio.to_thread(_finish, actor, summary)
    except Exception as ex:      # noqa: BLE001
        msg = getattr(ex, "message", None) or f"{type(ex).__name__}: {ex}"
        await asyncio.to_thread(_record_failure, tx, str(msg))
        raise SwitchoverError(503, "switchover_failed", f"The switch-over did not finish ({str(msg)[:200]}) — retry "
                                                        "or abort it.", last_error=str(msg)[:500]) from ex
    started, err = _start_council()
    return {"state": "done", **{k: v for k, v in summary.items() if k != "already_applied"},
            "council_task_started": started, "council_task_error": err}


async def run(reason: str = "", actor: str = "user", engine=None) -> dict:
    """The switch-over (see the module docstring). Raises SwitchoverError (409 / 503)."""
    with _Operation():
        _refuse_sandbox()
        b = await asyncio.to_thread(blockers, engine)
        if b:
            raise SwitchoverError(409, "switchover_blocked", "The switch-over is blocked.", b)
        await asyncio.to_thread(spend_seed, _now())           # refuse BEFORE anything changes when unreadable
        previous = await asyncio.to_thread(store.switchover)
        try:
            await asyncio.to_thread(db.add_audit, actor, "council_switchover", {"stage": "pending", "reason": reason})
        except Exception as ex:  # noqa: BLE001 — audit-first
            raise SwitchoverError(503, "audit_unavailable", "audit log unavailable — the action was not carried "
                                                            "out") from ex
        tx = uuid.uuid4().hex[:16]
        await asyncio.to_thread(_set_state, "pending", actor, {"tx": tx, "previous": previous})
        return await _complete(actor, reason, tx, engine)


async def retry(reason: str = "", actor: str = "user") -> dict:
    """The SAME unfinished attempt again (review M2)."""
    from strategy_platform.execution import control
    with _Operation():
        _refuse_sandbox()
        info = await asyncio.to_thread(state_info)
        if info["state"] != "pending":
            raise SwitchoverError(409, "not_pending", "There is no unfinished switch-over attempt.")
        try:
            await asyncio.to_thread(db.add_audit, actor, "council_switchover", {"stage": "retry", "tx": info["tx"],
                                                                              "reason": reason})
        except Exception as ex:  # noqa: BLE001
            raise SwitchoverError(503, "audit_unavailable", "audit log unavailable — the action was not carried "
                                                            "out") from ex
        return await _complete(actor, reason, info["tx"] or "", control.legacy_engine())


def _finish(actor: str, summary: dict) -> None:
    store.set_orders_enabled(False, actor, "switchover")      # the user turns the Council orders on
    _set_state("done", actor, {"tx": summary.get("tx")})
    try:
        db.add_audit(actor, "council_switchover", {"stage": "done", **{k: summary.get(k) for k in
                                                                       ("params_version", "spend_seed_usd",
                                                                        "legacy", "hwm")}})
    except Exception:            # noqa: BLE001 — the switch-over happened; the pending audit row exists
        logger.exception("audit row council_switchover (done) failed")


def recover() -> Optional[str]:
    """Backend start: ``pending`` → the same attempt is finished once (idempotent). On failure the state stays
    ``pending`` with ``last_error`` + a critical alert (review M2) and "pending" is returned — main.py then runs NO
    engine and the Council view offers Retry / Abort. Returns the state."""
    try:
        st = store.switchover()
    except Exception:          # noqa: BLE001
        logger.exception("Council switch-over state unreadable at start — the old engine is NOT started")
        return "unreadable"
    try:
        check_legacy_duplicates()            # visible at every start (critical alert), never only a log line
    except Exception:          # noqa: BLE001
        logger.exception("legacy duplicate check at start failed")
    if st != "pending":
        return st
    try:
        with _Operation():
            tx = pending_tx()
            try:
                summary = apply("system", "recovered at backend start", tx)
                _finish("system", summary)
            except Exception as ex:  # noqa: BLE001
                _record_failure(tx, str(getattr(ex, "message", None) or f"{type(ex).__name__}: {ex}"))
                return "pending"
    except SwitchoverError:
        return "pending"
    logger.warning("Council switch-over recovered at start (was pending)")
    return "done"


def _start_old_engine(actor: str):
    """Re-creates the old engine (autopilot OFF), starts it and hands it to control / the /fund API / budgets.
    Returns (started, error)."""
    from strategy_platform.execution import control
    try:
        with _ndb()._connect() as c:
            c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
                      "excluded.value", (OLD_AUTOPILOT_KEY, "0"))
    except Exception:            # noqa: BLE001 — start() forces it off as well
        logger.exception("could not write the old autopilot flag")
    engine = _engine_factory()
    started, error = True, None
    try:
        engine.start()
    except RuntimeError as ex:   # FundEngine.start refusal (state) or no running event loop — both reported
        started, error = False, str(ex)[:300]
        logger.error("old engine not started: %s", ex)
    control.set_legacy_engine(engine)
    if _on_engine is not None:
        _on_engine(engine)
    return started, error


def _seed_old(now: datetime, usd: float) -> None:
    with _ndb()._connect() as c:
        c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
                  "excluded.value", (OLD_SPEND_KEY, json.dumps({"day": _day(now), "usd": usd})))


async def abort(reason: str = "", actor: str = "user") -> dict:
    """Gives up an unfinished attempt and goes back to the old engine (review M2)."""
    from strategy_platform.execution import control
    from . import service
    with _Operation():
        _refuse_sandbox()
        info = await asyncio.to_thread(pending_info)
        if info is None:
            raise SwitchoverError(409, "not_pending", "There is no unfinished switch-over attempt.")
        if _engine_factory is None:
            raise SwitchoverError(503, "engine_unavailable", "The old engine cannot be re-created in this backend.")
        now = _now()
        seed = await asyncio.to_thread(spend_seed, now) if info["applied"] else None
        try:
            await asyncio.to_thread(db.add_audit, actor, "council_switchover_abort",
                                    {"tx": info["tx"], "applied": info["applied"], "reason": reason,
                                     "last_error": info["last_error"]})
        except Exception as ex:  # noqa: BLE001
            raise SwitchoverError(503, "audit_unavailable", "audit log unavailable — the action was not carried "
                                                            "out") from ex
        await asyncio.to_thread(control.council_orders_off, "abort", actor)
        svc = service.default()
        if svc is not None:
            await svc.stop()
        if info["applied"]:
            await asyncio.to_thread(_seed_old, now, seed)
            state = "reverted"
        else:
            state = info["previous"]
        await asyncio.to_thread(_set_state, state, actor, {"aborted_tx": info["tx"]})
        started, error = _start_old_engine(actor)
        return {"state": state, "applied": info["applied"], "old_engine_started": started, "old_engine_error": error}


async def revert(reason: str = "", actor: str = "user") -> dict:
    """Back to the old engine (before R4 only; see the module docstring)."""
    from strategy_platform.execution import control
    from . import service
    with _Operation():
        _refuse_sandbox()
        state = await asyncio.to_thread(store.switchover)
        if state == "reverted":                       # review minor 2: idempotent, no false "not reverted"
            eng = control.legacy_engine()
            return {"state": "reverted", "already": True, "old_engine_started": bool(getattr(eng, "running", False)),
                    "old_engine_error": None if eng is not None else "the old engine is not registered"}
        if state != "done":
            raise SwitchoverError(409, "not_switched_over", "The Council is not switched over — nothing to revert"
                                  + (" (an attempt is unfinished — abort it instead)." if state == "pending" else "."))
        if _engine_factory is None:
            raise SwitchoverError(503, "engine_unavailable", "The old engine cannot be re-created in this backend.")
        now = _now()
        seed = await asyncio.to_thread(spend_seed, now)
        try:
            await asyncio.to_thread(db.add_audit, actor, "council_revert", {"reason": reason,
                                                                          "old_spend_seed_usd": seed})
        except Exception as ex:  # noqa: BLE001
            raise SwitchoverError(503, "audit_unavailable", "audit log unavailable — the action was not carried "
                                                            "out") from ex
        await asyncio.to_thread(control.council_orders_off, "revert", actor)
        svc = service.default()
        if svc is not None:
            await svc.stop()
        await asyncio.to_thread(_seed_old, now, seed)
        await asyncio.to_thread(_set_state, "reverted", actor)
        started, error = _start_old_engine(actor)
        return {"state": "reverted", "old_spend_seed_usd": seed, "old_engine_started": started,
                "old_engine_error": error}
