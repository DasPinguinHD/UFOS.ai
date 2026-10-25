# LOCK_API — pinned, hash-checked Python dependencies (Agent H, 2026-10-24) — for W (C#) and P (docs)

User decision 2026-10-24 16:15: lock files with `==` pins + `--hash` for all three backends; installers use
`pip install --require-hashes -r requirements.lock`; "Update Python packages" regenerates the lock deliberately.
Labels: HedgeFund files in-module; GM / LSA `run_backend.ps1`, `make_lockfile.py`, `requirements.lock` → `stage10lock`.

## Files
- `make_lockfile.py` — byte-identical in `HedgeFund/backend/tools/`, `Global Monitoring/backend/`,
  `LiveStreamAgent/backend/` (test: HedgeFund tests/test_s10_lockfile.py). Stdlib only; must run with the backend's
  OWN `.venv\Scripts\python.exe` (refuses otherwise, exit 2). Needs internet (PyPI).
  - default ("installed"): pins exactly the versions installed in the venv (`pip freeze` as constraints) →
    first-time lock = what is installed and tested today;
  - `--update`: fresh resolution, newest versions requirements.txt allows (what "Update Python packages" runs);
  - `--check`: exit 1 when a lock is missing or stale.
  - Resolution: `pip install --dry-run --ignore-installed --report` (pip ≥ 23) → full closure for THIS
    Python/platform, each line `name==version --hash=sha256:<hex>`; an entry without sha256 → nothing written (exit 4).
  - HedgeFund also writes `requirements-broker.lock` (ccxt set) from a JOINT resolution with the main set — see
    "Main + broker: one venv, joint resolution" below.
  - Exit codes: 0 ok · 1 `--check` found a missing / stale / not installable lock · 2 usage · 3 pip failed for the
    MAIN set (nothing written) · 4 a main package without sha256 (nothing written) · 5 requirements.lock written
    ALONE, the broker set cannot be resolved together with it (requirements-broker.lock names the reason).
- `requirements.lock` (+ HedgeFund `requirements-broker.lock`) — committed next to requirements.txt. Header line
  `# source: requirements.txt sha256=<lower-case hex>` = the requirements file the lock was made from.

## Installer protocol (run_*.ps1 — done; C# must match)
- Stamp `.venv\ufos-requirements.sha256` = SHA-256 (upper-case hex, like Get-FileHash) of **requirements.lock**
  when it exists; before the first lock: of requirements.txt (so an installed venv keeps starting until the lock
  exists). Install reason text: "requirements changed".
- An install (required, or -Upgrade) needs a CURRENT lock: missing → `UFOS_ENV: failed requirements.lock missing -
  run once (needs internet): .venv\Scripts\python.exe <tool>`; source sha ≠ sha256(requirements.txt) →
  `UFOS_ENV: failed requirements.lock is out of date (requirements changed) - run: … --update`. Never an unpinned
  install.
- Install command: `python -m pip install --require-hashes -r requirements.lock --disable-pip-version-check
  --no-input --progress-bar off` (no `--upgrade` anywhere).
- `-Upgrade` ("Update Python packages", root app → `-Upgrade -EnvOnly`): first `.venv\Scripts\python.exe <tool>
  --update` (exit ≠ 0 → `UFOS_ENV: failed could not regenerate requirements.lock …`), then the install above;
  "changed N" marker as before (freeze diff).
- `-Upgrade` and make_lockfile exit **5**: requirements.lock was regenerated alone → the main install still runs
  (the main environment never fails because of ccxt); the log says the broker set cannot be installed with it.
- HedgeFund optional broker step: from `requirements-broker.lock` with --require-hashes; status stamp = its hash
  once it exists; missing / stale lock → status "failed <hash> <utc> <reason>" (only binance-demo affected).
  Additionally refused (same status line, reason ≤ 280 ASCII chars): no `# with: requirements.lock sha256=<hex>`
  line, a `# with:` hash ≠ SHA-256 of the current requirements.lock ("made for another requirements.lock - run …
  --update"), or a `# conflict: <reason>` line ("ccxt cannot be installed together with requirements.lock: <reason>").
  <tool> = `tools\make_lockfile.py` (HedgeFund) / `make_lockfile.py` (GM, LSA).

## Main + broker: one venv, joint resolution (fix 2026-10-24, user's first run)
Bug: the user's first run wrote requirements.lock (installed mode, certifi==2026.10.19) and then failed the broker
set with ResolutionImpossible: ccxt 4.5.84 pins certifi==2026.6.17 (and every ccxt release pins certifi and other
shared packages EXACTLY), while the broker set was resolved with the main pins as constraints. The venv itself was
already inconsistent (`pip check`: ccxt wants certifi 2026.6.17). Under the old code `--update` ("Update Python
packages") failed the same way after it had already rewritten requirements.lock (exit 3 → the ps1 skipped the
install, the next start installed the new main lock, ccxt stayed broken) — every ccxt update would repeat it.

Choice (the lead's option 1, plus a visible "not installable" broker lock):
1. `alone` = requirements.txt resolved on its own (installed mode: freeze pins as constraints; --update: fresh).
   Fails → exit 3/4, nothing written (as before; this is the only way the main lock can fail).
2. `joint` = `-r requirements.txt -r requirements-broker.txt` resolved together, same constraints.
3. Joint OK → requirements.lock = requirements.txt resolved with ALL joint pins as constraints, requirements-broker.lock
   = requirements-broker.txt resolved with the joint pins — i.e. each file's closure taken from the one joint
   solution (pip computes it, markers included); a sanity check refuses any version/hash that differs from the joint
   one. Shared packages (certifi, aiohttp, yarl, …) are identical in both locks → one consistent venv.
   Main header `# resolved: together with requirements-broker.txt`; in --update mode `# held-by-broker: certifi
   2026.6.17 (alone: 2026.10.19)` lists main packages the broker set holds below their newest allowed version.
   Broker header `# with: requirements.lock sha256=<hex of the main lock file>` ties it to that exact main lock.
4. Joint fails (any reason: conflict, no wheel for this Python, no sha256) → requirements.lock = `alone` with
   `# resolved: alone - requirements-broker.txt cannot be resolved together with it`, and requirements-broker.lock is
   written WITHOUT packages: `# with: …`, `# conflict: <pip's "conflict is caused by" lines in one line>`, `# NOT
   INSTALLABLE … - <what to do>`. Exit 5 and the same text on stdout. Never silent: `--check` reports "not
   installable (…)" (exit 1), the ps1 broker step writes it into the status line the binance-demo broker shows.
   What to do: installed mode → run `--update` (resolves both together; certifi etc. move to ccxt's pins);
   --update mode → requirements-broker.txt (another ccxt) or requirements.txt has to change.
- pip runs without `--quiet` (the "The conflict is caused by:" lines are printed only at normal verbosity).
- Verified offline with real pip 26.2.1 and local wheels (certifi 2026.6.17/2026.10.19, ccxt pinning
  certifi==2026.6.17, the user's inconsistent venv): installed → exit 5 naming `ccxt 4.5.84 depends on
  certifi==2026.6.17`; `--update` → exit 0, both locks certifi==2026.6.17, `--require-hashes` install of both → `pip
  check` clean; installed mode afterwards → exit 0; a requirements.txt that pins certifi==2026.10.19 → exit 5.
- Trade-off: a ccxt release holds shared packages at its exact pins in the MAIN lock as well (shown as
  `# held-by-broker`). The app's informational "updates available" dot (PythonEnvManager.cs, pip list --outdated)
  will keep listing such a package (e.g. certifi) — W may want to skip packages named in that header line.

## W (C#) — needed so the app's own environment preparation agrees
- `Shared/PythonEnvironment.cs` (app-start preparation) and `Services/PythonEnvManager.cs` (status / "Update Python
  packages" / daily check) still hash requirements.txt and run `pip install -r requirements.txt [--upgrade]`.
  Please switch to the protocol above: same stamp rule (lock if present), same lock check (header source line),
  install `--require-hashes -r requirements.lock`, refuse with the same message when the lock is missing/stale;
  "Update Python packages" = run the module's ps1 with `-Upgrade -EnvOnly` (or run `<tool> --update` + the install
  yourself). The daily online "updates available" check can stay informational (pip list --outdated), but an update
  is only ever applied through `--update` + lock install.
- Otherwise the app would see a stamp mismatch after the scripts ran (lock hash vs requirements.txt hash) and
  reinstall unpinned.

## Re-run after the joint-resolution fix (HedgeFund; overwrites the requirements.lock written 16:12Z)
```
cd <repo>\UFOS.ai\HedgeFund\backend
.venv\Scripts\python.exe tools\make_lockfile.py --update
```
(expected: exit 0, both locks with certifi==2026.6.17 and a `# held-by-broker: certifi …` line; then start the app
or run "Update Python packages" — the main install from requirements.lock moves certifi to 2026.6.17 and the broker
step installs requirements-broker.lock.) Alternative in one step: Settings → "Update Python packages" (runs
`run_hedgefund_backend.ps1 -Upgrade -EnvOnly`: `--update` + both installs). GM / LSA have no broker set (unchanged).

## One-time step for the user (no PyPI access from the agent machines)
After the S10-E python change (it removes python-dotenv from requirements.txt — a lock made before would be stale),
in a terminal, per backend folder:
```
cd <repo>\UFOS.ai\HedgeFund\backend          & .venv\Scripts\python.exe tools\make_lockfile.py
cd <repo>\UFOS.ai\Global Monitoring\backend  & .venv\Scripts\python.exe make_lockfile.py
cd <repo>\UFOS.ai\LiveStreamAgent\backend    & .venv\Scripts\python.exe make_lockfile.py
```
(default mode pins what is installed; then start the app — the stamp changes once and the venv is re-checked
against the lock with --require-hashes.) Known limit: build dependencies of an sdist-only package are not
hash-checked by pip.
