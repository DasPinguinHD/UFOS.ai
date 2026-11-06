"""Sensitivity analysis for any Risk Lab model: one-way (tornado), two-way tables, and Sobol indices.

A model is a function of named inputs. Local and grid methods take a scalar model
``f(params: Dict[str, float]) -> float``; the Sobol estimator takes a vectorised model
``f(params: Dict[str, np.ndarray]) -> np.ndarray`` (:func:`rowwise` wraps a scalar model).

**One-way (tornado).** Each input is moved alone to the low and the high end of its range, all
others at the base value; the swing ``|f(high) - f(low)|`` ranks the inputs. Local: interactions
and the shape between the ends are not seen.

**Two-way table.** ``f`` on the grid of two inputs, the others at base. Cells where the model
raises one of the exception types passed in ``invalid`` (e.g. a DCF with discount rate <= terminal
growth) are NaN and counted - an explicit, opt-in rule; any other exception propagates.

**Sobol indices** (variance-based, global; Sobol' 1993 / 2001). For independent inputs X_i,
``S_i = V[E(Y | X_i)] / V(Y)`` (first order: the share of the output variance explained by X_i
alone) and ``S_Ti = E[V(Y | X_~i)] / V(Y)`` (total order: X_i including all its interactions).
Sampling scheme and estimators of Saltelli et al. (2010), Table 2 (implemented here from the
formulas; the v0.9 review confirmed them against the paper and found them consistent with the
SALib implementation; the tests check them against the analytic indices of the Ishigami function
and of a linear model):

* two independent N x k sample matrices A and B (plain Monte Carlo here, so that the bootstrap
  error below is valid; Saltelli et al. use quasi-random sequences); ``A_B^(i)`` = A with its
  column i taken from B; cost N (k + 2) model runs;
* first order (Saltelli et al. 2010, estimator (b), after Sobol' 2007):
  ``V_i = 1/N sum_j f(B)_j (f(A_B^(i))_j - f(A)_j)``;
* total order (Jansen 1999; Saltelli et al. 2010, estimator (f)):
  ``V_Ti = 1/(2N) sum_j (f(A)_j - f(A_B^(i))_j)^2``;
* ``V(Y)`` = the variance of the 2N outputs f(A), f(B); ``S_i = V_i / V(Y)``, ``S_Ti = V_Ti / V(Y)``;
* centring (a numerical-stability variant of estimator (b), not a different estimand): in the
  first-order estimator f(B) is replaced by f(B) - c, with c the mean of the 2N outputs. Because
  ``E[f(A_B^(i)) - f(A)] = 0`` the estimand is unchanged; estimating c adds an O(1/N) bias. It
  removes the dependence of the estimator's variance on the level of the output (without it, a
  model whose mean is large against its spread - e.g. a value per share of 30 +- 4 - gives very
  noisy first-order indices). The total-order estimator needs no centring.

Monte-Carlo error: a bootstrap over the N rows (the rows of A, B and every A_B^(i) are resampled
together; the variance is re-estimated in each resample) gives a standard error and a percentile
interval for every index. The estimates are unbiased only asymptotically; small N can give
indices slightly below 0 or above 1 - they are reported as estimated, never clipped.

Inputs must be independent (the indices are defined for independent inputs); dependencies are not
modelled. Distributions are given by their quantile function (:class:`Uniform`, :class:`Normal`,
:class:`DiscreteUniform` for whole-number inputs such as a number of years).

References: Sobol', I. M. (2001), Global sensitivity indices for nonlinear mathematical models and
their Monte Carlo estimates, Math. Comput. Simul. 55, 271-280; Jansen, M. J. W. (1999), Analysis of
variance designs for model output, Comput. Phys. Commun. 117, 35-43; Saltelli, A. et al. (2010),
Variance based sensitivity analysis of model output. Design and estimator for the total
sensitivity index, Comput. Phys. Commun. 181, 259-270; Ishigami, T. and Homma, T. (1990), An
importance quantification technique in uncertainty analysis for computer models, ISUMA '90.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Type

import numpy as np
from scipy import stats

from .conventions import SeedLike, make_rng

ScalarModel = Callable[[Dict[str, float]], float]
VectorModel = Callable[[Dict[str, np.ndarray]], np.ndarray]


# ---- one-way and two-way --------------------------------------------------------------------

@dataclass(frozen=True)
class TornadoBar:
    name: str
    low: float
    high: float
    y_low: float
    y_high: float
    base_y: float

    @property
    def swing(self) -> float:
        return abs(self.y_high - self.y_low)


def tornado(f: ScalarModel, base: Mapping[str, float], ranges: Mapping[str, Tuple[float, float]]) -> List[TornadoBar]:
    """One-way sensitivity of ``f`` to every input in ``ranges`` (low, high), largest swing first
    (ties keep the order of ``ranges``)."""
    unknown = set(ranges) - set(base)
    if unknown:
        raise KeyError(f"ranges for inputs not in base: {sorted(unknown)}")
    y0 = float(f(dict(base)))
    bars = []
    for name, (lo, hi) in ranges.items():
        y_lo = float(f({**base, name: lo}))
        y_hi = float(f({**base, name: hi}))
        bars.append(TornadoBar(name, lo, hi, y_lo, y_hi, y0))
    order = sorted(range(len(bars)), key=lambda i: (-bars[i].swing, i))
    return [bars[i] for i in order]


@dataclass(frozen=True)
class TwoWayTable:
    x_name: str
    x_values: np.ndarray
    y_name: str
    y_values: np.ndarray
    z: np.ndarray            # shape (len(y_values), len(x_values)); NaN = invalid input combination
    invalid: int


def two_way(f: ScalarModel, base: Mapping[str, float], x_name: str, x_values: Sequence[float], y_name: str,
            y_values: Sequence[float], invalid: Tuple[Type[BaseException], ...] = ()) -> TwoWayTable:
    """``f`` on the grid ``y_values x x_values`` (rows = y). Exceptions of the types in ``invalid`` give NaN
    (counted in ``invalid``); every other exception propagates."""
    for n in (x_name, y_name):
        if n not in base:
            raise KeyError(f"{n!r} not in base")
    xs, ys = np.asarray(x_values, dtype=np.float64), np.asarray(y_values, dtype=np.float64)
    z = np.empty((ys.size, xs.size))
    bad = 0
    for i, yv in enumerate(ys):
        for j, xv in enumerate(xs):
            try:
                z[i, j] = float(f({**base, x_name: float(xv), y_name: float(yv)}))
            except invalid:
                z[i, j] = np.nan
                bad += 1
    return TwoWayTable(x_name, xs, y_name, ys, z, bad)


# ---- distributions ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Uniform:
    lo: float
    hi: float

    def __post_init__(self):
        if not self.hi > self.lo:
            raise ValueError("Uniform needs hi > lo")

    def ppf(self, u: np.ndarray) -> np.ndarray:
        return self.lo + (self.hi - self.lo) * u


@dataclass(frozen=True)
class Normal:
    mu: float
    sd: float

    def __post_init__(self):
        if not self.sd > 0:
            raise ValueError("Normal needs sd > 0")

    def ppf(self, u: np.ndarray) -> np.ndarray:
        return self.mu + self.sd * stats.norm.ppf(u)


@dataclass(frozen=True)
class DiscreteUniform:
    """Equally likely whole numbers lo, lo + 1, ..., hi (as floats)."""
    lo: int
    hi: int

    def __post_init__(self):
        if int(self.lo) != self.lo or int(self.hi) != self.hi or not self.hi > self.lo:
            raise ValueError("DiscreteUniform needs whole numbers hi > lo")

    def ppf(self, u: np.ndarray) -> np.ndarray:
        k = self.hi - self.lo + 1
        return self.lo + np.minimum(np.floor(np.asarray(u) * k), k - 1)


def rowwise(f: ScalarModel) -> VectorModel:
    """Wrap a scalar model for :func:`sobol_indices` (one call per row)."""
    def g(params: Dict[str, np.ndarray]) -> np.ndarray:
        names = list(params)
        n = len(params[names[0]])
        return np.asarray([f({k: float(params[k][j]) for k in names}) for j in range(n)], dtype=np.float64)
    return g


# ---- Sobol indices ------------------------------------------------------------------------------

@dataclass(frozen=True)
class SobolResult:
    names: Tuple[str, ...]
    first: np.ndarray
    total: np.ndarray
    first_se: np.ndarray
    total_se: np.ndarray
    first_ci: np.ndarray       # shape (k, 2), bootstrap percentile interval
    total_ci: np.ndarray
    variance: float
    mean: float
    n: int
    evaluations: int
    n_boot: int
    ci_level: float

    def as_rows(self) -> List[Dict[str, float]]:
        return [{"name": nm, "first": float(self.first[i]), "first_se": float(self.first_se[i]),
                 "first_lo": float(self.first_ci[i, 0]), "first_hi": float(self.first_ci[i, 1]),
                 "total": float(self.total[i]), "total_se": float(self.total_se[i]),
                 "total_lo": float(self.total_ci[i, 0]), "total_hi": float(self.total_ci[i, 1])}
                for i, nm in enumerate(self.names)]


def _indices(fA: np.ndarray, fB: np.ndarray, fAB: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """First and total indices from f(A) (N,), f(B) (N,), f(A_B^(i)) (k, N)."""
    both = np.concatenate([fA, fB])
    v = np.var(both)
    if not v > 0:
        raise ValueError("the model output has zero variance: Sobol indices are undefined")
    c = both.mean()          # centring (module docstring): same expectation, variance independent of the level
    vi = np.mean((fB - c)[None, :] * (fAB - fA[None, :]), axis=1)
    vti = 0.5 * np.mean((fA[None, :] - fAB) ** 2, axis=1)
    return vi / v, vti / v


def sobol_indices(f: VectorModel, dists: Mapping[str, object], n: int, seed: SeedLike, n_boot: int = 500,
                  ci_level: float = 0.95) -> SobolResult:
    """First- and total-order Sobol indices of ``f`` for independent inputs ``dists`` (name -> distribution with
    ``ppf``), with N = ``n`` base rows (module docstring). Bootstrap with ``n_boot`` resamples (0 = none)."""
    if n < 2:
        raise ValueError("n must be >= 2")
    if not 0 < ci_level < 1:
        raise ValueError("ci_level must be in (0, 1)")
    names = tuple(dists)
    k = len(names)
    if k < 1:
        raise ValueError("at least one input is needed")
    rng = make_rng(seed)
    UA, UB = rng.random((n, k)), rng.random((n, k))
    XA = {nm: dists[nm].ppf(UA[:, i]) for i, nm in enumerate(names)}
    XB = {nm: dists[nm].ppf(UB[:, i]) for i, nm in enumerate(names)}

    def run(X) -> np.ndarray:
        y = np.asarray(f(X), dtype=np.float64)
        if y.shape != (n,):
            raise ValueError(f"the model must return shape ({n},), got {y.shape}")
        if not np.all(np.isfinite(y)):
            raise ValueError("the model returned non-finite values")
        return y

    fA, fB = run(XA), run(XB)
    fAB = np.empty((k, n))
    for i, nm in enumerate(names):
        fAB[i] = run({**XA, nm: XB[nm]})
    S, ST = _indices(fA, fB, fAB)
    if n_boot > 0:
        bs, bt = np.empty((n_boot, k)), np.empty((n_boot, k))
        for b in range(n_boot):
            j = rng.integers(0, n, size=n)
            bs[b], bt[b] = _indices(fA[j], fB[j], fAB[:, j])
        q = [(1 - ci_level) / 2 * 100, (1 + ci_level) / 2 * 100]
        s_se, t_se = bs.std(axis=0, ddof=1), bt.std(axis=0, ddof=1)
        s_ci, t_ci = np.percentile(bs, q, axis=0).T, np.percentile(bt, q, axis=0).T
    else:
        s_se = t_se = np.full(k, np.nan)
        s_ci = t_ci = np.full((k, 2), np.nan)
    allf = np.concatenate([fA, fB])
    return SobolResult(names, S, ST, s_se, t_se, s_ci, t_ci, float(np.var(allf)), float(np.mean(allf)), n,
                       n * (k + 2), n_boot, ci_level)


# ---- test functions ------------------------------------------------------------------------------

def ishigami(params: Mapping[str, np.ndarray], a: float = 7.0, b: float = 0.1) -> np.ndarray:
    """Ishigami-Homma function ``sin x1 + a sin^2 x2 + b x3^4 sin x1`` (inputs x1, x2, x3 ~ U(-pi, pi))."""
    x1, x2, x3 = (np.asarray(params[k], dtype=np.float64) for k in ("x1", "x2", "x3"))
    return np.sin(x1) + a * np.sin(x2) ** 2 + b * x3 ** 4 * np.sin(x1)


ISHIGAMI_INPUTS = {"x1": Uniform(-math.pi, math.pi), "x2": Uniform(-math.pi, math.pi), "x3": Uniform(-math.pi, math.pi)}
