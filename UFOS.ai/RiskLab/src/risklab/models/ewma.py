"""EWMA (RiskMetrics) volatility model with zero mean."""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from risklab import analytic
from risklab.conventions import as_loss_array, check_alphas
from risklab.dists import LocScale
from risklab.models.base import Forecast
from risklab.models.garch import standard_factors


class EWMAModel:
    """RiskMetrics EWMA: ``sigma2_{t+1} = lam * sigma2_t + (1 - lam) * L_t**2``, zero mean.

    Initialisation (``fit``): ``sigma2`` at day ``init`` of the window is the
    zero-mean (uncentred) variance ``mean(L**2)`` of the first ``init`` losses,
    consistent with the zero-mean recursion; the recursion then runs over losses
    ``init .. n-1`` of the window, and
    the result is the variance forecast for the day after the window. With
    ``n == init`` the forecast is that initial value. ``update(loss)`` applies
    one recursion step with the new loss (parameters are fixed; nothing is estimated).

    ``dist="normal"``: normal VaR / ES with ``sigma = sqrt(sigma2)``.
    ``dist="t"``: a Student-t scaled to unit variance with FIXED ``nu`` (> 2),
    which must be given (RiskMetrics-t; nu is not estimated here).
    """

    def __init__(self, lam: float = 0.94, dist: str = "normal", nu: Optional[float] = None, init: int = 30):
        lam = float(lam)
        if not 0.0 < lam < 1.0:
            raise ValueError("lam must lie strictly inside (0, 1)")
        if dist not in ("normal", "t"):
            raise ValueError("dist must be 'normal' or 't'")
        if dist == "t":
            if nu is None or not float(nu) > 2.0:
                raise ValueError("dist='t' needs a fixed nu > 2")
            nu = float(nu)
        elif nu is not None:
            raise ValueError("nu is only used with dist='t'")
        if isinstance(init, bool) or not isinstance(init, int) or init < 1:
            raise ValueError("init must be an int >= 1")
        self.lam, self.dist, self.nu, self.init = lam, dist, nu, init
        self.name = "ewma_t" if dist == "t" else "ewma"
        self.sigma2_next: Optional[float] = None
        self._factors = {}

    def fit(self, losses_past: np.ndarray) -> None:
        x = as_loss_array(losses_past, "losses_past")
        if x.shape[0] < self.init:
            raise ValueError(f"EWMA needs at least init={self.init} losses")
        s2 = float(np.mean(x[:self.init] ** 2))
        for v in x[self.init:]:
            s2 = self.lam * s2 + (1.0 - self.lam) * float(v) ** 2
        self.sigma2_next = s2

    def update(self, loss: float) -> None:
        if self.sigma2_next is None:
            raise RuntimeError("fit() must be called before update()")
        self.sigma2_next = self.lam * self.sigma2_next + (1.0 - self.lam) * float(loss) ** 2

    def forecast(self, alphas: Sequence[float]) -> Forecast:
        if self.sigma2_next is None:
            raise RuntimeError("fit() must be called before forecast()")
        sigma = float(np.sqrt(self.sigma2_next))
        var, es = {}, {}
        for a in check_alphas(alphas):
            kv, ke = self._factors.get(a) or self._factors.setdefault(a, standard_factors(a, self.nu))
            var[a], es[a] = sigma * kv, sigma * ke
        dist = (LocScale(0.0, sigma) if self.dist == "normal"
                else LocScale(0.0, float(analytic.t_scale_from_sd(sigma, self.nu)), self.nu))
        return Forecast(var=var, es=es, dist=dist)
