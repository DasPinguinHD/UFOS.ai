"""Selection + diversity (SPEC §16.3), deterministic.

- rank: score desc, Sharpe desc, slot asc; variants without a score last (failed, not scorable, duplicates).
- diversity (b): daily-return correlation ≥ CORRELATION_DUPLICATE over ≥ MIN_COMMON_DAYS common valued days
  (from the run cards' equity curves) with a better-ranked variant -> duplicate (never elite or finalist).
  (Diversity (a), identical logic hash, is enforced before the backtest by the loop.)
- k = max(1, round(N / 3)) elites, c = max(1, floor(N / 3)) culled (unscorable / failed / duplicates first,
  then the worst), the rest are refined. With fewer usable variants the elites shrink first.
- finalists: the best ≤ k non-duplicate variants with score > 0 whose pre-checks all passed.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

CORRELATION_DUPLICATE = 0.95
MIN_COMMON_DAYS = 60


def k_elites(n: int) -> int:
    return max(1, int(math.floor(n / 3.0 + 0.5)))


def c_culled(n: int) -> int:
    return max(1, n // 3)


def returns_of(curve: Optional[dict]) -> Dict[str, float]:
    """date -> daily return between consecutive VALUED points of a run card curve."""
    if not isinstance(curve, dict):
        return {}
    dates, eq = curve.get("dates") or [], curve.get("equity") or []
    out, prev = {}, None
    for d, e in zip(dates, eq):
        if not isinstance(e, (int, float)) or isinstance(e, bool) or not math.isfinite(float(e)) or e <= 0:
            continue
        if prev is not None:
            out[str(d)] = float(e) / prev - 1.0
        prev = float(e)
    return out


def correlation(a: Dict[str, float], b: Dict[str, float]) -> Optional[float]:
    common = sorted(set(a) & set(b))
    if len(common) < MIN_COMMON_DAYS:
        return None
    x, y = [a[d] for d in common], [b[d] for d in common]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxx = sum((v - mx) ** 2 for v in x)
    syy = sum((v - my) ** 2 for v in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((p - mx) * (q - my) for p, q in zip(x, y)) / math.sqrt(sxx * syy)


def rank_key(v: dict):
    s, sh = v.get("score"), v.get("sharpe")
    return (0 if s is not None else 1, -(s if s is not None else 0.0), -(sh if sh is not None else 0.0),
            int(v.get("slot") or 0))


def select(population: Sequence[dict], n: int) -> dict:
    """``population``: [{variant_id, slot, score|None, sharpe|None, returns: {date: r}, checks_passed, state}]
    -> {ranking: [variant_id], elites, refine, culled, duplicates: {variant_id: {of, correlation}},
    k, c, reasons: {variant_id: text}}."""
    ranked = sorted(population, key=rank_key)
    dups: Dict[str, dict] = {}
    kept: List[dict] = []
    for v in ranked:
        if v.get("score") is None:
            continue
        for w in kept:
            c = correlation(v.get("returns") or {}, w.get("returns") or {})
            if c is not None and c >= CORRELATION_DUPLICATE:
                dups[v["variant_id"]] = {"of": w["variant_id"], "correlation": round(c, 4)}
                break
        else:
            kept.append(v)
    k, c = k_elites(n), c_culled(n)
    reasons: Dict[str, str] = {}
    usable = [v for v in ranked if v.get("score") is not None and v["variant_id"] not in dups]
    unusable = [v for v in ranked if v not in usable]
    for v in unusable:
        if v["variant_id"] in dups:
            d = dups[v["variant_id"]]
            reasons[v["variant_id"]] = f"duplicate of {d['of']} (daily-return correlation {d['correlation']:.2f})"
        else:
            reasons[v["variant_id"]] = v.get("reason") or "no score"
    elites = usable[:k]
    rest = usable[k:]
    # cull: unusable first, then the worst usable ones, until c are culled (unusable are always culled)
    n_cull_usable = max(0, c - len(unusable))
    culled_usable = rest[len(rest) - n_cull_usable:] if n_cull_usable else []
    refine = [v for v in rest if v not in culled_usable]
    for v in culled_usable:
        reasons[v["variant_id"]] = "culled: among the lowest scores of the generation"
    return {"ranking": [v["variant_id"] for v in ranked], "elites": [v["variant_id"] for v in elites],
            "refine": [v["variant_id"] for v in refine],
            "culled": [v["variant_id"] for v in unusable + culled_usable], "duplicates": dups, "k": k, "c": c,
            "reasons": reasons}


def finalists(population: Sequence[dict], n: int, duplicates: Dict[str, dict]) -> List[str]:
    k = k_elites(n)
    out = []
    for v in sorted(population, key=rank_key):
        if v["variant_id"] in duplicates or v.get("score") is None:
            continue
        if v["score"] > 0 and v.get("checks_passed"):
            out.append(v["variant_id"])
        if len(out) >= k:
            break
    return out
