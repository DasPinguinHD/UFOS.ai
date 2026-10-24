# Market Research — implementation contract

> Part of UFOS.ai, a proof-of-concept portfolio project. Research only, paper
> trading only, not investment advice. Concept: `docs/market-research-plan.md`
> (repo) / Claude Project `claude/market-research-plan.md`. This file is
> BINDING for every stage MR1–MR8; change it together with the code.
> Written 2026-10-16 (MR0) while the user was away — decisions marked
> "default (user away 2026-10-16) — confirm" are the plan's recommendations
> taken by default and must be confirmed by the user afterwards.
> **Confirmed by the user 2026-10-24 ("Das passt"):** Q1–Q12 and C1–C5 (§2, marked "confirmed by the user
> 2026-10-24") AND every other "default (user away …) — confirm" choice further down in this file.

**Status of this file:** complete for MR0 (2026-10-16). §1 = the MR1
WPF↔backend contract.

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

## 11. Open points / verify

- Alpaca stream paths, news stream availability on the free plan, logos
  endpoint access on the free plan (MR6: Alpaca documents logos as a paid Broker API add-on → a
  401/403 is expected on the free plan and shown as "not on this plan"; crypto path `BTCUSD` and
  whether stream news carry `images` — verify live), assets endpoint ETF flag (verify at
  MR2/MR6 against the live API).
- Extended-hours end on early-close days (17:00 ET assumed).
- KLineChart exact 10.x version: pinned **10.0.3** at MR3a (2026-10-16); the vendored files are
  still to be fetched (`MarketResearch/Chart/tests/fetch_engine.py`, see MR3a notes).

## 12. Extensions of `strategy_platform` code (record here AND in its SPEC)

| stage | extension | status |
|---|---|---|
| MR1 | none (reuses `api/routes.dumps`, `read_body`, `user_error`, `core.errors`) | — |
| MR2 | `data/http_gate.py`: `research_priority()` context + research bucket/reserve + hard 60/60 s cap on sends, slot rule, `note_send()`, `ResearchYield` + `research_yield_check()`, `is_research_priority()`, `research_stats()`, `TokenBucket.try_acquire_above()` (§7.5, §13 MR2a notes + review fixes) | built 2026-10-16 (MR2a) |
| MR2 | `data/network.py`: `AdaptiveLimiter.try_acquire()` (non-blocking slot, review M3) | built 2026-10-16 (MR2a) |
| MR2 | `data/cache.py`: series lock via `_held(key)` — normal waiters counted, a research holder waiting in the gate yields (`ResearchYield`) and `get_bars` retries ≤ 50× (review M3b) | built 2026-10-16 (MR2a) |
| MR2 | `data/providers/alpaca.py`: `assets()` (trading API asset list, `load_trading_sdk`, kwarg `trading_sdk`); `harden_client(on_send=)` → gate `note_send` per HTTP request; `get_bars` timeframe `"30Min"` (Research only, not via cache/pit; `known_at` = start + 30 min); IEX/SIP 1Min via the existing `get_bars` | built 2026-10-16 (MR2a) |
| MR5 | none — indicators call the DSL evaluator (`strategy/evaluate.evaluate_node`, `strategy/panel.Panel`) unchanged; splits via the existing `AlpacaProvider.corporate_actions`; session VWAP + compare overlay are Research-only (`market_research/indicators.py`) | built 2026-10-17 (dependency noted in its SPEC §12) |
| MR6 | `data/providers/alpaca.py`: `images` in news dicts (`_news_images`, https only, ≤ 5), `logo(symbol)` (plain `requests` through the same gate, data type `logos`, constructor kwarg `http=None`), `LogosNotAvailable(DataSourceUnavailable)`, `load_requests()`, constants `LOGO_*` / `PNG_SIGNATURE` | built 2026-10-17 (MR6 notes) |
| MR7 | none — alerts use the hub (`trade_hook`, MR7 addition in `market_research`), `stats.quote.prev_close` and MR5 drawings read-only | built 2026-10-17 |
| MR8 | `backtest/runcard.py`: `trades_summary` keeps the fills (`fills_by_symbol` {SYM: [[ts, side, qty, fill_price]…]}, ≤ `CARD_FILLS_MAX` 10 000 per card, `fills_truncated`) — additive key of `trades_json` on NEW cards (finished cards stay immutable; older cards have none → Research says so) | built 2026-10-17 |

## 13. Stage notes

(Each stage appends "MRn notes (implemented <date>)" here: what was built,
deviations, review fixes.)

### 13.1 MR1 notes (implemented 2026-10-16: backend part)

- Files: `__init__.py` (`ResearchService`, `install(app)`, `service()`),
  `limits.py` (§4.4 constants + `clip(text)`), `supervisor.py`
  (`ResearchSupervisor`, `RestartPolicy`, `describe_error`), `store.py`
  (v1: `meta`, `audit`; `status()` without I/O: `ok` / `unavailable` with
  the `StoreUnavailable` user message / `not_opened`; an operation failure
  turns it `unavailable` until the next successful operation),
  `maintenance.py`, `feeds.py` (MR1: all feeds `off`, "Live data is not
  built yet (stage MR2)."), `api.py` (`GET /research/feeds/status`,
  `status_payload(supervisor, feeds, now)`).
- main.py: `_install_research(app)` right after `v2_routes.register(app)`
  (lazy `import market_research` inside try/except → on failure a `*
  /research/{tail:.*}` route answering 503 `ResearchUnavailable`);
  `research.start()` after `engine.start()`; `/shutdown` awaits
  `research.shutdown()` (≤ 2 s) before scheduling the exit. Research tasks
  are not part of main's `gather()`.
- The WebSocket server on 8865 (websockets) is untouched; `/research/ws`
  (MR2) lives on the aiohttp app (8867) — the middleware accepts the token
  only as header there.
- Tests: `tests/test_mr_shell.py` (27): supervisor isolation with a dummy
  engine task, restart/failed/return/healthy-reset/bounded stop/no-loop
  start, store WAL + idempotent migrations + corrupt quarantine + newer
  schema + unopenable path + prune, maintenance retry, exact payload keys,
  400 on query params, generic 500, 401/403 through `api_auth.http_middleware`,
  main.py wiring incl. the 503 stub.
- Review fixes (2026-10-16): H1 own Research thread pool (`executor.py`,
  4 workers, context copied), `maintenance.py` uses it, AST test forbids
  `to_thread` / default executor in the package, research acquire timeout
  30 s; L4 `SystemExit` from a Research task = crash (test),
  `KeyboardInterrupt` passed on (documented); L5 `user_version` read before
  WAL (test: newer-schema file byte-identical, journal mode unchanged); L6
  all-or-nothing quarantine (tests with a simulated `PermissionError`);
  SPEC only: M2 §4.3.1, M3 §0 Time / bars / bridge, L7 §1.2 + §7.6
  re-check schedule, L8 probe under research priority, L10 frame cap +
  chunking. MR tests: 34.

### MR2b notes (implemented 2026-10-16: market-data hub + `/research/ws`)

**Files.** `hub/hub.py` (`MarketDataHub`, `chunk_bars`, `default_probe`), `hub/alpaca_stream.py`
(`StreamConnection`, `HubConfig`), `hub/protocol.py` (Alpaca stream JSON protocol, error codes, texts),
`hub/transport.py` (`websockets` client, lazy import, TLS verified), `hub/candles.py` (`LiveCandle`,
`bucket_for`, `RateLimiter`), `hub/demand.py` (`parse_topic`, `Subscriber`, `rank_symbols`), `hub/ws.py`
(`ResearchWsSession`, `register_ws`, `encode`), `hub/aio.py` (`within()` — `asyncio.wait_for` replacement:
on Python ≤ 3.11 `wait_for` can swallow a cancellation, which made a hub task ignore `supervisor.stop()`
in a test). Wiring: `ResearchService(hub=None)` creates `MarketDataHub(data=lambda: self.data)`,
`hub.status_fn = status_payload`, supervised task **`market_data_hub`** ("Live market data (Alpaca
streams)"), `self.feeds = feeds.HubFeeds(hub)` (feeds/demand from the hub, `budget()` unchanged);
`install()` adds `GET /research/ws` via `hub.ws.register_ws` (not in `api.ROUTES`). No
`strategy_platform` code changed by MR2b (reuses `secrets.require_secret`, `providers.alpaca` texts /
`_clean` / `_safe_url` / `news_symbol`, `base.parse_ts`, `core.instruments`, `network.probe` /
`next_probe_in`, `http_gate.research_priority`, `routes.dumps`, `api_auth` middleware) and MR2a's
`bars/aggregate.py` (buckets, `aggregate`), `bars/intraday.py` (`sip_through`, `MinuteLoader`),
`sources.py` (`daily`, `crypto_hourly`). alpaca-py's stream classes are NOT used (they run their own
event loop/thread and reconnect logic that the Research supervisor could not stop or observe).

**Constants added to `limits.py`:** `HUB_STABLE_SECONDS` 60 (a connection live this long resets the
backoff), `RECHECK_MISSING_KEYS_SECONDS` 60, `RECHECK_REFUSED_SECONDS` 300, `STREAM_CONNECT_TIMEOUT_SECONDS`
15, `STREAM_HANDSHAKE_TIMEOUT_SECONDS` 10, `STREAM_PING_TIMEOUT_SECONDS` 10, `STREAM_MAX_FRAME_BYTES`
4 MiB, `TRADE_MAX_PER_SECOND` 4, `FEEDS_MAX_PER_SECOND` 1, `WS_HELLO_TIMEOUT_SECONDS` 10,
`WS_SEND_QUEUE_MAX` 500, `WS_BAD_MESSAGE_LIMIT` 5, `WS_MAX_TOPIC_CHARS` 64, `SEED_RETRY_SECONDS` 60,
`LATE_TRADE_GRACE_SECONDS` 15 (review fix H1).

**Alpaca side (per endpoint, one connection per account).** URLs as §7.6. Protocol: wait for
`[{"T":"success","msg":"connected"}]` → send `{"action":"auth","key","secret"}` (keys read via
`run_blocking` at every connect, only in this frame) → `authenticated` → state `live` →
`{"action":"subscribe","trades":[…],"bars":[…]}` (news: `"news":[…]`; crypto `BTC/USD`, news crypto tag
`BTCUSD`), diffs as `unsubscribe` + `subscribe`. Frames `t` (trade), `b` (minute bar), `n` (news),
`subscription`, `error`. Errors: 401/402 → `unavailable` with `AUTH_MESSAGE`; 406 → `unavailable`
"Live data unavailable: Alpaca allows one stream connection per account and another program is using
it."; 409/410 → `unavailable` "Live data unavailable: your Alpaca market-data plan does not include this
stream."; 405 (review fix L5; Alpaca docs: it rejects the WHOLE request) → Alpaca answers every
subscribe/unsubscribe in order, the hub keeps a FIFO of open requests: the refused request's symbols stay
OFF the wire (not in `subscribed`) and get `symbol_status` "Stream limit: Alpaca refused more symbols — X
is not live."; a refused batch of several symbols is retried at once one symbol per request (so as many as
fit go live); refused symbols are retried (one per request) whenever a slot frees (an unsubscribe is sent)
and at every demand change — never in a loop. A symbol missing from a `subscription` answer without an
error → "Alpaca did not confirm the subscription — X is not live." (a 405 that follows a partial answer
relabels those with the limit text). 400 answers (pops) the oldest request too; 403 logged; 404/407/500, socket close, handshake
timeout → `waiting`. Missing keys / Credential Manager unreadable → `unavailable` with that message
(re-check 60 s, no connect attempted). HTTP 401/403 on the upgrade = keys rejected, 406 = connection limit.
Silence: no frame for `STREAM_SILENCE_SECONDS` → WebSocket ping; no pong within 10 s → `waiting`.
Waiting: backoff 2, 5, 10, 30, 60, 60 … s; from attempt 2 the probe runs first (`network.probe("alpaca")`
in the Research pool inside `research_priority()`); if `network.next_probe_in()` > 0 the hub waits that
long asynchronously (no Research thread blocked); probe False → stay waiting (next attempt, no connect);
probe `UserFacingError` → `unavailable` (re-check 300 s). Re-checks in `unavailable`: 60 s (keys) / 300 s
(refused) and once at every demand change (views/alerts count). Lazy: a connection runs while
`views + alerts > 0` AND the endpoint has ≥ 1 symbol; it closes `HUB_LINGER_SECONDS` after that stops.

**Feed rows (`feeds[]`, §1.2) from the hub:** `message` texts — `off`: "Off — live streams run only while
a Research view is visible or an alert is active." / "Off — no stocks symbol is open." / "Off — no crypto
symbol is open." / "Off — no news subscription is open."; `connecting`: "Connecting to Alpaca (IEX · real
time)…"; `live`: "Live · n symbol(s)"; `waiting`: "Waiting for connection (attempt n) — next try in s s"
(while probing: "… — checking the connection"); `unavailable`: the reason. `attempt` = n only while
`waiting`; `connected_since` while live; `last_message_at` = last data frame; `symbols` = live symbols
(0 unless live). Before the hub task runs / while it restarts every feed is `off` with "Live market data is
not running (see the Research status)." / "Live market data stopped — the Research task is restarting or
stopped."

**`/research/ws` — exact messages.** Auth: `api_auth` middleware on the upgrade, header `X-UFOS-Token`
only (no token / `?token=` → HTTP 401 before the handler). aiohttp `WebSocketResponse(heartbeat=20,
max_msg_size=65 536)` (memory guard; aiohttp closes 1009 above it); ≥ 4 open sessions → accepted then
closed 1013 "too many Research connections".

Client → server (JSON object, exactly these keys, else `bad_message`):
- `{"op":"hello","visible":true|false}` — first message, within 10 s, else close 1008 "hello expected";
  a second hello → `bad_message`. The server answers with one `feeds` message.
- `{"op":"visible","visible":bool}`.
- `{"op":"subscribe","topics":[…]}` / `{"op":"unsubscribe","topics":[…]}` — 1–64 topics; one invalid
  topic rejects the whole message (`bad_message`); more than 64 subscriptions in total →
  `{"type":"error","code":"subscription_limit","message":"At most 64 subscriptions per connection —
  nothing was subscribed."}` (whole message rejected). Topics: `trades:<SYM>`, `candle:<SYM>:<tf>:<session>`
  (tf `1m 5m 15m 1H 4H 1D 1W`; session `rth`/`eth` for equities, **`utc` for crypto** — the value the REST
  `/bars` echoes), `news:<SYM>`, `alerts`, `feeds`; symbols case-insensitive, stored upper-case. Subscribing
  `feeds` sends the current `feeds` message at once.
- `bad_message` reasons (plain text): "only JSON text frames are accepted", "message larger than 16384
  bytes", "message is not valid JSON", "unknown op (hello, visible, subscribe, unsubscribe)", "<op>:
  fields must be exactly …", "hello must be the first message", "hello was already sent", "<op>: visible
  must be true or false", "<op>: topics must be a list of 1–64 strings", "<op>: invalid topic '…': <why>".
  5 in a row (a valid message resets the count) → close 1008 "repeated bad messages" (queued errors are
  flushed first, ≤ 1 s).

Server → client (`t`/`ts` UTC ISO with seconds):
- `{"type":"feeds","data":<§1.2 payload>}` — after hello, on subscribing `feeds`, then on every change
  (`generated_at` ignored for the comparison), checked once per second.
- `{"type":"trade","symbol":"AAPL","price":187.12,"size":100.0,"ts":"2026-10-16T14:00:01+00:00",
  "feed":"iex"|"crypto"}` — latest per symbol, ≤ 4/s per symbol.
- `{"type":"candle","symbol","tf","session","bar":{"t","o","h","l","c","v","vwap","n","feed","final"}}` —
  ≤ 4/s per topic; equities `feed:"iex_tail", final:false` (also the bucket's last message — the next
  bucket's `t` starts a new candle; SIP replaces it later), crypto `feed:"crypto"`, the closing message
  `final:true` (sent on the first trade of the next bucket or by the clock at the bucket end).
  **Late data / corrections (review fix H1):** the hub closes a bucket by its own clock, and a stream minute
  bar always arrives after its minute; a trade or minute bar of the bucket closed LAST never restarts it:
  arriving ≤ `LATE_TRADE_GRACE_SECONDS` (15 s, hub clock) after the bucket end it is merged into the closed
  bar and the corrected bar is sent again as a `candle` with the SAME `t` (crypto `final:true`, equities
  `final:false`; only if it changed) — the client replaces its bar with that `t`, which may be older than
  the current candle; later or older-bucket data is dropped (counted per candle, `late_dropped`). Never a
  one-trade candle for a closed `t`. A candle is
  only sent once its bucket is complete since its start: at topic start / after a reconnect / after a
  stream-limit slot is gained, the elapsed part of the current bucket is seeded from REST
  (`MinuteLoader.equity_minutes` = SIP + IEX tail like the REST bars, crypto `crypto_minutes`; 1W adds the
  finished days from `sources.daily` / `crypto_hourly` — crypto parts are HOURLY rows, each ending inside its
  own hour; review fix M2: the close is the latest part's close); stream minute bars replace their minute
  (authoritative), REST seed minutes replace trades-only minutes. Seed failure →
  `{"type":"error","code":"candle_unavailable","topic":"candle:…","message":"Live candle for X tf
  unavailable: <reason>"}`, no candle (never a partial one), retry after 60 s.
- `{"type":"sip_replace","symbol","tf","session","bars":[…],"part":k,"parts":n}` — every 60 s for ≤ 8
  equity symbols with an intraday candle topic (1m–4H; 1D/1W are re-fetched by REST): completed buckets
  whose end lies in (previous `sip_through`, new `sip_through`] built from SIP 1m (`feed:"sip",
  final:true`); ≤ 500 bars per message, parts consecutive, oldest first. The first refresh after a
  subscribe starts 2 minutes before the then-current `sip_through` (covers the REST load). Failure →
  `symbol_status {live: <stream state>, reason: "SIP update failed: <reason> The IEX live tail stays marked
  as tail."}`, retried next round.
- `{"type":"symbol_status","symbol","live":bool,"reason":str|null}` — for every symbol of the
  connection's `trades:`/`candle:` topics whenever (live, reason) changes (first time on subscribe).
  Reasons: "Stream limit: 30 symbols — X is not live." (priority: views' topics in subscribe order,
  interleaved over connections, then alerts), "Stream limit: Alpaca refused more symbols — X is not
  live.", "Alpaca did not confirm the subscription — X is not live.", "Stocks|Crypto stream: waiting for connection (attempt n).", "… stream: connecting.", "… stream
  is off.", the `unavailable` message, "Live market data is not running (see the Research status).", the
  SIP failure text (with `live: true`).
- `{"type":"news","item":{id, headline, summary, publisher, url, created_at, updated_at, symbols,
  images: []}}` — plain text, capped, `url` only http(s) (else null).
- `{"type":"error","code":"bad_message"|"subscription_limit"|"candle_unavailable"|"message_too_large",
  "message"}`; `message_too_large` replaces any frame above 1 MiB (logged as a server bug).

Back-pressure: send queue ≤ 500; `candle` (key topic + bar t), `trade` (symbol), `symbol_status`
(symbol) and `feeds` are coalesced in place (newest wins, position kept); any message that does not fit →
close 1013 "send queue full" (never a silent drop). Close codes: 1000 normal, 1008 policy, 1011 internal
error, 1013 overloaded / too many connections / queue full.

**Isolation.** All hub parts run inside the one supervised task; an unexpected exception closes every
stream connection and the supervisor restarts the task (5/15/30/60/120 s) — the engine is unaffected
(test with a dummy engine). A failing subscriber is logged and skipped. Blocking work (keys, REST seeds, SIP
refresh, probe) only via `executor.run_blocking` inside `research_priority()`.

**Tests** (fake Alpaca transport, no network): `tests/test_mr_hub.py` (26: multiplexing, crypto/news
endpoints, lazy start/linger, alert demand, 30-symbol limit + slot freed, 405, 406/402/409/missing keys +
re-check schedule + demand-change re-check, backoff/attempt counter/probe only from attempt 2 + spacing,
probe error, drop + stable reset, silence → ping → reconnect, default probe under research priority in a
Research thread, candle throttle + boundary + minute bar, crypto final, REST seed, seed failure, tail → SIP
1m/5m, chunking 500 + 100, SIP failure, 8-symbol cap, hub crash isolated), `tests/test_mr_hub_ws.py` (8:
hello timeout, hello/visible/subscribe/unsubscribe/close, 5 bad → 1008, topic/limit validation, queue
overflow 1013 + coalescing, frame cap, header-token auth through the middleware, install + 1013 on too
many clients), `tests/test_mr_hub_candles.py` (13: anchors, last bucket, early close, DST US vs EU, 1W
label + Monday holiday, agreement with `bars/aggregate.py`, OHLCV/vwap, close, seed + minute bars without
double counting). `test_mr_shell.py`: the payload test now also expects `market_data_hub` and the
hub's "not running" feed message.

**Open / verify (live API):** stream paths and news-stream access on the free plan (§11); whether Alpaca
accepts part of a subscribe batch before a 405 (the hub only trusts the `subscription` answer); trade
conditions are not filtered (IEX trades as sent; the minute bar corrects each minute). `transport.py` is
checked statically against the Windows venv's websockets 17.2 (`asyncio.client.connect` kwargs,
`InvalidStatus.response.status_code`); it cannot run on the 3.10 test VM (websockets 17 needs ≥ 3.11) — first
live run on the user's PC. Final check 2026-10-16: the stream loop also cancels its pending wake waiter on
cancel. Review fixes H1 (late trades/bars → corrected bar, never a restart), M2 (crypto 1W hourly parts), L5
(405 whole-request handling + retries): MR tests 139 OK, full suite OK (split runs), scope guard stage0 + mr0 ok.

### MR2a notes (implemented 2026-10-16: REST data side)

**Files.** `assets.py` (refresh, search, `require`, `SymbolNotCovered`, supervised task **`asset_refresh`**),
`market.py` (market status), `sources.py` (`AlpacaSources`: the ONLY Alpaca access of the REST side),
`bars/aggregate.py` (pure bucket logic; also used by MR2b), `bars/intraday.py` (`sip_through`,
`retention_day`, `MinuteLoader`), `bars/series.py` (`/bars` windows, paging, 1D/1W, `BadParam`),
`stats/context.py` (reference session, `num`), `stats/snapshot.py`, `stats/keystats.py`, `data.py`
(`DataService`, blocking, called via `run_blocking` inside `research_priority()`), `data_api.py`
(`DataRoutes` mixin of `api.ResearchApi`, `DATA_ROUTES` appended to `api.ROUTES`). Store **v2**
(`assets`, `bars_1m`, `bars_1m_days`). `ResearchService(data=None)` → `self.data = DataService()`.
`feeds.budget()` reads `http_gate.research_stats("alpaca")` (`research_used_last_minute` int,
`waiting_seconds` float | null). Tests: `test_mr_data_bars` (21), `test_mr_data_stats` (11),
`test_mr_data_market` (10), `test_mr_data_gate` (10), `test_mr_data_api` (6) + `tests/mr_data_testkit.py`;
`test_mr_shell` asserts adapted (task list, measured budget, prune result with v2 tables).

**Exact JSON (all keys always present; `Num` = `{value, feed, feed_label, as_of, reason}` §4.1).**

`GET /research/assets?q=aap&limit=2` →
```json
{"items": [{"symbol": "AAPL", "name": "Apple Inc. Common Stock", "asset_class": "equity", "kind": "stock",
            "exchange": "NASDAQ", "tradable": true}],
 "refreshed_at": "2026-10-15T15:00:00+00:00", "source": "alpaca"}
```
`GET /research/market/status` →
```json
{"equities": {"state": "open", "session_date": "2026-10-15", "early_close": false,
              "opens_at": "2026-10-15T13:30:00+00:00", "closes_at": "2026-10-15T20:00:00+00:00",
              "next_open_at": "2026-10-16T13:30:00+00:00", "next_close_at": "2026-10-15T20:00:00+00:00",
              "extended_opens_at": "2026-10-15T08:00:00+00:00", "extended_closes_at": "2026-10-16T00:00:00+00:00",
              "tz": "America/New_York",
              "ny": {"opens_at": "2026-10-15T09:30:00-04:00", "closes_at": "2026-10-15T16:00:00-04:00",
                     "next_open_at": "2026-10-16T09:30:00-04:00", "next_close_at": "2026-10-15T16:00:00-04:00",
                     "extended_opens_at": "2026-10-15T04:00:00-04:00", "extended_closes_at": "2026-10-15T20:00:00-04:00"}},
 "crypto": {"state": "open", "day_start": "2026-10-15T00:00:00+00:00", "day_end": "2026-10-16T00:00:00+00:00",
            "tz": "UTC"},
 "generated_at": "2026-10-15T15:00:00+00:00"}
```
`state` ∈ `pre_market` (04:00–open) · `open` · `after_hours` (close–20:00, 17:00 on early-close days) ·
`closed` · `holiday` (weekday without session). `session_date` = the session in progress (pre/open/after)
or the next one; `next_open_at`/`next_close_at` = first regular open/close strictly after now.

`GET /research/symbols/AAPL/snapshot` →
```json
{"symbol": "AAPL", "name": "Apple Inc. Common Stock", "exchange": "NASDAQ", "asset_class": "equity", "kind": "stock",
 "last": {"value": 120.0, "feed": "iex", "feed_label": "IEX · real time", "as_of": "2026-10-15T15:00:00+00:00", "reason": null},
 "prev_close": {"value": 100.0, "feed": "sip_daily", "feed_label": "SIP · daily bars", "as_of": "2026-10-14T20:00:00+00:00", "reason": null},
 "change": {"value": 20.0, "feed": "computed", "feed_label": "IEX · real time − SIP · daily bars", "as_of": "…", "reason": null},
 "change_pct": {"value": 20.0, "feed": "computed", "feed_label": "(IEX · real time − SIP · daily bars) / previous close", "as_of": "…", "reason": null},
 "day_low": {"value": 99.5, "feed": "computed", "feed_label": "SIP + IEX live tail (regular session)", "as_of": "…", "reason": null},
 "day_high": {"…": "same shape"},
 "volume": {"value": 750.0, "feed": "sip_delayed", "feed_label": "SIP · 15 min delayed", "as_of": "2026-10-15T14:45:00+00:00", "reason": null},
 "market": {"…": "the equities (or crypto) part of /research/market/status"}}
```
Null example: `{"value": null, "feed": "sip_delayed", "feed_label": "SIP · 15 min delayed", "as_of": null,
"reason": "SIP volume is 15 minutes delayed — the first value is available at 09:45 ET."}`. Crypto:
`last.feed` `crypto`, `prev_close` label "Alpaca crypto · previous UTC day close", day range label
"Alpaca crypto 1m (UTC day)", `volume` `{feed: "crypto", feed_label: "Alpaca crypto · UTC day volume"}`.

`GET /research/symbols/AAPL/bars?tf=15m&session=eth&limit=2` →
```json
{"symbol": "AAPL", "tf": "15m", "session": "eth",
 "bars": [{"t": "2026-10-15T14:30:00+00:00", "o": 101.2, "h": 102.0, "l": 100.9, "c": 101.5, "v": 150.0,
           "vwap": 101.4, "n": 45, "feed": "sip", "final": true},
          {"t": "2026-10-15T14:45:00+00:00", "o": 101.5, "h": 101.9, "l": 101.1, "c": 101.6, "v": 15.0,
           "vwap": 101.5, "n": 45, "feed": "iex_tail", "final": false}],
 "has_more_left": true, "sip_through": "2026-10-15T14:45:00+00:00", "tz": "America/New_York"}
```
Crypto: `"session": "utc"`, `"sip_through": null`, `"tz": "UTC"`, `feed` `crypto`. Bar `feed` ∈ `sip`,
`iex_tail`, `sip_daily` (completed 1D, and 1W made only of them), `crypto`; today's 1D candle has the feed
of its minutes (`sip`/`iex_tail`), `final: false`.

`GET /research/symbols/AAPL/stats` →
```json
{"open": {"value": 109.5, "feed": "sip_delayed", "feed_label": "SIP · 15 min delayed", "as_of": "2026-10-15T13:30:00+00:00", "reason": null},
 "vwap": {"value": 103.9, "feed": "computed", "feed_label": "SIP 1m + IEX live tail (regular session)", "as_of": "…", "reason": null},
 "avg_volume_30d": {"value": 4918.5, "feed": "sip_daily", "feed_label": "SIP · daily bars", "as_of": "2026-10-14T04:00:00+00:00", "reason": null},
 "exchange": "NASDAQ",
 "performance": {"1W": {"pct": {"value": 10.0, "feed": "computed", "feed_label": "IEX last trade vs SIP daily close (split-adjusted)", "as_of": "…", "reason": null},
                        "base_date": "2026-10-08"},
                 "1M": {"…": "…"}, "3M": {"…": "…"}, "6M": {"…": "…"}, "YTD": {"…": "…"}, "1Y": {"…": "…"}},
 "range_52w": {"low": {"…": "Num sip_daily"}, "high": {"…": "Num sip_daily"}, "from": "2025-10-17", "to": "2026-10-14"},
 "notes": ["Performance excludes dividends."]}
```

**Errors.** Validation → 400 `{error: "invalid input", errors: {field: msg}}` (unknown/duplicate param,
`tf`, `session` (given for crypto → 400), `limit` 1…5000 intraday / 2000 1D/1W, `before`, `symbol`, `q`);
`before` older than the 30-day bound for 1m/5m/15m → 400 `{error: "Intraday history older than 30 days is
not loaded in Research.", errors: {before: same}}`; not in Alpaca's list → 404 `{error, kind:
"SymbolNotCovered"}` (crypto text = §4.1, equities "Not available from Alpaca: X is not in Alpaca's list of
active US equities."); missing keys / source / calendar / store → 503 `{error: user_message, kind}`
(`SecretMissing`, `DataSourceUnavailable`, `SourceAuthRejected`, `CalendarUnavailable`, `StoreUnavailable`);
anything else → generic 500. A SIP failure is a 503 — IEX is never used instead.

**Deviations / decisions (default — user away 2026-10-16 — confirm):**
- Market status uses `core/calendars` only, as C2 says — no Alpaca clock/calendar request (the task brief
  mentioned the Alpaca clock; C2 is binding). Additive keys: `extended_opens_at`, `extended_closes_at`,
  `tz`, `ny` (the same times with the New York offset).
- Store v2: `bars_1m.feed` CHECK also allows `crypto` (crypto 1m has neither SIP nor IEX); new table
  `bars_1m_days(symbol, day, feed, through, fetched_at)` = per-day coverage (minutes without trades
  are absent, so rows alone cannot say "fetched"), pruned with `bars_1m` (`RETENTION_ALIASES`).
  The 1m days kept/served = the last `INTRADAY_LOOKBACK_DAYS` (30) NY days (crypto: UTC days), one day
  inside the prune cutoff, so a served day is never half-pruned.
- 1H/4H older than the 30-day 1m store: SIP 1m fetched per request, aggregated, NOT stored, ≤
  `MAX_TRANSIENT_SESSIONS` (130) older sessions per request (the 6-month 4H window fits; paging goes on
  with `before`).
- Crypto 1H/4H/1D/1W: completed 1Hour bars from the hedgefund.db cache (UTC aligned) + stored crypto 1m
  after the newest cached hour; NOT Alpaca's crypto 1Day bars (their midnight is not guaranteed to be UTC —
  Q9 needs UTC days). ≤ `CRYPTO_HOURLY_MAX_DAYS` (1800) per request.
- The IEX tail is fetched at every load (`AlpacaProvider` IEX 1Min) and never stored; tail minutes are
  never final. Crypto coverage stays 2 min behind now (a late minute bar is fetched again).
- 1D `t` = 00:00 America/New_York of the session date (as Alpaca); 1W `t` = Monday 00:00 NY (also when
  Monday is a holiday), crypto Monday 00:00 UTC. For equities 1D/1W, `session` only changes how today's
  partial candle is built (RTH or ETH minutes). 1D freshness = `last_close_before(now − 15 min)`.
- Reference session (header/stats): today (NY) once its pre-market started (04:00 ET) on a trading day,
  else the previous trading day; `prev_close` = the session before it. Day values are "at close" after the
  session (`as_of` = close). Day range/VWAP/open = regular session only.
- Stats use the split-adjusted SIP daily series (`adjustment="split"`, dividends excluded); the chart's 1D
  uses `all` (§7.2). 52W window = (ref − 52 weeks, ref], completed daily bars only (today's partial
  candle not included). avg_volume_30d: the last 30 completed daily bars; fewer → null + reason.
- A failed last-trade request makes `last` (and change/performance) null WITH the reason; every other
  source failure fails the request with 503 (no partial data from another source).
- `kind: etf` = `KNOWN_ETFS` or the word ETF/ETN in the name (Alpaca has no ETF flag — verify). OTC assets
  and non-USD crypto pairs are not listed. Asset names: whitespace/control characters stripped, ≤ 120 chars.
- `asset_refresh`: due after 24 h (meta `assets_refreshed_at`), a failure is logged and retried after
  `ASSET_REFRESH_RETRY_SECONDS` (30 min), the old list is kept; an empty answer never replaces the list;
  search with an empty table refreshes once and answers 503 if that fails.
- The asset list call (Alpaca trading API, `TradingClient(paper=True)`) goes through the same Alpaca gate
  (counts against the 180/60 budget although Alpaca limits the trading API separately — conservative).
- Gate (§7.5): in addition to the 60/min research bucket (capacity 5) a hard sliding cap of 60 research
  requests per 60 s (reserved at permission time, re-stamped at send) → never above 60/min; "reserve" =
  Research takes a shared token only while more than `RESEARCH_RESERVE_TOKENS` (3) remain; Research takes
  its tokens BEFORE the concurrency slot (never holds a slot while waiting); normal callers that have to
  wait for a shared token are counted (`_normal_waiting`) and Research waits while any are.
- New `limits.py` constants: `RETENTION_ALIASES`, `ASSET_REFRESH_RETRY_SECONDS` 1800,
  `ASSET_QUERY_MAX_CHARS` 32, `ASSET_LIMIT_DEFAULT` 20, `ASSET_LIMIT_MAX` 50, `MAX_ASSET_NAME_CHARS` 120,
  `INTRADAY_LOOKBACK_DAYS` 30, `MAX_TRANSIENT_SESSIONS` 130, `FETCH_1M_DAYS` {equity 10, crypto 6},
  `CRYPTO_HOURLY_MAX_DAYS` 1800, `STATS_DAILY_LOOKBACK_DAYS` 400.

**Strategy Platform extensions (also in `strategy_platform/SPEC.md` §12):** `data/http_gate.py` —
`research_priority()`, `is_research_priority()`, `research_stats(source)`, `HttpGate.research_stats()`,
`TokenBucket.try_acquire_above(reserve)`, constants `RESEARCH_*`; normal path unchanged except that a
caller waiting for a shared token is counted. `data/providers/alpaca.py` — `assets()` (+
`load_trading_sdk()`, constructor kwarg `trading_sdk=None`, data type `assets`). The IEX tail and SIP 1m use
the existing `get_bars` unchanged.

**Open / verify (live API):** the assets endpoint fields (`class`, `exchange`, no ETF flag); whether
Alpaca's IEX 1Min REST includes the still-open minute (treated as tail, not final, either way);
extended-hours end on early-close days (17:00 ET assumed).

**MR2a review fixes (2026-10-16).**
- M3a — slots: Research takes its permit and shared token first, then a concurrency slot only via
  `AdaptiveLimiter.try_acquire()` while no normal caller waits for a token or slot, holding at most
  max(1, limit − 1) slots (limit 1 → only while no normal request is active). A platform request waits at
  most for requests already in flight.
- M3b — bar cache: `BarCache` counts normal callers waiting for a series lock; a research-priority holder
  polls that in every gate wait step (`http_gate.research_yield_check`) and raises `ResearchYield`
  (`is_cancellation`: no source error recorded), the lock is released, `get_bars` retries (≤ 50×, then
  503 "Research gave way … too often"). Test: platform latency < 1.5 s while a research fill of the same
  series waits in the gate.
- M4 — 1H/4H days older than the 1m store: SIP **30Min** bars (clock aligned = 09:30/04:00 anchors; one
  call per ≤ `TRANSIENT_30M_DAYS` = 300 calendar days; the 6-month 4H window = 1 call instead of ~13),
  not stored; buckets equal the 1m aggregation (test incl. DST weeks and an early close).
- L6 — the 60/min cap counts actual sends: stamps at send time (after the slot), `note_send()` per HTTP
  request (alpaca-py pages); crypto 1m batches bounded by SPAN (≤ 6 calendar days), like equities.
- L7 — empty asset table + failed refresh: the next on-demand refresh not before
  `ASSET_EMPTY_RETRY_SECONDS` (60 s); meanwhile the same error (kind + reason) is raised again.
- Tests: `test_mr_data_priority.py` (8) new; bars +2 (30Min), market +1 (retry spacing), gate +1
  (30Min provider); slot test uses a real `AdaptiveLimiter`. New limits: `TRANSIENT_30M_DAYS`,
  `ASSET_EMPTY_RETRY_SECONDS`.


**MR2a follow-up — bar session flag `s` (2026-10-17).** Every INTRADAY equity bar (1m … 4H) carries
`"s": "pre"|"rth"|"post"` (bridge contract §10 MR3: extended-hours shading) — in `GET …/bars` and, from
the hub, in `candle` and `sip_replace` messages (`hub/candles.py` `_make_bar(…, s)`; `sip_replace` uses
`aggregate`). Rule (`bars/aggregate.session_flag`): bucket START vs the regular session of its New York day
(`core/calendars`, early closes included): start < open → `pre`, open ≤ start < close → `rth`, ≥ close →
`post`; an ETH bucket spanning the open/close (1H 09:00, 4H 08:00 / 12:00; 12:00 on early-close days) is
classified by its start. `session=rth` bars are all `rth`. Crypto and 1D/1W bars carry no `s`. Example:
`{"t": "2026-10-14T13:30:00+00:00", "o": …, "feed": "sip", "final": true, "s": "rth"}`. Tests: 04:00 / 09:29 /
09:30 / 15:59 / 16:00 / 19:59 boundaries, DST weeks (US vs EU), early close (13:00 → `post`), REST + hub
live candle, absence for crypto/1D/1W. `test_mr_hub_candles` exact-dict assert adapted (`"s": "rth"`).
The WPF side (`ChartHost` bar DTO → bridge) must pass `s` through to the page (MR3/MR4 WPF — not done here).

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

### MR6 backend notes (implemented 2026-10-17: news + logos — routes + exact JSON, binding for MR6-WPF)

**Files.** `news_text.py` (untrusted text → plain text, link / image-URL rules, `sanitize_item`), `media.py`
(image checks, `ImageFetcher` = the thumbnail proxy's downloader), `news.py` (`NewsService`: REST news,
stream hook, store queue, thumbnail setting + proxy), `logos.py` (`LogoService`), `news_api.py` (`NewsRoutes`
mixin, `NEWS_ROUTES` appended to `api.ROUTES`, `Binary` answers → `web.Response` in `api.register`). Store
**v5** (`news`, `news_symbols`, `news_images`, `logos`). Supervised task **`news_stream_store`** ("News stream
storage": writes queued stream items, refreshes the cached thumbnail setting). Changed (additive):
`hub/hub.py` (`news_hook`; without it items go out with `images: []`), `hub/protocol.py` (`parse_news` =
`news_text.sanitize_item`), `sources.py` (`news`, `logo`), `api.py`, `__init__.py`, `store.py`, `limits.py`.
Strategy Platform extension (§12, also in its SPEC): `providers/alpaca.py` `images` + `logo()`.

**Untrusted text (§9).** Headline / summary / publisher: HTML tags removed (`script`/`style`/`svg`/… content
dropped, block tags → space), entities decoded, a second strip when the decoded text still contains tags
(`&lt;b&gt;x&lt;/b&gt;` → `x`), a tag cut off at the end removed, control characters / line separators →
space, format / private-use characters removed, whitespace collapsed, caps 300 / 500 / 100 chars ("…").
Plain comparisons survive (`x < y`). The result is plain text — the WPF shows it in a TextBlock, never as
markup. Links (`url`): only `http`/`https`, a host, no `user:pw@`, no whitespace/control characters,
≤ 1000 chars — else `null` (open with the default browser, show the domain). `symbols` = the
Alpaca/Benzinga tags (`AAPL`, crypto `BTCUSD`), ≤ 50, `^[A-Z0-9][A-Z0-9./-]{0,19}$`. Items without a
positive integer id, a headline or `created_at` are dropped.

**`GET /research/symbols/{s}/news?limit=&before=`** (`limit` 1–50, default 20; `before` ISO-8601,
exclusive; unknown / repeated params → 400; symbol 400 / 404 `SymbolNotCovered` as §4.1):
```json
{"symbol": "AAPL", "source": "Alpaca · Benzinga", "source_label": "Benzinga via Alpaca",
 "items": [{"id": 41234567, "headline": "Apple beats estimates", "summary": "Plain text …",
            "publisher": "benzinga", "source_label": "Benzinga via Alpaca",
            "url": "https://www.benzinga.com/news/26/10/41234567/apple-beats",
            "created_at": "2026-10-15T14:00:00+00:00", "updated_at": "2026-10-15T14:05:00+00:00",
            "symbols": ["AAPL", "MSFT"],
            "images": [{"size": "thumb", "path": "/research/news/images/3f9c…64 hex"},
                       {"size": "small", "path": "/research/news/images/a01b…64 hex"}]}],
 "thumbnails": true, "fetched_at": "2026-10-15T15:00:00+00:00", "has_more": true,
 "next_before": "2026-10-14T09:12:00+00:00",
 "note": "News: Benzinga via Alpaca. Headlines and summaries are shown as plain text; links open the original article in your browser."}
```
- Newest first (`created_at`, then `id`). `has_more` = the page is full; next page: `before=next_before`
  (items sharing that exact second with the boundary can be skipped — the WPF de-dups by `id` anyway).
  `summary`, `publisher`, `url`, `updated_at` may be `null`; `images` is `[]` when the item has none or
  thumbnails are off. `images[].size` ∈ `thumb`, `small` (Alpaca's `large` is not passed on — above the
  size cap); at most one per size. `fetched_at` = when Alpaca last answered for this symbol.
- Source: `AlpacaProvider.news` through `sources.news`, Research pool + `research_priority()` (≤ 60/min,
  yields to trading). The first page of a symbol is asked from Alpaca at most every `NEWS_REFRESH_SECONDS`
  (60 s) per symbol (a request for a LARGER page asks again); in between and for the merge the answer is
  read from the store (Alpaca answers + stream items, de-duplicated by id). `before` pages always ask Alpaca
  (`end = before − 1 s`, Alpaca's end is inclusive).
- Alpaca failure → **503** `{"error": "News unavailable: <the source's message>", "kind": "SecretMissing" |
  "SourceAuthRejected" | "DataSourceUnavailable" | …}` — never stored news instead (no fallback). Store
  unavailable → 503 `StoreUnavailable`.

**Store / de-dup / retention.** `news.id` = Alpaca's id; an existing row is replaced only by a NEWER
`updated_at` (Alpaca corrections); a stream copy without images gets the REST images later. 7-day
retention by `created_at` for `news`, `news_symbols`, `news_images` (`news_images.created_at` = the newest
news item using that image URL, so a shared image is kept as long as a current item uses it). Stream items
are queued (≤ 1000; full → delivered but not stored, logged) and written by `news_stream_store`.

**WebSocket `news:<SYM>`** (topic of §4.3, MR2b routing unchanged: the hub subscribes the union of all
`news:` topics on the ONE news stream connection and sends each item once per connection that has a
matching topic): `{"type": "news", "item": <the item object above>}`. A repeat (same `id` and `updated_at`)
is not sent again; an update (newer `updated_at`) is sent again with the same `id` — the WPF replaces the
card by `id`. Items missed while disconnected are not replayed: after a reconnect the WPF re-fetches the
REST list (like bars, §4.3.1). The news feed row of `/research/feeds/status` (`id: "news"`) is the stream's
state (`off` / `connecting` / `live` / `waiting` / `unavailable` + message, MR2b); REST failures are shown on
the news card (503 above).

**Thumbnails — proxy + setting.** The WPF loads ONLY `http://127.0.0.1:8867` + `images[].path` (with the
token header, like every route) and never a third-party URL.
- `GET /research/news/images/{key}` (`key` = sha256 hex of an image URL that came from Alpaca's news data;
  anything else → 404 `{"error": "Unknown news image.", "kind": "UnknownImage"}`): 200 with the image bytes,
  `Content-Type` image/png | image/jpeg | image/gif, `Cache-Control: private, max-age=3600`,
  `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'none'; sandbox`. Thumbnails off
  → **409** `{"error": "News thumbnails are switched off (Research settings) — no image is loaded.", "kind":
  "ThumbnailsOff"}` (nothing is fetched). Download / check failure → **503** `{"error": "Thumbnail
  unavailable: <reason>", "kind": "ThumbnailUnavailable"}` (WPF: card without a picture). Thumbnail pool full
  (review M2) → **503** `{"error": "Thumbnail loading is busy — try again shortly.", "kind": "ThumbnailBusy"}`
  — the WPF treats `ThumbnailBusy` (or a 429) as "retry later".
- Download rules (`media.py`): https only, DNS host name (no IP literal / localhost / `.local`), port 443, the
  name must resolve to public addresses only; no automatic redirects — ≤ 3 hops, each checked the same way;
  timeouts 10 s per connect/read and a HARD 20 s total (review H1: every read is ≤ 8 KiB with the socket
  timeout set to the remaining time, and a kill timer shuts the socket at the deadline — dripping headers,
  chunk lines, TLS handshake or body cannot hold a thread longer; DNS runs in a helper thread bounded by the
  remaining time, ≤ 4 lookups pending); the connection goes to the IP that passed the public-address check
  (review L5: no second resolution — `http.client` pinned to that IP, `Host`, TLS SNI and certificate check
  use the host name); ≤ 512 KB (Content-Length first, then while reading);
  `Content-Type` ∈ png/jpeg/gif (SVG, WebP — review L6: no WIC decoding of third-party WebP — and everything
  else refused) AND the file signature must match;
  ≤ 4096 px per side (header). Own `http_gate.HttpGate` source `news_images` (60/min, one attempt) — image
  hosts are not Alpaca, the Alpaca budget is not touched; gate wait ≤ 5 s. Downloads run on their OWN pool
  (review M2: `executor.run_image`, 2 workers, ≤ 8 jobs running + waiting, then 503 `ThumbnailBusy`) — never
  on the 4-worker Research pool, so hanging image hosts cannot delay bars / news / alert writes. Cache in `news_images`: `ok` served from the store;
  `rejected` (rule broken) never retried; `failed` (network / HTTP status) retried after 15 min.
- `GET /research/news/settings` / `PUT /research/news/settings {"thumbnails": bool}` (exactly that key;
  else 400; a change writes an audit row `news_thumbnails_set`):
```json
{"thumbnails": true,
 "note": "News thumbnails are loaded by the HedgeFund backend (never by the app) from the image servers named in the Alpaca/Benzinga news data; those servers see your IP address when the backend fetches an image. Switch thumbnails off and no image request is made.",
 "source_label": "Benzinga via Alpaca",
 "logos": {"state": "not_on_plan", "reason": "Logos are not available on this Alpaca plan: Alpaca refused the logo request (HTTP 403: forbidden) — Alpaca offers the logo endpoint as a paid add-on (Broker API); it is usually not part of the free plan",
           "checked_at": "2026-10-15T15:00:00+00:00"}}
```
  Default on (C4, meta `news_thumbnails`). `logos.state` ∈ `unknown` (not asked yet), `available`,
  `not_on_plan` (`reason`, `checked_at` set; else `null`). The WPF shows the setting "Show news thumbnails"
  with the `note` text (privacy note) in its Research settings.

**`GET /research/symbols/{s}/logo`** (no query parameters): 200 `image/png` bytes (`Cache-Control: private,
max-age=86400`, nosniff, CSP as above), or 404 `{"error": "No logo available from Alpaca for MSFT.", "kind":
"NoLogo"}`, or 404 `{"error": "Logos are not available on this Alpaca plan: <reason>", "kind":
"LogosNotOnPlan"}`; other failures 503 `{error, kind}` (keys missing, network, a logo that fails the checks).
The WPF shows ticker initials as plain text for any non-200 (normal empty state — not a data fallback) and
puts `error` in the tooltip. Source: Alpaca `GET https://data.alpaca.markets/v1beta1/logos/{symbol}`
(crypto: `BTCUSD` spelling — verify), never `?placeholder=true`; keys in headers only; no redirects; checks
HTTP 200, `image/png`, PNG signature, ≤ 256 KB, ≤ 1024 px per side. Alpaca's docs/blog list logos as a paid
add-on of the Broker API (Essential/Core plans) — on the user's free plan a 401/403 is expected: remembered
account-wide (meta `logos_refused`), every symbol answers `LogosNotOnPlan` without a request for 24 h, then
Alpaca is asked again. Cache `logos`: `ok` 30 days, `none` (Alpaca 404) 7 days; pruned after 30 days.

**Tests** (`tests/test_mr_news.py` 25, `tests/test_mr_news_media.py` 22, kit `tests/mr_news_testkit.py`;
fakes only): sanitising (tags, scripts, entities, double-encoded markup, truncated tag, control/format
characters, caps), link and image-URL scheme/host filtering, REST shape + order + no third-party URL in the
answer, research priority on the Research pool, refresh spacing + paging + de-dup, update only when newer,
503 with the cause kind, validation, crypto tag; retention (news + symbols + images 7 days, logos 30 days,
shared image kept); thumbnails setting (off → no paths, proxy 409, nothing fetched, audit; on again); stream:
routing by symbol through the hub, one message per connection, repeat dropped, update re-sent, unsubscribed
symbol not sent, stored by the task, thumbnails off, no image URLs without the MR6 service, hook failure
isolated; image checks (4 types + dimensions, SVG, wrong type, mismatch, size, pixels), fetcher (headers,
Content-Length / streamed oversize, wrong type / SVG, socket timeout, total deadline, network error, HTTP
404, unsafe URLs, private resolution, checked redirects + limit), proxy cache (ok cached, rejected not
retried, failed retried after the window, unknown key); provider `logo` (URL, headers, no redirect, 404 →
None, 401/403 → `LogosNotAvailable`, type / size / signature / redirect checks, crypto path) and news
`images` https-only; logo cache (30 days ok, 7 days none, plan refusal remembered + re-checked, status) and
the binary answer through `api.register` (aiohttp stubbed). MR tests 268 OK; full suite OK (split runs);
scope guard stage0 + mr0 ok.

**Defaults (user away 2026-10-17) — confirm:** thumbnails proxied by the backend and cached 7 days (with the
news); only `thumb`/`small` sizes; 512 KB / 4096 px caps; a 401/403 from the logo endpoint is read as "not on
this plan" (the message names the HTTP status, so wrong keys are recognisable — other Research functions
show the key error anyway); logo refusal re-checked once a day; first news page cached 60 s per symbol.

### MR6 WPF notes (implemented 2026-10-17: news card, Research settings, logos)

- **Files.** New `MarketResearch/ResearchView.News.cs` (partial: news card, live news, thumbnails, logos, settings).
  Changed additively: `ResearchClient.cs` (`GetNewsAsync`, `Get/PutNewsSettingsAsync`, `GetBinaryAsync` + DTOs `NewsList`,
  `NewsItem`, `NewsImage`, `NewsSettings`, `LogosState`, `BinaryResult`), `ResearchView.xaml` (news card, header logo
  `Image`, "⚙ Settings" + popup under the status strip), `ResearchView.xaml.cs` (subscribe/unsubscribe `NewsReceived`,
  re-fetch news on re-sync), `ResearchView.Workspace.cs` (`RenderHeader` → `NewsTrackActive()`, palette rows get a logo
  badge), `ResearchView.Watchlists.cs` (topic `news:<SYM>` of the active tab after its `trades:`; rows get a logo badge).
  `ResearchSocket.cs` (`NewsReceived` event, `case "news"`, `WsNews {item}`) was added by the MR5-fix agent on request.
  Static checks only (no dotnet).
- **News card.** Follows the active tab: GET …/news?limit=20 (newest first), "Older news" = `before=next_before`, de-dup
  by `id` (REST page replaces the list; a stream repeat with the same `updated_at` is ignored, an update replaces the card),
  ≤ 200 kept / 60 shown. Live: `news` messages whose tags contain the symbol (crypto tag `BTCUSD`); after a reconnect the
  first page is re-fetched (nothing replayed). A REST failure → "News unavailable: <reason>" verbatim + Retry, the list is
  cleared (no older list as current); an older-page failure keeps the list with its own line. Card: headline (plain text; a
  button only when the link passes an http/https + host + no user-info check, opened with `NewsroomLinks.Open` = the
  Newsroom's default-browser helper, the host in the tooltip), meta "Benzinga via Alpaca · 12 min ago · publisher"
  (ages tick every 30 s; exact local time in the tooltip), ≤ 8 ticker chips (+n) — a chip looks the tag up in the asset list
  (GET /research/palette, exact match; `XXXUSD` also as `XXX-USD`) and opens it, otherwise a message; summary (2 lines).
  Header chip "● LIVE / NOT LIVE" = the news stream row of feeds/status (or the socket state).
- **Thumbnails.** Only `images[].path` matching `^/research/news/images/[0-9a-f]{64}$`, fetched from 127.0.0.1:8867 with
  the token (`GetBinaryAsync` refuses any path outside `/research/`); ≤ 512 KB (Content-Length and while reading),
  signature PNG/JPEG/GIF/WebP, pixel size ≤ 4096 read from the header before decoding, decoded to 192 px width on a worker
  thread, frozen, session cache ≤ 300; 409 ThumbnailsOff / 503 / 404 → card without a picture (logged). WebP needs the
  Windows WebP codec — without it the card has no picture.
- **Settings (⚙ under the status strip).** GET /research/news/settings when opened; "☑/☐ Show news thumbnails" → PUT
  {thumbnails}, then the image cache is cleared and the news list re-fetched; the backend's privacy `note` verbatim;
  logo state line (available / not on this plan + reason / not checked yet). Esc closes.
- **Logos.** Header 40 px box, watchlist rows 14 px, palette rows 16 px: initials (plain text) are the normal empty state;
  GET /research/symbols/{s}/logo (≤ 2 in flight, ≤ 256 KB, `image/png` + PNG signature, ≤ 1024 px, decoded to 96 px,
  frozen, session cache). `LogosNotOnPlan` (or settings `logos.state = not_on_plan`) stops all further logo requests for
  the session; the reason is in the header logo tooltip. No third-party fetch anywhere.
- **Accessibility.** Names on the card, headline buttons ("News: … Opens host in the browser"), chips ("Open the symbol X"),
  thumbnails, logo badges, settings toggle (with its state); live regions for the news message and the settings message.
- **Verify on the PC:** first build; JPEG/WebP thumbnails; Popup focus for the settings toggle; ticker chip lookup for
  crypto tags; logo route on the free plan (expected: LogosNotOnPlan → initials).
- **Review fixes (2026-10-17).** M2: thumbnails are requested only for cards in the visible part of the news list
  (± 120 px, re-checked on scroll and after every render), at most 2 requests in flight; queued requests are dropped
  when the card scrolls out, the list is re-rendered or the symbol changes; a proxy answer 503 "busy" (kind
  `ThumbnailBusy` or "busy" in `error`) or 429 is retried after 5 / 15 / 30 s (then no picture) — the card simply has
  no picture meanwhile, no error banner. L6: WebP is no longer accepted client-side (PNG/JPEG/GIF signatures only),
  matching the backend proxy's allow-list.

### MR7 backend notes (implemented 2026-10-17: alerts — routes + exact JSON + engine, binding for MR7-WPF)

**Files.** `alerts.py` (validation, CRUD, `AlertEngine`, `AlertSub`), `alerts_api.py` (`AlertRoutes` mixin,
`ALERT_ROUTES` appended to `api.ROUTES`). Store **v7** (`alerts`, `alert_log`; v6 = MR5 review, so MR7 took the
next free number — the next free one is v8). Supervised task **`alert_engine`** ("Price & news alerts").
Changed additively: `hub/hub.py` (`trade_hook`: called for EVERY stream trade of a streaming symbol, errors
logged and swallowed), `api.py`, `__init__.py`, `store.py`, `limits.py`. Reads (no writes) MR5 drawings via
`store.drawing_get`; no `strategy_platform` change.

**Rules.** `kind` + `params` (exactly these keys; unknown → 400):
- `price_cross` `{level: number > 0 and ≤ 1e9, direction: "up"|"down"}`;
- `line_cross` `{drawing_id: 32 hex, direction?: "up"|"down"|"any" (default any)}` — the drawing must be an
  `alertLine` of the same symbol; one alert per line (409). The level = the line's current price
  (`points[0].p`), re-read every `ALERT_RELOAD_SECONDS` (5 s) and after every alert CRUD — a moved or
  split-rescaled line applies within 5 s. **Link (revised 2026-10-17): the backend owns `drawings.alert_id`
  and writes it in the SAME transaction as the alert** — create (enabled) / switch on → the drawing's
  `alert_id` = the alert id (and its `updated_at` = the alert's write time); switch off / delete / automatic
  switch-off → `alert_id` = null. Inside that transaction the line is checked again (exists, `alertLine`, same
  symbol, not linked to ANOTHER existing alert — a link to a no longer existing alert id is replaced); a
  failure writes nothing (409 "The linked alert line no longer exists." / "This alert line already has an
  alert.", 400 for tool/symbol). A disabled line alert keeps `drawing_id` but the drawing shows no link. **The
  WPF never PATCHes `alert_id` itself** (no second call, no dangling link); after an alert create / PATCH /
  DELETE it reloads that symbol's drawings (`GET /research/drawings?symbol=`) or sends the page an
  `upsertDrawing` with `alert_id` set/cleared (the new `updated_at` comes with the reload). A deleted line → the alert
  is switched off: `enabled: false`, `disabled_reason: "The linked alert line was deleted — this alert is
  switched off."`, audit `alert_disabled`; it cannot be switched on again (409);
- `day_move` `{pct: 0.1–50, direction: "up"|"down"|"any"}` — move of the last trade vs the previous close
  (§7.4: SIP daily close before the reference session, split-adjusted; crypto previous UTC day close —
  `stats.quote.prev_close`, Research pool + research priority, re-read when the day changes, failures retried
  after 60 s); at most ONE trigger per day (NY reference session, crypto UTC day) and side;
- `news` `{}` — a news item for the symbol on the news stream (MR6, after its de-dup); once per news id
  (updates of an item never trigger again).
`cooldown_seconds` 60–86 400 (default 900), `enabled` (default true). ≤ 200 rules (409 "At most 200 alerts —
delete one first."); an identical rule (symbol + kind + params) → 409 "An identical alert for this symbol
already exists."; unknown symbol → 404 `SymbolNotCovered`; `symbol`/`kind` cannot be PATCHed (400).

**Order of work (review M3, 2026-10-17).** The task loop flushes pending triggers FIRST (log + send), then
re-reads the rules (local SQLite), then flushes again; the previous closes for `day_move` are fetched by a
separate coroutine on its own schedule (`ALERT_RELOAD_SECONDS`, failures retried after 60 s) — a slow or
hanging Alpaca request never delays a trigger (test: a trigger is written and sent < 1 s while the fetch
hangs). A crash of that coroutine ends the task like any crash.

**Evaluation (`AlertEngine`, event loop — arithmetic only; store writes on the Research pool).** Trades
come from `hub.trade_hook` (every trade, not the coalesced `trade` messages). A crossing needs a PREVIOUS live
trade of the symbol: `up` = prev < level ≤ last, `down` = prev > level ≥ last — a jump over the level
between two trades (gap) fires; the first trade after a start, a restart or a stream gap (`symbol_status`
`live: false`) only sets the baseline. Late (older) trades are ignored. IEX trades of the extended session
count (stocks); with the market closed nothing trades, the rule stays armed (status note). De-dup = one
trigger per rule per cooldown (`last_triggered_at`, persisted) + the day / news-id rules above (state
persisted in `alerts.state_json`). A trigger is written FIRST (`alert_log` row + rule state, one transaction)
and then sent — a restart never repeats it (cooldown and day/news de-dup survive; the baseline rule prevents
a trigger from the first post-restart trade). Store unavailable at that moment → the message is still sent
with `logged: false`. Minute bars are not evaluated separately (the IEX bars are built from the same trades).

**Demand / limits.** Every ENABLED rule is one hub subscriber of kind `alert` (topic `trades:<SYM>`, news
rules `news:<SYM>`) → `/research/feeds/status` `demand.alerts` = enabled rules and the streams run while ≥ 1
is enabled (stated: `runtime.note`). Priority for the 30-symbol stock limit: views first, then alerts in
creation order; a rule beyond it is `not_live` with "Stream limit: 30 symbols — X is not live. This alert
cannot fire while the symbol is not live." (no snapshot, no polling — Q10). Switching a rule off / deleting it
releases its demand at once; the task ending (crash / shutdown) unregisters all.

**Alert object** (every key always present):
```json
{"id": "9a0e…32 hex", "symbol": "AAPL", "kind": "price_cross", "params": {"level": 190.0, "direction": "up"},
 "cooldown_seconds": 900, "enabled": true, "drawing_id": null, "last_triggered_at": null,
 "disabled_reason": null, "created_at": "2026-10-17T12:00:00+00:00", "updated_at": "2026-10-17T12:00:00+00:00",
 "status": {"state": "armed", "reason": null, "live": true, "level": 190.0,
            "last_price": {"price": 187.12, "ts": "2026-10-17T14:00:01+00:00"}, "prev_close": null,
            "cooldown_until": null}}
```
`status.state` ∈ `armed` · `waiting` (reason: "Armed with the next live trade — a crossing needs a previous
price." [+ " Market closed — no trades until the next session."], "Previous close unavailable: …", "Loading …",
"Starting the live stream…") · `cooldown` (`cooldown_until`, reason "Fired at … — quiet until …") ·
`not_live` (the hub's reason + " This alert cannot fire while the symbol is not live.") · `disabled`
(`disabled_reason` or "Switched off.") · `not_running` ("The alert task is not running (see the Research
status). Alerts work only while UFOS.ai runs — …"). `day_move` adds `"levels": {"up", "down"}` (price
thresholds) and `prev_close`; `news` has `level: null`, `last_price: null`.

**Routes.**
- `GET /research/alerts` → `{"alerts": [<alert>…] (oldest first), "limits": {"max_alerts": 200,
  "cooldown_default": 900, "cooldown_min": 60, "cooldown_max": 86400, "day_move_pct_min": 0.1,
  "day_move_pct_max": 50.0}, "runtime": {"running": true, "note": "Alerts work only while UFOS.ai runs — there
  is no background service; nothing is checked while the app is closed.", "store_error": null, "active": 3}}`.
  The WPF shows `runtime.note` in the Alerts section (always visible) and `running: false` as a banner.
- `POST /research/alerts {symbol, kind, params, cooldown_seconds?, enabled?}` → 201 `<alert>` (audit
  `alert_created`). `PATCH /research/alerts/{id} {params?, cooldown_seconds?, enabled?}` (≥ 1 key) → 200
  `<alert>` (audit `alert_updated`; switching on clears `disabled_reason`). `DELETE /research/alerts/{id}` →
  200 `{"deleted": id}` (audit; the log rows stay). Bodies ≤ 4096 bytes; unknown id → 404 `{"error": "Alert
  not found."}`.
- `GET /research/alerts/log?limit=1…500 (100)&alert_id=` → `{"items": [{"id": 12, "alert_id": "9a0e…",
  "symbol": "AAPL", "ts": "2026-10-17T14:00:02+00:00", "kind": "price_cross", "message": "AAPL crossed above
  190.00 — last trade 190.05.", "price": 190.05, "detail": {"side": "up", "previous_price": 189.9, "level": 190.0,
  "trade_ts": "2026-10-17T14:00:02+00:00"}}], "limit": 100, "retention_days": 90}` newest first (90-day
  retention by `ts`). `detail`: day_move `{side, pct, prev_close, day, level}`, news `{news_id, url,
  source_label, level: null}`.

**WebSocket `alerts` topic → toast.** `{"type": "alert", "alert_id": "9a0e…", "symbol": "AAPL", "kind":
"price_cross", "title": "Research alert · AAPL", "message": "AAPL crossed above 190.00 — last trade 190.05.",
"ts": "2026-10-17T14:00:02+00:00", "price": 190.05, "level": 190.0, "log_id": 12, "logged": true}` (news:
`price`/`level` null). The WPF shows `title` + `message` via the existing HedgeFund toast helper (plain text)
and adds the row to the Alerts log view; not queueable → close 1013 (never dropped, §4.3); alerts missed while
the socket was down are in `GET /research/alerts/log`. Messages: price `"<SYM> crossed above|below <level> —
last trade <price>."`, line `"<SYM> crossed the alert line at <level> (upward|downward) — last trade
<price>."`, day move `"<SYM> is up|down <x.xx> % today[ (UTC day)] — last trade <p> vs previous close <pc>."`,
news `"News for <SYM>: <headline ≤ 200>"`.

**Isolation.** Hook errors are caught in the hub (stream continues); one failing rule never stops the others;
an engine crash ends the task (all alert subscribers released, `status` → `not_running`) and the supervisor
restarts it — the paper engine is never affected. Research never places orders from an alert.

**Tests** (`tests/test_mr_alerts.py` 15, `tests/test_mr_alerts_engine.py` 14, kit `tests/mr_alerts_testkit.py`):
every rule type + 21 invalid bodies, line link rules, atomic drawing link (5 tests: set/clear on create, switch
on/off, delete, system disable; line linked to another alert refused, dangling link replaced; checks repeated
inside the transaction; failed insert rolls the link back), limits + duplicates, PATCH/DELETE, log route, store v7 +
90-day retention + disable audit, routes registered; crossing up/down incl. gap and exact touch, baseline at
start / after a stream gap / after a restart, late trades, direction filter, cooldown + suppression, restart
without a repeated trigger, deleted-before-logging, broken rule isolated, moved line → new level / deleted
line → disabled + demand released, day move once per day and side + next day + missing previous close, news
once per id + cooldown, market-closed status, real hub: demand registration/release, 30-symbol limit message,
end-to-end trigger over the fake stream, engine crash releases demand while the hub runs, failing trade hook.
MR tests 308 OK; full suite OK (split runs); scope guard stage0 + mr0 ok.

**Defaults (user away 2026-10-17) — confirm:** first trade after (re)start/gap never triggers; day move
fires once per day and side; extended-hours IEX trades count; the WPF (not the backend) sets the drawing's
the backend (not the WPF) owns the drawing's `alert_id` (revised 2026-10-17); price_cross `direction` only up|down
(line and day move also any). Resolved 2026-10-17 (MR5 follow-up): drawing POST/PATCH refuse `alert_id` (400), imports strip it
(`alert_id_ignored: true`) — see MR5 notes.

### MR7 WPF notes (implemented 2026-10-17: alerts — editor, Alerts section, alert-line link, toasts)

- **Files.** New `MarketResearch/ResearchView.Alerts.cs` (partial: 🔔 editor, Alerts section + log, alert-line
  wiring, live log rows), `MarketResearch/ResearchAlertNotifier.cs` (app-wide toasts). Changed:
  `ResearchClient.cs` (alert routes + DTOs exactly as "MR7 backend notes"), `ResearchSocket.cs` (`alert`
  messages → `AlertReceived`; `SetAlertsWanted`), `ResearchView.xaml` (🔔 `BellButton` in the tab bar, Alerts page
  = `AlertsPanel`), `ResearchView.xaml.cs` (InitAlerts / section hook / shutdown keeps the socket),
  `ResearchView.Watchlists.cs` (topic `alerts` before `feeds`), `ResearchView.Drawings.cs` (alert-line hooks;
  `alert_id` never sent in drawing POST/PATCH), `ResearchView.Indicators.cs` (bell refresh),
  `Fund/FundNotifications.cs` (+ `ShowResearchAlert(title, message)`: same toast path, honours "mute
  notifications"), `HedgeFundWindow.xaml.cs` (+ `ResearchAlertNotifier.EnsureStarted()` next to the engine toasts).
  Checked statically only (no dotnet on the VM).
- **Socket demand (deviation from §4.3.1 "open only while visible", needed for toasts — default, user away
  2026-10-17 — confirm):** the ONE socket also stays open while ≥ 1 alert rule is enabled (checked every 60 s by
  `ResearchAlertNotifier` and after every alert write in the view), with `hello/visible: false` when no Research
  view is shown (the hub counts it as no view; the alert rules keep the streams on themselves) and the topic
  `alerts`. Closing the Hedge Fund window no longer shuts the socket down (only the view's demand). Alerts that
  fire while the socket is down are not toasted afterwards — they are in the alert log. Alerts run only while
  UFOS.ai runs (backend `runtime.note`, shown in the Alerts section and in the editor).
- **Toasts.** Every WS `alert` → `FundNotifications.ShowResearchAlert(title, message)` (plain text, ≤ 120 / 240
  chars, one per `log_id`), muted by the Hedge Fund "mute notifications" option; the view adds the row to its log
  and counts "ALERTS · n NEW" on the sidebar entry until the section is opened.
- **🔔 editor** (tab bar, per open symbol; "🔔 n" = enabled rules of the symbol): Price crosses (above/below +
  level, prefilled with the last trade), Move today (up/down/either + %), News; quiet time in minutes (seconds to
  the backend), On; edit mode for any rule (kind fixed; alert-line rules: direction + quiet time; the level is the
  line). Only number parsing is checked client-side; ranges/duplicates/limits are the backend's messages,
  verbatim (400 field texts "Not saved: params.level: …", 409 "At most 200 alerts …", "An identical alert …").
- **Alerts section** (sidebar ALERTS): "ALERTS · n/200 · k ACTIVE", runtime note (always), task not running /
  store error banner lines, "At most 200 alerts — delete one first." at the limit, per rule: symbol ·
  description · state word (ARMED / WAITING / COOLDOWN / NOT LIVE / OFF / NOT RUNNING, colour + word) + the
  backend's reason verbatim (incl. "Stream limit: 30 symbols — X is not live. This alert cannot fire while the
  symbol is not live."), last fired (local time), quiet time, last price; buttons ON/OFF (PATCH enabled),
  ⌖ open chart, ✎ edit, × delete (inline confirm; log rows stay). Refreshed every 10 s while shown and ~6 s after a
  trigger / a line move. Log: newest first, 90 days (backend retention), 100 rows + "Load more" (+100 up to the
  route's 500).
- **Alert line (⍾).** When an `alertLine` drawing is stored (POST answer without `alert_id`) the view creates its
  `line_cross` alert (direction any, default quiet time) and says so; the backend links the drawing in the same
  transaction, the view re-reads the symbol's drawings and `upsertDrawing`s that line (bell on the page). After
  every line-alert PATCH/DELETE the same re-read happens. Moving the line (PATCH points) → "Alert line moved to X
  — the alert uses the new level within 5 s." and the list shows `status.level` ~6 s later. Deleting the line →
  "its alert is switched off" (backend), the rule stays listed (OFF + reason) until deleted. The WPF never
  PATCHes `alert_id` (it is removed from drawing POST/PATCH bodies).
- **Open / verify on the PC:** first build; a toast while the Research view is hidden / the Hedge Fund window is
  closed (socket kept for alerts); mute option; emoji 🔔 rendering in Consolas fallback.
- **Review fix L4 (2026-10-17).** While no Research view is shown but the socket stays open for alert toasts,
  `ResearchSocket.CurrentTopics` subscribes only `alerts` (the view's `candle:`/`trades:`/`news:`/`watchlist:`/`feeds`
  topics are unsubscribed at the visibility change, so the hub stops building candles / SIP tail refreshes for an
  invisible chart); on show the full set is subscribed again and the view re-fetches the open chart and news (no
  re-sync follows on an open connection); a re-sync while hidden is ignored by the view (re-fetched on show).

### MR8 backend notes (implemented 2026-10-17: in-module integrations — routes + exact JSON, binding for MR8-WPF)

**Files.** `integrations.py` (read-only views), `integrations_api.py` (`IntegrationRoutes` mixin,
`INTEGRATION_ROUTES` appended to `api.ROUTES`). No store migration (nothing new is stored by Research; the
next free market_research.db version stays v8). Strategy Platform extension (§12 here and in its SPEC):
`backtest/runcard.py` stores the fills on new run cards. No other module's code changed.

**Read-only guarantee.** Other modules' databases (`newsroom.db` = Newsroom + HedgeFund fund tables,
`hedgefund.db` = Strategy Platform) are opened ONLY via `integrations.ro_connect`: SQLite URI `mode=ro` +
`PRAGMA query_only = 1` — any write raises; their store modules' write functions are never called; a missing
file is never created. **No order route exists in Research** (tested: no `/research/*order*` path; every
MR8 route is GET). Errors: source missing / not set up → 503 `{"error": <reason>, "kind":
"IntegrationUnavailable"}`; unknown run / watchlist → 404 `{error}`; bad input → 400 `{error: "invalid
input", errors}`. External text (insider names/roles, release names, "why" texts) → `news_text.plain_text`;
links http(s) only (`safe_link`), else null.

**`GET /research/portfolio`** (no query) — the HedgeFund paper account as the engine stored it:
```json
{"source": "HedgeFund paper account", "read_only": true, "paper": true, "generated_at": "2026-10-17T14:00:00+00:00",
 "cash": 90000.0,
 "equity": {"value": 100500.0, "ts": "2026-10-15T20:00:00+00:00", "drawdown_pct": 0.4, "reason": null},
 "starting_capital": 100000.0,
 "positions": [{"symbol": "AAPL", "qty": 50.0, "side": "long", "avg_price": 200.0, "last_price": 210.0,
                "market_value": 10500.0, "unrealized_pnl": 500.0, "unrealized_pct": 5.0, "realized_pnl": 0.0,
                "opened_at": "2026-10-02T14:00:00+00:00", "updated_at": "2026-10-15T20:00:00+00:00", "reason": null},
               {"symbol": "TSLA", "qty": -10.0, "side": "short", "avg_price": 300.0, "last_price": null,
                "market_value": null, "unrealized_pnl": null, "unrealized_pct": null, "realized_pnl": 0.0,
                "opened_at": null, "updated_at": "…", "reason": "No engine mark for this position yet."}],
 "totals": {"positions": 2, "long_mv": 10500.0, "short_mv": 0.0, "unrealized_pnl": 500.0, "complete": false},
 "engine": {"autopilot": true, "halted": false},
 "marks_note": "Prices are the HedgeFund engine's last marks (written at its runs) — not live quotes.",
 "note": "Read-only view of the HedgeFund paper account. Research places no orders — Buy/Sell opens the HedgeFund manual paper order panel (same confirmations and audit). Paper trading only."}
```
`cash` = sum of the fund cash ledger; `last_price` = the engine's mark (`positions.last_price`); no price is
fetched by Research (a missing mark stays null with the reason; `totals.complete: false`). The WPF may show
live P&L from the `trades:` stream itself, labelled as such. Not set up (no ledger rows / tables) → 503.

**Buy/Sell hand-over (WPF only, no backend route).** Buttons Buy / Sell (and Short / Cover where the
HedgeFund panel offers them) in the symbol header and the Portfolio rows open the HedgeFund window's existing
**manual paper order panel** (`HedgeFundWindow`: `ManualPanel`, `TickerInput`, `SizeInput`, buttons
`ManualBuy/Sell/Short/Cover`) with `TickerInput` = the platform symbol (`AAPL`, `BTC-USD`) and the chosen side
highlighted / focused — size empty, NOTHING submitted automatically. The existing panel keeps its
validation, confirmations, `POST /fund/orders` and audit; paper only, never live. Research passes only
`{symbol, side}`.

**Macro section.** Reuses the Newsroom routes as they are: `GET /newsroom/macro` (FRED series + derived
indicators, `status`) and `GET /newsroom/calendar` (upcoming releases + FOMC) — same token header; no
Research copy (reuse, no duplication). Their `status` / error texts are shown as given.

**`GET /research/symbols/{s}/markers?from=&to=&kinds=`** (`from`/`to` ISO-8601, default the last 365 days
ending now, `from < to`, span ≤ 7400 days; `kinds` comma list ⊆ `insider,calendar`, default both):
```json
{"symbol": "AAPL", "from": "2026-10-01T00:00:00+00:00", "to": "2026-11-01T00:00:00+00:00",
 "kinds": ["insider", "calendar"],
 "items": [{"t": "2026-10-13T04:00:00+00:00", "t_end": null, "kind": "insider", "side": "sell",
            "label": "Insider sell: John Roe (CEO) — 500 sh @ $20.00",
            "detail": {"owner": "John Roe", "roles": ["CEO"], "shares": 500.0, "price": 20.0, "value_usd": 10000.0,
                       "filed_at": "2026-10-14T21:00:00+00:00", "accession": "0001", "url": "https://www.sec.gov/…",
                       "plan_10b5_1": false, "source": "SEC EDGAR Form 4 (Newsroom)"}},
           {"t": "2026-10-20T12:30:00+00:00", "t_end": null, "kind": "release", "side": null, "label": "CPI (inflation)",
            "detail": {"importance": "high", "series_id": "CPIAUCSL", "why": "…", "url": null,
                       "source": "FRED release calendar (Newsroom)"}},
           {"t": "2026-10-28T18:00:00+00:00", "t_end": null, "kind": "fomc", "side": null, "label": "FOMC rate decision",
            "detail": {"meeting_start": "2026-10-27", "sep": false, "importance": "high",
                       "source": "Federal Reserve (Newsroom list)"}}],
 "truncated": false,
 "unavailable": {"earnings": "The Newsroom has no earnings calendar — earnings dates are not shown."},
 "coverage": {"insider": "Form 4 filings the Newsroom keeps (last 5 days of filings)",
              "calendar": "FOMC decisions from the Newsroom's list; FRED release dates of its latest calendar snapshot (upcoming window only)"}}
```
- `kind` ∈ `insider` (Form 4 open-market purchase `P` → `side: "buy"`, sale `S` → `"sell"`; grants,
  exercises, tax withholding and derivative rows are not markers; `t` = 00:00 New York of the transaction
  date), `fomc` (decision day 14:00 ET, from the Newsroom's verified `FOMC_MEETINGS` list — read from
  `newsroom/calendar_collector.py` with `ast.literal_eval`, not duplicated; the snapshot's FOMC rows are
  merged), `release` (FRED release dates of the Newsroom's latest calendar snapshot, `time_hint` "08:30 ET"
  etc. → that New York time, else 00:00). Sorted by `t`; ≤ 2000 items (`truncated`). Calendar markers are
  market-wide (also for crypto).
- `unavailable` per part (always `earnings`): `insider` (crypto: "Form 4 insider filings exist only for
  US-listed companies."; no table yet), `releases` (no snapshot yet: "The Newsroom has no economic calendar
  snapshot yet (FRED key missing or the first poll is pending) — release dates are not shown."), `fomc` (list
  unreadable). The Newsroom keeps Form 4 filings only 5 days (its retention) and the calendar only for the
  upcoming window — older chart ranges have fewer markers (stated in `coverage`). newsroom.db missing → 503.

**`GET /research/backtests/{run_id}/markers?symbol=&from=&to=`** (`run_id` `bt-` + 12 hex else 404;
`symbol` required; `from`/`to` optional filters) — read-only from the run card:
```json
{"run_id": "bt-aaaaaaaaaaaa", "strategy_id": "momentum", "version": 3, "symbol": "AAPL", "read_only": true,
 "items": [{"t": "2026-01-02T05:00:00+00:00", "t_end": "2026-04-02T04:00:00+00:00", "kind": "period_best", "side": null,
            "label": "Best 63-session window: +12.0 %",
            "detail": {"strategy_return": 0.12, "benchmark_return": 0.05, "excess": 0.07}},
           {"t": "2026-01-05T21:00:00+00:00", "t_end": null, "kind": "fill", "side": "buy", "label": "Buy 10 @ 150.00",
            "detail": {"qty": 10.0, "price": 150.0, "entry": true}},
           {"t": "2026-05-04T04:00:00+00:00", "t_end": null, "kind": "drawdown", "side": null,
            "label": "Drawdown 11.0 % (not recovered)",
            "detail": {"trough": "2026-06-10T04:00:00+00:00", "depth": 0.11, "recovered": false,
                       "contributors": [{"symbol": "AAPL", "pnl": -900.0}], "symbol_contributed": true}}],
 "truncated": false, "unavailable": {}}
```
`kind` ∈ `fill` (the symbol's fills; `side` buy|sell|short|cover, `detail.entry` = buy/short), `period_best`,
`period_worst` (§11 attribution `periods.best/worst`, range `t`…`t_end`), `drawdown` (§11 `periods.drawdowns`,
`t` = peak, `t_end` = recovery or null, `detail.trough`); period dates → 00:00 New York (crypto: UTC). The
run must be `done` (else 503 "The run is running — markers exist only for finished runs."). `unavailable`:
`fills` ("This run card was created before entries/exits were stored on cards (Strategy Platform,
2026-10-17) — run the backtest again."), `fills_after_cap` (card fill cap reached), `periods` (no
attribution / its `unavailable` reason). The WPF opens the run from the Strategy Platform ("Open in Market
Research": symbol + run id) and draws these as chart markers (labels plain text).

**`GET /research/watchlists/{id}/universe`** — a watchlist as Strategy Platform universe input:
```json
{"id": "6f1c…", "name": "Tech", "universe": {"symbols": ["AAPL", "SPY"]}, "count": 2, "asset_classes": ["equity"],
 "excluded": [{"symbol": "GONE", "reason": "No longer in Alpaca's asset list."}], "max_symbols": 500, "reason": null,
 "note": "Paste/apply this list as universe.symbols in the Library's strategy editor — Research does not change strategies. Mixed stock/crypto universes get the Strategy Platform's calendar warning."}
```
Symbols in list order, normalised by `core/instruments` (the validator's rule), de-duplicated, ≤ the
Strategy Platform's `MAX_SYMBOLS` (500); uncovered items excluded with the reason; empty → `universe: null`
+ reason. The WPF hands it to the Library's strategy editor (universe field) — the user saves there; no
strategy write from Research.

**Tests** (`tests/test_mr_integrations.py` 12; temp newsroom.db via the Newsroom/fund writers, a minimal
temp `backtest_runs` table): portfolio shape + values + missing marks + not set up + missing file (not
created); read-only (file hash unchanged after reads, the connection refuses INSERT/DELETE, no write SQL /
writer calls in `integrations.py`, no order route, MR8 routes GET only); markers (insider buy/sell in range,
codes/derivatives filtered, plain-text names, unsafe links null, release time hints, FOMC once, earnings
unavailable, crypto, missing snapshot, validation, default window); backtest markers (fills + periods +
drawdown, range filter, other symbol, old card, running run, unknown/bad ids, validation, file unchanged);
universe (route, exclusions, empty, 404/400); run-card fills + cap (Strategy Platform extension). MR tests
325 OK; full suite OK (split runs); scope guard stage0 + mr0 ok.

**Defaults (user away 2026-10-17) — confirm:** separate backtest-markers route; only open-market Form 4
purchases/sales as insider markers; card fills capped at 10 000; portfolio values = engine marks (no live
re-pricing in the backend); Macro = the existing Newsroom routes.

### MR8 WPF notes (implemented 2026-10-17: Portfolio, Macro, Buy/Sell hand-over, markers, backtest link, universe)

- **Files.** New `MarketResearch/ResearchView.Integrations.cs` (Portfolio, Macro, order hand-over, markers, backtest
  link, universe) and `MarketResearch/Chart/ChartHost.Markers.cs` (bridge revision 4 host side, `setMarkers`).
  Changed additively: `ResearchClient.cs` (`GetPortfolioAsync`, `GetMarkersAsync`, `GetBacktestMarkersAsync`,
  `GetWatchlistUniverseAsync` + DTOs), `ResearchView.xaml` (nav PORTFOLIO + MACRO, pages, Buy/Sell/Short/Cover row in the
  header, MARKERS toggle + note), `ResearchView.xaml.cs` (Nav_Checked sections, markers hook after loadBars, shutdown),
  `ResearchView.Workspace.cs` (session sections portfolio/macro, order buttons), `ResearchView.Watchlists.cs` ("Use as
  universe…" in a list's edit mode), `Chart/ChartHost.cs` (`SendMarkers()` in `SendAll`), `HedgeFundWindow.xaml.cs`
  (`ResearchView_OrderHandOverRequested`, `OpenInResearch`), `Platform/BacktestReportView.cs` ("Open in Market
  Research:" one button per symbol of a finished run). Static checks only (no dotnet).
- **Portfolio** (sidebar PORTFOLIO, chip "PAPER ACCOUNT · READ-ONLY"): GET /research/portfolio when shown and every
  60 s while shown + Refresh; cash, equity (+ time), drawdown, start capital, engine state; rows symbol (opens the
  tab) · side · qty · avg · MARK (engine mark, tooltip "not a live quote") · value · unrealized ▲/▼ ± · % · realized,
  per-row reason, totals (incomplete flagged), `marks_note` + `note` verbatim. Failure → "Portfolio unavailable:
  <reason>", nothing old shown. No order function in this section.
- **Macro** (sidebar MACRO): the Financial Newsroom's own `MacroIndicatorsView` and `EconomicCalendarView`
  (/newsroom/macro, /newsroom/calendar) hosted as they are (no copy), tabs MACRO INDICATORS / ECONOMIC CALENDAR,
  created on first use, `Shutdown()` with the view.
- **Buy/Sell/Short/Cover** (symbol header): enabled for symbols the paper engine accepts (`FundAssets.IsValidSymbol`,
  ≤ 14 chars like the panel's ticker box), otherwise disabled with the reason in the tooltip. Click → event
  `OrderHandOverRequested {symbol, side}` → `HedgeFundWindow` switches to Overview, activates itself, sets
  `TickerInput` = symbol, clears `SizeInput`, writes "From Market Research: <Side> <SYM> — enter the size and press
  <Side>. The order is confirmed before it is sent (paper account only)." and focuses the size box. Nothing is
  submitted; the panel's validation, confirmation, POST /fund/orders and audit are unchanged.
- **Markers** (toolbar MARKERS, per tab, default off): GET …/markers?kinds=insider,calendar&from=<oldest bar>&to=now,
  plus GET /research/backtests/{run}/markers?symbol= for a tab opened from a backtest (toggle reads "MARKERS +
  BACKTEST", on automatically). Note line: count, every `unavailable` reason verbatim (always "earnings unavailable:
  The Newsroom has no earnings calendar …", insider for crypto, fills for old run cards), truncation, route errors;
  tooltip lists the first 25 labels with local times. Markers go to the page via `setMarkers` (revision 4) — until the
  page reports revision 4 the note says they cannot be drawn yet. Cleared at every loadBars, re-loaded when on.
- **Open in Market Research** (backtest report of a finished run): one button per non-excluded symbol of the run's
  data snapshot → `HedgeFundWindow.OpenInResearch(symbol, runId)` (owner chain, else the app's Hedge Fund window) →
  Research section (or the pop-out) with that symbol and the run's markers; old run cards show the backend's
  "fills unavailable: … run the backtest again." in the markers note.
- **Use as universe** (watchlist edit mode): GET …/universe → a NEW strategy draft (`NewStrategyTemplate` with
  `universe = {symbols: [...]}`, name "New strategy (<list>)") opened in the Library's `StrategyEditorWindow` — not
  saved; the user saves there (the Library shows it after its next refresh). Excluded symbols + the backend `note`
  are shown in the watchlist message; empty → its `reason`. No strategy write from Research.
- **Pending (chart page):** bridge revision 4 `setMarkers` (see "Revision 4" in the bridge contract) — to be
  implemented by the MR8 chart-page agent; the host side is ready and gated by `ready.revision ≥ 4`.
- **Verify on the PC:** first build; hand-over focus (Research popped out → Hedge Fund window comes to front);
  Newsroom views inside the Research section (sizes, their popups); editor window from Research; markers once the
  page implements revision 4.

### MR8 chart notes (implemented 2026-10-17: chart page — bridge revision 4 `setMarkers`)

- `chart.js` reports `ready.revision: 4` and implements "Revision 4 — MR8 chart markers" exactly: schema
  (≤ 2000 markers, unique `id` ≤ 40 plain chars, `t`/`tEnd` epoch ms with `tEnd ≥ t`, the 7 kinds, sides,
  `label` ≤ 200 plain text, unknown keys → `bad_message`, whole message ignored). Anchoring per draw: the
  bar containing `t` (start ≤ t < start + period), else the first bar starting after `t`, none outside the
  loaded data; ranges from the bar of `t` (clipped to the first bar) to the bar of `tEnd` (or the last bar).
  Markers survive `loadBars` of the same symbol (other timeframe → re-anchored) and are cleared by the page
  on a symbol change (the host also sends `[]` + the new set). Drawing: two data-less engine indicators on
  `candle_pane` (`UFOS_MARK_BANDS` zLevel −1: `period_best` up / `period_worst`, `drawdown` down at 10 %
  alpha behind the candles; `UFOS_MARK_POINTS` in front: ▲ below the bar low for buy/cover in `up`, ▼ above
  the high for sell/short in `down`, ● muted without side, stacked when several share a bar; `fomc`/`release`
  dashed vertical `caution` line + tag "FOMC"/"R"). No figures → the price scale never changes. Hover: the
  legend row `#legend-marks` lists glyph + label (textContent) of the hovered bar's markers (3 + "n more").
- Tests (`run_tests.js`, +21; 229/229 with the test double): revision, snapping in-bar / weekend gap /
  outside range, range clipping + open drawdown, hover legend (plain text, no HTML), same-symbol tf change
  keeps, symbol change and `[]` clear, 11 schema rejections. Screenshots `14_mr8_markers`, `15_mr8_marker_hover`.
- **Verify with `--engine real`:** indicator `draw` params `yAxis.convertToPixel(price)` (used for the glyph
  position; missing → `engine_api`), `xAxis.convertToPixel(dataIndex)`, zLevel −1 (bands behind candles) vs
  2 (glyphs in front), two data-less indicators stacked on `candle_pane` not affecting the y-range, redraw
  after `setMarkers` via the re-sent last bar.

**MR6/MR7 review fixes (2026-10-17, backend).** H1 deadline-capped thumbnail download + bounded DNS (drip-server
tests for body and headers, DNS timeout test); L5 connection pinned to the checked IP with SNI/certificate
check on the host name (test); M2 own thumbnail pool + 503 `ThumbnailBusy` (test: 10 hanging images → 2 busy,
the news route still answers < 1 s); L6 WebP removed from the allowed thumbnail types (tests); M3 alert
triggers flushed before reloading, previous closes on their own schedule (test). New limits:
`NEWS_IMAGE_GATE_WAIT_SECONDS` 5, `IMAGE_EXECUTOR_WORKERS` 2, `IMAGE_QUEUE_MAX` 8. MR tests 333 OK; full suite OK
(split runs); scope guard stage0 + mr0 ok.

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

### Live-test fix (2026-10-18): optional query parameters

Live click-test: every `/research` route with an optional query parameter that was not sent (palette `q`,
bars/news `before`) returned 500 "internal error" — chart and news showed "Research unavailable". Cause:
`data_api._one()` called aiohttp's `MultiDictProxy.getall(key)`, which raises `KeyError` for a missing key
unless a default is passed; the test fakes returned `[]` instead. Fix: `query.getall(key, [])`; the five test
fakes now follow aiohttp semantics (KeyError without default) and `tests/test_mr_query_getall.py` pins it.
Previous file: `data/backups/data_api.py.20261018-pregetall.stale`.
