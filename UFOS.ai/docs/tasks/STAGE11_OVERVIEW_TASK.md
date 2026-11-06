# Stage 11 — Overview (design D3) + 5-group navigation + shared UI components

User decisions 2026-11-06 (design artifact "Hedge Fund Overview – drafts", target **D3 "Markets + risk"**):
- The Overview is rebuilt after D3. UI text English; section headings are nouns (never questions); no section
  numbering; no empty area at the bottom of the page.
- Navigation: **5 groups now** — Overview · Strategies · Trading · Research · Risk Lab.
- Council of AIs and the old engine leave the Overview completely. The user asked whether to remove them; the lead
  decided NOT to remove anything now (the old engine still holds the paper account with 2 pending orders and the
  migration depends on it). The whole old Overview content moves **unchanged** into a tab "Legacy: Council & old
  engine" under Trading. Retiring it is a later user decision.
- New backend endpoints for market tickers, risk metrics and an activity feed are built now (with tests).
- Market ticker band = a new, clearly labelled live source (approved 2026-11-06). The options-structure part stays
  archive-only (OS2 API; no live fallback).
- Commit permission: after an independent review and green checks the lead commits locally (no push). WPF cannot be
  built by agents → the WPF part is committed only after the user's build; the backend part may be committed alone.

Scope label: `stage11ov` (scope_guard routine as in os2: check → read "RESULT: ok" → close, as SEPARATE calls; never
chained with `&&`). Scope: `UFOS.ai/HedgeFund/**`, `UFOS.ai/docs/**`.

## 0. Binding rules (from CLAUDE.md / SPEC, restated)
- Missing data reads **"not available (reason)"**. No fallbacks, no stand-in values, no second source for the same
  value, no interpolation. Each value has exactly one source.
- Every value shown carries **source + age** (or "close <date>" for daily data). Outside trading hours the last close
  is shown and marked "closed".
- Colour rules: red/green (`LongBrush`/`ShortBrush`) **only** for the user's own P&L; market changes in neutral grey
  with a sign; blue/orange only for the gamma regime; violet (`AiAccentBrush`) only for AI; amber (`RiskBrush`) only
  for warnings/attention.
- Every chart has the shared full-screen button (`Shared/FullScreenHost.cs`: `CreateButton`/`Wire`/`RegisterF11`).
- Untrusted text (alert bodies, symbols from the backend) is plain text.
- OS2 `provisional: true` must be visible ("provisional") wherever OS2 values appear.
- Nothing here trades or changes trading behaviour. Read-only dashboards only (Run now / Kill switch stay where they
  are in the toolbar, unchanged).
- Tests: Python `unittest`, no network (fakes), synthetic data only; VM chunks ≤ 170 s; full suite once at the end in
  the cloud container via the lead. WPF: static checks only (`tools/check_cs_ambiguity.py`,
  `tools/check_shared_cs_members.py`, `tools/check_no_env_cs.py`), the user builds.
- Never open real DBs over the VM mount; never print secrets; `check_repo_data.py` must pass.
- No deletions: replaced files are renamed `*.stale` only if a file is replaced wholesale (prefer editing).

## 1. Backend — new read-only routes (`strategy_platform/api/overview_routes.py`, token-protected, GET)

### 1.1 `GET /v2/overview/markets`
Response `{generated_at, items: [Item…]}` for exactly these items, in this order:

| key | label | source (single, no fallback) | value / change | series |
|---|---|---|---|---|
| SPY, QQQ, IWM, GLD | "SPY", "QQQ", "IWM", "Gold (GLD)" | Alpaca via the existing market_research data layer (`market_research/data.py` snapshot + bars; respect its rate budget; reuse, do not add a second Alpaca client) | last, change_pct vs prev close | today's regular session 5-min closes (last session when closed) |
| BTC | "BTC" | Alpaca crypto (`BTC/USD`) via the same layer | last, change_pct (24 h, state it) | last 24 h, 15-min closes |
| US10Y | "US 10Y" | FRED `DGS10` from the existing Newsroom FRED store (read the newest stored value; no new network path if the store already holds it — check `newsroom` macro code) | value in %, change in bp vs previous observation | last 30 observations (daily) |
| EURUSD | "EUR/USD" | FRED `DEXUSEU` (same rule; if the Newsroom store does not collect it, add it to that collector's series list — document) | value, change_pct vs previous observation | last 30 observations |
| VIX | "VIX" | OS2 archive: the latest SPY surface `vix_structure.vix` (= the archived FRED VIXCLS daily close) | value; `as_of` = the value date, slot age separate; change in pt vs the newest archived value with an EARLIER date (none → `change: null` + reason) | one point per distinct value date (≤ 30) |

`Item = {key, label, available, reason, value, change, change_unit ("pct"|"bp"|"pt"), series: [{t, v}], source
("Alpaca"|"FRED"|"Archive"), as_of (ISO), age_minutes, session ("open"|"closed"|"daily"), note}`.
- Server-side cache 60 s (one Alpaca round per minute at most); a failed item is `available:false` with the reason;
  the other items still return.
- `session:"daily"` items show "close <date>" in the UI instead of an age.

### 1.2 `GET /v2/overview/risk`
Risk figures of the **active paper account** (the account that currently trades):
- while `legacy_account.status` is not `migrated`: the old-engine account (`/fund` equity series + state);
- after migration: the platform portfolio (the `/v2/portfolio/state` total; find the platform equity history; if
  none exists, the series-based figures are `available:false` "no platform equity history yet").
Response `{account: "legacy"|"platform", as_of, n_daily_returns, figures: {var99_1d, es975_1d, net_exposure_pct,
gross_exposure_pct, max_drawdown_pct, vol20_ann_pct}}`, each figure `{value, unit, available, reason, method}`.
- Daily returns from end-of-day equity (last point per NY trading day), simple returns.
- **VaR 99 % / ES 97.5 % 1 day: historical simulation** on the daily P&L in USD (equity_t−1 × r_t applied to the latest
  equity), loss positive; quantile convention identical to RiskLab `models/historical.py` `hs_var_es` (re-implement,
  do NOT import RiskLab — its README forbids imports either way; cite it in a comment). Minimum window = the RiskLab
  rule ceil(1/(1−α)): **VaR 99 % needs ≥ 100 daily returns, ES 97.5 % ≥ 40** (lead decision after review,
  2026-11-06; supersedes the earlier "≥ 60"), else `available:false` with "needs ≥ N daily returns, have n".
- vol20: stdev of the last 20 daily returns × √252 (needs ≥ 20). Max drawdown: over the whole series (peak-to-trough
  of equity). Exposure: from the account state (net = (long−short)/equity, gross = (long+short)/equity).
- Tests: hand-computed small series (quantile convention, ES average, too-few-returns reason, drawdown, vol).

### 1.3 `GET /v2/overview/activity?limit=20`
Merged, newest first: `{items: [{ts, kind, title, detail, severity ("info"|"warning"|"error"), link}], sources:
{name: "ok"|"not available (reason)"}}` from: OMS orders (placed/filled/cancelled), fills, monitoring alerts, loops
finished, options-archive runs (failed/partial only, plus the latest ok), Council runs, old-engine `/fund/events`.
`link` names the target view (`trading/orders`, `trading/monitoring`, `strategies/loop`, …) for the UI to navigate.
Each source read with its own try; a failing source is listed in `sources`, the rest still returns. Plain-text fields.

### 1.4 Docs
SPEC: new § "26. Stage 11 — Overview API" (routes, schemas, rules above); CODEMAP lines; backend README route list.

## 2. WPF — shared components (`HedgeFund/Ui/`, code-built like `Platform/*View.cs`)
- `HfTokens` (one place for the D3 colours/sizes, mapped to the existing window brushes where they exist).
- `Card` (panel background, 1 px border, radius 12, padding 16), `SectionHeader(title, rightContent)`,
  `KeyValueRow(label, value, sub, dotKind)`, `AttentionStrip` (one line, amber left edge, "N open" + inline links,
  hidden when N = 0 → then a one-line "Nothing needs attention" in grey), `TickerCell`, `Sparkline` (native
  OnRender, neutral grey), `GexBarChart` (bars per strike, orange < 0 / blue > 0, dashed flip line, solid spot line,
  axis labels), `RangeBar` (expected move), `TermCurve` (points + labels), `StatChip` (risk row).
- Every chart control gets a ⛶ button via `FullScreenHost` + F11.
- Reused by later stage-11 views (Library, Loop, Trading …) — keep them generic.

## 3. WPF — navigation (5 groups)
- Rail: Overview · Strategies · Trading · Research · Risk Lab (Risk Lab separated by a divider). Icon + short label,
  active item with a blue edge bar (D3). Monitoring badge moves to the Trading item (and its sub-tab).
- Groups with several views get a sub-tab strip at the top of the content area:
  - Strategies: Library · Studio · Loop
  - Trading: Portfolio & Trading · Monitoring · Shadow Account · **Legacy: Council & old engine**
- Existing hosts and lazy creation stay (`EnsureXView`, `ActivateAsync`, timers only while visible). The old
  `OverviewScroll` (with `CouncilHost` and every Legacy* section) is shown under Trading › Legacy, **unchanged**
  (same names, same handlers, same polling).
- Risk Lab: a placeholder view "Risk Lab view — next step of stage 11" (honest; no fake content).
- Keyboard: rail and sub-tabs reachable by Tab; AutomationProperties.Name on every item.

## 4. WPF — the new Overview (`HedgeFund/Platform/OverviewView.cs` + `OverviewHost`)
Layout (D3), fills the window height (no empty band at the bottom; the main row stretches):
1. **Attention strip**: pending orders (old engine `pending_orders` and platform working orders, count + "→ Orders"),
   migration/switch-over open (from `/v2/portfolio/migration` / Council `switchover`; text "Migration & Council
   switch-over pending"), unseen alerts (`MonitoringNotifier.Unacked`), halts/kill switch active (from state).
   Each item navigates to its view.
2. **Market band** (one card): header "Market" + "Global Monitoring →" (opens the GM window if the app offers a hook;
   else omit the link — no dead links). Row 1: 8 `TickerCell`s from `/v2/overview/markets` (label, change with sign in
   grey, value, sparkline, "source · age" / "close <date>"). Row 2 "Options structure" + "provisional · <age>":
   - **SPY gamma exposure by strike** (`/v2/options-structure/gex?symbol=SPY&slot=latest`, variant **A** bars — the
     regime label uses A; state "variant A" in the chart caption), flip + spot lines, regime dot + label from
     `/state` (`regime.gex_sign`, `agree`), caption "Flip <x> (<d> % below/above spot) · variants A/B/C <k>/3 agree".
   - **QQQ gamma**: `/state?symbol=QQQ` `regime.signs{A,B,C}` as three rows "Variant A · long calls / short puts",
     "Variant B · short all options", "Variant C · moneyness split" (definitions from `gex.py`), + "no regime shown
     while variants disagree" when `agree` is false.
   - **Expected move · SPY**: `/surface` `expected_move.today` (or next) — ± %, range bar with spot and bounds; next
     expiry from `/calendar`.
   - **VIX term structure**: `/surface` `vix_structure` — plot only the points that exist (VIX, VIX3M; no invented
     9D/6M), label (contango/backwardation), ratio.
   - Unavailable parts show "not available (reason)" with the OS2 reason; `available:false` because of age → say so.
3. **Strategies card**: active paper account (§1.2 rule) — equity, today, total (P&L in red/green), range buttons
   1M/3M/All (client-side filter of the series); **risk row** (`/v2/overview/risk`: VaR 99 % 1d, ES 97.5 % 1d,
   Exposure, Max DD, Vol 20d, each "not available (reason)" when unavailable; link "Risk Lab →" to the rail item);
   **equity curve vs SPY** (reuse `Fund/EquityChart.cs` or a generic chart from `Ui/`; ⛶); **strategy tiles** from the
   platform (`/v2/portfolio/accounts` sources: name, state, total P&L — label "total", no "today" per strategy because
   it does not exist) — up to 3 + "All strategies →", plus an incubation tile (count of running incubations, "Start
   incubation →"). No Council/old-engine tile unless the platform lists them as strategy sources.
4. **Right column**: **System** (one-line rows: Loop latest status + finalists; Validation/backtests running count;
   Options archive state + "today collected/expected"; AI budget today (sum of the Hedge Fund module entries with a
   daily period from `/ai/budgets`, violet; document the rule); Connections (brokers + data sources: "all ok" or the
   first problem)). **Recent activity** below, filling the rest (`/v2/overview/activity`), each row time · kind ·
   detail, click → `link`; "All →" to Monitoring › Alerts.
- Polling: markets 60 s, OS2 state 60 s (gex/surface when the slot changes), risk 5 min, activity 15 s, system 15 s,
  account via the existing 5 s `/fund/state` poll — only while the Overview is visible (follow the view pattern).
- Responsive: below ~1250 px window width the right column moves under the strategies card; the ticker row wraps to
  2 rows. Minimum: no clipped text (ellipsis + tooltip).
- C# client: `PlatformClient.Overview.cs` (markets, risk, activity) and `PlatformClient.OptionsStructure.cs`
  (state, gex, surface, calendar) with DTOs; tolerant JSON (missing fields → null → "not available").

## 5. Acceptance
- Backend tests for 1.1–1.3 (shapes; each item's unavailable path; cache; risk maths by hand; activity merge order;
  a failing source). Existing suites green.
- Static C# checks pass; every new chart has ⛶; no German UI text; no question headings; no numbering.
- The old Overview content is reachable and unchanged under Trading › Legacy (diff shows only the move of its
  host/visibility wiring).
- SPEC §26 + CODEMAP + todo updated; `check_repo_data.py` ok.
- Independent review before commit.
