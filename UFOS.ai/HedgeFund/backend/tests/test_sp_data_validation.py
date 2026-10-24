"""strategy_platform/data/validation.py: findings for duplicates,
non-positive prices, high<low, gaps (NYSE calendar / every UTC day),
8-sigma outliers without a corporate action, and stale data. Offline.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_data_validation -v
"""
import math
import unittest
from datetime import date, timedelta

try:
    from test_sp_data_support import crypto_row, equity_row, utc
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import crypto_row, equity_row, utc

from strategy_platform.core import calendars
from strategy_platform.data.validation import validate_bars


def sessions(start, n):
    out, d = [], start
    while len(out) < n:
        if calendars.is_trading_day(d):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def series(days, base=100.0):
    rows, p = [], base
    for i, d in enumerate(days):
        p *= 1 + (0.004 if i % 2 else -0.003)
        rows.append(equity_row(d, p))
    return rows


def codes(findings):
    return [f["code"] for f in findings]


class ValidationTest(unittest.TestCase):
    def test_clean_series_has_no_findings(self):
        rows = series(sessions(date(2026, 6, 1), 60))
        self.assertEqual(validate_bars(rows, symbol="SPY"), [])

    def test_duplicates_prices_and_ohlc(self):
        rows = series(sessions(date(2026, 9, 1), 5))
        rows.append(dict(rows[2]))
        rows[0] = dict(rows[0], low=-1.0)
        rows[1] = dict(rows[1], high=1.0, low=2.0)
        rows[3] = dict(rows[3], open=rows[3]["high"] * 2)
        f = validate_bars(rows, symbol="SPY")
        self.assertEqual(sorted(codes(f)), ["duplicate", "high_below_low", "non_positive", "ohlc_outside"])
        self.assertTrue(all(set(x) == {"code", "severity", "message", "ts"} for x in f))
        self.assertEqual({x["code"]: x["severity"] for x in f}["duplicate"], "error")

    def test_gaps_follow_the_calendar(self):
        days = sessions(date(2026, 8, 31), 8)                      # spans Labor Day (no gap there)
        self.assertNotIn("2026-09-07", days)
        self.assertEqual(validate_bars(series(days), symbol="SPY"), [])
        f = validate_bars(series(days[:3] + days[4:]), symbol="SPY")
        self.assertEqual(codes(f), ["gap"])
        self.assertIn(days[3], f[0]["message"])

    def test_crypto_gaps_every_utc_day(self):
        rows = [crypto_row(f"2026-09-{d:02d}", 100.0) for d in (1, 2, 3, 5)]
        f = validate_bars(rows, symbol="BTC-USD")
        self.assertEqual(codes(f), ["gap"])
        self.assertIn("2026-09-04", f[0]["message"])

    def test_calendar_unavailable_is_reported(self):
        rows = [equity_row("2031-03-03", 1.0, known_at="2031-03-03T21:00:00+00:00"),      # beyond 2030 (S10-H)
                equity_row("2031-03-04", 1.0, known_at="2031-03-04T21:00:00+00:00")]
        self.assertEqual(codes(validate_bars(rows, symbol="SPY")), ["calendar_unavailable"])

    def test_outlier_unless_corporate_action(self):
        days = sessions(date(2026, 5, 1), 40)
        rows = series(days)
        rows[30] = dict(rows[30], close=rows[29]["close"] * 1.5, high=rows[29]["close"] * 1.6)
        f = [x for x in validate_bars(rows, symbol="SPY") if x["code"] == "return_outlier"]
        self.assertEqual([x["ts"] for x in f], [rows[30]["ts"]])    # the jump (it then widens sigma)
        ca = [{"symbol": "SPY", "kind": "forward_split", "ex_date": days[30]}]
        self.assertEqual([x for x in validate_bars(rows, symbol="SPY", corporate_actions=ca)
                          if x["code"] == "return_outlier"], [])
        self.assertTrue(math.isfinite(rows[30]["close"]))

    def test_stale(self):
        rows = series(sessions(date(2026, 9, 1), 20))              # last bar Tue 2026-09-29
        self.assertEqual(rows[-1]["ts"][:10], "2026-09-29")
        self.assertEqual(validate_bars(rows, symbol="SPY", now=utc(2026, 9, 30, 15)), [])     # Wed in session
        f = validate_bars(rows, symbol="SPY", now=utc(2026, 9, 30, 21))                      # Wed after close
        self.assertEqual(codes(f), ["stale"])
        crypto = [crypto_row("2026-09-27", 1.0), crypto_row("2026-09-28", 1.0)]
        self.assertEqual(validate_bars(crypto, symbol="BTC-USD", now=utc(2026, 9, 30, 12)), [])
        self.assertEqual(codes(validate_bars(crypto, symbol="BTC-USD", now=utc(2026, 10, 1, 1))), ["stale"])


if __name__ == "__main__":
    unittest.main()
