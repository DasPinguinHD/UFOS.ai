"""Banking-book instruments, their cash flows and repricing, and a synthetic bank balance sheet.

Conventions: times in years from today; notionals in currency units (the study uses EUR
millions); ``side = +1`` for an asset (cash flows received), ``-1`` for a liability (paid).
Every instrument gives

* ``cashflows()`` -> :class:`CashFlows` (signed by ``side``): the **run-off** cash flows for
  the economic value of equity (EVE), principal and contractual interest;
* ``repriced_fraction(t)``: the share of the balance whose rate has been reset by time t
  under a **constant balance sheet** (maturing or amortised balances are replaced by the same
  product at the new rate) - used for the net interest income (NII);
* ``rate`` (the current contractual rate) and ``new_rate(base, shocked)``: the rate at which
  the repriced balance earns or pays; for market-linked products the contractual rate plus the
  shock to the zero rate at the product's repricing tenor (so the base scenario reproduces the
  contractual rate and the margin is kept).

Simplifications (all listed in :func:`instrument_assumptions`): annual / periodic coupons on
exact year fractions; no day counts, no credit spreads in discounting (the risk-free curve
discounts every cash flow, as the BCBS standardised framework does), commercial margins after
the next repricing of a floating-rate note are excluded (it reprices at par).

Non-maturity deposits (NMD, :class:`NonMaturityDeposit`): a **replicating portfolio**:

* the non-core share reprices and runs off overnight (cash flow at 1/365 years);
* the core share runs off in equal monthly tranches over ``2 x avg_maturity`` years, so its
  average maturity is ``avg_maturity + 1/24`` years, i.e. approximately ``avg_maturity``
  (uniform run-off, tranches at the month ends);
* the deposit rate follows market rates with a pass-through ``beta`` on the shock to the
  3-month zero rate, floored at ``rate_floor`` (default 0).

Caps on the core share and the average maturity per NMD category are parameters
(:data:`NMD_CAPS`). The defaults are the caps of the BCBS IRRBB standard (BCBS 2016, d368,
table 2; Basel Framework SRP31) **as recalled**: retail transactional 90 % / 5 years, retail
non-transactional 70 % / 4.5 years, wholesale 50 % / 4 years. They could not be verified
against the primary text in this session and are therefore marked as an ASSUMPTION; the EU
rules (EBA guidelines / RTS) may differ. They are a check, not law: ``enforce_caps=False``
allows any value (and the result's assumptions say so).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

from risklab.conventions import SeedLike, make_rng

OVERNIGHT = 1.0 / 365.0
SHORT_TENOR = 0.25          # tenor of the market rate that drives NMD pass-through

NMD_CAPS: Dict[str, Dict[str, float]] = {
    "retail_transactional": {"core_share": 0.90, "avg_maturity": 5.0},
    "retail_non_transactional": {"core_share": 0.70, "avg_maturity": 4.5},
    "wholesale": {"core_share": 0.50, "avg_maturity": 4.0},
}
NMD_CAPS_SOURCE = ("BCBS (2016) d368 table 2 / Basel Framework SRP31, as recalled - ASSUMPTION "
                   "(not verified against the primary text)")


@dataclass(frozen=True)
class CashFlows:
    times: np.ndarray
    amounts: np.ndarray

    def __post_init__(self):
        if self.times.shape != self.amounts.shape or np.any(self.times <= 0):
            raise ValueError("cash-flow times must be > 0 and match the amounts")


def _cf(times, amounts) -> CashFlows:
    return CashFlows(np.asarray(times, dtype=np.float64), np.asarray(amounts, dtype=np.float64))


def _check(notional, maturity, freq=1):
    if notional <= 0:
        raise ValueError("notional must be > 0")
    if maturity <= 0:
        raise ValueError("maturity must be > 0")
    if freq not in (1, 2, 4, 12):
        raise ValueError("freq must be 1, 2, 4 or 12")


def _schedule(maturity: float, freq: int) -> np.ndarray:
    n = int(round(maturity * freq))
    if abs(n / freq - maturity) > 1e-9:
        raise ValueError("maturity must be a whole number of periods")
    return np.arange(1, n + 1) / freq


@dataclass(frozen=True)
class FixedBond:
    """Fixed-rate bullet: coupons ``notional * coupon / freq`` and the notional at maturity."""
    notional: float
    coupon: float
    maturity: float
    freq: int = 1
    side: int = 1
    name: str = "fixed bond"

    def __post_init__(self):
        _check(self.notional, self.maturity, self.freq)

    @property
    def rate(self) -> float:
        return self.coupon

    def cashflows(self) -> CashFlows:
        t = _schedule(self.maturity, self.freq)
        a = np.full(t.size, self.notional * self.coupon / self.freq)
        a[-1] += self.notional
        return _cf(t, self.side * a)

    def repriced_fraction(self, t):
        return (np.asarray(t, dtype=np.float64) >= self.maturity).astype(np.float64)

    def new_rate(self, base, shocked) -> float:
        L = self.maturity
        return self.coupon + float(shocked.zero(L) - base.zero(L))


@dataclass(frozen=True)
class AmortisingLoan:
    """Annuity loan: equal payments ``A = N (r/f) / (1 - (1 + r/f)^-n)`` per period."""
    notional: float
    rate_: float
    maturity: float
    freq: int = 12
    side: int = 1
    name: str = "annuity loan"

    def __post_init__(self):
        _check(self.notional, self.maturity, self.freq)

    @property
    def rate(self) -> float:
        return self.rate_

    def payment(self) -> float:
        n = int(round(self.maturity * self.freq))
        i = self.rate_ / self.freq
        return self.notional / n if i == 0 else self.notional * i / (1.0 - (1.0 + i) ** -n)

    def balances(self) -> np.ndarray:
        """Outstanding principal after each payment (last = 0)."""
        n = int(round(self.maturity * self.freq))
        i, A = self.rate_ / self.freq, self.payment()
        b, out = self.notional, []
        for _ in range(n):
            b = b * (1.0 + i) - A
            out.append(max(b, 0.0))
        return np.asarray(out)

    def cashflows(self) -> CashFlows:
        t = _schedule(self.maturity, self.freq)
        return _cf(t, np.full(t.size, self.side * self.payment()))

    def repriced_fraction(self, t):
        """Amortised share of the principal by time t (re-lent at the new rate)."""
        t = np.asarray(t, dtype=np.float64)
        sched = _schedule(self.maturity, self.freq)
        bal = np.concatenate([[self.notional], self.balances()])
        k = np.searchsorted(sched, t, side="right")          # payments made by t
        return 1.0 - bal[np.minimum(k, sched.size)] / self.notional

    def new_rate(self, base, shocked) -> float:
        L = self.maturity
        return self.rate_ + float(shocked.zero(L) - base.zero(L))


@dataclass(frozen=True)
class FloatingRateNote:
    """Floating-rate note / loan: reprices to par at the next reset; the current coupon is fixed until then.

    EVE cash flows: ``N (1 + current_coupon * accrual)`` at ``next_reset`` (later margins excluded)."""
    notional: float
    current_coupon: float
    next_reset: float
    reset_period: float = 0.25
    maturity: float = 5.0
    side: int = 1
    name: str = "floating-rate note"

    def __post_init__(self):
        _check(self.notional, self.maturity)
        if not 0 < self.next_reset <= min(self.reset_period, self.maturity):
            raise ValueError("next_reset must be in (0, min(reset_period, maturity)]")

    @property
    def rate(self) -> float:
        return self.current_coupon

    def cashflows(self) -> CashFlows:
        return _cf([self.next_reset], [self.side * self.notional * (1.0 + self.current_coupon * self.reset_period)])

    def repriced_fraction(self, t):
        return (np.asarray(t, dtype=np.float64) >= self.next_reset).astype(np.float64)

    def new_rate(self, base, shocked) -> float:
        L = self.reset_period
        return self.current_coupon + float(shocked.zero(L) - base.zero(L))


@dataclass(frozen=True)
class TermDeposit:
    """Term deposit (a liability by default): principal and interest ``N (1 + r)^T`` at maturity."""
    notional: float
    rate_: float
    maturity: float
    side: int = -1
    name: str = "term deposit"

    def __post_init__(self):
        _check(self.notional, self.maturity)

    @property
    def rate(self) -> float:
        return self.rate_

    def cashflows(self) -> CashFlows:
        return _cf([self.maturity], [self.side * self.notional * (1.0 + self.rate_) ** self.maturity])

    def repriced_fraction(self, t):
        return (np.asarray(t, dtype=np.float64) >= self.maturity).astype(np.float64)

    def new_rate(self, base, shocked) -> float:
        L = self.maturity
        return self.rate_ + float(shocked.zero(L) - base.zero(L))


@dataclass(frozen=True)
class NonMaturityDeposit:
    """Non-maturity deposit with a replicating portfolio (module docstring)."""
    balance: float
    core_share: float
    avg_maturity: float
    rate_: float = 0.0
    beta: float = 0.3
    rate_floor: float = 0.0
    category: str = "retail_transactional"
    enforce_caps: bool = True
    side: int = -1
    name: str = "non-maturity deposit"

    def __post_init__(self):
        if self.balance <= 0:
            raise ValueError("balance must be > 0")
        if not 0.0 <= self.core_share <= 1.0:
            raise ValueError("core_share must be in [0, 1]")
        if not self.avg_maturity > 0:
            raise ValueError("avg_maturity must be > 0")
        if not 0.0 <= self.beta <= 1.0:
            raise ValueError("beta must be in [0, 1]")
        if self.enforce_caps:
            if self.category not in NMD_CAPS:
                raise ValueError(f"unknown NMD category {self.category!r} (one of {', '.join(NMD_CAPS)})")
            cap = NMD_CAPS[self.category]
            if self.core_share > cap["core_share"] + 1e-12 or self.avg_maturity > cap["avg_maturity"] + 1e-12:
                raise ValueError(f"{self.category}: core share {self.core_share:g} / average maturity "
                                 f"{self.avg_maturity:g} exceed the caps {cap} ({NMD_CAPS_SOURCE}); pass "
                                 "enforce_caps=False to model it anyway")

    @property
    def rate(self) -> float:
        return self.rate_

    def tranche_times(self) -> np.ndarray:
        n = max(1, int(round(2.0 * self.avg_maturity * 12)))
        return np.arange(1, n + 1) / 12.0 * (2.0 * self.avg_maturity * 12 / n)

    def cashflows(self) -> CashFlows:
        tt = self.tranche_times()
        core = self.balance * self.core_share / tt.size * (1.0 + self.rate_ * tt)
        t = np.concatenate([[OVERNIGHT], tt])
        a = np.concatenate([[self.balance * (1.0 - self.core_share) * (1.0 + self.rate_ * OVERNIGHT)], core])
        return _cf(t, self.side * a)

    def repriced_fraction(self, t):
        """The deposit rate follows market rates (with ``beta``) for the whole balance from day 0."""
        return np.ones_like(np.asarray(t, dtype=np.float64))

    def new_rate(self, base, shocked) -> float:
        d = float(shocked.zero(SHORT_TENOR) - base.zero(SHORT_TENOR))
        return max(self.rate_floor, self.rate_ + self.beta * d)


def instrument_assumptions() -> Dict[str, str]:
    return {
        "discounting": "every cash flow on the risk-free zero curve (no credit spreads); continuous compounding",
        "run-off": "EVE on contractual run-off cash flows (principal + interest); equity excluded",
        "constant balance sheet": "for NII, maturing / amortised / reset balances are replaced by the same product "
                                  "at the contractual rate plus the shock to the zero rate at its repricing tenor",
        "FRN": "reprices to par at the next reset; margins after it are excluded from EVE",
        "NMD replicating portfolio": "non-core overnight; core in equal monthly tranches over 2 x average maturity",
        "NMD rate": f"pass-through beta on the {SHORT_TENOR:g}-year zero-rate shock, floored at the rate floor",
        "NMD caps": NMD_CAPS_SOURCE,
    }


@dataclass
class BalanceSheet:
    assets: List
    liabilities: List
    tier1: float
    description: Dict[str, str] = field(default_factory=dict)

    @property
    def instruments(self) -> List:
        return list(self.assets) + list(self.liabilities)

    def totals(self) -> Dict[str, float]:
        a = sum(_book_value(x) for x in self.assets)
        l = sum(_book_value(x) for x in self.liabilities)
        return {"assets": a, "liabilities": l, "equity": a - l, "tier1": self.tier1}


def _book_value(x) -> float:
    return float(getattr(x, "notional", None) or getattr(x, "balance"))


REFERENCE_COMPOSITION = (
    # (kind, side, notional (EUR m), parameters) - synthetic; shares chosen to resemble a small retail bank
    ("annuity", 1, 300.0, {"rate_": 0.032, "maturity": 10.0, "name": "mortgages (annuity, 10 y fixed)"}),
    ("annuity", 1, 150.0, {"rate_": 0.045, "maturity": 5.0, "name": "corporate loans (annuity, 5 y)"}),
    ("bond", 1, 120.0, {"coupon": 0.025, "maturity": 5.0, "name": "government bonds (5 y)"}),
    ("bond", 1, 50.0, {"coupon": 0.028, "maturity": 10.0, "name": "government bonds (10 y)"}),
    ("frn", 1, 300.0, {"current_coupon": 0.035, "next_reset": 0.10, "reset_period": 0.25, "maturity": 5.0,
                       "name": "floating-rate loans (3 m reset)"}),
    ("frn", 1, 80.0, {"current_coupon": 0.020, "next_reset": OVERNIGHT, "reset_period": OVERNIGHT, "maturity": 1.0,
                      "name": "central-bank reserves (overnight)"}),
    ("nmd", -1, 450.0, {"core_share": 0.70, "avg_maturity": 3.0, "rate_": 0.001, "beta": 0.3,
                        "category": "retail_transactional", "name": "retail current accounts (NMD)"}),
    ("nmd", -1, 150.0, {"core_share": 0.50, "avg_maturity": 2.0, "rate_": 0.005, "beta": 0.5,
                        "category": "retail_non_transactional", "name": "retail savings (NMD)"}),
    ("term", -1, 120.0, {"rate_": 0.025, "maturity": 1.0, "name": "term deposits (1 y)"}),
    ("term", -1, 80.0, {"rate_": 0.028, "maturity": 3.0, "name": "term deposits (3 y)"}),
    ("bond", -1, 100.0, {"coupon": 0.029, "maturity": 7.0, "name": "covered bond issued (7 y)"}),
)
TIER1_SHARE = 0.09        # Tier 1 capital as a share of total assets (synthetic; generous for a retail bank)


def _make(kind, side, notional, p):
    p = dict(p)
    if kind == "annuity":
        return AmortisingLoan(notional, side=side, **p)
    if kind == "bond":
        return FixedBond(notional, side=side, **p)
    if kind == "frn":
        return FloatingRateNote(notional, side=side, **p)
    if kind == "term":
        return TermDeposit(notional, side=side, **p)
    if kind == "nmd":
        return NonMaturityDeposit(notional, side=side, **p)
    raise ValueError(kind)


def synthetic_balance_sheet(seed: Optional[SeedLike] = None, nmd_core_share: Optional[float] = None,
                            nmd_avg_maturity: Optional[float] = None, enforce_caps: bool = True) -> BalanceSheet:
    """The synthetic bank (EUR millions; :data:`REFERENCE_COMPOSITION`, Tier 1 = 9 % of assets).

    ``seed=None`` gives the reference bank. With a seed, every notional is scaled by an independent
    uniform factor in [0.7, 1.3] and every fixed rate moves by a uniform +-50 bp (floored at 0), so a
    study can look at a population of similar banks. ``nmd_core_share`` / ``nmd_avg_maturity``
    override the current-account NMD assumptions (the NMD sensitivity)."""
    rng = None if seed is None else make_rng(seed)
    assets, liabs = [], []
    for kind, side, notional, p in REFERENCE_COMPOSITION:
        p = dict(p)
        if rng is not None:
            notional = notional * float(rng.uniform(0.7, 1.3))
            for k in ("rate_", "coupon", "current_coupon"):
                if k in p and kind != "nmd":
                    p[k] = max(0.0, p[k] + float(rng.uniform(-0.005, 0.005)))
        if kind == "nmd":
            p["enforce_caps"] = enforce_caps
            if p["category"] == "retail_transactional":
                if nmd_core_share is not None:
                    p["core_share"] = nmd_core_share
                if nmd_avg_maturity is not None:
                    p["avg_maturity"] = nmd_avg_maturity
        (assets if side > 0 else liabs).append(_make(kind, side, notional, p))
    total_assets = sum(_book_value(x) for x in assets)
    desc = {"composition": "REFERENCE_COMPOSITION (risklab.rates.instruments)",
            "tier1_rule": f"{TIER1_SHARE:.0%} of total assets (synthetic)",
            "variation": "reference bank" if seed is None else "notionals x U(0.7, 1.3), fixed rates +- U(50 bp)"}
    return BalanceSheet(assets, liabs, TIER1_SHARE * total_assets, desc)
