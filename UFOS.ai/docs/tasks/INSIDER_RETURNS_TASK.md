# INSIDER-RET — return after the trade for Financial Newsroom → Insider Trades (user decisions 2026-10-24 20:29 Berlin)

Queued AFTER the Council R2+R3 commit. Backend: H (newsroom). UI: W. Contract first: UFOS.ai/docs/tasks/INSIDER_RET_API.md.

## User decisions
- Two start points side by side: **from the SEC filing** (main value: first tradable price after the EDGAR acceptance
  time; after hours → next regular open) and **from the insider's trade date** (small, next to it; labelled "not
  tradable — insider's own timing"). The gap between them shows what the reporting delay costs.
- Horizons 1 / 5 / 20 / 60 trading days (accepted default); open horizons shown as "running (n of 20 days)".
- Benchmark: **sector ETF** — excess return = stock return − sector ETF return over the same window. Mapping:
  SEC company SIC code (EDGAR submissions JSON) → sector ETF table (XLK, XLF, XLV, XLE, XLI, XLY, XLP, XLU, XLB, XLRE,
  XLC); the table lives in one file with its source/date. Unmapped SIC → raw return shown, excess shown as
  "no sector ETF for SIC nnnn" (no silent SPY substitute — would be an unrequested fallback).
- Trades measured: open-market purchases (P) and sales (S); for S the excess is scored with the sign reversed
  ("did the price fall afterwards?"). Shown separately: 10b5-1 planned vs not; by role (CEO / CFO / Director /
  10 % owner / other) and single trade vs cluster.
- Retention: insider filings + computed returns kept **2 years** (today 5 days) — only the core fields needed for
  the cards and returns; update the retention rule in conventions/SPEC.

## Design notes
- Prices: Alpaca daily/minute bars through the existing http gate (rate limits!), point-in-time; split/dividend
  handling stated (adjusted bars for returns; say which).
- Recompute incrementally once per day after the close; never per UI refresh. No AI calls.
- Statistics view: per group n, mean/median excess per horizon, hit rate, with sample size and a plain caveat
  ("descriptive statistics, not a validated signal — small samples are noise"). No significance theatre; show n.
- Cards: "since filing: +3.2 % (sector XLK +1.1 % → excess +2.1 %) · 5 d" + small "from trade date: +5.0 %".
- **Backfill: YES (user 2026-10-24 20:31)** — load past Form 4 filings for the 2-year retention window from SEC EDGAR
  (quarterly full-index / form.idx → Form 4 XML), respecting the SEC fair-access limit (≤10 req/s, declared
  User-Agent from the Credential Manager), resumable with a progress state, gaps recorded. Only open-market P/S
  rows are kept after parsing. Estimate volume/time in the contract before running; runs in the background, never
  during market-hour trading cycles if it competes for the Alpaca gate (prices for backfilled rows are fetched in
  batches through the gate).
- Later bridge: the same return engine can serve congress trades (STOCK Act) if that package is approved.

## UI (user decisions 2026-10-24 20:40): see CONGRESS_TASK.md "Clarity rules" — the Insider tab of the new
"Disclosures" section; statistics on the "Track record" tab.
