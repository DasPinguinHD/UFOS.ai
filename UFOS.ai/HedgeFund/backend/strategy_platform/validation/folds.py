"""Walk-forward folds and daily IC series of ONE backtest result (SPEC §9.3
step 2). The spec is fixed — nothing is refitted: the training backtest's
simulated range is cut into K = 5 consecutive, equal-length out-of-sample
segments (``numpy.array_split`` of the simulated days: lengths differ by at
most one day) and every fold is measured on its own days.

Per fold: ``{fold, start, end, days, returns, sharpe, total_return,
max_drawdown, mean_ic, ic_days, horizon, reason}``.
- returns: daily returns r_j = E_j / E_(j-1) - 1 of consecutive VALID equity
  points (as backtest/metrics.py), assigned to the fold of day j (the first
  day of a fold therefore carries the return from the previous fold's last
  close — it is earned inside the fold);
- sharpe = mean / std (ddof 1) * sqrt(P), None with a reason for < 2 returns
  or zero variance; total_return = product(1 + r) - 1; max_drawdown on the
  fold's own equity path (fraction);
- mean_ic (cross-sectional strategies): mean of the daily rank IC at
  ``horizon`` over the fold's days t whose forward window t + h ALSO ends
  inside the fold (strictly out-of-sample per fold), None + reason when no
  such day exists.

Daily IC (``daily_ic``): Spearman rank correlation of the signal row t with
the forward return C[t+h] / C[t] - 1 of the ``all`` close series, >= 5 valid
pairs and non-constant sides (``backtest.metrics.spearman`` — the same
definition as the run card's signal quality), for t in the simulated range
with t + h inside it.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

K_FOLDS = 5
IC_HORIZONS = (1, 5, 10, 21)          # G1: best of these horizons (SPEC §9.4)
IC_MIN_SYMBOLS = 5                     # = backtest.metrics.IC_MIN_SYMBOLS


def _field(result, key: str, default=None):
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def periods_per_year(result) -> int:
    from ..backtest import metrics
    return metrics.PERIODS_PER_YEAR[(_field(result, "range") or {}).get("calendar", "nyse")]


def fold_bounds(n: int, k: int = K_FOLDS) -> List[Tuple[int, int]]:
    """Inclusive (first, last) day indices of k consecutive, equal-length segments of n days."""
    if n < k:
        raise ValueError(f"{n} simulated day(s) cannot be split into {k} folds")
    import numpy as np
    return [(int(p[0]), int(p[-1])) for p in np.array_split(np.arange(n), k)]


def daily_returns(equity: Sequence[Optional[float]]) -> List[Tuple[int, float]]:
    """[(day index j, r_j)] over consecutive valid equity points (metrics.valid_points)."""
    from ..backtest import metrics
    pts = metrics.valid_points(equity)
    return [(pts[k][0], pts[k][1] / pts[k - 1][1] - 1.0) for k in range(1, len(pts))]


def benchmark_pairs(result) -> Tuple[List[float], List[float]]:
    """Paired daily returns (strategy, benchmark) on days where both accounts are valued on both days."""
    eq = list(_field(result, "equity") or [])
    bm = list(_field(result, "benchmark") or [])
    rs, rb = [], []
    for j in range(1, min(len(eq), len(bm))):
        a0, a1, b0, b1 = eq[j - 1], eq[j], bm[j - 1], bm[j]
        if all(x is not None and math.isfinite(float(x)) and float(x) > 0 for x in (a0, a1, b0, b1)):
            rs.append(float(a1) / float(a0) - 1.0)
            rb.append(float(b1) / float(b0) - 1.0)
    return rs, rb


def signal_mode(analysis: Optional[dict]) -> Optional[str]:
    """``cross_sectional`` (>= 5 symbols), ``time_series`` (fewer) or None without the analysis hook —
    the same split as backtest/metrics.signal_quality."""
    if not analysis:
        return None
    return "cross_sectional" if len(analysis.get("symbols") or []) >= IC_MIN_SYMBOLS else "time_series"


def daily_ic(analysis: dict, horizons: Sequence[int] = IC_HORIZONS) -> Dict[int, Dict[str, float]]:
    """{h: {date (ISO): ic}} for the simulated range of the analysis hook (module docstring)."""
    import numpy as np
    from ..backtest import metrics
    S = np.asarray(analysis["signal"], dtype=float)
    C = np.asarray(analysis["close_all"], dtype=float)
    C = np.where(C > 0, C, np.nan)
    i0, i1 = int(analysis["i0"]), int(analysis["i1"])
    dates = list(analysis["dates"])
    out: Dict[int, Dict[str, float]] = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        for h in horizons:
            series: Dict[str, float] = {}
            for t in range(i0, i1 - h + 1):
                v = metrics.spearman(S[t], C[t + h] / C[t] - 1.0, IC_MIN_SYMBOLS)
                if v is not None:
                    series[dates[t]] = v
            out[int(h)] = series
    return out


def _max_drawdown(rets: Sequence[float]) -> Optional[float]:
    if not rets:
        return None
    v, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        v *= 1.0 + r
        peak = max(peak, v)
        mdd = max(mdd, 1.0 - v / peak)
    return mdd


def fold_metrics(result, *, ic: Optional[Dict[str, float]] = None, horizon: Optional[int] = None,
                 analysis: Optional[dict] = None, k: int = K_FOLDS) -> List[dict]:
    """Per-fold metrics of one backtest result (module docstring). ``ic`` = {date: ic} at ``horizon``;
    ``analysis`` gives the panel dates for the "forward window inside the fold" rule."""
    from . import stats
    dates = list(_field(result, "dates") or [])
    P = periods_per_year(result)
    rets = daily_returns(_field(result, "equity") or [])
    pidx = {d: i for i, d in enumerate((analysis or {}).get("dates") or [])}
    out = []
    for f, (a, b) in enumerate(fold_bounds(len(dates), k), start=1):
        r = [x for j, x in rets if a <= j <= b]
        row = {"fold": f, "start": dates[a], "end": dates[b], "days": b - a + 1, "returns": len(r),
               "sharpe": None, "total_return": None, "max_drawdown": None, "mean_ic": None, "ic_days": 0,
               "horizon": horizon, "reason": None}
        reasons = []
        s = stats.sharpe(r, P)
        if s is None:
            reasons.append("Sharpe: fewer than two returns or zero variance in the fold")
        row["sharpe"] = s
        if r:
            row["total_return"] = math.prod(1.0 + x for x in r) - 1.0
            row["max_drawdown"] = _max_drawdown(r)
        if ic is not None and horizon is not None:
            end_p = pidx.get(dates[b])
            vals = []
            for d in dates[a:b + 1]:
                t = pidx.get(d)
                if d in ic and t is not None and end_p is not None and t + horizon <= end_p:
                    vals.append(ic[d])
            row["ic_days"] = len(vals)
            if vals:
                row["mean_ic"] = math.fsum(vals) / len(vals)
            else:
                reasons.append(f"IC: no day in the fold with a {horizon}-day forward window inside the fold")
        row["reason"] = "; ".join(reasons) or None
        out.append(row)
    return out
