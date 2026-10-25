# Strategy Platform — implementation contract (CURRENT)

> Part of UFOS.ai, a proof-of-concept portfolio project. Paper trading by default, not investment advice. Plan and
> decisions: project doc `claude/strategy-platform-plan.md`. Change this file together with the code.
>
> **How to read this file (DOCS-SLIM, 2026-10-24):** it states what is TRUE NOW — data model, routes, behaviour,
> limits and the user decisions still in force. Decision history, review rounds, superseded revisions and as-built
> narratives live verbatim in `spec-archive/` (one file per stage). Section numbers (§N.M) are stable — code, tests
> and comments cite them; a section that moved leaves a one-line stub `→ spec-archive/<file>`.

## 0. Ground rules (all stages)

- **Scope:** agents change only files inside `UFOS.ai/HedgeFund/` unless a scope label allows more; check with
  `python HedgeFund/tools/scope_guard.py check --label <label>`.
- **No unrequested fallbacks** (CLAUDE.md): a failing source, a missing key or a used-up budget STOPS the affected
  function with a `core.errors.UserFacingError` subclass whose `user_message` the UI shows — no alternative source,
  no stale value as if current, no default, no cheaper model, no rule-based stand-in. Accepted fallbacks carry
  `# FALLBACK (accepted by the user <date>): …` at the code site (list §6).
- **Security by default:** every route behind `api_auth` (session token + Host check, loopback only); secrets only
  from the Windows Credential Manager (`secrets.py`), never logged / returned / written by the backend; all input
  validated; audit-first for user actions (refused when the audit row cannot be written; switching OFF always works).
- **Python:** 3.14 Windows venv (installed only from `requirements.lock` with `--require-hashes`); offline tests also
  on 3.10 without third-party packages → lazy imports of `alpaca`, `pandas`, `aiohttp`, `openai`, `pyarrow`; stdlib
  `unittest`, temp dirs, fakes; never the real DB, the network or real credentials.
- **Time:** UTC ISO-8601 (`timespec="seconds"`); market logic in `America/New_York` (`zoneinfo`).
- **No licensed market data or personal data in the repository** (lead 2026-10-25): real provider data (Alpaca /
  Tiingo / EODHD bars, quotes, news) and personal data (broker exports, IBANs, account numbers) are never committed,
  not even as test fixtures. Tests use synthetic, seeded data; a real-data variant reads a git-ignored file under
  `backend/data/local_fixtures/` and skips with a clear reason when it is absent. Before every commit
  `python HedgeFund/tools/check_repo_data.py` (stdlib, read-only git) must say OK — it scans what would be committed
  (tracked + untracked-not-ignored) for data files outside `vendor/`, price-series-like JSON / .py / .txt, Alpaca bar
  records, IBANs with a valid checksum, Alpaca account numbers, the `tr-import` folder and `vendor/` files whose
  sha256 differs from their MANIFEST; reviewed false positives in `HedgeFund/tools/repo-data-allow.json` (path + rule
  + reason).
- English code/comments/UI; AI labelling + cost ledger rules of CLAUDE.md apply to every LLM output; user-facing
  notices come from `core/notices.py` (§23.2).

## 1. Package layout

```
strategy_platform/
  core/       errors, instruments, calendars (NYSE + crypto, ONE source), clock, params, types, llm, notices,
              venv_guard, tls, fixture
  secrets.py  Windows Credential Manager reader (§2)
  data/       registry, http_gate, cache, pit, validation, licences, seam, market_feed, providers/*
  storage/    db.py — hedgefund.db (§5)
  api/        routes.py (/v2 core) + trading_routes, broker_routes, monitoring_routes, options_archive_routes,
              council_routes
  strategy/   DSL, compiler, library, features (§7)      lifecycle/  registry, gates (§7.10, §9)
  backtest/   engine v2, jobs, explain (§8, §11)        validation/ gates, stats, parallel workers (§9, §10)
  research/   studio, memory, papers, committee (§13, §14)   discovery/  loop (§16)
  portfolio/  decision, allocator, allocation, netting, ledger, checks, risk, settings, views (§17)
  execution/  paper_store, paper_broker, accounts, oms, control, migration (until R4), brokers/* (§17, §18)
  live/       runtime, incubation (§17)                 monitoring/ alerts, decay, reports, insights, shadow (§19, §20)
  options_archive/ OS1 archive process (§21)            council/    the Council of AIs strategy (§22)
```

### 1.1 Calendars and clock

- `core/calendars.py` is the ONE source of NYSE / crypto session logic (sessions, holidays, early closes, funding
  windows; full export list in `calendars.__all__`). NYSE years **2000–2030**: 2000–2015 from the historical rules +
  special closures (§8.9), 2016–2027 hardcoded published rows, 2028–2030 from the current NYSE rules (§23.2).
  Outside → `CalendarUnavailable(UserFacingError)` (no weekday guess); `last_close_before` raises instead of now.
- `core/clock.py`: `Clock`, `FixedClock` (tests), `SYSTEM_CLOCK`, `resolve`, `to_utc`, `iso`.

## 2. Secrets

`secrets.py` (Credential Manager only — no `.env` exists since S10-E, §23.6). Generic credential, TargetName
`UFOS.ai/HedgeFund/<name>`, blob = UTF-8 value, persist LOCAL_MACHINE; invalid UTF-8 / NUL → `SecretStoreUnavailable`
("… stored in an unreadable format — set it again in ⚙ Keys & data"). Written ONLY by the WPF
(`Shared/CredentialVault.cs`); the backend reads at use time (a changed key applies without restart).

| name | used for |
|---|---|
| `openrouter-api-key` | all AI calls (shared by every module) |
| `fred-api-key`, `sec-edgar-user-agent` | newsroom collectors (FRED, SEC EDGAR) |
| `alpaca-paper-key-id`, `alpaca-paper-secret-key` | market data + the Alpaca paper broker |
| `tiingo-api-key` | pre-2016 history (§8.1) |
| `binance-demo-api-key`, `binance-demo-api-secret` | Binance demo broker (§18.14) |
| `eodhd-api-key` | price source (§19 / §20) |
| `kraken-demo-*` | RETIRED (`RETIRED_SECRETS`: keys/changed accepts them for "removed" only, §23.3) |

GM / LSA names (`opensky-*`, `deepgram-api-key`) are audited by keys/changed too (`OTHER_MODULE_SECRETS`).
Collectors gate every poll on their key (`newsroom/keys.wait_for_secret`, re-check every 60 s); `keys.redact` masks
key values / `api_key=` in error texts.

## 3. Data layer

- **One configured source per data type, no fallback chains** (`data/registry.py`): equity / crypto bars + quotes,
  news, corporate actions → `alpaca`; macro → `fred`; insider → `sec_edgar` (newsroom collectors); pre-2016 history
  via the Tiingo seam (§8.1). `registry.fetch` records the source status after every call; `test_source("alpaca")`
  backs `POST /v2/data/test`.
- **Alpaca** (`providers/alpaca.py`, lazy alpaca-py, raw JSON dicts): keys read per call; stocks `feed="iex"`,
  `adjustment="all"`; history from 2016 (earlier → `HistoryNotAvailable`); crypto symbols mapped via
  `instruments`; error mapping 401/403 → keys rejected / plan message, 429/5xx/timeouts retried by the gate, other →
  `DataSourceUnavailable`; malformed data → error, never skipped. SDK retry off, 20 s timeout per request.
- **HTTP gate:** one token bucket per source; Alpaca 150 req/min, burst 10 (fixed split with the options archive's
  own 30/min, §21.1); wait ≤ 120 s; retries only for 429/5xx/timeouts of the same call (3 attempts, 1 s / 3 s,
  `Retry-After` ≤ 10 s).
- **Cache** (`bars` table): only bars with `known_at <= now`; explicit freshness (`require_fresh_through`) — the
  last session's bar must exist or `DataSourceUnavailable` (stale bars never returned as current); adjusted history
  refetched whole when the newest/first cached bar changed (no mixed adjustment bases); windowed fetches,
  `RequestTooLarge` beyond `MAX_REQUEST_RANGE`.
- **Point-in-time:** `known_at` = bar end (daily equity = that session's close incl. early closes); `bars_as_of`.
- **Validation:** `validate_bars` reports `duplicate`, `non_positive`, `high_below_low`, `ohlc_outside`, `gap`,
  `calendar_unavailable`, `return_outlier` (> 8 σ), `stale` — findings only, nothing repaired.
- **Licences:** alpaca `personal_use`, fred `unknown`, sec_edgar `commercial_ok` (stored per source row).
- Full stage-1 text (provider dict shapes, cache windows, gate numbers history): → spec-archive/01-stage1-foundation.md

## 4. API v2 (core)

All `/v2/*` behind `api_auth`; handlers `(query, body, match) -> (status, payload)`; bodies must be JSON objects,
unknown fields → 400 `{error, errors}`; `UserFacingError` → 503 `{error: user_message, kind}`; never a stack trace.
- `GET /v2/settings/keys` → `{"keys": [{name, label, target, present, error}]}` (never values).
- `POST /v2/settings/keys/changed {name, action: set|removed|imported}` → audit `key_set|key_removed|key_imported`
  (a `value` field → 400).
- `GET /v2/data/sources`, `POST /v2/data/test {source: "alpaca"}` → `{ok, checks: [{name, ok, message}]}`.
- Stage routes: §7.11, §8.5, §9, §10, §11, §13–§14, §16, §17.10, §18.9, §19.10, §20, §21.11, §22.11; the main state
  payloads carry `"mode": "normal" | "sandbox"` (`MODE_PATHS`, Sandbox profile).

## 5. Storage — hedgefund.db

`backend/data/hedgefund.db`, WAL, `PRAGMA user_version` migrations (**v26**), corrupt file quarantined
(`*.corrupt-<ts>`) and recreated; every failure → `StorageUnavailable(UserFacingError)`, "not found" → `None` / `[]`,
bad arguments → `ValueError` — nothing swallowed. Times normalised to UTC ISO. Connection per call (asyncio callers
use `asyncio.to_thread`). A consistent backup is written before the v23 and v25 migrations
(`data/backups/hedgefund.db.pre-v<N>-<UTC>`, `*.tmp` leftovers never count); meta `schema.created_version` records
the version a file was created at.
- Tables by stage: v1 `meta`, `audit`, `data_sources`, `bars`; v3 `bars.adjustment`, `corporate_actions`,
  `seam_checks`; v4 `volumes`; v5+ strategies / backtests / validations / studio / memory / papers / committee /
  loops (§7–§16); stage 7 `pf_*`, `pa_*`, `oms_*`, `sl_*`, `rt_*`, `incubation_*` (§17); stage 8 `br_*` (§18);
  stage 9 `mon_*`, `sh_*` (§19, §20); v25 `cl_*` (§22.3); v26 unique legacy `old_id` indexes (§22.14).
- Core API: `init/close/is_open/db_path/storage_info`, `meta_get/meta_set`, `add_audit` (actors user, engine,
  system, data) / `list_audit`, `data_source_*`, `upsert_bars` / `query_bars` (one source + one adjustment series,
  point-in-time `known_by`), `latest_bar_ts`, corporate actions, seam checks, volumes, backtest run cards (insert +
  guarded transitions only). Function table: → spec-archive/01-stage1-foundation.md §5.1
- The old engine's account stays in newsroom.db until R4; §17.9 copied it into `paper-council`.

## 6. Accepted fallbacks (user decisions 2026-10-02, still in force)

Each carries `# FALLBACK (accepted by the user 2026-10-02): …` at the code site:
- Economic Calendar without the FRED key shows the hardcoded FOMC dates ("… Showing FOMC dates only.");
  per-release `fred/release/dates` request when the bulk answer has gaps.
- `core/llm.py`: the pre-call estimate is booked when OpenRouter reports no cost; code-default model ids only when
  `newsroom.ai` cannot be imported (offline tests — S10-E: no environment read, an invalid settings file raises).
- A non-https OpenRouter base URL → the default https URL (`config.safe_openrouter_base_url`).
- SEC EDGAR: a failed later feed page keeps the entries already loaded; unparsable Form 4 details → the feed title.
- Change detection: the judge re-uses a recent AI decision when nothing material changed (`source="carried"`, the
  original judgement time).
Everything else was removed (rule judge stand-in, valuation at stored prices, macro on stale FRED, premium stand-in,
unscaled sizing, empty newsroom accessors, …) — list and reasons: → spec-archive/01-stage1-foundation.md §6

## 7. Stage 2 — strategy definition

Full text (interview, B1/B2 notes, review fixes): → spec-archive/07-stage2-strategy-definition.md

- **Decisions in force:** universe = liquid US ETFs + crypto; JSON editor + file import; the rule part of the agent
  committee is the library strategy "Council Rules (no AI)" (`agentic_committee_rules`); any syntactically valid
  symbol is allowed (bounded by 500 symbols + the HTTP gate); imported files are untrusted (state `draft`, claims
  shown as "claimed by the file", texts marked "external text · unverified"); numpy allowed (lazy import).
- **§7.1 Layout:** `strategy/` limits, schema, dsl, validator, evaluate, panel, portfolio, sdk, universe, summary,
  preview, `features/`, `alphas/`; `lifecycle/registry.py`.
- **§7.2 Limits:** text 262 144 bytes, depth 32, nodes 2000, window 2520, lookback 5040, symbols 500, params 32,
  features 64, tags 16; name 80, tag 32, description 4000. `parse_spec_text` rejects duplicate keys, NaN/Infinity,
  non-object roots, lone surrogates (`parse_error` with line/column; size → `limit_exceeded`). Data only — nothing
  executed.
- **§7.3 StrategySpec v1** (`"schema": "ufos.strategy/1"`): name, description, tags, category, universe (preset or
  symbols), timeframe `1Day`, parameters {value, min, max, step, integer}, features (named sub-expressions, no
  cycles), signal, portfolio {mode long_only|short_only|long_short, selection top_n|bottom_n|top_bottom_n|threshold|all,
  weighting equal|signal|inverse_vol, vol_lookback, max_weight (0,1], gross_exposure (0,3]}, rebalance
  daily|weekly|monthly, risk {stop_loss_pct, take_profit_pct, vol_target_annual, max_drawdown_stop_pct}, execution
  {market, next_open|next_close only}, costs paper_broker|custom. Unknown keys → error; defaults written into the
  stored spec (`with_defaults`); `content_hash` = sha256 of the canonical JSON without provenance.
- **§7.4 DSL:** literals, `param`, `ref`, `field` (OHLCV, vwap, trade_count), `ext`, `op`. Element-wise ops (add …
  fill_nan, exp, tanh, nan), time-series ops per symbol (lag, delta, pct_change, ts_* family, ts_corr/cov,
  decay_linear, ewm_mean, returns, rsi, atr, sma), cross-sectional (cs_rank 0..1, cs_zscore, cs_demean, cs_scale,
  cs_winsorize). **Look-ahead ban:** windows/shifts ≥ 1, no op reads after t, timing next_* only, `ext` only
  point-in-time providers. `dsl.catalogue()`, `expr_text()`.
- **§7.5 Validator:** `validate(spec) -> {ok, errors, warnings, facts}`; stable codes (unknown_key, missing_key,
  bad_type, out_of_range, unknown_op, bad_arity, bad_window, look_ahead, unknown_param/ref/field/ext, ref_cycle,
  not_point_in_time, bad_symbol, too_many_symbols, limit_exceeded, bad_timing, text_too_long, parse_error);
  warnings unused_param/feature, mixed_calendars, survivorship, short_history, crypto_short_selling.
- **§7.6 Provenance:** the system owns the envelope (`origin` library|user|import|duplicate, `created_by`,
  `parent`, `ai_generated` false|true|"unknown", `external_text`, `claimed`, `source_file` {name, sha256, bytes});
  external texts are plain text everywhere and delimited as untrusted before any LLM sees them; editing creates a
  new immutable version and returns the strategy to `draft`; library strategies are read-only ("Duplicate as draft").
- **§7.7 Evaluation:** `Panel` (NYSE sessions; crypto-only on UTC days; mixed: crypto value known by the session
  close and within 24 h); `evaluate` deterministic and causal (property-tested: `panel[:t+1]` gives the same row t);
  cost check `MAX_EVAL_CELL_OPS` 2e10 → `EvaluationTooExpensive`; `portfolio.target_weights` (excess over max_weight
  held as cash, ties by symbol); preview ≤ 60 s, fresh bars required, symbols without history listed in
  `symbols_missing`.
- **§7.8 External features:** `macro.regime_score` (the macro agent's score from FRED first releases / ALFRED
  vintages, `ext_observations`), `calendar.event_risk` (CPI / jobs / GDP: release day + the trading day before;
  FOMC: release day only — no look-ahead). Day d uses data published ≤ d − 1; failures raise, no stand-in.
- **§7.9 Alpha library:** 30 entries — 15 classic (TS / XS momentum, dual momentum, reversal, 52-week high,
  SMA 50/200, Donchian, RSI(2), Bollinger, low vol, inverse vol, sector rotation, risk-on/off, crypto trend,
  vol-targeted trend), 14 Alpha101 (#2 #3 #4 #6 #9 #12 #13 #14 #16 #33 #41 #53 #54 #101) and Council Rules (no AI);
  each validates, passes the causality property, finite weights.
- **§7.10 Lifecycle:** `strategies`, insert-only `strategy_versions`, `strategy_transitions`; states
  `idea draft backtested candidate validated incubation live scaled retired`; user moves idea↔draft, any → retired,
  retired → draft; upward states need evidence (§8 run cards, §9 gates); live / scaled need the live guard (§18);
  reason ≤ 500 + audit per transition; library seeded idempotently at start (`library_status`).
- **§7.11 API:** `GET /v2/dsl/operators`, `GET /v2/universes`, `GET /v2/strategies[?state&origin&category]`,
  `GET /v2/strategies/{id}`, `GET …/versions/{v}` (spec, envelope, validation, summary), `POST
  /v2/strategies/validate {text}`, `POST /v2/strategies {text, origin, file_name?, file_sha256?}` → 201,
  `PUT /v2/strategies/{id} {text, base_version}` (stale → 409, library → 409, identical → `unchanged`),
  `POST …/duplicate`, `POST …/transition {to, reason}`, `POST …/preview {version?, days 1..5}`,
  `GET …/versions/{v}/export`. Body limit 4 × MAX_TEXT_BYTES + 4 KB (JSON-escaped text).
- **§7.12 WPF:** navigation rail; Library (Board per state, Alpha library table, detail pane, Edit / Duplicate /
  Move to… / Preview / Export); JSON editor with live validation; import with size limit + untrusted-file
  confirmation. §7.13 / §7.14 (B2 / B1 implementation notes) → archive.

## 8. Stage 3 — backtest engine v2

Full text (interview, 3b–3e notes, the removed 2026-10-05 refresh design, review fixes):
→ spec-archive/08-stage3-backtest-engine.md

- **Decisions in force:** $100,000 default capital (1,000–100,000,000); benchmark per request / spec key
  `benchmark` (not default-filled) / else SPY; raw split-adjusted prices + dividends as cash like the paper account;
  equity orders capped at **1 % of the 20-day average dollar volume** (excess dropped, missing volume → order
  refused); **no participation limit for crypto** (user 2026-10-03, warning `crypto_no_participation_limit`).
- **§8.1 Data:** historical daily equity bars from **Alpaca SIP** (`equity_bars_history`, source `alpaca_sip`,
  request end clamped to the last session close ≥ 15 min ago — never IEX instead); the live paper engine keeps IEX
  (source `alpaca`), never mixed. Two series per symbol and day: `all` (signals) and `split` (fills, valuation,
  volume). Before **2016-01-04** (the seam) on demand from **Tiingo** (`equity_bars_pre2016`, key `tiingo-api-key`,
  only dates < seam stored, gate 40/h + 800/day sliding windows persisted in meta `http_gate/tiingo/requests`; 429
  stops at once). Dividends / splits: Alpaca ≥ seam, Tiingo < seam (`corporate_actions`).
- **§8.2 Seam:** overlap = first 60 sessions from 2016-01-04; daily returns must agree (|Δr| ≤ 0.002 on ≥ 95 % of
  days, no day > 0.02, ≥ 50 returns); factor = median(alpaca/tiingo close), spread ≤ 0.5 %; re-checked against the
  current Alpaca overlap at every call (`seam_checks` row on change). Fail → no pre-seam data for that symbol +
  warning `seam_mismatch` (never guessed). Volume: SIP consolidated from the seam on, Tiingo before (seam volume
  check |r − 1| ≤ 25 % on ≥ 90 % of days, |median − 1| ≤ 10 %; mismatch → `seam_volume_mismatch`). Validator warning
  `absolute_price_level` + run warning `absolute_level_two_sources` for specs comparing price levels to constants.
- **§8.3 Engine** (`backtest/engine.py`, `ENGINE_VERSION "sp-backtest/3c.1"`): drives the REAL `PaperBroker` (now
  `execution/paper_broker.py`) on an in-memory store with a simulated clock → identical fees, slippage, Reg T margin,
  borrow, Rule 201, dividends, crypto perps. Per day: accruals → orders of the previous decision (next_open = open +
  6 min phase, next_close = close − 1 min) → margin; decision at the close from data known then. Spec risk block
  enforced (stop / take-profit flat until next rebalance, vol target only de-levers, drawdown stop halts the run);
  warm-up = max(max_lookback, 20, 61 with vol target) + 5 sessions. Deterministic. **Parity test:** fills, ledger and
  positions equal a normal PaperBroker run fed the same quotes.
- **§8.4 Metrics / run card / stress:** CAGR, vol, Sharpe / Sortino / Calmar (zero rate), max drawdown + duration,
  hit rate / profit factor daily AND per trade (FIFO lots), turnover, exposure, alpha / beta, costs (fees, slippage,
  borrow, funding, % of gross P&L when > 0), signal quality (rank IC h = 1/5/10/21/63, ICIR, decay + half-life; < 5
  symbols → rolling 13-week positive share), capacity (q90 participation). Run card `backtest_runs` (v5; insert +
  guarded status transitions only; finished cards immutable; `survivorship` warning always). Stress (on request):
  episodes dot-com 2000-03-24→2002-10-09, GFC 2007-10-09→2009-03-09, Q4 2018, Covid 2020-02-19→2020-03-23, rate shock
  2022, crypto 2021-11-10→2022-11-21 (warm-up clamped to 2000-01-03, `short_warmup`); costs ×2 / slippage ×3 per asset
  class; Monte-Carlo circular block bootstrap, 1,000 paths, seed 20261003 (p5/p50/p95 drawdown + CAGR).
  `draft → backtested` needs a `done` card of the current version.
- **§8.5 Jobs + API:** ONE backtest at a time (409 `{error, run_id}`); progress phases queued/data/simulate/metrics/
  stress/saving (memory only); cards left queued/running at start → failed "Backend stopped during the run".
  `POST /v2/backtests {strategy_id, version?, start?, end?, benchmark?, capital?, stress?, history_from?: "2016" |
  "2000"}` → 202 `{run_id}` (Tiingo key checked at once only when equity data before the seam is loaded → 503
  SecretMissing); `GET /v2/backtests?strategy_id&limit` (1–200); `GET /v2/backtests/{id}` (card + progress, range,
  metrics, curve, trades, warnings incl. `many_late_starts`, stress, data, licences, cost_model);
  `POST /v2/backtests/{id}/cancel` (202 cancelling / 200 cancelled / 409 finished).
- **§8.6 WPF:** Run backtest… dialog (range, "From 2000", benchmark, capital, stress) → progress; BacktestReportView
  (equity vs benchmark with the seam marked, drawdown, metrics, IC decay, stress, warnings, data / licences).
- §8.7–§8.11 implementation notes, §8.12 (refresh / end limit / `wait_for_latest` — REMOVED 2026-10-06; persistent
  Tiingo gate counters KEPT) → archive.

## 9. Stage 4 — validation gates

Full text (interview, V1 / V2 notes, statistics choices, report shape, review fixes):
→ spec-archive/09-stage4-validation-gates.md

- **Decisions in force:** gates are **hard with a documented exception** (G1–G8 only, reason ≥ 30 chars); preset
  **Standard** (Lenient / Strict selectable, overrides in settings); two steps — **Validate…** (all gates, no
  holdout, repeatable → `candidate`) then **Open holdout (once)** with the typed strategy name (→ `validated`); the
  holdout opens **once per strategy family**; every finished backtest / validation / holdout run of a family member
  is a **trial**; normal runs end before the holdout window; a family that saw the window is **contaminated**; a
  family that FAILS the holdout is locked until ≥ 12 new unseen months exist, then one re-test on those months only.
- **§9.1 Families / holdout window:** family = root strategy + duplicates / imports of its exports + any NEW
  strategy whose logic hash (spec without name / description / tags / provenance) equals an existing version's
  (`strategy_logic`, v7); window = last complete session − **24 months** (equities / mixed) or − **12 months**
  (crypto-only); `seen_until(family)` = max simulated end of all family runs; explicit start/end inside the window
  → 400 `HoldoutWindowViolation`; stress episodes inside → "not available". Opening sets seen_until = window end and
  writes an insert-only `holdout_events` row (counter: other families that opened an overlapping window of the same
  universe).
- **§9.2 Trials:** `trial_number` set once at finish (family trial count); deflated Sharpe uses N = trials + 1 and
  the trials' Sharpe variance floored at 1 / years (Bailey & López de Prado 2014; Lo 2002).
- **§9.3 Validation run:** training backtest (end = window start − 1) → K = 5 walk-forward folds (fixed spec) →
  parameter grid from min/max/step (≤ 64 configurations, base forced in) → stress (costs ×2, slippage ×3, episodes
  outside the window) → gates → run card kind `validation` (`metrics_json.validation` {passed RAW, version
  "sp-validation/4.1", report}).
- **§9.4 Gates (Standard; Lenient / Strict):**

  | # | Gate | Pass rule |
  |---|---|---|
  | G1 | Consistency | rank-IC Newey-West t ≥ 2.0 (1.5 / 3.0) at the best of h 1/5/10/21 AND mean IC > 0 in ≥ 4 of 5 folds (3 / 5); time-series (< 5 symbols): Sharpe > 0 in ≥ 4 of 5 folds |
  | G2 | Alpha | alpha vs benchmark > 0 with NW t ≥ 2.0 (1.5 / 3.0) |
  | G3 | Signal decay | IC half-life ≥ mean holding period (no decay passes only with a significant 1-day IC; time-series: not applicable = pass) |
  | G4 | Deflated Sharpe | probability ≥ 0.95 (0.90 / 0.99) |
  | G5 | Overfitting | PBO (CSCV, S = 10 blocks) ≤ 0.30 (0.50 / 0.20); < 4 configurations → FAIL |
  | G6 | Robustness | grid neighbours within ±20 % keep ≥ 50 % of base Sharpe (40 / 60 %); costs ×2 (×2 / ×3) net Sharpe > 0; each stress episode DD ≤ 1.5 × benchmark DD |
  | G7 | Sample size | ≥ 100 closed trades (50 / 200) |
  | G8 | Capacity | ≥ 10 × capital (crypto-only: not applicable = pass; mixed: equity orders only) |
  | H | Holdout | net Sharpe > 0 AND ≥ 50 % of training Sharpe AND mean IC > 0 (time-series: Sharpe only); **no exception** |

  A metric that cannot be computed → fail with the reason (never a default pass). `candidate` needs all G1–G8
  pass/exception in the latest validation run of the CURRENT version; `validated` needs H pass. Exceptions belong
  to one run (insert-only `validation_exceptions`, audit `validation_exception`).
- **§9.5 Lifecycle + API:** `POST /v2/validations {strategy_id, version?, history_from?}` → 202 (shares the single
  job slot: 409 "A backtest or validation run is already running."); `GET /v2/validations[?strategy_id&limit]`;
  `GET /v2/validations/{id}` (validation and holdout cards, merged report); `POST …/{id}/exceptions {gate,
  reason}`; `POST …/{id}/cancel`; `GET /v2/strategies/{id}/holdout` (eligibility, window, unseen_from,
  eligible_from, can_open, blocked_reason); `POST /v2/strategies/{id}/holdout {confirm}` → 202 (wrong name 400, not
  eligible / no passing validation 409). Holdout card + event written in ONE transaction (`runcard.finish_holdout`).
  `GET/PUT /v2/settings/gates {preset, overrides}` (keys g1_ic_t … g8_capacity_multiple; meta
  `validation/gate_settings`; audit `gate_settings_changed`). Strategy rows carry chips family_id, contaminated,
  holdout_failed_until, trial_count, has_exception. Preview limited to 5 days (no peeking into the window).
- **§9.6 Calibration (acceptance):** planted AR(1) signal passes G1–G8 + H; ≥ 95 % of 40 noise seeds fail ≥ 1 gate;
  best-of-64 noise fails G4 or G5.
- **§9.7 WPF:** Validate… / Open holdout (once)… (disabled with the eligibility date), ValidationReportView (gate
  table, folds, grid heat table, PBO / DSR, stress, exceptions, holdout), chips `contaminated`, `holdout failed
  (until …)`, `exception`. §9.8 / §9.9 implementation notes → archive.

## 10. Robustness — parallel loading, parallel compute, resumable runs

Full text (R1 / R2 notes, error classification list, worker isolation details, review fixes):
→ spec-archive/10-robustness.md

- **Decisions in force:** adaptive data-loading concurrency (toggle; off → fixed 1–8); validation compute on all
  CPU cores but one; failed runs resumable (button) and optionally auto-resumed after a connection error (no time
  limit, at most **5** automatic resumes per run); a resumed run counts as ONE trial; compute workers get no network,
  no keys, no DB; resume only on an identical basis; checkpoints tamper-evident.
- **§10.1 Network + adaptive loading:** settings in meta `network.adaptive` (default true) / `network.fixed` (1–8,
  default 4); AIMD limiter per source (start 4, min 1, max 8; connection error → halve; 20 successes → +1) IN FRONT of
  the HTTP gate (rate limits unchanged: Alpaca 150/min, Tiingo 40/h + 800/day). An invalid stored value → every
  request raises `StoredSettingsInvalid` ("save them again in … Network") — no default. Run symbols load in a thread
  pool (results assembled in sequential order — identical; the earliest failing symbol's error wins). Corporate
  actions chunked ≤ 10 symbols × ≤ 3 years. Connection-type failures carry `kind == "connection"`
  (`network.error_kind`); anything with an HTTP status is never connection-type. `network.probe(source)` ≥ 30 s apart
  (alpaca: latest SPY trade; tiingo / FRED / SEC: the next real request is the probe).
- **§10.2 Parallel compute** (`validation/parallel.py`): `spawn` pool, workers = min(cpu − 1, memory guard ≤ 2 GB,
  tasks); 1 worker = inline. Workers get only in-memory data (snapshot of the parent's loader calls; missing →
  prefetch rounds ≤ 3); worker guard + audit hook block secrets, sockets, sqlite, subprocess, registry, `.env` /
  `*.db` / api-token / credential paths; a watchdog ends workers when the parent dies; `/shutdown` terminates pools.
  Grid + stress in the pool; pool == sequential report (tested). Plain backtests stay single-process.
- **§10.3 Resumable runs:** insert-only `run_checkpoints` (sha256 per step; mismatch → recompute + warning
  `checkpoint_mismatch`); run cards (v8) carry `resumed_from` (UNIQUE), `resume_count`, `error_kind`
  (connection | interrupted | basis | error), `auto_resume`. Resume = NEW card, allowed only for `failed` +
  `error_kind == connection` + identical basis (version, content hash, request, resolved end, holdout window,
  engine / report version, data hashes without volatile keys) — else 409 "The basis changed (…) — start a new run.";
  **a holdout run is never resumable**. Auto-resume: job `waiting` ("Connection lost — waiting for Alpaca (attempt
  n/5)…"), probe every ≥ 30 s, blocks the job slot, cancellable; restart → queued/running `interrupted`, waiting →
  `connection` (resumable).
- **§10.4 API + WPF:** `GET/PUT /v2/settings/network {adaptive, fixed}` (audit `network_settings_changed`);
  `auto_resume` on backtest / validation requests; `POST /v2/backtests/{id}/resume`, `POST
  /v2/validations/{id}/resume {auto_resume?}` → 202 `{run_id, resumed_from}`; rows carry resumed_from,
  resume_count, error_kind, auto_resume, resumable, status `waiting`. WPF: Network settings, the auto-resume
  checkbox, Resume button, waiting message + Cancel. §10.1.1 / §10.5 implementation notes → archive.

## 11. Explain — attribution (layer 1) + AI explanation (layer 2)

Full text (X1 / X2 notes, fact-id catalogue, number-check rules, error codes): → spec-archive/11-explain.md

- **Decisions in force:** layer 1 is deterministic (no AI, always shown); layer 2 only puts layer 1 into words;
  model selectable per click (Standard / Premium); the AI sees the numbers AND the strategy logic (via OpenRouter);
  explanations stored with the run (regenerate = new row, costs again); own daily budget; EN / DE; every number in
  the AI text is checked against layer 1 and unmatched ones are MARKED (never removed); runs finished before the
  attribution existed get none (no recomputation).
- **§11.1 Attribution** (`backtest/attribution.py`, `"sp-attribution/1"`, engine `"sp-backtest/3c.2"` records
  per-symbol P&L, `record_symbol_pnl` off in grid / stress runs): computed when a backtest / validation base /
  holdout run finishes; stored in `backtest_runs.attribution_json` (v9). Blocks (each may be `{unavailable}`):
  decomposition (OLS vs benchmark: market / alpha / residual; price P&L, costs, dividends, `unattributed`),
  by_symbol (top 10 / bottom 10 / others, reconciles), by_year, by_regime (benchmark trend 63 / vol 21 sessions,
  warm-up 63, no look-ahead), long_short, periods (best / worst 3 × 63 sessions, top 3 drawdowns with
  contributors), costs, signal. **Fact table** `facts: [{id, label, value, unit, display}]` with stable ids
  (`dec.*`, `sym.*`, `year.*`, `regime.*`, `ls.*`, `period.*`, `dd.*`, `cost.*`, `sig.*`, `run.*`; validation adds
  `gate_facts` / `gate.<G>.*`, `val.*`, report `"sp-validation/4.2"`). API cards carry `attribution` +
  `attribution_unavailable` (reason).
- **§11.2 AI explanation** (`backtest/explain.py`): `POST /v2/backtests/{id}/explanations` /
  `POST /v2/validations/{id}/explanations {model: standard|premium, language: en|de}` → 200 (90 s timeout, one at a
  time); `GET …/explanations` (list + explainable + estimate + budget). Via `core/llm.chat_json`, feature
  `sp-explain`, label "Hedge Fund · Explanations", temperature 0.2, max_tokens 2000. Prompt: only the facts, cite
  `[id]`, outside causes only in `hypotheses`, no advice, no parameter suggestions; strategy name / description /
  tags / claimed provenance are UNTRUSTED quoted data (truncated); never keys, other runs, or holdout facts in a
  non-holdout explanation; cap 40 000 chars (facts trimmed, else 409 `prompt_too_large`). Output schema {summary ≤
  600, sections ≤ 6 × {title, text ≤ 1500}, hypotheses ≤ 5, limits ≤ 5} — anything else 502 `bad_json` (cost
  booked). Unit-aware number check (EN / DE formats, %, $, scale words, citation binding, 0.5 % tolerance; allowed
  without a fact: integers 1–12, years 1990–2100) → `check {ok, unverified, unknown_ids}`. Budget
  `explain_daily_budget_usd` (default 1.00, 0 = off, max 50; spend per UTC day in meta, fail closed; a failed spend
  write pauses explanations until restart); `GET/PUT /v2/settings/explain`. Storage insert-only
  `run_explanations` (v10) + provenance (EU AI Act Art. 50). Errors `{error, code}`: 404, 409 wrong_kind / not_done /
  no_attribution / busy / prompt_too_large, 402 budget, 502 bad_json / llm_error, 504 timeout, 503 no_key / …
- **§11.3 WPF:** Attribution section in both reports (always); "Explain with AI…" panel (tier + cost estimate, EN /
  DE, "✦ AI-generated · <model> · <time>" header, unverified numbers red, hypotheses box, plain text only,
  Regenerate / earlier versions); budget in Settings. §11.4 decision (old runs: none), §11.5 / §11.6 notes → archive.

## 12. Market Research consumer

Market Research (`backend/market_research/`, contract `market_research/SPEC.md`) reuses this data layer; every
extension of `strategy_platform` code it needs is recorded in BOTH SPECs. In force:
- `data/http_gate.py` research priority (`research_priority()` contextvar): research takes its own token (60/min,
  hard cap 60 per sliding 60 s) and a shared token only while > 3 remain and no normal caller waits; concurrency
  slot only while no normal caller waits (≤ limit − 1); every page counts (`note_send`); a waiting research call
  yields (`ResearchYield`, no source error); > 30 s → "Research waited too long for the Alpaca rate limit (shared
  with trading)". Normal priority unchanged.
- `providers/alpaca.py`: `assets()` (active us_equity + crypto), timeframe `30Min` (Research only, never cached),
  news `images` (https, ≤ 5), `logo(symbol)` (PNG ≤ 256 KB; 401/403 → `LogosNotAvailable`).
- Research chart indicators use this DSL evaluator (`sma`, `ts_std`, `ewm_mean`, `rsi`, arithmetic) — operator
  semantic changes change the chart (`tests/test_mr_indicators.py`).
- `backtest/runcard.py` adds `fills_by_symbol` (≤ 10 000 fills, `fills_truncated`) to new cards' `trades_json` for
  the Research markers route. Full text: → spec-archive/12-market-research-consumer.md

## 13. Stage 5a — Strategy Studio + memory

Full text (5a backend / WPF notes, payload shapes, guard reasons, review fixes):
→ spec-archive/13-stage5a-studio-memory.md

- **Decisions in force:** own **ideation AI budget $10 / month, max $2 / day** (shared by Studio, papers and the
  committee; 0 = off, max 200); **every valid AI draft is backtested automatically** (each run is a trial — the
  Studio shows the family's trial count); **memory = computed facts only** (no AI-written lessons, no holdout
  results); the AI uses only the DSL and may propose missing operators as text ("unverified AI suggestion", never
  executed).
- **§13.1 Studio** (`research/studio.py`, `research/prompts.py`): insert-only `studio_sessions`, `studio_messages`,
  `studio_backtests` (queue event log), `studio_draft_saves` (v11 / v12). One session = one strategy family (first
  valid draft creates a strategy `origin="studio"`, `ai_generated: true`, envelope `ai_provenance`). `POST
  /v2/studio/sessions {title?}`; `GET /v2/studio/sessions`; `GET …/{id}` (messages, versions, runs, trial_count,
  queue, estimate, budget, `ai_interaction_notice` = "You are interacting with an AI system. … AI-generated and not
  verified." + the RESEARCH notice, §23.2); `POST …/{id}/messages {text ≤ 4000, model}` (sync, one Studio call at a
  time, 90 s, answer + at most ONE repair round); `POST …/drafts/{message_id}/cancel`; `POST
  …/drafts/{message_id}/save` (calling it IS the confirmation, fix1023); `GET /v2/studio/operator-requests`; `GET/PUT
  /v2/settings/ideation`. LLM `core/llm.chat_json` feature `sp-studio` (temperature 0.3, max_tokens 4000, prompt
  cap 80 000); budget checked BEFORE every call (month AND day) with a process-wide reservation shared by Studio /
  papers / committee; failed attempted calls charged; spend write failure pauses the Studio until restart.
  A valid draft is saved + backtested automatically only in states idea / draft / backtested — otherwise
  `requires_confirmation` ("demotes <state> to draft") and the save route. Queue ≤ 3 waiting drafts per session,
  FIFO when the job slot frees (also at backend start).
- **On-topic guard:** answer schema requires `kind: draft|clarify|off_topic`; deterministic `scope_guard` (reply /
  assumption / clarify length limits, 3rd clarify in a row, draft without spec, invalid final draft → only the
  validator card) and an **independent topic check** (Standard model, temperature 0, feature `sp-studio-guard`,
  sees only the AI's displayable texts) — failure / error / budget block → everything displayable discarded
  (`discarded: true`, `discarded_reason`, `topic_check`), cost shown, fixed non-AI scope note. The same check runs
  on paper extractions and committee role texts. No holdout facts in any Studio prompt; user text is quoted data.
- **§13.2 Memory** (`research/memory.py`): insert-only `memory_facts` written when a backtest / validation (not
  holdout) finishes — kinds metrics, contributors, periods, regimes, warning, gate_failed, validation_result,
  grid_flatness; deterministic text. Prompt block: own family (≤ 12) + related families (same universe +4, shared
  operators +1 each ≤ 3, recency); ≤ 20 facts, ≤ 6000 chars; shown to the user ("Memory used for this draft").
  `GET /v2/memory?family_id&strategy_id&limit`.
- **§13.3 WPF:** rail entry "Studio": sessions, chat (AI bubbles with "✦ AI-generated · <model> · <time>" + cost;
  discarded replies only as a neutral system note), Standard / Premium with estimate, budget line, draft cards
  (validator result, spec diff vs the stored version, operator requests, assumptions, trial count, backtest status
  + Open report), "Memory used" expander, pinned AI-interaction notice; ideation budget in Settings.
- §13.4 tests, §13.5 / §13.6 implementation notes → archive.

## 14. Stage 5b — paper → strategy + investment committee

Full text (backend / WPF notes, payload shapes, error codes, review fixes):
→ spec-archive/14-stage5b-papers-committee.md

- **Decisions in force:** papers by **PDF upload, pasted text, or an arXiv / SSRN link only**; **replicated** = our
  backtest over the paper's period has the same sign and is within **±30 %** of the reported Sharpe (else "not
  replicated", both numbers shown); the committee runs **automatically after a finished validation only when
  enabled** (default off), else a "Convene committee…" button; budget = the ideation budget (§13); the memo is
  advisory only — it never changes a strategy's state.
- **§14.1 Ingestion** (`research/papers.py`, `research/pdf_worker.py`): PDF ≤ 20 MB / ≤ 150 pages, text ≤ 200 000
  chars, links only `https://arxiv.org/abs|pdf/<id>`, `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=<n>` (exact
  host allow-list, redirects only inside it, SSRF-hardened fetch — private / loopback / link-local refused, 30 s,
  20 MB, gate source `papers` 6/min); SSRN login wall → "SSRN did not provide the PDF without login — download it in
  the browser and upload it" (no scraping). PDF → text in a separate `python -I` process (60 s, memory guard, no
  JavaScript / attachments / OCR; env scrubbed by `papers.worker_env()`), `pypdf` pinned. Paper text is UNTRUSTED
  (quoted data only). Stored: `papers` row + `data/papers/<sha256>.txt`; the temporary PDF is deleted right after.
- **§14.2 Paper → strategy:** AI extraction (feature `sp-paper`) with a ≤ 300-char source quote per field →
  **shown to the user for confirmation / edit before anything runs** (period and reported Sharpe are
  user-confirmed, required); not a testable strategy → fixed non-AI note (the AI reason is never returned). Confirm
  → Studio-rule draft (scope guard, topic check, validator, one repair) → strategy `origin="paper"` →
  **replication backtest over the paper's period** (clamped to our data and before the holdout window — shown;
  counts as a trial).
- **§14.3 Committee** (`research/committee.py`): roles Researcher → Quant → Risk Manager → Reviewer (devil's
  advocate) → Chair (memo {summary, arguments for / against, confidence 0–1 + reason, sizing % of capital + rules,
  stop rules, open questions}); inputs: strategy logic, gate facts, attribution facts, memory; holdout facts ONLY when
  the holdout was opened for this exact version (labelled `holdout.*`). Number check per message (§11.2), topic check
  per role (failed → fixed "not shown" text, run continues), EU AI Act label + cost per role; Standard default,
  Premium selectable; one committee at a time; cancellable between roles; insert-only `committee_runs`,
  `committee_messages`, `committee_events`; auto run skipped by budget → `skipped` run with "committee skipped:
  <reason>".
- **§14.4 API:** `POST /v2/papers` (multipart `file` | `{text, title?}` | `{url}`; same sha256 → existing paper);
  `GET /v2/papers`, `GET /v2/papers/{id}`; `POST …/{id}/extract {model}`; `POST …/{id}/confirm {extraction_id, model,
  sample_start, sample_end, reported_sharpe, …}`; `GET …/{id}/replication`; `POST /v2/committee {strategy_id,
  version?, validation_run_id?, model}` → 202 (409 busy / no_validation); `GET /v2/committee/estimate`;
  `GET /v2/committee`, `GET /v2/committee/{id}`, `POST …/{id}/cancel`; `GET/PUT /v2/settings/committee
  {auto_after_validation, model}`. Storage v13 (all insert-only).
- **§14.5 WPF:** Studio "Paper" tab (upload / link / paste, extraction review with sources, Confirm & build,
  replication card) and the committee panel on the validation report + strategy detail (estimate, progress per
  role, memo with unverified numbers marked). §14.6 tests, §14.7 / §14.8 notes → archive.

## 15. Stages 6–10 + central AI budget window — interview decisions (user, 2026-10-17)

Binding decisions still in force (detail per stage in its own section); full text → spec-archive/15-stages6-10-interview.md
- **Central AI budget window** (MainWindow): controls ALL AI budgets of all modules (HedgeFund fund/Council,
  explanations, ideation, loop, reports, newsroom caps; GM Assess; LSA) with a documented registration interface
  (`UFOS.ai/docs/ai-budgets-spec.md`).
- **Stage 6 loop:** default 6 variants × 4 generations (max 8 × 5); own loop budget; "fully autonomous" also
  validates finalists; the holdout is ALWAYS opened only by the user; every backtest is a trial (§16).
- **Stage 7:** inverse-volatility allocator with correlation penalty; incubation length chosen per run, then
  immutable; default limits 2 % daily loss (no new orders until the next day) and 10 % drawdown kill; netting; pre- /
  post-trade checks; OMS state machine; the old paper account migrated (§17).
- **Stage 8:** Alpaca paper (stocks / ETFs) and **Binance demo** (crypto; the Kraken demo was retired 2026-10-19,
  §18.13 / §23.3); IBKR / MT5 not built (adapter interface open); reconciliation halts on mismatch; live guard built,
  **live stays OFF** (stage 12 only on the user's explicit go) (§18).
- **Stage 9:** decay → strategy paused + the user decides; alerts as Windows toasts; **no tax export** (fixed notice
  to the brokers' annual tax documents, §19.15); Shadow Account imports with column mapping + templates (§19, §20).
- **Stage 10:** the Council migrated as a strategy with a parity gate (§22), careful cleanup (deletion list
  approved; rename to `*.stale` first), whole-app security audit (§23), operating guide.

## 16. Stage 6 — discovery loop

Full text (run model detail, payload JSON, event names, WPF notes, review fixes):
→ spec-archive/16-stage6-discovery-loop.md

- **Decisions in force** (implementation choices confirmed by the user 2026-10-24): default 6 variants × 4
  generations (max 8 × 5), search points P 0..3 (default 1); seed = existing strategy (incl. library), idea text or
  confirmed paper; own **loop AI budget** (monthly 10, daily 2, per run 1 USD; 0 = off, max 200); switch **"fully
  autonomous"** (validates its finalists G1–G8) snapshot at start — off = finalists proposed for approval; the
  **holdout is ALWAYS opened only by the user** (the loop never calls the holdout path, never reads holdout data,
  never changes a lifecycle state); **every backtest is a trial**.
- **§16.1 Run model** (`discovery/loop.py`): one loop at a time (409 `busy` + loop_id); orchestrator thread, every
  backtest / validation a normal job on the ONE job slot. Variants live in the SEED's family (origin `loop`, parent
  links, `ai_provenance`); refining the seed creates a new strategy (the user's strategies never get loop versions);
  the AI must keep the seed's universe. Gen 1: seed (its existing matching backtest reused — no new trial) +
  mutations (6 fixed aspects); gen g ≥ 2: elites carried, "refine" variants get diagnosis + refinement, culled slots
  refilled with children of elites. Finalists = best ≤ k non-duplicate variants with score > 0 passing the
  pre-checks (none → "No finalist: no variant passed the pre-checks."); chosen versions promoted. Statuses running,
  waiting_slot, waiting_connection, paused, paused_budget, interrupted, done, failed, cancelled. Insert-only events,
  re-entrant (stored steps never recomputed; a cut-off AI call repeats and costs again); restart → interrupted,
  auto-resume if configured; a pending cancel becomes `cancelled` at start.
- **§16.2 Scoring** (deterministic, run card only, `"sp-loop-score/1"`): score = Sharpe − min(1, 0.01 × max(0,
  turnover − 12)) − 2 × max(0, maxDD − 0.20) − min(1, 0.5 × max(0, cost_share − 0.5)) − 0.25 × failed pre-checks
  (trades ≥ g7, signal t ≥ g1 / TS positive share > 0.5, alpha > 0, capacity ≥ g8 × capital; uncomputable = fail);
  holdout guard (`HoldoutDataRefused`); projected G4 impact (trials now → after the loop, expected max Sharpe).
- **§16.3 Selection:** rank score, Sharpe, slot; logic-hash duplicates refused before any backtest; daily-return
  correlation ≥ 0.95 (≥ 60 days) with a better variant → duplicate; k = max(1, round(N/3)) elites, c = max(1, ⌊N/3⌋)
  culled, the rest refined.
- **§16.4 Parameter search:** per new AI variant up to P extra versions (value ± step of used parameters, name
  order, no logic duplicates), best score chosen; max backtests 1 + (N−1)(1+P) + (G−1)(N−k)(1+P), hard cap 120.
- **§16.5 LLM roles:** diagnosis (`sp-loop-diagnosis`, facts only, number-checked) and refinement (`sp-loop`, Studio
  schema + scope guard + validator + one repair, temperature 0.4) and seed (`sp-loop-seed`); ONE independent topic
  check per variant over every displayable text (failed → texts discarded, variant failed); provenance + cost on
  every AI text; no stand-in on any failure.
- **§16.6 Budget** (`discovery/budget.py`): checked before every call (current UTC day / month), reservations, failed
  spend write pauses the loop; day / month reached → `paused_budget`; per-run cap → finish pending work, end `done`
  "stopped early: per-run AI cap reached". Central window keys `loop`, `loop_month`, `loop_run` (period "run").
- **§16.7 Storage** v14 insert-only: `loop_runs`, `loop_variants`, `loop_events`, `loop_ai_messages`; state = fold
  over events.
- **§16.8 Slot sharing:** the loop submits one job at a time, after Studio / paper queues, ≥ 5 s after its previous
  job (room for a manual click); manual 409 bodies add `loop_id`.
- **§16.9 Memory:** loop runs write memory facts via the finish hook + one `loop_result` fact.
- **§16.10 API:** `POST /v2/loops` → 202; `GET /v2/loops`, `GET /v2/loops/{id}?events_after=`, `GET
  /v2/loops/estimate`, `POST …/{id}/cancel|pause|resume`, `POST …/finalists/{variant_id}/approve|reject`, `GET/PUT
  /v2/settings/loop`. Finalist states proposed, approved, rejected, validating, `passed_gates` (= passed G1–G8,
  NOT lifecycle `validated`; `eligible_state: "candidate"` for the user's own step), failed_gates,
  validation_failed / cancelled / start_failed.
- §16.11 tests (planted signal found, noise rejected, budgets, resume, diversity, no holdout), §16.12 backend notes,
  §16.13 WPF ("Loop" rail entry: runs, status, finalists panel, generations grid, AI messages, start / settings
  dialogs) → archive.

## 17. Stage 7 — portfolio, risk, OMS (paper)

Full text (§17.0 audit 2026-10-18, exact JSON of every payload §17.11, backend notes §17.12, test list):
→ spec-archive/17-stage7-portfolio-risk-oms.md

- **Decisions in force** (§15, user 2026-10-17; the agent's choices confirmed by the user 2026-10-24): allocator =
  inverse volatility with a correlation penalty; **incubation length chosen per run, then immutable**; hard limits
  **2 % daily loss** (no new risk-increasing orders until the next NY day / UTC day) and **10 % drawdown from peak**
  (kill: cancel all, close all, auto trading off), adjustable with audit; netting; pre- / post-trade checks; OMS state
  machine; **one strategy runtime for backtest, paper and live** (same evaluator, portfolio / risk code and cost
  model — the real `fund.broker.PaperBroker`). Market orders only on the internal paper adapter.
- **§17.1 Accounts + coexistence:** platform paper accounts (`pf_accounts`; `execution/paper_store.py` = the
  `fund.store` interface on `pa_*` tables scoped by `account_id`): `paper-main` (opened by the user, 1,000–100,000,000,
  default 100,000) and `paper-council` (created ONLY by the migration of the old paper account, §17.9; source
  `council-of-ais`). **An account has exactly ONE owner at any time; the old engine and the new runtime never share
  one.** After the migration the old engine was advisory only (409 "This paper account was migrated to the Strategy
  Platform on <date> …"); since the stage-10 switchover the Council itself runs on the platform (§22). Kill switch
  and dead-man heartbeat cover all halves (one client heartbeat `engine.touch_client`, timeout
  `autotrade.heartbeat_minutes`, §22); auto trading is forced OFF at every backend start.
- **§17.2 Lifecycle:** `validated → incubation` ONLY via `POST /v2/incubation` (generic transition route → 409);
  `incubation → validated` = abort; a new version saved while incubating → `draft` and the run aborts. Incubation run
  (`incubation_runs`, insert-only by trigger): `length_sessions` 20..250 (default 60), strategy calendar (NYSE /
  UTC days for crypto-only), `start_date` = next session. Expected band from the version's latest done holdout card
  (daily LOG returns): `passed` when the realised cumulative log return ∈ n·μ ± 1.96·σ·√n, max DD ≤ 1.5 × holdout max
  DD and missed sessions ≤ 20 % (missed > 20 % → `inconclusive`, else `failed`). `live_eligible` = latest completed
  run passed and still in incubation with that version; the strategy keeps paper-trading after completion. Live
  steps refused (stage 8 guard, stage 12 only on the user's go).
- **§17.3 Runtime** (`live/runtime.py`): 60 s asyncio tick, worker thread, `POST /v2/oms/runtime/run`. Decision per
  strategy once per completed daily bar via the backtest's `DataLoader` + `portfolio/decision.py::decide` (shared
  with `backtest/engine.run`); decision ≥ close + 20 min (NYSE) / 00:20 UTC (crypto-only); first decision = session
  before `start_date`; data failure → that decision `failed` (no stale signal). Execution `next_open` ≥ open + 6 min,
  `next_close` in [close − 10, close − 1 min]; unexecuted before the next decision → `missed`. Housekeeping per
  account: accruals → queued fills → margin → post-trade checks → snapshots (5 min while relevant, else 30 min).
- **§17.4 Allocator** (`portfolio/allocator.py`): vol = 60-session sub-ledger std (≥ 20), else holdout-card vol
  (`vol_source` ledger|holdout — definition, not fallback), none → weight 0; ρ̄ over 120 common sessions (≥ 20);
  raw = (1/vol)/(1 + λρ̄), λ 1.0; min-weight 0.05 → renormalise → cap 0.40 (excess stays cash) → × max_total 1.0;
  monthly / on entry-exit / on user rebalance; drift band 5 %; `pf_allocations` insert-only.
- **§17.5 Netting + sub-ledgers:** opposite deltas crossed internally (no fee, no market self-cross), residual =
  one account order (two legs on a sign flip), allocations pro rata; equities ≤ 1 % of 20-day ADV (missing → order
  refused). `sl_entries` insert-only (capital, fill, fee, slippage, borrow_fee, dividend(_short), funding,
  liquidation_fee, cross, migration); every cash row attributed once; **invariant: Σ sub-ledger cash = account cash,
  Σ qty = account qty**; accruals per SIGNED quantity.
- **§17.6 Checks** (`portfolio/checks.py`, `risk.py`; `GET/PUT /v2/portfolio/settings`, insert-only `pf_settings`):
  max order notional 50 % / $1,000,000; max position 50 % equities / 40 % crypto; price collar 5 % / 10 % (no
  reference → risk-increasing order fails, closing passes); buying power; shorts / Rule 201; hours (equities 09:35–16:00
  NY); duplicate guard 60 s; 100 orders / day; daily loss 2 %; drawdown kill 10 % (`POST /v2/oms/resume` clears it and
  rebases the peak). Each result stored on the order; failed → `rejected` with the message.
- **§17.7 OMS** (`execution/oms.py`): persisted state machine new → submitted → accepted → partially_filled → filled
  (+ cancelled / rejected / expired), transitions checked in SQL, `oms_order_events`. **Stored (`new`) BEFORE sent.**
  Ids: runtime `rt-<execution>-<symbol>-<leg>`; manual `client_order_id` `^[A-Za-z0-9_-]{8,64}$` (GUID), reserved
  prefixes `rt- run- kill- kil- ddk- dra- mar-` → 400; repeated id → stored order (200). Adapter interface
  `brokers/base.py` (`account, positions, submit, cancel, order, fills, capabilities, name, paper`); retry 3× within the
  call, ≤ 5 attempts; restart sync: stale `new` → cancelled, `submitted` → `order()` lookup / re-send < 5 min / rejected.
  TIF runtime `day`, protective `gtc`. Auto trading off → decisions recorded, no orders (sessions missed).
- **§17.8 Storage** hedgefund.db v15 (later versions add, §1/§22): `pf_*`, `pa_*`, `oms_orders`, `oms_order_events`,
  `sl_entries`, `sl_snapshots`, `rt_decisions`, `rt_executions`, `incubation_runs`, `incubation_events`.
- **§17.9 Migration** (`execution/migration.py`, done once on the user's DB): dry run → blockers → `POST
  /v2/portfolio/migration` (optional note; no typed confirm since fix1023) → freeze, copy into `paper-council` in one transaction, mark migrated; startup recovery completes or
  un-freezes. Parity acceptance: AccountView equal for the same quotes. Tests only on temp copies.
- **§17.10 API** (api_auth, strict bodies, `{error, code}`): `/v2/portfolio/{state, accounts, accounts/{id},
  accounts/{id}/equity, sources, sources/{id}/ledger, allocation, allocation/rebalance, settings, migration,
  migration/dry-run}`; `/v2/oms/{orders, orders/{id}, orders/{id}/cancel, fills, autotrade, kill, resume, runtime,
  runtime/run}`; `/v2/incubation{, /eligibility, /{id}, /{id}/abort}`. Exact payloads (AccountSummary, SourceSummary,
  Settings, OmsOrder, IncubationRun, …) → archive §17.11 / §17.12 — binding for the WPF "Portfolio & Trading" view.
  `GET /v2/portfolio/summary` (stored values only, MainWindow tile): §17.15.
- **§17.13 WPF** ("Portfolio & Trading" rail entry; one pinned kill button = `POST /fund/kill`, per-half result) and
  **§17.14 review fixes 2026-10-18** — full text in spec-archive/18-stage8-brokers.md (they sat between §18 parts).
  In force from §17.14: a `submitted` order can be cancelled (adapter asked first, never re-sent after a kill); every
  re-send re-checks halt / auto trading / daily-loss latch (`sync()` reports `not_resent`); the daily-loss block is
  **latched** until the next NY / UTC day (`day_loss.*.latched`); migration serialised by a lock (`migration_running`
  blocker, 503 `migration_failed`, never un-freezes once a `pf_migrations` row exists — **the migration is not
  reversible**); allocator floor removing every strategy → `allocation.status "no_allocation"` + `status_reason`, no
  capital moved (no fallback weights).
- **§17.15 `GET /v2/portfolio/summary`** (built 2026-10-25, contract `UFOS.ai/docs/tasks/PORTFOLIO_SUMMARY_API.md`,
  `portfolio/summary.py`): stored values only for the MainWindow tile (30 s poll) — reads hedgefund.db and the old
  engine's stored flags, builds no account / broker object (no broker call, no market data, no quote, no valuation),
  does NOT touch the heartbeat; no query parameters (400); `mode` stamped. Per active account: `equity` / `as_of` =
  newest stored equity snapshot, `day_pnl` / `day_pnl_pct` vs the last snapshot before 00:00 New York (00:00 UTC for
  `binance-demo`), `spark` (≤ 30 snapshots), positions / working-order counts, `halt`, `kill_pending`, `currency`; a
  missing value is null WITH `reason`. Plus `totals` per currency (null when an account lacks the value), `autotrade`,
  `halts`, `kill`, `council` (switch-over state, orders switch), `old_engine` (before the switch-over).
- **Known limits:** runtime cycle list + last decision errors in memory; GET quotes = the runtime's last quotes
  (`valuation.ok false` until the next cycle); IBKR / MT5 adapters not built.
- **Product notices / direction (user, 2026-10-17):** context-specific notices held centrally (`Shared/AiContent.cs`,
  backend `core/notices`, §23.2) replace the blanket "not financial advice" line; built shippable in theory, but it
  stays the user's own project (project doc `claude/project-direction.md`).
## 18. Stage 8 — brokers

Full text (exact JSON §18.10, backend / WPF notes §18.11 / §18.12, fixes + Kraken live tests + retirement §18.13,
Binance contract + notes + review fixes §18.14; also §17.13 / §17.14): → spec-archive/18-stage8-brokers.md

- **Decisions in force:** **Alpaca PAPER** for US stocks / ETFs; **Binance Spot Demo Mode** for crypto (user
  2026-10-19; replaces the Kraken Futures demo, which Kraken decommissioned — `kraken-demo` is **retired**);
  IBKR / MT5 not built (adapter interface open); reconciliation **halts** on mismatch, no auto-correction; **live
  guard built, live OFF** (stage 12 only on the user's explicit go). Every broker URL the code can build is a paper /
  demo URL.
- **§18.1 Model — mirror book, broker = truth:** broker accounts in insert-only `br_accounts` (`alpaca-paper`,
  `binance-demo`; `kraken-demo` retired). `accounts.rows()` / `all_accounts()` return internal + connected broker
  accounts, so OMS, checks, settings, sub-ledgers, allocator, runtime, kill and dead-man work unchanged. The mirror
  (`pa_*` with the broker account id) is written ONLY from broker fills and user-resolved reconciliations, valued with
  PaperBroker code (never executed). Cash basis: Alpaca `cash`; Binance USDT total (free + locked). Connect: `POST
  /v2/brokers/{id}/test` (read-only) → `POST …/connect` (optional note; typed
  confirms dropped by fix1023); Alpaca
  requires flat, no open orders, ≥ $1,000; Binance see below. Disconnect only when flat, no working orders, no
  incubation, no route.
- **§18.2 Routing:** `paper-main` is the default; venue rule (`alpaca-paper` equities / ETFs, `binance-demo` crypto
  long-only; mixed universes only on `paper-main`); `br_routes` insert-only, `POST /v2/brokers/routing {strategy_id,
  account_id, reason?}` — target open, not halted, last reconciliation ok, strategy
  flat and without a running incubation. `POST /v2/incubation` / eligibility use the route (codes `route_mismatch`,
  `wrong_venue`, `not_reconciled`, `long_only`, `retired`). A route to a retired or now-refused account is shown
  `blocked` (never moved).
- **§18.3 Adapters** (`execution/brokers/`): interface adds `snapshot()`, `open_orders()`, `cancel_all()`,
  `instrument()`, `endpoint`, `capabilities`; order types market / limit / stop / stop_limit / bracket, TIF day /
  gtc; acks with `legs`, `broker_status`; every network call through an `http_gate` gate; transient →
  `BrokerUnavailable`; broker refusals = rejected acks with the reason. **Ambiguous sends are never blindly re-sent**:
  the adapter looks the client id up first, every time.
  - `alpaca_paper` (alpaca-py `TradingClient(paper=True)`, keys `alpaca-paper-key-id` / `alpaca-paper-secret-key`,
    gate `alpaca` shared budget, base URL asserted `https://paper-api.alpaca.markets`, no redirects followed): whole
    shares, regular session only, PDT refusal shown, Reg T 2× buying power, easy-to-borrow shorts; fills by polling
    (15 s while working + every cycle); fee 0 (any broker cash change shows as a reconciliation mismatch).
  - `binance_demo` (ccxt `binance`, pinned `ccxt==4.5.84` in `backend/requirements-broker.txt`, installed in its own
    pip run — not installed → state `not_installed` with the reason; `enable_demo_trading(True)`, `urls['api']` cut to
    spot `public` / `private` / `v1`, every request URL asserted on `demo-api.binance.com`, redirects refused): keys
    `binance-demo-api-key` / `binance-demo-api-secret` (HMAC only; PEM refused); `BASE-USD` ↔ `BASE/USDT`; money is
    **USDT, shown as USDT, never converted** (`currency`, `currency_note`, setting `max_order_notional_usdt`, own
    BASE/USDT quotes via `accounts.quote_for`); **long only** (short / cover / oversell blocked with an account event);
    market + limit GTC; filters LOT_SIZE / MARKET_LOT_SIZE / PRICE_FILTER / NOTIONAL named on refusal; client ids
    `UFOS_<cid>`, kill / cancel_all touch only `UFOS_` orders; fees from trades (base commission booked net, BNB fee
    not booked → mismatch); **first connect records a confirmed baseline** (`starting_balances`, 409
    `balances_changed` / `platform_orders_open` / `too_small`) — positions = balance − baseline; missing baseline
    blocks the account; error codes (-2015, -1022, -2014, -1021) shown with sanitized hints; storage v23 rebuilt
    `br_accounts` (backup `data/backups/hedgefund.db.pre-v23-<stamp>` first). Verify tool
    `tools/verify_binance_demo.py` (read-only; test order only with `--place-test-order`).
  - `kraken_demo` **retired 2026-10-19**: every action 409 `retired`, no request to Kraken's private / demo API is
    possible, skipped by runtime / poller / start-up / global kill; `BrokerStatus.state "retired"`; dormant code on
    the stage-10 deletion list. The Kraken PUBLIC price source (`data/providers/kraken_public.py`) is separate.
- **§18.4 OMS on brokers:** same state machine / stored-before-sent / recovery as §17.7; fills booked as cumulative
  DELTAS per broker order id (`br_fill_marks`, `brokers/mirror.py`), bracket legs imported as `<cid>.tp` / `.sl`.
  `oms_orders` adds `order_type`, `limit_price`, `stop_price`, `legs_json`, `parent_client_order_id`,
  `broker_status`; the internal paper broker accepts market only. Extra pre-trade checks `venue`, `position_side`,
  `broker_link` (connected, link ok, last reconciliation ok ≤ 15 min; protective orders skip it and queue).
- **§18.5 Reconciliation** (`brokers/reconcile.py`): triggers start / cycle / poll (≥ 5 min old) / connect / manual /
  resolve; compares positions exactly, cash basis within tolerance (Alpaca max($1, 0.01 % equity); Binance max(1
  USDT, 0.05 % of the cash basis)), open orders both ways. `br_reconciliations` insert-only. `mismatch` → that account
  HALTS (kind `reconcile`, sticky until a resolve or reconnect) — no orders at all except the broker kill; one alert
  per halt. `unreachable` → no halt, `broker_link` blocks new orders. Resolve `POST …/resolve {action: recheck |
  cancel_unknown_orders | accept_broker (only on a halted account), reason?}`;
  Binance accept below the baseline moves the baseline, never a mirror short.
- **§18.6 Weak links / kill / dead-man:** link state `ok | waiting | auth_failed | keys_missing | not_installed |
  error | idle` with backoff min(15 s · 2^(n−1), 300 s); user actions try once during backoff; no cached broker
  number shown as current. **Verified broker kill** (`POST /v2/brokers/{id}/kill`, also inside the global kill):
  halt → cancel OMS + broker orders → bounded wait → close every position the BROKER reports (never guessed from the
  mirror) → retry rejected closes ≤ 3 → VERIFY flat; not flat / unreadable → `ok: false`, `pending: true`,
  `kill_pending`, the 15 s poller continues; `POST /v2/oms/resume` 409 `kill_pending` / `reconcile_mismatch`.
  Binance dust → `skipped`, refused cancels → `refused`.
- **§18.7 Live guard** (`brokers/live_guard.py`): would need all of per-broker flag, typed "GO LIVE <ID> WITH REAL
  MONEY", strategy state `live`, capital cap 100..1,000,000 — but `BUILD_ALLOWS_LIVE = False`: always refused "Live
  trading is disabled in this build (stage 12 requires the user's explicit go)."; `endpoint_for(…, live=True)` raises;
  live hosts only in `live_guard.LIVE_HOSTS` (source-scan test).
- **§18.8 Storage** v16 (+ v23 rebuild): `br_accounts`, `br_connections`, `br_routes`, `br_reconciliations`,
  `br_resolutions`, `br_live_guard`, `br_imports` (insert-only), `br_fill_marks`.
- **§18.9 API:** `GET /v2/brokers`, `GET /v2/brokers/{id}`, `POST …/{id}/test|connect|disconnect|reconcile|resolve|
  kill`, `GET …/{id}/reconciliations`, `GET …/{id}/instrument?symbol=`, `GET/PUT …/{id}/live-guard`, `POST
  …/{id}/live-guard/check`, `GET/POST /v2/brokers/routing`. BrokerStatus / Test / Kill / Reconciliation JSON (incl.
  `chip`, `currency`, `baseline`, `kill`, `state`, `retired`) → archive §18.10–§18.14 (binding for Settings → Brokers).
## 19. Stage 9 — monitoring & Shadow Account

Full text (contract, exact JSON §19.11, backend / WPF notes §19.12 / §19.13, review fixes §19.14, the removed tax
export §19.5, Trade Republic importer detail §19.16): → spec-archive/19-stage9-monitoring-shadow.md

- **Decisions in force** (§15 + user 2026-10-18; agent choices confirmed by the user 2026-10-24): **decay → the
  strategy is PAUSED** (no new orders, positions kept) **+ the user decides** (keep running / demote); **alerts =
  Windows toasts**; **NO tax export** (removed 2026-10-18, fixed notice §19.15); Shadow Account imports (generic
  mapping + templates; Trade Republic real format) — **data stays local**. Stage 9 places no new kind of order (the
  pause only removes orders; demote uses the §17.2 abort path). Package `strategy_platform/monitoring/` (+
  `shadow/`), routes `api/monitoring_routes.py`, task `sp-monitor`, storage v17 (+ v19 / v20 shadow rebuilds).
- **§19.1 Live vs expected** (`monitoring/expected.py`): every active incubation run; expected basis = the run's
  frozen `expected_json` (never recomputed — no look-ahead); realised = sub-ledger daily log returns from the
  snapshot before `start_date` up to the last COMPLETED session; tracking series (cum log, k·μ, band k·μ ±
  1.96·σ·√k, drawdown, rolling vol) and statistics (`z_cum`, `in_band`, `tracking_error`, `vol_ratio`, `dd_ratio`,
  `rolling_z`, Sharpe).
- **§19.2 Decay monitor** (`monitoring/decay.py`, constants ONLY in `decay.RULES_BY_BASIS`): shared `MIN_SESSIONS
  20, RETURN_WINDOW 60, RETURN_CONSECUTIVE 3, VOL_WINDOW 20, VOL_FACTOR 2.0, VOL_CONSECUTIVE 3`; per holdout basis
  `RETURN_Z` / `DD_FACTOR`: **equity_24m 3.0 / 2.0 · equity_12m 3.5 / 2.5 · equity_6m 3.5 / 3.5 · crypto_12m 3.2 /
  2.5** (basis from the calendar and holdout session count). Rules per completed session with data up to t only:
  `return_band` (rolling sum below w·μ − Z·σ·√w for 3 sessions), `drawdown` (> DD_FACTOR × expected max DD, at
  once), `volatility` (> 2 × expected for 3 sessions). Checks insert-only `mon_decay_checks` (missed days caught up
  in order). States `insufficient | ok | watch | paused`. **Pause:** decisions recorded, no orders for that strategy;
  its own risk-REDUCING protective exits (`force` symbols it holds) still run and raise alert
  `protective_exit_while_paused`; manual / kill / other strategies unaffected; paused sessions count as missed; no
  auto-resume / auto-demote. **Decision** `POST /v2/monitoring/strategies/{id}/decision`: `keep_running` (optional note;
  afterwards only returns after the decision count — fresh `MIN_SESSIONS` sample, drawdown measured from the
  decision) or `demote` (optional note; no typed confirm since fix1023 → incubation aborted, back to `validated`, positions closed by
  the runtime).
- **§19.3 Alerts** (`monitoring/alerts.py`): insert-only `mon_alerts` (`dedupe_key` UNIQUE) + `mon_alert_acks`; plain
  backend text, never AI text / keys / account numbers. Kinds (severity · toast) in `alerts.KINDS`: decay,
  reconciliation_halt, kill_switch, drawdown_kill (critical · yes); daily_loss_limit, autotrade_off,
  broker_disconnected, protective_exit_while_paused (warning · yes); incubation_finished (info · yes);
  broker_reconnected, report_ready (info · no); stage 10 adds the Council kinds (§22). Producers: decay monitor,
  report scheduler, the **audit scanner** (cursor `mon.alerts.audit_cursor`, set to newest on first start), the
  **broker link watcher** (down ≥ 10 min → one alert per outage). `sp-monitor` task: 30 s scanner + watcher, 60 s
  decay, report scheduler; failures shown in `overview.monitor.last_error`, never touch trading. WPF polls `GET
  /v2/monitoring/alerts?after=` every 15 s, one toast per new `toast: true` alert, `POST …/alerts/ack`.
- **§19.4 Reports** (`monitoring/reports.py`): daily (UTC date) / weekly (ISO week) created after 00:30 UTC of the
  next day / Monday, or on demand; insert-only `mon_reports`; deterministic numbers per account / strategy + a §11.1
  fact table. Optional AI commentary (`POST …/reports/{id}/commentary {model, language}`, feature `sp-report`, input =
  facts only with anonymised labels, §11.2 output + number check + provenance, `mon_commentaries`); budget key
  **`reports`** ($0.50/day default, max $20, 0 = off; 402 `budget`). P&L excludes the cash-flow kinds deposit /
  reset / broker_adjustment / migration.
- **§19.5 Tax export — REMOVED** (see §19.15). Archive only.
- **§19.6 Shadow import** (`shadow/importers.py`, `normalize.py`): `POST /v2/shadow/accounts/{id}/imports?template=
  &profile_id=&dry_run=` (multipart `file`; 5 MB, 50,000 rows, 64 columns, 2,048 chars / field, no NUL; UTF-8 or
  cp1252; `;` `,` tab); every cell is TEXT (formula-looking text neutralised on export); schema detection → 409
  `unknown_format` unless `generic` + profile. Templates `trade_republic` (real format, §19.16), `scalable`,
  `ibkr_flex` (both "verify against a real export"), `generic` (insert-only mapping profiles `POST
  /v2/shadow/profiles`). Normalised `sh_transactions` (insert-only, content-hash / `ext_id` dedupe); FIFO lots
  (excess sell → `unmatched`, no invented cost basis).
- **§19.7 Diagnostics** (`shadow/diagnostics.py`, each block may be `{unavailable}`): disposition effect (PGR / PLR /
  DE / t), turnover / fees, holding periods, concentration (HHI, effective N), trading after losses, timing vs
  benchmark (SPY, 20 sessions). Prices (`monitoring/prices.py`): **raw closes** for trade-price comparisons, `all`
  series only for return series; split after the first purchase or missing corporate actions → instrument excluded;
  market data only for identifiable US-listed USD securities, else the user's valuation (`PUT …/valuation`);
  unpriced counted, never a stand-in.
- **§19.8 Shadow portfolios + audit report:** the user's exact dated flows applied to ≤ 5 rule portfolios
  (`buy_and_hold`, `monthly_rebalance`, `strategy`) with the unit-value method (parity tested); USD returns on EUR
  flows via DEXUSEU of the **exact date** (missing → `unavailable`, no fallback); XIRR for user and shadows. Report
  `POST …/report {format: html|json}` (self-contained HTML, CSP `default-src 'none'`, no script, escaped) +
  optional AI commentary (`POST …/commentary`, facts only, positions anonymised, budget `reports`).
- **§19.9 Storage** v17 (all insert-only): `mon_decay_checks`, `mon_decay_events`, `mon_alerts`, `mon_alert_acks`,
  `mon_reports`, `mon_commentaries`, `sh_accounts`, `sh_profiles`, `sh_imports`, `sh_transactions`,
  `sh_classifications`, `sh_valuations`.
- **§19.10 API:** `/v2/monitoring/{overview, strategies/{id}, strategies/{id}/decision, decay/rules, decay/run,
  alerts, alerts/ack, reports, reports/{id}, reports/{id}/commentary, tax-notice}`; `/v2/shadow/{templates, profiles,
  accounts, accounts/{id}, accounts/{id}/imports, accounts/{id}/transactions, accounts/{id}/valuation,
  classifications, accounts/{id}/diagnostics, accounts/{id}/shadows, accounts/{id}/report, accounts/{id}/commentary}`.
  The tax routes answer 404. Exact JSON → archive §19.11–§19.14 (binding for the WPF "Monitoring" / "Shadow
  Account" views).
- **§19.15 No tax export (user decision 2026-10-18):** `monitoring/tax_export.py` → `.stale`, tax routes 404; `GET
  /v2/monitoring/tax-notice` → `{version: 1, decided: "2026-10-18", notice: "UFOS.ai does not produce tax data or tax
  reports. For your tax return, use the official annual tax documents from each broker …", paper_notice, tax_export:
  false}` (full text in the archive). EUR/USD rates (`monitoring/fx.py`, exact date only) stay for Shadow
  conversions. Statement `tax` fields are imported facts, not tax data.
- **§19.16 Trade Republic importer:** the real TR CSV (UTF-8, comma, quoted, fixed header); `category/type` → kind
  map (amount-based cost basis for all classes, dividends gross + withholding, `tax_refund` rows); **privacy:**
  counterparty name / IBAN / payment reference / MCC never read, description never stored, IBAN-like text →
  `[removed]` (tested); `transaction_id` = `ext_id` dedupe (overlapping exports import only new rows); storage v19 /
  v20 rebuilt `sh_transactions` (rows copied unchanged).
## 20. Stage 9b — portfolio insights (Shadow data only)

Full text (contract, exact JSON §20.4, backend / WPF notes §20.5 / §20.6, review fixes §20.7, shell start §20.8,
buckets §20.9, symbol mapping §20.10, price sources + EODHD facts §20.11): → spec-archive/20-stage9b-insights.md

- **Decisions in force** (user 2026-10-18): "Portfolio insights" = the MainWindow "Portfolio Insight" card + HedgeFund
  → Monitoring → Insights; data ONLY from the Shadow Account imports (never platform / paper / broker accounts);
  fixed label everywhere "Rough guide only — not tax data. The broker's annual tax statement (e.g.
  Jahressteuerbescheinigung) is authoritative."; **original currency everywhere, no FX conversion, sums per
  currency**; **one configured price source per data type, no fallback chains**; §19.15 holds (no tax export / tax
  maths). Scope exception for MainWindow files: `HedgeFund/tools/scope-allow-insight0.json`.
- **§20.1 Calculation** (`monitoring/insights.py`, deterministic, local): FIFO over the whole history per account /
  currency, a part belongs to the year of its sale; buckets **`brokerage`** (share, etf, fund) / **`other`** (bonds,
  crypto, derivatives, private funds, other) / **`unclassified`** (never guessed — user classifications), each
  `{gains, losses, net, parts}`; unmatched sales and rejected rows listed, not used; income / costs as imported
  (dividends, withholding, interest, trading / other fees, tax refunds); YoY deltas; state `no_imports | incomplete |
  ok` with `reasons[]`. Nothing sent to an AI.
- **Price order per open position** (§20.11): (1) a valuation price (import first, then the user's own); (2) the
  configured listing in the SAME currency as the cost → price, value, unrealised (`day_change` for market prices);
  (3) a listing in a DIFFERENT currency → `listing.value` only, "different currencies — not converted"; (4) "not
  available (no price source): <reason>". Weights / coverage / concentration over valued positions only.
- **Sources** (§20.11): US listings → Alpaca (USD); stocks / ETFs incl. EU venues by ISIN → **EODHD end-of-day**
  (`data/providers/eodhd.py`, gate `eodhd`, key `eodhd-api-key`, `EODHD_DAILY_LIMIT = 20` calls / UTC day — at the
  limit no request and "EODHD daily limit reached (20/20) — next refresh tomorrow"; EOD fetched once per listing per
  day, search cached 30 days per ISIN; personal use only, no redistribution, report notes "prices: EODHD, personal
  use"); crypto BTC / ETH / SOL in EUR → Kraken public (`data/providers/kraken_public.py`, no key); derivatives,
  bonds, private funds, other coins → manual valuation only. The only conversion left: the shadow-portfolio
  simulation, labelled "simulated in USD — converted at the FRED rate of each flow date".
- **§20.10 Symbol mapping** (global per ISIN, insert-only `sh_symbol_maps` v21, newest wins, removal = null row,
  audited; `monitoring/shadow/symbols.py`): `GET/PUT /v2/shadow/symbol-mappings` (US ticker with
  `confirm_us_listing`, or an EODHD listing picked from `GET /v2/shadow/listings?isin=` — validated against the
  search); `GET /v2/shadow/price-sources`; `POST /v2/shadow/price-sources/eodhd/refresh {listing}` (costs 1 call,
  audited).
- **§20.2 API** (api_auth; GETs do NOT touch the trading heartbeat): `GET /v2/insights/summary` (card), `GET
  /v2/insights/portfolio?year=&account=` (detail). Exact JSON → archive §20.4 / §20.9–§20.11.
- **§20.3 / §20.8 MainWindow card:** `Shared/BackendAuth.cs` (find backend dir, read `data/api-token` on every
  call, header `X-UFOS-Token`, `http://127.0.0.1:8867`, `GET /health` then the request); `IPortfolioInsightProvider`.
  Backend not running → the card **starts the Hedge Fund backend** via `HedgeFund/HedgeFundBackendHost.cs` (the same
  start path as opening the Hedge Fund window) and shows "Hedge Fund backend not running — click to start" meanwhile,
  never stale numbers (no snapshot cache — proposal only).
## Click-test fixes 2026-10-22, Retest fixes 2026-10-23, Fix batch fix1023 — rules in force

Full text (root causes, measurements, round 2 / 3 reviews, WPF dialog list): → spec-archive/fixes-2026-10-22-23.md

- **AI drafts — parameter form (1022 #1):** every DSL parameter MUST be `{"value", "min", "max", "step"}` (+
  `"integer": true` for windows / lags / counts) — rule 3a in the Studio and loop system prompts; repair messages
  carry `parameter_fixes [{parameter, missing, required_form, instruction}]`; `studio._validate_spec` reports a
  missing `step` as `missing_key`. Missing values are never filled in.
- **Stress (1022 #2 / #3):** with the paper cost model "Costs x2" and "Slippage x3" are identical by construction
  (the slippage scenario carries a `note`); every stress scenario has `warning_summary {total, counts, symbols,
  text}` (computed on read for old cards) — the WPF shows `text`.
- **Reports (1022 #4):** an account opened at / after the period end is left out; opened inside → `partial: true` +
  note; no report for a period without accounts.
- **Loop seed reuse (1022 #5):** a version's identical done backtest card (same engine version) is reused; `GET
  /v2/loops/estimate` answers `seed_reused` (`history_from` 2016 | 2000).
- **TLS (1022 #7):** `core/tls.client_context()` = system store + certifi, CERT_REQUIRED + host-name check (explicit,
  no `assert`) for FRED / Eurostat / calendar / SEC sessions; verification never disabled.
- **Capacity (1022 #8):** `metrics.capacity.at_most` only when the 1 % participation limit binds at the tested
  capital; else null and `capacity_usd` is the estimate.
- **Stage numbering (1022 #9):** stage 11 = the visual overhaul; the first live step is **stage 12** everywhere.
- **Valuation after a manual fill (1023 #1):** `oms.place` hands its fetched quotes to `runtime.remember_quotes`
  (real quotes only); GETs still never fetch.
- **Price collar (1023 #2):** reference = the decision's `ref_price`, else a fresh latest quote; no reference →
  risk-increasing order FAILS, closing / protective passes with the reason (no implicit pass).
- **Venue-aware quotes (1023 #4):** `BrokerAccount.can_hold(symbol)` — a venue never reads quotes for what it cannot
  hold.
- **Order quote deadline (1023 #5):** all quotes of one order in parallel with `ORDER_QUOTE_DEADLINE_SECONDS = 8`;
  worst case per broker order ≈ 456 s → the WPF uses a ~30 s client timeout, shows "sent — the result follows in the
  order list" and polls by `client_order_id` (idempotent).
- **fix1023 A — confirmation style (user 2026-10-23: the users are professionals):** no typed confirm words, no "I
  understand" flags; `reason` is an optional note (missing / null / "" → "", ≤ 500 chars, longer → 400); sent
  `confirm` / `confirm_*` fields are accepted and ignored. Applies to incubation start / abort, decay decision,
  strategy transitions (evidence still required), resume, rebalance, settings, migration, broker connect /
  disconnect / resolve / routing, Shadow import removal and symbol mappings, Studio draft save, Market Research
  drawing delete. **Kept on purpose:** the live guard, Open holdout (typed strategy name), gate exceptions (reason ≥
  30 chars, never for H). WPF: ONE confirmation step with a summary, optional note box and a precise action button;
  busy-disabled while in flight (`TradingUi.SetBusy`, `PlatformClient.WithNote`).
- **fix1023 B — incubation / decay:** incubation start serialised (`incubation._start_lock`, second start → 409
  `already_incubating`); allocation after a start runs in a background thread (waits ≤ `ALLOCATION_WAIT_SECONDS` 3 s,
  response `allocation {status: done|failed|pending}`, failure → WARNING alert `allocation_failed` + audit, retried
  next cycle); `GET /v2/incubation?strategy_id=&started_after=<ISO with offset>` confirms a start after a timeout;
  decisions of an aborted run → `status: "cancelled"`; `GET /v2/monitoring/strategies/{sid}` adds
  `decay.paused.numbers` (+ `synthetic`, `numbers_note`), `decay.last_check_at`, `last_check_source`; `GET
  /v2/incubation/{id}` adds `decay_events`; one pause event per pause (BEGIN IMMEDIATE), every pause its own CRITICAL
  `decay` alert (dedupe `decay:<run>:<day>:<event id>`).
- **Shadow (fix1023 round 2):** `GET /v2/shadow/symbol-check?ticker=&isin=` (read-only Alpaca asset check; 503
  `symbol_check_unavailable`); **hedgefund.db v24** `sh_symbol_maps.confirmed_via` ("dialog" | "confirm_flag").
- **Log spam (1022 #6):** "valuation paused" logged once per symbol-set change, then ≤ every 10 min.

## Sandbox profile (contract, user decision 2026-10-23)

Purpose: click-test the incubation / monitoring flow while the user's real databases stay clean (no strategy has
passed validation + holdout; nothing is faked in the real DB). Code `sandbox_profile.py`, `tools/sandbox.py`,
`sandbox_fixtures.py`, `strategy_platform/core/fixture.py`; tests `tests/test_sandbox_profile.py`. Full text:
→ spec-archive/sandbox-profile.md

- **S1 Activation by tool, never by UI** (`HedgeFund\backend`: `.venv\Scripts\python.exe tools\sandbox.py …`; refuses
  outside the backend venv): `on` — refused while the backend answers `/health`; copies hedgefund.db (required),
  newsroom.db, market_research.db with the SQLite backup API into `data/sandbox/` (+ `data/papers/`), earlier copies →
  `*.<stamp>.stale`; writes marker `data/SANDBOX_ACTIVE` (hashes); a source change during the copy aborts; then seeds
  the incubation fixture (failure → exit 4, sandbox stays ON, never skipped silently). `off` — refused while the
  backend runs; re-hashes the real files (exit 0 identical / 3 changed; empty vs absent `-wal` = same state, `-shm`
  ignored); marker → `.stale`. `status`. `seed-incubation-test` re-seeds.
- **S2 Backend start:** `sandbox_profile.startup()` before any DB opens; with the marker every DB path is under
  `data/sandbox/` (`UFOS_HEDGEFUND_DB` ignored), WARNING banner + `[SANDBOX]` on every log line; unreadable marker /
  missing copy → exit 2 (never a fallback to the real files). `"mode": "normal" | "sandbox"` (+ `sandbox {since,
  banner, brokers, data_dir, prices_note}`) on `/health`, `/version`, `/fund/state`, `/v2/portfolio/state`,
  `/v2/oms/runtime`, `/v2/incubation`, `/v2/monitoring/overview`, `/v2/brokers`.
- **S3 Isolation:** broker accounts hard-blocked (`SandboxBlockedAdapter`, 409 `sandbox_blocked`, `state:
  "blocked_sandbox"`, routing paper-main only); market DATA reads allowed; old engine autopilot refused, no scheduled
  cycles; kill acts on copies only; AI cost lines `"mode": "sandbox"` + label prefix "SANDBOX · ".
- **S4 Seeding:** "SANDBOX TEST — incubation fixture" (tag `sandbox-fixture`, SPY / IEF trend, long only) moved to
  `validated` through `registry.transition` with synthetic cards (`metrics.fixture = true`, `fixture_note`
  "synthetic fixture for UI testing — no real validation"); payloads carry `fixture` / `fixture_note` only for it.
- **S5 `force-decay "<strategy>"`:** sandbox only; one synthetic shock session → `decay._pause` (audit `synthetic:
  true`); monitoring shows `decay.paused.synthetic`.
## 21. Options Structure layer — OS0 contract (2026-10-23), OS1 archive built (2026-10-24)

Full text (endpoint table, request budget reasoning, tables as planned vs built, timelines, the OS0 tool's checks a–f,
OS1 as-built detail, test lists): → spec-archive/21-options-structure.md. Plan: project doc
`claude/options-structure-plan.md` (approved 2026-10-23). OS0 = contract + read-only check + frozen pre-registration;
OS1 = archive (built); OS2–OS4 after stage 10 and Depth & Range. **Nothing here trades** — the traded instrument stays
SPY / QQQ **shares** through the OMS (options trading deferred, 0DTE struck, no crypto).

- **§21.1 Sources (GET only):** Alpaca trading API (paper host) `/v2/options/contracts` (+ OI; `expiration_date_lte`
  ALWAYS explicit, `limit` 1 000); Alpaca data API `/v1beta1/options/snapshots/{SPY,QQQ}` (**`feed=indicative`
  ALWAYS explicit** — `opra` is paid, not used) and `/v1beta1/options/bars`; spot = latest IEX trade; FRED `VIXCLS`
  (30-day) vs `VXVCLS` (3-month), `VXNCLS` for QQQ (no short-term series exists on FRED). **Budget split:** backend
  `http_gate` `alpaca` **150 req/min** (+ burst 10; Market Research ≤ 60 of it), the archive its own **30 req/min**;
  total ≤ 190 < 200 (trading + data APIs assumed to share one limit).
- **§21.2 Universe / filter / cadence** (CONFIRMED 2026-10-24): SPY (primary), QQQ (replication); strikes **±10 %** of
  spot, expiries **0 … 45 days** (same-day archived); contracts / OI 08:30 NY, FRED 08:45, intraday every 15 min
  09:45 … 15:45 + fixed **09:55** and **15:25**, EOD 16:15 (≈ 11.5 GB / year accepted). **Slot rule:** a slot snapshot
  must FINISH by its slot time (start slot − 90 s); quotes after the slot excluded; unfinished = gap. **Early close**
  (NYSE calendar): intraday 09:45 … 12:45 + 09:55, no 15:25, EOD 13:15; **H1 and H2 not evaluated on early-close
  sessions** (user 2026-10-24). NY wall-clock slots (DST gap week tested).
- **§21.3 Point-in-time (binding):** every value carries `valid_for`, `available_at`, `observed_at` + via `os_runs`
  `source`, `feed`, `model_version`, `assumptions_hash`; epoch ms UTC. OI: `valid_for` = T, `available_at` ≥ **T+1
  08:00 NY** (earlier fetch → `early_oi`), counts only if `valid_for` = the previous session; corrections = new rows.
  Chain: `valid_for` = quote time, staleness ≤ 5 min at the slot (**PROVISIONAL**), coverage ≥ 80 % (**PROVISIONAL**);
  indicative trades +15 min. Backfilled bars never count for H1 / H2. Readers use only `available_at` ≤ decision time.
  **Gaps recorded in `os_gaps`, never filled** (no interpolation, previous value or second source).
- **§21.4 Archive service:** own process via the Windows scheduled task **`UFOS.ai OptionsArchive`** (root folder,
  logon trigger, least privilege, `pythonw.exe tools\options_archive.py service`; the WPF builds the XML from a fixed
  template and runs `schtasks.exe` — the backend supplies DATA only, `GET /v2/options-archive/registration`);
  registered only after the user's ONE confirmation dialog in ⚙ Keys & data → Data sources (audit via `POST
  /v2/options-archive/audit`). Keys via `secrets.py` per run; **no listening port**; allow-listed GETs, no redirects,
  `core/tls.py`. Own DB `data/options_archive.db` (WAL, insert-only, the archive is the only writer, backend opens it
  read-only); log `data/logs/options_archive.log` (redacted); status `data/os_archive_status.json`; stop file
  `data/options_archive.stop`; lock `data/options_archive.lock`. Sandbox mode: register / manual runs disabled (409 /
  exit 6), unregister allowed.
- **§21.5 Tables** (`options_archive/db.py`, schema 1, all insert-only, BEFORE UPDATE / DELETE abort, recursive
  triggers on): `os_runs` (+ `slot_label`, `retries`, `app_version`, `skew_ms`, kind `manual`), `os_contracts`,
  `os_contract_sightings`, `os_open_interest` (key symbol, valid_for, observed_ms), `os_chain_snapshots`, `os_bars`
  (empty in OS1), `os_fred`, `os_gaps`, `os_schema`, `os_configs`.
- **§21.6 / §21.7 Flow + evaluation:** archive → OS2 computations (IV, greeks, GEX × 2–3 dealer assumptions — a signal
  counts only if it holds under all) → regime service (`available_at` ≤ decision time) → consumers. **No running
  results of H1 / H2 before the evaluation date** (progress counter only); the 2024-02 approximation backtest is
  exploratory.
- **§21.8 Frozen pre-registration:** file **`HedgeFund/docs/preregistration/OS-H1-H2-2026-10-23.md`**,
  frozen **2026-10-23**, before the first archived day (user approval 2026-10-22). **sha256
  `95d3ac66736ad358df23b4628f0c32425f4b0b4be37138f7b52a813afc3b8195`** (2 198 bytes, UTF-8, LF; `HedgeFund/.gitattributes`
  marks `docs/preregistration/**` `-text`). `tests/test_os_preregistration.py` recomputes it. Never edited — changes
  only as NEW dated files (their sha256 recorded here). Measurability rules (§21.2 / §21.3) live here.
- **§21.9 OS0 check** `tools/verify_alpaca_options.py` (read-only, allow-list + transport shared with the archive in
  `options_archive/transport.py`; checks a–f; report `data/os0/verify_alpaca_options_<UTC>.json`, scrubbed; exit codes
  0 / 1 / 2 keys / 3 stopped / 4 HTTP 429).
- **§21.10 Status:** OS0 run 20261024T114530Z (no FAIL) confirmed filter, cadence, OI semantics + floor, FRED series
  (`CONFIRMED` in `options_archive/config.py`). **Still PROVISIONAL until the RTH run on Monday 2026-10-26:** quote
  staleness 5 min and coverage 80 % (`PROVISIONAL_KEYS`).
- **§21.11 OS1 as built** (package `strategy_platform/options_archive/`: `config.py` (all numbers, ranges, override
  `data/options_archive_config.json` — a bad override is REFUSED, archive exit 3, routes 409 `config_invalid`),
  `transport.py`, `db.py`, `slots.py` (30 slots / day; a slot is collected as that slot only inside its window, else
  `missing`), `collector.py` (one run = one slot, one transaction; clock skew > 30 s gap, > 5 min stop), `report.py`,
  `service.py`, `registration.py`). CLI `tools\options_archive.py run-once | run-slot | status | gaps | service` (exit
  codes 0–7). Backend read-only API (`api/options_archive_routes.py`): `GET /v2/options-archive/status | gaps | runs |
  registration`, `POST /v2/options-archive/audit`. **Not in OS1:** the WPF card, option bars, any computation (OS2).
  Awaiting the user's registration.
## 22. Stage 10 — Council of AIs migration (contract rev. 3; R1 + R2 + R3 built and committed 2026-10-24; the real switch-over only by the user in the app)

Full text (review history, rev. 2 / rev. 3 answers, full tables, R1 / R2 as-built detail, parity harness detail):
→ spec-archive/22-stage10-council-migration.md. Basis: SP §15, the approved deletion list
`HedgeFund/docs/STAGE10_DELETION_LIST.md`, FUND SPEC §7–§10 (agents, judge, sizing — still the behavioural contract),
§17.9. Open task files: UFOS.ai/docs/tasks/ (switch-over fix API, the user's approved difference list).

**Status:** R1 (build) + R2 (switch-over backend) + R3 (WPF Council view, Agent W) committed (main 4df6db3, PR #6
61eb5f8). **Not switched over** on the user's DB. Sequence decided by the user 2026-10-24: after Monday 2026-10-26's open
(the old engine's 2 queued orders fill) → the §17.9 migration (not reversible) → the switch-over R2 → click-test →
R4 / R5 (see UFOS.ai/docs/todo.md).

| # | Decision in force |
|---|---|
| D1 | `council` = non-DSL strategy kind, exempt from G1–G8 / holdout / incubation; labelled **"not validated — AI strategy"** (user 2026-10-24) |
| D2 | Intraday cadence + "Run now"; the old crypto risk watch is dropped (runtime covers it, §22.6) (user) |
| D3 | The latest old `fund_params` copied ONCE into `cl_params` at R2, in the switch-over transaction (user) |
| D4 | Old-engine history: one read-only JSONL export before R4, no history view (user) |
| D5 | Old runs / recommendations / signals also imported once into `cl_*` (`legacy = 1`) at R2 (lead) |
| D6 | Kill / "Stop engine…" use `/v2/oms/kill`; Resume = dialog over halted accounts; R2 + R3 ship in one build; after R2 `/fund/kill` + `/fund/resume` are audited aliases (never 410) until R4 (lead) |
| D7 | Option B: outside the equity window and in the opening grace equities are ANALYSED only — "market closed — no order placed"; scheduled cycles only in session after the grace; crypto 24/7 (user 2026-10-24) |
| D8 | "Run now" reports `orders {equity, crypto}`; an exhausted budget = old D9 behaviour (cycle runs, no AI judgement for affected symbols, carried recommendations still sized); no 503 budget (lead) |
| D9 | `paper-council` broker params editable via audited insert-only override `cl_broker_params`; the risk agent's 75 % drawdown rule reads `pf_settings.drawdown_kill_pct` (lead) |
| D10 | One per-account trade lock shared by the Council order step, runtime execution and `control.kill_account` (lead) |
| D11 | Stale Council crypto orders expire after a fixed 60 min at any time (OMS `accepted → expired`) (lead) |
| D12 | The §17.9 migration (`execution/migration.py`, `/v2/portfolio/migration*`, WPF dialog) removed at R4 (user 2026-10-24) |

- **§22.1 Council as a platform strategy:** package `strategy_platform/council/` (`agents/`, `judge.py`, `sizing.py`,
  `indicators.py`, `service.py`, `store.py`, `params.py`, `spend.py`, `switchover.py`); decision logic moved unchanged;
  no rule-based stand-in (a symbol without judgement is HELD, reason recorded). Trades only `paper-council`, source
  `council-of-ais`; sizing base = the Council sub-ledger equity; `_unallocated` of `paper-council` must be empty (else
  `partial`, no orders). Trade counting per NY day (equities) / UTC day (crypto); a reversal = 2 orders.
- **§22.2 AI budget:** limit `cl_params.ai_daily_budget_usd`, budget key `fund` (label "Council of AIs"), cost-log
  modules `fund-sentiment` / `fund-judge`; ONE spend counter meta `council.ai_spend {day, usd}` written with each
  `cl_ai_calls` row, seeded at R2 with max(old `fund.ai_spend` today, Σ cost log today); **fails closed** (unreadable
  = exhausted); after a revert the old counter is seeded the same way. Before R2 no live LLM call ("Run now" 503
  `not_active`).
- **§22.3 Storage** hedgefund.db **v25** (pre-v25 backup `data/backups/hedgefund.db.pre-v25-<UTC>`; all insert-only):
  `cl_params`, `cl_broker_params`, `cl_runs`, `cl_run_results`, `cl_signals`, `cl_decisions`, `cl_positions`,
  `cl_executions` (submit-time outcome only; later lifecycle from the OMS by `client_order_id`), `cl_ai_calls`;
  **v26** UNIQUE partial indexes `ux_cl_runs_legacy_old_id` / `ux_cl_decisions_legacy_old_id`. Meta:
  `council.switchover` (`none | pending | done | reverted` + tx / time / error), `council.orders_enabled`,
  `council.ai_spend`, `council.legacy_hwm`, `council.r2_tx`, `autotrade.heartbeat_minutes`, `schema.created_version`.
- **§22.4 Params — one owner per value:** `cl_params` (watchlist, min_confidence, max_position_pct,
  crypto_max_position_pct, max_trades_per_day, agent_weights, risk_enabled, judge_model_mode, change_threshold,
  judge_stale_hours, min_hold_days, ai_daily_budget_usd, `interval_minutes` 5 … 60 in multiples of 5 — rounded up on
  copy); `drawdown_stop_pct` → `pf_settings.drawdown_kill_pct`; shorts / fees / margins / borrow / funding →
  paper-council broker params + `cl_broker_params`; `autopilot_heartbeat_minutes` → platform meta
  `autotrade.heartbeat_minutes` (5 … 240; **unreadable → fail closed**: auto trading off + alert, enabling 409
  `heartbeat_unreadable`); `closed_interval_minutes`, `crypto_risk_watch`, `starting_capital` dropped.
- **§22.5 Task + execution:** own task `council-cycle` with its own lock; only the order step enters
  `runtime.submit_source_targets` under the per-account trade lock and re-checks before each submit (auto trading on,
  Council orders on, not halted, no kill, not sandbox, switch-over done) → else `not_executed` + reason. Lock
  ordering: never two account locks; kills walk accounts one at a time. Schedule: first cycle at open + 5 min grace +
  5 s, then every `interval_minutes`, only in session; missed cycles never caught up (`core/calendars.py`). `POST
  /v2/council/run` → 202 `{run_id, started, orders: {equity, crypto}}` / 409 `running` / 503 `not_active`; the run row
  is created up front; unfinished runs are swept to `error` "interrupted …" at task start. Origin `council` (OMS,
  broker maps, client-id prefix `cl-`, re-send gate); auto trading off / dead-man / Council switch off cancel queued
  Council orders; every kill and every backend start force `council.orders_enabled` off (audit
  `council_orders_off`); switch changes audit-first, ON refused while halted (409). Working-order rules: never replace
  pending protective / manual orders; keep an identical / leading order; else cancel ("replaced by a newer target")
  and re-plan, ≤ 1 working Council order per symbol; reversal second leg after the first fill; equity `day`, crypto
  60 min (D11). **Heartbeat:** Council GETs do not touch it; from the R2 + R3 build the app-wide poll of `GET
  /v2/monitoring/alerts` (every 15 s) is the heartbeat. Sandbox: no scheduled cycles / orders; "Run now" = analysis;
  R2 / R4 refused (409 `sandbox`).
- **§22.6 Runtime replaces the crypto risk watch:** quotes, accruals, queued fills, margin, drawdown kill (waits on
  stale crypto marks), snapshots — every 5 min while crypto is held; the 60-min stale crypto expiry added (D11).
- **§22.7 Module moves M1–M6 DONE:** `core/types.py`, `core/params.py`, `core/llm.py`, `data/market_feed.py`,
  `execution/paper_broker.py`, `strategy/features/macro_agent.py`; old `fund/*` paths are 3-line same-object shims
  until R4. Guard `tests/test_s10_no_new_fund_imports.py` (AST): only `fund/`, fund tests / kits, the sandbox test, the
  parity / switchover tests and kits, `execution/migration.py` (until R4), and named `LEGACY_TEST_IMPORTS` may import
  `fund`.
- **§22.8 Switch-over / revert / removal:**
  - **R2 preconditions** (`switchover.blockers`, shown by the dry run `GET /v2/council/switchover`): not sandbox;
    state not done / pending; **§17.9 migration done** (`not_migrated`); `paper-council` exists (`no_account`) with
    empty `_unallocated`; old autopilot off; pre-v25 backup exists (no `*.tmp`) or DB created at v25+; parity accepted
    (`PARITY_ACCEPTED = "2026-10-24"`); today's spend readable; no `legacy_duplicates` (the blocker carries `table`, `rows`; the dialog shows "Table
    cl_runs · n duplicate row(s)"); Council service built
    (`council_unavailable`).
  - **R2 steps** (`POST /v2/council/switchover {reason?}`): audit-first → `pending` with attempt `tx` → the running
    `FundEngine` stopped in-process (≤ 120 s, `_stopping` set first) → ONE transaction (legacy import above the
    high-water mark re-read inside BEGIN IMMEDIATE, params copy, heartbeat meta, spend seed, mark, `council.r2_tx`) →
    `done`, Council orders OFF, Council task started (`council_task_started` / `council_task_error`). Idempotent per
    attempt; `main.py` at start: `done` → no `FundEngine`, Council task; `pending` → `recover()` retries once, failure
    leaves `pending` + alert and runs NO engine; else the old engine. One switch-over operation at a time (409
    `switchover_running`, `switchover_pending`, `concurrent_import`). A failure after `pending` → 503
    `switchover_failed`, critical alert `council_switchover_failed`; `POST …/switchover/retry` (same attempt) and
    `POST …/switchover/abort` (not applied → previous state; applied → like a revert).
  - **Revert** (`POST /v2/council/switchover/revert`, before R4 only, refused in sandbox): Council orders off + working
    orders cancelled → task stopped → old spend seeded → `reverted` → the old engine re-created in-process and
    registered with `control`; `cl_*` rows stay; already reverted → 200 `already`; returns `old_engine_started` /
    `old_engine_error`. The switch-over revert does **not** undo the §17.9 migration.
  - **`/fund/*` per request:** `pending` / `done` → 410 `{"code": "replaced"}` except `/fund/kill` →
    `control.kill_all(via=…)` and `/fund/resume` → `control.resume("paper-council", via=…)` (audited); unreadable →
    503 (kill still forwarded).
  - **`kill_all`** → `{"platform", "legacy", "council"}`: before R2 legacy = the old kill incl. its closing orders,
    council `{"applicable": false}`; after (or pending) legacy "not applicable", council `{applicable, ok,
    orders_enabled: false, cancelled_orders, error}`.
  - **Emergency code rollback below v25** (user's decision only): stop the backend, rename hedgefund.db (+ wal / shm)
    to `*.v25-<UTC>.stale`, copy the pre-v25 backup back, start older code — everything written since is lost.
  - **§22.8a Legacy import** (above `council.legacy_hwm`): old `fund_params` referenced → `cl_params` legacy frozen;
    `fund_runs` (risk-watch runs skipped; trigger `schedule` → `scheduled`) → `cl_runs` + `cl_run_results`;
    `fund_agent_scores` → `cl_signals` (AI provenance only for llm / cached); `fund_recommendations` → `cl_decisions`
    (`rules` never AI-marked); executed → `submitted` "old engine order #N", skipped → `skipped`. Change-detection
    memory rebuilt from the newest legacy decisions.
  - **R3** (WPF, same build): Council view on `/v2/council/*`; `FundNotifications` toasts from the Council alert kinds;
    Research hand-over → `/v2/oms/orders`. **R4** (refused in sandbox): history export (§22.12), golden parity fixture,
    rename to `*.stale` the old `fund/` engine files, shims, `/fund/kill` / `/fund/resume` aliases and the §17.9
    migration (D12). **R5:** suite + build + click-test → delete the `.stale` files (with the user's delete
    permission). newsroom.db keeps its old tables.
- **§22.9 Labels / dropped features:** never incubated / live-eligible / promoted / allocator-managed / decay-monitored;
  "not validated — AI strategy", record "forward paper record, N days, not validated"; sentiment / judge outputs
  "AI-generated" + provenance. Dropped: manual accept, `/fund/export/*`, `/fund/backtest(s)`, old toasts → alert kinds
  `council_flip`, `council_order_filled`, `council_run_failed`, `council_budget_reached`, `council_orders_off`,
  `council_switchover_failed`, `council_legacy_duplicates`.
- **§22.10 Parity (acceptance gate, passed):** harness `tests/council_parity_testkit.py` +
  `tests/test_s10_council_parity.py` (+ `_scenarios.py`; data window (c) = SYNTHETIC bars from
  `tests/council_parity_synth_bars.py` — SPY, QQQ, TLT, GLD, BTC-USD, 260 bars to 2026-10-23, NYSE days / BTC 7 days,
  seeded random walk — `SyntheticBarsTest` always runs; `RealDataTest` reads the git-ignored
  `backend/data/local_fixtures/council_parity_real_bars.json` (`REAL_BARS`, `real_bars_available()`,
  `REAL_BARS_MISSING`) and is skipped without it; re-export with `HedgeFund/tools/export_council_parity_bars.py`
  from a COPY of hedgefund.db, sha256 checked before / after, one source per day alpaca_sip > alpaca > tiingo,
  `adjustment = 'all'`):
  real §17.9 migration on temp copies, ONE injected clock (`WallClockSpy`; only `core.llm.provenance` may read the wall
  clock), scripted fake LLM keyed by (feature, request hash), `SKIP_REASON_MAP` for execution outcomes; result 60
  sessions / 768 ticks / 414 orders / 258 AI calls / **0 mismatches**, 24 scenario tests, the Council started through
  the real `switchover.run`. **Deliberate differences APPROVED by the user 2026-10-24 17:59 (Berlin), "Jo" — all 9:**
  (1) no stock orders while closed / in the grace; (2) first run at open + 5 min + 5 s; (3) no queued stock orders to
  replace; (4) a stock flip while closed is a decision only; (5) unfilled crypto orders expire after 60 min; (6) no
  separate overnight crypto check; (7) platform pre-trade checks; (8) platform status / audit / event names; (9) manual
  accept, old CSV exports, old backtest button and old pop-ups not carried over. Open before R4: the golden fixture;
  lockstep-unreachable cases (crypto $10 leg, working-order replace / keep, pending manual / protective, account
  unreadable) are covered by unit tests only.
- **§22.11 API:** `GET /v2/council/state` (active, switchover, `switchover_error`, task, orders_enabled, label,
  record, next_due_at, budget, last_run, symbols[] with agents / verdict / provenance / target / execution incl. OMS
  `order_state`, sandbox `mode`); `GET /v2/council/runs?limit=`, `GET /v2/council/runs/{id}`; `POST /v2/council/run`;
  `GET/PUT /v2/council/params`; `GET/PUT /v2/council/broker-params`; `PUT /v2/council/orders {enabled, reason?}`;
  `GET /v2/council/switchover` (dry run + `pending` + `actions`); `POST /v2/council/switchover`, `…/retry`, `…/abort`,
  `…/revert`. Errors `{error, code}`. Exact shapes → archive §22.11 / §22.14 and the task file in UFOS.ai/docs/tasks/.
- **§22.12 History export (before R4):** a SQLite-backup temp copy of newsroom.db → `data/backups/stage10-<UTC>/
  old_engine_history.jsonl` (`{"table", "row"}` per row, header with counts) + `.sha256`, counts re-read; the real
  newsroom.db is never written.
## 23. Stage 10 — hardening, security, Kraken removal, .env removal (as-built 2026-10-24)

Full text (per-agent batch notes S10-B / H / K / U / A1 / A2 / E, exact texts, test names): →
spec-archive/23-stage10-hardening-security-env.md. Python settings / secrets contract and the lock protocol: task
files `ENV_API.md` (+ `ENV_API_DEFAULTS.json`) and `LOCK_API.md` in UFOS.ai/docs/tasks/.

- **§22.13 R1 as-built / §22.14 R2 as-built + review fixes:** → `spec-archive/22-stage10-council-migration.md` (cited by `council/switchover.py`).
- **§23.1 Backup + cleanup (S10-B):** backup `HedgeFund/backend/data/backups/stage10-20261024T115619Z/` (hedgefund.db
  v24, newsroom.db v3, market_research.db v7, `SHA256SUMS`); 106 files deleted with the user's permission per
  `DELETE_MANIFEST.txt`; recent `*.stale` copies kept until the click-test (`KEEP_UNTIL_CLICKTEST.txt`). **Agent rule:
  never open a real DB over the VM mount** — copy db + `-wal` to a local temp dir, verify sha256 before = after, open
  the copy.
- **§23.2 Backend hardening (S10-H):**
  - **Tools venv guard:** every `backend/tools/*` CLI refuses a non-backend Python before importing more than stdlib
    (`core/venv_guard.py`; exit codes sandbox 2, options_archive 3, verify_alpaca_options 3, verify_binance_demo 5); a
    new tool needs the guard + an entry in `tests/test_s10_hardening.VenvGuardTest.TOOLS`.
  - **NYSE calendar 2000–2030** (`core/calendars.py`): 2016–2027 hardcoded, 2028–2030 projected from the current NYSE
    rules (special closures must be added when announced); 2031+ → `CalendarUnavailable` (fail closed).
  - Deterministic committee status (no false "interrupted"); `market_research.db` startup race (`not_opened` while the
    first open runs); `incubation.realised` includes the first session.
  - **Context notices** (`core/notices.py`, word-for-word `Shared/AiContent.cs`): ANALYSIS "AI-generated analysis —
    may be inaccurate; information, not a recommendation."; RESEARCH "AI-generated research output for your own
    decision. Not a personal recommendation by a licensed advisor; you decide and carry the risk. Backtests are
    hypothetical."; PAPER_EXECUTION "Automated trading on your own paper account under the limits you set. Paper only
    — no real money."; (d) live notice only if live is ever enabled; short lines PAPER / PAPER_DEMO / HYPOTHETICAL /
    REVIEW. Payload `notice` of portfolio / brokers / monitoring = PAPER_EXECUTION; Studio / loop
    `AI_INTERACTION_NOTICE` = EU AI Act Art. 50(1) sentence + RESEARCH; no user-facing platform text carries the
    blanket "not investment advice" (`NoticesTest`).
- **§23.3 Kraken demo removal (S10-K):** adapter → `.stale`, every kraken-demo branch gone (mirror, service,
  reconcile — cash basis for every broker, live_guard endpoint); `LIVE_HOSTS` keeps the Kraken production hosts; key
  names kept only as `secrets.RETIRED_SECRETS` (POST `/v2/settings/keys/changed` accepts them for action "removed"
  only). Kept: the retirement guard (`accounts.RETIRED`, `mirror.RetiredAdapter`, 409 `retired`, card shown only while
  a kraken-demo row exists — the user's DB has none), ccxt (Binance), `kraken_public.py`. WPF: retired keys shown only
  while stored, "Remove" only.
- **§23.4 WPF hardening (S10-U):** single instance (named mutex `Local\UFOS.ai.App.<SID>.Instance` + activate event;
  guard failure → app starts + non-blocking note); context notices in `Shared/AiContent.cs` (Analysis / Research /
  PaperExecution); Studio discarded replies as one system note; **kill path** = `POST /v2/oms/kill` (the Resume dialog
  per §22.8); Research Buy / Sell / Short / Cover → Trading order dialog (`/v2/oms/orders`).
- **§23.5 Security audit fixes (S10-A1 / A2):** every backend binds **127.0.0.1 only**; **Host-header check** on every
  request + WebSocket (all three `api_auth.py` copies identical; 403 "host not allowed"); public `/health` + `/version`
  trimmed (no pid / paths); HedgeFund WS placeholder 8865 removed; PDF worker env scrubbed; `backend/.gitignore`
  allow-lists `data/`; `OPENROUTER_BASE_URL` only https on openrouter.ai (Python GM / LSA + C#); GM article fetch
  public addresses only (`PublicOnlyResolver`, ≤ 3 manual redirects, LRU 200); GM / LSA log pruning 30 days; generic
  GM error messages. WPF: GM WebView2 locked to the one `NavigateToString` page (CSP with the inline-script hash,
  allow-listed requests / host messages), world-atlas vendored by `Global Monitoring/tools/fetch_world_atlas.py`
  (pinned sha512, one source per load: Vendored / Cdn / VendorRejected); ArticleWindow InPrivate + locked down; all
  PowerShell launches via `Shared/WindowsPowerShell.cs` (`-NoProfile -ExecutionPolicy Bypass -File <x.ps1>`);
  data-source credit lines; `Shared/RotatingLogFile.cs` (5 MB × 3).
- **§23.6 `.env` removed — Credential Manager only (S10-E):** no backend reads `.env` or a secret from the environment
  (guard tests ×3); keys only via HedgeFund `strategy_platform/secrets.py`, GM / LSA `credstore.py`; ONE shared
  `openrouter-api-key`; non-secret tunables in optional strict `<backend>/data/settings.json` (`settings_file.py` ×3
  identical; invalid file refuses the start; AI fields re-read per call → `LlmUnavailable`); `core/llm.py` reads no
  environment. **Pinned dependencies:** `make_lockfile.py` (×3) writes `requirements.lock` (+
  `requirements-broker.lock`) with sha256; `run_*.ps1` / `Shared/PythonEnvironment.cs` install only `--require-hashes
  -r requirements.lock`; the user runs `make_lockfile.py` once per backend. **Docker removed** (user 2026-10-24) →
  `*.stale`. Root `.gitignore`: `/docs/`, `/UFOS.ai/docs/todo.md`, `*.stale`, `*-backup/`. WPF: `Shared/CredentialVault.cs`
  + `CredentialCatalog.cs` (every credential, "Used by"), main app settings `%LOCALAPPDATA%\UFOS.ai\app-settings.json`
  (`Shared/MainAppSettings.cs`), one-time "Import from old .env files…" (`Platform/EnvMigration.cs` — the only code
  that reads a .env), LiveStream Agent YouTube URL field; guard `HedgeFund/tools/check_no_env_cs.py` +
  `tests/test_s10_env_cs_guard.py`.
- **§23.7 Open follow-ups** (tracked in UFOS.ai/docs/todo.md): repo-root README setup text (lead); GM / LSA start on
  the PC; whether the startup notice body (`Shared/AiDisclaimerWindow.cs`, user-mandated) and MainWindow / GM / LSA
  wording follow the stage-10 notices (user decision). Resolved: CLAUDE.md, Docker, lock files, GM / LSA key names
  in the keys audit (`secrets.OTHER_MODULE_SECRETS`), `BackendSettingSpecs` tied to the schemas
  (`ImportSpecsMatchSchemasTest`).

## 24. Financial Newsroom — Disclosures (insider returns; INSIDER-RET, 2026-10-24)

Contract: `UFOS.ai/docs/tasks/INSIDER_RET_API.md` (decisions §11). Backend built (slices a–c, Agent H); the WPF
Disclosures section (Insider tab, Track record, status line) is open. As-delivered notes:
→ spec-archive/pending-docs-2026-10-24-25.md

- **Retention:** disclosures (insider trades; later congress) are kept **2 years** by filing date — `disclosure_trades`,
  `disclosure_returns` (`newsroom/db.py` `DISCLOSURES_RETENTION_DAYS = 730`, daily `prune()`; its result gains the key
  "disclosures" only when rows were removed). The 5-day `insider_filings` list (old Insider Trades view + AI summary) is
  unchanged.
- **Storage:** newsroom.db **v4** — `disclosure_trades`, `disclosure_returns`, `issuer_sic`, `disclosure_jobs`.
- **Routes** (token-protected): `GET /newsroom/disclosures/insider` (filters `days`, `code`, `role`, `plan`, `cluster`,
  `ticker`, `measurable`, `amendments`, `limit`, `offset`; "N of M shown"; ONE return chip per row); `GET
  /newsroom/disclosures/insider/{id}` (both start points, all horizons, 10b5-1, official SEC link); `GET
  /newsroom/disclosures/track-record?source=insider&group=direction|plan|role|cluster&horizon=&start=` (n, mean / median
  score, hit rate, small-sample flag, caveat — descriptive; SIC → GICS sector-ETF approximation); `GET
  /newsroom/disclosures/markers?ticker=&from=&to=&kinds=insider` (dated by the filing day, merged per day / side); `GET
  /newsroom/disclosures/status` (backfill steps + gaps, daily job, live writes, counts, unmapped SIC codes, DB sizes,
  current SEC interval); `POST /newsroom/disclosures/backfill {"action": "start"|"pause"|"resume"}` (the user's button;
  Origin header → 403, wrong state → 409).
- **Returns:** from filing = the first 1-minute IEX bar after the EDGAR acceptance minute, else the next open; from
  trade date = that close (not tradable). Horizons 1 / 5 / 20 / 60 sessions; sector ETF over the same window; excess =
  r − r_etf; sales scored −excess; adjusted bars (splits + dividends); no SPY substitute.
- **Clusters** point-in-time (only trades already filed count — no relabelling, no look-ahead in the track record);
  thresholds of `insider_summary` (2 buyers / 3 unplanned sellers within 14 days).
- **Shared SEC limiter** (`newsroom/sec_limiter.py`): collector + daily job + backfill share one request schedule
  (`sec_edgar_min_request_interval_seconds`; 0.2 s while the backfill's EDGAR steps run).
- **Daily job** 17:00 New York on trading days (+ at start when missed), research priority, no AI. **Backfill**
  user-started: SEC quarterly data sets (links read from the SEC page) → issuer submissions for acceptance times + SIC (a
  pilot stops and asks if missing) → gap days via the daily index → prices outside US market hours.
- Tests: `test_disclosures_engine.py`, `test_disclosures_live.py`, `test_disclosures_backfill.py`.

## 25. Cross-module UI rules (full screen, module tiles)

As-delivered notes (W, 2026-10-24): → spec-archive/pending-docs-2026-10-24-25.md. Tasks:
`UFOS.ai/docs/tasks/CHART_FULLSCREEN_TASK.md`, `TILES_TASK.md`, `TILES_API.md` (decisions §10).

- **§25.1 Full screen "⛶"** (user rule 2026-10-24): every chart, map and dense table has "⛶" (tooltip "Full screen (Esc
  to exit)"); F11 works while the keyboard focus is inside it. True full screen of the monitor; SAME instance (no
  reload — drawings, zoom, timeframe kept); a top bar with the title, live header text and an exit control; Esc
  restores the exact window state (an Esc already handled by an inner popup / dropdown closes only that); a view that is
  replaced while full screen (section switch, pop-out closed) exits full screen first. One building block
  `Shared/FullScreenHost.cs` (in place, never re-parents, no WebView2 re-creation); HedgeFund helper
  `FundUi.FullScreenButton` / `HeadingWithFullScreen`. Places (built, click-test open): Research chart (also in the
  pop-out), Global Monitoring globe (layer toggles become a floating menu, event feed collapsible), backtest report
  charts (equity, drawdown, IC decay), Newsroom macro view (zoomed 1–2 at ≥ 1500 px), Overview equity curve, Loop
  generation grid; focus mode for the Orders blotter, Transaction Log and System Logs. Disclosures / Track record follow
  when built.
- **§25.2 Module tiles** (user rule 2026-10-24): three preview modes per tile — **Live** (2–3 key figures; never starts a
  backend, no AI or paid call; stored figures shown "backend off — as of HH:MM" — accepted by the user 2026-10-24;
  sandbox figures with a SANDBOX chip and never stored), **Last view** (window picture blurred IN MEMORY before any
  write, stored only under `%LOCALAPPDATA%\UFOS.ai\tile-previews\`, never in the repo, never a picture of sandbox data;
  captured every 60 s while the module window is visible and active, one more at Closing only if ready within 300 ms),
  **Illustration**; chosen via "⋯", persisted in app-settings.json `tile_modes` (an invalid entry → visible error +
  Live). A mode a module cannot offer is a disabled menu item with its reason, and the tile shows the Illustration with
  that reason. Contracts in the public project `UFOS.ai.ModuleTiles`; providers live in the modules (GET-only loopback
  reads: Global Monitoring `GET /events/summary?hours=24`, LiveStream Agent `GET /verdicts?limit=1` labelled "last
  verdict" (AI), Hedge Fund `GET /v2/portfolio/summary` (§17.15) → Equity (primary-currency total; other currencies
  only in the detail line, never added up), Day P&L (+ % of the day reference), Trading (KILLED / HALTED / kill pending,
  else auto ON|OFF, + Council ON|OFF after the switch-over), sparkline from paper-main's stored snapshots; a value the
  route lacks reads "not available" with the route's reason). A static test keeps the C# parser's field names in step with
  the route payload (`tests/test_tiles_static.py`). Status: wired 2026-10-25 (static checks + offline parser check);
  user build + click-test pending.
