"""Decay monitor (SPEC §19.2): ONE documented rule set (``RULES`` — every constant in one place), evaluated once
per completed session of every active incubation run with ONLY the returns up to that session and the frozen
expected basis (§19.1, no look-ahead). A trigger PAUSES the strategy (the runtime withholds its orders; positions,
capital and protective orders are untouched) and asks the user to decide: keep running or demote (§17.2 abort).

Storage: ``mon_decay_checks`` (one row per run + day), ``mon_decay_events`` (paused / keep_running / demoted) — both
insert-only. Audit ``decay_paused`` (engine) / ``decay_decision`` (user; written BEFORE acting — no audit, no action).
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from ..execution.errors import TradingError
from ..storage import db
from . import expected

logger = logging.getLogger(__name__)

# --- the rule (SPEC §19.2 / §19.14; every constant in ONE place, indexed by the holdout basis) ---------------------
# default (user away 2026-10-18) — confirm. Calibrated by the seeded Monte Carlo of tests/test_sp_monitor_decay.py
# (SPEC §19.14): noise ≈ 5 % pauses per strategy-year for each basis, decay of the daily mean by 0.5 σ detected.
_BASE = {"MIN_SESSIONS": 20, "RETURN_WINDOW": 60, "RETURN_CONSECUTIVE": 3, "VOL_WINDOW": 20, "VOL_FACTOR": 2.0,
         "VOL_CONSECUTIVE": 3}
RULES_BY_BASIS = {
    "equity_24m": dict(_BASE, RETURN_Z=3.0, DD_FACTOR=2.0),    # 24-month equity holdout (§9.1 default)
    "equity_12m": dict(_BASE, RETURN_Z=3.5, DD_FACTOR=2.5),
    "equity_6m": dict(_BASE, RETURN_Z=3.5, DD_FACTOR=3.5),
    "crypto_12m": dict(_BASE, RETURN_Z=3.2, DD_FACTOR=2.5),      # crypto-only universes, UTC days
}
BASIS_LABELS = {"equity_24m": "equity holdout ≥ 18 months", "equity_12m": "equity holdout 9–18 months",
                "equity_6m": "equity holdout < 9 months", "crypto_12m": "crypto holdout (UTC days)"}
RULES = RULES_BY_BASIS["equity_24m"]          # the default basis (kept for callers that need one set)


def basis_of(exp: Optional[dict], calendar: Optional[str] = None) -> str:
    """The rule set of an expected basis: crypto (UTC calendar) or the equity holdout length in sessions."""
    cal = calendar or (exp or {}).get("calendar") or "nyse"
    if cal == "utc":
        return "crypto_12m"
    n = (exp or {}).get("sessions")
    n = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else 504
    if n >= 378:
        return "equity_24m"
    if n >= 189:
        return "equity_12m"
    return "equity_6m"


def rules_for(exp: Optional[dict], calendar: Optional[str] = None) -> dict:
    return RULES_BY_BASIS[basis_of(exp, calendar)]


RULE_IDS = ("return_band", "drawdown", "volatility")
RULE_LABELS = {"return_band": "Return below the expected band", "drawdown": "Drawdown beyond the limit",
               "volatility": "Volatility far above the expected"}


def rule_text(R: dict) -> List[str]:
    return [
        f"return_band: the sum of the last w = min(n, {R['RETURN_WINDOW']}) daily log returns is below "
        f"w·μ − {R['RETURN_Z']:g}·σ·√w for {R['RETURN_CONSECUTIVE']} consecutive sessions (n ≥ {R['MIN_SESSIONS']}; "
        "μ, σ from the holdout run, frozen at incubation start).",
        f"drawdown: the current drawdown exceeds {R['DD_FACTOR']:g} × the holdout max drawdown (fires at once).",
        f"volatility: the realised volatility of the last {R['VOL_WINDOW']} sessions exceeds {R['VOL_FACTOR']:g} × the "
        f"expected volatility for {R['VOL_CONSECUTIVE']} consecutive sessions (n ≥ {R['MIN_SESSIONS']}).",
        "A trigger pauses the strategy (no new or increasing orders; positions kept; its own risk-reducing protective "
        "exits still run) until you decide: keep running (from then on ONLY the returns after your decision count, "
        "with the same minimum sample) or demote (back to validated; its positions are then closed by the runtime).",
    ]


RULE_TEXT = rule_text(RULES)
ACTIONS = ("keep_running", "demote")
DEMOTE_CONFIRM = "DEMOTE"
SYNTHETIC_NUMBERS_NOTE = "synthetic pause — live numbers not affected"
PAUSE_REASON = "paused by the decay monitor — waiting for your decision (keep running / demote)"
NOT_PAUSED = "This strategy is not paused by the decay monitor."
NOT_FOUND = "This strategy has never incubated — nothing is monitored."
REASON_MAX = 500


def rules_payload() -> dict:
    return {"rules": dict(RULES), "default_basis": "equity_24m",
            "by_basis": {k: dict(v) for k, v in RULES_BY_BASIS.items()}, "basis_labels": dict(BASIS_LABELS),
            "text": rule_text(RULES)}


# --- pure assessment ------------------------------------------------------------------------------------------

def _std(xs: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    m = math.fsum(xs) / n
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (n - 1))


def _band(lr: Sequence[float], k: int, mu: float, sd: float, R: dict):
    """(breached, S, threshold) of return_band on the first k returns."""
    w = min(k, R["RETURN_WINDOW"])
    s = math.fsum(lr[k - w:k])
    thr = w * mu - R["RETURN_Z"] * sd * math.sqrt(w)
    return s < thr, s, thr


def _vol(lr: Sequence[float], k: int, P: int, vexp: float, R: dict):
    vw = R["VOL_WINDOW"]
    s = _std(lr[k - vw:k]) * math.sqrt(P)
    thr = R["VOL_FACTOR"] * vexp
    return s > thr, s, thr


def decay_line(lr: Sequence[float], exp: dict) -> Optional[float]:
    """The cumulative-log level the curve must stay above (return_band), for the chart; None before MIN_SESSIONS."""
    R = rules_for(exp)
    k = len(lr)
    sd = float(exp["std_daily"])
    if k < R["MIN_SESSIONS"] or sd <= 0:
        return None
    w = min(k, R["RETURN_WINDOW"])
    before = math.fsum(lr[:k - w])
    return before + w * float(exp["mean_daily"]) - R["RETURN_Z"] * sd * math.sqrt(w)


def assess(exp: dict, lr: Sequence[float], calendar: str = "nyse", rearm: Optional[dict] = None) -> dict:
    """The rules on the returns ``lr`` (oldest first, up to the assessed session only). ``rearm`` = the latest
    keep-running decision ``{index}``: from then on ONLY the returns after it count (a fresh sample with the same
    minimum before a re-pause; the drawdown is measured from the decision)."""
    R = rules_for(exp, calendar)
    ri = int((rearm or {}).get("index") or 0)
    lr = list(lr[ri:]) if ri else list(lr)
    n = len(lr)
    P = expected.periods(calendar)
    mu, sd = float(exp["mean_daily"]), float(exp["std_daily"])
    mdd = exp.get("max_drawdown")
    vexp = sd * math.sqrt(P)
    MIN = R["MIN_SESSIONS"]
    rules: List[dict] = []

    def rule(rid, breached=False, count=0, needed=1, value=None, threshold=None, message=None):
        rules.append({"rule": rid, "label": RULE_LABELS[rid], "breached": bool(breached), "count": int(count),
                      "needed": int(needed), "value": value, "threshold": threshold, "message": message})

    since = " since your keep-running decision" if ri else ""
    need = R["RETURN_CONSECUTIVE"]
    if sd <= 0:
        rule("return_band", needed=need, message="the expected volatility is 0 — the band is undefined")
    elif n < MIN:
        rule("return_band", needed=need, message=f"needs {MIN} sessions{since} ({n} so far)")
    else:
        br, s, thr = _band(lr, n, mu, sd, R)
        count, k = 0, n
        while br and k >= MIN:
            if not _band(lr, k, mu, sd, R)[0]:
                break
            count += 1
            k -= 1
        rule("return_band", br, count, need, s, thr, None)
    dd_now, _mdd_real = expected.drawdown_now(lr)
    if not isinstance(mdd, (int, float)) or mdd is None:
        rule("drawdown", message="the holdout max drawdown is unknown")
    else:
        thr = R["DD_FACTOR"] * float(mdd)
        br = dd_now > thr + 1e-12
        rule("drawdown", br, 1 if br else 0, 1, dd_now, thr, None)
    vneed = R["VOL_CONSECUTIVE"]
    vmin = max(MIN, R["VOL_WINDOW"])
    if sd <= 0:
        rule("volatility", needed=vneed, message="the expected volatility is 0")
    elif n < vmin:
        rule("volatility", needed=vneed, message=f"needs {vmin} sessions{since} ({n} so far)")
    else:
        br, s, thr = _vol(lr, n, P, vexp, R)
        count, k = 0, n
        while br and k >= vmin:
            if not _vol(lr, k, P, vexp, R)[0]:
                break
            count += 1
            k -= 1
        rule("volatility", br, count, vneed, s, thr, None)
    triggered = [r["rule"] for r in rules if r["breached"] and r["count"] >= r["needed"]]
    if triggered:
        state = "paused"
    elif any(r["breached"] for r in rules):
        state = "watch"
    elif n < MIN:
        state = "insufficient"
    else:
        state = "ok"
    return {"n": n, "state": state, "triggered": triggered, "rules": rules, "drawdown": dd_now,
            "basis": basis_of(exp, calendar), "since_index": ri}


# --- storage --------------------------------------------------------------------------------------------------

def _now(now: Optional[datetime] = None) -> datetime:
    from ..execution import accounts
    return (now or accounts.now()).astimezone(timezone.utc)


def events(run_id: Optional[str] = None, strategy_id: Optional[str] = None, limit: int = 200) -> List[dict]:
    q, args = "SELECT * FROM mon_decay_events", []
    conds = []
    if run_id:
        conds.append("run_id = ?")
        args.append(run_id)
    if strategy_id:
        conds.append("strategy_id = ?")
        args.append(strategy_id)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    out = []
    for r in reversed(rows):
        r["data"] = json.loads(r.pop("data_json")) if r.get("data_json") else {}
        out.append(r)
    return out


def latest_event(run_id: str) -> Optional[dict]:
    ev = events(run_id=run_id, limit=1)
    return ev[-1] if ev else None


def _insert_event(strategy_id: str, run_id: str, action: str, actor: str, reason: Optional[str], rule: Optional[str],
                  check_day: Optional[str], data: dict, ts: str) -> dict:
    with db._connect() as conn:
        cur = conn.execute("INSERT INTO mon_decay_events (ts, strategy_id, run_id, action, actor, reason, rule, check_day,"
                           " data_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (ts, strategy_id, run_id, action, actor, reason, rule, check_day,
                            json.dumps(data, default=str)))
        eid = int(cur.lastrowid)
    return {"id": eid, "ts": ts, "strategy_id": strategy_id, "run_id": run_id, "action": action, "actor": actor,
            "reason": reason, "rule": rule, "check_day": check_day, "data": data}


def checked_days(run_id: str) -> set:
    with db._connect() as conn:
        return {r["day"] for r in conn.execute("SELECT day FROM mon_decay_checks WHERE run_id = ?", (run_id,))}


def checks(run_id: str, limit: int = 250) -> List[dict]:
    with db._connect() as conn:
        rows = conn.execute("SELECT day, ts, state, result_json FROM mon_decay_checks WHERE run_id = ? ORDER BY day DESC"
                            " LIMIT ?", (run_id, int(limit))).fetchall()
    out = []
    for r in reversed(rows):
        res = json.loads(r["result_json"])
        out.append({"day": r["day"], "ts": r["ts"], "state": r["state"], "triggered": res.get("triggered") or [],
                    "stats": {x["rule"]: {"value": x["value"], "threshold": x["threshold"], "count": x["count"],
                                          "breached": x["breached"]} for x in res.get("rules") or []}})
    return out


def paused_run_ids() -> set:
    """Runs whose latest decay event is ``paused`` (the runtime withholds their orders)."""
    with db._connect() as conn:
        rows = conn.execute("SELECT e.run_id, e.action FROM mon_decay_events e JOIN (SELECT run_id, MAX(id) AS m FROM"
                            " mon_decay_events GROUP BY run_id) x ON e.id = x.m").fetchall()
    return {r["run_id"] for r in rows if r["action"] == "paused"}


def _rearm_of(ev: Optional[dict]) -> Optional[dict]:
    if ev is None or ev["action"] != "keep_running":
        return None
    d = ev.get("data") or {}
    return {"index": d.get("index") or 0}


# --- checks ---------------------------------------------------------------------------------------------------

def check_run(run: dict, now: Optional[datetime] = None) -> dict:
    """Evaluates every completed session of ``run`` that has no check yet (in date order, each with data up to
    that day only). Returns {checked, paused, error}."""
    now = _now(now)
    exp = expected.basis(run)
    if exp is None:
        return {"checked": 0, "paused": False, "error": "the run has no expected basis"}
    last = expected.last_completed(run, now)
    if last is None:
        return {"checked": 0, "paused": False, "error": None}
    series = expected.realised(run, last)
    done = checked_days(run["id"])
    ev = latest_event(run["id"])
    lr = [x for _d, x in series]
    checked, paused = 0, False
    ts = now.isoformat(timespec="seconds")
    for idx, (day, _x) in enumerate(series):
        if day in done:
            continue
        is_paused = ev is not None and ev["action"] == "paused"
        a = assess(exp, lr[:idx + 1], run["calendar"], _rearm_of(ev))
        state = "paused" if is_paused else a["state"]
        res = {"n": a["n"], "triggered": a["triggered"], "rules": a["rules"], "drawdown": a["drawdown"],
               "rearm": _rearm_of(ev)}
        with db._connect() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO mon_decay_checks (run_id, strategy_id, day, ts, state, result_json)"
                               " VALUES (?, ?, ?, ?, ?, ?)", (run["id"], run["strategy_id"], day, ts, state,
                                                             json.dumps(res, default=str)))
            inserted = cur.rowcount == 1
        if not inserted:
            continue                       # a concurrent check stored this day first
        checked += 1
        if not is_paused and a["triggered"]:
            ev = _pause(run, a, day, ts)
            paused = True
    return {"checked": checked, "paused": paused, "error": None}


def _pause(run: dict, a: dict, day: str, ts: str, *, synthetic: Optional[dict] = None) -> dict:
    """``synthetic`` (sandbox only, tools/sandbox.py force-decay — SPEC "Sandbox profile" S5): the assessment ran on
    a synthetic deviation; the reason, the event data and the audit row say so."""
    from . import alerts
    rule = a["triggered"][0]
    r = next(x for x in a["rules"] if x["rule"] == rule)
    name = (db.strategy_get(run["strategy_id"]) or {}).get("name") or run["strategy_id"]
    reason = _reason_text(rule, r)
    data = {"index": a["n"], "dd": a["drawdown"], "triggered": a["triggered"], "value": r["value"],
            "threshold": r["threshold"]}
    if synthetic:
        reason = f"SANDBOX force-decay — synthetic deviation, not real performance: {reason}"
        data.update(synthetic)
    # fix1023 round 2: ONE pause event per pause — the latest event is re-read under the write lock, so a check and a
    # sandbox force-decay (another process) racing for the same run cannot both pause it (and raise two alerts)
    with db._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute("SELECT id, action FROM mon_decay_events WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                            (run["id"],)).fetchone()
        if last is not None and last["action"] == "paused":
            eid = None
        else:
            db._insert_audit(conn, "engine", "decay_paused",
                             {"strategy_id": run["strategy_id"], "run_id": run["id"], "rule": rule, "day": day,
                              "value": r["value"], "threshold": r["threshold"],
                              **({"synthetic": True} if synthetic else {})})
            cur = conn.execute("INSERT INTO mon_decay_events (ts, strategy_id, run_id, action, actor, reason, rule,"
                               " check_day, data_json) VALUES (?, ?, ?, 'paused', 'engine', ?, ?, ?, ?)",
                               (ts, run["strategy_id"], run["id"], reason, rule, day, json.dumps(data, default=str)))
            eid = int(cur.lastrowid)
    if eid is None:                       # paused meanwhile by the other path: no second event, no second alert
        ev = dict(latest_event(run["id"]), existing=True)
        logger.info("decay monitor: %s was already paused (event %s) — no second pause", run["strategy_id"], ev["id"])
        return ev
    ev = {"id": eid, "ts": ts, "strategy_id": run["strategy_id"], "run_id": run["id"], "action": "paused",
          "actor": "engine", "reason": reason, "rule": rule, "check_day": day, "data": data}
    try:
        alerts.emit("decay", f"Strategy paused: decay detected — {name}"[:160],
                    f"{reason}. The strategy keeps its positions but sends no new orders until you decide (keep "
                    f"running / demote) in Monitoring. Paper trading · simulated money.",
                    f"decay:{run['id']}:{day}:{ev['id']}", account_id=run["account_id"], strategy_id=run["strategy_id"],
                    ref=run["id"], data={"rule": rule, "day": day})
    except Exception:            # noqa: BLE001 — the pause itself stands; the alert failure is logged
        logger.exception("decay alert failed")
    logger.warning("decay monitor paused %s (%s): %s", run["strategy_id"], rule, reason)
    return ev


def _reason_text(rule: str, r: dict) -> str:
    v, t = r.get("value"), r.get("threshold")
    if rule == "return_band":
        return (f"Return below the expected band for {r['count']} sessions (rolling log return {v:.4f} < "
                f"{t:.4f})")
    if rule == "drawdown":
        return f"Drawdown {v:.2%} beyond the limit {t:.2%} (a multiple of the holdout max drawdown)"
    return f"Volatility {v:.2%} above {t:.2%} (2 × expected) for {r['count']} sessions"


def run_checks(now: Optional[datetime] = None) -> dict:
    from ..live import incubation
    out = {"checked": 0, "paused": [], "errors": {}}
    for run in incubation.active_runs():
        try:
            res = check_run(run, now)
            out["checked"] += res["checked"]
            if res["paused"]:
                out["paused"].append(run["strategy_id"])
            if res.get("error"):
                out["errors"][run["strategy_id"]] = res["error"]
        except Exception as ex:   # noqa: BLE001 — one run failing never stops the others
            logger.exception("decay check of %s failed", run.get("id"))
            out["errors"][run["strategy_id"]] = (getattr(ex, "user_message", None) or type(ex).__name__)[:200]
    return out


# --- protective exits while paused (review fix M3 + addition, 2026-10-18) ---------------------------------------

PROTECTIVE_KIND = "protective_exit_while_paused"


def record_protective_exit(run: dict, order: dict, qty: float, symbols) -> Optional[int]:
    """One alert per OMS order and source (dedupe ``protexit:<client order id>:<source>``) — visible in the
    strategy's monitoring view. Never raises into the runtime (a failure is logged)."""
    from . import alerts
    try:
        name = (db.strategy_get(run["strategy_id"]) or {}).get("name") or run["strategy_id"]
        sym = order.get("symbol")
        reason = (f"protective exit ({', '.join(symbols)}: stop loss / take profit / drawdown stop of the strategy) "
                  f"while paused by the decay monitor")
        return alerts.emit(PROTECTIVE_KIND, f"Protective exit while paused: {name} — {order.get('side')} {sym}"[:160],
                           f"The paused strategy reduced {sym} by {abs(float(qty)):g} ({reason}). Only risk-reducing "
                           "exits run while it is paused; nothing opens or increases exposure. Order "
                           f"{order.get('client_order_id')}: {order.get('state')}. Paper trading · simulated money.",
                           f"protexit:{order.get('client_order_id')}:{run['source_id']}",
                           account_id=run["account_id"], strategy_id=run["strategy_id"], ref=run["id"],
                           data={"run_id": run["id"], "symbol": sym, "qty": abs(float(qty)), "side": order.get("side"),
                                 "client_order_id": order.get("client_order_id"), "state": order.get("state"),
                                 "reason": reason})
    except Exception:            # noqa: BLE001 — recording must never block a protective exit
        logger.exception("protective exit alert failed")
        return None


def protective_exits(strategy_id: str, limit: int = 100) -> List[dict]:
    with db._connect() as conn:
        rows = conn.execute("SELECT id, ts, data_json FROM mon_alerts WHERE kind = ? AND strategy_id = ? ORDER BY id DESC"
                            " LIMIT ?", (PROTECTIVE_KIND, strategy_id, int(limit))).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["data_json"]) if r["data_json"] else {}
        out.append({"alert_id": r["id"], "ts": r["ts"], **{k: d.get(k) for k in ("run_id", "symbol", "qty", "side",
                                                                                  "client_order_id", "state",
                                                                                  "reason")}})
    return out


# --- views ----------------------------------------------------------------------------------------------------

def run_decisions(run_id: str) -> List[dict]:
    """fix1023 round 2 (B3): the decay decision history of ONE incubation run (paused / keep_running / demoted), oldest
    first — for the run detail, so it stays reachable after a demote / abort."""
    return [{"id": e["id"], "ts": e["ts"], "action": e["action"], "actor": e["actor"], "reason": e["reason"],
             "rule": e["rule"], "check_day": e["check_day"], "synthetic": (e.get("data") or {}).get("synthetic") is True}
            for e in events(run_id=run_id, limit=500)]


def run_for(strategy_id: str) -> Optional[dict]:
    from ..live import incubation
    for r in incubation.active_runs():
        if r["strategy_id"] == strategy_id:
            return r
    rows = incubation.run_rows(strategy_id)
    return rows[0] if rows else None


def monitor_view(run: dict, now: Optional[datetime] = None, *, with_series: bool = False) -> dict:
    from ..live import incubation
    now = _now(now)
    exp = expected.basis(run)
    last = expected.last_completed(run, now)
    series = expected.realised(run, last) if last is not None else []
    tr = expected.track(exp, series, decay_line if exp else None)
    strat = db.strategy_get(run["strategy_id"]) or {}
    end = incubation.end_event(run["id"])
    ev = latest_event(run["id"])
    decay = {"state": "insufficient", "last_check_day": None, "rules": [], "paused": None,
             "basis": basis_of(exp, run["calendar"]), "since_index": 0}
    if exp is not None:
        a = assess(exp, [x for _d, x in series], run["calendar"], _rearm_of(ev))
        decay["state"] = a["state"] if a["state"] != "paused" else "watch"    # only an event pauses
        decay["rules"] = a["rules"]
        decay["basis"] = a["basis"]
        decay["since_index"] = a["since_index"]
    if ev is not None and ev["action"] == "paused":
        decay["state"] = "paused"
        d = ev.get("data") or {}
        synthetic = d.get("synthetic") is True
        decay["paused"] = {"at": ev["ts"], "rule": ev["rule"], "reason": ev["reason"], "check_day": ev["check_day"],
                           # fix1023 B2: the numbers that triggered THIS pause (not the live assessment)
                           "numbers": {"rule": ev["rule"], "value": d.get("value"), "threshold": d.get("threshold"),
                                       "triggered": list(d.get("triggered") or []), "synthetic": synthetic}}
        if synthetic:                                             # sandbox force-decay (absent otherwise)
            from ..core import fixture
            decay["paused"].update(synthetic=True, numbers_note=SYNTHETIC_NUMBERS_NOTE, **fixture.fields())
    days = checks(run["id"], limit=1)
    decay["last_check_day"] = days[-1]["day"] if days else None
    decay["last_check_at"] = days[-1]["ts"] if days else None
    decay["last_check_source"] = "check" if days else None
    if ev is not None and ev["action"] == "paused" and (decay["last_check_at"] is None or ev["ts"] > decay["last_check_at"]):
        # fix1023 B2: the pause is the newest decay evaluation (a forced or same-day pause has no check row)
        decay["last_check_day"], decay["last_check_at"], decay["last_check_source"] = ev["check_day"], ev["ts"], "pause"
    with db._connect() as conn:
        decay["protective_exits"] = int(conn.execute("SELECT COUNT(*) FROM mon_alerts WHERE kind = ? AND ref = ?",
                                                     (PROTECTIVE_KIND, run["id"])).fetchone()[0])
    out = {"strategy_id": run["strategy_id"], "name": strat.get("name"), "version": run["version"],
           "state": strat.get("state"), "run_id": run["id"], "account_id": run["account_id"],
           "run_status": "running" if end is None else end["event"], "calendar": run["calendar"],
           "sessions": tr["sessions"], "last_session": tr["last_session"], "realised": tr["realised"],
           "expected": expected.expected_out(exp), "deviation": tr["deviation"], "decay": decay}
    from ..core import fixture
    out.update(fixture.strategy_fields(run["strategy_id"]))      # sandbox fixture label (absent otherwise)
    if with_series:
        out["_series"] = tr["points"]
    return out


def detail(strategy_id: str, now: Optional[datetime] = None) -> dict:
    run = run_for(strategy_id)
    if run is None:
        raise TradingError(404, "not_found", NOT_FOUND)
    view = monitor_view(run, now, with_series=True)
    series = view.pop("_series")
    evs = [{"id": e["id"], "ts": e["ts"], "action": e["action"], "actor": e["actor"], "reason": e["reason"],
            "rule": e["rule"], "run_id": e["run_id"]} for e in events(strategy_id=strategy_id)]
    return {"strategy": view, "series": series, "checks": checks(run["id"]), "events": evs, "rules": rules_payload(),
            "protective_exits": protective_exits(strategy_id)}


def decide(strategy_id: str, action: str, reason: str, confirm: Optional[str] = None, actor: str = "user",
           now: Optional[datetime] = None) -> dict:
    from ..live import incubation
    if action not in ACTIONS:
        raise ValueError("action: keep_running | demote")
    now = _now(now)
    run = None
    for r in incubation.active_runs():
        if r["strategy_id"] == strategy_id:
            run = r
    if run is None:
        if not incubation.run_rows(strategy_id):
            raise TradingError(404, "not_found", NOT_FOUND)
        raise TradingError(409, "not_paused", NOT_PAUSED)
    ev = latest_event(run["id"])
    if ev is None or ev["action"] != "paused":
        raise TradingError(409, "not_paused", NOT_PAUSED)
    # fix1023: no typed "DEMOTE" any more (``confirm`` accepted and ignored)
    reason = (reason or "").strip()[:REASON_MAX]
    ts = now.isoformat(timespec="seconds")
    exp = expected.basis(run)
    last = expected.last_completed(run, now)
    lr = [x for _d, x in (expected.realised(run, last) if last is not None else [])]
    dd_now, _ = expected.drawdown_now(lr)
    # the audit row first: a user action without its audit row is refused (CLAUDE.md)
    db.add_audit(actor, "decay_decision", {"strategy_id": strategy_id, "run_id": run["id"], "action": action,
                                           "reason": reason, "paused_rule": ev.get("rule")})
    if action == "keep_running":
        out_ev = _insert_event(strategy_id, run["id"], "keep_running", actor, reason, ev.get("rule"),
                               last.isoformat() if last else None, {"index": len(lr), "dd": dd_now}, ts)
    else:
        why = "demoted after the decay monitor paused it" + (f": {reason}" if reason else "")
        incubation.abort(run["id"], why, actor, move_state=True,
                         now=now)
        out_ev = _insert_event(strategy_id, run["id"], "demoted", actor, reason, ev.get("rule"),
                               last.isoformat() if last else None, {"index": len(lr), "dd": dd_now}, ts)
    _ = exp
    return {"strategy": monitor_view(run, now), "event": {k: out_ev[k] for k in ("id", "ts", "action", "actor",
                                                                                 "reason", "rule", "run_id")}}
