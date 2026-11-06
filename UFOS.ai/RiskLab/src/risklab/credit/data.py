"""Synthetic loan portfolio with a known true PD model and correlated defaults.

Each period t = 0..T-1 has a cross-section of ``n`` borrowers. Features of borrower i in period t
(synthetic, stated in :data:`FEATURES`):

* ``leverage`` ~ U(0.1, 0.9) (debt / assets);
* ``log_icr`` = log interest coverage ~ N(1.2, 0.6);
* ``log_size`` = log total assets (EUR m) ~ N(3.0, 1.0);
* ``sector`` in {0, 1, 2} (equal shares; two dummies, sector 0 is the base);
* the observable macro variable ``macro_t`` (e.g. GDP growth in standard units), an AR(1)
  with coefficient 0.7, common to all borrowers of the period.

True PD (one year): ``logit p = b0 + b . x`` with :data:`TRUE_COEF`.

Defaults follow a **one-factor Gaussian copula** (Vasicek): borrower i defaults in period t if
``sqrt(rho) Z_t + sqrt(1 - rho) e_it < Phi^-1(p_it)``, with the latent systematic factor
``Z_t ~ N(0, 1)`` (independent of ``macro_t`` and unobservable) and idiosyncratic ``e_it``.
Unconditionally the default probability is exactly ``p_it``; given Z_t the defaults of a period
are correlated with asset correlation ``rho`` (default 0.12, the lower end of the Basel
corporate correlation range; a modelling choice, not a calibration).

Optional ``macro_path`` overrides the AR(1) macro variable (used for a downturn scenario).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence

import numpy as np
from scipy import stats

from risklab.conventions import SeedLike, make_rng

FEATURES = ("leverage", "log_icr", "log_size", "sector_1", "sector_2", "macro")
TRUE_COEF: Dict[str, float] = {"const": -4.0, "leverage": 2.5, "log_icr": -0.9, "log_size": -0.25,
                               "sector_1": 0.4, "sector_2": -0.3, "macro": -0.45}
RHO = 0.12
MACRO_PHI = 0.7


@dataclass(frozen=True)
class Panel:
    X: np.ndarray            # (N, k) features in FEATURES order (no constant)
    y: np.ndarray            # (N,) default indicator 0/1
    period: np.ndarray       # (N,) period index
    pd_true: np.ndarray      # (N,) true one-year PD
    macro: np.ndarray        # (T,) macro variable per period
    z: np.ndarray            # (T,) latent systematic factor per period
    rho: float

    def select(self, mask) -> "Panel":
        m = np.asarray(mask, dtype=bool)
        return Panel(self.X[m], self.y[m], self.period[m], self.pd_true[m], self.macro, self.z, self.rho)


def true_pd(X: np.ndarray, coef: Dict[str, float] = TRUE_COEF) -> np.ndarray:
    b = np.array([coef[f] for f in FEATURES])
    return 1.0 / (1.0 + np.exp(-(coef["const"] + X @ b)))


def macro_ar1(T: int, rng: np.random.Generator, phi: float = MACRO_PHI) -> np.ndarray:
    m = np.empty(T)
    m[0] = rng.standard_normal()
    for t in range(1, T):
        m[t] = phi * m[t - 1] + np.sqrt(1 - phi * phi) * rng.standard_normal()
    return m


def generate_panel(n: int, T: int, seed: SeedLike, rho: float = RHO, coef: Dict[str, float] = TRUE_COEF,
                   macro_path: Optional[Sequence[float]] = None) -> Panel:
    """``T`` periods of ``n`` borrowers (module docstring)."""
    if n < 1 or T < 1:
        raise ValueError("n and T must be >= 1")
    if not 0.0 <= rho < 1.0:
        raise ValueError("rho must be in [0, 1)")
    rng = make_rng(seed)
    macro = macro_ar1(T, rng) if macro_path is None else np.asarray(macro_path, dtype=np.float64)
    if macro.shape != (T,):
        raise ValueError("macro_path must have length T")
    z = rng.standard_normal(T)
    N = n * T
    period = np.repeat(np.arange(T), n)
    sector = rng.integers(0, 3, N)
    X = np.column_stack([rng.uniform(0.1, 0.9, N), rng.normal(1.2, 0.6, N), rng.normal(3.0, 1.0, N),
                         (sector == 1).astype(float), (sector == 2).astype(float), macro[period]])
    p = true_pd(X, coef)
    eps = rng.standard_normal(N)
    latent = np.sqrt(rho) * z[period] + np.sqrt(1.0 - rho) * eps
    y = (latent < stats.norm.ppf(p)).astype(np.int8)
    return Panel(X, y, period, p, macro, z, rho)


def conditional_pd(pd, rho: float, z) -> np.ndarray:
    """Default probability given the systematic factor: ``Phi((Phi^-1(p) - sqrt(rho) z) / sqrt(1 - rho))``."""
    return stats.norm.cdf((stats.norm.ppf(pd) - np.sqrt(rho) * np.asarray(z)) / np.sqrt(1.0 - rho))
