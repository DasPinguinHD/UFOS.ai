# RISKLAB v0.5 — overfitting study of the UFOS validation gates (showcase piece 3)

Question: **how reliable are the UFOS strategy-validation gates?** How often does pure noise (after a search over
many configurations) get through, and how often is a real but weak signal recognised? Synthetic data only. Builds on
v0.4 (d5c3a68). All earlier rules apply: deterministic, no silent fallbacks, English, `check_repo_data.py`, no git,
**no private links in any file**.

## 1. Scope: the statistical gates
The gate definitions are in `UFOS.ai/HedgeFund/backend/strategy_platform/SPEC.md` §9.2–§9.4 and §9.6; the code is in
`strategy_platform/validation/{stats.py,gates.py,folds.py,grid.py}`. Read both.
- **In scope:** G1 (time-series form: Sharpe > 0 in ≥ 4 of 5 walk-forward folds; plus the cross-sectional rank-IC
  form with a Newey-West t, if a cross-sectional synthetic universe is feasible in the budget), G2 (alpha NW t vs a
  synthetic benchmark), G4 (deflated Sharpe, N = trials + 1, Sharpe variance floored at 1/years), G5 (PBO by CSCV,
  S = 10, the "< 4 configurations → FAIL" rule), G6 only its grid-neighbour part (±20 % keeps ≥ 50 % of the base
  Sharpe), G7 (trade count), and H (holdout: net Sharpe > 0 AND ≥ 50 % of training Sharpe).
  Presets: Lenient / Standard / Strict.
- **Out of scope** (needs the full engine / market data): G3 decay, costs and stress episodes in G6, G8 capacity.
  State this clearly in the study and the report.

## 2. Implementation: independent re-implementation + parity
- `risklab.validation`: deflated Sharpe, PBO / CSCV, NW t, the walk-forward fold split, the grid-neighbour rule and
  the gate pass rules with the three presets, **implemented in RiskLab from the SPEC and the papers** (Bailey & López
  de Prado 2014 for DSR; Bailey, Borwein, López de Prado & Zhu 2017 for PBO / CSCV; Newey & West 1987, with the
  lag rule stated). RiskLab still imports nothing from the HedgeFund backend.
- **Parity test on the HedgeFund side:** `HedgeFund/backend/tests/test_risklab_gate_parity.py` adds `RiskLab/src`
  to sys.path (it skips with a reason if it is missing) and compares RiskLab's DSR, PBO, NW t and gate decisions
  against `strategy_platform.validation` on fixed synthetic inputs (tolerances stated). Every difference is either
  fixed in RiskLab or documented as an intentional deviation, after asking the lead. Do NOT change HedgeFund code;
  report any HedgeFund bug you find instead.

## 3. Synthetic strategy-search simulator (`risklab.validation.sim`)
- A "strategy family" = a parametrised daily trading rule on synthetic returns: e.g. a moving-average / momentum
  rule with a parameter grid of up to 64 configurations (min / max / step like the UFOS DSL), so that G5 / G6 have a
  real grid.
- Data: iid t returns, GARCH-t returns (vol clustering), plus a planted signal of controllable strength (an AR(1)
  component, or a predictive factor with a target Sharpe of 0, 0.25, 0.5, 0.75, 1.0 per year).
- The search: K trials (K ∈ {1, 8, 64, 256}); the best in-sample configuration is selected, as a user or the loop
  would; then all gates run on the selected candidate with the trial count K (G4) and the full grid (G5 / G6).
- Holdout: the last 24 months are never seen by the search; H is evaluated once.
- Sample lengths: 5, 10, 20 years of daily data before the holdout.
- Positions → daily strategy returns with a simple cost model (bps per turnover, a parameter); the trade count for
  G7.

## 4. The study (`studies/gate_overfitting.py`)
- **False-positive rate (FPR)** under the null (signal 0): the share of searches whose selected candidate passes
  (a) each gate alone, (b) G1–G8 in scope, (c) G1–G8 + H. Per K, sample length, data type and preset.
- **Power:** the same under planted signals; the power curve against the true Sharpe.
- **Holdout value:** how much H adds on top of G1–G8 (conditional pass rates).
- **Calibration check:** reproduce SP §9.6 ("≥ 95 % of noise seeds fail ≥ 1 gate"; "best-of-64 noise fails G4 or
  G5") with stated se.
- Monte-Carlo se on every rate; smoke / default (≈ 3 min, measured) / full modes; `--save-results` with the code
  fingerprint as in v0.4.

## 5. Report
- Reuse the v0.4 pipeline: new figures (FPR vs K per preset; power curves; per-gate rejection heat table; holdout
  increment), new tables, and a findings register for this study (severities marked PROPOSED).
- A second LaTeX document `report/overfitting_study.tex` (8–12 pages) with the same `\usertext` slots:
  question, design, gates in / out of scope, results, limitations (synthetic, gates partly out of scope, one rule
  family), findings. `build_report.py --doc overfitting` builds it; publish the draft PDF next to the validation
  report.
- README: an "Overfitting study" section with the key numbers.

## 6. Tests (independent)
- DSR against the paper's worked example (or a hand computation); PBO = 0.5-ish on pure noise with many
  configurations and ≈ 0 for a dominant true configuration; NW t against statsmodels-free hand code with a known
  lag; the CSCV split counts (C(S, S/2) combinations).
- Gate rules: boundary cases for each preset.
- The simulator: planted Sharpe recovered on long samples; selection over K raises the in-sample Sharpe as expected
  (expected maximum of K normals).
- The study in smoke mode; the parity test (on the HedgeFund side, skipping when unavailable).

## 7. Delivery
- Version 0.5.0. Scope label `risklab8`: the RiskLab paths + the one new HedgeFund test file + the docs/tasks README
  line.
- Tests on the VM ($HOME/rlvenv) + the container; the HedgeFund parity test with the HedgeFund backend venv (or the
  container offload as in CLAUDE.md).
- Then an independent review.
