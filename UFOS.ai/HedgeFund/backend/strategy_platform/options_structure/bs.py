"""Black–Scholes–Merton with a continuous dividend yield q (SP §21.12): prices, greeks and the implied volatility —
the platform's OWN numbers (the vendor IV / greeks in the archive are only compared, never used).

    d1 = (ln(S/K) + (r − q + σ²/2) T) / (σ √T),   d2 = d1 − σ √T
    call = S e^{−qT} N(d1) − K e^{−rT} N(d2),      put = K e^{−rT} N(−d2) − S e^{−qT} N(−d1)
    delta_call = e^{−qT} N(d1), delta_put = −e^{−qT} N(−d1);  gamma = e^{−qT} n(d1) / (S σ √T)
    vega = S e^{−qT} n(d1) √T (per 1.00 vol, i.e. per 100 vol points);  theta per YEAR (calendar)
    dollar gamma (per contract, per 1 % move) = gamma × multiplier × S² × 0.01

Implied volatility: a bracketed root finder (pure Python, no SciPy in the backend: Newton steps kept inside the
bracket, bisection otherwise) on [iv_min, iv_max]. A price outside the no-arbitrage bounds (below the discounted
intrinsic value or above the upper bound) or no sign change in the bracket → :class:`NoIV` with the reason; the value is never clipped.

The rate: :func:`rate_from_discount_yield` converts a T-bill discount yield (FRED DTB3, % on an ACT/360 discount
basis) into a continuously compounded ACT/365 rate: price = 1 − d·n/360, r = −ln(price)·365/n.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

SQRT2 = math.sqrt(2.0)
INV_SQRT2PI = 1.0 / math.sqrt(2.0 * math.pi)


class NoIV(ValueError):
    """No implied volatility: the reason says why (intrinsic, bounds, no root in the bracket, bad input)."""


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / SQRT2)


def norm_pdf(x: float) -> float:
    return INV_SQRT2PI * math.exp(-0.5 * x * x)


def _check(S, K, T, sigma=None):
    for name, v in (("spot", S), ("strike", K), ("time", T)):
        if not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise ValueError(f"{name} must be a positive finite number (got {v!r})")
    if sigma is not None and (not math.isfinite(sigma) or sigma <= 0):
        raise ValueError(f"volatility must be positive (got {sigma!r})")


def d1d2(S, K, T, r, q, sigma):
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / v
    return d1, d1 - v


def price(kind: str, S, K, T, r, q, sigma) -> float:
    _check(S, K, T, sigma)
    d1, d2 = d1d2(S, K, T, r, q, sigma)
    dq, dr = math.exp(-q * T), math.exp(-r * T)
    if kind == "call":
        return S * dq * norm_cdf(d1) - K * dr * norm_cdf(d2)
    if kind == "put":
        return K * dr * norm_cdf(-d2) - S * dq * norm_cdf(-d1)
    raise ValueError("kind must be call or put")


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float        # per calendar year


def greeks(kind: str, S, K, T, r, q, sigma) -> Greeks:
    _check(S, K, T, sigma)
    d1, d2 = d1d2(S, K, T, r, q, sigma)
    dq, dr = math.exp(-q * T), math.exp(-r * T)
    sq = math.sqrt(T)
    pdf = norm_pdf(d1)
    gamma = dq * pdf / (S * sigma * sq)
    vega = S * dq * pdf * sq
    common = -S * dq * pdf * sigma / (2.0 * sq)
    if kind == "call":
        p = S * dq * norm_cdf(d1) - K * dr * norm_cdf(d2)
        delta = dq * norm_cdf(d1)
        theta = common - r * K * dr * norm_cdf(d2) + q * S * dq * norm_cdf(d1)
    elif kind == "put":
        p = K * dr * norm_cdf(-d2) - S * dq * norm_cdf(-d1)
        delta = -dq * norm_cdf(-d1)
        theta = common + r * K * dr * norm_cdf(-d2) - q * S * dq * norm_cdf(-d1)
    else:
        raise ValueError("kind must be call or put")
    return Greeks(p, delta, gamma, vega, theta)


def gamma(S, K, T, r, q, sigma) -> float:
    _check(S, K, T, sigma)
    d1, _ = d1d2(S, K, T, r, q, sigma)
    return math.exp(-q * T) * norm_pdf(d1) / (S * sigma * math.sqrt(T))


def dollar_gamma(g: float, S: float, multiplier: float) -> float:
    """USD change of delta-dollars for a 1 % move, per contract: gamma × multiplier × S² × 0.01."""
    return g * multiplier * S * S * 0.01


def bounds(kind: str, S, K, T, r, q):
    """No-arbitrage (lower, upper) bounds of a European option price."""
    fs, fk = S * math.exp(-q * T), K * math.exp(-r * T)
    if kind == "call":
        return max(0.0, fs - fk), fs
    return max(0.0, fk - fs), fk


def implied_vol(kind: str, target: float, S, K, T, r, q, lo: float = 0.001, hi: float = 5.0,
                tol: float = 1e-10, max_iter: int = 200) -> float:
    """σ with price(σ) = target, found in [lo, hi]; raises :class:`NoIV` with the reason."""
    try:
        _check(S, K, T)
    except ValueError as ex:
        raise NoIV(str(ex)) from None
    if not isinstance(target, (int, float)) or not math.isfinite(target) or target <= 0:
        raise NoIV("no positive option price")
    lb, ub = bounds(kind, S, K, T, r, q)
    if target <= lb:
        raise NoIV(f"price {target:.4g} at or below the no-arbitrage lower bound {lb:.4g} (intrinsic value)")
    if target >= ub:
        raise NoIV(f"price {target:.4g} at or above the upper bound {ub:.4g}")
    f = lambda s: price(kind, S, K, T, r, q, s) - target   # noqa: E731 — increasing in σ
    a, b = lo, hi
    fa, fb = f(a), f(b)
    if fa > 0:
        raise NoIV(f"price {target:.4g} below the model price at σ = {lo:g} — no root in [{lo:g}, {hi:g}]")
    if fb < 0:
        raise NoIV(f"price {target:.4g} above the model price at σ = {hi:g} — no root in [{lo:g}, {hi:g}]")
    # Newton steps safeguarded by the bracket (bisection whenever a step leaves it) on a monotone function
    x = 0.3 if a < 0.3 < b else 0.5 * (a + b)
    for _ in range(max_iter):
        fx = f(x)
        if abs(fx) <= tol * max(1.0, target):
            return x
        if fx < 0:
            a = x
        else:
            b = x
        if b - a <= 1e-12:
            return 0.5 * (a + b)
        d1, _ = d1d2(S, K, T, r, q, x)
        vega = S * math.exp(-q * T) * norm_pdf(d1) * math.sqrt(T)
        xn = x - fx / vega if vega > 1e-12 else None
        x = xn if xn is not None and a < xn < b else 0.5 * (a + b)
    raise NoIV(f"no convergence in {max_iter} steps")


def rate_from_discount_yield(d_pct: float, days: int = 91) -> float:
    """T-bill discount yield (%, ACT/360) → continuously compounded rate (decimal, ACT/365)."""
    if not math.isfinite(d_pct) or not 0 < days <= 366:
        raise ValueError("bad discount yield / tenor")
    p = 1.0 - d_pct / 100.0 * days / 360.0
    if p <= 0:
        raise ValueError("discount yield gives a non-positive price")
    return -math.log(p) * 365.0 / days


def forward(S: float, T: float, r: float, q: float) -> float:
    return S * math.exp((r - q) * T)


def parity_dividend_yield(call_mid: float, put_mid: float, S: float, K: float, T: float, r: float) -> Optional[float]:
    """Implied continuous q from put-call parity C − P = S e^{−qT} − K e^{−rT} (a CROSS-CHECK of the q assumption);
    None when the implied discounted spot is not positive."""
    x = call_mid - put_mid + K * math.exp(-r * T)
    if x <= 0 or T <= 0:
        return None
    return -math.log(x / S) / T
