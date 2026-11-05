# VaR / ES backtest size and power (synthetic data)

VaR tests at alpha = 0.99, ES test (Acerbi-Szekely Z2, one-sided, 500 Monte-Carlo draws) at alpha = 0.975; estimation window = 250 days, tests at 5 %, M = 1000 replications per row, seed = 20261105. Refits: Student-t every 50 days, conditional models (GARCH-t, FHS, EWMA-t) every 50 days with a daily variance update in between, static models daily. risklab 0.4.0, numpy 2.5.3, scipy 1.18.1.

**Monte-Carlo error.** Every rate is shown as `rate ± se` with se = sqrt(r (1 - r) / M); at M = 1000 a rate near 5 % has se ≈ ±0.7 percentage points.

## Reference size under H0

| T | Kupiec exact size | Kupiec rejects x in | binomial exact size | binomial rejects x in |
|---|---|---|---|---|
| 250 | 9.48 % | {0} ∪ {7..250} | 4.12 % | {6..250} |
| 1000 | 5.51 % | {0..4} ∪ {17..1000} | 3.65 % | {0..3} ∪ {17..1000} |

Kupiec is asymptotic (chi-square). At T = 250 it rejects x = 0: LR = -2 T ln(1 - p) = 5.03 > 3.84, p = 0.025, and P(X = 0) = 0.081 under H0, so its exact size is far above 5 %. The exact binomial test keeps the size at or below 5 % by construction (it cannot reject x = 0 at T = 250: P(X = 0) alone exceeds 5 %).
For ind, cc and Z2 there is no closed form: their reference is the empirical rate of the oracle row of the same data and T (`ref_size_ind`, `ref_size_cc`, `ref_size_z2` in the CSV). Z2 uses a Monte-Carlo p-value under each day's predictive distribution, so its size is at most 5 % up to Monte-Carlo error.

## Rejection rates (%, ± Monte-Carlo se)

| role | data | model | T | exceed rate | Kupiec | binomial exact | ind | cc | ES Z2 | ind no-info | TL green / yellow / red | mean abs rel VaR error | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| size | iid_normal | oracle | 250 | 1.04 % | 8.7 ± 0.9 | 5.3 ± 0.7 | 0.9 ± 0.3 | 1.1 ± 0.3 | 6.4 ± 0.8 | 6.5 % | 88.0 % / 12.0 % / 0.0 % | 0.0 % | true VaR |
| size | iid_normal | oracle | 1000 | 1.01 % | 5.7 ± 0.7 | 4.1 ± 0.6 | 1.9 ± 0.4 | 2.7 ± 0.5 | 4.8 ± 0.7 | 0.1 % | 88.7 % / 11.3 % / 0.0 % | 0.0 % | true VaR |
| size | iid_t5 | oracle | 250 | 0.98 % | 9.0 ± 0.9 | 4.0 ± 0.6 | 1.0 ± 0.3 | 1.0 ± 0.3 | 5.1 ± 0.7 | 8.0 % | 89.7 % / 10.3 % / 0.0 % | 0.0 % | true VaR |
| size | iid_t5 | oracle | 1000 | 1.00 % | 7.4 ± 0.8 | 5.0 ± 0.7 | 2.4 ± 0.5 | 4.1 ± 0.6 | 6.5 ± 0.8 | 0.0 % | 89.4 % / 10.5 % / 0.1 % | 0.0 % | true VaR |
| size | garch_normal | oracle | 250 | 0.99 % | 10.6 ± 1.0 | 4.5 ± 0.7 | 1.2 ± 0.3 | 1.0 ± 0.3 | 4.6 ± 0.7 | 9.2 % | 89.0 % / 11.0 % / 0.0 % | 0.0 % | true VaR |
| size | garch_normal | oracle | 1000 | 1.01 % | 6.2 ± 0.8 | 4.6 ± 0.7 | 2.6 ± 0.5 | 2.8 ± 0.5 | 4.6 ± 0.7 | 0.0 % | 88.0 % / 12.0 % / 0.0 % | 0.0 % | true VaR |
| size | regime | oracle | 250 | 1.00 % | 10.5 ± 1.0 | 4.3 ± 0.6 | 1.5 ± 0.4 | 1.0 ± 0.3 | 5.0 ± 0.7 | 8.7 % | 88.0 % / 12.0 % / 0.0 % | 0.0 % | truth conditional on the realised state (oracle) |
| size | regime | oracle | 1000 | 0.99 % | 5.1 ± 0.7 | 3.4 ± 0.6 | 1.9 ± 0.4 | 2.6 ± 0.5 | 4.4 ± 0.6 | 0.0 % | 88.7 % / 11.3 % / 0.0 % | 0.0 % | truth conditional on the realised state (oracle) |
| size | garch_t6 | oracle | 250 | 1.01 % | 9.6 ± 0.9 | 3.8 ± 0.6 | 1.6 ± 0.4 | 1.0 ± 0.3 | 4.4 ± 0.6 | 8.3 % | 89.8 % / 10.2 % / 0.0 % | 0.0 % | true VaR |
| size | garch_t6 | oracle | 1000 | 0.98 % | 4.6 ± 0.7 | 2.5 ± 0.5 | 1.5 ± 0.4 | 1.7 ± 0.4 | 4.6 ± 0.7 | 0.0 % | 89.5 % / 10.5 % / 0.0 % | 0.0 % | true VaR |
| size | iid_normal | normal | 250 | 1.08 % | 7.7 ± 0.8 | 4.5 ± 0.7 | 1.1 ± 0.3 | 0.7 ± 0.3 | 6.2 ± 0.8 | 6.2 % | 88.1 % / 11.9 % / 0.0 % | 4.3 % | estimation risk |
| size | iid_normal | normal | 1000 | 1.06 % | 2.9 ± 0.5 | 2.5 ± 0.5 | 0.8 ± 0.3 | 1.3 ± 0.4 | 3.7 ± 0.6 | 0.0 % | 86.4 % / 13.6 % / 0.0 % | 4.2 % | estimation risk |
| size | iid_normal | hs | 250 | 1.21 % | 4.6 ± 0.7 | 5.9 ± 0.7 | 1.9 ± 0.4 | 1.3 ± 0.4 | 8.3 ± 0.9 | 3.1 % | 84.9 % / 15.1 % / 0.0 % | 7.9 % | not a clean size row: HS exceedance probability (n-k+1)/(n+1) = 1.20 % |
| size | iid_normal | hs | 1000 | 1.21 % | 1.9 ± 0.4 | 1.9 ± 0.4 | 1.1 ± 0.3 | 1.3 ± 0.4 | 6.9 ± 0.8 | 0.0 % | 83.6 % / 16.3 % / 0.1 % | 8.0 % | not a clean size row: HS exceedance probability (n-k+1)/(n+1) = 1.20 % |
| size | iid_t5 | student_t | 250 | 1.12 % | 7.4 ± 0.8 | 5.8 ± 0.7 | 1.6 ± 0.4 | 1.5 ± 0.4 | 7.5 ± 0.8 | 5.2 % | 84.5 % / 15.5 % / 0.0 % | 8.2 % | estimation risk; t refit every 50 days |
| size | iid_t5 | student_t | 1000 | 1.09 % | 4.5 ± 0.7 | 3.3 ± 0.6 | 2.5 ± 0.5 | 3.3 ± 0.6 | 4.6 ± 0.7 | 0.0 % | 86.6 % / 13.4 % / 0.0 % | 8.1 % | estimation risk; t refit every 50 days |
| size | garch_t6 | garch_t | 250 | 1.25 % | 12.0 ± 1.0 | 11.7 ± 1.0 | 2.0 ± 0.4 | 4.0 ± 0.6 | 12.2 ± 1.0 | 6.6 % | 78.7 % / 20.8 % / 0.5 % | 11.8 % | correct model class; estimation risk; refit every 50 days |
| size | garch_t6 | garch_t | 1000 | 1.25 % | 10.6 ± 1.0 | 10.5 ± 1.0 | 2.4 ± 0.5 | 6.2 ± 0.8 | 17.7 ± 1.2 | 0.0 % | 80.2 % / 19.1 % / 0.7 % | 11.5 % | correct model class; estimation risk; refit every 50 days |
| size | garch_t6 | fhs | 250 | 1.35 % | 11.1 ± 1.0 | 14.1 ± 1.1 | 2.0 ± 0.4 | 4.9 ± 0.7 | 16.9 ± 1.2 | 4.3 % | 74.0 % / 25.3 % / 0.7 % | 15.7 % | correct model class; estimation risk; refit every 50 days |
| size | garch_t6 | fhs | 1000 | 1.35 % | 15.3 ± 1.1 | 15.2 ± 1.1 | 3.4 ± 0.6 | 9.8 ± 0.9 | 33.6 ± 1.5 | 0.0 % | 77.6 % / 21.1 % / 1.3 % | 15.4 % | correct model class; estimation risk; refit every 50 days |
| size | garch_t6 | garch_t (window 1000) | 1000 | 1.03 % | 5.8 ± 0.7 | 4.3 ± 0.6 | 2.0 ± 0.4 | 2.5 ± 0.5 | 4.9 ± 0.7 | 0.0 % | 87.3 % / 12.7 % / 0.0 % | 5.0 % | correct model class; 1000-day estimation window (isolates estimation risk); refit every 50 days |
| size | garch_t6 | fhs (window 1000) | 1000 | 1.12 % | 5.5 ± 0.7 | 4.9 ± 0.7 | 1.8 ± 0.4 | 2.4 ± 0.5 | 5.7 ± 0.7 | 0.0 % | 83.7 % / 16.3 % / 0.0 % | 7.6 % | correct model class; 1000-day estimation window (isolates estimation risk); refit every 50 days |
| power | iid_t5 | normal | 250 | 1.58 % | 10.8 ± 1.0 | 19.2 ± 1.2 | 1.7 ± 0.4 | 5.4 ± 0.7 | 18.2 ± 1.2 | 1.2 % | 63.0 % / 36.7 % / 0.3 % | 11.8 % | not size-adjusted |
| power | iid_t5 | normal | 1000 | 1.58 % | 40.6 ± 1.6 | 40.6 ± 1.6 | 2.5 ± 0.5 | 24.5 ± 1.4 | 35.6 ± 1.5 | 0.0 % | 64.0 % / 35.8 % / 0.2 % | 11.7 % | not size-adjusted |
| power | garch_normal | normal | 250 | 1.44 % | 27.4 ± 1.4 | 21.0 ± 1.3 | 3.8 ± 0.6 | 13.8 ± 1.1 | 23.9 ± 1.3 | 12.6 % | 67.7 % / 28.2 % / 4.1 % | 19.8 % | not size-adjusted |
| power | garch_normal | normal | 1000 | 1.52 % | 35.6 ± 1.5 | 35.5 ± 1.5 | 16.1 ± 1.2 | 35.4 ± 1.5 | 41.6 ± 1.6 | 0.0 % | 66.4 % / 28.3 % / 5.3 % | 19.8 % | not size-adjusted |
| power | garch_normal | hs | 250 | 1.41 % | 22.7 ± 1.3 | 21.1 ± 1.3 | 4.2 ± 0.6 | 11.4 ± 1.0 | 25.8 ± 1.4 | 10.3 % | 68.4 % / 29.3 % / 2.3 % | 23.4 % | not size-adjusted |
| power | garch_normal | hs | 1000 | 1.45 % | 25.3 ± 1.4 | 25.3 ± 1.4 | 12.8 ± 1.1 | 24.6 ± 1.4 | 45.6 ± 1.6 | 0.0 % | 67.9 % / 29.2 % / 2.9 % | 23.1 % | not size-adjusted |
| power | garch_t6 | normal | 250 | 1.85 % | 27.5 ± 1.4 | 32.4 ± 1.5 | 6.1 ± 0.8 | 20.8 ± 1.3 | 32.2 ± 1.5 | 4.9 % | 55.4 % / 36.9 % / 7.7 % | 23.2 % | not size-adjusted |
| power | garch_t6 | normal | 1000 | 1.87 % | 65.2 ± 1.5 | 65.2 ± 1.5 | 15.7 ± 1.2 | 59.7 ± 1.6 | 64.7 ± 1.5 | 0.0 % | 56.3 % / 34.9 % / 8.8 % | 22.5 % | not size-adjusted |
| power | garch_t6 | hs | 250 | 1.35 % | 19.3 ± 1.2 | 17.4 ± 1.2 | 5.4 ± 0.7 | 9.8 ± 0.9 | 24.5 ± 1.4 | 8.7 % | 72.6 % / 25.7 % / 1.7 % | 30.6 % | not size-adjusted |
| power | garch_t6 | hs | 1000 | 1.38 % | 20.1 ± 1.3 | 19.9 ± 1.3 | 10.8 ± 1.0 | 19.8 ± 1.3 | 37.9 ± 1.5 | 0.0 % | 73.5 % / 24.7 % / 1.8 % | 29.3 % | not size-adjusted |
| power | regime | normal | 250 | 2.27 % | 37.8 ± 1.5 | 48.2 ± 1.6 | 11.5 ± 1.0 | 37.1 ± 1.5 | 43.5 ± 1.6 | 2.2 % | 39.5 % / 46.9 % / 13.6 % | 59.0 % | not size-adjusted |
| power | regime | normal | 1000 | 2.28 % | 92.4 ± 0.8 | 92.4 ± 0.8 | 31.4 ± 1.5 | 89.6 ± 1.0 | 86.8 ± 1.1 | 0.0 % | 39.7 % / 49.3 % / 11.0 % | 59.6 % | not size-adjusted |
| power | regime | hs | 250 | 1.35 % | 14.6 ± 1.1 | 16.5 ± 1.2 | 7.1 ± 0.8 | 10.3 ± 1.0 | 25.0 ± 1.4 | 6.4 % | 73.1 % / 26.1 % / 0.8 % | 82.9 % | not size-adjusted |
| power | regime | hs | 1000 | 1.35 % | 16.1 ± 1.2 | 16.1 ± 1.2 | 16.4 ± 1.2 | 21.6 ± 1.3 | 38.8 ± 1.5 | 0.0 % | 73.6 % / 25.7 % / 0.7 % | 83.7 % | not size-adjusted |
| conditional | garch_t6 | ewma_t | 250 | 1.28 % | 6.3 ± 0.8 | 9.1 ± 0.9 | 2.1 ± 0.5 | 1.9 ± 0.4 | 11.1 ± 1.0 | 3.4 % | 78.5 % / 21.5 % / 0.0 % | 8.1 % | conditional; EWMA lambda 0.94, nu 6 fixed (not the true dynamics) |
| conditional | garch_t6 | ewma_t | 1000 | 1.24 % | 7.6 ± 0.8 | 7.5 ± 0.8 | 1.4 ± 0.4 | 4.1 ± 0.6 | 20.9 ± 1.3 | 0.0 % | 82.6 % / 17.4 % / 0.0 % | 8.1 % | conditional; EWMA lambda 0.94, nu 6 fixed (not the true dynamics) |
| conditional | regime | garch_t | 250 | 1.24 % | 11.6 ± 1.0 | 11.1 ± 1.0 | 1.9 ± 0.4 | 4.9 ± 0.7 | 12.2 ± 1.0 | 5.7 % | 79.2 % / 20.3 % / 0.5 % | 40.9 % | conditional but misspecified (regime data); compare with the static rows |
| conditional | regime | garch_t | 1000 | 1.27 % | 12.8 ± 1.1 | 12.4 ± 1.0 | 4.3 ± 0.6 | 9.5 ± 0.9 | 18.0 ± 1.2 | 0.0 % | 79.4 % / 20.2 % / 0.4 % | 40.8 % | conditional but misspecified (regime data); compare with the static rows |
| conditional | regime | fhs | 250 | 1.36 % | 10.9 ± 1.0 | 14.2 ± 1.1 | 2.2 ± 0.5 | 5.7 ± 0.7 | 17.5 ± 1.2 | 4.6 % | 75.1 % / 23.5 % / 1.4 % | 42.7 % | conditional but misspecified (regime data); compare with the static rows |
| conditional | regime | fhs | 1000 | 1.37 % | 17.4 ± 1.2 | 17.4 ± 1.2 | 5.2 ± 0.7 | 12.2 ± 1.0 | 34.7 ± 1.5 | 0.0 % | 75.7 % / 23.7 % / 0.6 % | 43.2 % | conditional but misspecified (regime data); compare with the static rows |
| conditional | regime | ewma_t | 250 | 1.33 % | 6.3 ± 0.8 | 10.5 ± 1.0 | 5.7 ± 0.7 | 4.3 ± 0.6 | 13.9 ± 1.1 | 3.1 % | 76.8 % / 23.2 % / 0.0 % | 43.9 % | conditional but misspecified (regime data); compare with the static rows |
| conditional | regime | ewma_t | 1000 | 1.37 % | 19.4 ± 1.3 | 19.1 ± 1.2 | 11.5 ± 1.0 | 19.2 ± 1.2 | 35.1 ± 1.5 | 0.0 % | 75.2 % / 24.7 % / 0.1 % | 44.2 % | conditional but misspecified (regime data); compare with the static rows |

## Reading the table

- Power rows are not size-adjusted. Compare each with the reference size of the same T and test: e.g. normal on iid_t5 at T = 250 is rejected by Kupiec in 10.8 ± 1.0 % of the runs, against an exact size of 9.5 %: practically no power (within 2 se of the size).
- HS on iid data is not a clean size row: with k = ceil(n alpha) the true exceedance probability is (n - k + 1) / (n + 1) = 1.20 % at n = 250, not 1 %.
- The regime VaR error is measured against the state-conditional oracle (the true VaR given the realised state of the day), which no model can reach from past data alone.
- "ind no-info": share of runs in which the independence test had no information (no exceedance, or only exceedances, on days 2..T); its statistic is then 0 and it counts as not rejected.
- "mean abs rel VaR error": mean over days of |VaR forecast - true VaR| / true VaR.
- "conditional" rows: GARCH-t, FHS and EWMA-t on data they do not exactly describe (regime) or with fixed, not estimated, dynamics (EWMA-t); compare them with the static rows of the same data and T.

## Static against conditional models

Rejection rates in % ± se. The first line of each block is the reference size: the exact size for Kupiec and the binomial test, the oracle row of the same data and T for cc and Z2. A correct model should be rejected at about the reference rate; anything clearly above it is over-rejection.

**garch_t6, T = 250**

| model | role | exceed rate | Kupiec | binomial exact | cc | ES Z2 |
|---|---|---|---|---|---|---|
| reference size | | 1.00 % (target) | 9.5 % | 4.1 % | 1.0 ± 0.3 | 4.4 ± 0.6 |
| garch_t | size | 1.25 % | 12.0 ± 1.0 | 11.7 ± 1.0 | 4.0 ± 0.6 | 12.2 ± 1.0 |
| fhs | size | 1.35 % | 11.1 ± 1.0 | 14.1 ± 1.1 | 4.9 ± 0.7 | 16.9 ± 1.2 |
| normal | power | 1.85 % | 27.5 ± 1.4 | 32.4 ± 1.5 | 20.8 ± 1.3 | 32.2 ± 1.5 |
| hs | power | 1.35 % | 19.3 ± 1.2 | 17.4 ± 1.2 | 9.8 ± 0.9 | 24.5 ± 1.4 |
| ewma_t | conditional | 1.28 % | 6.3 ± 0.8 | 9.1 ± 0.9 | 1.9 ± 0.4 | 11.1 ± 1.0 |

**garch_t6, T = 1000**

| model | role | exceed rate | Kupiec | binomial exact | cc | ES Z2 |
|---|---|---|---|---|---|---|
| reference size | | 1.00 % (target) | 5.5 % | 3.6 % | 1.7 ± 0.4 | 4.6 ± 0.7 |
| garch_t | size | 1.25 % | 10.6 ± 1.0 | 10.5 ± 1.0 | 6.2 ± 0.8 | 17.7 ± 1.2 |
| fhs | size | 1.35 % | 15.3 ± 1.1 | 15.2 ± 1.1 | 9.8 ± 0.9 | 33.6 ± 1.5 |
| garch_t (window 1000) | size | 1.03 % | 5.8 ± 0.7 | 4.3 ± 0.6 | 2.5 ± 0.5 | 4.9 ± 0.7 |
| fhs (window 1000) | size | 1.12 % | 5.5 ± 0.7 | 4.9 ± 0.7 | 2.4 ± 0.5 | 5.7 ± 0.7 |
| normal | power | 1.87 % | 65.2 ± 1.5 | 65.2 ± 1.5 | 59.7 ± 1.6 | 64.7 ± 1.5 |
| hs | power | 1.38 % | 20.1 ± 1.3 | 19.9 ± 1.3 | 19.8 ± 1.3 | 37.9 ± 1.5 |
| ewma_t | conditional | 1.24 % | 7.6 ± 0.8 | 7.5 ± 0.8 | 4.1 ± 0.6 | 20.9 ± 1.3 |

**regime, T = 250**

| model | role | exceed rate | Kupiec | binomial exact | cc | ES Z2 |
|---|---|---|---|---|---|---|
| reference size | | 1.00 % (target) | 9.5 % | 4.1 % | 1.0 ± 0.3 | 5.0 ± 0.7 |
| normal | power | 2.27 % | 37.8 ± 1.5 | 48.2 ± 1.6 | 37.1 ± 1.5 | 43.5 ± 1.6 |
| hs | power | 1.35 % | 14.6 ± 1.1 | 16.5 ± 1.2 | 10.3 ± 1.0 | 25.0 ± 1.4 |
| garch_t | conditional | 1.24 % | 11.6 ± 1.0 | 11.1 ± 1.0 | 4.9 ± 0.7 | 12.2 ± 1.0 |
| fhs | conditional | 1.36 % | 10.9 ± 1.0 | 14.2 ± 1.1 | 5.7 ± 0.7 | 17.5 ± 1.2 |
| ewma_t | conditional | 1.33 % | 6.3 ± 0.8 | 10.5 ± 1.0 | 4.3 ± 0.6 | 13.9 ± 1.1 |

**regime, T = 1000**

| model | role | exceed rate | Kupiec | binomial exact | cc | ES Z2 |
|---|---|---|---|---|---|---|
| reference size | | 1.00 % (target) | 5.5 % | 3.6 % | 2.6 ± 0.5 | 4.4 ± 0.6 |
| normal | power | 2.28 % | 92.4 ± 0.8 | 92.4 ± 0.8 | 89.6 ± 1.0 | 86.8 ± 1.1 |
| hs | power | 1.35 % | 16.1 ± 1.2 | 16.1 ± 1.2 | 21.6 ± 1.3 | 38.8 ± 1.5 |
| garch_t | conditional | 1.27 % | 12.8 ± 1.1 | 12.4 ± 1.0 | 9.5 ± 0.9 | 18.0 ± 1.2 |
| fhs | conditional | 1.37 % | 17.4 ± 1.2 | 17.4 ± 1.2 | 12.2 ± 1.0 | 34.7 ± 1.5 |
| ewma_t | conditional | 1.37 % | 19.4 ± 1.3 | 19.1 ± 1.2 | 19.2 ± 1.2 | 35.1 ± 1.5 |

Reading (computed from the rows above):

- Conditional models (250-day window) against static normal: rejected less often in 48 of 48 comparisons (model x data x T x test).
- Conditional models (250-day window) against static hs: rejected less often in 44 of 48 comparisons (model x data x T x test).
- Estimation risk, garch_t on garch_t6, T = 1000: exceedance rate 1.25 % with a 250-day window against 1.03 % with 1000 days; Kupiec 10.6 ± 1.0 against 5.8 ± 0.7 (exact size 5.5 %); Z2 17.7 ± 1.2 against 4.9 ± 0.7 (oracle 4.6 %).
- Estimation risk, fhs on garch_t6, T = 1000: exceedance rate 1.35 % with a 250-day window against 1.12 % with 1000 days; Kupiec 15.3 ± 1.1 against 5.5 ± 0.7 (exact size 5.5 %); Z2 33.6 ± 1.5 against 5.7 ± 0.7 (oracle 4.6 %).
- A correctly specified model that is rejected clearly above the reference size with the short window but near it with the long window over-rejects because of estimation error, not because of its form.
