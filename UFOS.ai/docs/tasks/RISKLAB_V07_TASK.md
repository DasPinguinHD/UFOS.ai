# RISKLAB v0.7 — interest rate risk in the banking book (IRRBB)

Showcase module (user plan 2026-11-06): well documented, traceable, synthetic data only. All Risk Lab rules apply
(English, deterministic, no silent fallbacks / FitError-style explicit errors, check_repo_data, no git, no private
links, report PDFs only via `--publish-to` outside the repo).

## 1. Yield curve (`risklab.rates.curve`)
- Nelson–Siegel and Svensson (NSS) parametrisation: zero rates z(t), discount factors, forward rates; continuous
  compounding by default, conversions documented.
- Fitting to synthetic par yields or zero rates (least squares with bounds; decay parameters bounded > 0; several
  deterministic starts; explicit error on failure).
- A synthetic curve generator: dynamic Nelson–Siegel factors (level / slope / curvature as AR(1)), seeds, so
  that studies have known truth.
- Optional: a `CurveSource` interface for user-provided curve parameters (e.g. Bundesbank / ECB published NSS
  parameters entered by the user); no data download or committed data.

## 2. Instruments and cash flows (`risklab.rates.instruments`)
- Fixed-rate bullet bond, amortising loan (annuity), floating-rate note (reset at the next date), term deposit,
  and a simple non-maturity deposit (NMD) with a documented replicating-portfolio assumption (core / non-core
  split and an average maturity cap, as parameters; EBA caps cited and documented, not hard-wired as law).
- A synthetic bank balance sheet generator (assets / liabilities / equity) with a documented composition.

## 3. Risk measures (`risklab.rates.irrbb`)
- PV, Macaulay / modified duration, convexity, DV01 / PV01, key-rate durations (bucketed bumps with a documented
  interpolation).
- **EBA / BCBS standardised shock scenarios** (the six: parallel up / down, steepener, flattener, short up / short
  down) with the currency-specific shock sizes as parameters (defaults for EUR from the BCBS 2016 standards,
  source cited in the docstring) and the post-shock lower bound (floor) as documented in the guidelines.
- ΔEVE per scenario (run-off balance sheet, equity excluded) and the supervisory outlier test (ΔEVE vs Tier 1
  capital thresholds as parameters, 15 % / the SOT on NII as a documented parameter).
- ΔNII over 12 months for a constant balance sheet with repricing (simple, documented assumptions).
- Explicit "assumptions" object that every result carries (so the report can list them).

## 4. Study (`studies/irrbb_scenarios.py`)
- On the synthetic balance sheet: ΔEVE / ΔNII per scenario; key-rate profile; sensitivity of ΔEVE to the NMD
  assumptions (core share, maturity) — this is the classic model-risk point of IRRBB.
- Check: duration approximation vs full revaluation for parallel shocks of increasing size (where convexity
  matters).
- smoke / default / full modes, measured runtimes, `--save-results` with fingerprints.

## 5. Report
- Figures (curve and shocked curves; ΔEVE bars per scenario; key-rate profile; NMD sensitivity heat map;
  duration vs full revaluation) and tables (scenario table, outlier test, assumptions register, findings register
  with PROPOSED severities).
- `report/irrbb.tex` (6–10 pages, `\usertext` slots) + `build_report.py --doc irrbb`; build the PDF into out/report
  and report the path.

## 6. Tests (independent)
- Bond PV / duration / convexity against closed forms; DV01 by finite difference vs analytic; key-rate durations
  sum ≈ the effective duration; NSS fit recovers synthetic parameters; shock definitions against the published
  formulas (BCBS IRRBB 2016 annex: parallel / short / long shock shapes); the floor; ΔEVE sign conventions;
  the NMD replicating portfolio.

## 7. Delivery
- Version 0.7.0; README section; scope label `risklab11`; tests (VM + container); check → read ok → close;
  independent review.
