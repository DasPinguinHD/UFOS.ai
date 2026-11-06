"""PD model: logistic regression by maximum likelihood, discrimination and calibration tests, master scale.

* **Logistic regression** (:func:`fit_logit`): Newton-Raphson on the log-likelihood with an
  intercept; standard errors from the inverse observed information. Explicit errors:
  :class:`SeparationError` when the data are (quasi-)separated (the MLE does not exist: a fitted
  probability reaches 0 or 1 to machine precision, or the coefficients diverge), and
  :class:`ConvergenceError` when Newton does not converge in ``max_iter`` steps. Both subclass
  :class:`risklab.models.FitError`. No silent regularisation.
* **Discrimination:** ROC AUC as the Mann-Whitney statistic ``P(score_default > score_non-default)``
  with ties counted 1/2 (:func:`auc`); Gini = accuracy ratio = ``2 AUC - 1`` (for the CAP of a
  binary target the accuracy ratio equals the Gini of the ROC); the CAP curve (:func:`cap_curve`).
* **Calibration:** Hosmer-Lemeshow over ``g`` groups of equal size by predicted PD,
  ``HL = sum (O - E)^2 / (E (1 - E / n))``, chi-square with ``g - 2`` degrees of freedom
  (Hosmer and Lemeshow 2000); Brier score; per rating grade the one-sided **binomial test**
  ``P(D >= d | n, PD)`` and the **Jeffreys test** ``p = F_Beta(d + 1/2, n - d + 1/2)(PD)``
  (the posterior probability, under a Jeffreys prior, that the default rate is <= PD; a small
  value says the grade's PD is too low). The Jeffreys test as used in the ECB's instructions for
  reporting the validation results of IRB models (2019) - as recalled, ASSUMPTION; the formula
  is the standard Jeffreys-prior posterior.
* **Master scale** (:data:`MASTER_SCALE`): grade boundaries on the PD (synthetic, 9 grades); a
  grade's PD is the mean predicted PD of its members unless given.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import stats

from risklab.models.base import FitError


class SeparationError(FitError):
    """The data are (quasi-)separated: the logistic MLE does not exist."""


class ConvergenceError(FitError):
    """Newton-Raphson did not converge."""


@dataclass(frozen=True)
class LogitFit:
    coef: np.ndarray        # [const, b1, ..., bk]
    se: np.ndarray
    loglik: float
    iterations: int
    n: int

    def predict(self, X) -> np.ndarray:
        X = np.asarray(X, dtype=np.float64)
        return 1.0 / (1.0 + np.exp(-(self.coef[0] + X @ self.coef[1:])))


def fit_logit(X, y, max_iter: int = 50, tol: float = 1e-10, max_abs_eta: float = 35.0) -> LogitFit:
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if X.ndim != 2 or y.shape != (X.shape[0],):
        raise ValueError("X must be n x k and y of length n")
    if not np.all((y == 0) | (y == 1)):
        raise ValueError("y must be 0 / 1")
    if y.sum() == 0 or y.sum() == y.size:
        raise SeparationError("only one class in y: the MLE does not exist")
    A = np.column_stack([np.ones(X.shape[0]), X])
    if np.linalg.matrix_rank(A) < A.shape[1]:
        raise FitError("the design matrix is rank deficient")
    p0 = y.mean()
    b = np.zeros(A.shape[1])
    b[0] = np.log(p0 / (1 - p0))
    ll_old = -np.inf
    for it in range(1, max_iter + 1):
        eta = A @ b
        if np.max(np.abs(eta)) > max_abs_eta:
            raise SeparationError(f"linear predictor exceeds {max_abs_eta:g} in absolute value: (quasi-)separation")
        p = 1.0 / (1.0 + np.exp(-eta))
        W = p * (1 - p)
        H = A.T @ (A * W[:, None])
        g = A.T @ (y - p)
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            raise SeparationError("singular information matrix (separation)") from None
        b = b + step
        eta = A @ b
        ll = float(np.sum(y * eta - np.logaddexp(0.0, eta)))
        if np.max(np.abs(step)) < tol or abs(ll - ll_old) < tol * (1 + abs(ll)):
            p = 1.0 / (1.0 + np.exp(-eta))
            if np.min(np.minimum(p, 1 - p)) < 1e-12:
                raise SeparationError("fitted probabilities of 0 or 1: (quasi-)separation")
            H = A.T @ (A * (p * (1 - p))[:, None])
            se = np.sqrt(np.diag(np.linalg.inv(H)))
            return LogitFit(b, se, ll, it, int(y.size))
        ll_old = ll
    raise ConvergenceError(f"Newton-Raphson did not converge in {max_iter} iterations")


def auc(score, y) -> float:
    """ROC AUC = Mann-Whitney statistic (ties 1/2)."""
    s = np.asarray(score, dtype=np.float64)
    y = np.asarray(y).astype(bool)
    n1, n0 = int(y.sum()), int((~y).sum())
    if n1 == 0 or n0 == 0:
        raise ValueError("AUC needs both defaults and non-defaults")
    r = stats.rankdata(s)
    return float((r[y].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def gini(score, y) -> float:
    return 2.0 * auc(score, y) - 1.0


def cap_curve(score, y) -> Tuple[np.ndarray, np.ndarray]:
    """(share of borrowers, share of defaults captured), borrowers ordered from the riskiest score."""
    s = np.asarray(score, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    o = np.argsort(-s, kind="mergesort")
    x = np.concatenate([[0.0], np.arange(1, s.size + 1) / s.size])
    c = np.concatenate([[0.0], np.cumsum(y[o]) / y.sum()])
    return x, c


def brier(p, y) -> float:
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def hosmer_lemeshow(p, y, groups: int = 10, *, fitted_on_sample: bool) -> Dict[str, float]:
    """Hosmer-Lemeshow statistic over ``groups`` groups of equal size (sorted by ``p``) and its chi-square p-value.

    Degrees of freedom (Hosmer and Lemeshow, Applied Logistic Regression): ``groups - 2`` only when the
    probabilities were estimated by a model fitted on THIS sample (the development sample); for
    probabilities that were not estimated from the sample - the true PD, or a model applied out of
    time / out of sample - the statistic is compared with chi-square(``groups``). ``fitted_on_sample``
    is a required keyword so that every caller states which case applies (no default)."""
    p = np.asarray(p, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    o = np.argsort(p, kind="mergesort")
    stat = 0.0
    for idx in np.array_split(o, groups):
        n, E, O = idx.size, p[idx].sum(), y[idx].sum()
        stat += (O - E) ** 2 / (E * (1 - E / n))
    df = groups - 2 if fitted_on_sample else groups
    return {"statistic": float(stat), "df": df, "p_value": float(stats.chi2.sf(stat, df))}


MASTER_SCALE = (0.0, 0.001, 0.0025, 0.005, 0.01, 0.02, 0.04, 0.08, 0.15, 1.0)   # 9 grades (synthetic)


def grade_of(pd, bounds: Sequence[float] = MASTER_SCALE) -> np.ndarray:
    """Grade 1..len(bounds)-1 with ``bounds[g-1] <= PD < bounds[g]`` (the last grade includes 1)."""
    b = np.asarray(bounds)
    g = np.searchsorted(b, np.asarray(pd, dtype=np.float64), side="right")
    return np.clip(g, 1, b.size - 1)


def binomial_test(d: int, n: int, pd: float) -> float:
    """One-sided p-value P(D >= d) under Binomial(n, pd)."""
    return float(stats.binom.sf(d - 1, n, pd)) if d > 0 else 1.0


def jeffreys_test(d: int, n: int, pd: float) -> float:
    """p-value of the Jeffreys test: ``F_Beta(d + 1/2, n - d + 1/2)(pd)``."""
    return float(stats.beta.cdf(pd, d + 0.5, n - d + 0.5))


def grade_table(pd_pred, y, bounds: Sequence[float] = MASTER_SCALE, grade_pd: Optional[Sequence[float]] = None
                ) -> List[Dict[str, float]]:
    """Per grade: n, defaults, the grade PD (mean predicted PD, or ``grade_pd``), the observed default rate and
    the binomial / Jeffreys p-values."""
    g = grade_of(pd_pred, bounds)
    y = np.asarray(y)
    rows = []
    for k in range(1, len(bounds)):
        m = g == k
        n = int(m.sum())
        if n == 0:
            continue
        d = int(y[m].sum())
        pdk = float(grade_pd[k - 1]) if grade_pd is not None else float(np.mean(np.asarray(pd_pred)[m]))
        rows.append({"grade": k, "n": n, "defaults": d, "pd": pdk, "dr": d / n,
                     "binomial_p": binomial_test(d, n, pdk), "jeffreys_p": jeffreys_test(d, n, pdk)})
    return rows
