# OS2 — Options Structure computations (unparked 2026-11-06, full scope)

User decisions 2026-11-06: Options Structure is unparked **before** the stage-11 Overview so that its market-state
strip can show real structure data; **full OS2 at once**; the market strip reads **archived data only**, showing the
slot's age; no fresh slot → "not available (reason)" — no live fallback.
Plan: Claude Project `claude/options-structure-plan.md` §4 (OS2), §6, §6b; contract: SP SPEC §21 (OS0 / OS1 as built).
Archive state (2026-11-06, status file): running since ~2026-10-29, 50 runs, 242 530 chain snapshots, 19 755 OI rows,
124 gaps; config still **PROVISIONAL** (quote staleness 5 min, coverage 80 %) because the RTH verification run has not
happened yet.

## Binding rules
- **Nothing here trades** and nothing feeds a strategy yet (OS4 = DSL operators comes later, after Depth & Range).
- **Point-in-time:** every computed value carries `valid_for`, `available_at` (= max of its inputs' `available_at`),
  `computed_at`, `model_version`, `assumptions_hash`, and its source run ids. Readers use only `available_at` ≤ the
  as-of time. OI of day T counts only from T+1 08:00 NY (SP §21.3).
- **Gaps are never filled** (no interpolation across slots, no previous value, no second source). A computation whose
  inputs fail the quality rules is stored as `unavailable` with the reason.
- **PROVISIONAL config:** while `config.provisional` is true, every OS2 output carries `provisional: true` and the UI
  must show it.
- **Frozen pre-registration (SP §21.8) stays untouched:** OS2 computes NO H1 / H2 statistics, no regressions on
  r_first / r_last, no hit rates per regime. A progress counter (sessions archived / 120) is allowed.
- The archive DB is opened **read-only** by OS2; OS2 writes its own tables in a separate DB `data/options_structure.db`
  (insert-only, schema-versioned, WAL) — or in hedgefund.db if that is the established pattern; decide and document.
  Never write into `options_archive.db`.
- Real DBs are never opened over the VM mount by agents; tests use synthetic chains only (no Alpaca data in the
  repo; check_repo_data must pass).
- Scope: `UFOS.ai/HedgeFund/**` (scope label `os2`).

## 1. Inputs and quote quality (`strategy_platform/options_structure/quotes.py`)
- From an archived chain snapshot: bid / ask / last / IV / greeks as delivered (indicative feed), the underlying spot
  (IEX), the contract (strike, expiry, type), OI (point-in-time).
- Quality flags per quote: crossed / locked, zero bid, spread > X % of mid, stale (> quote_max_age), outside the strike
  band; a mid is used only if it is not flagged. Thresholds in the config with the provisional marker.
- Coverage per expiry (share of usable quotes); below `coverage_min_pct` → that expiry is `unavailable`.

## 2. IV and greeks — own computation (`bs.py`)
- Black–Scholes–Merton with a continuous dividend yield q and rate r; implied vol by a bracketed root finder (no
  silent clipping; `unavailable` with the reason if no root, e.g. a price below intrinsic).
- Delta, gamma, vega, theta; per-contract dollar gamma.
- **Rate r:** a FRED series of 3-month T-bill (`DTB3`) or SOFR — add it to the archive's FRED fetch only if that is
  the cleanest point-in-time path; otherwise read the newest FRED value with `available_at` ≤ as-of from the existing
  Newsroom FRED store. Decide, document, mark as ASSUMPTION; never a hard-coded rate. **Dividend yield q:** a
  documented per-symbol assumption parameter (e.g. trailing 12-month yield entered in the settings), visible in every
  output's assumptions; implied-forward alternative (put-call parity) as a cross-check where both sides are quoted.
- Compare our IV with the vendor IV in the snapshot (difference statistics in the data-quality view; no replacement).

## 3. GEX (`gex.py`)
- GEX per strike and total = Σ OI × gamma × contract multiplier × spot² × 0.01 (state the convention: per 1 % move,
  in USD); by expiry bucket (0–7 d, 8–30 d, 31–45 d, all).
- **2–3 dealer-positioning assumptions in parallel** (visible parameter): (A) baseline "dealers long calls / short
  puts" (calls +, puts −; the pre-registration convention); (B) "dealers short all options" (all −); (C) a
  moneyness-split variant (e.g. dealers short OTM puts and OTM calls, long ITM) — document the definitions.
- Gamma flip: the spot level where total GEX(S) crosses zero, re-computed for hypothetical spots ±5 % (V2 curve);
  none in range → `null` with the reason. Call wall / put wall: strikes with the largest call / put GEX.
- "Variants agree" = sign agreement of total GEX across A–C.

## 4. Skew, expected move, term structure, VRP (`surface.py`)
- Skew: 25-delta risk reversal and 25-delta butterfly per expiry (interpolated in delta within one expiry only; no
  extrapolation); a smile curve (IV vs log-moneyness) per expiry for V4; percentile of today's RR vs the archived
  history (only as long as the archive exists; state n).
- Expected move: (a) ATM straddle mid for the nearest expiry ≥ today, (b) IV_ATM · √(t) · spot; both, with their
  difference; for today and for the next expiry.
- ATM term structure: ATM IV per expiry; FRED VIX (VIXCLS) vs VIX3M (VXVCLS) (and VXN for QQQ) → contango /
  backwardation label, slope.
- Variance risk premium: IV_ATM(≈30 d) − realised volatility (close-to-close, 21 sessions, from the platform bar
  cache) — state the window and the definition.

## 5. Calendar (`calendar.py`)
- Monthly options expiration (3rd Friday; holiday rule), quarterly expirations, VIX expiration (Wednesday 30 days before
  the next month's SPX expiry; holiday rule), plus FOMC / CPI from the Newsroom economic calendar where available.
  Features: days to next expiration / event. Tested against known dates.

## 6. Regime service (`regime.py`)
- Per symbol and slot: regime = sign of total GEX under A (+ the agreement across A–C), distance of spot to the flip and
  walls in %, position inside today's expected move, VIX structure label, VRP sign, next event.
- Computed **after each archive slot** (a background job in the backend that reads newly archived runs; idempotent,
  resumable; no network calls of its own) and stored point-in-time.
- No consumers change their behaviour in OS2 (allocator / risk / explain integrations are OS4).

## 7. API (token-protected, read-only)
- `GET /v2/options-structure/state?symbol=SPY&as_of=` — the latest regime snapshot with age, provisional flag,
  assumptions, the reasons for any unavailable part (for the Overview market strip).
- `GET /v2/options-structure/gex?symbol=&slot=` (per strike, per variant, per bucket, flip curve), `…/surface?symbol=
  &slot=` (smile, term structure, RR / BF, expected move), `…/history?symbol=&field=&days=` (time series of regime
  fields, for later charts), `…/calendar`, `…/quality?symbol=&days=` (coverage per slot, gaps, IV-vs-vendor stats,
  OI staleness), `…/progress` (sessions archived towards 120; no H1/H2 results).
- Errors: 400 with the reason; 404 for an unknown slot; never a stand-in value.

## 8. Tests (synthetic, independent)
- BSM prices and greeks against closed forms / put-call parity / finite differences; IV round trip; no-root cases.
- GEX on a constructed chain with a known answer; the flip on a constructed two-strike case solved by hand; the
  variant definitions.
- 25Δ RR / BF and the expected-move formulas on a constructed smile; the term-structure label.
- Point-in-time: OI before T+1 08:00 is ignored; outputs carry max(available_at); gap → unavailable.
- Calendar against a list of known 2025/2026 expiration dates (from the published OCC/CBOE rule, hand-computed).
- Regime job idempotence and resume; read-only access to the archive (a test that the archive DB is opened
  `mode=ro`).
- API shapes; provisional flag propagation.

## 9. Delivery
- SPEC §21 extended (OS2 as built) + CODEMAP; README of the archive / structure parts; scope label `os2`
  (check → read ok → close).
- Tests on the VM in chunks + the full HedgeFund suite in the container; check_repo_data.
- A short dry run against a **copy** of the user's `options_archive.db` (copied to a local temp dir, sha256 before =
  after) to report real coverage / unavailability rates — results reported to the lead only, never committed.
- Then an independent review; a PDF report for the user's folder (via the lead).
