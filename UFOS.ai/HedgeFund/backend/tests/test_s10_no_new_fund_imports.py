"""Stage 10 S10-C guard (SP §22.7): outside the old engine, nothing imports ``fund`` any more.

AST scan of every backend ``*.py`` (multi-line imports included; ``*.stale``, ``.venv``, ``data`` and caches skipped).
Exempt until their removal step: ``fund/`` itself, the old-engine tests and kits, ``tests/test_sandbox_profile.py``,
``tests/test_s10_council_parity.py`` +
``tests/council_parity_testkit.py`` + ``…_scenarios.py`` (the parity gate drives the old engine; golden fixture
before R4),
``execution/migration.py`` (old-store reader, until R4), and ``main.py`` +
``ai_budgets.py`` (until R2), and ``LEGACY_TEST_IMPORTS`` (named platform tests × named old modules, until R4).
``NOT_YET_MOVED`` lists the shared modules whose move (M3–M6) is still to come — it
shrinks with every step and is empty after M6; a moved module imported through its old path fails the test."""
import ast
import os
import unittest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".venv", "data", "__pycache__", "bin", "obj", "node_modules"}
EXEMPT_FILES = {"tests/test_sandbox_profile.py", "tests/test_s10_council_parity.py", "tests/fund_testkit.py",
                "tests/council_parity_testkit.py", "tests/test_s10_council_parity_scenarios.py",
                "tests/crypto_testkit.py", "strategy_platform/execution/migration.py", "main.py", "ai_budgets.py",
                "tests/test_s10_no_new_fund_imports.py"}
EXEMPT_PREFIXES = ("fund/", "tests/test_fund_")
# platform tests that compare with / drive the OLD engine (parity, regressions) — exactly these modules, until R4
LEGACY_TEST_IMPORTS = {
    "tests/test_ai_budgets.py": {"fund.engine"}, "tests/test_mr_integrations.py": {"fund.store"},
    "tests/test_sp_backtest_parity.py": {"fund.store"}, "tests/test_sp_clicktest_1022.py": {"fund.engine"},
    "tests/test_sp_risk_controls.py": {"fund.store", "fund.engine"},
    "tests/test_sp_strategy_eval.py": {"fund.indicators"},
    "tests/test_sp_trading_parity.py": {"fund.store", "fund.engine"},
}
MOVED = {"fund.types": "strategy_platform.core.types", "fund.params": "strategy_platform.core.params",
         "fund.llm": "strategy_platform.core.llm", "fund.market": "strategy_platform.data.market_feed",
         "fund.broker": "strategy_platform.execution.paper_broker",
         "fund.agents.macro": "strategy_platform.strategy.features.macro_agent"}
NOT_YET_MOVED: set = set()      # empty after M6 — every shared module moved


def _fund_imports(tree):
    """Fully-qualified fund modules imported by a module (``from fund import x`` → ``fund.x``)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, node.lineno) for a in node.names if a.name == "fund" or a.name.startswith("fund.")]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and \
                (node.module == "fund" or node.module.startswith("fund.")):
            if node.module == "fund":
                out += [(f"fund.{a.name}", node.lineno) for a in node.names]
            else:
                out.append((node.module, node.lineno))
    return out


def _files():
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                yield os.path.relpath(path, BACKEND).replace(os.sep, "/"), path


class NoNewFundImportsTest(unittest.TestCase):
    def test_scan(self):
        bad, seen = [], 0
        for rel, path in _files():
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), rel)
            seen += 1
            exempt = rel in EXEMPT_FILES or rel.startswith(EXEMPT_PREFIXES)
            for mod, line in _fund_imports(tree):
                if mod in MOVED and not rel.startswith("fund/"):
                    bad.append(f"{rel}:{line} imports the moved {mod} — use {MOVED[mod]}")
                elif not exempt and mod not in NOT_YET_MOVED and mod not in LEGACY_TEST_IMPORTS.get(rel, ()):
                    bad.append(f"{rel}:{line} imports {mod} (old engine)")
        self.assertGreater(seen, 100)
        self.assertEqual(bad, [])

    def test_shims_are_the_same_module(self):
        import importlib
        for old, new in MOVED.items():
            self.assertIs(importlib.import_module(old), importlib.import_module(new), old)

    def test_scanner_sees_multiline_and_from_imports(self):
        src = "from fund import (\n    llm,\n    params as P)\nimport fund.broker\nfrom fund.types import Bar\n"
        self.assertEqual(sorted(m for m, _l in _fund_imports(ast.parse(src))),
                         ["fund.broker", "fund.llm", "fund.params", "fund.types"])


if __name__ == "__main__":
    unittest.main()
