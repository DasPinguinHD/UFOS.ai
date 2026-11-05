# Risk Lab

A bank-style market-risk lab: estimate one-day Value-at-Risk (VaR) and Expected
Shortfall (ES), and backtest the forecasts the way a bank's risk management does.
It is a showcase for risk management and is part of UFOS.ai. It is independent of
the HedgeFund backend: there are no imports between them, and it has its own
tests and dependencies.

**Status: v0.2.** Implemented:

- the models: historical simulation (HS), normal and Student-t VaR / ES;
- the true VaR / ES of the synthetic generators;
- the backtests: Kupiec POF, the exact binomial test, Christoffersen
  independence and conditional coverage, and the Basel traffic light;
- a size / power study as the first showcase result.

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
- The closed forms (`var_normal`, `es_normal`, `var_t`, `es_t`, and the
  unit-variance t `var_t_std` / `es_t_std`) live in `risklab.analytic`.

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
  ci/risklab-tests.yml  GitHub Actions workflow (copy to .github/workflows/)
  src/risklab/
    conventions.py      sign / alpha / seed conventions
    analytic.py         closed-form normal / t VaR and ES
    data/synthetic.py   synthetic generators -> SyntheticSeries
    data/truth.py       true conditional VaR / ES of a SyntheticSeries
    models/             RiskModel protocol, Forecast; HS, normal, Student-t
    engine.py           walk_forward -> BacktestFrame (no look-ahead)
    backtest/           TestResult; Kupiec, Christoffersen, traffic light
    report/export.py    deterministic JSON / CSV / table export (default folder RiskLab/out/)
  studies/
    var_backtest_power.py   size / power of the backtests on synthetic data
    results/                saved full-run summary (synthetic)
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
study runs in a small "smoke" mode inside the tests.

## The size / power study

```
python studies/var_backtest_power.py                # M = 100: a demo, about 1-1.5 minutes
python studies/var_backtest_power.py --mode full    # M = 1000: for conclusions, about 8-15 minutes
python studies/var_backtest_power.py --m 300 --out-dir out/my_run
```

What it does:

- Each replication draws `250 + T` synthetic days (T = 250 and T = 1000).
- The models are fitted walk-forward on a rolling 250-day window and forecast
  the next day's 99 % VaR. The Student-t model is refitted every 50 days
  (maximum likelihood is the expensive step), the other models daily.
- Kupiec, the exact binomial test, ind and cc (all at 5 %) and the traffic
  light run on the T forecast days.
- **Reference size:**
  - Kupiec and the binomial test: the exact size per T, from binomial sums.
  - ind and cc: the empirical rate of the oracle row of the same data and T.
- **Size** rows:
  - the oracle (the true VaR) on all generators;
  - correctly specified estimated models on iid data. Their rate can differ
    from the reference: that is estimation risk.
  - HS on normal data is marked "not a clean size row", because of the
    1.20 % HS exceedance probability.
- **Power** rows: normal on t data; normal and HS on GARCH and regime data.
  These rates are **not size-adjusted**: compare each with the reference size
  of the same T and test.
- Every rate carries its Monte-Carlo standard error, `se = sqrt(r (1 - r) / M)`.
  Near 5 % that is ±2.2 points at M = 100 and ±0.7 points at M = 1000.
  M = 100 is a demo; use `--mode full` for conclusions.

Output: `out/var_backtest_power/results.csv`, `results.json` and `summary.md`.
The full run (M = 1000, seed 20261105) is kept in
[`studies/results/var_backtest_power_full_summary.md`](studies/results/var_backtest_power_full_summary.md).
Its main results, as rejection rates in % ± se:

- **Kupiec's size is wrong at T = 250.**
  - Exact size: 9.48 % at T = 250, 5.51 % at T = 1000.
  - Oracle rows: 8.7 ± 0.9 (iid normal) to 10.6 ± 1.0 (GARCH) at T = 250;
    4.8 ± 0.7 to 7.4 ± 0.8 at T = 1000.
  - The exact binomial test stays near or below 5 %: 4.0 ± 0.6 to 5.3 ± 0.7
    at T = 250.
- **The binomial test can find more than Kupiec.** Normal VaR on t5 data at
  T = 250: Kupiec 10.8 ± 1.0 against its size of 9.5, so practically no power.
  The exact binomial test reaches 19.2 ± 1.2 against a size of 4.1. At T = 1000
  both reach 40.6 ± 1.6.
- **Clustering is hard to see.**
  - The independence test is undersized: 0.9-2.6 % on the oracle rows.
  - Against static models on GARCH data it is weak: 3.8 ± 0.6 at T = 250 and
    16.1 ± 1.2 at T = 1000 for the normal model.
  - Against the normal model on regime data the coverage tests do much better:
    cc rejects 36.7 ± 1.5 at T = 250 and 90.5 ± 0.9 at T = 1000.
- **The traffic light** puts about 10-12 % of correct models into yellow at T = 250
  (oracle rows). The normal model on regime data is in red in 13.1 % of the
  runs.

## Open points

- **Pinned lock:** the repo rule asks for pinned, hash-checked dependencies.
  The lock (`requirements.txt` with hashes) is generated later on the user's PC.
  Until then `requirements.in` lists the unpinned direct dependencies.
- **CI:** copy `ci/risklab-tests.yml` to `.github/workflows/` (`.github` is protected).
- **CI hardening:** pin the GitHub actions (`actions/checkout`, `actions/setup-python`)
  to full commit SHAs instead of version tags.

## Roadmap

- **v0.1:** the skeleton.
- **v0.2 (this version):** HS, normal and t VaR / ES; true VaR / ES; Kupiec,
  Christoffersen and the traffic light; the size / power study.
- **v0.3:** GARCH, filtered historical simulation (FHS), Monte Carlo, square-root-of-time
  scaling, and an ES backtest.
