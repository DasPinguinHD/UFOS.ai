# Global Monitoring — Data Backend

> Part of UFOS.ai, a proof-of-concept portfolio project (not a published product) — see the repo root README.

Python backend that collects real data for the Global Monitoring System's
globe and hands it to the WPF frontend (`globe.html`, WebView2), following
the same shape as `LiveStreamAgent/backend`: independent collectors, JSONL
event logging, WebSocket broadcast.

**Status: live.** Four collectors are implemented and wired up (world-news
RSS, GDACS, OpenSky, Yahoo); none of them needs a key. `OPENROUTER_API_KEY`
is optional and only powers the ✦ AI buttons.

**2026-09-24: Insider Trades (SEC EDGAR) and Macro Indicators (FRED) moved
to the Hedge Fund module's Financial Newsroom** (`HedgeFund/backend/newsroom/`,
`HedgeFund/Newsroom/`) — see the last section of this file. Older dated
sections below that describe them are kept as history.

## Logging — where to actually look when something fails

There are three separate logs, each answering a different question:

| Log | What it's for | Where |
|---|---|---|
| `data/logs/backend.log` | Operational diagnostics — every `logger.info/warning/exception` across `main.py`, every collector, `gdelt_client.py`, `assess.py`, plus aiohttp's own per-request access log (method, path, status, timing) | This directory, rotates at 2MB × 3 backups |
| `data/logs/<date>.jsonl` | Structured data events — every marker emitted and every `source_status` update, one JSON object per line | This directory, one file per day |
| `data/logs/process.log` | The raw stdout/stderr of the backend subprocess as the WPF app sees it — venv setup, `pip install` output, and anything printed before Python's own logging even initializes | Written by `MainWindow.xaml.cs`, only exists when the app auto-starts the backend |

Before this (2026-09-22), operational diagnostics only went to the console
via `logging.basicConfig()` — fine if you're running `python main.py`
directly in a terminal, but the WPF app starts this as a hidden subprocess
and piped its output into `Debug.WriteLine`, which is only visible in an
attached debugger's Output window. Running the built app normally meant
this diagnostic output went nowhere anyone could read it back — effectively
no logging at all. `logging_setup.py`'s `configure_logging()` (called once,
at the top of `main.py`) and `MainWindow.xaml.cs`'s new `process.log`
writer are what fixed that; `backend.log` in particular is the first place
to look for *why* something failed (a collector crash, a failed `/assess`
call, whether a request even reached the server at all).

## What happens when you run it

```
python main.py
```

1. Starts a WebSocket server (`ws://0.0.0.0:8767` by default) and a small
   HTTP API (`http://0.0.0.0:8768`).
2. Starts one background poller per collector (see `collectors/`), each on
   its own schedule. A collector crashing, or being disabled because a
   required API key is missing, never stops the others.
3. Every new item a collector finds is:
   - deduplicated and kept in memory (`events.py`'s `EventStore`, last ~60
     events per category, so re-polling the same feed doesn't spam clients),
   - appended to `data/logs/<date>.jsonl` (`logger.py`),
   - broadcast to every connected frontend as a WebSocket message
     `{"type": "event", "payload": <event>}`.
4. After every poll cycle (success **or** failure) each collector also
   reports its own health as `{"type": "source_status", "payload": {source,
   ok, message, timestamp}}` — this is what drives the traffic-light list
   under "Data Sources" in the app's left sidebar. A red light there means
   exactly what it says: that collector's last poll failed, and the reason
   is in `message`.
5. When a frontend connects, it immediately gets a `"snapshot"` message
   (every event currently held) plus the latest `"source_status"` for every
   collector, so it doesn't have to wait for the next poll cycle to show
   something.
6. `GET /localnews?city=&country=` answers the "click a capital star" local
   news lookup on demand (not a background poller) — see `localnews.py`.
7. `POST /assess` answers the "Assess"/"Sentiment" button on a marker: the
   frontend sends `{category, title, summary, source}` for the currently
   selected marker, this calls OpenRouter with a short, hedged system prompt
   (`assess.py`), and returns `{"assessment": "..."}`. If `OPENROUTER_API_KEY`
   isn't set in `.env`, it answers `503` with an explanatory error instead of
   crashing anything, and the frontend just shows that message in place of
   a result.
8. `GET /events?category=<category>` — the general `/events` route with an
   optional filter. (Until 2026-09-24 it also fed the native Insider Trades
   popup, which moved to the Hedge Fund module.)

To keep the map itself readable with many simultaneous markers,
`events.py`'s `EventStore` caps how many markers per category it keeps at
all (`CATEGORY_LIMITS`, currently `{"flight": 15}`), and `globe.html` only
shows a marker's label on hover or when it's the selected marker, instead of
drawing every label all the time. Markers also lost the translucent "zone"
ring around each core dot (a marker is just its dot now), and events that
share an exact/near-exact coordinate (e.g. several GDELT articles placed at
the same country centroid) get a small, stable per-event jitter in the
frontend so they fan out into a little cloud instead of stacking into one
blob. The user's manual zoom ceiling was also doubled, so a crowded region
can be zoomed into further to spread markers apart on screen.

### Flights: pre-filtering, not just a display cap

The "Notable Flights" layer was showing far more aircraft than actually
useful, and the display cap alone wasn't the real fix — the underlying
callsign filter itself was too broad. `military_registry.py`'s
`MILITARY_CALLSIGN_PREFIXES` used to include several *generic national air
force* prefixes (`GAF`, `FAF`, `HAF`, `PLF`, `KAF`, `CFC`, `IAM`, `SUI`,
`CTM`, `RRR`) that match literally any routine transport or training flight
of that country's air force — i.e. most of what's actually airborne on a
given day, not "notable" by any reasonable definition. That list is now
trimmed to genuinely distinctive strategic-airlift/VIP/special-mission
callsigns (`RCH`, `SAM`, `REACH`, `FORTE`, `DOOM`, `NATO`, `ASCOT`, `GRZLY`,
`VENUS`). `opensky_collector.py` also now requires a minimum altitude
(`OPENSKY_MIN_NOTABLE_ALTITUDE_METERS`, default 1500m) on top of a matching
callsign, so a local training circuit or a routine approach/departure near a
matching aircraft's home base doesn't count either.

### GDELT rate limiting: a real circuit breaker, not just spacing

GDELT is queried both by `gdelt_collector.py` (periodic geopolitics poll)
and by `/localnews` on every capital-star click; a per-IP rate limit means
hitting it from both uncoordinated is what caused "429 Too Many Requests" on
local news lookups. The first fix (a minimum spacing between requests plus a
couple of immediate in-call retries) turned out not to be enough in
practice — GDELT's limiting is a real, IP-wide cooldown, and retrying
straight back into it just adds more requests to a server that's already
saying no. `gdelt_client.py` now works as an actual circuit breaker shared
by both callers: a 429 starts a cooldown window (30s, doubling up to a
10-minute cap on repeated failures) recorded in one place, and *any* GDELT
call made while that cooldown is active fails immediately with no network
request at all — instead of everyone independently guessing whether it's
safe to try again. A success resets the cooldown back to the short end.

### Insider Trades moved off the globe

> **Historical (moved 2026-09-24):** insider trades are no longer part of this module — see "Insider Trades & Macro Indicators moved to the Hedge Fund module" at the end of this file.

Every SEC EDGAR filing is placed at a single fixed Washington D.C. point
(`geo.py`'s `SEC_EDGAR_APPROX_LOCATION` — SEC EDGAR filings don't carry a
real geographic location worth plotting), so as globe markers they were
never really "on a map," just a permanent pile of overlapping dots in one
spot. Insider trades are no longer a globe layer at all: `category="insider"`
events still flow through the backend exactly as before (WebSocket, event
log, `/events`), but `globe.html` no longer maps them to a marker layer. A
"◆ View Insider Trades" button under the left sidebar's "Macro (FRED)"
section opens a native WPF popup (`InsiderTradesWindow`, styled like this
window's own borderless chrome) that fetches `GET /events?category=insider`
and lists the filings as cards. The web page reaches the native window via
WebView2's message bridge (`window.chrome.webview.postMessage`, handled in
`MainWindow.xaml.cs`'s `CoreWebView2_WebMessageReceived`) rather than a
second copy of the UI in HTML.

### Assess/Sentiment button

Two separate bugs found here so far:

1. **CORS preflight** — `POST /assess` sends a JSON body
   (`Content-Type: application/json`), which is not a CORS "simple
   request" — the browser sends a preflight `OPTIONS` first and blocks the
   real `POST` if that preflight response doesn't explicitly allow the
   method and header. The old `_options` handler only ever sent
   `Access-Control-Allow-Origin`, enough for the plain `GET` endpoints but
   not a JSON `POST`. Fixed: the preflight response now also sends
   `Access-Control-Allow-Methods` and `Access-Control-Allow-Headers:
   Content-Type`.
2. **Stale API key** — `config.py`'s `OPENROUTER_API_KEY` (and the other
   `OPENROUTER_*` values) used to be read once, at process start. Adding the
   key to `.env` *after* the backend is already running — a very easy thing
   to do right after first seeing "no key configured" — had no effect until
   a full restart. `assess.py` now re-reads `.env` on every call instead of
   trusting that startup snapshot, so saving `.env` and clicking the button
   again (no restart needed) picks it up immediately.

If the button still doesn't work after both of these: the backend logs the
real underlying error (`logger.warning("AI assessment failed for %r: %s", ...)`,
visible in the `python main.py` console) whenever `/assess` returns an
error, and the frontend shows that same error text under "AI Assessment —
unavailable" in the sidebar — that text is the next thing to check, since it
distinguishes "can't reach the backend at all" from "backend reached, but
OpenRouter itself rejected the request" (bad key, invalid model name,
OpenRouter-side rate limit, etc.), which need different fixes.

### News diversity + clustering (GDELT places by country, not by story)

Two related problems, both stemming from the same root cause: GDELT only
gives a *source country* per article, not a real per-story location.

**Clustering:** the old code placed every article from a country at that
country's single centroid point — for a country the size of the US, that
meant every US story (and GDELT skews heavily toward US/UK sources simply
because that's where most English-language reporting comes from) landed on
the exact same pixel, reading as one solid cluster of overlapping markers
regardless of how different the actual stories were. Fixed in `geo.py`:
`country_point()` replaces the bare centroid with a deterministic,
size-scaled offset (bigger spread for large countries, tight for small
ones) derived from the article's own URL/id, so many US stories now fan out
across roughly the country's real footprint instead of stacking.

**Diversity:** `gdelt_collector.py`'s single keyword query
(`conflict OR military OR war OR ...`) ranks purely by volume, and that
same US/UK English-language volume bias meant actual major conflict zones
(Yemen, Iran, Ukraine, ...) could be crowded out of the results entirely,
even though real English-language coverage of them exists. Two fixes: a
second query (`QUERY_HOTSPOTS`) explicitly names current major
conflict/crisis regions and runs alongside the general query every cycle,
guaranteeing them a look regardless of how they'd rank on keywords alone;
and a per-source-country cap (`MAX_PER_COUNTRY_PER_POLL = 4`) means no
single country — in practice, usually the US — can fill the whole batch and
leave no room for anywhere else.

### GDACS parsing bug (why "Weather & Disasters" showed nothing)

`gdacs_collector.py` looked for `<geo:lat>`/`<geo:long>` as direct children
of `<item>`, but GDACS actually nests them one level deeper, inside a
`<geo:Point>` element. `ElementTree`'s `find()` only searches direct
children unless you ask for `.//`, so that lookup always returned `None` and
every single alert was silently skipped — the collector reported success
(`"N/N alerts placed"` with N=0) while never placing anything. Fixed by
searching `.//geo:lat` / `.//geo:long` instead.

The frontend (`globe.html`) connects to the WebSocket directly via
`new WebSocket('ws://127.0.0.1:8767')` and to the HTTP API via `fetch()` —
no C# relay needed, since WebView2 supports both natively.

## The collectors

See `collectors/README.md` for the source, update cadence, and every known
simplification/caveat of each one. Short version:

| Collector | Source | Needs a key? |
|---|---|---|
| `news_collector.py` | World-news RSS feeds (`news_feeds.py`; replaced GDELT 2026-09-24) | no |
| `gdacs_collector.py` | GDACS GeoRSS/CAP feed | no |
| `opensky_collector.py` | OpenSky Network `states/all` | no (optional, for a higher quota) |
| `yahoo_collector.py` | Yahoo Finance chart API | no |

`sec_edgar_collector.py` and `fred_collector.py` moved to
`HedgeFund/backend/newsroom/` on 2026-09-24 (see the last section).

## Setup

1. `pip install -r requirements.txt`
2. Copy `.env.example` to `.env`. No key is required for the data layers.
   If you want the ✦ AI buttons ("Assess"/"Sentiment", "Sum up local news")
   to return a real AI read instead of "no key configured", set
   `OPENROUTER_API_KEY` (same key as the root app's "Summarize with AI"
   button — see https://openrouter.ai/keys).

> **Note:** this is a personal portfolio / proof-of-concept project, not a
> published product. (`SEC_EDGAR_USER_AGENT` and `FRED_API_KEY` are no longer
> read here — they belong in `HedgeFund/backend/.env` since 2026-09-24; an
> old `.env` that still has them is harmless.)

## Running

```
python main.py
```

Health check: `http://127.0.0.1:8768/health`
Current events (HTTP, in case you don't want to wait for the WS snapshot):
`http://127.0.0.1:8768/events`


## The backend is a long-running Python process — a C# rebuild does not restart it

This tripped up a whole debugging session on 2026-09-22, so it's worth being
explicit about: `dotnet build` (or a full Visual Studio rebuild) only
recompiles the WPF/C# app. It has **no effect whatsoever** on an already-
running `python main.py` process. If Global Monitoring's `MainWindow`
finds a backend already answering on `127.0.0.1:8768/health`, it leaves it
alone and does not start (or restart) its own copy — see
`IsBackendAlreadyRunningAsync()` in `MainWindow.xaml.cs`. That's normally
convenient (you can run the backend yourself in a terminal for easier
debugging, and the app won't fight you for the port), but it means: after
changing *any* `.py` file in this folder, the running backend keeps
executing the old code, silently, indefinitely — across as many
`dotnet build`s and app relaunches as you like — until that specific Python
process is actually stopped and started again.

Symptoms this causes, all seen in the same session because one stale
process was quietly still running from hours earlier:
- A Python-side bug fix (the GDELT circuit breaker, the GDACS parsing fix,
  the CORS preflight fix, `assess.py`'s env-refresh fix, `logging_setup.py`)
  appears to "not work" no matter how carefully it's re-checked, because the
  browser/app is still talking to the pre-fix process.
- The new `data/logs/backend.log` (from `logging_setup.py`) and
  `data/logs/process.log` (from `MainWindow.xaml.cs`, only written when this
  app itself launches the process) stay completely empty/missing, even
  right after "making sure everything is logged again" — because neither of
  those was added until after the currently-running process had already
  started.
- `data/logs/<date>.jsonl` (the structured event log, always present, from
  `logger.py`) keeps growing the whole time and looks perfectly healthy,
  which makes "the backend isn't working" hard to believe — it's working,
  it's just running old code.

**How to tell**: check the *format* of a recent line in
`data/logs/<date>.jsonl` for a given source against that source's current
`report_status(...)` call in its collector — e.g. `gdelt_collector.py`
currently reports `"{placed}/{len(merged)} articles placed (...)"` with a
hotspot-query breakdown; a plain old `"29/30 articles placed"` with no
breakdown means the running process predates that rewrite.

**The fix**: fully stop the actual `python.exe`/`pythonw.exe` process
running `main.py` for this backend (Task Manager, or Ctrl+C in whatever
terminal it's running in) before testing a Python-side change. If the app
itself started it, closing the Global Monitoring window does this
automatically (`TryStopBackendHelper()`); if it was started manually or is
left over from an earlier app session, the app has no way to know that and
will never kill it for you.


## 2026-09-24 round: richer Assess input, GDELT rate limiting (again), event placement

Five fixes from the first real test pass after the backend restart:

- **Flights had too little content for the Assess button.** A flight's
  summary used to be just `"United States — 3284m, heading 252°"` — nothing
  for an LLM to actually say anything about, hence "input too sparse to
  derive substantive insight." `military_registry.py` now maps each known
  callsign prefix to a plain-English description (`describe_callsign()`,
  e.g. `RCH` → "US Air Mobility Command strategic airlift"), and
  `opensky_collector.py` pulls the fuller telemetry OpenSky already gives us
  — ground speed, climbing/descending/level, squawk code, a 16-point compass
  heading — instead of just altitude and a raw heading number.
- **Same problem for Weather & Disasters.** The GDACS collector was building
  its own terse `"EQ — Tonga — Magnitude 5.7M"` tag line and throwing away
  GDACS's own `<description>`, which is already a real sentence with the
  actual substance (e.g. *"On 9/23/2026 2:41:02 PM, an earthquake occurred in
  Tonga potentially affecting 1 thousand in MMI IV. The earthquake had
  Magnitude 5.7M, Depth:10km."*). `gdacs_collector.py` now uses that
  description as the summary, plus the alert level and population-affected
  text GDACS also carries.
- **Long headlines cluttering the globe.** A marker's on-globe label (drawn
  next to it on hover/selection) now truncates to ~46 characters with an
  ellipsis (`truncateLabel()` in `globe.html`). The sidebar's "Selected"
  panel is unaffected — it always shows the full, untruncated title and
  summary; only the label crowding the map itself is shortened.
- **GDELT rate-limited again for /localnews.** Even with the earlier circuit
  breaker, the shared minimum request spacing (5s) still wasn't
  conservative enough. Raised `GDELT_MIN_REQUEST_INTERVAL_SECONDS` to 12s and
  `LOCALNEWS_CACHE_SECONDS` to 30 minutes (from 5) — a capital city's local
  news genuinely doesn't need re-fetching every few minutes, so the real fix
  is needing fewer requests in the first place, not just surviving the ones
  that get rate-limited.
- **News markers placed at the reporting outlet's location, not the actual
  event's location.** GDELT's DOC 2.0 API only gives the source outlet's
  country, never a per-article location — the old code placed every article
  at its *source* country's point, so a story headlined "Renewed fighting in
  north Ethiopia..." picked up by a Syria-flagged outlet landed near Syria,
  nowhere near Ethiopia. `geo.py`'s new `detect_mentioned_country()` scans
  the headline itself for a known country/hotspot name (last mention wins,
  since English headlines are usually "[actor] does [action] to
  [place]" — "Russian strikes on Ukraine" should place in Ukraine, not
  Russia) and `gdelt_collector.py` places the marker there when a mention is
  found, falling back to the old source-country placement otherwise. This is
  a coarse keyword heuristic, not real geocoding — GDELT's separate GKG API
  surface has actual per-article locations, but adopting it is a bigger
  change than this round covers.

## 2026-09-24 (evening): Assess 400 errors, GDELT still 429 after restart

- **Assess failed with "Requested token count exceeds the model's maximum
  context length".** `assess.py` never set `max_tokens`. Some OpenRouter
  upstream providers (seen: GMICloud) then reserve the model's *entire*
  context window (131072 tokens) for the completion, so any input at all
  pushes the request over the limit. It worked earlier the same day because
  OpenRouter had routed to a different provider — hence intermittent. Fixed
  with an explicit `max_tokens=400` (the answer is 2-3 sentences). Provider
  errors are also condensed to one readable line in the sidebar now; the
  full error still goes to `backend.log`.
- **GDELT answered 429 to the very first request after a fresh restart**
  (visible in `backend.log`). GDELT was still penalizing the IP from
  earlier activity, but the circuit breaker's cooldown lived only in memory
  and a restart wiped it, and its 30s initial cooldown was much shorter
  than GDELT's real penalty window. `gdelt_client.py` now persists the
  cooldown deadline to `data/gdelt_cooldown.json` (git-ignored) so it
  survives restarts, starts at 2 minutes (doubling to a 15-minute cap), and
  raises a clean one-sentence `GdeltRateLimited` on a 429 instead of
  aiohttp's raw `429, message=..., url=...` text.
- **Local News fallback.** When the live `/localnews` GDELT lookup fails,
  `main.py` now answers with stories about that country the geopolitics
  collector already holds in memory (matched via
  `geo.detect_mentioned_country()`), with a notice explaining why, and
  without making an extra GDELT request. The error is only shown if
  there is nothing matching in memory either.

## Insider Trades: real Form 4 details + "Sum up most notable" (2026-09-24)

> **Historical (moved 2026-09-24):** insider trades are no longer part of this module — see "Insider Trades & Macro Indicators moved to the Hedge Fund module" at the end of this file.

- **`sec_edgar_collector.py` now parses each Form 4 itself.** The Atom feed
  only gives "4 - Caras Matthew L (0001609077) (Reporting)" and a timestamp —
  no issuer, role, buy/sell or size. For each new accession number the
  collector now fetches the filing's `index.json`, picks the Form 4 XML and
  extracts issuer + ticker, reporting owner + CIK, role (director / officer
  title / 10% owner) and every non-derivative transaction (code, shares,
  price, value, holdings afterwards). Two requests per filing, cached per
  accession so each filing is only fetched once, spaced by
  `SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS`. The feed's duplicate entries
  (every filing appears once for the person and once for the issuer) and
  non-Form-4 types are filtered out. The feed window was raised from 40 to
  100 entries (≈50 filings). The first poll after a restart takes ~1 minute
  because of the detail fetches; later polls only fetch new filings.
- **`POST /insider-summary`** (`main.py` + `insider_summary.py` +
  `assess.summarize_insider_trades()`), used by the popup's new
  "✦ Sum up most notable" button. `insider_summary.py` builds a compact
  one-line-per-filing table sorted by dollar value and computes the
  "unusual" flags in code rather than leaving them to the model:
  `OPEN-MARKET-BUY` (code P), `CROSS-COMPANY` (the same owner CIK is an
  insider of 2+ different issuers in the loaded data — e.g. the CEO of A
  trading B; only detectable within the ~50 filings in memory, not EDGAR's
  full history), `BIG-STAKE-CHANGE` (≥50% change in holdings), `10%-OWNER`,
  and `routine-compensation` for grant/withholding/exercise-only filings.
  The model is asked to lead with the largest trades, then the flagged
  unusual ones.

## Past disaster alerts filtered, layer checkboxes also filter the Event Feed (2026-09-24)

- **GDACS keeps listing alerts long after they've ended** (e.g. "forest fire
  in Brazil, 07/09–20/09" still shown on 24/09). `gdacs_collector.py` now
  drops an alert when GDACS marks it `<gdacs:iscurrent>false` or its
  `<gdacs:todate>` is more than `GDACS_MAX_HOURS_SINCE_END` (default 48h) in
  the past. Point-in-time events like earthquakes (fromdate == todate) stay
  visible for that long after they happened. Each disaster event carries an
  `ends_at` field, and `main.py` prunes aged-out alerts from the store every
  10 minutes and broadcasts a `remove` WebSocket message, so an alert that
  ends while the app is running also disappears without a restart.
- **Layer checkboxes now filter the right-hand Event Feed too**, not just the
  globe (`globe.html`). Unticking the category of the currently selected
  marker also clears the selection.

## Insider Trades: only 3 of 100 feed entries were Form 4s; readable summary; auto-refresh (2026-09-24)

> **Historical (moved 2026-09-24):** insider trades are no longer part of this module — see "Insider Trades & Macro Indicators moved to the Hedge Fund module" at the end of this file.

- **EDGAR's `getcurrent&type=4` is a prefix filter**: it also returns
  424B2/424B3 prospectuses (filed in bulk by big banks), 425, 40-F... A
  100-entry page held only 3 real Form 4 filings, so the AI summary was
  working from 3 trades. `sec_edgar_collector.py` now pages through the feed
  (`start=0,100,...`) until it has 40 Form 4 filings or 6 pages.
- **What was traded**: each transaction now carries its `securityTitle`
  ("Common Stock", "Stock Option (Right to Buy)", ...), and derivative
  transactions (options/RSUs/warrants) are parsed too, not only plain shares.
- **Readable summary**: `insider_summary.py` writes each transaction as
  "BOUGHT on open market 400 x Common Shares at $26.75 = $10.7K, owns 15,200
  afterwards" instead of transaction codes, and the prompt
  (`assess.INSIDER_SYSTEM_PROMPT`) requires one plain sentence per trade —
  who, bought/sold, how many of which security, which company, total and
  per-share price, date, holdings afterwards — under "LARGEST TRADES" and
  "MOST UNUSUAL" (with a one-line reason), and forbids raw codes like (P).
- **Popup auto-refresh every 10s** (`InsiderTradesWindow.xaml.cs`): re-reads
  only the local backend's list (no extra SEC requests — the collector still
  polls SEC on its own schedule), silently: no "Loading…" flash, scroll
  position kept, list only rebuilt if it changed, last list kept if the
  backend is briefly unreachable. An "updated HH:mm:ss" line under the
  subtitle shows it's alive.

## SEC EDGAR: "poll failed: " with an empty message (2026-09-24)

> **Historical (moved 2026-09-24):** insider trades are no longer part of this module — see "Insider Trades & Macro Indicators moved to the Hedge Fund module" at the end of this file.

That was a timeout — asyncio/aiohttp timeout exceptions have an empty
`str()`, so the log line ended after the colon. EDGAR's `getcurrent` feed is
occasionally slow, and with the new multi-page fetch one slow page threw away
the entire poll, leaving the Insider Trades popup empty. Now: 45s timeout per
feed page with one retry; if a *later* page still fails, the entries from the
pages that did load are used anyway; a fully failed poll retries after 30s
instead of waiting a full `SEC_EDGAR_POLL_SECONDS`; and every SEC error is
logged with its exception type (e.g. `TimeoutError`). Filings are emitted one
by one as their Form 4 details arrive, so the popup fills up gradually over
the first minute or two.

## Insider summary: name the traded stock by ticker, strip markdown (2026-09-24)

> **Historical (moved 2026-09-24):** insider trades are no longer part of this module — see "Insider Trades & Macro Indicators moved to the Hedge Fund module" at the end of this file.

A Form 4 always concerns the *issuer's* securities — the stock traded is the
stock of the company the person is an insider of. The summary said things
like "bought 42,857 shares of Class A common stock", which reads as if the
company were unknown. The table line now reads "BOUGHT ... 42,857 AIAI Class A
Common Stock", and the prompt requires the ticker in the sentence ("bought
42,857 AIAI shares (Class A common stock of AIAI Holdings)"). The model also
ignored "no markdown" once ("### **MOST UNUSUAL**", "---"); since the popup
is a plain WPF TextBlock, `assess._strip_markdown()` now removes headings,
bold/italics and horizontal rules as a safety net.

Follow-up: the summary now leads with the company's full name ("bought
42,857 shares of AIAI Holdings (AIAI, Class A common stock)"), the ticker
only in parentheses. `sec_edgar_collector.pretty_company_name()` turns
EDGAR's ALL-CAPS issuer names into normal case ("BAR HARBOR BANKSHARES" ->
"Bar Harbor Bankshares"), keeps the ticker and vowel-less codes upper-case
(AIAI, IBM, CNX, LLC) and drops state-of-incorporation suffixes ("/DE/").
Limit: original mixed case can't be recovered ("CAPSOVISION" ->
"Capsovision", not "CapsoVision").

## The app never actually started this backend by itself (fixed 2026-09-24)

`MainWindow.xaml.cs` located `run_backend.ps1` with one fixed relative path,
`<exe folder>\..\..\..\backend\run_backend.ps1`. That's only correct when
`GlobalMonitoring.exe` runs standalone from `Global Monitoring\bin\Debug\<tfm>`.
Opened from the root UFOS.ai app (the normal way — all module windows run in
the root app's process), the exe folder is the *root* app's
`bin\Debug\<tfm>`, so the path became `UFOS.ai\backend\run_backend.ps1`,
which doesn't exist. The backend was therefore only ever running when started
by hand (`DEBUG_runbackend.bat` / `python main.py`); with that window closed,
the globe had nothing to connect to. Evidence: `data/logs/process.log` (only
written when the app launches the backend) had never been created.

`FindBackendScript()` now walks up from the exe folder and accepts
`<dir>\Global Monitoring\backend\run_backend.ps1` (hosted by the root app)
or `<dir>\backend\run_backend.ps1` when `<dir>` contains
`GlobalMonitoring.csproj` (standalone). If nothing is found, every path it
tried is written to the Backend Log (module "Global Monitoring"). Note that
`run_backend.ps1` runs `pip install -r requirements.txt --upgrade` on every
start, so the first connection after opening the window can take 10-30s.

## GDELT replaced by world-news RSS + Google News (2026-09-24)

**Why:** GDELT's DOC 2.0 API rate-limits per IP with a long penalty window
that extends itself. In practice the city-star local news never returned a
single article, and the geopolitics layer only got through occasionally —
even with the circuit breaker and the persisted cooldown, every backend
restart during development landed inside the penalty. The sections above
about GDELT rate limiting are history now; `gdelt_client.py`,
`collectors/gdelt_collector.py` and `data/gdelt_cooldown.json` were deleted.

**Geopolitics layer** — `collectors/news_collector.py` + `news_feeds.py`:
- Fetches RSS feeds from BBC World, Al Jazeera, The Guardian (World), DW,
  France 24, UN News, NPR World and CBC World concurrently every
  `NEWS_POLL_SECONDS` (10 min). No API key. A failing feed is skipped and
  named in the status light; the others still load.
- Keeps stories that are recent (`NEWS_MAX_AGE_HOURS`, 48h), on-topic
  (`TOPIC_PATTERN`: war, sanctions, election, protest, ... — world feeds also
  carry sport and culture) and locatable. RSS items have no country field,
  so the location comes from the headline (then the description) via
  `geo.detect_mentioned_country()`, which now knows every country in
  `COUNTRY_CENTROIDS` (~100) plus adjectives and capitals (~310 keywords).
  Stories without a recognizable location are not placed.
- Max. 4 stories per country per poll; event source = the outlet's name;
  summary = the feed's teaser paragraph (better Assess input than GDELT's
  bare headline).

**Local news (city stars)** — `localnews.py`: Google News search RSS
(`"<city>" <country> when:3d`), no key. If that fails or finds nothing, the
stories already loaded from the world feeds are searched for the city or
country name (no extra request), with a notice in the panel. Google News
RSS is meant for personal, non-commercial use — fine for this
proof-of-concept; a commercial build would need a licensed news API
(e.g. NewsAPI, GNews, The Guardian Open Platform).

## Stopping the debugger orphaned the backend (fixed 2026-09-24)

Visual Studio's Stop button kills the app process without running
`Window_Closing`, so `TryStopBackendHelper()` never ran and `python.exe`
kept running — with whatever code it had loaded. At the next debug start the
app found it on port 8768 and used it: the GDELT→RSS switch looked like it
"didn't work" because the orphan still ran the GDELT code.

Two fixes in `MainWindow.xaml.cs`:
- **Job Object** (`BackendProcessJob`): the started backend (powershell.exe
  and the python.exe it spawns) is put into a Windows Job Object with
  `KILL_ON_JOB_CLOSE`. When the app process ends for any reason — close,
  crash, debugger stop — Windows terminates the backend too.
- **Stale check** (`HandleAlreadyRunningBackendAsync`): if a backend is
  already running, the app compares `GET /version` → `code_mtime` (newest
  `.py` mtime when that process started, see `main.py`) with the newest
  `.py` file on disk. If the disk is newer, it calls `POST /shutdown`
  (loopback-only) and starts a fresh backend. A backend too old to have
  `/version` can't be restarted this way — the app then shows a message
  asking to end `python.exe` in Task Manager once.

## Assess button: real context instead of "not enough context" (2026-09-24)

The model only ever saw the marker's headline + one-line teaser (or a
callsign + telemetry), and the prompt told it to say so when that was thin —
so "not enough context" was the most common answer. Now:

- The frontend sends the event `id`; `/assess` looks the event up in the
  store and `assess_context.build_context()` adds:
  - **full article text** for news markers (page fetched, `<p>` text
    extracted with stdlib `HTMLParser`, max 6,000 chars, cached 1h; falls
    back to the teaser if the page can't be read — paywall, JS-only, timeout);
  - **approximate location** for flights/disasters/news
    (`geo.nearest_country()`, centroid-based, phrased as approximate);
  - **related live events**: same country (by headline) or within 1,500 km,
    newest first, max 8 — e.g. conflict news near a military flight. Markets
    get the other quotes + latest headlines as backdrop.
- New prompt (`assess.SYSTEM_PROMPT`): 3-5 sentences, connect related events
  when relevant, general background knowledge allowed but phrased as
  background ("typically", "as of my training data"), never invented
  specifics about the event itself; no opening complaint about context.
- The response carries `context_used` (e.g. `["location", "full article
  text", "3 related events"]`), shown under the answer as "Based on: …".

Follow-up (same night), after an assessment of a Sicilian flood mixed things up:
- **We fed it a contradiction:** GDACS' `<gdacs:population>` for floods holds
  a death count ("0 deaths") that contradicted the description ("caused 1
  deaths"). Death counts now come from the description only.
- **Structured facts** for disasters (`event["facts"]`: type, country, alert
  level, severity, ISO start/end dates) — GDACS descriptions mix dd/mm and
  m/d date formats. Meaningless "Magnitude 0" severities are dropped.
- **Related events filtered per category** (`assess_context._RELATED_RULES`):
  a disaster gets disasters ≤1,000 km + news about the same country; a flight
  gets flights ≤500 km + news ≤800 km; news gets disasters/flights ≤500 km +
  same-country news. Max 5, each with its distance, labelled as *candidates*.
- **Prompt rules:** mention a candidate only if clearly connected and never
  comment on the rest; flag contradictions instead of picking one; no
  conclusions from missing information; background only if specific, no
  generic filler.

## City panel: "✦ AI Sum up local news" (2026-09-24)

`POST /localnews-summary {city, country}` (`main.py`) →
`assess.summarize_local_news()`. Reuses the cached `/localnews` lookup, but
with up to 20 headlines (`localnews.SUMMARY_MAX_RECORDS`) instead of the 8
shown in the panel. Input is headlines + outlet + time (+ teaser where the
world-feed fallback has one) — the articles themselves are not fetched
(Google News links are redirects), and the prompt tells the model so: lead
with the 1-3 most significant developments, group headlines about the same
story, never invent details. The response carries `provenance`
(`generator: UFOS.ai/GlobalMonitoring/localnews-summary`) and `headlines`
(count, shown as "Based on: N headlines ..."). Labelled per
`claude/conventions.md` → "AI content labelling".

## News relevance scoring (2026-09-24)

One weak keyword used to be enough to get onto the globe ("war" in "prisoner
of war" → a Nazi-era trial; "rebel" → a story about a Catholic sect; a
pop-star profile). `news_collector.relevance()` now scores headline +
teaser: strong terms (conflict/security AND economy/markets: sanctions,
tariffs, interest rates, inflation, oil prices, default, ...) count 4 in the
headline / 2 in the teaser, medium terms (government, talks, markets, ...)
2 / 1. A story needs a strong term and a score ≥ 4. Culture, sport,
religion, celebrity and WWII-era history are excluded unless the headline
itself has a strong term ("Pope calls for ceasefire in Gaza" stays).
Phrases like "prisoner of war", "cold war", "price war" are neutralized
before scoring. Economy feeds were added (BBC Business, Guardian Business,
DW Business). 13 hand-written test headlines (real misfires + wanted
stories) all classify correctly.

Article text extraction also got a fallback: if the structural parser finds
< 300 chars (a Guardian article came back empty), a plain regex over all
`<p>` elements is used; if the result is still empty, `backend.log` notes
the HTML size and `<p>` count (usually a JavaScript-rendered page).

## Insider Trades & Macro Indicators moved to the Hedge Fund module (2026-09-24)

Neither feature has a real place on a globe (every Form 4 used to sit on one
Washington D.C. point; FRED series are national aggregates shown as a text
ticker), and both are trading-desk information. They now live in the Hedge
Fund module's **Financial Newsroom** (Hedge Fund window → "Financial
Newsroom ▾"):

- Moved to `HedgeFund/backend/newsroom/`: `sec_edgar_collector.py`,
  `insider_summary.py`, the insider-summary prompt (`ai.py`) and an extended
  `fred_collector.py` (6 series with history for sparklines). Endpoints:
  `GET /newsroom/insider`, `POST /newsroom/insider-summary`,
  `GET /newsroom/macro` on `127.0.0.1:8867`.
- WPF: `InsiderTradesWindow` became `HedgeFund/Newsroom/InsiderTradesView`;
  the globe's "Macro (FRED)" ticker became `MacroIndicatorsView`.
- Removed here: both collectors, `insider_summary.py`,
  `assess.summarize_insider_trades()`, `POST /insider-summary`, the SEC/FRED
  settings in `config.py`/`.env.example`, `geo.SEC_EDGAR_APPROX_LOCATION` /
  `FRED_APPROX_LOCATION`; in `globe.html` the "Macro Indicators" layer
  toggle, the "Macro (FRED)" ticker, the "◆ View Insider Trades" button and
  the two source-status rows; in `MainWindow.xaml.cs` the `openInsiderTrades`
  WebView2 message. The keys were copied to `HedgeFund/backend/.env`; the
  leftover `SEC_EDGAR_*` / `FRED_*` lines in this module's `.env` are unused.

## AI cost ledger (2026-09-25)

Every successful OpenRouter completion (`assess.assess_marker` → "Globe · AI
Assess / Sentiment", `assess.summarize_local_news` → "Globe · AI Sum up local
news") appends one JSON line to the shared UFOS.ai ledger
`%LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl` via `cost_ledger.record()` (this
module's own copy of the writer; same spec in every module). Cost comes from
OpenRouter's `usage.cost` (1 credit = 1 USD), read by `assess.usage_info()`;
tokens/cost may be `null` if not reported. Writing never raises — failures are
only logged as warnings. `UFOS_AI_COST_LOG` (full file path) overrides the
location, e.g. for tests.

`/assess` and `/localnews-summary` also return `"usage": {"cost_usd",
"prompt_tokens", "completion_tokens"}`; `globe.html` shows it as a grey line
under the AI caveat ("Cost: $0.0042 · 1,512 in / 874 out tokens (charged by
OpenRouter)", or "Cost: not reported by OpenRouter").

Tests: `python -m unittest tests.test_cost_ledger` (from `backend/`, stdlib only).
