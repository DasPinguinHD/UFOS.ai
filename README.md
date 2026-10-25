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

## Data sources & licensing

UFOS.ai ships no market data or other third-party data: every feed is fetched at runtime with the user's own accounts and keys (stored in the Windows Credential Manager) or from keyless public endpoints.
Each user is responsible for complying with each provider's terms, especially for any commercial use; the author uses UFOS.ai for personal research only, and the MIT license covers the code, not any data it retrieves.

| Source | Used for | Provider terms (summary) | Key needed | Link |
|---|---|---|---|---|
| Alpaca (Basic/free data plan, Paper trading) | IEX real-time quotes, 15-min-delayed historical bars, Benzinga news, corporate actions; paper orders | API data may not be redistributed; news is provided by Benzinga | own account | [redistribution FAQ](https://alpaca.markets/support/redistribute-alpaca-api) · [disclosures](https://alpaca.markets/disclosures) |
| Tiingo (Starter/free) | End-of-day prices before 2016-01-04 | "Internal use only": your own personal use; no display or sharing with others | own account | [pricing & license](https://www.tiingo.com/about/pricing) |
| EODHD (free plan) | Daily history for non-US listings (Shadow Account) | Non-professional = personal use only; no sharing, resale or redistribution | own account | [terms](https://eodhd.com/financial-apis/terms-conditions) |
| SEC EDGAR | Form 4 filings, submissions JSON, Insider Transactions Data Sets | Public; may be copied and redistributed; fair access ≤ 10 req/s with a declared User-Agent | none (User-Agent with contact e-mail) | [fair access](https://www.sec.gov/os/accessing-edgar-data) · [privacy/dissemination](https://www.sec.gov/privacy) |
| FRED (St. Louis Fed) | Macro series | Attribution required; copyrighted series need the owner's permission for anything beyond personal use | own API key | [legal](https://fred.stlouisfed.org/legal/) · [API terms](https://fred.stlouisfed.org/docs/api/terms_of_use.html) |
| GDACS (EU JRC / UN) | Disaster alerts | Provided "as is"; automatic alerts, not a substitute for official warnings; reuse terms: see provider terms | none | [terms of use](https://gdacs.org/About/termofuse.aspx) |
| OpenSky Network | Live flight positions | API is for research and non-commercial purposes; commercial use needs a separate agreement | none (anonymous) or own account | [API docs & terms](https://openskynetwork.github.io/opensky-api/) |
| Yahoo Finance | Unofficial public chart endpoint, RSS headlines | Yahoo ToS bar commercial reuse and automated collection without permission; unofficial, may break | none | [Yahoo ToS](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html) |
| MarketWatch / Dow Jones RSS | Headlines shown with link to original | See provider terms | none | [Dow Jones terms](https://www.dowjones.com/terms-of-use/) |
| Natural Earth via `world-atlas` (vendored `land-110m.json`) | World map background | Data public domain; `world-atlas` packaging ISC | none | [Natural Earth terms](https://www.naturalearthdata.com/about/terms-of-use/) · [world-atlas](https://github.com/topojson/world-atlas) |
| KLineCharts (vendored) | Chart engine | Apache-2.0; LICENSE + NOTICE kept with the vendored copy | none | [repo](https://github.com/klinecharts/KLineChart) |
| Binance Spot Demo (via ccxt) | Demo trading (virtual funds) | Binance Demo Mode / Product Terms of Use apply | own demo key | [demo terms](https://developers.binance.com/docs/binance-spot-api-docs/demo-mode/DEMO-TERMS-OF-USE) |
| kadoa-org/congress-trading-monitor | STOCK Act congressional trades (public GitHub JSON) | Code MIT; data "for research and educational purposes"; underlying filings restricted by statute (see Notes) | none | [repo](https://github.com/kadoa-org/congress-trading-monitor) |
| OpenRouter | LLM API (pay-per-use service, not data) | OpenRouter ToS plus each model provider's terms | own key | [terms](https://openrouter.ai/terms) |
| Deepgram | Speech-to-text (service, not data) | Deepgram ToS | own key | [terms](https://deepgram.com/terms) |
| YouTube (via yt-dlp) — **being replaced** | LiveStream Agent currently transcribes a user-supplied live stream URL; planned switch to official sources that permit it (owner decision 2026-10-25) | YouTube ToS bar downloading and automated access unless authorized by YouTube | none | [YouTube ToS](https://www.youtube.com/static?gl=EN&template=terms) |

**Notes**

- **OpenSky:** the API is for research and non-commercial purposes only. Commercial users must contact OpenSky. Publications should cite Schäfer et al., "Bringing Up OpenSky" (IPSN 2014).
- **Tiingo / EODHD / Alpaca:** free tiers are for personal use only. Do not share accounts, display data to others, or redistribute data, including bundling it into a fork or a hosted service.
- **Yahoo Finance:** UFOS.ai uses an unofficial, undocumented endpoint. It is not affiliated with or endorsed by Yahoo, it may stop working at any time, and Yahoo's ToS restrict automated collection.
- **Congressional trades:** under 5 U.S.C. § 13107(c) (Ethics in Government Act), financial disclosure reports may not be obtained or used for any commercial purpose (news media excepted), for credit ratings, or for soliciting money. See [Senate eFD](https://efdsearch.senate.gov/search/) and [OGE](https://www.oge.gov/web/OGE.nsf/Officials%20Individual%20Disclosures%20Search%20Collection).
- **FRED:** cite as "Source: FRED" or "Source: <original source> via FRED", and keep any copyright notices. Copyrighted series (marked "Copyright" in the series notes) need the owner's permission for anything beyond personal use. *This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.*
- **SEC EDGAR:** enter your own name and e-mail as the SEC User-Agent in ⚙ Keys & data, and stay under 10 requests per second.
- **YouTube:** downloading via yt-dlp is not authorized by YouTube's ToS; the LiveStream Agent is being moved to official sources that permit programmatic access. Until then, only transcribe streams you are entitled to process.

**Repository rule:** no third-party data is ever committed (prices, news text, filings exports, databases, caches) — test data is synthetic, and `HedgeFund/tools/check_repo_data.py` checks every commit.

## License

See [`LICENSE`](LICENSE) (MIT).
