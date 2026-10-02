"""All SQLite access of the HedgeFund loop (fund/SPEC.md §2), built on
newsroom/db.py (same file data/newsroom.db, schema v3).

Proof of concept, paper trading only. Rules inherited from newsroom.db:
- every function is a safe no-op returning a neutral default while the store
  is disabled (db.init() not called / failed) and never raises into the
  caller (errors are logged);
- one connection per call; timestamps are UTC ISO-8601 strings;
- JSON columns are written with json.dumps(default=str) and decoded on read.
Stdlib only.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from newsroom import db

from . import params as fund_params
from .types import AgentSignal, Bar

logger = logging.getLogger(__name__)

_guard = db._guard
_connect = db._connect

META_AUTOPILOT = "fund.autopilot"
META_HALTED = "fund.halted"
META_PEAK_EQUITY = "fund.peak_equity"
META_RESET_AT = "fund.reset_at"
META_STARTING_CAPITAL = "fund.starting_capital"
META_LAST_ACCRUAL = "fund.last_accrual_date"
META_LAST_DIVIDEND = "fund.last_dividend_date"
META_AI_SPEND = "fund.ai_spend"
META_LAST_FUNDING = "fund.last_funding_at"     # UTC iso of the last perp funding window booked
META_CRYPTO_ROLLOUT = "fund.crypto_rollout"    # one-time watchlist migration done (iso timestamp)
META_BARS_FETCHED = "fund.bars_fetched:"       # + SYMBOL -> {"at": iso, "days": n}

ORDER_UPDATABLE = ("status", "qty", "fill_price", "fees", "filled_at", "reject_reason", "notional",
                   "rationale", "run_id", "recommendation_id", "provenance_json")


def now_iso() -> str:
    return db.utc_now_iso()


def _iso(ts) -> str:
    """datetime/str/None -> UTC ISO-8601 (seconds)."""
    if ts is None:
        return db.utc_now_iso()
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat(timespec="seconds")
    return db.normalise_timestamp(ts)


def _dumps(value) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


# --- params ------------------------------------------------------------------

@_guard(None)
def latest_params() -> Optional[dict]:
    """Newest stored params version: {"version", "created_at", "params",
    "actor", "note"} (params as stored, not merged) or None."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM fund_params ORDER BY version DESC LIMIT 1").fetchone()
    return _params_row(row)


@_guard(None)
def get_params(version: int) -> Optional[dict]:
    """One stored params version (same shape as latest_params) or None."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM fund_params WHERE version = ?", (int(version),)).fetchone()
    return _params_row(row)


def _params_row(row) -> Optional[dict]:
    if row is None:
        return None
    return {"version": row["version"], "created_at": row["created_at"], "params": _loads(row["params_json"]),
            "actor": row["actor"], "note": row["note"]}


@_guard(None)
def save_params(params: dict, actor: str, note: Optional[str] = None, created_at=None) -> Optional[int]:
    """Stores a full params dict as a new version (max + 1, starting at 1);
    returns the version. The caller validates/merges first (fund.params)."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        version = (conn.execute("SELECT COALESCE(MAX(version), 0) FROM fund_params").fetchone()[0] or 0) + 1
        conn.execute("INSERT INTO fund_params(version, created_at, params_json, actor, note) VALUES (?, ?, ?, ?, ?)",
                     (version, _iso(created_at), _dumps(params), str(actor), note))
    return version


def effective_params() -> Tuple[int, dict]:
    """(version, full params) of the latest version merged over DEFAULTS;
    (0, DEFAULTS copy) when nothing is stored or the store is disabled."""
    latest = latest_params()
    if not latest or not isinstance(latest.get("params"), dict):
        return 0, fund_params.defaults()
    return int(latest["version"]), fund_params.merge(latest["params"], None)


# --- runs --------------------------------------------------------------------

@_guard(None)
def create_run(started_at, params_version: int, trigger: str, market_open: bool,
               status: str = "running") -> Optional[int]:
    """Inserts a fund_runs row (trigger schedule|manual|startup); returns its id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO fund_runs(started_at, params_version, trigger, status, market_open) VALUES (?, ?, ?, ?, ?)",
            (_iso(started_at), int(params_version), str(trigger), str(status), 1 if market_open else 0))
        return cur.lastrowid


@_guard(False)
def finish_run(run_id: int, status: str, finished_at=None, judge_mode: Optional[str] = None,
               models: Optional[str] = None, cost_usd: float = 0.0, error: Optional[str] = None,
               summary: Optional[dict] = None) -> bool:
    """Completes a run (status ok|partial|error|skipped). ``models`` is a
    display string (e.g. comma-separated ids). ``summary`` is free JSON; put
    the symbols whose action flipped under summary["flips"] (list) — list_runs
    serves it as ``flips``."""
    if isinstance(models, (list, tuple)):
        models = ", ".join(str(m) for m in models if m)
    with _connect() as conn:
        conn.execute("UPDATE fund_runs SET finished_at = ?, status = ?, judge_mode = ?, models = ?, cost_usd = ?, "
                     "error = ?, summary_json = ? WHERE id = ?",
                     (_iso(finished_at), str(status), judge_mode, models, float(cost_usd or 0.0),
                      (str(error)[:2000] if error else None), _dumps(summary), int(run_id)))
    return True


def _run_row(r) -> dict:
    summary = _loads(r["summary_json"])
    return {"id": r["id"], "started_at": r["started_at"], "finished_at": r["finished_at"], "status": r["status"],
            "trigger": r["trigger"], "params_version": r["params_version"], "market_open": bool(r["market_open"]),
            "judge_mode": r["judge_mode"], "models": r["models"], "cost_usd": r["cost_usd"] or 0.0,
            "error": r["error"], "summary": summary}


@_guard(list)
def list_runs(limit: int = 50) -> List[dict]:
    """Newest first; each run dict + "recommendations" (count), "orders"
    (count) and "flips" (summary["flips"] or [])."""
    limit = max(1, min(int(limit), 500))
    with _connect() as conn:
        rows = conn.execute("""SELECT r.*,
                   (SELECT COUNT(*) FROM fund_recommendations x WHERE x.run_id = r.id) AS n_recs,
                   (SELECT COUNT(*) FROM orders o WHERE o.run_id = r.id) AS n_orders
               FROM fund_runs r ORDER BY r.started_at DESC, r.id DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        d = _run_row(r)
        d["recommendations"] = r["n_recs"]
        d["orders"] = r["n_orders"]
        flips = d["summary"].get("flips") if isinstance(d["summary"], dict) else None
        d["flips"] = flips if isinstance(flips, list) else []
        out.append(d)
    return out


@_guard(None)
def last_run() -> Optional[dict]:
    """The newest run dict (without counts) or None."""
    with _connect() as conn:
        r = conn.execute("SELECT * FROM fund_runs ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
    return _run_row(r) if r else None


@_guard(None)
def get_run(run_id: int) -> Optional[dict]:
    """{"run", "params" (stored params version dict or None), "agents",
    "recommendations", "orders"} or None when the run doesn't exist."""
    with _connect() as conn:
        r = conn.execute("SELECT * FROM fund_runs WHERE id = ?", (int(run_id),)).fetchone()
    if r is None:
        return None
    run = _run_row(r)
    return {"run": run, "params": get_params(run["params_version"]), "agents": agent_scores(run_id),
            "recommendations": list_recommendations(limit=1000, run_id=run_id),
            "orders": list_orders(limit=1000, run_id=run_id)}


# --- agent scores ------------------------------------------------------------

@_guard(0)
def save_agent_scores(run_id: int, signals: Iterable) -> int:
    """Stores AgentSignal objects (or dicts with the same fields) of one run;
    (run_id, symbol, agent) is unique — a repeat replaces. Returns the count."""
    rows = []
    for s in signals or []:
        d = s.to_dict() if isinstance(s, AgentSignal) else dict(s)
        rows.append((int(run_id), d["symbol"], d["agent"], float(d.get("score") or 0.0),
                     float(d.get("confidence") or 0.0), d.get("summary"), _dumps(d.get("details") or {}),
                     d.get("source") or "rules", _dumps(d.get("provenance"))))
    if not rows:
        return 0
    with _connect() as conn:
        conn.executemany("INSERT OR REPLACE INTO fund_agent_scores(run_id, symbol, agent, score, confidence, summary, "
                         "details_json, source, provenance_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


@_guard(list)
def agent_scores(run_id: int, symbol: Optional[str] = None) -> List[dict]:
    """[{run_id, symbol, agent, score, confidence, summary, details, source,
    provenance}] of one run (optionally one symbol), ordered by symbol, agent."""
    sql = "SELECT * FROM fund_agent_scores WHERE run_id = ?"
    args: tuple = (int(run_id),)
    if symbol:
        sql += " AND symbol = ?"
        args += (symbol,)
    with _connect() as conn:
        rows = conn.execute(sql + " ORDER BY symbol, agent", args).fetchall()
    return [{"run_id": r["run_id"], "symbol": r["symbol"], "agent": r["agent"], "score": r["score"],
             "confidence": r["confidence"], "summary": r["summary"], "details": _loads(r["details_json"]) or {},
             "source": r["source"], "provenance": _loads(r["provenance_json"])} for r in rows]


# --- recommendations ---------------------------------------------------------

def _rec_row(r) -> dict:
    return {"id": r["id"], "run_id": r["run_id"], "ts": r["ts"], "symbol": r["symbol"], "action": r["action"],
            "confidence": r["confidence"], "target_weight": r["target_weight"], "rule_score": r["rule_score"],
            "rationale": r["rationale"], "source": r["source"], "model": r["model"],
            "provenance": _loads(r["provenance_json"]), "usage": _loads(r["usage_json"]), "status": r["status"],
            "skip_reason": r["skip_reason"], "order_id": r["order_id"]}


@_guard(None)
def save_recommendation(run_id: int, ts, symbol: str, action: str, confidence: float, target_weight: float,
                        rule_score: Optional[float] = None, rationale: Optional[str] = None, source: str = "rules",
                        model: Optional[str] = None, provenance: Optional[dict] = None,
                        usage: Optional[dict] = None, status: str = "open",
                        supersede: bool = True) -> Optional[int]:
    """Stores a recommendation (action LONG|SHORT|FLAT, source llm|rules|carried,
    target_weight signed fraction of equity); returns its id. supersede=True
    marks earlier 'open' AND 'skipped' recommendations of the symbol
    'superseded', so only the newest recommendation per symbol is executable
    (a manual accept of an old skipped row can't act on stale reasoning).
    Both statements run in one transaction."""
    with _connect() as conn:
        if supersede:
            conn.execute("UPDATE fund_recommendations SET status = 'superseded' "
                         "WHERE symbol = ? AND status IN ('open', 'skipped')", (symbol,))
        cur = conn.execute(
            "INSERT INTO fund_recommendations(run_id, ts, symbol, action, confidence, target_weight, rule_score, "
            "rationale, source, model, provenance_json, usage_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(run_id), _iso(ts), symbol, action, float(confidence), float(target_weight),
             None if rule_score is None else float(rule_score), rationale, source, model,
             _dumps(provenance), _dumps(usage), status))
        return cur.lastrowid


@_guard(False)
def update_recommendation_status(rec_id: int, status: str, skip_reason: Optional[str] = None,
                                 order_id: Optional[int] = None) -> bool:
    """status open|executed|skipped|superseded; order_id links the (first) order."""
    with _connect() as conn:
        n = conn.execute("UPDATE fund_recommendations SET status = ?, skip_reason = ?, "
                         "order_id = COALESCE(?, order_id) WHERE id = ?",
                         (status, skip_reason, order_id, int(rec_id))).rowcount
    return n > 0


@_guard(None)
def latest_recommendation_id(symbol: str) -> Optional[int]:
    """Id of the newest recommendation of the symbol (any status) or None
    (none stored, store disabled or failed). The engine may compare it with
    a recommendation it is about to execute: only the latest is current."""
    with _connect() as conn:
        r = conn.execute("SELECT MAX(id) FROM fund_recommendations WHERE symbol = ?", (symbol,)).fetchone()
    return int(r[0]) if r and r[0] is not None else None


@_guard(None)
def get_recommendation(rec_id: int) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM fund_recommendations WHERE id = ?", (int(rec_id),)).fetchone()
    return _rec_row(r) if r else None


@_guard(dict)
def latest_recommendations() -> Dict[str, dict]:
    """symbol -> newest recommendation (any source/status)."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM fund_recommendations WHERE id IN "
                            "(SELECT MAX(id) FROM fund_recommendations GROUP BY symbol)").fetchall()
    return {r["symbol"]: _rec_row(r) for r in rows}


@_guard(None)
def last_judged_recommendation(symbol: str) -> Optional[dict]:
    """Newest recommendation of the symbol with source llm or rules (i.e. not
    carried) — the reference for the judge's change detection."""
    with _connect() as conn:
        r = conn.execute("SELECT * FROM fund_recommendations WHERE symbol = ? AND source IN ('llm', 'rules') "
                         "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
    return _rec_row(r) if r else None


@_guard(list)
def list_recommendations(limit: int = 500, run_id: Optional[int] = None, symbol: Optional[str] = None) -> List[dict]:
    """Newest first (by id), optionally one run / one symbol (decisions export)."""
    limit = max(1, min(int(limit), 100000))
    sql, args = "SELECT * FROM fund_recommendations WHERE 1=1", ()
    if run_id is not None:
        sql += " AND run_id = ?"
        args += (int(run_id),)
    if symbol:
        sql += " AND symbol = ?"
        args += (symbol,)
    with _connect() as conn:
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", args + (limit,)).fetchall()
    return [_rec_row(r) for r in rows]


# --- orders ------------------------------------------------------------------

def _order_row(r) -> dict:
    d = dict(r)
    d["provenance"] = _loads(d.pop("provenance_json", None))
    return d


@_guard(None)
def insert_order(ts, symbol: str, side: str, qty: float, status: str, origin: str,
                 run_id: Optional[int] = None, recommendation_id: Optional[int] = None,
                 rationale: Optional[str] = None, notional: Optional[float] = None,
                 fill_price: Optional[float] = None, fees: Optional[float] = None, filled_at=None,
                 reject_reason: Optional[str] = None, provenance: Optional[dict] = None) -> Optional[int]:
    """Records a paper market order (mode 'paper', broker 'paper'); side
    buy|sell|short|cover, status pending|filled|rejected|cancelled. Returns id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO orders(ts, symbol, side, qty, order_type, status, broker, mode, rationale, provenance_json, "
            "run_id, recommendation_id, origin, fill_price, fees, filled_at, reject_reason, notional) "
            "VALUES (?, ?, ?, ?, 'market', ?, 'paper', 'paper', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_iso(ts), symbol, side, float(qty), status, rationale, _dumps(provenance), run_id, recommendation_id,
             origin, fill_price, fees, None if filled_at is None else _iso(filled_at), reject_reason, notional))
        return cur.lastrowid


@_guard(False)
def update_order(order_id: int, **fields) -> bool:
    """Updates whitelisted columns (ORDER_UPDATABLE; 'provenance' is accepted
    and serialised). Unknown fields raise -> logged, returns False."""
    if "provenance" in fields:
        fields["provenance_json"] = _dumps(fields.pop("provenance"))
    if "filled_at" in fields and fields["filled_at"] is not None:
        fields["filled_at"] = _iso(fields["filled_at"])
    bad = [k for k in fields if k not in ORDER_UPDATABLE]
    if bad:
        raise ValueError(f"not updatable: {bad}")
    if not fields:
        return False
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as conn:
        n = conn.execute(f"UPDATE orders SET {cols} WHERE id = ?", tuple(fields.values()) + (int(order_id),)).rowcount
    return n > 0


@_guard(None)
def get_order(order_id: int) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM orders WHERE id = ?", (int(order_id),)).fetchone()
    return _order_row(r) if r else None


@_guard(list)
def list_orders(limit: int = 100, run_id: Optional[int] = None) -> List[dict]:
    """Newest first; every orders column (provenance decoded as "provenance")."""
    limit = max(1, min(int(limit), 100000))
    sql, args = "SELECT * FROM orders", ()
    if run_id is not None:
        sql += " WHERE run_id = ?"
        args = (int(run_id),)
    with _connect() as conn:
        rows = conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?", args + (limit,)).fetchall()
    return [_order_row(r) for r in rows]


@_guard(list)
def pending_orders(symbol: Optional[str] = None) -> List[dict]:
    """Orders with status 'pending', oldest first (optionally one symbol)."""
    sql, args = "SELECT * FROM orders WHERE status = 'pending'", ()
    if symbol:
        sql += " AND symbol = ?"
        args = (symbol,)
    with _connect() as conn:
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    return [_order_row(r) for r in rows]


@_guard(list)
def orders_on_trading_day(day: str, origin: Optional[str] = None,
                          statuses: Tuple[str, ...] = ("filled", "pending")) -> List[dict]:
    """Orders that belong to NYSE trading day ``day`` ('YYYY-MM-DD'),
    optionally one origin, with a status in ``statuses``. An order belongs to
    market.trading_day(filled_at or ts): a fill during the session counts for
    that day, an order queued while the market is closed (weekend, night)
    counts for the session it will execute in (SPEC §9 max_trades_per_day)."""
    from . import market  # stdlib-only module; imported here to keep store's import light
    d = date.fromisoformat(str(day)[:10])
    lo = (d - timedelta(days=7)).isoformat()      # long weekends / holidays before the session
    hi = (d + timedelta(days=2)).isoformat()
    marks = ",".join("?" for _ in statuses)
    sql = f"SELECT * FROM orders WHERE ts >= ? AND ts < ? AND status IN ({marks})"
    args: tuple = (lo, hi) + tuple(statuses)
    if origin:
        sql += " AND origin = ?"
        args += (origin,)
    with _connect() as conn:
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    out = []
    for r in rows:
        try:
            if market.trading_day(r["filled_at"] or r["ts"]) == d.isoformat():
                out.append(_order_row(r))
        except ValueError:
            continue
    return out


@_guard(list)
def orders_on_utc_day(day: str, origin: Optional[str] = None,
                      statuses: Tuple[str, ...] = ("filled", "pending")) -> List[dict]:
    """Orders whose UTC date of (filled_at or ts) is ``day`` ('YYYY-MM-DD'),
    optionally one origin. Crypto trades 24/7, so its autopilot orders are
    counted per UTC day (SPEC §9 max_trades_per_day), not per NY trading day."""
    d = date.fromisoformat(str(day)[:10])
    lo = (d - timedelta(days=1)).isoformat()
    hi = (d + timedelta(days=2)).isoformat()
    marks = ",".join("?" for _ in statuses)
    sql = f"SELECT * FROM orders WHERE ts >= ? AND ts < ? AND status IN ({marks})"
    args: tuple = (lo, hi) + tuple(statuses)
    if origin:
        sql += " AND origin = ?"
        args += (origin,)
    with _connect() as conn:
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    return [_order_row(r) for r in rows if _utc_day(r["filled_at"] or r["ts"]) == d.isoformat()]


# --- positions ---------------------------------------------------------------

def _pos_row(r) -> dict:
    return {"symbol": r["symbol"], "qty": r["qty"], "avg_price": r["avg_price"], "updated_at": r["updated_at"],
            "opened_at": r["opened_at"], "realized_pnl": r["realized_pnl"] or 0.0,
            "borrow_rate_pct": r["borrow_rate_pct"], "last_price": r["last_price"]}


@_guard(list)
def get_positions(include_flat: bool = False) -> List[dict]:
    """Position rows ordered by symbol; flat rows (qty 0, kept for their
    realized P&L) only with include_flat=True."""
    sql = "SELECT * FROM positions" + ("" if include_flat else " WHERE qty != 0") + " ORDER BY symbol"
    with _connect() as conn:
        return [_pos_row(r) for r in conn.execute(sql).fetchall()]


@_guard(None)
def get_position(symbol: str) -> Optional[dict]:
    """One position row (possibly flat) or None."""
    with _connect() as conn:
        r = conn.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,)).fetchone()
    return _pos_row(r) if r else None


@_guard(False)
def upsert_position(symbol: str, qty: float, avg_price: Optional[float], updated_at=None, opened_at=None,
                    realized_pnl: float = 0.0, borrow_rate_pct: Optional[float] = None,
                    last_price: Optional[float] = None) -> bool:
    """Writes the full position row (qty < 0 = short)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO positions(symbol, qty, avg_price, updated_at, opened_at, realized_pnl, borrow_rate_pct, "
            "last_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET qty = excluded.qty, "
            "avg_price = excluded.avg_price, updated_at = excluded.updated_at, opened_at = excluded.opened_at, "
            "realized_pnl = excluded.realized_pnl, borrow_rate_pct = excluded.borrow_rate_pct, "
            "last_price = excluded.last_price",
            (symbol, float(qty), avg_price, _iso(updated_at), None if opened_at is None else _iso(opened_at),
             float(realized_pnl or 0.0), borrow_rate_pct, last_price))
    return True


@_guard(0)
def set_last_prices(prices: Dict[str, float], updated_at=None) -> int:
    """Marks positions to market: last_price per symbol (existing rows only)."""
    rows = [(float(p), s) for s, p in (prices or {}).items() if p is not None]
    if not rows:
        return 0
    with _connect() as conn:
        conn.executemany("UPDATE positions SET last_price = ? WHERE symbol = ?", rows)
    return len(rows)


@_guard(False)
def set_borrow_rate(symbol: str, rate_pct: Optional[float]) -> bool:
    with _connect() as conn:
        conn.execute("UPDATE positions SET borrow_rate_pct = ? WHERE symbol = ?", (rate_pct, symbol))
    return True


@_guard(False)
def delete_position(symbol: str) -> bool:
    with _connect() as conn:
        return conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,)).rowcount > 0


@_guard(0)
def clear_positions() -> int:
    """Removes every position row (paper reset only). Returns the count."""
    with _connect() as conn:
        return conn.execute("DELETE FROM positions").rowcount


# --- cash ledger -------------------------------------------------------------

@_guard(None)
def add_cash_entry(ts, kind: str, amount: float, symbol: Optional[str] = None,
                   note: Optional[str] = None) -> Optional[int]:
    """kind deposit|reset|fill|fee|borrow_fee|dividend|dividend_short|funding|
    liquidation_fee; amount signed (+ = cash in). Returns the id."""
    with _connect() as conn:
        cur = conn.execute("INSERT INTO fund_cash_ledger(ts, kind, symbol, amount, note) VALUES (?, ?, ?, ?, ?)",
                           (_iso(ts), kind, symbol, float(amount), note))
        return cur.lastrowid


@_guard(None)
def cash_balance() -> Optional[float]:
    """Cash = sum of the ledger (includes short-sale proceeds). None when the
    state is unreadable (store disabled or a SQLite error) — never 0.0 as a
    stand-in: a fake zero would collapse equity and could trigger the
    drawdown stop. Callers must treat None as "state unavailable"."""
    with _connect() as conn:
        return float(conn.execute("SELECT COALESCE(SUM(amount), 0) FROM fund_cash_ledger").fetchone()[0] or 0.0)


@_guard(None)
def account_snapshot() -> Optional[dict]:
    """{"cash": float, "positions": [position rows, qty != 0],
    "ledger_rows": int} read in ONE connection (consistent), or None when the
    state is unreadable (store disabled or a SQLite error). The broker's
    equity maths uses this so that a transient failure can't turn into
    "cash 0" or "no positions"."""
    with _connect() as conn:
        n, cash = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM fund_cash_ledger").fetchone()
        rows = conn.execute("SELECT * FROM positions WHERE qty != 0 ORDER BY symbol").fetchall()
    return {"cash": float(cash or 0.0), "positions": [_pos_row(r) for r in rows], "ledger_rows": int(n or 0)}


@_guard(None)
def booked_dividend_keys(since_day: Optional[str] = None) -> Optional[set]:
    """{(symbol, ex_date 'YYYY-MM-DD')} of dividend / dividend_short ledger
    rows (dedupe key of the dividend accrual). The key is read from the
    note, which the broker always writes as "ex YYYY-MM-DD: ..." (the format
    used since the first version, so older rows dedupe too). ``since_day``
    limits to ex-dates >= that day. None when the ledger is unreadable — the
    caller must then book nothing (double booking is worse than a delay)."""
    with _connect() as conn:
        rows = conn.execute("SELECT symbol, note FROM fund_cash_ledger "
                            "WHERE kind IN ('dividend', 'dividend_short')").fetchall()
    out = set()
    for r in rows:
        note = r["note"] or ""
        if not note.startswith("ex ") or len(note) < 13:
            continue
        day = note[3:13]
        if since_day is None or day >= str(since_day)[:10]:
            out.add((r["symbol"], day))
    return out


@_guard(None)
def booked_funding_keys(since=None) -> Optional[set]:
    """{(symbol, "YYYY-MM-DDTHH")} of booked perp funding windows (ledger
    kind 'funding', note "fw YYYY-MM-DDTHH: ..."), rows with ts >= since.
    None when unreadable — the caller then books nothing (no double booking)."""
    sql, args = "SELECT symbol, note FROM fund_cash_ledger WHERE kind = 'funding'", ()
    if since is not None:
        sql += " AND ts >= ?"
        args = (_iso(since),)
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = set()
    for r in rows:
        note = r["note"] or ""
        if note.startswith("fw ") and len(note) >= 16:
            out.add((r["symbol"], note[3:16]))
    return out


@_guard(None)
def net_filled_qty_since(symbol: str, since) -> Optional[float]:
    """Signed share change of the symbol from fills with filled_at >= since
    (buy/cover +, sell/short -); None when unreadable. Used to reconstruct
    the quantity held at an ex-dividend date: held_then = held_now - this."""
    with _connect() as conn:
        r = conn.execute("SELECT COALESCE(SUM(CASE WHEN side IN ('buy', 'cover') THEN qty ELSE -qty END), 0) "
                         "FROM orders WHERE symbol = ? AND status = 'filled' AND filled_at >= ?",
                         (symbol, _iso(since))).fetchone()
    return float(r[0] or 0.0)


@_guard(None)
def ledger_sum(kind: str, since=None) -> Optional[float]:
    """Signed sum of the ledger rows of one kind (optionally ts >= since);
    None when unreadable."""
    sql, args = "SELECT COALESCE(SUM(amount), 0) FROM fund_cash_ledger WHERE kind = ?", (str(kind),)
    if since is not None:
        sql += " AND ts >= ?"
        args += (_iso(since),)
    with _connect() as conn:
        return float(conn.execute(sql, args).fetchone()[0] or 0.0)


@_guard(list)
def cash_entries(limit: int = 100, kind: Optional[str] = None, since=None) -> List[dict]:
    """Newest first."""
    limit = max(1, min(int(limit), 100000))
    sql, args = "SELECT * FROM fund_cash_ledger WHERE 1=1", ()
    if kind:
        sql += " AND kind = ?"
        args += (kind,)
    if since is not None:
        sql += " AND ts >= ?"
        args += (_iso(since),)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", args + (limit,)).fetchall()]


# --- equity curve ------------------------------------------------------------

@_guard(False)
def add_equity_point(ts, equity: float, cash: float, long_mv: float, short_mv: float,
                     benchmark: Optional[float] = None, drawdown_pct: Optional[float] = None) -> bool:
    """One snapshot (ts unique; a repeat replaces). benchmark = SPY price."""
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO equity_curve(ts, equity, cash, mode, long_mv, short_mv, benchmark, "
                     "drawdown_pct) VALUES (?, ?, ?, 'paper', ?, ?, ?, ?)",
                     (_iso(ts), float(equity), float(cash), float(long_mv), float(short_mv), benchmark, drawdown_pct))
    return True


def _eq_row(r) -> dict:
    return {"ts": r["ts"], "equity": r["equity"], "cash": r["cash"], "long_mv": r["long_mv"],
            "short_mv": r["short_mv"], "benchmark": r["benchmark"], "drawdown_pct": r["drawdown_pct"]}


@_guard(list)
def equity_points(since=None, limit: int = 20000) -> List[dict]:
    """Oldest first; since = UTC ISO/datetime (inclusive)."""
    limit = max(1, min(int(limit), 200000))
    sql, args = "SELECT * FROM equity_curve WHERE mode = 'paper'", ()
    if since is not None:
        sql += " AND ts >= ?"
        args = (_iso(since),)
    with _connect() as conn:
        rows = conn.execute(f"SELECT * FROM ({sql} ORDER BY ts DESC LIMIT ?) ORDER BY ts", args + (limit,)).fetchall()
    return [_eq_row(r) for r in rows]


@_guard(None)
def equity_point_before(ts) -> Optional[dict]:
    """The newest snapshot strictly before ts (day P&L reference) or None."""
    with _connect() as conn:
        r = conn.execute("SELECT * FROM equity_curve WHERE mode = 'paper' AND ts < ? ORDER BY ts DESC LIMIT 1",
                         (_iso(ts),)).fetchone()
    return _eq_row(r) if r else None


# --- events / audit / backtests ----------------------------------------------

@_guard(None)
def add_event(kind: str, title: str, symbol: Optional[str] = None, body: Optional[str] = None,
              ts=None) -> Optional[int]:
    """kind flip|order|halt|margin_call|resume|error|autopilot. Returns id."""
    with _connect() as conn:
        return conn.execute("INSERT INTO fund_events(ts, kind, symbol, title, body) VALUES (?, ?, ?, ?, ?)",
                            (_iso(ts), kind, symbol, title, body)).lastrowid


@_guard(list)
def events_after(after_id: int = 0, limit: int = 50) -> List[dict]:
    """Events with id > after_id, oldest first, at most ``limit`` (max 500)."""
    limit = max(1, min(int(limit), 500))
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM fund_events WHERE id > ? ORDER BY id LIMIT ?",
                            (int(after_id or 0), limit)).fetchall()
    return [dict(r) for r in rows]


@_guard(None)
def add_audit(actor: str, action: str, detail=None, ts=None) -> Optional[int]:
    """actor user|engine|system; detail any JSON-able value. Returns the new
    row id (int > 0), or None when the row could NOT be written (store
    disabled or a SQLite error). Security by default: callers refuse a
    user-initiated state change when this returns None (no unaudited
    actions)."""
    with _connect() as conn:
        return conn.execute("INSERT INTO fund_audit(ts, actor, action, detail_json) VALUES (?, ?, ?, ?)",
                            (_iso(ts), actor, action, _dumps(detail))).lastrowid


@_guard(list)
def list_audit(limit: int = 100) -> List[dict]:
    """Newest first: [{id, ts, actor, action, detail}]."""
    limit = max(1, min(int(limit), 10000))
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM fund_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
             "detail": _loads(r["detail_json"])} for r in rows]


@_guard(None)
def save_backtest(request: dict, result: dict, created_at=None) -> Optional[int]:
    with _connect() as conn:
        return conn.execute("INSERT INTO fund_backtests(created_at, request_json, result_json) VALUES (?, ?, ?)",
                            (_iso(created_at), _dumps(request), _dumps(result))).lastrowid


@_guard(list)
def list_backtests(limit: int = 10) -> List[dict]:
    """Newest first: [{id, created_at, request, summary}] with summary =
    result["portfolio"] (full result via get_backtest)."""
    limit = max(1, min(int(limit), 200))
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM fund_backtests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        result = _loads(r["result_json"])
        out.append({"id": r["id"], "created_at": r["created_at"], "request": _loads(r["request_json"]),
                    "summary": result.get("portfolio") if isinstance(result, dict) else None})
    return out


@_guard(None)
def get_backtest(backtest_id: int) -> Optional[dict]:
    with _connect() as conn:
        r = conn.execute("SELECT * FROM fund_backtests WHERE id = ?", (int(backtest_id),)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "created_at": r["created_at"], "request": _loads(r["request_json"]),
            "result": _loads(r["result_json"])}


# --- price cache (prices table, source "yfinance") ---------------------------

@_guard(0)
def save_bars(symbol: str, bars: Iterable[Bar], source: str = "yfinance") -> int:
    """Upserts daily bars; ts = '<date>T00:00:00+00:00' (the exchange date)."""
    rows = [(symbol, f"{b.date[:10]}T00:00:00+00:00", b.open, b.high, b.low, b.close, b.volume, source)
            for b in bars or []]
    if not rows:
        return 0
    with _connect() as conn:
        conn.executemany("INSERT OR REPLACE INTO prices(symbol, ts, open, high, low, close, volume, source) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


@_guard(list)
def load_bars(symbol: str, limit: int = 400, source: str = "yfinance") -> List[Bar]:
    """The newest ``limit`` cached daily bars, oldest first."""
    limit = max(1, int(limit))
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM (SELECT * FROM prices WHERE symbol = ? AND source = ? ORDER BY ts DESC "
                            "LIMIT ?) ORDER BY ts", (symbol, source, limit)).fetchall()
    return [Bar(date=r["ts"][:10], open=r["open"], high=r["high"], low=r["low"], close=r["close"],
                volume=r["volume"] or 0.0) for r in rows if r["close"] is not None]


def bars_fetched(symbol: str) -> Optional[dict]:
    """{"at": iso, "days": n} of the last successful bar download or None."""
    v = _loads(db.meta_get(META_BARS_FETCHED + symbol))
    return v if isinstance(v, dict) else None


def set_bars_fetched(symbol: str, at, days: int) -> bool:
    return db.meta_set(META_BARS_FETCHED + symbol, _dumps({"at": _iso(at), "days": int(days)}))


# --- state (meta keys "fund.*", SPEC §2) --------------------------------------

def _float_meta(key: str) -> Optional[float]:
    v = db.meta_get(key)
    try:
        return None if v is None else float(v)
    except ValueError:
        return None


def get_autopilot() -> bool:
    return db.meta_get(META_AUTOPILOT) == "1"


def set_autopilot(enabled: bool) -> bool:
    """Persists "1"/"0" (the engine forces "0" at every backend start)."""
    return db.meta_set(META_AUTOPILOT, "1" if enabled else "0")


def get_halted() -> dict:
    """{"active": bool, "reason": str|None, "at": iso|None}."""
    v = _loads(db.meta_get(META_HALTED))
    if not isinstance(v, dict):
        return {"active": False, "reason": None, "at": None}
    return {"active": bool(v.get("active")), "reason": v.get("reason"), "at": v.get("at")}


def set_halted(active: bool, reason: Optional[str] = None, at=None) -> bool:
    return db.meta_set(META_HALTED, _dumps({"active": bool(active), "reason": reason if active else None,
                                            "at": _iso(at) if active else None}))


def get_peak_equity() -> Optional[float]:
    return _float_meta(META_PEAK_EQUITY)


def set_peak_equity(value: float) -> bool:
    return db.meta_set(META_PEAK_EQUITY, repr(float(value)))


def get_reset_at() -> Optional[str]:
    return db.meta_get(META_RESET_AT)


def set_reset_at(ts) -> bool:
    return db.meta_set(META_RESET_AT, _iso(ts))


def get_starting_capital() -> Optional[float]:
    return _float_meta(META_STARTING_CAPITAL)


def set_starting_capital(value: float) -> bool:
    return db.meta_set(META_STARTING_CAPITAL, repr(float(value)))


def get_last_accrual_date() -> Optional[str]:
    return db.meta_get(META_LAST_ACCRUAL)


def set_last_accrual_date(day: str) -> bool:
    """day = NY date 'YYYY-MM-DD' up to which borrow fees are booked."""
    return db.meta_set(META_LAST_ACCRUAL, str(day)[:10])


def get_last_dividend_date() -> Optional[str]:
    return db.meta_get(META_LAST_DIVIDEND)


def set_last_dividend_date(day: str) -> bool:
    return db.meta_set(META_LAST_DIVIDEND, str(day)[:10])


def get_last_funding_at() -> Optional[str]:
    return db.meta_get(META_LAST_FUNDING)


def set_last_funding_at(ts) -> bool:
    """UTC timestamp of the last perp funding window booked (SPEC §6)."""
    return db.meta_set(META_LAST_FUNDING, _iso(ts))


def get_crypto_rollout() -> Optional[str]:
    return db.meta_get(META_CRYPTO_ROLLOUT)


def set_crypto_rollout(ts) -> bool:
    return db.meta_set(META_CRYPTO_ROLLOUT, _iso(ts))


def _utc_day(now=None) -> str:
    dt = now if isinstance(now, datetime) else (datetime.fromisoformat(_iso(now)) if now else datetime.now(timezone.utc))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).date().isoformat()


@_guard(None)
def ai_spend_today(now=None) -> Optional[float]:
    """USD spent on OpenRouter by the fund on the current UTC day (0.0 when
    nothing was booked today). None when the spend is UNKNOWN (store
    disabled, SQLite error, unreadable value) — the budget must then fail
    closed (no LLM call), never treat unknown as 0."""
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_AI_SPEND,)).fetchone()
    if row is None:
        return 0.0
    v = _loads(row[0])
    if not isinstance(v, dict):
        return None
    if v.get("day") != _utc_day(now):
        return 0.0
    try:
        usd = float(v.get("usd") or 0.0)
    except (TypeError, ValueError):
        return None
    return usd if usd == usd and usd >= 0 else None


@_guard(None)
def add_ai_spend(usd: float, now=None) -> Optional[float]:
    """Adds a call's cost to the UTC-day total (atomic read-modify-write; a
    new day starts at 0). Returns the new total, or None when it could not
    be booked (store disabled / SQLite error) — the budget should then stop
    spending for the day."""
    day = _utc_day(now)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (META_AI_SPEND,)).fetchone()
        cur = _loads(row[0]) if row else None
        total = float(cur.get("usd") or 0.0) if isinstance(cur, dict) and cur.get("day") == day else 0.0
        total += max(0.0, float(usd or 0.0))
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (META_AI_SPEND, _dumps({"day": day, "usd": total})))
    return total
