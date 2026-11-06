"""Validation statistics re-implemented from the UFOS strategy-platform SPEC (§9.2, §9.4) and the papers.

Nothing here is imported from the HedgeFund backend; a parity test on the HedgeFund
side compares the two implementations on fixed inputs.

* **Sharpe** (annualised): ``mean(r) / std(r, ddof=1) * sqrt(P)``, zero risk-free rate;
  None for fewer than 2 returns or zero variance. Sums are exactly rounded (``math.fsum``),
  so a constant series has a standard deviation of exactly 0 (not a rounding residue).
* **Probabilistic Sharpe ratio** (Bailey and Lopez de Prado 2012), per-period SR, SR0:
  ``PSR = Phi((SR - SR0) sqrt(n - 1) / sqrt(1 - g3 SR + (g4 - 1) / 4 SR^2))`` with the
  population skewness g3 and the (non-excess) kurtosis g4; a non-positive radicand
  makes it not computable.
* **Deflated Sharpe ratio** (Bailey and Lopez de Prado 2014): DSR = PSR(SR0) with the
  expected maximum Sharpe of N unskilled trials
  ``SR0_a = sqrt(V) ((1 - gamma) Phi^-1(1 - 1/N) + gamma Phi^-1(1 - 1/(N e)))``
  (gamma = Euler-Mascheroni), SR0 = 0 for N = 1, and V = the sample variance (ddof 1) of
  the trials' ANNUALISED Sharpe ratios, floored at ``1 / years`` of the evaluated sample
  (Lo 2002: Var(SR_a) ~ 1 / years for an iid series with true Sharpe 0). SPEC §9.2:
  N = trials + 1. Fewer than 20 returns -> not computable.
* **PBO by CSCV** (Bailey, Borwein, Lopez de Prado and Zhu 2017): the T x N return
  matrix is cut into S = 10 contiguous blocks (sizes differ by at most one row, as
  ``numpy.array_split``); for each of the C(S, S/2) = 252 in-sample block sets, the
  in-sample Sharpe winner n* (first on ties) gets the relative out-of-sample rank
  ``omega = rank(n*) / (N + 1)`` (ascending, average ranks on ties) and the logit
  ``lambda = ln(omega / (1 - omega))``; PBO = share of combinations with lambda <= 0
  (the paper's integral of the logit density up to 0). Sharpe ratios of the block
  unions are computed from per-block sums, sums of squares and counts (identical to
  the direct computation up to rounding); a union with zero variance gets 0 if its
  mean is 0, else +-inf (ranking only). Needs N >= 4 and T >= 5 S rows.
* **Newey-West** (Newey and West 1987) OLS covariance
  ``T / (T - k) (X'X)^-1 S (X'X)^-1`` with Bartlett weights ``1 - l / (L + 1)`` and the
  lag rule of Newey and West (1994) ``L = floor(4 (T / 100)^(2/9))``.
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Dict, Optional, Sequence

import numpy as np
from scipy import stats as _st

EULER_GAMMA = 0.5772156649015329
PBO_BLOCKS = 10
PBO_MIN_CONFIGS = 4
PBO_MIN_ROWS_PER_BLOCK = 5
MIN_RETURNS = 20


def _clean(x) -> np.ndarray:
    a = np.asarray([v for v in x if v is not None], dtype=np.float64)
    return a[np.isfinite(a)]


def sharpe(returns, periods_per_year: int = 1) -> Optional[float]:
    r = _clean(returns)
    if r.size < 2:
        return None
    m, sd = _mean_std(r)
    if sd == 0.0:
        return None
    return m / sd * math.sqrt(periods_per_year)


def _mean_std(r: np.ndarray):
    """Mean and std (ddof 1) with exactly rounded sums (math.fsum): a constant series has std exactly 0."""
    m = math.fsum(r) / r.size
    return m, math.sqrt(math.fsum((r - m) ** 2) / (r.size - 1))


def moments(returns) -> Dict[str, Optional[float]]:
    """{n, mean, std (ddof 1), skew, kurtosis} - skew / kurtosis from population moments, kurtosis NOT excess."""
    r = _clean(returns)
    out: Dict[str, Optional[float]] = {"n": int(r.size), "mean": None, "std": None, "skew": None, "kurtosis": None}
    if r.size < 2:
        return out
    m, sd = _mean_std(r)
    d = r - m
    s0 = math.sqrt(math.fsum(d * d) / r.size)
    out["mean"], out["std"] = m, sd
    if s0 > 0:
        out["skew"] = math.fsum(d ** 3) / r.size / s0 ** 3
        out["kurtosis"] = math.fsum(d ** 4) / r.size / s0 ** 4
    return out


def psr(sr: float, sr0: float, n: int, skew: float, kurtosis: float) -> Optional[float]:
    rad = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    if n < 2 or not rad > 0:
        return None
    return float(_st.norm.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(rad)))


def expected_max_sharpe(n_trials: int, variance: float) -> float:
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if variance < 0:
        raise ValueError("variance must be >= 0")
    if n_trials == 1:
        return 0.0
    n = float(n_trials)
    return math.sqrt(variance) * ((1.0 - EULER_GAMMA) * float(_st.norm.ppf(1.0 - 1.0 / n))
                                  + EULER_GAMMA * float(_st.norm.ppf(1.0 - 1.0 / (n * math.e))))


def deflated_sharpe(returns, periods_per_year: int, n_trials: int, trial_sharpes: Sequence[float]) -> dict:
    """{value | None, reason, sharpe_annual, sr0_annual, trial_variance, variance_source, n_trials}."""
    r = _clean(returns)
    out = {"value": None, "reason": None, "sharpe_annual": None, "sr0_annual": None, "trial_variance": None,
           "variance_source": None, "n_trials": int(n_trials)}
    if n_trials < 1:
        out["reason"] = "the trial count is not known"
        return out
    if r.size < MIN_RETURNS:
        out["reason"] = f"only {r.size} returns (needs {MIN_RETURNS})"
        return out
    mo = moments(r)
    if not mo["std"] or mo["skew"] is None:
        out["reason"] = "zero variance"
        return out
    sr = mo["mean"] / mo["std"]
    out["sharpe_annual"] = sr * math.sqrt(periods_per_year)
    ts = _clean(trial_sharpes)
    vmin = 1.0 / (r.size / float(periods_per_year))
    v = _mean_std(ts)[1] ** 2 if ts.size >= 2 else None
    if v is None or v < vmin:
        out["trial_variance"], out["variance_source"] = vmin, "minimum"
    else:
        out["trial_variance"], out["variance_source"] = v, "trials"
    sr0_a = expected_max_sharpe(int(n_trials), out["trial_variance"])
    out["sr0_annual"] = sr0_a
    p = psr(sr, sr0_a / math.sqrt(periods_per_year), int(r.size), mo["skew"], mo["kurtosis"])
    if p is None:
        out["reason"] = "the skewness / kurtosis term of the PSR is not positive"
        return out
    out["value"] = p
    return out


def _union_sharpe(n: np.ndarray, s: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Sharpe (ddof 1, per period) from counts n, sums s and sums of squares q (arrays over configurations)."""
    m = s / n
    var = (q - s * m) / (n - 1)
    var = np.where(var < 0, 0.0, var)     # rounding below zero
    sd = np.sqrt(var)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = m / sd
    zero = sd == 0
    return np.where(zero, np.where(m > 0, np.inf, np.where(m < 0, -np.inf, 0.0)), out)


def pbo_cscv(matrix, blocks: int = PBO_BLOCKS) -> dict:
    """{value | None, reason, configs, rows, combinations, overfit, mean_logit}; see the module docstring."""
    M = np.asarray(matrix, dtype=np.float64)
    out = {"value": None, "reason": None, "configs": int(M.shape[1]) if M.ndim == 2 else 0,
           "rows": int(M.shape[0]) if M.ndim == 2 else 0, "combinations": 0, "overfit": 0, "mean_logit": None}
    if M.ndim != 2:
        out["reason"] = "not a matrix"
        return out
    T, N = M.shape
    if N < PBO_MIN_CONFIGS:
        out["reason"] = f"needs >= {PBO_MIN_CONFIGS} configurations (has {N})"
        return out
    if blocks < 2 or blocks % 2:
        raise ValueError("blocks must be an even number >= 2")
    if T < PBO_MIN_ROWS_PER_BLOCK * blocks:
        out["reason"] = f"only {T} rows (needs {PBO_MIN_ROWS_PER_BLOCK * blocks})"
        return out
    if not np.all(np.isfinite(M)):
        out["reason"] = "missing values"
        return out
    parts = np.array_split(np.arange(T), blocks)
    cnt = np.array([len(p) for p in parts], dtype=np.float64)
    S = np.stack([M[p].sum(axis=0) for p in parts])          # blocks x N
    Q = np.stack([(M[p] ** 2).sum(axis=0) for p in parts])
    combos = list(combinations(range(blocks), blocks // 2))
    A = np.zeros((len(combos), blocks))                       # in-sample indicator per combination
    for c, is_b in enumerate(combos):
        A[c, list(is_b)] = 1.0
    n_is = (A @ cnt)[:, None]
    s_is, q_is = A @ S, A @ Q
    sh_is = _union_sharpe(n_is, s_is, q_is)
    sh_oos = _union_sharpe(cnt.sum() - n_is, S.sum(axis=0) - s_is, Q.sum(axis=0) - q_is)
    best = np.argmax(sh_is, axis=1)                           # first on ties
    ranks = _st.rankdata(sh_oos, method="average", axis=1)
    omega = ranks[np.arange(len(combos)), best] / (N + 1.0)
    logits = [math.log(w / (1.0 - w)) for w in omega]
    out["combinations"] = len(logits)
    out["overfit"] = sum(1 for x in logits if x <= 0)
    out["mean_logit"] = float(np.mean(logits))
    out["value"] = out["overfit"] / len(logits)
    return out


def nw_lag(n: int) -> int:
    return int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))) if n > 0 else 0


def newey_west_ols(y, X, lag: Optional[int] = None) -> dict:
    """OLS with Newey-West standard errors -> {coef, se, t, lag, n}; ValueError for too few rows / singular X."""
    y = np.asarray(y, dtype=np.float64)
    X = np.asarray(X, dtype=np.float64)
    n, k = X.shape
    if n <= k + 1:
        raise ValueError("too few observations")
    L = nw_lag(n) if lag is None else int(lag)
    XtX = X.T @ X
    if np.linalg.matrix_rank(XtX) < k:
        raise ValueError("singular design matrix")
    inv = np.linalg.inv(XtX)
    b = inv @ (X.T @ y)
    u = X * (y - X @ b)[:, None]
    S = u.T @ u
    for l in range(1, min(L, n - 1) + 1):
        G = u[l:].T @ u[:-l]
        S = S + (1.0 - l / (L + 1.0)) * (G + G.T)
    cov = inv @ S @ inv * (n / float(n - k))
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    t = [float(b[i] / se[i]) if se[i] > 0 else None for i in range(k)]
    return {"coef": [float(v) for v in b], "se": [float(v) for v in se], "t": t, "lag": L, "n": int(n)}


def mean_t_newey_west(x, min_lag: int = 0) -> dict:
    """t-stat of the mean with Newey-West errors (lag = max(rule, min_lag)) -> {value | None, reason, mean, se, lag}."""
    a = _clean(x)
    out = {"value": None, "reason": None, "mean": float(a.mean()) if a.size else None, "se": None, "lag": None,
           "n": int(a.size)}
    if a.size < MIN_RETURNS:
        out["reason"] = f"only {a.size} observations (needs {MIN_RETURNS})"
        return out
    try:
        fit = newey_west_ols(a, np.ones((a.size, 1)), max(nw_lag(a.size), int(min_lag)))
    except ValueError as exc:
        out["reason"] = str(exc)
        return out
    out["se"], out["lag"] = fit["se"][0], fit["lag"]
    if fit["t"][0] is None:
        out["reason"] = "zero variance"
        return out
    out["value"] = fit["t"][0]
    return out


def alpha_newey_west(strategy, benchmark, periods_per_year: int) -> dict:
    """Alpha of strategy on benchmark returns (OLS with intercept) with a Newey-West t ->
    {value (t) | None, reason, alpha_daily, alpha_annual, beta, se, lag, n}."""
    s = np.asarray(strategy, dtype=np.float64)
    b = np.asarray(benchmark, dtype=np.float64)
    ok = np.isfinite(s) & np.isfinite(b)
    s, b = s[ok], b[ok]
    out = {"value": None, "reason": None, "alpha_daily": None, "alpha_annual": None, "beta": None, "se": None,
           "lag": None, "n": int(s.size)}
    if s.size < MIN_RETURNS:
        out["reason"] = f"only {s.size} paired returns (needs {MIN_RETURNS})"
        return out
    if float(np.std(b)) == 0.0:
        out["reason"] = "the benchmark returns have zero variance"
        return out
    try:
        fit = newey_west_ols(s, np.column_stack([np.ones(s.size), b]))
    except ValueError as exc:
        out["reason"] = str(exc)
        return out
    out["alpha_daily"], out["beta"] = fit["coef"][0], fit["coef"][1]
    out["alpha_annual"] = fit["coef"][0] * periods_per_year
    out["se"], out["lag"] = fit["se"][0], fit["lag"]
    if fit["t"][0] is None:
        out["reason"] = "zero residual variance"
        return out
    out["value"] = fit["t"][0]
    return out
