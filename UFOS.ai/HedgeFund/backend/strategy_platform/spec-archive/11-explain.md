# Strategy Platform SPEC — archive: 11-explain

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 11. Explain: attribution (layer 1) + AI explanation (layer 2) (contract, 2026-10-14)

Reason (user, 2026-10-14): a button in the backtest and validation report
that answers "why did the strategy perform the way it did", AI-supported.
Risk named and accepted: an LLM finds a plausible story for ANY result
(confabulation, hindsight). Therefore two layers: layer 1 computes the
answer deterministically (numbers, no AI); layer 2 only puts layer 1 into
words. Interview decisions (user, 2026-10-14): model **selectable per
click** (Standard / Premium, as in the Newsroom); the AI may see the
**numbers AND the strategy logic** (the spec leaves the machine via
OpenRouter); explanations are **stored with the run** (regenerate = extra
click, costs again); layer 1 is **always visible** as a report section
without AI; **own daily budget** for explanations (not the fund's AI
budget); language **selectable EN/DE**; backend **checks every number** in
the AI text against layer 1 and **marks** unmatched numbers (shown, not
discarded).

### 11.1 Layer 1 — attribution (`backtest/attribution.py`, owner X1)

- Computed at the end of every backtest job (also the base run of a
  validation) from the in-memory result; pure Python/numpy, deterministic,
  no network, no AI. Stored on the run card in a new column
  `attribution_json` (migration; part of the insert-only finished card).
  Runs finished before this change have no attribution → the report says
  "Not available for runs created before engine <version> — run the
  backtest again." (no recomputation, no guessing).
- **Engine inputs:** the engine records per symbol and day (sparse, only
  non-zero) mark-to-market P&L on the `split` series, dividends received /
  paid, fees, slippage, borrow fees, funding → `BacktestResult.symbol_pnl`.
  ENGINE_VERSION bump. The PaperBroker parity test must stay green
  (recording only, no behaviour change).
- **Contents** (every block may be `{"unavailable": reason}`):
  1. `decomposition` — daily OLS r_t = a + b·m_t + e_t vs the run's
     benchmark (same regression as metrics alpha/beta): cumulative and
     annualised market part (b·m), alpha part (a), residual (e); gross P&L,
     costs (fees, slippage, borrow, funding), dividends net, cash; must
     reconcile to the equity change (`unattributed` reported; test: within
     0.01 % of starting capital on synthetic data).
  2. `by_symbol` — per symbol: P&L $ and share of total, long P&L, short
     P&L, days held, fills, costs; top 10 and bottom 10 + "others".
     Sum over symbols + cash/other = total P&L (test).
  3. `by_year` — per calendar year: strategy return, benchmark return,
     excess, max drawdown, avg net exposure.
  4. `by_regime` — 4 market regimes from the BENCHMARK, known at t (no
     look-ahead): trend = trailing 63-session benchmark return ≥ 0 → up,
     else down; volatility = trailing 21-session realised vol ≥ its
     expanding median → volatile, else calm (first 63 sessions =
     "warm-up", excluded). Per regime: days, share of days, strategy and
     benchmark annualised return, hit rate. Constants in one place.
  5. `long_short` — P&L and average exposure of the long and short side.
  6. `periods` — best 3 and worst 3 non-overlapping 63-session windows
     (strategy vs benchmark return); top 3 drawdowns (start, trough,
     recovery or "not recovered", depth) with the 3 symbols that lost most
     in each.
  7. `costs` — totals and as % of gross P&L, turnover (from metrics).
  8. `signal` — references to the stored IC / ICIR / half-life metrics
     (copied, not recomputed).
- **Fact table:** `facts: [{id, label, value, unit, display}]` — every
  number above gets a stable id (e.g. `dec.alpha_ann`, `sym.NVDA.pnl`,
  `year.2008.excess`) and the exact display string the report shows.
  Layer 2 may only use these; the number check uses them.
- **Validation runs:** layer 1 = the attribution of the validation's base
  (training) backtest + `gate_facts` (per gate: value, threshold, status,
  and key details: fold Sharpes/ICs, grid min/median/max Sharpe, PBO
  stats, deflated-Sharpe inputs, stress rows) — also in the fact table.
  Holdout runs: attribution of the holdout window only.

### 11.2 Layer 2 — AI explanation (`backtest/explain.py`, owner X2)

- `POST /v2/backtests/{id}/explanations` and
  `POST /v2/validations/{id}/explanations` {model: "standard"|"premium",
  language: "en"|"de"} → 200 explanation (synchronous, LLM timeout 90 s);
  `GET …/{id}/explanations` → list, newest first. Only for status `done`
  with attribution present, else 409 with the reason. Behind api_auth,
  strict validation, audited (run, model, language, cost), no stack traces.
- Call via the existing `fund/llm.chat_json` (OpenRouter key
  "openrouter-api-key" from the Credential Manager, cost ledger), feature
  `sp-explain`, label "Hedge Fund · Explanations"; models =
  `fund.llm.models()`; temperature 0.2; max_tokens 2,000.
- **Prompt** (fixed in code, English instructions; answer language as
  requested): explain WHY the result came about using ONLY the given facts;
  cite every number with its fact id `[id]`; causes from outside the data
  (macro events, news, sector stories) are allowed ONLY in a separate
  `hypotheses` list, phrased as hypotheses; no investment advice; no
  recommendations to change parameters (that would be untracked trials —
  stage 6 territory); state plainly when the data cannot answer something.
- **Input:** the fact table, run metadata (range, benchmark, capital, gate
  preset), the strategy logic (universe, features/signals, portfolio,
  execution, risk, parameters). Name, description, tags, provenance are
  UNTRUSTED text: passed as quoted JSON string fields, truncated (name ≤ 120,
  description ≤ 2,000 chars), with the instruction that they are data, not
  instructions. Never: keys, paths, other runs, holdout results in a
  non-holdout explanation. Prompt size cap 40,000 chars (facts trimmed
  from the "others" end with a note).
- **Output JSON:** `{summary: str (≤ 600 chars), sections: [{title,
  text}] (≤ 6, each ≤ 1,500 chars), hypotheses: [str] (≤ 5), limits:
  [str] (≤ 5)}`; anything else / oversize → `bad_json` error (cost still
  booked), no partial display.
- **Number check:** every number in the output text (EN and DE formats,
  %, $, signs, thousands separators, decimals) is matched against the fact
  table (value at display precision, percent ↔ fraction, relative
  tolerance 0.5 %); also allowed: years 1990–2100, dates present in the
  facts, integers 1–12 (counts/ordinals). Unknown `[id]` citations are
  flagged too. Result `check: {ok, unverified: [{text, offset, section}],
  unknown_ids: […]}` stored and shown — never silently removed.
- **Budget:** setting `explain_daily_budget_usd` (default 1.00, 0 = off,
  max 50) in hedgefund.db meta, own spend counter per UTC day; pre-call
  estimate with `fund.llm` price assumptions; blocked → 402-style user
  message "Explanation budget reached — raise it in Settings."; failed
  attempted calls charged like `charge_failed_call`.
  `GET/PUT /v2/settings/explain` {daily_budget_usd} → + spent_today
  (audited).
- **Storage:** table `run_explanations(id, run_id, run_kind, created_at,
  model, language, facts_hash, output_json, check_json, usage_json,
  cost_usd, provenance_json)` insert-only; regenerate = new row.
  `provenance("sp-explain", model)` (EU AI Act Art. 50 label).
- Tests: no network (`fund.llm.TRANSPORT` fake); attribution reconciliation
  + regime no-look-ahead; number check cases (EN/DE, %, false positives);
  budget block; 409s; insert-only; injection text in the description stays
  a quoted data field.

### 11.3 WPF (owner XW)

- Backtest report and validation report: new section **Attribution**
  (always, no AI): decomposition bar (market / alpha / costs / dividends),
  by-symbol top/bottom table, by-year table (strategy vs benchmark,
  excess coloured), regime table, best/worst periods, drawdown
  contributors; validation report: attribution of the base run.
- Button **"Explain with AI…"** (enabled only for `done` runs with
  attribution; tooltip with the reason otherwise) → panel: Standard /
  Premium toggle with estimated cost, EN / DE toggle, "Generate".
  Waiting state "Writing explanation…". Result header "✦ AI-generated ·
  <model> · <time> — explains the computed attribution; not verified, not
  investment advice". Unverified numbers highlighted red with tooltip "not
  in the computed data"; hypotheses in their own labelled box; plain text
  only (no markup, no links). "Regenerate (costs again)" and "Earlier
  versions (n)".
- Settings: "AI explanations — daily budget (USD)" with today's spend.

### 11.4 Decision: existing runs (user, 2026-10-14)

Runs finished before this change get NO attribution (also no partial one
from stored curves and no replay button): "We are in the development phase,
that is fine." The report shows the "Not available … run the backtest again"
note from §11.1; the "Explain with AI…" button is disabled with that reason.

### 11.5 X1 notes (implemented 2026-10-14: attribution — §11.1, backend part)

- **Files**: `backtest/attribution.py` (new; formulas in its docstring),
  `backtest/engine.py` (recording only), `backtest/runcard.py`,
  `backtest/jobs.py`, `validation/jobs.py`, `validation/report.py`,
  `storage/db.py`, `api/routes.py`; review fixes 2026-10-16 (L3, L7) also
  `validation/parallel.py`, `backtest/stress.py`. Tests: `test_sp_attribution.py` (21);
  key sets extended in `test_sp_api_backtests.py` /
  `test_sp_api_validation.py`; `test_sp_resume.py` asserts
  `SCHEMA_VERSION >= 8` (was `== 8`).
- **Engine** `ENGINE_VERSION = "sp-backtest/3c.2"` (the resume basis
  includes it: a run started on 3c.1 cannot be resumed → "The basis changed
  (the engine was updated …)"). New dataclass field `BacktestResult.symbol_pnl`
  = `{fields: ["date", "symbol", "pnl", "mtm", "dividends", "fees",
  "slippage", "borrow", "funding", "mv"], rows: [[…], …]}` — one row per
  (VALUED close, symbol) where the symbol was held at that close, was held
  at the previous valued close, or booked cash in between; sorted by date,
  symbol. Cash ledger rows are binned to the first valued close at/after
  their ts. `mtm` = mv_t − mv_prev + fill cash (contains the fill
  slippage); `dividends` (dividend + dividend_short), `fees` (−(fee +
  liquidation_fee)), `borrow` (borrow_fee), `funding` = signed cash (+ =
  gain); `slippage` = POSITIVE reconstructed cost (metrics.costs rule),
  already inside `mtm`; `pnl` = mtm + dividends + fees + borrow + funding;
  `mv` = signed qty × close quote. Ledger rows without a symbol other than
  deposit/reset → symbol `"_cash"` (none today). Σ pnl = last valued equity
  − capital (tested). The simulation is unchanged (parity tests green).
  `validation.jobs._strip_result` drops `symbol_pnl` from the payload
  shipped to pool workers.
- **Recording flag (review L7, 2026-10-16)**: `engine.run(…,
  record_symbol_pnl=True)`; default ON (single backtests, validation base
  run, holdout run); OFF (`symbol_pnl` None) in every grid run
  (`parallel.grid_task`, the sequential grid loop in `validation/jobs.py`)
  and stress run (`stress.episode`, `stress.synthetic_costs`) — their
  results are reduced to returns/metrics. Same simulation either way
  (tested: equity, fills, ledger equal). An attribution of a current-engine
  result without symbol_pnl says "Per-symbol P&L was not recorded for this
  run (engine flag record_symbol_pnl off)." (`attribution.NOT_RECORDED`);
  engine 3c.1 results keep the old-run text.
- **Storage v9**: `backtest_runs.attribution_json TEXT` (in `RUN_COLUMNS`
  and `RUN_RESULT_COLUMNS`; written only by running → done); the
  `backtest_runs_running_note` trigger is rebuilt to refuse it too. Cards
  finished before v9 stay NULL (§11.4). Validation and holdout runs live in
  the same table (kind), so they use the same column. **X2: take v10.**
- **When computed**: `jobs._run` after the metrics (phase `metrics`,
  message "computing the attribution"); the validation job from the BASE
  (training) result and its metrics; the holdout job from the holdout
  window's result (= the window only). Never checkpointed: the base run is
  not a checkpoint step (§10.5), a resumed attempt re-runs it from the data
  checkpoints and recomputes the attribution deterministically.
- **`attribution` (stored JSON, `attribution.compute(result, metrics)`)**:
  `{version: "sp-attribution/1", engine_version, currency: "USD", range:
  {start, end, calendar}, capital, benchmark, decomposition, by_symbol,
  by_year, by_regime, long_short, periods, costs, signal, facts, note?}`;
  every block may instead be `{unavailable: reason}`; `note` (only when the
  result had no symbol_pnl) = the old-run text.
  - `decomposition` = `{total_pnl, regression: {benchmark, pairs, beta,
    alpha_daily, market_cum, alpha_cum, residual_cum, market_ann,
    alpha_ann, residual_ann, market_pnl, alpha_pnl, residual_pnl,
    outside_regression_pnl} | {unavailable}, sources: {price_pnl,
    slippage, fees, borrow, funding, dividends_net, cash_other,
    costs_total, gross_pnl, unattributed} | {unavailable}}` (costs
    positive; beta/alpha_ann = `metrics.benchmark_stats`, the same OLS;
    market+alpha+residual+outside = total_pnl).
  - `by_symbol` = `{symbols, top: [row], bottom: [row], others: {count,
    pnl, long_pnl, short_pnl, flat_pnl, costs, dividends, share},
    reconciliation: {total_pnl, symbols_pnl, cash_other, unattributed}}`,
    row = `{symbol, pnl, share, long_pnl, short_pnl, flat_pnl, days_held,
    fills, costs, dividends}`; top = best min(10, ⌈n/2⌉), bottom = the 10
    worst of the rest (worst first).
  - `by_year` = `{years: [{year, days, return, pnl, benchmark_return,
    excess, max_drawdown, avg_net_exposure}]}`.
  - `by_regime` = `{benchmark, trend_window: 63, vol_window: 21,
    warmup_sessions: 63, warmup_days, unknown_days, labelled_days, regimes:
    [{regime (up_calm|up_volatile|down_calm|down_volatile), days, share,
    strategy_return_ann, benchmark_return_ann, hit_rate}]}` (label of day
    j's return from the benchmark account up to the close of j−1;
    `attribution.regime_labels`).
  - `long_short` = `{long_pnl, short_pnl, flat_pnl, avg_long_exposure,
    avg_short_exposure}` (row side = sign of mv at the previous valued
    close, else at this close, else flat).
  - `periods` = `{window: 63, best: [{start, end, strategy_return,
    benchmark_return, excess}] ×3, worst: […] ×3, drawdowns: [{start
    (peak), trough, recovery (null = not recovered), recovered, depth,
    days_to_trough, contributors: [{symbol, pnl}] (≤ 3, losses only)}] ×≤3}`.
  - `costs` = `{fees, liquidation_fees, slippage, borrow, funding, total,
    gross_pnl, net_pnl, pct_of_gross_pnl, pct_of_gross: {fees, …},
    pct_of_gross_reason?, turnover_annual}` (copied from metrics).
  - `signal` = cross-sectional `{mode, symbols, horizons: [{horizon,
    mean_ic, icir, t_stat, days, reason}], half_life_days,
    half_life_reason}` | time-series `{mode, symbols, window_days, windows,
    positive_share, mean_corr, reason}` (copied from metrics).
- **`facts`** = `[{id, label, value, unit, display}]`, value = the raw
  number (or ISO date / status text), display = the exact report string;
  null values are omitted. Units and display: `usd` "$1,234.56" /
  "-$1,234.56" (`run.capital`, `gate.G8.*` with 0 decimals); `fraction`
  (×100) "12.34 %"; `percent` (already %) "12.34 %"; `number` (2 decimals;
  ICs 4, probabilities/PBO/DSR 3); `count` "12"; `days` "12.3"; `date`
  ISO; `status` text. **Ids**: `run.capital|start|end`;
  `dec.total_pnl|beta|market_cum|market_ann|market_pnl|alpha_cum|alpha_ann|
  alpha_pnl|residual_cum|residual_ann|residual_pnl|outside_regression_pnl|
  price_pnl|slippage|fees|borrow|funding|dividends_net|cash_other|
  costs_total|gross_pnl|unattributed`; `sym.<SYMBOL>.pnl|share|long_pnl|
  short_pnl|days_held|fills|costs` (top + bottom only), `sym._others.pnl|
  count|share`, `sym.symbols`, `sym.unattributed`; `year.<YYYY>.return|
  benchmark|excess|max_drawdown|net_exposure|pnl`; `regime.<regime>.days|
  share|strategy_ann|benchmark_ann|hit_rate`, `regime.warmup_days`,
  `regime.unknown_days`; `ls.long.pnl|exposure`, `ls.short.pnl|exposure`,
  `ls.flat.pnl`; `period.best<k>.start|end|strategy|benchmark|excess`,
  `period.worst<k>.…` (k = 1..3); `dd.<k>.depth|start|trough|recovery
  ("not recovered" as status)|days_to_trough`, `dd.<k>.sym.<SYMBOL>.pnl`;
  `cost.fees|liquidation_fees|slippage|borrow|funding|total`,
  `cost.pct_of_gross`, `cost.<component>.pct_of_gross`, `cost.turnover`;
  `sig.ic_h<h>.mean|icir|t_stat|horizon`, `sig.half_life_days`,
  `sig.ts.positive_share|mean_corr|window_weeks (13)|window_days (65/91)`.
  **Method constants as facts** (review L3: numbers named in labels must
  be fact values for the number check), unit `count`, display = the
  integer: `run.periods_per_year` (252 | 365), `regime.trend_window` (63),
  `regime.vol_window` (21), `regime.warmup_sessions` (63), `period.window`
  (63), `sym.top_n` (10), `sig.ic_h<h>.horizon`,
  `val.stress.<key>.percentile_p5|p50|p95` (5/50/95, Monte-Carlo),
  `gate.H.horizon`, `gate.H.required_ratio` (fraction, "50.00 %"). Tested:
  every integer > 12 (not a year) in any label is a fact value.
- **`gate_facts`** (validation report AND holdout report, key
  `report.gate_facts`; `REPORT_VERSION` "sp-validation/4.2";
  `attribution.gate_facts(report)`, recomputed by `report.merge` so the
  statuses follow exceptions) = `[{gate, name, status, value, threshold,
  facts: [fact]}]` for every gate in the report (G1–G8; holdout: H). Ids:
  `gate.<G>.status|value|threshold` (units: G1 number (time-series: count),
  G2 number, G3 days, G4/G5 number 3 dp, G6 fraction, G7 count, G8 usd, H
  number) + details: G1 `val.fold.<k>.sharpe|mean_ic|total_return`,
  `gate.G1.folds_positive|best_horizon`; G2 `gate.G2.alpha_annual|beta`; G3
  `gate.G3.half_life_days|avg_holding_days`; G4 `val.dsr.sharpe_annual|
  sr0_annual|n_trials|trial_variance|skew|kurtosis|returns`; G5
  `val.pbo.value|configs|combinations|overfit_combinations|mean_logit`; G6
  `val.grid.sharpe_min|sharpe_median|sharpe_max|configs|base_sharpe`,
  `gate.G6.costs_sharpe`, `val.stress.<key>.strategy_dd|benchmark_dd`
  (episodes), `val.stress.<key>.total_return|sharpe|max_drawdown` (done
  scenarios), `val.stress.monte_carlo.max_drawdown_p5|p50|p95`,
  `….cagr_p5|p50|p95`; G7 `gate.G7.trades_closed|trades_open`; G8
  `gate.G8.capacity_usd|capital`; H `gate.H.sharpe|training_sharpe|mean_ic`.
- **API payloads** (behind api_auth): `GET /v2/backtests/{id}` adds
  `attribution` (the stored JSON, any kind) and `attribution_unavailable`
  (null when present, else the reason: "The attribution is computed when
  the run finishes." for queued/running, "Not available: the run is
  <status>." for failed/cancelled, and for done cards without it "Not
  available for runs created before engine sp-backtest/3c.2 — run the
  backtest again."). `GET /v2/validations/{id}` adds the same two fields;
  there `attribution` = `attribution.with_gate_facts(stored, merged
  report)`: the stored attribution + `gate_facts` + their facts appended to
  `facts` (ONE fact table for layer 2 and the number check). Lists
  unchanged. X2: use `attribution.unavailable_reason(card)` for the 409
  text and `with_gate_facts` for validation explanations.

### 11.6 X2 notes (implemented 2026-10-15: AI explanation — §11.2, backend part)

- **Files**: `backtest/explain.py` (new: prompt, call, schema, number check,
  budget, storage glue), `storage/db.py` (v10), `api/routes.py` (6 routes).
  Tests: `test_sp_explain.py` (unit, 29), `test_sp_explain_api.py` (routes on
  real attributions of a synthetic backtest + validation, 18). `fund/llm.py`
  and `newsroom/*` unchanged: `chat_json(…, feature="sp-explain",
  feature_label="Hedge Fund · Explanations")` registers the ledger label.
- **Storage v10**: table `run_explanations(id, run_id, run_kind
  (backtest|validation|holdout), created_at, model, language (en|de),
  facts_hash, output_json, check_json, usage_json, cost_usd,
  provenance_json)`, insert-only (triggers refuse UPDATE/DELETE); row +
  audit `explanation_created` in ONE transaction. Meta keys:
  `explain_daily_budget_usd` (absent = 1.00), `explain_spend_usd:<UTC
  YYYY-MM-DD>` (read-modify-write under `BEGIN IMMEDIATE`; an unreadable
  value fails closed → no call).
- **POST `/v2/backtests/{id}/explanations`** (kind backtest) and **POST
  `/v2/validations/{id}/explanations`** (kind validation|holdout), body
  exactly `{"model": "standard"|"premium", "language": "en"|"de"}` (unknown
  fields / query parameters → 400 `{error, errors: {field: message}}`).
  One call at a time per backend. 200 = the explanation:
  ```
  {"id": 7, "run_id": "…", "run_kind": "backtest", "created_at": "2026-10-15T09:00:00+00:00",
   "model": "<id OpenRouter answered with>", "model_tier": "standard", "language": "en",
   "facts_hash": "<sha256>",
   "output": {"summary": "…", "sections": [{"title": "…", "text": "…"}], "hypotheses": ["…"], "limits": ["…"]},
   "check": {"ok": false, "unverified": [{"text": "987.654,32 $", "offset": 41, "section": "summary"}],
             "unknown_ids": ["dec.nonexistent"]},
   "usage": {"cost_usd": 0.002, "prompt_tokens": …, "completion_tokens": …, "estimate_usd": 0.0135,
             "cost_estimated": false, "prompt_chars": …, "facts_total": …, "facts_sent": …, "facts_omitted": 0},
   "cost_usd": 0.002,
   "provenance": {"ai_generated": true, "generator": "UFOS.ai/HedgeFund/sp-explain", "provider": "OpenRouter",
                  "model": "…", "generated_at": "…", "human_reviewed": false,
                  "label": "AI-generated content (EU AI Act Art. 50)", "model_tier": "standard"}}
  ```
  `check.unverified[].section` ∈ `summary`, `sections.<i>.title`,
  `sections.<i>.text`, `hypotheses.<i>`, `limits.<i>`; `offset` = character
  offset inside that field (highlight `text` there). `[id]` citations stay
  in the text (the UI may show or strip them).
- **Errors** (all `{error: <user message>, code, …}`, never a stack trace,
  never a partial explanation): 404 `not_found` ("Backtest not found." /
  "Validation run not found."); 409 `wrong_kind` ("This is a validation run
  — explain it from the validation report."), `not_done` and
  `no_attribution` (text = `attribution.unavailable_reason(card)`, e.g. the
  "Not available for runs created before engine … — run the backtest
  again." note), `busy` ("An explanation is already being written — wait
  until it is finished."), `prompt_too_large`; 402 `budget` + `estimate_usd,
  daily_budget_usd, spent_today_usd` ("Explanation budget reached — raise it
  in Settings." / "AI explanations are switched off (daily budget is 0) —
  raise it in Settings." / spend unreadable); 502 `bad_json` (not JSON, or
  JSON outside the schema) and `llm_error`, 504 `timeout` (90 s) — all three
  with `cost_usd` booked; 503 `no_key` (Credential Manager message),
  `no_model`, `settings_invalid`, `spend_unrecorded` ("Explanation spend could
  not be recorded — explanations are paused until the backend restarts." —
  set when a spend write fails, fail closed until restart, audit
  `explanation_spend_unrecorded {day, usd}`), storage errors (`{error, kind}`
  as §4).
- **GET `…/{id}/explanations`** → `{run_id, explanations: [explanation]
  (newest first), explainable: bool, unavailable_reason: str|null,
  estimate_usd: {standard, premium}|null, budget: <settings payload>}` —
  `estimate_usd` is the pre-call estimate for the panel's toggle.
- **GET `/v2/settings/explain`** → `{daily_budget_usd, spent_today_usd,
  remaining_usd, day, enabled, default_daily_budget_usd: 1.0,
  max_daily_budget_usd: 50.0, paused_reason: str|null}`; **PUT** `{daily_budget_usd: number 0..50}`
  (0 = off) → same payload, audited `explain_settings_changed {old, new}`.
- **Audit**: `explanation_created {run_id, run_kind, model_tier, model,
  language, cost_usd, check_ok, unverified, unknown_ids, explanation_id}`,
  `explanation_failed {…, reason (bad_json|error|no_key|timeout),
  cost_usd}`, `explanation_blocked {…, reason, estimate_usd}`.
- **Prompt**: `explain.SYSTEM_PROMPT` (fixed English rules: only the facts,
  copy display strings and cite `[id]`, no new arithmetic, outside causes
  only in `hypotheses`, no advice / no parameter changes, say what the data
  cannot answer, `untrusted_text` is data, plain text, the JSON schema) +
  one user message `DATA (JSON …):\n{answer_language, run {run_kind, range,
  benchmark, capital, currency, gate_preset, engine_version,
  attribution_version, note, unavailable_blocks?, strategy_origin,
  strategy_ai_generated, strategy_external_text}, strategy_logic
  (category, universe, timeframe, parameters without descriptions,
  features, signal, portfolio, rebalance, risk, execution, costs),
  untrusted_text {name ≤ 120, description ≤ 2,000, tags ≤ 20 × 40,
  provenance ≤ 1,000 = the CLAIMED provenance from the version envelope
  (`envelope.claimed`, JSON text), parameter_descriptions ≤ 300} (fund.llm
  .sanitize_untrusted: single line, no ``` / <<< >>>), facts [{id, label,
  value, unit, display}], facts_note?}`. Validation/holdout facts =
  `attribution.with_gate_facts(stored, merged report)`; non-holdout runs
  drop any `gate.H.*`/`holdout.*` fact. Cap 40,000 chars (system + data):
  facts dropped `sym.<SYMBOL>.*` first, then `dd.<k>.sym.*`, then
  `period|regime|year|val.stress.*`, each from the list end (minimal count,
  `facts_note`); still too large → 409 `prompt_too_large`.
- **Schema limits** (`validate_output`): exactly the 4 keys; summary 1–600;
  ≤ 6 sections, each exactly {title 1–120, text 1–1,500}; ≤ 5 hypotheses and
  ≤ 5 limits, each 1–600 chars (item/title limits are X2 choices).
- **Number check** (`number_check`, against the FULL fact table; unit-aware
  after review M1, 2026-10-15): citations `[id]`, `[a, b]` are masked
  (unknown ids → `unknown_ids`); dates (ISO, DE `dd.mm.yyyy`, `yyyy-mm`) must
  occur in a fact value/display. Numbers: EN/DE separators, NBSP/apostrophe
  thousands (a lone `,`/`.` + 3 digits is read in the answer language),
  signs `+ - − –` (a dash after a digit is a range, not a sign), currency
  `$ US$ USD` (USD) and `€ EUR` (EUR) before — also glued, `USD98,765` — or
  after, `% percent Prozent pp Prozentpunkte`, `‰`, `bp/bps/Basispunkte`,
  scale words `k/thousand/Tsd.`, `m/M/million/Mio.`, `bn/billion/Mrd.`; a
  number glued to a letter (`Q3`, `H1`) is skipped. Token kinds: percent →
  only `fraction` (×100) and `percent` facts; USD → only `usd` facts (value
  × scale word, no ×100/÷100); EUR → never matches; plain → only
  `number|count|days|ratio` (and other numeric units). **Citation binding**:
  a number followed by a citation in the same clause (no `; ! ? newline`,
  no `. , :` + space, no other number in between) is checked ONLY against
  the cited fact(s), still unit-aware. Match = relative tolerance 0.5 %
  against the fact value and each number of its display string (display
  precision), or the exact display text; an unsigned text number matches
  either sign, an explicit sign must match. Allowed without a fact: plain
  integers 1–12 and four-digit years 1990–2100. Everything else →
  `unverified` (shown, never removed). Fabricated-number pass rate on a
  156-fact synthetic run: 3.75 % (test bound 6 %).
- **Budget**: `fund.llm.Budget` over the meta keys (fail closed; a failed
  spend write pauses explanations until restart — `fund.llm` only logs it);
  pre-call `fund.llm.estimate_cost(messages, 2000, tier)`; success books
  `usage_cost(usage, estimate)`; failures `charge_failed_call` (timeout =
  attempted → estimate). Separate from the fund's `ai_daily_budget_usd`.
