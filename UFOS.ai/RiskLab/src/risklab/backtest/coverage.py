"""Coverage backtests: Kupiec POF, the exact binomial test, Christoffersen independence / conditional coverage.

Size: Kupiec, ind and cc use the asymptotic chi-square distribution. With few
expected exceedances (T = 250, alpha = 0.99: 2.5 expected) the true size of
Kupiec at 5 % is 9.48 %, because x = 0 alone is rejected (LR = 5.03,
p = 0.025, P(X = 0) = 8.1 %). :func:`exact_size` computes the exact size of
the count-based tests; :func:`binomial_exact` is the size-correct alternative
(size <= the nominal level by construction).

All likelihoods use ``0 * ln 0 = 0`` exactly (``scipy.special.xlogy``), so the
edge cases (no exceedance, only exceedances, missing transitions) need no
epsilon. The likelihood-ratio statistics are clamped at 0 from below, only to
remove floating-point round-off when the two likelihoods are equal.

References: Kupiec (1995), "Techniques for verifying the accuracy of risk
measurement models", Journal of Derivatives 3(2); Christoffersen (1998),
"Evaluating interval forecasts", International Economic Review 39(4).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from scipy import stats
from scipy.special import xlogy

from risklab.backtest.base import TestResult, as_exceedances, check_level, decide


def _lr_uc(n_obs: int, n_exceed: int, p: float) -> float:
    """-2 ln[(1-p)^(T-x) p^x / ((1-p_hat)^(T-x) p_hat^x)], p_hat = x / T."""
    t, x = float(n_obs), float(n_exceed)
    p_hat = x / t
    ll0 = xlogy(t - x, 1.0 - p) + xlogy(x, p)
    ll1 = xlogy(t - x, 1.0 - p_hat) + xlogy(x, p_hat)
    return max(0.0, float(-2.0 * (ll0 - ll1)))


def transition_counts(exceedances) -> dict:
    """First-order transition counts ``n_ij`` = #days with I_{t-1} = i and I_t = j (t = 2..T)."""
    e = as_exceedances(exceedances, 2).astype(np.int64)
    prev, cur = e[:-1], e[1:]
    return {
        "n00": int(np.sum((prev == 0) & (cur == 0))),
        "n01": int(np.sum((prev == 0) & (cur == 1))),
        "n10": int(np.sum((prev == 1) & (cur == 0))),
        "n11": int(np.sum((prev == 1) & (cur == 1))),
    }


def _lr_ind(c: dict):
    n00, n01, n10, n11 = c["n00"], c["n01"], c["n10"], c["n11"]
    n = n00 + n01 + n10 + n11
    pi = (n01 + n11) / n
    pi01 = n01 / (n00 + n01) if n00 + n01 else 0.0
    pi11 = n11 / (n10 + n11) if n10 + n11 else 0.0
    ll0 = xlogy(n00 + n10, 1.0 - pi) + xlogy(n01 + n11, pi)
    ll1 = xlogy(n00, 1.0 - pi01) + xlogy(n01, pi01) + xlogy(n10, 1.0 - pi11) + xlogy(n11, pi11)
    notes = []
    if n01 + n11 == 0:
        notes.append("no exceedance on days 2..T: LR_ind = 0 by construction, the test has no information")
    elif n00 + n10 == 0:
        notes.append("every day 2..T is an exceedance: LR_ind = 0 by construction, the test has no information")
    if n10 + n11 == 0:
        notes.append("no transition out of state 1 (no exceedance on days 1..T-1): pi11 is not identified, "
                     "its likelihood terms are 0")
    if n00 + n01 == 0:
        notes.append("no transition out of state 0: pi01 is not identified, its likelihood terms are 0")
    pis = {"pi": pi,
           "pi01": pi01 if n00 + n01 else None,  # None = not identified (no value invented)
           "pi11": pi11 if n10 + n11 else None}
    return max(0.0, float(-2.0 * (ll0 - ll1))), notes, pis


def _kupiec_p_value(n_obs: int, n_exceed: int, p: float) -> float:
    return float(stats.chi2.sf(_lr_uc(n_obs, n_exceed, p), 1))


def _binom_p_value(n_obs: int, n_exceed: int, p: float) -> float:
    return float(stats.binomtest(n_exceed, n_obs, p, alternative="two-sided").pvalue)


_COUNT_TESTS = {"kupiec_pof": _kupiec_p_value, "binomial_exact": _binom_p_value}


def exact_size(test: str, n_obs: int, alpha: float, reject_at: float = 0.05) -> dict:
    """Exact size of a count-based test under H0 (X ~ Binomial(T, 1 - alpha)).

    ``test`` is "kupiec_pof" or "binomial_exact". Returns
    ``{"size": P(reject | H0), "reject_counts": [x, ...]}``: the sum of the
    binomial probabilities of all counts x whose p-value is below ``reject_at``.
    Example: Kupiec, T = 250, alpha = 0.99, 5 %: rejection set {0} and {7, ..., 250},
    size 9.48 %; T = 1000: 5.51 %.
    """
    if test not in _COUNT_TESTS:
        raise ValueError(f"exact_size supports {sorted(_COUNT_TESTS)}")
    a = check_level("alpha", alpha)
    r = check_level("reject_at", reject_at)
    if isinstance(n_obs, bool) or not isinstance(n_obs, (int, np.integer)) or n_obs < 1:
        raise ValueError("n_obs must be an int >= 1")
    p = 1.0 - a
    xs = [x for x in range(int(n_obs) + 1) if _COUNT_TESTS[test](int(n_obs), x, p) < r]
    size = float(np.sum(stats.binom.pmf(xs, int(n_obs), p))) if xs else 0.0
    return {"size": size, "reject_counts": xs}


def kupiec_pof(exceedances, alpha: float, reject_at: float = 0.05) -> TestResult:
    """Kupiec proportion-of-failures test (unconditional coverage).

    H0: the exceedance probability equals ``p = 1 - alpha``.
    ``LR_uc = -2 ln[(1-p)^(T-x) p^x / ((1-p_hat)^(T-x) p_hat^x)]`` with
    ``p_hat = x / T``; asymptotically chi-square with 1 degree of freedom.
    x = 0 and x = T are exact (``0 * ln 0 = 0``).

    Args:
        exceedances: 1-D bool (or 0/1) array, one entry per backtest day.
        alpha: VaR confidence level in (0, 1).
        reject_at: significance level in (0, 1).
    Returns:
        TestResult(name="kupiec_pof", statistic=LR_uc, df=1, p_value, reject_at,
        decision "reject" / "not rejected", n_obs=T, n_exceed=x).
    """
    a = check_level("alpha", alpha)
    r = check_level("reject_at", reject_at)
    e = as_exceedances(exceedances, 1)
    t, x = int(e.shape[0]), int(np.count_nonzero(e))
    lr = _lr_uc(t, x, 1.0 - a)
    p_value = float(stats.chi2.sf(lr, 1))
    return TestResult("kupiec_pof", lr, 1, p_value, r, decide(p_value, r), t, x, "",
                      {"expected_rate": 1.0 - a, "observed_rate": x / t})


def binomial_exact(exceedances, alpha: float, reject_at: float = 0.05) -> TestResult:
    """Exact two-sided binomial test of the exceedance count (size-correct alternative to Kupiec).

    H0: X ~ Binomial(T, 1 - alpha). The two-sided p-value is the total
    probability of all counts that are at most as likely as the observed one
    (the "minimum likelihood" method of ``scipy.stats.binomtest``). Because the
    p-value is exact, the size is at most ``reject_at`` for every T (it is
    usually below it: the binomial is discrete).

    Returns:
        TestResult(name="binomial_exact", statistic=x (the count), df=None,
        p_value, reject_at, decision, n_obs=T, n_exceed=x,
        details={"expected": T * (1 - alpha)}). For the size at a given T use
        :func:`exact_size` ("binomial_exact", T, alpha).
    """
    a = check_level("alpha", alpha)
    r = check_level("reject_at", reject_at)
    e = as_exceedances(exceedances, 1)
    t, x = int(e.shape[0]), int(np.count_nonzero(e))
    p_value = _binom_p_value(t, x, 1.0 - a)
    return TestResult("binomial_exact", float(x), None, p_value, r, decide(p_value, r), t, x, "",
                      {"expected": t * (1.0 - a)})


def christoffersen_ind(exceedances, alpha: Optional[float] = None, reject_at: float = 0.05) -> TestResult:
    """Christoffersen independence test against a first-order Markov alternative.

    H0: ``P(I_t = 1 | I_{t-1} = 0) = P(I_t = 1 | I_{t-1} = 1)``. Uses the
    transition counts n00, n01, n10, n11 over days 2..T;
    ``LR_ind = -2 ln[L(pi) / L(pi01, pi11)]``, chi-square with 1 degree of freedom.
    Degenerate cases (no exceedance, no transition out of state 1, ...) keep the
    statistic as computed (``0 * ln 0 = 0``) and explain it in ``notes``.

    Args:
        exceedances: 1-D bool (or 0/1) array, at least 2 days.
        alpha: not used (the independence test does not involve the coverage
            level); accepted so that all coverage tests share one call form.
        reject_at: significance level in (0, 1).
    Returns:
        TestResult(name="christoffersen_ind", statistic=LR_ind, df=1, p_value, ...,
        n_obs=T, n_exceed=x, details={"n00", "n01", "n10", "n11", "pi", "pi01", "pi11"}).
    """
    if alpha is not None:
        check_level("alpha", alpha)
    r = check_level("reject_at", reject_at)
    e = as_exceedances(exceedances, 2)
    c = transition_counts(e)
    lr, notes, pis = _lr_ind(c)
    p_value = float(stats.chi2.sf(lr, 1))
    return TestResult("christoffersen_ind", lr, 1, p_value, r, decide(p_value, r), int(e.shape[0]),
                      int(np.count_nonzero(e)), "; ".join(notes), {**c, **pis})


def christoffersen_cc(exceedances, alpha: float, reject_at: float = 0.05) -> TestResult:
    """Christoffersen conditional coverage test: ``LR_cc = LR_uc + LR_ind``, chi-square with 2 df.

    Convention (Christoffersen 1998): both parts are computed on the T-1
    transitions, i.e. conditional on the first observation. ``LR_uc`` here
    therefore uses days 2..T (it can differ slightly from :func:`kupiec_pof` on
    all T days), and ``LR_cc`` equals exactly
    ``-2 ln[L(p = 1 - alpha) / L(pi01, pi11)]`` on those transitions.

    Returns:
        TestResult(name="christoffersen_cc", statistic=LR_cc, df=2, p_value, ...,
        n_obs=T, n_exceed=x over all T days, details with "lr_uc", "lr_ind" and the counts).
    """
    a = check_level("alpha", alpha)
    r = check_level("reject_at", reject_at)
    e = as_exceedances(exceedances, 2)
    c = transition_counts(e)
    lr_ind, notes, pis = _lr_ind(c)
    tail = e[1:]
    lr_uc = _lr_uc(int(tail.shape[0]), int(np.count_nonzero(tail)), 1.0 - a)
    lr = lr_uc + lr_ind
    p_value = float(stats.chi2.sf(lr, 2))
    notes = ["LR_uc on days 2..T (conditional on the first observation, Christoffersen 1998)"] + notes
    return TestResult("christoffersen_cc", lr, 2, p_value, r, decide(p_value, r), int(e.shape[0]),
                      int(np.count_nonzero(e)), "; ".join(notes),
                      {"lr_uc": lr_uc, "lr_ind": lr_ind, **c, **pis})
