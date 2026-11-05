"""Walk-forward (rolling-window) forecasting engine.

For every day ``t >= window`` the model forecasts day ``t`` from data before t
only. Look-ahead is impossible by construction:

* at a refit day the model is created fresh and ``fit()`` receives a read-only
  COPY of ``losses[t-window:t]``; it never sees the full series or any index >= t;
* on the days between refits a stateful model (one with an ``update`` method,
  e.g. GARCH or EWMA) receives exactly one float, ``L_{t-1}``, before it
  forecasts day t; models without ``update`` forecast again from their last fit
  (the v0.1 / v0.2 behaviour).

:func:`walk_forward_h` backtests h-day forecasts on NON-overlapping h-day
blocks: overlapping blocks would share days, so their exceedances would be
dependent and the coverage tests (which assume independent trials) would be
invalid. The price is a small T: n days give ``floor((n - window) / h)`` blocks
(e.g. 1000 days after the window -> 100 ten-day blocks).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol, Sequence, Tuple

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
        dists: per-day predictive distributions (``Forecast.dist``) as a tuple, or
            None if the model provides none (not exported).
    All arrays are read-only.
    """

    t: np.ndarray
    loss: np.ndarray
    var: Dict[float, np.ndarray]
    es: Optional[Dict[float, np.ndarray]]
    exceedance: Dict[float, np.ndarray]
    meta: dict = field(default_factory=dict)
    dists: Optional[Tuple[Any, ...]] = None

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
                 alphas: Sequence[float], refit_every: Optional[int] = None) -> BacktestFrame:
    """Rolling-window one-day-ahead forecasts of VaR (and ES) for ``losses``.

    Args:
        losses: 1-D daily losses (positive = loss), oldest first.
        model_factory: zero-argument callable returning a fresh :class:`RiskModel`.
            A fresh model is created at every refit, so no state leaks between fits.
        window: length of the estimation window (>= 1, < len(losses)).
        alphas: confidence levels in (0, 1).
        refit_every: refit on the latest window every k days (>= 1). None (default)
            means the ``refit_every`` attribute of the first model the factory
            returns (the one fitted on day ``window``), else 1; no extra model is
            created to read it.
            Between refits a model with ``update(loss)`` is rolled forward with
            ``L_{t-1}``; a model without it forecasts again from its last fit.
    Returns:
        A :class:`BacktestFrame` for days ``window .. n-1``.
    """
    data = as_loss_array(losses)
    n = data.shape[0]
    window = _check_int("window", window, 1)
    if refit_every is not None:
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
    dists = []
    for i, t in enumerate(days):
        if i == 0 or i % refit_every == 0:
            past = data[t - window:t].copy()
            past.setflags(write=False)
            model = model_factory()
            name = getattr(model, "name", type(model).__name__)
            if refit_every is None:  # first model: read its preferred interval
                refit_every = _check_int("refit_every", getattr(model, "refit_every", 1), 1)
            model.fit(past)
        elif hasattr(model, "update"):
            model.update(float(data[t - 1]))  # only yesterday's realised loss
        fc = model.forecast(alphas)
        v, e = _read_forecast(fc, alphas, int(t))
        dists.append(getattr(fc, "dist", None))
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
        dists=None if any(d is None for d in dists) else tuple(dists),
    )


class HorizonForecaster(Protocol):
    """An h-day VaR / ES forecaster: ``fit(losses_past)``, then ``forecast_h(alphas, h)``.

    ``forecast_h`` returns a :class:`Forecast` for the SUM of the next h daily
    losses (see :mod:`risklab.horizon` for implementations).
    """

    name: str

    def fit(self, losses_past: np.ndarray) -> None:
        ...

    def forecast_h(self, alphas: Sequence[float], h: int) -> Forecast:
        ...


def walk_forward_h(losses, forecaster_factory: Callable[[], HorizonForecaster], window: int,
                   alphas: Sequence[float], h: int) -> BacktestFrame:
    """h-day forecasts on NON-overlapping blocks.

    Origins ``t = window, window + h, window + 2h, ...`` while ``t + h <= n``. At
    each origin a fresh forecaster is fitted on a read-only copy of
    ``losses[t-window:t]`` and forecasts the h-day loss
    ``losses[t] + ... + losses[t+h-1]``. The frame's ``t`` holds the origins and
    ``loss`` the realised h-day sums; ``meta["h"]`` = h.
    """
    data = as_loss_array(losses)
    n = data.shape[0]
    window = _check_int("window", window, 1)
    h = _check_int("h", h, 1)
    alphas = check_alphas(alphas)
    origins = np.arange(window, n - h + 1, h, dtype=np.int64)
    if origins.size == 0:
        raise ValueError("no complete h-day block after the window")
    var = {a: np.empty(origins.shape[0]) for a in alphas}
    es: Optional[Dict[float, np.ndarray]] = None
    has_es: Optional[bool] = None
    name = None
    for i, t in enumerate(origins):
        past = data[t - window:t].copy()
        past.setflags(write=False)
        model = forecaster_factory()
        name = getattr(model, "name", type(model).__name__)
        model.fit(past)
        v, e = _read_forecast(model.forecast_h(alphas, h), alphas, int(t))
        if has_es is None:
            has_es = e is not None
            if has_es:
                es = {a: np.empty(origins.shape[0]) for a in alphas}
        elif has_es != (e is not None):
            raise ValueError(f"the forecaster returned ES on some origins but not on others (origin {t})")
        for a in alphas:
            var[a][i] = v[a]
            if es is not None:
                es[a][i] = e[a]
    sums = np.array([data[t:t + h].sum() for t in origins])
    exceed = {a: sums > var[a] for a in alphas}
    return BacktestFrame(t=origins, loss=sums, var=var, es=es, exceedance=exceed,
                         meta={"model": name, "window": window, "h": h, "alphas": list(alphas),
                               "n_input": n, "blocks": "non-overlapping"})
