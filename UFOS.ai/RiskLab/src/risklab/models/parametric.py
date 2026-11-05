"""Parametric normal and Student-t models (stubs until sheet 1 is derived with the user)."""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from risklab.conventions import not_derived_yet
from risklab.models.base import Forecast


class NormalModel:
    """Normal VaR / ES with mean and standard deviation estimated from the window.

    Interface contract (to be implemented in v0.2, sheet 1):
    * ``fit(losses_past)``: 1-D float64 past losses (positive = loss);
      estimates the loss mean and standard deviation (estimator choice: sheet 1).
    * ``forecast(alphas)``: for each alpha in (0, 1), one-day VaR_alpha and
      ES_alpha of the loss, in loss units with the loss sign.
    """

    name = "normal"

    def fit(self, losses_past: np.ndarray) -> None:
        raise not_derived_yet()

    def forecast(self, alphas: Sequence[float]) -> Forecast:
        raise not_derived_yet()


class StudentTModel:
    """Student-t VaR / ES (location-scale t, scaled to the window's standard deviation).

    Interface contract (to be implemented in v0.2, sheet 1):
    * ``__init__(nu=None)``: fixed degrees of freedom (> 2), or None to estimate them.
    * ``fit(losses_past)``: 1-D float64 past losses (positive = loss).
    * ``forecast(alphas)``: for each alpha in (0, 1), one-day VaR_alpha and
      ES_alpha of the loss, in loss units with the loss sign.
    """

    name = "student_t"

    def __init__(self, nu: Optional[float] = None):
        if nu is not None and not float(nu) > 2.0:
            raise ValueError("nu must be > 2")
        self.nu = None if nu is None else float(nu)

    def fit(self, losses_past: np.ndarray) -> None:
        raise not_derived_yet()

    def forecast(self, alphas: Sequence[float]) -> Forecast:
        raise not_derived_yet()
