"""Risk Lab: a bank-style market-risk lab (VaR / ES estimation and backtesting).

v0.3: historical simulation, normal, Student-t, EWMA, GARCH(1,1)-t and
filtered-HS VaR / ES; h-day VaR / ES (square-root-of-time, GARCH Monte Carlo,
stationary block bootstrap); the true 1-day and h-day VaR / ES of the synthetic
generators; Kupiec, exact binomial, Christoffersen, the Basel traffic light and
the Acerbi-Szekely ES test; a walk-forward engine (stateful models, h-day
blocks) and a deterministic results export.

v0.4: the validation report pipeline - figures (optional extra ``report``,
matplotlib), LaTeX tables and a report skeleton (``report/``).

v0.5: ``risklab.validation`` - an independent re-implementation of the UFOS
strategy-platform validation gates (DSR, PBO / CSCV, Newey-West t, walk-forward
folds, the parameter grid and its neighbours, the gate presets) and a simulator
for the overfitting study of those gates (``studies/gate_overfitting.py``).
"""

__version__ = "0.9.0"
