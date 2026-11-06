# RISKLAB v0.6 — presentation for applications (README, landing page, quickstart, lock, CI)

Goal: a recruiter or a hiring manager in bank risk management understands in **60 seconds** what the Risk Lab is,
what it found and that it is reproducible. Builds on v0.5 + the review fixes. All rules stay in force (English,
synthetic data only, no private links, no PDFs in the repo, `check_repo_data.py`, no git).

## 1. RiskLab/README.md — restructure (content stays accurate; numbers from studies/results only)
1. **Top (first screen):** one-sentence pitch ("A bank-style market-risk lab: VaR / ES models, regulatory
   backtests and a model-validation report — with the tests' own size and power measured on synthetic data with
   known truth."), then 4–6 **key findings** as bullets with numbers ± se, each linked to its study summary. Then
   one gallery row of 2–3 PNGs (estimation-window curve, rejection rates, survivorship).
2. **Reproduce in 3 commands** (venv / install / run the tests + one smoke study), verified in a fresh container
   on Linux. Add the Windows PowerShell equivalent, verified on the PC VM where possible; otherwise say it is
   unverified.
3. **Architecture:** a Mermaid diagram (GitHub renders it): data generators → models → walk-forward engine →
   backtests → studies → report pipeline; plus a short module map.
4. **Studies overview:** a table (question, design, headline result, runtime, link to the summary).
5. **Validation & overfitting reports:** how to build them (`report/build_report.py --doc … --publish-to <dir>`);
   no PDF links.
6. **Quality:** test count, independent reviews per version (no names, no links), reproducibility guarantees exactly
   as proven (within one numpy version; code fingerprints).
7. **Limitations** and **roadmap** (IRRBB next).
- Keep the detailed conventions in a separate `docs/conventions.md` inside RiskLab, linked from the README, so that
  the README stays short.

## 2. Repository landing page (root `README.md`)
- Add a short, prominent "Risk Lab" section near the top (3–5 lines + a link to `UFOS.ai/RiskLab/README.md`).
  Do not rewrite or remove the existing content, apart from moving it below. Keep the "Data sources & licensing"
  section intact.

## 3. Quickstart example
- `RiskLab/examples/quickstart.py` (≤ 40 lines): generate GARCH-t data, run walk-forward with NormalModel vs
  GarchTModel, print Kupiec / cc / Z2 / traffic light for both. Runs in < 30 s. A test runs it in smoke form. The
  README shows its output.

## 4. Dependencies: lock + CI
- Generate hash-pinned lock files for the core and for the `report` extra (`requirements.lock`,
  `requirements-report.lock`) with `pip-compile --generate-hashes` (or an equivalent), from requirements.in. They
  must install on Python 3.10–3.13 on Linux and Windows: check the wheel availability, and document the generator
  command and its version. If cross-platform hashing is not possible in one file, say so and choose a documented
  approach. No silent fallback.
- CI file `RiskLab/ci/risklab-tests.yml`: install with `--require-hashes` from the locks; matrix Python 3.10 +
  3.12 (+ 3.13 if the locks allow); run the tests; run the smoke studies. Pin the actions by commit SHA if the
  container can resolve them (`gh api` / `git ls-remote`); otherwise keep the tags and leave the open point.
  The user copies the file into `.github/workflows/` (protected).
- README "open points": update.

## 5. Overview PDF (for the user's report folder, NOT the repo)
- `report/overview.tex` + `build_report.py --doc overview`: 3–4 pages, "Risk Lab — project overview": pitch,
  architecture figure, key results table, reproduce, limitations. Build it into `out/report/` in the container and
  report the path; the lead delivers it.

## 6. Delivery
- Version 0.6.0. Scope label `risklab10` (exact paths incl. the root README).
- Tests (VM + container), check_repo_data; check → read ok → close (never chained).
- Then an independent review focused on accuracy: every number in the README matches studies/results; no claim
  beyond the evidence.
