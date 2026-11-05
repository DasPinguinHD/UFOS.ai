# Task and contract files

Open work packages and their contracts, moved here from the lead session's scratchpad on 2026-10-25 so that any
session (local or cloud) can pick them up. Priorities live in `UFOS.ai/docs/todo.md`; rules in `CLAUDE.md`.

| File | What | Status (2026-10-28) |
|---|---|---|
| `AGENT_BRIEF.md` | Common brief for sub-agents (access, tests, offload) | reference |
| `RISKLAB_V01_TASK.md` | Risk Lab v0.1 skeleton — standalone package `UFOS.ai/RiskLab/` (career focus, todo §0) | skeleton built 2026-10-28 (H): the sheet-1 stubs are implemented in v0.2; lockfile + CI copy to `.github/workflows/` pending (user) |
| `RISKLAB_V02_TASK.md` | Risk Lab v0.2 — HS / normal / t VaR + ES, true VaR / ES of the synthetic data, Kupiec / Christoffersen / traffic light, size-power study `studies/var_backtest_power.py` | built 2026-11-05 (H); review fixes M1/M2 + minor applied (risklab3); full-run summary in `RiskLab/studies/results/`; lockfile + CI copy still open (user) |
| `TILES_TASK.md`, `TILES_API.md` | MainWindow tile previews (Live / blurred last view / illustration, "⋯") | wired 2026-10-25 (incl. Hedge Fund Live via `GET /v2/portfolio/summary`); dark "⋯" menu 2026-10-28; open: grey tile strip decision, user build + click-test |
| `CHART_FULLSCREEN_TASK.md` | "⛶" full screen for charts, maps, tables (shared `Shared/FullScreenHost.cs`) | built (static checks), click-test pending |
| `INSIDER_RETURNS_TASK.md`, `INSIDER_RET_API.md` | Return after the trade for insider filings + 2-year Form 4 backfill | backend + Disclosures UI built (SP §24); user click-test pending |
| `CONGRESS_TASK.md`, `CONGRESS_API.md` | Congress trades (STOCK Act) via kadoa repo JSON, "Disclosures" section | backend a/b/c applied 2026-10-28; Congress tab built (W, 2026-10-28); user build + click-test pending; watch-list UI next |
| `GM_SUMMARY_API.md` | Global Monitoring `GET /events/summary` for the tile | built |
| `PORTFOLIO_SUMMARY_API.md` | `GET /v2/portfolio/summary` for the Hedge Fund tile (SP §17.15) | built 2026-10-25 (P); wired into the tile 2026-10-25 |
| `LSA_SOURCES_TASK.md` | LiveStream Agent: official sources instead of YouTube; measure / show / fix transcript gaps | PARKED 2026-10-28 (career focus) |
| `SWITCHOVER_FIX_API.md` | Council switch-over retry / abort / review fixes (SP §22.14) | built |
| `COUNCIL_DIFFS_FOR_USER.md` | The 9 deliberate Council differences the user approved 2026-10-24 | reference |
| `LOCK_API.md` | Pinned, hash-checked Python dependencies (`make_lockfile.py`) | built |
| `ENV_API.md` | Credential Manager only, `.env` retired, settings.json | built |
