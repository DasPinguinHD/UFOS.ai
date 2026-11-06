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
from typing import Dict, Optional, Sequence

import numpy as np

from risklab import analytic
from risklab.conventions import check_alphas, make_rng
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


@dataclass(frozen=True)
class TrueRiskH:
    """True h-day VaR / ES per origin: ``{alpha: array over origins}``; ``var_se`` = Monte-Carlo error (0 if exact)."""

    origins: np.ndarray
    h: int
    var: Dict[float, np.ndarray]
    es: Dict[float, np.ndarray]
    var_se: Dict[float, np.ndarray]
    method: str


def true_var_es_h(series: SyntheticSeries, alphas: Sequence[float], h: int, n_sims: int, seed,
                  origins: Optional[Sequence[int]] = None) -> TrueRiskH:
    """True VaR / ES of ``L_t + ... + L_{t+h-1}`` given the information before t, per origin t.

    * iid normal: closed form ``h mu + sqrt(h) sigma Phi^-1(alpha)`` (``var_se`` = 0).
    * iid scaled t: Monte Carlo (the sum of h unit-variance t's has no closed
      form) with ``n_sims`` paths; ``var_se`` from the binomial order-statistic
      interval (see :func:`risklab.horizon.empirical_var_es`).
    * GARCH: Monte Carlo from the TRUE parameters, started at the true
      conditional variance of day t (``sigma_true[t]**2``); the same innovations
      for every origin (common random numbers). ``origins`` is required.
    * regime switching: not supported in v0.3 (ValueError).

    ``origins`` default: day 0 for the iid generators.
    """
    from risklab.horizon import empirical_var_es, simulate_garch_sums  # local: avoids an import cycle
    from risklab.models.garch import GarchParams

    alphas = check_alphas(alphas)
    if isinstance(h, bool) or not isinstance(h, (int, np.integer)) or h < 1:
        raise ValueError("h must be an int >= 1")
    gen = series.meta.get("generator")
    if origins is None:
        if gen == "garch11":
            raise ValueError("origins are required for GARCH series")
        origins = [0]
    org = np.asarray(origins, dtype=np.int64)
    if org.ndim != 1 or org.size == 0 or np.any(org < 0) or np.any(org >= len(series)):
        raise ValueError("origins must be valid day indices")
    var = {a: np.empty(org.size) for a in alphas}
    es = {a: np.empty(org.size) for a in alphas}
    se = {a: np.zeros(org.size) for a in alphas}
    if gen == "iid_normal":
        mu, sigma = series.meta["mu"], series.meta["sigma"]
        for a in alphas:
            var[a][:] = h * mu + np.sqrt(h) * sigma * float(analytic.var_normal(a))
            es[a][:] = h * mu + np.sqrt(h) * sigma * float(analytic.es_normal(a))
        method = "closed form"
    elif gen == "iid_student_t":
        mu, sigma, nu = series.meta["mu"], series.meta["sigma"], series.meta["nu"]
        rng = make_rng(seed)
        z = rng.standard_t(nu, size=(int(n_sims), h)) * np.sqrt((nu - 2.0) / nu)
        sums = h * mu + sigma * z.sum(axis=1)
        for a in alphas:
            est = empirical_var_es(sums, a)
            var[a][:], es[a][:], se[a][:] = est.var, est.es, est.var_se
        method = f"monte carlo ({int(n_sims)} paths)"
    elif gen == "garch11":
        m = series.meta
        params = GarchParams(0.0, m["omega"], m["alpha1"], m["beta1"],
                             m["nu"] if m["innovations"] == "t" else None)
        for i, t in enumerate(org):
            sums = simulate_garch_sums(params, float(series.sigma_true[t]) ** 2, h, int(n_sims), make_rng(seed))
            for a in alphas:
                est = empirical_var_es(sums, a)
                var[a][i], es[a][i], se[a][i] = est.var, est.es, est.var_se
        method = f"monte carlo ({int(n_sims)} paths, true parameters, common random numbers)"
    else:
        raise ValueError(f"true h-day risk is not available for generator {gen!r}")
    return TrueRiskH(org, int(h), var, es, se, method)
