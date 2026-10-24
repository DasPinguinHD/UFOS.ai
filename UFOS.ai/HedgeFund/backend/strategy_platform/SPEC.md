# Strategy Platform — implementation contract

> Part of UFOS.ai, a proof-of-concept portfolio project. Paper trading by
> default, not investment advice. Plan and decisions: project doc
> `claude/strategy-platform-plan.md`. Change this file together with the code.

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

## 7. Stage 2 — strategy definition (contract, 2026-10-02)

Interview decisions (user, 2026-10-02): universe = liquid US ETFs + crypto;
navigation shell in the Hedge Fund window now (Overview = today's dashboard,
Library); **JSON editor + file import** allowed; the rule-based part of the
agent committee becomes a library strategy. Security measures accepted:
size/depth limits, provenance of imported files not trusted, texts marked as
unverified ("external"). NOT accepted: restricting specs to known symbols →
any syntactically valid symbol is allowed (residual risk: many symbols → many
data requests; bounded by the 500-symbol limit and the HTTP gate).

**numpy** is allowed from stage 2 on (it is in the venv via pandas; add
`numpy>=2.1` to requirements.txt). Import it lazily inside functions of
modules that main.py imports at startup; tests may use it.

### 7.1 Layout

```
strategy_platform/strategy/
  limits.py      all size limits (one place) + safe JSON parser
  schema.py      StrategySpec schema, canonical JSON, content hash
  dsl.py         operator registry (name, category, arity, arg kinds, doc), expr_text()
  validator.py   validate(spec) -> ValidationResult (errors + warnings + facts)
  evaluate.py    numpy evaluator: Panel -> signal panel (causal)
  panel.py       Panel (dates x symbols per field), build from bars (calendar alignment)
  portfolio.py   signal row -> target weights (mode, selection, weighting, caps)
  sdk.py         Strategy base (on_bar(ctx) -> weights), Context (PIT guard), SpecStrategy
  universe.py    presets (liquid_etfs_crypto), categories
  summary.py     summary_texts(spec) for the UI (§7.11 summary)
  preview.py     preview() on current bars (§7.7)
  features/      external feature providers (macro.regime_score, calendar.event_risk)
  alphas/        library: one module per family, LIBRARY list of specs (+ metadata)
strategy_platform/lifecycle/
  registry.py    strategies, immutable versions, transitions, seeding of the library
```

### 7.2 Limits (`limits.py`) and safe parsing

`MAX_TEXT_BYTES = 262144` (UTF-8 bytes of the submitted JSON text),
`MAX_DEPTH = 32` (JSON nesting), `MAX_NODES = 2000` (all JSON values in the
spec), `MAX_WINDOW = 2520` (bars), `MAX_SYMBOLS = 500`, `MAX_PARAMS = 32`,
`MAX_FEATURES = 64`, `MAX_TAGS = 16`, text lengths: name 80, tag 32,
description 4000, param description 300, feature name 40 (`^[a-z][a-z0-9_]{0,39}$`).
`parse_spec_text(text) -> dict`: rejects (ValueError with a clear message,
incl. line/column for JSON syntax errors) non-str, > MAX_TEXT_BYTES, invalid
JSON, duplicate keys (`object_pairs_hook`), `NaN`/`Infinity` (`parse_constant`),
non-object root, depth > MAX_DEPTH, nodes > MAX_NODES. Depth is checked
WITHOUT recursion limits being hit (iterative walk). No `eval`, no pickle,
nothing executed — a spec is data only.

### 7.3 StrategySpec v1 (`schema.py`)

User-owned document (what the editor shows / files contain):

```json
{
  "schema": "ufos.strategy/1",
  "name": "Cross-sectional momentum 12-1",
  "description": "…",
  "tags": ["momentum"],
  "category": "momentum",               // momentum|reversal|trend|volatility|value|carry|allocation|alpha101|composite|other
  "universe": {"preset": "liquid_etfs_crypto", "asset_classes": ["equity"]}
              // or {"symbols": ["SPY", "TLT", "BTC-USD"]} (≤ MAX_SYMBOLS, instruments.instrument() must accept each)
  "timeframe": "1Day",                   // only 1Day in stage 2
  "parameters": {"lookback": {"value": 252, "min": 126, "max": 378, "step": 21, "integer": true, "description": "…"}},
  "features": {"mom": {"op": "…"}},      // named sub-expressions, referenced by {"ref": "mom"}; no cycles
  "signal": {"op": "cs_rank", "args": [{"ref": "mom"}]},
  "portfolio": {
    "mode": "long_only",                 // long_only|short_only|long_short
    "selection": {"type": "top_n", "n": 5},   // top_n|bottom_n|top_bottom_n|threshold|all ; threshold: long_above / short_below
    "weighting": "equal",                // equal|signal|inverse_vol (inverse_vol needs vol_lookback)
    "vol_lookback": 60,
    "max_weight": 0.25, "gross_exposure": 1.0
  },
  "rebalance": {"frequency": "monthly"}, // daily|weekly|monthly (first trading day of week/month)
  "risk": {"stop_loss_pct": null, "take_profit_pct": null, "vol_target_annual": null, "max_drawdown_stop_pct": null},
  "execution": {"order_type": "market", "timing": "next_open"},   // timing: next_open|next_close ONLY (no same-bar fills)
  "costs": {"model": "paper_broker"},    // or {"model": "custom", "fee_bps": 5, "slippage_bps": 5}
  "provenance": {}                       // optional, free-form claims; see 7.6
}
```

Unknown keys anywhere → error (path given). All numbers finite. A parameter
is used in expressions as `{"param": "lookback"}`; using it where an integer
window is required needs `"integer": true` and integer min/max/value; a
parameter's value must lie in [min, max]. Unused parameters/features →
warning. `canonical_json(spec)` = sorted keys, no whitespace, UTF-8;
`content_hash = sha256(canonical_json(user-owned part without provenance))`
hex. Missing optional blocks get documented defaults written INTO the stored
spec on save (so the hash covers them) — these are spec defaults, not data
fallbacks.

### 7.4 DSL (`dsl.py`) — expression nodes

- number literal (finite); `{"param": name}`; `{"ref": feature_name}`;
  `{"field": f}` with f ∈ `open high low close volume vwap trade_count`;
  `{"ext": name}` external feature (7.8); `{"op": name, "args": [...]}`.
- Element-wise: `add sub mul div neg abs sign log sqrt pow signed_power
  min max clip where gt ge lt le eq ne and or not is_nan fill_nan`
  (div by 0, log ≤ 0, sqrt < 0 → NaN; `pow` exponent must be a constant).
- Time series, per symbol, window `n` = positive integer constant or integer
  param ≤ MAX_WINDOW: `lag(x,n≥1) delta pct_change ts_mean ts_std ts_sum
  ts_min ts_max ts_argmin ts_argmax ts_rank ts_zscore ts_product ts_skew
  ts_corr(x,y,n) ts_cov(x,y,n) decay_linear ewm_mean(x, halflife)`;
  helpers `returns(n)` = pct_change(close,n), `rsi(n)`, `atr(n)` (Wilder, on
  high/low/close), `sma(x,n)`=ts_mean.
- Cross-sectional, per date over symbols with a value: `cs_rank` (0..1,
  average ties) `cs_zscore cs_demean cs_scale` (sum |x| = 1) `cs_winsorize(x,k)`.
- **Look-ahead ban (validator):** unknown op → error; `lag`/windows must be
  ≥ 1 (no 0 or negative shifts, no centred windows); no op may read bars
  after t; `execution.timing` must be next_*; `ext` only providers with
  `point_in_time = True`.
- `dsl.catalogue()` → list of `{name, category, signature, description}` (for
  the editor help); `expr_text(node)` → readable one-liner, e.g.
  `cs_rank(pct_change(close, 252) - pct_change(close, 21))`.

### 7.5 Validator (`validator.py`)

`validate(spec: dict) -> ValidationResult{ok, errors:[{path, code, message}],
warnings:[…], facts:{max_lookback, required_fields, required_ext, symbols_count,
asset_classes, param_count, node_count}}`. Paths in JSONPath style
(`$.signal.args[0].args[1]`). Codes (stable, tested): `unknown_key`,
`missing_key`, `bad_type`, `out_of_range`, `unknown_op`, `bad_arity`,
`bad_window`, `look_ahead`, `unknown_param`, `unknown_ref`, `ref_cycle`,
`unknown_field`, `unknown_ext`, `not_point_in_time`, `bad_symbol`,
`too_many_symbols`, `limit_exceeded`, `bad_timing`, `text_too_long`.
Warnings: `unused_param`, `unused_feature`, `mixed_calendars`,
`survivorship` (preset universes are today's ETF list — mild selection bias),
`short_history` (max_lookback > 756), `crypto_short_selling` (perps).
`validate_text(text)` = parse (7.2) + validate; parse errors become one error
with code `parse_error` and `line`/`column`.

### 7.6 Provenance and external text (import security)

The SYSTEM owns the envelope; nothing in a file can set it. Envelope per
version: `{strategy_id, version, content_hash, origin: library|user|import|
duplicate, created_at, created_by: user|system, parent: {strategy_id, version}
or null, ai_generated: false|true|"unknown", external_text: bool,
claimed: {...} or null, source_file: {name, sha256, bytes} or null}`.
- Import: the file's `provenance` block (and any state/validated/run-card
  like claims inside it) is moved to `claimed` verbatim (size-limited) and
  shown ONLY as "claimed by the file"; state always starts at `draft`;
  `ai_generated = "unknown"` unless the user later sets it; `external_text =
  true`. The file name only (no directory path) is stored.
- `external_text` texts (name, description, tags, param descriptions,
  feature names, claimed) are plain text everywhere (WPF TextBlocks, never
  rendered as markup/links) and get the UI chip "external text · unverified".
  Stage 5 must wrap them in delimiters before giving them to an LLM (note in
  §7.12).
- Editing a user strategy creates a new version (immutable versions); the
  strategy's state returns to `draft` (evidence belonged to the old version).
  Library strategies are read-only → "Duplicate as draft".

### 7.7 Evaluation (`panel.py`, `evaluate.py`, `portfolio.py`)

- `Panel`: `dates` (ascending), `symbols`, `fields: {name: ndarray[dates,
  symbols]}` (NaN = no value). `Panel.from_bars(bars_by_symbol, calendar)`:
  equities on NYSE sessions; crypto-only universe on UTC days; mixed universe
  on NYSE sessions with each crypto value = last crypto daily bar KNOWN by
  that session's close (`data/pit.known_at`) — warning `mixed_calendars`.
- `evaluate(spec, panel) -> ndarray[dates, symbols]` (signal); NaN
  propagation per numpy; deterministic; time-series ops need ≥ n valid values
  in the window else NaN.
- **Causality property (the look-ahead acceptance test):** for every operator
  and every library strategy, evaluating on `panel[:t+1]` gives the same row
  t as evaluating on the full panel (within 1e-9), for random t on synthetic
  data.
- `portfolio.target_weights(spec, signal_row, panel, t) -> {symbol: weight}`:
  NaN excluded; ties broken by symbol; respects mode, selection, weighting,
  `max_weight` (excess is NOT redistributed — held as cash), gross/net;
  deterministic.
- `rebalance_dates(spec, dates)`.
- `preview(spec, as_of=None, days=20) -> {as_of, rows: [{symbol, signal,
  rank, target_weight}], history: [{date, weights}], warnings, data: {source,
  from, to, symbols_missing}}` loads daily bars via `data/cache.get_bars`
  (Alpaca, one source). A symbol without data → listed in
  `symbols_missing` and the call FAILS with `DataSourceUnavailable` if the
  data source failed; a symbol that simply has no history (e.g. not listed
  yet) is excluded and listed (that is a property of the universe, not a
  fallback). Bound: ≤ 60 s wall time, else a clear error.

### 7.8 External features (`features/`)

`ExtFeature(name, description, point_in_time=True, scope="global"|"asset_class",
load(start, end) -> {date: value} or {asset_class: {date: value}})`.
Registry `features.get(name)`, `features.catalogue()`.
- `macro.regime_score`: the rule-based macro agent's score, computed ONLY from
  FRED data as first published (ALFRED vintages: request with
  `realtime_start=1776-07-04&realtime_end=9999-12-31`, use each observation's
  first `realtime_start` as its known date). Stored in hedgefund.db table
  `ext_observations(series_id, date, value, realtime_start, source,
  fetched_at)`. FRED key via `secrets.require_secret("fred-api-key")`. Failure
  → `DataSourceUnavailable` (no stand-in).
- `calendar.event_risk`: 1 on the trading day before/of a high-impact US
  release (CPI, employment situation, FOMC, GDP), else 0 — from FRED release
  dates (dates are scheduled in advance → point in time). If a release
  cannot be loaded → the feature fails with a clear message.
- Tests use fakes only (no network).

### 7.9 Alpha library v1 (`alphas/`)

Each entry = a valid spec + metadata `{key, family, reference (paper/book,
year), expected_holding_days, notes}`. ~15 classic + Alpha101 subset:
time-series momentum 12-1 (Moskowitz/Ooi/Pedersen 2012), cross-sectional
momentum 12-1 (Jegadeesh/Titman 1993), dual momentum (Antonacci 2014),
short-term reversal 5d, 52-week-high proximity (George/Hwang 2004), SMA
50/200 trend, Donchian breakout 55/20 (turtle), RSI(2) mean reversion
(Connors), Bollinger z-score reversion, low volatility (Ang et al. 2006),
inverse-volatility allocation, sector-rotation momentum (sector ETFs),
risk-on/off switch (SPY vs TLT trend), crypto trend with vol target,
volatility-targeted trend; Alpha101 (Kakushadze 2016) subset that needs only
OHLCV/vwap and no industry neutralisation (#2, #3, #4, #6, #9, #12, #13,
#14, #16, #33, #41, #53, #54, #101) with a note that they were designed for
single-stock cross-sections; **Council Rules (no AI)** (named "Agentic
Committee (rules)" until 2026-10-06; key `agentic_committee_rules`): Technical
(SMA50/200, 20d/60d momentum, RSI, ATR) + `macro.regime_score` + 
`calendar.event_risk` as size reduction, mirroring `fund/agents` rules.
Acceptance: every entry validates with 0 errors, passes the causality
property and produces finite weights on synthetic data.

### 7.10 Lifecycle registry (`lifecycle/registry.py`, hedgefund.db v2)

Tables: `strategies(id PK, name, origin, category, state, current_version,
created_at, updated_at, state_changed_at, library_key UNIQUE NULL)`,
`strategy_versions(strategy_id, version, spec_json, content_hash,
envelope_json, validation_json, created_at, PK(strategy_id, version))` —
insert-only (no UPDATE/DELETE code path), `strategy_transitions(id, ts,
strategy_id, version, from_state, to_state, actor, reason, evidence_json)`.
States: `idea draft backtested candidate validated incubation live scaled
retired`. Stage-2 rules: user may move `idea↔draft`, any → `retired`,
`retired → draft`; every state from `backtested` upward needs evidence
(run-card ids, stage 3+) → refused with "needs a backtest run card — comes
with stage 3"; `live`/`scaled` additionally need the live guard (stage 8).
Reason text required (≤ 500) for every user transition; audit row each.
Library seeding at startup: idempotent by `library_key` + content hash; a
changed library spec becomes a new version of the library strategy (origin
library, actor system).

### 7.11 API v2 additions (behind api_auth; error shapes as §4)

- `GET /v2/dsl/operators` → `{operators: catalogue(), fields, ext: features.catalogue()}`
- `GET /v2/universes` → `{universes: [{key, name, symbols: [{symbol, category, asset_class}], note}]}`
- `GET /v2/strategies?state=&origin=&category=` → `{strategies: [{id, name,
  origin, category, state, current_version, content_hash, tags,
  updated_at, ai_generated, external_text, library_key, valid}]}`
- `GET /v2/strategies/{id}` → `{strategy, versions: [{version, content_hash,
  created_at, origin}], transitions: [...], allowed_transitions: [{to,
  allowed, reason_if_not}]}`
- `GET /v2/strategies/{id}/versions/{v}` → `{spec, spec_text (pretty JSON),
  envelope, validation, summary: {universe_text, signal_text, features_text:
  {name: text}, portfolio_text, rebalance_text, risk_text}, library: metadata or null}`
- `POST /v2/strategies/validate` `{text}` → ValidationResult (200 even when
  invalid; 413-style 400 when > MAX_TEXT_BYTES)
- `POST /v2/strategies` `{text, origin: "user"|"import", file_name?,
  file_sha256?}` → 201 `{id, version}`; invalid → 400 with ValidationResult
  (only valid specs are stored)
- `PUT /v2/strategies/{id}` `{text, base_version}` → 200 `{id, version}`;
  stale base → 409; library → 409 "Library strategies are read-only —
  duplicate them first."
- `POST /v2/strategies/{id}/duplicate` `{name?}` → 201
- `POST /v2/strategies/{id}/transition` `{to, reason}` → 200 / 409 with message
- `POST /v2/strategies/{id}/preview` `{version?, days?}` → preview (7.7);
  data failure → 503 user message.
- `GET /v2/strategies/{id}/versions/{v}/export` → `{file_name, text}` (spec
  with `provenance.exported_from = {strategy_id, version, content_hash}`).
- Request body for text routes may be up to MAX_TEXT_BYTES + 4 KB; aiohttp
  `client_max_size` must allow it (check main.py's app creation — HedgeFund
  file).

### 7.12 WPF (HedgeFund only)

- `HedgeFundWindow`: a slim left navigation rail (design system tokens,
  keyboard accessible, `AutomationProperties.Name`): **Overview** (today's
  dashboard content unchanged), **Library**; later entries (Studio, Loop,
  Validation, Portfolio, Trading, Shadow Account) are NOT shown yet. Toolbar
  with Financial Newsroom, Keys & data, Run now, Kill switch stays pinned.
- `Platform/LibraryView.xaml(.cs)`: tabs **Board** (columns per lifecycle
  state with strategy cards: name, category, origin chip, version, chips
  "external text · unverified" / "AI involvement unknown") and **Alpha
  library** (table: name, family, reference, holding days). Detail pane:
  summary texts, signal expression, parameters table, versions,
  transitions, validation warnings, buttons Edit (user strategies) / Duplicate
  as draft / Move to… (only allowed transitions enabled; disabled ones show
  `reason_if_not` as tooltip; reason prompt) / Preview signal (table of
  symbol, signal, rank, target weight; errors shown verbatim) / Export….
- `Platform/StrategyEditorWindow`: monospace JSON editor (AcceptsReturn,
  AcceptsTab, no word wrap), live validation (debounced 600 ms →
  POST /validate), error list (path, code, message, line/column for parse
  errors; double-click on a parse error moves the caret), operator help pane
  from `/v2/dsl/operators`, Save (new strategy or new version with
  base_version; 409 → reload prompt). New strategy starts from a template.
- Import: "Import…" button → OpenFileDialog `*.json`; refuse > 256 KB before
  reading; read strict UTF-8; confirmation dialog "Imported strategies are
  untrusted: they start as draft, their claims are shown as unverified, and
  nothing in them runs code." → POST origin import with file name only + SHA-256.
- Export: SaveFileDialog, writes `text` from the export route.
- All texts from strategies are shown as plain text. No AI output in stage 2.
- Stage-5 reminder (do not build now): external texts must be delimited as
  untrusted data before they are given to an LLM.

### 7.13 B2 notes (implemented 2026-10-02: features, storage v2, lifecycle, API)

- **Features** (`strategy/features/`): `ExtFeature(name, description, load,
  point_in_time, scope, source, key_for)`, `values_for(symbols, start, end) ->
  {symbol: {date: value}}` (used by `evaluate.load_ext`), registry `get`
  (KeyError), `names`, `catalogue() -> [{name, description, point_in_time,
  scope, source}]`. Timing rule: the value for day d uses data first published
  on or before d − 1 (FRED dates carry no time of day; H.15 yields come after
  the close). A day before any input is known has NO value (never 0).
- `features/fred.py`: sync stdlib client, every request through
  `http_gate.gate_for("fred")` (default 60/min; 429/5xx/timeouts retried),
  key per call via `require_secret("fred-api-key")`, errors redacted with
  `newsroom.keys.redact`; 401/403 or a 400 about `api_key` → "FRED rejected
  the API key …". ALFRED request = `output_type=4` (initial release only) +
  `realtime_start=1776-07-04&realtime_end=9999-12-31`, earliest
  `realtime_start` per date kept; observation windows of 366 days (daily),
  20 y (monthly) to stay below FRED's 2000-vintage limit per JSON request
  (residual risk: if FRED counts vintages over the whole real-time period a
  daily series answers HTTP 400 — surfaced verbatim, not hidden).
- `macro.regime_score` (scope `asset_class`; keys = the macro agent's asset
  classes, `key_for = fund.agents.macro.asset_class`): runs the agent's own
  `evaluate()` per day on a context built from first releases of DGS10,
  T10Y3M, VIXCLS, BAMLH0A0HYM2, SAHMREALTIME, FEDFUNDS, PCEPILFE/PAYEMS/INDPRO
  (`pc1`); regime via `newsroom.macro_derived.regime` on the last 48 known
  monthly points; real policy rate = FEDFUNDS − core PCE YoY on the newest
  common month. History fetched from start − 5 y. Stored in
  `ext_observations`; meta `ext_cover:fred:<series>` = {from, fetched_at}:
  the store is used only when it covers the start AND was fetched after
  `end` (then all releases known by `end` are stored), else FRED is asked again.
- `calendar.event_risk` (scope global): FRED releases CPI 10, Employment
  Situation 50, GDP 53, FOMC Press Release 101 (`release/dates` incl.
  scheduled dates), for every calendar day: CPI / jobs / GDP (BLS/BEA
  calendars published months ahead; reschedulings are announced before the
  new date) → 1 on the release day AND the NYSE trading day before it; FOMC →
  1 ONLY on the release day (non-trading day → next trading day), because
  release 101 also holds unscheduled actions (2020-03-03, 2020-03-15,
  2008-01-22, 2001-09-17 …) and FRED does not mark scheduled meetings — a
  day-before flag would be look-ahead (review fix 2026-10-03); the statement
  is known by that day's close and signals trade at the next open/close.
  Else 0. Fails (`DataSourceUnavailable`) when a
  release has no date on/before `start` or no scheduled date after `end`.
- **Storage v2**: tables as §7.10 + `ext_observations` (PK series_id, date,
  source); triggers make `strategy_versions` insert-only. db API:
  `strategy_create`, `strategy_add_version` (raises `StaleVersion`),
  `strategy_set_state`, `strategy_get`, `strategy_by_library_key`,
  `strategies_list(state, origin, category)`, `strategy_versions_list`,
  `strategy_version_get`, `strategy_transitions_list`,
  `upsert_ext_observations`, `query_ext_observations`. Every write and its
  audit row share one transaction.
- **Lifecycle** (`lifecycle/registry.py`): ids `s-<12 hex>`; stored spec =
  `with_defaults(spec)` without `provenance` (claims → `envelope.claimed`,
  ≤ 16 KB, for every origin); `created_by` user|system; import →
  `ai_generated "unknown"`, `external_text true` (sticky across edits and
  duplicates), `source_file {name (base name, ≤ 120 chars), sha256 + bytes of
  the received text, sha256_claimed (client hash, only when different)}`.
  Library strategies start in `idea`, user/import/duplicate in `draft`;
  library strategies accept no transition (read-only). Saving identical
  content returns `{unchanged: true}` without a new version. Audit actions:
  `strategy_created`, `strategy_version_created`, `strategy_transition`.
  `seed_library()` runs in main.py after `sp_storage.init`; its status
  `{ok, at, message, created, updated, unchanged, errors: [{key, message}]}`
  is returned as `library_status` by GET /v2/strategies.
- **API response shapes** (beyond §7.11): spec errors → 400
  `{error, ok, errors: [...], warnings, facts}`; body/query errors → 400
  `{error, errors: {field: message}}`; unknown id/version → 404 `{error}`;
  409 `{error, current_version?}`; POST /transition → `{strategy,
  allowed_transitions}`; PUT → `{id, version, unchanged}`; GET
  /v2/strategies/{id} `strategy` also has `created_at, state_changed_at,
  description, library` (metadata or null). Preview `days` 1..250 (default
  20, = `preview.MAX_PREVIEW_DAYS`) — route limit now 1..5, default 5 (§9.9). Body limit: `client_max_size = 4 × MAX_TEXT_BYTES + 4 KB` (texts arrive
  JSON-escaped; .NET escapes non-ASCII as \uXXXX); the decoded text is
  checked against MAX_TEXT_BYTES (400 "too large").

### 7.14 B1 notes (implemented 2026-10-02: limits, schema, DSL, validator, evaluator, portfolio, SDK, universe, library, preview)

- **Public API** (for B2/WPF): `limits.parse_spec_text(text)` (`SpecParseError`
  with `code`, `line`, `column`, `as_error()`); `validator.validate(spec, *,
  ext_catalogue=None)`, `validate_text(text, *, ext_catalogue=None)`,
  `parse_and_validate(text) -> (spec|None, ValidationResult)`
  (`.ok/.errors/.warnings/.facts/.to_dict()/.codes()`); `schema.with_defaults`,
  `canonical_json`, `content_hash`, `pretty_json`; `dsl.catalogue()`,
  `dsl.expr_text(node)`, `dsl.FIELDS`; `summary.summary_texts(spec)` (the §7.11
  `summary`); `universe.universes()`, `resolve(universe)`; `alphas.library()` /
  `LIBRARY` = `[{key, spec, metadata: {key, family, reference,
  expected_holding_days, notes}}]`, `alphas.get(key)`, `metadata(key)`, `keys()`;
  `evaluate.evaluate(spec, panel, *, ext_values=None, param_values=None)`;
  `portfolio.target_weights`, `rebalance_dates`, `weights_history`,
  `held_weights`; `sdk.Strategy/Context/SpecStrategy/run`;
  `preview.preview(spec, as_of=None, days=20, *, get_bars=None, clock=None,
  ext_values=None, ext_catalogue=None, configured_source=None,
  time_limit_s=60, monotonic=time.monotonic)`.
- **Extra operators** (beyond §7.4, needed by the library): `exp`, `tanh`
  (technical agent's momentum squashing) and `nan()` (a missing value, to
  exclude a symbol, e.g. dual momentum's absolute filter).
- **Parse codes**: oversize text / depth / nodes → `limit_exceeded`; syntax,
  duplicate keys, NaN/Infinity, non-object root, non-text → `parse_error`
  (+ `line`/`column` for syntax errors). Depth counts containers (root object
  = 1), scanned on the text before `json.loads`. `validate(dict)` applies the
  same depth/node limits.
- **Validator code choices**: a window/shift/half-life ≤ 0 (literal or a
  parameter's `min`) → `look_ahead`; non-integer, too long, below the op's
  minimum (ts_std/ts_rank/ts_zscore/ts_corr/ts_cov/rsi ≥ 2, ts_skew ≥ 3) or not
  constant → `bad_window`; enum mismatch → `out_of_range`; non-finite number or
  wrong JSON type → `bad_type`; parameter names follow the feature-name rule.
  `facts.max_lookback` = history to load: lag-like ops add n, windows n − 1,
  Wilder ops (rsi, atr) 4n, `ewm_mean` ceil(4·halflife), plus `vol_lookback`
  for inverse_vol; window parameters at their `max`.
- **Units/ranges**: `risk.*_pct` in percent (0, 100], `vol_target_annual` a
  fraction (0, 5]; `gross_exposure` (0, 3]; `max_weight` (0, 1]; custom costs
  0..1000 bps. The risk block is stored and summarised but applied only by the
  backtest/runner (stage 3+). Defaults written by `with_defaults`: description
  "", tags [], category other, timeframe 1Day, parameters/features {},
  portfolio {long_only, all, equal, vol_lookback 60, max_weight 1, gross 1},
  rebalance daily, risk all null, execution market/next_open, costs
  paper_broker, preset asset_classes [equity, crypto], param integer false /
  description "". `content_hash` hashes `with_defaults(spec)` minus provenance,
  so a spec and its stored form hash the same.
- **Semantics**: `ts_rank` 0..1 (average ties); `ts_argmax/argmin` = bars
  since the extreme (ties → most recent); `ewm_mean` and Wilder smoothing start
  at the panel's first row and restart after a missing value (causal; a
  backtest and a preview with different start dates can differ slightly in
  the warm-up); `cs_rank` with one value → 0.5. Portfolio rules in the
  `portfolio.py` docstring: threshold is strict, inverse_vol needs a complete
  `vol_lookback` window (else the symbol is not eligible), a zero weight total
  → no position (cash), never a stand-in weighting.
- **Panel**: in a mixed universe a crypto value must be known ≤ the session
  close AND within 24 h before it (with complete data it always is) — an older
  bar is not carried forward; rows end at the last session with an equity bar.
- **External features**: evaluator aligns `values_for` output as of each panel
  date (newest value dated ≤ the row date) and loads from first date − 400 days
  when values are not injected; `ext_values` accepts ndarray / per-symbol /
  global dicts (tests inject fakes).
- **Preview**: per symbol first `get_bars` without freshness (empty → excluded
  and listed in `symbols_missing`), then with `require_fresh_through` (last NYSE
  close ≤ as_of, crypto: as_of) — stale or failing data raises the cache's
  `DataSourceUnavailable`. Extra fields: `history[].rebalance`; warnings may add
  `symbols_missing` and `no_signal`. Invalid spec/days/as_of → `InvalidRequest`;
  > 60 s → `PreviewTimeout(UserFacingError)`.
- **SDK**: `on_bar` returns `None` on non-rebalance bars (keep positions); the
  acceptance test compares `sdk.run(SpecStrategy)` with
  `portfolio.weights_history`.
- **Library** (30 entries): 15 classic + 14 Alpha101 + Council Rules (no
  AI). Stateless approximations are named in each entry's notes (Donchian
  as "55-day high more recent than 20-day low", RSI(2) held while the
  condition holds, dual momentum vs 0 instead of T-bills); the committee's
  documented differences are in `alphas/composite.py`. Tests:
  `tests/test_sp_strategy_{limits,validator,eval,portfolio,preview}.py`,
  `tests/test_sp_alphas.py` (fakes only).
- **Review fixes (2026-10-02)**: rolling windows run in chunks of ≤
  `limits.ROLLING_CHUNK_ELEMENTS` (2 M values) — memory O(dates × symbols);
  `evaluate.estimate_cost` (Σ per-cell op cost — window ops n, ts_corr/cov 2n,
  rsi/atr/ewm and cs ops +10, others 1 — × dates × symbols, features once) is
  checked before every evaluation: > `limits.MAX_EVAL_CELL_OPS` (2e10; every
  library entry fits 500 symbols × 2600 days, worst ≈ 1.2e9) →
  `EvaluationTooExpensive(UserFacingError)` "This strategy is too expensive to
  evaluate (…)"; `facts.max_lookback` > `limits.MAX_LOOKBACK` (5040) →
  `limit_exceeded`; keys/strings with lone surrogates → `parse_error` (text) /
  `bad_type` (dict); library labels: `tsmom_12m` (plain 12-month return, MOP
  2012), `xsmom_12_1` reference adds the Carhart (1997) 12-1 convention,
  low-volatility / SMA 50-200 / risk-on-off notes no longer attribute rules to
  papers that used different ones.
- Review fixes (2026-10-03): `strategy_add_version` reads + inserts inside
  `BEGIN IMMEDIATE` and maps a version-number conflict to `StaleVersion`
  (409, never 503); an unreadable stored record (spec/envelope/validation
  JSON) raises `StorageUnavailable` (503) instead of an empty default that
  would drop the sticky `external_text`/`ai_generated` flags;
  `library_metadata` returns null only for "not a library strategy / key no
  longer in the library", other failures propagate; preview: a
  `ValueError`/`TypeError` from panel/evaluate/portfolio (bad source data
  after validation) becomes `PreviewDataError` (503 `{error, kind}`) instead
  of 400. Suggestion for B1: raise a `UserFacingError` from
  `Panel.from_bars` for malformed/duplicate bars.

## 8. Stage 3 — backtest engine v2 (contract, 2026-10-03)

Interview decisions (user, 2026-10-03): a **second data source, Tiingo**, for
daily equity/ETF history BEFORE 2016 — for the stress episodes 2001/2008 AND
for longer backtests (user explicitly requested it — this widens decision D8:
two sources, but never for the same period and never as a substitute for
each other); seam at the first Alpaca day with an overlap check and a
constant factor (§8.2); benchmark selectable per strategy, default SPY;
$100,000 capital, orders capped at 1 % of average daily volume; **raw
(split-adjusted) prices + dividends as cash** exactly like the paper
account; the old Overview backtest is replaced immediately (UI only — the
old `/fund/backtest` route stays until stage 10).

### 8.1 Data: price series and corporate actions

- **Source update (user decision 2026-10-06):** the free plan's IEX daily
  history turned out to start 2020-07-27 for almost all ETFs (SPY 2018-11),
  not 2016. Historical daily bars (both series) for backtests therefore come
  from Alpaca **SIP** — data type `equity_bars_history`, source label
  `alpaca_sip` (`AlpacaSipProvider`, `feed="sip"`, request end clamped to
  now − 15 min: "For historical queries, the `end` parameter must be at least
  15 minutes old to query SIP data without a subscription", Alpaca Market Data
  FAQ), licence `personal_use`. The live paper engine keeps IEX
  (`equity_bars`, latest quotes/trades) — its cached rows stay untouched
  under source `alpaca`; backtests (data/seam.py) read only `alpaca_sip` from
  the seam on, so IEX and SIP rows are never mixed. A refused SIP request →
  `DataSourceUnavailable` naming the 15-minute/subscription rule, never IEX
  instead. `POST /v2/data/test` adds the check "SIP historical daily bars
  (SPY, 2016-01-04 and newest)". Below, "Alpaca" for history means SIP.
  Daily-bar clamp (fix 2026-10-06, failure during market hours: a window
  reaching into today's session returns today's in-progress daily bar, which
  holds the last 15 minutes → "subscription does not permit querying recent
  SIP data"): `alpaca.sip_request_end("1Day", now)` = close of the last NYSE
  session whose close is ≥ 15 min ago (`calendars.last_close_before(now −
  15 min)`); intraday: now − 15 min. Applied inside
  `AlpacaProvider.get_bars` for the SIP feed, so every path (cache
  head/tail/refetch, seam overlap, volume, connection test) uses it; the
  effective end is logged at DEBUG.
- Two price series per symbol and day, stored separately (hedgefund.db
  migration: `bars` gets column `adjustment TEXT NOT NULL DEFAULT 'all'` in
  the primary key; existing rows = `all`):
  - `all` (splits + dividends) → **signals** (DSL evaluation), as today;
  - `split` (splits only) → **fills, valuation, volume limit**.
- Dividends (cash, ex-date, pay-date if known) and splits per symbol in a
  table `corporate_actions(symbol, kind, ex_date, cash_amount, split_ratio,
  source, known_at, fetched_at)`; Alpaca corporate actions for ≥ 2016 (the
  provider already exposes them), Tiingo `divCash`/`splitFactor` for < 2016.
- Alpaca: `adjustment="split"` request for the `split` series (same IEX feed,
  same gate, same cache rules incl. freshness and the adjustment-change
  head check).
- **Tiingo provider** (`data/providers/tiingo.py`, data type
  `equity_bars_pre2016`, licence `personal_use`): REST
  `https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate=&endDate=`
  with header `Authorization: Token <key>`; key via
  `secrets.require_secret("tiingo-api-key")` (add to `secrets.SECRETS`, label
  "Tiingo API key", no legacy env names); TLS verified; requests through
  `http_gate` with Tiingo's documented free-tier limits (check the official
  docs/pricing page and put the numbers + source URL in a comment; be
  conservative). From one response build: `split` series (raw OHLC divided by
  the cumulative future split factor up to the seam date), `all` series
  (`adjOpen/adjHigh/adjLow/adjClose/adjVolume`), dividends, splits. Only
  dates < the seam date are ever stored from Tiingo. Missing key →
  `SecretMissing` ("Tiingo API key is not set — enter it in Hedge Fund →
  Keys & data."), failures → `DataSourceUnavailable("tiingo", …)`.
  No network in tests (fakes).
- The cache must never mix sources for one (symbol, adjustment, day).
- **On demand only (user, 2026-10-03):** no bulk preload — Tiingo data for a
  symbol is fetched the first time a backtest needs that symbol before the
  seam, then cached; progress shows the rate-limit wait.

### 8.2 The seam (`data/seam.py`)

- Seam date = 2016-01-04 (first NYSE session of 2016; constant).
- Overlap window = the first 60 NYSE sessions from the seam: fetch both
  sources for it (Tiingo only for this check — not stored as prices).
- Per symbol and adjustment: daily returns in the overlap must agree
  (default: |r_tiingo − r_alpaca| ≤ 0.002 on ≥ 95 % of days and no single
  day > 0.02 — constants in one place); then factor = median(alpaca_close /
  tiingo_close) over the overlap; the ratio's relative spread must be
  ≤ 0.5 %. Pass → all Tiingo prices < seam × factor (volume ÷ factor for the
  `split` series when the factor reflects a split basis difference — document
  the rule). Fail → no pre-2016 data for that symbol; the backtest lists it
  in `warnings` (`seam_mismatch`, with numbers) — never guessed.
- Seam results stored (`seam_checks(symbol, adjustment, checked_at, ok,
  factor, stats_json)`) and shown in the run card.
- Validator: a spec that compares a raw price/volume to an absolute
  constant (e.g. `gt(close, 100)`) gets warning `absolute_price_level` (always)
  and the backtest the run-level warning `absolute_level_two_sources`
  when the backtest range starts before the seam.

### 8.3 Engine (`backtest/engine.py`)

- **Drives the real `fund.broker.PaperBroker`** with an in-memory store
  (`backtest/memstore.py`, same functions the broker uses from `fund.store`)
  and a simulated clock, quote function and dividends function → identical
  fees, slippage, Reg T margin, borrow fees, Rule 201, dividends, crypto
  perps (funding, liquidation). If PaperBroker cannot be driven that way
  without changing its behaviour, stop and report (no reimplementation that
  could drift).
- Loop per session day t (equities: NYSE; crypto-only: UTC days; mixed:
  NYSE sessions with crypto PIT alignment as in §7.7): signals from data
  known at the close of t (`all` series) → on rebalance days targets via
  `strategy.portfolio` → orders executed at `execution.timing`: `next_open`
  = open of t+1 (`split` series), `next_close` = close of t+1; daily
  accruals (dividends cash on ex-date for held longs, paid by shorts;
  borrow; funding) via the broker; mark to market at the close.
- **Participation limit:** order notional ≤ 1 % of the 20-day average
  dollar volume (`split` close × volume) known at t; excess dropped (not
  carried), counted in warnings. Missing volume → order refused for that
  symbol that day + warning (no default volume).
- Risk block of the spec (stop loss, take profit, vol target, drawdown
  stop) enforced; `costs.model = custom` overrides broker fees/slippage for
  this run only.
- Benchmark: request field `benchmark` (symbol), else spec field
  `benchmark` (optional key, NOT default-filled, validator: valid symbol),
  else `SPY`; buy-and-hold with the same costs and dividends.
- Capital default 100,000 (request may set 1,000–100,000,000).
- Deterministic: same inputs → identical result (seeded RNG only for
  Monte-Carlo).
- **Parity test (acceptance):** for fixed bars/targets, the fills, cash
  ledger and positions of the backtest equal those of a PaperBroker run
  with the normal store fed the same quotes.

### 8.4 Metrics, run card, stress tests

- Metrics (`backtest/metrics.py`): CAGR, vol, Sharpe, Sortino, Calmar, max
  drawdown + duration, hit rate, profit factor, turnover, avg gross/net
  exposure, alpha/beta vs benchmark, costs total (fees, slippage, borrow,
  funding) and as % of gross P&L; signal quality: daily cross-sectional rank
  IC (signal at t vs forward returns of the `all` series) for horizons
  1/5/10/21/63, ICIR, IC decay curve + half-life (time-series strategies:
  rolling-13-week positive share instead); capacity estimate (capital at
  which the 1 % limit would bind on 10 % of orders).
- **Run card** (`backtest/runcard.py`, table `backtest_runs`, insert-only
  once finished): id, created_at, strategy_id, version, content_hash, spec
  snapshot, request params, data snapshot (per symbol: sources + ranges,
  seam result, row hashes), licence classes, cost model, engine version,
  seed, metrics, equity curve (daily: equity, benchmark, gross/net exposure),
  trades summary, warnings (survivorship, seam, gaps, short history,
  missing symbols, participation cuts), status (queued, running, done,
  failed, cancelled), error message; trial number (null until stage 6).
- **Stress tests** (`backtest/stress.py`), run on request:
  episodes = dot-com 2000-03-24→2002-10-09, GFC 2007-10-09→2009-03-09,
  Q4 2018 2018-09-20→2018-12-24, Covid 2020-02-19→2020-03-23, rate shock
  2022-01-03→2022-10-12, crypto 2022 2021-11-10→2022-11-21 (only symbols
  with data in the window; an episode without enough data → "not available:
  …"); synthetic = costs ×2, slippage ×3, Monte-Carlo bootstrap of daily
  returns (1,000 paths, fixed seed → 5/50/95 % drawdown and CAGR).
- Lifecycle: `draft → backtested` is now allowed when a finished run card
  for the current version exists (its id becomes the evidence);
  `allowed_transitions` reflects it.

### 8.5 Jobs + API (behind api_auth)

- One backtest at a time (CPU bound): `backtest/jobs.py` runs it in a
  worker thread, progress events (phase: data, seam, simulate, metrics,
  stress; percent; message e.g. "waiting for Tiingo rate limit (n
  requests left this hour)"), cancellation flag checked per simulated day.
  A second request while one runs → 409 "A backtest is already running."
- `POST /v2/backtests` `{strategy_id, version?, start?, end?, benchmark?,
  capital?, stress?: bool, history_from?: "2016" | "2000"}` → 202 `{run_id}`; defaults: start = earliest
  date with data for the universe (≥ 2000-01-03; pre-seam only with a
  Tiingo key — without it start defaults to the seam and an explicit
  earlier start → 503 SecretMissing), end = last complete session.
  **User decision 2026-10-03:** the default start is 2016-01-04 (the seam,
  first Alpaca day — `engine.run(start=None)` loads from there); a start
  before it (≥ 2000-01-03, Tiingo key needed) is an explicit user choice.
  **Fix 2026-10-09 (user decision): `history_from`** = the data LOAD start
  of a run without an explicit start — `"2016"` (default) = 2016-01-04,
  `"2000"` = 2000-01-03 (Tiingo key needed); trading starts after the
  strategy's warm-up in both cases. `start` keeps meaning an explicit first
  TRADING day: its warm-up is loaded before it, `history_from` is then only
  recorded; a warm-up reaching before 2000-01-03 → error "The backtest needs
  N sessions of history before its start (…), but the earliest data is
  2000-01-03 — use 'From 2000' or a later start." (was: "The NYSE trading
  calendar has no data for 1999", because the dialog's "From 2000" toggle
  sent start = 2000-01-03). The dialog toggle sends `history_from: "2000"`
  and no start (unless a start date is typed). Stress episodes keep their
  documented clamp (§8.10: warm-up from 2000-01-03 at the earliest, warning
  `short_warmup`) via `engine.run(clamp_warmup=True)` — the dot-com episode
  (2000-03-24) failed on the same 1999 calendar error before.
  Tests: `test_sp_backtest_history_from.py` (12).
- `GET /v2/backtests?strategy_id=&limit=` → list (id, created_at, status,
  strategy name, version, range, key metrics).
- `GET /v2/backtests/{id}` → full run card (+ `progress` while running).
- `POST /v2/backtests/{id}/cancel`.
- `GET /v2/settings/keys` lists the new Tiingo key (status only).

### 8.6 WPF

- Library detail: **Run backtest…** (dialog: range, benchmark, capital,
  stress tests on/off) → progress panel (phase, percent, message, Cancel).
- `Platform/BacktestReportView`: equity vs benchmark (seam line marked),
  drawdown chart, metrics table, IC decay chart, stress results table,
  warnings list, data/licence section, run history per strategy.
- Overview: the old Backtest button opens the new dialog for "Council
  Rules (no AI)" (library key `agentic_committee_rules`) instead of the old window; old code
  untouched otherwise (removed in stage 10).
- Keys & data: Tiingo key row (set / remove; import from `.env` not
  applicable — no legacy name).
- No AI output in stage 3.

### 8.7 3b notes (implemented 2026-10-03: data — §8.1, §8.2)

- **Storage v3** (§5.1): `bars.adjustment` in the PK (rebuild in one
  transaction, existing rows `all`), `corporate_actions`, `seam_checks`;
  `migrate()` now rolls back a failed version. `pit.action_known_at(ex_date)`
  = 09:30 NY of the ex-date (conservative PIT for actions, any year).
- **Cache/Alpaca**: `BarCache.get_bars(..., adjustment="all"|"split")`,
  `cache.get_bars(..., adjustment=)`, `pit.bars_as_of(..., adjustment=)`;
  each series has its own cache key, freshness and adjustment-change checks;
  rows carry `adjustment`. `"all"` keeps the stage-1 call shape (no keyword
  to the provider). `split` for crypto → `ValueError` (crypto has no
  corporate actions; `seam.history` serves crypto's single series for both).
  `AlpacaProvider.get_bars(..., adjustment="split")` → `Adjustment.SPLIT`.
- **Tiingo** (`providers/tiingo.py`, data type `equity_bars_pre2016` →
  `tiingo`, licence `personal_use`): stdlib `urllib` + `ssl.create_default_context()`,
  header `Authorization: Token`, key per call via `require_key()` (message
  "Tiingo API key is not set — enter it in Hedge Fund → Keys & data."),
  every error text `redact`ed. Limits from https://www.tiingo.com/about/pricing
  (free: 50 req/h, 1,000 req/day, 500 symbols/month, 1 GB/month, internal
  use only); gate = 40/h + 800/day sliding windows (`http_gate.WindowLimiter`,
  since 2026-10-05 persisted in hedgefund.db meta, §8.12), 20/min
  burst 3, acquire timeout 3,900 s, progress events every 5 s
  `{source, waiting, wait_seconds, message: "waiting for Tiingo rate limit (n
  requests left this hour, …)", remaining}` (the callback may raise to
  cancel). 429 → stop at once (no retry), 5xx/timeouts → gate retries,
  404 → `UnknownSymbol` (InvalidRequest; seam turns it into warning
  `no_pre_seam_data`). Fields per https://www.tiingo.com/documentation/end-of-day.
- **One request per symbol**: `2000-01-03 … overlap end (2016-03-30)`.
  Split series = raw ÷ product of later `splitFactor`s up to the END OF THE
  RESPONSE (= overlap end, not the seam date — one split basis for the
  pre-seam rows AND the overlap, so a split inside the overlap cannot break
  the check; splits after it are absorbed by the factor). Only rows < seam
  are stored (`Seam._store_pre_seam` is the only Tiingo write path and
  refuses later dates); the overlap closes/volumes and overlap splits are kept
  in `meta` key `tiingo_history/<SYMBOL>` (written last = fetch complete;
  `not_found` is remembered too). Stored Tiingo prices are unscaled; the
  factor is applied at read time.
- **Seam** (`data/seam.py`, constants at the top): the check is re-run
  against the CURRENT Alpaca overlap at every call (a new `seam_checks` row
  only when the result changes), so Alpaca re-basing after a dividend/split
  updates the factor without refetching Tiingo. Spread = (max − min) /
  median of the ratio; ≥ 50 common returns required. Volume rule: ÷ factor
  for both series (dollar volume invariant). Known discontinuity, not
  corrected: Alpaca free = IEX-only volume vs Tiingo consolidated → warning
  `volume_basis_differs` (median IEX/consolidated ratio from the overlap).
  Other warnings: `seam_mismatch` (with stats), `no_pre_seam_data`,
  `short_history`. (`volume_basis_differs` removed again with §8.8.)
- **Public API for 3c**: `seam.history(symbol, adjustment, start, end=None,
  *, progress=None) -> (rows, info)`; info `{symbol, adjustment, start, end,
  sources, ranges: {source: {first, last, rows}}, seam: {ok, factor, stats,
  checked_at} | None, warnings, licences}`. `seam.corporate_actions(symbol,
  start, end=None, *, progress=None) -> (rows, info)`: Tiingo < seam (only
  when the `split` seam check passes), Alpaca ≥ seam (provider op, stored,
  refreshed once per NY day from the seam through today); each row adds
  `cash_amount_split_adj` = dividend per share on the `split` series' share
  basis (raw ÷ later splits; Tiingo also × factor). Splits are informational
  — `split` prices are already split-adjusted. Pre-seam start without key →
  `SecretMissing`; start < 2000-01-03 → `InvalidRequest`.
- `GET /v2/settings/keys` lists `tiingo-api-key` (from `secrets.SECRETS`).
  WPF: `CredentialStore.Secrets` row (no env names); Keys & data shows "No
  legacy .env name"; `.env` import skips keys without env names. C# checked
  statically only (no dotnet here). Registry `test_source` stays Alpaca-only.
- **Volume (§8.8, 2026-10-03)**: data type `equity_volume` → `tiingo`
  (registry, licence `personal_use`), storage v4 table `volumes`.
  `seam.volume(symbol, start, end=None, *, progress=None) -> (rows, info)`,
  rows `{date, volume, close_split, dollar_volume_split, known_at, source}`
  (shares on the CURRENT split basis = Alpaca `split` series basis;
  `dollar_volume_split` = volume × split close = raw close × raw volume),
  info `{symbol, start, end, source, licence, first, last, rows, basis_date,
  warnings}`. ONE Tiingo request per symbol for 2000-01-03 … today
  (`TiingoProvider.volume_prices`), which also feeds the pre-seam price
  record (built from the rows up to the overlap end = same split basis as
  before) — `history()` now also requests the full range, so a symbol costs
  one request in total. Tail refresh from the stored `through` date when the
  last completed session in the range is missing; a split after `through`
  → full refetch (`replace_all`). Still missing after the fetch → 
  `DataSourceUnavailable` "no consolidated volume for X through D yet"; no
  key → `SecretMissing`; unknown ticker → `UnknownSymbol`; Tiingo failure →
  its error — never an IEX stand-in. Crypto → `ValueError` (crypto volume
  stays in `crypto_bars`). `history()` no longer emits
  `volume_basis_differs` (obsolete; the median IEX/consolidated ratio stays
  in the seam stats).
- Tests: `test_sp_data_tiingo.py` (12), `test_sp_data_seam.py` (13),
  `test_sp_data_volume.py` (6).

### 8.8 Volume source decision (user, 2026-10-03)

Alpaca's free feed is IEX only (~2-3 % of US consolidated volume), so the
1 % participation limit on IEX volume would be 30-50x too strict after the
seam and would break behaviour at the seam. Decision: **Tiingo is THE source
of the data type `equity_volume` for ALL years** (consolidated volume, split
basis, from the same Tiingo daily response, fetched on demand); prices stay
Alpaca >= seam / Tiingo < seam. One source per data type is kept (D8).
Tiingo unavailable or key missing -> the backtest stops with the clear
message (no IEX volume stand-in). The participation limit uses only
`equity_volume`; crypto volume stays Alpaca (crypto_bars). The
`volume_basis_differs` warning of 3b becomes obsolete for the limit.

**Superseded 2026-10-06 (user decision):** with Alpaca SIP history (§8.1)
`equity_volume` = Alpaca SIP consolidated volume from the seam on (the
`split` series' volume, source `alpaca_sip`) and Tiingo volume ONLY before
the seam, joined like prices: the seam check of the `split` series also
compares volumes (stats `volume`: per overlap day r = sip / (tiingo ÷
factor); |r − 1| ≤ 25 % on ≥ 90 % of the days and |median r − 1| ≤ 10 %; ≥ 50
days); mismatch → no pre-seam volume (orders before the seam refused for
lack of volume) + warning `seam_volume_mismatch`. `seam.volume(symbol,
start, end=None, *, progress=None) -> (rows {date, volume, close_split,
dollar_volume_split, known_at, source}, info {symbol, start, end, source,
sources, ranges, seam, warnings, licences, first, last, rows})`. The Tiingo
key is needed only when a run loads equity data before the seam (start incl.
warm-up < 2016-01-04; `engine.needs_pre_seam_equity`, checked by the API at
submit time). The `volumes` table (schema v4) is no longer written (kept,
schema migrations are append-only); Tiingo requests now cover only
2000-01-03 … overlap end.

**Crypto (user decision, 2026-10-03):** NO participation limit for crypto
symbols — Alpaca crypto volume covers only Alpaca's own venue and would be
far too strict. The engine skips the 1 % cap for crypto (equities unchanged:
Tiingo `equity_volume`) and adds one run-level warning
`crypto_no_participation_limit` ("No volume limit for crypto (user decision
2026-10-03) — fills assume the order size is always tradable.") when the
universe contains crypto.

### 8.9 3c notes (implemented 2026-10-03: engine core — §8.3)

- **Files**: `backtest/__init__.py`, `backtest/memstore.py`, `backtest/engine.py`;
  tests `test_sp_backtest_engine.py` (15), `test_sp_backtest_parity.py` (6),
  helper `test_sp_backtest_support.py`. `fund/broker.py` is UNCHANGED — the
  real PaperBroker is driven through its existing injection points (store,
  quote_fn, clock, dividends_fn, params_fn, bars_fn; session_fn = the real
  calendar).
- **memstore.MemStore(params)**: every fund.store function the broker uses
  (orders, positions, cash ledger, equity curve, events, fund.* meta), same
  shapes/semantics; no `db` attribute; sums with `math.fsum` (SQLite SUM may
  differ in the last bits). Extra: `all_orders()`, `all_cash_entries()`,
  `all_events()`, `list_orders`, `cash_entries`, `equity_points`.
- **engine.run(spec, *, start=None, end=None, capital=100000, benchmark=None,
  progress=None, cancel=None, data=None) -> BacktestResult** (`.to_dict()`).
  Fields: `engine_version` ("sp-backtest/3c.1"), `spec_hash`, `request`,
  `range {start, end, calendar, load_start, first_data, end_limited_by {date,
  symbol, source}, end_explicit, warmup_sessions, timing}`, daily `dates,
  equity, cash, long_mv, short_mv, drawdown_pct, benchmark` (None = valuation
  paused that day), `benchmark_info {symbol, calendar, orders, ledger}`,
  `positions` ([{date, positions}] only on change), `orders` (store rows),
  `fills` (compact), `ledger`, `events`, `warnings` ([{code, message,
  symbol?, count?, dates?, dropped_notional?, …}]), `data` (per symbol: role,
  excluded, `all`/`split`/`bars`/`volume`/`corporate_actions` = loader info +
  `row_hash` sha256), `broker_params`, `costs`, `stats {participation_cuts,
  dropped_notional, missing_volume_refusals, stop_exits, take_profit_exits,
  rebalances, days}`. Errors: `InvalidRequest` (spec/request/range),
  `BacktestDataError(UserFacingError)`, `BacktestCancelled(UserFacingError)`,
  plus the data layer's own errors (SecretMissing, DataSourceUnavailable,
  CalendarUnavailable). `DataLoader(seam=None, get_bars=None)` = the default
  loader (methods `history`, `corporate_actions`, `volume`, `crypto_bars`,
  `ext_values`); `phase_times(day, calendar)`; constants at the top.
- **Execution clock** (module docstring): NYSE open phase = open + 6 min,
  close phase = close − 1 min; UTC days 00:00 / 23:59. Per day: accruals →
  orders of the previous decision (next_open: open phase; next_close: close
  phase) → check_margin; close: accruals, …, snapshot, decision. Crypto in a
  mixed run: newest price known at the phase time (bar open at its start,
  close at known_at), ≤ 24 h old. Orders: `target(sym, w, "autopilot")`,
  reductions first; the participation cap sends `w' = capped qty × price /
  equity`. First day uses the targets of the latest rebalance row ≤ it
  (`held_weights` semantics).
- **Choices** (documented in the engine docstring): custom costs → broker
  fees = fee + slippage (half slippage / half fee, warning
  `custom_costs_split` when they differ); broker params = fund defaults with
  `starting_capital = capital`, `short_exposure_max_pct = 100 × gross`;
  vol target only de-levers; stop/take-profit symbols stay flat until the next
  rebalance; drawdown stop halts for the rest of the run; warm-up = max(
  max_lookback, 20, 61 with vol target) + 5 sessions; a symbol without any
  `all` bars is excluded (`symbol_excluded`), Tiingo `UnknownSymbol` for
  volume → `no_volume_source` and its orders are refused; benchmark of a
  crypto-only strategy with an equity benchmark runs on NYSE sessions (UTC
  days show its latest close value).
- **Follow-ups (2026-10-03)**: (1) `core.calendars` now covers 2000–2027:
  2016–2027 hardcoded as before, 2000–2015 rule-based (`RULE_YEARS`,
  `rule_holidays`, `rule_early_closes`; historical rules + special closures
  2001-09-11..14, 2004-06-11, 2007-01-02, 2012-10-29/30; early closes Jul 3
  Mon/Tue/Thu (Wed from 2013), Fri Jul 5 before 2013, day after Thanksgiving,
  Dec 24 Mon–Thu, 2003-12-26 — source: exchange_calendars XNYS, cited in the
  code); session counts tested (2000 252, 2001 248, 2004 252, 2007 251,
  2008 253, 2012 250, 2015 252) and the rules reproduce the 2016–2027 tables
  except Juneteenth and days of mourning. Before 2000 / after 2027 →
  `CalendarUnavailable`. The backtest seam test no longer patches the
  calendar. (2) Optional spec key `benchmark` (schema `TOP_KEYS`, NOT
  default-filled → existing content hashes unchanged, tested with a fixed
  hash; validator `bad_symbol` unless `instruments.instrument()` accepts
  it). (3) Engine default start loads from the seam (no Tiingo key needed);
  earlier ranges are explicit (§8.5). (4) Crypto has no participation limit
  (user, 2026-10-03, §8.8): `_Sim.execute` caps equities only, warning
  `crypto_no_participation_limit`; the crypto ADV code was removed. Still open: the BarCache tail slack (5 days)
  can leave the newest Alpaca rows uncached after a seam overlap fetch — the
  engine reports the shorter end via `end_limited_by`.

### 8.10 3d notes (implemented 2026-10-04: metrics, run card, stress — §8.4)

- **Engine hooks** (the only `engine.py` changes; second hook
  `cost_overrides`, see stress below; `ENGINE_VERSION` unchanged —
  the simulation is untouched): the result carries a plain attribute
  `analysis` (NOT a dataclass field → `to_dict()` and the run card
  unchanged) = `{dates, symbols, signal (ndarray), close_all (the `all`
  close panel), i0, i1, calendar, dollar_volume {symbol: {date: split-basis
  dollar volume}}, crypto}` — input of signal quality and capacity (reused,
  no re-evaluation).
- **`backtest/metrics.py`**: `compute(result, *, analysis=None,
  include_signal=True) -> {calendar, performance, exposure, benchmark,
  costs, signal_quality, capacity, unavailable}`; `summary(metrics) ->
  {total_return, cagr, sharpe, max_drawdown, volatility}` (run lists). All
  formulas in the module docstring. Fractions (0.12 = 12 %); P = 252 NYSE /
  365 crypto-only; Sharpe/alpha vs a ZERO rate (no risk-free series yet);
  hit rate / profit factor BOTH daily (`hit_rate_daily`,
  `profit_factor_daily`) and per trade (user decision 2026-10-04:
  `hit_rate_trades`, `profit_factor_trades`, `avg_trade_pnl`,
  `trades_closed`, `trades_open`; `match_trades(fills)` = FIFO lots per
  symbol, longs and shorts, partial closes / scale-ins / flips handled, P&L
  net of the entry fee and pro-rata exit fees, slippage inside fill prices,
  dividends/borrow/funding not attributed; lots still open at the end
  excluded and counted);
  turnover one-sided per year; costs = fees + liquidation fees + slippage
  (reconstructed per fill from the broker's half-cost-in-price rule) +
  borrow + funding, % of gross P&L only when gross P&L > 0. A metric that
  cannot be computed is null + `unavailable["group.name"] = reason`.
  Signal quality: ≥ 5 symbols → daily cross-sectional Spearman rank IC vs
  forward `all` returns (h = 1/5/10/21/63, only t + h inside the run, ≥ 5
  pairs/day, ≥ 20 days), ICIR = mean/std (daily, not annualised), naive
  t-stat; decay curve = IC vs the 1-day return at lag h; half-life = ln 2 /
  λ from OLS of ln|IC(h)| on h over lags with the sign of IC(1) (≥ 3
  points; λ ≤ 0 → null "does not decay"). < 5 symbols → time-series mode:
  rolling 13-week (65 sessions / 91 UTC days) Spearman correlation of
  signal_t vs the next-day return per symbol, positive share of windows
  (interpretation of "rolling-13-week positive share"). Capacity = capital /
  q90(fill notional ÷ (1 % × 20-session ADV at the decision day)) over
  equity strategy fills (≥ 10); when ≥ 10 % of the orders were already cut
  → null with `at_most = capital`; crypto excluded (no limit, §8.8).
- **Storage v5** table `backtest_runs` (§5.1 rows): columns id (`bt-<12
  hex>`), created/updated/started/finished_at, strategy_id, version,
  content_hash, status, spec_json, request_json `{start, end, benchmark,
  capital, stress}`, seed, trial_number (NULL until stage 6),
  engine_version, range/data/licences/cost_model/metrics/curve/trades/
  warnings/stress JSON, error. Triggers: no DELETE; no UPDATE of a finished
  card; every UPDATE must be one of the status transitions; identity
  columns (id, created_at, strategy_id, version, content_hash, spec_json,
  request_json, seed, trial_number) fixed.
- **`backtest/runcard.py`** (for 3e jobs/API/WPF): `create(strategy_id,
  version=None, request=None, *, seed=None, run_id=None) -> card` (seed
  defaults to `stress.MC_SEED` when `request.stress`), `mark_running(id)`,
  `finish(id, result, metrics, *, stress=None)`, `fail(id, message)`,
  `cancel(id, message=…)`, `get(id) -> card | None` (JSON decoded: spec,
  request, range, data, licences, cost_model, metrics, curve {dates, equity,
  benchmark, gross_exposure, net_exposure, drawdown_pct}, trades, warnings,
  stress), `list_runs(strategy_id=None, *, version=None, status=None,
  limit=50) -> [{…, request, range, key_metrics}]`, `latest_done(strategy_id,
  version)`; `build_payload`. Errors: `NotFound`, `StatusConflict`,
  `ValueError`, `StorageUnavailable`. Licences = `data/licences.licence_for`
  of every source named in the data snapshot; a `survivorship` warning is
  always added (static universe). Curve rounded (cents / 1e-6). Values are
  stored with `allow_nan=False` (a NaN is a bug, surfaced).
- **`backtest/stress.py`**: `run_stress(spec, base_result, *, data=None,
  progress=None, cancel=None, seed=MC_SEED, run_engine=None) -> {seed,
  scenarios: [{key, kind, label, status (done | not_available | failed),
  reason, window, range, metrics, warnings}]}`; also `episode`,
  `synthetic_costs`, `scaled_costs`, `monte_carlo`, `block_length`.
  Episodes run `engine.run(start, end, clamp_warmup=True)` (warm-up loaded
  by the engine; < 2000-01-03 → loading from 2000-01-03 + warning
  `short_warmup` — `clamp_warmup` added 2026-10-09, before the dot-com
  episode failed on the 1999 calendar error); `SecretMissing` / `InvalidRequest`
  / `BacktestDataError` → "not available: <message>"; other data/storage
  errors → `failed` with the message; simulated range missing the episode
  start/end by > 7 days → not available. Costs ×2 / slippage ×3 on the base
  range, EACH asset class with its own rates (user decision 2026-10-04):
  equity fee/slippage = `fees_bps`/2 each, crypto = `crypto_fees_bps`/2
  each (or a custom spec's fee/slippage for both); new per-fill cost per
  class = fee × m_fee + slippage × m_slip, passed as
  `engine.run(cost_overrides={fees_bps, crypto_fees_bps})` — second
  engine hook: per-class overrides written into THAT run's broker params
  after the spec's costs (0..2000 bps, warning `cost_override`, echoed in
  `costs.overrides`; None = normal runs unchanged). Mixed equity+crypto
  strategies are covered; tested: fee and slippage rates exactly ×2 per
  class. Monte-Carlo:
  circular moving-block bootstrap, 1,000 paths, L = max(2, round(n^(1/3))),
  PCG64 seed 20261003 → p5/p50/p95 of max drawdown and CAGR; < 60 returns →
  not available.
- **Lifecycle**: `draft → backtested` allowed when a `done` card of the
  CURRENT version exists; evidence_json `{run_id, version}` (newest card or
  `transition(..., run_id=)`); reason_if_not "needs a finished backtest of
  version N" / "only a draft can become backtested"; higher evidence states:
  "needs evidence from a later stage — stage 3 allows only draft →
  backtested"; `strategy_set_state(expected_version=)` refuses when a new
  version was saved meanwhile. `test_sp_lifecycle.py` assertions updated
  to the new texts.
- Tests: `test_sp_backtest_metrics.py`, `test_sp_backtest_runcard.py`,
  `test_sp_backtest_stress.py`, `test_sp_backtest_review.py`.
- **Review fixes (2026-10-04)**: (1) validator warning `absolute_price_level`
  (`validator.absolute_level_uses`) when a price level — open/high/low/
  close/vwap, directly, via a `ref`, or through level-preserving ops (lag,
  sma/ts_mean, ts_min/max, ewm_mean, add/sub/mul/min/max, div by a non-level,
  delta, ts_std, atr …; a ratio of two levels is scale-free) — is compared by
  gt/ge/lt/le/eq/ne/min/max/clip (also inside a `where` condition) or by a
  `threshold` selection with a NON-ZERO constant or a parameter (0 is
  scale-invariant): the `all` level depends on later dividends/splits and
  pre-seam levels are scaled Tiingo prices. The engine adds the run-level
  `absolute_level_two_sources` (§8.2) when such a spec with equities loads
  data before the seam. No new spec fields; the library stays free of it
  (tested). (2) `fills[].participation_cut` marks orders the engine cut
  (whole-share rounding puts their ratio below 1); `metrics.capacity` counts
  flag OR ratio ≥ 0.999 as "at the limit"; a fill without the flag →
  ValueError; missing capital → null + reason. (3) `seam.volume` returns the
  rows it has when only the newest sessions are missing (warning
  `volume_lagging`, `info.last`); the engine's `end_limited_by` cuts a
  default end there, an explicit later end stays an `InvalidRequest`; no rows
  at all in the range → `DataSourceUnavailable`; while only the newest
  session is missing the Tiingo tail is asked at most once per
  `seam.VOLUME_RECHECK` (1 h) per symbol. (4) No defaults for missing bar
  fields: low/high missing → `BacktestDataError`; a crypto bar without
  `known_at` uses `data.pit.known_at` (the documented rule).


### 8.11 3e notes (implemented 2026-10-04: jobs + API — §8.5, backend part)

- **`backtest/jobs.py`**: `JobManager(run_engine=None, compute_metrics=None,
  run_stress=None, data_factory=None)` (defaults engine.run /
  metrics.compute(result, analysis=result.analysis) / stress.run_stress /
  `engine.DataLoader()`), process-wide `manager()` (`set_manager` for tests).
  `submit(strategy_id, version, request) -> run_id` creates the card
  (`queued`) under the lock and starts ONE daemon worker thread; another
  active job → `JobBusy` (409). Worker: cancelled while queued → `cancel`;
  `mark_running` → engine (phases `data`, `simulate`; the seam check and the
  Tiingo rate-limit wait are messages inside `data`) → `metrics` → `stress`
  (only with `request.stress`; seed = the card's seed) → `saving` →
  `runcard.finish`. Outcomes: `BacktestCancelled` → `runcard.cancel` ("The
  backtest was cancelled."); `UserFacingError` → `fail(user_message)`; any
  other exception → traceback in backend.log, card error "Backtest failed —
  see backend.log". Progress in memory only (lost on restart).
  `recover_interrupted()` (main.py, right after `sp_storage.init` and
  `ensure_configured`) fails every `queued`/`running` card with "Backend
  stopped during the run".
- **Routes** (behind api_auth; validation 400 `{error, errors: {field:
  reason}}`, unknown query parameters 400, no stack traces):
  - `POST /v2/backtests` body `{strategy_id: str, version?: int ≥ 1, start?:
    "YYYY-MM-DD", end?: "YYYY-MM-DD", benchmark?: symbol, capital?: number
    1,000–100,000,000, stress?: bool, history_from?: "2016" | "2000"
    (2026-10-09, §8.5; another value → 400)}`; unknown fields 400 (the
    2026-10-05 field `wait_for_latest` was removed again 2026-10-06, §8.12);
    start ≥
    2000-01-03 and < end; benchmark normalised (upper case). The Tiingo key
    is checked at once (`tiingo.require_key`, no run card) ONLY when the run
    loads equity data (universe or benchmark) before 2016-01-04 — load
    start = explicit start incl. warm-up, else `history_from`,
    `engine.needs_pre_seam_equity(spec, start, benchmark, history_from)`
    (user decision 2026-10-06;
    before: any equity universe, when Tiingo was the volume source) → 503
    `{error, kind: "SecretMissing"}`. Unknown strategy/version → 404. Busy →
    409 `{error: "A backtest is already running.", run_id}`. OK → 202
    `{run_id}`. The stored request is `{start, end, benchmark, capital,
    stress, history_from}` (null = engine default: loading from the seam
    (`history_from` null = "2016"), trading after the warm-up, end = last
    complete session, benchmark = spec/SPY, capital = 100,000; cards from
    before 2026-10-09 have no `history_from` = "2016").
  - `GET /v2/backtests?strategy_id=&limit=` (limit 1–200, default 50) →
    `{runs: [{id, created_at, started_at, finished_at, status, error,
    strategy_id, strategy_name, version, request, range, key_metrics}]}`
    newest first (`key_metrics` = `{total_return, cagr, sharpe,
    max_drawdown, volatility}` or null; `range` as below or null).
  - `GET /v2/backtests/{id}` → 404 `{error: "Backtest not found."}` or 200:
    `id, status (queued|running|done|failed|cancelled), created_at,
    started_at, finished_at, error, strategy_id, strategy_name, version,
    content_hash, engine_version, seed, trial_number, request, spec,
    seam_date ("2016-01-04"), progress, range, key_metrics, metrics, curve,
    trades, warnings, stress, data, licences, cost_model`.
    - `progress` = `{phase (queued|data|simulate|metrics|stress|saving),
      percent (0–100 within the phase), message, updated_at}` while
      queued/running, else null.
    - `range` = `{start, end, calendar (nyse|utc), load_start, first_data,
      end_limited_by: {date, symbol, source} | null, end_explicit,
      warmup_sessions, timing}`.
    - `metrics` = `metrics.compute` output `{calendar, performance,
      exposure, benchmark, costs, signal_quality (incl. ic decay curve),
      capacity, unavailable}` (§8.10).
    - `curve` = parallel arrays `{dates, equity, benchmark, gross_exposure,
      net_exposure, drawdown_pct}` (null entries = valuation paused).
    - `warnings` = `[{code, message, …extra keys (symbol, count, dates, …)}]`
      (always includes `survivorship`).
    - `stress` = `{seed, scenarios: [{key, kind, label, status (done |
      not_available | failed), reason, window, range, metrics, warnings}]}`
      or null when not requested.
    - `data` = per symbol `[{symbol, asset_class, role
      (strategy|benchmark|strategy+benchmark), excluded, prices: {sources,
      ranges: [{source, first, last, rows}]}, volume: {source, first, last,
      rows} | null, dividends (count) | null, seam: [{adjustment (all|split),
      ok, factor, checked_at, stats}]}]` (the raw snapshot with row hashes
      stays in the DB).
    - `licences` = `{source: {licence, note}}`; `cost_model` = `{costs,
      broker_params, benchmark}`; `trades` = runcard trades summary.
    Metrics/curve/data/stress are null (data `[]`) until `done`.
  - `POST /v2/backtests/{id}/cancel` (no body) → active job: 202 `{run_id,
    status: "cancelling"}` (the card becomes `cancelled` within one
    simulated day); a queued/running card without a live job: 200 `{run_id,
    status: "cancelled"}`; finished: 409 `{error: "The backtest is already
    <status>.", status}`; unknown 404.
- `GET /v2/settings/keys` already lists `tiingo-api-key` (3b; tested).
- Tests: `test_sp_backtest_jobs.py` (11), `test_sp_api_backtests.py` (13).

### 8.12 Data refresh, end limit, persistent gate (user decision 2026-10-05, "Beides kombinieren")

> **2026-10-06 (user decision): the background refresh, `GET
> /v2/data/refresh`, the end limit, `wait_for_latest`,
> `end_limited_by_tiingo` and `tiingo_not_published_yet` are REMOVED** —
> they existed only for daily Tiingo volume tails, and Tiingo now serves
> only the years before the seam (§8.1 source update, §8.8). Renamed, not
> deleted: `data/refresh.py.stale`, `tests/test_sp_data_refresh.py.stale`,
> `tests/test_sp_data_wait_latest.py.stale`, `tests/test_sp_data_volume.py.stale`.
> KEPT: the persistent gate counters (below), `storage.db.meta_keys`,
> `registry.fetch` passing cancellations through (`is_cancellation`, no
> source error status). `range.end_limited_by.provider` stays. New:
> engine run warning `many_late_starts` (> `LATE_START_SHARE` 25 % of the
> universe with no data until > `LATE_START_DAYS` 365 days after the load
> start — a data problem like the IEX one cannot hide again; per symbol
> `short_history` as before, now also for a late SIP start). Tests:
> `test_sp_data_sip.py` (15). The text below is history.

Problem: ~60 equity ETFs per universe and a 40/h Tiingo gate → the first
backtest of every new trading day waited > 1 h for 60 volume tails.

- **Background refresh** (`data/refresh.py`, `RefreshJob`, daemon thread
  started by main.py via `refresh.start()`; status `GET /v2/data/refresh` →
  `{refresh: {running, state (not_started | idle | refreshing | paused |
  waiting_for_publication | waiting_for_rate_limit | done | error), message,
  target_session, symbols, done, pending, last_pass_at, next_retry_at,
  errors [{symbol, message, at}] (last 20), tiingo_requests_left {hour,
  day}}}`, never data). Every 60 s: target = newest NYSE session whose
  20:05 New York has passed (Tiingo: "Most US Equity prices are available at
  5:30 PM EST, however exchanges may send corrections until 8 PM EST",
  https://www.tiingo.com/documentation/end-of-day). Symbols = equities with
  cached volume (`seam.volume_symbols()`, = already used); per symbol
  `seam.refresh_volume_tail(sym, target)` (the normal tail rules incl. the
  split → full refetch) and the Alpaca `all`/`split` tails into the price
  cache. Once per symbol per session (in-process memory + the stored
  `through`); failures retried after 30 min, at most 3 per session; a tail
  without the target row (not published) backs the whole job off 30 min
  (one probe request, not 60). Skips while a backtest is queued/running
  (`jobs.manager().active_run_id()`), resumes after; leaves 5 hourly Tiingo
  slots free and never waits for the gate. Only while the backend runs.
- **End limit** — request field `wait_for_latest` (bool, default true; user
  decision 2026-10-05): `jobs` builds `engine.DataLoader(wait_for_latest=…)`
  (a `data_factory` loader gets the attribute set), which passes
  `newest_optional = (end is None)` and `wait_for_latest` to `seam.volume`.
  When only the newest completed session is missing:
  - **case A** (Tiingo has it; the gate may have no free slot): `true` →
    the tail IS requested, waiting for the gate like first-time loading
    (progress "waiting for Tiingo rate limit (…)", cancellable — the
    engine's progress callback raises `BacktestCancelled`, which
    `registry.fetch` passes through without marking the source as failed:
    `is_cancellation`); the run reaches the newest session. `false` →
    accepted FALLBACK (§6): NO request, rows end at the stored day,
    `info.end_limited_by = {source: "tiingo", data_type: "equity_volume",
    symbol, date, missing, reason: "rate_limit_no_wait"}`, warning
    `end_limited_by_tiingo`.
  - **case B** (a tail request returned no row for the newest session —
    not published yet): both modes end at the last published session,
    `end_limited_by.reason = "not_published"`, warning
    `tiingo_not_published_yet` "Tiingo has not published <date> yet
    (usually final after 20:00 New York / 02:00 Berlin) — the backtest ends
    on <last date>." Detected by ONE request (the first symbol's tail in
    wait mode, or the background refresh's probe via
    `refresh_volume_tail`) and remembered per session for
    `seam.NOT_PUBLISHED_RECHECK` (10 min, in memory) — meanwhile no other
    symbol's tail is requested for it. `false` without a known probe cannot
    tell A from B and reports A's warning.
  The engine aggregates each code into ONE run warning (`symbols`,
  `missing`). The engine's existing min-over-sources end then
  stops there; `range.end_limited_by` gained `provider` ("tiingo" |
  "alpaca"). Older missing rows → normal fetch (first loading); explicit end
  beyond → `InvalidRequest` as before (explicit ends never use the
  shortcut; they keep the 3d `volume_lagging` behaviour).
- **Persistent gate counters**: `http_gate.PersistedStamps(key)` — the
  process gate of every windowed source (Tiingo) keeps its request times
  (UTC epoch, JSON list) in hedgefund.db meta `http_gate/tiingo/requests`,
  loaded on first use (wall clock → monotonic; future stamps count as now),
  saved after every acquisition. A load/save failure raises
  `StorageUnavailable` and the request is not sent (no silent reset).
  Closes the "restart starts the windows empty" gap of §8.7.
- `storage.db.meta_keys(prefix)`; `Seam.volume_through(symbol)`,
  `Seam.refresh_volume_tail(symbol, target)`, `Seam.volume_symbols()`.
- Tests: `test_sp_data_refresh.py` (14), `test_sp_data_wait_latest.py` (10).

## 9. Stage 4 — validation gates (contract, 2026-10-09)

Interview decisions (user, 2026-10-06/09): gates are **hard with a documented
exception**; thresholds preset **Standard** (Lenient/Strict selectable,
values editable in settings); two steps in the UI — **"Validate…"** (all
gates, no holdout, repeatable) → `candidate`, then **"Open holdout (once)"**
with typed confirmation → `validated`; the holdout may be opened **once per
strategy family** (strategy + all copies/duplicates/versions); **every
finished backtest/validation run of any family member counts as a trial**;
normal backtests end before the holdout window; families that already saw
the window are **contaminated**; a family that FAILS the holdout is
**locked** until ≥ 12 new unseen months exist, then may be re-tested ONCE on
exactly those new months (same rule as contaminated); the report shows how
many other families already opened the holdout window of the same universe.

### 9.1 Families, "seen until", holdout window

- **Family** = a root strategy and every strategy created from it by
  duplicate/import-of-export (follow `parent` links in envelopes; store
  `family_id` on `strategies`, migration computes it).
- **Holdout window** (global, rolling): from `holdout_start` to the last
  complete session; `holdout_start` = last complete session − 24 months for
  universes with equities (also mixed), − 12 months for crypto-only.
  Constants in one place.
- **seen_until(family)** = max simulated end date over all run cards of all
  family members (backtests, validation runs, holdout runs) — persisted per
  family, migration computes it from existing run cards.
- **Normal backtests and validation runs** must end before `holdout_start`:
  default end = `holdout_start − 1 session`; an explicit end inside the
  window → 400 "This end date lies inside the locked holdout window
  (from <date>). The holdout is opened once per strategy family via 'Open
  holdout'." Stress episodes inside the window are skipped with "not
  available: inside the locked holdout window".
- **Contaminated:** seen_until(family) ≥ holdout_start (e.g. today's SMA
  50/200 and the Bollinger copy family). Shown as a chip.
- **Holdout eligibility:** unseen window = (max(seen_until, holdout_start−1),
  last session]; fresh family → the full window (24/12 months); contaminated
  or holdout-failed family → eligible once the unseen window is ≥ 12
  months, and the test uses ONLY the unseen window. Not eligible → the
  button is disabled with the exact date when it becomes eligible.
- Opening the holdout sets seen_until = last session (whatever the result)
  and writes an immutable `holdout_events` row (family, strategy, version,
  window, result, run id, ts). Universe counter = number of holdout events
  whose window overlaps the current one, by universe preset/symbol set.

### 9.2 Trials

- trial_count(family) = number of finished run cards (backtest, validation,
  holdout) of all family members; `trial_number` on run cards is now set.
- Deflated Sharpe uses the family trial count and the variance of the
  annualised Sharpe ratios of those trials (Bailey & López de Prado 2014);
  with 1 trial, variance term uses the documented minimum.

### 9.3 Validation run (`validation/`)

One job (same single-job runner as backtests, same 409 rule):
1. Backtest on the training range (default load per `history_from`, end =
   holdout_start − 1).
2. **Walk-forward folds:** K = 5 consecutive, equal-length out-of-sample
   segments of the training range (the spec is fixed — no refit); per-fold
   metrics.
3. **Parameter grid** from the spec's `parameters` (min/max/step; ≤ 64
   configurations, evenly subsampled if more) → one backtest per
   configuration (needed for PBO and robustness).
4. Stress: costs ×2 and slippage ×3 (per class), historical episodes
   outside the holdout window.
5. Gates (below) → validation report (run card kind `validation`,
   insert-only).

### 9.4 Gates (Standard thresholds; Lenient / Strict in parentheses)

| # | Gate | Pass rule |
|---|---|---|
| G1 | Consistency | cross-sectional: daily rank-IC t-stat ≥ 2.0 (1.5 / 3.0) at the best of horizons 1/5/10/21 AND mean IC > 0 in ≥ 4 of 5 folds (3 / 5); time-series (single symbol): Sharpe > 0 in ≥ 4 of 5 folds |
| G2 | Alpha | alpha vs the run's benchmark, Newey-West t-stat ≥ 2.0 (1.5 / 3.0) and alpha > 0 |
| G3 | Signal decay | IC half-life ≥ average holding period of closed trades (time-series strategies: skipped as "not applicable — single-symbol strategy", counts as pass with that note) |
| G4 | Deflated Sharpe | probability ≥ 0.95 (0.90 / 0.99) |
| G5 | Overfitting (PBO, CSCV with S = 10 blocks over the parameter grid) | PBO ≤ 0.30 (0.50 / 0.20); needs ≥ 4 configurations — fewer → FAIL "needs ≥ 4 parameter configurations (add min/max/step to parameters)" |
| G6 | Robustness | every grid neighbour within ±20 % of each parameter keeps ≥ 50 % of the base Sharpe (40 % / 60 %); costs ×2 net Sharpe > 0 (×2 / ×3); each available stress episode: max drawdown ≤ 1.5 × the benchmark's in that episode |
| G7 | Sample size | ≥ 100 closed trades (50 / 200) |
| G8 | Capacity | capacity estimate ≥ 10 × capital |
| H | Holdout (final, separate step) | on the holdout (or unseen) window: net Sharpe > 0 AND ≥ 50 % of the training Sharpe AND mean IC > 0 (time-series: Sharpe only); **no exception possible** |

Every gate result: `{gate, status: pass|fail|exception, value, threshold,
explanation, details}`. A metric that cannot be computed → `fail` with the
reason (never a default pass), except the explicitly listed G3 case.
- **Exception:** a failed G1–G8 may be overridden by the user with a written
  reason (≥ 30 characters) → status `exception`, audit row, shown in the
  report and on the strategy card; the holdout gate H can never be
  overridden. Exceptions belong to the validation run (a new run needs new
  exceptions).
- `candidate` requires all G1–G8 pass/exception in the latest validation run
  of the CURRENT version; `validated` requires H pass for that version.
- Settings: `GET/PUT /v2/settings/gates` {preset, overrides} — audited.

### 9.5 Lifecycle + API

- Transitions: `backtested → candidate` via a passing validation run
  (evidence run id); `candidate → validated` via a passing holdout run;
  holdout fail → family flag `holdout_failed_at`, strategy stays
  `candidate` but cannot move to validated/incubation; contaminated or
  locked families show the eligibility date. Existing rules for higher
  states unchanged (incubation etc. later stages).
- `POST /v2/validations` {strategy_id, version?, history_from?} → 202
  {run_id}; `GET /v2/validations/{id}` (progress + report);
  `GET /v2/validations?strategy_id=`;
  `POST /v2/validations/{id}/exceptions` {gate, reason} → updated report;
  `GET /v2/strategies/{id}/holdout` → {eligible, window, unseen_from,
  eligible_from, family_events, universe_events};
  `POST /v2/strategies/{id}/holdout` {confirm: "<exact strategy name>"} →
  202 {run_id}; wrong confirmation → 400.
- All behind api_auth, strict validation, no stack traces.

### 9.6 Calibration (acceptance)

Synthetic data with a planted, persistent cross-sectional signal → a
strategy using it passes G1–G8 (Standard) and H; pure-noise strategies fail
(≥ 95 % of 40 seeds fail at least one gate); an overfitted grid (best of 64
noise configs) fails G4 or G5. Library alphas: the report runs end-to-end on
synthetic data (no requirement to pass).

### 9.7 WPF

- Library detail: buttons **Validate…** (dialog: history_from) and **Open
  holdout (once)…** (disabled + tooltip with reason/eligibility date when
  not eligible; dialog explains "once per family", shows the window and the
  universe counter, requires typing the exact strategy name).
- `Platform/ValidationReportView`: gate table (status icon, value,
  threshold, plain-language explanation), fold chart, parameter-grid heat
  table, PBO/deflated Sharpe section, stress table, exceptions (button
  "Add exception…" per failed G1–G8 with reason box), holdout section.
- Chips on strategy cards: `contaminated`, `holdout failed (until …)`,
  `exception`; trial count in the detail pane.
- Backtest dialog: default end now holdout_start − 1 (shown), date picker
  refuses dates inside the window with the backend message.

### 9.8 V1 notes (implemented 2026-10-09: families, holdout lock, trials, lifecycle — §9.1, §9.2, §9.5 lifecycle)

- **Storage v6** (one transaction, like v3): `strategies.family_id`
  (+ index); `strategy_families(family_id PK, seen_until YYYY-MM-DD,
  holdout_failed_at, created_at, updated_at)`; `holdout_events(id, ts,
  family_id, strategy_id, version, window_start, window_end, universe_kind,
  universe_key, result pass|fail, run_id)` insert-only (triggers);
  `validation_exceptions(id, ts, run_id, gate G1–G8, reason, actor,
  UNIQUE(run_id, gate))` insert-only (for V2, §9.4);
  `backtest_runs.kind` (`backtest` | `validation` | `holdout`, existing
  cards `backtest`). `trial_number` may now be written exactly once:
  NULL → n together with running → done. The identity trigger was recreated
  for that, and it also fixes `kind`. The migration data step
  (`_v6_families`) computes family_id from the first version's envelope
  (`db.family_parent_of`: `parent` naming ANOTHER strategy = a duplicate; an
  import whose `claimed.exported_from` {strategy_id, version,
  content_hash} matches an existing version's hash exactly; unverifiable
  claims never join a family). It also computes seen_until = max `range.end`
  of the family's done cards and backfills `trial_number` of the existing
  done cards (order finished_at, id within the family). Run lists order by
  `created_at DESC, rowid DESC`, so "latest" is well defined within one second.
- **db API (v6)**: `family_parent_of`, `backtest_run_finish(run_id, fields, *,
  seen_until, audit) -> trial | None` (BEGIN IMMEDIATE: count, update, raise
  seen_until), `family_get`, `family_record_seen`, `family_strategy_ids`,
  `family_trial_count`, `family_done_runs`, `holdout_event_insert(event, *,
  audit)` (also sets seen_until = max(…, window_end) and holdout_failed_at =
  ts on fail / NULL on pass), `holdout_events_list(family_id=None, *,
  universe_key=None)`, `validation_exception_add(run_id, gate, reason, *,
  actor="user", audit=None) -> id` (gate G1–G8, reason 30..2000 chars, the
  card must be a done `validation` card, one per gate → else ValueError),
  `validation_exceptions_list(run_id)`, `backtest_runs_list(…, kind=)`.
- **`validation/families.py`** (public API for V2, names fixed):
  `family_of(strategy_id)`, `holdout_window(universe_kind, last_session=None)
  -> {start, end}` (dates), `seen_until(family_id) -> date | None`,
  `record_seen(family_id, end)`, `is_contaminated(family_id, window)`,
  `holdout_eligibility(strategy_id, now=None)` (ISO strings: {eligible,
  reason, window, unseen_from, eligible_from, family_events,
  universe_events, mode full|unseen, contaminated, holdout_failed_at,
  seen_until, universe_kind, holdout_start}), `record_holdout_event(
  strategy_id, version, window, result, run_id) -> id`, `trial_count`,
  `family_trial_sharpes` (`metrics_json.performance.sharpe` of the done
  cards; null values are left out), `training_end(spec, last_session=None)`,
  `HoldoutWindowViolation(InvalidRequest)` (→ 400; message per §9.1, "This
  start date …" for a start). Helpers: `universe_kind(spec)`,
  `universe_key(spec)` (`preset:<name>:<classes>` | `symbols:<sha256[:16]>`),
  `last_session(kind, now)`, `lock_for_kind(kind, now)`,
  `check_request_dates(spec, start, end)`, `family_chips(row, spec)`,
  `add_months`, `set_clock` (tests). Constants `EQUITY_HOLDOUT_MONTHS` 24,
  `CRYPTO_HOLDOUT_MONTHS` 12, `REOPEN_UNSEEN_MONTHS` 12.
- **Window rules (choices)**: last complete session = NY date of
  `calendars.last_close_before(now)` (equity/mixed) or UTC yesterday
  (crypto). start = the first session on/after last − N months, with the day
  clamped to the month end. Training end = the session before start.
  Eligibility as §9.1. `eligible_from` = the first session on/after anchor +
  12 months (anchor = max(seen_until, training end)): the family becomes
  eligible once that session is complete. `universe_events` = the number of
  OTHER families with a holdout event on the same `universe_key` whose
  window overlaps the current full window. A passed (re-)test clears
  `holdout_failed_at`; the events keep the history. An eligibility date
  beyond the NYSE calendar (2028+) raises `CalendarUnavailable`; no date is
  guessed.
- **Engine** (`engine.run(…, holdout_access=False)`): every run is locked.
  end None → load and simulate up to the training end; when the lock ends
  the run, `range.end_limited_by` is null. An explicit start/end on/after
  the window start → `HoldoutWindowViolation`. `range.holdout = {
  universe_kind, start, end, training_end, access}`. The universe kind comes
  from the strategy symbols; the benchmark does not count.
  `holdout_access=True` is ONLY for V2's holdout job.
- **Run cards**: `runcard.create(…, kind="backtest")`. A `backtest` or
  `validation` card with an explicit request start/end inside the window →
  `HoldoutWindowViolation` before anything is stored. Through
  `jobs.submit`, that reaches the route synchronously as 400. `holdout` cards
  get no date check. `finish` → `db.backtest_run_finish` (trial_number =
  family trial count at finish, seen_until raised to `range.end`; a result
  without `range.end` → ValueError). `list_runs(…, kind=)` and
  `latest_done(…, kind=)`; list rows add `kind` and `trial_number`.
- **Stress**: `run_stress(…, holdout_access=False)`. An episode whose end is
  on/after the window start (from `range.holdout.start` of the base run, else
  computed: `holdout_start_for`) → `not_available` "not available: inside
  the locked holdout window". Synthetic cost re-runs pass `holdout_access`
  only for a holdout base run.
- **Lifecycle — JSON keys read**: `backtested → candidate` takes the LATEST
  done `kind = 'validation'` card of the current version (an explicit
  `run_id` must be that card) and calls `validation.report.effective_passed(
  card) is True`. This is V2's function: the raw
  `metrics_json.validation.passed` merged with `validation_exceptions`;
  without the module the check raises, no stand-in. `candidate → validated`
  takes the latest done `kind = 'holdout'` card of the current version and
  requires `metrics_json.holdout.passed is True`. A family with
  `holdout_failed_at` set → `validated`/`incubation` refused ("the strategy
  family failed the holdout on D — locked until E (≥ 12 new unseen months),
  then one re-test on those months only"). Without a holdout card, the reason
  includes the eligibility ("… — the holdout can be opened now (once per
  strategy family)" / "… from DATE: <reason>"). `evidence_json` = {run_id,
  version, kind}; draft → backtested is unchanged ({run_id, version}).
  `registry.get()` rows and `registry.family_chips(row, spec=None)` carry
  {family_id, contaminated, holdout_failed_until, trial_count}.
- **Review fixes (2026-10-09)**: (1) *Family bypass by copy-paste.* Storage
  v7 adds the insert-only table `strategy_logic(strategy_id, version,
  logic_hash, PK(strategy_id, version))`, indexed by `logic_hash` and
  backfilled from the stored specs. `strategy_versions` keeps its columns.
  `db.logic_hash_of(spec)` = sha256 of the canonical stored spec without name,
  description, category, tags, provenance and the parameters' descriptions.
  `strategy_create` and `strategy_add_version` write the row in the same
  transaction. A NEW strategy without a parent link joins the family of the
  oldest strategy having a version with the same logic hash. This covers a
  spec text pasted as a new strategy and an import of an export with its
  provenance stripped (`registry._family_for`). A strategy EDITED into a twin
  keeps its family. Its eligibility, chips and lifecycle lock use
  `families.family_state(strategy_id)`: {family_id, related = own family +
  every family owning a version with the current logic hash, seen_until =
  max, holdout_failed_at = newest}; `family_events` counts the related
  families' events. Limit: a spec that differs in ANY value (one parameter
  default, max_weight …) has another logic hash and gets its own family. The
  `universe_events` counter in the report covers such near-duplicates. db
  API: `logic_hash_of`, `logic_hash_get(strategy_id, version)`,
  `families_with_logic_hash(logic_hash)`.
  (2) *Atomic holdout result.* `runcard.finish_holdout(run_id, result,
  metrics, *, family_event={"window": {"start", "end"}, "result":
  "pass"|"fail"}, stress=None) -> card + holdout_event_id` (the card must be
  kind `holdout` and `running`). It finishes the card (trial_number,
  seen_until) AND writes the holdout event (seen_until = window end,
  holdout_failed_at) in ONE transaction via `db.backtest_run_finish(…,
  holdout_event=)`; a failure in between writes neither. Audit action
  `holdout_finished` carries `holdout_event_id`. Helper
  `families.holdout_event_row(strategy_id, version, window, result, run_id)`.
  `families.record_holdout_event` stays for tests and tools; the holdout job
  must use `finish_holdout` instead of `record_holdout_event` + `finish`.
- Tests: `test_sp_families.py` (20), `test_sp_families_lifecycle.py` (16);
  `test_sp_api_strategies.py` transition text updated to stage 4.

### 9.9 V2 notes (implemented 2026-10-09: statistics, folds, grid, gates, jobs, report, API — §9.2–§9.6)

- **Files**: `validation/stats.py` (DSR, PBO/CSCV, Newey-West, normal
  CDF/inverse — no scipy), `folds.py`, `grid.py`, `gates.py`, `report.py`,
  `jobs.py`; formulas in each module docstring. Tests:
  `test_sp_validation_stats.py` (12), `test_sp_validation_gates.py` (18),
  `test_sp_validation_calibration.py` (4), `test_sp_validation_jobs.py` (4,
  real engine), `test_sp_api_validation.py` (6); fakes in
  `test_sp_validation_support.py`. `test_sp_api_strategies.py` /
  `test_sp_api_backtests.py` key-set assertions extended (chips, `kind`).
- **Statistics (choices)**: DSR = PSR(SR0) with per-period SR, skew and
  (non-excess) kurtosis; SR0 = expected max Sharpe of N trials, N = the
  family's finished trials + 1 (the running validation run); trial variance
  = sample variance of the trials' annualised Sharpes floored at V_min =
  1 / years of the training sample (Lo 2002 null variance; re-runs have no
  dispersion). PBO: S = 10 contiguous blocks (array_split), all 252
  combinations, IS winner's OOS average rank / (N + 1), PBO = share of
  logits ≤ 0; needs ≥ 4 configurations and ≥ 50 common days. Newey-West
  lag floor(4 (T/100)^(2/9)); G1's IC t-stat uses lag max(rule, h − 1)
  (overlapping h-day returns — stricter than the run card's naive t).
- **Folds**: K = 5 equal segments of the training run's simulated days;
  per-fold Sharpe, total return, max drawdown, mean IC at the best horizon
  counting only days whose forward window ends inside the fold.
- **Grid**: used parameters with min/max/step (plus their value), integer
  rounding, Cartesian product, > 64 → indices round(i (n−1)/63) with the
  base configuration forced in; parameters without `step` or flagged
  `unused_param` stay fixed. Neighbours: every varied parameter within ±20 %
  of its base value (base 0 → 20 % of max − min). A failed grid run → PBO
  not computed (G5 fail) / failed neighbour (G6 fail) — never a partial grid.
- **Gates**: exactly §9.4; mode `time_series` = fewer than 5 symbols (the
  metrics split) → G1 fold-Sharpe rule, G3 "Not applicable — single-symbol
  strategy …" (pass), H Sharpe only. G3: an IC that does not decay (fit
  λ ≤ 0) passes (unbounded half-life); holding = mean sessions entry→exit of
  FIFO closed lots. G6 (c) with no available episode holds vacuously (noted
  in `details.notes`). G3's "no decay" pass additionally needs a
  significantly positive 1-day IC (mean > 0, NW t ≥ `g1_ic_t`), else fail
  (review: flat noise ICs). G8 (user decision 2026-10-09): crypto-only →
  "Not applicable — No capacity measurement for crypto (user decision
  2026-10-03: no volume limit for crypto)" (pass, `details.not_applicable`);
  mixed universes → the estimate from the equity orders only
  (`metrics.capacity` skips crypto fills); no estimate otherwise (limit
  binding, too few orders) → fail. `passed` requires all of G1–G8 present
  and pass/exception (a missing gate → not passed). H: Sharpe > 0, ≥ 50 % of the training Sharpe (of the validation
  run used), mean IC > 0 at the validation's best horizon; constants, no
  override, no exception.
- **Settings**: `GET /v2/settings/gates` → `{preset, overrides, effective,
  updated_at, presets: {lenient|standard|strict: {key: value}}, ranges: {key:
  {min, max, integer}}}`; `PUT {preset, overrides?}` → same (400 `{error,
  errors: {field|"overrides.<key>": reason}}`). Keys: `g1_ic_t`,
  `g1_folds_min`, `g2_alpha_t`, `g4_dsr_min`, `g5_pbo_max`,
  `g6_neighbour_ratio`, `g6_cost_multiplier`, `g6_episode_dd_ratio`,
  `g7_min_trades`, `g8_capacity_multiple`. Stored in meta
  `validation/gate_settings`, audit `gate_settings_changed` {old, new}; none
  stored = `standard` (user default); unreadable → 503.
- **Jobs** on `backtest.jobs.manager()` through its public
  `JobManager.submit_work(create_card, work, *, label, generic_msg,
  cancelled_msg)` (review fix 2026-10-09; `submit` is now a thin caller —
  same outcome handling: queued-cancel, mark_running, BacktestCancelled →
  cancel, UserFacingError → fail(message), else traceback + generic
  message). `create_card` runs under the manager lock: the holdout job
  re-checks eligibility + the validation run there (no double open). Busy → `JobBusy` (409 "A backtest or
  validation run is already running." from the validation routes; POST
  /v2/backtests keeps its message). Progress phases: `data`, `simulate`,
  `metrics`, `folds`, `grid`, `stress`, `gates`, `saving`. Cancel via POST
  /v2/backtests/{id}/cancel or /v2/validations/{id}/cancel. Validation card:
  kind `validation`, request `{history_from, stress: true}` (others null),
  `metrics_json` = training `metrics.compute` output + `validation: {passed
  (RAW), version "sp-validation/4.1", report}`; `stress_json` = run_stress
  output (+ `costs_x3` when the G6 multiplier is 3). Holdout card: kind
  `holdout`, request {start, end = eligible window, benchmark/capital of the
  validation run}, `metrics_json.holdout = {passed, version, report}`;
  card and holdout event are written together by `runcard.finish_holdout`
  (one transaction, V1 review fix); a run that fails/cancels before its
  result records no event. Opening needs an
  eligible family AND a passing (with exceptions) latest done validation run
  of the current version (409 otherwise).
- **Report** (`report`, also `GET /v2/validations/{id}.report`): `{version,
  passed, passed_raw?, mode (cross_sectional|time_series|null), gates:
  [{gate, name, status (pass|fail|exception), value, threshold, explanation,
  details, exception?: {id, ts, reason, actor}}], folds: [{fold, start, end,
  days, returns, sharpe, total_return, max_drawdown, mean_ic, ic_days,
  horizon, reason}], grid: {axes, fixed, total, subsampled, max_configs,
  base_index, base, neighbours, configs: [{index, params, sharpe, reason,
  base}], sharpes, reasons, base_sharpe}, pbo: {value, reason, configs, rows,
  blocks, combinations, logits, overfit_combinations, mean_logit}, dsr:
  {value, reason, sharpe_annual, sr0_annual, n_trials, trial_variance,
  variance_source, trials_used, skew, kurtosis, returns}, stress, trial_count,
  thresholds: {preset, overrides, effective}, training: {range, sharpe,
  sharpe_reason, returns, periods_per_year, benchmark, alpha, holding}, ic:
  {best_horizon, horizons: [{horizon, value (NW t), mean, se, lag, n,
  reason}]}, holdout (eligibility snapshot), exceptions: [{id, ts, gate,
  reason, actor}]}`. Holdout report: `{version, passed, mode, gates: [H],
  window, range, validation_run_id, training_sharpe, eligibility, metrics}`.
  Exceptions: `db.validation_exception_add` (V1 table), audit
  `validation_exception` {run_id, gate, reason}; merged on read;
  `report.effective_passed(card)` is what the lifecycle calls.
- **Routes** (behind api_auth; 400 `{error, errors}`; unknown query keys 400):
  - `POST /v2/validations {strategy_id, version?, history_from?: "2016"|"2000"}`
    → 202 `{run_id}`; 404 `{error}`; 409 `{error, run_id}`; Tiingo key rule as
    POST /v2/backtests (503 SecretMissing).
  - `GET /v2/validations?strategy_id=&limit=` (1–200, default 50) → `{runs:
    [{id, kind (validation|holdout), created_at, started_at, finished_at,
    status, error, strategy_id, strategy_name, version, trial_number, request,
    range, key_metrics, passed (merged; null until done), exceptions (count;
    0 for holdout)}]}` newest first.
  - `GET /v2/validations/{id}` (validation AND holdout cards) → `{id, kind,
    status, created_at, started_at, finished_at, error, strategy_id,
    strategy_name, version, content_hash, trial_number, request, range,
    progress ({phase, percent, message, updated_at} while active, else null),
    key_metrics, passed, report (merged; null until done), warnings}`; 404
    `{error: "Validation run not found."}` (also for plain backtests).
  - `POST /v2/validations/{id}/exceptions {gate, reason}` → 200 = the detail
    above; 400 (reason < 30 chars, gate H, unknown gate, gate that did not
    fail) `{error, errors: {gate|reason: msg}}`; 404; 409 `{error}` (already
    excepted / run not done).
  - `POST /v2/validations/{id}/cancel` = backtest cancel semantics.
  - `GET /v2/strategies/{id}/holdout` → V1's `holdout_eligibility` keys +
    `{version, training_end, validation_run_id, validation_passed, can_open,
    blocked_reason}`.
  - `POST /v2/strategies/{id}/holdout {confirm}` → 202 `{run_id}`; wrong name
    → 400 `{error: "The confirmation does not match the strategy name.",
    errors: {confirm}}`; not eligible / no passing validation → 409 `{error,
    eligible_from}`; busy 409; 404.
  - `POST /v2/backtests` with start/end inside the window → 400 `{error,
    kind: "HoldoutWindowViolation", errors: {start|end: msg}, holdout_start}`.
  - `POST /v2/strategies/{id}/preview`: `days` 1..5, default 5 (was 1..250,
    default 20; review fix 2026-10-09) — the held-weight history reached
    into the locked holdout window without counting as a trial or raising
    seen_until (250 days of weights + public prices ≈ half the window's
    performance); now only the current signal row + at most the last 5
    sessions. `strategy.preview` keeps its own limit for internal callers.
  - `GET /v2/validations` reads list columns only (no full cards).
  - Strategy list rows and detail `strategy` add `family_id, contaminated,
    holdout_failed_until, trial_count` (V1 chips) and `has_exception` (latest
    done validation run of the current version has ≥ 1 exception); backtest
    list rows and detail add `kind`.
- **Calibration** (§9.6, stand-in engine, 10 symbols 2020–2024): planted
  AR(1) factor (φ 0.98) passes G1–G8 + H; of 40 noise seeds ≥ 95 % fail ≥ 1 gate
  (asserted); best-of-64 noise (64 trials) fails G4 or G5 on 3 seeds.
  A library entry's grid + report runs end-to-end; the real-engine pipeline
  (6 symbols, 4 configs) runs in `test_sp_validation_jobs.py` (~10 s).

## 10. Robustness: parallel loading, parallel compute, resumable runs (contract, 2026-10-09)

Reason (user, 2026-10-09): on a weak Wi-Fi connection validations failed
with Alpaca timeouts; the program must cope with that. Interview decisions:
data loading **adaptive concurrency** (toggle; off → fixed number 1–8);
validation compute on **all CPU cores but one**; failed runs can be
**resumed** (button) and optionally **auto-resumed** after a connection
error (no time limit — weak machines must not be punished — but at most
**5 automatic resumes** per run); a resumed run counts as **one trial**.
Security measures accepted: compute workers get no network and no keys;
resume only on an identical basis; checkpoints tamper-evident; auto-resume
bounded (5 attempts, connectivity probe at most every 30 s, visible,
cancellable).

### 10.1 Network settings + adaptive loading (owner R1)

- `data/network.py`: settings stored in hedgefund.db meta
  (`network.adaptive: bool` default true, `network.fixed: int` 1–8 default
  4); `get_settings() -> {adaptive, fixed, current, min, max}`;
  `set_settings(adaptive, fixed)` (validated, audited by the caller).
- **Adaptive limiter** per source (alpaca, tiingo): concurrency starts at 4
  (max 8, min 1); a timeout/connection error halves it (floor 1); 20
  consecutive successes raise it by 1 (AIMD). Fixed mode = constant. It
  sits IN FRONT of the existing http_gate (rate limits unchanged:
  Alpaca 180/min, Tiingo 40/h — concurrency never bypasses them).
- **Parallel fetching:** the backtest/validation data loader fetches the
  symbols of a run concurrently through a thread pool bounded by the
  limiter (bars all/split, volume, corporate actions). Results identical
  to sequential (deterministic assembly order).
- **Chunking:** corporate actions per request ≤ 10 symbols and ≤ 3 years
  (configurable constants); bars as today. Request timeout stays 20 s; the
  retry policy of the gate stays (bounded).
- A connection-type failure raises `DataSourceUnavailable` with
  `kind="connection"` in its detail (timeout, DNS, unreachable, TLS
  handshake) so jobs can tell connection errors from data/auth errors.
- Connectivity probe `probe(source) -> bool`: one tiny request (Alpaca:
  latest SPY trade; Tiingo: not probed — only pre-2016 loads, use the next
  real request), never more often than every 30 s per source.

### 10.1.1 R1 notes (implemented 2026-10-09: network settings, limiter, parallel loading, chunking, error kind, probe)

- **`data/network.py`**: `get_settings() -> {adaptive, fixed, current: {alpaca,
  tiingo}, min: 1, max: 8}` (reads meta every call; `StorageUnavailable` when
  the DB cannot be read); `set_settings(adaptive, fixed=None)` (None keeps the
  stored `fixed`; refused when that is invalid) — `adaptive` must be
  a bool, `fixed` an int 1–8 (bools/floats/strings refused) →
  `InvalidNetworkSettings` (subclass of BOTH `InvalidRequest` and
  `ValueError`); stores meta `network.adaptive` ("true"/"false") and
  `network.fixed` ("1".."8"), applies at once, returns the get_settings
  shape; auditing is the caller's job. Switching to adaptive restarts every
  limiter at 4; fixed mode sets the limit to `fixed`. Missing keys = the
  defaults (true / 4). Before hedgefund.db is open (tests, start-up) the
  limiters use the defaults and load the stored settings at the first
  request after it is open (speed only — never data/source/results).
- **Invalid stored value** (review fix 2026-10-10, e.g. meta
  `network.adaptive = "yes"`): every Alpaca/Tiingo request (live paper feed
  included) and `pool_size()` raise `StoredSettingsInvalid` "The stored
  network settings are invalid — save them again in Hedge Fund → Keys & data
  → Network." — no default is used instead (no-fallback rule). GET does NOT
  raise: the invalid field is `null` and three fields are ADDED (only in this
  state, so the normal shape stays `{adaptive, fixed, current, min, max}`):
  `stored_invalid: true`, `error` (that message), `stored`
  `{"network.adaptive", "network.fixed"}` = the raw stored values (printable,
  ≤ 40 chars; no secrets live there). PUT repairs it: `set_settings`
  validates only the NEW input and stores it; the route's audit `old` is
  `{adaptive, fixed, stored_invalid: true, stored}` in that case.
- **AIMD** (`AdaptiveLimiter`, `limiter_for(source)`, sources `alpaca`,
  `tiingo`; `alpaca_sip` uses the alpaca gate and limiter): per request
  attempt — success → +1 to the streak, 20 in a row → limit +1 (max 8);
  connection-type failure → limit // 2 (floor 1), streak 0; other failures
  (HTTP 4xx/5xx/429, auth, bad data, SecretMissing) change nothing. A lowered
  limit lets running requests finish; new ones wait until in-flight < limit.
- **Gate integration** (`http_gate.HttpGate(concurrency=…)`; `gate_for`
  attaches `network.limiter_for` for alpaca/tiingo): per attempt slot →
  window limiter → token bucket → request → release (the slot is NOT held
  during the retry backoff). Rate limits unchanged (Alpaca 180/min + burst
  10, Tiingo 40/h + 800/day); `calls`/`retries` counters now under a lock.
  A thread waiting for a slot calls `progress` every 5 s with an event
  WITHOUT "message" (`{source, waiting: True, concurrency}`) so a cancel can
  interrupt the wait. Live paper engine (`fund/market.py AlpacaFeed`) uses
  the same gates → its requests are limited too (concurrency only, no other
  behaviour change).
- **Error-kind contract (for R2 / jobs):** a `DataSourceUnavailable` caused
  by a connection-type failure has the instance attribute
  `kind == "connection"`; every other error has NO `kind` attribute, so
  `getattr(ex, "kind", None) == "connection"` is the test (helper
  `network.error_kind(ex)` returns "connection" or None; constant
  `network.CONNECTION`). Connection-type (`network.is_connection_error`):
  timeouts (connect/read, `TimeoutError`, `socket.timeout`, requests
  `ConnectTimeout`/`ReadTimeout`, aiohttp/asyncio timeouts), DNS
  (`socket.gaierror`/`herror`, urllib3 `NameResolutionError`), refused /
  reset / aborted / network or host unreachable (`ConnectionError`
  subclasses, errno ECONN*/ENET*/EHOST*/ETIMEDOUT/EPIPE and the Windows WSA
  codes), TLS (`ssl.SSLError` incl. certificate verification — a resume
  verifies again, nothing is bypassed), proxy errors, connection broken
  mid-response (`ChunkedEncodingError`, `IncompleteRead`,
  `ServerDisconnectedError`), aiohttp `ClientConnectorError`/`ClientOSError`
  — also when wrapped (`reason`, `__cause__`, exception args). Anything that
  carries an HTTP status (a response arrived: 429, 5xx, 4xx) is never
  connection-type. Set by `http_gate` (retries exhausted on timeouts →
  "… timed out — gave up after 3 attempts" + kind; network/TLS → at once +
  kind) and by `registry.fetch`'s catch-all. Registry status recording is
  unchanged.
- **Probe** `network.probe(source) -> bool`: alpaca = one
  `registry.fetch("equity_quotes", "latest_trade", "SPY")` (gate + limiter,
  records the source status); True = it worked, False = a connection-type
  failure, any other error (keys rejected, key missing, …) is RAISED
  (waiting cannot fix it). tiingo = no request, returns True (the next real
  request is the probe). Spacing ≥ 30 s per source (`PROBE_MIN_INTERVAL_SECONDS`):
  an earlier call SLEEPS until the interval has passed;
  `network.next_probe_in(source)` gives the seconds left so a job can wait
  cancellably first. `Prober` / `set_prober` for tests.
- **Parallel loading** (`backtest/engine.py _load_symbols`, used by `run`):
  the symbols of a run (strategy symbols + benchmark) load in a
  `ThreadPoolExecutor` of `loader.max_workers` (if the loader sets it) or
  `network.pool_size()` workers (8 adaptive / `fixed`); requests in flight
  are bounded per source by the limiters. Results, warnings
  (`no_volume_source` and data warnings) and `loaded` are assembled in the
  sequential order → identical result (test). First failure: symbols not yet
  started are skipped and running gate waits stop (internal `_LoadStopped`,
  `is_cancellation=True` so the source status is untouched); the error
  raised is the one of the earliest symbol (in load order) that failed on
  its own — what a sequential load raises. Progress: "loading SYM (k/n)" with
  the percentage of finished symbols; cancel is checked per symbol and in
  every gate progress event, from the worker threads.
- **Chunking** (`providers/alpaca.py`): `CORPORATE_ACTIONS_MAX_SYMBOLS = 10`,
  `CORPORATE_ACTIONS_MAX_DAYS = 1095` (≤ 3 years); `corporate_action_chunks`
  partitions symbols × [start, end] (groups in the given order, windows
  oldest first; start > end → one unchanged request); each chunk is one gate
  call; rows merged, an action `id` reported by two chunks kept once, sorted
  by a total key (ex_date, symbol, kind, then all values) → identical to one
  request (test). The seam's per-symbol refresh (seam → today) now makes
  ~4 requests per symbol. Bars unchanged (`FETCH_WINDOW`); request timeouts
  (Alpaca 20 s, Tiingo 30 s) and the gate's retry policy unchanged.
- Tests: `tests/test_sp_network.py` (classification incl. real `requests`
  classes when installed, gate/registry marking, AIMD, fixed mode, settings
  on a temp DB, probe spacing) and `tests/test_sp_network_loading.py`
  (limiter never exceeds its limit, token bucket ≤ 190/min and Tiingo-style
  window under 8 parallel callers with a fake clock, parallel == sequential
  engine result, first-error rule, chunking equivalence, live-feed path).

### 10.2 Parallel compute (owner R2)

- `validation/parallel.py`: a process pool (`spawn`, Windows-safe) with
  `workers = max(1, os.cpu_count() - 1)`; memory guard: estimated bytes per
  task (panel size × configs) — the pool size is reduced so the estimate
  stays ≤ 2 GB total; documented constants.
- Workers receive ONLY in-memory data (panels, corporate actions, volume,
  spec, request params) — they never import secrets, never open network
  connections, never touch the DB (assert in the worker entry: a guard
  that makes `secrets`, `http_gate` and storage imports raise in the
  worker). Results return to the parent, which writes everything.
- Grid backtests and stress runs of a validation run in the pool; results
  are assembled in the deterministic config order → identical report to a
  sequential run (test). Cancel terminates all workers.
- Backtests (single run) stay single-process.

### 10.3 Resumable runs (owner R2)

- **Checkpoints:** table `run_checkpoints(run_id, step_key, payload_json,
  sha256, created_at)` insert-only; step keys e.g. `data`, `base`,
  `fold:k`, `grid:<config-hash>`, `stress:<name>`. Each payload's sha256
  is stored and verified on reuse; mismatch → that step is recomputed and
  a warning `checkpoint_mismatch` is recorded.
- **Data checkpoint:** the run's data snapshot (per symbol: sources, ranges,
  row hashes — already in run cards) — the actual bars live in the cache.
- **Resume** (`POST /v2/backtests/{id}/resume` and
  `POST /v2/validations/{id}/resume`): allowed only for a run with status
  `failed` whose error kind is `connection` (others → 409 with the reason)
  and only if the basis is identical: same strategy version + content
  hash, same request (incl. end date as RESOLVED at the first start), same
  holdout window, and the cached data for the already-loaded symbols still
  hashes the same. Otherwise 409 "The basis changed (…) — start a new
  run." A resume creates a NEW run card with `resumed_from` = the failed
  run id (run cards stay insert-only after finish) and the same
  `trial_number` (counts as one trial: trial counting ignores cards that
  have a later `resumed_from` successor, and a holdout run is NEVER
  resumable — a failed holdout run is final; document why: the window
  could be re-run with knowledge of a partial result).
- **Auto-resume:** request flag `auto_resume: bool` (default false, UI
  checkbox). On a connection failure the job state becomes `waiting`
  (progress message "Connection lost — waiting for Alpaca (attempt n/5)…",
  probe every ≥ 30 s, no time limit); when the probe succeeds the run
  resumes from its checkpoints; after 5 automatic resumes the run fails
  normally (manual Resume still possible). Waiting runs block the single
  job slot (one run at a time) and are cancellable.
- Crash recovery (backend restart) marks queued/running/waiting runs failed
  as today with kind `connection`=false → resumable only if the last error
  was a connection error.

### 10.4 API + WPF (owner R2 routes, W UI)

- `GET/PUT /v2/settings/network` {adaptive, fixed} → {adaptive, fixed,
  current: {alpaca, tiingo}, min, max} (audited).
- Backtest/validation requests accept `auto_resume`; run cards and list
  rows carry `resumed_from`, `resume_count`, `error_kind`, `resumable`.
- WPF: settings section "Network" (toggle Adaptive; when off a number
  input 1–8 is visible; shows current concurrency); run dialogs get the
  checkbox "If the connection drops: wait and resume automatically (max 5
  times)"; failed runs with `resumable` show **Resume**; waiting runs show
  the waiting message + Cancel; reports show "resumed from <id>".

### 10.5 R2 notes (implemented 2026-10-09: parallel compute, checkpoints, resume, auto-resume, routes — §10.2–§10.4)

- **`validation/parallel.py`**: `plan_workers(symbols, sessions, n_tasks=None,
  cpu_count=None)` → `{workers, cpu_workers, memory_workers,
  per_worker_bytes, limit_bytes}`; workers = min(max(1, cpu − 1), memory
  guard, tasks). Estimate per worker = `WORKER_BASE_BYTES` (200 MB) +
  symbols × sessions × (`ROW_BYTES` 700 × `SERIES_PER_SYMBOL` 4 ×
  `SNAPSHOT_COPIES` 2 + 8 × `PANEL_ARRAYS` 12); total ≤ `MEMORY_LIMIT_BYTES`
  (2 GB). Sessions are estimated from history_from → training end
  (`ValidationJobs.pool_plan`). **1 worker = inline** (no process; the tasks
  run in the job thread on the job's loader — the sequential computation).
  `TaskRunner(workers, loader, run_engine, cancel, progress).run(phase,
  shared, tasks)` → results in task order. Tasks (`TASKS`, top-level,
  picklable): `grid`, `episode` (`stress.episode`), `costs`
  (`stress.synthetic_costs`), `isolation_check` (guard self-test).
- **Pool**: `multiprocessing.get_context("spawn")`; during `Process.start()`
  `sys.modules["__main__"]` is swapped for an empty module so `python
  main.py` is NOT re-executed in workers (tested with a fake main script).
  Data: every task first runs against a `CollectingLoader` (empty data — the
  engine records its loader calls, then stops with "no bars"); the parent
  fetches those calls through the job's `RecordingLoader` (network, cache,
  data checkpoints) and ships a snapshot `{call: pickled (rows, info) |
  frozen UserFacingError}`; workers use `ReplayLoader` (missing call or
  external feature `{"ext"}` → `NeedsPrefetch` → the parent loads it with
  `evaluate.load_ext` and re-runs those tasks; ≤ 3 rounds). A connection
  error is never shipped: it stops the run (wait/resume). Monte-Carlo runs
  in the parent. Cancel polls every 0.2 s and terminates all workers.
  A worker crash → RuntimeError (traceback in backend.log) → generic failure.
- **Worker guard** (`install_worker_guard`, first thing in `_worker_main`):
  `strategy_platform.secrets`, `keyring`, `win32cred` are stub modules (import
  works — `strategy/features/fred.py` imports `secrets` at module level —
  every use raises `WorkerIsolationError`); `socket.connect/connect_ex/
  sendto/create_connection/getaddrinfo` and `sqlite3.connect` raise;
  `storage.db.init/_open/_connect` raise; env vars matching `SECRET_ENV_RE`
  are removed. `http_gate` and `storage.db` cannot be refused at import
  (the engine imports them via seam/families) — their USE is blocked.
- **Validation jobs**: grid + stress run through the runner
  (`run_validation(…, runner, checkpoints, stress_tasks)`; `runner=None` =
  the old loops, kept for calibration tests; injected `run_stress` → the
  stress part keeps calling it). Stress assembly = run_stress's order
  (episodes incl. locked ones, costs_x2, slippage_x3, monte_carlo) + costs_x
  m when G6's multiplier ≠ 2. Pool == sequential: full report equality
  tested (`test_sp_parallel`). Plain backtests stay single-process.
- **Storage v8**: `backtest_runs.resumed_from` (UNIQUE when set — a chain
  cannot fork), `resume_count` (≥ 0, never decreases), `error_kind`
  (`connection` | `interrupted` | `basis` | `error`, set by `runcard.fail`),
  `auto_resume` (0/1, always 0 for holdout); a running card may be updated
  running→running ONLY in `resume_count` / `error_kind` (trigger,
  `db.backtest_run_note`). Insert-only `run_checkpoints(id, run_id,
  step_key, payload_json, sha256, created_at)`; db API
  `checkpoint_insert`, `checkpoints_list(run_ids)`, `backtest_run_chain`,
  `backtest_run_successors`. Trial counts (`family_trial_count`,
  `backtest_run_finish`, `family_done_runs`) skip cards that have a
  `resumed_from` successor (only failed cards can have one; the failed card
  never had a trial number, the chain's final done card gets one → ONE
  trial).
- **Checkpoints** (`runcard.CheckpointStore(run_id)` over the chain run →
  resumed_from → …): `sha256(run_id \n step_key \n payload_json)`; `get`
  verifies the NEWEST row of a step — mismatch → None + warning
  `{code: "checkpoint_mismatch", step, run_id, message}` on the card, the
  step is recomputed (no older row is used). Steps: `basis`,
  `data:<method>:<symbol>…` (row count + sha256 of the rows),
  `grid:<hash>`, `stress:<key>:<hash>` (hash = spec content hash, range,
  capital, benchmark, scenario parameters).
- **Basis** (`runcard.current_basis/basis_problem/ensure_basis`): strategy
  id, version, content hash (also re-checked against the stored version),
  request, resolved end (request end or the training end), holdout window
  {start, end}. Written at the first start; every later attempt (auto or
  manual) compares → `BasisChanged` ("The basis changed (…) — start a new
  run.", error_kind `basis`, not resumable). Data: a call whose rows hash
  differently from the data checkpoint of an earlier attempt → BasisChanged.
  The window rolls daily, so a resume on another trading day is refused
  (user-accepted strictness).
- **Resume** `jobs.resume(run_id, auto_resume=None)` → NEW card
  (`runcard.create_resumed`: same strategy/version/hash/spec/request/seed/
  kind, `resumed_from`, `resume_count` = old + 1, auto_resume = given or
  inherited); refusals (`runcard.resume_refusal`, re-checked under the
  runner lock): holdout ("A holdout run cannot be resumed: a failed holdout
  run is final — re-running the window with knowledge of a partial result
  would contaminate it …"), status ≠ failed, error_kind ≠ connection,
  already resumed ("This run was already resumed as <id>."), basis changed.
- **Auto-resume** (`JobManager._work`): on a connection error with
  `auto_resume` and < `MAX_AUTO_RESUMES` (5) automatic resumes: card
  error_kind `connection` (noted), job `waiting` (progress phase `waiting`,
  message "Connection lost — waiting for Alpaca (attempt n/5)…" / Tiingo by
  the error's `source`), waits `max(30 s, network.next_probe_in)` on the
  cancel event, then `network.probe`; True → resume_count + 1, error_kind
  cleared, the SAME card's work re-runs (checkpoints reused); no time limit;
  a probe error that is not a connection error fails the run. The 6th
  connection failure fails the card (`connection`, resumable by hand). The
  budget is per card (a manual resume starts a new budget). Cancel while
  waiting → cancelled. Stress runs inside the job turn a connection error into
  a run failure (not a "failed" scenario). Crash recovery: queued/running →
  failed `interrupted` ("Backend stopped during the run"), a card that was
  waiting → failed `connection` ("Backend stopped while the run was waiting
  for the connection", resumable).
- **Routes** (behind api_auth): `POST /v2/backtests` and `POST
  /v2/validations` accept `auto_resume: bool` (default false). `POST
  /v2/backtests/{id}/resume` (any kind; holdout → 409) and `POST
  /v2/validations/{id}/resume` (validation/holdout cards, else 404) body
  `{auto_resume?: bool}` → 202 `{run_id, resumed_from}`; 404 `{error}`; 409
  `{error}` (refusal text) or `{error, run_id}` (busy); 400 `{error, errors}`.
  Backtest/validation list rows and details add `resumed_from, resume_count,
  error_kind, auto_resume, resumable` (resumable = failed + error_kind
  connection + not holdout + not yet resumed; the basis is checked on
  Resume) and `status` = "waiting" while the job waits. `GET
  /v2/settings/network` → `{adaptive, fixed, current: {alpaca, tiingo},
  min, max}`; `PUT {adaptive, fixed?}` (fixed 1–8, default = current) →
  same shape, audit `network_settings_changed` {old, new}.
- **Review fixes (2026-10-10)**: (1) *Orphan workers*: every worker runs a
  watchdog thread on `multiprocessing.parent_process().sentinel` (+ getppid
  change on POSIX) → `os._exit` within ~`WATCHDOG_SECONDS` (1 s) when the
  parent dies; running pools are registered (`active_workers()`) and
  `parallel.terminate_all()` is called by main.py's `/shutdown` before its
  `os._exit`. (3) *Data checkpoint* hashes rows without `VOLATILE_ROW_KEYS`
  (fetched_at, known_at, checked_at, updated_at, created_at,
  realtime_start, retrieved_at) — daily re-fetched corporate actions no
  longer break an overnight resume; the auto-resume wait loop re-checks the
  basis before every probe and fails the run (`basis`, with the reason) once
  it cannot match any more (e.g. the holdout window rolled). (4)
  *Classification*: `is_connection_error` follows `__cause__` and
  `ConnectionLost` only (not `__context__`); same for `jobs.source_of`.
  (5) *Audit hook* (`install_audit_hook`, in the worker entry before
  unpickling; numpy/engine modules are imported first because numpy loads
  ctypes): raises `WorkerIsolationError` on `socket.*`, `ctypes.*`,
  `sqlite3.connect` (incl. `_sqlite3` / `sqlite3.Connection(path)`),
  `subprocess.*` / `os.exec*` / `os.spawn*` / `os.system` / `os.fork`,
  `winreg.*`, urllib/http.client connects, and `open()` of `.env`,
  `*.db*`/`*.sqlite*`, `data/api-token` and Windows credential/vault/protect
  or `.ssh` paths (monkey-patch guards kept). (6) The basis includes
  `engine_version` (`engine.ENGINE_VERSION`) and `report_version`
  (`report.REPORT_VERSION`): a mismatch → 409 "The basis changed (the engine
  was updated (A → B)) — start a new run." (7b) `jobs.source_of` → alpaca |
  tiingo | fred | sec_edgar | the source name | unknown; message "waiting for
  FRED/SEC EDGAR/…"; sources without a `data.network` probe (FRED, SEC
  EDGAR, others) send no probe request — after the 30 s wait the run resumes
  and its next real request is the probe (fails again if still down).
- Tests: `test_sp_parallel.py` (17: plan, freeze/thaw, ext prefetch,
  classification (`__context__` vs `__cause__`), volatile-key hash,
  forbidden paths, isolation guard + audit hook in a real worker (17
  bypass checks), spawn from a script, workers end when the parent dies,
  `terminate_all`, cancel, pool == sequential report, inline == sequential,
  stress scenarios incl. done episodes and costs x3, validation job on the
  pool, pool cancel), `test_sp_resume.py` (20). `test_sp_api_backtests.py` /
  `test_sp_api_validation.py` key sets extended by the new fields.

## 11. Explain: attribution (layer 1) + AI explanation (layer 2) (contract, 2026-10-14)

Reason (user, 2026-10-14): a button in the backtest and validation report
that answers "why did the strategy perform the way it did", AI-supported.
Risk named and accepted: an LLM finds a plausible story for ANY result
(confabulation, hindsight). Therefore two layers: layer 1 computes the
answer deterministically (numbers, no AI); layer 2 only puts layer 1 into
words. Interview decisions (user, 2026-10-14): model **selectable per
click** (Standard / Premium, as in the Newsroom); the AI may see the
**numbers AND the strategy logic** (the spec leaves the machine via
OpenRouter); explanations are **stored with the run** (regenerate = extra
click, costs again); layer 1 is **always visible** as a report section
without AI; **own daily budget** for explanations (not the fund's AI
budget); language **selectable EN/DE**; backend **checks every number** in
the AI text against layer 1 and **marks** unmatched numbers (shown, not
discarded).

### 11.1 Layer 1 — attribution (`backtest/attribution.py`, owner X1)

- Computed at the end of every backtest job (also the base run of a
  validation) from the in-memory result; pure Python/numpy, deterministic,
  no network, no AI. Stored on the run card in a new column
  `attribution_json` (migration; part of the insert-only finished card).
  Runs finished before this change have no attribution → the report says
  "Not available for runs created before engine <version> — run the
  backtest again." (no recomputation, no guessing).
- **Engine inputs:** the engine records per symbol and day (sparse, only
  non-zero) mark-to-market P&L on the `split` series, dividends received /
  paid, fees, slippage, borrow fees, funding → `BacktestResult.symbol_pnl`.
  ENGINE_VERSION bump. The PaperBroker parity test must stay green
  (recording only, no behaviour change).
- **Contents** (every block may be `{"unavailable": reason}`):
  1. `decomposition` — daily OLS r_t = a + b·m_t + e_t vs the run's
     benchmark (same regression as metrics alpha/beta): cumulative and
     annualised market part (b·m), alpha part (a), residual (e); gross P&L,
     costs (fees, slippage, borrow, funding), dividends net, cash; must
     reconcile to the equity change (`unattributed` reported; test: within
     0.01 % of starting capital on synthetic data).
  2. `by_symbol` — per symbol: P&L $ and share of total, long P&L, short
     P&L, days held, fills, costs; top 10 and bottom 10 + "others".
     Sum over symbols + cash/other = total P&L (test).
  3. `by_year` — per calendar year: strategy return, benchmark return,
     excess, max drawdown, avg net exposure.
  4. `by_regime` — 4 market regimes from the BENCHMARK, known at t (no
     look-ahead): trend = trailing 63-session benchmark return ≥ 0 → up,
     else down; volatility = trailing 21-session realised vol ≥ its
     expanding median → volatile, else calm (first 63 sessions =
     "warm-up", excluded). Per regime: days, share of days, strategy and
     benchmark annualised return, hit rate. Constants in one place.
  5. `long_short` — P&L and average exposure of the long and short side.
  6. `periods` — best 3 and worst 3 non-overlapping 63-session windows
     (strategy vs benchmark return); top 3 drawdowns (start, trough,
     recovery or "not recovered", depth) with the 3 symbols that lost most
     in each.
  7. `costs` — totals and as % of gross P&L, turnover (from metrics).
  8. `signal` — references to the stored IC / ICIR / half-life metrics
     (copied, not recomputed).
- **Fact table:** `facts: [{id, label, value, unit, display}]` — every
  number above gets a stable id (e.g. `dec.alpha_ann`, `sym.NVDA.pnl`,
  `year.2008.excess`) and the exact display string the report shows.
  Layer 2 may only use these; the number check uses them.
- **Validation runs:** layer 1 = the attribution of the validation's base
  (training) backtest + `gate_facts` (per gate: value, threshold, status,
  and key details: fold Sharpes/ICs, grid min/median/max Sharpe, PBO
  stats, deflated-Sharpe inputs, stress rows) — also in the fact table.
  Holdout runs: attribution of the holdout window only.

### 11.2 Layer 2 — AI explanation (`backtest/explain.py`, owner X2)

- `POST /v2/backtests/{id}/explanations` and
  `POST /v2/validations/{id}/explanations` {model: "standard"|"premium",
  language: "en"|"de"} → 200 explanation (synchronous, LLM timeout 90 s);
  `GET …/{id}/explanations` → list, newest first. Only for status `done`
  with attribution present, else 409 with the reason. Behind api_auth,
  strict validation, audited (run, model, language, cost), no stack traces.
- Call via the existing `fund/llm.chat_json` (OpenRouter key
  "openrouter-api-key" from the Credential Manager, cost ledger), feature
  `sp-explain`, label "Hedge Fund · Explanations"; models =
  `fund.llm.models()`; temperature 0.2; max_tokens 2,000.
- **Prompt** (fixed in code, English instructions; answer language as
  requested): explain WHY the result came about using ONLY the given facts;
  cite every number with its fact id `[id]`; causes from outside the data
  (macro events, news, sector stories) are allowed ONLY in a separate
  `hypotheses` list, phrased as hypotheses; no investment advice; no
  recommendations to change parameters (that would be untracked trials —
  stage 6 territory); state plainly when the data cannot answer something.
- **Input:** the fact table, run metadata (range, benchmark, capital, gate
  preset), the strategy logic (universe, features/signals, portfolio,
  execution, risk, parameters). Name, description, tags, provenance are
  UNTRUSTED text: passed as quoted JSON string fields, truncated (name ≤ 120,
  description ≤ 2,000 chars), with the instruction that they are data, not
  instructions. Never: keys, paths, other runs, holdout results in a
  non-holdout explanation. Prompt size cap 40,000 chars (facts trimmed
  from the "others" end with a note).
- **Output JSON:** `{summary: str (≤ 600 chars), sections: [{title,
  text}] (≤ 6, each ≤ 1,500 chars), hypotheses: [str] (≤ 5), limits:
  [str] (≤ 5)}`; anything else / oversize → `bad_json` error (cost still
  booked), no partial display.
- **Number check:** every number in the output text (EN and DE formats,
  %, $, signs, thousands separators, decimals) is matched against the fact
  table (value at display precision, percent ↔ fraction, relative
  tolerance 0.5 %); also allowed: years 1990–2100, dates present in the
  facts, integers 1–12 (counts/ordinals). Unknown `[id]` citations are
  flagged too. Result `check: {ok, unverified: [{text, offset, section}],
  unknown_ids: […]}` stored and shown — never silently removed.
- **Budget:** setting `explain_daily_budget_usd` (default 1.00, 0 = off,
  max 50) in hedgefund.db meta, own spend counter per UTC day; pre-call
  estimate with `fund.llm` price assumptions; blocked → 402-style user
  message "Explanation budget reached — raise it in Settings."; failed
  attempted calls charged like `charge_failed_call`.
  `GET/PUT /v2/settings/explain` {daily_budget_usd} → + spent_today
  (audited).
- **Storage:** table `run_explanations(id, run_id, run_kind, created_at,
  model, language, facts_hash, output_json, check_json, usage_json,
  cost_usd, provenance_json)` insert-only; regenerate = new row.
  `provenance("sp-explain", model)` (EU AI Act Art. 50 label).
- Tests: no network (`fund.llm.TRANSPORT` fake); attribution reconciliation
  + regime no-look-ahead; number check cases (EN/DE, %, false positives);
  budget block; 409s; insert-only; injection text in the description stays
  a quoted data field.

### 11.3 WPF (owner XW)

- Backtest report and validation report: new section **Attribution**
  (always, no AI): decomposition bar (market / alpha / costs / dividends),
  by-symbol top/bottom table, by-year table (strategy vs benchmark,
  excess coloured), regime table, best/worst periods, drawdown
  contributors; validation report: attribution of the base run.
- Button **"Explain with AI…"** (enabled only for `done` runs with
  attribution; tooltip with the reason otherwise) → panel: Standard /
  Premium toggle with estimated cost, EN / DE toggle, "Generate".
  Waiting state "Writing explanation…". Result header "✦ AI-generated ·
  <model> · <time> — explains the computed attribution; not verified, not
  investment advice". Unverified numbers highlighted red with tooltip "not
  in the computed data"; hypotheses in their own labelled box; plain text
  only (no markup, no links). "Regenerate (costs again)" and "Earlier
  versions (n)".
- Settings: "AI explanations — daily budget (USD)" with today's spend.

### 11.4 Decision: existing runs (user, 2026-10-14)

Runs finished before this change get NO attribution (also no partial one
from stored curves and no replay button): "We are in the development phase,
that is fine." The report shows the "Not available … run the backtest again"
note from §11.1; the "Explain with AI…" button is disabled with that reason.

### 11.5 X1 notes (implemented 2026-10-14: attribution — §11.1, backend part)

- **Files**: `backtest/attribution.py` (new; formulas in its docstring),
  `backtest/engine.py` (recording only), `backtest/runcard.py`,
  `backtest/jobs.py`, `validation/jobs.py`, `validation/report.py`,
  `storage/db.py`, `api/routes.py`; review fixes 2026-10-16 (L3, L7) also
  `validation/parallel.py`, `backtest/stress.py`. Tests: `test_sp_attribution.py` (21);
  key sets extended in `test_sp_api_backtests.py` /
  `test_sp_api_validation.py`; `test_sp_resume.py` asserts
  `SCHEMA_VERSION >= 8` (was `== 8`).
- **Engine** `ENGINE_VERSION = "sp-backtest/3c.2"` (the resume basis
  includes it: a run started on 3c.1 cannot be resumed → "The basis changed
  (the engine was updated …)"). New dataclass field `BacktestResult.symbol_pnl`
  = `{fields: ["date", "symbol", "pnl", "mtm", "dividends", "fees",
  "slippage", "borrow", "funding", "mv"], rows: [[…], …]}` — one row per
  (VALUED close, symbol) where the symbol was held at that close, was held
  at the previous valued close, or booked cash in between; sorted by date,
  symbol. Cash ledger rows are binned to the first valued close at/after
  their ts. `mtm` = mv_t − mv_prev + fill cash (contains the fill
  slippage); `dividends` (dividend + dividend_short), `fees` (−(fee +
  liquidation_fee)), `borrow` (borrow_fee), `funding` = signed cash (+ =
  gain); `slippage` = POSITIVE reconstructed cost (metrics.costs rule),
  already inside `mtm`; `pnl` = mtm + dividends + fees + borrow + funding;
  `mv` = signed qty × close quote. Ledger rows without a symbol other than
  deposit/reset → symbol `"_cash"` (none today). Σ pnl = last valued equity
  − capital (tested). The simulation is unchanged (parity tests green).
  `validation.jobs._strip_result` drops `symbol_pnl` from the payload
  shipped to pool workers.
- **Recording flag (review L7, 2026-10-16)**: `engine.run(…,
  record_symbol_pnl=True)`; default ON (single backtests, validation base
  run, holdout run); OFF (`symbol_pnl` None) in every grid run
  (`parallel.grid_task`, the sequential grid loop in `validation/jobs.py`)
  and stress run (`stress.episode`, `stress.synthetic_costs`) — their
  results are reduced to returns/metrics. Same simulation either way
  (tested: equity, fills, ledger equal). An attribution of a current-engine
  result without symbol_pnl says "Per-symbol P&L was not recorded for this
  run (engine flag record_symbol_pnl off)." (`attribution.NOT_RECORDED`);
  engine 3c.1 results keep the old-run text.
- **Storage v9**: `backtest_runs.attribution_json TEXT` (in `RUN_COLUMNS`
  and `RUN_RESULT_COLUMNS`; written only by running → done); the
  `backtest_runs_running_note` trigger is rebuilt to refuse it too. Cards
  finished before v9 stay NULL (§11.4). Validation and holdout runs live in
  the same table (kind), so they use the same column. **X2: take v10.**
- **When computed**: `jobs._run` after the metrics (phase `metrics`,
  message "computing the attribution"); the validation job from the BASE
  (training) result and its metrics; the holdout job from the holdout
  window's result (= the window only). Never checkpointed: the base run is
  not a checkpoint step (§10.5), a resumed attempt re-runs it from the data
  checkpoints and recomputes the attribution deterministically.
- **`attribution` (stored JSON, `attribution.compute(result, metrics)`)**:
  `{version: "sp-attribution/1", engine_version, currency: "USD", range:
  {start, end, calendar}, capital, benchmark, decomposition, by_symbol,
  by_year, by_regime, long_short, periods, costs, signal, facts, note?}`;
  every block may instead be `{unavailable: reason}`; `note` (only when the
  result had no symbol_pnl) = the old-run text.
  - `decomposition` = `{total_pnl, regression: {benchmark, pairs, beta,
    alpha_daily, market_cum, alpha_cum, residual_cum, market_ann,
    alpha_ann, residual_ann, market_pnl, alpha_pnl, residual_pnl,
    outside_regression_pnl} | {unavailable}, sources: {price_pnl,
    slippage, fees, borrow, funding, dividends_net, cash_other,
    costs_total, gross_pnl, unattributed} | {unavailable}}` (costs
    positive; beta/alpha_ann = `metrics.benchmark_stats`, the same OLS;
    market+alpha+residual+outside = total_pnl).
  - `by_symbol` = `{symbols, top: [row], bottom: [row], others: {count,
    pnl, long_pnl, short_pnl, flat_pnl, costs, dividends, share},
    reconciliation: {total_pnl, symbols_pnl, cash_other, unattributed}}`,
    row = `{symbol, pnl, share, long_pnl, short_pnl, flat_pnl, days_held,
    fills, costs, dividends}`; top = best min(10, ⌈n/2⌉), bottom = the 10
    worst of the rest (worst first).
  - `by_year` = `{years: [{year, days, return, pnl, benchmark_return,
    excess, max_drawdown, avg_net_exposure}]}`.
  - `by_regime` = `{benchmark, trend_window: 63, vol_window: 21,
    warmup_sessions: 63, warmup_days, unknown_days, labelled_days, regimes:
    [{regime (up_calm|up_volatile|down_calm|down_volatile), days, share,
    strategy_return_ann, benchmark_return_ann, hit_rate}]}` (label of day
    j's return from the benchmark account up to the close of j−1;
    `attribution.regime_labels`).
  - `long_short` = `{long_pnl, short_pnl, flat_pnl, avg_long_exposure,
    avg_short_exposure}` (row side = sign of mv at the previous valued
    close, else at this close, else flat).
  - `periods` = `{window: 63, best: [{start, end, strategy_return,
    benchmark_return, excess}] ×3, worst: […] ×3, drawdowns: [{start
    (peak), trough, recovery (null = not recovered), recovered, depth,
    days_to_trough, contributors: [{symbol, pnl}] (≤ 3, losses only)}] ×≤3}`.
  - `costs` = `{fees, liquidation_fees, slippage, borrow, funding, total,
    gross_pnl, net_pnl, pct_of_gross_pnl, pct_of_gross: {fees, …},
    pct_of_gross_reason?, turnover_annual}` (copied from metrics).
  - `signal` = cross-sectional `{mode, symbols, horizons: [{horizon,
    mean_ic, icir, t_stat, days, reason}], half_life_days,
    half_life_reason}` | time-series `{mode, symbols, window_days, windows,
    positive_share, mean_corr, reason}` (copied from metrics).
- **`facts`** = `[{id, label, value, unit, display}]`, value = the raw
  number (or ISO date / status text), display = the exact report string;
  null values are omitted. Units and display: `usd` "$1,234.56" /
  "-$1,234.56" (`run.capital`, `gate.G8.*` with 0 decimals); `fraction`
  (×100) "12.34 %"; `percent` (already %) "12.34 %"; `number` (2 decimals;
  ICs 4, probabilities/PBO/DSR 3); `count` "12"; `days` "12.3"; `date`
  ISO; `status` text. **Ids**: `run.capital|start|end`;
  `dec.total_pnl|beta|market_cum|market_ann|market_pnl|alpha_cum|alpha_ann|
  alpha_pnl|residual_cum|residual_ann|residual_pnl|outside_regression_pnl|
  price_pnl|slippage|fees|borrow|funding|dividends_net|cash_other|
  costs_total|gross_pnl|unattributed`; `sym.<SYMBOL>.pnl|share|long_pnl|
  short_pnl|days_held|fills|costs` (top + bottom only), `sym._others.pnl|
  count|share`, `sym.symbols`, `sym.unattributed`; `year.<YYYY>.return|
  benchmark|excess|max_drawdown|net_exposure|pnl`; `regime.<regime>.days|
  share|strategy_ann|benchmark_ann|hit_rate`, `regime.warmup_days`,
  `regime.unknown_days`; `ls.long.pnl|exposure`, `ls.short.pnl|exposure`,
  `ls.flat.pnl`; `period.best<k>.start|end|strategy|benchmark|excess`,
  `period.worst<k>.…` (k = 1..3); `dd.<k>.depth|start|trough|recovery
  ("not recovered" as status)|days_to_trough`, `dd.<k>.sym.<SYMBOL>.pnl`;
  `cost.fees|liquidation_fees|slippage|borrow|funding|total`,
  `cost.pct_of_gross`, `cost.<component>.pct_of_gross`, `cost.turnover`;
  `sig.ic_h<h>.mean|icir|t_stat|horizon`, `sig.half_life_days`,
  `sig.ts.positive_share|mean_corr|window_weeks (13)|window_days (65/91)`.
  **Method constants as facts** (review L3: numbers named in labels must
  be fact values for the number check), unit `count`, display = the
  integer: `run.periods_per_year` (252 | 365), `regime.trend_window` (63),
  `regime.vol_window` (21), `regime.warmup_sessions` (63), `period.window`
  (63), `sym.top_n` (10), `sig.ic_h<h>.horizon`,
  `val.stress.<key>.percentile_p5|p50|p95` (5/50/95, Monte-Carlo),
  `gate.H.horizon`, `gate.H.required_ratio` (fraction, "50.00 %"). Tested:
  every integer > 12 (not a year) in any label is a fact value.
- **`gate_facts`** (validation report AND holdout report, key
  `report.gate_facts`; `REPORT_VERSION` "sp-validation/4.2";
  `attribution.gate_facts(report)`, recomputed by `report.merge` so the
  statuses follow exceptions) = `[{gate, name, status, value, threshold,
  facts: [fact]}]` for every gate in the report (G1–G8; holdout: H). Ids:
  `gate.<G>.status|value|threshold` (units: G1 number (time-series: count),
  G2 number, G3 days, G4/G5 number 3 dp, G6 fraction, G7 count, G8 usd, H
  number) + details: G1 `val.fold.<k>.sharpe|mean_ic|total_return`,
  `gate.G1.folds_positive|best_horizon`; G2 `gate.G2.alpha_annual|beta`; G3
  `gate.G3.half_life_days|avg_holding_days`; G4 `val.dsr.sharpe_annual|
  sr0_annual|n_trials|trial_variance|skew|kurtosis|returns`; G5
  `val.pbo.value|configs|combinations|overfit_combinations|mean_logit`; G6
  `val.grid.sharpe_min|sharpe_median|sharpe_max|configs|base_sharpe`,
  `gate.G6.costs_sharpe`, `val.stress.<key>.strategy_dd|benchmark_dd`
  (episodes), `val.stress.<key>.total_return|sharpe|max_drawdown` (done
  scenarios), `val.stress.monte_carlo.max_drawdown_p5|p50|p95`,
  `….cagr_p5|p50|p95`; G7 `gate.G7.trades_closed|trades_open`; G8
  `gate.G8.capacity_usd|capital`; H `gate.H.sharpe|training_sharpe|mean_ic`.
- **API payloads** (behind api_auth): `GET /v2/backtests/{id}` adds
  `attribution` (the stored JSON, any kind) and `attribution_unavailable`
  (null when present, else the reason: "The attribution is computed when
  the run finishes." for queued/running, "Not available: the run is
  <status>." for failed/cancelled, and for done cards without it "Not
  available for runs created before engine sp-backtest/3c.2 — run the
  backtest again."). `GET /v2/validations/{id}` adds the same two fields;
  there `attribution` = `attribution.with_gate_facts(stored, merged
  report)`: the stored attribution + `gate_facts` + their facts appended to
  `facts` (ONE fact table for layer 2 and the number check). Lists
  unchanged. X2: use `attribution.unavailable_reason(card)` for the 409
  text and `with_gate_facts` for validation explanations.

### 11.6 X2 notes (implemented 2026-10-15: AI explanation — §11.2, backend part)

- **Files**: `backtest/explain.py` (new: prompt, call, schema, number check,
  budget, storage glue), `storage/db.py` (v10), `api/routes.py` (6 routes).
  Tests: `test_sp_explain.py` (unit, 29), `test_sp_explain_api.py` (routes on
  real attributions of a synthetic backtest + validation, 18). `fund/llm.py`
  and `newsroom/*` unchanged: `chat_json(…, feature="sp-explain",
  feature_label="Hedge Fund · Explanations")` registers the ledger label.
- **Storage v10**: table `run_explanations(id, run_id, run_kind
  (backtest|validation|holdout), created_at, model, language (en|de),
  facts_hash, output_json, check_json, usage_json, cost_usd,
  provenance_json)`, insert-only (triggers refuse UPDATE/DELETE); row +
  audit `explanation_created` in ONE transaction. Meta keys:
  `explain_daily_budget_usd` (absent = 1.00), `explain_spend_usd:<UTC
  YYYY-MM-DD>` (read-modify-write under `BEGIN IMMEDIATE`; an unreadable
  value fails closed → no call).
- **POST `/v2/backtests/{id}/explanations`** (kind backtest) and **POST
  `/v2/validations/{id}/explanations`** (kind validation|holdout), body
  exactly `{"model": "standard"|"premium", "language": "en"|"de"}` (unknown
  fields / query parameters → 400 `{error, errors: {field: message}}`).
  One call at a time per backend. 200 = the explanation:
  ```
  {"id": 7, "run_id": "…", "run_kind": "backtest", "created_at": "2026-10-15T09:00:00+00:00",
   "model": "<id OpenRouter answered with>", "model_tier": "standard", "language": "en",
   "facts_hash": "<sha256>",
   "output": {"summary": "…", "sections": [{"title": "…", "text": "…"}], "hypotheses": ["…"], "limits": ["…"]},
   "check": {"ok": false, "unverified": [{"text": "987.654,32 $", "offset": 41, "section": "summary"}],
             "unknown_ids": ["dec.nonexistent"]},
   "usage": {"cost_usd": 0.002, "prompt_tokens": …, "completion_tokens": …, "estimate_usd": 0.0135,
             "cost_estimated": false, "prompt_chars": …, "facts_total": …, "facts_sent": …, "facts_omitted": 0},
   "cost_usd": 0.002,
   "provenance": {"ai_generated": true, "generator": "UFOS.ai/HedgeFund/sp-explain", "provider": "OpenRouter",
                  "model": "…", "generated_at": "…", "human_reviewed": false,
                  "label": "AI-generated content (EU AI Act Art. 50)", "model_tier": "standard"}}
  ```
  `check.unverified[].section` ∈ `summary`, `sections.<i>.title`,
  `sections.<i>.text`, `hypotheses.<i>`, `limits.<i>`; `offset` = character
  offset inside that field (highlight `text` there). `[id]` citations stay
  in the text (the UI may show or strip them).
- **Errors** (all `{error: <user message>, code, …}`, never a stack trace,
  never a partial explanation): 404 `not_found` ("Backtest not found." /
  "Validation run not found."); 409 `wrong_kind` ("This is a validation run
  — explain it from the validation report."), `not_done` and
  `no_attribution` (text = `attribution.unavailable_reason(card)`, e.g. the
  "Not available for runs created before engine … — run the backtest
  again." note), `busy` ("An explanation is already being written — wait
  until it is finished."), `prompt_too_large`; 402 `budget` + `estimate_usd,
  daily_budget_usd, spent_today_usd` ("Explanation budget reached — raise it
  in Settings." / "AI explanations are switched off (daily budget is 0) —
  raise it in Settings." / spend unreadable); 502 `bad_json` (not JSON, or
  JSON outside the schema) and `llm_error`, 504 `timeout` (90 s) — all three
  with `cost_usd` booked; 503 `no_key` (Credential Manager message),
  `no_model`, `settings_invalid`, `spend_unrecorded` ("Explanation spend could
  not be recorded — explanations are paused until the backend restarts." —
  set when a spend write fails, fail closed until restart, audit
  `explanation_spend_unrecorded {day, usd}`), storage errors (`{error, kind}`
  as §4).
- **GET `…/{id}/explanations`** → `{run_id, explanations: [explanation]
  (newest first), explainable: bool, unavailable_reason: str|null,
  estimate_usd: {standard, premium}|null, budget: <settings payload>}` —
  `estimate_usd` is the pre-call estimate for the panel's toggle.
- **GET `/v2/settings/explain`** → `{daily_budget_usd, spent_today_usd,
  remaining_usd, day, enabled, default_daily_budget_usd: 1.0,
  max_daily_budget_usd: 50.0, paused_reason: str|null}`; **PUT** `{daily_budget_usd: number 0..50}`
  (0 = off) → same payload, audited `explain_settings_changed {old, new}`.
- **Audit**: `explanation_created {run_id, run_kind, model_tier, model,
  language, cost_usd, check_ok, unverified, unknown_ids, explanation_id}`,
  `explanation_failed {…, reason (bad_json|error|no_key|timeout),
  cost_usd}`, `explanation_blocked {…, reason, estimate_usd}`.
- **Prompt**: `explain.SYSTEM_PROMPT` (fixed English rules: only the facts,
  copy display strings and cite `[id]`, no new arithmetic, outside causes
  only in `hypotheses`, no advice / no parameter changes, say what the data
  cannot answer, `untrusted_text` is data, plain text, the JSON schema) +
  one user message `DATA (JSON …):\n{answer_language, run {run_kind, range,
  benchmark, capital, currency, gate_preset, engine_version,
  attribution_version, note, unavailable_blocks?, strategy_origin,
  strategy_ai_generated, strategy_external_text}, strategy_logic
  (category, universe, timeframe, parameters without descriptions,
  features, signal, portfolio, rebalance, risk, execution, costs),
  untrusted_text {name ≤ 120, description ≤ 2,000, tags ≤ 20 × 40,
  provenance ≤ 1,000 = the CLAIMED provenance from the version envelope
  (`envelope.claimed`, JSON text), parameter_descriptions ≤ 300} (fund.llm
  .sanitize_untrusted: single line, no ``` / <<< >>>), facts [{id, label,
  value, unit, display}], facts_note?}`. Validation/holdout facts =
  `attribution.with_gate_facts(stored, merged report)`; non-holdout runs
  drop any `gate.H.*`/`holdout.*` fact. Cap 40,000 chars (system + data):
  facts dropped `sym.<SYMBOL>.*` first, then `dd.<k>.sym.*`, then
  `period|regime|year|val.stress.*`, each from the list end (minimal count,
  `facts_note`); still too large → 409 `prompt_too_large`.
- **Schema limits** (`validate_output`): exactly the 4 keys; summary 1–600;
  ≤ 6 sections, each exactly {title 1–120, text 1–1,500}; ≤ 5 hypotheses and
  ≤ 5 limits, each 1–600 chars (item/title limits are X2 choices).
- **Number check** (`number_check`, against the FULL fact table; unit-aware
  after review M1, 2026-10-15): citations `[id]`, `[a, b]` are masked
  (unknown ids → `unknown_ids`); dates (ISO, DE `dd.mm.yyyy`, `yyyy-mm`) must
  occur in a fact value/display. Numbers: EN/DE separators, NBSP/apostrophe
  thousands (a lone `,`/`.` + 3 digits is read in the answer language),
  signs `+ - − –` (a dash after a digit is a range, not a sign), currency
  `$ US$ USD` (USD) and `€ EUR` (EUR) before — also glued, `USD98,765` — or
  after, `% percent Prozent pp Prozentpunkte`, `‰`, `bp/bps/Basispunkte`,
  scale words `k/thousand/Tsd.`, `m/M/million/Mio.`, `bn/billion/Mrd.`; a
  number glued to a letter (`Q3`, `H1`) is skipped. Token kinds: percent →
  only `fraction` (×100) and `percent` facts; USD → only `usd` facts (value
  × scale word, no ×100/÷100); EUR → never matches; plain → only
  `number|count|days|ratio` (and other numeric units). **Citation binding**:
  a number followed by a citation in the same clause (no `; ! ? newline`,
  no `. , :` + space, no other number in between) is checked ONLY against
  the cited fact(s), still unit-aware. Match = relative tolerance 0.5 %
  against the fact value and each number of its display string (display
  precision), or the exact display text; an unsigned text number matches
  either sign, an explicit sign must match. Allowed without a fact: plain
  integers 1–12 and four-digit years 1990–2100. Everything else →
  `unverified` (shown, never removed). Fabricated-number pass rate on a
  156-fact synthetic run: 3.75 % (test bound 6 %).
- **Budget**: `fund.llm.Budget` over the meta keys (fail closed; a failed
  spend write pauses explanations until restart — `fund.llm` only logs it);
  pre-call `fund.llm.estimate_cost(messages, 2000, tier)`; success books
  `usage_cost(usage, estimate)`; failures `charge_failed_call` (timeout =
  attempted → estimate). Separate from the fund's `ai_daily_budget_usd`.

## 12. Market Research consumer (2026-10-16)

The Market Research section (`backend/market_research/`, contract
`market_research/SPEC.md`) reuses this data layer. Every extension of
`strategy_platform` code it needs is recorded in BOTH SPECs (its §12).
MR1 (2026-10-16): no change to `strategy_platform` code — it reuses
`api/routes.dumps`, `read_body`, `NO_BODY`, `user_error` and `core.errors`.
MR2a (built 2026-10-16):
- `data/http_gate.py`: `research_priority()` (context manager setting a
  `contextvars` flag; Market Research copies the context into its own thread
  pool), `is_research_priority()`, module `research_stats(source)` /
  `HttpGate.research_stats()` → `{used_last_minute, waiting_seconds}`,
  `TokenBucket.try_acquire_above(reserve)`, constants `RESEARCH_MAX_PER_MINUTE`
  60, `RESEARCH_CAPACITY` 5, `RESEARCH_WINDOW_SECONDS` 60, `RESEARCH_RESERVE_TOKENS`
  3, `RESEARCH_ACQUIRE_TIMEOUT_SECONDS` 30, `RESEARCH_WAIT_STEP_SECONDS` 0.25.
  Under research priority an attempt takes a research token (60/min bucket +
  hard cap 60 per sliding 60 s), then a shared token only while more than 3
  remain and no normal caller waits for one — BEFORE the concurrency slot;
  after 30 s `DataSourceUnavailable` "Research waited too long for the Alpaca
  rate limit (shared with trading)". Normal priority: unchanged (shared bucket
  180/min, same order slot → window → token); the only addition is that a
  normal caller that must wait for a shared token is counted while it waits.
- `data/providers/alpaca.py`: `AlpacaProvider.assets()` (trading API
  `TradingClient(paper=True, raw_data=True).get_all_assets`, active `us_equity`
  + `crypto`, one gate call each, data type `assets`, same keys/gate/error
  classification), `load_trading_sdk()`, constructor kwarg `trading_sdk=None`.
  SIP/IEX 1Min for Research use the existing `get_bars` unchanged
  (`sip_request_end` clamps SIP requests to now − 15 min).
MR2a review fixes (2026-10-16):
- `http_gate.py`: Research takes a concurrency slot only after its tokens,
  via `network.AdaptiveLimiter.try_acquire()` (new, non-blocking), only while
  no normal caller waits for a token or slot, at most max(1, limit − 1) slots
  (limit 1 → only while no normal request is active); normal callers waiting
  for a slot are counted. The research cap counts SENDS: stamped at send time;
  `HttpGate.note_send()` is called by providers before every HTTP request and,
  under research priority, makes every further page of one call take its own
  permit + shared token (normal priority: only counted). `ResearchYield`
  (`UserFacingError`, `is_cancellation = True` → the registry records no
  source error) + `research_yield_check(fn)`: a waiting research call gives way
  when `fn()` is true.
- `cache.py`: the per-series lock is taken via `_held(key)` — normal callers
  waiting for it are counted; a research-priority holder that waits in the gate
  yields (`ResearchYield`), the lock is released and `get_bars` retries (≤ 50,
  `MAX_RESEARCH_YIELDS`). Normal callers: behaviour unchanged.
- `providers/alpaca.py`: `harden_client(client, timeout, on_send=None)` calls
  `on_send` (the gate's `note_send`) before every request; `get_bars` accepts
  timeframe `"30Min"` (`RESEARCH_TIMEFRAME`; Market Research only — never via
  the cache or `pit`, whose `TIMEFRAMES` are unchanged; `known_at` = start +
  30 min; SIP end clamped by `sip_request_end` like 1Min).
MR6 (built 2026-10-17): `providers/alpaca.py` —
- news dicts carry `images: [{size, url}]` (`_news_images`: https URLs only via
  `_safe_url`, ≤ 5; existing keys unchanged);
- `AlpacaProvider.logo(symbol) -> bytes | None`: `GET
  https://data.alpaca.markets/v1beta1/logos/{symbol}` (crypto in the news spelling
  `BTCUSD`, never `?placeholder=true`) with plain `requests` (`load_requests()`,
  constructor kwarg `http=None` = a session factory for tests), keys in the
  `APCA-API-*` headers only, `allow_redirects=False`, through the SAME Alpaca gate
  (data type `logos`, `note_send` per request, research priority applies);
  HTTP 404 → `None`; 401/403 → `LogosNotAvailable(DataSourceUnavailable)` (Alpaca
  lists logos as a paid Broker API add-on); other statuses classified as before;
  checks: `image/png`, PNG signature, ≤ `LOGO_MAX_BYTES` (262 144).
MR5 (built 2026-10-17): no change to `strategy_platform` code. Market Research
computes its chart indicators (decision Q8) WITH this package's DSL evaluator —
`strategy/evaluate.evaluate_node` on a one-column `strategy/panel.Panel` of the
chart's bars (operators `sma`, `ts_std`, `ewm_mean`, `rsi`, `add/sub/mul/div`) —
and reads splits via the existing `AlpacaProvider.corporate_actions` (unchanged).
Consequence: changing the semantics of those operators (warm-up NaNs, seeding,
sample std, Wilder) changes the Research chart too; `tests/test_mr_indicators.py`
asserts the parity. Session VWAP and the compare overlay are Research-only (no
DSL operator).

MR8 (built 2026-10-17): `backtest/runcard.py` — `trades_summary(result)` adds
`fills_by_symbol` = `{SYMBOL: [[ts, side, qty, fill_price], …]}` (chronological,
at most `CARD_FILLS_MAX` = 10 000 fills per card, later fills dropped and
`fills_truncated: true`) to `trades_json` of NEW cards (additive key; the card
stays insert-only/immutable; cards finished before have no such key). Reason:
Market Research draws a run's entries/exits as chart markers
(`GET /research/backtests/{run_id}/markers`, read-only `mode=ro` connection to
hedgefund.db). No engine / simulation / metrics change.

## 13. Stage 5a — Strategy Studio + memory (contract, 2026-10-17)

Interview decisions (user, 2026-10-17): stage 5 in two sub-stages — **5a
Strategy Studio + memory** now, 5b paper → strategy + investment committee
later (5b: papers by PDF upload, pasted text AND open-access links (arXiv /
SSRN, https, size limit); committee = new 5-role panel reusing the Council
of AIs' AI plumbing). **Own ideation AI budget: $10 per month, max $2 per
day** (shared by 5a and 5b, separate from the fund's and the explanations'
budgets). **Every valid AI draft is backtested automatically** (user chose
this knowingly: each backtest counts as a trial → stricter G4 later; the
Studio shows the family's trial count next to every draft). **Memory = only
computed facts** (no AI-written lessons, no holdout results). The AI may use
**only the DSL** (56 validated operators); it may additionally **propose
missing operators as text** (never executed, shown as "operator request").

### 13.1 Backend (`strategy_platform/research/studio.py`, `memory.py`)

- Conversation model: `studio_sessions(id, strategy_id, created_at, title)`,
  `studio_messages(id, session_id, role user|assistant|system_note, text,
  spec_version, run_id, operator_requests_json, provenance_json, usage_json,
  cost_usd, created_at)` — insert-only. One session = one strategy family
  (the first valid draft creates a strategy `origin="studio"`, state draft,
  `ai_generated: true` in the envelope; follow-ups create new versions).
- `POST /v2/studio/sessions` {title?} → session; `POST /v2/studio/sessions/{id}/messages`
  {text ≤ 4,000 chars, model: standard|premium} → assistant message (sync,
  LLM timeout 90 s); `GET /v2/studio/sessions`, `GET …/{id}` (messages +
  versions + runs + trial count); `GET/PUT /v2/settings/ideation`
  {monthly_budget_usd (default 10, 0 = off, max 200), daily_cap_usd
  (default 2)} + spent_month/spent_today (audited).
- LLM via `fund/llm.chat_json` (feature `sp-studio`, label "Hedge Fund ·
  Strategy Studio"), budget check BEFORE the call (month AND day; estimate
  as in §11), failed attempted calls charged; no stand-in text.
- Prompt: role "Researcher"; input = user text (untrusted, quoted), the
  DSL reference (operators with signatures and descriptions, generated from
  the validator's registry — single source of truth), universe presets,
  the current spec (follow-ups), the last run's facts (§11 fact table
  summary) and the MEMORY block (13.2). Output JSON: {reply ≤ 2,000 chars,
  spec (StrategySpec JSON) | null, operator_requests: [{name, purpose,
  signature}] ≤ 5, assumptions: [str] ≤ 5}. Spec → existing validator;
  invalid → one automatic repair round with the validator's errors (counts
  against the budget), still invalid → shown with errors, no backtest.
- Valid spec → new version (provenance: model, prompt hash, session id,
  ai_generated) → automatic backtest job (§8.5, default range, ends before
  the holdout window; the single-job rule applies: if a job is running, the
  draft is queued as "waiting for the backtest slot" — visible, cancellable;
  queue length ≤ 3 per session) → the message gets run_id; the report and
  §11 attribution are available as usual. Trial counting unchanged (every
  finished run counts).
- Operator requests are stored and listed (`GET /v2/studio/operator-requests`)
  — text only; never executed; marked "unverified AI suggestion".
- No holdout data anywhere in Studio prompts (assert: facts with holdout
  ids are filtered, as in §11.2).

### 13.2 Memory (computed facts only)

- `memory_facts(id, family_id, strategy_id, version, run_id, kind, fact_json,
  created_at)` insert-only, written automatically when a backtest or
  validation run finishes (NOT holdout runs): which gates failed with
  values/thresholds, Sharpe/alpha t/cost share of gross P&L, turnover,
  dominant periods/regimes and top contributors from the §11 attribution,
  parameter-grid flatness (from validations), warnings (survivorship,
  participation cuts).
- Memory block for the Studio prompt: the current family's facts + the
  most relevant facts of other families with overlapping universe/operators
  (deterministic ranking: same universe preset, shared operators, recency;
  max 20 facts, ≤ 6,000 chars). Shown to the user in the Studio ("Memory
  used for this draft") — no hidden context.
- `GET /v2/memory?family_id=&strategy_id=` read-only list.

### 13.3 WPF

- Rail entry **"Studio"** (after Library, before Research): session list,
  chat view (user/assistant bubbles; assistant replies with the EU AI Act
  header "✦ AI-generated · <model> · <time>" + cost line), Standard/Premium
  toggle with estimated cost, budget line (month/day spent), draft cards
  (validator result, spec diff vs previous version, "operator requests"
  box, assumptions, trial count of the family, backtest status → mini
  metrics + "Open report"), "Memory used" expander, "Open in Library".
  Plain text only. Settings: ideation budget fields.

### 13.4 Tests
No network (TRANSPORT fake): budget month/day blocks + 0 = off; repair round;
invalid spec never backtested; auto-backtest queueing + single-job rule;
operator requests stored, never executed; memory facts written on finish
(not for holdout), ranking deterministic, size cap; no holdout facts in
prompts; prompt-injection text in the user message stays quoted; insert-only;
routes validation/auth.

### 13.5 5a backend notes (X2, implemented 2026-10-17: §13.1, §13.2, §13.4 backend)

All routes behind api_auth; strict bodies (unknown fields / query parameters
→ 400 `{error, errors: {field: message}}`); other errors `{error, code, …}`
(same shape as §11.6), never a stack trace. Times UTC ISO seconds.

- **POST `/v2/studio/sessions`** `{title?: str ≤ 120}` → 200 `session`:
  `{id: "ss-<12 hex>", title: str|null, created_at, strategy_id: str|null,
  family_id: str|null, trial_count: int, message_count: int,
  last_message_at: str|null}`. Audit `studio_session_created`.
- **GET `/v2/studio/sessions`** `?limit=1..200 (50)` → `{sessions:
  [session] (newest first)}`.
- **GET `/v2/studio/sessions/{id}`** → `{session, messages: [message]
  (oldest first), versions: [{version, created_at, content_hash,
  message_id}], runs: [{run_id, version, kind, status, created_at,
  finished_at, key_metrics|null}], trial_count, queue: [draft_backtest]
  (waiting drafts, FIFO), estimate_usd: {standard, premium} (pre-call
  estimate of the next message with the current context and a 4,000-char
  text), budget: <ideation settings payload>}`. 404 `not_found`
  "Studio session not found.".
- **POST `/v2/studio/sessions/{id}/messages`** `{text: str 1..4,000,
  model: "standard"|"premium"}` → 200 `{user_message: message,
  assistant_message: message, notes: [message]}` (sync; one Studio call at
  a time per backend; LLM timeout 90 s per call; at most 2 calls = the
  answer + one repair round). Nothing is stored when the call fails.
  Errors: 402 `budget` + `estimate_usd, monthly_budget_usd,
  spent_month_usd, daily_cap_usd, spent_today_usd` ("AI ideation is
  switched off (monthly budget is 0) — raise it in Settings." / "Monthly
  ideation budget reached — raise it in Settings." / "Daily ideation cap
  reached — try again tomorrow (UTC) or raise it in Settings." / spend
  unreadable); 503 `spend_unrecorded` ("Ideation spend could not be
  recorded — the Studio is paused until the backend restarts."); 409
  `busy` ("The Studio is already answering — wait until it is finished."),
  `prompt_too_large`; 502 `bad_json` / `llm_error`, 504 `timeout` (all
  three with `cost_usd` booked); 503 `no_key`, `no_model`.
- **message** = `{id, session_id, role: "user"|"assistant"|"system_note",
  text, created_at, spec_version: int|null, run_id: str|null,
  operator_requests: [{name, purpose, signature}], assumptions: [str],
  draft: draft|null, memory_used: [memory_fact_short], model: str|null,
  model_tier: "standard"|"premium"|null, provenance: {…EU AI Act label…,
  "model_tier", "prompt_hash", "session_id"}|null, usage: {cost_usd,
  prompt_tokens, completion_tokens, calls, estimate_usd}|null, cost_usd:
  number|null}`. User messages: text only. `system_note`: backend notes
  (queue full, backtest could not start, cancelled) — plain text, no AI.
- **draft** (assistant messages that carried a spec) = `{spec: <the
  proposed StrategySpec as returned by the AI (after repair)>, valid: bool,
  validation: {ok, errors: [{path, code, message, line?, column?}],
  warnings}, repaired: bool, repair_errors: [error]|null (errors of the
  first answer when a repair round ran), repair_skipped: str|null (e.g. the
  budget message), strategy_id: str|null, version: int|null, unchanged:
  bool, backtest: draft_backtest|null, requires_confirmation: str|null,
  not_saved: bool, not_saved_reason: str|null, saved_by_confirmation: bool,
  base_version: int|null}` — invalid drafts get no version and no backtest.
  **Review fix M1 (2026-10-17):** a valid follow-up draft is saved and
  backtested automatically ONLY while the strategy's state is `idea`,
  `draft` or `backtested`; otherwise it stays unsaved with
  `requires_confirmation: "demotes <state> to draft"` (e.g. "demotes
  validated to draft") until the user confirms (route below). `not_saved:
  true` + `not_saved_reason` = a valid draft that could not be saved (e.g.
  the strategy changed meanwhile — `valid` stays true).
- **draft_backtest** = `{message_id, state: "waiting"|"started"|"cancelled"|
  "start_failed"|"not_queued", position: int|null (1-based, waiting only),
  run_id: str|null, run_status: "queued"|"running"|"waiting"|"done"|
  "failed"|"cancelled"|null, key_metrics: {total_return, cagr, sharpe,
  max_drawdown, volatility}|null, message: str|null}` — "waiting" =
  "Waiting for the backtest slot" (another job is running; ≤ 3 waiting
  drafts per session, a 4th → `not_queued` "Backtest queue full (3 drafts
  waiting) — start this version from the Library."). The queue is
  persisted and dispatched FIFO when the job slot frees (and on every
  Studio request).
- **POST `/v2/studio/sessions/{id}/drafts/{message_id}/cancel`** (no body)
  → 200 `draft_backtest` (state cancelled); 409 `not_waiting` "This draft
  is not waiting for the backtest slot." (a started run is cancelled via
  `POST /v2/backtests/{run_id}/cancel`).
- **POST `/v2/studio/sessions/{id}/drafts/{message_id}/save`** `{confirm:
  true}` (exactly) → 200 `{draft, notes: [message]}` — saves the draft that
  `requires_confirmation` as a new version (demoting the strategy to draft,
  audited `studio_draft_saved`), then starts/queues its backtest like an
  automatic draft. Errors: 404 `not_found`; 409 `not_confirmable` ("This
  draft does not need a confirmation." — includes already saved / invalid
  drafts), 409 `stale` ("The strategy was changed meanwhile (current version
  N) — ask the Studio for a new draft."); 400 when `confirm` is not true.
- **GET `/v2/studio/operator-requests`** `?session_id=` (optional) →
  `{operator_requests: [{id, session_id, message_id, name, purpose,
  signature, created_at, label: "unverified AI suggestion"}]}` (newest
  first) — text only, never executed.
- **GET `/v2/settings/ideation`** → `{monthly_budget_usd (default 10),
  daily_cap_usd (default 2), spent_month_usd, spent_today_usd,
  remaining_month_usd, remaining_today_usd, month: "YYYY-MM", day, enabled,
  default_monthly_budget_usd: 10.0, default_daily_cap_usd: 2.0,
  max_monthly_budget_usd: 200.0, max_daily_cap_usd: 200.0,
  paused_reason: str|null}`; **PUT** `{monthly_budget_usd, daily_cap_usd}`
  (both required, numbers 0..200; 0 = off) → same payload, audited
  `ideation_settings_changed {old, new}`.
- **GET `/v2/memory`** `?family_id=&strategy_id=&limit=1..500 (200)` →
  `{facts: [memory_fact] (newest first)}`; `memory_fact` = `{id, family_id,
  strategy_id, version, run_id, kind, fact: {text, data, universe_key,
  operators, run_kind, range}, created_at}`; `memory_fact_short` (in
  messages) = `{id, family_id, kind, text, run_id}`.
- **Implementation**: `research/studio.py` (sessions, messages, budget,
  queue, routes' logic), `research/prompts.py` (fixed `SYSTEM_PROMPT`
  "Researcher", `REPAIR_INSTRUCTION`, DSL reference = `dsl.catalogue()` +
  `dsl.FIELDS` + `validator.ext_catalogue_default()` (single source of
  truth), schema reference from `strategy.schema` constants + an example,
  universe presets, answer schema check), `research/memory.py`. LLM:
  `fund.llm.chat_json(feature "sp-studio", label "Hedge Fund · Strategy
  Studio")`, temperature 0.3, max_tokens 4,000, 90 s, model =
  `fund.llm.models()[tier]` (no default); prompt cap 80,000 chars (409
  `prompt_too_large`). Data message: `{user_request (verbatim, quoted JSON
  string), conversation_so_far (last 10 turns, ≤ 2,000 chars each),
  current_spec, last_run {run_id, kind, version, range, key_metrics,
  facts [{id, label, display}] (≤ 60 / 6,000 chars, holdout ids removed),
  omitted}, memory [{id, kind, family, text}], universe_presets, schema,
  dsl}`. The AI's `provenance` key in a spec is dropped (an AI claim is not
  provenance). Answer schema: exactly `reply` (1–2,000), `spec` (object |
  null), optional `operator_requests` (≤ 5, {name 1–400, purpose ≤ 400,
  signature ≤ 400}), optional `assumptions` (≤ 5, 1–400 each); anything
  else → `bad_json`. Repair round only when the spec is invalid; a repair
  blocked by the budget → `draft.repair_skipped` = the budget message (no
  error, the invalid draft is shown).
- **Registry** (`lifecycle/registry.py`): origin `studio` added to
  `ORIGINS`; `create_from_studio(spec, ai_provenance)` and
  `new_studio_version(strategy_id, spec, base_version, ai_provenance)`
  (shared `_add_version`); envelope key `ai_provenance {feature, model,
  model_tier, prompt_hash, session_id, generated_at}` + `ai_generated:
  true`. Family / trial rules unchanged.
- **Storage v11**: `studio_sessions(id, created_at, title, strategy_id)`
  (insert-only; `strategy_id` set ONCE by the first valid draft),
  `studio_messages(… §13.1 columns …, detail_json {draft, assumptions,
  memory_used})`, `studio_backtests(id, session_id, message_id, strategy_id,
  version, event waiting|started|cancelled|start_failed|not_queued, run_id,
  note, created_at)` (the queue as an insert-only event log; a draft's
  state = its latest event; the message's `run_id` in the API = the
  `started` event's run id — the message row itself stays insert-only),
  `memory_facts(§13.2 columns)`; all insert-only by triggers. Meta:
  `ideation_monthly_budget_usd`, `ideation_daily_cap_usd`,
  `ideation_spend_month_usd:<YYYY-MM>`, `ideation_spend_day_usd:<YYYY-MM-DD>`
  (both counters booked in one transaction).
- **Jobs hook** (`backtest/jobs.py` `_after_job`, after every job, never
  raises): `memory.on_run_finished(run_id)` then `studio.on_slot_free()`
  (FIFO dispatch of waiting drafts). Automatic backtests use
  `JobManager.submit(strategy_id, version, {})` = the default range, which
  ends before the holdout window; failures to start (e.g. a missing key)
  → `start_failed` + a system note.
- **Memory kinds**: `metrics` (Sharpe, total return, CAGR, max drawdown,
  alpha, beta, cost share of gross P&L, turnover, closed trades),
  `contributors` (top/bottom 3 symbols), `periods` (best/worst 63-session
  window), `regimes` (best/worst regime), `warning` (code + message, ≤ 6),
  validations also `gate_failed` (gate, name, value, threshold — G2 value
  = alpha t), `validation_result`, `grid_flatness` (val.grid.* facts).
  Fact text is formatted deterministically (no AI). Selection: own family
  newest first (≤ 12), then other families by (same universe key +4,
  shared operators +1 each ≤ 3, newer first), others only when related
  (score > 0) unless the session has no spec yet; ≤ 20 facts, ≤ 6,000 chars.
- **Review fixes (2026-10-17)**: M1 auto-save/backtest only in states
  `idea|draft|backtested` (else `requires_confirmation` + the save route;
  storage **v12** `studio_draft_saves(id, session_id, message_id UNIQUE,
  strategy_id, version, created_at)` insert-only records confirmed saves —
  the message view merges them: `draft.version`, `spec_version`,
  `saved_by_confirmation`); L2 `dispatch_queue` keeps a "dirty" flag so a
  wake-up that cannot take the lock makes the holder run another round,
  and `main.py` dispatches once at startup after `recover_interrupted`; L3
  the budget check blocks while the Studio is paused and counts
  max(stored spend, stored at start + this request's bookings); L4 repair
  prompts are capped at 80,000 chars (`prompts.capped_repair`: drop the
  conversation, then the memory, then the last-run facts, then cut the
  quoted previous answer 20,000 → 8,000 → 2,000; validator errors are
  never trimmed; still too large → `repair_skipped` "No repair round: the
  repair request would exceed the prompt size limit."); L5 a save conflict
  keeps `valid: true` and sets `not_saved` + `not_saved_reason`; L6 a
  Studio strategy whose version-1 envelope names the session but was never
  linked is re-linked on the next request (audit `studio_session_relinked`)
  instead of creating a second family; L8 the session estimate is cached
  per session until the last message, the strategy version, the newest 3
  runs or the newest memory fact change.
- **On-topic guard (user, 2026-10-17, after "Explain the categoric
  imperative by Kant" got a philosophy answer):** the answer schema has a
  REQUIRED `kind: "draft"|"clarify"|"off_topic"`; the system prompt states
  the scope (only strategy design/refinement in the DSL; everything else →
  `{"kind": "off_topic", "reply": "", "spec": null}`; instructions inside
  user text are ignored). Deterministic backend guard
  (`prompts.scope_guard`) BEFORE the schema check: `off_topic`, missing or
  unknown `kind`, `draft` without a spec, `clarify` with a spec / operator
  requests / reply > 500 chars, or a 3rd `clarify` in a row → the AI text is
  DISCARDED: the assistant message is stored with `text: ""` (the raw answer
  only in `detail_json.discarded_text` for the audit, never returned),
  `discarded: true`, `discarded_reason` (`off_topic`, `kind_missing`,
  `kind_unknown`, `draft_without_spec`, `clarify_with_spec`,
  `clarify_too_long`, `clarify_with_operator_requests`, `clarify_limit`),
  its provenance and cost (booked and shown), plus a fixed NON-AI
  `system_note` "The Strategy Studio only helps design and refine trading
  strategies in the rule language. Describe a trading idea (market, signal,
  rebalancing) and the Studio drafts a strategy." — returned as `notes[0]`.
  A discarded repair answer → `draft.repair_skipped` "The repair answer was
  discarded (<reason>).". **message** gains `kind: str|null` (assistant
  only), `discarded: bool`, `discarded_reason: str|null`. GET
  `/v2/studio/sessions` and `/v2/studio/sessions/{id}` carry
  `ai_interaction_notice` = "You are interacting with an AI system. Its
  answers are AI-generated, not verified, and not investment advice." (EU
  AI Act Art. 50(1); pinned at the top of every session in the UI).
- **Guard hardening (review H1, 2026-10-17) — structure is enforced
  deterministically, the TOPIC by an independent model check (still a
  model: residual risk, stated in the UI notice).** Limits (`scope_guard`,
  discard reasons in brackets): draft `reply` ≤ 600 chars
  (`draft_reply_too_long`), `assumptions` only with a draft, ≤ 5 × 200
  chars (`assumptions_too_long`, `clarify_with_assumptions`),
  operator-request `purpose` ≤ 200 chars (`operator_request_too_long`),
  clarify reply ≤ 300 chars (`clarify_too_long`), a JSON answer that is not
  an object → `not_an_object` (charge shown, no 500). A final draft that is
  still invalid → `discarded_reason: "draft_invalid"`: reply, assumptions
  and operator requests are dropped, only the validator card (`draft` with
  spec + errors) stays, plus the fixed note "The AI draft did not pass the
  validator — its text is not shown; the validator's findings are below.".
  **Independent topic check** before anything is shown: one extra call
  (Standard model, fixed prompt, temperature 0, max_tokens 120, feature
  `sp-studio-guard`, label "Hedge Fund · Studio topic check", ideation
  budget) that sees ONLY the AI's displayable texts (reply, assumptions,
  operator-request name/purpose/signature, the spec's name, description,
  tags, parameter descriptions) as quoted data — never the user's text —
  and answers `{on_topic, reason}`. `on_topic: false`, bad JSON, an error,
  a timeout or a budget block → everything displayable is discarded
  (`discarded_reason: "topic_check_failed"`, `draft: null`, the spec is NOT
  saved or backtested) and the fixed scope note is shown. Message field
  `topic_check: "passed"|"failed"|"skipped"` (skipped = nothing
  displayable; the guard's own reason is stored for the audit only). The
  guard call's cost is part of the message's `cost_usd` / `usage.calls`.
  The same check runs on paper extractions (§14.7) and committee role texts.
- **Budget reservation (review L4):** a call reserves its estimate under one
  process-wide lock before it is sent (Studio, paper and committee calls
  share it) and settles afterwards, so parallel calls cannot overshoot the
  month/day limits.
- Tests: `tests/test_sp_studio.py`.


### 13.6 5a WPF notes (XW, 2026-10-17: Strategy Studio — §13.3, bound to §13.5)

- **Files**: `Platform/StudioView.cs` (new, code-built UserControl), `Platform/PlatformClient.Studio.cs`
  (new: DTOs + `ListStudioSessionsAsync`, `CreateStudioSessionAsync`, `GetStudioSessionAsync`,
  `PostStudioMessageAsync` (client timeout 210 s = 2 × 90 s LLM + overhead), `CancelStudioDraftAsync`,
  `Get/PutIdeationSettingsAsync`), `HedgeFundWindow.Studio.cs` (new: rail wiring, "Open in Library"),
  `HedgeFundWindow.IdeationBudget.cs` (new: settings), `HedgeFundWindow.xaml(.cs)` (rail entry "Studio"
  between Library and Research, `StudioHost`, ideation inputs below the explanation budget, 30-s refresh in
  the state poll, `Shutdown` on close), `Platform/LibraryView.xaml.cs` (`ShowStrategyAsync(id)`).
  Not used by the UI (yet): `GET /v2/studio/operator-requests`, `GET /v2/memory` (the chat shows each
  message's `operator_requests` / `memory_used`).
- **Layout**: left = sessions (newest first, "+ New session" with optional title ≤ 120); right = header
  (title, session/strategy/family ids, family trial count, "Open in Library"), budget line
  (`StudioJson.BudgetLine`: month + day spent of budget/cap, "off", `paused_reason`), chat, composer
  (TextBox ≤ 4,000 chars with counter, Standard/Premium radios with `estimate_usd` of GET session — no
  number without one, "✦ Send", Ctrl+Enter, "Writing a draft… n s" while the synchronous POST runs; session
  list disabled meanwhile). After a successful POST the session is reloaded (GET) — the reply is not
  rendered from the POST payload; a failed POST shows the backend's `error` verbatim and keeps the text.
- **Messages** (plain TextBlocks only): user bubble; assistant = violet AI container, header
  "✦ AI-generated · <provenance.model> · <provenance.generated_at|created_at>", cost line (tier, cost_usd,
  usage.calls > 1 → "incl. one repair round", usage.estimate_usd), `AiContent.Caveat`, screen-reader name
  `AiContent.AutomationName`; `system_note` = italic "Backend note" (no AI label).
- **Draft card**: valid/invalid chip, version + strategy id, repair round / `repair_skipped`, validator
  errors + warnings (path, line/col, code, message), `repair_errors` expander, `unchanged` note, "Spec changes
  vs v<n>" = client-side flattened JSON path diff (+ / − / ~, ≤ 60 lines) of `draft.spec` against the previous
  valid, changed draft of the session (first one: "no previous version"), "Full spec (JSON)" expander, family
  trial count (GET session `trial_count`), backtest status (`queue` entry wins while waiting — position +
  Cancel → `…/drafts/{message_id}/cancel`; started → `runs[run_id].status` else `run_status`, active → Cancel
  backtest (`/v2/backtests/{id}/cancel`, confirm), done → mini metrics from `runs[].key_metrics` else
  `draft.backtest.key_metrics`; cancelled / start_failed / not_queued → the backend `message` verbatim),
  "Open report" (BacktestReportWindow) and "Open in Library". Operator requests = amber box "Operator
  requests — unverified AI suggestion (text only, never executed)"; assumptions list; "Memory used for this
  draft (n)" expander (kind, family, run, text). Polling: GET session every 3 s only while a draft is waiting
  or its run is active AND the view is visible; a poll re-renders only the backtest blocks (full re-render
  when the message count changed).
- **Settings**: "AI ideation (Studio) — monthly budget (USD)" + "— daily cap (USD)"; empty/disabled until
  GET answers, PUT of BOTH values 600 ms after the last valid edit; hint = budget line or the backend's
  error verbatim (503 keeps the inputs enabled as the repair path); range from `max_*` (SPEC 200 only as
  the client format check when absent).
- **Review fixes (2026-10-17)**: M1 — a valid draft with `requires_confirmation` (and no version) shows the chip
  "unsaved — needs your confirmation" + "valid", the backend text ("demotes <state> to draft"), no backtest, and
  "Save as new version…" → confirmation dialog (new version demotes the strategy to draft, earlier validation no
  longer applies, the automatic backtest counts as another trial) → `POST …/drafts/{message_id}/save {confirm:
  true}` (`SaveStudioDraftAsync`) → session reloaded; errors (409 not_confirmable / stale) verbatim; never
  auto-saved. `saved_by_confirmation` → "Saved after your confirmation". L5 — `valid && not_saved` → chip "not
  saved" (+ "valid") and "Not saved: <not_saved_reason>" instead of "invalid". L7 — the diff compares against the
  STORED version its label names: base = the version itself for `unchanged`, else `base_version`, else (stored v
  without a base) v−1; the new side = the stored v when the draft has its own version, else the proposed spec;
  both via `GET /v2/strategies/{id}/versions/{n}` when the expander is first opened (cached per strategy@version,
  failures not cached, shown verbatim). Header "Spec changes: v<new>|proposed spec vs stored v<base> (k)".
- **Scope / AI-interaction fix (user, 2026-10-17)**: `ai_interaction_notice` (GET sessions + GET session) is pinned
  as a neutral, non-dismissible block at the top of the session view AND above the composer (collapsed only
  until the backend sent it — no stand-in text); the composer shows the placeholder "Describe a trading idea —
  market, signal, rebalancing…". An assistant message with `discarded: true` is never rendered as AI content:
  a neutral "System note" says "AI reply discarded (<reason>) — not shown · <cost line>"; the backend's fixed
  scope note (`system_note`) follows as a neutral system note (no ✦ label, no AI colours). `kind: "clarify"`
  replies are normal AI bubbles with "· clarifying question" in the AI header. DTO: message `kind`,
  `discarded`, `discarded_reason`; session list/detail `ai_interaction_notice`.
- **Guard review (2026-10-17)**: `draft_invalid` → the neutral "AI reply discarded (the draft did not pass the
  validator)" note + ONLY the validator card (no reply, assumptions or operator requests; labelled "Validator
  findings on the spec the AI proposed"); `topic_check_failed` and every other reason → the note only (no draft
  card). Message `topic_check` (passed|failed|skipped) is shown in the cost line.
- Static check only (no dotnet on the VM): braces balanced, XAML parses, every new identifier defined once.

## 14. Stage 5b — paper → strategy + investment committee (contract, 2026-10-17)

Interview decisions (user, 2026-10-17): papers come in by **PDF upload,
pasted text, or a link to arXiv or SSRN only** (no other sites);
**replicated** = our backtest over the paper's period hits the paper's
reported Sharpe within **±30 %** and with the same sign, otherwise "not
replicated" with both numbers side by side; the **committee runs
automatically after a finished validation when enabled in Settings**
(default off); when the setting is off, a **"Convene committee…" button**
is shown instead (only then). Budget = the ideation budget of §13 ($10/month,
$2/day). The memo is advisory only — it never changes a strategy's state.

### 14.1 Paper ingestion (`research/papers.py`)
- Inputs: PDF upload (≤ 20 MB, ≤ 150 pages), pasted text (≤ 200,000 chars),
  link: `https://arxiv.org/abs/<id>` | `https://arxiv.org/pdf/<id>` |
  `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=<n>` and the SSRN
  delivery PDF the abstract page links to — exact host allow-list
  (arxiv.org, export.arxiv.org, papers.ssrn.com), https only, redirects only
  within the allow-list, SSRF-hardened fetch (resolve → reject private /
  loopback / link-local → connect to the checked IP with SNI/cert for the
  hostname — same rules as market_research/news media fetch; hard 30 s
  deadline, size cap 20 MB, content-type allow-list pdf/html for the
  abstract page), through http_gate (own source "papers", conservative
  limit). SSRN often requires a login/captcha → clear message "SSRN did
  not provide the PDF without login — download it in the browser and upload
  it", never a scraping workaround.
- PDF → text in a separate process with limits (time 60 s, memory guard),
  pure-Python extractor added to requirements (pinned); no JavaScript, no
  embedded files, no OCR. The paper text is UNTRUSTED (prompt injection):
  passed to the LLM only as quoted data with the instruction that it is
  data; never shown as markup.
- Stored: `papers(id, source kind, url/filename, sha256, title, authors,
  year, text_chars, created_at)` + extracted text in a file under
  backend/data/papers/<sha256>.txt (not the PDF itself — copyright: keep the
  user's upload only as long as needed; delete the temporary PDF after
  extraction... renamed/removed by the backend's own temp handling).

### 14.2 Paper → strategy + replication (`research/paper_strategy.py`)
- LLM extraction (feature `sp-paper`, ideation budget) → JSON {hypothesis,
  universe, signal construction, rebalancing, holding period, sample period
  (start/end), reported results {sharpe, cagr?, max_dd?, net/gross,
  costs assumed}, caveats}, each field with the page/quote snippet it came
  from (≤ 300 chars) → **shown to the user for confirmation/edit before
  anything runs** (the reported Sharpe and period are user-confirmed
  numbers, not trusted AI output).
- Then a Studio-like draft (DSL only, operator requests as text) → validator
  → strategy `origin="paper"` with provenance {paper id, sha256, title} →
  **replication backtest over the paper's period** (clamped to our data;
  the clamp is shown; holdout window excluded as always) → replication
  score: replicated iff our Sharpe within ±30 % of the reported one and
  same sign; report shows both numbers, period used vs paper period,
  gross/net mismatch notes, universe differences (survivorship warning).
  The replication run is a normal finished run → counts as a trial.

### 14.3 Investment committee (`research/committee.py`)
- Five roles in order: Researcher (thesis), Quant (implementation vs
  thesis, data issues), Risk Manager (stress, drawdowns, sizing), Reviewer /
  Devil's Advocate (overfitting, weak gates, regime dependence), Chair
  (memo). Input per role: the strategy logic, the latest validation report's
  gate facts, the §11 attribution facts, memory facts (§13.2), previous
  roles' outputs. NEVER holdout results unless the holdout was already
  opened for this exact version (then included and labelled).
- Memo JSON {summary, arguments_for[], arguments_against[], confidence
  0–1 with reason, conditional sizing (as % of capital, rules), stop rules,
  open questions}; numbers checked with the §11.2 number check against the
  facts (unverified marked). EU AI Act label per role output + cost line.
- Model: Standard by default, Premium selectable per run (manual) / in
  Settings (auto). Cost estimate shown before a manual run.
- Trigger: Settings `committee_auto_after_validation` (default false). If
  true → after every finished (non-holdout) validation run, if the ideation
  budget allows; otherwise a note "committee skipped: <reason>" on the
  validation report. If false → the WPF shows "Convene committee…" on the
  validation report and strategy detail (hidden when the setting is on).
- Stored insert-only: `committee_runs`, `committee_messages`; progress
  events per role (polling); cancellable between roles (charged so far).

### 14.4 API (behind api_auth, strict validation, audited where state/cost)
`POST /v2/papers` (multipart PDF | {text} | {url}) → paper;
`GET /v2/papers`, `GET /v2/papers/{id}`;
`POST /v2/papers/{id}/extract` {model} → extraction (unconfirmed);
`POST /v2/papers/{id}/confirm` {edited fields} → draft + replication run;
`GET /v2/papers/{id}/replication`;
`POST /v2/committee` {strategy_id, version?, validation_run_id?, model} →
202 run; `GET /v2/committee/{id}`; `POST /v2/committee/{id}/cancel`;
`GET/PUT /v2/settings/committee` {auto_after_validation, model}.

### 14.5 WPF
- Studio gets a "Paper" tab: upload / paste / link (arXiv/SSRN only, the
  rule shown), extraction review form (fields with source snippets,
  editable, "Confirm & build strategy"), replication card (paper vs ours,
  replicated / not replicated chip, period/clamp notes, "Open report").
- Committee: on the validation report + strategy detail — "Convene
  committee…" (only when auto is off) with model + estimate; progress per
  role; memo view (AI labels, unverified numbers red, plain text); history.
  Settings: "Run the investment committee automatically after each
  validation" + model.

### 14.6 Tests
No network (fakes): host allow-list + redirect rules + SSRF (private IPs,
DNS rebinding) + size/time limits; SSRN login page → message; PDF extractor
limits (huge/odd PDFs, timeout) in the subprocess; injection text stays
quoted; extraction requires confirmation before any run; replication score
rule (±30 %, sign, clamping); committee role order, holdout exclusion,
number check, cancel, budget blocks, auto vs manual trigger; insert-only.

### 14.7 5b backend notes (X2, implemented 2026-10-17: §14.1–§14.4, §14.6 backend)

All routes behind api_auth, strict bodies/queries (400 `{error, errors}`),
other errors `{error, code, …}` (as §11.6/§13.5), never a stack trace. AI
outputs carry `provenance` (EU AI Act label) + `cost_usd`. Budget = the
ideation budget of §13 (402 `budget` / 503 `spend_unrecorded` as §13.5).

**Papers**
- **POST `/v2/papers`** — one of: `multipart/form-data` with ONE part
  `file` (PDF, ≤ 20 MB, ≤ 150 pages); JSON `{text: str 1..200,000, title?:
  str ≤ 300}`; JSON `{url: "https://arxiv.org/abs/<id>" |
  "https://arxiv.org/pdf/<id>" | "https://papers.ssrn.com/sol3/papers.cfm?
  abstract_id=<n>"}` → 200 `paper`. Errors: 400 `url_not_allowed` ("Only
  arXiv and SSRN links are accepted (https://arxiv.org/abs/…,
  https://arxiv.org/pdf/…, https://papers.ssrn.com/sol3/papers.cfm?
  abstract_id=…)."), 400 `pdf_invalid` (not a PDF / encrypted / too many
  pages / no text — reason in `error`), 413-style 400 `too_large`, 502
  `fetch_failed` (reason), 502 `ssrn_login` ("SSRN did not provide the PDF
  without login — download it in the browser and upload it"), 400
  `fetch_rejected` (private address, redirect outside the allow-list, wrong
  content type, size), 504 `extract_timeout` ("The PDF text extraction took
  longer than 60 s."). The same sha256 again → the existing paper (200,
  `duplicate: true`).
- `paper` = `{id: "p-<12 hex>", source_kind: "pdf"|"text"|"arxiv"|"ssrn",
  url: str|null, filename: str|null, sha256, title: str|null, authors:
  str|null, year: int|null, text_chars: int, pages: int|null,
  created_at, duplicate: bool, status: "new"|"extracted"|"not_relevant"|
  "confirmed", latest_extraction_id: int|null, strategy_id: str|null}`.
  Title/authors/year come from the PDF metadata / arXiv / the user's
  `title` (untrusted plain text).
- **GET `/v2/papers`** `?limit=1..200 (50)` → `{papers: [paper]}` (newest
  first); **GET `/v2/papers/{id}`** → `{paper, extractions: [extraction]
  (newest first), confirmation: confirmation|null, replication:
  replication|null, text_preview: str (first 2,000 chars, plain text)}`.
- **POST `/v2/papers/{id}/extract`** `{model: "standard"|"premium"}` → 200
  `extraction` (unconfirmed; nothing runs). `extraction` = `{id, paper_id,
  created_at, model, model_tier, relevant: bool, not_relevant_reason:
  str|null, fields: {hypothesis, universe, signal, rebalancing,
  holding_period: {value: str|null, source: str|null (≤ 300-char quote /
  page ref)}, sample_period: {start: "YYYY-MM-DD"|null, end|null, source},
  reported: {sharpe: number|null, cagr: number|null, max_dd: number|null,
  basis: "net"|"gross"|"unknown", costs_assumed: str|null, source},
  caveats: [str] (≤ 8)}|null, note: str|null, provenance, usage, cost_usd,
  text_used_chars, text_truncated: bool}`. `relevant: false` (the text is not
  about a testable trading/investment strategy) → `fields: null`, `note` =
  the fixed non-AI text "This text does not describe a testable trading or
  investment strategy, so the Studio cannot build a strategy from it."; the
  AI reason is in `not_relevant_reason`… shown only as "Reason given by the
  AI" (plain text).
- **POST `/v2/papers/{id}/confirm`** `{extraction_id: int, model:
  "standard"|"premium", sample_start: "YYYY-MM-DD", sample_end:
  "YYYY-MM-DD", reported_sharpe: number, reported_basis?:
  "net"|"gross"|"unknown", hypothesis?, universe?, signal?, rebalancing?,
  holding_period?: str ≤ 2,000}` (the user-confirmed values; the reported
  Sharpe and the period are REQUIRED here) → 200 `{confirmation, draft
  (§13.5 draft shape), replication}`; the draft call uses the Studio rules
  (kind/scope guard, validator, one repair round). Errors: 409
  `not_relevant`, 409 `already_confirmed`, 409 `period_outside_data`
  ("The paper's period lies outside the data we can backtest (…)."), 404.
  `confirmation` = `{id, paper_id, extraction_id, created_at, confirmed:
  {…the body fields…}, strategy_id|null, version|null}`.
- **GET `/v2/papers/{id}/replication`** → `replication` = `{state:
  "none"|"waiting"|"started"|"start_failed", run_id, run_status,
  paper_period: {start, end}, period_used: {start, end}|null, clamped:
  bool, clamp_notes: [str], reported_sharpe, our_sharpe: number|null,
  tolerance: 0.30, replicated: true|false|null (null until the run is
  done), verdict: "replicated"|"not replicated"|"pending"|"failed",
  notes: [str] (gross/net mismatch, universe/survivorship warnings,
  costs), key_metrics|null}`. Replicated ⇔ same sign AND |ours − reported|
  ≤ 0.30 × |reported|. When the job slot is busy the replication waits
  ("Waiting for the backtest slot"; own FIFO, dispatched after the Studio queue when the slot frees).

**Committee**
- **POST `/v2/committee`** `{strategy_id, version?: int, validation_run_id?:
  str, model: "standard"|"premium"}` → 202 `{run}`; 409 `busy` (one
  committee at a time), 409 `no_validation` ("Run a validation first — the
  committee reviews a validated report."), 402/503 budget.
- **GET `/v2/committee/estimate`** `?strategy_id=&version=&validation_run_id=
  &model=` → `{estimate_usd, roles: 5}` (pre-call estimate of all 5 calls).
- **GET `/v2/committee`** `?strategy_id=&validation_run_id=&limit=` →
  `{runs: [run]}` (newest first). **GET `/v2/committee/{id}`** → `{run,
  messages: [committee_message] (role order), memo: memo|null,
  memo_check: check|null}`. **POST `/v2/committee/{id}/cancel`** (no body) →
  `{run}` (stops before the next role; charged so far).
- `run` = `{id: "cr-<12 hex>", strategy_id, version, validation_run_id,
  model_tier, model, trigger: "manual"|"auto", created_at, status:
  "running"|"done"|"failed"|"cancelled"|"skipped"|"interrupted", note:
  str|null (e.g. "committee skipped: <budget message>"), progress: {done:
  int, total: 5, current_role: str|null}, roles: ["researcher", "quant",
  "risk_manager", "reviewer", "chair"], holdout_included: bool, cost_usd}`.
- `committee_message` = `{id, run_id, role, seq, output: {summary: str,
  points: [str]} (roles 1–4) | memo (chair), check: {ok, unverified,
  unknown_ids} (§11.2), provenance, usage, cost_usd, created_at}`.
- `memo` = `{summary, arguments_for: [str], arguments_against: [str],
  confidence: {value: 0..1, reason}, sizing: {percent_of_capital: 0..100,
  rules: [str]}, stop_rules: [str], open_questions: [str]}` — advisory
  only, never changes a strategy's state.
- **GET/PUT `/v2/settings/committee`** `{auto_after_validation: bool,
  model: "standard"|"premium"}` (defaults false / standard; PUT audited
  `committee_settings_changed`). Auto: after every finished non-holdout
  validation run (job after-hook); a budget block → a run with status
  `skipped` and `note` "committee skipped: <reason>".
- **Number-check section keys** (committee, per message): roles 1–4
  `summary`, `points.<i>`; chair (memo) `summary`, `arguments_for.<i>`,
  `arguments_against.<i>`, `confidence.reason`, `sizing.rules.<i>`,
  `stop_rules.<i>`, `open_questions.<i>`; `offset` = character offset in
  that text. `sizing.percent_of_capital` / `confidence.value` are structured
  fields (not checked). Holdout facts (only when the holdout was opened for
  this exact version) get ids `holdout.<original id>` and sit in
  `data.holdout {label, facts}`; otherwise no holdout id reaches a prompt.
- **Implementation**: `research/papers.py` (URL rules `classify_url`,
  `PaperFetcher` — SSRF rules re-implemented locally so the MR fetcher is
  unchanged; gate source `papers`, 6/min, 1 attempt), `research/pdf_worker.py`
  (the subprocess: `python -I pdf_worker.py <pdf> <max pages> <max chars>
  <memory MB>`; RLIMIT_AS on POSIX, a working-set watchdog thread on Windows
  (exit 3); no JavaScript / attachments / OCR), `research/paper_strategy.py`,
  `research/committee.py`; `requirements.txt` pins `pypdf==6.18.0` (pure
  Python). The temporary PDF (`tempfile.mkstemp`, prefix `ufos-paper-`) is
  removed right after the extraction; only `data/papers/<sha256>.txt` +
  the `papers` row remain. Extracted text ≤ 2,000,000 chars; the extraction
  prompt uses the first 60,000 (`text_truncated`). Multipart upload: read in
  64 KB chunks with the 20 MB cap (`routes.read_paper_body`); the JSON body
  limit was raised to 6 × 200,000 + 4,096 bytes for pasted texts.
- **Extraction** (feature `sp-paper`, label "Hedge Fund · Paper import",
  max_tokens 2,500, fixed `EXTRACT_SYSTEM_PROMPT`, paper title/text as quoted
  JSON fields): missing `relevant` or any schema break → 502 `bad_json`
  (cost booked). **Confirm**: draft call through the Studio prompt + rules
  (`prompts.build_messages` with the confirmed fields as the quoted
  `user_request`, `scope_guard`, validator, one capped repair round); an
  off-topic / non-draft answer → `{confirmation: null, draft:
  {discarded_reason}, note: <the Studio scope note>}`; an invalid draft →
  `confirmation: null` (nothing stored, the user may confirm again — costs
  again). Valid → `create_from_paper` (origin `paper`, envelope
  `ai_provenance.paper {id, sha256, title}`) + `paper_confirmations` row +
  replication with explicit `start/end` (clamped: start ≥ the data start
  `seam.EARLIEST_START`, end ≤ `families.training_end`); the Tiingo key
  check of POST /v2/backtests applies (missing key → `start_failed` with its
  message). A paper period ending before the data start → 409
  `period_outside_data` BEFORE any AI call.
- **Committee**: feature `sp-committee`, label "Hedge Fund · Investment
  Committee", max_tokens 1,800, fixed `SYSTEM_PROMPT` + role task; the
  worker thread calls the roles in order, budget-checked per call (the
  first call also before the run is created → 402 without a run). A failed
  call ends the run `failed` (note names the role and the charge);
  `run.cost_usd` = sum of the stored role messages. One committee at a time
  per backend; `interrupted` = no terminal event and no live worker (backend
  restart). Validation runs from before §11 (no attribution) → gate facts
  only (noted in `data.run.note`).
- **Job after-hook** (`backtest/jobs._after_job`, never raises): memory →
  `committee.on_validation_finished` (setting on + kind validation + done) →
  Studio queue → paper replication queue.
- **Storage v13**: `papers`, `paper_extractions`, `paper_confirmations`
  (UNIQUE paper_id), `paper_replications` (events waiting|started|
  start_failed), `committee_runs`, `committee_messages` (UNIQUE run_id+seq),
  `committee_events` (started|role_done|cancel_requested|cancelled|failed|
  done|skipped) — all insert-only; meta `committee_auto_after_validation`
  ("0"/"1"), `committee_model`. Registry: origin `paper` + `create_from_paper`.
- Tests: `test_sp_papers.py` (10; the real-pypdf test is skipped when pypdf
  is missing), `test_sp_paper_strategy.py` (8), `test_sp_committee.py` (8).
- **Review fixes (2026-10-17):** extraction `not_relevant_reason` is NEVER
  returned (always null; kept in storage/audit) — the client gets only the
  fixed `note`. Extraction gains `discarded_reason: null|"topic_check_failed"`:
  the independent topic check (§13.5) sees the displayable fields (values,
  sources, caveats); a failed check → `relevant: false`, `fields: null`, the
  fixed note, paper status `not_relevant`. The confirm draft's spec texts
  are topic-checked before the strategy is saved (failed → `confirmation:
  null`, `draft: {discarded_reason: "topic_check_failed"}`, the Studio scope
  note). Committee: each role's texts are topic-checked; a failed check →
  that message's `output` = `{summary: "This committee contribution was not
  shown: it did not pass the topic check.", points: []}` (chair: the memo
  fields empty, `memo: null`), `check: null`, plus `discarded: true`,
  `discarded_reason: "topic_check_failed"` on the message; the run
  continues. A failed role call now always ends the run with a note naming
  the role and the charge; an auto committee that cannot start (budget,
  missing model, …) → a `skipped` run with "committee skipped: <reason>".


### 14.8 5b WPF notes (XW, 2026-10-17: papers, replication, committee — §14.5, bound to §14.7)

- **Files**: new `Platform/StudioPaperView.cs` (Studio "Paper" tab), `Platform/CommitteePanel.cs`,
  `Platform/PlatformClient.Papers.cs` (DTOs + calls; the only multipart call = POST /v2/papers with ONE part
  `file`, application/pdf; add timeout 120 s, extract 120 s, confirm 210 s), `Platform/PlatformClient.Committee.cs`,
  `HedgeFundWindow.CommitteeSettings.cs`; changed `Platform/StudioView.cs` (tabs "Chat" | "Paper", the paper
  view created on first use; `NoticeBlock` / `SetNotice` / `ApplyBackendNoticeAsync` shared),
  `Platform/ExplainPanel.cs` (`Rich` internal — reused for the committee's unverified numbers),
  `Platform/ValidationReportView.cs` + `Platform/LibraryView.xaml.cs` (CommitteePanel), `HedgeFundWindow.xaml(.cs)`
  (committee setting below the ideation budget, 30-s refresh).
- **Paper tab**: left = "Upload PDF…" (OpenFileDialog; > 20 MB refused client-side with the size, no upload), link
  field with the rule shown (client check `^https://(arxiv.org/(abs|pdf)/…|papers.ssrn.com/sol3/papers.cfm?abstract_id=<n>)$`;
  the backend's allow-list is authoritative — `url_not_allowed`, `ssrn_login`, `fetch_*`, `pdf_invalid`,
  `extract_timeout` verbatim), paste text (≤ 200,000) + optional title (≤ 300); paper list. Right = pinned
  AI-interaction notice (text from GET /v2/studio/sessions?limit=1 — no local text), paper meta (untrusted plain
  text) + text preview, "1 · Extraction (AI)" (AI header + cost line + caveat; Standard/Premium; "Extract with AI" /
  "Extract again (costs again)"; no estimate number — §14.7 offers none for the extraction); `relevant: false` →
  the backend's fixed note as a neutral block + "Reason given by the AI" in an AI box. "2 · Review": every field an
  editable TextBox (≤ 2,000) with "Source in the paper: “…”" below (none → amber "check this value yourself"),
  sample start/end + reported Sharpe REQUIRED (client format check), basis net/gross/unknown, other reported values
  shown read-only; "Confirm & build strategy" (model toggle, confirmation dialog: AI draft + replication backtest =
  a trial) → POST confirm with exactly the confirmed fields (empty text fields omitted); an invalid draft is shown
  after the reload. After confirmation: "Confirmed values" (from `confirmation.confirmed`). "3 · Replication" card:
  verdict chip (replicated / not replicated / pending / failed), "Paper Sharpe X · our Sharpe Y — replicated = same
  sign and within ±<tolerance>", paper period vs period used (+ "clamped", `clamp_notes`), `notes` (gross/net,
  survivorship, costs), waiting / start_failed / running state, key metrics, "Open report", "Open in Library".
  Polled every 3 s (GET …/replication) only while waiting/running and visible.
- **Committee panel** (validation report of a non-holdout validation run with its run id; Library detail of the
  current version): GET /v2/settings/committee decides — auto on → note only; auto off → "✦ Convene committee…"
  (options: Standard/Premium with GET /v2/committee/estimate per tier, "Convene (5 AI calls)", Close); unknown →
  no button + the reason. Runs = GET /v2/committee?strategy_id=&validation_run_id=; `skipped` runs show their `note`
  verbatim (amber); newest running/non-skipped run shown; history expander (newest first). Run view: header
  (status, trigger, version, tier, cost, holdout_included), roles ✓ / ▶ / ○ from `progress`, Cancel (confirm) while
  running (polled every 2 s while loaded); memo view = pinned notice, one AI container per role ("✦ AI-generated ·
  model · time · role" + cost line + caveat), roles 1–4 summary + points, chair = memo (summary, arguments for /
  against, confidence, sizing, stop rules, open questions; `memo_check` else the message check). Unverified
  numbers via `ExplainPanel.Rich` on the field keys `summary`, `points.<i>`, `arguments_for.<i>`,
  `arguments_against.<i>`, `confidence.reason`, `sizing.rules.<i>`, `stop_rules.<i>`, `open_questions.<i>` —
  **§14.7 does not fix the `section` names of the committee check**; entries with another key are listed under the
  role ("Could not be marked in the text") — never dropped. Backend: please confirm/align these keys.
- **Settings**: "Run the investment committee automatically after each validation" (ToggleSwitch) + Standard /
  Premium; disabled until GET answers; a click PUTs both (needs both known — otherwise asks for the missing one).
- **Review fixes (2026-10-17)**: M1 — a not-relevant / `topic_check_failed` extraction shows ONLY the backend's
  fixed `note` as a neutral block plus a neutral cost line ("— no AI text is shown"); no AI box, no AI header
  (`not_relevant_reason` is never displayed). L3 — confirm with `draft.discarded_reason` (or an invalid draft) →
  a neutral "System note — no strategy, no backtest" with the response's fixed `note`; `draft_invalid` (or no
  reason + invalid) adds the validator findings; `topic_check_failed` / other reasons the note only. Committee:
  a role message with `discarded: true` is a neutral "System note · <role> · cost" with the fixed text from
  `output.summary` — no AI label, no number check. DTOs: `StudioDraft.discarded_reason`,
  `PaperConfirmResult.note`, `PaperExtraction.discarded_reason`, `CommitteeMessage.discarded(_reason)`,
  `StudioMessage.topic_check`.

## 15. Stages 6–10 + central AI budget window — interview decisions (user, 2026-10-17)

Binding decisions from the interview of 2026-10-17 (each stage still gets its
own detailed contract section before it is built):

- **B — central AI budget window (MainWindow):** built FIRST. Lives in the
  MainWindow (user explicitly lifts the HedgeFund-only scope rule for this
  window and its wiring); controls ALL AI budgets of ALL modules (HedgeFund:
  fund/Council, explanations, ideation, loop; plus new daily caps for Global
  Monitoring Assess, LiveStreamAgent and the Newsroom analysis) and shows the
  spend from the shared cost log; provides a **documented interface so new
  modules can register their budgets**. Own contract + scope-guard snapshot
  with an explicit allow-list of the files outside HedgeFund it may change.
- **Stage 6 — discovery loop:** default 6 variants × 4 generations (max
  configurable 8 × 5); start from an existing strategy, an idea in words or
  a confirmed paper; own **loop AI budget** (set in the budget window);
  switch "fully autonomous" (loop also validates its finalists, G1–G8) —
  when off, finalists are proposed for the user's approval; the holdout is
  ALWAYS opened only by the user; every backtest counts as a trial.
- **Stage 7 — portfolio, risk, OMS (paper):** allocator = inverse
  volatility with a correlation penalty; **incubation length chosen per
  strategy before each incubation run, then immutable**; default hard limits
  2 % daily loss (no new orders until next day) and 10 % drawdown from peak
  (kill switch: close all, autopilot off) — adjustable; netting; pre-/post-
  trade checks; OMS state machine; migration of the old paper engine.
- **Stage 8 — brokers:** Alpaca paper (stocks/ETFs) and **Kraken via ccxt
  (demo futures)** for crypto; IBKR/MT5 NOT built, but the broker adapter
  interface stays open for them (TODO item at the end); reconciliation halts
  on mismatch; **live guard built (4 conditions + capital cap) but live
  stays OFF**; stage 12 only on the user's explicit go.
- **Stage 9 — monitoring & Shadow Account:** decay → strategy paused (no
  new orders, positions kept) + user decides; alerts = Windows toasts;
  ~~yearly German tax CSV (Anlage KAP basis, not tax advice)~~ **SUPERSEDED 2026-10-18 by the user: NO tax export
  — a fixed notice points to the brokers' annual tax documents (§19.15)**; Shadow Account
  imports: generic column mapping + templates for Trade Republic, Scalable
  Capital and IBKR Flex; data stays local.
- **Stage 10 — cleanup & hardening:** the Council of AIs is migrated into
  the new platform as a strategy (parity test against the old engine), then
  a SAFE and CAREFUL cleanup of old code/structures (deletion list approved
  by the user; rename to *.stale first); contract of the migration: §22; security & compliance audit of the
  **whole app** (all modules, MainWindow, Shared); operating guide.
- **Process:** B, then stages 6–9 in one go (each: contract → implement →
  independent review → fix → docs; short report after each stage); HALT
  before stage 10 for the user's click-tests and the deletion approval.

## 16. Stage 6 — discovery loop (contract, 2026-10-17)

Decisions: §15 (user, 2026-10-17) — default 6 variants × 4 generations (max
8 × 5); seed = existing strategy, idea text or confirmed paper; own **loop
AI budget**; switch **"fully autonomous"** (the loop validates its
finalists G1–G8 itself; off = finalists are proposed for the user's
approval); the **holdout is ALWAYS opened only by the user** (the loop never
opens it, never reads holdout runs or facts); **every backtest counts as a
trial**. Plan: project doc "L5 Validation & discovery loop" (population N ×
G generations → backtest → score → LLM diagnosis → refinement (LLM edits
the spec + bounded deterministic parameter search) → selection; budgets;
trial counting; resumable; live progress). Values marked **(confirmed by the user
2026-10-24)** were chosen by the implementing agent while the user was away and confirmed by the user on
2026-10-24.

### 16.1 Run model (`discovery/loop.py`)

- **One loop at a time per backend** (second start → 409 `busy`). A loop
  runs in its own daemon thread (the orchestrator); it never runs a backtest
  itself — every backtest/validation is a normal job on the ONE job slot
  (§8.5/§9.9, `JobManager.submit_work`).
- **Config** (`POST /v2/loops`): `seed` = `{kind: "strategy", strategy_id,
  version?}` | `{kind: "idea", text ≤ 4,000}` | `{kind: "paper", paper_id}`
  (the paper must be confirmed — §14.7 — and have a strategy);
  `variants` N 2..8 (default 6), `generations` G 1..5 (default 4),
  `search_points` P 0..3 (default 1, **confirmed by the user 2026-10-24**), `model`
  standard|premium (default standard), `history_from` "2016"|"2000"
  (default "2016"), `auto_resume` bool (default true). The "fully
  autonomous" switch and the budget come from Settings and are
  SNAPSHOT into the loop at its start (shown on the loop).
- **Family:** every variant lives in the SEED's strategy family, so all its
  backtests are trials of that family (no family bypass): variants are new
  strategies created like a duplicate (`parent` = the variant they derive
  from → `_family_for` follows the parent link; origin **`loop`**, state
  draft, envelope `ai_generated: true` + `ai_provenance {feature
  "sp-loop", model, model_tier, prompt_hash, loop_id, generation,
  variant_id}`). An **idea** seed first gets ONE Studio-style draft call
  (Studio prompt, `kind` + scope guard, validator, ≤ 1 repair round, topic
  check); its valid spec becomes the root strategy (origin `loop`, own new
  family). Invalid / off topic → the loop ends `failed` with the reason
  (no stand-in idea). A **strategy** seed may be any strategy incl. a
  library entry (variants are copies). A **paper** seed = the paper's
  confirmed strategy (current version).
- **Variant** = one (strategy_id, version) slot in a generation:
  `{id "lv-<12 hex>", generation 1..G, slot 1..N, origin seed|mutation|
  refined|child, parent_variant_id, strategy_id, versions: [{version,
  kind base|search, run_id, score}], chosen_version, state …}`. A refined
  variant is a NEW VERSION of its parent's strategy (lineage); a child of
  an elite is a NEW STRATEGY (duplicate-style, parent = the elite); search
  points (16.4) are further versions of the variant's strategy.
- **Generation 1:** slot 1 = the seed itself (its existing done backtest of
  that version is reused when the card is kind `backtest`, request all
  defaults and `range.end` = the current training end — no new trial;
  otherwise it is backtested); slots 2..N = **mutations** of the seed (one
  refinement call each, told which aspect to vary and given the specs
  already proposed in this generation — diversity).
- **Generation g ≥ 2:** built from the selection of g−1 (16.3): elites are
  carried over unchanged (no new backtest, same score); every "refine"
  variant gets diagnosis + refinement (a new version); every culled slot is
  refilled with a child of an elite (round-robin, best elite first:
  diagnosis of the elite + refinement into a new strategy).
- **Backtests:** request `{history_from}` (all other fields engine
  defaults — the range ends before the holdout window as every normal
  backtest, §9.1), `auto_resume` from the loop config. Every finished
  backtest is a trial of the family (existing counting, §9.2/§10.5).
- **Finalists** (after the last generation's selection): the best ≤ k
  variants (k as 16.3) that are not duplicates, have score > 0 and pass all
  pre-checks (16.2). None → loop `done` with "No finalist: no variant
  passed the pre-checks." (planted noise must end here). Each finalist is
  **promoted**: when its chosen version is not the strategy's current
  version, the chosen spec is added as a new version (no backtest — same
  logic hash as the evaluated version; noted).
- **Autonomous ON:** the loop submits a validation run (§9.3, G1–G8, NO
  holdout) for each finalist in order (same slot rules), records
  `{run_id, passed (merged)}`; the loop ends `done` when they finished.
  **Autonomous OFF:** finalists are `proposed`; the user approves
  (→ the loop submits the validation when the slot is free, even after the
  loop is `done`) or rejects (reason optional) each one. In both modes the
  loop NEVER calls the holdout path (`submit_holdout`), never changes a
  lifecycle state (variants stay drafts; the validation card is evidence
  for the user's own transitions, §9.5).
- **Statuses:** `running`, `waiting_slot`, `waiting_connection`, `paused`
  (by the user), `paused_budget` (day/month loop budget reached — resume
  after raising it / next day), `interrupted` (backend stopped), `done`,
  `failed`, `cancelled`. Terminal: done, failed, cancelled.
- **Resumable:** every completed step is persisted insert-only (16.7)
  BEFORE the next starts; the orchestrator is re-entrant and rebuilds its
  position from the stored events (a stored LLM answer, variant or score is
  never recomputed). Backend restart → `recover()` at startup marks
  running loops `interrupted`; with `auto_resume` they continue after
  startup automatically, else `POST …/resume`. A loop backtest that failed
  with a connection error (after the job's own auto-resume, §10.3) → loop
  status `waiting_connection` (probe ≥ 30 s via `data.network.probe`, no
  time limit, cancellable), then the run is resumed (`jobs.resume` when
  `resumable`, else a fresh submit — failed cards are never trials). An
  LLM call cut off by a crash is repeated on resume (costs again; its
  booked spend stays booked).
- **Cancel** (`POST …/cancel`): stops after the current step; a running
  loop backtest/validation is cancelled via `JobManager.cancel`; an LLM
  call in flight finishes (charged), its result is stored, then stop.
  **Pause** / **Resume** similarly.

### 16.2 Scoring (`discovery/scoring.py`, deterministic, no AI)

From the variant's done run card ONLY (`metrics` + `attribution`):
`score = sharpe − p_turnover − p_drawdown − p_costs − 0.25 × failed_checks`
- `sharpe` = `metrics.performance.sharpe` (net of the engine's costs);
  None → `score: null` + reason (culled).
- `p_turnover = min(1, 0.01 × max(0, turnover_annual − 12))`
  (`metrics.exposure.turnover_annual`, one-sided book turnover/yr;
  unavailable → 0 with a note).
- `p_drawdown = 2 × max(0, max_drawdown − 0.20)` (fraction).
- `p_costs = min(1, 0.5 × max(0, cost_share − 0.5))`, cost_share =
  `metrics.costs.pct_of_gross_pnl / 100` (only when gross P&L > 0).
- **Pre-checks** (gate-like, run-card only — NOT the gates; thresholds from
  the current gate settings §9.9): `trades` closed trades ≥ `g7_min_trades`;
  `signal` cross-sectional: best naive IC t-stat of horizons 1/5/10/21 ≥
  `g1_ic_t` (time-series: positive window share > 0.5); `alpha` annual
  alpha > 0; `capacity` estimate ≥ `g8_capacity_multiple` × capital
  (crypto-only: n/a = pass, as G8). A value that cannot be computed →
  the check fails with the reason.
- Constants in one place (**confirmed by the user 2026-10-24**). Holdout guard: the
  card must be kind `backtest` with `range.holdout.access` false and
  `range.end` < `range.holdout.start`, else `HoldoutDataRefused` (the loop
  fails — never happens on the normal path; tested).
- **Projected G4 impact** (shown on the loop): `trials_now` = family trial
  count, `planned_backtests` = remaining planned backtests of this loop,
  `sr0_now` / `sr0_projected` = `stats.expected_max_sharpe(n, V)` with
  V = sample variance of the family's trial Sharpes floored at V_min (as
  §9.9, sample = 1 year when unknown) — "the deflated-Sharpe gate will
  compare against an expected maximum Sharpe of X now → Y after this loop".

### 16.3 Selection + diversity (`discovery/selection.py`)

- Rank: score desc, Sharpe desc, slot asc; null scores last.
- **Diversity:** (a) a spec whose `db.logic_hash_of` equals any spec
  already evaluated in this loop is refused BEFORE its backtest (state
  `duplicate`, no trial); (b) daily-return correlation ≥ 0.95 (common
  valued days ≥ 60, from the run cards' equity curves) with a better-ranked
  variant of the same population → `duplicate` (not eligible as elite or
  finalist, culled).
- k = max(1, round(N/3)) elites (best non-duplicates with a score), c =
  max(1, ⌊N/3⌋) culled (failed / unscorable / duplicates first, then the
  worst); the rest are refined. N = 2 → 1 elite, 1 culled.

### 16.4 Bounded deterministic parameter search (`discovery/search.py`)

For every NEW LLM variant (mutation, refined, child): up to P extra
configurations: parameters used by the spec that have min/max/step
(`validation.grid` rules: integer rounding, unused parameters fixed), in
name order, candidates value − step, value + step (clamped, ≠ value), taken
in that order, skipping logic-hash duplicates; each = a new version + a
backtest (= a trial). The variant's `chosen_version` = best score (tie →
the base). Max backtests per loop: 1 + (N−1)(1+P) + (G−1)(N−k)(1+P),
hard cap 120.

### 16.5 LLM roles (`discovery/loop.py`, feature ids for the cost ledger)

- **diagnosis** (`sp-loop-diagnosis`, label "Hedge Fund · Discovery loop"):
  reads computed facts ONLY — the score card (16.2), key metrics, the §11
  fact table of the run (holdout ids filtered, ≤ 60 facts), the family's
  memory facts (§13.2) — never the spec text of other families, never
  holdout data. Output `{summary ≤ 600, weaknesses ≤ 5 × 200, ideas ≤ 3 ×
  200}`; numbers checked with the §11.2 number check (`check`, unverified
  shown, never removed).
- **refinement** (`sp-loop`): the Studio answer schema (`kind` draft only;
  clarify/off_topic → discarded), Studio scope guard, DSL/schema reference
  (single source of truth), the parent spec, its diagnosis and score card,
  diversity context (sibling specs' operators/parameters), memory; output
  spec → the Studio validator path (`registry.validate_spec`, ≤ 1 repair
  round), operator requests stored as text ("unverified AI suggestion",
  never executed). Temperature 0.4, max_tokens 4,000, 90 s.
- **Topic guard:** ONE independent topic check (§13.5, `topic_guard`) per
  variant over every displayable AI text (diagnosis summary/weaknesses/
  ideas, refinement reply/assumptions/operator requests, spec name/
  description/tags/parameter descriptions); failed → all texts discarded
  (`discarded_reason "topic_check_failed"`), the variant is `failed`
  (no backtest).
- Every AI text carries `provenance` (EU AI Act Art. 50 label) + cost;
  the loop payload has `ai_interaction_notice`.
- A failed / blocked / invalid AI step leaves that slot `failed` with the
  reason — no stand-in variant, no cheaper model, no retry with other text.

### 16.6 Loop AI budget (`discovery/budget.py`, key `loop`)

- Own budget, separate from fund / explanations / ideation: `monthly_usd`
  (default 10), `daily_usd` (default 2), `per_run_usd` (default 1)
  (**confirmed by the user 2026-10-24**), 0 = off, max 200. Checked BEFORE every
  call (all three; estimate as §11; in-flight estimates reserved),
  failed attempted calls charged, a failed spend write pauses the loop
  until restart (fail closed). Day/month reached → `paused_budget`;
  per-run cap reached → the loop stops creating variants, finishes the
  pending backtests and the selection, and ends `done` with the note
  "stopped early: per-run AI cap reached".
- Meta keys `loop_monthly_budget_usd`, `loop_daily_budget_usd`,
  `loop_run_cap_usd`, spend `loop_spend_month_usd:<YYYY-MM>`,
  `loop_spend_day_usd:<YYYY-MM-DD>`, `loop_spend_run_usd:<loop_id>` (booked
  in one transaction). `GET/PUT /v2/settings/loop` (budget + autonomous).
- **Central budget window (§15 B):** `budget.descriptor()` /
  `budget.apply(values)` and the hooks `ai_budget_entries` / `ai_budget_put`
  (`UFOS.ai/docs/ai-budgets-spec.md` §4.4, keys `loop`, `loop_month`,
  `loop_run`) expose the loop budget to the central `GET/PUT /ai/budgets`
  (details §16.12).

### 16.7 Storage (next free version) — insert-only

`loop_runs` (config, seed, created_at), `loop_variants` (one row
per generation slot; versions, run ids and scores come from events), `loop_events` (append-only log: every state
change, step result, score, selection, finalist decision, budget block,
pause/resume/cancel/interrupt, with JSON data), `loop_ai_messages` (role
diagnosis|refinement|seed, output, check, provenance, usage, cost,
discarded) — all insert-only by triggers; state = fold over the events.

### 16.8 Single-job slot sharing

- The loop submits ONE job at a time and only when (a) the slot is free,
  (b) no Studio draft or paper replication is waiting (their FIFO queues
  get a free slot first — `_after_job` dispatches them before the loop),
  and (c) ≥ `YIELD_SECONDS` (5 s) passed since its previous job ended —
  the window in which a manual Backtest / Validate click takes the slot.
- A manual request while a loop job runs → the existing 409 (body adds
  `loop_id` when the active job belongs to a loop). The loop shows
  `waiting_slot` while someone else's job runs. Pause frees the slot after
  the current job.

### 16.9 Memory

Every loop backtest/validation writes its memory facts through the
existing finish hook (§13.2). At the end the loop writes one deterministic
memory fact `loop_result` (counts, best score/Sharpe, finalists, cull
reasons) for the family (run_id = the loop id). Loop prompts use
`memory.select_for_prompt` (no holdout facts).

### 16.10 API (behind api_auth, strict validation, audited where state/cost)

`POST /v2/loops` → 202 loop; `GET /v2/loops?limit=`; `GET /v2/loops/{id}`
(`?events_after=` for polling); `POST /v2/loops/{id}/cancel|pause|resume`;
`POST /v2/loops/{id}/finalists/{variant_id}/approve|reject`;
`GET /v2/loops/estimate?…` (pre-start estimate: backtests, AI calls, USD,
projected trials/G4); `GET/PUT /v2/settings/loop`. Exact JSON in 16.x.

### 16.11 Tests (`tests/test_sp_loop*.py`, fake LLM transport, synthetic data)

Planted signal found + planted noise rejected (acceptance), budget blocks
(day/month/per-run, 0 = off), resume after interruption (no recomputed
step, no double trial), cancel, trial counting (every backtest one trial,
reused seed card none), diversity (logic-hash duplicate never backtested,
correlated duplicate culled), autonomous vs approval mode, no holdout data
(no holdout call, holdout-guarded scoring), topic guard on displayable
texts, route validation/auth.

### 16.12 Stage 6 backend notes (implemented 2026-10-17: §16.1–§16.11, backend part — for the WPF "Loop" view)

All routes behind api_auth; strict bodies/queries (unknown fields / parameters → 400 `{error: "invalid input",
errors: {field: message}}`); other errors `{error, code, …}` (as §11.6/§13.5), never a stack trace. Times UTC
ISO seconds. Every AI text carries `provenance` (EU AI Act Art. 50 label) + `cost_usd`; payloads carry
`ai_interaction_notice` (pin it at the top of the view).

- **Files**: `discovery/{__init__, loop.py (orchestrator, state fold, API functions), scoring.py, selection.py,
  search.py, budget.py, prompts.py (Diagnostician + Refiner prompts)}`; `research/loop.py` (hook module named by
  `ai_budgets.PROVIDER_MODULES`, re-exports `discovery.budget.ai_budget_entries/ai_budget_put`, no logic);
  `lifecycle/registry.py` (origin `loop`, `create_from_loop(spec, ai_provenance, parent)`,
  `new_loop_version(...)` — only for origin-`loop` strategies); `storage/db.py` (**v14**); `api/routes.py`
  (11 routes; busy 409 bodies of /v2/backtests, /v2/validations, holdout and resume add `loop_id` when the
  slot is held by a loop job); `backtest/jobs.py` (`_after_job` → `discovery.loop.on_slot_free()` after the
  Studio and paper queues); `research/memory.py` (kind `loop_result`, prompt run kind `loop`); `main.py`
  (`discovery.loop.recover()` at startup after the Studio dispatch).
- **Storage v14** (insert-only by triggers; generic helpers `db.s5b_insert/s5b_select`): `loop_runs(id "lr-<12
  hex>", created_at, seed_kind strategy|idea|paper, seed_json, config_json)`, `loop_variants(id "lv-<12 hex>",
  loop_id, generation, slot, origin seed|mutation|refined|child|elite, parent_variant_id, created_at,
  UNIQUE(loop_id, generation, slot))`, `loop_events(id, loop_id, event, generation, variant_id, data_json,
  created_at)`, `loop_ai_messages(id, loop_id, variant_id, role seed|diagnosis|refinement, output_json,
  check_json, provenance_json, usage_json, cost_usd, created_at)`. Meta: `loop_monthly_budget_usd`,
  `loop_daily_budget_usd`, `loop_run_cap_usd`, `loop_autonomous` ("0"/"1"), spend
  `loop_spend_month_usd:<YYYY-MM>`, `loop_spend_day_usd:<YYYY-MM-DD>`, `loop_spend_run_usd:<loop id>` (one
  transaction, `db.loop_spend_get/add`).
- **Implementation choices** (confirmed by the user 2026-10-24): the loop never adds versions to the user's strategies —
  refining the seed creates a new strategy (parent link → same family); a refined loop variant is a new version
  of its own loop strategy; a child of an elite is a new strategy. The AI must keep the seed's universe
  (else the variant fails "The loop keeps the seed's universe …"). Strategy names get the suffix
  ` (loop <6 hex> g<G>·<slot>)`. Mutation aspects cycle through 6 fixed texts (`prompts.MUTATION_ASPECTS`).
  Search default P = 1. The seed's existing backtest is reused when it is a done `backtest` card of the
  current engine version with no explicit range, benchmark / capital absent or equal to the defaults, the loop's
  history setting and `range.end` = today's training end — stress scenarios on it do not matter (no new trial;
  widened by the click-test fixes 2026-10-22). The topic check calls use the
  Studio guard's feature id `sp-studio-guard` in the cost log but are charged to the LOOP budget. Budget limits
  are re-read before every call (changes apply immediately). A crash between an LLM call and its storage
  repeats the call on resume (booked spend stays booked); a stored step is never recomputed.
- **Loop statuses** (`status`): persisted `running | paused | paused_budget | interrupted | done | failed |
  cancelled` (`persisted_status`), plus transient `waiting_slot | waiting_connection` while the orchestrator
  waits (only in `status`). `can: {cancel, pause, resume}` tells which buttons to enable.
- **Variant `state`**: `planned | ai | saved | backtesting | scored | failed | duplicate`; `origin` `seed |
  mutation | refined | child | elite` (elite = carried over, `carried_from` = the previous generation's
  variant, same strategy/run/score, no new backtest). **Finalist `state`**: `proposed | approved | rejected |
  validating | passed_gates | failed_gates | validation_failed | validation_cancelled |
  validation_start_failed` (renamed 2026-10-17 from `validated` / `validation_failed_gates`: `passed_gates` =
  the validation run passed G1–G8 — NOT the lifecycle state `validated`, which needs the holdout that only the
  user opens. Derived on read, never stored, so no migration. `eligible_state: "candidate" | null` tells the
  next lifecycle step the USER may take (backtested → candidate); the loop itself never changes a lifecycle
  state and never sets `validated`).

**POST `/v2/loops`** body `{seed: {kind: "strategy", strategy_id, version?: int} | {kind: "idea", text:
1..4000} | {kind: "paper", paper_id}, variants?: 2..8 (6), generations?: 1..5 (4), search_points?: 0..3 (1),
model?: "standard"|"premium", history_from?: "2016"|"2000", auto_resume?: bool (true)}` → **202** = the loop
payload (below). Errors: 404 `not_found` (strategy / version / paper), 409 `paper_not_confirmed`, 409 `busy`
(`{error, code, loop_id}` — one loop at a time), 402 `budget` (`{error, code, estimate_usd,
monthly_budget_usd, daily_cap_usd, per_run_cap_usd, spent_month_usd, spent_today_usd, spent_run_usd}`;
also when the per-run cap is smaller than one call), 503 `spend_unrecorded` / `no_model` /
`settings_invalid`. The "fully autonomous" switch and the budget limits are snapshot into `config`.

**GET `/v2/loops/{id}`** `?events_after=<event id>` (poll every 2 s; `events` then holds only newer events,
≤ 500) → 200 (404 `{error: "Discovery loop not found.", code: "not_found"}`):
```
{"id": "lr-e722df19bfc0", "created_at": "2026-10-17T18:33:49+00:00",
 "status": "running", "persisted_status": "running", "status_reason": null, "active": true,
 "seed": {"kind": "strategy", "strategy_id": "s-e5161912bd6f", "version": 1},      // idea: {"kind","text"}; paper: + paper_id
 "root_strategy_id": "s-e5161912bd6f", "family_id": "s-e5161912bd6f",
 "config": {"variants": 6, "generations": 4, "search_points": 1, "model": "standard", "history_from": "2016",
            "auto_resume": true, "autonomous": false},
 "progress": {"phase": "backtest", "message": "simulate: day 120/190", "updated_at": "…", "generation": 2,
              "variant_id": "lv-…", "slot": null, "run_id": "bt-…", "percent": 63.0} | null,
   // phase: seed | plan | ai | backtest | validation | waiting_slot | waiting_connection | selection
 "job_progress": {"phase", "percent", "message", "updated_at"} | null,         // §8.11 progress of the run
 "counts": {"backtests_started": 5, "backtests_planned": 35, "variants": 4, "failed": 0, "duplicates": 0,
            "ai_messages": 3},
 "trials": {"trials_now": 5, "planned_backtests": 30, "trials_projected": 35, "sr0_now": 1.6068,
            "sr0_projected": 2.6512, "trial_variance": 1.527273, "variance_source": "minimum"|"trials",
            "years": 0.655, "note": "The deflated-Sharpe gate (G4) compares …"} | {"unavailable": "<reason>"},
   // show: "Family trials: 5 now → 35 after this loop; G4 bar (expected max Sharpe) 1.61 → 2.65"
 "budget": {"spent_run_usd": 0.006 | null, "ai_messages_cost_usd": 0.006,
            "limits": {"monthly_usd": 10.0, "daily_usd": 2.0, "per_run_usd": 1.0} | {"unavailable": "…"},
            "paused_reason": null},
 "run_cap_reached": false,
 "generations": [{"generation": 1, "variants": [variant, …],
                  "selection": {"elites": ["lv-…"], "refine": ["lv-…"], "culled": ["lv-…"],
                                "duplicates": {"lv-…": {"of": "lv-…", "correlation": 0.97}}, "k": 2, "c": 2,
                                "reasons": {"lv-…": "culled: among the lowest scores of the generation"},
                                "ranking": ["lv-…", …]} | null}],
 "finalists": [finalist, …], "finalists_note": null | "No finalist: no variant passed the pre-checks.",
 "done_note": null | "stopped early: per-run AI cap reached", "notes": [str],
 "events": [{"id": 3, "event": "seed_ready", "generation": null, "variant_id": null, "data": {…}, "created_at": "…"}],
 "last_event_id": 36, "can": {"cancel": true, "pause": true, "resume": false},
 "ai_interaction_notice": "You are interacting with an AI system. The loop's diagnoses and strategy changes are AI-generated, not verified, and not investment advice.",
 "holdout_note": "The loop never opens the holdout: it is opened only by you, once per strategy family, from the strategy's validation report."}
```
`variant`:
```
{"id": "lv-943454fc7472", "generation": 2, "slot": 2, "origin": "child", "parent_variant_id": "lv-1960545fea0c",
 "carried_from": null, "strategy_id": "s-5dd67de28715", "strategy_name": "variant m3 (loop e722df g2·2)",
 "versions": [{"version": 1, "kind": "base", "parameter": null, "value": null, "run_id": "bt-f1d2c75a070e",
               "run_status": "done", "reused": false, "score": -0.432019, "error": null},
              {"version": 2, "kind": "search", "parameter": "lookback", "value": 5, "run_id": "bt-0ac996676240",
               "run_status": "done", "reused": false, "score": 0.513041, "error": null}],
 "chosen_version": 2, "run_id": "bt-0ac996676240", "state": "scored", "reason": null,
 "score": {"version": "sp-loop-score/1", "run_id": "bt-…", "score": 0.513041, "reason": null, "sharpe": 0.9275,
           "penalties": {"turnover": 0.164484, "drawdown": 0.0, "costs": 0.0, "checks": 0.25},
           "inputs": {"turnover_annual": 28.45, "max_drawdown": 0.0867, "cost_share_pct": 33.9,
                      "total_return": 0.0588, "cagr": 0.0912, "trades_closed": 95},
           "checks": [{"key": "trades"|"signal"|"alpha"|"capacity", "status": "pass"|"fail"|"n/a", "value": 95,
                       "threshold": 100, "explanation": "95 closed trades (pre-check needs ≥ 100)"}, …],
           "failed_checks": ["trades"], "checks_passed": false, "notes": [str],
           "range": {"start": "2024-02-09", "end": "2024-10-07"},
           "thresholds": {"g1_ic_t": 2.0, "g7_min_trades": 100, "g8_capacity_multiple": 10.0}} | null,
 "key_metrics": {"sharpe", "total_return", "cagr", "max_drawdown"} | null,
 "topic_check": "passed" | "failed" | "pending" | "skipped" | null,
 "diagnosis": ai_message | null, "refinement": ai_message | null}
```
`ai_message` (shown ONLY when `output` is not null; `pending: true` = written but not yet topic-checked → show
"checking…"; `discarded: true` → show the fixed text "The AI texts of this variant did not pass the topic check —
they are not shown." / the reason, never AI text):
```
{"id": 2, "role": "diagnosis"|"refinement", "pending": false, "discarded": false,
 "discarded_reason": null | "topic_check_failed" | "draft_invalid" | "bad_json" | "off_topic" | "not_a_draft" | …,
 "topic_check": "passed",
 "output": diagnosis {"summary", "weaknesses": [str], "ideas": [str]}
         | refinement {"kind": "draft", "reply", "assumptions": [str],
                       "operator_requests": [{"name", "purpose", "signature"}],   // label "unverified AI suggestion"
                       "spec": {…}, "valid": true, "validation": {"ok", "errors", "warnings"}, "repaired": bool,
                       "repair_errors": [..]|null, "task": "mutation"|"refined"|"child", "aspect": str|null,
                       "operator_requests_label": "unverified AI suggestion"}
         | draft_invalid: {"validation": {…}, "spec": {…}} (the validator card only) | null,
 "check": {"ok", "unverified": [{"text", "offset", "section": "summary"|"weaknesses.<i>"|"ideas.<i>"}],
           "unknown_ids": []} | null,                                   // diagnosis only: mark unverified numbers red
 "provenance": {"ai_generated": true, "generator": "UFOS.ai/HedgeFund/sp-loop-diagnosis", "provider": "OpenRouter",
                "model": "…", "generated_at": "…", "human_reviewed": false,
                "label": "AI-generated content (EU AI Act Art. 50)", "model_tier": "standard", "role": "diagnosis",
                "loop_id": "lr-…"},
 "usage": {"cost_usd", "estimate_usd", "prompt_tokens", "completion_tokens"}, "cost_usd": 0.002, "created_at": "…"}
```
`finalist`:
```
{"variant_id": "lv-b32a3469b74e", "strategy_id": "s-a7e489180aa8", "version": 2, "evaluated_version": 2,
 "promoted": false, "score": 0.948421, "sharpe": 1.1336, "run_id": "bt-a1a15f1163fd", "state": "proposed",
 "validation_run_id": null, "validation_status": null, "validation_error": null, "passed": null,
 "eligible_state": null, "reject_reason": null, "decided_at": null}
```
(`promoted: true` = the evaluated version was re-saved as the strategy's current `version` — same logic hash;
"Open validation report" → `GET /v2/validations/{validation_run_id}`; "Open holdout…" stays the Library's
own button — the loop view never offers it.)
Event names (`events[].event`): `created, running, seed_ready, generation_planned, ai_started, ai_message,
topic_checked, variant_saved, variant_failed, variant_duplicate, backtest_started, backtest_finished,
variant_scored, selection, run_cap_reached, note, finalists, promoted, approved, rejected, validation_started,
validation_finished, cancel_requested, paused, paused_budget, interrupted, cancelled, failed, done` (`data` per
event; `topic_checked.data` never contains the guard's reason).

**GET `/v2/loops`** `?limit=1..200 (50)` → `{loops: [{id, created_at, status, status_reason, seed: {kind,
strategy_id?, version?, paper_id?}, seed_text: str|null, root_strategy_id, family_id, config: {variants,
generations, search_points, model, autonomous}, generations_done, best_score: number|null, finalists: int,
backtests_started}], ai_interaction_notice}` (newest first).
**GET `/v2/loops/estimate`** `?strategy_id=&idea=true|false&variants=&generations=&search_points=&model=` →
`{backtests_planned: 35, ai_calls_planned: 46, estimate_per_call_usd: 0.019409, estimate_usd: 0.892791,
validations_if_autonomous: 2, trials: {…as above…}, budget: <descriptor>, note}` (show before "Start").
**POST `/v2/loops/{id}/cancel | pause | resume`** (no body) → 200 loop payload; 409 `not_running` ("The loop is
already <status>." / "The loop is not running."), `not_resumable` ("The loop is <status> — start a new loop."),
`running`, `busy`; 404. Cancel stops after the current step and cancels the loop's running job; pause lets the
running job finish (frees the slot); resume continues from the stored events (also after `paused_budget` once
the budget allows, and after `interrupted`).
**POST `/v2/loops/{id}/finalists/{variant_id}/approve`** (no body) → 200 loop payload (the validation is
submitted when the slot is free — `state` approved → validating); **…/reject** `{reason?: ≤ 500}` → 200. 409
`autonomous` (the loop validated itself), `decided` ("This finalist is already <state>."); 404 `not_found`
("Finalist not found.").
**GET `/v2/settings/loop`** → `{autonomous: false, budget: {key: "loop", module: "hedgefund", label: "Discovery
loop", cost_log_label, features: [...], limits: [{key: "daily_usd"|"monthly_usd"|"per_run_usd", label, value,
default, min: 0, max: 200, period: "day"|"month"|"run"}], spent: {today_usd, month_usd}, day, month, enabled,
zero_means: "off", paused_reason}, defaults: {variants: 6, generations: 4, search_points: 1}, limits: {variants:
[2, 8], generations: [1, 5], search_points: [0, 3], max_backtests: 120}, autonomous_note, holdout_note}`;
**PUT** `{autonomous?: bool, monthly_usd?, daily_usd?, per_run_usd?: 0..200}` (≥ 1 field) → same payload; audit
`loop_settings_changed {old, new}` / `loop_budget_changed {old, new}`. Settings UI: a toggle "Discovery loop:
fully autonomous (validates its finalists G1–G8 itself; never opens the holdout)" + the three limits (or leave
the limits to the central AI budget window).
- **Review fixes (2026-10-17) — JSON changes for the WPF agent:**
  (M3) a `draft_invalid` refinement returns `output: {"validation": {ok, errors, warnings}}` ONLY (no spec,
  so no AI-written name/description/tags/parameter descriptions; matches the Studio fix).
  (L6) an AI message whose variant failed before its topic check (e.g. the refinement failed after the
  diagnosis) is finalised: `pending: false`, `topic_check: "skipped"`, `discarded: true`,
  `discarded_reason: "not_topic_checked"`, `output: null` (its cost is still shown).
  (L7) `trials` (loop payload and estimate) adds `validations_planned` (k finalist validations in autonomous
  mode, else 0); `planned_backtests` stays the backtest count, `trials_projected` = trials_now +
  planned_backtests + validations_planned; holdout cards never enter `years` / the trial variance.
  (M1) a loop with a pending cancel becomes `cancelled` at the next backend start (never auto-resumed);
  resume of such a loop → 409 `not_resumable` ("The loop was cancelled — start a new loop.").
  (M4) a variant whose backtest cannot start / resume or whose strategy changed meanwhile is `failed` with the
  real reason (`variant_failed.data.code`: `start_failed`, `connection`, `conflict`) — the loop continues.
  (L5) a budget block during the topic check pauses the loop (`paused_budget`) or ends it early (per-run cap,
  the variant fails with code `run_cap`) instead of `topic_check_failed`.
  (M2) the loop budget books and checks the CURRENT UTC day/month on every call (midnight-safe).
  (L8) generation planning is one transaction (variant rows + `generation_planned` + the elites' carry-over
  `variant_scored` events); a crash cannot leave a half-planned generation.
- **Central AI budget window** (`GET/PUT /ai/budgets`, `UFOS.ai/docs/ai-budgets-spec.md` §4.4): three schema-1
  entries `loop` (period day), `loop_month` (month), `loop_run` (period **"run"** — per loop run; `period_id` =
  the running loop id or null, `spent_usd` = that loop's spend, 0 when none runs; the WPF window should label it
  "per loop run"), group "Strategy Platform", min 0, max 200, step 0.1, applies "immediately", `cost_log`
  `[{module: "HedgeFund", feature: "sp-loop" | "sp-loop-diagnosis" | "sp-loop-seed"}]`. `ai_budget_put(key,
  limit_usd)` = the same stored values as PUT /v2/settings/loop (audited `loop_budget_changed`).
- **Audit**: `loop_created`, `loop_cancel_requested`, `loop_pause_requested`, `loop_resumed`,
  `loop_finalist_approved`, `loop_finalist_rejected`, `loop_validation_started`, `loop_spend_unrecorded`, the
  Studio's `studio_call_blocked`/`studio_call_failed` rows for loop calls (detail `loop_id`), plus the normal
  strategy/backtest rows.
- **Tests** (all offline, fake `fund.llm.TRANSPORT`): `test_sp_loop_units.py` (20: scoring, holdout guard,
  selection/diversity, search, budget + hook, schemas), `test_sp_loop.py` (28 — incl. the review-fix tests M1–M4, L5–L8 and the passed_gates naming: full loop with trials/family/
  memory/payload, autonomous vs approval, reused seed card, logic duplicate, topic guard, off-topic/invalid,
  injection, budget off / day pause + resume / per-run cap, cancel, pause/resume, crash recovery, connection
  loss + resume chain = 1 trial, one loop at a time + 409 loop_id, routes, auth, insert-only),
  `test_sp_loop_acceptance.py` (2: planted signal found + planted noise rejected; noise only → "No finalist" —
  idea seed, real engine, ~75 s); support `test_sp_loop_support.py`.

### 16.13 Stage 6 WPF notes (implemented 2026-10-17: rail entry "Loop" — bound to §16.12)

Not built yet by the implementing agent (no dotnet on the VM): checked statically only — the user builds and
click-tests. Choices marked **(confirmed by the user 2026-10-24)** were taken while the user was away and confirmed
by the user on 2026-10-24 ("Das passt").

- **Files**: `Platform/LoopView.cs` (new, code-built UserControl), `Platform/LoopDialogs.cs` (new:
  `LoopStartDialog`, `LoopSettingsDialog`), `Platform/PlatformClient.Loops.cs` (new: DTOs exactly as §16.12 +
  `ListLoopsAsync`, `GetLoopAsync(id, events_after)`, `StartLoopAsync`, `EstimateLoopAsync`,
  `Cancel/Pause/ResumeLoopAsync`, `Approve/RejectLoopFinalistAsync`, `Get/PutLoopSettingsAsync`,
  `ReadConflictLoopId`, `ReadErrorCode`; shared texts `LoopText`), `HedgeFundWindow.Loop.cs` (new: lazy view,
  "Open in Library" reuses the Studio's handler), `HedgeFundWindow.xaml(.cs)` (rail entry "Loop" ↻ between
  Studio and Research, `LoopHost`, visibility switching, `Shutdown` on close), `Platform/StudioView.cs`
  (`SpecDiff` private → internal, reused for the refinement diffs). Computed DTO accessors are `[JsonIgnore]`
  (`LoopAiMessage.Id`); blocks whose shape varies (`progress`, `job_progress`, `counts`, `trials` incl.
  `{unavailable}`, `budget`, `score`, `key_metrics`, `selection`, AI `output`, `check`, `provenance`, `usage`,
  event `data`, settings `budget`/`limits`) stay `JsonElement` and are read by name (`BtJson`).
- **Layout**: left = runs (GET /v2/loops?limit=50: seed label — strategy name from GET /v2/strategies when
  known, else the id; idea text truncated; paper id —, status, created, N×G, generations done, best score,
  finalists, "autonomous"), "✦ New loop…" (Ctrl+N), "Settings…", ↻ (F5). Right = the backend's pinned
  `ai_interaction_notice` (StudioView.NoticeBlock, not dismissible), status area (re-rendered every poll) and the
  scrolling body (re-rendered only when a signature of last_event_id / variant states / run statuses /
  finalist states changed; expander states and the scroll offset survive a re-render).
- **Status area**: seed title + status chip (live region); Pause / Resume (✦, AI tooltip) / Cancel loop
  (confirmation) exactly as `can` allows; ids line (loop, created, root strategy, family); config line incl.
  "fully autonomous ON/OFF (snapshot at start)"; a fixed explanation per status — `waiting_slot` ("another job …
  continues automatically"), `waiting_connection` (probe ≥ 30 s, no limit, one trial), `paused`, `paused_budget`
  (raise it in Settings… / central window or next UTC day, then Resume), `interrupted` (auto-resume on → continues
  after backend start, else Resume; completed steps never recomputed, a cut-off AI call is repeated and costs
  again), `failed` + `status_reason`, `cancelled`, `done`; step line from `progress` (phase label, generation /
  variant "g2·3", percent, message, run id) and job line from `job_progress` (waiting phase → "connection lost —
  resumes automatically"); counts line; budget line (`spent_run_usd` of the per-run cap, `ai_messages_cost_usd`,
  day / month limits or `limits.unavailable`, `paused_reason`); trials line in amber ("Family trials: n now → m
  after this loop; G4 bar (expected max Sharpe) a → b", `unavailable` reason verbatim, `note` as tooltip);
  `run_cap_reached`, `done_note`, `notes`.
- **Connection loss / crash**: a failed GET keeps the last payload, shows "Connection to the backend lost
  (attempt n): <message>" and keeps polling every 2 s; once the backend answers again the body re-renders
  (an interrupted loop then shows its resume state). 404 → the backend's text. Polling: every 2 s only while the
  view is visible and the loop is not terminal / `active`, or a finalist is `approved` / `validating`, or the
  connection is lost; `events_after` = last seen event id (events accumulated locally, ≤ 500 kept); the run list
  is refreshed on every status change and every 10th poll.
- **Finalists panel** (always first in the body): mode line (autonomous: "the loop validates its finalists itself
  (G1–G8, no holdout)"; approval: "approve → gate validation; a finalist that passes is your candidate proposal — move it
  to Candidate yourself in the Library; the loop never changes a lifecycle state"); per finalist: g·slot + strategy
  name, state chip, strategy id / version (evaluated version), score, Sharpe, run, `promoted` note, a fixed line
  per state (proposed / approved "starts when the slot is free" / validating + run / `passed_gates` = "Passed G1–G8 → eligible for candidate" + the backend's `eligible_state`
  ("eligible for Candidate: move it there yourself in the Library") — never "Validated": lifecycle `validated`
  needs the holdout, which only the user opens in the Library (§9.5) / `failed_gates` "Failed the gates" / validation failed / cancelled /
  could not start + `validation_error` / discarded + `reject_reason`), `decided_at`. Buttons: approval mode +
  `proposed` → "Approve → validate…" (confirmation: validation run, no AI, no holdout, no lifecycle change) and
  "Discard…" (form, reason optional ≤ 500, checked locally); always "Open report" (BacktestReportWindow),
  "Open validation report" (ValidationReportWindow, when `validation_run_id`), "Open in Library". No holdout
  button anywhere (`holdout_note` shown at the end of the body). `finalists_note` (e.g. "No finalist: …") shown
  verbatim in amber.
- **Grid** "Generations × variants": rows G1…, columns slots 1…N; each cell a focusable button "#slot glyph
  score|state" (★ kept/elite, ↻ refined next, ✕ culled, ≡ duplicate, ! failed, • scored awaiting selection, …
  in progress — glyph + text, never colour alone; tooltip + automation name with the full summary); Enter /
  click opens that variant's expander and scrolls it into view.
- **Generations**: one expander per generation (latest open by default; header: counts kept / refined /
  culled or "selection pending"; k / c explanation). Per variant an expander (header "g2·3 · origin · score ·
  Sharpe" + state chip + marker chip): strategy name + id, origin sentence (seed / mutation + `aspect` / refined
  from / child of elite / elite carried from — no new backtest), `reason`, selection `reasons[id]`, duplicate
  `of` + correlation, mini metrics (`key_metrics`), versions (v, base / search parameter = value, run + status,
  score, chosen, reused = no new trial, error, "Report" per run), score card (score = Sharpe − turnover −
  drawdown − costs − checks, inputs, pre-checks with ✓/✕/– text, checks_passed, range "training data, before the
  holdout", notes — labelled "deterministic … no AI"), then the AI messages, "Open report" / "Open in Library".
- **AI messages** (`diagnosis`, `refinement`; plain text only): `output` shown ONLY when not null. Pending →
  violet "✦ AI-GENERATED · <role> · checking… (independent topic check pending)" + cost line, no text.
  Discarded / topic check failed → neutral system note with the fixed text "The AI texts of this variant did not
  pass the topic check — they are not shown." (other reasons: "AI <role> discarded (<reason label>) — not shown")
  + cost line; `draft_invalid` with an output → neutral box with ONLY the validator findings. Shown → violet AI
  container: badge `AiContent.Badge(provenance.model, provenance.generated_at)` + role BEFORE the text, cost
  line (tier from `provenance.model_tier`, `cost_usd`, tokens in/out, `estimate_usd`, topic check, "loop AI
  budget"), `AiContent.Caveat` AFTER, screen-reader name `AiContent.AutomationName`. Diagnosis: summary,
  weaknesses, ideas with the `check.unverified` numbers as red underlined runs where the text at `offset` of that
  `section` matches (count + list below; `unknown_ids` listed). Refinement: task + fixed mutation aspect,
  reply, repair round note, validator result (errors / warnings), `repair_errors` expander, operator requests in
  the amber box labelled with `operator_requests_label` ("unverified AI suggestion", text only, never executed),
  assumptions, "Spec changes: v<base> vs parent g·slot v<chosen>" (lazy: GET both stored versions, cached;
  client-side flattened JSON diff via StudioView.SpecDiff, ≤ 60 lines; a variant without a stored version is
  compared as "proposed spec (not saved)"), "Proposed spec (JSON, as returned by the AI)" expander.
- **Activity**: expander with the last 200 events (newest first): time, event, generation, variant label and a
  whitelist of scalar `data` fields (message, reason, note, text, role, kind, version, run_id, status, parameter,
  value, cost_usd, passed, score.score) — no AI text (events carry none).
- **Start dialog** (`LoopStartDialog`, opened after GET /v2/strategies, /v2/papers?limit=50 and
  /v2/settings/loop): seed radios "Library strategy" (filterable list of all strategies incl. alpha-library
  entries — name, state, current version, origin, trials; optional version ≥ 1) | "Idea in words" (TextBox ≤ 4,000
  + counter; note that an invalid / off-topic draft fails the loop) | "Confirmed paper" (papers with status
  `confirmed` and a `strategy_id`; none → "confirm one in Studio → Paper first"); radio rows for variants /
  generations / search points from the settings' `limits` and `defaults` (contract values 2–8/6, 1–5/4, 0–3/1
  only while the settings did not load — said so in the dialog); Standard / Premium; "Load data from 2000"
  (history_from); "Continue automatically after a backend restart or a lost connection" (auto_resume, default
  on); the "fully autonomous" state from Settings (ON / OFF / not known) + "snapshot at the start" + the
  backend's `holdout_note`. Estimate (debounced 400 ms on every change, GET /v2/loops/estimate with the seed's
  strategy id — the paper's `strategy_id` for a paper, `idea=true` without an id for an idea): "Planned: n
  backtests (each one a trial of the seed's family) · m AI calls · estimated $x (≈ $y per call, tier)" (+ "up to k
  validation runs" when autonomous), the loop budget line (per run, today, month, off, paused), warnings when the
  loop AI is off, when the estimate exceeds the per-run cap ("stopped early …") or today's remainder ("may
  pause …"), and the G4 box in amber ("⚠ Family trials: … ; G4 bar …" + why more trials raise the bar + the
  backend's notes). "✦ Start loop" (Ctrl+Enter; no default button so Enter in a list does not start a costly run)
  sends POST /v2/loops from the dialog: success or 409 busy with `loop_id` closes it (busy → "Show the running
  loop?"); every other refusal (400, 402 budget, 404, 409 paper_not_confirmed, 503) stays in the dialog verbatim.
- **Settings dialog** (`LoopSettingsDialog`, Loop → "Settings…" — **confirmed by the user 2026-10-24**: the loop settings
  live in the Loop view, not in the Hedge Fund settings panel): toggle "Discovery loop: fully autonomous
  (validates its finalists G1–G8 itself; never opens the holdout)" — turning it ON asks for an explicit
  confirmation (danger dialog: finalists validated without asking, holdout ALWAYS manual, no lifecycle change,
  running loops keep their snapshot), OFF is saved at once; a refusal restores the stored state. Three limit
  inputs (labels from the descriptor; empty = not sent; 0..`max` (else 200), 0 = off) + "Save limits"
  (PUT with only the filled fields), the spend line, a note that the same limits are in the central AI budget
  window ("per loop run"), and the backend's defaults / ranges / max backtests (read-only: the backend has no
  PUT for defaults). Inputs stay disabled until GET answered; a 503 enables them (saving repairs invalid values).
- **Review fixes of §16.12 handled**: finalist states `passed_gates` / `failed_gates` + `eligible_state`
  (`LoopFinalist.EligibleState`, shown as "eligible for Candidate — move it yourself"; nothing transitions), M3 (draft_invalid → validator findings only), L6 (`not_topic_checked` →
  "the variant failed before its topic check", cost shown), L7 (`validations_planned` → "(incl. k finalist
  validations)" in the trials line), M1 (409 `not_resumable` shown verbatim), M4/L5 (variant `reason` shown;
  `code` in the activity log).
- **Keyboard / accessibility**: F5 reload, Ctrl+N new loop, Tab through list, actions, grid cells and
  expanders; Esc closes the dialogs; every button, list, row and AI block has an `AutomationProperties.Name`
  (AI blocks start with "AI-generated"), status / estimate / errors are live regions.

## 17. Stage 7 — portfolio, risk, OMS (paper) (contract, 2026-10-17)

Decisions: §15 (user, 2026-10-17) — allocator = inverse volatility with a correlation penalty; **incubation
length chosen per strategy before each incubation run, then immutable**; default hard limits **2 % daily
loss** (no new orders until the next session / UTC day) and **10 % drawdown from peak** (kill switch: cancel
all, close all, auto trading off) — adjustable with audit; netting; pre-/post-trade checks; OMS state machine;
migration of the old paper account. Plan: project doc "L6 lifecycle / L7 Portfolio & risk / L8 Execution
(OMS part)" — **one strategy runtime for backtest, paper and live**: a strategy emits target positions; the
same evaluator, portfolio and risk code and the same cost model (the real `fund.broker.PaperBroker`) turn
them into orders. PAPER ONLY: no live code path exists (stage 8 builds the live guard, live stays OFF; stage
11 only on the user's explicit go). Values marked **(confirmed by the user 2026-10-24)** were chosen by the
implementing agent while the user was away (2026-10-17) and confirmed by the user on 2026-10-24.

### 17.0 Audit after interruption (2026-10-18)

The stage-7 agent was stopped on 2026-10-17 after writing §17.12 (SPEC last saved 21:59; last code file
21:42). Audit of the state it left (read-only first, then fixes listed below):
- **What exists:** §17.1–§17.12 complete (contract, exact JSON §17.11, implementation notes §17.12 — no
  half-written subsection). All files named in §17.12 exist: `portfolio/` (9 modules), `execution/` (7 +
  `brokers/{base,paper}.py`), `live/{runtime,incubation}.py`, `api/trading_routes.py` (28 routes = the §17.10
  list), `storage/db.py` v15 (20 tables, insert-only triggers, incubation length immutable by trigger), hooks in
  `lifecycle/registry.py`, `backtest/engine.py` (shared `portfolio/decision.decide`), `fund/{engine,store}.py`,
  `fund/SPEC.md` §16, `main.py` wiring; tests `test_sp_{oms,pretrade,risk_controls,portfolio,trading_parity,
  incubation,trading_api}.py`, `test_fund_migrated.py`, `sp_trading_testkit.py`.
- **Broken / half-written:** nothing. No syntax errors (all backend `.py` parse), no TODO/FIXME except the
  intended IBKR/MT5 note in `brokers/base.py`; the abstract `NotImplementedError`s are the adapter interface.
  Full suite green on the VM (Python 3.10), split runs: fund 336 (1 skip), mr 333, other 134, sp 867 (≈1,670); after the fixes below all
  affected splits re-ran green (sp 868) and the scope guards stage0/mr0/budget0 are ok.
  The user's backend already started once with this code (2026-10-17 23:49 local): hedgefund.db migrated to v15,
  no stage-7 error in backend.log.
- **Missing (the process step after implementation):** the independent review — not done before the stop.
  Done now (focused: OMS/state machine, stored-before-sent, kill/dead-man, migration, checks, incubation band,
  allocator), plus a real-data acceptance on TEMP COPIES of the real hedgefund.db + newsroom.db (outside the
  mount; the real files untouched): v14→v15 not needed (already v15), migration dry run `ok` (4 positions
  BTC-USD, ETH-USD, QQQ, TLT; 4 orders, 61 ledger rows, 136 equity points, no blockers), real run on the copy →
  `paper-council` AccountView equals the old engine's for the same quotes (0 mismatches), Council sub-ledger cash
  = account cash, quantities equal, nothing re-attributed / re-imported.
- **Fixes (2026-10-18):** (1) `POST /v2/oms/orders` `client_order_id` now uses the contract pattern
  `^[A-Za-z0-9_-]{8,64}$` (it accepted `[A-Za-z0-9_.:-]{8,80}`) and refuses the engine's own prefixes (`rt-`,
  `run-`, `kill-`, `kil-`, `ddk-`, `dra-`, `mar-`) so a manual id can never pre-empt a runtime/protective order id
  (GET/cancel paths keep the wider stored-id pattern — runtime ids carry symbols such as `BRK.B`); test in
  `test_sp_trading_api.py`. (2) `incubation.expected_for`: the holdout returns used by the allocator's young-
  strategy volatility were dated against the unfiltered curve dates (shifted after a valuation gap) — now dated by
  their own valued point; first test of `expected_for` on the real run-card shape (it was stubbed everywhere):
  `test_sp_incubation.ExpectedBandFromTheCardTest`. (3) main.py: if the stage-7 wiring fails at startup,
  `/fund/kill` now reports `platform {ok: false, error: "…wiring failed at startup…"}` instead of omitting the
  platform half (fund/SPEC.md §16).
- **Deliberate, unchanged:** `POST /v2/oms/kill` is lenient about its body (a malformed body still kills — the
  kill switch always answers 200); the allocator applies min-weight → renormalise → cap → × max_total (docstring).
- **Open (outside this backend task):** the WPF "Portfolio & Trading" view (binds to §17.11/§17.12); docs/todo.md
  still lists stage 7 as planned; the user's click-test; all "(confirmed by the user 2026-10-24)" values of §17.

### 17.1 Accounts, coexistence with the old engine (the safety rule)

- **Platform paper accounts** (hedgefund.db, `pf_accounts`, insert-only): each one is an INTERNAL paper
  account driven by the real `fund.broker.PaperBroker` on a SQLite store with exactly the store interface
  of `fund.store` / `backtest/memstore.py` (`execution/paper_store.py`, tables `pa_*`, scoped by
  `account_id`) → identical fees, slippage, Reg T margin, borrow, Rule 201, dividends, crypto perps,
  funding, liquidation as the old engine and the backtest. Stage 7 knows two accounts:
  - `paper-main` — opened by the USER (`POST /v2/portfolio/accounts`, starting capital 1,000–100,000,000,
    default 100,000; broker params = `fund.params` defaults, like the backtest). Incubating strategies
    trade here.
  - `paper-council` — created ONLY by the migration of the old paper account (17.9): a 1:1 copy of the old
    account (cash ledger, positions, orders, equity curve, peak/start/accrual/funding state, broker
    params of the old engine's latest params version). Its only strategy source is **"Council of AIs"**
    (source id `council-of-ais`, kind `council`): the migrated positions are HELD (no target source until
    the Council itself becomes a platform strategy in stage 10); the user can reduce/close them with
    manual platform orders.
- **Coexistence rule (confirmed by the user 2026-10-24; the SAFEST of the options): an account has exactly ONE
  owner at any time, and the old engine and the new runtime never share an account.**
  - Before the migration: the old engine (`fund/`) owns the old account (newsroom.db) exactly as today;
    the runtime/OMS only trade `paper-main`. Nothing of the platform writes newsroom.db trading tables.
  - The migration is a **hard switch** started only by the user (dry run first, typed confirmation
    `MIGRATE`): it freezes the old account first (newsroom.db meta `fund.migrated_to_platform` =
    `{status: "migrating"|"migrated", migration_id, at}`), copies it, then marks it `migrated`. From then
    on the old engine is **advisory only**: its cycles still run agents + judge and store recommendations,
    but every trading path refuses with 409 "This paper account was migrated to the Strategy Platform on
    <date> — it is traded in Portfolio & Trading now." (autopilot on, manual orders, recommendation
    execute, reset; the cycle/risk watch skip accruals, pending fills, margin, drawdown stop, autopilot
    orders and equity snapshots; run summary `account_migrated`). The old tables stay as the read-only
    historical record (nothing deleted). `/fund/state` adds `legacy_account {status, migrated_at,
    migration_id}`.
  - The old autopilot is NOT turned into a target source in stage 7 (it would need a second order path
    into the same netting while the old engine keeps its own broker — rejected as less safe).
- **Kill switch covers both:** `POST /fund/kill` (the pinned button) ALSO runs the platform kill
  (all platform accounts); `POST /v2/oms/kill` runs the platform kill AND the old engine's kill
  (`engine.kill`). Each half works when the other fails (errors reported per half, never swallowed).
- **Dead-man switch covers both:** ONE heartbeat — every authenticated `GET /fund/state`, `GET
  /fund/events`, `GET /v2/portfolio/*`, `GET /v2/oms/*`, `GET /v2/incubation*` touches the same client
  timestamp (`engine.touch_client`); the platform's **auto trading** switch (17.7) uses the same timeout
  `autopilot_heartbeat_minutes` (fund params) and switches itself off with audit `engine`/
  `autotrade_heartbeat_off` (+ cancels the queued runtime orders, positions kept). Auto trading is forced
  OFF at every backend start (like the old autopilot).

### 17.2 Lifecycle beyond candidate

`candidate → validated` (holdout, §9.5, unchanged) → **incubation** → (**live-eligible**, a derived flag,
not a state) → `live`/`scaled` (stage 8/11 — refused now: "live trading is off — the live guard comes with
stage 8 and a live step only on your explicit go (stage 12)") → `retired`.
- **Who may trigger what:** only the USER promotes (`validated → incubation` by starting an incubation
  run; abort; retire; resume; migration; manual orders; auto trading on). The runtime/engine only acts
  inside the rules: decisions, orders, post-trade halts/kills, auto trading off (heartbeat / kill),
  completing an incubation run. The decay pause comes in stage 9.
- `validated → incubation` is ONLY possible through `POST /v2/incubation` (the generic transition route
  answers 409 "start an incubation run (Portfolio & Trading → Incubation) — its length is fixed then").
  `incubation → validated` = abort the run; `incubation → retired` (transition route) also aborts it. A new
  version saved while incubating returns the strategy to `draft` (existing rule) → the runtime aborts the
  run ("a new version was saved — incubation ended"). A family with `holdout_failed_at` stays locked (§9.8).
- **Incubation run** (`incubation_runs`, insert-only by triggers — the length can never change):
  `length_sessions` 20..250 (**default 60**, confirmed by the user 2026-10-24), sessions of the strategy's calendar
  (NYSE sessions for equity/mixed universes, UTC days for crypto-only), chosen in the start request and
  stored with `start_date` (the first session the runtime may trade — the next session after the start
  request) and `end_date` (computed from the calendar). A new run (after an abort/completion) chooses its
  length again. Events (`incubation_events`, insert-only): `started`, `session` (per completed session:
  traded or missed + equity), `completed`, `aborted`.
- **Expected band** (from the version's latest done `holdout` card = the validated evidence): daily
  returns of its equity curve → mean μ, std σ, max drawdown. **Result at completion** (default — user
  confirm): `passed` when (a) the realised cumulative log return of the strategy's sub-ledger lies within
  n·μ ± 1.96·σ·√n (n = traded sessions), (b) realised max drawdown ≤ 1.5 × the holdout max drawdown and
  (c) missed sessions ≤ 20 % of the length; (c) violated → `inconclusive`; else `failed`. Reported:
  realised return/vol/Sharpe/max DD, expected band, `tracking_error` (annualised std of realised daily
  return − μ), missed sessions. `live_eligible` = latest completed run `passed` and the strategy still in
  `incubation` with that version. After completion the strategy KEEPS trading on paper (state incubation)
  until the user aborts/retires it or stage 8/11 promotes it.

### 17.3 Strategy runtime (`live/runtime.py`) — the same code as the backtest

- One runtime for all platform accounts; an asyncio task in main.py ticks every 60 s and runs the work in
  a worker thread; `POST /v2/oms/runtime/run` runs one cycle now.
- **Decision** per incubating strategy (and later active ones) once per new completed daily bar: the
  same data loader as the backtest (`backtest.engine.DataLoader`: `all` series for signals, SIP history
  after the 15-minute rule, crypto UTC bars), `Panel.from_bars`, `evaluate`, `portfolio.rebalance_mask`
  and the decision block `portfolio/decision.py::decide` — EXTRACTED from `backtest/engine.run` and called
  by both (rebalance targets via `portfolio.target_weights`, stop loss / take profit exits, drawdown stop
  on the strategy's own sub-ledger, vol target). Decision time: first cycle ≥ close + 20 min (equities,
  NYSE) / 00:20 UTC (crypto-only). State between decisions (stopped symbols, halted) is stored with the
  decision (`rt_decisions`, insert-only). Data failure → that strategy's decision is `failed` with the
  reason (no stale signal, no stand-in); other strategies continue.
- **Execution** at the spec's `execution.timing`: `next_open` = first cycle ≥ open + 6 min of the next
  session (as the backtest); `next_close` = first cycle in [close − 10 min, close − 1 min] (default — user
  confirm: live cannot hit the closing print). A decision not executed before the next decision exists is
  `missed`. Desired quantity per source = `round_qty(|w × E_source / quote|)` signed (the backtest's
  formula); symbols leaving the targets → 0; `force` symbols → 0; no plan → hold.
- **Housekeeping** per cycle and per account (the old engine's order): accruals (borrow, dividends,
  funding) → queued fills (`process_pending`) → margin check → post-trade checks (17.6) → equity snapshot
  (+ sub-ledger snapshots, 17.5). Every 5 min while the US session is open or an account holds crypto or a
  working order exists, else every 30 min.

### 17.4 Allocator (`portfolio/allocator.py`, deterministic)

Per account, over its allocator-managed sources (incubating strategies; the Council source is not
allocated — it keeps its own capital):
- **Volatility** per source: annualised std of the source's daily sub-ledger returns over the last 60
  sessions (≥ 20 required); with fewer incubation sessions the strategy's expected volatility from its
  holdout card's curve (last 60 sessions) — that is the allocator's DEFINITION (`vol_source`
  `ledger`|`holdout`), not a fallback; neither → weight 0 with "no volatility estimate".
- **Correlation penalty:** pairwise correlation of daily returns over the last 120 common sessions (≥ 20,
  else that pair is left out of the mean); ρ̄_i = mean_j max(0, ρ_ij); raw_i = (1/vol_i) / (1 + λ ρ̄_i),
  λ = 1.0 (0..5).
- **Weights:** normalised, then capped per strategy at `max_weight` 0.40 (0.05..1.0) — the excess is NOT
  redistributed (stays unallocated cash); a weight below `min_weight` 0.05 (0..0.5) becomes 0 (and is
  then re-normalised once); `max_total` 1.0 (0.1..1.0) of the pool. Pool = Σ equity of the managed
  sources + `_unallocated` cash.
- **Rebalancing:** monthly (first decision of a new calendar month), when a strategy enters/leaves
  incubation, or on the user's `POST /v2/portfolio/allocation/rebalance`; only sources whose target
  capital differs by > 5 % of the pool (drift band) get a capital transfer (virtual `capital` entries
  between `_unallocated` and the source — cash moves inside the account only). Every allocation is stored
  (`pf_allocations`, insert-only) with its inputs.

### 17.5 Netting + virtual sub-ledgers (`portfolio/netting.py`, `portfolio/ledger.py`)

- **Netting:** per account and instrument the sources' deltas (desired − sub-ledger qty) are netted:
  opposite deltas are **crossed internally** at the current quote (sub-ledger `cross` entries, no fee,
  no account order — no self-crossing in the market); the residual is ONE account order (two legs when
  the account position flips sign: close first, open after the fill — PaperBroker rule), allocated to the
  sources whose delta points that way, pro rata (`allocations` on the OMS order). Reductions first.
- **Participation:** equities ≤ 1 % of the 20-day average dollar volume (the backtest's limit, applied to
  the account order); missing volume → that symbol's order refused (no default volume).
- **Sub-ledgers** (`sl_entries`, insert-only): per (account, source): `capital`, `fill` (cash, signed qty,
  price), `fee`, `slippage` (informational, the adverse price part), `borrow_fee`, `dividend`,
  `dividend_short`, `funding`, `liquidation_fee`, `cross`, `migration`. Every account cash-ledger row is
  attributed exactly once (cursor in the same transaction): fills by the order's allocations (a reduced
  fill pro rata), accruals pro rata by the sources' quantity in that symbol, deposits/resets to
  `_unallocated`. **Invariant (tested):** Σ sub-ledger cash = account cash and Σ sub-ledger qty = account
  qty per symbol. Exposure/P&L per source = its positions marked at the account's quotes (average-cost
  realised P&L; costs by kind).

### 17.6 Pre-trade and post-trade checks (`portfolio/checks.py`, `portfolio/risk.py`)

Per account settings (`GET/PUT /v2/portfolio/settings`, insert-only history `pf_settings`, every change
audited with reason; bounds enforced, 400 otherwise). Defaults (confirmed by the user 2026-10-24 where not set by §15):

| check | default | bounds |
|---|---|---|
| max order notional | 50 % of account equity and ≤ $1,000,000 | 1–100 %; $100–$10,000,000 |
| max position (after the order) | 50 % equities / 40 % crypto of equity | 1–100 % |
| price collar (quote vs a reference: the decision's reference price; else a fresh read of the latest quote — no reference → a risk-increasing order FAILS, a closing order passes with the reason, retest 2026-10-23) | 5 % equities / 10 % crypto | 0.5–50 % |
| buying power | broker formula (BP ≥ est. notional + fees; runtime orders are reduced like `target()`, manual orders rejected) | — |
| short / borrow | `allow_shorts`, `not_shortable`, Rule 201 (broker) | — |
| trading hours | equities 09:35–16:00 NY sessions only (protective orders queue) ; crypto 24/7 | — |
| duplicate guard | one working order per account + symbol; identical order (source, symbol, side, qty) within 60 s refused | 10–3600 s |
| daily order count | 100 per account per NY day (equities) and per UTC day (crypto) | 1–500 |
| **daily loss limit** | **2 %** of the day's reference equity (last snapshot before 00:00 NY for equity orders / 00:00 UTC for crypto orders) → no new risk-increasing orders until the next NY day / UTC day (reducing and protective orders still pass) | 0.5–20 % |
| **drawdown from peak** | **10 %** → that account's KILL: halt, cancel all, close all (protective `kill-switch` orders, queued while closed), auto trading OFF; `POST /v2/oms/resume` (user) clears the halt and rebases the peak | 2–50 % |

Each check result is stored with the OMS order (`checks: [{check, ok, message}]`). A failed check →
order `rejected` with that message (never silently skipped).

### 17.7 OMS (`execution/oms.py`) + broker adapter (`execution/brokers/`)

- **State machine** (persisted `oms_orders`, transitions checked in SQL, every transition an
  `oms_order_events` row, insert-only): `new → submitted → accepted → partially_filled → filled`, plus
  `cancelled`, `rejected`, `expired` (terminal: filled, cancelled, rejected, expired). Allowed:
  new→submitted|rejected|cancelled; submitted→accepted|partially_filled|filled|rejected|submitted (retry);
  accepted→partially_filled|filled|cancelled|rejected|expired; partially_filled→filled|cancelled|expired.
- **Idempotent client order ids:** runtime orders `rt-<execution id>-<symbol>-<leg>`; manual orders take
  the client's `client_order_id` (`^[A-Za-z0-9_-]{8,64}$`, the WPF sends a GUID) or get `man-<12 hex>`; a
  repeated id returns the stored order (200, nothing sent twice). **The order is stored (`new`) BEFORE it
  is sent**; then `submitted`, then the adapter call.
- **Adapter interface** (`brokers/base.py`): `account() -> dict`, `positions() -> list`, `submit(order) ->
  ack {broker_order_id, status: accepted|filled|rejected, filled_qty, avg_price, fees, reject_reason}`,
  `cancel(client_order_id) -> bool`, `order(client_order_id) -> ack|None`, `fills(after) -> [fill]`
  (poll-based fills stream), `capabilities {fractional, shorting, crypto_perps, hours}`, `name`, `paper:
  true`. The ONLY adapter now: `InternalPaperAdapter` (PaperBroker + `PlatformPaperStore`; the broker order
  row carries the client order id; one broker call = ONE SQLite transaction, so a crash never leaves half
  a fill). Stage 8 adds Alpaca paper + Kraken demo; IBKR/MT5 fit the same interface (TODO).
- **Retry policy:** a transient adapter error (`BrokerUnavailable`, e.g. the store locked) → 3 attempts
  (1 s, 3 s) within the call; still failing → stays `submitted`, retried by the next cycle; after 5
  attempts → `rejected` "broker unreachable — gave up after 5 attempts". **Crash between store and send:**
  at startup and every cycle, `new` orders older than 30 s → `cancelled` "not sent before a restart"
  (the intent is stale; the runtime re-decides); `submitted` → `order(client_order_id)`: known → state
  synced; unknown and younger than 5 min → re-sent (same id); older → `rejected` "lost before reaching the
  broker".
- **Partial fills:** a fill smaller than the order (reduced to buying power, clipped to the position) →
  `partially_filled`, and when the broker is done with it → `cancelled` with `filled_qty > 0` ("rest
  cancelled: …"). TIF: runtime orders `day` (queued equity orders not filled by the session end →
  `expired`), protective orders `gtc`.
- **Auto trading** (`POST /v2/oms/autotrade {enabled}`): off → the runtime still decides and records
  targets but sends no orders (decisions `not_executed: auto trading off`; incubation sessions count as
  missed). On refused while an account is halted. Heartbeat + kill turn it off (17.1).

### 17.8 Storage — hedgefund.db v15 (one transaction)

`pf_accounts`, `pf_settings`, `pf_allocations`, `pf_migrations` (insert-only); `pa_orders`
(+`account_id`, `client_order_id` UNIQUE), `pa_positions` (PK account_id, symbol), `pa_cash_ledger`,
`pa_equity` (PK account_id, ts), `pa_events`, `pa_meta` (PK account_id, key) — the paper broker's book;
`oms_orders` (state updated only through the transition function), `oms_order_events` (insert-only);
`sl_entries` (insert-only), `sl_snapshots` (PK account_id, source_id, day); `rt_decisions`,
`rt_executions` (insert-only); `incubation_runs`, `incubation_events` (insert-only — an UPDATE of
`length_sessions` is impossible by trigger). Audit actions: `account_opened`, `settings_changed`,
`allocation`, `incubation_started`, `incubation_aborted`, `incubation_completed`, `oms_order`,
`oms_cancel`, `autotrade`, `autotrade_heartbeat_off`, `platform_kill`, `platform_resume`,
`post_trade_halt`, `migration_dry_run`, `migration`.

### 17.9 Migration of the old paper account (`execution/migration.py`)

- `POST /v2/portfolio/migration/dry-run` → report (nothing written except an audit row): counts (orders,
  ledger rows, positions, equity points), cash, positions, equity at the latest marks, the old account's
  halt/autopilot state, blockers, the broker params that will be copied, the post-trade limits the
  `paper-council` account gets (drawdown kill = the old `drawdown_stop_pct`, daily loss 2 %).
- **Blockers:** old autopilot ON, a cycle in progress, pending (queued) orders in the old account, an
  unreadable old store, `paper-council` already existing, a previous migration.
- `POST /v2/portfolio/migration {confirm: "MIGRATE", reason}` → (1) freeze the old account
  (`migrating`, under the old engine's trade lock), (2) read it, (3) write `paper-council` + Council
  sub-ledger (`migration` entries) + `pf_migrations` row in ONE hedgefund.db transaction, (4) mark it
  `migrated`. Crash recovery at startup: `migrating` with a `pf_migrations` row → completes (4); without →
  un-freezes (nothing moved). **Parity (acceptance):** `paper-council`'s AccountView equals the old
  account's AccountView for the same quotes. Tests run the migration only on temp copies (never the real DB).

### 17.10 API (behind api_auth; strict bodies/queries — unknown fields → 400 `{error: "invalid input",
errors: {field: message}}`; other errors `{error, code, …}`; never a stack trace; times UTC ISO seconds)

`/v2/portfolio/*`: `GET /v2/portfolio/state`; `POST /v2/portfolio/accounts`; `GET
/v2/portfolio/accounts/{id}`; `GET /v2/portfolio/accounts/{id}/equity?since=`; `GET
/v2/portfolio/sources?account_id=`; `GET /v2/portfolio/sources/{source_id}/ledger?account_id=&limit=`;
`GET /v2/portfolio/allocation?account_id=`; `POST /v2/portfolio/allocation/rebalance`; `GET/PUT
/v2/portfolio/settings`; `GET /v2/portfolio/migration`; `POST /v2/portfolio/migration/dry-run`; `POST
/v2/portfolio/migration`.
`/v2/oms/*`: `GET /v2/oms/orders?account_id=&state=&limit=`; `GET /v2/oms/orders/{client_order_id}`;
`POST /v2/oms/orders`; `POST /v2/oms/orders/{client_order_id}/cancel`; `GET /v2/oms/fills?account_id=&after=`;
`POST /v2/oms/autotrade`; `POST /v2/oms/kill`; `POST /v2/oms/resume`; `GET /v2/oms/runtime`; `POST
/v2/oms/runtime/run`.
`/v2/incubation*`: `GET /v2/incubation?strategy_id=`; `GET /v2/incubation/eligibility?strategy_id=`; `POST /v2/incubation`; `GET /v2/incubation/{id}`;
`POST /v2/incubation/{id}/abort`. No route changes an incubation run's length.
Exact JSON: §17.11 (backend notes).

### 17.11 Backend notes — exact JSON for the WPF "Portfolio & Trading" view

Status: written FIRST as the binding payload contract (2026-10-17); implementation notes are added in
§17.12 when the backend is done (any deviation is listed there). All numbers are JSON numbers (never NaN —
null when unknown); money USD; percentages as the UI shows them (2.0 = 2 %); weights as fractions.

**AccountView** (= the old `/fund/state` `account` keys, from `PaperBroker.account`): `{equity, cash,
long_mv, short_mv, buying_power, peak_equity, drawdown_pct, maintenance_required, margin_excess,
short_exposure_pct, starting_capital, reset_at, day_pnl, total_pnl, total_pnl_pct, funding_total,
crypto_margin_excess}`.

**AccountSummary**:
```json
{"id": "paper-main", "label": "Platform paper account", "broker": "internal_paper", "paper": true,
 "origin": "opened", "opened_at": "2026-10-17T12:00:00+00:00",
 "account": {"equity": 100000.0, "cash": 100000.0, "...": "AccountView"},
 "valuation": {"ok": true, "message": null},
 "halt": {"active": false, "kind": null, "reason": null, "at": null},
 "day_loss": {"equity": {"pnl_pct": -0.4, "reference_equity": 100400.0, "limit_pct": 2.0, "blocked": false,
                         "until": null},
              "crypto": {"pnl_pct": -0.1, "reference_equity": 100100.0, "limit_pct": 2.0, "blocked": false,
                         "until": null}},
 "drawdown": {"pct": 1.2, "kill_pct": 10.0},
 "positions_count": 3, "working_orders": 1, "sources": ["_unallocated", "s-1a2b3c"]}
```
`account` is null and `valuation {ok: false, message: "valuation paused: no current quote for X"}` when a
held position has no current quote (no stored-price valuation); `halt.kind` `"kill" | "drawdown" | null`;
`day_loss.*.until` = when the block lifts (ISO) while `blocked`.

**GET /v2/portfolio/state**:
```json
{"now": "…", "paper_only": true, "notice": "Paper trading only · simulated · not investment advice",
 "autotrade": {"enabled": false, "heartbeat_minutes": 15, "last_client_seen": "…|null", "off_at": "…|null",
               "changed_at": "…|null", "changed_by": "user|engine|system|null"},
 "runtime": {"running": true, "cycle_in_progress": false, "last_cycle_at": "…|null", "next_cycle_at": "…|null",
             "last_error": null},
 "legacy_account": {"status": "active|migrating|migrated", "owner": "old engine|platform", "migrated_at": null,
                    "migration_id": null},
 "accounts": ["AccountSummary"],
 "can": {"open_account": true, "migrate": true}}
```

**POST /v2/portfolio/accounts** `{starting_capital?: 1000..100000000 (100000), label?: ≤ 60 chars}` → 201
`{"account": AccountSummary}`; 409 `{error, code: "account_exists"}` (stage 7: one opened account
`paper-main`).

**GET /v2/portfolio/accounts/{id}** → `{"account": AccountSummary, "positions": [Position], "working_orders":
[OmsOrder], "sources": [SourceSummary], "settings": Settings}`; Position = `{symbol, qty, asset_class,
instrument: "stock|spot|perp", side: "long|short", avg_price, last_price, market_value, weight,
unrealized_pnl, realized_pnl, opened_at, borrow_rate_pct, by_source: [{source_id, qty}]}` (values null
without a current quote). 404 `{error, code: "not_found"}`.

**GET /v2/portfolio/accounts/{id}/equity?since=ISO** → `{"account_id", "starting_capital", "reset_at",
"points": [{ts, equity, cash, long_mv, short_mv, drawdown_pct}]}` (≤ 5,000 newest points).

**SourceSummary** (`GET /v2/portfolio/sources?account_id=` → `{"account_id", "sources": [...]}`):
```json
{"source_id": "s-1a2b3c", "kind": "strategy", "name": "SMA 50/200 trend", "strategy_id": "1a2b…",
 "version": 3, "state": "incubation", "incubation_id": "inc-…", "allocation_weight": 0.4,
 "capital_in": 40000.0, "cash": 1200.5, "equity": 40890.1, "valued": true,
 "positions": [{"symbol": "SPY", "qty": 60, "avg_price": 612.1, "last_price": 615.0,
                "market_value": 36900.0, "unrealized_pnl": 174.0}],
 "pnl": {"realized": 12.0, "unrealized": 174.0, "total": 890.1},
 "costs": {"fees": 9.2, "slippage": 9.2, "borrow": 0.0, "funding": 0.0, "dividends": 0.0,
           "liquidation": 0.0},
 "exposure": {"long_mv": 36900.0, "short_mv": 0.0, "gross_pct": 90.2, "net_pct": 90.2}}
```
`kind` `strategy | council | unallocated`; `source_id` = the strategy id for strategies (ids look like
`s-1a2b3c4d5e6f`), `council-of-ais`, `_unallocated`; `pnl.total` = equity − capital_in; `costs.dividends` signed (+ received).

**GET /v2/portfolio/sources/{source_id}/ledger?account_id=&limit=1..1000 (200)** → `{"account_id",
"source_id", "entries": [{id, ts, kind, symbol, qty, price, amount, ref, note}]}` newest first; kinds as
§17.5.

**GET /v2/portfolio/allocation?account_id=** →
```json
{"account_id": "paper-main",
 "allocation": {"id": 4, "created_at": "…", "reason": "monthly rebalance", "actor": "engine",
                "pool": 100000.0, "unallocated_weight": 0.2,
                "weights": [{"source_id": "s-…", "name": "…", "weight": 0.4, "target_capital": 40000.0,
                             "vol": 0.12, "vol_source": "holdout", "mean_corr": 0.3, "penalty": 1.3,
                             "capped": true, "note": null}]},
 "params": {"vol_window": 60, "vol_min": 20, "corr_window": 120, "corr_min": 20, "lambda": 1.0,
            "max_weight": 0.4, "min_weight": 0.05, "max_total": 1.0, "drift_band": 0.05,
            "rebalance": "monthly"},
 "next_rebalance": "2026-11"}
```
`allocation` null before the first one; a source without a volatility estimate has `weight 0`, `vol
null`, `note` "no volatility estimate (…)". **POST /v2/portfolio/allocation/rebalance** `{account_id,
reason: 3..500}` → the same payload.

**Settings** (`GET /v2/portfolio/settings?account_id=` / `PUT /v2/portfolio/settings {account_id, reason:
3..500, changes: {key: number}}`) →
```json
{"account_id": "paper-main", "version": 2, "updated_at": "…", "updated_by": "user",
 "settings": {"max_order_notional_pct": 50.0, "max_order_notional_usd": 1000000.0,
              "max_position_pct_equity": 50.0, "max_position_pct_crypto": 40.0,
              "price_collar_pct_equity": 5.0, "price_collar_pct_crypto": 10.0,
              "duplicate_window_seconds": 60, "max_orders_per_day": 100,
              "daily_loss_pct": 2.0, "drawdown_kill_pct": 10.0,
              "alloc_max_weight": 0.4, "alloc_min_weight": 0.05, "alloc_max_total": 1.0,
              "alloc_lambda": 1.0, "alloc_drift_band": 0.05},
 "defaults": {"…": "same keys"},
 "bounds": {"daily_loss_pct": {"min": 0.5, "max": 20, "step": 0.1, "unit": "%",
                               "label": "Daily loss limit", "group": "post_trade"}}}
```
Groups: `pre_trade`, `post_trade`, `allocator`. Unknown key / out of bounds → 400 with `errors: {key: msg}`.

**Migration**: `GET /v2/portfolio/migration` → `{"legacy_account": {status, owner, migrated_at,
migration_id}, "migration": null | {id, created_at, account_id, summary}, "can_migrate": bool}`.
`POST /v2/portfolio/migration/dry-run {}` →
```json
{"ok": true, "blockers": [{"code": "autopilot_on", "message": "Switch the old autopilot off first."}],
 "legacy": {"orders": 120, "ledger_rows": 340, "equity_points": 900, "cash": 61210.0,
            "starting_capital": 100000.0, "peak_equity": 101200.0, "reset_at": "…",
            "halted": false, "autopilot": false, "pending_orders": 0,
            "positions": [{"symbol": "SPY", "qty": 60, "avg_price": 612.1}]},
 "target_account_id": "paper-council", "source_id": "council-of-ais",
 "settings": {"drawdown_kill_pct": 8.0, "daily_loss_pct": 2.0},
 "broker_params": {"fees_bps": 5.0, "...": "fund params keys used by the broker"},
 "note": "The old engine keeps analysing (recommendations) but no longer trades this account."}
```
`POST /v2/portfolio/migration {confirm: "MIGRATE", reason: 3..500}` → 200 `{"migration": {id, created_at,
account_id, summary}, "account": AccountSummary}`; 400 `confirm` ≠ "MIGRATE"; 409 `{error, code:
"migration_blocked", blockers: [...]}`.

**OmsOrder**:
```json
{"client_order_id": "rt-ex1a2b3c4d-SPY-1", "account_id": "paper-main", "created_at": "…",
 "updated_at": "…", "symbol": "SPY", "asset_class": "equity", "side": "buy", "qty": 60,
 "filled_qty": 60, "avg_fill_price": 615.15, "fees": 9.2, "notional": 36909.0,
 "state": "filled", "terminal": true, "origin": "runtime", "tif": "day", "reason": "strategy rebalance",
 "ref_price": 615.0, "broker_order_id": "17", "reject_reason": null, "attempts": 1,
 "allocations": [{"source_id": "s-…", "qty": 60}],
 "checks": [{"check": "max_order_notional", "ok": true, "message": null}],
 "execution_id": "ex-…"}
```
`origin` `runtime | manual | kill | drawdown | margin` (`margin` = a broker margin call / liquidation
recorded by the OMS); `state` `new | submitted | accepted | partially_filled | filled | cancelled |
rejected | expired`.
- `GET /v2/oms/orders?account_id=&state=&limit=1..500 (100)` → `{"orders": [OmsOrder]}` newest first.
- `GET /v2/oms/orders/{client_order_id}` → `{"order": OmsOrder, "events": [{id, ts, from_state,
  to_state, note}]}`.
- `POST /v2/oms/orders {account_id, symbol, side: buy|sell|short|cover, qty? | notional?, source_id?
  (default: _unallocated on paper-main, council-of-ais on paper-council), reason?: ≤ 500,
  client_order_id?}` → 201 `{"order"}` (a failed pre-trade check is an order in state `rejected` with
  its `checks`), 200 `{"order"}` for a repeated client_order_id; 409 `{error, code: "halted" |
  "not_open"}`; 404 account.
- `POST /v2/oms/orders/{client_order_id}/cancel {}` → 200 `{"order"}`; 409 `{error, code:
  "not_cancellable"}`.
- `GET /v2/oms/fills?account_id=&after=0&limit=1..500` → `{"fills": [{id, ts, client_order_id, symbol,
  side, qty, price, fees, notional}], "next_after": 42}` oldest first.
- `POST /v2/oms/autotrade {enabled: bool}` → `{"autotrade": {...as in state}}`; 409 `{error, code:
  "halted"}` when enabling while an account is halted.
- `POST /v2/oms/kill {reason?: ≤ 200}` → `{"platform": {"ok": true, "error": null, "accounts":
  [{"account_id", "cancelled": 2, "orders": [OmsOrder]}]}, "legacy": {"ok": true, "error": null,
  "halted": true}}` (always 200; each half reports its own failure).
- `POST /v2/oms/resume {account_id, reason: 3..500}` → `{"account": AccountSummary}` (auto trading stays
  off; the peak is rebased to the current equity; 503 when the account cannot be valued).
- `GET /v2/oms/runtime` → `{"running", "cycle_in_progress", "last_cycle_at", "next_cycle_at",
  "last_error", "cycles": [{"started_at", "finished_at", "status": "ok|partial|error|skipped", "summary":
  {"decisions": n, "orders": n, "errors": {key: msg}}}]}` (last 50, kept in memory).
- `POST /v2/oms/runtime/run {}` → 202 `{"queued": true}`.

**IncubationRun**:
```json
{"id": "inc-1a2b3c4d5e6f", "strategy_id": "…", "strategy_name": "…", "version": 3,
 "account_id": "paper-main", "source_id": "s-…", "length_sessions": 60, "calendar": "nyse",
 "start_date": "2026-10-19", "end_date": "2027-01-13", "created_at": "…", "created_by": "user",
 "reason": "…", "status": "running",
 "progress": {"sessions_elapsed": 12, "sessions_total": 60, "traded": 11, "missed": 1,
              "last_session": "2026-11-03"},
 "realised": {"return": 0.012, "log_return": 0.0119, "vol": 0.11, "sharpe": 1.4, "max_drawdown": 0.021,
              "equity": 40480.0},
 "expected": {"mean_daily": 0.0004, "std_daily": 0.008, "vol": 0.127, "max_drawdown": 0.09,
              "source_run_id": "bt-…", "sessions": 504},
 "band": {"lo": -0.06, "hi": 0.07, "n": 11}, "tracking_error": 0.05,
 "result": null, "result_reasons": [], "live_eligible": false, "ended_at": null, "end_reason": null}
```
`status` `running | completed | aborted`; `result` `passed | failed | inconclusive | null`.
- `GET /v2/incubation?strategy_id=` → `{"runs": [IncubationRun], "defaults": {"length_sessions": 60, "min":
  20, "max": 250}}` newest first.
- `GET /v2/incubation/eligibility?strategy_id=` → `{"eligible": bool, "reason": null|str, "calendar":
  "nyse|utc", "defaults": {...}, "account_id": "paper-main", "start_date_if_now": "YYYY-MM-DD"}`.
- `POST /v2/incubation {strategy_id, length_sessions: 20..250, reason: 3..500, account_id?:
  "paper-main"}` → 201 `{"run": IncubationRun, "strategy": {id, state, current_version}}`; 404; 409
  `{error, code: "not_validated" | "already_incubating" | "account_not_open" | "halted" | "locked"}`.
- `GET /v2/incubation/{id}` → `{"run": IncubationRun, "events": [{id, ts, event, data}], "decisions":
  [{id, bar_date, created_at, status: "pending|executed|missed|failed|not_executed", reason, targets:
  {symbol: weight} | null, force: [symbol], error}]}` (newest 300 events / 100 decisions).
- `POST /v2/incubation/{id}/abort {reason: 3..500}` → `{"run", "strategy"}`; 409 `{error, code:
  "not_running"}`.

### 17.12 Stage 7 backend notes (implemented 2026-10-17: §17.1–§17.11, backend part — for the WPF "Portfolio & Trading" view)

All §17.11 payloads are implemented as written, with these additions/deviations (the WPF agent binds to them):
- **Source ids:** a strategy's source id IS its strategy id (`s-<12 hex>`, e.g. `s-1a2b3c4d5e6f`), not `s-<id>`.
  Fixed sources: `_unallocated` (paper-main's deposit and every capital not allocated), `council-of-ais`
  (paper-council). `SourceSummary.capital_in` of the Council = the old account's starting capital (so
  `pnl.total` continues the old total P&L).
- **Settings defaults** (final; the contract's first numbers were too tight for one strategy at the 40 % allocator
  cap): max order notional 50 % of equity and $1,000,000; max position 50 % (equities) / 40 % (crypto); 100 orders
  per class and day; the rest as §17.6 (**confirmed by the user 2026-10-24**). `PUT /v2/portfolio/settings` writes a FULL new
  settings row (`version` = number of rows) — `changes` are merged into the current values.
- **Extra keys** (additive): `OmsOrder` also has `notional_req` (a manual notional order), `allow_reduce`;
  allocation `weights[]` have `target_capital`, `allocation.transfers: [{source_id, amount}]`; kill `orders` entries
  are OmsOrders or `{symbol, state: "error", reject_reason}`; the incubation `decisions` list may start with an
  in-memory entry `{id: null, status: "failed", error, created_at}` = the latest failed decision attempt (data not
  there yet, source down) — it is retried every cycle and never persisted as a stale signal.
- **Audit 2026-10-18 (§17.0, WPF-relevant):** `POST /v2/oms/orders` `client_order_id` = `^[A-Za-z0-9_-]{8,64}$`
  (send a GUID, `Guid.NewGuid().ToString()`); the prefixes `rt-`, `run-`, `kill-`, `kil-`, `ddk-`, `dra-`, `mar-`
  (any case) are reserved → 400 `{"error": "invalid input", "errors": {"client_order_id": "the prefixes rt-, run-,
  kill-, kil-, ddk-, dra-, mar- are reserved for the engine"}}`; other bad ids → `"8..64 characters [A-Za-z0-9_-]
  (e.g. a GUID)"`. `GET /v2/oms/orders/{client_order_id}` and `…/cancel` still accept every stored id
  (`[A-Za-z0-9_.:-]{8,80}`, runtime ids carry symbols). `/fund/kill` always has `platform` now (when the stage-7
  wiring failed at startup: `{ok: false, error: "RuntimeError: Strategy Platform stage 7 wiring failed at startup
  (…)", accounts: []}`) — show it like any other half failure.
- **Old engine (`fund/`, recorded in fund/SPEC.md §16):** `GET /fund/state` adds `legacy_account {status:
  active|migrating|migrated, owner: "old engine"|"platform", migrated_at, migration_id}`; `POST /fund/kill`'s
  response adds `platform` (the platform half: `{ok, error, accounts}`); after the migration every trading path
  answers 409 with "This paper account was migrated to the Strategy Platform on <date> — it is traded in Portfolio
  & Trading now."; the cycle is advisory (summary `account_migrated`); the old kill only HALTS the frozen account
  (the platform half closes the real positions in paper-council).
- **Files:** `portfolio/{decision.py, allocator.py, allocation.py, netting.py, ledger.py, checks.py, risk.py,
  settings.py, views.py}`, `execution/{paper_store.py, accounts.py, oms.py, control.py, migration.py, errors.py,
  brokers/{base.py, paper.py}}`, `live/{runtime.py, incubation.py}`, `api/trading_routes.py` (mixin of `V2Api`,
  28 routes appended to `routes.ROUTES`), `storage/db.py` (**v15**, 20 tables), `lifecycle/registry.py`
  (`incubation_transition`, generic route refuses `validated → incubation` / `incubation → validated`, leaving
  incubation by another transition ends the run, live message), `backtest/engine.py` (the decision block now calls
  `portfolio/decision.decide` — no behaviour change, `ENGINE_VERSION` unchanged, all backtest/parity tests green),
  `fund/{engine.py, store.py}` (§17.1 hooks), `main.py` (wiring: `accounts.configure(AlpacaFeed)`, kill/migrated
  hooks, `migration.recover()`, auto trading forced off, the runtime task).
- **Choices** (confirmed by the user 2026-10-24): the runtime's first decision is for the session BEFORE `start_date` (so the
  first execution happens on `start_date`); stop-loss / take-profit checks use the split-adjusted close of the
  decision bar (the fill basis); a strategy that left incubation keeps its positions until the next tradable
  moment, then its source is closed to 0 (orders `runtime`), and its cash returns to `_unallocated` once flat
  (allocation reason "release capital …"); ADV for the participation cap = the mean of the last 20 complete SIP
  dollar volumes (no complete 20 → that equity symbol's order is refused, as in the backtest); accruals are
  attributed per share × each source's SIGNED quantity (a source short against another's long pays the lender's
  share); orders without allocations (kill, broker margin calls) go to the sources holding the symbol in the
  reducing direction; incubation band uses daily LOG returns of the holdout card's curve; `tracking_error` = the
  annualised RMS deviation of the realised daily return from μ; sessions without an executed decision (auto
  trading off, backend down, window missed, halted) count as missed. Market orders only (limit/stop/bracket and
  reconciliation come with the real brokers, stage 8).
- **Startup / recovery:** `migration.recover()` (complete or undo an interrupted migration), auto trading forced
  OFF, the runtime's first cycle (≤ 60 s) runs `oms.sync` = the crash-between-store-and-send recovery (§17.7).
- **Known limits:** the runtime keeps its cycle list and the last decision errors in memory (lost on restart);
  quotes shown by the GETs are the runtime's last quotes (a held symbol without one shows null values and
  `valuation.ok false` until the next cycle); IBKR/MT5 adapters are not built (TODO, interface in
  `execution/brokers/base.py`).
- **Tests** (all offline, temp databases only, never the real DBs): `test_sp_oms.py` (16: transitions,
  idempotency, stored-before-sent, retry policy, crash between store and send, one broker call = one transaction,
  partial fill, day expiry, cancel, margin-order import), `test_sp_pretrade.py` (11: every pre-trade check),
  `test_sp_risk_controls.py` (10: drawdown kill + stale-mark guard, daily-loss events, auto trading, heartbeat
  shared with the old engine, kill switch covering both engines, each half's own failure, resume),
  `test_sp_portfolio.py` (12: allocator math, transfers, netting, splits, attribution invariant),
  `test_sp_trading_parity.py` (6: order parity incl. borrow + perp funding, target parity runtime vs the old
  `target()`, migration dry run / blockers / real run on temp copies / AccountView parity / coexistence / crash
  recovery / failed copy un-freezes), `test_sp_incubation.py` (8: immutability, rules, abort/restart with a new
  length, retire / new version end the run, live stays off, runtime end to end, data-not-there-yet,
  passed/failed/inconclusive), `test_sp_trading_api.py` (7), `test_fund_migrated.py` (2: the advisory old engine).
  `test_sp_families_lifecycle.py` updated (incubation text of stage 7).

- **Stage 10 addition (user, 2026-10-17):** replace the blanket "not
  financial advice" line across the WHOLE app with context-specific notices
  held centrally (Shared/AiContent.cs): (a) analyses/explanations/news
  summaries — "AI-generated analysis — may be inaccurate; information, not a
  recommendation."; (b) strategies, committee memos, loop finalists —
  "AI-generated research output for your own decision. Not a personal
  recommendation by a licensed advisor; you decide and carry the risk.
  Backtests are hypothetical."; (c) autonomous execution — "Automated trading
  on your own paper account under the limits you set. Paper only — no real
  money."; (d) a separate explicit notice if live trading is ever enabled.
  Background: own-use tool ≠ regulated advice; a sold product with
  recommendations or autonomous trading for others would likely need a
  licence (WpIG/KWG, MiFID II) — no disclaimer changes that; legal check stays
  a precondition for "sellable".

- **Project direction (user, 2026-10-17):** built to be shippable IN THEORY
  (product-grade quality, licensing-safe "tool" design — the user builds,
  checks and decides), but NOT shipped: it stays the user's own project,
  trading at most with the user's own money and accounts; no features whose
  only purpose is serving other users. (Claude Project: claude/project-direction.md)

## 18. Stage 8 — brokers (contract, 2026-10-18)

Decisions: §15 (user, 2026-10-17) — **Alpaca PAPER** for stocks/ETFs, **Kraken Futures via ccxt, DEMO** for
crypto; IBKR/MT5 NOT built (the adapter interface stays open, TODO); reconciliation **halts** on mismatch;
**live guard built (4 conditions + capital cap) but live stays OFF**; stage 12 only on the user's explicit go.
Project direction (user, 2026-10-17): shippable-in-theory quality, licensing-safe tool design, the user's own
money/accounts only. Values marked **(default — user confirm)** were chosen by the implementing agent while the
user is away (2026-10-18). Stage 8 adds NO live endpoint: every broker URL the code can build is a paper/demo URL
(18.7), and the live guard refuses with the hard-coded message even when all four conditions hold.

### 18.1 Broker accounts — the model (mirror book, broker = truth)

- Two **broker accounts** next to the internal ones (§17.1):
  | account id | broker | venue | instruments |
  |---|---|---|---|
  | `alpaca-paper` | `alpaca_paper` | Alpaca paper trading (`https://paper-api.alpaca.markets`) | US stocks/ETFs (whole shares) |
  | `kraken-demo` | `kraken_demo` | Kraken Futures demo (`https://demo-futures.kraken.com`) | crypto `BASE-USD` as linear perpetuals `PF_*` |
  Rows in the new insert-only table `br_accounts` (`pf_accounts` and its CHECK stay untouched);
  `accounts.rows()` / `accounts.get()` / `all_accounts()` return internal AND connected broker accounts, so the
  stage-7 OMS, pre-/post-trade checks, settings, sub-ledgers, allocator, runtime, kill and dead-man switch work on
  them unchanged. A disconnected broker account is skipped by the runtime (its rows stay as history).
- **Mirror book:** a broker account's book in `pa_*` (account_id = the broker account id) is the platform's
  EXPECTED state — written ONLY from broker fills (the broker's fill price and fee; 18.4) and from a
  reconciliation the user resolved (18.5). It is valued with the same `fund.broker.PaperBroker` valuation code
  (equity, cash, exposure, drawdown, day P&L) — PaperBroker never EXECUTES on a mirror book (no simulated fill,
  accrual, funding, margin call or liquidation). The broker is the truth; reconciliation compares the two and
  halts that broker on any mismatch.
- **Cash basis:** Alpaca — the account's `cash` (spot accounting, the mirror's cash ledger is identical by
  construction). Kraken (margin-settled perps) — `collateral basis` = the flex account's balance value
  (collateral without unrealised P&L); the mirror's comparable number = mirror cash + Σ qty × avg entry price
  (the spot-style cost basis converted back; equal for a correct mirror). Equity is shown but not reconciled (the
  marks differ: the platform values with its Alpaca crypto quotes, Kraken with its mark price).
- **Connect** (Settings → Brokers): `POST /v2/brokers/{id}/test` (SDK installed, keys present, paper/demo
  endpoint asserted, account / positions / open orders readable; Kraken: markets loaded) → `POST
  /v2/brokers/{id}/connect {confirm: "CONNECT ALPACA-PAPER" | "CONNECT KRAKEN-DEMO", reason}`. Preconditions
  (default — user confirm): test ok; the broker account is **flat with no open orders** (the platform never
  imports foreign positions — close them / reset the paper account at the broker first) and its cash /
  collateral ≥ $1,000. First connect: `br_accounts` row (params = fund defaults with `starting_capital` = the
  opening cash/collateral), mirror deposit = opening balance → `_unallocated`, settings = defaults, reconciliation
  `connect` → `connected`. Reconnect (after a disconnect): the mirror continues; the connect reconciliation must be
  ok (a cash change at the broker in between → mismatch → resolve, 18.5).
- **Disconnect** `POST /v2/brokers/{id}/disconnect {reason}`: only when the mirror and the broker are flat, no
  working orders, no running incubation and no strategy routed there (409 otherwise). Audit everything.

### 18.2 Routing — which strategy / instrument goes where, and the switch

- The internal paper account `paper-main` stays the DEFAULT for every strategy until the user switches it.
- **Venue rule:** `alpaca-paper` takes only equities/ETFs, `kraken-demo` only crypto `BASE-USD`; a strategy
  whose universe mixes both can only run on `paper-main` (stage 8; a split across two brokers is not built).
- **Route per strategy** (`br_routes`, insert-only history; the newest row wins; none = `paper-main`).
  `POST /v2/brokers/routing {strategy_id, account_id, confirm, reason}` — `confirm` = `"ROUTE TO <ACCOUNT-ID
  IN CAPITALS>"` (e.g. `ROUTE TO ALPACA-PAPER`, `ROUTE TO PAPER-MAIN`). Preconditions: target account open /
  connected, not halted, its last reconciliation `ok`; the venue rule; the strategy has NO running incubation run
  (a run's account is fixed — abort it first; the next run starts on the routed account); the strategy's source
  holds nothing in any account (flat). Effect: `POST /v2/incubation` uses the route as its `account_id` (another
  value → 409 `route_mismatch`). Audit `broker_route`.
- **Manual orders** may target a broker account directly (`POST /v2/oms/orders {account_id: "alpaca-paper" …}`),
  subject to the venue rule (400) and all checks.

### 18.3 Adapters (`execution/brokers/`)

Interface (`base.py`, extended — IBKR/MT5 fit the same, TODO): `account()`, `positions()`, `submit(order)`,
`cancel(client_order_id)`, `order(client_order_id)`, `fills(after)` (internal paper), plus for real brokers
`snapshot() -> {account, positions, open_orders, at}` (one read for reconciliation), `open_orders()`,
`cancel_all() -> int`, `instrument(symbol) -> InstrumentInfo`, `endpoint`, `capabilities {order_types, tifs,
fractional, shorting, crypto_perps, hours, extended_hours, bracket}`, `name`, `paper: true`.
Order dict adds `type: market|limit|stop|stop_limit|bracket`, `limit_price`, `stop_price`, `take_profit
{limit_price}`, `stop_loss {stop_price, limit_price?}`, `tif: day|gtc`. Ack adds statuses `partially_filled`,
`expired`, and `legs: [{client_order_id, broker_order_id, type, side, qty, status, filled_qty, avg_price, fees}]`,
`broker_status` (the raw broker status), `fees` (cumulative). Every adapter call that reaches the network goes
through an `http_gate` gate; transient failures raise `BrokerUnavailable` (`kind = "connection"` for transport
failures); refusals by the broker are acks with `status: rejected` and the broker's reason.

**`alpaca_paper`** (alpaca-py ≥ 0.44 `TradingClient(paper=True)`, lazy import → "alpaca-py not installed"):
- Keys `alpaca-paper-key-id` / `alpaca-paper-secret-key` (existing entries), read per call; hardened client (no SDK
  retries, 20 s timeout, `harden_client`); gate `alpaca` (the ONE shared 180/min budget, normal priority, data type
  `trading`); the client's base URL is asserted to be `https://paper-api.alpaca.markets` (18.7).
- `submit`: `get_order_by_client_id(cid)` FIRST (404 → unknown) → `submit_order(Market|Limit|Stop|StopLimit
  OrderRequest, client_order_id=cid, time_in_force=day|gtc)`; bracket = `order_class=bracket` with `take_profit` /
  `stop_loss` (TIF day|gtc, legs reported in `legs`). Sides: buy/cover → BUY, sell/short → SELL. Notional only for
  market DAY orders. A send that fails ambiguously (timeout, connection drop, 5xx, HTTP 422 "client_order_id must
  be unique") is NEVER blindly re-sent: the adapter looks the id up again → found: its ack; not found:
  `BrokerUnavailable` (the OMS re-sends later with the SAME id, looking it up first every time).
- Status map: `new, accepted, pending_new, accepted_for_bidding, calculated, held, pending_replace, replaced,
  pending_cancel, done_for_day, stopped` → accepted; `partially_filled` → partially_filled; `filled` → filled;
  `canceled` → cancelled; `expired` → expired; `rejected, suspended` → rejected.
- Fills: polling of the working orders' status (`filled_qty`, `filled_avg_price`, incl. bracket legs with
  `nested=true`) — every 15 s while a broker order works and every runtime cycle. The trade-update WebSocket
  stream is NOT used in stage 8 (one code path that survives weak Wi-Fi; noted for later). Fees: 0 (commission-
  free; whether Alpaca paper simulates SEC/TAF fees, dividends and borrow fees → **verify**; any such cash change
  shows up as a reconciliation mismatch, never silently).
- Rules: the platform sends **whole shares** for equities (`round_qty`); fractional and notional orders are only
  valid as market/limit DAY and never short (the adapter refuses otherwise); **hours** — the platform trades the
  regular session (pre-trade check, §17.6); `extended_hours` (limit DAY only at Alpaca) is not enabled in stage 8;
  **PDT** — Alpaca applies pattern-day-trader protection to margin accounts below $25,000 (a 4th day trade within 5
  business days is refused) → ack `rejected` "Alpaca refused: pattern day trader protection (equity below
  $25,000) — …"; **intraday margin** — Alpaca allows up to 4× intraday / 2× overnight buying power; the platform's
  own buying-power check stays the conservative Reg T 2× of the mirror; **shorts** — easy-to-borrow only (Alpaca
  refuses hard-to-borrow); crypto is never sent to Alpaca (venue rule).

**`kraken_demo`** (ccxt `krakenfutures`, pinned `ccxt==4.5.84` — lazy import guard: not installed → state
`not_installed` "ccxt is not installed in the HedgeFund backend environment (restart the app so it installs
requirements.txt)"):
- Endpoint: `ccxt.krakenfutures({...}).set_sandbox_mode(True)` swaps `urls['api']` for `urls['test']` =
  `https://demo-futures.kraken.com/derivatives/api/` (+ `/api/charts/`, `/api/history/`) — checked in ccxt's
  source (2026-10-18); after construction EVERY `urls['api']` value must start with
  `https://demo-futures.kraken.com/` or the adapter refuses (18.7). `enableRateLimit: true`, `timeout: 20000`; own
  gate `kraken_demo` (60 req/min, burst 5); reads retried on 429/5xx/timeouts (3 attempts), sends never retried
  by the gate (lookup first, as Alpaca).
- Keys: NEW Credential Manager entries `kraken-demo-api-key` ("Kraken Futures demo API key (public)") and
  `kraken-demo-api-secret` ("Kraken Futures demo API secret (private)") — created at demo-futures.kraken.com →
  Settings → Create key (full access on the DEMO only). Status-only in `GET /v2/settings/keys`.
- Symbols: platform `BASE-USD` ↔ ccxt `BASE/USD:USD` ↔ Kraken `PF_<KBASE>USD` (Kraken spells BTC `XBT`, DOGE `XDG`;
  ccxt unifies them) — only active linear multi-collateral perpetuals (`PF_*`); inverse `PI_*` and fixed-maturity
  `FI_*`/`FF_*` are never used. No such market → "BASE-USD is not available on Kraken Futures demo".
- Sizes: amount step = the market's `precision.amount` (`contractValueTradePrecision`), minimum =
  `limits.amount.min` or the step; quantities are rounded DOWN to the step; below → rejected "size below Kraken's
  minimum (0.0001 BTC)"; the platform's $10 crypto minimum still applies.
- Orders: market (`mkt`), limit (`lmt`), stop (`stp`, trigger `mark`), stop_limit (`stp` + limit); bracket →
  refused ("bracket orders are not supported on Kraken Futures demo"); TIF `gtc` only (`day` refused — runtime
  orders on `kraken-demo` are sent `gtc`); `sell` and `cover` are sent `reduceOnly` (a closing order can never
  open the opposite side). Client id → `cliOrdId`; lookup by id = open orders, then the closed orders of the last 7
  days, matched on `clientOrderId` (**verify against the demo**, §18.13). Leverage is never changed by the platform; the mirror's buying-power check
  treats a long like spot (≤ 1× notional) and a short with the perp margin of the fund params (conservative).
- Funding: realised funding payments are imported before every reconciliation (`fetch_funding_history`, deduped
  by id → mirror `funding` rows, attributed like §17.5); fees are taken from the fill when Kraken reports them,
  else estimated with the market's taker rate (the residual is inside the cash tolerance; **verify** on the demo).
  Margin / liquidation info per position (initial/maintenance margin, liquidation price, unrealised P&L,
  leverage) is shown read-only; a liquidation at the broker appears as a position mismatch → halt.

**Licence / terms notes (in `GET /v2/brokers`, `terms`)** — all marked **verify** by the user:
Alpaca paper — free paper account under Alpaca's terms (own use; market data on the free plan is for the account
holder only, §3); Kraken Futures demo — free demo account with simulated funds, sign-up by e-mail, no KYC; real
Kraken derivatives for German residents run through Kraken's EU (MiFID) entity and Kraken's MiCA licence — not
needed for the demo and irrelevant until stage 12. The tool trades only the user's own accounts (project
direction).

### 18.4 OMS on broker accounts

- Same state machine, ids, stored-before-sent, retry policy and crash recovery as §17.7 (the adapter is the real
  broker). **No duplicate orders:** the client order id is the broker-side idempotency key AND the adapter looks
  it up before every (re)send (18.3); `sync` (startup + every cycle + the 15 s poller) re-reads `submitted`
  orders by id before re-sending.
- New ack statuses: `partially_filled` → the order stays working in `partially_filled` (filled_qty/avg updated);
  `expired` → `expired`; `cancelled` with `filled_qty > 0` → `partially_filled` → `cancelled` ("rest cancelled at
  the broker"). (Internal paper semantics unchanged.)
- **Fill booking** (`brokers/mirror.py`): per broker order id (parent and every bracket leg) the last booked
  cumulative `(filled_qty, avg_price, fees)` is kept in `br_fill_marks`; a new ack books only the DELTA (qty, its
  exact price = (q₁·p₁ − q₀·p₀)/(q₁ − q₀), fee delta) into the mirror in ONE transaction (pa_orders row with the
  broker order id, cash `fill` + `fee` rows, position avg/realised P&L like PaperBroker) → sub-ledgers attribute it
  by the OMS order's allocations (§17.5; a later delta of the same order carries `<cid>~<n>`). Bracket legs are
  imported as OMS orders `<parent cid>.tp` / `.sl` (`parent_client_order_id`, the parent's origin and
  allocations).
- New OMS columns (v16): `order_type` (`market` default), `limit_price`, `stop_price`, `legs_json`,
  `parent_client_order_id`, `broker_status`. `POST /v2/oms/orders` accepts `type`, `limit_price`, `stop_price`,
  `take_profit {limit_price}`, `stop_loss {stop_price, limit_price?}`, `tif` (day|gtc); the internal paper broker
  accepts market only (400 "the internal paper broker supports market orders only"). Runtime and protective orders
  are market orders.
- New pre-trade checks on broker accounts (first failure rejects, §17.6): `venue` (instrument allowed here; size
  rounded to the broker's step and ≥ its minimum; order type / TIF supported), `broker_link` (connected, link
  `ok`, last reconciliation `ok` and ≤ 15 min old — else "waiting for broker (attempt n) — no new orders until it
  answers and reconciles"). Protective (kill/drawdown) orders skip `broker_link` and queue/retry.

### 18.5 Reconciliation (`brokers/reconcile.py`) — halts on mismatch, manual resolve

- **When:** at backend start (trigger `start`, before the first runtime execution), in every runtime cycle's
  housekeeping (`cycle`), after connect (`connect`), on `POST /v2/brokers/{id}/reconcile` (`manual`) and after a
  resolve (`resolve`). Before comparing: OMS `sync` (fills booked), Kraken funding import.
- **Compares** (one broker `snapshot()`): (1) positions — every symbol's quantity exactly (to the broker's step;
  equities whole shares); (2) cash basis (18.1) — |broker − expected| ≤ tolerance = max($1.00, 0.01 % of equity)
  Alpaca / max($5.00, 0.25 % of equity) Kraken (default — user confirm); (3) open orders — every broker open order
  must be a working OMS order (by client id, legs by broker id) and every OMS order in `accepted` /
  `partially_filled` must be open at the broker (after a status lookup).
- **Result** stored insert-only in `br_reconciliations` (`ok | mismatch | unreachable`). `mismatch` → that broker
  account HALTS (kind `reconcile`): NO new orders at all — also no reducing ones (the mirror is wrong, so a
  "closing" order could open a position) — only the broker kill switch (which uses the BROKER's positions) still
  works; alert = account event + audit `broker_reconcile_mismatch` with the clear reason ("Reconciliation
  mismatch: SPY 10 at the broker vs 0 expected; cash 1,234.50 vs 1,000.00 (tolerance 1.00)"). The runtime skips the
  account (decisions `not_executed: account halted`), other accounts continue. **No auto-correction.**
  `unreachable` (connection) → no halt, but `broker_link` blocks new orders until a reconciliation is ok.
- **Resolve** `POST /v2/brokers/{id}/resolve {action, reason, confirm?}` (user only, audited `broker_resolve`):
  `recheck` (the user fixed it at the broker, e.g. cancelled a stray order → reconcile again);
  `cancel_unknown_orders` (cancels at the broker the open orders the OMS does not know → reconcile);
  `accept_broker` with `confirm: "ACCEPT BROKER VALUES"` (the mirror adopts the broker's positions/avg prices and
  cash basis: positions set, one `broker_adjustment` cash row, sub-ledger `other` rows to `_unallocated`; OMS
  orders not open at the broker → `cancelled` "not open at the broker (resolved by the user)"). Each action ends
  with a reconciliation; `ok` clears the reconcile halt (auto trading is not switched on by it).
  `POST /v2/oms/resume` on an account with a reconcile halt → 409 `reconcile_mismatch`.

### 18.6 Weak connections, kill switch, dead-man switch

- **Link state** per broker (in memory + shown): `ok | waiting | auth_failed | keys_missing | not_installed |
  error | idle` with `attempt`, `next_retry_at`, `last_ok_at`, `last_error`. A transport failure (reuses
  `network.is_connection_error`) → `waiting`, attempt n+1, adaptive backoff min(15 s · 2^(n−1), 300 s); the
  poller/runtime do not call that broker before `next_retry_at` (user actions — test, reconcile, kill — always
  try); message "Waiting for broker (attempt 3) — next try 12:04:10 UTC". Success → `ok`, attempt 0. Rejected keys
  → `auth_failed` ("Alpaca rejected the paper keys — check them in Settings → Keys"), re-checked every 5 min.
  Never a duplicate order (18.4); never a stand-in value (no cached broker numbers shown as current — the last
  snapshot is shown with its time).
- **Kill switch per broker** `POST /v2/brokers/{id}/kill {reason?}`: halt (kind `kill`), cancel ALL open orders at
  the broker (`cancel_all`, also orders unknown to the OMS) and the OMS working orders, then close every position
  the BROKER reports with protective OMS market orders (`kill-…`, reduceOnly on Kraken). Unreachable broker → the
  halt is set, the kill orders are stored and re-sent by `sync` until they reach the broker; the response says so.
  The global kill (`POST /v2/oms/kill`, `/fund/kill`) runs this for every connected broker account (each account
  reports its own result). Resume: `POST /v2/oms/resume` (needs reconciliation ok).
- **Dead-man switch:** unchanged ONE heartbeat (§17.1); auto trading off cancels the runtime orders on broker
  accounts too (at the broker).

### 18.7 Live guard (`brokers/live_guard.py`) — built, live OFF

- Live would require ALL of: (1) the per-broker live flag in settings (`br_live_guard`, insert-only history,
  `PUT /v2/brokers/{id}/live-guard`), (2) the typed confirmation `"GO LIVE <ACCOUNT-ID IN CAPITALS> WITH REAL
  MONEY"`, (3) the strategy in lifecycle state `live`, (4) a capital cap set (`capital_cap_usd` 100..1,000,000).
- `BUILD_ALLOWS_LIVE = False` (hard-coded): `evaluate()` reports each condition, and the decision is ALWAYS
  `allowed: false` with `refusal` = **"Live trading is disabled in this build (stage 12 requires the user's
  explicit go)."** No live endpoint is configured: `endpoint_for(broker, live=True)` raises
  `LiveTradingDisabled`; adapters build clients only through `paper_endpoint(broker)` and assert the result
  (`assert_paper_url`); the known live hosts (`api.alpaca.markets`, `futures.kraken.com` without `demo-`) are
  listed only in the guard's deny-list. Tests: each condition missing → refused and named; all four met → still
  refused with the build message; a live URL injected into a client / ccxt `urls` → refused; a source scan finds no
  live host outside the deny-list.

### 18.8 Storage — hedgefund.db v16 (one transaction)

`br_accounts` (insert-only: id, broker, label, params_json, created_at, created_by), `br_connections`
(insert-only: account_id, ts, status connected|disconnected, actor, reason, detail_json), `br_routes`
(insert-only), `br_reconciliations` (insert-only), `br_resolutions` (insert-only), `br_live_guard` (insert-only),
`br_imports` (insert-only dedupe of imported broker events, PK account_id + kind + ext_id), `br_fill_marks`
(cursor per broker order id); `oms_orders` + `order_type`, `limit_price`, `stop_price`, `legs_json`,
`parent_client_order_id`, `broker_status`. Audit actions: `broker_test`, `broker_connect`, `broker_disconnect`,
`broker_route`, `broker_reconcile_mismatch`, `broker_resolve`, `broker_kill`, `live_guard_settings`,
`live_guard_check`.

### 18.9 API (behind api_auth; strict input as §17.10; `{id}` = `alpaca-paper | kraken-demo`)

`GET /v2/brokers`; `GET /v2/brokers/{id}`; `POST /v2/brokers/{id}/test`; `POST /v2/brokers/{id}/connect`;
`POST /v2/brokers/{id}/disconnect`; `POST /v2/brokers/{id}/reconcile`; `GET /v2/brokers/{id}/reconciliations?limit=`;
`POST /v2/brokers/{id}/resolve`; `POST /v2/brokers/{id}/kill`; `GET /v2/brokers/{id}/instrument?symbol=`;
`GET/PUT /v2/brokers/{id}/live-guard`; `POST /v2/brokers/{id}/live-guard/check`; `GET /v2/brokers/routing`;
`POST /v2/brokers/routing`. Changed: `POST /v2/oms/orders` (account ids + order types, 18.4), `POST
/v2/incubation` (`account_id` = the route), `GET /v2/settings/keys` (two new key names), AccountSummary (+`broker`
fields). Every GET touches the heartbeat. Exact JSON: §18.10.

### 18.10 Backend notes — exact JSON (binding for the WPF Settings → Brokers view + broker status)

Written FIRST (2026-10-18); deviations found while implementing are listed in §18.11. Numbers are JSON numbers
or null (never NaN); money USD; times UTC ISO seconds.

**BrokerStatus**:
```json
{"id": "alpaca-paper", "broker": "alpaca_paper", "label": "Alpaca paper account",
 "venue": "Alpaca — paper trading", "instruments": "US stocks and ETFs", "paper": true, "live": false,
 "endpoint": "https://paper-api.alpaca.markets",
 "sdk": {"name": "alpaca-py", "installed": true, "version": "0.44.0", "message": null},
 "keys": [{"name": "alpaca-paper-key-id", "label": "Alpaca paper API key ID", "present": true, "error": null},
          {"name": "alpaca-paper-secret-key", "label": "Alpaca paper API secret key", "present": true, "error": null}],
 "connection": {"status": "never_connected|connected|disconnected", "since": "…|null", "by": "user|null",
                "reason": "…|null"},
 "link": {"state": "ok|waiting|auth_failed|keys_missing|not_installed|error|idle", "attempt": 0,
          "next_retry_at": null, "last_ok_at": "…|null", "last_error": null,
          "message": "Connected · last answer 12:03:55 UTC"},
 "reconciliation": {"status": "ok|mismatch|unreachable|never", "id": 12, "at": "…|null", "trigger": "cycle",
                    "reason": null, "mismatches": []},
 "halt": {"active": false, "kind": "kill|drawdown|reconcile|null", "reason": null, "at": null},
 "snapshot": {"at": "…|null",
              "account": {"equity": 100000.0, "cash": 100000.0, "cash_basis": 100000.0, "buying_power": 200000.0,
                          "initial_margin": null, "maintenance_margin": null, "unrealized_pnl": null,
                          "status": "ACTIVE", "pattern_day_trader": false, "daytrade_count": 0, "multiplier": 2,
                          "shorting_enabled": true, "trading_blocked": false},
              "positions_count": 0, "open_orders_count": 0},
 "working_orders": 0, "routes": [{"strategy_id": "s-1a2b3c4d5e6f", "name": "SMA trend"}],
 "capabilities": {"order_types": ["market", "limit", "stop", "stop_limit", "bracket"], "tifs": ["day", "gtc"],
                  "fractional": "not used (whole shares)", "shorting": "easy-to-borrow only",
                  "crypto_perps": false, "hours": "regular NYSE session (extended hours not enabled)",
                  "extended_hours": false, "bracket": true},
 "notes": ["Pattern-day-trader protection applies below $25,000 equity.", "…"],
 "terms": {"summary": "…", "verify": ["…"]},
 "live_guard": {"build_allows_live": false, "message": "Live trading is disabled in this build (stage 12 requires the user's explicit go)."},
 "can": {"test": true, "connect": true, "disconnect": false, "reconcile": false, "resolve": false, "kill": false}}
```
`snapshot.account` is null before the first successful read; for `kraken-demo` it carries `equity` (portfolio
value), `cash` = `cash_basis` (collateral), `buying_power` (available margin), `initial_margin`,
`maintenance_margin`, `unrealized_pnl`, and null for the Alpaca-only keys. `sdk.version` null when unknown.

**GET /v2/brokers** → `{"brokers": [BrokerStatus, BrokerStatus], "paper_only": true, "notice": "Paper/demo
trading only · simulated money · not investment advice", "live_trading": {"enabled": false, "message": "Live
trading is disabled in this build (stage 12 requires the user's explicit go)."}}`.

**GET /v2/brokers/{id}** → `{"broker": BrokerStatus, "positions": [BrokerPosition], "open_orders": [BrokerOrder],
"reconciliations": [Reconciliation] (newest 20), "events": [{id, ts, kind, symbol, title, body}] (newest 50)}` —
from the LAST snapshot (no broker call; `snapshot.at` says when).
BrokerPosition = `{symbol, broker_symbol, qty, side: long|short, avg_price, mark_price, market_value,
unrealized_pnl, liquidation_price, initial_margin, maintenance_margin, leverage}` (null where the broker gives
none). BrokerOrder = `{broker_order_id, client_order_id, symbol, broker_symbol, side: buy|sell, qty, filled_qty,
type, limit_price, stop_price, tif, status, broker_status, created_at, known: bool}` (`known` = an OMS order).

**POST /v2/brokers/{id}/test {}** → 200 `{"ok": bool, "endpoint": "…", "paper": true, "checks": [{"name":
"SDK installed", "ok": true, "message": null}, {"name": "Keys present"…}, {"name": "Paper/demo endpoint"…},
{"name": "Account readable"…}, {"name": "Positions readable"…}, {"name": "Open orders readable"…}, {"name":
"Markets loaded" (kraken)…}], "flat": bool, "cash_basis": number|null}` (a failed check names why; later checks
"Skipped — <reason>").

**POST /v2/brokers/{id}/connect {confirm, reason}** → 200 `{"broker": BrokerStatus, "reconciliation":
Reconciliation}`; 400 confirm ≠ `CONNECT <ID IN CAPITALS>`; 409 `{error, code: "test_failed" | "not_flat" |
"already_connected" | "too_small" | "reconcile_mismatch", checks?, positions?, open_orders?}`.
**POST /v2/brokers/{id}/disconnect {reason}** → 200 `{"broker"}`; 409 `{error, code: "not_connected" | "not_flat"
| "working_orders" | "incubating" | "routed"}`.

**Reconciliation**:
```json
{"id": 12, "account_id": "alpaca-paper", "at": "…", "trigger": "start|cycle|connect|manual|resolve",
 "status": "ok|mismatch|unreachable",
 "positions": [{"symbol": "SPY", "expected": 0.0, "broker": 10.0, "ok": false}],
 "cash": {"basis": "cash|collateral", "expected": 1000.0, "broker": 1000.0, "diff": 0.0, "tolerance": 1.0,
          "ok": true},
 "open_orders": {"unknown_at_broker": [BrokerOrder], "missing_at_broker": [{"client_order_id": "…",
                 "state": "accepted"}], "ok": true},
 "mismatches": [{"kind": "position|cash|unknown_order|missing_order", "symbol": "SPY|null", "message": "…"}],
 "reason": "Reconciliation mismatch: …" , "error": null, "halted": true}
```
`unreachable` has `error` (the reason) and empty comparisons. `GET /v2/brokers/{id}/reconciliations?limit=1..200
(20)` → `{"reconciliations": [...]}`; `POST /v2/brokers/{id}/reconcile {}` → `{"reconciliation", "broker"}`.

**POST /v2/brokers/{id}/resolve {action: "recheck" | "cancel_unknown_orders" | "accept_broker", reason: 3..500,
confirm?: "ACCEPT BROKER VALUES"}** → 200 `{"resolution": {id, ts, action, reason, detail}, "reconciliation",
"broker"}`; 409 `{error, code: "nothing_to_resolve" | "not_connected" | "unreachable"}`.

**POST /v2/brokers/{id}/kill {reason?: ≤ 200}** → 200 (always) `{"account_id", "ok": bool, "error": null|str,
"cancelled_at_broker": 2, "oms_cancelled": 1, "orders": [OmsOrder | {"symbol", "state": "error",
"reject_reason"}], "halt": {...}, "note": "…|null"}`.

**GET /v2/brokers/{id}/instrument?symbol=SPY** → `{"symbol": "SPY", "broker_symbol": "SPY" | "PF_XBTUSD",
"ccxt_symbol": null | "BTC/USD:USD", "tradable": true, "fractionable": true|null, "shortable": true|null,
"easy_to_borrow": true|null, "min_qty": 1.0, "qty_step": 1.0, "tick_size": 0.01|null, "contract_size":
null|1.0, "message": null}` (calls the broker; 400 bad symbol / wrong venue; 503 broker unreachable).

**Live guard** `GET /v2/brokers/{id}/live-guard` →
```json
{"id": "alpaca-paper", "build_allows_live": false,
 "message": "Live trading is disabled in this build (stage 12 requires the user's explicit go).",
 "settings": {"live_flag": false, "capital_cap_usd": null, "updated_at": null, "updated_by": null, "reason": null},
 "confirmation_phrase": "GO LIVE ALPACA-PAPER WITH REAL MONEY",
 "conditions": [{"id": "live_flag", "label": "Live flag switched on for this broker", "ok": false, "message": "…"},
                {"id": "typed_confirmation", "label": "Typed confirmation", "ok": null, "message": "checked per request"},
                {"id": "strategy_live", "label": "Strategy in state Live", "ok": null, "message": "checked per request"},
                {"id": "capital_cap", "label": "Capital cap set", "ok": false, "message": "…"}]}
```
`PUT /v2/brokers/{id}/live-guard {live_flag: bool, capital_cap_usd: number|null, reason: 3..500}` → the same
payload (audited). `POST /v2/brokers/{id}/live-guard/check {strategy_id, confirm}` → 200 `{"allowed": false,
"failed": ["live_flag", …], "conditions": [… all four with ok true|false], "refusal": "Live trading is disabled in
this build (stage 12 requires the user's explicit go)."}` (audited; never `allowed: true`).

**Routing** `GET /v2/brokers/routing` → `{"default_account_id": "paper-main", "routes": [{"strategy_id",
"name", "state", "universe": "equity|crypto|mixed", "account_id": "paper-main", "since": null, "by": null,
"reason": null, "allowed_accounts": ["paper-main", "alpaca-paper"], "running_incubation": "inc-…|null"}]}`
(strategies in state validated / incubation / live, plus any with a route row). `POST /v2/brokers/routing
{strategy_id, account_id, confirm: "ROUTE TO ALPACA-PAPER", reason}` → 200 `{"route": {...one entry}}`; 400
confirm; 404 strategy; 409 `{error, code: "not_connected" | "halted" | "not_reconciled" | "wrong_venue" |
"incubating" | "not_flat"}`.

**AccountSummary** (§17.11) of a broker account: `broker` = `alpaca_paper | kraken_demo`, `origin` =
`"connected"`, plus `"broker_link": {"state", "message"}` and `"reconciliation": {"status", "at", "reason"}`
(internal accounts: both null). **OmsOrder** adds `order_type`, `limit_price`, `stop_price`, `legs: [{…}]`,
`parent_client_order_id`, `broker_status`. **GET /v2/settings/keys** lists `kraken-demo-api-key` and
`kraken-demo-api-secret` (status only).

### 17.13 Stage 7 WPF notes (implemented 2026-10-18: rail entry "Portfolio & Trading" — bound to §17.11/§17.12)

Static checks only (no dotnet on the VM — the user builds). PAPER ONLY: the view has no live path and no kill button of its own.
- **Files:** `Platform/PlatformClient.Trading.cs` (all 28 stage-7 routes except `POST /v2/oms/kill` and the equity route, DTOs exactly as
  §17.11/§17.12, computed properties `[JsonIgnore]`, `DescribeFailure` = the `error` text + the 400 `errors {field: msg}` / 409 `blockers`
  verbatim, `ReadKillHalf`), `Platform/TradingView.cs` + `TradingView.Sections.cs` (the view), `Platform/TradingDialogs.cs` (reason,
  incubation start, risk-limit change, migration, manual order), `HedgeFundWindow.Trading.cs` (`EnsureTradingView`); edited:
  `HedgeFundWindow.xaml` (rail entry `NavTradingButton` "Portfolio & Trading" between Loop and Research, host `TradingHost`, Overview banner
  `LegacyBanner`, kill-switch name/tooltip), `HedgeFundWindow.xaml.cs` (rail switch, `Shutdown` on close, kill dialog + per-half result
  `KillResultText`: title "Both engines halted" only when both halves confirmed and no closing order ended `error`/`rejected`, else
  "Halted — some closing orders failed" / "Kill switch — check the results"; `UpdateLegacyBanner`), `Fund/FundClient.cs` (`FundKillResponse.error/platform`, `FundState.legacy_account`).
  `Platform/_commit_test.txt.stale` is a tiny file-transfer test of the implementing agent (safe to delete).
- **Kill switch (verified against the code, §17.1):** `POST /fund/kill` → `engine.kill(propagate=True)` → the old engine's halt +
  `platform_kill_fn` = `control.kill_platform` (all platform accounts: auto trading off, cancel all, close all) — so the ONE pinned toolbar
  button covers both; it is NOT duplicated in the view. Its confirmation now lists both halves; the result dialog shows each half separately
  (old engine: `error` or the closing orders; platform: `ok/error` and per account cancelled count, closing orders, `error`; a missing
  `platform` = older backend → said so). The view's header shows the scope (accounts named) and the shared dead-man heartbeat.
- **Coexistence (§17.1):** header strip — old account `legacy_account.status` chip (ACTIVE · OWNER: OLD ENGINE / MIGRATING · FROZEN /
  MIGRATED · OWNER: PLATFORM with date + id) next to the platform accounts and the rule "one owner per account". The Overview shows
  `LegacyBanner` (from `/fund/state.legacy_account`) once the account is migrating/migrated; its own trading controls are unchanged and get
  the backend's 409 text.
- **Polling:** `GET /v2/portfolio/state` + the selected section every 5 s, only while the view is visible (each GET touches the shared
  heartbeat); a section is re-rendered only when its JSON signature changed (keeps focus/expanders); a failed state call keeps the last
  state with "Connection lost (attempt n)". Valuation paused (`account null`, `valuation.ok false`) → banner + empty values, never a stored
  price.
- **Header controls:** auto trading button (`POST /v2/oms/autotrade`): switching ON asks for confirmation (limits per account, off-conditions:
  kill, drawdown kill, backend start, heartbeat minutes; notice (c) of §15 stage 10); OFF is immediate; disabled while an account is halted
  (the backend's 409 is shown otherwise). Runtime line from `state.runtime` + "▶ Run cycle now" (`POST /v2/oms/runtime/run`).
- **Portfolio tab:** per account card — chips (PAPER, origin, HALTED·kind / TRADING ALLOWED, VALUATION PAUSED), equity, cash, buying
  power, long/short MV, gross/net exposure % of equity (WPF-computed from AccountView: (long ± |short|) / equity), day/total P&L, peak,
  margin excess, funding; **gauges**: daily loss equities (NY day) and crypto (UTC day) = max(0, −pnl_pct) vs `limit_pct`, drawdown vs
  `kill_pct`; chips OK / NEAR LIMIT (≥ 50 % of the limit — UI threshold, confirmed by the user 2026-10-24) / BLOCKED until … (backend `blocked`) /
  KILLED · HALTED (`halt.kind`) / NO REFERENCE YET / NOT VALUED; each gauge is focusable with name + item status. Halt box with "Resume…"
  (reason dialog; says auto trading stays off and the peak is rebased). Positions with by-source split, working orders, "New paper
  order…", runtime cycles expander (`GET /v2/oms/runtime`). No account → "Open platform paper account…" (capital 1,000–100,000,000, label).
- **Strategies tab** (account selector): allocation panel (latest allocation id/time/actor/reason/pool/unallocated, transfers, params,
  next rebalance, "Rebalance now…" with reason) + one card per source: weight (+ target capital, vol and `vol_source`, mean corr, penalty,
  CAPPED, note), capital in, cash, equity, P&L realised/unrealised/total, costs by kind, exposure, positions, ledger expander (newest 200).
  **State chips** (derived, display only): INCUBATING · DAY x OF N (x = `progress.sessions_elapsed`, N = `length_sessions`; tooltip says N is
  fixed) / INCUBATION COMPLETE · result / ACTIVE (live/scaled — not reachable in stage 7) / RELEASED · state; plus PAUSED · ACCOUNT HALTED
  or PAUSED · AUTO TRADING OFF (decides, sends no orders). Stage 9's decay pause should add a backend state field — the chip then reads it.
- **Netting tab:** per instrument (positions ∪ latest targets ∪ working orders): account position, last price, per contributor current
  qty (`by_source`), target weight of its latest recorded decision (bar date, status; `force` → 0), "≈ target qty" = the runtime's
  formula sign(w)·round_down(|w·source equity / last price|) with the price shown in the view (no price → "—", "no current quote in the
  view" — never a stand-in), Council/Unallocated holdings "held"; net ≈ target vs current → residual, working orders with their
  allocations; the transient latest failed decision (`id null`) is shown verbatim. The text says the runtime re-computes with its live quote
  and the checks — the view is explanatory, not the netting engine. (Optional later: a backend "netting plan" route would replace the
  WPF approximation.)
- **Orders tab:** filters account (server), state (server, 8 states), origin (client: runtime/manual/kill/drawdown/margin), symbol
  (client, contains); newest 200; ListBox (arrows/Enter) with time, account, origin, side·qty·symbol, filled @ avg, state, reject reason
  verbatim (else the client order id). Detail: selectable client order id, broker order id, execution, attempts, tif, timestamps, reason,
  allocations, **pre-trade checks** ✓/✗ with the message verbatim, state timeline (`events` from → to, note, data), fills (cursor stream
  `GET /v2/oms/fills`, kept per account, ≤ 5,000), "Cancel order…" for non-terminal orders (409 verbatim).
- **Manual order dialog:** symbol, side buy/sell/short/cover, qty or USD notional, booking source (paper-main: any source, default
  `_unallocated`; paper-council: `council-of-ais` only), optional reason; ONE `client_order_id` per dialog = `Guid.NewGuid().ToString("N")`
  (32 hex, matches the pattern, no reserved prefix) → a retry after a time-out returns the stored order; the result popup lists the
  checks (a failed check = order `rejected`).
- **Incubation tab:** intro (only the user starts/aborts; length immutable; live off), runs list, detail (chips incl. LENGTH N · FIXED,
  result, LIVE-ELIGIBLE (LIVE IS OFF); realised / expected / band / tracking error / traded·missed; result reasons; decisions incl. the
  transient failure; events), "Abort run…" (danger reason dialog: back to validated, positions closed to 0, a new run chooses its length
  again). **Start dialog** (Ctrl+I): validated strategies only, eligibility from `GET /v2/incubation/eligibility` (reason verbatim, calendar,
  first session), length box (backend min/max/default), amber callout that the length can never be changed (trigger), reason 3–500, a
  required tick "I understand that the length of N sessions is fixed …" — Start is enabled only when eligible + length valid + tick +
  reason; 409/400 verbatim inline.
- **Migration tab:** status (legacy status/owner/date, migration record + summary, can_migrate), "Dry run" (`POST …/dry-run`, report:
  blockers code + message verbatim, counts, cash/start/peak, halted/autopilot/pending, positions, paper-council limits, broker params,
  note), "Migrate…" enabled only when `can_migrate` and a dry run without blockers is ≤ 30 min old → dialog (one-way switch explained,
  typed `MIGRATE`, reason; 409 blockers verbatim).
- **Risk limits tab** (account selector): groups post-trade / pre-trade / allocator from `bounds` (label, unit, default, range, step); the
  editor is NOT refreshed by the poll (typed values survive); client check mirrors the bounds and whole-number keys (the backend still
  decides, never clamped); "Review and save…" → dialog listing old → new (default); a LOOSENED limit shows a "Loosened limit" warning
  (post-trade ones marked "hard") and needs an extra tick "I confirm loosening …" (tightening only needs the audit note), audit note 3–500 → `PUT /v2/portfolio/settings`; field errors verbatim. Settings version/updated_at/updated_by shown (the
  backend exposes no history list).
- **Keyboard / screen readers:** tabs Ctrl+1…7 (arrow keys inside the tab row, Tab enters once), F5 reload, Ctrl+N new order, Ctrl+I start
  incubation; every button/list/input has `AutomationProperties.Name` (+ HelpText where useful), status texts are live regions, error
  blocks assertive. All backend/strategy texts are plain `TextBlock.Text` (untrusted).
- **Click-test (user):** (1) rail → Portfolio & Trading: header shows ACTIVE · OWNER: OLD ENGINE, kill scope, auto trading OFF; (2) open
  paper-main (100,000) → card, gauges OK; (3) Incubation → Start… with a validated strategy: tick required, length 20–250, start → run
  INCUBATING · DAY 0 OF N; (4) Strategies: the source card shows weight + chips; (5) Auto trading ON (confirm) → Run cycle now; (6) New
  paper order (e.g. buy 1 SPY) → Orders: state timeline, checks ✓, fill; try a too large order → rejected with the check text; cancel a
  working order; (7) Risk limits: change daily loss 2 → 1.5 with a note → version +1; out-of-range value → inline error; (8) Migration: dry
  run → report/blockers (old autopilot ON is a blocker); migrate only if wanted (one-way) → Overview shows the migrated banner;
  (9) toolbar ■ Kill switch → dialog lists both halves → result shows old engine + each platform account; Resume… per account here and
  "Resume engine" in the toolbar; (10) close/reopen the window: no poll while hidden.

### 18.11 Stage 8 backend notes (implemented 2026-10-18: §18.1–§18.10, backend part — for the WPF Settings → Brokers view)

All §18.10 payloads are implemented as written, with these additions/deviations (the WPF agent binds to them):
- **Files:** `execution/brokers/{common.py (link state, ack helpers), alpaca_paper.py, kraken_demo.py, mirror.py
  (BrokerAccount, MirrorBroker, MirroredAdapter, fill booking), reconcile.py, service.py (test/connect/disconnect,
  status, routing, kill, poller), live_guard.py}`, `base.py` (interface extended), `api/broker_routes.py` (15 routes,
  mixin of `V2Api`, appended to `routes.ROUTES`), `storage/db.py` **v16** (8 tables + 6 `oms_orders` columns),
  `secrets.py` (2 keys), changes in `execution/{accounts.py, oms.py, control.py}`, `portfolio/{checks.py,
  ledger.py, views.py}`, `live/runtime.py` (reconcile in housekeeping), `api/trading_routes.py`, `main.py` (task
  `sp-brokers`), `requirements.txt` (`ccxt==4.5.84`).
- **ccxt on the VM:** not installable here (no package index reachable from the VM or the container) → the
  adapter is behind the lazy import guard (state `not_installed`, "ccxt is not installed …") and tested with a fake
  ccxt. ccxt 4.5.84 lists Python 3.10–3.14; its native dependencies (aiohttp, yarl, cryptography, pycares via
  aiodns) need cp314 wheels on the user's PC — **verify** the first `pip install` after this change (a failing
  install marks the whole backend environment `failed` in run_hedgefund_backend.ps1).
- **Kill switch while the broker is unreachable** (refines §18.6): the broker's positions cannot be read, so NO
  closing orders are guessed from the mirror (no stand-in); the halt is set, `note` = "The broker's positions could
  not be read — the account is halted and the positions are closed automatically as soon as the broker answers.",
  pa_meta `kill_pending` is set and the 15 s poller completes the flatten from the broker's positions when the link
  recovers. `POST /v2/oms/resume` answers 409 `kill_pending` meanwhile (and 409 `reconcile_mismatch` while a
  reconcile halt holds). A position the platform cannot map (Kraken non-`PF_`) shows as symbol
  `UNMAPPED:<ccxt symbol>`; the kill reports it as `{state: "error", reject_reason: "… close it at the broker"}`.
- **Reconcile halt is sticky:** a later `ok` reconciliation in a cycle does NOT clear the halt (status shows `ok`,
  `halt.kind` stays `reconcile`) — only `POST …/resolve` (any action ending ok) or a reconnect clears it. One alert
  (event + audit) per halt, not per cycle. Triggers also include `poll` (the 15 s poller reconciles when the last
  reconciliation is ≥ 5 min old) — `broker_link` accepts reconciliations ≤ 15 min old.
- **OMS on a broker:** an order whose send failed ambiguously stays `submitted` (never re-sent until a lookup
  answered "unknown"); while the link backs off, `GET` routes work but `oms.sync` / the runtime step for that
  account report "Waiting for broker (attempt n) — next try HH:MM:SS UTC"; new orders are `rejected` by
  `broker_link` with that text. Kill/drawdown orders always try the broker (no backoff). Cancel on a broker is
  asynchronous: `POST /v2/oms/orders/{id}/cancel` returns the order (state may still be `accepted` with an event
  "cancel requested at the broker"; a fill that raced the cancel ends `filled`). A cancelled/expired order keeps
  its partial `filled_qty`.
- **OmsOrder** (all accounts): `order_type`, `limit_price`, `stop_price`, `bracket` (`{take_profit:
  {limit_price}, stop_loss: {stop_price, limit_price?}}` or null), `legs` (`["<cid>.tp", "<cid>.sl"]` for a
  bracket, else `[]`; the legs are OMS orders with `parent_client_order_id`), `broker_status`. Manual non-market
  orders only (`type` refused for runtime/protective origins); a bracket needs side buy|short; `notional` only with
  market. Runtime orders on `kraken-demo` are `gtc`.
- **Extra pre-trade checks** on broker accounts (in `checks`): `venue`, `position_side` (buy while short → "cover
  it first", short while long → "sell it first", sell/cover above the held quantity), `broker_link`.
- **Fees / cash:** Alpaca fills book fee 0; Kraken fills book the reported fee or the taker-rate estimate (0.05 %
  on PF_XBTUSD in the fake — the real rate comes from ccxt's market). Accept-broker books `broker_adjustment` cash
  rows (attributed to `_unallocated`, sub-ledger kind `other`) and position changes as sub-ledger `cross` rows (the
  holders "sell" to `_unallocated` at the broker's average price) — Σ sub-ledger cash = account cash holds.
- **Timestamps** of reconciliations / fill bookings use the platform clock (`accounts.now()`), like the OMS.
- **Defaults (default — user confirm):** connect only when the broker account is flat and ≥ $1,000; cash
  tolerances max($1, 0.01 % of equity) Alpaca / max($5, 0.25 %) Kraken; link backoff 15 s · 2^(n−1) ≤ 300 s, auth
  re-check 5 min; poller 15 s; reconciliation max age 15 min; Kraken stop-market refused (stop_limit only), closed-
  order lookback 7 days; whole shares only on alpaca-paper; extended hours off; trade-update stream not used.
- **Known limits:** the runtime's end-to-end incubation on a broker account is covered by the OMS/reconcile tests
  but not by a full runtime test (the runtime code path is the stage-7 one with the broker adapter); the Kraken
  funding import, the flex-balance fields (`balanceValue`, `portfolioValue`, `availableMargin`) and fee reporting
  follow ccxt's / Kraken's documented shapes but are **unverified against the live demo** (no network in this
  build); Alpaca paper's handling of dividends / regulatory fees is unverified — any difference surfaces as a
  reconciliation mismatch (halt), never silently. IBKR / MT5 adapters not built (TODO; interface in `base.py`).
- **What the user must provide:** Kraken Futures demo account (demo-futures.kraken.com, e-mail sign-up) → API key
  (public + private) with full access on the DEMO → Hedge Fund → Settings → Keys (`kraken-demo-api-key`,
  `kraken-demo-api-secret`); the demo's multi-collateral wallet funded with demo USD. Alpaca: the existing paper
  keys; the paper account flat (reset it at alpaca.markets if needed) before the first connect.
- **Tests** (offline, temp DBs, fakes): `test_sp_broker_adapters.py` (14: both adapters against fake SDKs),
  `test_sp_broker_oms.py` (15: connect/disconnect, lifecycle, partial fills, rejects, reconnect without duplicates,
  backoff, async cancel, bracket legs, venue/size/TIF), `test_sp_broker_reconcile.py` (14: mismatch halt +
  resolve ×3, sticky halt, tolerance, unreachable, stale, funding import, runtime, kill per broker incl.
  unreachable → completed later, global kill, dead-man), `test_sp_broker_live_guard.py` (6), `test_sp_broker_api.py`
  (5); fixtures `sp_broker_testkit.py`. `test_sp_trading_api.py` updated (AccountSummary keys).

### 17.14 Review fixes 2026-10-18 (independent review of stage 7: H1, M1, M2, L2 — backend, code only, no schema change)

- **H1 — a `submitted` order could outlive the kill switch** (a send that hit `BrokerUnavailable` stays `submitted`;
  `cancel()` refused it, `cancel_working` skipped it silently and the next `sync` re-sent it on the killed account).
  Fixed in `execution/oms.py`: `ALLOWED["cancelled"]` includes `submitted`; `cancel()` of a `submitted` order asks
  the adapter (`order(id)`): unknown there → `cancelled` "cancelled before it reached the broker" (never re-sent);
  known → state synced, a still-working order is cancelled AT THE ADAPTER (broker accounts: request + read back, as
  for `accepted`); adapter unreachable → best-effort adapter cancel, then `cancelled` "broker unreachable … never
  re-sent" (for a real broker a later appearance is a reconciliation mismatch, §18; a late fill is still booked by the
  mirror). `cancel_working` never stops on one failing order (logged). **Every re-send** (`_send` of a `submitted`
  order from `sync`) first re-checks: account halted → `cancelled` "not re-sent: the account was halted meanwhile
  (…)"; runtime order while auto trading is off → "not re-sent: auto trading was switched off meanwhile"; risk-
  increasing order while the class day's daily-loss block is latched → "not re-sent: the daily loss limit was reached
  meanwhile (at …)". Protective orders (kill/drawdown/margin) are re-sent as before. `sync()` adds `not_resent: n`.
  Covers the kill switch, auto trading off and the heartbeat switch (all go through `cancel_working`). Stage-8
  adapters checked: Alpaca paper / Kraken demo / mirrored `cancel(client_order_id)` look the id up and return False
  for an unknown id — the new `submitted` path asks `order()` first, so they are never asked to cancel an unknown id
  as a success. Tests: `test_sp_oms.SubmittedOrderSafetyTest` (kill, auto off, heartbeat off, re-send re-check,
  cancel of a submitted order already filled at the broker → `filled`, not falsely cancelled);
  `test_crash_between_store_and_send` now turns auto trading on before expecting the runtime re-send.
- **M1 — the 2 % daily-loss block now holds until the next NY day (equity) / UTC day (crypto).** `portfolio/risk.py`:
  `latch_key(cls, now)` = `day_loss:<cls>:<date the block lifts>` (the runtime's key since stage 7), `latched()`,
  `record_block()` (meta key + account event + audit `post_trade_halt`, once per class day — used by the runtime AND
  by `oms.place` when a pre-trade check sees the limit hit); `day_loss()` returns `blocked: true` while latched and an
  additive key **`latched`** (bool) per class (`AccountSummary.day_loss.equity|crypto.latched`). `checks.run` blocks
  risk-increasing orders while the key exists: message "daily loss limit was reached today (at <ISO>); the block holds
  — no new <cls> orders until <ISO>". Reducing and protective orders still pass. Test:
  `test_sp_pretrade.test_daily_loss_block_holds_when_the_price_recovers_the_same_day`.
- **M2 — migration race.** `execution/migration.py`: `run()` is serialised by a module lock (a concurrent request →
  409 `migration_blocked`, `blockers: [{code: "migration_running", …}]`); `account_exists` / `already_migrated` are
  re-checked AFTER the freeze; a failed copy is 503 `migration_failed` ("… nothing was moved; the old paper account is
  active again") or, when a migration record exists meanwhile, 409 `migration_blocked` — never a generic error; once
  a `pf_migrations` record exists the newsroom flag is never cleared (set to `migrated` with that record's id), and
  `recover()` completes with ANY record (it used to un-freeze when the ids differed). Tests:
  `test_sp_trading_parity.MigrationTest.test_concurrent_request_is_refused_and_never_unfreezes`,
  `test_failed_copy_unfreezes` (now expects 503 `migration_failed`).
- **L2 — allocator floor (definition, §17.4 addendum):** when there are managed strategies but none gets a weight > 0
  — the min-weight floor would remove every strategy with a volatility estimate, or no strategy has an estimate —
  the result is **no allocation**: `allocator.weights_result()` → `status: "no_allocation"` with a reason; the
  rebalance moves NO capital (capital stays where it is; the release of strategies that left incubation still
  happens), stores the record and shows it. No other fallback (no lowering of the floor, no equal weights). Additive
  keys in `GET /v2/portfolio/allocation` (and the rebalance answer): `allocation.status: "ok" | "no_allocation"`,
  `allocation.status_reason: null | str` (e.g. "No allocation: all 3 strategies with a volatility estimate fall below
  the minimum weight 0.5 (…) — lower the minimum weight in the allocator settings, then rebalance. The capital stays
  where it is."), `unallocated_weight: null`, each `weights[].weight 0`, `target_capital null`, `note` "weight 0.3333
  below the minimum 0.5 — every strategy is below it, so no allocation was made"; `SourceSummary.allocation_weight`
  is null then. WPF: show `status_reason` as a notice above the weights when `status == "no_allocation"`. Tests:
  `test_sp_portfolio.AllocatorTest.test_floor_removing_every_strategy_is_no_allocation`,
  `NoAllocationRebalanceTest`.
- **WPF (2026-10-18, bound to the keys above):** Strategies tab shows `allocation.status == "no_allocation"` as an amber notice
  "No allocation — no capital moved …" + `status_reason` verbatim above the weights (weights "— (no allocation made)"; the
  rebalance answer pops the same text); `TradingAllocation.status/status_reason` + `[JsonIgnore] IsNoAllocation`; the
  daily-loss chip reads "BLOCKED (HOLDS) until …" when `latched`; a failed migration shows `PlatformClient.MigrationFailureText`
  (409 migration_blocked: "Refused …" + every blocker code/message, `migration_running` named as a concurrent request; 503
  migration_failed: "nothing was moved; the old paper account is active again"; backend text verbatim) and drops the stale dry
  run; cancel/expire reasons (`reject_reason`, e.g. "not re-sent: the account was halted meanwhile (…)") are shown in the blotter
  (amber) and the detail as "Cancel reason: …" / "Expired: …" (red stays for rejects).
- No storage version change (oms state CHECK already allowed every state; transitions are enforced in code). Full
  suite green in split runs; scope guards stage0/mr0/budget0 ok.

### 18.12 Stage 8 WPF notes (implemented 2026-10-18: "Settings → Brokers" — bound to §18.10/§18.11)

Static checks only (no dotnet on the VM — the user builds). PAPER / DEMO ONLY: the app has no live control at all.
- **Placement:** "Settings" = the Hedge Fund toolbar window "⚙ Keys & data" (`Platform/KeysAndDataWindow`, now titled "Keys, data
  sources & brokers"; the backend's texts say "Settings → Keys" / "Settings → Brokers → Routing"). New section **Brokers (paper /
  demo)** between Keys and Data sources. Portfolio & Trading gets a "Brokers…" header button (Ctrl+B) that opens / focuses that
  window scrolled to Brokers (`TradingView.BrokersRequested` → `HedgeFundWindow.OpenKeysWindow(brokers: true)`).
- **Files:** new `Platform/PlatformClient.Brokers.cs` (12 routes of §18.9 — NOT `PUT …/live-guard` and NOT `POST …/live-guard/check`,
  deliberately: no code path can switch live on; DTOs exactly §18.10, uncertain scalars as `JsonElement`, computed properties
  `[JsonIgnore]`; `ReadBrokerRefusal` parses a 409 body `{error, code, checks?, positions?, open_orders?}`), `Platform/BrokerDialogs.cs`
  (`BrokerText` helpers, `BrokerConfirmDialog` typed phrase + reason, `BrokerResolveDialog`, `BrokerRouteDialog`),
  `Platform/KeysAndDataWindow.Brokers.cs` (the section); edited: `KeysAndDataWindow.xaml` (panel `BrokersPanel`/`BrokersHost`),
  `KeysAndDataWindow.xaml.cs` (Kraken key pair, `InitBrokers`, first load), `CredentialStore.cs` (constants `KrakenDemoApiKey/Secret`),
  `PlatformClient.Trading.cs` (AccountSummary `broker_link`/`reconciliation`, kill account `broker {ok, error, cancelled_at_broker,
  note}`, incubation start without `account_id`), `TradingView.cs`, `TradingDialogs.cs` (incubation intro), `HedgeFundWindow.xaml.cs`
  (`OpenKeysWindow`, toolbar kill dialog + result), `HedgeFundWindow.Trading.cs`.
- **Keys:** the two Kraken rows come from `CredentialStore.Secrets` (status only, Set…/Remove, no .env import); "Set…" on either row
  asks for key (public) + secret (private) together (like the Alpaca pair), text: demo-futures.kraken.com → Settings → Create key,
  DEMO only. Values never shown, logged or sent; the audit call carries name + action only.
- **Cards** (one per adapter): *internal paper broker* (paper-main / paper-council from `GET /v2/portfolio/state`: always available,
  reconciliation not applicable, equity/cash/buying power or "not valued" verbatim, halt); *alpaca-paper* and *kraken-demo* from
  `GET /v2/brokers`: chips PAPER/DEMO · connection · link (`WAITING FOR BROKER (ATTEMPT n)` from `link.attempt`, KEYS REJECTED, KEYS
  MISSING, SDK NOT INSTALLED, …) · reconciliation (`RECONCILIATION OK` / `HALTED · RECONCILIATION MISMATCH` / `HALTED · RESOLVE
  REQUIRED (last check OK)` for the sticky halt of §18.11 / UNREACHABLE / NOT RECONCILED YET) · other halts; endpoint; SDK —
  `sdk.installed false` → `sdk.message` verbatim in a red callout (ccxt: "ccxt is not installed in the HedgeFund backend environment
  (restart the app so it installs requirements.txt)"); keys present/missing (status only); `link.message` verbatim + next automatic
  try / last answer / last error; connection since/by/reason; **account at the broker** = the LAST snapshot with its time ("updated
  by reconciliations — not a live quote"; null → "No broker snapshot yet … No stand-in values"; Alpaca: status, PDT, day trades,
  multiplier, shorting, trading blocked; Kraken: portfolio value, collateral, available margin, initial/maintenance margin,
  unrealised P&L); reconciliation box (reason + every mismatch message verbatim, "Nothing is corrected automatically"); routes;
  buttons **Reconcile now** (`can.reconcile`), **Resolve…** (`can.resolve`), **Test connection**, **Connect…/Reconnect…**
  (`can.connect`), **Disconnect…** (`can.disconnect`), **■ Kill this broker…** (`can.kill`); last action message; test result
  (checks ✓/✕ with messages, cash basis, flat; not flat → positions/open orders listed + the precondition text); expanders "Broker
  details" (`GET /v2/brokers/{id}` while open: positions, open orders with `known`, newest 20 reconciliations, newest 50 events) and
  "Capabilities, notes and terms (verify)". A red banner on top says the toolbar ■ Kill switch is the GLOBAL one.
- **Connect…:** explains first connect (flat, no open orders, ≥ $1,000 = opening balance; never imports positions; Alpaca: reset
  the paper account at alpaca.markets) vs reconnect (mirror continues, connect reconciliation must be OK), then reconciliation
  every start/cycle/5 min with halt on mismatch, strategies stay on paper-main until routed; typed `CONNECT <ID>` + reason; a 409
  (`test_failed` / `not_flat` / `too_small` / `reconcile_mismatch` / `already_connected`) is shown inline verbatim with the failed
  checks and the positions / open orders found. **Disconnect…:** reason dialog; 409 verbatim on the card.
- **Resolve…** (`BrokerResolveDialog`): intro (halted, no auto-correction, only OK clears the halt, auto trading stays off), the
  mismatch reason + every mismatch verbatim, radio recheck / cancel_unknown_orders / accept_broker; accept shows the consequences
  (positions/avg prices/cash adopted, one adjustment to Unallocated, OMS orders not open → cancelled, not undoable) and needs the
  typed `ACCEPT BROKER VALUES`; reason 3–500. Result on the card: OK → "reconcile halt cleared"; else the new status + reason.
- **Routing** (panel below the cards, `GET /v2/brokers/routing`): per strategy name/id, `→ ACCOUNT` chip, state, universe,
  allowed accounts, since/by/reason, running incubation; **Change route…** → `BrokerRouteDialog`: targets paper-main /
  alpaca-paper / kraken-demo — disabled with the reason when the venue rule forbids it or the broker is not connected / halted /
  not reconciled OK (display only; the backend decides), the consequences of the chosen target (internal simulation vs Alpaca paper
  rules, PDT, reconciliation halts, "waiting for broker", never sent twice; Kraken demo perps, funding, reduce-only, leverage
  untouched), typed `ROUTE TO <ID>` + reason; 409 verbatim. **Incubation start no longer sends `account_id`** (the backend uses the
  route; sending paper-main for a routed strategy would be 409 `route_mismatch`).
- **Live guard** (panel, `GET /v2/brokers/{id}/live-guard` on load / refresh): chip LIVE TRADING: OFF, the fixed text "Live trading is
  disabled in this build (stage 12 requires your explicit go)", per broker a read-only checklist (focusable rows with name + item
  status): "Build allows live trading — OFF (hard-coded)", the four conditions (live flag, typed confirmation with the phrase, strategy
  Live, capital cap) each OFF (an `ok: true` would read "SET — no effect"), capital cap "OFF — not set" or the value "no effect";
  `build_allows_live: true` from a backend → red "Unexpected … do not trade". No control.
- **Per-broker kill:** confirmation (this broker only; halt, cancel ALL open orders at the broker incl. unknown ones + the OMS working
  orders, close every position the BROKER reports, reduce-only on Kraken, unreachable → halted now and flattened when it answers;
  resume in Portfolio & Trading), optional reason ≤ 200 → result popup + card message (counts, closing orders, `note`, failures).
  **Toolbar kill** (global, unchanged button): its dialog names the broker half; the result lists per broker account "(n of them at
  the broker)", `BROKER: <error>` (marks the result not confirmed) and the `note`.
- **Portfolio & Trading:** broker account cards add link + reconciliation chips, `broker_link.message` and the reconciliation reason
  verbatim and a "mirrors your Alpaca PAPER / Kraken DEMO account — the broker is the truth" line; a reconcile halt shows
  "Resolve in Settings → Brokers…" instead of Resume (resume would be 409 `reconcile_mismatch`); halt kind "reconciliation mismatch";
  kill-scope text and the accounts sentence mention broker accounts.
- **Polling:** `GET /v2/brokers` + `/v2/portfolio/state` + `/v2/brokers/routing` (+ open detail expanders) every 5 s while the window is
  visible and not minimised, paused while a broker action runs; stops on close. Each GET touches the shared heartbeat (said in the
  status line). Re-render only when the JSON signature changed; the focused control (by automation name) and expanders are kept.
  A failed poll keeps the last state: "Connection lost (attempt n) — showing the last known state …"; older backend (404) → "older than
  stage 8; restart it".
- **Keyboard / screen readers:** Ctrl+B jumps to Brokers (also from Portfolio & Trading), F5 refreshes the whole window; every button,
  chip, card, expander and checklist row has `AutomationProperties.Name`; status / action messages are live regions (errors
  assertive). All broker / backend texts are plain `TextBlock.Text` (untrusted).
- **Finding (backend, not changed here):** `GET /v2/incubation/eligibility` always checks `paper-main` (`incubation.eligibility`
  default) — for a strategy routed to a broker the dialog may say "eligible · account paper-main" while the run starts on the route;
  the start itself is checked against the routed account. Suggest: eligibility uses `service.route_for(sid)`.
- **Verified kill (bound to §18.13, 2026-10-18):** `BrokerKillResult` + `flat`, `pending`, `open_orders_left`, `attempts`, `not_flat
  [{symbol, broker_qty, close_order, close_state, reject_reason}]`; global kill `broker` + `flat`, `pending`, `not_flat`;
  `BrokerStatus.kill {pending, at, not_flat}`. `BrokerText.KillFlatText` (red): "Broker NOT flat — positions left: SYM qty, …" + one
  line per symbol verbatim (close order + state + reject reason), "flat state unknown" for `flat: null`, open orders left, "The
  account stays HALTED; the kill retries automatically every 15 s …". Titles: "halted and flat" ONLY when `flat == true` (per-broker
  popup) — otherwise "NOT flat" / "flat state unknown" / "check the results"; the toolbar result is never "Both engines halted" while a
  broker is not verified flat; the broker card shows "■ Kill pending since …" from `kill`.
- **Click-test (user):** (1) ⚙ Keys & data → Keys: Kraken rows "Not set" → Set… asks key + secret together → ● Set; Remove asks
  first. (2) Brokers: three cards; Kraken without ccxt → red ccxt text verbatim + SDK NOT INSTALLED; Test connection → checks
  (✕ SDK installed, later "Skipped — …"). (3) Alpaca → Test connection → checks ✓, cash basis, flat yes/no. (4) Alpaca with a
  position → Connect… → typed CONNECT ALPACA-PAPER → refused "… holds positions or open orders …" + the positions listed. (5) Flat
  → Connect → card CONNECTED · LINK OK · RECONCILIATION OK, snapshot with time; Portfolio & Trading shows alpaca-paper with chips.
  (6) Routing → Change route… on an equity strategy → kraken-demo disabled (venue rule) → choose alpaca-paper, read the
  consequences, type ROUTE TO ALPACA-PAPER + reason → chip → ALPACA-PAPER; Incubation → Start… → run on alpaca-paper. (7) Make a
  mismatch (buy 1 share directly at alpaca.markets) → Reconcile now → HALTED · RECONCILIATION MISMATCH with "SPY 1 at the broker
  vs 0 expected"; Resolve… → accept needs ACCEPT BROKER VALUES → OK clears the halt. (8) Wi-Fi off → link WAITING FOR BROKER
  (ATTEMPT n) + "next automatic try"; status line "Connection lost (attempt n)" only if the backend itself is unreachable.
  (9) ■ Kill this broker… → confirmation → result popup; toolbar ■ Kill switch → result lists the broker line. (10) Live guard:
  every row OFF + the fixed text; no button. (11) Keyboard: Ctrl+B, Tab through the card, Enter/Space on buttons, Esc closes dialogs.

### 18.13 Stage 8 backend fixes 2026-10-18

- **Incubation eligibility on the route** (finding of the stage-8 WPF agent, §18.12): `incubation.eligibility(sid,
  now, account_id=None)` and `incubation.start(…, account_id=None)` now resolve `account_id` to the strategy's
  route (`brokers.service.route_for`, default `paper-main`) instead of always `paper-main`; `GET
  /v2/incubation/eligibility` therefore reports the routed `account_id` and checks it. For a broker account the
  preconditions are those of `broker_link` + the route: connected (else `account_not_open`), venue rule
  (`wrong_venue`), not halted (`halted`; a reconcile halt says "resolve the reconciliation first"), link ok and last
  reconciliation `ok` ≤ 15 min (`not_reconciled`, with the "Waiting for broker (attempt n) …" text). The capital
  check stays the allocator's (the run's source gets capital from that account's `_unallocated` pool). No payload
  key changed (`account_id` was already in the eligibility payload; the WPF no longer sends `account_id` on start).
  Tests: `test_sp_broker_incubation.py` (3: routed to alpaca-paper — halted / stale / ok → run on alpaca-paper;
  routed to kraken-demo — unreachable / ok → run on kraken-demo; explicit wrong venue, default paper-main).

**Review fixes 2026-10-18** (independent review of stage 8; backend, no schema change):
- **H — verified broker kill** (`service.kill`): halt → cancel OMS working orders and ALL broker orders → bounded
  wait (≤ 10 s, polls 0.5 s) until the broker reports no open orders other than the platform's own working closing
  orders (cancels are asynchronous; Alpaca keeps the quantity of pending-cancel orders and bracket legs) → close
  every position the BROKER reports → wait ≤ 10 s per attempt for fills; rejected closes are re-sent up to 3
  attempts (new ids; never a second close while one works) → VERIFY the broker is flat. Not flat or unreadable →
  `ok: false`, `pending: true`, the account stays halted, pa_meta `kill_pending` holds `not_flat`, and the 15 s
  poller continues one bounded step per tick (closes only where none works) until the broker is flat. Never a
  silent success. **Kill response JSON — new keys** (`POST /v2/brokers/{id}/kill`; also inside the global kill's
  per-account `broker` object, which now carries `ok, error, cancelled_at_broker, note, flat, pending, not_flat`):
  ```json
  {"account_id": "alpaca-paper", "ok": false, "error": "1 order(s) still open at the broker after 10 s (cancel pending)",
   "cancelled_at_broker": 2, "oms_cancelled": 1, "orders": [OmsOrder | {"symbol", "state": "error", "reject_reason"}],
   "halt": {"active": true, "kind": "kill", "reason": "…", "at": "…"},
   "note": "The broker still holds positions (see not_flat) — the account stays halted and the kill retries automatically every 15 s until the broker is flat.",
   "flat": false, "pending": true, "open_orders_left": 1, "attempts": 3,
   "not_flat": [{"symbol": "SPY", "broker_qty": 10.0, "close_order": "kill-1a2b3c4d5e6f", "close_state": "rejected",
                 "reject_reason": "Alpaca refused the order (HTTP 403: insufficient qty available for order …)"}]}
  ```
  `flat`: true (verified flat) | false (positions remain) | null (positions unreadable — `note` = "The broker's
  positions could not be read — …"); `ok` is true only when `flat` is true and no step failed; `error` = the failed
  steps joined by "; " or "the broker is not flat yet". **BrokerStatus adds** `"kill": {"pending": bool, "at":
  "…|null", "not_flat": [same entries]}`. `POST /v2/oms/resume` stays 409 `kill_pending` until flat.
- **M — Kraken `DuplicateOrderId`** (a ccxt subclass of `InvalidOrder`) on a send is ambiguous, not a refusal: the
  adapter looks the `cliOrdId` up (open orders, then closed orders of the last 7 days — **verify against the demo**
  that the closed-orders history returns `cliOrdId` and covers the window) → found: its ack; not found:
  `BrokerUnavailable` (re-sent later with the same id after another lookup).
- **M — ccxt isolation:** ccxt is no longer in `requirements.txt` (so neither the app's
  `Shared/PythonEnvironment.cs` nor the script's main install can fail on it). It is pinned in
  `backend/requirements-broker.txt` (`ccxt==4.5.84`) and installed by `run_hedgefund_backend.ps1`
  (`Invoke-OptionalInstall`) in its OWN pip run after the main environment — also with `-SkipInstall`, under the
  same `.venv.lock`; its result goes to `.venv/ufos-optional-status.txt` (`ok <sha256> <utc>` |
  `failed <sha256> <utc> <reason>`), retried when requirements-broker.txt changes, with `-Upgrade`, or 24 h after a
  failure; a failure never changes `UFOS_ENV` / `$EnvOk`. The kraken-demo broker then shows, verbatim through the
  existing not-installed state (`sdk.installed false`, `sdk.message`, link `not_installed`): "Kraken adapter
  unavailable: ccxt could not be installed (<reason>)" (reason = "pip exited with code N[: <last pip ERROR line>]",
  ASCII, ≤ 280 chars); without a recorded failure the old text "ccxt is not installed …" stays. This is isolation
  of an optional dependency, not a fallback (no second source; Kraken simply is unavailable). Supersedes the ccxt
  line of §18.11.
- **L-a — user actions try once during backoff:** manual reconcile, resolve and kill run inside
  `MirroredAdapter.user_attempt()`: every broker call tries until the first failure, after which the rest of that
  action is blocked as usual (one real attempt, no hammering). Automatic callers keep the backoff.
- **L-b — `accept_broker` only on a halted account:** 409 `nothing_to_resolve` ("… is not halted by a
  reconciliation mismatch — accepting the broker's values is only possible to resolve one.").
- **L-c — resume while the last reconciliation is `unreachable`** is allowed on purpose (the halt it clears is a
  kill/drawdown halt; `broker_link` still refuses every new order until a reconciliation is `ok`).
- **L-d — reconnect with a mismatch** answers 200 with `broker.connection.status = "connected"` and
  `reconciliation.status = "mismatch"`, `halt.kind = "reconcile"` — intended: the account is connected but halted
  until resolved.
- Tests: `test_sp_broker_review_fixes.py` (9: pending-cancel released → flat; qty held beyond the bound → not flat,
  pending, resume refused, completed by the poller; rejected close retried; queued close → not flat, no duplicate,
  completed later; global kill reports it; manual reconcile during backoff (one real attempt); accept_broker refused
  when not halted; Kraken DuplicateOrderId looked up; install failure shown verbatim). `sp_broker_testkit.FakeBroker`
  models pending cancels and held quantities.

**Manual verification (user-approved 2026-10-18): `HedgeFund/backend/tools/verify_kraken_demo.py`.** Confirms against
the REAL Kraken Futures demo the two assumptions of the duplicate-order lookup (`KrakenDemoAdapter._find`): (a) the
closed-orders history returns our `cliOrdId`, (b) how far back it reaches (assumed ≥ 7 days). Run on Windows:
`cd HedgeFund\backend; .\.venv\Scripts\python.exe tools\verify_kraken_demo.py` (no arguments — any argument is
refused, nothing can be switched).
- **Demo only:** the exchange is built by the adapter itself (`exchange()`: ccxt sandbox mode + the live-guard
  allow-list of every `urls['api']` value); the script checks the resolved URLs again and stops with exit code 3
  before the first request if any is not on `demo-futures.kraken.com`; `BUILD_ALLOWS_LIVE` stays False (printed as
  INFO). Keys: Credential Manager `kraken-demo-api-key` / `kraken-demo-api-secret` (presence check first; missing
  or unreadable → the "Hedge Fund → ⚙ Keys & data → Keys" hint, exit 2); key values are never printed and are
  masked in every output line.
- **Steps** (one line each, `OK` / `FAIL` / `INFO` + reason): 1 ccxt importable + version (INFO if ≠ 4.5.84);
  demo endpoint; 2 account (equity, collateral, available margin only); 3 ONE post-only limit BUY of the minimum
  size on PF_XBTUSD (`BTC-USD`) at 50 % below the last price (cannot fill), cliOrdId
  `ufos-verify-<yyyymmddHHMMSS>-<6 hex>`, sent once through the adapter's send path (an ambiguous failure is looked
  up, never re-sent); 4 cancel + confirm (≤ 10 checks × 2 s); 5a open orders (INFO), 5b exactly `_find`'s call
  `fetch_closed_orders(None, now − 7 days)` contains the cliOrdId (≤ 6 tries × 2 s; FAIL also says whether the order
  id came back without `clientOrderId`), 5c `adapter._find(cid)` finds it; 6 oldest closed-order timestamp (call
  without `since`) — ≥ 7 days → OK, else INFO "can't verify 7-day depth yet — run again after a week of demo use";
  7 safety net: on any error after step 3 the order is cancelled and the outcome printed. Reads use the adapter's
  gate (bounded retries, 20 s timeouts); errors are one line `ErrorClass: message`, no stack traces (library
  logging silenced below CRITICAL). Last line `RESULT: OK` (exit 0) or `RESULT: FAIL (n)` (exit 1).
- **What to do with the result:** 5b/5c OK and 6 OK → the §18.3 lookup assumption holds (remove the "verify" mark);
  5b FAIL but 5c OK → the order is found through `fetch_canceled_orders` (the lookup still works); 5c FAIL → the
  duplicate-order lookup must be changed before relying on Kraken resends; 6 INFO → re-run later.
- Tests: `tests/test_sp_broker_verify_tool.py` (7, fake ccxt, no network: demo-URL refusal before any request,
  missing key, happy path incl. the exact `since` window, cliOrdId missing from the history, cancel safety net,
  fresh account depth INFO, arguments refused; no key value in any output).

**Fixes 2026-10-18 (live test)** — user's click-test 18:05 (keys set, ccxt 4.5.84, endpoint demo-futures.kraken.com):
Test connection → "✕ Account readable — Kraken refused the request (krakenfutures fetchBalance has no account for
flex)". ccxt's `fetch_balance` picks the multi-collateral (`flex`) account itself and raises that text when the
`/accounts` answer has none — without saying which accounts exist. (The 4.5.84 source of `fetch_balance` could not
be read offline here — no package index on the VM/container and the web fetch truncates the 106 kB file before it;
the account model below is from Kraken's REST reference "Get wallets" (`GET /accounts`): `cash` (`cashAccount`),
single-collateral `marginAccount`s keyed by instrument (e.g. `fi_xbtusd`), `flex` (`multiCollateralMarginAccount`:
`balanceValue`, `portfolioValue`, `availableMargin`, `initialMargin`, `maintenanceMargin`, `pnl`,
`unrealizedFunding`, `totalUnrealized`, `currencies`…). Kraken documents `flex` as always present — the demo answer
evidently lacked it.)
- **Change:** `KrakenDemoAdapter.account()` no longer calls `fetch_balance`; it reads the RAW `GET /accounts` (ccxt
  implicit `private_get_accounts`, `accounts_overview()`) and picks the multi-collateral wallet EXPLICITLY
  (`pick_flex`: key `flex` with type `multiCollateralMarginAccount`, else the one account of that type). Cash and
  single-collateral margin accounts are never used for trading or valuation (no silent switch). Missing → a
  non-connection `BrokerUnavailable` (link `error`, no backoff hammering): "Kraken Futures demo has no
  multi-collateral (flex) wallet — needed for the PF_ perpetuals the platform trades; other accounts are never used.
  Found: cash + fi_xbtusd (single-collateral). Check at demo-futures.kraken.com that the multi-collateral
  wallet is set up and funded, then test again." (≤ 300 characters; "Found" is a summary grouped by kind, e.g.
  "cash + 5 single-collateral (fi_bchusd, …)", ≤ 44 characters).
  AccountView of kraken-demo adds `account_name` (the wallet used, normally `flex`).
- **Aligned paths:** `submit` refuses (rejected ack, same text) while the flex wallet is missing (checked with a
  5-min cached `/accounts` read, `require_flex`); `positions()` and `open_orders()` keep only linear USD perpetuals
  (`BASE/USD:USD` = PF_, the flex wallet's markets) — positions/orders of the demo's single-collateral accounts
  (PI_/FI_/FF_) are not the platform's and are no longer reported as `UNMAPPED:` foreign positions (which would have
  halted reconciliation forever). The kill switch closes PF_ positions only (the only ones the platform can open).
- `tools/verify_kraken_demo.py` step 2 uses the same logic: an INFO line "2a accounts: on the demo: <name (kind)…>;
  trading wallet: flex | MISSING", then `adapter.account()` (FAIL with the text above when missing — no order is
  placed).
- **User (Kraken demo side):** run Test connection again; if it now says the multi-collateral wallet is missing,
  check at demo-futures.kraken.com that the multi-collateral wallet exists and holds demo funds (the exact UI step is
  not verifiable offline), then Test connection / `tools\verify_kraken_demo.py` again.
- Tests: `test_sp_broker_adapters.py` (+2: only cash/margin → clear message + submit refused, flex only, all three,
  flex under another key; PF_-only open orders; fetch_balance in the fake now raises the user's error to prove it is
  unused), `test_sp_broker_verify_tool.py` (+1: no flex wallet → INFO list + FAIL, no order).
- **Review of this fix (2026-10-18):** `cancel_all()` (used by the broker kill) no longer calls ccxt
  `cancel_all_orders` (which cancels every open order of the demo, all wallets): it cancels the open orders of the
  PF_ markets one by one and counts only those; orders on the cash / single-collateral wallets are left untouched
  and counted in `last_untouched`. The kill's open-order wait, positions and flat check use the same scope (PF_ only,
  via `open_orders()` / `positions()`). **Kill response adds** `"untouched_orders": 0` (int) and `"untouched_note":
  null | "2 order(s) on other wallets left untouched (not this adapter's account)."` (always 0 / null on
  alpaca-paper). Tests: `test_sp_broker_review_fixes.py` (+3: cancel_all PF-only, the "Found" summary ≤ 44, kill
  reports untouched orders and is flat).
  **Follow-up:** the same two keys are also in the global kill's per-account `broker` object (`POST /v2/oms/kill`,
  `/fund/kill`: `ok, error, cancelled_at_broker, note, flat, pending, not_flat, untouched_orders, untouched_note`) and
  in `BrokerStatus.kill` = `{"pending", "at", "not_flat", "untouched_orders": int, "untouched_note": str|null}` — the
  values of the LAST kill on that broker (pa_meta `last_kill`; 0 / null before any kill). Test: the same test checks
  the broker card and the global kill.
- **Fix 2026-10-18/19 (live retest 23:55):** Test connection failed with "the accounts answer is not an object
  (unexpected response)" and the card went to "WAITING FOR BROKER (attempt 1)" — a format problem treated as a
  connection problem. **Verified response shape** (Kraken REST reference, Futures "Get wallets", `GET /accounts`,
  https://docs.kraken.com/api-reference/account-information/get-wallets.md): `{"result": "success", "accounts":
  {"cash": {"type": "cashAccount", "balances": {…}}, "<e.g. fi_xbtusd>": {"type": "marginAccount", …}, "flex":
  {"type": "multiCollateralMarginAccount", "balanceValue", "portfolioValue", "availableMargin", …}}, "serverTime":
  "…"}` (error: `{"result": "error", "error": "<code>", "serverTime"}`). The ccxt 4.5.84 source could NOT be read
  here (no PyPI/npm/GitHub access from the VM or the container; the web fetch truncates the 106 kB module before
  `fetch_balance`); from ccxt's base behaviour (`fetch` returns the parsed JSON only when the body starts with
  `{`/`[`, otherwise the raw TEXT) the likely cause is a body ccxt did not recognise as JSON (BOM / leading
  whitespace / an HTML page) — which would also explain the earlier "has no account for flex" (ccxt read the text
  as an empty answer). The new diagnostic will show the real shape on the next test. Changes:
  - `kraken_demo.harden_exchange(ex)`: the exchange's `fetch` is wrapped once — a text body is stripped of BOM /
    whitespace and parsed with ccxt's own `parse_json` when it is JSON; anything else raises `BrokerFormatError`
    "Kraken answered in an unexpected format — not a connection problem: unexpected response shape: <shape>" —
    for EVERY endpoint, so no ccxt method can read an unparsed answer as "no orders / no positions" silently.
    `accounts_overview()` additionally accepts a JSON text and requires an object with an `accounts` object;
    `result: error` → `BrokerSetupError` "Kraken Futures demo refused the accounts request (<code>)".
  - **Safe diagnostic** (`common.shape_of`): type + top-level keys (sanitised, ≤ 10) or length; a text only its
    first character class (`{ } [ ] < " '`, "letter/digit", "other"; `<` adds "(HTML page?)"). backend.log gets
    "Kraken demo: unexpected response shape for GET /derivatives/api/v3/accounts: <shape>" (path only — never the
    query, values or ids). Example: "unexpected response shape: dict, keys: result, serverTime".
  - **Not retryable:** new `BrokerNotRetryable` (`BrokerFormatError`, `BrokerSetupError` — the flex-missing message
    is a setup error now). `classify_failure` → link `state: "error"` with the new key **`link.retry: "manual"`**
    (`"auto"` otherwise), `next_retry_at: null`, message "<reason> — not retried automatically (Test connection /
    Reconcile now tries again)"; automatic callers (poller, runtime, OMS sync) are blocked; a user action makes one
    real attempt (§18.13 L-a) and a success resets it. Never "Waiting for broker" for these. Parse errors inside
    ccxt (`ValueError/KeyError/TypeError`) are format errors too; the Alpaca adapter's "unexpected response" cases
    use the same class ("Alpaca answered in an unexpected format — not a connection problem …"). A send whose answer
    is unreadable is ambiguous: looked up, never re-sent blindly.
  - `tools/verify_kraken_demo.py` step 2 shows the same text: "FAIL 2 account: BrokerFormatError: Kraken answered in
    an unexpected format — not a connection problem: unexpected response shape: …".
  - Tests: `test_sp_broker_format.py` (7: documented shape, JSON text with BOM/whitespace, HTML text (message + log
    without values/ids/query), list / JSON-list text / missing `accounts` / `result: error`, `shape_of` safety, link
    manual-only, format error → no waiting and no automatic retry, user reconcile recovers),
    `test_sp_broker_verify_tool.py` (+1).
- **Live retest 2026-10-19 15:47 — the diagnostic's result and what it means:** "unexpected response shape: str,
  length 15059, starts with < (HTML page?)". The user's backend.log has the request path:
  `GET /derivatives/api/v3/accounts` on `demo-futures.kraken.com` — exactly the documented private endpoint
  (Kraken "Futures introduction": demo REST base `https://demo-futures.kraken.com`, paths `/derivatives/api/v3/…`,
  example `https://demo-futures.kraken.com/derivatives/api/v3/tickers`,
  https://docs.kraken.com/exchange/guides/futures/introduction.md; "Get wallets" = `GET /accounts`,
  https://docs.kraken.com/api-reference/account-information/get-wallets.md). So the URL path built by ccxt's
  implicit `private_get_accounts` is correct; the request was answered with an HTML page (HTTP 2xx — ccxt raises on
  4xx/5xx). Signing (`postData + nonce + endpointPath`, SHA-256, HMAC-SHA-512 with the base64-decoded secret,
  https://docs.kraken.com/api/docs/guides/futures-rest) stays ccxt's — the URL is never hand-built here. Changes:
  - `harden_exchange`: every request's FULL URL is checked by `live_guard.assert_paper_url` before it is sent (a URL
    changed after construction is refused, nothing sent); the requests session is wrapped to capture SAFE response
    metadata — HTTP status, content type, server, redirect count + final host/path — appended to the message, e.g.
    "unexpected response shape: str, length 15059, starts with < (HTML page?); HTTP 200, text/html, server
    cloudflare, redirected 1× to demo-futures.kraken.com/ — looks like a Cloudflare challenge page?" (hints:
    Cloudflare challenge / captcha / login-form markers; never the body, the query or ids; ≤ 300 characters).
  - Test connection (kraken-demo) checks **Markets loaded** (public API, no keys) BEFORE "Account readable"; the
    verify tool adds step "1b public API". Both tell "the host serves the API" from "only private calls get HTML".
  - Tests (`test_sp_broker_format.py` +3): the accounts request URL is `https://demo-futures.kraken.com
    /derivatives/api/v3/accounts` (a fake built like ccxt's sign: `urls['api']['private'] + 'v3/' + path` — the
    real ccxt module is not available here), a URL changed to the live host is refused before sending, HTML answer
    metadata + challenge hint without values.
  - **Open (user):** run Test connection once more and send the new message (status / redirect / server / hint) —
    it decides between a redirect (e.g. to a login or region page), a challenge page (network / VPN / browser
    check), or an API change on the demo.
- **Kraken demo retired (user decision 2026-10-19):** the 16:40 retest showed "HTTP 200, text/html, server
  cloudflare, redirected 3× to pro.kraken.com/app" — Kraken decommissioned its Futures demo ("decommissioned at 13
  UTC on Monday, 14 July", https://support.kraken.com/articles/360024809011-api-testing-environment-derivatives; the
  demo host redirects, https://autoview.com/guides/kraken-futures-demo/). Minimal retirement, nothing deleted (the
  adapter etc. are on the stage-10 deletion list in todo.md):
  - `accounts.RETIRED = {"kraken-demo": "retired — Kraken decommissioned its Futures demo environment (2026); see
    Brokers notes"}` (+ `RETIRED_SOURCES`). A retired account is never in `accounts.rows()` / `all_accounts()` (runtime,
    poller, start-up reconciliation, allocator and the GLOBAL kill skip it without error); its rows stay as history.
  - Every action answers **409 `{"error": "kraken-demo: retired — …", "code": "retired", "sources": [...]}`**: test,
    connect, reconcile, resolve, kill (this broker), instrument, routing to it; disconnect of a still-connected row only
    writes the record (no broker call). `MirroredAdapter.remote_call` refuses locally for a retired account (also
    user calls), `pre_trade_checks` rejects (`venue`), `round_qty` makes no market lookup — **no request to Kraken's
    private/demo API is possible**. The Kraken PUBLIC price source (`data/providers/kraken_public.py`,
    api.kraken.com OHLC, Shadow Account) is separate and unchanged.
  - **BrokerStatus adds** `"state": "active" | "retired"`, `"retired": null | {"reason", "since": "2026-10-19",
    "sources": [url, url]}`; for a retired broker every `can.*` is false (except `disconnect` while a row is still
    connected) and `notes[0]` is the retirement note. **Routing:** `allowed_accounts` never lists a retired broker
    (crypto → `["paper-main"]`); a route to it is NOT moved: entries add `"blocked": bool, "blocked_reason": str|null`
    ("kraken-demo: retired — … — re-route the strategy"), `POST /v2/incubation` / eligibility answer code `retired`;
    re-routing to paper-main works as usual.
  - WPF: the Brokers card shows a RETIRED chip, the reason, the source URLs, the key status with "These keys are no
    longer used — you can remove them in Keys & data → Keys", all buttons disabled; Routing shows BLOCKED rows and the
    route dialog no longer offers kraken-demo. `tools/verify_kraken_demo.py` exits at once (exit 5, "RESULT: RETIRED")
    and is renamed `tools/verify_kraken_demo.py.stale` (its tests: `tests/test_sp_broker_verify_tool.py.stale`).
  - Tests: `test_sp_broker_retired.py` (5: 409 on every action + no call, a row connected before the retirement is
    skipped by runtime / poller / start-up / global kill and refused locally, routing blocked not moved + eligibility
    `retired` + re-route, Kraken public prices unaffected, the stale tool exits early); dormant Kraken mechanics keep
    their tests under `@legacy_kraken` until the stage-10 deletion.

### 18.14 Stage 8b — Binance Spot demo broker (contract, 2026-10-19)

User decision 2026-10-19: the retired Kraken Futures demo (§18.13) is replaced by **Binance Spot Demo Mode** —
SPOT, not futures (Binance stopped derivatives for German users in 2021). Same framework as §18.1–§18.13 (mirror
book, reconciliation halts, weak links, kill / dead-man, live guard OFF); only the differences are listed here.

**Facts (checked 2026-10-19 against the sources):**
- Binance Spot Demo Mode (https://developers.binance.com/en/docs/products/spot/demo-mode/general-info): REST
  `https://demo-api.binance.com/api`, WebSocket API `wss://demo-ws-api.binance.com/ws-api/v3`, streams
  `wss://demo-stream.binance.com/ws` (and `/stream`, port 9443 variants). Keys: log in, "Binance Demo Trading" →
  API Key Management. "IP Limits, Unfilled Order Count, Exchange Filters are exactly the same as the live
  exchange"; prices and order books are "similar to the live exchange"; the balance can be reset "whenever you want
  via the UI". Distinct from Spot **Testnet** (testnet.binance.vision, monthly reset) — Testnet is NOT used.
- ccxt 4.5.84 (the pinned version; source read in the installed package 2026-10-19): `binance.enable_demo_trading(
  True)` sets `urls['api'] = urls['demo']` (refused when sandbox mode is on); `urls['demo']` maps the spot APIs
  `public` / `private` → `https://demo-api.binance.com/api/v3` and `v1` → `https://demo-api.binance.com/api/v1`
  (plus `demo-fapi` / `demo-dapi` futures hosts, which this build never uses). There is no `sapi` entry in demo mode
  (ccxt itself skips `fetch_currencies` and the margin calls there). `sign()` raises `NotSupported` for an API name
  missing from `urls['api']` — before any request. ccxt issue https://github.com/ccxt/ccxt/issues/27039
  (`enable_demo_trading`, NOT `set_sandbox_mode`).

**Contract:**
1. **Account** `binance-demo` (broker `binance_demo`), label "Binance Spot demo account", chip
   **"DEMO · SIMULATED FUNDS"**, venue "Binance Spot — Demo Mode", instruments "Crypto spot BASE-USD → BASE/USDT
   (long only)". Storage v23 rebuilds `br_accounts` (SQLite cannot alter a CHECK) with `binance-demo` /
   `binance_demo` added; every row is copied, the insert-only triggers are recreated.
2. **Endpoint / live guard:** `PAPER_ENDPOINTS["binance_demo"] = "https://demo-api.binance.com"`; allowed hosts:
   `demo-api.binance.com` (https) — the demo WebSocket hosts `demo-ws-api.binance.com` / `demo-stream.binance.com`
   (wss) are allowed by the guard but NOT used in this build (REST polling only, like Kraken). ccxt `binance` with
   `defaultType: spot`, `fetchMarkets.types: ["spot"]`, `fetchMargins: false`, `fetchCurrencies: false`,
   `enableRateLimit`, 20 s timeout → `enable_demo_trading(True)` → `urls['api']` is cut down to the three spot
   entries (`public`, `private`, `v1`), each asserted on `demo-api.binance.com`; then EVERY request's full URL is
   asserted before it is sent (hard refusal `LiveTradingDisabled` otherwise). Live hosts (`api.binance.com`,
   `stream.binance.com`, `ws-api.binance.com`) appear only in `live_guard.LIVE_HOSTS` (source scan test).
3. **Keys:** Credential Manager `binance-demo-api-key` / `binance-demo-api-secret` (Keys & data rows like the
   others), read per call, never logged/returned. HMAC keys only — a secret that is a PEM private key (RSA /
   Ed25519) is refused before any request ("HMAC keys only — create a system-generated (HMAC) key at
   demo.binance.com"). Rejected keys → `auth_failed` (as Alpaca/Kraken).
4. **Symbols / currency:** platform `BASE-USD` ↔ ccxt `BASE/USDT` ↔ Binance `BASEUSDT` (spot markets with quote
   USDT only). The account's money is **USDT** and is shown as USDT everywhere (original-currency rule) — never
   relabelled or converted to USD: `BrokerStatus.currency = "USDT"`, `currency_note = "USDT — not converted"`;
   the mirror book stores the USDT amounts unchanged (its positions are valued with the platform's BASE-USD quotes —
   the views label the account's figures "USDT — not converted").
5. **Long only:** no short, no margin, no leverage (`capabilities.shorting = false`). Routing a strategy whose
   `portfolio.mode` is not `long_only` to `binance-demo` → **409 `long_only`** with the reason (also the incubation
   eligibility code). An order with side `short` / `cover`, or a `sell` above the held quantity, on binance-demo is
   rejected by the pre-trade check `venue` / `position_side` and writes a visible account event "Order blocked:
   short not possible on binance-demo" — never converted to flat/long silently.
6. **Orders:** `market` and `limit` (GTC) only. Before sending, from ccxt's market precision/limits (Binance
   filters): quantity rounded DOWN to the `LOT_SIZE` step; refused with the filter named — `LOT_SIZE` (min / max
   quantity), `MARKET_LOT_SIZE` (max for market orders, when present), `PRICE_FILTER` (limit price on the tick,
   min / max), `NOTIONAL` / `MIN_NOTIONAL` (quantity × price; price = the limit price, for market orders the
   demo's last price — unreadable → refused, no stand-in). Platform orders carry `newClientOrderId =
   "UFOS_" + client_order_id` (≤ 36 chars); lookups by `origClientOrderId` (the symbol from the OMS row).
   `cancel_all` / kill cancel ONLY open orders whose client id starts with `UFOS_`; others are counted and reported
   as untouched (as Kraken). Fees: from the order's trades (`myTrades?orderId=`, commission + asset): in USDT → the
   fee; in the base asset → the received quantity is net of it (the mirror books the net quantity, the fee valued
   at the fill price); in another asset (e.g. BNB) → not booked (the reconciliation then reports the BNB change —
   switch off "pay fees with BNB" in the demo; verify).
7. **First connect — baseline, NOT flat:** the demo comes pre-funded with several assets, so there is NO "flat"
   requirement on balances. Instead: no open orders with our `UFOS_` prefix (409 `platform_orders_open`), USDT
   total ≥ 1,000 (409 `too_small`), and the user **confirms the starting balances**: `POST
   /v2/brokers/binance-demo/connect {confirm, reason, starting_balances: {ASSET: total}}` = the non-zero balances
   the Test showed; the server reads them again and refuses with 409 `balances_changed` (fresh list) when they
   differ, 400 when missing. The confirmed balances are recorded as the **baseline** (`br_connections.detail_json`
   + the account's `pa_meta` key `baseline`) and are the basis for P&L: mirror starting capital = baseline USDT;
   the account's "positions" are `balance − baseline` per asset (the pre-funded holdings are never the platform's
   and are never traded or closed). Foreign open orders (not `UFOS_`) are allowed and reported. A balance reset in
   the demo UI later → reconciliation mismatch → resolve (§18.5).
8. **Test connection (read-only):** SDK installed, keys present, demo endpoint asserted, server time
   (`/api/v3/time`), markets loaded (`exchangeInfo`, count of trading BASE/USDT markets), account readable
   (balances), open orders readable. The result adds `balances` (non-zero `{asset, free, locked, total}`),
   `platform_open_orders`, `foreign_open_orders`, `currency: "USDT"`. Unexpected answers give the Kraken-style
   diagnostics (HTTP status, content type, server, redirects / final host+path; never bodies, queries or ids).
9. **Reconcile / weak links / kill / dead-man:** the §18.5/§18.6 framework. Cash basis = USDT total (free +
   locked) vs mirror cash (tolerance max(1 USDT, 0.05 % of the cash basis)); positions = baseline deltas, exact
   to 1e-7. The kill sells every positive delta (market); a remainder that the filters do not allow to sell
   (below `LOT_SIZE` min or `NOTIONAL` min — "dust") counts as flat for the kill and is listed (`kill.skipped`, ⚑ see the notes).
10. **Routing:** crypto strategies may route to `binance-demo` (long only, item 5); `paper-main` stays the default.
11. **Verify tool** `tools/verify_binance_demo.py`: read-only checks by default (time, markets, balances, open
    orders, the URL guard); a tiny round-trip order ONLY with the explicit flag `--place-test-order` (a
    LIMIT buy far below the market at the minimum notional, cancelled at once; refused while the account is
    connected with working orders).
12. **Tests** with fakes only (no network): URL guard, filters, short refusal, prefix-limited kill, USDT labelling,
    retry / diagnostic paths, key handling, baseline connect.

**Implementation notes (2026-10-19, stage 8b — binding for the WPF; deviations from the contract above marked ⚑):**
- Code: `execution/brokers/binance_demo.py` (adapter), `brokers/ccxt_support.py` (shared ccxt plumbing: lazy import,
  exception kinds, SAFE response metadata, `harden_exchange(ex, broker, …)` — the dormant Kraken adapter keeps its own
  copy until stage 10), `tools/verify_binance_demo.py`; account id in `accounts.BROKER_IDS` / `mirror.BROKER_IDS`
  (order: alpaca-paper, binance-demo, kraken-demo) and the API id patterns; `mirror.CRYPTO_VENUE = "binance-demo"`
  (venue messages); storage **v23** (br_accounts rebuild).
- **BrokerStatus adds** `chip` (alpaca "PAPER · SIMULATED FUNDS", binance/kraken "DEMO · SIMULATED FUNDS"), `currency`
  ("USD" | "USDT"), `currency_note` (binance "USDT — not converted", else null). The binance snapshot `account` is
  `{equity: null, cash: USDT free, cash_basis: USDT total, buying_power: USDT free, currency, currency_note,
  balances: [{asset, free, locked, total}], baseline_set, shorting_enabled: false, …}`; a binance position adds
  `{asset, balance, baseline, kill_skip: str|null, currency}` (qty = balance − baseline; before the first connect
  `positions` is empty).
- **Test** (binance) adds the checks "Server time" and "Markets loaded (exchangeInfo)" (between "Paper/demo endpoint" and
  "Account readable") and `server_time {server_time, skew_ms}`, `balances`, `platform_open_orders`,
  `foreign_open_orders`, `currency`, `currency_note`; `flat` has no meaning for binance (no flat rule).
- **Connect** body `starting_balances` (≤ 500 entries, asset `^[A-Z0-9]{1,20}$`, finite ≥ 0; 400 for another broker);
  binance first connect without it → 400 `{"_": "starting_balances: confirm …"}`; differing balances (tolerance
  max(1e-8, 1e-9·|x|)) → 409 `balances_changed` + `balances`; open `UFOS_` orders → 409 `platform_orders_open` +
  `open_orders`. The baseline is written to `pa_meta.baseline` before the account object is built and recorded in
  `br_connections.detail_json` / the audit row `broker_connect` (`baseline`, `currency`).
- **Fees** ⚑: the ack keeps `filled_qty` = the GROSS filled quantity (so the OMS sees the order filled) and adds
  `fee_base_qty` (cumulative commission taken from the bought asset); `mirror._book_delta` books each delta net
  (`dq − fee_base_qty·dq/filled`, exact for a uniform commission rate) — the cash change equals the USDT spent
  because `fees` includes the base commission valued at the fill price. Ack `fee_note` names a fee paid in another
  asset (not booked).
- **Kill** ⚑: the result adds `skipped: [{symbol, qty, reason}]` (instead of `dust`) — positions whose adapter
  `kill_skip` is set (dust below LOT_SIZE / NOTIONAL, below the baseline, an unmapped asset) are not sold and count
  as flat; `untouched_note` (binance) = "n order(s) without the platform's UFOS_ prefix left untouched (not the
  platform's)."
- **Reconcile:** binance tolerance (1 USDT, 0.05 % of the cash basis — the demo reports no equity); `cash.currency`;
  position mismatch hint "the balance changed outside the platform (traded, transferred or reset in the demo UI, or
  a fee paid in another asset)". **accept_broker** ⚑ on binance: a change BELOW the baseline or of an unmapped asset
  moves that asset's baseline (`resolution.detail.baseline_changes [{asset, change}]`) — never a short in the mirror;
  the USDT difference is the usual `broker_adjustment`.
- **Routing:** `allowed_accounts` crypto → `["paper-main", "binance-demo"]` minus `refused_accounts [{account_id,
  code: "long_only", reason}]` (a strategy whose `portfolio.mode` is not `long_only`); a route to binance-demo of a
  strategy that needs shorts now → `blocked: true` + reason (not moved); `POST /v2/brokers/routing` → 409
  `long_only`; incubation eligibility code `long_only`.
- **Pre-trade:** binance `short` / `cover` → `venue` "binance-demo is spot, long only — a short of … is not possible
  (blocked, not converted to flat or long)"; a `sell` above the held quantity → `position_side` "… exceeds the long
  position … — binance-demo is long only …"; both write the account event (kind `order`) "Order blocked: short not
  possible on binance-demo". Filters are checked in the adapter's `submit` (rejected ack, the filter named first:
  `LOT_SIZE: …`, `MARKET_LOT_SIZE: …`, `PRICE_FILTER: …`, `NOTIONAL: …`); the last price for the notional check is
  cached 60 s.
- **WPF:** Keys & data rows "Binance Spot demo API key / secret (HMAC)" (set together; a PEM private key is refused
  in the dialog); Brokers card: the backend's chip + a "USDT" chip, the "Amounts in USDT — not converted · spot, long
  only · baseline" line, USDT amounts (no $), balances, baseline state; Test shows the balances / UFOS_ vs other
  orders; Connect… (binance) needs a successful Test, lists the starting balances and a confirm checkbox, sends
  `starting_balances`; the route dialog offers binance-demo with its consequences and shows "long only" for
  strategies that need shorts; the kill popup lists `skipped`; the Portfolio account card formats binance-demo in
  USDT.
- **Tests:** `test_sp_broker_binance.py` (17: demo mode / URL guard / sapi impossible / source scan, keys, symbols /
  USDT / baseline deltas / dust, filters named, long only, prefix-limited cancel_all, fees base / quote / BNB, lost
  answer looked up, retries, HTML diagnostics), `test_sp_broker_binance_flow.py` (10: read-only Test, baseline connect
  400/409, net booking + exact reconcile, short + oversell blocked with event, routing long_only + eligibility,
  prefix-limited kill with dust, global kill, UI change → mismatch → accept rebases, storage v23 incl. the v22 → v23
  copy), `test_sp_broker_binance_verify.py` (4); fake exchange `tests/sp_binance_testkit.py`.
- **Review fixes 2026-10-19 (independent review of 8b, no blockers):**
  1. *Redirects:* `ccxt_support.harden_exchange` wraps the requests session with `allow_redirects=False`; any 3xx →
     `BrokerFormatError` "the server answered with a redirect (HTTP <status> → <Location host>) — not followed, no
     request leaves the demo host" (not retried; link `error`/`retry: manual`). Before, requests followed a 30x to
     another host with the `X-MBX-APIKEY` header (the guard checked only the first URL). Alpaca: alpaca-py's
     `RESTClient` already sends `allow_redirects=False` (alpaca/common/rest.py, `_request` opts) — no exposure, no change.
  2. *NOTIONAL on limit orders:* `filter_refusal` checked `qty × limit_price` already (the notional block follows both
     branches); now covered by a test (below min / above max), and `step_round_down` rounds in decimal arithmetic
     (8999 at step 0.00001 stayed 8998.99999 in binary floats).
  3. *Baseline:* `connect` writes the account row + settings, the connection row, `pa_meta.baseline` and the audit row
     in ONE transaction. A binance-demo account WITHOUT its baseline (e.g. a restored database) is blocked — the
     adapter's `positions()` raises "baseline missing — confirm the starting balances …; never assumed empty"
     (reconcile `unreachable`, link manual, `broker_link` check refuses orders); `BrokerStatus.baseline = {required,
     set, missing, balances, message}` (null for other brokers), `can.connect` true; `POST …/connect` with
     `starting_balances` works again WHILE connected (only in that state; audit `baseline_reconfirmed: true`; Test
     marks "Positions readable" failed with that message and still runs the other reads). WPF: red callout + the
     button "Confirm starting balances…".
  4. *USD/USDT:* new setting `max_order_notional_usdt` ("Max order notional (USDT)", same default/bounds) — an account
     sees only the cap of its own money (`settings.get` adds `currency`; the other key is a 400 "not used by
     binance-demo — its money is USDT"); `checks` uses the account's cap and writes amounts as "1,234.50 USDT".
     The book is valued and checked with the account's OWN prices: `accounts.quote_for(acc, sym)` (OMS, runtime,
     allocator, kill/flatten, resume) → `BrokerAccount.own_quote` → the demo's BASE/USDT last price (`fetch_ticker`,
     cached 60 s) as a `fund.types.Quote`; the MirrorBroker gets the same quote function. Not readable → None
     (valuation paused / "no current quote") — never the USD quote.
  5. *Step / tick:* from ccxt's `precisionMode` (TICK_SIZE = 4 for binance in 4.5.84 → the value is the increment, e.g.
     10 or 0.001; DECIMAL_PLACES → 10^-n; any other mode → `BrokerFormatError`); the heuristic is gone.
  6. *Refused cancels:* `cancel_all` counts them (`last_refused`); the kill JSON adds `refused: [{client_order_id,
     broker_order_id, reason}]`, `ok: false` and the error "n cancel(s) refused by the broker: …"; WPF lists them.
  7. *Backup before v23:* `db.backup_before(conn, target, 23)` runs before `migrate` for an EXISTING file at schema
     1…22: a consistent copy (sqlite backup API) `data/backups/hedgefund.db.pre-v23-<UTC stamp>`, once (an existing
     pre-v23 copy → none); a failed backup stops the migration (the file stays at its version).
  Tests: `test_sp_broker_binance_fixes.py` (10).
- **Live test 2026-10-23 — Binance error codes:** Test connection said only "rejected the API keys". ccxt 4.5.84 raises
  `AuthenticationError('binance {"code":-2015,"msg":"…"}')` (`binance.handle_errors`; -1021 arrives as `InvalidNonce`,
  a NetworkError). `binance_demo.binance_error` extracts `code` + `msg` (JSON body, or the code from the known text when
  ccxt matched the message alone); `sanitize` removes URLs, query strings, `signature=`/key=value secrets, the key and
  secret, IP addresses and long tokens (≤ 160 chars). Messages: "Binance Spot demo rejected the API keys (code -2015:
  <msg> · likely: <hint>). Check them in Hedge Fund → Settings → Keys." Hints: -2015 key not valid on Binance Demo
  (keys from binance.com live or testnet.binance.vision do not work), IP restriction, or missing permission 'Enable
  Reading'/'Spot trading'; -1022 signature mismatch — secret wrong or HMAC vs Ed25519/RSA key type; -2014 key format;
  -1021 PC clock out of sync (→ `BrokerSetupError`, not retried). Exception `detail` = "<class> code=<code>"; refused
  reads / sends show "code …: …" too; one redacted WARNING line per failure ("Binance demo request refused: <class>
  code=… msg=…"). Message caps raised to 500 (test check messages, link `last_error`, rejected acks).
  URL check (real ccxt 4.5.84 `sign()` with `enable_demo_trading` + the spot cut): fetch_balance → GET
  demo-api.binance.com `/api/v3/account` (one request), create_order → `/api/v3/order`, fetch_time → `/api/v3/time`,
  sapi → `NotSupported` before any request; the URL guard and the redirect refusal do not change the host.
  Tests: `test_sp_broker_binance_errors.py` (5).

## 19. Stage 9 — monitoring & Shadow Account (contract, 2026-10-18)

> **REMOVED 2026-10-18 (user decision): the tax export** — §19.5 and every tax / tax-FX item of §19.9–§19.14 (routes `/v2/monitoring/tax/*`, `tax_export.py`, the tax CSV, the §23 / Anlage KAP categories). The user (verbatim): "Ich glaube es ist nicht gut, wenn wir ein System zum export von steuerdaten haben. Das ist riskant, weil das muss alles richtig sein. Ich glaube es ist besser, darauf hinzuweisen, dass man sich seine steuerberichte bei den brokern extra holen muss". What replaces it: §19.15 (fixed notice route; the EUR/USD rates moved to `/v2/shadow/fx`).

Decisions: §15 (user, 2026-10-17) — **decay → the strategy is PAUSED** (no new orders, positions kept) **+ the user
decides** (demote / keep running); **alerts = Windows toasts**; **yearly German tax CSV** (Anlage KAP basis — **not
tax advice**); **Shadow Account** imports: generic column mapping + templates for **Trade Republic, Scalable
Capital, IBKR Flex**; **data stays local**. Project direction (user, 2026-10-17): shippable-in-theory quality, own
money / own accounts only. Plan: project doc "L9 Monitoring, reporting, Shadow Account" (live dashboard, realised vs
expected band, decay monitor, broker/reconciliation status, alerts, daily/weekly report with optional AI-labelled
commentary, tax/transaction export; Shadow Account: broker statements → behaviour diagnostics → rule-based shadow
portfolios → exportable audit report). Values marked **(confirmed by the user 2026-10-24)** were chosen by the implementing
agent while the user was away (2026-10-18) and confirmed by the user on 2026-10-24. Nothing in stage 9 places an order except what already exists (the pause
only REMOVES orders; "demote" uses the existing §17.2 abort path). PAPER / DEMO ONLY.

Package: `strategy_platform/monitoring/` — `expected.py`, `decay.py`, `alerts.py`, `reports.py`, `tax_export.py`,
`service.py` (the monitor task + overview), `fx.py` (EUR/USD rates), `prices.py` (daily closes for diagnostics /
shadows, injectable), `shadow/{importers.py, normalize.py, diagnostics.py, shadow_portfolio.py, report.py}`;
routes `api/monitoring_routes.py` (mixin of `V2Api`, `/v2/monitoring/*`, `/v2/shadow/*`); storage hedgefund.db
**v17**. Changes outside the package: `live/runtime.py` (pause hook, 19.2), `api/routes.py` (mixin + the CSV upload
reader), `storage/db.py` (v17), `ai_budgets.py` (budget key `reports`), `main.py` (task `sp-monitor`).

### 19.1 Live vs expected (`monitoring/expected.py`)

- **Scope:** every ACTIVE incubation run (`incubation.active_runs()`: running, or completed while the strategy still
  incubates that version and keeps trading on paper — §17.2); stage 12 (`live`/`scaled`) will use the same code.
- **Expected basis = the run's `expected_json`**, frozen at incubation start from the version's holdout card
  (§17.2: daily LOG returns of the holdout curve → μ, σ, max drawdown, vol = σ·√P, P = 252 NYSE / 365 UTC). It is
  never recomputed later → no look-ahead, and the band cannot be moved after the fact.
- **Realised:** the strategy's sub-ledger daily returns (`ledger.daily_returns`, capital flows removed) from the
  run's `start_date`, only sessions ≤ the last COMPLETED session (`incubation.last_completed_session`), as log
  returns `l_k = ln(1 + r_k)`.
- **Tracking series** per session k = 1..n: `cum_log_k = Σ l`, expected mean `k·μ`, incubation band
  `k·μ ± 1.96·σ·√k` (the §17.2 band), the decay line of rule `return_band` (19.2), drawdown of the cumulative log
  curve from its running peak (`1 − e^(cum − peak)`), rolling 20-session realised vol (annualised).
- **Deviation statistics:** `z_cum = (cum_log_n − n·μ) / (σ·√n)`; `in_band`; `tracking_error` (§17.2 definition,
  annualised RMS of `l − μ`); `vol_ratio = realised vol / expected vol`; `dd_ratio = realised max DD / expected max
  DD`; `rolling_z` = z of the last min(n, 60) sessions (the `return_band` statistic); realised vs expected Sharpe
  (`μ/σ·√P`).

### 19.2 Decay monitor (`monitoring/decay.py`) — documented rule, constants in ONE place (`decay.RULES`)

| constant | default | meaning |
|---|---|---|
| `MIN_SESSIONS` | 20 | no rule fires before 20 realised sessions (except `drawdown`) |
| `RETURN_WINDOW` | 60 | rolling window w = min(n, 60) of `return_band` |
| `RETURN_Z` | 3.0 (contract draft 2.33 — calibrated, §19.12) | one-sided 99.87 % lower bound |
| `RETURN_CONSECUTIVE` | 3 | consecutive breached sessions before it triggers |
| `DD_FACTOR` | 2.0 (contract draft 1.5 — calibrated, §19.12) | drawdown limit = 2 × the holdout max drawdown |
| `VOL_WINDOW` / `VOL_FACTOR` / `VOL_CONSECUTIVE` | 20 / 2.0 / 3 | realised vol of the last 20 sessions > 2 × expected for 3 sessions |
| `REARM_DD_STEP` | 0.25 | after "keep running": `drawdown` fires again only 0.25 × expected max DD deeper than at the decision |

(all **confirmed by the user 2026-10-24**). Rules, evaluated once per completed session `t` of each active run, using ONLY
returns up to `t` and the frozen expected basis:
- **`return_band`** (performance decay): `S = Σ` of the last w log returns; breached when `S < w·μ − Z·σ·√w`;
  triggers after `RETURN_CONSECUTIVE` consecutive breached sessions (n ≥ `MIN_SESSIONS`).
- **`drawdown`** (risk decay): current drawdown of the cumulative log curve since the run start
  `> DD_FACTOR × expected max DD` (2 ×) → triggers at once (no minimum sessions).
- **`volatility`** (regime change): std of the last `VOL_WINDOW` log returns × √P `> VOL_FACTOR × expected vol` for
  `VOL_CONSECUTIVE` sessions (n ≥ `MIN_SESSIONS`).
- Information-coefficient decay is NOT a rule in stage 9 (the runtime does not track live IC — noted).
- Under noise (returns drawn from the expected μ, σ) the rules must almost never fire, under a real decay (mean
  shifted down by ≥ 0.5 σ per session, or vol doubled, or a drawdown beyond the limit) they must fire within a few
  weeks — acceptance tests in 19.9 (plan acceptance "triggers on simulated decay, not on noise").
- **Checks** are stored insert-only (`mon_decay_checks`, UNIQUE run + day) with the statistics, each rule's
  `{breached, count, value, threshold}` and the resulting state. Missed days (backend down) are caught up in date
  order, each with data up to that day only.
- **States** per strategy: `ok` (no rule breached) · `watch` (a rule breached, not yet triggered — shown, no action)
  · `paused` (triggered) · after a decision `ok` again. `insufficient` while n < `MIN_SESSIONS` and no drawdown.
- **Trigger → pause** (engine, audited `decay_paused`, event row `mon_decay_events` action `paused`, alert `decay`):
  the strategy keeps its positions and its capital; the runtime records its decisions but **sends no orders for it**
  (`rt_executions` `not_executed`, reason "paused by the decay monitor — waiting for your decision (keep running /
  demote)"); protective orders (kill switch, drawdown kill, margin), manual orders and the account's other
  strategies are unaffected; incubation sessions while paused count as missed (§17.2 (c)). No auto-resume, no
  auto-demote: it stays paused until the user decides.
- **User decision** (`POST /v2/monitoring/strategies/{id}/decision`, audited `decay_decision`):
  - `keep_running` (reason 3..500) → unpaused at once; counters restart after the decision day (`return_band` and
    `volatility` need fresh consecutive breaches); `drawdown` re-arms at the decision-time drawdown + `REARM_DD_STEP`
    × expected max DD.
  - `demote` (reason + `confirm: "DEMOTE"`) → the incubation run is aborted and the strategy goes back to
    `validated` (the §17.2 abort path: `incubation.abort(move_state=True)`); its positions are then closed by the
    runtime in the next tradable window like any ended source (§17.12). Refused (409) when the strategy is not paused.

### 19.3 Alerts (`monitoring/alerts.py`) — Windows toasts in the WPF, events from the backend

- `mon_alerts` (insert-only, `dedupe_key` UNIQUE) + `mon_alert_acks` (insert-only). An alert never contains AI text,
  keys or account numbers; titles/bodies are plain text written by the backend (symbols are the only external text).
- **Kinds** (`alerts.KINDS`, severity · toast): `decay` critical · yes; `reconciliation_halt` critical · yes;
  `kill_switch` critical · yes; `drawdown_kill` critical · yes; `daily_loss_limit` warning · yes; `autotrade_off`
  (dead-man switch) warning · yes; `broker_disconnected` warning · yes; `broker_reconnected` info · no;
  `incubation_finished` info · yes; `report_ready` info · no.
- **Producers:** (a) direct: the decay monitor, the report scheduler; (b) the **audit scanner** — reads audit rows
  after the cursor `mon.alerts.audit_cursor` (hedgefund.db meta; on the very first start the cursor is set to the
  current newest id, so history does not flood the user): `broker_reconcile_mismatch` → `reconciliation_halt`,
  `platform_kill` / `broker_kill` → `kill_switch`, `post_trade_halt {kind: drawdown}` → `drawdown_kill`,
  `post_trade_halt {kind: daily_loss}` → `daily_loss_limit`, `autotrade_heartbeat_off` → `autotrade_off`,
  `incubation_completed` → `incubation_finished`; (c) the **broker link watcher** — a connected broker account whose
  link state is `waiting | error | auth_failed` for ≥ `BROKER_DOWN_MINUTES` = 10 min (since its `last_ok_at`, else
  since the watcher first saw it down) → ONE `broker_disconnected` per outage; back to `ok` after such an alert →
  `broker_reconnected`. (confirmed by the user 2026-10-24)
- **Monitor task** `sp-monitor` (main.py; own asyncio task; a failure is logged and shown in
  `overview.monitor.last_error`, never touches trading): every 30 s audit scanner + link watcher; every 60 s the
  decay checks for newly completed sessions; the report scheduler (19.4).
- **WPF contract:** poll `GET /v2/monitoring/alerts?after=<last seen id>` every 15 s while the HedgeFund backend is
  up; for each new alert with `toast: true` show ONE Windows toast (title + body as plain text, severity icon;
  clicking opens the HedgeFund window → Monitoring); `POST /v2/monitoring/alerts/ack` when the user dismisses /
  views them. Toasts are a notification channel only — every alert also stays in the list.

### 19.4 Reports (`monitoring/reports.py`) — numbers first, optional AI commentary (labelled)

- **Periods:** `daily` = a UTC calendar date (`period_id` `YYYY-MM-DD`); `weekly` = an ISO week (`YYYY-Www`,
  Monday–Sunday UTC). The scheduler creates the daily report of D at the first monitor tick after D+1 00:30 UTC and
  the weekly report of week W after the following Monday 00:30 UTC (once each; `created_by: engine`, alert
  `report_ready`); the user can create one at any time (`created_by: user`; a new row each time, newest wins).
  Stored insert-only (`mon_reports`).
- **Numbers** (deterministic, no AI): per account — equity at the last snapshot before the period and at the last
  snapshot in it, P&L, return %, max drawdown % in the period, fees, orders (total / filled / rejected / cancelled),
  halts; per active strategy — sessions in the period, log return, expected mean and incubation band for those
  sessions, z, decay state; alerts by kind; latest reconciliation per broker. A **fact table** in the §11.1 shape
  (`facts: [{id, label, value, unit, display}]`, ids like `acc.paper-main.return_pct`, `str.1.z`).
- **AI commentary** (button, `POST …/commentary {model: standard|premium, language: en|de}`): feature `sp-report`,
  label "Hedge Fund · Report commentary", via `fund.llm.chat_json` (OpenRouter key from the Credential Manager, cost
  ledger), temperature 0.2, max_tokens 1,500, timeout 90 s. **Input = the fact table only** (numbers with generated
  labels; strategies appear as "strategy 1..n", no names, no symbols, no account numbers). Output = the §11.2 shape
  `{summary, sections, hypotheses, limits}`, validated by `explain.validate_output`, every number checked by
  `explain.number_check` (flagged, never removed), `provenance("sp-report", model)`; stored insert-only
  (`mon_commentaries`, kind `report`). **Budget key `reports`** in the central budget module (`ai_budgets.py`,
  `ai_caps.DailyCaps` over hedgefund.db meta, group "Strategy Platform", default **$0.50/day**, max $20, estimate
  $0.02 standard / $0.20 premium, 0 = off; confirmed by the user 2026-10-24) — checked BEFORE the call, failed calls charged
  like the other features; blocked → 402 `{error, code: "budget", budget_key: "reports", reason}`. The Shadow
  Account commentary (19.8) uses the same key.

### 19.5 Tax export (`monitoring/tax_export.py`) — Anlage KAP basis, **not tax advice** — **REMOVED 2026-10-18 (user decision)**

> **REMOVED 2026-10-18 (user decision, quote in §19 and §19.15).** Kept below only as the record of what was built; `tax_export.py` is renamed `tax_export.py.stale`. The FX paragraph's rates live on for the Shadow Account (§19.15).

- **One CSV per account and calendar year**: platform accounts (`paper-main`, `paper-council`, `alpaca-paper`,
  `kraken-demo` — simulated → `illustrative: true`, file name `…-ILLUSTRATIVE.csv`, every row `illustrative=yes`)
  and Shadow accounts (`shadow:<id>`, imported real statements). The file is returned to the WPF, which saves it
  where the user chooses (local file); the backend keeps only an audit row (`tax_export`: account, year, sha256).
- **Method:** FIFO per account and instrument (the rule for securities in collective custody, §20 Abs. 4 Satz 7
  EStG); gain in EUR = sale proceeds converted at the SALE date − acquisition cost converted at the PURCHASE date
  (§20 Abs. 4 Satz 1 EStG), transaction fees inside cost / proceeds; partial sales split lots (one row per lot
  part); short sales (paper accounts) open a short lot, the gain is realised at the cover (dated the cover).
  Dividends / interest at the payment date, withheld tax as reported, account fees listed separately (not
  deductible, §20 Abs. 9 EStG).
- **FX source (named, existing):** FRED series **`DEXUSEU`** ("U.S. Dollars to One Euro", Federal Reserve H.10 noon
  buying rates in New York), loaded with the existing FRED client (`strategy/features/fred.py`, key `fred-api-key`,
  gate `fred`) by `POST /v2/monitoring/tax/fx {year}` into `ext_observations` (series `DEXUSEU`, source `fred`).
  EUR = USD ÷ rate. **This is NOT the ECB euro reference rate** — the app's data layer has no ECB source; whether
  the Fed H.10 rate is acceptable for the user's return (or an ECB source should be added) is a decision for the
  user / tax advisor (**user confirm**). Every file names the source. **Lookup rule** (confirmed by the user 2026-10-24): the
  rate of the transaction date; on a day without a fixing, the latest earlier fixing at most 5 calendar days back;
  otherwise the export is refused (409 `fx_missing` with the dates — refresh the rates). EUR amounts need no rate;
  other currencies are refused ("no FX source for GBP in the data layer").
- **Categories** (Anlage KAP *categories* — the line numbers change between form years, check the form of that year):
  `KAP_AKTIEN_GEWINN` gains from selling shares · `KAP_AKTIEN_VERLUST` losses from selling shares (separate loss
  pot, §20 Abs. 6 Satz 4 EStG) · `KAP_SONSTIGE` other capital gains/losses (ETFs / investment funds, bonds, other —
  InvStG partial exemption and Vorabpauschale are NOT computed) · `KAP_TERMIN` derivatives (perpetual futures incl.
  funding) · `KAP_DIVIDENDEN` dividends / distributions · `KAP_ZINSEN` interest · `STEUERABZUG` withheld tax as
  reported (domestic KapESt/Soli/KiSt or foreign withholding — crediting is NOT computed) · `NICHT_ABZIEHBAR` account
  fees · `SO_23_KRYPTO` crypto spot sales = private sales §23 EStG (Anlage SO, not KAP; `held_days` and
  `over_one_year` given) · `UNKLASSIFIZIERT` instrument class unknown (classify it) · `UNGEDECKT` a sale without an
  earlier purchase in the data (missing history — excluded from totals). Classification: platform symbols by
  `core/instruments` (ETF list → fund, other equities → share; crypto long = spot, crypto short / kraken-demo =
  perp); Shadow instruments by the statement's type column or the user's classification (`PUT
  /v2/shadow/classifications`) — never guessed from the name. The broker's country (`DE` → domestic withholding,
  `other`) is a column so the user can tell which totals the broker already taxed.
- **CSV layout** (UTF-8 with BOM, CRLF, separator `;`, decimal comma, no thousands separator, dates `YYYY-MM-DD`;
  text cells that start with `= + - @`, tab or CR are prefixed with `'` — formula-injection safe): line 1 a notice
  (`# UFOS.ai tax export <year> · <account> · NOT TAX ADVICE — an aid for your own return; verify against your
  broker's tax certificate · FX: FRED DEXUSEU (Fed H.10), not the ECB reference rate[ · ILLUSTRATIVE — simulated
  paper trades]`), line 2 the header, then one row per event, then one `summary` row per category:
  `account;broker;broker_country;year;row_type;category;date;acquired;instrument;isin;asset_class;quantity;currency;
  proceeds;cost;fx_rate_sale;fx_rate_buy;proceeds_eur;cost_eur;gain_eur;fees_eur;tax_withheld_eur;held_days;
  over_one_year;illustrative;note` — `row_type` `sale | cover | dividend | interest | withholding | fee | funding |
  summary`.
- **Out of scope (stated in the response `limits`):** InvStG (Teilfreistellung, Vorabpauschale), loss offsetting
  across accounts / years, Sparer-Pauschbetrag, crediting of foreign tax, currency gains on foreign-currency cash
  (§23), accrued interest (Stückzinsen), corporate actions other than cash dividends.

### 19.6 Shadow Account — import + normalisation (`shadow/importers.py`, `shadow/normalize.py`)

- **Data stays local:** statements are parsed in the backend, stored in hedgefund.db (`sh_*`), never sent anywhere.
  Only a user-triggered AI commentary sends NUMBERS (19.8).
- **Upload:** `POST /v2/shadow/accounts/{id}/imports?template=&profile_id=&dry_run=` as `multipart/form-data` with ONE
  part `file` (read in chunks). **Limits:** 5 MB, 50,000 data rows, 64 columns, 2,048 characters per field, NUL bytes
  refused, ≤ 200 row errors listed (counts are complete). Encoding: UTF-8 (BOM optional), else Windows-1252 when the
  bytes are not valid UTF-8 (the detected encoding is reported; a profile may fix it). Delimiter `;` `,` or tab
  (detected from the header line unless fixed). Every cell is TEXT — nothing is evaluated; formula-looking text
  (`=cmd|…`) is stored as text and neutralised on every export; a numeric field that does not parse rejects that row.
- **Schema detection:** the header row is compared with every template's required columns (case/space-insensitive);
  the best full match wins (`detection.template`, `score`, `missing`); no full match → 409 `unknown_format`
  ("map the columns with the generic importer") unless `template=generic` with a profile.
- **Templates** (`scalable` and `ibkr_flex` are marked **"verify against a real export"** — written from public
  descriptions, not from a real file; the template list says so): `trade_republic` (the REAL transaction export —
  §19.16, replaces the guessed format), `scalable` (Scalable Capital
  "Transactions" CSV: `date;time;status;reference;description;assetType;type;isin;shares;price;amount;fee;tax;
  currency`), `ibkr_flex` (IBKR Flex Query CSV, sections Trades — `CurrencyPrimary, AssetClass, Symbol,
  Description, ISIN, TradeDate, Quantity, TradePrice, IBCommission, Buy/Sell` — and Cash Transactions — `Type,
  Amount, CurrencyPrimary, Date/Time` or `SettleDate` …; a file may hold several sections, each starting with its
  own header line), `generic` (a saved mapping profile).
- **Generic mapping profile** (`POST /v2/shadow/profiles`, insert-only versions per name): `{delimiter: ";"|","|
  "\t"|"auto", decimal: ","|".", thousands: ""|"."|","|" "|"'", date_format: strptime pattern (e.g. "%d.%m.%Y"),
  encoding: "auto"|"utf-8"|"cp1252", columns: {canonical field: header}, type_map: {raw type: kind}, default_currency:
  "EUR"|"USD", amount_sign: "signed"|"positive"}`. Canonical fields: `date` (required), `type` (required), `symbol`,
  `isin`, `name`, `quantity`, `price`, `amount`, `fee`, `tax`, `currency`, `asset_class`, `time`, `status`.
- **Normalised transaction** (`sh_transactions`, insert-only): `{date, kind: buy|sell|dividend|interest|withholding|
  fee|deposit|withdrawal, key (ISIN, else symbol), symbol, isin, name, asset_class: share|etf|fund|bond|crypto|
  derivative|other|null, qty ≥ 0, price ≥ 0, currency, gross ≥ 0, fee ≥ 0, tax ≥ 0, net (signed cash effect: buy
  −(gross+fee); sell +(gross−fee−tax); dividend/interest +(gross−tax); withholding −tax; fee −fee; deposit +;
  withdrawal −)}` + `import_id`, `row`, `raw_type`. Rows of type "ignore" (e.g. cancelled, transfers the user maps
  away) are counted as `skipped`. **Dedupe:** a content hash per account (date, kind, key, qty, price, net, row
  occurrence) — re-importing the same file adds nothing (`duplicates`).
- **FIFO lots** (`normalize.fifo`): per account + key in date order (same-day: buys before sells); a sell consumes
  the oldest lots, partial lots split; a sell beyond the held quantity → the excess is `unmatched` (reported, no
  invented cost basis; tax category `UNGEDECKT`).

### 19.7 Behaviour diagnostics (`shadow/diagnostics.py`) — deterministic, each block may be `{unavailable: reason}`

Prices: `monitoring/prices.py` — daily closes from the data layer (`backtest.engine.DataLoader`, `all`-adjusted) for
instruments with a US `symbol` the data layer knows, plus the user's valuation (`PUT …/valuation {as_of, prices}`);
an instrument without a price on a needed day is counted in `coverage.unpriced` — never a stand-in.
- **Disposition effect** (Odean 1998): on every day with a sell, every held position is a realised gain/loss (sold
  that day: sale price vs FIFO average cost of the lots sold) or a paper gain/loss (held: that day's close vs average
  cost); `PGR = RG/(RG+PG)`, `PLR = RL/(RL+PL)`, `DE = PGR − PLR`, `t = DE / √(PGR(1−PGR)/(RG+PG) + PLR(1−PLR)/(RL+PL))`.
- **Overtrading / turnover:** trades, buys, sells, trades per month, traded volume, average cost basis (daily,
  cost-valued — needs no prices), `turnover_annual = (volume / 2) / avg cost basis × 365 / days`, fees, fee share of
  volume, fee drag per year (% of the average cost basis), round trips < 30 days.
- **Holding periods:** from closed FIFO lot parts (quantity-weighted): median / mean / p25 / p75 days; winners vs
  losers median (a disposition sign when losers are held longer).
- **Concentration:** cost-basis weights now and averaged over days held: HHI, effective N = 1/HHI, top-1 / top-3.
- **Trading after losses:** for each sell realising a loss vs a gain: buys in the next 5 calendar days (count and
  average size) → `buys_after_loss_avg`, `buys_after_gain_avg`, ratio, size after losses vs overall.
- **Timing vs market:** benchmark (default `SPY`) forward return over the next 20 sessions after each buy / sell vs
  the unconditional average 20-session forward return over the same period.
- Plus `facts` (§11.1 shape) for the report and the AI commentary.

### 19.8 Shadow portfolios + audit report (`shadow/shadow_portfolio.py`, `shadow/report.py`)

- **Same cash flows:** the user's dated investment flows `F_d` (buys + fees in, sell proceeds out, per day, in the
  account's base currency) are applied to a rule portfolio with the unit-value method `V_d = V_{d−1}·(1 + R_d) +
  F_d` at the day's close (fractional, no costs, no taxes; a withdrawal larger than the shadow's value makes its cash
  negative = financed at 0 %, shown as `cash_min`). **Parity (tested):** the shadow receives exactly the user's flows
  (Σ and every date). Rules (≤ 5 per request): `{type: "buy_and_hold", symbol}` (default SPY), `{type:
  "monthly_rebalance", weights: {symbol: w}}` (weights sum 1, rebalanced on the first session of each month),
  `{type: "strategy", strategy_id}` (the daily returns of the strategy's latest done backtest card of its current
  version — the card must cover the whole period, else `unavailable` with the reason). USD rule returns are applied
  to EUR flows by converting each flow at DEXUSEU of its date and the end value at the end date (FX effect included;
  rates missing → `unavailable`).
- **User side:** end value = positions at the valuation prices (market data / the user's valuation); not all priced →
  `user.valued: false` with the reason (the shadows are still shown). Money-weighted return (XIRR, annualised) for
  the user and every shadow on the identical flows; shadow time-weighted return.
- **Audit report** (`POST /v2/shadow/accounts/{id}/report {format: html|json}`): self-contained HTML (inline CSS, no
  script, no external resource, every text HTML-escaped) or JSON with: account, imports (file name, sha256, template,
  counts), period, positions, diagnostics, shadow comparison, method notes, limits, "Hypothetical comparison —
  not investment advice; your data never left this computer"; returned for the WPF to save (local file), audit
  `shadow_report_export`.
- **AI commentary** on the diagnostics + shadows (`POST /v2/shadow/accounts/{id}/commentary`): facts only (no names,
  ISINs, symbols, file names — positions appear as "position 1..n"), budget key `reports`, same rules as 19.4
  (`mon_commentaries` kind `shadow`).

### 19.9 Storage v17, audit, tests

- **hedgefund.db v17** (one transaction, all insert-only by triggers): `mon_decay_checks`, `mon_decay_events`,
  `mon_alerts`, `mon_alert_acks`, `mon_reports`, `mon_commentaries`, `sh_accounts`, `sh_profiles`, `sh_imports`,
  `sh_transactions`, `sh_classifications`, `sh_valuations`. Audit actions: `decay_paused`, `decay_decision`,
  `alerts_acked`, `report_created`, `report_commentary`, `fx_refresh`, `tax_export`, `shadow_account_created`,
  `shadow_profile_saved`, `shadow_import`, `shadow_classification`, `shadow_valuation`, `shadow_report_export`,
  `shadow_commentary`.
- **Tests** (`tests/test_sp_monitor_*.py`, offline, temp DBs, fakes): decay fires on simulated decay and not on noise
  (seeded Monte Carlo), drawdown / vol rules, no look-ahead (a check of day t ignores later returns), keep-running
  re-arm; pause semantics in the runtime (no orders, positions kept, decisions recorded, demote aborts + closes);
  alert events from every producer, dedupe, first-start cursor, ack; reports + commentary (fake LLM, budget block,
  number check, provenance); tax CSV on a synthetic year with known answers (FX at buy/sell dates, partial sales,
  dividends + withholding, shares vs other, crypto §23, short cover, missing FX → 409, formula-safe cells);
  importers on synthetic samples of each template + generic profile + malicious CSV (formula injection, huge rows /
  fields, NUL, too many rows/columns, bad numbers); FIFO + diagnostics math on known cases; shadow cash-flow parity;
  API (strict input, multipart, 404/409).

### 19.10 API (behind api_auth; strict input as §17.10 — unknown fields/parameters → 400 `{error: "invalid input",
errors}`; refused → `{error, code, …}`; never a stack trace; times UTC ISO seconds; money as JSON numbers, null when
unknown; percentages as the UI shows them, 2.0 = 2 %)

`/v2/monitoring/*`: `GET overview`; `GET strategies/{strategy_id}`; `POST strategies/{strategy_id}/decision`;
`GET decay/rules`; `POST decay/run`; `GET alerts?after=&limit=&unacked=`; `POST alerts/ack`; `GET
reports?period=&limit=`; `POST reports`; `GET reports/{id}`; `POST reports/{id}/commentary`; `GET tax/accounts`;
`GET tax/fx?year=`; `POST tax/fx`; `POST tax/export` (**the four tax routes REMOVED 2026-10-18 — §19.15**).
`/v2/shadow/*`: `GET templates`; `GET profiles`; `POST profiles`; `GET accounts`; `POST accounts`; `GET
accounts/{id}`; `POST accounts/{id}/imports` (multipart); `GET accounts/{id}/transactions?limit=&offset=`; `PUT
accounts/{id}/valuation`; `GET classifications`; `PUT classifications`; `GET accounts/{id}/diagnostics?benchmark=`;
`POST accounts/{id}/shadows`; `POST accounts/{id}/report`; `POST accounts/{id}/commentary`.

### 19.11 Exact JSON (binding for the WPF "Monitoring" and "Shadow Account" views)

**StrategyMonitor**:
```json
{"strategy_id": "s-1a2b3c4d5e6f", "name": "SMA 50/200 trend", "version": 3, "state": "incubation",
 "run_id": "inc-1a2b3c4d5e6f", "account_id": "paper-main", "run_status": "running", "calendar": "nyse",
 "sessions": 34, "last_session": "2026-10-16",
 "realised": {"cum_log_return": 0.021, "return": 0.0212, "vol": 0.11, "sharpe": 1.2, "drawdown": 0.012,
              "max_drawdown": 0.03},
 "expected": {"mean_daily": 0.0004, "std_daily": 0.008, "vol": 0.127, "sharpe": 0.79, "max_drawdown": 0.09,
              "source_run_id": "bt-…"},
 "deviation": {"z_cum": 0.4, "band": {"lo": -0.0778, "hi": 0.1050, "n": 34}, "in_band": true,
               "tracking_error": 0.05, "vol_ratio": 0.87, "dd_ratio": 0.33, "rolling_z": -0.3},
 "decay": {"state": "ok", "last_check_day": "2026-10-16",
           "rules": [{"rule": "return_band", "label": "Return below the expected band", "breached": false,
                      "count": 0, "needed": 3, "value": 0.021, "threshold": -0.083, "message": null}],
           "paused": null}}
```
`realised.*`/`deviation.*` null with fewer than 2 sessions; `decay.state` `insufficient | ok | watch | paused`;
`decay.paused` = `{at, rule, reason, check_day}` while paused; rule ids `return_band | drawdown | volatility`.

**GET /v2/monitoring/overview** →
```json
{"now": "…", "paper_only": true,
 "notice": "Monitoring of paper/demo trading · simulated · not investment advice",
 "monitor": {"running": true, "last_tick_at": "…|null", "last_error": null},
 "alerts": {"unacked": 2, "last_id": 41},
 "accounts": [{"account_id": "paper-main", "label": "Platform paper account", "broker": "internal_paper",
               "paper": true, "equity": 101234.5, "day_pnl": 120.0, "drawdown_pct": 1.2, "valuation_ok": true,
               "valuation_message": null, "halt": {"active": false, "kind": null, "reason": null, "at": null},
               "link": null, "reconciliation": null}],
 "strategies": ["StrategyMonitor"]}
```
Broker accounts: `link` = `{state, attempt, message, last_ok_at, next_retry_at}`, `reconciliation` = `{status: ok |
mismatch | unreachable, at, reason}`.

**GET /v2/monitoring/strategies/{strategy_id}** → `{"strategy": StrategyMonitor, "series": [{"day", "n", "ret",
"cum_log", "exp_mean", "band_lo", "band_hi", "decay_lo", "drawdown", "roll_vol"}], "checks": [{"day", "state",
"triggered": ["return_band"], "stats": {…}}], "events": [{"id", "ts", "action": "paused|keep_running|demoted",
"actor", "reason", "rule", "run_id"}], "rules": DecayRules}` (newest 250 checks, oldest first series); 404 `{error,
code: "not_found"}` when the strategy never incubated.

**POST /v2/monitoring/strategies/{strategy_id}/decision** `{action: "keep_running" | "demote", reason: 3..500,
confirm?: "DEMOTE"}` → `{"strategy": StrategyMonitor, "event": {…}}`; 400 confirm; 409 `{error, code: "not_paused"}`.

**GET /v2/monitoring/decay/rules** → `{"rules": {"MIN_SESSIONS": 20, "RETURN_WINDOW": 60, "RETURN_Z": 3.0,
"RETURN_CONSECUTIVE": 3, "DD_FACTOR": 2.0, "VOL_WINDOW": 20, "VOL_FACTOR": 2.0, "VOL_CONSECUTIVE": 3,
"REARM_DD_STEP": 0.25}, "text": ["…one sentence per rule…"]}` = DecayRules. **POST /v2/monitoring/decay/run** `{}` →
`{"checked": 3, "paused": ["s-…"], "errors": {}}`.

**Alert**: `{"id": 42, "ts": "…", "kind": "decay", "severity": "critical", "title": "Strategy paused: decay
detected", "body": "…", "account_id": "paper-main|null", "strategy_id": "s-…|null", "ref": "…|null", "toast": true,
"acked": false, "acked_at": null, "data": {}}`.
**GET /v2/monitoring/alerts?after=0&limit=1..500 (100)&unacked=0|1** → `{"alerts": [Alert] (newest first, id >
after), "last_id": 42, "unacked": 2, "kinds": {"decay": {"severity": "critical", "toast": true, "label": "Decay —
strategy paused"}}}`. **POST /v2/monitoring/alerts/ack** `{ids: [int] (1..500)} | {all: true}` → `{"acked": 3,
"unacked": 0}`.

**Report**: `{"id": "rep-…", "period": "daily|weekly", "period_id": "2026-10-16|2026-W42", "start": "…", "end": "…",
"created_at": "…", "created_by": "engine|user", "numbers": {"accounts": [{"account_id", "paper": true,
"equity_start", "equity_end", "pnl", "return_pct", "max_drawdown_pct", "fees", "orders": {"total", "filled",
"rejected", "cancelled"}, "halts"}], "strategies": [{"label": "strategy 1", "strategy_id", "name", "run_id",
"sessions", "log_return", "expected_mean", "band_lo", "band_hi", "z", "decay_state"}], "alerts": {"total", "by_kind":
{}}, "brokers": [{"account_id", "status", "at"}]}, "facts": [{"id", "label", "value", "unit", "display"}],
"commentaries": [Commentary]}`. **GET /v2/monitoring/reports?period=&limit=1..200 (50)** → `{"reports":
[{"id", "period", "period_id", "created_at", "created_by", "start", "end"}]}`; **GET …/reports/{id}** →
`{"report": Report}`; **POST /v2/monitoring/reports** `{period, date?: "YYYY-MM-DD"}` (a day inside the period;
default = the last complete period) → 201 `{"report"}`.
**Commentary**: `{"id": "com-…", "kind": "report|shadow", "ref_id": "rep-…|sha-…", "created_at", "model",
"language", "output": {"summary", "sections": [{"title", "text"}], "hypotheses": [], "limits": []}, "check": {"ok",
"unverified": [{"text", "offset", "section"}], "unknown_ids": []}, "usage": {…}, "cost_usd": 0.004, "provenance":
{"ai_generated": true, "generator", "provider", "model", "generated_at", "human_reviewed": false, "label"}}`.
**POST /v2/monitoring/reports/{id}/commentary** `{model: "standard"|"premium", language: "en"|"de"}` → 200
`{"commentary"}`; 402 budget; 502 `bad_json`; 504 timeout; 503 key missing.

**REMOVED 2026-10-18 (§19.15) — the following tax payloads no longer exist:** **GET /v2/monitoring/tax/accounts** → `{"accounts": [{"id": "paper-main", "kind": "platform|shadow", "label",
"broker", "broker_country": "DE|other", "illustrative": true, "years": [2026]}], "fx": {"series": "DEXUSEU",
"source": "fred", "name": "U.S. Dollars to One Euro (Federal Reserve H.10, noon buying rates in New York)", "note":
"Not the ECB euro reference rate — …"}, "notice": "…not tax advice…"}`.
**GET /v2/monitoring/tax/fx?year=2026** → `{"series": "DEXUSEU", "year": 2026, "observations": 198, "first":
"2026-01-02", "last": "2026-10-16", "fetched_at": "…|null"}`; **POST /v2/monitoring/tax/fx** `{year: 2000..2100}` →
the same + `"added": n` (network; 503 `{error, code: "fx_unavailable"}` with the FRED reason; FRED key missing → 503
with "add the FRED key in Hedge Fund → Settings → Keys").
**POST /v2/monitoring/tax/export** `{account: "paper-main" | "shadow:sha-…", year}` → 200 `{"filename":
"ufos-tax-2026-paper-main-ILLUSTRATIVE.csv", "content_type": "text/csv; charset=utf-8", "csv": "…", "sha256",
"illustrative": true, "rows": 12, "summary": [{"category", "count", "gains_eur", "losses_eur", "net_eur"}],
"warnings": ["…"], "limits": ["…"], "fx": {"series", "source", "used": 9}, "notice": "…"}`; 409 `{error, code:
"fx_missing", "missing": ["2026-03-02"]}`; 404 account.

**ShadowAccount**: `{"id": "sha-1a2b3c4d5e6f", "label": "My TR depot", "base_currency": "EUR", "broker_country":
"DE", "created_at", "imports": 2, "transactions": 140, "first_date": "2024-01-03", "last_date": "2026-10-10",
"templates": ["trade_republic"]}`.
**ImportResult**: `{"import": ImportSummary|null (null on a dry run), "detection": {"template", "score", "encoding",
"delimiter", "header": [], "missing": []}, "preview": [Txn] (≤ 20), "counts": {"rows", "accepted", "rejected",
"duplicates", "skipped"}, "errors": [{"row", "field", "message"}] (≤ 200), "warnings": []}`; ImportSummary =
`{"id": "imp-…", "account_id", "created_at", "filename", "sha256", "template", "profile_id", "rows", "accepted",
"rejected", "duplicates", "skipped"}`; Txn = `{"id", "date", "kind", "key", "symbol", "isin", "name", "asset_class",
"qty", "price", "currency", "gross", "fee", "tax", "net", "import_id", "row", "raw_type"}`.
- `GET /v2/shadow/templates` → `{"templates": [{"id", "label", "verify": true, "verify_note", "required_columns",
  "type_map"}], "canonical_fields": [], "kinds": [], "asset_classes": [], "limits": {"max_bytes": 5242880,
  "max_rows": 50000, "max_columns": 64, "max_field_chars": 2048}, "notice": "…stays on this computer…"}`.
- `GET/POST /v2/shadow/profiles` → `{"profiles": [{"id", "name", "version", "created_at", "mapping"}]}` / 201
  `{"profile"}` (body `{name: 1..60, mapping}`; invalid mapping → 400 per field).
- `GET /v2/shadow/accounts` → `{"accounts": [ShadowAccount], "notice"}`; `POST` `{label: 1..60, base_currency:
  "EUR"|"USD", broker_country: "DE"|"other"}` → 201 `{"account"}`.
- `GET /v2/shadow/accounts/{id}` → `{"account", "imports": [ImportSummary], "positions": [{"key", "symbol", "isin",
  "name", "asset_class", "currency", "qty", "cost", "avg_cost", "lots"}], "unmatched": [{"date", "key", "qty"}],
  "valuation": null | {"as_of", "prices": [{"key", "price"}], "ts"}}`.
- `POST /v2/shadow/accounts/{id}/imports?template=auto|trade_republic|scalable|ibkr_flex|generic&profile_id=&dry_run=
  true|false` (multipart, part `file`) → 200 (dry run) / 201 ImportResult; 400 too large / malformed; 409
  `unknown_format` (+ `detection`).
- `GET /v2/shadow/accounts/{id}/transactions?limit=1..1000 (200)&offset=0` → `{"transactions": [Txn], "total"}`
  (date order).
- `PUT /v2/shadow/accounts/{id}/valuation` `{as_of: "YYYY-MM-DD", prices: [{key, price > 0}] (1..500)}` →
  `{"valuation"}`.
- `GET/PUT /v2/shadow/classifications` → `{"classifications": [{"key", "asset_class", "ts", "reason"}]}` / `{key,
  asset_class, reason?}` → `{"classification"}`.
- `GET /v2/shadow/accounts/{id}/diagnostics?benchmark=SPY` → `{"account_id", "base_currency", "period": {"first",
  "last", "days"}, "coverage": {"instruments": 12, "priced": 9, "unpriced": ["key…"], "price_source"},
  "disposition": {"pgr", "plr", "de", "t_stat", "realized_gains", "realized_losses", "paper_gains", "paper_losses",
  "sell_days", "unpriced_cases"}, "turnover": {"trades", "buys", "sells", "trades_per_month", "volume",
  "avg_cost_basis", "turnover_annual", "fees", "fee_share_pct", "fee_drag_annual_pct", "round_trips_under_30d"},
  "holding": {"closed_parts", "median_days", "mean_days", "p25_days", "p75_days", "winners_median_days",
  "losers_median_days"}, "concentration": {"current": {"hhi", "effective_n", "top1_pct", "top3_pct"}, "average":
  {"hhi", "effective_n", "top1_pct", "top3_pct"}, "positions", "basis": "cost"}, "after_losses": {"window_days": 5,
  "loss_sells", "gain_sells", "buys_after_loss_avg", "buys_after_gain_avg", "ratio", "size_after_loss_avg",
  "size_avg"}, "timing": {"benchmark", "horizon_sessions": 20, "buys", "sells", "buy_fwd_avg_pct",
  "sell_fwd_avg_pct", "uncond_fwd_avg_pct"}, "facts": []}` — any block may be `{"unavailable": "reason"}`.
- `POST /v2/shadow/accounts/{id}/shadows` `{rules: [ShadowRule] (1..5)}` → `{"account_id", "base_currency",
  "period": {"first", "last"}, "flows": {"count", "sum_in", "sum_out"}, "user": {"valued", "message", "end_value",
  "pnl", "mwr_annual"}, "shadows": [{"rule", "label", "end_value", "pnl", "mwr_annual", "twr", "cash_min",
  "parity": {"flows_equal": true, "max_abs_diff": 0.0, "count": 41}} | {"rule", "label", "unavailable"}], "notice":
  "Hypothetical — no costs, taxes or slippage; fractional units; flows at the day's close. Not investment advice."}`.
- `POST /v2/shadow/accounts/{id}/report` `{format: "html"|"json", rules?: [ShadowRule], benchmark?}` →
  `{"filename", "content_type", "content", "sha256", "generated_at"}`.
- `POST /v2/shadow/accounts/{id}/commentary` `{model, language, benchmark?}` → `{"commentary"}` (kind `shadow`).

WPF notes (binding): AI commentary blocks follow the CLAUDE.md AI labelling rules (✦ trigger, badge before, caveat
after, violet container, cost line, provenance kept); the tax export dialog shows the notice + the FX source line
and, for paper accounts, "ILLUSTRATIVE — simulated trades, not reportable"; the Shadow Account view states "Your
statements stay on this computer — nothing is uploaded; an AI commentary sends only numbers."

### 19.12 Stage 9 backend notes (implemented 2026-10-18: §19.1–§19.11, backend part — for the WPF "Monitoring" and "Shadow Account" views)

All §19.11 payloads are implemented as written, with these additions / deviations (the WPF agent binds to them):
- **Decay constants after calibration (confirmed by the user 2026-10-24):** `RETURN_Z = 3.0` (not 2.33) and `DD_FACTOR = 2.0`
  (not 1.5) — the contract values fired on pure noise in ≈ 21 % of 250-session runs. Seeded Monte Carlo (200 runs,
  μ = 0.0004, σ = 0.01, holdout max DD from a 500-session path), now: noise over 250 sessions → ≈ 5 % false pauses (one
  per ≈ 20 strategy-years); a decay of the daily mean by 0.5 σ → paused in 100 % of runs, median 33 sessions; the same
  after 40 good sessions → ≥ 93 %; volatility × 2.5 → all within ≤ 30 sessions; a decay of only 0.3 σ is caught in
  ≈ 2/3 of runs within 120 sessions (the honest limit of the rule). `GET /v2/monitoring/decay/rules` returns the final
  numbers: `{"MIN_SESSIONS": 20, "RETURN_WINDOW": 60, "RETURN_Z": 3.0, "RETURN_CONSECUTIVE": 3, "DD_FACTOR": 2.0,
  "VOL_WINDOW": 20, "VOL_FACTOR": 2.0, "VOL_CONSECUTIVE": 3, "REARM_DD_STEP": 0.25}`; `text` = 4 sentences.
- **Realised series** starts at the snapshot of the session BEFORE `start_date`, so the first session's own return
  counts (finding: stage 7's `incubation.realised` starts at `start_date` and therefore drops the first session's
  return from the §17.2 band — left unchanged, noted for the stage-10 review).
- **Pause in the runtime** (`live/runtime.py::_execute`): when a paused run's decision reaches its execution window,
  it is recorded `rt_executions {status: "not_executed", reason: "paused by the decay monitor — waiting for your
  decision (keep running / demote)"}`; `decay.paused_run_ids()` failing fails that account's execution step closed
  (no orders). Kill switch, drawdown kill, margin and manual orders are untouched. `GET /v2/incubation/{id}`
  decisions show the `not_executed` status with that reason.
- **Decision route:** `confirm` is required (`"DEMOTE"`) for `demote` and refused for `keep_running` (400 `{errors:
  {confirm}}`); 409 `not_paused`; 404 `not_found` (never incubated). Response `event` = `{id, ts, action, actor,
  reason, rule, run_id}`. `StrategyMonitor.decay.rules[]` are computed live from the stored series (the same rule
  and re-arm as the checks); only a stored `paused` event makes `state: "paused"`.
- **Alerts:** `GET /v2/monitoring/alerts` is always newest first (also with `after`); `data` carries `{audit_id,
  action}` for scanner alerts, `{rule, day}` for `decay`, `{state, since}` for `broker_disconnected`.
  `report_ready` alerts are created only for scheduler (engine) reports. All GETs of `/v2/monitoring/*` overview,
  strategies, alerts and reports touch the one heartbeat. Monitor task `sp-monitor` in main.py (30 s tick; decay +
  reports every 2nd tick); `overview.monitor = {running, last_tick_at, last_error}`.
- **Overview accounts:** `{account_id, label, broker, paper: true, equity, day_pnl, drawdown_pct, valuation_ok,
  valuation_message, halt, link, reconciliation}` — `reconciliation = {status: ok|mismatch|unreachable|never, at,
  reason}` (from the stage-8 AccountSummary), `link` only for broker accounts.
- **Reports:** P&L = equity change minus cash flows of kinds `deposit, reset, broker_adjustment, migration` in the
  period; `equity_start` = the last `pa_equity` point before the period (else its first point); `fees` = −Σ `fee` +
  `liquidation_fee` cash rows; `halts` = alerts of kinds drawdown_kill / kill_switch / reconciliation_halt /
  daily_loss_limit for that account. Report facts use units `usd | percent | count | fraction | number | status`.
  Commentary: one at a time (409 `busy`), 402 `{error, code: "budget", budget_key: "reports", reason}`, 502 `bad_json`
  / `ai_error`, 503 `no_key` / `no_model`, 504 `timeout`; a timeout or an attempted failed call books the estimate,
  a bad answer books its reported cost. Feature id `sp-report` in the cost ledger. The prompt contains only `{facts:
  [{id, label, display}]}`.
- **Budget key `reports`:** `ai_budgets.reports_caps()` (`ai_caps.DailyCaps`, hedgefund.db meta, audited
  `ai_budget_changed`), source `ReportsSource` between the loop hook and the Newsroom caps → `/ai/budgets` order: fund,
  explain, ideation_month, ideation_day, (loop), **reports**, newsroom_analysis, newsroom_insider_summary;
  `UFOS.ai/docs/ai-budgets-spec.md` updated (§4.3 table, §4.2 list, HedgeFund order). The MainWindow AI-budgets
  window renders entries generically from the payload — no WPF change is needed for the new key.
- **(REMOVED 2026-10-18, §19.15) Tax export:** `GET /v2/monitoring/tax/accounts` adds `categories: {id: label}`; Shadow years come from the
  transactions, platform years from filled `pa_orders` + income/fee cash rows. The CSV's first line is ONE unquoted
  notice (its `;` replaced by `,`). Summary rows: `proceeds_eur` = positive total, `cost_eur` = negative total,
  `gain_eur` = net (for `STEUERABZUG` the withheld tax incl. the tax on sale / dividend rows; `UNGEDECKT` rows count
  but are excluded from totals). Platform fills are ordered by fill id (a short before its cover), Shadow
  transactions by date with buys before sells. Platform classification: KNOWN_ETFS → fund (KAP_SONSTIGE), other
  equities → share; internal-paper crypto long = spot (SO_23), crypto short and every `kraken-demo` fill = derivative
  (KAP_TERMIN); equity short covers → KAP_SONSTIGE with the note "short sale — classification to verify"; borrow fees
  → NICHT_ABZIEHBAR, funding / liquidation fees → KAP_TERMIN, dividend compensation paid on a short → KAP_SONSTIGE.
  Errors: 409 `fx_missing {missing: [dates ≤ 200], years}`, 409 `fx_unsupported {currencies}`, 404 `not_found`.
  `POST /v2/monitoring/tax/fx` loads 20 Dec of the year before … 31 Dec (≤ today); 503 `fx_unavailable` (FRED key
  missing / FRED down), 409 `future_year`.
- **Shadow imports:** the multipart reader is `api/monitoring_routes.read_csv_body` (reads_body mode `"csv"` in
  `routes.register`); unknown fields are reported before field errors (as in stage 8). A dry run returns 200 with
  `import: null`, a real import 201; 409 `unknown_format` carries `detection`; 400 codes `too_large | empty | binary |
  encoding | malformed | too_many_rows | too_many_columns | csv_invalid | profile_required`. Rows of IBKR
  `LevelOfDetail` ≠ EXECUTION and FX (`AssetClass CASH`) trades are `skipped`; Scalable rows with a status other than
  executed are `skipped`; IBKR withholding-tax refunds (positive amounts) are row errors. Section codes `HEADER/DATA`
  (Flex "include section codes") are understood. Transactions: the API also returns `key`, `gross`, `net`.
  `GET /v2/shadow/accounts/{id}` positions carry the FIFO cost in the instrument's currency.
- **Diagnostics / shadows:** the end date of every comparison = the latest valuation's `as_of`, else today (UTC);
  prices "on" a day = the last close at most 4 calendar days before (diagnostics) / 6 days (shadow rule prices);
  your valuation price wins over market data on its as-of day; a transaction whose currency cannot be converted to
  the base currency is left out with a `warnings` entry (diagnostics + shadows both carry `warnings: []`).
  `mwr_annual` is null when no annualised root exists in [−99.99 %, +1,000 %] (e.g. a few days of history) — never a
  stand-in. The `strategy` rule uses the latest done `backtest` card of the strategy's CURRENT version. Diagnostics
  payload adds `warnings`; an account without convertible transactions answers `{…, "period": null, "unavailable":
  reason, "facts": []}`.
- **Report export:** HTML with `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'`, no script,
  every text escaped; JSON = the same content structured; audit `shadow_report_export {account_id, format, sha256}`.
- **Files:** `monitoring/{__init__, expected, decay, alerts, fx, prices, reports, service, tax_export}.py`,
  `monitoring/shadow/{__init__, importers, normalize, diagnostics, shadow_portfolio, service, report}.py`,
  `api/monitoring_routes.py` (30 routes, mixin of `V2Api`, appended to `routes.ROUTES`), `storage/db.py` **v17** (12
  tables, all insert-only by triggers), `live/runtime.py` (pause hook), `api/routes.py` (mixin, routes, `"csv"` body
  mode), `ai_budgets.py` (`reports`), `main.py` (task `sp-monitor`).
- **Tests** (offline, temp DBs, fakes; `tests/sp_monitor_testkit.py`): `test_sp_monitor_decay.py` (14: calibration
  noise vs decay ×4, rule units, re-arm, no look-ahead + pause + alert + audit, decisions keep/demote, overview),
  `test_sp_monitor_pause.py` (3: runtime withholds orders / keeps positions / resumes; demote closes; kill still works),
  `test_sp_monitor_alerts.py` (5), `test_sp_monitor_reports.py` (5), `test_sp_monitor_tax.py` (6: known answers incl.
  FX at both dates, partial sales, weekend rate, dividends + withholding, uncovered sale, crypto §23 + one-year flag,
  classification, formula safety, short cover on paper, missing FX, FRED refresh fake), `test_sp_monitor_shadow.py`
  (16: every template, generic profile, malicious files, FIFO, diagnostics math, shadow parity incl. financing,
  monthly rebalance, XIRR, report escaping, API end to end). `test_ai_budgets.py` updated (key `reports`). Full
  suite green on the VM (Python 3.10) in split runs; scope guards stage0 / mr0 / budget0 ok.
- **Known limits / verify:** the three import templates are written from public descriptions — **verify against
  real exports** (column names, signs of amounts/fees, date formats; Trade Republic's own export may differ from
  the assumed `Datum;Typ;Wert;Notiz;ISIN;Stück;Gebühren;Steuern`); FX = FRED DEXUSEU (Fed H.10), **not** the ECB
  reference rate (user decision); InvStG, loss pots across accounts, foreign-tax crediting and corporate actions are
  not computed (listed in `limits`); IC decay is not a rule; Shadow instruments with only an ISIN (TR / Scalable) have
  no market price — disposition paper gains/losses and the user's end value need a valuation; the alert stream is
  polled (no push channel to the WPF). **The WPF still has to build** the Monitoring view, the Shadow Account view and
  the toast notifier (poll `GET /v2/monitoring/alerts?after=`).

### 19.13 Stage 9 WPF notes (implemented 2026-10-18: rail entries "Monitoring" + "Shadow Account", toasts — bound to §19.11/§19.12)

Static checks only (no dotnet on the VM — the user builds). PAPER / DEMO ONLY: nothing in these views places an order; "Demote" uses
the backend's §17.2 abort path.
- **Files (new):** `Platform/PlatformClient.Monitoring.cs` (15 `/v2/monitoring/*` routes, DTOs exactly §19.11/§19.12, computed props
  `[JsonIgnore]`, `CommentaryFailureText` = 402 `error` + `reason` + budget key verbatim, `GetTaxNoticeAsync`), `Platform/PlatformClient.Shadow.cs`
  (15 `/v2/shadow/*` routes incl. the multipart import — ONE part `file`, `profile_id` only with `template=generic`; `ReadShadowRefusal`
  reads 409 `unknown_format` + `detection`), `Platform/MonitoringUi.cs` (chips/tables, `ShJson` readers for raw blocks, `MonCommentaryPanel`,
  `MonitoringBandChart`), `Platform/MonitoringView.cs` + `.Strategies.cs` + `.Alerts.cs`, `Platform/MonitoringDialogs.cs` (`MonDecisionDialog`),
  `Platform/MonitoringNotifier.cs` (toasts), `Platform/ShadowAccountView.cs`, `Platform/ShadowDialogs.cs` (new account, valuation,
  classification, mapping profile, import wizard), `HedgeFundWindow.Monitoring.cs`. **Edited:** `HedgeFundWindow.xaml` (rail
  `NavMonitoringButton` + badge `NavMonitoringBadge`, `NavShadowButton`, hosts `MonitoringHost` / `ShadowHost`), `HedgeFundWindow.xaml.cs`
  (rail switch, notifier start + badge + pending toast open in `Window_Loaded`, unsubscribe + `Shutdown` on close),
  `Fund/FundNotifications.cs` (`ShowMonitoringAlert`, toast arguments `ufos=monitoring;alert=<id>`, activation routing),
  `Fund/FundSettings.cs` (`MonitoringToasts`, key `monitoring_toasts`, default on).
- **Toasts (decision):** the module already uses `Microsoft.Toolkit.Uwp.Notifications` 7.1.3 with TFM `net10.0-windows10.0.19041.0`
  (Fund/FundNotifications.cs) → reused; **no package, no TFM, no csproj change**. `MonitoringNotifier` (app-wide like FundNotifications,
  started by the Hedge Fund window, keeps running while UFOS.ai runs — a toast must also reach a hidden/closed window) polls `GET
  /v2/monitoring/alerts?after=<last id>` every 15 s only while the backend is online; the first poll only remembers `last_id` (no replay
  after a start — old alerts are in the list + badge); every new `toast: true` alert → one toast ("Hedge Fund · Critical/Warning · title",
  body, attribution "Paper / demo trading · simulated · not investment advice"); > 4 in one poll → one summary toast. On/off: Monitoring →
  Alerts "Windows toasts for monitoring alerts" AND the existing "Mute notifications". Click → the open Hedge Fund window shows Monitoring →
  Alerts with that alert selected and acked; no window open → main window to front and the next Hedge Fund window opens there
  (`MonitoringNotifier.RequestOpen`). In-app: rail badge "● n" (unacked), Monitoring banners (paused strategies, unacked alerts, monitor
  error, connection lost).
- **Monitoring view** (Ctrl+1…5, F5, Ctrl+B = Keys & data → Brokers; polls overview + the open section every 10 s only while visible):
  *Strategies* — per StrategyMonitor: chips (decay state, run status, account, INSIDE/OUTSIDE THE BAND), realised vs expected (cumulative
  log return as simple %, band, z, rolling z, vol / DD / Sharpe vs expected, tracking error, frozen μ/σ), rules table (value vs threshold,
  count of needed, message); PAUSED → red box: since/check day/rule, the backend's reason verbatim, the rule's numbers, consequences, buttons
  **Keep running…** (dialog: why, what happens incl. re-arm `REARM_DD_STEP`, required tick, audit note 3–500) and **Demote…** (typed
  `DEMOTE` + audit note; abort → validated, positions closed by the runtime); 400/409 verbatim in the dialog. Expander per strategy: band
  chart (realised, expected mean dashed, band area, decay line red dashed; focusable, ←/→/Home/End cursor announced), events (pause /
  keep / demote, reason), newest 15 checks, rule text. *Accounts & brokers* — monitor task (running, last tick, last error), per account
  equity / day P&L / drawdown, halt chip + reason, broker link + reconciliation chips (`BrokerText`) with texts verbatim, "Keys & data →
  Brokers…". *Alerts* — toast setting, "only not yet seen", list (severity, NEW/SEEN, time, kind label, title, body; Enter / double click
  opens the strategy / account), detail (data, ref), mark one / all as seen, kinds table. *Reports* — daily/weekly list, "Create report
  now…" (optional day), numbers tables (accounts, strategies, alerts by kind, brokers), fact-table expander, **✦ AI commentary…**
  (Standard/Premium, EN/DE; badge `AiContent.Badge` before, caveat after, violet container, cost line, number check: unverified numbers
  red in the text + listed, unknown ids; failures with `AiContent.UnavailableBadge` + the backend's text verbatim, 402 incl. reason and
  budget key "reports"). *Tax reports* (Ctrl+5) — replaces the former Tax export (user decision 2026-10-18, §19.15: UFOS.ai produces NO tax data):
  a fixed notice, `notice` + `paper_notice` of `GET /v2/monitoring/tax-notice` verbatim as plain text (AutomationProperties); a
  failed call → "Tax reports notice not available (reason)", no stand-in text. Removed from the WPF: tax DTOs / client methods, the
  export + SaveFileDialog flow, FX loading in Monitoring, `fx_missing` handling (no whole file became unused → nothing renamed to
  `.stale`; the csproj compiles by glob, no explicit file names).
- **Shadow Account view** (no polling; Ctrl+1…5, Ctrl+O import, F5): header chip DATA STAYS ON THIS PC + the §19.11 sentence; account
  picker, "New Shadow account…" (label, EUR/USD, DE/other). *Overview* — account, **FX rates (EUR/USD)** panel (§19.15 `GET|POST
  /v2/shadow/fx`, years from the account's transactions, "Load FX rates"; conversions only, not tax data), imports (file, template,
  counts, sha256), FIFO positions,
  "Set valuation prices…" (as of + price per key), "Classify an instrument…" (asset class + reason), unmatched sales. *Transactions* —
  paged 200. *Diagnostics* — benchmark + Compute; blocks coverage, disposition (PGR/PLR/DE/t), turnover, holding periods, concentration,
  after losses, timing; each block shows "Unavailable: <reason>" verbatim; warnings. *Shadow portfolios* — up to 5 rules (buy & hold /
  monthly rebalance "SPY=0.6, AGG=0.4" / strategy id; checked client-side as the backend does), table you vs each rule (end value, P&L,
  MWR, TWR, cash-flow parity "same flows ✓ (n)" or FLOWS DIFFER, negative cash note), notice. *Export & AI* — HTML/JSON audit report →
  SaveFileDialog (sha256 re-checked), AI commentary (numbers only, budget "reports").
- **Import wizard:** 1 File — limits from `GET templates` checked BEFORE sending (size, NUL bytes, ≈ lines, header columns); 2 Format —
  Auto / Trade Republic / Scalable Capital / IBKR Flex (each "VERIFY AGAINST A REAL EXPORT" chip + verify_note + required columns + type
  map) / Generic with a profile (latest version per name) or "New mapping profile…" (delimiter, decimal, thousands, date format,
  encoding, currency, sign, header per canonical field offered from the file, type map lines `raw = kind`); 3 Preview — dry run:
  detection (template, score, encoding, delimiter, header, missing), counts, ≤ 200 row errors, warnings, first 20 normalised rows; 409
  `unknown_format` → message + detection + "Use the generic importer…"; 4 Import → result (id, sha256, counts). Imported text is plain
  `TextBlock.Text` everywhere (nothing evaluated).
- **Aligned with §19.14 (re-read 2026-10-18, overrides the items above where they differ; tax items superseded by §19.15):** `overview.monitor.{stale, tick_in_progress, tick_started_at}` → "MONITOR STALE" header + banner; a strategy entry with `error` → card
  "NOT EVALUATED" + the error (never hidden). Decay: `decay.basis` with `basis_labels` + RETURN_Z / DD_FACTOR of that basis on every card
  (`GET decay/rules` loaded on activation / F5), `since_index` > 0 → "evaluating returns since your keep-running decision (after session
  n)", detail table "Calibration per holdout basis" (`by_basis`, `default_basis` marked); the keep-running dialog explains the fresh
  sample of `MIN_SESSIONS` and drawdown from the decision (REARM_DD_STEP removed). Protective exits: `decay.protective_exits` count on
  the card, `protective_exits[]` table in the detail; alert kind `protective_exit_while_paused` (warning, toast) needs no special code —
  toast + list use the backend's title / body / kind label, "Open strategy" via `strategy_id`. Diagnostics: `coverage.excluded [{key,
  name, reason}]` and `disposition.excluded_instruments` listed verbatim. Shadow: `POST …/imports/{import_id}/remove {confirm: "REMOVE
  IMPORT", reason}` → Overview "Remove <file>…" (typed REMOVE IMPORT + reason); 409 `duplicate_import` → verbatim + "remove the earlier
  import first". Paths / codes / fields checked against §19.14.
- **WPF review fixes 2026-10-18 (independent review of the stage-9 WPF, no HIGH):** M1 every Shadow dialog (new account, valuation,
  classification, mapping profile) has a `_closed` guard — Esc while a request runs never sets `DialogResult` on a closed window (the
  import wizard and MonDecisionDialog already had it); M2 a toast that cannot be shown is never dropped silently:
  `FundNotifications.MonitoringToastFailure` → `MonitoringNotifier.ToastProblem` ("Windows toasts are not available (…)") shown as a
  Monitoring banner and in Alerts; an unreadable toast setting means NO toast + that reason (never assumed on); M3
  `disposition.excluded_instruments` is read as the NUMBER of instruments not counted (reasons in `coverage.excluded`). LOW: no stand-in
  values for missing data — import limits / canonical fields / kinds / asset classes missing → "not available (reason)" and the step is
  blocked; MIN_SESSIONS unreadable → named, not assumed; the size
  limit is re-checked on the bytes actually read (TOCTOU); the paused box and the banner say that the strategy's OWN reduce-only
  protective exits (stop loss / take profit / drawdown stop to 0) still run while paused (§19.14 M3, confirmed by the user 2026-10-24).
- **Findings / limits:** shadow commentaries are not listed by the backend (no GET) — only those created in the session are shown; the
  notifier cannot know alerts fired while UFOS.ai was closed except via the unacked count (badge / list).
- **Click-test (user):** (1) rail → Monitoring: header monitor line, Strategies empty text or cards; (2) incubating strategy → chips,
  numbers, rules table; expander → band chart, ←/→ reads values; (3) force a pause (test) → red box, banner, toast "Hedge Fund ·
  Critical · Strategy paused…" → click → Monitoring → Alerts with it selected; (4) Keep running… → tick + note → card back to OK;
  Demote… → typed DEMOTE → strategy gone, Library shows validated; (5) Accounts & brokers → chips / "Keys & data → Brokers…"; (6) Alerts →
  toggle toasts off/on, mark all as seen → badge disappears; (7) Reports → Create report now (empty day) → tables; ✦ AI commentary →
  Generate → labelled block + number check; budget 0 → 402 text verbatim; (8) Monitoring → Tax reports shows the tax-reports notice (backend text; backend stopped → "notice not available (…)"); (9) Shadow Account → New account → Import statement… → pick a TR/Scalable/IBKR CSV → Auto → Preview → Import; same file again
  → duplicates; a CSV with `=cmd|…` cells shows them as text; > 5 MB file → refused before sending; (10) Generic profile from an unknown
  CSV; (11) Diagnostics → Compute (unavailable blocks with reasons), Set valuation prices → recompute; (12) Shadow portfolios → SPY + 60/40
  → parity ✓; (13) Export audit report (HTML) → opens offline; (14) close/reopen the window: no Monitoring poll while hidden; toasts keep
  coming while UFOS.ai runs.
- **Stage 9c alignment (§19.16, 2026-10-18):** import preview / result show `counts.already_known` ("n transaction(s) already
  imported — skipped"); transactions (list + preview) show the kind labels incl. "staking reward", "tax refund", "pending
  private-market order", "corporate action (unpaired)", "Original (FX)" (`original_amount` `original_currency` @ `fx_rate`, as
  reported) and "Note / id" (`note`, `ext_id`); the Trade Republic template no longer carries the "VERIFY AGAINST A REAL EXPORT" mark
  (backend `verify: false`) and shows its `privacy` text. Click-test: (16) import the TR export → Import; import the same export
  again → refused as `duplicate_import`; import a LATER, overlapping TR export → preview "n transaction(s) already imported —
  skipped", only the new rows are added.

### 19.14 Review fixes 2026-10-18 (independent stage-9 review: H1, H2, M3–M6, LOW — backend; JSON changes for the WPF)

These override §19.2, §19.5, §19.7, §19.11 and §19.12 wherever they differ. Storage is now **hedgefund.db v18**.

- **H1 — FX strict (no unrequested fallback).** (**The tax parts REMOVED 2026-10-18 — §19.15**; the exact-date rule stays for the Shadow Account conversions.) `monitoring/fx.py` uses the DEXUSEU fixing of the EXACT date only.
  The "last earlier fixing ≤ 5 days" lookup is removed. A date the export needs without a fixing → **409
  `fx_missing`**:
  ```json
  {"error": "EUR/USD rates (FRED DEXUSEU) are missing for 2 date(s) the export needs — …", "code": "fx_missing",
   "missing": ["2026-01-01", "2026-06-01"],
   "details": [{"date": "2026-01-01", "reason": "FRED DEXUSEU has no fixing on that day (e.g. a US holiday or a weekend)",
                "rows": ["sale 2026-01-01 AAPL (acquired 2025-03-03)"]},
               {"date": "2026-06-01", "reason": "after the last loaded fixing (2026-05-05) — refresh the FX rates",
                "rows": ["…"]}],
   "years": [2026], "last_fixing": "2026-05-05"}
  ```
  `details[].rows` lists at most 20 descriptions per date. **Lazy FX (LOW):** amounts stay in the transaction
  currency, and a rate is required only for a lot part REALISED in the export year: its sale date and its purchase
  date (for a short: the sale is at the open, the purchase at the cover). Plus the dates of in-year income, tax and
  fee rows, and of uncovered sales. Lots closed before the year, and lots still open, need no rate.
  **CSV columns** (inserted after `fx_rate_buy`): `fx_date` (date of the fixing used for the sale or event amount),
  `fx_date_buy` (date of the fixing used for the acquisition cost) and `fx_source` (`FRED DEXUSEU`; empty for EUR).
  Response `fx` = `{series, source, used, lookup: "exact date only"}`. `GET /v2/monitoring/tax/accounts` `fx` adds
  `lookup` and `open_question`. `POST /v2/monitoring/tax/fx` now loads 1 Jan … 31 Dec (≤ today) of the year.
  **OPEN QUESTION for the user:** allow using the last earlier fixing (marked per row) for days without a FRED
  fixing, e.g. US holidays or crypto weekends? Until the user decides, such days are refused.
  Shadow conversions use the same exact rule: a transaction without a fixing is left out with a `warnings` entry, and
  a shadow flow without one makes that rule `unavailable`.
- **H2 — diagnostics price basis** (`monitoring/prices.py`, `shadow/service.Context`):
  - **Raw closes** (as traded) for every comparison with a trade price: paper gains/losses and the user's end value.
    Raw = the `split`-adjusted series × the split ratios (data-layer corporate actions, new/old) with a LATER
    ex-date. The `all` (total-return) series is used only for return series: shadow rules and benchmark timing.
  - A stock split after the instrument's first imported purchase → that instrument is **excluded** ("not available:
    stock split of X on D — imported lots are not split-adjusted"). Missing corporate actions → excluded.
  - Market data **only** for instruments identifiable as US-listed securities in USD: a symbol, currency USD, ISIN
    absent or starting with `US`, and asset class not crypto, derivative or bond. Every other instrument → "not
    available: no price source for <ISIN or name>" unless the user valued it. Never a ticker guess. Prices are of
    the exact day (no earlier close).
  - `coverage` = `{instruments, priced, excluded: [{key, name, reason}] (≤ 200), unpriced: [key], price_source}`.
  - `disposition` adds `excluded_instruments`. `shadows.user.message` names each unpriced position with its reason.
- **M3 — pause vs protective exits** (`live/runtime.py::_execute`; **confirmed by the user 2026-10-24**).
  While a strategy is paused, its own risk-REDUCING protective exits still run: the decision's `force` symbols that
  it holds (stop loss, take profit, drawdown stop) go to 0. Nothing that opens or increases exposure runs; all other
  targets are ignored. Without such an exit the decision stays `not_executed` (paused reason). An executed
  protective exit has `rt_executions.notes` "<run id>: paused by the decay monitor — protective exits only (SPY)".
  **Every such order is recorded** (addition): a new alert kind `protective_exit_while_paused` (severity warning,
  toast yes), deduped per order and source (`protexit:<client_order_id>:<source_id>`). Alert `data`:
  `{run_id, symbol, qty, side, client_order_id, state, reason}`; `strategy_id`/`account_id`/`ref` (= run id) are
  set. The strategy view shows it in two places:
  - `GET /v2/monitoring/strategies/{id}` adds `"protective_exits": [{"alert_id", "ts", "run_id", "symbol", "qty",
    "side", "client_order_id", "state", "reason"}]` (newest first, ≤ 100).
  - `StrategyMonitor.decay.protective_exits` = the count for the run.
- **M4 — keep running.** After the decision ONLY the returns after it count: a fresh sample with the same
  `MIN_SESSIONS` before `return_band` / `volatility` can fire, and the drawdown is measured from the decision.
  `REARM_DD_STEP` is gone. `assess` returns `since_index` (sessions before the decision). Rule messages read
  "needs 20 sessions since your keep-running decision (n so far)". Simulated: a year of noise after a keep-running
  at session 60 re-pauses ≤ 7 % (test).
- **M5 — calibration per holdout basis** (`decay.RULES_BY_BASIS`, ONE place; **confirmed by the user 2026-10-24**):
  - `basis_of(expected)` picks the set: `crypto_12m` for UTC calendars. For equity it uses the holdout session count
    (`expected.sessions`): ≥ 378 → `equity_24m`, ≥ 189 → `equity_12m`, else `equity_6m`.
  - Shared constants: `MIN_SESSIONS 20, RETURN_WINDOW 60, RETURN_CONSECUTIVE 3, VOL_WINDOW 20, VOL_FACTOR 2.0,
    VOL_CONSECUTIVE 3`.
  - Per basis (`RETURN_Z` / `DD_FACTOR`): **equity_24m 3.0 / 2.0 · equity_12m 3.5 / 2.5 · equity_6m 3.5 / 3.5 ·
    crypto_12m 3.2 / 2.5**.
  - Seeded Monte Carlo (200 runs per basis; μ/σ = 0.04 per session: equity σ 1 %, crypto σ 3 %). The expected basis
    is ESTIMATED from a simulated holdout of that length; live noise is the true process for one strategy-year (252
    NYSE / 365 UTC sessions); decay = mean − 0.5 σ for 120 sessions:

    | basis | noise pauses / strategy-year | decay caught | median sessions to the pause |
    |---|---|---|---|
    | equity_24m (504) | 3.5 % | 98 % | 35 |
    | equity_12m (252) | 4.5 % | 95 % | 46 |
    | equity_6m (126) | 5.0 % | 89 % | 42 |
    | crypto_12m (365) | 4.5 % | 95.5 % | 39 |

    Shorter holdouts estimate μ, σ and the max drawdown less precisely, which explains the wider bands. Tests:
    `RuleCalibrationTest` (noise ≤ 6 %, decay ≥ 85 %, median ≤ 50 per basis; volatility × 2.5 caught ≤ 32 sessions at
    the 95th percentile; keep-running noise ≤ 7 %).
  - **JSON:** `GET /v2/monitoring/decay/rules` →
    ```json
    {"rules": {"MIN_SESSIONS": 20, "RETURN_WINDOW": 60, "RETURN_CONSECUTIVE": 3, "VOL_WINDOW": 20, "VOL_FACTOR": 2.0,
               "VOL_CONSECUTIVE": 3, "RETURN_Z": 3.0, "DD_FACTOR": 2.0},
     "default_basis": "equity_24m",
     "by_basis": {"equity_24m": {"…": "as rules"}, "equity_12m": {"RETURN_Z": 3.5, "DD_FACTOR": 2.5, "…": "…"},
                  "equity_6m": {"RETURN_Z": 3.5, "DD_FACTOR": 3.5, "…": "…"},
                  "crypto_12m": {"RETURN_Z": 3.2, "DD_FACTOR": 2.5, "…": "…"}},
     "basis_labels": {"equity_24m": "equity holdout ≥ 18 months", "equity_12m": "equity holdout 9–18 months",
                      "equity_6m": "equity holdout < 9 months", "crypto_12m": "crypto holdout (UTC days)"},
     "text": ["4 sentences (default basis)"]}
    ```
  - `StrategyMonitor.decay` adds `"basis": "equity_12m"`, `"since_index": 0` and `"protective_exits": 0`.
- **M6 — §23 one-year rule. REMOVED 2026-10-18 with the tax export (§19.15).** `tax_export.over_one_year(acquired, sold)` = sold > acquisition date + 1 calendar year
  (29 Feb → 28 Feb). Example: 2024-02-29 → 2025-02-28 is not over a year, 2025-03-01 is. The `over_one_year` column
  is filled ONLY in `SO_23_KRYPTO`, which only crypto spot uses. Share sales are never §23 (tested).
- **LOW**
  - **Remove an import batch:** `POST /v2/shadow/accounts/{id}/imports/{import_id}/remove {confirm: "REMOVE
    IMPORT", reason: 3..500}` → 200 `{"removed": {"import_id", "transactions", "ts"}, "account": ShadowAccount}`.
    Errors: 400 `{errors: {confirm}}`, 404 `not_found`.
    - It deletes database rows only (`sh_imports` + its `sh_transactions`), never a file.
    - Each removal is recorded insert-only in `sh_import_removals`, with audit `shadow_import_removed` in the same
      transaction. v18 drops the delete triggers of those two tables only.
  - **Identical file twice:** → 409 `{"error": "This exact file was imported already (imp-…).", "code":
    "duplicate_import", "import_id": "imp-…"}`.
    - UNIQUE index `(account_id, sha256)`. The import row, its transactions and the audit row `shadow_import` are
      written in ONE transaction, so a concurrent identical import fails atomically (409).
    - A dry run of an already-imported file adds a `warnings` entry instead.
    - A different file with the same rows still dedupes row by row (`duplicates`).
  - **`watch_links`** counts only alerts actually created (a deduped one is not counted).
  - **Overview:** a strategy whose view fails is kept: `{"strategy_id", "run_id", "account_id", "name", "error"}`.
  - **Monitor status:** `overview.monitor` = `{"running", "last_tick_at", "last_error", "tick_in_progress",
    "tick_started_at", "stale"}`. `stale` is true when a tick has run > 300 s, or when no tick finished for > 300 s
    while the task runs. The task never starts a second tick while one hangs.
- **Files changed:**
  - `monitoring/{decay, fx, tax_export, prices, service, alerts}.py`, `monitoring/shadow/service.py`
  - `api/monitoring_routes.py` (31 routes)
  - `live/runtime.py`
  - `storage/db.py` (v18)
- **Tests:** 56 in `test_sp_monitor_*` (+ the calibration per basis, keep-running noise, protective exit + its
  record, strict FX / lazy FX / after-last-fixing, one-year + leap day, raw / split / US-only prices, duplicate import
  + removal, stale tick, failing strategy kept).
- **Suite:** the full suite is green on the VM (split runs). Scope guards stage0 / mr0 / budget0 are ok.
- **Pre-existing flaky test (not stage 9, not touched):** `test_sp_committee` sometimes ends a run `interrupted` under
  VM load. `committee._worker` ends "interrupted" when its terminal-event write fails (SQLite busy). It passes when
  re-run. Noted for stage 10.

### 19.15 No tax export — fixed notice (user decision 2026-10-18; binding for the WPF)

User (verbatim, 2026-10-18): "Ich glaube es ist nicht gut, wenn wir ein System zum export von steuerdaten haben. Das ist
riskant, weil das muss alles richtig sein. Ich glaube es ist besser, darauf hinzuweisen, dass man sich seine
steuerberichte bei den brokern extra holen muss".

- **Removed:**
  - The routes `GET /v2/monitoring/tax/accounts`, `GET|POST /v2/monitoring/tax/fx` and `POST /v2/monitoring/tax/export`
    (→ 404).
  - `monitoring/tax_export.py` → `tax_export.py.stale`; `tests/test_sp_monitor_tax.py` → `.stale`.
  - The §23 / Anlage KAP / tax-CSV logic, and the FX open question and the FRED-vs-ECB question of the tax export.
- **Notice route:** `GET /v2/monitoring/tax-notice` (no parameters; an unknown parameter → 400) → 200:
  ```json
  {"version": 1, "decided": "2026-10-18",
   "notice": "UFOS.ai does not produce tax data or tax reports. For your tax return, use the official annual tax documents from each broker (e.g. the Jahressteuerbescheinigung of a German broker, or the broker's annual tax statement). Figures shown in UFOS.ai (P&L, realised gains, reports) are for monitoring only and are not a basis for tax filings.",
   "paper_notice": "Paper and demo accounts are simulated and have no tax relevance.",
   "tax_export": false}
  ```
  The source is `monitoring/tax_notice.py` (ONE wording for backend and WPF). The Shadow Account audit report (HTML
  and JSON, key `tax_notice`) carries the same `notice`; its disclaimer no longer says "not tax advice".
- **EUR/USD rates stay**, for the Shadow Account conversions only (behaviour analysis, monitoring only):
  - `monitoring/fx.py` (FRED DEXUSEU, exact dates, no lookup); its tax wording is removed.
  - Routes **moved**: `GET /v2/shadow/fx?year=2000..2100` → `{"series": "DEXUSEU", "year", "observations", "first",
    "last", "fetched_at"}`; `POST /v2/shadow/fx {year}` → the same + `"added"` (503 `fx_unavailable`, 409
    `future_year`; audit `fx_refresh`).
  - Shadow warnings now say "load the FX rates (Shadow Account → FX rates)".
- **Unchanged and NOT tax data:** behaviour diagnostics, shadow portfolios, reports. The statement field `tax`
  (withheld tax as the broker reported it) remains as imported data, because the net cash of a row needs it.
- **DB (storage stays v18, nothing dropped):** the tax export stored no tables or columns of its own. Unused since
  2026-10-18 — stage-10 cleanup candidates:
  - audit rows with action `tax_export` (if any were written);
  - the files `monitoring/tax_export.py.stale` and `tests/test_sp_monitor_tax.py.stale`.
  - `ext_observations` (DEXUSEU) stays in use (Shadow Account).
- **Tests:** `tests/test_sp_monitor_fx.py` (2: exact-date FX refresh and lookup, the fixed notice). The shadow API test
  checks that no `/v2/monitoring/tax/*` route exists, the notice route, `/v2/shadow/fx`, and the notice in the report.

### 19.16 Trade Republic importer (real format, 2026-10-18)

The guessed `Datum;Typ;Wert;…` template is REPLACED (in place) by the real Trade Republic transaction export, written
from a real (sanitized) file — the template is no longer marked "verify against a real export" (Scalable / IBKR Flex
still are). Tests use a SYNTHETIC fixture (`tests/tr_synthetic_fixture.py`: the same 23 columns and quoting, invented
ISINs / amounts / ids / IBANs / persons); no real row is in the repository.

- **Format:** UTF-8, comma, every field quoted, header exactly `datetime,date,account_type,category,type,asset_class,
  name,symbol,shares,price,amount,fee,tax,currency,original_amount,original_currency,fx_rate,description,
  transaction_id,counterparty_name,counterparty_iban,payment_reference,mcc_code` (all 23 required for detection).
  Rows are NOT chronological → sorted by `datetime` (ISO with `Z`, 3 or 6 fractional digits; anything else rejects
  the row); the transaction's day is the `date` column. Dot decimals; signed: a sale's `shares` are negative,
  `amount` is the cash (a purchase negative), `fee` negative, `tax` negative when paid and positive when refunded.
  `account_type` is not used.
- **Rows** (`category/type` → kind):
  - `TRADING/BUY|SELL` for STOCK→share, FUND→fund, BOND→bond, CRYPTO→crypto, DERIVATIVE→derivative: cost basis /
    proceeds = `|amount|` for ALL classes (a bond's price is a fraction of the nominal and its amount includes accrued
    interest — qty × price is NOT used), `fee` separate; a sale's paid tax (negative) is the sale's `tax`. A securities
    `symbol` is its ISIN; a crypto's `symbol` is the ticker and its key (the `XF000…` id in the description is not
    used — staking rows do not carry it). A positive tax on a trade → a separate `tax_refund` (ext id `<id>|tax`).
  - `CASH/DIVIDEND`: `amount` = gross EUR, `tax` = withholding; `original_amount`, `original_currency`, `fx_rate`
    stored as reported (never recomputed).
  - `CASH/INTEREST_PAYMENT` → `interest` (its own line next to dividends; tax as withholding).
  - `CASH/TAX_OPTIMIZATION` (amount 0, tax +x) → `tax_refund` x ("tax refunds as reported in the import"); a
    negative tax there → `withholding`.
  - `CORPORATE_ACTION/WARRANT_EXERCISE` (shares −n, no amount, currency empty) + `CASH/TILG` of the same ISIN (same or
    nearest date, ≤ 31 days) → ONE `sell` (qty n, proceeds = the TILG amount, raw type `WARRANT_EXERCISE+TILG`, ext id
    `<warrant id>|<tilg id>`). A missing partner is NEVER guessed: the row is stored as kind `corporate_action` (net 0,
    with a note) — the position stays open and the insights list the reason; a later export that brings the partner
    books the pair and the reason disappears.
  - `DELIVERY/FREE_RECEIPT` (CRYPTO) → `staking` (user decision 2026-10-18: cost basis = shares × price at receipt;
    no cash; note "staking reward"; it opens a FIFO lot everywhere — Shadow FIFO, diagnostics, insights). A receipt
    without a price is rejected (no fallback).
  - `CASH/PRIVATE_MARKET_BUY` (PRIVATE_FUND) → `pending_buy` ("pending private-market order (pre-payment) — not a
    position"): cash out, no lot, listed per account in the insights (`pending`).
  - `CASH/TRANSFER_INSTANT_INBOUND`, `CASH/CUSTOMER_INPAYMENT` → `deposit`; `CASH/TRANSFER_INSTANT_OUTBOUND` →
    `withdrawal`.
  - Anything else (e.g. card payments) → skipped with the reason "unsupported Trade Republic type <category/type> — not
    imported" + one warning with the counts per type.
- **Privacy (user decision 2026-10-18):** `counterparty_name`, `counterparty_iban`, `payment_reference` and
  `mcc_code` are never read (they do not reach memory beyond the CSV reader, the database, the import report or any
  response); `description` is never stored. Transfer rows keep only date, kind, amount and currency (+ the transaction
  id for dedupe and the raw type) — no name. IBAN-like text (compact or in blocks of four) is replaced by `[removed]`
  in every kept name. Tested: no IBAN / person / reference of the fixture is in the database dump or in any response
  (import, transactions, insights, audit report).
- **Dedupe:** `transaction_id` = `ext_id` (unique per account). An overlapping export imports only the new rows:
  `counts.already_known` = rows whose transaction id is known (new key in the import response; `sh_imports.duplicates`
  stores duplicates + already known). An identical file is still refused with 409 `duplicate_import`. Rows without an
  id use the content hash as before.
- **Storage v19 (+ v20 below):** `sh_transactions` rebuilt (rows copied unchanged; update trigger + index
  recreated): kinds
  + `tax_refund`, `staking`, `pending_buy`, `corporate_action`; columns + `ext_id`, `original_amount`,
  `original_currency`, `fx_rate`, `note`; partial UNIQUE index `(account_id, ext_id)`.
- **JSON changes (WPF):** a normalised transaction (import preview, `GET …/transactions`) has the new fields `ext_id`,
  `original_amount`, `original_currency`, `fx_rate`, `note` and may have the four new kinds; the import `counts` gain
  `already_known`; the template list's `trade_republic` has `verify: false`, the new label and a `privacy` text.
- **Unrealised P&L** of TR positions follows the existing price-source rules (§20.1): crypto, bonds and derivatives have
  no market data; a valuation (user) prices them.
- **Audit report (9c addendum):** an account without data shows "No trades imported yet — nothing to analyse." under
  "Behaviour diagnostics" and every diagnostic with "Not available: no trades imported" (never an empty heading;
  JSON: `diagnostics.message`); the "Your portfolio" shadow row shows "—" with "not available: no statements imported"
  (`user.end_value` null, `user.message` "no statements imported") instead of a stand-in 0.00.
- **Review fixes 2026-10-18 (independent stage-9c review):**
  - HIGH — same-day order: FIFO, diagnostics and the shadow portfolios order by `normalize.sort_key` = (date, `ts`,
    kind order, `seq`, id). `ts` = the statement's own timestamp, sortable `YYYY-MM-DDTHH:MM:SS.fffffffff` (TR
    `datetime`; Scalable `time`; IBKR the time part of `TradeDate` / `TradeTime` / `Date/Time`; generic the mapped
    `time` column; null when the export has none — an unparsable time rejects the row); `seq` = the position in the
    parsed file (TR: datetime order). `row_no` stays the original file line, only for messages / the preview `row`.
    Storage **v20**: `sh_transactions.ts`, `sh_transactions.seq` (older rows null — unchanged order). Regression test:
    two buys on one day, the later one first in the file, a sale the next day → +50.00 (was −50.00).
  - MEDIUM — a portfolio without buys / sales / staking (e.g. deposits only) has no value: `user.end_value` null,
    `user.message` "no trades imported"; the report row shows "—" with "not available: no trades imported".
  - MEDIUM — a `transaction_id` twice in ONE file: identical content → skipped "repeated row (same transaction_id and
    content) — imported once"; different content → row error (`transaction_id`, "duplicate transaction_id with
    different content"); the first occurrence is kept.
  - LOW — ids are never truncated: a TR `transaction_id` over 120 characters is a row error; any `ext_id` over 255
    characters (composed pair ids included) is a row error.
  - LOW — content check across identities: a row with an unknown id whose content (date, kind, key, qty, price, net,
    currency) matches an earlier row without an id or with an id this file does not carry — and a row without an id
    that matches an earlier row with an id — is skipped as "possible duplicate (content matches an earlier import)"
    (listed in the import report's skipped rows + a warning), never merged.
  - LOW — IBAN scrub widened: country code + check digits + 10–30 alphanumerics, compact (any case) or with single
    spaces / dashes anywhere (upper case); a match that starts with a whole ISIN token keeps the ISIN.
  - LOW — a pending private-market order's fee is part of `fees` (trading fees) in its year; the pending entry's note
    says so.
  - **JSON changes (WPF):** a transaction (import preview, `GET …/transactions`) has `ts` (string or null); import
    `counts` gain `possible_duplicates` (also included in `counts.skipped`, with one entry per row in the import
    report's skipped list); `shadows.user.message` may be "no trades imported"; insights `pending[].note` may end
    with "; its fee is included in the fees". No field was removed or renamed.

## 20. Stage 9b — portfolio insights (contract, 2026-10-18)

User decisions (interview, 2026-10-18): "Portfolio insights" are integrated now, before stage 10, as "at least a rough
guide, also for taxes".
- **Location:** a compact card in the MainWindow (the existing "Portfolio Insight" overview card) AND a detail view in
  HedgeFund → Monitoring.
- **Data:** ONLY the Shadow Account imports (the user's real depots). Never platform, paper or broker accounts.
- **Figures:**
  - realised P&L (shares vs other), dividends / withholding and fees;
  - unrealised P&L and allocation;
  - the same for the previous year.
- **Label everywhere** (fixed): "Rough guide only — not tax data. The broker's annual tax statement (e.g.
  Jahressteuerbescheinigung) is authoritative."
- **Amounts in the account/instrument currency, NO FX conversion; sums per currency.**

The §19.15 decision still holds: no tax export, no tax categories, no tax maths — these are monitoring figures only.

Scope exception (user, 2026-10-18): `HedgeFund/tools/scope-allow-insight0.json` lists `UFOS.ai/MainWindow.xaml`,
`UFOS.ai/MainWindow.xaml.cs` and `UFOS.ai/Services/IPortfolioInsightProvider.cs`. The last file is the card's existing
provider seam (its comment: swapping the placeholder provider is the only change MainWindow needs). The snapshot
`insight0` was taken before any edit. `UFOS.ai/Shared/BackendAuth.cs` is reused unchanged; no new shared helper.

### 20.1 Calculation (`strategy_platform/monitoring/insights.py`, deterministic, no network except (b))

- **Basis:** every Shadow account (`sh_accounts`) with its normalised transactions (§19.6, the user's classifications
  applied). Calendar years in UTC: the current year `Y` = the year of today (UTC) unless `year=` is given, and the
  comparison year `Y−1`.
- **Realised P&L:** FIFO (`shadow/normalize.Book`) over the account's WHOLE history in the instrument currency.
  - Cost = gross + fee; proceeds = gross − fee.
  - A sale realises the parts of the oldest lots; partial sales split a lot pro rata.
  - A part belongs to the year of its SALE date; per account, per currency and year, three buckets:
    - **`shares`** — the import's instrument type is `share`;
    - **`other`** — `etf | fund | bond | crypto | derivative | other`;
    - **`unclassified`** — the type is unknown. It is never guessed; the user can classify it (§19.6
      classifications).
  - Each bucket is `{gains, losses, net, parts}` (gains ≥ 0, losses ≤ 0).
  - **Not used, listed with the reason:**
    - sales beyond the imported purchases (`unmatched` — "no earlier purchase in the imported data");
    - rows the importer rejected (count per import, from the import report).
  - Corporate actions (splits, spin-offs, mergers) are not in the imported transactions. A position whose market data
    shows a split after its first purchase is not valued (see unrealised). Stated in `limits`.
- **Income and costs** per account / currency / year:
  - `dividends` (gross);
  - `withholding` (tax on dividend rows + `withholding` rows, as given in the import);
  - `interest`;
  - `trading_fees` (fees on buy/sell rows);
  - `other_fees` (`fee` rows).
  All as given in the import — nothing recomputed.
- **Unrealised P&L and allocation** (open FIFO lots of today; cost in the instrument currency). Price source per
  position, in this order, never estimated:
  1. **`valuation`** — a position valuation: the latest valuation row of the account that contains the position. A
     valuation contained in an import comes first (IBKR Flex "Open Positions" section: `Symbol`, `Quantity`,
     `MarkPrice`, `CurrencyPrimary`, `ReportDate` → stored with the import id, ignored once that import is removed).
     Next comes one the user entered (`PUT …/valuation`, §19.11). The position shows its `as_of` and
     `source: "import" | "user"`.
  2. **`market`** — the data layer's latest RAW close, only for USD-listed US securities (as in the §19.14 H2 fix):
     symbol present, currency USD, ISIN absent or starting with `US`, not crypto / derivative / bond. A split after the
     first purchase → not valued.
  3. otherwise **`none`** — "not available (no price source)" per position, with the reason.
  - Per position: `value = qty × price`, `unrealised = value − cost`.
  - `day_change` = `qty × (last − previous close)` only for `market` prices; null otherwise.
- **Allocation and coverage** per account and per currency (never summed across currencies):
  - `weight = value / Σ value` over the VALUED positions only;
  - `coverage_pct = Σ cost of the valued positions / Σ cost of all open positions × 100`;
  - concentration over the valued positions: `{hhi, effective_n, top1_pct, top3_pct}`.
- **Year-over-year:** per account and currency, `delta = current − previous` for realised net, dividends, withholding,
  interest and fees.
- **State:**
  - `no_imports` — no Shadow account has transactions;
  - `incomplete` — any position without a price, an unmatched sale, an unclassified realised part or rejected import
    rows; `reasons[]` lists each;
  - `ok` otherwise.
- **Privacy:** everything is computed locally from hedgefund.db; nothing is sent to an AI or anywhere else. Market data
  for (b) is fetched through the existing data layer, the same as the Shadow Account diagnostics.

### 20.2 API (behind api_auth; strict query; GETs do NOT touch the trading heartbeat — a MainWindow card must not keep the dead-man switch alive)

- `GET /v2/insights/summary` (no parameters) → the card payload (§20.4).
- `GET /v2/insights/portfolio?year=2000..2100&account=sha-…` → the detail payload (§20.4). `account` optional:
  without it, all accounts are included. Unknown account → 404; bad year → 400.

### 20.3 MainWindow card — how it reaches the HedgeFund backend (existing mechanism, no new helper)

- **Shared/BackendAuth.cs** is linked into the main app.
  - `BackendAuth.FindBackendDir("HedgeFund", "backend")` walks up from the exe and finds the folder with main.py.
  - `BackendAuth.ReadToken(dir)` reads `<dir>/data/api-token` on EVERY call (the backend writes a new token at each
    start; never logged).
  - The token goes in header `X-UFOS-Token`. The base URL is fixed: `http://127.0.0.1:8867`.
- This is exactly how the AI-budget window reaches the backends (`Shared/AiBudgetRegistry.cs` + `AiBudgetProviders.cs`
  entry `hedgefund`, port 8867): first `GET /health` without a token (2 s timeout), then the request with the token
  (10 s timeout).
- The card implements `IPortfolioInsightProvider` in `Services/IPortfolioInsightProvider.cs` with that sequence and
  `GET /v2/insights/summary`.
- **Backend not running** (health fails, no token file, connection refused): the card shows "Hedge Fund backend not
  running — click to start" (§20.8; was "…open Hedge Fund to load your insights") and NO numbers.
  - There is no existing snapshot mechanism for module figures, so nothing is cached and no stand-in is shown.
  - Proposal (NOT built — the user decides): the backend could write a small local snapshot
    `HedgeFund/backend/data/insights-summary.json` after each computation, and the card could show it labelled "as of
    <time>, backend not running".
- The card shows the payload's `headline` and `detail`, then the label, plainly (it is not AI text — no AI badge). On
  click it opens HedgeFund → Monitoring → Insights.

### 20.4 Exact JSON (binding for the WPF card and the Monitoring → Insights view)

**GET /v2/insights/summary** →
```json
{"state": "ok | incomplete | no_imports", "reasons": ["2 positions without a price source"],
 "generated_at": "2026-10-18T09:00:00+00:00", "year": 2026, "previous_year": 2025, "accounts": 2,
 "label": "Rough guide only — not tax data. The broker's annual tax statement (e.g. Jahressteuerbescheinigung) is authoritative.",
 "tax_notice": "UFOS.ai does not produce tax data or tax reports. …",
 "currencies": [{"currency": "EUR", "realised_net": 1234.56, "realised_shares": 1000.0, "realised_other": 234.56,
                 "realised_unclassified": 0.0, "dividends": 80.0, "withholding": 12.0, "fees": 9.9,
                 "unrealised": 3400.0, "market_value": 25000.0, "coverage_pct": 82.0,
                 "previous": {"realised_net": 900.0, "dividends": 60.0, "fees": 12.0}}],
 "headline": "Realised 2026: +1,234.56 EUR · −20.00 USD",
 "detail": "Unrealised +3,400.00 EUR (coverage 82 %) · 2 depots",
 "as_of": {"valuation": "2026-10-16", "market": "2026-10-17"}, "unpriced": 2}
```
- `unpriced` = number of positions without a price source — the same rule as the reason "N position(s) without a price
  source" (no price, has lots; a position valued only in another currency is NOT counted). Clients use this field and never
  derive it from `positions − valued` (live test 2026-10-19).
- `no_imports`: `currencies: []`, `headline: "No depot imported yet"`, `detail: "Import a broker statement in Hedge Fund
  → Shadow Account"`.
- `as_of.valuation` / `as_of.market` = the newest valuation as-of date / the newest market close used (null when
  none).

**GET /v2/insights/portfolio?year=&account=** →
```json
{"state": "…", "reasons": [], "generated_at": "…", "year": 2026, "previous_year": 2025, "label": "…", "tax_notice": "…",
 "privacy": "Computed on this computer from your imported statements — nothing is sent anywhere.",
 "limits": ["No FX conversion — sums are per currency.", "…"],
 "accounts": [{"account_id": "sha-…", "label": "My TR depot", "templates": ["trade_republic"], "state": "…",
   "reasons": [],
   "years": {"2026": YearBlock, "2025": YearBlock},
   "positions": [Position],
   "allocation": [{"currency": "EUR", "market_value": 25000.0, "cost_valued": 21000.0, "cost_total": 25600.0,
                   "coverage_pct": 82.03, "unrealised": 4000.0, "day_change": null,
                   "concentration": {"hhi": 0.31, "effective_n": 3.2, "top1_pct": 41.0, "top3_pct": 88.0}}],
   "not_used": [{"date": "2026-03-01", "key": "US…", "qty": 2.0, "reason": "no earlier purchase in the imported data"}],
   "rejected_rows": [{"import_id": "imp-…", "filename": "tr.csv", "rejected": 2}]}],
 "totals": {"years": {"2026": YearBlock, "2025": YearBlock}, "allocation": [ "per currency as above" ]},
 "yoy": [{"currency": "EUR", "realised_net": {"current": 1234.56, "previous": 900.0, "delta": 334.56},
          "dividends": {"…": "…"}, "withholding": {"…": "…"}, "interest": {"…": "…"}, "fees": {"…": "…"}}]}
```
- **YearBlock** = `{"currencies": [{"currency": "EUR", "realised": {"shares": {"gains", "losses", "net", "parts"},
  "other": {…}, "unclassified": {…}, "net": …}, "dividends": …, "withholding": …, "interest": …, "trading_fees": …,
  "other_fees": …, "fees": …}]}`.
- **Position** = `{"key", "symbol", "isin", "name", "asset_class", "currency", "qty", "cost", "avg_cost", "price",
  "price_source": "valuation|market|none", "valuation_source": "import|user|null", "as_of", "value", "unrealised",
  "unrealised_pct", "day_change", "weight", "note"}`. `price`, `value`, `unrealised`, `weight` are null for `none`;
  `note` is then "not available (no price source): <reason>".

### 20.5 Stage 9b backend notes (implemented 2026-10-18: §20.1–§20.4 — for the MainWindow card and Monitoring → Insights)

All §20.4 payloads are implemented as written, with these exact details / additions (the WPF binds to them):
- **Routes:**
  - `GET /v2/insights/summary` (any query parameter → 400).
  - `GET /v2/insights/portfolio?year=&account=`: `year` 2000..2100 (default: the current UTC year), `account` =
    `sha-<12 hex>` (unknown → 404 `{error, code: "not_found"}`; a malformed one → 404 too).
  - Neither touches the trading heartbeat.
  - Both sit in `api/monitoring_routes.py` (now 34 routes); the module is `monitoring/insights.py`.
- **Numbers:** money is rounded to 2 decimals, `avg_cost` to 6, weights to 6 (fractions), percentages to 2.
  - Realised buckets carry `{gains (≥ 0), losses (≤ 0), net, parts}`; `realised.net` = the sum of the three.
  - `fees` = `trading_fees` (fees on buy/sell rows, in the year of the trade) + `other_fees` (`fee` rows).
  - `withholding` = the tax of dividend and interest rows + `withholding` rows.
- **FIFO lots** are kept per (instrument key, currency): an instrument bought in two currencies never mixes lots.
  `not_used` lists the unmatched sales of the current and the previous year.
- **Allocation entries** add `"positions"` and `"valued"` (counts). `market_value`, `unrealised` and `concentration` are
  null when nothing in that currency is valued; `day_change` is null without a market-priced position.
  `totals.allocation` recomputes the weights over all accounts per currency.
- **Price paths:**
  - **Import valuation:** the IBKR Flex "Open Positions" section is detected by `MarkPrice` + `Quantity` + `Symbol`
    without `TradeDate`. Only `LevelOfDetail` SUMMARY (or none) is used; `AssetClass CASH` is skipped. It is stored as
    `sh_valuations` rows with actor `import` and prices `[{key, price, qty, currency, import_id}]`, one row per
    `ReportDate`. A removed import's valuation rows are ignored.
  - **User valuation:** its price is taken in the position's currency.
  - **Market:** the latest RAW close ≤ today (exact session date as `as_of`); `day_change` = qty × (last − previous
    session close). No split adjustment of lots — a split after the first purchase → "not available".
  - **Not available:** `note` = "not available (no price source): <reason>". Reasons: "no symbol in the import",
    "traded in EUR — market data only for USD-listed US securities", "ISIN … is not a US security", "crypto — no
    market data source", "stock split of X on D — …", "no market data for X (…)", "no corporate actions for X".
- **States / reasons:**
  - Account reasons: "n position(s) without a price source", "n sale(s) without an earlier imported purchase (not
    used)", "n realised part(s) of unclassified instruments (classify them in Shadow Account)", "n imported row(s)
    were rejected at import".
  - Top-level reasons are prefixed with the account label.
  - `no_imports` → reasons `["No depot imported yet — import a broker statement in Hedge Fund → Shadow Account."]`.
  - An account without transactions is `no_imports` inside an otherwise ok payload.
- **Summary strings** (plain text, not AI):
  - `headline` = "Realised 2026: " + per currency "+1,234.56 EUR" / "−20.00 USD" joined by " · " (currencies with net
    0 omitted; none → "nothing realised yet").
  - `detail` = per valued currency "Unrealised +3,400.00 EUR (coverage 82 %)", then "n depot(s)"; no valued position
    → "Unrealised: no price source".
  - `summary.currencies[]` covers every currency with figures in the year or open positions (0.0 where nothing
    happened); `unrealised`, `market_value` and `coverage_pct` are null without positions.
- **Changed Shadow payloads (additive):**
  - `ImportResult.counts` adds `positions`.
  - `GET /v2/shadow/accounts/{id}` → `valuation` adds `source: "import" | "user"`.
  - The Shadow diagnostics / shadows use the newest valuation row (import or user) as before.
- **MainWindow mechanism (§20.3) confirmed from the code:** `Shared/BackendAuth.cs` (`FindBackendDir`, `ReadToken`,
  `CreateClient`, header `X-UFOS-Token`). `Shared/AiBudgetRegistry.cs` does health first (2 s), then the request
  (10 s), and is linked into the main app. The seam is `Services/IPortfolioInsightProvider.cs` (`PortfolioInsight(Headline,
  Detail, GeneratedAt, AiModel = null)` — `AiModel` stays null: no AI). No snapshot mechanism exists (proposal in
  §20.3, not built).
- **Tests:** `tests/test_sp_insights.py` (6).
  - FIFO across the 2025/2026 boundary and partial sales; the shares / other / unclassified split.
  - Dividends with withholding, `withholding` rows, fees; multi-currency without conversion; the previous year
    (`year=2025`).
  - All four price paths (import valuation wins over market, user valuation, market raw close + day change, not
    available).
  - Coverage + concentration; an import valuation dropped with its import; the summary strings; the routes; no imports.
  - `test_sp_monitor_shadow.py` updated (`counts.positions`).

### 20.6 Stage 9b WPF notes (implemented 2026-10-18: Monitoring → Portfolio insights + the MainWindow card — bound to §20.4/§20.5)

Static checks only (no dotnet on the VM — the user builds). Figures only from the payload; no stand-in numbers; the payload's
`label` / `tax_notice` / `privacy` shown verbatim (no second copy in the WPF).
- **Files:** new `HedgeFund/Platform/PlatformClient.Insights.cs` (both routes; DTOs exactly §20.4 + §20.5 `allocation.positions /
  valued`; no computed properties), `HedgeFund/Platform/MonitoringView.Insights.cs`; edited `HedgeFund/Platform/MonitoringView.cs`
  (tab "Portfolio insights" Ctrl+5, Tax reports moves to Ctrl+6, `ShowInsightsAsync`), `HedgeFund/HedgeFundWindow.Monitoring.cs`
  (`public ShowPortfolioInsights()` — the hook for the card), and within scope `insight0`: `UFOS.ai/Services/IPortfolioInsightProvider.cs`,
  `UFOS.ai/MainWindow.xaml`, `UFOS.ai/MainWindow.xaml.cs`.
- **Seam change (§20.5 described the old one):** `IPortfolioInsightProvider.GetCardAsync()` → `PortfolioInsightCard(State, Lines,
  MoreLines, Detail, Label, AsOf, Message, Reasons)`; the placeholder provider and the `PortfolioInsight` record are gone (MainWindow
  was the only user). `HedgeFundPortfolioInsightProvider` = the §20.3 sequence exactly: `BackendAuth.FindBackendDir("HedgeFund",
  "backend")` → `GET http://127.0.0.1:8867/health` without a token (2 s) → `BackendAuth.ReadToken` (every call) → `GET
  /v2/insights/summary` with `X-UFOS-Token` (10 s; a 401 re-reads the token once); no proxy / redirects / cookies; body ≤ 256 KB;
  nothing cached.
- **Card** (`PortfolioInsightCardButton`, same `OverviewCardButtonTemplate` + brushes as the other overview cards). Simplified
  after live feedback 2026-10-19 ("zu unübersichtlich"): rendered ONLY from structured fields of `PortfolioInsightCard` that the
  provider fills from the JSON (no parsing of display text). Face: label "PORTFOLIO INSIGHT · <year>"; one row per currency
  "<signed amount>  Realised" (`currencies[].realised_net`, net 0 omitted; red/green by sign); one row per currency with positions
  "<signed amount>  Unrealised · <coverage> % priced", or "Unrealised — not available" when nothing is valued; max 2 rows each +
  "+N more currencies — open insights"; amber "⚠ N position(s) without a price" from `unpriced` only when `incomplete`; one muted
  line "Not tax data — see broker's annual statement". ToolTip: depot count, as-of (valuation / market close), computed time, all
  reasons, the full `label`. Amounts per currency, never summed across currencies. States: backend not running → "Hedge Fund
  backend not running — click to start" (§20.8); `no_imports` → "No broker statements imported yet — Hedge Fund → Shadow Account";
  errors → "not available (reason)" (max 2 short lines). Refresh: the existing
  5-min overview timer + window activation (≥ 60 s apart), skipped while the MainWindow is hidden or minimized; stops with the window
  (timer stopped, `Activated` unsubscribed in `Window_Closing`). Click / Enter / Space → an open Hedge Fund window switches to Monitoring →
  Portfolio insights; otherwise it is opened through the Hedge Fund tile's `OpenModuleWindow` path and switches once loaded.
  AutomationProperties.Name carries the whole card text. Not AI output (no AI badge).
- **Monitoring → Portfolio insights** (loaded on opening the tab, F5 and filter changes; no polling): header with DATA STAYS ON THIS PC,
  the `label` (amber callout), `tax_notice`, `privacy`; filters year (Current / Previous = `year−1`) and depot (All / each Shadow
  account → `account=`), "↻ Recompute"; state chip + reasons; as-of line (computed, newest valuation / market as-of from the
  positions unless the payload sends `as_of`); per currency tables: realised P&L (shares / other / unclassified: gains, losses, net,
  lot parts; net), dividends / withholding / interest / trading fees / other fees / fees — for the year and the previous year;
  year-over-year (current, previous, change); unrealised P&L + allocation with the **coverage % in large type** per currency, market
  value, day change (market prices only), cost valued / total, positions valued n of m, concentration; then per depot the same plus
  valued positions (price source + as of, value, unrealised, weight), "Not valued" (each `note` verbatim), "Transactions not used"
  (`not_used` reasons, `rejected_rows`); limits. `no_imports` → "No broker statement imported yet — …". Errors → "Portfolio insights not
  available (reason)".
- **Compile-safety scan** of every touched file: string-literal quote adjacency, brace / parenthesis balance, shadowing (the card
  button was renamed `PortfolioInsightCardButton` so it does not hide the record type `PortfolioInsightCard` inside MainWindow), partial
  members unique, usings checked.
- **Click-test (user):** (1) start UFOS.ai without Hedge Fund → card "Hedge Fund backend not running — …", no numbers; (2) open Hedge
  Fund, wait ≤ 5 min or re-activate the main window → card figures; with no imports → "No broker statements imported yet — …";
  (3) import a statement (Shadow Account) → card lines per currency, "+n more" with > 3 currencies, detail with coverage, as-of, the
  label small; (4) click the card (and Tab + Enter) → Hedge Fund → Monitoring → Portfolio insights; (5) Portfolio insights: label +
  tax notice + DATA STAYS ON THIS PC, tables per currency, coverage % large, "Not valued" with reasons, "Transactions not used";
  (6) Previous year → figures of year−1; depot filter → one depot; (7) Set valuation prices for an unpriced position → Recompute →
  coverage rises; (8) stop the backend → card "not running" at the next refresh, view "not available (…)"; (9) minimize the main window
  → no refresh while minimized; (10) §20.8: backend not running → card "Hedge Fund backend not running — click to start" → click → "Starting
  Hedge Fund backend (also runs the paper engine)…" + progress (first start / package install shows "installing Python packages …"),
  no window opens → figures appear; click again → Hedge Fund opens at Portfolio insights and attaches to the running backend (no
  restart, Newsroom dot already online); (11) Hedge Fund window → "Start the Hedge Fund backend together with UFOS.ai" ON → restart
  UFOS.ai → the UI is usable at once, the card shows the start progress, then figures; OFF (default) → the card says "click to start";
  (12) after a card start: Portfolio & Trading shows auto trading OFF, the Overview shows the autopilot OFF (forced at start);
  (13) break the start (e.g. rename the backend folder) → card "not running — click to start" + "Hedge Fund backend: offline — …".
- **Stage 9c alignment (§19.16 / §20.9, 2026-10-18):** realised buckets `brokerage` / `other` / `unclassified` with the labels of
  `portfolio.buckets` (default "Brokerage (stocks + ETFs)"); summary `realised_brokerage`, `interest`, `tax_refunds`; income table
  per currency: dividends, interest, withholding, trading / other fees, fees, taxes on sales, tax refunds (as reported); per depot
  "Pending private-market orders — not positions" (`pending`) and "Corporate actions without their partner row"
  (`unpaired_corporate_actions`, notes verbatim). Staking-reward notes come verbatim in the position `note`. The card reads only
  `realised_net` / `detail` — unaffected. Click-tests: (14) a TR depot with ETFs → their sales in "Brokerage (stocks + ETFs)";
  (15) a TR private-market pre-payment → listed under pending, not as a position; a warrant exercise without its TILG row →
  listed under corporate actions without partner.

### 20.7 Review fixes 2026-10-18 (independent stage-9b review: HIGH, MEDIUM, LOW — backend; JSON changes for the WPF)

These override §20.1, §20.4 and §20.5 where they differ.
- **HIGH — the broker quantity is checked.**
  - An import valuation (IBKR Flex "Open Positions") is used only when the broker's `Quantity` equals the FIFO lots
    of that (instrument, currency), within max(1e-6, 1e-4 × qty).
  - On a mismatch the position is NOT valued: `price_source: "none"`, `broker_qty: 30.0`, `note: "not available (no
    price source): broker reports 30, imported lots give 1 on 2026-09-30 — missing history or a split?"`. It does not
    fall back to another price source.
  - **Broker positions without imported lots** (in the newest import snapshot, but with no open lot) are listed,
    never dropped: Position with `qty: 0.0`, `cost: null`, `avg_cost: null`, `broker_qty: 5.0`, `as_of`,
    `price_source: "none"`, `note: "not available (no price source): broker reports 5 on 2026-09-30, no imported
    lots — import the older statements"`.
  - Both make the account `incomplete`, with reasons "n position(s) where the broker's quantity differs from the
    imported lots: KEY (broker 30, lots 1)" and "n broker position(s) without imported lots: KEY (5)".
  - Allocation counts them in `positions` but not in `valued`; `cost_total` ignores a null cost.
  - **New Position key:** `broker_qty` (number | null).
- **MEDIUM — cheap polls, no cache.**
  - Market prices load only a recent window of 20 calendar days (the latest and the previous close). Splits load
    separately from the first purchase (`prices.Closes(start, end, actions_start=…)`).
  - Test: 50 USD positions → one price load per symbol, a 20-day window, the summary < 3 s with a fake provider.
  - No result cache (it would show stale numbers).
- **LOW**
  - **Flex positions parser:**
    - `Quantity` / `MarkPrice` are bounded by `normalize._fin` (finite, |x| ≤ 1e12).
    - A negative quantity is rejected: "short positions are not supported (negative Quantity)". Zero quantity or
      price is rejected.
    - The ISIN is validated (`ISIN_RE`); the quantity is never `abs()`ed.
    - Text cells (e.g. a formula-like symbol) stay inert data.
  - **Taxes on sales:** the tax on sell rows, as reported in the import, is shown as its own figure (the same
    rough-guide label applies), never dropped:
    - YearBlock currency entries add `"taxes_on_sales": 26.38`;
    - `summary.currencies[]` adds `"taxes_on_sales"`.
  - **User valuations by (instrument, currency):** `PUT /v2/shadow/accounts/{id}/valuation` prices accept an
    optional `currency` (`{key, price, currency?}`, a currency of `normalize.CURRENCIES`).
    - A price with a currency values only that (instrument, currency) position.
    - A price without a currency values the instrument only when it is held in exactly ONE currency. Otherwise:
      "not available (no price source): your valuation of KEY has no currency, but it is held in EUR and USD — enter
      the price with its currency".
    - `GET /v2/shadow/accounts/{id}` → `valuation.prices[]` may carry `currency`.
  - **Summary `detail`:** a currency with open positions but nothing valued appears as "EUR: nothing valued (n
    position(s) without a price source)".
  - **Market data only for identified US securities:** an ISIN starting with `US` is now REQUIRED (an ISIN-less
    symbol in USD, e.g. an unclassified crypto ticker, gets no market price: "no ISIN in the import — market data
    only for identified US securities (US ISIN)").
    - The same rule now applies to the Shadow Account diagnostics (`Context.market_symbol`).
  - **summary.currencies[]** adds `"positions"` and `"valued"` (counts).
- **Tests:** `tests/test_sp_insights.py` now 24 (+ a named case per checklist item: FIFO across the year boundary,
  partial sale, buckets, dividends + withholding, multi-currency, previous year; qty mismatch + positions without lots,
  Flex parser limits / malicious input, user valuation per currency, USD crypto symbol without market price, taxes on
  sales, coverage maths, 50 positions cheap).

### 20.8 Backend started from the shell (stage 9b, user decision 2026-10-18 — WPF; no backend change)

- **What:** the MainWindow "Portfolio Insight" card STARTS the Hedge Fund backend when it is not running (no confirmation, the
  window does not open; card: "Starting Hedge Fund backend (also runs the paper engine)…" + the start progress verbatim —
  checking… / Python environment … / installing Python packages … — can take a few minutes / starting…; then the figures once it
  answers; a failed start → "Hedge Fund backend not running — click to start" + "Hedge Fund backend: offline — <reason>"). A running
  backend → the click opens Hedge Fund → Monitoring → Portfolio insights (§20.6). Down → "Hedge Fund backend not running — click to
  start"; no cached or old numbers. New setting **"Start the Hedge Fund backend together with UFOS.ai"** (OFF by default;
  `%LOCALAPPDATA%\UFOS.ai\hedgefund-backend.json` key `start_with_app`, `Fund/FundSettings.StartWithApp`, toggle in the Hedge Fund
  window next to "Keep the engine running after UFOS.ai closes"): ON → the MainWindow starts the backend at app start in the
  background (`Dispatcher.BeginInvoke(ApplicationIdle)`, never awaited — the UI never waits).
- **How (no new start path):** `HedgeFund/HedgeFundBackendHost.cs` = a PUBLIC facade over the internal `Newsroom/NewsroomBackend`
  (`Status`, `IsOnline`, `IsStarting`, `StatusChanged` — raised on any thread, marshalled by the MainWindow with
  `Dispatcher.BeginInvoke` —, `EnsureStartedAsync`, `StartWithApp`). It is the SAME `EnsureStartedAsync` the Hedge Fund window
  calls in `Window_Loaded`: one static state per app process (the main app hosts HedgeFundApp.dll), so a Hedge Fund window opened
  later finds the in-flight start task or the live process and attaches without a restart; a backend from an earlier session
  (detached) is reused through the unchanged `/version` check (code mtime ≥ newest .py on disk). Lifetime unchanged: "app" (Job
  Object, ends with UFOS.ai) or "detached" (per hedgefund-backend.json).
- **Safety check (from the code, 2026-10-18) — starting from the card is equivalent to opening the Hedge Fund window today:**
  - The backend process and its start are identical (same script `run_hedgefund_backend.ps1 -SkipInstall`, same environment wait,
    same Job Object / detached handling).
  - **Strategy Platform:** `control.force_off_at_start()` switches auto trading OFF at every backend start (audit "backend start");
    the runtime (60 s) records decisions but sends no orders while auto trading is off; the broker poller (15 s) reconciles and
    completes a pending kill (protective only); the monitor task stores alerts / checks / reports (no orders, no AI).
  - **Old paper engine (fund/):** `engine.start()` forces the autopilot OFF (audit "autopilot_forced_off" when it was on). Before
    the migration it then runs its schedule as before: scheduled full analysis cycles during the US session (agents + AI judge /
    sentiment within the fund's AI daily budget — these cost money if that budget is > 0) and the 5-min risk watch (drawdown stop /
    kill and margin actions on EXISTING positions — protective); no autopilot orders while the autopilot is off. After the migration
    the old engine only analyses (advisory); its trading (autopilot, manual, execute) is refused.
  - **Dead-man switch / kill switch:** unchanged. With no window nothing touches the heartbeat (the card's `/v2/insights/summary`
    does not, §20.2; the notifiers start only with the Hedge Fund window), but both auto-trading flags are already off at start and
    nothing can switch them on without the Hedge Fund window — so nothing trades automatically on a start from the card. The kill
    switch stays where it is (Hedge Fund toolbar).
  - **Finding (no change made):** with "start with UFOS.ai" ON, the old engine's scheduled AI analysis cycles run whenever UFOS.ai
    runs, also when the Hedge Fund window is never opened (AI cost within the fund AI daily budget; the same already happens today
    after the window was opened once, because the backend lives as long as the app).
- **Scope:** inside HedgeFund/**: `HedgeFundBackendHost.cs` (new), `Fund/FundSettings.cs`, `HedgeFundWindow.xaml(.cs)`; outside
  (insight0): `MainWindow.xaml.cs`, `Services/IPortfolioInsightProvider.cs` (the not-running text). Nothing else (no App.xaml.cs).
- **Review fixes 2026-10-18:** the const `HedgeFundPortfolioInsightProvider.NotRunningText` is now really "Hedge Fund backend not
  running — click to start" (the first change had been committed from a stale copy); every status other than checking / starting /
  online ends a card start and is shown verbatim (offline / "not started" → "not running — click to start" + "Hedge Fund backend:
  <status>"; e.g. "outdated backend running — …" as the headline); after the click `EnsureStartedAsync` is awaited and the end state
  applied, so a no-op start (process alive but not answering) never leaves "Starting…"; a click before the first refresh (or in the
  down state) asks `/health` first (`HedgeFundBackendHost.IsRunningAsync`) — running → opens Hedge Fund, else starts it; the failure
  reason stays on the card until a start succeeds or the backend answers again; the accessible name says "Press to start the backend"
  in the down state; `FundSettings.StartWithApp` keeps OFF on an unreadable / corrupt hedgefund-backend.json but logs the reason once
  (AppLog, module HedgeFund).

### 20.9 Bucket change (user decision 2026-10-18 — backend; JSON changes for the WPF)

- Realised P&L buckets are now **`brokerage`** = stocks AND ETFs / funds (asset classes share, etf, fund — for every
  importer: Trade Republic STOCK/FUND, Scalable, IBKR, generic), **`other`** = bonds, crypto, derivatives, private funds
  and other, **`unclassified`**. Labels: "Brokerage (stocks + ETFs)", "Other (bonds, crypto, derivatives, other)",
  "Unclassified" (`portfolio.buckets: [{key, label}]`).
- **JSON changes (WPF — `PlatformClient.Insights.cs` still reads the old names and must be updated):** YearBlock
  `realised.shares` → `realised.brokerage`; summary `realised_shares` → `realised_brokerage`. New: YearBlock
  `tax_refunds` (as reported in the import); summary rows `interest` and `tax_refunds`; per account `pending`
  (`[{date, key, name, currency, amount, fee, note}]` — pending private-market orders, not positions) and
  `unpaired_corporate_actions` (`[{date, key, qty, note, ext_id}]`, also named in `reasons`); a position that holds a
  staking reward has a `note` containing "includes staking rewards (cost basis = market value at receipt)".
- LIMITS add: tax refunds as given; staking rewards at their receipt value; pending private-market orders are not
  positions.

### 20.10 Symbol mapping (user decision 2026-10-18, option (a) — backend; JSON for the WPF)

- **Why:** Trade Republic exports ISINs only, so no position had a ticker ("no symbol in the import") and none could
  be priced from market data. The user maps ISIN → US exchange ticker ONCE per instrument. Option (c), a price
  source with ISIN lookup, comes later.
- **Scope: global.** An ISIN identifies the instrument in every Shadow account, so one mapping applies to all of
  them. Storage **v21** `sh_symbol_maps` is insert-only: the newest row per ISIN wins, and a removal is a row with
  `symbol` null. Every change is audited (`shadow_symbol_map` / `shadow_symbol_unmap`, actor user).
  Code: `monitoring/shadow/symbols.py`.
- **Validation (all required; otherwise refused with the reason, nothing saved):**
  - The ISIN is valid: format plus check digit, else 400 `invalid_isin`.
  - The ISIN is a US security (`US…`), else 409 `non_us_isin`: "… not a US security — … enter a valuation price
    instead". Non-US instruments stay "not available" unless the user sets a valuation.
  - `confirm_us_listing: true`, else 400.
  - The ticker is in Alpaca spelling (`^[A-Z][A-Z0-9.]{0,9}$`).
  - The ticker is not mapped to another ISIN (409).
  - The ticker is in the Alpaca asset list (`AlpacaProvider.assets()`, active assets; cached 6 h in memory) with
    `asset_class` `us_equity` (stocks and ETFs), `status` active, `tradable` true and an exchange that is not OTC.
    Otherwise 409 `symbol_refused` with the reason.
  - If the asset list cannot be read → 503 `symbol_check_unavailable`, nothing saved.
  - Never an automatic mapping, never a guess from names. The Alpaca name is only shown back.
- **Price path (insights §20.1, diagnostics §19):**
  - A mapped US ISIN (not crypto / derivative / bond) uses the raw closes of the mapped ticker from the 20-day window
    (splits since the first purchase refused as before).
  - Unmapped, the old rule holds: import symbol + USD + US ISIN.
  - **(REVERTED by §20.11 — original currency, no conversion.)** Insights: for a position held in EUR, the USD close
    was converted with the FRED DEXUSEU fixing of THAT close's
    day (exact date; no fixing → "not available: no FRED DEXUSEU fixing on <day> …"). This is the only conversion in
    the insights; sums stay per currency (LIMITS[0] says so). Other currencies → "no FX source …".
  - Diagnostics convert every price to the base currency as before, so a mapping works for any trade currency.
  - Reasons for unmapped instruments now point to the mapping: "no ticker in the import — map it to its US ticker
    (Shadow Account → Symbol mapping)" and "ISIN X is not a US security — enter a valuation price".
- **API (behind api_auth; strict; insights GETs still do not touch the heartbeat):**
  - `GET /v2/shadow/symbol-mappings` →
    `{"scope": "global", "notice": "…", "mappings": [Mapping]}` (sorted by ISIN).
  - `PUT /v2/shadow/symbol-mappings` with body `{"isin": "US78409V1044", "symbol": "SPGI", "confirm_us_listing": true,
    "reason"?: "3..500 chars"}` → 200 `{"mapping": Mapping}`; 400 `{error, errors{field}}` for shape;
    400/409/503 `{error, code}` for refusals.
  - `DELETE /v2/shadow/symbol-mappings/{isin}` → 200 `{"removed": {"isin", "symbol", "ts"}}`; 404 `not_found`.
  - **Mapping** = `{"isin": "US78409V1044", "symbol": "SPGI", "exchange": "NYSE", "asset_name": "S&P Global Inc.",
    "confirmed_us_listing": true, "source": "user_mapping", "validated_against": "alpaca_assets",
    "created_at": "2026-10-18T10:00:00+00:00", "reason": null}`.
- **JSON changes (WPF):**
  - Insights Position gains `price_detail`: "market close via mapped symbol SPGI (user mapping)" (+ "; USD 500.0000
    converted at the FRED DEXUSEU fixing of <day> (1.2500 USD per EUR)"), "market close of X (symbol from the
    import)", "your valuation" or "valuation in the import".
  - Insights Position also gains `market_symbol`, `symbol_source` (`user_mapping` | `import` | null) and `fx`
    (`{"usd_per_eur", "date", "source": "FRED DEXUSEU", "usd_close"}` | null). `price_source` keeps its values
    (`valuation` | `market` | `none`).
  - Diagnostics `coverage.price_sources`: `[{"key", "symbol", "source": "user_mapping" | "import" | "valuation",
    "price_source": "market close via mapped symbol SPGI (user mapping)" | …}]`.
- **Live-audit fixes (2026-10-18, same stage):**
  - **Disposition coverage rule:** `diagnostics.MIN_PRICED_SHARE = 0.8` is the single constant. Cases are realised
    plus paper gains / losses on sell days, unpriced ones included. If the priced share is below 0.8, the block is
    `{"unavailable": "not meaningful: n of m cases unpriced (add symbol mappings or valuation prices)",
    "not_meaningful": true, "cases", "unpriced_cases", "priced_share", "min_priced_share", "sell_days"}`.
    No PGR / PLR / t-statistic is shown in the API, the report ("Not meaningful: …") or the facts for the AI
    commentary. A shown block also carries `cases`, `priced_share` and `min_priced_share`.
  - **Fees:** diagnostics `turnover.fees` now includes the order fee of a pending private-market order. It is
    therefore the same number as insights `fees` (trades + fee rows + pending order fees).

### 20.11 Price sources in original currency (stage 9d, user decisions 2026-10-18 — backend; exact JSON for the WPF)

User decisions (2026-10-18): "Ich glaube wir sollten immer in der Originalwährung schreiben" → "wir brauchen eine
Kursquelle" → "Ja los gehts".

**1. Original currency everywhere.**
- No FX conversion for valuations or P&L. The §20.10 conversion of a mapped USD close into EUR is REVERTED; the
  Position field `fx` is removed.
- A listing in a different currency than the position's cost (e.g. a USD ticker on a TR position bought in EUR)
  shows its market value in the listing currency next to the cost, with NO unrealised P&L and the note
  "different currencies — not converted".
- The only conversion that stays is inside the shadow-portfolio simulation (SPY bought with EUR flows). It is
  labelled "simulated in USD — converted at the FRED rate of each flow date".
- Diagnostics (§19) compare a price with a trade only when the listing currency equals the trade currency.
  Otherwise the case is unpriced: "different currencies — not converted". The stage-9 normalisation of trades into
  the account's base currency (to_base) is unchanged.

**2. One configured source per data type — no fallback chains.**

| Data type | Source | Currency | Key |
|---|---|---|---|
| US listings (stocks / ETFs) | Alpaca (existing, §19.14 H2 / §20.10) | USD | Alpaca paper keys |
| Stocks / ETFs incl. EU venues, by ISIN | EODHD end-of-day (`eodhd`) | the listing's currency | `eodhd-api-key` (Credential Manager) |
| Crypto BTC / ETH / SOL in EUR | Kraken public market data (`kraken_public`) | EUR | none |
| Issuer derivatives (turbos / calls), bonds (e.g. US Treasury), private funds, other coins | no source — manual valuation | — | — |

OpenFIGI (an ISIN → ticker lookup) is a possible later option only; it is not built.

**3. Price-source order per open position** (insights §20.1):
1. A valuation price: one contained in the import first, then the user's own (as §20.1).
2. The configured listing in the SAME currency as the position's cost → `price`, `value`, `unrealised`.
3. A mapped listing in a DIFFERENT currency → `listing.value` in the listing currency only; `value` and
   `unrealised` are null; note "different currencies — not converted".
4. "not available (no price source): <reason>".

**4. EODHD (verified 2026-10-18 from the official docs; "verify" = not confirmed there).**
Sources:
- EOD endpoint: https://eodhd.com/financial-apis/api-for-historical-data-and-volumes
- Search: https://eodhd.com/financial-apis/search-api-for-stocks-etfs-mutual-funds
- Exchanges list: https://eodhd.com/financial-apis/exchanges-api-list-of-tickers-and-trading-hours
- Limits: https://eodhd.com/financial-apis/api-limits
- Pricing: https://eodhd.com/pricing
- Terms: https://eodhd.com/financial-apis/terms-conditions
- Personal vs commercial use: https://eodhd.com/financial-apis/commercial-vs-personal-license-use

What the docs say:
- **EOD:** `GET https://eodhd.com/api/eod/{CODE}.{EXCHANGE}?api_token=…&fmt=json&from=&to=` returns
  `[{date, open, high, low, close, adjusted_close, volume}]`. `close` is as traded (unadjusted). Exchange codes are
  EODHD codes, not MICs (`US`, `LSE`, `XETRA`, `PA`, …).
- **Search:** `GET https://eodhd.com/api/search/{query}?api_token=…` accepts "a ticker symbol, a company name, or
  an ISIN" (verified). It returns `[{Code, Exchange, Name, Type, Country, Currency, ISIN, isPrimary, previousClose,
  previousCloseDate}]` (verified). It is included in the Free plan (verified).
- **Exchanges list:** `GET https://eodhd.com/api/exchanges-list/?api_token=…` returns
  `[{Name, Code, OperatingMIC, Country, Currency, CountryISO2, CountryISO3}]`.
- **Costs:** 1 API call per request (EOD, search and exchanges list; a 404 also costs 1).
- **Free plan:** "20 API calls per day" (plus 500 welcome-bonus calls per the EOD page) and "1 yr" of EOD history,
  listed under "Personal Plans — Personal use" (verified). The daily limit resets at midnight GMT. When it is
  exceeded the API answers HTTP 402 (minute limit: 429). `/api/user` (0 calls) reports `apiRequests` and
  `dailyRateLimit`; it is not used.
- **Cheapest paid EOD plan:** "EOD Historical All-World" at $19.99/month, 100,000 calls/day (verified).
- **Venues on the free / cheapest plans:** **verify.**
  - XETRA is the documented example code (`SAP.XETRA`). The EOD page says the free plan gets "the same endpoint
    for any ticker", but no page lists the venues per plan.
  - Tradegate, Lang & Schwarz, Stuttgart and gettex are not mentioned in the docs read.
  - Check with your own key: the exchanges list (1 call), or the candidates of a search.
- **Terms:** Non-professional users may "store, manipulate, and analyze the data for private, non-commercial
  purposes". They are prohibited from "redistributing, displaying, or granting access" to others (verified). UFOS.ai
  shows the data only to the key owner on this computer and never exports EODHD prices. Audit reports contain no
  EODHD prices other than position values — **verify** whether sharing such a report counts as redistribution;
  until then the report notes "prices: EODHD, personal use".

**5. EODHD in the data layer** (`data/providers/eodhd.py`; HTTP gate source `eodhd`).
- **Key:** `eodhd-api-key` ("EODHD API key"), read from the Credential Manager at use time. It travels only in the
  query string to eodhd.com and is redacted from every error and log. Never in .env, logs or responses.
- **Daily budget (free plan):** `EODHD_DAILY_LIMIT = 20` calls per UTC day.
  - The counter persists in `meta` `eodhd/calls/<YYYY-MM-DD>`.
  - A call is counted BEFORE it is sent; with no counter, nothing is sent.
  - At the limit: no request, and the message "EODHD daily limit reached (20/20) — next refresh tomorrow".
- **Cache:** EOD history is fetched once per listing per UTC day, covering the last 365 days (the free plan's
  history) in 1 call. It is stored in `meta` `eodhd/eod/<listing>` = `{fetched, bars: [[day, close]]}`, so card
  polls, insights and diagnostics share it. The close of the day therefore appears the next day.
- **Search results:** cached per ISIN for 30 days in `meta` `eodhd/search/<ISIN>`. Candidates whose `ISIN` differs
  from the query are dropped.
- **Splits:** they are not checked for EODHD listings (it would cost calls). A split after your purchase shows a
  wrong value — enter a valuation (LIMITS says so).

**6. Kraken public** (`data/providers/kraken_public.py`; gate source `kraken_public`).
- `GET https://api.kraken.com/0/public/OHLC?pair=<pair>&interval=1440`. No key and no private path: the provider
  allows only host `api.kraken.com` with path prefix `/0/public/` (asserted before every request; the broker live
  guard is unaffected).
- Response: `{error: [], result: {<pair key>: [[time, open, high, low, close, vwap, volume, count], …], last}}`
  (https://docs.kraken.com/api-reference/market-data/get-ohlc-data.md). The last entry is the current,
  uncommitted day and is NOT used. Closes are cached in memory for 15 minutes per pair.
- Fixed table (unambiguous): TR crypto symbol `BTC` → `XBTEUR` ("XBT/EUR"), `ETH` → `ETHEUR` ("ETH/EUR"),
  `SOL` → `SOLEUR` ("SOL/EUR"). It applies only to crypto positions whose cost currency is EUR. Other coins →
  "not available (no price source): crypto <X> — only BTC, ETH, SOL in EUR (Kraken public)".

**7. API (behind api_auth; strict; GETs do not touch the heartbeat).**

- **`GET /v2/shadow/price-sources`** →
```json
{"order": ["valuation", "listing_same_currency", "listing_other_currency", "not_available"],
 "sources": [
  {"id": "alpaca_us", "label": "Alpaca — US listings (USD)", "currency": "USD", "key_name": null},
  {"id": "eodhd", "label": "EODHD — end-of-day closes, stocks / ETFs incl. EU venues (listing currency)",
   "currency": "listing", "key_name": "eodhd-api-key", "key_present": true, "key_error": null,
   "budget": {"plan": "free", "day": "2026-10-18", "daily_limit": 20, "used_today": 3, "left_today": 17,
              "resets": "00:00 GMT", "message": null},
   "cached_listings_today": 2, "terms": "personal use only — no redistribution (EODHD terms)"},
  {"id": "kraken_public", "label": "Kraken public market data — crypto in EUR", "currency": "EUR",
   "key_name": null, "pairs": {"BTC": "XBT/EUR", "ETH": "ETH/EUR", "SOL": "SOL/EUR"}}],
 "no_source": ["issuer derivatives (turbos, calls) — enter a valuation", "bonds (e.g. US Treasury) — enter a valuation",
               "private funds — enter a valuation", "other crypto coins — enter a valuation"],
 "later": ["OpenFIGI ISIN → ticker lookup (not built)"]}
```
  - `budget.message` = "EODHD daily limit reached (20/20) — next refresh tomorrow" at the limit, else null.
  - `key_present` is null when the Credential Manager cannot be read (`key_error` says why).
- **`GET /v2/shadow/listings?isin=<ISIN>`** (EODHD search: 1 call, or 0 when cached) →
```json
{"isin": "DE0007164600", "source": "eodhd", "cached": false, "searched_at": "2026-10-18T10:00:00+00:00",
 "candidates": [{"listing": "SAP.XETRA", "code": "SAP", "exchange": "XETRA", "name": "SAP SE", "type": "Common Stock",
                 "country": "Germany", "currency": "EUR", "is_primary": true, "previous_close": 120.5,
                 "previous_close_date": "2026-10-16"}],
 "budget": {"…": "as above"}}
```
  - Errors: 400 bad ISIN; 409 `eodhd_limit` (the message above); 424 `key_missing` "EODHD API key is not set —
    enter it in Hedge Fund → Settings → Keys."; 503 `source_unavailable` (redacted reason).
  - The user picks ONE candidate; nothing is picked automatically, and `is_primary` is only shown.
- **`PUT /v2/shadow/symbol-mappings`** — two body forms:
  - (§20.10, unchanged) `{"isin", "symbol", "confirm_us_listing": true, "reason"?, "source"?: "alpaca_us"}` →
    Alpaca.
  - (new) `{"isin", "source": "eodhd", "listing": "SAP.XETRA", "confirm_listing": true, "reason"?}`. The listing
    must be one of the cached search candidates of that ISIN (no network call). Otherwise 409 `listing_refused`
    ("search the listings of <ISIN> first" / "<listing> is not a listing of <ISIN> in the EODHD search").
    Any ISIN (also non-US) is allowed.
- **Mapping** (GET / PUT; §20.10 fields kept, new ones added):
```json
{"isin": "DE0007164600", "symbol": "SAP", "exchange": "XETRA", "asset_name": "SAP SE", "confirmed_us_listing": false,
 "confirmed_listing": true, "source": "user_mapping", "price_source": "eodhd", "listing": "SAP.XETRA",
 "currency": "EUR", "validated_against": "eodhd_search", "created_at": "…", "reason": null}
```
  - Alpaca mappings: `price_source` `alpaca_us`, `listing` = the ticker, `currency` USD, `validated_against`
    `alpaca_assets`.
  - Storage **v22**: `sh_symbol_maps` + `price_source`, `listing`, `currency`. Older rows are alpaca_us / USD.
- **Insights Position** (changes against §20.4/§20.10):
  - `price_source`: `valuation` | `market` | `market_other_currency` | `none`.
  - `price_provider`: `alpaca_us` | `eodhd` | `kraken_public` | null.
  - `symbol_source`: `user_mapping` | `import` | `fixed_pair` | null.
  - `market_symbol`: the ticker / listing / pair.
  - `listing`: `{"currency", "price", "value", "as_of"}` — only for `market_other_currency`, else null.
  - `price_detail` texts:
    - "your valuation" / "valuation in the import"
    - "market close via mapped symbol SPGI (user mapping)" (Alpaca, USD position)
    - "market close of SPGI (symbol from the import)"
    - "EODHD close of SAP.XETRA (EUR, your listing)"
    - "Kraken close of XBT/EUR (public market data)"
    - `market_other_currency`: "<one of the above> — USD 1,000.00; different currencies — not converted"
  - `note` (`none` or `market_other_currency`):
    - "not available (no price source): …" with the reason, e.g. "derivative — issuer product without a price
      source; enter a valuation price", "bond — no price source; enter a valuation price", "no listing mapped —
      pick its listing (Shadow Account → Symbol mapping)", "EODHD daily limit reached (20/20) — next refresh
      tomorrow", "EODHD API key is not set — enter it in Hedge Fund → Settings → Keys.", "crypto X — only BTC, ETH, SOL in EUR
      (Kraken public)".
    - Or "different currencies — not converted (listing USD, cost EUR)".
  - Removed: `fx`.
- **Allocation:** only `value` (cost currency) is summed. `market_other_currency` positions count as not valued.
  Reasons add "n position(s) valued only in another currency (no P&L)".
- **Diagnostics:** `coverage.price_sources[].provider` is added; cross-currency cases are excluded with
  "different currencies — not converted".
- **Shadows:** a non-USD account's shadow rows carry `"conversion": "simulated in USD — converted at the FRED rate
  of each flow date"` (the report shows it in the Note column).
- **Review fixes 2026-10-18 (independent stage-9d review):**
  - HIGH — failed fetches are cached too:
    - The outcome per listing per UTC day is stored as `meta` `eodhd/eod/<listing>` = `{fetched, fetched_at,
      ok: true, bars}` or `{fetched, fetched_at, ok: false, reason}`.
    - A failure is shown with the SAME text until tomorrow or an explicit refresh: "EODHD SAP.XETRA: EODHD has no
      data for this listing (HTTP 404) — cached until tomorrow (refresh costs 1 call)".
    - It is not cached when no call was made (daily limit reached, key missing).
    - No 4xx (incl. 402 / 429) is retried; a 5xx or a timeout is retried ONCE. Every attempt is counted.
  - **Refresh:** `POST /v2/shadow/price-sources/eodhd/refresh` with body `{"listing": "SAP.XETRA"}` (one of your
    EODHD listings, else 404) fetches again now and costs 1 call. It is audited (`shadow_eodhd_refresh`) and returns
    `{"listing", "ok", "reason", "closes": n, "last": {"date", "close"} | null, "cost": "1 EODHD call",
    "budget": Budget}`. A failed refresh is 200 with `ok: false` and the reason; the limit gives 409 `eodhd_limit`.
  - `GET /v2/shadow/price-sources` → the eodhd entry gains `"failures_today": [{"listing", "reason",
    "fetched_at"}]` and `"refresh": "POST /v2/shadow/price-sources/eodhd/refresh {listing} — costs 1 EODHD call"`.
    `cached_listings_today` counts successful fetches only.
  - MEDIUM — single flight: one lock per listing and per ISIN search covers the cache check, the fetch and the
    store. Parallel first-of-day reads make ONE call (tested with threads).
  - LOW — the audit report (HTML + JSON `price_notes`) carries "Prices: EODHD (personal use, no
    redistribution)." and / or "Prices: Kraken public market data (crypto in EUR)." when its numbers used them.
    The unused `insights._eligible` is removed.

## Click-test fixes 2026-10-22 (backend; live click-test with the user, build green)

1. **Studio / loop drafts with incomplete parameters (HIGH).**
   - Root cause: session "Test" 12:45. The first answer had window arguments that were not integer parameters
     (`bad_window`). The single repair answer rewrote both parameters as `{"value", "integer": true}` and dropped
     `min` / `max` (`missing_key`). With only one repair round, the draft failed.
   - The parameter form was only in the schema example. There was no explicit rule, and the repair message
     neither named the parameter form nor warned against dropping keys.
   - The loop's Refiner prompt had the same gap. The core validator does not require `step`, so a variant without
     `step` passed and then failed G5 ("needs ≥ 4 parameter configurations").
   - Fix — rule 3a in BOTH system prompts (Studio `research/prompts.SYSTEM_PROMPT`, loop
     `discovery/prompts.REFINE_SYSTEM_PROMPT`): every parameter MUST be `{"value", "min", "max", "step"}` (+
     `"integer": true` for windows / lags / counts).
   - Fix — the repair message carries `parameter_fixes: [{parameter, missing: [keys], required_form}]` and
     `parameter_form`, with the instruction never to drop min / max / step.
   - Fix — `studio._validate_spec` (every AI-draft path: Studio, loop seed / refinement, paper drafts) reports a
     missing `step` as `missing_key` `$.parameters.<name>.step`.
   - Missing values are NEVER filled in. Tests use the recorded answer of the session.
2. **Stress "Costs x2" = "Slippage x3" (verified: not a code bug).**
   - With the paper-broker cost model fee = slippage = half of the per-fill cost, so 1 × fee + 3 × slippage =
     2 × (fee + slippage). Both scenarios charge the same per-fill cost (bt-fff968623113: equity 10 bps, crypto
     40 bps in both), and the runs are identical by construction.
   - The slippage scenario now carries `note` "same per-fill cost as "Costs x2" for this cost model … identical by
     construction, not a separate stress".
   - Test: with fee ≠ slippage (custom 1 / 4 bps) the scenarios differ (10 vs 13 bps, different returns).
   - Changing the multipliers is a user decision (2026-10-04 defined them) and was not done.
3. **Stress warnings — new field `warning_summary` on every stress scenario** (in new runs and computed on read
   for stored cards, `runcard.get`):
   `{"total": 46, "counts": {"symbol_excluded": 34, "seam_mismatch": 10, …}, "symbols": {"symbol_excluded": ["AGG",
   …], …}, "text": "34 symbols excluded (no data in the window): AGG, BIL, … and 26 more; 9 symbols with a seam
   mismatch (no pre-2016 data used): INDA, SPY, …; 1 symbol without data before 2016: XLC; shorter warm-up than
   requested"}`.
   - `text` is the status line; `""` when there are no warnings.
   - The raw `warnings` list is unchanged.
   - The WPF shows `warning_summary.text` instead of joining the raw codes.
4. **Reports before an account existed.**
   - `reports.accounts_in(s, e)`: an account opened at or after the period end is left out. An account opened
     inside the period gets `opened` (ts), `partial: true` and `note` "opened <ts> — partial period", plus a
     status fact `acc.<id>.partial`. Otherwise `opened: null`, `partial: false`, `note: null`.
   - The scheduler creates no report for a period in which no account existed.
   - Account `created_at` now comes from the accounts clock (`accounts.now()`; real time in production).
5. **Loop seed re-backtest (not intentional).**
   - §16.2 meant to reuse an identical run. The check refused cards with explicit default benchmark / capital
     (the WPF sends `SPY` / `100000`) and cards with stress scenarios.
   - The reuse rule is now as written in §16 above: it scans the version's done backtest cards, and the engine
     version must match.
6. **Log spam:** `fund.engine` logs "valuation paused: no current quote for …" once per change of the symbol set,
   then at most every 10 min (`VALUATION_PAUSED_LOG_SECONDS`). A successful valuation resets it. The UI state is
   unchanged.
7. **Eurostat TLS:**
   - Most likely cause: Python on Windows reads only the roots ALREADY installed in the Windows store, while Windows
     installs trusted roots on demand through CryptoAPI, which Python never triggers. An incomplete server chain
     would look the same — **verify** (ec.europa.eu is not reachable from the build machine).
   - Fix: `strategy_platform/core/tls.client_context()` = system store + certifi roots (`certifi` added to
     requirements.txt), CERT_REQUIRED + host-name check. It is used by the FRED / Eurostat, calendar and SEC EDGAR
     aiohttp sessions. Verification is never disabled.
   - If the cause is a missing intermediate on the server side, certifi does not help — then the reason stays
     visible in the status.
8. **Capacity "At most" (no backend bug).**
   - `metrics.capacity.at_most` is set ONLY when the 1 % participation limit already binds at the tested capital
     (then `capacity_usd` is null and `reason` explains it). Otherwise `at_most` is null and `capacity_usd` is the
     estimate.
   - bt-fff968623113: `capacity_usd` 7.6 M, `at_most` null.
   - The WPF shows "— (not binding …)" for null.
9. **Live-guard text:** "Live trading is disabled in this build (stage 12 requires the user's explicit go)." —
   stage 11 is now the visual overhaul. All backend texts and this SPEC say stage 12 for the first live step.
- **Review follow-ups (2026-10-22):**
  - `GET /v2/loops/estimate` uses the same seed-reuse check (`discovery.loop.reusable_seed_card`; new optional
    query `history_from` = 2016 | 2000) and answers `seed_reused` (bool). With a reusable seed card,
    `backtests_planned` and the projected G4 trials are one lower.
  - `core/tls.client_context()` checks `CERT_REQUIRED` + `check_hostname` explicitly and raises otherwise (no
    `assert`, safe under `python -O`). Should Eurostat turn out to send an incomplete chain, the status keeps the
    verification error — no workaround.
  - Every `parameter_fixes` entry has an `instruction`, e.g. 'Add "step" (a number > 0: the grid step between min
    and max, integer for windows) to parameter "lookback"; keep value, min, max unchanged. Use "integer": true when
    it is a window, lag or count.'

## Retest fixes 2026-10-23 (backend)

1. **Valuation right after a manual fill.**
   - Before: GETs read only the runtime's last quotes (§17.11, no network in a GET). A symbol bought between two
     cycles (24/7 crypto) had no quote, so the account showed "valuation paused" and "NO REFERENCE YET" until the
     next cycle (up to 5 min).
   - Now `oms.place` hands the quotes it fetched for the order (the traded symbol + every held one, through the
     account's one quote source and its HTTP gate) to `runtime.remember_quotes`. The fill is valued at once.
   - Only real quotes are kept (price > 0), never a stand-in. GETs still never fetch.
2. **Price collar without a reference.**
   - Before: the check passed with "no reference price given". SPEC §17 had not decided such a pass, so it was an
     implicit fallback and is removed.
   - Reference: the decision's `ref_price` (runtime orders), else a fresh read of the latest quote
     (`checks.latest_quote_price`, the account's one quote source).
   - A resting limit / stop price away from the market is legitimate and is not used as the reference.
   - If no reference can be read, a risk-increasing order FAILS: "no reference price for <SYM> — the latest quote
     could not be read, so the price collar cannot be checked".
   - A closing / protective order (kill switch, stops) passes with "closing order: no reference price … — not
     blocked", exactly as the quote check treats closing orders.
   - A passing check states its basis, e.g. "0.00 % from the latest quote 81,681.6000 (collar 10 %)".
3. Kill switch, migration dry run and Eurostat TLS confirmed in the retest — no change.
4. **Venue-aware quotes (retest 14:01).** `BrokerAccount.can_hold(symbol)`: kraken-demo / binance-demo hold crypto
   only, alpaca-paper equities only. A crypto venue no longer reads its own quote for SPY: no request, no log line.
   The runtime fetches the SPY benchmark only where the venue can hold it.
5. **Order quote deadline + worst-case submit duration (for the WPF client timeout).**
   - `oms.place` reads every quote one order needs (the traded symbol, held symbols without a quote, the collar's
     fresh read) IN PARALLEL with ONE deadline, `ORDER_QUOTE_DEADLINE_SECONDS = 8`. A read still running then is
     abandoned (never used): "no current quote for BTC-USD (no quote within 8 s) — order not sent".
   - Worst case per order, internal paper account (paper-main): about 8 s of quotes + local work ≈ 10 s.
   - Worst case per order, broker account (alpaca-paper, binance-demo), theoretical:
     - Quotes take up to 8 s.
     - One adapter submit takes up to 148 s: an id lookup ≤ 64 s (3 gate attempts × 20 s timeout + 1 s + 3 s
       backoff), the send ≤ 20 s (never retried by the gate), and a lookup after an ambiguous send ≤ 64 s.
     - The OMS tries 3 times in the request with 1 s / 3 s pauses.
     - Total ≈ 456 s (≈ 7.6 min). Typical failures end much earlier.
   - The WPF should not block that long. With a client timeout of about 30 s, it shows "sent — the result follows
     in the order list" and polls the order by its `client_order_id` (idempotent; the order is never duplicated).

## Sandbox profile (contract, user decision 2026-10-23)

Purpose: click-test the incubation / monitoring flow while the user's real databases stay clean. No strategy has
passed validation + holdout yet; the holdout is never peeked at and no evidence is faked in the real DB.
Code: `sandbox_profile.py` (mode, paths, guards), `tools/sandbox.py` (CLI), `sandbox_fixtures.py` (seeding,
force-decay), `strategy_platform/core/fixture.py` (labels). Tests: `tests/test_sandbox_profile.py`.

- **S1 Activation by tool, never by UI** (run from `HedgeFund\backend`: `.venv\Scripts\python.exe tools\sandbox.py …`):
  - `on`: refused while the backend answers `GET /health` on 127.0.0.1:`HEALTH_PORT` ("close UFOS.ai first"; an
    unclear answer — timeout, odd error — is also a refusal). Copies hedgefund.db, newsroom.db, market_research.db
    (those present; hedgefund.db required) into `data/sandbox/` with the SQLite online backup API (source opened
    read-only, copy `PRAGMA quick_check`ed, then renamed into place) plus `data/papers/` if present. An earlier
    sandbox copy is renamed `*.<UTC stamp>.stale`, never deleted. Then writes the marker `data/SANDBOX_ACTIVE`
    (JSON: `mode`, `created_at`, per file `source_sha256`, `source_wal_sha256`, `source_bytes`, `copy_sha256`).
    A real file that changes while it is copied aborts `on` (no marker).
  - Then `on` seeds the incubation fixture inside the sandbox (user 2026-10-24; same code path as
    `seed-incubation-test`, S4) and prints its name, id and state.
  - If seeding fails, `on` says so with the reason and exits 4. The sandbox stays ON; the user runs
    `seed-incubation-test` after fixing the cause. Seeding is never skipped silently.
  - `seed-incubation-test` stays as a separate command for re-seeding.
  - Venv guard (2026-10-24): the tool refuses to run unless `sys.prefix` is `HedgeFund\backend\.venv` ("run with
    .venv\Scripts\python.exe tools\sandbox.py …"). The system Python lacks the backend's packages, e.g. tzdata:
    "No time zone found with key America/New_York".
  - `off`: refused while the backend runs. Re-hashes the real files and reports per file whether they are identical
    to `on` (exit 0 identical, 3 changed = the real backend or another program wrote them in between); renames the
    marker to `SANDBOX_ACTIVE.<stamp>.stale` (the result is written into it); `data/sandbox/` is kept.
  - An EMPTY `-wal` and an absent `-wal` are the same database state (2026-10-24). `-shm` is never compared.
  - The read-only source connection of `on` creates an empty `-wal` / `-shm` pair. Any later ordinary open + close
    of the real file by any SQLite client (even a pure read) removes the pair without changing data.
  - Such a pair is reported as `note: "wal: empty → absent (no data change)"` (or "absent → empty"); markers
    written before this change (an empty-file hash) compare the same way. A non-empty WAL difference stays a
    change.
  - `status`: mode, backend running, marker summary, real files unchanged so far.
- **S2 Backend start.** `main.py` calls `sandbox_profile.startup()` before any database is opened; the mode is fixed
  for the process. With the marker: every database path (`storage.db`, `newsroom.db`, `market_research.store`
  `default_path()`; the paper texts and db backups follow hedgefund.db) is in `data/sandbox/`
  (`UFOS_HEDGEFUND_DB` is ignored there), a `#`-framed WARNING says so and every later log line carries
  `[SANDBOX]`. An unreadable marker or a missing sandbox copy stops the start (exit 2, CRITICAL log) — never a
  fallback to the real files. `data/api-token` and `data/logs/` stay where the app reads them.
  - Payload field for the app banner: `"mode": "normal" | "sandbox"` on `GET /health` (now JSON
    `{"status": "ok", "mode": …}`; the app only checks the 200), `GET /version`, `GET /fund/state`,
    `GET /v2/portfolio/state`, `/v2/oms/runtime`, `/v2/incubation`, `/v2/monitoring/overview`, `/v2/brokers`.
    In sandbox mode also `"sandbox": {since, banner, brokers: "blocked in sandbox", data_dir: "data/sandbox",
    prices_note}` (fix1023: "prices for copied positions may be unavailable while brokers are blocked").
- **S3 Isolation in sandbox mode.**
  - Broker accounts (alpaca-paper, binance-demo; kraken-demo stays retired) are hard-blocked:
    `mirror.remote_adapter` returns `SandboxBlockedAdapter` (every call raises "blocked in sandbox", no client is
    built — also not a test fake), `accounts.rows()` lists none of them as active (runtime, 15 s poller, start-up
    reconciliation, allocator and kill switch skip them), every broker action answers 409 `sandbox_blocked`,
    pre-trade checks reject with the reason, routing offers paper-main only, an incubation start on a broker route is
    refused (`sandbox_blocked`), `GET /v2/brokers[/{id}]` shows `state: "blocked_sandbox"`,
    `sandbox_blocked: {label: "blocked in sandbox", reason}`, all `can` false.
  - Market DATA reads stay allowed (quotes, bars, the Alpaca data API incl. its asset list) — the runtime needs prices.
  - Old engine: `set_autopilot(true)` → 409 with the sandbox text; no scheduled full cycles (`schedule_mode`
    "manual_only", no next run); "Run now" still runs inside the sandbox.
  - Kill switch: acts on the sandbox copies only; a broker account is never contacted (`sandbox_blocked` in the
    per-account result).
  - AI cost ledger lines get `"mode": "sandbox"` and the label prefix "SANDBOX · " (normal lines unchanged).
  - Not changed: discovery-loop auto-resume, Research and Newsroom collectors run on the sandbox copies (their AI
    calls are tagged as above).
- **S4 Seeding** `tools/sandbox.py seed-incubation-test` — refused unless the marker is active and the process is in
  sandbox mode (`require_sandbox`: explicit check + assert; the CLI opens no database before it). Creates
  "SANDBOX TEST — incubation fixture" (tag `sandbox-fixture`; SPY/IEF, hold the one further above its 200-day
  average, top 1, monthly rebalance, long only — the runtime trades it on paper-main) and moves it
  draft → backtested → candidate → validated THROUGH `registry.transition` with synthetic run cards (backtest,
  validation with G1–G8 "pass — not evaluated", holdout with H; deterministic made-up curves, no market data, the real
  holdout window's dates only as labels; the holdout event belongs to the synthetic holdout card). Every card's
  `metrics.fixture = true`, `metrics.fixture_note = "synthetic fixture for UI testing — no real validation"`, a
  `fixture` warning; reports carry both keys too; transition reasons start with "SANDBOX fixture — …". Idempotent
  (an existing fixture is reported, an interrupted seeding continues).
  - API: `fixture: true` + `fixture_note` on run lists / details (`/v2/backtests…`, `/v2/validations…`), strategy
    list / detail (by the tag), incubation status, monitoring views — absent everywhere else (normal payloads
    unchanged).
- **S5 Decay helper** `tools/sandbox.py force-decay "<strategy name or id>"` — sandbox only, needs an active
  incubation run. Appends ONE synthetic shock session (drawdown = DD_FACTOR × holdout max DD + 5 %) to the run's
  real sessions, evaluates `decay.assess` and pauses through `decay._pause` (audit `decay_paused` with
  `synthetic: true`, event, alert). Reason "SANDBOX force-decay — synthetic deviation, not real performance: …";
  the monitoring view shows `decay.paused.synthetic: true` + the fixture keys. The runtime then withholds the
  strategy's orders as after a real trigger; keep running / demote work as usual.

## Fix batch fix1023 (2026-10-23) — confirmation style + sandbox click-test findings (backend + WPF)

**A. Confirmation style (user decision 2026-10-23: the users are professionals).** No typed confirm words, no
"I understand" flags, audit notes OPTIONAL. The app shows ONE confirmation step with a summary and a precise button.
Server side: the fields below are no longer required. If sent, `confirm` / `confirm_*` are accepted and ignored.
`reason` is an optional note (missing / null / "" → stored as ""; ≤ 500 characters, longer → 400).
- POST /v2/incubation, POST /v2/incubation/{id}/abort — reason.
- POST /v2/monitoring/strategies/{sid}/decision — reason; confirm "DEMOTE" (`decay.decide` no longer checks it).
- POST /v2/strategies/{id}/transition — reason (`registry.transition` / `incubation_transition` accept "").
  Promotions still need their stage evidence (run cards); only the note became optional.
- POST /v2/oms/resume, POST /v2/portfolio/allocation/rebalance, PUT /v2/portfolio/settings — reason.
- POST /v2/portfolio/migration — confirm "MIGRATE" (`migration.run` no longer checks it), reason.
- POST /v2/brokers/{id}/connect — confirm "CONNECT …" (`service.connect`), reason. The binance-demo
  `starting_balances` baseline (§18.14) is data and unchanged.
- POST /v2/brokers/{id}/disconnect — reason. POST /v2/brokers/{id}/resolve — reason; confirm "ACCEPT BROKER VALUES"
  (`reconcile.resolve`). POST /v2/brokers/routing — confirm "ROUTE TO …" (`service.set_route`), reason.
- Shadow: POST …/imports/{import_id}/remove — confirm "REMOVE IMPORT" (`shadow.service.remove_import`), reason.
  PUT /v2/shadow/symbol-mappings — `confirm_us_listing` / `confirm_listing`. These map the user's
  (read-only) monitored holdings to price symbols; no money moves.
- POST /v2/studio/sessions/{id}/drafts/{mid}/save — `confirm: true` (calling the endpoint IS the confirmation).
- Market Research: DELETE /research/drawings?symbol= — `confirm=true` (also note in market_research/SPEC.md).
**Kept on purpose:** the live guard (stage 12: PUT /v2/brokers/{id}/live-guard needs a reason, POST
…/live-guard/check its confirm) and the validation-evidence interventions (POST /v2/strategies/{id}/holdout: typed
strategy name; POST /v2/validations/{id}/exceptions: reason ≥ 30 characters, never for H).

**B. Sandbox click-test findings.**
1. Incubation start is serialised (`incubation._start_lock`). A double submit gets 409 `already_incubating`
   ("an incubation run of this strategy is still running"). Never a second run.
2. GET /v2/monitoring/strategies/{sid}:
   - `strategy.decay.paused.numbers` = {rule, value, threshold, triggered, synthetic}: the values that
     triggered THIS pause, from the event.
   - A synthetic (sandbox force-decay) pause also has `paused.synthetic: true` and
     `paused.numbers_note: "synthetic pause — live numbers not affected"`.
   - New `decay.last_check_at` and `decay.last_check_source` ("check" | "pause" | null). A pause newer than the
     last scheduled check counts as the last check (`last_check_day` = its check day).
3. The decay decision history must stay reachable. The app never hid `events` without a series; the actual causes
   were that the strategy detail was not refreshed while expanded (WPF) and that after a demote the decisions were
   unreachable. Now GET /v2/incubation/{id} carries `decay_events`
   [{id, ts, action, actor, reason, rule, check_day, synthetic}], ended runs included (`decay.run_decisions`).
4. (WPF) Status lines are replaced on every monitor refresh.
5. Every pause raises its own CRITICAL `decay` alert. The dedupe key is now `decay:<run>:<day>:<event id>`. Before,
   a re-pause after keep running on the same check day was deduplicated away.
6. Sandbox: `sandbox.prices_note` = "prices for copied positions may be unavailable while brokers are blocked"
   (text only, no stand-in prices).
**WPF (fix1023).** Pattern for every confirmation dialog: ONE step. It shows a summary, the existing warning text,
an optional note box ("Note (optional, stored in the audit log)", ≤ 500 characters) and a precise action button.
Examples: "Start 60-session incubation (length fixed)", "Demote — abort run inc-… and close positions",
"Keep running — end the pause, orders resume", "Route <strategy> to alpaca-paper",
"Accept broker values — overwrite the mirror of <id>", "Connect binance-demo — record these balances as the baseline",
"Save 2 changes — loosens 1 limit", "Move to Retired". The request carries `reason` only when a note was typed and
sends no confirm word (PlatformClient.WithNote).
While a request is in flight, the action button is disabled and shows what is happening ("Starting…",
"Sending…", "Demoting…", "Resolving…", "Switching…", "Saving…", "Migrating…", "Placing…", "Opening…") via
TradingUi.SetBusy, so a double click cannot submit twice.
Changed: Start incubation (tick + reason removed), Keep running (tick), Demote (typed DEMOTE), Abort run, Resume
account, Rebalance, Disconnect (generic TradingReasonDialog → optional note), Risk-limit settings ("I confirm
loosening" tick → button text names the loosened limits), Migration (typed MIGRATE), Broker connect / reconnect
(typed CONNECT …), Binance baseline ("I confirm these starting balances" tick → button text), Resolve reconciliation
(typed ACCEPT BROKER VALUES), Routing (typed ROUTE TO …), Shadow remove import (typed REMOVE IMPORT, same dialog
class), Shadow symbol mapping (confirmation tick → statement + button text), Library "Move to…" (required reason →
optional). Already one-step, unchanged: kill switch (global and per broker, optional reason), order dialog
(optional reason; now also busy-disabled), autopilot / auto-trading switches.
Kept on purpose: the live guard (read-only checklist in this build, no control can enable live), Open holdout
(typed strategy name; busy text added), gate exceptions (reason ≥ 30 characters).
Monitoring: the paused block and the decision dialog show `paused.numbers` as "Numbers at the pause" (or "SYNTHETIC
numbers at the pause …" + numbers_note in red); a synthetic pause never shows the live "Numbers now" line. "Last
decay check" uses last_check_at / last_check_source ("the pause at …; no scheduled check since"); "none yet" only
when there is neither a check nor a pause. The green action line (e.g. "Keep running: … no longer paused") is
cleared on the next monitor refresh that shows a different strategy / decay state (fingerprint of id, run, run
status, decay state and pause time). The sandbox strip shows `sandbox.prices_note`.

**Round 2 (review 2026-10-23).**
- A second start (sequential or concurrent) → 409 `already_incubating` "an incubation run of this strategy is
  still running". The running run is checked before the state.
- Shadow notes (classifications, both mapping sources) are optional notes (1–2 characters fine, ≤ 500).
- NEW GET /v2/shadow/symbol-check?ticker=&isin= (read-only, `symbols.lookup`): {ticker, found, asset_name, exchange,
  status, tradable, asset_class, usable, refused_reason, mapped_to_isin, isin, isin_valid, isin_us,
  current_mapping}. The app shows Alpaca's asset name before saving; 503 `symbol_check_unavailable` when the list
  cannot be read.
- Mapping provenance, **hedgefund.db v24**: `sh_symbol_maps.confirmed_via` TEXT ("dialog" | "confirm_flag"; NULL in
  rows before v24 reads as "confirm_flag", since the flag was required then). `confirmed_us_listing` = 1 only for a
  flag. Payload `confirmed_via`; `confirmed_listing` (eodhd) is true only for a flag. Additive column, no rewrite, no
  pre-migration backup (unlike v23). ~~OS1's archive tables take v25+.~~ (struck 2026-10-24: the archive has its own `options_archive.db`; v25 = the Council tables, §22.3)
- `decay.last_check_source` = "check" when the scheduled check produced the pause (equal timestamps), "pause" only
  for a newer pause.
- One pause event per pause: `decay._pause` re-reads the latest event inside BEGIN IMMEDIATE (cross-process safe:
  the check in the backend vs force-decay in the CLI); the loser returns the existing event (`existing: true`) and
  raises no alert. Alerts keep INSERT OR IGNORE with the per-event dedupe key.
- Texts built from an empty note have no trailing ": " (abort, demote, retire-from-incubation, resolve, resume,
  allocation notes).
**Round 3 (sandbox click-test 2026-10-23: "The backend did not answer in time" on Start incubation).**
- Measured cause: POST /v2/incubation took ≈ 67 s. The run was committed at once, but the synchronous allocation
  after the start read a BTC-USD quote, and Alpaca timed out 3 × 20 s plus backoff.
- Now `incubation.start` commits under `_start_lock` and then runs `allocation.rebalance` in a background thread. It
  waits at most `ALLOCATION_WAIT_SECONDS` = 3 s.
  - Response `allocation`: {status: done | failed (+ error) | pending (+ note)}.
  - A failure, also a late one, raises a WARNING alert `allocation_failed` (new alert kind; ref = run id) and an
    audit row `incubation_allocation_failed`. The runtime retries the allocation at its next cycle.
  - 409 `already_incubating` is unchanged.
- GET /v2/incubation?strategy_id=&started_after=<ISO with offset> returns only runs with `created_at` ≥ that time
  and echoes `started_after`. A client confirms a start after a timeout this way. Without an offset or unparsable →
  400.
- GET /v2/incubation/{id} `decisions`: a decision pending when its run was aborted has `status: "cancelled"`,
  `cancelled_at` and `cancelled_note`. The runtime only executes decisions of active runs.
- `incubation.run_events` (and the abort event) leave empty strings out of `data`.
Tests: tests/test_fix1023.py, tests/test_fix1023_round2.py, tests/test_fix1023_round3.py; old tests updated to the new
contract (backups *.pre-fix1023.stale / *.pre-r2.stale).

## 21. Options Structure layer — OS0 contract (2026-10-23), OS1 archive built (2026-10-24)

Plan: project doc `claude/options-structure-plan.md` (concept 2026-10-21, decisions §3, phases §4, point-in-time §6,
pre-registration §7); approved by the user 2026-10-23 ("Yes los gehts"). OS0 = this contract + a READ-ONLY data check +
the frozen pre-registration. OS1 (archive) is built next, both before stage 10; OS2–OS4 after stage 10 and Depth &
Range. This section is the binding contract for OS1–OS4 (review 2026-10-23 incorporated). Nothing here trades; the
traded instrument stays SPY / QQQ **shares** through the existing OMS (options trading deferred, 0DTE struck, no
crypto). Values marked **PROVISIONAL** are confirmed or changed after the user's runs of the check (§21.10; filter,
cadence, OI floor and FRED series confirmed 2026-10-24; staleness + coverage after the RTH run) — since
OS1 they all live in ONE config module (`strategy_platform/options_archive/config.py`, overridable by
`data/options_archive_config.json` without code edits, §21.11).

### 21.1 Data sources (to be confirmed by the user's run of the check tool, §21.9)

| Data | Endpoint (GET only) | Notes |
|---|---|---|
| Option contracts + open interest | Alpaca **trading** API, paper host `https://paper-api.alpaca.markets/v2/options/contracts` (`underlying_symbols`, `status=active`, `expiration_date_gte/lte`, `strike_price_gte/lte`, `limit` ≤ 10 000 — the OS0 tool and the archive use **1 000** (a 10 000 page timed out, user run 2026-10-23), `page_token`) | **`expiration_date_lte` MUST always be passed explicitly** (with `expiration_date_gte`): the endpoint's default is "up to the next weekend". `open_interest` + `open_interest_date` — meaning **confirmed by the OS0 run 20261024T114530Z**: the OI of the trading day `open_interest_date`, published once a day on the morning of the NEXT trading day (Sat 2026-10-24: `open_interest_date` 2026-10-22 on every row — Friday's OI not yet published); OI present on 72–79 % of the contracts; `close_price(_date)` |
| Chain snapshot | Alpaca **data** API `https://data.alpaca.markets/v1beta1/options/snapshots/{SPY,QQQ}` (`feed`, `limit` ≤ 1 000, filters as above, `page_token`) | **`feed=indicative` MUST always be passed explicitly** — the endpoint's default is `opra` (paid, not used; an upgrade is a separate user decision, plan §3). `latestQuote` (`t`, bid/ask/sizes), `latestTrade`, `greeks` (delta, gamma, theta, vega, rho), `impliedVolatility`, `dailyBar` (`v` = the day's volume). Indicative = derived quotes, trades 15 min delayed |
| Option bars | `https://data.alpaca.markets/v1beta1/options/bars` (`1Min`, `1Day`, …; no `feed` parameter) | history since 2024-02 (Alpaca docs) — verified by the check with an expired Feb 2024 contract |
| Underlying | the existing Alpaca stock provider (§3: SIP history, IEX live) | spot for the strike filter = the latest IEX trade at snapshot time (its time = `spot_t`) |
| VIX term structure | FRED via the existing key `fred-api-key` (§7.8 client / newsroom collector): **VIXCLS** (30-day, S&P 500), **VXVCLS** (3-month), **VXNCLS** (Nasdaq-100, for the QQQ replication) | **Confirmed by the OS0 run 20261024T114530Z:** VIXCLS, VXVCLS, VXNCLS exist; NO short-term series on FRED (`VXSTCLS`, `VIX9DCLS` do not exist). → **VIX term structure = VIXCLS (30-day) vs VXVCLS (3-month)**; VXNCLS for the QQQ replication; no other source without a user decision (no fallback) |

**Request budget — fixed split, no cross-process limiter (lead decision 2026-10-23):** Alpaca allows 200 req/min per
account. The backend's `http_gate` source `alpaca` is lowered from 180 to **150 req/min** (+ the existing burst 10 →
≤ 160 in any minute; Market Research keeps ≤ 60 of it, §3 / MR Q3); the OS1 archive process has its **own** gate of
**30 req/min**; total ≤ 190 < 200. We ASSUME that Alpaca's trading API and data API share one per-account limit
(conservative): the official docs state 200/min per account / key, but say nothing explicit about the two APIs
sharing it; the only statement found is a community answer in the Alpaca forum ("completely independent of one's
market data subscription", https://forum.alpaca.markets/t/is-there-a-way-to-increase-the-200-min-api-call-limit-for-the-trading-endpoint/18110)
— not official, so the split stays conservative. The archive runs one request at a time with a pause between pages.
One filtered snapshot of both symbols = contract pages (once a day) + chain pages; the check reports the measured
numbers for 1 / 13 / 28 / 78 snapshots per session (`d_budget`). A snapshot that cannot finish by its slot time is a
gap, never completed later with other data.

### 21.2 Universe, filter, cadence and slots

- **SPY** (primary), **QQQ** (replication). No other underlyings in Phase A.
- **Filter (CONFIRMED 2026-10-24 — user decision after the OS0 run 20261024T114530Z):** strikes within
  **±10 %** of the spot at snapshot time, expiries **0 … 45 calendar days** (same-day expiries are ARCHIVED because they
  carry gamma, even though 0DTE trading is struck). Measured grid: ±5 / 10 / 15 % × ≤ 30 / 45 / 60 days.
- **Cadence (CONFIRMED 2026-10-24 — user decision, ≈ 11.5 GB / year accepted; OS0 run: 7 requests per intraday
  snapshot, 19 per EOD snapshot incl. the contract pages):** contracts / OI once per morning (08:30 New York, before the open); one EOD snapshot
  (16:15 New York); intraday snapshots every 15 minutes in RTH (09:45 … 15:45) PLUS fixed slots at **09:55** (H2: GEX at
  10:00) and **15:25** (H1: chain at or before 15:25) → **28** snapshots per session (the check's `PROPOSED_CADENCE`).
- **Slot rule (implementation rule — the frozen pre-registration is NOT edited):** a slot snapshot must FINISH by its
  slot time (e.g. 15:25:00.000 New York): it starts early enough (slot − 90 s, adjusted from the measured pages and
  latency); every quote stamped AFTER the slot time is excluded (counted as a gap for that contract); a snapshot not
  finished by the slot time is a gap for that slot. This makes "chain snapshot at or before 15:25" hold.
- **Early-close days (OS1 decision 2026-10-24, implementation rule):** NYSE calendar `core/calendars.py` (holidays +
  13:00 early closes). Intraday slots only while the market is open (09:45 … 12:45), 09:55 kept, **no 15:25 slot**
  (after the close). **H1 and H2 undefined on early-close sessions — CONFIRMED by the user 2026-10-24** ("Ja, lass die
  nicht auswerten"; decided before the first early close 2026-11-27): such sessions are excluded from BOTH
  evaluations with the reason "early close" (the frozen pre-registration is not edited); EOD at close + 15 min
  (13:15). Holidays / weekends: no slots. Slots are New York wall-clock times (zoneinfo) — the EU/US DST gap week
  (2026-10-25 … 11-01) is tested.

### 21.3 Point-in-time rules (binding for every archived and computed value)

- Every value carries: `valid_for` (the market time it describes), `available_at` (the earliest time the platform
  could have known it), `observed_at` (when the archive fetched it), and — through its run (`os_runs`, §21.5) —
  `source` (`alpaca_trading` | `alpaca_data` | `fred`), `feed` (`indicative` | null), `model_version` (`raw` for
  archived values; OS2 sets its own, e.g. `gex-1`) and `assumptions_hash` (sha256 of the canonical JSON of filter +
  cadence for raw values; of the model assumptions — dealer positioning, rates, dividends — for computed values).
  All times are integer epoch milliseconds UTC in storage.
- **Open interest:** `valid_for` = `open_interest_date` (trading day T); `available_at` = `observed_at` of the fetch
  that returned it, and never earlier than **T+1 08:00 New York** (T+1 = the next trading day; publication on the
  morning of T+1 **confirmed by the OS0 run 20261024T114530Z**; the exact 08:00 time is watched by the archive's
  `early_oi` flag in its first OI runs) — an earlier fetch is flagged
  `early_oi` and not used before that time. **Staleness:** OI counts for a decision only if `valid_for` == the
  PREVIOUS trading session of the decision day; otherwise the dependent value is `unavailable` + an `os_gaps` row.
  A corrected OI for the same `valid_for` is a NEW row with a later `available_at`; readers take the row with the
  latest `available_at` ≤ decision time.
- **Chain snapshot:** `valid_for` = `latestQuote.t` per contract (the slot is kept as `slot`); `available_at` =
  `observed_at` (receive time). **Staleness (PROVISIONAL):** a quote counts only if its age at the SLOT time is
  ≤ 5 min (and it is not after the slot, §21.2); otherwise that contract is `unavailable` + an `os_gaps` row.
  Indicative trades are 15 min delayed: a trade-based value gets `available_at` ≥ trade time + 15 min.
- **Coverage (PROVISIONAL):** GEX (and every aggregate over the chain) is `unavailable` when fewer than **80 %** of the
  in-filter contracts have a usable quote + greeks + OI at the slot.
- **Bars (approximation backtest only; the bars endpoint has no feed parameter):** the rule is based on the
  `backfill` flag only — rows fetched from history after the fact are `backfill = 1`, labelled exploratory, and never
  count for H1/H2; `available_at` = bar end for live-collected bars.
- **FRED:** `valid_for` = the observation date; `available_at` = `observed_at` (FRED publishes with a lag; ALFRED
  vintages are not used in Phase A).
- **Readers** (computations, backtests, the regime service, the evaluation of H1/H2) use ONLY rows with
  `available_at` ≤ decision time; a missing value makes the dependent result `unavailable` with the reason.
- **Gaps** (a slot not collected or not finished in time, a page failed, a field missing, a stale quote / OI) are
  recorded in `os_gaps` and shown — NEVER filled with stand-ins (no interpolation, no previous value, no second source).

### 21.4 Archive service (OS1) — **built 2026-10-24** (details §21.11)

- Its own background process, started by a **Windows scheduled task** (independent of the UFOS.ai app and backend):
  data collection only — no trading, no AI, no broker writes. Code inside `HedgeFund/` (scope guard); its own
  security review in OS1.
- Keys only from the Windows Credential Manager (`alpaca-paper-key-id` / `alpaca-paper-secret-key`, `fred-api-key`
  via `strategy_platform/secrets.py`), read per run, never written anywhere else, never logged.
- **No listening port** (no HTTP server; outgoing GETs only on the §21.1 allow-list — the same allow-list pattern as
  the OS0 tool, paper host from `live_guard.paper_endpoint`, redirects not followed, TLS via `core/tls.py`); its own
  Alpaca gate of 30 req/min (§21.1).
- Own log (`data/logs/options_archive.log`, rotated, redacted) + a **gap report** (per day: expected vs collected slots,
  missing pages / fields, stale quotes / OI) and a status file the backend reads.
- Status, start / stop and the gap report in **⚙ Keys & data → Data sources**; the scheduled task is registered only
  after the user confirms it there — never silently. **Confirmation style (decision 2026-10-23, replaces "typed
  confirmation"; typed words stay only for the live-trading switch):** ONE confirmation dialog showing a precise
  summary — what runs (the archive process, GET-only, no trading), when (at logon of the current user, sleeps until
  the next NY slot), which account / keys (the current Windows user; Alpaca PAPER key pair + `fred-api-key` from the
  Credential Manager, read per run), where data goes (`data/options_archive.db`, status file, log) and how to stop
  (Unregister… / Task Scheduler / the stop file) — plus an audit entry (`options_archive_registered` /
  `…_register_failed` / `…_unregistered` / …, `POST /v2/options-archive/audit`).
- **Registration mechanics (lead decisions 2026-10-24, review M1 + Round 2b):** no PowerShell and no script text from
  the backend. The backend supplies DATA only (paths + the summary, `GET /v2/options-archive/registration`); the WPF
  builds the task XML from a FIXED template and runs `%SystemRoot%\System32\schtasks.exe` as the current user: task
  **`UFOS.ai OptionsArchive` in the ROOT task folder** (a standard user may not create a subfolder), `LogonTrigger` of
  the current user, `InteractiveToken`, `LeastPrivilege`, `MultipleInstancesPolicy IgnoreNew`, `StartWhenAvailable
  false`, no time limit, `RestartOnFailure` 3× / 5 min, action = the venv's `pythonw.exe "<backend>\tools\
  options_archive.py" service`, working directory = the backend dir; `/Create … /XML … /F` then `/Run` (it starts
  right away — the summary says so); unregister = `/End` + `/Delete`. No admin rights, no stored password.
- **Sandbox mode** (backend marker `data/SANDBOX_ACTIVE`): registering and manual runs are disabled (status
  `actions_enabled: false`, registration `available: false`, audit of register / run-once actions → 409 `sandbox`,
  CLI `run-once` / `run-slot` → exit 6); unregister stays possible; an already registered service keeps collecting
  (it only writes its own `options_archive.db`).
- Own SQLite file `HedgeFund/backend/data/options_archive.db` (WAL; the archive is the only writer; the backend opens it
  read-only), own schema version, insert-only like `hedgefund.db`; retention: raw snapshots kept (no deletion in Phase
  A; size from §21.9 `e_storage`).

### 21.5 Tables (OS1 migration `strategy_platform/options_archive/db.py`, schema version 1 — **built 2026-10-24**)

All tables insert-only (triggers); times are integer epoch ms UTC; run-constant columns live in `os_runs` only; a null
is a recorded gap, never a default. Option `type` / `strike` / `expiration` are derived from the OCC symbol (and
stored once in `os_contracts`).

| Table | Columns | Key |
|---|---|---|
| `os_runs` | `id`, `kind` (`oi` / `eod` / `intraday` / `fred`), `slot_ms` (planned), `started_ms`, `finished_ms`, `status` (`ok` / `partial` / `failed` / `late`), `requests`, `pages`, `error`, `source`, `feed`, `spot_source` (e.g. `alpaca_iex_trade`), `model_version`, `assumptions_hash` | `id`; unique (`kind`, `slot_ms`) |
| `os_contracts` | `symbol` (OCC), `underlying`, `type`, `strike`, `expiration`, `style`, `size`, `first_seen_ms` | (`symbol`, `first_seen_ms`) — a changed attribute is a new row |
| `os_contract_sightings` | `run_id`, `symbol`, `seen_ms` (replaces a mutable `last_seen`) | (`run_id`, `symbol`) |
| `os_open_interest` | `run_id`, `symbol`, `open_interest`, `valid_for` (T, date), `available_ms`, `observed_ms`, `early_oi` | (`symbol`, `valid_for`, `available_ms`) — a correction is a new row with a later `available_ms` |
| `os_chain_snapshots` | `run_id`, `symbol`, `spot`, `spot_t_ms`, `bid`, `ask`, `bid_size`, `ask_size`, `quote_t_ms` (= `valid_for`), `last`, `last_t_ms`, `daily_volume` (`dailyBar.v`), `iv`, `delta`, `gamma`, `theta`, `vega`, `rho`, `available_ms`, `observed_ms` | (`run_id`, `symbol`) — the slot comes from the run |
| `os_bars` | `symbol`, `timeframe`, `t_ms` (= `valid_for`), `o`, `h`, `l`, `c`, `v`, `n`, `vw`, `available_ms`, `observed_ms`, `backfill` | (`symbol`, `timeframe`, `t_ms`) |
| `os_fred` | `series_id`, `date` (= `valid_for`), `value`, `available_ms`, `observed_ms` | (`series_id`, `date`, `available_ms`) |
| `os_gaps` | `id`, `run_id`, `slot_ms`, `kind`, `underlying`, `symbol`, `what`, `reason`, `recorded_ms` | `id` |

Storage estimate per row (incl. SQLite record header and the primary-key index): snapshot ≈ 260 B, OI ≈ 125 B — the
check's `e_storage` uses these.

As built (differences to the plan above): `os_runs` also has `slot_label` (`09:55`, `oi`, `eod`, …), `retries`,
`app_version`, kind `manual` (a "run one snapshot now" run — never counts for a slot) and an index (`slot_ms`, `kind`)
instead of a UNIQUE key (insert-only: a re-run is a new run; the gap report takes the best run per slot), and
`skew_ms` (server `Date` − PC clock of the run; review m8). `os_open_interest` is keyed by (`symbol`, `valid_for`,
`observed_ms`) — two observations never collapse (two pre-floor fetches share `available_ms` = the floor; readers take
the latest `available_ms` ≤ t, ties → the latest `observed_ms`). `PRAGMA recursive_triggers=ON`, so even the implicit
delete of an `INSERT OR REPLACE` aborts. Two extra
tables: `os_schema` (`version`, `applied_ms`; plus `PRAGMA user_version`) and `os_configs` (`hash`, `json`,
`first_ms` — the canonical config behind each run's `assumptions_hash`). `os_open_interest.early_oi` (0/1). Every
table has `BEFORE UPDATE` / `BEFORE DELETE` triggers that abort. `os_bars` is created but not filled by OS1 (bars are an
OS2 / backtest concern).

### 21.6 Data flow and point-in-time timeline

```
 Alpaca trading API ─ contracts + OI (08:30 NY) ─┐
 Alpaca data API ─ chain snapshots (slots, RTH) ─┼─► OS1 archive service ──► options_archive.db ──► OS2 computations
 FRED ─ VIXCLS / VXVCLS / VXNCLS (daily) ────────┘   (scheduled task,        (raw, PIT columns,      (IV, greeks, GEX ×
                                                       GET only, no port,      os_gaps)                2–3 dealer variants,
                                                       own 30 req/min gate)                            flip, walls, skew,
                                                                                                         expected move)
                                                                                                            │
                     consumers ◄── regime service (available_at ≤ decision time) ◄──────────────────────────┘
       (DSL operators OS4 · allocator · risk checks · Explain · decay monitor · Structure tab / overlays OS3 ·
        H1/H2 evaluation after ≥ 120 sessions)
```

```
 day T                                   │ day T+1
 09:30 open … 15:25 slot … 16:00 close   │ 08:00 NY (confirmed, OS0): OI of T published → available_at(OI_T) ≥ T+1 08:00
 slot s: the snapshot finishes by s; valid_for = quote time ≤ s; age at s ≤ 5 min; available_at = receive time ≤ s
 decision at 15:30 on T+1 may use: OI valid_for = T (the previous session, available since T+1 08:00) + the 15:25
 snapshot of T+1;  decision at 15:30 on T may NOT use OI of T (not available before T+1 08:00)
```

### 21.7 Robustness and evaluation rules (from the pre-registration)

- GEX is computed under 2–3 dealer-positioning assumptions in parallel (plan §6); a signal counts only if it holds
  under all of them.
- **No running results** of H1/H2 before the evaluation date — the UI shows only a progress counter (sessions archived,
  data health). The approximation backtest from 2024-02 is reported but labelled exploratory and does not count.

### 21.8 Frozen pre-registration

- File: **`HedgeFund/docs/preregistration/OS-H1-H2-2026-10-23.md`** (the folder `HedgeFund/docs/` was created for it),
  frozen **2026-10-23**, before the first archived day; user approval 2026-10-22 "Ja, so einfrieren".
- **sha256 `95d3ac66736ad358df23b4628f0c32425f4b0b4be37138f7b52a813afc3b8195`** (2 198 bytes, UTF-8, LF line
  endings; `HedgeFund/.gitattributes` marks `docs/preregistration/**` as `-text` so no CRLF conversion changes the
  bytes). The hypothesis block is the plan's §7 text verbatim; the header adds status, approval, the rules and the
  footnote for "§6".
- `tests/test_os_preregistration.py` recomputes the sha256 and fails on any change. Rule: the file is never edited;
  changes only as NEW, separately dated hypothesis files (their own sha256 recorded here). Implementation rules that
  make the hypotheses measurable (§21.2 slot rule, §21.3 staleness) live here, not in the frozen file.

### 21.9 OS0 read-only check — `tools/verify_alpaca_options.py`

- Usage (user, PowerShell, from `HedgeFund\backend`; once during US market hours and once after the close — the tool
  reads the Alpaca market clock itself): `.\.venv\Scripts\python.exe tools\verify_alpaca_options.py`
- GET only on an allow-list of (host, path): the paper host from `live_guard.paper_endpoint("alpaca_paper")`
  `/v2/options/contracts`, `/v2/clock`; `data.alpaca.markets` `/v1beta1/options/snapshots/(SPY|QQQ)`,
  `/v1beta1/options/bars`, `/v2/stocks/(SPY|QQQ)/trades/latest`; `api.stlouisfed.org` `/fred/series`,
  `/fred/series/observations`. Anything else is refused before sending; https only, default port; redirects not
  followed (a 3xx comes back as that status); TLS `core/tls.py`. Since OS1 the allow-list, opener, body cap,
  retry and sanitizing live in the shared module `strategy_platform/options_archive/transport.py` (one copy for the
  tool and the archive); the tool keeps its own settings (timeouts, page size, pauses) in a thin `Http` subclass.
- Timeouts per endpoint: 60 s for contracts / snapshots / bars, 20 s for the clock, the latest trade and FRED.
- An answer > 20 MB stops the run (never truncated). 0.5 s between pages, ≤ 40 pages per query, contracts
  `limit=1000` per page. One progress line per page ("contracts SPY: page 3 — 3000 rows so far").
- The first HTTP 429 stops the run.
- Network errors (os0 add-on, user runs 2026-10-23: a 10 000-contract page timed out at 20 s; "URLError" alone was
  not diagnosable):
  - After a timeout or connection reset, ONE bounded retry follows, 2 s later. It is counted in the request
    budget and listed in the report as `requests.retried` [{endpoint, path, reason}]; the second failure stops the
    run ("(after 1 retry)").
  - Every other network error stops at once.
  - The reason is sanitized: `Type: message` of the root cause (e.g. gaierror, SSLCertVerificationError,
    ConnectionRefusedError, TimeoutError), with URLs, query strings and key=… redacted, ≤ 200 characters.
    Non-network exceptions are reported by type only.
- The clock comes first: 401/403 → stop (exit 2, nothing else requested); clock unreadable → stop (exit 3; no date is
  assumed — no UTC fallback). The New York date from the clock bounds every query.
- Checks a–f (PASS / FAIL / PARTIAL + facts): a contracts — the window today … +60 d AND the full list (explicit far
  bound), each with contracts, expiries, malformed `expiration_date` rows, OI + OI-date share, pages; b snapshots
  (explicit `feed=indicative`, explicit expiry bounds; greeks / IV share, quote ages vs the snapshot time — signed,
  never clamped; quotes after the snapshot time and older than 5 min counted; latency, pages, filter grid counts);
  c bars (ATM call of the nearest expiry ≥ 7 days: 1Day / 1Min — no ATM contract is a FAIL; history probes
  `SPY240119C00480000` (before 2024-02) and `SPY240216C00500000` → earliest bar); d budget (EOD / intraday requests,
  minutes and RTH share at the archive's 30 req/min, next to the backend's 150 + burst 10 and Alpaca's 200);
  e storage (rows × bytes, §21.5); f FRED (exists / "not on FRED" / error with FRED's `error_message`; a Credential
  Manager read error is reported as such, never as "no key").
- A data error inside a check (ValueError / TypeError / KeyError / AttributeError) makes THAT check FAIL with the
  error (type + message) — the run continues. Network errors (sanitized reason, see above — a FRED URL carries the
  key), a refused request, an oversized answer or a 429 stop the run: "RESULT: STOPPED", every check that did not run is
  FAIL "not run".
- Keys: the Alpaca paper pair + `fred-api-key` from the Credential Manager (`secrets.get_secret`), never printed,
  logged or written; the JSON report is scrubbed for them before saving:
  `HedgeFund/backend/data/os0/verify_alpaca_options_<UTC>.json` (git-ignored: `data/os0/*` in `backend/.gitignore`).
- Exit codes: 0 no FAIL · 1 a FAIL · 2 Alpaca keys missing / unreadable / rejected · 3 stopped (network, clock, refused
  request, oversized answer) · 4 stopped on HTTP 429.
- Tests (fakes, no network; one loopback HTTP server for the no-redirect opener): `tests/test_os_verify_alpaca_options.py`
  (27: allow-list + GET only, explicit expiry bounds against a fake with Alpaca's next-weekend default, explicit feed,
  ATM found and 1Day / 1Min measured, no key material in output / report — also not from an exception text with a
  FRED URL, paging + pauses, 429 / timeout / clock / keys-rejected stops with the right exit codes and "not run"
  checks, malformed `expiration_date`, a data error = FAIL not stop, signed quote ages, missing fields → PARTIAL with
  0 %, market closed, FRED errors vs missing series vs key read error, no-redirect + size limit, budget / storage
  arithmetic, the backend gate = the split, retry once after a timeout then stop, no retry for other network errors,
  sanitized reasons, progress lines, page size 1 000), `tests/test_os_preregistration.py` (2).

### 21.10 OS0 results (the user's run 2026-10-24) and what is still open

- **Run `data/os0/verify_alpaca_options_20261024T114530Z.json`** (Saturday, market closed; no FAIL): SPY 5 542
  contracts in the 60-day window (12 746 in the full list), QQQ 5 394 (10 172); OI present 72–79 %,
  `open_interest_date` 2026-10-22 everywhere (OI of T published the morning of the next trading day); greeks / IV
  present 90–94 % (indicative); option bars back to 2024-01-18; budget 7 requests per intraday snapshot, 19 per EOD
  snapshot; FRED: VIXCLS / VXVCLS / VXNCLS exist, VXSTCLS / VIX9DCLS do not.
- **Confirmed 2026-10-24** (user decision 13:49; set as the defaults in `options_archive/config.py`, no override
  file; `CONFIRMED` there): filter ±10 % / ≤ 45 days, cadence 28 snapshots per session (≈ 11.5 GB / year accepted),
  the meaning of `open_interest_date` + the T+1 08:00 floor, the FRED series.
- **Still PROVISIONAL until the RTH run on Monday 2026-10-26 (14:30–21:00 local):** the quote-staleness threshold
  (5 min at the slot) and the coverage minimum (80 %) — a Saturday run cannot measure quote freshness
  (`PROVISIONAL_KEYS` in `config.py`; the status payload's `config.provisional_keys`).

### 21.11 OS1 archive — as built (2026-10-24, review round 2 included)

Status: **built — awaiting the OS0 data-check numbers (§21.10) + the user's registration in ⚙ Keys & data**. Nothing
has been registered or run against Alpaca / FRED (build machine: no network, no keys); the real `data/*.db` were not
touched — all tests use temp dirs and a fake transport.

- **Package `HedgeFund/backend/strategy_platform/options_archive/`** (under strategy_platform: it reuses the NYSE
  calendar `core/calendars.py`, `core/tls.py`, `live_guard`, `secrets.py`; a top-level `options_archive` package would be
  shadowed by the CLI `tools/options_archive.py`): `config.py` (ALL provisional numbers), `transport.py` (shared with the
  OS0 tool), `db.py` (migration §21.5), `slots.py` (NY slots), `collector.py` (one run = one slot), `report.py` (gap
  report / status payloads), `service.py` (process loop, lock, log), `registration.py` (registration DATA).
- **Config** (`config.py`, `VERSION os1-1`, `provisional: true` while any key is provisional; `CONFIRMED` /
  `PROVISIONAL_KEYS` / `key_status()` — kept out of the hashed config — say which, also in the status payload's
  `config.confirmed_keys` / `provisional_keys`): symbols SPY/QQQ, strike band ±10 %, expiries 0…45 d,
  cadence 15 min 09:45…15:45, fixed slots 09:55 / 15:25, EOD close + 15 min, OI 08:30, FRED 08:45 (VIXCLS, VXVCLS,
  VXNCLS; last 10 observations), slot lead 90 s, quote max age 300 s, coverage 80 %, OI floor 08:00 NY, own gate
  30 req/min, page size 1 000, 0.5 s between pages, ≤ 40 pages, ONE retry 2 s later, timeouts contracts 30 s /
  snapshots 20 s / stock 10 s / FRED 20 s, EOD grace 45 min, heartbeat 60 s. Override: `data/options_archive_config.json`
  with any subset of these keys. Validation (review M2): every number has a range (`RANGES`, e.g. cadence 5…390 min,
  retries 0…1, max_pages 1…100, timeouts 1…120 s, heartbeat 10…120 s), finite numbers only (NaN / Infinity / 1e999
  refused), whole numbers where the default is one, symbols ⊆ {SPY, QQQ}, HH:MM 00:00…23:59, `oi_slot` ≥ `oi_floor`,
  `oi_slot` / `fred_slot` before `intraday_first`. A bad override is REFUSED with the reason (never clamped): the archive
  does not start (exit 3), the backend routes answer 409 `config_invalid`. `assumptions_hash` = sha256 of the canonical
  config JSON (stored once in `os_configs`, per run in `os_runs`).
- **Slots** (`slots.py`): per NY trading day `oi` 08:30, `fred` 08:45, 25 intraday (09:45…15:45 every 15 min) + 09:55 +
  15:25, `eod` 16:15 = **30 slots** (early close: §21.2 — intraday 09:45…12:45 + 09:55, no 15:25, EOD 13:15). The
  slot loop is bounded (cadence < 1 raises). **Windows** — a slot is collected AS THAT SLOT only inside its window:
  chain slots slot − 90 s … slot; `eod` slot − 90 s … slot + 45 min; `oi` / `fred` from the slot until the close of
  THAT session. Outside → `missing` (never run later under the old slot, also not after the PC slept).
- **Collector** (`collector.py`): chain slot = spot (the latest IEX trade fetched at the run start, i.e. before the
  slot; its time stored as `spot_t_ms`, not age-checked) → snapshots with explicit `feed=indicative`,
  `expiration_date_gte/lte`, `strike_price_gte/lte` (±band around that spot), `limit`; every quote is checked at the
  SLOT time: after the slot → gap `after_slot`; older than 300 s → `stale_quote`; missing quote / greeks / IV →
  `field_missing`; usable share < 80 % → `coverage` (quote + greeks + IV only — OI is joined by the OS2 computation, which
  applies its own coverage rule); a failed page → `page_failed`; finished after the slot → run `late`. The row is still
  stored with its own times (nothing replaced). `oi` slot = contracts with explicit expiry bounds and NO strike filter
  (all strikes of the expiry window — the 08:30 run has no trustworthy pre-market spot) → `os_contracts` (new row only
  when an attribute changes), `os_contract_sightings`, `os_open_interest` (new row only when new / changed;
  `available_ms` = max(observed, T+1 08:00 floor), `early_oi`); a contract listed twice with different OI in one answer
  → gap `oi_conflict` (first value kept); `stale_oi` gap when `valid_for` ≠ the previous trading session. `fred` =
  observations (desc, last 10) → `os_fred` (new row only for a new or revised value; "." → null + gap), `not_on_fred` /
  `fred_error` gaps. **Clock** (review m8): every answer's `Date` header vs this PC's clock → `os_runs.skew_ms`; > 30 s a
  `clock_skew` gap, > 5 min the run stops (exit 3, nothing stored). One SQLite transaction per run. Gap rows are
  per (underlying, kind) with the count in the text (no per-contract rows, no example symbols).
- **HTTP** (`transport.py`): allow-list = §21.9; own sliding-window gate (≤ 30 in ANY 60 s); every attempt counted;
  ONE retry after a timeout / connection reset / HTTP 5xx (recorded in `os_runs.retries`), then the run stops
  (`failed`, sanitized reason); HTTP 429 → stop at once (no retry; the service waits for the next slot); 401/403 →
  keys rejected (exit 2).
- **Process** (`service.py`, `tools/options_archive.py service`): single instance (lock file `data/options_archive.lock`,
  OS lock — released by the OS when the process dies; a second archive process → exit 5, also `run-once` while the
  service runs); sleeps in ≤ 30 s steps until the next slot start; heartbeat ≥ every 60 s in `data/os_archive_status.json`
  — also between the pages of a long run; stops cleanly on Ctrl+C, the scheduled task's End, or the file
  `data/options_archive.stop`; **no listening port**. Resilience (review M3): an unexpected error in one run is logged
  with its traceback (redacted) and recorded as a `failed` run + `crash` gap — the service continues; status-file
  writes retry (Windows `PermissionError` on replace) and never raise; a fatal loop error → state `error` with a
  sanitized `last_error` and exit 7 (the task's RestartOnFailure restarts it). The calendar's end (no data for the
  coming days) → state `error` with the message, retry hourly (no guessed trading days). Log
  `data/logs/options_archive.log` (2 MB × 3, rotated, no propagation to other handlers) with a redaction filter for
  every key read in the process and `api_key=` / `APCA-*` header values.
- **CLI** `tools\options_archive.py run-once | run-slot <HH:MM|oi|eod|fred> | status | gaps [--date YYYY-MM-DD] |
  service`; exit codes 0 ok · 1 partial / failed run · 2 keys missing / unreadable / rejected · 3 stopped (network,
  clock, config, calendar) · 4 HTTP 429 · 5 another archive process holds the lock · 6 refused in sandbox mode · 7
  internal error. `run-once` = today's catch-up (windows above) + one `manual` chain snapshot of both symbols;
  `run-slot` outside the slot's window → a `manual:<slot>` run (kind `manual`, never ranked as the slot's run). Both
  write ONLY the `manual` block of the status file (finished_at, status, run_ids, exit_code, error) — never the
  service's state or heartbeat (review m11).
- **Registration** (`registration.py`; DATA only, §21.4): task name `UFOS.ai OptionsArchive` (root folder),
  `backend_dir`, `workdir`, `pythonw` (null when missing → `available: false`, no console fallback), `python`, `tool`,
  `service_args`, `run_once_args`, `available` / `unavailable_reason` / `sandbox`, and `summary` (what / when — "starts
  right after you confirm and at every logon" / account / keys / data / stop — what Unregister, Task Scheduler End and
  the stop file do). The XML template and the schtasks calls are fixed in the WPF.
- **Backend API** (read-only, `strategy_platform/api/options_archive_routes.py`, behind api_auth; the DB is opened
  `mode=ro`; works when the archive never ran → `state: not_set_up`): `GET /v2/options-archive/status` (state
  not_set_up / ok / degraded / error, db, service heartbeat — alive = ≤ 180 s, `manual` block, last run (with
  `skew_ms`), next slot, today's expected / collected / partial / late / failed / missing / pending slots and
  `fixed_slot_gaps` (a late 09:55 / 15:25 counts as a gap), counts, last error sanitized, config, `sandbox` /
  `actions_enabled` / `actions_note`, paths), `GET /v2/options-archive/gaps?date=` or `from&to` (≤ 31 days inclusive;
  years 2000…2100, else 400; per day slots × status + gap rows ≤ 200; a day beyond the calendar shows `error`),
  `GET /v2/options-archive/runs?limit=1…200`, `GET /v2/options-archive/registration`, `POST /v2/options-archive/audit`
  `{action ∈ registered | register_failed | unregistered | unregister_failed | run_once | run_once_failed, detail ≤ 300}`
  → `hedgefund.db` audit `options_archive_<action>` (the only write; sandbox: register / run-once actions → 409).
  Gap kinds: `after_slot`, `stale_quote`, `field_missing`, `coverage`, `page_failed`, `stale_oi`, `oi_conflict`, `late`,
  `not_collected`, `network`, `rate_limited`, `keys`, `refused`, `not_on_fred`, `fred_error`, `clock_skew`, `crash`.
  Field-level payloads: `options_archive/report.py` (`status_payload`, `day_report`, `run_view`) and
  `registration.payload`.
- **Tests** (no network, temp dirs, fake Alpaca / FRED with a `Date` header, fake clock): `tests/test_os_archive_db_slots.py`
  (7: migration + insert-only triggers on every table, a newer schema refused, config override, 30 slots, early close
  2026-11-27 + Thanksgiving, DST week 2026-10-25 / 11-01, next slot), `tests/test_os_archive_collector.py` (15: chain PIT
  stamping + explicit parameters, staleness / after-slot / field / coverage gaps with rows kept, late snapshot, no spot,
  OI floor + corrections + stale / early OI, FRED, retry once then stop, 5xx retried once, keys rejected → exit 2, 429,
  30 / min gate, allow-list, no key material), `tests/test_os_archive_service.py` (11: loop + catch-up + missing slots,
  stop file, early-close day, US DST change in the loop, lock, CLI, API routes, registration DATA, audit),
  `tests/test_os_archive_round2.py` (26: config ranges incl. cadence 0 / NaN / Infinity, CLI refuses a bad config,
  bounded slot loop, INSERT OR REPLACE cannot delete, OI pre-floor correction + duplicate conflict, clock skew ok / gap /
  stop / no header, crashing run recorded + loop continues + redacted traceback, status write failure, fatal error →
  exit 7, heartbeat during a long run, slot windows, missed morning slots after sleep, run-slot outside the window →
  manual, calendar end in status + hourly retry, run-once writes only the manual block, sandbox exit 6 + 409, internal
  error exit 7, late fixed slot = gap, /gaps 31 days inclusive + bad dates 400).
- **Not in OS1:** the WPF card (other agent, against the same contract); option bars (`os_bars` empty; OS2 /
  backtest); any computation (OS2); confirming the provisional numbers (the user's OS0 run); the early-close rule
  (pending the user's confirmation, §21.2).


## 22. Stage 10 — Council of AIs migration (CONTRACT rev. 3 — approved for implementation (review 2026-10-24); batch S10-C; D1–D4, D7, D12 confirmed by the user; module moves M1–M6 DONE; R1 built 2026-10-24; deliberate differences approved 2026-10-24 17:59; R2 (backend) built 2026-10-24 — the real switch-over only by the user in the app — as-built §22.13 / §22.14)

Basis: SP §15 ("Stage 10 — the Council of AIs is migrated into the new platform as a strategy (parity test against the
old engine), then a SAFE and CAREFUL cleanup"), the approved deletion list `HedgeFund/docs/STAGE10_DELETION_LIST.md`
(A1 / A4 / B rows, all "after the Council migration"), FUND SPEC §7–§10 (agents, judge, sizing, engine) and SP §17.9
(the old paper account already lives in `paper-council`, source `council-of-ais`, positions HELD). Rev. 2 answered the
independent contract review of 2026-10-24 (2 critical, 9 major), rev. 3 the re-review (N1–N7 + notes); lead decisions
are marked **[lead]**.

| # | Decision | Status |
|---|---|---|
| D1 | `council` is a non-DSL strategy kind, exempt from G1–G8 / holdout / incubation; labelled **"not validated — AI strategy"** everywhere (§22.9) | confirmed by the user 2026-10-24 14:04 |
| D2 | Intraday cadence + "Run now"; the old crypto risk watch is dropped | confirmed by the user — subject to the review verifying the runtime coverage table in §22.6 |
| D3 | The latest old `fund_params` version is copied ONCE into hedgefund.db (`cl_params`), key by key per §22.4, at R2 in the switch-over transaction | confirmed by the user |
| D4 | Old-engine history: a read-only JSONL export before R4; no read-only history view | confirmed by the user ("Ein Export reicht") |
| D5 | The old runs / recommendations / signals are ALSO imported once into `cl_*` with `legacy = 1` at R2, in the switch-over transaction (cold start of the change detection + Explain of the held positions), in addition to the D4 JSONL export | **[lead]** decided 2026-10-24 |
| D6 | The WPF kill switch / "Stop engine…" move to `/v2/oms/kill` BEFORE R2; the pinned Resume becomes a dialog over the halted platform accounts + the old engine BEFORE R2 (§22.8); R2 and R3 ship in ONE build; after R2 `/fund/kill` and `/fund/resume` are audited aliases of the platform routes (never 410) until R4 removes them | **[lead]** (review C1, re-review N4) |
| D7 | **Option B — platform standard:** outside the equity trading window and during the opening grace the Council only ANALYSES equities — the decision is recorded with "market closed — no order placed" (no queueing, no special check path); scheduled cycles run only inside the session, the first one after the opening grace; crypto unaffected (24/7) — §22.5 | confirmed by the user 2026-10-24 14:27 (review M1); a deliberate difference vs the old engine (§22.10) |
| D8 | "Run now" reports `orders` per class `{"equity": bool, "crypto": bool}`; an exhausted AI budget behaves exactly like the old D9 — the cycle runs, the affected symbols get no AI judgement (recorded reason), carried recommendations are still sized / executed as before — for scheduled cycles and "Run now" alike; no `503 budget` | **[lead]** (re-review N5) |
| D9 | `paper-council`'s broker params are editable through an audited, versioned, insert-only override (`cl_broker_params`, Council view → params editor, the same bounds as `/fund/params` today); the risk agent's 75 % drawdown rule reads `pf_settings.drawdown_kill_pct` | **[lead]** (re-review N6) |
| D10 | One per-account trade lock shared by the Council order step, the runtime execution step and `control.kill_account` — no non-protective order after a kill | **[lead]** (re-review N7) |
| D11 | Stale Council crypto orders expire after a fixed **60 min** (the old value) at ANY time, as OMS `accepted → expired` transitions — a deliberate difference (§22.10) | **[lead]** |
| D12 | The §17.9 migration is removed at R4: `execution/migration.py`, the `/v2/portfolio/migration*` routes and the WPF migration dialog (renamed to `*.stale` first, deleted in the R4 / R5 deletion step; R2 requires the migration done, so it has no further use) — row added to the deletion list | approved by the user 2026-10-24 14:42 ("Ja") |

### 22.1 The Council as a platform strategy

- **Kind `council`, not a DSL spec (D1).** The Council calls an LLM (sentiment + judge); an LLM cannot be backtested
  without look-ahead, so it is NOT a StrategySpec and never goes through G1–G8 / holdout / incubation. Its rule part
  stays the backtestable library entry "Council Rules (no AI)" (`agentic_committee_rules`, §7.9) — unchanged.
- **Code:** new package `strategy_platform/council/` — `agents/{technical,risk,sentiment}.py` (moved from
  `fund/agents/`), the macro agent from `strategy/features/macro_agent.py` (moved there in M6, §22.7), `judge.py`
  (rule score, rule suggestion with hysteresis, change detection, batch LLM judge, model modes incl.
  `premium_on_flip`), `sizing.py`, `service.py` (one council cycle + the task, §22.5), `store.py` (§22.3),
  `params.py` (§22.4). The decision logic is MOVED unchanged (FUND §7–§9 stay the behavioural contract); only inputs /
  outputs are re-wired. No rule-based stand-in (FUND D9): a symbol whose judgement is unavailable gets NO new target —
  its position is HELD and the reason recorded.
- **Where it trades:** only `paper-council`, source `council-of-ais`; never another account, no broker routing in
  stage 10. **Sizing base** = the `council-of-ais` sub-ledger equity (§17.5); precondition (checked at R2 and at every
  cycle): `paper-council`'s `_unallocated` source holds 0 cash and 0 positions — else the cycle records `partial`
  "paper-council has unallocated capital — the Council sizes only on its own sub-ledger" and places no orders.
- **Trade counting** (`max_trades_per_day`): Council orders (origin `council`) that reached the OMS as submitted /
  accepted / partially filled / filled per NY trading day (equities) or UTC day (crypto); rejected-by-check and
  cancelled-before-submit orders do not count; a reversal = 2 orders (FUND §9 rule "only the close fits today").

### 22.2 AI budget (review C2)

- **Limit:** `cl_params.ai_daily_budget_usd` (copied at R2). The budget key stays `fund` (label "Council of AIs"),
  the cost-log modules stay `fund-sentiment` / `fund-judge`; `ai_budgets.FundSource` reads the limit from `cl_params`
  and the spend from the counter below from R2 on (until R2 it reads the old engine as today).
- **ONE spend counter** — hedgefund.db meta `council.ai_spend` `{day (UTC), usd}`, written in the same transaction as
  the call's `cl_ai_calls` row. **Seeded at R2** in the switch-over transaction with
  `max(old newsroom.db meta fund.ai_spend for today, Σ cost-log fund-sentiment + fund-judge for today)`.
  **Fails closed:** an unreadable counter or cost log = budget exhausted ("Today's AI spend could not be read — AI
  calls are paused …"); pre-call estimate booked for a sent-but-failed call (FUND §8.6 rules unchanged).
- **Exhausted budget (D8):** exactly the old D9 behaviour — the cycle runs; symbols that need a judgement get none
  ("AI budget reached — raise the limit in Settings to continue.", recorded per symbol); carried recommendations are
  sized and executed as before; scheduled cycles and "Run now" alike (no `503 budget`).
- **No double calling in R1:** before R2 the Council makes NO live LLM call (no scheduled cycles, "Run now" answers
  503 `not_active`); LLM calls happen only inside the parity tests with the fake client. From R2 on the old engine is
  stopped (§22.8), so only one caller exists.
- **After a revert** (§22.8) the old engine's counter (newsroom.db meta `fund.ai_spend`, today) is seeded with
  `max(old meta, Σ cost-log fund-sentiment + fund-judge for today)` before the old engine starts again.

### 22.3 Data model (hedgefund.db migration v25, insert-only; review M6b)

Pre-v25 backup (`data/backups/hedgefund.db.pre-v25-<UTC>`, like v23) — part of the rollback (§22.8). All tables have
no-update / no-delete triggers; times UTC ISO; `legacy` = 1 for rows imported from the old engine (D5).

| Table | Columns (key) |
|---|---|
| `cl_params` | `version` PK, `params_json`, `created_at`, `actor`, `reason`, `source` (`copied from fund_params v<N>` / `user` / `legacy fund_params v<N>`), `legacy` (legacy rows are frozen snapshots, never effective) |
| `cl_broker_params` | `version` PK, `params_json` (only the broker keys of §22.4), `created_at`, `actor`, `reason` — the override of `paper-council`'s migrated broker params (D9) |
| `cl_runs` | `id` PK, `old_id` (legacy), `trigger` (`scheduled` / `manual` / `startup`), `started_at`, `params_version`, `session_state` (open / closed / early_close / holiday), `orders_enabled` (0/1 at start), `sandbox` (0/1), `legacy` |
| `cl_run_results` | `run_id` PK, `finished_at`, `status` (ok / partial / error), `judge_mode` (llm / carried / mixed / unavailable / none; legacy also `rules`), `cost_usd`, `summary_json` (`judge_unavailable`, `data_unavailable`, `valuation_paused`, …) |
| `cl_signals` | (`run_id`, `symbol`, `agent`) PK, `score`, `confidence`, `summary`, `details_json`, `source` (rules / llm / cached / unavailable), `ai_generated` (0/1), `provenance_json` (model, provider, prompt version, generated_at — EU AI Act), `legacy` |
| `cl_decisions` | `id` PK, `old_id` (legacy), `run_id`, `symbol`, `rule_score`, `agreement`, `rule_action`, `judge_source` (llm / carried / unavailable; legacy also `rules`), `action`, `confidence`, `rationale`, `model`, `cost_share_usd`, `unavailable_reason`, `target_weight`, `sizing_note`, `ai_generated`, `provenance_json`, `legacy` |
| `cl_positions` | (`run_id`, `symbol`) PK, `qty`, `avg_price`, `mark`, `weight`, `subledger_equity` — the per-symbol state the run decided on |
| `cl_executions` | `id` PK, `decision_id`, `client_order_id` (null when no order), `outcome` (`submitted` / `rejected` / `not_executed` / `skipped`), `reason`, `at` — the SUBMIT-TIME outcome only; the order's later lifecycle (fills, cancel, expiry) is read from the OMS by `client_order_id` |
| `cl_ai_calls` | `id` PK, `run_id`, `feature` (`sentiment` / `judge` / `judge_premium`), `model`, `request_hash`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `status` (`ok` / `error` / `budget` / `no_key` / `invalid`), `provenance_json`, `at` |

Hedgefund.db meta: `council.switchover` (`pending` / `done` / `reverted` + time), `council.orders_enabled`,
`council.ai_spend`, `autotrade.heartbeat_minutes` (§22.4), `council.legacy_hwm` (`{"runs": max old run id,
"recommendations": max old id}` — §22.8a). The stale SPEC note "OS1's archive tables take v25+" (fix
batch fix1023) is struck: the archive has its own file `options_archive.db`.

### 22.4 Params — one source of truth per value (review M3 / M4)

Copied at R2 from the latest old `fund_params` version in the switch-over transaction (D3):

| Old key | New owner | Consumer | Fate |
|---|---|---|---|
| `watchlist`, `min_confidence`, `max_position_pct`, `crypto_max_position_pct`, `max_trades_per_day`, `agent_weights`, `risk_enabled`, `judge_model_mode`, `change_threshold`, `judge_stale_hours`, `min_hold_days`, `ai_daily_budget_usd` | `cl_params` | Council | kept (same limits as `fund/params.LIMITS`) |
| `interval_minutes` | `cl_params` | Council task | kept; bounds **5 … 60, multiples of 5** (an old value that is not a multiple of 5 is rounded UP and the copy row says so) |
| `drawdown_stop_pct` | paper-council post-trade limit (`pf_settings.drawdown_kill_pct`, set by §17.9) | runtime drawdown kill + the Council's risk agent (its "drawdown ≥ 75 % of the stop → ×0.5" rule reads `pf_settings.drawdown_kill_pct`, D9) | dropped from the Council (one source) |
| `allow_shorts`, `not_shortable`, `short_exposure_max_pct` | paper-council broker params (migrated by §17.9) + the `cl_broker_params` override (D9) | broker + the Council's risk agent (reads the effective values) | dropped from the Council params |
| `fees_bps`, `crypto_fees_bps`, margins, borrow rates / overrides, crypto funding / liquidation fee | paper-council broker params + the `cl_broker_params` override (D9; same bounds as `fund/params.LIMITS`; audit-first; effective from the next fill / accrual) | broker | dropped from the Council params |
| `autopilot_heartbeat_minutes` | **platform setting** hedgefund.db meta `autotrade.heartbeat_minutes` (bounds 5 … 240) | `execution/control.py` dead-man | moved; read explicitly; **unreadable → fail closed** (auto trading switched off + alert), never a silent DEFAULT; the legacy read of the fund params is removed at R2; test |
| `closed_interval_minutes`, `crypto_risk_watch` | — | — | dropped (D2) |
| `starting_capital` | — | — | dropped (the account exists) |

### 22.5 The Council task, schedule, execution (review M1 / M2 / M5 / M6)

- **Own task** `council-cycle` (asyncio + worker thread) with its OWN lock — the agents and the LLM calls run OUTSIDE
  `runtime.cycle`. Only the order step enters the runtime / OMS path: `runtime.submit_source_targets(account,
  source, targets, run_id)` takes the **per-account trade lock** (D10) and **re-checks right before each submit**:
  auto trading ON, `council.orders_enabled`, account not halted, kill not active, not sandbox, switch-over `done`; any
  failing → `cl_executions.outcome = not_executed` with the reason.
- **Per-account trade lock (D10):** `control.trade_lock(account_id)` (re-entrant) is held by the Council order step,
  by the runtime execution step and by `control.kill_account` while it halts and liquidates; every non-protective
  submit re-checks the halt inside the lock — no non-protective order can follow a kill. **Lock ordering:** a thread
  never holds two account trade locks; `kill_all` / `kill_platform` and `_cancel_runtime_orders` walk the accounts ONE
  AT A TIME (take, act, release); `oms.cancel_working` takes no trade lock or only its own account's; the runtime's
  drawdown path (`set_autotrade(False)` then `kill_account`, `runtime.py` ~273–274) takes the lock inside
  `kill_account` only, after `set_autotrade` (which takes no trade lock). Tests: a Council submit racing a kill places
  nothing after the kill; a `kill_all` while another account's execution step runs does not deadlock.
- **Schedule (D7):** scheduled cycles run only while the session is open AND after the opening grace: the first
  cycle of a session at open + 5 min grace + 5 s (09:35:05 New York on a regular day); then next = last start +
  `interval_minutes`; startup + 20 s runs a cycle only when that point lies inside the session after the grace (confirmed
  by the re-review: first cycle = open + grace + 5 s); a cycle
  that comes due outside it is not run; missed cycles are never caught up. Early close / holiday / DST from
  `core/calendars.py`. The grace length is the paper broker's `PENDING_FILL_DELAY_MINUTES` (5 min) — one constant.
- **`POST /v2/council/run`** → `202 {"run_id": n, "orders": {"equity": bool, "crypto": bool}}` (D8; `equity` false
  outside the window / in the grace per D7; both false in sandbox or when Council orders / auto trading are off) ·
  `409 {"error": …, "code": "running"}` (a cycle runs) · `503 {"code": "not_active"}` before R2. An exhausted budget
  does NOT refuse the run (D8).
- **Heartbeat:** the Council GET routes do NOT touch the client heartbeat. Today the ONE always-on heartbeat is the
  app-wide `FundNotifications` poll of `GET /fund/events` (every 15 s while UFOS.ai runs and the backend is online —
  FUND §10; `control.last_seen()` = the newer of the platform touch and the old engine's `touch_client`);
  `/v2/oms/runtime` is polled only by Trading → Portfolio (`TradingView.cs` ~404) and the monitoring view's own polls
  stop when the view is hidden. Because `/fund/events` goes away with R2 + R3, the app-wide `MonitoringNotifier` poll of
  `GET /v2/monitoring/alerts` (every 15 s, started once by the Hedge Fund window, kept running while UFOS.ai runs —
  `MonitoringNotifier.cs`) becomes the heartbeat in the R2 + R3 build: `mon_alerts` calls `control.touch()` (today it
  does not); test: with only that poll running the dead-man never fires, without any poll it fires after
  `autotrade.heartbeat_minutes`.
- **Origin `council`** — added in R1 (not in the M moves), each with a test:
  - OMS: `oms.ORIGINS`, the attribution / sub-ledger mapping, client-id prefix `cl-`;
  - broker level: `ORDER_ORIGINS` (`core/types.py` after M1) and the `BROKER_ORIGIN` / `OMS_ORIGIN` maps in
    `execution/brokers/paper.py` (lines ~29–31, used at ~44 / 96 / 138) and `brokers/mirror.py` `BROKER_ORIGIN`;
  - `oms._resend_blocker` re-checks, for origin `council`, the Council switch, the switch-over state and sandbox;
  - `control._cancel_runtime_orders` and the auto-trading-off / dead-man paths cancel queued (unfilled) Council
    orders too (`cancelled: …` with the cause); protective / manual / kill orders stay;
  - the Council switch OFF cancels them likewise;
  - every kill forces `council.orders_enabled` = off — hooked into `control.kill_account` (so the runtime drawdown kill
    does it too; audit `council_orders_off`, cause `kill` / `drawdown`);
  - backend start forces `council.orders_enabled` = off explicitly (audit cause `startup`) — the user turns it on;
  - switch changes are audit-first (refused when the audit cannot be written; turning OFF always works) and turning
    ON is refused while the account is halted (409 `halted`).
- **D7 — equities outside the window / opening grace (decided: option B, platform standard):** a Council run that
  happens off-session (manual "Run now": evening, weekend, holiday) or inside the opening grace computes everything
  (agents, judge, sizing, target weight) and records each equity decision with `cl_executions.outcome =
  not_executed`, reason **"market closed — no order placed"**; no order is queued, there is no special check path; the
  next in-session cycle decides again from fresh data. Crypto symbols are traded 24/7 as in every run.
- **The Council's own working orders (in-session; the old `broker.target` rules, origin `council`):** queued orders
  do not exist under D7, but an order can still be working (a buying-power partial fill, an order waiting for a quote,
  a two-leg reversal whose second leg waits for the first fill). Per symbol, before a new order:
  - a pending protective (kill / drawdown / margin) or manual order → no Council order; `not_executed`
    "protective order pending" / "manual order pending" (the Council never replaces them);
  - a working Council order identical to what would be placed now, the first leg of the same transition, or already
    leading to the target → KEPT, no new order (no cancel / re-submit churn);
  - otherwise → the working Council order is cancelled ("replaced by a newer target — run N") and the target is
    re-planned in the same step; at most ONE working Council order per symbol;
  - a reversal sends the second leg only after the first leg FILLED (in the session that is normally the same cycle);
  - unfilled Council equity orders end with the session (tif `day` → `expired`); Council crypto orders still
    unfilled after **60 min** expire at any time (D11, OMS `accepted → expired`, §22.6).
- **Sandbox (review M6):** no scheduled cycles and no Council orders in sandbox mode; "Run now" runs an ANALYSIS
  (agents + judge, budget applies, `orders: {"equity": false, "crypto": false}`); R2 and R4 refuse in sandbox (409
  `sandbox`).

### 22.6 What the runtime covers instead of the old crypto risk watch (D2 — to be verified by the review)

| Old risk-watch step (outside the session, crypto held) | Platform runtime (§17.3, every 5 min while crypto is held, else 30 min, ALL platform accounts incl. `paper-council`) |
|---|---|
| quotes / mark to market | yes — quotes of held symbols per account |
| accruals (perp funding, borrow, dividends) | yes — before fills |
| pending protective + user orders filled | yes — queued fills |
| perp liquidation / margin check | yes — margin check + OMS sync |
| drawdown stop (may liquidate) | yes — post-trade drawdown kill (`pf_settings`, = the old `drawdown_stop_pct`); waits on stale crypto marks (`stale_marks`), as the old engine did |
| equity snapshot | yes — attribution + snapshots (account and sub-ledgers) |
| cancel queued AUTOPILOT crypto orders older than 2 × the risk-watch interval (default 2 × 30 = 60 min) | **not covered today → added (D11):** Council crypto orders still unfilled after a fixed 60 min expire at ANY time (OMS `accepted → expired`, "stale — re-evaluated at the next run") — a deliberate difference (the old rule ran only in the risk watch) |

### 22.7 Moving the six shared modules FIRST (review M9)

M1–M6 happen BEFORE R1 (the council package is then written on the final paths), each verified by the existing
parity tests (`test_sp_trading_parity`, `test_sp_backtest_parity`) + the full suite. Each step moves the file, switches
EVERY importer (platform, tests, `ai_budgets.py`, `main.py`, `fund/` itself), updates logger names asserted in tests,
and leaves no empty package `__init__` behind:

| Step | From | To |
|---|---|---|
| M1 + M2 (together) | `fund/types.py`, `fund/params.py` | `strategy_platform/core/types.py`, `strategy_platform/core/params.py` |
| M3 | `fund/llm.py` | `strategy_platform/core/llm.py` (budget key / cost modules unchanged) |
| M4 | `fund/market.py` | `strategy_platform/data/market_feed.py` (behaviour unchanged; its NYSE helpers keep using `core/calendars.py` — extended through 2030 in S10-H) |
| M5 | `fund/broker.py` | `strategy_platform/execution/paper_broker.py` |
| M6 | `fund/agents/macro.py` | `strategy_platform/strategy/features/macro_agent.py` (as in the approved deletion list; the Council and `macro_regime.py` import it) |

- **Temporary shims only:** each old path keeps a 3-line shim until R4 —
  `import sys; from strategy_platform.core import params as _m; sys.modules[__name__] = _m` — the SAME module object
  (no second copy of state; monkeypatching keeps working). Docstring "TEMPORARY shim (stage 10 S10-C) — removed with
  fund/ (R4)". No permanent fallback, no try/except import.
- **Guard test** `test_s10_no_new_fund_imports` (AST scan, multi-line imports included): nothing imports `fund`
  except `fund/` itself, `tests/test_fund_*`, the fund test kits, `tests/test_sandbox_profile.py`,
  `tests/test_s10_council_parity.py`, `execution/migration.py` (old-store reader — until R4, when the migration and
  its UI are removed, D12), `main.py` + `ai_budgets.py` until R2, and `LEGACY_TEST_IMPORTS` — named platform tests ×
  named old modules that compare with / drive the old engine until R4 (`test_ai_budgets` fund.engine,
  `test_mr_integrations` fund.store, `test_sp_backtest_parity` fund.store, `test_sp_clicktest_1022` fund.engine,
  `test_sp_risk_controls` fund.store + fund.engine, `test_sp_strategy_eval` fund.indicators, `test_sp_trading_parity`
  fund.store + fund.engine). `NOT_YET_MOVED` tolerates the not-yet-moved shared modules and shrinks with each step.

### 22.8 Switch-over, rollback, removal (review C1 / M8)

- **Before R2 (Agent W, can ship now — D6):** the pinned kill button and "Stop engine…" call `/v2/oms/kill`
  (`control.kill_all` — the platform kill AND the old engine's kill); a WPF static test + a backend test assert that the
  route the pinned button calls returns a kill result. Resume: see the next bullet (`/v2/oms/resume` is per account and
  never touches the old engine).
- **Kill / resume across the switch-over (Agent W's C1 findings, 2026-10-24):**
  - `control.kill_all` (`POST /v2/oms/kill`) returns `{"platform", "legacy", "council"}`. **Before R2:** `legacy` =
    the old engine's kill result passed through INCLUDING its closing-order list (`{"ok", "error", "halted",
    "orders": [...]}` — the list `/fund/kill` used to return, so the kill dialog shows the same information);
    `council` = `{"applicable": false}`. **After R2** (`council.switchover = done`): `legacy` =
    `{"ok": true, "applicable": false, "note": "not applicable — switched over to the Council"}` (never a failure);
    `council` = `{"applicable": true, "ok": bool, "orders_enabled": false, "cancelled_orders": n, "error": null|text}`
    (the Council switch forced off, its working orders cancelled); the `paper-council` positions are closed by the
    PLATFORM half (it is a platform account). The overall result is ok when `platform.ok` and (before R2)
    `legacy.ok` / (after R2) `council.ok`. Tests: a kill after the switch-over reports no failed half; the Council
    switch is off and its working orders are cancelled; a kill before R2 carries the old closing-order list.
  - **Resume BEFORE R2 (re-review N4, Agent W can ship now):** the pinned "Resume" opens a dialog listing the
    halted platform accounts AND the old engine (when halted) → one `POST /v2/oms/resume {"account_id", "reason"?}` per
    chosen account + `POST /fund/resume` for the old engine. **In the R2 + R3 build** the dialog lists the halted
    accounts only (paper-main, paper-council, broker accounts) → one `POST /v2/oms/resume` per chosen account; resuming `paper-council` clears the halt only —
    Council orders stay OFF until the user turns them on (`PUT /v2/council/orders`). After R2 `/fund/kill` and
    `/fund/resume` are NOT 410 (D6): they delegate to `control.kill_all` and to `control.resume("paper-council")`, so an
    older WPF build still stops / resumes correctly. The forwarding routes are audited exactly like the platform ones
    (audit-first; the same actions / details as `/v2/oms/kill` / `/v2/oms/resume`, plus `via: "/fund/kill"` /
    `"/fund/resume"`). They exist only from R2 until R4 and are REMOVED with the rest of `fund/` in R4 (lead decision
    2026-10-24, together with passing the old closing-order list through `kill_all` until R2).
  - The old Overview order panel (`POST /fund/orders`) goes with R3; manual orders are placed in Trading
    (`/v2/oms/orders`), Research's hand-over likewise.
- **R1** — moves done (§22.7); council package, v25, `/v2/council/*` built; parity green (§22.10); review gate. Old
  engine unchanged (analysis only), Council not active.
- **R2 = switch-over** (one user dialog + audit; refused in sandbox; preconditions: old account migrated —
  `pf_migrations` row; parity report accepted; `_unallocated` = 0; old autopilot off): (1) pre-v25 backup exists;
  (2) meta `council.switchover = pending` + audit; (3) ONE hedgefund.db transaction: `cl_params` copy (§22.4),
  `autotrade.heartbeat_minutes`, the spend seed (§22.2), the legacy import (D5); (4) `council.switchover = done`.
  `main.py` reads `council.switchover` at start (the `/fund/*` routes and `kill_all` read it per request): `done` → no
  `FundEngine`, Council task on; `pending` (crash between
  2 and 4) → step 3 is re-run idempotently (the transaction either happened — rows present — or not) then 4;
  anything else → old engine as today.
  **Stopping the old engine (re-review N3):** between steps 2 and 3 the running `FundEngine` is stopped IN-PROCESS
  under its `_trade_lock` (the scheduler task is cancelled, a running cycle is awaited, autopilot off; no backend
  restart needed); `FundEngine.start()` refuses while `council.switchover` is `pending` or `done`, and `main.py` does not
  create it then. `/fund/*` answer `410 Gone` `{"error": "The old engine was replaced by the
  Council strategy — use /v2/council/*", "code": "replaced"}` EXCEPT kill / resume (never 410, D6 — they delegate,
  see "Kill / resume across the switch-over" above). **R2 and R3 ship
  in ONE build.**
- **Revert (before R4 only):** `POST /v2/council/switchover/revert` (one dialog, audit; Council orders off, its working
  orders cancelled, `council.switchover = reverted`, the old engine's spend counter seeded (§22.2), then the old engine
  is RE-CONSTRUCTED in-process (a new `FundEngine` with its untouched newsroom.db params, started) and registered with
  `control.set_legacy_engine(engine)`, so `kill_all` reports the legacy half again; the `cl_*` rows stay —
  insert-only). The `/fund/*` routes decide per request from `council.switchover` at RUNTIME (not the startup state):
  `done` → 410 / the kill-resume aliases; `none` / `reverted` → the old engine's routes. A later R2 imports
  only old ids above the high-water mark (§22.8a).
- **Code rollback below v25 (emergency only, the user's decision; the revert above is the normal path):** (1) stop the
  backend (and close the app); (2) rename `hedgefund.db` and its `-wal` / `-shm` to `*.v25-<UTC>.stale` (never
  deleted); (3) copy `data/backups/hedgefund.db.pre-v25-<UTC>` to `hedgefund.db` (backend stopped — a plain file copy
  of a closed SQLite file; `PRAGMA quick_check` = ok on the copy); (4) start the older code. Everything written to
  hedgefund.db after the backup is lost (platform orders, ledgers, alerts since R2) — the dialog / guide says so.
- **§22.8a Legacy import (D5, inside the R2 transaction; idempotent via `council.legacy_hwm`)** — only old rows with
  an id above the high-water mark are imported (a second R2 after a revert imports only the new old ids); the mark is
  written in the same transaction:

  | Old (newsroom.db) | New | Mapping |
  |---|---|---|
  | `fund_params` versions referenced by imported runs | `cl_params` (`legacy` = 1, `source` "legacy fund_params v<N>") | `params_json` verbatim; frozen snapshot, never effective; `cl_runs.params_version` points at it |
  | `fund_runs` (trigger schedule / manual / startup; risk-watch runs are NOT imported — no decisions) | `cl_runs` (`legacy` = 1, `old_id` = id) + `cl_run_results` | trigger `schedule` → `scheduled`; `market_open` 1 → `open`, 0 → `closed`; `orders_enabled` / `sandbox` NULL (unknown); status `ok` / `partial` / `error` as is, `running` → `error` "never finished (old engine)", `skipped` → `partial` "skipped (old engine)"; `judge_mode` as is (`rules` allowed for legacy only); `cost_usd`, `summary_json` (+ `error` into it) |
  | `fund_agent_scores` | `cl_signals` (`legacy` = 1) | verbatim; `ai_generated` = 1 and `provenance_json` copied ONLY for `source` `llm` / `cached` (sentiment); `rules` / `unavailable` rows: `ai_generated` = 0, no provenance |
  | `fund_recommendations` | `cl_decisions` (`legacy` = 1, `old_id` = id) | action, confidence, target_weight, rule_score, rationale, model, provenance as is; `judge_source` = `source` (`llm` / `carried`; `rules` rows → `rules`, `ai_generated` = 0 and NEVER AI provenance); `rule_action` / `agreement` NULL; `cost_share_usd` from `usage_json.cost` when present |
  | `fund_recommendations.status` / `skip_reason` / `order_id` | `cl_executions` | `executed` → `submitted` (`client_order_id` NULL, reason "old engine order #<order_id>"); `skipped` → `skipped` + `skip_reason`; `open` / `superseded` → no row |
  | — | `cl_positions` | none for legacy runs |

  The change-detection memory after R2 is rebuilt from the newest legacy decisions per symbol (cold start).
- **R3** — WPF (Agent W, same build as R2): Council view (§22.11) on `/v2/council/*`; `FundClient` keeps only the shared
  HTTP / token base; `FundNotifications` keeps the toast host, its `/fund/events` source is replaced by the Council
  alert kinds; EquityChart / FundWindows / FundFormat / FundSettings stay; Research's order hand-over goes to
  `/v2/oms/orders` (a manual paper order, pre-filled) instead of `/fund/orders`.
- **R4** — refused in sandbox; history export (§22.12); golden fixture of the old path frozen (§22.10); rename to
  `*.stale`: `fund/engine.py`, `api.py` (incl. the `/fund/kill` / `/fund/resume` aliases), `judge.py`,
  `agents/{technical,risk,sentiment}.py`, `sizing.py`, `indicators.py`, `backtest.py`, `export.py`, `store.py`, the six
  shims, `fund/__init__.py`, `fund/agents/__init__.py`, AND the §17.9 migration (`execution/migration.py`, its routes
  `/v2/portfolio/migration*` and the WPF migration dialog — D12, approved by the user 2026-10-24; the guard-test
  exemption ends); tests per the deletion list.
- **R5** — full suite green + the user's build + click-test → delete the `.stale` files (deletion step with the user's
  delete permission). newsroom.db keeps its old tables.

### 22.9 Labelling, dropped features, observability

- **Labels:** the Council is never incubated, never live-eligible, never promoted, never allocator-managed, never
  decay-monitored; every view shows **"not validated — AI strategy"** and its record as **"forward paper record, N days,
  not validated"**. Sentiment and judge outputs carry **"AI-generated"** + their provenance (model, provider, prompt
  version, time — EU AI Act transparency).
- **Dropped with the old engine (explicit):** "execute recommendation" (manual accept) — the user places a manual
  order in Trading; the CSV exports `/fund/export/*` — replaced by the history export + the platform's own exports;
  `/fund/backtest(s)` — the backtestable part is "Council Rules (no AI)"; the old `flip` / `order` toasts — replaced
  by alert kinds `council_flip`, `council_order_filled`, `council_run_failed`, `council_budget_reached`,
  `council_orders_off` (monitoring alerts → Windows toasts); the SPY benchmark line — kept on the paper-council
  account equity chart (W).
- **Observability:** `GET /v2/council/state` carries the last run / result, next due time, task state (idle /
  running / not_active / sandbox), orders_enabled, budget (limit, spent, status) and the per-symbol cards; the backend
  log names every run start / end with id, status, cost.

### 22.10 Parity test (acceptance gate before R2; review M7)

- **Start state:** the REAL §17.9 migration run on a temp copy of an old store (synthetic old account with positions,
  queued orders, ledger) → `paper-council`; both paths start from that.
- **Harness** `tests/test_s10_council_parity.py` (+ `council_parity_testkit.py`): the OLD path (the body of
  `FundEngine` full cycles on a temp newsroom.db copy) and the NEW path (`council.service` + runtime order step on a
  temp hedgefund.db; the same PaperBroker) driven by ONE injected clock (a test asserts neither path reads the wall
  clock) with housekeeping (accruals, queued fills, margin, drawdown) on identical ticks; fake feed (quotes / bars /
  dividends), fixed macro / calendar / insider / news fixtures.
- **Fake LLM** keyed by (`feature`, canonical request hash) with scripted answers / usage; an unknown request FAILS
  the test loudly; the complete call logs of both paths are compared (feature, model, request, order of calls).
- **Lockstep window (re-review N1):** the lockstep comparison covers in-session cycles after the opening grace and all
  crypto targets; opening-grace and off-session EQUITY targets are kept OUT of the lockstep window and tested as
  separate scenario tests that assert the outcome `not_executed` "market closed — no order placed" (their decisions —
  signals, verdict, target weight — are still compared with the old path).
- **Data window:** (a) synthetic: 60 NY sessions, 8 symbols (6 equities / ETFs incl. one not shortable, BTC-USD,
  ETH-USD), a cycle every 30 min in the session ≈ 780 cycles, including a DST change, an early close, a holiday and
  the opening grace; (b) off-session manual runs (evening, weekend, across 00:00 UTC); (c) a frozen real-data
  fixture: 260 daily bars of SPY, QQQ, TLT, GLD, BTC-USD exported once from a BACKUP copy of hedgefund.db (never the
  real file).
- **Schedule equivalence test:** for a list of clock traces both schedules produce the same cycle start times.
- **Must match exactly** (floats abs ≤ 1e-12): agent signals (score, confidence, details, source); rule score /
  agreement / suggestion; the judge symbol set and canonical prompt; parsed verdicts (percent / invalid rules, veto
  mapping); recommendation source + unavailable text; target weights; order intents (symbol, side, qty after
  `round_qty`); skip reasons (text or one fixed mapping table); AI cost per run; every budget decision. After the
  replay: cash, positions, equity within 1e-6 USD.
- **Scenarios (each at least once):** judge unavailable (no key, budget 0, budget reached, unreadable spend, garbage
  JSON, timeout); `premium_on_flip` incl. a failing premium call; short veto / not shortable / block_short_increase;
  min_confidence; min_hold_days; max_trades_per_day (NY day vs UTC day, reversal with one order left); buying-power
  partial fill; crypto $10 minimum leg; two-leg reversal in the session (equity + crypto) and while closed (crypto);
  replace / keep of a working in-session Council order; manual / protective
  pending orders; kill / dead-man / Council switch off mid-cycle; drawdown stop; halted account; valuation paused;
  account unreadable; calendar / insider / FRED unavailable; budget across 00:00 UTC; spend restored after a restart;
  restart mid-window (memory rebuilt); cold start after R2 from the legacy import.
- **Deliberate differences** (table in the test; **the USER approves this list** before R2):

  | Difference | Old engine | Council (platform) | In the parity comparison |
  |---|---|---|---|
  | Equity orders outside the window / in the opening grace (D7) | queued for the open (filled at open + grace) | none — "market closed — no order placed" | decisions (signals, verdict, target weight) compared; **off-window equity order placement excluded**; account state compared only where it is not affected |
  | First scheduled cycle of a session | open + 5 s | open + 5 min grace + 5 s | schedule-equivalence test uses the new rule; the old trace is shifted by the grace |
  | Replace of a pending (queued) equity order off-window | the old engine replaced its queued order | no queued equity orders exist (D7) | separate scenario test only |
  | Two-leg equity reversal while closed | first leg queued, second leg after the open fill | decision only, no order (D7) | separate scenario test only |
  | Stale crypto order expiry (D11) | 2 × risk-watch interval, only in the risk watch | fixed 60 min at any time (OMS `accepted → expired`) | separate test |
  | Crypto risk watch (D2) | own cycle every `closed_interval_minutes` | runtime housekeeping (§22.6) | account level only |
  | Pre-trade checks | none | platform checks | fixtures stay inside the limits; a separate test shows a rejection → `not_executed` |
  | OMS state names, audit / event names | old names | platform names | mapped table |
  | Dropped features (§22.9) | manual accept, CSV exports, `/fund/backtest`, toasts | — / alert kinds | not compared |
- **APPROVED by the user 2026-10-24 17:59 (Berlin), "Jo" — all 9 points** of the plain-language list
  `scratchpad/mr/COUNCIL_DIFFS_FOR_USER.md`: (1) no stock orders while the market is closed / in the opening grace
  ("market closed — no order placed"; crypto 24/7); (2) first run of the day at open + 5 min + 5 s; (3) no queued stock
  orders to replace; (4) a stock flip while closed is a decision only; (5) unfilled crypto orders expire after a fixed
  60 min; (6) no separate overnight crypto check (the runtime covers it); (7) platform pre-trade checks before every
  order; (8) platform status / audit / event names; (9) manual accept, the old CSV exports, the old backtest button and
  the old pop-ups are not carried over (alerts + a one-time history export instead).
- **Golden fixture:** before R4 the old path's outputs over the whole window are frozen into a fixture, so the parity
  test keeps running after `fund/` is gone.
- **Acceptance:** 0 mismatches; the printed report (cycles, fields, scenarios) goes to the review gate.

### 22.11 API JSON and the Council view

- `GET /v2/council/state` → `{"active": bool, "switchover": "none|pending|done|reverted", "task": "idle|running|not_active|sandbox", "orders_enabled": bool, "label": "not validated — AI strategy", "record": {"since": "…", "days": n}, "next_due_at": "…Z"|null, "budget": {"limit_usd": x, "spent_usd": y|null, "status": "ok|reached|off|unreadable"}, "last_run": <run>|null, "symbols": [{"symbol", "action", "confidence", "judge_source", "rationale", "ai_generated", "provenance", "agents": [{"agent", "score", "confidence", "summary", "source"}], "target_weight", "position": {"qty", "weight"}, "execution": {"outcome", "reason", "client_order_id", "order_state" (read from the OMS)}|null, "unavailable_reason"}]}`
- `GET /v2/council/runs?limit=1..200` → `{"runs": [<run>]}`, `<run>` = `{"id", "trigger", "started_at", "finished_at", "status", "judge_mode", "cost_usd", "params_version", "orders_enabled", "legacy", "summary"}`; `GET /v2/council/runs/{id}` → `<run>` + `signals`, `decisions`, `executions`, `ai_calls`.
- `POST /v2/council/run` (§22.5) · `GET /v2/council/params` → `{"version", "params", "limits"}` · `PUT /v2/council/params {params…, reason?}` → new version (strict, unknown keys 400) · `PUT /v2/council/orders {"enabled": bool, "reason"?}` → `{"orders_enabled", "audit_id"}` (409 `halted` / 503 audit) · `GET /v2/council/switchover` → dry run `{ok, blockers, state, parity_accepted, params: {from_version, council, notes}, heartbeat_minutes, spend_seed_usd, legacy: {runs, signals, recommendations, hwm_before, hwm_after}}` · `POST /v2/council/switchover {reason?}` → `200 {state: "done", tx, params_version, spend_seed_usd, legacy, hwm, …}` / 409 `sandbox` / 409 `switchover_blocked` + `blockers` / 503 · `POST /v2/council/switchover/revert {reason?}` → `200 {state: "reverted", old_spend_seed_usd}` / 409 `not_switched_over` (dialogs; built in R2, §22.14). Errors `{error, code}`, never a stack trace.
- **Toolbar (W):** the pinned kill button calls `/v2/oms/kill` (already before R2, D6) and shows the three halves
  (§22.8). "Resume…": BEFORE R2 the halted platform accounts + the old engine (`/v2/oms/resume` per account,
  `/fund/resume` for the old engine); in the R2 + R3 build the halted accounts only (`/v2/oms/resume` per account).
- `GET /v2/council/broker-params` → `{"version", "effective", "migrated", "override", "limits"}` · `PUT
  /v2/council/broker-params {params…, reason?}` → new override version (strict; same bounds as `/fund/params`; audit-first)
  (D9).
- **Council view (W):** header with the label, record days, task state, next due, budget; "Run now"; "Council orders"
  switch; per-symbol cards (agents, verdict, AI-generated rationale + provenance, target vs position, execution
  outcome / why no order); run list + run detail; params dialog (Council params + broker-params override, D9);
  switch-over / revert dialogs.

### 22.12 History export (D4)

Before R4: the real newsroom.db is copied with the SQLite backup API (source read-only) into a temp file; from that
copy the tables `fund_runs`, `fund_agent_scores`, `fund_recommendations`, `fund_events`, `fund_audit`, `fund_params`,
`fund_backtests`, `fund_cash_ledger`, `orders`, `positions`, `equity_curve`, `signals` and the newsroom.db `meta` rows
whose key starts with `fund.` are written to
`data/backups/stage10-<UTC>/old_engine_history.jsonl` (one JSON object per row: `{"table", "row"}`) with a header line
(`newsroom.db` schema version, export time, row counts per table) and a `.sha256` file; the counts are re-read and
compared. The temp copy is deleted afterwards; the real newsroom.db is never written.

### 22.13 R1 as-built (2026-10-24, build-only — nothing switched over)

- **Module moves M1–M6 done** (suite green after each): `core/types.py`, `core/params.py`, `core/llm.py`,
  `data/market_feed.py`, `execution/paper_broker.py`, `strategy/features/macro_agent.py`; the old `fund/*` paths are
  same-object shims until R4; guard `tests/test_s10_no_new_fund_imports.py` (`NOT_YET_MOVED` empty).
- **Code:** hedgefund.db **v25** (`cl_params`, `cl_broker_params`, `cl_runs`, `cl_run_results`, `cl_signals`,
  `cl_decisions`, `cl_positions`, `cl_executions`, `cl_ai_calls`; insert-only triggers; pre-v25 backup);
  `strategy_platform/council/` (`service.py`, `store.py`, `params.py`, `spend.py`, agents / judge / sizing / indicators
  copied unchanged from `fund/`); `PaperBroker.plan_target` (side-effect-free plan of `target`); origin `council`
  (OMS, broker maps, client-id prefix `cl-`, re-send gate, `control.council_orders_off` on kill / start);
  alert kinds `council_*`; `api/council_routes.py` (`/v2/council/{state,runs,runs/{id},run,params,broker-params,orders}`,
  `/v2/council/state` carries the sandbox `mode` stamp); `main.py` builds `council.service.default()` on the same feed
  and newsroom accessors as the old engine but does NOT start it (R2 starts it); switch-over / revert routes, the
  heartbeat move (`GET /v2/monitoring/alerts` touches) and the legacy import are R2 work.
- **"Run now" (lead decision 2026-10-24: follow the contract):** `POST /v2/council/run` answers
  `202 {"run_id": n, "started": true, "orders": {"equity": bool, "crypto": bool}}`. `request_run` creates the
  `cl_runs` row up front (trigger `manual`, params version, session label, orders gate, sandbox flag) and hands its id
  to the Council task, which runs the cycle on that row (no second row). A row whose cycle cannot start (another
  cycle holds the lock, no params) is finished at once as `error` "not started: …"; at every Council task start
  `store.sweep_unfinished` finishes each non-legacy run without a result as `error` "interrupted — the backend
  stopped before this Council cycle finished" (`cl_run_results` is insert-only, so the result row is the only
  status) — no run stays "running" after a restart.
- **Settings (S10-E, ENV_API §9):** `core.llm.models()` reads the model ids only via `newsroom.ai.analysis_models()`
  (H moves it to `data/settings.json`); no environment read; an unreadable settings source raises
  `LlmUnavailable("error")` → the judge / sentiment report "AI unavailable" (no silent default); code defaults only
  when newsroom.ai is not importable (offline tests).
- **Hardening found by the parity harness:** `council.params.from_old` / `broker_from_old` refuse the stored version
  ROW (`fund_store.latest_params()` → `{"version", "params", …}` silently copied the defaults) — R2 passes
  `fund_store.effective_params()[1]`.
- **Parity harness (§22.10) built:** `tests/council_parity_testkit.py` + `tests/test_s10_council_parity.py` — the
  REAL §17.9 migration of one synthetic old account (positions from the previous session; queued orders block §17.9,
  so none are seeded), the old path on a SQLite-backup copy of newsroom.db, ONE injected clock, a deterministic fake
  market (8 symbols: 6 equities / ETFs incl. not-shortable XLE, BTC-USD, ETH-USD), ONE scripted fake LLM keyed by
  (feature, request hash) — the new path may only send requests the old path sent (misses fail the test) — and per
  tick: signals (score, confidence, source, details), verdicts, target weights, order intents, AI call logs, AI cost;
  at the end cash / positions / equity (≤ 1e-6). Lockstep window = in-session ticks after the opening grace every
  30 min (`UFOS_PARITY_SESSIONS`, default 2 in the suite). Executions are compared through ONE fixed mapping
  (`SKIP_REASON_MAP` / `map_skip`): the old single status "skipped" ↔ the Council's `skipped` / `rejected` /
  `not_executed` with the IDENTICAL reason text; "engine halted" ↔ "the account is halted"; "autopilot switched off"
  ↔ "auto trading is off" / "Council orders are off"; old status `open` (autopilot off) ↔ `not_executed` + gate
  reason. Orders are compared by what executed `(symbol, side, filled qty)` — the old store keeps the broker-clipped
  qty (refused leg: rejected qty 0), the OMS the requested qty + `filled_qty`.
- **Scenarios** (`tests/test_s10_council_parity_scenarios.py`, each asserts parity AND that the scenario happened):
  judge unavailable — no key, garbage JSON, AI budget 0, budget reached, unreadable spend (both fail closed),
  timeout (pre-call estimate charged); `premium_on_flip` incl. a failing premium call; not shortable / short
  exposure cap / shorts off (vetoes); min_confidence; min_hold_days; max_trades_per_day; buying-power partial fill;
  two-leg reversal in the session and crypto reversal while closed; halted account; kill mid-cycle; Council switch
  off mid-cycle; dead-man (no client heartbeat); drawdown stop (price shock → both halt + liquidate, books equal);
  valuation paused; calendar / insider / FRED unavailable; budget across 00:00 UTC; spend restored after a restart;
  restart mid-window (change-detection memory rebuilt from the DBs); plus D7 (grace / after the close) and the
  schedule-equivalence test in `test_s10_council_parity.py`.
- **Real-data fixture (c):** `tests/council_parity_real_bars.json` — last 260 daily bars up to 2026-10-23 of SPY, QQQ,
  TLT, GLD, BTC-USD, exported once from a COPY of the stage-10 backup hedgefund.db (sha256 recorded, one source per
  day: alpaca_sip > alpaca > tiingo); `RealWorld` replays it — parity over 6 ticks.
- **No wall-clock read:** `WallClockSpy` swaps `datetime` in the decision-path modules (old engine, Council, core.llm,
  paper broker, OMS, accounts, control, runtime, ledger / risk / checks, calendars, market feed) for a recording
  subclass; during the lockstep ticks the only call site is `core.llm.provenance` (the AI provenance stamp, metadata
  — `WALL_CLOCK_ALLOWED`). The OLD store booked its AI spend on the wall-clock UTC day (`fund.store._utc_day` without
  `now`); the harness pins that one old-engine read to the injected clock (the Council already uses the clock).
- **Results:** 60 NYSE sessions from 2026-10-05 (incl. the DST change, Thanksgiving + the early close after it) — 768 ticks, 414 Council
  orders, 258 AI calls, 0 unscripted requests, 0 mismatches (run twice, the second time with the execution /
  skip-reason and filled-qty comparison); 24 scenario tests green; real-data fixture green.
- **Open before the R2 gate:** the cold start from the legacy import (R2 builds the import); crypto $10 minimum leg,
  replace / keep of a working in-session Council order and manual / protective pending orders (not reachable with
  immediate paper fills in the session — partly covered by the `plan_target` unit tests, not by lockstep); account unreadable;
  the golden fixture (before R4). The deliberate differences were APPROVED by the user 2026-10-24 17:59 (§22.10).

### 22.14 R2 as-built (2026-10-24, backend; the WPF dialog + Council view are R3, Agent W, same build)

**Nothing was switched over by an agent.** The real switch-over happens only when the user confirms the dialog in the
app (`POST /v2/council/switchover`); every test runs on temp copies (temp hedgefund.db / newsroom.db / cost log).

- **`strategy_platform/council/switchover.py`** — `blockers` / `dry_run` / `run` / `apply` / `recover` / `revert`:
  - preconditions: not sandbox (409 `sandbox`); state not `done`; §17.9 migration done; `paper-council` exists and its
    `_unallocated` is empty; old autopilot off; the pre-v25 backup `data/backups/hedgefund.db.pre-v25-*` exists OR the
    database was created at v25+ (new meta `schema.created_version`, written once by `db.migrate` on an empty file);
    parity accepted (`PARITY_ACCEPTED = "2026-10-24"`, the user's approval §22.10); today's AI spend readable;
  - steps: audit `council_switchover {stage: pending}` (audit-first) → meta `council.switchover = pending` with an
    attempt id `tx` → the running `FundEngine` stopped in-process (a running cycle awaited ≤ 120 s, task cancelled,
    autopilot off under its `_trade_lock`, unregistered from `control`) → ONE hedgefund.db transaction: legacy import
    above the high-water mark (§22.8a; risk-watch runs — trigger `risk-watch` or a `startup` run with
    `summary.risk_watch` — skipped; `rules` decisions never AI-marked; executed → `submitted` "old engine order #N",
    skipped → `skipped` + reason), the Council params copied from the latest `fund_params` (`source` "copied from
    fund_params vN"), meta `autotrade.heartbeat_minutes` `{minutes, source}`, the spend counter seeded with
    max(old meta `fund.ai_spend` today, Σ cost log `fund-sentiment` + `fund-judge` today), the high-water mark and the
    marker `council.r2_tx` (with `tx`) → `council.switchover = done`, Council orders OFF (cause `switchover`), audit
    `council_switchover {stage: done}`, the Council task started;
  - idempotent per attempt: `apply` with the same `tx` writes nothing twice; backend start with `pending` →
    `recover` finishes the same attempt; a later switch-over after a revert has a new `tx` and imports only old ids
    above the mark;
  - revert: audit `council_revert` → Council orders off + working orders cancelled → Council task stopped → old
    counter `fund.ai_spend` seeded with max(old, cost log) → `reverted` → the old engine re-created by the factory
    `main.py` registered (`configure`) and handed to `control`, the `/fund` API and the AI-budget source.
- **`main.py`:** reads the state at start via `switchover.recover()`: `done` → no `FundEngine`, the Council task
  starts; `none` / `reverted` / unreadable → the old engine (its `start()` refuses while `pending` / `done` or when the
  state is unreadable with hedgefund.db open).
- **`/fund/*` (fund/api.py):** decided per request (`switch_state`): `pending` / `done` → 410 `{"code": "replaced"}`
  except `/fund/kill` → `control.kill_all(via="/fund/kill")` and `/fund/resume` → `control.resume("paper-council",
  via="/fund/resume")` (audited like the platform routes, with `via`); unreadable → 503 (kill still forwarded).
- **`control`:** `kill_all` → `{"platform", "legacy", "council"}` per §22.8 (before R2: legacy incl. the old closing
  orders, council `{"applicable": false}`; after: legacy "not applicable", council `{applicable, ok, orders_enabled,
  cancelled_orders, error}`); the heartbeat timeout from R2 on is meta `autotrade.heartbeat_minutes` — missing /
  invalid → `check_heartbeat` switches auto trading off (audit `autotrade_heartbeat_off` with the reason) and
  `set_autotrade(True)` answers 409 `heartbeat_unreadable`; `GET /v2/monitoring/alerts` touches the heartbeat.
- **AI budgets:** `FundSource` reads `cl_params.ai_daily_budget_usd` + the Council counter from R2 on; a PUT writes a
  new `cl_params` version (audit-first, `via: "/ai/budgets"`).
- **Tests:** `tests/test_s10_council_switchover.py` (preconditions + sandbox, unreadable spend refuses before any
  change, the full run incl. legacy counts / params / heartbeat / seed / cold-start memory, kill / resume / 410
  after R2, kill before R2, AI budget, heartbeat fail-closed + the alerts poll, pending recovery, revert + a second
  run importing only new ids); the parity harness now starts the Council through the REAL `switchover.run` (0
  mismatches unchanged).
- **Review fixes (independent review of R2 + R3, 2026-10-24; API: `scratchpad/mr/SWITCHOVER_FIX_API.md`):**
  - **M1 (double import):** ONE switch-over operation at a time (process lock around run / retry / abort / revert /
    recover → 409 `switchover_running`); a new run while `pending` is blocked (`switchover_pending`); the
    high-water mark is re-read INSIDE `BEGIN IMMEDIATE` (moved → 409 `concurrent_import`, nothing written);
    hedgefund.db **v26**: UNIQUE partial indexes `ux_cl_runs_legacy_old_id` / `ux_cl_decisions_legacy_old_id`
    (`old_id … WHERE legacy = 1`; a database that already holds duplicates cannot get them — then NOT silent: a
    critical alert `council_legacy_duplicates` at every start and the switch-over blocker `legacy_duplicates`
    {table, rows}, visible in the dry run). Test:
    two threads → one `done`, one `switchover_running`, no duplicate rows.
  - **M2 (stuck pending):** a failure after `pending` keeps `pending` + `last_error` / `error_at` (audit
    `council_switchover {stage: failed}`, critical alert `council_switchover_failed`), answered 503
    `switchover_failed` {last_error}; `GET /v2/council/switchover` adds `pending` {tx, at, actor, previous, applied,
    last_error, error_at} + `actions` {run, retry, abort, revert}; `GET /v2/council/state` adds `switchover_error`;
    `POST /v2/council/switchover/retry` (same attempt) and `POST /v2/council/switchover/abort` (transaction not
    applied → back to `previous` none / reverted; applied → like a revert with the old spend seeded; always Council
    orders off and the old engine re-created with autopilot off); `recover()` at start retries once and on failure
    leaves `pending` + alert — `main.py` then runs NO engine and logs it as an error.
  - **Minors:** the old engine's `_stopping` is set before the up-to-120 s wait (no new cycle); revert / abort return
    `old_engine_started` + `old_engine_error` (refusal or no loop reported, never swallowed); a revert of an already
    reverted state answers 200 `already`; blocker `council_unavailable` when the service failed to build and the
    switch-over answer carries `council_task_started` / `council_task_error`; `kill_all` treats `pending` like
    `done`; a failing `/fund/kill` alias reports both halves as failed/unknown (no fake ok); `*.tmp` leftovers never
    count as the pre-v25 backup (switch-over check AND `db.backup_before`); revert reads the state / spend off the
    event loop and is refused in sandbox.
- **The user's click-test (after the R2 + R3 build):** see the todo item S10-C.

## 23. Stage 10 — hardening, security, Kraken removal, .env removal (as-built, merged 2026-10-24)

Merged by Agent P from the batch notes `scratchpad/mr/SPEC_PENDING_s10_{A,B,E_W,H,K,U}.md` (text as delivered by
Agents H and W, lightly shortened). The Council migration is §22. Open decisions are listed in §23.7.

### 23.1 S10-B backup + cleanup (Agent H)

- Fresh backup of the three real DBs: `HedgeFund/backend/data/backups/stage10-20261024T115619Z/` (hedgefund.db v24,
  newsroom.db v3, market_research.db v7; quick_check ok; `SHA256SUMS.json` / `.txt`; byte copy verified unchanged,
  rebuilt with the SQLite backup API on the VM side, copied back).
- Deletion (user permission 13:54): 106 files, 1.36 GB, each sha256-verified against `DELETE_MANIFEST.txt` (308 rows;
  deleted subset `DELETE_NOW.txt`). 202 recent `*.stale` rollback copies kept until the click-test
  (`KEEP_UNTIL_CLICKTEST.txt`). Kept untouched: the real DBs, `options_archive.db*`, `tr-import/`, scope-guard
  manifests, current sandbox copies.
- Agent rule: never open a real DB over the VM mount with SQLite ("disk I/O error" on the WAL index, stray
  `-wal` / `-shm`) — copy db + `-wal` to a VM-local temp dir, verify sha256 before = after, open the copy.

### 23.2 S10-H backend hardening (Agent H)

**S10-H tools venv guard.** Every command-line tool in backend/tools/ refuses the system Python BEFORE it imports
anything beyond the standard library: strategy_platform/core/venv_guard.py (stdlib only) — problem(tool,
backend_dir, prefix) returns None when realpath(sys.prefix) == realpath(<backend>/.venv) (normcase), else
"This is not the backend's Python (<prefix>) — run with .venv\Scripts\python.exe tools\<tool> …  (from
HedgeFund\backend)"; require(tool, backend_dir, exit_code) prints "REFUSED: <message>" to stderr and exits. Only the
__main__ path is guarded (importing a tool in tests never triggers it). Exit codes (documented in each tool's
docstring): sandbox.py 2 (its existing venv_problem() now delegates to venv_guard), options_archive.py 3 ("stopped:
… / not the backend venv"), verify_alpaca_options.py 3, verify_binance_demo.py 5 (4 = "test order refused" already
taken). A new tool needs the guard + an entry in tests/test_s10_hardening.VenvGuardTest.TOOLS (the test fails
otherwise).

**S10-H NYSE calendar through 2030.** core/calendars.py: CALENDAR_YEARS = 2000–2030. 2016–2027 stay hardcoded
(published rows); 2028–2030 (PROJECTED_YEARS) come from modern_holidays(year) / modern_early_closes(year), the
current NYSE rules: New Year's Day (Sunday → Monday; on a Saturday no closure), MLK Day (3rd Mon Jan), Washington's
Birthday (3rd Mon Feb), Good Friday, Memorial Day (last Mon May), Juneteenth (since 2022), Independence Day, Labor
Day (1st Mon Sep), Thanksgiving (4th Thu Nov), Christmas — Jun 19 / Jul 4 / Dec 25 on Saturday → Friday before, on
Sunday → Monday after; 13:00 early closes: Jul 3 Mon–Thu, day after Thanksgiving, Dec 24 Mon–Thu. Source:
nyse.com/markets/hours-calendars (checked 2026-10-24; the published calendar covers 2026–2028). 2028 = the
published calendar; 2029–2030 are the rules projected — an unforeseeable special closure (day of mourning, weather)
must be added when the NYSE announces it. A test proves the rule functions reproduce the hardcoded 2022–2027 rows
(minus the 2025-01-09 Carter day of mourning). Each of 2028/2029/2030 has 251 sessions. 2031+ still raises
CalendarUnavailable (fail-closed, unchanged).

**S10-H deterministic committee status.** research/committee.run_out reads "is the worker active" BEFORE the run's
events. The worker commits its terminal event before it leaves _active, so a run that is no longer active always
shows its terminal event; a run finishing between the two reads now reports "running" (the next poll sees "done")
instead of a false "interrupted" (the cause of the flaky test_sp_committee).

**S10-H market_research.db startup race.** market_research/store.init sets _opening = True for the duration of the
open (reset in finally). While the FIRST open runs, status() returns state "not_opened" (starting) instead of
"unavailable", and an operation that needs the path (_require_path) waits for the running open (takes the store
lock init holds) instead of failing. A failed open still reports "unavailable" with its reason.

**S10-H incubation.realised counts the first session (SPEC §19.12 note resolved).** live/incubation.realised reads
ledger.daily_returns from the snapshot of the session BEFORE start_date (previous NYSE trading day; for a utc-calendar
run the day before) and keeps returns with start_date <= day <= end_date — the first session's own return is
included, as in monitoring/expected.realised (before, the §17.2 band dropped it).

**S10-H context notices (backend part, SPEC §15 "Stage 10 addition").** strategy_platform/core/notices.py holds the
texts; the three AI-context notices are word-for-word Shared/AiContent.cs (the WPF shows payload notices verbatim):
ANALYSIS (a) "AI-generated analysis — may be inaccurate; information, not a recommendation."; RESEARCH (b)
"AI-generated research output for your own decision. Not a personal recommendation by a licensed advisor; you
decide and carry the risk. Backtests are hypothetical."; PAPER_EXECUTION (c) "Automated trading on your own paper
account under the limits you set. Paper only — no real money." (d) a live notice is defined only if live trading is
ever enabled. Short context lines: PAPER "Paper trading · simulated money.", PAPER_DEMO "Paper/demo trading ·
simulated money.", HYPOTHETICAL "Hypothetical, simulated results — they do not indicate future results.", REVIEW
"For your own review.".
- payload `notice` of /v2/portfolio/state (portfolio/views.NOTICE), /v2/brokers… (execution/brokers/service.NOTICE),
  /v2/monitoring/overview (monitoring/service.NOTICE) = PAPER_EXECUTION;
- Studio / discovery loop AI_INTERACTION_NOTICE = "You are interacting with an AI system. … AI-generated and not
  verified. " + RESEARCH (EU AI Act Art. 50(1) sentence kept);
- alert / event bodies (monitoring/alerts, reconcile, broker kill, control.kill_all, incubation allocation,
  decay, risk caps): "Paper trading · simulated money." / "Paper/demo trading · simulated money.";
- Shadow rule portfolios: "… Hypothetical, simulated results — they do not indicate future results."; Shadow report
  disclaimer: "… monitoring only, for your own review. …";
- newsroom macro phase disclaimers: "… not a forecast — information, not a recommendation.".
fund/ (old engine) is untouched — it goes with S10-C. A test (NoticesTest) checks parity with the AiContent.cs
literals and that no user-facing strategy_platform text carries the blanket "not investment advice".

### 23.3 S10-K Kraken demo removal (backend: Agent H; WPF: Agent W)

**S10-K Kraken demo removal (backend).** The Kraken Futures demo adapter (execution/brokers/kraken_demo.py → .stale)
and every kraken-demo branch are gone:
- mirror: no Kraken adapter; venue / tif / can_hold rules name binance-demo only;
- service: no Kraken meta / "Markets loaded (PF_*)" test check / other-wallet kill note; crypto route targets =
  paper-main | binance-demo;
- reconcile: no Kraken funding import, no collateral basis (cash basis for every broker), no Kraken tolerance /
  liquidation hint;
- live_guard: no Kraken demo endpoint / host. LIVE_HOSTS (the deny-list of production hosts, documentation and
  the source-scan test) keeps futures.kraken.com / api.kraken.com;
- secrets: the key names kraken-demo-api-key / -secret left SECRETS (never read, not listed by GET
  /v2/settings/keys). They are kept as secrets.RETIRED_SECRETS: POST /v2/settings/keys/changed accepts them for
  action "removed" ONLY (audit row key_removed {name}); "set" / "imported" → 400 "<name> is retired — only action
  "removed" is accepted". Lets the WPF audit the removal of a still-stored key cleanly (W request 2).
KEPT (the retirement guard, SPEC §18.13): accounts.RETIRED / RETIRED_SOURCES / retired_reason; kraken-demo stays a
known id (accounts.BROKER_IDS = ACTIVE_BROKER_IDS (alpaca-paper, binance-demo) + the retired ids; the route regexes
keep accepting it) so an old kraken-demo row / route in hedgefund.db is answered 409 "retired", shown blocked under
Routing and never re-routed silently. Such a row gets mirror.RetiredAdapter (every call raises BrokerSetupError
"<id>: retired …"; no client, real or fake, is ever built). GET /v2/brokers lists the retired card only while
hedgefund.db holds a kraken-demo row (account, connection record or a route); its card has chip "RETIRED", no keys,
endpoint / sdk / terms null, every action off except "disconnect" of a still-connected record (record only). The
user's hedgefund.db (checked on a copy 2026-10-24) has no kraken-demo account / connection / route — only audit
history — so the card does not show. Also kept: ccxt (binance-demo), data/providers/kraken_public.py (Kraken PUBLIC
crypto prices for Shadow / insights), the storage/db.py migration CHECK lists, all audit rows.
requirements.txt: the ccxt comment names binance-demo.

**S10-K (WPF).** CredentialStore: the kraken-demo key names left the active key list; they are kept as
RetiredSecrets and shown in ⚙ Keys & data ONLY while still stored in the Credential Manager, with "Remove" as the
only action ("Kraken Futures demo retired 2026-10-19 — this stored key is not used by anything; remove it").
PlatformClient.BrokerIds = alpaca-paper, binance-demo (KrakenDemoId stays to name / block an existing retired row:
RetiredBrokerCard, AccountTitle, IsBrokerId, TradingView account line). Removed: the Kraken route target and its
consequence text, every kraken branch of the active broker card (margin fields, collateral wording, reduce-only kill
text), Kraken key entry (pair dialog, tooltip, validation).

### 23.4 S10-U WPF hardening (Agent W)

**S10-U single instance (scope label stage10ui: UFOS.ai/App.xaml.cs, UFOS.ai/Shared/AiContent.cs).** App.OnStartup
takes a named mutex "Local\UFOS.ai.App.<user SID>.Instance" BEFORE the AI notice. A second start signals the named
event "…Activate" (after AllowSetForegroundWindow(ASFW_ANY)), logs one line and exits with code 0 — no window, no
notice. The first instance listens on a background thread and brings its main window (or the AI notice while that is
still open) to the front (SW_RESTORE keeps a maximized state, Activate, SetForegroundWindow, one Topmost toggle). If
the guard itself cannot be created (exception on the mutex name) the start continues and the failure is logged.
The standalone HedgeFundApp.exe has no guard (developer entry point).

**S10-U context notices (SPEC §15 "Stage 10 addition").** Shared/AiContent.cs holds the three notices:
AnalysisNotice (a) "AI-generated analysis — may be inaccurate; information, not a recommendation." (also the value of
AiContent.Caveat, so every existing caveat line changes with it); ResearchNotice (b) "AI-generated research output
for your own decision. Not a personal recommendation by a licensed advisor; you decide and carry the risk.
Backtests are hypothetical." (Studio replies, paper → strategy, committee memos, loop finalists);
PaperExecutionNotice (c) "Automated trading on your own paper account under the limits you set. Paper only — no real
money." (d) is defined only if live trading is ever enabled. The HedgeFund WPF no longer shows the blanket "not
investment advice": paper contexts say "Paper trading · simulated money", backtests / validations "Hypothetical,
simulated results — they do not indicate future results.", analyses / AI explanations "information, not a
recommendation", Shadow statistics "for your own review". Outside the module (not in stage10ui): MainWindow,
Global Monitoring, LiveStreamAgent, Shared/AiDisclaimerWindow.cs still carry their own wording except where they use
AiContent.Caveat — a later label if the user wants them changed too.

**S10-U Studio polish.** A discarded / empty off-topic AI answer is never an empty AI block: followed by the
backend's fixed note it renders as ONE system note ("<backend note> · AI reply not shown (reason) · cost …");
without a note as one small system note with the cost.

**C1 kill path.** The toolbar ■ Kill switch and "Stop engine…" call POST /v2/oms/kill (control.kill_all): the platform
half (every platform account, brokers included) AND the old engine's kill (eng.kill(propagate=False)) — each half
reports its own result; the old engine's half is {ok, error, halted} only (its closing orders are listed in the
Overview, no longer in the result dialog). If the old engine is not running, its half says so (ok=false).
The toolbar "Resume" of the OLD engine stays on POST /fund/resume: /v2/oms/resume resumes ONE platform account
(account_id required) and has no old-engine half — the old-engine resume goes away with the Council migration (S10-C).
Market Research's Buy / Sell / Short / Cover hand-over opens Portfolio & Trading's order dialog (POST /v2/oms/orders,
pre-filled symbol + side, account = the first platform account) instead of the old engine's /fund/orders panel.

**Single-instance guard — failure case (user decision 2026-10-24, option A).** If the named lock cannot be created
the app starts normally, logs a warning AND shows a small non-blocking note in the main window's lower right corner:
"Single-instance protection unavailable this session — a second start would open a second window." (OK or closes
itself after 30 s).

### 23.5 S10-A security audit fixes

**S10-A1 backend / python (Agent H; scope label `stage10sec`).**

**S10-A1 (stage 10 security audit, backend / python).**
- M6: every backend listens on 127.0.0.1 only — `api_auth.bind_host(what)` → 127.0.0.1 (the remote-access opt-in
  and the host settings were removed with S10-E / the Docker removal, §23.6).
- L2 Host check (all three api_auth.py copies, kept identical): every HTTP request and WebSocket handshake —
  /health and /version included — needs Host: 127.0.0.1:<port> or localhost:<port>, <port> = the port the server
  listens on (read from the socket; unreadable → refused, fail closed); otherwise 403 {"error": "host not allowed"}
  (DNS-rebinding protection). check(..., port=None) stays pure (no Host check without a port).
- L2 public routes trimmed: HedgeFund GET /health = {"status": "ok", "mode"}; GET /version = {"code_mtime",
  "started_at", "mode"} — no pid, no sandbox details / data_dir (those stay in the token-protected state payloads).
  Global Monitoring GET /version = {"code_mtime", "started_at"} (no pid). The apps only read code_mtime.
- L9: the HedgeFund WebSocket placeholder on 8865 is removed (no client: WPF, GM, LSA checked) — config WS_HOST /
  WS_PORT gone.
- L5: the PDF worker (research/papers.extract_pdf) runs with env = papers.worker_env(): the backend's environment
  minus every name matching validation/parallel.SECRET_ENV_RE (as the validation workers).
- M2: HedgeFund/backend/.gitignore allow-lists data/: `data/*`, `!data/.keep`, `!data/scenarios/`,
  `data/scenarios/*`, `!data/scenarios/.keep` (the only versioned data/ files — read from the git index),
  plus `data/sandbox0-backup/` and `*.stale`.
- M1: superseded — Docker was removed entirely (user decision 2026-10-24 16:56, §23.6).
- M3 (part): LiveStreamAgent requirements no longer list the PyPI "asyncio" backport (stdlib).
- L1: GM (config.py + the per-call re-read in assess.py) and LSA (config.py) accept OPENROUTER_BASE_URL only as
  https on host openrouter.ai, default port, without user info / query / fragment (= W's C# check in
  Services/OpenRouterClient.cs); otherwise warning + the default https://openrouter.ai/api/v1 (the
  fallback the user accepted for the HedgeFund copy; the URL itself is not logged).
- L3: GM assess_context article fetch — public addresses only: IP-literal hosts and every redirect hop checked
  (check_url: http/https, no credentials, no localhost/.local, literal IP must be global), names resolved through
  PublicOnlyResolver (aiohttp resolver dropping private / loopback / link-local / reserved / CGNAT / multicast /
  IPv4-mapped-private results; none left → no connection) on its own session; redirects followed by hand, at most
  3; the article cache is an LRU of 200 entries (1 h TTL as before). Refusals are logged, the context then simply
  has no article text.
- L6: GM / LSA prune data/logs/<yyyy-mm-dd>.jsonl older than 30 days at start (logger.prune_old_logs; other files
  untouched). process.log (GM) is written by the WPF app (MainWindow.xaml.cs), env-setup.log / ui-errors.log by the
  scripts / app — not python-owned, so no size cap here (WPF-side follow-up if wanted); GM backend.log already rotates
  (2 MB × 3).
- L10: GM /localnews, /assess, /localnews-summary answer generic messages ("… (details in backend.log)"); the
  exception text goes to backend.log only.

**M3 dependency pinning (proposal, decision open; the C# side of the lock protocol is in §23.6).**
- Per backend a `requirements.lock` generated with `pip-compile --generate-hashes` (pip-tools) from the current
  `requirements.txt` on the user's Python 3.13 (Windows), committed next to it.
- run_*.ps1 installs with `pip install --require-hashes -r requirements.lock` (fails closed on any hash mismatch);
  `requirements.txt` stays the human-edited input. Updating = re-run pip-compile, review the diff, click-test.
- Optional packages (HedgeFund requirements-broker.txt: ccxt) get their own lock (same pattern, own pip step as now).
- Cost: one lock per Python version/OS (wheels differ); pip-tools as a dev-only tool. Alternative with less tooling:
  `pip freeze` pins without hashes (protects against surprise upgrades, not against a tampered index).

**S10-A2 WPF (Agent W; scope label `stage10secui`).**

- **M4 Global Monitoring WebView2** (`Global Monitoring/MainWindow.xaml.cs`, `globe.html`): the globe page stays
  on NavigateToString (location about:blank, opaque origin). Only that one navigation is allowed (flag set right
  before NavigateToString, consumed by its NavigationStarting); every other top-level navigation is cancelled
  (a user-clicked http(s) link opens in the default browser), every frame navigation is cancelled, downloads are
  cancelled, permission requests denied, WebResourceRequested answers 403 to everything except the page itself,
  `http://127.0.0.1:8768`, `ws://127.0.0.1:8767` and the pinned Natural Earth file. Host messages (incl. the
  API token) are accepted only from the page URI forms of that navigation and only after its ContentLoading
  (allow-list instead of the former http(s) deny-list). Settings: no script dialogs, host objects, accelerator
  keys, autofill, swipe/pinch.
  CSP meta in globe.html: `default-src 'none'`; `script-src` = sha256 of the single inline script, computed by
  the host at load time (placeholder `'sha256-GM_INLINE_SCRIPT_HASH'`; unreplaced → script blocked);
  `connect-src` = the two backend ports + the pinned NE URL; frames/objects/workers/forms/base all `'none'`.
  Natural Earth: pinned `world-atlas@2.0.2/land-110m.json`, fetched with `credentials:'omit'`, no referrer.
  NOT vendored yet: neither the container nor the device reaches npm/jsdelivr (proxy 403) — follow-up: vendor
  the file (small, well under 1 MB) with sha256 like klinecharts, then drop the CDN from CSP / allow-list.
  The localStorage cache in globe.html never worked (opaque origin) — comment corrected, code left.
  `backend/config.py` `DEFAULT_ALLOWED_ORIGINS = ("null",)`: verified still required (the page's cross-origin
  fetch() sends `Origin: null`); unchanged.
- **M5 ArticleWindow** (`ArticleWindow.xaml.cs`): InPrivate controller (CoreWebView2ControllerOptions,
  SDK 1.0.4191.47), DevTools / script dialogs / host objects / web messages / autofill off, downloads cancelled,
  permissions denied. Side effect: MarketWatch's bot challenge may run on every article.
- **L4 PowerShell** (new `Shared/WindowsPowerShell.cs`): all five launch sites (Newsroom 2×, Global Monitoring,
  LiveStream Agent, PythonEnvManager) use `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe` and
  `-NoProfile -ExecutionPolicy Bypass … -File "<x.ps1>" …`. Every launched file is a .ps1 (the helper refuses
  anything else); Bypass stays because Windows client editions default to Restricted and RemoteSigned refuses an
  unsigned repo script with Mark of the Web; it is process-scoped and Group Policy still wins.
- **L1-C#** (`Services/OpenRouterClient.cs`): OPENROUTER_BASE_URL must be https, host openrouter.ai, default port,
  no user info/query/fragment — checked before the budget reservation; otherwise the request is not sent and the
  user sees "AI summary failed: OPENROUTER_BASE_URL … must be an https URL on openrouter.ai …".
- **L7**: globe.html AI tooltip/caveat = AiContent.TriggerTooltip / AnalysisNotice; stale "not investment
  advice" comments fixed in AnalysisReasoningView.xaml.cs and HedgeFundWindow.xaml.cs. Open: the startup notice
  body (Shared/AiDisclaimerWindow.cs, "No investment advice: …") is unchanged — user-mandated text ("do not
  remove or soften"); its comments match it. Needs a user decision if it should follow the Stage 10 wording.
- **L8**: main window — one data-source line under the overview cards (Yahoo Finance chart endpoint; MarketWatch /
  Dow Jones and Yahoo Finance RSS; © publishers, not affiliated; tooltip with hosts). LiveStream Agent — market-data
  footnote names Yahoo Finance via yfinance (unofficial, not affiliated, personal use).
- **L6 follow-up** (new `Shared/RotatingLogFile.cs`): the backend process.log written by the Global Monitoring WPF
  and by NewsroomBackend.cs is rotated at 5 MB (process.log.1…3, oldest dropped); the detached Hedge Fund backend's
  -LogFile is rotated before each start. A failed rotation is written into the log, writing continues.
- **Minor**: LiveStream Agent health wait uses a 2 s per-request HttpClient timeout (was the 100 s default), client
  and responses disposed.

**World-atlas vendoring (addendum, round 2).**
- New `Global Monitoring/tools/fetch_world_atlas.py` (stdlib only, same scheme as fetch_engine.py): pinned
  world-atlas@2.0.2, registry metadata integrity + tarball URL == pin, tarball sha512 == pin (three-way), extracts only
  package/land-110m.json (checked: TopoJSON with objects.land) + the licence file, writes `Global Monitoring/vendor/`
  + MANIFEST (`<file> <version> <source_url> <sha256> <fetched_at>`), old files → *.stale. Pinned integrity
  `sha512-IXfV0qwlKXpckz1FhwXVwKRjiIhOnWttOskm5CtxMsjgE/MXAYRHWJqgXOpM8IkcPBoXnyTU5lFHcYa5ChG0LQ==` (registry, read
  2026-10-24 via a web reader; the script stops if the registry disagrees, override only via --expect-integrity).
- GlobalMonitoring.csproj embeds vendor/land-110m.json, MANIFEST, LICENSE* only when present (rebuild after the tool).
- MainWindow decides one source per page load and logs it: Vendored (sha256 == MANIFEST, version 2.0.2, licence
  present → JSON sent to the page by host message; CDN removed from CSP connect-src and the resource allow-list) /
  Cdn (no vendored files in the build → pinned CDN URL only) / VendorRejected (any check fails → hand-drawn outlines,
  AppLog error, CDN NOT used). The page shows the source in a line under the globe ("Coastlines: … — vendored copy
  (sha256 checked)" / "… — pinned CDN file (no vendored copy in this build)" / "hand-drawn outlines — …reason").

### 23.6 S10-E ".env removed, Credential Manager only"

Python contract: `scratchpad/mr/ENV_API.md` (Agent H; secrets only from the Windows Credential Manager, tunables from
`backend/data/settings.json` per backend, strict loading, no `.env` read anywhere; the process-environment
deployment switches stay); `core/llm.py` reads no environment (§22.13, ENV_API §9).

**Python part (Agent H; labels `stage10env`, `stage10lock`, `stage10git`; details ENV_API.md + ENV_API_DEFAULTS.json,
LOCK_API.md).**
- No backend reads `.env` or a secret from the environment (guard tests in all three backends). Keys: Windows
  Credential Manager only — HedgeFund `strategy_platform/secrets.py`, GM / LSA `credstore.py` (byte-identical); ONE
  shared `openrouter-api-key` (= the existing HedgeFund entry). Non-secret settings: optional
  `<backend>/data/settings.json` via `settings_file.py` (byte-identical ×3; strict — an invalid file refuses the
  start; the AI fields are re-read per call and an invalid file raises `settings_file.SettingsError` from
  `newsroom.ai._current_settings()` / `analysis_models()`, which `core/llm` turns into `LlmUnavailable`). LSA
  `youtube_url` is written by the LiveStream Agent window (atomic write + backend restart). GET /v2/settings/keys
  without `env_names` / `env_value_ignored`; POST /v2/settings/keys/changed also audits the GM / LSA key names
  (`secrets.OTHER_MODULE_SECRETS`); `ImportSpecsMatchSchemasTest` ties W's `BackendSettingSpecs.cs` to the three
  `config.SCHEMA` tables. python-dotenv removed; `.env.example` → `.env.example.stale`; READMEs updated.
- **Pinned dependencies:** `make_lockfile.py` (×3, identical) writes `requirements.lock` (+ HedgeFund
  `requirements-broker.lock`) with `==` pins and sha256 per package; `run_*.ps1` install only
  `--require-hashes -r requirements.lock`; "Update Python packages" = `make_lockfile.py --update` + install. The user
  runs `make_lockfile.py` once per backend (no PyPI from the agent machines).
- **Root .gitignore:** `/docs/` (root only) + `/UFOS.ai/docs/todo.md`; `HedgeFund/docs/` and
  `UFOS.ai/docs/ai-budgets-spec.md` versioned; global `*.stale` and `*-backup/`.
- **Docker removed (user 2026-10-24 16:56):** Dockerfile + .dockerignore ×3 and docker-compose.yml → `*.stale`; every
  backend listens on 127.0.0.1 only (ALLOW_REMOTE_ACCESS / WS_HOST / HTTP_HOST removed).
- `tools/check_no_env_cs.py`: the exemptions are the exact UFOS.ai-relative paths of the importer only
  (`EnvMigration.cs`, `EnvMigrationDialog.cs`, `BackendSettingSpecs.cs`); `scan(root, prefix)` so a HedgeFund-only
  checkout matches them too.
- Test isolation: `test_fund_api` / `test_s10_security` drop a stubbed `newsroom.ai` with `_drop_module()` (pop +
  the parent package attribute), so later imports never see the stub.

**WPF / C# part (Agent W; labels `stage10envui`, `stage10lockui`, `stage10secui` amended).**
- One Credential Manager layer for every WPF module: `Shared/CredentialVault.cs` (write / delete / state / constant-time
  compare / stored length / ReadValue — the latter only for code that must send a key itself: the main app's OpenRouter
  call). `Shared/CredentialCatalog.cs` lists ALL credentials (ENV_API §2): the Hedge Fund names unchanged, shared
  `openrouter-api-key` (target `UFOS.ai/HedgeFund/openrouter-api-key`, user decision 16:25), `opensky-client-id/-secret`
  (`UFOS.ai/GlobalMonitoring/…`), `deepgram-api-key` (`UFOS.ai/LiveStreamAgent/…`), retired kraken-demo-* (removal
  only). HedgeFund `Platform/CredentialStore.cs` keeps its API and delegates to the vault.
- ⚙ Keys & data lists every credential with "Used by: …"; the "found in .env" status / legacy-name line are gone
  (GET /v2/settings/keys without env_names / env_value_ignored).
- Main app: `Services/EnvConfig.cs` retired (.stale); `OpenRouterClient` reads the shared key from the Credential
  Manager and base URL / model from `%LOCALAPPDATA%\UFOS.ai\app-settings.json` (`Shared/MainAppSettings.cs`: optional,
  strict — unknown key / wrong type / key-like name → readable error; defaults = the former code defaults).
  `UFOS.ai/.env.example` retired (.stale).
- One-time migration "Import from old .env files…" (`Platform/EnvMigration.cs` + `EnvMigrationDialog.cs` +
  `BackendSettingSpecs.cs`) — the ONLY code that reads a .env: discovers every .env (UFOS.ai folder recursively without
  bin/obj/.venv/node_modules/.git, plus a .env in the repo root), ONE dialog with names only (keys: new / same / differs
  → keep or replace / several values → pick a file / unreadable stored → replace; settings: moved only when different
  from the code default; process switches dropped; unknown names dropped unless ticked "keep in file"); apply writes
  each key and verifies by read-back (equal value + equal byte length), writes settings atomically and re-reads them,
  then rewrites each .env without the handled lines or DELETES it when nothing is left; no backups; result shown in the
  dialog, log lines + POST /v2/settings/keys/changed "imported" per imported key (names only).
- LiveStream Agent window: "YouTube URL" field + "Save & restart" (validates https + youtube.com / www / m / youtu.be,
  no user info, ≤ 500; writes backend\data\settings.json youtube_url atomically via `Shared/BackendSettingsFile.cs`,
  restarts the backend); shows the stored value / why it is missing at window load.
- Texts: "set it in ⚙ Keys & data" (ArticleWindow, Economic calendar, Insider trades, key dialogs).
- Guard: `HedgeFund/tools/check_no_env_cs.py` + `backend/tests/test_s10_env_cs_guard.py` — no C#/XAML/csproj reads a
  .env path, a dotenv library, EnvConfig or a secret-like environment variable (EnvMigration files excepted).

**Lock protocol (C#).**
- `Shared/PythonEnvironment.cs`: stamp = SHA-256 of requirements.lock (requirements.txt before the first lock); install
  only `pip install --require-hashes -r requirements.lock`; missing / no source line / stale lock → stop with the
  same texts as Get-LockProblem (exact make_lockfile.py command). `Services/PythonEnvManager.cs`: same stamp; status
  names the lock problem; "Update Python packages" = run_*.ps1 -Upgrade -EnvOnly (H's scripts: --update + lock install).

**L6 follow-up (S10-A2).**
- `Shared/RotatingLogFile.cs`: process.log of Global Monitoring and the Hedge Fund backend rotate at 5 MB, 3 old files;
  the detached HF backend's -LogFile is rotated before each start; a failed rotation is written into the log.

### 23.7 Open decisions / follow-ups (from the batch notes)

Status 2026-10-24 (lead): CLAUDE.md — RESOLVED (updated by the lead); README — IN PROGRESS (the lead); Docker —
RESOLVED (removed, Agent H, §23.6); lock files — RESOLVED (`make_lockfile.py` exists; the user runs it once).

- S10-E (W): 1. CLAUDE.md still says secrets live in `.env` (lines ~64, 70, 97, 100) — **resolved: updated by the lead**.
  2. Repo-root README.md setup steps 2 (copy backend/.env.example) — needs: "Keys: ⚙ Keys & data (Windows Credential
     Manager); settings: backend\data\settings.json (optional)". Outside my labels — **in progress: the lead edits the README**.
  3. POST /v2/settings/keys/changed must accept the GM/LSA catalog names (opensky-*, deepgram-api-key) or those imports
     show "not recorded in the backend audit log" (H).
  4. BackendSettingSpecs = defaults transcribed from today's config.py (ENV_API_DEFAULTS.json not there yet) — H please
     confirm keys/types/defaults match the settings schemas.
  5. Docker — **resolved: Docker was removed (user decision 2026-10-24 16:56, Agent H)**.
  6. CODEMAP §4.1 (secrets) — H's names; I updated the Shared / Platform rows only.
- S10-A1: M3 lock files — **resolved** (`make_lockfile.py`; the user runs it once); L7 (old `fund/` wording — goes with S10-C); process.log cap (done WPF-side,
  §23.6 L6); GM / LSA need a start on the PC (their venvs cannot run on the agent VM).
- S10-A2: the startup notice body (`Shared/AiDisclaimerWindow.cs`, "No investment advice: …") is user-mandated —
  user decision whether it follows the Stage 10 wording.
- S10-U: MainWindow, Global Monitoring, LiveStreamAgent, `AiDisclaimerWindow.cs` keep their own notice wording outside
  `stage10ui` — a later label if the user wants them changed.

