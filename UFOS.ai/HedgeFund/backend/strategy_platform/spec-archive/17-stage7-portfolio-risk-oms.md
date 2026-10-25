# Strategy Platform SPEC — archive: 17-stage7-portfolio-risk-oms

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 17. Stage 7 — portfolio, risk, OMS (paper) (contract, 2026-10-17)

Decisions: §15 (user, 2026-10-17) — allocator = inverse volatility with a correlation penalty; **incubation
length chosen per strategy before each incubation run, then immutable**; default hard limits **2 % daily
loss** (no new orders until the next session / UTC day) and **10 % drawdown from peak** (kill switch: cancel
all, close all, auto trading off) — adjustable with audit; netting; pre-/post-trade checks; OMS state machine;
migration of the old paper account. Plan: project doc "L6 lifecycle / L7 Portfolio & risk / L8 Execution
(OMS part)" — **one strategy runtime for backtest, paper and live**: a strategy emits target positions; the
same evaluator, portfolio and risk code and the same cost model (the real `fund.broker.PaperBroker`) turn
them into orders. PAPER ONLY: no live code path exists (stage 8 builds the live guard, live stays OFF; stage
11 only on the user's explicit go). Values marked **(confirmed by the user 2026-10-24)** were chosen by the
implementing agent while the user was away (2026-10-17) and confirmed by the user on 2026-10-24.

### 17.0 Audit after interruption (2026-10-18)

The stage-7 agent was stopped on 2026-10-17 after writing §17.12 (SPEC last saved 21:59; last code file
21:42). Audit of the state it left (read-only first, then fixes listed below):
- **What exists:** §17.1–§17.12 complete (contract, exact JSON §17.11, implementation notes §17.12 — no
  half-written subsection). All files named in §17.12 exist: `portfolio/` (9 modules), `execution/` (7 +
  `brokers/{base,paper}.py`), `live/{runtime,incubation}.py`, `api/trading_routes.py` (28 routes = the §17.10
  list), `storage/db.py` v15 (20 tables, insert-only triggers, incubation length immutable by trigger), hooks in
  `lifecycle/registry.py`, `backtest/engine.py` (shared `portfolio/decision.decide`), `fund/{engine,store}.py`,
  `fund/SPEC.md` §16, `main.py` wiring; tests `test_sp_{oms,pretrade,risk_controls,portfolio,trading_parity,
  incubation,trading_api}.py`, `test_fund_migrated.py`, `sp_trading_testkit.py`.
- **Broken / half-written:** nothing. No syntax errors (all backend `.py` parse), no TODO/FIXME except the
  intended IBKR/MT5 note in `brokers/base.py`; the abstract `NotImplementedError`s are the adapter interface.
  Full suite green on the VM (Python 3.10), split runs: fund 336 (1 skip), mr 333, other 134, sp 867 (≈1,670); after the fixes below all
  affected splits re-ran green (sp 868) and the scope guards stage0/mr0/budget0 are ok.
  The user's backend already started once with this code (2026-10-17 23:49 local): hedgefund.db migrated to v15,
  no stage-7 error in backend.log.
- **Missing (the process step after implementation):** the independent review — not done before the stop.
  Done now (focused: OMS/state machine, stored-before-sent, kill/dead-man, migration, checks, incubation band,
  allocator), plus a real-data acceptance on TEMP COPIES of the real hedgefund.db + newsroom.db (outside the
  mount; the real files untouched): v14→v15 not needed (already v15), migration dry run `ok` (4 positions
  BTC-USD, ETH-USD, QQQ, TLT; 4 orders, 61 ledger rows, 136 equity points, no blockers), real run on the copy →
  `paper-council` AccountView equals the old engine's for the same quotes (0 mismatches), Council sub-ledger cash
  = account cash, quantities equal, nothing re-attributed / re-imported.
- **Fixes (2026-10-18):** (1) `POST /v2/oms/orders` `client_order_id` now uses the contract pattern
  `^[A-Za-z0-9_-]{8,64}$` (it accepted `[A-Za-z0-9_.:-]{8,80}`) and refuses the engine's own prefixes (`rt-`,
  `run-`, `kill-`, `kil-`, `ddk-`, `dra-`, `mar-`) so a manual id can never pre-empt a runtime/protective order id
  (GET/cancel paths keep the wider stored-id pattern — runtime ids carry symbols such as `BRK.B`); test in
  `test_sp_trading_api.py`. (2) `incubation.expected_for`: the holdout returns used by the allocator's young-
  strategy volatility were dated against the unfiltered curve dates (shifted after a valuation gap) — now dated by
  their own valued point; first test of `expected_for` on the real run-card shape (it was stubbed everywhere):
  `test_sp_incubation.ExpectedBandFromTheCardTest`. (3) main.py: if the stage-7 wiring fails at startup,
  `/fund/kill` now reports `platform {ok: false, error: "…wiring failed at startup…"}` instead of omitting the
  platform half (fund/SPEC.md §16).
- **Deliberate, unchanged:** `POST /v2/oms/kill` is lenient about its body (a malformed body still kills — the
  kill switch always answers 200); the allocator applies min-weight → renormalise → cap → × max_total (docstring).
- **Open (outside this backend task):** the WPF "Portfolio & Trading" view (binds to §17.11/§17.12); docs/todo.md
  still lists stage 7 as planned; the user's click-test; all "(confirmed by the user 2026-10-24)" values of §17.

### 17.1 Accounts, coexistence with the old engine (the safety rule)

- **Platform paper accounts** (hedgefund.db, `pf_accounts`, insert-only): each one is an INTERNAL paper
  account driven by the real `fund.broker.PaperBroker` on a SQLite store with exactly the store interface
  of `fund.store` / `backtest/memstore.py` (`execution/paper_store.py`, tables `pa_*`, scoped by
  `account_id`) → identical fees, slippage, Reg T margin, borrow, Rule 201, dividends, crypto perps,
  funding, liquidation as the old engine and the backtest. Stage 7 knows two accounts:
  - `paper-main` — opened by the USER (`POST /v2/portfolio/accounts`, starting capital 1,000–100,000,000,
    default 100,000; broker params = `fund.params` defaults, like the backtest). Incubating strategies
    trade here.
  - `paper-council` — created ONLY by the migration of the old paper account (17.9): a 1:1 copy of the old
    account (cash ledger, positions, orders, equity curve, peak/start/accrual/funding state, broker
    params of the old engine's latest params version). Its only strategy source is **"Council of AIs"**
    (source id `council-of-ais`, kind `council`): the migrated positions are HELD (no target source until
    the Council itself becomes a platform strategy in stage 10); the user can reduce/close them with
    manual platform orders.
- **Coexistence rule (confirmed by the user 2026-10-24; the SAFEST of the options): an account has exactly ONE
  owner at any time, and the old engine and the new runtime never share an account.**
  - Before the migration: the old engine (`fund/`) owns the old account (newsroom.db) exactly as today;
    the runtime/OMS only trade `paper-main`. Nothing of the platform writes newsroom.db trading tables.
  - The migration is a **hard switch** started only by the user (dry run first, typed confirmation
    `MIGRATE`): it freezes the old account first (newsroom.db meta `fund.migrated_to_platform` =
    `{status: "migrating"|"migrated", migration_id, at}`), copies it, then marks it `migrated`. From then
    on the old engine is **advisory only**: its cycles still run agents + judge and store recommendations,
    but every trading path refuses with 409 "This paper account was migrated to the Strategy Platform on
    <date> — it is traded in Portfolio & Trading now." (autopilot on, manual orders, recommendation
    execute, reset; the cycle/risk watch skip accruals, pending fills, margin, drawdown stop, autopilot
    orders and equity snapshots; run summary `account_migrated`). The old tables stay as the read-only
    historical record (nothing deleted). `/fund/state` adds `legacy_account {status, migrated_at,
    migration_id}`.
  - The old autopilot is NOT turned into a target source in stage 7 (it would need a second order path
    into the same netting while the old engine keeps its own broker — rejected as less safe).
- **Kill switch covers both:** `POST /fund/kill` (the pinned button) ALSO runs the platform kill
  (all platform accounts); `POST /v2/oms/kill` runs the platform kill AND the old engine's kill
  (`engine.kill`). Each half works when the other fails (errors reported per half, never swallowed).
- **Dead-man switch covers both:** ONE heartbeat — every authenticated `GET /fund/state`, `GET
  /fund/events`, `GET /v2/portfolio/*`, `GET /v2/oms/*`, `GET /v2/incubation*` touches the same client
  timestamp (`engine.touch_client`); the platform's **auto trading** switch (17.7) uses the same timeout
  `autopilot_heartbeat_minutes` (fund params) and switches itself off with audit `engine`/
  `autotrade_heartbeat_off` (+ cancels the queued runtime orders, positions kept). Auto trading is forced
  OFF at every backend start (like the old autopilot).

### 17.2 Lifecycle beyond candidate

`candidate → validated` (holdout, §9.5, unchanged) → **incubation** → (**live-eligible**, a derived flag,
not a state) → `live`/`scaled` (stage 8/11 — refused now: "live trading is off — the live guard comes with
stage 8 and a live step only on your explicit go (stage 12)") → `retired`.
- **Who may trigger what:** only the USER promotes (`validated → incubation` by starting an incubation
  run; abort; retire; resume; migration; manual orders; auto trading on). The runtime/engine only acts
  inside the rules: decisions, orders, post-trade halts/kills, auto trading off (heartbeat / kill),
  completing an incubation run. The decay pause comes in stage 9.
- `validated → incubation` is ONLY possible through `POST /v2/incubation` (the generic transition route
  answers 409 "start an incubation run (Portfolio & Trading → Incubation) — its length is fixed then").
  `incubation → validated` = abort the run; `incubation → retired` (transition route) also aborts it. A new
  version saved while incubating returns the strategy to `draft` (existing rule) → the runtime aborts the
  run ("a new version was saved — incubation ended"). A family with `holdout_failed_at` stays locked (§9.8).
- **Incubation run** (`incubation_runs`, insert-only by triggers — the length can never change):
  `length_sessions` 20..250 (**default 60**, confirmed by the user 2026-10-24), sessions of the strategy's calendar
  (NYSE sessions for equity/mixed universes, UTC days for crypto-only), chosen in the start request and
  stored with `start_date` (the first session the runtime may trade — the next session after the start
  request) and `end_date` (computed from the calendar). A new run (after an abort/completion) chooses its
  length again. Events (`incubation_events`, insert-only): `started`, `session` (per completed session:
  traded or missed + equity), `completed`, `aborted`.
- **Expected band** (from the version's latest done `holdout` card = the validated evidence): daily
  returns of its equity curve → mean μ, std σ, max drawdown. **Result at completion** (default — user
  confirm): `passed` when (a) the realised cumulative log return of the strategy's sub-ledger lies within
  n·μ ± 1.96·σ·√n (n = traded sessions), (b) realised max drawdown ≤ 1.5 × the holdout max drawdown and
  (c) missed sessions ≤ 20 % of the length; (c) violated → `inconclusive`; else `failed`. Reported:
  realised return/vol/Sharpe/max DD, expected band, `tracking_error` (annualised std of realised daily
  return − μ), missed sessions. `live_eligible` = latest completed run `passed` and the strategy still in
  `incubation` with that version. After completion the strategy KEEPS trading on paper (state incubation)
  until the user aborts/retires it or stage 8/11 promotes it.

### 17.3 Strategy runtime (`live/runtime.py`) — the same code as the backtest

- One runtime for all platform accounts; an asyncio task in main.py ticks every 60 s and runs the work in
  a worker thread; `POST /v2/oms/runtime/run` runs one cycle now.
- **Decision** per incubating strategy (and later active ones) once per new completed daily bar: the
  same data loader as the backtest (`backtest.engine.DataLoader`: `all` series for signals, SIP history
  after the 15-minute rule, crypto UTC bars), `Panel.from_bars`, `evaluate`, `portfolio.rebalance_mask`
  and the decision block `portfolio/decision.py::decide` — EXTRACTED from `backtest/engine.run` and called
  by both (rebalance targets via `portfolio.target_weights`, stop loss / take profit exits, drawdown stop
  on the strategy's own sub-ledger, vol target). Decision time: first cycle ≥ close + 20 min (equities,
  NYSE) / 00:20 UTC (crypto-only). State between decisions (stopped symbols, halted) is stored with the
  decision (`rt_decisions`, insert-only). Data failure → that strategy's decision is `failed` with the
  reason (no stale signal, no stand-in); other strategies continue.
- **Execution** at the spec's `execution.timing`: `next_open` = first cycle ≥ open + 6 min of the next
  session (as the backtest); `next_close` = first cycle in [close − 10 min, close − 1 min] (default — user
  confirm: live cannot hit the closing print). A decision not executed before the next decision exists is
  `missed`. Desired quantity per source = `round_qty(|w × E_source / quote|)` signed (the backtest's
  formula); symbols leaving the targets → 0; `force` symbols → 0; no plan → hold.
- **Housekeeping** per cycle and per account (the old engine's order): accruals (borrow, dividends,
  funding) → queued fills (`process_pending`) → margin check → post-trade checks (17.6) → equity snapshot
  (+ sub-ledger snapshots, 17.5). Every 5 min while the US session is open or an account holds crypto or a
  working order exists, else every 30 min.

### 17.4 Allocator (`portfolio/allocator.py`, deterministic)

Per account, over its allocator-managed sources (incubating strategies; the Council source is not
allocated — it keeps its own capital):
- **Volatility** per source: annualised std of the source's daily sub-ledger returns over the last 60
  sessions (≥ 20 required); with fewer incubation sessions the strategy's expected volatility from its
  holdout card's curve (last 60 sessions) — that is the allocator's DEFINITION (`vol_source`
  `ledger`|`holdout`), not a fallback; neither → weight 0 with "no volatility estimate".
- **Correlation penalty:** pairwise correlation of daily returns over the last 120 common sessions (≥ 20,
  else that pair is left out of the mean); ρ̄_i = mean_j max(0, ρ_ij); raw_i = (1/vol_i) / (1 + λ ρ̄_i),
  λ = 1.0 (0..5).
- **Weights:** normalised, then capped per strategy at `max_weight` 0.40 (0.05..1.0) — the excess is NOT
  redistributed (stays unallocated cash); a weight below `min_weight` 0.05 (0..0.5) becomes 0 (and is
  then re-normalised once); `max_total` 1.0 (0.1..1.0) of the pool. Pool = Σ equity of the managed
  sources + `_unallocated` cash.
- **Rebalancing:** monthly (first decision of a new calendar month), when a strategy enters/leaves
  incubation, or on the user's `POST /v2/portfolio/allocation/rebalance`; only sources whose target
  capital differs by > 5 % of the pool (drift band) get a capital transfer (virtual `capital` entries
  between `_unallocated` and the source — cash moves inside the account only). Every allocation is stored
  (`pf_allocations`, insert-only) with its inputs.

### 17.5 Netting + virtual sub-ledgers (`portfolio/netting.py`, `portfolio/ledger.py`)

- **Netting:** per account and instrument the sources' deltas (desired − sub-ledger qty) are netted:
  opposite deltas are **crossed internally** at the current quote (sub-ledger `cross` entries, no fee,
  no account order — no self-crossing in the market); the residual is ONE account order (two legs when
  the account position flips sign: close first, open after the fill — PaperBroker rule), allocated to the
  sources whose delta points that way, pro rata (`allocations` on the OMS order). Reductions first.
- **Participation:** equities ≤ 1 % of the 20-day average dollar volume (the backtest's limit, applied to
  the account order); missing volume → that symbol's order refused (no default volume).
- **Sub-ledgers** (`sl_entries`, insert-only): per (account, source): `capital`, `fill` (cash, signed qty,
  price), `fee`, `slippage` (informational, the adverse price part), `borrow_fee`, `dividend`,
  `dividend_short`, `funding`, `liquidation_fee`, `cross`, `migration`. Every account cash-ledger row is
  attributed exactly once (cursor in the same transaction): fills by the order's allocations (a reduced
  fill pro rata), accruals pro rata by the sources' quantity in that symbol, deposits/resets to
  `_unallocated`. **Invariant (tested):** Σ sub-ledger cash = account cash and Σ sub-ledger qty = account
  qty per symbol. Exposure/P&L per source = its positions marked at the account's quotes (average-cost
  realised P&L; costs by kind).

### 17.6 Pre-trade and post-trade checks (`portfolio/checks.py`, `portfolio/risk.py`)

Per account settings (`GET/PUT /v2/portfolio/settings`, insert-only history `pf_settings`, every change
audited with reason; bounds enforced, 400 otherwise). Defaults (confirmed by the user 2026-10-24 where not set by §15):

| check | default | bounds |
|---|---|---|
| max order notional | 50 % of account equity and ≤ $1,000,000 | 1–100 %; $100–$10,000,000 |
| max position (after the order) | 50 % equities / 40 % crypto of equity | 1–100 % |
| price collar (quote vs a reference: the decision's reference price; else a fresh read of the latest quote — no reference → a risk-increasing order FAILS, a closing order passes with the reason, retest 2026-10-23) | 5 % equities / 10 % crypto | 0.5–50 % |
| buying power | broker formula (BP ≥ est. notional + fees; runtime orders are reduced like `target()`, manual orders rejected) | — |
| short / borrow | `allow_shorts`, `not_shortable`, Rule 201 (broker) | — |
| trading hours | equities 09:35–16:00 NY sessions only (protective orders queue) ; crypto 24/7 | — |
| duplicate guard | one working order per account + symbol; identical order (source, symbol, side, qty) within 60 s refused | 10–3600 s |
| daily order count | 100 per account per NY day (equities) and per UTC day (crypto) | 1–500 |
| **daily loss limit** | **2 %** of the day's reference equity (last snapshot before 00:00 NY for equity orders / 00:00 UTC for crypto orders) → no new risk-increasing orders until the next NY day / UTC day (reducing and protective orders still pass) | 0.5–20 % |
| **drawdown from peak** | **10 %** → that account's KILL: halt, cancel all, close all (protective `kill-switch` orders, queued while closed), auto trading OFF; `POST /v2/oms/resume` (user) clears the halt and rebases the peak | 2–50 % |

Each check result is stored with the OMS order (`checks: [{check, ok, message}]`). A failed check →
order `rejected` with that message (never silently skipped).

### 17.7 OMS (`execution/oms.py`) + broker adapter (`execution/brokers/`)

- **State machine** (persisted `oms_orders`, transitions checked in SQL, every transition an
  `oms_order_events` row, insert-only): `new → submitted → accepted → partially_filled → filled`, plus
  `cancelled`, `rejected`, `expired` (terminal: filled, cancelled, rejected, expired). Allowed:
  new→submitted|rejected|cancelled; submitted→accepted|partially_filled|filled|rejected|submitted (retry);
  accepted→partially_filled|filled|cancelled|rejected|expired; partially_filled→filled|cancelled|expired.
- **Idempotent client order ids:** runtime orders `rt-<execution id>-<symbol>-<leg>`; manual orders take
  the client's `client_order_id` (`^[A-Za-z0-9_-]{8,64}$`, the WPF sends a GUID) or get `man-<12 hex>`; a
  repeated id returns the stored order (200, nothing sent twice). **The order is stored (`new`) BEFORE it
  is sent**; then `submitted`, then the adapter call.
- **Adapter interface** (`brokers/base.py`): `account() -> dict`, `positions() -> list`, `submit(order) ->
  ack {broker_order_id, status: accepted|filled|rejected, filled_qty, avg_price, fees, reject_reason}`,
  `cancel(client_order_id) -> bool`, `order(client_order_id) -> ack|None`, `fills(after) -> [fill]`
  (poll-based fills stream), `capabilities {fractional, shorting, crypto_perps, hours}`, `name`, `paper:
  true`. The ONLY adapter now: `InternalPaperAdapter` (PaperBroker + `PlatformPaperStore`; the broker order
  row carries the client order id; one broker call = ONE SQLite transaction, so a crash never leaves half
  a fill). Stage 8 adds Alpaca paper + Kraken demo; IBKR/MT5 fit the same interface (TODO).
- **Retry policy:** a transient adapter error (`BrokerUnavailable`, e.g. the store locked) → 3 attempts
  (1 s, 3 s) within the call; still failing → stays `submitted`, retried by the next cycle; after 5
  attempts → `rejected` "broker unreachable — gave up after 5 attempts". **Crash between store and send:**
  at startup and every cycle, `new` orders older than 30 s → `cancelled` "not sent before a restart"
  (the intent is stale; the runtime re-decides); `submitted` → `order(client_order_id)`: known → state
  synced; unknown and younger than 5 min → re-sent (same id); older → `rejected` "lost before reaching the
  broker".
- **Partial fills:** a fill smaller than the order (reduced to buying power, clipped to the position) →
  `partially_filled`, and when the broker is done with it → `cancelled` with `filled_qty > 0` ("rest
  cancelled: …"). TIF: runtime orders `day` (queued equity orders not filled by the session end →
  `expired`), protective orders `gtc`.
- **Auto trading** (`POST /v2/oms/autotrade {enabled}`): off → the runtime still decides and records
  targets but sends no orders (decisions `not_executed: auto trading off`; incubation sessions count as
  missed). On refused while an account is halted. Heartbeat + kill turn it off (17.1).

### 17.8 Storage — hedgefund.db v15 (one transaction)

`pf_accounts`, `pf_settings`, `pf_allocations`, `pf_migrations` (insert-only); `pa_orders`
(+`account_id`, `client_order_id` UNIQUE), `pa_positions` (PK account_id, symbol), `pa_cash_ledger`,
`pa_equity` (PK account_id, ts), `pa_events`, `pa_meta` (PK account_id, key) — the paper broker's book;
`oms_orders` (state updated only through the transition function), `oms_order_events` (insert-only);
`sl_entries` (insert-only), `sl_snapshots` (PK account_id, source_id, day); `rt_decisions`,
`rt_executions` (insert-only); `incubation_runs`, `incubation_events` (insert-only — an UPDATE of
`length_sessions` is impossible by trigger). Audit actions: `account_opened`, `settings_changed`,
`allocation`, `incubation_started`, `incubation_aborted`, `incubation_completed`, `oms_order`,
`oms_cancel`, `autotrade`, `autotrade_heartbeat_off`, `platform_kill`, `platform_resume`,
`post_trade_halt`, `migration_dry_run`, `migration`.

### 17.9 Migration of the old paper account (`execution/migration.py`)

- `POST /v2/portfolio/migration/dry-run` → report (nothing written except an audit row): counts (orders,
  ledger rows, positions, equity points), cash, positions, equity at the latest marks, the old account's
  halt/autopilot state, blockers, the broker params that will be copied, the post-trade limits the
  `paper-council` account gets (drawdown kill = the old `drawdown_stop_pct`, daily loss 2 %).
- **Blockers:** old autopilot ON, a cycle in progress, pending (queued) orders in the old account, an
  unreadable old store, `paper-council` already existing, a previous migration.
- `POST /v2/portfolio/migration {confirm: "MIGRATE", reason}` → (1) freeze the old account
  (`migrating`, under the old engine's trade lock), (2) read it, (3) write `paper-council` + Council
  sub-ledger (`migration` entries) + `pf_migrations` row in ONE hedgefund.db transaction, (4) mark it
  `migrated`. Crash recovery at startup: `migrating` with a `pf_migrations` row → completes (4); without →
  un-freezes (nothing moved). **Parity (acceptance):** `paper-council`'s AccountView equals the old
  account's AccountView for the same quotes. Tests run the migration only on temp copies (never the real DB).

### 17.10 API (behind api_auth; strict bodies/queries — unknown fields → 400 `{error: "invalid input",
errors: {field: message}}`; other errors `{error, code, …}`; never a stack trace; times UTC ISO seconds)

`/v2/portfolio/*`: `GET /v2/portfolio/state`; `POST /v2/portfolio/accounts`; `GET
/v2/portfolio/accounts/{id}`; `GET /v2/portfolio/accounts/{id}/equity?since=`; `GET
/v2/portfolio/sources?account_id=`; `GET /v2/portfolio/sources/{source_id}/ledger?account_id=&limit=`;
`GET /v2/portfolio/allocation?account_id=`; `POST /v2/portfolio/allocation/rebalance`; `GET/PUT
/v2/portfolio/settings`; `GET /v2/portfolio/migration`; `POST /v2/portfolio/migration/dry-run`; `POST
/v2/portfolio/migration`.
`/v2/oms/*`: `GET /v2/oms/orders?account_id=&state=&limit=`; `GET /v2/oms/orders/{client_order_id}`;
`POST /v2/oms/orders`; `POST /v2/oms/orders/{client_order_id}/cancel`; `GET /v2/oms/fills?account_id=&after=`;
`POST /v2/oms/autotrade`; `POST /v2/oms/kill`; `POST /v2/oms/resume`; `GET /v2/oms/runtime`; `POST
/v2/oms/runtime/run`.
`/v2/incubation*`: `GET /v2/incubation?strategy_id=`; `GET /v2/incubation/eligibility?strategy_id=`; `POST /v2/incubation`; `GET /v2/incubation/{id}`;
`POST /v2/incubation/{id}/abort`. No route changes an incubation run's length.
Exact JSON: §17.11 (backend notes).

### 17.11 Backend notes — exact JSON for the WPF "Portfolio & Trading" view

Status: written FIRST as the binding payload contract (2026-10-17); implementation notes are added in
§17.12 when the backend is done (any deviation is listed there). All numbers are JSON numbers (never NaN —
null when unknown); money USD; percentages as the UI shows them (2.0 = 2 %); weights as fractions.

**AccountView** (= the old `/fund/state` `account` keys, from `PaperBroker.account`): `{equity, cash,
long_mv, short_mv, buying_power, peak_equity, drawdown_pct, maintenance_required, margin_excess,
short_exposure_pct, starting_capital, reset_at, day_pnl, total_pnl, total_pnl_pct, funding_total,
crypto_margin_excess}`.

**AccountSummary**:
```json
{"id": "paper-main", "label": "Platform paper account", "broker": "internal_paper", "paper": true,
 "origin": "opened", "opened_at": "2026-10-17T12:00:00+00:00",
 "account": {"equity": 100000.0, "cash": 100000.0, "...": "AccountView"},
 "valuation": {"ok": true, "message": null},
 "halt": {"active": false, "kind": null, "reason": null, "at": null},
 "day_loss": {"equity": {"pnl_pct": -0.4, "reference_equity": 100400.0, "limit_pct": 2.0, "blocked": false,
                         "until": null},
              "crypto": {"pnl_pct": -0.1, "reference_equity": 100100.0, "limit_pct": 2.0, "blocked": false,
                         "until": null}},
 "drawdown": {"pct": 1.2, "kill_pct": 10.0},
 "positions_count": 3, "working_orders": 1, "sources": ["_unallocated", "s-1a2b3c"]}
```
`account` is null and `valuation {ok: false, message: "valuation paused: no current quote for X"}` when a
held position has no current quote (no stored-price valuation); `halt.kind` `"kill" | "drawdown" | null`;
`day_loss.*.until` = when the block lifts (ISO) while `blocked`.

**GET /v2/portfolio/state**:
```json
{"now": "…", "paper_only": true, "notice": "Paper trading only · simulated · not investment advice",
 "autotrade": {"enabled": false, "heartbeat_minutes": 15, "last_client_seen": "…|null", "off_at": "…|null",
               "changed_at": "…|null", "changed_by": "user|engine|system|null"},
 "runtime": {"running": true, "cycle_in_progress": false, "last_cycle_at": "…|null", "next_cycle_at": "…|null",
             "last_error": null},
 "legacy_account": {"status": "active|migrating|migrated", "owner": "old engine|platform", "migrated_at": null,
                    "migration_id": null},
 "accounts": ["AccountSummary"],
 "can": {"open_account": true, "migrate": true}}
```

**POST /v2/portfolio/accounts** `{starting_capital?: 1000..100000000 (100000), label?: ≤ 60 chars}` → 201
`{"account": AccountSummary}`; 409 `{error, code: "account_exists"}` (stage 7: one opened account
`paper-main`).

**GET /v2/portfolio/accounts/{id}** → `{"account": AccountSummary, "positions": [Position], "working_orders":
[OmsOrder], "sources": [SourceSummary], "settings": Settings}`; Position = `{symbol, qty, asset_class,
instrument: "stock|spot|perp", side: "long|short", avg_price, last_price, market_value, weight,
unrealized_pnl, realized_pnl, opened_at, borrow_rate_pct, by_source: [{source_id, qty}]}` (values null
without a current quote). 404 `{error, code: "not_found"}`.

**GET /v2/portfolio/accounts/{id}/equity?since=ISO** → `{"account_id", "starting_capital", "reset_at",
"points": [{ts, equity, cash, long_mv, short_mv, drawdown_pct}]}` (≤ 5,000 newest points).

**SourceSummary** (`GET /v2/portfolio/sources?account_id=` → `{"account_id", "sources": [...]}`):
```json
{"source_id": "s-1a2b3c", "kind": "strategy", "name": "SMA 50/200 trend", "strategy_id": "1a2b…",
 "version": 3, "state": "incubation", "incubation_id": "inc-…", "allocation_weight": 0.4,
 "capital_in": 40000.0, "cash": 1200.5, "equity": 40890.1, "valued": true,
 "positions": [{"symbol": "SPY", "qty": 60, "avg_price": 612.1, "last_price": 615.0,
                "market_value": 36900.0, "unrealized_pnl": 174.0}],
 "pnl": {"realized": 12.0, "unrealized": 174.0, "total": 890.1},
 "costs": {"fees": 9.2, "slippage": 9.2, "borrow": 0.0, "funding": 0.0, "dividends": 0.0,
           "liquidation": 0.0},
 "exposure": {"long_mv": 36900.0, "short_mv": 0.0, "gross_pct": 90.2, "net_pct": 90.2}}
```
`kind` `strategy | council | unallocated`; `source_id` = the strategy id for strategies (ids look like
`s-1a2b3c4d5e6f`), `council-of-ais`, `_unallocated`; `pnl.total` = equity − capital_in; `costs.dividends` signed (+ received).

**GET /v2/portfolio/sources/{source_id}/ledger?account_id=&limit=1..1000 (200)** → `{"account_id",
"source_id", "entries": [{id, ts, kind, symbol, qty, price, amount, ref, note}]}` newest first; kinds as
§17.5.

**GET /v2/portfolio/allocation?account_id=** →
```json
{"account_id": "paper-main",
 "allocation": {"id": 4, "created_at": "…", "reason": "monthly rebalance", "actor": "engine",
                "pool": 100000.0, "unallocated_weight": 0.2,
                "weights": [{"source_id": "s-…", "name": "…", "weight": 0.4, "target_capital": 40000.0,
                             "vol": 0.12, "vol_source": "holdout", "mean_corr": 0.3, "penalty": 1.3,
                             "capped": true, "note": null}]},
 "params": {"vol_window": 60, "vol_min": 20, "corr_window": 120, "corr_min": 20, "lambda": 1.0,
            "max_weight": 0.4, "min_weight": 0.05, "max_total": 1.0, "drift_band": 0.05,
            "rebalance": "monthly"},
 "next_rebalance": "2026-11"}
```
`allocation` null before the first one; a source without a volatility estimate has `weight 0`, `vol
null`, `note` "no volatility estimate (…)". **POST /v2/portfolio/allocation/rebalance** `{account_id,
reason: 3..500}` → the same payload.

**Settings** (`GET /v2/portfolio/settings?account_id=` / `PUT /v2/portfolio/settings {account_id, reason:
3..500, changes: {key: number}}`) →
```json
{"account_id": "paper-main", "version": 2, "updated_at": "…", "updated_by": "user",
 "settings": {"max_order_notional_pct": 50.0, "max_order_notional_usd": 1000000.0,
              "max_position_pct_equity": 50.0, "max_position_pct_crypto": 40.0,
              "price_collar_pct_equity": 5.0, "price_collar_pct_crypto": 10.0,
              "duplicate_window_seconds": 60, "max_orders_per_day": 100,
              "daily_loss_pct": 2.0, "drawdown_kill_pct": 10.0,
              "alloc_max_weight": 0.4, "alloc_min_weight": 0.05, "alloc_max_total": 1.0,
              "alloc_lambda": 1.0, "alloc_drift_band": 0.05},
 "defaults": {"…": "same keys"},
 "bounds": {"daily_loss_pct": {"min": 0.5, "max": 20, "step": 0.1, "unit": "%",
                               "label": "Daily loss limit", "group": "post_trade"}}}
```
Groups: `pre_trade`, `post_trade`, `allocator`. Unknown key / out of bounds → 400 with `errors: {key: msg}`.

**Migration**: `GET /v2/portfolio/migration` → `{"legacy_account": {status, owner, migrated_at,
migration_id}, "migration": null | {id, created_at, account_id, summary}, "can_migrate": bool}`.
`POST /v2/portfolio/migration/dry-run {}` →
```json
{"ok": true, "blockers": [{"code": "autopilot_on", "message": "Switch the old autopilot off first."}],
 "legacy": {"orders": 120, "ledger_rows": 340, "equity_points": 900, "cash": 61210.0,
            "starting_capital": 100000.0, "peak_equity": 101200.0, "reset_at": "…",
            "halted": false, "autopilot": false, "pending_orders": 0,
            "positions": [{"symbol": "SPY", "qty": 60, "avg_price": 612.1}]},
 "target_account_id": "paper-council", "source_id": "council-of-ais",
 "settings": {"drawdown_kill_pct": 8.0, "daily_loss_pct": 2.0},
 "broker_params": {"fees_bps": 5.0, "...": "fund params keys used by the broker"},
 "note": "The old engine keeps analysing (recommendations) but no longer trades this account."}
```
`POST /v2/portfolio/migration {confirm: "MIGRATE", reason: 3..500}` → 200 `{"migration": {id, created_at,
account_id, summary}, "account": AccountSummary}`; 400 `confirm` ≠ "MIGRATE"; 409 `{error, code:
"migration_blocked", blockers: [...]}`.

**OmsOrder**:
```json
{"client_order_id": "rt-ex1a2b3c4d-SPY-1", "account_id": "paper-main", "created_at": "…",
 "updated_at": "…", "symbol": "SPY", "asset_class": "equity", "side": "buy", "qty": 60,
 "filled_qty": 60, "avg_fill_price": 615.15, "fees": 9.2, "notional": 36909.0,
 "state": "filled", "terminal": true, "origin": "runtime", "tif": "day", "reason": "strategy rebalance",
 "ref_price": 615.0, "broker_order_id": "17", "reject_reason": null, "attempts": 1,
 "allocations": [{"source_id": "s-…", "qty": 60}],
 "checks": [{"check": "max_order_notional", "ok": true, "message": null}],
 "execution_id": "ex-…"}
```
`origin` `runtime | manual | kill | drawdown | margin` (`margin` = a broker margin call / liquidation
recorded by the OMS); `state` `new | submitted | accepted | partially_filled | filled | cancelled |
rejected | expired`.
- `GET /v2/oms/orders?account_id=&state=&limit=1..500 (100)` → `{"orders": [OmsOrder]}` newest first.
- `GET /v2/oms/orders/{client_order_id}` → `{"order": OmsOrder, "events": [{id, ts, from_state,
  to_state, note}]}`.
- `POST /v2/oms/orders {account_id, symbol, side: buy|sell|short|cover, qty? | notional?, source_id?
  (default: _unallocated on paper-main, council-of-ais on paper-council), reason?: ≤ 500,
  client_order_id?}` → 201 `{"order"}` (a failed pre-trade check is an order in state `rejected` with
  its `checks`), 200 `{"order"}` for a repeated client_order_id; 409 `{error, code: "halted" |
  "not_open"}`; 404 account.
- `POST /v2/oms/orders/{client_order_id}/cancel {}` → 200 `{"order"}`; 409 `{error, code:
  "not_cancellable"}`.
- `GET /v2/oms/fills?account_id=&after=0&limit=1..500` → `{"fills": [{id, ts, client_order_id, symbol,
  side, qty, price, fees, notional}], "next_after": 42}` oldest first.
- `POST /v2/oms/autotrade {enabled: bool}` → `{"autotrade": {...as in state}}`; 409 `{error, code:
  "halted"}` when enabling while an account is halted.
- `POST /v2/oms/kill {reason?: ≤ 200}` → `{"platform": {"ok": true, "error": null, "accounts":
  [{"account_id", "cancelled": 2, "orders": [OmsOrder]}]}, "legacy": {"ok": true, "error": null,
  "halted": true}}` (always 200; each half reports its own failure).
- `POST /v2/oms/resume {account_id, reason: 3..500}` → `{"account": AccountSummary}` (auto trading stays
  off; the peak is rebased to the current equity; 503 when the account cannot be valued).
- `GET /v2/oms/runtime` → `{"running", "cycle_in_progress", "last_cycle_at", "next_cycle_at",
  "last_error", "cycles": [{"started_at", "finished_at", "status": "ok|partial|error|skipped", "summary":
  {"decisions": n, "orders": n, "errors": {key: msg}}}]}` (last 50, kept in memory).
- `POST /v2/oms/runtime/run {}` → 202 `{"queued": true}`.

**IncubationRun**:
```json
{"id": "inc-1a2b3c4d5e6f", "strategy_id": "…", "strategy_name": "…", "version": 3,
 "account_id": "paper-main", "source_id": "s-…", "length_sessions": 60, "calendar": "nyse",
 "start_date": "2026-10-19", "end_date": "2027-01-13", "created_at": "…", "created_by": "user",
 "reason": "…", "status": "running",
 "progress": {"sessions_elapsed": 12, "sessions_total": 60, "traded": 11, "missed": 1,
              "last_session": "2026-11-03"},
 "realised": {"return": 0.012, "log_return": 0.0119, "vol": 0.11, "sharpe": 1.4, "max_drawdown": 0.021,
              "equity": 40480.0},
 "expected": {"mean_daily": 0.0004, "std_daily": 0.008, "vol": 0.127, "max_drawdown": 0.09,
              "source_run_id": "bt-…", "sessions": 504},
 "band": {"lo": -0.06, "hi": 0.07, "n": 11}, "tracking_error": 0.05,
 "result": null, "result_reasons": [], "live_eligible": false, "ended_at": null, "end_reason": null}
```
`status` `running | completed | aborted`; `result` `passed | failed | inconclusive | null`.
- `GET /v2/incubation?strategy_id=` → `{"runs": [IncubationRun], "defaults": {"length_sessions": 60, "min":
  20, "max": 250}}` newest first.
- `GET /v2/incubation/eligibility?strategy_id=` → `{"eligible": bool, "reason": null|str, "calendar":
  "nyse|utc", "defaults": {...}, "account_id": "paper-main", "start_date_if_now": "YYYY-MM-DD"}`.
- `POST /v2/incubation {strategy_id, length_sessions: 20..250, reason: 3..500, account_id?:
  "paper-main"}` → 201 `{"run": IncubationRun, "strategy": {id, state, current_version}}`; 404; 409
  `{error, code: "not_validated" | "already_incubating" | "account_not_open" | "halted" | "locked"}`.
- `GET /v2/incubation/{id}` → `{"run": IncubationRun, "events": [{id, ts, event, data}], "decisions":
  [{id, bar_date, created_at, status: "pending|executed|missed|failed|not_executed", reason, targets:
  {symbol: weight} | null, force: [symbol], error}]}` (newest 300 events / 100 decisions).
- `POST /v2/incubation/{id}/abort {reason: 3..500}` → `{"run", "strategy"}`; 409 `{error, code:
  "not_running"}`.

### 17.12 Stage 7 backend notes (implemented 2026-10-17: §17.1–§17.11, backend part — for the WPF "Portfolio & Trading" view)

All §17.11 payloads are implemented as written, with these additions/deviations (the WPF agent binds to them):
- **Source ids:** a strategy's source id IS its strategy id (`s-<12 hex>`, e.g. `s-1a2b3c4d5e6f`), not `s-<id>`.
  Fixed sources: `_unallocated` (paper-main's deposit and every capital not allocated), `council-of-ais`
  (paper-council). `SourceSummary.capital_in` of the Council = the old account's starting capital (so
  `pnl.total` continues the old total P&L).
- **Settings defaults** (final; the contract's first numbers were too tight for one strategy at the 40 % allocator
  cap): max order notional 50 % of equity and $1,000,000; max position 50 % (equities) / 40 % (crypto); 100 orders
  per class and day; the rest as §17.6 (**confirmed by the user 2026-10-24**). `PUT /v2/portfolio/settings` writes a FULL new
  settings row (`version` = number of rows) — `changes` are merged into the current values.
- **Extra keys** (additive): `OmsOrder` also has `notional_req` (a manual notional order), `allow_reduce`;
  allocation `weights[]` have `target_capital`, `allocation.transfers: [{source_id, amount}]`; kill `orders` entries
  are OmsOrders or `{symbol, state: "error", reject_reason}`; the incubation `decisions` list may start with an
  in-memory entry `{id: null, status: "failed", error, created_at}` = the latest failed decision attempt (data not
  there yet, source down) — it is retried every cycle and never persisted as a stale signal.
- **Audit 2026-10-18 (§17.0, WPF-relevant):** `POST /v2/oms/orders` `client_order_id` = `^[A-Za-z0-9_-]{8,64}$`
  (send a GUID, `Guid.NewGuid().ToString()`); the prefixes `rt-`, `run-`, `kill-`, `kil-`, `ddk-`, `dra-`, `mar-`
  (any case) are reserved → 400 `{"error": "invalid input", "errors": {"client_order_id": "the prefixes rt-, run-,
  kill-, kil-, ddk-, dra-, mar- are reserved for the engine"}}`; other bad ids → `"8..64 characters [A-Za-z0-9_-]
  (e.g. a GUID)"`. `GET /v2/oms/orders/{client_order_id}` and `…/cancel` still accept every stored id
  (`[A-Za-z0-9_.:-]{8,80}`, runtime ids carry symbols). `/fund/kill` always has `platform` now (when the stage-7
  wiring failed at startup: `{ok: false, error: "RuntimeError: Strategy Platform stage 7 wiring failed at startup
  (…)", accounts: []}`) — show it like any other half failure.
- **Old engine (`fund/`, recorded in fund/SPEC.md §16):** `GET /fund/state` adds `legacy_account {status:
  active|migrating|migrated, owner: "old engine"|"platform", migrated_at, migration_id}`; `POST /fund/kill`'s
  response adds `platform` (the platform half: `{ok, error, accounts}`); after the migration every trading path
  answers 409 with "This paper account was migrated to the Strategy Platform on <date> — it is traded in Portfolio
  & Trading now."; the cycle is advisory (summary `account_migrated`); the old kill only HALTS the frozen account
  (the platform half closes the real positions in paper-council).
- **Files:** `portfolio/{decision.py, allocator.py, allocation.py, netting.py, ledger.py, checks.py, risk.py,
  settings.py, views.py}`, `execution/{paper_store.py, accounts.py, oms.py, control.py, migration.py, errors.py,
  brokers/{base.py, paper.py}}`, `live/{runtime.py, incubation.py}`, `api/trading_routes.py` (mixin of `V2Api`,
  28 routes appended to `routes.ROUTES`), `storage/db.py` (**v15**, 20 tables), `lifecycle/registry.py`
  (`incubation_transition`, generic route refuses `validated → incubation` / `incubation → validated`, leaving
  incubation by another transition ends the run, live message), `backtest/engine.py` (the decision block now calls
  `portfolio/decision.decide` — no behaviour change, `ENGINE_VERSION` unchanged, all backtest/parity tests green),
  `fund/{engine.py, store.py}` (§17.1 hooks), `main.py` (wiring: `accounts.configure(AlpacaFeed)`, kill/migrated
  hooks, `migration.recover()`, auto trading forced off, the runtime task).
- **Choices** (confirmed by the user 2026-10-24): the runtime's first decision is for the session BEFORE `start_date` (so the
  first execution happens on `start_date`); stop-loss / take-profit checks use the split-adjusted close of the
  decision bar (the fill basis); a strategy that left incubation keeps its positions until the next tradable
  moment, then its source is closed to 0 (orders `runtime`), and its cash returns to `_unallocated` once flat
  (allocation reason "release capital …"); ADV for the participation cap = the mean of the last 20 complete SIP
  dollar volumes (no complete 20 → that equity symbol's order is refused, as in the backtest); accruals are
  attributed per share × each source's SIGNED quantity (a source short against another's long pays the lender's
  share); orders without allocations (kill, broker margin calls) go to the sources holding the symbol in the
  reducing direction; incubation band uses daily LOG returns of the holdout card's curve; `tracking_error` = the
  annualised RMS deviation of the realised daily return from μ; sessions without an executed decision (auto
  trading off, backend down, window missed, halted) count as missed. Market orders only (limit/stop/bracket and
  reconciliation come with the real brokers, stage 8).
- **Startup / recovery:** `migration.recover()` (complete or undo an interrupted migration), auto trading forced
  OFF, the runtime's first cycle (≤ 60 s) runs `oms.sync` = the crash-between-store-and-send recovery (§17.7).
- **Known limits:** the runtime keeps its cycle list and the last decision errors in memory (lost on restart);
  quotes shown by the GETs are the runtime's last quotes (a held symbol without one shows null values and
  `valuation.ok false` until the next cycle); IBKR/MT5 adapters are not built (TODO, interface in
  `execution/brokers/base.py`).
- **Tests** (all offline, temp databases only, never the real DBs): `test_sp_oms.py` (16: transitions,
  idempotency, stored-before-sent, retry policy, crash between store and send, one broker call = one transaction,
  partial fill, day expiry, cancel, margin-order import), `test_sp_pretrade.py` (11: every pre-trade check),
  `test_sp_risk_controls.py` (10: drawdown kill + stale-mark guard, daily-loss events, auto trading, heartbeat
  shared with the old engine, kill switch covering both engines, each half's own failure, resume),
  `test_sp_portfolio.py` (12: allocator math, transfers, netting, splits, attribution invariant),
  `test_sp_trading_parity.py` (6: order parity incl. borrow + perp funding, target parity runtime vs the old
  `target()`, migration dry run / blockers / real run on temp copies / AccountView parity / coexistence / crash
  recovery / failed copy un-freezes), `test_sp_incubation.py` (8: immutability, rules, abort/restart with a new
  length, retire / new version end the run, live stays off, runtime end to end, data-not-there-yet,
  passed/failed/inconclusive), `test_sp_trading_api.py` (7), `test_fund_migrated.py` (2: the advisory old engine).
  `test_sp_families_lifecycle.py` updated (incubation text of stage 7).

- **Stage 10 addition (user, 2026-10-17):** replace the blanket "not
  financial advice" line across the WHOLE app with context-specific notices
  held centrally (Shared/AiContent.cs): (a) analyses/explanations/news
  summaries — "AI-generated analysis — may be inaccurate; information, not a
  recommendation."; (b) strategies, committee memos, loop finalists —
  "AI-generated research output for your own decision. Not a personal
  recommendation by a licensed advisor; you decide and carry the risk.
  Backtests are hypothetical."; (c) autonomous execution — "Automated trading
  on your own paper account under the limits you set. Paper only — no real
  money."; (d) a separate explicit notice if live trading is ever enabled.
  Background: own-use tool ≠ regulated advice; a sold product with
  recommendations or autonomous trading for others would likely need a
  licence (WpIG/KWG, MiFID II) — no disclaimer changes that; legal check stays
  a precondition for "sellable".

- **Project direction (user, 2026-10-17):** built to be shippable IN THEORY
  (product-grade quality, licensing-safe "tool" design — the user builds,
  checks and decides), but NOT shipped: it stays the user's own project,
  trading at most with the user's own money and accounts; no features whose
  only purpose is serving other users. (Claude Project: claude/project-direction.md)
