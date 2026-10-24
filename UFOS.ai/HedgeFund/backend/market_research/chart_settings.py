"""Per-symbol chart settings (SPEC §4.2 MR5 ``GET/PUT /research/chart-settings?symbol=``, §13 MR5 notes):
``{type, indicators, magnet, session}`` — indicators are spec strings validated (and normalised) by
``indicators.parse_list`` (≤ 8, ≤ 3 sub-panes). BLOCKING (SQLite).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

from strategy_platform.core.clock import iso, to_utc

from . import indicators as ind
from . import store as default_store
from .bars.series import BadParam
from .drawings import DrawingInvalid, symbol_of

TYPES = ("candles", "line", "area")
MAGNETS = ("off", "weak", "strong")
KEYS = ("type", "indicators", "magnet", "session")


def defaults(asset_class: str) -> dict:
    return {"type": "candles", "indicators": [], "magnet": "off", "session": "rth" if asset_class == "equity" else None}


def get(symbol: str, *, store=default_store) -> dict:
    inst = symbol_of(symbol)
    row = store.chart_settings_get(inst.symbol)
    if row is None:
        return {"symbol": inst.symbol, "saved": False, "settings": defaults(inst.asset_class), "updated_at": None}
    return {"symbol": inst.symbol, "saved": True, "settings": row["settings"], "updated_at": row["updated_at"]}


def validate(body, asset_class: str, symbol: str) -> dict:
    errors: Dict[str, str] = {}
    if not isinstance(body, dict):
        raise DrawingInvalid({"_": "a JSON object body is required"})
    for k in body:
        if k not in KEYS:
            errors[str(k)[:40]] = "unknown field"
    for k in KEYS:
        if k not in body:
            errors[k] = "required"
    if errors:
        raise DrawingInvalid(errors)
    out = {}
    if body["type"] not in TYPES:
        errors["type"] = "one of " + ", ".join(TYPES)
    if body["magnet"] not in MAGNETS:
        errors["magnet"] = "one of " + ", ".join(MAGNETS)
    s = body["session"]
    if asset_class == "crypto":
        if s is not None:
            errors["session"] = "must be null for crypto"
    elif s not in ("rth", "eth"):
        errors["session"] = "rth or eth"
    lst = body["indicators"]
    if not isinstance(lst, list) or not all(isinstance(x, str) for x in lst):
        errors["indicators"] = "a list of indicator spec strings"
    elif lst:
        try:
            out["indicators"] = [sp.key for sp in ind.parse_list(lst, symbol)]
        except BadParam as ex:
            errors["indicators"] = ex.user_message
    else:
        out["indicators"] = []
    if errors:
        raise DrawingInvalid(errors)
    out.update({"type": body["type"], "magnet": body["magnet"], "session": s})
    return {k: out[k] for k in KEYS}


def put(symbol: str, body, *, store=default_store, now=None) -> dict:
    inst = symbol_of(symbol)
    settings = validate(body, inst.asset_class, inst.symbol)
    t = iso(to_utc(now or datetime.now(timezone.utc)))
    store.chart_settings_put(inst.symbol, settings, t)
    return {"symbol": inst.symbol, "saved": True, "settings": settings, "updated_at": t}
