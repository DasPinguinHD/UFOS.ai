# Strategy Platform SPEC — archive: sandbox-profile

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## Sandbox profile (contract, user decision 2026-10-23)

Purpose: click-test the incubation / monitoring flow while the user's real databases stay clean. No strategy has
passed validation + holdout yet; the holdout is never peeked at and no evidence is faked in the real DB.
Code: `sandbox_profile.py` (mode, paths, guards), `tools/sandbox.py` (CLI), `sandbox_fixtures.py` (seeding,
force-decay), `strategy_platform/core/fixture.py` (labels). Tests: `tests/test_sandbox_profile.py`.

- **S1 Activation by tool, never by UI** (run from `HedgeFund\backend`: `.venv\Scripts\python.exe tools\sandbox.py …`):
  - `on`: refused while the backend answers `GET /health` on 127.0.0.1:`HEALTH_PORT` ("close UFOS.ai first"; an
    unclear answer — timeout, odd error — is also a refusal). Copies hedgefund.db, newsroom.db, market_research.db
    (those present; hedgefund.db required) into `data/sandbox/` with the SQLite online backup API (source opened
    read-only, copy `PRAGMA quick_check`ed, then renamed into place) plus `data/papers/` if present. An earlier
    sandbox copy is renamed `*.<UTC stamp>.stale`, never deleted. Then writes the marker `data/SANDBOX_ACTIVE`
    (JSON: `mode`, `created_at`, per file `source_sha256`, `source_wal_sha256`, `source_bytes`, `copy_sha256`).
    A real file that changes while it is copied aborts `on` (no marker).
  - Then `on` seeds the incubation fixture inside the sandbox (user 2026-10-24; same code path as
    `seed-incubation-test`, S4) and prints its name, id and state.
  - If seeding fails, `on` says so with the reason and exits 4. The sandbox stays ON; the user runs
    `seed-incubation-test` after fixing the cause. Seeding is never skipped silently.
  - `seed-incubation-test` stays as a separate command for re-seeding.
  - Venv guard (2026-10-24): the tool refuses to run unless `sys.prefix` is `HedgeFund\backend\.venv` ("run with
    .venv\Scripts\python.exe tools\sandbox.py …"). The system Python lacks the backend's packages, e.g. tzdata:
    "No time zone found with key America/New_York".
  - `off`: refused while the backend runs. Re-hashes the real files and reports per file whether they are identical
    to `on` (exit 0 identical, 3 changed = the real backend or another program wrote them in between); renames the
    marker to `SANDBOX_ACTIVE.<stamp>.stale` (the result is written into it); `data/sandbox/` is kept.
  - An EMPTY `-wal` and an absent `-wal` are the same database state (2026-10-24). `-shm` is never compared.
  - The read-only source connection of `on` creates an empty `-wal` / `-shm` pair. Any later ordinary open + close
    of the real file by any SQLite client (even a pure read) removes the pair without changing data.
  - Such a pair is reported as `note: "wal: empty → absent (no data change)"` (or "absent → empty"); markers
    written before this change (an empty-file hash) compare the same way. A non-empty WAL difference stays a
    change.
  - `status`: mode, backend running, marker summary, real files unchanged so far.
- **S2 Backend start.** `main.py` calls `sandbox_profile.startup()` before any database is opened; the mode is fixed
  for the process. With the marker: every database path (`storage.db`, `newsroom.db`, `market_research.store`
  `default_path()`; the paper texts and db backups follow hedgefund.db) is in `data/sandbox/`
  (`UFOS_HEDGEFUND_DB` is ignored there), a `#`-framed WARNING says so and every later log line carries
  `[SANDBOX]`. An unreadable marker or a missing sandbox copy stops the start (exit 2, CRITICAL log) — never a
  fallback to the real files. `data/api-token` and `data/logs/` stay where the app reads them.
  - Payload field for the app banner: `"mode": "normal" | "sandbox"` on `GET /health` (now JSON
    `{"status": "ok", "mode": …}`; the app only checks the 200), `GET /version`, `GET /fund/state`,
    `GET /v2/portfolio/state`, `/v2/oms/runtime`, `/v2/incubation`, `/v2/monitoring/overview`, `/v2/brokers`.
    In sandbox mode also `"sandbox": {since, banner, brokers: "blocked in sandbox", data_dir: "data/sandbox",
    prices_note}` (fix1023: "prices for copied positions may be unavailable while brokers are blocked").
- **S3 Isolation in sandbox mode.**
  - Broker accounts (alpaca-paper, binance-demo; kraken-demo stays retired) are hard-blocked:
    `mirror.remote_adapter` returns `SandboxBlockedAdapter` (every call raises "blocked in sandbox", no client is
    built — also not a test fake), `accounts.rows()` lists none of them as active (runtime, 15 s poller, start-up
    reconciliation, allocator and kill switch skip them), every broker action answers 409 `sandbox_blocked`,
    pre-trade checks reject with the reason, routing offers paper-main only, an incubation start on a broker route is
    refused (`sandbox_blocked`), `GET /v2/brokers[/{id}]` shows `state: "blocked_sandbox"`,
    `sandbox_blocked: {label: "blocked in sandbox", reason}`, all `can` false.
  - Market DATA reads stay allowed (quotes, bars, the Alpaca data API incl. its asset list) — the runtime needs prices.
  - Old engine: `set_autopilot(true)` → 409 with the sandbox text; no scheduled full cycles (`schedule_mode`
    "manual_only", no next run); "Run now" still runs inside the sandbox.
  - Kill switch: acts on the sandbox copies only; a broker account is never contacted (`sandbox_blocked` in the
    per-account result).
  - AI cost ledger lines get `"mode": "sandbox"` and the label prefix "SANDBOX · " (normal lines unchanged).
  - Not changed: discovery-loop auto-resume, Research and Newsroom collectors run on the sandbox copies (their AI
    calls are tagged as above).
- **S4 Seeding** `tools/sandbox.py seed-incubation-test` — refused unless the marker is active and the process is in
  sandbox mode (`require_sandbox`: explicit check + assert; the CLI opens no database before it). Creates
  "SANDBOX TEST — incubation fixture" (tag `sandbox-fixture`; SPY/IEF, hold the one further above its 200-day
  average, top 1, monthly rebalance, long only — the runtime trades it on paper-main) and moves it
  draft → backtested → candidate → validated THROUGH `registry.transition` with synthetic run cards (backtest,
  validation with G1–G8 "pass — not evaluated", holdout with H; deterministic made-up curves, no market data, the real
  holdout window's dates only as labels; the holdout event belongs to the synthetic holdout card). Every card's
  `metrics.fixture = true`, `metrics.fixture_note = "synthetic fixture for UI testing — no real validation"`, a
  `fixture` warning; reports carry both keys too; transition reasons start with "SANDBOX fixture — …". Idempotent
  (an existing fixture is reported, an interrupted seeding continues).
  - API: `fixture: true` + `fixture_note` on run lists / details (`/v2/backtests…`, `/v2/validations…`), strategy
    list / detail (by the tag), incubation status, monitoring views — absent everywhere else (normal payloads
    unchanged).
- **S5 Decay helper** `tools/sandbox.py force-decay "<strategy name or id>"` — sandbox only, needs an active
  incubation run. Appends ONE synthetic shock session (drawdown = DD_FACTOR × holdout max DD + 5 %) to the run's
  real sessions, evaluates `decay.assess` and pauses through `decay._pause` (audit `decay_paused` with
  `synthetic: true`, event, alert). Reason "SANDBOX force-decay — synthetic deviation, not real performance: …";
  the monitoring view shows `decay.paused.synthetic: true` + the fixture keys. The runtime then withholds the
  strategy's orders as after a real trigger; keep running / demote work as usual.
