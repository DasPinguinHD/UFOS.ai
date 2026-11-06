"""Predictive one-day loss distributions attached to a :class:`~risklab.models.base.Forecast`.

Each object provides a vectorised ``ppf(u)`` (the quantile function of the
loss, ``u`` in (0, 1)), which is all the Acerbi-Szekely Monte-Carlo test needs
to simulate losses under H0. The location-scale family also provides ``cdf``
so that the test can draw only the tail (exact, see
:func:`risklab.backtest.es_backtest.acerbi_szekely_z2`).
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
from scipy import special

#: Decimal places used to round ``n * alpha`` before ``ceil`` (removes binary noise:
#: ``1 / (1 - 0.99) = 100.00000000000009``).
ROUND_DIGITS = 9


def ceil_rounded(x: float) -> int:
    """``ceil(x)`` after rounding x to :data:`ROUND_DIGITS` decimals."""
    return int(np.ceil(round(float(x), ROUND_DIGITS)))


def order_statistic_index(n: int, alpha: float) -> int:
    """1-based rank ``k = ceil(n * alpha)`` of the empirical alpha-quantile (HS convention)."""
    return ceil_rounded(n * alpha)


class LocScale:
    """``loc + scale * Z`` with Z standard normal (``nu=None``) or a STANDARD Student-t with ``nu`` df.

    For a unit-variance t innovation pass ``scale = sigma * sqrt((nu - 2) / nu)``
    (see :func:`risklab.analytic.t_scale_from_sd`).
    """

    def __init__(self, loc: float, scale: float, nu: Optional[float] = None):
        self.loc = float(loc)
        self.scale = float(scale)
        self.nu = None if nu is None else float(nu)
        if not np.isfinite(self.loc) or not (np.isfinite(self.scale) and self.scale >= 0):
            raise ValueError("loc must be finite and scale finite and >= 0")
        if self.nu is not None and not self.nu > 0:
            raise ValueError("nu must be > 0")

    @property
    def family(self) -> Tuple[str, Optional[float]]:
        """("normal", None) or ("t", nu): days with the same family can be simulated together."""
        return ("normal", None) if self.nu is None else ("t", self.nu)

    def std_ppf(self, u):
        u = np.asarray(u, dtype=np.float64)
        return special.ndtri(u) if self.nu is None else special.stdtrit(self.nu, u)

    def std_cdf(self, z):
        z = np.asarray(z, dtype=np.float64)
        return special.ndtr(z) if self.nu is None else special.stdtr(self.nu, z)

    def ppf(self, u):
        return self.loc + self.scale * self.std_ppf(u)

    def cdf(self, x):
        x = np.asarray(x, dtype=np.float64)
        if self.scale == 0:
            return (x >= self.loc).astype(np.float64)
        return self.std_cdf((x - self.loc) / self.scale)

    def __repr__(self) -> str:
        return f"LocScale(loc={self.loc!r}, scale={self.scale!r}, nu={self.nu!r})"


class Empirical:
    """Empirical distribution of a sample, optionally shifted and scaled: ``loc + scale * X_emp``.

    ``ppf(u) = loc + scale * x_(k)`` with ``k = max(1, ceil(n * u))`` (the inverse
    empirical CDF; the same order-statistic convention as historical simulation).
    HS uses ``loc = 0, scale = 1`` on the window losses; filtered HS uses the
    standardised residuals with the day's ``mu`` and ``sigma``.
    """

    def __init__(self, sample, loc: float = 0.0, scale: float = 1.0):
        x = np.sort(np.asarray(sample, dtype=np.float64))
        if x.ndim != 1 or x.size == 0 or not np.all(np.isfinite(x)):
            raise ValueError("sample must be a non-empty finite 1-D array")
        x.setflags(write=False)
        self.sorted = x
        self.loc = float(loc)
        self.scale = float(scale)
        if not (np.isfinite(self.scale) and self.scale >= 0):
            raise ValueError("scale must be finite and >= 0")

    def ppf(self, u):
        u = np.asarray(u, dtype=np.float64)
        n = self.sorted.shape[0]
        k = np.clip(np.ceil(np.round(n * u, 9)).astype(np.int64), 1, n)
        return self.loc + self.scale * self.sorted[k - 1]

    def quantile(self, alpha: float) -> float:
        """The HS order statistic ``x_(ceil(n alpha))`` (scaled), as :class:`HistoricalSimulation`."""
        return self.loc + self.scale * float(self.sorted[order_statistic_index(self.sorted.shape[0], alpha) - 1])

    def __repr__(self) -> str:
        return f"Empirical(n={self.sorted.shape[0]}, loc={self.loc!r}, scale={self.scale!r})"
