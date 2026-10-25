# ENV_API — S10-E ".env removed, Credential Manager only" — python contract (Agent H, 2026-10-24; user decisions 16:25 recorded)

For Agent W (C#) to build against; P merges into SPEC. Scope labels: `stage10env` (H, python, files outside
HedgeFund: GM / LSA backends) — W has `stage10envui`. Decisions needed from the user are marked **[DECISION]**.

## 1. What "no .env" means for the backends
- No python code reads a `.env` file any more: `load_dotenv` / `from dotenv` removed everywhere (HedgeFund
  config.py + newsroom/ai.py; GM config.py + assess.py; LSA config.py). `python-dotenv` leaves all three
  requirements.txt (fund/llm.py only mentions it in a comment — P's file, S10-C).
- **Secrets** come ONLY from the Windows Credential Manager (§2). No secret is ever read from `os.environ` either
  (guard test §6) — the old env names (OPENROUTER_API_KEY, DEEPGRAM_API_KEY, OPENSKY_CLIENT_*, …) are dead.
- **Tunables** (poll intervals, retention, model ids, base URL, YouTube URL) come from a per-backend JSON file
  `backend/data/settings.json` (§3) — optional, git-ignored runtime data, no secrets allowed in it.
- **Process environment:** only CORS_ALLOWED_ORIGINS and the test overrides (UFOS_AI_COST_LOG, LOCALAPPDATA, the
  DB-path variables) remain. ALLOW_REMOTE_ACCESS, WS_HOST and HTTP_HOST were REMOVED with Docker (user decision
  2026-10-24 16:56): every server listens on 127.0.0.1, always (api_auth.bind_host()). The import dialog drops them.

## 2. Credentials (Windows Credential Manager, generic credential)
Format unchanged (HedgeFund contract, CredentialStore.cs ↔ strategy_platform/secrets.py): Type GENERIC,
TargetName = `<target>`, CredentialBlob = UTF-8 bytes of the value, Persist LOCAL_MACHINE. Backends only READ,
at use time (a changed key applies without restart), never log / return / send a value except to its service.

| name (UI / API)          | target                                         | used by                              | was (.env)              |
|--------------------------|------------------------------------------------|--------------------------------------|-------------------------|
| `openrouter-api-key`     | `UFOS.ai/HedgeFund/openrouter-api-key` (exists) | HedgeFund, GM, LSA, main app (C#)    | OPENROUTER_API_KEY      |
| `fred-api-key`           | `UFOS.ai/HedgeFund/fred-api-key` (exists)       | HedgeFund                            | FRED_API_KEY            |
| `sec-edgar-user-agent`   | `UFOS.ai/HedgeFund/sec-edgar-user-agent` (exists)| HedgeFund                           | SEC_EDGAR_USER_AGENT    |
| `alpaca-paper-key-id`, `alpaca-paper-secret-key`, `tiingo-api-key`, `binance-demo-api-key`, `binance-demo-api-secret`, `eodhd-api-key` | `UFOS.ai/HedgeFund/<name>` (exist) | HedgeFund | (ALPACA_*/APCA_* for alpaca) |
| `deepgram-api-key`       | `UFOS.ai/LiveStreamAgent/deepgram-api-key` (new) | LSA                                 | DEEPGRAM_API_KEY        |
| `opensky-client-id`      | `UFOS.ai/GlobalMonitoring/opensky-client-id` (new) | GM                                | OPENSKY_CLIENT_ID       |
| `opensky-client-secret`  | `UFOS.ai/GlobalMonitoring/opensky-client-secret` (new) | GM                            | OPENSKY_CLIENT_SECRET   |
| main-app keys (W lists them: whatever Services/EnvConfig.cs reads today) | W defines, same scheme `UFOS.ai/<Module>/<name>` | main app | W fills in |

**DECIDED (user 2026-10-24 16:25): ONE shared OpenRouter key** for all modules = the existing credential
`UFOS.ai/HedgeFund/openrouter-api-key`, name `openrouter-api-key`. No alias: a second (neutral) target would need a
copy or a lookup order, i.e. two places for one secret — so the existing name stays and is documented as shared
(Keys & data shows it once, under "Shared", with the modules that use it). AI budgets stay per module.
Retired names stay as in S10-K (`kraken-demo-*`, removal only).

## 3. Python reader for GM + LSA: `credstore.py` (byte-identical copies, like ai_caps.py)
Files: `Global Monitoring/backend/credstore.py`, `LiveStreamAgent/backend/credstore.py` (a test in each backend
asserts the two copies are byte-identical, as for ai_caps.py). Stdlib + ctypes only.
```
TARGETS = {"openrouter-api-key": ("UFOS.ai/HedgeFund/openrouter-api-key", "OpenRouter API key"),
           "deepgram-api-key": ("UFOS.ai/LiveStreamAgent/deepgram-api-key", "Deepgram API key"),
           "opensky-client-id": ("UFOS.ai/GlobalMonitoring/opensky-client-id", "OpenSky client id"),
           "opensky-client-secret": ("UFOS.ai/GlobalMonitoring/opensky-client-secret", "OpenSky client secret")}
class SecretStoreUnavailable(Exception)   # .user_message: "The Windows Credential Manager could not be read." /
                                          # "<label> is stored in an unreadable format — set it again in ⚙ Keys & data."
class SecretMissing(Exception)            # .user_message: "<label> is not set — enter it in ⚙ Keys & data."
get_secret(name) -> Optional[str]         # value or None; unknown name -> KeyError; store failure -> SecretStoreUnavailable
require_secret(name) -> str               # value or SecretMissing
status(names) -> [{name, label, target, present: bool|None, error: str|None}]   # never values
set_store(store) / MemoryStore            # tests (non-Windows -> SecretStoreUnavailable unless a store is set)
```
Read on every use (no cache) — replaces GM's "re-read .env on every click". Decoding identical to HedgeFund
`secrets.decode_blob` (strict UTF-8, NUL refused, stripped, empty = None).
HedgeFund keeps `strategy_platform/secrets.py` (already CM-only); its `.env` remnants go: the legacy env-name
column of `SECRETS`, `env_shadow_present()`, `config.log_ignored_env_secrets()`.
**API change (W):** GET /v2/settings/keys entries lose `env_names` and `env_value_ignored` (no .env to point to);
remaining fields unchanged {name, label, target, present, error}. POST /v2/settings/keys/changed unchanged
(action "imported" stays — the import dialog audits with it).

## 4. Non-secret settings: `backend/data/settings.json` per backend
Optional file; missing file or missing key → the code default. Read once at start, except the AI fields (model,
base URL), which are read per call like the keys. Strict: unknown key, wrong type or out-of-range value → the
backend logs the problem and REFUSES TO START with "data/settings.json: <key>: <reason>" (as int(os.getenv) crashed
before — no silent fallback). A value that looks like a secret (key name containing key/secret/token/password) is
refused the same way. Keys = today's env names in lower case:
- **HedgeFund** (`HedgeFund/backend/data/settings.json`): openrouter_base_url (https on openrouter.ai, L1 rule),
  openrouter_model, openrouter_analysis_premium_model, market_data_poll_seconds, sec_edgar_poll_seconds,
  sec_edgar_min_request_interval_seconds, fred_poll_seconds, calendar_poll_seconds, insider_display_days,
  insider_display_max, insider_retention_days, ai_results_retention_days. (HEALTH_PORT stays fixed 8867 — the app
  hardcodes it; WS_PORT is gone since S10-A1.)
- **Global Monitoring**: news_poll_seconds, news_max_age_hours, gdacs_poll_seconds, gdacs_max_hours_since_end,
  opensky_poll_seconds, opensky_min_notable_altitude_meters, yahoo_poll_seconds, openrouter_base_url,
  openrouter_model, localnews_max_records, localnews_cache_seconds, localnews_error_cache_seconds,
  event_retention_days, price_retention_days, flight_track_retention_hours, localnews_retention_hours,
  ai_output_retention_days, flight_cache_max_age_hours, assess_reuse_seconds. (Ports 8767/8768 stay fixed.)
- **LiveStreamAgent**: youtube_url (required to start; missing / invalid → the start is refused with "Set the
  YouTube URL in the LiveStream Agent window" — **DECIDED (user 16:25):** the import dialog moves it once, afterwards
  the user edits it in a small field of the LiveStream Agent window; write path see §4a), openrouter_base_url,
  openrouter_model,
  market_data_poll_seconds, verdict_interval_seconds, verdict_max_reason_chars, verdict_confidence_scale,
  verdict_initial_delay_seconds. (Port 8765/8766 fixed.)
**§4a Write path (youtube_url; H's call):** the WPF writes `LiveStreamAgent/backend/data/settings.json` itself —
read the current JSON (keep every other key), set `youtube_url`, write a temp file in the same folder and replace
atomically (File.Replace / Move with overwrite), then restart the LSA backend (the window already starts/stops it).
Validation on both sides, same rule: https, host youtube.com / www.youtube.com / m.youtube.com / youtu.be, no user
info, ≤ 500 chars; the backend re-validates at start and refuses an invalid value with the message above. No backend
route (the backend refuses to start without a URL, so a route could not set the first one). The backend never
writes settings.json.

Shared python helper per backend: `settings_file.py` (byte-identical in the three backends, schema passed in by
config.py) — load(path, schema) -> dict, SettingsError.

## 5. Migration (W, in the WPF — agents never read .env)
Proposal for the import dialog, matching §2–§4: for each discovered .env (`UFOS.ai/.env`, `HedgeFund/backend/.env`,
`Global Monitoring/backend/.env`, `LiveStreamAgent/backend/.env`, plus any other found): secret names (§2 "was"
column) → Credential Manager (verified read-back, keep/replace prompt, audit `key_imported`); known non-secret names
(§4) → that backend's data/settings.json, ONLY when the value differs from the code default (the defaults are
listed in ENV_API_DEFAULTS.json, which H writes next to this file during implementation); deployment switches
(ALLOW_REMOTE_ACCESS, WS_HOST, HTTP_HOST, CORS_ALLOWED_ORIGINS, WS_PORT, HEALTH_PORT) are dropped (reported by
name). Then the .env is deleted — nothing in it is read any more. Unknown names: listed by name, the user decides
"drop" (default) — never copied anywhere.

## 6. Guard tests (python, one per backend)
- no `dotenv` import / `load_dotenv` / `.env` file access in any non-test .py of the backend;
- no `os.getenv` / `os.environ` read of a secret-like name (KEY|SECRET|TOKEN|PASSW|CREDENTIAL, plus the old names)
  outside the allowed process switches (§1) and the worker scrubbing code (validation/parallel.py, papers.py);
- `.env.example` removed (renamed `.env.example.stale` — no secrets in it) and the README says "keys live in the
  Credential Manager (⚙ Keys & data)";
- credstore.py / settings_file.py copies byte-identical.

## 7. Status texts (python side)
- GM /assess, /localnews-summary without key → 503 {"error": "The OpenRouter API key is not set — enter it in ⚙ Keys &
  data.", "code": "key_missing", "key": "openrouter-api-key"}; Credential Manager unreadable → 503 code
  "key_store_unavailable" with its message.
- LSA without Deepgram / OpenRouter key: the start continues (WebSocket + health up), the feature reports
  "Deepgram API key is not set — enter it in ⚙ Keys & data." in its status message (instead of today's .env text);
  missing youtube_url as in §4.
- GM OpenSky without client id/secret: anonymous mode as today (the credential pair is optional), status line says
  "anonymous (no OpenSky credentials in ⚙ Keys & data)".

## 8. Order / impact
python first (H), then W's dialog; until the user ran the import, GM/LSA will report the keys as missing (the
HedgeFund keys are already in the Credential Manager). The backends need a restart after the change.

## 9. Note from P (S10-C, 2026-10-24): strategy_platform/core/llm.py (was fund/llm.py, M3)
- Done: core/llm.py no longer reads the environment (the `os.environ` OPENROUTER_MODEL / OPENROUTER_ANALYSIS_PREMIUM_MODEL
  fallback and `import os` are gone; dotenv only in comments before, now removed too).
- `core.llm.models()` takes the model ids ONLY from `newsroom.ai.analysis_models()` (H's file), read per call.
  **H: when newsroom.ai switches to data/settings.json, keep `analysis_models() -> {"standard", "premium"}`** and
  let it RAISE (e.g. `settings_file.SettingsError`) on an unreadable / invalid file. core/llm turns that into
  `LlmUnavailable("error", "AI model settings unreadable — <reason>")`; the Council judge / sentiment then report
  "AI unavailable" for the run (no silent default). Base URL + key keep coming from `newsroom.ai._current_settings()`.
- Only when newsroom.ai is not importable (offline tests without `openai` — no AI call possible then) does
  core/llm use `DEFAULT_MODELS` = the code defaults (= "no settings.json", which the contract allows).
- `core.llm.safe_base_url` still imports `config.safe_openrouter_base_url` lazily — keep that name in config.py.

## 9. Implementation status (python, Agent H, 2026-10-24) — DONE
- New files: `settings_file.py` (HedgeFund, GM, LSA — byte-identical), `credstore.py` (GM, LSA — byte-identical),
  tests `tests/test_s10_env.py` in all three backends (guards + units). `ENV_API_DEFAULTS.json` (next to this file):
  per backend every settings.json key with kind / default / range and its former .env name, the secret-name →
  credential mapping, the dropped switches — for W's import dialog.
- Strictness as specified: invalid data/settings.json → the backend refuses to start ("REFUSED to start:
  data/settings.json: <key>: <reason>", exit 2); the AI fields are re-read per call (an invalid edit then fails that
  AI call with the message). Ports are fixed constants now (HF 8867, GM 8767/8768, LSA 8765/8766).
- GM: /assess + /localnews-summary without key → 503 {"error": <message>, "code": "key_missing" |
  "key_store_unavailable", "key": "openrouter-api-key"}. OpenSky pair optional (anonymous without it; an unreadable
  store logs a warning and stays anonymous).
- LSA: verdict / analysis without key → {"error": "key_missing" | "key_store_unavailable", "key", "detail"} (no budget
  reserved, no call); transcription without the Deepgram key logs "Transcription not started: Deepgram API key is not
  set — enter it in ⚙ Keys & data." and re-checks every 30 s; missing youtube_url → start refused with "Set the
  YouTube URL in the LiveStream Agent window (it is not set yet)." (exit message).
- HedgeFund: config without dotenv / log_ignored_env_secrets; `config.ai_settings()` for newsroom/ai.py (and for P's
  core/llm.py); `config.safe_openrouter_base_url` kept for existing callers. GET /v2/settings/keys entries =
  {name, label, target, present, error}. `secrets.SECRETS` values are plain labels now.
- `.env.example` of the three backends renamed `.env.example.stale` (templates, no secrets); READMEs say "keys in the
  Credential Manager (⚙ Keys & data), settings in data/settings.json". python-dotenv removed from the requirements
  → the requirements.lock files must be generated AFTER this change (LOCK_API.md one-time step).

## 10. Follow-ups 2026-10-24 (after W's report / Docker decision) — DONE
- POST /v2/settings/keys/changed (HedgeFund) also audits the GM / LSA names of Shared/CredentialCatalog.cs —
  `deepgram-api-key`, `opensky-client-id`, `opensky-client-secret` (secrets.OTHER_MODULE_SECRETS; actions set /
  imported / removed); `openrouter-api-key` was already accepted; retired kraken names: "removed" only (S10-K).
- `ENV_API_DEFAULTS.json` (this folder) = the backends' config.SCHEMA; HedgeFund tests/test_s10_env.py
  `ImportSpecsMatchSchemasTest` parses HedgeFund/Platform/BackendSettingSpecs.cs and requires the same keys, kinds and
  defaults for all three backends (green today).
- Docker removed: Dockerfile + .dockerignore of the three backends and the root docker-compose.yml renamed *.stale;
  api_auth.bind_host(what) always returns 127.0.0.1 (copies identical); WS_HOST / HTTP_HOST gone from the configs.
