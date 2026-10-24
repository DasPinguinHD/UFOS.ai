"""StrategySpec v1 schema, documented defaults, canonical JSON, content hash
(SPEC §7.3).

The spec is a user-owned JSON document. ``with_defaults(spec)`` returns a
copy with every missing OPTIONAL block / key filled with its documented
default (spec defaults written into the stored spec — not data fallbacks).
``content_hash(spec)`` = sha256 hex of ``canonical_json`` of
``with_defaults(spec)`` without ``provenance``: a spec and its stored,
default-filled form hash the same, and key order / whitespace never matter.

Part of UFOS.ai, a proof-of-concept portfolio project.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict

SCHEMA_ID = "ufos.strategy/1"
CATEGORIES = ("momentum", "reversal", "trend", "volatility", "value", "carry", "allocation", "alpha101",
              "composite", "other")
TIMEFRAMES = ("1Day",)
MODES = ("long_only", "short_only", "long_short")
SELECTION_TYPES = ("top_n", "bottom_n", "top_bottom_n", "threshold", "all")
WEIGHTINGS = ("equal", "signal", "inverse_vol")
FREQUENCIES = ("daily", "weekly", "monthly")
ORDER_TYPES = ("market",)
TIMINGS = ("next_open", "next_close")
COST_MODELS = ("paper_broker", "custom")
ASSET_CLASSES = ("equity", "crypto")

# key -> required? (top level)
TOP_KEYS = {
    "schema": True, "name": True, "description": False, "tags": False, "category": False,
    "universe": True, "timeframe": False, "parameters": False, "features": False, "signal": True,
    "portfolio": False, "rebalance": False, "risk": False, "execution": False, "costs": False,
    "provenance": False,
    "benchmark": False,     # optional symbol (stage 3, SPEC §8.3); NOT default-filled -> old hashes unchanged
}
UNIVERSE_KEYS = ("preset", "asset_classes", "symbols")
PARAM_KEYS = ("value", "min", "max", "step", "integer", "description")
PORTFOLIO_KEYS = ("mode", "selection", "weighting", "vol_lookback", "max_weight", "gross_exposure")
SELECTION_KEYS = ("type", "n", "long_above", "short_below")
REBALANCE_KEYS = ("frequency",)
RISK_KEYS = ("stop_loss_pct", "take_profit_pct", "vol_target_annual", "max_drawdown_stop_pct")
EXECUTION_KEYS = ("order_type", "timing")
COSTS_KEYS = ("model", "fee_bps", "slippage_bps")

DEFAULTS: Dict[str, Any] = {
    "description": "",
    "tags": [],
    "category": "other",
    "timeframe": "1Day",
    "parameters": {},
    "features": {},
    "portfolio": {"mode": "long_only", "selection": {"type": "all"}, "weighting": "equal",
                  "vol_lookback": 60, "max_weight": 1.0, "gross_exposure": 1.0},
    "rebalance": {"frequency": "daily"},
    "risk": {"stop_loss_pct": None, "take_profit_pct": None, "vol_target_annual": None,
             "max_drawdown_stop_pct": None},
    "execution": {"order_type": "market", "timing": "next_open"},
    "costs": {"model": "paper_broker"},
}
DEFAULT_PRESET_ASSET_CLASSES = ["equity", "crypto"]


def with_defaults(spec: dict) -> dict:
    """Deep copy of ``spec`` with documented defaults filled in. Only fills
    missing keys of blocks that are dicts; invalid values are left for the
    validator (never silently replaced)."""
    out = copy.deepcopy(spec) if isinstance(spec, dict) else spec
    if not isinstance(out, dict):
        return out
    for key, default in DEFAULTS.items():
        if key not in out:
            out[key] = copy.deepcopy(default)
        elif isinstance(default, dict) and isinstance(out[key], dict) and key not in ("parameters", "features"):
            for k, v in default.items():
                if k not in out[key]:
                    out[key][k] = copy.deepcopy(v)
    uni = out.get("universe")
    if isinstance(uni, dict) and "preset" in uni and "asset_classes" not in uni:
        uni["asset_classes"] = list(DEFAULT_PRESET_ASSET_CLASSES)
    params = out.get("parameters")
    if isinstance(params, dict):
        for p in params.values():
            if isinstance(p, dict):
                p.setdefault("integer", False)
                p.setdefault("description", "")
    return out


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 characters kept (not \\u-escaped)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def user_part(spec: dict) -> dict:
    """The hashed part: the default-filled spec without ``provenance``."""
    full = with_defaults(spec)
    return {k: v for k, v in full.items() if k != "provenance"}


def content_hash(spec: dict) -> str:
    return hashlib.sha256(canonical_json(user_part(spec)).encode("utf-8")).hexdigest()


def pretty_json(spec: dict) -> str:
    """Readable text for the editor / export (2-space indent, stable order of
    the top-level blocks as in the contract)."""
    order = list(TOP_KEYS)
    ordered = {k: spec[k] for k in order if k in spec}
    ordered.update({k: v for k, v in spec.items() if k not in ordered})
    return json.dumps(ordered, indent=2, ensure_ascii=False, allow_nan=False)
