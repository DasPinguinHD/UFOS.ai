"""strategy_platform/core/calendars.py (NYSE + crypto sessions, moved from
fund/market.py) and core/clock.py. Offline, stdlib unittest.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_calendars -v
"""
import unittest
from datetime import date, datetime, timezone

from strategy_platform.core import calendars as C
from strategy_platform.core.clock import Clock, FixedClock, iso, resolve, SYSTEM_CLOCK, to_utc
from strategy_platform.core.errors import UserFacingError


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class HolidayTablesTest(unittest.TestCase):
    # Official NYSE trading-day counts per year.
    TRADING_DAYS = {2016: 252, 2017: 251, 2018: 251, 2019: 252, 2020: 253, 2021: 252, 2022: 251, 2023: 250,
                    2024: 252, 2025: 250, 2026: 251, 2027: 251}

    def test_trading_day_counts(self):
        for year, n in self.TRADING_DAYS.items():
            self.assertEqual(len(C.trading_days(f"{year}-01-01", f"{year}-12-31")), n, year)

    def test_tables_are_weekdays_inside_the_covered_years(self):
        for d in C.NYSE_HOLIDAYS | C.NYSE_EARLY_CLOSES:
            self.assertLess(d.weekday(), 5, d)
            self.assertIn(d.year, C.CALENDAR_YEARS)
        self.assertFalse(C.NYSE_HOLIDAYS & C.NYSE_EARLY_CLOSES)
        for year in (2026, 2027):
            self.assertEqual(len([d for d in C.NYSE_HOLIDAYS if d.year == year]), 10, year)

    def test_known_days(self):
        for d in ("2018-12-05", "2025-01-09", "2022-06-20", "2021-12-24", "2026-04-03", "2026-07-03",
                  "2027-06-18", "2027-12-24"):
            self.assertFalse(C.is_trading_day(d), d)
        self.assertTrue(C.is_trading_day("2027-12-31"))      # Jan 1 2028 is a Saturday: no closure
        self.assertTrue(C.is_trading_day("2021-12-31"))
        self.assertFalse(C.is_trading_day("2026-09-26"))      # Saturday
        self.assertTrue(C.is_early_close("2025-12-24"))
        self.assertFalse(C.is_early_close("2026-07-02"))

    def test_outside_the_calendar_raises_instead_of_guessing(self):
        for d in ("1999-12-31", "2031-01-02", date(1995, 5, 5)):
            with self.assertRaises(C.CalendarUnavailable) as cm:
                C.is_trading_day(d)
            self.assertIsInstance(cm.exception, UserFacingError)
            self.assertIn("no trading days are guessed", cm.exception.user_message)
        self.assertFalse(C.is_covered("2031-01-02"))
        self.assertTrue(C.is_covered("2028-01-03"))                # S10-H: covered through 2030
        self.assertTrue(C.is_covered("2016-01-04"))
        self.assertTrue(C.is_covered("2000-01-03"))
        with self.assertRaises(C.CalendarUnavailable):
            C.previous_trading_day("2000-01-03")              # the day before lies in 1999
        with self.assertRaises(C.CalendarUnavailable):
            C.next_trading_day("2030-12-31")                  # the next day is in 2031


class RuleYearsTest(unittest.TestCase):
    """2000-2015: rule-based holidays + special closures (exchange_calendars XNYS rules)."""
    # Official NYSE session counts (2001: Sept 11-14 closed; 2012: Hurricane Sandy).
    TRADING_DAYS = {2000: 252, 2001: 248, 2004: 252, 2007: 251, 2008: 253, 2012: 250, 2015: 252}

    def test_trading_day_counts(self):
        for year, n in self.TRADING_DAYS.items():
            self.assertEqual(len(C.trading_days(f"{year}-01-01", f"{year}-12-31")), n, year)

    def test_special_closures_and_rules(self):
        for d in ("2001-09-11", "2001-09-14", "2004-06-11", "2007-01-02", "2012-10-29", "2012-10-30",
                  "2000-01-17", "2008-03-21", "2006-01-02", "2015-07-03", "2004-12-24", "2010-12-24"):
            self.assertFalse(C.is_trading_day(d), d)
        for d in ("2001-09-17", "2000-01-03", "2010-12-31", "2011-12-30", "2004-06-10", "2012-10-31"):
            self.assertTrue(C.is_trading_day(d), d)
        for d in ("2002-07-05", "2003-12-26", "2013-07-03", "2008-11-28", "2009-12-24"):
            self.assertTrue(C.is_early_close(d), d)
        for d in ("2002-07-03", "2013-07-05", "2004-12-23"):
            self.assertFalse(C.is_early_close(d), d)
        self.assertEqual(C.session_bounds("2002-07-05")[1], utc(2002, 7, 5, 17, 0))

    def test_rules_reproduce_the_hardcoded_years(self):
        extra = {"2018-12-05", "2025-01-09"}                     # days of mourning (not rule-based)
        for year in range(2016, 2028):
            rules = {d.isoformat() for d in C.rule_holidays(year)}
            table = {d.isoformat() for d in C.NYSE_HOLIDAYS if d.year == year}
            juneteenth = {d for d in table - rules if d[5:7] == "06" and year >= 2022}
            self.assertEqual(table - rules - extra - juneteenth, set(), year)
            self.assertEqual(rules - table, set(), year)


class SessionTest(unittest.TestCase):
    """Same expectations as tests/test_fund_market.py CalendarTest (the code moved here)."""

    def test_regular_session_and_weekend(self):
        s = C.session_state(utc(2026, 9, 28, 14, 0))
        self.assertEqual((s["open"], s["session"], s["trading_day"]), (True, "regular", "2026-09-28"))
        self.assertEqual(s["next_close"], "2026-09-28T20:00:00+00:00")
        self.assertEqual(s["next_open"], "2026-09-29T13:30:00+00:00")
        s = C.session_state(utc(2026, 9, 26, 15, 0))
        self.assertEqual((s["open"], s["next_close"], s["trading_day"]), (False, None, "2026-09-28"))
        s = C.session_state(utc(2026, 9, 28, 20, 0))          # exactly at the close: closed
        self.assertEqual((s["open"], s["trading_day"]), (False, "2026-09-29"))
        self.assertTrue(C.is_open(utc(2026, 9, 28, 13, 30)))
        self.assertEqual(C.trading_day(utc(2026, 9, 25, 21, 0)), "2026-09-28")
        self.assertEqual(C.session_state("2026-09-28T14:00:00Z")["open"], True)   # ISO string input

    def test_dst_and_early_closes(self):
        self.assertFalse(C.is_open(utc(2026, 3, 6, 13, 45)))
        self.assertTrue(C.is_open(utc(2026, 3, 9, 13, 45)))
        self.assertEqual(C.session_bounds("2026-11-02")[0], utc(2026, 11, 2, 14, 30))
        self.assertEqual(C.session_state(utc(2026, 12, 24, 15))["next_close"], "2026-12-24T18:00:00+00:00")
        self.assertEqual(C.session_bounds("2019-07-03")[1], utc(2019, 7, 3, 17, 0))   # 13:00 EDT
        self.assertIsNone(C.session_bounds("2026-12-25"))

    def test_counting(self):
        self.assertEqual(C.trading_days_between("2026-09-25", "2026-09-28"), 1)
        self.assertEqual(C.trading_days_between("2026-09-28", "2026-09-25"), 0)
        self.assertEqual(C.trading_days_between("2026-12-21", "2027-01-04"), 8)
        self.assertEqual(C.trading_days_between(utc(2026, 9, 28, 14), utc(2026, 9, 29, 1)), 0)
        self.assertEqual(C.next_trading_day("2026-12-24"), date(2026, 12, 28))
        self.assertEqual(C.previous_trading_day("2026-09-08"), date(2026, 9, 4))
        self.assertEqual(C.trading_days("2026-09-04", "2026-09-08"), [date(2026, 9, 4), date(2026, 9, 8)])
        self.assertEqual(C.trading_days("2026-09-08", "2026-09-04"), [])

    def test_last_close_before(self):
        self.assertEqual(C.last_close_before(utc(2026, 9, 26, 12)), utc(2026, 9, 25, 20))
        self.assertEqual(C.last_close_before(utc(2026, 9, 28, 20, 1)), utc(2026, 9, 28, 20))


class CryptoTest(unittest.TestCase):
    SAT = utc(2026, 10, 3, 9, 30)

    def test_tradable_and_funding(self):
        self.assertTrue(C.is_crypto("BTC-USD"))
        self.assertTrue(C.is_crypto("eth-usd"))
        self.assertFalse(C.is_crypto("SPY"))
        self.assertFalse(C.is_crypto(None))
        self.assertTrue(C.is_tradable("BTC-USD", self.SAT))
        self.assertFalse(C.is_tradable("SPY", self.SAT))
        self.assertEqual(C.last_funding_window(self.SAT), self.SAT.replace(hour=8, minute=0))
        w = C.funding_windows_between(utc(2026, 10, 2, 8), self.SAT)
        self.assertEqual(w, [utc(2026, 10, 2, 16), utc(2026, 10, 3, 0), utc(2026, 10, 3, 8)])
        self.assertEqual(C.funding_windows_between(self.SAT, self.SAT), [])
        self.assertEqual(C.utc_days_between(utc(2026, 10, 3, 23), utc(2026, 10, 4, 1)), 1)
        self.assertEqual(C.utc_days_between("2026-10-04", "2026-10-03"), 0)

    def test_reexport_list_is_complete(self):
        for name in C.__all__:
            self.assertTrue(hasattr(C, name), name)


class ClockTest(unittest.TestCase):
    def test_fixed_clock(self):
        c = FixedClock("2026-10-02T14:00:00Z")
        self.assertEqual(c.now_iso(), "2026-10-02T14:00:00+00:00")
        c.advance(minutes=30)
        self.assertEqual(c.now_utc(), utc(2026, 10, 2, 14, 30))
        c.set(datetime(2026, 10, 3, 1, 0))                    # naive = UTC
        self.assertEqual(c.now_utc().tzinfo, timezone.utc)
        self.assertEqual(c.now_utc(), utc(2026, 10, 3, 1))

    def test_system_clock_and_helpers(self):
        self.assertEqual(Clock().now_utc().tzinfo, timezone.utc)
        self.assertIs(resolve(None), SYSTEM_CLOCK)
        c = FixedClock()
        self.assertIs(resolve(c), c)
        self.assertEqual(iso(datetime(2026, 1, 1, 12, 0, 0, 500)), "2026-01-01T12:00:00+00:00")
        self.assertEqual(to_utc("2026-01-01T13:00:00+01:00"), utc(2026, 1, 1, 12))
        with self.assertRaises(TypeError):
            to_utc(12345)
        with self.assertRaises(ValueError):
            to_utc("not a time")


if __name__ == "__main__":
    unittest.main()
