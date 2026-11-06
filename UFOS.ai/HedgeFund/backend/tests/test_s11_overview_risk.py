"""Stage 11 Overview risk maths (SP §26.2, ``portfolio/overview_risk.py``): the RiskLab HS quantile convention
(re-implemented), ES as the tail mean, the ≥ 60-returns rule, end-of-day returns between consecutive trading days,
max drawdown and vol 20 — all hand-computed on synthetic series. No network, no database."""
import math
import os
import sys
import unittest
from datetime import date, datetime, time, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.portfolio import overview_risk as R  # noqa: E402

NY = calendars.NY


def close_ts(d: date) -> str:
    return datetime.combine(d, time(16, 5), tzinfo=NY).astimezone(timezone.utc).isoformat()


def series(returns, start=date(2026, 3, 2), e0=100000.0, drop_day=None):
    """Points: day 0 = e0, then one end-of-day point per trading day with the given simple returns; plus a midday
    point (= the previous close, so it changes nothing) and a weekend point (ignored)."""
    days = calendars.trading_days(start, start + timedelta(days=400))[:len(returns) + 1]
    pts, e = [], e0
    eq = [e0]
    for r in returns:
        e = e * (1 + r)
        eq.append(e)
    for i, (d, v) in enumerate(zip(days, eq)):
        if d == drop_day:
            continue
        if i > 0:
            pts.append({"ts": datetime.combine(d, time(12, 0), tzinfo=NY).astimezone(timezone.utc).isoformat(),
                        "equity": eq[i - 1]})
        pts.append({"ts": close_ts(d), "equity": v})
        if d.weekday() == 4:
            sat = d + timedelta(days=1)
            pts.append({"ts": datetime.combine(sat, time(12, 0), tzinfo=NY).isoformat(), "equity": v})
    return pts, days, eq


class QuantileTest(unittest.TestCase):
    def test_min_window_and_rounding(self):
        self.assertEqual(R.min_window(0.99), 100)          # 1 / 0.01 = 99.99999999999991 → rounded → 100
        self.assertEqual(R.min_window(0.975), 40)
        self.assertEqual(R._ceil_rounded(0.1 * 3 * 10), 3)  # 3.0000000000000004 is 3, not 4

    def test_var_es_hand(self):
        losses = list(range(1, 101))                        # n = 100
        self.assertEqual(R.hs_var_es(losses, 0.99), (99.0, 99.5))     # k = 99; tail {99, 100}
        self.assertEqual(R.hs_var_es(losses, 0.975), (98.0, 99.0))    # k = 98; tail {98, 99, 100}
        self.assertEqual(R.hs_var_es(reversed(losses), 0.99), (99.0, 99.5))   # order does not matter

    def test_ties_and_tail_mean(self):
        xs = [0.0] * 36 + [2.0, 3.0, 5.0, 7.0]              # n = 40, k = ceil(39) = 39 → 5; tail {5, 7}
        self.assertEqual(R.hs_var_es(xs, 0.975), (5.0, 6.0))
        xs = [0.0] * 37 + [3.0, 5.0, 5.0]                   # tie at the VaR: both 5s are in the tail
        self.assertEqual(R.hs_var_es(xs, 0.975), (5.0, 5.0))

    def test_too_short(self):
        with self.assertRaises(ValueError):
            R.hs_var_es(range(99), 0.99)
        with self.assertRaises(ValueError):
            R.hs_var_es(range(39), 0.975)


class SeriesTest(unittest.TestCase):
    def test_full_figures(self):
        rets = [(k - 50) / 10000.0 for k in range(1, 101)]     # −0.49 % … +0.50 %, monotonic
        pts, days, eq = series(rets)
        now = datetime.combine(days[-1], time(18, 0), tzinfo=NY)
        figs, n, det = R.series_figures(pts, now)
        self.assertEqual(n, 100)
        self.assertEqual(det["gaps_skipped"], 0)
        e = eq[-1]
        # losses = −E·r; VaR 99 % = 2nd largest loss = 0.0048·E; ES 97.5 % = mean(0.0047, 0.0048, 0.0049)·E
        self.assertAlmostEqual(figs["var99_1d"]["value"], 0.0048 * e, places=6)
        self.assertAlmostEqual(figs["es975_1d"]["value"], 0.0048 * e, places=6)
        self.assertEqual(figs["var99_1d"]["unit"], "usd")
        # vol20: returns 31..50 / 10000 → sample variance of 20 consecutive integers = 20·21/12 = 35
        self.assertAlmostEqual(figs["vol20_ann_pct"]["value"], math.sqrt(35) / 10000 * math.sqrt(252) * 100, places=9)
        # max drawdown: from e0 down through the 49 negative returns
        trough = 100000.0
        for k in range(1, 50):
            trough *= 1 + (k - 50) / 10000.0
        self.assertAlmostEqual(figs["max_drawdown_pct"]["value"], (1 - trough / 100000.0) * 100, places=9)
        self.assertTrue(all(f["available"] and f["reason"] is None for f in figs.values()))

    def test_too_few_returns(self):
        pts, days, _ = series([0.001] * 59)
        figs, n, _ = R.series_figures(pts, datetime.combine(days[-1], time(18, 0), tzinfo=NY))
        self.assertEqual(n, 59)
        for k in ("var99_1d", "es975_1d"):
            self.assertFalse(figs[k]["available"])
            self.assertIsNone(figs[k]["value"])
            self.assertEqual(figs[k]["reason"], "needs ≥ 60 daily returns, have 59")
        self.assertTrue(figs["vol20_ann_pct"]["available"])
        self.assertAlmostEqual(figs["vol20_ann_pct"]["value"], 0.0, places=9)

    def test_between_60_and_100_returns(self):
        pts, days, _ = series([(-1) ** k * 0.001 * (k % 7) for k in range(70)])
        figs, n, _ = R.series_figures(pts, datetime.combine(days[-1], time(18, 0), tzinfo=NY))
        self.assertEqual(n, 70)
        self.assertFalse(figs["var99_1d"]["available"])               # RiskLab minimum window at 99 %: 100
        self.assertIn("≥ 100 daily returns", figs["var99_1d"]["reason"])
        self.assertTrue(figs["es975_1d"]["available"])                # 97.5 % needs 40

    def test_gap_and_open_session(self):
        rets = [0.001] * 70
        days = calendars.trading_days(date(2026, 3, 2), date(2027, 3, 1))[:71]
        pts, _, _ = series(rets, drop_day=days[30])
        figs, n, det = R.series_figures(pts, datetime.combine(days[-1], time(18, 0), tzinfo=NY))
        self.assertEqual((n, det["gaps_skipped"]), (68, 1))           # returns 30 and 31 lost, one gap
        # during the last session its close is not end of day yet
        _, n2, det2 = R.series_figures(pts, datetime.combine(days[-1], time(15, 0), tzinfo=NY))
        self.assertEqual(n2, 67)
        self.assertEqual(det2["last_day"], days[-2].isoformat())

    def test_outside_calendar_is_a_gap(self):
        far = date(2199, 1, 6)
        self.assertFalse(calendars.is_covered(far))
        rets, gaps = R.daily_returns([(far, 100.0), (far + timedelta(days=1), 101.0)])
        self.assertEqual((rets, gaps), ([], 1))                      # never a 500, never guessed

    def test_unavailable_and_bad_points(self):
        figs, n, _ = R.series_figures([], datetime(2026, 11, 6, tzinfo=timezone.utc), "no platform equity history yet")
        self.assertEqual(n, 0)
        self.assertTrue(all(not f["available"] and f["reason"] == "no platform equity history yet"
                            for f in figs.values()))
        self.assertIsNone(R.max_drawdown_pct([{"ts": "x", "equity": None}, {"ts": "y", "equity": 5.0}]))
        self.assertAlmostEqual(R.max_drawdown_pct([{"equity": 100}, {"equity": None}, {"equity": 80},
                                                   {"equity": 120}, {"equity": 90}]), 25.0)

    def test_exposure(self):
        f = R.exposure_figures(100000, 60000, 20000)
        self.assertAlmostEqual(f["net_exposure_pct"]["value"], 40.0)
        self.assertAlmostEqual(f["gross_exposure_pct"]["value"], 80.0)
        f = R.exposure_figures(None, None, None, "Valuation paused: no quote")
        self.assertEqual(f["net_exposure_pct"]["reason"], "Valuation paused: no quote")
        self.assertFalse(f["gross_exposure_pct"]["available"])
        self.assertFalse(R.exposure_figures(0, 0, 0)["net_exposure_pct"]["available"])


if __name__ == "__main__":
    unittest.main()
