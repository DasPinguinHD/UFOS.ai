# Market Research SPEC — archive: 10-stages-and-bridge-contracts

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 10. Stages — deliverables + acceptance tests

Each stage: implement → tests → independent review → fix → update this
SPEC (+ notes section) / docs / todo → report with a click-test list.

### MR1 — section shell

Deliverables (backend): package files `__init__.py`, `limits.py`,
`supervisor.py`, `store.py` (v1), `feeds.py`, `api.py` with
`GET /research/feeds/status` (§1.2); main.py: guarded install (503 stub on
failure), `service.start()` after `engine.start()`, `await
service.shutdown(2.0)` in `/shutdown`; supervised task `store_maintenance`
(opens the store in a Research pool thread, retries every 60 s while unavailable, prunes
daily). (WPF): rail entry "Research" (tooltip "Market Research — charts,
watchlists, news") after Overview/Library, content created on first use,
`ResearchView` skeleton with the sidebar sections Overview / Symbol / Alerts
(empty states), status bar from feeds/status (§1.5), "⧉ Pop out" →
`ResearchPopoutWindow` (borderless, 1440×900, min 1100×680, StateChanged +
drag-to-restore), closing it returns the view to the rail section.
Acceptance tests (`tests/test_mr_shell.py`): a crashing Research task →
`research.state == "unavailable"` with the reason, bounded restarts,
`failed` after the budget, while a dummy "engine" task keeps ticking; a
returning task counts as a crash; `stop()` bounded; store migrations
idempotent, WAL, corrupt file renamed + recreated, newer schema refused;
route: 401 without token (api_auth), exact §1.2 key set, 400 on a query
parameter, 500 without internals; main.py wiring.

### MR2 — data layer extensions

Deliverables: §4.2 MR2 routes, §7 (assets refresh daily as supervised
task `asset_refresh`, market status, model c, aggregation, stats, hub,
gate priority, WebSocket). Strategy Platform extensions recorded in §12
and in its SPEC. Acceptance tests (`test_mr_data_*.py`, fakes only): DST
switch weeks (US vs EU dates), early close (last bucket 13:00), RTH/ETH
anchors for 1H/4H, aggregation values/vwap, tail → SIP replacement, stats
§7.4 incl. YTD and null-with-reason, crypto UTC day, market status states,
assets search order + 503, hub: multiplexing, 30-symbol limit message, 406
→ unavailable, reconnect backoff + probe use + waiting message, lazy
start/linger, gate priority (research ≤ 60/min, yields to a waiting normal
caller, normal path unchanged), WebSocket protocol limits.

### MR3 — chart

Deliverables: `HedgeFund/MarketResearch/Chart/` with `vendor/klinecharts.min.js`
(exact 10.x version ≥ 2 weeks old, LICENSE, NOTICE if shipped, MANIFEST:
`name version source_url sha256 fetched_at`), `chart.html/.js/.css`,
`ChartHost.cs` (WebView2 1.0.4191.47 PackageReference in
`MultiAgentHedgeFund.csproj`, F3), bridge (plan §3.3 message list, security §9,
every message `{type, v: 1, …}`, bars as `{t: epoch ms UTC, o, h, l, c, v,
feed, final}` — converted from the API's ISO `t` in `ChartHost.cs` only, §0
Time), candles/volume, timeframes, types, legend (candle % =
close vs open), last-price line coloured by the DAY change, live candle,
paging (`needMoreLeft`), tail candles drawn with lower opacity.
Acceptance: Playwright headless in the cloud container (Chromium at
/opt/pw-browsers, synthetic data, screenshots): renders, CSP blocks
`fetch`, schema rejects bad messages, paging, live update, tail styling;
WPF checked statically.

#### MR3 bridge contract v1 (WPF `ChartHost.cs` ↔ `chart.js`) — binding for MR3a and MR3b

Written 2026-10-16 by MR3b (WPF side) while MR3a builds the page in parallel; MR3a may refine
it — any change goes into this block AND both sides.

- **Hosting.** Page URL `https://chart.ufos.local/chart.html` (virtual host mapping
  `chart.ufos.local` → `<app base dir>\MarketResearch\Chart`, `DenyCors`; the csproj copies
  `MarketResearch\Chart\**` to the output). The page references its files RELATIVELY
  (`chart.js`, `chart.css`, `vendor/klinecharts.min.js`), never the host name. CSP as §9 (meta tag in
  `chart.html`). No token, no network, no `eval`, host objects off.
- **Engine check (C#, before navigating).** `vendor/MANIFEST`: one line per vendored file,
  whitespace-separated `<file name relative to vendor/> <version> <source_url> <sha256 hex> <fetched_at>`
  (`#` lines = comments). ChartHost hashes every listed file; a missing MANIFEST, a listed file that is
  missing, a mismatch, or no listed `.js` file → chart error `engine_modified` "Chart engine files do
  not match vendor/MANIFEST (…) — reinstall/rebuild the app." The page is NOT loaded then.
- **Envelope.** Every message is a JSON object `{"type": <string>, "v": 1, …}`. Host → page:
  `CoreWebView2.PostWebMessageAsJson` (page: `window.chrome.webview.addEventListener("message", e => e.data)`
  — `e.data` is the object). Page → host: `window.chrome.webview.postMessage(<object>)` (host reads
  `WebMessageAsJson`; a JSON *string* holding the object is accepted too). Size caps: host → page ≤ 4 MiB
  serialized (host refuses to send more → chart error `too_large`), page → host ≤ 64 KiB (host drops
  larger messages, logs once). Unknown `type` / wrong `v` / wrong field types → dropped + logged on the
  receiving side (page: `error {code: "bad_message"}` back to the host, once per type).
- **Bar (bridge).** `{"t": <int, epoch ms UTC, bar start>, "o", "h", "l", "c": <finite number>,
  "v": <finite number|null>, "feed": "sip"|"iex_tail"|"sip_daily"|"crypto", "final": <bool>}` —
  converted from the API's ISO `t` in `ChartHost.cs` only (§0 Time). Lists oldest first, `t` strictly
  increasing. `feed: "iex_tail"` (always `final: false`) = drawn with lower opacity (live tail, §7.1).
- **Host → page.**
  - `init {theme: {background, grid, text, axisText, muted, up, down, accent, caution, fontFamily},
    locale: "en-US", tz: <IANA zone of the PC|null>}` — colours `#RRGGBB`; sent once after every
    `ready` (also after a page reload / WebView re-creation), before anything else.
  - `loadBars {symbol, tf: "1m"|"5m"|"15m"|"1H"|"4H"|"1D"|"1W", session: "rth"|"eth"|"utc",
    assetClass: "equity"|"crypto", exchangeTz: "America/New_York"|"UTC", bars: [bar…],
    hasMoreLeft: bool, sipThrough: <epoch ms|null>}` — replaces EVERYTHING (new symbol/tf/session or a
    reload after a reconnect); resets the view to the right edge. `bars` may be empty (page shows an
    empty chart; the WPF overlay explains why).
  - `prependBars {bars: [bar…], hasMoreLeft: bool}` — older history (answer to `needMoreLeft`); all `t`
    < the current oldest bar (overlapping `t` are ignored by the page); keeps the visible range.
    `bars` may be empty (then only `hasMoreLeft` changes).
  - `updateBar {bar}` — live candle: same `t` as an existing bar → replace it (may be OLDER than the
    last bar: late-trade correction, MR2b H1); `t` newer than the last bar → append; older and not
    present → ignored.
  - `replaceBars {bars: [bar…]}` — SIP replacement (`sip_replace`, one message per WS part): each bar
    replaces the bar with the same `t`, or is inserted in time order when missing; nothing else changes.
  - `setType {chartType: "candles"|"line"|"area"}`.
  - `setLastPrice {price: <finite number|null>, dayChangeSign: -1|0|1|null}` — the last-price line,
    coloured by the DAY change (up/down/neutral; null = unknown → neutral). `price: null` hides the line
    (live connection lost → never shown as current, §4.3.1).
  - Reserved for MR5 (not sent in MR3): `setIndicators`, `loadDrawings`, `setTool`, `command`.
- **Page → host.**
  - `ready {engine: "klinecharts", engineVersion: <string ≤ 32>}` — after the message listener is
    installed and the chart object exists; may come again after a reload.
  - `needMoreLeft {beforeTs: <int epoch ms>}` — the user scrolled to the left edge; the host ignores it
    while a page is loading or when `hasMoreLeft` is false, and always pages from ITS oldest bar (the
    page's value is only a trigger). At most one request in flight.
  - `crosshair {bar: <bar|null>}` — optional; ignored by the host in MR3 (the legend is in the page).
  - `error {code: <[a-z0-9_]{1,40}>, message: <string ≤ 300>}` — the WPF chart card shows
    "Chart error (<code>): <message>" with Reload; the rest of the window keeps working (§8).

- **Refinements by MR3a (2026-10-16, backward-compatible; the page implements them).**
  - `init` may also carry `timeMode: "local"|"exchange"` (default `local` = `tz`, or the PC zone when
    `tz` is null; `exchange` = `loadBars.exchangeTz`); `init` may be re-sent at any time (theme / time
    zone switch). `theme` may also carry `upDim`, `downDim` (`#RRGGBB`, volume bars; default = up/down
    at 55 % alpha). Any other theme key → `bad_message`. `fontFamily`: local font names only
    (`[A-Za-z0-9 ,"'-]{1,120}`), no web fonts.
  - `loadBars` may also carry `pricePrecision` (0–10) / `volumePrecision` (0–8); default: from the
    data (≥ 2 decimals, ≥ 4 below 1.00) / 0 for equities, 4 for crypto. `symbol` must match
    `^[A-Z0-9][A-Z0-9./-]{0,31}$`. Max 20 000 bars per `loadBars` / `prependBars`, 5 000 per `replaceBars`.
  - Bar: optional `s: "pre"|"rth"|"post"` (session flag → extended-hours shading; no flag, no shading —
    the page never guesses sessions, early closes would be wrong), optional `vwap`, `n` (accepted,
    unused). Checks: `t` int in [1990, 2100), prices finite in (0, 1e9], `h ≥ l`, `o`/`c` within
    [`l`, `h`], `v` null or finite ≥ 0, `iex_tail` never `final`; unknown bar keys → `bad_message`.
  - `prependBars {bars: [], hasMoreLeft: true}` ends paging for the current load (the engine would
    re-trigger `needMoreLeft` at once) — on a fetch failure the host sends `hasMoreLeft: false` and shows
    its own error. A `prependBars` without a pending `needMoreLeft` is merged in place.
  - `setLastPrice`: the dashed line + axis label sit at the LAST BAR CLOSE (engine price mark, = last trade
    while the live candle updates), coloured by `dayChangeSign` (null/0 → muted); `price: null` hides it.
  - `bad_message` / other errors are de-duplicated per (code, type, message) within 2 s.
  - MR5-reserved messages are accepted now, schema-checked, no effect: `setIndicators {indicators:
    [{name, id?, params?, pane?: "overlay"|"sub", visible?}] ≤ 16}`, `loadDrawings {drawings: [{id: 32 hex,
    tool, points: [{t, p}] ≤ 16, text?, style?, locked?, visible?}] ≤ 500}`, `setTool {name|null}`,
    `command {name: undo|redo|lock|hideAll|deleteSelected|resetView}` (`resetView` already works).
  - Page error codes: `bad_message`, `too_large`, `not_initialised` (data before `init`),
    `engine_missing`, `engine_version` (not 10.x), `engine_init`, `internal`.

#### MR5 bridge contract (revision 2: drawings + indicators) — binding for the MR5 chart page and MR5 WPF

Written 2026-10-17 by the MR5 chart-page agent BEFORE the code, from the WPF agent's proposal; any
change goes into this block AND both sides. Additive to "MR3 bridge contract v1": same envelope
`{type, v: 1}`, same caps (host → page ≤ 4 MiB, page → host ≤ 64 KiB), unknown types/fields still
`bad_message`. The page's `ready` gains `revision: 2` (int; absent = MR3 page). Point `t` = epoch ms on
the bridge (ChartHost converts ISO ↔ ms, §0 Time).

- **Bridge drawing** = the "MR5 backend notes" drawing with ms `t`:
  `{id, tool, points: [{t, p}], style: {color, width, dash, levels}, name, text, locked, visible,
  timeframes, alert_id}`; the backend's `symbol`, `price_basis`, `created_at`, `updated_at` may be passed
  through unchanged (accepted, ignored). Palette → colour: `accent`, `up`, `down`, `caution`, `text`,
  `muted` = the `init.theme` colours; `blue` = `#4C8DFF`, `violet` = `#B48CF2` (fixed page colours —
  the WPF style swatches use the same values). Validation exactly as the backend (tools + point counts,
  `brush` 2–500 points, palette keys, width 1–4, dash, `levels` only for `fibonacciLine` (1–12 distinct
  numbers in [−100, 400]), `text` ≤ 280 / `name` ≤ 40 plain text, `simpleAnnotation` needs `text`,
  `alert_id` 32 hex only for `alertLine`, `timeframes` `"all"` or distinct tfs). Text is rendered as
  plain text (canvas text / `textContent`), never HTML.
- **Ids.** The page creates drawings with a local id `localId` = `"c-<n>"` (`^c-[0-9]{1,9}$`); the host
  POSTs and answers `drawingSaved`; the page re-keys the drawing to the server `id` (32 hex). Changes
  made before the answer are queued and sent (as `drawingChanged` with the server id) right after it.

**Host → page**
- `loadDrawings {drawings: [drawing…] ≤ 500, append?: bool}` — ALL drawings of the loaded symbol (the
  PAGE filters by `timeframes` against the current tf and re-filters on every `loadBars`); without
  `append` it replaces everything and clears the undo history; `append: true` adds a further chunk
  (for > 4 MiB). Sent after `loadBars` when a symbol opens, after delete-all (empty list) and after an
  import (`replace`).
- `upsertDrawing {drawing}` — persisted state from the backend (import append, split rescale, a PATCH
  answer that differs): replaced silently, no echo; clears the undo history (old states would be wrong).
- `drawingSaved {localId, id, drawing}` — answer to `drawingCreated` (POST 201); `drawing` = the stored one.
- `drawingRejected {op: "create"|"change"|"delete", localId: str|null, id: str|null, message ≤ 300}` —
  the backend refused (400/409/…): the page reverts that step (create → overlay removed, change → old
  state, delete → restored) and drops it from the undo history; the host shows the message.
- `editDrawing {id, changes}` — object-panel edit (`changes` ⊆ `{style, name, text, locked, visible,
  timeframes, alert_id}`, style keys merged): applied by the page as an UNDOABLE user step and echoed as
  `drawingChanged` (the host persists only on that echo — one write path).
- `removeDrawing {id}` — object-panel delete: undoable, echoed as `drawingDeleted`.
- `selectDrawing {id|null}`, `jumpToDrawing {id}` (scrolls to the first point; selects it).
- `setTool {name: tool|null, style?: {color, width, dash, levels?}}` — `null` = cursor; `style` = the
  rail's current style for new drawings (default accent/1/solid). Tool names = the backend tools.
- `setMagnet {mode: "off"|"weak"|"strong"}` (snap to O/H/L/C while drawing/moving).
- `command {name}` — v1 names plus: `lockAll`, `unlockAll`, `showAll`, `cancel` (= Esc),
  `saveMeasure`. `lock` = toggle lock of the selected drawing; `hideAll`/`showAll` = a VIEW toggle (not
  persisted, not undoable); `lockAll`/`unlockAll`/`deleteSelected` = undoable (one step), echoed.
- `setIndicators {indicators: [{key, kind, pane: "overlay"|"sub", outputs: [key…], label ≤ 80,
  note: str|null}] ≤ 8 (≤ 3 with pane "sub"), t: [epoch ms…] (strictly increasing, ≤ 20 000),
  series: {outputKey: [number|null…] (len(t) each)}, volume?: bool (default true)}` — the backend
  `/indicators` answer with ms `t` (`indicators` = its `indicators` list; extra keys `params`,
  `warmup_bars` accepted, ignored). Replaces all indicator panes. The page NEVER computes indicators
  (Q8); it aligns values to its bars by `t` (a bar without a value has none). `indicators: []` removes all.
- `updateIndicators {t, series}` — merges values by `t` into the current set (older pages after
  `prependBars`, refreshed values after a bar close); keys must be outputs of the current set.

**Page → host**
- `drawingCreated {localId, drawing}` — `drawing` without `id` (and without the pass-through keys).
  `measure` drawings are temporary: sent only after `command saveMeasure` or Enter on a selected measure.
- `drawingChanged {id, changes, final: true}` — `changes` ⊆ `{points, style, name, text, locked,
  visible, timeframes, alert_id}` (only changed keys; `points` always complete). MR5 sends only
  `final: true` (drag end / edit / undo / redo); `final: false` is reserved.
- `drawingDeleted {id}`.
- `selectionChanged {id: str|null, localId: str|null, drawing: drawing|null}`.
- `undoState {canUndo, canRedo}` — after every change of the history (50 steps per chart; cleared by a
  replacing `loadDrawings`, `upsertDrawing` and a new symbol).
- `toolChanged {name: tool|null}` — after a drawing is finished (back to cursor), Esc, or a page
  shortcut (Alt+T `segment`, Alt+H `horizontalStraightLine`, Alt+F `fibonacciLine`, Alt+R `rect`).
- `shortcut {key: "ctrl+k"|"ctrl+t"|"ctrl+w"|"ctrl+tab"|"ctrl+shift+tab"}` — keys the WPF handles,
  forwarded while the WebView has focus. Page-handled keys: Ctrl+Z, Ctrl+Y / Ctrl+Shift+Z, Del, Esc,
  Enter (save measure / finish text), Alt+T/H/F/R.
- Page error codes add `drawing_unknown` (an id the page doesn't have — message only, no state change).
  Also `drawing_invalid` (a finished drawing outside the chart range was discarded).
- **Page re-creation** (pop-out / Reload): the host re-sends `init` → `loadBars` → `loadDrawings` (all
  saved drawings) → `setIndicators` → `setTool`/`setMagnet` → `command hideAll` if it was on. Drawings
  still waiting for their POST answer are not re-sent; an answer that arrives for a page that no longer
  knows the `localId` is sent as `upsertDrawing` (a `drawingSaved` with an unknown `localId` is ignored
  by the page anyway). Undo history does not survive a re-creation.

**Persistence flow (host).** `drawingCreated` → POST → `drawingSaved`/`drawingRejected`;
`drawingChanged` → PATCH (only `changes`) → (answer differs → `upsertDrawing`; error →
`drawingRejected`); `drawingDeleted` → DELETE (404 = already gone, no reject). Undo/redo/lock-all/
delete-selected arrive as ordinary created/changed/deleted messages (an undone create = `drawingDeleted`,
a redone delete = `drawingCreated` with a new `localId`). Chart settings PUT (type/indicators/magnet/
session) stays host-side.

**Revision 3 (MR4/MR5 review fixes H2 + M4, 2026-10-17; backward-compatible, `ready.revision: 3`).**
Written before the code; binding for the chart page and the WPF host.
- `drawingCreated {localId, clientId, drawing}` — `clientId` = a UUID v4 (lowercase, 36 chars) generated
  by the page per create; the host passes it to the backend as `client_id` (POST is idempotent on it,
  backend `client_id UNIQUE`), so a retried POST (offline save queue) never stores a duplicate. Bridge
  drawing objects coming from the backend may carry `client_id` (string|null) — accepted, ignored.
- **One create per drawing while its POST is pending.** Undo of a create whose `drawingSaved` has not
  arrived marks it "delete after save" (no message); a redo before the answer cancels that mark and
  REUSES the same `localId` + `clientId` — no second `drawingCreated`. When `drawingSaved` arrives the
  page sends exactly what is still needed: `drawingDeleted {id}` if the drawing is (still) undone/deleted,
  else one `drawingChanged` with the difference between the created body and the current state (if any).
  A `drawingSaved` for a `localId` the page has superseded is answered with `drawingDeleted {id}`
  (never two stored copies). Redo after the answer (create → saved → undo = DELETE → redo) is a new
  create with a new `localId` + `clientId`.
- **Rejections are reconciled by the host's authoritative state.** After `drawingRejected` with
  `op: "change"` or `"delete"` the host sends `upsertDrawing {drawing}` with the state it holds (the
  backend's / the state before the refused step). On `drawingRejected` the page drops that drawing's
  undo/redo entries and never reverts to an intermediate state by itself (change: it keeps showing the
  local state until the `upsertDrawing` arrives; delete: it restores the drawing as it was before the
  delete); `create`: removes the drawing (no upsert). `upsertDrawing` now drops only the undo/redo entries
  of THAT drawing (revision 2: the whole history) and replaces its state authoritatively (also a drawing
  the page had deleted).


**Revision 4 — MR8 chart markers (2026-10-17; additive, backward-compatible, `ready.revision: 4`).**
Written by MR8-WPF before the page code (the MR8 chart-page agent implements it; the host side is in
`Chart/ChartHost.Markers.cs`). The host sends it ONLY to pages reporting `revision ≥ 4` (an older page would
answer `bad_message`); until then the WPF says "markers loaded, but this chart page cannot draw them yet".
- Host → page `setMarkers {markers: [marker…]}` (≤ 2000; an empty list clears all markers). Replaces the whole
  marker set; markers are kept across `loadBars` / `prependBars` (they are time-based; the host sends an empty
  list right after a `loadBars` of another symbol and then the new set) and re-sent by the host after a page
  re-creation (after `loadBars` and the MR5 state).
- Marker: `{id: string ≤ 40 (unique per message), t: int epoch ms UTC, tEnd: int epoch ms | null (≥ t; a range),
  kind: "insider"|"fomc"|"release"|"fill"|"period_best"|"period_worst"|"drawdown", side: "buy"|"sell"|"short"|
  "cover"|null, label: string ≤ 200 (plain text — textContent only)}`. Unknown keys / kinds / sides, a bad `t`,
  `tEnd < t` → `bad_message` (whole message ignored).
- Drawing (page): point markers (`tEnd` null) at the bar containing `t` (the first bar with start ≥ `t` when `t`
  falls between bars; none when outside the loaded data): `insider`/`fill` buy|cover = small ▲ below the bar in
  `up`, sell|short = ▼ above the bar in `down`; `fomc`/`release` = a thin dashed vertical line in `caution` with
  a short tag ("FOMC", "R") at the top. Ranges: `period_best` = band in `upDim`, `period_worst` and `drawdown` =
  band in `downDim` (low opacity, behind candles), from the bar of `t` to the bar of `tEnd` (or the last bar when
  `tEnd` is null — "not recovered"). Hover / crosshair over a marker shows its `label` in the legend (never
  colour alone: glyph + text). Markers never change the price scale.
- No page → host message for markers.

### MR4 — symbol workspace

Deliverables: header (§7.4, every number with feed tooltip), key stats,
performance tiles, 52W bar, watchlists (lists = groups, collapse,
reorder, add/remove), tabs + session restore, Ctrl+K palette
(`/research/assets`), live status. Store v3. Tests: watchlist/session
CRUD + limits + validation, session round-trip.

### MR5 — drawing tools + indicators

Deliverables: tool rail (plan §4.1 list; KLineChart overlays + custom
`rect`, `measure`, `alertLine`), magnet/lock/hide/undo-redo (50 steps, page
side), object panel, persistence (store v4; drawing `{id, symbol, tool,
points: [{t, p}], style: {color: one of 8 palette keys, width 1–4, dash:
solid|dashed}, text|null, locked, visible, timeframes: "all"|[tf…],
alert_id|null, created_at, updated_at}`), split rescaling (corporate
actions after `basis_as_of`: prices ÷ split ratio, logged in
`drawing_rescales` + audit), export/import JSON (`{version: 1, symbol,
drawings}`, ≤ `MAX_IMPORT_BYTES`, schema-checked, ids re-assigned).
Indicators via the Strategy Platform DSL implementations (Q8): SMA = `sma`,
Bollinger = `sma ± k·ts_std` (sample std — note in the tooltip), RSI =
`rsi` (Wilder), EMA(n) = `ewm_mean(halflife = ln 2 / −ln(1 − 2/(n+1)))`,
MACD = EMA12 − EMA26, signal = EMA9 of MACD, session VWAP (Research-only,
§7.4), compare overlay (% from left edge). Max 8 indicator series per
request, 3 sub-panes. Tests: equality with the DSL evaluator on the same
series, rescale, import limits.

### MR6 — news + logos

Deliverables: news via `AlpacaProvider.news` (extension: `images` in the
news dict) + news stream in the hub, store v5 (7 days), cards (headline,
publisher, age, tickers, thumbnail), links in the default browser; logos
(new provider op `logo(symbol)`, verify free-plan access; 30-day cache,
negative cache `none` 7 days). Tests: sanitising, URL scheme filter,
retention, logo PNG check / 404.

### MR7 — alerts

Deliverables: rules `price_cross {level, direction: up|down}`,
`line_cross {drawing_id}`, `day_move {pct 0.1–50, direction: up|down|any}`,
`news {}`; evaluated in the hub on live trades / news (cooldown, de-dup:
one trigger per rule per cooldown), store v7 (built), WS `alert` → existing
HedgeFund toast helper, alert log (90 days), alert lines linked to
drawings; active alerts keep streams on (stated: "Alerts work only while
UFOS.ai runs."). A rule whose symbol is beyond the stream limit shows "not
live". Tests: crossing logic (gaps, both directions), cooldown, de-dup,
limit handling.

### MR8 — in-module integrations

Deliverables: sidebar Portfolio = HedgeFund paper account read-only
(`GET /research/portfolio` from the fund engine's state, no order
functions); Buy/Sell hand-over (WPF opens the existing manual paper-order
flow pre-filled with symbol + side; same confirmations/audit; never live);
Macro section (Newsroom macro data); markers `GET
/research/symbols/{s}/markers?from=&to=&kinds=insider,calendar,backtest:<run_id>`
→ `{items: [{t, kind, label, side|null}]}` from the Newsroom (Form 4,
calendar/FOMC) and Strategy Platform run cards + §11 attribution (periods,
drawdowns); "Use watchlist as universe" hand-over to the Strategy editor.
Tests: read-only guarantee (no write call reachable), marker mapping.

