"""Tiny helpers to write library specs as Python data (no logic)."""
from __future__ import annotations

from typing import Optional

from ..schema import SCHEMA_ID

ETF_PRESET = {"preset": "liquid_etfs_crypto", "asset_classes": ["equity"]}
ALL_PRESET = {"preset": "liquid_etfs_crypto", "asset_classes": ["equity", "crypto"]}


def op(name: str, *args) -> dict:
    return {"op": name, "args": list(args)}


def f(name: str) -> dict:
    return {"field": name}


def p(name: str) -> dict:
    return {"param": name}


def r(name: str) -> dict:
    return {"ref": name}


def ext(name: str) -> dict:
    return {"ext": name}


def param(value, lo, hi, *, step=None, integer=True, description="") -> dict:
    out = {"value": value, "min": lo, "max": hi, "integer": integer, "description": description}
    if step is not None:
        out["step"] = step
    return out


def spec(*, name, category, description, tags, universe, signal, portfolio, frequency,
         features: Optional[dict] = None, parameters: Optional[dict] = None, risk: Optional[dict] = None,
         timing: str = "next_open") -> dict:
    pf = {"mode": "long_only", "selection": {"type": "all"}, "weighting": "equal", "vol_lookback": 60,
          "max_weight": 1.0, "gross_exposure": 1.0}
    pf.update(portfolio)
    rk = {"stop_loss_pct": None, "take_profit_pct": None, "vol_target_annual": None, "max_drawdown_stop_pct": None}
    rk.update(risk or {})
    return {
        "schema": SCHEMA_ID, "name": name, "description": description, "tags": list(tags),
        "category": category, "universe": universe, "timeframe": "1Day",
        "parameters": parameters or {}, "features": features or {}, "signal": signal, "portfolio": pf,
        "rebalance": {"frequency": frequency}, "risk": rk,
        "execution": {"order_type": "market", "timing": timing}, "costs": {"model": "paper_broker"},
    }


def entry(key, family, reference, holding_days, notes, the_spec) -> dict:
    return {"key": key, "spec": the_spec,
            "metadata": {"key": key, "family": family, "reference": reference,
                         "expected_holding_days": holding_days, "notes": notes}}
