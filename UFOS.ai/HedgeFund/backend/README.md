# HedgeFund Module

> Part of UFOS.ai, a proof-of-concept portfolio project (not a published product) — see the repo root README.

An independent research project within UFOS.ai:
a multi-agent simulation of a hedge fund decision process (paper trading, no real trades).

> No regulated financial product. For research and demonstration only.

## Status
- **Dashboard (agents, verdict, portfolio, execution):** illustrative only —
  hardcoded sample data, no agents or broker connected yet. The WebSocket on
  `WS_PORT` (8865) is a placeholder for the agent pipeline.
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
PCE YoY — the Real Policy Rate now uses it, CPI as fallback), ICSA (initial
jobless claims, shown in thousands), BAMLH0A0HYM2 (ICE BofA high-yield OAS —
FRED only keeps a few years of history for ICE series, enough for the cards)
and VIXCLS. 11 raw series + 4 derived; one FRED poll = 12 requests every 6 h.
**Macro regime map (2026-09-25, step 3):** a wide card at the top of the
Macro section. `macro_derived.regime()` places the economy in a 2×2 of
growth momentum (x = 6-month change of the average YoY growth of nonfarm
payrolls `PAYEMS` and industrial production `INDPRO` — fetched for this only,
no cards) × inflation momentum (y = 6-month change of core PCE YoY, CPI as
fallback): Goldilocks (green), Reflation / Slowdown (orange), Stagflation
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
requests in parallel, 15 s timeout each; without `FRED_API_KEY` only the FOMC
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
  indicator) replaces our reconstruction, which stays only as a fallback (the
  card's formula then says "own calculation").
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
  costs more per call; any OpenRouter model id). Both re-read from `.env` on
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

Also: `GET /newsroom/status` (per-source status), `GET /health`,
`GET /version` + loopback-only `POST /shutdown` (stale-backend check, same
mechanism as Global Monitoring).

**Keys** (`.env`, see `.env.example`): `SEC_EDGAR_USER_AGENT` (required by
SEC — name + contact email), `FRED_API_KEY` (free registration),
`OPENROUTER_API_KEY` (optional, only for the AI summary). A collector
without its key reports itself as failed and the view shows why; nothing
crashes.

**Backend lifecycle:** the Hedge Fund window starts this backend when it
opens (`Newsroom/NewsroomBackend.cs` → `run_hedgefund_backend.ps1`) and stops
it when it closes. The process tree is bound to the app with a Windows Job
Object, so a crash or a stopped debugger never leaves an orphaned
`python.exe`. An already-running backend is reused only if its code is
current (`/version` vs. the `.py` files on disk), otherwise replaced. Output
goes to the app-wide "Backend Log" window (module "HedgeFund") and to
`data/logs/process.log`; Python logging goes to `data/logs/backend.log`.
The first start creates the venv and installs `requirements.txt` (incl.
pandas/yfinance) — that can take a few minutes; the views say so.

**Adding a section:** a collector in `newsroom/`, an entry in `main.py`'s
`NEWSROOM_SOURCES` plus a GET endpoint, a UserControl in
`HedgeFund/Newsroom/`, and one line in `NewsroomSections.cs` — the window's
navigation is built from that list (the first entry is the start section).
