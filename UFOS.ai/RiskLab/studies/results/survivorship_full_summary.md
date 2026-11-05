# Survivorship bias in 10-year growth base rates (synthetic universe)

N = 2000 firms, 10 years, log growth mu_i + sigma eps with mu_i ~ N(0.05, 0.05), sigma = 0.15; yearly exit probability min(0.9, h0 exp(-kappa z)), h0 = 0.04; M = 2000 cohorts per setting, seed = 20261108. risklab 0.4.0, numpy 2.5.3. No real firms or data.

Base rate = share of firms with a 10-year CAGR >= the threshold, in %. Three rates, three questions:

- naive (survivors only): what share of today's listed firms grew this fast?
- counterfactual (all firms, had they survived): what share of firms has the growth driver for this?
- full cohort (exited = miss): how likely is a firm picked at the start to achieve it as a going concern?

Decomposition: total bias = naive - full cohort = selection (naive - counterfactual) + attrition (counterfactual - full cohort), in percentage points. Acquisitions are treated like failures (a modelling choice). The ± se is Monte-Carlo noise only; the parameter choices dominate the size of the bias.

| h0 | kappa | survival rate | CAGR threshold | naive | counterfactual | full cohort | total bias (pp) | selection (pp) | attrition (pp) |
|---|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 100.0 % | 0 % | 76.6 % | 76.6 % | 76.6 % | 0.0 ± 0.00 | 0.0 ± 0.00 | 0.0 ± 0.00 |
| 0 | 0 | 100.0 % | 5 % | 50.7 % | 50.7 % | 50.7 % | 0.0 ± 0.00 | 0.0 ± 0.00 | 0.0 ± 0.00 |
| 0 | 0 | 100.0 % | 10 % | 25.6 % | 25.6 % | 25.6 % | 0.0 ± 0.00 | 0.0 ± 0.00 | 0.0 ± 0.00 |
| 0 | 0 | 100.0 % | 15 % | 9.6 % | 9.6 % | 9.6 % | 0.0 ± 0.00 | 0.0 ± 0.00 | 0.0 ± 0.00 |
| 0 | 0 | 100.0 % | 20 % | 2.7 % | 2.7 % | 2.7 % | 0.0 ± 0.00 | 0.0 ± 0.00 | 0.0 ± 0.00 |
| 0.04 | 0 | 66.5 % | 0 % | 76.6 % | 76.6 % | 50.9 % | 25.7 ± 0.02 | 0.0 ± 0.01 | 25.7 ± 0.02 |
| 0.04 | 0 | 66.5 % | 5 % | 50.7 % | 50.7 % | 33.7 % | 17.0 ± 0.02 | 0.0 ± 0.02 | 17.0 ± 0.02 |
| 0.04 | 0 | 66.5 % | 10 % | 25.6 % | 25.6 % | 17.0 % | 8.6 ± 0.01 | 0.0 ± 0.02 | 8.6 ± 0.01 |
| 0.04 | 0 | 66.5 % | 15 % | 9.7 % | 9.7 % | 6.4 % | 3.2 ± 0.01 | 0.0 ± 0.01 | 3.2 ± 0.01 |
| 0.04 | 0 | 66.5 % | 20 % | 2.7 % | 2.7 % | 1.8 % | 0.9 ± 0.00 | 0.0 ± 0.01 | 0.9 ± 0.00 |
| 0.04 | 0.5 | 63.0 % | 0 % | 79.9 % | 76.6 % | 50.3 % | 29.5 ± 0.02 | 3.3 ± 0.02 | 26.3 ± 0.02 |
| 0.04 | 0.5 | 63.0 % | 5 % | 54.8 % | 50.7 % | 34.5 % | 20.2 ± 0.02 | 4.1 ± 0.02 | 16.2 ± 0.02 |
| 0.04 | 0.5 | 63.0 % | 10 % | 28.7 % | 25.5 % | 18.1 % | 10.6 ± 0.01 | 3.2 ± 0.02 | 7.4 ± 0.01 |
| 0.04 | 0.5 | 63.0 % | 15 % | 11.3 % | 9.6 % | 7.1 % | 4.2 ± 0.01 | 1.6 ± 0.01 | 2.5 ± 0.01 |
| 0.04 | 0.5 | 63.0 % | 20 % | 3.3 % | 2.7 % | 2.1 % | 1.2 ± 0.00 | 0.6 ± 0.01 | 0.7 ± 0.00 |
| 0.04 | 1 | 51.8 % | 0 % | 85.9 % | 76.6 % | 44.5 % | 41.4 ± 0.02 | 9.3 ± 0.02 | 32.1 ± 0.02 |
| 0.04 | 1 | 51.8 % | 5 % | 62.3 % | 50.7 % | 32.3 % | 30.0 ± 0.02 | 11.6 ± 0.02 | 18.4 ± 0.02 |
| 0.04 | 1 | 51.8 % | 10 % | 34.3 % | 25.5 % | 17.8 % | 16.5 ± 0.02 | 8.8 ± 0.02 | 7.8 ± 0.01 |
| 0.04 | 1 | 51.8 % | 15 % | 14.0 % | 9.6 % | 7.2 % | 6.7 ± 0.01 | 4.3 ± 0.01 | 2.4 ± 0.01 |
| 0.04 | 1 | 51.8 % | 20 % | 4.2 % | 2.7 % | 2.2 % | 2.0 ± 0.01 | 1.5 ± 0.01 | 0.5 ± 0.00 |
| 0.04 | 1.5 | 35.9 % | 0 % | 93.5 % | 76.6 % | 33.6 % | 59.9 ± 0.03 | 17.0 ± 0.02 | 43.0 ± 0.02 |
| 0.04 | 1.5 | 35.9 % | 5 % | 74.4 % | 50.7 % | 26.7 % | 47.7 ± 0.03 | 23.7 ± 0.03 | 24.0 ± 0.02 |
| 0.04 | 1.5 | 35.9 % | 10 % | 44.6 % | 25.6 % | 16.0 % | 28.6 ± 0.03 | 19.1 ± 0.03 | 9.5 ± 0.01 |
| 0.04 | 1.5 | 35.9 % | 15 % | 19.3 % | 9.6 % | 6.9 % | 12.4 ± 0.02 | 9.7 ± 0.02 | 2.7 ± 0.01 |
| 0.04 | 1.5 | 35.9 % | 20 % | 6.1 % | 2.7 % | 2.2 % | 3.9 ± 0.01 | 3.3 ± 0.01 | 0.6 ± 0.00 |
| 0.04 | 2 | 23.8 % | 0 % | 97.8 % | 76.6 % | 23.3 % | 74.5 ± 0.02 | 21.2 ± 0.02 | 53.3 ± 0.03 |
| 0.04 | 2 | 23.8 % | 5 % | 85.0 % | 50.6 % | 20.2 % | 64.7 ± 0.03 | 34.3 ± 0.04 | 30.4 ± 0.02 |
| 0.04 | 2 | 23.8 % | 10 % | 56.4 % | 25.5 % | 13.4 % | 43.0 ± 0.04 | 30.9 ± 0.04 | 12.1 ± 0.02 |
| 0.04 | 2 | 23.8 % | 15 % | 26.5 % | 9.6 % | 6.3 % | 20.2 ± 0.03 | 16.9 ± 0.04 | 3.3 ± 0.01 |
| 0.04 | 2 | 23.8 % | 20 % | 8.8 % | 2.7 % | 2.1 % | 6.7 ± 0.02 | 6.0 ± 0.02 | 0.7 ± 0.00 |

Reading: with zero hazard all three rates coincide. With exits unrelated to growth (kappa = 0) the survivors are a random sample: the selection bias is 0 up to noise, and the naive rate overstates only the full-cohort rate (attrition, dead = miss). With kappa > 0 the exits remove exactly the slow growers, and the naive rate also overstates the counterfactual rate (selection).
