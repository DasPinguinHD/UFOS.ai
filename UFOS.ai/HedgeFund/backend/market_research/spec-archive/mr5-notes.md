# Market Research SPEC — archive: mr5-notes

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

### MR5 backend notes (implemented 2026-10-17: drawings, chart settings, indicators, split rule)

Written FIRST (before the code) so the chart page / WPF agent of MR5 can start; synced with the code
after implementation (2026-10-17). Backend only — the tool rail, overlays, object panel, undo/redo and the WPF
wiring come from the MR5 chart/WPF agent, who reads this block.

**Files.** `drawings.py` (validation, CRUD, export/import, split rule, supervised task
**`drawing_split_check`**), `indicators.py` (indicator specs, computation on the DSL evaluator, session
VWAP, compare overlay), `chart_settings.py`, `drawings_api.py` (`DrawingRoutes` mixin of `api.ResearchApi`,
`DRAWING_ROUTES` appended to `api.ROUTES`). Store **v4** (`drawings`, `drawing_rescales`, `chart_settings`).
`sources.AlpacaSources.splits()` (Alpaca corporate actions via the existing provider — no new
strategy_platform code). Errors as §4.1 (400 `{error: "invalid input", errors: {field: msg}}`, 404
`{error}`, 409 `{error}`, 503 `{error, kind}`, 500 generic). All times UTC ISO with seconds; drawing points
keep ISO `t` — `ChartHost.cs` converts to/from epoch ms (§0 Time).

**Drawing object (every key always present).**
```json
{"id": "0f1e…32 hex", "symbol": "AAPL", "tool": "segment",
 "points": [{"t": "2026-10-15T14:30:00+00:00", "p": 187.12}, {"t": "2026-10-16T15:00:00+00:00", "p": 190.5}],
 "style": {"color": "accent", "width": 2, "dash": "solid", "levels": null},
 "name": null, "text": null, "locked": false, "visible": true, "timeframes": "all", "alert_id": null,
 "price_basis": {"as_of": "2026-10-15", "factor": 1.0, "rescales": 0},
 "created_at": "2026-10-15T14:31:02+00:00", "updated_at": "2026-10-15T14:31:02+00:00"}
```
- `tool` and point counts: `segment` 2, `rayLine` 2, `straightLine` 2, `horizontalStraightLine` 1,
  `horizontalRayLine` 1, `verticalStraightLine` 1, `priceLine` 1, `parallelStraightLine` 3,
  `priceChannelLine` 3, `fibonacciLine` 2, `rect` 2 (opposite corners), `measure` 2, `simpleAnnotation` 1
  (text note — `text` required), `simpleTag` 1, `arrowUp` 1, `arrowDown` 1, `brush` 2–500 (freehand),
  `alertLine` 1 (custom horizontal line + bell). Exactly that many points (brush: 2…`MAX_BRUSH_POINTS`);
  KLineChart overlay names are used where they exist; `rect`, `measure`, `arrowUp`, `arrowDown`,
  `alertLine` are custom overlays of the page.
- point `{t, p}`: `t` ISO-8601 with offset (naive = UTC), 1990-01-01 ≤ t < 2100-01-01, stored/returned as
  UTC ISO seconds; `p` finite, 0 < p ≤ 1e9 (also for `verticalStraightLine`: the price of the anchor).
  Points need NOT be in time order (a line may be drawn right-to-left) and may lie right of the last bar.
- `style`: `color` ∈ `accent`, `up`, `down`, `caution`, `text`, `muted`, `blue`, `violet` (8 palette keys;
  the first six map to the bridge `init.theme` colours, `blue`/`violet` are fixed page colours);
  `width` int 1–4; `dash` `solid`|`dashed`; `levels` `null` (= 0, 23.6, 38.2, 50, 61.8, 78.6, 100) or a
  list of 1–12 distinct numbers in [−100, 400] — `fibonacciLine` only (others: must be null). POST may
  give a partial style (missing keys = defaults `accent`, 1, `solid`, null); PATCH merges keys.
- `name` (object panel "rename"): null or 1–40 chars plain text (no control characters); `text`: null or
  1–280 chars plain text (newline/tab allowed, other control characters → 400) — rendered as plain text
  only (escape in the page / TextBlock in WPF). `simpleAnnotation` needs a non-empty `text`.
- `timeframes`: `"all"` or a non-empty list of distinct tfs from `1m 5m 15m 1H 4H 1D 1W` (shown only there).
- `alert_id` (read-only since MR7, follow-up 2026-10-17): null, or the id of the alert linked to this
  `alertLine` — written ONLY by the alert transaction (`store._link_line` / `_unlink_line`); in responses
  only. POST/PATCH with an `alert_id` key (any value, also null) → 400 `{errors: {alert_id: "alert_id is
  managed by alerts …"}}`; import entries: stripped (`alert_id_ignored: true` in the import answer).
- `client_id` (review M3): null or the UUID the client sent on POST/import (idempotency key, below).
- `price_basis` (read-only, server-maintained, split rule below): `as_of` = the New York date whose
  split-adjusted price basis the points are in (equities; set to today's NY date on create and on every
  PATCH that changes `points`), `null` for crypto (no splits); `factor` = product of the split factors
  applied so far; `rescales` = how many were applied.
- Size: one drawing's JSON (compact) ≤ `MAX_DRAWING_JSON_BYTES` (16 384) — a brush of ~350 points; at most
  `MAX_DRAWINGS_PER_SYMBOL` (500) per symbol (→ 409 "At most 500 drawings per symbol — delete some first.").

**Routes** (behind `api_auth`; symbols via `core/instruments` — NOT checked against the asset list:
drawings are local):
- `GET /research/drawings?symbol=AAPL` → `{"symbol", "drawings": [drawing…] (created order), "count",
  "limit": 500, "split_check": {"state": "ok"|"failed"|"pending"|"not_applicable", "checked_at": iso|null,
  "reason": str|null}}` — `not_applicable` for crypto; `failed` keeps the reason of the last failed
  corporate-actions check (e.g. missing keys) — the page/WPF shows "Split check failed: <reason> — drawings
  may be misplaced after a split." Prices are never changed by a failed check.
- `POST /research/drawings` body `{symbol, tool, points, style?, name?, text?, locked?, visible?,
  timeframes?, client_id?}` → **201** drawing (server id, uuid4 hex). Unknown/duplicate body fields → 400.
- `PATCH /research/drawings/{id}` body ⊆ `{points, style, name, text, locked, visible, timeframes}`
  (non-empty; `alert_id` → 400 "alert_id is managed by alerts"; `symbol`/`tool`/`id`/`price_basis`/times → 400) → 200 drawing. A **locked**
  drawing refuses `points`/`style`/`text`/`name` changes with 409 "The drawing is locked — unlock it
  first." unless the same PATCH sets `locked: false`. Unknown id → 404 "Drawing not found.".
- `DELETE /research/drawings/{id}` → 200 `{"deleted": id}`; locked → 409 (same text); unknown → 404.
- `DELETE /research/drawings?symbol=AAPL&confirm=true` → 200 `{"symbol", "deleted": n}` (also locked ones —
  "delete all" is confirmed in the UI); `confirm` must be exactly `true`. **fix1023 (2026-10-23):** `confirm` is no
  longer required (accepted and ignored) — the app's one-step dialog is the confirmation (strategy_platform/SPEC.md
  "Fix batch fix1023").
- `GET /research/drawings/export?symbol=AAPL` → 200 the file object
  `{"version": 1, "symbol": "AAPL", "exported_at": iso, "drawings": [{tool, points, style, name, text,
  locked, visible, timeframes, price_basis: {as_of}}]}` (no ids, no alert links, no times). The WPF saves
  it as `<SYMBOL>-drawings.json`. Larger than `MAX_IMPORT_BYTES` → 409 (the export would not import again).
- `POST /research/drawings/import?symbol=AAPL&mode=append|replace` body = the file object → 200
  `{"symbol", "imported": n, "replaced": m, "drawings": [new drawing…]}`. Checks: compact body ≤
  `MAX_IMPORT_BYTES` (262 144), `version == 1`, `symbol` equal to the query symbol ("This file holds
  drawings for MSFT, not AAPL."), no keys beyond the exported ones (`exported_at` optional; per drawing
  `tool`, `points`, `price_basis` required, the rest optional with the POST defaults), every drawing
  validated like POST (errors keyed `drawings[3].points`), total ≤ 500 after the import. All or nothing
  (one transaction). Ids re-assigned, `alert_id` dropped (alerts are not exported), `created_at` = now,
  `price_basis.as_of` taken from the file (so a split between export and import is rescaled by the next
  split check — the import triggers one at once). `mode` default `append`; `replace` deletes the symbol's
  drawings first (same transaction).
- `GET /research/chart-settings?symbol=AAPL` → `{"symbol", "saved": bool, "settings": {"type":
  "candles"|"line"|"area", "indicators": [spec…], "magnet": "off"|"weak"|"strong", "session":
  "rth"|"eth"|null}, "updated_at": iso|null}` (defaults when not saved: candles, [], off, rth/null for
  crypto). `PUT /research/chart-settings?symbol=AAPL` body = exactly the four settings keys → 200 the same
  shape. `indicators` = indicator spec strings (grammar below, ≤ 8, ≤ 3 sub-panes, normalised keys);
  crypto `session` must be null, equities `rth`/`eth`.

**Indicators — `GET /research/symbols/{s}/indicators?tf=&session=&before=&limit=&ind=`.** `tf`, `session`,
`before`, `limit` exactly as `/bars` (same validation, same window → the same bars); `ind` (required, ≤ 400
chars) = comma-separated specs:

| spec (defaults) | params | pane | output keys | definition (Q8: the Strategy Platform DSL evaluator itself) |
|---|---|---|---|---|
| `sma:<n>[:<src>]` | n 1–500 | overlay | `sma:20:close` | DSL `sma(src, n)` |
| `ema:<n>[:<src>]` | n 2–500 | overlay | `ema:20:close` | DSL `ewm_mean(src, halflife = ln 2 / −ln(1 − 2/(n+1)))` (α = 2/(n+1)) |
| `bb[:<n>[:<k>[:<src>]]]` (20, 2, close) | n 2–500, k 0.1–10 | overlay | `bb:20:2:close:mid` / `:upper` / `:lower` | mid DSL `sma(src, n)`, bands `sma ± k·ts_std(src, n)` (sample std, n − 1) |
| `rsi[:<n>]` (14) | n 2–100 | sub | `rsi:14` | DSL `rsi(n)` (Wilder, close) |
| `macd[:<fast>:<slow>:<signal>]` (12, 26, 9) | 2–200, fast < slow | sub | `macd:12:26:9:macd` / `:signal` / `:hist` | macd = EMA(fast) − EMA(slow) (DSL `ewm_mean` as EMA), signal = `ewm_mean(macd, hl(signal))`, hist = macd − signal |
| `vwap` | — | overlay | `vwap` | Research-only (no DSL operator): session VWAP Σ(vwap·v)/Σv of the chart's bars from the session start (rth 09:30 / eth 04:00 NY; crypto 00:00 UTC), reset every session; **intraday tf only** (1D/1W → 400) |
| `cmp:<SYMBOL>` | same asset class | overlay | `cmp:SPY:pct`, `cmp:SPY:price` | Research-only: pct = (c_other(t) / c_other(base) − 1)·100, price = c_self(base) · c_other(t) / c_other(base); base = the first bar of this response where the other symbol has a bar |

`src` ∈ `close` (default), `open`, `high`, `low`, `hl2`, `hlc3`, `ohlc4`. Specs are normalised (`SMA:20` →
`sma:20:close`, `bb` → `bb:20:2:close`, `macd` → `macd:12:26:9`, `cmp:spy` → `cmp:SPY`, k printed with
`%g`); a duplicate after normalisation → 400. Limits: ≤ `MAX_INDICATORS` 8 specs per request, ≤
`MAX_SUB_PANES` 3 sub-pane specs (rsi, macd; the chart's own volume pane is not counted), ≤ `MAX_COMPARE`
2 `cmp`, compare symbol ≠ the chart symbol.

Response:
```json
{"symbol": "AAPL", "tf": "1D", "session": "rth",
 "t": ["2024-10-15T04:00:00+00:00", "…"],
 "series": {"sma:20:close": [null, "…", 187.2], "rsi:14": ["…"]},
 "indicators": [{"key": "sma:20:close", "kind": "sma", "pane": "overlay", "outputs": ["sma:20:close"],
                 "params": {"n": 20, "source": "close"}, "label": "SMA 20 (close)", "warmup_bars": 19,
                 "note": null}],
 "has_more_left": true, "sip_through": null, "tz": "America/New_York",
 "warmup_loaded": 80, "last_final": false}
```
- `t` = exactly the bars `/bars` returns for the same `tf/session/before/limit` (oldest first); every series
  has `len(t)` values, `null` where there is no value. The page aligns by `t` (a newer live bar that the
  indicator response does not have simply has no indicator value until the next fetch).
- **Warm-up:** before computing, up to `W` older bars are loaded with a second `/bars`-equivalent call
  (`before` = the window's first bar; W = max over the specs of: sma/bb n − 1, ema 4·n, rsi 10·n, macd
  4·slow + 4·signal, vwap one session of buckets, capped at the tf's `MAX_BARS_PER_REQUEST`) and cut off again
  (`warmup_loaded` = how many). Values whose look-back is not complete inside the loaded history are
  `null`: sma/bb first n − 1 loaded bars, ema first n − 1, rsi first n, macd first slow − 1 (signal/hist
  slow + signal − 2), vwap: a session whose first bar is not loaded. No look-ahead: every value at bar i
  uses bars ≤ i only (the DSL evaluator is causal). Recursive smoothers (ema, rsi, macd) start at the first
  loaded bar, so a value can differ in the far decimals from a longer history (seed weight < 0.05 % when the full
  warm-up was loaded; more when history runs out — noted per indicator).
- `last_final` = whether the last bar of `t` is final; the values of a non-final (live) bar are provisional.
  The page does NOT compute indicators itself (Q8 — one implementation): it re-fetches the indicator route
  with `limit` covering the visible range when a live candle closes (`final: true`) — at most once per bar
  close and topic — and after every `loadBars`/`prependBars` (paging: `before` = the oldest loaded bar, same
  as `/bars`).
- Notes per indicator (`note`, plain text): bb "Bands use the sample standard deviation (n − 1), as the
  Strategy Platform's ts_std."; ema/macd/rsi "Seeded from the loaded history (warm-up …)."; vwap "Session
  VWAP of the chart's bars (rth from 09:30, eth from 04:00 ET; crypto from 00:00 UTC). A bar without a
  VWAP ends the line for the rest of its session — no stand-in value."; cmp "% change from the first bar of
  this window; the page may rebase to its visible left edge: (1 + p(t)/100) / (1 + p(t₀)/100) − 1.".
- Errors: as `/bars`; plus `ind` grammar/limits → 400 `{error: "invalid input", errors: {ind: msg}}`;
  `vwap` on 1D/1W → 400; compare symbol not in Alpaca's list → 404 `SymbolNotCovered` (its message);
  compare of another asset class → 400. A source failure for the compare symbol fails the request (503) —
  no partial response.

**Split rule** (plan §4.2). The chart's equity bars are split-adjusted (Alpaca `adjustment=all`), so after a
split the stored drawing prices must be rescaled, or a line drawn at 400 before a 4:1 split would float at
4× the price. Supervised task **`drawing_split_check`** ("Drawing split check (daily)"): at start (as soon
as the store is open) and every 24 h, plus at once after an import, for every equity symbol with drawings:
Alpaca corporate actions (`forward_split`, `reverse_split`) via the existing provider
(`AlpacaProvider.corporate_actions` through `sources.AlpacaSources.splits`, Research pool,
`http_gate.research_priority()`), ex_date in (oldest `price_basis.as_of`, today]. For each drawing, every
split with `ex_date > as_of` not yet applied to it (in ex-date order): factor = `new_rate / old_rate`
(4:1 forward → 4; 1:10 reverse → 0.1), every `p` ÷ factor, `as_of` = ex_date, `factor` ×= f,
`rescales` += 1 — one transaction per drawing with a `drawing_rescales` row (`drawing_id, action_key, ts,
factor, ex_date, detail_json {kind, old_rate, new_rate, before, after}`) and an audit row (`system`,
`drawing_rescaled`). **Idempotent:** `action_key` = `<kind>:<ex_date>:<old_rate>:<new_rate>` is UNIQUE per
drawing, and `as_of` moves past the ex-date, so a repeated check (or a crash between drawings) never
applies a split twice. A split without usable rates is skipped, logged, audited (`split_unusable`) and
named in `split_check.reason` — prices are not guessed. Failure (keys missing, Alpaca down) → logged,
`split_check {state: failed, reason}`, retried after `SPLIT_CHECK_RETRY_SECONDS` (1 h); never a Research
crash (`UserFacingError`s are states, like `asset_refresh`). Dividend adjustments are NOT rescaled
(small, continuous; plan §4.2 names splits only). MR7 note: alert levels linked to an `alertLine` must
follow the drawing's rescale (MR7 reads the drawing's current price).

**Implementation (MR4 backend, 2026-10-17).**
- Files: `watchlists.py` (validation, SQL on store v3, one write lock + sequence numbers, `Workspace` = the
  in-memory `{list id: [symbols]}` for the hub), `session.py` (session, recents, palette), `workspace_api.py`
  (`WorkspaceRoutes` mixin of `api.ResearchApi`, `WORKSPACE_ROUTES` appended to `api.ROUTES`),
  `stats/quote.py` (prev close + quote rows, `PrevCloseCache`); changed additively: `store.py` (migration v3),
  `limits.py` (MR4 block), `api.py`, `__init__.py` (`ResearchService.workspace`, `hub.watchlist_symbols =
  workspace.symbols_for`, `prev_close_cache`), `hub/demand.py` (topic `watchlist:<id>`, `Topic.list_id`,
  `Subscriber.expanded` / `wants()`; `symbols()` expands watchlist topics at their position),
  `hub/hub.py` (`_expand_watchlists()` at every recompute, `_subs_with` via `wants()`, `last_trades` — latest
  stream trade per streaming symbol, never older than the stored one, cleared when the symbol stops
  streaming / the connection drops / the task ends — and `quote_state(symbol)`), `stats/context.py`
  (`equity_reference_days(now)` factored out, same logic), `stats/keystats.py` (`range_52w.position_pct`).
  No `strategy_platform` code changed (§12: nothing new).
- Hub expansion: the event loop never touches SQLite — `Workspace.symbols_for` answers from memory; before the
  first load (backend restart, WPF re-subscribes before any REST call) it starts ONE background load on the
  Research pool and the hub re-expands when it lands (store unavailable → retried at a later expansion, not
  before `WORKSPACE_RELOAD_SECONDS`). Every watchlist route applies its answer (newest sequence wins) and
  calls `hub.refresh()` only if the symbols changed.
- Status codes chosen: state conflicts (21st list, duplicate name, symbol already in list, full list) → 409;
  body limits (> 200 symbols in a PUT, name length, body size) → 400; unknown/malformed id → 404.
- Quote `live` for a symbol whose stream is up but that no shown view subscribes → `false` with "Not live —
  live quotes run only while this watchlist is shown in a Research view." (`hub.NOT_SUBSCRIBED_MESSAGE`).
- Not done in MR4 (still open from MR3b): the bar session flag `s` (extended-hours shading) — needs the
  calendar per bar in `/bars` and in live candles; candidate for a later stage.
- Tests: `tests/test_mr_workspace.py` (16: CRUD, list reorder + compaction, 20-list / 200-item / 40-char
  limits, 409s, name validation incl. control/format/bidi characters, injection-looking names stored and
  returned verbatim, body/param/id validation, PUT replace/reorder keeps `added_at`, delisted symbol kept on
  reorder, SymbolNotCovered 404 with `symbols`, no audit rows, session default + round-trip + 17 invalid
  bodies + unreadable stored session, recents cap/order, palette ranking with recents/watchlists, routes +
  store v3), `tests/test_mr_workspace_live.py` (12: topic parsing/expansion/`wants`, live hub with a fake
  Alpaca stream: tab + 31-symbol list → 30 subscribed, per-symbol limit messages, trades for list symbols,
  quotes (stream last, prev close, change, no-trade reason, limit rows all null), list change re-expands
  without re-subscribe, delete expands to nothing, nothing fetched beyond the limit, not-subscribed and
  hub-stopped reasons, uncovered item, lazy load after restart, older answers ignored, `/research/ws`
  subscribe of `watchlist:` = 1 of 64 + 64-cap + bad id, prev close = the header's value + cache + budget →
  `complete: false` + per-row source failure, 52W position).

**Review fixes (2026-10-17, MR4+MR5 review).**
- **H1 compare base:** `/indicators` takes an optional `cmp_base=<ISO t>` (only together with a `cmp:` spec,
  else 400): the base bar is that bar (t must be the exact start of a bar of BOTH symbols on the request's
  tf/session — inside the window or older, fetched with a 1-bar `/bars` call; missing → 400
  `{errors: {cmp_base: "… has no 1D bar starting at … — use the t of a loaded bar"}}`, no stand-in). With it
  every window bar where the other symbol has a bar gets a value and the same `t` gives the same value in
  every request (full window, live refresh with a smaller `limit`, paging left). The WPF/page sends the t of
  the FIRST LOADED bar (and re-sends the same base after paging left — or rebases explicitly with the formula
  in the cmp note). `indicators[].base` echoes `{t, close, other_close}`. Without `cmp_base` the old rule (first
  window bar) applies.
- **M3 idempotency:** POST `/research/drawings` takes an optional `client_id` (UUID, 36 chars with hyphens or 32
  hex; stored lower-case hyphenated, UNIQUE — store v6). A repeated POST with a stored `client_id` creates
  nothing and answers **200** with the existing drawing (its content is not compared with the retry); 201 =
  created; the same `client_id` on another symbol → 409 "This client_id already belongs to a drawing of
  another symbol.". The drawing object now carries `"client_id": str|null` (not exported, not patchable).
  Import: optional query `client_id=<UUID>`; entry i gets `uuid5(client_id, i)`; a retried import (any of them
  stored) writes nothing (also in `replace` mode) and answers 200 with the stored drawings and
  `"repeated": true` (a normal import: `"repeated": false`). The WPF should send a fresh UUID per user action
  and reuse it for automatic retries only.
- **L6:** a non-string `tool` (list/object/number/null) → 400 `errors.tool` (POST and import), never a 500.
- **L7:** `text`/`name` rules aligned with `watchlists.check_name`: Unicode space separators (Zs, e.g. U+00A0,
  U+202F) are allowed; categories C* (control, format such as U+200B / U+202E, private use, unassigned) and
  Zl/Zp are refused with "must not contain control or formatting characters (found U+XXXX)" — newline and tab
  stay allowed in `text`; `name` is stripped of leading/trailing spaces.
- **L9:** `GET /research/drawings?symbol=` of an equity triggers a split check (`drawings.request_check()` →
  the supervised task, non-blocking for the request) when `split_check.checked_at` is not today (NY date) or
  missing — closes the window between the daily runs. The check covers all equity symbols with drawings (one
  provider call), so the global `split_check` status stays truthful.
- Store **v6** (`drawings.client_id` + UNIQUE index, idempotent `_add_column` helper), claimed after MR6's v5
  (SPEC §6 table updated: MR7 alerts → v7). Tests: `tests/test_mr_drawings_review.py` (7: POST retry 200 +
  409 other symbol + invalid ids, import retry incl. replace, non-string tool POST/import, Unicode spaces vs
  control/format characters, list triggers the check only when not run today / never for crypto, the wake-up
  reaches the task, v6 migration idempotent + UNIQUE), `tests/test_mr_indicators_cmp.py` (same t → same value
  across full / limit-300 / paged windows, missing base bar 400, route validation).

**Follow-up (2026-10-17, after MR7): `alert_id` is owned by the alerts.** Only the alert transaction writes
`drawings.alert_id`. `POST /research/drawings` and `PATCH /research/drawings/{id}` with an `alert_id` key (any
value, null included) → 400 `{error: "invalid input", errors: {alert_id: "alert_id is managed by alerts (…)"}}`,
nothing changed; responses keep `alert_id`; other PATCHes keep an existing link. Import: an entry's `alert_id`
is stripped (never imported, as before all imported drawings start unlinked) and the answer carries
`"alert_id_ignored": true|false` (always present, also on `repeated` answers). The page's `drawingChanged`
may still list `alert_id` (bridge) — the WPF never forwards it (MR7 WPF notes). The MR7 tests that simulate a
stale/dangling link now write it through the store. Tests: `test_mr_drawings_review.AlertIdManagedTest` (2) +
adapted `test_mr_drawings`.

**Implementation details (2026-10-17).**
- Validation is in `drawings.py` (`validate_create`, `validate_patch`, `validate_import`) — errors keyed per
  field (`points[1].p`, `style.width`, `drawings[3].points`, at most 50 per answer). Body size: POST/PATCH
  compact JSON ≤ `MAX_DRAWING_JSON_BYTES` + 2 048, chart settings ≤ 4 096, import ≤ `MAX_IMPORT_BYTES`
  (compact JSON; `NaN`/`Infinity` literals, which `json.loads` accepts, are refused as non-finite numbers).
  A PATCH racing the split check (optimistic `updated_at` match) → 409 "The drawing was changed at the same
  time (e.g. by the split check) — reload it.". Single deletes, delete-all and imports are audited (`user`).
- `price_basis.as_of` is set to today's NY date on create and on every PATCH with `points` (the points are
  then in the chart's current basis); a split that happened but was not checked yet at that moment would be
  missed for that drawing — the window is small (check at start + daily) and accepted (default — user
  away 2026-10-16 — confirm).
- Split check: ONE provider call for all equity symbols with drawings (`sources.AlpacaSources.splits` →
  `AlpacaProvider.corporate_actions(kinds=forward_split, reverse_split)`, which chunks itself), range
  (oldest basis + 1 day, today]; nothing to do (all bases = today, or no equity drawings) → no request.
  Status in meta `drawings.split_check` `{state, checked_at, reason}`. Task wake-up: `drawings.request_check()`
  (called by the import route).
- Store: `migrate()` now runs `MIGRATIONS` sorted by version and `SCHEMA_VERSION = max(...)` (stages land in
  parallel: MR4's v3 and MR5's v4 were written at the same time). Store API added: `drawings_list`,
  `drawing_get`, `drawing_insert` (count check + insert in one `BEGIN IMMEDIATE`), `drawing_update`,
  `drawing_delete`, `drawings_delete_symbol`, `drawings_import`, `drawings_oldest_basis`,
  `drawing_apply_rescales`, `drawing_rescales_list`, `chart_settings_get/put`. Drawings, rescale log and
  chart settings are kept until deleted (no retention).
- Indicators: `indicators.compute(data, …)` calls `DataService.bars` twice (window, then the warm-up prefix
  with `before` = the window's first bar; a `BadParam` there, e.g. the 30-day intraday bound, = no older
  bars) plus ≤ 3 pages per compare symbol. All in one Research-pool call under research priority. The DSL
  evaluator runs on `Panel(dates = bar times, symbols = [symbol])` with the fields open/high/low/close/
  volume/vwap; EMA/MACD/RSI warm-up nulls are applied on top of the DSL's own NaNs (`warmup_nulls`).
- New `limits.py` constants: `SPLIT_CHECK_INTERVAL_SECONDS` 86 400, `SPLIT_CHECK_RETRY_SECONDS` 3 600,
  `MAX_INDICATORS` 8, `MAX_SUB_PANES` 3, `MAX_COMPARE` 2, `IND_QUERY_MAX_CHARS` 400, `EMA_WARMUP_FACTOR` 4,
  `WILDER_WARMUP_FACTOR` 10.
- Deviations / choices (default — user away 2026-10-16 — confirm): the indicator list parameter is `ind`
  (SPEC §4.2; the task brief said `set`); "max 8 indicator series" = 8 indicator SPECS (a Bollinger or MACD
  spec has 3 output lines); drawing field `name` (object panel "rename") and style key `levels` (Fibonacci
  levels, plan §4.1) are additive to the §10 model; arrow markers are two tools `arrowUp`/`arrowDown`;
  dividends are not rescaled; compare overlay only within one asset class.
- **For the MR5 chart/WPF agent:** the MR3a page accepts `loadDrawings` points ≤ 16 — `brush` needs up to
  500 (raise the page/host limit for brush only); points come with ISO `t` from the backend
  (`ChartHost.cs` converts, §0 Time); text/name are plain text (`textContent` / TextBlock); the object panel
  edits via PATCH (send only changed keys); undo/redo stays page-side (50 steps) and persists the resulting
  state via PATCH/POST/DELETE; "delete all" = `DELETE ?symbol=&confirm=true` after the UI confirmation;
  export/import files are `<SYMBOL>-drawings.json` via the WPF save/open dialogs; show `split_check.reason`
  when `state == "failed"`; indicator series are sent to the page as data (`setIndicators` gets the
  series, the page never computes them — Q8); chart settings PUT on every indicator/type/magnet/session change
  (debounced), GET when a symbol opens.
- **Tests** (37, all offline): `tests/test_mr_drawings.py` (14: every tool's point counts ± 1, defaults +
  time normalisation, 26 rejection cases, tool-specific fields (text note, Fibonacci levels, alert id,
  timeframes order), size limits (500-point brush too large, 501 refused, per-symbol cap 409), CRUD + 404s,
  lock rules, points PATCH resets the basis, delete-all confirm, route registration, export/import round
  trip (replace + append, ids re-assigned, alert links dropped), 14 bad files rejected with nothing written,
  import cap 409, crypto basis, export above the import limit 409, chart settings);
  `tests/test_mr_drawings_split.py` (7: forward split ÷4 once + log + audit + idempotent incl. a manually
  reset basis, reverse ×10 then ÷2 in ex-date order, failures (SecretMissing / DataSourceUnavailable)
  change nothing + `failed` status, unusable rates skipped + audited, future splits ignored, crypto never
  asked, no request when nothing is due, import keeps the file basis then rescales, task at start under
  research priority in a Research thread + wake-up, retry delay 1 h); `tests/test_mr_indicators.py` (16:
  grammar/normalisation, 24 rejections incl. 4 sub-panes / 9 specs / 3 compares, EMA half-life ↔ α, parity
  with the DSL evaluator on `Panel.from_bars` (sma, ema on hlc3, Bollinger, rsi, macd/signal/hist), pandas /
  hand-written Wilder references, warm-up nulls without history, warm-up history loaded + cut (values equal
  the longer computation), no look-ahead (prefix cuts), empty window, VWAP session reset + a bar without
  vwap ends the session line, incomplete first session null, VWAP on 1D refused, compare pct/price/base + a
  missing bar null + other asset class refused, route: same `t` as `/bars` for 1D / 15m eth / 1H, response
  keys, validation 400s, unknown compare symbol 404, crypto + compare). MR tests 216 OK; full suite OK in
  split runs (sp a/b/c–e/f–p/q–s/t–z, rest); scope guard stage0 + mr0 ok.

### MR5 WPF notes (implemented 2026-10-17: tool rail, objects panel, persistence, indicators, chart settings)

- **Files.** New `MarketResearch/Chart/ChartHost.Drawings.cs` (partial: bridge revision 2 host side — page
  events `drawingCreated/Changed/Deleted`, `selectionChanged`, `undoState`, `toolChanged`, `shortcut`; host
  messages `loadDrawings` (chunked ≤ 3 MiB with `append`), `drawingSaved`/`drawingRejected`/`upsertDrawing`,
  `editDrawing`, `removeDrawing`, `selectDrawing`, `jumpToDrawing`, `setTool {name, style}`, `setMagnet`,
  `command`, `setIndicators`, `updateIndicators`; ISO ↔ ms for drawing points and indicator `t` ONLY here),
  `MarketResearch/ResearchView.Drawings.cs` (rail, style popup, objects panel, write queue, export/import,
  keys), `MarketResearch/ResearchView.Indicators.cs` (indicators menu, /indicators, chart settings).
  Changed: `ChartHost.cs` (partial; `ready.revision`, page epoch, MR5 state re-sent after `loadBars`, drawings/
  indicators cleared on a symbol change, page error `drawing_invalid` → drawing status line, `drawing_unknown`
  log only; bar session flag `s` validated (`pre|rth|post`, else the bar is dropped like any invalid bar) and
  passed through, omitted when absent — the page refuses `s: null`), `ResearchClient.cs` (MR5 routes + DTOs,
  `ApiBar.s`; optional field-error prefix "Not imported: " / "Chart settings not saved: " / "Indicators not
  loaded: "), `ResearchView.xaml` (INDICATORS ▾ / OBJECTS · n buttons, drawing status row, rail / chart /
  objects columns), `ResearchView.xaml.cs` + `ResearchView.Workspace.cs` (hooks, `ResearchTab.FromSession`,
  palette mode `Compare`, Ctrl+T/W/Tab). Checked statically only (no dotnet on the VM).
- **Rail** (left of the chart, shown for revision-2 pages, enabled once the symbol's drawings are loaded):
  cursor + the 18 backend tools (tooltips + AutomationProperties incl. Alt+T trend line, Alt+H horizontal,
  Alt+F Fibonacci, Alt+R rectangle, Esc cursor — handled by the page while it has the focus and by the view
  otherwise), style ◐ (popup: 8 swatches = bridge palette incl. blue #4C8DFF / violet #B48CF2, width 1–4,
  solid/dashed; applies to the selected drawing via `editDrawing`, else to new drawings via `setTool.style`;
  Fibonacci levels stay default), magnet off → weak → strong (persisted in chart settings), ✓ keep measure
  (only while an unsaved measure is selected), undo/redo (enabled by `undoState`), lock/unlock all, hide/show all
  (view only), delete all (inline confirmation in the status row → `DELETE ?confirm=true`, then an empty
  `loadDrawings`).
- **Objects panel** (OBJECTS · n): per drawing name/note (plain text) + tool · TF · locked/hidden; buttons jump
  ⌖, show/hide, lock/unlock, TF (popup: all or a subset; ≥ 1 required), rename ✎ (inline, Enter/Esc, ≤ 40,
  no control characters, empty = no name), delete × — every edit goes through the page (`editDrawing` /
  `removeDrawing` → undoable → echoed → PATCH/DELETE: one write path). Export… (`<SYMBOL>-drawings.json`,
  indented UTF-8) / Import… (file ≤ 1 MiB read, ≤ 256 KiB compact checked before sending, then Append /
  Replace all / Cancel; afterwards the drawings are re-loaded). Load errors + Retry, split-check failure text
  ("Split check failed: … — drawings may be misplaced after a split.").
- **Persistence queue (decision, default — user away 2026-10-17 — confirm).** FIFO, one request at a time;
  PATCH waits 400 ms and absorbs later PATCHes of the same drawing (when it is the last queued op).
  Refusal (400 field messages, 409 lock/limit/race, 404, 500) → `drawingRejected` (page reverts) + the text
  verbatim in the status row. Transient (backend not reachable, 401, 503) → the queue PAUSES and retries 2, 5,
  10, 30, 60, 60 … s with a visible countdown and count ("… lost if UFOS.ai is closed before they are saved");
  bounded at 1000 waiting changes (≥ 2 × 500 for lock-all), more are refused at once. In memory only (not
  persisted across an app restart — stated in the UI). A POST answer for a page that no longer knows the
  `localId` (re-created page / replacing load) is sent as `upsertDrawing`; a PATCH answer that differs from
  the host copy (e.g. split rescale) as `upsertDrawing`; DELETE 404 = already gone. Drawings loaded while
  changes for that symbol were waiting are loaded once more when the queue drains. Delete-all and import wait
  until the symbol has no waiting changes. A drawing made while the symbol's drawings are NOT loaded is
  refused (no partial state).
- **Load rules.** Drawings: GET when a symbol's bars are loaded and its drawings are not (symbol change, page
  re-created, failed earlier); NOT on a timeframe change (the page filters by `timeframes` itself).
  Chart settings: GET when a symbol is activated (cached per symbol); indicators + magnet always from them;
  type/session only for a tab opened now (a tab restored from /research/session keeps its own); PUT 800 ms after
  a type / session / magnet / indicator change; never PUT before they were loaded (no overwrite).
- **Indicators.** Menu: active list (backend label + note tooltip, ×), add SMA/EMA (period + source cycle
  close/open/high/low/hl2/hlc3/ohlc4), Bollinger (n, k, source), RSI (n), MACD (fast < slow, signal), VWAP
  (kept in the settings, requested only on 1m…4H — "intraday only" note), compare via the Ctrl+K palette in
  "COMPARE WITH" mode (same asset class, ≠ chart symbol). Client checks mirror the backend (≤ 8 specs, ≤ 3
  panes "At most 3 indicator panes (RSI, MACD) — remove one first.", ≤ 2 compares, ranges, duplicates);
  the backend's normalised spellings replace the local list after the PUT. Data: `/indicators` with the
  chart's tf/session and `limit` = the loaded bar count (≤ 5000 intraday / 2000 daily) → `setIndicators`;
  after paging the same `before` as `/bars` → `updateIndicators`; after a live candle closes (`final`) once
  per bar, 1.5 s later, `limit` 300 → `updateIndicators`. Errors verbatim in the status row; the chart keeps
  working. Volume stays on (no setting in chart settings).
- **Shortcuts.** Page `shortcut` and the view: Ctrl+K / Ctrl+T palette, Ctrl+W close tab, Ctrl+Tab /
  Ctrl+Shift+Tab next/previous tab (not while a text box or the palette has the focus).
- **Open / verify on the PC:** first build; popups (style / TF / indicators) in the layered windows; the
  rail focus order; page revision-2 messages end to end with the real engine.
- **MR5 WPF review fixes (2026-10-17, bridge revision 3).** H1: every compare request carries
  `cmp_base` = the ISO start of the first bar of the full load's window (the same value on the live
  refresh); paging left with a compare active re-loads the whole window (the base moves to the new left edge —
  "% from the left edge"), without a compare the older page is merged as before. M3: `drawingCreated.clientId`
  (UUID v4, required from revision 3) is sent as `client_id` on the POST — a retry after a timeout reuses it,
  the backend's UNIQUE `client_id` makes the POST idempotent. M4: after `drawingRejected` (change/delete) the
  host always sends `upsertDrawing` with the backend's last CONFIRMED state of that drawing (load / POST /
  PATCH answers, kept per id; fallback = the host copy before the step); merged PATCHes keep the first
  step's before-state; a PATCH answer is not upserted while later changes of the same drawing are queued;
  style / rename / delete of a locked drawing are refused in the WPF with "The drawing is locked — unlock it
  first". M5: `ResearchSocket` keeps the server's subscription ORDER; when a topic would only be appended
  although it belongs in front (tab switch: chart first, priority for the 30-symbol limit), it unsubscribes
  everything and sends ONE subscribe in `BuildTopics` order (removals / true appends stay a diff). L8: the
  import is checked and SENT as compact JSON with non-ASCII as raw UTF-8 (`ResearchClient.CompactJson`,
  `RawJsonBody`) — the byte count equals the backend's rule. Shortcuts: only exact Ctrl+K/T/W (no Shift) and
  Ctrl+Tab / Ctrl+Shift+Tab, in the page (chart agent) and in the view.

### MR5 chart notes (implemented 2026-10-17: chart page — drawings + indicators, bridge revision 2)

- **Files:** `MarketResearch/Chart/chart.js` (bridge rev 2: schema mirrors the backend drawing/indicator
  validation; modules `indicators` + `drawings`; `ready.revision = 2`), `chart.html` (legend row
  `#legend-ind`), `chart.css` (note editor, freehand preview); tests `tests/run_tests.js` (+107 checks,
  195/195 with the test double), `tests/fake_klinecharts.js` (overlays, interactive-draw hooks, figure
  indicators, sub panes, removeIndicator, convertTo/FromPixel). Contract: "MR5 bridge contract
  (revision 2)" above. Screenshots (double): `10_mr5_all_tools`, `11_mr5_hidden`, `12_mr5_indicators`,
  `13_mr5_three_subpanes` (scratchpad of the session; not shipped).
- **Tools → engine overlays.** KLineChart built-ins: `segment`, `rayLine`, `straightLine`,
  `horizontalStraightLine`, `horizontalRayLine`, `verticalStraightLine`, `priceLine`,
  `parallelStraightLine`, `priceChannelLine`, `simpleAnnotation` (text = `extendData`), `simpleTag`.
  Custom (`registerOverlay`, geometry only from `coordinates`): `fibonacciLine` → `ufosFib` (levels
  configurable via `style.levels`, default 0/23.6/38.2/50/61.8/78.6/100; 0 % at the 2nd point, 100 % at
  the 1st; built-in Fibonacci has fixed levels), `rect` → `ufosRect`, `measure` → `ufosMeasure` (Δ price,
  Δ %, bars, duration; temporary until Enter / `command saveMeasure`), `arrowUp`/`arrowDown` →
  `ufosArrowUp/Down`, `alertLine` → `ufosAlertLine` (full-width line + bell + price; `alert_id` kept,
  MR7 links it), `brush` → `ufosBrush` (freehand captured by the PAGE with pointer events, thinned to
  ≤ 500 points, path in `extendData`, drawn with `chart.convertToPixel`; overlay anchors = first/last
  point; a brush is not draggable). Points = `{timestamp: t, value: p}` → the same drawing appears on
  every timeframe; `timeframes` filtered by the page on each `loadBars`.
- **Interaction.** Interactive drawing = `createOverlay({name, …, onDrawEnd})` without points; after the
  last point the page sends `drawingCreated` and returns to the cursor (`toolChanged null`). Text note:
  inline `<textarea>` (plain text, max 280, Enter saves, Shift+Enter newline, Esc cancels; double-click
  a note edits it); rendered as canvas text only. Drag end (`onPressedMoveEnd`) → `drawingChanged`;
  `onSelected/onDeselected` → `selectionChanged`; an overlay the engine removes itself (`onRemoved`
  not caused by the page) counts as a user delete. Magnet `off|weak|strong` → overlay `mode`
  `normal|weak_magnet|strong_magnet` (new overlays + `overrideOverlay` for existing). Lock = overlay
  `lock`; hide all = view toggle (overlays re-created invisible). Undo/redo: page history of
  create/delete/change/batch steps (50, cleared by a replacing `loadDrawings`, `upsertDrawing`, a new
  symbol); every step is persisted through the normal created/changed/deleted messages; a rejected
  step is reverted and removed from the history. Keys: Ctrl+Z, Ctrl+Y / Ctrl+Shift+Z, Del, Esc, Enter,
  Alt+T/H/F/R (by `KeyboardEvent.code`); Ctrl+K/T/W/Tab/Shift+Tab forwarded as `shortcut`.
  (WPF note: Alt+letter may also reach WPF access keys when the WebView passes them on.)
- **Indicators (Q8).** One registered indicator template per spec (`UFOS_I_<key>`), `calc` maps the
  engine's bars to the backend values by `t` (no computation), `figures` `v0..v2` (lines; MACD `hist` =
  bar figure coloured by sign; Bollinger bands dashed; RSI + 30/70 guides, range 0–100; compare: the
  `:price` line is plotted, `:pct` is shown in the legend). Overlay specs on `candle_pane`
  (`createIndicator(value, true, {id: 'candle_pane'})`), sub specs on `ufos_sub_0..2`
  (`createIndicator(value, false, {id, height: 90})`); `volume: false` removes the VOL pane. Values are
  cleared on a `loadBars` with another symbol/tf/session; `updateIndicators` merges and redraws.
  Legend row: label (palette colour) + value at the hovered/last bar; the backend `note` as tooltip.
- **Calls to verify with `run_tests.js --engine real`** (MR5 additions; MR3a list above still applies):
  `klinecharts.registerOverlay({name, totalStep, needDefaultPointFigure, createPointFigures({overlay,
  coordinates, bounding}) → [{type: line|polygon|text, attrs, styles, ignoreEvent}]})` and the figure
  attrs/styles keys (`polygon.style 'stroke_fill'`, `borderStyle`, `text.backgroundColor`, `align`,
  `baseline`); `klinecharts.getSupportedOverlays()` containing the 11 built-ins;
  `chart.createOverlay({name, id, groupId, points: [{timestamp, value}], styles {point, line, polygon,
  rect, text}, lock, visible, mode, extendData, onDrawEnd, onPressedMoveEnd, onSelected, onDeselected,
  onRemoved, onDoubleClick})` → id (string or array), interactive drawing without points, event payload
  `e.overlay.points[{timestamp, value, dataIndex}]` (timestamp right of the last bar), callback return
  value semantics; `simpleAnnotation`/`simpleTag` taking the text from `extendData`;
  `chart.removeOverlay({id})` (+ whether it fires `onRemoved`), `chart.overrideOverlay({id, mode})`,
  `chart.convertToPixel(points[], {paneId})` / `convertFromPixel(coords[], {paneId})` shapes,
  `chart.scrollToTimestamp`; magnet mode names `weak_magnet`/`strong_magnet`; whether the engine
  handles Delete/Backspace on a selected overlay itself; indicator templates with `figures` +
  per-figure `styles(data)` callbacks (`data.current`), `minValue`/`maxValue`, `precision`, re-
  registering a template name, `createIndicator` with paneOptions `{id, height, minHeight}` creating a
  new pane, `removeIndicator({name, paneId})` (v10) vs `(paneId, name)` (v9) — the page tries both and
  reports `engine_api` if neither works; pointer events on the chart container in the capture phase not
  disturbing the engine's own handlers while the brush is off.
- **Runtime checks** (`error engine_api`, once per detail, drawings disabled when fatal for them):
  missing `registerOverlay`/`createOverlay`/`removeOverlay`/`overrideOverlay`/`convertToPixel`/
  `convertFromPixel`, missing built-in overlays, `createOverlay` returning nothing,
  `overrideOverlay`/`removeOverlay` throwing, `convertTo/FromPixel` not returning arrays,
  `removeIndicator` without effect, `createIndicator` landing on another pane.
- **Deviations / defaults (user away — confirm):** measure stays temporary until Enter/`saveMeasure`;
  hide all is a view toggle (not persisted); a brush cannot be dragged; `blue` `#4C8DFF`, `violet`
  `#B48CF2` (agreed with the WPF agent); indicator colours assigned by the page in spec order
  (accent, violet, caution, blue, text, up, down, muted; MACD signal caution/blue).
- **MR5 chart review fixes (revision 3, 2026-10-17).** H2: a create whose POST is pending is never sent
  twice — undo marks it "delete after save", redo reuses its `localId` + `clientId`; `drawingSaved`
  then sends `drawingDeleted` (still undone) or one `drawingChanged` with the difference between the
  stored body and the current state; a superseded `localId` is answered with `drawingDeleted`.
  `drawingCreated` carries `clientId` (UUID v4 via `crypto.getRandomValues`); backend objects may carry
  `client_id`. M4: no local revert to an intermediate state on `drawingRejected(change)` (the host's
  `upsertDrawing` decides), `drawingRejected(delete)` restores the pre-delete state, `upsertDrawing`
  replaces the drawing authoritatively (also a deleted one) and drops only its history. Ctrl+Shift+K/T/W
  are no longer forwarded as Ctrl+K/T/W. `ready.revision` = 3. Harness 207/207 (test double; +12: undo/
  redo sequences before/after the answer, offline churn with late out-of-order answers, clientId format
  and uniqueness, rejected change/delete + authoritative upsert, client_id accepted).

