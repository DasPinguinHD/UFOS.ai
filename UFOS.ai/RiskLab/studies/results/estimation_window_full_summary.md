# Estimation window: over-rejection of a correct GARCH-t / FHS against the window length (synthetic)

GARCH(1,1)-t6 data, T = 1000 forecast days (the same days for every window), VaR at 0.99, ES (Z2) at 0.975, tests at 5 %, refit every 50 days, M = 1000 replications, seed = 20261107. risklab 0.5.0, numpy 2.5.3, scipy 1.18.1.

Rejection rates in % ± se (se = sqrt(r (1 - r) / M)); the exceedance rate ± its se over replications. Reference sizes: Kupiec 5.51 % and binomial 3.65 % (exact), cc 2.0 ± 0.4 and Z2 5.0 ± 0.7 (oracle row).

| model | window | exceed rate | paired diff vs oracle (pp) | Kupiec | binomial | cc | Z2 | mean abs rel VaR error | note |
|---|---|---|---|---|---|---|---|---|---|
| oracle | true VaR | 1.01 ± 0.01 % | -- | 4.7 ± 0.7 | 3.4 ± 0.6 | 2.0 ± 0.4 | 5.0 ± 0.7 | 0.0 % |  |
| garch_t | 250 | 1.27 ± 0.01 % | +0.259 ± 0.010 | 11.8 ± 1.0 | 11.6 ± 1.0 | 6.7 ± 0.8 | 19.5 ± 1.3 | 11.6 % |  |
| garch_t | 500 | 1.11 ± 0.01 % | +0.096 ± 0.008 | 4.5 ± 0.7 | 4.2 ± 0.6 | 2.4 ± 0.5 | 7.4 ± 0.8 | 7.5 % |  |
| garch_t | 750 | 1.06 ± 0.01 % | +0.052 ± 0.007 | 3.6 ± 0.6 | 2.7 ± 0.5 | 2.2 ± 0.5 | 5.6 ± 0.7 | 6.0 % |  |
| garch_t | 1000 | 1.05 ± 0.01 % | +0.041 ± 0.007 | 3.7 ± 0.6 | 2.9 ± 0.5 | 2.4 ± 0.5 | 6.0 ± 0.8 | 5.1 % |  |
| fhs | 250 | 1.37 ± 0.01 % | +0.355 ± 0.013 | 15.9 ± 1.2 | 15.9 ± 1.2 | 9.8 ± 0.9 | 36.0 ± 1.5 | 15.5 % |  |
| fhs | 500 | 1.25 ± 0.01 % | +0.235 ± 0.011 | 6.7 ± 0.8 | 6.7 ± 0.8 | 5.1 ± 0.7 | 8.3 ± 0.9 | 10.5 % |  |
| fhs | 750 | 1.09 ± 0.01 % | +0.080 ± 0.010 | 2.7 ± 0.5 | 2.4 ± 0.5 | 1.6 ± 0.4 | 6.3 ± 0.8 | 8.7 % |  |
| fhs | 1000 | 1.13 ± 0.01 % | +0.115 ± 0.010 | 4.4 ± 0.6 | 4.3 ± 0.6 | 2.9 ± 0.5 | 6.3 ± 0.8 | 7.3 % |  |

Reading: a correctly specified model should be rejected at about the reference size; the distance above it at short windows is estimation risk, and it shrinks as the window grows. The rejection rates can return to the reference size while the VaR is still slightly anti-conservative: the paired difference of the exceedance rate against the oracle (same data, same days) measures that directly; it is significant where it exceeds about 2 se.
