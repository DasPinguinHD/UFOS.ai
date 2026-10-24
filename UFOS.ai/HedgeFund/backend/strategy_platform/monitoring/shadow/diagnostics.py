"""Behaviour diagnostics of a Shadow account (SPEC §19.7) — deterministic, no AI, no network of its own (prices come
from ``monitoring.prices.Closes`` / the user's valuation). Every block may be ``{"unavailable": reason}``.

Input: normalised transactions already converted to the account's base currency (``to_base``): each has
``gross_b, fee_b, tax_b, net_b`` (and ``fx`` = base per unit of its currency at its date).
"""
from __future__ import annotations

import bisect
import math
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import normalize

AFTER_LOSS_DAYS = 5
TIMING_HORIZON = 20
ROUND_TRIP_DAYS = 30


# --- base currency ---------------------------------------------------------------------------------------------

def to_base(txns: Sequence[dict], base: str, rates) -> Tuple[List[dict], List[str], List[str]]:
    """-> (converted txns, missing FX dates, unsupported currencies). A txn that cannot be converted is left out."""
    from .. import fx
    out, missing, unsupported = [], [], []
    for t in txns:
        try:
            if t["currency"] == base:
                f = 1.0
            else:
                one = rates.convert(1.0, t["currency"], base, t["date"], missing)
                if one is None:
                    continue
                f = one
        except fx.FxUnsupported as ex:
            unsupported.append(ex.currency)
            continue
        c = dict(t)
        c.update({"fx": f, "gross_b": (t["gross"] or 0.0) * f, "fee_b": (t["fee"] or 0.0) * f,
                  "tax_b": (t["tax"] or 0.0) * f, "net_b": (t["net"] or 0.0) * f,
                  "price_b": (t["price"] * f) if t.get("price") is not None else None})
        out.append(c)
    return out, sorted(set(missing)), sorted(set(unsupported))


def _book_apply(book: normalize.Book, t: dict) -> List[dict]:
    if t["kind"] in normalize.ACQUISITIONS:          # a staking reward opens a lot at its receipt value
        return book.buy(t["key"], t["date"], t["qty"], {"b": t["gross_b"] + t["fee_b"]}, {"txn": t.get("id")})
    if t["kind"] == "sell":
        return book.sell(t["key"], t["date"], t["qty"], {"b": t["gross_b"] - t["fee_b"]}, {"txn": t.get("id")})
    return []


def _by_day(txns: Sequence[dict]) -> List[Tuple[str, List[dict]]]:
    days: Dict[str, List[dict]] = {}
    for t in sorted(txns, key=normalize.sort_key):
        days.setdefault(t["date"], []).append(t)
    return sorted(days.items())


def _wpct(pairs: List[Tuple[float, float]], q: float) -> Optional[float]:
    """Weighted percentile (value, weight) — the smallest value whose cumulative weight reaches q."""
    pairs = sorted(p for p in pairs if p[1] > 0)
    tot = math.fsum(w for _v, w in pairs)
    if tot <= 0:
        return None
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * tot - 1e-12:
            return v
    return pairs[-1][0]


# --- blocks --------------------------------------------------------------------------------------------------

# the ONE coverage rule of the disposition effect (report, API, diagnostics view): below this share of priced cases
# (realised + paper gains / losses on sell days) PGR / PLR are "not meaningful" and not shown
MIN_PRICED_SHARE = 0.8


def not_meaningful(unpriced: int, cases: int) -> str:
    return (f"not meaningful: {unpriced} of {cases} cases unpriced (add symbol mappings or valuation prices)")


def disposition(txns: Sequence[dict], price_b: Callable[[str, str], Optional[float]]) -> dict:
    book = normalize.Book()
    rg = rl = pg = pl = 0
    unpriced = 0
    sell_days = 0
    for day, items in _by_day(txns):
        sells = {t["key"] for t in items if t["kind"] == "sell"}
        before = book.holdings() if sells else {}
        realised: Dict[str, float] = {}
        for t in items:
            for p in _book_apply(book, t):
                if t["kind"] == "sell":
                    realised[p["key"]] = realised.get(p["key"], 0.0) + p["proceeds"]["b"] - p["cost"]["b"]
        if not sells or not before:
            continue
        sell_days += 1
        for key, h in before.items():
            if h["qty"] <= 0:
                continue
            if key in sells:
                g = realised.get(key)
                if g is None:
                    continue
                if g > 0:
                    rg += 1
                elif g < 0:
                    rl += 1
                continue
            px = price_b(key, day)
            if px is None:
                unpriced += 1
                continue
            avg = h["amounts"]["b"] / h["qty"]
            if px > avg:
                pg += 1
            elif px < avg:
                pl += 1
    if sell_days == 0:
        return {"unavailable": "no sells — the disposition effect needs realised trades"}
    cases = rg + rl + pg + pl + unpriced
    priced_share = (cases - unpriced) / cases if cases else None
    if priced_share is not None and priced_share < MIN_PRICED_SHARE:
        # live audit 2026-10-18: with most paper gains / losses unpriced PGR / PLR are meaningless — no numbers
        return {"unavailable": not_meaningful(unpriced, cases), "not_meaningful": True, "cases": cases,
                "unpriced_cases": unpriced, "priced_share": priced_share, "min_priced_share": MIN_PRICED_SHARE,
                "sell_days": sell_days}
    pgr = rg / (rg + pg) if rg + pg else None
    plr = rl / (rl + pl) if rl + pl else None
    de = (pgr - plr) if pgr is not None and plr is not None else None
    t_stat = None
    if de is not None:
        var = (pgr * (1 - pgr) / (rg + pg)) + (plr * (1 - plr) / (rl + pl))
        t_stat = de / math.sqrt(var) if var > 0 else None
    return {"pgr": pgr, "plr": plr, "de": de, "t_stat": t_stat, "realized_gains": rg, "realized_losses": rl,
            "paper_gains": pg, "paper_losses": pl, "sell_days": sell_days, "unpriced_cases": unpriced, "cases": cases,
            "priced_share": priced_share, "min_priced_share": MIN_PRICED_SHARE}


def _daily_basis(txns: Sequence[dict], first: str, last: str) -> Tuple[List[float], List[Dict[str, float]]]:
    """Cost basis per calendar day (total and per key) after that day's transactions."""
    book = normalize.Book()
    by = dict(_by_day(txns))
    d, end = date.fromisoformat(first), date.fromisoformat(last)
    totals, per = [], []
    while d <= end:
        for t in by.get(d.isoformat(), []):
            _book_apply(book, t)
        h = {k: v["amounts"]["b"] for k, v in book.holdings().items() if v["qty"] > 0}
        per.append(h)
        totals.append(math.fsum(h.values()))
        d += timedelta(days=1)
    return totals, per


def turnover(txns: Sequence[dict], first: str, last: str, closed: List[dict]) -> dict:
    trades = [t for t in txns if t["kind"] in ("buy", "sell")]
    if not trades:
        return {"unavailable": "no trades"}
    buys = sum(1 for t in trades if t["kind"] == "buy")
    days = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
    volume = math.fsum(t["gross_b"] for t in trades)
    # every fee as in the insights (§20.1): trades, fee rows and the order fee of a pending private-market order
    fees = math.fsum(t["fee_b"] for t in trades) + math.fsum(t["fee_b"] for t in txns
                                                             if t["kind"] in ("fee", "pending_buy"))
    totals, _per = _daily_basis(txns, first, last)
    held = [x for x in totals if x > 0]
    avg_basis = math.fsum(held) / len(held) if held else None
    rt = {(p["key"], p["close_date"]) for p in closed
          if normalize.holding_days(p["open_date"], p["close_date"]) < ROUND_TRIP_DAYS}
    out = {"trades": len(trades), "buys": buys, "sells": len(trades) - buys,
           "trades_per_month": len(trades) / (days / 30.4375), "volume": volume, "avg_cost_basis": avg_basis,
           "turnover_annual": None, "fees": fees, "fee_share_pct": (fees / volume * 100.0) if volume else None,
           "fee_drag_annual_pct": None, "round_trips_under_30d": len(rt)}
    if avg_basis:
        out["turnover_annual"] = (volume / 2.0) / avg_basis * 365.0 / days
        out["fee_drag_annual_pct"] = fees / avg_basis * 365.0 / days * 100.0
    return out


def holding(closed: List[dict]) -> dict:
    if not closed:
        return {"unavailable": "no closed positions"}
    pairs = [(normalize.holding_days(p["open_date"], p["close_date"]), p["qty"]) for p in closed]
    win = [(normalize.holding_days(p["open_date"], p["close_date"]), p["qty"]) for p in closed
           if p["proceeds"]["b"] - p["cost"]["b"] > 0]
    los = [(normalize.holding_days(p["open_date"], p["close_date"]), p["qty"]) for p in closed
           if p["proceeds"]["b"] - p["cost"]["b"] < 0]
    tot = math.fsum(w for _v, w in pairs)
    return {"closed_parts": len(closed), "median_days": _wpct(pairs, 0.5),
            "mean_days": math.fsum(v * w for v, w in pairs) / tot if tot else None,
            "p25_days": _wpct(pairs, 0.25), "p75_days": _wpct(pairs, 0.75),
            "winners_median_days": _wpct(win, 0.5) if win else None,
            "losers_median_days": _wpct(los, 0.5) if los else None}


def _conc(weights: Dict[str, float]) -> Optional[dict]:
    tot = math.fsum(weights.values())
    if tot <= 0:
        return None
    w = sorted((v / tot for v in weights.values()), reverse=True)
    hhi = math.fsum(x * x for x in w)
    return {"hhi": hhi, "effective_n": 1.0 / hhi if hhi > 0 else None, "top1_pct": w[0] * 100.0,
            "top3_pct": math.fsum(w[:3]) * 100.0}


def concentration(txns: Sequence[dict], first: str, last: str) -> dict:
    _tot, per = _daily_basis(txns, first, last)
    days = [p for p in per if p]
    if not days:
        return {"unavailable": "no positions"}
    cur = _conc(per[-1]) if per[-1] else None
    cs = [_conc(p) for p in days]
    avg = {k: math.fsum(c[k] for c in cs) / len(cs) for k in ("hhi", "top1_pct", "top3_pct")}
    avg["effective_n"] = 1.0 / avg["hhi"] if avg["hhi"] > 0 else None
    return {"current": cur, "average": avg, "positions": len(per[-1]), "basis": "cost"}


def after_losses(txns: Sequence[dict], closed: List[dict]) -> dict:
    res: Dict[Tuple[str, str], float] = {}
    for p in closed:
        k = (p["key"], p["close_date"])
        res[k] = res.get(k, 0.0) + p["proceeds"]["b"] - p["cost"]["b"]
    buys = sorted((t["date"], t["gross_b"]) for t in txns if t["kind"] == "buy")
    if not res or not buys:
        return {"unavailable": "needs realised sells and buys"}
    bdays = [d for d, _g in buys]

    def window(day):
        lo = bisect.bisect_right(bdays, day)
        hi = bisect.bisect_right(bdays, (date.fromisoformat(day) + timedelta(days=AFTER_LOSS_DAYS)).isoformat())
        return buys[lo:hi]

    loss = [window(d) for (k, d), g in res.items() if g < 0]
    gain = [window(d) for (k, d), g in res.items() if g > 0]
    la = math.fsum(len(w) for w in loss) / len(loss) if loss else None
    ga = math.fsum(len(w) for w in gain) / len(gain) if gain else None
    after = [g for w in loss for _d, g in w]
    return {"window_days": AFTER_LOSS_DAYS, "loss_sells": len(loss), "gain_sells": len(gain),
            "buys_after_loss_avg": la, "buys_after_gain_avg": ga,
            "ratio": (la / ga) if la is not None and ga else None,
            "size_after_loss_avg": math.fsum(after) / len(after) if after else None,
            "size_avg": math.fsum(g for _d, g in buys) / len(buys)}


def timing(txns: Sequence[dict], bench: Optional[List[Tuple[str, float]]], symbol: str) -> dict:
    if not bench or len(bench) <= TIMING_HORIZON:
        return {"unavailable": f"no benchmark prices for {symbol} in the period"}
    days = [d for d, _c in bench]
    closes = [c for _d, c in bench]

    def fwd(day) -> Optional[float]:
        i = bisect.bisect_left(days, day)
        if i + TIMING_HORIZON >= len(days):
            return None
        return closes[i + TIMING_HORIZON] / closes[i] - 1.0

    first = min(t["date"] for t in txns)
    i0 = bisect.bisect_left(days, first)
    unc = [closes[i + TIMING_HORIZON] / closes[i] - 1.0 for i in range(i0, len(days) - TIMING_HORIZON)]
    b = [x for x in (fwd(t["date"]) for t in txns if t["kind"] == "buy") if x is not None]
    s = [x for x in (fwd(t["date"]) for t in txns if t["kind"] == "sell") if x is not None]
    if not unc:
        return {"unavailable": "the period is too short for 20-session forward returns"}

    def avg(xs):
        return math.fsum(xs) / len(xs) * 100.0 if xs else None
    return {"benchmark": symbol, "horizon_sessions": TIMING_HORIZON, "buys": len(b), "sells": len(s),
            "buy_fwd_avg_pct": avg(b), "sell_fwd_avg_pct": avg(s), "uncond_fwd_avg_pct": avg(unc)}


# --- the whole set ----------------------------------------------------------------------------------------------

def compute(txns_b: Sequence[dict], *, price_b: Callable[[str, str], Optional[float]],
            bench: Optional[List[Tuple[str, float]]], bench_symbol: str) -> dict:
    trades = [t for t in txns_b if t["kind"] in ("buy", "sell")]
    if not txns_b:
        return {"empty": True}
    first = min(t["date"] for t in txns_b)
    last = max(t["date"] for t in txns_b)
    book = normalize.Book()
    for t in sorted(txns_b, key=normalize.sort_key):
        _book_apply(book, t)
    closed = book.closed
    out = {"period": {"first": first, "last": last,
                      "days": (date.fromisoformat(last) - date.fromisoformat(first)).days + 1},
           "disposition": disposition(txns_b, price_b) if trades else {"unavailable": "no trades"},
           "turnover": turnover(txns_b, first, last, closed),
           "holding": holding(closed),
           "concentration": concentration(txns_b, first, last) if trades else {"unavailable": "no trades"},
           "after_losses": after_losses(txns_b, closed),
           "timing": timing(trades, bench, bench_symbol) if trades else {"unavailable": "no trades"},
           "_book": book}
    return out


def facts(d: dict) -> List[dict]:
    """§11.1-shaped fact table of the numeric diagnostics (for the report + the AI commentary)."""
    rows: List[dict] = []

    def add(fid, label, v, unit):
        if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        if unit == "fraction":
            disp = f"{v * 100:.1f} %"
        elif unit == "percent":
            disp = f"{v:.2f} %"
        elif unit in ("count", "days"):
            disp = f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"
        else:
            disp = f"{v:,.2f}"
        rows.append({"id": fid, "label": label, "value": v, "unit": unit, "display": disp})

    di = d.get("disposition") or {}
    add("disp.pgr", "Proportion of gains realised (PGR)", di.get("pgr"), "fraction")
    add("disp.plr", "Proportion of losses realised (PLR)", di.get("plr"), "fraction")
    add("disp.de", "Disposition effect PGR − PLR", di.get("de"), "fraction")
    add("disp.t", "t statistic of the disposition effect", di.get("t_stat"), "number")
    tu = d.get("turnover") or {}
    add("turn.trades", "Trades", tu.get("trades"), "count")
    add("turn.per_month", "Trades per month", tu.get("trades_per_month"), "number")
    add("turn.annual", "Annual turnover (× average cost basis)", tu.get("turnover_annual"), "number")
    add("turn.fee_share", "Fees as % of traded volume", tu.get("fee_share_pct"), "percent")
    add("turn.fee_drag", "Fee drag per year (% of cost basis)", tu.get("fee_drag_annual_pct"), "percent")
    add("turn.round_trips", "Round trips under 30 days", tu.get("round_trips_under_30d"), "count")
    ho = d.get("holding") or {}
    add("hold.median", "Median holding period (days)", ho.get("median_days"), "days")
    add("hold.winners", "Median holding of winners (days)", ho.get("winners_median_days"), "days")
    add("hold.losers", "Median holding of losers (days)", ho.get("losers_median_days"), "days")
    co = (d.get("concentration") or {}).get("current") or {}
    add("conc.top1", "Largest position (% of cost basis)", co.get("top1_pct"), "percent")
    add("conc.effn", "Effective number of positions", co.get("effective_n"), "number")
    al = d.get("after_losses") or {}
    add("loss.buys_after_loss", "Buys within 5 days after a loss", al.get("buys_after_loss_avg"), "number")
    add("loss.buys_after_gain", "Buys within 5 days after a gain", al.get("buys_after_gain_avg"), "number")
    ti = d.get("timing") or {}
    add("time.buy_fwd", "Benchmark 20-session return after buys", ti.get("buy_fwd_avg_pct"), "percent")
    add("time.uncond_fwd", "Benchmark average 20-session return", ti.get("uncond_fwd_avg_pct"), "percent")
    return rows
