# Market Research SPEC — archive: mr8-notes

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

### MR8 backend notes (implemented 2026-10-17: in-module integrations — routes + exact JSON, binding for MR8-WPF)

**Files.** `integrations.py` (read-only views), `integrations_api.py` (`IntegrationRoutes` mixin,
`INTEGRATION_ROUTES` appended to `api.ROUTES`). No store migration (nothing new is stored by Research; the
next free market_research.db version stays v8). Strategy Platform extension (§12 here and in its SPEC):
`backtest/runcard.py` stores the fills on new run cards. No other module's code changed.

**Read-only guarantee.** Other modules' databases (`newsroom.db` = Newsroom + HedgeFund fund tables,
`hedgefund.db` = Strategy Platform) are opened ONLY via `integrations.ro_connect`: SQLite URI `mode=ro` +
`PRAGMA query_only = 1` — any write raises; their store modules' write functions are never called; a missing
file is never created. **No order route exists in Research** (tested: no `/research/*order*` path; every
MR8 route is GET). Errors: source missing / not set up → 503 `{"error": <reason>, "kind":
"IntegrationUnavailable"}`; unknown run / watchlist → 404 `{error}`; bad input → 400 `{error: "invalid
input", errors}`. External text (insider names/roles, release names, "why" texts) → `news_text.plain_text`;
links http(s) only (`safe_link`), else null.

**`GET /research/portfolio`** (no query) — the HedgeFund paper account as the engine stored it:
```json
{"source": "HedgeFund paper account", "read_only": true, "paper": true, "generated_at": "2026-10-17T14:00:00+00:00",
 "cash": 90000.0,
 "equity": {"value": 100500.0, "ts": "2026-10-15T20:00:00+00:00", "drawdown_pct": 0.4, "reason": null},
 "starting_capital": 100000.0,
 "positions": [{"symbol": "AAPL", "qty": 50.0, "side": "long", "avg_price": 200.0, "last_price": 210.0,
                "market_value": 10500.0, "unrealized_pnl": 500.0, "unrealized_pct": 5.0, "realized_pnl": 0.0,
                "opened_at": "2026-10-02T14:00:00+00:00", "updated_at": "2026-10-15T20:00:00+00:00", "reason": null},
               {"symbol": "TSLA", "qty": -10.0, "side": "short", "avg_price": 300.0, "last_price": null,
                "market_value": null, "unrealized_pnl": null, "unrealized_pct": null, "realized_pnl": 0.0,
                "opened_at": null, "updated_at": "…", "reason": "No engine mark for this position yet."}],
 "totals": {"positions": 2, "long_mv": 10500.0, "short_mv": 0.0, "unrealized_pnl": 500.0, "complete": false},
 "engine": {"autopilot": true, "halted": false},
 "marks_note": "Prices are the HedgeFund engine's last marks (written at its runs) — not live quotes.",
 "note": "Read-only view of the HedgeFund paper account. Research places no orders — Buy/Sell opens the HedgeFund manual paper order panel (same confirmations and audit). Paper trading only."}
```
`cash` = sum of the fund cash ledger; `last_price` = the engine's mark (`positions.last_price`); no price is
fetched by Research (a missing mark stays null with the reason; `totals.complete: false`). The WPF may show
live P&L from the `trades:` stream itself, labelled as such. Not set up (no ledger rows / tables) → 503.

**Buy/Sell hand-over (WPF only, no backend route).** Buttons Buy / Sell (and Short / Cover where the
HedgeFund panel offers them) in the symbol header and the Portfolio rows open the HedgeFund window's existing
**manual paper order panel** (`HedgeFundWindow`: `ManualPanel`, `TickerInput`, `SizeInput`, buttons
`ManualBuy/Sell/Short/Cover`) with `TickerInput` = the platform symbol (`AAPL`, `BTC-USD`) and the chosen side
highlighted / focused — size empty, NOTHING submitted automatically. The existing panel keeps its
validation, confirmations, `POST /fund/orders` and audit; paper only, never live. Research passes only
`{symbol, side}`.

**Macro section.** Reuses the Newsroom routes as they are: `GET /newsroom/macro` (FRED series + derived
indicators, `status`) and `GET /newsroom/calendar` (upcoming releases + FOMC) — same token header; no
Research copy (reuse, no duplication). Their `status` / error texts are shown as given.

**`GET /research/symbols/{s}/markers?from=&to=&kinds=`** (`from`/`to` ISO-8601, default the last 365 days
ending now, `from < to`, span ≤ 7400 days; `kinds` comma list ⊆ `insider,calendar`, default both):
```json
{"symbol": "AAPL", "from": "2026-10-01T00:00:00+00:00", "to": "2026-11-01T00:00:00+00:00",
 "kinds": ["insider", "calendar"],
 "items": [{"t": "2026-10-13T04:00:00+00:00", "t_end": null, "kind": "insider", "side": "sell",
            "label": "Insider sell: John Roe (CEO) — 500 sh @ $20.00",
            "detail": {"owner": "John Roe", "roles": ["CEO"], "shares": 500.0, "price": 20.0, "value_usd": 10000.0,
                       "filed_at": "2026-10-14T21:00:00+00:00", "accession": "0001", "url": "https://www.sec.gov/…",
                       "plan_10b5_1": false, "source": "SEC EDGAR Form 4 (Newsroom)"}},
           {"t": "2026-10-20T12:30:00+00:00", "t_end": null, "kind": "release", "side": null, "label": "CPI (inflation)",
            "detail": {"importance": "high", "series_id": "CPIAUCSL", "why": "…", "url": null,
                       "source": "FRED release calendar (Newsroom)"}},
           {"t": "2026-10-28T18:00:00+00:00", "t_end": null, "kind": "fomc", "side": null, "label": "FOMC rate decision",
            "detail": {"meeting_start": "2026-10-27", "sep": false, "importance": "high",
                       "source": "Federal Reserve (Newsroom list)"}}],
 "truncated": false,
 "unavailable": {"earnings": "The Newsroom has no earnings calendar — earnings dates are not shown."},
 "coverage": {"insider": "Form 4 filings the Newsroom keeps (last 5 days of filings)",
              "calendar": "FOMC decisions from the Newsroom's list; FRED release dates of its latest calendar snapshot (upcoming window only)"}}
```
- `kind` ∈ `insider` (Form 4 open-market purchase `P` → `side: "buy"`, sale `S` → `"sell"`; grants,
  exercises, tax withholding and derivative rows are not markers; `t` = 00:00 New York of the transaction
  date), `fomc` (decision day 14:00 ET, from the Newsroom's verified `FOMC_MEETINGS` list — read from
  `newsroom/calendar_collector.py` with `ast.literal_eval`, not duplicated; the snapshot's FOMC rows are
  merged), `release` (FRED release dates of the Newsroom's latest calendar snapshot, `time_hint` "08:30 ET"
  etc. → that New York time, else 00:00). Sorted by `t`; ≤ 2000 items (`truncated`). Calendar markers are
  market-wide (also for crypto).
- `unavailable` per part (always `earnings`): `insider` (crypto: "Form 4 insider filings exist only for
  US-listed companies."; no table yet), `releases` (no snapshot yet: "The Newsroom has no economic calendar
  snapshot yet (FRED key missing or the first poll is pending) — release dates are not shown."), `fomc` (list
  unreadable). The Newsroom keeps Form 4 filings only 5 days (its retention) and the calendar only for the
  upcoming window — older chart ranges have fewer markers (stated in `coverage`). newsroom.db missing → 503.

**`GET /research/backtests/{run_id}/markers?symbol=&from=&to=`** (`run_id` `bt-` + 12 hex else 404;
`symbol` required; `from`/`to` optional filters) — read-only from the run card:
```json
{"run_id": "bt-aaaaaaaaaaaa", "strategy_id": "momentum", "version": 3, "symbol": "AAPL", "read_only": true,
 "items": [{"t": "2026-01-02T05:00:00+00:00", "t_end": "2026-04-02T04:00:00+00:00", "kind": "period_best", "side": null,
            "label": "Best 63-session window: +12.0 %",
            "detail": {"strategy_return": 0.12, "benchmark_return": 0.05, "excess": 0.07}},
           {"t": "2026-01-05T21:00:00+00:00", "t_end": null, "kind": "fill", "side": "buy", "label": "Buy 10 @ 150.00",
            "detail": {"qty": 10.0, "price": 150.0, "entry": true}},
           {"t": "2026-05-04T04:00:00+00:00", "t_end": null, "kind": "drawdown", "side": null,
            "label": "Drawdown 11.0 % (not recovered)",
            "detail": {"trough": "2026-06-10T04:00:00+00:00", "depth": 0.11, "recovered": false,
                       "contributors": [{"symbol": "AAPL", "pnl": -900.0}], "symbol_contributed": true}}],
 "truncated": false, "unavailable": {}}
```
`kind` ∈ `fill` (the symbol's fills; `side` buy|sell|short|cover, `detail.entry` = buy/short), `period_best`,
`period_worst` (§11 attribution `periods.best/worst`, range `t`…`t_end`), `drawdown` (§11 `periods.drawdowns`,
`t` = peak, `t_end` = recovery or null, `detail.trough`); period dates → 00:00 New York (crypto: UTC). The
run must be `done` (else 503 "The run is running — markers exist only for finished runs."). `unavailable`:
`fills` ("This run card was created before entries/exits were stored on cards (Strategy Platform,
2026-10-17) — run the backtest again."), `fills_after_cap` (card fill cap reached), `periods` (no
attribution / its `unavailable` reason). The WPF opens the run from the Strategy Platform ("Open in Market
Research": symbol + run id) and draws these as chart markers (labels plain text).

**`GET /research/watchlists/{id}/universe`** — a watchlist as Strategy Platform universe input:
```json
{"id": "6f1c…", "name": "Tech", "universe": {"symbols": ["AAPL", "SPY"]}, "count": 2, "asset_classes": ["equity"],
 "excluded": [{"symbol": "GONE", "reason": "No longer in Alpaca's asset list."}], "max_symbols": 500, "reason": null,
 "note": "Paste/apply this list as universe.symbols in the Library's strategy editor — Research does not change strategies. Mixed stock/crypto universes get the Strategy Platform's calendar warning."}
```
Symbols in list order, normalised by `core/instruments` (the validator's rule), de-duplicated, ≤ the
Strategy Platform's `MAX_SYMBOLS` (500); uncovered items excluded with the reason; empty → `universe: null`
+ reason. The WPF hands it to the Library's strategy editor (universe field) — the user saves there; no
strategy write from Research.

**Tests** (`tests/test_mr_integrations.py` 12; temp newsroom.db via the Newsroom/fund writers, a minimal
temp `backtest_runs` table): portfolio shape + values + missing marks + not set up + missing file (not
created); read-only (file hash unchanged after reads, the connection refuses INSERT/DELETE, no write SQL /
writer calls in `integrations.py`, no order route, MR8 routes GET only); markers (insider buy/sell in range,
codes/derivatives filtered, plain-text names, unsafe links null, release time hints, FOMC once, earnings
unavailable, crypto, missing snapshot, validation, default window); backtest markers (fills + periods +
drawdown, range filter, other symbol, old card, running run, unknown/bad ids, validation, file unchanged);
universe (route, exclusions, empty, 404/400); run-card fills + cap (Strategy Platform extension). MR tests
325 OK; full suite OK (split runs); scope guard stage0 + mr0 ok.

**Defaults (user away 2026-10-17) — confirm:** separate backtest-markers route; only open-market Form 4
purchases/sales as insider markers; card fills capped at 10 000; portfolio values = engine marks (no live
re-pricing in the backend); Macro = the existing Newsroom routes.

### MR8 WPF notes (implemented 2026-10-17: Portfolio, Macro, Buy/Sell hand-over, markers, backtest link, universe)

- **Files.** New `MarketResearch/ResearchView.Integrations.cs` (Portfolio, Macro, order hand-over, markers, backtest
  link, universe) and `MarketResearch/Chart/ChartHost.Markers.cs` (bridge revision 4 host side, `setMarkers`).
  Changed additively: `ResearchClient.cs` (`GetPortfolioAsync`, `GetMarkersAsync`, `GetBacktestMarkersAsync`,
  `GetWatchlistUniverseAsync` + DTOs), `ResearchView.xaml` (nav PORTFOLIO + MACRO, pages, Buy/Sell/Short/Cover row in the
  header, MARKERS toggle + note), `ResearchView.xaml.cs` (Nav_Checked sections, markers hook after loadBars, shutdown),
  `ResearchView.Workspace.cs` (session sections portfolio/macro, order buttons), `ResearchView.Watchlists.cs` ("Use as
  universe…" in a list's edit mode), `Chart/ChartHost.cs` (`SendMarkers()` in `SendAll`), `HedgeFundWindow.xaml.cs`
  (`ResearchView_OrderHandOverRequested`, `OpenInResearch`), `Platform/BacktestReportView.cs` ("Open in Market
  Research:" one button per symbol of a finished run). Static checks only (no dotnet).
- **Portfolio** (sidebar PORTFOLIO, chip "PAPER ACCOUNT · READ-ONLY"): GET /research/portfolio when shown and every
  60 s while shown + Refresh; cash, equity (+ time), drawdown, start capital, engine state; rows symbol (opens the
  tab) · side · qty · avg · MARK (engine mark, tooltip "not a live quote") · value · unrealized ▲/▼ ± · % · realized,
  per-row reason, totals (incomplete flagged), `marks_note` + `note` verbatim. Failure → "Portfolio unavailable:
  <reason>", nothing old shown. No order function in this section.
- **Macro** (sidebar MACRO): the Financial Newsroom's own `MacroIndicatorsView` and `EconomicCalendarView`
  (/newsroom/macro, /newsroom/calendar) hosted as they are (no copy), tabs MACRO INDICATORS / ECONOMIC CALENDAR,
  created on first use, `Shutdown()` with the view.
- **Buy/Sell/Short/Cover** (symbol header): enabled for symbols the paper engine accepts (`FundAssets.IsValidSymbol`,
  ≤ 14 chars like the panel's ticker box), otherwise disabled with the reason in the tooltip. Click → event
  `OrderHandOverRequested {symbol, side}` → `HedgeFundWindow` switches to Overview, activates itself, sets
  `TickerInput` = symbol, clears `SizeInput`, writes "From Market Research: <Side> <SYM> — enter the size and press
  <Side>. The order is confirmed before it is sent (paper account only)." and focuses the size box. Nothing is
  submitted; the panel's validation, confirmation, POST /fund/orders and audit are unchanged.
- **Markers** (toolbar MARKERS, per tab, default off): GET …/markers?kinds=insider,calendar&from=<oldest bar>&to=now,
  plus GET /research/backtests/{run}/markers?symbol= for a tab opened from a backtest (toggle reads "MARKERS +
  BACKTEST", on automatically). Note line: count, every `unavailable` reason verbatim (always "earnings unavailable:
  The Newsroom has no earnings calendar …", insider for crypto, fills for old run cards), truncation, route errors;
  tooltip lists the first 25 labels with local times. Markers go to the page via `setMarkers` (revision 4) — until the
  page reports revision 4 the note says they cannot be drawn yet. Cleared at every loadBars, re-loaded when on.
- **Open in Market Research** (backtest report of a finished run): one button per non-excluded symbol of the run's
  data snapshot → `HedgeFundWindow.OpenInResearch(symbol, runId)` (owner chain, else the app's Hedge Fund window) →
  Research section (or the pop-out) with that symbol and the run's markers; old run cards show the backend's
  "fills unavailable: … run the backtest again." in the markers note.
- **Use as universe** (watchlist edit mode): GET …/universe → a NEW strategy draft (`NewStrategyTemplate` with
  `universe = {symbols: [...]}`, name "New strategy (<list>)") opened in the Library's `StrategyEditorWindow` — not
  saved; the user saves there (the Library shows it after its next refresh). Excluded symbols + the backend `note`
  are shown in the watchlist message; empty → its `reason`. No strategy write from Research.
- **Pending (chart page):** bridge revision 4 `setMarkers` (see "Revision 4" in the bridge contract) — to be
  implemented by the MR8 chart-page agent; the host side is ready and gated by `ready.revision ≥ 4`.
- **Verify on the PC:** first build; hand-over focus (Research popped out → Hedge Fund window comes to front);
  Newsroom views inside the Research section (sizes, their popups); editor window from Research; markers once the
  page implements revision 4.

### MR8 chart notes (implemented 2026-10-17: chart page — bridge revision 4 `setMarkers`)

- `chart.js` reports `ready.revision: 4` and implements "Revision 4 — MR8 chart markers" exactly: schema
  (≤ 2000 markers, unique `id` ≤ 40 plain chars, `t`/`tEnd` epoch ms with `tEnd ≥ t`, the 7 kinds, sides,
  `label` ≤ 200 plain text, unknown keys → `bad_message`, whole message ignored). Anchoring per draw: the
  bar containing `t` (start ≤ t < start + period), else the first bar starting after `t`, none outside the
  loaded data; ranges from the bar of `t` (clipped to the first bar) to the bar of `tEnd` (or the last bar).
  Markers survive `loadBars` of the same symbol (other timeframe → re-anchored) and are cleared by the page
  on a symbol change (the host also sends `[]` + the new set). Drawing: two data-less engine indicators on
  `candle_pane` (`UFOS_MARK_BANDS` zLevel −1: `period_best` up / `period_worst`, `drawdown` down at 10 %
  alpha behind the candles; `UFOS_MARK_POINTS` in front: ▲ below the bar low for buy/cover in `up`, ▼ above
  the high for sell/short in `down`, ● muted without side, stacked when several share a bar; `fomc`/`release`
  dashed vertical `caution` line + tag "FOMC"/"R"). No figures → the price scale never changes. Hover: the
  legend row `#legend-marks` lists glyph + label (textContent) of the hovered bar's markers (3 + "n more").
- Tests (`run_tests.js`, +21; 229/229 with the test double): revision, snapping in-bar / weekend gap /
  outside range, range clipping + open drawdown, hover legend (plain text, no HTML), same-symbol tf change
  keeps, symbol change and `[]` clear, 11 schema rejections. Screenshots `14_mr8_markers`, `15_mr8_marker_hover`.
- **Verify with `--engine real`:** indicator `draw` params `yAxis.convertToPixel(price)` (used for the glyph
  position; missing → `engine_api`), `xAxis.convertToPixel(dataIndex)`, zLevel −1 (bands behind candles) vs
  2 (glyphs in front), two data-less indicators stacked on `candle_pane` not affecting the y-range, redraw
  after `setMarkers` via the re-sent last bar.

**MR6/MR7 review fixes (2026-10-17, backend).** H1 deadline-capped thumbnail download + bounded DNS (drip-server
tests for body and headers, DNS timeout test); L5 connection pinned to the checked IP with SNI/certificate
check on the host name (test); M2 own thumbnail pool + 503 `ThumbnailBusy` (test: 10 hanging images → 2 busy,
the news route still answers < 1 s); L6 WebP removed from the allowed thumbnail types (tests); M3 alert
triggers flushed before reloading, previous closes on their own schedule (test). New limits:
`NEWS_IMAGE_GATE_WAIT_SECONDS` 5, `IMAGE_EXECUTOR_WORKERS` 2, `IMAGE_QUEUE_MAX` 8. MR tests 333 OK; full suite OK
(split runs); scope guard stage0 + mr0 ok.

