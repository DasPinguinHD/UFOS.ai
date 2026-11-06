"""Gamma exposure (GEX) under three dealer-positioning assumptions in parallel (SP §21.12, task §3).

Convention: USD per 1 % move of the underlying, per contract ``OI × gamma × multiplier × S² × 0.01`` (gamma = the
platform's own BSM gamma at the contract's own IV), signed by the dealer position:

* **A** (baseline, the pre-registration's convention): dealers LONG calls, SHORT puts → calls +, puts −;
* **B**: dealers short ALL options → every contract −;
* **C** (moneyness split): dealers short OTM options and long ITM ones — a call is OTM when K > S, a put when K < S
  (S = the snapshot spot; the positioning is fixed at the snapshot, also on the flip curve) → OTM −, ITM +.

**Total GEX (user decision 2026-11-06, SP §21.3 / §21.12): the sum over ALL archived expiries that pass the
quote-coverage rule**; expiries that fail it are EXCLUDED and listed with their quote coverage ("0-7 d: 2 of 3 expiries
excluded (68 % usable quotes)"), with the excluded share of the unsigned gamma notional (a lower bound — the failed
quotes have no gamma) and the excluded open interest. The regime uses this total under A, with the A–C agreement.
Only contracts with fresh OI reported enter (a contract without (fresh) OI contributes 0 (no OI reported)); the OI
coverage is shown as "OI reported for x %" (Alpaca never reported an explicit 0). **Evaluability (user decision
2026-11-06 b):** the decision time is not evaluable for the regime when the excluded expiries carry more than
``max_excluded_oi_pct`` (= 15) % of the total OI (``total.evaluable``; the regime is then unavailable with the reason
"GEX partial: excluded OI x % > 15 %"). The **bucket view** (``cfg.buckets``; days = calendar
days from the slot date to the expiration) stays strict: a bucket with ANY failing expiry, or without any contract
with OI, is unavailable — never a partial sum presented as complete. Per strike, walls and the flip curve use the
total's contracts.

Gamma flip: the total GEX(S′) re-computed for hypothetical spots S′ = S·(1 + x), x ∈ [−range, +range]
(``flip_points`` grid points; sticky strike: each contract keeps its own IV and time; the dealer signs stay those
of the snapshot) — the V2 curve. The flip = the zero crossing nearest to the spot (linear between the two grid points
around the sign change); none in range → ``null`` with the reason. Call wall / put wall = the strike with the largest
unsigned call / put GEX. "Variants agree" = the total GEX has the same non-zero sign under A, B and C.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Optional

from . import bs
from .model import Priced, PricedChain

VARIANTS = ("A", "B", "C")
VARIANT_TEXT = {"A": "dealers long calls / short puts (calls +, puts −) — pre-registration baseline",
                "B": "dealers short all options (all −)",
                "C": "dealers short OTM options, long ITM (moneyness at the snapshot spot)"}


def dealer_sign(variant: str, kind: str, strike: float, spot: float) -> int:
    if variant == "A":
        return 1 if kind == "call" else -1
    if variant == "B":
        return -1
    if variant == "C":
        otm = strike > spot if kind == "call" else strike < spot
        return -1 if otm else 1
    raise ValueError(f"unknown dealer variant {variant!r}")


def contract_gex(p: Priced, S_eval: float, r: float) -> float:
    """Unsigned GEX of one contract at the (hypothetical) spot ``S_eval``: OI × gamma(S_eval) × mult × S_eval² × 0.01
    (the contract's own IV and its expiry's implied dividend yield)."""
    g = bs.gamma(S_eval, p.q.strike, p.T, r, p.div_q, p.iv)
    return p.q.oi * bs.dollar_gamma(g, S_eval, p.q.multiplier)


def _bucket_of(days: int, buckets) -> Optional[str]:
    for label, lo, hi in buckets:
        if lo <= days <= hi:
            return label
    return None


def oi_text(n_oi: int, n: int) -> Optional[str]:
    return f"OI reported for {100.0 * n_oi / n:.1f} % of the contracts" if n else None


def compute(pc: PricedChain, cfg) -> dict:
    S = pc.chain.spot
    out = {"convention": "USD per 1 % move: OI × gamma × multiplier × spot² × 0.01", "spot": S,
           "variants": dict(VARIANT_TEXT), "total": None, "buckets": {}, "per_strike": [], "walls": None,
           "flip": None, "flip_curve": None, "agree": None, "signs": None, "available": False, "status": "unavailable",
           "reasons": []}
    if S is None:
        out["reasons"].append("no spot")
        return out
    exps = pc.expiries
    r = pc.rate
    contracts = [p for p in pc.contracts if p.in_filter and p.gex_ok]
    labels = [b[0] for b in cfg.buckets]
    members: Dict[str, list] = {lab: [] for lab in labels}
    bucket_of = {}
    for exp, e in exps.items():
        lab = _bucket_of(e["days"], cfg.buckets)
        bucket_of[exp] = lab
        if lab:
            members[lab].append(exp)
    # --- bucket view (strict: a bucket with a failing expiry is unavailable) --------------------------------------
    for lab in labels:
        ex = sorted(members[lab])
        bad = [exps[x] for x in ex if not exps[x]["available_iv"]]
        n_in = sum(exps[x]["contracts"] for x in ex)
        n_oi = sum(exps[x]["with_oi"] for x in ex)
        n_iv = sum(exps[x]["usable_iv"] for x in ex)
        n_gex = sum(exps[x]["usable_gex"] for x in ex)
        b = {"expiries": [x.isoformat() for x in ex], "available": False, "reason": None, "total": None,
             "oi_coverage": {"contracts": n_in, "with_oi": n_oi, "share_pct": round(100.0 * n_oi / n_in, 2) if n_in else None,
                             "text": oi_text(n_oi, n_in), "usable_with_oi": n_gex,
                             "share_of_usable_pct": round(100.0 * n_gex / n_iv, 2) if n_iv else None}}
        if not ex:
            b["reason"] = "no archived expiry in this bucket"
        elif bad:
            b["reason"] = "; ".join(f"{e['expiration']}: {e['reason_iv']}" for e in bad)[:600]
        elif n_gex == 0:
            b["reason"] = "no contract in this bucket has open interest reported (and a usable quote)"
        else:
            b["available"] = True
            tot = {v: 0.0 for v in VARIANTS}
            for p in contracts:
                if p.q.expiration in ex:
                    g = contract_gex(p, S, r)
                    for v in VARIANTS:
                        tot[v] += dealer_sign(v, p.q.kind, p.q.strike, S) * g
            b["total"] = {v: tot[v] for v in VARIANTS}
            b["contracts"] = sum(1 for p in contracts if p.q.expiration in ex)
        out["buckets"][lab] = b
    # --- total GEX = sum over the quote-qualified expiries (user decision 2026-11-06, SP §21.3 / §21.12) ----------
    inc = sorted(x for x in exps if exps[x]["available_iv"])
    exc = sorted(x for x in exps if not exps[x]["available_iv"])
    used = [p for p in contracts if p.q.expiration in inc]
    n_all = sum(exps[x]["contracts"] for x in exps)
    n_oi_all = sum(exps[x]["with_oi"] for x in exps)
    # excluded share: unsigned gamma notional of the excluded expiries' contracts that still have an own IV + OI (the
    # failed quotes have no IV → a LOWER bound), and the excluded OI (all contracts with OI, quotes or not)
    exc_gex = sum(contract_gex(p, S, r) for p in contracts if p.q.expiration in exc)
    inc_gex = sum(contract_gex(p, S, r) for p in used)
    oi_of = lambda xs: sum(p.q.oi for p in pc.contracts if p.in_filter and p.has_oi and p.q.expiration in xs)  # noqa
    oi_exc, oi_tot = oi_of(set(exc)), oi_of(set(exps))
    per_bucket = {}
    for x in exc:
        per_bucket.setdefault(bucket_of.get(x) or "other", []).append(exps[x]["coverage_iv_pct"])
    summary = []
    for lab in labels + ["other"]:
        if lab in per_bucket:
            cv = per_bucket[lab]
            n_b = len(members.get(lab, [])) or len(cv)
            rng = f"{min(cv):.0f} %" if min(cv) == max(cv) else f"{min(cv):.0f}–{max(cv):.0f} %"
            summary.append(f"{lab} d: {len(cv)} of {n_b} expiries excluded ({rng} usable quotes)")
    t = {"rule": "sum over all archived expiries that pass the quote-coverage rule; failing expiries are excluded and "
                 "listed (user decision 2026-11-06)",
         "included": [x.isoformat() for x in inc],
         "excluded": [{"expiration": x.isoformat(), "days": exps[x]["days"], "bucket": bucket_of.get(x),
                       "coverage_iv_pct": exps[x]["coverage_iv_pct"], "reason": exps[x]["reason_iv"]} for x in exc],
         "excluded_text": "; ".join(summary) or None,
         "excluded_gamma_share_pct": (round(100.0 * exc_gex / (exc_gex + inc_gex), 2)
                                      if exc and (exc_gex + inc_gex) > 0 and exc_gex > 0 else (0.0 if not exc else None)),
         "excluded_gamma_note": "lower bound: contracts of excluded expiries whose quotes failed have no gamma",
         "excluded_oi": {"contracts_with_oi": sum(exps[x]["with_oi"] for x in exc), "open_interest": oi_exc,
                         "share_of_total_oi_pct": round(100.0 * oi_exc / oi_tot, 2) if oi_tot else None},
         "oi_coverage": {"contracts": n_all, "with_oi": n_oi_all, "text": oi_text(n_oi_all, n_all),
                         "share_pct": round(100.0 * n_oi_all / n_all, 2) if n_all else None},
         "contracts": len(used), "value": None, "status": "unavailable", "reason": None}
    out["total"] = t
    share, lim = t["excluded_oi"]["share_of_total_oi_pct"], float(cfg.max_excluded_oi_pct)
    ev = {"rule": f"not evaluable when the excluded expiries carry > {lim:g} % of the total OI (user decision "
                  "2026-11-06 b)", "max_excluded_oi_pct": lim, "excluded_oi_pct": share, "ok": False, "reason": None}
    if share is None:
        ev["reason"] = "GEX not evaluable: no open interest reported"
    elif share > lim:
        ev["reason"] = f"GEX partial: excluded OI {share:.1f} % > {lim:g} %" + \
            (f" ({t['excluded_text']})" if t["excluded_text"] else "")
    else:
        ev["ok"] = True
    t["evaluable"] = ev
    if not inc:
        t["reason"] = "no archived expiry passes the quote-coverage rule"
    elif not used:
        t["reason"] = "no quote-qualified contract has open interest reported"
    else:
        tot = {v: 0.0 for v in VARIANTS}
        for p in used:
            g = contract_gex(p, S, r)
            for v in VARIANTS:
                tot[v] += dealer_sign(v, p.q.kind, p.q.strike, S) * g
        t["value"] = tot
        t["status"] = "partial" if exc else "ok"
    out["available"] = t["value"] is not None
    out["status"] = t["status"]
    if not out["available"]:
        out["reasons"].append(f"GEX unavailable: {t['reason']}")
        return out
    if exc:
        out["reasons"].append(f"partial: {t['excluded_text']}")
    strikes: Dict[float, dict] = defaultdict(lambda: {"call": 0.0, "put": 0.0, **{v: 0.0 for v in VARIANTS}})
    for p in used:
        g = contract_gex(p, S, r)
        row = strikes[p.q.strike]
        row[p.q.kind] += g
        for v in VARIANTS:
            row[v] += dealer_sign(v, p.q.kind, p.q.strike, S) * g
    out["per_strike"] = [{"strike": k, "call_gex": row["call"], "put_gex": row["put"],
                          **{f"gex_{v}": row[v] for v in VARIANTS}} for k, row in sorted(strikes.items())]
    cw = max(out["per_strike"], key=lambda x: x["call_gex"])
    pw = max(out["per_strike"], key=lambda x: x["put_gex"])
    out["walls"] = {"call_wall": cw["strike"] if cw["call_gex"] > 0 else None,
                    "put_wall": pw["strike"] if pw["put_gex"] > 0 else None}
    totals = t["value"]
    signs = {v: (0 if totals[v] == 0 else (1 if totals[v] > 0 else -1)) for v in VARIANTS}
    out["agree"] = len(set(signs.values())) == 1 and 0 not in signs.values()
    out["signs"] = signs
    out["flip_curve"], out["flip"] = flip_curve(used, S, r, cfg)
    return out


def flip_curve(contracts: List[Priced], S: float, r: float, cfg) -> tuple:
    n = int(cfg.flip_points)
    rng = float(cfg.flip_range_pct) / 100.0
    xs = [-rng + 2.0 * rng * i / (n - 1) for i in range(n)]
    curve = {v: [] for v in VARIANTS}
    spots = [S * (1.0 + x) for x in xs]
    for Se in spots:
        tot = {v: 0.0 for v in VARIANTS}
        for p in contracts:
            g = contract_gex(p, Se, r)
            for v in VARIANTS:
                tot[v] += dealer_sign(v, p.q.kind, p.q.strike, S) * g
        for v in VARIANTS:
            curve[v].append(tot[v])
    flips = {v: _flip(spots, curve[v], S, rng) for v in VARIANTS}
    return {"spots": spots, **{v: curve[v] for v in VARIANTS}}, flips


def _flip(spots: List[float], values: List[float], S: float, rng: float) -> dict:
    crossings = []
    for i in range(len(spots) - 1):
        a, b = values[i], values[i + 1]
        if a == 0:
            crossings.append(spots[i])
        elif (a < 0 < b) or (a > 0 > b):
            crossings.append(spots[i] + (spots[i + 1] - spots[i]) * (-a) / (b - a))
    if values and values[-1] == 0:
        crossings.append(spots[-1])
    crossings = sorted(set(crossings))
    if not crossings:
        return {"level": None, "crossings": [], "reason": f"no sign change of total GEX within ±{100 * rng:g} % of "
                                                          "the spot"}
    level = min(crossings, key=lambda c: abs(c - S))
    return {"level": level, "crossings": crossings, "distance_pct": 100.0 * (level / S - 1.0), "reason": None}


def sign_of(x: Optional[float]) -> Optional[int]:
    if x is None or not math.isfinite(x):
        return None
    return 0 if x == 0 else (1 if x > 0 else -1)
