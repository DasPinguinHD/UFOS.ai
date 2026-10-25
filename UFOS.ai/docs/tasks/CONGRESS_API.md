# CONGRESS_API — congress trades (STOCK Act) in the HedgeFund Newsroom (Agent H, 2026-10-24, DRAFT)

Implements CONGRESS_TASK.md (user decisions 2026-10-24 20:31–20:40 Berlin). It builds on INSIDER_RET_API.md (accepted
21:05): the same tables, return engine, Disclosures API and Clarity rules. Backend: H (`newsroom/`). UI: W.
Open questions: §10. Code follows after INSIDER-RET slices (a)–(c) and the lead's go on this contract.

## 1. Source (user choice) — checked 2026-10-24
- **Repository:** `kadoa-org/congress-trading-monitor` (MIT code). Static JSON under
  `https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/`, refreshed daily by
  kadoa-bot ("Daily refresh: YYYY-MM-DD").
- **`filers.json`:** a JSON array of `{id, full_name, branch, chamber, party, state, agency, level, office, photo_url,
  trade_count, purchases, sales, late_filings, est_volume}`. Example id: `house_josh_gottheimer`; the executive
  branch, e.g. `oge_…`, has chamber null.
- **`filer/<id>.json`:** `{"filer": {id, full_name, branch, chamber, party, state, agency, level, office, photo_url},
  "trades": [row…]}`. It holds the full history of that filer.
- **`trades.json`:** the newest about 5,000 rows (~4.3 MB). Each row also carries `filer_name, chamber, party, state,
  office` (CONGRESS_TASK.md).
- **Row (verbatim fields, example 2026-10-07):**
  - `id` ("house_20035590_g1"), `filing_id`, `filer_id`, `source_id` (house_clerk | senate_efd | oge_executive);
  - `transaction_date`, `notification_date`, `filing_date` (dates only);
  - `owner` (e.g. "JT", "SP", "DC", null = self), `ticker`, `asset_name`, `asset_type` (e.g. "ST" stock, "OP"
    option);
  - `transaction_type` ("Purchase", "Sale (Partial)", "Sale (Full)", …);
  - `amount_range_low/high/label` ("$1,001 - $15,000"), `comment`, `is_late` (0|1), `days_to_file`;
  - `doc_url` (the official House/Senate PDF or page), `filing_type` ("PTR");
  - plus kadoa's own `ret_since`, `excess_since`, `ret_30d`, `ret_1y` — **ignored**; we compute our own (user).
- **Scope (user):** `source_id ∈ {house_clerk, senate_efd}`. `oge_executive` is dropped at import.
- **Terms:** the code is MIT. The underlying filings are public records under the Ethics in Government Act, with its
  use restrictions (no commercial use, no credit rating, no solicitation). Personal research only: no
  redistribution, no public signal feed. This is shown once in the tab's info line.
- **Dependency risk:**
  - The newest `ingested_at` / the repo's daily refresh date is stored. If no new refresh arrives for more than 3 days,
    the tab says "source not updated since <date>" (Caution) and the status endpoint says so.
  - There is no fallback to other sources.
  - Every row keeps `doc_url`, so it can be traced to the official filing.

## 2. Mapping to `disclosure_trades` (shared table, `source = 'congress'`)
- **Identity:** id = `congress:<row id>`; dedupe and amended re-filings by the row id (the latest content wins,
  `first_seen` is kept).
- **Shared columns:**
  - `filing_date` ← filing_date; `accepted_at` = null (the sources give no time of day);
  - `ticker` (upper-cased, may be null); `issuer_name` ← asset_name;
  - `owner_name` ← filer full name; `owner_cik` ← filer_id;
  - `code` ← "P" for Purchase, "S" for "Sale (Partial)" / "Sale (Full)". Any other type ("Exchange", …) is stored
    with `code = "X"` and is not measurable ("not measurable (transaction type)");
  - `trade_date_first` = `trade_date_last` ← transaction_date; `official_url` ← doc_url (http/https only).
- **Congress columns (new, nullable for insiders):**
  - chamber (house|senate), party, state, office;
  - owner_type (self|spouse|joint|child|other from `owner`);
  - amount_low, amount_high, amount_label;
  - days_to_file, is_late (the source flag, > 45 days);
  - asset_type, filing_type, source_ingested_at.
- **Measurable (user):** a ticker plus an equity/ETF asset type. The accepted list is in one constant (House codes
  "ST" stock, "EF" ETF, plus the Senate equivalents found in the pilot, §10 Q2). Others are stored with
  "not measurable (asset type <x>)". Options ("OP") are not measured.
- **Insider-only fields** (shares, value_usd, plan_10b5_1, cluster, role_class) stay null for congress rows.

## 3. Returns — the same engine as INSIDER-RET (§2 there), with one difference
- **From filing (main):** the sources give only a filing **date**, no time. Entry = **the open of the first regular
  session after `filing_date`** (`entry_basis = "next_open_after_filing_date"`). This is conservative, because the
  publication time on that day is unknown (§10 Q1). No minute bars are needed.
- **From transaction date (small, "not tradable — member's own timing"):** the close of `transaction_date` (previous
  trading day if it was not one), as for insiders.
- **Everything else is shared:** horizons 1/5/20/60 sessions, the sector ETF via the issuer SIC (ticker → CIK via
  SEC `company_tickers.json`, one request per day, cached), excess = r − r_etf, score = −excess for sales, the
  running / complete / not-measurable states.

## 4. Collection
- **Initial history (2 years, same as insiders):**
  1. `filers.json`, keeping filers with chamber house/senate (~369).
  2. `filer/<id>.json` for each of them.
  3. Keep rows with `source_id` in scope and `filing_date ≥ today − 730 days`.
  - About 370 requests, about 50–100 MB, at 1 request per second (polite; GitHub raw CDN), so ~6–7 minutes.
  - Started with the same "Start backfill" button (Congress tab or the shared Disclosures status), resumable per
    filer, gaps recorded.
- **Every 6 h (conditional pull, decision §10.4; + at start):**
  1. `trades.json` with `If-None-Match`/ETag (304 = nothing new).
  2. New or changed rows are upserted.
  3. `filers.json` (ETag); filers whose `trade_count` changed and whose new rows are not all in `trades.json` →
     re-pull `filer/<id>.json`.
  4. The return job (shared daily job, INSIDER_RET §6).
- **Errors:** an HTTP error, invalid JSON or an unexpected shape (missing required fields) → the job fails visibly
  ("congress source: <reason>"), keeps the old rows and retries at the next run. A row without `id` / `filing_date`
  is skipped and counted in the status ("n rows rejected: <reason>").
- **Retention:** 2 years by filing_date (shared prune).
- **Volume:** about 2 years × ~6,000–8,000 House+Senate rows a year → ~15,000 rows (a few MB). Daily bars for the
  ~1,500 distinct tickers mostly come from the shared bar cache already.

## 5. Watchlist alerts (user: Monitoring → Alerts only, NO toast) — covers insiders AND congress (decision 21:05)
- **Watched members:** a user list (filer ids) in the new table `disclosure_watch`, with kind 'member' and key
  filer_id. API in §6.
  - A new congress row of a watched member → alert kind `disclosure_watched_member`.
- **Held ticker appears:** a new insider OR congress trade whose ticker is held in a platform paper account or the
  Shadow Account (current positions from the platform portfolio store) → alert kind `disclosure_held_ticker`.
- Both alerts go through `strategy_platform.monitoring.alerts.emit` with two new KINDS entries (`toast: False`,
  severity info).
  - dedupe_key = `disclosure:<kind>:<trade id>`.
  - Plain text: "Congress: <name> (<chamber>, <party>-<state>) sold <ticker> ($1,001–$15,000), traded 2026-09-25,
    filed 2026-10-07".
- **Backfill never alerts:** only rows first seen by the daily/live path after the backfill finished.

## 6. HTTP API (token-protected; shapes like INSIDER_RET §7)
- **`GET /newsroom/disclosures/congress`** — query: `days`, `code` (P,S), `chamber`, `party` (D|R|I), `late`
  (all|late|on_time), `ticker`, `member` (filer id), `measurable`, `limit`, `offset`.
  - Response as for insiders: `total_all`, `total_matching`, `shown`, rows with the same `chip`.
  - Row extra fields: `chamber`, `party`, `state`, `owner_type`, `amount_label`, `days_to_file`, `is_late`,
    `member_id`, `watched` (bool). `source.updated_through` = the source's refresh date, `source.stale` (bool).
- **`GET /newsroom/disclosures/congress/{id}`** — the details: both start points, all horizons, `official_url`,
  `asset_type`, `comment`.
- **`GET /newsroom/disclosures/track-record?source=congress&group=direction|chamber|party|late|amount`**
  - amount buckets: ≤ $15k, $15k–50k, $50k–250k, > $250k (by `amount_low`).
  - Same caveat and small-sample rule as for insiders.
- **`GET /newsroom/disclosures/markers?…&kinds=insider,congress`** — congress markers dated by the filing day,
  same-day merge with a count (user). The kinds are separate toggles in MARKERS, off by default.
- **`GET /newsroom/disclosures/members?q=`** — the filers in scope, for the watch list picker:
  `[{id, name, chamber, party, state, trade_count, watched}]`.
- **`PUT /newsroom/disclosures/watch`** `{"members": ["house_…", …]}` — replaces the watched member list.
  - Unknown ids → 400 listing them. A request from a web page (Origin header) → 403, as for `PUT /ai/budgets`.
- **Status:** `/newsroom/disclosures/status` gains `congress: {"backfill", "last_pull", "updated_through", "stale",
  "rows", "rejected"}`.

## 7. UI data needs (W) — Clarity rules
- **Placement:** the Congress tab in Disclosures, with the same compact row, filter bar and chip.
- **Row content:** who (name · chamber · party-state), ticker, buy/sell, amount range, transaction → filing date
  with days to file and a late flag (> 45 days). Details on expand.
- **Defaults:** open-market P/S with a ticker, last 30 days, "N of M shown".
- **Info line:** the source and its staleness, plus the terms sentence ("public records under the Ethics in
  Government Act — personal research only").
- **Fixed congress colour** (W). Watch-list picker; alerts only in Monitoring → Alerts.

## 8. Not in scope
- No DSL or strategy signal (later, strictly by filing date, through the gates).
- No executive-branch (OGE) rows.
- No kadoa returns.
- No other congress data source.

## 9. Tests (offline fixtures)
- Mapping of each transaction type and asset type; OGE dropped; dedupe and amendment by id.
- Staleness after 3 days; ETag 304 path; rejected rows counted.
- The next-open-after-filing-date entry; amount buckets.
- Watch-list PUT validation (400/403); alerts emitted once and never during the backfill; the held-ticker check
  across sources.
- Markers merge per day and kind.

## 10. Decisions (lead, 2026-10-24 — contract accepted)
1. **Filing start:** the next session's open after the filing date. This is conservative: only the date is known, and
   many filings land after hours.
2. **Measurable:** stock + ETF only; the Senate codes are confirmed in the pilot (logged list) and added to the
   constant.
3. **Staleness:** "source not updated since <date>" after 3 **calendar** days without a new refresh. kadoa skipped
   single days in its history (e.g. 10-17), so one missing day is normal and not flagged.
4. **Pull schedule:** no fixed time (the bot's refresh time is unknown). A conditional pull (ETag / If-None-Match)
   every 6 h — nearly free when unchanged. The status shows the last successful refresh and the source's commit date
   when available. (This replaces the "14:00 UTC" in §4.)
