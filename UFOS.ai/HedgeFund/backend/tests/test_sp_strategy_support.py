"""Shared fakes for the stage-2 strategy tests (synthetic panels, fake
external features). No network, no database."""
from datetime import date, timedelta

import numpy as np

from strategy_platform.strategy.panel import Panel

EXT_CATALOGUE = [
    {"name": "macro.regime_score", "description": "fake", "point_in_time": True, "scope": "asset_class"},
    {"name": "calendar.event_risk", "description": "fake", "point_in_time": True, "scope": "global"},
    {"name": "test.leaky", "description": "fake", "point_in_time": False, "scope": "global"},
]


def business_days(start: date, n: int):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def synthetic_panel(n_days=400, symbols=None, seed=7, nan_rate=0.01, late_listing=True, start=date(2017, 1, 2)):
    """Random-walk OHLCV panel with isolated NaNs and (optionally) a symbol
    that starts trading later."""
    rng = np.random.default_rng(seed)
    symbols = list(symbols or ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"])
    S = len(symbols)
    rets = rng.normal(0.0003, 0.015, size=(n_days, S))
    close = 50.0 * np.exp(np.cumsum(rets, axis=0)) * (1 + np.arange(S))[None, :] / 2
    open_ = close * np.exp(rng.normal(0, 0.005, size=close.shape))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, size=close.shape)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, size=close.shape)))
    volume = rng.integers(100_000, 5_000_000, size=close.shape).astype(float)
    vwap = (high + low + close) / 3
    trades = np.round(volume / 100)
    fields = {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "vwap": vwap,
              "trade_count": trades}
    mask = rng.random(close.shape) < nan_rate
    if late_listing and S > 1:
        mask[: n_days // 4, S - 1] = True
    for k in fields:
        fields[k] = np.where(mask, np.nan, fields[k])
    return Panel(business_days(start, n_days), symbols, fields)


def fake_ext_values(panel, seed=3):
    """{name: raw values} like ExtFeature.values_for: per-symbol and global dicts with ISO keys,
    some dates missing (as-of alignment)."""
    rng = np.random.default_rng(seed)
    macro = {}
    for s in panel.symbols:
        macro[s] = {d.isoformat(): float(rng.uniform(-0.6, 0.6)) for i, d in enumerate(panel.dates) if i % 21 == 0}
    event = {d.isoformat(): float(rng.random() < 0.08) for d in panel.dates}
    return {"macro.regime_score": macro, "calendar.event_risk": event}
