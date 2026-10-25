# Strategy Platform SPEC — archive: 14-stage5b-papers-committee

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 14. Stage 5b — paper → strategy + investment committee (contract, 2026-10-17)

Interview decisions (user, 2026-10-17): papers come in by **PDF upload,
pasted text, or a link to arXiv or SSRN only** (no other sites);
**replicated** = our backtest over the paper's period hits the paper's
reported Sharpe within **±30 %** and with the same sign, otherwise "not
replicated" with both numbers side by side; the **committee runs
automatically after a finished validation when enabled in Settings**
(default off); when the setting is off, a **"Convene committee…" button**
is shown instead (only then). Budget = the ideation budget of §13 ($10/month,
$2/day). The memo is advisory only — it never changes a strategy's state.

### 14.1 Paper ingestion (`research/papers.py`)
- Inputs: PDF upload (≤ 20 MB, ≤ 150 pages), pasted text (≤ 200,000 chars),
  link: `https://arxiv.org/abs/<id>` | `https://arxiv.org/pdf/<id>` |
  `https://papers.ssrn.com/sol3/papers.cfm?abstract_id=<n>` and the SSRN
  delivery PDF the abstract page links to — exact host allow-list
  (arxiv.org, export.arxiv.org, papers.ssrn.com), https only, redirects only
  within the allow-list, SSRF-hardened fetch (resolve → reject private /
  loopback / link-local → connect to the checked IP with SNI/cert for the
  hostname — same rules as market_research/news media fetch; hard 30 s
  deadline, size cap 20 MB, content-type allow-list pdf/html for the
  abstract page), through http_gate (own source "papers", conservative
  limit). SSRN often requires a login/captcha → clear message "SSRN did
  not provide the PDF without login — download it in the browser and upload
  it", never a scraping workaround.
- PDF → text in a separate process with limits (time 60 s, memory guard),
  pure-Python extractor added to requirements (pinned); no JavaScript, no
  embedded files, no OCR. The paper text is UNTRUSTED (prompt injection):
  passed to the LLM only as quoted data with the instruction that it is
  data; never shown as markup.
- Stored: `papers(id, source kind, url/filename, sha256, title, authors,
  year, text_chars, created_at)` + extracted text in a file under
  backend/data/papers/<sha256>.txt (not the PDF itself — copyright: keep the
  user's upload only as long as needed; delete the temporary PDF after
  extraction... renamed/removed by the backend's own temp handling).

### 14.2 Paper → strategy + replication (`research/paper_strategy.py`)
- LLM extraction (feature `sp-paper`, ideation budget) → JSON {hypothesis,
  universe, signal construction, rebalancing, holding period, sample period
  (start/end), reported results {sharpe, cagr?, max_dd?, net/gross,
  costs assumed}, caveats}, each field with the page/quote snippet it came
  from (≤ 300 chars) → **shown to the user for confirmation/edit before
  anything runs** (the reported Sharpe and period are user-confirmed
  numbers, not trusted AI output).
- Then a Studio-like draft (DSL only, operator requests as text) → validator
  → strategy `origin="paper"` with provenance {paper id, sha256, title} →
  **replication backtest over the paper's period** (clamped to our data;
  the clamp is shown; holdout window excluded as always) → replication
  score: replicated iff our Sharpe within ±30 % of the reported one and
  same sign; report shows both numbers, period used vs paper period,
  gross/net mismatch notes, universe differences (survivorship warning).
  The replication run is a normal finished run → counts as a trial.

### 14.3 Investment committee (`research/committee.py`)
- Five roles in order: Researcher (thesis), Quant (implementation vs
  thesis, data issues), Risk Manager (stress, drawdowns, sizing), Reviewer /
  Devil's Advocate (overfitting, weak gates, regime dependence), Chair
  (memo). Input per role: the strategy logic, the latest validation report's
  gate facts, the §11 attribution facts, memory facts (§13.2), previous
  roles' outputs. NEVER holdout results unless the holdout was already
  opened for this exact version (then included and labelled).
- Memo JSON {summary, arguments_for[], arguments_against[], confidence
  0–1 with reason, conditional sizing (as % of capital, rules), stop rules,
  open questions}; numbers checked with the §11.2 number check against the
  facts (unverified marked). EU AI Act label per role output + cost line.
- Model: Standard by default, Premium selectable per run (manual) / in
  Settings (auto). Cost estimate shown before a manual run.
- Trigger: Settings `committee_auto_after_validation` (default false). If
  true → after every finished (non-holdout) validation run, if the ideation
  budget allows; otherwise a note "committee skipped: <reason>" on the
  validation report. If false → the WPF shows "Convene committee…" on the
  validation report and strategy detail (hidden when the setting is on).
- Stored insert-only: `committee_runs`, `committee_messages`; progress
  events per role (polling); cancellable between roles (charged so far).

### 14.4 API (behind api_auth, strict validation, audited where state/cost)
`POST /v2/papers` (multipart PDF | {text} | {url}) → paper;
`GET /v2/papers`, `GET /v2/papers/{id}`;
`POST /v2/papers/{id}/extract` {model} → extraction (unconfirmed);
`POST /v2/papers/{id}/confirm` {edited fields} → draft + replication run;
`GET /v2/papers/{id}/replication`;
`POST /v2/committee` {strategy_id, version?, validation_run_id?, model} →
202 run; `GET /v2/committee/{id}`; `POST /v2/committee/{id}/cancel`;
`GET/PUT /v2/settings/committee` {auto_after_validation, model}.

### 14.5 WPF
- Studio gets a "Paper" tab: upload / paste / link (arXiv/SSRN only, the
  rule shown), extraction review form (fields with source snippets,
  editable, "Confirm & build strategy"), replication card (paper vs ours,
  replicated / not replicated chip, period/clamp notes, "Open report").
- Committee: on the validation report + strategy detail — "Convene
  committee…" (only when auto is off) with model + estimate; progress per
  role; memo view (AI labels, unverified numbers red, plain text); history.
  Settings: "Run the investment committee automatically after each
  validation" + model.

### 14.6 Tests
No network (fakes): host allow-list + redirect rules + SSRF (private IPs,
DNS rebinding) + size/time limits; SSRN login page → message; PDF extractor
limits (huge/odd PDFs, timeout) in the subprocess; injection text stays
quoted; extraction requires confirmation before any run; replication score
rule (±30 %, sign, clamping); committee role order, holdout exclusion,
number check, cancel, budget blocks, auto vs manual trigger; insert-only.

### 14.7 5b backend notes (X2, implemented 2026-10-17: §14.1–§14.4, §14.6 backend)

All routes behind api_auth, strict bodies/queries (400 `{error, errors}`),
other errors `{error, code, …}` (as §11.6/§13.5), never a stack trace. AI
outputs carry `provenance` (EU AI Act label) + `cost_usd`. Budget = the
ideation budget of §13 (402 `budget` / 503 `spend_unrecorded` as §13.5).

**Papers**
- **POST `/v2/papers`** — one of: `multipart/form-data` with ONE part
  `file` (PDF, ≤ 20 MB, ≤ 150 pages); JSON `{text: str 1..200,000, title?:
  str ≤ 300}`; JSON `{url: "https://arxiv.org/abs/<id>" |
  "https://arxiv.org/pdf/<id>" | "https://papers.ssrn.com/sol3/papers.cfm?
  abstract_id=<n>"}` → 200 `paper`. Errors: 400 `url_not_allowed` ("Only
  arXiv and SSRN links are accepted (https://arxiv.org/abs/…,
  https://arxiv.org/pdf/…, https://papers.ssrn.com/sol3/papers.cfm?
  abstract_id=…)."), 400 `pdf_invalid` (not a PDF / encrypted / too many
  pages / no text — reason in `error`), 413-style 400 `too_large`, 502
  `fetch_failed` (reason), 502 `ssrn_login` ("SSRN did not provide the PDF
  without login — download it in the browser and upload it"), 400
  `fetch_rejected` (private address, redirect outside the allow-list, wrong
  content type, size), 504 `extract_timeout` ("The PDF text extraction took
  longer than 60 s."). The same sha256 again → the existing paper (200,
  `duplicate: true`).
- `paper` = `{id: "p-<12 hex>", source_kind: "pdf"|"text"|"arxiv"|"ssrn",
  url: str|null, filename: str|null, sha256, title: str|null, authors:
  str|null, year: int|null, text_chars: int, pages: int|null,
  created_at, duplicate: bool, status: "new"|"extracted"|"not_relevant"|
  "confirmed", latest_extraction_id: int|null, strategy_id: str|null}`.
  Title/authors/year come from the PDF metadata / arXiv / the user's
  `title` (untrusted plain text).
- **GET `/v2/papers`** `?limit=1..200 (50)` → `{papers: [paper]}` (newest
  first); **GET `/v2/papers/{id}`** → `{paper, extractions: [extraction]
  (newest first), confirmation: confirmation|null, replication:
  replication|null, text_preview: str (first 2,000 chars, plain text)}`.
- **POST `/v2/papers/{id}/extract`** `{model: "standard"|"premium"}` → 200
  `extraction` (unconfirmed; nothing runs). `extraction` = `{id, paper_id,
  created_at, model, model_tier, relevant: bool, not_relevant_reason:
  str|null, fields: {hypothesis, universe, signal, rebalancing,
  holding_period: {value: str|null, source: str|null (≤ 300-char quote /
  page ref)}, sample_period: {start: "YYYY-MM-DD"|null, end|null, source},
  reported: {sharpe: number|null, cagr: number|null, max_dd: number|null,
  basis: "net"|"gross"|"unknown", costs_assumed: str|null, source},
  caveats: [str] (≤ 8)}|null, note: str|null, provenance, usage, cost_usd,
  text_used_chars, text_truncated: bool}`. `relevant: false` (the text is not
  about a testable trading/investment strategy) → `fields: null`, `note` =
  the fixed non-AI text "This text does not describe a testable trading or
  investment strategy, so the Studio cannot build a strategy from it."; the
  AI reason is in `not_relevant_reason`… shown only as "Reason given by the
  AI" (plain text).
- **POST `/v2/papers/{id}/confirm`** `{extraction_id: int, model:
  "standard"|"premium", sample_start: "YYYY-MM-DD", sample_end:
  "YYYY-MM-DD", reported_sharpe: number, reported_basis?:
  "net"|"gross"|"unknown", hypothesis?, universe?, signal?, rebalancing?,
  holding_period?: str ≤ 2,000}` (the user-confirmed values; the reported
  Sharpe and the period are REQUIRED here) → 200 `{confirmation, draft
  (§13.5 draft shape), replication}`; the draft call uses the Studio rules
  (kind/scope guard, validator, one repair round). Errors: 409
  `not_relevant`, 409 `already_confirmed`, 409 `period_outside_data`
  ("The paper's period lies outside the data we can backtest (…)."), 404.
  `confirmation` = `{id, paper_id, extraction_id, created_at, confirmed:
  {…the body fields…}, strategy_id|null, version|null}`.
- **GET `/v2/papers/{id}/replication`** → `replication` = `{state:
  "none"|"waiting"|"started"|"start_failed", run_id, run_status,
  paper_period: {start, end}, period_used: {start, end}|null, clamped:
  bool, clamp_notes: [str], reported_sharpe, our_sharpe: number|null,
  tolerance: 0.30, replicated: true|false|null (null until the run is
  done), verdict: "replicated"|"not replicated"|"pending"|"failed",
  notes: [str] (gross/net mismatch, universe/survivorship warnings,
  costs), key_metrics|null}`. Replicated ⇔ same sign AND |ours − reported|
  ≤ 0.30 × |reported|. When the job slot is busy the replication waits
  ("Waiting for the backtest slot"; own FIFO, dispatched after the Studio queue when the slot frees).

**Committee**
- **POST `/v2/committee`** `{strategy_id, version?: int, validation_run_id?:
  str, model: "standard"|"premium"}` → 202 `{run}`; 409 `busy` (one
  committee at a time), 409 `no_validation` ("Run a validation first — the
  committee reviews a validated report."), 402/503 budget.
- **GET `/v2/committee/estimate`** `?strategy_id=&version=&validation_run_id=
  &model=` → `{estimate_usd, roles: 5}` (pre-call estimate of all 5 calls).
- **GET `/v2/committee`** `?strategy_id=&validation_run_id=&limit=` →
  `{runs: [run]}` (newest first). **GET `/v2/committee/{id}`** → `{run,
  messages: [committee_message] (role order), memo: memo|null,
  memo_check: check|null}`. **POST `/v2/committee/{id}/cancel`** (no body) →
  `{run}` (stops before the next role; charged so far).
- `run` = `{id: "cr-<12 hex>", strategy_id, version, validation_run_id,
  model_tier, model, trigger: "manual"|"auto", created_at, status:
  "running"|"done"|"failed"|"cancelled"|"skipped"|"interrupted", note:
  str|null (e.g. "committee skipped: <budget message>"), progress: {done:
  int, total: 5, current_role: str|null}, roles: ["researcher", "quant",
  "risk_manager", "reviewer", "chair"], holdout_included: bool, cost_usd}`.
- `committee_message` = `{id, run_id, role, seq, output: {summary: str,
  points: [str]} (roles 1–4) | memo (chair), check: {ok, unverified,
  unknown_ids} (§11.2), provenance, usage, cost_usd, created_at}`.
- `memo` = `{summary, arguments_for: [str], arguments_against: [str],
  confidence: {value: 0..1, reason}, sizing: {percent_of_capital: 0..100,
  rules: [str]}, stop_rules: [str], open_questions: [str]}` — advisory
  only, never changes a strategy's state.
- **GET/PUT `/v2/settings/committee`** `{auto_after_validation: bool,
  model: "standard"|"premium"}` (defaults false / standard; PUT audited
  `committee_settings_changed`). Auto: after every finished non-holdout
  validation run (job after-hook); a budget block → a run with status
  `skipped` and `note` "committee skipped: <reason>".
- **Number-check section keys** (committee, per message): roles 1–4
  `summary`, `points.<i>`; chair (memo) `summary`, `arguments_for.<i>`,
  `arguments_against.<i>`, `confidence.reason`, `sizing.rules.<i>`,
  `stop_rules.<i>`, `open_questions.<i>`; `offset` = character offset in
  that text. `sizing.percent_of_capital` / `confidence.value` are structured
  fields (not checked). Holdout facts (only when the holdout was opened for
  this exact version) get ids `holdout.<original id>` and sit in
  `data.holdout {label, facts}`; otherwise no holdout id reaches a prompt.
- **Implementation**: `research/papers.py` (URL rules `classify_url`,
  `PaperFetcher` — SSRF rules re-implemented locally so the MR fetcher is
  unchanged; gate source `papers`, 6/min, 1 attempt), `research/pdf_worker.py`
  (the subprocess: `python -I pdf_worker.py <pdf> <max pages> <max chars>
  <memory MB>`; RLIMIT_AS on POSIX, a working-set watchdog thread on Windows
  (exit 3); no JavaScript / attachments / OCR), `research/paper_strategy.py`,
  `research/committee.py`; `requirements.txt` pins `pypdf==6.18.0` (pure
  Python). The temporary PDF (`tempfile.mkstemp`, prefix `ufos-paper-`) is
  removed right after the extraction; only `data/papers/<sha256>.txt` +
  the `papers` row remain. Extracted text ≤ 2,000,000 chars; the extraction
  prompt uses the first 60,000 (`text_truncated`). Multipart upload: read in
  64 KB chunks with the 20 MB cap (`routes.read_paper_body`); the JSON body
  limit was raised to 6 × 200,000 + 4,096 bytes for pasted texts.
- **Extraction** (feature `sp-paper`, label "Hedge Fund · Paper import",
  max_tokens 2,500, fixed `EXTRACT_SYSTEM_PROMPT`, paper title/text as quoted
  JSON fields): missing `relevant` or any schema break → 502 `bad_json`
  (cost booked). **Confirm**: draft call through the Studio prompt + rules
  (`prompts.build_messages` with the confirmed fields as the quoted
  `user_request`, `scope_guard`, validator, one capped repair round); an
  off-topic / non-draft answer → `{confirmation: null, draft:
  {discarded_reason}, note: <the Studio scope note>}`; an invalid draft →
  `confirmation: null` (nothing stored, the user may confirm again — costs
  again). Valid → `create_from_paper` (origin `paper`, envelope
  `ai_provenance.paper {id, sha256, title}`) + `paper_confirmations` row +
  replication with explicit `start/end` (clamped: start ≥ the data start
  `seam.EARLIEST_START`, end ≤ `families.training_end`); the Tiingo key
  check of POST /v2/backtests applies (missing key → `start_failed` with its
  message). A paper period ending before the data start → 409
  `period_outside_data` BEFORE any AI call.
- **Committee**: feature `sp-committee`, label "Hedge Fund · Investment
  Committee", max_tokens 1,800, fixed `SYSTEM_PROMPT` + role task; the
  worker thread calls the roles in order, budget-checked per call (the
  first call also before the run is created → 402 without a run). A failed
  call ends the run `failed` (note names the role and the charge);
  `run.cost_usd` = sum of the stored role messages. One committee at a time
  per backend; `interrupted` = no terminal event and no live worker (backend
  restart). Validation runs from before §11 (no attribution) → gate facts
  only (noted in `data.run.note`).
- **Job after-hook** (`backtest/jobs._after_job`, never raises): memory →
  `committee.on_validation_finished` (setting on + kind validation + done) →
  Studio queue → paper replication queue.
- **Storage v13**: `papers`, `paper_extractions`, `paper_confirmations`
  (UNIQUE paper_id), `paper_replications` (events waiting|started|
  start_failed), `committee_runs`, `committee_messages` (UNIQUE run_id+seq),
  `committee_events` (started|role_done|cancel_requested|cancelled|failed|
  done|skipped) — all insert-only; meta `committee_auto_after_validation`
  ("0"/"1"), `committee_model`. Registry: origin `paper` + `create_from_paper`.
- Tests: `test_sp_papers.py` (10; the real-pypdf test is skipped when pypdf
  is missing), `test_sp_paper_strategy.py` (8), `test_sp_committee.py` (8).
- **Review fixes (2026-10-17):** extraction `not_relevant_reason` is NEVER
  returned (always null; kept in storage/audit) — the client gets only the
  fixed `note`. Extraction gains `discarded_reason: null|"topic_check_failed"`:
  the independent topic check (§13.5) sees the displayable fields (values,
  sources, caveats); a failed check → `relevant: false`, `fields: null`, the
  fixed note, paper status `not_relevant`. The confirm draft's spec texts
  are topic-checked before the strategy is saved (failed → `confirmation:
  null`, `draft: {discarded_reason: "topic_check_failed"}`, the Studio scope
  note). Committee: each role's texts are topic-checked; a failed check →
  that message's `output` = `{summary: "This committee contribution was not
  shown: it did not pass the topic check.", points: []}` (chair: the memo
  fields empty, `memo: null`), `check: null`, plus `discarded: true`,
  `discarded_reason: "topic_check_failed"` on the message; the run
  continues. A failed role call now always ends the run with a note naming
  the role and the charge; an auto committee that cannot start (budget,
  missing model, …) → a `skipped` run with "committee skipped: <reason>".


### 14.8 5b WPF notes (XW, 2026-10-17: papers, replication, committee — §14.5, bound to §14.7)

- **Files**: new `Platform/StudioPaperView.cs` (Studio "Paper" tab), `Platform/CommitteePanel.cs`,
  `Platform/PlatformClient.Papers.cs` (DTOs + calls; the only multipart call = POST /v2/papers with ONE part
  `file`, application/pdf; add timeout 120 s, extract 120 s, confirm 210 s), `Platform/PlatformClient.Committee.cs`,
  `HedgeFundWindow.CommitteeSettings.cs`; changed `Platform/StudioView.cs` (tabs "Chat" | "Paper", the paper
  view created on first use; `NoticeBlock` / `SetNotice` / `ApplyBackendNoticeAsync` shared),
  `Platform/ExplainPanel.cs` (`Rich` internal — reused for the committee's unverified numbers),
  `Platform/ValidationReportView.cs` + `Platform/LibraryView.xaml.cs` (CommitteePanel), `HedgeFundWindow.xaml(.cs)`
  (committee setting below the ideation budget, 30-s refresh).
- **Paper tab**: left = "Upload PDF…" (OpenFileDialog; > 20 MB refused client-side with the size, no upload), link
  field with the rule shown (client check `^https://(arxiv.org/(abs|pdf)/…|papers.ssrn.com/sol3/papers.cfm?abstract_id=<n>)$`;
  the backend's allow-list is authoritative — `url_not_allowed`, `ssrn_login`, `fetch_*`, `pdf_invalid`,
  `extract_timeout` verbatim), paste text (≤ 200,000) + optional title (≤ 300); paper list. Right = pinned
  AI-interaction notice (text from GET /v2/studio/sessions?limit=1 — no local text), paper meta (untrusted plain
  text) + text preview, "1 · Extraction (AI)" (AI header + cost line + caveat; Standard/Premium; "Extract with AI" /
  "Extract again (costs again)"; no estimate number — §14.7 offers none for the extraction); `relevant: false` →
  the backend's fixed note as a neutral block + "Reason given by the AI" in an AI box. "2 · Review": every field an
  editable TextBox (≤ 2,000) with "Source in the paper: “…”" below (none → amber "check this value yourself"),
  sample start/end + reported Sharpe REQUIRED (client format check), basis net/gross/unknown, other reported values
  shown read-only; "Confirm & build strategy" (model toggle, confirmation dialog: AI draft + replication backtest =
  a trial) → POST confirm with exactly the confirmed fields (empty text fields omitted); an invalid draft is shown
  after the reload. After confirmation: "Confirmed values" (from `confirmation.confirmed`). "3 · Replication" card:
  verdict chip (replicated / not replicated / pending / failed), "Paper Sharpe X · our Sharpe Y — replicated = same
  sign and within ±<tolerance>", paper period vs period used (+ "clamped", `clamp_notes`), `notes` (gross/net,
  survivorship, costs), waiting / start_failed / running state, key metrics, "Open report", "Open in Library".
  Polled every 3 s (GET …/replication) only while waiting/running and visible.
- **Committee panel** (validation report of a non-holdout validation run with its run id; Library detail of the
  current version): GET /v2/settings/committee decides — auto on → note only; auto off → "✦ Convene committee…"
  (options: Standard/Premium with GET /v2/committee/estimate per tier, "Convene (5 AI calls)", Close); unknown →
  no button + the reason. Runs = GET /v2/committee?strategy_id=&validation_run_id=; `skipped` runs show their `note`
  verbatim (amber); newest running/non-skipped run shown; history expander (newest first). Run view: header
  (status, trigger, version, tier, cost, holdout_included), roles ✓ / ▶ / ○ from `progress`, Cancel (confirm) while
  running (polled every 2 s while loaded); memo view = pinned notice, one AI container per role ("✦ AI-generated ·
  model · time · role" + cost line + caveat), roles 1–4 summary + points, chair = memo (summary, arguments for /
  against, confidence, sizing, stop rules, open questions; `memo_check` else the message check). Unverified
  numbers via `ExplainPanel.Rich` on the field keys `summary`, `points.<i>`, `arguments_for.<i>`,
  `arguments_against.<i>`, `confidence.reason`, `sizing.rules.<i>`, `stop_rules.<i>`, `open_questions.<i>` —
  **§14.7 does not fix the `section` names of the committee check**; entries with another key are listed under the
  role ("Could not be marked in the text") — never dropped. Backend: please confirm/align these keys.
- **Settings**: "Run the investment committee automatically after each validation" (ToggleSwitch) + Standard /
  Premium; disabled until GET answers; a click PUTs both (needs both known — otherwise asks for the missing one).
- **Review fixes (2026-10-17)**: M1 — a not-relevant / `topic_check_failed` extraction shows ONLY the backend's
  fixed `note` as a neutral block plus a neutral cost line ("— no AI text is shown"); no AI box, no AI header
  (`not_relevant_reason` is never displayed). L3 — confirm with `draft.discarded_reason` (or an invalid draft) →
  a neutral "System note — no strategy, no backtest" with the response's fixed `note`; `draft_invalid` (or no
  reason + invalid) adds the validator findings; `topic_check_failed` / other reasons the note only. Committee:
  a role message with `discarded: true` is a neutral "System note · <role> · cost" with the fixed text from
  `output.summary` — no AI label, no number check. DTOs: `StudioDraft.discarded_reason`,
  `PaperConfirmResult.note`, `PaperExtraction.discarded_reason`, `CommitteeMessage.discarded(_reason)`,
  `StudioMessage.topic_check`.
