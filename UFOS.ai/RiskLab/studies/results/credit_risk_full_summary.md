# Credit risk study (synthetic data)

M = 200 replications, 2000000 loss draws, seed 20261111. True PD model and asset correlation 0.12 known. risklab 0.8.0, numpy 2.5.3. No real borrowers or data.

## A. PD model quality vs sample size (out of time)

Failed fits are excluded: every average is conditional on a successful fit. Hosmer-Lemeshow out of time uses chi-square with g = 10 degrees of freedom. Macro coefficient: mean and spread (sd) of the estimate over the successful fits against the mean MLE standard error.

| N train | failed fits | AUC | AUC gap to true PD | coef RMSE | Brier | HL rejects (5 %) | macro coef (true -0.45): mean / sd / mean MLE se |
|---|---|---|---|---|---|---|---|
| 500 | 74 | 0.675 ± 0.007 | +0.072 ± 0.007 | 1.594 ± 0.192 | 0.0404 | 96.8 ± 1.6 % | -0.560 / 2.678 / 1.074 |
| 2000 | 4 | 0.710 ± 0.005 | +0.036 ± 0.004 | 0.788 ± 0.039 | 0.0226 | 94.4 ± 1.6 % | -0.416 / 1.478 / 0.499 |
| 10000 | 0 | 0.714 ± 0.005 | +0.029 ± 0.005 | 0.547 ± 0.037 | 0.0244 | 93.0 ± 1.8 % | -0.562 / 1.252 / 0.229 |
| 50000 | 0 | 0.730 ± 0.004 | +0.018 ± 0.004 | 0.421 ± 0.023 | 0.0171 | 91.0 ± 2.0 % | -0.507 / 1.026 / 0.087 |

## B. Calibration tests on the TRUE PD, one period (rejection rate at 5 %)

The two per-grade tests reject if ANY grade with >= 50 borrowers has p < 5 % (about 8 grades: multiple testing, so even with independent defaults the rate is well above 5 %).

| asset correlation | test | rejects |
|---|---|---|
| 0.12 | hosmer_lemeshow | 82.5 ± 2.7 % |
| 0.12 | binomial_any_grade | 36.0 ± 3.4 % |
| 0.12 | jeffreys_any_grade | 38.5 ± 3.4 % |
| 0 | hosmer_lemeshow | 5.5 ± 1.6 % |
| 0 | binomial_any_grade | 27.5 ± 3.2 % |
| 0 | jeffreys_any_grade | 34.5 ± 3.4 % |

## B. Downturn period (macro = -2 sd)

| model | mean predicted PD | median predicted PD | realised default rate |
|---|---|---|---|
| full | 5.46 ± 0.67 % | 3.22 % | 3.65 ± 0.20 % |
| ttc | 1.65 ± 0.05 % | 1.54 % | 3.65 ± 0.20 % |
| true_pd | 3.55 ± 0.00 % | 3.55 % | 3.65 ± 0.20 % |

## C. IFRS 9 ECL coverage (% of EAD) by downside weight (rows) and SICR multiple (columns)

| downside weight | x1.5 | x2 | x2.5 | x3 | x4 | x5 |
|---|---|---|---|---|---|---|
| 0 | 1.52 (30 % st. 2) | 1.29 (16 % st. 2) | 1.17 (9 % st. 2) | 1.09 (6 % st. 2) | 1.02 (3 % st. 2) | 0.97 (2 % st. 2) |
| 0.1 | 1.58 (30 % st. 2) | 1.34 (16 % st. 2) | 1.22 (9 % st. 2) | 1.13 (6 % st. 2) | 1.07 (3 % st. 2) | 1.02 (2 % st. 2) |
| 0.2 | 1.63 (30 % st. 2) | 1.40 (16 % st. 2) | 1.27 (9 % st. 2) | 1.18 (6 % st. 2) | 1.12 (3 % st. 2) | 1.07 (2 % st. 2) |
| 0.3 | 1.68 (30 % st. 2) | 1.45 (16 % st. 2) | 1.32 (9 % st. 2) | 1.23 (6 % st. 2) | 1.17 (3 % st. 2) | 1.12 (2 % st. 2) |
| 0.4 | 1.74 (30 % st. 2) | 1.50 (16 % st. 2) | 1.36 (9 % st. 2) | 1.28 (6 % st. 2) | 1.21 (3 % st. 2) | 1.16 (2 % st. 2) |
| 0.5 | 1.79 (30 % st. 2) | 1.55 (16 % st. 2) | 1.41 (9 % st. 2) | 1.33 (6 % st. 2) | 1.26 (3 % st. 2) | 1.21 (2 % st. 2) |
| 0.6 | 1.84 (30 % st. 2) | 1.60 (16 % st. 2) | 1.46 (9 % st. 2) | 1.37 (6 % st. 2) | 1.31 (3 % st. 2) | 1.26 (2 % st. 2) |

## D. IRB capital vs simulated 99.9 % loss (M = 1 year)

| portfolio | names | sum K | EL | q99.9 (95 % CI) | q99.9 - EL | ratio to sum K |
|---|---|---|---|---|---|---|
| granular | 10000 | 630.6 | 81.5 | 707.9 (702.5-713.7) | 626.4 | 0.993 |
| concentrated | 30 | 630.6 | 81.5 | 1147.5 (1147.5-1147.5) | 1066.0 | 1.691 |
| homogeneous_10 | 10 | 586.2 | 45.0 | 1350.0 (1350.0-1350.0) | 1305.0 | 2.226 |
| homogeneous_30 | 30 | 586.2 | 45.0 | 900.0 (900.0-900.0) | 855.0 | 1.458 |
| homogeneous_100 | 100 | 586.2 | 45.0 | 720.0 (720.0-720.0) | 675.0 | 1.151 |
| homogeneous_300 | 300 | 586.2 | 45.0 | 660.0 (645.0-660.0) | 615.0 | 1.049 |
| homogeneous_1000 | 1000 | 586.2 | 45.0 | 639.0 (634.5-643.5) | 594.0 | 1.013 |
| homogeneous_10000 | 10000 | 586.2 | 45.0 | 629.6 (623.7-634.5) | 584.6 | 0.997 |
