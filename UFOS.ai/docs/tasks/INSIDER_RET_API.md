# INSIDER_RET_API — return after the trade for insider filings + 2-year backfill (Agent H, 2026-10-24, DRAFT)

Implements INSIDER_RETURNS_TASK.md (user decisions 2026-10-24 20:29–20:40 Berlin) and the "Clarity rules" in
CONGRESS_TASK.md. Backend: H, in `HedgeFund/backend/newsroom/`. UI: W.
The return engine is written source-neutral, so CONGRESS can reuse it unchanged (it gets its own contract).
Open questions: §11. No code is written until the lead accepts this contract.

## 1. Measured unit ("trade")
- **One trade = one Form 4 accession × one open-market code**: `P` (purchase) or `S` (sale), non-derivative table only.
  - The rows of that code in the filing are aggregated: shares = sum, value = Σ shares × price, avg price =
    value / shares.
  - `trade_date_first` and `trade_date_last` are the earliest and latest transaction dates of those rows.
  - A filing with both P and S rows gives two trades. Every other code (A, M, F, G, …), derivative rows and
    derivative-only filings are not stored.
- **Amendments (4/A):** stored and shown with an "amendment" badge, but **not measured**, so the same trades are not
  counted twice. Only original Form 4s enter the returns and the statistics.
- **Ticker:** `issuerTradingSymbol` as filed, upper-cased.
  - Missing or unusable ticker → stored, `measurable = false`, reason "no ticker".
  - Alpaca has no bars for the ticker → "no price data for <T> (renamed/delisted?)". Never guessed.
- **Owner:** the first reporting owner, as the live collector does. Joint filings carry `co_reporters: n`.
- **Role class:** one value from CEO · CFO · Director · 10% owner · Other, in this precedence:
  - CEO = officer title matching `chief executive|\bceo\b|principal executive`;
  - CFO = officer title matching `chief financial|\bcfo\b|principal financial`;
  - Director = isDirector without a CEO/CFO title;
  - 10% owner = isTenPercentOwner only;
  - otherwise Other.
  - The raw roles and title are kept for the details view.
- **10b5-1:** the live collector's rule: the `aff10b5One` checkbox, else a footnote mentioning 10b5-1 that is not
  negated.
  - Backfill uses the data sets' `AFF10B5ONE` and FOOTNOTES table with the same regexes.
  - `plan_source` = checkbox | footnote | null.
- **Cluster:** the existing `insider_summary.find_clusters` rule: the same issuer, ≥ 2 different insiders buying or
  ≥ 3 selling (unplanned only), within 14 days by trade date.
  - It is computed over the stored 2 years. `cluster = true` for every member trade.
  - The Track record splits "single vs cluster".

## 2. Two start points (user decision)
- **From filing (main, tradable):** the entry is the first tradable price after the EDGAR acceptance time.
  - Accepted during the regular session (09:30–16:00 ET, 13:00 on early-close days, via
    `strategy_platform.core.calendars`): entry = the **open of the first 1-minute IEX bar that starts strictly
    after the acceptance minute**, same session. `entry_basis = "minute"`.
  - No IEX trade after acceptance before the close (illiquid stock) → the next session's open,
    `entry_basis = "next_open_no_trade"`, stated in the details.
  - Accepted outside the session (before 09:30, after the close, weekend or holiday) → the **open of the next
    regular session** (daily bar). `entry_basis = "next_open"`.
  - Acceptance time: live from the Atom feed `<updated>` (acceptance time with offset). In the backfill, see §5.
- **From the insider's trade date (small, "not tradable — insider's own timing"):** entry = the **close of
  `trade_date_first`** (daily bar). If that day was not a trading day → the close of the previous trading day,
  stated as `entry_basis = "prev_close_non_trading_day"`.
  - The insider's reported price is shown in the details but not used for returns (it is unadjusted).
- **Horizon h ∈ {1, 5, 20, 60} trading days:** exit = the close of the h-th regular session after the entry
  session E (E = the session the entry price comes from).
  - An open entry therefore also includes the entry session itself (stated in the UI tooltip).
  - A horizon is "running" until session E+h has closed: `sessions_done` = n, shown as "running (n of 20 days)".
- **Prices:** Alpaca IEX bars with `adjustment="all"` (splits and dividends, so the returns are total-return-like —
  stated in the UI).
  - Daily bars come through `strategy_platform.data.cache.get_bars(…, "1Day", adjustment="all")`, i.e. the shared
    bar cache in hedgefund.db and the HTTP gate.
  - The minute bars for an entry are a single gate call for the window [acceptance, session close], **not** written
    to the bar cache: only the derived entry price is stored, otherwise the cache would grow by about 10 M rows.
- **Benchmark:** the sector ETF of the issuer (§4), measured over exactly the same window: the same entry minute,
  open or close rule, and the same exit close.
  - Return r = exit / entry − 1.
  - Excess = r_stock − r_etf, a simple difference as the user asked.
  - **score** = excess for P, −excess for S ("did the price fall afterwards?").
  - The cards show the real excess (with its sign); the Track record uses the score.
- **No sector ETF** (unmapped SIC): r_stock is shown, while excess and score are null with the reason
  "no sector ETF for SIC nnnn". There is no SPY substitute.

## 3. Storage (newsroom.db, migration v-next) and retention
New tables. `insider_filings`, the full event payload for the live 5-day list, stays unchanged.
- **`disclosure_trades`** — the core fields only, so CONGRESS can share it via `source`:
  - id TEXT PK: `insider:<accession>:<P|S>`;
  - source 'insider', accession, form ('4' | '4/A');
  - accepted_at (UTC ISO, or null), filing_date (date);
  - issuer_cik, ticker, issuer_name;
  - owner_cik, owner_name, role_class, roles_raw, co_reporters;
  - code (P|S), shares, value_usd, avg_price;
  - trade_date_first, trade_date_last;
  - plan_10b5_1, plan_source, cluster;
  - measurable, not_measurable_reason;
  - official_url (the filing index page on sec.gov);
  - origin ('live' | 'backfill'), first_seen.
- **`disclosure_returns`:**
  - key (trade_id, start 'filing' | 'trade_date', horizon 1|5|20|60);
  - entry_ts, entry_basis, entry_price, etf, etf_entry_price, sessions_done;
  - exit_date, exit_price, etf_exit_price, r_stock, r_etf, excess, score;
  - state 'complete' | 'running' | 'not_measurable', reason;
  - computed_at.
- **`issuer_sic`:** cik PK, sic, sic_description, tickers_json, fetched_at. Refreshed when older than 90 days.
- **`disclosure_jobs`:** key PK (e.g. `backfill:dataset:2025q2`, `backfill:gap:2026-10-06`, `returns:daily`),
  state, progress_json, last_error, updated_at. Used for resume, progress display and recorded gaps.
- **Retention (user: 2 years):** `disclosure_trades` / `disclosure_returns` with filing_date older than 730 days are
  removed in the existing daily `prune()`.
  - `insider_filings` stays at `insider_retention_days` (default 5); the cards are now served from
    `disclosure_trades`.
  - The rule goes to conventions/SPEC via PENDING_DOCS_H.md (doc freeze).
- **Volume:** about 200,000 trades over 2 years (§5): roughly 80 MB in `disclosure_trades` and 120 MB in
  `disclosure_returns` (8 rows per trade).
  - Plus daily bars in hedgefund.db: about 5,000 symbols × 500 bars ≈ 2.5 M rows (~250 MB) via the shared bar cache.
  - Please confirm that size (§11 Q5).

## 4. SIC → sector ETF table
- **File:** `HedgeFund/backend/newsroom/sector_etf_map.json`:
  `{"schema": 1, "source": "Agent H mapping of SEC SIC codes to SPDR Select Sector ETFs (GICS sectors), 2026-10-24 — an approximation: SIC predates GICS", "rules": [{"sic_from": 2833, "sic_to": 2836, "etf": "XLV", "label": "Pharmaceuticals"}, …]}`.
- **Lookup:** the first matching rule. The table puts specific ranges before their 2-digit group.
- **ETFs:** XLK XLF XLV XLE XLI XLY XLP XLU XLB XLRE XLC (all have history for the full 2-year window).
- **Core of the table (full list in the file):**

| SIC | ETF | SIC | ETF |
|---|---|---|---|
| 0100–0999 agriculture (0800–0899 forestry → XLB) | XLP | 4000–4799 transport (4600–4699 pipelines → XLE) | XLI |
| 1000–1099, 1400–1499 metal/non-metal mining | XLB | 4800–4899 communications | XLC |
| 1200–1399 coal, oil & gas extraction | XLE | 4900–4999 utilities (4922–4923 gas transmission → XLE; 4950–4959 waste → XLI) | XLU |
| 1500–1799 construction (1531 homebuilders → XLY) | XLI | 5000–5199 wholesale (5122 drugs → XLV; 5140–5149 groceries → XLP) | XLI |
| 2000–2199 food, tobacco | XLP | 5200–5999 retail (5400–5499 food stores, 5912 drug stores → XLP) | XLY |
| 2200–2399, 2500–2599, 3100–3199 textiles, apparel, furniture, leather | XLY | 6000–6499, 6700–6799 banks, brokers, insurance, holding (6798 REITs → XLRE; 6770 blank checks → none) | XLF |
| 2400–2499, 2600–2699, 2800–2899, 3000–3099, 3200–3399 materials (2833–2836 pharma → XLV; 2840–2844 cosmetics → XLP) | XLB | 6500–6599 real estate | XLRE |
| 2700–2799 publishing | XLC | 7000–7099, 7200–7299, 7900–7999, 8200–8299 hotels, personal services, leisure, education | XLY |
| 2900–2999 refining | XLE | 7370–7379 software & IT services | XLK |
| 3400–3499, 3500–3599 metal products, machinery (3570–3579 computers → XLK) | XLI | 7310–7319 advertising, 7810–7829 film | XLC |
| 3600–3699 electronics (3630–3639 appliances → XLY) | XLK | 7300–7399 other business services | XLI |
| 3700–3799 transport equipment (3711, 3713–3714 vehicles/parts → XLY) | XLI | 8000–8099 health services, 8731 biological research | XLV |
| 3800–3899 instruments (3812 defense → XLI; 3841–3851 medical → XLV; 3873 watches → XLY) | XLK | 8700–8799 engineering & management services | XLI |
| 3900–3999 misc. manufacturing | XLY | 9995, 9999, missing | none |

- **SIC source:** `https://data.sec.gov/submissions/CIK##########.json` → `sic`, `sicDescription`, `tickers` (one
  request per issuer, cached in `issuer_sic`, via the shared SEC limiter of §6).

## 5. 2-year backfill (user: yes) — sources, volume, time
**Sources:**
1. **SEC "Insider Transactions Data Sets"** (DERA, quarterly `YYYYqN_form345.zip`, linked from
   https://www.sec.gov/dera/data/form-345; the exact download URL is taken from that page's links at implementation
   time). Checked 2026-10-24: available through **2026 Q3**, 7.6–13.2 MB per quarter.
   - They are the flattened as-filed XML of Forms 3, 4 and 5: SUBMISSION (accession, FILING_DATE, DOCUMENT_TYPE,
     issuer CIK/name/symbol, AFF10B5ONE), REPORTINGOWNER (relationship, title), NONDERIV_TRANS (code, date,
     shares, price, A/D), FOOTNOTES.
   - The 8 quarters **2024 Q4 – 2026 Q3** ≈ 80 MB, i.e. 8 requests in total, instead of about 450,000 single-filing
     requests.
   - Only Form 4 and 4/A rows with code P/S are kept.
2. **Acceptance time:** the data sets have only FILING_DATE (a date). Acceptance date-times come from the issuer's
   submissions JSON (`filings.recent.acceptanceDateTime` by accessionNumber, plus its older `files[]` pages for the
   first year). That is the same request that gives the SIC.
   - An accession not found there → `accepted_at = null` → the filing start is `not_measurable`, reason
     "acceptance time unknown" (no date-only substitute). The trade-date start is still measured.
   - Before the full run, a pilot of 50 accessions checks that issuer submissions list the Form 4s. If they do not,
     the run stops and asks: the alternative is one `…-index-headers.html` request per filing, about 200,000
     requests (§11 Q2).
3. **Gap after the last data set (2026-10-01 → the day the live store starts):** EDGAR daily index `form.YYYYMMDD.idx`
   (one per day), then one complete-submission `.txt` per Form 4. The `.txt` header carries ACCEPTANCE-DATETIME
   and the XML is parsed with the live collector's `_parse_form4`.
   - Later data sets (2026 Q4 from about January) are not needed then. A quarter that was gap-filled is marked done.

**Volume and time estimate** (SEC limiter default `sec_edgar_min_request_interval_seconds` = 0.5 s, i.e. 2 req/s;
the second figure is at 0.2 s = 5 req/s):

| Step | Requests | Transfer | Time |
|---|---|---|---|
| Data sets 2024 Q4–2026 Q3 | 8 | ~80 MB | < 5 min |
| Issuer submissions JSON (~5,000 issuers with P/S, + ~1,000 older pages) | ~6,000 | ~0.5–1 GB | ~50 min / ~20 min |
| Gap 2026-10-01 → live start (~17 trading days × ~850 Form 4) | ~14,500 | ~120 MB | ~2 h / ~50 min |
| Alpaca daily bars, 2 years (~5,000 tickers + 11 ETFs, 1 page each) | ~5,000 | small | ~35 min at 150/min, ~85 min with research priority |
| Alpaca minute entries (in-session acceptances ≈ half of ~200,000 trades, deduplicated per symbol+day) | ~80,000–100,000 | moderate | ~10–11 h at 150/min, ~25 h with research priority (§11 Q3) |
| Returns computation | — | — | minutes (CPU) |

- **Trade counts are estimates:** about 230,000 Form 4s a year, of which roughly 40 % carry P or S. The pilot step
  logs real counts after the first data set, and the estimate in the status endpoint is updated.

**Run rules:**
- Started by the user: a button in the Disclosures status line, "Load 2-year history (~N h in the background)". No
  automatic start (§11 Q1).
- Resumable per job key; pause and resume are possible.
- Gaps are recorded per day or quarter with their reason and shown in the status endpoint. A failed day is retried
  on the next run and never skipped silently.
- **EDGAR part:** any time, through the same process-wide SEC limiter as the live collector. While the backfill's
  EDGAR steps run, the limiter interval is 0.2 s (5 req/s, decision §11.4); afterwards it returns to the configured
  value. The sum of live + backfill never exceeds that rate (< 10 req/s), with the declared User-Agent from the Credential Manager (missing → the job
  waits with the same message as the collector).
- **Alpaca part:** only outside 09:00–16:30 ET on trading days, inside `http_gate.research_priority()`, so it always
  yields to trading and council calls. Batches of 500 symbols, progress stored after each batch.
- No AI calls at any point.

## 6. Daily job and live path
- **Live:** every new Form 4 from the existing collector with P/S rows is also written to `disclosure_trades`
  (origin 'live', `accepted_at` = feed `<updated>`). This is idempotent by id.
- **Daily job:** once per trading day at 16:45 ET plus a 15-minute margin for final IEX bars, and once at start if
  the last run was missed. Never per UI refresh.
  1. SIC for new issuers.
  2. Entry prices for new trades (minute or daily).
  3. Daily-bar tail for symbols with running horizons: about 2,000–3,000 symbols, ~1 request each through the bar
     cache, ~20 min at 150/min.
  4. Recompute the running and completed returns.
  5. Clusters for new trades.
  - Progress and errors go in `disclosure_jobs`. A failed step is retried at the next run and shown in the status.

## 7. HTTP API (HedgeFund backend, token-protected, read-only except §7.5)
Base `/newsroom/disclosures`. All times are UTC ISO with `Z`; percent values are floats (0.021 = +2.1 %).

**7.1 `GET /newsroom/disclosures/insider`** — the list (Insider tab).
- **Query:**
  - `days` = 1–730, default 30;
  - `code` = `P,S` (default) | `P` | `S`;
  - `role` = CEO|CFO|Director|10pct|Other (comma list);
  - `plan` = all|planned|unplanned;
  - `cluster` = all|cluster|single;
  - `ticker`;
  - `measurable` = all (default) | yes;
  - `amendments` = hide (default) | show;
  - `limit` = 1–500, default 100; `offset`.
  - Invalid values → 400 with the reason (never clamped).
- **Default filters** = the Clarity rule: open-market P/S with a ticker, last 30 days.
- **Response:** `{"total_all": 1200, "total_matching": 42, "shown": 42, "filters": {…echo…}, "rows": [row…],
  "source": {"backfill": "complete|running|not started", "last_daily_job": "…Z", "stale": false}}`.
  - It carries both counts for "42 of 1,200 shown · change filters".
- **row** (compact):
  - `{"id", "ticker", "issuer_name", "owner_name", "role_class", "code", "value_usd", "shares", "trade_date_first",
    "accepted_at", "reporting_delay_days", "plan_10b5_1", "cluster", "form", "chip": chip}`.
  - `reporting_delay_days` = accepted_at date − trade_date_first, in calendar days.
- **chip** (ONE return chip; the server picks it):
  - `{"start": "filing", "horizon": 20, "state": "complete"|"running"|"not_measurable", "sessions_done": 12,
    "excess": 0.021, "r_stock": 0.032, "etf": "XLK", "reason": null}`.
  - Rule: the longest **complete** horizon among 1, 5, 20, 60. If none is complete, the 1-day horizon as running.
  - W formats it, e.g. "+2.1 % vs XLK · 20 d", "running (12 of 20 d)", "not measurable — no sector ETF for SIC 6770".

**7.2 `GET /newsroom/disclosures/insider/{id}`** — the details on expand:
- the row fields, plus `roles_raw`, `co_reporters`, `avg_price` (as reported), `trade_date_last`, `plan_source`,
  `official_url` (opens in the default browser, http/https only), `sic`, `sic_description`, `etf`;
- `returns`: `{"filing": {"entry_ts", "entry_basis", "entry_price", "etf_entry_price", "horizons": [{"h": 1,
  "state", "sessions_done", "exit_date", "r_stock", "r_etf", "excess", "score"}, …]}, "trade_date": {…same…}}`;
- `notes`: plain sentences, e.g. "prices adjusted for splits and dividends (IEX feed)", "sale: score = −excess".

**7.3 `GET /newsroom/disclosures/track-record?source=insider&horizon=20&start=filing&group=direction|plan|role|cluster&days=730`**
- **Response:** `{"caveat": "Descriptive statistics, not a validated signal — small samples are noise. Sector benchmark: approximation (SIC → GICS-based sector ETFs).", "horizon",
  "start", "groups": [{"group": "P", "label": "Purchases", "n": 812, "n_running": 40, "n_not_measurable": 33,
  "mean_score", "median_score", "hit_rate", "small_sample": false}, …]}`.
- `small_sample` = n < 30, shown greyed. No p-values or confidence stars. n is always shown.
- `group=direction` lists P and S separately, with score signs as defined in §2.

**7.4 `GET /newsroom/disclosures/markers?ticker=X&from=YYYY-MM-DD&to=YYYY-MM-DD&kinds=insider`** — for the Research
chart MARKERS control:
- `[{"date", "kind": "insider", "side": "buy"|"sell", "count": 3, "ids": [...], "value_usd"}]`;
- same-day trades of one side are merged with a count; dated by `accepted_at` (filing day), not the trade date
  (§11 Q6).

**7.5 `GET /newsroom/disclosures/status`** and `POST /newsroom/disclosures/backfill` `{"action": "start"|"pause"|"resume"}`
- **status:** `{"backfill": {"state": "not_started|running|paused|complete|failed", "steps": [{"key", "state",
  "done", "total", "eta_seconds"}], "gaps": [{"key", "reason"}], "estimate": {"requests_sec", "requests_alpaca",
  "hours"}}, "daily_job": {"last_run", "next_run", "state", "last_error"}, "counts": {"trades", "measurable",
  "complete_20d"}, "sic_unmapped": [{"sic", "n"}], "storage": {"newsroom_db_bytes", "hedgefund_db_bytes"},
  "sec_interval_seconds": 0.2}` (0.2 while the backfill's EDGAR steps run, else the configured value).
- **POST** returns the new status. 409 if the action does not fit the state, with a readable reason.

## 8. UI data needs (W) — from the Clarity rules
- **Disclosures section:** ONE Newsroom section "Disclosures" replaces "Insider Trades", with tabs Insider · Congress
  · Track record.
  - Shared card layout, filter bar and return chip.
  - The Insider tab uses 7.1/7.2, the Track record tab 7.3 (statistics only there, never on cards), the status line
    7.5.
- **Compact rows:** who, ticker, buy/sell, amount, reporting delay, the chip. Details only on expand: both start
  points, all horizons, 10b5-1 and its source, official document link.
- **Text for the trade-date start:** "from trade date — not tradable (insider's own timing)".
- **Colours:** a fixed insider colour (W's design-system choice); violet stays reserved for AI. Nothing here is AI
  output.
- **Chart markers:** off by default; an Insider toggle in MARKERS, using 7.4.
- **Alerts:** "watched member files / held ticker appears" are part of the CONGRESS package (Monitoring → Alerts
  only, no toast). Whether insiders get the "held ticker appears" alert as well is §11 Q7.

## 9. Not in scope
- No strategy or DSL signal.
- No AI.
- No data source other than SEC and Alpaca.
- No change to the existing 5-day insider list or the insider AI summary (they keep reading `insider_filings`).

## 10. Tests (offline, fixtures)
- Aggregation per accession × code, amendment handling, role class, the 10b5-1 rules, clusters.
- The entry rules: in-session minute / no trade / outside session / holiday / early close; horizon and running
  counts; ETF window equality; excess and score signs; unmapped SIC.
- Data-set TSV parsing (tiny fixture zips), acceptance-time join, gap-day parsing of a `.txt` fixture.
- Job resume and gaps; the shared SEC limiter (live + backfill never exceed the rate); the Alpaca part refused inside
  market hours.
- API filters, 400s, counts and the chip rule; retention at 730 days; `sector_etf_map.json` schema with no
  overlapping rules.

## 11. Decisions (user + lead, 2026-10-24 21:05 — contract accepted)
1. **Backfill start:** only from a user button "Start backfill" in Disclosures, with progress and pause/resume (user).
2. **Acceptance-time pilot:** if Form 4s are missing from the issuer submissions → the run **stops and asks** (lead).
3. **Minute entries:** option (a) — all of them, in the background outside US market hours, always yielding to trading
   calls (`research_priority`), over about 1–2 days (user).
4. **SEC rate:** **0.2 s per request while the backfill runs** (5 req/s, < 10 req/s fair access), one shared limiter
   for live + backfill; back to the configured default (`sec_edgar_min_request_interval_seconds`, 0.5 s) afterwards
   (lead).
5. **Storage:** about 200 MB + 250 MB is acceptable (lead). The backfill status shows the DB sizes (newsroom.db,
   hedgefund.db) → `status.storage: {"newsroom_db_bytes", "hedgefund_db_bytes"}`.
6. **Chart markers:** dated by the **filing (acceptance) day** (user).
7. **"Held ticker appears" alert:** for insiders AND congress, Monitoring → Alerts only, no toast (user). It is
   implemented with the CONGRESS package (shared alert path).
8. **SIC mapping:** accepted as documented. It is kept in ONE editable file (`newsroom/sector_etf_map.json`) with its
   source and date. The Track-record caveat adds: "Sector benchmark: approximation (SIC → GICS-based sector
   ETFs)." (lead).

Implementation slices (lead, cloud move ahead), each commit-ready with green tests: (a) storage + return engine,
(b) live collection, (c) backfill.
