"""Free-cash-flow DCF: explicit forecast period, Gordon terminal value, enterprise-to-equity bridge.

Textbook construction (e.g. Koller, Goedhart and Wessels, *Valuation*, the enterprise DCF model;
Damodaran, *Investment Valuation*, the FCFF model). All rates are annual decimals; cash flows
arrive at the END of each year (no mid-year convention).

Growth path over the explicit period of N years:

* constant (``fade=False``): ``g_t = g`` for t = 1..N;
* linear fade (``fade=True``, N >= 2): ``g_t = g + (g_T - g) (t - 1) / (N - 1)`` - the start
  growth ``g`` in year 1, falling (or rising) in a straight line to the terminal growth ``g_T``
  in year N.

Value::

    FCF_t = FCF_0 prod_{s<=t} (1 + g_s)
    EV    = sum_{t=1..N} FCF_t (1 + r)^-t  +  TV (1 + r)^-N,    TV = FCF_N (1 + g_T) / (r - g_T)
    equity = EV - net debt - other claims,   value per share = equity / shares

With ``g = g_T`` the explicit period changes nothing: ``EV = FCF_0 (1 + g) / (r - g)``, the
Gordon growth perpetuity (tested).

Input rules (each violation raises :class:`DCFInputError`, never a silently adjusted value):
finite inputs; ``r > -1``; ``g, g_T > -1``; ``r > g_T`` strictly (otherwise the terminal value
does not exist); ``years`` a whole number >= 1 (>= 2 with a fade); ``shares > 0``. ``fcf0`` may
have any sign here; the reverse DCF needs ``fcf0 > 0`` (:mod:`risklab.valuation.reverse`).

Everything is vectorised (:func:`per_share_vec`) so that Monte-Carlo sensitivity analyses can
evaluate many input sets at once; :func:`dcf` is the scalar, itemised version.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Dict

import numpy as np


class DCFInputError(ValueError):
    """Inputs for which the DCF is undefined (e.g. discount rate <= terminal growth)."""


@dataclass(frozen=True)
class DCFInputs:
    fcf0: float                 # free cash flow to the firm of the last year (year 0)
    discount_rate: float        # r, e.g. the WACC
    terminal_growth: float      # g_T, growth after the explicit period
    growth: float               # g: constant growth, or the start growth with fade=True
    years: int = 10             # N, length of the explicit period
    fade: bool = False
    net_debt: float = 0.0
    other_claims: float = 0.0   # minorities, pension deficits, ... (an input)
    shares: float = 1.0

    def __post_init__(self):
        _validate(np.asarray([self.fcf0], dtype=np.float64), np.asarray([self.discount_rate], dtype=np.float64),
                  np.asarray([self.terminal_growth], dtype=np.float64), np.asarray([self.growth], dtype=np.float64),
                  self.years, self.fade, np.asarray([self.net_debt], dtype=np.float64),
                  np.asarray([self.other_claims], dtype=np.float64), np.asarray([self.shares], dtype=np.float64))

    def replace(self, **changes) -> "DCFInputs":
        return dataclasses.replace(self, **changes)


@dataclass(frozen=True)
class DCFResult:
    growth_path: np.ndarray
    fcf: np.ndarray
    discount_factors: np.ndarray
    pv_explicit: float
    terminal_value: float
    pv_terminal: float
    enterprise_value: float
    equity_value: float
    per_share: float

    @property
    def terminal_share(self) -> float:
        """Share of the enterprise value that comes from the terminal value."""
        return self.pv_terminal / self.enterprise_value


def _check_years(years, fade: bool) -> int:
    if isinstance(years, bool) or not isinstance(years, (int, np.integer)) or years < 1:
        raise DCFInputError(f"years must be a whole number >= 1, got {years!r}")
    if fade and years < 2:
        raise DCFInputError("a linear fade needs years >= 2")
    return int(years)


def _validate(fcf0, r, gT, g, years, fade, net_debt, other, shares) -> int:
    n = _check_years(years, fade)
    for name, a in (("fcf0", fcf0), ("discount_rate", r), ("terminal_growth", gT), ("growth", g),
                    ("net_debt", net_debt), ("other_claims", other), ("shares", shares)):
        if not np.all(np.isfinite(a)):
            raise DCFInputError(f"{name} must be finite")
    if np.any(r <= -1.0):
        raise DCFInputError("discount_rate must be > -1")
    if np.any(g <= -1.0) or np.any(gT <= -1.0):
        raise DCFInputError("growth and terminal_growth must be > -1")
    bad = int(np.count_nonzero(r <= gT))
    if bad:
        raise DCFInputError(f"discount_rate must exceed terminal_growth (violated in {bad} of {r.size} input sets): "
                            "the Gordon terminal value does not exist otherwise")
    if np.any(shares <= 0):
        raise DCFInputError("shares must be > 0")
    return n


def growth_path(growth: float, terminal_growth: float, years: int, fade: bool = False) -> np.ndarray:
    """Year-by-year growth g_1..g_N (module docstring)."""
    n = _check_years(years, fade)
    return _paths(np.atleast_1d(np.float64(growth)), np.atleast_1d(np.float64(terminal_growth)), n, fade)[0]


def _paths(g: np.ndarray, gT: np.ndarray, n: int, fade: bool) -> np.ndarray:
    if not fade:
        return np.repeat(g[:, None], n, axis=1)
    w = np.arange(n, dtype=np.float64) / (n - 1)
    return g[:, None] + (gT - g)[:, None] * w[None, :]


def _ev_parts(fcf0, r, gT, g, n: int, fade: bool):
    gp = _paths(g, gT, n, fade)
    fcf = fcf0[:, None] * np.cumprod(1.0 + gp, axis=1)
    t = np.arange(1, n + 1, dtype=np.float64)
    df = (1.0 + r)[:, None] ** -t[None, :]
    pv_explicit = np.sum(fcf * df, axis=1)
    tv = fcf[:, -1] * (1.0 + gT) / (r - gT)
    return gp, fcf, df, pv_explicit, tv, tv * df[:, -1]


def _bcast(**kw) -> Dict[str, np.ndarray]:
    arrs = np.broadcast_arrays(*[np.atleast_1d(np.asarray(v, dtype=np.float64)) for v in kw.values()])
    return {k: a.astype(np.float64).ravel() for k, a in zip(kw, arrs)}


def per_share_vec(fcf0, discount_rate, terminal_growth, growth, years: int = 10, fade: bool = False,
                  net_debt=0.0, other_claims=0.0, shares=1.0) -> np.ndarray:
    """Value per share for arrays of inputs (broadcast against each other; ``years`` and ``fade`` are scalars)."""
    a = _bcast(fcf0=fcf0, r=discount_rate, gT=terminal_growth, g=growth, nd=net_debt, oc=other_claims, sh=shares)
    n = _validate(a["fcf0"], a["r"], a["gT"], a["g"], years, fade, a["nd"], a["oc"], a["sh"])
    *_, pv_explicit, _tv, pv_tv = _ev_parts(a["fcf0"], a["r"], a["gT"], a["g"], n, fade)
    return (pv_explicit + pv_tv - a["nd"] - a["oc"]) / a["sh"]


def dcf(inputs: DCFInputs) -> DCFResult:
    """The itemised DCF of one input set."""
    one = lambda x: np.asarray([x], dtype=np.float64)  # noqa: E731
    gp, fcf, df, pv_e, tv, pv_tv = _ev_parts(one(inputs.fcf0), one(inputs.discount_rate), one(inputs.terminal_growth),
                                             one(inputs.growth), int(inputs.years), inputs.fade)
    ev = float(pv_e[0] + pv_tv[0])
    eq = ev - inputs.net_debt - inputs.other_claims
    return DCFResult(growth_path=gp[0], fcf=fcf[0], discount_factors=df[0], pv_explicit=float(pv_e[0]),
                     terminal_value=float(tv[0]), pv_terminal=float(pv_tv[0]), enterprise_value=ev,
                     equity_value=eq, per_share=eq / inputs.shares)


def value_per_share(inputs: DCFInputs) -> float:
    return dcf(inputs).per_share


def explicit_cagr(inputs: DCFInputs) -> float:
    """Compound annual FCF growth over the explicit period, ``(FCF_N / FCF_0)^(1/N) - 1``
    (equals ``growth`` without a fade)."""
    gp = growth_path(inputs.growth, inputs.terminal_growth, inputs.years, inputs.fade)
    return float(math.exp(np.mean(np.log1p(gp))) - 1.0)
