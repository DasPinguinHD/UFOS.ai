# Strategy Platform SPEC — archive: 07-stage2-strategy-definition

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 7. Stage 2 — strategy definition (contract, 2026-10-02)

Interview decisions (user, 2026-10-02): universe = liquid US ETFs + crypto;
navigation shell in the Hedge Fund window now (Overview = today's dashboard,
Library); **JSON editor + file import** allowed; the rule-based part of the
agent committee becomes a library strategy. Security measures accepted:
size/depth limits, provenance of imported files not trusted, texts marked as
unverified ("external"). NOT accepted: restricting specs to known symbols →
any syntactically valid symbol is allowed (residual risk: many symbols → many
data requests; bounded by the 500-symbol limit and the HTTP gate).

**numpy** is allowed from stage 2 on (it is in the venv via pandas; add
`numpy>=2.1` to requirements.txt). Import it lazily inside functions of
modules that main.py imports at startup; tests may use it.

### 7.1 Layout

```
strategy_platform/strategy/
  limits.py      all size limits (one place) + safe JSON parser
  schema.py      StrategySpec schema, canonical JSON, content hash
  dsl.py         operator registry (name, category, arity, arg kinds, doc), expr_text()
  validator.py   validate(spec) -> ValidationResult (errors + warnings + facts)
  evaluate.py    numpy evaluator: Panel -> signal panel (causal)
  panel.py       Panel (dates x symbols per field), build from bars (calendar alignment)
  portfolio.py   signal row -> target weights (mode, selection, weighting, caps)
  sdk.py         Strategy base (on_bar(ctx) -> weights), Context (PIT guard), SpecStrategy
  universe.py    presets (liquid_etfs_crypto), categories
  summary.py     summary_texts(spec) for the UI (§7.11 summary)
  preview.py     preview() on current bars (§7.7)
  features/      external feature providers (macro.regime_score, calendar.event_risk)
  alphas/        library: one module per family, LIBRARY list of specs (+ metadata)
strategy_platform/lifecycle/
  registry.py    strategies, immutable versions, transitions, seeding of the library
```

### 7.2 Limits (`limits.py`) and safe parsing

`MAX_TEXT_BYTES = 262144` (UTF-8 bytes of the submitted JSON text),
`MAX_DEPTH = 32` (JSON nesting), `MAX_NODES = 2000` (all JSON values in the
spec), `MAX_WINDOW = 2520` (bars), `MAX_SYMBOLS = 500`, `MAX_PARAMS = 32`,
`MAX_FEATURES = 64`, `MAX_TAGS = 16`, text lengths: name 80, tag 32,
description 4000, param description 300, feature name 40 (`^[a-z][a-z0-9_]{0,39}$`).
`parse_spec_text(text) -> dict`: rejects (ValueError with a clear message,
incl. line/column for JSON syntax errors) non-str, > MAX_TEXT_BYTES, invalid
JSON, duplicate keys (`object_pairs_hook`), `NaN`/`Infinity` (`parse_constant`),
non-object root, depth > MAX_DEPTH, nodes > MAX_NODES. Depth is checked
WITHOUT recursion limits being hit (iterative walk). No `eval`, no pickle,
nothing executed — a spec is data only.

### 7.3 StrategySpec v1 (`schema.py`)

User-owned document (what the editor shows / files contain):

```json
{
  "schema": "ufos.strategy/1",
  "name": "Cross-sectional momentum 12-1",
  "description": "…",
  "tags": ["momentum"],
  "category": "momentum",               // momentum|reversal|trend|volatility|value|carry|allocation|alpha101|composite|other
  "universe": {"preset": "liquid_etfs_crypto", "asset_classes": ["equity"]}
              // or {"symbols": ["SPY", "TLT", "BTC-USD"]} (≤ MAX_SYMBOLS, instruments.instrument() must accept each)
  "timeframe": "1Day",                   // only 1Day in stage 2
  "parameters": {"lookback": {"value": 252, "min": 126, "max": 378, "step": 21, "integer": true, "description": "…"}},
  "features": {"mom": {"op": "…"}},      // named sub-expressions, referenced by {"ref": "mom"}; no cycles
  "signal": {"op": "cs_rank", "args": [{"ref": "mom"}]},
  "portfolio": {
    "mode": "long_only",                 // long_only|short_only|long_short
    "selection": {"type": "top_n", "n": 5},   // top_n|bottom_n|top_bottom_n|threshold|all ; threshold: long_above / short_below
    "weighting": "equal",                // equal|signal|inverse_vol (inverse_vol needs vol_lookback)
    "vol_lookback": 60,
    "max_weight": 0.25, "gross_exposure": 1.0
  },
  "rebalance": {"frequency": "monthly"}, // daily|weekly|monthly (first trading day of week/month)
  "risk": {"stop_loss_pct": null, "take_profit_pct": null, "vol_target_annual": null, "max_drawdown_stop_pct": null},
  "execution": {"order_type": "market", "timing": "next_open"},   // timing: next_open|next_close ONLY (no same-bar fills)
  "costs": {"model": "paper_broker"},    // or {"model": "custom", "fee_bps": 5, "slippage_bps": 5}
  "provenance": {}                       // optional, free-form claims; see 7.6
}
```

Unknown keys anywhere → error (path given). All numbers finite. A parameter
is used in expressions as `{"param": "lookback"}`; using it where an integer
window is required needs `"integer": true` and integer min/max/value; a
parameter's value must lie in [min, max]. Unused parameters/features →
warning. `canonical_json(spec)` = sorted keys, no whitespace, UTF-8;
`content_hash = sha256(canonical_json(user-owned part without provenance))`
hex. Missing optional blocks get documented defaults written INTO the stored
spec on save (so the hash covers them) — these are spec defaults, not data
fallbacks.

### 7.4 DSL (`dsl.py`) — expression nodes

- number literal (finite); `{"param": name}`; `{"ref": feature_name}`;
  `{"field": f}` with f ∈ `open high low close volume vwap trade_count`;
  `{"ext": name}` external feature (7.8); `{"op": name, "args": [...]}`.
- Element-wise: `add sub mul div neg abs sign log sqrt pow signed_power
  min max clip where gt ge lt le eq ne and or not is_nan fill_nan`
  (div by 0, log ≤ 0, sqrt < 0 → NaN; `pow` exponent must be a constant).
- Time series, per symbol, window `n` = positive integer constant or integer
  param ≤ MAX_WINDOW: `lag(x,n≥1) delta pct_change ts_mean ts_std ts_sum
  ts_min ts_max ts_argmin ts_argmax ts_rank ts_zscore ts_product ts_skew
  ts_corr(x,y,n) ts_cov(x,y,n) decay_linear ewm_mean(x, halflife)`;
  helpers `returns(n)` = pct_change(close,n), `rsi(n)`, `atr(n)` (Wilder, on
  high/low/close), `sma(x,n)`=ts_mean.
- Cross-sectional, per date over symbols with a value: `cs_rank` (0..1,
  average ties) `cs_zscore cs_demean cs_scale` (sum |x| = 1) `cs_winsorize(x,k)`.
- **Look-ahead ban (validator):** unknown op → error; `lag`/windows must be
  ≥ 1 (no 0 or negative shifts, no centred windows); no op may read bars
  after t; `execution.timing` must be next_*; `ext` only providers with
  `point_in_time = True`.
- `dsl.catalogue()` → list of `{name, category, signature, description}` (for
  the editor help); `expr_text(node)` → readable one-liner, e.g.
  `cs_rank(pct_change(close, 252) - pct_change(close, 21))`.

### 7.5 Validator (`validator.py`)

`validate(spec: dict) -> ValidationResult{ok, errors:[{path, code, message}],
warnings:[…], facts:{max_lookback, required_fields, required_ext, symbols_count,
asset_classes, param_count, node_count}}`. Paths in JSONPath style
(`$.signal.args[0].args[1]`). Codes (stable, tested): `unknown_key`,
`missing_key`, `bad_type`, `out_of_range`, `unknown_op`, `bad_arity`,
`bad_window`, `look_ahead`, `unknown_param`, `unknown_ref`, `ref_cycle`,
`unknown_field`, `unknown_ext`, `not_point_in_time`, `bad_symbol`,
`too_many_symbols`, `limit_exceeded`, `bad_timing`, `text_too_long`.
Warnings: `unused_param`, `unused_feature`, `mixed_calendars`,
`survivorship` (preset universes are today's ETF list — mild selection bias),
`short_history` (max_lookback > 756), `crypto_short_selling` (perps).
`validate_text(text)` = parse (7.2) + validate; parse errors become one error
with code `parse_error` and `line`/`column`.

### 7.6 Provenance and external text (import security)

The SYSTEM owns the envelope; nothing in a file can set it. Envelope per
version: `{strategy_id, version, content_hash, origin: library|user|import|
duplicate, created_at, created_by: user|system, parent: {strategy_id, version}
or null, ai_generated: false|true|"unknown", external_text: bool,
claimed: {...} or null, source_file: {name, sha256, bytes} or null}`.
- Import: the file's `provenance` block (and any state/validated/run-card
  like claims inside it) is moved to `claimed` verbatim (size-limited) and
  shown ONLY as "claimed by the file"; state always starts at `draft`;
  `ai_generated = "unknown"` unless the user later sets it; `external_text =
  true`. The file name only (no directory path) is stored.
- `external_text` texts (name, description, tags, param descriptions,
  feature names, claimed) are plain text everywhere (WPF TextBlocks, never
  rendered as markup/links) and get the UI chip "external text · unverified".
  Stage 5 must wrap them in delimiters before giving them to an LLM (note in
  §7.12).
- Editing a user strategy creates a new version (immutable versions); the
  strategy's state returns to `draft` (evidence belonged to the old version).
  Library strategies are read-only → "Duplicate as draft".

### 7.7 Evaluation (`panel.py`, `evaluate.py`, `portfolio.py`)

- `Panel`: `dates` (ascending), `symbols`, `fields: {name: ndarray[dates,
  symbols]}` (NaN = no value). `Panel.from_bars(bars_by_symbol, calendar)`:
  equities on NYSE sessions; crypto-only universe on UTC days; mixed universe
  on NYSE sessions with each crypto value = last crypto daily bar KNOWN by
  that session's close (`data/pit.known_at`) — warning `mixed_calendars`.
- `evaluate(spec, panel) -> ndarray[dates, symbols]` (signal); NaN
  propagation per numpy; deterministic; time-series ops need ≥ n valid values
  in the window else NaN.
- **Causality property (the look-ahead acceptance test):** for every operator
  and every library strategy, evaluating on `panel[:t+1]` gives the same row
  t as evaluating on the full panel (within 1e-9), for random t on synthetic
  data.
- `portfolio.target_weights(spec, signal_row, panel, t) -> {symbol: weight}`:
  NaN excluded; ties broken by symbol; respects mode, selection, weighting,
  `max_weight` (excess is NOT redistributed — held as cash), gross/net;
  deterministic.
- `rebalance_dates(spec, dates)`.
- `preview(spec, as_of=None, days=20) -> {as_of, rows: [{symbol, signal,
  rank, target_weight}], history: [{date, weights}], warnings, data: {source,
  from, to, symbols_missing}}` loads daily bars via `data/cache.get_bars`
  (Alpaca, one source). A symbol without data → listed in
  `symbols_missing` and the call FAILS with `DataSourceUnavailable` if the
  data source failed; a symbol that simply has no history (e.g. not listed
  yet) is excluded and listed (that is a property of the universe, not a
  fallback). Bound: ≤ 60 s wall time, else a clear error.

### 7.8 External features (`features/`)

`ExtFeature(name, description, point_in_time=True, scope="global"|"asset_class",
load(start, end) -> {date: value} or {asset_class: {date: value}})`.
Registry `features.get(name)`, `features.catalogue()`.
- `macro.regime_score`: the rule-based macro agent's score, computed ONLY from
  FRED data as first published (ALFRED vintages: request with
  `realtime_start=1776-07-04&realtime_end=9999-12-31`, use each observation's
  first `realtime_start` as its known date). Stored in hedgefund.db table
  `ext_observations(series_id, date, value, realtime_start, source,
  fetched_at)`. FRED key via `secrets.require_secret("fred-api-key")`. Failure
  → `DataSourceUnavailable` (no stand-in).
- `calendar.event_risk`: 1 on the trading day before/of a high-impact US
  release (CPI, employment situation, FOMC, GDP), else 0 — from FRED release
  dates (dates are scheduled in advance → point in time). If a release
  cannot be loaded → the feature fails with a clear message.
- Tests use fakes only (no network).

### 7.9 Alpha library v1 (`alphas/`)

Each entry = a valid spec + metadata `{key, family, reference (paper/book,
year), expected_holding_days, notes}`. ~15 classic + Alpha101 subset:
time-series momentum 12-1 (Moskowitz/Ooi/Pedersen 2012), cross-sectional
momentum 12-1 (Jegadeesh/Titman 1993), dual momentum (Antonacci 2014),
short-term reversal 5d, 52-week-high proximity (George/Hwang 2004), SMA
50/200 trend, Donchian breakout 55/20 (turtle), RSI(2) mean reversion
(Connors), Bollinger z-score reversion, low volatility (Ang et al. 2006),
inverse-volatility allocation, sector-rotation momentum (sector ETFs),
risk-on/off switch (SPY vs TLT trend), crypto trend with vol target,
volatility-targeted trend; Alpha101 (Kakushadze 2016) subset that needs only
OHLCV/vwap and no industry neutralisation (#2, #3, #4, #6, #9, #12, #13,
#14, #16, #33, #41, #53, #54, #101) with a note that they were designed for
single-stock cross-sections; **Council Rules (no AI)** (named "Agentic
Committee (rules)" until 2026-10-06; key `agentic_committee_rules`): Technical
(SMA50/200, 20d/60d momentum, RSI, ATR) + `macro.regime_score` + 
`calendar.event_risk` as size reduction, mirroring `fund/agents` rules.
Acceptance: every entry validates with 0 errors, passes the causality
property and produces finite weights on synthetic data.

### 7.10 Lifecycle registry (`lifecycle/registry.py`, hedgefund.db v2)

Tables: `strategies(id PK, name, origin, category, state, current_version,
created_at, updated_at, state_changed_at, library_key UNIQUE NULL)`,
`strategy_versions(strategy_id, version, spec_json, content_hash,
envelope_json, validation_json, created_at, PK(strategy_id, version))` —
insert-only (no UPDATE/DELETE code path), `strategy_transitions(id, ts,
strategy_id, version, from_state, to_state, actor, reason, evidence_json)`.
States: `idea draft backtested candidate validated incubation live scaled
retired`. Stage-2 rules: user may move `idea↔draft`, any → `retired`,
`retired → draft`; every state from `backtested` upward needs evidence
(run-card ids, stage 3+) → refused with "needs a backtest run card — comes
with stage 3"; `live`/`scaled` additionally need the live guard (stage 8).
Reason text required (≤ 500) for every user transition; audit row each.
Library seeding at startup: idempotent by `library_key` + content hash; a
changed library spec becomes a new version of the library strategy (origin
library, actor system).

### 7.11 API v2 additions (behind api_auth; error shapes as §4)

- `GET /v2/dsl/operators` → `{operators: catalogue(), fields, ext: features.catalogue()}`
- `GET /v2/universes` → `{universes: [{key, name, symbols: [{symbol, category, asset_class}], note}]}`
- `GET /v2/strategies?state=&origin=&category=` → `{strategies: [{id, name,
  origin, category, state, current_version, content_hash, tags,
  updated_at, ai_generated, external_text, library_key, valid}]}`
- `GET /v2/strategies/{id}` → `{strategy, versions: [{version, content_hash,
  created_at, origin}], transitions: [...], allowed_transitions: [{to,
  allowed, reason_if_not}]}`
- `GET /v2/strategies/{id}/versions/{v}` → `{spec, spec_text (pretty JSON),
  envelope, validation, summary: {universe_text, signal_text, features_text:
  {name: text}, portfolio_text, rebalance_text, risk_text}, library: metadata or null}`
- `POST /v2/strategies/validate` `{text}` → ValidationResult (200 even when
  invalid; 413-style 400 when > MAX_TEXT_BYTES)
- `POST /v2/strategies` `{text, origin: "user"|"import", file_name?,
  file_sha256?}` → 201 `{id, version}`; invalid → 400 with ValidationResult
  (only valid specs are stored)
- `PUT /v2/strategies/{id}` `{text, base_version}` → 200 `{id, version}`;
  stale base → 409; library → 409 "Library strategies are read-only —
  duplicate them first."
- `POST /v2/strategies/{id}/duplicate` `{name?}` → 201
- `POST /v2/strategies/{id}/transition` `{to, reason}` → 200 / 409 with message
- `POST /v2/strategies/{id}/preview` `{version?, days?}` → preview (7.7);
  data failure → 503 user message.
- `GET /v2/strategies/{id}/versions/{v}/export` → `{file_name, text}` (spec
  with `provenance.exported_from = {strategy_id, version, content_hash}`).
- Request body for text routes may be up to MAX_TEXT_BYTES + 4 KB; aiohttp
  `client_max_size` must allow it (check main.py's app creation — HedgeFund
  file).

### 7.12 WPF (HedgeFund only)

- `HedgeFundWindow`: a slim left navigation rail (design system tokens,
  keyboard accessible, `AutomationProperties.Name`): **Overview** (today's
  dashboard content unchanged), **Library**; later entries (Studio, Loop,
  Validation, Portfolio, Trading, Shadow Account) are NOT shown yet. Toolbar
  with Financial Newsroom, Keys & data, Run now, Kill switch stays pinned.
- `Platform/LibraryView.xaml(.cs)`: tabs **Board** (columns per lifecycle
  state with strategy cards: name, category, origin chip, version, chips
  "external text · unverified" / "AI involvement unknown") and **Alpha
  library** (table: name, family, reference, holding days). Detail pane:
  summary texts, signal expression, parameters table, versions,
  transitions, validation warnings, buttons Edit (user strategies) / Duplicate
  as draft / Move to… (only allowed transitions enabled; disabled ones show
  `reason_if_not` as tooltip; reason prompt) / Preview signal (table of
  symbol, signal, rank, target weight; errors shown verbatim) / Export….
- `Platform/StrategyEditorWindow`: monospace JSON editor (AcceptsReturn,
  AcceptsTab, no word wrap), live validation (debounced 600 ms →
  POST /validate), error list (path, code, message, line/column for parse
  errors; double-click on a parse error moves the caret), operator help pane
  from `/v2/dsl/operators`, Save (new strategy or new version with
  base_version; 409 → reload prompt). New strategy starts from a template.
- Import: "Import…" button → OpenFileDialog `*.json`; refuse > 256 KB before
  reading; read strict UTF-8; confirmation dialog "Imported strategies are
  untrusted: they start as draft, their claims are shown as unverified, and
  nothing in them runs code." → POST origin import with file name only + SHA-256.
- Export: SaveFileDialog, writes `text` from the export route.
- All texts from strategies are shown as plain text. No AI output in stage 2.
- Stage-5 reminder (do not build now): external texts must be delimited as
  untrusted data before they are given to an LLM.

### 7.13 B2 notes (implemented 2026-10-02: features, storage v2, lifecycle, API)

- **Features** (`strategy/features/`): `ExtFeature(name, description, load,
  point_in_time, scope, source, key_for)`, `values_for(symbols, start, end) ->
  {symbol: {date: value}}` (used by `evaluate.load_ext`), registry `get`
  (KeyError), `names`, `catalogue() -> [{name, description, point_in_time,
  scope, source}]`. Timing rule: the value for day d uses data first published
  on or before d − 1 (FRED dates carry no time of day; H.15 yields come after
  the close). A day before any input is known has NO value (never 0).
- `features/fred.py`: sync stdlib client, every request through
  `http_gate.gate_for("fred")` (default 60/min; 429/5xx/timeouts retried),
  key per call via `require_secret("fred-api-key")`, errors redacted with
  `newsroom.keys.redact`; 401/403 or a 400 about `api_key` → "FRED rejected
  the API key …". ALFRED request = `output_type=4` (initial release only) +
  `realtime_start=1776-07-04&realtime_end=9999-12-31`, earliest
  `realtime_start` per date kept; observation windows of 366 days (daily),
  20 y (monthly) to stay below FRED's 2000-vintage limit per JSON request
  (residual risk: if FRED counts vintages over the whole real-time period a
  daily series answers HTTP 400 — surfaced verbatim, not hidden).
- `macro.regime_score` (scope `asset_class`; keys = the macro agent's asset
  classes, `key_for = fund.agents.macro.asset_class`): runs the agent's own
  `evaluate()` per day on a context built from first releases of DGS10,
  T10Y3M, VIXCLS, BAMLH0A0HYM2, SAHMREALTIME, FEDFUNDS, PCEPILFE/PAYEMS/INDPRO
  (`pc1`); regime via `newsroom.macro_derived.regime` on the last 48 known
  monthly points; real policy rate = FEDFUNDS − core PCE YoY on the newest
  common month. History fetched from start − 5 y. Stored in
  `ext_observations`; meta `ext_cover:fred:<series>` = {from, fetched_at}:
  the store is used only when it covers the start AND was fetched after
  `end` (then all releases known by `end` are stored), else FRED is asked again.
- `calendar.event_risk` (scope global): FRED releases CPI 10, Employment
  Situation 50, GDP 53, FOMC Press Release 101 (`release/dates` incl.
  scheduled dates), for every calendar day: CPI / jobs / GDP (BLS/BEA
  calendars published months ahead; reschedulings are announced before the
  new date) → 1 on the release day AND the NYSE trading day before it; FOMC →
  1 ONLY on the release day (non-trading day → next trading day), because
  release 101 also holds unscheduled actions (2020-03-03, 2020-03-15,
  2008-01-22, 2001-09-17 …) and FRED does not mark scheduled meetings — a
  day-before flag would be look-ahead (review fix 2026-10-03); the statement
  is known by that day's close and signals trade at the next open/close.
  Else 0. Fails (`DataSourceUnavailable`) when a
  release has no date on/before `start` or no scheduled date after `end`.
- **Storage v2**: tables as §7.10 + `ext_observations` (PK series_id, date,
  source); triggers make `strategy_versions` insert-only. db API:
  `strategy_create`, `strategy_add_version` (raises `StaleVersion`),
  `strategy_set_state`, `strategy_get`, `strategy_by_library_key`,
  `strategies_list(state, origin, category)`, `strategy_versions_list`,
  `strategy_version_get`, `strategy_transitions_list`,
  `upsert_ext_observations`, `query_ext_observations`. Every write and its
  audit row share one transaction.
- **Lifecycle** (`lifecycle/registry.py`): ids `s-<12 hex>`; stored spec =
  `with_defaults(spec)` without `provenance` (claims → `envelope.claimed`,
  ≤ 16 KB, for every origin); `created_by` user|system; import →
  `ai_generated "unknown"`, `external_text true` (sticky across edits and
  duplicates), `source_file {name (base name, ≤ 120 chars), sha256 + bytes of
  the received text, sha256_claimed (client hash, only when different)}`.
  Library strategies start in `idea`, user/import/duplicate in `draft`;
  library strategies accept no transition (read-only). Saving identical
  content returns `{unchanged: true}` without a new version. Audit actions:
  `strategy_created`, `strategy_version_created`, `strategy_transition`.
  `seed_library()` runs in main.py after `sp_storage.init`; its status
  `{ok, at, message, created, updated, unchanged, errors: [{key, message}]}`
  is returned as `library_status` by GET /v2/strategies.
- **API response shapes** (beyond §7.11): spec errors → 400
  `{error, ok, errors: [...], warnings, facts}`; body/query errors → 400
  `{error, errors: {field: message}}`; unknown id/version → 404 `{error}`;
  409 `{error, current_version?}`; POST /transition → `{strategy,
  allowed_transitions}`; PUT → `{id, version, unchanged}`; GET
  /v2/strategies/{id} `strategy` also has `created_at, state_changed_at,
  description, library` (metadata or null). Preview `days` 1..250 (default
  20, = `preview.MAX_PREVIEW_DAYS`) — route limit now 1..5, default 5 (§9.9). Body limit: `client_max_size = 4 × MAX_TEXT_BYTES + 4 KB` (texts arrive
  JSON-escaped; .NET escapes non-ASCII as \uXXXX); the decoded text is
  checked against MAX_TEXT_BYTES (400 "too large").

### 7.14 B1 notes (implemented 2026-10-02: limits, schema, DSL, validator, evaluator, portfolio, SDK, universe, library, preview)

- **Public API** (for B2/WPF): `limits.parse_spec_text(text)` (`SpecParseError`
  with `code`, `line`, `column`, `as_error()`); `validator.validate(spec, *,
  ext_catalogue=None)`, `validate_text(text, *, ext_catalogue=None)`,
  `parse_and_validate(text) -> (spec|None, ValidationResult)`
  (`.ok/.errors/.warnings/.facts/.to_dict()/.codes()`); `schema.with_defaults`,
  `canonical_json`, `content_hash`, `pretty_json`; `dsl.catalogue()`,
  `dsl.expr_text(node)`, `dsl.FIELDS`; `summary.summary_texts(spec)` (the §7.11
  `summary`); `universe.universes()`, `resolve(universe)`; `alphas.library()` /
  `LIBRARY` = `[{key, spec, metadata: {key, family, reference,
  expected_holding_days, notes}}]`, `alphas.get(key)`, `metadata(key)`, `keys()`;
  `evaluate.evaluate(spec, panel, *, ext_values=None, param_values=None)`;
  `portfolio.target_weights`, `rebalance_dates`, `weights_history`,
  `held_weights`; `sdk.Strategy/Context/SpecStrategy/run`;
  `preview.preview(spec, as_of=None, days=20, *, get_bars=None, clock=None,
  ext_values=None, ext_catalogue=None, configured_source=None,
  time_limit_s=60, monotonic=time.monotonic)`.
- **Extra operators** (beyond §7.4, needed by the library): `exp`, `tanh`
  (technical agent's momentum squashing) and `nan()` (a missing value, to
  exclude a symbol, e.g. dual momentum's absolute filter).
- **Parse codes**: oversize text / depth / nodes → `limit_exceeded`; syntax,
  duplicate keys, NaN/Infinity, non-object root, non-text → `parse_error`
  (+ `line`/`column` for syntax errors). Depth counts containers (root object
  = 1), scanned on the text before `json.loads`. `validate(dict)` applies the
  same depth/node limits.
- **Validator code choices**: a window/shift/half-life ≤ 0 (literal or a
  parameter's `min`) → `look_ahead`; non-integer, too long, below the op's
  minimum (ts_std/ts_rank/ts_zscore/ts_corr/ts_cov/rsi ≥ 2, ts_skew ≥ 3) or not
  constant → `bad_window`; enum mismatch → `out_of_range`; non-finite number or
  wrong JSON type → `bad_type`; parameter names follow the feature-name rule.
  `facts.max_lookback` = history to load: lag-like ops add n, windows n − 1,
  Wilder ops (rsi, atr) 4n, `ewm_mean` ceil(4·halflife), plus `vol_lookback`
  for inverse_vol; window parameters at their `max`.
- **Units/ranges**: `risk.*_pct` in percent (0, 100], `vol_target_annual` a
  fraction (0, 5]; `gross_exposure` (0, 3]; `max_weight` (0, 1]; custom costs
  0..1000 bps. The risk block is stored and summarised but applied only by the
  backtest/runner (stage 3+). Defaults written by `with_defaults`: description
  "", tags [], category other, timeframe 1Day, parameters/features {},
  portfolio {long_only, all, equal, vol_lookback 60, max_weight 1, gross 1},
  rebalance daily, risk all null, execution market/next_open, costs
  paper_broker, preset asset_classes [equity, crypto], param integer false /
  description "". `content_hash` hashes `with_defaults(spec)` minus provenance,
  so a spec and its stored form hash the same.
- **Semantics**: `ts_rank` 0..1 (average ties); `ts_argmax/argmin` = bars
  since the extreme (ties → most recent); `ewm_mean` and Wilder smoothing start
  at the panel's first row and restart after a missing value (causal; a
  backtest and a preview with different start dates can differ slightly in
  the warm-up); `cs_rank` with one value → 0.5. Portfolio rules in the
  `portfolio.py` docstring: threshold is strict, inverse_vol needs a complete
  `vol_lookback` window (else the symbol is not eligible), a zero weight total
  → no position (cash), never a stand-in weighting.
- **Panel**: in a mixed universe a crypto value must be known ≤ the session
  close AND within 24 h before it (with complete data it always is) — an older
  bar is not carried forward; rows end at the last session with an equity bar.
- **External features**: evaluator aligns `values_for` output as of each panel
  date (newest value dated ≤ the row date) and loads from first date − 400 days
  when values are not injected; `ext_values` accepts ndarray / per-symbol /
  global dicts (tests inject fakes).
- **Preview**: per symbol first `get_bars` without freshness (empty → excluded
  and listed in `symbols_missing`), then with `require_fresh_through` (last NYSE
  close ≤ as_of, crypto: as_of) — stale or failing data raises the cache's
  `DataSourceUnavailable`. Extra fields: `history[].rebalance`; warnings may add
  `symbols_missing` and `no_signal`. Invalid spec/days/as_of → `InvalidRequest`;
  > 60 s → `PreviewTimeout(UserFacingError)`.
- **SDK**: `on_bar` returns `None` on non-rebalance bars (keep positions); the
  acceptance test compares `sdk.run(SpecStrategy)` with
  `portfolio.weights_history`.
- **Library** (30 entries): 15 classic + 14 Alpha101 + Council Rules (no
  AI). Stateless approximations are named in each entry's notes (Donchian
  as "55-day high more recent than 20-day low", RSI(2) held while the
  condition holds, dual momentum vs 0 instead of T-bills); the committee's
  documented differences are in `alphas/composite.py`. Tests:
  `tests/test_sp_strategy_{limits,validator,eval,portfolio,preview}.py`,
  `tests/test_sp_alphas.py` (fakes only).
- **Review fixes (2026-10-02)**: rolling windows run in chunks of ≤
  `limits.ROLLING_CHUNK_ELEMENTS` (2 M values) — memory O(dates × symbols);
  `evaluate.estimate_cost` (Σ per-cell op cost — window ops n, ts_corr/cov 2n,
  rsi/atr/ewm and cs ops +10, others 1 — × dates × symbols, features once) is
  checked before every evaluation: > `limits.MAX_EVAL_CELL_OPS` (2e10; every
  library entry fits 500 symbols × 2600 days, worst ≈ 1.2e9) →
  `EvaluationTooExpensive(UserFacingError)` "This strategy is too expensive to
  evaluate (…)"; `facts.max_lookback` > `limits.MAX_LOOKBACK` (5040) →
  `limit_exceeded`; keys/strings with lone surrogates → `parse_error` (text) /
  `bad_type` (dict); library labels: `tsmom_12m` (plain 12-month return, MOP
  2012), `xsmom_12_1` reference adds the Carhart (1997) 12-1 convention,
  low-volatility / SMA 50-200 / risk-on-off notes no longer attribute rules to
  papers that used different ones.
- Review fixes (2026-10-03): `strategy_add_version` reads + inserts inside
  `BEGIN IMMEDIATE` and maps a version-number conflict to `StaleVersion`
  (409, never 503); an unreadable stored record (spec/envelope/validation
  JSON) raises `StorageUnavailable` (503) instead of an empty default that
  would drop the sticky `external_text`/`ai_generated` flags;
  `library_metadata` returns null only for "not a library strategy / key no
  longer in the library", other failures propagate; preview: a
  `ValueError`/`TypeError` from panel/evaluate/portfolio (bad source data
  after validation) becomes `PreviewDataError` (503 `{error, kind}`) instead
  of 400. Suggestion for B1: raise a `UserFacingError` from
  `Panel.from_bars` for malformed/duplicate bars.
