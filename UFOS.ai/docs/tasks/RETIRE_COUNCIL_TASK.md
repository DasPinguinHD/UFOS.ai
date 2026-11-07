# Retire the Council of AIs and the old engine (reversible)

User decision 2026-11-06 (after the §17.9 migration to `paper-council` at 19:14 UTC and the Council switch-over
`done` at 19:15 UTC): **retire BOTH** the old engine (`backend/fund/`, `/fund/*`) and the platform Council strategy
(`backend/strategy_platform/council/`, `/v2/council/*`). Nothing is deleted; it must be reversible.
Inventory (read-only, 2026-11-06) is the basis of this contract; verify every file:line before relying on it.

Why now: since the switch-over the Council service is active and runs a full AI cycle (sentiment + judge) every
interval during the US session although its orders are OFF (`council/service.py` `run_forever`) — real AI spend
for nothing.

Scope label: `retire1`. Scope: `UFOS.ai/HedgeFund/**`, `UFOS.ai/docs/**`.

## 0. Binding rules
- **No balance, ledger, position or order history is touched.** `paper-council` stays a normal platform paper
  account (manual orders into the source `council-of-ais`, platform kill / halt / resume / marks unchanged).
- **`council.switchover` stays `done`.** The heartbeat / dead-man (`execution/control.py`), `kill_all`, `/fund/*`,
  `oms`, `portfolio/summary.py` and WPF `_councilReplaced` branch on it — do not reuse it as the retirement marker.
- **New marker:** hedgefund.db meta key `retired.council_and_old_engine` =
  `{retired: bool, at, actor, reason, audit_id}`. Unreadable marker at start-up → start neither the Council task nor
  the old engine and raise a critical alert (fail closed; no fallback).
- **Code stays** (no `*.stale` renames of live modules — `fund/store.py` is needed by `migration.legacy_state`,
  `main.py` imports `fund/*`, tests import the packages). SP §22.8 R4 / R5 (rename / delete `fund/`) are **postponed**
  until the user declares the retirement final — reword that SPEC text accordingly.
- The stage-10 import guard (`tests/test_s10_no_new_fund_imports.py`) must stay green without weakening it.
- No new fallbacks; missing / retired things read "retired" or "not available (reason)" honestly.
- Tests: `unittest`, temp DBs, no network; VM chunks ≤ 170 s; existing behaviour without a marker stays covered.
- WPF: static checks only (the user builds).

## 1. Backend
1. **Retire / un-retire routes** (`api/council_routes.py`, token-protected, refused in sandbox):
   - `POST /v2/council/retire {reason}` — audit row first (refuse if it cannot be written), then
     `control.council_orders_off("retired")` (cancels working Council orders), `CouncilService.stop()`, write the
     marker. Idempotent.
   - `POST /v2/council/unretire` — audit row first, clear the marker, `CouncilService.start()`; Council orders stay
     **OFF** (the user turns them on). The old engine comes back only via the existing revert, after un-retiring.
2. **Start-up** (`main.py` where the `council-cycle` task is started): marker set → keep the service object built
   but do not start its task. Restart re-applies the marker.
3. **Routes while retired:**
   - Council write routes (`run`, `params PUT`, `broker-params PUT`, `orders PUT`, `switchover POST`, `retry`,
     `abort`, `revert`) → **410** `{code: "retired", error: "Retired by the user on <date> — un-retire first"}`.
   - Council read routes (`state`, `runs`, `runs/{id}`, `params GET`, `switchover GET`) stay 200 and carry
     `retired: true` (+ `retired_at`, `reason`).
   - `/fund/*`: existing 410s get `code: "retired"`; **`/fund/kill` and `/fund/resume` keep forwarding to the
     platform** (a kill switch never answers 410).
   - `oms._council_blocker`: also refuse Council-origin orders while retired (second safety net).
4. **AI budgets** (user decision): the Council / fund entry ("Fund AI — sentiment + judge (Council of AIs)") is
   **hidden** from `GET /ai/budgets` while retired; `PUT` for it → 409 `retired`. Spend history stays in the DB.
5. **History pruning** (user decision): the existing newsroom.db old-engine pruning (runs 90 d without
   recommendation/order, events 30 d) **keeps running** — no change.
6. **Readers report "retired" instead of errors:** `portfolio/summary.py` council / old-engine parts, `kill_all`'s
   council half (stays the `done` branch), Overview `src_old_engine` (today it already raises "not running" → show
   `"retired"` as the source state, not an error) and `src_council` (keeps listing past runs).
7. Tests: marker round trip; retire cancels working Council orders and stops the task; start-up with marker / with
   unreadable marker; each write route 410 / read route 200 + `retired`; `/fund/kill` still forwards; AI budget
   entry hidden + PUT 409; oms blocker; un-retire restarts with orders OFF; no balance change (assert ledger rows and
   cash identical before / after).

## 2. WPF
1. Trading sub-tab "Legacy: Council & old engine" → **"Retired: Council & old engine"**: a read-only card (retire
   date + reason, the Council's recent runs list as history, a short note "paper-council is now managed by hand —
   no managing strategy", button **"Un-retire…"** with one confirmation dialog). While NOT retired (e.g. after an
   un-retire) the existing CouncilView + Legacy sections show as today, plus a **"Retire…"** button (one confirmation
   dialog with the reason field). The old-engine Legacy* sections stay hidden after the switch-over as today.
2. Keep the CouncilView start load (`HedgeFundWindow.Navigation.cs`) so `_councilReplaced` and
   `FundNotifications.SetCouncilSwitchedOver(true)` still get their value — the 5 s `/fund/state` poll and the
   `/fund/events` poll must NOT restart.
3. Toolbar: hide **▶ Run now** (it calls the old engine's `/fund/run`), its hint text and "Resume engine" while
   retired; keep the **Kill switch** (platform kill) — update its tooltip, confirmation text and result line
   ("platform + paper-council"; no old-engine / Council wording while retired).
4. Overview: no Council tile; any Council source tile reads "retired"; the activity feed shows old-engine / Council
   sources as "retired", not as errors; attention strip unaffected.
5. MainWindow module tile (`Tiles/HedgeFundTileSummary.cs`): "Council retired" instead of ON/OFF.
6. Monitoring alert "Council requested" (if any) opens the Retired card.

## 3. Docs
SPEC §22 (retirement as built, marker, routes, R4/R5 postponed), CODEMAP, backend README, `UFOS.ai/docs/todo.md`
(retirement done after the user's build; R4/R5 parked with the reason).

## 4. Acceptance
Backend tests green (touched modules on the VM, full suite once in the container via the lead); static C# checks
green; independent review; the user retires via the app button (the lead does not retire on the user's behalf) and
click-tests; no balance change.
