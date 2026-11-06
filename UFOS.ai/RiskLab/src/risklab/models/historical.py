"""Historical simulation: empirical VaR / ES of the window's losses."""
from __future__ import annotations

from typing import Sequence

import numpy as np

from risklab.conventions import as_loss_array, check_alphas
from risklab.dists import Empirical, ceil_rounded, order_statistic_index  # noqa: F401 (re-exported)
from risklab.models.base import Forecast


def min_window(alpha: float) -> int:
    """Smallest window for HS at level alpha: ``ceil(1 / (1 - alpha))`` (rounded to 9 decimals first)."""
    return ceil_rounded(1.0 / (1.0 - alpha))


def hs_var_es(sorted_x: np.ndarray, alpha: float):
    """(VaR, ES) of an ascending sample with the HS convention; ValueError if the sample is too short."""
    n = sorted_x.shape[0]
    need = min_window(alpha)
    if n < need:
        raise ValueError(f"historical simulation at alpha={alpha} needs at least {need} observations "
                         f"(ceil(1 / (1 - alpha))), got {n}")
    v = float(sorted_x[order_statistic_index(n, alpha) - 1])
    return v, float(np.mean(sorted_x[sorted_x >= v]))


def exceedance_probability(n: int, alpha: float) -> float:
    """True exceedance probability of the HS VaR for iid continuous losses: ``(n - k + 1) / (n + 1)``.

    The rank of the next loss among n + 1 iid draws is uniform, so it exceeds
    the k-th order statistic of the first n with probability (n - k + 1) / (n + 1).
    """
    k = order_statistic_index(n, alpha)
    return (n - k + 1) / (n + 1)


class HistoricalSimulation:
    """Historical-simulation VaR / ES on the fitted window of past losses.

    Convention (one, fixed, no interpolation):
    * Sort the n window losses ascending: ``L_(1) <= ... <= L_(n)``.
    * ``VaR_alpha = L_(k)`` with ``k = ceil(n * alpha)`` (the empirical
      alpha-quantile, the inverse of the empirical CDF; ``n * alpha`` is rounded
      to 9 decimals before ``ceil`` to remove floating-point noise).
    * ``ES_alpha`` = the mean of all window losses ``>= VaR_alpha`` (tail mean).
      Ties: every loss equal to the VaR is in the tail, so the tail can hold more
      than ``n - k + 1`` losses when values repeat.
    * The window must hold at least ``ceil(1 / (1 - alpha))`` losses. Then
      ``n * (1 - alpha) >= 1``, so ``k <= n - 1``: the VaR is never the sample
      maximum and the tail holds at least two losses. Shorter windows raise
      ValueError. Never extrapolates beyond the largest observed loss.

    Finite-sample bias: for iid continuous losses, the next loss exceeds
    ``L_(k)`` with probability ``(n - k + 1) / (n + 1)``, not ``1 - alpha``
    (see :func:`exceedance_probability`). At n = 250, alpha = 0.99 (k = 248)
    this is 3 / 251 = 1.20 % instead of 1 %, so HS on iid data is not a clean
    size benchmark for the coverage tests.
    """

    name = "historical_simulation"

    def __init__(self, method: str = "empirical"):
        if method != "empirical":
            raise ValueError("HistoricalSimulation supports only method='empirical'")
        self.method = method
        self._sorted = None

    def fit(self, losses_past: np.ndarray) -> None:
        self._sorted = np.sort(as_loss_array(losses_past, "losses_past"))

    def forecast(self, alphas: Sequence[float]) -> Forecast:
        if self._sorted is None:
            raise RuntimeError("fit() must be called before forecast()")
        var, es = {}, {}
        for a in check_alphas(alphas):
            var[a], es[a] = hs_var_es(self._sorted, a)
        return Forecast(var=var, es=es, dist=Empirical(self._sorted))
