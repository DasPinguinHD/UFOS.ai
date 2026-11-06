"""GARCH(1,1) with normal or unit-variance Student-t innovations, fitted by maximum likelihood.

Model for the loss: ``L_t = mu + e_t``, ``e_t = sigma_t * z_t``,
``sigma2_t = omega + alpha * e_{t-1}**2 + beta * sigma2_{t-1}``, with z_t iid
N(0, 1) or a Student-t scaled to unit variance (nu > 2).

Estimation (:func:`fit_garch11`):

* The data are standardised first: ``x = (L - mean) / sd`` (sample mean, sd with
  ddof = 0); the fit runs on x and the parameters are mapped back
  (``mu_L = mean + sd * mu``, ``omega_L = sd**2 * omega``; alpha, beta, nu unchanged).
* The recursion starts at the sample variance: ``sigma2_0 = 1`` on the
  standardised data (= the sample variance, ddof = 0, in loss units).
* Reparametrisation (keeps every constraint satisfied, so a violation is
  impossible): ``omega = exp(w)``; persistence ``p = alpha + beta`` and share
  ``g = alpha / p``, so ``alpha = p * g``, ``beta = p * (1 - g)``. L-BFGS-B box:
  ``mu`` in [-5, 5], ``w`` in [-20, 3] (standardised units), ``p`` in [0, 0.999]
  (so alpha + beta <= 0.999 < 1), ``g`` in [0, 1] (alpha, beta >= 0), and for t
  innovations ``nu`` in [2.05, 200].
* The gradient is analytic (the variance derivatives follow the same linear
  recursion and are computed with ``scipy.signal.lfilter``).
* Three deterministic starts, ``(p, g, nu)`` = (0.95, 0.10, 8), (0.90, 0.20, 5),
  (0.98, 0.05, 20), with ``omega = 1 - p`` (unconditional variance 1) and mu = 0.
  The start with the lowest negative log-likelihood wins; if its optimiser run
  did not report success, :class:`~risklab.models.base.FitError` is raised.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
from scipy import optimize, signal, special

from risklab import analytic
from risklab.conventions import as_loss_array, check_alphas
from risklab.dists import LocScale
from risklab.models.base import FitError, Forecast

NU_BOUNDS = (2.05, 200.0)
MU_BOUNDS = (-5.0, 5.0)
LOG_OMEGA_BOUNDS = (-20.0, 3.0)
PERSISTENCE_BOUNDS = (0.0, 0.999)
SHARE_BOUNDS = (0.0, 1.0)
STARTS = ((0.95, 0.10, 8.0), (0.90, 0.20, 5.0), (0.98, 0.05, 20.0))
MIN_OBS = 50


@dataclass(frozen=True)
class GarchParams:
    """Fitted GARCH(1,1) parameters in loss units. ``nu`` is None for normal innovations."""

    mu: float
    omega: float
    alpha: float
    beta: float
    nu: Optional[float]

    @property
    def persistence(self) -> float:
        return self.alpha + self.beta

    @property
    def unconditional_variance(self) -> float:
        return self.omega / (1.0 - self.alpha - self.beta)


def variance_path(params: GarchParams, losses: np.ndarray, sigma2_0: float) -> np.ndarray:
    """``sigma2_t`` for t = 0 .. n (n + 1 values; the last one is the forecast for day n)."""
    e2 = (np.asarray(losses, dtype=np.float64) - params.mu) ** 2
    x = np.concatenate([[0.0], params.omega + params.alpha * e2])
    # with x_0 = 0 and initial state sigma2_0: y_0 = sigma2_0, y_t = x_t + beta * y_{t-1}
    y, _ = signal.lfilter([1.0], [1.0, -params.beta], x, zi=np.array([sigma2_0]))
    return y


def _filter(beta: float, inp: np.ndarray, y0: float) -> np.ndarray:
    """y_0 = y0, y_t = inp_t + beta * y_{t-1} for t >= 1 (inp_0 is ignored)."""
    x = inp.copy()
    x[0] = 0.0
    y, _ = signal.lfilter([1.0], [1.0, -beta], x, zi=np.array([y0]))
    return y


def garch_neg_loglik(theta, x: np.ndarray, dist: str):
    """Negative log-likelihood and gradient on standardised data ``x``.

    ``theta = (mu, w, p, g[, nu])`` (see the module docstring); ``sigma2_0 = 1``.
    """
    if dist == "t":
        mu, w, p, g, nu = theta
    else:
        mu, w, p, g = theta
        nu = None
    omega = np.exp(w)
    alpha, beta = p * g, p * (1.0 - g)
    e = x - mu
    e2 = e * e
    n = x.shape[0]
    lag_e2 = np.concatenate([[0.0], e2[:-1]])
    lag_e = np.concatenate([[0.0], e[:-1]])
    h = _filter(beta, omega + alpha * lag_e2, 1.0)
    if np.any(h <= 0) or not np.all(np.isfinite(h)):
        return np.inf, np.zeros(len(theta))
    if nu is None:
        ll = -0.5 * np.sum(np.log(2 * np.pi) + np.log(h) + e2 / h)
        dl_dh = -0.5 / h + 0.5 * e2 / (h * h)
        dl_de = -e / h
    else:
        q = e2 / ((nu - 2.0) * h)
        w_ = 1.0 + q
        log_w = np.log1p(q)
        const = special.gammaln((nu + 1.0) / 2.0) - special.gammaln(nu / 2.0) - 0.5 * np.log(np.pi * (nu - 2.0))
        ll = n * const - 0.5 * np.sum(np.log(h)) - 0.5 * (nu + 1.0) * np.sum(log_w)
        dl_dh = -0.5 / h + 0.5 * (nu + 1.0) * q / (w_ * h)
        dl_de = -(nu + 1.0) * e / ((nu - 2.0) * h * w_)
        d_nu = (n * (0.5 * special.digamma((nu + 1.0) / 2.0) - 0.5 * special.digamma(nu / 2.0) - 0.5 / (nu - 2.0))
                - 0.5 * np.sum(log_w) + 0.5 * (nu + 1.0) * np.sum(q / (w_ * (nu - 2.0))))
    lag_h = np.concatenate([[0.0], h[:-1]])
    # d sigma2_t / d(omega, alpha, beta, mu): the same recursion with coefficient beta, D_0 = 0
    inp = np.vstack([np.ones(n), lag_e2, lag_h, -2.0 * alpha * lag_e])
    inp[:, 0] = 0.0
    d, _ = signal.lfilter([1.0], [1.0, -beta], inp, axis=1, zi=np.zeros((4, 1)))
    g_omega, g_alpha, g_beta, g_mu_h = d @ dl_dh
    g_mu = g_mu_h - np.sum(dl_de)
    grad = [g_mu, g_omega * omega, g * g_alpha + (1.0 - g) * g_beta, p * g_alpha - p * g_beta]
    if nu is not None:
        grad.append(d_nu)
    if not np.isfinite(ll):
        return np.inf, np.zeros(len(theta))
    return float(-ll), -np.array(grad, dtype=np.float64)


def fit_garch11(losses, dist: str = "t") -> GarchParams:
    """Maximum-likelihood GARCH(1,1) fit (``dist="normal"``: Gaussian QMLE). See the module docstring.

    Raises:
        ValueError: fewer than 50 observations, zero variance, or an unknown ``dist``.
        FitError: the best optimiser run did not report convergence.
    """
    if dist not in ("normal", "t"):
        raise ValueError("dist must be 'normal' or 't'")
    data = as_loss_array(losses, "losses_past")
    if data.shape[0] < MIN_OBS:
        raise ValueError(f"GARCH(1,1) needs at least {MIN_OBS} observations")
    m, sd = float(np.mean(data)), float(np.std(data))
    if not sd > 0:
        raise ValueError("the window has zero variance; GARCH cannot be fitted")
    x = (data - m) / sd
    bounds = [MU_BOUNDS, LOG_OMEGA_BOUNDS, PERSISTENCE_BOUNDS, SHARE_BOUNDS]
    if dist == "t":
        bounds.append(NU_BOUNDS)
    best = None
    for p0, g0, nu0 in STARTS:
        theta0 = [0.0, np.log(1.0 - p0), p0, g0] + ([nu0] if dist == "t" else [])
        res = optimize.minimize(garch_neg_loglik, theta0, args=(x, dist), jac=True, method="L-BFGS-B",
                                bounds=bounds)
        if np.isfinite(res.fun) and (best is None or res.fun < best.fun):
            best = res
    if best is None:
        raise FitError("GARCH(1,1) fit failed: no finite likelihood at any start")
    if not best.success:
        raise FitError(f"GARCH(1,1) fit did not converge: {best.message}")
    th = best.x
    mu, w, p, g = th[:4]
    return GarchParams(mu=m + sd * float(mu), omega=sd * sd * float(np.exp(w)), alpha=float(p * g),
                       beta=float(p * (1.0 - g)), nu=float(th[4]) if dist == "t" else None)


def standard_factors(alpha: float, nu: Optional[float]):
    """(VaR, ES) of the unit-variance innovation (normal if nu is None, else unit-variance t)."""
    if nu is None:
        return float(analytic.var_normal(alpha)), float(analytic.es_normal(alpha))
    return float(analytic.var_t_std(alpha, nu)), float(analytic.es_t_std(alpha, nu))


def _dist_for(mu: float, sigma: float, nu: Optional[float]) -> LocScale:
    if nu is None:
        return LocScale(mu, sigma)
    return LocScale(mu, float(analytic.t_scale_from_sd(sigma, nu)), nu)


class GarchTModel:
    """GARCH(1,1) with unit-variance t innovations (``dist="t"``, default) or normal QMLE.

    * ``fit(window)``: MLE (:func:`fit_garch11`), then the variance recursion over
      the window from ``sigma2_0`` = the window's sample variance; the forecast
      variance for the next day is ``sigma2_n``.
    * ``update(loss)``: parameters fixed, ``sigma2 <- omega + alpha (loss - mu)**2 + beta sigma2``.
    * ``refit_every`` (default 20) is read by :func:`risklab.engine.walk_forward`.
    * VaR / ES: ``mu + sigma * q`` with the unit-variance t (or normal) quantile /
      tail mean from :mod:`risklab.analytic`; ``Forecast.dist`` is the day's
      predictive distribution.
    """

    name = "garch_t"

    def __init__(self, refit_every: int = 20, dist: str = "t"):
        if isinstance(refit_every, bool) or not isinstance(refit_every, int) or refit_every < 1:
            raise ValueError("refit_every must be an int >= 1")
        if dist not in ("normal", "t"):
            raise ValueError("dist must be 'normal' or 't'")
        self.refit_every = refit_every
        self.dist = dist
        if dist == "normal":
            self.name = "garch_normal"
        self.params: Optional[GarchParams] = None
        self.sigma2_next: Optional[float] = None
        self._factors = {}

    def fit(self, losses_past: np.ndarray) -> None:
        x = as_loss_array(losses_past, "losses_past")
        self._factors = {}
        self.params = fit_garch11(x, self.dist)
        path = variance_path(self.params, x, float(np.var(x)))
        self.sigma2_next = float(path[-1])

    def update(self, loss: float) -> None:
        if self.params is None:
            raise RuntimeError("fit() must be called before update()")
        p = self.params
        self.sigma2_next = p.omega + p.alpha * (float(loss) - p.mu) ** 2 + p.beta * self.sigma2_next

    def forecast(self, alphas: Sequence[float]) -> Forecast:
        if self.params is None:
            raise RuntimeError("fit() must be called before forecast()")
        p, sigma = self.params, float(np.sqrt(self.sigma2_next))
        var, es = {}, {}
        for a in check_alphas(alphas):
            kv, ke = self._factors.get(a) or self._factors.setdefault(a, standard_factors(a, p.nu))
            var[a], es[a] = p.mu + sigma * kv, p.mu + sigma * ke
        return Forecast(var=var, es=es, dist=_dist_for(p.mu, sigma, p.nu))
