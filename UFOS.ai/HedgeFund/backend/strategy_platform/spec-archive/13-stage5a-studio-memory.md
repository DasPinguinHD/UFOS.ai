# Strategy Platform SPEC — archive: 13-stage5a-studio-memory

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 13. Stage 5a — Strategy Studio + memory (contract, 2026-10-17)

Interview decisions (user, 2026-10-17): stage 5 in two sub-stages — **5a
Strategy Studio + memory** now, 5b paper → strategy + investment committee
later (5b: papers by PDF upload, pasted text AND open-access links (arXiv /
SSRN, https, size limit); committee = new 5-role panel reusing the Council
of AIs' AI plumbing). **Own ideation AI budget: $10 per month, max $2 per
day** (shared by 5a and 5b, separate from the fund's and the explanations'
budgets). **Every valid AI draft is backtested automatically** (user chose
this knowingly: each backtest counts as a trial → stricter G4 later; the
Studio shows the family's trial count next to every draft). **Memory = only
computed facts** (no AI-written lessons, no holdout results). The AI may use
**only the DSL** (56 validated operators); it may additionally **propose
missing operators as text** (never executed, shown as "operator request").

### 13.1 Backend (`strategy_platform/research/studio.py`, `memory.py`)

- Conversation model: `studio_sessions(id, strategy_id, created_at, title)`,
  `studio_messages(id, session_id, role user|assistant|system_note, text,
  spec_version, run_id, operator_requests_json, provenance_json, usage_json,
  cost_usd, created_at)` — insert-only. One session = one strategy family
  (the first valid draft creates a strategy `origin="studio"`, state draft,
  `ai_generated: true` in the envelope; follow-ups create new versions).
- `POST /v2/studio/sessions` {title?} → session; `POST /v2/studio/sessions/{id}/messages`
  {text ≤ 4,000 chars, model: standard|premium} → assistant message (sync,
  LLM timeout 90 s); `GET /v2/studio/sessions`, `GET …/{id}` (messages +
  versions + runs + trial count); `GET/PUT /v2/settings/ideation`
  {monthly_budget_usd (default 10, 0 = off, max 200), daily_cap_usd
  (default 2)} + spent_month/spent_today (audited).
- LLM via `fund/llm.chat_json` (feature `sp-studio`, label "Hedge Fund ·
  Strategy Studio"), budget check BEFORE the call (month AND day; estimate
  as in §11), failed attempted calls charged; no stand-in text.
- Prompt: role "Researcher"; input = user text (untrusted, quoted), the
  DSL reference (operators with signatures and descriptions, generated from
  the validator's registry — single source of truth), universe presets,
  the current spec (follow-ups), the last run's facts (§11 fact table
  summary) and the MEMORY block (13.2). Output JSON: {reply ≤ 2,000 chars,
  spec (StrategySpec JSON) | null, operator_requests: [{name, purpose,
  signature}] ≤ 5, assumptions: [str] ≤ 5}. Spec → existing validator;
  invalid → one automatic repair round with the validator's errors (counts
  against the budget), still invalid → shown with errors, no backtest.
- Valid spec → new version (provenance: model, prompt hash, session id,
  ai_generated) → automatic backtest job (§8.5, default range, ends before
  the holdout window; the single-job rule applies: if a job is running, the
  draft is queued as "waiting for the backtest slot" — visible, cancellable;
  queue length ≤ 3 per session) → the message gets run_id; the report and
  §11 attribution are available as usual. Trial counting unchanged (every
  finished run counts).
- Operator requests are stored and listed (`GET /v2/studio/operator-requests`)
  — text only; never executed; marked "unverified AI suggestion".
- No holdout data anywhere in Studio prompts (assert: facts with holdout
  ids are filtered, as in §11.2).

### 13.2 Memory (computed facts only)

- `memory_facts(id, family_id, strategy_id, version, run_id, kind, fact_json,
  created_at)` insert-only, written automatically when a backtest or
  validation run finishes (NOT holdout runs): which gates failed with
  values/thresholds, Sharpe/alpha t/cost share of gross P&L, turnover,
  dominant periods/regimes and top contributors from the §11 attribution,
  parameter-grid flatness (from validations), warnings (survivorship,
  participation cuts).
- Memory block for the Studio prompt: the current family's facts + the
  most relevant facts of other families with overlapping universe/operators
  (deterministic ranking: same universe preset, shared operators, recency;
  max 20 facts, ≤ 6,000 chars). Shown to the user in the Studio ("Memory
  used for this draft") — no hidden context.
- `GET /v2/memory?family_id=&strategy_id=` read-only list.

### 13.3 WPF

- Rail entry **"Studio"** (after Library, before Research): session list,
  chat view (user/assistant bubbles; assistant replies with the EU AI Act
  header "✦ AI-generated · <model> · <time>" + cost line), Standard/Premium
  toggle with estimated cost, budget line (month/day spent), draft cards
  (validator result, spec diff vs previous version, "operator requests"
  box, assumptions, trial count of the family, backtest status → mini
  metrics + "Open report"), "Memory used" expander, "Open in Library".
  Plain text only. Settings: ideation budget fields.

### 13.4 Tests
No network (TRANSPORT fake): budget month/day blocks + 0 = off; repair round;
invalid spec never backtested; auto-backtest queueing + single-job rule;
operator requests stored, never executed; memory facts written on finish
(not for holdout), ranking deterministic, size cap; no holdout facts in
prompts; prompt-injection text in the user message stays quoted; insert-only;
routes validation/auth.

### 13.5 5a backend notes (X2, implemented 2026-10-17: §13.1, §13.2, §13.4 backend)

All routes behind api_auth; strict bodies (unknown fields / query parameters
→ 400 `{error, errors: {field: message}}`); other errors `{error, code, …}`
(same shape as §11.6), never a stack trace. Times UTC ISO seconds.

- **POST `/v2/studio/sessions`** `{title?: str ≤ 120}` → 200 `session`:
  `{id: "ss-<12 hex>", title: str|null, created_at, strategy_id: str|null,
  family_id: str|null, trial_count: int, message_count: int,
  last_message_at: str|null}`. Audit `studio_session_created`.
- **GET `/v2/studio/sessions`** `?limit=1..200 (50)` → `{sessions:
  [session] (newest first)}`.
- **GET `/v2/studio/sessions/{id}`** → `{session, messages: [message]
  (oldest first), versions: [{version, created_at, content_hash,
  message_id}], runs: [{run_id, version, kind, status, created_at,
  finished_at, key_metrics|null}], trial_count, queue: [draft_backtest]
  (waiting drafts, FIFO), estimate_usd: {standard, premium} (pre-call
  estimate of the next message with the current context and a 4,000-char
  text), budget: <ideation settings payload>}`. 404 `not_found`
  "Studio session not found.".
- **POST `/v2/studio/sessions/{id}/messages`** `{text: str 1..4,000,
  model: "standard"|"premium"}` → 200 `{user_message: message,
  assistant_message: message, notes: [message]}` (sync; one Studio call at
  a time per backend; LLM timeout 90 s per call; at most 2 calls = the
  answer + one repair round). Nothing is stored when the call fails.
  Errors: 402 `budget` + `estimate_usd, monthly_budget_usd,
  spent_month_usd, daily_cap_usd, spent_today_usd` ("AI ideation is
  switched off (monthly budget is 0) — raise it in Settings." / "Monthly
  ideation budget reached — raise it in Settings." / "Daily ideation cap
  reached — try again tomorrow (UTC) or raise it in Settings." / spend
  unreadable); 503 `spend_unrecorded` ("Ideation spend could not be
  recorded — the Studio is paused until the backend restarts."); 409
  `busy` ("The Studio is already answering — wait until it is finished."),
  `prompt_too_large`; 502 `bad_json` / `llm_error`, 504 `timeout` (all
  three with `cost_usd` booked); 503 `no_key`, `no_model`.
- **message** = `{id, session_id, role: "user"|"assistant"|"system_note",
  text, created_at, spec_version: int|null, run_id: str|null,
  operator_requests: [{name, purpose, signature}], assumptions: [str],
  draft: draft|null, memory_used: [memory_fact_short], model: str|null,
  model_tier: "standard"|"premium"|null, provenance: {…EU AI Act label…,
  "model_tier", "prompt_hash", "session_id"}|null, usage: {cost_usd,
  prompt_tokens, completion_tokens, calls, estimate_usd}|null, cost_usd:
  number|null}`. User messages: text only. `system_note`: backend notes
  (queue full, backtest could not start, cancelled) — plain text, no AI.
- **draft** (assistant messages that carried a spec) = `{spec: <the
  proposed StrategySpec as returned by the AI (after repair)>, valid: bool,
  validation: {ok, errors: [{path, code, message, line?, column?}],
  warnings}, repaired: bool, repair_errors: [error]|null (errors of the
  first answer when a repair round ran), repair_skipped: str|null (e.g. the
  budget message), strategy_id: str|null, version: int|null, unchanged:
  bool, backtest: draft_backtest|null, requires_confirmation: str|null,
  not_saved: bool, not_saved_reason: str|null, saved_by_confirmation: bool,
  base_version: int|null}` — invalid drafts get no version and no backtest.
  **Review fix M1 (2026-10-17):** a valid follow-up draft is saved and
  backtested automatically ONLY while the strategy's state is `idea`,
  `draft` or `backtested`; otherwise it stays unsaved with
  `requires_confirmation: "demotes <state> to draft"` (e.g. "demotes
  validated to draft") until the user confirms (route below). `not_saved:
  true` + `not_saved_reason` = a valid draft that could not be saved (e.g.
  the strategy changed meanwhile — `valid` stays true).
- **draft_backtest** = `{message_id, state: "waiting"|"started"|"cancelled"|
  "start_failed"|"not_queued", position: int|null (1-based, waiting only),
  run_id: str|null, run_status: "queued"|"running"|"waiting"|"done"|
  "failed"|"cancelled"|null, key_metrics: {total_return, cagr, sharpe,
  max_drawdown, volatility}|null, message: str|null}` — "waiting" =
  "Waiting for the backtest slot" (another job is running; ≤ 3 waiting
  drafts per session, a 4th → `not_queued` "Backtest queue full (3 drafts
  waiting) — start this version from the Library."). The queue is
  persisted and dispatched FIFO when the job slot frees (and on every
  Studio request).
- **POST `/v2/studio/sessions/{id}/drafts/{message_id}/cancel`** (no body)
  → 200 `draft_backtest` (state cancelled); 409 `not_waiting` "This draft
  is not waiting for the backtest slot." (a started run is cancelled via
  `POST /v2/backtests/{run_id}/cancel`).
- **POST `/v2/studio/sessions/{id}/drafts/{message_id}/save`** `{confirm:
  true}` (exactly) → 200 `{draft, notes: [message]}` — saves the draft that
  `requires_confirmation` as a new version (demoting the strategy to draft,
  audited `studio_draft_saved`), then starts/queues its backtest like an
  automatic draft. Errors: 404 `not_found`; 409 `not_confirmable` ("This
  draft does not need a confirmation." — includes already saved / invalid
  drafts), 409 `stale` ("The strategy was changed meanwhile (current version
  N) — ask the Studio for a new draft."); 400 when `confirm` is not true.
- **GET `/v2/studio/operator-requests`** `?session_id=` (optional) →
  `{operator_requests: [{id, session_id, message_id, name, purpose,
  signature, created_at, label: "unverified AI suggestion"}]}` (newest
  first) — text only, never executed.
- **GET `/v2/settings/ideation`** → `{monthly_budget_usd (default 10),
  daily_cap_usd (default 2), spent_month_usd, spent_today_usd,
  remaining_month_usd, remaining_today_usd, month: "YYYY-MM", day, enabled,
  default_monthly_budget_usd: 10.0, default_daily_cap_usd: 2.0,
  max_monthly_budget_usd: 200.0, max_daily_cap_usd: 200.0,
  paused_reason: str|null}`; **PUT** `{monthly_budget_usd, daily_cap_usd}`
  (both required, numbers 0..200; 0 = off) → same payload, audited
  `ideation_settings_changed {old, new}`.
- **GET `/v2/memory`** `?family_id=&strategy_id=&limit=1..500 (200)` →
  `{facts: [memory_fact] (newest first)}`; `memory_fact` = `{id, family_id,
  strategy_id, version, run_id, kind, fact: {text, data, universe_key,
  operators, run_kind, range}, created_at}`; `memory_fact_short` (in
  messages) = `{id, family_id, kind, text, run_id}`.
- **Implementation**: `research/studio.py` (sessions, messages, budget,
  queue, routes' logic), `research/prompts.py` (fixed `SYSTEM_PROMPT`
  "Researcher", `REPAIR_INSTRUCTION`, DSL reference = `dsl.catalogue()` +
  `dsl.FIELDS` + `validator.ext_catalogue_default()` (single source of
  truth), schema reference from `strategy.schema` constants + an example,
  universe presets, answer schema check), `research/memory.py`. LLM:
  `fund.llm.chat_json(feature "sp-studio", label "Hedge Fund · Strategy
  Studio")`, temperature 0.3, max_tokens 4,000, 90 s, model =
  `fund.llm.models()[tier]` (no default); prompt cap 80,000 chars (409
  `prompt_too_large`). Data message: `{user_request (verbatim, quoted JSON
  string), conversation_so_far (last 10 turns, ≤ 2,000 chars each),
  current_spec, last_run {run_id, kind, version, range, key_metrics,
  facts [{id, label, display}] (≤ 60 / 6,000 chars, holdout ids removed),
  omitted}, memory [{id, kind, family, text}], universe_presets, schema,
  dsl}`. The AI's `provenance` key in a spec is dropped (an AI claim is not
  provenance). Answer schema: exactly `reply` (1–2,000), `spec` (object |
  null), optional `operator_requests` (≤ 5, {name 1–400, purpose ≤ 400,
  signature ≤ 400}), optional `assumptions` (≤ 5, 1–400 each); anything
  else → `bad_json`. Repair round only when the spec is invalid; a repair
  blocked by the budget → `draft.repair_skipped` = the budget message (no
  error, the invalid draft is shown).
- **Registry** (`lifecycle/registry.py`): origin `studio` added to
  `ORIGINS`; `create_from_studio(spec, ai_provenance)` and
  `new_studio_version(strategy_id, spec, base_version, ai_provenance)`
  (shared `_add_version`); envelope key `ai_provenance {feature, model,
  model_tier, prompt_hash, session_id, generated_at}` + `ai_generated:
  true`. Family / trial rules unchanged.
- **Storage v11**: `studio_sessions(id, created_at, title, strategy_id)`
  (insert-only; `strategy_id` set ONCE by the first valid draft),
  `studio_messages(… §13.1 columns …, detail_json {draft, assumptions,
  memory_used})`, `studio_backtests(id, session_id, message_id, strategy_id,
  version, event waiting|started|cancelled|start_failed|not_queued, run_id,
  note, created_at)` (the queue as an insert-only event log; a draft's
  state = its latest event; the message's `run_id` in the API = the
  `started` event's run id — the message row itself stays insert-only),
  `memory_facts(§13.2 columns)`; all insert-only by triggers. Meta:
  `ideation_monthly_budget_usd`, `ideation_daily_cap_usd`,
  `ideation_spend_month_usd:<YYYY-MM>`, `ideation_spend_day_usd:<YYYY-MM-DD>`
  (both counters booked in one transaction).
- **Jobs hook** (`backtest/jobs.py` `_after_job`, after every job, never
  raises): `memory.on_run_finished(run_id)` then `studio.on_slot_free()`
  (FIFO dispatch of waiting drafts). Automatic backtests use
  `JobManager.submit(strategy_id, version, {})` = the default range, which
  ends before the holdout window; failures to start (e.g. a missing key)
  → `start_failed` + a system note.
- **Memory kinds**: `metrics` (Sharpe, total return, CAGR, max drawdown,
  alpha, beta, cost share of gross P&L, turnover, closed trades),
  `contributors` (top/bottom 3 symbols), `periods` (best/worst 63-session
  window), `regimes` (best/worst regime), `warning` (code + message, ≤ 6),
  validations also `gate_failed` (gate, name, value, threshold — G2 value
  = alpha t), `validation_result`, `grid_flatness` (val.grid.* facts).
  Fact text is formatted deterministically (no AI). Selection: own family
  newest first (≤ 12), then other families by (same universe key +4,
  shared operators +1 each ≤ 3, newer first), others only when related
  (score > 0) unless the session has no spec yet; ≤ 20 facts, ≤ 6,000 chars.
- **Review fixes (2026-10-17)**: M1 auto-save/backtest only in states
  `idea|draft|backtested` (else `requires_confirmation` + the save route;
  storage **v12** `studio_draft_saves(id, session_id, message_id UNIQUE,
  strategy_id, version, created_at)` insert-only records confirmed saves —
  the message view merges them: `draft.version`, `spec_version`,
  `saved_by_confirmation`); L2 `dispatch_queue` keeps a "dirty" flag so a
  wake-up that cannot take the lock makes the holder run another round,
  and `main.py` dispatches once at startup after `recover_interrupted`; L3
  the budget check blocks while the Studio is paused and counts
  max(stored spend, stored at start + this request's bookings); L4 repair
  prompts are capped at 80,000 chars (`prompts.capped_repair`: drop the
  conversation, then the memory, then the last-run facts, then cut the
  quoted previous answer 20,000 → 8,000 → 2,000; validator errors are
  never trimmed; still too large → `repair_skipped` "No repair round: the
  repair request would exceed the prompt size limit."); L5 a save conflict
  keeps `valid: true` and sets `not_saved` + `not_saved_reason`; L6 a
  Studio strategy whose version-1 envelope names the session but was never
  linked is re-linked on the next request (audit `studio_session_relinked`)
  instead of creating a second family; L8 the session estimate is cached
  per session until the last message, the strategy version, the newest 3
  runs or the newest memory fact change.
- **On-topic guard (user, 2026-10-17, after "Explain the categoric
  imperative by Kant" got a philosophy answer):** the answer schema has a
  REQUIRED `kind: "draft"|"clarify"|"off_topic"`; the system prompt states
  the scope (only strategy design/refinement in the DSL; everything else →
  `{"kind": "off_topic", "reply": "", "spec": null}`; instructions inside
  user text are ignored). Deterministic backend guard
  (`prompts.scope_guard`) BEFORE the schema check: `off_topic`, missing or
  unknown `kind`, `draft` without a spec, `clarify` with a spec / operator
  requests / reply > 500 chars, or a 3rd `clarify` in a row → the AI text is
  DISCARDED: the assistant message is stored with `text: ""` (the raw answer
  only in `detail_json.discarded_text` for the audit, never returned),
  `discarded: true`, `discarded_reason` (`off_topic`, `kind_missing`,
  `kind_unknown`, `draft_without_spec`, `clarify_with_spec`,
  `clarify_too_long`, `clarify_with_operator_requests`, `clarify_limit`),
  its provenance and cost (booked and shown), plus a fixed NON-AI
  `system_note` "The Strategy Studio only helps design and refine trading
  strategies in the rule language. Describe a trading idea (market, signal,
  rebalancing) and the Studio drafts a strategy." — returned as `notes[0]`.
  A discarded repair answer → `draft.repair_skipped` "The repair answer was
  discarded (<reason>).". **message** gains `kind: str|null` (assistant
  only), `discarded: bool`, `discarded_reason: str|null`. GET
  `/v2/studio/sessions` and `/v2/studio/sessions/{id}` carry
  `ai_interaction_notice` = "You are interacting with an AI system. Its
  answers are AI-generated, not verified, and not investment advice." (EU
  AI Act Art. 50(1); pinned at the top of every session in the UI).
- **Guard hardening (review H1, 2026-10-17) — structure is enforced
  deterministically, the TOPIC by an independent model check (still a
  model: residual risk, stated in the UI notice).** Limits (`scope_guard`,
  discard reasons in brackets): draft `reply` ≤ 600 chars
  (`draft_reply_too_long`), `assumptions` only with a draft, ≤ 5 × 200
  chars (`assumptions_too_long`, `clarify_with_assumptions`),
  operator-request `purpose` ≤ 200 chars (`operator_request_too_long`),
  clarify reply ≤ 300 chars (`clarify_too_long`), a JSON answer that is not
  an object → `not_an_object` (charge shown, no 500). A final draft that is
  still invalid → `discarded_reason: "draft_invalid"`: reply, assumptions
  and operator requests are dropped, only the validator card (`draft` with
  spec + errors) stays, plus the fixed note "The AI draft did not pass the
  validator — its text is not shown; the validator's findings are below.".
  **Independent topic check** before anything is shown: one extra call
  (Standard model, fixed prompt, temperature 0, max_tokens 120, feature
  `sp-studio-guard`, label "Hedge Fund · Studio topic check", ideation
  budget) that sees ONLY the AI's displayable texts (reply, assumptions,
  operator-request name/purpose/signature, the spec's name, description,
  tags, parameter descriptions) as quoted data — never the user's text —
  and answers `{on_topic, reason}`. `on_topic: false`, bad JSON, an error,
  a timeout or a budget block → everything displayable is discarded
  (`discarded_reason: "topic_check_failed"`, `draft: null`, the spec is NOT
  saved or backtested) and the fixed scope note is shown. Message field
  `topic_check: "passed"|"failed"|"skipped"` (skipped = nothing
  displayable; the guard's own reason is stored for the audit only). The
  guard call's cost is part of the message's `cost_usd` / `usage.calls`.
  The same check runs on paper extractions (§14.7) and committee role texts.
- **Budget reservation (review L4):** a call reserves its estimate under one
  process-wide lock before it is sent (Studio, paper and committee calls
  share it) and settles afterwards, so parallel calls cannot overshoot the
  month/day limits.
- Tests: `tests/test_sp_studio.py`.


### 13.6 5a WPF notes (XW, 2026-10-17: Strategy Studio — §13.3, bound to §13.5)

- **Files**: `Platform/StudioView.cs` (new, code-built UserControl), `Platform/PlatformClient.Studio.cs`
  (new: DTOs + `ListStudioSessionsAsync`, `CreateStudioSessionAsync`, `GetStudioSessionAsync`,
  `PostStudioMessageAsync` (client timeout 210 s = 2 × 90 s LLM + overhead), `CancelStudioDraftAsync`,
  `Get/PutIdeationSettingsAsync`), `HedgeFundWindow.Studio.cs` (new: rail wiring, "Open in Library"),
  `HedgeFundWindow.IdeationBudget.cs` (new: settings), `HedgeFundWindow.xaml(.cs)` (rail entry "Studio"
  between Library and Research, `StudioHost`, ideation inputs below the explanation budget, 30-s refresh in
  the state poll, `Shutdown` on close), `Platform/LibraryView.xaml.cs` (`ShowStrategyAsync(id)`).
  Not used by the UI (yet): `GET /v2/studio/operator-requests`, `GET /v2/memory` (the chat shows each
  message's `operator_requests` / `memory_used`).
- **Layout**: left = sessions (newest first, "+ New session" with optional title ≤ 120); right = header
  (title, session/strategy/family ids, family trial count, "Open in Library"), budget line
  (`StudioJson.BudgetLine`: month + day spent of budget/cap, "off", `paused_reason`), chat, composer
  (TextBox ≤ 4,000 chars with counter, Standard/Premium radios with `estimate_usd` of GET session — no
  number without one, "✦ Send", Ctrl+Enter, "Writing a draft… n s" while the synchronous POST runs; session
  list disabled meanwhile). After a successful POST the session is reloaded (GET) — the reply is not
  rendered from the POST payload; a failed POST shows the backend's `error` verbatim and keeps the text.
- **Messages** (plain TextBlocks only): user bubble; assistant = violet AI container, header
  "✦ AI-generated · <provenance.model> · <provenance.generated_at|created_at>", cost line (tier, cost_usd,
  usage.calls > 1 → "incl. one repair round", usage.estimate_usd), `AiContent.Caveat`, screen-reader name
  `AiContent.AutomationName`; `system_note` = italic "Backend note" (no AI label).
- **Draft card**: valid/invalid chip, version + strategy id, repair round / `repair_skipped`, validator
  errors + warnings (path, line/col, code, message), `repair_errors` expander, `unchanged` note, "Spec changes
  vs v<n>" = client-side flattened JSON path diff (+ / − / ~, ≤ 60 lines) of `draft.spec` against the previous
  valid, changed draft of the session (first one: "no previous version"), "Full spec (JSON)" expander, family
  trial count (GET session `trial_count`), backtest status (`queue` entry wins while waiting — position +
  Cancel → `…/drafts/{message_id}/cancel`; started → `runs[run_id].status` else `run_status`, active → Cancel
  backtest (`/v2/backtests/{id}/cancel`, confirm), done → mini metrics from `runs[].key_metrics` else
  `draft.backtest.key_metrics`; cancelled / start_failed / not_queued → the backend `message` verbatim),
  "Open report" (BacktestReportWindow) and "Open in Library". Operator requests = amber box "Operator
  requests — unverified AI suggestion (text only, never executed)"; assumptions list; "Memory used for this
  draft (n)" expander (kind, family, run, text). Polling: GET session every 3 s only while a draft is waiting
  or its run is active AND the view is visible; a poll re-renders only the backtest blocks (full re-render
  when the message count changed).
- **Settings**: "AI ideation (Studio) — monthly budget (USD)" + "— daily cap (USD)"; empty/disabled until
  GET answers, PUT of BOTH values 600 ms after the last valid edit; hint = budget line or the backend's
  error verbatim (503 keeps the inputs enabled as the repair path); range from `max_*` (SPEC 200 only as
  the client format check when absent).
- **Review fixes (2026-10-17)**: M1 — a valid draft with `requires_confirmation` (and no version) shows the chip
  "unsaved — needs your confirmation" + "valid", the backend text ("demotes <state> to draft"), no backtest, and
  "Save as new version…" → confirmation dialog (new version demotes the strategy to draft, earlier validation no
  longer applies, the automatic backtest counts as another trial) → `POST …/drafts/{message_id}/save {confirm:
  true}` (`SaveStudioDraftAsync`) → session reloaded; errors (409 not_confirmable / stale) verbatim; never
  auto-saved. `saved_by_confirmation` → "Saved after your confirmation". L5 — `valid && not_saved` → chip "not
  saved" (+ "valid") and "Not saved: <not_saved_reason>" instead of "invalid". L7 — the diff compares against the
  STORED version its label names: base = the version itself for `unchanged`, else `base_version`, else (stored v
  without a base) v−1; the new side = the stored v when the draft has its own version, else the proposed spec;
  both via `GET /v2/strategies/{id}/versions/{n}` when the expander is first opened (cached per strategy@version,
  failures not cached, shown verbatim). Header "Spec changes: v<new>|proposed spec vs stored v<base> (k)".
- **Scope / AI-interaction fix (user, 2026-10-17)**: `ai_interaction_notice` (GET sessions + GET session) is pinned
  as a neutral, non-dismissible block at the top of the session view AND above the composer (collapsed only
  until the backend sent it — no stand-in text); the composer shows the placeholder "Describe a trading idea —
  market, signal, rebalancing…". An assistant message with `discarded: true` is never rendered as AI content:
  a neutral "System note" says "AI reply discarded (<reason>) — not shown · <cost line>"; the backend's fixed
  scope note (`system_note`) follows as a neutral system note (no ✦ label, no AI colours). `kind: "clarify"`
  replies are normal AI bubbles with "· clarifying question" in the AI header. DTO: message `kind`,
  `discarded`, `discarded_reason`; session list/detail `ai_interaction_notice`.
- **Guard review (2026-10-17)**: `draft_invalid` → the neutral "AI reply discarded (the draft did not pass the
  validator)" note + ONLY the validator card (no reply, assumptions or operator requests; labelled "Validator
  findings on the spec the AI proposed"); `topic_check_failed` and every other reason → the note only (no draft
  card). Message `topic_check` (passed|failed|skipped) is shown in the cost line.
- Static check only (no dotnet on the VM): braces balanced, XAML parses, every new identifier defined once.
