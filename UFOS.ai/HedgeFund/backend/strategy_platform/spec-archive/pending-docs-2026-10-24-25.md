# Strategy Platform SPEC — archive: pending doc notes of the doc freeze (2026-10-24 / 25)

Verbatim notes of Agents H and W held during DOCS-SLIM (PENDING_DOCS_H.md / PENDING_DOCS_W.md), merged into the current contract on 2026-10-25 (SP §0 licence rule, §22.8 / §22.10, §24, §25; MR §10; CODEMAP; todo). Contract files they name are kept in `UFOS.ai/docs/tasks/`.

# PENDING_DOCS_H — Agent H's doc changes held during DOCS-SLIM (doc freeze from 2026-10-24, after commit 4df6db3)

## GM-SUMMARY (done 2026-10-24, label gmsummary0)
- **Global Monitoring backend README, HTTP API list:** add
  `GET /events/summary?hours=24` — module tile: events of the last N hours per category plus feed status. It is a pure
  read (no collector wake-up, unlike `/events`), token-protected, and `hours` must be 1–168 (400 otherwise).
  Contract: UFOS.ai/docs/tasks/GM_SUMMARY_API.md.
- **CODEMAP, Global Monitoring backend:** add `events_summary.py` — parse_hours / summarize for GET /events/summary (no I/O).
  Add `tests/test_events_summary.py`.
- **todo (Global Monitoring):** done — read-only tile endpoint (TILES_API §9 Q5 answered with option "pure read").

## INSIDER-RET (2026-10-24, contract UFOS.ai/docs/tasks/INSIDER_RET_API.md, decisions §11) — slices (a)–(c)

### conventions / SPEC — retention rule
- Disclosures (insider trades, later congress) are kept **2 years** by filing date: `disclosure_trades`,
  `disclosure_returns` (`newsroom/db.py` `DISCLOSURES_RETENTION_DAYS = 730`, daily `prune()`).
- The 5-day `insider_filings` list (full payload, the old Insider Trades popup and AI summary) is unchanged.

### newsroom.db
- Schema **v4**: `disclosure_trades`, `disclosure_returns`, `issuer_sic`, `disclosure_jobs` (see INSIDER_RET_API §3).
- `prune()` result gains the key "disclosures" only when rows were removed.

### HedgeFund backend README — HTTP API (token-protected)
- `GET /newsroom/disclosures/insider` — list with filters `days`, `code`, `role`, `plan`, `cluster`, `ticker`,
  `measurable`, `amendments`, `limit`, `offset`. Returns "N of M shown" counts and ONE return chip per row.
- `GET /newsroom/disclosures/insider/{id}` — details: both start points, all horizons, 10b5-1, official SEC link.
- `GET /newsroom/disclosures/track-record?source=insider&group=direction|plan|role|cluster&horizon=&start=` —
  n, mean/median score, hit rate, small-sample flag, caveat (descriptive; SIC → GICS sector ETF approximation).
- `GET /newsroom/disclosures/markers?ticker=&from=&to=&kinds=insider` — dated by the filing day, merged per day/side.
- `GET /newsroom/disclosures/status` — backfill steps and gaps, daily job, live writes, counts, unmapped SIC codes,
  DB sizes, current SEC interval.
- `POST /newsroom/disclosures/backfill {"action": "start"|"pause"|"resume"}` — the user's button. A web page
  (Origin header) gets 403; a wrong state gets 409.

### Behaviour notes for SPEC / README
- **Returns:** from filing = first 1-minute IEX bar after the EDGAR acceptance minute, or the next open (after hours
  or no trade). From trade date = close (not tradable). Horizons 1/5/20/60 sessions; sector ETF over the same
  window; excess = r − r_etf; sales scored −excess. Adjusted bars (splits + dividends). No SPY substitute.
- **Clusters:** point-in-time: only trades already filed count, so a later filing never relabels an earlier trade
  (avoids look-ahead in the Track record). The thresholds are those of `insider_summary` (2 buyers / 3 unplanned
  sellers within 14 days).
- **Shared SEC limiter** (`newsroom/sec_limiter.py`): live collector + daily job + backfill share one request
  schedule (`sec_edgar_min_request_interval_seconds`; 0.2 s while the backfill's EDGAR steps run).
- **Daily job:** 17:00 New York on trading days (+ at start when missed), research priority, no AI.
- **Backfill:** user-started. SEC quarterly data sets (links read from the SEC page) → issuer submissions for
  acceptance times + SIC (a pilot stops and asks if they are missing) → gap days via the daily index → prices
  outside US market hours.

### CODEMAP rows (HedgeFund backend, newsroom/)
- `disclosure_db.py` — storage of the Disclosures (trades, returns, SIC, jobs); never swallows errors.
- `disclosure_trades.py` — Form 4 → trades (P/S aggregation, role class, point-in-time clusters).
- `disclosure_returns.py` — return engine (entries, horizons, ETF excess, score, chip, Track-record stats) +
  `AlpacaPrices`.
- `sector_map.py` + `sector_etf_map.json` — SIC → sector ETF (one editable file with source/date).
- `sec_limiter.py` — one fair-access limiter for every sec.gov request.
- `disclosures_service.py` — live hook, SEC fetch, daily job + scheduler.
- `disclosures_api.py` — `/newsroom/disclosures/*` routes.
- `disclosures_backfill.py` — the 2-year backfill (steps, pause/resume, status).
- tests: `test_disclosures_engine.py` (slice a), `test_disclosures_live.py` (b), `test_disclosures_backfill.py` (c).

### todo
- INSIDER-RET backend done (slices a–c). Next: W's Disclosures UI (Insider tab, Track record, status line with
  "Start backfill"), then CONGRESS.

## LICENCE clean-up (lead 2026-10-25)

### conventions — new rule
- **No licensed market data in the repository.** Real provider data (Alpaca/Tiingo/EODHD bars, quotes, news) and
  personal data (broker exports, IBANs, account numbers) are never committed, not even as test fixtures.
- Tests use synthetic, seeded data. A real-data variant may read a git-ignored file under
  `backend/data/local_fixtures/` and must skip with a clear reason when the file is absent.
- **Check before every commit:** `python HedgeFund/tools/check_repo_data.py` (stdlib, read-only git) must say OK.
  - It scans exactly what would be committed (tracked + untracked-not-ignored) for:
    - data files outside `vendor/`;
    - price-series-like JSON / .py / .txt;
    - Alpaca bar records;
    - IBANs with a valid checksum, Alpaca account numbers, the `tr-import` folder;
    - `vendor/` files whose sha256 differs from their MANIFEST.
  - Reviewed false positives go in `HedgeFund/tools/repo-data-allow.json` (path + rule + reason).

### SPEC §22.10 (c) / CODEMAP — the Council parity gate
- `tests/council_parity_synth_bars.py` (new) generates SYNTHETIC bars of the old fixture's shape at test time: SPY,
  QQQ, TLT, GLD and BTC-USD; 260 bars up to 2026-10-23; equities on NYSE days and BTC 7 days a week; a seeded
  random walk with realistic drift and volatility.
- `council_parity_testkit`:
  - `BarDocWorld` → `SyntheticBarsWorld` (committed) and `RealWorld` (local file `REAL_BARS` =
    `backend/data/local_fixtures/council_parity_real_bars.json`);
  - `real_bars_available()` and `REAL_BARS_MISSING`.
- `test_s10_council_parity_scenarios`: `SyntheticBarsTest` always runs; `RealDataTest` is skipped when the local file
  is absent.
- `HedgeFund/tools/export_council_parity_bars.py` (new, stdlib, next to scope_guard.py) re-exports the local file from a COPY of hedgefund.db: it copies
  the db plus -wal/-shm, checks that the sha256 is unchanged before and after, opens only the copy, takes one source
  per day (alpaca_sip > alpaca > tiingo) from the `adjustment = 'all'` series, and keeps the last 260 bars.
- `HedgeFund/tools/check_repo_data.py` + `repo-data-allow.json` (new); tests in `tests/test_licence_data.py`.

### todo
- Done: licence clean-up (synthetic parity fixture, local real-data variant + export tool, repo data check).
- Proposed: run `check_repo_data.py` in the commit routine (a pre-commit hook is the user's choice).


# PENDING_DOCS_W — Agent W's doc changes held during the doc freeze (after commit 4df6db3)

## CHART_FULLSCREEN (done 2026-10-24; HedgeFund files plus label fullscreen0 for the files outside HedgeFund)

### CODEMAP
- **Shared:** add `FullScreenHost.cs`. It is the one full-screen block for charts, maps and dense tables, linked into all 4
  assemblies.
  - It works in place and never re-parents anything, so a WebView2 is never re-created.
  - The window covers the whole monitor, taskbar area included, and is topmost while it has focus.
  - Everything off the path to the target is collapsed. Every change is recorded and undone in reverse order on exit, and
    the window state is restored exactly.
  - A 30 px adorner bar shows the title, the header text (refreshed every second) and an "Exit full screen" button.
  - Esc and F11 exit. Esc that an inner element has already handled (for example a popup or a dropdown) closes only that
    element.
  - API: `Enter`, `Toggle`, `Wire`, `CreateButton`, `RegisterF11`, `IsActive`, `ActiveFor`, `Exit`, and the event `Changed`.
- **HedgeFund Fund/FundWindows.cs (FundUi):** add `FullScreenButton(target, title, header, f11Scope, registerF11)` and
  `HeadingWithFullScreen(heading, button)`.
- **Places:**
  - **Research chart:** `ResearchView.xaml` has `ChartFullScreenButton`; the target is `ChartCard`. Works in the pop-out window too.
  - **Global Monitoring globe:** `MainWindow.xaml` has a title-bar `FullScreenButton`; the target is `GlobeArea`.
    `globe.html` gets `{type:"fullscreen", on}`: the layer toggles become a floating menu and the event feed can be collapsed.
  - **Backtest report:** `BacktestReportView.cs` has "⛶" in the section headings for equity, drawdown and IC decay
    (`Section(title, button)` / `ReplaceSection`).
  - **Newsroom macro view:** `MacroIndicatorsView` has a "⛶" next to Refresh; the whole view goes full screen. While it is
    full screen, `MacroListContent` is zoomed by 1 to 2 (≥ 1500 px of layout width), because the sparklines are drawn at a
    fixed size.
  - **Overview equity curve:** `HedgeFundWindow.xaml` has `EquityFullScreenButton` in `EquityCurvePanel`; the target is
    `EquityChartHost`. The bar shows equity, day P&L and total P&L.
  - **Discovery Loop:** `LoopView` puts the generation × slot grid in a persistent `_gridHost`. While it is full screen,
    only the grid refreshes in place and the rest of the body re-renders on exit. Clicking a cell exits and opens the
    variant.
  - **Focus mode, Orders:** `TradingView.Sections.cs` puts "⛶" next to "New paper order…"; the target is the blotter list.
  - **Focus mode, Transaction Log:** `TransactionLogWindow` has a "⛶" next to Open folder; the target is `EntriesPanel` and
    the bar shows the totals.
  - **Focus mode, System Logs:** `LiveStreamAgent/BackendLogsWindow.cs` has a "⛶" in the filter bar; the target is the log
    text. Esc ends focus mode before it closes the window.
  - Disclosures and Track record come later.
- **Scope label:** `HedgeFund/tools/scope-allow-fullscreen0.json`, with amendment 1 for the place-7 files. Backups are in
  `HedgeFund/backend/data/fullscreen-backup/*.pre-fs.stale`.

### SPEC (UI section, cross-module)
- Full screen rule: every chart, map and dense table has "⛶" (tooltip "Full screen (Esc to exit)"), and F11 works where
  the keyboard focus is inside it.
  - True full screen of the monitor.
  - Same instance: no reload, and drawings, zoom and timeframe are kept.
  - The bar at the top shows the title and live header text, with an exit control on the right.
  - Esc restores the exact window state.
  - A view that is replaced while full screen (for example by switching the section or closing the pop-out) exits full
    screen first.

### todo
- Done: CHART_FULLSCREEN places 1 to 7. Disclosures and Track record are still open and will use `FundUi.FullScreenButton`
  once those views exist.

## council_legacy_duplicates (SWITCHOVER_FIX_API addendum)
- **CODEMAP / SPEC §22.14:** `CouncilBlocker` gets `table` and `rows`. The switch-over dialog shows "Table cl_runs ·
  n duplicate row(s)" under the blocker. The alert kind needs no extra mapping, because every `council_*` alert gets the
  "Council view…" button.

## TILES (slice c, 2026-10-24; label tiles0)

### CODEMAP
- **New project `UFOS.ai.ModuleTiles`** (net10.0-windows, WPF, WebView2 1.0.4191.47, references UFOS.ai.Logging). It is
  public and referenced by the main app, HedgeFund, Global Monitoring and LiveStream Agent.
  - `TileContracts.cs`:
    - `IModuleTilePreview` with `ModuleId`, `DisplayName`, `Live`/`LastViewUnavailableReason`, `ReadLiveAsync`,
      `CreateIllustration`, `OwnsWindow`, `IsSandbox`.
    - The types `TileMode`, `TileTone`, `TileFigure`, `TileDot`, `TileLiveData` and `TileLiveResult`/`State`.
  - `TileStorage.cs`:
    - `TileBrushes`: design-system keys; a missing key is an error.
    - `TilePaths`: `%LOCALAPPDATA%\UFOS.ai\tile-previews\{live,last-view}`.
    - `TileLiveStore`: stored figures, schema 1.
    - `TileLastViewStore`: blurred PNG plus metadata. `Delete` serves the "Delete last view" menu item.
  - `TileBlur.cs`: area average down to 1/10, then a 5×5 box blur run twice, all in memory. `BlurredPng` can only be
    created here, and the store accepts only that type.
  - `TileCapture.cs`, `TileCaptureService`:
    - A capture every 60 s of each visible and active module window (`OwnsWindow`; also windows opened outside the tile,
      found via a Window Loaded class handler).
    - One more capture at Closing, only if it is ready within 300 ms.
    - WebView2 and WebView2CompositionControl are captured with `CapturePreviewAsync`.
    - No capture while the provider reports sandbox data.
  - `LoopbackReader.cs`: GET only, against 127.0.0.1. `/health` (800 ms, no token) decides "running"; requests carry the
    token, ≤ 3 s, ≤ 256 KB, no proxy or redirects.
  - `TileArt.cs`: building blocks for the motifs (160 × 90 canvas).
- **Main app `Tiles/`:**
  - `ModuleTileHost.cs`: attaches a preview per tile (adds a third row to the tile Grid; "⋯" bottom right), refreshes Live
    every 30 s and pauses while the window is minimised or hidden, applies the mode logic and starts the capture service.
  - `TilePreviewView.cs`: the renders and the "⋯" menu, plus the tile's AutomationName.
  - `TileModeSettings.cs`: the `tile_modes` key in app-settings.json.
  - `MainAppTilePreviews.cs`: Transaction Log (ledger spend, plus "budgets: see AI budgets"), System Logs (errors and
    warnings over 24 h in app.log and the HF/GM backend.log plus rotations; LSA keeps no log file), and NotBuilt
    (Sentiment Optimization, Strategy Discovery).
- **Module providers:**
  - `HedgeFund/Tiles/HedgeFundTilePreview.cs`: `/health` only. Shows "not available yet" until P's
    `GET /v2/portfolio/summary` exists; no guessed fields. TODO: the figures once the route is confirmed.
  - `Global Monitoring/Tiles/GlobalMonitoringTilePreview.cs`: `GET /events/summary?hours=24` (H's pure read); category
    dots, total and feed status.
  - `LiveStreamAgent/Tiles/LiveStreamAgentTilePreview.cs`: `GET /verdicts?limit=1`; last verdict and confidence
    (✦ AI, model) and the "last verdict at" time.
- **Shared/MainAppSettings.cs:** accepts the key `tile_modes`, which TileModeSettings validates.
- **MainWindow:** the tiles `SentimentOptimizationTile` and `StrategyDiscoveryTile` are now named; `StartTilePreviews()`
  runs in Window_Loaded.
- **Test:** `HedgeFund/backend/tests/test_tiles_static.py`:
  - Blur privacy: only the blurred type is written.
  - Providers: GET only, no process start, no AI.
  - HedgeFund: no guessed summary fields.

### SPEC (main window, module tiles)
- The three preview modes per tile, the mode persistence, and the Live rules: no backend start, no AI or paid call;
  stored figures with "backend off — as of HH:MM", accepted by the user 2026-10-24. Sandbox figures are shown with a
  SANDBOX chip and never stored.
- Last view privacy: blurred in memory before any write, stored only under LOCALAPPDATA, no picture of sandbox data.
- Capture timing per §10.2.
- A mode a module cannot offer is a disabled menu item with its reason, and the tile shows the Illustration with that
  reason.

### todo
- Open: the Hedge Fund Live figures after P's `GET /v2/portfolio/summary`.
- Tile previews for Disclosures and Track record are not needed (they are not tiles).
