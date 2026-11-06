"""Parameter grid and grid neighbours, re-implemented from the UFOS SPEC §9.3 / §9.4 (G5, G6).

Parameters are ``{name: {min, max, step, value, integer?}}`` (the UFOS strategy DSL fields;
every parameter here is used by the rule - the platform's "unused parameter" check has no
counterpart).

* Axis of a parameter: min, min + step, ... <= max (float tolerance 1e-9 step) plus its
  ``value``; sorted, duplicates removed; integer parameters rounded (values outside
  [min, max] after rounding dropped). One value only -> the parameter stays fixed.
* Configurations: the Cartesian product, lexicographic, axes sorted by name. More than
  ``max_configs`` (64) -> the product indices ``round(i (n - 1) / (max_configs - 1))``,
  i = 0..max_configs-1; if the base configuration (all ``value``\\ s) is missing, it replaces
  the pick nearest to it in product order (the smaller index on a tie).
* Neighbours (G6): every configuration other than the base whose EVERY varied parameter
  lies within +-20 % of the base value (``|v - b| <= 0.2 |b| + 1e-12``; base 0 -> 20 % of
  max - min).
"""
from __future__ import annotations

import math
from typing import Dict, List

MAX_CONFIGS = 64
NEIGHBOUR_TOLERANCE = 0.20
MAX_AXIS_VALUES = 10_000


def axis_values(p: dict) -> list:
    lo, hi, step = float(p["min"]), float(p["max"]), float(p["step"])
    if step <= 0:
        raise ValueError("step must be > 0")
    n = int(math.floor((hi - lo) / step + 1e-9)) + 1
    if n > MAX_AXIS_VALUES:
        raise ValueError(f"min/max/step give {n} values (at most {MAX_AXIS_VALUES})")
    vals = [lo + i * step for i in range(n)] + [float(p["value"])]
    if p.get("integer"):
        return sorted({int(round(v)) for v in vals if lo - 1e-9 <= round(v) <= hi + 1e-9})
    return sorted({round(v, 12) for v in vals})


def build(params: Dict[str, dict], max_configs: int = MAX_CONFIGS) -> dict:
    """{axes, fixed, configs (list of {name: value}), base, base_index, total, subsampled, picks}."""
    axes: Dict[str, list] = {}
    fixed: Dict[str, object] = {}
    for name in sorted(params):
        p = params[name]
        vals = axis_values(p) if "step" in p else [p["value"]]
        if len(vals) < 2:
            fixed[name] = p["value"]
        else:
            axes[name] = vals
    names = list(axes)
    base = {n: (int(round(params[n]["value"])) if params[n].get("integer") else round(float(params[n]["value"]), 12))
            for n in names}
    sizes = [len(axes[n]) for n in names]
    total = math.prod(sizes) if names else 1

    def nth(i: int) -> tuple:
        out = []
        for n, k in zip(reversed(names), reversed(sizes)):
            i, r = divmod(i, k)
            out.append(axes[n][r])
        return tuple(reversed(out))

    base_i = 0
    for n, k in zip(names, sizes):
        base_i = base_i * k + axes[n].index(base[n])
    if total <= max_configs:
        picks = list(range(total))
    else:
        picks = sorted({int(round(i * (total - 1) / (max_configs - 1))) for i in range(max_configs)})
        if base_i not in picks:
            k = min(range(len(picks)), key=lambda j: (abs(picks[j] - base_i), picks[j]))
            picks[k] = base_i
            picks.sort()
    configs = [dict(zip(names, nth(i))) for i in picks]
    return {"axes": axes, "fixed": fixed, "configs": configs, "base": base, "base_index": picks.index(base_i),
            "total": total, "subsampled": total > max_configs, "picks": picks}


def neighbours(grid: dict, params: Dict[str, dict], tolerance: float = NEIGHBOUR_TOLERANCE) -> List[int]:
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
