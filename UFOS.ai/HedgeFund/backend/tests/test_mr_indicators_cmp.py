"""Market Research MR5 review H1 — compare overlay with a fixed base (``cmp_base``): the same t gives the same
value in every request window (full window, a short live refresh, a page further left), the base may lie
outside the window, a base without a bar of either symbol is a 400 (no stand-in), and the route validates it.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_indicators_cmp -v
"""
import asyncio
import unittest

try:
    from test_mr_indicators import FakeData, daily_bars, RouteTest
    from test_mr_data_api import Q
except ImportError:  # pragma: no cover - run as package
    from tests.test_mr_indicators import FakeData, daily_bars, RouteTest
    from tests.test_mr_data_api import Q

from market_research import indicators as ind
from market_research.bars.series import BadParam
from strategy_platform.core.clock import to_utc


class CompareBaseTest(unittest.TestCase):
    def setUp(self):
        self.a = daily_bars(400, seed=1)
        self.b = daily_bars(400, seed=2)
        self.data = FakeData({"AAPL": self.a, "SPY": self.b})
        self.specs = ind.parse_list("cmp:SPY", "AAPL")

    def run_(self, limit, before=None, base=None):
        return ind.compute(self.data, "AAPL", "1D", None, to_utc(before) if before else None, limit, self.specs,
                           to_utc(base) if base else None)

    def by_t(self, r, key="cmp:SPY:pct"):
        return dict(zip(r["t"], r["series"][key]))

    def test_same_t_same_value_across_windows(self):
        base_t = self.a[0]["t"]                                   # the first loaded bar (the WPF sends it)
        full = self.by_t(self.run_(400, base=base_t))
        short = self.by_t(self.run_(300, base=base_t))            # live refresh with limit 300
        left = self.by_t(self.run_(150, before=self.a[200]["t"], base=base_t))   # a page further left
        for part in (short, left):
            for t, v in part.items():
                self.assertAlmostEqual(v, full[t], places=12)
        # without a base the windows disagree (the bug the review found)
        nb_full, nb_short = self.by_t(self.run_(400)), self.by_t(self.run_(300))
        t = self.a[-1]["t"]
        self.assertNotAlmostEqual(nb_full[t], nb_short[t], places=6)
        ref = (self.b[-1]["c"] / self.b[0]["c"] - 1) * 100
        self.assertAlmostEqual(full[t], ref, places=9)
        price = self.by_t(self.run_(300, base=base_t), "cmp:SPY:price")
        self.assertAlmostEqual(price[t], self.a[0]["c"] * self.b[-1]["c"] / self.b[0]["c"], places=9)
        r = self.run_(300, base=base_t)
        self.assertEqual(r["indicators"][0]["base"], {"t": base_t, "close": self.a[0]["c"], "other_close": self.b[0]["c"]})

    def test_base_without_bar_is_refused(self):
        b = [x for x in self.b if x["t"] != self.a[5]["t"]]
        data = FakeData({"AAPL": self.a, "SPY": b})
        with self.assertRaises(BadParam) as cm:
            ind.compute(data, "AAPL", "1D", None, None, 300, self.specs, to_utc(self.a[5]["t"]))
        self.assertEqual(cm.exception.field, "cmp_base")
        self.assertIn("compare symbol SPY", cm.exception.user_message)
        with self.assertRaises(BadParam):                          # not a bar start (12:00 instead of midnight)
            ind.compute(self.data, "AAPL", "1D", None, None, 300, self.specs,
                        to_utc(self.a[5]["t"]).replace(hour=12))


class CompareBaseRouteTest(RouteTest):
    def test_route_param(self):
        st, bars = self.call("symbol_bars", "AAPL", ("tf", "1D"), ("limit", "100"))
        base = bars["bars"][0]["t"]
        st1, r1 = self.call("symbol_indicators", "AAPL", ("tf", "1D"), ("limit", "100"), ("ind", "cmp:SPY"),
                            ("cmp_base", base))
        st2, r2 = self.call("symbol_indicators", "AAPL", ("tf", "1D"), ("limit", "30"), ("ind", "cmp:SPY"),
                            ("cmp_base", base))
        self.assertEqual((st1, st2), (200, 200), r2)
        m = dict(zip(r1["t"], r1["series"]["cmp:SPY:pct"]))
        for t, v in zip(r2["t"], r2["series"]["cmp:SPY:pct"]):
            self.assertEqual(v, m[t])
        for pairs in ((("ind", "sma:5"), ("cmp_base", base)), (("ind", "cmp:SPY"), ("cmp_base", "yesterday")),
                      (("ind", "cmp:SPY"), ("cmp_base", base), ("cmp_base", base)),
                      (("ind", "cmp:SPY"), ("cmp_base", "2026-10-15T12:34:00+00:00"))):
            st, p = self.call("symbol_indicators", "AAPL", ("tf", "1D"), ("limit", "30"), *pairs)
            self.assertEqual(st, 400, pairs)
            self.assertIn("cmp_base", p["errors"])


if __name__ == "__main__":
    unittest.main()
