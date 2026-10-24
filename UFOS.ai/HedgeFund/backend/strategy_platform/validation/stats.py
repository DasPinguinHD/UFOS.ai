"""Validation statistics (SPEC §9.2, §9.4): deflated Sharpe ratio, PBO via
CSCV, Newey-West t-statistics and small helpers. Pure functions on plain
sequences / numpy arrays (numpy imported lazily); no I/O.

A statistic that cannot be computed returns ``value None`` together with a
``reason`` — the gates turn that into a FAIL (never a default value).

Formulas
--------
Per-period returns r_1..r_T (T observations, P periods per year).

- Sharpe (per period) SR = mean(r) / std(r, ddof=1); annualised SR_a =
  SR * sqrt(P) (zero risk-free rate, as backtest/metrics.py).
- Skewness g3 = mean((r - m)^3) / s^3 and kurtosis g4 = mean((r - m)^4) / s^4
  (NOT excess; s = population std, ddof 0).
- Probabilistic Sharpe ratio (Bailey & López de Prado 2012):
  PSR(SR0) = Phi( (SR - SR0) * sqrt(T - 1) / sqrt(1 - g3 * SR + (g4 - 1) / 4 * SR^2) )
  with SR and SR0 PER PERIOD. A non-positive radicand -> not computable.
- Expected maximum Sharpe of N independent trials (Bailey & López de Prado
  2014, "The Deflated Sharpe Ratio", eq. for SR0):
  SR0_a = sqrt(V) * ((1 - gamma) * PhiInv(1 - 1/N) + gamma * PhiInv(1 - 1/(N e)))
  with gamma = Euler-Mascheroni constant, e = Euler's number and V = the
  variance of the ANNUALISED Sharpe ratios of the trials. N = 1 -> SR0 = 0
  (no selection happened). SR0 per period = SR0_a / sqrt(P).
- Deflated Sharpe ratio DSR = PSR(SR0) (the probability that the true
  Sharpe exceeds the expected maximum of N unskilled trials).
- Trial variance V = sample variance (ddof 1) of the trials' annualised
  Sharpe ratios, floored at the documented minimum
  V_min = 1 / years (years = T / P of the evaluated sample): the asymptotic
  variance of an annualised Sharpe estimate of an i.i.d. series with a true
  Sharpe of 0 (Lo 2002, Var(SR_a) ~ (1 + SR^2 / 2) / years). Re-runs of the
  same backtest count as trials (SPEC §9.2) but have no dispersion; the floor
  keeps N such trials from deflating nothing. With 1 trial V = V_min (and
  SR0 = 0 anyway).
- PBO by combinatorially symmetric cross-validation (Bailey, Borwein,
  López de Prado & Zhu 2017): the T x N matrix of daily returns of N
  configurations is cut into S contiguous blocks (S = 10, numpy.array_split:
  sizes differ by at most one row); for each of the C(S, S/2) = 252 ways to
  choose S/2 blocks as in-sample (IS, the rest out-of-sample OOS, both in
  time order): n* = argmax of the IS Sharpe (first on ties); omega = rank of
  n*'s OOS Sharpe among all N (ascending, average ranks on ties) / (N + 1);
  logit lambda = ln(omega / (1 - omega)). PBO = share of combinations with
  lambda <= 0 (the IS winner is at or below the OOS median). Sharpe on a
  block set with zero variance: 0 when its mean is 0, else +-inf (used for
  ranking only). Needs N >= 4 configurations and T >= 5 * S rows.
- Newey-West HAC (Newey & West 1987) for OLS y = X b + e:
  Cov(b) = T / (T - k) * (X'X)^-1 S (X'X)^-1, S = sum_t u_t u_t' + sum_{l=1..L}
  w_l sum_t (u_t u_{t-l}' + u_{t-l} u_t'), u_t = x_t e_t, Bartlett weights
  w_l = 1 - l / (L + 1). Lag rule (Newey & West 1994):
  L = floor(4 * (T / 100) ** (2 / 9)). For a mean of overlapping h-day
  quantities (h-day forward IC) L = max(rule, h - 1).
- Alpha: OLS of strategy returns on benchmark returns (daily, both accounts
  valued on both days, as metrics.benchmark_stats); alpha_annual =
  alpha_daily * P; t = alpha_daily / se_NW(alpha_daily).

Normal CDF via math.erfc; the inverse by Acklam's rational approximation
refined with one Halley step (|error| < 1e-12 on (1e-300, 1 - 1e-16)).
scipy is not used (not installed on the test interpreter).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Dict, List, Optional, Sequence

EULER_GAMMA = 0.5772156649015329
PBO_BLOCKS = 10
PBO_MIN_CONFIGS = 4
PBO_MIN_ROWS_PER_BLOCK = 5
MIN_RETURNS = 20              # fewer daily returns -> a Sharpe-based statistic is not computed


# --- normal distribution ------------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02,
      -3.066479806614716e+01, 2.506628277459239e+00)
_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01,
      -1.328068155288572e+01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00,
      4.374664141464968e+00, 2.938163982698783e+00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00)


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF for 0 < p < 1 (ValueError otherwise)."""
    if not (0.0 < p < 1.0):
        raise ValueError("norm_ppf: p must lie in (0, 1)")
    lo = 0.02425
    if p < lo:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    elif p <= 1.0 - lo:
        q = p - 0.5
        r = q * q
        x = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / \
            (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    # one Halley step on Phi(x) - p
    e = norm_cdf(x) - p
    u = e * math.sqrt(2.0 * math.pi) * math.exp(x * x / 2.0)
    return x - u / (1.0 + x * u / 2.0)


# --- moments / Sharpe --------------------------------------------------------------------------

def _clean(xs: Sequence[float]) -> List[float]:
    return [float(x) for x in xs if x is not None and math.isfinite(float(x))]


def mean(xs: Sequence[float]) -> Optional[float]:
    return math.fsum(xs) / len(xs) if len(xs) else None


def std(xs: Sequence[float], ddof: int = 1) -> Optional[float]:
    n = len(xs)
    if n - ddof <= 0:
        return None
    m = math.fsum(xs) / n
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (n - ddof))


def sharpe(returns: Sequence[float], periods_per_year: int = 1) -> Optional[float]:
    """Annualised Sharpe (per period when periods_per_year = 1); None for < 2 returns or zero variance."""
    r = _clean(returns)
    sd = std(r)
    if sd is None or sd == 0:
        return None
    return mean(r) / sd * math.sqrt(periods_per_year)


def moments(returns: Sequence[float]) -> dict:
    """{n, mean, std, skew, kurtosis} (kurtosis NOT excess; population moments for skew/kurtosis)."""
    r = _clean(returns)
    n = len(r)
    out = {"n": n, "mean": None, "std": None, "skew": None, "kurtosis": None}
    if n < 2:
        return out
    m = math.fsum(r) / n
    s0 = math.sqrt(math.fsum((x - m) ** 2 for x in r) / n)
    out["mean"], out["std"] = m, std(r)
    if s0 > 0:
        out["skew"] = math.fsum((x - m) ** 3 for x in r) / n / s0 ** 3
        out["kurtosis"] = math.fsum((x - m) ** 4 for x in r) / n / s0 ** 4
    return out


def psr(sr: float, sr0: float, n: int, skew: float, kurtosis: float) -> Optional[float]:
    """Probabilistic Sharpe ratio (per-period SR / SR0); None when the variance term is not positive."""
    rad = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    if n < 2 or not (rad > 0):
        return None
    return norm_cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(rad))


def expected_max_sharpe(n_trials: int, variance: float) -> float:
    """Expected maximum (annualised) Sharpe of ``n_trials`` unskilled trials with Sharpe variance ``variance``."""
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if variance < 0:
        raise ValueError("variance must be >= 0")
    if n_trials == 1:
        return 0.0
    n = float(n_trials)
    return math.sqrt(variance) * ((1.0 - EULER_GAMMA) * norm_ppf(1.0 - 1.0 / n)
                                  + EULER_GAMMA * norm_ppf(1.0 - 1.0 / (n * math.e)))


def min_trial_variance(n_returns: int, periods_per_year: int) -> float:
    """The documented minimum V_min = 1 / years of the evaluated sample (module docstring)."""
    years = n_returns / float(periods_per_year)
    return 1.0 / years


def deflated_sharpe(returns: Sequence[float], periods_per_year: int, n_trials: int,
                    trial_sharpes: Sequence[float]) -> dict:
    """Deflated Sharpe ratio of ``returns`` (module docstring) ->
    {value (probability) | None, reason, sharpe_annual, sr0_annual, n_trials, trial_variance,
    variance_source ("trials" | "minimum"), trials_used, skew, kurtosis, returns}."""
    r = _clean(returns)
    out: Dict[str, object] = {"value": None, "reason": None, "sharpe_annual": None, "sr0_annual": None,
                              "n_trials": int(n_trials), "trial_variance": None, "variance_source": None,
                              "trials_used": 0, "skew": None, "kurtosis": None, "returns": len(r)}
    if n_trials < 1:
        out["reason"] = "the trial count is not known (needs >= 1)"
        return out
    if len(r) < MIN_RETURNS:
        out["reason"] = f"only {len(r)} daily returns (needs {MIN_RETURNS})"
        return out
    mo = moments(r)
    if not mo["std"] or mo["skew"] is None:
        out["reason"] = "the daily returns have zero variance"
        return out
    sr = mo["mean"] / mo["std"]
    out["sharpe_annual"] = sr * math.sqrt(periods_per_year)
    out["skew"], out["kurtosis"] = mo["skew"], mo["kurtosis"]
    ts = _clean(trial_sharpes)
    out["trials_used"] = len(ts)
    vmin = min_trial_variance(len(r), periods_per_year)
    v = std(ts) ** 2 if len(ts) >= 2 else None
    if v is None or v < vmin:
        out["trial_variance"], out["variance_source"] = vmin, "minimum"
    else:
        out["trial_variance"], out["variance_source"] = v, "trials"
    sr0_a = expected_max_sharpe(int(n_trials), float(out["trial_variance"]))
    out["sr0_annual"] = sr0_a
    p = psr(sr, sr0_a / math.sqrt(periods_per_year), len(r), mo["skew"], mo["kurtosis"])
    if p is None:
        out["reason"] = "the skewness/kurtosis term of the PSR is not positive (extreme return distribution)"
        return out
    out["value"] = p
    return out


# --- PBO (CSCV) ---------------------------------------------------------------------------------

def _block_sharpe(np, block_returns):
    m = block_returns.mean(axis=0)
    sd = block_returns.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = m / sd
    zero = sd == 0
    s = np.where(zero, np.where(m > 0, np.inf, np.where(m < 0, -np.inf, 0.0)), s)
    return s


def _avg_rank(np, values):
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    sv = values[order]
    i, n = 0, len(values)
    while i < n:
        j = i
        while j + 1 < n and sv[j + 1] == sv[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def pbo_cscv(matrix, blocks: int = PBO_BLOCKS) -> dict:
    """Probability of backtest overfitting of a T x N matrix of per-period returns (rows = days in time
    order, columns = configurations; no NaN) -> {value | None, reason, configs, rows, blocks,
    combinations, logits, overfit_combinations, mean_logit}."""
    import numpy as np
    M = np.asarray(matrix, dtype=float)
    out: Dict[str, object] = {"value": None, "reason": None, "configs": int(M.shape[1]) if M.ndim == 2 else 0,
                              "rows": int(M.shape[0]) if M.ndim == 2 else 0, "blocks": blocks,
                              "combinations": 0, "logits": [], "overfit_combinations": 0, "mean_logit": None}
    if M.ndim != 2:
        out["reason"] = "the return matrix is not two-dimensional"
        return out
    T, N = M.shape
    if N < PBO_MIN_CONFIGS:
        out["reason"] = f"needs >= {PBO_MIN_CONFIGS} parameter configurations (has {N})"
        return out
    if blocks < 2 or blocks % 2:
        raise ValueError("blocks must be an even number >= 2")
    if T < PBO_MIN_ROWS_PER_BLOCK * blocks:
        out["reason"] = f"only {T} common days (needs {PBO_MIN_ROWS_PER_BLOCK * blocks} for {blocks} blocks)"
        return out
    if not np.all(np.isfinite(M)):
        out["reason"] = "the return matrix contains missing values"
        return out
    parts = np.array_split(np.arange(T), blocks)
    logits = []
    for is_blocks in combinations(range(blocks), blocks // 2):
        is_idx = np.concatenate([parts[b] for b in is_blocks])
        oos_idx = np.concatenate([parts[b] for b in range(blocks) if b not in is_blocks])
        s_is = _block_sharpe(np, M[is_idx])
        s_oos = _block_sharpe(np, M[oos_idx])
        best = int(np.argmax(s_is))
        omega = _avg_rank(np, s_oos)[best] / (N + 1.0)
        logits.append(math.log(omega / (1.0 - omega)))
    out["combinations"] = len(logits)
    out["logits"] = logits
    out["overfit_combinations"] = sum(1 for x in logits if x <= 0)
    out["mean_logit"] = math.fsum(logits) / len(logits)
    out["value"] = out["overfit_combinations"] / len(logits)
    return out


# --- Newey-West ---------------------------------------------------------------------------------

def nw_lag(n: int) -> int:
    """Newey & West (1994) automatic lag: floor(4 (n / 100) ** (2 / 9))."""
    return int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))) if n > 0 else 0


def newey_west_ols(y, X, lag: Optional[int] = None) -> dict:
    """OLS with Newey-West standard errors -> {coef, se, t, lag, n} (lists in column order of X);
    raises ValueError for a singular design or too few rows."""
    import numpy as np
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float)
    n, k = X.shape
    if n <= k + 1:
        raise ValueError("too few observations for the regression")
    L = nw_lag(n) if lag is None else int(lag)
    XtX = X.T @ X
    if np.linalg.matrix_rank(XtX) < k:
        raise ValueError("singular design matrix")
    inv = np.linalg.inv(XtX)
    b = inv @ X.T @ y
    e = y - X @ b
    U = X * e[:, None]
    S = U.T @ U
    for l in range(1, min(L, n - 1) + 1):
        w = 1.0 - l / (L + 1.0)
        G = U[l:].T @ U[:-l]
        S += w * (G + G.T)
    cov = inv @ S @ inv * (n / float(n - k))
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(se > 0, b / se, np.nan)
    return {"coef": [float(v) for v in b], "se": [float(v) for v in se],
            "t": [None if not math.isfinite(float(v)) else float(v) for v in t], "lag": L, "n": int(n)}


def mean_t_newey_west(xs: Sequence[float], min_lag: int = 0) -> dict:
    """t-stat of the mean of ``xs`` with Newey-West errors, lag = max(rule, min_lag) ->
    {value (t) | None, reason, mean, se, lag, n}."""
    x = _clean(xs)
    out = {"value": None, "reason": None, "mean": mean(x), "se": None, "lag": None, "n": len(x)}
    if len(x) < MIN_RETURNS:
        out["reason"] = f"only {len(x)} observations (needs {MIN_RETURNS})"
        return out
    import numpy as np
    lag = max(nw_lag(len(x)), int(min_lag))
    try:
        fit = newey_west_ols(np.asarray(x), np.ones((len(x), 1)), lag)
    except ValueError as ex:
        out["reason"] = str(ex)
        return out
    out["se"], out["lag"] = fit["se"][0], fit["lag"]
    if fit["t"][0] is None:
        out["reason"] = "zero variance"
        return out
    out["value"] = fit["t"][0]
    return out


def alpha_newey_west(strategy_returns: Sequence[float], benchmark_returns: Sequence[float],
                     periods_per_year: int) -> dict:
    """Alpha of the strategy vs the benchmark (paired daily returns) with a Newey-West t-stat ->
    {value (t) | None, reason, alpha_daily, alpha_annual, beta, se, lag, n}."""
    import numpy as np
    pairs = [(float(a), float(b)) for a, b in zip(strategy_returns, benchmark_returns)
             if a is not None and b is not None and math.isfinite(float(a)) and math.isfinite(float(b))]
    out = {"value": None, "reason": None, "alpha_daily": None, "alpha_annual": None, "beta": None, "se": None,
           "lag": None, "n": len(pairs)}
    if len(pairs) < MIN_RETURNS:
        out["reason"] = f"only {len(pairs)} days with both accounts valued (needs {MIN_RETURNS})"
        return out
    rs = np.array([p[0] for p in pairs])
    rb = np.array([p[1] for p in pairs])
    if float(np.std(rb)) == 0.0:
        out["reason"] = "the benchmark returns have zero variance"
        return out
    try:
        fit = newey_west_ols(rs, np.column_stack([np.ones(len(rs)), rb]))
    except ValueError as ex:
        out["reason"] = str(ex)
        return out
    out["alpha_daily"], out["beta"] = fit["coef"][0], fit["coef"][1]
    out["alpha_annual"] = fit["coef"][0] * periods_per_year
    out["se"], out["lag"] = fit["se"][0], fit["lag"]
    if fit["t"][0] is None:
        out["reason"] = "the regression residuals have zero variance"
        return out
    out["value"] = fit["t"][0]
    return out
