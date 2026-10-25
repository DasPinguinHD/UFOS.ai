"""Static checks of the module tile previews (TILES_API.md; C# — no compiler here, text checks). Offline, read-only.

- Last view privacy: the only write of a picture takes the BLURRED type (TileBlur.BlurredPng, constructor internal) — an
  unblurred image never reaches disk; the capture service itself writes no file.
- Live never starts a backend and never makes a paid / AI / mutating call: the tile providers and the loopback reader use
  GET only, no OpenRouter, no process start, no backend host.
- Storage only under %LOCALAPPDATA%\\UFOS.ai\\tile-previews.
Files are checked as *.cs or, before they are wired into the build, as *.cs.staged."""
import os
import re
import unittest

UFOS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))


def _read(*rel):
    base = os.path.join(UFOS, *rel)
    for path in (base, base + ".staged"):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    raise unittest.SkipTest("not in this checkout: " + "/".join(rel))


_TOKENS = re.compile(r'@"(?:[^"]|"")*"|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])\'|/\*.*?\*/|//[^\n]*', re.S)


def _strip_comments(src):
    """Removes // and /* */ comments; string and char literals (e.g. "http://…") are kept."""
    return _TOKENS.sub(lambda m: "" if m.group(0).startswith("/") else m.group(0), src)


LIB = ("UFOS.ai.ModuleTiles",)
PROVIDERS = [
    ("HedgeFund", "Tiles", "HedgeFundTilePreview.cs"),
    ("Global Monitoring", "Tiles", "GlobalMonitoringTilePreview.cs"),
    ("LiveStreamAgent", "Tiles", "LiveStreamAgentTilePreview.cs"),
    ("Tiles", "MainAppTilePreviews.cs"),
]


class TileBlurPrivacyTest(unittest.TestCase):
    def test_blurred_type_only_from_blur(self):
        src = _strip_comments(_read(*LIB, "TileBlur.cs"))
        self.assertRegex(src, r"public sealed class BlurredPng")
        self.assertRegex(src, r"internal BlurredPng\(byte\[\] bytes")
        self.assertNotRegex(src, r"public BlurredPng\(")
        self.assertNotRegex(src, r"File\.|FileStream")   # blur works in memory only

    def test_store_takes_only_blurred_png(self):
        src = _strip_comments(_read(*LIB, "TileStorage.cs"))
        m = re.search(r"static void Save\(string moduleId, TileBlur\.BlurredPng png, DateTimeOffset takenAt\)", src)
        self.assertIsNotNone(m, "TileLastViewStore.Save must take TileBlur.BlurredPng")
        self.assertNotRegex(src, r"BitmapSource|RenderTargetBitmap")   # no unblurred bitmap type in the store

    def test_capture_writes_nothing_itself(self):
        src = _strip_comments(_read(*LIB, "TileCapture.cs"))
        self.assertNotRegex(src, r"File\.(Write|Create|Open|Move|Copy)|new FileStream|\.Save\(ms|PngBitmapEncoder")
        saves = re.findall(r"TileLastViewStore\.Save\(([^;]*)\);", src)
        self.assertEqual(1, len(saves))
        self.assertIn("TileBlur.BlurToPng", src)

    def test_storage_root_is_localappdata(self):
        src = _strip_comments(_read(*LIB, "TileStorage.cs"))
        self.assertIn('Environment.SpecialFolder.LocalApplicationData), "UFOS.ai", "tile-previews"', src)


class TileLiveReadOnlyTest(unittest.TestCase):
    def test_loopback_reader_is_get_only(self):
        src = _strip_comments(_read(*LIB, "LoopbackReader.cs"))
        self.assertNotRegex(src, r"HttpMethod\.(Post|Put|Delete|Patch)|PostAsync|PutAsync|DeleteAsync")
        self.assertIn('"http://127.0.0.1:"', src)
        self.assertIn("UseProxy = false", src)
        self.assertIn("AllowAutoRedirect = false", src)

    def test_providers_never_start_or_pay(self):
        for rel in PROVIDERS:
            src = _strip_comments(_read(*rel))
            with self.subTest(rel="/".join(rel)):
                self.assertNotRegex(src, r"HttpClient|HttpMethod\.|PostAsync|PutAsync|SendAsync")
                self.assertNotRegex(src, r"Process\.Start|BackendHost|EnsureBackend|StartBackend|OpenRouter|LlmClient")

    def test_hedgefund_does_not_guess_summary_fields(self):
        # lead 2026-10-24: GET /v2/portfolio/summary is not built yet — visible "not available", no guessed fields
        src = _strip_comments(_read("HedgeFund", "Tiles", "HedgeFundTilePreview.cs"))
        self.assertNotIn("GetJsonAsync", src)
        self.assertIn("TileLiveResult.Unavailable(SummaryNotBuilt", src)


if __name__ == "__main__":
    unittest.main()
