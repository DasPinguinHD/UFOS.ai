# Risk Lab conventions

The binding conventions of the Risk Lab (signs, confidence levels, seeds), the model, engine,
horizon and backtest conventions, and the data policy. Moved here from the README in v0.6
so that the README stays short; the content is unchanged.

## General

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

## Interest rates (`risklab.rates`, v0.7)

- **Time** is in years from today, as exact year fractions; no day-count
  calendar is modelled.
- **Rates** are decimals, continuously compounded zero rates `z(t)`:
  - discount factor `D(t) = exp(-z(t) t)`;
  - conversion to m-times-a-year compounding: `r_m = m (exp(z / m) - 1)`;
  - par yields are those of annual-coupon bonds.
- **Signs:** assets carry positive cash flows and liabilities negative ones.
  - `EVE` is the PV of all run-off cash flows, equity excluded.
  - `Delta EVE = EVE(shocked) - EVE(base)`: negative is a loss.
  - The outlier tests compare the largest decline with Tier 1.
- **Shocks** are added to the zero curve and then floored at
  `min(floor(t), z(t))`. The shapes, sizes, the floor and their sources are
  in the docstring of `risklab.rates.irrbb` (`SHOCK_SOURCES`).
- **Curves from par yields:** par yields are fitted directly with Nelson-Siegel
  or Nelson-Siegel-Svensson (least squares on the par yields of the fitted
  curve). There is no bootstrapping.
- **Non-maturity deposits:** the replicating portfolio's core tranches run off
  monthly over `2 x avg_maturity` years. Their mean maturity is therefore
  `≈ avg_maturity + 1/24` years.
- **Unverified parameters** are not guessed. A parameter that could not be
  checked against a primary text is marked `ASSUMPTION` or `verify` in the
  results' assumptions register.

## Credit risk (`risklab.credit`, v0.8)

- **PD, LGD, EAD** are decimals and currency units. The PD is a one-year
  probability.
- **Defaults** follow a one-factor Gaussian copula. Obligor i defaults when
  `sqrt(rho) Z + sqrt(1 - rho) e_i < G(PD_i)`. A low Z means many defaults.
- **Logistic fits** stop with an explicit `SeparationError` or
  `ConvergenceError` (subclasses of `FitError`). They are never silently
  regularised.
- **The AUC** is the Mann-Whitney statistic, with ties counted 1/2. The
  Gini (accuracy ratio) is `2 AUC - 1`.
- **Hosmer-Lemeshow:** `hosmer_lemeshow(..., fitted_on_sample=...)` must
  state the case. Degrees of freedom g - 2 only for the development sample of
  a fitted model; g for the true PD or a model applied out of time.
- **IFRS 9:** default at the end of each year, with the exposure at the
  start of the year, discounted at the EIR. The 12-month ECL has a horizon of
  1 year, the lifetime ECL the remaining maturity, with a constant hazard.
  Stage 3 has PD = 1 and is undiscounted (LGD x EAD).
- **IFRS 9 staging:** the current 12-month PD and the PD at origination are
  compared on the same basis: both base-scenario PDs without the scenario
  overlay. The scenarios change the ECL amount, not the stage.
- **IRB:** K is per unit of EAD and RWA = 12.5 K EAD (× `scaling`). The PD
  floor and the M bounds are parameters.
- **Sources:** the register in the results meta (`irb_sources`,
  `ifrs9_sources`) gives every source. Items that are not verified are marked
  ASSUMPTION.


## Valuation and sensitivity (`risklab.valuation`, `risklab.sensitivity`, v0.9)

- **Rates and growth** are annual decimals. Free cash flows arrive at the
  END of each year (no mid-year convention). The terminal value is the Gordon
  value `FCF_N (1 + g_T) / (r - g_T)` at year N.
- **Growth path:** constant, or a linear fade from the start growth in year 1
  to the terminal growth in year N (`fade=True`, N >= 2).
- **Equity bridge:** equity = EV - net debt - other claims; the value per
  share is equity / shares.
- **Invalid inputs** (`r <= g_T`, growth <= -100 %, non-integer years, shares
  <= 0, non-finite values) raise `DCFInputError`. Nothing is clipped.
- **Reverse DCF:** Brent's method on an explicit bracket. If the price is
  outside the values at the bracket ends, `NoRootInBracket` reports both
  values; there is no extrapolation. It needs `fcf0 > 0`, which makes the
  root unique.
- **Base rates:** the universe reproduces the v0.4 survivorship study draw
  for draw (a parity test guards it). Comparing FCF growth with its revenue
  growth assumes a constant FCF margin.
- **Tornado:** each input alone at the ends of its range, sorted by swing.
  Ties keep the input order.
- **Two-way tables:** a cell is NaN only for exception types the caller
  passes in `invalid`; any other error propagates.
- **Sobol indices:**
  - Saltelli sampling with matrices A, B and A_B^(i), N (k + 2) runs, plain
    Monte Carlo.
  - First order with outputs centred by their mean; total order by the
    Jansen estimator.
  - Bootstrap over the N rows for the se and a percentile interval.
  - Estimates are reported as estimated, never clipped to [0, 1].
  - Inputs must be independent, and the indices hold only for the stated
    ranges. Whole-number inputs (e.g. years) use `DiscreteUniform`.
