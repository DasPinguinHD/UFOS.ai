# AI budgets — contract for the central AI budget window (task B)

Status: contract, 2026-10-17 (written before the code; binding for the backend
parts and for the WPF agent). Decision source: user interview 2026-10-17,
`HedgeFund/backend/strategy_platform/SPEC.md` §15 "B — central AI budget window".
UFOS.ai is a proof-of-concept portfolio project — paper trading only, not a product.

## 0. Goal and rules

One MainWindow window ("AI budgets") shows and edits ALL AI budgets of ALL
modules and shows their spend; every module backend stays the authority for its
own limits and spend (compartmentalization). Rules that apply throughout:

- **Backend values are authoritative.** The window shows limits/spend only from
  the module backend (`GET /ai/budgets`). If a backend is not running, the window
  shows "<Module> backend not running — start the module to edit its budgets".
  Totals read from the shared cost log (`%LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl`)
  may be shown next to it, clearly labelled "from the cost log" — they are
  information, never a stand-in for a limit or for the backend's spend
  (no unrequested fallbacks, CLAUDE.md).
- **Enforced BEFORE each call, fail closed.** A limit of 0 = off. An unreadable
  spend/limit, a spend that could not be recorded, or an unusable store blocks
  the call with a readable message — never a silent pass.
- **Security by default.** Every route sits behind the backend's `api_auth`
  (loopback bind, per-start token `X-UFOS-Token` from `<backend>/data/api-token`,
  Origin allow-list). `PUT /ai/budgets` additionally refuses any request that
  carries an `Origin` header (budgets can only be changed by the native app,
  not by a web page such as the globe WebView). Strict validation, body ≤ 4 KB,
  no stack traces, no secrets in requests, responses, logs or the window.
- **Persisted in the module's own DB, not `.env`**; audited where an audit
  exists (HedgeFund), logged (old → new) otherwise.
- UTC periods: "day" = UTC calendar day, "month" = UTC calendar month
  (same as the existing fund/explain/ideation budgets).

## 1. Today's AI calls and budgets (investigation, 2026-10-17)

| Module (cost-log `module`) | Feature (cost-log `feature`) | Budget before task B |
|---|---|---|
| HedgeFund | `fund-sentiment`, `fund-judge` (Council of AIs, paper engine) | `ai_daily_budget_usd` fund param (default 1.00, 0..50, versioned `PUT /fund/params`, applies from the next cycle; spend `fund.store.ai_spend_today`, `fund.llm.Budget` fail closed) |
| HedgeFund | `sp-explain` (backtest/validation explanations) | `explain_daily_budget_usd` (default 1.00, 0..50; `GET/PUT /v2/settings/explain`, audited `explain_settings_changed`) |
| HedgeFund | `sp-studio`, `sp-paper`, `sp-committee` | ideation budget: monthly 10.00 + daily cap 2.00 (0..200; `GET/PUT /v2/settings/ideation`, audited `ideation_settings_changed`) |
| HedgeFund | discovery loop (stage 6, built in parallel) | own loop budget — plugs in via §4.4 as key `loop` |
| HedgeFund | `analysis` (Financial Newsroom → Analysis & Reasoning, standard/premium tier) | **none** → new daily cap (§3) |
| HedgeFund | `insider-summary` (Insider Trades → Sum up most notable) | **none** → new daily cap (§3) |
| Global Monitoring | `assess` (globe marker ✦ Assess/Sentiment) | **none** (only the 30-min reuse cache) → new daily cap |
| Global Monitoring | `localnews-summary` (city panel ✦ AI Sum up local news) | **none** → new daily cap |
| LiveStreamAgent | `analysis` (per transcript segment, runs in the background), `verdict` (every 90 s) | **none** → new daily caps |
| LiveStreamAgent | Deepgram live transcription (`transcription.py`, paid speech-to-text, billed per audio minute) | **none, and no cost-log line** → new daily cap `transcription` + estimated cost-log lines (review L5, 2026-10-17) |
| UFOS.ai (main app) | "Summarize with AI" (`Services/OpenRouterClient.cs`, runs in the app, no backend) | none → **app-side daily cap** `article_summary` (review follow-up 2026-10-17, app-local provider "UFOS.ai app", §5 step 2b / §10.1) |

Every call already appends one cost-log line (each module's own `cost_ledger`
writer); MainWindow → Transaction Log lists them.

## 2. Scope exception and scope guard

User, 2026-10-17: for task B only, files outside `UFOS.ai/HedgeFund/` MAY be
changed — only those on this allow-list (`HedgeFund/tools/scope-allow-budget0.json`,
embedded into snapshot `budget0` before anything outside was changed):

- MainWindow shell: `UFOS.ai/MainWindow.xaml`, `UFOS.ai/MainWindow.xaml.cs`,
  new `UFOS.ai/AiBudgetsWindow.xaml`, `UFOS.ai/AiBudgetsWindow.xaml.cs`
- new Shared files: `UFOS.ai/Shared/IAiBudgetProvider.cs`, `UFOS.ai/Shared/AiBudget*.cs`
- Global Monitoring backend: `ai_caps.py` (new), `main.py`, `README.md`, `tests/test_ai_caps*.py` (new)
- LiveStreamAgent backend: `ai_caps.py` (new), `main.py`, `llm_analyst.py`, `README.md`, `tests/test_ai_caps*.py` (new),
  `cost_ledger.py` (added by review L5, 2026-10-17: `provider` parameter + label for the Deepgram lines)
- LiveStreamAgent client hook-up: `UFOS.ai/LiveStreamAgent/WebSocketService.cs`,
  `UFOS.ai/LiveStreamAgent/LivestreamAgent.xaml`, `UFOS.ai/LiveStreamAgent/LivestreamAgent.xaml.cs`
- `CLAUDE.md` (one pointer paragraph); this contract (`UFOS.ai/docs/ai-budgets-spec.md`,
  a permanent doc exception of the scope guard)

Everything else outside HedgeFund stays untouched (the globe page already shows
a backend `error` text, so it needs no change; `api_auth.py` copies stay identical).

Checks (run from `$HOME/mnt/UFOS.ai/UFOS.ai`):

```
python3 HedgeFund/tools/scope_guard.py check --label budget0
    # only "allow-listed changes" (+ doc exceptions) may appear -> RESULT: ok
python3 HedgeFund/tools/scope_guard.py check --label stage0
python3 HedgeFund/tools/scope_guard.py check --label mr0
    # older snapshots: the approved copy embedded in snapshot budget0 is applied automatically
    # ("approved allow-list of snapshot 'budget0' applied"); everything else must be unchanged -> RESULT: ok
python3 HedgeFund/tools/scope_guard.py check --label stage0 --strict
    # without any allow-list (shows the allow-listed files as CHANGED/ADDED — for review only)
```

How it works: `snapshot --label budget0 --allow-list HedgeFund/tools/scope-allow-budget0.json`
embedded the approved list (sha256 `8368292bddac…`) into
`HedgeFund/backend/data/scope-guard/manifest-budget0.json`; `check` applies the embedded
copies of all snapshots (never the editable JSON file unless it is passed with `--allow-list`).

Pattern rules (review L3): an entry is an exact path, or a path whose LAST segment
contains `*` with at least 3 literal characters before the first `*` (e.g.
`UFOS.ai/Shared/AiBudget*.cs`); `*` matches within that one segment only (never
`/`); no wildcards in directory parts, no `?`/`[`, no `..`, no absolute paths.
Invalid entries are refused (snapshot/amend/`--allow-list`) or ignored with a
warning (an old embedded copy).

Closing the exception (review M1): `scope_guard.py close --label budget0` (run once
at the very end of task B, after the WPF agent) records the sha256 of every file the
allow-list matches (or "absent") in the manifest. From then on every check skips such
a file ONLY while its current hash equals the recorded one — any later change is
reported as CHANGED/ADDED/REMOVED and needs a new approved exception.
`scope_guard.py amend --label budget0 --allow-list HedgeFund/tools/scope-allow-budget0.json`
re-embeds an extended list (only additions, each listed explicitly, recorded under
`amendments`; refused after `close`).

`--allow-file PATH` adds one ad-hoc exact path for a check (printed for review). An
entry of a passed list that is not in the approved copy embedded in the
snapshot is printed under "NOT in the snapshot's approved copy — review": a WPF
agent that needs one more file outside HedgeFund must ask the main session
(which asks the user) instead of silently extending the list.

## 3. New daily caps (defaults chosen so that normal use does not change)

| Backend | key | label | default (USD/day) | min..max | per-call estimate |
|---|---|---|---|---|---|
| Global Monitoring | `assess` | Globe · AI Assess / Sentiment | 1.00 | 0..50 | 0.005 |
| Global Monitoring | `localnews_summary` | Globe · AI Sum up local news | 1.00 | 0..50 | 0.005 |
| LiveStreamAgent | `analysis` | Live analysis (per transcript segment) | 5.00 | 0..50 | 0.005 |
| LiveStreamAgent | `verdicts` | Live verdicts (every 90 s) | 1.00 | 0..50 | 0.003 |
| LiveStreamAgent | `transcription` | Live transcription (Deepgram) | 3.00 | 0..50 | 0.05 per 5-minute interval |
| HedgeFund (Strategy Platform, stage 9) | `reports` | Hedge Fund · Report commentary | 0.50 | 0..20 | 0.02 standard / 0.20 premium |
| HedgeFund (Newsroom) | `newsroom_analysis` | Newsroom · Analysis & Reasoning | 5.00 | 0..50 | 0.02 standard / 0.30 premium |
| HedgeFund (Newsroom) | `newsroom_insider_summary` | Newsroom · Insider summary | 1.00 | 0..50 | 0.02 |

Why generous enough: measured costs (local DBs, default model
`deepseek/deepseek-chat-v3-0324`): LSA analysis ≈ $0.0009/call, verdict ≈
$0.00035, globe Assess ≈ $0.0003. $1/day ≈ 3,000 Assess clicks; LSA $5 ≈ 5,000
segment analyses (a busy Fed press conference produces a few hundred per hour)
and $1 ≈ 2,800 verdicts (40/h). Newsroom premium (default
`anthropic/claude-opus-5.5`) costs roughly $0.1–0.3 per analysis → $5 ≈ 15–50
premium or ~1,000 standard analyses. A more expensive model configured in
`.env` reaches the caps sooner — that is intended (cost limit).

Transcription (review L5): Deepgram bills streamed audio by the minute; the backend
gets no cost back, so it **estimates** cost = audio minutes sent × `TRANSCRIPTION_USD_PER_MINUTE`
(constant in `LiveStreamAgent/backend/main.py`, 0.0100 USD/min — conservative,
above Deepgram's published pay-as-you-go streaming prices for nova-2; change the
constant if your plan differs). The audio is metered at 16 kHz mono PCM16 (32,000 bytes/s).
Before a session opens, and before every further 5-minute interval, the cap is
reserved for that interval (0.05 USD); a full interval is booked and written to the
cost log (`feature: "transcription"`, `provider: "Deepgram"`, `model: "deepgram/nova-2"`,
`status: "estimated"`), the partial last interval at session end. When the cap
blocks, no more audio is sent, the Deepgram stream is closed cleanly (close-stream
message, ffmpeg stopped), the `ai_budget` message is broadcast, and the backend
re-checks every 30 s and resumes the stream once the cap allows it (no crash, no
restart loop). $3/day ≈ 5 h of streaming at the estimate.

Enforcement (shared helper `ai_caps.py`, one identical copy per backend:
`Global Monitoring/backend/ai_caps.py`, `LiveStreamAgent/backend/ai_caps.py`,
`HedgeFund/backend/ai_caps.py` — keep them identical, like `api_auth.py`):

- Before each call: `reserve(key, estimate)` refuses when
  `limit == 0` (off), or `spent_today + estimates in flight + estimate > limit`,
  or the limit/spend cannot be read, or a spend write failed earlier in this
  process ("paused until the backend restarts"), or the store is the in-memory
  stand-in (GMS/LSA storage fall back to an in-memory DB when the file is unusable
  — budgets could then be bypassed by a restart, so AI calls are refused).
- After the call: `settle(reservation, cost_usd)` books OpenRouter's reported
  `usage.cost`; a completed call without a reported cost books the estimate
  (fail closed); a timed-out request books the estimate (the provider may bill
  it); a call that never left / was rejected books nothing (`release`).
- Messages (exact wording, `{label}` = budget label):
  - off: "{label} is switched off (daily cap is 0) — raise it in AI budgets (main window → Settings)."
  - reached: "Daily AI cap for {label} reached (${spent} of ${limit} today, UTC) — raise it in AI budgets (main window → Settings) or try again tomorrow."
  - unreadable: "{label}: today's AI spend could not be read — AI calls are paused until it can be read again (see backend.log)."
  - unrecorded: "{label}: AI spend could not be recorded — AI calls are paused until the backend restarts."
  - invalid stored limit: "The stored daily cap for {label} is invalid — set it again in AI budgets (main window → Settings)."
  - store unavailable: "{label}: the module database is unavailable — AI calls are paused (see backend.log)."
- After every successful `PUT /ai/budgets` the LiveStreamAgent re-checks all its keys
  (`caps.status`) and broadcasts `ai_budget` for every key whose state changed
  (review L4) — the window's amber line clears at once.
- HTTP surfaces: GMS `POST /assess` and `POST /localnews-summary`, HedgeFund
  `POST /newsroom/analysis` and `POST /newsroom/insider-summary` answer
  **402** `{error: <message>, code: "budget", budget_key, reason: off|reached|unreadable|unrecorded|invalid|store}`
  (checked after the GMS reuse cache: a cached answer costs nothing).
- LiveStreamAgent (no HTTP trigger): a blocked verdict/analysis is skipped (no
  LLM call, nothing that looks like analysis is broadcast) and the backend
  broadcasts a WebSocket message `{"type": "ai_budget", "payload": {"key",
  "label", "blocked": true, "reason", "message"}}` when the state of a key
  changes, `{"blocked": false, ...}` when it is lifted, and the current blocked
  keys to every newly connected client.
- Persistence: GMS `gms.db` meta, LSA `livestream.db` meta, Newsroom caps in
  `hedgefund.db` meta (the HedgeFund module's strict store; keys
  `ai_cap_limit:<key>`, spend `ai_cap_spend:<key>:<YYYY-MM-DD>`). Audit:
  HedgeFund `ai_budget_changed` {module, key, old, new} in the Strategy Platform
  audit (refused with 503 when the audit row cannot be written); GMS/LSA have no
  audit table → one log line "AI budget changed: <key> <old> -> <new> (by the app)".

## 4. Backend API (every module backend)

### 4.1 Routes

- `GET /ai/budgets` → 200 module payload (§4.2). No query parameters (unknown → 400).
- `PUT /ai/budgets` body `{"key": "<budget key>", "limit_usd": <number>}` —
  exactly ONE budget per request (atomic; the window saves one field at a time)
  → 200 module payload + `"changed": {"key", "old", "new"}`.
  - 400 `{error: "invalid input", errors: {field: message}}`: not an object,
    unknown/missing fields, unknown key ("unknown budget key"), `limit_usd` not
    a finite number (bool rejected) or outside `min..max` ("a number 0..50 (0 = off)"),
    a value > 0 that rounds to 0 at 6 decimals ("at least 0.000001, or exactly 0 = off",
    review L7), entry not editable.
  - 403 `{error: "budgets can only be changed from the app"}` when an `Origin` header is present.
  - 413/400 body larger than 4 KB; 503 `{error, kind}` store/audit unavailable.
- Ports (loopback, existing): HedgeFund 8867, Global Monitoring 8768, LiveStreamAgent 8766.

### 4.2 Module payload (schema 1)

```json
{
  "schema": 1,
  "module": "hedgefund | global-monitoring | livestream-agent",
  "label": "Hedge Fund | Global Monitoring | LiveStream Agent",
  "generated_at": "2026-10-17T12:00:00+00:00",
  "day": "2026-10-17",
  "month": "2026-10",
  "budgets": [
    {
      "key": "assess",
      "label": "Globe · AI Assess / Sentiment",
      "group": "Globe",
      "period": "day",
      "period_id": "2026-10-17",
      "limit_usd": 1.0,
      "default_usd": 1.0,
      "min": 0.0,
      "max": 50.0,
      "step": 0.1,
      "spent_usd": 0.0123,
      "remaining_usd": 0.9877,
      "enabled": true,
      "status": "ok",
      "message": null,
      "editable": true,
      "applies": "immediately",
      "notes": "Checked before every Assess/Sentiment click; a cached answer (30 min) costs nothing.",
      "cost_log": [{"module": "Global Monitoring", "feature": "assess"}]
    }
  ]
}
```

- `period`: `day` (UTC day) | `month` (UTC month) | `run` (run = cap per loop run, no running spend:
  `spent_usd`/`remaining_usd` may be null or absent; the window shows "—" and "per run"). `run` added
  2026-10-17 for the stage-6 key `loop_run`; clients show any other value as "per <value>".
- `status`: `ok` | `blocked` (limit reached / off — still editable) | `paused`
  (spend unrecorded) | `error` (this entry could not be read; `limit_usd`,
  `spent_usd`, `remaining_usd` may be `null`, `message` says why).
- `message`: the backend's user-facing text for blocked/paused/error, else null.
- `applies`: `immediately` or `next_cycle` (fund param: a new params version
  applies from the next fund cycle).
- `cost_log`: the cost-log (`module`, `feature`) pairs this budget covers — for
  the window's "from the cost log" figures only.
- Numbers are plain JSON numbers (no NaN), USD, rounded to 6 decimals.
- Unknown extra fields must be ignored by clients (forward compatibility).

### 4.3 HedgeFund `/ai/budgets` (aggregator `HedgeFund/backend/ai_budgets.py`)

| key | group | period | source of limit / spend | PUT goes to | default | max |
|---|---|---|---|---|---|---|
| `fund` | Fund (Council of AIs) | day | fund params `ai_daily_budget_usd` / `fund.store.ai_spend_today` | `engine.update_params({"ai_daily_budget_usd": v}, "user")` (audited `params_update`, applies next cycle) | 1.00 | 50 |

`fund` (review L6): when the params come back as version 0 (= the built-in defaults),
the aggregator checks the fund store (newsroom.db): disabled or unreadable →
`status: "error"`, `limit_usd: null`, message with the reason — never the defaults
shown as if they were stored. Version 0 on a readable store (nothing saved yet) is
the real effective value.
| `explain` | Strategy Platform | day | `backtest.explain.get_settings()` | `explain.put_settings` (audited `explain_settings_changed`) | 1.00 | 50 |
| `ideation_month` | Strategy Platform | month | `research.studio.get_settings()` | `studio.put_settings` with the other value unchanged (audited `ideation_settings_changed`) | 10.00 | 200 |
| `ideation_day` | Strategy Platform | day | same | same | 2.00 | 200 |
| `loop` | Strategy Platform | (stage 6) | stage-6 hook §4.4 | stage-6 hook | (stage 6) | (stage 6) |
| `reports` | Strategy Platform | day | `ai_caps` over hedgefund.db meta (`ai_budgets.reports_caps()`, stage 9 — strategy_platform SPEC §19.4) | `ai_caps` (audited `ai_budget_changed`) | 0.50 | 20 |
| `newsroom_analysis` | Financial Newsroom | day | `ai_caps` over hedgefund.db meta | `ai_caps` (audited `ai_budget_changed`) | 5.00 | 50 |
| `newsroom_insider_summary` | Financial Newsroom | day | same | same | 1.00 | 50 |

The existing routes (`/fund/params`, `/v2/settings/explain`, `/v2/settings/ideation`)
and the HedgeFund Settings UI keep working unchanged — `/ai/budgets` is a second
door to the same stored values. One failing source yields one `status: "error"`
entry (generic message, details in backend.log); the other entries still answer.

### 4.4 How the stage-6 loop budget plugs in (coordination with the stage-6 agent)

`ai_budgets.PROVIDER_MODULES = ("strategy_platform.research.loop",)` — dotted
module paths imported lazily. The stage-6 module (rename the entry if the loop
lives elsewhere) exposes:

```python
def ai_budget_entries(now=None) -> list:
    """Standard entries (§4.2) for the loop budget(s): key "loop" (a second one,
    if needed, "loop_month"), group "Strategy Platform", period "day"|"month"|"run" (key "loop_run": cap per loop run, no running spend),
    min/max/default/step, spent_usd/remaining_usd from its own spend counter in
    hedgefund.db meta, status/message as §4.2, cost_log [{"module": "HedgeFund",
    "feature": "<its cost-ledger feature id>"}]."""

def ai_budget_put(key: str, limit_usd: float, actor: str = "user") -> None:
    """Validates key + range itself (raise ValueError("<field message>") -> 400),
    stores the limit in hedgefund.db meta and writes its audit row
    (e.g. loop_settings_changed); storage/audit failure -> raise a UserFacingError
    (-> 503). Must be the same values its own /v2 settings route uses."""
```

Rules: keys must start with `loop`; the aggregator calls both in a worker
thread; any exception in `ai_budget_entries` becomes one `{key: "loop",
status: "error"}` entry. While the module does not exist yet
(`ModuleNotFoundError` for exactly that module path) there is simply no `loop`
entry — a development-order guard, not a data fallback. The loop's own budget
check (BEFORE each call, fail closed) is the stage-6 agent's job, like explain/ideation.

### 4.5 Global Monitoring / LiveStreamAgent

`ai_caps.DailyCaps` owns the module's caps (§3) and the payload/PUT logic;
`main.py` only adds the two routes and the checks. Module ids/labels:
`global-monitoring` / "Global Monitoring" (group "Globe"),
`livestream-agent` / "LiveStream Agent" (group "Live stream").

## 5. How a new module plugs in (documented interface)

1. Backend: copy `ai_caps.py` (unchanged) into the backend, declare its
   `CapDef`s, check `reserve()`/`settle()` around every LLM call, add
   `GET/PUT /ai/budgets` behind its `api_auth` (`DailyCaps.payload()` /
   `DailyCaps.handle_put()`), persist via the module's own DB (a strict
   key/value adapter that raises on failure). A backend with its own budget
   logic may implement the routes itself — only the JSON of §4 is binding.
2. App: add ONE entry to `UFOS.ai/Shared/AiBudgetProviders.cs` (module id,
   label, backend folder relative to the repo, port, cost-log module names,
   start hint). Nothing else — the window lists every registered provider.
   (A static list was chosen over scanning `module.json` files: compile-time,
   no file parsing, nothing on disk can add a provider.)
   2b. App-local variant (no backend, e.g. the main app's "Summarize with AI"): a class that
   implements `IAiBudgetProvider` AND `IAiBudgetLocalSource` (`LocalPayload()` → the same
   schema-1 payload, `LocalPut(key, limit)` → the same PUT result), registered in the same list
   (Port 0, no backend folder); it enforces its caps in-process BEFORE each call, fail closed, and
   persists them in a small JSON under `%LOCALAPPDATA%\UFOS.ai\` (reference: `Shared/AiBudgetAppCaps.cs`).
3. Cost log: the module's `cost_ledger` writes lines with its `module` name;
   list that name in the provider entry and the features in each budget's
   `cost_log`.

## 6. WPF surface (for the WPF agent — the backend agent does NOT build it)

### 6.1 Files (allow-list §2; `Shared/*.cs` is linked into EVERY module csproj,
so Shared files must compile without main-app types such as `Services/*`)

- `Shared/IAiBudgetProvider.cs`:
  ```csharp
  internal interface IAiBudgetProvider
  {
      string ModuleId { get; }                 // "hedgefund" — must equal the payload's "module"
      string Label { get; }                    // "Hedge Fund"
      string[] BackendRelativeDir { get; }     // { "HedgeFund", "backend" } (BackendAuth.FindBackendDir)
      int Port { get; }                        // 8867 — always http://127.0.0.1:<Port>
      IReadOnlyList<string> CostLogModules { get; } // { "HedgeFund" }
      string StartHint { get; }                // "Open the Hedge Fund module to start its backend."
  }
  ```
- `Shared/AiBudgetProviders.cs`: `internal sealed record AiBudgetProvider(...) : IAiBudgetProvider`
  + `internal static class AiBudgetProviders { public static IReadOnlyList<IAiBudgetProvider> All }`
  with exactly: hedgefund / "Hedge Fund" / HedgeFund\backend / 8867 / {"HedgeFund"};
  global-monitoring / "Global Monitoring" / "Global Monitoring"\backend / 8768 / {"Global Monitoring"};
  livestream-agent / "LiveStream Agent" / LiveStreamAgent\backend / 8766 / {"LiveStreamAgent"}.
- `Shared/AiBudgetRegistry.cs`: `AiBudgetRegistry.Providers` (= `AiBudgetProviders.All`,
  duplicate module ids rejected at first use) and
  `Task<AiBudgetModuleState> LoadAsync(IAiBudgetProvider p, CancellationToken ct)` /
  `Task<AiBudgetPutResult> PutAsync(IAiBudgetProvider p, string key, decimal limitUsd, CancellationToken ct)`.
  Availability order: backend folder not found → `NotInstalled`; no token file →
  `NotRunning`; `GET /health` fails (2 s timeout) → `NotRunning`; `GET /ai/budgets`
  401 → re-read token once, still 401 → `NotRunning` ("backend restarted — refresh");
  404 → `Outdated` ("restart the module — its backend is older than this app");
  payload `module` ≠ `ModuleId` or `schema` ≠ 1 → `Invalid`; else `Ok` + DTOs.
  Token via `BackendAuth.ReadToken` on every call, never logged/shown; 10 s timeout.
- `Shared/AiBudgetModels.cs` (DTOs): `[JsonPropertyName]` on every property
  (snake_case), `[JsonIgnore]` on computed properties (the
  PropertyNameCaseInsensitive collision bug), nullable numbers for
  `limit_usd/spent_usd/remaining_usd`; plain text only (no markup) for
  `label/notes/message` (cap length, e.g. 300 chars).
- `AiBudgetsWindow.xaml(.cs)` (main app) + MainWindow Settings popup button
  "AI budgets…" (above "Python packages"), opening one instance (reuse
  `OpenModuleWindow`-style single-instance logic). DesignSystem brushes only; if
  `WindowStyle="None"`: `StateChanged` full-screen padding + drag-out-of-full-screen
  handlers (CLAUDE.md reference implementation).

### 6.2 Layout and behaviour

- One card per provider (registry order), header = label + state
  ("running" / "backend not running" / …) + "Refresh".
- `Ok`: rows grouped by `group`: label, period ("per day (UTC)" / "per month
  (UTC)"), limit input (min..max, step, 0 = off), "Spent $x of $y · $z left",
  status `message` (amber for blocked/paused, red for error), `notes` and
  `applies` ("applies from the next fund cycle"); a "Default: $d" link resets the
  input to `default_usd`. Save: PUT 600 ms after the last valid edit (or on
  Enter/LostFocus); invalid input → inline hint, no PUT; 400 → show
  `errors.limit_usd`; 403/503 → show `error`; success → re-render from the
  returned payload (never from the typed value).
- Not running / not installed / outdated / invalid: one line
  "<Label> backend not running — start the module to edit its budgets" (or the
  matching text), no inputs; below, read-only "from the cost log" totals.
- Cost-log figures (`AiCostLedger.ReadAll()`, main app only): per module
  "Cost log: $a today (UTC) · $b this month (UTC)" and per budget the sum over
  its `cost_log` pairs, always with the caption "from the cost log" and a tooltip
  "Informational — the backend's own spend counter is authoritative (it also
  counts estimates for failed/unpriced calls)". Lines with `cost_usd = null`
  are counted as "n calls without a reported cost".
- Refresh on open, on "Refresh", and every 30 s while the window is visible.
- ~~A final row "UFOS.ai main app — Summarize with AI: no budget"~~ — superseded (§10.1): the
  main app is the app-local provider card "UFOS.ai app" with its own editable daily cap.

### 6.3 LiveStreamAgent hook-up

`WebSocketService.cs` handles `type == "ai_budget"` (payload as §3) → event
`OnAiBudget(key, label, blocked, message)`; `LivestreamAgent.xaml(.cs)` shows a
small amber status line under the verdict area while any key is blocked
(plain text = `message`), cleared on `blocked: false`. No change to the window's
design otherwise.

### 6.4 Security notes for the WPF side

Loopback URLs only (built from `Port`, never from payload data); token only in
the header; no secrets shown; all payload text shown as plain text; PUT only on
a user edit; a payload > 256 KB or with more than 64 budgets is rejected as `Invalid`.

## 7. Tests (no network)

- `HedgeFund/backend/tests/test_ai_budgets.py`: `ai_caps` (reserve/settle,
  off, reached incl. in-flight estimates, unreadable, unrecorded pause, invalid
  stored limit, PUT validation, payload shape) + the aggregator (all keys,
  fund/explain/ideation delegation, audit refusal, loop hook present/absent/
  broken, Origin refusal in the route layer) + newsroom 402.
- `Global Monitoring/backend/tests/test_ai_caps.py`, `LiveStreamAgent/backend/tests/test_ai_caps.py`:
  storage adapter on a temp DB (outside the mount), in-memory refusal, payload/PUT,
  LSA analyst skip + `ai_budget` message.

## 8. Residual risks / open items

- Process-local reservations: two backends never share a key, so this is enough;
  a second instance of the same backend (port already taken) never serves calls.
- The token file is readable by any program of the same Windows user (as before).
- ~~Main app "Summarize with AI" has no budget~~ — resolved by the app-side cap (§10.1).
- Cost-log totals can differ from the backend spend (estimates for failed or
  unpriced calls are booked by the backend only) — labelled in the window.

## 9. Implementation notes (backend, implemented 2026-10-17)

- **Files.** `HedgeFund/backend/ai_caps.py` (= `Global Monitoring/backend/ai_caps.py` =
  `LiveStreamAgent/backend/ai_caps.py`, byte-identical; a test asserts it), `HedgeFund/backend/ai_budgets.py`
  (aggregator + `register(app, engine)` + `newsroom_caps()` + `guarded_newsroom_call()`), wiring in the
  three `main.py`, `LiveStreamAgent/backend/llm_analyst.py` (`LLMAnalyst(store, caps)`), scope guard
  extension (`HedgeFund/tools/scope_guard.py`, `scope-allow-budget0.json`). No strategy_platform code
  was changed (the aggregator only calls `explain.get_settings/check_settings/put_settings`,
  `studio.get_settings/limits/check_settings/put_settings`, `storage.db.meta_get/meta_set/add_audit`).
- **`ai_caps` API.** `CapDef(key, label, default_usd, estimate_usd, max_usd=50, min_usd=0, step=0.1,
  group, notes, cost_log)`; `DailyCaps(module_id, label, caps, kv_get, kv_set, kv_add=None,
  available=None, audit=None, clock=None, logger=None)` with `reserve(key, estimate=None)`,
  `settle(res, cost_usd)`, `release(res)`, `charge_for_exception(res, ex)` (timeout anywhere in the
  cause chain -> estimate), `status(key)` -> None | (reason, message), `entries()/payload()`,
  `check_put(body)`, `put(key, v, actor)`, `handle_get(query)`, `handle_put(body, origin, query)`;
  helpers `guarded_call(caps, key, estimate, factory)` (await a `(text, model, usage)` coroutine under
  a cap), `read_json_body(request)` (≤ 4 KB), `refuse_put`, `check_put_body`, `module_payload`,
  `storage_kv(store)` (GMS/LSA `storage.Storage`: sentinel on the fail-soft `_run/_write` -> raises;
  `available()` = not `in_memory`; spend added in one transaction).
- **HedgeFund.** Order of `budgets`: fund, explain, ideation_month, ideation_day, (loop entries),
  reports (stage 9, 2026-10-18), newsroom_analysis, newsroom_insider_summary. Fund: `engine.get_params()` + `engine.store.ai_spend_today()`
  (None -> `status: "error"` with `fund.llm.SPEND_UNREADABLE_MESSAGE`); PUT -> `engine.update_params`
  (EngineError -> its status). Explain/ideation: their own messages (`BUDGET_*`, `OFF_MESSAGE`, …),
  `paused_reason` -> `status: "paused"`. Ideation PUT keeps the other value (`studio.limits()`); an invalid
  stored ideation value -> 503 ("set both ideation values in Hedge Fund → Settings"). Newsroom: caps over
  hedgefund.db meta, audit `ai_budget_changed` {module, key, old, new, actor: "user"} BEFORE the write
  (refused with 503 when it fails). `POST /newsroom/analysis` estimate by tier (0.02 / 0.30),
  `POST /newsroom/insider-summary` 0.02; blocked -> 402 `ai_caps.BudgetBlocked.payload()`.
  The routes run the sync aggregator in `asyncio.to_thread`; PUT with an Origin header -> 403 before
  the body is read.
- **Loop hook.** `ai_budgets.PROVIDER_MODULES = ("strategy_platform.research.loop",)`; `HookSource`
  imports lazily; missing module -> no entry; entries whose key does not start with `loop` -> error entry;
  `ai_budget_put` ValueError -> 400 `errors.limit_usd`, ExplainError-style (`status` + `payload()`) ->
  its status, other UserFacingError -> 503, anything else -> 500 (logged). The PUT range check uses the
  hook entry's own `min`/`max`.
- **GMS.** `main.AI_CAPS` + `build_ai_caps(db_store)`; `/assess`: reuse cache -> `budgets.status("assess")`
  (402 before the context is fetched) -> `guarded_call` around `assess.assess_marker` (authoritative
  reserve); `/localnews-summary` likewise before the headline fetch.
- **LSA.** `main.AI_CAPS`, `build_ai_caps(store)`, `BudgetNotifier(caps, broadcaster)` (`update(key,
  result)` -> True when the result was a budget block; any other result clears the block);
  `_run_analysis_loop(..., notifier)` skips a blocked analysis (no log_event/broadcast),
  `_run_verdict_loop(..., notifier)` updates it (a blocked verdict is skipped as before: it carries `error`).
  Warm start appends the current `ai_budget` messages.
- **Review fixes (2026-10-17).** M1/L3 scope guard: `amend`/`close` commands, `_Rule` (open = segment-local
  patterns, closed = frozen sha256 per file), embedded lists of other snapshots applied unless `--strict`;
  tests `HedgeFund/backend/tests/test_scope_guard.py`. L4: `BudgetNotifier.refresh()` after a 200 PUT.
  L5: `main.TranscriptionMeter` + `metered_chunks()` wrap `pcm_chunks` (transcription.py unchanged),
  `_run_transcription(queue, budgets, notifier)` waits/re-checks every 30 s while blocked;
  `cost_ledger.record(..., provider=)`. L6: `FundSource._store_problem()` for params version 0.
  L7: `ai_caps.check_put_body` rejects values that round to 0 at 6 decimals.
- **Tests (no network).** `HedgeFund/backend/tests/test_ai_budgets.py` (26), `Global Monitoring/backend/
  tests/test_ai_caps.py` (10), `LiveStreamAgent/backend/tests/test_ai_caps.py` (10). Full suites run
  green 2026-10-17 (HedgeFund split: non-sp/mr 461, mr 333, sp 747; GMS 47; LSA 41).

## 10. WPF notes (implemented 2026-10-17, not built — no dotnet on the VM; static checks only)

- **Files.** `Shared/IAiBudgetProvider.cs` (interface exactly as §6.1 + the "how a new module plugs in"
  comment of §5), `Shared/AiBudgetProviders.cs` (record `AiBudgetProvider` + `AiBudgetProviders.All`:
  hedgefund 8867, global-monitoring 8768, livestream-agent 8766 — the Newsroom caps are entries of the
  HedgeFund payload, the main app is not a provider), `Shared/AiBudgetModels.cs` (DTOs, `AiBudgetAvailability`,
  `AiBudgetModuleState`, `AiBudgetPutResult`, `AiBudgetText.Clean`), `Shared/AiBudgetRegistry.cs`
  (`Providers`, `LoadAsync`, `PutAsync`), `AiBudgetsWindow.xaml(.cs)`, MainWindow Settings popup button
  "AI budgets…" (above "Python packages"; single instance, a second click activates it; closed with
  MainWindow), LSA hook-up (§6.3). **No csproj change:** the main app compiles `Shared/*.cs` by the SDK
  default glob and every module csproj links `..\Shared\*.cs` (wildcard), so the new Shared files are
  picked up everywhere; they use only BCL types, `BackendAuth` and `UFOS.ai.Logging` (referenced by all).
- **Availability.** As §6.1, plus one extra state `Error` (backend reachable, but GET answered another
  HTTP error or timed out after 10 s) showing "<Label> backend answered HTTP <code>: <error verbatim>".
  `NotInstalled`/`Outdated`/`Invalid` texts name the cause. A refused token is re-read once (GET and PUT;
  a 401 is refused before anything changes, so the PUT retry is safe).
- **HTTP hygiene.** One `HttpClient` (no proxy, no redirects, no cookies), URLs `http://127.0.0.1:<Port>`
  only, token header read per call, never logged; no `Origin` header; bodies capped at 256 KB while
  reading; payload rejected (`Invalid`) for schema ≠ 1, module ≠ id, > 64 budgets, missing/duplicate/
  odd keys; text fields sanitized (control/bidi characters removed, ≤ 300 chars) and shown as plain text.
- **Window.** Borderless, black 18 px title bar, DesignSystem tokens only, full-screen padding +
  drag-out-of-full-screen (same code as TransactionLogWindow). One card per provider (state dot + text,
  "updated hh:mm:ss", Refresh), rows grouped by `group`: label, period ("per day (UTC)" / "per month (UTC)" /
  "per run"), on/off, `applies`, status `message` (amber blocked/paused, red error), notes, limit input
  (range hint "min–max USD · 0 = off", Up/Down = ±step, "Default: $d" link), "Spent $x of $y" / "$z left"
  ("—" when null — e.g. `run`). Save: 600 ms after the last valid edit, on Enter or leaving the field;
  invalid → inline hint, no PUT; equal to the stored value → no PUT; one PUT in flight per window
  (semaphore); success re-renders from the returned payload and shows "Saved: $old → $new" (+ "applies
  from the next fund cycle"); failure shows the backend text verbatim ("Not saved: …"), is not retried
  automatically (Enter retries, Esc restores the stored value). A GET that started before a PUT answer
  was rendered is discarded (epoch). Auto-refresh never overwrites a field with an unsaved edit.
  Refresh: on open, per card / "Refresh all" / F5, every 30 s while visible and not minimized.
- **Cost log.** `AiCostLedger.ReadAll()` per refresh (worker thread): per card "Cost log: $a today (UTC) ·
  $b this month (UTC)" (+ "n calls without a reported cost"), per budget the sum over its `cost_log` pairs
  for its own period (`run` → none), each with the caption "from the cost log" and the §6.2 tooltip;
  while a backend is not usable, read-only per-feature lines (this month). (The former final row
  "UFOS.ai main app — no budget" is replaced by the "UFOS.ai app" card, §10.1.) Note:
  `ReadAll` returns an empty list for a locked file (shows $0 until the next refresh).
- **Keyboard / accessibility.** Tab through Refresh buttons, inputs and Default links (visible dashed
  focus ring), Enter saves, Esc restores an unsaved field or closes the window, F5 refreshes;
  `AutomationProperties.Name`/`HelpText` on inputs, cards and rows, live regions (polite) for state,
  messages and save results.
- **LiveStreamAgent.** `WebSocketService.OnAiBudget(key, label, blocked, message)` for `type == "ai_budget"`
  (text sanitized); `LivestreamAgent` keeps the blocked keys and shows their messages in an amber line
  (`AiBudgetStatusText`) under the verdict caveat; cleared on `blocked: false` and on every (re)connect
  (the backend re-sends the blocked keys to a new client).

### 10.1 App-local cap for "Summarize with AI" (review follow-up, 2026-10-17) — files approved (`amend` budget0 +2), implemented

User decision "all AI budgets of all modules": the main app's "Summarize with AI" gets an app-side daily cap
(no backend). **Files needed outside HedgeFund (WPF agent → backend agent / main session: please add to
`HedgeFund/tools/scope-allow-budget0.json` and its embedded approved copy BEFORE they are edited):**

- `UFOS.ai/ArticleWindow.xaml.cs` — show the cap's readable message (no "AI summary failed:" prefix).
- `UFOS.ai/Services/OpenRouterClient.cs` — `reserve` BEFORE the HTTP call, `settle` with `usage.cost` after.
- new `UFOS.ai/Shared/AiBudgetAppCaps.cs` — already covered by the approved glob `UFOS.ai/Shared/AiBudget*.cs`.
- (already approved: `Shared/IAiBudgetProvider.cs`, `Shared/AiBudget*.cs`, `AiBudgetsWindow.xaml(.cs)`, this contract.)

Design (binding for the WPF side): key `article_summary`, label "Article · Summarize with AI", group
"Main app", period day (UTC), default $1.00, 0..50, step 0.1, 0 = off, per-call estimate $0.01; state in
`%LOCALAPPDATA%\UFOS.ai\ai-budgets-app.json` (`{"schema":1,"limits":{key:usd},"spend":{key:{"day","usd"}}}`,
written atomically, never `.env`); same messages as §3 (with "app" instead of "backend" where relevant);
fail closed (unreadable file → calls blocked + not editable until the file is fixed/renamed; a failed
spend write pauses calls until the app restarts); registered as app-local provider "UFOS.ai app"
(module id `ufos-app`) that answers the same schema-1 payload / PUT result in-process (§5 step 2b).

**Implemented (WPF agent, 2026-10-17; static checks only).** `Shared/AiBudgetAppCaps.cs`
(`AiBudgetAppCaps.Instance`: `IAiBudgetProvider` + `IAiBudgetLocalSource`, module `ufos-app`, Port 0;
`Reserve(key)` → `Reservation` or `AiBudgetBlockedException(key, reason, message)`, `Settle(res, cost)`,
`Release(res)`, `ChargeForException(res, ex)`: HTTP error status / connection / name-resolution / TLS /
proxy failures book nothing, timeouts and other failures after sending book the estimate; lock-protected,
in-flight estimates counted; JSON written atomically via temp file + move; file > 64 KB, not an object or
schema ≠ 1 = unreadable). `IAiBudgetLocalSource` lives in `Shared/IAiBudgetProvider.cs`; `AiBudgetRegistry`
answers it in-process (worker thread) and skips the port/folder checks for it; the window shows its card
("in the app") last in the registry order. `Services/OpenRouterClient.SummarizeArticleAsync`: reserve
BEFORE the request (after the API-key check), settle with `usage.cost` right after the response is parsed
(before the text), `ChargeForException` on any failure before that; `ArticleWindow` shows
`AiBudgetBlockedException.Message` as is (AI-unavailable presentation, no "failed:" prefix).
