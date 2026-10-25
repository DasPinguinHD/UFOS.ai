# UFOS.ai — common brief for sub-agents (recreated 2026-10-23)

Project: UFOS.ai (proof-of-concept portfolio project), WPF .NET 10 + Python backends, user's Windows PC.
Repo on the device: `$HOME/mnt/UFOS.ai/UFOS.ai` (Windows: `C:\Users\leonh\source\repos\UFOS.ai\UFOS.ai`).
Access only via mcp__remote-devices__device_bash (load with ToolSearch "select:mcp__remote-devices__device_bash");
each call ≤ 170 s; background processes do not survive the call.

Rules (user, standing):
- Never read or print `.env` secret values or any key; keys live in the Windows Credential Manager only.
- No git — never run git commands.
- Change only `UFOS.ai/HedgeFund/**` plus explicitly approved exceptions (scope labels); run
  `python3 HedgeFund/tools/scope_guard.py check --label <label>` for the labels named in your task (usually stage0, mr0,
  budget0) and any new label you were told to create. A closed label needs a NEW label for new exceptions.
- Never delete files: rename to `*.stale` instead (deleting in connected folders is not permitted anyway).
- NEVER add unrequested fallbacks (no stand-in data, no silent defaults); a failure stops the function with a clear message.
- Paper/demo only; live trading stays off (hard-coded). No real orders.
- Edit files with python read-modify-write or sed — never re-type files from truncated tool output.
- ONE todo file: `UFOS.ai/docs/todo.md` (open items only; done items → `UFOS.ai/docs/todo-archive/<yyyy-mm>.md`). The
  root `docs/todo.md` copy is retired (DOCS-SLIM 2026-10-24). Rules: `CLAUDE.md` (single rule source).
- No scratchpad or `/tmp` paths in repo docs — refer to `UFOS.ai/docs/tasks/<file>`.
- Contract first: SPEC `HedgeFund/backend/strategy_platform/SPEC.md` (Market Research: `HedgeFund/backend/market_research/SPEC.md`).
- UI text, code and docs in English; reports to the lead in English, short.

- NEVER open a real DB (`data/*.db`) with SQLite from the VM over the mount (disk I/O error + stray -wal/-shm next to
  the live DB, 2026-10-24). Copy the file first, verify sha256 before = after the copy, open only the copy.
- File deletion is enabled for this session (user, 2026-10-24) ONLY for lead-approved manifests — otherwise still
  rename to *.stale; never delete anything on your own.

Testing:
- Python tests: VM Python 3.10 + unittest (numpy, pandas; no scipy), from `HedgeFund/backend`:
  `python3 -m unittest discover -s tests -p 'test_sp_*.py'` etc. — run in chunks ≤ 170 s (list modules explicitly).
  Code must also run on Python 3.14 (Windows venv). No network in tests (fakes). Temp dirs outside the repo; never touch the
  user's real DBs (`HedgeFund/backend/data/*.db`) — use copies in a temp dir and remove them afterwards.
- WPF cannot be built here: static checks only (usings, ambiguous types, nullable, XAML names). The user builds.
- (test_sp_committee / test_mr_drawings_split were made deterministic in S10-H.)

Test discipline (user decision 2026-10-23 — the VM has only 2 cores / 3.8 GB and is shared by all agents):
- While working, run ONLY the test modules your change touches (list them explicitly). Run the FULL suite once at the
  end — and preferably in the cloud container (below), not on the VM.
- **Offload tests to the cloud container** (own 2 cores / 7.8 GB, Python 3.13 + numpy/pandas; also must stay
  Python 3.10-compatible on the VM — keep syntax conservative):
  1. VM (device_bash), bundle the HedgeFund tree (+ the two ai_caps.py copies a test compares) without data/secrets:
     `cd "$HOME/mnt/UFOS.ai/UFOS.ai" && tar --exclude='./HedgeFund/backend/data' --exclude='./HedgeFund/backend/.venv' --exclude='__pycache__' --exclude='./HedgeFund/backend/.env' --exclude='bin' --exclude='obj' -czf HedgeFund/backend/data/agent_transfer/hf_<yourid>.tgz ./HedgeFund "./Global Monitoring/backend/ai_caps.py" ./LiveStreamAgent/backend/ai_caps.py`
     (`data/agent_transfer/` is gitignored; overwrite your own file each round; never include .env or *.db.
     *.stale files ARE included on purpose — a test reads verify_kraken_demo.py.stale.)
     Note: other agents may be editing at the same time — a failure in an area you did not touch may be their
     work in progress; check before "fixing" it.
  2. Stage it: device_stage_files path
     `C:\Users\leonh\source\repos\UFOS.ai\UFOS.ai\HedgeFund\backend\data\agent_transfer\hf_<yourid>.tgz`
     → appears at `/mnt/user-data/uploads/UFOS.ai/UFOS.ai/HedgeFund/backend/data/agent_transfer/hf_<yourid>.tgz`
     (wait ~2 s).
  3. Container (your Bash tool): extract into YOUR OWN dir `scratchpad/hftest_<yourid>/` (rm -rf it first; the
     archive contains ./HedgeFund, ./Global Monitoring, ./LiveStreamAgent), `cd .../HedgeFund/backend`, run
     `python3 -m unittest discover -s tests -p 'test_*.py'`. The full suite takes ~4–6 min → run it with
     `run_in_background` (or split by pattern into chunks < 10 min). Tests must not need network or real DBs.
  4. Fix on the VM (the repo there is the source of truth), re-bundle, re-run only what failed.
- Never run two full suites at the same time on the VM.
- **Code map: `HedgeFund/docs/CODEMAP.md` (exists since 2026-10-23) — READ IT FIRST** instead of re-exploring;
  update it in the same change when you add, move, rename or retire a file (one line per file).
