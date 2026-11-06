# Operationalisation note OS-H1/H2 — "total GEX" (2026-11-06)

- **Status:** dated note, 2026-11-06 — written before any H1 / H2 session was evaluated (no running results exist).
- **Decision:** user, 2026-11-06 (relayed by the lead).
- **Relation to the frozen pre-registration:** `OS-H1-H2-2026-10-23.md` (sha256
  `95d3ac66736ad358df23b4628f0c32425f4b0b4be37138f7b52a813afc3b8195`) is NOT edited. Its hypotheses are unchanged.
  This note only fixes how its term "sign of total GEX" is measured from the archived data. The sha256 of this note is
  recorded in `HedgeFund/backend/strategy_platform/SPEC.md` §21.8 / §21.12.

## Operational definition

1. **Total GEX** at a decision time = the sum of GEX over ALL archived expiries (0 … 45 calendar days) whose chain
   snapshot passes the quote-coverage rule (share of in-band contracts with a usable quote and an own implied
   volatility ≥ the archive's coverage minimum). Expiries that fail the rule are EXCLUDED from the sum and listed
   with their quote coverage; the excluded share (gamma notional where computable, otherwise the excluded open
   interest) is reported with every value.
2. **GEX of a contract** = OI × gamma × contract multiplier × spot² × 0.01 (USD per 1 % move), with the platform's own
   Black–Scholes–Merton gamma (rate: FRED DTB3; dividend yield implied per expiry from put-call parity).
3. **Open interest**: per contract the newest value with `valid_for` ≤ the previous session and `available_at` ≤ the
   decision time (in practice the session before the previous one). A contract without reported open interest
   contributes 0.
4. **Sign convention** (unchanged): calls +, puts − (variant A); the robustness rule "a signal counts only if it holds
   under all dealer variants" uses the agreement of the sign of this total under variants A, B and C.
5. A decision time with no expiry passing the quote-coverage rule has no total GEX (the session is not evaluated for
   that decision).
