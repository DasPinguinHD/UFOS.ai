"""OS2 calendar (SP §21.12): monthly / quarterly / VIX expirations for 2025 and 2026 against dates computed by hand
from the published OCC / Cboe rules (3rd Friday, holiday → the trading day before; VIX = 30 days before the next
month's SPX expiration), and the event features with their point-in-time rule."""
import unittest
from datetime import date, datetime, timezone

from strategy_platform.options_structure import calendar as cal

MONTHLY = {2025: ["01-17", "02-21", "03-21", "04-17", "05-16", "06-20", "07-18", "08-15", "09-19", "10-17", "11-21",
                  "12-19"],
           2026: ["01-16", "02-20", "03-20", "04-17", "05-15", "06-18", "07-17", "08-21", "09-18", "10-16", "11-20",
                  "12-18"]}
# hand-computed: (next month's monthly expiration) − 30 days; Good Friday 2025-04-18 → SPX Thu 04-17 → VIX Tue 03-18;
# Juneteenth 2026-06-19 → SPX Thu 06-18 → VIX Tue 05-19
VIX = {2025: ["01-22", "02-19", "03-18", "04-16", "05-21", "06-18", "07-16", "08-20", "09-17", "10-22", "11-19", "12-17"],
       2026: ["01-21", "02-18", "03-18", "04-15", "05-19", "06-17", "07-22", "08-19", "09-16", "10-21", "11-18",
              "12-16"]}


def ms(d: date, h=15) -> int:
    return int(datetime(d.year, d.month, d.day, h, 0, tzinfo=timezone.utc).timestamp() * 1000)


class ExpirationTest(unittest.TestCase):
    def test_monthly(self):
        for y, days in MONTHLY.items():
            self.assertEqual([cal.monthly_expiration(y, m).isoformat()[5:] for m in range(1, 13)], days, y)

    def test_vix(self):
        for y, days in VIX.items():
            self.assertEqual([cal.vix_expiration(y, m).isoformat()[5:] for m in range(1, 13)], days, y)

    def test_quarterly_and_next(self):
        self.assertEqual(cal.next_quarterly(date(2026, 3, 21)), date(2026, 6, 18))
        self.assertEqual(cal.next_monthly(date(2026, 4, 17)), date(2026, 4, 17))       # on the day → 0 days
        self.assertEqual(cal.next_vix(date(2026, 5, 20)), date(2026, 6, 17))
        e = cal.expirations(date(2026, 6, 1), date(2026, 6, 30))
        self.assertEqual(e, [{"date": "2026-06-17", "kind": "vix", "quarterly": False},
                             {"date": "2026-06-18", "kind": "monthly", "quarterly": True}])


class FeatureTest(unittest.TestCase):
    def test_events_and_point_in_time(self):
        as_of = ms(date(2026, 11, 6))
        f = cal.features(as_of, ([date(2026, 12, 9)], None), ([date(2026, 11, 13), date(2026, 12, 10)], as_of - 1, None))
        self.assertEqual(f["monthly_expiration"], {"date": "2026-11-20", "days": 14})
        self.assertEqual(f["vix_expiration"], {"date": "2026-11-18", "days": 12})
        self.assertEqual(f["fomc"]["days"], 33)
        self.assertEqual(f["cpi"]["date"], "2026-11-13")
        self.assertEqual(f["next_event"]["name"], "cpi")
        # a calendar snapshot produced AFTER the as-of time is not used (point-in-time)
        f = cal.features(as_of, ([date(2026, 12, 9)], None), ([date(2026, 11, 13)], as_of + 1, None))
        self.assertIsNone(f["cpi"])
        self.assertTrue(any("point-in-time" in r for r in f["reasons"]))
        f = cal.features(as_of, ([], "FOMC schedule not loadable (ImportError)"), ([], None, "no snapshot"))
        self.assertIsNone(f["fomc"])
        self.assertTrue(any("ImportError" in r for r in f["reasons"]))
        self.assertEqual(f["next_event"]["name"], "vix_expiration")


if __name__ == "__main__":
    unittest.main()
