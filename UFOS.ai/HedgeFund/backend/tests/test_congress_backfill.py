"""CONGRESS slice (c) — the 2-year initial load through the "Start backfill" button (CONGRESS_API.md §4): filers.json ->
every House/Senate filer's file (executive filers skipped), rows filed in the 2-year window only, origin backfill,
one request per second, a failing filer recorded as a gap, live rows never overwritten, no alerts, and the price step
computing the backfilled congress rows (ticker -> CIK -> SIC -> sector ETF). All data SYNTHETIC; offline."""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timezone
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from newsroom import congress, db, disclosure_alerts as al, disclosure_db as ddb  # noqa: E402
from newsroom import disclosures_backfill as bfm, disclosures_service as svc  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 2, 0, tzinfo=UTC)          # Friday 22:00 New York: outside market hours


def trade(i, filed, **kw):
    r = {"id": f"house_7{i:06d}_g1", "filing_id": f"house_7{i:06d}", "filer_id": "house_jane_example",
         "source_id": "house_clerk", "transaction_date": filed, "filing_date": filed, "owner": None, "ticker": "AAA",
         "asset_name": "Example Corp", "asset_type": "ST", "transaction_type": "Purchase", "amount_range_low": 1001,
         "amount_range_high": 15000, "amount_range_label": "$1,001 - $15,000", "is_late": 0, "days_to_file": 3,
         "doc_url": "https://disclosures.example.invalid/7.pdf", "filing_type": "PTR"}
    r.update(kw)
    return r


FILERS = [{"id": "house_jane_example", "full_name": "Jane Example", "chamber": "house", "party": "D", "state": "XX"},
          {"id": "senate_bad_example", "full_name": "Bad Example", "chamber": "senate", "party": "R", "state": "YY"},
          {"id": "oge_exec_example", "full_name": "Exec Example", "chamber": None, "branch": "executive"}]
FILES = {"house_jane_example": {"filer": FILERS[0], "trades": [trade(1, "2026-10-02"), trade(2, "2024-01-05"),
                                                               trade(3, "2026-09-01", ticker="SPY", asset_type="EF")]}}


class FakeHttp:
    def __init__(self):
        self.calls = []

    def __call__(self, url, etag=None):
        self.calls.append(url)
        name = url[len(congress.BASE_URL):]
        if name == "filers.json":
            return 200, None, json.dumps(FILERS).encode()
        fid = name[len("filer/"):-len(".json")]
        if fid in FILES:
            return 200, None, json.dumps(FILES[fid]).encode()
        raise congress.SourceError(url + ": HTTP 404")


def fake_sec(url):
    if url == bfm.DATASETS_PAGE:
        return b"<html>no data sets</html>"
    if "/daily-index/" in url:
        return b""
    if url == svc.COMPANY_TICKERS_URL:
        return json.dumps({"0": {"cik_str": 123456, "ticker": "AAA", "title": "Example Corp"}}).encode()
    if url.endswith("CIK0000123456.json"):
        return json.dumps({"sic": "3674", "tickers": ["AAA"], "filings": {"recent": {}}}).encode()
    raise svc.SecUnavailable(url + ": HTTP 404")


def series(base, step):
    return {d: (base * (1 + step * k) * 0.999, base * (1 + step * k))
            for k, d in enumerate(calendars.trading_days(date(2024, 9, 1), date(2027, 3, 31)))}


class FakePrices:
    def __init__(self, now=None):
        self.data = {"AAA": series(10, 0.001), "SPY": series(500, 0.0005), "XLK": series(100, 0.0004)}

    def daily(self, symbol, first, last):
        return {d: v for d, v in self.data.get(symbol, {}).items() if first <= d <= last}

    def minutes(self, symbol, start, end):
        return []


class CongressBackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="congress-bf-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(db.close)
        self.assertTrue(db.init(os.path.join(self.tmp, "newsroom.db"), legacy=""))
        if importlib.util.find_spec("aiohttp") is None:          # the gap step imports the live Form 4 parser
            stub = types.ModuleType("aiohttp")
            stub.ClientSession, stub.ClientTimeout, stub.TCPConnector = object, (lambda **kw: None), (lambda **kw: None)
            patcher = mock.patch.dict(sys.modules, {"aiohttp": stub})
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_initial_load_and_prices(self):
        live, _ = congress.map_rows([trade(1, "2026-10-02", amount_range_label="LIVE")])
        ddb.upsert_trades(live)
        http, sleeps = FakeHttp(), []
        bf = bfm.Backfill(fetch=fake_sec, prices_factory=FakePrices, clock=lambda: NOW, sleep=sleeps.append, http=http)
        with mock.patch.object(al, "on_new_trades", side_effect=AssertionError("the backfill never alerts")):
            self.assertTrue(bf.control("start")[0])
            bf._thread.join(timeout=60)
        st = bf.state()
        self.assertEqual(st["state"], "complete", st)
        self.assertEqual(http.calls, [congress.BASE_URL + "filers.json",
                                      congress.BASE_URL + "filer/house_jane_example.json",
                                      congress.BASE_URL + "filer/senate_bad_example.json"])   # executive skipped
        self.assertEqual(sleeps.count(bfm.CONGRESS_REQUEST_PAUSE), 2)                       # one per filer file
        t1 = ddb.get_trade("congress:house_7000001_g1")
        self.assertEqual((t1["origin"], t1["amount_label"]), ("live", "LIVE"))            # live row kept
        self.assertIsNone(ddb.get_trade("congress:house_7000002_g1"))                     # older than 2 years
        t3 = ddb.get_trade("congress:house_7000003_g1")
        self.assertEqual((t3["origin"], t3["chamber"], t3["party"], t3["owner_name"]),
                         ("backfill", "house", "D", "Jane Example"))
        steps = {s["key"]: s for s in bfm.status()["steps"]}
        self.assertEqual([g["key"] for g in steps["congress"]["gaps"]], ["senate_bad_example"])
        r3 = ddb.returns_for([t3["id"]])[t3["id"]]
        self.assertEqual(len(r3), 8)
        f1 = next(r for r in r3 if r["start"] == "filing" and r["horizon"] == 1)
        self.assertEqual((f1["entry_basis"], f1["etf"]), ("next_open_after_filing_date", None))
        self.assertIn("no SEC company for ticker SPY", f1["reason"])
        self.assertEqual(bfm.status()["estimate"]["requests_congress"], 370)


if __name__ == "__main__":
    unittest.main()
