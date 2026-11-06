"""The CI workflow file parses as YAML and installs from the hash-pinned lock with SHA-pinned actions."""
import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / "ci" / "risklab-tests.yml"

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed: the CI workflow parse test is skipped")
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def test_structure(self):
        job = self.doc["jobs"]["test"]
        self.assertEqual(job["strategy"]["matrix"]["python-version"], ["3.10", "3.12", "3.13"])
        steps = job["steps"]
        runs = [s["run"] for s in steps if "run" in s]
        self.assertTrue(all(isinstance(r, str) and r.strip() for r in runs))
        install = next(r for r in runs if "pip install" in r)
        for part in ("--require-hashes", "--only-binary=:all:", "-r requirements-report.lock"):
            self.assertIn(part, install)
        self.assertTrue(any("unittest discover" in r for r in runs))
        self.assertTrue(any("examples/quickstart.py" in r for r in runs))
        self.assertTrue(any("--mode smoke" in r for r in runs))

    def test_actions_pinned_by_sha(self):
        uses = [s["uses"] for s in self.doc["jobs"]["test"]["steps"] if "uses" in s]
        self.assertEqual(len(uses), 2)
        for u in uses:
            self.assertRegex(u, r"^actions/(checkout|setup-python)@[0-9a-f]{40}$")

    def test_every_run_step_is_a_block_scalar(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"^\s*run: (?!\|).+$", text, flags=re.MULTILINE), [])


if __name__ == "__main__":
    unittest.main()
