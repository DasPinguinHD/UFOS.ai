"""Static checks of the module tile previews (TILES_API.md; C# — no compiler here, text checks). Offline, read-only.

- Last view privacy: the only write of a picture takes the BLURRED type (TileBlur.BlurredPng, constructor internal) — an
  unblurred image never reaches disk; the capture service itself writes no file.
- Live never starts a backend and never makes a paid / AI / mutating call: the tile providers and the loopback reader use
  GET only, no OpenRouter, no process start, no backend host.
- Storage only under %LOCALAPPDATA%\\UFOS.ai\\tile-previews.
Files are checked as *.cs (wired 2026-10-25) or, in an older checkout, as *.cs.staged."""
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
    ("HedgeFund", "Tiles", "HedgeFundTileSummary.cs"),
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

    def test_hedgefund_reads_only_the_summary_route(self):
        # wired 2026-10-25: the tile reads GET /v2/portfolio/summary (SP §17.15, stored values only) and nothing else
        src = _strip_comments(_read("HedgeFund", "Tiles", "HedgeFundTilePreview.cs"))
        self.assertEqual(re.findall(r'GetJsonAsync\("([^"]*)"', src), ["/v2/portfolio/summary"])
        self.assertNotIn("SummaryNotBuilt", src)


class MainAppSettingsTileModesTest(unittest.TestCase):
    def test_tile_modes_key_accepted_by_the_strict_loader(self):
        # the "⋯" menu writes tile_modes into app-settings.json; the strict OpenRouter loader must not reject it
        src = _strip_comments(_read("Shared", "MainAppSettings.cs"))
        self.assertIn('TileModesKey = "tile_modes"', src)
        self.assertIn("if (name == TileModesKey) continue;", src)
        tm = _strip_comments(_read("Tiles", "TileModeSettings.cs"))
        self.assertIn('Key = "tile_modes"', tm)


class HedgeFundSummaryContractTest(unittest.TestCase):
    """Every field the C# tile parser reads exists in the real route payload (contract drift guard, temp DB)."""

    def test_parser_fields_exist_in_payload(self):
        try:
            from tests.test_sp_portfolio_summary import SummaryTest, call
        except ImportError:  # pragma: no cover
            from test_sp_portfolio_summary import SummaryTest, call
        src = _strip_comments(_read("HedgeFund", "Tiles", "HedgeFundTileSummary.cs"))
        wanted = set(re.findall(r'(?:GetProperty|TryGetProperty)\("([a-z_]+)"', src))
        self.assertTrue(wanted)
        keys = set()

        class Probe(SummaryTest):
            def runTest(inner):
                from datetime import timedelta
                now = inner.clock()
                inner._point(now - timedelta(days=1), 100000.0)
                inner._point(now - timedelta(minutes=5), 100500.0)
                _st, payload = call()

                def walk(x):
                    if isinstance(x, dict):
                        keys.update(x)
                        for v in x.values():
                            walk(v)
                    elif isinstance(x, list):
                        for v in x:
                            walk(v)
                walk(payload)

        result = unittest.TestResult()
        Probe().run(result)
        self.assertEqual(result.errors + result.failures, [])
        keys.add("mode")                      # stamped by routes for MODE_PATHS (sandbox profile)
        self.assertEqual(sorted(wanted - keys), [])

if __name__ == "__main__":
    unittest.main()
