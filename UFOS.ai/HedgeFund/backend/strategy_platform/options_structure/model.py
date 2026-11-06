"""Implied dividend yield per expiry, own IV and greeks for every usable quote of a chain, and coverage per expiry
(SP §21.12, task §1–2; user decisions 2026-11-06).

A contract is IN the filter when it is not ``expired`` and not ``outside_band``.

* **Implied q per expiry** (put-call parity, European form): for every strike within ±``parity_band_pct`` of the
  spot where BOTH the call and the put have a usable mid, ``q_K = −ln((C − P + K e^{−rT}) / S) / T``; the expiry's q
  is the MEDIAN of these (robust to single bad quotes); fewer than ``parity_min_pairs`` pairs → the expiry is
  unavailable ("no implied dividend yield") — no fallback to the fixed per-symbol yield (that is a cross-check only).
  SPY / QQQ options are American: early exercise makes parity approximate; the implied q is a market-consistent
  carry, not a dividend forecast. **Quality flag** (``q_flags``): |q| > ``q_flag_abs_pct`` (``abs_q``) or the median
  absolute deviation of the pair values > ``q_flag_mad_pct`` (``pair_dispersion``) → the expiry is FLAGGED and shown
  in the quality view (``q_flagged_expiries``); it stays in use — flagged + shown, never silently dropped or used
  silently (thresholds PROVISIONAL; decision documented in SP §21.12).
* **Surface coverage** = in-filter contracts with a usable mid and an own IV / in-filter contracts; below the
  archive's ``coverage_min_pct`` (PROVISIONAL) the expiry is ``unavailable`` (quote quality gating).
* **OI coverage** = in-filter contracts with fresh point-in-time OI (+ multiplier) / in-filter contracts — shown, NOT
  gating (user decision 2026-11-06): a contract without (fresh) OI contributes 0 (no OI reported); a bucket is
  unavailable only when its quotes fail the coverage minimum or no contract in it has OI.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from . import bs
from .quotes import Chain, Quote


@dataclass
class Priced:
    q: Quote
    T: float
    in_filter: bool
    div_q: Optional[float] = None       # the expiry's implied dividend yield (None → no IV)
    iv: Optional[float] = None
    iv_reason: Optional[str] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    vega: Optional[float] = None
    theta: Optional[float] = None

    @property
    def has_oi(self) -> bool:
        return self.q.oi is not None and self.q.multiplier is not None and self.q.oi_reason is None

    @property
    def gex_ok(self) -> bool:
        return self.iv is not None and self.has_oi


@dataclass
class PricedChain:
    chain: Chain
    rate: float
    contracts: List[Priced]
    q_by_expiry: Dict[date, dict] = field(default_factory=dict)
    expiries: Dict[date, dict] = field(default_factory=dict)

    def q_of(self, exp: date) -> Optional[float]:
        return (self.q_by_expiry.get(exp) or {}).get("q")

    def by_expiry(self, exp: date, *, gex: bool = False, iv: bool = True) -> List[Priced]:
        out = [p for p in self.contracts if p.q.expiration == exp and p.in_filter]
        if gex:
            return [p for p in out if p.gex_ok]
        return [p for p in out if p.iv is not None] if iv else out


def implied_q(quotes: List[Quote], S: float, T: float, rate: float, cfg) -> dict:
    """The parity-implied dividend yield of one expiry (median over near-ATM pairs) with its inputs."""
    by_k: Dict[float, Dict[str, Quote]] = {}
    for q in quotes:
        if q.mid is not None and abs(q.strike / S - 1.0) * 100.0 <= cfg.parity_band_pct + 1e-12:
            by_k.setdefault(q.strike, {})[q.kind] = q
    vals = []
    for k, d in sorted(by_k.items()):
        if "call" in d and "put" in d and T > 0:
            x = bs.parity_dividend_yield(d["call"].mid, d["put"].mid, S, k, T, rate)
            if x is not None and math.isfinite(x):
                vals.append(x)
    out = {"q": None, "pairs": len(vals), "band_pct": cfg.parity_band_pct, "min_pairs": cfg.parity_min_pairs,
           "mad": None, "flags": [], "reason": None}
    if len(vals) < cfg.parity_min_pairs:
        out["reason"] = (f"no implied dividend yield: {len(vals)} put-call pair(s) with both mids usable within "
                         f"±{cfg.parity_band_pct:g} % of the spot (≥ {cfg.parity_min_pairs} needed)")
        return out
    med = statistics.median(vals)
    out["q"], out["mad"] = med, statistics.median(abs(v - med) for v in vals)
    if abs(med) * 100.0 > cfg.q_flag_abs_pct:
        out["flags"].append(f"abs_q: |q| = {abs(med) * 100.0:.2f} % > {cfg.q_flag_abs_pct:g} %")
    if out["mad"] * 100.0 > cfg.q_flag_mad_pct:
        out["flags"].append(f"pair_dispersion: MAD = {out['mad'] * 100.0:.2f} % > {cfg.q_flag_mad_pct:g} %")
    return out


def price_chain(chain: Chain, rate: float, cfg, coverage_min_pct: float) -> PricedChain:
    S = chain.spot
    contracts = [Priced(q, q.t_years(chain.slot_ms), in_filter=not ({"expired", "outside_band"} & set(q.flags)))
                 for q in chain.quotes]
    pc = PricedChain(chain, rate, contracts)
    exps = sorted({p.q.expiration for p in contracts if p.in_filter})
    if S is not None and S > 0:
        for exp in exps:
            rows = [p for p in contracts if p.q.expiration == exp and p.in_filter]
            pc.q_by_expiry[exp] = implied_q([p.q for p in rows], S, rows[0].T, rate, cfg)
    for p in contracts:
        qd = pc.q_by_expiry.get(p.q.expiration) or {}
        p.div_q = qd.get("q")
        if p.q.mid is None:
            p.iv_reason = "quote not usable: " + ", ".join(p.q.flags or ["no quote"])
        elif S is None or S <= 0:
            p.iv_reason = "no spot"
        elif p.div_q is None:
            p.iv_reason = qd.get("reason") or "no implied dividend yield for this expiry"
        else:
            try:
                p.iv = bs.implied_vol(p.q.kind, p.q.mid, S, p.q.strike, p.T, rate, p.div_q, cfg.iv_min, cfg.iv_max)
                g = bs.greeks(p.q.kind, S, p.q.strike, p.T, rate, p.div_q, p.iv)
                p.delta, p.gamma, p.vega, p.theta = g.delta, g.gamma, g.vega, g.theta
            except (bs.NoIV, ValueError) as ex:
                p.iv, p.iv_reason = None, str(ex)[:200]
    for exp in exps:
        rows = [p for p in contracts if p.q.expiration == exp and p.in_filter]
        n = len(rows)
        n_iv = sum(1 for p in rows if p.iv is not None)
        n_oi = sum(1 for p in rows if p.has_oi)
        n_gex = sum(1 for p in rows if p.gex_ok)
        cov_iv = 100.0 * n_iv / n
        qd = pc.q_by_expiry.get(exp) or {}
        e = {"expiration": exp.isoformat(), "days": (exp - chain.slot_date).days, "contracts": n,
             "usable_iv": n_iv, "with_oi": n_oi, "usable_gex": n_gex, "coverage_iv_pct": round(cov_iv, 2),
             "oi_coverage_pct": round(100.0 * n_oi / n, 2), "implied_q": qd.get("q"), "q_pairs": qd.get("pairs"),
             "q_mad": qd.get("mad"), "q_flags": list(qd.get("flags") or []),
             "available_iv": cov_iv >= coverage_min_pct and qd.get("q") is not None}
        if qd.get("q") is None:
            e["reason_iv"] = qd.get("reason") or "no implied dividend yield"
        elif not e["available_iv"]:
            e["reason_iv"] = (f"{n_iv} of {n} contracts with a usable mid and own IV ({cov_iv:.1f} % < "
                              f"{coverage_min_pct:g} %)")
        else:
            e["reason_iv"] = None
        pc.expiries[exp] = e
    return pc


def vendor_iv_stats(pc: PricedChain) -> dict:
    """Own IV minus the vendor's (indicative snapshot) IV, in vol points ×100 — a data-quality view, no replacement."""
    d = [100.0 * (p.iv - p.q.vendor_iv) for p in pc.contracts
         if p.iv is not None and p.q.vendor_iv is not None and math.isfinite(p.q.vendor_iv)]
    if not d:
        return {"n": 0, "mean": None, "median": None, "mean_abs": None, "p95_abs": None}
    a = sorted(abs(x) for x in d)
    return {"n": len(d), "mean": round(statistics.fmean(d), 4), "median": round(statistics.median(d), 4),
            "mean_abs": round(statistics.fmean(a), 4), "p95_abs": round(a[min(len(a) - 1, int(0.95 * len(a)))], 4)}
