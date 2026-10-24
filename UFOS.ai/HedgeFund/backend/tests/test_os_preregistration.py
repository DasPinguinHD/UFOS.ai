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


if __name__ == "__main__":
    unittest.main()
