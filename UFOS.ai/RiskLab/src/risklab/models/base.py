"""The risk-model interface used by the walk-forward engine.

Contract (all models):
* ``fit(losses_past)`` receives a read-only 1-D float64 array of PAST daily
  losses only (positive = loss), oldest first. It returns None and keeps
  whatever state the model needs.
* ``forecast(alphas)`` returns the one-day-ahead :class:`Forecast` for the day
  directly after the fitted window (or after the last :meth:`update`), for
  every requested confidence level.
* Optional, for conditional (stateful) models: ``update(loss)`` receives the
  realised loss of the day just forecast, so that the model can roll its
  state (e.g. the GARCH variance) forward between refits with its parameters
  fixed. The engine calls it only with ``L_{t-1}`` before forecasting day t.
* Optional ``refit_every`` attribute: the model's preferred refit interval,
  used by :func:`risklab.engine.walk_forward` when its ``refit_every`` is None.
* Units: the same units as the losses (e.g. a fraction of portfolio value).
  VaR and ES are thresholds on the LOSS: for a typical portfolio they are
  positive numbers, and an exceedance is ``loss > var``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, Sequence, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class Forecast:
    """One-day-ahead risk forecast.

    Attributes:
        var: {alpha: VaR_alpha} for every requested alpha (loss units, loss sign).
        es: {alpha: ES_alpha}, or None if the model does not forecast ES.
        dist: optional predictive loss distribution of the day, an object with a
            vectorised ``ppf(u)`` (see :mod:`risklab.dists`); needed by the
            Acerbi-Szekely ES backtest.
    """

    var: Dict[float, float]
    es: Optional[Dict[float, float]] = None
    dist: Optional[Any] = None


class FitError(RuntimeError):
    """A model fit failed (e.g. the optimiser did not report convergence). Never silently replaced."""


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
