# TILES — module tile previews in the MainWindow (user decision 2026-10-24 19:27 Berlin)

Start AFTER R3. Scope: main app (MainWindow tiles, Shared/), every module (preview provider). New scope label `tiles0`.

## User decisions (interview 2026-10-24)
- ALL THREE preview modes are implemented, for EVERY module (rule in conventions + CLAUDE.md: a new module is not
  done until it provides all three):
  1. **Live** — 2–3 real key figures per tile (default mode).
  2. **Last view** — picture of the module window, taken when the module window closes.
  3. **Illustration** — a designed, static vector motif per module from the design system (no information).
- The user picks the mode per tile via a "⋯" button (three horizontal dots) at the bottom right of the tile → menu:
  Live · Last view · Illustration (+ "Delete last view" when one exists). Choice persisted per module in the main
  app's non-secret settings (MainAppSettings).
- Default: Live.
- Last view privacy: the picture is stored ONLY blurred/pixelated (numbers unreadable, layout recognisable). The blur
  is applied in memory before anything is written — an unblurred image never touches disk. Stored only under
  %LOCALAPPDATA%\UFOS.ai\tile-previews\ (never the repo). Sandbox mode takes no pictures.
- Live refresh: every 30 s while the main window is visible; paused while minimised/hidden.
- Live must NEVER start a backend and NEVER trigger an AI call or any paid call. When a backend is not running the
  tile shows the last stored figures with "backend off — as of HH:MM" (this is the user-accepted behaviour for the
  tile, record acceptance 2026-10-24 in the code comment); with no stored figures yet: "no data yet".

## Content per module (Live)
- Hedge Fund: paper equity + day P&L (mini sparkline), open positions count, autopilot/auto-trading + kill state.
- Global Monitoring: events last 24 h by category (coloured dots, counts).
- LiveStream Agent: last verdict + confidence, time of last session.
- Transaction Log: AI cost today / this month vs budget.
- System Logs: errors / warnings last 24 h.
- Sentiment Optimization, Strategy Discovery (not built): Live and Last view are disabled in the menu with the reason
  "module not built yet"; the tile shows the Illustration (not a silent fallback: the menu says why).
- Live figures are read from local sources only (cost ledger, logs, the module's own local status file / running
  backend's read-only endpoint with token). Define per module where each figure comes from in the contract.

## Contract first
- Write UFOS.ai/docs/tasks/TILES_API.md: the module preview interface (C#), data source per figure, storage format of
  the stored figures and the blurred picture, the blur method (strong enough that 8–10 pt numbers are unreadable),
  WebView2 capture (CoreWebView2.CapturePreviewAsync for globe/chart areas), accessibility (AutomationName of the tile
  states the mode and the figures), design-system brushes only.
- Report open questions before implementing.
