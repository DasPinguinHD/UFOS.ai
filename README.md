# UFOS.ai

> **Proof-of-concept portfolio project.** UFOS.ai is not a published product,
> not investment advice, and not intended for production/commercial use. It
> exists to demonstrate software design and engineering work (architecture,
> WPF/.NET desktop UI, Python data-collection backends, integration across
> free public APIs) — for a portfolio / résumé, not as a real financial
> trading or monitoring system. Nothing here should be relied on to make
> actual financial decisions.

UFOS.ai (FinMoS — Finance Monitoring System) is a modular WPF (.NET) desktop
app that experiments with aggregating market, geopolitical, and macro data
onto a single app shell, with each module (Global Monitoring, HedgeFund,
LiveStream Agent, …) as an independent, self-contained component with its
own Python backend where relevant.

## Modules

- **MainWindow** — the app shell: module launcher, overview cards, settings.
- **Global Monitoring System** — an interactive globe aggregating geopolitics
  (world-news RSS feeds), disasters (GDACS), notable flights (OpenSky), and
  market quotes (Yahoo Finance). See `docs/global-monitoring-system-plan.md`.
- **HedgeFund** — illustrative multi-agent trading dashboard (UI implemented,
  backend agents not yet wired to live data), plus the live **Financial
  Newsroom** submenu: insider trades (SEC EDGAR, with an AI summary) and macro
  indicators (FRED) — moved here from Global Monitoring on 2026-09-24. See
  `UFOS.ai/HedgeFund/backend/README.md`.
- **LiveStream Agent** — transcribes a live stream, cross-references market
  data, and produces an LLM-generated sentiment verdict.
- **Transaction Log** — what every AI request cost, across all modules, as
  charged by OpenRouter (`usage.cost`, 1 credit = 1 USD), newest first with
  the model's vendor, module/feature, tokens and running totals. All modules
  append to one local ledger, `%LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl`
  (one JSON object per line); nothing is uploaded. Vendor logos are the
  vendors' own favicons, fetched at runtime and cached locally.
- **Sentiment Optimization / Trading Strategy Discovery** — planned, not yet
  implemented.

Each module is independent: its own `.csproj`, its own `backend/` (Python)
where it has one, its own `.env`. The shared visual language lives in
`UFOS.ai/Styles/DesignSystem.xaml`.

## Running it

Each module with a backend has its own `backend/README.md` and
`.env.example` — copy the latter to `.env` and fill in whatever it asks for
(most data sources here are free and need no key; a couple of small
exceptions are called out in each module's README). Module windows that
have a backend start and stop it automatically when opened/closed.

Build the WPF app with `dotnet build` / open `UFOS.ai.slnx` in Visual
Studio.

## Status

Under active development, changes daily. See `docs/todo.md` for the current
task list and `docs/global-monitoring-system-plan.md` for the most
detailed example of how a module here gets planned, built, and
self-assessed.
