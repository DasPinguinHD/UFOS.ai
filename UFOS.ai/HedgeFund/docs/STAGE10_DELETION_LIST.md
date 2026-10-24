# Stage 10 deletion list — approved by the user 2026-10-24

Status: **approved by the user 2026-10-24** (as drafted, with ONE exception: the real Trade Republic export under
`data/backups/tr-import/` stays — see D / E). Nothing deleted or renamed yet; execution at stage 10. Prepared read-only from `HedgeFund/docs/CODEMAP.md`, `docs/todo.md`
("Stage 10 Removal of old `fund/` code & hardening", "Your items before stage 10") and SP §15. Rules for stage 10
(SP §15 / todo): approved rows are first **renamed to `*.stale`**; deleted only after a full test run + build + your
click-test; the Council of AIs is migrated into the platform FIRST (parity test against the old engine) — rows marked
**after the Council migration** are blocked until then. Never the real databases (`data/hedgefund.db`,
`newsroom.db`, `market_research.db`, `options_archive.db`) or the current sandbox copies.

How "references" was checked: `grep` of Python imports (`from fund…`, `import fund…`, module names) in `backend/`
(without `.venv` / `data`) and of C# type / route names in `HedgeFund/*.cs` (without `bin` / `obj`), 2026-10-24.
Multi-line imports may hide a few more users — every row is re-checked when it is executed.

Actions: **delete** = rename to `*.stale` at stage-10 start, delete after the green run · **move, then delete** = the
code still used by the platform moves into `strategy_platform/` first · **keep** = stays (reason given).

Important at the time of writing: **sandbox mode is ON** (`data/SANDBOX_ACTIVE`, created 2026-10-24 08:44 UTC by
`tools/sandbox.py on`) — the current sandbox copies and the marker must not be touched until `tools/sandbox.py off`.

---

## (A) Code

### A1 Old engine `backend/fund/` (8 739 lines) — all **after the Council migration**

| Path | What it is | Why it can go | Still referenced by (blockers) | Risk | Proposed action |
|---|---|---|---|---|---|
| `fund/engine.py` (2 074) | Old engine: scheduling, cycles, halt / kill, `/fund/state` | Replaced by the migrated Council strategy + platform runtime / control | `main.py` (starts `FundEngine`), `ai_budgets.py` (`EngineError`), `fund/api.py`; kill switch acts on both engines (`execution/control.py`) | High — the app's kill switch and the Overview read it | after the Council migration: delete (+ `main.py` wiring, control.py second engine) |
| `fund/api.py` (321) | `/fund/*` routes (21 routes) | No engine → no routes | `main.py` (`fund_api.register`); WPF `Fund/FundClient.cs`, `HedgeFundWindow.xaml.cs`, `HedgeFundWindow.Trading.cs`, `Fund/FundNotifications.cs` (`/fund/events`), `Newsroom/NewsroomBackend.cs`, `MarketResearch/ResearchView.Integrations.cs`, `Platform/PlatformClient.{Brokers,Trading}.cs` (call or mention `/fund/*`) | High — WPF breaks if routes vanish first | after the Council migration: delete together with the WPF callers (A4) |
| `fund/judge.py` (599), `fund/agents/{technical,risk,sentiment}.py` (128–337) | Rule score + LLM judge; the agents | Become the Council strategy's logic (migrated, parity-tested) | only inside `fund/` (engine, backtest, llm) | Medium — parity test must pass first | after the Council migration: delete |
| `fund/agents/macro.py` (341) | Macro agent | Logic needed by the platform | `strategy_platform/strategy/features/macro_regime.py` imports it | Medium | after the Council migration: **move, then delete** (into `strategy/features/`) |
| `fund/sizing.py` (157), `fund/indicators.py` (187), `fund/backtest.py` (513), `fund/export.py` (121) | Old sizing, indicators, rule-only backtest, CSV exports | Platform has its own (backtest engine, metrics, SDK) | only inside `fund/` (engine, backtest, api) | Low | after the Council migration: delete |
| `fund/broker.py` (1 344) | `PaperBroker` simulation | — | **used by the platform**: `backtest/engine.py`, `execution/accounts.py`, `brokers/mirror.py`, `brokers/paper.py`, `oms.py`, `control.py`, `live/runtime.py`, `portfolio/{views,netting}.py` (`PaperBroker`, `StateUnavailable`, `round_qty`, `fmt_qty`, `ValuationPaused`) | High — backtests + paper accounts run on it | **move, then delete**: move to `strategy_platform/execution/paper_broker.py` (no behaviour change, parity tests), then remove the `fund/` copy |
| `fund/params.py` (398) | Parameters / constants | — | **used by 24 platform modules** (`backtest/*`, `execution/*`, `brokers/*`, `portfolio/*`, `live/runtime.py`, `api/{broker,trading}_routes.py`, `ai_budgets.py`) | High | **move, then delete** (into `strategy_platform/core/`) |
| `fund/types.py` (238) | `Bar`, `Quote`, `OrderRequest` dataclasses | — | `backtest/engine.py`, `brokers/{paper,binance_demo}.py` | Medium | **move, then delete** |
| `fund/llm.py` (485) | OpenRouter client + budget `fund` | — | `ai_budgets.py`, `backtest/explain.py`, `discovery/loop.py`, `monitoring/reports.py`, `research/{committee,paper_strategy,studio}.py` | High — all AI features use it | **move, then delete** (into `strategy_platform/core/llm.py`; budget names stay) |
| `fund/market.py` (369) | Alpaca feed, NYSE helpers | — | `main.py` (`AlpacaFeed`), `live/runtime.py`, `fund/broker.py` | High | **move, then delete** |
| `fund/store.py` (958) | Old-engine SQLite access (newsroom.db tables) | Engine gone → only migration code reads it | `main.py`, `execution/migration.py` (old account → `paper-council`), `fund/params.py` / `broker.py` / `llm.py` | Medium — the one-way migration needs it while an unmigrated old account may exist | after the Council migration + after the move of params / broker / llm: delete (keep a read-only reader only if `migration.py` stays) |
| `fund/__init__.py`, `fund/agents/__init__.py` | Package markers | — | everything above | — | delete last, when `fund/` is empty |

### A2 Retired Kraken demo (SP §18.13, retired 2026-10-19)

| Path | What it is | Why it can go | Still referenced by (blockers) | Risk | Proposed action |
|---|---|---|---|---|---|
| `strategy_platform/execution/brokers/kraken_demo.py` (851) | Kraken Futures demo adapter | Kraken decommissioned the demo | `brokers/service.py` (lazy import), `brokers/mirror.py` (lazy import `KrakenDemoAdapter`); tests (B2) | Medium | delete, removing the two lazy-import branches in the same change |
| `kraken-demo` branches in `brokers/{service,mirror,reconcile,live_guard}.py`, `execution/accounts.py`, `api/{broker,trading}_routes.py`, `execution/oms.py` | Account / routing / reconcile branches | Dead code since the retirement | each other | Medium | delete the branches; **keep** `accounts.RETIRED` + its 409 / BLOCKED handling while a `kraken-demo` row can exist in your hedgefund.db (see E) |
| `strategy_platform/secrets.py` entries `kraken-demo-api-key` / `-secret` | Credential names | No adapter | WPF `Platform/CredentialStore.cs`, test kit `KEYS` | Low — keys stay in the Credential Manager until you remove them in ⚙ Keys & data | delete (WPF + backend in one change) |
| WPF: `Platform/CredentialStore.cs` (Kraken entries), `KeysAndDataWindow.xaml(.cs)`, `KeysAndDataWindow.Brokers.cs`, `BrokerDialogs.cs`, `PlatformClient.Brokers.cs`, `TradingView.cs` (kraken-demo texts / branches) | UI for the retired broker | Retired | each other | Medium — WPF build only on your PC | delete the branches; your build + click-test |
| `requirements.txt` line 17 comment ("only the kraken-demo broker becomes unavailable") | Comment | Outdated | — | none | reword to binance-demo |

### A3 Tax-export leftovers (SP §19.15, tax export removed 2026-10-18)

| Path | What it is | Why it can go | Still referenced by | Risk | Proposed action |
|---|---|---|---|---|---|
| `strategy_platform/monitoring/tax_export.py.stale` | Removed tax export | Your decision 2026-10-18 | none (`.stale`, not imported) | none | delete |
| audit rows `tax_export` in hedgefund.db | History | — | — | — | **keep** (insert-only audit trail; see E) |
| `"tax_export": false` in `monitoring/tax_notice.py` + `PlatformClient.Monitoring.cs` | The notice contract flag | — | tests `test_sp_monitor_fx`, `test_sp_monitor_shadow` | — | **keep** (part of the fixed-notice payload) |

### A4 WPF old-engine parts — all **after the Council migration**

| Path | What it is | Still referenced by (blockers) | Proposed action |
|---|---|---|---|
| `HedgeFundWindow.xaml.cs` old-engine dashboard (polls `/fund/state`, chips, agents, verdict, orders, kill / resume) + `HedgeFundWindow.Trading.cs` `/fund/*` calls | The engine dashboard | stage-11 Overview redesign replaces it; kill switch toolbar | after the Council migration: remove the `/fund/*` parts (stage 11 rebuilds the Overview) |
| `Fund/FundClient.cs` `/fund/*` methods + DTOs | Typed engine client | `FundClient` base pieces are used by `ResearchClient`, `PlatformClient`, `KeysAndDataWindow.Network`, `ValidationDialogs`, `FundNotifications` | **move, then delete**: keep the shared HTTP / token base (rename later), delete only the `/fund/*` methods |
| `Fund/FundNotifications.cs` `/fund/events` polling | Engine toasts | toast infrastructure used by `ResearchAlertNotifier`, `MonitoringNotifier` | delete only the `/fund/events` source; keep the toast host |
| `Fund/EquityChart.cs` | Equity line chart | `Platform/BacktestCharts.cs`, `HedgeFundWindow.xaml.cs` | **keep** (used by the platform) |

### A5 §17.9 migration — **added 2026-10-24, user-approved** (SP §22 D12)

| Path | What it is | Why it can go | Still referenced by (blockers) | Risk | Proposed action |
|---|---|---|---|---|---|
| `strategy_platform/execution/migration.py`, the routes `/v2/portfolio/migration*` (in `api/trading_routes.py`), the WPF migration dialog | One-way migration of the old paper account into `paper-council` (SP §17.9) | The switch-over R2 requires the migration done — afterwards it has no use | `fund/store.py` (old-store reader), its tests, the WPF Trading view | Low after R2 | at R4: rename to `*.stale`, delete in the R4 / R5 deletion step (with the rest of `fund/`) |

---

## (B) Tests

| Path | What it is | Why it can go | Still referenced by (blockers) | Risk | Proposed action |
|---|---|---|---|---|---|
| `tests/test_fund_{agents,api,backtest,engine,export,indicators,judge,sizing,crypto,crypto_engine,crypto_misc,crypto_review,review_fixes,migrated}.py` (14 files, 233 tests) | Old-engine tests | Their code goes (A1) | the code in A1 | Low once A1 is gone | after the Council migration: delete with their modules |
| `tests/test_fund_{broker,market,params,store}.py` (4 files, 103 tests) | Tests of code that MOVES | Move with the code | A1 move rows | Medium | **move** with `broker.py` / `market.py` / `params.py` / `store.py` (renamed `test_sp_…`) |
| `tests/fund_testkit.py`, `tests/crypto_testkit.py` | Test kits | Used by the fund tests | **`tests/test_sandbox_profile.py`** also imports them | Low | after the Council migration: move the parts `test_sandbox_profile` needs into its own kit, then delete |
| `tests/test_sp_trading_parity.py`, `tests/test_sp_backtest_parity.py` | Parity platform ↔ old engine / PaperBroker | — | — | — | **keep** until the migration + broker move are proven; then re-point to the moved broker |
| `@legacy_kraken` tests: `test_sp_broker_{format(1),incubation(2),oms(2),reconcile(1),review_fixes(1)}.py`, `KrakenAdapterTest` + `FakeKraken` in `test_sp_broker_adapters.py`, the Kraken parts of `sp_broker_testkit.py` (`legacy_kraken`, `kind == "kraken"`, Kraken keys) | Dormant Kraken mechanics | Adapter goes (A2) | `kraken_demo.py` | Low | delete with A2 |
| `tests/test_sp_broker_retired.py` → `RetiredToolTest.test_stale_verify_tool_exits_early` | Checks the `.stale` Kraken verify tool | The tool goes (C) | **`tools/verify_kraken_demo.py.stale`** | Low | delete this test with the tool; **keep** `RetiredTest` (409 / BLOCKED while a kraken-demo row exists) |
| `tests/test_sp_broker_verify_tool.py.stale`, `tests/test_sp_monitor_tax.py.stale`, `tests/test_sp_data_{refresh,volume,wait_latest}.py.stale` | Retired tests (already `.stale`) | Their modules are gone | none | none | delete |
| `test_sp_committee` (flaky) | — | — | — | — | **keep** — make deterministic (todo, stage 10) |

---

## (C) Backups / `*.stale` files under `HedgeFund/` (260 `.stale` files, ≈ 474 MB — 451 MB of it old sandbox DB copies)

None of them is compiled or imported (`*.csproj` compiles `*.cs` only; unittest discovers `test_*.py` only); the
only code that WRITES `.stale` names is `tools/sandbox.py` (on / off) and `MarketResearch/Chart/tests/fetch_engine.py`
— both keep working without the old files. Proposed action for every group: **delete** (they are already the
"renamed" stage), after one final look by you.

| Folder | Files | Size | What they are |
|---|---:|---:|---|
| `HedgeFund/` (root) | 8 | 1.39 MB | `HedgeFundWindow.xaml(.cs)` pre-dlgguard / fix1023 / maxwin / review / sandbox / timeout / zorder copies |
| `HedgeFund/Platform/` | 93 | 4.46 MB | Pre-change copies of the Platform views / clients / dialogs (pre-fix1023 r1–r3, pre-sandbox, pre-timeout, pre-dlgguard, pre-wheel, pre-os1r2 …), 2026-10-03 … 10-24; one `_commit_test.txt.stale` |
| `HedgeFund/Fund/` | 7 | 0.34 MB | `FundClient.cs` / `FundWindows.cs` pre-change copies |
| `HedgeFund/MarketResearch/` (+ `Chart/`, `Chart/vendor/`) | 8 | 0.40 MB | Pre-change views; `chart.js` pre-crossfix; replaced KLineChart vendor files (2026-10-18) |
| `HedgeFund/Newsroom/` | 2 | 0.02 MB | `FinancialNewsroomWindow.xaml.cs` pre-maxwin / zorder |
| `backend/` (root) | 2 | 0.05 MB | `main.py`, `sandbox_profile.py` pre-change copies |
| `backend/__pycache__/` | 2 | 0.01 MB | `sp3d_patch_{lifecycle,spec}.py.stale` (old one-off patch scripts) |
| `backend/fund/`, `backend/market_research/`, `backend/newsroom/` | 6 | 0.23 MB | Pre-sandbox / pre-fix1023 copies |
| `backend/strategy_platform/**` | 37 | 2.02 MB | Pre-change copies (api routes, brokers, control, incubation, decay, storage/db.py, SPEC.md …) + `data/refresh.py.stale`, `monitoring/tax_export.py.stale` |
| `backend/tests/` | 21 | 0.33 MB | Pre-change test copies + the retired tests listed in (B) |
| `backend/tools/` | 1 | 0.02 MB | `verify_kraken_demo.py.stale` (blocker for `RetiredToolTest`, see B) |
| `backend/data/` (root) | 33 | 4.80 MB | Old SPEC / todo / ai-budgets-spec snapshots, `hfsrc*.tgz.stale` (4), `maxwin-src.tgz.stale`, `fixpatch_*` (5) and `sbxpatch_*` (5) patch scripts, `patch_*.py.stale` (3), `spec*.part / *-wpf-append.md.stale` (7, incl. `spec1913-wpf-append.md.stale` already marked "safe to delete by hand"), `SANDBOX_ACTIVE.<stamp>.stale` (2 old markers), `s9_transfer_test.txt.stale`, `MainWindow.xaml.cs.pre-review-20261022.stale` |
| `backend/data/backups/` (`.stale` only) | 16 | 8.93 MB | Pre-change SPEC / MR-SPEC / CODEMAP / todo / module copies, `incomplete-attempt.db(-journal).stale` (2026-10-02) |
| `backend/data/maxwin0-backup/` | 5 | 0.22 MB | Agent backups of files OUTSIDE HedgeFund (MainWindow, Global Monitoring, LiveStreamAgent, TransactionLog, ArticleWindow) before the maxwin change |
| `backend/data/sandbox0-backup/` | 3 | 0.13 MB | Agent backups of `MainWindow.xaml(.cs)` / `IPortfolioInsightProvider.cs` before the sandbox change |
| `backend/data/sandbox/` (`.stale` only) | 12 | 451 MB | Two OLD sandbox copy sets (`*.20261023T213850Z.stale`, `*.20261024T084428Z.stale`: hedgefund / newsroom / market_research + wal / shm) — NOT the current copies |
| `backend/data/scope-guard/` (`.stale` only) | 4 | 0.04 MB | `spec7…10-incoming.md.stale` (old spec drafts) |
| **Total** | **260** | **≈ 474 MB** | |

---

## (D) Runtime data (`backend/data/`, git-ignored) — never the real DBs

| Path | What it is | Why it can go | Still referenced by | Risk | Proposed action |
|---|---|---|---|---|---|
| `data/agent_transfer/*.tgz` (9 files, ≈ 35 MB) + `verify_alpaca_options.py.pre_os1` | Agents' test bundles (code only, no data, no secrets) | One-off transfers | agents re-create them per run | none | delete (after the current agent round) |
| `data/backups/hedgefund.db.pre-v23-20261020T145740Z` (204 MB) | Automatic backup before migration v23 | DB is at v24 and verified | — | Medium — the only pre-v23 copy | delete **after** a fresh full backup is taken at the start of stage 10 |
| `data/backups/pre-test-20261018/` (hedgefund / market_research / newsroom .db, 215 MB) | Manual backup before the 2026-10-18 tests | Superseded | — | Medium | delete after the fresh stage-10 backup |
| `data/backups/newsroom-before-strategy-platform-2026-10-02.db` (6.4 MB) | newsroom.db before the platform start | Old-engine data is migrated (`paper-council`) | — | Low–medium | delete after the fresh stage-10 backup (or keep as the last pre-platform copy — your call) |
| `data/backups/tr-import/` (`Transaktionsexport.csv`, a Shadow HTML report) | **Your real Trade Republic export** used to verify the import (2026-10-18) | — | — | Personal financial data on disk | **keep** (user decision 2026-10-24; see E) |
| `data/os0/verify_alpaca_options_*.json` (3) | Your OS0 check reports (timed-out runs 2026-10-23) | Superseded by the fixed tool's run | — | none | delete after your successful OS0 run |
| `data/backtests/`, `data/scenarios/` | Empty | — | backtest / stress code writes there | — | **keep** (folders used by code) |

---

## (E) Keep-list

| Path / item | Reason |
|---|---|
| `requirements-broker.txt`, the optional ccxt install step in `run_hedgefund_backend.ps1`, `execution/brokers/ccxt_support.py` | The Binance Spot demo uses them (SP §18.14) |
| `strategy_platform/data/providers/kraken_public.py` | Kraken PUBLIC market data (crypto prices for Shadow / insights) — a data source, not the retired demo broker |
| `execution/accounts.RETIRED` + `test_sp_broker_retired.RetiredTest` | Your hedgefund.db may still hold a `kraken-demo` account / route; it must stay BLOCKED (409), never silently re-routed |
| `storage/db.py` CHECK lists with `'kraken-demo'` / `'kraken_demo'` (migrations v18 / v23) | Migration history of an insert-only DB; changing old migrations breaks existing DBs |
| Audit rows `tax_export`, `kraken…` and every other audit row | Insert-only audit trail (by design) |
| Old-engine tables in `newsroom.db` (`prices`, `signals`, `orders`, `positions`, `equity_curve`, `fund_*`) | Real DB — dropped only by a future migration with your separate decision |
| `data/SANDBOX_ACTIVE` + `data/sandbox/{hedgefund,newsroom,market_research}.db(-wal/-shm)` | **Sandbox mode is ON** (2026-10-24) — live marker + current copies |
| `data/scope-guard/manifest-*.json` (7) | Used by the scope-guard checks (stage0, mr0, budget0, insight0, …) |
| `data/logs/*` | Live logs (rotated by their writers) |
| `data/backups/tr-import/` (your real Trade Republic export + Shadow report) | User decision 2026-10-24: stays. Git-ignored via `data/backups/` in `backend/.gitignore` and not in the git index — it never leaves this PC through the repo |
| `Fund/FundWindows.cs` (UI kit), `Fund/FundFormat.cs`, `Fund/FundSettings.cs`, `Fund/EquityChart.cs` | Shared UI kit / settings / chart used across Platform, Research, Newsroom |
| `tests/test_sp_*parity*.py` | Parity proof for the migration + broker move |
| `HedgeFund/docs/preregistration/OS-H1-H2-2026-10-23.md` | Frozen pre-registration (sha256 test) |

---

## Order proposed for stage 10

1. Fresh full backup of the three real DBs (SQLite backup API); `tools/sandbox.py off` first.
2. Delete groups C and D (no code impact) — after your approval of this list.
3. Kraken demo removal (A2 + B Kraken rows), green run.
4. Council of AIs migration with the parity test (SP §15).
5. Move `broker` / `params` / `types` / `llm` / `market` / `agents/macro` into `strategy_platform/` (A1 move rows + their tests), green run + parity.
6. Remove the remaining `fund/` engine, `/fund/*` routes and the WPF engine dashboard parts (A1 / A4 / B fund rows) — rename to `*.stale` first, your build + click-test, then delete.
