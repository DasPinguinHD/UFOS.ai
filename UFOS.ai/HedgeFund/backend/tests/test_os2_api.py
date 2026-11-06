"""OS2 API (SP §21.12): ``/v2/options-structure/*`` shapes, the provisional flag on every payload, the state's age
rule (no fresh slot → not available with the reason), 400 / 404 / 409, the progress counter without H1 / H2."""
import asyncio
import json
import math
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from os2_testkit import OS2Case, build_chain, ny_ms  # noqa: E402

from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.options_structure import regime  # noqa: E402

D = date(2026, 11, 5)
SLOT = ny_ms(D, 10, 0)
PREV = calendars.previous_trading_day(D)


def bars(symbol, t):
    out, d = [], PREV
    for i in range(30):
        out.append((d, 600.0 * math.exp(0.004 * math.cos(i)), ny_ms(d, 16, 30)))
        d = calendars.previous_trading_day(d)
    return sorted(out)


class ApiTest(OS2Case):
    def setUp(self):
        super().setUp()
        from strategy_platform.api import routes
        self.routes = routes
        self.api = routes.V2Api()
        self.api.data_dir = self.tmp
        self.now = datetime.fromtimestamp((SLOT + 5 * 60000) / 1000, timezone.utc)
        self.api.now_fn = lambda: self.now
        a = self.arch
        for s in ("DTB3", "VIXCLS", "VXVCLS"):
            a.fred(s, PREV, {"DTB3": 4.0, "VIXCLS": 18.0, "VXVCLS": 20.0}[s], ny_ms(D, 8, 46))
        build_chain(a, D, ny_ms(D, 9, 55), oi_fn=lambda k, K, e: 3000 if k == "put" else 1000, label="09:55")
        build_chain(a, D, SLOT, oi_fn=lambda k, K, e: 3000 if k == "put" else 1000)
        a.run("intraday", ny_ms(D, 15, 25), status="late", label="15:25")      # a late fixed slot is a gap
        regime.tick(self.tmp, regime.Context(self.cfg, self.acfg, bars_fn=bars, fomc=([date(2026, 12, 9)], None),
                                             cpi=([], None, "no snapshot")))

    def call(self, name, query=None):
        status, payload = asyncio.run(getattr(self.api, name)(query or {}, self.routes.NO_BODY, {}))
        self.routes.dumps(payload)
        return status, payload

    def test_routes_registered(self):
        paths = {p for _m, p, _n, _b in self.routes.ROUTES}
        for p in ("state", "gex", "surface", "history", "calendar", "quality", "progress"):
            self.assertIn(f"/v2/options-structure/{p}", paths)
        self.assertTrue(all(m == "GET" for m, p, _n, _b in self.routes.ROUTES if "options-structure" in p))

    def test_state(self):
        st, p = self.call("options_structure_state", {"symbol": "SPY"})
        self.assertEqual(st, 200)
        self.assertTrue(p["available"], p)
        self.assertTrue(p["provisional"])
        self.assertEqual(p["slot"], datetime.fromtimestamp(SLOT / 1000, timezone.utc).isoformat().replace("+00:00", "Z"))
        self.assertEqual(p["age_minutes"], 5.0)
        self.assertEqual(p["regime"]["gex_sign"], -1)
        self.assertIn("dealer_variants", p["assumptions"])
        # as of 09:58 the 10:00 slot does not exist yet → the 09:55 one
        st, p = self.call("options_structure_state", {"symbol": "SPY", "as_of": str(ny_ms(D, 9, 58))})
        self.assertEqual(p["age_minutes"], 3.0)
        # an hour later: no fresh slot → not available with the reason (no live fallback)
        st, p = self.call("options_structure_state", {"symbol": "SPY", "as_of": str(SLOT + 3600000)})
        self.assertFalse(p["available"])
        self.assertIn("no fresh slot", p["reason"])
        self.assertNotIn("regime", p)
        st, p = self.call("options_structure_state", {"symbol": "QQQ"})
        self.assertFalse(p["available"])                                  # no QQQ chain archived
        self.assertTrue(p["reason"])

    def test_gex_surface_slots(self):
        st, p = self.call("options_structure_gex", {"symbol": "SPY", "slot": "latest"})
        self.assertEqual(st, 200)
        self.assertEqual(set(p["gex"]["buckets"]), {"0-7", "8-30", "31-45"})
        self.assertEqual(p["gex"]["total"]["status"], "ok")
        self.assertEqual(set(p["gex"]["flip_curve"]), {"spots", "A", "B", "C"})
        self.assertTrue(p["provisional"])
        iso = datetime.fromtimestamp(ny_ms(D, 9, 55) / 1000, timezone.utc).isoformat()
        st, p = self.call("options_structure_surface", {"symbol": "SPY", "slot": iso})
        self.assertEqual(st, 200)
        self.assertIn("expected_move", p["surface"])
        st, p = self.call("options_structure_gex", {"symbol": "SPY", "slot": str(ny_ms(D, 10, 30))})
        self.assertEqual((st, p["code"]), (404, "unknown_slot"))
        st, p = self.call("options_structure_gex", {"symbol": "SPY", "slot": "yesterday"})
        self.assertEqual(st, 400)
        st, p = self.call("options_structure_gex", {"symbol": "IWM"})
        self.assertEqual(st, 400)

    def test_history_quality_calendar_progress(self):
        st, p = self.call("options_structure_history", {"symbol": "SPY", "field": "gex_sign", "days": "5"})
        self.assertEqual(st, 200)
        self.assertEqual([x["value"] for x in p["points"]], [-1, -1])
        st, p = self.call("options_structure_history", {"symbol": "SPY", "field": "gex_total_A"})
        self.assertLess(p["points"][0]["value"], 0)
        self.assertEqual(self.call("options_structure_history", {"field": "r_last"})[0], 400)
        self.assertEqual(self.call("options_structure_history", {"field": "gex_sign", "days": "500"})[0], 400)
        st, p = self.call("options_structure_quality", {"symbol": "SPY", "days": "2"})
        self.assertEqual((st, p["summary"]["slots"]), (200, 2))
        self.assertIn("vendor_iv", p["slots"][0])
        st, p = self.call("options_structure_calendar", {"days": "60"})
        self.assertEqual(st, 200)
        self.assertIn({"date": "2026-11-20", "kind": "monthly", "quarterly": False}, p["expirations"])
        st, p = self.call("options_structure_progress")
        self.assertEqual(st, 200)
        self.assertEqual((p["target"], p["sessions_archived"], p["sessions_eligible"]), (120, 1, 0))
        self.arch.run("intraday", ny_ms(D, 15, 25), status="ok", label="15:25")   # a second, on-time 15:25 run
        self.assertEqual(self.call("options_structure_progress")[1]["sessions_eligible"], 1)
        text = json.dumps(p).lower()
        for banned in ('"h1', '"h2', "hit_rate", "p_value", "r_first", "r_last"):
            self.assertNotIn(banned, text)
        self.assertEqual(self.call("options_structure_progress", {"x": "1"})[0], 400)

    def test_progress_counts_sessions_regardless_of_the_archive_hash(self):
        # the archive's assumptions_hash changed (e.g. rate_series added) — sessions still count
        d2 = calendars.next_trading_day(D)
        for label, hh, mm in (("09:55", 9, 55), ("15:25", 15, 25)):
            self.arch.run("intraday", ny_ms(d2, hh, mm), label=label, assumptions_hash="new-hash")
        self.arch.run("intraday", ny_ms(D, 15, 25), status="ok", label="15:25", assumptions_hash="old-hash")
        st, p = self.call("options_structure_progress")
        self.assertEqual((p["sessions_archived"], p["sessions_eligible"]), (2, 2))

    def test_history_note_and_state_oi(self):
        st, p = self.call("options_structure_history", {"symbol": "SPY", "field": "gex_sign"})
        self.assertIn("restarts after a change", p["note"])
        st, p = self.call("options_structure_state", {"symbol": "SPY"})
        self.assertEqual(p["regime"]["oi"]["newest_valid_for"], PREV.isoformat())

    def test_bad_config_is_409(self):
        self.write_override("options_structure_config.json", {"spread_max_pct": -1})
        st, p = self.call("options_structure_state")
        self.assertEqual((st, p["code"]), (409, "config_invalid"))

    def test_bad_as_of(self):
        self.assertEqual(self.call("options_structure_state", {"as_of": "2026-11-05T10:00"})[0], 400)   # no offset
        self.assertEqual(self.call("options_structure_state", {"nope": "1"})[0], 400)


if __name__ == "__main__":
    unittest.main()
