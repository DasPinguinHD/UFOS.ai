# RISKLAB v0.9 — reverse DCF and sensitivity analysis

Showcase module (user plan 2026-11-06). Methods are textbook (reverse DCF, sensitivity tables, tornado / Sobol);
NO code from third-party repositories (e.g. the "investor-skills" repo is all-rights-reserved). No market or
company data in the repo: inputs are user-provided at runtime or synthetic examples clearly labelled as such.

## 1. DCF core (`risklab.valuation.dcf`)
- Free-cash-flow DCF with an explicit forecast period (growth path: constant, or fading linearly to terminal) and
  a Gordon terminal value; enterprise → equity value bridge (net debt, other claims as inputs); per-share value.
- Input validation (discount rate > terminal growth etc.) with explicit errors.

## 2. Reverse DCF (`risklab.valuation.reverse`)
- Solve for the implied constant growth (or implied fade start) that equates the DCF value to a given price
  (root finding with bracketing; error if no root in the bracket); also the implied discount rate for a given
  growth.
- Base-rate context: compare the implied growth with the distribution of 10-year growth from the survivorship
  study's synthetic universe — both the survivors-only and the full-cohort view (link the v0.4 survivorship
  decomposition). Clearly labelled synthetic.

## 3. Sensitivity analysis (`risklab.sensitivity`)
- Generic one-way sensitivity (tornado), two-way tables (e.g. discount rate × terminal growth), and global
  variance-based sensitivity (Sobol first / total order via Saltelli sampling, implemented from the papers; MC error
  reported) — usable for the DCF and for the other Risk Lab models (e.g. the ΔEVE of v0.7 vs NMD assumptions, the
  ECL of v0.8 vs scenario weights). Show at least one cross-module use.

## 4. Study + report
- `studies/valuation_sensitivity.py`: for a labelled synthetic company: implied growth vs price; tornado; Sobol
  indices; how often the implied growth is achieved under the synthetic base rates (survivors vs full cohort).
- Figures / tables / findings; `report/valuation.tex` (5–8 pages, `\usertext`) + `--doc valuation`; PDF into
  out/report.

## 5. Tests (independent)
- The DCF of a constant-growth perpetuity equals the closed form; reverse DCF round trip (value → implied growth →
  value); bracketing errors; Sobol indices on the Ishigami function against the analytic values (tolerance from MC
  error); tornado ordering on a linear model.

## 6. Delivery
- Version 0.9.0; README; label `risklab13`; tests (VM + container); check → ok → close; independent review.
- After v0.9 the Risk Lab block ends; the standard UFOS plan resumes with stage 11.
