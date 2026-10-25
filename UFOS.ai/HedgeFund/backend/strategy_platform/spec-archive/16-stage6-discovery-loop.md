# Strategy Platform SPEC — archive: 16-stage6-discovery-loop

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 16. Stage 6 — discovery loop (contract, 2026-10-17)

Decisions: §15 (user, 2026-10-17) — default 6 variants × 4 generations (max
8 × 5); seed = existing strategy, idea text or confirmed paper; own **loop
AI budget**; switch **"fully autonomous"** (the loop validates its
finalists G1–G8 itself; off = finalists are proposed for the user's
approval); the **holdout is ALWAYS opened only by the user** (the loop never
opens it, never reads holdout runs or facts); **every backtest counts as a
trial**. Plan: project doc "L5 Validation & discovery loop" (population N ×
G generations → backtest → score → LLM diagnosis → refinement (LLM edits
the spec + bounded deterministic parameter search) → selection; budgets;
trial counting; resumable; live progress). Values marked **(confirmed by the user
2026-10-24)** were chosen by the implementing agent while the user was away and confirmed by the user on
2026-10-24.

### 16.1 Run model (`discovery/loop.py`)

- **One loop at a time per backend** (second start → 409 `busy`). A loop
  runs in its own daemon thread (the orchestrator); it never runs a backtest
  itself — every backtest/validation is a normal job on the ONE job slot
  (§8.5/§9.9, `JobManager.submit_work`).
- **Config** (`POST /v2/loops`): `seed` = `{kind: "strategy", strategy_id,
  version?}` | `{kind: "idea", text ≤ 4,000}` | `{kind: "paper", paper_id}`
  (the paper must be confirmed — §14.7 — and have a strategy);
  `variants` N 2..8 (default 6), `generations` G 1..5 (default 4),
  `search_points` P 0..3 (default 1, **confirmed by the user 2026-10-24**), `model`
  standard|premium (default standard), `history_from` "2016"|"2000"
  (default "2016"), `auto_resume` bool (default true). The "fully
  autonomous" switch and the budget come from Settings and are
  SNAPSHOT into the loop at its start (shown on the loop).
- **Family:** every variant lives in the SEED's strategy family, so all its
  backtests are trials of that family (no family bypass): variants are new
  strategies created like a duplicate (`parent` = the variant they derive
  from → `_family_for` follows the parent link; origin **`loop`**, state
  draft, envelope `ai_generated: true` + `ai_provenance {feature
  "sp-loop", model, model_tier, prompt_hash, loop_id, generation,
  variant_id}`). An **idea** seed first gets ONE Studio-style draft call
  (Studio prompt, `kind` + scope guard, validator, ≤ 1 repair round, topic
  check); its valid spec becomes the root strategy (origin `loop`, own new
  family). Invalid / off topic → the loop ends `failed` with the reason
  (no stand-in idea). A **strategy** seed may be any strategy incl. a
  library entry (variants are copies). A **paper** seed = the paper's
  confirmed strategy (current version).
- **Variant** = one (strategy_id, version) slot in a generation:
  `{id "lv-<12 hex>", generation 1..G, slot 1..N, origin seed|mutation|
  refined|child, parent_variant_id, strategy_id, versions: [{version,
  kind base|search, run_id, score}], chosen_version, state …}`. A refined
  variant is a NEW VERSION of its parent's strategy (lineage); a child of
  an elite is a NEW STRATEGY (duplicate-style, parent = the elite); search
  points (16.4) are further versions of the variant's strategy.
- **Generation 1:** slot 1 = the seed itself (its existing done backtest of
  that version is reused when the card is kind `backtest`, request all
  defaults and `range.end` = the current training end — no new trial;
  otherwise it is backtested); slots 2..N = **mutations** of the seed (one
  refinement call each, told which aspect to vary and given the specs
  already proposed in this generation — diversity).
- **Generation g ≥ 2:** built from the selection of g−1 (16.3): elites are
  carried over unchanged (no new backtest, same score); every "refine"
  variant gets diagnosis + refinement (a new version); every culled slot is
  refilled with a child of an elite (round-robin, best elite first:
  diagnosis of the elite + refinement into a new strategy).
- **Backtests:** request `{history_from}` (all other fields engine
  defaults — the range ends before the holdout window as every normal
  backtest, §9.1), `auto_resume` from the loop config. Every finished
  backtest is a trial of the family (existing counting, §9.2/§10.5).
- **Finalists** (after the last generation's selection): the best ≤ k
  variants (k as 16.3) that are not duplicates, have score > 0 and pass all
  pre-checks (16.2). None → loop `done` with "No finalist: no variant
  passed the pre-checks." (planted noise must end here). Each finalist is
  **promoted**: when its chosen version is not the strategy's current
  version, the chosen spec is added as a new version (no backtest — same
  logic hash as the evaluated version; noted).
- **Autonomous ON:** the loop submits a validation run (§9.3, G1–G8, NO
  holdout) for each finalist in order (same slot rules), records
  `{run_id, passed (merged)}`; the loop ends `done` when they finished.
  **Autonomous OFF:** finalists are `proposed`; the user approves
  (→ the loop submits the validation when the slot is free, even after the
  loop is `done`) or rejects (reason optional) each one. In both modes the
  loop NEVER calls the holdout path (`submit_holdout`), never changes a
  lifecycle state (variants stay drafts; the validation card is evidence
  for the user's own transitions, §9.5).
- **Statuses:** `running`, `waiting_slot`, `waiting_connection`, `paused`
  (by the user), `paused_budget` (day/month loop budget reached — resume
  after raising it / next day), `interrupted` (backend stopped), `done`,
  `failed`, `cancelled`. Terminal: done, failed, cancelled.
- **Resumable:** every completed step is persisted insert-only (16.7)
  BEFORE the next starts; the orchestrator is re-entrant and rebuilds its
  position from the stored events (a stored LLM answer, variant or score is
  never recomputed). Backend restart → `recover()` at startup marks
  running loops `interrupted`; with `auto_resume` they continue after
  startup automatically, else `POST …/resume`. A loop backtest that failed
  with a connection error (after the job's own auto-resume, §10.3) → loop
  status `waiting_connection` (probe ≥ 30 s via `data.network.probe`, no
  time limit, cancellable), then the run is resumed (`jobs.resume` when
  `resumable`, else a fresh submit — failed cards are never trials). An
  LLM call cut off by a crash is repeated on resume (costs again; its
  booked spend stays booked).
- **Cancel** (`POST …/cancel`): stops after the current step; a running
  loop backtest/validation is cancelled via `JobManager.cancel`; an LLM
  call in flight finishes (charged), its result is stored, then stop.
  **Pause** / **Resume** similarly.

### 16.2 Scoring (`discovery/scoring.py`, deterministic, no AI)

From the variant's done run card ONLY (`metrics` + `attribution`):
`score = sharpe − p_turnover − p_drawdown − p_costs − 0.25 × failed_checks`
- `sharpe` = `metrics.performance.sharpe` (net of the engine's costs);
  None → `score: null` + reason (culled).
- `p_turnover = min(1, 0.01 × max(0, turnover_annual − 12))`
  (`metrics.exposure.turnover_annual`, one-sided book turnover/yr;
  unavailable → 0 with a note).
- `p_drawdown = 2 × max(0, max_drawdown − 0.20)` (fraction).
- `p_costs = min(1, 0.5 × max(0, cost_share − 0.5))`, cost_share =
  `metrics.costs.pct_of_gross_pnl / 100` (only when gross P&L > 0).
- **Pre-checks** (gate-like, run-card only — NOT the gates; thresholds from
  the current gate settings §9.9): `trades` closed trades ≥ `g7_min_trades`;
  `signal` cross-sectional: best naive IC t-stat of horizons 1/5/10/21 ≥
  `g1_ic_t` (time-series: positive window share > 0.5); `alpha` annual
  alpha > 0; `capacity` estimate ≥ `g8_capacity_multiple` × capital
  (crypto-only: n/a = pass, as G8). A value that cannot be computed →
  the check fails with the reason.
- Constants in one place (**confirmed by the user 2026-10-24**). Holdout guard: the
  card must be kind `backtest` with `range.holdout.access` false and
  `range.end` < `range.holdout.start`, else `HoldoutDataRefused` (the loop
  fails — never happens on the normal path; tested).
- **Projected G4 impact** (shown on the loop): `trials_now` = family trial
  count, `planned_backtests` = remaining planned backtests of this loop,
  `sr0_now` / `sr0_projected` = `stats.expected_max_sharpe(n, V)` with
  V = sample variance of the family's trial Sharpes floored at V_min (as
  §9.9, sample = 1 year when unknown) — "the deflated-Sharpe gate will
  compare against an expected maximum Sharpe of X now → Y after this loop".

### 16.3 Selection + diversity (`discovery/selection.py`)

- Rank: score desc, Sharpe desc, slot asc; null scores last.
- **Diversity:** (a) a spec whose `db.logic_hash_of` equals any spec
  already evaluated in this loop is refused BEFORE its backtest (state
  `duplicate`, no trial); (b) daily-return correlation ≥ 0.95 (common
  valued days ≥ 60, from the run cards' equity curves) with a better-ranked
  variant of the same population → `duplicate` (not eligible as elite or
  finalist, culled).
- k = max(1, round(N/3)) elites (best non-duplicates with a score), c =
  max(1, ⌊N/3⌋) culled (failed / unscorable / duplicates first, then the
  worst); the rest are refined. N = 2 → 1 elite, 1 culled.

### 16.4 Bounded deterministic parameter search (`discovery/search.py`)

For every NEW LLM variant (mutation, refined, child): up to P extra
configurations: parameters used by the spec that have min/max/step
(`validation.grid` rules: integer rounding, unused parameters fixed), in
name order, candidates value − step, value + step (clamped, ≠ value), taken
in that order, skipping logic-hash duplicates; each = a new version + a
backtest (= a trial). The variant's `chosen_version` = best score (tie →
the base). Max backtests per loop: 1 + (N−1)(1+P) + (G−1)(N−k)(1+P),
hard cap 120.

### 16.5 LLM roles (`discovery/loop.py`, feature ids for the cost ledger)

- **diagnosis** (`sp-loop-diagnosis`, label "Hedge Fund · Discovery loop"):
  reads computed facts ONLY — the score card (16.2), key metrics, the §11
  fact table of the run (holdout ids filtered, ≤ 60 facts), the family's
  memory facts (§13.2) — never the spec text of other families, never
  holdout data. Output `{summary ≤ 600, weaknesses ≤ 5 × 200, ideas ≤ 3 ×
  200}`; numbers checked with the §11.2 number check (`check`, unverified
  shown, never removed).
- **refinement** (`sp-loop`): the Studio answer schema (`kind` draft only;
  clarify/off_topic → discarded), Studio scope guard, DSL/schema reference
  (single source of truth), the parent spec, its diagnosis and score card,
  diversity context (sibling specs' operators/parameters), memory; output
  spec → the Studio validator path (`registry.validate_spec`, ≤ 1 repair
  round), operator requests stored as text ("unverified AI suggestion",
  never executed). Temperature 0.4, max_tokens 4,000, 90 s.
- **Topic guard:** ONE independent topic check (§13.5, `topic_guard`) per
  variant over every displayable AI text (diagnosis summary/weaknesses/
  ideas, refinement reply/assumptions/operator requests, spec name/
  description/tags/parameter descriptions); failed → all texts discarded
  (`discarded_reason "topic_check_failed"`), the variant is `failed`
  (no backtest).
- Every AI text carries `provenance` (EU AI Act Art. 50 label) + cost;
  the loop payload has `ai_interaction_notice`.
- A failed / blocked / invalid AI step leaves that slot `failed` with the
  reason — no stand-in variant, no cheaper model, no retry with other text.

### 16.6 Loop AI budget (`discovery/budget.py`, key `loop`)

- Own budget, separate from fund / explanations / ideation: `monthly_usd`
  (default 10), `daily_usd` (default 2), `per_run_usd` (default 1)
  (**confirmed by the user 2026-10-24**), 0 = off, max 200. Checked BEFORE every
  call (all three; estimate as §11; in-flight estimates reserved),
  failed attempted calls charged, a failed spend write pauses the loop
  until restart (fail closed). Day/month reached → `paused_budget`;
  per-run cap reached → the loop stops creating variants, finishes the
  pending backtests and the selection, and ends `done` with the note
  "stopped early: per-run AI cap reached".
- Meta keys `loop_monthly_budget_usd`, `loop_daily_budget_usd`,
  `loop_run_cap_usd`, spend `loop_spend_month_usd:<YYYY-MM>`,
  `loop_spend_day_usd:<YYYY-MM-DD>`, `loop_spend_run_usd:<loop_id>` (booked
  in one transaction). `GET/PUT /v2/settings/loop` (budget + autonomous).
- **Central budget window (§15 B):** `budget.descriptor()` /
  `budget.apply(values)` and the hooks `ai_budget_entries` / `ai_budget_put`
  (`UFOS.ai/docs/ai-budgets-spec.md` §4.4, keys `loop`, `loop_month`,
  `loop_run`) expose the loop budget to the central `GET/PUT /ai/budgets`
  (details §16.12).

### 16.7 Storage (next free version) — insert-only

`loop_runs` (config, seed, created_at), `loop_variants` (one row
per generation slot; versions, run ids and scores come from events), `loop_events` (append-only log: every state
change, step result, score, selection, finalist decision, budget block,
pause/resume/cancel/interrupt, with JSON data), `loop_ai_messages` (role
diagnosis|refinement|seed, output, check, provenance, usage, cost,
discarded) — all insert-only by triggers; state = fold over the events.

### 16.8 Single-job slot sharing

- The loop submits ONE job at a time and only when (a) the slot is free,
  (b) no Studio draft or paper replication is waiting (their FIFO queues
  get a free slot first — `_after_job` dispatches them before the loop),
  and (c) ≥ `YIELD_SECONDS` (5 s) passed since its previous job ended —
  the window in which a manual Backtest / Validate click takes the slot.
- A manual request while a loop job runs → the existing 409 (body adds
  `loop_id` when the active job belongs to a loop). The loop shows
  `waiting_slot` while someone else's job runs. Pause frees the slot after
  the current job.

### 16.9 Memory

Every loop backtest/validation writes its memory facts through the
existing finish hook (§13.2). At the end the loop writes one deterministic
memory fact `loop_result` (counts, best score/Sharpe, finalists, cull
reasons) for the family (run_id = the loop id). Loop prompts use
`memory.select_for_prompt` (no holdout facts).

### 16.10 API (behind api_auth, strict validation, audited where state/cost)

`POST /v2/loops` → 202 loop; `GET /v2/loops?limit=`; `GET /v2/loops/{id}`
(`?events_after=` for polling); `POST /v2/loops/{id}/cancel|pause|resume`;
`POST /v2/loops/{id}/finalists/{variant_id}/approve|reject`;
`GET /v2/loops/estimate?…` (pre-start estimate: backtests, AI calls, USD,
projected trials/G4); `GET/PUT /v2/settings/loop`. Exact JSON in 16.x.

### 16.11 Tests (`tests/test_sp_loop*.py`, fake LLM transport, synthetic data)

Planted signal found + planted noise rejected (acceptance), budget blocks
(day/month/per-run, 0 = off), resume after interruption (no recomputed
step, no double trial), cancel, trial counting (every backtest one trial,
reused seed card none), diversity (logic-hash duplicate never backtested,
correlated duplicate culled), autonomous vs approval mode, no holdout data
(no holdout call, holdout-guarded scoring), topic guard on displayable
texts, route validation/auth.

### 16.12 Stage 6 backend notes (implemented 2026-10-17: §16.1–§16.11, backend part — for the WPF "Loop" view)

All routes behind api_auth; strict bodies/queries (unknown fields / parameters → 400 `{error: "invalid input",
errors: {field: message}}`); other errors `{error, code, …}` (as §11.6/§13.5), never a stack trace. Times UTC
ISO seconds. Every AI text carries `provenance` (EU AI Act Art. 50 label) + `cost_usd`; payloads carry
`ai_interaction_notice` (pin it at the top of the view).

- **Files**: `discovery/{__init__, loop.py (orchestrator, state fold, API functions), scoring.py, selection.py,
  search.py, budget.py, prompts.py (Diagnostician + Refiner prompts)}`; `research/loop.py` (hook module named by
  `ai_budgets.PROVIDER_MODULES`, re-exports `discovery.budget.ai_budget_entries/ai_budget_put`, no logic);
  `lifecycle/registry.py` (origin `loop`, `create_from_loop(spec, ai_provenance, parent)`,
  `new_loop_version(...)` — only for origin-`loop` strategies); `storage/db.py` (**v14**); `api/routes.py`
  (11 routes; busy 409 bodies of /v2/backtests, /v2/validations, holdout and resume add `loop_id` when the
  slot is held by a loop job); `backtest/jobs.py` (`_after_job` → `discovery.loop.on_slot_free()` after the
  Studio and paper queues); `research/memory.py` (kind `loop_result`, prompt run kind `loop`); `main.py`
  (`discovery.loop.recover()` at startup after the Studio dispatch).
- **Storage v14** (insert-only by triggers; generic helpers `db.s5b_insert/s5b_select`): `loop_runs(id "lr-<12
  hex>", created_at, seed_kind strategy|idea|paper, seed_json, config_json)`, `loop_variants(id "lv-<12 hex>",
  loop_id, generation, slot, origin seed|mutation|refined|child|elite, parent_variant_id, created_at,
  UNIQUE(loop_id, generation, slot))`, `loop_events(id, loop_id, event, generation, variant_id, data_json,
  created_at)`, `loop_ai_messages(id, loop_id, variant_id, role seed|diagnosis|refinement, output_json,
  check_json, provenance_json, usage_json, cost_usd, created_at)`. Meta: `loop_monthly_budget_usd`,
  `loop_daily_budget_usd`, `loop_run_cap_usd`, `loop_autonomous` ("0"/"1"), spend
  `loop_spend_month_usd:<YYYY-MM>`, `loop_spend_day_usd:<YYYY-MM-DD>`, `loop_spend_run_usd:<loop id>` (one
  transaction, `db.loop_spend_get/add`).
- **Implementation choices** (confirmed by the user 2026-10-24): the loop never adds versions to the user's strategies —
  refining the seed creates a new strategy (parent link → same family); a refined loop variant is a new version
  of its own loop strategy; a child of an elite is a new strategy. The AI must keep the seed's universe
  (else the variant fails "The loop keeps the seed's universe …"). Strategy names get the suffix
  ` (loop <6 hex> g<G>·<slot>)`. Mutation aspects cycle through 6 fixed texts (`prompts.MUTATION_ASPECTS`).
  Search default P = 1. The seed's existing backtest is reused when it is a done `backtest` card of the
  current engine version with no explicit range, benchmark / capital absent or equal to the defaults, the loop's
  history setting and `range.end` = today's training end — stress scenarios on it do not matter (no new trial;
  widened by the click-test fixes 2026-10-22). The topic check calls use the
  Studio guard's feature id `sp-studio-guard` in the cost log but are charged to the LOOP budget. Budget limits
  are re-read before every call (changes apply immediately). A crash between an LLM call and its storage
  repeats the call on resume (booked spend stays booked); a stored step is never recomputed.
- **Loop statuses** (`status`): persisted `running | paused | paused_budget | interrupted | done | failed |
  cancelled` (`persisted_status`), plus transient `waiting_slot | waiting_connection` while the orchestrator
  waits (only in `status`). `can: {cancel, pause, resume}` tells which buttons to enable.
- **Variant `state`**: `planned | ai | saved | backtesting | scored | failed | duplicate`; `origin` `seed |
  mutation | refined | child | elite` (elite = carried over, `carried_from` = the previous generation's
  variant, same strategy/run/score, no new backtest). **Finalist `state`**: `proposed | approved | rejected |
  validating | passed_gates | failed_gates | validation_failed | validation_cancelled |
  validation_start_failed` (renamed 2026-10-17 from `validated` / `validation_failed_gates`: `passed_gates` =
  the validation run passed G1–G8 — NOT the lifecycle state `validated`, which needs the holdout that only the
  user opens. Derived on read, never stored, so no migration. `eligible_state: "candidate" | null` tells the
  next lifecycle step the USER may take (backtested → candidate); the loop itself never changes a lifecycle
  state and never sets `validated`).

**POST `/v2/loops`** body `{seed: {kind: "strategy", strategy_id, version?: int} | {kind: "idea", text:
1..4000} | {kind: "paper", paper_id}, variants?: 2..8 (6), generations?: 1..5 (4), search_points?: 0..3 (1),
model?: "standard"|"premium", history_from?: "2016"|"2000", auto_resume?: bool (true)}` → **202** = the loop
payload (below). Errors: 404 `not_found` (strategy / version / paper), 409 `paper_not_confirmed`, 409 `busy`
(`{error, code, loop_id}` — one loop at a time), 402 `budget` (`{error, code, estimate_usd,
monthly_budget_usd, daily_cap_usd, per_run_cap_usd, spent_month_usd, spent_today_usd, spent_run_usd}`;
also when the per-run cap is smaller than one call), 503 `spend_unrecorded` / `no_model` /
`settings_invalid`. The "fully autonomous" switch and the budget limits are snapshot into `config`.

**GET `/v2/loops/{id}`** `?events_after=<event id>` (poll every 2 s; `events` then holds only newer events,
≤ 500) → 200 (404 `{error: "Discovery loop not found.", code: "not_found"}`):
```
{"id": "lr-e722df19bfc0", "created_at": "2026-10-17T18:33:49+00:00",
 "status": "running", "persisted_status": "running", "status_reason": null, "active": true,
 "seed": {"kind": "strategy", "strategy_id": "s-e5161912bd6f", "version": 1},      // idea: {"kind","text"}; paper: + paper_id
 "root_strategy_id": "s-e5161912bd6f", "family_id": "s-e5161912bd6f",
 "config": {"variants": 6, "generations": 4, "search_points": 1, "model": "standard", "history_from": "2016",
            "auto_resume": true, "autonomous": false},
 "progress": {"phase": "backtest", "message": "simulate: day 120/190", "updated_at": "…", "generation": 2,
              "variant_id": "lv-…", "slot": null, "run_id": "bt-…", "percent": 63.0} | null,
   // phase: seed | plan | ai | backtest | validation | waiting_slot | waiting_connection | selection
 "job_progress": {"phase", "percent", "message", "updated_at"} | null,         // §8.11 progress of the run
 "counts": {"backtests_started": 5, "backtests_planned": 35, "variants": 4, "failed": 0, "duplicates": 0,
            "ai_messages": 3},
 "trials": {"trials_now": 5, "planned_backtests": 30, "trials_projected": 35, "sr0_now": 1.6068,
            "sr0_projected": 2.6512, "trial_variance": 1.527273, "variance_source": "minimum"|"trials",
            "years": 0.655, "note": "The deflated-Sharpe gate (G4) compares …"} | {"unavailable": "<reason>"},
   // show: "Family trials: 5 now → 35 after this loop; G4 bar (expected max Sharpe) 1.61 → 2.65"
 "budget": {"spent_run_usd": 0.006 | null, "ai_messages_cost_usd": 0.006,
            "limits": {"monthly_usd": 10.0, "daily_usd": 2.0, "per_run_usd": 1.0} | {"unavailable": "…"},
            "paused_reason": null},
 "run_cap_reached": false,
 "generations": [{"generation": 1, "variants": [variant, …],
                  "selection": {"elites": ["lv-…"], "refine": ["lv-…"], "culled": ["lv-…"],
                                "duplicates": {"lv-…": {"of": "lv-…", "correlation": 0.97}}, "k": 2, "c": 2,
                                "reasons": {"lv-…": "culled: among the lowest scores of the generation"},
                                "ranking": ["lv-…", …]} | null}],
 "finalists": [finalist, …], "finalists_note": null | "No finalist: no variant passed the pre-checks.",
 "done_note": null | "stopped early: per-run AI cap reached", "notes": [str],
 "events": [{"id": 3, "event": "seed_ready", "generation": null, "variant_id": null, "data": {…}, "created_at": "…"}],
 "last_event_id": 36, "can": {"cancel": true, "pause": true, "resume": false},
 "ai_interaction_notice": "You are interacting with an AI system. The loop's diagnoses and strategy changes are AI-generated, not verified, and not investment advice.",
 "holdout_note": "The loop never opens the holdout: it is opened only by you, once per strategy family, from the strategy's validation report."}
```
`variant`:
```
{"id": "lv-943454fc7472", "generation": 2, "slot": 2, "origin": "child", "parent_variant_id": "lv-1960545fea0c",
 "carried_from": null, "strategy_id": "s-5dd67de28715", "strategy_name": "variant m3 (loop e722df g2·2)",
 "versions": [{"version": 1, "kind": "base", "parameter": null, "value": null, "run_id": "bt-f1d2c75a070e",
               "run_status": "done", "reused": false, "score": -0.432019, "error": null},
              {"version": 2, "kind": "search", "parameter": "lookback", "value": 5, "run_id": "bt-0ac996676240",
               "run_status": "done", "reused": false, "score": 0.513041, "error": null}],
 "chosen_version": 2, "run_id": "bt-0ac996676240", "state": "scored", "reason": null,
 "score": {"version": "sp-loop-score/1", "run_id": "bt-…", "score": 0.513041, "reason": null, "sharpe": 0.9275,
           "penalties": {"turnover": 0.164484, "drawdown": 0.0, "costs": 0.0, "checks": 0.25},
           "inputs": {"turnover_annual": 28.45, "max_drawdown": 0.0867, "cost_share_pct": 33.9,
                      "total_return": 0.0588, "cagr": 0.0912, "trades_closed": 95},
           "checks": [{"key": "trades"|"signal"|"alpha"|"capacity", "status": "pass"|"fail"|"n/a", "value": 95,
                       "threshold": 100, "explanation": "95 closed trades (pre-check needs ≥ 100)"}, …],
           "failed_checks": ["trades"], "checks_passed": false, "notes": [str],
           "range": {"start": "2024-02-09", "end": "2024-10-07"},
           "thresholds": {"g1_ic_t": 2.0, "g7_min_trades": 100, "g8_capacity_multiple": 10.0}} | null,
 "key_metrics": {"sharpe", "total_return", "cagr", "max_drawdown"} | null,
 "topic_check": "passed" | "failed" | "pending" | "skipped" | null,
 "diagnosis": ai_message | null, "refinement": ai_message | null}
```
`ai_message` (shown ONLY when `output` is not null; `pending: true` = written but not yet topic-checked → show
"checking…"; `discarded: true` → show the fixed text "The AI texts of this variant did not pass the topic check —
they are not shown." / the reason, never AI text):
```
{"id": 2, "role": "diagnosis"|"refinement", "pending": false, "discarded": false,
 "discarded_reason": null | "topic_check_failed" | "draft_invalid" | "bad_json" | "off_topic" | "not_a_draft" | …,
 "topic_check": "passed",
 "output": diagnosis {"summary", "weaknesses": [str], "ideas": [str]}
         | refinement {"kind": "draft", "reply", "assumptions": [str],
                       "operator_requests": [{"name", "purpose", "signature"}],   // label "unverified AI suggestion"
                       "spec": {…}, "valid": true, "validation": {"ok", "errors", "warnings"}, "repaired": bool,
                       "repair_errors": [..]|null, "task": "mutation"|"refined"|"child", "aspect": str|null,
                       "operator_requests_label": "unverified AI suggestion"}
         | draft_invalid: {"validation": {…}, "spec": {…}} (the validator card only) | null,
 "check": {"ok", "unverified": [{"text", "offset", "section": "summary"|"weaknesses.<i>"|"ideas.<i>"}],
           "unknown_ids": []} | null,                                   // diagnosis only: mark unverified numbers red
 "provenance": {"ai_generated": true, "generator": "UFOS.ai/HedgeFund/sp-loop-diagnosis", "provider": "OpenRouter",
                "model": "…", "generated_at": "…", "human_reviewed": false,
                "label": "AI-generated content (EU AI Act Art. 50)", "model_tier": "standard", "role": "diagnosis",
                "loop_id": "lr-…"},
 "usage": {"cost_usd", "estimate_usd", "prompt_tokens", "completion_tokens"}, "cost_usd": 0.002, "created_at": "…"}
```
`finalist`:
```
{"variant_id": "lv-b32a3469b74e", "strategy_id": "s-a7e489180aa8", "version": 2, "evaluated_version": 2,
 "promoted": false, "score": 0.948421, "sharpe": 1.1336, "run_id": "bt-a1a15f1163fd", "state": "proposed",
 "validation_run_id": null, "validation_status": null, "validation_error": null, "passed": null,
 "eligible_state": null, "reject_reason": null, "decided_at": null}
```
(`promoted: true` = the evaluated version was re-saved as the strategy's current `version` — same logic hash;
"Open validation report" → `GET /v2/validations/{validation_run_id}`; "Open holdout…" stays the Library's
own button — the loop view never offers it.)
Event names (`events[].event`): `created, running, seed_ready, generation_planned, ai_started, ai_message,
topic_checked, variant_saved, variant_failed, variant_duplicate, backtest_started, backtest_finished,
variant_scored, selection, run_cap_reached, note, finalists, promoted, approved, rejected, validation_started,
validation_finished, cancel_requested, paused, paused_budget, interrupted, cancelled, failed, done` (`data` per
event; `topic_checked.data` never contains the guard's reason).

**GET `/v2/loops`** `?limit=1..200 (50)` → `{loops: [{id, created_at, status, status_reason, seed: {kind,
strategy_id?, version?, paper_id?}, seed_text: str|null, root_strategy_id, family_id, config: {variants,
generations, search_points, model, autonomous}, generations_done, best_score: number|null, finalists: int,
backtests_started}], ai_interaction_notice}` (newest first).
**GET `/v2/loops/estimate`** `?strategy_id=&idea=true|false&variants=&generations=&search_points=&model=` →
`{backtests_planned: 35, ai_calls_planned: 46, estimate_per_call_usd: 0.019409, estimate_usd: 0.892791,
validations_if_autonomous: 2, trials: {…as above…}, budget: <descriptor>, note}` (show before "Start").
**POST `/v2/loops/{id}/cancel | pause | resume`** (no body) → 200 loop payload; 409 `not_running` ("The loop is
already <status>." / "The loop is not running."), `not_resumable` ("The loop is <status> — start a new loop."),
`running`, `busy`; 404. Cancel stops after the current step and cancels the loop's running job; pause lets the
running job finish (frees the slot); resume continues from the stored events (also after `paused_budget` once
the budget allows, and after `interrupted`).
**POST `/v2/loops/{id}/finalists/{variant_id}/approve`** (no body) → 200 loop payload (the validation is
submitted when the slot is free — `state` approved → validating); **…/reject** `{reason?: ≤ 500}` → 200. 409
`autonomous` (the loop validated itself), `decided` ("This finalist is already <state>."); 404 `not_found`
("Finalist not found.").
**GET `/v2/settings/loop`** → `{autonomous: false, budget: {key: "loop", module: "hedgefund", label: "Discovery
loop", cost_log_label, features: [...], limits: [{key: "daily_usd"|"monthly_usd"|"per_run_usd", label, value,
default, min: 0, max: 200, period: "day"|"month"|"run"}], spent: {today_usd, month_usd}, day, month, enabled,
zero_means: "off", paused_reason}, defaults: {variants: 6, generations: 4, search_points: 1}, limits: {variants:
[2, 8], generations: [1, 5], search_points: [0, 3], max_backtests: 120}, autonomous_note, holdout_note}`;
**PUT** `{autonomous?: bool, monthly_usd?, daily_usd?, per_run_usd?: 0..200}` (≥ 1 field) → same payload; audit
`loop_settings_changed {old, new}` / `loop_budget_changed {old, new}`. Settings UI: a toggle "Discovery loop:
fully autonomous (validates its finalists G1–G8 itself; never opens the holdout)" + the three limits (or leave
the limits to the central AI budget window).
- **Review fixes (2026-10-17) — JSON changes for the WPF agent:**
  (M3) a `draft_invalid` refinement returns `output: {"validation": {ok, errors, warnings}}` ONLY (no spec,
  so no AI-written name/description/tags/parameter descriptions; matches the Studio fix).
  (L6) an AI message whose variant failed before its topic check (e.g. the refinement failed after the
  diagnosis) is finalised: `pending: false`, `topic_check: "skipped"`, `discarded: true`,
  `discarded_reason: "not_topic_checked"`, `output: null` (its cost is still shown).
  (L7) `trials` (loop payload and estimate) adds `validations_planned` (k finalist validations in autonomous
  mode, else 0); `planned_backtests` stays the backtest count, `trials_projected` = trials_now +
  planned_backtests + validations_planned; holdout cards never enter `years` / the trial variance.
  (M1) a loop with a pending cancel becomes `cancelled` at the next backend start (never auto-resumed);
  resume of such a loop → 409 `not_resumable` ("The loop was cancelled — start a new loop.").
  (M4) a variant whose backtest cannot start / resume or whose strategy changed meanwhile is `failed` with the
  real reason (`variant_failed.data.code`: `start_failed`, `connection`, `conflict`) — the loop continues.
  (L5) a budget block during the topic check pauses the loop (`paused_budget`) or ends it early (per-run cap,
  the variant fails with code `run_cap`) instead of `topic_check_failed`.
  (M2) the loop budget books and checks the CURRENT UTC day/month on every call (midnight-safe).
  (L8) generation planning is one transaction (variant rows + `generation_planned` + the elites' carry-over
  `variant_scored` events); a crash cannot leave a half-planned generation.
- **Central AI budget window** (`GET/PUT /ai/budgets`, `UFOS.ai/docs/ai-budgets-spec.md` §4.4): three schema-1
  entries `loop` (period day), `loop_month` (month), `loop_run` (period **"run"** — per loop run; `period_id` =
  the running loop id or null, `spent_usd` = that loop's spend, 0 when none runs; the WPF window should label it
  "per loop run"), group "Strategy Platform", min 0, max 200, step 0.1, applies "immediately", `cost_log`
  `[{module: "HedgeFund", feature: "sp-loop" | "sp-loop-diagnosis" | "sp-loop-seed"}]`. `ai_budget_put(key,
  limit_usd)` = the same stored values as PUT /v2/settings/loop (audited `loop_budget_changed`).
- **Audit**: `loop_created`, `loop_cancel_requested`, `loop_pause_requested`, `loop_resumed`,
  `loop_finalist_approved`, `loop_finalist_rejected`, `loop_validation_started`, `loop_spend_unrecorded`, the
  Studio's `studio_call_blocked`/`studio_call_failed` rows for loop calls (detail `loop_id`), plus the normal
  strategy/backtest rows.
- **Tests** (all offline, fake `fund.llm.TRANSPORT`): `test_sp_loop_units.py` (20: scoring, holdout guard,
  selection/diversity, search, budget + hook, schemas), `test_sp_loop.py` (28 — incl. the review-fix tests M1–M4, L5–L8 and the passed_gates naming: full loop with trials/family/
  memory/payload, autonomous vs approval, reused seed card, logic duplicate, topic guard, off-topic/invalid,
  injection, budget off / day pause + resume / per-run cap, cancel, pause/resume, crash recovery, connection
  loss + resume chain = 1 trial, one loop at a time + 409 loop_id, routes, auth, insert-only),
  `test_sp_loop_acceptance.py` (2: planted signal found + planted noise rejected; noise only → "No finalist" —
  idea seed, real engine, ~75 s); support `test_sp_loop_support.py`.

### 16.13 Stage 6 WPF notes (implemented 2026-10-17: rail entry "Loop" — bound to §16.12)

Not built yet by the implementing agent (no dotnet on the VM): checked statically only — the user builds and
click-tests. Choices marked **(confirmed by the user 2026-10-24)** were taken while the user was away and confirmed
by the user on 2026-10-24 ("Das passt").

- **Files**: `Platform/LoopView.cs` (new, code-built UserControl), `Platform/LoopDialogs.cs` (new:
  `LoopStartDialog`, `LoopSettingsDialog`), `Platform/PlatformClient.Loops.cs` (new: DTOs exactly as §16.12 +
  `ListLoopsAsync`, `GetLoopAsync(id, events_after)`, `StartLoopAsync`, `EstimateLoopAsync`,
  `Cancel/Pause/ResumeLoopAsync`, `Approve/RejectLoopFinalistAsync`, `Get/PutLoopSettingsAsync`,
  `ReadConflictLoopId`, `ReadErrorCode`; shared texts `LoopText`), `HedgeFundWindow.Loop.cs` (new: lazy view,
  "Open in Library" reuses the Studio's handler), `HedgeFundWindow.xaml(.cs)` (rail entry "Loop" ↻ between
  Studio and Research, `LoopHost`, visibility switching, `Shutdown` on close), `Platform/StudioView.cs`
  (`SpecDiff` private → internal, reused for the refinement diffs). Computed DTO accessors are `[JsonIgnore]`
  (`LoopAiMessage.Id`); blocks whose shape varies (`progress`, `job_progress`, `counts`, `trials` incl.
  `{unavailable}`, `budget`, `score`, `key_metrics`, `selection`, AI `output`, `check`, `provenance`, `usage`,
  event `data`, settings `budget`/`limits`) stay `JsonElement` and are read by name (`BtJson`).
- **Layout**: left = runs (GET /v2/loops?limit=50: seed label — strategy name from GET /v2/strategies when
  known, else the id; idea text truncated; paper id —, status, created, N×G, generations done, best score,
  finalists, "autonomous"), "✦ New loop…" (Ctrl+N), "Settings…", ↻ (F5). Right = the backend's pinned
  `ai_interaction_notice` (StudioView.NoticeBlock, not dismissible), status area (re-rendered every poll) and the
  scrolling body (re-rendered only when a signature of last_event_id / variant states / run statuses /
  finalist states changed; expander states and the scroll offset survive a re-render).
- **Status area**: seed title + status chip (live region); Pause / Resume (✦, AI tooltip) / Cancel loop
  (confirmation) exactly as `can` allows; ids line (loop, created, root strategy, family); config line incl.
  "fully autonomous ON/OFF (snapshot at start)"; a fixed explanation per status — `waiting_slot` ("another job …
  continues automatically"), `waiting_connection` (probe ≥ 30 s, no limit, one trial), `paused`, `paused_budget`
  (raise it in Settings… / central window or next UTC day, then Resume), `interrupted` (auto-resume on → continues
  after backend start, else Resume; completed steps never recomputed, a cut-off AI call is repeated and costs
  again), `failed` + `status_reason`, `cancelled`, `done`; step line from `progress` (phase label, generation /
  variant "g2·3", percent, message, run id) and job line from `job_progress` (waiting phase → "connection lost —
  resumes automatically"); counts line; budget line (`spent_run_usd` of the per-run cap, `ai_messages_cost_usd`,
  day / month limits or `limits.unavailable`, `paused_reason`); trials line in amber ("Family trials: n now → m
  after this loop; G4 bar (expected max Sharpe) a → b", `unavailable` reason verbatim, `note` as tooltip);
  `run_cap_reached`, `done_note`, `notes`.
- **Connection loss / crash**: a failed GET keeps the last payload, shows "Connection to the backend lost
  (attempt n): <message>" and keeps polling every 2 s; once the backend answers again the body re-renders
  (an interrupted loop then shows its resume state). 404 → the backend's text. Polling: every 2 s only while the
  view is visible and the loop is not terminal / `active`, or a finalist is `approved` / `validating`, or the
  connection is lost; `events_after` = last seen event id (events accumulated locally, ≤ 500 kept); the run list
  is refreshed on every status change and every 10th poll.
- **Finalists panel** (always first in the body): mode line (autonomous: "the loop validates its finalists itself
  (G1–G8, no holdout)"; approval: "approve → gate validation; a finalist that passes is your candidate proposal — move it
  to Candidate yourself in the Library; the loop never changes a lifecycle state"); per finalist: g·slot + strategy
  name, state chip, strategy id / version (evaluated version), score, Sharpe, run, `promoted` note, a fixed line
  per state (proposed / approved "starts when the slot is free" / validating + run / `passed_gates` = "Passed G1–G8 → eligible for candidate" + the backend's `eligible_state`
  ("eligible for Candidate: move it there yourself in the Library") — never "Validated": lifecycle `validated`
  needs the holdout, which only the user opens in the Library (§9.5) / `failed_gates` "Failed the gates" / validation failed / cancelled /
  could not start + `validation_error` / discarded + `reject_reason`), `decided_at`. Buttons: approval mode +
  `proposed` → "Approve → validate…" (confirmation: validation run, no AI, no holdout, no lifecycle change) and
  "Discard…" (form, reason optional ≤ 500, checked locally); always "Open report" (BacktestReportWindow),
  "Open validation report" (ValidationReportWindow, when `validation_run_id`), "Open in Library". No holdout
  button anywhere (`holdout_note` shown at the end of the body). `finalists_note` (e.g. "No finalist: …") shown
  verbatim in amber.
- **Grid** "Generations × variants": rows G1…, columns slots 1…N; each cell a focusable button "#slot glyph
  score|state" (★ kept/elite, ↻ refined next, ✕ culled, ≡ duplicate, ! failed, • scored awaiting selection, …
  in progress — glyph + text, never colour alone; tooltip + automation name with the full summary); Enter /
  click opens that variant's expander and scrolls it into view.
- **Generations**: one expander per generation (latest open by default; header: counts kept / refined /
  culled or "selection pending"; k / c explanation). Per variant an expander (header "g2·3 · origin · score ·
  Sharpe" + state chip + marker chip): strategy name + id, origin sentence (seed / mutation + `aspect` / refined
  from / child of elite / elite carried from — no new backtest), `reason`, selection `reasons[id]`, duplicate
  `of` + correlation, mini metrics (`key_metrics`), versions (v, base / search parameter = value, run + status,
  score, chosen, reused = no new trial, error, "Report" per run), score card (score = Sharpe − turnover −
  drawdown − costs − checks, inputs, pre-checks with ✓/✕/– text, checks_passed, range "training data, before the
  holdout", notes — labelled "deterministic … no AI"), then the AI messages, "Open report" / "Open in Library".
- **AI messages** (`diagnosis`, `refinement`; plain text only): `output` shown ONLY when not null. Pending →
  violet "✦ AI-GENERATED · <role> · checking… (independent topic check pending)" + cost line, no text.
  Discarded / topic check failed → neutral system note with the fixed text "The AI texts of this variant did not
  pass the topic check — they are not shown." (other reasons: "AI <role> discarded (<reason label>) — not shown")
  + cost line; `draft_invalid` with an output → neutral box with ONLY the validator findings. Shown → violet AI
  container: badge `AiContent.Badge(provenance.model, provenance.generated_at)` + role BEFORE the text, cost
  line (tier from `provenance.model_tier`, `cost_usd`, tokens in/out, `estimate_usd`, topic check, "loop AI
  budget"), `AiContent.Caveat` AFTER, screen-reader name `AiContent.AutomationName`. Diagnosis: summary,
  weaknesses, ideas with the `check.unverified` numbers as red underlined runs where the text at `offset` of that
  `section` matches (count + list below; `unknown_ids` listed). Refinement: task + fixed mutation aspect,
  reply, repair round note, validator result (errors / warnings), `repair_errors` expander, operator requests in
  the amber box labelled with `operator_requests_label` ("unverified AI suggestion", text only, never executed),
  assumptions, "Spec changes: v<base> vs parent g·slot v<chosen>" (lazy: GET both stored versions, cached;
  client-side flattened JSON diff via StudioView.SpecDiff, ≤ 60 lines; a variant without a stored version is
  compared as "proposed spec (not saved)"), "Proposed spec (JSON, as returned by the AI)" expander.
- **Activity**: expander with the last 200 events (newest first): time, event, generation, variant label and a
  whitelist of scalar `data` fields (message, reason, note, text, role, kind, version, run_id, status, parameter,
  value, cost_usd, passed, score.score) — no AI text (events carry none).
- **Start dialog** (`LoopStartDialog`, opened after GET /v2/strategies, /v2/papers?limit=50 and
  /v2/settings/loop): seed radios "Library strategy" (filterable list of all strategies incl. alpha-library
  entries — name, state, current version, origin, trials; optional version ≥ 1) | "Idea in words" (TextBox ≤ 4,000
  + counter; note that an invalid / off-topic draft fails the loop) | "Confirmed paper" (papers with status
  `confirmed` and a `strategy_id`; none → "confirm one in Studio → Paper first"); radio rows for variants /
  generations / search points from the settings' `limits` and `defaults` (contract values 2–8/6, 1–5/4, 0–3/1
  only while the settings did not load — said so in the dialog); Standard / Premium; "Load data from 2000"
  (history_from); "Continue automatically after a backend restart or a lost connection" (auto_resume, default
  on); the "fully autonomous" state from Settings (ON / OFF / not known) + "snapshot at the start" + the
  backend's `holdout_note`. Estimate (debounced 400 ms on every change, GET /v2/loops/estimate with the seed's
  strategy id — the paper's `strategy_id` for a paper, `idea=true` without an id for an idea): "Planned: n
  backtests (each one a trial of the seed's family) · m AI calls · estimated $x (≈ $y per call, tier)" (+ "up to k
  validation runs" when autonomous), the loop budget line (per run, today, month, off, paused), warnings when the
  loop AI is off, when the estimate exceeds the per-run cap ("stopped early …") or today's remainder ("may
  pause …"), and the G4 box in amber ("⚠ Family trials: … ; G4 bar …" + why more trials raise the bar + the
  backend's notes). "✦ Start loop" (Ctrl+Enter; no default button so Enter in a list does not start a costly run)
  sends POST /v2/loops from the dialog: success or 409 busy with `loop_id` closes it (busy → "Show the running
  loop?"); every other refusal (400, 402 budget, 404, 409 paper_not_confirmed, 503) stays in the dialog verbatim.
- **Settings dialog** (`LoopSettingsDialog`, Loop → "Settings…" — **confirmed by the user 2026-10-24**: the loop settings
  live in the Loop view, not in the Hedge Fund settings panel): toggle "Discovery loop: fully autonomous
  (validates its finalists G1–G8 itself; never opens the holdout)" — turning it ON asks for an explicit
  confirmation (danger dialog: finalists validated without asking, holdout ALWAYS manual, no lifecycle change,
  running loops keep their snapshot), OFF is saved at once; a refusal restores the stored state. Three limit
  inputs (labels from the descriptor; empty = not sent; 0..`max` (else 200), 0 = off) + "Save limits"
  (PUT with only the filled fields), the spend line, a note that the same limits are in the central AI budget
  window ("per loop run"), and the backend's defaults / ranges / max backtests (read-only: the backend has no
  PUT for defaults). Inputs stay disabled until GET answered; a 503 enables them (saving repairs invalid values).
- **Review fixes of §16.12 handled**: finalist states `passed_gates` / `failed_gates` + `eligible_state`
  (`LoopFinalist.EligibleState`, shown as "eligible for Candidate — move it yourself"; nothing transitions), M3 (draft_invalid → validator findings only), L6 (`not_topic_checked` →
  "the variant failed before its topic check", cost shown), L7 (`validations_planned` → "(incl. k finalist
  validations)" in the trials line), M1 (409 `not_resumable` shown verbatim), M4/L5 (variant `reason` shown;
  `code` in the activity log).
- **Keyboard / accessibility**: F5 reload, Ctrl+N new loop, Tab through list, actions, grid cells and
  expanders; Esc closes the dialogs; every button, list, row and AI block has an `AutomationProperties.Name`
  (AI blocks start with "AI-generated"), status / estimate / errors are live regions.
