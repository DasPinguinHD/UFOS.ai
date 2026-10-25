# GET /v2/portfolio/summary — Hedge Fund tile summary (Agent P, built 2026-10-25)

For the MainWindow Hedge Fund tile (`TILES_API.md` §10.4). SPEC: `strategy_platform/SPEC.md` §17.15.
Code: `strategy_platform/portfolio/summary.py`, route in `api/trading_routes.py`; tests
`tests/test_sp_portfolio_summary.py`.

## Rules
- **Stored values only.** Reads hedgefund.db (account rows, `pa_equity` snapshots, `pa_positions`, `pa_meta`,
  `oms_orders`, `meta`) and, before the switch-over, the registered old engine's own stored flags. It builds no
  account / broker object: no broker call, no market data, no quote, no valuation. Cheap enough for a 30 s poll.
- **Does NOT touch the client heartbeat** (a tile must not keep the dead-man switch alive).
- Behind `api_auth` (token + Host check). No query parameters (any → 400 `{error: "invalid input", errors}`).
- A value that is not stored is `null` WITH a `reason` — never a stand-in.
- Amounts stay in the account's currency (`binance-demo` = USDT); totals are per currency, never mixed.
- The payload carries `"mode": "normal" | "sandbox"` (+ the `sandbox` block in sandbox mode), like `/v2/portfolio/state`.
  The tile shows sandbox figures with a SANDBOX chip and never stores them.

## Payload (200)
```json
{"schema": 1, "generated_at": "2026-10-25T14:00:00+00:00", "paper_only": true,
 "notice": "Automated trading on your own paper account under the limits you set. Paper only — no real money.",
 "stored_values_only": true, "mode": "normal",
 "accounts": [
   {"id": "paper-main", "label": "Platform paper account", "broker": "internal_paper", "paper": true,
    "currency": "USD", "equity": 101500.0, "as_of": "2026-10-25T13:55:00+00:00",
    "day_pnl": 1500.0, "day_pnl_pct": 1.5,
    "day_reference": {"ts": "2026-10-24T20:00:00+00:00", "equity": 100000.0},
    "day_starts_at": "2026-10-25T04:00:00+00:00", "reason": null,
    "positions_count": 3, "working_orders": 0,
    "halt": {"active": false, "kind": null, "reason": null, "at": null}, "kill_pending": false,
    "spark": [{"ts": "…", "equity": 100000.0}]}],
 "totals": {"USD": {"accounts": 2, "equity": 201500.0, "day_pnl": 1700.0, "missing": []}},
 "positions_count": 5,
 "autotrade": {"enabled": false, "changed_at": "…|null", "changed_by": "user|engine|system|null"},
 "halts": {"any": false, "accounts": []},
 "kill": {"active": false, "accounts": [], "pending": []},
 "council": {"switchover": "none|pending|done|reverted|null", "error": null, "orders_enabled": false,
             "label": "not validated — AI strategy"},
 "old_engine": {"applicable": true, "halted": false, "autopilot": false, "reason": null}}
```

## Field definitions
- `accounts[]` = active accounts (internal + connected broker accounts; retired / sandbox-blocked brokers are not
  listed), in `accounts.rows()` order.
- `equity` / `as_of` = the newest stored equity snapshot (written by the runtime every 5–30 min). None → `equity:
  null`, `reason` "no equity snapshot stored yet — …".
- `day_pnl` = newest snapshot − the last snapshot before `day_starts_at` (00:00 New York; 00:00 UTC for the crypto-only
  `binance-demo`); `day_pnl_pct` relative to that reference. No reference → null + `reason` "no equity snapshot before …
  — day P&L not available". (This is snapshot-based; the Portfolio view's live `day_pnl` may differ slightly.)
- `spark` = the newest ≤ 30 stored snapshots, oldest first (for an optional sparkline).
- `halt` = the account's stored halt (`kill`, `drawdown`, `reconcile` for broker accounts …); `kill_pending` = a broker
  kill still flattening.
- `totals.<currency>.equity` / `day_pnl` = sums over that currency's accounts, null when any of them lacks the value
  (`missing` lists them).
- `halts.any` / `kill.active` = any account halted / halted by a kill; `kill.pending` = broker kills still running.
- `council.switchover` null + `error` when the state is unreadable. `old_engine.applicable` false after the switch-over;
  `halted` / `autopilot` null + `reason` when the old engine is not running or unreadable.

## Tile mapping (W)
Equity + day P&L of the USD total (or per account), positions count, auto trading on/off, halts / kill chip, Council
orders on/off; sparkline from `spark` of paper-main if wanted. A backend that is off → stored tile figures "backend off
— as of HH:MM" (tile rule), never this route.
