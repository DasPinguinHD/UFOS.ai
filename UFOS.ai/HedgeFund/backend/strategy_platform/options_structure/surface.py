"""Skew, smile, ATM term structure, expected move, VIX term structure and the variance risk premium (SP §21.12,
task §4). Everything from the platform's own IVs of ONE slot; interpolation only inside one expiry (in log-moneyness
or delta) or, for the 30-day ATM IV, between two expiries of the same slot in total variance — never across slots,
never extrapolated (a value that needs extrapolation is ``unavailable`` with the reason).

* Smile per expiry: (k = ln(K / F), IV) of OTM options (puts for K < F, calls for K ≥ F), F = S e^{(r − q) T}.
* ATM IV per expiry: linear in k between the two smile points around k = 0.
* 25Δ risk reversal RR = IV(call Δ = +0.25) − IV(put Δ = −0.25); butterfly BF = ½ (IV_c25 + IV_p25) − IV_ATM; the
  25Δ IVs by linear interpolation in the own BSM delta among the OTM calls / puts of the expiry.
* Expected move for an expiry: (a) the ATM straddle mid (call + put at the strike nearest the spot, both usable),
  (b) the model straddle √(2/π) · IV_ATM · √T · S (≈ 0.80 σ√T S — the value of an ATM straddle, so that the
  difference is not structurally −20 %), plus (c) the one-sigma move IV_ATM · √T · S for reference; the difference
  (a) − (b) — for "today" (the nearest available expiry on or after the slot date) and for the next one.
* IV_30: total variance w = IV² T linear in T between the expiries around 30 days; IV_30 = √(w_30 / T_30).
* VIX term structure (FRED, archived): VIXCLS (30 d) vs VXVCLS (3 m) of the SAME date → contango (VXV > VIX) /
  backwardation (VXV < VIX) / flat; slope = VXV − VIX; ratio = VIX / VXV. QQQ: VXNCLS (30 d) is shown as a level —
  there is no 3-month Nasdaq series on FRED, so the label is the S&P one (stated).
* VRP = IV_30 − RV_21 (decimal vol): RV = standard deviation (ddof 1) of the last 21 close-to-close log returns ×
  √252 from the platform's cached daily bars known by the slot; the newest close must be the previous session's.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from . import bs
from .model import Priced, PricedChain


def _interp(x0, x1, y0, y1, x):
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def smile(pc: PricedChain, exp: date) -> Tuple[Optional[float], List[dict]]:
    """(forward, OTM smile points sorted by k)."""
    rows = pc.by_expiry(exp)
    if not rows or pc.chain.spot is None:
        return None, []
    T = rows[0].T
    F = bs.forward(pc.chain.spot, T, pc.rate, pc.q_of(exp))
    pts = [{"strike": p.q.strike, "k": math.log(p.q.strike / F), "iv": p.iv, "kind": p.q.kind, "delta": p.delta}
           for p in rows if (p.q.kind == "put" and p.q.strike < F) or (p.q.kind == "call" and p.q.strike >= F)]
    return F, sorted(pts, key=lambda d: d["k"])


def atm_iv(points: List[dict]) -> Tuple[Optional[float], Optional[str]]:
    lo = [p for p in points if p["k"] <= 0]
    hi = [p for p in points if p["k"] >= 0]
    if not lo or not hi:
        return None, "no usable strikes on both sides of the forward (no extrapolation)"
    a, b = lo[-1], hi[0]
    return _interp(a["k"], b["k"], a["iv"], b["iv"], 0.0), None


def delta_iv(points: List[dict], kind: str, target: float) -> Tuple[Optional[float], Optional[str]]:
    """IV at |delta| = target among the OTM options of ``kind`` (linear in delta, bracketed only)."""
    pts = sorted((abs(p["delta"]), p["iv"]) for p in points if p["kind"] == kind and p["delta"] is not None)
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if d0 <= target <= d1:
            return _interp(d0, d1, v0, v1, target), None
    if pts and pts[-1][0] == target:
        return pts[-1][1], None
    return None, f"{100 * target:g}-delta {kind} not bracketed by usable quotes (no extrapolation)"


def expiry_view(pc: PricedChain, exp: date, cfg) -> dict:
    e = pc.expiries[exp]
    out = {"expiration": exp.isoformat(), "days": e["days"], "coverage_iv_pct": e["coverage_iv_pct"],
           "available": e["available_iv"], "reason": e["reason_iv"], "T": None, "forward": None, "atm_iv": None,
           "rr25": None, "bf25": None, "smile": [], "reasons": []}
    if not e["available_iv"]:
        return out
    F, pts = smile(pc, exp)
    rows = pc.by_expiry(exp)
    out["T"], out["forward"] = rows[0].T, F
    out["smile"] = [{"k": round(p["k"], 6), "strike": p["strike"], "iv": p["iv"], "kind": p["kind"]} for p in pts]
    atm, why = atm_iv(pts)
    out["atm_iv"] = atm
    if why:
        out["reasons"].append(f"ATM IV: {why}")
    c25, wc = delta_iv(pts, "call", cfg.rr_delta)
    p25, wp = delta_iv(pts, "put", cfg.rr_delta)
    for w in (wc, wp):
        if w:
            out["reasons"].append(w)
    if c25 is not None and p25 is not None:
        out["rr25"] = c25 - p25
        if atm is not None:
            out["bf25"] = 0.5 * (c25 + p25) - atm
    out["iv_c25"], out["iv_p25"] = c25, p25
    return out


def expected_move(pc: PricedChain, exp: date, view: dict) -> dict:
    S = pc.chain.spot
    out = {"expiration": exp.isoformat(), "straddle": None, "iv_based": None, "one_sigma": None, "difference": None,
           "strike": None, "iv_based_definition": "sqrt(2/pi) × IV_ATM × sqrt(T) × spot (model ATM straddle)",
           "reasons": []}
    if not view["available"]:
        out["reasons"].append(f"expiry unavailable: {view['reason']}")
        return out
    by_k: Dict[float, Dict[str, Priced]] = {}
    for p in pc.contracts:
        if p.q.expiration == exp and p.in_filter and p.q.mid is not None:
            by_k.setdefault(p.q.strike, {})[p.q.kind] = p
    both = [k for k, d in by_k.items() if "call" in d and "put" in d]
    if both:
        k = min(both, key=lambda x: (abs(x - S), x))
        out["strike"] = k
        out["straddle"] = by_k[k]["call"].q.mid + by_k[k]["put"].q.mid
    else:
        out["reasons"].append("no strike with both a usable call and put mid")
    if view["atm_iv"] is not None and view["T"] is not None and view["T"] > 0:
        out["one_sigma"] = view["atm_iv"] * math.sqrt(view["T"]) * S
        out["iv_based"] = math.sqrt(2.0 / math.pi) * out["one_sigma"]
    else:
        out["reasons"].append("no ATM IV")
    if out["straddle"] is not None and out["iv_based"] is not None:
        out["difference"] = out["straddle"] - out["iv_based"]
    return out


def iv_at_days(views: Sequence[dict], days: int) -> Tuple[Optional[float], Optional[str]]:
    """ATM IV at ``days`` by total-variance interpolation between the two available expiries around it."""
    pts = sorted((v["T"], v["atm_iv"]) for v in views if v["available"] and v["atm_iv"] is not None and v["T"])
    t = days / 365.0
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            w = _interp(t0, t1, v0 * v0 * t0, v1 * v1 * t1, t)
            if w <= 0:
                return None, "non-positive interpolated total variance"
            return math.sqrt(w / t), None
    return None, f"no available expiries around {days} days (no extrapolation)"


def vix_structure(fred: Dict[str, Optional[dict]], symbol: str) -> dict:
    vix, vxv, vxn = fred.get("VIXCLS"), fred.get("VXVCLS"), fred.get("VXNCLS")
    out = {"label": None, "slope": None, "ratio": None, "vix": vix, "vxv": vxv, "vxn": vxn if symbol == "QQQ" else None,
           "note": "S&P 500 VIX (30 d) vs VIX3M (FRED VXVCLS); for QQQ the Nasdaq VXN (30 d) is shown as a level — "
                   "FRED has no 3-month Nasdaq series", "reason": None, "available_ms": None}
    if not vix or not vxv:
        out["reason"] = "VIXCLS / VXVCLS not archived by the slot"
        return out
    if vix["date"] != vxv["date"]:
        out["reason"] = f"VIXCLS ({vix['date']}) and VXVCLS ({vxv['date']}) are of different dates"
        return out
    a, b = float(vix["value"]), float(vxv["value"])
    out["slope"], out["ratio"] = b - a, (a / b if b else None)
    out["label"] = "contango" if b > a else ("backwardation" if b < a else "flat")
    out["available_ms"] = max(vix["available_ms"], vxv["available_ms"])
    return out


def realised_vol(closes: List[Tuple[date, float]], sessions: int, expected_last: date) -> Tuple[Optional[float], Optional[str]]:
    if len(closes) < sessions + 1:
        return None, f"{len(closes)} daily closes cached by the slot, {sessions + 1} needed"
    tail = closes[-(sessions + 1):]
    if tail[-1][0] != expected_last:
        return None, f"newest cached close is {tail[-1][0]}, not the previous session {expected_last}"
    rets = [math.log(b[1] / a[1]) for a, b in zip(tail, tail[1:])]
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var * 252.0), None


def compute(pc: PricedChain, cfg, fred: Dict[str, Optional[dict]], closes, expected_last: Optional[date]) -> dict:
    exps = sorted(pc.expiries)
    views = [expiry_view(pc, x, cfg) for x in exps]
    avail = [v for v in views if v["available"]]
    out = {"expiries": views, "term_structure": [{"expiration": v["expiration"], "days": v["days"], "T": v["T"],
                                                   "atm_iv": v["atm_iv"]} for v in avail],
           "expected_move": {"today": None, "next": None}, "iv30": None, "rv": None, "vrp": None,
           "vix_structure": vix_structure(fred, pc.chain.symbol), "reasons": []}
    sd = pc.chain.slot_date
    future = [x for x, v in zip(exps, views) if x >= sd and v["available"]]
    for name, i in (("today", 0), ("next", 1)):
        if len(future) > i:
            x = future[i]
            out["expected_move"][name] = expected_move(pc, x, views[exps.index(x)])
        else:
            out["reasons"].append(f"expected move ({name}): no available expiry")
    iv30, why = iv_at_days(views, cfg.vrp_target_days)
    out["iv30"] = iv30
    if why:
        out["reasons"].append(f"IV {cfg.vrp_target_days} d: {why}")
    if expected_last is None:
        rv, rwhy = None, "previous session unknown (calendar)"
    else:
        rv, rwhy = realised_vol(closes or [], cfg.rv_sessions, expected_last)
    out["rv"] = {"value": rv, "sessions": cfg.rv_sessions, "definition": "stdev(ddof 1) of close-to-close log "
                 f"returns over {cfg.rv_sessions} sessions × √252, cached daily bars known by the slot",
                 "reason": rwhy}
    if iv30 is not None and rv is not None:
        out["vrp"] = {"value": iv30 - rv, "sign": 1 if iv30 > rv else (-1 if iv30 < rv else 0),
                      "definition": f"IV_ATM({cfg.vrp_target_days} d) − RV({cfg.rv_sessions} sessions)"}
    else:
        out["reasons"].append("VRP unavailable: " + "; ".join(x for x in (why, rwhy) if x))
    return out
