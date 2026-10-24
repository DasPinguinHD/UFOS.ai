"""Stress tests (SPEC §8.4, notes §8.10), run on request after a base
backtest.

Scenarios (each returns ``{key, kind, label, status, reason, window, range,
metrics, warnings}``; status ``done`` | ``not_available`` (reason
"not available: …") | ``failed`` (a data source/storage error, its
user message) — one scenario never stops the others; cancellation does):

- historical episodes (``EPISODES``): ``engine.run`` with ``start`` /
  ``end`` = the episode; the engine loads its warm-up before the start
  (2000-01-03 at the earliest: ``clamp_warmup=True``, fix 2026-10-09 — the
  dot-com episode's warm-up reached into 1999 and failed on the calendar;
  a shorter warm-up is reported as warning ``short_warmup``). Only symbols with data in the window take part (the
  engine excludes the others, warning ``symbol_excluded``). Not available
  when the engine cannot run the window (no symbol with data, fewer than two
  days, pre-2016 without the Tiingo key -> "not available: Tiingo API key
  is not set …") or when the simulated range misses the episode's start or
  end by more than ``EDGE_TOLERANCE_DAYS`` calendar days (data starts later
  / ends earlier). An episode whose end lies on/after the start of the locked
  holdout window (SPEC §9.1, ``holdout_start_for``) is not run at all:
  "not available: inside the locked holdout window";
- synthetic costs: the base run's range (explicit start/end) with
  ``engine.run(cost_overrides=…)`` — ``costs_x2`` = fee and slippage x2,
  ``slippage_x3`` = slippage x3, fee unchanged, EACH asset class with its
  own rates (user decision 2026-10-04): equity from ``fees_bps``, crypto
  from ``crypto_fees_bps`` (each split half fee / half slippage, as the
  broker books it), or a custom spec's fee / slippage for both. The new
  per-fill cost per class goes into that run's broker params (the broker
  again books half / half — the total per fill is exact). It applies to the
  benchmark account too (engine rule: same costs);
- Monte-Carlo (``monte_carlo``): circular moving-block bootstrap of the base
  run's daily returns, ``MC_PATHS`` paths of the same length n, block length
  L = max(2, round(n ** (1/3))) (the n^(1/3) rate of Hall, Horowitz & Jing
  1995 for block bootstraps; keeps short-range autocorrelation and
  volatility clusters), ``numpy.random.default_rng(seed)`` (PCG64; default
  ``MC_SEED``) -> 5 / 50 / 95 % percentiles (linear) of the paths' max
  drawdown and CAGR (= end value ** (P / n) - 1). Needs >= ``MC_MIN_RETURNS``
  returns.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import copy
import math
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional

EPISODES = (
    ("dotcom", "Dot-com crash", date(2000, 3, 24), date(2002, 10, 9)),
    ("gfc", "Global financial crisis", date(2007, 10, 9), date(2009, 3, 9)),
    ("q4_2018", "Q4 2018 sell-off", date(2018, 9, 20), date(2018, 12, 24)),
    ("covid", "Covid crash", date(2020, 2, 19), date(2020, 3, 23)),
    ("rate_shock_2022", "2022 rate shock", date(2022, 1, 3), date(2022, 10, 12)),
    ("crypto_2022", "Crypto 2022", date(2021, 11, 10), date(2022, 11, 21)),
)
COST_MULTIPLIER = 2.0
SLIPPAGE_MULTIPLIER = 3.0
MC_PATHS = 1000
MC_SEED = 20261003
MC_MIN_RETURNS = 60
MC_PERCENTILES = (5, 50, 95)
EDGE_TOLERANCE_DAYS = 7
EPISODE_METRICS = ("total_return", "cagr", "max_drawdown", "volatility", "sharpe", "worst_day", "best_day")


def _field(result, key: str):
    return result.get(key) if isinstance(result, dict) else getattr(result, key)


WARNING_TEXT = {   # click-test 2026-10-22: a structured summary instead of one raw code per symbol
    "seam_mismatch": "with a seam mismatch (no pre-2016 data used)",
    "no_pre_seam_data": "without data before 2016",
    "symbol_excluded": "excluded (no data in the window)",
    "no_volume_source": "without a volume source",
}
SUMMARY_SYMBOLS_IN_TEXT = 8


def warning_summary(warnings) -> dict:
    """{total, counts {code: n}, symbols {code: [symbols]}, text} of a scenario's warnings (the raw list stays in
    ``warnings``). ``text`` e.g. "6 symbols with a seam mismatch (no pre-2016 data used): SPY, …; 40 symbols
    excluded (no data in the window): …"; "" when there are none."""
    counts: Dict[str, int] = {}
    syms: Dict[str, List[str]] = {}
    for w in warnings or []:
        if not isinstance(w, dict):
            continue
        code = str(w.get("code") or "warning")
        counts[code] = counts.get(code, 0) + 1
        if w.get("symbol") and str(w["symbol"]) not in syms.setdefault(code, []):
            syms[code].append(str(w["symbol"]))
    parts = []
    for code in sorted(counts, key=lambda c: (-counts[c], c)):
        lst = sorted(syms.get(code) or [])
        if lst:
            n = len(lst)
            shown = ", ".join(lst[:SUMMARY_SYMBOLS_IN_TEXT]) + (f" and {n - SUMMARY_SYMBOLS_IN_TEXT} more"
                                                                 if n > SUMMARY_SYMBOLS_IN_TEXT else "")
            parts.append(f"{n} symbol{'s' if n != 1 else ''} {WARNING_TEXT.get(code, code.replace('_', ' '))}: "
                         f"{shown}")
        elif code == "short_warmup":
            parts.append("shorter warm-up than requested")
        else:
            parts.append(f"{counts[code]} × {code.replace('_', ' ')}")
    return {"total": sum(counts.values()), "counts": counts, "symbols": {c: sorted(v) for c, v in syms.items()},
            "text": "; ".join(parts)}


def with_summaries(stress_block):
    """Presentation fields, computed ON READ (``runcard.get``) so stored cards and every execution path (sequential,
    process pool, resumed) stay identical: ``warning_summary`` on every scenario and ``note`` on "Slippage x3" when
    it charges the same per-fill cost as "Costs x2"."""
    if isinstance(stress_block, dict):
        scen = [sc for sc in stress_block.get("scenarios") or [] if isinstance(sc, dict)]
        for sc in scen:
            sc["warning_summary"] = warning_summary(sc.get("warnings"))
        by = {sc.get("key"): sc for sc in scen}
        if "costs_x2" in by and "slippage_x3" in by:
            same_cost_note(by["costs_x2"], by["slippage_x3"])
    return stress_block


def _scenario(key: str, kind: str, label: str, **kw) -> dict:
    out = {"key": key, "kind": kind, "label": label, "status": None, "reason": None, "window": None,
           "range": None, "metrics": None, "warnings": []}
    out.update(kw)
    return out


def _summary(result) -> dict:
    from . import metrics as m
    full = m.compute(result, include_signal=False)
    perf, bench = full["performance"], full["benchmark"]
    return {"strategy": {k: perf.get(k) for k in EPISODE_METRICS},
            "benchmark": {"symbol": bench.get("symbol"), "total_return": bench.get("total_return"),
                          "max_drawdown": bench.get("max_drawdown")},
            "costs_total": full["costs"].get("total"), "unavailable": full["unavailable"]}


def _run_engine(run_engine, spec, out: dict, **kw):
    """Runs the engine for one scenario; fills status/reason. Returns the result or None."""
    from ..core.errors import InvalidRequest, SecretMissing, UserFacingError
    from .engine import BacktestCancelled, BacktestDataError
    try:
        return run_engine(spec, **kw)
    except BacktestCancelled:
        raise
    except SecretMissing as ex:
        out["status"], out["reason"] = "not_available", "not available: " + ex.user_message
    except (InvalidRequest, BacktestDataError) as ex:
        out["status"], out["reason"] = "not_available", "not available: " + ex.user_message
    except UserFacingError as ex:
        out["status"], out["reason"] = "failed", ex.user_message
    return None


def _short_warmup(result) -> Optional[dict]:
    from ..core import calendars
    rng = _field(result, "range")
    first, start = date.fromisoformat(rng["first_data"]), date.fromisoformat(rng["start"])
    if rng["calendar"] == "nyse":
        have = max(0, len(calendars.trading_days(first, start)) - 1)
    else:
        have = (start - first).days
    need = int(rng["warmup_sessions"])
    if have < need:
        return {"code": "short_warmup", "message": f"Only {have} day(s) of history before the episode start "
                f"(data from {rng['first_data']}); the strategy asks for {need} — early signals may be missing.",
                "have": have, "need": need}
    return None


def episode(key: str, label: str, start: date, end: date, spec: dict, *, capital: float, benchmark: str,
            data=None, cancel=None, progress=None, run_engine=None) -> dict:
    from . import engine
    run_engine = run_engine or engine.run
    out = _scenario(key, "episode", label, window={"start": start.isoformat(), "end": end.isoformat()})
    res = _run_engine(run_engine, spec, out, start=start, end=end, capital=capital, benchmark=benchmark,
                      data=data, cancel=cancel, progress=progress, clamp_warmup=True,
                      record_symbol_pnl=False)                 # SPEC §11.5 (L7): stress runs carry no symbol_pnl
    if res is None:
        return out
    rng = _field(res, "range")
    out["range"] = {"start": rng["start"], "end": rng["end"]}
    if date.fromisoformat(rng["start"]) > start + timedelta(days=EDGE_TOLERANCE_DAYS) or \
            date.fromisoformat(rng["end"]) < end - timedelta(days=EDGE_TOLERANCE_DAYS):
        out["status"] = "not_available"
        out["reason"] = (f"not available: the data covers only {rng['start']} … {rng['end']} of the episode "
                         f"{start.isoformat()} … {end.isoformat()}")
        return out
    out["status"] = "done"
    out["metrics"] = _summary(res)
    warn = [w for w in _field(res, "warnings") if w.get("code") in ("symbol_excluded", "no_volume_source",
                                                                    "seam_mismatch", "no_pre_seam_data")]
    sw = _short_warmup(res)
    if sw:
        warn.append(sw)
    out["warnings"] = warn
    return out


def scaled_costs(spec: dict, fee_mult: float, slippage_mult: float):
    """(cost_overrides for ``engine.run``, per-class detail, None) or (None, None, reason).
    Each asset class keeps its OWN rates (user decision 2026-10-04): equity fee/slippage =
    ``fees_bps`` / 2 each, crypto = ``crypto_fees_bps`` / 2 each (the paper broker books half
    of the per-fill cost as fee, half as slippage) — or a custom spec's fee/slippage for both
    classes. New per-fill cost per class = fee x fee_mult + slippage x slippage_mult."""
    from strategy_platform.core import params as fund_params
    from ..strategy import schema
    from .engine import COST_OVERRIDE_MAX_BPS
    c = schema.with_defaults(spec)["costs"]
    if c.get("model") == "custom":
        base = {"equity": (float(c["fee_bps"]), float(c["slippage_bps"])),
                "crypto": (float(c["fee_bps"]), float(c["slippage_bps"]))}
    else:
        p = fund_params.defaults()
        base = {"equity": (float(p["fees_bps"]) / 2.0, float(p["fees_bps"]) / 2.0),
                "crypto": (float(p["crypto_fees_bps"]) / 2.0, float(p["crypto_fees_bps"]) / 2.0)}
    detail, overrides = {}, {}
    for cls, key in (("equity", "fees_bps"), ("crypto", "crypto_fees_bps")):
        fee, slip = base[cls]
        nf, ns = fee * fee_mult, slip * slippage_mult
        if nf + ns > COST_OVERRIDE_MAX_BPS:
            return None, None, f"not available: the scaled {cls} cost {nf + ns:g} bps exceeds " \
                               f"{COST_OVERRIDE_MAX_BPS:g} bps per fill"
        detail[cls] = {"base_fee_bps": fee, "base_slippage_bps": slip, "fee_bps": nf, "slippage_bps": ns,
                       "per_fill_bps": nf + ns}
        overrides[key] = nf + ns
    return overrides, detail, None


SAME_COST_NOTE = ("same per-fill cost as \"Costs x2\" for this cost model (fee = slippage = half of the per-fill "
                  "cost: 1 x fee + 3 x slippage = 2 x (fee + slippage)) — the results are identical by construction, "
                  "not a separate stress")


def same_cost_note(costs_x2: dict, slippage_x3: dict) -> None:
    """Click-test 2026-10-22: with the paper-broker model (fee = slippage) "Slippage x3" charges exactly the
    per-fill cost of "Costs x2", so both runs are identical — say so on the slippage scenario (``note``)."""
    a, b = costs_x2.get("costs"), slippage_x3.get("costs")
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys() and all(
            abs(a[c]["per_fill_bps"] - b[c]["per_fill_bps"]) < 1e-9 for c in a):
        slippage_x3["note"] = SAME_COST_NOTE


def synthetic_costs(key: str, label: str, spec: dict, base_result, fee_mult: float, slippage_mult: float, *,
                    data=None, cancel=None, progress=None, run_engine=None, holdout_access: bool = False) -> dict:
    from . import engine
    run_engine = run_engine or engine.run
    rng = _field(base_result, "range")
    out = _scenario(key, "synthetic", label, window={"start": rng["start"], "end": rng["end"]})
    overrides, detail, why = scaled_costs(spec, fee_mult, slippage_mult)
    if overrides is None:
        out["status"], out["reason"] = "not_available", why
        return out
    out["costs"] = detail
    extra = {"holdout_access": True} if holdout_access else {}      # only the holdout job's base run
    res = _run_engine(run_engine, spec, out, start=rng["start"], end=rng["end"],
                      capital=_field(base_result, "request")["capital"],
                      benchmark=_field(base_result, "benchmark_info")["symbol"], data=data, cancel=cancel,
                      progress=progress, cost_overrides=overrides, record_symbol_pnl=False, **extra)
    if res is None:
        return out
    rr = _field(res, "range")
    out["range"] = {"start": rr["start"], "end": rr["end"]}
    out["status"] = "done"
    out["metrics"] = _summary(res)
    base = _summary(base_result)["strategy"]
    out["metrics"]["delta_vs_base"] = {k: (None if out["metrics"]["strategy"].get(k) is None or base.get(k) is None
                                           else out["metrics"]["strategy"][k] - base[k])
                                       for k in ("total_return", "cagr", "sharpe", "max_drawdown")}
    return out


def block_length(n: int) -> int:
    return max(2, int(round(n ** (1.0 / 3.0))))


def monte_carlo(returns: List[float], *, periods_per_year: int, paths: int = MC_PATHS, seed: int = MC_SEED) -> dict:
    """Bootstrap distribution of max drawdown and CAGR (module docstring). Deterministic for a seed."""
    import numpy as np
    out = _scenario("monte_carlo", "monte_carlo", "Monte-Carlo bootstrap of daily returns")
    n = len(returns)
    out.update({"paths": paths, "seed": seed, "returns": n, "block_length": None})
    if n < MC_MIN_RETURNS:
        out["status"] = "not_available"
        out["reason"] = f"not available: only {n} daily returns (needs {MC_MIN_RETURNS})"
        return out
    L = block_length(n)
    out["block_length"] = L
    r = np.asarray(returns, dtype=float)
    rng = np.random.default_rng(seed)
    k = -(-n // L)
    starts = rng.integers(0, n, size=(paths, k))
    idx = ((starts[:, :, None] + np.arange(L)[None, None, :]) % n).reshape(paths, k * L)[:, :n]
    growth = np.cumprod(1.0 + r[idx], axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.ones((paths, 1)), growth], axis=1), axis=1)[:, 1:]
    mdd = np.max(1.0 - growth / peak, axis=1)
    end = growth[:, -1]
    cagr = np.where(end > 0, np.power(np.clip(end, 1e-300, None), periods_per_year / n) - 1.0, -1.0)
    out["status"] = "done"
    out["metrics"] = {
        "max_drawdown": {f"p{p}": float(np.percentile(mdd, p)) for p in MC_PERCENTILES},
        "cagr": {f"p{p}": float(np.percentile(cagr, p)) for p in MC_PERCENTILES},
    }
    return out


def holdout_start_for(spec: dict, base_result) -> date:
    """Start of the locked holdout window (SPEC §9.1): the base run's ``range.holdout.start`` when the
    engine recorded it, else computed now for the spec's universe."""
    from ..validation import families
    hold = (_field(base_result, "range") or {}).get("holdout")
    if isinstance(hold, dict) and hold.get("start"):
        return date.fromisoformat(hold["start"])
    return families.lock_for_kind(families.universe_kind(spec))["start"]


def run_stress(spec: dict, base_result, *, data=None, progress: Optional[Callable[[str, float, str], None]] = None,
               cancel: Optional[Callable[[], bool]] = None, seed: int = MC_SEED, run_engine=None,
               episodes=EPISODES, holdout_access: bool = False) -> dict:
    """All scenarios for ``spec`` after its base run ``base_result`` -> {seed, scenarios: [...]}.
    Episodes reaching into the locked holdout window (end on/after its start) are not run:
    ``not_available`` with ``families.LOCKED_EPISODE_REASON`` (SPEC §9.1). ``holdout_access`` (the
    holdout job only) lets the synthetic cost scenarios re-run a base range inside the window."""
    from ..validation import families
    from . import metrics as m
    from .engine import BacktestCancelled
    capital = _field(base_result, "request")["capital"]
    bench = _field(base_result, "benchmark_info")["symbol"]
    total = len(episodes) + 3
    scenarios: List[dict] = []

    def emit(k: int, message: str) -> None:
        if progress is not None:
            progress("stress", round(100.0 * k / total, 2), message)

    def check() -> None:
        if cancel is not None and cancel():
            raise BacktestCancelled()

    hs = holdout_start_for(spec, base_result)
    for k, (key, label, a, b) in enumerate(episodes):
        check()
        emit(k, f"stress: {label}")
        if b >= hs:
            scenarios.append(_scenario(key, "episode", label, status="not_available",
                                       reason=families.LOCKED_EPISODE_REASON,
                                       window={"start": a.isoformat(), "end": b.isoformat()}))
            continue
        scenarios.append(episode(key, label, a, b, spec, capital=capital, benchmark=bench, data=data,
                                 cancel=cancel, run_engine=run_engine))
    n = len(episodes)
    check()
    emit(n, "stress: costs x2")
    scenarios.append(synthetic_costs("costs_x2", "Costs x2", spec, base_result, COST_MULTIPLIER, COST_MULTIPLIER,
                                     data=data, cancel=cancel, run_engine=run_engine,
                                     holdout_access=holdout_access))
    check()
    emit(n + 1, "stress: slippage x3")
    scenarios.append(synthetic_costs("slippage_x3", "Slippage x3", spec, base_result, 1.0, SLIPPAGE_MULTIPLIER,
                                     data=data, cancel=cancel, run_engine=run_engine,
                                     holdout_access=holdout_access))
    check()
    emit(n + 2, "stress: Monte-Carlo")
    cal = _field(base_result, "range")["calendar"]
    rets = m.returns_of(m.valid_points(_field(base_result, "equity")))
    scenarios.append(monte_carlo(rets, periods_per_year=m.PERIODS_PER_YEAR[cal], seed=seed))
    emit(total, "stress tests finished")
    return {"seed": seed, "scenarios": scenarios}
