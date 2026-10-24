"""Validation gates G1–G8 and the holdout gate H (SPEC §9.4) plus the gate
settings (``GET/PUT /v2/settings/gates``).

Every gate returns ``{gate, name, status (pass | fail), value, threshold,
explanation, details}`` (``exception`` is only set later by report.py when
the user overrides a failed G1–G8). A metric that cannot be computed ->
``fail`` with the reason — never a default pass — except G3 for
time-series strategies ("not applicable", counts as pass with that note).

Rules (Standard thresholds; Lenient / Strict in the presets below):
- G1 Consistency — cross-sectional (>= 5 symbols): the Newey-West t-stat of
  the mean daily rank IC (lag = max(Newey-West rule, h - 1) because h-day
  forward returns overlap; the run card's naive t ignores the overlap) at
  the BEST of the horizons 1/5/10/21 >= ``g1_ic_t`` AND the fold mean IC
  (same horizon) > 0 in >= ``g1_folds_min`` of 5 folds. Time-series
  (< 5 symbols, the backtest/metrics split): fold Sharpe > 0 in >=
  ``g1_folds_min`` of 5 folds. value = best t (cross-sectional) / folds
  with Sharpe > 0 (time-series).
- G2 Alpha — OLS of daily strategy on benchmark returns, alpha > 0 AND its
  Newey-West t-stat >= ``g2_alpha_t`` (stats.alpha_newey_west).
- G3 Signal decay — IC half-life (run card signal quality, OLS fit of
  ln|IC(h)|) >= the average holding period of closed trades (FIFO lots of
  backtest/metrics.match_trades, sessions from entry to exit day). An IC
  that does not decay over the measured lags (fitted lambda <= 0) has an
  unbounded half-life -> pass (value null, stated) ONLY when the 1-day IC
  is significantly positive (mean > 0, NW t >= ``g1_ic_t``), else fail
  (review 2026-10-09: flat noise ICs). Time-series strategies:
  pass "not applicable — single-symbol strategy" (fewer than 5 symbols: no
  cross-sectional IC exists).
- G4 Deflated Sharpe — DSR probability (stats.deflated_sharpe with the
  family trial count + the trials' Sharpe variance) >= ``g4_dsr_min``.
- G5 Overfitting — PBO (CSCV, S = 10) over the parameter grid <=
  ``g5_pbo_max``; < 4 configurations -> fail "needs ≥ 4 parameter
  configurations (add min/max/step to parameters)".
- G6 Robustness — all of: (a) every grid neighbour (±20 %, grid.py) keeps a
  Sharpe >= ``g6_neighbour_ratio`` x the base Sharpe (base Sharpe must be
  > 0; no neighbour in the grid or a neighbour run that failed -> fail);
  (b) the costs x ``g6_cost_multiplier`` run (fee AND slippage scaled, per
  asset class — backtest/stress.scaled_costs) has a net Sharpe > 0;
  (c) each AVAILABLE stress episode (outside the holdout window): strategy
  max drawdown <= ``g6_episode_dd_ratio`` x the benchmark's in that episode
  (no available episode -> (c) holds vacuously, stated in the details).
  value = the worst neighbour Sharpe ratio.
- G7 Sample size — closed trades >= ``g7_min_trades``.
- G8 Capacity — capacity estimate (run card) >= ``g8_capacity_multiple`` x
  the tested capital; no estimate (e.g. the limit already binds) -> fail
  with the run card's reason. Crypto-only universes: "not applicable"
  (pass) with ``G8_CRYPTO_NOTE`` (user decision 2026-10-09); mixed
  universes: the estimate covers the equity orders only.
- H Holdout — on the holdout (or unseen) window: net Sharpe > 0 AND >=
  ``H_TRAINING_RATIO`` (50 %) of the training Sharpe AND mean IC > 0 at the
  horizon G1 selected (time-series: Sharpe only). Fixed constants — not in
  the presets, not overridable, never an exception.

Settings: ``{preset: standard|lenient|strict, overrides: {key: number}}``
stored in hedgefund.db meta ``GATE_SETTINGS_KEY`` (JSON), every change
audited (actor ``user``, action ``gate_settings_changed``, old + new). No
stored value = the user's documented default preset ``standard``
(interview 2026-10-09), not a fallback; an unreadable stored value ->
``StorageUnavailable`` (never silently reset).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import json
import math
import threading
from typing import Dict, List, Optional

GATE_NAMES = {"G1": "Consistency", "G2": "Alpha", "G3": "Signal decay", "G4": "Deflated Sharpe",
              "G5": "Overfitting (PBO)", "G6": "Robustness", "G7": "Sample size", "G8": "Capacity",
              "H": "Holdout"}
TRAINING_GATES = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")
PRESET_NAMES = ("lenient", "standard", "strict")
DEFAULT_PRESET = "standard"          # user decision 2026-10-09 (SPEC §9 interview)
PRESETS: Dict[str, Dict[str, float]] = {
    "lenient": {"g1_ic_t": 1.5, "g1_folds_min": 3, "g2_alpha_t": 1.5, "g4_dsr_min": 0.90, "g5_pbo_max": 0.50,
                "g6_neighbour_ratio": 0.40, "g6_cost_multiplier": 2.0, "g6_episode_dd_ratio": 1.5,
                "g7_min_trades": 50, "g8_capacity_multiple": 10.0},
    "standard": {"g1_ic_t": 2.0, "g1_folds_min": 4, "g2_alpha_t": 2.0, "g4_dsr_min": 0.95, "g5_pbo_max": 0.30,
                 "g6_neighbour_ratio": 0.50, "g6_cost_multiplier": 2.0, "g6_episode_dd_ratio": 1.5,
                 "g7_min_trades": 100, "g8_capacity_multiple": 10.0},
    "strict": {"g1_ic_t": 3.0, "g1_folds_min": 5, "g2_alpha_t": 3.0, "g4_dsr_min": 0.99, "g5_pbo_max": 0.20,
               "g6_neighbour_ratio": 0.60, "g6_cost_multiplier": 3.0, "g6_episode_dd_ratio": 1.5,
               "g7_min_trades": 200, "g8_capacity_multiple": 10.0},
}
# key -> (minimum, maximum, integer) — the validated ranges of PUT /v2/settings/gates overrides
THRESHOLD_RANGES = {
    "g1_ic_t": (0.0, 10.0, False), "g1_folds_min": (1, 5, True), "g2_alpha_t": (0.0, 10.0, False),
    "g4_dsr_min": (0.5, 0.999, False), "g5_pbo_max": (0.01, 1.0, False), "g6_neighbour_ratio": (0.0, 1.0, False),
    "g6_cost_multiplier": (1.0, 5.0, False), "g6_episode_dd_ratio": (1.0, 5.0, False),
    "g7_min_trades": (1, 10000, True), "g8_capacity_multiple": (1.0, 1000.0, False),
}
H_TRAINING_RATIO = 0.50
GATE_SETTINGS_KEY = "validation/gate_settings"
K_FOLDS = 5
_LOCK = threading.Lock()


# --- settings -------------------------------------------------------------------------------------

def check_settings(body) -> Dict[str, str]:
    """{field: message} for a PUT body {preset, overrides?}; empty when valid."""
    if not isinstance(body, dict):
        return {"_": "expected a JSON object {preset, overrides?}"}
    errors = {k: "unknown field" for k in sorted(set(body) - {"preset", "overrides"})}
    if "preset" not in body:
        errors["preset"] = "required"
    elif body["preset"] not in PRESET_NAMES:
        errors["preset"] = "one of " + ", ".join(PRESET_NAMES)
    ov = body.get("overrides", {})
    if not isinstance(ov, dict):
        errors["overrides"] = "expected an object {key: number}"
        return errors
    for k, v in ov.items():
        rng = THRESHOLD_RANGES.get(k)
        if rng is None:
            errors[f"overrides.{k}"] = "unknown threshold (one of " + ", ".join(sorted(THRESHOLD_RANGES)) + ")"
            continue
        lo, hi, integer = rng
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
            errors[f"overrides.{k}"] = "expected a number"
        elif integer and not float(v).is_integer():
            errors[f"overrides.{k}"] = "expected an integer"
        elif not lo <= float(v) <= hi:
            errors[f"overrides.{k}"] = f"{lo:g} … {hi:g}"
    return errors


def effective(preset: str, overrides: Optional[dict] = None) -> Dict[str, float]:
    """The thresholds of ``preset`` with ``overrides`` applied (integers kept integral)."""
    out = dict(PRESETS[preset])
    for k, v in (overrides or {}).items():
        out[k] = int(v) if THRESHOLD_RANGES[k][2] else float(v)
    return out


def _with_effective(s: dict) -> dict:
    return {"preset": s["preset"], "overrides": dict(s.get("overrides") or {}),
            "effective": effective(s["preset"], s.get("overrides")), "updated_at": s.get("updated_at"),
            "presets": PRESETS, "ranges": {k: {"min": a, "max": b, "integer": c}
                                           for k, (a, b, c) in THRESHOLD_RANGES.items()}}


def get_settings() -> dict:
    """{preset, overrides, effective, updated_at, presets, ranges}."""
    from ..storage import db
    raw = db.meta_get(GATE_SETTINGS_KEY)
    if raw is None:
        return _with_effective({"preset": DEFAULT_PRESET, "overrides": {}, "updated_at": None})
    try:
        s = json.loads(raw)
        if not isinstance(s, dict) or check_settings({"preset": s.get("preset"), "overrides": s.get("overrides", {})}):
            raise ValueError("invalid")
    except ValueError:
        raise db.StorageUnavailable("the stored gate settings in hedgefund.db are unreadable — save them again "
                                    "in Settings → Validation gates") from None
    return _with_effective(s)


def put_settings(body: dict, actor: str = "user") -> dict:
    """Validates (ValueError with the field messages as JSON), stores and audits the settings -> get_settings()."""
    from ..storage import db
    errors = check_settings(body)
    if errors:
        raise ValueError(json.dumps(errors))
    new = {"preset": body["preset"], "overrides": {k: (int(v) if THRESHOLD_RANGES[k][2] else float(v))
                                                    for k, v in sorted((body.get("overrides") or {}).items())},
           "updated_at": db.utc_now_iso()}
    with _LOCK:
        old = db.meta_get(GATE_SETTINGS_KEY)
        db.meta_set(GATE_SETTINGS_KEY, json.dumps(new, sort_keys=True))
        db.add_audit(actor, "gate_settings_changed", {"old": json.loads(old) if old else None, "new": new})
    return get_settings()


# --- gate helpers ---------------------------------------------------------------------------------

def _gate(gate: str, status: str, value, threshold, explanation: str, details: Optional[dict] = None) -> dict:
    return {"gate": gate, "name": GATE_NAMES[gate], "status": status, "value": value, "threshold": threshold,
            "explanation": explanation, "details": details or {}}


def _fmt(x, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def g1(inp: dict, th: dict) -> dict:
    mode = inp.get("mode")
    need = int(th["g1_folds_min"])
    folds = inp.get("folds") or []
    if mode is None:
        return _gate("G1", "fail", None, th["g1_ic_t"], "Cannot be computed: the signal panel of the training "
                     "backtest is not available.", {})
    if mode == "time_series":
        known = [f for f in folds if f.get("sharpe") is not None]
        pos = sum(1 for f in known if f["sharpe"] > 0)
        det = {"mode": mode, "folds_positive": pos, "folds_required": need, "folds": len(folds),
               "fold_sharpes": [f.get("sharpe") for f in folds]}
        if len(folds) != K_FOLDS:
            return _gate("G1", "fail", None, need, f"Cannot be computed: needs {K_FOLDS} folds.", det)
        ok = pos >= need
        return _gate("G1", "pass" if ok else "fail", pos, need,
                     f"Time-series strategy: the Sharpe ratio is positive in {pos} of {K_FOLDS} folds "
                     f"(needs {need}).", det)
    tests = inp.get("ic_tests") or {}
    best_h = inp.get("best_horizon")
    det = {"mode": mode, "horizons": [dict(t, horizon=int(h)) for h, t in sorted(tests.items(), key=lambda kv: int(kv[0]))],
           "best_horizon": best_h, "folds_required": need}
    if best_h is None:
        reasons = "; ".join(f"{h}d: {t.get('reason')}" for h, t in sorted(tests.items(), key=lambda kv: int(kv[0])))
        return _gate("G1", "fail", None, th["g1_ic_t"], "Cannot be computed: no horizon has a rank-IC t-stat"
                     + (f" ({reasons})." if reasons else "."), det)
    t = tests[best_h]["value"]
    known = [f for f in folds if f.get("mean_ic") is not None]
    pos = sum(1 for f in known if f["mean_ic"] > 0)
    det.update({"folds_positive": pos, "fold_mean_ic": [f.get("mean_ic") for f in folds]})
    if len(folds) != K_FOLDS:
        return _gate("G1", "fail", t, th["g1_ic_t"], f"Cannot be computed: needs {K_FOLDS} folds.", det)
    ok = t >= th["g1_ic_t"] and pos >= need
    return _gate("G1", "pass" if ok else "fail", t, th["g1_ic_t"],
                 f"Daily rank IC at {best_h} day(s): Newey-West t-stat {_fmt(t)} (needs ≥ {th['g1_ic_t']:g}); "
                 f"mean IC positive in {pos} of {K_FOLDS} folds (needs {need}).", det)


def g2(inp: dict, th: dict) -> dict:
    a = inp.get("alpha") or {}
    t = a.get("value")
    det = {k: a.get(k) for k in ("alpha_daily", "alpha_annual", "beta", "se", "lag", "n", "reason")}
    det["benchmark"] = inp.get("benchmark")
    if t is None:
        return _gate("G2", "fail", None, th["g2_alpha_t"], f"Cannot be computed: {a.get('reason') or 'no alpha'}.",
                     det)
    ok = a["alpha_daily"] > 0 and t >= th["g2_alpha_t"]
    return _gate("G2", "pass" if ok else "fail", t, th["g2_alpha_t"],
                 f"Alpha vs {inp.get('benchmark') or 'the benchmark'}: {_fmt(100 * a['alpha_annual'])} % per year, "
                 f"Newey-West t-stat {_fmt(t)} (needs alpha > 0 and t ≥ {th['g2_alpha_t']:g}).", det)


def g3(inp: dict, th: dict) -> dict:
    mode = inp.get("mode")
    hl = inp.get("half_life") or {}
    hold = inp.get("holding") or {}
    det = {"mode": mode, "half_life_days": hl.get("value"), "half_life_reason": hl.get("reason"),
           "half_life_fit": hl.get("fit"), "avg_holding_days": hold.get("value"), "trades": hold.get("trades"),
           "holding_reason": hold.get("reason")}
    if mode == "time_series":
        det["not_applicable"] = True
        return _gate("G3", "pass", None, None, "Not applicable — single-symbol strategy (fewer than 5 symbols: no "
                     "cross-sectional IC, so no IC half-life).", det)
    if mode is None:
        return _gate("G3", "fail", None, None, "Cannot be computed: the signal panel is not available.", det)
    if hold.get("value") is None:
        return _gate("G3", "fail", None, None, f"Cannot be computed: {hold.get('reason') or 'no closed trade'}.", det)
    fit = hl.get("fit") or {}
    if hl.get("value") is None:
        if fit.get("lambda") is not None and fit["lambda"] <= 0:
            det["does_not_decay"] = True
            # review 2026-10-09: a flat noise IC also "does not decay" — only a significantly POSITIVE 1-day IC
            # (Newey-West t >= g1_ic_t) may pass as an unbounded half-life
            t1 = (inp.get("ic_tests") or {}).get(1) or {}
            det["ic_1d_t"], det["ic_1d_mean"] = t1.get("value"), t1.get("mean")
            if t1.get("value") is None or t1.get("mean") is None or t1["mean"] <= 0 or t1["value"] < th["g1_ic_t"]:
                return _gate("G3", "fail", None, hold["value"], "The IC shows no decay, but the 1-day IC is not "
                             f"significantly positive (mean {_fmt(t1.get('mean'), 4)}, Newey-West t "
                             f"{_fmt(t1.get('value'))}, needs > 0 and t ≥ {th['g1_ic_t']:g}) — a flat IC around zero "
                             "is no persistent signal.", det)
            return _gate("G3", "pass", None, hold["value"], "The IC does not decay over the measured lags (unbounded "
                         f"half-life) — longer than the average holding period of {_fmt(hold['value'], 1)} days.", det)
        return _gate("G3", "fail", None, hold["value"], f"Cannot be computed: {hl.get('reason') or 'no half-life'}.",
                     det)
    ok = hl["value"] >= hold["value"]
    return _gate("G3", "pass" if ok else "fail", hl["value"], hold["value"],
                 f"IC half-life {_fmt(hl['value'], 1)} days vs an average holding period of "
                 f"{_fmt(hold['value'], 1)} days (the signal must last at least as long as the positions).", det)


def g4(inp: dict, th: dict) -> dict:
    d = inp.get("dsr") or {}
    det = {k: d.get(k) for k in ("sharpe_annual", "sr0_annual", "n_trials", "trial_variance", "variance_source",
                                 "trials_used", "skew", "kurtosis", "returns", "reason")}
    if d.get("value") is None:
        return _gate("G4", "fail", None, th["g4_dsr_min"], f"Cannot be computed: {d.get('reason') or 'no DSR'}.", det)
    ok = d["value"] >= th["g4_dsr_min"]
    return _gate("G4", "pass" if ok else "fail", d["value"], th["g4_dsr_min"],
                 f"Probability that the true Sharpe exceeds the best of {d['n_trials']} unskilled trial(s) "
                 f"(expected maximum {_fmt(d['sr0_annual'])}): {_fmt(d['value'], 3)} "
                 f"(needs ≥ {th['g4_dsr_min']:g}).", det)


def g5(inp: dict, th: dict) -> dict:
    p = inp.get("pbo") or {}
    det = {k: p.get(k) for k in ("configs", "rows", "blocks", "combinations", "overfit_combinations", "mean_logit",
                                 "reason")}
    if p.get("configs", 0) < 4:
        return _gate("G5", "fail", None, th["g5_pbo_max"], "needs ≥ 4 parameter configurations (add min/max/step "
                     "to parameters)", det)
    if p.get("value") is None:
        return _gate("G5", "fail", None, th["g5_pbo_max"], f"Cannot be computed: {p.get('reason') or 'no PBO'}.", det)
    ok = p["value"] <= th["g5_pbo_max"]
    return _gate("G5", "pass" if ok else "fail", p["value"], th["g5_pbo_max"],
                 f"Probability of backtest overfitting over {p['configs']} parameter configurations: "
                 f"{_fmt(p['value'], 3)} (needs ≤ {th['g5_pbo_max']:g}).", det)


def g6(inp: dict, th: dict) -> dict:
    grid = inp.get("grid") or {}
    base = grid.get("base_sharpe")
    nb = grid.get("neighbours") or []
    sharpes = grid.get("sharpes") or []
    reasons_fail, notes = [], []
    worst = None
    nb_rows = []
    if base is None:
        reasons_fail.append("the base Sharpe is not available")
    elif base <= 0:
        reasons_fail.append(f"the base Sharpe {_fmt(base)} is not positive")
    if not nb:
        reasons_fail.append("no grid neighbour within ±20 % of every parameter (add a finer step)")
    for i in nb:
        s = sharpes[i] if i < len(sharpes) else None
        ratio = s / base if (s is not None and base is not None and base > 0) else None
        nb_rows.append({"index": i, "params": (grid.get("configs") or [{}] * (i + 1))[i], "sharpe": s,
                        "ratio": ratio})
        if s is None:
            reasons_fail.append(f"neighbour #{i} has no Sharpe ({(grid.get('reasons') or {}).get(i) or 'run failed'})")
        elif ratio is not None:
            worst = ratio if worst is None else min(worst, ratio)
    if worst is not None and worst < th["g6_neighbour_ratio"]:
        reasons_fail.append(f"a neighbour keeps only {_fmt(100 * worst, 0)} % of the base Sharpe "
                            f"(needs {th['g6_neighbour_ratio'] * 100:g} %)")
    c = inp.get("costs") or {}
    if c.get("sharpe") is None:
        reasons_fail.append(f"costs ×{th['g6_cost_multiplier']:g}: {c.get('reason') or 'not computed'}")
    elif c["sharpe"] <= 0:
        reasons_fail.append(f"costs ×{th['g6_cost_multiplier']:g}: net Sharpe {_fmt(c['sharpe'])} ≤ 0")
    ep_rows = []
    for e in inp.get("episodes") or []:
        row = dict(e)
        if e.get("status") != "done":
            row["check"] = "skipped"
        elif e.get("strategy_dd") is None or e.get("benchmark_dd") is None:
            row["check"] = "fail"
            reasons_fail.append(f"{e.get('label')}: drawdown not computable")
        else:
            lim = th["g6_episode_dd_ratio"] * e["benchmark_dd"]
            row["limit"] = lim
            row["check"] = "pass" if e["strategy_dd"] <= lim + 1e-12 else "fail"
            if row["check"] == "fail":
                reasons_fail.append(f"{e.get('label')}: max drawdown {_fmt(100 * e['strategy_dd'], 1)} % > "
                                    f"{th['g6_episode_dd_ratio']:g} × the benchmark's {_fmt(100 * e['benchmark_dd'], 1)} %")
        ep_rows.append(row)
    if not any(r.get("check") in ("pass", "fail") for r in ep_rows):
        notes.append("no stress episode available outside the holdout window — the episode check holds vacuously")
    det = {"base_sharpe": base, "neighbours": nb_rows, "worst_ratio": worst, "costs": c, "episodes": ep_rows,
           "notes": notes, "failures": reasons_fail}
    ok = not reasons_fail
    expl = ("Neighbours keep ≥ {:g} % of the base Sharpe, costs ×{:g} stay profitable and episode drawdowns stay "
            "within {:g} × the benchmark's.".format(th["g6_neighbour_ratio"] * 100, th["g6_cost_multiplier"],
                                                    th["g6_episode_dd_ratio"])) if ok else \
        ("Not robust: " + "; ".join(reasons_fail) + ".")
    return _gate("G6", "pass" if ok else "fail", worst, th["g6_neighbour_ratio"], expl, det)


def g7(inp: dict, th: dict) -> dict:
    n = inp.get("trades_closed")
    need = int(th["g7_min_trades"])
    if n is None:
        return _gate("G7", "fail", None, need, "Cannot be computed: no trade count.", {})
    return _gate("G7", "pass" if n >= need else "fail", n, need,
                 f"{n} closed trades (needs ≥ {need}).", {"trades_closed": n, "trades_open": inp.get("trades_open")})


G8_CRYPTO_NOTE = ("No capacity measurement for crypto (user decision 2026-10-03: no volume limit for crypto)")


def g8(inp: dict, th: dict) -> dict:
    cap = inp.get("capacity") or {}
    capital = cap.get("capital")
    mult = th["g8_capacity_multiple"]
    det = {k: cap.get(k) for k in ("capacity_usd", "at_most", "capital", "orders", "orders_at_limit", "q90_ratio",
                                   "reason")}
    det["universe_kind"] = inp.get("universe_kind")
    if inp.get("universe_kind") == "crypto":
        # user decision 2026-10-09: crypto-only -> not applicable (counts as pass); mixed universes are measured
        # on their equity orders only (backtest/metrics.capacity excludes crypto fills)
        det["not_applicable"] = True
        return _gate("G8", "pass", None, None, f"Not applicable — {G8_CRYPTO_NOTE}.", det)
    need = mult * capital if capital else None
    if cap.get("capacity_usd") is None or not capital:
        return _gate("G8", "fail", None, need, f"Cannot be computed: {cap.get('reason') or 'no capacity estimate'}.",
                     det)
    ok = cap["capacity_usd"] >= need
    return _gate("G8", "pass" if ok else "fail", cap["capacity_usd"], need,
                 f"Capacity estimate ${cap['capacity_usd']:,.0f} vs {mult:g} × the tested capital "
                 f"(${need:,.0f}).", det)


GATE_FUNCS = (g1, g2, g3, g4, g5, g6, g7, g8)


def evaluate(inp: dict, thresholds: dict) -> List[dict]:
    """G1–G8 for the validation inputs (see jobs.py for the keys)."""
    return [f(inp, thresholds) for f in GATE_FUNCS]


def holdout_gate(*, sharpe: Optional[float], training_sharpe: Optional[float], mode: Optional[str],
                 mean_ic: Optional[float], horizon: Optional[int], sharpe_reason: Optional[str] = None,
                 ic_reason: Optional[str] = None, window: Optional[dict] = None) -> dict:
    """Gate H (module docstring) — never overridable."""
    det = {"sharpe": sharpe, "training_sharpe": training_sharpe, "required_ratio": H_TRAINING_RATIO, "mode": mode,
           "mean_ic": mean_ic, "horizon": horizon, "window": window, "sharpe_reason": sharpe_reason,
           "ic_reason": ic_reason, "exception_allowed": False}
    if sharpe is None:
        return _gate("H", "fail", None, None, f"Cannot be computed: {sharpe_reason or 'no Sharpe on the window'}.", det)
    if training_sharpe is None:
        return _gate("H", "fail", sharpe, None, "Cannot be computed: the training Sharpe of the validation run is "
                     "not available.", det)
    need = max(0.0, H_TRAINING_RATIO * training_sharpe)
    fails = []
    if sharpe <= 0:
        fails.append(f"net Sharpe {_fmt(sharpe)} ≤ 0")
    if sharpe < H_TRAINING_RATIO * training_sharpe:
        fails.append(f"Sharpe {_fmt(sharpe)} < 50 % of the training Sharpe {_fmt(training_sharpe)}")
    if mode == "cross_sectional":
        if mean_ic is None:
            fails.append(f"mean IC not computable ({ic_reason or 'no IC'})")
        elif mean_ic <= 0:
            fails.append(f"mean IC {_fmt(mean_ic, 4)} ≤ 0")
    elif mode is None:
        fails.append("the signal panel is not available")
    ok = not fails
    expl = (f"Holdout Sharpe {_fmt(sharpe)} vs training {_fmt(training_sharpe)}"
            + (f", mean IC {_fmt(mean_ic, 4)} at {horizon} day(s)" if mode == "cross_sectional" else "")
            + (" — passed." if ok else " — failed: " + "; ".join(fails) + "."))
    return _gate("H", "pass" if ok else "fail", sharpe, need, expl, det)
