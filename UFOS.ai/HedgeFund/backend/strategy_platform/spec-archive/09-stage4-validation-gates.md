# Strategy Platform SPEC — archive: 09-stage4-validation-gates

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 9. Stage 4 — validation gates (contract, 2026-10-09)

Interview decisions (user, 2026-10-06/09): gates are **hard with a documented
exception**; thresholds preset **Standard** (Lenient/Strict selectable,
values editable in settings); two steps in the UI — **"Validate…"** (all
gates, no holdout, repeatable) → `candidate`, then **"Open holdout (once)"**
with typed confirmation → `validated`; the holdout may be opened **once per
strategy family** (strategy + all copies/duplicates/versions); **every
finished backtest/validation run of any family member counts as a trial**;
normal backtests end before the holdout window; families that already saw
the window are **contaminated**; a family that FAILS the holdout is
**locked** until ≥ 12 new unseen months exist, then may be re-tested ONCE on
exactly those new months (same rule as contaminated); the report shows how
many other families already opened the holdout window of the same universe.

### 9.1 Families, "seen until", holdout window

- **Family** = a root strategy and every strategy created from it by
  duplicate/import-of-export (follow `parent` links in envelopes; store
  `family_id` on `strategies`, migration computes it).
- **Holdout window** (global, rolling): from `holdout_start` to the last
  complete session; `holdout_start` = last complete session − 24 months for
  universes with equities (also mixed), − 12 months for crypto-only.
  Constants in one place.
- **seen_until(family)** = max simulated end date over all run cards of all
  family members (backtests, validation runs, holdout runs) — persisted per
  family, migration computes it from existing run cards.
- **Normal backtests and validation runs** must end before `holdout_start`:
  default end = `holdout_start − 1 session`; an explicit end inside the
  window → 400 "This end date lies inside the locked holdout window
  (from <date>). The holdout is opened once per strategy family via 'Open
  holdout'." Stress episodes inside the window are skipped with "not
  available: inside the locked holdout window".
- **Contaminated:** seen_until(family) ≥ holdout_start (e.g. today's SMA
  50/200 and the Bollinger copy family). Shown as a chip.
- **Holdout eligibility:** unseen window = (max(seen_until, holdout_start−1),
  last session]; fresh family → the full window (24/12 months); contaminated
  or holdout-failed family → eligible once the unseen window is ≥ 12
  months, and the test uses ONLY the unseen window. Not eligible → the
  button is disabled with the exact date when it becomes eligible.
- Opening the holdout sets seen_until = last session (whatever the result)
  and writes an immutable `holdout_events` row (family, strategy, version,
  window, result, run id, ts). Universe counter = number of holdout events
  whose window overlaps the current one, by universe preset/symbol set.

### 9.2 Trials

- trial_count(family) = number of finished run cards (backtest, validation,
  holdout) of all family members; `trial_number` on run cards is now set.
- Deflated Sharpe uses the family trial count and the variance of the
  annualised Sharpe ratios of those trials (Bailey & López de Prado 2014);
  with 1 trial, variance term uses the documented minimum.

### 9.3 Validation run (`validation/`)

One job (same single-job runner as backtests, same 409 rule):
1. Backtest on the training range (default load per `history_from`, end =
   holdout_start − 1).
2. **Walk-forward folds:** K = 5 consecutive, equal-length out-of-sample
   segments of the training range (the spec is fixed — no refit); per-fold
   metrics.
3. **Parameter grid** from the spec's `parameters` (min/max/step; ≤ 64
   configurations, evenly subsampled if more) → one backtest per
   configuration (needed for PBO and robustness).
4. Stress: costs ×2 and slippage ×3 (per class), historical episodes
   outside the holdout window.
5. Gates (below) → validation report (run card kind `validation`,
   insert-only).

### 9.4 Gates (Standard thresholds; Lenient / Strict in parentheses)

| # | Gate | Pass rule |
|---|---|---|
| G1 | Consistency | cross-sectional: daily rank-IC t-stat ≥ 2.0 (1.5 / 3.0) at the best of horizons 1/5/10/21 AND mean IC > 0 in ≥ 4 of 5 folds (3 / 5); time-series (single symbol): Sharpe > 0 in ≥ 4 of 5 folds |
| G2 | Alpha | alpha vs the run's benchmark, Newey-West t-stat ≥ 2.0 (1.5 / 3.0) and alpha > 0 |
| G3 | Signal decay | IC half-life ≥ average holding period of closed trades (time-series strategies: skipped as "not applicable — single-symbol strategy", counts as pass with that note) |
| G4 | Deflated Sharpe | probability ≥ 0.95 (0.90 / 0.99) |
| G5 | Overfitting (PBO, CSCV with S = 10 blocks over the parameter grid) | PBO ≤ 0.30 (0.50 / 0.20); needs ≥ 4 configurations — fewer → FAIL "needs ≥ 4 parameter configurations (add min/max/step to parameters)" |
| G6 | Robustness | every grid neighbour within ±20 % of each parameter keeps ≥ 50 % of the base Sharpe (40 % / 60 %); costs ×2 net Sharpe > 0 (×2 / ×3); each available stress episode: max drawdown ≤ 1.5 × the benchmark's in that episode |
| G7 | Sample size | ≥ 100 closed trades (50 / 200) |
| G8 | Capacity | capacity estimate ≥ 10 × capital |
| H | Holdout (final, separate step) | on the holdout (or unseen) window: net Sharpe > 0 AND ≥ 50 % of the training Sharpe AND mean IC > 0 (time-series: Sharpe only); **no exception possible** |

Every gate result: `{gate, status: pass|fail|exception, value, threshold,
explanation, details}`. A metric that cannot be computed → `fail` with the
reason (never a default pass), except the explicitly listed G3 case.
- **Exception:** a failed G1–G8 may be overridden by the user with a written
  reason (≥ 30 characters) → status `exception`, audit row, shown in the
  report and on the strategy card; the holdout gate H can never be
  overridden. Exceptions belong to the validation run (a new run needs new
  exceptions).
- `candidate` requires all G1–G8 pass/exception in the latest validation run
  of the CURRENT version; `validated` requires H pass for that version.
- Settings: `GET/PUT /v2/settings/gates` {preset, overrides} — audited.

### 9.5 Lifecycle + API

- Transitions: `backtested → candidate` via a passing validation run
  (evidence run id); `candidate → validated` via a passing holdout run;
  holdout fail → family flag `holdout_failed_at`, strategy stays
  `candidate` but cannot move to validated/incubation; contaminated or
  locked families show the eligibility date. Existing rules for higher
  states unchanged (incubation etc. later stages).
- `POST /v2/validations` {strategy_id, version?, history_from?} → 202
  {run_id}; `GET /v2/validations/{id}` (progress + report);
  `GET /v2/validations?strategy_id=`;
  `POST /v2/validations/{id}/exceptions` {gate, reason} → updated report;
  `GET /v2/strategies/{id}/holdout` → {eligible, window, unseen_from,
  eligible_from, family_events, universe_events};
  `POST /v2/strategies/{id}/holdout` {confirm: "<exact strategy name>"} →
  202 {run_id}; wrong confirmation → 400.
- All behind api_auth, strict validation, no stack traces.

### 9.6 Calibration (acceptance)

Synthetic data with a planted, persistent cross-sectional signal → a
strategy using it passes G1–G8 (Standard) and H; pure-noise strategies fail
(≥ 95 % of 40 seeds fail at least one gate); an overfitted grid (best of 64
noise configs) fails G4 or G5. Library alphas: the report runs end-to-end on
synthetic data (no requirement to pass).

### 9.7 WPF

- Library detail: buttons **Validate…** (dialog: history_from) and **Open
  holdout (once)…** (disabled + tooltip with reason/eligibility date when
  not eligible; dialog explains "once per family", shows the window and the
  universe counter, requires typing the exact strategy name).
- `Platform/ValidationReportView`: gate table (status icon, value,
  threshold, plain-language explanation), fold chart, parameter-grid heat
  table, PBO/deflated Sharpe section, stress table, exceptions (button
  "Add exception…" per failed G1–G8 with reason box), holdout section.
- Chips on strategy cards: `contaminated`, `holdout failed (until …)`,
  `exception`; trial count in the detail pane.
- Backtest dialog: default end now holdout_start − 1 (shown), date picker
  refuses dates inside the window with the backend message.

### 9.8 V1 notes (implemented 2026-10-09: families, holdout lock, trials, lifecycle — §9.1, §9.2, §9.5 lifecycle)

- **Storage v6** (one transaction, like v3): `strategies.family_id`
  (+ index); `strategy_families(family_id PK, seen_until YYYY-MM-DD,
  holdout_failed_at, created_at, updated_at)`; `holdout_events(id, ts,
  family_id, strategy_id, version, window_start, window_end, universe_kind,
  universe_key, result pass|fail, run_id)` insert-only (triggers);
  `validation_exceptions(id, ts, run_id, gate G1–G8, reason, actor,
  UNIQUE(run_id, gate))` insert-only (for V2, §9.4);
  `backtest_runs.kind` (`backtest` | `validation` | `holdout`, existing
  cards `backtest`). `trial_number` may now be written exactly once:
  NULL → n together with running → done. The identity trigger was recreated
  for that, and it also fixes `kind`. The migration data step
  (`_v6_families`) computes family_id from the first version's envelope
  (`db.family_parent_of`: `parent` naming ANOTHER strategy = a duplicate; an
  import whose `claimed.exported_from` {strategy_id, version,
  content_hash} matches an existing version's hash exactly; unverifiable
  claims never join a family). It also computes seen_until = max `range.end`
  of the family's done cards and backfills `trial_number` of the existing
  done cards (order finished_at, id within the family). Run lists order by
  `created_at DESC, rowid DESC`, so "latest" is well defined within one second.
- **db API (v6)**: `family_parent_of`, `backtest_run_finish(run_id, fields, *,
  seen_until, audit) -> trial | None` (BEGIN IMMEDIATE: count, update, raise
  seen_until), `family_get`, `family_record_seen`, `family_strategy_ids`,
  `family_trial_count`, `family_done_runs`, `holdout_event_insert(event, *,
  audit)` (also sets seen_until = max(…, window_end) and holdout_failed_at =
  ts on fail / NULL on pass), `holdout_events_list(family_id=None, *,
  universe_key=None)`, `validation_exception_add(run_id, gate, reason, *,
  actor="user", audit=None) -> id` (gate G1–G8, reason 30..2000 chars, the
  card must be a done `validation` card, one per gate → else ValueError),
  `validation_exceptions_list(run_id)`, `backtest_runs_list(…, kind=)`.
- **`validation/families.py`** (public API for V2, names fixed):
  `family_of(strategy_id)`, `holdout_window(universe_kind, last_session=None)
  -> {start, end}` (dates), `seen_until(family_id) -> date | None`,
  `record_seen(family_id, end)`, `is_contaminated(family_id, window)`,
  `holdout_eligibility(strategy_id, now=None)` (ISO strings: {eligible,
  reason, window, unseen_from, eligible_from, family_events,
  universe_events, mode full|unseen, contaminated, holdout_failed_at,
  seen_until, universe_kind, holdout_start}), `record_holdout_event(
  strategy_id, version, window, result, run_id) -> id`, `trial_count`,
  `family_trial_sharpes` (`metrics_json.performance.sharpe` of the done
  cards; null values are left out), `training_end(spec, last_session=None)`,
  `HoldoutWindowViolation(InvalidRequest)` (→ 400; message per §9.1, "This
  start date …" for a start). Helpers: `universe_kind(spec)`,
  `universe_key(spec)` (`preset:<name>:<classes>` | `symbols:<sha256[:16]>`),
  `last_session(kind, now)`, `lock_for_kind(kind, now)`,
  `check_request_dates(spec, start, end)`, `family_chips(row, spec)`,
  `add_months`, `set_clock` (tests). Constants `EQUITY_HOLDOUT_MONTHS` 24,
  `CRYPTO_HOLDOUT_MONTHS` 12, `REOPEN_UNSEEN_MONTHS` 12.
- **Window rules (choices)**: last complete session = NY date of
  `calendars.last_close_before(now)` (equity/mixed) or UTC yesterday
  (crypto). start = the first session on/after last − N months, with the day
  clamped to the month end. Training end = the session before start.
  Eligibility as §9.1. `eligible_from` = the first session on/after anchor +
  12 months (anchor = max(seen_until, training end)): the family becomes
  eligible once that session is complete. `universe_events` = the number of
  OTHER families with a holdout event on the same `universe_key` whose
  window overlaps the current full window. A passed (re-)test clears
  `holdout_failed_at`; the events keep the history. An eligibility date
  beyond the NYSE calendar (2028+) raises `CalendarUnavailable`; no date is
  guessed.
- **Engine** (`engine.run(…, holdout_access=False)`): every run is locked.
  end None → load and simulate up to the training end; when the lock ends
  the run, `range.end_limited_by` is null. An explicit start/end on/after
  the window start → `HoldoutWindowViolation`. `range.holdout = {
  universe_kind, start, end, training_end, access}`. The universe kind comes
  from the strategy symbols; the benchmark does not count.
  `holdout_access=True` is ONLY for V2's holdout job.
- **Run cards**: `runcard.create(…, kind="backtest")`. A `backtest` or
  `validation` card with an explicit request start/end inside the window →
  `HoldoutWindowViolation` before anything is stored. Through
  `jobs.submit`, that reaches the route synchronously as 400. `holdout` cards
  get no date check. `finish` → `db.backtest_run_finish` (trial_number =
  family trial count at finish, seen_until raised to `range.end`; a result
  without `range.end` → ValueError). `list_runs(…, kind=)` and
  `latest_done(…, kind=)`; list rows add `kind` and `trial_number`.
- **Stress**: `run_stress(…, holdout_access=False)`. An episode whose end is
  on/after the window start (from `range.holdout.start` of the base run, else
  computed: `holdout_start_for`) → `not_available` "not available: inside
  the locked holdout window". Synthetic cost re-runs pass `holdout_access`
  only for a holdout base run.
- **Lifecycle — JSON keys read**: `backtested → candidate` takes the LATEST
  done `kind = 'validation'` card of the current version (an explicit
  `run_id` must be that card) and calls `validation.report.effective_passed(
  card) is True`. This is V2's function: the raw
  `metrics_json.validation.passed` merged with `validation_exceptions`;
  without the module the check raises, no stand-in. `candidate → validated`
  takes the latest done `kind = 'holdout'` card of the current version and
  requires `metrics_json.holdout.passed is True`. A family with
  `holdout_failed_at` set → `validated`/`incubation` refused ("the strategy
  family failed the holdout on D — locked until E (≥ 12 new unseen months),
  then one re-test on those months only"). Without a holdout card, the reason
  includes the eligibility ("… — the holdout can be opened now (once per
  strategy family)" / "… from DATE: <reason>"). `evidence_json` = {run_id,
  version, kind}; draft → backtested is unchanged ({run_id, version}).
  `registry.get()` rows and `registry.family_chips(row, spec=None)` carry
  {family_id, contaminated, holdout_failed_until, trial_count}.
- **Review fixes (2026-10-09)**: (1) *Family bypass by copy-paste.* Storage
  v7 adds the insert-only table `strategy_logic(strategy_id, version,
  logic_hash, PK(strategy_id, version))`, indexed by `logic_hash` and
  backfilled from the stored specs. `strategy_versions` keeps its columns.
  `db.logic_hash_of(spec)` = sha256 of the canonical stored spec without name,
  description, category, tags, provenance and the parameters' descriptions.
  `strategy_create` and `strategy_add_version` write the row in the same
  transaction. A NEW strategy without a parent link joins the family of the
  oldest strategy having a version with the same logic hash. This covers a
  spec text pasted as a new strategy and an import of an export with its
  provenance stripped (`registry._family_for`). A strategy EDITED into a twin
  keeps its family. Its eligibility, chips and lifecycle lock use
  `families.family_state(strategy_id)`: {family_id, related = own family +
  every family owning a version with the current logic hash, seen_until =
  max, holdout_failed_at = newest}; `family_events` counts the related
  families' events. Limit: a spec that differs in ANY value (one parameter
  default, max_weight …) has another logic hash and gets its own family. The
  `universe_events` counter in the report covers such near-duplicates. db
  API: `logic_hash_of`, `logic_hash_get(strategy_id, version)`,
  `families_with_logic_hash(logic_hash)`.
  (2) *Atomic holdout result.* `runcard.finish_holdout(run_id, result,
  metrics, *, family_event={"window": {"start", "end"}, "result":
  "pass"|"fail"}, stress=None) -> card + holdout_event_id` (the card must be
  kind `holdout` and `running`). It finishes the card (trial_number,
  seen_until) AND writes the holdout event (seen_until = window end,
  holdout_failed_at) in ONE transaction via `db.backtest_run_finish(…,
  holdout_event=)`; a failure in between writes neither. Audit action
  `holdout_finished` carries `holdout_event_id`. Helper
  `families.holdout_event_row(strategy_id, version, window, result, run_id)`.
  `families.record_holdout_event` stays for tests and tools; the holdout job
  must use `finish_holdout` instead of `record_holdout_event` + `finish`.
- Tests: `test_sp_families.py` (20), `test_sp_families_lifecycle.py` (16);
  `test_sp_api_strategies.py` transition text updated to stage 4.

### 9.9 V2 notes (implemented 2026-10-09: statistics, folds, grid, gates, jobs, report, API — §9.2–§9.6)

- **Files**: `validation/stats.py` (DSR, PBO/CSCV, Newey-West, normal
  CDF/inverse — no scipy), `folds.py`, `grid.py`, `gates.py`, `report.py`,
  `jobs.py`; formulas in each module docstring. Tests:
  `test_sp_validation_stats.py` (12), `test_sp_validation_gates.py` (18),
  `test_sp_validation_calibration.py` (4), `test_sp_validation_jobs.py` (4,
  real engine), `test_sp_api_validation.py` (6); fakes in
  `test_sp_validation_support.py`. `test_sp_api_strategies.py` /
  `test_sp_api_backtests.py` key-set assertions extended (chips, `kind`).
- **Statistics (choices)**: DSR = PSR(SR0) with per-period SR, skew and
  (non-excess) kurtosis; SR0 = expected max Sharpe of N trials, N = the
  family's finished trials + 1 (the running validation run); trial variance
  = sample variance of the trials' annualised Sharpes floored at V_min =
  1 / years of the training sample (Lo 2002 null variance; re-runs have no
  dispersion). PBO: S = 10 contiguous blocks (array_split), all 252
  combinations, IS winner's OOS average rank / (N + 1), PBO = share of
  logits ≤ 0; needs ≥ 4 configurations and ≥ 50 common days. Newey-West
  lag floor(4 (T/100)^(2/9)); G1's IC t-stat uses lag max(rule, h − 1)
  (overlapping h-day returns — stricter than the run card's naive t).
- **Folds**: K = 5 equal segments of the training run's simulated days;
  per-fold Sharpe, total return, max drawdown, mean IC at the best horizon
  counting only days whose forward window ends inside the fold.
- **Grid**: used parameters with min/max/step (plus their value), integer
  rounding, Cartesian product, > 64 → indices round(i (n−1)/63) with the
  base configuration forced in; parameters without `step` or flagged
  `unused_param` stay fixed. Neighbours: every varied parameter within ±20 %
  of its base value (base 0 → 20 % of max − min). A failed grid run → PBO
  not computed (G5 fail) / failed neighbour (G6 fail) — never a partial grid.
- **Gates**: exactly §9.4; mode `time_series` = fewer than 5 symbols (the
  metrics split) → G1 fold-Sharpe rule, G3 "Not applicable — single-symbol
  strategy …" (pass), H Sharpe only. G3: an IC that does not decay (fit
  λ ≤ 0) passes (unbounded half-life); holding = mean sessions entry→exit of
  FIFO closed lots. G6 (c) with no available episode holds vacuously (noted
  in `details.notes`). G3's "no decay" pass additionally needs a
  significantly positive 1-day IC (mean > 0, NW t ≥ `g1_ic_t`), else fail
  (review: flat noise ICs). G8 (user decision 2026-10-09): crypto-only →
  "Not applicable — No capacity measurement for crypto (user decision
  2026-10-03: no volume limit for crypto)" (pass, `details.not_applicable`);
  mixed universes → the estimate from the equity orders only
  (`metrics.capacity` skips crypto fills); no estimate otherwise (limit
  binding, too few orders) → fail. `passed` requires all of G1–G8 present
  and pass/exception (a missing gate → not passed). H: Sharpe > 0, ≥ 50 % of the training Sharpe (of the validation
  run used), mean IC > 0 at the validation's best horizon; constants, no
  override, no exception.
- **Settings**: `GET /v2/settings/gates` → `{preset, overrides, effective,
  updated_at, presets: {lenient|standard|strict: {key: value}}, ranges: {key:
  {min, max, integer}}}`; `PUT {preset, overrides?}` → same (400 `{error,
  errors: {field|"overrides.<key>": reason}}`). Keys: `g1_ic_t`,
  `g1_folds_min`, `g2_alpha_t`, `g4_dsr_min`, `g5_pbo_max`,
  `g6_neighbour_ratio`, `g6_cost_multiplier`, `g6_episode_dd_ratio`,
  `g7_min_trades`, `g8_capacity_multiple`. Stored in meta
  `validation/gate_settings`, audit `gate_settings_changed` {old, new}; none
  stored = `standard` (user default); unreadable → 503.
- **Jobs** on `backtest.jobs.manager()` through its public
  `JobManager.submit_work(create_card, work, *, label, generic_msg,
  cancelled_msg)` (review fix 2026-10-09; `submit` is now a thin caller —
  same outcome handling: queued-cancel, mark_running, BacktestCancelled →
  cancel, UserFacingError → fail(message), else traceback + generic
  message). `create_card` runs under the manager lock: the holdout job
  re-checks eligibility + the validation run there (no double open). Busy → `JobBusy` (409 "A backtest or
  validation run is already running." from the validation routes; POST
  /v2/backtests keeps its message). Progress phases: `data`, `simulate`,
  `metrics`, `folds`, `grid`, `stress`, `gates`, `saving`. Cancel via POST
  /v2/backtests/{id}/cancel or /v2/validations/{id}/cancel. Validation card:
  kind `validation`, request `{history_from, stress: true}` (others null),
  `metrics_json` = training `metrics.compute` output + `validation: {passed
  (RAW), version "sp-validation/4.1", report}`; `stress_json` = run_stress
  output (+ `costs_x3` when the G6 multiplier is 3). Holdout card: kind
  `holdout`, request {start, end = eligible window, benchmark/capital of the
  validation run}, `metrics_json.holdout = {passed, version, report}`;
  card and holdout event are written together by `runcard.finish_holdout`
  (one transaction, V1 review fix); a run that fails/cancels before its
  result records no event. Opening needs an
  eligible family AND a passing (with exceptions) latest done validation run
  of the current version (409 otherwise).
- **Report** (`report`, also `GET /v2/validations/{id}.report`): `{version,
  passed, passed_raw?, mode (cross_sectional|time_series|null), gates:
  [{gate, name, status (pass|fail|exception), value, threshold, explanation,
  details, exception?: {id, ts, reason, actor}}], folds: [{fold, start, end,
  days, returns, sharpe, total_return, max_drawdown, mean_ic, ic_days,
  horizon, reason}], grid: {axes, fixed, total, subsampled, max_configs,
  base_index, base, neighbours, configs: [{index, params, sharpe, reason,
  base}], sharpes, reasons, base_sharpe}, pbo: {value, reason, configs, rows,
  blocks, combinations, logits, overfit_combinations, mean_logit}, dsr:
  {value, reason, sharpe_annual, sr0_annual, n_trials, trial_variance,
  variance_source, trials_used, skew, kurtosis, returns}, stress, trial_count,
  thresholds: {preset, overrides, effective}, training: {range, sharpe,
  sharpe_reason, returns, periods_per_year, benchmark, alpha, holding}, ic:
  {best_horizon, horizons: [{horizon, value (NW t), mean, se, lag, n,
  reason}]}, holdout (eligibility snapshot), exceptions: [{id, ts, gate,
  reason, actor}]}`. Holdout report: `{version, passed, mode, gates: [H],
  window, range, validation_run_id, training_sharpe, eligibility, metrics}`.
  Exceptions: `db.validation_exception_add` (V1 table), audit
  `validation_exception` {run_id, gate, reason}; merged on read;
  `report.effective_passed(card)` is what the lifecycle calls.
- **Routes** (behind api_auth; 400 `{error, errors}`; unknown query keys 400):
  - `POST /v2/validations {strategy_id, version?, history_from?: "2016"|"2000"}`
    → 202 `{run_id}`; 404 `{error}`; 409 `{error, run_id}`; Tiingo key rule as
    POST /v2/backtests (503 SecretMissing).
  - `GET /v2/validations?strategy_id=&limit=` (1–200, default 50) → `{runs:
    [{id, kind (validation|holdout), created_at, started_at, finished_at,
    status, error, strategy_id, strategy_name, version, trial_number, request,
    range, key_metrics, passed (merged; null until done), exceptions (count;
    0 for holdout)}]}` newest first.
  - `GET /v2/validations/{id}` (validation AND holdout cards) → `{id, kind,
    status, created_at, started_at, finished_at, error, strategy_id,
    strategy_name, version, content_hash, trial_number, request, range,
    progress ({phase, percent, message, updated_at} while active, else null),
    key_metrics, passed, report (merged; null until done), warnings}`; 404
    `{error: "Validation run not found."}` (also for plain backtests).
  - `POST /v2/validations/{id}/exceptions {gate, reason}` → 200 = the detail
    above; 400 (reason < 30 chars, gate H, unknown gate, gate that did not
    fail) `{error, errors: {gate|reason: msg}}`; 404; 409 `{error}` (already
    excepted / run not done).
  - `POST /v2/validations/{id}/cancel` = backtest cancel semantics.
  - `GET /v2/strategies/{id}/holdout` → V1's `holdout_eligibility` keys +
    `{version, training_end, validation_run_id, validation_passed, can_open,
    blocked_reason}`.
  - `POST /v2/strategies/{id}/holdout {confirm}` → 202 `{run_id}`; wrong name
    → 400 `{error: "The confirmation does not match the strategy name.",
    errors: {confirm}}`; not eligible / no passing validation → 409 `{error,
    eligible_from}`; busy 409; 404.
  - `POST /v2/backtests` with start/end inside the window → 400 `{error,
    kind: "HoldoutWindowViolation", errors: {start|end: msg}, holdout_start}`.
  - `POST /v2/strategies/{id}/preview`: `days` 1..5, default 5 (was 1..250,
    default 20; review fix 2026-10-09) — the held-weight history reached
    into the locked holdout window without counting as a trial or raising
    seen_until (250 days of weights + public prices ≈ half the window's
    performance); now only the current signal row + at most the last 5
    sessions. `strategy.preview` keeps its own limit for internal callers.
  - `GET /v2/validations` reads list columns only (no full cards).
  - Strategy list rows and detail `strategy` add `family_id, contaminated,
    holdout_failed_until, trial_count` (V1 chips) and `has_exception` (latest
    done validation run of the current version has ≥ 1 exception); backtest
    list rows and detail add `kind`.
- **Calibration** (§9.6, stand-in engine, 10 symbols 2020–2024): planted
  AR(1) factor (φ 0.98) passes G1–G8 + H; of 40 noise seeds ≥ 95 % fail ≥ 1 gate
  (asserted); best-of-64 noise (64 trials) fails G4 or G5 on 3 seeds.
  A library entry's grid + report runs end-to-end; the real-engine pipeline
  (6 symbols, 4 configs) runs in `test_sp_validation_jobs.py` (~10 s).
