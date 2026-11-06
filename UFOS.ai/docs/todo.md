# UFOS.ai — TODO

> Part of UFOS.ai, a proof-of-concept portfolio project (not a published product).

The ONE living task list (there is no second copy). Open items only, in priority order. Done items move to
`UFOS.ai/docs/todo-archive/<yyyy-mm>.md` (verbatim); the long-form click-test step lists of the items below are kept
in `todo-archive/2026-10.md` section B. Open task / contract files for agents: `UFOS.ai/docs/tasks/`. Contracts:
`HedgeFund/backend/strategy_platform/SPEC.md` (SP), `HedgeFund/backend/market_research/SPEC.md` (MR),
`HedgeFund/backend/fund/SPEC.md` (old engine); file map `HedgeFund/docs/CODEMAP.md`; rules `CLAUDE.md`.

Last updated: 2026-11-06 (stage 11: new Overview D3 built — backend committed b0f97c6, WPF awaits the user build)

## 0. Current focus (2026-11-06)

User decision 2026-11-06: the career focus is **paused**; the Risk Lab block is **complete** (v0.1–v0.9, commits
d5c3a68 … 4c2eb27 + CI c10521e, merged to master via PR #4; 322 tests; CI `.github/workflows/risklab-tests.yml` green).
Plan / history: Claude Project `claude/career-plan-bank-risk.md`; reports in the user's folder
`C:\Users\leonh\Documents\UFOS.ai` (never in the repo). Development returns to the standard plan with **stage 11**
(§4), starting with an interview.

Open user items:
- [ ] Decide the grey tile strip (MainWindow tiles).
- [ ] Build + click-test the Disclosures section incl. the Congress tab (Newsroom → Disclosures).
- [ ] Prose slots (`\usertext`) in the Risk Lab reports (user, outside the coding chat).

Stage 11 interview 2026-11-06 (user): order as planned (shared styles → Hedge Fund Overview → other HF views → Newsroom →
GM → LSA → Logs; MainWindow stays); **Options Structure is unparked first** — OS2 computations before the Overview, so
its market section can show the gamma regime etc.; a **Risk Lab view** in the app comes after the Overview (Risk Lab stays
a standalone package); screenshot inventory of the current UI runs now (lead, remote).

Done 2026-11-06 (user): the 2 old-engine orders filled at the open (16:34 UTC); RTH check `verify_alpaca_options`
20261106T163343Z = OK (no FAIL); options archive re-registered (DTB3); §17.9 migration to `paper-council` (19:14 UTC) and
the Council switch-over (state `done`, 19:15 UTC; Council orders OFF until the user turns them on). New build with the
stage-11 UI built by the user.
- [ ] Lead proposal pending the user's OK: mark the archive keys quote staleness 5 min / coverage 80 % CONFIRMED (RTH run
  20261106T163343Z: greeks / IV 90.5 % SPY, 91 % QQQ).

Stage 11 Overview (contract `UFOS.ai/docs/tasks/STAGE11_OVERVIEW_TASK.md`, design D3 "Markets + risk", user
2026-11-06): backend `/v2/overview/{markets,risk,activity}` committed (b0f97c6, 2381 tests OK); WPF (5-group rail,
`HedgeFund/Ui/` components, `Platform/OverviewView*`, Legacy tab) reviewed twice, static checks OK, **not built yet**.
- [ ] **User: click-test the Overview** (build done 2026-11-06) (list in the contract §5 and the
  report `2026-11-06_Stage11_Overview.pdf`); report build errors → then the lead commits the WPF part.
- [ ] User decision: broker mirror accounts (alpaca-paper, binance-demo, kraken-demo) stay OUT of the Overview account
  set after migration (lead assumption, SP §26.2)?
- [ ] After migration + switch-over (user decision 2026-11-06): retire Council / old engine (now under Trading › Legacy) — interview on how, then contract.
- [ ] Next in stage 11: Risk Lab view (replaces the placeholder), then the other HF views with the `Ui/` components.

**Parked** (kept, not deleted): LSA-SRC, Depth & Range. R4 / R5 stay scheduled after the
Council switch-over.

## 1. Now — in this order

- [ ] **Click-test of the R2 + R3 build (S10-C, user)** after the switch-over: (1) Council view → "Switch over…" shows
  the dry run (params, heartbeat, AI spend seed, legacy runs), no blockers; confirm → state "done", Council orders
  OFF, no old-engine data any more; (2) restart app / backend → still switched over, the Council task runs (first run
  at open + 5 min + 5 s); (3) Council orders ON, "Run now" in the session → orders on paper-council; (4) kill switch →
  no failed half, Council orders OFF; Resume paper-council → halt cleared, orders stay OFF; (5) app closed longer than
  the heartbeat minutes → auto trading off; (6) Settings → AI budgets: the Council budget edits; (7) optional "Revert"
  → the old engine runs again; (8) a half-finished switch-over shows the error with "Retry" / "Abort".
- [ ] **TILES — module tile previews** (Live / last view blurred / illustration via ⋯ menu; mandatory for every
  module). `UFOS.ai/docs/tasks/TILES_TASK.md` + `TILES_API.md` (decisions §10). WIRED 2026-10-25 (cloud lead session,
  label tiles0): `*.cs.staged` → `*.cs`, project `UFOS.ai.ModuleTiles` referenced by the main app + the three modules +
  `UFOS.ai.slnx`, `MainAppSettings` accepts `tile_modes`, host started in `MainWindow.Window_Loaded`; Hedge Fund Live
  figures from `GET /v2/portfolio/summary` (Equity · Day P&L · Trading + sparkline, SP §25.2). Static checks + offline
  parser check only. **Open (user): build + click-test** — each tile's ⋯ menu (Live / Last view / Illustration, Delete
  last view), Live figures with each backend on and off ("backend off — as of HH:MM" after it was on once), the two
  not-built tiles show the Illustration with "module not built yet", a Last view picture appears after a module window was
  open and active ≥ 60 s (blurred, numbers unreadable), sandbox → SANDBOX chip and no picture. The "⋯" menu now has
  dark templates (W, 2026-10-28). Open decision: the grey tile strip (§0).
- [ ] **Newsroom Disclosures** — INSIDER-RET + CONGRESS backends and the WPF section (Insider / Congress / Track record)
  are built (SP §24, §24.2; done items in the archive). Open: user build + click-test (§0); the Congress watch-list UI
  ("coming next"); "⛶" for Disclosures / Track record via `FundUi.FullScreenButton`.
- [ ] **PARKED 2026-10-28 — LSA-SRC — LiveStream Agent: official sources instead of YouTube** (user decision 2026-10-25; H backend + W
  window; `UFOS.ai/docs/tasks/LSA_SOURCES_TASK.md`): allow-listed official streams with their terms (Fed / ECB /
  Treasury / BLS / BEA candidates — interview first), source picker instead of a free YouTube URL, remove yt-dlp;
  transcript gaps — measure (Deepgram start + duration), show (explicit gap markers + count), fix by cause; tests with
  fakes. Contract `LSA_SOURCES_API.md` first.
- [ ] **R4 / R5 — remove the rest of `fund/`** (stays scheduled after the switch-over + click-test; not started): history export (SP
  §22.12) + golden parity fixture first, then rename to `*.stale` (engine files, shims, `/fund/kill` + `/fund/resume`
  aliases, the §17.9 migration — D12), then R5 delete with the user's permission. Also delete the S10-K `*.stale`
  files and, after the click-test, the 202 copies in `KEEP_UNTIL_CLICKTEST.txt`.
- [ ] **S10-G operating guide** (start / stop, keys, sandbox, options archive, backups, recovery, logs).
- [ ] **Research → Portfolio reads the frozen old account** (finding 2026-10-24): `market_research/integrations.py
  portfolio()` reads the old engine's paper account in newsroom.db; after the §17.9 migration that account is frozen.
  Switch it to `paper-council` / the platform accounts (MR §10) — decide with the lead.
- [ ] **README** (repo root) — setup text for keys (⚙ Keys & data, Credential Manager) and `backend\data\settings.json`
  (lead, in progress).

## 2. Decisions the user still owes

Decided 2026-10-25: **code comments in English** (user) — copilot-instructions follow CLAUDE.md.


- [ ] Holdout: keep the typed strategy name, or a mandatory reason instead (fix1023; today typed name).
- [ ] Startup notice body (`Shared/AiDisclaimerWindow.cs`, "No investment advice: …", user-mandated) and the wording
  in MainWindow / Global Monitoring / LiveStream Agent: follow the stage-10 context notices or stay (SP §23.7).
- [ ] Approve re-freezing the `budget0` scope-guard hash of `UFOS.ai/AiBudgetsWindow.xaml.cs` after a green build
  (compile fix: shadowed `Key`).
- [ ] Module identifier file `module.json` per module (flagged, not approved — ask before building).

## 3. Your build + click-tests (nothing below is confirmed on the PC yet)

One `dotnet build`, then per item. Full step lists: `todo-archive/2026-10.md` section B.
- [ ] **FRED API notice** — done 2026-10-25 (W), click-test pending.
- [ ] **CHART_FULLSCREEN "⛶"** — places 1–7 built 2026-10-24 (W, SP §25.1): Research chart, GM globe, backtest
  report charts, Newsroom macro view, Overview equity curve, Loop grid; focus mode for Orders, Transaction Log, System
  Logs. Click-test: Esc / F11, same instance (drawings / zoom kept), exact window state restored. Disclosures / Track
  record get it when those views exist.
- [ ] **fix1023 confirmation style + sandbox findings** (SP "Fix batch fix1023"): every action is ONE dialog with a
  summary + precise button, notes optional, button busy while sending; double start → 409; paused block shows the
  pause's own numbers; every pause alerts.
- [ ] **Click-test fixes 2026-10-22** (SP "Click-test fixes"): AI-draft parameter form, stress note +
  `warning_summary`, partial-period reports, loop seed reuse, Eurostat TLS (verify the cause on the PC), capacity "At
  most", live-guard text "stage 12".
- [ ] **Stage 10 batches:** S10-K (backend restart; retired Kraken keys only removable), S10-U (second app start brings
  the first to the front; Studio off-topic prompt → one note), S10-A (GM / LSA need a start on the PC), S10-E (import
  dialog for old `.env` files, LiveStream Agent YouTube URL field). Run `make_lockfile.py` once per backend.
- [ ] **Stage 9 / 9b / 9c / 9d** (SP §19 / §20): Monitoring (pause → toast → keep running / demote), alerts,
  reports + AI commentary; Shadow Account imports (TR real export (counts as expected); re-import →
  "already imported"); Portfolio insights + MainWindow card (incl. the card starting the backend); price sources
  (EODHD counter, symbol mapping, Kraken public), live click-test fixes of 2026-10-18 (Run backtest…, Brokers… /
  Ctrl+B, wheel scroll, Research chart + news, import wizard). **Your items:** free EODHD key → ⚙ Keys & data; map
  your positions' listings; load FX rates for the SPY shadow; check the IBKR Flex "Open Positions" section with a real
  export if you ever have one.
- [ ] **Stage 8 / 8b brokers** (SP §18): Alpaca paper (account flat before Connect; routing; mismatch → Resolve;
  Wi-Fi off → waiting; broker kill verified flat); **Binance Spot demo** (HMAC demo keys, "pay fees with BNB" off,
  `tools\verify_binance_demo.py`, baseline connect, long-only routing, USDT amounts, UFOS_-only kill, dust listed,
  demo-UI change → mismatch → accept). Remove the retired Kraken keys in ⚙ Keys & data; re-route any strategy shown
  BLOCKED.
- [ ] **Stage 7 Portfolio & Trading** (SP §17): paper-main card, incubation start, allocation (or "No allocation"),
  auto trading, manual order + rejection + cancel, risk-limit change, migration dry run (the real migration: see §1),
  kill + resume, no poll while hidden.
- [ ] **Stage 6 discovery loop** (SP §16): new loop, estimate + G4 box, generations grid, pause / resume / cancel,
  approval mode, fully autonomous, budget 0, Wi-Fi off → waiting_connection, no holdout button.
- [ ] **Stage 5a / 5b / Studio hardening** (SP §13 / §14): Studio session → AI draft → auto backtest; off-topic (Kant)
  → fixed note only; Paper tab (upload / paste / arXiv) → review → replication card; committee → memo; budgets.
- [ ] **B — central AI budget window** (MainWindow ⚙ → "AI budgets…"; `UFOS.ai/docs/ai-budgets-spec.md`).
- [ ] **Market Research MR1–MR8** (MR SPEC; real chart engine already tested 128 / 128): Research tab + pop-out, Ctrl+K,
  timeframes / types / RTH, live candle, drawings + reload, indicators, watchlists, news, alerts + toast, Portfolio,
  Buy pre-fill, backtest markers. Not verified live: stream paths / news on the free plan, logos plan, WebView2
  composition control.
- [ ] **Stages 1–4 + §10 + §11** (SP §2–§11): keys in ⚙ Keys & data; Library / JSON editor / preview signal; Run
  backtest… (Tiingo key needed for equity backtests; "From 2000"); Validate… (gate table, exception ≥ 30 chars,
  holdout once with the typed name); Network settings + resume after Wi-Fi loss; Attribution + "Explain with AI…".
  Small follow-ups: sort the parameter-grid heat-table columns; check that the dot-com stress episode measures
  strategy and benchmark over the same window. Unverified against live FRED: vintage limit for daily series, future
  FOMC dates in release 101.
- [ ] **Old engine + earlier rounds** (fund/SPEC; until R4): paper engine, crypto 24/7, Copilot review fixes + security
  hardening, storage + environment round, Financial Newsroom (+ review round, Economic Calendar — **FOMC dates are
  hardcoded, update yearly**), AI cost tracking + Transaction Log, Analysis & Reasoning, EU AI Act startup notice +
  labels (toggle "Show AI notice at startup"). Global Monitoring: re-verify the 2026-09-24 fixes on the PC (Assess
  input richness, GDELT placement + rate limit, label truncation).
- [ ] **Delete by hand** (when convenient): `HedgeFund/backend/data/spec1913-wpf-append.md.stale`,
  `.git/*.stale-from-claude` + `git gc`, `HedgeFund/backend/data/backups/incomplete-attempt.*.stale`, unused
  `Global Monitoring/backend/db.py` / `LiveStreamAgent/backend/db.py`, old DB files under
  `%LOCALAPPDATA%\UFOS.ai\data\` (repo cleanup: §5).

- [ ] Optional: run `HedgeFund/tools/check_repo_data.py` from a pre-commit hook (user's choice; today it is a manual
  step of the commit routine, SP §0).

## 4. Next stages (planned)

- [ ] **NEXT (2026-11-06) — Stage 11 — visual overhaul of all modules** (plan §11 in `claude/strategy-platform-plan.md`): shared styles →
  Hedge Fund Overview re-invented (attention / strategies / market / system; the Council is one strategy row) → other
  Hedge Fund views → Newsroom → Global Monitoring → LiveStream Agent → System Logs / Transaction Log. MainWindow stays.
  No animations unless needed, dark only, fixed colour meanings (red/green P&L, blue/orange regime, violet AI, amber
  warning); mockups approved per module; one scope label per module outside HedgeFund.
- [ ] **PARKED 2026-10-28 — Depth & Range revision** (`claude/depth-range-plan.md`): factor attribution, purged / embargoed CV +
  data-snooping test, realistic costs, engine calibration suite, second-source robustness gate, point-in-time XBRL;
  pre-registration, Brier calibration, red-team checks, decision journal, ruin probability.
- [ ] **PARKED 2026-10-28 — Options Structure OS2–OS5** (after stage 10 and Depth & Range; SP §21): OS2 computations (IV, greeks, GEX under
  2–3 dealer assumptions, flip, walls, skew, expected move, VIX term structure, VRP, expiration calendar), OS3 UI
  (Structure tab, overlays, market-state strip, rail regrouped Overview / Strategies / Trading / Research), OS4 daily
  features + regime service, OS5 intraday "light". First H1 / H2 evaluation after ≥ 120 archived sessions; stop
  criterion ~6 months.
- [ ] **Documentation for professional users** — after stage 11, on the presentation screenshots
  (`claude/presentation-plan.md`).
- [ ] Broker adapters IBKR / MetaTrader 5 (interface open, SP §18.3).
- [ ] Stage 12 — first live step: optional, only on the user's explicit go.
- [ ] Backlog from the Reels interest profile R1–R8 (`claude/reels-interest-profile.md`) — not scheduled.

## 5. Smaller open items
- [ ] Click-test 2026-10-25 (lead, remote): full screen OK on the Research chart, macro view, equity curve, orders table;
  FRED notice OK in Macro, Calendar and Keys & data. Findings: (1) the Orders focus mode shows rows without column
  headers; (2) the Overview equity curve's SPY line shows sharp one-day spikes around late Sep / early Oct — check the
  benchmark series for bad bars. Not yet clicked: globe, backtest report, loop grid, Transaction Log, System Logs.

- [ ] LiveStream Agent: handle `verdict_history` in `WebSocketService.cs` (restored verdicts after a restart).
- [ ] Analysis & Reasoning: typed follow-up questions (small chat per analysis, labelled, tier switch, cost hint).
- [ ] Global Monitoring: persistent AI sentiment cache (V2 feature #16).
- [ ] Repo cleanup: `HedgeFund/[OBSOLETE] HedgeFundApp/` and the `UFOS.ai.csproj.Backup*.tmp` files (still present).

## 6. Mid-term / ideas

- [ ] **Documents module** (deferred until the HedgeFund is complete): user documents (local Markdown, folders,
  export, AI drafts labelled) + professional documents (links with preview, paper search: arXiv, OpenAlex, Semantic
  Scholar, SSRN). Interview open: preview fetching security, APIs, folder rules, export, linking, backup.
- [ ] News source for the MainWindow "Markets" card (GMS `news_feeds.py` could be reused).
- [ ] Source for the daily verse card.
- [ ] Module backend lifecycle (shared process supervisor in the app shell for all backends).
- [ ] HedgeFund window: check whether any dashboard part is still illustrative (the platform views are live) — then
  close or wire the rest.
- [ ] Research spikes: Sentiment Optimization; Trading Strategy Discovery (MainWindow tiles illustrative until then).
- [ ] Idea: UFOS.ai as a local read-only MCP server for the user's own Claude client (first prototype: Financial
  Newsroom; read-only tools, untrusted text, AI labels on anything flowing back).
- [ ] Module marketplace (distribution format, versioning, discovery; third-party modules must use
  `Styles/DesignSystem.xaml`).

## Context for future sessions

- **Design ethos (MainWindow, 2026-09-21):** the app shell uses the global `Styles/DesignSystem.xaml`; modules
  duplicate only module-specific style / config; the MainWindow may feel a bit "nerdy" (terminal / Bloomberg-key
  language — finance is often made to look more complicated than it is); do not change other windows' design as a side
  effect.
- Modules stay independent (own `.csproj`, own backend); keys live in the Windows Credential Manager, tunables in each
  backend's optional `data/settings.json` (no `.env` since S10-E).
- Default UI / output language: English (`claude/conventions.md`).
- **A `dotnet build` never restarts a Python backend.** If a backend already answers on its port (GM 8768, HedgeFund
  8867), the app leaves it running — kill and restart `python.exe` for a Python change to take effect (see `Global
  Monitoring/backend/README.md`).
- Until the switch-over the old engine's scheduled AI cycles run whenever the HedgeFund backend runs (capped by the
  "fund" budget, $1/day; set it to 0 to stop them).
- MainWindow design reference: Version 11 (private artifact, link in the Claude Project).
