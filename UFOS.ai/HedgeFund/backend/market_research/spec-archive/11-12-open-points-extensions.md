# Market Research SPEC — archive: 11-12-open-points-extensions

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 11. Open points / verify

- Alpaca stream paths, news stream availability on the free plan, logos
  endpoint access on the free plan (MR6: Alpaca documents logos as a paid Broker API add-on → a
  401/403 is expected on the free plan and shown as "not on this plan"; crypto path `BTCUSD` and
  whether stream news carry `images` — verify live), assets endpoint ETF flag (verify at
  MR2/MR6 against the live API).
- Extended-hours end on early-close days (17:00 ET assumed).
- KLineChart exact 10.x version: pinned **10.0.3** at MR3a (2026-10-16); the vendored files are
  still to be fetched (`MarketResearch/Chart/tests/fetch_engine.py`, see MR3a notes).

## 12. Extensions of `strategy_platform` code (record here AND in its SPEC)

| stage | extension | status |
|---|---|---|
| MR1 | none (reuses `api/routes.dumps`, `read_body`, `user_error`, `core.errors`) | — |
| MR2 | `data/http_gate.py`: `research_priority()` context + research bucket/reserve + hard 60/60 s cap on sends, slot rule, `note_send()`, `ResearchYield` + `research_yield_check()`, `is_research_priority()`, `research_stats()`, `TokenBucket.try_acquire_above()` (§7.5, §13 MR2a notes + review fixes) | built 2026-10-16 (MR2a) |
| MR2 | `data/network.py`: `AdaptiveLimiter.try_acquire()` (non-blocking slot, review M3) | built 2026-10-16 (MR2a) |
| MR2 | `data/cache.py`: series lock via `_held(key)` — normal waiters counted, a research holder waiting in the gate yields (`ResearchYield`) and `get_bars` retries ≤ 50× (review M3b) | built 2026-10-16 (MR2a) |
| MR2 | `data/providers/alpaca.py`: `assets()` (trading API asset list, `load_trading_sdk`, kwarg `trading_sdk`); `harden_client(on_send=)` → gate `note_send` per HTTP request; `get_bars` timeframe `"30Min"` (Research only, not via cache/pit; `known_at` = start + 30 min); IEX/SIP 1Min via the existing `get_bars` | built 2026-10-16 (MR2a) |
| MR5 | none — indicators call the DSL evaluator (`strategy/evaluate.evaluate_node`, `strategy/panel.Panel`) unchanged; splits via the existing `AlpacaProvider.corporate_actions`; session VWAP + compare overlay are Research-only (`market_research/indicators.py`) | built 2026-10-17 (dependency noted in its SPEC §12) |
| MR6 | `data/providers/alpaca.py`: `images` in news dicts (`_news_images`, https only, ≤ 5), `logo(symbol)` (plain `requests` through the same gate, data type `logos`, constructor kwarg `http=None`), `LogosNotAvailable(DataSourceUnavailable)`, `load_requests()`, constants `LOGO_*` / `PNG_SIGNATURE` | built 2026-10-17 (MR6 notes) |
| MR7 | none — alerts use the hub (`trade_hook`, MR7 addition in `market_research`), `stats.quote.prev_close` and MR5 drawings read-only | built 2026-10-17 |
| MR8 | `backtest/runcard.py`: `trades_summary` keeps the fills (`fills_by_symbol` {SYM: [[ts, side, qty, fill_price]…]}, ≤ `CARD_FILLS_MAX` 10 000 per card, `fills_truncated`) — additive key of `trades_json` on NEW cards (finished cards stay immutable; older cards have none → Research says so) | built 2026-10-17 |

