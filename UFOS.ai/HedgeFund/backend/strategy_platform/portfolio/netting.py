"""Netting of all strategy sources' deltas into ONE order per instrument (SPEC §17.5).

Per symbol: the sources' deltas (desired − sub-ledger quantity) in opposite directions
are CROSSED internally (no market order — no self-crossing between our own strategies,
avoids wash-trade patterns); the residual is one account order, two legs when the
account position flips sign (close first, then open — the PaperBroker's transition
rule), allocated to the sources whose residual points that way. Orders are returned
reductions first (the backtest's execution order). Pure functions; no I/O.

Part of UFOS.ai, a proof-of-concept portfolio project; paper trading only.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .ledger import EPS, split_qty


def _rq(symbol: str, x: float) -> float:
    from strategy_platform.execution.paper_broker import round_qty
    return round_qty(symbol, x)


def legs_for(symbol: str, held: float, net: float) -> List[Tuple[str, float]]:
    """Account-level legs moving ``held`` by ``net`` (both signed)."""
    target = held + net
    if abs(net) <= EPS:
        return []
    if held >= -EPS and target >= -EPS:
        return [("buy", net)] if net > 0 else [("sell", -net)]
    if held <= EPS and target <= EPS:
        return [("short", -net)] if net < 0 else [("cover", net)]
    if held > 0:
        return [("sell", held), ("short", -target)]
    return [("cover", -held), ("buy", target)]


def net(deltas: Dict[str, Dict[str, float]], account_qty: Dict[str, float],
        max_qty: Optional[Dict[str, float]] = None) -> dict:
    """``deltas`` {symbol: {source: signed delta}}; ``account_qty`` {symbol: held qty};
    ``max_qty`` {symbol: max |net order qty|} (participation cap; the residual allocations
    shrink pro rata). Returns {"crosses": [{symbol, legs: [(source, signed qty)]}],
    "orders": [{symbol, reducing, net, legs: [{side, qty, allocations: [(source, qty)]}], capped}]}."""
    crosses, orders = [], []
    for sym in sorted(deltas):
        d = {s: float(v) for s, v in deltas[sym].items() if abs(float(v)) > EPS}
        if not d:
            continue
        buys = sorted((s, v) for s, v in d.items() if v > 0)
        sells = sorted((s, -v) for s, v in d.items() if v < 0)
        B = math.fsum(v for _s, v in buys)
        S = math.fsum(v for _s, v in sells)
        X = min(B, S)
        resid = dict(d)
        if X > EPS:
            bq = split_qty(sym, X, buys)
            sq = split_qty(sym, X, sells)
            legs = [(s, q) for s, q in bq if q > EPS] + [(s, -q) for s, q in sq if q > EPS]
            crosses.append({"symbol": sym, "legs": legs})
            for s, q in legs:
                resid[s] = resid[s] - q
        netq = math.fsum(resid.values())
        if abs(netq) <= EPS:
            continue
        direction = 1.0 if netq > 0 else -1.0
        alloc = sorted((s, abs(v)) for s, v in resid.items() if v * direction > EPS)
        total = math.fsum(v for _s, v in alloc)
        capped = False
        if max_qty is not None and sym in max_qty and max_qty[sym] is not None and total > max_qty[sym] + EPS:
            allowed = _rq(sym, max(0.0, float(max_qty[sym])))
            capped = True
            alloc = [(s, q) for s, q in split_qty(sym, allowed, alloc) if q > EPS]
            total = math.fsum(v for _s, v in alloc)
            if total <= EPS:
                orders.append({"symbol": sym, "reducing": False, "net": 0.0, "legs": [], "capped": True})
                continue
        held = float(account_qty.get(sym, 0.0))
        legs = legs_for(sym, held, direction * total)
        out_legs = []
        remaining = list(alloc)
        for i, (side, q) in enumerate(legs):
            if i == len(legs) - 1:
                parts = [(s, v) for s, v in remaining if v > EPS]
            else:
                parts = [(s, v) for s, v in split_qty(sym, q, remaining) if v > EPS]
                left = dict(remaining)
                for s, v in parts:
                    left[s] = left[s] - v
                remaining = sorted((s, v) for s, v in left.items() if v > EPS)
            out_legs.append({"side": side, "qty": q, "allocations": parts})
        desired = held + direction * total
        reducing = abs(desired) <= EPS or (desired * held > 0 and abs(desired) < abs(held))
        orders.append({"symbol": sym, "reducing": reducing, "net": direction * total, "legs": out_legs,
                       "capped": capped})
    orders.sort(key=lambda o: (0 if o["reducing"] else 1, o["symbol"]))
    return {"crosses": crosses, "orders": orders}
