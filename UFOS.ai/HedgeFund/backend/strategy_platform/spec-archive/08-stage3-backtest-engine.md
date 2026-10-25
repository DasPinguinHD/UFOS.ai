# Strategy Platform SPEC — archive: 08-stage3-backtest-engine

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

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
