"""Bounded deterministic parameter search (SPEC §16.4).

For a new LLM variant: up to ``points`` extra configurations. Parameters used by the spec (the validator's
``unused_param`` warning excludes the rest — ``validation.grid.unused_params``) that have min/max/step, in
name order; candidates value − step, value + step (integer rounding as ``validation.grid``, clamped to
[min, max], ≠ value), taken in that order; a candidate whose logic hash is already known (``exclude``) is
skipped. Every candidate becomes a new version + one backtest (= one trial).
"""
from __future__ import annotations

import copy
import math
from typing import Iterable, List, Set


def logic_hash(spec: dict) -> str:
    """``db.logic_hash_of`` of the default-filled spec without its provenance block (= what the registry stores),
    so a raw AI spec and its stored version hash the same."""
    from ..storage import db
    from ..strategy import schema
    stored = {k: v for k, v in schema.with_defaults(dict(spec)).items() if k != "provenance"}
    return db.logic_hash_of(stored)


def _candidates_for(p: dict) -> List:
    lo, hi, step, value = float(p["min"]), float(p["max"]), float(p["step"]), p["value"]
    if not (math.isfinite(lo) and math.isfinite(hi) and math.isfinite(step)) or step <= 0:
        return []
    out = []
    for raw in (float(value) - step, float(value) + step):
        v = int(round(raw)) if p.get("integer") else round(raw, 12)
        if lo - 1e-9 <= v <= hi + 1e-9 and v != value and v not in out:
            out.append(v)
    return out


def candidates(spec: dict, points: int, exclude: Iterable[str] = ()) -> List[dict]:
    """-> up to ``points`` specs (deep copies with one parameter changed) in the deterministic order."""
    from ..validation import grid
    if points <= 0:
        return []
    params = spec.get("parameters") if isinstance(spec.get("parameters"), dict) else {}
    try:
        unused = grid.unused_params(spec)
    except Exception:     # noqa: BLE001 — an unvalidatable spec gets no search points
        return []
    seen: Set[str] = set(exclude)
    seen.add(logic_hash(spec))
    out: List[dict] = []
    for name in sorted(params):
        p = params[name]
        if name in unused or not isinstance(p, dict) or any(p.get(k) is None for k in ("min", "max", "step", "value")):
            continue
        try:
            vals = _candidates_for(p)
        except (TypeError, ValueError):
            continue
        for v in vals:
            cand = copy.deepcopy(spec)
            cand["parameters"][name]["value"] = v
            h = logic_hash(cand)
            if h in seen:
                continue
            seen.add(h)
            out.append({"spec": cand, "parameter": name, "value": v, "logic_hash": h})
            if len(out) >= points:
                return out
    return out
