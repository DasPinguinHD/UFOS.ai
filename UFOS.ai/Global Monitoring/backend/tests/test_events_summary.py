"""GET /events/summary (contract GM_SUMMARY_API, 2026-10-24): counts per category in the window (the boundary
included), undated events, unknown categories appended, the fixed category order, the feed status per collector, the
strict ``hours`` parameter (never clamped), the response keys, and the pure-read guarantees (no collector wake-up,
no storage, token-protected). Offline; does not need aiohttp."""
import importlib.util
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import events_summary as es  # noqa: E402

NOW = datetime(2026, 10, 24, 17, 5, 0, tzinfo=timezone.utc)
COLLECTORS = ("news", "gdacs", "opensky", "yahoo")


def ago(**kw):
    return (NOW - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


class SummaryTest(unittest.TestCase):
    def test_counts_window_and_order(self):
        events = [
            {"id": "n1", "category": "geopolitics", "timestamp": ago(hours=1)},
            {"id": "n2", "category": "geopolitics", "timestamp": ago(hours=24)},            # exactly at `since`: counted
            {"id": "n3", "category": "geopolitics", "timestamp": ago(hours=24, seconds=1)},  # just outside
            {"id": "d1", "category": "disaster", "timestamp": ago(days=3)},
            {"id": "f1", "category": "flight", "timestamp": ago(days=2), "observed_at": ago(minutes=2)},  # observed_at wins
            {"id": "m1", "category": "markets", "timestamp": (NOW - timedelta(hours=2)).timestamp()},     # unix seconds
            {"id": "u1", "category": "geopolitics"},                                       # no time -> undated
            {"id": "u2", "category": "flight", "timestamp": "not a time"},
            {"id": "x1", "category": "volcano", "timestamp": ago(hours=3)},                # unknown -> appended
            {"id": "x2", "timestamp": ago(hours=3)},                                       # no category -> "unknown"
        ]
        out = es.summarize(events, {}, COLLECTORS, 24, now=NOW)
        self.assertEqual(list(out), ["schema", "as_of", "window_hours", "since", "total", "undated", "categories",
                                     "sources"])
        self.assertEqual((out["schema"], out["as_of"], out["since"], out["window_hours"]),
                         (1, "2026-10-24T17:05:00Z", "2026-10-23T17:05:00Z", 24))
        self.assertEqual([(c["category"], c["source"], c["count"]) for c in out["categories"]], [
            ("geopolitics", "news", 2), ("disaster", "gdacs", 0), ("flight", "opensky", 1), ("markets", "yahoo", 1),
            ("unknown", None, 1), ("volcano", None, 1)])
        self.assertEqual(out["undated"], 2)
        self.assertEqual(out["total"], 6)
        self.assertEqual(es.summarize(events, {}, COLLECTORS, 72, now=NOW)["categories"][1]["count"], 1)

    def test_sources_status(self):
        status = {"news": {"source": "news", "ok": True, "message": "42 headlines", "timestamp": "2026-10-24T17:01:12Z"},
                  "opensky": {"source": "opensky", "ok": False, "message": "credentials missing", "timestamp": None,
                              "cached": False, "extra": "not passed on"},
                  "gdacs": {"source": "gdacs", "ok": True, "message": "from cache (updated 18:00) — refreshing…",
                            "timestamp": "2026-10-24T16:00:00Z", "cached": True}}
        out = es.summarize([], status, COLLECTORS, now=NOW)
        self.assertEqual([s["source"] for s in out["sources"]], list(COLLECTORS))
        self.assertEqual(out["sources"][0], {"source": "news", "ok": True, "message": "42 headlines",
                                             "timestamp": "2026-10-24T17:01:12Z", "cached": False})
        self.assertEqual(out["sources"][1]["cached"], True)
        self.assertEqual(set(out["sources"][2]), {"source", "ok", "message", "timestamp", "cached"})
        self.assertIs(out["sources"][2]["ok"], False)
        self.assertEqual(out["sources"][3], {"source": "yahoo", "ok": None, "message": "starting…", "timestamp": None,
                                             "cached": False})
        self.assertEqual(out["total"], 0)
        self.assertEqual([c["count"] for c in out["categories"]], [0, 0, 0, 0])   # all four always present

    def test_hours_parameter_strict(self):
        self.assertEqual(es.parse_hours(None), 24)
        self.assertEqual(es.parse_hours(""), 24)
        self.assertEqual(es.parse_hours("1"), 1)
        self.assertEqual(es.parse_hours(" 168 "), 168)
        for bad in ("0", "169", "-1", "+5", "2.5", "abc", "1e2", "٣", "99999"):
            with self.assertRaises(ValueError, msg=bad) as cm:
                es.parse_hours(bad)
            self.assertEqual(str(cm.exception), "hours must be an integer 1-168")


class PureReadTest(unittest.TestCase):
    def setUp(self):
        self.main = (BACKEND / "main.py").read_text(encoding="utf-8")

    def handler(self):
        m = re.search(r"\n    async def _events_summary\(request\):\n(.*?)\n    async def ", self.main, re.S)
        self.assertIsNotNone(m, "handler _events_summary not found in main.py")
        return m.group(1)

    def test_route_and_handler_have_no_side_effects(self):
        self.assertIn('add_get("/events/summary", _events_summary)', self.main)
        body = self.handler()
        for forbidden in ("_refresh_stale_sources", "request_refresh", "db_store", "persistence", "log_event",
                          "broadcast", "assess", "aiohttp.ClientSession", "await "):
            self.assertNotIn(forbidden, body, forbidden)
        self.assertIn("events_summary.summarize(store.snapshot(), source_status, COLLECTORS, hours)", body)
        self.assertIn("status=400", body)

    def test_module_imports_nothing_with_side_effects(self):
        src = (BACKEND / "events_summary.py").read_text(encoding="utf-8")
        imports = set(re.findall(r"^(?:import|from) (\w+)", src, re.M))
        self.assertEqual(imports, {"__future__", "re", "datetime", "typing", "storage"})
        self.assertNotRegex(src, r"storage\.(?!norm_ts|parse_iso)\w+\(")         # only the two pure helpers

    def test_token_protected(self):
        spec = importlib.util.spec_from_file_location("gm_api_auth_summary", BACKEND / "api_auth.py")
        auth = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(auth)
        self.assertNotIn("/events/summary", auth.PUBLIC_PATHS)


if __name__ == "__main__":
    unittest.main()
