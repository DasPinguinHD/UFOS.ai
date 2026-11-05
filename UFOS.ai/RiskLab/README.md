# Risk Lab

A bank-style market-risk lab: estimate one-day Value-at-Risk (VaR) and Expected
Shortfall (ES), and backtest the forecasts the way a bank's risk management does.
It is a showcase for risk management and is part of UFOS.ai. It is independent of
the HedgeFund backend: there are no imports between them, and it has its own
tests and dependencies.

**Status: v0.4.** Implemented:

- one-day models:
  - static: historical simulation (HS), normal and Student-t;
  - conditional: EWMA (RiskMetrics), GARCH(1,1)-t and filtered HS;
- the h-day (e.g. Basel 10-day) VaR / ES: square-root-of-time, GARCH Monte
  Carlo and the stationary block bootstrap;
- the true one-day and h-day VaR / ES of the synthetic generators;
- the VaR backtests: Kupiec POF, the exact binomial test, Christoffersen
  independence and conditional coverage, and the Basel traffic light;
- the ES backtest: Acerbi-Szekely Z2;
- four studies: size / power of the backtests, the 10-day VaR methods, the
  estimation window, and survivorship bias;
- the **validation report pipeline**: seven figures, seven LaTeX tables
  (including a findings register) and a report skeleton with marked text slots.
  A draft PDF is built from synthetic data only (see "The validation report" below).

The derivation sheets are worked through separately, outside this code (user
decision 2026-11-05).

## Conventions

- `L` is the loss: positive = loss. Returns convert by `L = -r`.
- `alpha` is the confidence level, strictly inside (0, 1), e.g. 0.99. VaR and
  ES are thresholds on the loss: `VaR_alpha` is the alpha-quantile of the loss,
  and `ES_alpha = E[L | L >= VaR_alpha]`. An exceedance is `loss > var`.
- The horizon is one trading day. Arrays are 1-D float64 numpy arrays.
- Every random function takes `seed` (an int) or a `numpy.random.Generator`.
  The same seed gives byte-identical output within the same numpy version.
  Exports record `numpy_version`.

### Model conventions

- **Historical simulation:**
  - Sort the n window losses ascending. VaR is the order statistic `L_(k)` with
    `k = ceil(n * alpha)`. There is no interpolation; `n * alpha` is rounded to
    9 decimals first, to remove floating-point noise.
  - ES is the mean of all window losses `>= VaR`. Ties at the VaR are all in the
    tail.
  - A window shorter than `ceil(1 / (1 - alpha))` raises ValueError (100 days
    at 99 %, 40 days at 97.5 %). HS never extrapolates beyond the largest
    observed loss.
  - Finite-sample bias: for iid losses the next loss exceeds `L_(k)` with
    probability `(n - k + 1) / (n + 1)`, not `1 - alpha`. At n = 250 and 99 %
    (k = 248) that is 3 / 251 = 1.20 % instead of 1 %
    (`risklab.models.historical.exceedance_probability`).
- **Normal:** the sample mean and the sample standard deviation (ddof = 1) of the window.
- **Student-t:**
  - Location-scale t fitted by maximum likelihood. The objective is the same
    as `scipy.stats.t.fit`, solved with L-BFGS-B and an analytic gradient.
  - The degrees of freedom are bounded to nu in [2.05, 200]: the variance must
    exist, and above 200 the t is numerically a normal.
  - `StudentTModel(nu=5)` fixes nu and fits only location and scale.
  - If the optimiser does not report convergence, the fit raises `FitError`;
    there is no silent fallback.
  - The scale is not the standard deviation: `sd = scale * sqrt(nu / (nu - 2))`.
- **EWMA** (`EWMAModel(lam=0.94, dist="normal" | "t", nu, init=30)`), RiskMetrics:
  - The recursion is `sigma2_{t+1} = lam sigma2_t + (1 - lam) L_t^2`, with zero mean.
  - Start: the zero-mean (uncentred) variance `mean(L^2)` of the first `init`
    window losses, consistent with the zero-mean recursion;
    the recursion then runs over the rest of the window.
  - `dist="t"` needs a fixed `nu`: a unit-variance t, with nu not estimated.
- **GARCH(1,1)** (`GarchTModel(refit_every=20, dist="t")`):
  - The model is `L_t = mu + sigma_t z_t`,
    `sigma2_t = omega + alpha e_{t-1}^2 + beta sigma2_{t-1}`, with z
    unit-variance t(nu), or normal for the QMLE.
  - Fitted by maximum likelihood on the standardised window, with an analytic
    gradient. The recursion starts at the sample variance.
  - Reparametrised so that no constraint can be violated: `omega = exp(w)`,
    `alpha = p g`, `beta = p (1 - g)`, with persistence `p in [0, 0.999]`,
    share `g in [0, 1]` and `nu in [2.05, 200]`.
  - Three deterministic starts. If the best run did not converge, the fit
    raises `FitError`; there is no silent fallback.
  - Between refits the parameters stay fixed, and the variance is rolled
    forward daily with the realised loss.
- **Filtered HS** (`FilteredHS(garch="normal" | "t", refit_every=20)`,
  Barone-Adesi et al. 1999):
  - Fit GARCH(1,1) (Gaussian QMLE by default) and standardise the residuals,
    `z = (L - mu) / sigma`.
  - `VaR = mu + sigma_{t+1} z_(ceil(n alpha))`, with the HS order-statistic
    convention, and `ES = mu + sigma_{t+1} mean(z >= that quantile)`.
- **Predictive distributions:** every model's `Forecast.dist` has a vectorised
  `ppf(u)` (`risklab.dists`). For normal, t, EWMA and GARCH it is location-scale
  normal or t. For HS it is the empirical distribution of the window, and for
  FHS the empirical z, scaled. The ES backtest simulates from it.
- The closed forms (`var_normal`, `es_normal`, `var_t`, `es_t`, and the
  unit-variance t `var_t_std` / `es_t_std`) live in `risklab.analytic`.

### Engine conventions

- At a refit day the model is created fresh and `fit()` gets a read-only copy
  of the window `[t - window, t)`.
- Between refits, a model with `update(loss)` (EWMA, GARCH, FHS) receives
  exactly `L_{t-1}` before it forecasts day t. Models without `update` forecast
  again from their last fit, as in v0.2. Spy tests check both.
- `walk_forward(refit_every=None)` uses the `refit_every` attribute of the
  first model the factory returns (20 for GARCH and FHS), otherwise 1. No
  extra model is created to read it.
- **h-day backtests** (`walk_forward_h`) use **non-overlapping** h-day blocks.
  Overlapping blocks share days, so their exceedances are dependent and the
  coverage tests would be invalid. The price is a small T: 1000 days after the
  window give only 100 ten-day blocks.

### Multi-day horizon (`risklab.horizon`, `risklab.bootstrap`)

- **Square-root-of-time** (`sqrt_time`, `SqrtTimeForecaster`): `VaR_h ~ sqrt(h) VaR_1`.
  It is an approximation, exact only for iid normal losses with zero mean.
- **GARCH Monte Carlo** (`GarchMCForecaster`): simulate N h-day paths of the
  fitted GARCH(1,1)-t from `sigma2_{t+1}`, sum them, and take the empirical
  quantile and the tail mean. The Monte-Carlo error of the VaR comes from a
  distribution-free 95 % interval of binomial order statistics.
- **Stationary block bootstrap** (`BootstrapForecaster`, Politis and Romano 1994):
  - Geometric block lengths with mean `block_len`, wrapping around circularly.
  - `block_len="auto"` uses Politis and White (2004) with the Patton, Politis
    and White (2009) correction (flat-top kernel, `b = (2 G^2 / D_SB)^(1/3) n^(1/3)`
    with `D_SB = 2 g(0)^2`). If the kernel estimate `g(0)` is not positive (e.g. an
    over-differenced series) the block length is undefined, and the code raises
    `BlockLengthError` instead of picking a default.
  - With `block_len = 1` the indices equal the iid bootstrap exactly (same seed).
- **True h-day risk** (`true_var_es_h`):
  - closed form for iid normal;
  - Monte Carlo with a stated error for iid t;
  - Monte Carlo from the true GARCH parameters, given the true variance at the
    origin;
  - not available for regime switching (ValueError).

### Backtest conventions

- The likelihoods use `0 * ln 0 = 0` exactly, with no epsilon.
- The decision is "reject" or "not rejected", never "accepted".
- **Kupiec POF:** `LR_uc` on all T days, chi-square with 1 degree of freedom.
  It is asymptotic, and with few expected exceedances its size is wrong. At
  T = 250 and 99 % it rejects x = 0: LR = -2 T ln 0.99 = 5.03, p = 0.025, and
  P(X = 0) = 8.1 % under H0. With x >= 7 also rejected, the exact size at 5 %
  is 9.48 %; at T = 1000 it is 5.51 %. `exact_size("kupiec_pof", T, alpha)`
  computes it.
- **Exact binomial test** (`binomial_exact`): two-sided exact test of the
  exceedance count (the minimum-likelihood p-value of `scipy.stats.binomtest`).
  Its size is at most the nominal level for every T: 4.12 % at T = 250, where
  it cannot reject x = 0, and 3.65 % at T = 1000. Use it when the size matters.
- **Christoffersen independence:** first-order Markov transition counts over
  days 2..T, chi-square with 1 degree of freedom. In degenerate cases (no
  exceedance, no transition out of state 1) the statistic is reported as
  computed, `notes` explains why, and parameters that are not identified are
  `None`.
- **Christoffersen conditional coverage:** `LR_cc = LR_uc + LR_ind`,
  chi-square with 2 degrees of freedom. Following Christoffersen (1998), both
  parts are computed on the T-1 transitions, so `LR_uc` here uses days 2..T.
  `LR_cc` is then exactly the joint likelihood ratio.
- **Acerbi-Szekely Z2** (ES, `acerbi_szekely_z2`):
  - `Z2 = sum L_t I_t / (T (1 - alpha) ES_t) - 1`, with `I_t = 1{L_t > VaR_t}`.
  - Loss sign: Z2 > 0 means the realised tail losses exceed the predicted ES,
    i.e. risk is underestimated. The test is one-sided against that. This Z2
    is minus the paper's P&L-sign Z2, which rejects for negative values.
  - p-value by Monte Carlo under H0: simulate every day's loss from
    `Forecast.dist` M times (fixed seed) and set
    `p = (1 + #{Z2_sim >= Z2}) / (1 + M)`.
- **Traffic light** (BCBS 1996):
  - Uses the last `window` days (250 by default). The zones follow from the
    binomial CDF under H0: green while `P(X <= x) < 95 %`, red once
    `>= 99.99 %`.
  - At 250 days and 99 % this gives green 0-4, yellow 5-9 and red 10+.
  - The plus factor (0.40 / 0.50 / 0.65 / 0.75 / 0.85 for 5-9 exceptions,
    1.00 from 10) is reported only for 250 days at 99 %.

## Data policy

- Only synthetic data goes into the repo (`risklab.data.synthetic`: i.i.d. normal,
  scaled Student-t, GARCH(1,1) and two-state regime switching).
- Each generator stores the true per-day parameters. `risklab.data.true_var_es`
  turns them into the true conditional VaR / ES, used to measure estimator error
  and test power. For regime switching, the truth is conditional on the realised
  state of the day: an oracle that no model can reach from past data alone.
- Your own data stays in `local_data/`, which git ignores. Exports go to `out/`,
  which git also ignores. No third-party or licensed data goes into the repo.

## Layout

```
RiskLab/
  pyproject.toml        package "risklab", src layout, Python >= 3.10
  requirements.in       direct dependencies (numpy, scipy), unpinned
  requirements-report.in  optional extra "report" (matplotlib), unpinned
  ci/risklab-tests.yml  GitHub Actions workflow (copy to .github/workflows/)
  src/risklab/
    conventions.py      sign / alpha / seed conventions
    analytic.py         closed-form normal / t VaR and ES
    data/synthetic.py   synthetic generators -> SyntheticSeries
    data/truth.py       true conditional VaR / ES of a SyntheticSeries
    dists.py            predictive distributions (LocScale, Empirical)
    models/             RiskModel protocol, Forecast, FitError; HS, normal, Student-t,
                        EWMA, GARCH(1,1), filtered HS
    engine.py           walk_forward (stateful models), walk_forward_h (h-day blocks)
    horizon.py          sqrt-of-time, GARCH Monte Carlo, bootstrap h-day forecasters
    bootstrap.py        stationary block bootstrap, Politis-White block length
    backtest/           TestResult; Kupiec, binomial, Christoffersen, traffic light, Z2
    report/export.py    deterministic JSON / CSV / table export (default folder RiskLab/out/)
    report/figures.py   the seven report figures (needs the "report" extra)
    report/tables.py    the seven LaTeX tables incl. the findings register
  studies/
    var_backtest_power.py   size / power of the VaR and ES backtests on synthetic data
    horizon_10d.py          10-day VaR: sqrt(10) vs GARCH Monte Carlo vs block bootstrap
    estimation_window.py    over-rejection of GARCH-t / FHS against the window length
    survivorship.py         survivorship bias of growth base rates (synthetic universe)
    results/                full-run summaries (.md) and results (.json), synthetic
  report/
    validation_report.tex   the report skeleton (\usertext slots)
    build_figures.py        figures -> report/figures/
    build_report.py         figures + tables + pdflatex -> out/report/
    figures/, tables/       generated figures (PNG, PDF, LaTeX snippets) and tables
    validation_report_draft.pdf   the current draft (synthetic data only)
  tests/                unittest, no network
```

## Running the tests

From `UFOS.ai/RiskLab/`:

```
python -m pip install -r requirements.in
python -m unittest discover -s tests
```

A path shim in the tests puts `src/` on `sys.path`, so no install and no
`PYTHONPATH` are needed. `-t .` (package mode, used by CI) works as well. The
studies run in a small "smoke" mode inside the tests.

The core tests need only numpy and scipy. The figure tests need the optional
extra (`pip install -r requirements-report.in`, or `pip install -e .[report]`);
without matplotlib they are skipped with a message saying so.

## The size / power study

```
python studies/var_backtest_power.py                # M = 30: a demo; measured 126 s (2.1 min) on the user's PC
python studies/var_backtest_power.py --mode full --save-results    # M = 1000: about 43 minutes in the cloud container
python studies/var_backtest_power.py --m 300 --out-dir out/my_run
```

What it does:

- Each replication draws `window + T` synthetic days (T = 250 and T = 1000;
  window = 250 unless stated).
- The models are fitted walk-forward on a rolling window and forecast the
  next day's 99 % VaR and 97.5 % ES.
- Refits: static models daily; the Student-t model every 50 days; GARCH-t, FHS
  and EWMA-t every 50 days, with a daily variance update in between.
- Kupiec, the exact binomial test, ind, cc (all on the 99 % VaR, at 5 %) and
  the traffic light run on the T forecast days. The Acerbi-Szekely Z2 test
  (97.5 % ES, one-sided, 500 Monte-Carlo draws) runs on the same days.
- Its Monte-Carlo seed is a `SeedSequence` child of the data seed.
- **Reference size:**
  - Kupiec and the binomial test: the exact size per T, from binomial sums.
  - ind, cc and Z2: the empirical rate of the oracle row of the same data and T.
- **Size** rows:
  - the oracle (the true VaR) on all generators;
  - correctly specified estimated models: normal on normal, t on t, and
    GARCH-t and FHS on GARCH-t data. Their rate can differ from the
    reference: that is estimation risk.
  - GARCH-t and FHS also run with a **1000-day estimation window** (T = 1000)
    to isolate that estimation risk.
  - HS on normal data is marked "not a clean size row", because of the
    1.20 % HS exceedance probability.
- **Power** rows: normal on t data; normal and HS on GARCH and regime data.
  These rates are **not size-adjusted**: compare each with the reference size
  of the same T and test.
- **Conditional** rows: EWMA-t on GARCH-t data, and GARCH-t, FHS and EWMA-t on
  regime data (adaptive, but misspecified).
- The summary ends with a **static against conditional** table: Kupiec, the
  binomial test, cc and Z2 side by side, each with its reference size.
- Every rate carries its Monte-Carlo standard error, `se = sqrt(r (1 - r) / M)`.
  Near 5 % that is ±4.0 points at M = 30 and ±0.7 points at M = 1000.
  M = 30 is a demo; use `--mode full` for conclusions.

Output: `out/var_backtest_power/results.csv`, `results.json` and `summary.md`.
The full run (M = 1000, seed 20261105) is kept in
[`studies/results/var_backtest_power_full_summary.md`](studies/results/var_backtest_power_full_summary.md).
Its main results, as rejection rates in % ± se:

- **Kupiec's size is wrong at T = 250.**
  - Exact size: 9.48 % at T = 250, 5.51 % at T = 1000.
  - Oracle rows: 8.7 ± 0.9 to 10.6 ± 1.0 at T = 250; 4.6 ± 0.7 to 7.4 ± 0.8
    at T = 1000.
  - The exact binomial test stays near or below 5 %: 3.8 ± 0.6 to 5.3 ± 0.7
    at T = 250.
- **The binomial test can find more than Kupiec.** Normal VaR on t5 data at
  T = 250: Kupiec 10.8 ± 1.0 against its size of 9.5, so practically no power.
  The exact binomial test reaches 19.2 ± 1.2 against a size of 4.1.
- **The ES test holds its size and adds power.**
  - Z2 on the oracle rows: ≈ 5 % (4.4 ± 0.6 to 6.5 ± 0.8).
  - Normal on t5 data at T = 250: Z2 18.2 ± 1.2 against Kupiec 10.8 ± 1.0.
  - HS on GARCH-t data at T = 1000: Z2 37.9 ± 1.5 against cc 19.8 ± 1.3.
- **Conditional against static models** (T = 1000, GARCH-t data; the reference
  sizes are 5.5 Kupiec, 3.6 binomial, 1.7 ± 0.4 cc and 4.6 ± 0.7 Z2):

  | model | exceed rate | Kupiec | binomial | cc | Z2 |
  |---|---|---|---|---|---|
  | static normal | 1.87 % | 65.2 ± 1.5 | 65.2 ± 1.5 | 59.7 ± 1.6 | 64.7 ± 1.5 |
  | static HS | 1.38 % | 20.1 ± 1.3 | 19.9 ± 1.3 | 19.8 ± 1.3 | 37.9 ± 1.5 |
  | GARCH-t, window 250 | 1.25 % | 10.6 ± 1.0 | 10.5 ± 1.0 | 6.2 ± 0.8 | 17.7 ± 1.2 |
  | FHS, window 250 | 1.35 % | 15.3 ± 1.1 | 15.2 ± 1.1 | 9.8 ± 0.9 | 33.6 ± 1.5 |
  | EWMA-t | 1.24 % | 7.6 ± 0.8 | 7.5 ± 0.8 | 4.1 ± 0.6 | 20.9 ± 1.3 |
  | GARCH-t, window 1000 | 1.03 % | 5.8 ± 0.7 | 4.3 ± 0.6 | 2.5 ± 0.5 | 4.9 ± 0.7 |
  | FHS, window 1000 | 1.12 % | 5.5 ± 0.7 | 4.9 ± 0.7 | 2.4 ± 0.5 | 5.7 ± 0.7 |

  - With a 250-day window, the conditional models are rejected far less often
    than the static normal model: in all 48 comparisons (models x data x T x
    tests).
  - Against static HS the gap is smaller: lower in 44 of 48 comparisons. On
    regime data at T = 1000, FHS and EWMA-t are rejected about as often as HS
    by Kupiec and the binomial test.
  - **But they are not "passing".** With a 250-day window and refits every 50
    days, even the correctly specified GARCH-t over-rejects at about twice the
    reference size: Kupiec 12.0 / 10.6 %, binomial 11.7 / 10.5 % and
    Z2 12.2 / 17.7 % at T = 250 / 1000. Its exceedance rate is 1.25 %.
  - FHS's Z2 rate reaches 33.6 %.
- **This over-rejection is estimation risk.** With a 1000-day estimation
  window, GARCH-t has 1.03 % exceedances and FHS 1.12 %, and every test is back
  near its reference size (table above). Short-window GARCH estimates are too
  noisy for a 99 % VaR.
- **Clustering is hard to see** with the independence test alone. It is
  undersized (0.9-2.6 % on the oracle rows) and weak: 15.7 ± 1.2 against the
  static normal model on GARCH-t data at T = 1000.
- Seeds: the oracle, iid and GARCH-normal rows use the same data as v0.2.
  The regime rows use new data seeds since v0.3, because the GARCH-t generator
  shifted the generator index. The Z2 rates changed slightly with the
  independent Monte-Carlo seeds.

## The 10-day horizon study

```
python studies/horizon_10d.py                # M = 200: a demo; measured 30 s on the user's PC
python studies/horizon_10d.py --mode full --save-results    # M = 1000: about 2 minutes in the cloud container
```

What it does:

- Data: iid normal, iid t5, and GARCH(1,1)-t6.
- Every replication uses a 500-day window and estimates the 10-day 99 % VaR
  in four ways:
  - `sqrt10_normal`: sqrt(10) x the normal one-day VaR;
  - `sqrt10_garch`: sqrt(10) x the GARCH-t one-day VaR;
  - `garch_mc`: GARCH-t Monte Carlo with 20 000 paths;
  - `bootstrap`: the stationary bootstrap with an automatic block length,
    20 000 paths.
- Each estimate is compared with the true 10-day VaR: closed form for iid
  normal, otherwise Monte Carlo with 50 000 paths.
- Seeds: the GARCH Monte Carlo, the bootstrap and the Monte-Carlo truth use
  independent `SeedSequence` children of the data seed.
- Reported: the relative bias ± its se, the relative RMSE, and the simulation
  errors of the estimate and of the truth.

The full run (M = 1000) is in
[`studies/results/horizon_10d_full_summary.md`](studies/results/horizon_10d_full_summary.md).
Its main results, as relative bias ± se and RMSE:

- **iid normal:** sqrt(10) x normal is unbiased (-0.0 ± 0.1 %, RMSE 3.6 %).
  The simulation methods add error: GARCH MC 6.9 % and bootstrap 7.3 % RMSE.
- **iid t5:**
  - sqrt(10) x the fat-tailed GARCH-t one-day VaR overstates the 10-day VaR
    (+8.6 ± 0.3 %), as the central limit theorem predicts.
  - GARCH MC (+0.7 ± 0.3 %) and the bootstrap (+0.3 ± 0.5 %) are close to unbiased.
- **GARCH-t6:**
  - sqrt(10) x normal (-0.7 ± 0.9 %), sqrt(10) x GARCH (+0.6 ± 0.3 %) and
    GARCH MC (-0.3 ± 0.4 %) are close to unbiased on average.
  - The bootstrap is biased: +3.7 ± 1.2 %, about 3 se. Its automatic block
    length is chosen on the loss levels (mean 1.96 days), which are nearly
    uncorrelated under GARCH, because the dependence is in the squares. It
    also resamples the whole window, so it does not condition on the current
    volatility.
  - The RMSE separates the methods: bootstrap 37.2 %, sqrt(10) x normal
    29.3 % (it ignores the current volatility), GARCH MC 11.5 % and
    sqrt(10) x GARCH 9.8 %.
  - With persistence 0.98, volatility mean-reverts slowly over 10 days, so
    scaling the conditional one-day VaR is as good as the full simulation here.

## The estimation-window study

```
python studies/estimation_window.py                # M = 20: a demo; measured 58 s on the user's PC
python studies/estimation_window.py --mode full --save-results    # M = 1000: about 34 minutes in the cloud container
```

- Data: GARCH(1,1)-t6, with T = 1000 forecast days.
- Models: GARCH-t and FHS, each with estimation windows of 250, 500, 750 and
  1000 days, refitted every 50 days.
- All windows forecast the same 1000 days (a paired design).
- Reported:
  - the exceedance rate and its **paired difference against the oracle**:
    the per-replication difference on the same data and days, mean ± se;
  - the Kupiec / binomial / cc / Z2 rejection rates with their reference sizes;
  - the VaR error against the truth.

The full run is in
[`studies/results/estimation_window_full_summary.md`](studies/results/estimation_window_full_summary.md).

Results in % ± se; the reference sizes are Kupiec 5.51, binomial 3.65,
cc 2.0 ± 0.4 and Z2 5.0 ± 0.7, and the oracle exceedance rate is 1.01 %:

| model | window | exceed rate | paired diff vs oracle (pp) | Kupiec | binomial | cc | Z2 |
|---|---|---|---|---|---|---|---|
| GARCH-t | 250 | 1.27 | +0.259 ± 0.010 | 11.8 ± 1.0 | 11.6 ± 1.0 | 6.7 ± 0.8 | 19.5 ± 1.3 |
| GARCH-t | 500 | 1.11 | +0.096 ± 0.008 | 4.5 ± 0.7 | 4.2 ± 0.6 | 2.4 ± 0.5 | 7.4 ± 0.8 |
| GARCH-t | 750 | 1.06 | +0.052 ± 0.007 | 3.6 ± 0.6 | 2.7 ± 0.5 | 2.2 ± 0.5 | 5.6 ± 0.7 |
| GARCH-t | 1000 | 1.05 | +0.041 ± 0.007 | 3.7 ± 0.6 | 2.9 ± 0.5 | 2.4 ± 0.5 | 6.0 ± 0.8 |
| FHS | 250 | 1.37 | +0.355 ± 0.013 | 15.9 ± 1.2 | 15.9 ± 1.2 | 9.8 ± 0.9 | 36.0 ± 1.5 |
| FHS | 500 | 1.25 | +0.235 ± 0.011 | 6.7 ± 0.8 | 6.7 ± 0.8 | 5.1 ± 0.7 | 8.3 ± 0.9 |
| FHS | 750 | 1.09 | +0.080 ± 0.010 | 2.7 ± 0.5 | 2.4 ± 0.5 | 1.6 ± 0.4 | 6.3 ± 0.8 |
| FHS | 1000 | 1.13 | +0.115 ± 0.010 | 4.4 ± 0.6 | 4.3 ± 0.6 | 2.9 ± 0.5 | 6.3 ± 0.8 |

- **The rejection rates** fall to about the reference size by 500 days for
  GARCH-t and by 750 days for FHS.
- **But the VaR stays slightly anti-conservative.** The exceedance rate is
  1.05-1.13 % against the oracle's 1.01 %. The paired difference stays
  significant even at 1000 days: +0.041 ± 0.007 points for GARCH-t and
  +0.115 ± 0.010 for FHS.
- This is parameter uncertainty. With M = 1000, the tests on T = 1000 days
  cannot see such a small excess, but the paired comparison with the truth
  can.
- **Two 1000-day GARCH-t Kupiec rates, reconciled.** This study gives
  3.7 ± 0.6 %; the 1000-day row of the power study gives 5.8 ± 0.7 %.
  - The code and the design are the same: series of 2000 days, a 1000-day
    window, refits every 50 days, the same model and tests. I checked this
    per replication: on the same series both paths give identical exceedances.
  - Only the data seeds differ.
  - The difference, 2.1 ± 0.9 points (2.3 se), is Monte-Carlo noise. There
    are 8 such comparisons between the two studies (4 tests × 2 models at
    1000 days), and the chance that at least one differs by 2.3 se or more is
    about 16 %.
  - Pooled: 4.75 ± 0.48 %, against an exact size of 5.51 %.

## The survivorship study

```
python studies/survivorship.py                # M = 200: a demo, a few seconds
python studies/survivorship.py --mode full --save-results    # M = 2000: measured 7 s in the cloud container
```

**Setup** (synthetic, no real firms or data):

- 2000 firms over 10 years. Each firm has a log-growth drift drawn from
  N(5 %, 5 %) and yearly shocks with sd 15 %.
- Each year a firm exits with probability `min(0.9, h0 exp(-kappa z))`, where
  z is that year's standardised growth. Poor growth raises the hazard.

**Three base rates** for "the share of firms with a 10-year CAGR >= g", each
answering a different question:

- **naive** (survivors only): what share of today's listed firms grew this fast?
  This is what a database of current constituents shows.
- **counterfactual** (all firms, had they survived): what share of firms has the
  growth driver for this? Every growth path is simulated in full, so the CAGR
  exists for exited firms too.
- **full cohort** (exited firms count as misses): how likely is a firm picked
  at the start to achieve it as a going concern? This is the forecasting base
  rate.

**Decomposition** of the naive bias (naive - full cohort):

- **selection** = naive - counterfactual: the survivors are not a random
  sample when exits depend on growth;
- **attrition** = counterfactual - full cohort: firms that would have reached
  the threshold but exited count as misses.

**Results** (full run, CAGR >= 10 %, percentage points ± Monte-Carlo se):

| h0 | kappa | survival | naive | counterfactual | full cohort | selection | attrition | total |
|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 100.0 % | 25.6 | 25.6 | 25.6 | 0.0 | 0.0 | 0.0 |
| 0.04 | 0 | 66.5 % | 25.6 | 25.6 | 17.0 | 0.0 ± 0.02 | 8.6 ± 0.01 | 8.6 ± 0.01 |
| 0.04 | 1 | 51.8 % | 34.3 | 25.5 | 17.8 | 8.8 ± 0.02 | 7.8 ± 0.01 | 16.5 ± 0.02 |
| 0.04 | 2 | 23.8 % | 56.4 | 25.5 | 13.4 | 30.9 ± 0.04 | 12.1 ± 0.02 | 43.0 ± 0.04 |

- With zero hazard all three rates coincide exactly (tested).
- With exits unrelated to growth (kappa = 0), the survivors are a random
  sample: the naive rate equals the counterfactual one, and the selection bias
  is 0. The naive rate then overstates only the full-cohort rate, because
  exited firms count as misses (attrition 8.6 points).
- With kappa > 0, the exits remove the slow growers, and the selection bias
  grows quickly (8.8 points at kappa = 1, 30.9 at kappa = 2).
- **Modelling choices:**
  - Acquisitions are treated like failures. A takeover after a strong decade
    would reduce the bias.
  - The ± se is Monte-Carlo noise only. The size of the bias is driven by the
    assumed hazard (h0, kappa) and the growth model, not by the simulation
    error.

## The validation report

**Draft:** [`report/validation_report_draft.pdf`](report/validation_report_draft.pdf)
(synthetic data only). The prose is written by the author. Every open text slot
is an orange `\usertext{...}` box with guidance, so a build with open slots is
visibly a draft; the title page counts the open slots.

Build it from `UFOS.ai/RiskLab/` (needs the "report" extra and a TeX
installation):

```
pip install -r requirements-report.in
python report/build_report.py              # -> out/report/validation_report.pdf
python report/build_report.py --publish    # also copies it to report/validation_report_draft.pdf
```

**What the build does:**

- It checks the toolchain first: matplotlib, `pdflatex`, `kpsewhich`, the
  LaTeX packages and the four study result files. Anything missing stops the
  build with a clear message; there is no fallback renderer.
- It writes the figures to `report/figures/` and the tables to
  `report/tables/`.
- It runs `pdflatex` twice, and a third time only if LaTeX asks for a rerun.

**Required LaTeX packages:** geometry, graphicx, xcolor, booktabs, array,
amsmath, amssymb, caption, float, hyperref. These are standard TeX Live
packages; no lmodern or babel is needed, because the fonts are Computer
Modern.

**Inputs:** the figures and tables read the saved full-run results in
`studies/results/*_full_results.json`. Figures 1 and 6 come from fixed-seed
simulations.

To refresh an input, run its study with `--mode full --save-results`. This
writes `out/<study>/` and copies `results.json` and `summary.md` to
`studies/results/<study>_full_results.json` and `_full_summary.md`.

**No stale inputs:**

- Every results file records a code fingerprint (`meta.code_sha256`). It is a
  sha256 over the study script plus every `src/risklab/**/*.py` outside
  `risklab/report/`, with LF-normalised line endings.
- `build_report.py` recomputes the fingerprint and refuses an input whose
  fingerprint is missing or differs. `--allow-stale` builds anyway and prints a
  warning for each stale file.
- A test checks that the committed results match the current code.

**Reproducibility:**

- The figures use the Agg backend and a style fixed in code, which ignores any
  user `matplotlibrc`. Fonts are DejaVu, sizes are fixed, the PNG files carry
  no software tag and the PDF files no creation date.
- With the same matplotlib and FreeType versions, the files are byte-identical
  (tested).
- Across versions the bytes differ. matplotlib 3.10.9 (the user's PC) and
  3.11.2 (the container) gave different PNG / PDF bytes for every figure, but
  identical LaTeX snippets, including the numbers in the captions.
- The report PDF is built with a fixed `SOURCE_DATE_EPOCH`, no PDF date and an
  empty trailer ID. Two builds gave byte-identical PDFs in the container.

**File names and PDF numbering:** LaTeX numbers figures and tables in the
order they appear in the report, so the numbers differ from the file names:

| report | file | report | file |
|---|---|---|---|
| Figure 1 | `fig7_survivorship` | Table 1 | `T1_model_inventory` |
| Figure 2 | `fig6_qq_residuals` | Table 2 | `T6_survivorship` |
| Figure 3 | `fig1_exceedances` | Table 3 | `T2_size` |
| Figure 4 | `fig2_rejection_rates` | Table 4 | `T3_power` |
| Figure 5 | `fig4_traffic_light` | Table 5 | `T4_estimation_window` |
| Figure 6 | `fig3_estimation_window` | Table 6 | `T5_horizon` |
| Figure 7 | `fig5_horizon` | Table 7 | `T7_findings` |

**Gallery:**

Rejection rates by model and test (report Figure 4):

![Rejection rates by model and test](report/figures/fig2_rejection_rates.png)

Estimation-window curve (report Figure 6):

![Estimation-window curve](report/figures/fig3_estimation_window.png)

Survivorship bias: three base rates, selection and attrition (report Figure 1):

![Survivorship bias](report/figures/fig7_survivorship.png)

## Open points

- **Pinned lock:** the repo rule asks for pinned, hash-checked dependencies.
  The lock (`requirements.txt` with hashes) is generated later on the user's PC.
  Until then `requirements.in` lists the unpinned direct dependencies.
- **CI:** copy `ci/risklab-tests.yml` to `.github/workflows/` (`.github` is protected).
- **CI hardening:** pin the GitHub actions (`actions/checkout`, `actions/setup-python`)
  to full commit SHAs instead of version tags.

## Roadmap

- **v0.1:** the skeleton.
- **v0.2:** HS, normal and t VaR / ES; true VaR / ES; Kupiec, the exact
  binomial test, Christoffersen and the traffic light; the size / power study.
- **v0.3:** EWMA, GARCH(1,1)-t and filtered HS with daily
  state updates; h-day VaR / ES (sqrt-of-time, GARCH Monte Carlo, stationary
  bootstrap); the Acerbi-Szekely ES test; the 10-day horizon study.
- **v0.4 (this version):** the validation report pipeline (figures, tables,
  LaTeX skeleton, draft PDF), the estimation-window and survivorship studies.
- **Next:** the author writes the report prose (the text slots); then the
  user's own data in `local_data/` for an out-of-sample section (never in
  the repo).
- **Next (open):** further ES tests (Z1, Z3), multivariate portfolios, and real
  data in `local_data/` (never in the repo).
