"""Licence clean-up (lead 2026-10-25): the Council parity fixture is SYNTHETIC and generated at test time (same shape,
seeded random walk, BTC 7 days a week, realistic volatility, deterministic); the real-data variant reads only the
user's local git-ignored export (skipped with a clear reason when absent); the export tool works on a COPY of
hedgefund.db (sha256 before = after, source priority, last N bars); HedgeFund/tools/check_repo_data.py finds data
files, price series, Alpaca records, IBANs, account numbers, the TR import folder and vendor files that differ from
their MANIFEST — and passes on the real tree (when this is a git checkout). Offline."""
import importlib.util
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from statistics import pstdev
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(TESTS)
HF = os.path.dirname(BACKEND)
sys.path.insert(0, BACKEND)
sys.path.insert(0, TESTS)

import council_parity_synth_bars as synth  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EXPORT = _load("export_council_parity_bars_t", os.path.join(HF, "tools", "export_council_parity_bars.py"))
CHECK = _load("check_repo_data_t", os.path.join(HF, "tools", "check_repo_data.py"))


class SyntheticBarsTest(unittest.TestCase):
    def test_shape_and_determinism(self):
        doc = synth.generate()
        self.assertEqual(doc, synth.generate())                       # deterministic
        self.assertIn("SYNTHETIC", doc["_about"])
        self.assertEqual(sorted(doc["symbols"]), sorted(["SPY", "QQQ", "TLT", "GLD", "BTC-USD"]))
        for sym, v in doc["symbols"].items():
            bars = v["bars"]
            self.assertEqual((v["source"], len(bars), bars[-1][0]), (["synthetic"], 260, "2026-10-23"), sym)
            days = [date.fromisoformat(b[0]) for b in bars]
            if sym == "BTC-USD":
                self.assertEqual(days, [days[0] + timedelta(days=i) for i in range(260)])      # 7 days a week
                self.assertTrue(any(d.weekday() >= 5 for d in days))
            else:
                self.assertTrue(all(calendars.is_trading_day(d) for d in days))
                self.assertEqual(days, calendars.trading_days(days[0], days[-1]))
            for d, o, h, l, c, vol in bars:
                self.assertTrue(l <= min(o, c) and max(o, c) <= h and vol > 0, (sym, d))
            rets = [math.log(bars[i][4] / bars[i - 1][4]) for i in range(1, len(bars))]
            annual = pstdev(rets) * math.sqrt(365 if sym == "BTC-USD" else 252)
            target = synth.PARAMS[sym][3]
            self.assertTrue(0.6 * target < annual < 1.5 * target, (sym, annual))      # realistic volatility
        self.assertNotEqual(synth.generate(seed=2)["symbols"]["SPY"]["bars"], doc["symbols"]["SPY"]["bars"])

    def test_testkit_reads_real_bars_only_from_the_local_folder(self):
        import council_parity_testkit as kit
        self.assertEqual(os.path.relpath(kit.REAL_BARS, BACKEND).replace(os.sep, "/"),
                         "data/local_fixtures/council_parity_real_bars.json")
        self.assertFalse(os.path.exists(os.path.join(TESTS, "council_parity_real_bars.json")))
        self.assertIn("HedgeFund/tools/export_council_parity_bars.py", kit.REAL_BARS_MISSING)
        self.assertEqual(os.path.abspath(EXPORT.DEFAULT_OUT), os.path.abspath(kit.REAL_BARS))
        world = kit.SyntheticBarsWorld(lambda: None)
        self.assertEqual(len(world.real["SPY"]), 260)
        with open(os.path.join(TESTS, "test_s10_council_parity_scenarios.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("@unittest.skipUnless(kit.real_bars_available(), kit.REAL_BARS_MISSING)", src)


class ExportToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="parity-export-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "hedgefund.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE bars (symbol TEXT, timeframe TEXT, ts TEXT, open REAL, high REAL, low REAL, "
                     "close REAL, volume REAL, vwap REAL, trade_count INTEGER, source TEXT, known_at TEXT, "
                     "fetched_at TEXT, adjustment TEXT NOT NULL DEFAULT 'all')")
        doc = synth.generate()
        for sym, v in doc["symbols"].items():
            for d, o, h, l, c, vol in v["bars"]:
                ts = f"{d}T04:00:00Z" if sym != "BTC-USD" else f"{d}T00:00:00Z"
                for src, bump in (("alpaca", 0.0), ("tiingo", 1.0), ("alpaca_sip", 0.5)):
                    if sym == "BTC-USD" and src != "alpaca":
                        continue
                    conn.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,'all')",
                                 (sym, "1Day", ts, o + bump, h + bump, l, c + bump, vol, src, ts, ts))
                conn.execute("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,'split')",
                             (sym, "1Day", ts, 1, 1, 1, 1, 1, "alpaca_sip", ts, ts))
        conn.commit()
        conn.close()
        self.out = os.path.join(self.tmp, "local_fixtures", "council_parity_real_bars.json")

    def test_export_from_a_copy(self):
        before = EXPORT.sha256_file(self.db)
        opened = []
        real_connect = sqlite3.connect
        with mock.patch.object(EXPORT.sqlite3, "connect", side_effect=lambda p, *a, **k: (opened.append(p),
                                                                                         real_connect(p, *a, **k))[1]):
            doc = EXPORT.export(self.db, self.out, date(2026, 10, 23), 200)
        self.assertEqual(EXPORT.sha256_file(self.db), before)
        self.assertTrue(opened and all(os.path.abspath(p) != os.path.abspath(self.db) for p in opened))
        self.assertEqual(doc["source_sha256"], before)
        spy = doc["symbols"]["SPY"]
        self.assertEqual((len(spy["bars"]), spy["bars"][-1][0], spy["source"]), (200, "2026-10-23", ["alpaca_sip"]))
        ref = {b[0]: b for b in synth.generate()["symbols"]["SPY"]["bars"]}
        last = spy["bars"][-1]
        self.assertAlmostEqual(last[4], ref[last[0]][4] + 0.5)          # alpaca_sip wins, the 'split' series ignored
        self.assertEqual(doc["symbols"]["BTC-USD"]["source"], ["alpaca"])
        with open(self.out, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["symbols"]["QQQ"]["bars"], doc["symbols"]["QQQ"]["bars"])

    def test_refusals(self):
        def changing_copy(src, dst):
            shutil.copy2(src, dst)
            with open(src, "ab") as fh:
                fh.write(b"\0")
        with self.assertRaises(EXPORT.DatabaseChanged):
            EXPORT.export(self.db, self.out, date(2026, 10, 23), 200, copy=changing_copy)
        with self.assertRaises(ValueError):
            EXPORT.export(self.db, self.out, date(2026, 10, 23), 400)       # not enough bars
        self.assertEqual(EXPORT.main(["--db", os.path.join(self.tmp, "missing.db"), "--out", self.out]), 2)
        self.assertFalse(os.path.exists(self.out))


class CheckRepoDataTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="repo-data-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def put(self, rel, data):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb" if isinstance(data, bytes) else "w", **({} if isinstance(data, bytes) else
                                                                        {"encoding": "utf-8"})) as fh:
            fh.write(data)
        return rel

    def rules(self, paths):
        return {(p, r) for p, r, _d in CHECK.scan(self.root, paths)}

    def test_rules(self):
        series = [{"date": f"2026-01-{i % 28 + 1:02d}", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                   "volume": 10} for i in range(25)]
        paths = [
            self.put("a/data.db", b"SQLite format 3\0"), self.put("a/prices.csv", "d,c\n"),
            self.put("a/series.json", json.dumps(series)), self.put("a/nums.json", json.dumps(list(range(60)))),
            self.put("a/alpaca.json", json.dumps({"bars": [{"t": "x", "o": 1, "h": 2, "l": 0, "c": 1, "v": 3}]})),
            self.put("a/rows.py", "ROWS = [\n" + "".join(f"  [1.{i}, 2.0, 3.0, 4.5],\n" for i in range(25)) + "]\n"),
            self.put("a/rec.py", 'X = {"t": "2026-01-02", "o": 1.0, "h": 2, "l": 0.5, "c": 1.5, "v": 9}\n'),
            self.put("a/code.py", 'def bar(t, o, h, l, c, v):\n    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}\n'),
            self.put("a/iban.txt", "pay to DE89 3704 0044 0532 0130 00 please\n"),
            self.put("a/notiban.txt", "DE89 3704 0044 0532 0130 01 is not valid\n"),
            self.put("a/acct.txt", "account number: 123456789\nPA3XYZ123456\n"),
            self.put("backend/data/backups/tr-import/x.md", "export notes\n"),
            self.put("ok/readme.txt", "just text 1, 2, 3\n"),
        ]
        found = self.rules(paths)
        expected = {("a/data.db", "data.file"), ("a/prices.csv", "data.file"), ("a/series.json", "price.series"),
                    ("a/nums.json", "price.series"), ("a/alpaca.json", "provider.alpaca"),
                    ("a/rows.py", "price.series"), ("a/rec.py", "provider.alpaca"), ("a/iban.txt", "personal.iban"),
                    ("a/acct.txt", "personal.account"), ("backend/data/backups/tr-import/x.md", "personal.tr_import")}
        self.assertEqual(found, expected)

    def test_vendor_manifest(self):
        good = self.put("m/vendor/lib.js", "console.log(1)\n")
        big = self.put("m/vendor/data.json", json.dumps(list(range(500))))       # vendored data: only the manifest
        digest = CHECK.sha256_file(os.path.join(self.root, good))
        dgst2 = CHECK.sha256_file(os.path.join(self.root, big))
        man = self.put("m/vendor/MANIFEST", f"# c\nlib.js 1.0 https://x {digest} 2026\ndata.json 1 https://y {dgst2} 2026\n")
        readme = self.put("m/vendor/README.txt", "about\n")
        self.assertEqual(self.rules([good, big, man, readme]), set())
        self.put("m/vendor/lib.js", "console.log(1)\r\n")                              # converted line endings
        extra = self.put("m/vendor/extra.js", "x\n")
        self.assertEqual(self.rules([good, big, man, readme, extra]),
                         {("m/vendor/lib.js", "vendor.manifest"), ("m/vendor/extra.js", "vendor.manifest")})
        self.assertEqual(self.rules([self.put("n/vendor/z.js", "z\n")]), {("n/vendor/z.js", "vendor.manifest")})

    def test_iban_checksum(self):
        self.assertTrue(CHECK.iban_valid("DE89370400440532013000"))
        self.assertTrue(CHECK.iban_valid("GB33 BUKB 2020 1555 5555 55"))
        self.assertFalse(CHECK.iban_valid("DE89370400440532013001"))

    def test_allow_list(self):
        allow = os.path.join(self.root, "allow.json")
        with open(allow, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "entries": [
                {"path": "a/iban.txt", "rule": "personal.iban", "reason": "invented"},
                {"path": "gone.txt", "rule": "data.file", "reason": "old"}]}, fh)
        entries = CHECK.load_allow(allow)
        remaining, stale = CHECK.apply_allow([("a/iban.txt", "personal.iban", "x"), ("b", "data.file", "y")], entries)
        self.assertEqual((remaining, [e["path"] for e in stale]), ([("b", "data.file", "y")], ["gone.txt"]))
        with open(allow, "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "entries": [{"path": "x", "rule": "made.up", "reason": "r"}]}, fh)
        with self.assertRaises(CHECK.UsageError):
            CHECK.load_allow(allow)
        self.assertEqual(CHECK.load_allow(os.path.join(self.root, "none.json")), [])
        with open(CHECK.ALLOW_FILE, encoding="utf-8") as fh:
            shipped = json.load(fh)
        self.assertTrue(all(e["reason"].strip() and e["rule"] in CHECK.RULES for e in shipped["entries"]))

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_commit_set_from_git(self):
        def git(*a):
            subprocess.run(["git", "-C", self.root, *a], check=True, capture_output=True)
        git("init", "-q")
        self.put(".gitignore", "data/\n")
        self.put("tracked.py", "x = 1\n")
        self.put("new.py", "y = 2\n")
        self.put("data/secret.db", b"\0")
        git("add", "tracked.py", ".gitignore")
        self.assertEqual(CHECK.commit_set(self.root), [".gitignore", "new.py", "tracked.py"])   # ignored data/ left out


class RealTreeTest(unittest.TestCase):
    def test_current_tree_is_clean(self):
        root = os.path.dirname(os.path.dirname(HF))
        if not shutil.which("git") or not os.path.isdir(os.path.join(root, ".git")):
            self.skipTest("not a git checkout of the repository (e.g. a code bundle) — run "
                          "'python HedgeFund/tools/check_repo_data.py' in the repository")
        self.assertEqual(CHECK.main(["--root", root]), 0)


if __name__ == "__main__":
    unittest.main()
