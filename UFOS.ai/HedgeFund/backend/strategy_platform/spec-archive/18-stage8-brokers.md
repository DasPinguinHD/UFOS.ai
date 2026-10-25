# Strategy Platform SPEC — archive: 18-stage8-brokers

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

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
