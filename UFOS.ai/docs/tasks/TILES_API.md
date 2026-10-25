# TILES_API — module tile previews (Agent W, 2026-10-24, DRAFT for the lead — no code in the repo yet)

Implements TILES_TASK.md (user decisions 2026-10-24 19:27). Scope label `tiles0`. Open questions: §9 — please decide before
I implement.

## 1. Overview
Every module tile in the MainWindow shows ONE of three preview modes, chosen per tile with a "⋯" button (bottom right):
- **Live** (default) — 2–3 real key figures, refreshed every 30 s while the main window is visible (paused while minimised /
  hidden / the screen is locked).
- **Last view** — a picture of the module window taken when it closed, stored ONLY blurred.
- **Illustration** — a static vector motif per module (design-system brushes only, no information).
Menu items: `Live` · `Last view` · `Illustration` (radio-check on the current one) + `Delete last view` (only when one exists).
A mode a module cannot offer is shown DISABLED with its reason as the item's tooltip and AutomationProperties.HelpText
("module not built yet"; "no picture yet — it is taken when the window closes"). Nothing switches silently: a tile whose
chosen mode has nothing to show says so in the tile (e.g. "Last view: no picture yet").

## 2. Where the code lives (see §9 Q1)
Proposal: a small PUBLIC contract library `UFOS.ai.ModuleTiles` (new project, referenced by the main app and each module
project) — `Shared/*.cs` cannot carry the interface because it is compiled INTERNAL into every assembly (four different
types). Each module implements its provider in its own project (rule "every module implements all three modes"); the
main app registers them in one list.

```csharp
namespace UFOS.ai.ModuleTiles
{
    public enum TileMode { Live, LastView, Illustration }

    public sealed record TileFigure(string Label, string Value, TileTone Tone = TileTone.Neutral, string? Detail = null);
    public enum TileTone { Neutral, Positive, Negative, Caution, Ai }          // mapped to design-system brushes by the host

    public sealed record TileSpark(IReadOnlyList<double> Points);              // ≤ 60 points, drawn by the host (no axes)

    public sealed class TileLiveData
    {
        public required DateTimeOffset AsOf { get; init; }                    // when the figures were read
        public required IReadOnlyList<TileFigure> Figures { get; init; }      // 1–3 shown
        public TileSpark? Spark { get; init; }
        public IReadOnlyList<(string Label, int Count, string BrushKey)>? Dots { get; init; }   // GM categories
    }

    public enum TileLiveState { Fresh, BackendOff, NoDataYet, Unavailable, NotBuilt }
    public sealed record TileLiveResult(TileLiveState State, TileLiveData? Data, string? Reason);

    public interface IModuleTilePreview
    {
        string ModuleId { get; }            // "hedgefund" | "global-monitoring" | "livestream-agent" | "transaction-log"
                                            // | "system-logs" | "sentiment-optimization" | "strategy-discovery"
        string DisplayName { get; }
        /// null = available; else the reason shown in the menu ("module not built yet").
        string? LiveUnavailableReason { get; }
        string? LastViewUnavailableReason { get; }
        /// Reads the figures. MUST NOT start a backend, MUST NOT cause an AI call or any paid call (see §3 per module).
        /// Never throws: failures are a TileLiveResult with State Unavailable and a reason.
        Task<TileLiveResult> ReadLiveAsync(CancellationToken ct);
        /// The module's motif (vector, design-system brushes; resource key in the module's own ResourceDictionary).
        System.Windows.Media.Drawing Illustration { get; }
        /// Areas of the open module window that WPF cannot render (HwndHost WebView2): captured with CapturePreviewAsync.
        IReadOnlyList<TileCaptureArea> WebViewAreas(System.Windows.Window moduleWindow);
        /// True when the module window currently shows sandbox / fixture data → no picture is taken.
        bool IsSandbox(System.Windows.Window moduleWindow);
    }

    public sealed record TileCaptureArea(System.Windows.FrameworkElement Host, Microsoft.Web.WebView2.Core.CoreWebView2 Core);
}
```

## 3. Live — data source per figure (read-only, no backend start, no AI / paid call)
Backend "running" = its loopback /health answers within 800 ms (the same probe the modules use) — the tile NEVER starts one.
The token is read like the modules do (Shared/BackendAuth.cs). Every request: GET, ≤ 3 s timeout, no retry.

| Module | Figure | Source | Notes |
|---|---|---|---|
| Hedge Fund | paper equity + day P&L | `GET /v2/portfolio/state` → `accounts[]` (paper-main + paper-council: `account.equity`, `account.day_pnl`) | see Q4 (does it value positions / call brokers?) |
| | sparkline | `GET /v2/portfolio/accounts/paper-main/equity` (last ≤ 60 points) | |
| | open positions | `accounts[].positions_count` (sum) | |
| | auto trading / kill | `autotrade.enabled`, any `accounts[].halt.active` → "halted" (Negative tone), `GET /v2/council/state` `orders_enabled` after the switch-over | |
| Global Monitoring | events 24 h by category | `GET /events` (snapshot), counted per category with `ts ≥ now − 24 h` | Q5: `/events` also triggers `_refresh_stale_sources()` (free public feeds, but a side effect) |
| LiveStream Agent | last verdict + confidence | `GET /verdicts?limit=1` (read-only history incl. before a restart) | verdict text = AI output → the tile labels it ✦ AI-generated (badge + AutomationName), see §7 |
| | time of last session | newest verdict `ts` | Q6: or a session start time? |
| Transaction Log | AI cost today / this month | `%LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl` (Services/AiCostLedger.ReadAll) — local file, no backend | |
| | vs budget | Q3 — which budget? |  |
| System Logs | errors / warnings 24 h | `%LOCALAPPDATA%\UFOS.ai\logs\app.log` (AppLog) — last 24 h, counted by level; in-memory buffer first | |
| Sentiment Optimization, Strategy Discovery | — | not built: `LiveUnavailableReason = "module not built yet"` | tile shows the Illustration |

**Backend off:** the tile shows the LAST STORED figures with "backend off — as of HH:MM" (user-accepted behaviour, recorded
in the code comment: "accepted by the user 2026-10-24 (tiles interview)"); no stored figures → "no data yet". A read
error with the backend running → "figures could not be read (<reason>) — as of HH:MM" over the stored figures (Caution tone).

**Stored figures:** `%LOCALAPPDATA%\UFOS.ai\tile-previews\live\<moduleId>.json`
`{"schema": 1, "module": "...", "as_of": "...Z", "figures": [{"label","value","tone","detail"}], "spark": [..], "dots": [..]}`
written atomically (temp + move) after every fresh read; never secrets (figures only). Sandbox: figures read while the HF
backend reports `mode: sandbox` are shown with a SANDBOX chip and NOT stored (Q7).

## 4. Last view — capture, blur, storage
- **When:** the module window raises `Closing` → if not yet captured: `e.Cancel = true`, capture asynchronously (≤ 1.5 s
  budget), then close again with a flag (a capture failure never blocks the close; it is logged and the old picture stays).
  Also on app exit for every open module window (same budget, in parallel). Q2.
- **What:** `RenderTargetBitmap` of the window's root visual at 96 DPI × the window's DPI scale; WebView2 areas
  (HwndHost — Global Monitoring globe, Article/Research chart areas if any) via `CoreWebView2.CapturePreviewAsync(Png, stream)`
  drawn at the host element's position onto the same `DrawingVisual`. The Hedge Fund chart is a `WebView2CompositionControl`
  (visual hosting) — included via CapturePreviewAsync too (RenderTargetBitmap does not reliably capture it).
- **Blur (in memory, before anything is written):** scale the composite to 1/10 of its width with
  `TransformedBitmap` + `BitmapScalingMode.Fant` (area average) → a two-pass 5×5 box blur on the small `WriteableBitmap`
  → encode PNG. Result ≈ 120–190 px wide: an 8–10 pt number (11–13 px glyphs at 96 DPI) becomes ≈ 1 px tall — unreadable;
  layout (panels, colours, chart shapes) stays recognisable. Displayed stretched with `BitmapScalingMode.HighQuality`
  (soft). The unblurred bitmap exists only in memory and is released right after the blur. A unit test (static check)
  asserts that the only file write in the capture path takes the blurred bitmap.
- **Storage:** `%LOCALAPPDATA%\UFOS.ai\tile-previews\last-view\<moduleId>.png` + `<moduleId>.json`
  `{"schema": 1, "taken_at": "...Z", "width", "height", "blur": "fant-1/10+box5x5x2"}`; atomic write; never in the repo.
  "Delete last view" deletes both files (asks once: "Delete the stored picture of <module>?").
- **Sandbox:** `IsSandbox(window)` true (HF strip active / backend `mode: sandbox`) → no picture, log line.

## 5. Mode choice persistence
`%LOCALAPPDATA%\UFOS.ai\app-settings.json` (Shared/MainAppSettings.cs) gains `"tile_modes": {"<moduleId>": "live" |
"last_view" | "illustration"}` — strict like the other keys (unknown module id / value → readable error, the tile falls
back to NOTHING silently: it shows the error and the default Live until the file is fixed — see Q8). Missing → Live.

## 6. UI
- Tile layout unchanged at the top (name, status dot, version); the preview fills the area above the name (rounded,
  `ModuleTileBackgroundBrush`, 1 px `SubtleBorderBrush`); "⋯" = 22×22 transparent button bottom right
  (`SecondaryForegroundBrush`, hover `PrimaryForegroundBrush`), keyboard: Tab-reachable, Enter/Space/Shift+F10 open
  the menu; the tile's own click still opens the module (the "⋯" click does not bubble).
- Live: up to 3 figure rows (label 10 px `MutedForegroundBrush`, value 13 px Consolas, tone brush), sparkline 1.5 px
  (`AccentBlueBrush`), status line 10 px ("as of HH:MM" / "backend off — as of HH:MM" / "no data yet").
- Last view: the blurred PNG + "Last view · HH:MM, dd.MM." caption; no picture → "Last view: no picture yet — it is
  taken when the window closes".
- Illustration: the module's `Drawing` in a `DrawingImage`, design-system brushes only (DynamicResource keys).
- Design-system brushes only (Styles/DesignSystem.xaml keys); no new colours.

## 7. Accessibility
`AutomationProperties.Name` of the tile: "<Module>, <status>. Preview: Live — <figure label value; …>, as of HH:MM" /
"Preview: last view from <time> (blurred picture)" / "Preview: illustration". The LSA verdict figure is AI output:
the tile shows "✦ AI" next to it and the AutomationName starts that figure with "AI-generated verdict: …" (AI content
labelling rule). "⋯" button: Name "Preview options for <Module>", HelpText = current mode.

## 8. Conventions / CLAUDE.md
The rule (all three modes per module) is already in CLAUDE.md + conventions (lead). The contract above is the
"how": a new module adds an `IModuleTilePreview` (Live source table row, Illustration drawing, WebView areas, sandbox).

## 9. Open questions (please decide)
1. **Contract assembly:** new public project `UFOS.ai.ModuleTiles` (referenced by main app + modules) — OK? Alternative:
   all providers in the main app (it already references every module) — simpler, but then "the module implements" is
   only nominal.
2. **Capture moment:** deferring `Closing` by ≤ 1.5 s for the async WebView2 capture — OK? Alternative: capture every
   60 s while the window is open and active (no close delay, but more captures).
3. **Transaction Log "vs budget":** which budget — (a) the sum of today's AI caps of all modules (needs their running
   backends' `/ai/budgets`; otherwise "budget: backends off"), (b) only the main app's own cap (AiBudgetAppCaps), or
   (c) a monthly figure (there is no monthly budget today)?
4. **Hedge Fund `/v2/portfolio/state`:** does it value positions with live market data / call the broker APIs (Alpaca paper,
   Binance demo — free, but external)? If yes, is that acceptable for a 30 s tile poll, or does P/H add a cheap
   `GET /v2/tiles/summary` (stored values only)?
5. **Global Monitoring `/events`:** it also calls `_refresh_stale_sources()` (free feeds, side effect). Accept, or H adds a
   pure read (`/events?refresh=0` or `/tiles/summary`)?
6. **LiveStream Agent "time of last session":** the newest verdict time, or a real session start/stop time (the backend has
   none today)?
7. **Sandbox Live figures:** show with a SANDBOX chip and never store — OK?
8. **Invalid tile_modes entry:** show the error in the tile and use Live — OK (it is visible, not silent)?
9. **System Logs:** count only the app log (all modules' AppLog), or also the backends' backend.log files?
10. **Module windows opened outside the tiles** (Portfolio Insight card → Hedge Fund, toasts): capture those too — yes?

## 10. Decisions (lead, 2026-10-24) — these override §2–§9 where they differ
1. Contract assembly: new public project `UFOS.ai.ModuleTiles` (main app + every module reference it; providers live in
   the modules).
2. Capture: NO close delay. A periodic capture every 60 s while the module window is visible AND active; at `Closing`
   one more capture only if it completes within 300 ms (otherwise the last periodic picture stays). The blur/storage
   rules of §4 are unchanged (in memory, blurred before any write).
3. Transaction Log: AI spend today + this month from the ledger. "vs budget" only when EVERY cap can be read without a
   running backend; otherwise spend only + the note "budgets: see AI budgets". No guessed totals.
4. Hedge Fund Live: `GET /v2/portfolio/summary` (P adds it: stored values only — no broker calls, no market data).
   Fields to be confirmed by P; the tile uses equity, day P&L, positions count, auto trading, halts, (sparkline points
   if the route carries them, else no sparkline).
5. Global Monitoring Live: a pure read without side effects (H adds it; name/fields from H).
6. LiveStream Agent: the newest verdict time, labelled "last verdict" (AI-labelled).
7. Sandbox Live figures: SANDBOX chip, never stored.
8. Invalid `tile_modes` entry: visible error in the tile + Live.
9. System Logs: the app log + each backend's backend.log (local files only).
10. Any window of the module is captured (also when opened outside the tile).

Order (lead): after the R2+R3 commit — (a) CHART_FULLSCREEN_TASK.md, (b) TILES code. During the doc freeze
(DOCS_SLIM_TASK.md) doc changes go to UFOS.ai/docs/tasks/PENDING_DOCS_W.md, not to SPEC / CODEMAP / todo / CLAUDE.md.

## 11. As built — phase 2 wiring (2026-10-25, cloud lead session, label tiles0 re-snapshotted + amended for the task docs)
- `*.cs.staged` → `*.cs` (git mv); `UFOS.ai.ModuleTiles` referenced by `UFOS.ai.csproj` (+ `Compile Remove`), the Hedge
  Fund, Global Monitoring and LiveStream Agent projects and `UFOS.ai.slnx`.
- `Shared/MainAppSettings.cs`: the strict loader accepts `tile_modes` (validated by `Tiles/TileModeSettings.cs`) — without
  it the first "⋯" choice would have broken the OpenRouter settings load ("unknown setting").
- `MainWindow.xaml`: `x:Name` `SentimentOptimizationTile`, `StrategyDiscoveryTile`; `MainWindow.StartTilePreviews()` at
  the end of `Window_Loaded` (a setup failure is logged, the tiles keep opening their modules).
- Hedge Fund Live (§10.4, user choice 2026-10-25): `HedgeFund/Tiles/HedgeFundTileSummary.cs` maps the route —
  **Equity** = primary-currency total (USD when present; detail "N positions · M accounts" + other currencies separately),
  **Day P&L** = total + % of the day reference (green / red), **Trading** = KILLED / HALTED (red) / kill pending (amber),
  else "auto ON|OFF" (+ "Council ON|OFF" after the switch-over; old engine autopilot in the detail while it applies);
  sparkline = paper-main's stored snapshots. Missing values: "not available" + the route's own reason (Day P&L says
  "see Equity" when equity is missing too). Sandbox = payload `mode` or `/health` → chip, never stored.
- Checks: `tests/test_tiles_static.py` (HF reads only the summary route; `tile_modes` accepted; every field the C# parser
  reads exists in the real payload — drift guard); the parser was run offline against payloads generated by the real
  route code on synthetic temp DBs (normal, no snapshot, no day reference, kill + auto, no account, two currencies +
  sandbox + halt, bad schema). No WPF build by the agent — the user builds.
