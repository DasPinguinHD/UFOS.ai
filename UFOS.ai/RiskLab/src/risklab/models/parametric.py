"""Parametric normal and Student-t VaR / ES models (loss sign, one-day horizon)."""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np
from scipy import optimize, special

from risklab import analytic
from risklab.conventions import as_loss_array, check_alphas
from risklab.dists import LocScale
from risklab.models.base import FitError, Forecast

#: Bounds for the degrees of freedom of the t maximum-likelihood fit. Lower
#: bound 2.05: the variance must exist (nu > 2) and nu near 2 makes the fit
#: unstable. Upper bound 200: beyond it the t is numerically a normal and the
#: likelihood is flat in nu.
NU_BOUNDS = (2.05, 200.0)
#: Search box for location and log scale on the standardised data (see :func:`fit_t_mle`):
#: |loc| <= 20 start scales and scale within exp(+-10) of the start scale. It keeps the line
#: search away from overflow (u**2 for a vanishing scale) and never binds for real data.
LOC_BOUNDS = (-20.0, 20.0)
LOG_SCALE_BOUNDS = (-10.0, 10.0)




class NormalModel:
    """Normal VaR / ES with the window's sample mean and sample std (ddof=1).

    ``VaR = mu + sigma * Phi^-1(alpha)``, ``ES = mu + sigma * phi(Phi^-1(alpha)) / (1 - alpha)``.
    Needs at least 2 losses.
    """

    name = "normal"

    def __init__(self):
        self.mu = None
        self.sigma = None

    def fit(self, losses_past: np.ndarray) -> None:
        x = as_loss_array(losses_past, "losses_past")
        if x.shape[0] < 2:
            raise ValueError("NormalModel needs at least 2 losses")
        self.mu = float(np.mean(x))
        self.sigma = float(np.std(x, ddof=1))

    def forecast(self, alphas: Sequence[float]) -> Forecast:
        if self.mu is None:
            raise RuntimeError("fit() must be called before forecast()")
        var, es = {}, {}
        for a in check_alphas(alphas):
            var[a] = float(analytic.var_normal(a, self.mu, self.sigma))
            es[a] = float(analytic.es_normal(a, self.mu, self.sigma))
        return Forecast(var=var, es=es, dist=LocScale(self.mu, self.sigma))


def t_neg_loglik(params, x, nu_fixed=None):
    """Negative log-likelihood of a location-scale t and its gradient.

    ``params`` = ``(loc, log scale, nu)``, or ``(loc, log scale)`` with ``nu_fixed``.
    Per observation, with ``u = (x - loc) / scale`` and ``w = 1 + u**2 / nu``:
    ``log f = lgamma((nu+1)/2) - lgamma(nu/2) - log(nu*pi)/2 - log scale - (nu+1)/2 * log w``
    (the same density as ``scipy.stats.t.logpdf``). Returns ``(value, gradient)``.
    """
    if nu_fixed is None:
        loc, log_scale, nu = params
    else:
        (loc, log_scale), nu = params, nu_fixed
    scale = np.exp(log_scale)
    u = (x - loc) / scale
    w = 1.0 + u * u / nu
    n = x.shape[0]
    log_w = np.log1p(u * u / nu)
    ll = n * (special.gammaln((nu + 1.0) / 2.0) - special.gammaln(nu / 2.0) - 0.5 * np.log(nu * np.pi)
              - log_scale) - 0.5 * (nu + 1.0) * np.sum(log_w)
    d_loc = np.sum((nu + 1.0) * u / (nu * scale * w))
    d_log_scale = -n + np.sum((nu + 1.0) * u * u / (nu * w))
    grad = [-d_loc, -d_log_scale]
    if nu_fixed is None:
        d_nu = (n * (0.5 * special.digamma((nu + 1.0) / 2.0) - 0.5 * special.digamma(nu / 2.0) - 0.5 / nu)
                - 0.5 * np.sum(log_w) + 0.5 * (nu + 1.0) * np.sum(u * u / (nu * nu * w)))
        grad.append(-d_nu)
    return float(-ll), np.array(grad, dtype=np.float64)


def fit_t_mle(x: np.ndarray, nu: Optional[float] = None):
    """Maximum-likelihood fit of a location-scale t: returns ``(loc, scale, nu)``.

    The objective is the t negative log-likelihood (:func:`t_neg_loglik`, the
    same function ``scipy.stats.t.fit`` minimises, here with an analytic
    gradient for speed). It is minimised with L-BFGS-B over
    ``(loc, log scale, nu)`` so that nu can be bounded to :data:`NU_BOUNDS`;
    ``scipy.stats.t.fit`` itself cannot bound nu. Start values: the median, a
    MAD-based scale, and nu in {4, 8, 30} (the best of the three starts wins;
    deterministic). The fit runs on the data standardised by those start values
    (well-conditioned for daily losses of order 0.01) and is mapped back; on
    that scale loc and log scale are kept in :data:`LOC_BOUNDS` and
    :data:`LOG_SCALE_BOUNDS` (a wide box that only stops overflowing steps). With
    ``nu`` given, only ``loc`` and ``scale`` are fitted.

    Raises:
        FitError: if the optimiser run with the lowest objective did not report
            success (``res.success`` false); there is no silent fallback.
    """
    x = as_loss_array(x, "losses_past")
    if x.shape[0] < 3:
        raise ValueError("StudentTModel needs at least 3 losses")
    loc0 = float(np.median(x))
    mad = float(np.median(np.abs(x - loc0))) * 1.4826
    scale0 = mad if mad > 0 else float(np.std(x))
    if not scale0 > 0:
        raise ValueError("the window has zero dispersion; a t cannot be fitted")
    z = (x - loc0) / scale0
    best = None
    if nu is None:
        for nu0 in (4.0, 8.0, 30.0):
            res = optimize.minimize(t_neg_loglik, x0=[0.0, 0.0, nu0], args=(z, None), jac=True,
                                    method="L-BFGS-B", bounds=[LOC_BOUNDS, LOG_SCALE_BOUNDS, NU_BOUNDS])
            if np.isfinite(res.fun) and (best is None or res.fun < best.fun):
                best = res
        loc, log_scale, nu_hat = best.x
    else:
        res = optimize.minimize(t_neg_loglik, x0=[0.0, 0.0], args=(z, float(nu)), jac=True,
                                method="L-BFGS-B", bounds=[LOC_BOUNDS, LOG_SCALE_BOUNDS])
        best = res
        (loc, log_scale), nu_hat = res.x, float(nu)
    if best is None or not np.isfinite(best.fun):
        raise FitError("t maximum-likelihood fit failed: no finite objective value")
    if not best.success:
        raise FitError(f"t maximum-likelihood fit did not converge: {best.message}")
    return float(loc0 + scale0 * loc), float(scale0 * np.exp(log_scale)), float(nu_hat)


class StudentTModel:
    """Location-scale Student-t VaR / ES fitted by maximum likelihood.

    * ``nu=None``: fit ``(loc, scale, nu)``, nu bounded to :data:`NU_BOUNDS` = (2.05, 200).
    * ``nu`` given (> 2): fit ``loc`` and ``scale`` with nu fixed.

    ``VaR = loc + scale * t_nu^-1(alpha)``,
    ``ES = loc + scale * f_nu(q) / (1 - alpha) * (nu + q**2) / (nu - 1)``, ``q = t_nu^-1(alpha)``.
    ``scale`` is the t scale, NOT the standard deviation
    (``sd = scale * sqrt(nu / (nu - 2))``). See :func:`fit_t_mle`.
    """

    name = "student_t"

    def __init__(self, nu: Optional[float] = None):
        if nu is not None and not float(nu) > 2.0:
            raise ValueError("nu must be > 2")
        self.nu = None if nu is None else float(nu)
        self.loc = self.scale = self.nu_hat = None

    def fit(self, losses_past: np.ndarray) -> None:
        self.loc, self.scale, self.nu_hat = fit_t_mle(losses_past, self.nu)

    def forecast(self, alphas: Sequence[float]) -> Forecast:
        if self.loc is None:
            raise RuntimeError("fit() must be called before forecast()")
        var, es = {}, {}
        for a in check_alphas(alphas):
            var[a] = float(analytic.var_t(a, self.nu_hat, self.loc, self.scale))
            es[a] = float(analytic.es_t(a, self.nu_hat, self.loc, self.scale))
        return Forecast(var=var, es=es, dist=LocScale(self.loc, self.scale, self.nu_hat))
