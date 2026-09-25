# UFOS.ai

> **Proof-of-concept portfolio project.** UFOS.ai is not a published product,
> not investment advice, and not intended for production/commercial use. It
> exists to demonstrate software design and engineering work (architecture,
> WPF/.NET desktop UI, Python data-collection backends, integration across
> free public APIs) — for a portfolio / résumé, not as a real financial
> trading or monitoring system. Nothing here should be relied on to make
> actual financial decisions.

UFOS.ai (FinMoS — Finance Monitoring System) is a modular WPF (.NET 10)
desktop application that aggregates market, geopolitical, and macroeconomic
data into a single app shell. `UFOS.ai` acts as the host application: it
provides the shared shell (main window, design system, AI-cost ledger,
shared error reporting) and launches each module as an independent window.
Every module is a self-contained project with its own `.csproj` and,
where relevant, its own Python backend.

## Architecture

```
UFOS.ai.slnx
├── UFOS.ai/                     Host application (WPF, net10.0-windows)
│   ├── MainWindow               Module launcher and overview
│   ├── Styles/DesignSystem.xaml Shared design tokens, templates, brushes
│   ├── Services/                AI cost ledger, market quotes, news feed,
│   │                            OpenRouter client, vendor logos
│   ├── Shared/                  AI-disclosure helpers (EU AI Act Art. 50)
│   └── TransactionLogWindow     Cross-module AI-cost log viewer
│
├── UFOS.ai/Global Monitoring/    Module: interactive globe (WPF + WebView2)
│   └── backend/                 Python collectors + WebSocket/HTTP API
│
├── UFOS.ai/HedgeFund/            Module: multi-agent trading dashboard
│   ├── Newsroom/                Financial Newsroom sub-window (WPF)
│   └── backend/                 Python: newsroom collectors + API
│
├── UFOS.ai/LiveStreamAgent/      Module: live-stream sentiment analysis
│   └── backend/                 Python: transcription + LLM pipeline
│
└── UFOS.ai/UFOS.ai.Logging/      Shared logging library (net10.0)
```

Each WPF module window is created directly by the host
(`UFOS.ai/MainWindow.xaml.cs`) and shares the host's `IUiErrorReporter`
implementation for consistent error logging. Modules with a Python backend
start and stop that backend as a subprocess automatically when their window
opens or closes.

## Modules

| Module | Status | UI | Backend |
|---|---|---|---|
| Global Monitoring | Live | Interactive globe (WebView2), geopolitics, disasters, flights, market quotes | Python: world-news RSS, GDACS, OpenSky, Yahoo Finance collectors |
| HedgeFund | Dashboard illustrative; Financial Newsroom live | Multi-agent trading dashboard, Financial Newsroom sub-window | Python: SEC EDGAR insider trades, FRED macro indicators |
| LiveStream Agent | Live | Transcript view, market cross-reference, sentiment verdict | Python: yt-dlp + ffmpeg + Deepgram transcription, DeepSeek v3 (via OpenRouter) analysis |
| Sentiment Optimization | Planned | Not yet implemented | Not yet implemented |
| Trading Strategy Discovery | Planned | Not yet implemented | Not yet implemented |

### Global Monitoring System

An interactive globe aggregating geopolitics (world-news RSS feeds),
disasters (GDACS), notable flights (OpenSky), and market quotes (Yahoo
Finance). None of these sources require an API key. See
`docs/global-monitoring-system-plan.md` for the detailed design and
build history.

### HedgeFund

An illustrative multi-agent trading dashboard (UI implemented, backend
agents not yet wired to live data; paper trading only, no real trades),
plus the live **Financial Newsroom** section:

| Section | Data source | Refresh |
|---|---|---|
| Insider Trades | SEC EDGAR Form 4 filings, with an optional AI summary | Every 120s (default) |
| Macro Indicators | FRED (Fed funds rate, Treasury yields, CPI, core PCE, unemployment, initial claims, HY spread, VIX) | Every 6h (default) |

See `UFOS.ai/HedgeFund/backend/README.md` for endpoint and configuration
details.

### LiveStream Agent

Transcribes a live stream (for example, a Federal Reserve press
conference), cross-references live market data, and asks an LLM to produce
a structured long/short/neutral sentiment verdict per asset category
(Treasury yields, bond proxies, REITs, utilities, homebuilders, tech
giants, unprofitable growth, financials). See
`UFOS.ai/LiveStreamAgent/backend/README.md`.

### Transaction Log

Records what every AI request across all modules cost, as billed by
OpenRouter (`usage.cost`, 1 credit = 1 USD): newest first, with the
model's vendor, originating module/feature, token counts, and running
totals. All modules append to one local ledger,
`%LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl` (one JSON object per line);
nothing is uploaded elsewhere. Vendor logos are the vendors' own favicons,
fetched at runtime and cached locally.

## AI-generated content disclosure

Every AI-generated output in the UI (for example, the Financial Newsroom's
"Sum up most notable" button) is labelled per the EU AI Act, Article 50:
a visible badge, a disclosure line, and a machine-readable `provenance`
object are attached to any LLM-produced text. See
`UFOS.ai/Shared/AiContent.cs` for the shared implementation.

## Prerequisites

| Requirement | Notes |
|---|---|
| .NET 10 SDK | All WPF projects target `net10.0-windows`; the shared logging library targets `net10.0` |
| Windows | WPF and WebView2 dependencies require Windows |
| Python 3.10+ | Required for modules with a backend (Global Monitoring, HedgeFund, LiveStream Agent) |
| ffmpeg | Required by the LiveStream Agent backend for audio transcoding |

## Running it

1. Open `UFOS.ai.slnx` in Visual Studio, or build from the command line
   with `dotnet build`.
2. For each module with a backend, copy its `backend/.env.example` to
   `backend/.env` and fill in the required values (most data sources are
   free and need no key; exceptions are documented in each module's
   `backend/README.md`).
3. Run the host application (`UFOS.ai`). Module windows start and stop
   their own backend process automatically when opened and closed.

## Repository layout

| Path | Purpose |
|---|---|
| `UFOS.ai/` | Host WPF application and shared services |
| `UFOS.ai/Global Monitoring/` | Global Monitoring module (WPF + Python backend) |
| `UFOS.ai/HedgeFund/` | HedgeFund module, including the Financial Newsroom |
| `UFOS.ai/LiveStreamAgent/` | LiveStream Agent module (WPF + Python backend) |
| `UFOS.ai/UFOS.ai.Logging/` | Shared .NET logging library |
| `docs/` | Design notes and planning documents (git-ignored, local only) |

## Status

Under active development, with frequent changes. See
`docs/global-monitoring-system-plan.md` for the most detailed example of
how a module in this repository is planned, built, and self-assessed.
