# Market Research SPEC — archive: 00-mr0-contract-sections-0-9

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 0. Ground rules (all stages)

- **Scope:** only `UFOS.ai/HedgeFund/**` changes (plus `docs/market-research-plan.md`,
  `docs/todo.md`, `UFOS.ai/docs/todo.md` when a task says so). No MainWindow
  change, no new port (backend 8867), no new venv, no new MainWindow tile.
  Scope guard after every stage:
  `python HedgeFund/tools/scope_guard.py check --label stage0` → "RESULT: ok".
- **Package:** `HedgeFund/backend/market_research/` (import name
  `market_research`). NOT inside `strategy_platform/` (its planned
  `research/` package is Strategy Studio — a different thing).
- **No unrequested fallbacks:** a missing key / feed / source / limit stops
  the affected Research function with a clear message (reason + what to do).
  No second source, no snapshot instead of a stream, no cached value shown as
  current, no default value.
- **Don't disturb the trading engine:** every Research task runs under the
  Research supervisor (§1.4); a Research failure marks Research
  "unavailable" with the reason and never stops the paper engine, kill
  switch, dead-man switch, newsroom collectors or Strategy Platform jobs.
  Research never places orders.
- **Security:** loopback, `api_auth` token on every route and on the
  WebSocket, strict input validation (unknown fields/params → 400), size
  limits, external text (news, asset names) is untrusted plain text, no stack
  traces or secrets in responses or logs. Keys only via
  `strategy_platform/secrets.py` (backend) / `Platform/CredentialStore.cs`
  (WPF), names `alpaca-paper-key-id` / `alpaca-paper-secret-key` (D-MR2).
- **Python:** runs on 3.14 (Windows venv); offline tests on 3.10 stdlib
  `unittest` (+ numpy/pandas available, no scipy, **no aiohttp, no
  websockets** on the test VM → lazy imports inside functions, stub them in
  tests like `tests/test_sp_api.py`). Temp dirs outside the repo, fakes, no
  network. Tests: `HedgeFund/backend/tests/test_mr_*.py`.
- **Time:** UTC ISO-8601 strings with seconds (`2026-10-16T12:00:00+00:00`)
  in EVERY HTTP response and EVERY `/research/ws` message — bar `t`
  included (bar start). The ONLY exception is the chart bridge (WPF ↔ chart
  page, §10 MR3): there `t` is epoch milliseconds UTC (what KLineChart
  takes); the conversion ISO → epoch ms (and back for `needMoreLeft`,
  `crosshair`, drawing points) happens in exactly one place,
  `MarketResearch/Chart/ChartHost.cs`. Drawings are stored/sent by the
  backend with ISO `t` as well. Market logic in `America/New_York`
  (`strategy_platform/core/calendars.py`); the WPF converts to local time
  for display.
- **UI text English.** AI labelling rules apply to any AI output (none in v1).
- **Reuse, don't duplicate** (`strategy_platform` data layer, `http_gate`,
  `network`, `cache`, `core/calendars`, `core/clock`, `core/instruments`,
  `api_auth`, `routes.dumps/read_body/user_error`). Every extension of
  `strategy_platform` code is recorded in §12 here AND in
  `strategy_platform/SPEC.md`.

## 1. MR1 contract — WPF ↔ backend surface (final, 2026-10-16)

MR1 delivers the section shell. The WPF part (rail entry "Research",
`ResearchView` skeleton with empty states, pop-out window) talks to the
backend through exactly ONE route in MR1: `GET /research/feeds/status`.

### 1.1 Transport

- Base URL = the existing HedgeFund backend HTTP API `http://127.0.0.1:8867`
  (same client/token handling as `Platform/PlatformClient.cs`:
  header `X-UFOS-Token` from `backend/data/api-token` via
  `Shared/BackendAuth.cs`; re-read the token once on a 401 and retry once).
- All Research routes live under `/research/*`. The live WebSocket (from
  MR2) is `ws://127.0.0.1:8867/research/ws` — same aiohttp server, same
  port, token ONLY in the `X-UFOS-Token` request header
  (`ClientWebSocket.Options.SetRequestHeader`); a `?token=` query is NOT
  accepted (the HTTP middleware ignores query tokens — keeps the token out
  of URLs/logs).
- JSON: UTF-8, strict (no NaN; `routes.dumps`). Unknown JSON fields must be
  ignored by the WPF DTOs (forward compatible); computed DTO properties get
  `[JsonIgnore]` (PropertyNameCaseInsensitive collision bug).

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

| Situation | Response | WPF shows (Research section only; the rest of the HedgeFund window keeps working) |
|---|---|---|
| backend not reachable (connection refused / timeout 5 s) | — | "Research unavailable: the HedgeFund backend is not reachable." + Retry; polling continues |
| token missing/invalid | 401 `{"error": "missing or invalid API token"}` (api_auth) | re-read token once and retry; still 401 → "Research unavailable: the backend rejected the app's token — restart the HedgeFund backend." |
| Research package failed to import/install | 503 `{"error": "Market Research failed to load — see backend.log. Trading is not affected.", "kind": "ResearchUnavailable"}` for every `/research/*` path (stub route in main.py) | "Research unavailable: " + `error` |
| unexpected error inside a Research route | 500 `{"error": "internal error in Market Research (see backend.log)"}` | "Research unavailable: internal error (see backend.log)." |
| query parameter / bad input | 400 `{"error": "invalid input", "errors": {field: message}}` | client bug — log it, show `error` |
| old backend without the route | 404 | "Research needs a newer backend — restart the HedgeFund backend." (normally the stale-backend check already replaced it) |
| 200 with `research.state == "unavailable"` | — | banner "Research unavailable: " + `research.reason` (feeds still listed) |
| 200 with `research.state == "starting"` | — | "Research is starting…" |

### 1.4 Isolation (what the WPF can rely on)

- Research runs as separately supervised asyncio tasks (`supervisor.py`):
  each task has its own error boundary; an exception is logged
  (backend.log, with stack), recorded as the task's `last_error`, turns
  `research.state` to `unavailable` with the reason and NEVER propagates to
  main.py, the paper engine or the other tasks.
- Restart policy (bounded + visible): a crashed task restarts after
  5 s, 15 s, 30 s, 60 s, 120 s (attempt 1–5; reason `Research task "<name>"
  crashed (<last_error>) — restarting in <n> s (attempt <k> of 5). Trading is
  not affected.`). The 6th crash within a rolling 15 min (i.e. more than
  `max_restarts` = 5) → `failed`, reason
  `Research task "<name>" stopped after 6 crash(es) (<last_error>) — restart the HedgeFund backend. Trading is not affected.`
  A run that lasted ≥ 15 min clears the crash history. A task that ends
  while Research is stopping is never restarted.
- A task that RETURNS (instead of looping) is treated like a crash with
  reason `Research task "<name>" ended unexpectedly`.
- Startup/shutdown cannot block or crash the backend: installing the
  routes is wrapped in try/except in main.py (failure → the 503 stub
  above); `start()` only creates tasks (no I/O, no await on Research work);
  the store is opened INSIDE the supervised maintenance task (in a
  Research pool thread, §5.1 — never the default executor). Shutdown (`POST /shutdown`) gives Research at most 2 s to cancel
  its tasks, then exits as before.

### 1.5 WPF behaviour in MR1 (for the WPF agent)

- Poll `GET /research/feeds/status` every 5 s **only while the Research
  section or the pop-out window is visible** (stop when hidden; first poll
  immediately on show). From MR2 on, the same payload also arrives over
  `/research/ws` as message `{"type": "feeds", "data": <payload>}`; the WPF
  then polls only while the WebSocket is down.
- Status bar (sidebar bottom, plan §2.2): `●●● LIVE · <live_count>/<feed_count>`
  — one dot per feed coloured by state (`live` = `PositiveGreenBrush`,
  `connecting`/`waiting` = `CautionBrush`, `off` = `MutedForegroundBrush`,
  `unavailable` = `AccentRedBrush`); text never relies on colour alone
  (state word in the tooltip, e.g. "Stocks · IEX · real time — off: Live
  data is not built yet (stage MR2)."). When no feed is live the text reads
  `OFF · 0/3` (or `WAITING` / `UNAVAILABLE` when any feed is in that state,
  precedence unavailable > waiting > connecting > off).
- The `demand.note` text is shown under the status bar tooltip / settings
  (Q3 requires the UI to state when streams run).
- All strings from the payload are plain text (TextBlock, no markup).

## 2. Decisions

**By the user:**

| id | decision | date / where |
|---|---|---|
| D-MR1 | Research is a section of the HedgeFund module (rail entry "Research" + pop-out window), not a separate module | 2026-10-15, plan §12 |
| D-MR2 | Alpaca keys = the existing HedgeFund Credential Manager entries (`alpaca-paper-key-id`, `alpaca-paper-secret-key`); no second key set | 2026-10-15 |
| D-MR3 | No MainWindow tile, no new port (backend 8867), no new venv | 2026-10-15 |
| Q0 | Order: MR0–MR8 before Strategy Platform stage 5 | user confirmed 2026-10-16 ("Mach das autonom, während ich weg bin") |

**Plan recommendations taken — confirmed by the user 2026-10-24:**

| id | decision |
|---|---|
| Q1 | Chart engine **KLineChart 10.x** (Apache-2.0) in WebView2, vendored (exact pinned version, LICENSE, MANIFEST with sha256), behind the bridge contract (plan §3.3, §10 MR3), CSP without network. — confirmed by the user 2026-10-24 |
| Q2 | Intraday model **c**: SIP history + IEX live tail (≤ 15 min, drawn differently, replaced by SIP when available). Header volume = SIP session volume up to 15 min ago, labelled "SIP · 15 min delayed"; every number carries its feed in the tooltip (§7.1). — confirmed by the user 2026-10-24 |
| Q3 | Alpaca budget: the ONE shared `http_gate` stays **180 req/min** (not the plan's 200; **lowered to 150 req/min + burst 10 on 2026-10-23** — fixed split with the Options Structure archive's own 30/min, strategy_platform SPEC §21.1); Research may use **at most 60 req/min** of it and always yields to trading-critical / platform requests (priority, §7.5). Hub streams run only while a Research view is visible or an alert is active (stated in the UI). — confirmed by the user 2026-10-24 |
| Q4 | Buy/Sell = hand-over to the existing manual paper-order flow, pre-filled (MR8) — never live, Research never sends an order itself. — confirmed by the user 2026-10-24 |
| Q5 | Crypto: Alpaca coins only; others → "Not available from Alpaca (the configured crypto source)." — confirmed by the user 2026-10-24 |
| Q7 | Sidebar: Overview + Symbol + Alerts in MR1–MR7; Portfolio (paper account, read-only) + Macro (Newsroom) in MR8; Markets/Metrics later. — confirmed by the user 2026-10-24 |
| Q8 | Indicators: backend definitions shared with the Strategy Platform DSL operators (§10 MR5). — confirmed by the user 2026-10-24 |
| Q9 | Crypto "today" = **UTC day** (consistent with the Strategy Platform's crypto UTC days). — confirmed by the user 2026-10-24 |
| Q10 | Beyond the 30-symbol stream limit: clear message only, no snapshot fallback. — confirmed by the user 2026-10-24 |
| Q11 | AI features: none in v1. — confirmed by the user 2026-10-24 |
| Q12 | 1m bar retention: 30 days. — confirmed by the user 2026-10-24 |

**Implementation choices made in MR0 (not in the plan) — confirmed by the user 2026-10-24:**

- C1 Research's 1m bars live in `market_research.db` (`bars_1m`, 30-day
  retention); daily bars come from the existing hedgefund.db bar cache
  (kept, no retention) — a 30-day prune inside hedgefund.db would touch
  Strategy Platform data.
- C2 Session/market status from `core/calendars.py` (hardcoded NYSE
  calendar 2016–2027 + rule years), not from Alpaca's clock/calendar API
  (reuse; no extra requests). Outside the covered years →
  `CalendarUnavailable` message.
- C3 Research WebSocket on the existing aiohttp server (8867,
  `/research/ws`), header token only.
- C4 News thumbnails shown by default (setting "Show news thumbnails",
  off = no third-party image requests; residual risk: image hosts see the
  IP).
- C5 Audit rows in `market_research.db` kept 365 days.

**Plan fixes (found 2026-10-16):**

- F1 DB path = `HedgeFund/backend/data/market_research.db` (the plan's §5.6
  still had the standalone path `MarketResearch/backend/data/…`).
- F2 The hub needs reconnect with backoff + a visible "Waiting for
  connection (attempt n)" state + reuse of `network.probe` (weak Wi-Fi, like
  Strategy Platform SPEC §10).
- F3 The WebView2 PackageReference must be added to
  `HedgeFund/MultiAgentHedgeFund.csproj` (version **1.0.4191.47**, the one
  Global Monitoring and the main app use; the csproj name stays).
- F4 The plan's "200 req/min" budget is replaced by the existing 180/min
  gate (Q3).
- MR8 may reuse the Strategy Platform §11 attribution (periods, drawdowns)
  for chart markers.

## 3. Package layout (grows per stage)

```
HedgeFund/backend/market_research/
  SPEC.md          this contract
  __init__.py      MR1: install(app) -> ResearchService; ResearchService.start(), .shutdown(timeout)
  limits.py        MR1: all constants of §4.4 (one place)
  supervisor.py    MR1: ResearchSupervisor, RestartPolicy (§5)
  store.py         MR1: market_research.db (§6)
  feeds.py         MR1: feed status (MR1: static "off"; MR2: delegates to hub)
  maintenance.py   MR1: supervised task "store_maintenance" (open store, retry 60 s, daily prune)
  executor.py      MR1: the Research thread pool (§5.1) — the only way to run blocking work
  api.py           MR1: /research/* routes (§4), MR2+: WebSocket handler
  assets.py        MR2: asset list refresh + search
  market.py        MR2: market status (core/calendars)
  bars/            MR2: aggregate.py (5m/15m/1H/4H/1W), intraday.py (model c), history paging
  stats/           MR2: snapshot.py (header), keystats.py (key stats, performance, 52W)
  hub/             MR2: hub.py (MarketDataHub), alpaca_stream.py (one connection per endpoint), demand.py
  watchlists.py    MR4 (lists + hub expansion)   session.py MR4 (session, recents, palette)
  workspace_api.py MR4 routes   stats/quote.py MR4 (watchlist quotes)
  drawings.py      MR5   indicators.py MR5   chart_settings.py MR5   drawings_api.py MR5 (routes)
  news.py logos.py MR6   news_text.py (sanitiser) media.py (image checks + thumbnail proxy) news_api.py MR6 (routes)
  alerts.py        MR7 (rules, CRUD, AlertEngine)   alerts_api.py MR7 (routes)
  integrations.py  MR8 (read-only views)   integrations_api.py MR8 (routes)
HedgeFund/MarketResearch/          WPF, namespace UFOS.ai.HedgeFund.MarketResearch
  ResearchView.xaml(.cs)           MR1 skeleton → MR4 full
  ResearchPopoutWindow.xaml(.cs)   MR1 (borderless, full-screen + drag-to-restore rules)
  ResearchClient.cs                MR1 (HTTP), MR3 (+ MR2a data routes)
  ResearchSocket.cs                MR3 (the one /research/ws client, §4.3.1)
  Chart/ ChartHost.cs, chart.html/.js/.css, vendor/  MR3
  WatchlistPanel, CommandPalette, AlertsView, DrawingObjectsPanel, ResearchSettingsDialog  MR4–MR7
HedgeFund/backend/tests/test_mr_*.py
```

## 4. API (all under `/research/*`, behind `api_auth`)

### 4.1 Conventions

- Handlers like `strategy_platform/api/routes.py`: `ResearchApi` methods
  `(query, body, match) -> (status, payload)`, a `ROUTES` table, and
  `register(app, service)`; JSON via `routes.dumps`, bodies via
  `routes.read_body` (reuse). Body size: aiohttp `client_max_size` of
  main.py (≥ 1 MB); Research checks its own limits (§4.4) and answers 400
  above them.
- Errors: invalid input → 400 `{error: "invalid input", errors: {field: msg}}`
  (unknown query params / body fields included); unknown id/symbol → 404
  `{error}`; conflicts → 409 `{error}`; `InvalidRequest` → 400,
  other `UserFacingError` → 503 `{error: user_message, kind}`; anything
  else → 500 `{error: "internal error in Market Research (see backend.log)"}`.
- Symbols in paths are platform symbols (`core/instruments`): stocks/ETFs
  as on the exchange (`NVDA`, `BRK.B`), crypto `BASE-USD` (`BTC-USD`),
  case-insensitive, normalised upper-case; anything else → 400. A crypto
  symbol not in Alpaca's asset list → 404 `{error: "Not available from
  Alpaca (the configured crypto source).", kind: "SymbolNotCovered"}`.
- Timeframes `1m 5m 15m 1H 4H 1D 1W`; sessions `rth` (default) / `eth`
  (stocks only; crypto ignores it → 400 if given for crypto).
- Times: query/body ISO-8601 with offset (naive = UTC); responses UTC ISO.
- Value shape for header/stat numbers (no fallback values — a missing
  value is `null` WITH the reason):
  `Num = {value: number|null, feed: str, feed_label: str, as_of: iso|null, reason: str|null}`.
  `feed` ∈ `iex` (IEX · real time), `iex_tail` (IEX live tail), `sip_delayed`
  (SIP · 15 min delayed), `sip_daily` (SIP · daily bars), `crypto`
  (Alpaca crypto · real time), `computed` (derived; `feed_label` names the
  inputs).

### 4.2 Routes per stage

| stage | method + path | result |
|---|---|---|
| MR1 | `GET /research/feeds/status` | §1.2 |
| MR2 | `GET /research/assets?q=&limit=` | `{items: [{symbol, name, asset_class: equity\|crypto, kind: stock\|etf\|crypto, exchange, tradable}], refreshed_at, source: "alpaca"}`; q 1–32 chars (letters, digits, space, `.-/`), limit 1–50 (default 20); order: exact symbol, symbol prefix, name word prefix, name substring; table empty and refresh failing → 503 reason |
| MR2 | `GET /research/market/status` | `{equities: {state: open\|pre_market\|after_hours\|closed\|holiday, session_date, opens_at, closes_at, early_close, next_open_at, next_close_at}, crypto: {state: "open", day_start, day_end}, generated_at}` (§7.3) |
| MR2 | `GET /research/symbols/{s}/snapshot` | header (§7.4): `{symbol, name, exchange, asset_class, kind, last: Num, prev_close: Num, change: Num, change_pct: Num, day_low: Num, day_high: Num, volume: Num, market: <equities or crypto part of market/status>}` |
| MR2 | `GET /research/symbols/{s}/bars?tf=&session=&before=&limit=` | `{symbol, tf, session, bars: [{t (bar start, UTC ISO string), o, h, l, c, v, vwap, n, feed: sip\|iex_tail\|sip_daily\|crypto, final}], has_more_left, sip_through, tz: "America/New_York"\|"UTC"}` oldest first; `before` exclusive (paging left); limit default = §7.2 initial window, max `MAX_BARS_PER_REQUEST` |
| MR2 | `GET /research/symbols/{s}/stats` | `{open: Num, vwap: Num, avg_volume_30d: Num, exchange, performance: {"1W","1M","3M","6M","YTD","1Y": {pct: Num, base_date}}, range_52w: {low: Num, high: Num, from, to}, notes: ["Performance excludes dividends."]}` |
| MR2 | `GET /research/ws` (WebSocket upgrade) | §4.3 |
| MR4 | `GET /research/watchlists`, `POST /research/watchlists {name}`, `PATCH /research/watchlists/{id} {name?, collapsed?, position?}`, `DELETE /research/watchlists/{id}`, `PUT /research/watchlists/{id}/items {symbols: [...]}` | lists `{lists: [{id, name, position, collapsed, items: [{symbol, position}]}]}`; PUT replaces the ordered items (duplicates → 400); no list is seeded (empty state in the UI) |
| MR4 | `GET /research/session`, `PUT /research/session {tabs: [{symbol, tf, type}], active, sidebar: {collapsed, section}}` | tabs/session restore (≤ `MAX_TABS`) |
| MR4 | `POST /research/watchlists/{id}/items {symbol, position?}`, `DELETE /research/watchlists/{id}/items/{symbol}`, `GET /research/watchlists/{id}/quotes`, `POST/DELETE /research/recent`, `GET /research/palette?q=&limit=` | added at MR4; exact shapes (also of the rows above — tabs carry `session` too) in §13 "MR4 backend notes" |
| MR5 | `GET /research/drawings?symbol=`, `POST /research/drawings`, `PATCH /research/drawings/{id}`, `DELETE /research/drawings/{id}`, `DELETE /research/drawings?symbol=&confirm=true`, `GET /research/drawings/export?symbol=`, `POST /research/drawings/import` | §10 MR5 |
| MR5 | `GET /research/symbols/{s}/indicators?tf=&session=&before=&limit=&ind=` | `{t: [...], series: {"sma:20:close": [..], …}}` aligned with the bars of the same request |
| MR5 | `GET/PUT /research/chart-settings?symbol=` | `{type, indicators: [...], magnet, session}` |
| MR6 | `GET /research/symbols/{s}/news?limit=&before=` | `{symbol, source: "Alpaca · Benzinga", source_label: "Benzinga via Alpaca", items: [{id, headline, summary, publisher, source_label, url, created_at, updated_at, symbols, images: [{size, path}]}], thumbnails, fetched_at, has_more, next_before, note}` — exact JSON in §13 "MR6 backend notes" (`images[].path` = the backend thumbnail proxy, never a third-party URL) |
| MR6 | `GET /research/symbols/{s}/logo` | `image/png` (≤ `MAX_LOGO_BYTES`, PNG signature + pixel size checked) or 404 `{error, kind: "NoLogo"\|"LogosNotOnPlan"}` |
| MR6 | `GET /research/news/images/{key}`, `GET/PUT /research/news/settings {thumbnails}` | thumbnail proxy (image bytes; 409 `ThumbnailsOff`, 404 `UnknownImage`, 503 `ThumbnailUnavailable`) + the thumbnails setting (default on) — §13 MR6 notes |
| MR7 | `GET /research/alerts`, `POST /research/alerts`, `PATCH /research/alerts/{id}`, `DELETE /research/alerts/{id}`, `GET /research/alerts/log?limit=&alert_id=` | §10 MR7; exact JSON in §13 "MR7 backend notes" |
| MR8 | `GET /research/portfolio`, `GET /research/symbols/{s}/markers?from=&to=&kinds=insider,calendar`, `GET /research/backtests/{run_id}/markers?symbol=&from=&to=`, `GET /research/watchlists/{id}/universe` | read-only; exact JSON in §13 "MR8 backend notes" (backtest markers got their own route instead of `kinds=backtest:<id>`) |

### 4.3 WebSocket `/research/ws` (MR2)

- aiohttp `WebSocketResponse(heartbeat=WS_HEARTBEAT_SECONDS, max_msg_size=WS_MAX_MESSAGE_BYTES)`;
  token header required (middleware); at most `WS_MAX_CLIENTS`
  concurrent connections (more → close 1013 "too many Research
  connections"). Text frames, JSON objects only; a malformed / unknown /
  oversized client message → `{"type": "error", "code": "bad_message",
  "message": …}` (5 in a row → close 1008).
- Client → server: `{"op": "hello", "visible": bool}` (first message,
  within 10 s or close 1008); `{"op": "visible", "visible": bool}`;
  `{"op": "subscribe", "topics": [topic…]}`; `{"op": "unsubscribe",
  "topics": [...]}`. Topics: `trades:<SYM>`, `candle:<SYM>:<tf>:<session>`,
  `news:<SYM>`, `alerts`, `feeds`, and (MR4) `watchlist:<id>` = `trades:` of every item of that list
  (one subscription, §13 MR4 backend notes). ≤ `WS_MAX_SUBSCRIPTIONS` per connection.
- Server → client: `{"type": "feeds", "data": <§1.2 payload>}` (on
  connect and on every change, ≤ 1/s); `{"type": "trade", "symbol", "price",
  "size", "ts", "feed"}` (coalesced: latest per symbol, ≤ 4/s per symbol);
  `{"type": "candle", "symbol", "tf", "session", "bar": <bar>}` (current
  candle, ≤ `LIVE_CANDLE_MAX_PER_SECOND` per topic; a bar with `final: true`
  closes it); `{"type": "sip_replace", "symbol", "tf", "session", "bars":
  [...]}` (IEX tail candles replaced by SIP); `{"type": "symbol_status",
  "symbol", "live": bool, "reason"}` (e.g. stream limit); `{"type": "news",
  "item"}`; `{"type": "alert", "alert_id", "symbol", "message", "ts"}` (MR7 adds `kind`, `title`, `price`, `level`, `log_id`, `logged` — §13 MR7 notes);
  `{"type": "error", "code", "message"}`.
- Size cap server → client (review L10): one frame ≤ `WS_MAX_SERVER_MESSAGE_BYTES`
  (1 MiB). Bar lists (`sip_replace`, any later bar-carrying message) are
  chunked to ≤ `WS_MAX_BARS_PER_MESSAGE` (500) bars per message with
  `"part": k, "parts": n` (1-based; parts of one replacement are sent
  consecutively; the client applies each part as it arrives — parts are
  independent ranges, oldest first). A single message that would still
  exceed the cap (e.g. a huge `feeds` payload) is a server bug: logged,
  not sent, `{"type": "error", "code": "message_too_large"}` instead. The
  WPF sets its receive buffer accordingly and treats a larger frame as a
  protocol error (close 1009, reconnect per §4.3.1).
- Back-pressure: per-connection send queue ≤ 500 messages; trades/candles
  are coalesced (newest wins); if `feeds`/`alert`/`news` cannot be queued
  the connection is closed 1013 (the client reconnects and re-subscribes) —
  never silently dropped.
- Demand: a connection counts as a view while its last `visible` is true.
  The WPF closes the socket ≤ 5 s after the view is hidden.

### 4.3.1 WPF client behaviour on `/research/ws` (MR2, binding for the WPF agent)

- **Open** only while the Research section or the pop-out window is
  visible (not minimized); **close** (1000) ≤ 5 s after it is hidden or
  minimized; never more than one socket per app.
- **Handshake order** after every (re)connect: (1) `hello {visible}`;
  (2) wait for the first `feeds` message (≤ 10 s, else close + reconnect);
  (3) ONE `subscribe` with the full current topic set (tabs/charts first,
  then watchlists, then `alerts`, `feeds`) — the server keeps no state
  across connections, so everything is re-subscribed; (4) for open charts
  re-fetch `GET …/bars` for the visible range (candles missed while
  disconnected are not replayed over the socket).
- **Reconnect** with backoff 1, 2, 5, 10, 30 s, then every 30 s (no limit
  while visible); the UI shows `WAITING · attempt n` in the status bar and
  "Live connection to the backend lost — waiting (attempt n, next try in
  s s)." in its tooltip; live values are marked stale (greyed, "not live")
  — never shown as current. Reset the counter after a connection that
  lasted ≥ 60 s.
- **WS vs polling:** while the socket is connected the WPF does NOT poll
  `/research/feeds/status`; whenever it is down (connecting/waiting) it
  polls every `STATUS_POLL_SECONDS` (5 s) so feed states stay visible; the
  first `feeds` message after a reconnect stops polling again.
- **Close codes:** 1000/1001 normal → reconnect only if still visible;
  1013 (server overloaded / too many Research connections / send queue
  full) → reconnect with the backoff starting at 5 s; 1008 (policy:
  missing hello, repeated bad messages) → the client has a bug: do NOT
  auto-reconnect in a loop — show "Live connection refused by the backend
  (protocol error) — see backend.log." and retry once after 60 s; 1009
  (too large) → as 1008; 4401-style / HTTP 401 on the upgrade → re-read
  the token once (BackendAuth) and retry once, then "the backend rejected
  the app's token" (§1.3); HTTP 503 `ResearchUnavailable` on the upgrade →
  §1.3 banner, retry every 30 s; 404 → "Research needs a newer backend".
- `{"type": "error"}` messages are logged; `bad_message` means a client bug
  (log, no UI); other codes show their `message` in the status tooltip.

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

## 5. Supervisor (`supervisor.py`, MR1)

```python
RestartPolicy(delays=RESTART_DELAYS_SECONDS, max_restarts=MAX_RESTARTS,
              window_seconds=RESTART_WINDOW_SECONDS, healthy_after_seconds=HEALTHY_AFTER_SECONDS)
ResearchSupervisor(policy=None, *, monotonic=time.monotonic, now=utc_now, sleep=asyncio.sleep)
  .add(name, label, factory)     # factory: () -> awaitable (runs "forever"); names unique ([a-z_]{1,40});
                                 # after start() the task starts at once
  .start() -> None               # sync, needs the running loop, idempotent, NEVER raises
  async .stop(timeout=SHUTDOWN_TIMEOUT_SECONDS) -> None   # cancels all, waits ≤ timeout, NEVER raises
  .set_store_status(state, reason=None, schema_version=None)
  .status() -> {"research": {...}, "store": {...}, "tasks": [...]}   # §1.2 parts
  .state -> str
```

- Each task runs inside `_guard(name)`: `await factory()`; `CancelledError`
  → state `stopped` and re-raised (cooperative cancel); any other exception
  — including `SystemExit` raised by Research code (review L4: it must not
  end the backend) and other `BaseException` subclasses, only
  `CancelledError` and `KeyboardInterrupt` excepted — → `logger.exception`, `last_error`
  (§1.2 rule), `restarting` + `next_restart_at`, sleep the policy delay,
  run again; budget used → `failed`. A normal return counts as a crash
  ("ended unexpectedly"). The wrapper itself never raises; an
  `add_done_callback` logs anything that still escapes. `KeyboardInterrupt`
  is passed on: it is the console run's own Ctrl+C (Python delivers it to
  whatever coroutine runs at that moment), the process stop signal — not a
  Research failure. A task that ends while Research is stopping is never
  restarted.
- Crash counting: timestamps of crashes in the last `window_seconds`;
  a run that lasted ≥ `healthy_after_seconds` clears the list.
- Blocking work: see §5.1 (never the default executor).
- One process-wide service (`market_research.install(app)` creates it;
  `market_research.service()` returns it; tests build their own).

### 5.1 Research thread pool (`executor.py`, review H1)

- ALL blocking Research work — SQLite, aggregation of long ranges, Alpaca
  REST calls (which can wait in the shared http_gate), `network.probe` —
  runs via `await executor.run_blocking(fn, *args)`: a dedicated
  `ThreadPoolExecutor(max_workers=RESEARCH_EXECUTOR_WORKERS = 4,
  thread_name_prefix="research")`, the caller's `contextvars` context copied
  in (so `http_gate.research_priority()` applies in the thread).
- **Forbidden in `market_research/`:** `asyncio.to_thread`,
  `loop.run_in_executor(None, …)`, any other `run_in_executor` / own
  `ThreadPoolExecutor` outside `executor.py`. Reason: asyncio's default
  executor is shared with the paper engine (`fund/engine.py`:
  `check_heartbeat` / dead-man switch, `_halt_and_liquidate` / kill
  switch); Research requests waiting up to `RESEARCH_ACQUIRE_TIMEOUT_SECONDS`
  in the gate must never occupy those threads. Enforced by an AST test in
  `tests/test_mr_shell.py` (every new module is covered automatically).
- Work that needs more parallelism waits for a Research thread (bounded
  by design); `supervisor.stop()` calls `executor.shutdown()` (no wait,
  queued items cancelled).

## 6. Storage (`store.py`, `backend/data/market_research.db`)

- Same robustness rules as `strategy_platform/storage/db.py`: WAL,
  `synchronous=NORMAL`, a connection per call, `PRAGMA user_version`
  migrations (`MIGRATIONS`: append-only list of `(version, [sql | callable])`,
  each version in ONE transaction, idempotent `IF NOT EXISTS` statements),
  corrupt file (not a database / failed `quick_check`) → renamed to
  `market_research.db.corrupt-<UTC %Y%m%dT%H%M%SZ>` (with -wal/-shm) and
  recreated; locked/unopenable → NOT renamed, `StoreUnavailable`, re-open at
  most every 30 s. A schema newer than the code → `StoreUnavailable`
  ("written by a newer version"), file untouched: `PRAGMA user_version` is
  read FIRST, before `journal_mode=WAL` or anything else that writes
  (review L5). Quarantine is all-or-nothing (review L6): `-wal`, `-shm`,
  then the main file; if any rename fails (Windows: file held open by
  another process) the files already moved are moved back, init raises
  `StoreUnavailable` and the corrupt file is NOT recreated (retry later).
- Error contract: not found → `None` / `[]`; any failure →
  `StoreUnavailable(UserFacingError)` with "The Research database
  (market_research.db) is unavailable: <reason>. Research is paused —
  trading is not affected."; invalid arguments → `ValueError`.
- API (MR1): `init(path=None) -> int`, `close()`, `is_open()`, `db_path()`,
  `default_path()`, `status() -> {state, reason, file, schema_version}`
  (never raises), `storage_info() -> {file, size_bytes, schema_version, rows}`,
  `meta_get/meta_set`, `add_audit(actor, action, detail=None) -> int`
  (actor ∈ user, system), `list_audit(limit=50)`,
  `prune(now=None) -> {table: deleted}` (tables that exist only).

| version (stage) | tables |
|---|---|
| v1 (MR1) | `meta(key PK, value, updated_at)`, `audit(id PK AUTOINCREMENT, ts, actor, action, detail_json)` |
| v2 (MR2) | `assets(symbol PK, name, asset_class, kind, exchange, tradable, status, fetched_at)`; `bars_1m(symbol, ts, open, high, low, close, volume, vwap, trade_count, feed CHECK IN ('sip','iex','crypto'), fetched_at, PRIMARY KEY(symbol, ts, feed))`; `bars_1m_days(symbol, day, feed, through, fetched_at, PK(symbol, day, feed))` (coverage, MR2a notes) |
| v3 (MR4) | `watchlists(id PK, name, position, collapsed, created_at, updated_at)`; `watchlist_items(list_id, symbol, position, added_at, PRIMARY KEY(list_id, symbol))`; `session(id INTEGER PK CHECK(id = 1), state_json, updated_at)`; `recent_symbols(symbol PK, seq, used_at)` (built 2026-10-17) |
| v4 (MR5) | `drawings(id PK, symbol, tool, points_json, style_json, name, text, locked, visible, timeframes_json, split_basis (= cumulative split factor applied), basis_as_of (NY date, null for crypto), rescales, alert_id, created_at, updated_at)`; `drawing_rescales(id PK AUTOINCREMENT, drawing_id, action_key, ts, factor, ex_date, detail_json, UNIQUE(drawing_id, action_key))`; `chart_settings(symbol PK, settings_json, updated_at)` (built 2026-10-17, MR5 notes) |
| v5 (MR6) | `news(id INTEGER PK (Alpaca id), symbols_json, headline, summary, publisher, url, images_json, created_at, updated_at, fetched_at, origin CHECK IN ('rest','stream'))`; `news_symbols(id, symbol (Alpaca/Benzinga tag), created_at, PK(id, symbol))`; `news_images(key PK (sha256 of the URL), url, created_at (newest news item using it), status CHECK IN ('pending','ok','failed','rejected'), content_type, data BLOB, reason, fetched_at)`; `logos(symbol PK, png BLOB, status CHECK IN ('ok','none'), fetched_at)` (built 2026-10-17, MR6 notes) |
| v6 (MR5 review, 2026-10-17) | `drawings.client_id` (nullable, UNIQUE index `drawings_client_id`) — POST/import idempotency (MR5 notes, review M3). Claimed after MR6's v5; the next free version is v7 |
| v7 (MR7, claimed 2026-10-17 — next free version is v8) | `alerts(id PK, symbol, kind CHECK IN ('price_cross','line_cross','day_move','news'), params_json, cooldown_seconds, enabled, drawing_id (UNIQUE when set), last_triggered_at, state_json (de-dup state), disabled_reason, created_at, updated_at)`; `alert_log(id PK AUTOINCREMENT, alert_id, symbol, ts, kind, message, price, detail_json)` (built 2026-10-17, MR7 notes) |

Version numbers follow the order in which stages land; a stage landing out
of order takes the next free number and updates this table. Retention
(`prune`, daily by the maintenance task): `bars_1m` by `ts`, `news` (+ `news_symbols`,
`news_images`, MR6) by `created_at`, `logos` by `fetched_at`, `alert_log` by `ts`, `audit` by `ts`
(§4.4). Watchlists, session, drawings, chart settings, alerts: until
deleted.

## 7. Data rules (MR2, used by MR3–MR8)

### 7.1 Intraday model c (Q2)

- `sip_through` = now − 15 min, floored to the minute. 1m candles with
  start < `sip_through` come from SIP (`AlpacaSipProvider` 1Min,
  `sip_request_end` respected — the restricted last 15 min are never
  requested), stored in `bars_1m` (feed `sip`, final).
- The live tail (start ≥ `sip_through`) = IEX: at load from IEX 1Min REST
  bars (`AlpacaProvider`, feed `iex`), then from the hub's IEX trades
  (current candle). Tail candles carry `feed: "iex_tail", final: false`
  and are drawn differently (lower opacity + chip "IEX · live tail").
- Every `SIP_TAIL_REFRESH_SECONDS` the backend fetches the newly released
  SIP minutes for symbols with an open intraday chart and sends
  `sip_replace`; the chart replaces those candles. A SIP fetch failure
  leaves the IEX candles marked as tail (never presented as SIP) and puts
  the reason in `symbol_status`.
- Crypto: one feed (Alpaca crypto), no tail; `feed: "crypto"`.
- Header (stocks): last = last IEX trade (`iex`); volume = Σ SIP 1m volume of
  today's regular session up to `sip_through` (`sip_delayed`, label "SIP ·
  15 min delayed"); day low/high = RTH low/high over SIP minutes + IEX tail
  (`computed`, label "SIP + IEX live tail"); prev close from SIP daily
  bars (`sip_daily`).

### 7.2 Timeframes and aggregation

- 5m/15m/1H/4H aggregated from 1m by the backend (Research pool, §5.1), buckets
  in America/New_York: `rth` anchored at 09:30 (1H: 09:30 … 15:30, last
  bucket 30 min; 4H: 09:30, 13:30); `eth` anchored at 04:00 (04:00–20:00;
  1H: 04:00 … 19:00; 4H: 04:00, 08:00, 12:00, 16:00). Early close (13:00):
  the RTH session and its last bucket end at 13:00; ETH ends 17:00 (verify).
  Crypto: UTC-aligned floor(t, tf).
- Bucket: o = first, h = max, l = min, c = last, v = Σ, vwap = Σ(vwap·v)/Σv
  (null if any minute lacks vwap or Σv = 0), n = Σ; minutes without trades
  are absent (never filled). A bucket containing any tail minute is
  `iex_tail`, `final: false`.
- 1D: completed equity sessions from the hedgefund.db cache, SIP daily
  history (`equity_bars_history`, adjustment `all`); today's daily candle
  is built from today's 1m (model c, `final: false`). Crypto 1D = UTC day
  (Q9). 1W from 1D: Mon–Fri week (crypto Mon 00:00 UTC), labelled with the
  first day; current week partial (`final: false`).
- Initial windows (plan §3.4): 1m 2 sessions, 5m 5, 15m 10, 1H ~1 month
  (22 sessions), 4H 6 months, 1D 2 years, 1W since 2016. Paging left with
  `before`; `has_more_left` false at 2016-01-01 (`HISTORY_START`) or, for
  1m/5m/15m, at the 30-day bound of the request limits — older intraday
  → 400 "Intraday history older than 30 days is not loaded in Research."
  (default C1 — confirm).

### 7.3 Market status

From `core/calendars.py`: `session_state`, `session_bounds`,
`next_trading_day`; pre-market 04:00–09:30, after hours 16:00–20:00 ET
(early close: 13:00–17:00, verify); holiday = weekday that is not a trading
day; outside the covered years → 503 `CalendarUnavailable` message.
Crypto always `open`; `day_start/day_end` = current UTC day.

### 7.4 Statistic definitions (plan §5.4, binding)

| value | definition | feed |
|---|---|---|
| last | last IEX trade (stocks), last crypto trade | `iex` / `crypto` |
| change / change_pct | last − prev close; % of prev close; null when either is null | `computed` |
| prev_close | close of the last completed regular session (split-adjusted) | `sip_daily` |
| day range | RTH low/high today (crypto: UTC day) | `computed` |
| volume | §7.1 (crypto: UTC-day volume, `crypto`) | `sip_delayed` |
| open | first regular-session minute's open today | `sip_delayed` (or `iex_tail` before 09:45) |
| vwap | Σ(vwap·v)/Σv over today's RTH 1m bars (SIP + tail) | `computed` |
| avg_volume_30d | mean SIP daily volume of the last 30 completed sessions | `sip_daily` |
| performance 1W/1M/3M/6M/1Y | last vs close of the last session on or before today − 7 d / 1 / 3 / 6 / 12 months (split-adjusted, dividends excluded — note) | `computed` |
| YTD | last vs last close of the previous year | `computed` |
| 52-week range | min low / max high of daily bars of the last 52 weeks | `sip_daily` |

Market closed → values are the last session's values with `as_of` = that
close (not an error; the UI marks them "at close").

### 7.5 Alpaca budget and priority (Q3) — extension of `http_gate`

- `http_gate.research_priority()`: a context manager setting a
  `contextvars.ContextVar` (copied into the Research pool by
  `executor.run_blocking`, §5.1); every
  Research REST call runs inside it — no signature change through
  registry/providers.
- `HttpGate._attempt` under research priority: (1) a research
  `TokenBucket(RESEARCH_MAX_PER_MINUTE, capacity 5)`; (2) a shared token
  only while the shared bucket holds ≥ `RESEARCH_RESERVE_TOKENS` tokens AND
  no normal-priority caller is waiting; otherwise wait in steps ≤ 0.25 s;
  after `RESEARCH_ACQUIRE_TIMEOUT_SECONDS` → `DataSourceUnavailable`
  "Research waited too long for the Alpaca rate limit (shared with
  trading)". Normal-priority behaviour unchanged (tests from SPEC §3/§10
  still pass). Counters: research calls in the last 60 s → `budget.research_used_last_minute`;
  current research wait → `budget.waiting_seconds`.

### 7.6 Market-data hub (MR2)

- The ONLY owner of Alpaca stream connections (one per endpoint): stocks
  `wss://stream.data.alpaca.markets/v2/iex`, crypto
  `wss://stream.data.alpaca.markets/v1beta3/crypto/us`, news
  `wss://stream.data.alpaca.markets/v1beta1/news` (verify paths). Client:
  the `websockets` package (already a backend dependency, lazy import),
  JSON frames, TLS verified; keys via `secrets.require_secret` at every
  connect, never logged. The Strategy Platform's future live runtime
  subscribes THROUGH the hub.
- Subscriptions: union of all WS clients' topics + active alerts;
  stocks: trades + minute bars for ≤ `STOCK_STREAM_SYMBOL_LIMIT` symbols
  (priority: open tabs, then watchlist order, then alerts); the rest get
  `symbol_status {live: false, reason: "Stream limit: 30 symbols — X is not live."}`
  (Q10, no snapshot fallback).
- States per feed (§1.2): `off` → `connecting` → `live`; connection loss →
  `waiting` "Waiting for connection (attempt n) — next try in s s" with
  `RECONNECT_BACKOFF_SECONDS`; from attempt 2 the hub first calls
  `network.probe("alpaca")` via `executor.run_blocking` INSIDE
  `http_gate.research_priority()` (review L8: the probe's REST request
  counts against Research's 60/min and yields to trading; ≥ 30 s spacing
  built into `probe`; False → stay waiting; raised `UserFacingError` →
  `unavailable` with its message). No probe code exists in MR1.
  Alpaca 406 → `unavailable` "Live data unavailable: Alpaca allows one
  stream connection per account and another program is using it."
  auth failure (401/402) → `unavailable` with the `AUTH_MESSAGE` text;
  409/insufficient subscription → `unavailable` plan message; missing keys
  → `unavailable` with the `SecretMissing` message.
  **Re-check schedule for `unavailable`** (the ONLY automatic retries in
  that state — consistent with §1.2): missing keys every 60 s (a key
  entered later applies without restart); 406 / keys rejected / plan
  refused every 5 min while demand > 0; and once at every demand change
  (e.g. the view becomes visible again). The fast backoff is used only in
  `waiting`.
- Lazy: streams open only while `demand.streams_wanted`; close after
  `HUB_LINGER_SECONDS` without demand. Runs as a supervised task
  (`market_data_hub`); its crash = Research unavailable, never the engine.

## 8. Failure states (no fallbacks)

| Situation | Backend | UI |
|---|---|---|
| No Alpaca keys / rejected | 503 `{error, kind: SecretMissing\|DataSourceUnavailable}`; feeds `unavailable` | card "Alpaca keys missing / rejected — enter them in Hedge Fund → Keys & data." |
| Rate limit reached | requests wait in the gate | "Waiting for Alpaca rate limit (n s)" from `budget.waiting_seconds` |
| Stream 406 | feed `unavailable` (§7.6 text) | that text in the status tooltip + symbol header chip "not live" |
| Stream lost / weak Wi-Fi | feed `waiting` attempt n | "Waiting for connection (attempt n)" |
| Symbol beyond 30-symbol limit | `symbol_status live:false` | "Stream limit: 30 symbols — X is not live." |
| Crypto symbol not on Alpaca | 404 `SymbolNotCovered` | "Not available from Alpaca (the configured crypto source)." |
| Market closed / holiday | not an error | status chip + values "at close" |
| SIP history refused (plan) | 503 with `SIP_PLAN_MESSAGE` | that message on the chart card |
| News unavailable | 503 `{error: "News unavailable: <reason>", kind: <cause, e.g. SecretMissing / DataSourceUnavailable>}` (MR6) | news card "News unavailable: <reason>" (the error text already starts with it) |
| Logo missing | 404 `kind: NoLogo` | no logo, ticker initials shown as plain text (identification, not data) |
| Logos not on the Alpaca plan | 404 `kind: LogosNotOnPlan` (remembered 24 h) | initials (normal empty state); the reason in the tooltip / settings (`/research/news/settings` → `logos`) |
| Thumbnail off / failed | 409 `ThumbnailsOff` / 503 `ThumbnailUnavailable` | card without a picture |
| Chart page error | bridge `error{code, message}` | chart card shows the code; the rest keeps working |
| Research task crashed | `research.state = unavailable` + reason | banner; trading unaffected |
| Research DB unavailable | `store.state = unavailable` → research unavailable | banner with the reason |
| Research package failed to load | 503 `ResearchUnavailable` on `/research/*` | banner |
| Calendar year not covered | 503 `CalendarUnavailable` | its message |

## 9. Security

- Routes + WebSocket behind `api_auth` (token header, origin allow-list —
  none for this backend; no CORS for Research). Loopback only (8867).
- Input: symbols by `core/instruments` regexes; ids = 32-char lowercase
  hex (uuid4 hex) server-assigned; enums checked; numbers finite and in
  range; text lengths §4.4; unknown fields/params → 400; body size checked.
- External text (asset names, news headlines/summaries, publisher) is
  untrusted: stored as plain text (control chars stripped, length capped),
  rendered as plain text in WPF (TextBlock) and escaped in the chart page;
  URLs only `https`/`http` (`providers.alpaca._safe_url`), opened in the
  default browser; images only https, ≤ 512 KB (MR6: `MAX_NEWS_IMAGE_BYTES`), png/jpeg/gif
  only (no SVG, no WebP), fetched ONLY by the backend thumbnail proxy (`media.py`, public hosts, checked
  redirects, timeouts) — the WPF loads `/research/news/images/{key}` from 8867 and never a
  third-party URL — and decoded by WPF with a pixel-size cap.
- Chart page (MR3): local files via virtual host mapping
  (`chart.ufos.local` → `HedgeFund/MarketResearch/Chart` in the output dir; renamed from
  `research.ufos.local` at MR3, see the MR3 bridge contract), CSP
  `default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline';
  img-src 'self' data:; connect-src 'none'; font-src 'self'; base-uri 'none';
  form-action 'none'`, no network, no token in the page, navigation and
  new windows blocked, host objects off, dev tools off in Release, every
  bridge message schema-checked both ways (type, size ≤ 4 MB host→page,
  ≤ 64 KB page→host), no `eval`, vendored engine verified against
  MANIFEST sha256 at load (C# side, mismatch → chart error "engine file
  modified").
- Secrets never in logs, responses, the page or the DB.
- Residual risks (to tell the user): token file readable by programs of
  the same Windows user; news thumbnails reveal the IP to image hosts
  (setting to switch them off); the WebSocket is plain ws on loopback.

