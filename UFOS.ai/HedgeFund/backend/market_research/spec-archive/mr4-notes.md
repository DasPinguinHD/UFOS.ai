# Market Research SPEC — archive: mr4-notes

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

### MR4 backend notes (implemented 2026-10-17: symbol workspace — routes + exact JSON, binding for MR4-WPF)

> **Coordination:** MR4 uses store **v3** (`watchlists`, `watchlist_items`, `session`, `recent_symbols`);
> MR5 uses **v4** (drawings, chart settings). Both add their routes additively to `api.ROUTES`
> (MR4: `workspace_api.WORKSPACE_ROUTES`, mixin `WorkspaceRoutes`).

**General.** All routes under `/research/*` behind `api_auth` (header token), JSON as §4.1. Unknown query
parameters / body fields, wrong types → 400 `{error: "invalid input", errors: {field: msg}}`; a body that is
not a JSON object → 400; body > `MAX_WORKSPACE_BODY_BYTES` (32 768, serialized) → 400. Ids = 32-char
lowercase hex, server-assigned (`uuid4().hex`); a malformed or unknown id → 404 `{"error": "Watchlist not
found."}`. Names are untrusted user text: stored and returned exactly as given after trimming leading /
trailing whitespace (no HTML/SQL escaping on the way in — parameterised SQL; the WPF shows them as plain
text). **No audit rows** for watchlists, session, recents: they are UI preferences of the single local user
with no effect on orders, strategies or data (the `audit` table records actions with consequences, e.g.
MR5 drawing rescales, MR7 alerts) — default (user away 2026-10-17) — confirm.

**Watchlist object** (every key always present):
```json
{"id": "6f1c0b0e9a4d4c1f8b2e3d4c5b6a7980", "name": "Tech", "position": 0, "collapsed": false,
 "created_at": "2026-10-17T12:00:00+00:00", "updated_at": "2026-10-17T12:05:00+00:00",
 "items": [{"symbol": "AAPL", "position": 0, "added_at": "2026-10-17T12:01:00+00:00",
            "name": "Apple Inc. Common Stock", "asset_class": "equity", "kind": "stock", "exchange": "NASDAQ",
            "covered": true},
           {"symbol": "BTC-USD", "position": 1, "added_at": "2026-10-17T12:02:00+00:00",
            "name": "Bitcoin / US Dollar", "asset_class": "crypto", "kind": "crypto", "exchange": "CRYPTO",
            "covered": true}]}
```
`covered: false` = the symbol is no longer in Alpaca's asset list (then `name`, `kind`, `exchange` are
`null`; `asset_class` comes from the symbol syntax) — the row stays until the user removes it.

**Routes (watchlists = the groups of the panel; no list is seeded — empty state in the UI):**

| method + path | body | answer |
|---|---|---|
| `GET /research/watchlists` | — | 200 `{"lists": [<watchlist>…] (by position), "limits": {"max_lists": 20, "max_items": 200, "max_name_chars": 40}}` |
| `POST /research/watchlists` | `{"name": str}` | 201 `{"id": <new id>, "lists": […], "limits": {…}}` — appended at the end, `collapsed: false` |
| `PATCH /research/watchlists/{id}` | `{"name"?: str, "collapsed"?: bool, "position"?: int}` (≥ 1 key) | 200 `{"lists", "limits"}`; `position` 0…n−1 moves the list (the others shift) |
| `DELETE /research/watchlists/{id}` | — | 200 `{"lists", "limits"}` (positions compacted) |
| `PUT /research/watchlists/{id}/items` | `{"symbols": [str…]}` (0…200) | 200 `{"lists", "limits"}` — replaces the ordered items (reorder = PUT the new order) |
| `POST /research/watchlists/{id}/items` | `{"symbol": str, "position"?: int 0…n}` | 200 `{"lists", "limits"}` — default: appended |
| `DELETE /research/watchlists/{id}/items/{symbol}` | — | 200 `{"lists", "limits"}` |
| `GET /research/watchlists/{id}/quotes` | — | 200 quotes (below) |

Validation / errors:
- `name`: string; after trimming 1–40 characters; no control/format characters (Unicode categories C*,
  line/paragraph separators) → 400 `errors.name`. Names are unique case-insensitively → 409
  `{"error": "A watchlist named \"Tech\" already exists."}`. 21st list → 409 `{"error": "At most 20
  watchlists — delete one first."}`.
- Symbols: platform symbols (`core/instruments`, case-insensitive, stored upper-case, crypto `BASE-USD`;
  `BTC/USD` is accepted and stored `BTC-USD`) → 400 otherwise (`errors.symbol` / `errors["symbols[3]"]`).
  A symbol NEW to the list must be in Alpaca's asset list (`assets.require`, SQLite; an empty asset table is
  refreshed once — §13 MR2a) → 404 `{"error": <SymbolNotCovered text §4.1>, "kind": "SymbolNotCovered",
  "symbols": ["XYZ"]}`; symbols already in the list are accepted unchanged (a reorder never fails because a
  symbol was delisted meanwhile). Duplicates in `symbols` → 400 `{"errors": {"symbols": "AAPL is listed
  twice."}}`; > 200 → 400. `POST …/items`: already in the list → 409 `{"error": "AAPL is already in this
  watchlist."}`; list full → 409 `{"error": "A watchlist holds at most 200 symbols."}`.
  `DELETE …/items/{symbol}`: not in the list → 404 `{"error": "AAPL is not in this watchlist."}`.
- Asset list unavailable (empty table + refresh failing) / store unavailable → 503 `{error, kind}` (§4.1).

**Watchlist quotes** `GET /research/watchlists/{id}/quotes` (no query parameters):
```json
{"id": "6f1c…", "generated_at": "2026-10-17T14:00:05+00:00", "complete": true, "retry_after_seconds": null,
 "stream_limit": 30,
 "note": "Live quotes come only from the Alpaca streams while this watchlist is shown. Symbols beyond the 30-symbol stock stream limit are not live — no delayed or snapshot value is shown for them.",
 "items": [
  {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "asset_class": "equity", "kind": "stock", "covered": true,
   "live": true, "reason": null,
   "last": {"value": 187.12, "feed": "iex", "feed_label": "IEX · real time", "as_of": "2026-10-17T14:00:01+00:00", "reason": null},
   "prev_close": {"value": 185.0, "feed": "sip_daily", "feed_label": "SIP · daily bars", "as_of": "2026-10-16T20:00:00+00:00", "reason": null},
   "change": {"value": 2.12, "feed": "computed", "feed_label": "IEX · real time − SIP · daily bars", "as_of": "…", "reason": null},
   "change_pct": {"value": 1.146, "feed": "computed", "feed_label": "(IEX · real time − SIP · daily bars) / previous close", "as_of": "…", "reason": null}},
  {"symbol": "ZZZ", "…": "…", "live": false, "reason": "Stream limit: 30 symbols — ZZZ is not live.",
   "last": {"value": null, "feed": "iex", "feed_label": "IEX · real time", "as_of": null, "reason": "Stream limit: 30 symbols — ZZZ is not live."},
   "prev_close": {"value": null, "…": "same reason"}, "change": {"value": null, "…": "same reason"}, "change_pct": {"value": null, "…": "same reason"}}]}
```
- `live`/`reason` = the hub's per-symbol state (= the `symbol_status` of §4.3; e.g. "Stocks stream is off."
  while no view shows the list, "Stocks stream: waiting for connection (attempt 2).").
- `last` = the latest trade the hub received on the stream since the symbol went live (in memory only; never a
  REST snapshot). Live but no trade yet → `null`, reason "No trade on the live stream yet — the price appears
  with the next trade.". Not live → `null` with the live reason.
- Beyond the stock stream limit (Q10): ALL four values `null` with the limit text — no prev close, no
  snapshot, nothing fetched for that symbol.
- `prev_close` = the header's definition (§7.4: SIP daily close of the session before the reference session,
  split-adjusted; crypto: previous UTC day close) — from the hedgefund.db bar cache through the gate
  (research priority), kept in memory for `PREV_CLOSE_CACHE_SECONDS` (3600). Lookups stop after
  `QUOTE_LOOKUP_BUDGET_SECONDS` (15) per request: the rest get `null` with reason "Previous close is still
  loading — ask again." and the answer has `complete: false, retry_after_seconds: 5` (the WPF asks again
  after that while the list is shown). A source failure → `null` with its reason (that row only).
- The WPF keeps `last`/`change`/`change_pct` current from the WebSocket `trade` messages
  (change = price − `prev_close.value`); it re-requests quotes after a reconnect, a list change, and while
  `complete` is false. Uncovered symbols: `live: false`, reason = the SymbolNotCovered text, all values null.

**Live watchlists on `/research/ws` (demand registration, additive to §4.3):** new topic
**`watchlist:<id>`** (32-hex id). Subscribe it for every watchlist that is shown and expanded (unsubscribe
when collapsed/hidden). It counts as ONE subscription (64-cap) and stands for `trades:<SYM>` of every item
of the list in list order — `trade` and `symbol_status` messages for those symbols arrive exactly as for
`trades:` topics. The expansion follows every watchlist change at once (no re-subscribe needed). Priority
for the 30-symbol stock limit = topic order, so the WPF subscribes (§4.3.1) tabs/charts first, then the
`watchlist:` topics in panel order, then `alerts`, `feeds`. An unknown/deleted id expands to nothing.

**Session restore** `GET /research/session` / `PUT /research/session`:
```json
{"schema": 1,
 "tabs": [{"symbol": "AAPL", "tf": "1D", "type": "candles", "session": "rth"},
          {"symbol": "BTC-USD", "tf": "1H", "type": "line", "session": "utc"}],
 "active": 0,
 "sidebar": {"collapsed": false, "section": "symbol"},
 "saved": true, "updated_at": "2026-10-17T12:00:00+00:00", "note": null, "limits": {"max_tabs": 12}}
```
- Nothing saved yet → `tabs: [], active: null, sidebar: {"collapsed": false, "section": "overview"},
  saved: false, updated_at: null`. A stored session that no longer validates (other version / edited by
  hand) is answered like "nothing saved" with `note: "The saved session could not be read and was
  ignored."` (logged; the next PUT replaces it).
- PUT body = exactly `{"tabs", "active", "sidebar"}` (all required); answer 200 = the GET shape.
  `tabs` 0…12, each exactly `{symbol, tf, type, session}`: `tf` ∈ `1m 5m 15m 1H 4H 1D 1W`, `type` ∈
  `candles line area`, `session` = `rth`/`eth` for stocks/ETFs, `utc` for crypto (the value `/bars` echoes);
  the same symbol may be open in several tabs. `active`: `null` iff `tabs` is empty, else 0…len−1.
  `sidebar` exactly `{collapsed: bool, section: overview|symbol|alerts|portfolio|macro}` (MR8 sections
  accepted already). Symbols are checked by syntax only (restore must work without the network; an
  uncovered symbol shows the chart's 404 text). One row (`session.id = 1`), replaced on every PUT.

**Ctrl+K palette** — recent symbols + ranked search:
- `POST /research/recent {"symbol": str}` → 200 `{"recent": ["AAPL", "BTC-USD", …]}` (newest first, ≤
  `MAX_RECENT_SYMBOLS` = 20; the WPF posts when a symbol is opened from the palette/watchlist/tab bar; must
  be in the asset list → 404 SymbolNotCovered). `DELETE /research/recent` → 200 `{"recent": []}`.
- `GET /research/palette?q=&limit=` (`q` optional 0–32 chars, same charset as `/research/assets`; `limit`
  1–50, default 20):
```json
{"q": "aa", "recent": ["AAPL", "BTC-USD"],
 "items": [{"symbol": "AAPL", "name": "Apple Inc. Common Stock", "asset_class": "equity", "kind": "stock",
            "exchange": "NASDAQ", "tradable": true, "match": "symbol_prefix", "recent": true,
            "watchlists": ["6f1c0b0e9a4d4c1f8b2e3d4c5b6a7980"]}],
 "refreshed_at": "2026-10-17T03:00:00+00:00", "source": "alpaca"}
```
  `q` empty/absent → the recent symbols (newest first, `match: "recent"`); otherwise `match` ∈ `exact`,
  `symbol_prefix`, `name_word`, `name_substring` (the `/research/assets` order) and within each group recent
  symbols first (newest first), then shorter symbol, then alphabetical. `watchlists` = ids of the lists
  that contain the symbol (for a "★ in Tech" hint).

**Header / key stats / performance / 52W (MR4 UI):** served by MR2a's `/snapshot` (header numbers, market
chip) + `/stats` (open, VWAP, avg volume, exchange, performance tiles, 52W) + `/market/status`. MR4 adds
one additive key: `/stats` → `range_52w.position_pct` = Num `computed` ((last − low) / (high − low) · 100, not
clamped — the last trade can lie outside a range built from completed daily bars; `null` with reason when
an input is missing or high = low), label "Last trade within the 52-week range (completed daily bars)".

### MR4 WPF notes (implemented 2026-10-17: symbol workspace — header, key stats, watchlists, tabs + session, Ctrl+K palette)

- **Files.** New `MarketResearch/ResearchView.Workspace.cs` (partial: header, key stats, performance tiles, 52W,
  tabs + session restore/save, recent symbols, Ctrl+K palette, formatting), `MarketResearch/ResearchView.Watchlists.cs`
  (partial: watchlists, quotes, live rows, ☆ menu), `MarketResearch/RangeBar.cs` (low–high bar with a marker,
  OnRender, token brushes). Changed: `ResearchView.xaml` (new sidebar/tab bar/header/stats/palette layout; MR3 chart
  card unchanged), `ResearchView.xaml.cs` (MR3 exact-ticker box removed; Activate/CloseTab/toolbar save the session;
  socket handlers feed the watchlist rows; budget line), `ResearchClient.cs` (MR4 routes + DTOs exactly as "MR4 backend
  notes": `WatchlistsResponse/WatchlistDto/WatchlistItemDto/WatchlistLimits`, `WatchlistQuotes/WatchlistQuoteItem`,
  `SessionState/SessionTab/SessionSidebar/SessionLimits` (+ `note`), `RecentSymbols`, `PaletteResult/PaletteItem`,
  `Range52Weeks.position_pct`; workspace routes show 409 `error` and 400 field messages verbatim). The SPEC §3 layout
  names `WatchlistPanel`/`CommandPalette` became partial files of `ResearchView` (no second UserControl: the tokens
  live in the view's resources — deviation, default (user away 2026-10-17) — confirm). Checked statically only
  (no dotnet on the VM: brace/handler/element-name scripts + manual review).
- **Header (§7.4).** Logo placeholder = ticker initials (until MR6); `SYM · EXCHANGE`, kind chip (STOCK/ETF/CRYPTO),
  market chip in LOCAL time ("● OPEN · CLOSES 22:00", "PRE-MARKET · OPENS …", "AFTER HOURS · AT CLOSE",
  "CLOSED · AT CLOSE · OPENS …", "HOLIDAY · AT CLOSE", crypto "● OPEN · 24/7"), tooltip with local AND New York time
  (TimeZoneInfo `America/New_York`, Windows id as second lookup) + the PC's zone name; live chip (MR3). Last price
  (Consolas 22), "▲/▼ ±change ±%" vs previous close (computed client-side = §7.4 definition, from the live trade or
  the snapshot `last`), suffix "today / pre-market / after hours · day values at close / at close / today (UTC day)",
  day-range bar (marker = last), volume + its feed label in caps ("SIP · 15 MIN DELAYED"), prev close + session date.
  Every number: tooltip = what · feed label · as-of (local) or "not available — <reason>"; reasons of missing header
  values are also shown as a line under the price. Not live (socket down / symbol not live / status unknown) → price,
  change and markers greyed + "· not live" + the reason in the tooltip (never shown as current). Snapshot every 60 s
  while shown (with the market status), stats every 5 min; a failed refresh clears the values and shows the reason.
- **Key stats.** Open, VWAP, Avg volume (30d), Exchange (Num tooltips), performance tiles 1W 1M 3M 6M YTD 1Y
  (▲/▼ + signed %, green/red tint from the tokens with alpha, "—" + reason; tooltip: base date, feed, "Split-adjusted
  closes; dividends not included." + backend `notes`), 52-week bar with marker (last price) + low/high/date span,
  `position_pct` in the tooltip.
- **Watchlists.** GET on first show (retried ≥ 30 s apart once the backend answers, Retry button); empty state; per
  list header "▾ NAME · n" (click = collapse/expand, PATCH `collapsed`, applied at once, server answer wins), "+"
  (palette in "add to list" mode → POST …/items), "✎" edit mode (rename = PATCH name on Enter, ▲/▼ list = PATCH
  `position`, "Delete…" with an inline confirmation = DELETE, per row ▲ ▼ ×). Keyboard on a focused row: Enter opens,
  Delete removes (DELETE …/items/{symbol}), Alt+↑/↓ moves (PUT …/items with the new order); focus is kept on the moved
  row / next row / list header. No drag-and-drop (up/down buttons instead). Sidebar "+" = new list (POST, name 1–40,
  control characters refused client-side too). ☆/★ in the tab bar: menu with one toggle per list (☑ = contains the
  open symbol) + "new list with this symbol". Changes run one after another; every answer `{lists, limits}` replaces
  the local copy; failures show the backend text (409 verbatim) and re-load unless it was a 400/409.
- **Live rows.** Row = ▲/▼ · ticker · last price · signed % vs previous close; selected row (= active tab) tinted with
  AccentBlue. Values from GET …/quotes (after load/change, expand, every re-sync, again after `retry_after_seconds`
  while `complete: false`) and the socket (`trade` → price/%, `symbol_status` → live/reason; not live → no price, Q10).
  A row with `live: false` (while connected) or `covered: false` shows its reason on a second line (e.g. "Stream limit:
  30 symbols — X is not live."); while the socket is down the rows are greyed and one note above the lists says why.
  Topics (§4.3.1): `candle:` + `trades:` of the active tab, then `watchlist:<id>` for every EXPANDED list in panel
  order (collapsed lists are not streamed — stated in the header tooltip), then `feeds`.
- **Tabs + session.** Tabs opened from the palette / a watchlist (same symbol → its tab), "+" opens the palette, × closes,
  ≤ 12 (message beyond). GET /research/session on first show → tabs (symbol, tf, type, session; invalid entries skipped
  with a message), active index, sidebar collapsed + section (overview/symbol/alerts; MR8 sections ignored); asset class
  of a restored tab from the symbol syntax (`BASE-USD` = crypto, core/instruments), name/exchange/kind from the snapshot.
  A restore that fails is retried while the user has not opened anything; nothing is PUT before the user changes
  something. PUT 1 s after every change (tab open/close/switch, timeframe, chart type, RTH/ETH, sidebar toggle,
  section), a pending save is flushed when the Hedge Fund window closes; a failed save → "Open tabs not saved: <reason>".
  `note` (unreadable stored session) is shown. Recent: POST /research/recent when a symbol is activated by the user
  (not on restore); failures only logged.
- **Ctrl+K palette.** Overlay over the whole view (backdrop = WindowBackground at 78 %, click closes). Keyboard-only:
  the query box keeps the focus, ↑/↓/PgUp/PgDn select, Enter opens (or adds), Esc closes and returns the focus, Tab
  cycles inside the overlay; Enter before the answer arrived runs the search at once and opens the first result.
  GET /research/palette (debounced 180 ms, stale answers dropped by a generation counter); empty query = RECENT;
  rows: ticker · name (plain text) · tags OPEN / ★ LIST / RECENT / KIND · EXCHANGE; the selected row is announced via a
  live region; client-side input check (charset of /research/assets, ≤ 32); "Not available from Alpaca (the
  configured crypto source)" for an unmatched `XXX-USD`. Opened by Ctrl+K (PreviewKeyDown on the view), "⌕ Search
  Ctrl+K" in the sidebar and "+" in the tab bar. Ctrl+K while the focus is INSIDE the chart (WebView2) is not seen by
  WPF (the page forwards no keys) — click outside the chart first (verify on the PC).
- **Status strip.** MR1/MR3 strip kept (dots, LIVE/WAITING · attempt n, reasons, demand note "only while visible");
  new line "Waiting for the Alpaca rate limit (n s) — shared with trading, which goes first." from
  `budget.waiting_seconds` (§8). Sidebar collapse (◧ in the tab bar) hides it; the header's live chip stays.
- **Accessibility.** AutomationProperties names on every control incl. built rows/tiles/chips (state + numbers in
  words: "up 1.15 percent", "not live: …", "selected"); AcceleratorKey "Ctrl+K" on search/+; live regions for the
  palette selection and the watchlist/workspace messages; direction always by sign + arrow, never colour alone.
- **Colours.** Only the §2.3 tokens and the colours the view already used; tints = token colour with alpha (`Tint`).
- **Verify on the PC:** first build; Ctrl+K focus behaviour (rail button / pop-out / inside the chart); TextBox inside
  the ☆ Popup in the layered window; palette overlay above the WebView2 composition control; ET conversion
  (`America/New_York`) on Windows.

