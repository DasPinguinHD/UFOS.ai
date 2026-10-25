# UFOS.ai — rules for Claude (the single rule source)

UFOS.ai is a **proof-of-concept portfolio project, not a published product**: never imply production-readiness; no
real trading or commercial use. Built "shippable in theory, never shipped": product-grade quality, licensing-safe
tool design (the user builds, checks, decides), at most the user's own money and accounts, no features that only serve
other users (background: Claude Project `claude/project-direction.md`, `claude/conventions.md`).

## Start of a session — read these first

1. `UFOS.ai/docs/todo.md` — the ONE task list (open items, priorities). Done items: `UFOS.ai/docs/todo-archive/`.
2. `UFOS.ai/docs/tasks/` — open task / contract files; `HANDOFF.md` — current hand-over (when present).
3. `UFOS.ai/HedgeFund/docs/CODEMAP.md` — file map (read it instead of re-exploring the tree).
4. The governing contract before changing code: `HedgeFund/backend/strategy_platform/SPEC.md` (SP),
   `HedgeFund/backend/market_research/SPEC.md` (MR), `HedgeFund/backend/fund/SPEC.md` (old engine, until R4),
   `UFOS.ai/docs/ai-budgets-spec.md`. Each SPEC states the CURRENT contract; history is in its `spec-archive/`.
5. Plans live in the Claude Project ("Finance Monitoring System", `claude/*.md`); git-ignored local copies in the repo
   root `docs/`.

## Working with the user

- **Interview before implementing** (2026-09-25): for anything non-trivial ask focused questions with options and a
  recommendation (weak spots, edge cases, retention, migrations, error states, AI labelling, cost, performance, UX,
  security), then state the plan and build. Skip only for trivial edits or when the user says so. Applies to
  autonomous runs too.
- **Small steps, checked understanding** (2026-10-03): several small pieces per prompt; after each, check the user
  understood (comprehension question / "as expected?" / choice with recommendation) before going on.
- **Users are professionals** (fix1023, 2026-10-23): one confirmation step with a precise summary and button, optional
  notes; no "I understand" checkboxes. Exceptions: the live-trading switch keeps a typed confirmation; gate exceptions
  need a mandatory reason (≥ 30 characters); opening the holdout currently needs the typed strategy name (open user
  decision, see todo).
- Language: UI text, code comments, docs, commits — English unless the user asks otherwise. Reports to the lead /
  user short.
- Sub-agents allowed at your discretion (2026-09-25); same rules for them; verify their results before reporting.

## Hard rules

- **No unrequested fallbacks** (2026-10-02): never add a silent alternative (stand-in for an AI result, other data
  source, cached / older value, default, cheaper model, degraded mode). Stop that function and say what happened and
  what the user can do. A fallback only when the user asked for it or accepted your proposal — record date + place in
  the code comment and docs; list (never remove silently) existing ones you notice.
- **Secrets:** only in the Windows Credential Manager (entered via ⚙ Keys & data; one shared OpenRouter key). Backend
  access only via `strategy_platform/secrets.py` (GM / LSA `credstore.py`). Never read, print, log, commit or show a
  key; no code reads `.env` or secrets from environment variables (retired 2026-10-24, guard tests). Non-secret
  settings: each backend's optional strict `backend/data/settings.json`.
- **Security by default** (2026-09-25): local servers on `127.0.0.1` only; Host-header check + session token
  (`X-UFOS-Token`, `backend/data/api-token`) on every endpoint with side effects, cost or data; no CORS `*`; TLS with
  verification for every outbound call; validate all external input; open only http(s) links; no shell execution of
  untrusted strings; escape everything rendered; rate / cost limits; safe error messages; no secrets in logs; pinned
  dependencies (`requirements.lock`, `--require-hashes`). New endpoints are protected automatically; new clients must
  send the token. The three `api_auth.py` / `ai_caps.py` / `settings_file.py`
  copies stay identical. In the interview always ask how a security-relevant feature is secured and name residual
  risks before building and when reporting, and offer the user to have it hardened further.
- **Trading guardrails** (background `claude/conventions.md` "Autonomous trading"): paper / demo only — live trading
  stays OFF (hard-coded live guard; stage 12 only on the user's explicit go); auto trading off at every backend start;
  hard limits; a visible, pinned kill switch that cancels open orders; dead-man switch (a truly headless autopilot needs
  an explicit, separate opt-in); user actions are refused when their audit row cannot be written; every decision / order logged locally with provenance, never
  committed; no other people's money or accounts, no copy-trading, no public signal / P&L feed, no market-abuse
  patterns; broker keys only in the Credential Manager.
- **Real databases:** never open a real `data/*.db` over the VM mount — copy db (+ `-wal`) to a local temp dir, verify
  sha256 before = after, open the copy. Tests never touch real DBs. A switch-over / migration on the user's
  hedgefund.db happens only when the user triggers it in the app.
- **No deleting:** rename to `*.stale` instead; deletion only in a lead-approved step with the user's permission.
- **Git:** commits / pushes only when the user asked for them; the lead session commits, sub-agents use read-only git
  only. Never rewrite published history without the user's explicit go.
- **Public repo — no third-party data, no personal data** (2026-10-24, after a real-price test fixture had to be
  purged from the history): never commit market data (prices, bars, quotes, option chains, volumes), news or article
  text, provider responses, filings exports, databases, caches or exports — not even as small test fixtures. Test data
  is synthetic (generated, deterministic); tests that need real data export it at run time from the user's own DB into
  a git-ignored folder (`backend/data/local_fixtures/`) and skip when it is absent. Vendored third-party code / assets
  only under `vendor/` with licence + sha256 `MANIFEST`. No personal data either (account values, positions, broker
  details, private links). The pre-commit data check (`HedgeFund/tools/check_repo_data.py`) must pass before every
  commit. Each data source's terms are listed in the README ("Data sources & licensing").
- **Scope:** agents change only `UFOS.ai/HedgeFund/**`; anything else needs a scope label (allow-list JSON →
  `snapshot` → edit → `check` → `close`) via `HedgeFund/tools/scope_guard.py`; run the guards named in the task (e.g.
  stage0, mr0, budget0) at the end. A closed label needs a new label.
- **Editing:** re-read a file before editing it; exact-match edits or python read-modify-write — never re-type a file
  from truncated tool output. When others work in parallel, do not "fix" failures in areas you did not touch.

## AI features

- **Cost:** every OpenRouter call appends one line to `%LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl` (`ts, module,
  feature, feature_label, provider, model, cost_usd, prompt_tokens, completion_tokens, status`; each module its own
  writer; cost = the response's `usage.cost`, OpenRouter credits = USD) and button-triggered outputs show "Cost: $… · N
  in / M out tokens".
- **Budgets:** every backend exposes `GET/PUT /ai/budgets` (token, PUT refused with an `Origin` header) and enforces
  its caps BEFORE each call, fail closed; a new AI feature needs a budget entry; a new module plugs in via
  `UFOS.ai/Shared/AiBudgetProviders.cs`. Backend values are authoritative; cost-log totals are shown only labelled
  "from the cost log". Contract `UFOS.ai/docs/ai-budgets-spec.md`.
- **Labelling (EU AI Act Art. 50) — every AI output:** (1) trigger button starts with `✦` + `AiContent.TriggerTooltip`;
  (2) badge before the text `✦ AI-GENERATED · <model> · <HH:mm>` (`AiContent.Badge`, also while loading); (3) context
  notice after it — `AiContent.AnalysisNotice` (= `Caveat`) for analyses, `ResearchNotice` for strategies / committee /
  loop finalists, `PaperExecutionNotice` for autonomous paper execution (the startup notice's "No investment advice"
  wording is an open user decision — don't change it without that); (4)
  violet container (`AiAccentBrush` / `AiSurfaceBrush`, #B98CE8); (5) backend responses with LLM text carry a
  `provenance` object (`ai_generated, generator, provider, model, generated_at, human_reviewed: false, label`), kept by
  frontends; (6) screen-reader text starts with "AI-generated"; (7) failures show an error, never canned analysis;
  samples say "illustrative"; (8) history keeps the original model / time; (9) no hidden watermarks. Shared code
  `UFOS.ai/Shared/AiContent.cs` (+ backend `core/notices.py`, same texts; `globe.html` mirrors them — keep identical).
- **Startup notice** (`Shared/AiDisclaimerWindow.cs`): shown at every start of the app and each module exe, continue
  only after "I agree and want to continue" (closing it exits), re-readable in Settings; the only opt-out is the
  Settings toggle, effective only after agreeing to the current version (bump `NoticeVersion` when the text changes —
  that resets opt-outs). No "don't show again" shortcut in the dialog; never make per-output labels switchable; never
  remove or soften the notice.

## UI / WPF

- The MainWindow shell uses the shared `UFOS.ai/Styles/DesignSystem.xaml`; it may feel "nerdy" (terminal /
  Bloomberg-key language) — don't polish it generic. Modules stay independent (own `.csproj`, backend, settings).
- Don't change other windows' design as a side effect. New module windows follow the LiveStreamAgent borderless dark
  layout with the exact minimize / close buttons.
- **Borderless windows** (`WindowStyle="None"`): `StateChanged` pads the root border by
  `SystemParameters.WindowResizeBorderThickness` + 4 and zeroes the corner radii while maximized; the title bar
  restores a maximized window only when the mouse moves with the button held, keeps the cursor's relative spot, then
  `DragMove()`. Reference `Window_StateChanged` / `TitleBar_MouseMove` in `HedgeFund/HedgeFundWindow.xaml.cs` and
  `HedgeFund/Newsroom/FinancialNewsroomWindow.xaml.cs`. Check it for every new window.
- **Module tiles** (2026-10-24): every module provides Live (2–3 key figures; never starts a backend or a paid call),
  Last view (stored only blurred, never in the repo) and Illustration, selectable via "⋯".
- **Full screen** (2026-10-24): every chart, map and large table gets "⛶" (Esc exits, F11 while focused); same
  instance, state kept; one shared building block in `Shared/`.
- Untrusted text is plain text; missing data reads "not available (reason)".

## Python backends

- Environments are prepared once at app start (`Shared/PythonEnvironment.cs`): venv + `pip install --require-hashes -r
  requirements.lock` only when the lock changed (lock `backend/.venv.lock`); a missing / stale lock stops with the
  `make_lockfile.py` command. Every launcher awaits `PythonEnvironment.WaitForModuleAsync(...)` (and does not start on
  `false`) and passes `-SkipInstall` to its `run_*.ps1`.
- Tools in `backend/tools/` run only with the backend venv (`.venv\Scripts\python.exe tools\…`).
- A `dotnet build` never restarts a running Python backend — restart `python.exe` after a Python change.
- No Docker.

## Tests

- Python `unittest`, no network (fakes), temp dirs; code runs on Python 3.10 (VM) and 3.13 / 3.14 (Windows venv).
- While working: only the touched modules, in chunks ≤ 170 s on the VM. Full suite once at the end, preferably in the
  cloud container: tar the HedgeFund tree (+ GM / LSA `ai_caps.py`) without data / secrets into
  `HedgeFund/backend/data/agent_transfer/`, stage it, `python3 -m unittest discover -s tests -p 'test_*.py'`. Never two
  full suites on the VM at once.
- WPF cannot be built by agents: static checks only; the user builds and click-tests.

## Docs upkeep

- One todo: `UFOS.ai/docs/todo.md` (open items only; move done items verbatim to `todo-archive/<yyyy-mm>.md`).
- SPECs: change the current contract together with the code; keep § numbers stable; history, review rounds and
  as-built narratives go to `<dir>/spec-archive/` (one file per stage). Every `strategy_platform` extension made for
  Market Research is recorded in both SPECs.
- CODEMAP: one line per file, current state; update it in the same change when adding / moving / retiring a file.
- No scratchpad or `/tmp` paths in repo docs — refer to `UFOS.ai/docs/tasks/<file>`.
