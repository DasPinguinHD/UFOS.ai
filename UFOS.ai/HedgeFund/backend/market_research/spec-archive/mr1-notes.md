# Market Research SPEC — archive: mr1-notes

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives, exact JSON of that stage. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 13. Stage notes

(Each stage appends "MRn notes (implemented <date>)" here: what was built,
deviations, review fixes.)

### 13.1 MR1 notes (implemented 2026-10-16: backend part)

- Files: `__init__.py` (`ResearchService`, `install(app)`, `service()`),
  `limits.py` (§4.4 constants + `clip(text)`), `supervisor.py`
  (`ResearchSupervisor`, `RestartPolicy`, `describe_error`), `store.py`
  (v1: `meta`, `audit`; `status()` without I/O: `ok` / `unavailable` with
  the `StoreUnavailable` user message / `not_opened`; an operation failure
  turns it `unavailable` until the next successful operation),
  `maintenance.py`, `feeds.py` (MR1: all feeds `off`, "Live data is not
  built yet (stage MR2)."), `api.py` (`GET /research/feeds/status`,
  `status_payload(supervisor, feeds, now)`).
- main.py: `_install_research(app)` right after `v2_routes.register(app)`
  (lazy `import market_research` inside try/except → on failure a `*
  /research/{tail:.*}` route answering 503 `ResearchUnavailable`);
  `research.start()` after `engine.start()`; `/shutdown` awaits
  `research.shutdown()` (≤ 2 s) before scheduling the exit. Research tasks
  are not part of main's `gather()`.
- The WebSocket server on 8865 (websockets) is untouched; `/research/ws`
  (MR2) lives on the aiohttp app (8867) — the middleware accepts the token
  only as header there.
- Tests: `tests/test_mr_shell.py` (27): supervisor isolation with a dummy
  engine task, restart/failed/return/healthy-reset/bounded stop/no-loop
  start, store WAL + idempotent migrations + corrupt quarantine + newer
  schema + unopenable path + prune, maintenance retry, exact payload keys,
  400 on query params, generic 500, 401/403 through `api_auth.http_middleware`,
  main.py wiring incl. the 503 stub.
- Review fixes (2026-10-16): H1 own Research thread pool (`executor.py`,
  4 workers, context copied), `maintenance.py` uses it, AST test forbids
  `to_thread` / default executor in the package, research acquire timeout
  30 s; L4 `SystemExit` from a Research task = crash (test),
  `KeyboardInterrupt` passed on (documented); L5 `user_version` read before
  WAL (test: newer-schema file byte-identical, journal mode unchanged); L6
  all-or-nothing quarantine (tests with a simulated `PermissionError`);
  SPEC only: M2 §4.3.1, M3 §0 Time / bars / bridge, L7 §1.2 + §7.6
  re-check schedule, L8 probe under research priority, L10 frame cap +
  chunking. MR tests: 34.

