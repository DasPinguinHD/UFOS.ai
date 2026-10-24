"""Parameter grid of a spec (SPEC §9.3 step 3, §9.4 G5/G6).

Axes: every USED parameter with ``min``, ``max`` and ``step`` gets the values
min, min + step, min + 2 step, … <= max (float tolerance 1e-9 x step) plus
its spec ``value`` (it need not lie on the step lattice), sorted, duplicates
removed; integer parameters are rounded to integers (then deduplicated).
Parameters without ``step``, and parameters the validator reports as unused
(warning ``unused_param`` — varying them would only add identical copies),
stay fixed at their ``value`` (listed in ``fixed`` with the reason).

Configurations: the Cartesian product of the axes in lexicographic order
(axes sorted by name). More than ``MAX_CONFIGS`` (64) -> evenly subsampled:
indices round(i * (n - 1) / (MAX_CONFIGS - 1)), i = 0..63 (first and last
kept); the BASE configuration (all spec values) is always included — if
the even pick missed it, it replaces the picked configuration nearest to it
in product order. ``base_index`` = its position in ``configs``.

Neighbours (G6): configurations other than the base where EVERY varied
parameter lies within ±20 % of the base value: |v - base| <= 0.2 x |base|
(a base value of 0 uses 20 % of the parameter's range max - min instead).
Fixed parameters are equal in every configuration.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import copy
import math
from typing import Dict, List, Optional

MAX_CONFIGS = 64
NEIGHBOUR_TOLERANCE = 0.20
MAX_AXIS_VALUES = 10_000          # guards absurd min/max/step combinations


def unused_params(spec: dict) -> set:
    """Parameter names the validator flags as unused (warning ``unused_param``)."""
    from ..strategy import validator
    res = validator.validate(spec)
    out = set()
    for w in res.warnings:
        if (w.get("code") if isinstance(w, dict) else getattr(w, "code", None)) == "unused_param":
            path = w.get("path") if isinstance(w, dict) else getattr(w, "path", "")
            name = str(path).rsplit(".", 1)[-1].strip("[]'\"")
            out.add(name)
    return out


def axis_values(p: dict) -> List:
    lo, hi, step, value = float(p["min"]), float(p["max"]), float(p["step"]), p["value"]
    if step <= 0:
        raise ValueError("step must be > 0")
    n = int(math.floor((hi - lo) / step + 1e-9)) + 1
    if n > MAX_AXIS_VALUES:
        raise ValueError(f"min/max/step give {n} values (at most {MAX_AXIS_VALUES})")
    vals = [lo + i * step for i in range(n)] + [float(value)]
    if p.get("integer"):
        vals = sorted({int(round(v)) for v in vals if lo - 1e-9 <= round(v) <= hi + 1e-9})
    else:
        vals = sorted({round(v, 12) for v in vals})
    return vals


def build(spec: dict, max_configs: int = MAX_CONFIGS) -> dict:
    """{axes: {name: [values]}, fixed: {name: {value, reason}}, configs: [{name: value}], base: {name: value},
    base_index, total (product size), subsampled: bool, max_configs}."""
    params = spec.get("parameters") or {}
    unused = unused_params(spec) if params else set()
    axes: Dict[str, list] = {}
    fixed: Dict[str, dict] = {}
    for name in sorted(params):
        p = params[name]
        if name in unused:
            fixed[name] = {"value": p["value"], "reason": "unused parameter"}
        elif "step" not in p:
            fixed[name] = {"value": p["value"], "reason": "no step (add min/max/step to vary it)"}
        else:
            vals = axis_values(p)
            if len(vals) < 2:
                fixed[name] = {"value": p["value"], "reason": "min = max"}
            else:
                axes[name] = vals
    names = list(axes)
    base = {n: (int(round(params[n]["value"])) if params[n].get("integer") else round(float(params[n]["value"]), 12))
            for n in names}
    total = 1
    for n in names:
        total *= len(axes[n])
    base_tuple = tuple(base[n] for n in names)

    def nth(i: int) -> tuple:                     # i-th product element (lexicographic) without materialising
        out = []
        for n in reversed(names):
            i, r = divmod(i, len(axes[n]))
            out.append(axes[n][r])
        return tuple(reversed(out))

    def index_of(t: tuple) -> int:
        i = 0
        for n, v in zip(names, t):
            i = i * len(axes[n]) + axes[n].index(v)
        return i

    base_i = index_of(base_tuple) if names else 0
    if total <= max_configs:
        picks = list(range(total))
    else:
        picks = sorted({int(round(i * (total - 1) / (max_configs - 1))) for i in range(max_configs)})
        if base_i not in picks:
            nearest = min(range(len(picks)), key=lambda k: (abs(picks[k] - base_i), picks[k]))
            picks[nearest] = base_i
            picks.sort()
    configs = [dict(zip(names, nth(i))) for i in picks]
    return {"axes": axes, "fixed": fixed, "configs": configs, "base": base, "base_index": picks.index(base_i),
            "total": total, "subsampled": total > max_configs, "max_configs": max_configs}


def spec_with(spec: dict, config: dict) -> dict:
    """A deep copy of ``spec`` with the parameter values of ``config``."""
    out = copy.deepcopy(spec)
    for name, v in config.items():
        out["parameters"][name]["value"] = v
    return out


def neighbours(grid: dict, spec: dict, tolerance: float = NEIGHBOUR_TOLERANCE) -> List[int]:
    """Indices (into grid.configs) of the base configuration's neighbours (module docstring)."""
    params = spec.get("parameters") or {}
    base = grid["base"]
    out = []
    for i, cfg in enumerate(grid["configs"]):
        if i == grid["base_index"]:
            continue
        ok = True
        for n, v in cfg.items():
            b = base[n]
            tol = tolerance * abs(b) if b != 0 else tolerance * (float(params[n]["max"]) - float(params[n]["min"]))
            if abs(float(v) - float(b)) > tol + 1e-12:
                ok = False
                break
        if ok:
            out.append(i)
    return out
