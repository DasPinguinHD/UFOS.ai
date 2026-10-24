"""Sandbox fixtures (strategy_platform/SPEC.md "Sandbox profile" S4/S5, user decision 2026-10-23) — SANDBOX ONLY.

``seed_incubation_test()``: the strategy "SANDBOX TEST — incubation fixture" (tag ``sandbox-fixture``; a monthly
SPY/IEF trend switch the runtime can really trade on paper-main) moved to ``validated`` THROUGH THE REGISTRY with
clearly synthetic evidence: a backtest, a validation and a holdout run card whose metrics carry
``fixture: true`` + ``fixture_note`` ("synthetic fixture for UI testing — no real validation") and a ``fixture``
warning; the holdout event belongs to the synthetic holdout card. No market data is read, no holdout is peeked at:
the curves are a deterministic synthetic series, labelled as such.

``force_decay(strategy)``: feeds ONE synthetic shock session (labelled synthetic) after the run's real sessions into
the decay monitor's own rules (``decay.assess``) and pauses the strategy through the monitor's own pause path
(audit, event, alert) — the runtime then withholds its orders exactly as after a real trigger.

Both refuse unless this process runs in sandbox mode (``sandbox_profile.require_sandbox``).
Part of UFOS.ai, a proof-of-concept portfolio project; PAPER TRADING ONLY, not investment advice.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

import sandbox_profile as sp
from strategy_platform.core import fixture

REASON = f"SANDBOX fixture — {fixture.NOTE}"
ENGINE_VERSION = "sandbox-fixture/1"
SYMBOLS = ("SPY", "IEF")


# --- the strategy -------------------------------------------------------------------------------------------------

def fixture_spec() -> dict:
    from strategy_platform.strategy.alphas._build import f, op, p, param, spec
    return spec(name=fixture.STRATEGY_NAME, category="trend",
                description=("Sandbox click-test fixture: hold SPY or IEF, whichever trades further above its "
                             "200-day average; monthly rebalance. " + fixture.NOTE + "."),
                tags=[fixture.TAG], universe={"symbols": list(SYMBOLS)},
                parameters={"window": param(200, 100, 300, step=25, description="Trend average in bars.")},
                signal=op("sub", op("div", f("close"), op("sma", f("close"), p("window"))), 1),
                portfolio={"selection": {"type": "top_n", "n": 1}}, frequency="monthly")


def find_fixture() -> Optional[dict]:
    from strategy_platform.storage import db
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM strategies WHERE name = ? ORDER BY created_at",
                                              (fixture.STRATEGY_NAME,)).fetchall()]
    for r in rows:
        if fixture.is_fixture_strategy(r["id"]):
            return r
    return None


# --- synthetic run results ----------------------------------------------------------------------------------------

def _weekdays(n: int, end: date) -> List[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def synthetic_result(n: int, end: date, phase: float = 0.0) -> dict:
    """A deterministic synthetic daily curve (n weekdays ending ``end``) in the shape ``runcard.build_payload``
    reads. Not market data — labelled fixture everywhere."""
    days = _weekdays(n, end)
    eq, bm, dd = [], [], []
    nav = bnav = 100000.0
    peak = nav
    for k in range(n):
        if k:
            nav *= math.exp(0.0004 + 0.006 * math.sin(0.9 * k + phase) + 0.003 * math.sin(0.11 * k + phase))
            bnav *= math.exp(0.0003 + 0.008 * math.sin(0.7 * k + phase))
        peak = max(peak, nav)
        eq.append(nav)
        bm.append(bnav)
        dd.append((peak - nav) / peak * 100.0)          # percent, like the engine's snapshots
    note = {"code": "fixture", "message": f"{fixture.NOTE} — synthetic curve, no market data"}
    return {"engine_version": ENGINE_VERSION,
            "range": {"start": days[0].isoformat(), "end": days[-1].isoformat(), "calendar": "nyse", "sessions": n,
                      "fixture": True},
            "data": {}, "warnings": [note], "costs": None, "broker_params": None, "benchmark_info": {"symbol": "SPY"},
            "dates": [d.isoformat() for d in days], "equity": eq, "benchmark": bm, "long_mv": eq,
            "short_mv": [0.0] * n, "drawdown_pct": dd, "fills": [], "orders": [], "events": [],
            "stats": {"fixture": True}}


def _performance(result: dict) -> dict:
    eq = result["equity"]
    lr = [math.log(b / a) for a, b in zip(eq, eq[1:])]
    n = len(lr)
    mu = math.fsum(lr) / n
    sd = math.sqrt(math.fsum((x - mu) ** 2 for x in lr) / (n - 1))
    mdd = max(result["drawdown_pct"]) / 100.0
    years = n / 252.0
    total = eq[-1] / eq[0] - 1.0
    return {"total_return": total, "cagr": (eq[-1] / eq[0]) ** (1.0 / years) - 1.0,
            "volatility": sd * math.sqrt(252), "sharpe": mu / sd * math.sqrt(252) if sd > 0 else None, "max_drawdown": mdd, "fixture": True}


def _metrics(result: dict, **blocks) -> dict:
    m = {"performance": _performance(result)}
    m.update(blocks)
    m.update(fixture.metrics_fields())
    return m


def _gates(names: List[Tuple[str, str]]) -> List[dict]:
    return [{"gate": g, "name": name, "status": "pass", "value": None, "threshold": None,
             "explanation": f"{fixture.NOTE} — this gate was NOT evaluated", "details": {"fixture": True}}
            for g, name in names]


TRAINING_GATE_NAMES = [(f"G{k}", "fixture — not evaluated") for k in range(1, 9)]


def _report(gates: List[dict], holdout: Optional[dict]) -> dict:
    from strategy_platform.validation import report
    rep = {"version": report.REPORT_VERSION, "passed": report.passed_of(gates) if holdout is None else True,
           "mode": "fixture", "gates": gates, "folds": [], "grid": None, "pbo": None, "dsr": None, "stress": None,
           "trial_count": None, "thresholds": {"preset": "fixture", "overrides": {}, "effective": {}},
           "training": None, "ic": None, "holdout": holdout, "exceptions": []}
    from strategy_platform.backtest import attribution
    rep["gate_facts"] = attribution.gate_facts(rep)
    rep.update(fixture.metrics_fields())
    return rep


def _card(strategy_id: str, kind: str, result: dict, metrics: dict, family_event: Optional[dict] = None) -> dict:
    from strategy_platform.backtest import runcard
    card = runcard.create(strategy_id, kind=kind)
    runcard.mark_running(card["id"])
    if kind == "holdout":
        return runcard.finish_holdout(card["id"], result, metrics, family_event=family_event)
    return runcard.finish(card["id"], result, metrics)


# --- seeding ------------------------------------------------------------------------------------------------------

def seed_incubation_test(now: Optional[datetime] = None) -> dict:
    """Creates (or reports) the fixture strategy and moves it to ``validated`` through the registry."""
    sp.require_sandbox("seed-incubation-test")
    from strategy_platform.lifecycle import registry
    from strategy_platform.storage import db
    existing = find_fixture()
    state = existing["state"] if existing else None
    if state is not None and state not in ("draft", "backtested", "candidate"):
        return {"strategy_id": existing["id"], "name": existing["name"], "state": state, "created": False,
                "fixture_note": fixture.NOTE,
                "message": f"The fixture strategy already exists ({state}) — nothing changed."}
    sid = existing["id"] if existing else registry.create_from_text(json.dumps(fixture_spec()), "user")["id"]
    from strategy_platform.validation import families, report
    el = families.holdout_eligibility(sid, now)
    if not el["eligible"]:
        raise sp.SandboxError(f"The fixture's holdout window is not open: {el['reason']}")
    window = el["window"]
    # backtest + validation: synthetic curves ending at the training end; holdout: a synthetic curve over the holdout
    # window. Nothing is read from the market data or the real holdout — the series are made up and labelled so.
    train_end = families.lock_for_kind(el["universe_kind"])["training_end"]
    cards = {}
    if state in (None, "draft"):
        bt = synthetic_result(756, train_end, 0.0)
        cards["backtest"] = _card(sid, "backtest", bt, _metrics(bt))["id"]
        registry.transition(sid, "backtested", REASON, actor="system", run_id=cards["backtest"])
        state = "backtested"
    if state == "backtested":
        va = synthetic_result(756, train_end, 0.3)
        val_rep = _report(_gates(TRAINING_GATE_NAMES), None)
        cards["validation"] = _card(sid, "validation", va,
                                    _metrics(va, validation=report.stored_block(val_rep)))["id"]
        registry.transition(sid, "candidate", REASON, actor="system", run_id=cards["validation"])
    ho = synthetic_result(504, date.fromisoformat(window["end"]), 0.6)
    ho_block = {"passed": True, "fixture": True, "fixture_note": fixture.NOTE}
    ho_rep = _report(_gates([("H", "holdout (fixture — not evaluated)")]), ho_block)
    ho_card = _card(sid, "holdout", ho, _metrics(ho, holdout=report.stored_block(ho_rep)),
                    family_event={"window": window, "result": "pass"})
    cards["holdout"] = ho_card["id"]
    row = registry.transition(sid, "validated", REASON, actor="system", run_id=ho_card["id"])
    db.add_audit("system", "sandbox_fixture_seeded", {"strategy_id": sid, "note": fixture.NOTE,
                                                      "cards": cards})
    return {"strategy_id": sid, "name": row["name"], "state": row["state"], "created": True,
            "run_cards": cards,
            "holdout_event_id": ho_card.get("holdout_event_id"), "fixture_note": fixture.NOTE,
            "next": "Start UFOS.ai (sandbox), Portfolio & Trading → Incubation → start an incubation run of the "
                    "fixture on paper-main."}


# --- force-decay --------------------------------------------------------------------------------------------------

def _resolve(strategy: str) -> dict:
    from strategy_platform.storage import db
    text = (strategy or "").strip()
    if not text:
        raise sp.SandboxError('force-decay needs a strategy name or id, e.g. force-decay "SANDBOX TEST — '
                              'incubation fixture"')
    row = db.strategy_get(text)
    if row is not None:
        return row
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM strategies WHERE lower(name) = lower(?)",
                                              (text,)).fetchall()]
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise sp.SandboxError(f"No strategy named or with the id {text!r} in the sandbox.")
    raise sp.SandboxError(f"{len(rows)} strategies are named {text!r} — pass the id instead: "
                          + ", ".join(r["id"] for r in rows))


def force_decay(strategy: str, now: Optional[datetime] = None) -> dict:
    sp.require_sandbox("force-decay")
    from strategy_platform.live import incubation
    from strategy_platform.monitoring import decay, expected
    row = _resolve(strategy)
    run = next((r for r in incubation.active_runs() if r["strategy_id"] == row["id"]), None)
    if run is None:
        raise sp.SandboxError(f"{row['name']} has no active incubation run — start one first (Portfolio & Trading → "
                              "Incubation); the decay monitor only watches incubating strategies.")
    ev = decay.latest_event(run["id"])
    if ev is not None and ev["action"] == "paused":
        return {"strategy_id": row["id"], "run_id": run["id"], "paused": True, "already": True,
                "message": "The strategy is already paused by the decay monitor — decide in Monitoring."}
    exp = expected.basis(run)
    if exp is None or exp.get("max_drawdown") is None:
        raise sp.SandboxError("The run has no expected basis with a holdout max drawdown — the drawdown rule cannot "
                              "be fed.")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    last = expected.last_completed(run, now)
    real = [x for _d, x in (expected.realised(run, last) if last is not None else [])]
    R = decay.rules_for(exp, run["calendar"])
    target = min(0.95, R["DD_FACTOR"] * float(exp["max_drawdown"]) + 0.05)
    shock = math.log(1.0 - target)                       # ONE synthetic session: a drawdown beyond the limit
    series = real + [shock]
    rearm = decay._rearm_of(ev)
    a = decay.assess(exp, series, run["calendar"], rearm)
    if "drawdown" not in a["triggered"]:
        raise sp.SandboxError(f"The synthetic deviation did not trigger the drawdown rule ({a['rules']}) — nothing "
                              "was paused.")
    a = dict(a, triggered=["drawdown"] + [t for t in a["triggered"] if t != "drawdown"])
    day = (last or now.date()).isoformat()
    synthetic = {"synthetic": True, "fixture": True, "fixture_note": fixture.NOTE,
                 "synthetic_session_log_return": shock, "real_sessions": len(real),
                 "source": "tools/sandbox.py force-decay"}
    out_ev = decay._pause(run, a, day, now.isoformat(timespec="seconds"), synthetic=synthetic)
    return {"strategy_id": row["id"], "name": row["name"], "run_id": run["id"], "paused": True,
            "rule": "drawdown", "reason": out_ev["reason"], "event_id": out_ev["id"], "synthetic": True,
            "fixture_note": fixture.NOTE,
            "next": "Monitoring shows the strategy paused; decide keep running / demote there."}
