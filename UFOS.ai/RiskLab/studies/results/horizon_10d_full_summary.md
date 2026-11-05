# 10-day 99 % VaR: sqrt(10) scaling against GARCH Monte Carlo and the block bootstrap (synthetic data)

h = 10 days, alpha = 0.99, estimation window = 500 days, M = 1000 replications per data set, 20000 simulated paths per estimate, 50000 paths for the Monte-Carlo truth, seed = 20261106. risklab 0.4.0, numpy 2.5.3, scipy 1.18.1.

Bias and RMSE are relative to the true 10-day VaR of each replication (mean of (estimate - truth) / truth). `± se` is the Monte-Carlo standard error of the bias over the M replications. "est MC se" is the mean internal simulation error of the estimate (binomial order-statistic interval), "truth MC se" the same for the simulated truth (0 for the closed-form iid-normal truth).

| data | method | bias | RMSE | est MC se | truth MC se | mean block length | note |
|---|---|---|---|---|---|---|---|
| iid_normal | sqrt10_normal | -0.0 ± 0.1 % | 3.6 % | n/a | 0.00 % |  |  |
| iid_normal | sqrt10_garch | +0.7 ± 0.1 % | 4.7 % | n/a | 0.00 % |  |  |
| iid_normal | garch_mc | +0.2 ± 0.2 % | 6.9 % | 1.18 % | 0.00 % |  |  |
| iid_normal | bootstrap | -0.8 ± 0.2 % | 7.3 % | 1.15 % | 0.00 % | 1.48 |  |
| iid_t5 | sqrt10_normal | -2.9 ± 0.2 % | 6.8 % | n/a | 0.84 % |  |  |
| iid_t5 | sqrt10_garch | +8.6 ± 0.3 % | 13.8 % | n/a | 0.84 % |  |  |
| iid_t5 | garch_mc | +0.7 ± 0.3 % | 10.2 % | 1.41 % | 0.84 % |  |  |
| iid_t5 | bootstrap | +0.3 ± 0.5 % | 14.3 % | 1.31 % | 0.84 % | 1.50 |  |
| garch_t6 | sqrt10_normal | -0.7 ± 0.9 % | 29.3 % | n/a | 1.01 % |  |  |
| garch_t6 | sqrt10_garch | +0.6 ± 0.3 % | 9.8 % | n/a | 1.01 % |  |  |
| garch_t6 | garch_mc | -0.3 ± 0.4 % | 11.5 % | 1.60 % | 1.01 % |  |  |
| garch_t6 | bootstrap | +3.7 ± 1.2 % | 37.2 % | 1.44 % | 1.01 % | 1.96 |  |

## Reading the table

- What to expect: sqrt(10) scaling is exact only for iid normal losses with zero mean. For fat-tailed iid data the 10-day sum is closer to normal than one day (central limit theorem), so sqrt(10) x a fat-tailed one-day quantile tends to overstate the 10-day VaR; under GARCH, sqrt(10) ignores that volatility mean-reverts over the 10 days (it overstates in high-volatility states and understates in calm ones).
- GARCH Monte Carlo uses the fitted dynamics and innovation distribution; its error is mainly the estimation error of the 500-day fit plus the stated simulation error.
- The block bootstrap needs no model, but a 500-day window holds only about five 1 % tail days, so its 10-day tail is noisy. Its automatic block length is chosen on the loss LEVELS, which are nearly uncorrelated even under GARCH (the dependence is in the squares), so the blocks are short; and it resamples the whole window, so it does not condition on the current volatility.
- Truth: monte carlo (50000 paths, true parameters, common random numbers) for GARCH; closed form for iid normal.
- On GARCH-t6 the bootstrap bias is +3.7 ± 1.2 % (+3.1 se): biased; mean automatic block length 1.96 days.
