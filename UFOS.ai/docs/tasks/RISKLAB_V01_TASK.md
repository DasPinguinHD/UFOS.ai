# RISKLAB v0.1 — skeleton (overnight 2026-10-28)

Background: the career plan (Claude Project `claude/career-plan-bank-risk.md`) targets bank risk management.
The Risk Lab is a standalone Python package at `UFOS.ai/RiskLab/` (user decision 2026-10-28). It is independent of
the HedgeFund backend: no imports from it, its own tests, its own README.

## The most important rule: derive first, then code (user decision 2026-10-28)
The user derives the risk formulas himself (sheet 1: VaR / ES definitions, normal / t VaR and ES, square-root-of-time,
Kupiec, Basel traffic light, Christoffersen). **This round must NOT implement any of them**: no quantile / VaR / ES
formulas, no likelihood-ratio tests, no traffic-light zones, and no "true VaR" computed by the generators. Where
they belong, write a stub that raises `NotImplementedError("sheet 1 — implemented together with the user")`, with
a docstring stating the interface contract (inputs, outputs, units, sign convention).

## Scope of this round (plumbing only)
1. **Layout:** `UFOS.ai/RiskLab/`
   - `pyproject.toml`: package `risklab`, src layout, Python >= 3.10, dependencies `numpy`, `scipy`.
   - `requirements.in`: the unpinned direct dependencies. The hash-pinned lock is generated later on the user's PC.
     Note in the README that it is still missing (repo rule: pinned deps).
   - `src/risklab/`, `tests/`, `README.md`, `.gitignore` (ignores `local_data/`, `out/`, venvs).
2. **Conventions** (`src/risklab/conventions.py` + README):
   - `L` is the loss; positive = loss; returns are converted by `L = -r`.
   - `alpha` is the confidence level in (0, 1), e.g. 0.99.
   - Daily horizon. Arrays are float64 numpy arrays.
   - Every random function takes `seed` or a `numpy.random.Generator`; same seed = same output on every platform.
3. **Synthetic data** (`src/risklab/data/synthetic.py`): each generator returns a frozen dataclass
   `SyntheticSeries(losses, sigma_true, mu_true, dist, dist_params, regime, meta)`. It carries the per-day true
   conditional *parameters* only, never quantiles. Generators:
   - `iid_normal(n, mu, sigma, seed)`;
   - `iid_student_t(n, mu, sigma, nu, seed)`, scaled so that the standard deviation equals `sigma`
     (requires nu > 2; otherwise ValueError);
   - `garch11(n, omega, alpha1, beta1, innovations="normal"|"t", nu=None, seed, burn_in=500)`: requires
     alpha1 + beta1 < 1 and positive parameters, otherwise ValueError; t-innovations scaled to unit variance;
   - `regime_switching(n, sigmas=(low, high), transition=((p00, p01), (p10, p11)), seed)`: a two-state Markov chain;
     `regime` holds the state path.
   - (No jumps in v0.1.)
4. **Model interface** (`src/risklab/models/base.py`):
   - `class RiskModel(Protocol)`: `name`; `fit(losses_past: np.ndarray) -> None`;
     `forecast(alphas: Sequence[float]) -> Forecast`.
   - `Forecast(var: dict[float, float], es: dict[float, float] | None)`.
   - Stubs `HistoricalSimulation`, `NormalModel`, `StudentTModel` in `models/` that raise NotImplementedError
     (see the rule above).
5. **Walk-forward engine** (`src/risklab/engine.py`): `walk_forward(losses, model_factory, window, alphas,
   refit_every=1)`. For each t >= window it fits on `losses[t-window:t]` **only** and forecasts for day t. It returns
   `BacktestFrame`: arrays `t`, `loss`, `var[alpha]`, `es[alpha]` (or None) and `exceedance[alpha] = loss > var`.
   It must make look-ahead impossible: the model only ever receives a copy of the past window.
6. **Backtest interface** (`src/risklab/backtest/base.py`): a dataclass `TestResult(name, statistic, df, p_value,
   reject_at, decision, n_obs, n_exceed, notes)`; stubs `kupiec_pof`, `christoffersen_ind`,
   `christoffersen_cc` and `traffic_light` that raise NotImplementedError (sheet 1).
7. **Results export** (`src/risklab/report/export.py`): deterministic JSON + CSV writers for a `BacktestFrame` and
   a list of `TestResult`s (sorted keys, fixed float format, a `meta` block with the package version and seed).
   The default output folder is `out/` (git-ignored).
8. **Tests** (`tests/`, `unittest`, no network, temp dirs):
   - generators are reproducible (same seed → identical bytes);
   - parameter validation raises ValueError;
   - a large-n sanity check: sample mean and std within tolerance of the target (normal, scaled t), and GARCH
     unconditional variance ≈ omega / (1 - alpha1 - beta1);
   - regime path statistics roughly match the transition matrix;
   - the engine counts exceedances with a dummy constant-VaR model defined in the test, and a "spy" model
     proves that fit() never sees index >= t;
   - export round-trip.
   - Stubs raise NotImplementedError with the sheet-1 message.
9. **README.md** (English, short): purpose (a bank-style market-risk lab, a showcase for risk management), status
   v0.1 = skeleton, the "derive first" principle, data policy (synthetic in the repo, own data only in
   `local_data/`), layout, how to run the tests, roadmap (v0.2 = estimators + tests from sheet 1; v0.3 = GARCH / FHS /
   Monte Carlo + ES backtest).
10. **CI:** prepare `RiskLab/ci/risklab-tests.yml` (a GitHub Actions workflow: Python 3.10 + 3.12, unittest). The user
    copies it into `.github/workflows/` himself (`.github` is protected).

## Rules
- Repo rules from `CLAUDE.md` apply: no third-party / real data, `HedgeFund/tools/check_repo_data.py` must pass,
  no git (read-only status only), no deleting (`*.stale`), no network in tests, English code / comments / docs.
- Scope label `risklab0`: allow-list = `UFOS.ai/RiskLab/**` + this task file + `UFOS.ai/docs/tasks/README.md`.
- Tests run on the VM (Python 3.10) in chunks ≤ 170 s; a final run in the cloud container if needed.
- Report: files, test results, open points (lockfile, CI copy) — then stop.
