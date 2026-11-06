# Reverse DCF and sensitivity analysis (synthetic example)

Synthetic Co. (invented, not a real company): FCF 100, discount rate 9.0 %, terminal growth 2.0 %, 10-year explicit period, net debt 300, other claims 50, 100 shares. M = 2000 cohorts, Sobol N = 65536 (DCF, Ishigami), 1024 (IRRBB), 2048 (ECL), 500 bootstrap resamples, seed 20261112. risklab 0.9.0, numpy 2.5.3. No market or company data.

Reference price 30: implied constant growth 13.14 % a year for 10 years; terminal value = 63.1 % of the enterprise value.

## A. Implied growth vs price

| price | implied constant growth | implied fade start (fade CAGR) | implied discount rate at 5 % growth |
|---|---|---|---|
| 10 | 0.97 % | 0.17 % (1.08 %) | 11.29 % |
| 12.5 | 3.26 % | 4.26 % (3.13 %) | 9.91 % |
| 15 | 5.21 % | 7.81 % (4.89 %) | 8.90 % |
| 17.5 | 6.90 % | 10.95 % (6.44 %) | 8.11 % |
| 20 | 8.41 % | 13.77 % (7.82 %) | 7.49 % |
| 22.5 | 9.75 % | 16.33 % (9.07 %) | 6.99 % |
| 25 | 10.98 % | 18.67 % (10.21 %) | 6.56 % |
| 27.5 | 12.10 % | 20.84 % (11.26 %) | 6.21 % |
| 30 | 13.14 % | 22.86 % (12.23 %) | 5.91 % |
| 32.5 | 14.10 % | 24.74 % (13.14 %) | 5.64 % |
| 35 | 15.00 % | 26.51 % (13.99 %) | 5.41 % |
| 37.5 | 15.85 % | 28.18 % (14.79 %) | 5.21 % |
| 40 | 16.65 % | 29.77 % (15.54 %) | 5.03 % |
| 42.5 | 17.40 % | 31.27 % (16.26 %) | 4.87 % |
| 45 | 18.11 % | 32.70 % (16.94 %) | 4.73 % |
| 47.5 | 18.80 % | 34.07 % (17.59 %) | 4.60 % |
| 50 | 19.44 % | 35.38 % (18.21 %) | 4.48 % |
| 52.5 | 20.06 % | 36.63 % (18.80 %) | 4.37 % |
| 55 | 20.66 % | 37.84 % (19.37 %) | 4.27 % |
| 57.5 | 21.23 % | 39.00 % (19.92 %) | 4.18 % |
| 60 | 21.78 % | 40.12 % (20.44 %) | 4.10 % |

## B. How often is the implied 10-year CAGR achieved? (synthetic universe of the v0.4 study)

| kappa | survival rate | survivors only | full cohort (exit = miss) | counterfactual |
|---|---|---|---|---|
| 0 | 66.5 % | 14.3 ± 0.02 % | 9.5 ± 0.01 % | 14.4 ± 0.02 % |
| 1 | 51.8 % | 20.2 ± 0.03 % | 10.4 ± 0.02 % | 14.3 ± 0.02 % |
| 2 | 23.8 % | 36.7 ± 0.05 % | 8.7 ± 0.01 % | 14.3 ± 0.02 % |

## C/E. Tornado: value per share (dcf)

| input | low - high | output at low | output at high | swing |
|---|---|---|---|---|
| growth | 0.1014 - 0.1614 | 23.26 | 38.39 | 15.13 |
| years | 7 - 13 | 23.56 | 37.2 | 13.63 |
| discount_rate | 0.08 - 0.1 | 36.59 | 25.11 | 11.49 |
| fcf0 | 90 - 110 | 26.65 | 33.35 | 6.7 |
| terminal_growth | 0.01 - 0.03 | 27.17 | 33.77 | 6.593 |
| net_debt | 150 - 450 | 31.5 | 28.5 | 3 |

## C/E. Tornado: implied growth at the reference price (dcf)

| input | low - high | output at low | output at high | swing |
|---|---|---|---|---|
| years | 7 - 13 | 0.1711 | 0.1108 | 0.06029 |
| discount_rate | 0.08 - 0.1 | 0.108 | 0.1531 | 0.04507 |
| fcf0 | 90 - 110 | 0.1455 | 0.1187 | 0.02686 |
| terminal_growth | 0.01 - 0.03 | 0.1434 | 0.1174 | 0.02599 |
| net_debt | 150 - 450 | 0.1253 | 0.1373 | 0.01199 |

## C/E. Tornado: worst Delta EVE / Tier 1 (irrbb)

| input | low - high | output at low | output at high | swing |
|---|---|---|---|---|
| ca_avg_maturity | 1 - 5 | 0.2908 | 0.08416 | 0.2066 |
| ca_core_share | 0.5 - 0.9 | 0.2287 | 0.1232 | 0.1055 |
| sav_avg_maturity | 1 - 4.5 | 0.1906 | 0.1451 | 0.0455 |
| sav_core_share | 0.3 - 0.7 | 0.1885 | 0.1634 | 0.02509 |

## C/E. Tornado: ECL coverage (ecl)

| input | low - high | output at low | output at high | swing |
|---|---|---|---|---|
| lgd_scale | 0.8 - 1.2 | 0.007477 | 0.01122 | 0.003739 |
| pd_scale | 0.8 - 1.2 | 0.007681 | 0.01121 | 0.00353 |
| sicr_multiple | 2 - 4 | 0.01115 | 0.009201 | 0.001945 |
| downside_weight | 0.1 - 0.5 | 0.008427 | 0.01027 | 0.00184 |
| downside_severity | 0.5 - 1.5 | 0.008658 | 0.01003 | 0.001376 |

## D/E. Sobol indices: dcf / value_per_share (N = 65536, 524288 runs)

| input | first order ± se | total order ± se | analytic first / total |
|---|---|---|---|
| discount_rate | 0.1763 ± 0.0022 | 0.1898 ± 0.0012 | - |
| terminal_growth | 0.0588 ± 0.0014 | 0.0631 ± 0.0004 | - |
| growth | 0.3108 ± 0.0031 | 0.3489 ± 0.0025 | - |
| fcf0 | 0.0576 ± 0.0013 | 0.0619 ± 0.0004 | - |
| net_debt | 0.0115 ± 0.0006 | 0.0113 ± 0.0001 | - |
| years | 0.3395 ± 0.0033 | 0.3766 ± 0.0026 | - |

## D/E. Sobol indices: ishigami / y (N = 65536, 327680 runs)

| input | first order ± se | total order ± se | analytic first / total |
|---|---|---|---|
| x1 | 0.3137 ± 0.0039 | 0.5602 ± 0.0054 | 0.3139 / 0.5576 |
| x2 | 0.4403 ± 0.0034 | 0.4422 ± 0.0027 | 0.4424 / 0.4424 |
| x3 | 0.0028 ± 0.0034 | 0.2456 ± 0.0018 | 0.0000 / 0.2437 |

## D/E. Sobol indices: irrbb / worst_eve_tier1 (N = 1024, 6144 runs)

| input | first order ± se | total order ± se | analytic first / total |
|---|---|---|---|
| ca_core_share | 0.1883 ± 0.0196 | 0.1993 ± 0.0109 | - |
| ca_avg_maturity | 0.7696 ± 0.0342 | 0.7942 ± 0.0314 | - |
| sav_core_share | 0.0295 ± 0.0065 | 0.0207 ± 0.0012 | - |
| sav_avg_maturity | 0.0359 ± 0.0082 | 0.0353 ± 0.0018 | - |

## D/E. Sobol indices: ecl / coverage (N = 2048, 14336 runs)

| input | first order ± se | total order ± se | analytic first / total |
|---|---|---|---|
| downside_weight | 0.0922 ± 0.0087 | 0.0914 ± 0.0038 | - |
| downside_severity | 0.0508 ± 0.0073 | 0.0541 ± 0.0023 | - |
| sicr_multiple | 0.0813 ± 0.0108 | 0.1105 ± 0.0053 | - |
| lgd_scale | 0.3278 ± 0.0172 | 0.3550 ± 0.0120 | - |
| pd_scale | 0.4010 ± 0.0196 | 0.4546 ± 0.0166 | - |
