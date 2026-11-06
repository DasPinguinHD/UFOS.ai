"""Reverse DCF: the growth (or discount rate) that a given price implies.

Instead of forecasting cash flows to get a value, the reverse DCF takes the price as given and
solves the DCF of :mod:`risklab.valuation.dcf` for the one input that makes the model value equal
to it - "what does the market price in?" (Mauboussin and Rappaport, *Expectations Investing*).

* :func:`implied_growth` with ``mode="constant"``: the constant explicit-period growth g;
  ``mode="fade_start"``: the start growth of a linear fade to the terminal growth.
* :func:`implied_discount_rate`: the discount rate r for the given growth path.

Root finding: Brent's method (``scipy.optimize.brentq``) on a bracket ``[lo, hi]`` that the caller
can set. The function values at both ends must have opposite signs; otherwise
:class:`NoRootInBracket` is raised with both values - never a clipped or extrapolated answer.
Uniqueness: for ``fcf0 > 0`` the value per share is strictly increasing in g (every FCF_t and the
terminal value rise with g; also for the fade start, since every g_t rises with it) and strictly
decreasing in r on ``r > g_T``, so a root in the bracket is the only one. The reverse DCF
therefore requires ``fcf0 > 0`` (:class:`~risklab.valuation.dcf.DCFInputError` otherwise).
"""
from __future__ import annotations

from typing import Optional

from scipy import optimize

from .dcf import DCFInputError, DCFInputs, value_per_share

MODES = ("constant", "fade_start")
XTOL = 1e-12


class NoRootInBracket(ValueError):
    """The value at both ends of the bracket lies on the same side of the price."""

    def __init__(self, what: str, lo: float, hi: float, v_lo: float, v_hi: float, price: float):
        self.lo, self.hi, self.v_lo, self.v_hi, self.price = lo, hi, v_lo, v_hi, price
        super().__init__(f"no {what} in [{lo:g}, {hi:g}] gives the price {price:g}: the value per share is "
                         f"{v_lo:g} at {lo:g} and {v_hi:g} at {hi:g}; widen the bracket if economically sensible")


def _check(inputs: DCFInputs, price: float):
    if not inputs.fcf0 > 0:
        raise DCFInputError("the reverse DCF needs fcf0 > 0 (the value is then monotone in growth and the "
                            "discount rate, so the root is unique)")
    if not (price == price and abs(price) != float("inf")):
        raise DCFInputError("price must be finite")


def _solve(f, lo: float, hi: float, what: str, price: float) -> float:
    if not lo < hi:
        raise ValueError(f"bracket must have lo < hi, got [{lo}, {hi}]")
    f_lo, f_hi = f(lo), f(hi)
    if f_lo == 0.0:
        return lo
    if f_hi == 0.0:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        raise NoRootInBracket(what, lo, hi, f_lo + price, f_hi + price, price)
    return float(optimize.brentq(f, lo, hi, xtol=XTOL, rtol=4 * 2.220446049250313e-16, maxiter=200))


def implied_growth(price: float, inputs: DCFInputs, mode: str = "constant", lo: float = -0.5,
                   hi: float = 1.0) -> float:
    """The growth that makes the value per share equal ``price`` (``inputs.growth`` / ``inputs.fade`` are
    replaced: ``constant`` -> fade off, ``fade_start`` -> fade on)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    _check(inputs, price)
    if lo <= -1.0:
        raise ValueError("lo must be > -1 (growth of -100 % or less is undefined)")
    fade = mode == "fade_start"
    base = inputs.replace(fade=fade)
    return _solve(lambda g: value_per_share(base.replace(growth=g)) - price, lo, hi, f"{mode} growth", price)


def implied_discount_rate(price: float, inputs: DCFInputs, lo: Optional[float] = None, hi: float = 1.0) -> float:
    """The discount rate that makes the value per share equal ``price`` for the given growth path.
    ``lo`` defaults to ``terminal_growth + 1e-4`` (the value tends to infinity as r falls to g_T)."""
    _check(inputs, price)
    lo = inputs.terminal_growth + 1e-4 if lo is None else lo
    if lo <= inputs.terminal_growth:
        raise DCFInputError("lo must exceed terminal_growth")
    return _solve(lambda r: value_per_share(inputs.replace(discount_rate=r)) - price, lo, hi, "discount rate", price)
