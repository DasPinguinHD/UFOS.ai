"""Closed-form one-day VaR and ES of the normal and Student-t loss distributions.

Sign convention (see :mod:`risklab.conventions`): the argument is the LOSS
distribution. ``VaR_alpha`` is the alpha-quantile of the loss and ``ES_alpha``
the mean loss beyond it, ``E[L | L >= VaR_alpha]``. ``alpha`` lies in (0, 1).

Location-scale forms (``mu`` / ``loc`` and ``sigma`` / ``scale`` broadcast
like numpy arrays):

* normal:   ``VaR = mu + sigma * z``,  ``ES = mu + sigma * phi(z) / (1 - alpha)``
  with ``z = Phi^-1(alpha)``.
* Student-t with ``nu`` degrees of freedom, location ``loc`` and SCALE ``scale``
  (the scale is not the standard deviation; ``sd = scale * sqrt(nu / (nu - 2))``):
  ``VaR = loc + scale * q``,
  ``ES = loc + scale * f_nu(q) / (1 - alpha) * (nu + q**2) / (nu - 1)``
  with ``q = t_nu^-1(alpha)``; ES needs ``nu > 1``.
* "standardised" t (:func:`var_t_std`, :func:`es_t_std`): a t scaled to unit
  variance, then multiplied by ``sigma`` = the standard deviation; this is the
  innovation used by the synthetic generators. It needs ``nu > 2``.
"""
from __future__ import annotations

import numpy as np
from scipy import special

from risklab.conventions import check_alpha


def _nu(nu, minimum: float) -> np.ndarray:
    v = np.asarray(nu, dtype=np.float64)
    if not np.all(np.isfinite(v)) or np.any(v <= minimum):
        raise ValueError(f"nu must be finite and > {minimum:g}")
    return v


def _scale(name: str, s) -> np.ndarray:
    v = np.asarray(s, dtype=np.float64)
    if not np.all(np.isfinite(v)) or np.any(v < 0.0):
        raise ValueError(f"{name} must be finite and >= 0")
    return v


def var_normal(alpha: float, mu=0.0, sigma=1.0):
    """Normal VaR: ``mu + sigma * Phi^-1(alpha)``."""
    a = check_alpha(alpha)
    return np.asarray(mu, dtype=np.float64) + _scale("sigma", sigma) * special.ndtri(a)


def es_normal(alpha: float, mu=0.0, sigma=1.0):
    """Normal ES: ``mu + sigma * phi(Phi^-1(alpha)) / (1 - alpha)``."""
    a = check_alpha(alpha)
    z = special.ndtri(a)  # Phi^-1(alpha), as stats.norm.ppf but without its per-call overhead
    pdf = np.exp(-0.5 * z * z) / np.sqrt(2.0 * np.pi)
    return np.asarray(mu, dtype=np.float64) + _scale("sigma", sigma) * pdf / (1.0 - a)


def _t_pdf(q, nu):
    """Standard Student-t density f_nu(q) (as stats.t.pdf)."""
    log_f = (special.gammaln((nu + 1.0) / 2.0) - special.gammaln(nu / 2.0) - 0.5 * np.log(nu * np.pi)
             - 0.5 * (nu + 1.0) * np.log1p(q * q / nu))
    return np.exp(log_f)


def var_t(alpha: float, nu, loc=0.0, scale=1.0):
    """Location-scale Student-t VaR: ``loc + scale * t_nu^-1(alpha)`` (``nu > 0``)."""
    a = check_alpha(alpha)
    v = _nu(nu, 0.0)
    return np.asarray(loc, dtype=np.float64) + _scale("scale", scale) * special.stdtrit(v, a)


def es_t(alpha: float, nu, loc=0.0, scale=1.0):
    """Location-scale Student-t ES (needs ``nu > 1``).

    ``loc + scale * f_nu(q) / (1 - alpha) * (nu + q**2) / (nu - 1)``, ``q = t_nu^-1(alpha)``.
    """
    a = check_alpha(alpha)
    v = _nu(nu, 1.0)
    q = special.stdtrit(v, a)  # t_nu^-1(alpha), as stats.t.ppf without its per-call overhead
    tail = _t_pdf(q, v) / (1.0 - a) * (v + q * q) / (v - 1.0)
    return np.asarray(loc, dtype=np.float64) + _scale("scale", scale) * tail


def t_scale_from_sd(sd, nu):
    """The t scale whose standard deviation is ``sd``: ``sd * sqrt((nu - 2) / nu)`` (``nu > 2``)."""
    v = _nu(nu, 2.0)
    return _scale("sd", sd) * np.sqrt((v - 2.0) / v)


def var_t_std(alpha: float, nu, mu=0.0, sigma=1.0):
    """VaR of ``mu + sigma * Z`` with Z a unit-variance Student-t (``nu > 2``)."""
    return var_t(alpha, nu, mu, t_scale_from_sd(sigma, nu))


def es_t_std(alpha: float, nu, mu=0.0, sigma=1.0):
    """ES of ``mu + sigma * Z`` with Z a unit-variance Student-t (``nu > 2``)."""
    return es_t(alpha, nu, mu, t_scale_from_sd(sigma, nu))
