# CONGRESS — congress trades (STOCK Act) in the HedgeFund module (user decisions 2026-10-24 20:31–20:40 Berlin)

Queued after INSIDER-RET (shares its return engine). Backend: H (newsroom). UI: W. Contract first: CONGRESS_API.md.

## Source (user choice): the kadoa-org/congress-trading-monitor repo (MIT code), refreshed daily by kadoa-bot
- Checked 2026-10-24 in the browser: commits "Daily refresh: YYYY-MM-DD" every day (latest 2026-10-23).
- Files under https://raw.githubusercontent.com/kadoa-org/congress-trading-monitor/main/public/data/ :
  `trades.json` (newest 5,000 rows, ~4.3 MB, filing dates ~2.5 months), `filers.json` (452 filers, 369 congress),
  `filer/<id>.json` (full history per filer — e.g. house_josh_gottheimer back to 2018), `ticker/…`, prices/returns
  (THEIR computations — not used; we compute our own).
- Row fields: id, source_id (house_clerk / senate_efd / oge_executive), transaction_date, filing_date, owner, ticker,
  asset_name, asset_type, transaction_type, amount_range_low/high/label, days_to_file, is_late, comment, filer_id,
  filer_name, chamber, party, state, office, doc_url (official House/Senate document link), filing_type.
- Scope (user): House + Senate only → keep source_id house_clerk + senate_efd; drop oge_executive.
- Data terms: README says the disclosure data comes from public filings, "provided for research and educational
  purposes"; the underlying filings fall under the Ethics in Government Act use restrictions (no commercial use,
  no credit rating, no solicitation) — personal research only, no redistribution, no public signal feed (our rule).
- Dependency risk: a third party's daily job; if it stops, the card must say "source not updated since <date>"
  (staleness visible, no silent fallback to other sources). Keep doc_url so every row is traceable to the official
  filing.

## Features (user order)
1. Newsroom section "Congress Trades": cards like Insider Trades (who, chamber/party/state, ticker, buy/sell, amount
   range, transaction date → filing date, days to file, late flag > 45 days), filters chamber/party/late/ticker;
   link to the official document (opens in the default browser, http/https only).
2. Return after the trade — the SAME engine as INSIDER-RET: from filing date (main) + from transaction date (small),
   sector ETF via SIC, horizons 1/5/20/60 trading days, purchases and sales (sales scored reversed), statistics by
   chamber / party / late vs on time / amount range, with n and the "descriptive, not a validated signal" caveat.
   Only rows with a ticker and an equity/ETF asset type are measured; others listed as "not measurable (asset type)".
3. Research chart markers (congress + insider buys/sells on the price line via the existing MARKERS control) and a
   watchlist alarm: alert when a watched member files, or when a ticker held in a platform paper account / Shadow
   Account appears (Monitoring alert + toast).
- Not now: strategy signal in the DSL (later, strictly by filing date, through the gates).

## History
- 2 years back (same as insiders): initial load from filer/<id>.json for congress filers, keep rows with
  filing_date ≥ today − 2 y; daily incremental via trades.json (+ re-pull of filers whose trade_count changed).
- Retention 2 years; dedupe by row id; amended filings (same doc re-filed) handled by id.
- Network: GitHub raw, polite (one pull per day after the bot's refresh, conditional requests/ETag).

## Clarity rules (user decisions 2026-10-24 20:36–20:40) — apply to INSIDER-RET too
- ONE Newsroom section "Disclosures" (replaces "Insider Trades") with tabs: Insider · Congress · Track record.
  Shared card layout, shared filter bar, shared return chip.
- Compact rows: who, ticker, buy/sell, amount, reporting delay, ONE return chip (e.g. "+2.1 % vs XLK · 20 d");
  details only on expand (both start points, all horizons, 10b5-1, official document link).
- Default filters: open-market P/S with ticker, last 30 days; always show "42 of 1,200 shown · change filters".
- Group statistics only on the "Track record" tab, never on the cards.
- Chart markers off by default; separate toggles Insider / Congress in MARKERS; same-day trades merged into one
  marker with a count.
- Alerts (watched member files / held ticker appears): **Monitoring → Alerts only, NO toast** (user choice).
- Fixed colours: one for insider, one for congress; violet stays reserved for AI.
