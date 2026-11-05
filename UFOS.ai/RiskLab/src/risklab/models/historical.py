"""Historical simulation (stub until sheet 1 is derived with the user)."""
from __future__ import annotations

from typing import Sequence

import numpy as np

from risklab.conventions import not_derived_yet
from risklab.models.base import Forecast


class HistoricalSimulation:
    """Historical-simulation VaR / ES on the fitted window of past losses.

    Interface contract (to be implemented in v0.2, sheet 1):
    * ``fit(losses_past)``: 1-D float64 past losses (positive = loss), the
      window chosen by the engine; no parameters are estimated.
    * ``forecast(alphas)``: for each alpha in (0, 1), VaR_alpha and ES_alpha of
      the next day's loss, in loss units with the loss sign (positive for a
      typical portfolio). The quantile / tail-average definition used for a
      finite sample is part of sheet 1.
    """

    name = "historical_simulation"

    def fit(self, losses_past: np.ndarray) -> None:
        raise not_derived_yet()

    def forecast(self, alphas: Sequence[float]) -> Forecast:
        raise not_derived_yet()
