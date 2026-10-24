"""Stage 9 (SPEC §19.15): the EUR/USD rates the Shadow Account conversions need (FRED DEXUSEU, exact dates, loaded on
request through a fake FRED client) and the fixed tax notice — the tax export itself was REMOVED on 2026-10-18 by the
user's decision. Offline."""
import unittest
from datetime import date

try:
    from sp_monitor_testkit import MonCase
except ImportError:  # pragma: no cover
    from tests.sp_monitor_testkit import MonCase

from strategy_platform.core.errors import SecretMissing
from strategy_platform.execution.errors import TradingError
from strategy_platform.monitoring import fx, tax_notice


class FakeFred:
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc = rows or [], exc

    def first_releases(self, series_id, units, start, end, frequency="daily"):
        if self.exc:
            raise self.exc
        assert series_id == "DEXUSEU"
        return self.rows


class FxRefreshTest(MonCase):
    def test_refresh_and_exact_lookup(self):
        rows = [{"date": "2025-12-31", "value": 1.17, "realtime_start": "2026-01-02"},
                {"date": "2026-01-02", "value": 1.18, "realtime_start": "2026-01-05"}]
        out = fx.refresh(2026, client=FakeFred(rows))
        self.assertEqual((out["observations"], out["added"]), (1, 1))
        r = fx.Rates.load(date(2025, 12, 1), date(2026, 1, 10))
        self.assertIsNone(r.rate_to_eur("USD", "2026-01-01"))                       # no earlier-fixing lookup
        self.assertIn("no fixing on that day", r.reason("2026-01-01"))
        self.assertAlmostEqual(r.rate_to_eur("USD", "2026-01-02"), 1 / 1.18)
        self.assertIn("after the last loaded fixing (2026-01-02)", r.reason("2026-01-05"))
        with self.assertRaises(fx.FxUnsupported):
            r.rate_to_eur("GBP", "2026-01-02")
        with self.assertRaises(TradingError) as cm:
            fx.refresh(2026, client=FakeFred(exc=SecretMissing("fred-api-key", "The FRED API key")))
        self.assertEqual(cm.exception.code, "fx_unavailable")
        self.assertNotIn("tax", fx.NOTE.lower())


class TaxNoticeTest(unittest.TestCase):
    def test_fixed_wording(self):
        p = tax_notice.payload()
        self.assertEqual(set(p), {"version", "decided", "notice", "paper_notice", "tax_export"})
        self.assertFalse(p["tax_export"])
        self.assertIn("Jahressteuerbescheinigung", p["notice"])
        self.assertIn("no tax relevance", p["paper_notice"])


if __name__ == "__main__":
    unittest.main()
