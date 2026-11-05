"""True conditional VaR / ES of a synthetic series (for estimator error and test power).

Every generator in :mod:`risklab.data.synthetic` has the form
``L_t = mu_t + sigma_t * Z_t`` with ``Z_t`` a unit-variance innovation of the
family ``series.dist``, independent of the past. So the true one-day VaR / ES
of day t, conditional on the information up to t-1, follow in closed form from
the stored per-day parameters:

* ``dist == "normal"`` (iid normal, GARCH with normal innovations, regime
  switching with ``sigma_true = sigmas[regime]``): normal VaR / ES with
  ``mu_true[t]`` and ``sigma_true[t]``.
* ``dist == "t"`` (iid scaled t, GARCH with t innovations): unit-variance t with
  ``nu = dist_params["nu"]``, scaled by ``sigma_true[t]``.

Note for regime switching: the value is conditional on the realised state of
day t (``sigma_true[t]``), i.e. on more than the past losses. It is the
"oracle" benchmark, not a forecast any model could reach from past data alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Sequence

import numpy as np

from risklab import analytic
from risklab.conventions import check_alphas
from risklab.data.synthetic import SyntheticSeries


@dataclass(frozen=True)
class TrueRisk:
    """Per-day true VaR / ES: ``{alpha: float64 array of len(series)}`` (read-only arrays)."""

    var: Dict[float, np.ndarray]
    es: Dict[float, np.ndarray]

    def __post_init__(self):
        for d in (self.var, self.es):
            for arr in d.values():
                arr.setflags(write=False)


def true_var_es(series: SyntheticSeries, alphas: Sequence[float]) -> TrueRisk:
    """True conditional VaR and ES for every day of ``series`` and every alpha."""
    alphas = check_alphas(alphas)
    mu, sigma = series.mu_true, series.sigma_true
    var, es = {}, {}
    for a in alphas:
        if series.dist == "normal":
            var[a] = np.array(analytic.var_normal(a, mu, sigma), dtype=np.float64)
            es[a] = np.array(analytic.es_normal(a, mu, sigma), dtype=np.float64)
        elif series.dist == "t":
            nu = series.dist_params["nu"]
            var[a] = np.array(analytic.var_t_std(a, nu, mu, sigma), dtype=np.float64)
            es[a] = np.array(analytic.es_t_std(a, nu, mu, sigma), dtype=np.float64)
        else:
            raise ValueError(f"unknown distribution {series.dist!r}")
    return TrueRisk(var=var, es=es)
