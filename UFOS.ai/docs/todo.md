# UFOS.ai — TODO

> Part of UFOS.ai, a proof-of-concept portfolio project (not a published product).

The ONE living task list (there is no second copy). Open items only, in priority order. Done items move to
`UFOS.ai/docs/todo-archive/<yyyy-mm>.md` (verbatim); the long-form click-test step lists of the items below are kept
in `todo-archive/2026-10.md` section B. Open task / contract files for agents: `UFOS.ai/docs/tasks/`. Contracts:
`HedgeFund/backend/strategy_platform/SPEC.md` (SP), `HedgeFund/backend/market_research/SPEC.md` (MR),
`HedgeFund/backend/fund/SPEC.md` (old engine); file map `HedgeFund/docs/CODEMAP.md`; rules `CLAUDE.md`.

Last updated: 2026-10-24 (DOCS-SLIM)

## 1. Now — in this order

- [ ] **Monday 2026-10-26 — Options Structure RTH run (user).** During US hours (14:30–21:00 Berlin):
  `.\.venv\Scripts\python.exe tools\verify_alpaca_options.py` in `HedgeFund\backend`. Until then quote staleness
  (5 min) and coverage (80 %) stay PROVISIONAL (SP §21.10). Same day: the options archive's first slot (OI 08:30 New
  York = 13:30 Berlin) — check ⚙ Keys & data → Data sources or `tools\options_archive.py status` / `gaps`. The running
  archive picks up code changes only after a restart (next logon or Unregister… + Register…).
- [ ] **Council switch-over sequence — waits until after Monday 2026-10-26's open** (user decision 2026-10-24). The old
  engine's 2 queued orders (SELL 5 QQQ, COVER 50 TLT) fill at the open → the old account is flat → the user runs the
  §17.9 migration to `paper-council` (Portfolio & Trading → Migration: dry run, then migrate) → then the switch-over
  (Council view → "Switch over…", SP §22.8). **Nothing is cancelled by hand.** Click-test 2026-10-24 21:00: the
  switch-over dry run works and is correctly blocked (`not_migrated`, `no_account`); the migration dry run is blocked
  by `pending_orders=2`.
  - **Is the migration reversible? No.** There is no undo route or function. Once its `pf_migrations` record is
    committed the old account stays frozen for good (`execution/migration.py`: "never un-freeze once one exists"; the
    old tables stay as read-only history; SP §17.14). Only an interrupted migration without a record is undone at
    startup. The switch-over's own revert (SP §22.8) does not undo the migration.
- [ ] **Click-test of the R2 + R3 build (S10-C, user)** after the switch-over: (1) Council view → "Switch over…" shows
  the dry run (params, heartbeat, AI spend seed, legacy runs), no blockers; confirm → state "done", Council orders
  OFF, no old-engine data any more; (2) restart app / backend → still switched over, the Council task runs (first run
  at open + 5 min + 5 s); (3) Council orders ON, "Run now" in the session → orders on paper-council; (4) kill switch →
  no failed half, Council orders OFF; Resume paper-council → halt cleared, orders stay OFF; (5) app closed longer than
  the heartbeat minutes → auto trading off; (6) Settings → AI budgets: the Council budget edits; (7) optional "Revert"
  → the old engine runs again; (8) a half-finished switch-over shows the error with "Retry" / "Abort".
- [x] **GET /v2/portfolio/summary** (Agent P) — BUILT 2026-10-25 (SP §17.15, `UFOS.ai/docs/tasks/PORTFOLIO_SUMMARY_API.md`;
  tests green). Next: W wires the Hedge Fund tile provider to its fields. Original brief: stored values only — equity, day P&L from stored snapshots, open
  position count, auto-trading / kill / halt state, Council orders state; no broker calls, no market data,
  token-protected, cheap enough for a 30 s poll; for the MainWindow tile (W; `UFOS.ai/docs/tasks/TILES_API.md` §10.4 —
  the HedgeFund tile shows "not available yet" until the route exists, then its Live figures are wired).
- [ ] **TILES — module tile previews** (Live / last view blurred / illustration via ⋯ menu; mandatory for every
  module) — owner W. `UFOS.ai/docs/tasks/TILES_TASK.md` + `TILES_API.md` (decisions §10). Slice c built 2026-10-24:
  project `UFOS.ai.ModuleTiles` + main-app / module providers as `*.cs.staged` (wiring pending, label tiles0); GM
  `GET /events/summary` built (H). Open: wiring + build + click-test; the Hedge Fund Live figures after
  `GET /v2/portfolio/summary`.
- [ ] **Newsroom Disclosures (INSIDER-RET)** — backend slices a–c DONE 2026-10-24 (H; SP §24,
  `UFOS.ai/docs/tasks/INSIDER_RET_API.md`). Next: W's Disclosures UI (Insider tab, Track record, status line with
  "Start backfill"; "⛶" via `FundUi.FullScreenButton`), then CONGRESS (`CONGRESS_TASK.md` / `CONGRESS_API.md`).
- [ ] **LSA-SRC — LiveStream Agent: official sources instead of YouTube** (user decision 2026-10-25; H backend + W
  window; `UFOS.ai/docs/tasks/LSA_SOURCES_TASK.md`): allow-listed official streams with their terms (Fed / ECB /
  Treasury / BLS / BEA candidates — interview first), source picker instead of a free YouTube URL, remove yt-dlp;
  transcript gaps — measure (Deepgram start + duration), show (explicit gap markers + count), fix by cause; tests with
  fakes. Contract `LSA_SOURCES_API.md` first.
- [ ] **R4 / R5 — remove the rest of `fund/`** (after the switch-over + click-test; not started): history export (SP
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

- [ ] **Stage 11 — visual overhaul of all modules** (plan §11 in `claude/strategy-platform-plan.md`): shared styles →
  Hedge Fund Overview re-invented (attention / strategies / market / system; the Council is one strategy row) → other
  Hedge Fund views → Newsroom → Global Monitoring → LiveStream Agent → System Logs / Transaction Log. MainWindow stays.
  No animations unless needed, dark only, fixed colour meanings (red/green P&L, blue/orange regime, violet AI, amber
  warning); mockups approved per module; one scope label per module outside HedgeFund.
- [ ] **Depth & Range revision** (`claude/depth-range-plan.md`): factor attribution, purged / embargoed CV +
  data-snooping test, realistic costs, engine calibration suite, second-source robustness gate, point-in-time XBRL;
  pre-registration, Brier calibration, red-team checks, decision journal, ruin probability.
- [ ] **Options Structure OS2–OS5** (after stage 10 and Depth & Range; SP §21): OS2 computations (IV, greeks, GEX under
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
