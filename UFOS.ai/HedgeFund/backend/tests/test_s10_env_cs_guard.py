"""S10-E guard, C#/XAML side (tools/check_no_env_cs.py): no WPF code reads .env or a secret from the environment.

The full check needs the whole UFOS.ai folder (main app, Shared, Global Monitoring, LiveStream Agent); a checkout with
only the HedgeFund tree checks that tree. Offline, read-only."""
import importlib.util
import os
import tempfile
import unittest

BACKEND = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HEDGEFUND = os.path.normpath(os.path.join(BACKEND, ".."))
UFOS_ROOT = os.path.normpath(os.path.join(HEDGEFUND, ".."))
TOOL = os.path.join(HEDGEFUND, "tools", "check_no_env_cs.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_no_env_cs", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EnvCsGuardTest(unittest.TestCase):
    def test_solution_has_no_env_reader(self):
        guard = _load()
        if os.path.exists(os.path.join(UFOS_ROOT, "UFOS.ai.csproj")):
            violations = guard.scan(UFOS_ROOT)
        else:
            violations = guard.scan(HEDGEFUND, prefix="HedgeFund/")      # the exemptions are UFOS.ai-relative
        self.assertEqual([], violations)

    def test_guard_catches_readers(self):
        guard = _load()
        samples = {
            "A.cs": 'var p = Path.Combine(dir, ".env");\n',
            "B.cs": 'var k = Environment.GetEnvironmentVariable("OPENROUTER_API_KEY");\n',
            "C.cs": "var v = EnvConfig.Get(\"X\");\n",
            "D.csproj": '<None Include="..\\.env.example" />\n',
        }
        clean = {
            "E.cs": '// reads UFOS.ai/.env no more\nvar s = "set it in Keys & data"; /* ".env" */\n',
            "F.cs": 'var host = Environment.GetEnvironmentVariable("WS_HOST");\n',
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in {**samples, **clean}.items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            hits = {rel for rel, _, _ in guard.scan(tmp)}
        self.assertEqual(set(samples), hits)


if __name__ == "__main__":
    unittest.main()
