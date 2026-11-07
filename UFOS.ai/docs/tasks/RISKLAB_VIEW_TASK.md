# Stage 11 — Risk Lab view in the Hedge Fund window (design R2 "workbench")

User decisions 2026-11-06: layout **R2** (design artifact "Hedge Fund Overview – drafts", board R2): left a module
list (LIVE: Paper account · MODULES: IRRBB, Credit risk, Reverse DCF · STUDIES: Reports · recent runs), middle
inputs → Run → results (tables + charts, ⛶ on each), right a Methodology panel (formula + assumptions of the selected
figure) and the module's report. All four parts (paper account live, modules interactive, studies & reports,
methodology). Further decisions:
- **Wiring:** a one-shot CLI `python -m risklab.app <command>` in RiskLab's **own** venv, JSON on stdin → JSON on
  stdout, launched per Run by WPF (no port, no service). The RiskLab ↔ HedgeFund no-import rule stays.
- **IRRBB:** synthetic EUR balance sheet + EUR BCBS presets now, clearly labelled "synthetic"; a real US Treasury
  curve (FRED) is a separate later step (not in this task).
- **Module data:** the synthetic loan book / company / ranges move from `studies/` into the package (one source);
  the studies import them; saved study results are re-keyed or re-run and must reproduce the same numbers.
- **Opening reports:** allowed, narrowly: only `*.pdf` inside `C:\Users\leonh\Documents\UFOS.ai` (resolved full path
  inside the folder, no reparse points, name `^\d{4}-\d{2}-\d{2}_[A-Za-z0-9_-]+\.pdf$`), plus "Show in folder".
  The lead records this exception in CLAUDE.md.
Planning inventory 2026-11-06 is the basis; verify file:line before relying on it.

Scope labels: `rlapp` (RiskLab), `rlview-be` (HF backend + Shared), `rlview-ui` (WPF). Scope:
`UFOS.ai/RiskLab/**`, `UFOS.ai/HedgeFund/**`, `UFOS.ai/Shared/**`, `UFOS.ai/docs/**`.

## 0. Binding rules
- No fallbacks; failures → `{ok:false, reason}` → UI "not available (reason)". Each value one source.
- Every run is reproducible: inputs incl. `seed` and the package version echoed in the output.
- Results are hypothetical / synthetic — say so in the UI header and in every module result.
- Every chart and large table: ⛶ via `Shared/FullScreenHost.cs`.
- English UI, noun headings, no numbering; colour rules of SP §26 (amber = warnings such as SOT breaches).
- Tests: RiskLab's own suite + CI (no network); HF backend `unittest`; WPF static checks only (the user builds).
- Never open real DBs over the mount; secrets untouched; `check_repo_data.py` must pass.

## A. RiskLab package (`risklab/app/`, label `rlapp`)
1. `risklab/app/__main__.py` + `cli.py`: commands `var`, `backtest`, `irrbb`, `credit`, `dcf`, `methodology`,
   `version`. Input: one JSON object on stdin (size cap 2 MB; unknown keys → error). Output: one JSON object on
   stdout `{ok, command, version, inputs_echo, result | reason, warnings[]}`; exit 0 / 2 (bad input) / 3 (not
   computable, e.g. too few observations, `FitError`, `NoRootInBracket`).
2. `var` / `backtest`: input `{losses[] | returns[] + latest_equity, models[] ⊂ {hs, normal, t, ewma, garch_t,
   fhs}, alphas, window, refit_every, seed}`; 1-day only (MC / bootstrap are 10-day in the package → not offered in
   the app). Minimum windows per the package (HS ⌈1/(1−α)⌉, GARCH ≥ 50, walk-forward window < n); otherwise
   `reason` naming the requirement. Backtests: Kupiec, exact binomial (+ exact size), Christoffersen ind / cc,
   traffic light (250), Acerbi–Székely Z2 (`m_sim`, seed). Output the walk-forward series (VaR, ES, loss,
   exceedance) for the chart.
3. `irrbb`: `{preset ∈ {EUR_BCBS2016, EUR_BCBS2024}, floor on/off, nmd_core_share, nmd_avg_maturity, seed}` →
   curve points (base + shocked), ΔEVE / ΔNII per shock, % of Tier 1, outlier tests (15 % / 5 %), assumptions
   (`shock_assumptions`, `instrument_assumptions`, `SHOCK_SOURCES`). Label "synthetic EUR bank book".
4. `credit`: loan book from `risklab/defaults.py` (moved from `studies/credit_risk.py`), PD fit summary (AUC, Gini,
   HL test), IFRS 9 stage mix + ECL per scenario, IRB K / RWA; inputs: scenario weights, PD shift, seed.
5. `dcf`: company + ranges from `risklab/defaults.py` (moved from `studies/valuation_sensitivity.py`); reverse DCF
   implied growth / discount rate, tornado, two-way grid, Sobol (app default N = 1024, labelled "approximate";
   N field, max 16384), seed.
6. `methodology`: `{figure_id: {title, formula_text, assumptions[], source}}` from one new source
   `risklab/app/methodology.py` + the in-code assumption dicts; a test pins the figure ids and checks each `source`
   anchor exists in `docs/conventions.md`.
7. Move `loan_book`, `COMPANY`, ranges into `risklab/defaults.py`; studies import them; re-key / re-run the saved
   study results; a test proves the numbers are unchanged (golden values).
8. Tests: schema round trip per command, error JSON + exit codes, determinism under a seed, golden values vs. study
   rows, size cap / unknown keys; CI runs them. README "App CLI" section.

## B. HF backend + Shared (label `rlview-be`)
1. `GET /v2/risk/daily-returns` (token-protected, read-only): `{account_ids, as_of, days[], returns[], gaps,
   latest_equity, reason}` for the Overview's active account set (reuse `active_account_set` + `overview_risk`
   end-of-day logic; same refusal for > 1 account with series); parity test: RiskLab-convention HS VaR on these
   returns == `/v2/overview/risk` figure (computed inside the HF test with the HF re-implementation; no RiskLab
   import).
2. `Shared/PythonEnvironment.cs`: a RiskLab entry with its own lock contract (accept `requirements.lock` compiled
   from `requirements.in` by uv; staleness = sha256 of `requirements.in` recorded next to the venv); venv
   `UFOS.ai/RiskLab/.venv`, `pip install --require-hashes -r requirements.lock` only when the lock changed;
   missing / stale lock → stop with the exact uv command. `WaitForModuleAsync("risklab")`.
3. A generic one-shot runner in `Shared/` (generalise `OaProcess.RunAsync`): `ArgumentList`, hidden window, stdin
   JSON, captured stdout (cap 8 MB) / stderr, timeout (default 120 s, per command), cancellation = kill process
   tree, returns `{exitCode, stdout, stderr, timedOut, cancelled}`.
4. Tests (backend) + static checks (C#).

## C. WPF (label `rlview-ui`) — after A's JSON schema is fixed
1. `Platform/RiskLabView*.cs` replaces `RiskLabPlaceholderView` (rail target unchanged), R2 layout with
   `HedgeFund/Ui/` components: module list (+ recent runs of this session), inputs per module (validated), Run /
   Cancel with progress text, results (tables + charts: P&L vs −VaR with exceedances, IRRBB curves, ECL bars,
   tornado / Sobol bars), Methodology panel (selected figure; text verbatim from `methodology`), Report card.
2. Paper account: fetch `/v2/risk/daily-returns`, run `var` + `backtest`; with < 100 returns most figures read
   "not available (needs ≥ N daily returns, have n)" — honest, no stand-ins.
3. Reports: list the folder (regex above), map topics → modules (irrbb, credit, valuation / reverse-dcf,
   validation, overfitting, overview), newest per topic; "Open PDF" (rules above) and "Show in folder"
   (`explorer.exe /select,<path>` via ArgumentList).
4. Responsive like the Overview (narrow → methodology under results); no empty band at the bottom.

## D. Docs and acceptance
RiskLab README + CHANGELOG, SPEC (HF §26 addition for daily-returns; RiskLab docs for the CLI), CODEMAP, CLAUDE.md
(PDF exception — lead), todo. Independent review per part; backend + RiskLab suites green (full HF suite in the
container via the lead; RiskLab CI); C# static checks; the user builds and click-tests; PDF report via the lead.
