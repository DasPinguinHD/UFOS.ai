"""The frozen pre-registration of the Options Structure hypotheses (SPEC §21.8): the file's bytes must still hash to
the sha256 recorded at freezing (2026-10-23) — any change fails. Changes are only allowed as NEW, separately dated
hypothesis files. The SPEC must name the same path, hash and date."""
import hashlib
import os
import unittest

HF = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(HF, "docs", "preregistration", "OS-H1-H2-2026-10-23.md")
SHA256 = "95d3ac66736ad358df23b4628f0c32425f4b0b4be37138f7b52a813afc3b8195"
SPEC = os.path.join(HF, "backend", "strategy_platform", "SPEC.md")
# dated operationalisation note (user decision 2026-11-06): how "total GEX" is measured — the frozen file is unchanged
NOTE = os.path.join(HF, "docs", "preregistration", "OS-H1-H2-2026-11-06-operationalisation.md")
NOTE_SHA256 = "2f09ddbcd02f03f888cfc4daa899dd3b2bbe689a1135e4f3f7c5fe76bc3754bc"
NOTE_B = os.path.join(HF, "docs", "preregistration", "OS-H1-H2-2026-11-06b-operationalisation.md")
NOTE_B_SHA256 = "b54447554edae538996257bb8dacf536538b455e500c59f792e773ac25f7f05e"


class PreregistrationTest(unittest.TestCase):
    def test_file_unchanged(self):
        with open(PATH, "rb") as fh:
            data = fh.read()
        self.assertEqual(hashlib.sha256(data).hexdigest(), SHA256,
                         "the frozen pre-registration changed — never edit it; add a NEW dated hypothesis file instead")
        self.assertNotIn(b"\r", data)                       # byte-exact LF (see HedgeFund/.gitattributes)
        self.assertIn(b"FROZEN 2026-10-23", data)

    def test_spec_records_path_hash_and_date(self):
        with open(SPEC, encoding="utf-8") as fh:
            spec = fh.read()
        self.assertIn("HedgeFund/docs/preregistration/OS-H1-H2-2026-10-23.md", spec)
        self.assertIn(SHA256, spec)
        self.assertIn("frozen **2026-10-23**", spec)

    def test_operationalisation_note(self):
        with open(NOTE, "rb") as fh:
            data = fh.read()
        self.assertEqual(hashlib.sha256(data).hexdigest(), NOTE_SHA256, "the dated note changed — add a NEW note")
        self.assertNotIn(b"\r", data)
        self.assertIn(b"is NOT edited", data)
        self.assertIn(SHA256.encode(), data)                # it names the frozen file it operationalises
        with open(SPEC, encoding="utf-8") as fh:
            spec = fh.read()
        self.assertIn("HedgeFund/docs/preregistration/OS-H1-H2-2026-11-06-operationalisation.md", spec)
        self.assertIn(NOTE_SHA256, spec)

    def test_operationalisation_note_b(self):
        # os2d (user 2026-11-06): amends the first note — OI window D−1 / D−2 and the 15 % evaluability rule
        with open(NOTE_B, "rb") as fh:
            data = fh.read()
        self.assertEqual(hashlib.sha256(data).hexdigest(), NOTE_B_SHA256, "the dated note changed — add a NEW note")
        self.assertNotIn(b"\r", data)
        self.assertIn(b"are NOT edited", data)
        self.assertIn(SHA256.encode(), data)                # names the frozen file …
        self.assertIn(NOTE_SHA256.encode(), data)           # … and the note it amends
        self.assertIn("15 %".encode(), data)
        self.assertIn("(D−2)".encode(), data)
        with open(os.path.join(HF, "backend", "strategy_platform", "SPEC.md"), encoding="utf-8") as fh:
            spec = fh.read()
        self.assertIn("docs/preregistration/OS-H1-H2-2026-11-06b-operationalisation.md", spec)
        self.assertIn(NOTE_B_SHA256, spec)


if __name__ == "__main__":
    unittest.main()
