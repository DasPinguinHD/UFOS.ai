"""Rule-based shadow portfolios on the SAME cash flows (SPEC §19.8): "what if you had followed rule X".

Flows: per day ``F_d`` = −Σ net cash of the user's buys and sells (buys + fees in, sell proceeds out), in the
account's base currency. Every shadow receives exactly these flows (parity, tested) at the day's close; the rule's
components are held in USD units (prices from ``monitoring.prices``; a strategy = its backtest card's equity curve);
a withdrawal larger than the shadow's value draws its cash below 0 (financed at 0 %, reported as ``cash_min``).
EUR flows are converted at DEXUSEU of their date, the end value at the end date (FX effect included). No costs,
taxes or slippage; fractional units. Hypothetical — not investment advice.
"""
from __future__ import annotations

import bisect
import math
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

RULE_TYPES = ("buy_and_hold", "monthly_rebalance", "strategy")
MAX_RULES = 5
NOTICE = ("No costs, taxes or slippage; fractional units; flows at the day's close. Hypothetical, simulated results "
          "— they do not indicate future results.")          # S10-H (core/notices.HYPOTHETICAL)


class Unavailable(Exception):
    pass


def flows_of(txns_b: List[dict]) -> List[Tuple[str, float]]:
    by: Dict[str, float] = {}
    for t in txns_b:
        if t["kind"] in ("buy", "sell"):
            by[t["date"]] = by.get(t["date"], 0.0) - t["net_b"]
    return sorted((d, v) for d, v in by.items() if abs(v) > 1e-12)


def check_rule(r) -> Optional[str]:
    if not isinstance(r, dict) or r.get("type") not in RULE_TYPES:
        return "type: buy_and_hold | monthly_rebalance | strategy"
    t = r["type"]
    allowed = {"buy_and_hold": {"type", "symbol"}, "monthly_rebalance": {"type", "weights"},
               "strategy": {"type", "strategy_id"}}[t]
    if set(r) - allowed:
        return "unknown field(s): " + ", ".join(sorted(str(k)[:30] for k in set(r) - allowed))
    import re
    sym_re = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$|^[A-Z0-9]{2,10}-USD$")
    if t == "buy_and_hold":
        if not isinstance(r.get("symbol"), str) or not sym_re.match(r["symbol"].strip().upper()):
            return "symbol: a ticker such as SPY"
    elif t == "monthly_rebalance":
        w = r.get("weights")
        if not isinstance(w, dict) or not 1 <= len(w) <= 10:
            return "weights: {symbol: weight} with 1..10 symbols"
        for k, v in w.items():
            if not isinstance(k, str) or not sym_re.match(k.strip().upper()):
                return f"weights: {str(k)[:20]!r} is not a ticker"
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < float(v) <= 1:
                return "weights: each weight in (0, 1]"
        if abs(math.fsum(float(v) for v in w.values()) - 1.0) > 1e-6:
            return "weights must sum to 1"
    else:
        if not isinstance(r.get("strategy_id"), str) or not 1 <= len(r["strategy_id"]) <= 64:
            return "strategy_id required"
    return None


def label_of(r: dict) -> str:
    if r["type"] == "buy_and_hold":
        return f"Buy and hold {r['symbol'].upper()}"
    if r["type"] == "monthly_rebalance":
        return "Monthly rebalance " + " / ".join(f"{k.upper()} {float(v) * 100:.0f} %" for k, v in r["weights"].items())
    return f"Strategy {r['strategy_id']}"


class Series:
    def __init__(self, rows: List[Tuple[str, float]], name: str):
        if not rows:
            raise Unavailable(f"no prices for {name}")
        self.days = [d for d, _p in rows]
        self.px = [p for _d, p in rows]
        self.name = name

    def on(self, day: str) -> Optional[float]:
        i = bisect.bisect_right(self.days, day) - 1
        if i < 0 or (date.fromisoformat(day) - date.fromisoformat(self.days[i])).days > 6:
            return None
        return self.px[i]


def simulate(flows: List[Tuple[str, float]], comps: Dict[str, Series], weights: Dict[str, float], monthly: bool,
             end: str, to_usd: Callable[[float, str], Optional[float]],
             to_base: Callable[[float, str], Optional[float]]) -> dict:
    """The unit-value simulation; returns {end_value, cash_min, twr, applied: [(day, flow)]}."""
    if not flows:
        raise Unavailable("no trade flows")
    first = flows[0][0]
    for s in comps.values():
        if s.on(first) is None:
            raise Unavailable(f"the price history of {s.name} starts after the first flow ({first})")
        if s.on(end) is None:
            raise Unavailable(f"no price of {s.name} at the end date {end}")
    days = sorted({d for s in comps.values() for d in s.days if first <= d <= end} | {d for d, _f in flows} | {end})
    fl = dict(flows)
    units = {k: 0.0 for k in comps}
    cash = 0.0                     # base currency, may go negative (financed at 0 %)
    cash_min = 0.0
    twr = 1.0
    prev_val = None
    last_month = None
    applied = []
    for d in days:
        if d > end:
            break
        px = {k: comps[k].on(d) for k in comps}
        if any(v is None for v in px.values()):
            continue
        val_usd = math.fsum(units[k] * px[k] for k in comps)
        if prev_val is not None and prev_val > 0:
            twr *= val_usd / prev_val
        if monthly and d[:7] != last_month and val_usd > 0:
            for k in comps:
                units[k] = weights[k] * val_usd / px[k]
        last_month = d[:7]
        f = fl.get(d, 0.0)
        if f:
            applied.append((d, f))
            if f > 0:
                usd = to_usd(f, d)
                if usd is None:
                    raise Unavailable(f"no EUR/USD rate for {d} (refresh the FX rates)")
                if cash < 0:                       # repay the financing first (base currency)
                    rep = min(-cash, f)
                    cash += rep
                    usd = to_usd(f - rep, d) if f - rep > 0 else 0.0
                for k in comps:
                    units[k] += weights[k] * usd / px[k]
            else:
                need = -f
                need_usd = to_usd(need, d)
                if need_usd is None:
                    raise Unavailable(f"no EUR/USD rate for {d} (refresh the FX rates)")
                val = math.fsum(units[k] * px[k] for k in comps)
                take = min(need_usd, val)
                if val > 0:
                    for k in comps:
                        units[k] *= (1.0 - take / val)
                rest_usd = need_usd - take
                if rest_usd > 1e-9:
                    cash -= need * (rest_usd / need_usd)
                    cash_min = min(cash_min, cash)
        prev_val = math.fsum(units[k] * px[k] for k in comps)
    end_px = {k: comps[k].on(end) for k in comps}
    end_usd = math.fsum(units[k] * end_px[k] for k in comps)
    end_base = to_base(end_usd, end)
    if end_base is None:
        raise Unavailable(f"no EUR/USD rate for the end date {end}")
    return {"end_value": end_base + cash, "cash_min": cash_min, "twr": twr - 1.0, "applied": applied}


def xirr(cashflows: List[Tuple[str, float]]) -> Optional[float]:
    """Annualised money-weighted return of investor cash flows (negative = paid in); bisection, None if no root."""
    if len(cashflows) < 2 or not any(c > 0 for _d, c in cashflows) or not any(c < 0 for _d, c in cashflows):
        return None
    d0 = date.fromisoformat(min(d for d, _c in cashflows))

    def npv(r):
        return math.fsum(c / (1.0 + r) ** ((date.fromisoformat(d) - d0).days / 365.0) for d, c in cashflows)

    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-9:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def run_rule(rule: dict, flows, end: str, closes, strategy_series: Callable[[str], List[Tuple[str, float]]],
             to_usd, to_base) -> dict:
    t = rule["type"]
    if t == "buy_and_hold":
        sym = rule["symbol"].strip().upper()
        comps = {sym: Series(closes.series(sym) or [], sym)}
        weights, monthly = {sym: 1.0}, False
    elif t == "monthly_rebalance":
        comps, weights = {}, {}
        for k, v in rule["weights"].items():
            s = k.strip().upper()
            comps[s] = Series(closes.series(s) or [], s)
            weights[s] = float(v)
        monthly = True
    else:
        comps = {"strategy": Series(strategy_series(rule["strategy_id"]), f"strategy {rule['strategy_id']}")}
        weights, monthly = {"strategy": 1.0}, False
    return simulate(flows, comps, weights, monthly, end, to_usd, to_base)


def compare(txns_b: List[dict], rules: List[dict], end: str, *, closes, strategy_series, to_usd, to_base,
            user_end_value: Optional[float], user_message: Optional[str]) -> dict:
    flows = flows_of(txns_b)
    divs = [(t["date"], t["net_b"]) for t in txns_b if t["kind"] in ("dividend", "interest", "withholding")]
    sum_in = math.fsum(f for _d, f in flows if f > 0)
    sum_out = -math.fsum(f for _d, f in flows if f < 0)
    user = {"valued": user_end_value is not None, "message": user_message, "end_value": user_end_value,
            "pnl": None, "mwr_annual": None}
    if user_end_value is not None:
        user["pnl"] = user_end_value + math.fsum(v for _d, v in divs) - math.fsum(f for _d, f in flows)
        cf = [(d, -f) for d, f in flows] + list(divs) + [(end, user_end_value)]
        user["mwr_annual"] = xirr(cf)
    out_rules = []
    for r in rules:
        lab = label_of(r)
        try:
            res = run_rule(r, flows, end, closes, strategy_series, to_usd, to_base)
        except Unavailable as ex:
            out_rules.append({"rule": r, "label": lab, "unavailable": str(ex)})
            continue
        applied = res["applied"]
        diff = max((abs(a[1] - b[1]) for a, b in zip(applied, flows)), default=0.0)
        flows_equal = len(applied) == len(flows) and all(a[0] == b[0] for a, b in zip(applied, flows)) and diff < 1e-9
        cf = [(d, -f) for d, f in flows] + [(end, res["end_value"])]
        out_rules.append({"rule": r, "label": lab, "end_value": res["end_value"],
                          "pnl": res["end_value"] - math.fsum(f for _d, f in flows), "mwr_annual": xirr(cf),
                          "twr": res["twr"], "cash_min": res["cash_min"],
                          "parity": {"flows_equal": flows_equal, "max_abs_diff": diff, "count": len(applied)}})
    return {"period": {"first": flows[0][0] if flows else None, "last": end},
            "flows": {"count": len(flows), "sum_in": sum_in, "sum_out": sum_out}, "user": user,
            "shadows": out_rules, "notice": NOTICE}
