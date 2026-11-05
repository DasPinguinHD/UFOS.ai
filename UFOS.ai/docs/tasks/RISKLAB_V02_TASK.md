# RISKLAB v0.2 — VaR / ES estimators + backtests (sprint 1 of the career plan)

Change 2026-11-05 (user): learning and interview preparation happen outside this chat; **coding no longer waits for
the derivation sheets**. The stubs from v0.1 are now implemented. Everything else in v0.1 (conventions, engine,
export, data policy, repo rules) stays in force.

## 1. Models (`src/risklab/models/`), sign convention L = loss
All return `Forecast(var={alpha: v}, es={alpha: e})` for each alpha in (0, 1).
- `HistoricalSimulation(method="empirical")`:
  - VaR = the empirical alpha-quantile of the window losses. Use one documented convention: the order statistic
    ceil(n·alpha), with no interpolation. State it in the docstring and in the README.
  - ES = the mean of the losses ≥ VaR (tail mean). For small tails, document the tie handling.
  - Refuse windows with fewer than ceil(1/(1−alpha)) observations (ValueError with the reason). Never extrapolate.
- `NormalModel()`: μ and σ from the window (sample mean, sample std ddof=1).
  VaR = μ + σ Φ⁻¹(α); ES = μ + σ φ(Φ⁻¹(α))/(1−α).
- `StudentTModel(nu=None)`: location-scale t.
  - nu=None: fit (μ, s, ν) by maximum likelihood (scipy.stats.t.fit with ν bounded in (2.05, 200); document the
    bounds).
  - nu given: fit μ and s with ν fixed.
  - VaR = μ + s t_ν⁻¹(α).
  - ES = μ + s · [f_ν(t_ν⁻¹(α)) / (1−α)] · (ν + (t_ν⁻¹(α))²)/(ν−1).
  - Note that s is the scale, not the standard deviation.
- A shared helper `risklab.analytic` with closed-form `var_normal`, `es_normal`, `var_t`, `es_t` (standardised and
  location-scale). The models use it.

## 2. "Truth" for synthetic data (`src/risklab/data/truth.py`)
`true_var_es(series: SyntheticSeries, alphas)` returns per-day arrays of the true conditional VaR and ES from the
stored true parameters:
- iid normal and iid scaled t: closed form;
- GARCH: closed form, conditional on sigma_true (normal innovations, or t innovations scaled to unit variance);
- regime switching: closed form per day from sigmas[regime].

This makes it possible to measure estimator error and test power.

## 3. Backtests (`src/risklab/backtest/`), returning `TestResult`
- `kupiec_pof(exceedances, alpha)`:
  - LR_uc = −2 ln[(1−p)^{T−x} p^x / ((1−p̂)^{T−x} p̂^x)] with p = 1−α; χ²₁ p-value.
  - Edge cases x=0 and x=T handled exactly: the 0·ln 0 terms are 0. No eps fudge.
- `christoffersen_ind(exceedances)`:
  - transition counts n00, n01, n10, n11; LR_ind ~ χ²₁.
  - Degenerate cases (no exceedance, or no transition out of state 1): return the statistic with a `notes` entry
    explaining why; never invent a value.
- `christoffersen_cc(exceedances, alpha)` = LR_uc + LR_ind ~ χ²₂. Christoffersen's convention drops the first
  observation for LR_uc in the cc test. Document which convention is used.
- `traffic_light(exceedances, alpha=0.99, window=250)`:
  - Basel 1996 zones from the binomial CDF under H0: green while P(X ≤ x) < 95 %, red when ≥ 99.99 %, yellow in
    between.
  - The multiplier add-on table for 250 days at 99 % (0.00 up to 4; then 0.40 / 0.50 / 0.65 / 0.75 / 0.85 for
    5–9; 1.00 at 10 and above). Source cited in the docstring.
  - Zones for other windows are computed from the CDF; the add-on is only given for the 250/99 % case, else null with
    a note.
- `decision` = "reject" / "not rejected" at `reject_at` (default 0.05). Wording: never "accepted".

## 4. Study script (`studies/var_backtest_power.py`) — the first showcase result
Runs only on synthetic data, deterministic seeds, writes into `out/` (git-ignored):
- **Size:** for each data generator, run the CORRECT model class and report the rejection rates of Kupiec / ind / cc
  (target ≈ 5 %) over M Monte-Carlo replications.
- **Power:** wrong models on the same data: normal on t data; static models on GARCH and regime data; historical
  simulation on regime data. Report the rejection rates.
- Use T = 250 and T = 1000. M is configurable; the default is small enough for < 2 min on a laptop, and the full run
  is documented.
- Output: CSV + JSON (via `report/export.py`) and a Markdown summary table. No plotting dependency yet.

## 5. Tests
- Analytic values:
  - VaR_0.99 of N(0,1) = 2.326348 and ES_0.99 = 2.665214;
  - ES_0.975 of N(0,1) = 2.337803;
  - t values against scipy numerical integration (not against the same formula).
- Kupiec: T=250, α=0.99, x=7 → LR=5.497, p=0.0190; x=0 and x=T exact.
- Traffic light: zone boundaries 4/5 and 9/10 for 250 / 99 %.
- Christoffersen: a hand-computed small example; degenerate cases.
- HS: the order-statistic convention on a tiny known sample; the too-short window raises.
- `true_var_es` against Monte-Carlo quantiles of large simulated samples (tolerance stated).
- Engine end-to-end: NormalModel on iid normal data → the exceedance rate is near 1−α (tolerance from the binomial
  standard deviation).
- The study script runs in "smoke" mode (tiny M) inside the tests.

## 6. Rules
- Dependencies: numpy, scipy (now actually used). Still no plotting library.
- README: update the status (v0.2), the conventions (HS quantile, Christoffersen cc), and how to run the study.
  The open points stay (lock, CI copy, SHA pinning).
- Scope label `risklab2` (`UFOS.ai/RiskLab/**` by exact paths + this file + the docs/tasks README line).
- Repo rules from CLAUDE.md: synthetic data only, `check_repo_data.py`, no git, `*.stale` instead of deleting,
  English. The VM has no scipy: install it into a RiskLab venv on the VM if possible, otherwise run the full tests in
  the cloud container and say so.
- Then an independent review; then fixes.
