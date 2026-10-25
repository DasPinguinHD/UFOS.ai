# Strategy Platform SPEC — archive: 10-robustness

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 10. Robustness: parallel loading, parallel compute, resumable runs (contract, 2026-10-09)

Reason (user, 2026-10-09): on a weak Wi-Fi connection validations failed
with Alpaca timeouts; the program must cope with that. Interview decisions:
data loading **adaptive concurrency** (toggle; off → fixed number 1–8);
validation compute on **all CPU cores but one**; failed runs can be
**resumed** (button) and optionally **auto-resumed** after a connection
error (no time limit — weak machines must not be punished — but at most
**5 automatic resumes** per run); a resumed run counts as **one trial**.
Security measures accepted: compute workers get no network and no keys;
resume only on an identical basis; checkpoints tamper-evident; auto-resume
bounded (5 attempts, connectivity probe at most every 30 s, visible,
cancellable).

### 10.1 Network settings + adaptive loading (owner R1)

- `data/network.py`: settings stored in hedgefund.db meta
  (`network.adaptive: bool` default true, `network.fixed: int` 1–8 default
  4); `get_settings() -> {adaptive, fixed, current, min, max}`;
  `set_settings(adaptive, fixed)` (validated, audited by the caller).
- **Adaptive limiter** per source (alpaca, tiingo): concurrency starts at 4
  (max 8, min 1); a timeout/connection error halves it (floor 1); 20
  consecutive successes raise it by 1 (AIMD). Fixed mode = constant. It
  sits IN FRONT of the existing http_gate (rate limits unchanged:
  Alpaca 180/min, Tiingo 40/h — concurrency never bypasses them).
- **Parallel fetching:** the backtest/validation data loader fetches the
  symbols of a run concurrently through a thread pool bounded by the
  limiter (bars all/split, volume, corporate actions). Results identical
  to sequential (deterministic assembly order).
- **Chunking:** corporate actions per request ≤ 10 symbols and ≤ 3 years
  (configurable constants); bars as today. Request timeout stays 20 s; the
  retry policy of the gate stays (bounded).
- A connection-type failure raises `DataSourceUnavailable` with
  `kind="connection"` in its detail (timeout, DNS, unreachable, TLS
  handshake) so jobs can tell connection errors from data/auth errors.
- Connectivity probe `probe(source) -> bool`: one tiny request (Alpaca:
  latest SPY trade; Tiingo: not probed — only pre-2016 loads, use the next
  real request), never more often than every 30 s per source.

### 10.1.1 R1 notes (implemented 2026-10-09: network settings, limiter, parallel loading, chunking, error kind, probe)

- **`data/network.py`**: `get_settings() -> {adaptive, fixed, current: {alpaca,
  tiingo}, min: 1, max: 8}` (reads meta every call; `StorageUnavailable` when
  the DB cannot be read); `set_settings(adaptive, fixed=None)` (None keeps the
  stored `fixed`; refused when that is invalid) — `adaptive` must be
  a bool, `fixed` an int 1–8 (bools/floats/strings refused) →
  `InvalidNetworkSettings` (subclass of BOTH `InvalidRequest` and
  `ValueError`); stores meta `network.adaptive` ("true"/"false") and
  `network.fixed` ("1".."8"), applies at once, returns the get_settings
  shape; auditing is the caller's job. Switching to adaptive restarts every
  limiter at 4; fixed mode sets the limit to `fixed`. Missing keys = the
  defaults (true / 4). Before hedgefund.db is open (tests, start-up) the
  limiters use the defaults and load the stored settings at the first
  request after it is open (speed only — never data/source/results).
- **Invalid stored value** (review fix 2026-10-10, e.g. meta
  `network.adaptive = "yes"`): every Alpaca/Tiingo request (live paper feed
  included) and `pool_size()` raise `StoredSettingsInvalid` "The stored
  network settings are invalid — save them again in Hedge Fund → Keys & data
  → Network." — no default is used instead (no-fallback rule). GET does NOT
  raise: the invalid field is `null` and three fields are ADDED (only in this
  state, so the normal shape stays `{adaptive, fixed, current, min, max}`):
  `stored_invalid: true`, `error` (that message), `stored`
  `{"network.adaptive", "network.fixed"}` = the raw stored values (printable,
  ≤ 40 chars; no secrets live there). PUT repairs it: `set_settings`
  validates only the NEW input and stores it; the route's audit `old` is
  `{adaptive, fixed, stored_invalid: true, stored}` in that case.
- **AIMD** (`AdaptiveLimiter`, `limiter_for(source)`, sources `alpaca`,
  `tiingo`; `alpaca_sip` uses the alpaca gate and limiter): per request
  attempt — success → +1 to the streak, 20 in a row → limit +1 (max 8);
  connection-type failure → limit // 2 (floor 1), streak 0; other failures
  (HTTP 4xx/5xx/429, auth, bad data, SecretMissing) change nothing. A lowered
  limit lets running requests finish; new ones wait until in-flight < limit.
- **Gate integration** (`http_gate.HttpGate(concurrency=…)`; `gate_for`
  attaches `network.limiter_for` for alpaca/tiingo): per attempt slot →
  window limiter → token bucket → request → release (the slot is NOT held
  during the retry backoff). Rate limits unchanged (Alpaca 180/min + burst
  10, Tiingo 40/h + 800/day); `calls`/`retries` counters now under a lock.
  A thread waiting for a slot calls `progress` every 5 s with an event
  WITHOUT "message" (`{source, waiting: True, concurrency}`) so a cancel can
  interrupt the wait. Live paper engine (`fund/market.py AlpacaFeed`) uses
  the same gates → its requests are limited too (concurrency only, no other
  behaviour change).
- **Error-kind contract (for R2 / jobs):** a `DataSourceUnavailable` caused
  by a connection-type failure has the instance attribute
  `kind == "connection"`; every other error has NO `kind` attribute, so
  `getattr(ex, "kind", None) == "connection"` is the test (helper
  `network.error_kind(ex)` returns "connection" or None; constant
  `network.CONNECTION`). Connection-type (`network.is_connection_error`):
  timeouts (connect/read, `TimeoutError`, `socket.timeout`, requests
  `ConnectTimeout`/`ReadTimeout`, aiohttp/asyncio timeouts), DNS
  (`socket.gaierror`/`herror`, urllib3 `NameResolutionError`), refused /
  reset / aborted / network or host unreachable (`ConnectionError`
  subclasses, errno ECONN*/ENET*/EHOST*/ETIMEDOUT/EPIPE and the Windows WSA
  codes), TLS (`ssl.SSLError` incl. certificate verification — a resume
  verifies again, nothing is bypassed), proxy errors, connection broken
  mid-response (`ChunkedEncodingError`, `IncompleteRead`,
  `ServerDisconnectedError`), aiohttp `ClientConnectorError`/`ClientOSError`
  — also when wrapped (`reason`, `__cause__`, exception args). Anything that
  carries an HTTP status (a response arrived: 429, 5xx, 4xx) is never
  connection-type. Set by `http_gate` (retries exhausted on timeouts →
  "… timed out — gave up after 3 attempts" + kind; network/TLS → at once +
  kind) and by `registry.fetch`'s catch-all. Registry status recording is
  unchanged.
- **Probe** `network.probe(source) -> bool`: alpaca = one
  `registry.fetch("equity_quotes", "latest_trade", "SPY")` (gate + limiter,
  records the source status); True = it worked, False = a connection-type
  failure, any other error (keys rejected, key missing, …) is RAISED
  (waiting cannot fix it). tiingo = no request, returns True (the next real
  request is the probe). Spacing ≥ 30 s per source (`PROBE_MIN_INTERVAL_SECONDS`):
  an earlier call SLEEPS until the interval has passed;
  `network.next_probe_in(source)` gives the seconds left so a job can wait
  cancellably first. `Prober` / `set_prober` for tests.
- **Parallel loading** (`backtest/engine.py _load_symbols`, used by `run`):
  the symbols of a run (strategy symbols + benchmark) load in a
  `ThreadPoolExecutor` of `loader.max_workers` (if the loader sets it) or
  `network.pool_size()` workers (8 adaptive / `fixed`); requests in flight
  are bounded per source by the limiters. Results, warnings
  (`no_volume_source` and data warnings) and `loaded` are assembled in the
  sequential order → identical result (test). First failure: symbols not yet
  started are skipped and running gate waits stop (internal `_LoadStopped`,
  `is_cancellation=True` so the source status is untouched); the error
  raised is the one of the earliest symbol (in load order) that failed on
  its own — what a sequential load raises. Progress: "loading SYM (k/n)" with
  the percentage of finished symbols; cancel is checked per symbol and in
  every gate progress event, from the worker threads.
- **Chunking** (`providers/alpaca.py`): `CORPORATE_ACTIONS_MAX_SYMBOLS = 10`,
  `CORPORATE_ACTIONS_MAX_DAYS = 1095` (≤ 3 years); `corporate_action_chunks`
  partitions symbols × [start, end] (groups in the given order, windows
  oldest first; start > end → one unchanged request); each chunk is one gate
  call; rows merged, an action `id` reported by two chunks kept once, sorted
  by a total key (ex_date, symbol, kind, then all values) → identical to one
  request (test). The seam's per-symbol refresh (seam → today) now makes
  ~4 requests per symbol. Bars unchanged (`FETCH_WINDOW`); request timeouts
  (Alpaca 20 s, Tiingo 30 s) and the gate's retry policy unchanged.
- Tests: `tests/test_sp_network.py` (classification incl. real `requests`
  classes when installed, gate/registry marking, AIMD, fixed mode, settings
  on a temp DB, probe spacing) and `tests/test_sp_network_loading.py`
  (limiter never exceeds its limit, token bucket ≤ 190/min and Tiingo-style
  window under 8 parallel callers with a fake clock, parallel == sequential
  engine result, first-error rule, chunking equivalence, live-feed path).

### 10.2 Parallel compute (owner R2)

- `validation/parallel.py`: a process pool (`spawn`, Windows-safe) with
  `workers = max(1, os.cpu_count() - 1)`; memory guard: estimated bytes per
  task (panel size × configs) — the pool size is reduced so the estimate
  stays ≤ 2 GB total; documented constants.
- Workers receive ONLY in-memory data (panels, corporate actions, volume,
  spec, request params) — they never import secrets, never open network
  connections, never touch the DB (assert in the worker entry: a guard
  that makes `secrets`, `http_gate` and storage imports raise in the
  worker). Results return to the parent, which writes everything.
- Grid backtests and stress runs of a validation run in the pool; results
  are assembled in the deterministic config order → identical report to a
  sequential run (test). Cancel terminates all workers.
- Backtests (single run) stay single-process.

### 10.3 Resumable runs (owner R2)

- **Checkpoints:** table `run_checkpoints(run_id, step_key, payload_json,
  sha256, created_at)` insert-only; step keys e.g. `data`, `base`,
  `fold:k`, `grid:<config-hash>`, `stress:<name>`. Each payload's sha256
  is stored and verified on reuse; mismatch → that step is recomputed and
  a warning `checkpoint_mismatch` is recorded.
- **Data checkpoint:** the run's data snapshot (per symbol: sources, ranges,
  row hashes — already in run cards) — the actual bars live in the cache.
- **Resume** (`POST /v2/backtests/{id}/resume` and
  `POST /v2/validations/{id}/resume`): allowed only for a run with status
  `failed` whose error kind is `connection` (others → 409 with the reason)
  and only if the basis is identical: same strategy version + content
  hash, same request (incl. end date as RESOLVED at the first start), same
  holdout window, and the cached data for the already-loaded symbols still
  hashes the same. Otherwise 409 "The basis changed (…) — start a new
  run." A resume creates a NEW run card with `resumed_from` = the failed
  run id (run cards stay insert-only after finish) and the same
  `trial_number` (counts as one trial: trial counting ignores cards that
  have a later `resumed_from` successor, and a holdout run is NEVER
  resumable — a failed holdout run is final; document why: the window
  could be re-run with knowledge of a partial result).
- **Auto-resume:** request flag `auto_resume: bool` (default false, UI
  checkbox). On a connection failure the job state becomes `waiting`
  (progress message "Connection lost — waiting for Alpaca (attempt n/5)…",
  probe every ≥ 30 s, no time limit); when the probe succeeds the run
  resumes from its checkpoints; after 5 automatic resumes the run fails
  normally (manual Resume still possible). Waiting runs block the single
  job slot (one run at a time) and are cancellable.
- Crash recovery (backend restart) marks queued/running/waiting runs failed
  as today with kind `connection`=false → resumable only if the last error
  was a connection error.

### 10.4 API + WPF (owner R2 routes, W UI)

- `GET/PUT /v2/settings/network` {adaptive, fixed} → {adaptive, fixed,
  current: {alpaca, tiingo}, min, max} (audited).
- Backtest/validation requests accept `auto_resume`; run cards and list
  rows carry `resumed_from`, `resume_count`, `error_kind`, `resumable`.
- WPF: settings section "Network" (toggle Adaptive; when off a number
  input 1–8 is visible; shows current concurrency); run dialogs get the
  checkbox "If the connection drops: wait and resume automatically (max 5
  times)"; failed runs with `resumable` show **Resume**; waiting runs show
  the waiting message + Cancel; reports show "resumed from <id>".

### 10.5 R2 notes (implemented 2026-10-09: parallel compute, checkpoints, resume, auto-resume, routes — §10.2–§10.4)

- **`validation/parallel.py`**: `plan_workers(symbols, sessions, n_tasks=None,
  cpu_count=None)` → `{workers, cpu_workers, memory_workers,
  per_worker_bytes, limit_bytes}`; workers = min(max(1, cpu − 1), memory
  guard, tasks). Estimate per worker = `WORKER_BASE_BYTES` (200 MB) +
  symbols × sessions × (`ROW_BYTES` 700 × `SERIES_PER_SYMBOL` 4 ×
  `SNAPSHOT_COPIES` 2 + 8 × `PANEL_ARRAYS` 12); total ≤ `MEMORY_LIMIT_BYTES`
  (2 GB). Sessions are estimated from history_from → training end
  (`ValidationJobs.pool_plan`). **1 worker = inline** (no process; the tasks
  run in the job thread on the job's loader — the sequential computation).
  `TaskRunner(workers, loader, run_engine, cancel, progress).run(phase,
  shared, tasks)` → results in task order. Tasks (`TASKS`, top-level,
  picklable): `grid`, `episode` (`stress.episode`), `costs`
  (`stress.synthetic_costs`), `isolation_check` (guard self-test).
- **Pool**: `multiprocessing.get_context("spawn")`; during `Process.start()`
  `sys.modules["__main__"]` is swapped for an empty module so `python
  main.py` is NOT re-executed in workers (tested with a fake main script).
  Data: every task first runs against a `CollectingLoader` (empty data — the
  engine records its loader calls, then stops with "no bars"); the parent
  fetches those calls through the job's `RecordingLoader` (network, cache,
  data checkpoints) and ships a snapshot `{call: pickled (rows, info) |
  frozen UserFacingError}`; workers use `ReplayLoader` (missing call or
  external feature `{"ext"}` → `NeedsPrefetch` → the parent loads it with
  `evaluate.load_ext` and re-runs those tasks; ≤ 3 rounds). A connection
  error is never shipped: it stops the run (wait/resume). Monte-Carlo runs
  in the parent. Cancel polls every 0.2 s and terminates all workers.
  A worker crash → RuntimeError (traceback in backend.log) → generic failure.
- **Worker guard** (`install_worker_guard`, first thing in `_worker_main`):
  `strategy_platform.secrets`, `keyring`, `win32cred` are stub modules (import
  works — `strategy/features/fred.py` imports `secrets` at module level —
  every use raises `WorkerIsolationError`); `socket.connect/connect_ex/
  sendto/create_connection/getaddrinfo` and `sqlite3.connect` raise;
  `storage.db.init/_open/_connect` raise; env vars matching `SECRET_ENV_RE`
  are removed. `http_gate` and `storage.db` cannot be refused at import
  (the engine imports them via seam/families) — their USE is blocked.
- **Validation jobs**: grid + stress run through the runner
  (`run_validation(…, runner, checkpoints, stress_tasks)`; `runner=None` =
  the old loops, kept for calibration tests; injected `run_stress` → the
  stress part keeps calling it). Stress assembly = run_stress's order
  (episodes incl. locked ones, costs_x2, slippage_x3, monte_carlo) + costs_x
  m when G6's multiplier ≠ 2. Pool == sequential: full report equality
  tested (`test_sp_parallel`). Plain backtests stay single-process.
- **Storage v8**: `backtest_runs.resumed_from` (UNIQUE when set — a chain
  cannot fork), `resume_count` (≥ 0, never decreases), `error_kind`
  (`connection` | `interrupted` | `basis` | `error`, set by `runcard.fail`),
  `auto_resume` (0/1, always 0 for holdout); a running card may be updated
  running→running ONLY in `resume_count` / `error_kind` (trigger,
  `db.backtest_run_note`). Insert-only `run_checkpoints(id, run_id,
  step_key, payload_json, sha256, created_at)`; db API
  `checkpoint_insert`, `checkpoints_list(run_ids)`, `backtest_run_chain`,
  `backtest_run_successors`. Trial counts (`family_trial_count`,
  `backtest_run_finish`, `family_done_runs`) skip cards that have a
  `resumed_from` successor (only failed cards can have one; the failed card
  never had a trial number, the chain's final done card gets one → ONE
  trial).
- **Checkpoints** (`runcard.CheckpointStore(run_id)` over the chain run →
  resumed_from → …): `sha256(run_id \n step_key \n payload_json)`; `get`
  verifies the NEWEST row of a step — mismatch → None + warning
  `{code: "checkpoint_mismatch", step, run_id, message}` on the card, the
  step is recomputed (no older row is used). Steps: `basis`,
  `data:<method>:<symbol>…` (row count + sha256 of the rows),
  `grid:<hash>`, `stress:<key>:<hash>` (hash = spec content hash, range,
  capital, benchmark, scenario parameters).
- **Basis** (`runcard.current_basis/basis_problem/ensure_basis`): strategy
  id, version, content hash (also re-checked against the stored version),
  request, resolved end (request end or the training end), holdout window
  {start, end}. Written at the first start; every later attempt (auto or
  manual) compares → `BasisChanged` ("The basis changed (…) — start a new
  run.", error_kind `basis`, not resumable). Data: a call whose rows hash
  differently from the data checkpoint of an earlier attempt → BasisChanged.
  The window rolls daily, so a resume on another trading day is refused
  (user-accepted strictness).
- **Resume** `jobs.resume(run_id, auto_resume=None)` → NEW card
  (`runcard.create_resumed`: same strategy/version/hash/spec/request/seed/
  kind, `resumed_from`, `resume_count` = old + 1, auto_resume = given or
  inherited); refusals (`runcard.resume_refusal`, re-checked under the
  runner lock): holdout ("A holdout run cannot be resumed: a failed holdout
  run is final — re-running the window with knowledge of a partial result
  would contaminate it …"), status ≠ failed, error_kind ≠ connection,
  already resumed ("This run was already resumed as <id>."), basis changed.
- **Auto-resume** (`JobManager._work`): on a connection error with
  `auto_resume` and < `MAX_AUTO_RESUMES` (5) automatic resumes: card
  error_kind `connection` (noted), job `waiting` (progress phase `waiting`,
  message "Connection lost — waiting for Alpaca (attempt n/5)…" / Tiingo by
  the error's `source`), waits `max(30 s, network.next_probe_in)` on the
  cancel event, then `network.probe`; True → resume_count + 1, error_kind
  cleared, the SAME card's work re-runs (checkpoints reused); no time limit;
  a probe error that is not a connection error fails the run. The 6th
  connection failure fails the card (`connection`, resumable by hand). The
  budget is per card (a manual resume starts a new budget). Cancel while
  waiting → cancelled. Stress runs inside the job turn a connection error into
  a run failure (not a "failed" scenario). Crash recovery: queued/running →
  failed `interrupted` ("Backend stopped during the run"), a card that was
  waiting → failed `connection` ("Backend stopped while the run was waiting
  for the connection", resumable).
- **Routes** (behind api_auth): `POST /v2/backtests` and `POST
  /v2/validations` accept `auto_resume: bool` (default false). `POST
  /v2/backtests/{id}/resume` (any kind; holdout → 409) and `POST
  /v2/validations/{id}/resume` (validation/holdout cards, else 404) body
  `{auto_resume?: bool}` → 202 `{run_id, resumed_from}`; 404 `{error}`; 409
  `{error}` (refusal text) or `{error, run_id}` (busy); 400 `{error, errors}`.
  Backtest/validation list rows and details add `resumed_from, resume_count,
  error_kind, auto_resume, resumable` (resumable = failed + error_kind
  connection + not holdout + not yet resumed; the basis is checked on
  Resume) and `status` = "waiting" while the job waits. `GET
  /v2/settings/network` → `{adaptive, fixed, current: {alpaca, tiingo},
  min, max}`; `PUT {adaptive, fixed?}` (fixed 1–8, default = current) →
  same shape, audit `network_settings_changed` {old, new}.
- **Review fixes (2026-10-10)**: (1) *Orphan workers*: every worker runs a
  watchdog thread on `multiprocessing.parent_process().sentinel` (+ getppid
  change on POSIX) → `os._exit` within ~`WATCHDOG_SECONDS` (1 s) when the
  parent dies; running pools are registered (`active_workers()`) and
  `parallel.terminate_all()` is called by main.py's `/shutdown` before its
  `os._exit`. (3) *Data checkpoint* hashes rows without `VOLATILE_ROW_KEYS`
  (fetched_at, known_at, checked_at, updated_at, created_at,
  realtime_start, retrieved_at) — daily re-fetched corporate actions no
  longer break an overnight resume; the auto-resume wait loop re-checks the
  basis before every probe and fails the run (`basis`, with the reason) once
  it cannot match any more (e.g. the holdout window rolled). (4)
  *Classification*: `is_connection_error` follows `__cause__` and
  `ConnectionLost` only (not `__context__`); same for `jobs.source_of`.
  (5) *Audit hook* (`install_audit_hook`, in the worker entry before
  unpickling; numpy/engine modules are imported first because numpy loads
  ctypes): raises `WorkerIsolationError` on `socket.*`, `ctypes.*`,
  `sqlite3.connect` (incl. `_sqlite3` / `sqlite3.Connection(path)`),
  `subprocess.*` / `os.exec*` / `os.spawn*` / `os.system` / `os.fork`,
  `winreg.*`, urllib/http.client connects, and `open()` of `.env`,
  `*.db*`/`*.sqlite*`, `data/api-token` and Windows credential/vault/protect
  or `.ssh` paths (monkey-patch guards kept). (6) The basis includes
  `engine_version` (`engine.ENGINE_VERSION`) and `report_version`
  (`report.REPORT_VERSION`): a mismatch → 409 "The basis changed (the engine
  was updated (A → B)) — start a new run." (7b) `jobs.source_of` → alpaca |
  tiingo | fred | sec_edgar | the source name | unknown; message "waiting for
  FRED/SEC EDGAR/…"; sources without a `data.network` probe (FRED, SEC
  EDGAR, others) send no probe request — after the 30 s wait the run resumes
  and its next real request is the probe (fails again if still down).
- Tests: `test_sp_parallel.py` (17: plan, freeze/thaw, ext prefetch,
  classification (`__context__` vs `__cause__`), volatile-key hash,
  forbidden paths, isolation guard + audit hook in a real worker (17
  bypass checks), spawn from a script, workers end when the parent dies,
  `terminate_all`, cancel, pool == sequential report, inline == sequential,
  stress scenarios incl. done episodes and costs x3, validation job on the
  pool, pool cancel), `test_sp_resume.py` (20). `test_sp_api_backtests.py` /
  `test_sp_api_validation.py` key sets extended by the new fields.
