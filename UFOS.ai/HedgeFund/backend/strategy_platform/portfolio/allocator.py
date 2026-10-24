"""Allocator (SPEC §17.4): inverse volatility with a correlation penalty, per account,
over the allocator-managed sources (incubating — later active — strategies; the
Council source keeps its own capital and is not allocated).

Definition (deterministic):
- vol_i: annualised std of the source's daily sub-ledger returns over the last
  VOL_WINDOW (60) sessions when ≥ VOL_MIN (20) exist (``vol_source`` "ledger"); else the
  strategy's expected volatility from the daily returns of its holdout run card's
  equity curve (last 60; "holdout") — the allocator's definition for young strategies,
  not a data fallback; neither → weight 0 "no volatility estimate (…)".
- ρ̄_i = mean over the other sources j of max(0, corr(r_i, r_j)) over the last
  CORR_WINDOW (120) common days (≥ CORR_MIN (20), else that pair is left out).
- raw_i = (1 / vol_i) / (1 + λ ρ̄_i); w = raw / Σ raw; w_i < min_weight → 0 and the rest
  re-normalised once; then capped at max_weight (excess NOT redistributed — unallocated
  cash); then × max_total.
- Rebalancing: capital transfers between ``_unallocated`` and the sources (virtual
  ``capital`` sub-ledger entries) only where |target − equity| > drift_band × pool;
  outflows first, inflows limited by the unallocated cash. Stored in ``pf_allocations``.

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

import json
import math
from typing import Dict, List, Optional, Tuple

from ..storage import db

VOL_WINDOW, VOL_MIN = 60, 20
CORR_WINDOW, CORR_MIN = 120, 20
PERIODS = {"nyse": 252, "utc": 365}


def _std(x: List[float]) -> Optional[float]:
    n = len(x)
    if n < 2:
        return None
    m = math.fsum(x) / n
    return math.sqrt(math.fsum((v - m) ** 2 for v in x) / (n - 1))


def corr(a: List[float], b: List[float]) -> Optional[float]:
    n = len(a)
    if n < 2 or n != len(b):
        return None
    ma, mb = math.fsum(a) / n, math.fsum(b) / n
    sa = math.sqrt(math.fsum((v - ma) ** 2 for v in a))
    sb = math.sqrt(math.fsum((v - mb) ** 2 for v in b))
    if sa <= 0 or sb <= 0:
        return None
    return math.fsum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def weights(sources: List[dict], params: dict) -> List[dict]:
    """``sources``: [{source_id, name, ledger_returns: [(day, r)], holdout_returns: [(day, r)] | None,
    holdout_error, calendar}] → [{source_id, name, weight, vol, vol_source, mean_corr, penalty, capped, note}]."""
    lam = float(params.get("alloc_lambda", 1.0))
    wmax, wmin = float(params.get("alloc_max_weight", 0.4)), float(params.get("alloc_min_weight", 0.05))
    total = float(params.get("alloc_max_total", 1.0))
    rows = []
    for s in sources:
        P = PERIODS.get(s.get("calendar") or "nyse", 252)
        led = list(s.get("ledger_returns") or [])
        hold = list(s.get("holdout_returns") or [])
        row = {"source_id": s["source_id"], "name": s.get("name"), "weight": 0.0, "vol": None, "vol_source": None,
               "mean_corr": None, "penalty": None, "capped": False, "note": None, "_series": None}
        if len(led) >= VOL_MIN:
            sd = _std([r for _d, r in led[-VOL_WINDOW:]])
            row.update(vol_source="ledger", _series=led)
        elif len(hold) >= VOL_MIN:
            sd = _std([r for _d, r in hold[-VOL_WINDOW:]])
            row.update(vol_source="holdout", _series=hold)
        else:
            sd = None
            why = s.get("holdout_error") or f"{len(led)} incubation sessions and {len(hold)} holdout returns (need {VOL_MIN})"
            row["note"] = f"no volatility estimate ({why})"
        if sd is not None and sd > 0:
            row["vol"] = sd * math.sqrt(P)
        elif sd is not None:
            row["note"] = "no volatility estimate (zero variance)"
            row["vol_source"] = None
        rows.append(row)
    ok = [r for r in rows if r["vol"]]
    for r in ok:
        cs = []
        for o in ok:
            if o is r:
                continue
            a = dict(r["_series"][-CORR_WINDOW:])
            b = dict(o["_series"][-CORR_WINDOW:])
            days = sorted(set(a) & set(b))[-CORR_WINDOW:]
            if len(days) < CORR_MIN:
                continue
            c = corr([a[d] for d in days], [b[d] for d in days])
            if c is not None:
                cs.append(max(0.0, c))
        r["mean_corr"] = math.fsum(cs) / len(cs) if cs else 0.0
        r["penalty"] = 1.0 + lam * r["mean_corr"]
        r["_raw"] = (1.0 / r["vol"]) / r["penalty"]
    s_raw = math.fsum(r["_raw"] for r in ok)
    if s_raw > 0:
        for r in ok:
            r["weight"] = r["_raw"] / s_raw
        small = [r for r in ok if r["weight"] < wmin]
        if small and len(small) < len(ok):
            for r in small:
                r["weight"] = 0.0
                r["note"] = f"weight below the minimum {wmin:g} → 0"
            rest = math.fsum(r["_raw"] for r in ok if r not in small)
            for r in ok:
                if r not in small:
                    r["weight"] = r["_raw"] / rest
        elif small:
            # review fix L2 (2026-10-18): the floor would remove EVERY strategy — no allocation (weights_result()
            # reports status "no_allocation" with the reason; rebalance() moves no capital), never silent zeros
            for r in small:
                r["weight"] = 0.0
                r["note"] = (f"weight {r['_raw'] / s_raw:.4f} below the minimum {wmin:g} — every strategy is below "
                             "it, so no allocation was made")
        for r in ok:
            if r["weight"] > wmax:
                r["weight"] = wmax
                r["capped"] = True
            r["weight"] *= total
    for r in rows:
        r.pop("_series", None)
        r.pop("_raw", None)
    return rows


def weights_result(sources: List[dict], params: dict) -> dict:
    """``weights()`` plus the outcome (review fix L2): {"weights": rows, "status": "ok" | "no_allocation", "reason"}.
    ``no_allocation`` when there are managed sources but none gets a weight > 0 — every one lacks a volatility
    estimate, or the min-weight floor removes all of them (the reason names which and the floor)."""
    rows = weights(sources, params)
    if not rows or any(r["weight"] > 0 for r in rows):
        return {"weights": rows, "status": "ok", "reason": None}
    wmin = float(params.get("alloc_min_weight", 0.05))
    estimated = [r for r in rows if r["vol"]]
    if estimated:
        reason = (f"No allocation: all {len(estimated)} strategies with a volatility estimate fall below the minimum "
                  f"weight {wmin:g} (inverse-volatility weights of {len(estimated)} strategies cannot all reach it) "
                  "— lower the minimum weight in the allocator settings, then rebalance. The capital stays where it is.")
    else:
        reason = ("No allocation: no strategy has a volatility estimate yet (see each strategy's note). The capital "
                  "stays where it is.")
    return {"weights": rows, "status": "no_allocation", "reason": reason}


def latest(account_id: str) -> Optional[dict]:
    with db._connect() as conn:
        r = conn.execute("SELECT id, created_at, actor, reason, result_json FROM pf_allocations WHERE account_id = ?"
                         " ORDER BY id DESC LIMIT 1", (account_id,)).fetchone()
    if r is None:
        return None
    d = json.loads(r["result_json"])
    d.update(id=r["id"], created_at=r["created_at"], actor=r["actor"], reason=r["reason"])
    return d


def plan_transfers(pool: float, targets: Dict[str, float], equity: Dict[str, float], unallocated_cash: float,
                   drift: float) -> List[Tuple[str, float]]:
    """[(source, amount)] capital moves into (+) / out of (−) each source; outflows first, inflows limited by the
    unallocated cash available after the outflows."""
    moves = []
    for s, tgt in sorted(targets.items()):
        diff = tgt - equity.get(s, 0.0)
        if abs(diff) > drift * pool + 1e-9:
            moves.append((s, diff))
    out = [(s, d) for s, d in moves if d < 0]
    avail = unallocated_cash - math.fsum(d for _s, d in out)
    ins = [(s, d) for s, d in moves if d > 0]
    need = math.fsum(d for _s, d in ins)
    if need > avail + 1e-9 and need > 0:
        scale = max(0.0, avail) / need
        ins = [(s, d * scale) for s, d in ins]
    return out + [(s, d) for s, d in ins if d > 1e-9]
