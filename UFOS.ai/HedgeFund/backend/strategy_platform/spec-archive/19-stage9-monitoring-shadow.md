# Strategy Platform SPEC — archive: 19-stage9-monitoring-shadow

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

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
