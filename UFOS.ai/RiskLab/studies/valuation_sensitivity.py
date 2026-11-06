"""Reverse DCF and sensitivity analysis: what a price implies, how often that is achieved, and which
assumptions drive the answer - plus the same sensitivity tools applied to the IRRBB and credit models.

SYNTHETIC EXAMPLE ONLY: "Synthetic Co." below is an invented company with round numbers (free cash
flow 100, net debt 300, other claims 50, 100 shares, discount rate 9 %, terminal growth 2 %, a
10-year explicit period; currency units arbitrary). No market or company data; for a real company
the inputs are user-provided at run time (:mod:`risklab.valuation`).

Parts:

A. **Implied growth vs price.** For prices from 10 to 60 per share: the implied constant 10-year
   FCF growth, the implied start growth of a linear fade to the terminal growth (with its 10-year
   CAGR), and the implied discount rate at a fixed 5 % growth.
B. **Base rates.** How often firms achieve the implied 10-year CAGR in the synthetic universe of
   the v0.4 survivorship study (:mod:`risklab.valuation.base_rates`): survivors only, full cohort
   (exits = misses) and counterfactual; the curve over hurdles and the exit-hazard strength kappa
   (0, 1, 2) at the reference price. M cohorts of 2000 firms. FCF growth is compared with revenue
   growth (constant FCF margin assumed).
C. **One- and two-way sensitivity** of the value per share (at the growth implied by the reference
   price, so the base value equals the price) and of the implied growth itself: tornado over the
   discount rate, terminal growth, growth, FCF, net debt and the explicit-period length; two-way
   tables over discount rate x terminal growth.
D. **Sobol indices** (Saltelli sampling, Jansen total-order estimator, bootstrap errors;
   :mod:`risklab.sensitivity`) of the value per share over the SAME six inputs and ranges as the
   tornado of part C (the explicit period as a discrete uniform 7..13 years), and the same
   estimator on the Ishigami function against its analytic indices (a validation of the estimator).
E. **Cross-module use** of the same tools: (i) the worst Delta EVE / Tier 1 of the v0.7 synthetic
   bank against the NMD assumptions (tornado + Sobol); (ii) the IFRS 9 ECL coverage of a 200-loan
   synthetic book (v0.8) against the scenario weight and severity, the SICR threshold and LGD / PD
   scaling (tornado + Sobol). The ECL goes through ``ifrs9.portfolio_ecl`` and therefore through
   the v0.8 staging on the same basis (``pd_now`` vs ``pd_orig``, no scenario overlay): the scenario
   weight and severity change the amount, not the stage; ``pd_scale`` scales the current PD only and
   so does move loans into stage 2 - it represents a deterioration since origination (a test checks
   both).

Run from ``UFOS.ai/RiskLab/``::

    python studies/valuation_sensitivity.py                # default: M = 200 cohorts, N = 4096 (DCF Sobol)
    python studies/valuation_sensitivity.py --mode full    # M = 2000, N = 65 536; N = 1024 / 2048 cross-module

Measured run times in the build container: see the README studies table.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:  # path shim: runs without installing the package
    sys.path.insert(0, _SRC)

import numpy as np

import risklab
from risklab import sensitivity as SA
from risklab.conventions import make_rng
from risklab.credit import ifrs9 as F
from risklab.rates import curve as C
from risklab.rates import instruments as I
from risklab.rates import irrbb as R
from risklab.report.export import (code_fingerprint, default_out_dir, save_full_results, table_to_csv,
                                   table_to_json, text_to_file)
from risklab.valuation import base_rates as BR
from risklab.valuation import dcf as D
from risklab.valuation import reverse as RV

STUDY = "valuation_sensitivity"
# mode: (cohorts M, DCF / Ishigami Sobol N, IRRBB Sobol N, ECL Sobol N)
MODES = {"smoke": (5, 256, 8, 8), "default": (200, 4096, 64, 32), "full": (2000, 65536, 1024, 2048)}
COLUMNS = ["table", "price", "implied_growth", "implied_fade_start", "fade_cagr", "implied_discount_rate",
           "hurdle", "kappa", "m", "survival_rate", "survivors_only", "survivors_only_se", "full_cohort",
           "full_cohort_se", "counterfactual", "counterfactual_se", "model", "output", "name", "low", "high",
           "base", "y_low", "y_high", "base_y", "swing", "x_name", "x", "y_name", "y", "z", "n", "evaluations",
           "first", "first_se", "first_lo", "first_hi", "total", "total_se", "total_lo", "total_hi",
           "analytic_first", "analytic_total", "variance", "mean"]

#: the synthetic company (labelled synthetic everywhere it appears)
COMPANY = {"name": "Synthetic Co. (invented, not a real company)", "fcf0": 100.0, "discount_rate": 0.09,
           "terminal_growth": 0.02, "years": 10, "net_debt": 300.0, "other_claims": 50.0, "shares": 100.0}
CURVE = C.NelsonSiegel(0.030, -0.015, 0.010, 2.0)      # the v0.7 study's base curve


@dataclass
class ValuationConfig:
    m: int = MODES["default"][0]
    n_sobol: int = MODES["default"][1]
    n_irrbb: int = MODES["default"][2]
    n_ecl: int = MODES["default"][3]
    seed: int = 20261112
    n_boot: int = 500
    reference_price: float = 30.0
    prices: Tuple[float, ...] = tuple(float(x) for x in np.arange(10.0, 60.01, 2.5))
    fixed_growth_for_r: float = 0.05
    hurdles: Tuple[float, ...] = tuple(round(float(x), 4) for x in np.arange(-0.05, 0.2501, 0.01))
    kappas: Tuple[float, ...] = (0.0, 1.0, 2.0)
    r_grid: Tuple[float, ...] = (0.07, 0.075, 0.08, 0.085, 0.09, 0.095, 0.10, 0.105, 0.11)
    gt_grid: Tuple[float, ...] = (0.0, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03)
    n_loans: int = 200


def _seed(cfg: ValuationConfig, *key: int) -> int:
    return int(np.random.SeedSequence([cfg.seed, *key]).generate_state(1)[0])


def company_inputs(growth: float = 0.05, fade: bool = False) -> D.DCFInputs:
    c = COMPANY
    return D.DCFInputs(fcf0=c["fcf0"], discount_rate=c["discount_rate"], terminal_growth=c["terminal_growth"],
                       growth=growth, years=c["years"], fade=fade, net_debt=c["net_debt"],
                       other_claims=c["other_claims"], shares=c["shares"])


# ---- A / B: implied growth and base rates ------------------------------------------------------

def part_a(cfg: ValuationConfig) -> List[dict]:
    rows = []
    base = company_inputs()
    for p in cfg.prices:
        g = RV.implied_growth(p, base, "constant")
        gf = RV.implied_growth(p, base, "fade_start")
        r = RV.implied_discount_rate(p, base.replace(growth=cfg.fixed_growth_for_r))
        rows.append({"table": "implied", "price": p, "implied_growth": g, "implied_fade_start": gf,
                     "fade_cagr": D.explicit_cagr(base.replace(growth=gf, fade=True)), "implied_discount_rate": r})
    return rows


def part_b(cfg: ValuationConfig, progress=None) -> List[dict]:
    rows = []
    g_ref = RV.implied_growth(cfg.reference_price, company_inputs(), "constant")
    for i, k in enumerate(cfg.kappas):
        coh = BR.simulate_cohorts(BR.GrowthUniverse(kappa=k), cfg.m, _seed(cfg, 2, i))
        hurdles = cfg.hurdles if k == 1.0 else ()
        for h in hurdles:
            rows.append({"table": "base_rate_curve", "kappa": k, **coh.base_rates(h)})
        rows.append({"table": "base_rate_ref", "kappa": k, "price": cfg.reference_price, **coh.base_rates(g_ref)})
        if progress:
            progress(f"B: kappa = {k} done")
    return rows


# ---- C: tornado and two-way --------------------------------------------------------------------

def _value(p: Dict[str, float]) -> float:
    return D.value_per_share(D.DCFInputs(fcf0=p["fcf0"], discount_rate=p["discount_rate"],
                                         terminal_growth=p["terminal_growth"], growth=p["growth"],
                                         years=int(round(p["years"])), net_debt=p["net_debt"],
                                         other_claims=COMPANY["other_claims"], shares=COMPANY["shares"]))


def _implied(price: float) -> Callable[[Dict[str, float]], float]:
    def f(p: Dict[str, float]) -> float:
        inp = D.DCFInputs(fcf0=p["fcf0"], discount_rate=p["discount_rate"], terminal_growth=p["terminal_growth"],
                          growth=0.05, years=int(round(p["years"])), net_debt=p["net_debt"],
                          other_claims=COMPANY["other_claims"], shares=COMPANY["shares"])
        return RV.implied_growth(price, inp, "constant")
    return f


def dcf_base(cfg: ValuationConfig) -> Dict[str, float]:
    g_ref = RV.implied_growth(cfg.reference_price, company_inputs(), "constant")
    return {"fcf0": COMPANY["fcf0"], "discount_rate": COMPANY["discount_rate"],
            "terminal_growth": COMPANY["terminal_growth"], "growth": g_ref, "years": float(COMPANY["years"]),
            "net_debt": COMPANY["net_debt"]}


def dcf_ranges(base: Dict[str, float]) -> Dict[str, Tuple[float, float]]:
    return {"discount_rate": (base["discount_rate"] - 0.01, base["discount_rate"] + 0.01),
            "terminal_growth": (base["terminal_growth"] - 0.01, base["terminal_growth"] + 0.01),
            "growth": (base["growth"] - 0.03, base["growth"] + 0.03),
            "fcf0": (0.9 * base["fcf0"], 1.1 * base["fcf0"]),
            "net_debt": (0.5 * base["net_debt"], 1.5 * base["net_debt"]),
            "years": (7.0, 13.0)}


def _bars(model: str, output: str, bars: Sequence[SA.TornadoBar], base: Dict[str, float]) -> List[dict]:
    return [{"table": "tornado", "model": model, "output": output, "name": b.name, "low": b.low, "high": b.high,
             "base": base[b.name], "y_low": b.y_low, "y_high": b.y_high, "base_y": b.base_y, "swing": b.swing}
            for b in bars]


def part_c(cfg: ValuationConfig) -> List[dict]:
    base = dcf_base(cfg)
    rng_ = dcf_ranges(base)
    rows = _bars("dcf", "value_per_share", SA.tornado(_value, base, rng_), base)
    no_g = {k: v for k, v in rng_.items() if k != "growth"}
    rows += _bars("dcf", "implied_growth", SA.tornado(_implied(cfg.reference_price), base, no_g), base)
    for output, f in (("value_per_share", _value), ("implied_growth", _implied(cfg.reference_price))):
        t = SA.two_way(f, base, "discount_rate", cfg.r_grid, "terminal_growth", cfg.gt_grid)
        for i, yv in enumerate(t.y_values):
            for j, xv in enumerate(t.x_values):
                rows.append({"table": "two_way", "model": "dcf", "output": output, "x_name": t.x_name, "x": float(xv),
                             "y_name": t.y_name, "y": float(yv), "z": float(t.z[i, j])})
    return rows


# ---- D: Sobol (DCF and Ishigami) ---------------------------------------------------------------

def dcf_dists(base: Dict[str, float]) -> Dict[str, object]:
    """The tornado ranges of part C (:func:`dcf_ranges`) as independent uniform inputs; the explicit period
    as a discrete uniform over the whole years."""
    rg = dcf_ranges(base)
    out = {k: SA.Uniform(*rg[k]) for k in ("discount_rate", "terminal_growth", "growth", "fcf0", "net_debt")}
    out["years"] = SA.DiscreteUniform(int(rg["years"][0]), int(rg["years"][1]))
    return out


def _dcf_vec(p: Dict[str, np.ndarray]) -> np.ndarray:
    years = np.asarray(p["years"])
    out = np.empty(years.shape)
    for n in np.unique(years):           # the DCF takes the explicit period as a scalar
        j = years == n
        out[j] = D.per_share_vec(p["fcf0"][j], p["discount_rate"][j], p["terminal_growth"][j], p["growth"][j],
                                 int(n), False, p["net_debt"][j], COMPANY["other_claims"], COMPANY["shares"])
    return out


def ishigami_analytic(a: float = 7.0, b: float = 0.1) -> Dict[str, Tuple[float, float]]:
    pi = math.pi
    v = a * a / 8 + b * pi ** 4 / 5 + b * b * pi ** 8 / 18 + 0.5
    v1, v2, v13 = 0.5 * (1 + b * pi ** 4 / 5) ** 2, a * a / 8, b * b * pi ** 8 * (1 / 18 - 1 / 50)
    return {"x1": (v1 / v, (v1 + v13) / v), "x2": (v2 / v, v2 / v), "x3": (0.0, v13 / v)}


def _sobol_rows(model: str, output: str, res: SA.SobolResult, analytic=None) -> List[dict]:
    out = []
    for r in res.as_rows():
        row = {"table": "sobol", "model": model, "output": output, "n": res.n, "evaluations": res.evaluations,
               "variance": res.variance, "mean": res.mean, **r}
        if analytic:
            row["analytic_first"], row["analytic_total"] = analytic[r["name"]]
        out.append(row)
    return out


def part_d(cfg: ValuationConfig) -> List[dict]:
    base = dcf_base(cfg)
    rows = _sobol_rows("dcf", "value_per_share",
                       SA.sobol_indices(_dcf_vec, dcf_dists(base), cfg.n_sobol, _seed(cfg, 4, 0), cfg.n_boot))
    rows += _sobol_rows("ishigami", "y", SA.sobol_indices(SA.ishigami, SA.ISHIGAMI_INPUTS, cfg.n_sobol,
                                                          _seed(cfg, 4, 1), cfg.n_boot), ishigami_analytic())
    return rows


# ---- E: cross-module ---------------------------------------------------------------------------

IRRBB_BASE = {"ca_core_share": 0.70, "ca_avg_maturity": 3.0, "sav_core_share": 0.50, "sav_avg_maturity": 2.0}
IRRBB_RANGES = {"ca_core_share": (0.5, 0.9), "ca_avg_maturity": (1.0, 5.0), "sav_core_share": (0.3, 0.7),
                "sav_avg_maturity": (1.0, 4.5)}


def worst_eve_tier1(p: Dict[str, float]) -> float:
    """Worst Delta EVE decline / Tier 1 of the v0.7 reference bank (EUR_BCBS2016, with the floor) for given NMD
    assumptions of the current accounts (ca_*) and the savings (sav_*), all inside the NMD caps."""
    import dataclasses
    bs = I.synthetic_balance_sheet(nmd_core_share=p["ca_core_share"], nmd_avg_maturity=p["ca_avg_maturity"])
    liabs = [dataclasses.replace(x, core_share=p["sav_core_share"], avg_maturity=p["sav_avg_maturity"])
             if isinstance(x, I.NonMaturityDeposit) and x.category == "retail_non_transactional" else x
             for x in bs.liabilities]
    bs = I.BalanceSheet(bs.assets, liabs, bs.tier1, bs.description)
    return float(R.outlier_tests(R.run_scenarios(bs, CURVE, "EUR_BCBS2016"))["eve_ratio"])


def loan_book(cfg: ValuationConfig) -> List[F.Loan]:
    """A synthetic 200-loan book built like the v0.8 credit study's book (own seed)."""
    rng = make_rng(_seed(cfg, 5, 0))
    loans = []
    for _ in range(cfg.n_loans):
        pd0 = float(np.clip(np.exp(rng.normal(np.log(0.01), 0.8)), 0.0005, 0.2))
        pdn = float(np.clip(pd0 * np.exp(rng.normal(0.1, 0.6)), 0.0003, 0.5))
        u = rng.random()
        dpd = 90 if u < 0.01 else (30 if u < 0.03 else 0)
        loans.append(F.Loan(ead=float(rng.uniform(0.05, 2.0)), eir=float(rng.uniform(0.03, 0.07)),
                            maturity=int(rng.integers(1, 11)), lgd=float(rng.choice([0.25, 0.45])),
                            pd_now=pdn, pd_orig=pd0, dpd=dpd, amortising=bool(rng.random() < 0.7)))
    return loans


ECL_BASE = {"downside_weight": 0.3, "downside_severity": 1.0, "sicr_multiple": 3.0, "lgd_scale": 1.0, "pd_scale": 1.0}
ECL_RANGES = {"downside_weight": (0.1, 0.5), "downside_severity": (0.5, 1.5), "sicr_multiple": (2.0, 4.0),
              "lgd_scale": (0.8, 1.2), "pd_scale": (0.8, 1.2)}
DOWNSIDE = (1.8, 1.5, 1.2)


def ecl_coverage(loans: Sequence[F.Loan]) -> Callable[[Dict[str, float]], float]:
    """ECL / EAD of the book. ``downside_severity`` s scales the downside overlay: multiplier 1 + s (m - 1);
    ``pd_scale`` scales the CURRENT PD only (a deterioration or improvement since origination)."""
    import dataclasses

    def f(p: Dict[str, float]) -> float:
        w = p["downside_weight"]
        sc = (F.Scenario("upside", 0.2, (0.8, 0.9)), F.Scenario("base", round(0.8 - w, 12), ()),
              F.Scenario("downside", w, tuple(1.0 + p["downside_severity"] * (m - 1.0) for m in DOWNSIDE)))
        book = [dataclasses.replace(ln, lgd=min(1.0, ln.lgd * p["lgd_scale"]),
                                    pd_now=min(1.0, ln.pd_now * p["pd_scale"])) for ln in loans]
        return float(F.portfolio_ecl(book, sc, F.StagingRules(sicr_multiple=p["sicr_multiple"]))["coverage"])
    return f


def part_e(cfg: ValuationConfig, progress=None) -> List[dict]:
    rows = _bars("irrbb", "worst_eve_tier1", SA.tornado(worst_eve_tier1, IRRBB_BASE, IRRBB_RANGES), IRRBB_BASE)
    d_irrbb = {k: SA.Uniform(*v) for k, v in IRRBB_RANGES.items()}
    rows += _sobol_rows("irrbb", "worst_eve_tier1", SA.sobol_indices(SA.rowwise(worst_eve_tier1), d_irrbb,
                                                                    cfg.n_irrbb, _seed(cfg, 6, 0), cfg.n_boot))
    if progress:
        progress("E: IRRBB done")
    f = ecl_coverage(loan_book(cfg))
    rows += _bars("ecl", "coverage", SA.tornado(f, ECL_BASE, ECL_RANGES), ECL_BASE)
    d_ecl = {k: SA.Uniform(*v) for k, v in ECL_RANGES.items()}
    rows += _sobol_rows("ecl", "coverage", SA.sobol_indices(SA.rowwise(f), d_ecl, cfg.n_ecl, _seed(cfg, 6, 1),
                                                            cfg.n_boot))
    if progress:
        progress("E: ECL done")
    return rows


def run_study(cfg: ValuationConfig, progress: Optional[Callable[[str], None]] = None) -> List[dict]:
    if cfg.m < 2:
        raise ValueError("m must be >= 2")
    rows = part_a(cfg)
    rows += part_b(cfg, progress)
    rows += part_c(cfg)
    rows += part_d(cfg)
    if progress:
        progress("A-D done")
    return rows + part_e(cfg, progress)


def meta_extra(cfg: ValuationConfig) -> dict:
    inp = company_inputs(RV.implied_growth(cfg.reference_price, company_inputs(), "constant"))
    res = D.dcf(inp)
    return {"company": COMPANY, "synthetic": True,
            "reference": {"price": cfg.reference_price, "implied_growth": inp.growth,
                          "enterprise_value": res.enterprise_value, "terminal_share": res.terminal_share,
                          "pv_explicit": res.pv_explicit, "pv_terminal": res.pv_terminal},
            "dcf_ranges": {k: list(v) for k, v in dcf_ranges(dcf_base(cfg)).items()},
            "dcf_dists": {k: [d.lo, d.hi] for k, d in dcf_dists(dcf_base(cfg)).items()},
            "irrbb_ranges": {k: list(v) for k, v in IRRBB_RANGES.items()}, "irrbb_base": IRRBB_BASE,
            "ecl_ranges": {k: list(v) for k, v in ECL_RANGES.items()}, "ecl_base": ECL_BASE,
            "universe": asdict(BR.GrowthUniverse()),
            "sources": {
                "dcf": "enterprise DCF with Gordon terminal value (textbook: Koller, Goedhart and Wessels, Valuation; "
                       "Damodaran, Investment Valuation); end-of-year cash flows",
                "reverse DCF": "price as given, solve for the implied growth (Mauboussin and Rappaport, Expectations "
                               "Investing); Brent root finding on an explicit bracket",
                "sobol": "Sobol' (2001); estimators of Saltelli et al. (2010, Comput. Phys. Commun. 181, 259-270) "
                         "Table 2: first order (b), total order (f) = Jansen (1999); confirmed in the v0.9 review "
                         "(consistent with SALib) and checked against the analytic Ishigami indices; the first-order "
                         "estimator uses centred outputs (numerical-stability variant, same estimand, O(1/N) bias)",
                "base rates": "synthetic universe of the v0.4 survivorship study (same draws); FCF growth compared with "
                              "revenue growth assumes a constant FCF margin (modelling assumption)",
            }}


def _pct(x: Optional[float], d: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:.{d}f}"


def summary_markdown(rows: List[dict], cfg: ValuationConfig) -> str:
    ex = meta_extra(cfg)
    ref = ex["reference"]
    L = ["# Reverse DCF and sensitivity analysis (synthetic example)", "",
         f"{COMPANY['name']}: FCF {COMPANY['fcf0']:g}, discount rate {_pct(COMPANY['discount_rate'])} %, terminal growth "
         f"{_pct(COMPANY['terminal_growth'])} %, {COMPANY['years']}-year explicit period, net debt {COMPANY['net_debt']:g}, "
         f"other claims {COMPANY['other_claims']:g}, {COMPANY['shares']:g} shares. M = {cfg.m} cohorts, Sobol N = "
         f"{cfg.n_sobol} (DCF, Ishigami), {cfg.n_irrbb} (IRRBB), {cfg.n_ecl} (ECL), {cfg.n_boot} bootstrap resamples, seed "
         f"{cfg.seed}. risklab {risklab.__version__}, numpy {np.__version__}. No market or company data.", "",
         f"Reference price {cfg.reference_price:g}: implied constant growth {_pct(ref['implied_growth'], 2)} % a year for "
         f"{COMPANY['years']} years; terminal value = {_pct(ref['terminal_share'])} % of the enterprise value.", "",
         "## A. Implied growth vs price", "",
         "| price | implied constant growth | implied fade start (fade CAGR) | implied discount rate at 5 % growth |",
         "|---|---|---|---|"]
    for r in rows:
        if r["table"] == "implied":
            L.append(f"| {r['price']:g} | {_pct(r['implied_growth'], 2)} % | {_pct(r['implied_fade_start'], 2)} % "
                     f"({_pct(r['fade_cagr'], 2)} %) | {_pct(r['implied_discount_rate'], 2)} % |")
    L += ["", "## B. How often is the implied 10-year CAGR achieved? (synthetic universe of the v0.4 study)", "",
          "| kappa | survival rate | survivors only | full cohort (exit = miss) | counterfactual |", "|---|---|---|---|---|"]
    for r in rows:
        if r["table"] == "base_rate_ref":
            L.append(f"| {r['kappa']:g} | {_pct(r['survival_rate'])} % | {_pct(r['survivors_only'])} ± "
                     f"{_pct(r['survivors_only_se'], 2)} % | {_pct(r['full_cohort'])} ± {_pct(r['full_cohort_se'], 2)} % | "
                     f"{_pct(r['counterfactual'])} ± {_pct(r['counterfactual_se'], 2)} % |")
    for model, output, lab in (("dcf", "value_per_share", "value per share"), ("dcf", "implied_growth",
                               "implied growth at the reference price"), ("irrbb", "worst_eve_tier1",
                               "worst Delta EVE / Tier 1"), ("ecl", "coverage", "ECL coverage")):
        L += ["", f"## C/E. Tornado: {lab} ({model})", "", "| input | low - high | output at low | output at high | swing |",
              "|---|---|---|---|---|"]
        for r in rows:
            if r["table"] == "tornado" and r["model"] == model and r["output"] == output:
                L.append(f"| {r['name']} | {r['low']:.4g} - {r['high']:.4g} | {r['y_low']:.4g} | {r['y_high']:.4g} | "
                         f"{r['swing']:.4g} |")
    for model, output in (("dcf", "value_per_share"), ("ishigami", "y"), ("irrbb", "worst_eve_tier1"), ("ecl", "coverage")):
        rr = [r for r in rows if r["table"] == "sobol" and r["model"] == model]
        L += ["", f"## D/E. Sobol indices: {model} / {output} (N = {rr[0]['n']}, {rr[0]['evaluations']} runs)", "",
              "| input | first order ± se | total order ± se | analytic first / total |", "|---|---|---|---|"]
        for r in rr:
            an = (f"{r['analytic_first']:.4f} / {r['analytic_total']:.4f}" if "analytic_first" in r else "-")
            L.append(f"| {r['name']} | {r['first']:.4f} ± {r['first_se']:.4f} | {r['total']:.4f} ± {r['total_se']:.4f} | {an} |")
    return "\n".join(L) + "\n"


def write_outputs(rows: List[dict], cfg: ValuationConfig, out_dir: Path) -> List[Path]:
    meta = {"study": STUDY, "code_sha256": code_fingerprint(__file__),
            "config": {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(cfg).items()}, **meta_extra(cfg)}
    out_dir = Path(out_dir)
    return [table_to_csv(rows, COLUMNS, out_dir / "results.csv", cfg.seed, meta),
            table_to_json(rows, out_dir / "results.json", cfg.seed, meta),
            text_to_file(summary_markdown(rows, cfg), out_dir / "summary.md")]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=sorted(MODES), default="default",
                    help="smoke (M=5, N=256), default (M=200, N=4096) or full (M=2000, N=65536)")
    ap.add_argument("--seed", type=int, default=ValuationConfig.seed)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--save-results", action="store_true",
                    help="with --mode full: also save results.json / summary.md as studies/results/"
                         f"{STUDY}_full_*.json / .md (the inputs of the report)")
    args = ap.parse_args(argv)
    if args.save_results and (args.mode != "full" or args.seed != ap.get_default("seed")):
        ap.error("--save-results needs --mode full with the default seed")
    m, n, ni, ne = MODES[args.mode]
    cfg = ValuationConfig(m=m, n_sobol=n, n_irrbb=ni, n_ecl=ne, seed=args.seed)
    if args.mode == "smoke":
        cfg.n_boot = 50
    out_dir = Path(args.out_dir) if args.out_dir else default_out_dir() / STUDY
    log = None if args.quiet else (lambda msg: print(msg, flush=True))
    start = time.perf_counter()
    rows = run_study(cfg, log)
    paths = write_outputs(rows, cfg, out_dir)
    if args.save_results:
        paths += save_full_results(out_dir, STUDY)
    if log:
        log(f"{len(rows)} rows in {time.perf_counter() - start:.1f} s -> " + ", ".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
