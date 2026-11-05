"""Risk Lab: a bank-style market-risk lab (VaR / ES estimation and backtesting).

v0.3: historical simulation, normal, Student-t, EWMA, GARCH(1,1)-t and
filtered-HS VaR / ES; h-day VaR / ES (square-root-of-time, GARCH Monte Carlo,
stationary block bootstrap); the true 1-day and h-day VaR / ES of the synthetic
generators; Kupiec, exact binomial, Christoffersen, the Basel traffic light and
the Acerbi-Szekely ES test; a walk-forward engine (stateful models, h-day
blocks) and a deterministic results export.
"""

__version__ = "0.3.0"
