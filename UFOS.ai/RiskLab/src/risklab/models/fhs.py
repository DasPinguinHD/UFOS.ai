"""Filtered historical simulation (Barone-Adesi, Giannopoulos and Vosper 1999)."""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from risklab.conventions import as_loss_array, check_alphas
from risklab.dists import Empirical
from risklab.models.base import Forecast
from risklab.models.garch import GarchParams, fit_garch11, variance_path
from risklab.models.historical import hs_var_es


class FilteredHS:
    """GARCH(1,1)-filtered historical simulation.

    ``fit(window)``:
      1. fit GARCH(1,1) (``garch="normal"``: Gaussian QMLE, the default; ``"t"``:
         t-MLE) with :func:`~risklab.models.garch.fit_garch11`;
      2. run the variance recursion over the window (start: sample variance) and
         form the standardised residuals ``z_t = (L_t - mu) / sigma_t``;
      3. keep z and the variance forecast ``sigma2_{n}`` for the next day.

    ``VaR = mu + sigma_next * q_z`` with ``q_z`` the empirical alpha-quantile of z
    by the HS order-statistic convention (``z_(ceil(n alpha))``, no interpolation),
    and ``ES = mu + sigma_next * mean(z >= q_z)``. The residual set is fixed
    between refits; ``update(loss)`` rolls only ``sigma2`` forward.
    ``Forecast.dist`` is the empirical distribution of z, shifted by mu and scaled by sigma_next.
    """

    def __init__(self, garch: str = "normal", refit_every: int = 20):
        if garch not in ("normal", "t"):
            raise ValueError("garch must be 'normal' or 't'")
        if isinstance(refit_every, bool) or not isinstance(refit_every, int) or refit_every < 1:
            raise ValueError("refit_every must be an int >= 1")
        self.garch, self.refit_every = garch, refit_every
        self.name = "fhs" if garch == "normal" else "fhs_t"
        self.params: Optional[GarchParams] = None
        self.z: Optional[np.ndarray] = None
        self.sigma2_next: Optional[float] = None

    def fit(self, losses_past: np.ndarray) -> None:
        x = as_loss_array(losses_past, "losses_past")
        self.params = fit_garch11(x, self.garch)
        path = variance_path(self.params, x, float(np.var(x)))
        z = (x - self.params.mu) / np.sqrt(path[:-1])
        self.z = np.sort(z)
        self.z.setflags(write=False)
        self.sigma2_next = float(path[-1])

    def update(self, loss: float) -> None:
        if self.params is None:
            raise RuntimeError("fit() must be called before update()")
        p = self.params
        self.sigma2_next = p.omega + p.alpha * (float(loss) - p.mu) ** 2 + p.beta * self.sigma2_next

    def forecast(self, alphas: Sequence[float]) -> Forecast:
        if self.params is None:
            raise RuntimeError("fit() must be called before forecast()")
        mu, sigma = self.params.mu, float(np.sqrt(self.sigma2_next))
        var, es = {}, {}
        for a in check_alphas(alphas):
            qz, ez = hs_var_es(self.z, a)
            var[a], es[a] = mu + sigma * qz, mu + sigma * ez
        return Forecast(var=var, es=es, dist=Empirical(self.z, loc=mu, scale=sigma))
