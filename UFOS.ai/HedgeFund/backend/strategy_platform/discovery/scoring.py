"""Deterministic loop score (SPEC §16.2) — computed from ONE done backtest run card (metrics + range), no AI,
no network, never holdout data.

score = sharpe − p_turnover − p_drawdown − p_costs − CHECK_PENALTY × failed pre-checks
- sharpe      = metrics.performance.sharpe (net of the engine's costs); None -> score None (+ reason)
- p_turnover  = min(TURNOVER_PENALTY_CAP, TURNOVER_PENALTY × max(0, turnover_annual − TURNOVER_FREE))
                (metrics.exposure.turnover_annual, one-sided book turnover per year; unavailable -> 0 + note)
- p_drawdown  = DRAWDOWN_PENALTY × max(0, max_drawdown − DRAWDOWN_FREE)          (fractions)
- p_costs     = min(COST_PENALTY_CAP, COST_PENALTY × max(0, cost_share − COST_FREE)),
                cost_share = costs.pct_of_gross_pnl / 100
                (only when gross P&L > 0; else 0 + note)
Pre-checks (gate-like, from the run card only — NOT the validation gates; thresholds = current gate settings):
  trades    closed trades ≥ g7_min_trades
  signal    cross-sectional: best naive IC t-stat over horizons 1/5/10/21 ≥ g1_ic_t;
            time-series: positive 13-week window share > 0.5
  alpha     annual alpha vs the benchmark > 0
  capacity  capacity estimate ≥ g8_capacity_multiple × capital (crypto-only universe: n/a = pass, as G8)
A value that cannot be computed -> the check fails with the reason (never a default pass).

Projected G4 impact: expected maximum Sharpe of n unskilled trials (``validation.stats.expected_max_sharpe``)
with V = the sample variance of the family's trial Sharpes, floored at V_min = 1 / years (as §9.9; years =
the newest family card's sample length, 1 year when the family has no card yet).
"""
from __future__ import annotations

import math
from typing import List, Optional

# constants (SPEC §16.2 — "default — user confirm")
TURNOVER_FREE = 12.0
TURNOVER_PENALTY = 0.01
TURNOVER_PENALTY_CAP = 1.0
DRAWDOWN_FREE = 0.20
DRAWDOWN_PENALTY = 2.0
COST_FREE = 0.5
COST_PENALTY = 0.5
COST_PENALTY_CAP = 1.0
CHECK_PENALTY = 0.25
IC_HORIZONS = (1, 5, 10, 21)
TS_POSITIVE_SHARE_MIN = 0.5
CHECKS = ("trades", "signal", "alpha", "capacity")
SCORE_VERSION = "sp-loop-score/1"


class HoldoutDataRefused(Exception):
    """A card that is not a plain backtest ending before the locked holdout window reached the scoring."""


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def holdout_guard(card: dict) -> None:
    """Raises HoldoutDataRefused unless ``card`` is a ``backtest`` card without holdout access whose simulated
    end lies before the holdout window."""
    if (card.get("kind") or "backtest") != "backtest":
        raise HoldoutDataRefused(f"run {card.get('id')} is a {card.get('kind')} card — the loop scores backtests only")
    rng = card.get("range") or {}
    hold = rng.get("holdout") or {}
    if hold.get("access"):
        raise HoldoutDataRefused(f"run {card.get('id')} had holdout access")
    end, start = rng.get("end"), hold.get("start")
    if not (isinstance(end, str) and isinstance(start, str)) or not end < start:
        raise HoldoutDataRefused(f"run {card.get('id')} does not end before the holdout window "
                                 f"(end {end}, window from {start})")


def thresholds() -> dict:
    from ..validation import gates
    eff = gates.get_settings()["effective"]
    return {"g1_ic_t": float(eff["g1_ic_t"]), "g7_min_trades": int(eff["g7_min_trades"]),
            "g8_capacity_multiple": float(eff["g8_capacity_multiple"])}


def _crypto_only(card: dict) -> bool:
    try:
        from ..validation import families
        return families.universe_kind(card.get("spec") or {}) == "crypto"
    except Exception:     # noqa: BLE001 — unknown universe: not treated as crypto (the check then needs a value)
        return False


def _check(key, status, value, threshold, explanation) -> dict:
    return {"key": key, "status": status, "value": value, "threshold": threshold, "explanation": explanation}


def pre_checks(card: dict, th: dict) -> List[dict]:
    m = card.get("metrics") or {}
    perf, bench = m.get("performance") or {}, m.get("benchmark") or {}
    out = []
    n = perf.get("trades_closed")
    if _num(n):
        out.append(_check("trades", "pass" if n >= th["g7_min_trades"] else "fail", int(n), th["g7_min_trades"],
                          f"{int(n)} closed trades (pre-check needs ≥ {th['g7_min_trades']})"))
    else:
        out.append(_check("trades", "fail", None, th["g7_min_trades"], "closed trades could not be counted"))
    sq = m.get("signal_quality") or {}
    if sq.get("mode") == "cross_sectional":
        ts = [(h.get("t_stat"), h.get("horizon")) for h in sq.get("horizons") or []
              if h.get("horizon") in IC_HORIZONS and _num(h.get("t_stat"))]
        if ts:
            best, hor = max(ts, key=lambda x: (x[0], -x[1]))
            out.append(_check("signal", "pass" if best >= th["g1_ic_t"] else "fail", round(float(best), 4),
                              th["g1_ic_t"], f"best IC t-stat {best:.2f} at {hor} days (naive t; pre-check, not G1)"))
        else:
            out.append(_check("signal", "fail", None, th["g1_ic_t"], "no IC t-stat could be computed"))
    elif sq.get("mode") == "time_series":
        ps = sq.get("positive_share")
        if _num(ps):
            out.append(_check("signal", "pass" if ps > TS_POSITIVE_SHARE_MIN else "fail", round(float(ps), 4),
                              TS_POSITIVE_SHARE_MIN, f"positive 13-week signal/return windows {ps * 100:.1f} %"))
        else:
            out.append(_check("signal", "fail", None, TS_POSITIVE_SHARE_MIN, "the signal quality could not be computed"))
    else:
        out.append(_check("signal", "fail", None, th["g1_ic_t"],
                          "the signal quality could not be computed: " + str(sq.get("reason") or "no signal panel")))
    a = bench.get("alpha_annual")
    if _num(a):
        out.append(_check("alpha", "pass" if a > 0 else "fail", round(float(a), 6), 0.0,
                          f"annual alpha {a * 100:.2f} % vs the benchmark"))
    else:
        out.append(_check("alpha", "fail", None, 0.0, "alpha vs the benchmark could not be computed"))
    cap = m.get("capacity") or {}
    capital = cap.get("capital") if _num(cap.get("capital")) else (card.get("request") or {}).get("capital")
    need = th["g8_capacity_multiple"] * float(capital) if _num(capital) else None
    if _crypto_only(card):
        out.append(_check("capacity", "n/a", None, need, "not applicable — no capacity measurement for crypto"))
    elif _num(cap.get("capacity_usd")) and need is not None:
        c = float(cap["capacity_usd"])
        out.append(_check("capacity", "pass" if c >= need else "fail", round(c, 2), round(need, 2),
                          f"capacity estimate ${c:,.0f} (needs ≥ ${need:,.0f})"))
    else:
        out.append(_check("capacity", "fail", None, None if need is None else round(need, 2),
                          "no capacity estimate: " + str(cap.get("reason") or "unknown")))
    return out


def score(card: dict, th: Optional[dict] = None) -> dict:
    """The score card of a done backtest card (see the module docstring)."""
    holdout_guard(card)
    th = th or thresholds()
    m = card.get("metrics") or {}
    perf, expo, costs = m.get("performance") or {}, m.get("exposure") or {}, m.get("costs") or {}
    notes = []
    sharpe = perf.get("sharpe")
    turnover = expo.get("turnover_annual")
    if _num(turnover):
        p_t = min(TURNOVER_PENALTY_CAP, TURNOVER_PENALTY * max(0.0, float(turnover) - TURNOVER_FREE))
    else:
        p_t = 0.0
        notes.append("turnover unavailable — no turnover penalty")
    dd = perf.get("max_drawdown")
    p_d = DRAWDOWN_PENALTY * max(0.0, float(dd) - DRAWDOWN_FREE) if _num(dd) else 0.0
    if not _num(dd):
        notes.append("max drawdown unavailable — no drawdown penalty")
    share = costs.get("pct_of_gross_pnl")
    if _num(share) and _num(costs.get("gross_pnl")) and costs["gross_pnl"] > 0:
        p_c = min(COST_PENALTY_CAP, COST_PENALTY * max(0.0, float(share) / 100.0 - COST_FREE))
    else:
        p_c = 0.0
        notes.append("cost share unavailable (gross P&L ≤ 0) — no cost penalty")
    checks = pre_checks(card, th)
    failed = [c["key"] for c in checks if c["status"] == "fail"]
    out = {"version": SCORE_VERSION, "run_id": card.get("id"), "score": None, "reason": None,
           "sharpe": float(sharpe) if _num(sharpe) else None,
           "penalties": {"turnover": round(p_t, 6), "drawdown": round(p_d, 6), "costs": round(p_c, 6),
                         "checks": round(CHECK_PENALTY * len(failed), 6)},
           "inputs": {"turnover_annual": turnover if _num(turnover) else None,
                      "max_drawdown": dd if _num(dd) else None, "cost_share_pct": share if _num(share) else None,
                      "total_return": perf.get("total_return"), "cagr": perf.get("cagr"),
                      "trades_closed": perf.get("trades_closed")},
           "checks": checks, "failed_checks": failed, "checks_passed": not failed, "notes": notes,
           "range": {k: (card.get("range") or {}).get(k) for k in ("start", "end")}, "thresholds": th}
    if not _num(sharpe):
        out["reason"] = "not scorable: " + str(((m.get("unavailable") or {}).get("performance.sharpe"))
                                                or "the Sharpe ratio is unavailable")
        return out
    out["score"] = round(float(sharpe) - p_t - p_d - p_c - CHECK_PENALTY * len(failed), 6)
    return out


def score_facts(sc: dict, prefix: str = "score") -> List[dict]:
    """The score card as §11-style facts ({id, label, value, unit, display}) for the diagnosis prompt and the
    number check."""
    out = []

    def add(fid, label, value, unit, display):
        if value is not None:
            out.append({"id": f"{prefix}.{fid}", "label": label, "value": value, "unit": unit, "display": display})

    if sc.get("score") is not None:
        add("value", "loop score", sc["score"], "number", f"{sc['score']:.2f}")
    if sc.get("sharpe") is not None:
        add("sharpe", "net Sharpe ratio", sc["sharpe"], "number", f"{sc['sharpe']:.2f}")
    for k, v in (sc.get("penalties") or {}).items():
        add(f"penalty.{k}", f"{k} penalty", v, "number", f"{v:.2f}")
    for c in sc.get("checks") or []:
        add(f"check.{c['key']}.status", f"pre-check {c['key']}", c["status"], "status", c["status"])
        if _num(c.get("value")):
            add(f"check.{c['key']}.value", f"pre-check {c['key']} value", c["value"], "number", f"{c['value']:.2f}")
        if _num(c.get("threshold")):
            add(f"check.{c['key']}.threshold", f"pre-check {c['key']} threshold", c["threshold"], "number",
                f"{c['threshold']:.2f}")
    inp = sc.get("inputs") or {}
    if _num(inp.get("turnover_annual")):
        add("turnover_annual", "turnover per year", inp["turnover_annual"], "number", f"{inp['turnover_annual']:.2f}")
    if _num(inp.get("max_drawdown")):
        add("max_drawdown", "max drawdown", inp["max_drawdown"], "fraction", f"{inp['max_drawdown'] * 100:.2f} %")
    if _num(inp.get("cost_share_pct")):
        add("cost_share", "costs as share of gross P&L", inp["cost_share_pct"], "percent",
            f"{inp['cost_share_pct']:.2f} %")
    return out


def projected_g4(family_id: Optional[str], planned_backtests: int, planned_validations: int = 0) -> dict:
    """{trials_now, planned_backtests, validations_planned, trials_projected, sr0_now, sr0_projected,
    trial_variance, variance_source, years}. Every planned backtest AND finalist validation is one more trial
    (review L7); holdout cards never enter the variance or the sample length."""
    import json
    from ..storage import db
    from ..validation import families, stats
    trials = families.trial_count(family_id) if family_id else 0
    sharpes: List[float] = []
    years = None
    for r in (db.family_done_runs(family_id) if family_id else []):
        if r.get("kind") == "holdout" or not r.get("metrics_json"):
            continue
        try:
            m = json.loads(r["metrics_json"]) or {}
        except ValueError:
            continue
        perf = m.get("performance") or {}
        if _num(perf.get("sharpe")):
            sharpes.append(float(perf["sharpe"]))
        if _num(perf.get("years")) and perf["years"] > 0:
            years = float(perf["years"])                     # the newest non-holdout card wins
    years = years or 1.0
    vmin = 1.0 / years
    v, src = vmin, "minimum"
    if len(sharpes) >= 2:
        mu = sum(sharpes) / len(sharpes)
        sv = sum((x - mu) ** 2 for x in sharpes) / (len(sharpes) - 1)
        if sv >= vmin:
            v, src = sv, "trials"
    planned = max(0, int(planned_backtests))
    vals = max(0, int(planned_validations))

    def sr0(n):
        return round(stats.expected_max_sharpe(max(1, n), v), 4)

    return {"trials_now": trials, "planned_backtests": planned, "validations_planned": vals,
            "trials_projected": trials + planned + vals, "sr0_now": sr0(trials + 1),
            "sr0_projected": sr0(trials + planned + vals + 1), "trial_variance": round(v, 6),
            "variance_source": src, "years": round(years, 3),
            "note": ("The deflated-Sharpe gate (G4) compares a strategy's Sharpe against the expected maximum "
                     "Sharpe of the family's trials; every loop backtest and validation raises that bar.")}
