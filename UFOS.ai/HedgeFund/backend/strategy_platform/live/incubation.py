"""Incubation runs (SPEC §17.2): paper trading with live prices for a validated
strategy, for a length chosen BEFORE each run and then IMMUTABLE (``incubation_runs`` is
insert-only by triggers; no route or function changes a run's length).

- length 20..250 sessions (default 60) of the strategy's calendar: NYSE sessions for
  equity/mixed universes, UTC days for crypto-only ones; ``start_date`` = the first
  session after the start request, ``end_date`` = start + length − 1 sessions.
- expected band from the version's latest done holdout run card (the ``validated``
  evidence): daily LOG returns of its equity curve → μ, σ; max drawdown.
- result at completion: ``passed`` when the realised cumulative log return lies in
  n·μ ± 1.96·σ·√n (n = return days) AND the realised max drawdown ≤ 1.5 × the holdout's
  AND missed sessions ≤ 20 % of the length; missed > 20 % → ``inconclusive``; else
  ``failed``. ``tracking_error`` = annualised RMS deviation of the realised daily return
  from the expected mean. ``live_eligible`` = latest completed run passed and the
  strategy still incubating that version (stage 8/11 decide what follows; live is OFF).

Part of UFOS.ai, a proof-of-concept portfolio project; PAPER TRADING ONLY.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ..execution import accounts
from ..execution.errors import TradingError
from ..storage import db

logger = logging.getLogger(__name__)

DEFAULT_LENGTH, MIN_LENGTH, MAX_LENGTH = 60, 20, 250
BAND_Z = 1.96
DD_FACTOR = 1.5
MAX_MISSED_SHARE = 0.20
PERIODS = {"nyse": 252, "utc": 365}
NOT_FOUND = "No such incubation run."
REASON_MIN, REASON_MAX = 3, 500


def defaults() -> dict:
    return {"length_sessions": DEFAULT_LENGTH, "min": MIN_LENGTH, "max": MAX_LENGTH}


def source_id_for(strategy_id: str) -> str:
    """The sub-ledger source of a strategy = its id (strategy ids are "s-<hex>", distinct from the fixed
    sources ``_unallocated`` / ``council-of-ais``)."""
    return strategy_id


def calendar_of(spec: dict) -> str:
    from ..core.instruments import instrument
    from ..strategy import schema, universe
    full = schema.with_defaults(spec)
    classes = {instrument(s).asset_class for s in universe.resolve(full["universe"])}
    return "utc" if classes and classes == {"crypto"} else "nyse"


def session_day(calendar: str, ts: datetime) -> date:
    from ..core import calendars
    ts = ts.astimezone(timezone.utc)
    return ts.date() if calendar == "utc" else calendars.ny_date(ts)


def first_session_after(calendar: str, now: datetime) -> date:
    from ..core import calendars
    today = session_day(calendar, now)
    return today + timedelta(days=1) if calendar == "utc" else calendars.next_trading_day(today)


def add_sessions(calendar: str, start: date, n: int) -> date:
    """The n-th session counting ``start`` as the first (CalendarUnavailable beyond the calendar years)."""
    from ..core import calendars
    if calendar == "utc":
        return start + timedelta(days=n - 1)
    d = start if calendars.is_trading_day(start) else calendars.next_trading_day(start)
    for _ in range(n - 1):
        d = calendars.next_trading_day(d)
    return d


def sessions_between(calendar: str, start: date, end: date) -> List[date]:
    from ..core import calendars
    if end < start:
        return []
    if calendar == "utc":
        return [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return calendars.trading_days(start, end)


def last_completed_session(calendar: str, now: datetime, lag_minutes: int = 20) -> Optional[date]:
    """The newest session whose close (+ lag) lies at/before now."""
    from ..core import calendars
    t = now.astimezone(timezone.utc) - timedelta(minutes=lag_minutes)
    if calendar == "utc":
        return t.date() - timedelta(days=1)
    return calendars.ny_date(calendars.last_close_before(t))


# --- expected band from the holdout card ---------------------------------------------------------------

def _log_returns(equity: List[Optional[float]]) -> List[float]:
    pts = [float(e) for e in equity if e is not None and float(e) > 0]
    return [math.log(b / a) for a, b in zip(pts, pts[1:])]


def _max_dd_from_returns(rets: List[float], log: bool = True) -> float:
    nav, peak, dd = 1.0, 1.0, 0.0
    for r in rets:
        nav *= math.exp(r) if log else (1.0 + r)
        peak = max(peak, nav)
        dd = max(dd, (peak - nav) / peak)
    return dd


def expected_for(strategy_id: str, version: int) -> dict:
    from ..backtest import runcard
    latest = runcard.latest_done(strategy_id, int(version), kind="holdout")
    if latest is None:
        raise TradingError(409, "no_holdout", "The strategy version has no finished holdout run — it is not "
                                              "validated (§9.5).")
    card = runcard.get(latest["id"])
    curve = (card or {}).get("curve") or {}
    # (date, log return) between consecutive VALUED points — a None/0 equity point (valuation gap) is skipped and
    # the next return is dated by its own day (audit 2026-10-18: the dates were zipped against the unfiltered list)
    pts = [(d, float(e)) for d, e in zip(curve.get("dates") or [], curve.get("equity") or [])
           if e is not None and float(e) > 0]
    dated = [(pts[k][0], math.log(pts[k][1] / pts[k - 1][1])) for k in range(1, len(pts))]
    lr = [r for _d, r in dated]
    if len(lr) < 2:
        raise TradingError(409, "no_holdout", "The holdout run card has too few equity points for an expected band.")
    n = len(lr)
    mu = math.fsum(lr) / n
    sd = math.sqrt(math.fsum((x - mu) ** 2 for x in lr) / (n - 1))
    cal = ((card.get("range") or {}).get("calendar") or "nyse")
    mdd = ((card.get("metrics") or {}).get("performance") or {}).get("max_drawdown")
    if not isinstance(mdd, (int, float)):
        mdd = _max_dd_from_returns(lr)
    return {"mean_daily": mu, "std_daily": sd, "vol": sd * math.sqrt(PERIODS.get(cal, 252)),
            "max_drawdown": float(mdd), "source_run_id": latest["id"], "sessions": n, "calendar": cal,
            "returns": [[d, r] for d, r in dated][-250:]}


# --- storage ------------------------------------------------------------------------------------------------

RUN_COLS = ("id", "strategy_id", "version", "content_hash", "account_id", "source_id", "length_sessions", "calendar",
            "start_date", "end_date", "created_at", "created_by", "reason", "expected_json")


def run_row(run_id: str) -> Optional[dict]:
    with db._connect() as conn:
        r = conn.execute("SELECT * FROM incubation_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(r) if r else None


def run_rows(strategy_id: Optional[str] = None) -> List[dict]:
    q, args = "SELECT * FROM incubation_runs", []
    if strategy_id:
        q += " WHERE strategy_id = ?"
        args.append(strategy_id)
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY created_at DESC, rowid DESC", args).fetchall()]


def run_events(run_id: str, limit: int = 300) -> List[dict]:
    with db._connect() as conn:
        rows = conn.execute("SELECT id, ts, event, day, data_json FROM incubation_events WHERE run_id = ? ORDER BY id"
                            " DESC LIMIT ?", (run_id, int(limit))).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "event": r["event"], "day": r["day"],
             "data": _without_empty(json.loads(r["data_json"])) if r["data_json"] else None} for r in reversed(rows)]


def _without_empty(d):
    """fix1023 round 3: an empty note is left out of an event's data (no "reason=" in the app)."""
    return {k: v for k, v in d.items() if v != ""} if isinstance(d, dict) else d


def end_event(run_id: str) -> Optional[dict]:
    with db._connect() as conn:
        r = conn.execute("SELECT id, ts, event, data_json FROM incubation_events WHERE run_id = ? AND event IN"
                         " ('completed', 'aborted') LIMIT 1", (run_id,)).fetchone()
    return None if r is None else {"ts": r["ts"], "event": r["event"],
                                   "data": json.loads(r["data_json"]) if r["data_json"] else {}}


def add_event(run_id: str, event: str, data=None, day: Optional[str] = None) -> bool:
    import sqlite3  # noqa: F401
    try:
        with db._connect() as conn:
            conn.execute("INSERT INTO incubation_events (run_id, ts, event, day, data_json) VALUES (?, ?, ?, ?, ?)",
                         (run_id, db.utc_now_iso(), event, day, json.dumps(data, default=str) if data is not None else None))
        return True
    except db.StorageUnavailable as ex:
        if "UNIQUE" in str(ex.detail):
            return False
        raise


def active_runs() -> List[dict]:
    """Runs without an end event (running) + completed runs whose strategy still incubates that version
    (they keep trading on paper)."""
    from ..lifecycle import registry
    out = []
    for r in run_rows():
        end = end_event(r["id"])
        if end is not None and end["event"] == "aborted":
            continue
        row = db.strategy_get(r["strategy_id"])
        if end is not None and (row is None or row["state"] != "incubation" or int(row["current_version"]) !=
                                int(r["version"])):
            continue
        out.append(dict(r, _end=end))
    # newest run per strategy only
    seen, res = set(), []
    for r in out:
        if r["strategy_id"] in seen:
            continue
        seen.add(r["strategy_id"])
        res.append(r)
    return res


def running_for(strategy_id: str) -> Optional[dict]:
    for r in run_rows(strategy_id):
        if end_event(r["id"]) is None:
            return r
    return None


# --- eligibility / start / abort ----------------------------------------------------------------------------

def eligibility(strategy_id: str, now: Optional[datetime] = None, account_id: Optional[str] = None) -> dict:
    """``account_id`` None = the strategy's route (SPEC §18.2; fix 2026-10-18 — it used to be paper-main always)."""
    from ..lifecycle import registry
    now = now or accounts.now()
    if account_id is None:
        from ..execution.brokers import service as broker_service
        account_id = broker_service.route_for(strategy_id)
    try:
        row = registry.get(strategy_id)
    except registry.NotFound:
        raise TradingError(404, "not_found", "No such strategy.")
    reason, code = None, None
    cal = None
    start_if_now = None
    try:
        spec = registry.get_version(strategy_id, int(row["current_version"]))["spec"]
        cal = calendar_of(spec)
        start_if_now = first_session_after(cal, now).isoformat()
    except TradingError:
        raise
    except Exception as ex:      # noqa: BLE001 — shown as the reason
        reason, code = f"the calendar is not available ({getattr(ex, 'user_message', ex)})", "calendar"
    if reason is None and running_for(strategy_id) is not None:      # fix1023 round 2: checked first
        reason, code = "an incubation run of this strategy is still running", "already_incubating"
    if reason is None and row["state"] != "validated":
        reason, code = (f"only a validated strategy can start an incubation run (it is {row['state']})",
                        "already_incubating" if row["state"] == "incubation" else "not_validated")
    if reason is None:
        lock = registry._failed_lock(row)
        if lock:
            reason, code = lock, "locked"
    if reason is None:
        if accounts.is_broker(account_id):
            reason, code = _broker_reason(strategy_id, account_id, now)
        elif not accounts.exists(account_id):
            reason, code = accounts.NOT_OPEN, "account_not_open"
        elif accounts.get(account_id).halt()["active"]:
            reason, code = f"the account {account_id} is halted — resume it first", "halted"
    if reason is None:
        try:
            expected_for(strategy_id, int(row["current_version"]))
        except TradingError as ex:
            reason, code = ex.user_message, ex.code
    return {"eligible": reason is None, "reason": reason, "code": code, "calendar": cal, "defaults": defaults(),
            "account_id": account_id, "start_date_if_now": start_if_now}


def _broker_reason(strategy_id: str, account_id: str, now: datetime):
    """Stage 8 preconditions of a broker account as the incubation account (SPEC §18.2, §18.4): connected, venue rule,
    not halted, reconciled (the same rule as the ``broker_link`` pre-trade check). (reason, code) or (None, None)."""
    from ..execution.brokers import mirror
    from ..execution.brokers import service as broker_service
    why = accounts.retired_reason(account_id)
    if why:                      # a route to a retired broker blocks the start; it is never moved silently
        return f"{account_id}: {why} — re-route the strategy (Settings → Brokers → Routing)", "retired"
    why = accounts.sandbox_block_reason(account_id)
    if why:                      # sandbox profile: broker accounts never trade in the sandbox
        return f"{account_id}: {why} — route the strategy to paper-main for the click-test", "sandbox_blocked"
    if broker_service.connection(account_id)["status"] != "connected":
        return (f"the broker account {account_id} is not connected — connect it in Settings → Brokers",
                "account_not_open")
    cls = broker_service.universe_class(strategy_id)
    if account_id not in broker_service.allowed_accounts(cls):
        return (f"a {cls or 'unknown'} universe cannot run on {account_id} (stocks/ETFs → alpaca-paper, crypto → "
                "binance-demo, mixed → paper-main only)", "wrong_venue")
    why_long = broker_service.long_only_reason(strategy_id, account_id)
    if why_long:                 # SPEC §18.14 item 5: binance-demo is spot, long only
        return why_long, "long_only"
    acc = accounts.get(account_id)
    h = acc.halt()
    if h["active"]:
        return (f"the account {account_id} is halted ({h['reason'] or h['kind']}) — "
                + ("resolve the reconciliation first" if h["kind"] == "reconcile" else "resume it first"), "halted")
    why = mirror.link_block_reason(acc, now)
    if why:
        return why, "not_reconciled"
    return None, None


_start_lock = threading.Lock()


def start(strategy_id: str, length_sessions: int, reason: str, account_id: Optional[str] = None,
          actor: str = "user", now: Optional[datetime] = None) -> dict:
    """fix1023 B1: serialised — a double submit finds the first run and gets 409 ``already_incubating``; never a
    second run."""
    with _start_lock:
        out = _start(strategy_id, length_sessions, reason, account_id, actor, now)
    # fix1023 round 3: the run is committed — the allocation (may read quotes, i.e. wait for the network) runs after
    # the commit and never holds the HTTP answer longer than ALLOCATION_WAIT_SECONDS; a failure is an alert + audit row
    out["allocation"] = _allocate_after_start(out["run"]["id"], strategy_id, out.pop("_account_id"), now)
    return out


ALLOCATION_WAIT_SECONDS = 3.0
ALLOCATION_PENDING_NOTE = ("the capital allocation continues in the background — a failure raises an alert; the runtime "
                           "also rebalances at its next cycle")


def _allocate_after_start(run_id: str, strategy_id: str, account_id: str, now: Optional[datetime]) -> dict:
    """{status: done | failed | pending, error?, note?} — ``pending`` when it takes longer than the wait."""
    box: dict = {}

    def work():
        try:
            from ..portfolio import allocation
            allocation.rebalance(accounts.get(account_id), "strategy entered incubation", "engine", now=now)
            box["status"] = "done"
        except Exception as ex:      # noqa: BLE001 — recorded visibly (alert + audit), never dropped
            msg = (getattr(ex, "user_message", None) or f"{type(ex).__name__}: {ex}")[:300]
            box.update(status="failed", error=msg)
            _allocation_failed(run_id, strategy_id, account_id, msg)

    t = threading.Thread(target=work, name=f"alloc-{run_id}", daemon=True)
    t.start()
    t.join(ALLOCATION_WAIT_SECONDS)
    if t.is_alive():
        return {"status": "pending", "note": ALLOCATION_PENDING_NOTE}
    return dict(box)


def _allocation_failed(run_id: str, strategy_id: str, account_id: str, msg: str) -> None:
    logger.warning("allocation after the incubation start of %s failed (the runtime retries): %s", run_id, msg)
    try:
        db.add_audit("engine", "incubation_allocation_failed", {"run_id": run_id, "strategy_id": strategy_id,
                                                                "account_id": account_id, "error": msg})
        from ..monitoring import alerts
        alerts.emit("allocation_failed", f"Allocation after the incubation start failed — {account_id}"[:160],
                    f"Run {run_id}: {msg}. The run is started; its capital is allocated when the allocation succeeds "
                    "(the runtime retries at its next cycle). Paper trading · simulated money.",
                    f"allocfail:{run_id}", account_id=account_id, strategy_id=strategy_id, ref=run_id,
                    data={"run_id": run_id, "error": msg})
    except Exception:            # noqa: BLE001 — the warning above stays
        logger.exception("could not record the failed allocation of %s", run_id)


def _start(strategy_id: str, length_sessions: int, reason: str, account_id: Optional[str] = None,
           actor: str = "user", now: Optional[datetime] = None) -> dict:
    from ..lifecycle import registry
    reason = reason or ""                        # fix1023: an optional note
    if isinstance(length_sessions, bool) or not isinstance(length_sessions, int) \
            or not MIN_LENGTH <= length_sessions <= MAX_LENGTH:
        raise ValueError(f"length_sessions must be a whole number from {MIN_LENGTH} to {MAX_LENGTH}")
    now = now or accounts.now()
    el = eligibility(strategy_id, now, account_id)
    if not el["eligible"]:
        http = 404 if el["code"] == "not_found" else 409
        raise TradingError(http, el["code"] or "not_eligible", f"Can't start an incubation run: {el['reason']}.")
    account_id = el["account_id"]            # the route when not given (SPEC §18.2)
    row = registry.get(strategy_id)
    version = int(row["current_version"])
    v = registry.get_version(strategy_id, version)
    cal = calendar_of(v["spec"])
    start_d = first_session_after(cal, now)
    try:
        end_d = add_sessions(cal, start_d, length_sessions)
    except Exception as ex:      # noqa: BLE001 — CalendarUnavailable: no guessed end date
        raise TradingError(409, "calendar", f"The end date cannot be computed: {getattr(ex, 'user_message', ex)}")
    expected = expected_for(strategy_id, version)
    rid = f"inc-{uuid.uuid4().hex[:12]}"
    ts = db.utc_now_iso()
    with db._connect() as conn:
        conn.execute("INSERT INTO incubation_runs (id, strategy_id, version, content_hash, account_id, source_id,"
                     " length_sessions, calendar, start_date, end_date, created_at, created_by, reason, expected_json)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (rid, strategy_id, version, v["content_hash"], account_id, source_id_for(strategy_id),
                      length_sessions, cal, start_d.isoformat(), end_d.isoformat(), ts, actor, reason.strip()[:REASON_MAX],
                      json.dumps(expected)))
        conn.execute("INSERT INTO incubation_events (run_id, ts, event, day, data_json) VALUES (?, ?, 'started', NULL, ?)",
                     (rid, ts, json.dumps({"length_sessions": length_sessions, "start_date": start_d.isoformat(),
                                           "end_date": end_d.isoformat()})))
        conn.execute("INSERT INTO audit (ts, actor, action, detail_json) VALUES (?, ?, 'incubation_started', ?)",
                     (ts, actor, json.dumps({"run_id": rid, "strategy_id": strategy_id, "version": version,
                                             "length_sessions": length_sessions, "account_id": account_id})))
    try:
        strat = registry.incubation_transition(strategy_id, "incubation", reason, actor=actor, run_id=rid)
    except Exception as ex:
        add_event(rid, "aborted", {"reason": f"start failed: {getattr(ex, 'message', ex)}"[:300], "actor": "system"})
        raise TradingError(409, "not_validated", f"Can't start the incubation run: {getattr(ex, 'message', ex)}")
    # the allocation runs after the commit (``start`` → ``_allocate_after_start``, fix1023 round 3)
    return {"run": status(run_row(rid), now), "strategy": _strategy_brief(strat), "_account_id": account_id}


def _strategy_brief(row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    return {"id": row["id"], "state": row["state"], "current_version": row["current_version"]}


def abort(run_id: str, reason: str, actor: str = "user", *, move_state: bool = True,
          now: Optional[datetime] = None) -> dict:
    from ..lifecycle import registry
    r = run_row(run_id)
    if r is None:
        raise TradingError(404, "not_found", NOT_FOUND)
    if end_event(run_id) is not None and end_event(run_id)["event"] == "aborted":
        raise TradingError(409, "not_running", "This incubation run has already ended.")
    end = end_event(run_id)
    if end is not None:
        # completed earlier: aborting ends the paper trading that continued after completion
        row = db.strategy_get(r["strategy_id"])
        if not (row and row["state"] == "incubation"):
            raise TradingError(409, "not_running", "This incubation run has already ended.")
    else:
        add_event(run_id, "aborted", _without_empty({"reason": (reason or "")[:REASON_MAX], "actor": actor}))
    try:
        db.add_audit(actor, "incubation_aborted", {"run_id": run_id, "strategy_id": r["strategy_id"], "reason": reason})
    except Exception:            # noqa: BLE001
        logger.exception("audit row incubation_aborted failed")
    strat = db.strategy_get(r["strategy_id"])
    if move_state and strat is not None and strat["state"] == "incubation" \
            and int(strat["current_version"]) == int(r["version"]):
        strat = registry.incubation_transition(r["strategy_id"], "validated",
                                               f"incubation aborted: {reason}" if reason else "incubation aborted",
                                               actor=actor, run_id=run_id)
    return {"run": status(run_row(run_id), now), "strategy": _strategy_brief(strat)}


def on_left_incubation(strategy_id: str, reason: str, actor: str) -> None:
    """The strategy left incubation by another transition (e.g. → retired): its run ends."""
    r = running_for(strategy_id)
    if r is not None:
        add_event(r["id"], "aborted", {"reason": reason[:REASON_MAX], "actor": actor})


# --- status ----------------------------------------------------------------------------------------------

def _session_events(run_id: str) -> Dict[str, dict]:
    with db._connect() as conn:
        rows = conn.execute("SELECT day, data_json FROM incubation_events WHERE run_id = ? AND event = 'session'",
                            (run_id,)).fetchall()
    return {r["day"]: (json.loads(r["data_json"]) if r["data_json"] else {}) for r in rows}


def realised(run: dict) -> Tuple[dict, List[float]]:
    from ..core import calendars
    from ..portfolio import ledger
    # S10-H (SPEC §19.12 note): from the snapshot of the session BEFORE start_date — the first session's own return
    # counts (as in monitoring/expected.realised); before, the §17.2 band dropped it
    start = date.fromisoformat(run["start_date"])
    prev = start - timedelta(days=1) if run.get("calendar") == "utc" else calendars.previous_trading_day(start)
    rets = [r for d, r in ledger.daily_returns(run["account_id"], run["source_id"], prev.isoformat())
            if run["start_date"] <= d <= run["end_date"]]
    snaps = ledger.snapshots(run["account_id"], run["source_id"], run["start_date"])
    P = PERIODS.get(run["calendar"], 252)
    lr = [math.log(1.0 + r) for r in rets if r > -1.0]
    out = {"return": (math.exp(math.fsum(lr)) - 1.0) if lr else None, "log_return": math.fsum(lr) if lr else None,
           "vol": None, "sharpe": None, "max_drawdown": _max_dd_from_returns(lr) if lr else None,
           "equity": snaps[-1]["equity"] if snaps else None}
    if len(rets) >= 2:
        m = math.fsum(rets) / len(rets)
        sd = math.sqrt(math.fsum((x - m) ** 2 for x in rets) / (len(rets) - 1))
        out["vol"] = sd * math.sqrt(P)
        out["sharpe"] = (m / sd * math.sqrt(P)) if sd > 0 else None
    return out, lr


def evaluate(run: dict, sessions_traded: int, sessions_missed: int) -> Tuple[Optional[str], List[str], dict]:
    exp = json.loads(run["expected_json"]) if run.get("expected_json") else None
    real, lr = realised(run)
    n = len(lr)
    band = None
    te = None
    reasons: List[str] = []
    if exp:
        mu, sd = float(exp["mean_daily"]), float(exp["std_daily"])
        if n:
            band = {"lo": n * mu - BAND_Z * sd * math.sqrt(n), "hi": n * mu + BAND_Z * sd * math.sqrt(n), "n": n}
            te = math.sqrt(math.fsum((x - mu) ** 2 for x in lr) / n) * math.sqrt(PERIODS.get(run["calendar"], 252))
    result = None
    if sessions_missed > MAX_MISSED_SHARE * int(run["length_sessions"]):
        result = "inconclusive"
        reasons.append(f"{sessions_missed} of {run['length_sessions']} sessions were missed (more than "
                       f"{MAX_MISSED_SHARE:.0%})")
    elif band is None or real["log_return"] is None:
        result = "inconclusive"
        reasons.append("no realised returns to compare with the expected band")
    else:
        ok_band = band["lo"] <= real["log_return"] <= band["hi"]
        dd_lim = DD_FACTOR * float(exp["max_drawdown"])
        ok_dd = (real["max_drawdown"] or 0.0) <= dd_lim + 1e-12
        if not ok_band:
            reasons.append(f"realised log return {real['log_return']:.4f} outside the expected band "
                           f"[{band['lo']:.4f}, {band['hi']:.4f}]")
        if not ok_dd:
            reasons.append(f"realised max drawdown {real['max_drawdown']:.2%} above 1.5 × the holdout's "
                           f"{float(exp['max_drawdown']):.2%}")
        result = "passed" if (ok_band and ok_dd) else "failed"
    return result, reasons, {"realised": real, "band": band, "tracking_error": te}


def status(run: Optional[dict], now: Optional[datetime] = None) -> Optional[dict]:
    if run is None:
        return None
    from ..lifecycle import registry
    now = now or accounts.now()
    end = end_event(run["id"])
    ev = _session_events(run["id"])
    traded = sum(1 for v in ev.values() if v.get("traded"))
    missed = sum(1 for v in ev.values() if not v.get("traded"))
    last_session = max(ev) if ev else None
    exp = json.loads(run["expected_json"]) if run.get("expected_json") else None
    if exp:
        exp = {k: exp.get(k) for k in ("mean_daily", "std_daily", "vol", "max_drawdown", "source_run_id", "sessions")}
    if end is not None and end["event"] == "completed":
        d = end["data"] or {}
        result, reasons = d.get("result"), d.get("result_reasons") or []
        ev_out = {"realised": d.get("realised"), "band": d.get("band"), "tracking_error": d.get("tracking_error")}
    else:
        _r, _reasons, ev_out = evaluate(run, traded, missed)
        result, reasons = None, []
    strat = db.strategy_get(run["strategy_id"])
    st = "running" if end is None else end["event"]
    live_eligible = bool(st == "completed" and result == "passed" and strat is not None
                         and strat["state"] == "incubation" and int(strat["current_version"]) == int(run["version"]))
    out = {"id": run["id"], "strategy_id": run["strategy_id"], "strategy_name": strat["name"] if strat else None,
            "version": run["version"], "account_id": run["account_id"], "source_id": run["source_id"],
            "length_sessions": run["length_sessions"], "calendar": run["calendar"], "start_date": run["start_date"],
            "end_date": run["end_date"], "created_at": run["created_at"], "created_by": run["created_by"],
            "reason": run["reason"], "status": st,
            "progress": {"sessions_elapsed": len(ev), "sessions_total": run["length_sessions"], "traded": traded,
                         "missed": missed, "last_session": last_session},
            "realised": ev_out.get("realised"), "expected": exp, "band": ev_out.get("band"),
            "tracking_error": ev_out.get("tracking_error"), "result": result, "result_reasons": reasons,
            "live_eligible": live_eligible, "ended_at": end["ts"] if end else None,
            "end_reason": (end["data"] or {}).get("reason") if end else None}
    from ..core import fixture
    out.update(fixture.strategy_fields(run["strategy_id"]))     # sandbox fixture label (absent otherwise)
    return out


def record_session(run: dict, day: date, traded: bool, equity: Optional[float]) -> bool:
    return add_event(run["id"], "session", {"traded": bool(traded), "equity": equity}, day=day.isoformat())


def complete(run: dict, now: Optional[datetime] = None) -> Optional[dict]:
    ev = _session_events(run["id"])
    traded = sum(1 for v in ev.values() if v.get("traded"))
    missed = sum(1 for v in ev.values() if not v.get("traded"))
    result, reasons, ev_out = evaluate(run, traded, missed)
    data = {"result": result, "result_reasons": reasons, "traded": traded, "missed": missed, **ev_out}
    if add_event(run["id"], "completed", data):
        try:
            db.add_audit("engine", "incubation_completed", {"run_id": run["id"], "strategy_id": run["strategy_id"],
                                                            "result": result, "reasons": reasons})
        except Exception:        # noqa: BLE001
            logger.exception("audit row incubation_completed failed")
        return data
    return None
