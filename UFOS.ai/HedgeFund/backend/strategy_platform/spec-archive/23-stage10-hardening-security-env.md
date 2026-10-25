# Strategy Platform SPEC — archive: 23-stage10-hardening-security-env

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 23. Stage 10 — hardening, security, Kraken removal, .env removal (as-built, merged 2026-10-24)

Merged by Agent P from the batch notes `SPEC_PENDING_s10_{A,B,E_W,H,K,U}.md (agent working file, not kept)` (text as delivered by
Agents H and W, lightly shortened). The Council migration is §22. Open decisions are listed in §23.7.

### 23.1 S10-B backup + cleanup (Agent H)

- Fresh backup of the three real DBs: `HedgeFund/backend/data/backups/stage10-20261024T115619Z/` (hedgefund.db v24,
  newsroom.db v3, market_research.db v7; quick_check ok; `SHA256SUMS.json` / `.txt`; byte copy verified unchanged,
  rebuilt with the SQLite backup API on the VM side, copied back).
- Deletion (user permission 13:54): 106 files, 1.36 GB, each sha256-verified against `DELETE_MANIFEST.txt` (308 rows;
  deleted subset `DELETE_NOW.txt`). 202 recent `*.stale` rollback copies kept until the click-test
  (`KEEP_UNTIL_CLICKTEST.txt`). Kept untouched: the real DBs, `options_archive.db*`, `tr-import/`, scope-guard
  manifests, current sandbox copies.
- Agent rule: never open a real DB over the VM mount with SQLite ("disk I/O error" on the WAL index, stray
  `-wal` / `-shm`) — copy db + `-wal` to a VM-local temp dir, verify sha256 before = after, open the copy.

### 23.2 S10-H backend hardening (Agent H)

**S10-H tools venv guard.** Every command-line tool in backend/tools/ refuses the system Python BEFORE it imports
anything beyond the standard library: strategy_platform/core/venv_guard.py (stdlib only) — problem(tool,
backend_dir, prefix) returns None when realpath(sys.prefix) == realpath(<backend>/.venv) (normcase), else
"This is not the backend's Python (<prefix>) — run with .venv\Scripts\python.exe tools\<tool> …  (from
HedgeFund\backend)"; require(tool, backend_dir, exit_code) prints "REFUSED: <message>" to stderr and exits. Only the
__main__ path is guarded (importing a tool in tests never triggers it). Exit codes (documented in each tool's
docstring): sandbox.py 2 (its existing venv_problem() now delegates to venv_guard), options_archive.py 3 ("stopped:
… / not the backend venv"), verify_alpaca_options.py 3, verify_binance_demo.py 5 (4 = "test order refused" already
taken). A new tool needs the guard + an entry in tests/test_s10_hardening.VenvGuardTest.TOOLS (the test fails
otherwise).

**S10-H NYSE calendar through 2030.** core/calendars.py: CALENDAR_YEARS = 2000–2030. 2016–2027 stay hardcoded
(published rows); 2028–2030 (PROJECTED_YEARS) come from modern_holidays(year) / modern_early_closes(year), the
current NYSE rules: New Year's Day (Sunday → Monday; on a Saturday no closure), MLK Day (3rd Mon Jan), Washington's
Birthday (3rd Mon Feb), Good Friday, Memorial Day (last Mon May), Juneteenth (since 2022), Independence Day, Labor
Day (1st Mon Sep), Thanksgiving (4th Thu Nov), Christmas — Jun 19 / Jul 4 / Dec 25 on Saturday → Friday before, on
Sunday → Monday after; 13:00 early closes: Jul 3 Mon–Thu, day after Thanksgiving, Dec 24 Mon–Thu. Source:
nyse.com/markets/hours-calendars (checked 2026-10-24; the published calendar covers 2026–2028). 2028 = the
published calendar; 2029–2030 are the rules projected — an unforeseeable special closure (day of mourning, weather)
must be added when the NYSE announces it. A test proves the rule functions reproduce the hardcoded 2022–2027 rows
(minus the 2025-01-09 Carter day of mourning). Each of 2028/2029/2030 has 251 sessions. 2031+ still raises
CalendarUnavailable (fail-closed, unchanged).

**S10-H deterministic committee status.** research/committee.run_out reads "is the worker active" BEFORE the run's
events. The worker commits its terminal event before it leaves _active, so a run that is no longer active always
shows its terminal event; a run finishing between the two reads now reports "running" (the next poll sees "done")
instead of a false "interrupted" (the cause of the flaky test_sp_committee).

**S10-H market_research.db startup race.** market_research/store.init sets _opening = True for the duration of the
open (reset in finally). While the FIRST open runs, status() returns state "not_opened" (starting) instead of
"unavailable", and an operation that needs the path (_require_path) waits for the running open (takes the store
lock init holds) instead of failing. A failed open still reports "unavailable" with its reason.

**S10-H incubation.realised counts the first session (SPEC §19.12 note resolved).** live/incubation.realised reads
ledger.daily_returns from the snapshot of the session BEFORE start_date (previous NYSE trading day; for a utc-calendar
run the day before) and keeps returns with start_date <= day <= end_date — the first session's own return is
included, as in monitoring/expected.realised (before, the §17.2 band dropped it).

**S10-H context notices (backend part, SPEC §15 "Stage 10 addition").** strategy_platform/core/notices.py holds the
texts; the three AI-context notices are word-for-word Shared/AiContent.cs (the WPF shows payload notices verbatim):
ANALYSIS (a) "AI-generated analysis — may be inaccurate; information, not a recommendation."; RESEARCH (b)
"AI-generated research output for your own decision. Not a personal recommendation by a licensed advisor; you
decide and carry the risk. Backtests are hypothetical."; PAPER_EXECUTION (c) "Automated trading on your own paper
account under the limits you set. Paper only — no real money." (d) a live notice is defined only if live trading is
ever enabled. Short context lines: PAPER "Paper trading · simulated money.", PAPER_DEMO "Paper/demo trading ·
simulated money.", HYPOTHETICAL "Hypothetical, simulated results — they do not indicate future results.", REVIEW
"For your own review.".
- payload `notice` of /v2/portfolio/state (portfolio/views.NOTICE), /v2/brokers… (execution/brokers/service.NOTICE),
  /v2/monitoring/overview (monitoring/service.NOTICE) = PAPER_EXECUTION;
- Studio / discovery loop AI_INTERACTION_NOTICE = "You are interacting with an AI system. … AI-generated and not
  verified. " + RESEARCH (EU AI Act Art. 50(1) sentence kept);
- alert / event bodies (monitoring/alerts, reconcile, broker kill, control.kill_all, incubation allocation,
  decay, risk caps): "Paper trading · simulated money." / "Paper/demo trading · simulated money.";
- Shadow rule portfolios: "… Hypothetical, simulated results — they do not indicate future results."; Shadow report
  disclaimer: "… monitoring only, for your own review. …";
- newsroom macro phase disclaimers: "… not a forecast — information, not a recommendation.".
fund/ (old engine) is untouched — it goes with S10-C. A test (NoticesTest) checks parity with the AiContent.cs
literals and that no user-facing strategy_platform text carries the blanket "not investment advice".

### 23.3 S10-K Kraken demo removal (backend: Agent H; WPF: Agent W)

**S10-K Kraken demo removal (backend).** The Kraken Futures demo adapter (execution/brokers/kraken_demo.py → .stale)
and every kraken-demo branch are gone:
- mirror: no Kraken adapter; venue / tif / can_hold rules name binance-demo only;
- service: no Kraken meta / "Markets loaded (PF_*)" test check / other-wallet kill note; crypto route targets =
  paper-main | binance-demo;
- reconcile: no Kraken funding import, no collateral basis (cash basis for every broker), no Kraken tolerance /
  liquidation hint;
- live_guard: no Kraken demo endpoint / host. LIVE_HOSTS (the deny-list of production hosts, documentation and
  the source-scan test) keeps futures.kraken.com / api.kraken.com;
- secrets: the key names kraken-demo-api-key / -secret left SECRETS (never read, not listed by GET
  /v2/settings/keys). They are kept as secrets.RETIRED_SECRETS: POST /v2/settings/keys/changed accepts them for
  action "removed" ONLY (audit row key_removed {name}); "set" / "imported" → 400 "<name> is retired — only action
  "removed" is accepted". Lets the WPF audit the removal of a still-stored key cleanly (W request 2).
KEPT (the retirement guard, SPEC §18.13): accounts.RETIRED / RETIRED_SOURCES / retired_reason; kraken-demo stays a
known id (accounts.BROKER_IDS = ACTIVE_BROKER_IDS (alpaca-paper, binance-demo) + the retired ids; the route regexes
keep accepting it) so an old kraken-demo row / route in hedgefund.db is answered 409 "retired", shown blocked under
Routing and never re-routed silently. Such a row gets mirror.RetiredAdapter (every call raises BrokerSetupError
"<id>: retired …"; no client, real or fake, is ever built). GET /v2/brokers lists the retired card only while
hedgefund.db holds a kraken-demo row (account, connection record or a route); its card has chip "RETIRED", no keys,
endpoint / sdk / terms null, every action off except "disconnect" of a still-connected record (record only). The
user's hedgefund.db (checked on a copy 2026-10-24) has no kraken-demo account / connection / route — only audit
history — so the card does not show. Also kept: ccxt (binance-demo), data/providers/kraken_public.py (Kraken PUBLIC
crypto prices for Shadow / insights), the storage/db.py migration CHECK lists, all audit rows.
requirements.txt: the ccxt comment names binance-demo.

**S10-K (WPF).** CredentialStore: the kraken-demo key names left the active key list; they are kept as
RetiredSecrets and shown in ⚙ Keys & data ONLY while still stored in the Credential Manager, with "Remove" as the
only action ("Kraken Futures demo retired 2026-10-19 — this stored key is not used by anything; remove it").
PlatformClient.BrokerIds = alpaca-paper, binance-demo (KrakenDemoId stays to name / block an existing retired row:
RetiredBrokerCard, AccountTitle, IsBrokerId, TradingView account line). Removed: the Kraken route target and its
consequence text, every kraken branch of the active broker card (margin fields, collateral wording, reduce-only kill
text), Kraken key entry (pair dialog, tooltip, validation).

### 23.4 S10-U WPF hardening (Agent W)

**S10-U single instance (scope label stage10ui: UFOS.ai/App.xaml.cs, UFOS.ai/Shared/AiContent.cs).** App.OnStartup
takes a named mutex "Local\UFOS.ai.App.<user SID>.Instance" BEFORE the AI notice. A second start signals the named
event "…Activate" (after AllowSetForegroundWindow(ASFW_ANY)), logs one line and exits with code 0 — no window, no
notice. The first instance listens on a background thread and brings its main window (or the AI notice while that is
still open) to the front (SW_RESTORE keeps a maximized state, Activate, SetForegroundWindow, one Topmost toggle). If
the guard itself cannot be created (exception on the mutex name) the start continues and the failure is logged.
The standalone HedgeFundApp.exe has no guard (developer entry point).

**S10-U context notices (SPEC §15 "Stage 10 addition").** Shared/AiContent.cs holds the three notices:
AnalysisNotice (a) "AI-generated analysis — may be inaccurate; information, not a recommendation." (also the value of
AiContent.Caveat, so every existing caveat line changes with it); ResearchNotice (b) "AI-generated research output
for your own decision. Not a personal recommendation by a licensed advisor; you decide and carry the risk.
Backtests are hypothetical." (Studio replies, paper → strategy, committee memos, loop finalists);
PaperExecutionNotice (c) "Automated trading on your own paper account under the limits you set. Paper only — no real
money." (d) is defined only if live trading is ever enabled. The HedgeFund WPF no longer shows the blanket "not
investment advice": paper contexts say "Paper trading · simulated money", backtests / validations "Hypothetical,
simulated results — they do not indicate future results.", analyses / AI explanations "information, not a
recommendation", Shadow statistics "for your own review". Outside the module (not in stage10ui): MainWindow,
Global Monitoring, LiveStreamAgent, Shared/AiDisclaimerWindow.cs still carry their own wording except where they use
AiContent.Caveat — a later label if the user wants them changed too.

**S10-U Studio polish.** A discarded / empty off-topic AI answer is never an empty AI block: followed by the
backend's fixed note it renders as ONE system note ("<backend note> · AI reply not shown (reason) · cost …");
without a note as one small system note with the cost.

**C1 kill path.** The toolbar ■ Kill switch and "Stop engine…" call POST /v2/oms/kill (control.kill_all): the platform
half (every platform account, brokers included) AND the old engine's kill (eng.kill(propagate=False)) — each half
reports its own result; the old engine's half is {ok, error, halted} only (its closing orders are listed in the
Overview, no longer in the result dialog). If the old engine is not running, its half says so (ok=false).
The toolbar "Resume" of the OLD engine stays on POST /fund/resume: /v2/oms/resume resumes ONE platform account
(account_id required) and has no old-engine half — the old-engine resume goes away with the Council migration (S10-C).
Market Research's Buy / Sell / Short / Cover hand-over opens Portfolio & Trading's order dialog (POST /v2/oms/orders,
pre-filled symbol + side, account = the first platform account) instead of the old engine's /fund/orders panel.

**Single-instance guard — failure case (user decision 2026-10-24, option A).** If the named lock cannot be created
the app starts normally, logs a warning AND shows a small non-blocking note in the main window's lower right corner:
"Single-instance protection unavailable this session — a second start would open a second window." (OK or closes
itself after 30 s).

### 23.5 S10-A security audit fixes

**S10-A1 backend / python (Agent H; scope label `stage10sec`).**

**S10-A1 (stage 10 security audit, backend / python).**
- M6: every backend listens on 127.0.0.1 only — `api_auth.bind_host(what)` → 127.0.0.1 (the remote-access opt-in
  and the host settings were removed with S10-E / the Docker removal, §23.6).
- L2 Host check (all three api_auth.py copies, kept identical): every HTTP request and WebSocket handshake —
  /health and /version included — needs Host: 127.0.0.1:<port> or localhost:<port>, <port> = the port the server
  listens on (read from the socket; unreadable → refused, fail closed); otherwise 403 {"error": "host not allowed"}
  (DNS-rebinding protection). check(..., port=None) stays pure (no Host check without a port).
- L2 public routes trimmed: HedgeFund GET /health = {"status": "ok", "mode"}; GET /version = {"code_mtime",
  "started_at", "mode"} — no pid, no sandbox details / data_dir (those stay in the token-protected state payloads).
  Global Monitoring GET /version = {"code_mtime", "started_at"} (no pid). The apps only read code_mtime.
- L9: the HedgeFund WebSocket placeholder on 8865 is removed (no client: WPF, GM, LSA checked) — config WS_HOST /
  WS_PORT gone.
- L5: the PDF worker (research/papers.extract_pdf) runs with env = papers.worker_env(): the backend's environment
  minus every name matching validation/parallel.SECRET_ENV_RE (as the validation workers).
- M2: HedgeFund/backend/.gitignore allow-lists data/: `data/*`, `!data/.keep`, `!data/scenarios/`,
  `data/scenarios/*`, `!data/scenarios/.keep` (the only versioned data/ files — read from the git index),
  plus `data/sandbox0-backup/` and `*.stale`.
- M1: superseded — Docker was removed entirely (user decision 2026-10-24 16:56, §23.6).
- M3 (part): LiveStreamAgent requirements no longer list the PyPI "asyncio" backport (stdlib).
- L1: GM (config.py + the per-call re-read in assess.py) and LSA (config.py) accept OPENROUTER_BASE_URL only as
  https on host openrouter.ai, default port, without user info / query / fragment (= W's C# check in
  Services/OpenRouterClient.cs); otherwise warning + the default https://openrouter.ai/api/v1 (the
  fallback the user accepted for the HedgeFund copy; the URL itself is not logged).
- L3: GM assess_context article fetch — public addresses only: IP-literal hosts and every redirect hop checked
  (check_url: http/https, no credentials, no localhost/.local, literal IP must be global), names resolved through
  PublicOnlyResolver (aiohttp resolver dropping private / loopback / link-local / reserved / CGNAT / multicast /
  IPv4-mapped-private results; none left → no connection) on its own session; redirects followed by hand, at most
  3; the article cache is an LRU of 200 entries (1 h TTL as before). Refusals are logged, the context then simply
  has no article text.
- L6: GM / LSA prune data/logs/<yyyy-mm-dd>.jsonl older than 30 days at start (logger.prune_old_logs; other files
  untouched). process.log (GM) is written by the WPF app (MainWindow.xaml.cs), env-setup.log / ui-errors.log by the
  scripts / app — not python-owned, so no size cap here (WPF-side follow-up if wanted); GM backend.log already rotates
  (2 MB × 3).
- L10: GM /localnews, /assess, /localnews-summary answer generic messages ("… (details in backend.log)"); the
  exception text goes to backend.log only.

**M3 dependency pinning (proposal, decision open; the C# side of the lock protocol is in §23.6).**
- Per backend a `requirements.lock` generated with `pip-compile --generate-hashes` (pip-tools) from the current
  `requirements.txt` on the user's Python 3.13 (Windows), committed next to it.
- run_*.ps1 installs with `pip install --require-hashes -r requirements.lock` (fails closed on any hash mismatch);
  `requirements.txt` stays the human-edited input. Updating = re-run pip-compile, review the diff, click-test.
- Optional packages (HedgeFund requirements-broker.txt: ccxt) get their own lock (same pattern, own pip step as now).
- Cost: one lock per Python version/OS (wheels differ); pip-tools as a dev-only tool. Alternative with less tooling:
  `pip freeze` pins without hashes (protects against surprise upgrades, not against a tampered index).

**S10-A2 WPF (Agent W; scope label `stage10secui`).**

- **M4 Global Monitoring WebView2** (`Global Monitoring/MainWindow.xaml.cs`, `globe.html`): the globe page stays
  on NavigateToString (location about:blank, opaque origin). Only that one navigation is allowed (flag set right
  before NavigateToString, consumed by its NavigationStarting); every other top-level navigation is cancelled
  (a user-clicked http(s) link opens in the default browser), every frame navigation is cancelled, downloads are
  cancelled, permission requests denied, WebResourceRequested answers 403 to everything except the page itself,
  `http://127.0.0.1:8768`, `ws://127.0.0.1:8767` and the pinned Natural Earth file. Host messages (incl. the
  API token) are accepted only from the page URI forms of that navigation and only after its ContentLoading
  (allow-list instead of the former http(s) deny-list). Settings: no script dialogs, host objects, accelerator
  keys, autofill, swipe/pinch.
  CSP meta in globe.html: `default-src 'none'`; `script-src` = sha256 of the single inline script, computed by
  the host at load time (placeholder `'sha256-GM_INLINE_SCRIPT_HASH'`; unreplaced → script blocked);
  `connect-src` = the two backend ports + the pinned NE URL; frames/objects/workers/forms/base all `'none'`.
  Natural Earth: pinned `world-atlas@2.0.2/land-110m.json`, fetched with `credentials:'omit'`, no referrer.
  NOT vendored yet: neither the container nor the device reaches npm/jsdelivr (proxy 403) — follow-up: vendor
  the file (small, well under 1 MB) with sha256 like klinecharts, then drop the CDN from CSP / allow-list.
  The localStorage cache in globe.html never worked (opaque origin) — comment corrected, code left.
  `backend/config.py` `DEFAULT_ALLOWED_ORIGINS = ("null",)`: verified still required (the page's cross-origin
  fetch() sends `Origin: null`); unchanged.
- **M5 ArticleWindow** (`ArticleWindow.xaml.cs`): InPrivate controller (CoreWebView2ControllerOptions,
  SDK 1.0.4191.47), DevTools / script dialogs / host objects / web messages / autofill off, downloads cancelled,
  permissions denied. Side effect: MarketWatch's bot challenge may run on every article.
- **L4 PowerShell** (new `Shared/WindowsPowerShell.cs`): all five launch sites (Newsroom 2×, Global Monitoring,
  LiveStream Agent, PythonEnvManager) use `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe` and
  `-NoProfile -ExecutionPolicy Bypass … -File "<x.ps1>" …`. Every launched file is a .ps1 (the helper refuses
  anything else); Bypass stays because Windows client editions default to Restricted and RemoteSigned refuses an
  unsigned repo script with Mark of the Web; it is process-scoped and Group Policy still wins.
- **L1-C#** (`Services/OpenRouterClient.cs`): OPENROUTER_BASE_URL must be https, host openrouter.ai, default port,
  no user info/query/fragment — checked before the budget reservation; otherwise the request is not sent and the
  user sees "AI summary failed: OPENROUTER_BASE_URL … must be an https URL on openrouter.ai …".
- **L7**: globe.html AI tooltip/caveat = AiContent.TriggerTooltip / AnalysisNotice; stale "not investment
  advice" comments fixed in AnalysisReasoningView.xaml.cs and HedgeFundWindow.xaml.cs. Open: the startup notice
  body (Shared/AiDisclaimerWindow.cs, "No investment advice: …") is unchanged — user-mandated text ("do not
  remove or soften"); its comments match it. Needs a user decision if it should follow the Stage 10 wording.
- **L8**: main window — one data-source line under the overview cards (Yahoo Finance chart endpoint; MarketWatch /
  Dow Jones and Yahoo Finance RSS; © publishers, not affiliated; tooltip with hosts). LiveStream Agent — market-data
  footnote names Yahoo Finance via yfinance (unofficial, not affiliated, personal use).
- **L6 follow-up** (new `Shared/RotatingLogFile.cs`): the backend process.log written by the Global Monitoring WPF
  and by NewsroomBackend.cs is rotated at 5 MB (process.log.1…3, oldest dropped); the detached Hedge Fund backend's
  -LogFile is rotated before each start. A failed rotation is written into the log, writing continues.
- **Minor**: LiveStream Agent health wait uses a 2 s per-request HttpClient timeout (was the 100 s default), client
  and responses disposed.

**World-atlas vendoring (addendum, round 2).**
- New `Global Monitoring/tools/fetch_world_atlas.py` (stdlib only, same scheme as fetch_engine.py): pinned
  world-atlas@2.0.2, registry metadata integrity + tarball URL == pin, tarball sha512 == pin (three-way), extracts only
  package/land-110m.json (checked: TopoJSON with objects.land) + the licence file, writes `Global Monitoring/vendor/`
  + MANIFEST (`<file> <version> <source_url> <sha256> <fetched_at>`), old files → *.stale. Pinned integrity
  `sha512-IXfV0qwlKXpckz1FhwXVwKRjiIhOnWttOskm5CtxMsjgE/MXAYRHWJqgXOpM8IkcPBoXnyTU5lFHcYa5ChG0LQ==` (registry, read
  2026-10-24 via a web reader; the script stops if the registry disagrees, override only via --expect-integrity).
- GlobalMonitoring.csproj embeds vendor/land-110m.json, MANIFEST, LICENSE* only when present (rebuild after the tool).
- MainWindow decides one source per page load and logs it: Vendored (sha256 == MANIFEST, version 2.0.2, licence
  present → JSON sent to the page by host message; CDN removed from CSP connect-src and the resource allow-list) /
  Cdn (no vendored files in the build → pinned CDN URL only) / VendorRejected (any check fails → hand-drawn outlines,
  AppLog error, CDN NOT used). The page shows the source in a line under the globe ("Coastlines: … — vendored copy
  (sha256 checked)" / "… — pinned CDN file (no vendored copy in this build)" / "hand-drawn outlines — …reason").

### 23.6 S10-E ".env removed, Credential Manager only"

Python contract: `UFOS.ai/docs/tasks/ENV_API.md` (Agent H; secrets only from the Windows Credential Manager, tunables from
`backend/data/settings.json` per backend, strict loading, no `.env` read anywhere; the process-environment
deployment switches stay); `core/llm.py` reads no environment (§22.13, ENV_API §9).

**Python part (Agent H; labels `stage10env`, `stage10lock`, `stage10git`; details ENV_API.md + ENV_API_DEFAULTS.json,
LOCK_API.md).**
- No backend reads `.env` or a secret from the environment (guard tests in all three backends). Keys: Windows
  Credential Manager only — HedgeFund `strategy_platform/secrets.py`, GM / LSA `credstore.py` (byte-identical); ONE
  shared `openrouter-api-key` (= the existing HedgeFund entry). Non-secret settings: optional
  `<backend>/data/settings.json` via `settings_file.py` (byte-identical ×3; strict — an invalid file refuses the
  start; the AI fields are re-read per call and an invalid file raises `settings_file.SettingsError` from
  `newsroom.ai._current_settings()` / `analysis_models()`, which `core/llm` turns into `LlmUnavailable`). LSA
  `youtube_url` is written by the LiveStream Agent window (atomic write + backend restart). GET /v2/settings/keys
  without `env_names` / `env_value_ignored`; POST /v2/settings/keys/changed also audits the GM / LSA key names
  (`secrets.OTHER_MODULE_SECRETS`); `ImportSpecsMatchSchemasTest` ties W's `BackendSettingSpecs.cs` to the three
  `config.SCHEMA` tables. python-dotenv removed; `.env.example` → `.env.example.stale`; READMEs updated.
- **Pinned dependencies:** `make_lockfile.py` (×3, identical) writes `requirements.lock` (+ HedgeFund
  `requirements-broker.lock`) with `==` pins and sha256 per package; `run_*.ps1` install only
  `--require-hashes -r requirements.lock`; "Update Python packages" = `make_lockfile.py --update` + install. The user
  runs `make_lockfile.py` once per backend (no PyPI from the agent machines).
- **Root .gitignore:** `/docs/` (root only) + `/UFOS.ai/docs/todo.md`; `HedgeFund/docs/` and
  `UFOS.ai/docs/ai-budgets-spec.md` versioned; global `*.stale` and `*-backup/`.
- **Docker removed (user 2026-10-24 16:56):** Dockerfile + .dockerignore ×3 and docker-compose.yml → `*.stale`; every
  backend listens on 127.0.0.1 only (ALLOW_REMOTE_ACCESS / WS_HOST / HTTP_HOST removed).
- `tools/check_no_env_cs.py`: the exemptions are the exact UFOS.ai-relative paths of the importer only
  (`EnvMigration.cs`, `EnvMigrationDialog.cs`, `BackendSettingSpecs.cs`); `scan(root, prefix)` so a HedgeFund-only
  checkout matches them too.
- Test isolation: `test_fund_api` / `test_s10_security` drop a stubbed `newsroom.ai` with `_drop_module()` (pop +
  the parent package attribute), so later imports never see the stub.

**WPF / C# part (Agent W; labels `stage10envui`, `stage10lockui`, `stage10secui` amended).**
- One Credential Manager layer for every WPF module: `Shared/CredentialVault.cs` (write / delete / state / constant-time
  compare / stored length / ReadValue — the latter only for code that must send a key itself: the main app's OpenRouter
  call). `Shared/CredentialCatalog.cs` lists ALL credentials (ENV_API §2): the Hedge Fund names unchanged, shared
  `openrouter-api-key` (target `UFOS.ai/HedgeFund/openrouter-api-key`, user decision 16:25), `opensky-client-id/-secret`
  (`UFOS.ai/GlobalMonitoring/…`), `deepgram-api-key` (`UFOS.ai/LiveStreamAgent/…`), retired kraken-demo-* (removal
  only). HedgeFund `Platform/CredentialStore.cs` keeps its API and delegates to the vault.
- ⚙ Keys & data lists every credential with "Used by: …"; the "found in .env" status / legacy-name line are gone
  (GET /v2/settings/keys without env_names / env_value_ignored).
- Main app: `Services/EnvConfig.cs` retired (.stale); `OpenRouterClient` reads the shared key from the Credential
  Manager and base URL / model from `%LOCALAPPDATA%\UFOS.ai\app-settings.json` (`Shared/MainAppSettings.cs`: optional,
  strict — unknown key / wrong type / key-like name → readable error; defaults = the former code defaults).
  `UFOS.ai/.env.example` retired (.stale).
- One-time migration "Import from old .env files…" (`Platform/EnvMigration.cs` + `EnvMigrationDialog.cs` +
  `BackendSettingSpecs.cs`) — the ONLY code that reads a .env: discovers every .env (UFOS.ai folder recursively without
  bin/obj/.venv/node_modules/.git, plus a .env in the repo root), ONE dialog with names only (keys: new / same / differs
  → keep or replace / several values → pick a file / unreadable stored → replace; settings: moved only when different
  from the code default; process switches dropped; unknown names dropped unless ticked "keep in file"); apply writes
  each key and verifies by read-back (equal value + equal byte length), writes settings atomically and re-reads them,
  then rewrites each .env without the handled lines or DELETES it when nothing is left; no backups; result shown in the
  dialog, log lines + POST /v2/settings/keys/changed "imported" per imported key (names only).
- LiveStream Agent window: "YouTube URL" field + "Save & restart" (validates https + youtube.com / www / m / youtu.be,
  no user info, ≤ 500; writes backend\data\settings.json youtube_url atomically via `Shared/BackendSettingsFile.cs`,
  restarts the backend); shows the stored value / why it is missing at window load.
- Texts: "set it in ⚙ Keys & data" (ArticleWindow, Economic calendar, Insider trades, key dialogs).
- Guard: `HedgeFund/tools/check_no_env_cs.py` + `backend/tests/test_s10_env_cs_guard.py` — no C#/XAML/csproj reads a
  .env path, a dotenv library, EnvConfig or a secret-like environment variable (EnvMigration files excepted).

**Lock protocol (C#).**
- `Shared/PythonEnvironment.cs`: stamp = SHA-256 of requirements.lock (requirements.txt before the first lock); install
  only `pip install --require-hashes -r requirements.lock`; missing / no source line / stale lock → stop with the
  same texts as Get-LockProblem (exact make_lockfile.py command). `Services/PythonEnvManager.cs`: same stamp; status
  names the lock problem; "Update Python packages" = run_*.ps1 -Upgrade -EnvOnly (H's scripts: --update + lock install).

**L6 follow-up (S10-A2).**
- `Shared/RotatingLogFile.cs`: process.log of Global Monitoring and the Hedge Fund backend rotate at 5 MB, 3 old files;
  the detached HF backend's -LogFile is rotated before each start; a failed rotation is written into the log.

### 23.7 Open decisions / follow-ups (from the batch notes)

Status 2026-10-24 (lead): CLAUDE.md — RESOLVED (updated by the lead); README — IN PROGRESS (the lead); Docker —
RESOLVED (removed, Agent H, §23.6); lock files — RESOLVED (`make_lockfile.py` exists; the user runs it once).

- S10-E (W): 1. CLAUDE.md still says secrets live in `.env` (lines ~64, 70, 97, 100) — **resolved: updated by the lead**.
  2. Repo-root README.md setup steps 2 (copy backend/.env.example) — needs: "Keys: ⚙ Keys & data (Windows Credential
     Manager); settings: backend\data\settings.json (optional)". Outside my labels — **in progress: the lead edits the README**.
  3. POST /v2/settings/keys/changed must accept the GM/LSA catalog names (opensky-*, deepgram-api-key) or those imports
     show "not recorded in the backend audit log" (H).
  4. BackendSettingSpecs = defaults transcribed from today's config.py (ENV_API_DEFAULTS.json not there yet) — H please
     confirm keys/types/defaults match the settings schemas.
  5. Docker — **resolved: Docker was removed (user decision 2026-10-24 16:56, Agent H)**.
  6. CODEMAP §4.1 (secrets) — H's names; I updated the Shared / Platform rows only.
- S10-A1: M3 lock files — **resolved** (`make_lockfile.py`; the user runs it once); L7 (old `fund/` wording — goes with S10-C); process.log cap (done WPF-side,
  §23.6 L6); GM / LSA need a start on the PC (their venvs cannot run on the agent VM).
- S10-A2: the startup notice body (`Shared/AiDisclaimerWindow.cs`, "No investment advice: …") is user-mandated —
  user decision whether it follows the Stage 10 wording.
- S10-U: MainWindow, Global Monitoring, LiveStreamAgent, `AiDisclaimerWindow.cs` keep their own notice wording outside
  `stage10ui` — a later label if the user wants them changed.
