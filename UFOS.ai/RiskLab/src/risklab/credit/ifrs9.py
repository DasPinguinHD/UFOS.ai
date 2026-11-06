"""IFRS 9 expected credit loss (ECL): staging, 12-month and lifetime ECL, macro scenarios.

Rules (IFRS 9 Financial Instruments, section 5.5; B5.5.13 confirmed in the v0.8 review, the other
paragraph numbers as recalled - ASSUMPTION for the references, the rules themselves are the
standard's well-known structure):

* **Stage 1**: no significant increase in credit risk (SICR) since initial recognition ->
  12-month ECL (5.5.5);
* **Stage 2**: SICR -> lifetime ECL (5.5.3). The SICR trigger here (parameters, see
  :class:`StagingRules`): the 12-month PD has risen to at least ``sicr_multiple`` x its value at
  origination (a relative threshold; the 12-month PD as a proxy for the lifetime PD is a
  simplification the standard permits when it gives a reasonable approximation, B5.5.13) AND by
  at least ``sicr_abs`` in absolute terms; or the loan is
  ``dpd_backstop`` (30) days past due (the rebuttable presumption of 5.5.11);
* **Stage 3**: credit-impaired, here ``default_dpd`` (90) days past due (the rebuttable default
  presumption of B5.5.37) -> lifetime ECL with PD = 1: ``LGD x EAD``.

ECL of a loan for a horizon of H years (annual grid, default at the end of a year, discounted at
the effective interest rate EIR)::

    ECL = sum_{k=1..H} PD_marg(k) x LGD x EAD(k) x (1 + EIR)^-k,
    PD_marg(k) = S(k-1) h(k),   S(k) = prod_{j<=k} (1 - h(j)),   h(k) = min(1, PD_12m x mult(k))

H = 1 for 12-month ECL and the remaining maturity for lifetime ECL. ``mult(k)`` is the macro
overlay of a scenario (e.g. 1.8, 1.5, 1.2, 1, ... in a downside); the base scenario has 1.
EAD(k) is the outstanding balance at the start of year k of an annual annuity at the EIR
(amortising exposure) or the full balance (bullet). The ECL is the probability-weighted average
over the scenarios (5.5.17, probability-weighted, time value of money).

**Staging basis (v0.8 review):** the SICR test compares the current 12-month PD ``pd_now`` with
``pd_orig`` on the SAME basis - both are base-scenario PDs without the scenario overlay (no
origination-date scenario set exists to overlay ``pd_orig`` with). The scenarios therefore
change the measurement (the ECL amount), not the stage; up to v0.8.0 the overlaid,
probability-weighted current PD was compared with the plain origination PD, which mixed two bases
and let the downside weight move loans into stage 2.

**Simplifications** (documented, deliberate): the exposure of year k is the balance at the START
of the year while the default is assumed at the END of the year (slightly conservative for an
amortising loan); the stage-3 ECL is ``LGD x EAD``, undiscounted and without scenarios; the
lifetime PD has a constant hazard per year (the 12-month PD, times the scenario multipliers);
LGD is constant per loan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

import numpy as np

IFRS9_SOURCES = {
    "stages": "IFRS 9 5.5.3 (lifetime if SICR) / 5.5.5 (12-month otherwise); paragraph numbers as recalled - ASSUMPTION",
    "staging basis": "current 12-month PD and the PD at origination compared on the same basis: both base-scenario "
                     "PDs without the scenario overlay (modelling choice; scenarios affect the ECL, not the stage)",
    "backstop": "30 days past due: rebuttable presumption of SICR, IFRS 9 5.5.11 (as recalled)",
    "default": "90 days past due: rebuttable presumption of default, IFRS 9 B5.5.37 (as recalled)",
    "sicr proxy": "12-month PD change as a proxy for the lifetime PD change (IFRS 9 B5.5.13; confirmed in the v0.8 review)",
    "weighting": "probability-weighted over scenarios, discounted at the EIR (IFRS 9 5.5.17 as recalled)",
    "PD term structure": "constant hazard = 12-month PD x scenario multiplier per year (simple model, synthetic)",
    "EAD": "annual annuity balance at the start of each year (amortising) or full balance (bullet), with the default "
           "at the end of the year (simplification, slightly conservative)",
    "stage 3": "ECL = LGD x EAD, undiscounted and without scenarios (simplification)",
    "LGD": "constant per loan (synthetic parameter)",
}


@dataclass(frozen=True)
class Loan:
    ead: float               # current outstanding balance
    eir: float               # effective interest rate (annual)
    maturity: int            # remaining maturity in whole years (>= 1)
    lgd: float
    pd_now: float            # current 12-month PD (base scenario)
    pd_orig: float           # 12-month PD at origination
    dpd: int = 0             # days past due
    amortising: bool = True

    def __post_init__(self):
        if self.ead <= 0 or self.maturity < 1:
            raise ValueError("ead must be > 0 and maturity >= 1 year")
        for v in (self.lgd, self.pd_now, self.pd_orig):
            if not 0.0 <= v <= 1.0:
                raise ValueError("lgd and PDs must be in [0, 1]")
        if self.pd_orig <= 0:
            raise ValueError("pd_orig must be > 0 (relative SICR test)")


@dataclass(frozen=True)
class Scenario:
    name: str
    weight: float
    multipliers: Tuple[float, ...] = ()      # PD multiplier for years 1, 2, ...; afterwards 1

    def mult(self, k: int) -> float:
        return self.multipliers[k - 1] if k <= len(self.multipliers) else 1.0


DEFAULT_SCENARIOS = (Scenario("upside", 0.2, (0.8, 0.9)), Scenario("base", 0.5, ()),
                     Scenario("downside", 0.3, (1.8, 1.5, 1.2)))


@dataclass(frozen=True)
class StagingRules:
    sicr_multiple: float = 3.0
    sicr_abs: float = 0.001
    dpd_backstop: int = 30
    default_dpd: int = 90


def ead_profile(loan: Loan) -> np.ndarray:
    """Outstanding balance at the start of years 1..maturity."""
    T = loan.maturity
    if not loan.amortising:
        return np.full(T, loan.ead)
    r = loan.eir
    A = loan.ead / T if r == 0 else loan.ead * r / (1 - (1 + r) ** -T)
    bal, out = loan.ead, []
    for _ in range(T):
        out.append(bal)
        bal = bal * (1 + r) - A
    return np.asarray(out)


def marginal_pd(pd12: float, years: int, scenario: Scenario) -> np.ndarray:
    h = np.array([min(1.0, pd12 * scenario.mult(k)) for k in range(1, years + 1)])
    s_prev = np.concatenate([[1.0], np.cumprod(1.0 - h)[:-1]])
    return s_prev * h


def ecl_horizon(loan: Loan, years: int, scenario: Scenario) -> float:
    years = min(years, loan.maturity)
    k = np.arange(1, years + 1)
    return float(np.sum(marginal_pd(loan.pd_now, years, scenario) * loan.lgd * ead_profile(loan)[:years]
                        * (1 + loan.eir) ** -k))


def weighted_pd12(loan: Loan, scenarios: Sequence[Scenario]) -> float:
    return float(sum(s.weight * min(1.0, loan.pd_now * s.mult(1)) for s in scenarios))


def stage(loan: Loan, scenarios: Sequence[Scenario] = DEFAULT_SCENARIOS, rules: StagingRules = StagingRules()) -> int:
    """Stage 1 / 2 / 3. ``scenarios`` do not enter the SICR test (same-basis comparison of ``pd_now`` and
    ``pd_orig``, module docstring); the argument is kept for the signature of :func:`ecl`."""
    if loan.dpd >= rules.default_dpd:
        return 3
    if loan.dpd >= rules.dpd_backstop:
        return 2
    p = loan.pd_now          # same basis as pd_orig: base scenario, no overlay (module docstring)
    if p >= rules.sicr_multiple * loan.pd_orig and p - loan.pd_orig >= rules.sicr_abs:
        return 2
    return 1


def _check_weights(scenarios: Sequence[Scenario]):
    w = sum(s.weight for s in scenarios)
    if abs(w - 1.0) > 1e-9 or any(s.weight < 0 for s in scenarios):
        raise ValueError(f"scenario weights must be >= 0 and sum to 1 (sum {w})")


def ecl(loan: Loan, scenarios: Sequence[Scenario] = DEFAULT_SCENARIOS, rules: StagingRules = StagingRules()
        ) -> Dict[str, float]:
    """{stage, ecl, ecl_12m, ecl_lifetime} (probability-weighted)."""
    _check_weights(scenarios)
    st = stage(loan, scenarios, rules)
    e12 = sum(s.weight * ecl_horizon(loan, 1, s) for s in scenarios)
    elt = sum(s.weight * ecl_horizon(loan, loan.maturity, s) for s in scenarios)
    value = loan.lgd * loan.ead if st == 3 else (e12 if st == 1 else elt)
    return {"stage": st, "ecl": float(value), "ecl_12m": float(e12), "ecl_lifetime": float(elt)}


def portfolio_ecl(loans: Sequence[Loan], scenarios: Sequence[Scenario] = DEFAULT_SCENARIOS,
                  rules: StagingRules = StagingRules()) -> Dict[str, float]:
    tot = {"ecl": 0.0, "ead": 0.0, "stage1": 0, "stage2": 0, "stage3": 0, "ecl_stage2": 0.0}
    for ln in loans:
        r = ecl(ln, scenarios, rules)
        tot["ecl"] += r["ecl"]
        tot["ead"] += ln.ead
        tot[f"stage{r['stage']}"] += 1
        if r["stage"] == 2:
            tot["ecl_stage2"] += r["ecl"]
    tot["coverage"] = tot["ecl"] / tot["ead"]
    return tot
