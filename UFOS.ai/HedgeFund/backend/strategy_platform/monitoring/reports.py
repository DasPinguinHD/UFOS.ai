"""Daily / weekly reports (SPEC §19.4) — numbers first (deterministic), optional AI commentary (labelled, numbers
only, budget key ``reports``) — and the shared commentary call used by the Shadow Account (§19.8).

Periods are UTC: ``daily`` = one calendar date, ``weekly`` = an ISO week (Monday–Sunday). The scheduler
(``due``) creates the previous day's report after 00:30 UTC and the previous week's after Monday 00:30 UTC (once
each, ``created_by: engine``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ..execution.errors import TradingError
from ..storage import db
from . import alerts, decay, expected

logger = logging.getLogger(__name__)

PERIODS = ("daily", "weekly")
SCHEDULE_DELAY = timedelta(minutes=30)
FLOW_KINDS = ("deposit", "reset", "broker_adjustment", "migration")
FEE_KINDS = ("fee", "liquidation_fee")
FEATURE = "sp-report"
FEATURE_LABEL = "Hedge Fund · Report commentary"
BUDGET_KEY = "reports"
TEMPERATURE = 0.2
MAX_TOKENS = 1500
TIMEOUT_SECONDS = 90
TIERS = ("standard", "premium")
LANGUAGES = {"en": "English", "de": "German"}
NOT_FOUND = "No such report."
_busy = threading.Lock()


# --- periods ---------------------------------------------------------------------------------------------------

def bounds(period: str, day: date) -> Tuple[str, datetime, datetime]:
    if period == "daily":
        s = datetime.combine(day, time(0), tzinfo=timezone.utc)
        return day.isoformat(), s, s + timedelta(days=1)
    monday = day - timedelta(days=day.weekday())
    s = datetime.combine(monday, time(0), tzinfo=timezone.utc)
    y, w, _ = monday.isocalendar()
    return f"{y}-W{w:02d}", s, s + timedelta(days=7)


def last_complete(period: str, now: datetime) -> date:
    if period == "daily":
        return (now - timedelta(days=1)).date()
    return (now - timedelta(days=now.weekday() + 7)).date()


# --- numbers ---------------------------------------------------------------------------------------------------

def _account_numbers(account_id: str, s: str, e: str) -> dict:
    with db._connect() as conn:
        start = conn.execute("SELECT equity FROM pa_equity WHERE account_id = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                             (account_id, s)).fetchone()
        pts = [float(r[0]) for r in conn.execute("SELECT equity FROM pa_equity WHERE account_id = ? AND ts >= ? AND"
                                                 " ts < ? ORDER BY ts", (account_id, s, e)).fetchall()]
        flows = conn.execute(f"SELECT COALESCE(SUM(amount), 0) FROM pa_cash_ledger WHERE account_id = ? AND ts >= ? AND"
                             f" ts < ? AND kind IN ({','.join('?' * len(FLOW_KINDS))})",
                             (account_id, s, e, *FLOW_KINDS)).fetchone()[0]
        fees = conn.execute(f"SELECT COALESCE(SUM(amount), 0) FROM pa_cash_ledger WHERE account_id = ? AND ts >= ? AND"
                            f" ts < ? AND kind IN ({','.join('?' * len(FEE_KINDS))})",
                            (account_id, s, e, *FEE_KINDS)).fetchone()[0]
        orders = {r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM oms_orders WHERE account_id = ? AND"
                                                   " created_at >= ? AND created_at < ? GROUP BY state",
                                                   (account_id, s, e)).fetchall()}
    eq0 = float(start[0]) if start else (pts[0] if pts else None)
    eq1 = pts[-1] if pts else eq0
    pnl = (eq1 - eq0 - float(flows)) if eq0 is not None and eq1 is not None else None
    peak, mdd = None, 0.0
    for x in ([eq0] if eq0 is not None else []) + pts:
        peak = x if peak is None else max(peak, x)
        if peak and peak > 0:
            mdd = max(mdd, (peak - x) / peak)
    halts = sum(1 for a in alerts.in_period(s, e) if a["account_id"] == account_id and a["kind"] in
                ("drawdown_kill", "kill_switch", "reconciliation_halt", "daily_loss_limit"))
    return {"account_id": account_id, "paper": True, "equity_start": eq0, "equity_end": eq1, "pnl": pnl,
            "return_pct": (pnl / eq0 * 100.0) if pnl is not None and eq0 else None,
            "max_drawdown_pct": mdd * 100.0 if pts else None, "fees": -float(fees),
            "orders": {"total": sum(orders.values()), "filled": orders.get("filled", 0),
                       "rejected": orders.get("rejected", 0), "cancelled": orders.get("cancelled", 0)},
            "halts": halts}


def _strategy_numbers(s: str, e: str, now: datetime) -> List[dict]:
    from ..live import incubation
    out = []
    for i, run in enumerate(incubation.active_runs(), start=1):
        exp = expected.basis(run)
        last = expected.last_completed(run, now)
        series = expected.realised(run, last) if last is not None else []
        c = expected.session_counts(series, s[:10], e[:10])
        n, lr = c["n"], c["log_return"]
        row = {"label": f"strategy {i}", "strategy_id": run["strategy_id"],
               "name": (db.strategy_get(run["strategy_id"]) or {}).get("name"), "run_id": run["id"], "sessions": n,
               "log_return": lr if n else None, "expected_mean": None, "band_lo": None, "band_hi": None, "z": None,
               "decay_state": decay.monitor_view(run, now)["decay"]["state"]}
        if exp and n:
            mu, sd = exp["mean_daily"], exp["std_daily"]
            row.update({"expected_mean": n * mu, "band_lo": n * mu - 1.96 * sd * math.sqrt(n),
                        "band_hi": n * mu + 1.96 * sd * math.sqrt(n),
                        "z": (lr - n * mu) / (sd * math.sqrt(n)) if sd > 0 else None})
        out.append(row)
    return out


def _brokers(e: str) -> List[dict]:
    with db._connect() as conn:
        rows = conn.execute("SELECT r.account_id, r.status, r.ts FROM br_reconciliations r JOIN (SELECT account_id,"
                            " MAX(id) AS m FROM br_reconciliations WHERE ts < ? GROUP BY account_id) x ON r.id = x.m",
                            (e,)).fetchall()
    return [{"account_id": r["account_id"], "status": r["status"], "at": r["ts"]} for r in rows]


def _ts(v) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def accounts_in(s: str, e: str) -> List[dict]:
    """The accounts that existed during [s, e) (click-test 2026-10-22): an account opened at or after the period
    end is left out; one opened inside the period is marked ``opened`` (partial period)."""
    from ..execution import accounts
    ds, de = _ts(s), _ts(e)
    out = []
    for r in accounts.rows():
        opened = _ts(r.get("created_at"))
        if opened is not None and opened >= de:
            continue
        out.append({"id": r["id"], "opened": r.get("created_at") if opened is not None and opened > ds else None})
    return out


def numbers(s: str, e: str, now: datetime) -> dict:
    accs = []
    for a in accounts_in(s, e):
        n = _account_numbers(a["id"], s, e)
        n["opened"] = a["opened"]
        n["partial"] = a["opened"] is not None
        n["note"] = f"opened {a['opened']} — partial period" if a["opened"] else None
        accs.append(n)
    al = alerts.in_period(s, e)
    by: Dict[str, int] = {}
    for a in al:
        by[a["kind"]] = by.get(a["kind"], 0) + 1
    return {"accounts": accs, "strategies": _strategy_numbers(s, e, now), "alerts": {"total": len(al), "by_kind": by},
            "brokers": _brokers(e)}


def facts_of(n: dict) -> List[dict]:
    rows: List[dict] = []

    def add(fid, label, v, unit, disp=None):
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            return
        if disp is None:
            disp = {"usd": f"${v:,.2f}", "percent": f"{v:.2f} %", "count": f"{v:.0f}",
                    "number": f"{v:.2f}", "fraction": f"{v * 100:.2f} %"}.get(unit, f"{v}")
        rows.append({"id": fid, "label": label, "value": v, "unit": unit, "display": disp})

    for a in n["accounts"]:
        p = f"acc.{a['account_id']}"
        add(f"{p}.equity_end", f"{a['account_id']} equity at the end", a["equity_end"], "usd")
        add(f"{p}.pnl", f"{a['account_id']} P&L", a["pnl"], "usd")
        add(f"{p}.return_pct", f"{a['account_id']} return", a["return_pct"], "percent")
        add(f"{p}.max_dd_pct", f"{a['account_id']} max drawdown", a["max_drawdown_pct"], "percent")
        add(f"{p}.fees", f"{a['account_id']} fees", a["fees"], "usd")
        add(f"{p}.orders", f"{a['account_id']} orders", a["orders"]["total"], "count")
        add(f"{p}.filled", f"{a['account_id']} filled orders", a["orders"]["filled"], "count")
        add(f"{p}.rejected", f"{a['account_id']} rejected orders", a["orders"]["rejected"], "count")
        add(f"{p}.halts", f"{a['account_id']} halts / limit events", a["halts"], "count")
        if a.get("note"):
            rows.append({"id": f"{p}.partial", "label": f"{a['account_id']} coverage", "value": a["note"],
                         "unit": "status", "display": a["note"]})
    for i, s in enumerate(n["strategies"], start=1):
        p = f"str.{i}"
        add(f"{p}.sessions", f"{s['label']} sessions", s["sessions"], "count")
        add(f"{p}.log_return", f"{s['label']} log return", s["log_return"], "fraction")
        add(f"{p}.expected", f"{s['label']} expected log return", s["expected_mean"], "fraction")
        add(f"{p}.band_lo", f"{s['label']} band low", s["band_lo"], "fraction")
        add(f"{p}.band_hi", f"{s['label']} band high", s["band_hi"], "fraction")
        add(f"{p}.z", f"{s['label']} z-score vs expected", s["z"], "number")
        rows.append({"id": f"{p}.decay", "label": f"{s['label']} decay state", "value": s["decay_state"],
                     "unit": "status", "display": s["decay_state"]})
    add("alerts.total", "Alerts in the period", n["alerts"]["total"], "count")
    for k, v in sorted(n["alerts"]["by_kind"].items()):
        add(f"alerts.{k}", f"Alerts of kind {k}", v, "count")
    return rows


# --- storage -----------------------------------------------------------------------------------------------------

def _out(r, with_commentaries: bool = True) -> dict:
    out = {"id": r["id"], "period": r["period"], "period_id": r["period_id"], "start": r["start_ts"],
           "end": r["end_ts"], "created_at": r["created_at"], "created_by": r["created_by"]}
    if with_commentaries:
        out["numbers"] = json.loads(r["numbers_json"])
        out["facts"] = json.loads(r["facts_json"])
        out["commentaries"] = commentaries("report", r["id"])
    return out


def create(period: str, day: Optional[date] = None, actor: str = "user", now: Optional[datetime] = None) -> dict:
    from ..execution import accounts
    if period not in PERIODS:
        raise ValueError("period: daily | weekly")
    now = (now or accounts.now()).astimezone(timezone.utc)
    day = day or last_complete(period, now)
    pid, s, e = bounds(period, day)
    si, ei = s.isoformat(timespec="seconds"), e.isoformat(timespec="seconds")
    n = numbers(si, ei, now)
    f = facts_of(n)
    rid = f"rep-{uuid.uuid4().hex[:12]}"
    db.add_audit(actor, "report_created", {"report_id": rid, "period": period, "period_id": pid})
    with db._connect() as conn:
        conn.execute("INSERT INTO mon_reports (id, period, period_id, start_ts, end_ts, created_at, created_by,"
                     " numbers_json, facts_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (rid, period, pid, si, ei, now.isoformat(timespec="seconds"), actor, json.dumps(n, default=str),
                      json.dumps(f, default=str)))
        r = conn.execute("SELECT * FROM mon_reports WHERE id = ?", (rid,)).fetchone()
    if actor == "engine":
        try:
            alerts.emit("report_ready", f"{period.capitalize()} report {pid} ready",
                        "The numbers are in Monitoring → Reports.", f"report:{period}:{pid}", ref=rid, now=now)
        except Exception:        # noqa: BLE001
            logger.exception("report alert failed")
    return _out(r)


def get(rid: str) -> dict:
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM mon_reports WHERE id = ?", (rid,)).fetchone()
    if r is None:
        raise TradingError(404, "not_found", NOT_FOUND)
    return _out(r)


def list_reports(period: Optional[str] = None, limit: int = 50) -> dict:
    q, args = "SELECT * FROM mon_reports", []
    if period:
        q += " WHERE period = ?"
        args.append(period)
    q += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    args.append(int(limit))
    with db._connect() as conn:
        rows = conn.execute(q, args).fetchall()
    return {"reports": [_out(r, False) for r in rows]}


def due(now: datetime) -> List[dict]:
    """Creates the scheduled reports that are due (once each); returns the created ones."""
    from ..execution import accounts
    if not accounts.rows():
        return []
    made = []
    for period in PERIODS:
        day = last_complete(period, now)
        pid, s, e = bounds(period, day)
        if now < e + SCHEDULE_DELAY:
            continue
        if not accounts_in(s.isoformat(timespec="seconds"), e.isoformat(timespec="seconds")):
            continue                     # no account existed in that period: no scheduled report (2026-10-22)
        with db._connect() as conn:
            hit = conn.execute("SELECT 1 FROM mon_reports WHERE period = ? AND period_id = ? AND created_by = 'engine'",
                               (period, pid)).fetchone()
        if hit is None:
            made.append(create(period, day, "engine", now))
    return made


# --- AI commentary (shared with the Shadow Account) -------------------------------------------------------------

SYSTEM_PROMPT = """You write a short commentary on the numbers of a personal paper-trading / portfolio tool. \
Use ONLY the facts given (each fact has an id, a label and a display value). Cite every number with its fact id \
in square brackets, e.g. "the account lost 1.2 % [acc.paper-main.return_pct]". Do not invent numbers, do not \
compute new numbers except obvious differences, and say plainly when the facts cannot answer something. Causes \
from outside the data (news, macro, sectors) may ONLY appear in "hypotheses", phrased as hypotheses. No investment \
advice, no recommendations to buy, sell or change parameters. Answer in {language} as JSON: \
{{"summary": str (<= 600 chars), "sections": [{{"title": str, "text": str}}] (<= 6), "hypotheses": [str] (<= 5), \
"limits": [str] (<= 5)}} — nothing else."""


def _facts_hash(facts: List[dict]) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]


def _comm_out(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "ref_id": r["ref_id"], "created_at": r["created_at"],
            "model": r["model"], "language": r["language"], "output": json.loads(r["output_json"]),
            "check": json.loads(r["check_json"]), "usage": json.loads(r["usage_json"]) if r["usage_json"] else None,
            "cost_usd": r["cost_usd"], "provenance": json.loads(r["provenance_json"])}


def commentaries(kind: str, ref_id: str) -> List[dict]:
    with db._connect() as conn:
        rows = conn.execute("SELECT * FROM mon_commentaries WHERE kind = ? AND ref_id = ? ORDER BY created_at DESC,"
                            " rowid DESC", (kind, ref_id)).fetchall()
    return [_comm_out(r) for r in rows]


def _caps():
    import ai_budgets
    return ai_budgets.reports_caps()


async def commentary(kind: str, ref_id: str, facts: List[dict], tier: str, language: str,
                     actor: str = "user") -> dict:
    """One LLM call on the fact table (numbers only) → the stored, labelled commentary. Raises TradingError."""
    import ai_caps
    from strategy_platform.core import llm
    from ..backtest import explain
    if tier not in TIERS or language not in LANGUAGES:
        raise ValueError("model/language")
    if not facts:
        raise TradingError(409, "no_facts", "There are no numbers to comment on.")
    if not _busy.acquire(blocking=False):
        raise TradingError(409, "busy", "A commentary is already being written — wait until it is finished.")
    try:
        facts_in = [{"id": f["id"], "label": f["label"], "display": f["display"]} for f in facts][:400]
        messages = [{"role": "system", "content": SYSTEM_PROMPT.format(language=LANGUAGES[language])},
                    {"role": "user", "content": json.dumps({"facts": facts_in}, ensure_ascii=False)}]
        model = (llm.models() or {}).get(tier)
        if not model:
            raise TradingError(503, "no_model", "No AI model is configured for this tier.")
        estimate = llm.estimate_cost(messages, MAX_TOKENS, tier)
        caps = _caps()
        try:
            res = caps.reserve(BUDGET_KEY, estimate)
        except ai_caps.BudgetBlocked as ex:
            raise TradingError(402, "budget", ex.message, budget_key=BUDGET_KEY, reason=ex.reason)
        try:
            parsed, _raw, used_model, usage = await asyncio.wait_for(
                llm.chat_json(messages, model, MAX_TOKENS, FEATURE, FEATURE_LABEL, temperature=TEMPERATURE),
                timeout=TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            caps.settle(res, None)
            raise TradingError(504, "timeout", f"The AI did not answer within {TIMEOUT_SECONDS} s — nothing is shown "
                                               "(charged as attempted).")
        except llm.LlmUnavailable as ex:
            if ex.usage is not None:
                caps.settle(res, (ex.usage or {}).get("cost_usd"))
            elif ex.attempted:
                caps.settle(res, None)
            else:
                caps.release(res)
            status = 503 if ex.reason == "no_key" else 502
            raise TradingError(status, ex.reason if ex.reason in ("no_key", "bad_json") else "ai_error",
                               llm.sanitize_untrusted(ex.message, 300) or "The AI request failed.")
        except BaseException:
            caps.release(res)
            raise
        cost = llm.usage_cost(usage, estimate)
        caps.settle(res, cost)
        try:
            output = explain.validate_output(parsed)
        except ValueError as ve:
            raise TradingError(502, "bad_json", f"The AI answer did not have the required format ({ve}) — nothing is "
                                                f"shown (the call was charged: ${cost:.4f}).")
        check = explain.number_check(output, facts, language)
        used = used_model or model
        prov = dict(llm.provenance(FEATURE, used))
        prov["model_tier"] = tier
        cid = f"com-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        usage_out = dict(usage or {}, estimate_usd=round(estimate, 6))
        db.add_audit(actor, "report_commentary" if kind == "report" else "shadow_commentary",
                     {"commentary_id": cid, "ref_id": ref_id, "model": used, "cost_usd": round(cost, 6),
                      "check_ok": check["ok"]})
        with db._connect() as conn:
            conn.execute("INSERT INTO mon_commentaries (id, kind, ref_id, created_at, model, language, facts_hash,"
                         " output_json, check_json, usage_json, cost_usd, provenance_json) VALUES"
                         " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (cid, kind, ref_id, now, used, language, _facts_hash(facts),
                          json.dumps(output, ensure_ascii=False), json.dumps(check, ensure_ascii=False),
                          json.dumps(usage_out, default=str), cost, json.dumps(prov, ensure_ascii=False)))
            r = conn.execute("SELECT * FROM mon_commentaries WHERE id = ?", (cid,)).fetchone()
        return _comm_out(r)
    finally:
        _busy.release()


async def report_commentary(rid: str, tier: str, language: str) -> dict:
    rep = await asyncio.to_thread(get, rid)
    return await commentary("report", rid, rep["facts"], tier, language)
