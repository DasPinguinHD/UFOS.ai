# Strategy Platform SPEC — archive: 12-market-research-consumer

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

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
