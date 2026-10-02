# HedgeFund autonomous loop — implementation spec (contract)

> Part of UFOS.ai, a proof-of-concept portfolio project — not a published
> product, not investment advice. **Paper trading only**: there is no live
> trading code path at all (the broker only accepts `mode="paper"`).

Agreed with the user in the interview of 2026-09-26. This file is the
contract between the backend package `HedgeFund/backend/fund/`, its HTTP API
and the WPF window. Change it together with the code.

## 0. Ground rules (all parts)

- Python code runs on **Python 3.14 (Windows venv)**; the offline tests are
  also run with **Python 3.10** without third-party packages → no 3.11+
  syntax/stdlib (`typing.Self`, `datetime.UTC`, `tomllib`, `ExceptionGroup`),
  and **import `yfinance`, `pandas`, `openai`, `aiohttp` lazily** inside the
  functions that need them, never at module top level of `fund/*` (except
  `fund/api.py`, which may import aiohttp). Tests use fakes.
- Stdlib `unittest` tests in `HedgeFund/backend/tests/test_fund_*.py`, run
  from `HedgeFund/backend` with `python3 -m unittest discover -s tests`.
  Every test uses a temp directory / in-memory data — never the real DB.
- Timezones: store UTC ISO-8601 strings (`2026-09-26T14:05:00+00:00`,
  `timespec="seconds"`). Market logic uses `zoneinfo.ZoneInfo("America/New_York")`
  (`tzdata` is added to requirements.txt for Windows).
- Money in USD floats, rounded to cents only for display. Weights are
  fractions of equity (0.05 = 5 %); UI percentages are converted at the API edge
  only where stated (params store percentages as the UI shows them).
- English for code, comments, UI text. Keep the "proof of concept / not
  investment advice" tone.
- AI labelling (EU AI Act Art. 50, CLAUDE.md): every LLM-produced text carries
  a `provenance` dict (`newsroom.ai.provenance(feature, model)`, feature ids
  `fund-judge`, `fund-sentiment`); rule output is marked `source: "rules"` and
  never dressed as AI. Every OpenRouter call records its cost via
  `newsroom.cost_ledger` (module "HedgeFund", features `fund-judge` /
  `fund-sentiment`, labels "Hedge Fund · Judge", "Hedge Fund · Sentiment").
- Security (CLAUDE.md "Security by default"): all `/fund/*` routes sit behind
  the existing `api_auth` middleware (session token) automatically. Every
  state-changing call writes an audit row. No secrets in logs.

## 1. Package layout (`HedgeFund/backend/fund/`)

| File | Owner | Purpose |
|---|---|---|
| `types.py` | lead | shared dataclasses (given, don't change without the lead) |
| `params.py` | A | defaults, limits, validation, merge |
| `store.py` | A | all SQLite access for the fund (uses `newsroom.db`) |
| `market.py` | A | `PriceFeed` interface, `YFinanceFeed`, market calendar |
| `indicators.py` | B | pure indicator helpers (`sma`, `ema`, `rsi`, `atr`, `atr_pct`, `returns`, `max_drawdown`) |
| `llm.py` | B | `chat_json(...)` OpenRouter call (reuses `newsroom.ai` settings/provenance/usage/cost ledger) + `Budget` |
| `broker.py` | A | `PaperBroker` (fills, fees, pending orders, realistic shorts, margin, accruals, dividends) |
| `agents/technical.py`, `agents/macro.py`, `agents/sentiment.py`, `agents/risk.py` | B | the four agents |
| `judge.py` | B | change detection, rule judge, LLM judge (batch, JSON), model selection, budget |
| `sizing.py` | B | target weight from confidence, volatility, risk multiplier; trade decision |
| `backtest.py` | B | rule-only backtest over daily bars |
| `engine.py` | lead (D) | the loop, cycle orchestration, halt/kill/drawdown, events |
| `api.py` | lead (D) | aiohttp routes `/fund/*` |
| `export.py` | lead (D) | CSV exports |

Schema changes go into `newsroom/db.py` `MIGRATIONS` as **version 3** (same
file `data/newsroom.db`). Agent A owns that edit.

## 2. Database — schema v3 (in `newsroom/db.py`)

New tables:

```sql
CREATE TABLE fund_params (
  version INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
  params_json TEXT NOT NULL, actor TEXT NOT NULL, note TEXT);
CREATE TABLE fund_runs (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  params_version INTEGER NOT NULL, trigger TEXT NOT NULL,      -- schedule|manual|startup|risk-watch
  status TEXT NOT NULL,                                         -- running|ok|partial|error|skipped
  market_open INTEGER NOT NULL, judge_mode TEXT,                -- llm|rules|mixed|none
  models TEXT, cost_usd REAL NOT NULL DEFAULT 0,
  error TEXT, summary_json TEXT);
CREATE INDEX ix_fund_runs_started ON fund_runs(started_at);
CREATE TABLE fund_agent_scores (
  run_id INTEGER NOT NULL, symbol TEXT NOT NULL, agent TEXT NOT NULL,
  score REAL NOT NULL, confidence REAL NOT NULL, summary TEXT,
  details_json TEXT, source TEXT NOT NULL,                      -- rules|llm|cached|unavailable
  provenance_json TEXT,
  PRIMARY KEY (run_id, symbol, agent));
CREATE TABLE fund_recommendations (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, ts TEXT NOT NULL,
  symbol TEXT NOT NULL, action TEXT NOT NULL,                   -- LONG|SHORT|FLAT
  confidence REAL NOT NULL, target_weight REAL NOT NULL,        -- signed fraction of equity
  rule_score REAL, rationale TEXT, source TEXT NOT NULL,        -- llm|rules|carried
  model TEXT, provenance_json TEXT, usage_json TEXT,
  status TEXT NOT NULL DEFAULT 'open',                          -- open|executed|skipped|superseded
  skip_reason TEXT, order_id INTEGER);
CREATE INDEX ix_fund_recs_symbol ON fund_recommendations(symbol, id);
CREATE TABLE fund_events (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  kind TEXT NOT NULL,        -- flip|order|halt|margin_call|resume|error|autopilot
  symbol TEXT, title TEXT NOT NULL, body TEXT);
CREATE TABLE fund_audit (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  actor TEXT NOT NULL,       -- user|engine|system
  action TEXT NOT NULL, detail_json TEXT);
CREATE TABLE fund_cash_ledger (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  kind TEXT NOT NULL,        -- deposit|reset|fill|fee|borrow_fee|dividend|dividend_short|funding|liquidation_fee
  symbol TEXT, amount REAL NOT NULL, note TEXT);
CREATE TABLE fund_backtests (
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
  request_json TEXT NOT NULL, result_json TEXT NOT NULL);
```

Extensions of the v2 tables (ALTER TABLE ... ADD COLUMN):

- `orders`: `run_id INTEGER`, `recommendation_id INTEGER`,
  `origin TEXT` (autopilot|manual|manual-accept|kill-switch|drawdown-stop|margin-call|reset),
  `fill_price REAL`, `fees REAL`, `filled_at TEXT`, `reject_reason TEXT`,
  `notional REAL`. `side` values: `buy`, `sell`, `short`, `cover`.
  `status`: `pending` (market closed, executes at next open), `filled`,
  `rejected`, `cancelled`. `order_type` is `market`. `mode` stays `paper`.
- `positions`: `opened_at TEXT`, `realized_pnl REAL DEFAULT 0`,
  `borrow_rate_pct REAL` (shorts), `last_price REAL`. `qty < 0` = short.
- `equity_curve`: `long_mv REAL`, `short_mv REAL`, `benchmark REAL`
  (SPY close), `drawdown_pct REAL`.

State in the existing `meta` table, keys prefixed `fund.`:
`fund.autopilot` ("0"/"1", **forced to "0" at every backend start**),
`fund.halted` (JSON `{"active":bool,"reason":str,"at":iso}`), `fund.peak_equity`,
`fund.reset_at`, `fund.starting_capital`, `fund.last_accrual_date`
(YYYY-MM-DD, NY date), `fund.last_dividend_date` (NY date of the last
**complete** dividend scan — informative only, dedupe is by ledger note, §6),
`fund.ai_spend` (JSON `{"day":"YYYY-MM-DD","usd":float}`, UTC day),
`fund.last_funding_at` (UTC ISO of the last perp funding window booked, §6),
`fund.crypto_rollout` (UTC ISO; the one-time crypto watchlist migration ran, §3).
Order and position `qty` are REAL: whole shares for equities, up to 8
decimals for crypto.

Retention: `fund_runs`/`fund_agent_scores` 90 days (prune by `started_at`),
**except runs that produced a recommendation or an order** — their runs and
agent scores are the decision inputs behind kept rows (audit trail,
decisions export) and stay; recommendations, orders, positions, equity, cash
ledger, audit, params versions, backtests are kept; events 30 days. Prices:
existing 2-year rule.

## 3. Parameters (`params.py`)

Stored versioned in `fund_params`. `GET` returns the latest version. A
cycle reads the latest version **once at its start** and records
`params_version` in `fund_runs`. Changes never affect a running cycle.

```python
DEFAULTS = {
  "watchlist": ["SPY","QQQ","IWM","TLT","GLD","USO","XLE","XLF","BTC-USD","ETH-USD","SOL-USD"],
  "interval_minutes": 5,                  # 1..60, scheduled full cycles, US regular session only
  "closed_interval_minutes": 30,          # 5..240, crypto RISK-WATCH interval outside the session (§10)
  "min_confidence": 75,                   # %, 50..95 (existing slider)
  "max_position_pct": 5.0,                # % of equity per symbol, 1..15
  "max_trades_per_day": 6,                # autopilot orders per NY day, 1..20
  "drawdown_stop_pct": 8.0,               # % from peak equity, 2..20
  "agent_weights": {"technical": 1.0, "macro": 1.0, "sentiment": 0.5},  # 0..1 each, 0 = off
  "risk_enabled": True,
  "judge_model_mode": "standard",         # standard|premium|premium_on_flip
  "ai_daily_budget_usd": 1.0,             # 0..50; 0 = never call the LLM
  "fees_bps": 5.0,                        # commission+slippage per fill, 0..50
  "allow_shorts": True,
  "short_exposure_max_pct": 30.0,         # gross short MV / equity, 0..100
  "initial_margin_pct": 50.0,             # Reg T, 25..100
  "maintenance_margin_pct": 30.0,         # 25..100, <= initial
  "borrow_rate_default_pct": 0.3,         # annual, easy-to-borrow ETFs
  "borrow_rate_stock_pct": 3.0,           # annual, default for non-ETF symbols
  "borrow_rate_overrides": {},            # {"SYMBOL": pct}
  "not_shortable": [],                    # symbols without a locate
  "min_hold_days": 1,                     # 0..10, NY trading days
  "change_threshold": 0.15,               # agent score change that re-triggers the judge, 0.01..1
  "judge_stale_hours": 6,                 # re-judge after this many hours anyway, 1..48
  "starting_capital": 100000.0,           # used only by POST /fund/reset
  "autopilot_heartbeat_minutes": 15,      # 5..240, dead-man switch of the autopilot (§10)
  # crypto (2026-09-27, §6)
  "crypto_fees_bps": 20.0,                # 0..100, per fill: half slippage in the price, half fee
  "crypto_max_position_pct": 3.0,         # 0.5..15, % of equity per crypto symbol
  "crypto_initial_margin_pct": 50.0,      # 10..100, perp shorts, isolated margin
  "crypto_maintenance_margin_pct": 25.0,  # 5..100, STRICTLY < crypto_initial_margin_pct (400 otherwise)
  "crypto_funding_rate_pct_8h": 0.01,     # -0.5..0.5; + = longs pay shorts -> our perp shorts RECEIVE it
  "crypto_liquidation_fee_pct": 0.5,      # 0..5, extra fee of the notional on a forced perp cover
  "crypto_risk_watch": True,              # light risk watch outside the session (§10)
}
```

- `validate(partial) -> (clean_partial, errors)`; `merge(current, partial)`.
  Unknown keys → error. Watchlist: 1..25 symbols, `^[A-Z][A-Z0-9.\-]{0,9}$`,
  upper-cased, deduplicated. `maintenance_margin_pct <= initial_margin_pct`.
- `LIMITS` dict (min/max/step/type per key) is served to the UI.
- Numbers that are not finite or too large for a float (JSON `1e999`,
  `10**400`) are field errors (400), never a 500.
- `ETF_SYMBOLS` (known ETFs → default borrow rate) lives in `params.py`:
  SPY QQQ IWM DIA TLT IEF SHY GLD SLV USO UNG XLE XLF XLK XLV XLI XLY XLP XLU XLB XLRE XLC EEM EFA HYG LQD UUP VNQ.

### Crypto symbols and the rollout (2026-09-27)

- **Detection:** symbols matching `^[A-Z0-9]{2,10}-USD$` are asset class
  `crypto` (`params.is_crypto` / `asset_type` → "crypto"|"equity");
  `KNOWN_CRYPTO` (BTC, ETH, SOL, XRP, ADA, DOGE, AVAX, LINK, DOT, LTC, BCH
  vs USD) is documentation only. Watchlist / not_shortable pattern (served
  in LIMITS): `^(?:[A-Z][A-Z0-9.\-]{0,9}|[A-Z0-9]{2,10}-USD)$`.
- Class helpers: `fees_bps_for`, `max_position_pct_for`, `margin_pcts_for`
  (crypto_* keys for crypto); `borrow_rate_for(crypto) = 0`.
- LIMITS entries may carry `label` / `description` (UI text):
  `closed_interval_minutes` is labelled "Risk-watch interval (outside the
  US session)"; every crypto key has one.
- **Rollout (one-time, engine start):** when meta `fund.crypto_rollout` is
  not set and the latest stored watchlist contains none of BTC-USD / ETH-USD
  / SOL-USD, a new params version (actor `system`, note `crypto rollout:
  added BTC-USD, ETH-USD, SOL-USD`) appends them (only as many as the
  25-symbol cap allows), an audit row `system`/`crypto_rollout` is written
  and the flag is set. A watchlist that already holds one of them only sets
  the flag. **Fresh install** (no stored version): the crypto-inclusive
  DEFAULTS are stored explicitly as version 1 (actor `system`, note
  "initial parameters (defaults incl. BTC-USD, ETH-USD, SOL-USD)") + audit
  row + flag. Removing the symbols later is respected (never re-added).

## 4. Shared types (`types.py`, given)

See the file. Key ones: `Bar`, `Quote`, `AgentSignal`, `RiskAssessment`,
`MarketContext`, `JudgeDecision`, `PositionView`, `AccountView`,
`OrderRequest`, `OrderResult`.

## 5. Market data (`market.py`)

- `class PriceFeed(Protocol)`: `daily_bars(symbol, days=400) -> list[Bar]`
  (oldest first, adjusted), `quote(symbol) -> Quote|None`,
  `dividends(symbol, since: date) -> list[(date, amount_per_share)] | None`
  (`[]` = none, `None` = the fetch failed → unknown, retried later; results
  cached 2 h, failures not cached),
  `news(symbol, limit=8) -> list[dict(title, publisher, published_at, url)]`.
- `YFinanceFeed(cache_store)`: yfinance (lazy import), blocking calls are
  wrapped by the engine in `asyncio.to_thread`. Daily bars cached in the
  `prices` table (source "yfinance"), refetched at most every 30 min during
  the session / once after the close. Quote = `fast_info.last_price` with
  `delayed=True`, fallback last daily close. Failures return `None`/`[]` and
  log once per symbol per hour — never raise into the engine.
- Swappable later (Alpaca): nothing outside `market.py` imports yfinance.
- Market calendar (NYSE): `session_state(now) -> dict(open: bool,
  session: "regular"|"closed", next_open: iso, next_close: iso|None,
  trading_day: "YYYY-MM-DD")`. Regular session 09:30–16:00 America/New_York,
  Mon–Fri, minus NYSE holidays 2026–2027 (hardcoded list; update yearly) and
  early closes 13:00 (day after Thanksgiving, Christmas Eve when a weekday,
  Jul 3 when a weekday). `trading_days_between(a, b)`.
- Indicator helpers live in `indicators.py` (owner B), pure functions:
  `sma(values, n)`, `ema`, `rsi(values, 14)`, `atr(bars, 14)`, `atr_pct`,
  `returns`, `max_drawdown(values)`.

### Crypto (2026-09-27)

- `is_tradable(symbol, now)`: crypto always (24/7); equities/ETFs while the
  NYSE regular session is open (the broker adds the 5-min opening grace).
  `session_state` stays the US session only.
- `utc_days_between(a, b)` (crypto min-hold), `last_funding_window(now)`,
  `funding_windows_between(a, b)` — perp funding at 00:00/08:00/16:00 UTC.
- `YFinanceFeed`: crypto daily bars run 7 days a week (UTC dates) and are
  refreshed every 30 min around the clock; crypto quotes are near real-time
  (`Quote.delayed = False`, Yahoo/CoinMarketCap, not exchange-grade).
- **Stale quotes (review 2026-09-27):** when the Yahoo quote fails, the
  fallback (last cached daily close) carries `Quote.stale = True` and the
  bar's own timestamp (its date 00:00 UTC), never "now".
  `quote_is_stale(q, now)` = no quote, or a stale quote older than
  `STALE_QUOTE_MINUTES` (30). Crypto orders never fill on such a quote
  (they queue), and the engine never liquidates or drawdown-stops on it
  (§6, §10).

## 6. Paper broker (`broker.py`) — realistic long/short

`PaperBroker(store, feed_quote_fn, clock)`; all methods sync (the engine calls
them via `to_thread`). Only `mode="paper"` exists.

Account model (cash account for longs, Reg T margin for shorts):

- `cash` includes short-sale proceeds. `L` = Σ long MV, `S` = Σ |short MV|
  (marked to the latest quote). **Equity `E = cash + L − S`.**
- Longs are fully paid (no leverage on longs).
- **Buying power** `BP = cash − S × (1 + initial_margin)` for new longs
  (short proceeds and the initial margin on open shorts are locked).
  A new short of value V requires `V × initial_margin <= BP` (after fees).
- **Short limits**: `allow_shorts`, symbol not in `not_shortable`, gross
  `S_after <= short_exposure_max_pct × E`.
- **SEC Rule 201 (circuit breaker)**: a new short is rejected when the last
  price is ≤ 90 % of the previous close (rule applies for the rest of the day
  and the next day — approximate: today's or yesterday's close-to-low drop ≥ 10 %
  using daily bars if available).
- **Borrow fee**: accrued once per NY calendar day (catch-up for missed days
  from `fund.last_accrual_date`): `|short MV| × rate/360`, rate =
  override → ETF default → stock default. Booked to `fund_cash_ledger`
  (`borrow_fee`, negative) and deducted from cash.
- **Dividends**: every cycle, for every symbol held now or traded recently,
  ex-dates in the trailing 10 days (`DIVIDEND_LOOKBACK_DAYS`) up to today
  are scanned and **deduplicated by (symbol, ex-date)** against the booked
  ledger rows (note prefix `ex YYYY-MM-DD:`) — nothing is booked twice and a
  failed fetch (`None`) is simply retried. Quantity = shares held when the
  ex-date began (NY midnight): longs receive `qty × amount` (`dividend`),
  shorts pay it (`dividend_short`, negative). `fund.last_dividend_date` =
  the day of the last complete scan.
- **Maintenance margin / margin call**: after marking to market, if
  `cash − S < maintenance_margin × S` (equity behind the shorts below
  maintenance) → forced buy-to-cover of shorts (largest first) until satisfied;
  orders `origin="margin-call"`, event `margin_call`. If the market is closed
  the cover orders are `pending` and executed at the open (realistic: no
  trading while closed).
- **Fills**: market orders only. During the regular session: fill at the
  (delayed) quote with adverse slippage `fees_bps/2` in the price plus a fee
  of `fees_bps/2` of notional (together = `fees_bps`). Outside the session:
  status `pending`; `process_pending(now)` fills them at the first cycle in
  the session after re-validating (a pending buy that no longer fits BP is
  reduced or rejected). Quantity: whole shares (`floor`), min 1 share, else
  reject "size below one share".
- **Opening grace**: no fills during the first 5 minutes of the session
  (09:30–09:35 NY, `PENDING_FILL_DELAY_MINUTES`) — the delayed yfinance quote
  can still be yesterday's close; new orders are queued and pending ones
  wait. A quote whose timestamp is before today's open counts as stale.
- **Transitions**: long→short = `sell` all + `short` rest (two orders);
  short→long = `cover` + `buy`. The second leg is sent only after the first
  leg has **filled**: while the market is closed (or in the opening grace)
  only leg 1 is queued; the next cycle after its fill places leg 2.
- **Pending orders and `target()`**: protective pending orders
  (kill-switch/drawdown-stop/margin-call) and user orders (`manual`,
  `manual-accept`) are never cancelled by `target()`; the target is skipped
  ("protective order pending" / "manual order pending"). Autopilot targets
  may only replace pending autopilot orders, manual-accept may replace
  autopilot/manual-accept ones. An identical pending order (or leg 1 of the
  same transition) is kept (no cancel/re-queue churn while closed).
  `cancel_pending(origin, reason)` cancels the queued orders of one origin
  (used when the autopilot is switched off, §10).
- **Unreadable state fails closed**: cash and positions are read in one go
  via `store.account_snapshot()` (`None` on a SQLite failure; also
  `store.cash_balance()` → `None`). `account`, `snapshot_equity`,
  `check_margin`, `reset` then raise `StateUnavailable` instead of computing
  with cash 0; `submit`/`target` return an unrecorded rejection;
  `process_pending`/`daily_accruals` leave their work for the next cycle.
- Realized P&L per position on reduce/close; `avg_price` weighted for adds.
- API: `account(prices) -> AccountView`, `positions() -> list[PositionView]`,
  `submit(OrderRequest, now) -> OrderResult` (validates, records the order,
  fills or queues), `target(symbol, target_weight, origin, now, ...)` → list of
  `OrderResult` (computes the orders to reach a signed target weight),
  `liquidate_all(origin, now)`, `cancel_pending(origin, reason)`,
  `process_pending(now)`, `daily_accruals(now)`, `check_margin(now)`,
  `reset(starting_capital, now)`.
- Every fill/fee/borrow/dividend writes `fund_cash_ledger`; every order writes
  `orders`; equity snapshots via `snapshot_equity(now, benchmark_price)`.

### Crypto (2026-09-27)

Per symbol class (`params.is_crypto`); equities keep every rule above.

- **Fills:** crypto is tradable 24/7 — no session, no opening grace, a quote
  is never stale; queued crypto orders (e.g. no quote) fill at the next
  cycle. Quantity rounded DOWN to 8 decimals; below **$10 notional** →
  reject "size below minimum ($10 notional)" (closing a whole position is
  always allowed, dust included). Fees `crypto_fees_bps` (same half/half
  split). Transitions place both legs at once (both fill).
- **Longs = spot** (fully paid, no funding). **Shorts = simulated perpetual
  futures**, booked like a short sale (notional credited to cash, liability
  marked to market — the same P&L) but with their own margin:
  - buying power `BP = cash − S_eq·(1+im_eq) − S_cr·(1+im_cr)`; a new crypto
    short V needs `V·crypto_initial_margin + fees ≤ BP`;
  - **isolated perp margin per position**, measured against the **entry
    quote** `ref = avg_price / (1 − crypto_fees_bps/2)` (the fill price
    without the adverse slippage): posted = crypto_initial_margin × ref ×
    qty; when `posted + (ref − mark)·qty < crypto_maintenance_margin ×
    current notional` the whole short is
    **liquidated** (forced cover, origin `margin-call`, fills at once) and
    an extra ledger row `liquidation_fee` = `crypto_liquidation_fee_pct` of
    the notional is booked (included in the order's `fees`); event
    `margin_call` "Liquidation: crypto perp short force-covered";
  - account-level maintenance `R = mm_eq·S_eq + mm_cr·S_cr`; when
    `cash − S < R` forced covers, largest short first (a crypto short is
    covered whole and pays the liquidation fee; equity covers pending while
    closed). The short-exposure cap stays **gross over all shorts**.
  A fresh short therefore starts at `(im − mm) × notional > 0` (params
  require mm < im strictly), never below maintenance because of fees.
- Crypto positions without a live mark (`market.quote_is_stale`) are
  skipped by both margin steps; `broker.margin_warnings` lists them (the
  engine marks the run `partial`).
- **Funding** (every 8 h, 00/08/16 UTC, catch-up from `fund.last_funding_at`;
  only windows after the position's `opened_at`; marked at the current
  price): amount = |short notional| × `crypto_funding_rate_pct_8h`/100 per
  window, **one ledger row per symbol and window** (note key
  `fw YYYY-MM-DDTHH`, deduplicated via `store.booked_funding_keys`). Due
  windows of a short are booked **before any fill that changes it** (manual
  cover, kill switch, drawdown stop, liquidation, pending cover, adds), so a
  short closed between accruals keeps its funding. `fund.last_funding_at`
  advances only when every symbol was booked; a failure mid-way is retried
  without double booking.
  **Sign: positive rate = longs pay shorts, so our perp shorts RECEIVE it**
  (ledger `funding`, +); a negative rate makes them pay. Spot longs pay no
  funding. The first call only initialises the meta key (reset sets it to
  the latest window). Booked in `daily_accruals` (key `funding`,
  `funding_windows`).
- **No** borrow fee, dividends or SEC Rule 201 for crypto;
  `borrow_rate_pct` is null for crypto positions.
- `process_pending(now, params, prices, origins=None)` fills per symbol;
  `origins` limits it (the risk watch passes protective + manual +
  manual-accept). `expire_pending(origin, older_than, now, reason)` cancels
  old queued crypto orders of one origin.
- `AccountView` adds `funding_total` (signed sum of `funding` since the last
  reset) and `crypto_margin_excess` (sum of the isolated perp excesses,
  null without crypto shorts); `maintenance_required` / `margin_excess` are
  the account-level R and `cash − S − R`. `PositionView.asset_class`,
  `.instrument` ("spot" | "perp" | "stock").

## 7. Agents (`agents/*.py`) — `evaluate(ctx: MarketContext, symbol) -> AgentSignal`

Scores in [-1, 1] (+ = bullish/long), confidence in [0, 1], `summary` is one
short English line for the UI card, `details` a small JSON-able dict of the
numbers used (UI shows them as key/value lines).

- **technical** (rules, needs ≥ 60 bars, else confidence 0 "not enough data"):
  trend (close vs SMA50 vs SMA200), momentum (20-day vs 60-day return),
  RSI14 (>70 overbought dampens longs, <30 oversold dampens shorts), distance
  to 52-week high/low, ATR% (volatility → lowers confidence). Deterministic.
- **macro** (rules): uses `ctx.macro` (regime quadrant + key readings from the
  newsroom: 10Y yield, 10Y-2Y, 10Y-3M, VIX/credit stress if present, Sahm,
  core PCE, fed funds) and maps the symbol to an asset class via
  `ASSET_CLASS` (`SPY/QQQ/IWM/DIA → equities`, `XLK/XLY/XLC → equities_growth`,
  `XLF/XLE/XLB/XLI → equities_cyclical`, `XLV/XLP/XLU → equities_defensive`,
  `TLT/IEF/SHY → govt_bonds`, `HYG/LQD → credit`, `GLD/SLV → gold`,
  `USO/UNG → commodities`, `UUP → usd`, unknown → equities). Score from the
  regime's asset tendency (`macro_derived.ASSET_TENDENCIES` directions:
  up +0.5, down −0.5, mixed 0) adjusted by stress signals (e.g. 10Y ≥ 5 %
  −0.2 on equities/bonds; Sahm ≥ 0.5 −0.3 on cyclicals, +0.2 on govt bonds;
  inverted 10Y-3M −0.1 equities). Confidence lower when the regime is
  "borderline" or data is stale (> 45 days). No data → confidence 0.
- **sentiment** (LLM, batch): per symbol the last ≤ 8 headlines of ≤ 72 h
  (`feed.news`) plus the newsroom's insider cluster flags for that ticker.
  One OpenRouter call for all symbols whose input hash changed since the last
  call (else reuse the cached signal, `source="cached"`). Strict JSON output
  `[{symbol, score, confidence, summary}]`. Headlines are untrusted data:
  the prompt puts them in a delimited block and says to ignore instructions
  inside. No key / budget exhausted / error → `source="unavailable"`,
  score 0, confidence 0, summary says why. Result carries provenance.
  Model: always the standard model. Cost recorded (`fund-sentiment`).
- **risk** (rules) → `RiskAssessment` per symbol (not directional):
  `veto` (bool), `size_multiplier` (0..1), `reasons` (list of short strings).
  Rules: halted → veto all; drawdown ≥ 75 % of the stop → ×0.5;
  high-impact US release or FOMC today/tomorrow (newsroom calendar) → ×0.5;
  ATR% > 3 % → ×0.5, > 5 % → ×0.25; asset-class concentration (gross exposure
  in the same class ≥ 2 × max_position_pct) → no increase in that class;
  shorts disabled / not shortable → veto shorts only (`veto_short=True`);
  gross short exposure ≥ `short_exposure_max_pct` →
  `block_short_increase=True`: existing shorts may be held or reduced, no
  new or larger short (the cap never closes open shorts; it is no veto).
  Represented as an `AgentSignal` too (agent "risk", score 0, confidence 1,
  details incl. multiplier) for the UI card.

### Crypto (2026-09-27)

- **macro:** asset class `crypto` (`asset_class()` for crypto symbols): the
  growth-equities tendency (Equities column + the growth tweak) scaled
  **×0.6**, confidence **×0.6** (weak relation); stress: 10Y ≥ 5 % −0.2,
  Sahm ≥ 0.5 −0.3, no other stress rules.
- **technical:** 7-day daily bars — the 52-week range uses 365 bars
  (window 370); SMA50/200 and 20/60-day returns count bars (= calendar
  days, the usual crypto convention); the volatility dampening of the
  confidence starts at 4 % ATR instead of 2 %.
- **risk:** ATR thresholds for crypto ×0.5 above 6 %, ×0.25 above 9 %
  (equities 3 % / 5 %); concentration uses `crypto_max_position_pct`.
- **sentiment:** unchanged (yfinance news works for BTC-USD).
- **judge:** the prompt explains that crypto trades 24/7 (spot longs, perp
  shorts with funding/liquidation risk); each symbol carries `asset_class`
  and `trading_hours` ("24/7" | "US session").

## 8. Judge (`judge.py`)

1. **Rule score** per symbol: `s = Σ(w_a · score_a · conf_a) / Σ(w_a · conf_a)`
   over enabled directional agents (weight > 0, confidence > 0); `agreement`
   = share of weighted confidence agreeing with the sign of `s`.
2. **Rule judge** (always computed, used as fallback): hysteresis relative to
   the previous action — enter LONG when `s ≥ 0.20`, enter SHORT when
   `s ≤ −0.20`, go FLAT when `|s| < 0.08`, otherwise keep the previous action.
   Confidence `= clamp(|s| · (0.5 + 0.5 · agreement) · 1.6, 0, 1)`.
3. **Change detection** — the LLM judge is called for a symbol only when:
   no previous recommendation, any agent score moved ≥ `change_threshold`
   since the last *judged* recommendation, the rule action differs from the
   **previous rule action** (engine memory per symbol; after a restart
   derived from the newest recommendation's `rule_score`, and when that lies
   inside the hysteresis band compared with the last action instead — so an
   LLM that disagrees with the rules is not re-called every cycle), the
   position changed outside the loop, the last judgement is
   older than `judge_stale_hours`, or the params version changed in a way
   that affects decisions (weights, min_confidence, allow_shorts). Otherwise
   the previous recommendation is **carried** (`source="carried"`).
4. **LLM judge**: one batch call for all symbols that need it. Input per
   symbol: agent signals (score, confidence, summary, key details), risk
   assessment, current position, the rule suggestion, horizon "swing, days to
   weeks". Output strict JSON `[{symbol, action: LONG|SHORT|FLAT, confidence
   0..1, rationale ≤ 60 words}]`. Validation: unknown symbol/action → rule
   result for that symbol; confidence 0..1 as is, 1 < c ≤ 100 read as a
   percentage (c/100, so "3" = 3 %), negative / > 100 / non-finite / bool →
   invalid → rule result for that symbol. Vetoed directions are not
   allowed (LLM SHORT on a short-veto → FLAT). Temperature 0.2, max_tokens
   sized to the batch.
5. **Model mode**: `standard` → `OPENROUTER_MODEL`; `premium` →
   `OPENROUTER_ANALYSIS_PREMIUM_MODEL`; `premium_on_flip` → standard call
   first, then a second premium call only for symbols whose action flips
   against the previous action (LONG↔SHORT or entering/leaving FLAT from a
   position); the premium answer wins.
6. **Budget**: `fund.ai_spend` (UTC day) + estimated cost must stay ≤
   `ai_daily_budget_usd`; before each call check the spend; after each call add
   `usage.cost`. **Fails closed**: spend = max(stored spend, what this run
   booked); an unreadable stored spend (`store.ai_spend_today` → None) counts
   as exhausted. A call that was sent but failed without usage (timeout,
   network error — OpenRouter may bill it) books the pre-call estimate; a
   call that never left (no key) books nothing. Exhausted / no key / error →
   rule judge for those symbols,
   `source="rules"`, rationale starts with "Rule-based (AI budget reached)"
   or the matching reason. Same budget for sentiment + judge.

## 9. Sizing (`sizing.py`)

- Only act on a recommendation with confidence ≥ `min_confidence/100`; below
  that the target is 0 (FLAT) **for new positions**; an existing position is
  kept unless the action is FLAT or reversed with enough confidence. This
  check comes **before** the short permission, so a weak SHORT signal on a
  non-shortable symbol never closes a long.
- `target_weight = sign × max_position_pct/100 × confidence × vol_scaler ×
  risk.size_multiplier` with `vol_scaler = min(1, 0.015 / max(atr_pct, 1e-6))`
  (1.5 % daily ATR target). SHORT with `veto_short` → 0. SHORT with
  `block_short_increase` → an existing short is held or reduced
  (min of current and computed), no new short, a long is closed (0).
- Trade only if `|target − current_weight| ≥ 0.01` or the sign changes.
- `min_hold_days`: a position opened fewer NY trading days ago is not
  reduced/closed/reversed by the autopilot (kill switch, drawdown stop and
  margin calls ignore it).
- `max_trades_per_day`: count of autopilot orders (filled + pending) of the NY
  trading day; stop placing new ones when reached (recommendations still
  appear, status `skipped`, reason "daily trade limit"). A reversal needs
  two orders: with only one left today only the close is placed (target 0,
  recommendation `executed` with note "daily trade limit: only the close
  fits today").

### Crypto (2026-09-27)

- Crypto: `crypto_max_position_pct` instead of `max_position_pct`, vol
  target **3 %** daily ATR (`vol_scaler = min(1, 0.03/atr)`), and
  `min_hold_days` counts **UTC calendar days** (equities: NY trading days).
- `max_trades_per_day` is counted **per class** with the same limit value:
  equity autopilot orders per NY trading day (as above), crypto autopilot
  orders per **UTC day** (`store.orders_on_utc_day`) — a weekend crypto
  trade never uses the next NY trading day's equity budget.

## 10. Engine (`engine.py`)

- One asyncio task started from `main.py`; one cycle at a time (lock).
- Schedule (changed 2026-09-27, user decision): **scheduled full cycles run
  only during the US regular session**, next = last start +
  `interval_minutes` (`schedule_mode` "session"). **Outside the session
  there are no scheduled full cycles** (`schedule_mode` "manual_only"): only
  `POST /fund/run` ("Run now") runs a full cycle — crypto orders fill at
  once, equity orders are queued for the open as before; a scheduled cycle
  that comes due while closed is not run, the next one is at the next open
  (+5 s). The old "closed interval full cycle" is gone;
  `closed_interval_minutes` is now the risk-watch interval (below).
  Startup: first run 20 s after start (`trigger="startup"`) after the
  newsroom cache is loaded — a full cycle in the session, a light risk
  watch (quotes of the watchlist + positions, no agents/LLM) outside it.
  Missed runs are not caught up.
- **Crypto risk watch** (safety; my decision, explained here): with the
  schedule above, open crypto positions could go unwatched for a whole
  weekend. So when `crypto_risk_watch` is on (default), the US session is
  closed and crypto positions are open, `run_risk_watch()` runs every
  `closed_interval_minutes` (after the last full cycle / watch): quotes →
  accruals (funding, borrow, dividends) → queued AUTOPILOT crypto orders
  older than 2 × the risk-watch interval are cancelled ("stale —
  re-evaluated at the next run"; audit `engine`/`autopilot_orders_expired`,
  event `autopilot`) → pending protective and user (manual / manual-accept)
  orders are filled (crypto is tradable) → perp liquidation / margin check →
  drawdown stop (may liquidate — protective) → equity snapshot. **No agents, no LLM, no new orders, no
  recommendations**; recorded as a run with trigger `risk-watch`,
  `judge_mode` "none", cost 0, summary `risk_watch: true`. It does not
  update the engine's "positions after the previous cycle", so a
  liquidation counts as a position change outside the loop at the next
  full cycle.
- **Crypto rollout** at `start()` (§3), before the first run.
- **Stale crypto marks:** when a held crypto symbol has no live quote
  (`market.quote_is_stale`), the drawdown stop is skipped for that run
  (warning, run `partial`) and the margin check skips that symbol — never
  liquidate on a price that is not live. The kill switch still works.
- Cycle: read params (latest version) → market session → quotes/bars (cached)
  → daily accruals/dividends/funding (BEFORE fills, review 2026-09-27) →
  `broker.process_pending` (per symbol: crypto always, equities in the
  session after the grace) →
  mark to market, margin check → drawdown check (≥ stop → halt:
  autopilot off, `liquidate_all("drawdown-stop")`, event `halt`) → agents →
  judge → store recommendations → if autopilot and not halted: sizing →
  `broker.target(...)` per symbol → equity snapshot (+ SPY as benchmark) →
  events (`flip` when a symbol's action changes; `order` for autopilot fills) →
  finish run row (status, cost, judge mode, summary).
- Errors per symbol don't stop the cycle (`status="partial"`); a crash in the
  cycle marks the run `error` and the loop continues with the next schedule.
- Kill switch: `kill(reason)` → halt + liquidate all (`origin="kill-switch"`),
  pending when the market is closed. `resume()` clears the halt (autopilot stays off).
- Autopilot: `set_autopilot(enabled)`; refused while halted. Switching it
  off (user or heartbeat) also cancels the queued, unfilled `autopilot`
  orders (manual and protective pending orders stay); the audit row and the
  `autopilot` event say how many were cancelled.
- **Heartbeat (dead-man switch, guardrail)**: `engine.touch_client()` on
  every authenticated `GET /fund/state` and `GET /fund/events` (and when the
  user switches the autopilot on). When the autopilot is on and no client
  was seen for `autopilot_heartbeat_minutes`, the engine switches it off
  (audit `engine`/`autopilot_heartbeat_off`, event `autopilot` "Autopilot
  switched off: no app heartbeat for N min"). Checked at every cycle start
  and at least every 60 s by the loop. See §14.
- **Kill-switch ordering**: a `threading.Lock` (`_trade_lock`) serialises
  halt + liquidation with every non-protective placement (autopilot target,
  manual order, manual accept); these re-check the halt (and the autopilot
  flag) inside the lock — no non-protective order is placed after a kill
  switch. A failed liquidation keeps the halt and says so in the event.
- **Unreadable account state** (`broker.account` raises `StateUnavailable`
  or has no equity): the cycle skips the drawdown check, margin actions,
  autopilot orders (recommendations `skipped`, "account state unreadable")
  and the equity snapshot; run `partial` with the reason. Never liquidates
  on it.
- Every user action → `fund_audit` (actor "user"), written **first**; when
  the audit log is unavailable the action is refused (503 "audit log
  unavailable — the action was not carried out"). Exceptions: the kill
  switch and switching the autopilot off always work (error logged).
  Engine actions → actor "engine"; autopilot orders and their fills at the
  open → `autopilot_order` rows (`stage` placed|filled_at_open, order id,
  symbol, side, qty, recommendation id).

## 11. HTTP API (`api.py`, all token-protected)

| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/fund/state` | — | see below |
| GET | `/fund/params` | — | `{version, params, defaults, limits, created_at}` |
| PUT | `/fund/params` | partial params | `{version, params, effective_from}` (400 `{errors}`) |
| POST | `/fund/run` | — | `{queued: true}` |
| POST | `/fund/autopilot` | `{enabled: bool}` | `{autopilot}` (409 when halted) |
| POST | `/fund/kill` | `{reason?}` | `{halted: true, halt: {active, reason, at}, orders:[...]}` |
| POST | `/fund/resume` | — | `{halted:false}` |
| POST | `/fund/reset` | `{starting_capital}` | `{account}` |
| GET | `/fund/runs` | `?limit=50` | `[{id, started_at, finished_at, status, trigger, params_version, market_open, judge_mode, models, cost_usd, recommendations: n, orders: n, flips: [...]}]` |
| GET | `/fund/runs/{id}` | — | `{run, params, agents:[...], recommendations:[...], orders:[...]}` |
| GET | `/fund/orders` | `?limit=100` | `[order]` |
| POST | `/fund/orders` | `{symbol, side: buy|sell|short|cover, qty?|notional?}` | `OrderResult` (manual) |
| POST | `/fund/recommendations/{id}/execute` | — | `[OrderResult]` (manual-accept, uses sizing without min-hold) |
| GET | `/fund/equity` | `?since=iso` | `{points:[{ts, equity, cash, long_mv, short_mv, benchmark_equity, drawdown_pct}], starting_capital, reset_at}` |
| GET | `/fund/events` | `?after_id=0` | `[{id, ts, kind, symbol, title, body}]` (max 50) |
| GET | `/fund/audit` | `?limit=100` | `[...]` |
| POST | `/fund/backtest` | `{symbols?, start?, end?}` | backtest result (see §12) |
| GET | `/fund/backtests` | `?limit=10` | `[{id, created_at, request, summary}]` |
| GET | `/fund/export/orders.csv` | — | text/csv |
| GET | `/fund/export/decisions.csv` | — | text/csv (recommendations with agent scores) |

Both CSV exports end with a constant `note` column (paper-trading
disclaimer) on every row.

`GET /fund/state`:

```json
{
  "now": "...",
  "market": {"open": true, "session": "regular", "next_open": "...", "next_close": "...", "trading_day": "2026-09-28", "delayed_quotes": true},
  "engine": {"running": true, "cycle_in_progress": false, "next_run_at": "...", "interval_minutes": 5,
             "last_run": {"id": 12, "started_at": "...", "finished_at": "...", "status": "ok", "judge_mode": "mixed", "cost_usd": 0.0021, "error": null},
             "autopilot_heartbeat_minutes": 15, "autopilot_off_at": "... | null"},
  "last_client_seen": "... | null",
  "autopilot": false,
  "halted": {"active": false, "reason": null, "at": null},
  "params_version": 3,
  "pending_params": false,
  "ai_budget": {"limit_usd": 1.0, "spent_today_usd": 0.04, "exhausted": false, "spend_unreadable": false},
  "account": {"equity": 100512.3, "cash": 60210.0, "long_mv": 45100.0, "short_mv": 4797.7,
              "buying_power": 22000.0, "day_pnl": 120.5, "total_pnl": 512.3, "total_pnl_pct": 0.51,
              "peak_equity": 100600.0, "drawdown_pct": 0.09, "drawdown_stop_pct": 8.0,
              "short_exposure_pct": 4.8, "maintenance_required": 1439.3, "margin_excess": 53973.0,
              "starting_capital": 100000.0, "reset_at": "...", "unreadable": false},
  "positions": [{"symbol": "SPY", "qty": 60, "avg_price": 612.1, "last_price": 615.0, "market_value": 36900.0,
                 "weight": 0.367, "unrealized_pnl": 174.0, "realized_pnl": 0.0, "opened_at": "...",
                 "side": "long", "borrow_rate_pct": null}],
  "pending_orders": [...],
  "symbols": [{"symbol": "SPY", "quote": {"price": 615.0, "prev_close": 612.0, "change_pct": 0.49, "delayed": true, "ts": "..."},
               "agents": [{"agent": "technical", "score": 0.42, "confidence": 0.7, "summary": "...", "details": {...}, "source": "rules", "provenance": null},
                          {"agent": "macro", ...}, {"agent": "sentiment", "source": "llm", "provenance": {...}, ...},
                          {"agent": "risk", "score": 0, "confidence": 1, "summary": "...", "details": {"size_multiplier": 1.0, "veto": false, "veto_short": false, "reasons": []}}],
               "recommendation": {"id": 88, "action": "LONG", "confidence": 0.78, "target_weight": 0.039, "rule_score": 0.41,
                                  "rationale": "...", "source": "llm", "model": "deepseek/...", "provenance": {...},
                                  "usage": {"cost_usd": 0.0012, "prompt_tokens": 900, "completion_tokens": 120},
                                  "status": "open", "skip_reason": null, "ts": "...", "run_id": 12, "current_weight": 0.367}}]
}
```

`effective_from` = the next cycle start (`engine.next_run_at`); the UI shows
"applies from the next run (in m:ss)".

- `last_client_seen`: last heartbeat (§10); `engine.autopilot_off_at`: when
  the dead-man switch turns the autopilot off without a further poll (null
  while the autopilot is off).
- `account.unreadable = true`: every account field is null and
  `account.error` carries the reason (§10 unreadable state).
- `ai_budget.spend_unreadable = true`: today's spend could not be read, the
  budget counts as exhausted (§8.6).
- Status codes: 503 `{error}` for "audit log unavailable" (§10) and an
  unreadable paper account (`StateUnavailable`); 409 for a recommendation
  that is not the symbol's latest ("superseded by a newer recommendation")
  and for a second concurrent backtest ("a backtest is already running").

### Crypto contract additions (2026-09-27; WPF relies on these names)

- `GET/PUT /fund/params`: keys `crypto_fees_bps`, `crypto_max_position_pct`,
  `crypto_initial_margin_pct`, `crypto_maintenance_margin_pct`,
  `crypto_funding_rate_pct_8h`, `crypto_liquidation_fee_pct`,
  `crypto_risk_watch` with LIMITS entries (numeric: min/max/step/unit;
  `crypto_risk_watch`: bool); LIMITS may carry `label`/`description`.
- `GET /fund/state`:
  - `symbols[]`: `asset_class` ("crypto" | "equity"), `tradable_now` (bool;
    crypto always true, equities = US session open).
  - `engine`: `schedule_mode` ("session" | "manual_only"), `next_run_at`
    (iso | **null in manual_only**), `next_risk_watch_at` (iso | null — set
    only outside the session while `crypto_risk_watch` is on and crypto
    positions are open), plus `next_session_run_at` (iso: the next
    scheduled full cycle, i.e. the next open +5 s while closed),
    `crypto_risk_watch` (bool), `risk_watch_interval_minutes` (number);
    `interval_minutes` is always the session interval.
  - `positions[]`: `asset_class`, `instrument` ("spot" | "perp" | "stock"),
    fractional `qty` for crypto (`borrow_rate_pct` null for crypto).
  - `account`: `funding_total` (signed sum of funding since the last reset;
    + = received) and `crypto_margin_excess` (number | null).
- Orders / ledger: `qty` may be fractional (crypto); ledger kinds `funding`,
  `liquidation_fee`; runs may have trigger `risk-watch` (judge_mode "none").
- `POST /fund/orders`: crypto accepts a fractional `qty` (> 0, rounded down
  to 8 decimals by the broker; below $10 → a recorded rejection "size below
  minimum ($10 notional)"); a fractional `qty` for an equity → 400
  `{errors: {qty: "fractional quantities are only allowed for crypto …"}}`.
- `PUT /fund/params` `effective_from` = the earlier of the next full cycle
  and the next risk watch — the latter only when one will actually run
  (session closed, `crypto_risk_watch` on, crypto positions open; the same
  condition as `engine.next_risk_watch_at`).
- `POST /fund/orders`: `qty` / `notional` must be JSON numbers (a string or
  bool → 400 "must be a JSON number"); a fractional equity `qty` (also
  0.5) → 400 with the fractional message.

## 12. Backtest (`backtest.py`)

Rule-only: technical agent (on historical bars) + macro agent (monthly regime
history from `macro_derived` / stored observations — as of each date, no
look-ahead: a month's regime becomes known one month later) + risk sizing
(ATR). No sentiment, no LLM (non-deterministic and could know the outcome).
Daily steps at the close, fills next day's open, same fees and short
borrow cost as the paper broker (simplified: fees_bps and borrow at the
default rate, no dividends). Output:
`{symbols, start, end, portfolio: {cagr_pct, total_return_pct, max_drawdown_pct, sharpe, trades, hit_rate_pct, exposure_pct}, benchmark: {buy_and_hold per symbol + equal-weight}, per_symbol: [...], equity: [{date, equity, benchmark}] (weekly points), notes: [...]}`.
Default: watchlist, 5 years. Limits: at most 10 symbols and 20 years per
backtest (400 field errors); a default watchlist with more than 10 symbols
is cut to the first 10 (note in the result — with the 11-symbol default
watchlist SOL-USD is left out). One backtest at a time (409).

Crypto (2026-09-27): crypto symbols are allowed. The day calendar is the
union of all bar dates (crypto bars include weekends); an order for a
symbol without a bar that day waits for its next bar (equities over the
weekend) unless that symbol's next signal replaces it. Crypto uses
fractional quantities (8 decimals, legs below $10 dropped unless they close
the position), `crypto_fees_bps`, crypto sizing / ATR thresholds, no borrow
fee but perp funding on shorts (3 windows per calendar day at
`crypto_funding_rate_pct_8h`, positive = received), no liquidation model.
min_hold counts the symbol's own bars; Sharpe uses 365 periods when crypto
is included. `max_trades_per_day` is counted per asset class (crypto /
equity), as live. Bars fetched: calendar days + 30 for crypto.
`per_symbol[]` adds `funding` and `asset_class`.

## 13. WPF (HedgeFund window) — see §11 for data

- Header: watchlist chips (select symbol), status line
  ("Next run in 3:12 · last run 14:05 ok · AI today $0.04 / $1.00 ·
  quotes ~15 min delayed"), "▶ Run now", **"■ Kill switch"** (always
  visible, red, confirmation dialog).
- Agent cards (Technical / Macro / Sentiment / Risk) for the selected symbol
  with real score bar, confidence, summary, details tooltip; Sentiment card
  AI-labelled when `source` is llm/cached.
- Verdict: action + confidence ring + target size; AI badge
  `✦ AI-GENERATED · model · time` (or "Rule-based · no AI" for rules/fallback),
  rationale, caveat line, cost line; in manual mode "Execute" button
  (confirmation dialog) → `POST /fund/recommendations/{id}/execute`.
- Portfolio: equity, cash, day/total P&L, positions list (long/short, P&L,
  borrow rate), drawdown bar vs limit, margin excess, short exposure;
  equity curve canvas vs SPY benchmark.
- Execution: Manual (ticker, side buy/sell/short/cover, qty or $ → confirm)
  / Autonomous (toggle with confirmation; sliders min confidence, max
  position size, max trades/day, drawdown stop) + an "Engine settings"
  section: interval, closed-market interval, agent weights (3 sliders +
  risk on/off), judge model mode (Standard / Premium / Premium on flip),
  AI daily budget, allow shorts, short exposure cap, fees bps, watchlist
  editor. Changes are PUT (debounced ~600 ms), UI shows
  "Saved as v7 · applies from the next run (in 2:41)".
- History: list of runs (time, status, judge mode, cost, flips/orders),
  click → details popup (agents, recommendations, orders of that run).
- Backtest button (dialog: symbols, years) → result popup; Export buttons
  (orders.csv, decisions.csv → Save dialog).
- Windows toasts (Microsoft.Toolkit.Uwp.Notifications) for events `flip`,
  `order` (autopilot), `halt`, `margin_call`, polled via `/fund/events`
  every 15 s while the app runs; setting to mute.
- Polls `/fund/state` every 5 s while the window is visible.
- Keep all AI labelling + disclaimers ("Paper trading · simulated · not
  investment advice").

## 14. Backend lifetime

Default: the backend lives as long as the UFOS.ai app process (Job Object);
closing the HedgeFund window no longer stops it. Prepared option (settings
file `%LOCALAPPDATA%\UFOS.ai\hedgefund-backend.json`, `{"lifetime": "app" |
"detached"}`, UI checkbox "Keep the engine running after UFOS.ai closes
(experimental)"): in `detached` mode the backend is not bound to the Job
Object, its output goes to `backend/data/logs/process.log` via the run script
(`-LogFile`), and the app reuses the running backend on the next start (the
existing /version check + api-token file). `run_hedgefund_backend.ps1
-Headless` supports a later Windows Task Scheduler setup.

Detached mode does not use `CREATE_BREAKAWAY_FROM_JOB`: when UFOS.ai itself
runs inside a kill-on-close job (e.g. under a debugger), the "detached"
backend still dies with the app.

**Guardrail — autopilot heartbeat.** Because the backend may outlive the
window, "autopilot off after each start" and "the kill switch is always
visible" are protected by a dead-man switch (§10): without a client poll for
`autopilot_heartbeat_minutes` the autopilot switches itself off and its
queued orders are cancelled; positions are kept. A future truly headless
autopilot (Task Scheduler, no window) would need an **explicit opt-in**
replacing this guardrail — it is never implied by the detached setting.

## 15. Engine / API implementation notes (2026-09-26)

Decisions taken while wiring `engine.py` / `api.py` / `export.py`:

- Symbols analysed per cycle = watchlist + symbols still held (so the
  autopilot can close a position whose symbol was removed from the
  watchlist); `/fund/state` lists them in that order. SPY is fetched as
  quote-only benchmark when it is not on the watchlist.
- "Position changed outside the loop" = the positions at the start of a cycle
  differ from those after the previous cycle (manual orders, kill switch,
  reset); unknown for the first cycle after a start.
- `flip` event = the action differs from the symbol's previous
  recommendation (none for the very first one); run `summary.flips` =
  strings like `"SPY LONG→SHORT"`. `order` events: autopilot orders filled or
  pending, and pending autopilot orders filled at the open.
- Recommendation status with the autopilot on: `executed` (order placed),
  `skipped` + reason (sizing reason, "daily trade limit", "order already
  pending …", "no change needed", broker reject reason); autopilot off: `open`.
- `resume()` rebases `fund.peak_equity` to the current equity, otherwise a
  drawdown stop would fire again at the next cycle.
- While halted: `POST /fund/orders` accepts only sell/cover (409 for
  buy/short); executing a recommendation is refused (409).
- `POST /fund/reset` waits for a running cycle, switches the autopilot off
  and keeps an active halt (resume explicitly). Missing `starting_capital` →
  the params value.
- `POST /fund/kill` → `{halted: true, halt: {active, reason, at}, orders}`.
- Audit-first (§10): user actions write a `stage: "requested"` row before
  acting; manual orders / manual accepts add a `stage: "done"` row with the
  result. `params_update` rows carry `base_version` and the changes.
- LLM calls: the OpenRouter base URL re-read from `.env` goes through
  `config.safe_openrouter_base_url` (https only, else the default) in
  `newsroom.ai._current_settings()` and again in `fund.llm`; the
  AsyncOpenAI client is closed after every call.
- `POST /fund/recommendations/{id}/execute`: only the symbol's **latest**
  recommendation (409 "…superseded by a newer recommendation…" otherwise,
  re-checked inside the trade lock) with status `open`/`skipped`; nothing to
  do (e.g. below min confidence) → 409 with the reason. Risk limits come
  from the recommendation run's stored risk card (incl.
  `block_short_increase`). `save_recommendation` supersedes earlier `open`
  and `skipped` rows of the symbol.
- `GET /fund/equity` without `since` starts at the last reset;
  `benchmark_equity = starting_capital × SPY / SPY at the first snapshot
  after the reset` (null before it). Points also carry `benchmark` (SPY).
- Extra routes: `GET /fund/backtests/{id}` (full stored result).
  `POST /fund/backtest` returns the result plus its `id`; no price data →
  409. Backtests use a yfinance feed without the SQLite cache (the prices
  table is pruned to 2 years).
- Errors: 400 `{error, errors: {field: msg}}`, 404/409/503 `{error}`, 500
  generic message (details only in backend.log). All JSON responses are
  sanitised (NaN/Infinity → null, `allow_nan=False`).
- CSV exports: UTF-8, header row, RFC 4180 quoting, text cells starting with
  `= + - @` get a leading apostrophe (CSV injection guard); decisions carry
  `ai_generated` / `model` / `cost_usd` columns.

### Crypto rollout notes (2026-09-27)

- `/fund/state` keeps the symbol order watchlist + held symbols; crypto
  positions are listed like any other. `_housekeeping` fills pending
  orders per symbol in every full cycle (no longer only while the session
  is open); an autopilot crypto order filled from the queue gets the event
  "Queued autopilot order filled" (equities: "… at the open").
- The daily trade limit is counted per class (§9); the engine keeps two
  counters in `_apply_decisions`.
- Manual orders: crypto `qty` > 0 (fractional), equities whole shares ≥ 1
  (400 otherwise); `notional` works for both.
- User decisions of 2026-09-26: crypto for 24/7 trading (BTC/ETH/SOL),
  perp-style shorts, own crypto parameters, fractional quantities, the
  "only Run now outside the US session" rhythm; the risk watch is the
  engine's safety addition (§10).
- Residual simplifications: funding and borrow catch-up are booked at the
  current mark (the path of missed windows is unknown); perps are booked
  like short sales (same P&L, no separate collateral account); crypto
  quotes are Yahoo's near real-time data, not exchange-grade; the backtest
  has no perp liquidation.
