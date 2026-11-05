# Risk Lab

A bank-style market-risk lab: estimate one-day Value-at-Risk (VaR) and Expected
Shortfall (ES), and backtest the forecasts the way a bank's risk management does.
It is a showcase for risk management and is part of UFOS.ai. It is independent of
the HedgeFund backend: there are no imports between them, and it has its own
tests and dependencies.

**Status: v0.1, a skeleton.** The plumbing is in place: conventions, synthetic
data, the model interface, the walk-forward engine, the backtest interface and
the results export. The risk formulas are not implemented yet.

## Derive first, then code

The user derives the formulas himself first (sheet 1: the VaR / ES definitions,
normal and t VaR / ES, square-root-of-time, Kupiec, the Basel traffic light and
Christoffersen). Until then each of them is a stub that raises
`NotImplementedError("sheet 1 — implemented together with the user")`. The
stub's docstring states its interface contract.

## Conventions

- `L` is the loss: positive = loss. Returns convert by `L = -r`.
- `alpha` is the confidence level, strictly inside (0, 1), e.g. 0.99. VaR and
  ES are thresholds on the loss, and an exceedance is `loss > var`.
- The horizon is one trading day. Arrays are 1-D float64 numpy arrays.
- Every random function takes `seed` (an int) or a `numpy.random.Generator`.
  The same seed gives the same output.

See `src/risklab/conventions.py`.

## Data policy

- Only synthetic data goes into the repo (`risklab.data.synthetic`: i.i.d. normal,
  scaled Student-t, GARCH(1,1) and two-state regime switching). Each generator
  returns the true per-day parameters, never quantiles.
- Your own data stays in `local_data/`, which git ignores. Exports go to `out/`,
  which git also ignores. No third-party or licensed data goes into the repo.

## Layout

```
RiskLab/
  pyproject.toml        package "risklab", src layout, Python >= 3.10
  requirements.in       direct dependencies (numpy, scipy), unpinned
  ci/risklab-tests.yml  GitHub Actions workflow (copy to .github/workflows/)
  src/risklab/
    conventions.py      sign / alpha / seed conventions, sheet-1 message
    data/synthetic.py   synthetic generators -> SyntheticSeries
    models/             RiskModel protocol, Forecast; HS / normal / t stubs
    engine.py           walk_forward -> BacktestFrame (no look-ahead)
    backtest/base.py    TestResult; Kupiec / Christoffersen / traffic-light stubs
    report/export.py    deterministic JSON / CSV export (default folder RiskLab/out/)
  tests/                unittest, no network
```

## Running the tests

From `UFOS.ai/RiskLab/`:

```
python -m pip install -r requirements.in
python -m unittest discover -s tests
```

A path shim in the tests puts `src/` on `sys.path`, so no install and no
`PYTHONPATH` are needed. `-t .` (package mode, used by CI) works as well.

Random streams are byte-identical for the same seed within the same numpy
version. Exports record `numpy_version` in their `meta` block.

## Open points

- **Pinned lock:** the repo rule asks for pinned, hash-checked dependencies.
  The lock (`requirements.txt` with hashes) is generated later on the user's PC.
  Until then `requirements.in` lists the unpinned direct dependencies.
- **CI:** copy `ci/risklab-tests.yml` to `.github/workflows/` (`.github` is protected).
- **CI hardening:** pin the GitHub actions (`actions/checkout`, `actions/setup-python`)
  to full commit SHAs instead of version tags.

## Roadmap

- **v0.1 (this version):** the skeleton.
- **v0.2:** the estimators and backtests from sheet 1: historical simulation,
  normal and t VaR / ES, square-root-of-time, Kupiec POF, Christoffersen
  independence and conditional coverage, and the Basel traffic light.
- **v0.3:** GARCH, filtered historical simulation (FHS), Monte Carlo, and an ES backtest.
