# Strategy Platform SPEC — archive: 15-stages6-10-interview

Full text of these sections as of 2026-10-24 (before DOCS-SLIM), kept verbatim: decision history, review rounds, as-built narratives. The CURRENT contract is `../SPEC.md`; section numbers are unchanged. Agent working files of that time are named without a path; open contracts among them are kept in `UFOS.ai/docs/tasks/`.

## 15. Stages 6–10 + central AI budget window — interview decisions (user, 2026-10-17)

Binding decisions from the interview of 2026-10-17 (each stage still gets its
own detailed contract section before it is built):

- **B — central AI budget window (MainWindow):** built FIRST. Lives in the
  MainWindow (user explicitly lifts the HedgeFund-only scope rule for this
  window and its wiring); controls ALL AI budgets of ALL modules (HedgeFund:
  fund/Council, explanations, ideation, loop; plus new daily caps for Global
  Monitoring Assess, LiveStreamAgent and the Newsroom analysis) and shows the
  spend from the shared cost log; provides a **documented interface so new
  modules can register their budgets**. Own contract + scope-guard snapshot
  with an explicit allow-list of the files outside HedgeFund it may change.
- **Stage 6 — discovery loop:** default 6 variants × 4 generations (max
  configurable 8 × 5); start from an existing strategy, an idea in words or
  a confirmed paper; own **loop AI budget** (set in the budget window);
  switch "fully autonomous" (loop also validates its finalists, G1–G8) —
  when off, finalists are proposed for the user's approval; the holdout is
  ALWAYS opened only by the user; every backtest counts as a trial.
- **Stage 7 — portfolio, risk, OMS (paper):** allocator = inverse
  volatility with a correlation penalty; **incubation length chosen per
  strategy before each incubation run, then immutable**; default hard limits
  2 % daily loss (no new orders until next day) and 10 % drawdown from peak
  (kill switch: close all, autopilot off) — adjustable; netting; pre-/post-
  trade checks; OMS state machine; migration of the old paper engine.
- **Stage 8 — brokers:** Alpaca paper (stocks/ETFs) and **Kraken via ccxt
  (demo futures)** for crypto; IBKR/MT5 NOT built, but the broker adapter
  interface stays open for them (TODO item at the end); reconciliation halts
  on mismatch; **live guard built (4 conditions + capital cap) but live
  stays OFF**; stage 12 only on the user's explicit go.
- **Stage 9 — monitoring & Shadow Account:** decay → strategy paused (no
  new orders, positions kept) + user decides; alerts = Windows toasts;
  ~~yearly German tax CSV (Anlage KAP basis, not tax advice)~~ **SUPERSEDED 2026-10-18 by the user: NO tax export
  — a fixed notice points to the brokers' annual tax documents (§19.15)**; Shadow Account
  imports: generic column mapping + templates for Trade Republic, Scalable
  Capital and IBKR Flex; data stays local.
- **Stage 10 — cleanup & hardening:** the Council of AIs is migrated into
  the new platform as a strategy (parity test against the old engine), then
  a SAFE and CAREFUL cleanup of old code/structures (deletion list approved
  by the user; rename to *.stale first); contract of the migration: §22; security & compliance audit of the
  **whole app** (all modules, MainWindow, Shared); operating guide.
- **Process:** B, then stages 6–9 in one go (each: contract → implement →
  independent review → fix → docs; short report after each stage); HALT
  before stage 10 for the user's click-tests and the deletion approval.
