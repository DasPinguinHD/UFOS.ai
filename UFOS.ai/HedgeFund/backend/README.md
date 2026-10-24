# HedgeFund Module

> Part of UFOS.ai, a proof-of-concept portfolio project (not a published product) — see the repo root README.

An independent research project within UFOS.ai:
a multi-agent simulation of a hedge fund decision process (paper trading, no real trades).

> No regulated financial product. For research and demonstration only.

## Status
- **Paper-trading engine (2026-09-26):** package `fund/` — a loop every 5 min
  (1–60) during the US session: Technical/Macro (rules), Sentiment
  (LLM, batch), Risk Manager (rules) → Judge (LLM only when inputs changed,
  daily AI budget; no rule-based stand-in since 2026-10-02 — when the AI
  judge is unavailable or the budget is reached the symbol gets no new
  recommendation and no autopilot order, and the UI says why) →
  recommendation → internal paper broker
  (long/short/cash, Reg T margin, borrow fees, dividends, SEC Rule 201,
  5 bps fees/slippage, orders only in the session). Parameters are versioned
  and apply from the next run. Routes `/fund/*` (token-protected), rule-only
  backtest, CSV exports, Windows toasts. Market data (daily bars, quotes,
  dividends, news) comes from the Alpaca data layer
  (`strategy_platform/data`, bars cached in `data/hedgefund.db`; history
  since 2016) — a failing source pauses the affected symbol with a clear
  reason, no other source is used. **Contract: `fund/SPEC.md`.** The
  notes below about an illustrative dashboard predate this.
- **24/7 crypto (2026-09-27):** `BTC-USD`, `ETH-USD`, `SOL-USD` join the
  default watchlist (existing installs: one-time params migration at start,
  `fund.crypto_rollout`). Crypto trades around the clock with fractional
  quantities ($10 minimum), own fees (`crypto_fees_bps` 20) and position size
  (3 %); longs are spot, shorts simulated perpetual futures with isolated
  margin (50 % / 25 %), liquidation fee and funding every 8 h (positive rate =
  longs pay shorts, so the simulated shorts receive it). Scheduled full cycles
  run only during the US session; outside it only "Run now" runs a full cycle
  (crypto fills at once, equities queue for the open) and a light risk watch
  (no agents/AI/new orders) checks open crypto positions every
  `closed_interval_minutes`. Still paper trading only, not investment advice.
- **Dashboard (agents, verdict, portfolio, execution):** illustrative only —
  hardcoded sample data, no agents or broker connected yet. (The former
  WebSocket placeholder on 8865 was removed in stage 10 — nothing used it.)
- **Financial Newsroom: live.** Insider trades (SEC EDGAR) and macro
  indicators (FRED), moved here from the Global Monitoring module on
  2026-09-24. See below.

## Financial Newsroom

Opened from the Hedge Fund window's toolbar: the **"Financial Newsroom"**
button opens the newsroom window (`HedgeFund/Newsroom/FinancialNewsroomWindow`)
on its first section; the navigation list on the left switches sections.
(Until 2026-09-25 the button was a dropdown submenu with one entry per section.) The dot next to the button
shows the backend status (grey = not started, amber = starting, green =
online, red = offline).

| Section | Collector (`newsroom/`) | Endpoint(s) on 127.0.0.1:8867 | WPF view (`HedgeFund/Newsroom/`) |
|---|---|---|---|
| Insider Trades | `sec_edgar_collector.py` — SEC Form 4 filings, parsed per transaction (every `SEC_EDGAR_POLL_SECONDS`, default 120s) | `GET /newsroom/insider` → `{items, status}`; `POST /newsroom/insider-summary` → `{summary, filings, provenance}` | `InsiderTradesView` — cards, re-reads every 10s, "✦ Sum up most notable" (AI, labelled per `claude/conventions.md`) |
| Macro Indicators | `fred_collector.py` — FEDFUNDS, DGS2, DGS10, T10Y2Y, T10Y3M, CPI YoY (`units=pc1`), core PCE YoY, UNRATE, ICSA, HY spread, VIX; last 24 observations each (every `FRED_POLL_SECONDS`, default 6h) | `GET /newsroom/macro` → `{items, status}` | `MacroIndicatorsView` — value, change vs. previous observation, sparkline, how-to-read note, link to the FRED series; re-reads every 60s |

**Macro stability colours (2026-09-25):** each series carries
`macro.signal = {level, reason}` from `fred_collector.stability_signal()` —
green = stable, orange = watch, red = alert, shown as the sparkline colour plus
a text line. Rules of thumb (a reading aid, not a forecast): 10Y ≥ 5% / 5.5%,
2Y ≥ 5.5% / 6%, either moving ≥ 0.5 / 0.75 pp in ~5 weeks; 10Y–2Y below 0 /
−0.5 pp (inverted); Fed funds ±1 / ±2 pp in 12 months; CPI YoY ≥ 3% / 5% or
≤ 1% / 0%; unemployment Sahm-style rise ≥ 0.3 / 0.5 pp or level ≥ 6% / 7%.

**Derived indicators (2026-09-25, `newsroom/macro_derived.py`):** computed from
the same FRED data and shown as a second block of cards ("Derived
Indicators", same colours and "i" popup): Real Policy Rate (Fed funds − CPI
YoY), Yield-Curve Un-inversion (monthly 10Y−2Y vs. its 24-month low — red when
positive again after an inversion), Sahm Rule (with a dashed 0.5 line) and
Misery Index (unemployment + CPI YoY). For this the monthly series are fetched
with 48 observations and T10Y2Y once more as monthly averages
(`frequency=m`, 36 months); the cards still show the last 24 points.

**More series (2026-09-25, step 2):** T10Y3M (10Y−3M spread), PCEPILFE (core
PCE YoY — the Real Policy Rate uses it; without core PCE the card says
"unavailable: core PCE (PCEPILFE) missing", no CPI substitute since 2026-10-02), ICSA (initial
jobless claims, shown in thousands), BAMLH0A0HYM2 (ICE BofA high-yield OAS —
FRED only keeps a few years of history for ICE series, enough for the cards)
and VIXCLS. 11 raw series + 4 derived; one FRED poll = 12 requests every 6 h.
**Macro regime map (2026-09-25, step 3):** a wide card at the top of the
Macro section. `macro_derived.regime()` places the economy in a 2×2 of
growth momentum (x = 6-month change of the average YoY growth of nonfarm
payrolls `PAYEMS` and industrial production `INDPRO` — fetched for this only,
no cards) × inflation momentum (y = 6-month change of core PCE YoY; without
core PCE the regime is "unavailable: core PCE (PCEPILFE) missing" — no CPI
substitute since 2026-10-02): Goldilocks (green), Reflation / Slowdown (orange), Stagflation
(red). Shows the latest month plus a 12-month trail with hover tooltips,
months spent in the current regime and a "borderline" flag when either axis
is within ±0.1 pp of zero. Delivered as the event `macro-regime`
(`macro.group = "regime"`) in `GET /newsroom/macro`. One FRED poll is now
14 requests every 6 h.

**Insider Trades: 10b5-1 flag, clusters, filters (2026-09-25):** each parsed
Form 4 carries `plan_10b5_1` (+ `plan_10b5_1_source` "checkbox"/"footnote"):
the root-level `<aff10b5One>` checkbox (schema X0508, since 2023) or a footnote
mentioning "10b5-1" (negated mentions like "not ... 10b5-1" are ignored). Such
trades were scheduled in advance: the card says "pre-planned ... under a Rule
10b5-1 trading plan (less informative)", the AI table flags them
`PRE-PLANNED-10b5-1` (never `BIG-STAKE-CHANGE` for planned sales) and the
prompt treats them as context only. `insider_summary.find_clusters()` finds
companies where >= 2 different insiders bought (code P), or >= 3 sold (code S,
10b5-1 plans excluded), on the open market within 14 days (transaction dates);
`GET /newsroom/insider` returns them as `"clusters"` and the AI table flags
the rows `CLUSTER-BUY` / `CLUSTER-SELL`. The WPF view shows a "10b5-1 plan"
chip, a clusters banner (buys gold, sells muted) and a filter row (text over
title + summary, "Open-market purchases only"). Tests:
`python -m unittest tests.test_insider` (offline, stdlib only).

**Economic Calendar (2026-09-25):** third newsroom section ("◇ Economic
Calendar", WPF `EconomicCalendarView`, collector `newsroom/calendar_collector.py`,
source label "FRED release calendar"). Shows the next 45 days of market-moving
US releases plus FOMC rate decisions, grouped by day ("Today", "Tomorrow",
"Wed, Oct 1" + "in N days"). Curated series → their FRED release
(`fred/series/release`, cached for the process lifetime): CPI (`CPIAUCSL`),
jobs report (`PAYEMS`), PCE (`PCEPILFE`), GDP (`GDPC1`) — high importance
(orange dot) — and jobless claims (`ICSA`), retail sales (`RSAFS`), industrial
production (`INDPRO`), JOLTS (`JTSJOL`), PPI (`PPIFIS`), U. Michigan sentiment
(`UMCSENT`). Dates come from one paged `fred/releases/dates` call
(`include_release_dates_with_no_data=true`, filtered to the curated release
ids), with `fred/release/dates?release_id=…` as per-release fallback. Each row
also carries the series' latest value in its headline form ("last: 3.35% YoY
(Aug 2026)": CPI/core PCE `units=pc1`, payrolls `chg`, GDP `pca`, retail
sales / IP / PPI `pch`, the rest as levels). FOMC dates are hardcoded
(`FOMC_MEETINGS`, 2026–2027, from
https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm — **update
yearly**); the decision day is the 2nd meeting day, meetings with the Summary of
Economic Projections get a "SEP / dot plot" chip. Times ("08:30 ET", "14:00 ET",
…) are a static map of *typical* times, not guaranteed. No consensus forecasts
(not freely available). Poll every `CALENDAR_POLL_SECONDS` (default 6 h), ≤ 4
requests in parallel, 15 s timeout each; without the FRED key only the FOMC
dates are shown, with a warning. `GET /newsroom/calendar` →
`{events, updated, window_days, status}`; the view re-reads every 5 min (every
3 s while the first poll is pending or the backend is unreachable). Tests:
`python -m unittest tests.test_calendar` (offline, stdlib only).

**Macro review round (2026-09-25):**
- **Rules from one source:** all thresholds live in `newsroom/macro_rules.py`
  (`RULES`); `stability_signal()` evaluates them and `describe_rules()`
  generates the "i" popup texts from the same entries, so popup and logic can't
  drift apart. Derived indicators use the same table (curve un-inversion keeps
  its custom 24-month rule, described in `CUSTOM_RULE_TEXT`).
- **Official Sahm rule:** `SAHMREALTIME` from FRED (Claudia Sahm's real-time
  indicator) replaces our reconstruction. Since 2026-10-02 there is no
  reconstruction stand-in: without SAHMREALTIME the card says "unavailable:
  official Sahm indicator (SAHMREALTIME) missing". A derived indicator whose
  input is missing is sent as an event with `macro.unavailable` (no value);
  the regime likewise (`macro.regime = null`, `macro.unavailable`).
- **Smoother regime map:** growth and inflation inputs get a 3-month moving
  average before the 6-month momentum is taken (industrial production made
  the dot jump between quadrants almost monthly).
- **Historical context:** each card series is also fetched as monthly
  averages since 2000 (`frequency=m`, `observation_start=2000-01-01`) →
  `macro.long_history` and `macro.context` ("84th percentile since 2000 ·
  highest since Nov 2007"; the "since" part only for the top/bottom 10 % and
  when the previous comparable month is over a year back). Derived indicators
  are computed on the long histories too.
- **Next release:** monthly/weekly series carry `macro.next_release`
  (`{date, release}`) from FRED's release calendar (`series/release` →
  `releases/dates`, fallback `release/dates`); daily series have none.
- **Range switcher** in the view: Recent / 5Y / Since 2000 (monthly averages,
  at most 120 chart points); value, change and colour always stay on the
  recent data.
- **Euro area:** ECB deposit rate (`ECBDFR`), 10-year Bund
  (`IRLTLT01DEM156N`, OECD monthly) and euro-area HICP YoY
  (`CP0000EZ19M086NEST`, `units=pc1`) as their own "Euro Area" block.
- One poll is now ~45 FRED requests every 6 h (4 in parallel; FRED allows
  120/min). Tests: `python -m unittest tests.test_macro tests.test_insider
  tests.test_calendar` (stdlib only, offline, from `HedgeFund/backend`).

**Analysis & Reasoning (2026-09-25):** a newsroom section where the user
picks several items (or a preset template) and gets one AI analysis of how
they fit together. `newsroom/overview.py` (pure, no I/O) turns the data the
other sections already hold into tiles:
- `GET /newsroom/overview` → `{tiles, templates, generated_at, models}`.
  Tile = `{id, group, title, value, trend, change, level, subtitle, as_of,
  facts}` (all strings; `trend` up|down|flat|none, `level`
  ok|watch|alert|info; `facts` = 2-4 plain-text lines with every number the
  model gets). Ids: `regime`, `macro:<SERIES>` (US + euro area),
  `derived:<KEY>`, `insider:sentiment` (open-market buys vs. sells, Rule
  10b5-1 planned trades excluded; "watch" when ≥ 3 buys and buys ≥ half the
  sells, or ≥ 5 distinct buyers), `insider:cluster:<n>` (≤ 4),
  `insider:top:<n>` (3 largest unplanned open-market trades, purchases
  first), `calendar:<date>:<SERIES|fomc>` (next 6 high-importance releases +
  all FOMC decisions). Order: regime, US, euro area, derived, insider,
  calendar; missing data is skipped. Templates (`recession`, `inflation`,
  `rates`, `europe`, `insiders`) only list existing ids and are dropped below
  two tiles. `models` = `{standard, premium}` model ids (never keys).
- `POST /newsroom/analysis` `{tile_ids, tier: "standard"|"premium"}` →
  rebuilds the tiles fresh, keeps existing ids (deduped, max 25); < 2 → 400,
  no key → 503, empty answer / model error → 502; success →
  `{analysis, tiles_used, tier, provenance}` (generator
  `UFOS.ai/HedgeFund/analysis`). The user message is
  `build_analysis_context()`: the selected tiles plus the next 30 days of
  high-importance calendar dates + FOMC (always included), ~1-7k chars.
- Prompt (`ai.ANALYSIS_SYSTEM_PROMPT`, plain text, "•" bullets): BIG
  PICTURE · HOW THE SIGNALS FIT TOGETHER (confirmations and contradictions)
  · SCENARIOS (NEXT 3–12 MONTHS) (base/upside/downside with rough
  probabilities summing to ~100 % and "would be confirmed by") · WHAT TO
  WATCH (only provided dates) · ASSET-CLASS TENDENCIES (historical
  tendencies, hedged; no buy/sell/hold, no price targets) · UNCERTAINTIES &
  LIMITS. Only provided data plus marked background knowledge; 1600 max
  tokens, 120 s timeout.
- Tiers: "Standard" = `OPENROUTER_MODEL`; "Premium" =
  `OPENROUTER_ANALYSIS_PREMIUM_MODEL` (default `anthropic/claude-opus-5.5`,
  costs more per call; any OpenRouter model id). Both re-read from
  `data/settings.json` (`openrouter_model`, `openrouter_analysis_premium_model`) on
  every request. Tests: `python -m unittest tests.test_overview`.

**Cost per analysis (2026-09-25):** OpenRouter reports what each request
cost in `usage.cost` (credits; 1 credit = 1 USD — always included, see
openrouter.ai/docs/use-cases/usage-accounting). `ai.usage_info()` reads it
together with the token counts; `POST /newsroom/analysis` returns it as
`"usage": {"cost_usd", "prompt_tokens", "completion_tokens"}` (values may be
null if not reported) and the view shows "Cost: $0.0123 · 1,512 in / 874 out
tokens" under the analysis (also in Copy / Save as Markdown).

**AI cost ledger (2026-09-25):** every successful OpenRouter completion
("insider-summary" = Insider Trades · Sum up most notable, "analysis" =
Analysis & Reasoning; also when the text came back empty) is appended by
`newsroom/cost_ledger.py` `record()` — called from `newsroom/ai.py` — to the
shared ledger `%LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl` (one JSON object per
line: `ts` (UTC ISO-8601), `module` "HedgeFund", `feature`, `feature_label`,
`provider` "OpenRouter", `model` actually used, `cost_usd`, `prompt_tokens`,
`completion_tokens` (null if not reported), `status` "ok"). Same format in
every module (each keeps its own copy of the writer); only the backend that
makes the call writes it, never the WPF side. A failed write is only logged.
Env `UFOS_AI_COST_LOG` overrides the path (tests). `POST
/newsroom/insider-summary` now also returns `"usage"`, and the Insider
Trades view shows the same grey cost line under its AI summary. Tests:
`python -m unittest tests.test_cost_ledger`.

**Local SQLite store (2026-09-25):** the backend keeps its data in one
SQLite file per module: `HedgeFund/backend/data/newsroom.db` (moved there the
same evening from `%LOCALAPPDATA%\UFOS.ai\data\hedgefund.sqlite` — see
"Local storage (SQLite, 2026-09-25)" below; env `UFOS_HEDGEFUND_DB` overrides
the full path — used by the tests). Code:
`newsroom/db.py` (stdlib `sqlite3`, WAL, a connection per call, schema version
in `PRAGMA user_version` with idempotent migrations). A DB error never stops
the backend: it is logged and the backend continues in-memory only.

| Table | Content | Retention |
|---|---|---|
| `insider_filings` | every Form 4 seen (`accession` PK, `filed_at` in UTC, ticker, issuer, owner, full event JSON, `first_seen`, `last_seen`) | 5 days (by filing time) |
| `macro_observations` | raw observations per `(series_id, units, frequency, date)` — all FRED series (+ `USREC`) and Eurostat `EUROSTAT_IP_EA20` (`PCH_SM`) | kept (small) |
| `calendar_snapshot` | the latest Economic Calendar poll (one row) | latest only; events until event date + 1 day |
| `ai_results` | every AI output: insider summary + Analysis & Reasoning (`feature`, `model`, `tier`, `input_summary` JSON — tile ids/titles or the insider table —, `output_text`, `provenance_json`, `usage_json`) | 90 days |
| `meta` | `last_prune`, `last_ok:<source>` (last successful poll), `obs_full:<series>:<units>` (last full refetch), FRED release ids + next release dates | — |

- **Startup:** open → migrate → prune → load. Insider filings of the last
  `INSIDER_DISPLAY_DAYS` (default 5) go into the display list (their parsed
  details are reused, so a restart doesn't fetch them again), the macro cards,
  derived indicators and the regime map are rebuilt from the stored
  observations (they show immediately, before the first FRED poll), and the
  calendar comes from its snapshot (past dates dropped).
- **Insider Trades:** every poll upserts all filings (new → inserted, known →
  `last_seen` updated). The view only gets filings filed within the last
  `INSIDER_DISPLAY_DAYS` (at most `INSIDER_DISPLAY_MAX`, default 300); the
  database keeps them `INSIDER_RETENTION_DAYS` (default 5 — no long-term
  archive any more, set `insider_retention_days` higher in `data/settings.json` to keep one). Clusters and
  the AI summary work on the displayed set.
- **FRED incremental:** each series is fetched once as published since 2000,
  then each poll asks only for observations from (latest stored date − 400
  days) on (FRED revises CPI, payrolls, IP …) and replaces that tail; a full
  refetch happens when nothing is stored or 7 days after the last one (per
  series). Card histories, the monthly long histories (daily/weekly series =
  local monthly averages, as FRED's `frequency=m` returned before) and all
  derived values are built from the stored data. Release ids are cached in
  `meta`. One poll = 18 observation requests + 1 `releases/dates` + 1
  Eurostat (≈ 20, was ≈ 45, and far smaller responses); the log line
  "FRED poll done …: N HTTP requests (M full history refetches)" shows it.
- **AI history:** `GET /newsroom/ai-history?feature=insider-summary|analysis&limit=20`
  → `{items: [{id, created_at, feature, model, tier, input_summary,
  output_text, provenance, usage}], stored}` newest first (limit 1–200). Read
  only; the original model/time label stays in `provenance`.
- **Pruning:** at startup and every 24 h while running.
- **Reset:** close the app (so the backend stops), then delete
  `data/newsroom.db` (and `-wal` / `-shm` next to it if present). The next start
  recreates it and refetches everything once.

**Macro regime — extended view (2026-09-25):** the `macro-regime` event's
`macro.regime` keeps all its keys and adds `extended` (for the WPF "Expand"
view), built by `macro_derived.regime_extended()` from the stored long
histories with exactly the card's method (3-month smoothing, 6-month momentum):

```
"extended": {
  "trail_long": [{"date", "x", "y", "quadrant"}, ...],   // monthly since 2000 (PAYEMS/INDPRO YoY × core PCE YoY; [] without core PCE)
  "recessions": [{"start": "YYYY-MM", "end": "YYYY-MM"}], // FRED USREC (monthly 0/1) since 2000; a running one ends at the last month
  "components": [{"name", "axis", "latest", "six_months_ago", "change", "unit": "%", "date"}, ...],
      // "Nonfarm payrolls (YoY)" / "Industrial production (YoY)" (axis "growth"),
      // "Core PCE inflation (YoY)" or "CPI inflation (YoY)" (axis "inflation"); smoothed values
  "euro": {"x", "y", "quadrant", "label", "date", "trail": [last 12 {date, x, y, quadrant}],
           "inputs": "Eurostat industrial production (euro area, YoY) × HICP inflation (YoY)",
           "source_url": "https://ec.europa.eu/eurostat/databrowser/view/STS_INPR_M/default/table"} | null,
  "euro_error": "unavailable: euro-area point could not be computed (<Type>)" | null,
  "phase_behaviour": {"goldilocks"|"reflation"|"stagflation"|"slowdown":
                        {"summary", "assets": [{"asset", "tendency"} × Equities, Government bonds,
                                                Credit, US dollar, Commodities, Sectors]},
                      "disclaimer": "Historical tendencies, not a forecast and not investment advice."},
  "note": "…how the component values relate to the axes…"
}
```

Euro-area growth = Eurostat `sts_inpr_m` (EA20, B-D, calendar adjusted,
`PCH_SM` = YoY %), JSON-stat API without key, polled with the FRED collector
(from the latest stored month − 400 days); inflation = FRED
`CP0000EZ19M086NEST` YoY. Eurostat failures only leave `euro` null (logged).
`phase_behaviour` is static, hedged textbook tendencies (no single stocks, no
AI). Tests: `python -m unittest tests.test_store` (temp DB, offline).

**Local storage (SQLite, 2026-09-25):** decisions from the storage interview,
built on `newsroom/db.py` (the store above; no second storage module).

- **File:** `HedgeFund/backend/data/newsroom.db` — one database per module in
  its backend data folder; `*.db`, `*.db-wal`, `*.db-shm`, `*.db.corrupt-*`
  are git-ignored (`backend/.gitignore`). If it doesn't exist yet and the old
  `%LOCALAPPDATA%\UFOS.ai\data\hedgefund.sqlite` does, the old file is copied
  once (SQLite backup API) and left in place — delete it by hand afterwards.
- **Schema:** `PRAGMA user_version` = 2 (v1 = newsroom tables, v2 = trading
  tables below). WAL journal, a short-lived connection per call.
- **Retention** (pruned at every start and every 24 h; env overrides in `config.py`):

| Data | Table | Kept |
|---|---|---|
| Insider filings | `insider_filings` | 5 days by `filed_at` (unparsable time → `first_seen`) — `INSIDER_RETENTION_DAYS` |
| Calendar events | `calendar_snapshot` | until event date + 1 day |
| AI summaries / analyses | `ai_results` | 90 days — `AI_RESULTS_RETENTION_DAYS` |
| FRED / Eurostat observations | `macro_observations` | everything FRED returns (history since 2000 is reference data for percentiles, regime trail and derived indicators — deliberate deviation from "market data 2 years"; small) |
| Prices | `prices` | 2 years |
| Signals, orders, positions, equity | `signals`, `orders`, `positions`, `equity_curve` | kept (no pruning yet) |

- **Cache first, then reconcile:** at start the stored insider filings, macro
  cards and calendar are loaded before the collectors start, so `/newsroom/*`
  answers immediately. Each source with cached data reports
  `{"ok": true, "message": "from cache (updated HH:MM) — refreshing…", "cached": true}`
  until its first live poll; polls upsert by stable id (insider accession,
  macro series/date, the calendar snapshot row). Every successful poll stores
  `meta.last_ok:<source>`.
- **First poll skipped when fresh:** FRED and the calendar don't poll at start
  while their last successful poll is younger than `FRED_POLL_SECONDS` /
  `CALENDAR_POLL_SECONDS` (status: "from cache (updated HH:MM) — next refresh
  HH:MM"; logged). Without the FRED key they start at once (and report the
  missing key). SEC EDGAR always polls at start.
- **AI outputs:** every successful insider summary and analysis is stored in
  `ai_results` (text, model, tier, `input_summary` incl. `tiles_used`, usage,
  provenance, `created_at`) — persistence only, no UI.
- **Errors:** every DB call is guarded — an error is logged and never breaks a
  collector or an endpoint. A corrupt file is renamed to
  `newsroom.db.corrupt-<UTC timestamp>` (with `-wal`/`-shm`) and a fresh
  database is created; a locked/unopenable file is left alone and the backend
  runs in-memory only.
- **`GET /newsroom/status`** now returns `{"sources": [per-source status],
  "storage": {"path", "size_bytes", "schema_version", "rows": {table: n}}}`
  (`storage` is `null` when the store is off; no secrets).
- **Trading tables — prepared for the Hedge Fund trading work, not used yet:**
  `prices(symbol, ts, open, high, low, close, volume, source; PK symbol+ts)`,
  `signals(id, ts, source, symbol NULL = market-wide, kind, value, level, payload_json)`,
  `orders(id, ts, symbol, side, qty, order_type, limit_price, status, broker,
  broker_order_id, mode 'paper'|'live' DEFAULT 'paper', rationale, provenance_json)`,
  `positions(symbol PK, qty, avg_price, updated_at)`,
  `equity_curve(ts PK, equity, cash, mode)`.
- Tests: `python -m unittest tests.test_storage` (temp directory, offline).

**Expanded regime map (2026-09-25):** second pass on the WPF "Expand" view
(button top-right of the regime card; popup under the button that closes via
Collapse, Esc, pointer-leave timer or moving the window; shown inline below the
card instead when the view is ≥ 1400 px wide and the window ≥ 900 px tall —
same reparenting as LiveStreamAgent's market grid). `extended` additionally carries:

```
"episodes": [{"label": "GFC trough (Mar 2009)", "date", "x", "y", "quadrant"}, ...],
    // fixed reference months (macro_derived.EPISODES: Nov 2001, Jun 2005, Mar 2009, Apr 2020,
    // Jun 2022) read from trail_long — same method as today's point; only months in the data
"history": [{"date", "quadrant"}, ...],          // monthly since 2000 (= trail_long without x/y)
"component_series": {"payrolls_yoy", "indpro_yoy", "inflation_yoy": [{"date", "value"}] (raw YoY %),
                     "growth_momentum", "inflation_momentum": [...] (the smoothed axes, pp),
                     "inflation_name": "core PCE" | "CPI"},   // last 36 months each
"asset_tendencies": {"title", "columns": [Equities, Govt bonds, Credit, USD, Commodities],
                     "rows": [{"quadrant", "label", "cells": [{"asset", "direction": "up|down|mixed", "text"}]}],
                     "disclaimer": "Historical tendencies, simplified; not a forecast, not investment advice."}
```

No extra FRED requests (all from the stored long histories). The expanded map
defaults to a 36-month trail with quarter labels ("Q1 '25"); it keeps the
trail's own scale, so an episode outside it is pinned to the edge with an
arrow in its label and its real values in the tooltip. `asset_tendencies` is
static, deliberately coarse textbook data — the table's single source.

Also: `GET /newsroom/status` (per-source status), `GET /health`,
`GET /version` + loopback-only `POST /shutdown` (stale-backend check, same
mechanism as Global Monitoring).

**Keys** (since 2026-10-02: Windows Credential Manager only — enter or import
them in Hedge Fund → Settings → Keys; see `strategy_platform/SPEC.md` §2):
SEC EDGAR user agent (required by SEC — name + contact email), FRED API key
(free registration), OpenRouter API key (only for the AI features), Alpaca
paper keys. No `.env` any more (stage 10, 2026-10-24): nothing is read from
.env or the environment; old .env files are imported once in the app (⚙ Keys &
data); non-secret settings go into the optional `data/settings.json` (keys and
defaults: `config.SCHEMA`; an invalid file refuses the start). The backend reads the keys at use time, so a new key applies
without a restart. A collector without its key reports "… is not set — enter
it in Hedge Fund → Settings → Keys" as its status and re-checks every
minute; the AI routes answer 503 with that message; nothing crashes.

**Backend lifecycle:** the Hedge Fund window starts this backend when it
opens (`Newsroom/NewsroomBackend.cs` → `run_hedgefund_backend.ps1`) and stops
it when it closes. The process tree is bound to the app with a Windows Job
Object, so a crash or a stopped debugger never leaves an orphaned
`python.exe`. An already-running backend is reused only if its code is
current (`/version` vs. the `.py` files on disk), otherwise replaced. Output
goes to the app-wide "Backend Log" window (module "HedgeFund") and to
`data/logs/process.log`; Python logging goes to `data/logs/backend.log`.
The first start creates the venv and installs `requirements.txt` (incl.
pandas and the Alpaca SDK) — that can take a few minutes; the status line says so
("installing Python packages"). Later starts only install when
`requirements.txt` changed (see "Python environment" below).

**Adding a section:** a collector in `newsroom/`, an entry in `main.py`'s
`NEWSROOM_SOURCES` plus a GET endpoint, a UserControl in
`HedgeFund/Newsroom/`, and one line in `NewsroomSections.cs` — the window's
navigation is built from that list (the first entry is the start section).

## Python environment (since 2026-09-25)

**Started from the UFOS.ai app:** the environment is prepared once at app
start (`Shared/PythonEnvironment.cs`, in the background, one module after the
other: create the venv if missing, `pip install -r requirements.txt --upgrade`
only when the stamp doesn't match). The module's launcher (`Newsroom/NewsroomBackend.cs`) awaits
`PythonEnvironment.WaitForModuleAsync` before starting the script with
`-SkipInstall` (no venv creation, no pip); a standalone module exe prepares the
venv itself at that point. The rules below apply when you run the script by hand
(it writes the same stamp, so the app doesn't reinstall afterwards).

`run_hedgefund_backend.ps1` no longer runs `pip install --upgrade` on every start. It
installs `requirements.txt` (without `--upgrade`) only when that is
**required**: `.venv` is missing, `requirements.txt` changed since the last
successful install (SHA-256 in `.venv/ufos-requirements.sha256`), or the last
install didn't complete (`.venv/ufos-install-pending`). `-RecreateVenv` still
rebuilds the venv; `-Upgrade` runs `pip install -r requirements.txt --upgrade`;
`-EnvOnly` prepares the environment without starting `main.py`.

Every run holds `.venv.lock` (git-ignored, deleted automatically — the handle
is opened with DeleteOnClose) while it checks or changes the venv, so the
backend never starts while an install/update of the same venv is running; it
waits instead. A leftover lock counts as stale when its PID is gone or it is
older than 30 minutes. The script prints `UFOS_ENV: waiting | installing <reason>
| upgrading | changed <n> | failed <reason> | ready` marker lines, which
`Newsroom/NewsroomBackend.cs` turns into a status hint ("Installing Python packages…" /
"Waiting for the Python package update to finish…").

Optional upgrades are manual only: root app → Settings (gear) → "Update Python
packages" (upgrades all module venvs one after another; a running backend is stopped via `POST /shutdown` first and restarts the next time the Hedge Fund window needs it).
