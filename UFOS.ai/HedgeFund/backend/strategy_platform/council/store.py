"""Council of AIs storage (SP §22.3) — hedgefund.db v25, every table insert-only (triggers). Times UTC ISO seconds.

``cl_executions`` holds the SUBMIT-TIME outcome only; an order's later lifecycle is read from the OMS by
``client_order_id``. Rows imported from the old engine carry ``legacy = 1`` (and ``old_id``; SP §22.8a).
Meta keys: ``council.switchover``, ``council.orders_enabled``, ``council.ai_spend``, ``council.legacy_hwm``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from strategy_platform.storage import db

META_SWITCHOVER = "council.switchover"
META_ORDERS = "council.orders_enabled"
META_SPEND = "council.ai_spend"
META_HWM = "council.legacy_hwm"
SWITCHOVER_STATES = ("none", "pending", "done", "reverted")


def _iso(t) -> str:
    if isinstance(t, str):
        return t
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _j(v) -> Optional[str]:
    return None if v is None else json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def _l(t):
    if t is None:
        return None
    try:
        return json.loads(t)
    except ValueError:
        return t


# --- params -------------------------------------------------------------------------------------------------

def latest_params(conn=None) -> Optional[Tuple[int, Dict[str, Any]]]:
    """(version, params) of the newest EFFECTIVE (non-legacy) Council params, or None (not copied yet)."""
    def q(c):
        r = c.execute("SELECT version, params_json FROM cl_params WHERE legacy = 0 ORDER BY version DESC LIMIT 1") \
            .fetchone()
        return (int(r["version"]), json.loads(r["params_json"])) if r else None
    if conn is not None:
        return q(conn)
    with db._connect() as c:
        return q(c)


def add_params(params: Dict[str, Any], actor: str, reason: Optional[str], source: str, legacy: bool = False,
               now=None, conn=None) -> int:
    def ins(c):
        nxt = (c.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM cl_params").fetchone()[0])
        c.execute("INSERT INTO cl_params (version, params_json, created_at, actor, reason, source, legacy) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)", (nxt, _j(params), _iso(now or datetime.now(timezone.utc)), actor,
                                                   (reason or None) and str(reason)[:500], source[:200],
                                                   1 if legacy else 0))
        return int(nxt)
    if conn is not None:
        return ins(conn)
    with db._connect() as c:
        return ins(c)


def latest_broker_override(conn=None) -> Optional[Tuple[int, Dict[str, Any]]]:
    def q(c):
        r = c.execute("SELECT version, params_json FROM cl_broker_params ORDER BY version DESC LIMIT 1").fetchone()
        return (int(r["version"]), json.loads(r["params_json"])) if r else None
    if conn is not None:
        return q(conn)
    with db._connect() as c:
        return q(c)


def add_broker_override(params: Dict[str, Any], actor: str, reason: Optional[str], now=None, conn=None) -> int:
    def ins(c):
        nxt = c.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM cl_broker_params").fetchone()[0]
        c.execute("INSERT INTO cl_broker_params (version, params_json, created_at, actor, reason) VALUES (?, ?, ?, ?, ?)",
                  (nxt, _j(params), _iso(now or datetime.now(timezone.utc)), actor,
                   (reason or None) and str(reason)[:500]))
        return int(nxt)
    if conn is not None:
        return ins(conn)
    with db._connect() as c:
        return ins(c)


# --- runs ---------------------------------------------------------------------------------------------------

def create_run(trigger: str, started_at, params_version: Optional[int], session_state: Optional[str],
               orders_enabled: Optional[bool], sandbox: Optional[bool], *, old_id: Optional[int] = None,
               legacy: bool = False, conn=None) -> int:
    def ins(c):
        cur = c.execute("INSERT INTO cl_runs (old_id, trigger, started_at, params_version, session_state, "
                        "orders_enabled, sandbox, legacy) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (old_id, trigger, _iso(started_at), params_version, session_state,
                         None if orders_enabled is None else int(bool(orders_enabled)),
                         None if sandbox is None else int(bool(sandbox)), 1 if legacy else 0))
        return int(cur.lastrowid)
    if conn is not None:
        return ins(conn)
    with db._connect() as c:
        return ins(c)


def finish_run(run_id: int, finished_at, status: str, judge_mode: Optional[str], cost_usd: float,
               summary: Optional[dict], conn=None) -> None:
    def ins(c):
        c.execute("INSERT INTO cl_run_results (run_id, finished_at, status, judge_mode, cost_usd, summary_json) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (run_id, _iso(finished_at), status, judge_mode,
                                                float(cost_usd or 0.0), _j(summary)))
    if conn is not None:
        return ins(conn)
    with db._connect() as c:
        ins(c)


INTERRUPTED = "interrupted — the backend stopped before this Council cycle finished"


def sweep_unfinished(now) -> int:
    """Council task start: every non-legacy run without a result (backend stopped mid-cycle or before a "Run now"
    row was picked up) gets an "error" result — no run stays "running" forever. Returns the count."""
    with db._connect() as c:
        ids = [r[0] for r in c.execute("SELECT r.id FROM cl_runs r LEFT JOIN cl_run_results x ON x.run_id = r.id "
                                        "WHERE x.run_id IS NULL AND r.legacy = 0")]
        for rid in ids:
            finish_run(rid, now, "error", None, 0.0, {"error": INTERRUPTED}, conn=c)
    return len(ids)


def save_signals(run_id: int, rows: Iterable[dict], legacy: bool = False, conn=None) -> None:
    """rows: {symbol, agent, score, confidence, summary, details, source, ai_generated, provenance}."""
    vals = [(run_id, r["symbol"], r["agent"], r.get("score"), r.get("confidence"), r.get("summary"),
             _j(r.get("details")), r.get("source") or "rules", 1 if r.get("ai_generated") else 0,
             _j(r.get("provenance")) if r.get("ai_generated") else None, 1 if legacy else 0) for r in rows]
    sql = ("INSERT INTO cl_signals (run_id, symbol, agent, score, confidence, summary, details_json, source, "
           "ai_generated, provenance_json, legacy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    if conn is not None:
        conn.executemany(sql, vals)
        return
    with db._connect() as c:
        c.executemany(sql, vals)


DECISION_COLS = ("old_id", "run_id", "symbol", "rule_score", "agreement", "rule_action", "judge_source", "action",
                 "confidence", "rationale", "model", "cost_share_usd", "unavailable_reason", "target_weight",
                 "sizing_note", "ai_generated", "provenance_json", "legacy")


def add_decision(d: dict, conn=None) -> int:
    row = dict(d)
    row["provenance_json"] = _j(row.pop("provenance", None)) if row.get("ai_generated") else None
    row["ai_generated"] = 1 if row.get("ai_generated") else 0
    row["legacy"] = 1 if row.get("legacy") else 0
    vals = tuple(row.get(k) for k in DECISION_COLS)

    def ins(c):
        cur = c.execute(f"INSERT INTO cl_decisions ({', '.join(DECISION_COLS)}) VALUES "
                        f"({', '.join('?' * len(DECISION_COLS))})", vals)
        return int(cur.lastrowid)
    if conn is not None:
        return ins(conn)
    with db._connect() as c:
        return ins(c)


def add_execution(decision_id: Optional[int], outcome: str, reason: Optional[str], at,
                  client_order_id: Optional[str] = None, conn=None) -> int:
    def ins(c):
        cur = c.execute("INSERT INTO cl_executions (decision_id, client_order_id, outcome, reason, at) "
                        "VALUES (?, ?, ?, ?, ?)", (decision_id, client_order_id, outcome,
                                                   (reason or None) and str(reason)[:500], _iso(at)))
        return int(cur.lastrowid)
    if conn is not None:
        return ins(conn)
    with db._connect() as c:
        return ins(c)


def save_positions(run_id: int, rows: Iterable[dict], conn=None) -> None:
    vals = [(run_id, r["symbol"], r.get("qty"), r.get("avg_price"), r.get("mark"), r.get("weight"),
             r.get("subledger_equity")) for r in rows]
    sql = ("INSERT INTO cl_positions (run_id, symbol, qty, avg_price, mark, weight, subledger_equity) "
           "VALUES (?, ?, ?, ?, ?, ?, ?)")
    if conn is not None:
        conn.executemany(sql, vals)
        return
    with db._connect() as c:
        c.executemany(sql, vals)


def add_ai_call(run_id: Optional[int], feature: str, model: Optional[str], request_hash: Optional[str],
                usage: Optional[dict], cost_usd: float, status: str, provenance: Optional[dict], at,
                conn=None) -> int:
    u = usage or {}

    def ins(c):
        cur = c.execute("INSERT INTO cl_ai_calls (run_id, feature, model, request_hash, prompt_tokens, "
                        "completion_tokens, cost_usd, status, provenance_json, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_id, feature, model, request_hash, u.get("prompt_tokens"), u.get("completion_tokens"),
                         float(cost_usd or 0.0), status, _j(provenance), _iso(at)))
        return int(cur.lastrowid)
    if conn is not None:
        return ins(conn)
    with db._connect() as c:
        return ins(c)


# --- reads --------------------------------------------------------------------------------------------------

def _run_view(r) -> dict:
    d = dict(r)
    return {"id": d["id"], "trigger": d["trigger"], "started_at": d["started_at"], "finished_at": d.get("finished_at"),
            "status": d.get("status") or ("running" if d.get("finished_at") is None else None),
            "judge_mode": d.get("judge_mode"), "cost_usd": d.get("cost_usd"), "params_version": d["params_version"],
            "session_state": d.get("session_state"),
            "orders_enabled": None if d.get("orders_enabled") is None else bool(d["orders_enabled"]),
            "legacy": bool(d["legacy"]), "summary": _l(d.get("summary_json"))}


_RUN_SQL = ("SELECT r.*, x.finished_at, x.status, x.judge_mode, x.cost_usd, x.summary_json FROM cl_runs r "
            "LEFT JOIN cl_run_results x ON x.run_id = r.id")


def runs(limit: int = 50) -> List[dict]:
    limit = max(1, min(int(limit), 200))
    with db._connect() as c:
        return [_run_view(r) for r in c.execute(_RUN_SQL + " ORDER BY r.id DESC LIMIT ?", (limit,))]


def run_detail(run_id: int) -> Optional[dict]:
    with db._connect() as c:
        r = c.execute(_RUN_SQL + " WHERE r.id = ?", (int(run_id),)).fetchone()
        if r is None:
            return None
        out = _run_view(r)
        out["signals"] = [dict(x, details=_l(x["details_json"]), provenance=_l(x["provenance_json"]))
                          for x in map(dict, c.execute("SELECT * FROM cl_signals WHERE run_id = ? ORDER BY symbol, "
                                                       "agent", (run_id,)))]
        out["decisions"] = [dict(x, provenance=_l(x["provenance_json"])) for x in
                            map(dict, c.execute("SELECT * FROM cl_decisions WHERE run_id = ? ORDER BY symbol",
                                                (run_id,)))]
        ids = [d["id"] for d in out["decisions"]]
        out["executions"] = [dict(x) for x in c.execute(
            f"SELECT * FROM cl_executions WHERE decision_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids)] \
            if ids else []
        out["ai_calls"] = [dict(x, provenance=_l(x["provenance_json"])) for x in
                           map(dict, c.execute("SELECT * FROM cl_ai_calls WHERE run_id = ? ORDER BY id", (run_id,)))]
        out["positions"] = [dict(x) for x in c.execute("SELECT * FROM cl_positions WHERE run_id = ? ORDER BY symbol",
                                                       (run_id,))]
    for k in ("signals", "decisions", "ai_calls"):
        for x in out[k]:
            x.pop("details_json", None)
            x.pop("provenance_json", None)
    return out


def last_decisions(symbols: Optional[Iterable[str]] = None) -> Dict[str, dict]:
    """The newest decision per symbol that carries a judgement (llm / carried / legacy rules) — the change-detection
    memory after a restart (FUND §8.3; legacy rows after the switch-over, SP §22.8a)."""
    with db._connect() as c:
        rows = c.execute("SELECT d.*, r.started_at, r.params_version FROM cl_decisions d JOIN cl_runs r ON r.id = d.run_id "
                         "WHERE d.judge_source IN ('llm', 'carried', 'rules') ORDER BY d.id").fetchall()
    out: Dict[str, dict] = {}
    want = set(symbols) if symbols is not None else None
    for r in rows:
        if want is None or r["symbol"] in want:
            out[r["symbol"]] = dict(r)
    return out


_REC_SOURCES = ("llm", "carried", "rules")


def _rec(r) -> dict:
    """A decision as the moved judge's change detection expects a recommendation (FUND §8.3)."""
    return {"id": r["id"], "run_id": r["run_id"], "ts": r["started_at"], "symbol": r["symbol"], "action": r["action"],
            "confidence": r["confidence"], "target_weight": r["target_weight"], "rule_score": r["rule_score"],
            "rationale": r["rationale"], "source": r["judge_source"], "model": r["model"],
            "provenance": _l(r["provenance_json"])}


def latest_recommendations() -> Dict[str, dict]:
    """symbol -> newest decision with a recommendation (llm / carried / legacy rules)."""
    with db._connect() as c:
        rows = c.execute("SELECT d.*, r.started_at FROM cl_decisions d JOIN cl_runs r ON r.id = d.run_id WHERE d.id IN "
                         "(SELECT MAX(id) FROM cl_decisions WHERE judge_source IN ('llm', 'carried', 'rules') "
                         "GROUP BY symbol)").fetchall()
    return {r["symbol"]: _rec(r) for r in rows}


def last_judged(symbol: str) -> Optional[dict]:
    """Newest decision of the symbol with a fresh judgement (llm / legacy rules — not carried)."""
    with db._connect() as c:
        r = c.execute("SELECT d.*, r.started_at FROM cl_decisions d JOIN cl_runs r ON r.id = d.run_id WHERE d.symbol = ? "
                      "AND d.judge_source IN ('llm', 'rules') ORDER BY d.id DESC LIMIT 1", (symbol,)).fetchone()
    return _rec(r) if r else None


def agent_scores(run_id: int, symbol: Optional[str] = None) -> List[dict]:
    sql, args = "SELECT * FROM cl_signals WHERE run_id = ?", [int(run_id)]
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    with db._connect() as c:
        rows = c.execute(sql + " ORDER BY symbol, agent", args).fetchall()
    return [{"run_id": r["run_id"], "symbol": r["symbol"], "agent": r["agent"], "score": r["score"],
             "confidence": r["confidence"], "summary": r["summary"], "details": _l(r["details_json"]) or {},
             "source": r["source"], "provenance": _l(r["provenance_json"])} for r in rows]


def decision_params_of_run(run_id: int) -> Optional[dict]:
    """The decision-relevant params a run used (``summary.decision_params``; legacy runs: their frozen params)."""
    with db._connect() as c:
        r = c.execute("SELECT r.params_version, r.legacy, x.summary_json FROM cl_runs r LEFT JOIN cl_run_results x "
                      "ON x.run_id = r.id WHERE r.id = ?", (int(run_id),)).fetchone()
        if r is None:
            return None
        s = _l(r["summary_json"])
        if isinstance(s, dict) and isinstance(s.get("decision_params"), dict):
            return s["decision_params"]
        if r["params_version"] is not None:
            p = c.execute("SELECT params_json FROM cl_params WHERE version = ?", (r["params_version"],)).fetchone()
            return json.loads(p["params_json"]) if p else None
    return None


# --- meta -------------------------------------------------------------------------------------------------

def switchover() -> str:
    """'none' | 'pending' | 'done' | 'reverted' — raises StorageUnavailable (callers fail closed)."""
    v = db.meta_get(META_SWITCHOVER)
    if not v:
        return "none"
    try:
        st = json.loads(v).get("state")
    except (ValueError, AttributeError):
        st = None
    if st not in SWITCHOVER_STATES:
        raise db.StorageUnavailable("the Council switch-over state is unreadable", detail=str(v)[:100])
    return st


def orders_enabled() -> bool:
    """Council orders switch (default OFF; unreadable → OFF, fail closed)."""
    try:
        v = db.meta_get(META_ORDERS)
        return bool(v) and json.loads(v).get("enabled") is True
    except Exception:          # noqa: BLE001 — fail closed
        return False


def set_orders_enabled(enabled: bool, actor: str, cause: str, conn=None) -> None:
    value = _j({"enabled": bool(enabled), "actor": actor, "cause": cause,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if conn is not None:
        conn.execute("INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value = excluded.value, updated_at = excluded.updated_at", (META_ORDERS, value, db.utc_now_iso()))
        return
    db.meta_set(META_ORDERS, value)
