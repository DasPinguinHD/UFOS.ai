"""INSIDER-RET slice (b) — live collection, daily job, read API (contract INSIDER_RET_API.md §6/§7, accepted
2026-10-24): the shared SEC limiter (one slot booking for live + job + backfill, the 0.2 s backfill override is
restored), the collector using it before every request and marking amendments, the live hook, the daily job (SIC
fetch, trades waiting for a SIC, price errors retried, clusters, job row), the 17:00 New York schedule incl. missed
runs, and the API payloads (filters and 400s, "N of M shown", one chip, details, Track record, markers by filing day,
status with DB sizes). Offline; aiohttp is not needed."""
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from newsroom import db, disclosure_db as ddb, disclosure_returns as eng, disclosures_api as api  # noqa: E402
from newsroom import disclosures_service as svc, sec_limiter  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402

UTC = timezone.utc


def ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def series(base, step, first=date(2026, 9, 1), last=date(2027, 3, 31)):
    return {d: (base * (1 + step * k) * 0.999, base * (1 + step * k))
            for k, d in enumerate(calendars.trading_days(first, last))}


class FakePrices:
    def __init__(self, daily, fail=()):
        self.data, self.fail = daily, set(fail)

    def daily(self, symbol, first, last):
        if symbol in self.fail:
            raise eng.PriceUnavailable(f"{symbol}: source down")
        return {d: v for d, v in self.data.get(symbol, {}).items() if first <= d <= last}

    def minutes(self, symbol, start, end):
        return []


DETAILS = {"issuer_name": "Acme Inc", "issuer_ticker": "ACME", "issuer_cik": "0000123456", "owner_name": "Doe Jane",
           "owner_cik": "0000999", "owner_roles": ["Chief Financial Officer"], "plan_10b5_1": False,
           "transactions": [{"code": "P", "derivative": False, "date": "2026-10-01", "shares": 100.0, "price": 10.0}]}


def event(acc="0001234567-26-000042", details=DETAILS, filed="2026-10-05T17:30:00-04:00", form="4"):
    return {"id": "insider-" + acc, "details": details, "filed_at": filed, "form": form,
            "url": f"https://www.sec.gov/Archives/edgar/data/123456/{acc.replace('-', '')}/{acc}-index.htm"}


class LimiterTest(unittest.TestCase):
    def test_slots_shared_and_override_restored(self):
        clock = [100.0]
        lim = sec_limiter.SecLimiter(lambda: 0.5, clock=lambda: clock[0])
        self.assertEqual([lim.reserve() for _ in range(3)], [0.0, 0.5, 1.0])     # three callers, one schedule
        clock[0] = 110.0
        with lim.override(0.2):
            self.assertTrue(lim.overridden)
            self.assertEqual([round(lim.reserve(), 3) for _ in range(2)], [0.0, 0.2])
        self.assertFalse(lim.overridden)
        self.assertEqual(lim.interval(), 0.5)
        self.assertEqual(sec_limiter.SecLimiter(lambda: 0.01).interval(), 0.1)    # never faster than 10 req/s
        with self.assertRaises(RuntimeError):
            with lim.override(0.2):
                raise RuntimeError("boom")
        self.assertFalse(lim.overridden)

    def test_collector_books_every_request(self):
        with open(os.path.join(BACKEND, "newsroom", "sec_edgar_collector.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("asyncio.sleep(SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS)", src)
        self.assertEqual(src.count("await _sec_limiter.wait_async()"), 3)
        self.assertEqual(src.count("session.get("), 3)
        self.assertIn('event["form"] = "4/A" if feed_title.startswith("4/A") else "4"', src)

    def test_main_wiring(self):
        with open(os.path.join(BACKEND, "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("disclosures_service.on_insider_event(event)", src)
        self.assertIn("disclosures_api.register(app)", src)
        self.assertIn('disclosures_service.run_forever(), name="disclosures-daily"', src)


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="disc-live-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(db.close)
        self.assertTrue(db.init(os.path.join(self.tmp, "newsroom.db"), legacy=""))
        svc.LIVE_STATE.update(rows_written=0, last_at=None, last_error=None)


class LiveAndDailyTest(DbCase):
    def test_live_hook(self):
        self.assertEqual(svc.on_insider_event(event()), 1)
        t = ddb.get_trade("insider:0001234567-26-000042:P")
        self.assertEqual((t["accepted_at"], t["filing_date"], t["origin"], t["role_class"]),
                         ("2026-10-05T21:30:00+00:00", "2026-10-05", "live", "CFO"))
        self.assertTrue(t["official_url"].endswith("-index.htm"))
        self.assertEqual(svc.on_insider_event(event(details=None)), 0)
        self.assertEqual(svc.on_insider_event(event(acc="0001234567-26-000043", form="4/A")), 1)
        self.assertEqual(ddb.get_trade("insider:0001234567-26-000043:P")["measurable"], 0)
        db.close()
        self.assertEqual(svc.on_insider_event(event(acc="0001234567-26-000044")), 0)
        self.assertIn("StoreUnavailable", svc.LIVE_STATE["last_error"])

    def test_daily_job(self):
        svc.on_insider_event(event())
        other = dict(DETAILS, issuer_cik="0000777", issuer_ticker="BBB", owner_cik="0000888")
        svc.on_insider_event(event(acc="0001234567-26-000050", details=other))
        third = dict(DETAILS, issuer_cik="0000555", issuer_ticker="CCC", owner_cik="0000444")
        svc.on_insider_event(event(acc="0001234567-26-000051", details=third))

        def fetch(cik):
            if str(cik) == "777":
                raise svc.SecUnavailable("https://data.sec.gov/submissions/CIK0000000777.json: HTTP 503")
            return {"sic": "2834" if str(cik) == "123456" else "6770", "sicDescription": "x", "tickers": ["T"]}
        prices = FakePrices({"ACME": series(10, 0.01), "XLV": series(100, 0.002), "CCC": series(5, 0.0)},
                            fail={"CCC"})
        out = svc.run_daily(now=ts("2026-10-13T22:00:00Z"), prices=prices, fetch=fetch)
        self.assertEqual((out["state"], out["computed"], out["waiting_for_sic"], out["price_errors"],
                          out["sic_failed"]), ("partial", 1, 1, 1, 1))
        rows = ddb.returns_for(["insider:0001234567-26-000042:P"])["insider:0001234567-26-000042:P"]
        f1 = next(r for r in rows if r["start"] == "filing" and r["horizon"] == 1)
        self.assertEqual((f1["state"], f1["etf"], f1["entry_basis"]), ("complete", "XLV", "next_open"))
        job = ddb.job_get(svc.JOB_DAILY)
        self.assertEqual(job["state"], "partial")
        self.assertIn("HTTP 503", job["last_error"])
        self.assertIn("CCC: source down", job["last_error"])
        self.assertIsNone(ddb.sic_get(777))                      # retried next run, never guessed

    def test_schedule(self):
        fri_evening = ts("2026-10-23T22:30:00Z")                 # 18:30 New York, Friday
        self.assertEqual(svc.latest_slot(fri_evening), ts("2026-10-23T21:00:00Z"))
        self.assertEqual(svc.latest_slot(ts("2026-10-24T15:00:00Z")), ts("2026-10-23T21:00:00Z"))   # Saturday
        self.assertEqual(svc.next_slot(ts("2026-10-24T15:00:00Z")), ts("2026-10-26T21:00:00Z"))     # Monday
        self.assertTrue(svc.due(fri_evening, None))
        self.assertTrue(svc.due(fri_evening, {"progress": {"finished": "2026-10-22T21:05:00+00:00"}}))  # missed
        self.assertFalse(svc.due(fri_evening, {"progress": {"finished": "2026-10-23T21:20:00+00:00"}}))


class ApiTest(DbCase):
    def setUp(self):
        super().setUp()
        svc.on_insider_event(event())
        sale = dict(DETAILS, transactions=[{"code": "S", "derivative": False, "date": "2026-10-02", "shares": 5.0,
                                           "price": 11.0}], plan_10b5_1=True)
        svc.on_insider_event(event(acc="0001234567-26-000060", details=sale, filed="2026-10-06T10:00:00-04:00"))
        ddb.sic_put(123456, 2834, "Pharma", ["ACME"])
        svc.run_daily(now=ts("2026-10-20T22:00:00Z"),
                      prices=FakePrices({"ACME": series(10, 0.01), "XLV": series(100, 0.002)}),
                      fetch=lambda cik: {"sic": "2834"})
        self.today = date(2026, 10, 20)

    def test_list_filters_and_400s(self):
        out = api.list_payload({}, self.today)
        self.assertEqual((out["total_all"], out["total_matching"], out["shown"]), (2, 2, 2))
        row = out["rows"][0]
        self.assertEqual(set(row) >= {"ticker", "owner_name", "code", "value_usd", "reporting_delay_days", "chip"}, True)
        self.assertEqual(row["accepted_at"][-1], "Z")
        self.assertEqual(row["chip"]["state"], "complete")
        self.assertEqual(api.list_payload({"code": "S"}, self.today)["total_matching"], 1)
        self.assertEqual(api.list_payload({"plan": "planned"}, self.today)["rows"][0]["code"], "S")
        self.assertEqual(api.list_payload({"role": "CEO"}, self.today)["total_matching"], 0)
        self.assertEqual(api.list_payload({"days": "1"}, self.today)["total_matching"], 0)
        for bad in ({"days": "0"}, {"days": "731"}, {"code": "A"}, {"role": "Boss"}, {"plan": "x"},
                    {"ticker": "DROP TABLE"}, {"limit": "501"}, {"amendments": "maybe"}):
            with self.assertRaises(api.BadRequest, msg=str(bad)):
                api.list_payload(bad, self.today)

    def test_detail_track_record_markers_status(self):
        d = api.detail(ddb.get_trade("insider:0001234567-26-000042:P"),
                       ddb.returns_for(["insider:0001234567-26-000042:P"])["insider:0001234567-26-000042:P"])
        self.assertEqual((d["etf"], d["sic"], len(d["returns"]["filing"]["horizons"])), ("XLV", 2834, 4))
        self.assertEqual(d["returns"]["trade_date"]["entry_basis"], "close")
        self.assertTrue(d["official_url"].startswith("https://www.sec.gov/"))
        self.assertIn(api.sector_map.CAVEAT, d["notes"])
        tr = api.track_record_payload({"group": "direction", "horizon": "5"}, self.today)
        self.assertIn("approximation", tr["caveat"])
        self.assertEqual(sorted(g["group"] for g in tr["groups"]), ["Purchases", "Sales"])
        with self.assertRaises(api.BadRequest):
            api.track_record_payload({"horizon": "7"}, self.today)
        m = api.markers_payload({"ticker": "acme", "from": "2026-10-01", "to": "2026-10-31"})
        self.assertEqual([(x["date"], x["side"], x["count"]) for x in m["markers"]],
                         [("2026-10-05", "buy", 1), ("2026-10-06", "sell", 1)])      # filing (acceptance) day
        with self.assertRaises(api.BadRequest):
            api.markers_payload({"ticker": "ACME", "from": "2026-10-31", "to": "2026-10-01"})
        with self.assertRaises(api.BadRequest):
            api.markers_payload({"ticker": "ACME", "from": "2026-10-01", "to": "2026-10-31", "kinds": "congress"})
        st = api.status_payload()
        self.assertEqual(st["backfill"]["state"], "not_started")
        self.assertEqual([s["key"] for s in st["backfill"]["steps"]],
                         ["datasets", "pilot", "acceptance", "gap", "prices"])
        self.assertEqual(st["counts"]["trades"], 2)
        self.assertGreater(st["storage"]["newsroom_db_bytes"], 0)
        self.assertIn("hedgefund_db_bytes", st["storage"])
        self.assertEqual(st["daily_job"]["state"], "done")
        self.assertTrue(re.match(r"\d{4}-\d{2}-\d{2}T", st["daily_job"]["next_run"]))

    def test_routes(self):
        self.assertEqual([p for _m, p, _f in api.ROUTES], [
            "/newsroom/disclosures/insider", "/newsroom/disclosures/insider/{id}",
            "/newsroom/disclosures/track-record", "/newsroom/disclosures/markers", "/newsroom/disclosures/status"])
        self.assertTrue(api.TRADE_ID_RE.match("insider:0001234567-26-000042:P"))
        self.assertFalse(api.TRADE_ID_RE.match("insider:x' OR 1=1"))


if __name__ == "__main__":
    unittest.main()
