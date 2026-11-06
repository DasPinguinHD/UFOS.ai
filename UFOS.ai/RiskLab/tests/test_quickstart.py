"""The quickstart example runs (smoke form: fewer forecast days) and prints all four tests for both models."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quickstart.py"


class QuickstartTests(unittest.TestCase):
    def test_runs_and_prints_the_four_tests(self):
        env = dict(os.environ, RISKLAB_QUICKSTART_DAYS="300")
        res = subprocess.run([sys.executable, str(EXAMPLE)], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("300 forecast days", out)
        for model in ("static normal:", "GARCH(1,1)-t:"):
            self.assertIn(model, out)
        for test in ("kupiec_pof", "christoffersen_cc", "acerbi_szekely_z2", "traffic_light"):
            self.assertEqual(out.count(test), 2, test)

    def test_short_enough(self):
        self.assertLessEqual(len(EXAMPLE.read_text(encoding="utf-8").splitlines()), 40)


if __name__ == "__main__":
    unittest.main()
