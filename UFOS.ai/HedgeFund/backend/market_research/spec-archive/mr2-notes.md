# Market Research SPEC — archive: mr2-notes

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

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

