Vendored chart engine for the Market Research chart page (stage MR3).

Expected content (written by ..\tests\fetch_engine.py, never by hand):
  klinecharts.min.js   KLineChart 10.0.3 UMD build (npm package "klinecharts", Apache-2.0)
  LICENSE              the package's Apache-2.0 licence (NOTICE too if the package ships one)
  MANIFEST             "<file> <version> <source_url> <sha256> <fetched_at>" per file;
                       ChartHost.cs verifies every sha256 before loading the page.

Status 2026-10-16: not yet fetched - the build agents had no registry access (HTTP 403).
Run once on a machine with npm registry access:
  python HedgeFund\MarketResearch\Chart\tests\fetch_engine.py
then run the headless tests against the real engine:
  node HedgeFund\MarketResearch\Chart\tests\run_tests.js --engine real
Until then the chart card shows the "engine_modified" error (no fallback engine).
