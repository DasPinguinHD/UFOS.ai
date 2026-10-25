# Strategy Platform SPEC — archive: 21-options-structure

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 21. Options Structure layer — OS0 contract (2026-10-23), OS1 archive built (2026-10-24)

Plan: project doc `claude/options-structure-plan.md` (concept 2026-10-21, decisions §3, phases §4, point-in-time §6,
pre-registration §7); approved by the user 2026-10-23 ("Yes los gehts"). OS0 = this contract + a READ-ONLY data check +
the frozen pre-registration. OS1 (archive) is built next, both before stage 10; OS2–OS4 after stage 10 and Depth &
Range. This section is the binding contract for OS1–OS4 (review 2026-10-23 incorporated). Nothing here trades; the
traded instrument stays SPY / QQQ **shares** through the existing OMS (options trading deferred, 0DTE struck, no
crypto). Values marked **PROVISIONAL** are confirmed or changed after the user's runs of the check (§21.10; filter,
cadence, OI floor and FRED series confirmed 2026-10-24; staleness + coverage after the RTH run) — since
OS1 they all live in ONE config module (`strategy_platform/options_archive/config.py`, overridable by
`data/options_archive_config.json` without code edits, §21.11).

### 21.1 Data sources (to be confirmed by the user's run of the check tool, §21.9)

| Data | Endpoint (GET only) | Notes |
|---|---|---|
| Option contracts + open interest | Alpaca **trading** API, paper host `https://paper-api.alpaca.markets/v2/options/contracts` (`underlying_symbols`, `status=active`, `expiration_date_gte/lte`, `strike_price_gte/lte`, `limit` ≤ 10 000 — the OS0 tool and the archive use **1 000** (a 10 000 page timed out, user run 2026-10-23), `page_token`) | **`expiration_date_lte` MUST always be passed explicitly** (with `expiration_date_gte`): the endpoint's default is "up to the next weekend". `open_interest` + `open_interest_date` — meaning **confirmed by the OS0 run 20261024T114530Z**: the OI of the trading day `open_interest_date`, published once a day on the morning of the NEXT trading day (Sat 2026-10-24: `open_interest_date` 2026-10-22 on every row — Friday's OI not yet published); OI present on 72–79 % of the contracts; `close_price(_date)` |
| Chain snapshot | Alpaca **data** API `https://data.alpaca.markets/v1beta1/options/snapshots/{SPY,QQQ}` (`feed`, `limit` ≤ 1 000, filters as above, `page_token`) | **`feed=indicative` MUST always be passed explicitly** — the endpoint's default is `opra` (paid, not used; an upgrade is a separate user decision, plan §3). `latestQuote` (`t`, bid/ask/sizes), `latestTrade`, `greeks` (delta, gamma, theta, vega, rho), `impliedVolatility`, `dailyBar` (`v` = the day's volume). Indicative = derived quotes, trades 15 min delayed |
| Option bars | `https://data.alpaca.markets/v1beta1/options/bars` (`1Min`, `1Day`, …; no `feed` parameter) | history since 2024-02 (Alpaca docs) — verified by the check with an expired Feb 2024 contract |
| Underlying | the existing Alpaca stock provider (§3: SIP history, IEX live) | spot for the strike filter = the latest IEX trade at snapshot time (its time = `spot_t`) |
| VIX term structure | FRED via the existing key `fred-api-key` (§7.8 client / newsroom collector): **VIXCLS** (30-day, S&P 500), **VXVCLS** (3-month), **VXNCLS** (Nasdaq-100, for the QQQ replication) | **Confirmed by the OS0 run 20261024T114530Z:** VIXCLS, VXVCLS, VXNCLS exist; NO short-term series on FRED (`VXSTCLS`, `VIX9DCLS` do not exist). → **VIX term structure = VIXCLS (30-day) vs VXVCLS (3-month)**; VXNCLS for the QQQ replication; no other source without a user decision (no fallback) |

**Request budget — fixed split, no cross-process limiter (lead decision 2026-10-23):** Alpaca allows 200 req/min per
account. The backend's `http_gate` source `alpaca` is lowered from 180 to **150 req/min** (+ the existing burst 10 →
≤ 160 in any minute; Market Research keeps ≤ 60 of it, §3 / MR Q3); the OS1 archive process has its **own** gate of
**30 req/min**; total ≤ 190 < 200. We ASSUME that Alpaca's trading API and data API share one per-account limit
(conservative): the official docs state 200/min per account / key, but say nothing explicit about the two APIs
sharing it; the only statement found is a community answer in the Alpaca forum ("completely independent of one's
market data subscription", https://forum.alpaca.markets/t/is-there-a-way-to-increase-the-200-min-api-call-limit-for-the-trading-endpoint/18110)
— not official, so the split stays conservative. The archive runs one request at a time with a pause between pages.
One filtered snapshot of both symbols = contract pages (once a day) + chain pages; the check reports the measured
numbers for 1 / 13 / 28 / 78 snapshots per session (`d_budget`). A snapshot that cannot finish by its slot time is a
gap, never completed later with other data.

### 21.2 Universe, filter, cadence and slots

- **SPY** (primary), **QQQ** (replication). No other underlyings in Phase A.
- **Filter (CONFIRMED 2026-10-24 — user decision after the OS0 run 20261024T114530Z):** strikes within
  **±10 %** of the spot at snapshot time, expiries **0 … 45 calendar days** (same-day expiries are ARCHIVED because they
  carry gamma, even though 0DTE trading is struck). Measured grid: ±5 / 10 / 15 % × ≤ 30 / 45 / 60 days.
- **Cadence (CONFIRMED 2026-10-24 — user decision, ≈ 11.5 GB / year accepted; OS0 run: 7 requests per intraday
  snapshot, 19 per EOD snapshot incl. the contract pages):** contracts / OI once per morning (08:30 New York, before the open); one EOD snapshot
  (16:15 New York); intraday snapshots every 15 minutes in RTH (09:45 … 15:45) PLUS fixed slots at **09:55** (H2: GEX at
  10:00) and **15:25** (H1: chain at or before 15:25) → **28** snapshots per session (the check's `PROPOSED_CADENCE`).
- **Slot rule (implementation rule — the frozen pre-registration is NOT edited):** a slot snapshot must FINISH by its
  slot time (e.g. 15:25:00.000 New York): it starts early enough (slot − 90 s, adjusted from the measured pages and
  latency); every quote stamped AFTER the slot time is excluded (counted as a gap for that contract); a snapshot not
  finished by the slot time is a gap for that slot. This makes "chain snapshot at or before 15:25" hold.
- **Early-close days (OS1 decision 2026-10-24, implementation rule):** NYSE calendar `core/calendars.py` (holidays +
  13:00 early closes). Intraday slots only while the market is open (09:45 … 12:45), 09:55 kept, **no 15:25 slot**
  (after the close). **H1 and H2 undefined on early-close sessions — CONFIRMED by the user 2026-10-24** ("Ja, lass die
  nicht auswerten"; decided before the first early close 2026-11-27): such sessions are excluded from BOTH
  evaluations with the reason "early close" (the frozen pre-registration is not edited); EOD at close + 15 min
  (13:15). Holidays / weekends: no slots. Slots are New York wall-clock times (zoneinfo) — the EU/US DST gap week
  (2026-10-25 … 11-01) is tested.

### 21.3 Point-in-time rules (binding for every archived and computed value)

- Every value carries: `valid_for` (the market time it describes), `available_at` (the earliest time the platform
  could have known it), `observed_at` (when the archive fetched it), and — through its run (`os_runs`, §21.5) —
  `source` (`alpaca_trading` | `alpaca_data` | `fred`), `feed` (`indicative` | null), `model_version` (`raw` for
  archived values; OS2 sets its own, e.g. `gex-1`) and `assumptions_hash` (sha256 of the canonical JSON of filter +
  cadence for raw values; of the model assumptions — dealer positioning, rates, dividends — for computed values).
  All times are integer epoch milliseconds UTC in storage.
- **Open interest:** `valid_for` = `open_interest_date` (trading day T); `available_at` = `observed_at` of the fetch
  that returned it, and never earlier than **T+1 08:00 New York** (T+1 = the next trading day; publication on the
  morning of T+1 **confirmed by the OS0 run 20261024T114530Z**; the exact 08:00 time is watched by the archive's
  `early_oi` flag in its first OI runs) — an earlier fetch is flagged
  `early_oi` and not used before that time. **Staleness:** OI counts for a decision only if `valid_for` == the
  PREVIOUS trading session of the decision day; otherwise the dependent value is `unavailable` + an `os_gaps` row.
  A corrected OI for the same `valid_for` is a NEW row with a later `available_at`; readers take the row with the
  latest `available_at` ≤ decision time.
- **Chain snapshot:** `valid_for` = `latestQuote.t` per contract (the slot is kept as `slot`); `available_at` =
  `observed_at` (receive time). **Staleness (PROVISIONAL):** a quote counts only if its age at the SLOT time is
  ≤ 5 min (and it is not after the slot, §21.2); otherwise that contract is `unavailable` + an `os_gaps` row.
  Indicative trades are 15 min delayed: a trade-based value gets `available_at` ≥ trade time + 15 min.
- **Coverage (PROVISIONAL):** GEX (and every aggregate over the chain) is `unavailable` when fewer than **80 %** of the
  in-filter contracts have a usable quote + greeks + OI at the slot.
- **Bars (approximation backtest only; the bars endpoint has no feed parameter):** the rule is based on the
  `backfill` flag only — rows fetched from history after the fact are `backfill = 1`, labelled exploratory, and never
  count for H1/H2; `available_at` = bar end for live-collected bars.
- **FRED:** `valid_for` = the observation date; `available_at` = `observed_at` (FRED publishes with a lag; ALFRED
  vintages are not used in Phase A).
- **Readers** (computations, backtests, the regime service, the evaluation of H1/H2) use ONLY rows with
  `available_at` ≤ decision time; a missing value makes the dependent result `unavailable` with the reason.
- **Gaps** (a slot not collected or not finished in time, a page failed, a field missing, a stale quote / OI) are
  recorded in `os_gaps` and shown — NEVER filled with stand-ins (no interpolation, no previous value, no second source).

### 21.4 Archive service (OS1) — **built 2026-10-24** (details §21.11)

- Its own background process, started by a **Windows scheduled task** (independent of the UFOS.ai app and backend):
  data collection only — no trading, no AI, no broker writes. Code inside `HedgeFund/` (scope guard); its own
  security review in OS1.
- Keys only from the Windows Credential Manager (`alpaca-paper-key-id` / `alpaca-paper-secret-key`, `fred-api-key`
  via `strategy_platform/secrets.py`), read per run, never written anywhere else, never logged.
- **No listening port** (no HTTP server; outgoing GETs only on the §21.1 allow-list — the same allow-list pattern as
  the OS0 tool, paper host from `live_guard.paper_endpoint`, redirects not followed, TLS via `core/tls.py`); its own
  Alpaca gate of 30 req/min (§21.1).
- Own log (`data/logs/options_archive.log`, rotated, redacted) + a **gap report** (per day: expected vs collected slots,
  missing pages / fields, stale quotes / OI) and a status file the backend reads.
- Status, start / stop and the gap report in **⚙ Keys & data → Data sources**; the scheduled task is registered only
  after the user confirms it there — never silently. **Confirmation style (decision 2026-10-23, replaces "typed
  confirmation"; typed words stay only for the live-trading switch):** ONE confirmation dialog showing a precise
  summary — what runs (the archive process, GET-only, no trading), when (at logon of the current user, sleeps until
  the next NY slot), which account / keys (the current Windows user; Alpaca PAPER key pair + `fred-api-key` from the
  Credential Manager, read per run), where data goes (`data/options_archive.db`, status file, log) and how to stop
  (Unregister… / Task Scheduler / the stop file) — plus an audit entry (`options_archive_registered` /
  `…_register_failed` / `…_unregistered` / …, `POST /v2/options-archive/audit`).
- **Registration mechanics (lead decisions 2026-10-24, review M1 + Round 2b):** no PowerShell and no script text from
  the backend. The backend supplies DATA only (paths + the summary, `GET /v2/options-archive/registration`); the WPF
  builds the task XML from a FIXED template and runs `%SystemRoot%\System32\schtasks.exe` as the current user: task
  **`UFOS.ai OptionsArchive` in the ROOT task folder** (a standard user may not create a subfolder), `LogonTrigger` of
  the current user, `InteractiveToken`, `LeastPrivilege`, `MultipleInstancesPolicy IgnoreNew`, `StartWhenAvailable
  false`, no time limit, `RestartOnFailure` 3× / 5 min, action = the venv's `pythonw.exe "<backend>\tools\
  options_archive.py" service`, working directory = the backend dir; `/Create … /XML … /F` then `/Run` (it starts
  right away — the summary says so); unregister = `/End` + `/Delete`. No admin rights, no stored password.
- **Sandbox mode** (backend marker `data/SANDBOX_ACTIVE`): registering and manual runs are disabled (status
  `actions_enabled: false`, registration `available: false`, audit of register / run-once actions → 409 `sandbox`,
  CLI `run-once` / `run-slot` → exit 6); unregister stays possible; an already registered service keeps collecting
  (it only writes its own `options_archive.db`).
- Own SQLite file `HedgeFund/backend/data/options_archive.db` (WAL; the archive is the only writer; the backend opens it
  read-only), own schema version, insert-only like `hedgefund.db`; retention: raw snapshots kept (no deletion in Phase
  A; size from §21.9 `e_storage`).

### 21.5 Tables (OS1 migration `strategy_platform/options_archive/db.py`, schema version 1 — **built 2026-10-24**)

All tables insert-only (triggers); times are integer epoch ms UTC; run-constant columns live in `os_runs` only; a null
is a recorded gap, never a default. Option `type` / `strike` / `expiration` are derived from the OCC symbol (and
stored once in `os_contracts`).

| Table | Columns | Key |
|---|---|---|
| `os_runs` | `id`, `kind` (`oi` / `eod` / `intraday` / `fred`), `slot_ms` (planned), `started_ms`, `finished_ms`, `status` (`ok` / `partial` / `failed` / `late`), `requests`, `pages`, `error`, `source`, `feed`, `spot_source` (e.g. `alpaca_iex_trade`), `model_version`, `assumptions_hash` | `id`; unique (`kind`, `slot_ms`) |
| `os_contracts` | `symbol` (OCC), `underlying`, `type`, `strike`, `expiration`, `style`, `size`, `first_seen_ms` | (`symbol`, `first_seen_ms`) — a changed attribute is a new row |
| `os_contract_sightings` | `run_id`, `symbol`, `seen_ms` (replaces a mutable `last_seen`) | (`run_id`, `symbol`) |
| `os_open_interest` | `run_id`, `symbol`, `open_interest`, `valid_for` (T, date), `available_ms`, `observed_ms`, `early_oi` | (`symbol`, `valid_for`, `available_ms`) — a correction is a new row with a later `available_ms` |
| `os_chain_snapshots` | `run_id`, `symbol`, `spot`, `spot_t_ms`, `bid`, `ask`, `bid_size`, `ask_size`, `quote_t_ms` (= `valid_for`), `last`, `last_t_ms`, `daily_volume` (`dailyBar.v`), `iv`, `delta`, `gamma`, `theta`, `vega`, `rho`, `available_ms`, `observed_ms` | (`run_id`, `symbol`) — the slot comes from the run |
| `os_bars` | `symbol`, `timeframe`, `t_ms` (= `valid_for`), `o`, `h`, `l`, `c`, `v`, `n`, `vw`, `available_ms`, `observed_ms`, `backfill` | (`symbol`, `timeframe`, `t_ms`) |
| `os_fred` | `series_id`, `date` (= `valid_for`), `value`, `available_ms`, `observed_ms` | (`series_id`, `date`, `available_ms`) |
| `os_gaps` | `id`, `run_id`, `slot_ms`, `kind`, `underlying`, `symbol`, `what`, `reason`, `recorded_ms` | `id` |

Storage estimate per row (incl. SQLite record header and the primary-key index): snapshot ≈ 260 B, OI ≈ 125 B — the
check's `e_storage` uses these.

As built (differences to the plan above): `os_runs` also has `slot_label` (`09:55`, `oi`, `eod`, …), `retries`,
`app_version`, kind `manual` (a "run one snapshot now" run — never counts for a slot) and an index (`slot_ms`, `kind`)
instead of a UNIQUE key (insert-only: a re-run is a new run; the gap report takes the best run per slot), and
`skew_ms` (server `Date` − PC clock of the run; review m8). `os_open_interest` is keyed by (`symbol`, `valid_for`,
`observed_ms`) — two observations never collapse (two pre-floor fetches share `available_ms` = the floor; readers take
the latest `available_ms` ≤ t, ties → the latest `observed_ms`). `PRAGMA recursive_triggers=ON`, so even the implicit
delete of an `INSERT OR REPLACE` aborts. Two extra
tables: `os_schema` (`version`, `applied_ms`; plus `PRAGMA user_version`) and `os_configs` (`hash`, `json`,
`first_ms` — the canonical config behind each run's `assumptions_hash`). `os_open_interest.early_oi` (0/1). Every
table has `BEFORE UPDATE` / `BEFORE DELETE` triggers that abort. `os_bars` is created but not filled by OS1 (bars are an
OS2 / backtest concern).

### 21.6 Data flow and point-in-time timeline

```
 Alpaca trading API ─ contracts + OI (08:30 NY) ─┐
 Alpaca data API ─ chain snapshots (slots, RTH) ─┼─► OS1 archive service ──► options_archive.db ──► OS2 computations
 FRED ─ VIXCLS / VXVCLS / VXNCLS (daily) ────────┘   (scheduled task,        (raw, PIT columns,      (IV, greeks, GEX ×
                                                       GET only, no port,      os_gaps)                2–3 dealer variants,
                                                       own 30 req/min gate)                            flip, walls, skew,
                                                                                                         expected move)
                                                                                                            │
                     consumers ◄── regime service (available_at ≤ decision time) ◄──────────────────────────┘
       (DSL operators OS4 · allocator · risk checks · Explain · decay monitor · Structure tab / overlays OS3 ·
        H1/H2 evaluation after ≥ 120 sessions)
```

```
 day T                                   │ day T+1
 09:30 open … 15:25 slot … 16:00 close   │ 08:00 NY (confirmed, OS0): OI of T published → available_at(OI_T) ≥ T+1 08:00
 slot s: the snapshot finishes by s; valid_for = quote time ≤ s; age at s ≤ 5 min; available_at = receive time ≤ s
 decision at 15:30 on T+1 may use: OI valid_for = T (the previous session, available since T+1 08:00) + the 15:25
 snapshot of T+1;  decision at 15:30 on T may NOT use OI of T (not available before T+1 08:00)
```

### 21.7 Robustness and evaluation rules (from the pre-registration)

- GEX is computed under 2–3 dealer-positioning assumptions in parallel (plan §6); a signal counts only if it holds
  under all of them.
- **No running results** of H1/H2 before the evaluation date — the UI shows only a progress counter (sessions archived,
  data health). The approximation backtest from 2024-02 is reported but labelled exploratory and does not count.

### 21.8 Frozen pre-registration

- File: **`HedgeFund/docs/preregistration/OS-H1-H2-2026-10-23.md`** (the folder `HedgeFund/docs/` was created for it),
  frozen **2026-10-23**, before the first archived day; user approval 2026-10-22 "Ja, so einfrieren".
- **sha256 `95d3ac66736ad358df23b4628f0c32425f4b0b4be37138f7b52a813afc3b8195`** (2 198 bytes, UTF-8, LF line
  endings; `HedgeFund/.gitattributes` marks `docs/preregistration/**` as `-text` so no CRLF conversion changes the
  bytes). The hypothesis block is the plan's §7 text verbatim; the header adds status, approval, the rules and the
  footnote for "§6".
- `tests/test_os_preregistration.py` recomputes the sha256 and fails on any change. Rule: the file is never edited;
  changes only as NEW, separately dated hypothesis files (their own sha256 recorded here). Implementation rules that
  make the hypotheses measurable (§21.2 slot rule, §21.3 staleness) live here, not in the frozen file.

### 21.9 OS0 read-only check — `tools/verify_alpaca_options.py`

- Usage (user, PowerShell, from `HedgeFund\backend`; once during US market hours and once after the close — the tool
  reads the Alpaca market clock itself): `.\.venv\Scripts\python.exe tools\verify_alpaca_options.py`
- GET only on an allow-list of (host, path): the paper host from `live_guard.paper_endpoint("alpaca_paper")`
  `/v2/options/contracts`, `/v2/clock`; `data.alpaca.markets` `/v1beta1/options/snapshots/(SPY|QQQ)`,
  `/v1beta1/options/bars`, `/v2/stocks/(SPY|QQQ)/trades/latest`; `api.stlouisfed.org` `/fred/series`,
  `/fred/series/observations`. Anything else is refused before sending; https only, default port; redirects not
  followed (a 3xx comes back as that status); TLS `core/tls.py`. Since OS1 the allow-list, opener, body cap,
  retry and sanitizing live in the shared module `strategy_platform/options_archive/transport.py` (one copy for the
  tool and the archive); the tool keeps its own settings (timeouts, page size, pauses) in a thin `Http` subclass.
- Timeouts per endpoint: 60 s for contracts / snapshots / bars, 20 s for the clock, the latest trade and FRED.
- An answer > 20 MB stops the run (never truncated). 0.5 s between pages, ≤ 40 pages per query, contracts
  `limit=1000` per page. One progress line per page ("contracts SPY: page 3 — 3000 rows so far").
- The first HTTP 429 stops the run.
- Network errors (os0 add-on, user runs 2026-10-23: a 10 000-contract page timed out at 20 s; "URLError" alone was
  not diagnosable):
  - After a timeout or connection reset, ONE bounded retry follows, 2 s later. It is counted in the request
    budget and listed in the report as `requests.retried` [{endpoint, path, reason}]; the second failure stops the
    run ("(after 1 retry)").
  - Every other network error stops at once.
  - The reason is sanitized: `Type: message` of the root cause (e.g. gaierror, SSLCertVerificationError,
    ConnectionRefusedError, TimeoutError), with URLs, query strings and key=… redacted, ≤ 200 characters.
    Non-network exceptions are reported by type only.
- The clock comes first: 401/403 → stop (exit 2, nothing else requested); clock unreadable → stop (exit 3; no date is
  assumed — no UTC fallback). The New York date from the clock bounds every query.
- Checks a–f (PASS / FAIL / PARTIAL + facts): a contracts — the window today … +60 d AND the full list (explicit far
  bound), each with contracts, expiries, malformed `expiration_date` rows, OI + OI-date share, pages; b snapshots
  (explicit `feed=indicative`, explicit expiry bounds; greeks / IV share, quote ages vs the snapshot time — signed,
  never clamped; quotes after the snapshot time and older than 5 min counted; latency, pages, filter grid counts);
  c bars (ATM call of the nearest expiry ≥ 7 days: 1Day / 1Min — no ATM contract is a FAIL; history probes
  `SPY240119C00480000` (before 2024-02) and `SPY240216C00500000` → earliest bar); d budget (EOD / intraday requests,
  minutes and RTH share at the archive's 30 req/min, next to the backend's 150 + burst 10 and Alpaca's 200);
  e storage (rows × bytes, §21.5); f FRED (exists / "not on FRED" / error with FRED's `error_message`; a Credential
  Manager read error is reported as such, never as "no key").
- A data error inside a check (ValueError / TypeError / KeyError / AttributeError) makes THAT check FAIL with the
  error (type + message) — the run continues. Network errors (sanitized reason, see above — a FRED URL carries the
  key), a refused request, an oversized answer or a 429 stop the run: "RESULT: STOPPED", every check that did not run is
  FAIL "not run".
- Keys: the Alpaca paper pair + `fred-api-key` from the Credential Manager (`secrets.get_secret`), never printed,
  logged or written; the JSON report is scrubbed for them before saving:
  `HedgeFund/backend/data/os0/verify_alpaca_options_<UTC>.json` (git-ignored: `data/os0/*` in `backend/.gitignore`).
- Exit codes: 0 no FAIL · 1 a FAIL · 2 Alpaca keys missing / unreadable / rejected · 3 stopped (network, clock, refused
  request, oversized answer) · 4 stopped on HTTP 429.
- Tests (fakes, no network; one loopback HTTP server for the no-redirect opener): `tests/test_os_verify_alpaca_options.py`
  (27: allow-list + GET only, explicit expiry bounds against a fake with Alpaca's next-weekend default, explicit feed,
  ATM found and 1Day / 1Min measured, no key material in output / report — also not from an exception text with a
  FRED URL, paging + pauses, 429 / timeout / clock / keys-rejected stops with the right exit codes and "not run"
  checks, malformed `expiration_date`, a data error = FAIL not stop, signed quote ages, missing fields → PARTIAL with
  0 %, market closed, FRED errors vs missing series vs key read error, no-redirect + size limit, budget / storage
  arithmetic, the backend gate = the split, retry once after a timeout then stop, no retry for other network errors,
  sanitized reasons, progress lines, page size 1 000), `tests/test_os_preregistration.py` (2).

### 21.10 OS0 results (the user's run 2026-10-24) and what is still open

- **Run `data/os0/verify_alpaca_options_20261024T114530Z.json`** (Saturday, market closed; no FAIL): SPY 5 542
  contracts in the 60-day window (12 746 in the full list), QQQ 5 394 (10 172); OI present 72–79 %,
  `open_interest_date` 2026-10-22 everywhere (OI of T published the morning of the next trading day); greeks / IV
  present 90–94 % (indicative); option bars back to 2024-01-18; budget 7 requests per intraday snapshot, 19 per EOD
  snapshot; FRED: VIXCLS / VXVCLS / VXNCLS exist, VXSTCLS / VIX9DCLS do not.
- **Confirmed 2026-10-24** (user decision 13:49; set as the defaults in `options_archive/config.py`, no override
  file; `CONFIRMED` there): filter ±10 % / ≤ 45 days, cadence 28 snapshots per session (≈ 11.5 GB / year accepted),
  the meaning of `open_interest_date` + the T+1 08:00 floor, the FRED series.
- **Still PROVISIONAL until the RTH run on Monday 2026-10-26 (14:30–21:00 local):** the quote-staleness threshold
  (5 min at the slot) and the coverage minimum (80 %) — a Saturday run cannot measure quote freshness
  (`PROVISIONAL_KEYS` in `config.py`; the status payload's `config.provisional_keys`).

### 21.11 OS1 archive — as built (2026-10-24, review round 2 included)

Status: **built — awaiting the OS0 data-check numbers (§21.10) + the user's registration in ⚙ Keys & data**. Nothing
has been registered or run against Alpaca / FRED (build machine: no network, no keys); the real `data/*.db` were not
touched — all tests use temp dirs and a fake transport.

- **Package `HedgeFund/backend/strategy_platform/options_archive/`** (under strategy_platform: it reuses the NYSE
  calendar `core/calendars.py`, `core/tls.py`, `live_guard`, `secrets.py`; a top-level `options_archive` package would be
  shadowed by the CLI `tools/options_archive.py`): `config.py` (ALL provisional numbers), `transport.py` (shared with the
  OS0 tool), `db.py` (migration §21.5), `slots.py` (NY slots), `collector.py` (one run = one slot), `report.py` (gap
  report / status payloads), `service.py` (process loop, lock, log), `registration.py` (registration DATA).
- **Config** (`config.py`, `VERSION os1-1`, `provisional: true` while any key is provisional; `CONFIRMED` /
  `PROVISIONAL_KEYS` / `key_status()` — kept out of the hashed config — say which, also in the status payload's
  `config.confirmed_keys` / `provisional_keys`): symbols SPY/QQQ, strike band ±10 %, expiries 0…45 d,
  cadence 15 min 09:45…15:45, fixed slots 09:55 / 15:25, EOD close + 15 min, OI 08:30, FRED 08:45 (VIXCLS, VXVCLS,
  VXNCLS; last 10 observations), slot lead 90 s, quote max age 300 s, coverage 80 %, OI floor 08:00 NY, own gate
  30 req/min, page size 1 000, 0.5 s between pages, ≤ 40 pages, ONE retry 2 s later, timeouts contracts 30 s /
  snapshots 20 s / stock 10 s / FRED 20 s, EOD grace 45 min, heartbeat 60 s. Override: `data/options_archive_config.json`
  with any subset of these keys. Validation (review M2): every number has a range (`RANGES`, e.g. cadence 5…390 min,
  retries 0…1, max_pages 1…100, timeouts 1…120 s, heartbeat 10…120 s), finite numbers only (NaN / Infinity / 1e999
  refused), whole numbers where the default is one, symbols ⊆ {SPY, QQQ}, HH:MM 00:00…23:59, `oi_slot` ≥ `oi_floor`,
  `oi_slot` / `fred_slot` before `intraday_first`. A bad override is REFUSED with the reason (never clamped): the archive
  does not start (exit 3), the backend routes answer 409 `config_invalid`. `assumptions_hash` = sha256 of the canonical
  config JSON (stored once in `os_configs`, per run in `os_runs`).
- **Slots** (`slots.py`): per NY trading day `oi` 08:30, `fred` 08:45, 25 intraday (09:45…15:45 every 15 min) + 09:55 +
  15:25, `eod` 16:15 = **30 slots** (early close: §21.2 — intraday 09:45…12:45 + 09:55, no 15:25, EOD 13:15). The
  slot loop is bounded (cadence < 1 raises). **Windows** — a slot is collected AS THAT SLOT only inside its window:
  chain slots slot − 90 s … slot; `eod` slot − 90 s … slot + 45 min; `oi` / `fred` from the slot until the close of
  THAT session. Outside → `missing` (never run later under the old slot, also not after the PC slept).
- **Collector** (`collector.py`): chain slot = spot (the latest IEX trade fetched at the run start, i.e. before the
  slot; its time stored as `spot_t_ms`, not age-checked) → snapshots with explicit `feed=indicative`,
  `expiration_date_gte/lte`, `strike_price_gte/lte` (±band around that spot), `limit`; every quote is checked at the
  SLOT time: after the slot → gap `after_slot`; older than 300 s → `stale_quote`; missing quote / greeks / IV →
  `field_missing`; usable share < 80 % → `coverage` (quote + greeks + IV only — OI is joined by the OS2 computation, which
  applies its own coverage rule); a failed page → `page_failed`; finished after the slot → run `late`. The row is still
  stored with its own times (nothing replaced). `oi` slot = contracts with explicit expiry bounds and NO strike filter
  (all strikes of the expiry window — the 08:30 run has no trustworthy pre-market spot) → `os_contracts` (new row only
  when an attribute changes), `os_contract_sightings`, `os_open_interest` (new row only when new / changed;
  `available_ms` = max(observed, T+1 08:00 floor), `early_oi`); a contract listed twice with different OI in one answer
  → gap `oi_conflict` (first value kept); `stale_oi` gap when `valid_for` ≠ the previous trading session. `fred` =
  observations (desc, last 10) → `os_fred` (new row only for a new or revised value; "." → null + gap), `not_on_fred` /
  `fred_error` gaps. **Clock** (review m8): every answer's `Date` header vs this PC's clock → `os_runs.skew_ms`; > 30 s a
  `clock_skew` gap, > 5 min the run stops (exit 3, nothing stored). One SQLite transaction per run. Gap rows are
  per (underlying, kind) with the count in the text (no per-contract rows, no example symbols).
- **HTTP** (`transport.py`): allow-list = §21.9; own sliding-window gate (≤ 30 in ANY 60 s); every attempt counted;
  ONE retry after a timeout / connection reset / HTTP 5xx (recorded in `os_runs.retries`), then the run stops
  (`failed`, sanitized reason); HTTP 429 → stop at once (no retry; the service waits for the next slot); 401/403 →
  keys rejected (exit 2).
- **Process** (`service.py`, `tools/options_archive.py service`): single instance (lock file `data/options_archive.lock`,
  OS lock — released by the OS when the process dies; a second archive process → exit 5, also `run-once` while the
  service runs); sleeps in ≤ 30 s steps until the next slot start; heartbeat ≥ every 60 s in `data/os_archive_status.json`
  — also between the pages of a long run; stops cleanly on Ctrl+C, the scheduled task's End, or the file
  `data/options_archive.stop`; **no listening port**. Resilience (review M3): an unexpected error in one run is logged
  with its traceback (redacted) and recorded as a `failed` run + `crash` gap — the service continues; status-file
  writes retry (Windows `PermissionError` on replace) and never raise; a fatal loop error → state `error` with a
  sanitized `last_error` and exit 7 (the task's RestartOnFailure restarts it). The calendar's end (no data for the
  coming days) → state `error` with the message, retry hourly (no guessed trading days). Log
  `data/logs/options_archive.log` (2 MB × 3, rotated, no propagation to other handlers) with a redaction filter for
  every key read in the process and `api_key=` / `APCA-*` header values.
- **CLI** `tools\options_archive.py run-once | run-slot <HH:MM|oi|eod|fred> | status | gaps [--date YYYY-MM-DD] |
  service`; exit codes 0 ok · 1 partial / failed run · 2 keys missing / unreadable / rejected · 3 stopped (network,
  clock, config, calendar) · 4 HTTP 429 · 5 another archive process holds the lock · 6 refused in sandbox mode · 7
  internal error. `run-once` = today's catch-up (windows above) + one `manual` chain snapshot of both symbols;
  `run-slot` outside the slot's window → a `manual:<slot>` run (kind `manual`, never ranked as the slot's run). Both
  write ONLY the `manual` block of the status file (finished_at, status, run_ids, exit_code, error) — never the
  service's state or heartbeat (review m11).
- **Registration** (`registration.py`; DATA only, §21.4): task name `UFOS.ai OptionsArchive` (root folder),
  `backend_dir`, `workdir`, `pythonw` (null when missing → `available: false`, no console fallback), `python`, `tool`,
  `service_args`, `run_once_args`, `available` / `unavailable_reason` / `sandbox`, and `summary` (what / when — "starts
  right after you confirm and at every logon" / account / keys / data / stop — what Unregister, Task Scheduler End and
  the stop file do). The XML template and the schtasks calls are fixed in the WPF.
- **Backend API** (read-only, `strategy_platform/api/options_archive_routes.py`, behind api_auth; the DB is opened
  `mode=ro`; works when the archive never ran → `state: not_set_up`): `GET /v2/options-archive/status` (state
  not_set_up / ok / degraded / error, db, service heartbeat — alive = ≤ 180 s, `manual` block, last run (with
  `skew_ms`), next slot, today's expected / collected / partial / late / failed / missing / pending slots and
  `fixed_slot_gaps` (a late 09:55 / 15:25 counts as a gap), counts, last error sanitized, config, `sandbox` /
  `actions_enabled` / `actions_note`, paths), `GET /v2/options-archive/gaps?date=` or `from&to` (≤ 31 days inclusive;
  years 2000…2100, else 400; per day slots × status + gap rows ≤ 200; a day beyond the calendar shows `error`),
  `GET /v2/options-archive/runs?limit=1…200`, `GET /v2/options-archive/registration`, `POST /v2/options-archive/audit`
  `{action ∈ registered | register_failed | unregistered | unregister_failed | run_once | run_once_failed, detail ≤ 300}`
  → `hedgefund.db` audit `options_archive_<action>` (the only write; sandbox: register / run-once actions → 409).
  Gap kinds: `after_slot`, `stale_quote`, `field_missing`, `coverage`, `page_failed`, `stale_oi`, `oi_conflict`, `late`,
  `not_collected`, `network`, `rate_limited`, `keys`, `refused`, `not_on_fred`, `fred_error`, `clock_skew`, `crash`.
  Field-level payloads: `options_archive/report.py` (`status_payload`, `day_report`, `run_view`) and
  `registration.payload`.
- **Tests** (no network, temp dirs, fake Alpaca / FRED with a `Date` header, fake clock): `tests/test_os_archive_db_slots.py`
  (7: migration + insert-only triggers on every table, a newer schema refused, config override, 30 slots, early close
  2026-11-27 + Thanksgiving, DST week 2026-10-25 / 11-01, next slot), `tests/test_os_archive_collector.py` (15: chain PIT
  stamping + explicit parameters, staleness / after-slot / field / coverage gaps with rows kept, late snapshot, no spot,
  OI floor + corrections + stale / early OI, FRED, retry once then stop, 5xx retried once, keys rejected → exit 2, 429,
  30 / min gate, allow-list, no key material), `tests/test_os_archive_service.py` (11: loop + catch-up + missing slots,
  stop file, early-close day, US DST change in the loop, lock, CLI, API routes, registration DATA, audit),
  `tests/test_os_archive_round2.py` (26: config ranges incl. cadence 0 / NaN / Infinity, CLI refuses a bad config,
  bounded slot loop, INSERT OR REPLACE cannot delete, OI pre-floor correction + duplicate conflict, clock skew ok / gap /
  stop / no header, crashing run recorded + loop continues + redacted traceback, status write failure, fatal error →
  exit 7, heartbeat during a long run, slot windows, missed morning slots after sleep, run-slot outside the window →
  manual, calendar end in status + hourly retry, run-once writes only the manual block, sandbox exit 6 + 409, internal
  error exit 7, late fixed slot = gap, /gaps 31 days inclusive + bad dates 400).
- **Not in OS1:** the WPF card (other agent, against the same contract); option bars (`os_bars` empty; OS2 /
  backtest); any computation (OS2); confirming the provisional numbers (the user's OS0 run); the early-close rule
  (pending the user's confirmation, §21.2).
