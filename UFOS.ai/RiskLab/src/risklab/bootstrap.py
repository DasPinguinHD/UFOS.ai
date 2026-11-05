"""Stationary block bootstrap (Politis and Romano 1994) with automatic block length.

Index scheme (:func:`stationary_bootstrap_indices`): a path of length h starts
at a uniform index; at every later step, with probability ``p = 1 / block_len``
a new block starts at a fresh uniform index, otherwise the path continues with
the next index, wrapping around circularly (index n follows n - 1 ... 0). Block
lengths are therefore geometric with mean ``block_len``.

Draw order (fixed, so results are reproducible): first the candidate start
indices ``rng.integers(0, n, (n_paths, h))``, then the uniforms deciding new
blocks. With ``block_len = 1`` every step starts a new block, so the indices are
exactly those of :func:`iid_bootstrap_indices` with the same seed.

Automatic block length (:func:`optimal_block_length`): Politis and White (2004),
"Automatic block-length selection for the dependent bootstrap", with the
correction of Patton, Politis and White (2009):

1. autocorrelations rho(k) of the demeaned series;
2. ``m_hat`` = the smallest lag m such that ``|rho(m + k)| < c * sqrt(log10(n) / n)``
   for k = 1..K_n, with c = 2 and ``K_n = max(5, ceil(sqrt(log10(n))))``,
   searched up to ``m_max = ceil(sqrt(n)) + K_n``; ``M = min(2 * max(m_hat, 1), m_max)``;
3. flat-top kernel ``lambda(s) = 1`` for |s| <= 1/2, ``2 (1 - |s|)`` for 1/2 < |s| <= 1, else 0;
4. ``G = sum_{|k|<=M} lambda(k/M) |k| R(k)``, ``g0 = sum_{|k|<=M} lambda(k/M) R(k)``
   (R = autocovariances);
5. stationary bootstrap (corrected): ``D_SB = 2 g0**2`` and
   ``b_SB = (2 G**2 / D_SB)**(1/3) * n**(1/3)``;
6. b is capped at ``b_max = ceil(min(3 sqrt(n), n / 3))`` (as in the paper) and
   floored at 1 (a block has at least one observation).

If ``g0 <= 0`` (the kernel estimate of the spectral density at frequency 0 is
not positive, e.g. for a strongly anti-persistent series) the formula is
undefined and :func:`optimal_block_length` raises :class:`BlockLengthError`
(a ValueError): choose
``block_len`` explicitly. There is no silent default.
"""
from __future__ import annotations

import math
from typing import Union

import numpy as np

from risklab.conventions import as_loss_array


class BlockLengthError(ValueError):
    """The automatic block length is undefined for this series (g0 <= 0); choose block_len explicitly."""


def iid_bootstrap_indices(n: int, length: int, n_paths: int, rng: np.random.Generator) -> np.ndarray:
    """iid bootstrap: uniform indices, shape (n_paths, length)."""
    return rng.integers(0, n, size=(n_paths, length))


def stationary_bootstrap_indices(n: int, length: int, block_len: float, n_paths: int,
                                 rng: np.random.Generator) -> np.ndarray:
    """Stationary-bootstrap indices, shape (n_paths, length); see the module docstring."""
    b = float(block_len)
    if not (np.isfinite(b) and b >= 1.0):
        raise ValueError("block_len must be >= 1")
    starts = rng.integers(0, n, size=(n_paths, length))
    new_block = rng.random((n_paths, length)) < 1.0 / b
    new_block[:, 0] = True
    idx = np.empty((n_paths, length), dtype=np.int64)
    idx[:, 0] = starts[:, 0]
    for t in range(1, length):
        idx[:, t] = np.where(new_block[:, t], starts[:, t], (idx[:, t - 1] + 1) % n)
    return idx


def _autocov(x: np.ndarray, max_lag: int) -> np.ndarray:
    n = x.shape[0]
    return np.array([np.dot(x[:n - k], x[k:]) / n for k in range(max_lag + 1)])


def optimal_block_length(x) -> dict:
    """Politis-White (2004) / Patton-Politis-White (2009) block length for the stationary bootstrap.

    Returns ``{"b_sb", "m_hat", "M", "b_max"}``; see the module docstring.
    """
    data = as_loss_array(x, "x")
    n = data.shape[0]
    if n < 10:
        raise ValueError("at least 10 observations are needed")
    e = data - data.mean()
    k_n = max(5, int(math.ceil(math.sqrt(math.log10(n)))))
    m_max = int(math.ceil(math.sqrt(n))) + k_n
    b_max = int(math.ceil(min(3.0 * math.sqrt(n), n / 3.0)))
    acv = _autocov(e, min(m_max + k_n, n - 1))
    if not acv[0] > 0:
        raise ValueError("the series has zero variance")
    rho = acv / acv[0]
    thr = 2.0 * math.sqrt(math.log10(n) / n)
    m_hat = None
    for m in range(0, m_max - k_n + 1):
        lags = rho[m + 1:m + k_n + 1]
        if lags.size == k_n and np.all(np.abs(lags) < thr):
            m_hat = m
            break
    if m_hat is None:
        m_hat = m_max
    big_m = min(2 * max(m_hat, 1), m_max, acv.size - 1)
    k = np.arange(-big_m, big_m + 1)
    s = np.abs(k) / big_m
    lam = np.where(s <= 0.5, 1.0, np.where(s <= 1.0, 2.0 * (1.0 - s), 0.0))
    r = acv[np.abs(k)]
    g = float(np.sum(lam * np.abs(k) * r))
    g0 = float(np.sum(lam * r))
    if not g0 > 0:
        raise BlockLengthError("automatic block length undefined: the spectral density estimate at frequency 0 "
                         f"is not positive (g0 = {g0:.3g}); pass block_len explicitly")
    d_sb = 2.0 * g0 * g0
    b = (2.0 * g * g / d_sb) ** (1.0 / 3.0) * n ** (1.0 / 3.0)
    b = float(min(max(b, 1.0), b_max))
    return {"b_sb": b, "m_hat": int(m_hat), "M": int(big_m), "b_max": b_max}


def resolve_block_len(x, block_len: Union[float, str]) -> float:
    """``block_len`` as a float; ``"auto"`` -> :func:`optimal_block_length` ``["b_sb"]``."""
    if isinstance(block_len, str):
        if block_len != "auto":
            raise ValueError("block_len must be a number >= 1 or 'auto'")
        return optimal_block_length(x)["b_sb"]
    b = float(block_len)
    if not (np.isfinite(b) and b >= 1.0):
        raise ValueError("block_len must be >= 1")
    return b


def bootstrap_h_sums(x, h: int, block_len: Union[float, str], n_paths: int, rng: np.random.Generator) -> np.ndarray:
    """h-day sums of stationary-bootstrap paths of the series x (n_paths values)."""
    data = as_loss_array(x, "x")
    b = resolve_block_len(data, block_len)
    idx = stationary_bootstrap_indices(data.shape[0], int(h), b, int(n_paths), rng)
    return data[idx].sum(axis=1)
