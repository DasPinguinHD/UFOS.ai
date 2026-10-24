"""Validation and holdout jobs (SPEC §9.3, §9.5) on the EXISTING single-job
runner of ``backtest/jobs.py``.

Runner: the process-wide ``backtest.jobs.manager()`` holds at most ONE job.
These jobs use its public ``submit_work(create_card, work, …)`` (review fix
2026-10-09, replaces the earlier use of private members): ``JobBusy`` while
any job (backtest, validation or holdout) is active; the card is created
under the manager's lock; ``manager().progress(id)``, ``cancel(id)``,
``active_run_id()``, the 409 rule of POST /v2/backtests and
``recover_interrupted`` work unchanged for these cards. Outcomes as
backtests: cancellation -> ``runcard.cancel``, a ``UserFacingError`` ->
``runcard.fail(user_message)``, anything else -> traceback in backend.log +
a generic message. The holdout's ``create_card`` re-checks the
preconditions under that lock (no double open).

Validation job (run card kind ``validation``; request ``{history_from,
stress: true}``, other request keys null = engine defaults: capital
100,000, benchmark spec/SPY):
1. training backtest: ``engine.run(spec, end = families.training_end(spec),
   history_from)`` (+ ``metrics.compute``);
2. folds (``folds.py``) and the daily rank IC per horizon 1/5/10/21 with a
   Newey-West t-stat (lag max(rule, h - 1)); best horizon = the largest t;
3. parameter grid (``grid.py``): one backtest per configuration on the
   training run's simulated range (explicit start/end; the base
   configuration reuses the training run); a configuration that fails ->
   its Sharpe is null with the reason, PBO is then not computed (G5 fail)
   and a failed neighbour fails G6 — no partial grid is evaluated as if
   complete;
4. stress (``backtest.stress.run_stress``: episodes — V1 marks those inside
   the holdout window "not available" — costs x2, slippage x3,
   Monte-Carlo) plus a costs x m run when the G6 cost multiplier m != 2;
5. deflated Sharpe: N = the family's finished trials + 1 (this validation
   run, which counts once it is finished), trial Sharpes = the family's +
   this training Sharpe; PBO over the grid's daily returns on the days
   common to all configurations;
6. gates (``gates.py`` with the stored settings) -> report (``report.py``)
   -> ``runcard.finish(id, training result, metrics + {"validation":
   {passed, version, report}}, stress=…)``.

Holdout job (kind ``holdout``): needs a done validation run of the CURRENT
version whose report passes with exceptions (``report.effective_passed``;
the holdout opens once per family, so it is not wasted on a strategy that
cannot become validated) and ``families.holdout_eligibility`` eligible; the
backtest simulates exactly the eligible window (``engine.run(start, end,
holdout_access=True)``; the warm-up is loaded before the start and not
evaluated), gate H (``gates.holdout_gate``: training Sharpe and IC horizon
from that validation run) -> ``runcard.finish_holdout(..., metrics +
{"holdout": {passed, version, report}}, family_event={window, result})`` —
the card and the immutable holdout event (seen_until, holdout_failed_at) in
ONE transaction (V1 review fix 2026-10-09): a failure writes neither.
A run that fails or is cancelled before its result exists records no
event (nothing of the window was shown).

Parallel compute + resumable runs (SPEC §10.2/§10.3, R2 notes): with a
``runner`` (``parallel.TaskRunner``) the grid configurations and the stress
scenarios run as tasks — inline (1 worker) or on the spawned process pool —
and are assembled in config/scenario order, so the report is identical to the
sequential path (``runner=None``, kept for the calibration tests). The stress
scenarios are those of ``stress.run_stress`` (episodes, locked episodes,
costs x2, slippage x3, Monte-Carlo in the parent) in its order. With
``checkpoints`` (``runcard.CheckpointStore``) each grid configuration
(``grid:<hash>``) and stress scenario (``stress:<key>:<hash>``) is stored
when it finishes and reused by a resumed attempt of the same chain (sha256
verified; a mismatch recomputes the step with a ``checkpoint_mismatch``
warning). The hash covers everything the step depends on (spec content,
range, capital, benchmark, seed). The job checks the basis of the first
start (``runcard.ensure_basis``) before every attempt and loads data through
``parallel.RecordingLoader`` (data checkpoints).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ..core.errors import UserFacingError

logger = logging.getLogger(__name__)

BUSY_MESSAGE = "A backtest or validation run is already running."
VALIDATION_FAILURE = "Validation failed — see backend.log"
HOLDOUT_FAILURE = "Holdout run failed — see backend.log"
CANCELLED_VALIDATION = "The validation run was cancelled."
CANCELLED_HOLDOUT = "The holdout run was cancelled."
PHASES = ("queued", "data", "simulate", "metrics", "folds", "grid", "stress", "gates", "saving")


class HoldoutRefused(Exception):
    """The holdout cannot be opened now (HTTP 409) — ``message`` + ``extra`` payload keys."""

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


def _field(result, key: str, default=None):
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


# --- pure computation --------------------------------------------------------------------------

def _returns_by_date(result) -> Dict[str, float]:
    from . import folds
    dates = list(_field(result, "dates") or [])
    return {dates[j]: r for j, r in folds.daily_returns(_field(result, "equity") or [])}


def _sharpe_reason(metrics: dict) -> Optional[str]:
    return ((metrics or {}).get("unavailable") or {}).get("performance.sharpe")


def ic_tests(ic: Dict[int, Dict[str, float]]) -> Dict[int, dict]:
    """{h: {value (NW t), mean, se, lag, n, reason}} per horizon."""
    from . import stats
    out = {}
    for h, series in ic.items():
        vals = [series[d] for d in sorted(series)]
        out[int(h)] = stats.mean_t_newey_west(vals, min_lag=int(h) - 1)
    return out


def best_horizon(tests: Dict[int, dict]) -> Optional[int]:
    ok = [(t["value"], -h, h) for h, t in tests.items() if t.get("value") is not None]
    return max(ok)[2] if ok else None


def holding_periods(result) -> dict:
    """{value (mean sessions from entry to exit day of closed trades), trades, unmapped, reason}."""
    from ..backtest import metrics
    from ..core import calendars
    dates = list(_field(result, "dates") or [])
    idx = {d: i for i, d in enumerate(dates)}
    cal = (_field(result, "range") or {}).get("calendar", "nyse")

    def day(ts):
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        tz = calendars.NY if cal == "nyse" else timezone.utc
        return dt.astimezone(tz).date().isoformat()

    closed = metrics.match_trades(_field(result, "fills") or [])["closed"]
    spans, unmapped = [], 0
    for t in closed:
        a, b = idx.get(day(t["entry_ts"])), idx.get(day(t["exit_ts"])) if t.get("exit_ts") else None
        if a is None or b is None:
            unmapped += 1
        else:
            spans.append(b - a)
    out = {"value": None, "trades": len(spans), "unmapped": unmapped, "reason": None}
    if not spans:
        out["reason"] = "no closed trade" if not closed else "no closed trade could be mapped to simulated days"
    else:
        out["value"] = math.fsum(spans) / len(spans)
    return out


def _episode_rows(stress_out: Optional[dict]) -> List[dict]:
    rows = []
    for s in (stress_out or {}).get("scenarios") or []:
        if s.get("kind") != "episode":
            continue
        m = s.get("metrics") or {}
        rows.append({"key": s.get("key"), "label": s.get("label"), "status": s.get("status"),
                     "reason": s.get("reason"), "window": s.get("window"),
                     "strategy_dd": (m.get("strategy") or {}).get("max_drawdown"),
                     "benchmark_dd": (m.get("benchmark") or {}).get("max_drawdown")})
    return rows


def _costs_row(scenario: Optional[dict], multiplier: float) -> dict:
    if scenario is None:
        return {"multiplier": multiplier, "status": None, "sharpe": None, "reason": "the costs scenario did not run"}
    m = scenario.get("metrics") or {}
    sharpe = (m.get("strategy") or {}).get("sharpe")
    reason = scenario.get("reason")
    if scenario.get("status") == "done" and sharpe is None:
        reason = ((m.get("unavailable") or {}).get("performance.sharpe")) or "the Sharpe ratio is not computable"
    return {"multiplier": multiplier, "status": scenario.get("status"), "sharpe": sharpe, "reason": reason,
            "key": scenario.get("key")}


def _step_hash(obj) -> str:
    import hashlib
    from . import parallel
    return hashlib.sha256(parallel.canonical_json(obj).encode("utf-8")).hexdigest()[:32]


def _put_checkpoint(checkpoints, key: str, payload) -> None:
    if checkpoints is None:
        return
    try:
        checkpoints.put(key, payload)
    except ValueError as ex:          # e.g. a non-finite number: the step simply has no checkpoint
        logger.warning("checkpoint %s not written: %s", key, ex)


def _strip_result(result):
    """The base run for worker tasks without the engine's analysis attachment (panels)."""
    import dataclasses
    # symbol_pnl (SPEC §11.1) is not needed by any task — left out to keep the shipped payload small
    if isinstance(result, dict):
        return {k: v for k, v in result.items() if k not in ("analysis", "symbol_pnl")}
    if dataclasses.is_dataclass(result):
        names = {f.name for f in dataclasses.fields(result)}
        return dataclasses.replace(result, symbol_pnl=None) if "symbol_pnl" in names else dataclasses.replace(result)
    return result


def _grid_series_runner(spec, g, base_rets, rng, cap, benchmark, runner, checkpoints):
    """(series per config, reasons) via the task runner + checkpoints (SPEC §10.2/§10.3)."""
    from ..strategy import schema
    from . import grid
    n_cfg = len(g["configs"])
    series: List[Optional[Dict[str, float]]] = [None] * n_cfg
    reasons: Dict[int, str] = {}
    tasks, where = [], []
    for i, cfg in enumerate(g["configs"]):
        if i == g["base_index"]:
            series[i] = base_rets
            continue
        spec_i = grid.spec_with(spec, cfg)
        key = "grid:" + _step_hash({"spec": schema.content_hash(spec_i), "start": rng.get("start"),
                                    "end": rng.get("end"), "capital": cap, "benchmark": benchmark})
        hit = checkpoints.get(key) if checkpoints is not None else None
        if hit is not None:
            series[i] = hit.get("series")
            if series[i] is None:
                reasons[i] = hit.get("reason")
            continue
        tasks.append(("grid", {"spec": spec_i}))
        where.append((i, key))

    def done(j, out):
        _put_checkpoint(checkpoints, where[j][1], out)

    outs = runner.run("grid", {"start": rng.get("start"), "end": rng.get("end"), "capital": cap,
                               "benchmark": benchmark}, tasks, label="parameter grid configuration",
                      on_result=done)
    for (i, _), out in zip(where, outs):
        series[i] = out["series"]
        if series[i] is None:
            reasons[i] = out["reason"]
    return series, reasons


def _stress_runner(spec, result, *, mult: float, seed, runner, checkpoints) -> dict:
    """``stress.run_stress`` + the G6 costs x m scenario as runner tasks, in run_stress's order."""
    from ..backtest import metrics as mm
    from ..backtest import stress
    from ..strategy import schema
    from . import families
    rng = _field(result, "range") or {}
    capital = _field(result, "request")["capital"]
    bench = _field(result, "benchmark_info")["symbol"]
    seed = stress.MC_SEED if seed is None else seed
    base = {"spec": schema.content_hash(spec), "range": [rng.get("start"), rng.get("end")], "capital": capital,
            "benchmark": bench}
    hs = stress.holdout_start_for(spec, result)
    slots: List[Optional[dict]] = []
    tasks, where = [], []

    def plan(task, payload):
        key = "stress:" + payload["key"] + ":" + _step_hash(dict(base, task=task, payload=payload))
        hit = checkpoints.get(key) if checkpoints is not None else None
        slots.append(hit)
        if hit is None:
            tasks.append((task, payload))
            where.append((len(slots) - 1, key))

    for key, label, a, b in stress.EPISODES:
        if b >= hs:
            slots.append(stress._scenario(key, "episode", label, status="not_available",
                                          reason=families.LOCKED_EPISODE_REASON,
                                          window={"start": a.isoformat(), "end": b.isoformat()}))
            continue
        plan("episode", {"key": key, "label": label, "start": a.isoformat(), "end": b.isoformat()})
    plan("costs", {"key": "costs_x2", "label": "Costs x2", "fee_mult": stress.COST_MULTIPLIER,
                   "slippage_mult": stress.COST_MULTIPLIER})
    plan("costs", {"key": "slippage_x3", "label": "Slippage x3", "fee_mult": 1.0,
                   "slippage_mult": stress.SLIPPAGE_MULTIPLIER})
    mc_at = len(slots)
    slots.append(None)
    if mult != 2.0:
        plan("costs", {"key": f"costs_x{mult:g}", "label": f"Costs x{mult:g}", "fee_mult": mult,
                       "slippage_mult": mult})

    def done(j, out):
        _put_checkpoint(checkpoints, where[j][1], out)

    shared = {"spec": spec, "base_result": _strip_result(result), "stress_capital": capital, "stress_benchmark": bench}
    outs = runner.run("stress", shared, tasks, label="stress scenario", on_result=done)
    for (at, _), out in zip(where, outs):
        slots[at] = out
    cal = rng["calendar"]
    rets = mm.returns_of(mm.valid_points(_field(result, "equity")))
    slots[mc_at] = stress.monte_carlo(rets, periods_per_year=mm.PERIODS_PER_YEAR[cal], seed=seed)
    return {"seed": seed, "scenarios": slots}


def run_validation(spec: dict, *, run_engine: Callable, compute_metrics: Callable, run_stress: Callable,
                   synthetic_costs: Callable, training_end, history_from=None, capital=None, benchmark=None,
                   data=None, thresholds: dict, settings: dict, n_trials_before: int,
                   trial_sharpes_before: List[float], holdout: Optional[dict] = None,
                   progress: Optional[Callable[[str, float, str], None]] = None,
                   cancel: Optional[Callable[[], bool]] = None, seed: Optional[int] = None,
                   runner=None, checkpoints=None, stress_tasks: bool = True) -> dict:
    """The whole validation computation (module docstring) -> {result, metrics, stress, report}.
    Engine/metrics/stress callables are injected (the job passes the real ones). ``runner``: grid and
    stress as ``parallel.TaskRunner`` tasks (SPEC §10.2; None = the sequential loops below);
    ``checkpoints``: a ``runcard.CheckpointStore`` (SPEC §10.3). ``stress_tasks`` False (tests that inject
    their own ``run_stress``): the stress part calls ``run_stress`` even with a runner."""
    from ..backtest.engine import BacktestCancelled, DEFAULT_CAPITAL
    from . import folds, gates, grid, report, stats

    def emit(phase, pct, msg):
        if progress is not None:
            progress(phase, pct, msg)

    def check():
        if cancel is not None and cancel():
            raise BacktestCancelled()

    cap = capital if capital is not None else DEFAULT_CAPITAL
    # 1. training backtest
    emit("data", 0.0, "training backtest")
    result = run_engine(spec, end=training_end, history_from=history_from, capital=cap, benchmark=benchmark,
                        progress=progress, cancel=cancel, data=data)
    check()
    emit("metrics", 0.0, "computing training metrics")
    m = compute_metrics(result)
    analysis = getattr(result, "analysis", None) if not isinstance(result, dict) else result.get("analysis")
    P = folds.periods_per_year(result)
    rng = _field(result, "range") or {}
    mode = folds.signal_mode(analysis)
    # 2. IC + folds
    check()
    emit("folds", 0.0, "walk-forward folds")
    tests, best_h, ic = {}, None, {}
    if mode == "cross_sectional":
        ic = folds.daily_ic(analysis)
        tests = ic_tests(ic)
        best_h = best_horizon(tests)
    fold_rows = folds.fold_metrics(result, ic=ic.get(best_h) if best_h else None, horizon=best_h,
                                   analysis=analysis)
    # 3. grid
    g = grid.build(spec)
    n_cfg = len(g["configs"])
    sharpes: List[Optional[float]] = [None] * n_cfg
    reasons: Dict[int, str] = {}
    series: List[Optional[Dict[str, float]]] = [None] * n_cfg
    base_rets = _returns_by_date(result)
    if runner is not None:
        check()
        series, reasons = _grid_series_runner(spec, g, base_rets, rng, cap, benchmark, runner, checkpoints)
    for i, cfg in enumerate(g["configs"]):
        check()
        if runner is not None:
            if series[i] is None:
                continue
        else:
            emit("grid", 100.0 * i / max(1, n_cfg), f"parameter grid {i + 1}/{n_cfg}")
            if i == g["base_index"]:
                series[i] = base_rets
            else:
                try:
                    res_i = run_engine(grid.spec_with(spec, cfg), start=rng.get("start"), end=rng.get("end"),
                                       capital=cap, benchmark=benchmark, cancel=cancel, data=data,
                                       record_symbol_pnl=False)          # SPEC §11.5 (L7)
                except BacktestCancelled:
                    raise
                except UserFacingError as ex:
                    reasons[i] = ex.user_message
                    continue
                series[i] = _returns_by_date(res_i)
        vals = [series[i][d] for d in sorted(series[i])]
        sharpes[i] = stats.sharpe(vals, P)
        if sharpes[i] is None:
            reasons[i] = "fewer than two returns or zero variance"
    emit("grid", 100.0, "parameter grid finished")
    failed = [i for i in range(n_cfg) if series[i] is None]
    if failed:
        pbo = {"value": None, "reason": f"{len(failed)} grid configuration(s) failed to run (first: #{failed[0]}: "
               f"{reasons[failed[0]]})", "configs": n_cfg, "rows": 0, "blocks": stats.PBO_BLOCKS,
               "combinations": 0, "logits": [], "overfit_combinations": 0, "mean_logit": None}
    else:
        import numpy as np
        common = sorted(set.intersection(*[set(x) for x in series])) if n_cfg else []
        matrix = np.array([[series[i][d] for i in range(n_cfg)] for d in common], dtype=float).reshape(
            len(common), n_cfg)
        pbo = stats.pbo_cscv(matrix, stats.PBO_BLOCKS)
    grid_out = {"axes": g["axes"], "fixed": g["fixed"], "total": g["total"], "subsampled": g["subsampled"],
                "max_configs": g["max_configs"], "base_index": g["base_index"], "base": g["base"],
                "neighbours": grid.neighbours(g, spec),
                "configs": [{"index": i, "params": c, "sharpe": sharpes[i], "reason": reasons.get(i),
                             "base": i == g["base_index"]} for i, c in enumerate(g["configs"])],
                "sharpes": sharpes, "reasons": reasons,
                "base_sharpe": sharpes[g["base_index"]] if n_cfg else None}
    if not n_cfg:
        grid_out["base_sharpe"] = (m.get("performance") or {}).get("sharpe")
    # 4. stress
    check()
    emit("stress", 0.0, "stress tests")
    mult = float(thresholds["g6_cost_multiplier"])
    use_tasks = runner is not None and stress_tasks
    stress_engine = run_engine
    if runner is not None:
        from . import parallel
        stress_engine = parallel._connection_guarded(run_engine)     # a lost connection stops the run
    if use_tasks:
        stress_out = _stress_runner(spec, result, mult=mult, seed=seed, runner=runner, checkpoints=checkpoints)
    else:
        kw = {"data": data, "progress": progress, "cancel": cancel, "run_engine": stress_engine}
        if seed is not None:
            kw["seed"] = seed
        stress_out = run_stress(spec, result, **kw)
    scen = {s.get("key"): s for s in stress_out.get("scenarios") or []}
    if mult == 2.0:
        cost_scen = scen.get("costs_x2")
    elif use_tasks:
        cost_scen = scen.get(f"costs_x{mult:g}")
    else:
        check()
        cost_scen = synthetic_costs(f"costs_x{mult:g}", f"Costs x{mult:g}", spec, result, mult, mult, data=data,
                                    cancel=cancel, run_engine=stress_engine)
        stress_out = dict(stress_out, scenarios=list(stress_out.get("scenarios") or []) + [cost_scen])
    # 5. DSR, alpha, decay
    check()
    emit("gates", 0.0, "evaluating gates")
    train_rets = [base_rets[d] for d in sorted(base_rets)]
    train_sharpe = (m.get("performance") or {}).get("sharpe")
    n_trials = int(n_trials_before) + 1
    trial_sharpes = list(trial_sharpes_before) + ([train_sharpe] if train_sharpe is not None else [])
    dsr = stats.deflated_sharpe(train_rets, P, n_trials, trial_sharpes)
    rs, rb = folds.benchmark_pairs(result)
    alpha = stats.alpha_newey_west(rs, rb, P)
    sq = m.get("signal_quality") or {}
    half = {"value": sq.get("half_life_days"), "reason": sq.get("half_life_reason") or sq.get("reason"),
            "fit": sq.get("half_life_fit")}
    perf = m.get("performance") or {}
    from . import families
    inp = {"mode": mode, "universe_kind": families.universe_kind(spec), "ic_tests": tests, "best_horizon": best_h, "folds": fold_rows, "alpha": alpha,
           "benchmark": (_field(result, "benchmark_info") or {}).get("symbol"), "half_life": half,
           "holding": holding_periods(result), "dsr": dsr, "pbo": pbo, "grid": grid_out,
           "costs": _costs_row(cost_scen, mult), "episodes": _episode_rows(stress_out),
           "trades_closed": perf.get("trades_closed"), "trades_open": perf.get("trades_open"),
           "capacity": m.get("capacity") or {"reason": "no capacity estimate in the metrics"}}
    gate_rows = gates.evaluate(inp, thresholds)
    rep = report.build(
        gates=gate_rows, mode=mode, folds=fold_rows, grid=grid_out, pbo=pbo, dsr=dsr,
        stress=stress_out, trial_count=n_trials,
        thresholds={"preset": settings.get("preset"), "overrides": settings.get("overrides") or {},
                    "effective": thresholds},
        training={"range": rng, "sharpe": train_sharpe, "sharpe_reason": _sharpe_reason(m),
                  "returns": len(train_rets), "periods_per_year": P,
                  "benchmark": inp["benchmark"], "alpha": alpha, "holding": inp["holding"]},
        ic={"best_horizon": best_h, "horizons": [dict(t, horizon=h) for h, t in sorted(tests.items())]},
        holdout=holdout)
    emit("gates", 100.0, "gates evaluated")
    return {"result": result, "metrics": m, "stress": stress_out, "report": rep}


def run_holdout(spec: dict, *, run_engine: Callable, compute_metrics: Callable, window: dict, training_sharpe,
                horizon: Optional[int], capital=None, benchmark=None, data=None, eligibility: Optional[dict] = None,
                validation_run_id: Optional[str] = None, progress=None, cancel=None) -> dict:
    """The holdout computation -> {result, metrics, report} (module docstring)."""
    from ..backtest.engine import BacktestCancelled, DEFAULT_CAPITAL
    from . import folds, gates, report
    cap = capital if capital is not None else DEFAULT_CAPITAL
    if progress is not None:
        progress("data", 0.0, "holdout backtest")
    result = run_engine(spec, start=window["start"], end=window["end"], capital=cap, benchmark=benchmark,
                        progress=progress, cancel=cancel, data=data, holdout_access=True)
    if cancel is not None and cancel():
        raise BacktestCancelled()
    if progress is not None:
        progress("metrics", 0.0, "computing holdout metrics")
    m = compute_metrics(result)
    analysis = getattr(result, "analysis", None) if not isinstance(result, dict) else result.get("analysis")
    mode = folds.signal_mode(analysis)
    mean_ic = ic_reason = None
    ic_days = 0
    if mode == "cross_sectional":
        if horizon is None:
            ic_reason = "the validation run selected no IC horizon"
        else:
            series = folds.daily_ic(analysis, (int(horizon),))[int(horizon)]
            ic_days = len(series)
            if series:
                mean_ic = math.fsum(series.values()) / len(series)
            else:
                ic_reason = f"no day with a {horizon}-day forward return inside the window"
    perf = m.get("performance") or {}
    h = gates.holdout_gate(sharpe=perf.get("sharpe"), training_sharpe=training_sharpe, mode=mode, mean_ic=mean_ic,
                           horizon=horizon, sharpe_reason=_sharpe_reason(m), ic_reason=ic_reason,
                           window={"start": str(window["start"]), "end": str(window["end"])})
    h["details"]["ic_days"] = ic_days
    rng = _field(result, "range") or {}
    rep = {"version": report.REPORT_VERSION, "passed": h["status"] == "pass", "mode": mode, "gates": [h],
           "window": {"start": str(window["start"]), "end": str(window["end"])}, "range": rng,
           "validation_run_id": validation_run_id, "training_sharpe": training_sharpe,
           "eligibility": eligibility, "metrics": {k: perf.get(k) for k in ("total_return", "cagr", "sharpe",
                                                                            "max_drawdown", "volatility")}}
    from ..backtest import attribution as attribution_mod
    rep["gate_facts"] = attribution_mod.gate_facts(rep)              # SPEC §11.1 (X1): gate H facts
    return {"result": result, "metrics": m, "report": rep}


# --- jobs on the shared runner -------------------------------------------------------------------

class ValidationJobs:
    """Submits validation/holdout jobs onto ``backtest.jobs.manager()``. Callables injectable (tests):
    run_engine (engine.run), compute_metrics (metrics.compute with the analysis hook), run_stress
    (stress.run_stress), synthetic_costs (stress.synthetic_costs), data_factory (engine.DataLoader)."""

    def __init__(self, *, run_engine=None, compute_metrics=None, run_stress=None, synthetic_costs=None,
                 data_factory=None, manager=None, cpu_count: Optional[int] = None):
        self._run_engine = run_engine
        self._compute_metrics = compute_metrics
        self._run_stress = run_stress
        self._synthetic_costs = synthetic_costs
        self._data_factory = data_factory
        self._manager = manager
        self._cpu_count = cpu_count             # tests: replaces os.cpu_count() in the pool plan
        self.last_plan: Optional[dict] = None   # tests / log: the pool plan of the last validation work

    # dependencies
    def manager(self):
        from ..backtest import jobs as bt_jobs
        return self._manager or bt_jobs.manager()

    def _deps(self):
        from ..backtest import engine, stress
        run_engine = self._run_engine or engine.run

        def compute(result):
            if self._compute_metrics is not None:
                return self._compute_metrics(result)
            from ..backtest import metrics
            return metrics.compute(result, analysis=getattr(result, "analysis", None))
        return {"run_engine": run_engine, "compute_metrics": compute,
                "run_stress": self._run_stress or stress.run_stress,
                "synthetic_costs": self._synthetic_costs or stress.synthetic_costs,
                "data": self._data_factory() if self._data_factory else engine.DataLoader()}

    def _submit(self, create_card: Callable[[], dict], work: Callable, label: str, generic: str,
                cancelled: str) -> str:
        """Through the public ``backtest.jobs.JobManager.submit_work`` (one slot for all job kinds)."""
        return self.manager().submit_work(create_card, work, label=label, generic_msg=generic,
                                          cancelled_msg=cancelled)

    # validation
    def submit_validation(self, strategy_id: str, version: Optional[int], history_from: Optional[str], *,
                          auto_resume: bool = False) -> str:
        """Creates the queued validation card and starts the job -> run id (JobBusy / NotFound / ValueError)."""
        from ..backtest import runcard
        request = {"history_from": history_from, "stress": True}

        def create():
            return runcard.create(strategy_id, version, request, kind="validation", auto_resume=auto_resume)

        return self._submit(create, self._validation_work, "validation", VALIDATION_FAILURE, CANCELLED_VALIDATION)

    def resume_validation(self, run_id: str, *, auto_resume: Optional[bool] = None) -> str:
        """Manual resume of a failed validation card (SPEC §10.3) -> the NEW run id (``ResumeRefused``
        re-checked under the runner lock, ``JobBusy``)."""
        from ..backtest import runcard
        return self._submit(lambda: runcard.create_resumed(run_id, auto_resume=auto_resume), self._validation_work,
                            "validation", VALIDATION_FAILURE, CANCELLED_VALIDATION)

    def pool_plan(self, spec: dict, history_from: Optional[str], training_end) -> dict:
        """``parallel.plan_workers`` for this run: symbols (+ benchmark) x estimated sessions from the load
        start (history_from) to the training end; 1 worker when an injected engine cannot be pickled."""
        import pickle
        from ..backtest import engine
        from ..strategy import schema, universe
        from . import parallel
        n_symbols = len(universe.resolve(schema.with_defaults(spec)["universe"])) + 1
        days = max(1, (training_end - engine.history_from_date(history_from)).days)
        crypto = families_kind(spec) == "crypto"
        sessions = int(days * (1.0 if crypto else 252.0 / 365.0)) + 300
        plan = parallel.plan_workers(n_symbols, sessions, cpu_count=self._cpu_count)
        plan["symbols"], plan["sessions"] = n_symbols, sessions
        if self._run_engine is not None:
            try:
                pickle.dumps(self._run_engine)
            except Exception:  # noqa: BLE001 — a test fake that only lives in this process
                plan["workers"], plan["reason"] = 1, "the injected engine cannot be sent to worker processes"
        return plan

    def _validation_work(self, job, progress):
        """The validation job's work — re-entrant: the auto-resume re-runs it on the same card, a manual
        resume on the new card; checkpoints of the chain are reused (SPEC §10.3)."""
        from ..backtest import jobs as bt_jobs
        from ..backtest import runcard
        from ..backtest.engine import BacktestCancelled
        from . import families, gates, parallel
        deps = self._deps()
        card = runcard.get(job.run_id)
        strategy_id = card["strategy_id"]
        history_from = (card.get("request") or {}).get("history_from")
        store = runcard.CheckpointStore(job.run_id)
        runcard.ensure_basis(card, store)
        settings = gates.get_settings()
        fid = families.family_of(strategy_id)
        training_end = families.training_end(job.spec)
        plan = self.pool_plan(job.spec, history_from, training_end)
        self.last_plan = plan
        logger.info("validation %s: %d worker(s) (cpu %d, memory %d, %.0f MB per worker)", job.run_id,
                    plan["workers"], plan["cpu_workers"], plan["memory_workers"], plan["per_worker_bytes"] / 2 ** 20)
        loader = parallel.RecordingLoader(deps["data"], checkpoints=store, keep=plan["workers"] > 1)
        runner = parallel.TaskRunner(workers=plan["workers"], loader=loader, run_engine=self._run_engine,
                                     cancel=job.cancel_flag.is_set, progress=progress)
        out = run_validation(
            job.spec, run_engine=deps["run_engine"], compute_metrics=deps["compute_metrics"],
            run_stress=deps["run_stress"], synthetic_costs=deps["synthetic_costs"],
            training_end=training_end, history_from=history_from, data=loader,
            thresholds=settings["effective"], settings=settings, n_trials_before=families.trial_count(fid),
            trial_sharpes_before=families.family_trial_sharpes(fid),
            holdout=_eligibility_snapshot(strategy_id), progress=progress, cancel=job.cancel_flag.is_set,
            seed=card.get("seed"), runner=runner, checkpoints=store,
            stress_tasks=self._run_stress is None and self._synthetic_costs is None)
        if job.cancel_flag.is_set():
            raise BacktestCancelled()
        from . import report
        bt_jobs.add_warnings(out["result"], store.warnings)
        from ..backtest import attribution as attribution_mod
        # SPEC §11.1: the attribution of the base (training) run — computed from the in-memory result, never
        # checkpointed: a resumed attempt re-runs the base backtest (data checkpoints) and recomputes it
        attribution = attribution_mod.compute(out["result"], out["metrics"])
        metrics = dict(out["metrics"])
        metrics["validation"] = report.stored_block(out["report"])
        progress("saving", 0.0, "saving the validation report")
        runcard.finish(job.run_id, out["result"], metrics, stress=out["stress"], attribution=attribution)
        logger.info("validation %s finished (passed=%s)", job.run_id, out["report"]["passed"])

    # holdout
    def holdout_preconditions(self, strategy_id: str) -> dict:
        """{row, version, eligibility, validation card} or ``HoldoutRefused`` (409) / lifecycle NotFound."""
        from ..backtest import runcard
        from ..lifecycle import registry
        from . import families, report
        row = registry.get(strategy_id)
        version = int(row["current_version"])
        elig = families.holdout_eligibility(strategy_id)
        if not elig["eligible"]:
            raise HoldoutRefused(elig["reason"] or "The holdout cannot be opened now.",
                                 eligible_from=elig.get("eligible_from"))
        latest = runcard.latest_done(strategy_id, version, kind="validation")
        card = runcard.get(latest["id"]) if latest else None
        if card is None or not report.effective_passed(card):
            raise HoldoutRefused(f"Open the holdout only after a passing validation run of version {version} "
                                 "(all gates G1–G8 pass or have an exception).", eligible_from=None)
        return {"row": row, "version": version, "eligibility": elig, "validation": card}

    def submit_holdout(self, strategy_id: str, confirm: str) -> str:
        """Checks the typed confirmation (ValueError), the preconditions (HoldoutRefused) and starts the job."""
        from ..backtest import runcard
        pre = self.holdout_preconditions(strategy_id)
        if confirm != pre["row"]["name"]:
            raise ValueError("confirm: type the exact strategy name to open the holdout")
        window = pre["eligibility"]["window"]
        vcard = pre["validation"]
        vreq = vcard.get("request") or {}
        vrep = (vcard.get("metrics") or {}).get("validation", {}).get("report") or {}
        request = {"start": window["start"], "end": window["end"], "benchmark": vreq.get("benchmark"),
                   "capital": vreq.get("capital"), "stress": False, "history_from": None}
        training_sharpe = ((vcard.get("metrics") or {}).get("performance") or {}).get("sharpe")
        horizon = (vrep.get("ic") or {}).get("best_horizon")

        def create():
            # runs under the runner's lock: re-check so two opens cannot both pass (review 2026-10-09)
            again = self.holdout_preconditions(strategy_id)
            if again["version"] != pre["version"] or again["eligibility"]["window"] != window or \
                    again["validation"]["id"] != vcard["id"]:
                raise HoldoutRefused("The holdout state changed meanwhile — reload and try again.",
                                     eligible_from=again["eligibility"].get("eligible_from"))
            return runcard.create(strategy_id, pre["version"], request, kind="holdout")

        def work(job, progress):
            from ..backtest.engine import BacktestCancelled
            from . import report
            deps = self._deps()
            out = run_holdout(job.spec, run_engine=deps["run_engine"], compute_metrics=deps["compute_metrics"],
                              window=window, training_sharpe=training_sharpe, horizon=horizon,
                              capital=vreq.get("capital"), benchmark=vreq.get("benchmark"), data=deps["data"],
                              eligibility=pre["eligibility"], validation_run_id=vcard["id"], progress=progress,
                              cancel=job.cancel_flag.is_set)
            if job.cancel_flag.is_set():
                raise BacktestCancelled()
            result = "pass" if out["report"]["passed"] else "fail"
            from ..backtest import attribution as attribution_mod
            attribution = attribution_mod.compute(out["result"], out["metrics"])     # the holdout window only
            metrics = dict(out["metrics"])
            metrics["holdout"] = report.stored_block(out["report"])
            progress("saving", 0.0, "saving the holdout result")
            # card + holdout event in ONE transaction (V1 review fix 2026-10-09)
            runcard.finish_holdout(job.run_id, out["result"], metrics,
                                   family_event={"window": {"start": window["start"], "end": window["end"]},
                                                 "result": result}, attribution=attribution)
            logger.info("holdout %s finished (%s)", job.run_id, result)

        return self._submit(create, work, "holdout", HOLDOUT_FAILURE, CANCELLED_HOLDOUT)


def families_kind(spec: dict) -> str:
    from . import families
    return families.universe_kind(spec)


def _eligibility_snapshot(strategy_id: str) -> Optional[dict]:
    """Holdout eligibility snapshot for the report (universe counter etc.). Errors propagate."""
    from . import families
    return families.holdout_eligibility(strategy_id)


_SERVICE: Optional[ValidationJobs] = None
_SERVICE_LOCK = threading.Lock()


def service() -> ValidationJobs:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = ValidationJobs()
        return _SERVICE


def set_service(s: Optional[ValidationJobs]) -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = s
