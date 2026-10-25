# Strategy Platform SPEC — archive: 20-stage9b-insights

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

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
