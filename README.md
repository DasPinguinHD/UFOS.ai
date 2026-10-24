# UFOS.ai

> **Proof-of-concept portfolio project — no real money involved.** UFOS.ai is a personal research and
> engineering project, not a published product and not investment advice. It trades only simulated paper /
> demo accounts; live trading is switched off in the build. If that ever changes, it would only ever be with the
> author's own funds — never client or third-party money. LLM output can be wrong, hallucinated or delayed.
> Nothing here should be relied on to make financial decisions.

UFOS.ai (FinMoS — Finance Monitoring System) is a modular WPF (.NET 10) desktop application with local Python
backends. The host application provides the shared shell (main window, design system, AI budgets, AI-cost
ledger, error reporting) and launches each module as its own window; modules with a backend start and stop it
automatically on a loopback port with a per-session token.

## Modules

| Module | Status | What it does |
|---|---|---|
| **HedgeFund — Strategy Platform** | Main development focus | Strategy research and paper trading with honest validation: declarative strategy DSL, AI-assisted Studio (idea → spec), paper → strategy replication, one backtest engine for backtest / paper / live, validation gates (ICIR, signal decay, deflated Sharpe, PBO/CSCV, robustness, capacity) and a one-time holdout, discovery loop, incubation on paper with a decay monitor, OMS with pre-/post-trade checks, kill switch and dead-man switch, Alpaca paper + Binance Spot demo brokers, Shadow Account (own depot imports, local only), Market Research charts, Financial Newsroom (SEC EDGAR, FRED, news), sandbox profile for click-tests, options-structure archive (SPY/QQQ, point-in-time) |
| **Global Monitoring** | Live | Interactive globe (WebView2): world news, disasters (GDACS), flights (OpenSky), market quotes, AI assessments |
| **LiveStream Agent** | Live | Transcribes a live stream (e.g. a Fed press conference), cross-references market data and produces a labelled AI sentiment read |
| Sentiment Optimization, Strategy Discovery | Planned / superseded | Reserved tiles; strategy discovery now lives in the HedgeFund Strategy Platform |

## Principles

- **Paper first, honest evidence.** Strategies must pass validation gates and a one-time holdout before paper
  incubation; AI strategies that cannot be backtested without look-ahead are labelled "not validated".
- **No silent fallbacks.** Missing data is recorded as a gap, never filled with stand-in values.
- **Security by default.** Secrets live only in the Windows Credential Manager (⚙ Keys & data); backends bind
  127.0.0.1 with token + Origin + Host checks; WebView2 pages run with CSP and navigation allow-lists.
- **AI transparency (EU AI Act Art. 50).** Every AI-generated output carries a visible label and a
  machine-readable provenance; AI calls are capped by per-module budgets and logged in a local cost ledger.
- **Own use only.** No public signals or distribution of recommendations (MAR Art. 20).

## Repository layout

| Path | Purpose |
|---|---|
| `UFOS.ai/` | Host WPF application, `Services/`, `Shared/` (AI content, credentials, budgets, window helpers) |
| `UFOS.ai/HedgeFund/` | HedgeFund module (WPF) + `backend/` (Python: `strategy_platform/`, `market_research/`, `newsroom/`) |
| `UFOS.ai/HedgeFund/docs/` | Code map for contributors (`CODEMAP.md`), pre-registered hypotheses, deletion lists |
| `UFOS.ai/Global Monitoring/` | Global Monitoring module (WPF + Python backend) |
| `UFOS.ai/LiveStreamAgent/` | LiveStream Agent module (WPF + Python backend) |
| `UFOS.ai/UFOS.ai.Logging/` | Shared .NET logging library |
| `docs/` | Private planning notes (git-ignored, local only) |

The detailed contract for the Strategy Platform is `UFOS.ai/HedgeFund/backend/strategy_platform/SPEC.md`.

## Prerequisites

| Requirement | Notes |
|---|---|
| Windows + .NET 10 SDK | WPF / WebView2 |
| Python 3.10+ | Backends create their own `.venv` and install pinned, hash-checked requirements |
| ffmpeg | LiveStream Agent only |

## Running it

1. Open `UFOS.ai.slnx` in Visual Studio (or `dotnet build`) and run `UFOS.ai`.
2. Enter API keys in the app: ⚙ **Keys & data** (stored in the Windows Credential Manager, never in files).
   Most data sources are free; paid / keyed ones (OpenRouter, Alpaca paper, Binance demo, Deepgram, …) are listed
   there with their licence notes.
3. Module windows start and stop their own backend automatically. Python tools must be run with the backend's
   venv: `.venv\Scripts\python.exe tools\<tool>.py`.

**Python packages:** each backend installs exactly the pinned, hash-checked `requirements.lock`
(`pip install --require-hashes`); there is no unpinned install path. After cloning, or after changing a
`requirements.txt`, create the lock once per backend with its venv (needs internet):
`.venv\Scripts\python.exe make_lockfile.py` (HedgeFund: `tools\make_lockfile.py`). Optional upgrades:
Settings (gear) → "Update Python packages", which regenerates the lock and installs it.

## Status

Under active development. Stage plan, decisions and progress are tracked in the private planning notes and in
the Strategy Platform SPEC.

## License

See [`LICENSE`](LICENSE) (MIT).
