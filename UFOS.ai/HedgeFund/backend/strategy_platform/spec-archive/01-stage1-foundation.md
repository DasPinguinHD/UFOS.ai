# Strategy Platform SPEC — archive: 01-stage1-foundation

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 0. Ground rules (all stages)

- **Scope:** only files inside `UFOS.ai/HedgeFund/` may change (plus HedgeFund
  sections of the approved docs). Check with
  `python HedgeFund/tools/scope_guard.py check --label stage0`.
- **No unrequested fallbacks** (CLAUDE.md, 2026-10-02): a failing source, a
  missing key or a used-up budget STOPS the affected function and raises a
  `core.errors.UserFacingError` subclass whose `user_message` the UI shows.
  No alternative source, no stale/cached value used as if current, no default
  value, no cheaper model, no rule-based stand-in. Fallbacks only when the
  user accepted them — record date + place in a comment.
- **Security by default:** routes behind `api_auth` (session token); secrets
  only from the Windows Credential Manager (`secrets.py`), never logged,
  returned or written by the backend; validate all input.
- **Python:** runs on 3.14 (Windows venv); offline tests also on 3.10 without
  third-party packages → lazy-import `alpaca`, `pandas`, `aiohttp`, `openai`,
  `pyarrow` inside functions; stdlib `unittest`, temp dirs, fakes; never the
  real DB, never the network, never real credentials.
- **Time:** UTC ISO-8601 strings (`timespec="seconds"`); market logic in
  `America/New_York` via `zoneinfo`.
- English code/comments/UI text; AI labelling + cost ledger rules of CLAUDE.md
  apply to every LLM output.

## 1. Package layout (grows per stage)

```
strategy_platform/
  core/       errors.py, instruments.py (given), calendars.py, clock.py
  secrets.py  (given) Windows Credential Manager reader
  data/       registry.py, http_gate.py, cache.py, pit.py, validation.py,
              licences.py, seam.py (stage 3), providers/{base.py, alpaca.py,
              tiingo.py (stage 3)}
  storage/    db.py — hedgefund.db (backend/data/hedgefund.db), migrations
  api/        routes.py — /v2/* routes, mounted in main.py; trading_routes.py (stage 7)
  portfolio/  stage 7 — decision.py (shared with the backtest), allocator.py, allocation.py, netting.py,
              ledger.py, checks.py, risk.py, settings.py, views.py (§17)
  execution/  stage 7 — paper_store.py, brokers/{base,paper}.py, accounts.py, oms.py, control.py,
              migration.py, errors.py (§17)
  live/       stage 7 — runtime.py, incubation.py (§17)
  strategy/   stage 2 — see §7.1
  lifecycle/  stage 2 — see §7.10
```

### 1.1 Calendars and clock (stage 1, `core/calendars.py`, `core/clock.py`)

- `core/calendars.py` is the ONE source of NYSE/crypto session logic (moved
  from `fund/market.py`, which re-exports it — list in `calendars.__all__`):
  `NY, REGULAR_OPEN, REGULAR_CLOSE, EARLY_CLOSE, NYSE_HOLIDAYS,
  NYSE_EARLY_CLOSES, CALENDAR_YEARS, CalendarUnavailable, _to_utc, _as_date,
  _iso, ny_now, ny_date, is_covered, is_trading_day, is_early_close,
  session_bounds, next_trading_day, previous_trading_day, session_state,
  is_open, trading_day, trading_days_between, trading_days, last_close_before,
  is_crypto, is_tradable, utc_days_between, FUNDING_HOURS_UTC,
  last_funding_window, funding_windows_between`.
- Holidays/early closes hardcoded for **2016–2027** (Alpaca IEX history
  starts 2016; per-year trading-day counts are unit-tested). Outside these
  years `is_trading_day` (and everything built on it) raises
  `CalendarUnavailable(UserFacingError)` — the old "warn and treat every
  weekday as a trading day" default is NOT carried over (no fallback).
  `last_close_before` raises instead of returning `now`.
- New helpers: `trading_days(start, end) -> [date]` (inclusive; expected
  daily bars for gap validation), `is_early_close(d)`, `is_covered(d)`.
- `core/clock.py`: `Clock().now_utc()/now_iso()`, `FixedClock(now)` with
  `set()`/`advance()` for tests, `SYSTEM_CLOCK`, `resolve(clock|None)`,
  `to_utc(datetime|iso)`, `iso(dt)`.

## 2. Secrets (stage 1)

Given in `secrets.py`. Names and legacy `.env` names:

| name | label | ignored .env names |
|---|---|---|
| `openrouter-api-key` | OpenRouter API key | OPENROUTER_API_KEY |
| `fred-api-key` | FRED API key | FRED_API_KEY |
| `sec-edgar-user-agent` | SEC EDGAR user agent (name + e-mail) | SEC_EDGAR_USER_AGENT |
| `alpaca-paper-key-id` | Alpaca paper API key ID | ALPACA_API_KEY_ID, APCA_API_KEY_ID |
| `alpaca-paper-secret-key` | Alpaca paper API secret key | ALPACA_API_SECRET_KEY, APCA_API_SECRET_KEY |
| `tiingo-api-key` | Tiingo API key (stage 3, §8.1) | — (new key, no import) |
| `kraken-demo-api-key` | Kraken Futures demo API key (public) (stage 8, §18.3) | — (new key, no import) |
| `kraken-demo-api-secret` | Kraken Futures demo API secret (private) (stage 8, §18.3) | — (new key, no import) |

Credential: generic, TargetName `UFOS.ai/HedgeFund/<name>`, blob = UTF-8
value, persist LOCAL_MACHINE (a blob with invalid UTF-8 or a NUL byte →
`SecretStoreUnavailable` "<label> is stored in an unreadable format — set it
again in Settings → Keys.", `secrets.decode_blob`). Written ONLY by the WPF side
(`HedgeFund/Platform/CredentialStore.cs`: set, remove, import from
`backend/.env` — the `.env` file is left unchanged, user decision). The
backend reads at use time. `.env` values for these names are ignored; the
key status reports `env_value_ignored: true` so the UI can say "import it".

Wiring (stage 1, done):
- `config.py` has no secret constants any more; `config.log_ignored_env_secrets(log)`
  runs once at startup and logs per secret whose legacy env var is set:
  "<label>: <VAR> found in .env but ignored — import it in Hedge Fund →
  Settings → Keys" (never the value).
- OpenRouter: `newsroom/ai.py` `api_key()` (= `require_secret`),
  `key_error() -> UserFacingError|None`, `_current_settings()` (key from the
  Credential Manager, raises; base URL/models still re-read from `.env`).
  `/newsroom/insider-summary` and `/newsroom/analysis` answer 503
  `{error: user_message, kind}` when the key is missing. `fund/llm.py` maps
  `SecretMissing` → `LlmUnavailable("no_key", user_message)` (store
  unreadable → `"error"`).
- FRED / SEC EDGAR collectors: `newsroom/keys.py`
  `wait_for_secret(name, report_status, recheck_seconds=60, suffix="", on_missing=None)`
  gates every poll: while the key is missing the collector reports the
  SecretMissing message as its status (ok=false) and re-checks every minute;
  a key entered later applies without a restart. The FRED key is read per
  request (`fred_collector._fred_key()`, `calendar_collector._fetch_json`).
  `keys.redact(text, *values)` masks key values and `api_key=` query
  parameters in error texts (aiohttp errors contain the request URL — before
  2026-10-02 the FRED key could reach backend.log and the status line).

## 3. Data layer (stage 1)

Implemented 2026-10-02 in `strategy_platform/data/` (stdlib at import time;
alpaca-py imported lazily). Tests: `tests/test_sp_data_*.py` (fake SDK, no
network).

- **One configured source per data type, no fallback chains** (D8).
  `registry.py`: `DEFAULT_SOURCES` — `equity_bars`, `crypto_bars`,
  `equity_quotes`, `crypto_quotes`, `news`, `corporate_actions` (dividends,
  splits) → `alpaca`; `macro` → `fred`, `insider` → `sec_edgar` (served by
  the existing newsroom collectors; `get_provider` refuses them). Stage 1
  allows exactly one source per type (`ALLOWED_SOURCES`), so a missing
  `data_sources` row is seeded with it (+ licence) on first use or by
  `ensure_configured()` — configuration, not a fallback. API:
  `configured_source(dt)`, `set_source(dt, source)` (ValueError otherwise),
  `get_provider(dt)`, `fetch(dt, op, *args)` (calls the provider and records
  the status after EVERY success/failure via
  `storage.db.data_source_status_update(dt, ok, user_message, at)`;
  `HistoryNotAvailable` and `InvalidRequest` (a provider `ValueError` = bad
  caller argument: symbol, timeframe, time → `InvalidRequest`, 400 on /v2)
  are request errors and leave the status alone; an
  unexpected exception becomes `DataSourceUnavailable("unexpected error
  (<Type>)")`), `sources()`, `test_source("alpaca") -> {ok, source, checks:
  [{name, ok, message}]}` (checks "1 daily bar SPY", "1 daily bar BTC/USD",
  "Latest SPY quote"; missing/rejected keys → that message, the remaining
  checks "Skipped — <reason>"; statuses recorded). Module-level functions
  delegate to `default()` (tests: `set_default(Registry(...))`).
- **Providers** (`providers/base.py`): plain dicts — bar `{symbol, timeframe,
  ts (bar start), open, high, low, close, volume, vwap, trade_count, source,
  known_at, fetched_at}`, quote `{symbol, bid, ask, bid_size, ask_size, ts,
  source}`, trade `{symbol, price, size, ts, source}`, news `{id, headline,
  summary, publisher, url (http/https only), created_at, updated_at,
  symbols, images (MR6: [{size, url}], https only), source}`, corporate action `{symbol, kind (cash_dividend |
  forward_split | reverse_split), ex_date, rate, old_rate, new_rate,
  process_date, payable_date, special, source}`. Timeframes `1Day`, `1Hour`,
  `1Min`. Errors: `SourceAuthRejected(DataSourceUnavailable)`,
  `HistoryNotAvailable(UserFacingError)`.
- **Alpaca** (`providers/alpaca.py`, alpaca-py ≥ 0.44, lazy import; missing
  package → `DataSourceUnavailable` "alpaca-py package is not installed"):
  keys `alpaca-paper-key-id` / `alpaca-paper-secret-key` read via
  `secrets.require_secret` at every call (SecretMissing raised before any
  request); a client per call with `raw_data=True` (JSON → dicts, no pandas,
  no SDK models). Stocks: `StockHistoricalDataClient`, `feed="iex"` (free
  plan: IEX only, history since 2016 — earlier `start` →
  `HistoryNotAvailable` "History before 2016 is not available from the
  configured source (alpaca)."; the latest 15 min of SIP are restricted,
  never requested), `adjustment="all"` for bars; latest quote/trade IEX.
  Crypto: `CryptoHistoricalDataClient` (needs no key; keys are sent anyway —
  one code path), symbol mapping `instruments.instrument(s).alpaca_symbol`
  ("BTC-USD" ↔ "BTC/USD"). News: `NewsClient` (crypto tagged "BTCUSD",
  `sort="desc"`, `include_content=False`). Dividends/splits:
  `CorporateActionsClient` (equities only). Error mapping
  (`classify_error`): 401/403 → "Alpaca rejected the keys — check them in
  Hedge Fund → Settings → Keys." (a 403 about the subscription/plan says
  "your Alpaca market-data plan does not include this request"), 429 / 5xx /
  timeouts → retried by the gate, network/TLS and other 4xx →
  `DataSourceUnavailable` at once; malformed data → `DataSourceUnavailable`
  ("unexpected data from Alpaca"), never skipped silently.
  `harden_client`: the SDK's own retry loop (429/504, 3 × 3 s) is switched
  off (`client._retry = 0`) and every request gets a 20 s timeout (wrapper
  on `client._session.request`; the SDK sets none) — both SDK internals,
  re-check after alpaca-py upgrades.
- **HTTP gate** (`http_gate.py`): one thread-safe token bucket per source
  (`gate_for(source)`), Alpaca 150 requests/min with burst 10 (≤ 160 in any
  minute; the free plan allows 200; lowered from 180 on 2026-10-23 — fixed split with the Options Structure archive's
  own 30/min, §21.1), wait for a slot ≤ 120 s (else
  `DataSourceUnavailable`). Retries only for HTTP 429/5xx/timeouts of the
  SAME call: 3 attempts, backoff 1 s / 3 s (`Retry-After` honoured, capped
  10 s); exhausted → `DataSourceUnavailable("… — gave up after 3 attempts")`.
  A paginated SDK call counts as one token (stage 1 requests fit one page).
- **Cache** (`cache.py`, table `bars` in hedgefund.db via
  `storage.db.query_bars/upsert_bars`): `BarCache.get_bars(symbol,
  timeframe, start, end=None, *, require_fresh_through=None)`. Only bars with
  `known_at <= now` are stored or returned (no unfinished bar). Cached bars
  of closed periods are history, not a fallback. Freshness is explicit:
  `require_fresh_through=T` — daily EQUITY bars: the bar of the last session
  closing at or before T itself (`known_at >= calendars.last_close_before(T)`;
  sessions differ in length — early closes 13:00 NY — so "T − one day" would
  accept the previous session's bar, review fix 2026-10-02); fixed-length
  bars (crypto daily, intraday): newest `known_at > T − one period` (crypto:
  pass `now`); otherwise the tail is fetched and, if that fails or the source has
  no such bar yet, `DataSourceUnavailable` is raised — stale bars are never
  returned as current. Without a requirement, a range ending > one period +
  5 days after the newest cached bar is completed the same way. Adjusted
  prices change after dividends/splits: every tail fetch re-reads the newest
  cached bar and, if it differs, refetches and replaces the symbol's whole
  stored history; the head fetch (a start > 5 days before the first cached
  bar, fetched once — a later source start, e.g. an IPO, is remembered in
  memory) includes the first cached bar and does the same check, so bars
  of two adjustment bases are never mixed. Provider requests go window by
  window (`FETCH_WINDOW`: 1Day 3660 d, 1Hour 400 d, 1Min 6 d ≈ one Alpaca
  page ≈ one gate token); a range beyond `MAX_REQUEST_RANGE` (1Min 31 d,
  1Hour 5 y) → `RequestTooLarge` (an `InvalidRequest`, nothing fetched);
  writes go in chunks of `SAVE_CHUNK_ROWS` (10 000 ≤ the 50 000-row limit of
  `upsert_bars`). Storage failures raise `StorageUnavailable`.
- **Point-in-time** (`pit.py`): `known_at` = bar end (IEX delay 0): daily
  equity bar → that date's session close from `core.calendars` (16:00 NY,
  13:00 on early closes; 16:00 NY for a bar on a non-session date;
  `CalendarUnavailable` outside the calendar years); crypto daily and
  intraday bars → start + period (crypto daily = 24 h after its start,
  whichever midnight Alpaca aligns it to). `bars_as_of(symbol, timeframe,
  as_of)` reads the cache and returns only bars with `known_at <= as_of`.
- **Validation** (`validation.py`): `validate_bars(rows, *, symbol,
  timeframe, corporate_actions=(), now=None) -> [{code, severity, message,
  ts}]` — `duplicate`, `non_positive`, `high_below_low` (errors),
  `ohlc_outside`, `gap` (vs NYSE calendar / every UTC day for crypto; daily
  only), `calendar_unavailable`, `return_outlier` (|log return| > 8 σ of the
  previous 60 returns, ≥ 20, no split/dividend ex-date that day), `stale`
  (equities: no bar for the last completed session; crypto: none completed
  in 48 h). Findings only — nothing is repaired or dropped.
- **Licences** (`licences.py`, stored with each `data_sources` row):
  `alpaca` → `personal_use` (free plan: account holder's own use, no
  redistribution/public display); `fred` → `unknown` (public with
  attribution, but some series are third-party copyrighted, e.g. ICE BofA
  `BAMLH0A0HYM2`); `sec_edgar` → `commercial_ok` (public US-government data;
  declared User-Agent, 10 requests/s).
- **Old engine (`fund/`) switched to Alpaca now** (user decision 2026-10-02):
  `fund/market.py` `AlpacaFeed` implements `PriceFeed` on this layer (see
  fund/SPEC.md §5); `YFinanceFeed` and `parse_news_item` are removed,
  yfinance is out of requirements.txt. A missing quote/bar is NOT replaced
  by a cached close (that "stale cached close" behaviour was never accepted
  by the user and is removed): the feed raises with the reason, the symbol
  is skipped for that run, orders for it are not filled, the run is
  `partial`. The backtest refuses a start before 2016 (`fund/backtest.py`
  `HISTORY_ERROR`).

## 4. API v2 (stage 1, all behind api_auth)

- `GET /v2/settings/keys` → `{"keys": secrets.status()}` (never values).
- `POST /v2/settings/keys/changed` `{name, action: "set"|"removed"|"imported"}`
  → audit row (actor `user`), no value accepted (400 if a `value` field is sent).
- `GET /v2/data/sources` → `{"sources": [{data_type, source, status, last_ok_at,
  last_error_at, message, licence}]}`.
- `POST /v2/data/test` `{source: "alpaca"}` → fetches 1 daily bar for SPY and
  BTC/USD and the latest SPY quote; `{ok, checks: [{name, ok, message}]}`.
  Missing keys → `ok:false` with the SecretMissing message (HTTP 200).
- Errors: `UserFacingError` → 503 `{error: user_message, kind}`; validation →
  400 `{error, errors}`.

Implementation notes (stage 1): `api/routes.py` — `V2Api` handlers
`(query, body, match) -> (status, payload)` + `register(app)`, called in
main.py after `fund_api.register` (behind `api_auth`). Bodies must be JSON
objects; unknown fields → 400. Key audit actions: `key_set`, `key_removed`,
`key_imported` (detail `{name}`). GET /v2/data/sources reads ONLY the
`data_sources` table (rows written by the registry); `licence` is
`{terms, note}` or null when the registry stored none; empty list until the
registry configured the types. POST /v2/data/test lazily imports
`strategy_platform.data.registry` and calls `test_source("alpaca") -> dict`
in a worker thread (blocking is fine); while that module does not exist the
route answers 503 `{"error": "data layer not installed", "kind":
"DataLayerNotInstalled"}` (development-order guard, not a data fallback; a
missing dependency INSIDE the registry is not hidden → 500). A non-dict
result → 500.

## 5. Storage (stage 1)

`backend/data/hedgefund.db` (git-ignored `*.db`), WAL, `PRAGMA user_version`
migrations, same robustness rules as `newsroom/db.py` (corrupt → renamed
`*.corrupt-<ts>`, recreated; errors never crash a caller but are surfaced).
v1 tables: `meta`, `audit` (id, ts, actor, action, detail_json),
`data_sources`, `bars` (symbol, timeframe, ts, open, high, low, close,
volume, vwap, trade_count, source, known_at, fetched_at; PK symbol,
timeframe, ts, source). The old paper account stays in newsroom.db; stage 7
(§17.9) copies it into the platform account `paper-council` when the user migrates it.

### 5.1 `storage/db.py` API (stage 1, done)

Error contract: "not found" → `None` (lists: `[]`); any failure (not opened,
locked, unwritable, SQLite error) → `StorageUnavailable(UserFacingError)`
(user message names hedgefund.db; detail in the log); invalid arguments →
`ValueError`. Nothing is swallowed into a default value (unlike
`newsroom/db.py`'s `_guard`). All times are normalised to UTC ISO strings
`YYYY-MM-DDTHH:MM:SS+00:00` on write and on query bounds (inputs: aware or
naive (=UTC) `datetime`, `date` (= 00:00 UTC) or ISO string); unparsable →
`ValueError`, never "now".

| function | returns |
|---|---|
| `init(path=None) -> int` | schema version; default path `backend/data/hedgefund.db`; tests pass a temp path. Corrupt → quarantined + recreated; other failure → `StorageUnavailable`, later calls retry opening at most every `REOPEN_INTERVAL_SECONDS` (30). main.py calls it at startup. |
| `close()`, `is_open()`, `db_path()`, `default_path()` | |
| `storage_info() -> {path, size_bytes, schema_version, rows: {table: n}}` | |
| `meta_get(key) -> str or None`; `meta_set(key, value)` | value stored as `str(value)`; `None` deletes |
| `add_audit(actor, action, detail=None) -> int` | actor ∈ `user, engine, system, data`; detail JSON-serialisable, never secrets |
| `list_audit(limit=50, action=None) -> [{id, ts, actor, action, detail}]` | newest first |
| `data_source_set(data_type, source, *, licence=None, licence_note=None) -> row` | upsert of THE source of a type; licence ∈ `personal_use, commercial_ok, unknown`; a changed source clears status/last_*/message |
| `data_source_get(data_type) -> row or None`; `data_sources_list() -> [row]` | row = `{data_type, source, licence, licence_note, status, last_ok_at, last_error_at, message, updated_at}`, status `None`/`"ok"`/`"error"` |
| `data_source_status_update(data_type, ok: bool, message=None, at=None) -> row or None` | sets status + `last_ok_at`/`last_error_at` (= `at` or now); `None` when the type is not configured |
| `upsert_bars(rows) -> int` | rows: dicts with `BAR_COLUMNS` (`volume`, `vwap`, `trade_count` may be None/omitted; `adjustment` ∈ `all, split`, omitted = `all` = the column default and every stage-1 caller); INSERT OR REPLACE on (symbol, timeframe, ts, source, adjustment) in ONE transaction; one invalid row (missing/non-finite price, bad time, unknown field, unknown adjustment) → `ValueError`, nothing written; symbols upper-cased; ≤ 50 000 rows per call |
| `query_bars(symbol, timeframe, start, end, source, *, known_by=None, adjustment="all") -> [bar]` | ONE source and ONE adjustment series (no mixing), oldest first, `start <= ts <= end` (None = open), `known_by` → only `known_at <= known_by` (point-in-time); `[]` when none |
| `latest_bar_ts(symbol, timeframe, source, *, adjustment="all") -> str or None` | newest stored `ts` |
| `replace_corporate_actions(symbol, source, start, end, rows) -> int` (v3) | replaces ONE symbol/source's actions with ex_date in [start, end] (dates, None = open) in ONE transaction; rows `CA_COLUMNS` (`symbol, kind (cash_dividend, forward_split, reverse_split), ex_date, cash_amount (dividends, raw per share), split_ratio (splits, new/old), pay_date, source, known_at, fetched_at`); a row of another symbol/source/range or invalid → `ValueError`, nothing written |
| `query_corporate_actions(symbol, source, start=None, end=None) -> [row]` (v3) | ONE source, oldest first; `[]` when none |
| `seam_check_add(symbol, adjustment, ok, factor, stats, checked_at=None) -> row` (v3) | insert (history kept); factor required when ok |
| `seam_check_latest(symbol, adjustment) -> row or None`; `seam_checks_list(symbol=None, limit=100) -> [row]` (v3) | row `{symbol, adjustment, checked_at, ok, factor, stats}`, newest first |
| `store_volumes(symbol, source, rows, *, replace_all=False) -> int` (v4) | table `volumes` (`symbol, date, volume, close_split, source, known_at, fetched_at`; PK symbol, date, source) — consolidated volume (§8.8), never in `bars`; ONE transaction, `replace_all` deletes the symbol/source first |
| `query_volumes(symbol, source, start=None, end=None, *, known_by=None) -> [row]` (v4) | oldest first; `[]` when none |
| `backtest_run_insert(row, audit)` (v5) | new run card, status `queued`, + audit row in ONE transaction |
| `backtest_run_transition(run_id, from_status, to_status, fields=None, audit=None) -> bool` (v5) | the ONLY update: queued→running/failed/cancelled, running→done/failed/cancelled, result columns written in the same UPDATE; False when not in `from_status` |
| `backtest_run_get(run_id) -> row or None`; `backtest_runs_list(strategy_id=None, *, version=None, status=None, limit=50) -> [row]` (v5) | newest first; list rows without spec/data/curve |
| `strategy_set_state(..., *, expected_version=None)` (v5 change) | also refuses when the current version changed meanwhile |

`BAR_COLUMNS = (symbol, timeframe, ts, open, high, low, close, volume, vwap,
trade_count, source, known_at, fetched_at, adjustment)`. Schema v3 (stage 3,
2026-10-03): `bars.adjustment` (in the primary key; table rebuilt, existing
rows `all`), `corporate_actions`, `seam_checks` — the whole version runs in
ONE transaction (`migrate()` rolls a failed version back, v2 data intact). `ts` = bar start time. Prices
are stored as given; plausibility (non-positive, high<low, …) is
`data/validation.py`'s job. The connection is per call; callers in asyncio
code wrap the calls in `asyncio.to_thread`.

## 6. Fallbacks found (stage 1 review, 2026-10-02)

User decisions of 2026-10-02 (D9 and the fallback review). Code comments:
`# FALLBACK (accepted by the user 2026-10-02): …` at every accepted one
(line numbers as of 2026-10-02, paths relative to `HedgeFund/backend`);
no open ones remain.

**Accepted (kept):**
- `newsroom/calendar_collector.py:362` — Economic Calendar without the FRED
  key shows the hardcoded FOMC dates and says "… Showing FOMC dates only.".
- `newsroom/calendar_collector.py:327` — per-release `fred/release/dates`
  request when the bulk `fred/releases/dates` answer has gaps.
- `fund/llm.py:144` — hardcoded model ids when the AI module
  (`newsroom.ai`) fails to load.
- `fund/llm.py:321` — the pre-call estimate is booked when OpenRouter
  reports no cost.
- `fund/llm.py:232` / `config.py:33` / `newsroom/ai.py:38` — a non-https
  `OPENROUTER_BASE_URL` is replaced by the default https URL.
- `newsroom/sec_edgar_collector.py:352` — a failed later feed page keeps the
  entries of the pages already loaded.
- Change detection (design of 2026-09-26, confirmed 2026-10-02): the judge
  re-uses a recent AI decision when nothing material changed — also with
  budget 0 (`source="carried"`); `/fund/state` shows `recommendation.source`
  "carried" and `recommendation.judged_at` = the ORIGINAL judgement time.
- ~~`strategy_platform/data/seam.py` `Seam.volume(newest_optional=True,
  wait_for_latest=False)`~~ — OBSOLETE, REMOVED 2026-10-06: the accepted
  "end one session earlier when the newest Tiingo volume row is missing"
  path (user decisions 2026-10-05) existed only because Tiingo served daily
  volume for all years. Since 2026-10-06 volume from the seam on comes from
  Alpaca SIP (§8.8), so the path, `wait_for_latest`, the warnings
  `end_limited_by_tiingo` / `tiingo_not_published_yet` and the background
  refresh are gone (§8.12).

**Removed:**
- `fund/judge.py` rule judge + the engine's "judge failed — rule fallback"
  (D9): no recommendation, no autopilot order; reason per symbol
  (`judge_unavailable`), the older recommendation keeps its time and is
  marked `not_updated`; budget reached → "AI budget reached — raise the
  limit in Settings to continue."; same for the sentiment agent.
- Positions valued at the stored price without a quote (broker `_mark`,
  engine cycle / risk watch / manual accept): the broker raises
  `ValuationPaused` → for that run valuation, drawdown check, margin
  actions, equity snapshot and equity-dependent orders pause ("valuation
  paused: no current quote for X" in the run error/summary and
  `/fund/state` `account.valuation_paused`; positions listed with null
  values; manual accept → 503). Closing orders still queue (kill switch).
- Macro agent on stored FRED data while FRED fails / the key is missing:
  the engine reads the newsroom's FRED status (`macro_status_fn`); not ok →
  macro agent `source="unavailable"`, "Macro data unavailable: <reason>",
  confidence 0. (The newsroom display keeps its cache-first view with its
  date — not a trading input.) When available, the agent shows the data's
  as-of date (`details.as_of`, "(data as of …)").
- Review fixes (never accepted): a symbol with bars but no quote (or the
  reverse) is not analysed (`data_unavailable`); `AlpacaFeed._to_bar` no
  longer repairs OHL/volume (malformed bar → `DataSourceUnavailable`
  "Alpaca returned a malformed bar for X on D", validation errors too;
  missing volume stays None); manual accept without a stored risk card /
  size multiplier → 409 (no multiplier 1.0); sentiment reads
  `ai_daily_budget_usd` strictly (no 1.0 default); daily-bar freshness and
  prev_close after early closes; earlier in stage 1: `newsroom/ai.py` key
  from a reloaded `.env`, `fund/market.py` calendar guesses (weekday =
  trading day outside 2026–2027, `last_close_before` returning now).
- Security fix (not a fallback): FRED request errors could carry the
  `api_key` query parameter into backend.log and the source status → redacted.

Accepted as well (user decision 2026-10-02, second round):
- `newsroom/ai.py:270` / `config.py` — model ids missing in `.env` → the
  config defaults; an empty `OPENROUTER_ANALYSIS_PREMIUM_MODEL` → the
  default premium id.
- `newsroom/sec_edgar_collector.py:423` — unparsable Form 4 details → the
  card shows the feed title (and says the details could not be parsed).

Removed as well (user decision 2026-10-02, second round):
- `fund/judge.py` `premium_on_flip`: premium call failed / over budget /
  no valid item → no recommendation for that flipped symbol
  (`judge_unavailable` with the reason), no standard-answer stand-in.
- Sizing without ATR: manual accept → 409 "No daily bars for X — the
  position size can't be volatility-scaled; try again when market data is
  available"; the autopilot skips such a symbol.
- `fund/engine.py` `_newsroom()`: a failing calendar / insider accessor is
  no longer an empty list → risk card "unavailable: calendar data could not
  be loaded (…)" (no orders for the symbol, manual accept 409); the
  sentiment input/summary says "insider data unavailable".
- `fund/agents/sentiment.py` `insider_flags`: a failing cluster detection
  raises `InsiderDataUnavailable` (same handling).
- `newsroom/macro_derived.py`: no CPI instead of core PCE (Real Policy Rate,
  regime map and its extended view), no own Sahm reconstruction instead of
  SAHMREALTIME → the indicator / regime is sent as "unavailable: <input>
  missing" (`macro.unavailable`, no value; the regime with `regime: null`);
  the extended view reports `euro_error` instead of silently dropping the
  euro-area point.

**Nothing is pending any more** (2026-10-02).
