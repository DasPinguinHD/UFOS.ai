# Strategy Platform SPEC — archive: 22-stage10-council-migration

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 22. Stage 10 — Council of AIs migration (CONTRACT rev. 3 — approved for implementation (review 2026-10-24); batch S10-C; D1–D4, D7, D12 confirmed by the user; module moves M1–M6 DONE; R1 built 2026-10-24; deliberate differences approved 2026-10-24 17:59; R2 (backend) built 2026-10-24 — the real switch-over only by the user in the app — as-built §22.13 / §22.14)

Basis: SP §15 ("Stage 10 — the Council of AIs is migrated into the new platform as a strategy (parity test against the
old engine), then a SAFE and CAREFUL cleanup"), the approved deletion list `HedgeFund/docs/STAGE10_DELETION_LIST.md`
(A1 / A4 / B rows, all "after the Council migration"), FUND SPEC §7–§10 (agents, judge, sizing, engine) and SP §17.9
(the old paper account already lives in `paper-council`, source `council-of-ais`, positions HELD). Rev. 2 answered the
independent contract review of 2026-10-24 (2 critical, 9 major), rev. 3 the re-review (N1–N7 + notes); lead decisions
are marked **[lead]**.

| # | Decision | Status |
|---|---|---|
| D1 | `council` is a non-DSL strategy kind, exempt from G1–G8 / holdout / incubation; labelled **"not validated — AI strategy"** everywhere (§22.9) | confirmed by the user 2026-10-24 14:04 |
| D2 | Intraday cadence + "Run now"; the old crypto risk watch is dropped | confirmed by the user — subject to the review verifying the runtime coverage table in §22.6 |
| D3 | The latest old `fund_params` version is copied ONCE into hedgefund.db (`cl_params`), key by key per §22.4, at R2 in the switch-over transaction | confirmed by the user |
| D4 | Old-engine history: a read-only JSONL export before R4; no read-only history view | confirmed by the user ("Ein Export reicht") |
| D5 | The old runs / recommendations / signals are ALSO imported once into `cl_*` with `legacy = 1` at R2, in the switch-over transaction (cold start of the change detection + Explain of the held positions), in addition to the D4 JSONL export | **[lead]** decided 2026-10-24 |
| D6 | The WPF kill switch / "Stop engine…" move to `/v2/oms/kill` BEFORE R2; the pinned Resume becomes a dialog over the halted platform accounts + the old engine BEFORE R2 (§22.8); R2 and R3 ship in ONE build; after R2 `/fund/kill` and `/fund/resume` are audited aliases of the platform routes (never 410) until R4 removes them | **[lead]** (review C1, re-review N4) |
| D7 | **Option B — platform standard:** outside the equity trading window and during the opening grace the Council only ANALYSES equities — the decision is recorded with "market closed — no order placed" (no queueing, no special check path); scheduled cycles run only inside the session, the first one after the opening grace; crypto unaffected (24/7) — §22.5 | confirmed by the user 2026-10-24 14:27 (review M1); a deliberate difference vs the old engine (§22.10) |
| D8 | "Run now" reports `orders` per class `{"equity": bool, "crypto": bool}`; an exhausted AI budget behaves exactly like the old D9 — the cycle runs, the affected symbols get no AI judgement (recorded reason), carried recommendations are still sized / executed as before — for scheduled cycles and "Run now" alike; no `503 budget` | **[lead]** (re-review N5) |
| D9 | `paper-council`'s broker params are editable through an audited, versioned, insert-only override (`cl_broker_params`, Council view → params editor, the same bounds as `/fund/params` today); the risk agent's 75 % drawdown rule reads `pf_settings.drawdown_kill_pct` | **[lead]** (re-review N6) |
| D10 | One per-account trade lock shared by the Council order step, the runtime execution step and `control.kill_account` — no non-protective order after a kill | **[lead]** (re-review N7) |
| D11 | Stale Council crypto orders expire after a fixed **60 min** (the old value) at ANY time, as OMS `accepted → expired` transitions — a deliberate difference (§22.10) | **[lead]** |
| D12 | The §17.9 migration is removed at R4: `execution/migration.py`, the `/v2/portfolio/migration*` routes and the WPF migration dialog (renamed to `*.stale` first, deleted in the R4 / R5 deletion step; R2 requires the migration done, so it has no further use) — row added to the deletion list | approved by the user 2026-10-24 14:42 ("Ja") |

### 22.1 The Council as a platform strategy

- **Kind `council`, not a DSL spec (D1).** The Council calls an LLM (sentiment + judge); an LLM cannot be backtested
  without look-ahead, so it is NOT a StrategySpec and never goes through G1–G8 / holdout / incubation. Its rule part
  stays the backtestable library entry "Council Rules (no AI)" (`agentic_committee_rules`, §7.9) — unchanged.
- **Code:** new package `strategy_platform/council/` — `agents/{technical,risk,sentiment}.py` (moved from
  `fund/agents/`), the macro agent from `strategy/features/macro_agent.py` (moved there in M6, §22.7), `judge.py`
  (rule score, rule suggestion with hysteresis, change detection, batch LLM judge, model modes incl.
  `premium_on_flip`), `sizing.py`, `service.py` (one council cycle + the task, §22.5), `store.py` (§22.3),
  `params.py` (§22.4). The decision logic is MOVED unchanged (FUND §7–§9 stay the behavioural contract); only inputs /
  outputs are re-wired. No rule-based stand-in (FUND D9): a symbol whose judgement is unavailable gets NO new target —
  its position is HELD and the reason recorded.
- **Where it trades:** only `paper-council`, source `council-of-ais`; never another account, no broker routing in
  stage 10. **Sizing base** = the `council-of-ais` sub-ledger equity (§17.5); precondition (checked at R2 and at every
  cycle): `paper-council`'s `_unallocated` source holds 0 cash and 0 positions — else the cycle records `partial`
  "paper-council has unallocated capital — the Council sizes only on its own sub-ledger" and places no orders.
- **Trade counting** (`max_trades_per_day`): Council orders (origin `council`) that reached the OMS as submitted /
  accepted / partially filled / filled per NY trading day (equities) or UTC day (crypto); rejected-by-check and
  cancelled-before-submit orders do not count; a reversal = 2 orders (FUND §9 rule "only the close fits today").

### 22.2 AI budget (review C2)

- **Limit:** `cl_params.ai_daily_budget_usd` (copied at R2). The budget key stays `fund` (label "Council of AIs"),
  the cost-log modules stay `fund-sentiment` / `fund-judge`; `ai_budgets.FundSource` reads the limit from `cl_params`
  and the spend from the counter below from R2 on (until R2 it reads the old engine as today).
- **ONE spend counter** — hedgefund.db meta `council.ai_spend` `{day (UTC), usd}`, written in the same transaction as
  the call's `cl_ai_calls` row. **Seeded at R2** in the switch-over transaction with
  `max(old newsroom.db meta fund.ai_spend for today, Σ cost-log fund-sentiment + fund-judge for today)`.
  **Fails closed:** an unreadable counter or cost log = budget exhausted ("Today's AI spend could not be read — AI
  calls are paused …"); pre-call estimate booked for a sent-but-failed call (FUND §8.6 rules unchanged).
- **Exhausted budget (D8):** exactly the old D9 behaviour — the cycle runs; symbols that need a judgement get none
  ("AI budget reached — raise the limit in Settings to continue.", recorded per symbol); carried recommendations are
  sized and executed as before; scheduled cycles and "Run now" alike (no `503 budget`).
- **No double calling in R1:** before R2 the Council makes NO live LLM call (no scheduled cycles, "Run now" answers
  503 `not_active`); LLM calls happen only inside the parity tests with the fake client. From R2 on the old engine is
  stopped (§22.8), so only one caller exists.
- **After a revert** (§22.8) the old engine's counter (newsroom.db meta `fund.ai_spend`, today) is seeded with
  `max(old meta, Σ cost-log fund-sentiment + fund-judge for today)` before the old engine starts again.

### 22.3 Data model (hedgefund.db migration v25, insert-only; review M6b)

Pre-v25 backup (`data/backups/hedgefund.db.pre-v25-<UTC>`, like v23) — part of the rollback (§22.8). All tables have
no-update / no-delete triggers; times UTC ISO; `legacy` = 1 for rows imported from the old engine (D5).

| Table | Columns (key) |
|---|---|
| `cl_params` | `version` PK, `params_json`, `created_at`, `actor`, `reason`, `source` (`copied from fund_params v<N>` / `user` / `legacy fund_params v<N>`), `legacy` (legacy rows are frozen snapshots, never effective) |
| `cl_broker_params` | `version` PK, `params_json` (only the broker keys of §22.4), `created_at`, `actor`, `reason` — the override of `paper-council`'s migrated broker params (D9) |
| `cl_runs` | `id` PK, `old_id` (legacy), `trigger` (`scheduled` / `manual` / `startup`), `started_at`, `params_version`, `session_state` (open / closed / early_close / holiday), `orders_enabled` (0/1 at start), `sandbox` (0/1), `legacy` |
| `cl_run_results` | `run_id` PK, `finished_at`, `status` (ok / partial / error), `judge_mode` (llm / carried / mixed / unavailable / none; legacy also `rules`), `cost_usd`, `summary_json` (`judge_unavailable`, `data_unavailable`, `valuation_paused`, …) |
| `cl_signals` | (`run_id`, `symbol`, `agent`) PK, `score`, `confidence`, `summary`, `details_json`, `source` (rules / llm / cached / unavailable), `ai_generated` (0/1), `provenance_json` (model, provider, prompt version, generated_at — EU AI Act), `legacy` |
| `cl_decisions` | `id` PK, `old_id` (legacy), `run_id`, `symbol`, `rule_score`, `agreement`, `rule_action`, `judge_source` (llm / carried / unavailable; legacy also `rules`), `action`, `confidence`, `rationale`, `model`, `cost_share_usd`, `unavailable_reason`, `target_weight`, `sizing_note`, `ai_generated`, `provenance_json`, `legacy` |
| `cl_positions` | (`run_id`, `symbol`) PK, `qty`, `avg_price`, `mark`, `weight`, `subledger_equity` — the per-symbol state the run decided on |
| `cl_executions` | `id` PK, `decision_id`, `client_order_id` (null when no order), `outcome` (`submitted` / `rejected` / `not_executed` / `skipped`), `reason`, `at` — the SUBMIT-TIME outcome only; the order's later lifecycle (fills, cancel, expiry) is read from the OMS by `client_order_id` |
| `cl_ai_calls` | `id` PK, `run_id`, `feature` (`sentiment` / `judge` / `judge_premium`), `model`, `request_hash`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `status` (`ok` / `error` / `budget` / `no_key` / `invalid`), `provenance_json`, `at` |

Hedgefund.db meta: `council.switchover` (`pending` / `done` / `reverted` + time), `council.orders_enabled`,
`council.ai_spend`, `autotrade.heartbeat_minutes` (§22.4), `council.legacy_hwm` (`{"runs": max old run id,
"recommendations": max old id}` — §22.8a). The stale SPEC note "OS1's archive tables take v25+" (fix
batch fix1023) is struck: the archive has its own file `options_archive.db`.

### 22.4 Params — one source of truth per value (review M3 / M4)

Copied at R2 from the latest old `fund_params` version in the switch-over transaction (D3):

| Old key | New owner | Consumer | Fate |
|---|---|---|---|
| `watchlist`, `min_confidence`, `max_position_pct`, `crypto_max_position_pct`, `max_trades_per_day`, `agent_weights`, `risk_enabled`, `judge_model_mode`, `change_threshold`, `judge_stale_hours`, `min_hold_days`, `ai_daily_budget_usd` | `cl_params` | Council | kept (same limits as `fund/params.LIMITS`) |
| `interval_minutes` | `cl_params` | Council task | kept; bounds **5 … 60, multiples of 5** (an old value that is not a multiple of 5 is rounded UP and the copy row says so) |
| `drawdown_stop_pct` | paper-council post-trade limit (`pf_settings.drawdown_kill_pct`, set by §17.9) | runtime drawdown kill + the Council's risk agent (its "drawdown ≥ 75 % of the stop → ×0.5" rule reads `pf_settings.drawdown_kill_pct`, D9) | dropped from the Council (one source) |
| `allow_shorts`, `not_shortable`, `short_exposure_max_pct` | paper-council broker params (migrated by §17.9) + the `cl_broker_params` override (D9) | broker + the Council's risk agent (reads the effective values) | dropped from the Council params |
| `fees_bps`, `crypto_fees_bps`, margins, borrow rates / overrides, crypto funding / liquidation fee | paper-council broker params + the `cl_broker_params` override (D9; same bounds as `fund/params.LIMITS`; audit-first; effective from the next fill / accrual) | broker | dropped from the Council params |
| `autopilot_heartbeat_minutes` | **platform setting** hedgefund.db meta `autotrade.heartbeat_minutes` (bounds 5 … 240) | `execution/control.py` dead-man | moved; read explicitly; **unreadable → fail closed** (auto trading switched off + alert), never a silent DEFAULT; the legacy read of the fund params is removed at R2; test |
| `closed_interval_minutes`, `crypto_risk_watch` | — | — | dropped (D2) |
| `starting_capital` | — | — | dropped (the account exists) |

### 22.5 The Council task, schedule, execution (review M1 / M2 / M5 / M6)

- **Own task** `council-cycle` (asyncio + worker thread) with its OWN lock — the agents and the LLM calls run OUTSIDE
  `runtime.cycle`. Only the order step enters the runtime / OMS path: `runtime.submit_source_targets(account,
  source, targets, run_id)` takes the **per-account trade lock** (D10) and **re-checks right before each submit**:
  auto trading ON, `council.orders_enabled`, account not halted, kill not active, not sandbox, switch-over `done`; any
  failing → `cl_executions.outcome = not_executed` with the reason.
- **Per-account trade lock (D10):** `control.trade_lock(account_id)` (re-entrant) is held by the Council order step,
  by the runtime execution step and by `control.kill_account` while it halts and liquidates; every non-protective
  submit re-checks the halt inside the lock — no non-protective order can follow a kill. **Lock ordering:** a thread
  never holds two account trade locks; `kill_all` / `kill_platform` and `_cancel_runtime_orders` walk the accounts ONE
  AT A TIME (take, act, release); `oms.cancel_working` takes no trade lock or only its own account's; the runtime's
  drawdown path (`set_autotrade(False)` then `kill_account`, `runtime.py` ~273–274) takes the lock inside
  `kill_account` only, after `set_autotrade` (which takes no trade lock). Tests: a Council submit racing a kill places
  nothing after the kill; a `kill_all` while another account's execution step runs does not deadlock.
- **Schedule (D7):** scheduled cycles run only while the session is open AND after the opening grace: the first
  cycle of a session at open + 5 min grace + 5 s (09:35:05 New York on a regular day); then next = last start +
  `interval_minutes`; startup + 20 s runs a cycle only when that point lies inside the session after the grace (confirmed
  by the re-review: first cycle = open + grace + 5 s); a cycle
  that comes due outside it is not run; missed cycles are never caught up. Early close / holiday / DST from
  `core/calendars.py`. The grace length is the paper broker's `PENDING_FILL_DELAY_MINUTES` (5 min) — one constant.
- **`POST /v2/council/run`** → `202 {"run_id": n, "orders": {"equity": bool, "crypto": bool}}` (D8; `equity` false
  outside the window / in the grace per D7; both false in sandbox or when Council orders / auto trading are off) ·
  `409 {"error": …, "code": "running"}` (a cycle runs) · `503 {"code": "not_active"}` before R2. An exhausted budget
  does NOT refuse the run (D8).
- **Heartbeat:** the Council GET routes do NOT touch the client heartbeat. Today the ONE always-on heartbeat is the
  app-wide `FundNotifications` poll of `GET /fund/events` (every 15 s while UFOS.ai runs and the backend is online —
  FUND §10; `control.last_seen()` = the newer of the platform touch and the old engine's `touch_client`);
  `/v2/oms/runtime` is polled only by Trading → Portfolio (`TradingView.cs` ~404) and the monitoring view's own polls
  stop when the view is hidden. Because `/fund/events` goes away with R2 + R3, the app-wide `MonitoringNotifier` poll of
  `GET /v2/monitoring/alerts` (every 15 s, started once by the Hedge Fund window, kept running while UFOS.ai runs —
  `MonitoringNotifier.cs`) becomes the heartbeat in the R2 + R3 build: `mon_alerts` calls `control.touch()` (today it
  does not); test: with only that poll running the dead-man never fires, without any poll it fires after
  `autotrade.heartbeat_minutes`.
- **Origin `council`** — added in R1 (not in the M moves), each with a test:
  - OMS: `oms.ORIGINS`, the attribution / sub-ledger mapping, client-id prefix `cl-`;
  - broker level: `ORDER_ORIGINS` (`core/types.py` after M1) and the `BROKER_ORIGIN` / `OMS_ORIGIN` maps in
    `execution/brokers/paper.py` (lines ~29–31, used at ~44 / 96 / 138) and `brokers/mirror.py` `BROKER_ORIGIN`;
  - `oms._resend_blocker` re-checks, for origin `council`, the Council switch, the switch-over state and sandbox;
  - `control._cancel_runtime_orders` and the auto-trading-off / dead-man paths cancel queued (unfilled) Council
    orders too (`cancelled: …` with the cause); protective / manual / kill orders stay;
  - the Council switch OFF cancels them likewise;
  - every kill forces `council.orders_enabled` = off — hooked into `control.kill_account` (so the runtime drawdown kill
    does it too; audit `council_orders_off`, cause `kill` / `drawdown`);
  - backend start forces `council.orders_enabled` = off explicitly (audit cause `startup`) — the user turns it on;
  - switch changes are audit-first (refused when the audit cannot be written; turning OFF always works) and turning
    ON is refused while the account is halted (409 `halted`).
- **D7 — equities outside the window / opening grace (decided: option B, platform standard):** a Council run that
  happens off-session (manual "Run now": evening, weekend, holiday) or inside the opening grace computes everything
  (agents, judge, sizing, target weight) and records each equity decision with `cl_executions.outcome =
  not_executed`, reason **"market closed — no order placed"**; no order is queued, there is no special check path; the
  next in-session cycle decides again from fresh data. Crypto symbols are traded 24/7 as in every run.
- **The Council's own working orders (in-session; the old `broker.target` rules, origin `council`):** queued orders
  do not exist under D7, but an order can still be working (a buying-power partial fill, an order waiting for a quote,
  a two-leg reversal whose second leg waits for the first fill). Per symbol, before a new order:
  - a pending protective (kill / drawdown / margin) or manual order → no Council order; `not_executed`
    "protective order pending" / "manual order pending" (the Council never replaces them);
  - a working Council order identical to what would be placed now, the first leg of the same transition, or already
    leading to the target → KEPT, no new order (no cancel / re-submit churn);
  - otherwise → the working Council order is cancelled ("replaced by a newer target — run N") and the target is
    re-planned in the same step; at most ONE working Council order per symbol;
  - a reversal sends the second leg only after the first leg FILLED (in the session that is normally the same cycle);
  - unfilled Council equity orders end with the session (tif `day` → `expired`); Council crypto orders still
    unfilled after **60 min** expire at any time (D11, OMS `accepted → expired`, §22.6).
- **Sandbox (review M6):** no scheduled cycles and no Council orders in sandbox mode; "Run now" runs an ANALYSIS
  (agents + judge, budget applies, `orders: {"equity": false, "crypto": false}`); R2 and R4 refuse in sandbox (409
  `sandbox`).

### 22.6 What the runtime covers instead of the old crypto risk watch (D2 — to be verified by the review)

| Old risk-watch step (outside the session, crypto held) | Platform runtime (§17.3, every 5 min while crypto is held, else 30 min, ALL platform accounts incl. `paper-council`) |
|---|---|
| quotes / mark to market | yes — quotes of held symbols per account |
| accruals (perp funding, borrow, dividends) | yes — before fills |
| pending protective + user orders filled | yes — queued fills |
| perp liquidation / margin check | yes — margin check + OMS sync |
| drawdown stop (may liquidate) | yes — post-trade drawdown kill (`pf_settings`, = the old `drawdown_stop_pct`); waits on stale crypto marks (`stale_marks`), as the old engine did |
| equity snapshot | yes — attribution + snapshots (account and sub-ledgers) |
| cancel queued AUTOPILOT crypto orders older than 2 × the risk-watch interval (default 2 × 30 = 60 min) | **not covered today → added (D11):** Council crypto orders still unfilled after a fixed 60 min expire at ANY time (OMS `accepted → expired`, "stale — re-evaluated at the next run") — a deliberate difference (the old rule ran only in the risk watch) |

### 22.7 Moving the six shared modules FIRST (review M9)

M1–M6 happen BEFORE R1 (the council package is then written on the final paths), each verified by the existing
parity tests (`test_sp_trading_parity`, `test_sp_backtest_parity`) + the full suite. Each step moves the file, switches
EVERY importer (platform, tests, `ai_budgets.py`, `main.py`, `fund/` itself), updates logger names asserted in tests,
and leaves no empty package `__init__` behind:

| Step | From | To |
|---|---|---|
| M1 + M2 (together) | `fund/types.py`, `fund/params.py` | `strategy_platform/core/types.py`, `strategy_platform/core/params.py` |
| M3 | `fund/llm.py` | `strategy_platform/core/llm.py` (budget key / cost modules unchanged) |
| M4 | `fund/market.py` | `strategy_platform/data/market_feed.py` (behaviour unchanged; its NYSE helpers keep using `core/calendars.py` — extended through 2030 in S10-H) |
| M5 | `fund/broker.py` | `strategy_platform/execution/paper_broker.py` |
| M6 | `fund/agents/macro.py` | `strategy_platform/strategy/features/macro_agent.py` (as in the approved deletion list; the Council and `macro_regime.py` import it) |

- **Temporary shims only:** each old path keeps a 3-line shim until R4 —
  `import sys; from strategy_platform.core import params as _m; sys.modules[__name__] = _m` — the SAME module object
  (no second copy of state; monkeypatching keeps working). Docstring "TEMPORARY shim (stage 10 S10-C) — removed with
  fund/ (R4)". No permanent fallback, no try/except import.
- **Guard test** `test_s10_no_new_fund_imports` (AST scan, multi-line imports included): nothing imports `fund`
  except `fund/` itself, `tests/test_fund_*`, the fund test kits, `tests/test_sandbox_profile.py`,
  `tests/test_s10_council_parity.py`, `execution/migration.py` (old-store reader — until R4, when the migration and
  its UI are removed, D12), `main.py` + `ai_budgets.py` until R2, and `LEGACY_TEST_IMPORTS` — named platform tests ×
  named old modules that compare with / drive the old engine until R4 (`test_ai_budgets` fund.engine,
  `test_mr_integrations` fund.store, `test_sp_backtest_parity` fund.store, `test_sp_clicktest_1022` fund.engine,
  `test_sp_risk_controls` fund.store + fund.engine, `test_sp_strategy_eval` fund.indicators, `test_sp_trading_parity`
  fund.store + fund.engine). `NOT_YET_MOVED` tolerates the not-yet-moved shared modules and shrinks with each step.

### 22.8 Switch-over, rollback, removal (review C1 / M8)

- **Before R2 (Agent W, can ship now — D6):** the pinned kill button and "Stop engine…" call `/v2/oms/kill`
  (`control.kill_all` — the platform kill AND the old engine's kill); a WPF static test + a backend test assert that the
  route the pinned button calls returns a kill result. Resume: see the next bullet (`/v2/oms/resume` is per account and
  never touches the old engine).
- **Kill / resume across the switch-over (Agent W's C1 findings, 2026-10-24):**
  - `control.kill_all` (`POST /v2/oms/kill`) returns `{"platform", "legacy", "council"}`. **Before R2:** `legacy` =
    the old engine's kill result passed through INCLUDING its closing-order list (`{"ok", "error", "halted",
    "orders": [...]}` — the list `/fund/kill` used to return, so the kill dialog shows the same information);
    `council` = `{"applicable": false}`. **After R2** (`council.switchover = done`): `legacy` =
    `{"ok": true, "applicable": false, "note": "not applicable — switched over to the Council"}` (never a failure);
    `council` = `{"applicable": true, "ok": bool, "orders_enabled": false, "cancelled_orders": n, "error": null|text}`
    (the Council switch forced off, its working orders cancelled); the `paper-council` positions are closed by the
    PLATFORM half (it is a platform account). The overall result is ok when `platform.ok` and (before R2)
    `legacy.ok` / (after R2) `council.ok`. Tests: a kill after the switch-over reports no failed half; the Council
    switch is off and its working orders are cancelled; a kill before R2 carries the old closing-order list.
  - **Resume BEFORE R2 (re-review N4, Agent W can ship now):** the pinned "Resume" opens a dialog listing the
    halted platform accounts AND the old engine (when halted) → one `POST /v2/oms/resume {"account_id", "reason"?}` per
    chosen account + `POST /fund/resume` for the old engine. **In the R2 + R3 build** the dialog lists the halted
    accounts only (paper-main, paper-council, broker accounts) → one `POST /v2/oms/resume` per chosen account; resuming `paper-council` clears the halt only —
    Council orders stay OFF until the user turns them on (`PUT /v2/council/orders`). After R2 `/fund/kill` and
    `/fund/resume` are NOT 410 (D6): they delegate to `control.kill_all` and to `control.resume("paper-council")`, so an
    older WPF build still stops / resumes correctly. The forwarding routes are audited exactly like the platform ones
    (audit-first; the same actions / details as `/v2/oms/kill` / `/v2/oms/resume`, plus `via: "/fund/kill"` /
    `"/fund/resume"`). They exist only from R2 until R4 and are REMOVED with the rest of `fund/` in R4 (lead decision
    2026-10-24, together with passing the old closing-order list through `kill_all` until R2).
  - The old Overview order panel (`POST /fund/orders`) goes with R3; manual orders are placed in Trading
    (`/v2/oms/orders`), Research's hand-over likewise.
- **R1** — moves done (§22.7); council package, v25, `/v2/council/*` built; parity green (§22.10); review gate. Old
  engine unchanged (analysis only), Council not active.
- **R2 = switch-over** (one user dialog + audit; refused in sandbox; preconditions: old account migrated —
  `pf_migrations` row; parity report accepted; `_unallocated` = 0; old autopilot off): (1) pre-v25 backup exists;
  (2) meta `council.switchover = pending` + audit; (3) ONE hedgefund.db transaction: `cl_params` copy (§22.4),
  `autotrade.heartbeat_minutes`, the spend seed (§22.2), the legacy import (D5); (4) `council.switchover = done`.
  `main.py` reads `council.switchover` at start (the `/fund/*` routes and `kill_all` read it per request): `done` → no
  `FundEngine`, Council task on; `pending` (crash between
  2 and 4) → step 3 is re-run idempotently (the transaction either happened — rows present — or not) then 4;
  anything else → old engine as today.
  **Stopping the old engine (re-review N3):** between steps 2 and 3 the running `FundEngine` is stopped IN-PROCESS
  under its `_trade_lock` (the scheduler task is cancelled, a running cycle is awaited, autopilot off; no backend
  restart needed); `FundEngine.start()` refuses while `council.switchover` is `pending` or `done`, and `main.py` does not
  create it then. `/fund/*` answer `410 Gone` `{"error": "The old engine was replaced by the
  Council strategy — use /v2/council/*", "code": "replaced"}` EXCEPT kill / resume (never 410, D6 — they delegate,
  see "Kill / resume across the switch-over" above). **R2 and R3 ship
  in ONE build.**
- **Revert (before R4 only):** `POST /v2/council/switchover/revert` (one dialog, audit; Council orders off, its working
  orders cancelled, `council.switchover = reverted`, the old engine's spend counter seeded (§22.2), then the old engine
  is RE-CONSTRUCTED in-process (a new `FundEngine` with its untouched newsroom.db params, started) and registered with
  `control.set_legacy_engine(engine)`, so `kill_all` reports the legacy half again; the `cl_*` rows stay —
  insert-only). The `/fund/*` routes decide per request from `council.switchover` at RUNTIME (not the startup state):
  `done` → 410 / the kill-resume aliases; `none` / `reverted` → the old engine's routes. A later R2 imports
  only old ids above the high-water mark (§22.8a).
- **Code rollback below v25 (emergency only, the user's decision; the revert above is the normal path):** (1) stop the
  backend (and close the app); (2) rename `hedgefund.db` and its `-wal` / `-shm` to `*.v25-<UTC>.stale` (never
  deleted); (3) copy `data/backups/hedgefund.db.pre-v25-<UTC>` to `hedgefund.db` (backend stopped — a plain file copy
  of a closed SQLite file; `PRAGMA quick_check` = ok on the copy); (4) start the older code. Everything written to
  hedgefund.db after the backup is lost (platform orders, ledgers, alerts since R2) — the dialog / guide says so.
- **§22.8a Legacy import (D5, inside the R2 transaction; idempotent via `council.legacy_hwm`)** — only old rows with
  an id above the high-water mark are imported (a second R2 after a revert imports only the new old ids); the mark is
  written in the same transaction:

  | Old (newsroom.db) | New | Mapping |
  |---|---|---|
  | `fund_params` versions referenced by imported runs | `cl_params` (`legacy` = 1, `source` "legacy fund_params v<N>") | `params_json` verbatim; frozen snapshot, never effective; `cl_runs.params_version` points at it |
  | `fund_runs` (trigger schedule / manual / startup; risk-watch runs are NOT imported — no decisions) | `cl_runs` (`legacy` = 1, `old_id` = id) + `cl_run_results` | trigger `schedule` → `scheduled`; `market_open` 1 → `open`, 0 → `closed`; `orders_enabled` / `sandbox` NULL (unknown); status `ok` / `partial` / `error` as is, `running` → `error` "never finished (old engine)", `skipped` → `partial` "skipped (old engine)"; `judge_mode` as is (`rules` allowed for legacy only); `cost_usd`, `summary_json` (+ `error` into it) |
  | `fund_agent_scores` | `cl_signals` (`legacy` = 1) | verbatim; `ai_generated` = 1 and `provenance_json` copied ONLY for `source` `llm` / `cached` (sentiment); `rules` / `unavailable` rows: `ai_generated` = 0, no provenance |
  | `fund_recommendations` | `cl_decisions` (`legacy` = 1, `old_id` = id) | action, confidence, target_weight, rule_score, rationale, model, provenance as is; `judge_source` = `source` (`llm` / `carried`; `rules` rows → `rules`, `ai_generated` = 0 and NEVER AI provenance); `rule_action` / `agreement` NULL; `cost_share_usd` from `usage_json.cost` when present |
  | `fund_recommendations.status` / `skip_reason` / `order_id` | `cl_executions` | `executed` → `submitted` (`client_order_id` NULL, reason "old engine order #<order_id>"); `skipped` → `skipped` + `skip_reason`; `open` / `superseded` → no row |
  | — | `cl_positions` | none for legacy runs |

  The change-detection memory after R2 is rebuilt from the newest legacy decisions per symbol (cold start).
- **R3** — WPF (Agent W, same build as R2): Council view (§22.11) on `/v2/council/*`; `FundClient` keeps only the shared
  HTTP / token base; `FundNotifications` keeps the toast host, its `/fund/events` source is replaced by the Council
  alert kinds; EquityChart / FundWindows / FundFormat / FundSettings stay; Research's order hand-over goes to
  `/v2/oms/orders` (a manual paper order, pre-filled) instead of `/fund/orders`.
- **R4** — refused in sandbox; history export (§22.12); golden fixture of the old path frozen (§22.10); rename to
  `*.stale`: `fund/engine.py`, `api.py` (incl. the `/fund/kill` / `/fund/resume` aliases), `judge.py`,
  `agents/{technical,risk,sentiment}.py`, `sizing.py`, `indicators.py`, `backtest.py`, `export.py`, `store.py`, the six
  shims, `fund/__init__.py`, `fund/agents/__init__.py`, AND the §17.9 migration (`execution/migration.py`, its routes
  `/v2/portfolio/migration*` and the WPF migration dialog — D12, approved by the user 2026-10-24; the guard-test
  exemption ends); tests per the deletion list.
- **R5** — full suite green + the user's build + click-test → delete the `.stale` files (deletion step with the user's
  delete permission). newsroom.db keeps its old tables.

### 22.9 Labelling, dropped features, observability

- **Labels:** the Council is never incubated, never live-eligible, never promoted, never allocator-managed, never
  decay-monitored; every view shows **"not validated — AI strategy"** and its record as **"forward paper record, N days,
  not validated"**. Sentiment and judge outputs carry **"AI-generated"** + their provenance (model, provider, prompt
  version, time — EU AI Act transparency).
- **Dropped with the old engine (explicit):** "execute recommendation" (manual accept) — the user places a manual
  order in Trading; the CSV exports `/fund/export/*` — replaced by the history export + the platform's own exports;
  `/fund/backtest(s)` — the backtestable part is "Council Rules (no AI)"; the old `flip` / `order` toasts — replaced
  by alert kinds `council_flip`, `council_order_filled`, `council_run_failed`, `council_budget_reached`,
  `council_orders_off` (monitoring alerts → Windows toasts); the SPY benchmark line — kept on the paper-council
  account equity chart (W).
- **Observability:** `GET /v2/council/state` carries the last run / result, next due time, task state (idle /
  running / not_active / sandbox), orders_enabled, budget (limit, spent, status) and the per-symbol cards; the backend
  log names every run start / end with id, status, cost.

### 22.10 Parity test (acceptance gate before R2; review M7)

- **Start state:** the REAL §17.9 migration run on a temp copy of an old store (synthetic old account with positions,
  queued orders, ledger) → `paper-council`; both paths start from that.
- **Harness** `tests/test_s10_council_parity.py` (+ `council_parity_testkit.py`): the OLD path (the body of
  `FundEngine` full cycles on a temp newsroom.db copy) and the NEW path (`council.service` + runtime order step on a
  temp hedgefund.db; the same PaperBroker) driven by ONE injected clock (a test asserts neither path reads the wall
  clock) with housekeeping (accruals, queued fills, margin, drawdown) on identical ticks; fake feed (quotes / bars /
  dividends), fixed macro / calendar / insider / news fixtures.
- **Fake LLM** keyed by (`feature`, canonical request hash) with scripted answers / usage; an unknown request FAILS
  the test loudly; the complete call logs of both paths are compared (feature, model, request, order of calls).
- **Lockstep window (re-review N1):** the lockstep comparison covers in-session cycles after the opening grace and all
  crypto targets; opening-grace and off-session EQUITY targets are kept OUT of the lockstep window and tested as
  separate scenario tests that assert the outcome `not_executed` "market closed — no order placed" (their decisions —
  signals, verdict, target weight — are still compared with the old path).
- **Data window:** (a) synthetic: 60 NY sessions, 8 symbols (6 equities / ETFs incl. one not shortable, BTC-USD,
  ETH-USD), a cycle every 30 min in the session ≈ 780 cycles, including a DST change, an early close, a holiday and
  the opening grace; (b) off-session manual runs (evening, weekend, across 00:00 UTC); (c) a frozen real-data
  fixture: 260 daily bars of SPY, QQQ, TLT, GLD, BTC-USD exported once from a BACKUP copy of hedgefund.db (never the
  real file).
- **Schedule equivalence test:** for a list of clock traces both schedules produce the same cycle start times.
- **Must match exactly** (floats abs ≤ 1e-12): agent signals (score, confidence, details, source); rule score /
  agreement / suggestion; the judge symbol set and canonical prompt; parsed verdicts (percent / invalid rules, veto
  mapping); recommendation source + unavailable text; target weights; order intents (symbol, side, qty after
  `round_qty`); skip reasons (text or one fixed mapping table); AI cost per run; every budget decision. After the
  replay: cash, positions, equity within 1e-6 USD.
- **Scenarios (each at least once):** judge unavailable (no key, budget 0, budget reached, unreadable spend, garbage
  JSON, timeout); `premium_on_flip` incl. a failing premium call; short veto / not shortable / block_short_increase;
  min_confidence; min_hold_days; max_trades_per_day (NY day vs UTC day, reversal with one order left); buying-power
  partial fill; crypto $10 minimum leg; two-leg reversal in the session (equity + crypto) and while closed (crypto);
  replace / keep of a working in-session Council order; manual / protective
  pending orders; kill / dead-man / Council switch off mid-cycle; drawdown stop; halted account; valuation paused;
  account unreadable; calendar / insider / FRED unavailable; budget across 00:00 UTC; spend restored after a restart;
  restart mid-window (memory rebuilt); cold start after R2 from the legacy import.
- **Deliberate differences** (table in the test; **the USER approves this list** before R2):

  | Difference | Old engine | Council (platform) | In the parity comparison |
  |---|---|---|---|
  | Equity orders outside the window / in the opening grace (D7) | queued for the open (filled at open + grace) | none — "market closed — no order placed" | decisions (signals, verdict, target weight) compared; **off-window equity order placement excluded**; account state compared only where it is not affected |
  | First scheduled cycle of a session | open + 5 s | open + 5 min grace + 5 s | schedule-equivalence test uses the new rule; the old trace is shifted by the grace |
  | Replace of a pending (queued) equity order off-window | the old engine replaced its queued order | no queued equity orders exist (D7) | separate scenario test only |
  | Two-leg equity reversal while closed | first leg queued, second leg after the open fill | decision only, no order (D7) | separate scenario test only |
  | Stale crypto order expiry (D11) | 2 × risk-watch interval, only in the risk watch | fixed 60 min at any time (OMS `accepted → expired`) | separate test |
  | Crypto risk watch (D2) | own cycle every `closed_interval_minutes` | runtime housekeeping (§22.6) | account level only |
  | Pre-trade checks | none | platform checks | fixtures stay inside the limits; a separate test shows a rejection → `not_executed` |
  | OMS state names, audit / event names | old names | platform names | mapped table |
  | Dropped features (§22.9) | manual accept, CSV exports, `/fund/backtest`, toasts | — / alert kinds | not compared |
- **APPROVED by the user 2026-10-24 17:59 (Berlin), "Jo" — all 9 points** of the plain-language list
  `UFOS.ai/docs/tasks/COUNCIL_DIFFS_FOR_USER.md`: (1) no stock orders while the market is closed / in the opening grace
  ("market closed — no order placed"; crypto 24/7); (2) first run of the day at open + 5 min + 5 s; (3) no queued stock
  orders to replace; (4) a stock flip while closed is a decision only; (5) unfilled crypto orders expire after a fixed
  60 min; (6) no separate overnight crypto check (the runtime covers it); (7) platform pre-trade checks before every
  order; (8) platform status / audit / event names; (9) manual accept, the old CSV exports, the old backtest button and
  the old pop-ups are not carried over (alerts + a one-time history export instead).
- **Golden fixture:** before R4 the old path's outputs over the whole window are frozen into a fixture, so the parity
  test keeps running after `fund/` is gone.
- **Acceptance:** 0 mismatches; the printed report (cycles, fields, scenarios) goes to the review gate.

### 22.11 API JSON and the Council view

- `GET /v2/council/state` → `{"active": bool, "switchover": "none|pending|done|reverted", "task": "idle|running|not_active|sandbox", "orders_enabled": bool, "label": "not validated — AI strategy", "record": {"since": "…", "days": n}, "next_due_at": "…Z"|null, "budget": {"limit_usd": x, "spent_usd": y|null, "status": "ok|reached|off|unreadable"}, "last_run": <run>|null, "symbols": [{"symbol", "action", "confidence", "judge_source", "rationale", "ai_generated", "provenance", "agents": [{"agent", "score", "confidence", "summary", "source"}], "target_weight", "position": {"qty", "weight"}, "execution": {"outcome", "reason", "client_order_id", "order_state" (read from the OMS)}|null, "unavailable_reason"}]}`
- `GET /v2/council/runs?limit=1..200` → `{"runs": [<run>]}`, `<run>` = `{"id", "trigger", "started_at", "finished_at", "status", "judge_mode", "cost_usd", "params_version", "orders_enabled", "legacy", "summary"}`; `GET /v2/council/runs/{id}` → `<run>` + `signals`, `decisions`, `executions`, `ai_calls`.
- `POST /v2/council/run` (§22.5) · `GET /v2/council/params` → `{"version", "params", "limits"}` · `PUT /v2/council/params {params…, reason?}` → new version (strict, unknown keys 400) · `PUT /v2/council/orders {"enabled": bool, "reason"?}` → `{"orders_enabled", "audit_id"}` (409 `halted` / 503 audit) · `GET /v2/council/switchover` → dry run `{ok, blockers, state, parity_accepted, params: {from_version, council, notes}, heartbeat_minutes, spend_seed_usd, legacy: {runs, signals, recommendations, hwm_before, hwm_after}}` · `POST /v2/council/switchover {reason?}` → `200 {state: "done", tx, params_version, spend_seed_usd, legacy, hwm, …}` / 409 `sandbox` / 409 `switchover_blocked` + `blockers` / 503 · `POST /v2/council/switchover/revert {reason?}` → `200 {state: "reverted", old_spend_seed_usd}` / 409 `not_switched_over` (dialogs; built in R2, §22.14). Errors `{error, code}`, never a stack trace.
- **Toolbar (W):** the pinned kill button calls `/v2/oms/kill` (already before R2, D6) and shows the three halves
  (§22.8). "Resume…": BEFORE R2 the halted platform accounts + the old engine (`/v2/oms/resume` per account,
  `/fund/resume` for the old engine); in the R2 + R3 build the halted accounts only (`/v2/oms/resume` per account).
- `GET /v2/council/broker-params` → `{"version", "effective", "migrated", "override", "limits"}` · `PUT
  /v2/council/broker-params {params…, reason?}` → new override version (strict; same bounds as `/fund/params`; audit-first)
  (D9).
- **Council view (W):** header with the label, record days, task state, next due, budget; "Run now"; "Council orders"
  switch; per-symbol cards (agents, verdict, AI-generated rationale + provenance, target vs position, execution
  outcome / why no order); run list + run detail; params dialog (Council params + broker-params override, D9);
  switch-over / revert dialogs.

### 22.12 History export (D4)

Before R4: the real newsroom.db is copied with the SQLite backup API (source read-only) into a temp file; from that
copy the tables `fund_runs`, `fund_agent_scores`, `fund_recommendations`, `fund_events`, `fund_audit`, `fund_params`,
`fund_backtests`, `fund_cash_ledger`, `orders`, `positions`, `equity_curve`, `signals` and the newsroom.db `meta` rows
whose key starts with `fund.` are written to
`data/backups/stage10-<UTC>/old_engine_history.jsonl` (one JSON object per row: `{"table", "row"}`) with a header line
(`newsroom.db` schema version, export time, row counts per table) and a `.sha256` file; the counts are re-read and
compared. The temp copy is deleted afterwards; the real newsroom.db is never written.

### 22.13 R1 as-built (2026-10-24, build-only — nothing switched over)

- **Module moves M1–M6 done** (suite green after each): `core/types.py`, `core/params.py`, `core/llm.py`,
  `data/market_feed.py`, `execution/paper_broker.py`, `strategy/features/macro_agent.py`; the old `fund/*` paths are
  same-object shims until R4; guard `tests/test_s10_no_new_fund_imports.py` (`NOT_YET_MOVED` empty).
- **Code:** hedgefund.db **v25** (`cl_params`, `cl_broker_params`, `cl_runs`, `cl_run_results`, `cl_signals`,
  `cl_decisions`, `cl_positions`, `cl_executions`, `cl_ai_calls`; insert-only triggers; pre-v25 backup);
  `strategy_platform/council/` (`service.py`, `store.py`, `params.py`, `spend.py`, agents / judge / sizing / indicators
  copied unchanged from `fund/`); `PaperBroker.plan_target` (side-effect-free plan of `target`); origin `council`
  (OMS, broker maps, client-id prefix `cl-`, re-send gate, `control.council_orders_off` on kill / start);
  alert kinds `council_*`; `api/council_routes.py` (`/v2/council/{state,runs,runs/{id},run,params,broker-params,orders}`,
  `/v2/council/state` carries the sandbox `mode` stamp); `main.py` builds `council.service.default()` on the same feed
  and newsroom accessors as the old engine but does NOT start it (R2 starts it); switch-over / revert routes, the
  heartbeat move (`GET /v2/monitoring/alerts` touches) and the legacy import are R2 work.
- **"Run now" (lead decision 2026-10-24: follow the contract):** `POST /v2/council/run` answers
  `202 {"run_id": n, "started": true, "orders": {"equity": bool, "crypto": bool}}`. `request_run` creates the
  `cl_runs` row up front (trigger `manual`, params version, session label, orders gate, sandbox flag) and hands its id
  to the Council task, which runs the cycle on that row (no second row). A row whose cycle cannot start (another
  cycle holds the lock, no params) is finished at once as `error` "not started: …"; at every Council task start
  `store.sweep_unfinished` finishes each non-legacy run without a result as `error` "interrupted — the backend
  stopped before this Council cycle finished" (`cl_run_results` is insert-only, so the result row is the only
  status) — no run stays "running" after a restart.
- **Settings (S10-E, ENV_API §9):** `core.llm.models()` reads the model ids only via `newsroom.ai.analysis_models()`
  (H moves it to `data/settings.json`); no environment read; an unreadable settings source raises
  `LlmUnavailable("error")` → the judge / sentiment report "AI unavailable" (no silent default); code defaults only
  when newsroom.ai is not importable (offline tests).
- **Hardening found by the parity harness:** `council.params.from_old` / `broker_from_old` refuse the stored version
  ROW (`fund_store.latest_params()` → `{"version", "params", …}` silently copied the defaults) — R2 passes
  `fund_store.effective_params()[1]`.
- **Parity harness (§22.10) built:** `tests/council_parity_testkit.py` + `tests/test_s10_council_parity.py` — the
  REAL §17.9 migration of one synthetic old account (positions from the previous session; queued orders block §17.9,
  so none are seeded), the old path on a SQLite-backup copy of newsroom.db, ONE injected clock, a deterministic fake
  market (8 symbols: 6 equities / ETFs incl. not-shortable XLE, BTC-USD, ETH-USD), ONE scripted fake LLM keyed by
  (feature, request hash) — the new path may only send requests the old path sent (misses fail the test) — and per
  tick: signals (score, confidence, source, details), verdicts, target weights, order intents, AI call logs, AI cost;
  at the end cash / positions / equity (≤ 1e-6). Lockstep window = in-session ticks after the opening grace every
  30 min (`UFOS_PARITY_SESSIONS`, default 2 in the suite). Executions are compared through ONE fixed mapping
  (`SKIP_REASON_MAP` / `map_skip`): the old single status "skipped" ↔ the Council's `skipped` / `rejected` /
  `not_executed` with the IDENTICAL reason text; "engine halted" ↔ "the account is halted"; "autopilot switched off"
  ↔ "auto trading is off" / "Council orders are off"; old status `open` (autopilot off) ↔ `not_executed` + gate
  reason. Orders are compared by what executed `(symbol, side, filled qty)` — the old store keeps the broker-clipped
  qty (refused leg: rejected qty 0), the OMS the requested qty + `filled_qty`.
- **Scenarios** (`tests/test_s10_council_parity_scenarios.py`, each asserts parity AND that the scenario happened):
  judge unavailable — no key, garbage JSON, AI budget 0, budget reached, unreadable spend (both fail closed),
  timeout (pre-call estimate charged); `premium_on_flip` incl. a failing premium call; not shortable / short
  exposure cap / shorts off (vetoes); min_confidence; min_hold_days; max_trades_per_day; buying-power partial fill;
  two-leg reversal in the session and crypto reversal while closed; halted account; kill mid-cycle; Council switch
  off mid-cycle; dead-man (no client heartbeat); drawdown stop (price shock → both halt + liquidate, books equal);
  valuation paused; calendar / insider / FRED unavailable; budget across 00:00 UTC; spend restored after a restart;
  restart mid-window (change-detection memory rebuilt from the DBs); plus D7 (grace / after the close) and the
  schedule-equivalence test in `test_s10_council_parity.py`.
- **Real-data fixture (c):** `tests/council_parity_real_bars.json` — last 260 daily bars up to 2026-10-23 of SPY, QQQ,
  TLT, GLD, BTC-USD, exported once from a COPY of the stage-10 backup hedgefund.db (sha256 recorded, one source per
  day: alpaca_sip > alpaca > tiingo); `RealWorld` replays it — parity over 6 ticks.
- **No wall-clock read:** `WallClockSpy` swaps `datetime` in the decision-path modules (old engine, Council, core.llm,
  paper broker, OMS, accounts, control, runtime, ledger / risk / checks, calendars, market feed) for a recording
  subclass; during the lockstep ticks the only call site is `core.llm.provenance` (the AI provenance stamp, metadata
  — `WALL_CLOCK_ALLOWED`). The OLD store booked its AI spend on the wall-clock UTC day (`fund.store._utc_day` without
  `now`); the harness pins that one old-engine read to the injected clock (the Council already uses the clock).
- **Results:** 60 NYSE sessions from 2026-10-05 (incl. the DST change, Thanksgiving + the early close after it) — 768 ticks, 414 Council
  orders, 258 AI calls, 0 unscripted requests, 0 mismatches (run twice, the second time with the execution /
  skip-reason and filled-qty comparison); 24 scenario tests green; real-data fixture green.
- **Open before the R2 gate:** the cold start from the legacy import (R2 builds the import); crypto $10 minimum leg,
  replace / keep of a working in-session Council order and manual / protective pending orders (not reachable with
  immediate paper fills in the session — partly covered by the `plan_target` unit tests, not by lockstep); account unreadable;
  the golden fixture (before R4). The deliberate differences were APPROVED by the user 2026-10-24 17:59 (§22.10).

### 22.14 R2 as-built (2026-10-24, backend; the WPF dialog + Council view are R3, Agent W, same build)

**Nothing was switched over by an agent.** The real switch-over happens only when the user confirms the dialog in the
app (`POST /v2/council/switchover`); every test runs on temp copies (temp hedgefund.db / newsroom.db / cost log).

- **`strategy_platform/council/switchover.py`** — `blockers` / `dry_run` / `run` / `apply` / `recover` / `revert`:
  - preconditions: not sandbox (409 `sandbox`); state not `done`; §17.9 migration done; `paper-council` exists and its
    `_unallocated` is empty; old autopilot off; the pre-v25 backup `data/backups/hedgefund.db.pre-v25-*` exists OR the
    database was created at v25+ (new meta `schema.created_version`, written once by `db.migrate` on an empty file);
    parity accepted (`PARITY_ACCEPTED = "2026-10-24"`, the user's approval §22.10); today's AI spend readable;
  - steps: audit `council_switchover {stage: pending}` (audit-first) → meta `council.switchover = pending` with an
    attempt id `tx` → the running `FundEngine` stopped in-process (a running cycle awaited ≤ 120 s, task cancelled,
    autopilot off under its `_trade_lock`, unregistered from `control`) → ONE hedgefund.db transaction: legacy import
    above the high-water mark (§22.8a; risk-watch runs — trigger `risk-watch` or a `startup` run with
    `summary.risk_watch` — skipped; `rules` decisions never AI-marked; executed → `submitted` "old engine order #N",
    skipped → `skipped` + reason), the Council params copied from the latest `fund_params` (`source` "copied from
    fund_params vN"), meta `autotrade.heartbeat_minutes` `{minutes, source}`, the spend counter seeded with
    max(old meta `fund.ai_spend` today, Σ cost log `fund-sentiment` + `fund-judge` today), the high-water mark and the
    marker `council.r2_tx` (with `tx`) → `council.switchover = done`, Council orders OFF (cause `switchover`), audit
    `council_switchover {stage: done}`, the Council task started;
  - idempotent per attempt: `apply` with the same `tx` writes nothing twice; backend start with `pending` →
    `recover` finishes the same attempt; a later switch-over after a revert has a new `tx` and imports only old ids
    above the mark;
  - revert: audit `council_revert` → Council orders off + working orders cancelled → Council task stopped → old
    counter `fund.ai_spend` seeded with max(old, cost log) → `reverted` → the old engine re-created by the factory
    `main.py` registered (`configure`) and handed to `control`, the `/fund` API and the AI-budget source.
- **`main.py`:** reads the state at start via `switchover.recover()`: `done` → no `FundEngine`, the Council task
  starts; `none` / `reverted` / unreadable → the old engine (its `start()` refuses while `pending` / `done` or when the
  state is unreadable with hedgefund.db open).
- **`/fund/*` (fund/api.py):** decided per request (`switch_state`): `pending` / `done` → 410 `{"code": "replaced"}`
  except `/fund/kill` → `control.kill_all(via="/fund/kill")` and `/fund/resume` → `control.resume("paper-council",
  via="/fund/resume")` (audited like the platform routes, with `via`); unreadable → 503 (kill still forwarded).
- **`control`:** `kill_all` → `{"platform", "legacy", "council"}` per §22.8 (before R2: legacy incl. the old closing
  orders, council `{"applicable": false}`; after: legacy "not applicable", council `{applicable, ok, orders_enabled,
  cancelled_orders, error}`); the heartbeat timeout from R2 on is meta `autotrade.heartbeat_minutes` — missing /
  invalid → `check_heartbeat` switches auto trading off (audit `autotrade_heartbeat_off` with the reason) and
  `set_autotrade(True)` answers 409 `heartbeat_unreadable`; `GET /v2/monitoring/alerts` touches the heartbeat.
- **AI budgets:** `FundSource` reads `cl_params.ai_daily_budget_usd` + the Council counter from R2 on; a PUT writes a
  new `cl_params` version (audit-first, `via: "/ai/budgets"`).
- **Tests:** `tests/test_s10_council_switchover.py` (preconditions + sandbox, unreadable spend refuses before any
  change, the full run incl. legacy counts / params / heartbeat / seed / cold-start memory, kill / resume / 410
  after R2, kill before R2, AI budget, heartbeat fail-closed + the alerts poll, pending recovery, revert + a second
  run importing only new ids); the parity harness now starts the Council through the REAL `switchover.run` (0
  mismatches unchanged).
- **Review fixes (independent review of R2 + R3, 2026-10-24; API: `UFOS.ai/docs/tasks/SWITCHOVER_FIX_API.md`):**
  - **M1 (double import):** ONE switch-over operation at a time (process lock around run / retry / abort / revert /
    recover → 409 `switchover_running`); a new run while `pending` is blocked (`switchover_pending`); the
    high-water mark is re-read INSIDE `BEGIN IMMEDIATE` (moved → 409 `concurrent_import`, nothing written);
    hedgefund.db **v26**: UNIQUE partial indexes `ux_cl_runs_legacy_old_id` / `ux_cl_decisions_legacy_old_id`
    (`old_id … WHERE legacy = 1`; a database that already holds duplicates cannot get them — then NOT silent: a
    critical alert `council_legacy_duplicates` at every start and the switch-over blocker `legacy_duplicates`
    {table, rows}, visible in the dry run). Test:
    two threads → one `done`, one `switchover_running`, no duplicate rows.
  - **M2 (stuck pending):** a failure after `pending` keeps `pending` + `last_error` / `error_at` (audit
    `council_switchover {stage: failed}`, critical alert `council_switchover_failed`), answered 503
    `switchover_failed` {last_error}; `GET /v2/council/switchover` adds `pending` {tx, at, actor, previous, applied,
    last_error, error_at} + `actions` {run, retry, abort, revert}; `GET /v2/council/state` adds `switchover_error`;
    `POST /v2/council/switchover/retry` (same attempt) and `POST /v2/council/switchover/abort` (transaction not
    applied → back to `previous` none / reverted; applied → like a revert with the old spend seeded; always Council
    orders off and the old engine re-created with autopilot off); `recover()` at start retries once and on failure
    leaves `pending` + alert — `main.py` then runs NO engine and logs it as an error.
  - **Minors:** the old engine's `_stopping` is set before the up-to-120 s wait (no new cycle); revert / abort return
    `old_engine_started` + `old_engine_error` (refusal or no loop reported, never swallowed); a revert of an already
    reverted state answers 200 `already`; blocker `council_unavailable` when the service failed to build and the
    switch-over answer carries `council_task_started` / `council_task_error`; `kill_all` treats `pending` like
    `done`; a failing `/fund/kill` alias reports both halves as failed/unknown (no fake ok); `*.tmp` leftovers never
    count as the pre-v25 backup (switch-over check AND `db.backup_before`); revert reads the state / spend off the
    event loop and is refused in sandbox.
- **The user's click-test (after the R2 + R3 build):** see the todo item S10-C.
