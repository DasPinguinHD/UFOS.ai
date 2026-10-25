# MR-FS — Research chart: true full screen (user request 2026-10-24 20:38 Berlin)

Owner W (Market Research WPF + chart bridge). After the R2+R3 commit; small, can go before TILES code.

- A "⛶" button in the chart toolbar (tooltip "Full screen (Esc to exit)") and F11 while the chart has focus.
- TRUE full screen: chart + drawing tools + timeframe row + chart type + indicators/objects/markers fill the whole
  monitor (no window frame, no taskbar), on the monitor the window is on. Symbol, last price and change stay
  visible small at the top; a small "Exit full screen" control top right.
- Esc or the button exits and restores the previous window state exactly (normal/maximized, position).
- Same chart instance: drawings, indicators, zoom and selected timeframe are kept — no reload, no second WebView
  (move the host or use a borderless topmost window that re-parents the chart host; state must survive).
- Works from the Research tab and from a popped-out symbol window. Live updates continue.
- Follow the borderless-window rules (work area vs full monitor: here the FULL monitor incl. taskbar area).
- Accessibility: button AutomationName; Esc documented in the tooltip.

## Extended 2026-10-24 20:39 (user): general rule + six more places
Rule (CLAUDE.md + conventions): every chart, map and large table gets the "⛶" full-screen control, Esc exits,
same behaviour everywhere. Build ONE shared building block in `Shared/` (e.g. FullScreenHost: re-parents a host
element into a borderless full-monitor window and back, keeps state, Esc/F11, exit control, AutomationName) and
use it everywhere; no per-module copies.
Charts/maps → true full screen; dense tables → the same control fills the monitor ("focus mode").
Order: 1) Research chart (first user of the block) 2) Global Monitoring globe (layer toggles as a floating menu,
event feed collapsible) 3) Backtest report (Library → Open report: equity, drawdown, distribution) 4) Newsroom
macro charts 5) Overview equity curve 6) Discovery Loop population/score charts 7) dense tables: Transaction Log,
System Logs, Orders, later Disclosures/Track record.
