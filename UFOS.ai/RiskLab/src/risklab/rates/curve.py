"""Yield curves: Nelson-Siegel (NS) and Nelson-Siegel-Svensson (NSS), fitting, and a synthetic curve generator.

Conventions:

* time ``t`` in years (ACT/365-style year fractions; no day-count calendar is modelled);
* rates are decimals (0.03 = 3 %), **continuously compounded zero rates** ``z(t)``;
  discount factor ``D(t) = exp(-z(t) t)``; instantaneous forward ``f(t) = d/dt [z(t) t]``;
* conversions: annual compounding ``r_a = exp(z) - 1``; m-times-a-year compounding
  ``r_m = m (exp(z / m) - 1)`` (:func:`to_compounded`, :func:`from_compounded`);
* par yield of an annual-coupon bond with maturity T (integer years):
  ``c(T) = (1 - D(T)) / sum_{i=1..T} D(i)`` (:func:`par_yield`).

Nelson and Siegel (1987), Svensson (1994), zero-rate form with ``x = t / tau``::

    z(t) = b0 + b1 (1 - e^-x) / x + b2 ((1 - e^-x) / x - e^-x)                [NS]
         + b3 ((1 - e^-y) / y - e^-y),  y = t / tau2                           [NSS]
    f(t) = b0 + b1 e^-x + b2 x e^-x + b3 y e^-y

``z(0) = f(0) = b0 + b1`` (the limit), ``z(inf) = b0``. The same parametrisation is used for
the NSS parameters some central banks publish (e.g. the Deutsche Bundesbank's term-structure
estimates); users can enter such parameters through :class:`UserCurveParameters` - nothing is
downloaded and no curve data are committed.

Fitting (:func:`fit_curve`): least squares on zero rates or par yields with bounds
(``tau``, ``tau2`` in ``[TAU_MIN, TAU_MAX]``, betas in ``[-1, 1]``) from a fixed grid of
deterministic starting values; the best converged fit wins; if no start converges, or the best
root-mean-square error exceeds ``max_rmse``, :class:`risklab.models.FitError` is raised.

Synthetic curves (:func:`dns_factors`): the dynamic Nelson-Siegel model of Diebold and Li (2006)
with level / slope / curvature as independent AR(1) processes and a fixed ``tau``, monthly
steps; the parameters are stated in :data:`DNS_DEFAULTS` (synthetic, chosen for a plausible EUR
curve, not estimated from data).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Sequence, Tuple

import numpy as np
from scipy import optimize

from risklab.conventions import SeedLike, make_rng
from risklab.models.base import FitError

TAU_MIN, TAU_MAX = 0.05, 30.0
BETA_BOUND = 1.0


def _loading(t: np.ndarray, tau: float) -> Tuple[np.ndarray, np.ndarray]:
    """((1 - e^-x) / x, (1 - e^-x) / x - e^-x) with the t -> 0 limits (1, 0)."""
    t = np.asarray(t, dtype=np.float64)
    x = t / tau
    small = x < 1e-8
    xs = np.where(small, 1.0, x)
    a = np.where(small, 1.0 - x / 2.0, -np.expm1(-xs) / xs)
    return a, a - np.exp(-x)


@dataclass(frozen=True)
class NelsonSiegel:
    """Nelson-Siegel(-Svensson) curve; ``b3 = 0`` (and any ``tau2``) gives plain NS."""
    b0: float
    b1: float
    b2: float
    tau: float
    b3: float = 0.0
    tau2: float = 1.0

    def __post_init__(self):
        if not (self.tau > 0 and self.tau2 > 0):
            raise ValueError("tau and tau2 must be > 0")

    @property
    def is_svensson(self) -> bool:
        return self.b3 != 0.0

    def zero(self, t) -> np.ndarray:
        a1, a2 = _loading(t, self.tau)
        z = self.b0 + self.b1 * a1 + self.b2 * a2
        if self.b3 != 0.0:
            z = z + self.b3 * _loading(t, self.tau2)[1]
        return z

    def forward(self, t) -> np.ndarray:
        t = np.asarray(t, dtype=np.float64)
        x = t / self.tau
        f = self.b0 + self.b1 * np.exp(-x) + self.b2 * x * np.exp(-x)
        if self.b3 != 0.0:
            y = t / self.tau2
            f = f + self.b3 * y * np.exp(-y)
        return f

    def discount(self, t) -> np.ndarray:
        t = np.asarray(t, dtype=np.float64)
        return np.exp(-self.zero(t) * t)

    def params(self) -> Dict[str, float]:
        return {"b0": self.b0, "b1": self.b1, "b2": self.b2, "tau": self.tau, "b3": self.b3, "tau2": self.tau2}


class ZeroCurve:
    """A curve given by a zero-rate function ``z(t)`` (continuous compounding); shocked curves are of this type."""

    def __init__(self, zero_fn: Callable[[np.ndarray], np.ndarray], label: str = ""):
        self._z = zero_fn
        self.label = label

    def zero(self, t) -> np.ndarray:
        return np.asarray(self._z(np.asarray(t, dtype=np.float64)), dtype=np.float64)

    def discount(self, t) -> np.ndarray:
        t = np.asarray(t, dtype=np.float64)
        return np.exp(-self.zero(t) * t)

    def forward(self, t, h: float = 1e-5) -> np.ndarray:
        """Instantaneous forward by a central difference of ``z(t) t``."""
        t = np.asarray(t, dtype=np.float64)
        lo = np.maximum(t - h, 0.0)
        hi = t + h
        return (self.zero(hi) * hi - self.zero(lo) * lo) / (hi - lo)


def as_zero_curve(curve) -> ZeroCurve:
    return curve if isinstance(curve, ZeroCurve) else ZeroCurve(curve.zero, getattr(curve, "label", ""))


def shifted(curve, shift: Callable[[np.ndarray], np.ndarray], label: str = "",
            floor: Optional[Callable[[np.ndarray], np.ndarray]] = None) -> ZeroCurve:
    """``z(t) + shift(t)``, optionally floored at ``floor(t)`` (applied to the shifted rate)."""
    def z(t):
        s = curve.zero(t) + shift(t)
        return np.maximum(s, floor(t)) if floor is not None else s
    return ZeroCurve(z, label)


def to_compounded(z, m: Optional[int] = 1):
    """Continuous zero rate -> m-times-a-year compounded rate (m = 1: annual; m = None: continuous)."""
    z = np.asarray(z, dtype=np.float64)
    return z if m is None else m * np.expm1(z / m)


def from_compounded(r, m: Optional[int] = 1):
    r = np.asarray(r, dtype=np.float64)
    return r if m is None else m * np.log1p(r / m)


def par_yield(curve, maturities: Sequence[int]) -> np.ndarray:
    """Annual-coupon par yields for integer maturities (years)."""
    out = []
    for T in maturities:
        T = int(T)
        if T < 1:
            raise ValueError("par yields need integer maturities >= 1")
        d = curve.discount(np.arange(1, T + 1, dtype=np.float64))
        out.append((1.0 - d[-1]) / d.sum())
    return np.asarray(out)


@dataclass(frozen=True)
class CurveFit:
    curve: NelsonSiegel
    rmse: float
    kind: str
    model: str
    starts: int
    converged: int


def _starts(model: str, rates: np.ndarray):
    lvl = float(rates[-1])
    slope = float(rates[0] - rates[-1])
    for tau in (0.5, 1.5, 3.0, 6.0):
        for b2 in (-0.02, 0.0, 0.02):
            if model == "ns":
                yield [lvl, slope, b2, tau]
            else:
                for tau2 in (tau * 3.0, 10.0):
                    yield [lvl, slope, b2, tau, 0.0, tau2]


def fit_curve(maturities: Sequence[float], rates: Sequence[float], kind: str = "zero", model: str = "ns",
              max_rmse: float = 5e-4) -> CurveFit:
    """Fit NS (``model="ns"``) or NSS (``"nss"``) to zero rates (``kind="zero"``, continuous compounding) or
    annual-coupon par yields (``kind="par"``, integer maturities); see the module docstring."""
    t = np.asarray(maturities, dtype=np.float64)
    y = np.asarray(rates, dtype=np.float64)
    if t.ndim != 1 or t.shape != y.shape or t.size < (4 if model == "ns" else 6):
        raise ValueError("need matching 1-D maturities / rates with at least as many points as parameters")
    if kind not in ("zero", "par") or model not in ("ns", "nss"):
        raise ValueError("kind must be 'zero' or 'par', model 'ns' or 'nss'")
    if kind == "par" and not np.all(np.mod(t, 1.0) == 0):
        raise ValueError("par yields need integer maturities")

    def make(p):
        if model == "ns":
            return NelsonSiegel(p[0], p[1], p[2], p[3])
        return NelsonSiegel(p[0], p[1], p[2], p[3], p[4], p[5])

    def resid(p):
        c = make(p)
        return (c.zero(t) if kind == "zero" else par_yield(c, t)) - y

    n = 4 if model == "ns" else 6
    lo = [-BETA_BOUND] * 3 + [TAU_MIN] + ([-BETA_BOUND, TAU_MIN] if n == 6 else [])
    hi = [BETA_BOUND] * 3 + [TAU_MAX] + ([BETA_BOUND, TAU_MAX] if n == 6 else [])
    best, n_starts, n_conv = None, 0, 0
    for p0 in _starts(model, y):
        n_starts += 1
        p0 = np.clip(p0, np.array(lo) + 1e-9, np.array(hi) - 1e-9)
        try:
            r = optimize.least_squares(resid, p0, bounds=(lo, hi), method="trf", xtol=1e-12, ftol=1e-14,
                                       gtol=1e-12, max_nfev=5000)
        except (ValueError, FloatingPointError):
            continue
        if not r.success:
            continue
        n_conv += 1
        rmse = float(np.sqrt(np.mean(r.fun ** 2)))
        if best is None or rmse < best[1] - 1e-15:
            best = (r.x, rmse)
    if best is None:
        raise FitError(f"{model.upper()} fit: no start converged ({n_starts} starts)")
    if best[1] > max_rmse:
        raise FitError(f"{model.upper()} fit: best RMSE {best[1]:.2e} exceeds {max_rmse:.1e}")
    return CurveFit(make(best[0]), best[1], kind, model, n_starts, n_conv)


@dataclass(frozen=True)
class UserCurveParameters:
    """NS / NSS parameters entered by the user (e.g. published central-bank term-structure parameters).

    ``rates_in_percent=True`` if the betas are quoted in percent (as many publications do); the
    curve is built in decimals. The user is responsible for the source and its compounding
    convention (state it in ``source``); nothing is fetched or stored.
    """
    b0: float
    b1: float
    b2: float
    tau: float
    b3: float = 0.0
    tau2: float = 1.0
    rates_in_percent: bool = False
    source: str = "user input"

    def curve(self) -> NelsonSiegel:
        k = 0.01 if self.rates_in_percent else 1.0
        return NelsonSiegel(self.b0 * k, self.b1 * k, self.b2 * k, self.tau, self.b3 * k, self.tau2)


DNS_DEFAULTS = {
    # synthetic, plausible for a EUR swap curve; monthly AR(1): f_{t+1} = mu + phi (f_t - mu) + sd * eps
    "mu": (0.030, -0.015, 0.010), "phi": (0.99, 0.97, 0.95), "sd": (0.0015, 0.0020, 0.0030), "tau": 2.0,
}


def dns_factors(n: int, seed: SeedLike, mu=DNS_DEFAULTS["mu"], phi=DNS_DEFAULTS["phi"], sd=DNS_DEFAULTS["sd"],
                start: Optional[Sequence[float]] = None) -> np.ndarray:
    """n x 3 monthly level / slope / curvature factors of the dynamic Nelson-Siegel model (independent AR(1));
    the first row is ``start`` (default ``mu``)."""
    if n < 1:
        raise ValueError("n must be >= 1")
    mu, phi, sd = (np.asarray(v, dtype=np.float64) for v in (mu, phi, sd))
    if np.any(np.abs(phi) >= 1):
        raise ValueError("|phi| must be < 1")
    rng = make_rng(seed)
    out = np.empty((n, 3))
    out[0] = mu if start is None else np.asarray(start, dtype=np.float64)
    eps = rng.standard_normal((n - 1, 3))
    for i in range(1, n):
        out[i] = mu + phi * (out[i - 1] - mu) + sd * eps[i - 1]
    return out


def dns_curve(factors: Sequence[float], tau: float = DNS_DEFAULTS["tau"]) -> NelsonSiegel:
    return NelsonSiegel(float(factors[0]), float(factors[1]), float(factors[2]), tau)
