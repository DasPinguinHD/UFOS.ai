"""Multi-day (h-day) VaR / ES: square-root-of-time, GARCH Monte Carlo, stationary block bootstrap.

All three forecast the SUM of the next h daily losses. They implement the
:class:`~risklab.engine.HorizonForecaster` protocol for
:func:`risklab.engine.walk_forward_h` (non-overlapping h-day blocks).

* :class:`SqrtTimeForecaster` - ``VaR_h ~ sqrt(h) * VaR_1`` (and the same for ES).
  An APPROXIMATION: exact only for iid normal losses with zero mean; it ignores
  volatility dynamics, fat tails of sums and the mean.
* :class:`GarchMCForecaster` - fit GARCH(1,1)-t, simulate N paths of h days
  starting from sigma2_{t+1} with the variance recursion, sum, take the
  empirical quantile (HS order-statistic convention) and tail mean.
* :class:`BootstrapForecaster` - stationary block bootstrap of the window
  (:mod:`risklab.bootstrap`), h-day sums, empirical quantile and tail mean.

Monte-Carlo error of a simulated VaR (:func:`empirical_var_es`): a
distribution-free 95 % interval from binomial order statistics: ranks
``binom.ppf(0.025, N, alpha)`` and ``binom.ppf(0.975, N, alpha) + 1`` of the
sorted sums; ``var_se = (upper - lower) / (2 * 1.96)``. ES error: the standard
error of the tail mean, ``sd(tail) / sqrt(n_tail)`` (it ignores the VaR's own
uncertainty).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple, Union

import numpy as np
from scipy import stats

from risklab.bootstrap import bootstrap_h_sums, resolve_block_len
from risklab.conventions import SeedLike, as_loss_array, check_alphas, make_rng
from risklab.dists import order_statistic_index
from risklab.models.base import Forecast, RiskModel
from risklab.models.garch import GarchParams, GarchTModel


def sqrt_time(value, h: int):
    """``sqrt(h) * value`` - the square-root-of-time APPROXIMATION of an h-day VaR / ES."""
    if isinstance(h, bool) or not isinstance(h, (int, np.integer)) or h < 1:
        raise ValueError("h must be an int >= 1")
    return np.sqrt(h) * np.asarray(value, dtype=np.float64)


@dataclass(frozen=True)
class HorizonEstimate:
    """Simulated h-day VaR / ES with Monte-Carlo errors (see the module docstring)."""

    var: float
    es: float
    var_se: float
    var_ci95: Tuple[float, float]
    es_se: float
    n: int


def empirical_var_es(sums, alpha: float) -> HorizonEstimate:
    """VaR (order statistic ``ceil(N alpha)``), ES (mean of sums >= VaR) and their MC errors."""
    x = np.sort(as_loss_array(sums, "sums"))
    n = x.shape[0]
    k = order_statistic_index(n, alpha)
    var = float(x[k - 1])
    tail = x[x >= var]
    lo = int(stats.binom.ppf(0.025, n, alpha))
    hi = int(stats.binom.ppf(0.975, n, alpha)) + 1
    lo, hi = max(lo, 1), min(hi, n)
    ci = (float(x[lo - 1]), float(x[hi - 1]))
    es_se = float(np.std(tail, ddof=1) / np.sqrt(tail.size)) if tail.size > 1 else float("nan")
    return HorizonEstimate(var, float(tail.mean()), (ci[1] - ci[0]) / (2 * 1.96), ci, es_se, n)


def simulate_garch_sums(params: GarchParams, sigma2_next: float, h: int, n_paths: int,
                        rng: np.random.Generator) -> np.ndarray:
    """h-day loss sums of a GARCH(1,1) started at ``sigma2_next`` (variance of the first day).

    Day j: ``L_j = mu + sqrt(sigma2_j) z_j``; ``sigma2_{j+1} = omega + alpha (L_j - mu)**2 + beta sigma2_j``;
    z iid N(0, 1) or unit-variance t(nu). Draws: one (n_paths, h) innovation matrix.
    """
    if params.nu is None:
        z = rng.standard_normal((n_paths, h))
    else:
        z = rng.standard_t(params.nu, size=(n_paths, h)) * np.sqrt((params.nu - 2.0) / params.nu)
    s2 = np.full(n_paths, float(sigma2_next))
    total = np.zeros(n_paths)
    for j in range(h):
        e = np.sqrt(s2) * z[:, j]
        total += params.mu + e
        s2 = params.omega + params.alpha * e * e + params.beta * s2
    return total


def _forecast_from_sums(sums: np.ndarray, alphas: Sequence[float]) -> Forecast:
    var, es = {}, {}
    for a in check_alphas(alphas):
        est = empirical_var_es(sums, a)
        var[a], es[a] = est.var, est.es
    return Forecast(var=var, es=es)


class SqrtTimeForecaster:
    """``sqrt(h)`` times a one-day model's VaR / ES (an approximation; see the module docstring)."""

    def __init__(self, model_factory: Callable[[], RiskModel]):
        self.model_factory = model_factory
        self.model = None
        self.name = None

    def fit(self, losses_past: np.ndarray) -> None:
        self.model = self.model_factory()
        self.name = "sqrt_time_" + getattr(self.model, "name", type(self.model).__name__)
        self.model.fit(losses_past)

    def forecast_h(self, alphas: Sequence[float], h: int) -> Forecast:
        fc = self.model.forecast(alphas)
        return Forecast(var={a: float(sqrt_time(v, h)) for a, v in fc.var.items()},
                        es=None if fc.es is None else {a: float(sqrt_time(v, h)) for a, v in fc.es.items()})


class GarchMCForecaster:
    """h-day VaR / ES by Monte Carlo from a GARCH(1,1)-t fitted to the window.

    ``seed`` fixes the innovations; the same seed at every origin gives common
    random numbers across origins.
    """

    name = "garch_mc"

    def __init__(self, n_paths: int = 10_000, seed: SeedLike = 0, dist: str = "t"):
        if isinstance(n_paths, bool) or not isinstance(n_paths, int) or n_paths < 100:
            raise ValueError("n_paths must be an int >= 100")
        self.n_paths, self.seed, self.dist = n_paths, seed, dist
        self.model: Optional[GarchTModel] = None

    def fit(self, losses_past: np.ndarray) -> None:
        self.model = GarchTModel(dist=self.dist)
        self.model.fit(losses_past)

    def simulate(self, h: int) -> np.ndarray:
        return simulate_garch_sums(self.model.params, self.model.sigma2_next, int(h), self.n_paths,
                                   make_rng(self.seed))

    def forecast_h(self, alphas: Sequence[float], h: int) -> Forecast:
        return _forecast_from_sums(self.simulate(h), alphas)


class BootstrapForecaster:
    """h-day VaR / ES from stationary-bootstrap paths of the window (``block_len`` number or "auto")."""

    name = "block_bootstrap"

    def __init__(self, block_len: Union[float, str] = "auto", n_paths: int = 10_000, seed: SeedLike = 0):
        if isinstance(n_paths, bool) or not isinstance(n_paths, int) or n_paths < 100:
            raise ValueError("n_paths must be an int >= 100")
        if not isinstance(block_len, str):
            resolve_block_len(np.zeros(1), block_len)  # validates the number
        self.block_len, self.n_paths, self.seed = block_len, n_paths, seed
        self.window: Optional[np.ndarray] = None
        self.block_len_used: Optional[float] = None

    def fit(self, losses_past: np.ndarray) -> None:
        self.window = as_loss_array(losses_past, "losses_past")
        self.block_len_used = resolve_block_len(self.window, self.block_len)

    def simulate(self, h: int) -> np.ndarray:
        return bootstrap_h_sums(self.window, int(h), self.block_len_used, self.n_paths, make_rng(self.seed))

    def forecast_h(self, alphas: Sequence[float], h: int) -> Forecast:
        return _forecast_from_sums(self.simulate(h), alphas)
