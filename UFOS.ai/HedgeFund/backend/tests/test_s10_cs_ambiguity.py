"""Static check (tools/check_cs_ambiguity.py): no CS0104-style ambiguous type names in the WPF C# code.

2026-10-25: `TileMode` was ambiguous between System.Windows.Media and UFOS.ai.ModuleTiles in Tiles/TilePreviewView.cs and
broke the build. No compiler here — a text-based check. Offline, read-only."""
import importlib.util
import os
import unittest

HEDGEFUND = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
UFOS = os.path.normpath(os.path.join(HEDGEFUND, ".."))
TOOL = os.path.join(HEDGEFUND, "tools", "check_cs_ambiguity.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_cs_ambiguity", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LIB = "namespace UFOS.ai.ModuleTiles\n{\n    public enum TileMode { Live, LastView }\n}\n"
VIEW = ("using System.Windows.Media;\nusing UFOS.ai.ModuleTiles;\n{alias}\nnamespace UFOS.ai.Tiles\n{\n"
        "    internal sealed class View\n    {\n        private TileMode _mode;   // TileMode in a comment\n"
        "        public string TileModeText => \"TileMode\";\n        public void Set(TileMode mode) { }\n    }\n}\n")


class CsAmbiguityTest(unittest.TestCase):
    def test_tree_is_clean(self):
        if not os.path.isdir(os.path.join(UFOS, "Tiles")):
            self.skipTest("UFOS.ai C# tree not in this checkout")
        mod = _load()
        files = {p: open(p, encoding="utf-8-sig", errors="replace").read() for p in mod.cs_files(UFOS)}
        self.assertEqual([], mod.analyse(files, mod.find_projects(UFOS)))

    def test_finds_the_tilemode_break(self):
        mod = _load()
        found = mod.analyse({"lib/Lib.cs": LIB, "app/View.cs": VIEW.replace("{alias}", "")})
        self.assertEqual({("app/View.cs", "TileMode")}, {(f[0], f[2]) for f in found})
        self.assertEqual(["System.Windows.Media", "UFOS.ai.ModuleTiles"], found[0][3])

    def test_alias_resolves_it(self):
        mod = _load()
        fixed = VIEW.replace("{alias}", "using TileMode = UFOS.ai.ModuleTiles.TileMode;")
        self.assertEqual([], mod.analyse({"lib/Lib.cs": LIB, "app/View.cs": fixed}))

    def test_own_namespace_wins(self):
        mod = _load()
        src = ("using System.Windows.Media;\nusing System.Windows.Shapes;\nnamespace UFOS.ai.Tiles\n{\n"
               "    internal enum Stretch { A }\n    internal sealed class V { Stretch s; }\n}\n")
        self.assertEqual([], mod.analyse({"app/V.cs": src}))

    def test_member_access_and_declarations_are_not_uses(self):
        mod = _load()
        src = ("using System.IO;\nusing System.Windows.Shapes;\nnamespace X\n{\n    class C\n    {\n"
               "        string Path { get; set; }\n        void M(Foo f) { var p = f.Path; var o = new Foo { Path = 1 }; }\n    }\n}\n")
        self.assertEqual([], mod.analyse({"x/C.cs": src}))
        use = src.replace("var p = f.Path;", "var p = Path.Combine(\"a\", \"b\");")
        self.assertEqual(["Path"], [f[2] for f in mod.analyse({"x/C.cs": use})])


if __name__ == "__main__":
    unittest.main()
