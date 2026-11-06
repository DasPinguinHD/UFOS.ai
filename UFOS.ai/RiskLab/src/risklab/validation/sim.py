"""Simulator for the overfitting study of the UFOS validation gates.

**Rule family** (a parametrised daily trend rule, as a user would write it in the UFOS DSL):
moving-average crossover on the log price ``x_t``,
``position_t = sign(MA_fast(x)_t - MA_slow(x)_t)`` in {-1, +1} (long / short, 0 on an exact
tie), decided at the close of day t and held on day t + 1. Parameters ``fast`` in
4..19 step 1 and ``slow`` in 24..69 step 3 (integers): 16 x 16 = 256 configurations,
the search space (about 7 to 18 position changes a year under noise). The UFOS grid of the selected configuration (min / max / step as above,
``value`` = the selected one) has 256 configurations, so the platform subsamples it to 64
with the base forced in (:mod:`risklab.validation.grid`); G5 and G6 run on those 64.

**Returns** (daily, n days; sigma = 20 % a year, ``s = sigma / sqrt(252)``):
``r_t = mu * q_(t-1) + e_t`` where ``q_(t-1)`` is the position of the TRUE configuration
(fast 8, slow 42) computed sequentially from the simulated prices up to t - 1 (0 during
the first ``slow`` days) and ``mu = SR_target * s / sqrt(252)``, so the true configuration
earns a gross annualised Sharpe of ``SR_target`` (``SR_target = 0``: pure noise, no rule
has an edge). Innovations ``e_t``:

* ``iid_t``: ``s * z_t``, z standardised Student-t (nu = 5, unit variance);
* ``garch_t``: GARCH(1,1) ``h_t = w + a e_(t-1)^2 + b h_(t-1)``, a = 0.08, b = 0.90,
  ``w = s^2 (1 - a - b)`` (unconditional variance s^2), ``e_t = sqrt(h_t) z_t`` with the same
  standardised t; h starts at s^2.

**Strategy returns**: ``R_t = p_(t-1) r_t - c |p_(t-1) - p_(t-2)|`` with c = cost in bps per
unit of turnover (a long-to-short flip is 2 units); the first ``BURN_IN`` days (= max slow +
1) are dropped so every configuration is defined. Closed trades = the number of position
changes in the window (each change closes the previous position).

**Windows**: the evaluated sample is ``train_days`` (5, 10 or 20 years x 252) followed by the
holdout (the last 24 months = 504 days), evaluated once for the selected configuration (H).

**Search**: ``K`` configurations are tried, the first K of a random permutation of the 256
(the same permutation for every K, so the searches are nested on the same data; K = 1 is
one random configuration); the best training net Sharpe is selected (first on ties).

**Gate metrics of the selection** (as ``jobs.run_validation`` feeds them, SPEC §9.3):
G1 fold Sharpes of the training returns; G2 alpha vs buy and hold of the asset
(Newey-West t); G4 DSR with ``n_trials = K + 1`` and the trial Sharpes = the K training
Sharpes plus the training Sharpe of the validated run; G5 PBO over the 64-configuration
grid; G6 the grid neighbours' Sharpe ratio to the base; G7 closed trades; H holdout Sharpe.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from risklab.conventions import SeedLike, make_rng

from . import grid as _grid
from .folds import fold_sharpes
from .stats import alpha_newey_west, deflated_sharpe, pbo_cscv

PERIODS = 252
HOLDOUT_DAYS = 504
FAST = tuple(range(4, 20))            # 16 values
SLOW = tuple(range(24, 70, 3))        # 16 values
TRUE_CONFIG = (8, 42)
BURN_IN = max(SLOW) + 1
SIGMA_ANNUAL = 0.20
NU = 5.0
GARCH_A, GARCH_B = 0.08, 0.90
DATA_TYPES = ("iid_t", "garch_t")
#: configurations of the search space in product order (fast major, slow minor) = UFOS order (axes by name)
CONFIGS: Tuple[Tuple[int, int], ...] = tuple((f, s) for f in FAST for s in SLOW)
CONFIG_INDEX = {c: i for i, c in enumerate(CONFIGS)}


def param_spec(fast: int, slow: int) -> Dict[str, dict]:
    """The UFOS DSL parameters of a configuration (min / max / step of the family, value = the configuration)."""
    return {"fast": {"min": FAST[0], "max": FAST[-1], "step": 1, "value": int(fast), "integer": True},
            "slow": {"min": SLOW[0], "max": SLOW[-1], "step": 3, "value": int(slow), "integer": True}}


def _unit_t(rng: np.random.Generator, size) -> np.ndarray:
    return rng.standard_t(NU, size=size) * math.sqrt((NU - 2.0) / NU)


def _ma_sign(C: np.ndarray, t: int, f: int, s: int) -> np.ndarray:
    """sign(MA_f - MA_s) at day t from cumulative sums C (m x (n + 1), C[:, k] = sum of x_0..x_(k-1))."""
    ma_f = (C[:, t + 1] - C[:, t + 1 - f]) / f
    ma_s = (C[:, t + 1] - C[:, t + 1 - s]) / s
    return np.sign(ma_f - ma_s)


def simulate_returns(m: int, n: int, data: str, sr_target: float, seed: SeedLike,
                     true_config: Tuple[int, int] = TRUE_CONFIG, sigma_annual: float = SIGMA_ANNUAL) -> np.ndarray:
    """m x n daily returns (module docstring)."""
    if data not in DATA_TYPES:
        raise ValueError(f"data must be one of {DATA_TYPES}")
    rng = make_rng(seed)
    s = sigma_annual / math.sqrt(PERIODS)
    z = _unit_t(rng, (m, n))
    mu = sr_target * s / math.sqrt(PERIODS)
    if data == "iid_t" and mu == 0.0:
        return s * z
    f, sl = true_config
    w = s * s * (1.0 - GARCH_A - GARCH_B)
    h = np.full(m, s * s)
    e_prev = np.zeros(m)
    r = np.empty((m, n))
    x = np.zeros(m)
    C = np.zeros((m, n + 1))
    q = np.zeros(m)                     # position of the true rule decided at the previous close
    for t in range(n):
        if data == "garch_t":
            if t > 0:
                h = w + GARCH_A * e_prev * e_prev + GARCH_B * h
            e = np.sqrt(h) * z[:, t]
        else:
            e = s * z[:, t]
        r[:, t] = mu * q + e
        e_prev = e
        x = x + r[:, t]
        C[:, t + 1] = C[:, t] + x
        q = _ma_sign(C, t, f, sl) if (mu != 0.0 and t + 1 >= sl) else np.zeros(m)
    return r


def positions(r: np.ndarray) -> np.ndarray:
    """256 x n positions (int8) of every configuration from one return path (CONFIGS order);
    columns before ``BURN_IN - 1`` are not all defined (set to 0)."""
    x = np.cumsum(r)
    C = np.concatenate([[0.0], np.cumsum(x)])
    n = r.size

    def ma(k):
        out = np.full(n, np.nan)
        out[k - 1:] = (C[k:] - C[:-k]) / k
        return out

    mf = np.stack([ma(k) for k in FAST])
    ms = np.stack([ma(k) for k in SLOW])
    d = mf[:, None, :] - ms[None, :, :]
    p = np.where(np.isnan(d), 0.0, np.sign(d)).reshape(len(CONFIGS), n)
    return p.astype(np.int8)


def strategy_returns(pos: np.ndarray, r: np.ndarray, cost_bps: float) -> Tuple[np.ndarray, np.ndarray]:
    """(net returns, position changes) for days BURN_IN..n-1, shape (configs, n - BURN_IN)."""
    p = pos.astype(np.float64)
    gross = p[:, BURN_IN - 1:-1] * r[BURN_IN:]
    turn = np.abs(p[:, BURN_IN - 1:-1] - p[:, BURN_IN - 2:-2])
    return gross - cost_bps * 1e-4 * turn, turn > 0


def annual_sharpe_rows(R: np.ndarray) -> np.ndarray:
    """Annualised Sharpe of every row (NaN for zero variance)."""
    sd = R.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(sd > 0, R.mean(axis=1) / sd * math.sqrt(PERIODS), np.nan)


def select_best(train_sharpes: np.ndarray, tried: Sequence[int]) -> int:
    """Index (into the search space) of the best training Sharpe among ``tried`` (first on ties; NaN never wins)."""
    v = np.asarray(train_sharpes, dtype=np.float64)[list(tried)]
    v = np.where(np.isnan(v), -np.inf, v)
    return int(list(tried)[int(np.argmax(v))])


@dataclass
class PathEval:
    """Gate metrics of one simulated path for every K (dicts keyed by K)."""
    selected: Dict[int, Tuple[int, int]] = field(default_factory=dict)
    train_sharpe: Dict[int, Optional[float]] = field(default_factory=dict)
    fold_sharpes: Dict[int, List[Optional[float]]] = field(default_factory=dict)
    alpha_t: Dict[int, Optional[float]] = field(default_factory=dict)
    alpha_daily: Dict[int, Optional[float]] = field(default_factory=dict)
    dsr: Dict[int, Optional[float]] = field(default_factory=dict)
    pbo: Dict[int, Optional[float]] = field(default_factory=dict)
    pbo_configs: Dict[int, int] = field(default_factory=dict)
    base_sharpe: Dict[int, Optional[float]] = field(default_factory=dict)
    neighbour_sharpes: Dict[int, List[Optional[float]]] = field(default_factory=dict)
    trades: Dict[int, int] = field(default_factory=dict)
    holdout_sharpe: Dict[int, Optional[float]] = field(default_factory=dict)
    true_train_sharpe: Optional[float] = None


def _f(x) -> Optional[float]:
    return None if x is None or not np.isfinite(x) else float(x)


def evaluate_path(r: np.ndarray, train_days: int, ks: Sequence[int], cost_bps: float,
                  rng: np.random.Generator) -> PathEval:
    """Gate metrics of the best-of-K selection on one return path of length BURN_IN + train_days + HOLDOUT_DAYS."""
    if r.size != BURN_IN + train_days + HOLDOUT_DAYS:
        raise ValueError("path length must be BURN_IN + train_days + HOLDOUT_DAYS")
    pos = positions(r)
    R, changed = strategy_returns(pos, r, cost_bps)
    bench = r[BURN_IN:]
    tr, ho = slice(0, train_days), slice(train_days, train_days + HOLDOUT_DAYS)
    Rtr = R[:, tr]
    s_tr = annual_sharpe_rows(Rtr)
    perm = rng.permutation(len(CONFIGS))
    out = PathEval(true_train_sharpe=_f(s_tr[CONFIG_INDEX[TRUE_CONFIG]]))
    pbo_cache: Dict[int, dict] = {}
    for K in ks:
        tried = perm[:K]
        best = select_best(s_tr, tried)
        f, sl = CONFIGS[best]
        out.selected[K] = (f, sl)
        rb = Rtr[best]
        out.train_sharpe[K] = _f(s_tr[best])
        out.fold_sharpes[K] = fold_sharpes(rb, PERIODS)
        a = alpha_newey_west(rb, bench[tr], PERIODS)
        out.alpha_t[K], out.alpha_daily[K] = a["value"], a["alpha_daily"]
        trial = [float(v) for v in s_tr[tried] if np.isfinite(v)]
        if out.train_sharpe[K] is not None:
            trial.append(out.train_sharpe[K])
        out.dsr[K] = deflated_sharpe(rb, PERIODS, K + 1, trial)["value"]
        if best not in pbo_cache:
            spec = param_spec(f, sl)
            g = _grid.build(spec)
            idx = [CONFIG_INDEX[(c["fast"], c["slow"])] for c in g["configs"]]
            p = pbo_cscv(Rtr[idx].T)
            nb = _grid.neighbours(g, spec)
            pbo_cache[best] = {"pbo": p["value"], "configs": p["configs"],
                               "base": _f(s_tr[idx[g["base_index"]]]),
                               "nb": [_f(s_tr[idx[i]]) for i in nb]}
        c = pbo_cache[best]
        out.pbo[K], out.pbo_configs[K] = c["pbo"], c["configs"]
        out.base_sharpe[K], out.neighbour_sharpes[K] = c["base"], c["nb"]
        out.trades[K] = int(changed[best, tr].sum())
        sd = R[best, ho].std(ddof=1)
        out.holdout_sharpe[K] = _f(R[best, ho].mean() / sd * math.sqrt(PERIODS)) if sd > 0 else None
    return out
