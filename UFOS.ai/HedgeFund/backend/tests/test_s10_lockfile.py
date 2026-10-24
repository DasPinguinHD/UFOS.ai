"""Stage 10 pinned dependencies: make_lockfile.py (lock lines from pip's --report, sha256 required, source line,
stale detection, installed vs --update constraints), the JOINT resolution of the main + broker sets (one venv:
shared packages such as certifi agree in both locks; a broker conflict writes the main lock alone plus a "not
installable" broker lock that names it, exit 5 - user's first run 2026-10-24: ccxt 4.5.84 pins certifi==2026.6.17),
the copies in the three backends byte-identical, and the installers install only from the lock with
--require-hashes. Offline (pip is faked at the subprocess boundary)."""
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
MAIN, BROKER, JOINT = "requirements.txt", "requirements-broker.txt", "requirements.txt+requirements-broker.txt"

# pip's ResolutionImpossible output (shape of the user's run, 2026-10-24 16:12Z)
CONFLICT_STDERR = """ERROR: Cannot install -r requirements-broker.txt (line 6) because these package versions have conflicting dependencies.

The conflict is caused by:
    The user requested (constraint) certifi==2026.10.19
    ccxt 4.5.84 depends on certifi==2026.6.17

To fix this you could try to:
1. loosen the range of package versions you've specified
2. remove package versions to allow pip to attempt to solve the dependency conflict

ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/topics/dependency-resolution/#dealing-with-dependency-conflicts
"""


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load(path=TOOL):
    spec = importlib.util.spec_from_file_location("make_lockfile_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def item(name, version, digest=None, old_style=False):
    digest = digest if digest is not None else (H if version != "2026.6.17" else "c" * 64)
    info = {"hash": f"sha256={digest}"} if old_style else {"hashes": {"sha256": digest}}
    return {"metadata": {"name": name, "version": version}, "download_info": {"url": "https://x", "archive_info": info}}


class FakePip:
    """pip at the subprocess boundary. freeze -> the installed pins; install --dry-run --report -> ``resolved[key]``
    where key = the -r file names joined by "+" (several -r = one joint resolution). A value is a list of report
    items, a callable(constraints) -> list, or a string = pip's stderr for a failed run (exit 1). Every install call
    records ("constraints", [...] or None)."""

    def __init__(self, resolved, frozen=("aiohttp==3.10.0", "-e git+x#egg=y", "yarl==1.9.0"), fail=False):
        self.resolved, self.frozen, self.fail, self.calls = resolved, list(frozen), fail, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "freeze":
            return subprocess.CompletedProcess(args, 0, "\n".join(self.frozen) + "\n", "")
        if self.fail:
            return subprocess.CompletedProcess(args, 1, "", "ERROR: Could not find a version (offline)")
        reqs = [os.path.basename(args[i + 1]) for i, a in enumerate(args) if a == "-r"]
        cons = None
        if "-c" in args:
            with open(args[args.index("-c") + 1], encoding="utf-8") as fh:
                cons = fh.read().split()
        self.calls[-1].append(("constraints", cons))
        result = self.resolved["+".join(reqs)]
        if callable(result):
            result = result(cons)
        if isinstance(result, str):
            return subprocess.CompletedProcess(args, 1, "", result)
        with open(args[args.index("--report") + 1], "w", encoding="utf-8") as fh:
            json.dump({"install": result}, fh)
        return subprocess.CompletedProcess(args, 0, "", "")

    def installs(self):
        return [(tuple(os.path.basename(c[i + 1]) for i, a in enumerate(c) if a == "-r"), c[-1][1])
                for c in self.calls if c[0] == "install"]


def by_constraints(with_pins, without):
    """A resolution that follows the joint pins when they are given as constraints (like pip would)."""
    def run(cons):
        if cons and any(c.startswith("certifi==2026.6.17") for c in cons):
            return with_pins
        return without
    return run


class LockfileTest(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.mkdtemp(prefix="s10-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.write(MAIN, "aiohttp>=3.9\ncertifi>=2024.2.2\n")

    def write(self, name, text):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def lock(self, name):
        return _read(os.path.join(self.tmp, name))

    def state(self, req, lock):
        return self.m.lock_state(self.tmp, req, lock)

    def test_entries_need_sha256(self):
        e = self.m.entries_from_report({"install": [item("Yarl", "1.9.0"), item("aiohttp", "3.10.0", "b" * 64, True)]})
        self.assertEqual(e, [("aiohttp", "3.10.0", "b" * 64), ("yarl", "1.9.0", H)])
        with self.assertRaises(ValueError) as cm:
            self.m.entries_from_report({"install": [item("x", "1", digest="")]})
        self.assertIn("x==1", str(cm.exception))

    def test_render_parse_and_state(self):
        req = os.path.join(self.tmp, MAIN)
        text = self.m.render_lock([("aiohttp", "3.10.0", H)], MAIN, self.m.sha256_file(req), "installed",
                                  now="2026-10-24T00:00:00Z")
        self.assertIn("aiohttp==3.10.0 --hash=sha256:" + H, text.splitlines())
        src, pins = self.m.parse_lock(text)
        self.assertEqual((src, pins), ((MAIN, self.m.sha256_file(req)), {"aiohttp": "3.10.0"}))
        self.assertEqual(self.state(MAIN, "requirements.lock"), "missing")
        self.write("requirements.lock", text)
        self.assertEqual(self.state(MAIN, "requirements.lock"), "ok")
        with open(req, "a", encoding="utf-8") as fh:
            fh.write("openai>=1\n")
        self.assertTrue(self.state(MAIN, "requirements.lock").startswith("stale"))
        # the PowerShell installers read the same source line
        self.assertTrue(re.search(r"^# source: \S+ sha256=([0-9a-f]{64})$", text, re.M))

    def test_no_broker_set_single_resolution(self):
        pip = FakePip({MAIN: [item("aiohttp", "3.10.0"), item("yarl", "1.9.0")]})
        lines = []
        self.assertEqual(self.m.make(self.tmp, update=False, out=lines.append, pip=pip), 0)
        self.assertEqual(pip.installs(), [((MAIN,), ["aiohttp==3.10.0", "yarl==1.9.0"])])   # editable skipped
        main = self.lock("requirements.lock")
        self.assertIn("# mode: installed", main)
        self.assertNotIn("# resolved:", main)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "requirements-broker.lock")))
        for c in pip.calls[1:]:
            self.assertIn("--dry-run", c)
            self.assertIn("--ignore-installed", c)

    def test_joint_resolution_both_locks_agree(self):
        self.write(BROKER, "ccxt==4.5.84\n")
        shared = [item("aiohttp", "3.14.5"), item("certifi", "2026.6.17")]
        pip = FakePip({MAIN: shared, JOINT: shared + [item("ccxt", "4.5.84")],
                       BROKER: [item("ccxt", "4.5.84"), item("aiohttp", "3.14.5"), item("certifi", "2026.6.17")]},
                      frozen=("aiohttp==3.14.5", "certifi==2026.6.17", "ccxt==4.5.84"))
        lines = []
        self.assertEqual(self.m.make(self.tmp, update=False, out=lines.append, pip=pip), 0)
        frozen = ["aiohttp==3.14.5", "certifi==2026.6.17", "ccxt==4.5.84"]
        joint_pins = ["aiohttp==3.14.5", "ccxt==4.5.84", "certifi==2026.6.17"]
        self.assertEqual(pip.installs(), [((MAIN,), frozen), ((MAIN, BROKER), frozen),
                                          ((MAIN,), joint_pins), ((BROKER,), joint_pins)])
        main, broker = self.lock("requirements.lock"), self.lock("requirements-broker.lock")
        _s, mp = self.m.parse_lock(main)
        _s, bp = self.m.parse_lock(broker)
        self.assertEqual(mp, {"aiohttp": "3.14.5", "certifi": "2026.6.17"})
        self.assertEqual(bp["certifi"], mp["certifi"])                       # one venv: shared versions agree
        self.assertIn("# resolved: together with requirements-broker.txt", main)
        self.assertIn(f"# with: requirements.lock sha256={self.m.sha256_text(main)}", broker)
        self.assertNotIn("# conflict:", broker)
        self.assertEqual(self.state(MAIN, "requirements.lock"), "ok")
        self.assertEqual(self.state(BROKER, "requirements-broker.lock"), "ok")
        # the broker lock belongs to THAT main lock: a different main lock makes it stale
        self.write("requirements.lock", main.replace("3.14.5", "3.14.6"))
        self.assertTrue(self.state(BROKER, "requirements-broker.lock").startswith("stale (made for another"))

    def test_installed_mode_broker_conflict_writes_main_alone(self):
        """The user's first run: the venv has certifi 2026.10.19 but ccxt 4.5.84 pins certifi==2026.6.17."""
        self.write(BROKER, "ccxt==4.5.84\n")
        alone = [item("aiohttp", "3.14.5"), item("certifi", "2026.10.19")]
        pip = FakePip({MAIN: alone, JOINT: CONFLICT_STDERR},
                      frozen=("aiohttp==3.14.5", "certifi==2026.10.19", "ccxt==4.5.84"))
        lines = []
        self.assertEqual(self.m.make(self.tmp, update=False, out=lines.append, pip=pip), 5)
        main, broker = self.lock("requirements.lock"), self.lock("requirements-broker.lock")
        self.assertEqual(self.m.parse_lock(main)[1], {"aiohttp": "3.14.5", "certifi": "2026.10.19"})
        self.assertIn("# resolved: alone - requirements-broker.txt cannot be resolved together with it", main)
        self.assertEqual(self.m.parse_lock(broker)[1], {})                   # nothing installable
        conflict = re.search(r"^# conflict: (.+)$", broker, re.M).group(1)
        self.assertIn("ccxt 4.5.84 depends on certifi==2026.6.17", conflict)
        self.assertIn(f"# with: requirements.lock sha256={self.m.sha256_text(main)}", broker)
        self.assertEqual(self.state(MAIN, "requirements.lock"), "ok")       # the main env is never blocked
        st = self.state(BROKER, "requirements-broker.lock")
        self.assertTrue(st.startswith("not installable (conflicting dependencies:"), st)
        said = "\n".join(lines)
        self.assertIn("NOT INSTALLABLE", said)
        self.assertIn("certifi==2026.6.17", said)
        self.assertIn("make_lockfile.py --update", said)                     # the way out is named
        self.assertTrue(broker.isascii())

    def test_update_mode_joint_holds_back_shared_package(self):
        """--update ("Update Python packages"): fresh joint resolution; certifi follows ccxt's exact pin and the main
        lock says which package the broker set holds back."""
        self.write(BROKER, "ccxt==4.5.84\n")
        alone = [item("aiohttp", "3.14.5"), item("certifi", "2026.10.19")]
        joint = [item("aiohttp", "3.14.5"), item("ccxt", "4.5.84"), item("certifi", "2026.6.17")]
        main_j = [item("aiohttp", "3.14.5"), item("certifi", "2026.6.17")]
        pip = FakePip({MAIN: by_constraints(main_j, alone), JOINT: joint,
                       BROKER: [item("ccxt", "4.5.84"), item("certifi", "2026.6.17")]})
        lines = []
        self.assertEqual(self.m.make(self.tmp, update=True, out=lines.append, pip=pip), 0)
        self.assertFalse([c for c in pip.calls if c[0] == "freeze"])
        inst = pip.installs()
        self.assertEqual(inst[0], ((MAIN,), None))                           # fresh
        self.assertEqual(inst[1], ((MAIN, BROKER), None))                    # fresh, joint
        main = self.lock("requirements.lock")
        self.assertIn("# mode: update", main)
        self.assertEqual(self.m.parse_lock(main)[1]["certifi"], "2026.6.17")
        self.assertIn("# held-by-broker: certifi 2026.6.17 (alone: 2026.10.19)", main)
        self.assertEqual(self.state(BROKER, "requirements-broker.lock"), "ok")
        self.assertTrue(any("held-by-broker" in x for x in lines))

    def test_update_mode_conflict_names_the_fix(self):
        self.write(MAIN, "certifi==2026.10.19\n")
        self.write(BROKER, "ccxt==4.5.84\n")
        pip = FakePip({MAIN: [item("certifi", "2026.10.19")], JOINT: CONFLICT_STDERR})
        lines = []
        self.assertEqual(self.m.make(self.tmp, update=True, out=lines.append, pip=pip), 5)
        self.assertIn("certifi==2026.10.19", self.lock("requirements.lock"))
        broker = self.lock("requirements-broker.lock")
        self.assertIn("change requirements-broker.txt", broker)
        self.assertIn("change requirements-broker.txt", "\n".join(lines))

    def test_broker_without_sha256_or_other_pip_error_never_blocks_main(self):
        self.write(BROKER, "ccxt==4.5.84\n")
        for joint in ([item("aiohttp", "3.10.0"), item("ccxt", "4.5.84", digest="")],
                      "ERROR: No matching distribution found for ccxt==4.5.84"):
            pip = FakePip({MAIN: [item("aiohttp", "3.10.0")], JOINT: joint}, frozen=("aiohttp==3.10.0",))
            lines = []
            self.assertEqual(self.m.make(self.tmp, update=False, out=lines.append, pip=pip), 5)
            self.assertEqual(self.state(MAIN, "requirements.lock"), "ok")
            self.assertTrue(self.state(BROKER, "requirements-broker.lock").startswith("not installable"))
            self.assertIn("ccxt", re.search(r"^# conflict: (.+)$", self.lock("requirements-broker.lock"), re.M).group(1))

    def test_main_failures_write_nothing(self):
        self.write(BROKER, "ccxt==4.5.84\n")
        lines = []
        self.assertEqual(self.m.make(self.tmp, False, out=lines.append, pip=FakePip({}, fail=True)), 3)
        self.assertEqual(self.m.make(self.tmp, False, out=lines.append,
                                     pip=FakePip({MAIN: [item("x", "1", digest="")]})), 4)
        self.assertEqual(self.m.make(self.tmp, False, out=lines.append, pip=FakePip({MAIN: CONFLICT_STDERR})), 3)
        for name in ("requirements.lock", "requirements-broker.lock"):
            self.assertFalse(os.path.exists(os.path.join(self.tmp, name)))

    def test_old_broker_lock_without_with_line_is_stale(self):
        self.write(BROKER, "ccxt==4.5.84\n")
        text = self.m.render_lock([("ccxt", "4.5.84", H)], BROKER,
                                  self.m.sha256_file(os.path.join(self.tmp, BROKER)), "installed")
        self.write("requirements-broker.lock", text)
        self.assertTrue(self.state(BROKER, "requirements-broker.lock").startswith("stale (no '# with:' line"))

    def test_pip_reason(self):
        r = self.m.pip_reason(CONFLICT_STDERR)
        self.assertTrue(r.startswith("conflicting dependencies: The user requested (constraint) certifi==2026.10.19;"))
        self.assertEqual(self.m.pip_reason("x\nERROR: one\nERROR: two\n"), "ERROR: two")
        self.assertEqual(self.m.pip_reason("only — line"), "only line")
        self.assertEqual(self.m.pip_reason(""), "no output")

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

    def test_hedgefund_installer_handles_the_broker_conflict(self):
        """-Upgrade: make_lockfile exit 5 = main lock regenerated alone -> the main install still runs; the broker
        step refuses a "not installable" lock or one made for another requirements.lock (status shows why)."""
        with open(os.path.join(BACKEND, "run_hedgefund_backend.ps1"), encoding="ascii") as fh:
            hf = fh.read()
        self.assertIn("if ($lockExit -eq 5)", hf)
        self.assertIn("elseif ($lockExit -ne 0)", hf)
        self.assertIn("'^# conflict: (.+)$'", hf)
        self.assertIn("'^# with: requirements\\.lock sha256=([0-9a-f]{64})$'", hf)
        self.assertIn("if (-not $lockProblem) { $lockProblem = Get-BrokerLockProblem }", hf)


if __name__ == "__main__":
    unittest.main()
