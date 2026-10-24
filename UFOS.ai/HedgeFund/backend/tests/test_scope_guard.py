"""HedgeFund/tools/scope_guard.py — approved allow-lists (task B, 2026-10-17; review M1 + L3):
segment-local wildcards, refused broad entries, embedded lists applied to older snapshots, amend,
and ``close`` (an allow-listed file is skipped only while its hash equals the recorded one).
Works on a temporary fake repo tree (outside the mount). Stdlib only.

Run from HedgeFund/backend:  python -m unittest tests.test_scope_guard -v
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
sys.path.insert(0, str(TOOLS))
import scope_guard as sg  # noqa: E402

sys.path.remove(str(TOOLS))


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scope-guard-"))
        self.root = self.tmp / "repo"
        self.module = self.root / "UFOS.ai" / "HedgeFund"
        (self.module / "tools").mkdir(parents=True)
        self._saved = (sg.MODULE_DIR, sg.REPO_ROOT, sg.OUT_DIR)
        sg.MODULE_DIR, sg.REPO_ROOT = self.module, self.root
        sg.OUT_DIR = self.module / "backend" / "data" / "scope-guard"
        for rel in ("UFOS.ai/MainWindow.xaml", "UFOS.ai/Shared/AiContent.cs", "UFOS.ai/Other.cs", "CLAUDE.md"):
            self.write(rel, "v1")
        self.allow = self.module / "tools" / "scope-allow-x.json"

    def tearDown(self):
        sg.MODULE_DIR, sg.REPO_ROOT, sg.OUT_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def run_cmd(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sg.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def allow_list(self, files):
        self.allow.write_text(json.dumps({"label": "x", "files": files}), encoding="utf-8")
        return str(self.allow)

    def test_entry_rules(self):
        for bad in ("*", "UFOS.ai/*", "UFOS.ai/*.cs", "UFOS.ai/Sh*/AiBudget.cs", "UFOS.ai/Ai?.cs", "../x.cs",
                    "/abs/x.cs", "UFOS.ai//x.cs", "UFOS.ai/[ab].cs", "Ai*.cs"):
            self.assertIsNotNone(sg._entry_problem(bad), bad)
        for good in ("CLAUDE.md", "UFOS.ai/Shared/AiBudget*.cs", "UFOS.ai/X/tests/test_ai_caps*.py"):
            self.assertIsNone(sg._entry_problem(good), good)
        self.assertTrue(sg._match("UFOS.ai/Shared/AiBudgetModels.cs", "UFOS.ai/Shared/AiBudget*.cs"))
        self.assertFalse(sg._match("UFOS.ai/Shared/AiBudgetX/evil.cs", "UFOS.ai/Shared/AiBudget*.cs"))
        self.assertFalse(sg._match("UFOS.ai/Shared/Sub/AiBudgetA.cs", "UFOS.ai/Shared/AiBudget*.cs"))
        code, out = self.run_cmd("snapshot", "--label", "b", "--allow-list", self.allow_list(["UFOS.ai/*"]))
        self.assertEqual(code, 2)
        self.assertIn("at least 3 literal", out)

    def test_open_exception_then_close(self):
        self.run_cmd("snapshot", "--label", "stage0")
        self.assertEqual(self.run_cmd("snapshot", "--label", "b", "--allow-list",
                                      self.allow_list(["UFOS.ai/MainWindow.xaml", "UFOS.ai/Shared/AiBudget*.cs"]))[0], 0)
        self.write("UFOS.ai/MainWindow.xaml", "v2")
        self.write("UFOS.ai/Shared/AiBudgetModels.cs", "new")
        self.write("UFOS.ai/Shared/AiBudgetX/evil.cs", "evil")          # L3: * must not cross '/'
        code, out = self.run_cmd("check", "--label", "b")
        self.assertEqual(code, 1)
        self.assertIn("ADDED:\n  UFOS.ai/Shared/AiBudgetX/evil.cs", out)
        os.remove(self.root / "UFOS.ai/Shared/AiBudgetX/evil.cs")
        self.assertEqual(self.run_cmd("check", "--label", "b")[0], 0)
        code, out = self.run_cmd("check", "--label", "stage0")
        self.assertEqual(code, 0, out)                                   # embedded list applied
        self.assertIn("applied (open)", out)
        self.assertEqual(self.run_cmd("check", "--label", "stage0", "--strict")[0], 1)
        # close freezes the hashes
        code, out = self.run_cmd("close", "--label", "b")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_cmd("close", "--label", "b")[0], 2)    # once only
        for label in ("b", "stage0"):
            self.assertEqual(self.run_cmd("check", "--label", label)[0], 0, label)
        self.write("UFOS.ai/MainWindow.xaml", "v3")                       # a change AFTER close
        for label in ("b", "stage0"):
            code, out = self.run_cmd("check", "--label", label)
            self.assertEqual(code, 1, label)
            self.assertIn("CHANGED:\n  UFOS.ai/MainWindow.xaml", out)
        self.write("UFOS.ai/MainWindow.xaml", "v2")                       # back to the frozen content
        self.assertEqual(self.run_cmd("check", "--label", "stage0")[0], 0)
        self.write("UFOS.ai/Shared/AiBudgetLater.cs", "later")            # matches the pattern, not frozen
        code, out = self.run_cmd("check", "--label", "stage0")
        self.assertEqual(code, 1)
        self.assertIn("UFOS.ai/Shared/AiBudgetLater.cs", out)
        # amend is refused after close
        self.assertEqual(self.run_cmd("amend", "--label", "b", "--allow-list", str(self.allow))[0], 2)

    def test_absent_entry_frozen_as_absent(self):
        self.run_cmd("snapshot", "--label", "b", "--allow-list", self.allow_list(["UFOS.ai/NewWindow.xaml"]))
        self.run_cmd("close", "--label", "b")
        self.assertEqual(self.run_cmd("check", "--label", "b")[0], 0)
        self.write("UFOS.ai/NewWindow.xaml", "created after close")
        self.assertEqual(self.run_cmd("check", "--label", "b")[0], 1)

    def test_amend_adds_only(self):
        self.run_cmd("snapshot", "--label", "b", "--allow-list", self.allow_list(["UFOS.ai/MainWindow.xaml"]))
        self.write("UFOS.ai/Other.cs", "v2")
        self.assertEqual(self.run_cmd("check", "--label", "b")[0], 1)
        code, out = self.run_cmd("amend", "--label", "b", "--allow-list",
                                 self.allow_list(["UFOS.ai/MainWindow.xaml", "UFOS.ai/Other.cs"]))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_cmd("check", "--label", "b")[0], 0)
        manifest = json.loads((sg.OUT_DIR / "manifest-b.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["amendments"][0]["added"], ["UFOS.ai/Other.cs"])
        self.assertEqual(self.run_cmd("amend", "--label", "b", "--allow-list",
                                      self.allow_list(["UFOS.ai/Other.cs"]))[0], 2)     # no removals

    def test_allow_file_exact_only(self):
        self.run_cmd("snapshot", "--label", "stage0")
        self.write("UFOS.ai/Other.cs", "v2")
        self.assertEqual(self.run_cmd("check", "--label", "stage0", "--allow-file", "UFOS.ai/Other.cs")[0], 0)
        self.assertEqual(self.run_cmd("check", "--label", "stage0", "--allow-file", "UFOS.ai/Oth*.cs")[0], 2)


if __name__ == "__main__":
    unittest.main()
