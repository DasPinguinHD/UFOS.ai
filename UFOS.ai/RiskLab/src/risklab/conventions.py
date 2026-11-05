"""Shared conventions of the Risk Lab (binding for every module and test).

* ``L`` is the loss. Positive = loss, negative = gain. Returns ``r`` become
  losses by ``L = -r`` (see :func:`returns_to_losses`).
* ``alpha`` is the confidence level, strictly inside (0, 1), e.g. 0.99.
  A VaR at level ``alpha`` is a threshold on the loss ``L`` (a positive number
  for a typical portfolio), never on the return.
* Horizon: one trading day. Scaling to longer horizons is a sheet-1 topic.
* Arrays are one-dimensional float64 numpy arrays.
* Every random function takes ``seed`` (an int) or a ``numpy.random.Generator``.
  The same int seed gives the same output, byte for byte, within the same
  numpy version (PCG64 via ``numpy.random.default_rng``; numpy does not promise
  identical streams across versions, so exports record ``numpy_version``).
"""
from __future__ import annotations

from typing import Iterable, Optional, Union

import numpy as np

#: The message every not-yet-derived formula raises (derive-first rule).
SHEET1_MESSAGE = "sheet 1 — implemented together with the user"

SeedLike = Union[int, np.random.Generator, None]


def not_derived_yet() -> NotImplementedError:
    """The exception a sheet-1 stub raises (``raise not_derived_yet()``)."""
    return NotImplementedError(SHEET1_MESSAGE)


def make_rng(seed: SeedLike) -> np.random.Generator:
    """A ``numpy.random.Generator`` from an int seed or an existing Generator.

    ``None`` is refused on purpose: every random result in the lab must be
    reproducible.
    """
    if isinstance(seed, np.random.Generator):
        return seed
    if isinstance(seed, (bool, np.bool_)) or not isinstance(seed, (int, np.integer)):
        raise ValueError("seed must be an int or a numpy.random.Generator")
    if seed < 0:
        raise ValueError("seed must be >= 0")
    return np.random.default_rng(int(seed))


def seed_for_meta(seed: SeedLike) -> Optional[int]:
    """The int seed for result metadata, or None when a Generator was passed."""
    return None if isinstance(seed, np.random.Generator) else int(seed)


def as_loss_array(values, name: str = "losses") -> np.ndarray:
    """A 1-D float64 copy of ``values``; refuses empty input and NaN / inf."""
    arr = np.array(values, dtype=np.float64, copy=True)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    if arr.size == 0:
        raise ValueError(f"{name} must not be empty")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must be finite (no NaN / inf)")
    return arr


def returns_to_losses(returns) -> np.ndarray:
    """Convert returns to losses: ``L = -r`` (float64, new array)."""
    return -as_loss_array(returns, "returns")


def check_alpha(alpha) -> float:
    """``alpha`` as a float, checked to lie strictly inside (0, 1)."""
    a = float(alpha)
    if not (0.0 < a < 1.0):
        raise ValueError(f"alpha must lie strictly inside (0, 1), got {alpha!r}")
    return a


def check_alphas(alphas: Iterable[float]) -> tuple:
    """Checked, de-duplicated confidence levels in ascending order."""
    vals = sorted({check_alpha(a) for a in alphas})
    if not vals:
        raise ValueError("at least one alpha is required")
    return tuple(vals)
