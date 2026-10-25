# Task and contract files

Open work packages and their contracts, moved here from the lead session's scratchpad on 2026-10-25 so that any
session (local or cloud) can pick them up. Priorities live in `UFOS.ai/docs/todo.md`; rules in `CLAUDE.md`.

| File | What | Status (2026-10-25) |
|---|---|---|
| `AGENT_BRIEF.md` | Common brief for sub-agents (access, tests, offload) | reference |
| `TILES_TASK.md`, `TILES_API.md` | MainWindow tile previews (Live / blurred last view / illustration, "⋯") | phase 1 files staged (`*.cs.staged`), wiring pending; needs `GET /v2/portfolio/summary` |
| `CHART_FULLSCREEN_TASK.md` | "⛶" full screen for charts, maps, tables (shared `Shared/FullScreenHost.cs`) | built (static checks), click-test pending |
| `INSIDER_RETURNS_TASK.md`, `INSIDER_RET_API.md` | Return after the trade for insider filings + 2-year Form 4 backfill | backend in progress |
| `CONGRESS_TASK.md`, `CONGRESS_API.md` | Congress trades (STOCK Act) via kadoa repo JSON, "Disclosures" section | contract accepted, after INSIDER-RET |
| `GM_SUMMARY_API.md` | Global Monitoring `GET /events/summary` for the tile | built |
| `PORTFOLIO_SUMMARY_API.md` | `GET /v2/portfolio/summary` for the Hedge Fund tile (SP §17.15) | built 2026-10-25 (P); tile wiring W |
| `LSA_SOURCES_TASK.md` | LiveStream Agent: official sources instead of YouTube; measure / show / fix transcript gaps | open, interview first |
| `SWITCHOVER_FIX_API.md` | Council switch-over retry / abort / review fixes (SP §22.14) | built |
| `COUNCIL_DIFFS_FOR_USER.md` | The 9 deliberate Council differences the user approved 2026-10-24 | reference |
| `LOCK_API.md` | Pinned, hash-checked Python dependencies (`make_lockfile.py`) | built |
| `ENV_API.md` | Credential Manager only, `.env` retired, settings.json | built |
