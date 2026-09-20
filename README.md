# UFOS.ai

**UFOS.ai** is a research platform for automated market-analysis and trading experiments, built around a growing set of independent, modular agents. Each module tackles a different piece of the trading-research problem (live event analysis, multi-agent portfolio management, sentiment-driven allocation, strategy discovery) and is designed to eventually be developed, run, and swapped in or out on its own.

> ⚠️ **Research portfolio — no real money involved.** This repository is a personal research and illustration project. It does **not** trade with real capital today, and if that ever changes it will only ever be with the author's own personal funds — never client or third-party money. Nothing here is investment advice, a production trading system, or a signal you should act on. LLM output can be wrong, hallucinated, or delayed relative to live data. Treat every module in this repo as a technical demonstration, not a financial product.

---

## Table of contents

- [What is UFOS.ai](#what-is-ufosai)
- [Modules](#modules)
- [Repository layout](#repository-layout)
- [LiveStreamAgent module](#livestreamagent-module)
  - [How it works](#how-it-works)
  - [Features](#features)
  - [Architecture](#architecture)
  - [Asset categories & tickers](#asset-categories--tickers)
  - [Prerequisites](#prerequisites)
  - [Setup](#setup)
  - [Running everything together](#running-everything-together)
  - [Configuration reference](#configuration-reference)
  - [Smoke-testing the WebSocket](#smoke-testing-the-websocket)
  - [Logs & diagnostics](#logs--diagnostics)
  - [Known limitations](#known-limitations)
- [Planned modules](#planned-modules)
- [License](#license)

---

## What is UFOS.ai

The long-term goal of UFOS.ai is a single umbrella program made up of self-contained modules that share common conventions (config, logging, transport) but can otherwise be built, tested, and swapped independently — pick the module you need, plug it in, and it works alongside the others. That modular architecture is still taking shape: today there is one module with a working first version, and three more that exist as reserved slots for upcoming work.

## Modules

| Module | Folder | Status | Description |
|---|---|---|---|
| **LiveStreamAgent** | `UFOS.ai/LiveStreamAgent/` | 🟢 First working version | Listens to a live Fed press conference / FOMC stream, transcribes it in real time, and asks an LLM to turn the remarks plus live market data into a structured long/short read. Full WPF desktop client + Python backend. |
| **Multi-Agent Hedge Fund** | `UFOS.ai/HedgeFund/` | ⚪ Planned | A coordinated set of specialized trading agents intended to research and simulate a small multi-strategy fund. |
| **Market Sentiment Portfolio Optimizer** | `UFOS.ai/SentimentOptimization/` | ⚪ Planned | Portfolio allocation research driven by aggregated market/news sentiment signals. |
| **Automatic Trading Strategy Discovery Pipeline** | `UFOS.ai/TradingStrategyDiscovery/` | ⚪ Planned | An automated pipeline for generating, backtesting, and evaluating candidate trading strategies. |

The three planned modules currently exist only as empty, reserved folders (`.keep` placeholders) — see [Planned modules](#planned-modules) below.

## Repository layout

```
UFOS.ai.slnx                        # Solution file (single WPF project today)
docker-compose.yml                  # Container definition for the LiveStreamAgent backend
LICENSE
README.md

UFOS.ai/                            # WPF host application (net10.0-windows)
├── UFOS.ai.csproj
├── App.xaml(.cs)                   # App resources / global exception handling
├── AssemblyInfo.cs
│
├── LiveStreamAgent/                  # ── Module: live Fed-event analysis (first working module)
│   ├── LivestreamAgent.xaml(.cs)     # Main window: ticker grid, transcript, verdict ring, history
│   ├── WebSocketService.cs           # WS client + DTOs
│   ├── VerdictRecord.cs              # Verdict view model
│   ├── BackendLogsWindow.xaml(.cs)   # Backend log popup window
│   ├── DebugLogger.cs                # UI-side debug file logging
│   │
│   ├── backend/                      # Python backend
│   │   ├── main.py
│   │   ├── audio_stream.py
│   │   ├── transcription.py
│   │   ├── market_data.py
│   │   ├── llm_analyst.py
│   │   ├── broadcast.py
│   │   ├── logger.py
│   │   ├── config.py
│   │   ├── requirements.txt
│   │   ├── Dockerfile
│   │   ├── run_backend.ps1           # Creates venv, installs deps, launches main.py
│   │   ├── .env.example
│   │   ├── README.md                 # Backend-specific docs
│   │   └── data/
│   │       ├── logs/                 # JSONL event logs (git-ignored)
│   │       └── scenarios/            # Markdown scenario playbooks fed to the LLM (git-ignored)
│   │
│   └── tools/                        # Smoke-testing helpers
│       ├── ws_smoke.py / ws_smoke.ps1
│       ├── test_ws_client.py
│       └── run_full_smoke.ps1        # Build UI, start backend, smoke-test WS, launch UI
│
├── HedgeFund/                        # ── Module: Multi-Agent Hedge Fund (planned, placeholder only)
├── SentimentOptimization/            # ── Module: Market Sentiment Portfolio Optimizer (planned, placeholder only)
└── TradingStrategyDiscovery/         # ── Module: Automatic Trading Strategy Discovery Pipeline (planned, placeholder only)
```

---

## LiveStreamAgent module

The first working module in UFOS.ai. It listens to a live Federal Reserve press conference or FOMC statement, transcribes it in real time, and asks an LLM to interpret the remarks alongside live market data to produce a structured long/short read across key asset categories. The WPF host application displays the live transcript, market tickers, a rolling verdict history, and a confidence "ring" gauge, all fed over a WebSocket connection from the Python backend.

### How it works

```
YouTube live stream (yt-dlp)
    │
    ▼
ffmpeg  (transcodes audio to raw PCM16, mono, 16kHz)
    │
    ▼
Deepgram streaming transcription  →  finalized transcript segments
    │
    ▼
DeepSeek v3 (via OpenRouter)
    + live market snapshot (Yahoo Finance)
    + reference "scenario" playbooks
    │
    ▼
Structured JSON: long / short / neutral stance per asset category
    +
Periodic single-ticker LONG/SHORT "verdict" with a confidence score and rationale
    │
    ▼
WebSocket broadcast (ws://<host>:8765)  +  JSONL event log
    │
    ▼
UFOS.ai WPF host (live transcript, tickers, verdict ring, history, logs)
```

Market data is polled from Yahoo Finance's chart API on a fixed interval in the background and cached; each LLM call uses the latest cached snapshot rather than blocking on a fresh fetch.

### Features

- **Live transcription** of a YouTube livestream via `yt-dlp` + `ffmpeg` + Deepgram's streaming API.
- **LLM market analysis** (DeepSeek v3 via OpenRouter) that reads the rolling transcript and current prices and returns structured JSON stances per asset category.
- **Periodic "verdict" engine** that recommends a single LONG/SHORT call on a specific ticker with a confidence score and an evidence-based rationale.
- **Resilient backend**: each async task (market data, transcription, analysis, verdict loop, broadcast) is individually supervised and auto-restarts on crash with backoff.
- **WebSocket fan-out** to any number of connected clients, with typed JSON envelopes (`transcript`, `market_update`, `verdict`, `analysis`).
- **Health endpoint** (`/health` on port `8766`) so the desktop client can wait for backend readiness before connecting.
- **WPF desktop client** with:
  - Live, auto-scrolling transcript panel
  - Grouped market ticker tiles with trend arrows/colors
  - A circular confidence "ring" gauge and rolling confidence-history sparkline
  - A verdict history window (per-verdict confidence ring + rationale)
  - An in-app backend log viewer with save-to-file support
  - Auto-launch and auto-restart of the Python backend from the UI
- **JSONL event logging** of every transcript/analysis/verdict/market event for offline review.
- **Docker support** for the backend service.

### Architecture

| Layer | Tech | Responsibility |
|---|---|---|
| Audio ingestion | `yt-dlp`, `ffmpeg` | Resolve a live YouTube stream URL and transcode audio to raw PCM16 |
| Transcription | Deepgram streaming API (`nova-2`) | Convert PCM audio into finalized transcript segments |
| Market data | `aiohttp` + Yahoo Finance chart API | Poll and cache quotes for the configured ticker set |
| LLM analysis | OpenAI-compatible client → OpenRouter → DeepSeek v3 | Turn transcript + market snapshot + scenario playbooks into structured JSON |
| Transport | `websockets` server (`broadcast.py`) | Fan out typed JSON events to all connected clients |
| Orchestration | `asyncio`, supervised tasks (`main.py`) | Wire everything together; auto-restart failed subsystems |
| Desktop client | WPF (.NET, `net10.0-windows`), hosted by `UFOS.ai.csproj` | Render transcript, tickers, verdict ring/history, and backend logs |

#### Backend modules (`UFOS.ai/LiveStreamAgent/backend/`)

| File | Purpose |
|---|---|
| `main.py` | Entrypoint; wires audio → transcription → analysis → broadcast, plus the health endpoint and task supervision |
| `audio_stream.py` | Resolves the YouTube URL via `yt-dlp` and streams PCM chunks out of `ffmpeg` |
| `transcription.py` | Streams PCM to Deepgram and emits finalized transcript segments onto a queue |
| `market_data.py` | Polls Yahoo Finance's chart API per symbol on a fixed interval and exposes thread-safe snapshots |
| `llm_analyst.py` | Builds prompts from transcript + market snapshot + scenario playbooks; calls DeepSeek via OpenRouter for both the per-category analysis and the periodic verdict |
| `broadcast.py` | WebSocket server that registers/unregisters clients and fans out `{"type", "payload"}` JSON messages |
| `logger.py` | Appends every event to a daily JSONL file under `data/logs/` |
| `config.py` | Central config: env vars, ticker groupings, dataclasses for quotes/snapshots |

#### Desktop client (`UFOS.ai/LiveStreamAgent/`)

| File | Purpose |
|---|---|
| `LivestreamAgent.xaml` / `.xaml.cs` | Main window: ticker grid, transcript panel, verdict ring, confidence history, verdict-history popup, backend log popup, backend process management |
| `WebSocketService.cs` | Client-side WebSocket connection with health-check-gated retry/backoff, message parsing, and typed events |
| `VerdictRecord.cs` | Model for a single verdict entry (color/long-short classification for the UI) |
| `BackendLogsWindow.xaml` / `.xaml.cs` | Standalone backend log viewer window with refresh/save/close |
| `DebugLogger.cs` | Lightweight file logger for UI-side diagnostics (temp + repo-local log files) |
| `App.xaml` / `App.xaml.cs` (repo root of `UFOS.ai/`) | Application resources (shared scrollbar styles) and a global UI-exception handler that logs to `LiveStreamAgent/backend/data/logs/ui-errors.log` |

### Asset categories & tickers

The LLM analyst always reports on exactly these eight categories, mapped to the following proxy tickers:

| Category | Tickers |
|---|---|
| Treasury yields | `^TNX`, `^TYX` |
| Bond proxies | `TLT`, `AGG` |
| REITs | `VNQ` |
| Utilities | `XLU` |
| Homebuilders | `XHB`, `ITB` |
| Tech giants | `QQQ`, `MAGS` |
| Unprofitable growth | `ARKK` |
| Financials | `XLF` |

### Prerequisites

- **Windows** (the UI is a WPF app targeting `net10.0-windows`)
- **.NET SDK** compatible with `net10.0-windows`
- **Python 3.11+**
- **[ffmpeg](https://ffmpeg.org/)** available on `PATH`
- API keys:
  - **Deepgram** (streaming transcription)
  - **OpenRouter** (LLM access to DeepSeek v3)
- A live YouTube URL for the Fed press conference / FOMC stream you want to follow

### Setup

#### 1. Backend (Python)

```powershell
cd UFOS.ai/LiveStreamAgent/backend
cp .env.example .env      # then fill in the values below
```

Edit `.env`:

```
DEEPGRAM_API_KEY=your_deepgram_key
OPENROUTER_API_KEY=your_openrouter_key
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=deepseek/deepseek-chat-v3-0324

# Live YouTube stream/video URL of the Fed press conference or FOMC event
YOUTUBE_URL=https://www.youtube.com/watch?v=xxxxxxxx

WS_HOST=0.0.0.0
WS_PORT=8765

MARKET_DATA_POLL_SECONDS=15
```

Then either let the desktop app launch it for you (see below), or run it manually:

```powershell
cd UFOS.ai/LiveStreamAgent/backend
.\run_backend.ps1
```

`run_backend.ps1` will:
1. Skip startup if something is already listening on `127.0.0.1:8765`.
2. Create a `.venv` (unless `-RecreateVenv` is passed) and install `requirements.txt`.
3. Launch `python main.py`.

Once running, the backend exposes:
- `ws://0.0.0.0:8765` — the live event WebSocket
- `http://127.0.0.1:8766/health` — a plain-text readiness check

#### 2. Frontend (WPF / .NET)

```powershell
dotnet build UFOS.ai/UFOS.ai.csproj -c Debug
dotnet run --project UFOS.ai/UFOS.ai.csproj
```

On launch, the app's main window (`LivestreamAgent`) will:
1. Attempt to start `LiveStreamAgent/backend/run_backend.ps1` itself, if the script is found relative to the executable.
2. Poll `http://127.0.0.1:8766/health` for up to ~12 seconds.
3. Open a WebSocket connection to `ws://127.0.0.1:8765` and start rendering live data.

If the backend isn't reachable, the connection indicator turns red/error and a **Restart** button appears to retry starting the backend helper.

#### 3. Docker (backend only)

```bash
docker compose up --build
```

This is intended to build and run the backend service from `UFOS.ai/LiveStreamAgent/backend/Dockerfile`, mapping port `8765` and mounting the backend directory as a volume. You still need to supply the required environment variables (e.g. via a `.env` file referenced by `docker-compose.yml` or exported in your shell) and run the WPF client separately, pointing it at the container's WebSocket endpoint.

> **Note:** the checked-in `docker-compose.yml` still references the pre-restructure path (`./FedTrader/backend`) and has not yet been updated to `./UFOS.ai/LiveStreamAgent/backend`. Update the `build:` and `volumes:` paths in `docker-compose.yml` before running the command above — see [Known limitations](#known-limitations).

### Running everything together

`UFOS.ai/LiveStreamAgent/tools/run_full_smoke.ps1` automates a full end-to-end smoke run:

```powershell
cd UFOS.ai/LiveStreamAgent
.\tools\run_full_smoke.ps1
```

It will:
1. Stop any running `UFOS.ai` process.
2. Build the WPF project (Debug).
3. Start the backend in a hidden PowerShell window.
4. Run `tools/ws_smoke.ps1` against `ws://127.0.0.1:8765` to confirm the socket is accepting connections.
5. Launch the built `UFOS.ai.exe` if the smoke test passed.
6. Tail the last 200 lines of the WS debug log (`%TEMP%\UFOS.ai_ws_debug.log`).

### Configuration reference

All backend configuration lives in environment variables (see `LiveStreamAgent/backend/.env.example` and `LiveStreamAgent/backend/config.py`):

| Variable | Default | Description |
|---|---|---|
| `DEEPGRAM_API_KEY` | — | Deepgram streaming transcription API key |
| `OPENROUTER_API_KEY` | — | OpenRouter API key used to call DeepSeek v3 |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenRouter-compatible base URL |
| `OPENROUTER_MODEL` | `deepseek/deepseek-chat-v3-0324` | Model identifier passed to the chat completions API |
| `YOUTUBE_URL` | — | Live YouTube URL of the Fed press conference / FOMC event (required) |
| `WS_HOST` | `0.0.0.0` | WebSocket bind host |
| `WS_PORT` | `8765` | WebSocket bind port |
| `MARKET_DATA_POLL_SECONDS` | `15` | Interval between market data refreshes |
| `VERDICT_INTERVAL_SECONDS` | `90` | Interval between periodic LONG/SHORT verdicts |
| `VERDICT_MAX_REASON_CHARS` | `720` | Max characters allowed in a verdict's rationale |
| `VERDICT_CONFIDENCE_SCALE` | `100` | Scale used for the verdict confidence value |
| `VERDICT_INITIAL_DELAY_SECONDS` | `60` | Delay before the first verdict is generated, to let data accumulate |

### Smoke-testing the WebSocket

Standalone scripts under `UFOS.ai/LiveStreamAgent/tools/` let you sanity-check the WebSocket independently of the UI:

```powershell
# PowerShell wrapper
.\tools\ws_smoke.ps1 -Uri ws://127.0.0.1:8765 -Timeout 5

# Or directly
python .\tools\ws_smoke.py --uri ws://127.0.0.1:8765 --timeout 5
```

Exit codes: `0` = connected successfully, `1` = connection/send error, `2` = the `websockets` package is missing.

`tools/test_ws_client.py` is a minimal connect-and-hold variant used for quick manual checks.

### Logs & diagnostics

- **Backend event log**: `LiveStreamAgent/backend/data/logs/<YYYY-MM-DD>.jsonl` — every `transcript`, `analysis`, `verdict`, `market_update`, and `task_crash` event, one JSON object per line.
- **In-app log viewer**: the **Logs** button on the main window (and `BackendLogsWindow`) streams the backend's stdout/stderr buffer live, with **Refresh**, **Save**, and **Close** actions.
- **UI debug log**: `%TEMP%\UFOS.ai_ws_debug.log` and a repo-local `UFOS.ai_ws_debug_repo.log`, written by `DebugLogger` and `WebSocketService` for connection-level diagnostics.
- **UI exception log**: `LiveStreamAgent/backend/data/logs/ui-errors.log`, written by `App.ShowUiException` whenever an unhandled UI error is caught.

### Known limitations

- Market data relies solely on Yahoo Finance's chart API with no fallback source; if Yahoo changes its response shape or blocks requests, quotes for the affected tickers will silently go stale (check the logs for `Failed to fetch quote` warnings).
- Transcription quality depends entirely on Deepgram's handling of the live audio feed (accents, cross-talk, and audio dropouts will degrade segment quality).
- LLM output is only as good as the prompt and the reference scenario playbooks in `backend/data/scenarios/`; it is not guaranteed to be accurate, and its JSON output is defensively parsed but can still fail validation.
- The WPF client and backend communicate over unauthenticated, unencrypted local WebSocket/HTTP — this module is designed for local/single-user use, not for exposing over an untrusted network as-is.
- `docker-compose.yml` still points at the pre-restructure `./FedTrader/backend` path and needs to be updated to `./UFOS.ai/LiveStreamAgent/backend` before the Docker workflow will work as described above.

---

## Planned modules

The following folders are reserved for upcoming modules and currently contain no implementation beyond a `.keep` placeholder:

- **`UFOS.ai/HedgeFund/`** — Multi-Agent Hedge Fund: a set of coordinated, specialized agents intended to research a small multi-strategy fund model.
- **`UFOS.ai/SentimentOptimization/`** — Market Sentiment Portfolio Optimizer: portfolio allocation research driven by aggregated sentiment signals.
- **`UFOS.ai/TradingStrategyDiscovery/`** — Automatic Trading Strategy Discovery Pipeline: automated generation, backtesting, and evaluation of candidate trading strategies.

As UFOS.ai's modular architecture matures, each of these is expected to become an independently runnable module alongside LiveStreamAgent, sharing common conventions where it makes sense but not depending on the others to function.

## License

See [`LICENSE`](LICENSE) (MIT).
