# VaR backtest size and power (synthetic data)

alpha = 0.99, estimation window = 250 days, tests at 5 %, M = 1000 replications per row, seed = 20261105, Student-t refit every 50 days (other models daily). risklab 0.2.0, numpy 2.5.3, scipy 1.18.1.

**Monte-Carlo error.** Every rate is shown as `rate ± se` with se = sqrt(r (1 - r) / M); at M = 1000 a rate near 5 % has se ≈ ±0.7 percentage points.

## Reference size under H0

| T | Kupiec exact size | Kupiec rejects x in | binomial exact size | binomial rejects x in |
|---|---|---|---|---|
| 250 | 9.48 % | {0} ∪ {7..250} | 4.12 % | {6..250} |
| 1000 | 5.51 % | {0..4} ∪ {17..1000} | 3.65 % | {0..3} ∪ {17..1000} |

Kupiec is asymptotic (chi-square). At T = 250 it rejects x = 0: LR = -2 T ln(1 - p) = 5.03 > 3.84, p = 0.025, and P(X = 0) = 0.081 under H0, so its exact size is far above 5 %. The exact binomial test keeps the size at or below 5 % by construction (it cannot reject x = 0 at T = 250: P(X = 0) alone exceeds 5 %).
For ind and cc there is no closed form: their reference is the empirical rate of the oracle row of the same data and T (`ref_size_ind`, `ref_size_cc` in the CSV).

## Rejection rates (%, ± Monte-Carlo se)

| role | data | model | T | exceed rate | Kupiec | binomial exact | ind | cc | ind no-info | TL green / yellow / red | mean abs rel VaR error | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| size | iid_normal | oracle | 250 | 1.04 % | 8.7 ± 0.9 | 5.3 ± 0.7 | 0.9 ± 0.3 | 1.1 ± 0.3 | 6.5 % | 88.0 % / 12.0 % / 0.0 % | 0.0 % | true VaR |
| size | iid_normal | oracle | 1000 | 1.01 % | 5.7 ± 0.7 | 4.1 ± 0.6 | 1.9 ± 0.4 | 2.7 ± 0.5 | 0.1 % | 88.7 % / 11.3 % / 0.0 % | 0.0 % | true VaR |
| size | iid_t5 | oracle | 250 | 0.98 % | 9.0 ± 0.9 | 4.0 ± 0.6 | 1.0 ± 0.3 | 1.0 ± 0.3 | 8.0 % | 89.7 % / 10.3 % / 0.0 % | 0.0 % | true VaR |
| size | iid_t5 | oracle | 1000 | 1.00 % | 7.4 ± 0.8 | 5.0 ± 0.7 | 2.4 ± 0.5 | 4.1 ± 0.6 | 0.0 % | 89.4 % / 10.5 % / 0.1 % | 0.0 % | true VaR |
| size | garch_normal | oracle | 250 | 0.99 % | 10.6 ± 1.0 | 4.5 ± 0.7 | 1.2 ± 0.3 | 1.0 ± 0.3 | 9.2 % | 89.0 % / 11.0 % / 0.0 % | 0.0 % | true VaR |
| size | garch_normal | oracle | 1000 | 1.01 % | 6.2 ± 0.8 | 4.6 ± 0.7 | 2.6 ± 0.5 | 2.8 ± 0.5 | 0.0 % | 88.0 % / 12.0 % / 0.0 % | 0.0 % | true VaR |
| size | regime | oracle | 250 | 0.99 % | 8.9 ± 0.9 | 4.1 ± 0.6 | 1.2 ± 0.3 | 1.4 ± 0.4 | 7.3 % | 90.0 % / 9.7 % / 0.3 % | 0.0 % | truth conditional on the realised state (oracle) |
| size | regime | oracle | 1000 | 1.01 % | 4.8 ± 0.7 | 3.3 ± 0.6 | 1.6 ± 0.4 | 2.7 ± 0.5 | 0.0 % | 86.9 % / 13.0 % / 0.1 % | 0.0 % | truth conditional on the realised state (oracle) |
| size | iid_normal | normal | 250 | 1.08 % | 7.7 ± 0.8 | 4.5 ± 0.7 | 1.1 ± 0.3 | 0.7 ± 0.3 | 6.2 % | 88.1 % / 11.9 % / 0.0 % | 4.3 % | estimation risk |
| size | iid_normal | normal | 1000 | 1.06 % | 2.9 ± 0.5 | 2.5 ± 0.5 | 0.8 ± 0.3 | 1.3 ± 0.4 | 0.0 % | 86.4 % / 13.6 % / 0.0 % | 4.2 % | estimation risk |
| size | iid_normal | hs | 250 | 1.21 % | 4.6 ± 0.7 | 5.9 ± 0.7 | 1.9 ± 0.4 | 1.3 ± 0.4 | 3.1 % | 84.9 % / 15.1 % / 0.0 % | 7.9 % | not a clean size row: HS exceedance probability (n-k+1)/(n+1) = 1.20 % |
| size | iid_normal | hs | 1000 | 1.21 % | 1.9 ± 0.4 | 1.9 ± 0.4 | 1.1 ± 0.3 | 1.3 ± 0.4 | 0.0 % | 83.6 % / 16.3 % / 0.1 % | 8.0 % | not a clean size row: HS exceedance probability (n-k+1)/(n+1) = 1.20 % |
| size | iid_t5 | student_t | 250 | 1.12 % | 7.4 ± 0.8 | 5.8 ± 0.7 | 1.6 ± 0.4 | 1.5 ± 0.4 | 5.2 % | 84.5 % / 15.5 % / 0.0 % | 8.2 % | estimation risk; t refit every 50 days |
| size | iid_t5 | student_t | 1000 | 1.09 % | 4.5 ± 0.7 | 3.3 ± 0.6 | 2.5 ± 0.5 | 3.3 ± 0.6 | 0.0 % | 86.6 % / 13.4 % / 0.0 % | 8.1 % | estimation risk; t refit every 50 days |
| power | iid_t5 | normal | 250 | 1.58 % | 10.8 ± 1.0 | 19.2 ± 1.2 | 1.7 ± 0.4 | 5.4 ± 0.7 | 1.2 % | 63.0 % / 36.7 % / 0.3 % | 11.8 % | not size-adjusted |
| power | iid_t5 | normal | 1000 | 1.58 % | 40.6 ± 1.6 | 40.6 ± 1.6 | 2.5 ± 0.5 | 24.5 ± 1.4 | 0.0 % | 64.0 % / 35.8 % / 0.2 % | 11.7 % | not size-adjusted |
| power | garch_normal | normal | 250 | 1.44 % | 27.4 ± 1.4 | 21.0 ± 1.3 | 3.8 ± 0.6 | 13.8 ± 1.1 | 12.6 % | 67.7 % / 28.2 % / 4.1 % | 19.8 % | not size-adjusted |
| power | garch_normal | normal | 1000 | 1.52 % | 35.6 ± 1.5 | 35.5 ± 1.5 | 16.1 ± 1.2 | 35.4 ± 1.5 | 0.0 % | 66.4 % / 28.3 % / 5.3 % | 19.8 % | not size-adjusted |
| power | garch_normal | hs | 250 | 1.41 % | 22.7 ± 1.3 | 21.1 ± 1.3 | 4.2 ± 0.6 | 11.4 ± 1.0 | 10.3 % | 68.4 % / 29.3 % / 2.3 % | 23.4 % | not size-adjusted |
| power | garch_normal | hs | 1000 | 1.45 % | 25.3 ± 1.4 | 25.3 ± 1.4 | 12.8 ± 1.1 | 24.6 ± 1.4 | 0.0 % | 67.9 % / 29.2 % / 2.9 % | 23.1 % | not size-adjusted |
| power | regime | normal | 250 | 2.27 % | 39.4 ± 1.5 | 48.2 ± 1.6 | 10.1 ± 1.0 | 36.7 ± 1.5 | 3.1 % | 40.2 % / 46.7 % / 13.1 % | 58.9 % | not size-adjusted |
| power | regime | normal | 1000 | 2.32 % | 92.3 ± 0.8 | 92.3 ± 0.8 | 33.0 ± 1.5 | 90.5 ± 0.9 | 0.0 % | 36.1 % / 50.0 % / 13.9 % | 59.8 % | not size-adjusted |
| power | regime | hs | 250 | 1.37 % | 16.9 ± 1.2 | 15.6 ± 1.1 | 7.4 ± 0.8 | 9.9 ± 0.9 | 7.3 % | 71.4 % / 28.0 % / 0.6 % | 83.6 % | not size-adjusted |
| power | regime | hs | 1000 | 1.40 % | 20.6 ± 1.3 | 20.6 ± 1.3 | 19.3 ± 1.2 | 25.2 ± 1.4 | 0.0 % | 68.0 % / 30.5 % / 1.5 % | 83.4 % | not size-adjusted |

## Reading the table

- Power rows are not size-adjusted. Compare each with the reference size of the same T and test: e.g. normal on iid_t5 at T = 250 is rejected by Kupiec in 10.8 ± 1.0 % of the runs, against an exact size of 9.5 %: practically no power (within 2 se of the size).
- HS on iid data is not a clean size row: with k = ceil(n alpha) the true exceedance probability is (n - k + 1) / (n + 1) = 1.20 % at n = 250, not 1 %.
- The regime VaR error is measured against the state-conditional oracle (the true VaR given the realised state of the day), which no model can reach from past data alone.
- "ind no-info": share of runs in which the independence test had no information (no exceedance, or only exceedances, on days 2..T); its statistic is then 0 and it counts as not rejected.
- "mean abs rel VaR error": mean over days of |VaR forecast - true VaR| / true VaR.
