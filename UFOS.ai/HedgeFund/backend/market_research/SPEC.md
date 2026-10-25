# Market Research — implementation contract (CURRENT)

> Part of UFOS.ai, a proof-of-concept portfolio project. Research only, paper trading only, not investment advice.
> Concept: Claude Project `claude/market-research-plan.md`. BINDING for MR1–MR8; change it together with the code.
> All MR0 defaults (Q1–Q12, C1–C5 and every "default — confirm" choice) were **confirmed by the user 2026-10-24**
> ("Das passt").
>
> **How to read this file (DOCS-SLIM, 2026-10-24):** what is TRUE NOW. Stage notes (exact JSON per route, WPF /
> chart notes, review fixes), the bridge contracts and the full MR0 text live verbatim in `spec-archive/`. Section
> numbers are stable; moved text leaves a stub `→ spec-archive/<file>`.

## 0. Ground rules (all stages)

Full MR0 text of §0–§9: → spec-archive/00-mr0-contract-sections-0-9.md

- **Scope:** only `UFOS.ai/HedgeFund/**` (plus `UFOS.ai/docs/todo.md` when a task says so); no MainWindow change, no
  new port (backend 8867), no new venv, no new tile. Scope guard `python HedgeFund/tools/scope_guard.py check
  --label <label>` → "RESULT: ok".
- **Package** `HedgeFund/backend/market_research/` (import `market_research`) — NOT `strategy_platform/research/`
  (that is Strategy Studio).
- **No unrequested fallbacks:** a missing key / feed / source / limit stops the affected function with a clear message
  (reason + what to do); no second source, no snapshot instead of a stream, no cached value shown as current, no
  default value.
- **Never disturb trading:** every Research task runs under the Research supervisor (§1.4 / §5); a failure marks
  Research `unavailable` and never stops the paper engine, kill / dead-man switch, collectors or platform jobs.
  Research never places orders.
- **Security:** loopback, `api_auth` token on every route + the WebSocket, strict input (unknown fields / params →
  400), size limits, external text untrusted plain text, no stack traces / secrets in responses or logs. Keys only via
  `strategy_platform/secrets.py` / `Platform/CredentialStore.cs` (`alpaca-paper-key-id` / `alpaca-paper-secret-key`,
  D-MR2).
- **Python:** offline `unittest`, temp dirs, fakes, no network; aiohttp / websockets imported lazily and stubbed in
  tests. Tests `HedgeFund/backend/tests/test_mr_*.py`.
- **Time:** UTC ISO-8601 with seconds in EVERY HTTP response and `/research/ws` message (bar `t` = bar start). ONLY
  exception: the chart bridge uses epoch ms UTC, converted in exactly one place, `MarketResearch/Chart/ChartHost.cs`.
  Market logic in `America/New_York` (`core/calendars.py`); the WPF shows local time.
- UI text English; no AI features in v1 (Q11). **Reuse, don't duplicate** the `strategy_platform` data layer; every
  extension of `strategy_platform` code is recorded in §12 AND in `strategy_platform/SPEC.md` §12.

## 1. MR1 contract — WPF ↔ backend surface

### 1.1 Transport

- Base URL `http://127.0.0.1:8867`, header `X-UFOS-Token` from `backend/data/api-token` (`Shared/BackendAuth.cs`;
  re-read once on a 401 and retry once). Routes under `/research/*`; WebSocket `ws://127.0.0.1:8867/research/ws`,
  token ONLY in the request header (no `?token=`).
- JSON UTF-8 strict (no NaN, `routes.dumps`); WPF DTOs ignore unknown fields; computed DTO properties `[JsonIgnore]`.

### 1.2 `GET /research/feeds/status`

No query parameters (any query parameter → 400 `{error, errors}`). No body.
Answers **200** whenever the Research package is loaded — a broken Research
part is reported INSIDE the payload (`research.state`), not as an HTTP error.

Exact shape (MR1 values shown; every key is always present, `null` where
stated):

```json
{
  "schema": 1,
  "generated_at": "2026-10-16T12:00:05+00:00",
  "research": {
    "state": "ok",
    "reason": null,
    "since": "2026-10-16T12:00:00+00:00"
  },
  "store": {
    "state": "ok",
    "reason": null,
    "file": "market_research.db",
    "schema_version": 1
  },
  "tasks": [
    {
      "name": "store_maintenance",
      "label": "Research database maintenance",
      "state": "running",
      "restarts": 0,
      "max_restarts": 5,
      "last_error": null,
      "last_error_at": null,
      "next_restart_at": null
    }
  ],
  "feeds": [
    {"id": "stocks", "label": "Stocks", "feed_label": "IEX · real time",
     "state": "off", "message": "Live data is not built yet (stage MR2).",
     "attempt": 0, "connected_since": null, "last_message_at": null,
     "symbols": 0, "symbol_limit": 30},
    {"id": "crypto", "label": "Crypto", "feed_label": "Alpaca crypto · real time",
     "state": "off", "message": "Live data is not built yet (stage MR2).",
     "attempt": 0, "connected_since": null, "last_message_at": null,
     "symbols": 0, "symbol_limit": null},
    {"id": "news", "label": "News", "feed_label": "Alpaca · Benzinga",
     "state": "off", "message": "Live data is not built yet (stage MR2).",
     "attempt": 0, "connected_since": null, "last_message_at": null,
     "symbols": 0, "symbol_limit": null}
  ],
  "live_count": 0,
  "feed_count": 3,
  "demand": {
    "views": 0,
    "alerts": 0,
    "streams_wanted": false,
    "note": "Live streams run only while a Research view is visible or an alert is active."
  },
  "budget": {
    "source": "alpaca",
    "shared_per_minute": 180,
    "research_max_per_minute": 60,
    "research_used_last_minute": null,
    "waiting_seconds": null
  }
}
```

Field rules:

| field | values / meaning |
|---|---|
| `schema` | int, 1 in MR1–MR8; bumped only on a breaking change |
| `research.state` | `starting` (backend up, supervisor not started yet) · `ok` · `unavailable` · `stopped` (backend shutting down) |
| `research.reason` | `null` when `ok`/`starting`/`stopped`; otherwise ONE plain-text sentence ≤ 300 chars, safe to show (no stack trace, no secret), e.g. `Research task "store_maintenance" crashed (OSError — see backend.log) — restarting in 15 s (attempt 2 of 5). Trading is not affected.` |
| `research.since` | when the current state began (UTC ISO) |
| `store.state` | `ok` · `unavailable` (reason set) · `not_opened` (before the maintenance task opened it) |
| `store.file` | file NAME only (never the full path) |
| `store.schema_version` | int, `null` unless `ok` |
| `tasks[].state` | `pending` · `running` · `restarting` (crashed, waiting `next_restart_at`) · `failed` (gave up after `max_restarts`) · `stopped` |
| `tasks[].last_error` | plain-text reason of the last crash (`UserFacingError.user_message`, else `"<ExceptionType> — see backend.log"`), ≤ 300 chars, or `null` |
| `feeds[].id` | `stocks` · `crypto` · `news` (fixed order) |
| `feeds[].state` | `off` (not needed / not built) · `connecting` · `live` · `waiting` (reconnect backoff; `attempt` ≥ 1) · `unavailable` (`message` = reason; NOT on the fast reconnect backoff — only the slow re-check schedule of §7.6 applies: missing keys every 60 s, 406 / keys rejected / plan refused every 5 min while demand > 0, plus at every demand change) |
| `feeds[].message` | plain text ≤ 300 chars, always set (also for `live`, e.g. `Live · 12 symbols`) |
| `feeds[].attempt` | reconnect attempt counter (0 when not waiting) |
| `feeds[].symbol_limit` | 30 for stocks (Alpaca Basic plan), `null` = no limit |
| `live_count` / `feed_count` | number of feeds with `state == "live"` / number of feeds |
| `demand` | `views` = open Research WebSocket clients that report visible, `alerts` = active alert rules; `streams_wanted` = views + alerts > 0 |
| `budget.*` | Q3: the shared Alpaca gate stays 180/min (150/min since 2026-10-23, strategy_platform SPEC §21.1 — `shared_per_minute` follows `limits.SHARED_ALPACA_PER_MINUTE`), Research ≤ 60/min of it; `research_used_last_minute` / `waiting_seconds` are `null` until MR2 measures them (int / number then) |

`research.state` is derived: `stopped` if shutting down; else `starting`
until the supervisor started; else `unavailable` if the supervisor could not
start, the store is `unavailable`, or any task is `restarting`/`failed`
(reason = the first such item: start error, store, tasks in order); else
`starting` while the store is still `not_opened`; else `ok`. Feeds in `waiting`
or `unavailable` do NOT make Research unavailable (a feed problem is shown
per feed).

### 1.3 HTTP errors and what the WPF shows

| Situation | Response | WPF shows (Research section only) |
|---|---|---|
| backend not reachable (5 s) | — | "Research unavailable: the HedgeFund backend is not reachable." + Retry; polling continues |
| token missing / invalid | 401 (api_auth) | re-read token once; still 401 → "…the backend rejected the app's token — restart the HedgeFund backend." |
| Research package failed to load | 503 `{"error": "Market Research failed to load — see backend.log. Trading is not affected.", "kind": "ResearchUnavailable"}` for every `/research/*` (stub in main.py) | "Research unavailable: " + `error` |
| unexpected error in a route | 500 `{"error": "internal error in Market Research (see backend.log)"}` | "Research unavailable: internal error (see backend.log)." |
| bad input | 400 `{"error": "invalid input", "errors": {field: message}}` | client bug — log, show `error` |
| old backend | 404 | "Research needs a newer backend — restart the HedgeFund backend." |
| 200, `research.state` `unavailable` / `starting` | — | banner "Research unavailable: " + reason / "Research is starting…" |

### 1.4 Isolation

- Each Research task has its own error boundary (`supervisor.py`): an exception is logged with stack, recorded as
  `last_error`, turns `research.state` `unavailable` and never propagates to main.py, the engine or other tasks.
- Restarts after 5, 15, 30, 60, 120 s (attempt 1–5, reason text "Research task "<name>" crashed (…) — restarting in
  <n> s (attempt <k> of 5). Trading is not affected."); the 6th crash within 15 min → `failed` ("… restart the
  HedgeFund backend. Trading is not affected."); a run ≥ 15 min clears the history; a returning task = a crash ("ended
  unexpectedly"); no restart while stopping.
- Install wrapped in try / except in main.py (failure → the 503 stub); `start()` only creates tasks; the store is
  opened inside the supervised maintenance task (Research pool thread); `POST /shutdown` gives Research ≤ 2 s.

### 1.5 WPF status polling

- Poll `GET /research/feeds/status` every 5 s only while the Research section / pop-out is visible AND
  `/research/ws` is down (the socket delivers `{"type": "feeds", "data": <payload>}`).
- Status bar `●●● LIVE · <live_count>/<feed_count>` — one dot per feed (live green, connecting / waiting caution, off
  muted, unavailable red), state word in the tooltip; no feed live → `OFF` / `WAITING` / `UNAVAILABLE` (precedence
  unavailable > waiting > connecting > off). `demand.note` shown (Q3). Payload strings are plain text.

## 2. Decisions (all confirmed by the user; full table → spec-archive/00-mr0-contract-sections-0-9.md §2)

- **D-MR1** Research = a section of the HedgeFund module (rail entry "Research" + pop-out window). **D-MR2** the
  existing HedgeFund Alpaca paper keys. **D-MR3** no MainWindow tile, no new port / venv.
- **Q1** KLineChart **10.0.3** (Apache-2.0) in WebView2, vendored with LICENSE + MANIFEST sha256, behind the bridge
  contract, CSP without network. **Q2** intraday model **c** (SIP history + IEX live tail, §7.1). **Q3** the shared
  Alpaca `http_gate` is **150 req/min + burst 10** (since 2026-10-23; strategy_platform SPEC §21.1), Research ≤ **60
  req/min** of it and always yields (§7.5); streams only while a Research view is visible or an alert is active.
  **Q4** Buy / Sell = hand-over to the manual paper-order flow (Trading → `/v2/oms/orders`), never live, Research
  never orders. **Q5** crypto: Alpaca coins only ("Not available from Alpaca (the configured crypto source).").
  **Q7** sidebar Overview / Symbol / Alerts + Portfolio + Macro (MR8). **Q8** indicators = the platform DSL
  operators. **Q9** crypto "today" = UTC day. **Q10** beyond 30 streamed symbols: message only. **Q11** no AI in v1.
  **Q12** 1m retention 30 days.
- **C1** Research 1m bars in `market_research.db` (30 days); daily bars from the hedgefund.db cache. **C2** session
  status from `core/calendars.py` (no Alpaca clock calls). **C3** WebSocket on 8867 `/research/ws`, header token.
  **C4** news thumbnails on by default (setting; image hosts see the IP). **C5** audit rows kept 365 days.
- **F1** DB `HedgeFund/backend/data/market_research.db`. **F2** hub reconnect with backoff + visible waiting state +
  `network.probe`. **F3** WebView2 PackageReference **1.0.4191.47** in `MultiAgentHedgeFund.csproj`.

## 3. Package layout

`market_research/`: `__init__.py` (install / service), `limits.py` (§4.4, one place), `supervisor.py`, `store.py`,
`feeds.py`, `maintenance.py` (task `store_maintenance`), `executor.py` (§5.1), `api.py` + `data_api.py` (routes, WS),
`assets.py` (task `asset_refresh`), `market.py`, `bars/` (aggregate, intraday model c, history), `stats/` (snapshot,
keystats, quote), `hub/` (hub, alpaca_stream, candles, demand), `watchlists.py`, `session.py`, `workspace_api.py`
(MR4), `drawings.py`, `indicators.py`, `chart_settings.py`, `drawings_api.py` (MR5), `news.py`, `logos.py`,
`news_text.py`, `media.py`, `news_api.py` (MR6), `alerts.py`, `alerts_api.py` (MR7), `integrations.py`,
`integrations_api.py` (MR8). WPF `HedgeFund/MarketResearch/` (namespace `UFOS.ai.HedgeFund.MarketResearch`):
`ResearchView`, `ResearchPopoutWindow`, `ResearchClient.cs`, `ResearchSocket.cs` (the one WS client), `Chart/`
(`ChartHost.cs`, `chart.html/.js/.css`, `vendor/`, `tests/`), watchlist panel, command palette, alerts view, drawing
objects panel, settings dialog. File-level map: `HedgeFund/docs/CODEMAP.md`.

## 4. API (all under `/research/*`, behind `api_auth`)

### 4.1 Conventions

- `ResearchApi` methods `(query, body, match) -> (status, payload)` + `ROUTES`, `routes.dumps` / `read_body`; own
  limits (§4.4) → 400. Errors: 400 invalid input (unknown params / fields), 404 unknown id / symbol, 409 conflict,
  `UserFacingError` → 503 `{error, kind}`, else 500 generic. Optional query parameters may be absent
  (`query.getall(key, [])`, live-test fix 2026-10-18).
- Symbols: platform symbols (`NVDA`, `BRK.B`, crypto `BASE-USD`), upper-cased; crypto not in Alpaca's list → 404
  `SymbolNotCovered`. Timeframes `1m 5m 15m 1H 4H 1D 1W`; sessions `rth` (default) / `eth` (stocks only).
- **`Num` = `{value: number|null, feed, feed_label, as_of, reason}`** — a missing value is null WITH the reason;
  `feed` ∈ `iex`, `iex_tail`, `sip_delayed`, `sip_daily`, `crypto`, `computed`.

### 4.2 Routes (exact JSON per route → the stage notes in spec-archive/)

| stage | routes |
|---|---|
| MR1 | `GET /research/feeds/status` (§1.2) |
| MR2 | `GET /research/assets?q=&limit=` (q 1–32 chars, limit 1–50, order exact / prefix / name word / substring); `GET /research/market/status` (§7.3, + `extended_opens_at`, `extended_closes_at`, `tz`, `ny`); `GET /research/symbols/{s}/snapshot` (header §7.4); `GET /research/symbols/{s}/bars?tf=&session=&before=&limit=` (`bars [{t, o, h, l, c, v, vwap, n, feed, final, s?}]`, `has_more_left`, `sip_through`, `tz`; intraday equity bars carry `s: pre|rth|post`); `GET /research/symbols/{s}/stats`; `GET /research/ws` (§4.3) — archive mr2-notes.md |
| MR4 | `GET/POST /research/watchlists`, `PATCH/DELETE /research/watchlists/{id}`, `PUT/POST /research/watchlists/{id}/items`, `DELETE /research/watchlists/{id}/items/{symbol}`, `GET /research/watchlists/{id}/quotes`, `GET/PUT /research/session`, `POST/DELETE /research/recent`, `GET /research/palette?q=&limit=` — archive mr4-notes.md |
| MR5 | `GET/POST /research/drawings`, `PATCH/DELETE /research/drawings/{id}`, `DELETE /research/drawings?symbol=` (no confirm needed since fix1023), `GET /research/drawings/export?symbol=`, `POST /research/drawings/import`; `GET /research/symbols/{s}/indicators?…&ind=`; `GET/PUT /research/chart-settings?symbol=` — archive mr5-notes.md |
| MR6 | `GET /research/symbols/{s}/news?limit=&before=`; `GET /research/symbols/{s}/logo` (PNG or 404 `NoLogo` / `LogosNotOnPlan`); `GET /research/news/images/{key}` (backend thumbnail proxy — never a third-party URL to the WPF); `GET/PUT /research/news/settings {thumbnails}` — archive mr6-notes.md |
| MR7 | `GET/POST /research/alerts`, `PATCH/DELETE /research/alerts/{id}`, `GET /research/alerts/log?limit=&alert_id=` — archive mr7-notes.md |
| MR8 | `GET /research/portfolio`, `GET /research/symbols/{s}/markers?from=&to=&kinds=insider,calendar`, `GET /research/backtests/{run_id}/markers?symbol=&from=&to=`, `GET /research/watchlists/{id}/universe` (read-only) — archive mr8-notes.md |

### 4.3 WebSocket `/research/ws`

- `heartbeat` 20 s, client messages ≤ 16 KiB, ≤ 4 clients (more → close 1013), ≤ 64 subscriptions; 5 bad messages in
  a row → close 1008. Client → server: `hello {visible}` (within 10 s), `visible`, `subscribe` / `unsubscribe
  {topics}`; topics `trades:<SYM>`, `candle:<SYM>:<tf>:<session>`, `news:<SYM>`, `alerts`, `feeds`,
  `watchlist:<id>`.
- Server → client: `feeds` (on connect + changes, ≤ 1/s), `trade` (coalesced, ≤ 4/s per symbol), `candle` (≤ 4/s,
  `final: true` closes), `sip_replace` (IEX tail → SIP), `symbol_status {live, reason}`, `news`, `alert` (+ `kind`,
  `title`, `price`, `level`, `log_id`, `logged`), `error {code, message}`. Frames ≤ 1 MiB; bar lists chunked ≤ 500
  bars with `part` / `parts`; oversized → `message_too_large` error. Send queue ≤ 500; trades / candles coalesced;
  `feeds` / `alert` / `news` never dropped (connection closed 1013 instead). A connection is a view while `visible`.
  Exact messages → archive mr2-notes.md (MR2b).

### 4.3.1 WPF client on `/research/ws`

- One socket per app, open only while the section / pop-out is visible, close ≤ 5 s after hidden. Handshake: `hello`
  → first `feeds` (≤ 10 s) → ONE `subscribe` with all topics → re-fetch bars for open charts.
- Reconnect backoff 1, 2, 5, 10, 30 s then every 30 s (reset after 60 s connected), `WAITING · attempt n`, live values
  marked stale. Close codes: 1013 → backoff from 5 s; 1008 / 1009 → protocol error message, one retry after 60 s; 401
  → re-read token once; 503 → banner, retry 30 s; 404 → "needs a newer backend".

### 4.4 Constants (`limits.py`)

| name | value |
|---|---|
| `RESEARCH_MAX_PER_MINUTE` | 60 (Q3) |
| `RESEARCH_RESERVE_TOKENS` | 3 (shared-bucket tokens Research never takes) |
| `RESEARCH_ACQUIRE_TIMEOUT_SECONDS` | 30 (review H1: a waiting Research call occupies a Research thread) |
| `RESEARCH_EXECUTOR_WORKERS` | 4 (the Research thread pool, §5.1) |
| `STOCK_STREAM_SYMBOL_LIMIT` | 30 |
| `HUB_LINGER_SECONDS` | 60 (streams close this long after demand drops to 0) |
| `RECONNECT_BACKOFF_SECONDS` | (2, 5, 10, 30, 60), then 60 forever while demand > 0 |
| `STREAM_SILENCE_SECONDS` | 90 (no frame incl. pings → reconnect) |
| `WS_MAX_CLIENTS` / `WS_MAX_MESSAGE_BYTES` (client → server) / `WS_MAX_SUBSCRIPTIONS` / `WS_HEARTBEAT_SECONDS` | 4 / 16 384 / 64 / 20 |
| `WS_MAX_SERVER_MESSAGE_BYTES` / `WS_MAX_BARS_PER_MESSAGE` | 1 048 576 / 500 (§4.3) |
| `LIVE_CANDLE_MAX_PER_SECOND` | 4 |
| `SIP_DELAY_MINUTES` | 15 (= `providers.alpaca.SIP_DELAY`) |
| `SIP_TAIL_REFRESH_SECONDS` / `SIP_TAIL_MAX_SYMBOLS` | 60 / 8 (symbols with an open intraday chart) |
| `MAX_BARS_PER_REQUEST` | 5000 intraday, 2000 daily/weekly |
| `RETENTION_DAYS` | `bars_1m` 30, `news` 7, `logos` 30, `alert_log` 90, `audit` 365 |
| `ASSET_REFRESH_HOURS` | 24 |
| `MAX_WATCHLISTS` / `MAX_WATCHLIST_ITEMS` / `MAX_NAME_CHARS` | 20 / 200 / 40 |
| `MAX_TABS` | 12 |
| MR4: `MAX_RECENT_SYMBOLS` / `MAX_WORKSPACE_BODY_BYTES` / `QUOTE_LOOKUP_BUDGET_SECONDS` / `QUOTE_RETRY_SECONDS` / `PREV_CLOSE_CACHE_SECONDS` / `PREV_CLOSE_CACHE_MAX` / `WORKSPACE_RELOAD_SECONDS` | 20 / 32 768 / 15 / 5 / 3600 / 2000 / 30 (+ `SESSION_SCHEMA` 1, `TAB_TYPES`, `SIDEBAR_SECTIONS`) |
| `MAX_DRAWINGS_PER_SYMBOL` / `MAX_DRAWING_POINTS` / `MAX_BRUSH_POINTS` / `MAX_NOTE_CHARS` / `MAX_DRAWING_JSON_BYTES` | 500 / 10 / 500 / 280 / 16 384 |
| `MAX_IMPORT_BYTES` | 262 144 |
| `MAX_ALERTS` / `ALERT_COOLDOWN_SECONDS` (default, range) | 200 / 900 (60–86 400) |
| MR7: `ALERT_DAY_MOVE_PCT` / `ALERT_MAX_LEVEL` / `ALERT_RELOAD_SECONDS` / `ALERT_PREV_CLOSE_RETRY_SECONDS` / `ALERT_NEWS_IDS_KEPT` / `ALERT_FIRE_QUEUE_MAX` / `ALERT_LOG_LIMIT_DEFAULT` / `ALERT_LOG_LIMIT_MAX` / `MAX_ALERT_BODY_BYTES` | 0.1–50 / 1e9 / 5 / 60 / 50 / 1000 / 100 / 500 / 4096 |
| `MAX_LOGO_BYTES` | 262 144 |
| MR6: `NEWS_LIMIT_DEFAULT` / `NEWS_LIMIT_MAX` / `NEWS_REFRESH_SECONDS` / `MAX_NEWS_HEADLINE_CHARS` / `MAX_NEWS_SUMMARY_CHARS` / `MAX_NEWS_PUBLISHER_CHARS` / `MAX_NEWS_SYMBOLS` | 20 / 50 / 60 / 300 / 500 / 100 / 50 |
| MR6: `MAX_NEWS_IMAGE_BYTES` / `NEWS_IMAGE_TYPES` / `MAX_IMAGE_SIDE` / `NEWS_IMAGE_TIMEOUT_SECONDS` / `NEWS_IMAGE_TOTAL_SECONDS` / `NEWS_IMAGE_MAX_REDIRECTS` / `NEWS_IMAGE_RETRY_SECONDS` / `NEWS_IMAGES_PER_MINUTE` | 524 288 / png, jpeg, gif (no WebP, review L6) / 4096 px / 10 / 20 / 3 / 900 / 60 |
| MR6: `LOGO_NONE_DAYS` / `LOGO_PLAN_RECHECK_SECONDS` / `MAX_LOGO_SIDE` / `NEWS_SEEN_MAX` / `NEWS_STREAM_QUEUE_MAX` | 7 / 86 400 / 1024 px / 5000 / 1000 |
| `MAX_TEXT_CHARS` (reasons/messages in responses) | 300 |
| `STATUS_POLL_SECONDS` (WPF) | 5 |
| supervisor: `RESTART_DELAYS_SECONDS` / `MAX_RESTARTS` / `RESTART_WINDOW_SECONDS` / `HEALTHY_AFTER_SECONDS` / `SHUTDOWN_TIMEOUT_SECONDS` | (5, 15, 30, 60, 120) / 5 / 900 / 900 / 2.0 |
| `MAINTENANCE_INTERVAL_SECONDS` | 86 400 (prune), store re-open attempt every 60 s while unavailable |

## 5. Supervisor + thread pool

- `ResearchSupervisor` / `RestartPolicy` (`supervisor.py`): `add(name, label, factory)`, `start()` (never raises),
  `stop(timeout)` (≤ 2 s, never raises), `status()`; each task in `_guard` — any exception except `CancelledError` /
  `KeyboardInterrupt` (also `SystemExit` from Research code) → restart policy (§1.4). One process-wide service
  (`market_research.install(app)` / `service()`).
- **§5.1 Research thread pool** (`executor.py`): ALL blocking Research work runs via `await
  executor.run_blocking(fn, …)` (4 threads, `research` prefix, contextvars copied so `research_priority()` applies).
  **Forbidden in `market_research/`:** `asyncio.to_thread`, `run_in_executor(None, …)`, own executors (the default
  executor is shared with the engine's kill / dead-man paths) — AST test in `tests/test_mr_shell.py`.

## 6. Storage (`store.py`, `backend/data/market_research.db`)

- Same robustness rules as hedgefund.db: WAL, `synchronous=NORMAL`, connection per call, append-only `MIGRATIONS`
  (one transaction per version); `user_version` read FIRST (newer schema → `StoreUnavailable`, file untouched);
  corrupt file quarantined all-or-nothing to `market_research.db.corrupt-<UTC>` and recreated; locked → re-open ≤ every
  30 s. Errors → `StoreUnavailable` "The Research database (market_research.db) is unavailable: <reason>. Research is
  paused — trading is not affected."
- Versions: **v1** `meta`, `audit`; **v2** `assets`, `bars_1m` (feed sip / iex / crypto), `bars_1m_days` (coverage);
  **v3** `watchlists`, `watchlist_items`, `session`, `recent_symbols`; **v4** `drawings` (split basis + rescales),
  `drawing_rescales`, `chart_settings`; **v5** `news`, `news_symbols`, `news_images`, `logos`; **v6**
  `drawings.client_id` (UNIQUE, idempotent POST / import); **v7** `alerts`, `alert_log`. Next free version: **v8**
  (stages landing out of order take the next free number).
- Retention (daily prune): `bars_1m` 30 d, `news` (+ symbols, images) 7 d, `logos` 30 d, `alert_log` 90 d, `audit`
  365 d; watchlists, session, drawings, chart settings, alerts until deleted.

## 7. Data rules

- **§7.1 Intraday model c:** `sip_through` = now − 15 min (floored); 1m before it from SIP (stored, final); the tail
  from IEX (REST at load, then hub trades), `feed: "iex_tail", final: false`, drawn with lower opacity + chip;
  every 60 s newly released SIP minutes replace the tail (`sip_replace`, ≤ 8 symbols with an open intraday chart); a
  SIP failure leaves tail candles marked as tail. Crypto: one feed, no tail. Header: last = IEX trade; volume = Σ SIP
  RTH volume to `sip_through` ("SIP · 15 min delayed"); day range SIP + tail; prev close SIP daily.
- **§7.2 Aggregation:** 5m / 15m / 1H / 4H from 1m in New York buckets (`rth` anchored 09:30, `eth` 04:00; early close
  ends RTH 13:00); crypto UTC floor; o / h / l / c / v / vwap / n, empty minutes absent; a bucket with a tail minute
  is `iex_tail`. 1D from the hedgefund.db cache (adjustment `all`), today from 1m; crypto 1D = UTC day; 1W Mon–Fri.
  Initial windows 1m 2 sessions, 5m 5, 15m 10, 1H 22, 4H 6 months, 1D 2 years, 1W since 2016; intraday older than 30
  days → 400 "Intraday history older than 30 days is not loaded in Research." (1H / 4H beyond: SIP 1m fetched per
  request, not stored, ≤ 130 sessions); `HISTORY_START` 2016-01-01.
- **§7.3 Market status** from `core/calendars.py`: pre-market 04:00–09:30, after hours 16:00–20:00 ET (early close
  13:00–17:00); outside the covered years (2000–2030) → 503 `CalendarUnavailable`. Crypto always open, UTC day.
- **§7.4 Statistics (binding):** last (IEX / crypto), change / change_pct (computed), prev_close (last completed
  session, split-adjusted, `sip_daily`), day range (RTH; crypto UTC day), volume (§7.1), open (first RTH minute;
  `iex_tail` before 09:45), vwap (RTH 1m), avg_volume_30d, performance 1W / 1M / 3M / 6M / 1Y / YTD (split-adjusted,
  dividends excluded — note), 52-week range. Market closed → last session values with `as_of` = that close ("at
  close").
- **§7.5 Alpaca budget + priority** (`http_gate.research_priority()` contextvar): research bucket 60/min (capacity 5);
  a shared token only while ≥ 3 remain and no normal caller waits; hard cap 60 sends per sliding 60 s; a waiting
  research call yields (`ResearchYield`); > 30 s → "Research waited too long for the Alpaca rate limit (shared with
  trading)". `budget.research_used_last_minute` / `waiting_seconds` measured.
- **§7.6 Market-data hub** (`hub/`, supervised task `market_data_hub`): the ONLY owner of Alpaca streams (stocks IEX,
  crypto v1beta3, news v1beta1; keys at every connect, never logged); subscriptions = union of WS topics + active
  alerts; stocks ≤ 30 symbols (priority: tabs, watchlists, alerts) — the rest `symbol_status` "Stream limit: 30
  symbols — X is not live."; states off → connecting → live, loss → `waiting` with backoff (2, 5, 10, 30, 60 s) and
  `network.probe` under research priority from attempt 2; 406 → `unavailable` "…Alpaca allows one stream connection
  per account and another program is using it."; auth / plan / missing keys → `unavailable` with re-checks (missing
  keys 60 s; others 5 min while demand > 0; at every demand change). Streams open only while demanded, close 60 s
  after.

## 8. Failure states (no fallbacks)

No / rejected keys → 503 `SecretMissing` / `DataSourceUnavailable` + feeds `unavailable` ("Alpaca keys missing /
rejected — enter them in Hedge Fund → Keys & data."); rate limit → "Waiting for Alpaca rate limit (n s)"; stream 406
/ lost → `unavailable` / `waiting (attempt n)`; beyond 30 symbols → not live; crypto not on Alpaca → 404
`SymbolNotCovered`; market closed → values "at close" (not an error); SIP refused → 503 `SIP_PLAN_MESSAGE`; news →
503 "News unavailable: <reason>"; logo → 404 `NoLogo` / `LogosNotOnPlan` (initials shown); thumbnail → 409
`ThumbnailsOff` / 503 `ThumbnailUnavailable`; chart page error → bridge `error {code, message}`; task crash / DB
unavailable / package failed → `research.state unavailable` / 503 `ResearchUnavailable`; calendar year not covered →
503 `CalendarUnavailable`. Full table → archive §8.

## 9. Security

- Routes + WS behind `api_auth`, loopback 8867, Host-header check (strategy_platform SPEC §23.5), no CORS. Input:
  symbol regexes, server-assigned 32-hex ids, enums, finite ranged numbers, §4.4 lengths, unknown fields → 400.
- External text (asset names, news, publisher) stored as sanitised plain text, rendered as plain text / escaped; URLs
  only http(s), opened in the default browser; images only https, ≤ 512 KB, png / jpeg / gif, fetched ONLY by the
  backend proxy (`media.py`: public hosts, checked redirects, timeouts), decoded by WPF with a pixel cap.
- **Chart page:** local files via virtual host `chart.ufos.local` → `HedgeFund/MarketResearch/Chart`; CSP
  `default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'none';
  font-src 'self'; base-uri 'none'; form-action 'none'`; no network, no token in the page, navigation / new windows
  blocked, host objects off, dev tools off in Release; every bridge message schema-checked (≤ 4 MB host → page, ≤ 64
  KB page → host); no `eval`; vendored engine verified against MANIFEST sha256 at load ("engine file modified").
- Residual risks (told to the user): token file readable by the same Windows user; thumbnails reveal the IP (switch);
  plain ws on loopback.

## 10. Stages — deliverables (all MR1–MR8 built 2026-10-16 … 10-18)

Deliverables, acceptance tests and the binding **bridge contracts** (v1 for MR3; revision 2 drawings + indicators for
MR5; revision 4 `setMarkers` for MR8): → spec-archive/10-stages-and-bridge-contracts.md (+ the MR3 / MR5 / MR8 chart
notes). In force:
- **MR1** shell (rail entry "Research", pop-out 1440×900 borderless, status bar); **MR2** data layer + hub + WS;
  **MR3** chart (KLineChart 10.0.3 vendored via `Chart/tests/fetch_engine.py`, integrity pin = registry = tarball;
  bridge messages `{type, v, …}`, bars `{t: epoch ms, o, h, l, c, v, feed, final, s?}`; real-engine run 128 / 128);
  **MR4** symbol workspace (header, key stats, watchlists, tabs + session restore, Ctrl+K palette); **MR5** drawing
  tools (incl. custom `rect`, `measure`, `alertLine`; undo / redo 50; split rescaling logged; export / import JSON ≤
  256 KB, ids re-assigned) + indicators via the DSL (SMA, Bollinger = sma ± k·ts_std (sample std), RSI Wilder, EMA via
  `ewm_mean` half-life, MACD 12 / 26 / 9, session VWAP, compare overlay; ≤ 8 series, 3 sub-panes); **MR6** news +
  logos (7-day cache, logos 30 d, negative 7 d; logos are a paid add-on → `LogosNotOnPlan` on the free plan); **MR7**
  alerts `price_cross`, `line_cross`, `day_move`, `news` (cooldown + de-dup, hub-evaluated, toasts; "Alerts work only
  while UFOS.ai runs."); **MR8** read-only Portfolio + Macro, Buy / Sell hand-over (Trading order dialog,
  `/v2/oms/orders`), insider / calendar / backtest markers, "Use watchlist as universe".
- **Full screen** (SP §25.1): `ResearchView.xaml` `ChartFullScreenButton`, target `ChartCard` (same WebView2, drawings /
  zoom / timeframe kept), also in the pop-out window; replacing the view exits full screen first.
- **MR8 Portfolio data source:** `integrations.portfolio()` reads the OLD engine's paper account in newsroom.db
  (marks note "Prices are the HedgeFund engine's last marks"). After the §17.9 migration that account is frozen —
  open item in UFOS.ai/docs/todo.md (switch to `paper-council` / platform accounts).

## 11. Open points / verify

- Alpaca stream paths, news-stream availability and stream `images` on the free plan, assets ETF flag (verify live).
- Extended-hours end on early-close days (17:00 ET assumed).
- Stage-10 cleanup candidates: `Chart/chart.js.20261018-precrossfix.stale`, `vendor/*.stale` (copied to the output by
  the `MarketResearch\Chart\**` glob).

## 12. Extensions of `strategy_platform` code (record here AND in its SPEC)

| stage | extension |
|---|---|
| MR2 | `data/http_gate.py`: `research_priority()`, research bucket / reserve, hard 60 / 60 s cap, slot rule, `note_send()`, `ResearchYield` + `research_yield_check()`, `is_research_priority()`, `research_stats()`, `TokenBucket.try_acquire_above()` |
| MR2 | `data/network.py`: `AdaptiveLimiter.try_acquire()`; `data/cache.py`: series lock `_held(key)` (research holder yields, `get_bars` retries ≤ 50×) |
| MR2 | `data/providers/alpaca.py`: `assets()`, `harden_client(on_send=)`, timeframe `"30Min"` (Research only, not cached) |
| MR5 | none — indicators call `strategy/evaluate.evaluate_node` + `strategy/panel.Panel`; splits via `AlpacaProvider.corporate_actions` |
| MR6 | `data/providers/alpaca.py`: news `images` (https, ≤ 5), `logo(symbol)`, `LogosNotAvailable`, `load_requests()`, `LOGO_*` / `PNG_SIGNATURE` |
| MR7 | none (hub `trade_hook`, `stats.quote.prev_close`, drawings read-only) |
| MR8 | `backtest/runcard.py`: `fills_by_symbol` (≤ 10 000 per card, `fills_truncated`) on NEW cards |

## 13. Stage notes (archived)

Each stage's notes (files, exact JSON binding for the WPF, deviations, review fixes, tests) are kept verbatim:
- MR1 → spec-archive/mr1-notes.md · MR2a + MR2b → spec-archive/mr2-notes.md · MR3a + MR3b + the real-engine check
  2026-10-18 → spec-archive/mr3-notes.md · MR4 backend + WPF → spec-archive/mr4-notes.md · MR5 backend + WPF + chart
  → spec-archive/mr5-notes.md · MR6 → spec-archive/mr6-notes.md · MR7 → spec-archive/mr7-notes.md · MR8 backend +
  WPF + chart → spec-archive/mr8-notes.md · live-test fix 2026-10-18 (optional query parameters) →
  spec-archive/live-test-fix-2026-10-18.md
- A new stage appends its notes to a NEW archive file and updates the current sections above.
