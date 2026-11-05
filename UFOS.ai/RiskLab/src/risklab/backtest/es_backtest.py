"""Expected-shortfall backtest: Acerbi and Szekely (2014), test 2 ("Z2").

Loss-sign version used here (L = loss, positive = loss; VaR_t, ES_t > 0 for a
typical portfolio):

    Z2 = sum_t L_t * I_t / (T * (1 - alpha) * ES_t) - 1,   I_t = 1{L_t > VaR_t}.

Under H0 (the forecast distribution is the true one) E[Z2] = 0. Z2 > 0 means
the realised tail losses are larger than the predicted ES: risk is
UNDERESTIMATED, and the test is one-sided against that (reject for large Z2).

Sign convention against the paper: Acerbi and Szekely write P&L ``X_t = -L_t``
and ``Z2 = sum_t X_t I_t / (T alpha ES_t) + 1`` with their alpha = our 1 - alpha
and I_t = 1{X_t + VaR_t < 0}; they reject for NEGATIVE values. Our Z2 is minus
theirs, so "reject for large Z2" here is "reject for small Z2" there.

p-value by Monte Carlo under H0: for m = 1..M draw L_t^(m) from each day's
predictive distribution (``Forecast.dist``, via its ``ppf``; independent
across days and draws), recompute Z2^(m) with the SAME VaR_t and ES_t, and set
``p = (1 + #{Z2^(m) >= Z2}) / (1 + M)`` (a valid Monte-Carlo p-value: size at
most the nominal level).
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

from risklab.backtest.base import TestResult, check_level, decide
from risklab.conventions import as_loss_array, make_rng


def z2_statistic(losses, var, es, alpha: float) -> float:
    """``Z2 = sum L_t 1{L_t > VaR_t} / (T (1 - alpha) ES_t) - 1`` (loss sign)."""
    a = check_level("alpha", alpha)
    L, v, e = _arrays(losses, var, es)
    return float(np.sum(L * (L > v) / e) / (L.shape[0] * (1.0 - a)) - 1.0)


def _arrays(losses, var, es):
    L = as_loss_array(losses, "losses")
    v = as_loss_array(var, "var")
    e = as_loss_array(es, "es")
    if not (L.shape == v.shape == e.shape):
        raise ValueError("losses, var and es must have the same length")
    if np.any(e <= 0):
        raise ValueError("ES must be > 0 for the Z2 statistic (loss sign)")
    return L, v, e


def _simulate_tail_sums(dists: Sequence, var: np.ndarray, es: np.ndarray, m_sim: int,
                        rng: np.random.Generator) -> np.ndarray:
    """sum_t L_t^(m) I_t^(m) / ES_t for m = 1..M (one uniform matrix; exact tail-only draws where possible)."""
    T = var.shape[0]
    u = rng.random((m_sim, T))
    acc = np.zeros(m_sim)
    groups = {}
    for t, d in enumerate(dists):
        key = d.family if hasattr(d, "family") and hasattr(d, "cdf") else None
        groups.setdefault(key, []).append(t)
    for key, idx in groups.items():
        idx = np.asarray(idx)
        if key is None:
            # generic path: full quantile transform day by day
            for t in idx:
                lt = np.asarray(dists[t].ppf(u[:, t]), dtype=np.float64)
                acc += np.where(lt > var[t], lt, 0.0) / es[t]
            continue
        # location-scale family: L > VaR  <=>  u > F(VaR); only those draws need the quantile
        loc = np.array([dists[t].loc for t in idx])
        scale = np.array([dists[t].scale for t in idx])
        cut = np.array([float(dists[t].cdf(var[t])) for t in idx])
        sub = u[:, idx]
        mask = sub > cut
        rows, cols = np.nonzero(mask)
        if rows.size:
            z = dists[idx[0]].std_ppf(sub[rows, cols])
            lt = loc[cols] + scale[cols] * z
            keep = lt > var[idx][cols]  # guards the boundary u == F(VaR) exactly
            np.add.at(acc, rows[keep], lt[keep] / es[idx][cols][keep])
    return acc


def acerbi_szekely_z2(losses, var, es, dists: Sequence, alpha: float, *, seed, m_sim: int = 1000,
                      reject_at: float = 0.05) -> TestResult:
    """Acerbi-Szekely Z2 test of the ES forecasts (one-sided, against underestimation).

    Args:
        losses: realised daily losses L_t (positive = loss).
        var, es: the VaR_t and ES_t forecasts at level alpha for the same days.
        dists: per-day predictive distributions (objects with ``ppf``; see
            :mod:`risklab.dists`), e.g. ``BacktestFrame.dists``.
        alpha: VaR / ES confidence level in (0, 1).
        seed: int seed or Generator for the Monte-Carlo p-value (required).
        m_sim: number of Monte-Carlo draws M (>= 99).
        reject_at: significance level.
    Returns:
        TestResult(name="acerbi_szekely_z2", statistic=Z2, df=None, p_value
        (Monte Carlo), reject_at, decision, n_obs=T, n_exceed,
        details={"z2", "m_sim", "mean_z2_h0"}).
    """
    a = check_level("alpha", alpha)
    r = check_level("reject_at", reject_at)
    L, v, e = _arrays(losses, var, es)
    if dists is None or len(dists) != L.shape[0]:
        raise ValueError("one predictive distribution per day is required (Forecast.dist)")
    if isinstance(m_sim, bool) or not isinstance(m_sim, (int, np.integer)) or m_sim < 99:
        raise ValueError("m_sim must be an int >= 99")
    rng = make_rng(seed)
    T = L.shape[0]
    z2 = float(np.sum(L * (L > v) / e) / (T * (1.0 - a)) - 1.0)
    sims = _simulate_tail_sums(dists, v, e, int(m_sim), rng) / (T * (1.0 - a)) - 1.0
    p_value = float((1 + np.count_nonzero(sims >= z2)) / (1 + m_sim))
    return TestResult("acerbi_szekely_z2", z2, None, p_value, r, decide(p_value, r), T,
                      int(np.count_nonzero(L > v)),
                      "Z2 > 0: realised tail losses exceed the predicted ES (underestimation); one-sided",
                      {"z2": z2, "m_sim": int(m_sim), "mean_z2_h0": float(np.mean(sims))})


def z2_from_frame(frame, alpha: float, *, seed, m_sim: int = 1000, reject_at: float = 0.05) -> TestResult:
    """:func:`acerbi_szekely_z2` on a :class:`~risklab.engine.BacktestFrame` (needs ES and dists)."""
    a = float(alpha)
    if frame.es is None or frame.dists is None:
        raise ValueError("the frame needs ES forecasts and predictive distributions")
    return acerbi_szekely_z2(frame.loss, frame.var[a], frame.es[a], frame.dists, a, seed=seed,
                             m_sim=m_sim, reject_at=reject_at)
