"""Synthetic loss series with known true parameters (no real or third-party data).

Every generator returns a :class:`SyntheticSeries`. It carries the per-day true
conditional *parameters* (mean, scale, distribution) and, for the regime
model, the state path. It never carries a quantile, VaR or ES: those follow
from the parameters only once sheet 1 is derived (derive-first rule).

Model of every generator: ``L_t = mu_t + sigma_t * z_t`` where ``z_t`` is a
standardised innovation (mean 0, variance 1) of the family ``dist`` and ``L``
is the loss (positive = loss). So ``mu_true`` is the conditional mean and
``sigma_true`` the conditional standard deviation of the loss.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

from risklab.conventions import SeedLike, make_rng, seed_for_meta


@dataclass(frozen=True)
class SyntheticSeries:
    """A synthetic daily loss series and its true conditional parameters.

    Attributes:
        losses: float64, shape (n,). Positive = loss.
        sigma_true: float64, shape (n,). True conditional std of ``losses[t]``.
        mu_true: float64, shape (n,). True conditional mean of ``losses[t]``.
        dist: innovation family, ``"normal"`` or ``"t"`` (standardised to unit variance).
        dist_params: parameters of the family, e.g. ``{"nu": 5.0}``; empty for normal.
        regime: int64 state path, shape (n,), or None for single-regime generators.
        meta: generator name, its arguments and the seed (None if a Generator was passed).
    The arrays are read-only.
    """

    losses: np.ndarray
    sigma_true: np.ndarray
    mu_true: np.ndarray
    dist: str
    dist_params: dict = field(default_factory=dict)
    regime: Optional[np.ndarray] = None
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        for name in ("losses", "sigma_true", "mu_true", "regime"):
            arr = getattr(self, name)
            if arr is not None:
                arr.setflags(write=False)

    def __len__(self) -> int:
        return int(self.losses.shape[0])


def _check_n(n) -> int:
    if isinstance(n, bool) or not isinstance(n, (int, np.integer)) or n < 1:
        raise ValueError("n must be a positive int")
    return int(n)


def _check_positive(name: str, value) -> float:
    v = float(value)
    if not np.isfinite(v) or v <= 0.0:
        raise ValueError(f"{name} must be a finite number > 0, got {value!r}")
    return v


def _check_finite(name: str, value) -> float:
    v = float(value)
    if not np.isfinite(v):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return v


def _check_nu(nu) -> float:
    if nu is None:
        raise ValueError("nu is required for Student-t innovations")
    v = float(nu)
    if not np.isfinite(v) or v <= 2.0:
        raise ValueError(f"nu must be > 2 (finite variance), got {nu!r}")
    return v


def _unit_t(rng: np.random.Generator, nu: float, size: int) -> np.ndarray:
    """Student-t draws rescaled to unit variance (requires nu > 2)."""
    return rng.standard_t(nu, size=size) * np.sqrt((nu - 2.0) / nu)


def iid_normal(n: int, mu: float, sigma: float, seed: SeedLike) -> SyntheticSeries:
    """i.i.d. normal losses with mean ``mu`` and standard deviation ``sigma``."""
    n = _check_n(n)
    mu = _check_finite("mu", mu)
    sigma = _check_positive("sigma", sigma)
    rng = make_rng(seed)
    z = rng.standard_normal(n)
    return SyntheticSeries(
        losses=mu + sigma * z,
        sigma_true=np.full(n, sigma),
        mu_true=np.full(n, mu),
        dist="normal",
        dist_params={},
        regime=None,
        meta={"generator": "iid_normal", "n": n, "mu": mu, "sigma": sigma,
              "seed": seed_for_meta(seed)},
    )


def iid_student_t(n: int, mu: float, sigma: float, nu: float, seed: SeedLike) -> SyntheticSeries:
    """i.i.d. Student-t losses scaled so that the standard deviation equals ``sigma``.

    Requires ``nu > 2`` (otherwise the variance is not finite): ValueError.
    """
    n = _check_n(n)
    mu = _check_finite("mu", mu)
    sigma = _check_positive("sigma", sigma)
    nu = _check_nu(nu)
    rng = make_rng(seed)
    z = _unit_t(rng, nu, n)
    return SyntheticSeries(
        losses=mu + sigma * z,
        sigma_true=np.full(n, sigma),
        mu_true=np.full(n, mu),
        dist="t",
        dist_params={"nu": nu},
        regime=None,
        meta={"generator": "iid_student_t", "n": n, "mu": mu, "sigma": sigma, "nu": nu,
              "seed": seed_for_meta(seed)},
    )


def garch11(n: int, omega: float, alpha1: float, beta1: float, innovations: str = "normal",
            nu: Optional[float] = None, *, seed: SeedLike, burn_in: int = 500) -> SyntheticSeries:
    """GARCH(1,1) losses with zero conditional mean.

    ``sigma2_t = omega + alpha1 * L_{t-1}**2 + beta1 * sigma2_{t-1}``, ``L_t = sigma_t * z_t``.
    Requires omega, alpha1, beta1 > 0 and alpha1 + beta1 < 1 (covariance
    stationarity), otherwise ValueError. ``innovations="t"`` needs ``nu > 2``;
    the t innovations are scaled to unit variance. The recursion starts at the
    unconditional variance and the first ``burn_in`` days are discarded.
    """
    n = _check_n(n)
    omega = _check_positive("omega", omega)
    alpha1 = _check_positive("alpha1", alpha1)
    beta1 = _check_positive("beta1", beta1)
    if alpha1 + beta1 >= 1.0:
        raise ValueError("alpha1 + beta1 must be < 1 (covariance stationarity)")
    if isinstance(burn_in, bool) or not isinstance(burn_in, (int, np.integer)) or burn_in < 0:
        raise ValueError("burn_in must be an int >= 0")
    burn_in = int(burn_in)
    if innovations == "normal":
        if nu is not None:
            raise ValueError("nu is only used with innovations='t'")
        dist_params = {}
    elif innovations == "t":
        nu = _check_nu(nu)
        dist_params = {"nu": nu}
    else:
        raise ValueError("innovations must be 'normal' or 't'")

    rng = make_rng(seed)
    total = n + burn_in
    z = rng.standard_normal(total) if innovations == "normal" else _unit_t(rng, nu, total)
    sigma2 = np.empty(total)
    losses = np.empty(total)
    s2 = omega / (1.0 - alpha1 - beta1)
    for t in range(total):
        if t > 0:
            s2 = omega + alpha1 * losses[t - 1] ** 2 + beta1 * s2
        sigma2[t] = s2
        losses[t] = np.sqrt(s2) * z[t]
    return SyntheticSeries(
        losses=losses[burn_in:].copy(),
        sigma_true=np.sqrt(sigma2[burn_in:]),
        mu_true=np.zeros(n),
        dist=innovations,
        dist_params=dist_params,
        regime=None,
        meta={"generator": "garch11", "n": n, "omega": omega, "alpha1": alpha1, "beta1": beta1,
              "innovations": innovations, "nu": dist_params.get("nu"), "burn_in": burn_in,
              "seed": seed_for_meta(seed)},
    )


def regime_switching(n: int, sigmas: Sequence[float] = (0.01, 0.03),
                     transition: Sequence[Sequence[float]] = ((0.98, 0.02), (0.05, 0.95)),
                     *, seed: SeedLike, initial_state: int = 0) -> SyntheticSeries:
    """Two-state Markov regime model with zero mean and normal innovations.

    ``sigmas = (low, high)`` is the loss std in state 0 / 1; ``transition[i][j]``
    is P(state j tomorrow | state i today); each row must sum to 1. The chain
    starts in ``initial_state`` (day 0). ``regime`` holds the state path.
    """
    n = _check_n(n)
    if len(sigmas) != 2:
        raise ValueError("sigmas must be (low, high)")
    s = np.array([_check_positive("sigmas[0]", sigmas[0]), _check_positive("sigmas[1]", sigmas[1])])
    p = np.array(transition, dtype=np.float64)
    if p.shape != (2, 2):
        raise ValueError("transition must be a 2x2 matrix")
    if not np.all(np.isfinite(p)) or np.any(p < 0.0) or np.any(p > 1.0):
        raise ValueError("transition probabilities must lie in [0, 1]")
    if not np.allclose(p.sum(axis=1), 1.0, rtol=0.0, atol=1e-12):
        raise ValueError("each transition row must sum to 1")
    if initial_state not in (0, 1):
        raise ValueError("initial_state must be 0 or 1")

    rng = make_rng(seed)
    u = rng.random(n)
    z = rng.standard_normal(n)
    regime = np.empty(n, dtype=np.int64)
    state = int(initial_state)
    for t in range(n):
        if t > 0:
            state = 1 if u[t] < p[state, 1] else 0
        regime[t] = state
    sigma_true = s[regime]
    return SyntheticSeries(
        losses=sigma_true * z,
        sigma_true=sigma_true,
        mu_true=np.zeros(n),
        dist="normal",
        dist_params={},
        regime=regime,
        meta={"generator": "regime_switching", "n": n, "sigmas": [float(s[0]), float(s[1])],
              "transition": p.tolist(), "initial_state": int(initial_state),
              "seed": seed_for_meta(seed)},
    )
