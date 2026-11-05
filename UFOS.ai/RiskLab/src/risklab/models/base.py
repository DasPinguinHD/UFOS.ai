"""The risk-model interface used by the walk-forward engine.

Contract (all models):
* ``fit(losses_past)`` receives a read-only 1-D float64 array of PAST daily
  losses only (positive = loss), oldest first. It returns None and keeps
  whatever state the model needs.
* ``forecast(alphas)`` returns the one-day-ahead :class:`Forecast` for the day
  directly after the fitted window, for every requested confidence level.
* Units: the same units as the losses (e.g. a fraction of portfolio value).
  VaR and ES are thresholds on the LOSS: for a typical portfolio they are
  positive numbers, and an exceedance is ``loss > var``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Protocol, Sequence, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class Forecast:
    """One-day-ahead risk forecast.

    Attributes:
        var: {alpha: VaR_alpha} for every requested alpha (loss units, loss sign).
        es: {alpha: ES_alpha}, or None if the model does not forecast ES.
    """

    var: Dict[float, float]
    es: Optional[Dict[float, float]] = None


@runtime_checkable
class RiskModel(Protocol):
    """Structural interface of a one-day VaR / ES model."""

    name: str

    def fit(self, losses_past: np.ndarray) -> None:
        """Fit on past losses only (read-only float64 array, oldest first)."""
        ...

    def forecast(self, alphas: Sequence[float]) -> Forecast:
        """Forecast VaR (and optionally ES) for the next day at each alpha."""
        ...
