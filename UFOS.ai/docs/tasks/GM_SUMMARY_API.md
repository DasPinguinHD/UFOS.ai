# GM_SUMMARY_API — Global Monitoring tile summary (Agent H, 2026-10-24, contract for W — answers TILES_API §9 Q5)

A pure read for the MainWindow tile: events of the last N hours counted by category plus the feed status. The globe
keeps using `GET /events`, which still wakes stale collectors. The tile uses only this endpoint.
Implementation follows after the lead confirms the R2+R3 commit (new scope label with the exact Global Monitoring files).

## Endpoint
`GET /events/summary?hours=24` on the Global Monitoring HTTP API (127.0.0.1, same port as `/events`).
- **Auth:** token-protected like every route except `/health` and `/version` (api_auth: `X-UFOS-Token`, read with
  Shared/BackendAuth.cs). Missing or wrong token → 401 from the middleware.
- **`hours`:** optional integer 1–168, default 24. Anything else → 400 `{"error": "hours must be an integer 1-168"}`.
  It is never clamped silently.
- **No side effects:** no `_refresh_stale_sources()`, no collector wake-up, no network, no AI call, no DB read or
  write, no log line per request. It reads only the in-memory event store and the in-memory feed status, the same
  data the globe gets on connect. A test asserts that `refresh.request_refresh` is never called and that storage
  is not touched.

## Response 200
```json
{
  "schema": 1,
  "as_of": "2026-10-24T17:05:00Z",
  "window_hours": 24,
  "since": "2026-10-23T17:05:00Z",
  "total": 57,
  "undated": 0,
  "categories": [
    {"category": "geopolitics", "source": "news",    "count": 31},
    {"category": "disaster",    "source": "gdacs",   "count": 4},
    {"category": "flight",      "source": "opensky", "count": 18},
    {"category": "markets",     "source": "yahoo",   "count": 4}
  ],
  "sources": [
    {"source": "news",    "ok": true,  "message": "42 headlines", "timestamp": "2026-10-24T17:01:12Z", "cached": false},
    {"source": "gdacs",   "ok": true,  "message": "...",          "timestamp": "...",                  "cached": false},
    {"source": "opensky", "ok": false, "message": "OpenSky client id/secret missing - set them in Keys & data",
     "timestamp": "...", "cached": false},
    {"source": "yahoo",   "ok": null,  "message": "starting…",    "timestamp": null,                   "cached": false}
  ]
}
```

### Field rules
- **Time strings:** `as_of` and `since` are UTC ISO strings with `Z`. `since = as_of − hours`.
- **`categories`:** always these 4 entries in this fixed order, with count 0 included. An event category the backend
  does not know yet is appended after them in alphabetical order, with `"source": null`. It is never dropped.
- **Event time:** `observed_at` first, then `timestamp` (normalised with `storage.norm_ts`). An event is counted when
  its time is ≥ `since`.
  - Events with no usable time are not put in a category. They are counted in `undated`, so they are visible but
    not guessed.
  - `total` = the sum of the category counts (`undated` excluded).
- **What is counted:** the events the backend currently holds in memory, i.e. what the globe shows, including events
  restored from the local cache at start. It is not a historical count from the database.
  - Flights: there is one entry per aircraft, refreshed on every poll. So `flight` = aircraft whose last position lies
    in the window, not the number of polls.
- **`sources`:** one entry per feed in collector order (news, gdacs, opensky, yahoo). It is the same status object the
  globe receives as `source_status`.
  - `ok`: `true` = last poll fine; `false` = last poll failed (`message` says why, readable, no secrets);
    `null` = not polled yet ("starting…").
  - `cached: true` = the status comes from the local cache after a restart ("from cache (updated HH:MM) —
    refreshing…").
  - `timestamp` = time of that status (UTC ISO with `Z`), or null.

## Suggested use in the tile (W)
- Dots = `categories` (label from `category`, the count, brush key per category).
- One feed with `ok: false` → Caution tone plus its `message` in the tooltip.
- All feeds `ok: null` → "starting…".
- Backend off (no `/health`) → the stored figures, as described in TILES_API §3.
- Poll at most every 30 s. The call is cheap: one pass over the in-memory list, no I/O.

## Tests (Global Monitoring backend, offline)
- Counts per category and window, including the `hours` boundary, `undated`, an unknown category and the 400 for a
  bad `hours`.
- No refresh or storage call, and 401 without the token.
- The response keys and order match this file.
