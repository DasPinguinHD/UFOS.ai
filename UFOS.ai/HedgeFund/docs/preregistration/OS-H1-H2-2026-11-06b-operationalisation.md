# Operationalisation note OS-H1/H2 — OI window and evaluability (2026-11-06 b)

- **Status:** dated note, 2026-11-06 (second note of that day) — written before any H1 / H2 session was evaluated (no
  running results exist).
- **Decision:** user, 2026-11-06 (relayed by the lead after the follow-up review).
- **Amends:** `OS-H1-H2-2026-11-06-operationalisation.md` (sha256
  `2f09ddbcd02f03f888cfc4daa899dd3b2bbe689a1135e4f3f7c5fe76bc3754bc`), points 3 and 5. That note and the frozen
  pre-registration `OS-H1-H2-2026-10-23.md` (sha256
  `95d3ac66736ad358df23b4628f0c32425f4b0b4be37138f7b52a813afc3b8195`) are NOT edited. The hypotheses are unchanged.
  The sha256 of this note is recorded in `HedgeFund/backend/strategy_platform/SPEC.md` §21.3 / §21.8.

## Amendments

1. **Open interest window** (replaces point 3 of the 2026-11-06 note): per contract the newest value with
   `valid_for` ≤ the previous session and `available_at` ≤ the decision time is used ONLY when its `valid_for` is the
   previous session (D−1) or the one before (D−2) — the same window the archive treats as fresh. Older open interest
   (D−3 or earlier) is treated like missing open interest: the contract contributes 0 (no OI reported) and is
   counted as "no fresh OI" with the reason. The per-contract age distribution of the open interest is reported.
2. **Evaluability** (adds to point 5 of the 2026-11-06 note): a decision time is NOT evaluable when the expiries
   excluded from the total GEX (quote-coverage rule) carry more than **15 %** of the total open interest (the
   excluded-OI share: open interest of the excluded expiries' contracts over the open interest of all archived
   expiries, both with fresh open interest only). The regime is then unavailable with the reason "GEX partial:
   excluded OI x % > 15 %", and the session is not evaluated for that decision. A decision time without any open
   interest reported is not evaluable either.
