# RISKLAB v0.3 — conditional models, multi-day horizon, block bootstrap, ES backtest

Builds on v0.2 + the risklab3 review fixes. All v0.1/v0.2 rules stay in force: L = loss, synthetic data only in the
repo, deterministic seeds, no silent fallbacks (`FitError` instead), English, `check_repo_data.py`, no git.
Learning happens outside this chat; this is a coding task (user decision 2026-11-05).

## 1. Conditional 1-day models (`src/risklab/models/`)
All implement `RiskModel` and return `Forecast` for each alpha. Where the backtests need it, they also expose the
predictive distribution (see §4).
- **`EWMAModel(lam=0.94, dist="normal"|"t", nu=None)`** — RiskMetrics.
  - σ²_{t+1} = λσ²_t + (1−λ)L_t². Zero mean.
  - Initialisation: the sample variance of the first `init` observations of the window (documented, default 30).
  - VaR / ES from `risklab.analytic` with σ_{t+1}; for dist="t", the unit-variance t.
- **`GarchTModel(refit_every=20)`** — GARCH(1,1) with unit-variance t innovations, fitted by MLE.
  - Parameters (μ, ω, α, β, ν).
  - Constraints: ω > 0, α ≥ 0, β ≥ 0, α + β < 1, ν in (2.05, 200). Document the reparametrisation and bounds.
  - The variance recursion starts at the sample variance.
  - Between refits, keep the parameters fixed but update σ²_{t+1} daily with the new data. This needs a stateful
    model or a filter step: extend the engine contract carefully, so that look-ahead stays impossible and is tested.
  - Optimiser failure raises `FitError` (as for the t MLE). Use several deterministic starts.
- **`FilteredHS(garch="normal"|"t", refit_every=20)`** — Barone-Adesi et al. (1999).
  - Fit GARCH(1,1) (normal QMLE by default) and form the standardised residuals z_t = (L_t − μ)/σ_t.
  - VaR = μ + σ_{t+1} · (empirical α-quantile of z, with the v0.2 HS order-statistic convention).
  - ES = μ + σ_{t+1} · the tail mean of z.

## 2. Multi-day horizon h (Basel 10-day)
- `risklab.horizon`:
  - **Square-root-of-time:** VaR_h ≈ √h · VaR_1. Clearly labelled as an approximation.
  - **Monte Carlo h-day VaR / ES** from a fitted GARCH-t: simulate N paths of h days from σ_{t+1}, sum the losses,
    take the empirical quantile and tail mean. N and the seed are parameters; the Monte-Carlo standard error of the
    VaR is reported (e.g. by batch means or a binomial order-statistic interval).
  - **Stationary block bootstrap** (Politis & Romano 1994): resample the window with geometric block lengths of mean
    `block_len`, wrap around circularly, build h-day sums, take the quantile and tail mean.
    - `block_len` is fixed, or "auto" via Politis & White (2004) with the Patton, Politis & White (2009) correction.
    - Implement the method from the papers; no third-party package.
    - With block_len = 1 it must equal the iid bootstrap (tested).
- `true_var_es_h(series, alphas, h, n_sims, seed)`: the true h-day VaR / ES for the synthetic generators.
  - iid normal / t: closed form for normal; Monte Carlo for t, with a stated error.
  - GARCH: Monte Carlo from the true parameters, conditional on the true state at t.

## 3. Engine
- Support stateful models (filter updates between refits) without breaking v0.1 / v0.2 models.
  - fit() still only ever sees data < t.
  - The daily update only sees L_{t−1}.
  - Spy tests prove this.
- The h-day backtest uses non-overlapping h-day blocks (overlapping blocks break the independence assumptions of the
  tests). Document this and the resulting small T.

## 4. ES backtest — Acerbi & Székely (2014), test 2 ("Z2")
- Forecasts must provide VaR_t, ES_t and a way to simulate the predictive loss distribution:
  - an optional `Forecast.dist`, an object with `ppf(u)` (vectorised) for the day's predictive distribution;
  - implemented for normal, t, EWMA and GARCH-t;
  - for HS / FHS, the empirical distribution of the window (scaled for FHS).
- Z2 = Σ_t L_t·I_t / (T·(1−α)·ES_t) − 1, with I_t = 1{L_t > VaR_t}. Losses are positive, so Z2 > 0 means the realised
  tail losses exceed the predicted ES (risk underestimated). One-sided test against underestimation. Document the
  sign convention against the paper's profit-and-loss sign.
- p-value by Monte Carlo under H0: simulate L_t from each day's predictive distribution, M_sim draws (parameter),
  fixed seed.
- `TestResult` like the other tests; `details` carries Z2 and M_sim.

## 5. Study extension (`studies/`)
- **var_backtest_power:** add rows for the correctly specified conditional models on GARCH data (GarchTModel,
  FilteredHS, EWMA-t) and on regime data. Key question: do the conditional models pass where the static ones fail?
  Add the Acerbi–Székely Z2 rejection rate as a column. Keep the size / se / exact-size columns from risklab3.
- **New `studies/horizon_10d.py`:** on GARCH-t and iid data, compare the 10-day VaR (99 %) from √10 scaling, GARCH
  Monte Carlo and the block bootstrap against the true 10-day VaR (bias and RMSE); report Monte-Carlo errors.
  Smoke / default / full modes as before.
- The default runtime of each study stays under ~3 min on a laptop; full mode is documented. Save the full-run
  summaries in `studies/results/` (synthetic, allowed).

## 6. Tests (independent checks, no tautologies)
- EWMA: the recursion by hand on a tiny series.
- GARCH-t MLE: recovers the true parameters on a long synthetic series (n ≈ 20k, tolerances stated); constraint
  violations are impossible; FitError on a forced optimiser failure.
- FHS: on iid data it is close to HS; the residual standardisation is checked.
- Block bootstrap:
  - the mean block length is ≈ block_len;
  - reproducible with a seed;
  - block_len = 1 equals the iid bootstrap;
  - Politis–White "auto" on an AR(1) series gives a longer block than on white noise.
- h-day: √h is exact for the iid normal truth; the Monte Carlo agrees with the closed form for the normal case within
  the stated error.
- Acerbi–Székely: the Z2 sign on constructed examples; the size under H0 (correct model) is ≈ nominal over many
  replications (Monte-Carlo test, tolerance stated); power against ES underestimation.
- Engine: stateful look-ahead spy tests; the v0.2 tests stay green.

## 7. Delivery
- Version 0.3.0 (pyproject + `__init__`).
- README: status, new models, conventions, the horizon study, open points.
- Scope label `risklab4`: exact RiskLab paths + this file + the docs/tasks README line.
- Tests on the VM ($HOME/rlvenv) + the container.
- Then an independent review.
