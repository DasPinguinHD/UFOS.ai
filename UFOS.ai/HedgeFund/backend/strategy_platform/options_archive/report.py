"""Gap report, run listing and the status file of the options archive (SP §21.4) — used by the archive (writes the
status file / daily gap report JSON) and by the backend's READ-ONLY routes (``/v2/options-archive/*``)."""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from strategy_platform.core import calendars

from . import db as odb
from .config import VERSION, Config, key_status, paths
from .slots import NY, day_slots, next_slot

RANK = {"ok": 4, "partial": 3, "late": 2, "failed": 1}
GAP_ROWS_MAX = 200
GAP_DAYS_MAX = 31                 # /gaps: at most 31 days, both ends inclusive
ALIVE_SECONDS = 180


def iso_utc(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def run_view(row) -> dict:
    d = dict(row)
    return {"id": d["id"], "kind": d["kind"], "slot_label": d.get("slot_label"),
            "slot_ny": datetime.fromtimestamp(d["slot_ms"] / 1000, NY).isoformat(timespec="seconds"),
            "started_at": iso_utc(d["started_ms"]), "finished_at": iso_utc(d["finished_ms"]), "status": d["status"],
            "requests": d["requests"], "pages": d["pages"], "retries": d["retries"], "error": d["error"],
            "skew_ms": d.get("skew_ms")}


def runs(conn, limit: int = 50) -> List[dict]:
    if conn is None:
        return []
    return [run_view(r) for r in conn.execute("SELECT * FROM os_runs ORDER BY id DESC LIMIT ?", (int(limit),))]


def day_report(conn, d: date, cfg: Config, now_utc: datetime) -> dict:
    out = {"date": d.isoformat(), "trading_day": False, "early_close": False, "slots": [], "gap_rows": [],
           "gap_rows_truncated": False}
    try:
        out["trading_day"] = calendars.is_trading_day(d)
        out["early_close"] = out["trading_day"] and calendars.is_early_close(d)
        plan = day_slots(d, cfg)
    except calendars.CalendarUnavailable as ex:
        out["error"] = ex.user_message
        return out
    now_ms = int(now_utc.timestamp() * 1000)
    if not plan:
        return out
    lo, hi = plan[0].ms, plan[-1].ms
    rows = list(conn.execute("SELECT * FROM os_runs WHERE slot_ms BETWEEN ? AND ? AND kind != 'manual'",
                             (lo, hi))) if conn is not None else []
    gaps_by_run: Dict[int, int] = {}
    if conn is not None and rows:
        q = ",".join("?" * len(rows))
        for g in conn.execute(f"SELECT run_id, COUNT(*) AS n FROM os_gaps WHERE run_id IN ({q}) GROUP BY run_id",
                              [r["id"] for r in rows]):
            gaps_by_run[g["run_id"]] = g["n"]
    for s in plan:
        cands = [r for r in rows if r["slot_ms"] == s.ms and r["kind"] == s.kind]
        best = max(cands, key=lambda r: (RANK.get(r["status"], 0), r["id"])) if cands else None
        if best is not None:
            status, rid, n = best["status"], best["id"], gaps_by_run.get(best["id"], 0)
        else:
            status, rid, n = ("pending" if now_ms < s.ms else "missing"), None, (0 if now_ms < s.ms else 1)
        out["slots"].append({"slot_ny": s.ny.strftime("%H:%M"), "label": s.label, "kind": s.kind, "fixed": s.fixed,
                             "status": status, "run_id": rid, "gaps": n})
        if status == "missing":
            out["gap_rows"].append({"slot_ny": s.ny.strftime("%H:%M"), "kind": "not_collected", "underlying": None,
                                    "symbol": None, "what": f"slot {s.label} ({s.kind}) was not collected",
                                    "reason": "the archive did not run at that time (PC off / asleep / not registered)"})
    if conn is not None and rows:
        q = ",".join("?" * len(rows))
        for g in conn.execute(f"SELECT * FROM os_gaps WHERE run_id IN ({q}) ORDER BY id LIMIT ?",
                              [r["id"] for r in rows] + [GAP_ROWS_MAX + 1]):
            out["gap_rows"].append({"slot_ny": datetime.fromtimestamp(g["slot_ms"] / 1000, NY).strftime("%H:%M"),
                                    "kind": g["kind"], "underlying": g["underlying"], "symbol": g["symbol"],
                                    "what": g["what"], "reason": g["reason"]})
    if len(out["gap_rows"]) > GAP_ROWS_MAX:
        out["gap_rows"], out["gap_rows_truncated"] = out["gap_rows"][:GAP_ROWS_MAX], True
    return out


def gaps(conn, start: date, end: date, cfg: Config, now_utc: datetime) -> dict:
    if end < start or (end - start).days >= GAP_DAYS_MAX:
        raise ValueError(f"from … to must span 1 … {GAP_DAYS_MAX} days (inclusive)")
    days, d = [], start
    while d <= end:
        days.append(day_report(conn, d, cfg, now_utc))
        d += timedelta(days=1)
    return {"days": days}


def today_summary(rep: dict) -> dict:
    """Counts of today's slots. A LATE fixed slot (09:55 / 15:25) is a gap for H1 / H2: it is not ``collected`` and
    counts in ``fixed_slot_gaps`` (with failed / missing fixed slots)."""
    sl = rep["slots"]
    st = [s["status"] for s in sl]
    fixed_gaps = sum(1 for s in sl if s.get("fixed") and s["status"] in ("late", "failed", "missing"))
    late_fixed = sum(1 for s in sl if s.get("fixed") and s["status"] == "late")
    out = {"date_ny": rep["date"], "trading_day": rep["trading_day"], "early_close": rep["early_close"],
           "expected_slots": len(st),
           "collected": st.count("ok") + st.count("partial") + st.count("late") - late_fixed,
           "partial": st.count("partial") + st.count("late"), "late": st.count("late"), "failed": st.count("failed"),
           "missing": st.count("missing"), "pending": st.count("pending"), "fixed_slot_gaps": fixed_gaps}
    if rep.get("error"):
        out["error"] = rep["error"]
    return out


def safe_next_slot(now_utc: datetime, cfg: Config):
    """(next slot view | None, calendar error text | None) — the calendar's end is shown, never raised (review m7)."""
    try:
        nxt = next_slot(now_utc, cfg)
        return (nxt.view() if nxt else None), None
    except calendars.CalendarUnavailable as ex:
        return None, ex.user_message


def counts(conn) -> dict:
    if conn is None:
        return {"runs": 0, "snapshots": 0, "oi_rows": 0, "fred_rows": 0, "gaps": 0}
    q = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]   # noqa: E731
    return {"runs": q("os_runs"), "snapshots": q("os_chain_snapshots"), "oi_rows": q("os_open_interest"),
            "fred_rows": q("os_fred"), "gaps": q("os_gaps")}


def config_view(cfg: Config) -> dict:
    return {"provisional": cfg.provisional, "strike_band_pct": cfg.strike_band_pct, "expiry_days": cfg.expiry_days,
            "cadence_minutes": cfg.cadence_minutes, "fixed_slots_ny": list(cfg.fixed_slots),
            "eod_slot_ny": f"close + {cfg.eod_after_close_minutes} min", "oi_slot_ny": cfg.oi_slot,
            "fred_slot_ny": cfg.fred_slot, "quote_max_age_s": cfg.quote_max_age_s,
            "coverage_min_pct": cfg.coverage_min_pct, "oi_floor_ny": cfg.oi_floor, "rate_per_min": cfg.rate_per_min,
            "symbols": list(cfg.symbols), "fred_series": list(cfg.fred_series),
            "assumptions_hash": cfg.assumptions_hash, **key_status(cfg)}


def write_status(path: str, payload: dict, attempts: int = 5, pause: float = 0.2, sleep=time.sleep) -> bool:
    """Atomic write (tmp + replace) — the backend never reads a half file. On Windows ``os.replace`` fails with
    PermissionError while a reader has the file open: retried ``attempts`` times; NEVER raises (False = not written —
    the archive keeps collecting; the next heartbeat tries again)."""
    tmp = path + ".tmp"
    for i in range(max(1, attempts)):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1, default=str)
            os.replace(tmp, path)
            return True
        except (OSError, TypeError, ValueError):
            if i + 1 < attempts:
                sleep(pause)
    return False


def merge_status(path: str, block: str, value: dict, **kw) -> bool:
    """Writes ONE block (e.g. ``manual``) into the status file and keeps everything else as it is (``run-once`` never
    touches the service's state / heartbeat — review m11). Never raises."""
    cur = read_status(path) or {}
    cur[block] = value
    return write_status(path, cur, **kw)


def read_status(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def service_status(conn, cfg: Config, now_utc: datetime, state: str, pid: Optional[int],
                   last_error: Optional[str]) -> dict:
    """What the archive writes into ``os_archive_status.json``."""
    last = runs(conn, 1)
    nxt, cal_err = safe_next_slot(now_utc, cfg)
    rep = day_report(conn, now_utc.astimezone(NY).date(), cfg, now_utc)
    today = today_summary(rep)
    bad = today["failed"] or today["missing"] or today["fixed_slot_gaps"]
    return {"version": VERSION, "pid": pid, "state": state,
            "heartbeat_at": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "last_run": last[0] if last else None, "next_slot": nxt,
            "today": today, "counts": counts(conn), "last_error": last_error or cal_err,
            "health": "error" if (state == "error" or cal_err) else ("degraded" if bad else "ok"),
            "config": config_view(cfg)}


def status_payload(cfg: Config, now_utc: datetime, data_dir: Optional[str] = None,
                   actions: Optional[dict] = None) -> dict:
    """GET /v2/options-archive/status: the DB (read-only) + the status file; works when the archive never ran.
    ``actions`` = ``registration.availability()`` (sandbox / pythonw) — the card disables Register / Run now."""
    p = paths(data_dir)
    st = read_status(p["status"])
    conn, db_err = None, None
    try:
        conn = odb.open_ro(p["db"])
    except Exception as ex:          # noqa: BLE001
        db_err = f"the archive database could not be opened ({type(ex).__name__})"
    try:
        exists = os.path.exists(p["db"])
        size = os.path.getsize(p["db"]) if exists else None
        version = conn.execute("PRAGMA user_version").fetchone()[0] if conn is not None else None
        rep = day_report(conn, now_utc.astimezone(NY).date(), cfg, now_utc)
        last = runs(conn, 1)
        cnt = counts(conn) if conn is not None else counts(None)
    except Exception as ex:          # noqa: BLE001 — a locked / damaged DB is shown, not raised
        db_err = db_err or f"the archive database could not be read ({type(ex).__name__})"
        exists, size, version, rep, last, cnt = os.path.exists(p["db"]), None, None, None, [], counts(None)
    finally:
        if conn is not None:
            conn.close()
    hb = None
    if st and st.get("heartbeat_at"):
        try:
            hb = datetime.fromisoformat(str(st["heartbeat_at"]).replace("Z", "+00:00"))
        except ValueError:
            hb = None
    alive = None if st is None else (hb is not None and (now_utc - hb).total_seconds() <= ALIVE_SECONDS)
    nxt, cal_err = safe_next_slot(now_utc, cfg)
    today = today_summary(rep) if rep else None
    last_error = (st or {}).get("last_error")
    if cal_err:
        last_error = cal_err
    if not exists and st is None and not cal_err:
        state = "not_set_up"
    elif db_err or cal_err or (st and st.get("state") == "error"):
        state = "error"
    elif (today and (today["failed"] or today["missing"] or today["fixed_slot_gaps"])) or \
            (st is not None and st.get("state") not in ("stopped", None) and alive is False):
        state = "degraded"
    else:
        state = "ok"
    return {"state": state,
            "db": {"exists": exists, "schema_version": version, "size_bytes": size, "error": db_err},
            "service": {"status_file": st is not None, "state": (st or {}).get("state"),
                        "heartbeat_at": (st or {}).get("heartbeat_at"), "alive": alive, "pid": (st or {}).get("pid"),
                        "version": (st or {}).get("version")},
            "manual": (st or {}).get("manual"),
            "last_run": last[0] if last else None, "next_slot": nxt, "today": today,
            "counts": cnt, "last_error": last_error, "config": config_view(cfg),
            "sandbox": bool((actions or {}).get("sandbox")),
            "actions_enabled": bool((actions or {}).get("available", True)),
            "actions_note": (actions or {}).get("unavailable_reason"),
            "paths": {"db": p["db"], "status_file": p["status"], "log": p["log"]}}
