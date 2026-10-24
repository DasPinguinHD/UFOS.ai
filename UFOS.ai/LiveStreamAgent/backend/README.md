# Fed Agent

> Part of UFOS.ai, a proof-of-concept portfolio project (not a published product) — see the repo root README.

Autonomous pipeline that listens to a live Federal Reserve press conference / FOMC stream,
transcribes it in real time, and asks an LLM to interpret the remarks alongside live market
data to produce a structured long/short read across key asset categories.

## Pipeline

```
YouTube live stream (yt-dlp)
    -> ffmpeg (PCM16 mono, 16kHz)
    -> Deepgram streaming transcription
    -> finalized transcript segments
    -> DeepSeek v3 (via OpenRouter) + live market snapshot + scenario playbooks
    -> structured long/short/neutral stance per category
    -> WebSocket broadcast + JSONL log (for a future frontend)
```

Market data (Yahoo Finance quote pages) is scraped in the background on a fixed interval and
cached; each LLM call uses the latest cached snapshot.

## Asset categories & tickers

| Category | Tickers |
|---|---|
| Treasury yields | ^TNX, ^TYX |
| Bond proxies | TLT, AGG |
| REITs | VNQ |
| Utilities | XLU |
| Homebuilders | XHB, ITB |
| Tech giants | QQQ, MAGS |
| Unprofitable growth | ARKK |
| Financials | XLF |

## Setup

1. Install [ffmpeg](https://ffmpeg.org/) and ensure it's on your `PATH`.
2. `pip install -r requirements.txt` (or let `run_backend.ps1` do it — see
   "Python environment" below)
3. No `.env` any more (stage 10, 2026-10-24). Keys live in the Windows
   Credential Manager — enter them in the app under ⚙ Keys & data:
   - `deepgram-api-key` — Deepgram streaming transcription API key
   - `openrouter-api-key` — OpenRouter API key (ONE key shared by all modules)
   The YouTube URL of the live Fed press conference / FOMC stream is set in the
   LiveStream Agent window (stored as `youtube_url` in `data/settings.json`; other
   tunables there too — keys and defaults: `config.SCHEMA`).

## Running

```
python main.py
```

- Transcript, market, and analysis events are logged as JSON lines to `data/logs/<date>.jsonl`.
- The same events are broadcast live over a WebSocket at `ws://127.0.0.1:8765` (loopback only,
  session token required) for the LiveStream Agent window.

## Scenario playbooks

`data/scenarios/*.md` contain few-shot reference material (hawkish surprise, dovish pivot,
rate-decision surprises, balance sheet/QT commentary) describing historical market reaction
patterns. These are included in every LLM prompt to ground its interpretation.

## AI cost ledger (2026-09-25)

Every OpenRouter call this backend makes (the periodic **verdict** and the per-segment
**analysis**) is appended to the shared UFOS.ai cost ledger
`%LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl` (override with `UFOS_AI_COST_LOG` = full path) by
`cost_ledger.py` - one JSON line per call with `ts, module, feature, feature_label, provider,
model, cost_usd, prompt_tokens, completion_tokens, status`. `cost_usd` is OpenRouter's
`usage.cost` (credits; 1 credit = 1 USD). Writing never raises; failures are logged as warnings.
The line format is identical in every UFOS.ai module (each keeps its own small writer copy).

Verdict (and analysis) payloads now also carry an optional
`"usage": {"cost_usd", "prompt_tokens", "completion_tokens"}`; the WPF window shows it as a
small grey line under the reason ("Cost: $0.0021 · 812 in / 190 out tokens"), kept per entry in
the verdict history. Tests: `python -m unittest tests.test_cost_ledger -v` (from `backend/`).

## Notes / known limitations

- Yahoo Finance scraping has no fallback data source; if Yahoo changes its page structure or
  blocks requests, market snapshots will silently go stale for the affected tickers (see logs).
- No frontend is included yet — the WebSocket server and JSONL logs are the integration surface.

## Python environment (since 2026-09-25)

**Started from the UFOS.ai app:** the environment is prepared once at app
start (`Shared/PythonEnvironment.cs`, in the background, one module after the
other: create the venv if missing, `pip install -r requirements.txt --upgrade`
only when the stamp doesn't match). The module's launcher (`LivestreamAgent.xaml.cs`) awaits
`PythonEnvironment.WaitForModuleAsync` before starting the script with
`-SkipInstall` (no venv creation, no pip); a standalone module exe prepares the
venv itself at that point. The rules below apply when you run the script by hand
(it writes the same stamp, so the app doesn't reinstall afterwards).

`run_backend.ps1` no longer runs `pip install --upgrade` on every start. It
installs `requirements.txt` (without `--upgrade`) only when that is
**required**: `.venv` is missing, `requirements.txt` changed since the last
successful install (SHA-256 in `.venv/ufos-requirements.sha256`), or the last
install didn't complete (`.venv/ufos-install-pending`). `-RecreateVenv` still
rebuilds the venv; `-Upgrade` runs `pip install -r requirements.txt --upgrade`;
`-EnvOnly` prepares the environment without starting `main.py`.

Every run holds `.venv.lock` (git-ignored, deleted automatically — the handle
is opened with DeleteOnClose) while it checks or changes the venv, so the
backend never starts while an install/update of the same venv is running; it
waits instead. A leftover lock counts as stale when its PID is gone or it is
older than 30 minutes. The script prints `UFOS_ENV: waiting | installing <reason>
| upgrading | changed <n> | failed <reason> | ready` marker lines, which
`LivestreamAgent.xaml.cs` (connection status) turns into a status hint ("Installing Python packages…" /
"Waiting for the Python package update to finish…").

Optional upgrades are manual only: root app → Settings (gear) → "Update Python
packages" (upgrades all module venvs one after another; this backend has no `/shutdown` endpoint, so it is skipped while running — close the LiveStream Agent window first).

## Local storage (SQLite, 2026-09-25)

`storage.py` (the `Storage`/`Store` class) keeps what this backend produced across restarts. One
file per module (project convention "compartmentalization"; Global Monitoring and HedgeFund have
their own copies):

- **File:** `backend/data/livestream.db` (+ `-wal`/`-shm`; git-ignored). `UFOS_LSA_DB` (full file
  path) overrides it, e.g. for tests.
- **Behaviour:** WAL mode, one connection (`check_same_thread=False`) behind a lock, schema
  versioned via `PRAGMA user_version` (`storage.MIGRATIONS`, append-only). It never crashes the
  backend: every call logs its own SQLite error and returns a neutral default. A corrupt file is
  renamed to `livestream.db.corrupt-<UTC timestamp>` and a fresh database is created; if the
  file can't be used at all, an in-memory database is used for that session.
- **Startup:** retention runs, then the verdicts of the last 90 days (max. 500, the WPF history's
  cap) are restored into memory. Every newly connected WebSocket client gets the newest 20 as one
  `verdict_history` message (`{"verdicts": [...newest first, same shape as "verdict"...],
  "source": "local_storage"}`), plus the cached market snapshot; all of them are served
  read-only at `GET http://127.0.0.1:8766/verdicts?limit=100` and `GET /history/ai`.
  **Follow-up (WPF):** `WebSocketService.cs` ignores unknown message types, so the window's
  verdict history still starts empty after a restart until it handles `verdict_history` (or
  calls `/verdicts`) — the data is kept meanwhile.
- **While running:** every broadcast verdict and every successful per-segment analysis is stored
  by `llm_analyst.py` (failed/empty LLM results are not), transcript segments and quotes by
  `main.py`; a daily task re-applies the retention rules.

| Table | Content | Retention |
|---|---|---|
| `ai_outputs` | every verdict (`feature` "verdict") and analysis ("analysis"): `ts` (= provenance time), model, `input_json` (transcript + market snapshot), `output_text`, `provenance_json`, `usage_json` | 90 days |
| `items` source `transcript` | transcript segments (text) | 7 days |
| `items` source `quote` | latest quote per symbol (stored at most every 60 s) | 2 days |
| `prices` | `symbol, ts, price, source` — one point per symbol at most every 5 min and only when the price changed | 2 years |
| `source_state`, `meta` | last success/error of the quote feed; `last_retention_ts` | kept (one row each) |

**Reset:** stop the backend and delete `data/livestream.db` (plus `-wal`/`-shm`); it is recreated
empty on the next start.

Tests: `python -m unittest discover -s tests -t .` (from `backend/`, stdlib only, temp DB files;
`tests/test_storage.py`). This replaces the earlier `db.py` store
(`%LOCALAPPDATA%\UFOS.ai\data\livestream.sqlite`) and the first `storage.py` draft
(`data/livestreamagent.db`); those files are no longer read and can be deleted if they exist.

## Daily AI caps + `GET/PUT /ai/budgets` (2026-10-17, task B)

Contract: `UFOS.ai/docs/ai-budgets-spec.md`. `ai_caps.py` (identical copy in every backend) keeps a
daily cap per AI feature in `data/livestream.db` (meta): `analysis` (per transcript segment, default
$5.00/day) and `verdicts` (default $1.00/day), 0..50, 0 = off. `llm_analyst.py` checks the cap
BEFORE every call; a blocked call is not made and nothing analysis-like is broadcast. Instead the
backend sends a WebSocket message `{"type": "ai_budget", "payload": {key, label, blocked, reason,
message}}` when a key's state changes (and the currently blocked keys to a new client). Fail closed
(unreadable spend, failed spend write, in-memory stand-in DB). `GET /ai/budgets` (schema-1 payload),
`PUT /ai/budgets {key, limit_usd}` (token; 403 with an `Origin` header; changes are logged) on port
8766 — the MainWindow "AI budgets" window is the UI. Tests: `tests/test_ai_caps.py`.

Review fixes (2026-10-17): **Deepgram transcription** has its own daily cap `transcription`
(default $3.00/day, estimate = audio minutes × `TRANSCRIPTION_USD_PER_MINUTE` = $0.0100/min in
`main.py`, conservative): reserved before the stream opens and before every further 5 minutes;
each interval writes an estimated cost-log line (`provider: "Deepgram"`, `status: "estimated"`).
When the cap blocks, no more audio is sent, the Deepgram stream and ffmpeg are closed cleanly, the
window gets `ai_budget`, and the backend re-checks every 30 s and resumes once allowed. After every
successful `PUT /ai/budgets` all caps are re-checked and changed states are broadcast at once.
