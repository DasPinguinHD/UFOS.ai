"""Offline tests for the Economic Calendar section (newsroom/calendar_collector.py):
release-date filtering, FOMC window, event building and one full poll with the
FRED fetch function monkeypatched. Stdlib unittest only; no network, no .env.

Run from HedgeFund/backend:  python -m unittest tests.test_calendar -v

aiohttp, config and the newsroom package are stubbed so the module under test
loads without the backend's dependencies or its package __init__.
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest
from datetime import date, timedelta

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NEWSROOM = os.path.join(_BACKEND, "newsroom")


def _load_module():
    names = ("aiohttp", "config", "newsroom", "newsroom.calendar_collector")
    saved = {name: sys.modules.get(name) for name in names}
    try:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientSession = object
        aiohttp.ClientTimeout = lambda **kw: None
        sys.modules["aiohttp"] = aiohttp

        config = types.ModuleType("config")
        config.FRED_API_KEY = "test"
        config.CALENDAR_POLL_SECONDS = 60
        sys.modules["config"] = config

        pkg = types.ModuleType("newsroom")
        pkg.__path__ = [_NEWSROOM]
        sys.modules["newsroom"] = pkg

        spec = importlib.util.spec_from_file_location(
            "newsroom.calendar_collector", os.path.join(_NEWSROOM, "calendar_collector.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["newsroom.calendar_collector"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for name, m in saved.items():
            if m is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = m


cal = _load_module()

# Release ids as FRED has them (used only as fixtures here).
RELEASES = {"CPIAUCSL": 10, "PAYEMS": 50, "PCEPILFE": 54, "GDPC1": 53, "ICSA": 180,
            "RSAFS": 9, "INDPRO": 13, "JTSJOL": 192, "PPIFIS": 46, "UMCSENT": 91}


class PureHelperTests(unittest.TestCase):
    def test_select_release_dates_filters_ids_and_window(self):
        start, end = date(2026, 9, 25), date(2026, 11, 9)
        rows = [
            {"release_id": 10, "release_name": "CPI", "date": "2026-10-14"},
            {"release_id": 10, "release_name": "CPI", "date": "2026-10-14"},   # duplicate
            {"release_id": 10, "release_name": "CPI", "date": "2026-09-24"},   # before window
            {"release_id": 10, "release_name": "CPI", "date": "2026-11-12"},   # after window
            {"release_id": 999, "release_name": "Other", "date": "2026-10-01"},  # not curated
            {"release_id": "50", "release_name": "Jobs", "date": "2026-10-02"},  # id as string
            {"release_id": 50, "date": "garbage"},
            {"date": "2026-10-02"},
        ]
        got = cal.select_release_dates(rows, {10, 50}, start, end)
        self.assertEqual(got, {10: [date(2026, 10, 14)], 50: [date(2026, 10, 2)]})

    def test_select_release_dates_inclusive_bounds(self):
        d = date(2026, 9, 25)
        rows = [{"release_id": 10, "date": "2026-09-25"}, {"release_id": 10, "date": "2026-11-09"}]
        self.assertEqual(cal.select_release_dates(rows, [10], d, date(2026, 11, 9))[10],
                         [date(2026, 9, 25), date(2026, 11, 9)])

    def test_latest_observation_skips_missing(self):
        obs = [{"date": "2026-09-01", "value": "."}, {"date": "2026-08-01", "value": "3.35"}]
        self.assertEqual(cal.latest_observation(obs), ("2026-08-01", 3.35))
        self.assertIsNone(cal.latest_observation([]))

    def test_period_labels(self):
        self.assertEqual(cal.period_label("2026-08-01", "m"), "Aug 2026")
        self.assertEqual(cal.period_label("2026-04-01", "q"), "Q2 2026")
        self.assertEqual(cal.period_label("2026-09-20", "w"), "week of Sep 20, 2026")

    def test_format_last(self):
        self.assertEqual(cal.format_last(3.3456, "% YoY"), "3.35% YoY")
        self.assertEqual(cal.format_last(142, "K m/m"), "+142K m/m")
        self.assertEqual(cal.format_last(-33, "K m/m"), "-33K m/m")
        self.assertEqual(cal.format_last(231.0, "K"), "231K")
        self.assertEqual(cal.format_last(7.2, "M"), "7.20M")
        self.assertEqual(cal.format_last(55.14, "index"), "55.1")

    def test_fomc_window(self):
        ev = cal.fomc_events(date(2026, 9, 25), date(2026, 11, 9))
        self.assertEqual([e["date"] for e in ev], ["2026-10-28"])
        self.assertFalse(ev[0]["sep"])
        self.assertEqual(ev[0]["time_hint"], "14:00 ET")
        self.assertEqual(ev[0]["meeting_start"], "2026-10-27")
        ev = cal.fomc_events(date(2026, 11, 1), date(2026, 12, 16))
        self.assertEqual([(e["date"], e["sep"]) for e in ev], [("2026-12-09", True)])

    def test_fomc_list_sane(self):
        for first, second, _ in cal.FOMC_MEETINGS:
            self.assertEqual(date.fromisoformat(second) - date.fromisoformat(first), timedelta(days=1))
        self.assertEqual(sum(1 for m in cal.FOMC_MEETINGS if m[2]), 8)  # 4 SEP meetings a year

    def test_build_events_dedups_shared_release(self):
        releases = {"CPIAUCSL": {"id": 10, "name": "CPI"}, "UMCSENT": {"id": 10, "name": "CPI"}}
        d = date(2026, 10, 14)
        ev = cal.build_events(releases, {10: [d]}, {"CPIAUCSL": ("2026-08-01", 3.35)}, d, d)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["series_id"], "CPIAUCSL")
        self.assertEqual(ev[0]["last_display"], "3.35% YoY (Aug 2026)")
        self.assertEqual(ev[0]["release_link"], cal.release_page(10))

    def test_sort_events(self):
        ev = [
            {"date": "2026-10-02", "time_hint": "", "importance": "medium", "name": "B"},
            {"date": "2026-10-02", "time_hint": "08:30 ET", "importance": "medium", "name": "C"},
            {"date": "2026-10-02", "time_hint": "08:30 ET", "importance": "high", "name": "Z"},
            {"date": "2026-10-01", "time_hint": "10:00 ET", "importance": "medium", "name": "A"},
        ]
        self.assertEqual([e["name"] for e in cal.sort_events(ev)], ["A", "Z", "C", "B"])


class PollTests(unittest.TestCase):
    def setUp(self):
        cal._release_cache.clear()
        self._orig_fetch = cal._fetch_json
        self.calls = []
        today = date.today()
        self.today = today

        async def fake_fetch(session, endpoint, params):
            self.calls.append((endpoint, dict(params)))
            if endpoint == "series/release":
                sid = params["series_id"]
                if sid == "JTSJOL":
                    raise RuntimeError("HTTP 500")
                return {"releases": [{"id": RELEASES[sid], "name": f"Release {sid}", "link": "http://x"}]}
            if endpoint == "series/observations":
                if params["series_id"] == "GDPC1":
                    return {"observations": [{"date": "2026-04-01", "value": "."}]}
                return {"observations": [{"date": "2026-08-01", "value": "1500"}]}
            if endpoint == "releases/dates":
                return {"release_dates": [
                    {"release_id": 10, "release_name": "CPI", "date": (today + timedelta(days=3)).isoformat()},
                    {"release_id": 50, "release_name": "Jobs", "date": (today + timedelta(days=1)).isoformat()},
                    {"release_id": 50, "release_name": "Jobs", "date": (today + timedelta(days=60)).isoformat()},
                    {"release_id": 777, "release_name": "Other", "date": today.isoformat()},
                ]}
            if endpoint == "release/dates":
                if params["release_id"] == "180":
                    return {"release_dates": [{"release_id": 180, "date": today.isoformat()}]}
                return {"release_dates": []}
            raise AssertionError(endpoint)

        cal._fetch_json = fake_fetch

    def tearDown(self):
        cal._fetch_json = self._orig_fetch

    def _poll(self):
        emitted, statuses = [], []

        async def report_status(ok, msg):
            statuses.append((ok, msg))

        asyncio.run(cal._poll_once(None, emitted.append, report_status))
        return emitted, statuses

    def test_full_poll(self):
        emitted, statuses = self._poll()
        self.assertEqual(len(emitted), 1)
        payload = emitted[0]
        events = payload["events"]
        releases = [e for e in events if e["kind"] == "release"]
        self.assertEqual({e["series_id"] for e in releases}, {"CPIAUCSL", "PAYEMS", "ICSA"})
        # sorted by date
        self.assertEqual([e["date"] for e in events], sorted(e["date"] for e in events))
        # everything within [today, today+45]
        end = self.today + timedelta(days=cal.CALENDAR_WINDOW_DAYS)
        for e in events:
            self.assertTrue(self.today.isoformat() <= e["date"] <= end.isoformat(), e)
        claims = next(e for e in releases if e["series_id"] == "ICSA")  # found via the fallback
        self.assertEqual(claims["last_display"], "2K (week of Aug 1, 2026)")  # 1500 persons * 0.001
        jobs = next(e for e in releases if e["series_id"] == "PAYEMS")
        self.assertEqual(jobs["time_hint"], "08:30 ET")
        self.assertEqual(jobs["last_display"], "+1,500K m/m (Aug 2026)")
        # request parameters
        rd = next(p for ep, p in self.calls if ep == "releases/dates")
        self.assertEqual(rd["include_release_dates_with_no_data"], "true")
        self.assertEqual(rd["realtime_start"], self.today.isoformat())
        self.assertEqual(rd["realtime_end"], end.isoformat())
        units = {p["series_id"]: p.get("units") for ep, p in self.calls if ep == "series/observations"}
        self.assertEqual(units["CPIAUCSL"], "pc1")
        self.assertEqual(units["PCEPILFE"], "pc1")
        self.assertEqual(units["PAYEMS"], "chg")
        self.assertIsNone(units["ICSA"])
        # the failed series is reported, the poll still counts as ok
        self.assertTrue(any("JTSJOL" in e for e in payload["errors"]))
        self.assertTrue(statuses[-1][0])

    def test_release_ids_are_cached(self):
        self._poll()
        first = sum(1 for ep, _ in self.calls if ep == "series/release")
        self.calls.clear()
        self._poll()
        second = sum(1 for ep, _ in self.calls if ep == "series/release")
        self.assertEqual(first, len(cal.CURATED))
        self.assertEqual(second, 1)  # only the one that failed is retried


if __name__ == "__main__":
    unittest.main()
