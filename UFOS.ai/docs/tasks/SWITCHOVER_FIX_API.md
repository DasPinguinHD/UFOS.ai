# SWITCHOVER_FIX_API — R2 review fixes M1 / M2 (Agent P, 2026-10-24) — for Agent W (UI) and the lead

The routes below extend SPEC §22.11 / §22.14. Errors are always `{error, code}`, never a stack trace. Every route is
refused in sandbox (409 `sandbox`).

## 1. One operation at a time (M1)
- `POST /v2/council/switchover`, `…/retry`, `…/abort` and `…/revert` share ONE process-wide lock. A second request
  while one runs → **409 `{"code": "switchover_running", "error": "A switch-over operation is already running — wait
  for it."}`**.
- A new `POST /v2/council/switchover` while the state is `pending` → 409 `switchover_blocked` with the blocker
  `{"code": "switchover_pending", "message": "a switch-over attempt is unfinished — retry or abort it"}`.
- DB-level guards (no UI impact): the transaction re-reads the legacy high-water mark inside `BEGIN IMMEDIATE`
  (changed → 409 `concurrent_import`, nothing written); UNIQUE partial indexes on `cl_runs(old_id)` and
  `cl_decisions(old_id)` `WHERE legacy = 1` (schema v26).

## 2. GET /v2/council/switchover — new fields (M2)
```
{ …as before…,
  "state": "none" | "pending" | "done" | "reverted",
  "pending": null | {"tx": "…", "at": "…Z", "actor": "user|system", "previous": "none|reverted",
                     "applied": bool,            // the ONE transaction already committed for this attempt
                     "last_error": null | "text", "error_at": null | "…Z"},
  "actions": {"run": bool, "retry": bool, "abort": bool, "revert": bool}   // what the dialog may offer now
}
```
- `actions.run`: state none / reverted AND no blocker. `actions.retry` / `actions.abort`: state `pending`.
  `actions.revert`: state `done`.
- `blockers` gains `{"code": "council_unavailable", "message": "the Council service could not be built at backend
  start — see backend.log"}` when the service is missing.

## 3. POST /v2/council/switchover/retry `{reason?}` (M2)
- Only while `pending` (else 409 `not_pending`). Re-runs the SAME attempt (same `tx`): stops the old engine if one is
  still registered, applies the transaction (idempotent — skipped when `applied`), then `done`.
- 200 = the same body as a successful `POST /v2/council/switchover` (§4). Failure → 503 `switchover_failed`
  `{error, code, last_error}`; the state stays `pending` with the new `last_error`.

## 4. POST /v2/council/switchover — response (unchanged fields + new)
`200 {"state": "done", "tx", "params_version", "spend_seed_usd", "legacy", "hwm", …,
      "council_task_started": bool, "council_task_error": null | "text"}`.
A failure after `pending` → **503 `switchover_failed`** `{error, code, last_error}`; the state stays `pending` (the
dialog then offers Retry / Abort). Before `pending` nothing changes (409 / 503 as before).

## 5. POST /v2/council/switchover/abort `{reason?}` (M2)
- Only while `pending` (else 409 `not_pending`). Goes back to the old engine:
  - transaction NOT applied → state = `previous` (`none` or `reverted`), nothing else to undo;
  - transaction applied → like a revert: state `reverted`, the old engine's spend counter seeded (§22.2); the `cl_*`
    rows stay (insert-only; a later switch-over imports only newer old ids);
  - always: Council orders OFF, the old engine RE-CREATED with autopilot OFF and started, audit `council_switchover_abort`.
- `200 {"state": "none" | "reverted", "applied": bool, "old_engine_started": bool, "old_engine_error": null | "text"}`.

## 6. POST /v2/council/switchover/revert `{reason?}` — response (minor 2)
`200 {"state": "reverted", "old_spend_seed_usd", "old_engine_started": bool, "old_engine_error": null | "text"}`;
a revert while the state is already `reverted` → `200 {"state": "reverted", "already": true, "old_engine_started":
bool, …}` (no error). 409 `not_switched_over` only for `none` / `pending` (pending → use abort).

## 7. Backend start with a failed recovery (M2)
- `main.py`: `pending` at start → the same attempt is retried once; on failure the state stays `pending` with
  `last_error`, NO engine runs (the old one would refuse), alert kind **`council_switchover_failed`** (critical, toast)
  is raised and `GET /v2/council/state` shows `"switchover": "pending"` + `"switchover_error": "<last_error>"`.
  The app's Council view shows the error with Retry / Abort (§3 / §5).

## 8. Kill (minor 4 / 5)
- `kill_all` treats `pending` like `done` (legacy half "not applicable", council half reported).
- When `/fund/kill` (alias) itself fails, the halves say `{"ok": false, "error": "unknown — the kill request failed"}`
  (never a fake ok).

## Status (Agent P, 2026-10-24): IMPLEMENTED as specified above (SPEC §22.14 "Review fixes").
Notes for W: the new alert kind `council_switchover_failed` (critical, toast) may need an entry wherever the WPF maps
alert kinds; the 503 body of POST …/switchover and …/retry carries `last_error`; abort / revert answer
`old_engine_started` + `old_engine_error` (show the error when started is false).
Addendum (lead, 2026-10-24): blocker `{"code": "legacy_duplicates", "table": "cl_runs" | "cl_decisions", "rows": n,
"message": …}` in the dry run when imported history rows share an old id (database migrated with duplicates); new
alert kind `council_legacy_duplicates` (critical, toast) raised at every backend start while they exist.
