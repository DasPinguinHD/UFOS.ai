# HedgeFund module — code map for agents

Status: 2026-10-24 (current state only; history lives in the SPEC archives). Repo root on the device:
`$HOME/mnt/UFOS.ai/UFOS.ai` (Windows `C:\Users\leonh\source\repos\UFOS.ai\UFOS.ai`). Paths below are relative to
`UFOS.ai/HedgeFund/` unless they start with `UFOS.ai/`.

## 0. How to use this map

- **Read this first** instead of re-exploring the tree. Then read the governing SPEC section (column "SPEC") before
  changing anything. The SPEC is the contract; this map only points to it.
  - SP = `backend/strategy_platform/SPEC.md`
  - MR = `backend/market_research/SPEC.md`
  - FUND = `backend/fund/SPEC.md`
- **Update rule:** every agent that adds, moves, renames or retires a file updates this map in the same change (one
  line per file). If a file's responsibility changes, fix its line. Keep it compact (target ≤ ~600 lines).
- Line counts (≈) show the size, so you know when to read a file in parts.
- Ignore these as code:
  - `*.stale` files are backups.
  - `backend/data/` is runtime data and git-ignored.
  - `bin/`, `obj/` and `HedgeFundApp/obj` are build output.
- Vendored files (`**/vendor/**`, e.g. the KLineChart bundle, the world atlas) are pinned by sha256 in their
  `MANIFEST`; the root `.gitattributes` line `**/vendor/** -text` keeps git from rewriting their line endings (the
  klinecharts CRLF incident) — never re-save or normalise them; update only via their fetch tools.

## 1. Repo layout

| Path | What | Rule |
|---|---|---|
| `UFOS.ai/App.xaml.cs` | App start: single-instance guard (named mutex + activation event; Stage 10), AI notice at startup | outside: scope label (stage10ui) |
| `UFOS.ai/MainWindow.xaml(.cs)` | Shell window, module tiles, "Portfolio Insight" card | outside module: needs a scope label (insight0/sandbox0 were for the card) |
| `UFOS.ai/Shared/` | `BackendAuth.cs` (token file + header), `AiBudget*.cs` (central AI budget window), `PythonEnvironment.cs` (venv prep; installs only `requirements.lock` with --require-hashes), `WindowMaximize.cs`, `WindowsPowerShell.cs` (absolute powershell.exe path + .ps1 arguments for every backend launch), `RotatingLogFile.cs` (process.log cap 5 MB × 3), `CredentialVault.cs` (the one Credential Manager layer of every WPF module), `CredentialCatalog.cs` (all modules' credential names/targets), `MainAppSettings.cs` (main window non-secret settings, `%LOCALAPPDATA%\UFOS.ai\app-settings.json`), `BackendSettingsFile.cs` (atomic edit of a backend's `data/settings.json`, YouTube URL rule), `FullScreenHost.cs` (the one "⛶" full-screen block for charts / maps / dense tables, linked into all 4 assemblies: in place, never re-parents (no WebView2 re-creation), covers the monitor, collapses everything off the target path and undoes it in reverse, 30 px bar with title + live header text + "Exit full screen", Esc / F11; API `Enter`, `Toggle`, `Wire`, `CreateButton`, `RegisterF11`, `IsActive`, `ActiveFor`, `Exit`, event `Changed`) | outside: scope label |
| `UFOS.ai/Services/` | `IPortfolioInsightProvider.cs` (card provider → `/v2/insights/summary`), `PythonEnvManager.cs` (venv + health checks), OpenRouter (key from the Credential Manager) / news / quotes services | outside: scope label |
| `UFOS.ai/AiBudgetsWindow.xaml(.cs)` | Central AI budget window (reads `/ai/budgets` of every backend) | outside |
| `UFOS.ai/UFOS.ai.ModuleTiles/` | Public WPF project (net10.0-windows, WebView2 1.0.4191.47, refs UFOS.ai.Logging) referenced by the main app and the modules: `TileContracts.cs` (`IModuleTilePreview`: `ModuleId`, `DisplayName`, `Live` / `LastViewUnavailableReason`, `ReadLiveAsync`, `CreateIllustration`, `OwnsWindow`, `IsSandbox`; `TileMode`, `TileTone`, `TileFigure`, `TileDot`, `TileLiveData`, `TileLiveResult`), `TileStorage.cs` (`TileBrushes`, `TilePaths` `%LOCALAPPDATA%\UFOS.ai\tile-previews\{live,last-view}`, `TileLiveStore` schema 1, `TileLastViewStore` blurred PNG + metadata), `TileBlur.cs` (only place that creates `BlurredPng`), `TileCapture.cs` (`TileCaptureService`: every 60 s of each visible + active module window, one more at Closing if ready in 300 ms, WebView2 via `CapturePreviewAsync`, never sandbox data), `LoopbackReader.cs` (GET only to 127.0.0.1; `/health` 800 ms decides running; token, ≤ 3 s, ≤ 256 KB), `TileArt.cs` (160 × 90 motifs) | outside: scope label tiles0 |
| `UFOS.ai/Tiles/` | Main app tile wiring (compiled in since 2026-10-25; started by `MainWindow.StartTilePreviews` in `Window_Loaded`): `ModuleTileHost.cs` (one preview per tile, "⋯" menu, Live refresh 30 s paused while minimised / hidden, starts the capture service), `TilePreviewView.cs`, `TileModeSettings.cs` (`tile_modes` in app-settings.json), `MainAppTilePreviews.cs` (Transaction Log, System Logs, not-built modules); module providers `HedgeFund/Tiles/HedgeFundTilePreview.cs` (`GET /v2/portfolio/summary`) + `HedgeFund/Tiles/HedgeFundTileSummary.cs` (WPF-free payload → figures mapping), `Global Monitoring/Tiles/GlobalMonitoringTilePreview.cs` (`GET /events/summary?hours=24`), `LiveStreamAgent/Tiles/LiveStreamAgentTilePreview.cs` (`GET /verdicts?limit=1`); `Shared/MainAppSettings.cs` accepts the `tile_modes` key | outside: scope label tiles0 |
| `UFOS.ai/Global Monitoring/`, `UFOS.ai/LiveStreamAgent/` | Other modules (own WPF + `backend/`, own `ai_caps.py` copy). GM backend `events_summary.py` (`parse_hours` / `summarize` for the read-only tile route `GET /events/summary?hours=1..168`, no I/O; test `tests/test_events_summary.py`) | outside |
| `UFOS.ai/docs/` | `todo.md` (the ONE todo; open items only), `todo-archive/<yyyy-mm>.md` (done items), `tasks/` (open task / contract files for agents), `ai-budgets-spec.md` | |
| `docs/` (repo root, git-ignored) | `market-research-plan.md`, `global-monitoring-system-plan.md` (the old root `todo.md` copy is retired — moved to `HedgeFund/backend/data/docsslim0-backup/root-docs-todo.md.stale`; the pre-slim `UFOS.ai/docs/todo.md` is `docsslim0-backup/UFOS.ai-docs-todo.md.pre-docs-slim.stale`) | |
| `UFOS.ai/HedgeFund/` | **This module** — WPF (`*.cs`, `Fund/`, `Platform/`, `MarketResearch/`, `Newsroom/`) + Python `backend/` | default write scope |
| `HedgeFund/tools/` | `scope_guard.py` + `scope-allow-<label>.json` allow-lists | see §4.4 |
| `HedgeFund/docs/` | this map; `preregistration/OS-H1-H2-2026-10-23.md` (OS hypotheses, frozen — sha256 test; `HedgeFund/.gitattributes` keeps it byte-exact); `STAGE10_DELETION_LIST.md` (approved stage-10 deletion list) | |
| `backend/*/spec-archive/` | Verbatim history of the SP / MR SPECs (one file per stage; the SPEC itself states the current contract) | |

Scope rule:
- Write only inside `UFOS.ai/HedgeFund/**`.
- Any file outside needs a NEW scope label: allow-list JSON → `snapshot` → edit → `check` → `close`.
- Backups of outside files go to `backend/data/<label>-backup/`, never next to the file. A stray `.stale` outside
  the module trips the guard.

## 2. WPF (C#, `MultiAgentHedgeFund.csproj`, .NET 10)

### 2.1 Window shell and engine dashboard

| File | ≈Lines | Responsibility |
|---|---|---|
| `HedgeFundWindow.xaml` | 1400 | Window layout: title bar, sandbox strip, toolbar (Newsroom, Keys & data, Run now, Kill switch), nav rail, Overview dashboard |
| `HedgeFundWindow.xaml.cs` | 3280 | Old-engine dashboard (`/fund/*`, FUND §11/§13): polls `/fund/state` 5 s, chips, agents, verdict, orders, kill / resume, sandbox strip, nav switching |
| `HedgeFundWindow.{Studio,Loop,Trading,Monitoring}.cs` | 20–95 | Nav-rail entries: create Platform views on first use |
| `HedgeFundWindow.{ExplainBudget,IdeationBudget,CommitteeSettings}.cs` | 100–150 | Engine-settings rows for the AI budgets (SP §11.2, §13.1, §14.3) |
| `HedgeFundWindow.Council.cs` | 80 | Hosts the Council view (`CouncilHost`), hides the old-engine sections (`Legacy*`) and stops `/fund/state` polling once `council.switchover` is done / pending; "Resume…" dialog (halted accounts + old engine only before the switch-over) |
| `HedgeFundBackendHost.cs` | 46 | Public facade over `NewsroomBackend` for the main app (start / IsRunning) |
| "⛶" places (SP §25.1) | — | `HedgeFundWindow.xaml` `EquityFullScreenButton` (target `EquityChartHost`, bar: equity / day P&L / total P&L); `Platform/BacktestReportView.cs` section headings (equity, drawdown, IC decay — `Section(title, button)` / `ReplaceSection`); `Platform/LoopView.cs` generation × slot grid (`_gridHost`, refreshes in place; a cell click exits + opens the variant); `Platform/TradingView.Sections.cs` Orders blotter (focus mode); `Newsroom/MacroIndicatorsView` whole view (`MacroListContent` zoomed 1–2 at ≥ 1500 px); `MarketResearch/ResearchView.xaml` `ChartFullScreenButton` (`ChartCard`). Outside HedgeFund (label fullscreen0): GM `MainWindow.xaml` `FullScreenButton` → `GlobeArea` + `globe.html` `{type:"fullscreen", on}`; `TransactionLogWindow` → `EntriesPanel`; `LiveStreamAgent/BackendLogsWindow.cs` log text |
| `App.xaml(.cs)`, `HedgeFundApp/` | — | Standalone start (HedgeFundApp.exe) |
| `Styles/ScrollBars.xaml` | 180 | The one scroll-bar design of the module |

### 2.2 `Fund/` — old engine client and shared UI kit

| File | ≈Lines | Responsibility |
|---|---|---|
| `FundClient.cs` | 750 | Typed client `/fund/*` + DTOs (`FundState` incl. `mode` / `sandbox`), `KillAsync` (150 s) |
| `FundWindows.cs` | 1020 | **UI kit:** `FundPalette` (colours), `FundUi` (Text, MakeButton, Panel, wheel scrolling, `FixtureLine`, `FullScreenButton(target, title, header, f11Scope, registerF11)`, `HeadingWithFullScreen`), `FundPopupWindow` (borderless dialog base, maximize/work-area, **open-once `ShowDialog`**), `FundDialogs` (Confirm / Form / Info), `DialogGuard` (one-at-a-time key guard for load-then-open actions) |
| `FundFormat.cs` | 127 | Money / percent / time formatting (invariant) |
| `FundNotifications.cs` | 400 | App-wide toasts for `/fund/events` until the switch-over (410 "replaced" / Council view → stops; the Council's events come as monitoring alerts via `MonitoringNotifier`); `SourceProblem` shown, never silent |
| `FundSettings.cs` | 166 | Per-user JSON settings in `%LOCALAPPDATA%\UFOS.ai` (start-with-app, mute, toasts) |
| `EquityChart.cs` | 162 | Equity vs benchmark line chart (OnRender) |

### 2.3 `Platform/` — Strategy Platform views (all built in code)

Clients (one partial `PlatformClient` per SP stage). All go through `PlatformClient.SendAsync` → `PlatformResult<T>`
{Ok, Status, Message, RawBody, Offline, TimedOut}. `WithNote()` adds the optional `reason` field.

| File | SP | Routes |
|---|---|---|
| `PlatformClient.cs` | §4 | base: auth header, timeouts, errors, `DescribeFailure`, `WithNote` |
| `PlatformClient.Strategies.cs` | §7.11 | `/v2/strategies*`, `/v2/universes`, `/v2/dsl/operators` |
| `PlatformClient.Backtests.cs` | §8.5 | `/v2/backtests*` |
| `PlatformClient.Validation.cs` | §9 | `/v2/validations*`, holdout, `/v2/settings/gates` |
| `PlatformClient.Resilience.cs` | §10 | resume runs, auto-resume, `RunResume` helpers |
| `PlatformClient.Explain.cs` | §11 | explanations of attribution |
| `PlatformClient.Studio.cs` | §13 | `/v2/studio/*`, ideation budget |
| `PlatformClient.Papers.cs` / `.Committee.cs` | §14 | `/v2/papers*`, `/v2/committee*` |
| `PlatformClient.Loops.cs` | §16 | `/v2/loops*` |
| `PlatformClient.Trading.cs` | §17 | `/v2/portfolio/*`, `/v2/oms/*`, `/v2/incubation*` (submit timeout 150 s, `OrderNoAnswerText`); kill result with platform / legacy / council halves (§22.8) |
| `PlatformClient.Council.cs` | §22.11 | `/v2/council/{state,runs,run,orders,params,switchover,switchover/retry,switchover/abort,switchover/revert}` + DTOs (switch-over 180 s; pending attempt + actions) |
| `PlatformClient.Brokers.cs` | §18 | `/v2/brokers*`, routing, live guard (read) |
| `PlatformClient.Monitoring.cs` | §19 | `/v2/monitoring/*` (decay decision, alerts, reports) |
| `PlatformClient.Shadow.cs` | §19.6–8, §20.10–11 | `/v2/shadow/*` |
| `PlatformClient.Insights.cs` | §20 | `/v2/insights/*` |
| `PlatformClient.OptionsArchive.cs` | §21 (OS1) | `/v2/options-archive/{status,gaps,runs,registration,audit}` (read-only data + audit; registration = data only) |

Views and dialogs:

| File | ≈Lines | SP | Responsibility |
|---|---|---|---|
| `LibraryView.xaml(.cs)` | 1480 | §7.12 | Lifecycle board, alpha library, strategy detail, actions; `StrategyMoveDialog` |
| `StrategyEditorWindow.cs` | 610 | §7.12 | JSON spec editor + validate |
| `BacktestDialogs.cs` | 356 | §8.5 | "Run backtest…" dialog |
| `BacktestReportView.cs` | 1130 | §8.6 | Backtest report window (history, progress, metrics, charts) |
| `BacktestCharts.cs` | 770 | §8.6 | Chart tokens + equity / drawdown charts, wheel scroll |
| `AttributionView.cs` | 517 | §11.1 | Attribution section of the reports |
| `ExplainPanel.cs` | 663 | §11.2 | "Explain with AI…" panel |
| `ValidationDialogs.cs` | 600 | §9 | Validate…, Open holdout (typed name kept), gate exception (reason ≥ 30), gate settings |
| `ValidationReportView.cs` | 1270 | §9.3–9.7 | Validation / holdout report window |
| `StudioView.cs` | 1450 | §13.3 | Strategy Studio chat + drafts |
| `StudioPaperView.cs` | 944 | §14.1–14.2 | Paper import → strategy + replication |
| `CommitteePanel.cs` | 580 | §14.3 | Investment committee panel |
| `LoopView.cs` / `LoopDialogs.cs` | 1790 / 900 | §16 | Discovery loop list / detail / finalists; start + settings dialogs |
| `TradingView.cs` | 1250 | §17.13 | Portfolio & Trading header, accounts, positions, auto-trading, resume, new order; `InFlightButton` (button disabled while its request runs) |
| `TradingView.Sections.cs` | 1470 | §17.13 | Strategies, Netting, Orders, Incubation, Migration, Risk-limit sections |
| `CouncilView.cs`, `CouncilDialogs.cs` | 360/330 | §22.8, §22.11 | (`CouncilBlocker` carries `table` / `rows`: "Table cl_runs · n duplicate row(s)") Council view (header, budget, Run now, orders switch, AI budget…, Switch over… / Revert…, symbol cards with AI labelling, recent runs; 15 s poll); switch-over dialog (dry run → POST), revert confirm, `ResumeDialog` |
| `TradingDialogs.cs` | 770 | §17 | `TradingUi` kit (incl. `SetBusy`, `NoteBox`), generic confirm dialog, incubation start, settings, migration, order (timeout lookup) |
| `KeysAndDataWindow.xaml(.cs)` | 740 | §2, §4 | "⚙ Keys & data": Credential Manager key rows, data sources, `.env` import |
| `KeysAndDataWindow.Brokers.cs` | 1300 | §18.12 | Brokers section: test / connect / disconnect / kill / reconcile, routing, live-guard checklist |
| `KeysAndDataWindow.Network.cs` | 298 | §10.1 | Network concurrency setting |
| `KeysAndDataWindow.AutoRefresh.cs` | 150 | — | Window-wide auto refresh: 10 s while the backend is down or a state change settles (`AddSettle`, max 2 min + note), else 60 s; keys, sources, network, archive card (brokers keep their 5 s poll) |
| `KeysAndDataWindow.OptionsArchive.cs` | 360 | §21 (OS1) | Data sources → "Options archive (SPY/QQQ)" card: status chip (service heartbeat / manual run), task state, today's coverage, register / unregister / run once / gap report (60 s poll; sandbox disables register + run once) |
| `OptionsArchiveDialogs.cs` | 480 | §21 (OS1) | `OaTask` (fixed task XML + `schtasks.exe` /Create /Run /Query /End /Delete; app-side path validation — no PowerShell), `OaProcess` runner, register confirmation dialog, gap-report dialog |
| `BrokerDialogs.cs` | 611 | §18 | `BrokerText`, `BrokerConfirmDialog` (one step, note → submit; also used for Shadow "Remove import"), resolve reconciliation, route dialog |
| `CredentialStore.cs`, `PlatformDialogs.cs` | 140/150 | §2 | Keys of ALL modules (catalog `Shared/CredentialCatalog.cs`, access `Shared/CredentialVault.cs`), key-entry dialog |
| `EnvMigration.cs`, `EnvMigrationDialog.cs`, `BackendSettingSpecs.cs` | 600/290/70 | §23.6 | One-time "Import from old .env files…": the ONLY .env reader — scan all .env, names-only plan, keys → CredMan (read-back verified), settings → module settings files (only non-defaults), rewrite/delete the .env files, no backups |
| `MonitoringView.cs` | 600 | §19.13 | Monitoring shell: tabs, polling, banners, action line |
| `MonitoringView.Strategies.cs` | 420 | §19.1–2 | Strategy cards, paused block, decay detail, accounts |
| `MonitoringView.Alerts.cs` | 475 | §19.3–4, §19.15 | Alerts, reports, tax notice |
| `MonitoringView.Insights.cs` | 523 | §20 | Portfolio insights tab |
| `MonitoringDialogs.cs` | 127 | §19.2 | Keep running / Demote decision dialog |
| `MonitoringUi.cs` | 740 | §19.13 | Shared monitoring widgets (`MonUi`), band chart, explain-report panel |
| `MonitoringNotifier.cs` | 230 | §19.3, §22.8 | App-wide toasts for monitoring alerts (incl. the Council's `council_*` kinds after the switch-over); `PollProblem` shown in Monitoring + the Council view |
| `ShadowAccountView.cs` | 1092 | §19.6–8 | Shadow Account (user's own broker CSV imports) |
| `ShadowDialogs.cs` | 954 | §19.6–8 | New account, valuation, classification, column-mapping profile |
| `ShadowSymbolDialogs.cs` | 600 | §20.10–11 | ISIN → listing / US ticker mapping (US: `GET /v2/shadow/symbol-check` first, Alpaca name shown), mappings window, EODHD refresh flow |

### 2.4 `MarketResearch/` — Research section (MR)

| File | ≈Lines | MR | Responsibility |
|---|---|---|---|
| `ResearchClient.cs` | 1340 | §4 | Typed client `/research/*` + DTOs |
| `ResearchSocket.cs` | 770 | §4.3.1 | The one `/research/ws` WebSocket (topics, reconnect) |
| `ResearchView.xaml(.cs)` | 1160 | §1, MR1/MR3 | Section shell, tabs, chart card |
| `ResearchView.Workspace.cs` | 1390 | MR4 | Symbol header, key stats, palette, session restore |
| `ResearchView.Watchlists.cs` | 1110 | MR4 | Watchlists sidebar |
| `ResearchView.Drawings.cs` | 1390 | MR5 | Drawing tools + object list |
| `ResearchView.Indicators.cs` | 620 | MR5 | Indicators + chart settings |
| `ResearchView.News.cs` | 1110 | MR6 | News card, thumbnails, logos |
| `ResearchView.Alerts.cs` | 730 | MR7 | Alert editor + list |
| `ResearchView.Integrations.cs` | 565 | MR8 | Buy / Sell hand-over to the Trading order dialog (`/v2/oms/orders`), Portfolio / Macro views |
| `ResearchPopoutWindow.xaml(.cs)` | 155 | MR1 | Pop-out window hosting the single ResearchView |
| `ResearchAlertNotifier.cs` | 94 | MR7 | App-wide alert toasts |
| `RangeBar.cs` | 61 | MR4 | Low–high range bar |
| `Chart/ChartHost*.cs` + `chart.html/js/css` + `vendor/` | 1050+ | MR3/MR5 | WebView2 KLineChart + host bridge; `Chart/tests/run_tests.js` (node, fake KLineCharts) |

### 2.5 `Newsroom/` — Financial Newsroom window

| File | Responsibility |
|---|---|
| `NewsroomBackend.cs` | Starts / checks / stops the Python backend (`run_hedgefund_backend.ps1`, port 8867), `/health` status-code check, stale-backend replace |
| `FinancialNewsroomWindow.xaml(.cs)`, `NewsroomSections.cs` | Window + section registry |
| `InsiderTradesView`, `MacroIndicatorsView`, `EconomicCalendarView`, `AnalysisReasoningView` (`.xaml(.cs)`) | `/newsroom/insider`, `/macro`, `/calendar`, `/overview` + `/analysis` |

## 3. Backend (Python, `backend/`, aiohttp, Python 3.10 VM / 3.14 Windows venv)

### 3.1 Process root

| File | ≈Lines | Responsibility |
|---|---|---|
| `main.py` | 680 | Entry point. HTTP on 127.0.0.1:`HEALTH_PORT` (8867) only — always loopback, no WebSocket port. Registers the route groups (newsroom, `/fund`, `/ai/budgets`, `/v2`, `/research`) and starts the background tasks (§3.8). `sandbox_profile.startup()` runs first |
| `config.py` | 105 | `SCHEMA` + `SETTINGS` (+ `ai_settings()`) from the optional `data/settings.json` (models, base URL, poll intervals, retention) — no environment reads |
| `api_auth.py` | 191 | Loopback auth: session token (`data/api-token`, header `X-UFOS-Token`), Origin check, Host check (DNS-rebinding protection); public: `/health`, `/version` (trimmed payloads) |
| `ai_caps.py` | 506 | Daily AI caps + standard `/ai/budgets` payload (copies in other modules — a test compares them) |
| `ai_budgets.py` | 531 | `GET/PUT /ai/budgets`: aggregates budget keys fund, explain, ideation, loop, reports, newsroom caps (`UFOS.ai/docs/ai-budgets-spec.md` §4) |
| `logging_setup.py` | 45 | Console + rotating `data/logs/backend.log` |
| `sandbox_profile.py` | 206 | Sandbox mode, paths, guards, `stamp()` of `mode` into payloads (SP "Sandbox profile") |
| `sandbox_fixtures.py` | 252 | Sandbox-only seeding (`seed_incubation_test`) and `force_decay` |
| `run_hedgefund_backend.ps1`, `requirements*.txt`, `requirements*.lock`, `README.md` | — | Start script (venv; installs only `--require-hashes -r requirements.lock`), deps (`requirements-broker.txt`: pinned ccxt); no Docker |
| `settings_file.py` | small | Strict `data/settings.json` loader (identical copy in the three backends) |
| `agents/` | — | Empty (`.keep`) |

### 3.2 `strategy_platform/` (SP — the main system)

API routes (all `(query, body, match) -> (status, payload)`, behind api_auth):

| File | ≈Lines | Prefixes |
|---|---|---|
| `api/routes.py` | 2000 | `V2Api` core + mixins: settings (keys, data, network, explain, ideation, committee, loop, gates), strategies, backtests, validations, holdout, explanations, studio, memory, papers, committee, loops; `MODE_PATHS` stamping |
| `api/trading_routes.py` | 760 | `/v2/portfolio/*` (incl. `summary` for the tile — no heartbeat touch, SP §17.15), `/v2/oms/*`, `/v2/incubation*` (SP §17.10) |
| `api/broker_routes.py` | 336 | `/v2/brokers*`, `/v2/brokers/routing`, live guard (SP §18.9) |
| `api/monitoring_routes.py` | 741 | `/v2/monitoring/*`, `/v2/shadow/*`, `/v2/insights/*` (SP §19.10, §20) |
| `api/options_archive_routes.py` | 172 | `/v2/options-archive/{status,gaps,runs,registration}` read-only + `POST …/audit` (SP §21.11; sandbox → 409) |
| `api/council_routes.py` | 380 | `/v2/council/{state,runs,runs/{id},run,params,broker-params,orders}` + `GET/POST /v2/council/switchover`, `POST …/switchover/{revert,retry,abort}` (SP §22.11, §22.14) |

Subpackages:

| Package / file | ≈Lines | SP | Responsibility |
|---|---|---|---|
| `secrets.py` | 189 | §2 | Windows Credential Manager read (target `UFOS.ai/HedgeFund/<name>`); `RETIRED_SECRETS` (kraken-demo names, accepted by keys/changed for "removed" only) |
| `core/calendars.py` | 429 | §1 | NYSE + crypto calendars (single source of session logic) |
| `core/clock.py`, `errors.py`, `instruments.py`, `tls.py`, `fixture.py` | small | §1, §0 | Injectable clock; `UserFacingError`; symbol / asset class mapping; TLS context; sandbox fixture labels |
| `core/venv_guard.py`, `core/notices.py` | small | §23.2 | Tools refuse a non-backend Python (stdlib only); context notices (= `Shared/AiContent.cs` (a)/(b)/(c) + short context lines) |
| `core/params.py`, `core/types.py` | 240–400 | §22.7 (FUND §3, §4) | Broker / engine parameters (defaults, limits, validation, crypto rules) and the shared dataclasses (`Bar`, `Quote`, `OrderRequest`, …); `fund/params.py` / `fund/types.py` are same-module shims until R4 |
| `core/llm.py` | 485 | §22.7 (FUND §8, §0) | OpenRouter client, `Budget`, cost log (`fund-*` / `sp-*` features), `TRANSPORT` test hook; model ids only via `newsroom.ai.analysis_models()` (settings.json), never the environment |
| `storage/db.py` | 3025 | §5 | **hedgefund.db** schema + all access (migrations v1…v26 — v24 `sh_symbol_maps.confirmed_via`, v25 `cl_*` Council tables, v26 unique legacy `old_id` indexes; meta `schema.created_version`; tables §3.7) |
| `data/registry.py` | 327 | §3 | One configured source per data type (no fallback chains) |
| `data/http_gate.py` | 606 | §3, §21 | Token buckets (alpaca 150/min + burst 10, tiingo 20/min), retries (3 attempts), Research priority lane |
| `data/cache.py`, `pit.py`, `validation.py`, `licences.py` | 50–350 | §3 | Bar cache (table `bars`), point-in-time rules, bar findings, licence terms |
| `data/seam.py` | 625 | §8.1 | Tiingo ↔ Alpaca daily seam (2016-01-04) |
| `data/network.py` | 522 | §10.1 | Adaptive loading concurrency, connection-error kinds |
| `data/market_feed.py` | 369 | §22.7 (FUND §5) | `AlpacaFeed` (quotes / bars / news / dividends), `FakeFeed`, quote staleness |
| `data/providers/{alpaca,tiingo,eodhd,kraken_public,base}.py` | 120–730 | §3, §8.1, §20.11 | Provider adapters (alpaca-py SDK, Tiingo EOD, EODHD EOD + ISIN search, Kraken public OHLC) |
| `strategy/schema.py`, `dsl.py`, `validator.py`, `limits.py` | 115–790 | §7.3–7.5 | StrategySpec v1, DSL operator registry, validator, size limits / safe JSON |
| `strategy/evaluate.py`, `panel.py`, `portfolio.py`, `sdk.py` | 90–700 | §7.7, §7.1 | numpy evaluator, Panel, target weights, SDK |
| `strategy/preview.py`, `summary.py`, `universe.py` | 100–150 | §7.7, §7.11 | Signal preview, plain-text summary, universe presets |
| `strategy/features/{fred,macro_regime,event_risk}.py` | 110–240 | §7.8 | External features (FRED/ALFRED, macro regime, event risk) |
| `strategy/features/macro_agent.py` | 341 | §22.7 (FUND §7) | The macro agent (regime + stress → score / confidence; `asset_class`); used by `macro_regime.py`, the old engine and the Council |
| `strategy/alphas/*.py` | 26–92 | §7.9 | Seeded alpha library (momentum, reversal, trend, volatility, alpha101, Council Rules) |
| `lifecycle/registry.py` | 757 | §7.6, §7.10 | Strategies, immutable versions, transitions, library seeding |
| `backtest/engine.py` | 1340 | §8.3 | Day-by-day backtest on the real `PaperBroker` (`execution/paper_broker.py`; `memstore.py` stand-in store) |
| `backtest/jobs.py` | 505 | §8.5 | Single-job runner (worker thread) shared with validation |
| `backtest/runcard.py`, `metrics.py`, `stress.py` | 370–630 | §8.4 | Run cards (`backtest_runs`), metrics, stress scenarios |
| `backtest/attribution.py`, `explain.py` | 860–910 | §11 | Layer 1 attribution; layer 2 AI explanation (budget `explain`) |
| `validation/jobs.py`, `parallel.py` | 740–1020 | §9.3, §10.2 | Validation / holdout jobs, process pool |
| `validation/gates.py`, `stats.py`, `folds.py`, `grid.py`, `report.py` | 140–440 | §9.2–9.4 | Gates G1–G8 + H, DSR / PBO / NW stats, folds, grid, report + exceptions |
| `validation/families.py` | 424 | §9.1 | Families, seen-until, locked holdout window, trials |
| `research/studio.py`, `prompts.py`, `memory.py`, `topic_guard.py` | 64–925 | §13 | Strategy Studio (AI Researcher), memory of computed facts, topic guard (budget `ideation`) |
| `research/papers.py`, `pdf_worker.py`, `paper_strategy.py` | 105–585 | §14.1–14.2 | Paper ingestion (allow-listed links, PDF subprocess with `worker_env()` — secrets scrubbed), paper → strategy |
| `research/committee.py` | 458 | §14.3 | Five-role investment committee |
| `research/loop.py` | 4 | — | Hook name for `ai_budgets.PROVIDER_MODULES` |
| `discovery/loop.py` | 1740 | §16 | Discovery loop orchestrator (daemon thread) |
| `discovery/{budget,prompts,scoring,search,selection}.py` | 68–330 | §16.2–16.6 | Loop budget (`loop`), prompts, score, parameter search, selection |
| `portfolio/allocator.py`, `allocation.py` | 150–180 | §17.4 | Inverse-vol allocator + capital transfers |
| `portfolio/checks.py`, `risk.py`, `settings.py` | 85–220 | §17.6 | Pre-trade checks, daily loss / drawdown kill, per-account settings (`pf_settings`) |
| `portfolio/ledger.py`, `netting.py`, `decision.py`, `views.py` | 97–350 | §17.3, §17.5, §17.11 | Sub-ledgers, netting, shared decision block, read models |
| `portfolio/summary.py` | 170 | §17.15 | `GET /v2/portfolio/summary`: stored values only (snapshots, positions, halts, auto trading, Council switch) — no account / broker object, no quote |
| `execution/oms.py` | 721 | §17.7 | OMS state machine (`oms_orders` + events), idempotent client order id |
| `execution/paper_broker.py` | 1345 | §22.7 (FUND §6) | `PaperBroker` long/short simulation (backtest, platform paper accounts, mirror valuation, the old engine) |
| `execution/accounts.py`, `paper_store.py` | 260–480 | §17.1, §17.8 | Platform paper accounts + their SQLite book |
| `execution/control.py` | 430 | §17.1, §17.6, §22.8 | Auto-trading switch, shared heartbeat (dead-man; meta `autotrade.heartbeat_minutes` once switched over, fail closed), kill switch (`kill_all` → platform / legacy / council halves), `council_orders_off` |
| `execution/migration.py` | 395 | §17.9 | Old account → `paper-council` (one-way) |
| `council/service.py` | 790 | §22.1, §22.5 | The Council as a platform strategy — one cycle (agents → judge → sizing → order step under `oms.lock_for`, origin `council`, D7 market-closed rule), schedule (`next_due`), "Run now", D11 crypto expiry; `default()` built in `main.py`; started at start-up when switched over (or by the switch-over itself) |
| `council/store.py`, `params.py`, `spend.py` | 145–360 | §22.2–22.4 | `cl_*` access + switch-over / orders meta; Council params + broker override (D9) + agent view; the ONE AI spend counter booked with its `cl_ai_calls` row |
| `council/switchover.py` | 700 | §22.8 | Switch-over: preconditions / dry run / run (old engine stopped in-process, ONE transaction: params copy, heartbeat setting, spend seed, legacy import above the high-water mark) / `retry` / `abort` / `recover` / `revert`; one operation at a time; a failed attempt stays `pending` + `last_error` — the real run ONLY from the user's dialog |
| `council/agents/{technical,risk,sentiment}.py`, `judge.py`, `sizing.py`, `indicators.py` | 100–560 | FUND §7–§9 | Council agents / judge / sizing — copies of the `fund/` modules (R4 removes the originals) |
| `execution/brokers/service.py` | 1026 | §18.1–18.9 | Test / connect / disconnect, routing, per-broker kill, `run_forever` poller |
| `execution/brokers/mirror.py`, `reconcile.py` | 420–630 | §18.4–18.5 | Mirror book (+ `RetiredAdapter` for history rows of a retired broker); reconciliation (mismatch halts) |
| `execution/brokers/alpaca_paper.py`, `binance_demo.py`, `paper.py` | 170–890 | §18.3, §18.14 | Adapters (paper endpoints only) |
| `execution/brokers/{base,common,ccxt_support}.py` | 70–225 | §18.3 | Adapter interface, link state / backoff, lazy ccxt |
| `execution/brokers/live_guard.py` | 195 | §18.7 | Live guard: 4 conditions, paper-URL asserts — live OFF |
| `live/runtime.py` | 743 | §17.3 | Strategy runtime loop (same decision block as the backtest) |
| `live/incubation.py` | 530 | §17.2 | Incubation runs (immutable length), start lock (409 `already_incubating`), allocation after the commit (≤ 3 s wait, `allocation_failed` alert) |
| `monitoring/service.py` | 140 | §19.3 | `sp-monitor` task + overview payload |
| `monitoring/decay.py`, `expected.py` | 160–525 | §19.1–19.2 | Decay rules + pause / decisions; frozen expected band |
| `monitoring/alerts.py` | 271 | §19.3 | Alert kinds, emit / scan / ack |
| `monitoring/reports.py` | 397 | §19.4 | Daily / weekly reports + AI commentary (budget `reports`) |
| `monitoring/insights.py` | 513 | §20 | Portfolio insights (Shadow imports only) |
| `monitoring/fx.py`, `prices.py`, `tax_notice.py` | 19–150 | §19.6–8, §19.15 | EUR/USD (FRED DEXUSEU), Shadow closes, fixed tax notice (no tax export) |
| `monitoring/shadow/service.py` | 691 | §19.6–8 | Shadow storage + orchestration |
| `monitoring/shadow/{importers,normalize,diagnostics,shadow_portfolio,report}.py` | 180–945 | §19.6–8 | CSV importers (Trade Republic …), FIFO lots, diagnostics, rule portfolios, HTML audit report |
| `options_archive/config.py` | 202 | §21.10, §21.11 | OS1: ALL archive numbers (filter, cadence, staleness, gate, timeouts); `CONFIRMED` / `PROVISIONAL_KEYS` (staleness, coverage until the RTH run); override `data/options_archive_config.json` (validated against `RANGES`, refused — never clamped) |
| `options_archive/transport.py` | 289 | §21.4, §21.9 | GET allow-list, no-redirect opener, body cap, sliding 30/min gate, `Http` (one retry, sanitized reasons, 429 stop, server `Date` → clock skew) — shared with `tools/verify_alpaca_options.py` |
| `options_archive/db.py`, `slots.py` | 90–120 | §21.5, §21.2 | **options_archive.db** migration (insert-only triggers, schema v1); NY slots incl. early close / DST |
| `options_archive/collector.py` | 472 | §21.3 | One run per slot (chain / OI / FRED), PIT stamps, staleness / clock skew / OI conflicts → `os_gaps`, crash rows |
| `options_archive/report.py`, `service.py`, `registration.py` | 80–300 | §21.4, §21.11 | Gap report + status payloads (never-raising status writes, `manual` block); process loop (lock, stop file, slot windows, per-run crash handling, redacted rotated log); registration DATA (task `UFOS.ai OptionsArchive`, no script text) |
| `monitoring/shadow/{symbols,pricing}.py` | 80–285 | §20.10–11 | ISIN → listing maps (+ read-only US ticker check `lookup`, provenance `confirmed_via`), one pricing source per instrument |

### 3.3 `market_research/` (MR — Research section backend)

| File | ≈Lines | MR | Responsibility |
|---|---|---|---|
| `api.py` | 127 | §1.2, §4 | `ResearchApi` + mixins `data_api`, `workspace_api`, `drawings_api`, `news_api`, `alerts_api`, `integrations_api` |
| `supervisor.py`, `executor.py`, `maintenance.py` | 36–316 | §1.4, §5 | Supervised asyncio tasks, Research thread pool, store re-open |
| `store.py` | 1170 | §6 | **market_research.db** (tables §3.7) |
| `limits.py` | 173 | §4.4 | All constants / limits |
| `data.py`, `sources.py`, `assets.py`, `market.py`, `feeds.py` | 60–210 | §7, MR2 | Data service, Alpaca via SP data layer, asset list, market status, feed status |
| `bars/{series,intraday,aggregate}.py` | 190–250 | §7.1–7.2 | Bars endpoint, 1m store, session aggregation |
| `stats/{snapshot,keystats,quote,context}.py` | 95–160 | §7.4 | Header, key stats, watchlist quotes |
| `hub/hub.py` + `alpaca_stream.py`, `protocol.py`, `transport.py`, `demand.py`, `candles.py`, `aio.py` | 30–600 | §7.6 | MarketDataHub — sole owner of Alpaca streams; live candles |
| `hub/ws.py` | 306 | §4.3 | `/research/ws` for the app |
| `watchlists.py`, `session.py`, `workspace_api.py` | 180–430 | MR4 | Watchlists, session restore, palette |
| `drawings.py`, `drawings_api.py`, `chart_settings.py`, `indicators.py` | 80–770 | MR5 | Drawings CRUD / split; chart settings; indicators (SP DSL definitions) |
| `news.py`, `news_api.py`, `news_text.py`, `media.py`, `logos.py` | 120–380 | MR6, §9 | News store, untrusted text → plain text, thumbnail proxy, logos |
| `alerts.py`, `alerts_api.py` | 130–700 | MR7 | Alert rules + `AlertEngine` |
| `integrations.py`, `integrations_api.py` | 120–440 | MR8 | Read-only views of other HedgeFund parts (no order route) |

### 3.4 `fund/` — old engine "Council of AIs" (FUND, paper only; since SP §17.9 migration it only analyses)

| File | ≈Lines | FUND | Responsibility |
|---|---|---|---|
| `api.py` | 321 | §11 | `/fund/*` routes (+ `mode` stamping) |
| `engine.py` | 2070 | §10 | Scheduling, cycles, halt / kill / drawdown stop, `/fund/state` |
| `store.py` | 958 | §2 | SQLite access (newsroom.db schema v3) |
| `indicators.py`, `sizing.py` | 130–190 | §9 | Indicators, sizing |
| `params.py`, `types.py`, `llm.py`, `market.py`, `broker.py`, `agents/macro.py` | 8 | SP §22.7 | TEMPORARY shims → `core/{params,types,llm}.py`, `data/market_feed.py`, `execution/paper_broker.py`, `strategy/features/macro_agent.py` (same module object), removed at R4 |
| `judge.py` | 600 | §8 | Rule score + LLM judge (budget `fund`) |
| `agents/{technical,risk,sentiment}.py` | 130–340 | §7 | Agents (sentiment = LLM batch; macro moved, see above) |
| `backtest.py`, `export.py` | 120–513 | §12, §11 | Rule-only backtest, CSV exports |

### 3.5 `newsroom/` — Financial Newsroom backend

| File | Responsibility |
|---|---|
| `db.py` | **newsroom.db** (newsroom tables + fund schema v3), migrate / prune |
| `sec_edgar_collector.py`, `insider_summary.py` | SEC Form 4 insider trades + AI summary table |
| `fred_collector.py`, `macro_derived.py`, `macro_rules.py` | FRED macro series, derived indicators, stability signal |
| `calendar_collector.py` | FRED release calendar + FOMC |
| `overview.py`, `ai.py`, `cost_ledger.py` | "Analysis & Reasoning", OpenRouter helpers, shared AI cost ledger |
| `keys.py`, `store.py` | Keys from CredMan; in-memory event store |
| `disclosure_db.py` | Disclosures storage (trades, returns, SIC, jobs; newsroom.db v4); never swallows errors |
| `disclosure_trades.py` | Form 4 → trades (P/S aggregation, role class, point-in-time clusters) |
| `disclosure_returns.py` | Return engine (entries, horizons, sector-ETF excess, score, chip, track-record stats) + `AlpacaPrices` |
| `sector_map.py`, `sector_etf_map.json` | SIC → sector ETF (one editable file with source / date) |
| `sec_limiter.py` | One fair-access limiter for every sec.gov request (collector, daily job, backfill) |
| `disclosures_service.py`, `disclosures_api.py`, `disclosures_backfill.py` | Live hook + SEC fetch + daily job; `/newsroom/disclosures/*` routes; the user-started 2-year backfill (steps, pause / resume, status) — SP §24 |

### 3.6 `tools/` (backend) and `HedgeFund/tools/`

| File | Responsibility |
|---|---|
| `backend/tools/sandbox.py` | Sandbox CLI: `on` (copies + marker, then seeds the fixture) / `off` (hash compare; empty ≡ absent WAL) / `status` / `seed-incubation-test` / `force-decay`; refuses outside the backend venv (SP "Sandbox profile") |
| `backend/tools/verify_binance_demo.py` | Manual check against the real Binance Spot demo (SP §18.14) |
| `backend/tools/verify_alpaca_options.py` | OS0 read-only Alpaca options + FRED VIX check (SP §21.9; transport from `options_archive/transport.py`, per-endpoint timeouts, one counted retry, sanitized reasons, exit 0/1/2/3/4) |
| `backend/tools/options_archive.py` | OS1 archive CLI: `service` (the scheduled task) / `run-once` / `run-slot` / `status` / `gaps` (SP §21.11, exit 0–7) |
| `HedgeFund/tools/scope_guard.py` | Scope guard (`snapshot` / `check` / `amend` / `close`) |
| `HedgeFund/tools/check_repo_data.py`, `repo-data-allow.json` | Licence / privacy check of everything that would be committed (SP §0; run before every commit; test `tests/test_licence_data.py`) |
| `HedgeFund/tools/export_council_parity_bars.py` | Re-exports the git-ignored real-data parity file from a COPY of hedgefund.db (SP §22.10) |
| `HedgeFund/tools/check_shared_cs_members.py` | Static check: duplicate member / type names in `UFOS.ai/Shared/*.cs` (linked into 4 assemblies; test `tests/test_s10_shared_cs.py`) |
| `HedgeFund/tools/check_no_env_cs.py` | Guard: no C#/XAML code reads `.env` or a secret env var (test `tests/test_s10_env_cs_guard.py`) |
| `backend/tools/make_lockfile.py` | Writes `requirements.lock` / `requirements-broker.lock` (`==` pins + sha256); `--update` refreshes (the user runs it — no PyPI from the agent machines) |

### 3.7 Data files and table ownership (`backend/data/`, git-ignored)

| File | Owner | Tables (main) |
|---|---|---|
| `hedgefund.db` | `strategy_platform/storage/db.py` | `meta`, `audit`, `data_sources`, `bars`, `ext_observations`, `corporate_actions`, `seam_checks`, `volumes`; `strategies`, `strategy_versions`, `strategy_transitions`, `strategy_families`, `strategy_logic`; `backtest_runs`, `run_checkpoints`, `run_explanations`, `holdout_events`, `validation_exceptions`; `studio_*`, `memory_facts`, `papers`, `paper_*`, `committee_*`, `loop_*`; `pf_accounts`, `pf_settings`, `pf_allocations`, `pf_migrations`, `pa_*` (paper books), `oms_orders`, `oms_order_events`, `sl_entries`, `sl_snapshots`, `rt_decisions`, `rt_executions`, `incubation_runs`, `incubation_events`; `cl_*` (Council); `br_*` (brokers, routes, reconciliations, live guard); `mon_*` (decay checks / events, alerts, acks, reports, commentaries); `sh_*` (Shadow Account) |
| `market_research.db` | `market_research/store.py` | `assets`, `bars_1m`, `bars_1m_days`, `watchlists`, `watchlist_items`, `session`, `recent_symbols`, `drawings`, `drawing_rescales`, `chart_settings`, `news`, `news_symbols`, `news_images`, `logos`, `alerts`, `alert_log` |
| `newsroom.db` | `newsroom/db.py` (+ `fund/store.py`) | `insider_filings`, `macro_observations`, `calendar_snapshot`, `ai_results`, `disclosure_trades`, `disclosure_returns`, `issuer_sic`, `disclosure_jobs` (v4, 2-year retention); old engine: `prices`, `signals`, `orders`, `positions`, `equity_curve`, `fund_*` |
| `options_archive.db` | `strategy_platform/options_archive/db.py` (archive process only; backend opens it read-only) | `os_schema`, `os_configs`, `os_runs`, `os_contracts`, `os_contract_sightings`, `os_open_interest`, `os_chain_snapshots`, `os_bars`, `os_fred`, `os_gaps` |
| `os_archive_status.json`, `options_archive.lock`, `options_archive.stop`, `options_archive_config.json`, `logs/options_archive.log` | `options_archive/service.py`, `config.py` | archive status / heartbeat, single-instance lock, stop request, config override, archive log |
| `api-token` | `api_auth.py` | session token (rewritten every start) |
| `logs/backend.log` | `logging_setup.py` | |
| `sandbox/`, `SANDBOX_ACTIVE` | `tools/sandbox.py`, `sandbox_profile.py` | DB copies + marker |
| `backups/`, `backtests/`, `scenarios/`, `papers/` | storage / backtest / stress / papers | DB backups, run artefacts, stress data, paper texts |
| `backups/stage10-<UTC>/` | stage-10 backup | Stage-10 backup of the real DBs + `SHA256SUMS.*`, `DELETE_MANIFEST.txt` / `DELETE_NOW.txt` / `KEEP_UNTIL_CLICKTEST.txt` (cleanup record) |
| `scope-guard/` | `tools/scope_guard.py` | manifests `manifest-<label>.json` |
| `agent_transfer/`, `<label>-backup/` | agents | test bundles; backups of out-of-module files |
| `local_fixtures/` | developer | git-ignored real-data test inputs (e.g. `council_parity_real_bars.json`); tests skip without them |

Never open the real `*.db` files in tests. Use copies in a temp dir.

### 3.8 Background tasks (started in `main.py`)

| Task | Code | What |
|---|---|---|
| fund engine | `fund/engine.py` | Old-engine cycles (first cycle 20 s after the start) — only while NOT switched over (`start()` refuses then) |
| newsroom collectors + prune | `newsroom/*_collector.py`, `main._prune_forever` | SEC EDGAR 120 s, FRED / calendar 6 h, daily prune |
| `sp-runtime` | `live/runtime.py` | Strategy runtime (platform accounts) |
| `council-cycle` | `council/service.py run_forever` | Runs when `council.switchover = done` (then no `fund engine` task); scheduled cycles only in the session after the grace |
| `sp-brokers` | `execution/brokers/service.py run_forever` | 15 s broker poller / reconciliation |
| `sp-monitor` | `monitoring/service.py run_forever` | Audit scan, broker link watch (30 s), decay checks, reports |
| Research supervisor | `market_research/supervisor.py` | Hub, asset refresh, store maintenance, alerts |
| backtest / validation worker | `backtest/jobs.py`, `validation/parallel.py` | Single-job thread + process pool |
| discovery loop | `discovery/loop.py` | Daemon thread, one loop at a time |

Not a backend task: the OS1 options archive is its own process (`tools/options_archive.py service`, Windows scheduled
task registered by the WPF; SP §21.11).

## 4. Cross-cutting

### 4.1 Config and secrets (names only)
- Secrets live ONLY in the Windows Credential Manager, target `UFOS.ai/HedgeFund/<name>`
  (`strategy_platform/secrets.py`). WPF access: `Platform/CredentialStore.cs`.
- Names:
  - `openrouter-api-key`, `fred-api-key`, `sec-edgar-user-agent`
  - `alpaca-paper-key-id`, `alpaca-paper-secret-key`
  - `tiingo-api-key`
  - `binance-demo-api-key/-secret` (`kraken-demo-*`: retired, removal only — `RETIRED_SECRETS`)
  - `eodhd-api-key`
- No `.env` is read by any backend; the one-time WPF import (`Platform/EnvMigration*.cs`) is the only `.env`
  reader. **Never read or print `.env`.** GM / LSA keys: `credstore.py` (shared `openrouter-api-key`).
- Non-secret settings: optional `backend/data/settings.json` (`config.SCHEMA`, strict; keys = the old env names in
  lower case, e.g. `openrouter_base_url`, `openrouter_model`, `openrouter_analysis_premium_model`, poll intervals,
  retention days). Ports are fixed (HTTP 8867, loopback only).

### 4.2 Loopback auth
- `api_auth.py` writes a new token to `data/api-token` at every start.
- The app sends it as `X-UFOS-Token` (`UFOS.ai/Shared/BackendAuth.cs`). Requests with an Origin header are refused
  unless the origin is allowed.
- Public routes:
  - `/health` returns JSON `{status, mode}`. Callers check only the status code; the insight provider also reads `mode`.
  - `/version`.
  - `/shutdown` (loopback only).

### 4.3 HTTP gate / rate limits
- `strategy_platform/data/http_gate.py` wraps every provider call:
  - token bucket per source (alpaca 150/min + burst 10, tiingo 20/min; the OS1 archive has its own 30/min in
    `options_archive/transport.py` — fixed split under Alpaca's 200)
  - up to 3 attempts with back-off
  - acquire timeout of up to about 120 s
  - a Research priority lane (60/min).
- The client timeouts in the WPF (order / resume / kill 150 s) match this budget.
- Network concurrency: `data/network.py` (adaptive 1–8).

### 4.4 Scope guard
- `python3 HedgeFund/tools/scope_guard.py check --label <l>` from the repo parent (`$HOME/mnt/UFOS.ai`).
- Labels: stage0, mr0, budget0, budgetfix0, insight0, maxwin0, sandbox0, fullscreen0, gmsummary0, stage10ui, stage10sec,
  stage10secui, stage10env, stage10envui, stage10lock, stage10lockui, stage10git, tiles0, docsslim0, user1024 (the user's
  own change, closed).
- Allow-lists: `HedgeFund/tools/scope-allow-<label>.json`. Manifests: `backend/data/scope-guard/`.
- A closed label needs a new label for new exceptions.

### 4.5 Live guard
- `execution/brokers/live_guard.py` (SP §18.7). Live trading would need all four conditions: live flag, typed
  confirmation, strategy live state, capital cap.
- The build has live OFF: every adapter asserts paper / demo URLs.
- WPF shows a read-only checklist (`KeysAndDataWindow.Brokers.cs`). The strong confirmation stays.

### 4.6 Sandbox profile (SP "Sandbox profile")
- `tools/sandbox.py on` copies the DBs to `data/sandbox/` and writes the `data/SANDBOX_ACTIVE` marker. It refuses
  while the backend runs.
- The backend start (`sandbox_profile.startup()`) then:
  - points every DB at the copies
  - adds `[SANDBOX]` to the logs
  - blocks the brokers (409 `sandbox_blocked`).
- Payloads carry `mode` and a `sandbox` block {since, banner, brokers, data_dir, prices_note}.
- Fixtures carry `fixture: true` and `fixture_note` (`core/fixture.py`).
- WPF:
  - amber strip in `HedgeFundWindow`
  - SANDBOX chip on the MainWindow card
  - red `FundUi.FixtureLine` on runs, reports, strategies, incubation and monitoring.

### 4.7 AI budgets
- Central contract: `UFOS.ai/docs/ai-budgets-spec.md`.
- Backend: `ai_budgets.py` (aggregate) and `ai_caps.py` (daily caps).
- Keys and where they live:

  | Key | Code |
  |---|---|
  | `fund` (Council of AIs) | `core/llm.py`; limit in fund params until the switch-over, then `cl_params` + meta `council.ai_spend` |
  | `explain` | `backtest/explain.py` |
  | `ideation` | Studio, papers, committee |
  | `loop` | `discovery/budget.py` |
  | `reports` | `monitoring/reports.py` |
  | newsroom caps | `ai_caps.py` |

- Cost ledger: `newsroom/cost_ledger.py`.
- WPF: engine-settings rows `HedgeFundWindow.*Budget.cs`; central window `UFOS.ai/AiBudgetsWindow`.

### 4.8 Alerts
- `monitoring/alerts.py` defines the kinds:

  | Kind | Severity |
  |---|---|
  | `decay` | critical |
  | `reconciliation_halt` | critical |
  | `kill_switch` | critical |
  | `drawdown_kill` | critical |
  | `daily_loss_limit` | warning |
  | `autotrade_off` | warning |
  | `broker_disconnected` | warning |
  | `protective_exit_while_paused` | warning |
  | `broker_reconnected` | info |
  | `incubation_finished` | info |
  | `report_ready` | info |

- Each pause raises its own decay alert; Council kinds `council_*` (SP §22.9); `allocation_failed` (warning).
- Toasts: `Platform/MonitoringNotifier.cs`. Old engine: `Fund/FundNotifications.cs`. Research: `ResearchAlertNotifier.cs`.

### 4.9 UI conventions
- All dialogs are `FundPopupWindow` and start maximized only where requested (`UFOS.ai/Shared/WindowMaximize.cs`).
- Untrusted text is shown as plain text.
- Notices (Stage 10): no blanket "not investment advice" — use `AiContent.AnalysisNotice` (= `Caveat`) for analyses,
  `AiContent.ResearchNotice` for strategies / committee / loop finalists, `AiContent.PaperExecutionNotice` for autonomous paper execution.
- Missing data reads "not available (reason)". No stand-in values.
- Confirm dialogs:
  - one step, a precise button text and an optional note (`TradingUi.NoteBox`, `PlatformClient.WithNote`)
  - `TradingUi.SetBusy` while the request is in flight
  - exceptions that keep their strong checks: live guard, open holdout (typed name), gate exception (reason ≥ 30).
- Dialogs: `FundPopupWindow.ShowDialog` is open-once (type + title + owner; a second call activates
  the open one); actions that load data before a dialog run through `DialogGuard.RunAsync(key, …, trigger)` (button disabled,
  queued clicks ignored). `PlatformClient.SendAsync` parses responses off the UI thread.
- Client timeouts: a `PlatformResult.TimedOut` result says "No answer in time — … may have happened", then refreshes
  or looks the result up (order: by client order id).

## 5. Where do I change X?

| Task | Files |
|---|---|
| Add a `/v2` route | `strategy_platform/api/<area>_routes.py` + SP route table; WPF `Platform/PlatformClient.<Area>.cs` |
| Add a Research route | `market_research/<area>_api.py` + `store.py`; WPF `MarketResearch/ResearchClient.cs` |
| Change a hedgefund.db table | `strategy_platform/storage/db.py` (new migration version) + SP §5 / the stage's storage section |
| Add a data provider | `strategy_platform/data/providers/` + `data/registry.py` + `http_gate.py` rate; SP §3 |
| Change rate limits / retries | `strategy_platform/data/http_gate.py` (+ WPF client timeouts in `PlatformClient.Trading.cs`, `FundClient.cs`) |
| New DSL operator | `strategy/dsl.py` + `evaluate.py` + `validator.py`; Studio prompt reference is generated (`research/prompts.py`) |
| New library alpha | `strategy/alphas/<family>.py` (seeded by `lifecycle/registry.py`) |
| Lifecycle states / transitions | `lifecycle/registry.py`; WPF `LibraryView.xaml.cs` (`StrategyMoveDialog`) |
| Backtest costs / fills | `execution/paper_broker.py` (shared!) + `backtest/engine.py`, `memstore.py` |
| Metrics / stress scenarios | `backtest/metrics.py`, `backtest/stress.py`; WPF `BacktestReportView.cs`, `BacktestCharts.cs` |
| Validation gates / thresholds | `validation/gates.py` (+ `stats.py`); settings `GET/PUT /v2/settings/gates`; WPF `ValidationReportView.cs`, `ValidationDialogs.cs` |
| Holdout rules | `validation/families.py`, `validation/jobs.py`; WPF `OpenHoldoutDialog` |
| Explain / attribution | `backtest/attribution.py`, `explain.py`; WPF `AttributionView.cs`, `ExplainPanel.cs` |
| Studio / papers / committee prompts | `research/prompts.py`, `paper_strategy.py`, `committee.py`; topic check `topic_guard.py` |
| Discovery loop scoring | `discovery/scoring.py`, `selection.py`, `search.py`; WPF `LoopView.cs` |
| Allocator | `portfolio/allocator.py`, `allocation.py` |
| Pre-trade checks / risk limits | `portfolio/checks.py`, `risk.py`, `settings.py`; WPF `TradingView.Sections.cs` (Risk limits), `TradingSettingsDialog` |
| Order flow / OMS states | `execution/oms.py`, `api/trading_routes.py`; WPF `TradingOrderDialog`, `TradingView.Sections.cs` (Orders) |
| Kill switch | `execution/control.py` (both engines), `fund/engine.py`, broker kill `brokers/service.py`; WPF `HedgeFundWindow.xaml.cs` (toolbar), `KeysAndDataWindow.Brokers.cs` |
| Incubation | `live/incubation.py`, `live/runtime.py`; WPF `IncubationStartDialog`, `TradingView.Sections.cs` |
| Broker adapter behaviour | `execution/brokers/<broker>.py`, `common.py`, `mirror.py`, `reconcile.py` |
| Broker connect / routing dialogs | `execution/brokers/service.py`, `api/broker_routes.py`; WPF `BrokerDialogs.cs`, `KeysAndDataWindow.Brokers.cs` |
| Decay rules / pause UI | `monitoring/decay.py` (`RULES`); WPF `MonitoringView.Strategies.cs`, `MonitoringDialogs.cs`, `MonitoringUi.cs` |
| Alerts / toasts | `monitoring/alerts.py` (`KINDS`); WPF `MonitoringNotifier.cs`, `MonitoringView.Alerts.cs` |
| Reports | `monitoring/reports.py`; WPF `MonitoringView.Alerts.cs` |
| Shadow import format | `monitoring/shadow/importers.py`, `normalize.py`; WPF `ShadowDialogs.cs` |
| Shadow pricing / symbol maps | `monitoring/shadow/pricing.py`, `symbols.py`, `data/providers/eodhd.py`; WPF `ShadowSymbolDialogs.cs` |
| Portfolio insights / MainWindow card | `monitoring/insights.py`; WPF `MonitoringView.Insights.cs`; card `UFOS.ai/Services/IPortfolioInsightProvider.cs` + `UFOS.ai/MainWindow*` (scope label!) |
| Keys UI | `Platform/CredentialStore.cs`, `KeysAndDataWindow.xaml.cs`, `Shared/CredentialCatalog.cs` + `CredentialVault.cs`, import `Platform/EnvMigration*.cs`; backend `strategy_platform/secrets.py`, GM/LSA `credstore.py` |
| AI budgets | `ai_budgets.py`, `ai_caps.py`, the feature's budget module; WPF `HedgeFundWindow.*Budget.cs` |
| Newsroom section | `newsroom/<collector>.py` + `main.py` route; WPF `Newsroom/<Section>View` + `NewsroomSections.cs` |
| Full screen "⛶" for a new chart / map / table | `Shared/FullScreenHost.cs` via `FundUi.FullScreenButton` (SP §25.1) |
| Module tile preview | `UFOS.ai.ModuleTiles` contracts + the module's `Tiles/<Module>TilePreview.cs` (SP §25.2) |
| Council agents / judge | `council/agents/*.py`, `council/judge.py`, `council/params.py` (old engine copies in `fund/` until R4) |
| Research chart behaviour | `MarketResearch/Chart/chart.js` + `ChartHost*.cs` (bridge contract MR "MR3 bridge contract") |
| Research live data | `market_research/hub/*`, `stats/*`; WPF `ResearchSocket.cs` |
| Sandbox behaviour | `sandbox_profile.py`, `sandbox_fixtures.py`, `tools/sandbox.py`, `core/fixture.py` |
| Backend start / stop from the app | `Newsroom/NewsroomBackend.cs`, `run_hedgefund_backend.ps1`, `UFOS.ai/Services/PythonEnvManager.cs` (outside) |
| Options archive numbers / slots | `options_archive/config.py` (or `data/options_archive_config.json`), `slots.py`; SP §21.11 |
| Dialog look / shared widgets | `Fund/FundWindows.cs` (`FundUi`, `FundPopupWindow`), `Platform/TradingDialogs.cs` (`TradingUi`) |

## 6. Tests

Location: `backend/tests/` (unittest, no network, fakes; testkits `*_testkit.py`, `tr_synthetic_fixture.py`).

| Pattern | Area |
|---|---|
| `test_sp_data_*`, `test_sp_network*`, `test_sp_price_sources` | data layer, http_gate, providers, seam |
| `test_sp_strategy_*`, `test_sp_alphas`, `test_sp_features` | spec, DSL, evaluator, alphas |
| `test_sp_lifecycle`, `test_sp_families*`, `test_sp_storage`, `test_sp_secrets` | registry, families, storage, secrets |
| `test_sp_backtest_*`, `test_sp_attribution`, `test_sp_explain*`, `test_sp_resume`, `test_sp_parallel` | backtest, explain, resilience |
| `test_sp_validation_*` | gates, stats, jobs |
| `test_sp_studio`, `test_sp_papers`, `test_sp_paper_strategy`, `test_sp_committee` | research (stage 5) |
| `test_sp_loop*` | discovery loop |
| `test_sp_portfolio`, `test_sp_portfolio_summary`, `test_sp_pretrade`, `test_sp_risk_controls`, `test_sp_oms`, `test_sp_trading_*`, `test_sp_incubation` | stage 7 (+ the tile summary §17.15) |
| `test_sp_broker_*` | stage 8 brokers, live guard, reconcile, binance |
| `test_sp_monitor_*`, `test_sp_insights`, `test_sp_symbol_map`, `test_sp_tr_import` | stage 9 / 9b / Shadow |
| `test_sp_api*` | `/v2` routes |
| `test_sp_review_fixes`, `test_sp_clicktest_1022`, `test_fix1023`, `test_fix1023_round2`, `test_fix1023_round3`, `test_sandbox_profile` | fix batches, sandbox |
| `test_mr_*` | Market Research |
| `test_fund_*` | old engine |
| `test_s10_no_new_fund_imports` | Guard: nothing outside the old engine imports `fund` (exemptions per SP §22.7) |
| `test_s10_council_core`, `test_s10_council_service`, `test_s10_council_routes` | v25 tables, params, `plan_target`, origin `council`; cycle / D7 / D8 / schedule / Run now / D11; `/v2/council/*` + core/llm settings source |
| `test_s10_council_parity` (+ `council_parity_testkit.py`) | SP §22.10 parity gate: old engine vs Council in lockstep from the real §17.9 migration (`UFOS_PARITY_SESSIONS`, default 2; 60 = the full window, run separately), D7, schedule equivalence |
| `test_s10_council_parity_scenarios` (+ `council_parity_synth_bars.py`; optional local `data/local_fixtures/council_parity_real_bars.json`) | SP §22.10 scenarios (judge unavailable, model modes, short / sizing rules, reversals, gates, drawdown, data outages, budget day, restarts), the no-wall-clock check (`WallClockSpy`), the frozen real-data fixture |
| `test_s10_council_switchover` | Switch-over: preconditions, the switch-over transaction, kill / resume / 410 after the switch-over, heartbeat setting, AI budget, pending recovery, revert; review fixes (two-thread double import, pending + retry / abort, recovery failure, minors) |
| `test_s10_hardening`, `test_s10_security`, `test_sp_broker_retired`, `test_s10_env_cs_guard`, `test_s10_env`, `test_s10_lockfile` | venv guard, calendar 2028–2030, races, notices parity; security; Kraken retirement guard; C# `.env` guard, python env guard + settings, lock files |
| `test_os_*` | OS0 (options check, preregistration), OS1 archive (`test_os_archive_*`, testkit `os_archive_testkit.py`) |
| `test_ai_budgets`, `test_usage`, `test_cost_ledger` | AI budgets / ledger |
| `test_disclosures_engine`, `test_disclosures_live`, `test_disclosures_backfill` | Newsroom Disclosures (SP §24) |
| `test_licence_data`, `test_tiles_static` | repo data check; tile static checks (blur privacy, GET-only providers, no guessed HF summary fields) |
| `test_api_auth`, `test_scope_guard`, `test_storage`, `test_store`, `test_calendar`, `test_insider`, `test_macro`, `test_overview` | infra, newsroom |
| `MarketResearch/Chart/tests/run_tests.js` | chart page (node) |

- How to run:
  - While working, run only the touched modules: `python3 -m unittest tests.test_x tests.test_y` from `backend/`, in
    chunks of at most 170 s on the VM.
  - Run the full suite once at the end, preferably in the cloud container (CLAUDE.md "Tests"): tar the HedgeFund
    tree without data / secrets into `backend/data/agent_transfer/`, stage it, run `unittest discover`.
  - WPF has no build here: static checks only. The user builds.
