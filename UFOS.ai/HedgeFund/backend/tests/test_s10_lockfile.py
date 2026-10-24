"""Stage 10 pinned dependencies: make_lockfile.py (lock lines from pip's --report, sha256 required, source line,
stale detection, installed vs --update constraints, the broker set constrained by the main pins), the copies in the
three backends byte-identical, and the installers install only from the lock with --require-hashes. Offline (pip
is faked)."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(BACKEND, "tools", "make_lockfile.py")
REPO_MODULES = os.path.dirname(os.path.dirname(BACKEND))          # .../UFOS.ai (holds the module folders)
H = "a" * 64


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load(path=TOOL):
    spec = importlib.util.spec_from_file_location("make_lockfile_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def item(name, version, digest=H, old_style=False):
    info = {"hash": f"sha256={digest}"} if old_style else {"hashes": {"sha256": digest}}
    return {"metadata": {"name": name, "version": version}, "download_info": {"url": "https://x", "archive_info": info}}


class FakePip:
    """pip at the subprocess boundary: freeze -> the installed pins; install --dry-run --report -> a report built
    from ``resolved[<requirements file name>]`` (the constraints file is recorded)."""

    def __init__(self, resolved, frozen=("aiohttp==3.10.0", "-e git+x#egg=y", "yarl==1.9.0"), fail=False):
        self.resolved, self.frozen, self.fail, self.calls = resolved, list(frozen), fail, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "freeze":
            return subprocess.CompletedProcess(args, 0, "\n".join(self.frozen) + "\n", "")
        if self.fail:
            return subprocess.CompletedProcess(args, 1, "", "ERROR: Could not find a version (offline)")
        req = os.path.basename(args[args.index("-r") + 1])
        cons = None
        if "-c" in args:
            with open(args[args.index("-c") + 1], encoding="utf-8") as fh:
                cons = fh.read().split()
        self.calls[-1].append(("constraints", cons))
        with open(args[args.index("--report") + 1], "w", encoding="utf-8") as fh:
            json.dump({"install": self.resolved[req]}, fh)
        return subprocess.CompletedProcess(args, 0, "", "")


class LockfileTest(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.mkdtemp(prefix="s10-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, "requirements.txt"), "w", encoding="utf-8") as fh:
            fh.write("aiohttp>=3.9\n")

    def test_entries_need_sha256(self):
        e = self.m.entries_from_report({"install": [item("Yarl", "1.9.0"), item("aiohttp", "3.10.0", "b" * 64, True)]})
        self.assertEqual(e, [("aiohttp", "3.10.0", "b" * 64), ("yarl", "1.9.0", H)])
        with self.assertRaises(ValueError) as cm:
            self.m.entries_from_report({"install": [item("x", "1", digest="")]})
        self.assertIn("x==1", str(cm.exception))

    def test_render_parse_and_state(self):
        req = os.path.join(self.tmp, "requirements.txt")
        text = self.m.render_lock([("aiohttp", "3.10.0", H)], "requirements.txt", self.m.sha256_file(req), "installed",
                                  now="2026-10-24T00:00:00Z")
        self.assertIn("aiohttp==3.10.0 --hash=sha256:" + H, text.splitlines())
        src, pins = self.m.parse_lock(text)
        self.assertEqual((src, pins), (("requirements.txt", self.m.sha256_file(req)), {"aiohttp": "3.10.0"}))
        self.assertEqual(self.m.lock_state(self.tmp, "requirements.txt", "requirements.lock"), "missing")
        with open(os.path.join(self.tmp, "requirements.lock"), "w", encoding="utf-8") as fh:
            fh.write(text)
        self.assertEqual(self.m.lock_state(self.tmp, "requirements.txt", "requirements.lock"), "ok")
        with open(req, "a", encoding="utf-8") as fh:
            fh.write("openai>=1\n")
        self.assertTrue(self.m.lock_state(self.tmp, "requirements.txt", "requirements.lock").startswith("stale"))
        # the PowerShell installers read the same source line
        self.assertTrue(re.search(r"^# source: \S+ sha256=([0-9a-f]{64})$", text, re.M))

    def test_make_installed_and_update(self):
        with open(os.path.join(self.tmp, "requirements-broker.txt"), "w", encoding="utf-8") as fh:
            fh.write("ccxt==4.5.84\n")
        pip = FakePip({"requirements.txt": [item("aiohttp", "3.10.0"), item("yarl", "1.9.0")],
                       "requirements-broker.txt": [item("ccxt", "4.5.84"), item("yarl", "1.9.0")]})
        lines = []
        self.assertEqual(self.m.make(self.tmp, update=False, out=lines.append, pip=pip), 0)
        main = _read(os.path.join(self.tmp, "requirements.lock"))
        broker = _read(os.path.join(self.tmp, "requirements-broker.lock"))
        self.assertIn("# mode: installed", main)
        self.assertIn("ccxt==4.5.84 --hash=sha256:" + H, broker)
        installs = [c for c in pip.calls if c[0] == "install"]
        self.assertEqual(installs[0][-1], ("constraints", ["aiohttp==3.10.0", "yarl==1.9.0"]))   # editable skipped
        self.assertIn("aiohttp==3.10.0", installs[1][-1][1])                                    # main pins too
        for c in installs:
            self.assertIn("--dry-run", c)
            self.assertIn("--ignore-installed", c)
        pip2 = FakePip({"requirements.txt": [item("aiohttp", "3.11.0")], "requirements-broker.txt": [item("ccxt", "4.6")]})
        self.assertEqual(self.m.make(self.tmp, update=True, out=lines.append, pip=pip2), 0)
        inst = [c for c in pip2.calls if c[0] == "install"]
        self.assertEqual(inst[0][-1], ("constraints", None))                                    # fresh resolution
        self.assertEqual(inst[1][-1], ("constraints", ["aiohttp==3.11.0"]))
        self.assertFalse([c for c in pip2.calls if c[0] == "freeze"])
        self.assertIn("# mode: update", _read(os.path.join(self.tmp, "requirements.lock")))

    def test_failures_write_nothing(self):
        lines = []
        self.assertEqual(self.m.make(self.tmp, False, out=lines.append, pip=FakePip({}, fail=True)), 3)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "requirements.lock")))
        self.assertEqual(self.m.make(self.tmp, False, out=lines.append,
                                     pip=FakePip({"requirements.txt": [item("x", "1", digest="")]})), 4)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "requirements.lock")))

    def test_venv_check(self):
        self.assertIsNone(self.m.venv_problem(self.tmp, os.path.join(self.tmp, ".venv")))
        self.assertIn("not the backend's Python", self.m.venv_problem(self.tmp, "/usr"))
        self.assertEqual(self.m.backend_dir(TOOL), BACKEND)


class CopiesAndInstallersTest(unittest.TestCase):
    def test_copies_byte_identical(self):
        with open(TOOL, "rb") as fh:
            ours = fh.read()
        for module in ("Global Monitoring", "LiveStreamAgent"):
            other = os.path.join(REPO_MODULES, module, "backend", "make_lockfile.py")
            if not os.path.exists(other):
                self.skipTest(f"{module} not in this checkout")
            with open(other, "rb") as fh:
                self.assertEqual(fh.read(), ours, module)

    def test_installers_use_the_lock_only(self):
        scripts = [os.path.join(BACKEND, "run_hedgefund_backend.ps1")]
        scripts += [os.path.join(REPO_MODULES, m, "backend", "run_backend.ps1") for m in ("Global Monitoring",
                                                                                          "LiveStreamAgent")]
        for path in scripts:
            if not os.path.exists(path):
                continue
            with open(path, encoding="ascii") as fh:
                src = fh.read()
            self.assertNotIn("'--upgrade'", src, path)
            self.assertNotIn("'install', '-r', $Requirements", src, path)
            self.assertIn("'install', '--require-hashes', '-r', $ReqLock", src, path)
            self.assertIn("--update", src, path)
            self.assertIn("Get-LockProblem $Requirements $ReqLock", src, path)
        with open(scripts[0], encoding="ascii") as fh:
            hf = fh.read()
        self.assertIn("'install', '--require-hashes', '-r', $OptionalLock", hf)
        self.assertNotIn("'install', '-r', $OptionalRequirements", hf)


if __name__ == "__main__":
    unittest.main()
