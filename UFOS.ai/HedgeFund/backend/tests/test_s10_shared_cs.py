"""Static check of UFOS.ai/Shared/*.cs (tools/check_shared_cs_members.py): no duplicate member / type names.

Shared/*.cs is linked into four assemblies; a duplicate breaks every build (2026-10-24: two `LockFileName` constants in
PythonEnvironment.cs). No compiler here — a tokenizer-based check. Offline, read-only."""
import importlib.util
import os
import tempfile
import unittest

HEDGEFUND = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SHARED = os.path.normpath(os.path.join(HEDGEFUND, "..", "Shared"))
TOOL = os.path.join(HEDGEFUND, "tools", "check_shared_cs_members.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_shared_cs_members", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SharedCsMembersTest(unittest.TestCase):
    def test_shared_folder_is_clean(self):
        if not os.path.isdir(SHARED):
            self.skipTest("UFOS.ai/Shared not in this checkout")
        self.assertEqual([], _load().check(SHARED))

    def test_check_finds_duplicates(self):
        guard = _load()
        files = {
            "A.cs": (
                "namespace X.Shared\n{\n"
                "    internal static class Env\n    {\n"
                "        private const string LockFileName = \".venv.lock\";\n"
                "        internal const string LockFileName = \"requirements.lock\";\n"
                "        public static string Path(string a) => a;\n"
                "        public static string Path(int a) => \"\";   // overload: fine\n"
                "        private static readonly (string Name, string[] Dir)[] Modules = { };\n"
                "        public static int Count { get; set; }\n"
                "        public static void Count2() { var s = \"{ ; }\"; for (var i = 0; i < 2; i++) { } }\n"
                "    }\n"
                "    internal sealed class Holder { public int Value; public int Value() => 1; }\n"
                "}\n"),
            "B.cs": "namespace X.Shared\n{\n    internal enum Env2 { A, B }\n    internal sealed class Holder { }\n}\n",
            "C.cs": "namespace X.Shared\n{\n    internal partial class Part { public int A; }\n}\n",
            "D.cs": "namespace X.Shared\n{\n    internal partial class Part { public int B; public int A; }\n}\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in files.items():
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            problems = guard.check(tmp)
        text = "\n".join(problems)
        self.assertIn("Env.LockFileName (const) duplicates", text)
        self.assertIn("method Holder.Value has the name of the field/property", text)
        self.assertIn("type X.Shared.Holder is also declared", text)
        self.assertIn("Part.A (field/property) duplicates", text)
        self.assertNotIn("Path", text)
        self.assertNotIn("Modules", text)
        self.assertNotIn("Count", text)
        self.assertEqual(4, len(problems), text)


if __name__ == "__main__":
    unittest.main()
