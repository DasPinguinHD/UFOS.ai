# RISKLAB v0.8 — credit risk (PD model, IFRS 9 ECL, Basel IRB capital)

Showcase module (user plan 2026-11-06). Synthetic data only; all Risk Lab rules apply.

## 1. Synthetic loan portfolio (`risklab.credit.data`)
- Borrowers with features (e.g. leverage, interest coverage, sector, size, a macro factor), a known true PD model
  (logit with documented coefficients), defaults drawn from it; multi-period panel with a systematic factor
  (one-factor Gaussian copula, documented ρ) so defaults are correlated; seeds.

## 2. PD model (`risklab.credit.pd`)
- Logistic regression by MLE (numpy/scipy, no sklearn), with standard errors; train / test split by time.
- Discrimination: ROC AUC, Gini / accuracy ratio, CAP curve; calibration: Hosmer–Lemeshow, binomial test per
  rating grade, Brier score; a rating master scale (grades with PD bands); Jeffreys test (ECB/TRIM style) per grade.
- Explicit errors on separation / non-convergence.

## 3. IFRS 9 ECL (`risklab.credit.ifrs9`)
- 12-month vs lifetime ECL; staging rules (stage 1/2/3) with a documented significant-increase-in-credit-risk
  trigger (relative PD increase threshold and 30-days-past-due backstop as parameters); lifetime PD term
  structure from the model plus a macro scenario overlay (three weighted scenarios); LGD and EAD (amortising
  exposure) as documented simple models; discounting at the effective interest rate.

## 4. Basel IRB capital (`risklab.credit.irb`)
- The IRB risk-weight function for corporate exposures (Vasicek ASRF: correlation formula, maturity adjustment,
  K, RWA = 12.5·K·EAD), with the formula and source (BCBS 2006 / CRR Art. 153) cited; the PD floor as a
  parameter. Compare the regulatory capital with a Monte-Carlo loss distribution of the same portfolio (the
  one-factor model): check K vs the simulated 99.9 % quantile minus EL for a large granular portfolio, and show
  the granularity / concentration gap for a small one.

## 5. Study (`studies/credit_risk.py`)
- PD model quality vs sample size; calibration under a macro shift (model drift); ECL sensitivity to scenario
  weights and the SICR threshold (cliff effect at stage 2); IRB capital vs simulated losses (granular vs
  concentrated). smoke / default / full, runtimes, `--save-results`.

## 6. Report + tests
- Figures / tables / findings register; `report/credit.tex` (6–10 pages, `\usertext`) + `--doc credit`; PDF into
  out/report.
- Tests (independent): logistic MLE vs a hand-coded Newton solution on a tiny set; AUC vs the Mann–Whitney
  statistic; the IRB formula against a published worked value or a hand computation; ECL on a single loan by hand;
  staging edge cases; the Vasicek quantile vs Monte Carlo for a large portfolio.
- Version 0.8.0; README; label `risklab12`; tests (VM + container); check → ok → close; independent review.
