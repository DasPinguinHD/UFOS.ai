# IRRBB scenarios of a synthetic bank

Reference bank: assets 1000, liabilities 900, Tier 1 90 (EUR m, synthetic). Base curve: Nelson-Siegel {'b0': 0.03, 'b1': -0.015, 'b2': 0.01, 'tau': 2.0, 'b3': 0.0, 'tau2': 1.0}. Population: M = 2000 banks, seed 20261110. risklab 0.7.0, numpy 2.5.3. No market data.

## Delta EVE and Delta NII (% of Tier 1)

| preset | scenario | Delta EVE | Delta NII |
|---|---|---|---|
| EUR_BCBS2016 | parallel_up | -17.6 | 3.1 |
| EUR_BCBS2016 | parallel_down | 21.5 | -6.4 |
| EUR_BCBS2016 | steepener | -8.0 | -4.0 |
| EUR_BCBS2016 | flattener | 5.3 | 2.4 |
| EUR_BCBS2016 | short_up | -0.5 | 3.2 |
| EUR_BCBS2016 | short_down | 0.5 | -7.4 |
| EUR_BCBS2024 | parallel_up | -19.6 | 3.4 |
| EUR_BCBS2024 | parallel_down | 24.5 | -7.4 |
| EUR_BCBS2024 | steepener | -15.8 | -5.9 |
| EUR_BCBS2024 | flattener | 11.1 | 3.3 |
| EUR_BCBS2024 | short_up | -0.7 | 4.5 |
| EUR_BCBS2024 | short_down | 0.6 | -10.0 |

## Supervisory outlier tests

| preset | worst EVE scenario | EVE decline / Tier 1 | outlier (> 15 %) | worst NII (parallel) | NII decline / Tier 1 | outlier (> 5 %) |
|---|---|---|---|---|---|---|
| EUR_BCBS2016 | parallel_up | 17.6 | True | parallel_down | 6.4 | True |
| EUR_BCBS2024 | parallel_up | 19.6 | True | parallel_down | 7.4 | True |

## NMD sensitivity (worst Delta EVE / Tier 1, %, EUR_BCBS2016)

| core share \ avg maturity | 1 y | 2 y | 3 y | 4 y | 5 y |
|---|---|---|---|---|---|
| 50% | -31.1 | -26.7 | -22.9 | -19.4 | -16.3 |
| 60% | -30.1 | -24.9 | -20.2 | -16.1 | -12.4 |
| 70% | -29.1 | -23.0 | -17.6 | -12.7 | -8.4 |
| 80% | -28.1 | -21.2 | -15.0 | -9.4 | -4.5 |
| 90% | -27.1 | -19.3 | -12.3 | -6.1 | -1.5 |

## Duration check (net EVE, parallel shifts, no floor, EUR m)

| shift (bp) | full revaluation | first order | second order |
|---|---|---|---|
| -400 | 42.96 | 34.98 | 42.01 |
| -300 | 30.58 | 26.23 | 30.19 |
| -200 | 19.36 | 17.49 | 19.25 |
| -100 | 9.20 | 8.74 | 9.18 |
| -50 | 4.48 | 4.37 | 4.48 |
| +50 | -4.26 | -4.37 | -4.26 |
| +100 | -8.32 | -8.74 | -8.31 |
| +200 | -15.84 | -17.49 | -15.73 |
| +300 | -22.62 | -26.23 | -22.28 |
| +400 | -28.74 | -34.98 | -27.95 |

## Population of 2000 synthetic banks (worst Delta EVE / Tier 1, %)

| preset | mean ± se | 5 % | median | 95 % | EVE outliers | NII outliers |
|---|---|---|---|---|---|---|
| EUR_BCBS2016 | -17.6 ± 0.14 | -27.6 | -17.7 | -7.6 | 65.6 ± 1.1 % | 96.7 ± 0.4 % |
| EUR_BCBS2024 | -20.3 ± 0.13 | -30.7 | -19.8 | -11.8 | 77.1 ± 0.9 % | 100.0 ± 0.0 % |

Assumptions (sources or ASSUMPTION): see `meta.assumptions` in the results file.
