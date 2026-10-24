# Collectors

> Part of UFOS.ai, a proof-of-concept portfolio project (not a published product) — see the repo root README.

Each file here is one independent poller for one of this module's data
sources (`docs/global-monitoring-system-plan.md`, Section 3). All of them
share the interface described in `__init__.py`:
`async def run(emit, report_status) -> None`, looping forever.

## gdelt_collector.py — Geopolitics & Conflict

- **Source:** GDELT DOC 2.0 API (`api.gdeltproject.org/api/v2/doc/doc`), free,
  no key.
- **Query:** a fixed English-language keyword search for
  conflict/military/unrest terms, sorted by newest, polled every 15 minutes
  by default (`GDELT_POLL_SECONDS`) — matches GDELT's own indexing cadence,
  so polling faster buys nothing.
- **Placement:** GDELT's article list gives a `sourcecountry` field (a full
  country name, not coordinates). This is mapped to a country centroid via
  `geo.country_centroid()`. **Caveat:** this is one dot per country, not the
  actual event location — an article about Kharkiv and one about Lviv both
  land on the same Ukraine centroid. Articles from a country not in
  `geo.COUNTRY_CENTROIDS` are dropped, not guessed at.
- **Category:** `geopolitics` → red layer.

## gdacs_collector.py — Weather & Disasters

- **Source:** GDACS GeoRSS/CAP feed (`gdacs.org/xml/rss.xml`), free, no key.
- **Placement:** the feed itself carries real `<geo:lat>`/`<geo:long>` per
  event — no centroid table needed, this is the most precisely located of
  the six sources.
- **Coverage:** earthquakes, storms/cyclones, floods, volcanoes, droughts —
  everything GDACS tracks, not just earthquakes. Only events past GDACS's
  own significance threshold appear at all (it is not a firehose).
- **Category:** `disaster` → amber layer.

## opensky_collector.py — Notable Flights

- **Source:** OpenSky Network `states/all` (`opensky-network.org/api`).
  Works anonymously (rate-limited, ~400 weight-1 credits/day ≈ one call
  every ~4 minutes) or authenticated via OAuth2 client-credentials if
  `OPENSKY_CLIENT_ID`/`OPENSKY_CLIENT_SECRET` are set, for a higher quota.
- **Filtering:** OpenSky does **not** flag government/military aircraft —
  `military_registry.py` is a small, self-maintained list of callsign
  prefixes (RCH, SAM, NATO, ASCOT, GAF, …) matched against each flight's
  callsign. This is a heuristic that needs periodic upkeep, not an
  authoritative registry — see that module's docstring.
- **Category:** `flight` → violet layer.

## sec_edgar_collector.py / fred_collector.py — moved

Insider Trades (SEC EDGAR) and Macro Indicators (FRED) moved to the Hedge
Fund module's Financial Newsroom on 2026-09-24 — see
`HedgeFund/backend/newsroom/` and `HedgeFund/backend/README.md`.

## yahoo_collector.py — Markets

- **Source:** Yahoo Finance's chart API
  (`query1.finance.yahoo.com/v8/finance/chart/<symbol>`), the same
  endpoint/header pattern already used in
  `LiveStreamAgent/backend/market_data.py` ("the old Yahoo scraping").
  Free, no key, ~15 minutes delayed.
- **Tracked indices:** S&P 500 (New York), FTSE 100 (London), SSE Composite
  (Shanghai), Nikkei 225 (Tokyo), DAX (Frankfurt) — see
  `geo.FINANCIAL_CENTERS`.
- **Category:** `markets` → blue layer.
