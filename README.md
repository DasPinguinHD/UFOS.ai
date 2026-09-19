# FedTrader

**FedTrader** is a real-time trading-signal assistant that listens to a live Federal Reserve press conference or FOMC statement, transcribes it as it happens, and asks an LLM to interpret the Chair's remarks — alongside live market data — into a structured long/short read across key asset categories. A WPF desktop app displays the live transcript, market tickers, a rolling verdict history, and a confidence "ring" gauge, all fed over a WebSocket connection from a Python backend.

> ⚠️ **Disclaimer:** This project is an experimental research/engineering tool. It does **not** constitute financial advice. LLM output can be wrong, hallucinated, or delayed relative to the live audio. Do not use it as the sole basis for real trading decisions.

---

## Table of contents

- [How it works](#how-it-works)
- [Features](#features)
- [Architecture](#architecture)
- [Project structure](#project-structure)
- [Asset categories & tickers](#asset-categories--tickers)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
  - [1. Backend (Python)](#1-backend-python)
  - [2. Frontend (WPF / .NET)](#2-frontend-wpf--net)
  - [3. Docker (backend only)](#3-docker-backend-only)
- [Running everything together](#running-everything-together)
- [Configuration reference](#configuration-reference)
- [Smoke-testing the WebSocket](#smoke-testing-the-websocket)
- [Logs & diagnostics](#logs--diagnostics)
- [Known limitations](#known-limitations)
- [License](#license)

---

## How it works

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
FedTrader WPF desktop app (live transcript, tickers, verdict ring, history, logs)
```

Market data is polled from Yahoo Finance's chart API on a fixed interval in the background and cached; each LLM call uses the latest cached snapshot rather than blocking on a fresh fetch.

## Features

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

## Architecture

| Layer | Tech | Responsibility |
|---|---|---|
| Audio ingestion | `yt-dlp`, `ffmpeg` | Resolve a live YouTube stream URL and transcode audio to raw PCM16 |
| Transcription | Deepgram streaming API (`nova-2`) | Convert PCM audio into finalized transcript segments |
| Market data | `aiohttp` + Yahoo Finance chart API | Poll and cache quotes for the configured ticker set |
| LLM analysis | OpenAI-compatible client → OpenRouter → DeepSeek v3 | Turn transcript + market snapshot + scenario playbooks into structured JSON |
| Transport | `websockets` server (`broadcast.py`) | Fan out typed JSON events to all connected clients |
| Orchestration | `asyncio`, supervised tasks (`main.py`) | Wire everything together; auto-restart failed subsystems |
| Desktop client | WPF (.NET, `net10.0-windows`) | Render transcript, tickers, verdict ring/history, and backend logs |

### Backend modules (`FedTrader/backend/`)

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

### Desktop client (`FedTrader/`)

| File | Purpose |
|---|---|
| `MainWindow.xaml` / `.xaml.cs` | Main window: ticker grid, transcript panel, verdict ring, confidence history, verdict-history popup, backend log popup, backend process management |
| `WebSocketService.cs` | Client-side WebSocket connection with health-check-gated retry/backoff, message parsing, and typed events |
| `VerdictRecord.cs` | Model for a single verdict entry (color/long-short classification for the UI) |
| `BackendLogsWindow.xaml` / `.xaml.cs` | Standalone backend log viewer window with refresh/save/close |
| `DebugLogger.cs` | Lightweight file logger for UI-side diagnostics (temp + repo-local log files) |
| `App.xaml` / `App.xaml.cs` | Application resources (shared scrollbar styles) and a global UI-exception handler that logs to `backend/data/logs/ui-errors.log` |

## Project structure

```
FedTrader.slnx                     # Solution file
docker-compose.yml                 # Runs the backend container
remove_venv_cached.ps1             # One-off helper to untrack backend/.venv from git

FedTrader/
├── FedTrader.csproj                # WPF app project (net10.0-windows)
├── App.xaml(.cs)                   # App resources / global exception handling
├── MainWindow.xaml(.cs)            # Main UI
├── BackendLogsWindow.xaml(.cs)     # Backend log popup window
├── WebSocketService.cs             # WS client + DTOs
├── VerdictRecord.cs                # Verdict view model
├── DebugLogger.cs                  # UI-side debug file logging
├── AssemblyInfo.cs
│
├── backend/                        # Python backend ("Fed Agent")
│   ├── main.py
│   ├── audio_stream.py
│   ├── transcription.py
│   ├── market_data.py
│   ├── llm_analyst.py
│   ├── broadcast.py
│   ├── logger.py
│   ├── config.py
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── run_backend.ps1             # Creates venv, installs deps, launches main.py
│   ├── .env.example
│   ├── README.md                   # Backend-specific docs
│   └── data/
│       ├── logs/                   # JSONL event logs (git-ignored)
│       └── scenarios/              # Markdown scenario playbooks fed to the LLM (git-ignored)
│
├── tools/                          # Smoke-testing helpers
│   ├── ws_smoke.py / ws_smoke.ps1
│   ├── test_ws_client.py
│   └── run_full_smoke.ps1          # Build UI, start backend, smoke-test WS, launch UI
│
└── test_old_2026-09-18/            # Archived copies of the old tools/ scripts
```

## Asset categories & tickers

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

## Prerequisites

- **Windows** (the UI is a WPF app targeting `net10.0-windows`)
- **.NET SDK** compatible with `net10.0-windows`
- **Python 3.11+**
- **[ffmpeg](https://ffmpeg.org/)** available on `PATH`
- API keys:
  - **Deepgram** (streaming transcription)
  - **OpenRouter** (LLM access to DeepSeek v3)
- A live YouTube URL for the Fed press conference / FOMC stream you want to follow

## Setup

### 1. Backend (Python)

```powershell
cd FedTrader/backend
cp .env.example .env      # then fill in the values below
```

Edit `.env`:

```
DEEPGRAM_API_KEY=your_deepgram_key
OPENROUTER_API_KEY=your_openrouter_key
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=deepseek/deepseek-chat-v3-0324

YOUTUBE_URL=https://www.youtube.com/watch?v=xxxxxxxx

WS_HOST=0.0.0.0
WS_PORT=8765

MARKET_DATA_POLL_SECONDS=15
```

Then either let the desktop app launch it for you (see below), or run it manually:

```powershell
cd FedTrader/backend
.\run_backend.ps1
```

`run_backend.ps1` will:
1. Skip startup if something is already listening on `127.0.0.1:8765`.
2. Create a `.venv` (unless `-RecreateVenv` is passed) and install `requirements.txt`.
3. Launch `python main.py`.

Once running, the backend exposes:
- `ws://0.0.0.0:8765` — the live event WebSocket
- `http://127.0.0.1:8766/health` — a plain-text readiness check

### 2. Frontend (WPF / .NET)

```powershell
dotnet build FedTrader/FedTrader.csproj -c Debug
dotnet run --project FedTrader/FedTrader.csproj
```

On launch, `MainWindow` will:
1. Attempt to start `backend/run_backend.ps1` itself (via `TryStartBackendHelper`), if the script is found relative to the executable.
2. Poll `http://127.0.0.1:8766/health` for up to ~12 seconds.
3. Open a WebSocket connection to `ws://127.0.0.1:8765` and start rendering live data.

If the backend isn't reachable, the connection indicator turns red/error and a **Restart** button appears to retry starting the backend helper.

### 3. Docker (backend only)

```bash
docker compose up --build
```

This builds and runs the backend service from `FedTrader/backend/Dockerfile`, mapping port `8765` and mounting the backend directory as a volume. You still need to supply the required environment variables (e.g. via a `.env` file referenced by `docker-compose.yml` or exported in your shell) and run the WPF client separately, pointing it at the container's WebSocket endpoint.

## Running everything together

`FedTrader/tools/run_full_smoke.ps1` automates a full end-to-end smoke run:

```powershell
cd FedTrader
.\tools\run_full_smoke.ps1
```

It will:
1. Stop any running `FedTrader` process.
2. Build the WPF project (Debug).
3. Start the backend in a hidden PowerShell window.
4. Run `tools/ws_smoke.ps1` against `ws://127.0.0.1:8765` to confirm the socket is accepting connections.
5. Launch the built `FedTrader.exe` if the smoke test passed.
6. Tail the last 200 lines of the WS debug log (`%TEMP%\fedtrader_ws_debug.log`).

## Configuration reference

All backend configuration lives in environment variables (see `backend/.env.example` and `backend/config.py`):

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

## Smoke-testing the WebSocket

Standalone scripts under `FedTrader/tools/` let you sanity-check the WebSocket independently of the UI:

```powershell
# PowerShell wrapper
.\tools\ws_smoke.ps1 -Uri ws://127.0.0.1:8765 -Timeout 5

# Or directly
python .\tools\ws_smoke.py --uri ws://127.0.0.1:8765 --timeout 5
```

Exit codes: `0` = connected successfully, `1` = connection/send error, `2` = the `websockets` package is missing.

`tools/test_ws_client.py` is a minimal connect-and-hold variant used for quick manual checks.

> Older copies of these scripts are preserved under `FedTrader/test_old_2026-09-18/` for reference; the live versions to use are the ones in `FedTrader/tools/`.

## Logs & diagnostics

- **Backend event log**: `backend/data/logs/<YYYY-MM-DD>.jsonl` — every `transcript`, `analysis`, `verdict`, `market_update`, and `task_crash` event, one JSON object per line.
- **In-app log viewer**: the **Logs** button on the main window (and `BackendLogsWindow`) streams the backend's stdout/stderr buffer live, with **Refresh**, **Save**, and **Close** actions.
- **UI debug log**: `%TEMP%\fedtrader_ws_debug.log` and a repo-local `fedtrader_ws_debug_repo.log`, written by `DebugLogger` and `WebSocketService` for connection-level diagnostics.
- **UI exception log**: `backend/data/logs/ui-errors.log`, written by `App.ShowUiException` whenever an unhandled UI error is caught.

## Known limitations

- Market data relies solely on Yahoo Finance's chart API with no fallback source; if Yahoo changes its response shape or blocks requests, quotes for the affected tickers will silently go stale (check the logs for `Failed to fetch quote` warnings).
- Transcription quality depends entirely on Deepgram's handling of the live audio feed (accents, cross-talk, and audio dropouts will degrade segment quality).
- LLM output is only as good as the prompt and the reference scenario playbooks in `backend/data/scenarios/`; it is not guaranteed to be accurate, and its JSON output is defensively parsed but can still fail validation.
- The WPF client and backend communicate over unauthenticated, unencrypted local WebSocket/HTTP — this project is designed for local/single-user use, not for exposing over an untrusted network as-is.

## License

No license file is currently included in this repository. Add a `LICENSE` file to clarify usage terms before distributing or open-sourcing this project.
