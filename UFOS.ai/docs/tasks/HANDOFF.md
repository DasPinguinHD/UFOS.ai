# Hand-over — 2026-10-25 (lead session → cloud sessions)

Read first: `CLAUDE.md` (rules), `UFOS.ai/docs/todo.md` (priorities), this folder's `README.md` (task files),
`UFOS.ai/HedgeFund/docs/CODEMAP.md` (file map), the SPECs (current contracts; history in `spec-archive/`).

## Working model from now on
- **Cloud session** works in a clone of `github.com/DasPinguinHD/UFOS.ai` (branch `main`): code, Python tests, docs,
  commits — always on the user's request, never history rewrites without the user's explicit go.
- **The user's PC** (link the session to it when needed): WPF build + click-tests, anything with real data (DBs under
  `backend/data/`, the Credential Manager, the options archive task, `make_lockfile.py`, `verify_*` tools).
- The user only pulls, builds and tests in Visual Studio; local edits on the PC are announced first.
- Before every commit: `python HedgeFund/tools/check_repo_data.py` (no third-party / personal data), the touched
  tests, and the scope guards named in the task. Merge `main` → `master` via PR (the Claude Project syncs `master`).

## State at hand-over
- Committed + pushed (new history after the 2026-10-24 purge of a real-price test fixture; new GitHub repo):
  stage 10 incl. Council R1–R3 + review fixes, Credential-Manager-only secrets, lockfiles, security audit fixes.
- In the commit of 2026-10-25: docs slimmed (SPEC current + `spec-archive/`, one todo + `todo-archive/`, CODEMAP),
  CLAUDE.md as single rule source (now versioned), task files here, README "Data sources & licensing", licence
  clean-up (synthetic parity bars, `check_repo_data.py`, local real-data variant + export tool), FRED API notice,
  full-screen building block + 6 places, Global Monitoring `GET /events/summary`, INSIDER-RET backend slices a–c.
- TILES wired 2026-10-25 (cloud session, label tiles0; see `TILES_API.md` §11): all tile files compiled in, Hedge Fund
  Live figures from `GET /v2/portfolio/summary`. Not built by an agent (nuget.org is blocked in the cloud container) —
  the user's VS build + click-test (todo §1 "TILES") come next, together with the open full-screen click-test points.

## Open decisions / dates
- **Mon 2026-10-26:** options RTH check `verify_alpaca_options.py` (14:30–21:00 Berlin); the old engine's 2 queued
  orders fill at the open → then the user runs the migration to `paper-council` (**irreversible**) and the Council
  switch-over (revertible until R4). Nothing is cancelled by hand.
- User decisions still open: see `todo.md` §2 (holdout typed name, startup-notice wording, budget0 re-freeze,
  module.json).
- LiveStream Agent: official sources instead of YouTube (decided) — source list to be agreed with the user first
  (`LSA_SOURCES_TASK.md`).

## Next work (order)
1. User: build + click-test TILES (and the remaining full-screen places); fix build errors first.
2. INSIDER-RET UI ("Disclosures" section with Insider / Congress / Track record tabs) → CONGRESS backend + UI.
3. LSA-SRC (interview first).
4. R4/R5 after the switch-over and its click-test; then stage 11, Depth & Range, OS2–OS5 (todo §4).
