"""IRRBB risk measures: PV, duration, convexity, DV01, key-rate durations, the six standardised shock
scenarios, Delta EVE, Delta NII and the supervisory outlier tests.

**Sensitivities** (cash flows ``c_i`` at ``t_i``, zero curve ``z``, continuous compounding):

* ``PV = sum c_i exp(-z(t_i) t_i)``;
* at a flat yield ``y``: Macaulay duration ``D = sum t_i c_i e^{-y t_i} / PV``; with continuous
  compounding the modified duration equals ``D`` (``dPV/dy = -D PV``); with m-times-a-year
  compounding of the yield ``D_mod = D_mac / (1 + y / m)``; convexity ``C = sum t_i^2 c_i e^{-y t_i} / PV``
  (:func:`bond_measures`);
* on a curve: effective duration / convexity from parallel zero-rate bumps of +-``h`` (central
  differences), DV01 = ``PV(z - 1 bp) - PV(z)`` reported as the central ``(PV(z-1bp) - PV(z+1bp)) / 2``,
  analytic DV01 = ``1e-4 sum t_i c_i D(t_i)``;
* key-rate durations at :data:`KEY_RATES`: the zero curve is bumped by a "tent" around each key
  tenor (1 at the key, linear to 0 at the neighbouring keys, flat beyond the first / last key).
  The tents sum to 1 at every tenor, so the key-rate durations sum to the effective duration up
  to second-order terms.

**Standardised shocks** (BCBS 2016, *Interest rate risk in the banking book*, d368, annex 2;
Basel Framework SRP31.90-31.92), with ``S_short(t) = R_short e^{-t/x}``,
``S_long(t) = R_long (1 - e^{-t/x})``, ``x = 4``:

1. parallel up / down: ``+- R_parallel``;
2. short up / down: ``+- S_short(t)``;
3. steepener: ``-0.65 |S_short(t)| + 0.9 |S_long(t)|``;
4. flattener: ``+0.8 |S_short(t)| - 0.6 |S_long(t)|``.

Sources: the shapes with ``x = 4`` and the steepener weights (-0.65 / +0.9) are checked against the
worked example of BCBS d578 (SRP31.92); the flattener weights (+0.8 / -0.6) are confirmed by
EBA/RTS/2022/10 Art. 3(d) (now Commission Delegated Regulation (EU) 2024/856); see
:data:`SHOCK_SOURCES`.

Shock sizes (bp) are parameters (:data:`SHOCK_PRESETS`): ``EUR_BCBS2016`` = 200 / 250 / 100
(parallel / short / long; BCBS 2016 d368 table, the default as the task asks) and
``EUR_BCBS2024`` = 225 / 350 / 200 (the recalibration, BCBS d578 of 16 July 2024, to be applied
from 1 January 2026; read from the BIS publication's table).

**Post-shock floor** (:class:`Floor`): ``floor(t) = min(0, f0 + slope t)``; EBA defaults
``f0 = -150 bp`` and ``slope = +3 bp`` a year, reaching 0 at 50 years (EBA/RTS/2022/10 Art. 4(k),
now Delegated Regulation (EU) 2024/856). If the observed (pre-shock) rate is already below the
floor, the lower observed rate applies (``floor_t = min(floor(t), z(t))``; same article). The EU
floor is applied to both shock presets (BCBS 2016 and 2024 sizes) - a choice of this study; the
BCBS standard itself leaves the floor to national supervisors.

**Delta EVE** = EVE(shocked) - EVE(base), ``EVE = PV(asset cash flows) + PV(liability cash flows)``
(liabilities carry negative cash flows), run-off, equity excluded: a negative value is a loss.
**Supervisory outlier tests** (:func:`outlier_tests`): the largest EVE decline over the six
scenarios against ``eve_threshold x Tier 1`` (15 %, CRD Art. 98(5)), and the largest NII
decline over the parallel up / down scenarios against ``nii_threshold x Tier 1`` (5 %, Delegated
Regulation (EU) 2024/856 Art. 6, following the EBA Opinion of 26 April 2023 that replaced 2.5 %;
parallel scenarios only per Art. 6(1)). Both thresholds are parameters.

**Delta NII** over 12 months, constant balance sheet, instantaneous shock (:func:`nii`): monthly
grid; on each month the repriced share of each balance earns the new rate, the rest the
contractual rate (see :mod:`risklab.rates.instruments`).

Every result carries an :class:`Assumptions` record (name -> statement and source) so the report
can list them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .curve import ZeroCurve, as_zero_curve, shifted
from .instruments import BalanceSheet, CashFlows, instrument_assumptions

BP = 1e-4
X_DECAY = 4.0
KEY_RATES = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0)
SCENARIOS = ("parallel_up", "parallel_down", "steepener", "flattener", "short_up", "short_down")
NII_SCENARIOS = ("parallel_up", "parallel_down")

SHOCK_PRESETS: Dict[str, Dict[str, float]] = {
    "EUR_BCBS2016": {"parallel": 200.0, "short": 250.0, "long": 100.0},
    "EUR_BCBS2024": {"parallel": 225.0, "short": 350.0, "long": 200.0},
}
SHOCK_SOURCES = {
    "EUR_BCBS2016": "BCBS (2016) d368, annex 2, table of specified shocks: EUR 200 / 250 / 100 bp",
    "EUR_BCBS2024": "BCBS (2024) d578 recalibration (16 July 2024, effective 1 January 2026): EUR 225 / 350 / 200 bp",
    "shapes": "short e^{-t/4}, long 1 - e^{-t/4}: BCBS d368 annex 2 / SRP31.91; checked against d578",
    "steepener": "-0.65 |short| + 0.9 |long|: checked against the worked example in d578 (SRP31.92)",
    "flattener": "+0.8 |short| - 0.6 |long|: EBA/RTS/2022/10 Art. 3(d) (Delegated Regulation (EU) 2024/856)",
    "floor": "-150 bp at immediate maturity, +3 bp a year, 0 from 50 years: EBA/RTS/2022/10 Art. 4(k) "
             "(Delegated Regulation (EU) 2024/856); applied to both shock presets (a choice of this study)",
    "floor_observed": "observed rate below the floor -> the lower observed rate applies: EBA/RTS/2022/10 Art. 4(k) / 1(2)",
    "eve_sot": "EVE decline > 15 % of Tier 1: CRD Art. 98(5)",
    "nii_sot": "NII decline > 5 % of Tier 1 over the two parallel scenarios: Delegated Regulation (EU) 2024/856 "
               "Art. 6(1) (EBA Opinion of 26 April 2023, replacing 2.5 %)",
}


@dataclass
class Assumptions:
    """Name -> statement (with its source or 'ASSUMPTION'); attached to every result."""
    items: Dict[str, str] = field(default_factory=dict)

    def add(self, name: str, statement: str) -> "Assumptions":
        self.items[name] = statement
        return self

    def merged(self, other: "Assumptions") -> "Assumptions":
        return Assumptions({**self.items, **other.items})


# ---- sensitivities -------------------------------------------------------------------------

def pv(cf: CashFlows, curve) -> float:
    return float(np.sum(cf.amounts * curve.discount(cf.times)))


def bond_measures(cf: CashFlows, y: float, m: Optional[int] = None) -> Dict[str, float]:
    """PV, Macaulay / modified duration and convexity at a flat yield ``y``; ``m=None``: continuous
    compounding, else the yield is compounded m times a year (``D_mod = D_mac / (1 + y/m)``)."""
    t, c = cf.times, cf.amounts
    disc = np.exp(-y * t) if m is None else (1.0 + y / m) ** (-m * t)
    p = float(np.sum(c * disc))
    mac = float(np.sum(t * c * disc)) / p
    if m is None:
        mod, conv = mac, float(np.sum(t * t * c * disc)) / p
    else:
        mod = mac / (1.0 + y / m)
        conv = float(np.sum(c * t * (t + 1.0 / m) * disc)) / p / (1.0 + y / m) ** 2
    return {"pv": p, "macaulay": mac, "modified": mod, "convexity": conv}


def _bumped(curve, h: float) -> ZeroCurve:
    return shifted(curve, lambda t: np.full_like(np.asarray(t, dtype=np.float64), h))


def effective_measures(cf: CashFlows, curve, h: float = 1.0 * BP) -> Dict[str, float]:
    p0 = pv(cf, curve)
    up, dn = pv(cf, _bumped(curve, h)), pv(cf, _bumped(curve, -h))
    return {"pv": p0, "duration": (dn - up) / (2.0 * h * p0), "convexity": (up + dn - 2.0 * p0) / (h * h * p0),
            "dv01": (dn - up) / 2.0 * (BP / h),
            "dv01_analytic": float(BP * np.sum(cf.times * cf.amounts * curve.discount(cf.times)))}


def tent(k: int, keys: Sequence[float] = KEY_RATES):
    keys = np.asarray(keys, dtype=np.float64)

    def w(t):
        t = np.asarray(t, dtype=np.float64)
        out = np.zeros_like(t)
        if k > 0:
            lo, mid = keys[k - 1], keys[k]
            m = (t > lo) & (t <= mid)
            out = np.where(m, (t - lo) / (mid - lo), out)
        else:
            out = np.where(t <= keys[0], 1.0, out)
        if k < keys.size - 1:
            mid, hi = keys[k], keys[k + 1]
            m = (t > mid) & (t < hi)
            out = np.where(m, (hi - t) / (hi - mid), out)
        else:
            out = np.where(t >= keys[-1], 1.0, out)
        return out
    return w


def key_rate_durations(cf: CashFlows, curve, h: float = 1.0 * BP, keys: Sequence[float] = KEY_RATES,
                       per_unit_pv: bool = True) -> np.ndarray:
    """Key-rate durations (``per_unit_pv``) or key-rate DV01s in currency per bp (``per_unit_pv=False``)."""
    p0 = pv(cf, curve)
    out = []
    for k in range(len(keys)):
        w = tent(k, keys)
        up = pv(cf, shifted(curve, lambda t, w=w: h * w(t)))
        dn = pv(cf, shifted(curve, lambda t, w=w: -h * w(t)))
        out.append((dn - up) / (2.0 * h * p0) if per_unit_pv else (dn - up) / 2.0 * (BP / h))
    return np.asarray(out)


# ---- shocks --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Floor:
    f0_bp: float = -150.0
    slope_bp: float = 3.0

    def __call__(self, t):
        t = np.asarray(t, dtype=np.float64)
        return np.minimum(0.0, (self.f0_bp + self.slope_bp * t) * BP)


def shock(name: str, t, sizes: Dict[str, float], x: float = X_DECAY) -> np.ndarray:
    """The shock (decimal) of a scenario at tenors t; ``sizes`` in bp."""
    t = np.asarray(t, dtype=np.float64)
    s_short = sizes["short"] * BP * np.exp(-t / x)
    s_long = sizes["long"] * BP * (1.0 - np.exp(-t / x))
    if name == "parallel_up":
        return np.full_like(t, sizes["parallel"] * BP)
    if name == "parallel_down":
        return np.full_like(t, -sizes["parallel"] * BP)
    if name == "short_up":
        return s_short
    if name == "short_down":
        return -s_short
    if name == "steepener":
        return -0.65 * np.abs(s_short) + 0.9 * np.abs(s_long)
    if name == "flattener":
        return 0.8 * np.abs(s_short) - 0.6 * np.abs(s_long)
    raise ValueError(f"unknown scenario {name!r} (one of {', '.join(SCENARIOS)})")


def shocked_curve(curve, name: str, sizes: Dict[str, float], floor: Optional[Floor] = Floor(),
                  x: float = X_DECAY) -> ZeroCurve:
    """Base zero curve plus the scenario shock, floored at ``min(floor(t), z(t))`` (``floor=None``: no floor)."""
    base = as_zero_curve(curve)
    if floor is None:
        return shifted(base, lambda t: shock(name, t, sizes, x), label=name)
    return shifted(base, lambda t: shock(name, t, sizes, x), label=name,
                   floor=lambda t: np.minimum(floor(t), base.zero(t)))


def shock_assumptions(preset: str, floor: Optional[Floor]) -> Assumptions:
    a = Assumptions()
    a.add("shock sizes", f"{preset}: {SHOCK_PRESETS[preset]} bp - {SHOCK_SOURCES[preset]}")
    for k in ("shapes", "steepener", "flattener"):
        a.add(f"shock {k}", SHOCK_SOURCES[k])
    if floor is None:
        a.add("post-shock floor", "none (switched off)")
    else:
        a.add("post-shock floor", f"min(0, {floor.f0_bp:g} bp + {floor.slope_bp:g} bp x t) - {SHOCK_SOURCES['floor']}")
        a.add("floor below observed", SHOCK_SOURCES["floor_observed"])
    return a


# ---- EVE / NII -----------------------------------------------------------------------------

def eve(bs: BalanceSheet, curve) -> float:
    return sum(pv(x.cashflows(), curve) for x in bs.instruments)


def nii(bs: BalanceSheet, base, shocked, months: int = 12) -> float:
    """12-month net interest income (currency units) with the repricing rules of the instruments."""
    tm = np.arange(months) / 12.0
    total = 0.0
    for x in bs.instruments:
        bal = float(getattr(x, "notional", None) or getattr(x, "balance"))
        F = x.repriced_fraction(tm)
        r_new = x.new_rate(base, shocked)
        total += x.side * bal * float(np.sum((1.0 - F) * x.rate + F * r_new)) / 12.0
    return total


@dataclass
class ScenarioResult:
    scenario: str
    delta_eve: float
    delta_nii: Optional[float]
    eve_base: float
    nii_base: float


@dataclass
class IRRBBResult:
    preset: str
    rows: List[ScenarioResult]
    tier1: float
    assumptions: Assumptions

    def worst_eve(self) -> Tuple[str, float]:
        r = min(self.rows, key=lambda s: s.delta_eve)
        return r.scenario, r.delta_eve


def run_scenarios(bs: BalanceSheet, curve, preset: str = "EUR_BCBS2016", floor: Optional[Floor] = Floor()) -> IRRBBResult:
    """Delta EVE (all six scenarios) and Delta NII (all six; the NII outlier test uses parallel up / down)."""
    sizes = SHOCK_PRESETS[preset]
    base = as_zero_curve(curve)
    e0, n0 = eve(bs, base), nii(bs, base, base)
    rows = []
    for s in SCENARIOS:
        sc = shocked_curve(base, s, sizes, floor)
        rows.append(ScenarioResult(s, eve(bs, sc) - e0, nii(bs, base, sc) - n0, e0, n0))
    a = shock_assumptions(preset, floor)
    for k, v in instrument_assumptions().items():
        a.add(k, v)
    a.add("NII horizon", "12 months, monthly grid, constant balance sheet, instantaneous shock")
    a.add("EVE", "run-off cash flows, equity excluded, single currency (no 50 % weight on gains across currencies)")
    return IRRBBResult(preset, rows, bs.tier1, a)


def outlier_tests(res: IRRBBResult, eve_threshold: float = 0.15, nii_threshold: float = 0.05) -> Dict[str, object]:
    """EVE SOT over the six scenarios, NII SOT over parallel up / down (module docstring)."""
    w_s, w_e = res.worst_eve()
    nii_rows = [r for r in res.rows if r.scenario in NII_SCENARIOS]
    n_r = min(nii_rows, key=lambda r: r.delta_nii)
    eve_ratio = -w_e / res.tier1
    nii_ratio = -n_r.delta_nii / res.tier1
    return {"eve_worst_scenario": w_s, "eve_ratio": eve_ratio, "eve_threshold": eve_threshold,
            "eve_outlier": bool(eve_ratio > eve_threshold),
            "nii_worst_scenario": n_r.scenario, "nii_ratio": nii_ratio, "nii_threshold": nii_threshold,
            "nii_outlier": bool(nii_ratio > nii_threshold),
            "sources": {"eve": SHOCK_SOURCES["eve_sot"], "nii": SHOCK_SOURCES["nii_sot"]}}
