"""Walk-forward (rolling-window) forecasting engine.

For every day ``t >= window`` the model is fitted on ``losses[t-window:t]``
only and forecasts day ``t``. Look-ahead is impossible by construction: the
model receives a read-only COPY of the past window and never sees the full
series or any index >= t.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Sequence

import numpy as np

from risklab.conventions import as_loss_array, check_alphas
from risklab.models.base import Forecast, RiskModel


@dataclass(frozen=True)
class BacktestFrame:
    """Forecasts and realised losses of a walk-forward run (one row per forecast day).

    Attributes:
        t: int64 day indices into the input series (``window .. n-1``).
        loss: realised loss of day t.
        var: {alpha: float64 array} VaR forecast for day t.
        es: {alpha: float64 array} ES forecast for day t, or None if the model gives no ES.
        exceedance: {alpha: bool array} ``loss > var``.
        meta: model name, window, refit_every, alphas.
    All arrays are read-only.
    """

    t: np.ndarray
    loss: np.ndarray
    var: Dict[float, np.ndarray]
    es: Optional[Dict[float, np.ndarray]]
    exceedance: Dict[float, np.ndarray]
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        arrays = [self.t, self.loss, *self.var.values(), *self.exceedance.values()]
        if self.es is not None:
            arrays.extend(self.es.values())
        for arr in arrays:
            arr.setflags(write=False)

    @property
    def alphas(self) -> tuple:
        return tuple(sorted(self.var))

    def n_exceed(self, alpha: float) -> int:
        return int(np.count_nonzero(self.exceedance[float(alpha)]))

    def __len__(self) -> int:
        return int(self.t.shape[0])


def _check_int(name: str, value, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < minimum:
        raise ValueError(f"{name} must be an int >= {minimum}")
    return int(value)


def _read_forecast(fc, alphas: tuple, day: int):
    if not isinstance(fc, Forecast):
        raise TypeError(f"forecast() must return a Forecast (day {day})")
    var = {}
    es = None if fc.es is None else {}
    for a in alphas:
        if a not in fc.var:
            raise ValueError(f"forecast for day {day} has no VaR for alpha={a}")
        v = float(fc.var[a])
        if not np.isfinite(v):
            raise ValueError(f"VaR for day {day}, alpha={a} is not finite")
        var[a] = v
        if es is not None:
            if a not in fc.es:
                raise ValueError(f"forecast for day {day} has no ES for alpha={a}")
            e = float(fc.es[a])
            if not np.isfinite(e):
                raise ValueError(f"ES for day {day}, alpha={a} is not finite")
            es[a] = e
    return var, es


def walk_forward(losses, model_factory: Callable[[], RiskModel], window: int,
                 alphas: Sequence[float], refit_every: int = 1) -> BacktestFrame:
    """Rolling-window one-day-ahead forecasts of VaR (and ES) for ``losses``.

    Args:
        losses: 1-D daily losses (positive = loss), oldest first.
        model_factory: zero-argument callable returning a fresh :class:`RiskModel`.
            A fresh model is created at every refit, so no state leaks between fits.
        window: length of the estimation window (>= 1, < len(losses)).
        alphas: confidence levels in (0, 1).
        refit_every: refit on the latest window every k days (>= 1); between refits
            the last fitted model forecasts again (it still saw only past data).
    Returns:
        A :class:`BacktestFrame` for days ``window .. n-1``.
    """
    data = as_loss_array(losses)
    n = data.shape[0]
    window = _check_int("window", window, 1)
    refit_every = _check_int("refit_every", refit_every, 1)
    if window >= n:
        raise ValueError("window must be smaller than the number of losses")
    alphas = check_alphas(alphas)

    days = np.arange(window, n, dtype=np.int64)
    var = {a: np.empty(days.shape[0]) for a in alphas}
    es: Optional[Dict[float, np.ndarray]] = None
    has_es: Optional[bool] = None
    model = None
    name = None
    for i, t in enumerate(days):
        if i % refit_every == 0:
            past = data[t - window:t].copy()
            past.setflags(write=False)
            model = model_factory()
            name = getattr(model, "name", type(model).__name__)
            model.fit(past)
        v, e = _read_forecast(model.forecast(alphas), alphas, int(t))
        if has_es is None:
            has_es = e is not None
            if has_es:
                es = {a: np.empty(days.shape[0]) for a in alphas}
        elif has_es != (e is not None):
            raise ValueError(f"the model returned ES on some days but not on others (day {t})")
        for a in alphas:
            var[a][i] = v[a]
            if es is not None:
                es[a][i] = e[a]

    realised = data[window:].copy()
    exceed = {a: realised > var[a] for a in alphas}
    return BacktestFrame(
        t=days, loss=realised, var=var, es=es, exceedance=exceed,
        meta={"model": name, "window": window, "refit_every": refit_every, "alphas": list(alphas),
              "n_input": n},
    )
