# Market Research SPEC — archive: mr3-notes

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

### MR3a notes (implemented 2026-10-16: chart page, `MarketResearch/Chart/`)

- **Files (shipped):** `chart.html` (CSP meta exactly §9, no inline script, relative refs),
  `chart.js` (bridge + schema checks + KLineChart 10 adapter + legend; no eval/Function/string timers,
  external text only via `textContent`), `chart.css` (DesignSystem tokens as CSS variables, Consolas
  stack, no web fonts), `vendor/README.txt`. **Not shipped** (`tests/`, csproj `CopyToOutputDirectory=
  Never`): `run_tests.js` (Playwright/Node headless suite, 61 checks), `fake_klinecharts.js` (test double
  of the KLineChart 10 API subset), `csp_probe.js`, `fetch_engine.py`.
- **Engine NOT yet vendored (blocker):** the cloud container and the device VM both get HTTP 403 from
  the egress proxy for registry.npmjs.org, jsdelivr, unpkg, cdnjs, raw.githubusercontent (2026-10-16),
  so `vendor/klinecharts.min.js`, `LICENSE` and `MANIFEST` do not exist yet → ChartHost shows
  `engine_modified` (expected; no fallback engine). Pin: **klinecharts 10.0.3** (npm 2026-08-27,
  Apache-2.0; 10.0.4 = 2026-10-12 and 10.1.0 = 2026-10-14 are < 2 weeks old), tarball
  `https://registry.npmjs.org/klinecharts/-/klinecharts-10.0.3.tgz`, integrity
  `sha512-phvf5CyS7oGE46nuC+nxQxl9D+8+5w8q3reExh0oNeMV1Jws4X0CMHNkOI9xlNA/XWctNS5whZ3PqIJi8YM2Tw==` (read via a
  web reader — confirm with `npm view klinecharts@10.0.3 dist.integrity`). To finish: on a machine with
  registry access run `python HedgeFund/MarketResearch/Chart/tests/fetch_engine.py` (verifies the
  sha512, extracts ONLY `dist/umd/klinecharts.min.js`, `LICENSE`, `NOTICE` if present, writes `MANIFEST`
  in the bridge format, renames old files to `*.stale`), then
  `node HedgeFund/MarketResearch/Chart/tests/run_tests.js --engine real` (needs Playwright + Chromium).
- **Tested (2026-10-16, cloud container, Chromium headless, engine = test double): 61/61** (88/88 after the review fixes below) — ready,
  not_initialised, 15k load (≈ 0.1–0.2 s; budget 1.5 s), 20k load, legend (OHLC, candle % = close vs open
  with ▲/▼, volume, feed chip, "IEX · live tail", local/exchange time zone), paging (one
  `needMoreLeft` with `beforeTs` = oldest `t`, overlap ignored, empty answer ends paging), `updateBar`
  same/newer/older-existing (late-trade correction)/older-missing, `replaceBars` tail → SIP + insertion
  in order, last-price colour by day sign + hide, type switch, crosshair message, 19 invalid messages
  → `bad_message`, > 4 MiB → `too_large`, state unchanged, MR5 messages accepted, CSP blocks fetch /
  XHR / eval / `new Function` / inline script / external image, no request left loopback, 200 live
  updates < 1 s, no console errors. Screenshots (dark) checked visually against the tokens.
- **Engine adapter (KLineChart 10.0.3 semantics, from its `Store.ts`):** data via `setDataLoader`
  (`getBars` type `init` = the `loadBars` bars, `forward` = OLDER bars → `needMoreLeft`, callback held
  until `prependBars`; `backward` never pages); live via the `subscribeBar` callback (same `t` replaces
  last, newer appends); an OLDER bar (late correction, SIP replacement) is patched in place in
  `getDataList()` (the engine's live array) and redrawn once per frame by re-sending the last bar
  (indicator recalc + layout). Volume = built-in `VOL` (no MA lines) in its own pane; custom indicators
  `UFOS_SESSION` (zLevel −1, extended-hours shading from bar `s`) and `UFOS_TAIL` (veil at 55 % background
  over `iex_tail` bars in the price and volume panes = lower opacity, dashed caution marker + "IEX · live
  tail" label). Gapless axis = KLineChart's index axis. Engine tooltips off; legend is page DOM.
- **To verify with the real engine** (test double ≠ engine): style key names (`priceMark.last`,
  `tooltip.showRule`, `area.backgroundColor`), `onCrosshairChange` payload (`kLineData`), indicator
  `draw` return/ordering, `getIndicators({id})[0].paneId`, the engine bundle under the CSP (no
  `unsafe-eval`; `fetch_engine.py` warns if it finds `eval(`/`new Function`), smoothness at 15–20k bars.
- **MR3a review fixes (2026-10-17).** M2: a `loadBars` first answers a held `needMoreLeft` engine
  callback with `([], {forward: false})` (the engine's loading flag no longer depends on `resetData`),
  `initList` is kept until the next `loadBars` (an engine that requests `init` later still gets the new
  bars), a repeated engine paging request replaces the held callback without a second `needMoreLeft`.
  M3: `createIndicator(value{name, paneId}, isStack, paneOptions{id})` (both documented placements of the
  pane), the pane is resolved by indicator NAME via `getIndicators` (return value may be an indicator id
  or a pane id), the volume veil is its own template (`UFOS_TAIL_VOL`) instead of reading
  `indicator.paneId` in `draw`, `getBarSpace()` accepted as object (`bar`) or number. Runtime capability
  checks report `error {code: "engine_api", message: "Chart engine API mismatch: <detail>"}` (once per
  detail): missing `klinecharts.*` / `chart.*` functions (fatal, no `ready`), style keys absent from the
  engine's default styles, `createIndicator` returning nothing or landing on another pane, unexpected
  `draw` params, `getBarSpace` without a width, a crosshair payload without `kLineData`, `getBars('init')`
  not requested / bars missing / `subscribeBar` not called 400 ms after `loadBars`. The test double has
  switches (`resetClearsLoading`, `deferLoad`, `returnPaneId`, `paneFrom`, `barSpaceNumber`,
  `dropChartMethods`, `dropStyleKey`); the reload/paging/live scenario passes in 6 variants and the 3
  mismatch variants produce `engine_api`. L: `fetch_engine.py` requires pinned sha512 == registry
  `dist.integrity` (live `https://registry.npmjs.org/klinecharts/10.0.3`) == tarball sha512 and the
  registry `dist.tarball` == the pinned URL; `--expect-integrity` is an explicit, loudly printed override.
- **Calls to verify with `run_tests.js --engine real`** (only the double was run): `klinecharts.init(dom,
  {locale, timezone})`, `version()`, `registerIndicator({name, shortName, series, zLevel, figures: [],
  calc → array, draw → true})` incl. draw params `{ctx, chart, bounding, xAxis.convertToPixel(dataIndex)}`
  and zLevel −1 drawing BEHIND candles; `createIndicator` with paneOptions (`candle_pane`, `ufos_volume`,
  height 84) and `getIndicators({name})` shape; `VOL` with `calcParams: []` (no MA lines);
  `setDataLoader` (`getBars` types init/forward/backward + `callback(data, {forward, backward})`,
  `subscribeBar` callback); in-place edits of `getDataList()` + redraw by re-sending the last bar;
  `getVisibleRange()`, `getBarSpace()`, `scrollToDataIndex(0)` triggering the `forward` load,
  `scrollToRealTime`, `setBarSpace`, `setTimezone`, `resize`; `subscribeAction('onCrosshairChange')`
  payload; style keys `candle.type` (`candle_solid`/`area`), `candle.area.backgroundColor` gradient array,
  `candle.priceMark.{high,low}.textFamily`, `candle.priceMark.last.{upColor,downColor,noChangeColor,line,text}`,
  `candle.tooltip.showRule`, `indicator.{bars,lastValueMark,tooltip.showRule}`, `separator.*`,
  `crosshair.*.text.*`; the bundle under the CSP (no `unsafe-eval`); smoothness at 15–20k bars.
- **csproj:** `MultiAgentHedgeFund.csproj` + `<None Update="MarketResearch\Chart\**\*"
  CopyToOutputDirectory="PreserveNewest">` and `tests\**` → `Never` (additive; the WebView2
  PackageReference was already there).

### MR3b notes (implemented 2026-10-16: WPF side of the chart — ChartHost, live socket, ResearchView wiring)

- **Files.** New `MarketResearch/Chart/ChartHost.cs` (internal `UserControl`, the bridge host),
  `MarketResearch/ResearchSocket.cs` (the one `/research/ws` client + WS DTOs `WsTrade`, `WsCandle`,
  `WsSipReplace`, `WsSymbolStatus`, `WsError`); changed `ResearchClient.cs` (MR2a REST calls + DTOs
  `AssetSearchResult/AssetItem`, `NumValue`, `MarketPart/MarketStatus`, `SymbolSnapshot`, `SymbolStats`,
  `ApiBar`, `BarsResponse`; `Json` options now internal for the socket), `ResearchView.xaml(.cs)`. No change
  to HedgeFundWindow, the pop-out window, the csproj (MR3a's copy rule + the existing WebView2 1.0.4191.47
  reference are used) or anything outside `HedgeFund/`. Checked statically only (no dotnet on the VM).
- **WebView2 hosting (§9).** One `CoreWebView2Environment` per process, user data
  `%LOCALAPPDATA%\UFOS.ai\HedgeFund\MarketResearch\WebView2` (Global Monitoring keeps
  `…\UFOS.ai\GlobalMonitoring\WebView2`); virtual host **`chart.ufos.local`** (renamed from
  `research.ufos.local`, §9 updated) → `<AppContext.BaseDirectory>\MarketResearch\Chart`, `DenyCors`.
  Only `https://chart.ufos.local/chart.html` (no query) may load; other navigations, all frame
  navigations, new windows, downloads and permission requests are refused; a `WebResourceRequested` filter
  answers 403 for every host but `chart.ufos.local` (defence in depth next to the CSP). Settings: DevTools
  and browser accelerator keys only in Debug builds, context menu / script dialogs / host objects / status
  bar / zoom / pinch / swipe / autofill / password save / built-in error page off. Page → host: source must
  be the chart page, ≤ 64 KiB, `{type, v: 1}`, per-type checks (`needMoreLeft.beforeTs` int64 — the host
  pages from ITS oldest bar, one request in flight; `error.code` `[a-z0-9_]{1,40}`, message ≤ 300);
  host → page ≤ 4 MiB (else strip "too_large"). Engine check before navigating: every `vendor/MANIFEST`
  line (`<file> <version> <url> <sha256> <fetched_at>`, `#` comments and a `… sha256 …` header line
  skipped, paths confined to `vendor/`) hashed with SHA-256; ≥ 1 `.js` listed — else `engine_modified`
  in the chart card (the current state until MR3a's `fetch_engine.py` has run).
- **Control type (decision, default — user away 2026-10-16 — confirm):** `WebView2CompositionControl`
  (visual hosting) instead of the HwndHost `WebView2`: the Hedge Fund window and the pop-out are
  `AllowsTransparency` (layered) windows, and visual hosting lets the WPF loading/error overlay sit on
  top of the chart (no airspace). Trade-off: lower frame rate than windowed hosting (screen capture).
  Switch: `ChartHost.UseCompositionControl = false` (the overlay then hides the native view while shown).
- **Pop-out (decision):** neither control documents moving a live WebView2 to another top-level window,
  so `ChartHost` disposes the WebView on `Unloaded` and creates a new one on `Loaded`; the chart state
  (series meta, ≤ 20 000 bridge bars, type, last price) lives in `ChartHost` and is re-sent after the new
  page's `ready` (`init` → `loadBars` → `setType` → `setLastPrice`). Visible cost: "Starting chart…" for
  about a second after pop-out/return; no REST reload.
- **Time conversion:** only `ChartHost.TryToEpochMs` (ISO → epoch ms, naive = UTC, range 1990–2100) and
  `ChartHost.ToIso` (epoch ms → `yyyy-MM-ddTHH:mm:ss+00:00`, for `before=`). Bars are validated like the
  page does (prices in (0, 1e9], h ≥ l, o/c in [l, h], v ≥ 0 or null, feed enum, `final` present,
  `iex_tail` never final) — an invalid bar is dropped and logged, never repaired; the chart note says how
  many were dropped. The optional bar `s` session flag is NOT sent in MR3 (no extended-hours shading yet:
  classifying pre/rth/post needs the calendar incl. early closes — candidate for the backend in MR4).
- **REST (MR2a shapes).** `SearchAssetsAsync(q, limit)`, `GetMarketStatusAsync()`, `GetSnapshotAsync(s)`,
  `GetStatsAsync(s)` (not used before MR4), `GetBarsAsync(s, tf, session|null, before|null, limit|null)`
  (`session` omitted for crypto — the backend answers 400 otherwise). Timeout 60 s (gate wait ≤ 30 s +
  fetch; feeds/market status 5 s). Data routes show the backend's `error` verbatim: 404 with a JSON
  `error` (SymbolNotCovered), 503 except `ResearchUnavailable` (keys missing, SIP plan, calendar, store),
  400 other than "invalid input" (e.g. the 30-day intraday bound); a 404 without JSON = old backend.
- **WebSocket client (§4.3 / §4.3.1, MR2b messages).** `ResearchSocket.Instance` — one per app; wanted
  while a Research view is shown (visible, host not minimized); on hide `visible:false` at once and a
  clean 1000 close after a 3 s grace (≤ 5 s); a re-show while the old socket is still closing waits for
  it (never two sockets). Token header only (re-read at every connect), `CollectHttpResponseDetails` for
  the upgrade status, keep-alive ping every 15 s with a 10 s pong timeout (.NET 9+ `KeepAliveTimeout`).
  Handshake: `hello{visible:true}` → first `feeds` within 10 s (else reconnect) → ONE `subscribe` with
  the full topic set → `Resynced` (the view re-fetches the open chart if it was loaded before that
  subscribe). Topic set in MR3: `candle:<SYM>:<tf>:<rth|eth|utc>`, `trades:<SYM>` of the ACTIVE tab, then
  `feeds` (inactive tabs are not subscribed in MR3; watchlists/alerts follow in MR4/MR7); changes are
  sent as diffs (`unsubscribe` then `subscribe`). Backoff 1, 2, 5, 10, 30, 30 … s (1013 starts at 5 s),
  counter reset after a connection that lasted ≥ 60 s; 1008/1009 or a server message > 1 MiB (receive
  buffer accumulates ≤ `MaxFrameBytes` = 1 048 576, then close 1009) → "Live connection refused by the
  backend (protocol error) — see backend.log." + ONE retry after 60 s, then no automatic retry until the
  view is shown again; HTTP 401/403 on the upgrade → one immediate retry with a re-read token, then the
  §1.3 token text, re-checked every 60 s; 503 → "Research unavailable … (HTTP 503)", retry 30 s; 404 → old
  backend text, re-checked every 60 s (the 401/404 re-checks are an MR3b choice — §4.3.1 only says "retry
  once"/"show"; confirm). `error` messages are logged; `bad_message` only logged, `candle_unavailable` for
  the active topic shown on the chart (live chip + note), others appended to the status tooltip. `news` /
  `alert` ignored until MR6/MR7. All events on the UI thread; a failing handler is logged, never stops
  the loop.
- **WS vs polling:** `/research/feeds/status` is polled every 5 s only while the view is shown AND the
  socket is not connected; the socket's `feeds` messages render the status bar otherwise. While waiting
  the status bar reads `WAITING · attempt n`, tooltip + reason line "Live connection to the backend lost —
  waiting (attempt n, next try in s s)." (countdown refreshed every second); refused → `NOT LIVE` + reason.
- **ResearchView (minimal MR3 path; full workspace = MR4).** Sidebar box: exact ticker + Enter
  (`[A-Za-z0-9 .-/]{1,32}`, upper-cased, `/` → `-`) → `/research/assets?q=…&limit=20` → exact symbol
  match required ("No exact match …" otherwise) → tab (≤ 12, in memory only). Tab bar with × per tab.
  Chart card: timeframe 1m 5m 15m 1H 4H 1D 1W (default 1D), Candles/Line/Area, RTH ⇄ EXTENDED HOURS
  toggle (stocks only), chips "SIP · 15 MIN DELAYED · THROUGH hh:mm" / "SIP · DAILY BARS" / "ALPACA CRYPTO
  · REAL TIME" and "IEX · LIVE TAIL FROM hh:mm" (when tail candles are loaded), a note line (paging, 30-day
  intraday bound, 20 000-candle limit, dropped bars, symbol/candle reasons). Header (minimal): symbol ·
  exchange · kind, name (plain text), market chip from the snapshot and `/research/market/status` every
  60 s while shown ("● OPEN · CLOSES hh:mm", "PRE-MARKET", "AFTER HOURS · AT CLOSE", "CLOSED · AT CLOSE",
  "HOLIDAY · AT CLOSE", crypto "● OPEN · 24/7"), live chip ("● LIVE · IEX" / "● LIVE · ALPACA CRYPTO",
  "NOT LIVE" + `symbol_status` reason, "NOT LIVE · WAITING (ATTEMPT n)", "CONNECTING…"). States over the
  chart: "Starting chart…", "Loading SYM · tf…", backend reason + Retry (feed unavailable, SIP plan, keys,
  not covered), "No candles …"; host errors (runtime missing, files missing, engine_modified, page
  timeout/crash) + "Reload chart"; page `error` → strip "Chart error (code): message" + Reload/Dismiss.
  Live: `candle` → `updateBar` (same `t` replaces — late-trade corrected finals included), `sip_replace`
  parts → one `replaceBars` each, `trades` → `setLastPrice(price, sign(price − prev_close))`; socket
  down → `setLastPrice(null)` (line hidden, never shown as current). Paging: `needMoreLeft` →
  `/bars?before=<oldest ISO>`; failure → `prependBars{[], false}` + note.
- **Open / verify on the PC:** first build (`dotnet build`); composition control rendering inside the
  layered windows; WebView2 re-creation on pop-out/return; `ClientWebSocket.HttpStatusCode` for refused
  upgrades against aiohttp; engine files (`fetch_engine.py`) → the real KLineChart page.
- **MR3b review fixes (2026-10-17).** M1: live appends / SIP inserts trim the OLDEST bars to 20 000
  (`TrimToMaxBars`, paging ends: empty `prependBars{false}`), `loadBars` re-sends at most the newest 20 000
  → a pop-out/Reload never exceeds the page limit. L: `NavigationCompleted` with `OperationCanceled` for a
  navigation the host cancelled is ignored (no false "page could not be loaded"). L: only
  RenderProcessExited / RenderProcessUnresponsive / BrowserProcessExited are fatal; other process exits
  (GPU, utility, frame) are logged only. L: the host mirrors every page bar check incl. `v ≤ 1e15`
  (comment in `ChartHost.ConvertBar` points to chart.js `LIMITS` / `BAR_FIELDS` / `cBar`); a bad bar is
  dropped + logged. Also: a `needMoreLeft` while nothing more can be loaded is answered at once with an
  empty `prependBars{false}` (releases the page's pending left-edge request).

### MR3 real-engine check (2026-10-18)

KLineChart 10.0.3 vendored via `Chart/tests/fetch_engine.py` (integrity pin == registry == tarball).
Headless run with the real engine (cloud container, Playwright + Chromium) found two issues, fixed in `Chart/chart.js`:
- `onCrosshairChange` in 10.0.3 passes the raw crosshair `{x, y, paneId}` (no `kLineData`); the bar under the
  crosshair is now resolved via `convertFromPixel` -> `dataIndex` -> `getDataList()[i]` (host `crosshair` message had `bar: null`).
- Live-candle updates (same `t` as the last bar) are applied in place and redrawn once per frame (`refresh()`); a new
  candle still goes to the engine at once. 200 updates: 1.8 s -> 33 ms.
Result: real engine 128/128, fake engine 229/229. Previous chart.js kept as `chart.js.20261018-precrossfix.stale`;
the `vendor/*.stale` copies come from a second fetch run. Both are copied to the output by the `MarketResearch\Chart\**` glob —
stage-10 cleanup candidates.

