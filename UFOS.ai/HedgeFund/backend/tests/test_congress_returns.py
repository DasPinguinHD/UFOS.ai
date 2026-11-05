"""CONGRESS slice (b) — returns, Track-record groups, watchlist alerts (CONGRESS_API.md §3/§5/§6, decisions §10):
congress returns in the daily job (next open after the filing date, ticker -> CIK -> SIC -> sector ETF, ETF/fund
tickers without a benchmark, an unresolved ticker map waits), the company_tickers cache, run_all, the congress groups
(direction / chamber / party / late / amount) and markers, the member list + watch list (403 / 400), and the alerts:
Monitoring only (toast off), watched member + held ticker, only NEW rows, never on the first pull, the live Form 4
hook. All rows SYNTHETIC; offline."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from newsroom import congress, congress_api, db, disclosure_alerts as al, disclosure_db as ddb  # noqa: E402
from newsroom import disclosure_returns as eng, disclosures_api as api, disclosures_service as svc  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.monitoring import alerts as mon_alerts  # noqa: E402

UTC = timezone.utc


def row(i, **kw):
    r = {"id": f"house_8{i:06d}_g1", "filing_id": f"house_8{i:06d}", "filer_id": "house_jane_example",
         "source_id": "house_clerk", "transaction_date": "2026-09-28", "filing_date": "2026-10-02", "owner": "SP",
         "ticker": "AAA", "asset_name": "Example Corp", "asset_type": "ST", "transaction_type": "Purchase",
         "amount_range_low": 1001, "amount_range_high": 15000, "amount_range_label": "$1,001 - $15,000",
         "is_late": 0, "days_to_file": 4, "ingested_at": "2026-10-03 10:00:00",
         "doc_url": "https://disclosures.example.invalid/ptr/8.pdf", "filing_type": "PTR", "filer_name": "Jane Example",
         "chamber": "house", "party": "D", "state": "XX", "office": "XX-01"}
    r.update(kw)
    return r


def series(base, step, first=date(2026, 9, 1), last=date(2027, 3, 31)):
    return {d: (base * (1 + step * k) * 0.999, base * (1 + step * k))
            for k, d in enumerate(calendars.trading_days(first, last))}


class FakePrices:
    def __init__(self):
        self.data = {"AAA": series(10, 0.01), "SPY": series(500, 0.001), "XLK": series(100, 0.002)}

    def daily(self, symbol, first, last):
        return {d: v for d, v in self.data.get(symbol, {}).items() if first <= d <= last}

    def minutes(self, symbol, start, end):
        return []


class Fetch:
    def __init__(self, payload, etag='"e1"'):
        self.payload, self.etag = payload, etag

    def __call__(self, url, etag=None):
        if etag == self.etag:
            return 304, etag, b""
        return 200, self.etag, json.dumps(self.payload).encode()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="congress-b-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(db.close)
        self.assertTrue(db.init(os.path.join(self.tmp, "newsroom.db"), legacy=""))
        al.STATE.update(emitted=0, last_at=None, last_error=None)
        al._held_cache.update(at=0.0, value=None)

    def load(self, rows):
        mapped, _ = congress.map_rows(rows)
        ddb.upsert_trades(mapped)
        return mapped


class ReturnsTest(Case):
    def test_daily_job_congress(self):
        self.load([row(1), row(2, ticker="SPY", asset_type="EF", transaction_type="Sale (Partial)"),
                   row(3, ticker="ZZZ"), row(4, asset_type="OP")])
        now = datetime(2026, 10, 20, 22, tzinfo=UTC)
        out = svc.run_daily(now, prices=FakePrices(), fetch=lambda cik: {"sic": "3674", "tickers": ["AAA"]},
                            source="congress", job_key=svc.JOB_DAILY_CONGRESS,
                            tickers=lambda: {"AAA": "123456", "SPY-X": "9"})
        self.assertEqual((out["state"], out["computed"], out["waiting_for_sic"]), ("done", 4, 0))
        rets = ddb.returns_for(["congress:house_8000001_g1", "congress:house_8000002_g1", "congress:house_8000004_g1"])
        f1 = next(r for r in rets["congress:house_8000001_g1"] if r["start"] == "filing" and r["horizon"] == 1)
        self.assertEqual((f1["entry_basis"], f1["etf"], f1["state"], f1["entry_ts"][:10]),
                         ("next_open_after_filing_date", "XLK", "complete", "2026-10-05"))   # Fri filing -> Mon open
        spy = next(r for r in rets["congress:house_8000002_g1"] if r["start"] == "filing" and r["horizon"] == 1)
        self.assertEqual((spy["etf"], spy["excess"]), (None, None))
        self.assertIn("no SEC company for ticker SPY", spy["reason"])
        self.assertIsNotNone(spy["r_stock"])
        self.assertEqual({r["state"] for r in rets["congress:house_8000004_g1"]}, {"not_measurable"})
        self.assertEqual(ddb.get_trade("congress:house_8000001_g1")["issuer_cik"], "123456")
        self.assertEqual(ddb.job_get(svc.JOB_DAILY_CONGRESS)["state"], "done")

    def test_ticker_map_failure_waits(self):
        self.load([row(1)])

        def broken():
            raise svc.SecUnavailable("https://www.sec.gov/files/company_tickers.json: HTTP 503")
        out = svc.run_daily(datetime(2026, 10, 20, 22, tzinfo=UTC), prices=FakePrices(), source="congress",
                            job_key=svc.JOB_DAILY_CONGRESS, tickers=broken)
        self.assertEqual((out["state"], out["computed"], out["waiting_for_sic"]), ("partial", 0, 1))
        self.assertIn("tickers:", ddb.job_get(svc.JOB_DAILY_CONGRESS)["last_error"])
        self.assertEqual(ddb.returns_for(["congress:house_8000001_g1"])["congress:house_8000001_g1"], [])

    def test_company_tickers_cache_and_run_all(self):
        calls = []

        def get(url):
            calls.append(url)
            return json.dumps({"0": {"cik_str": 320193, "ticker": "aaa", "title": "Example"}}).encode()
        now = datetime(2026, 10, 20, 22, tzinfo=UTC)
        self.assertEqual(svc.company_tickers(now, get), {"AAA": "320193"})
        self.assertEqual(svc.company_tickers(now, get), {"AAA": "320193"})
        self.assertEqual(len(calls), 1)                                     # once a day
        with mock.patch.object(svc, "run_daily", side_effect=lambda *a, **k: k.get("source", "insider")) as rd:
            self.assertEqual(svc.run_all(now), {"insider": "insider", "congress": "congress"})
        self.assertEqual(rd.call_args_list[1].kwargs["job_key"], svc.JOB_DAILY_CONGRESS)


class TrackRecordAndMarkersTest(Case):
    def test_groups_and_markers(self):
        self.load([row(1), row(2, party="R", chamber="senate", source_id="senate_efd", is_late=1,
                              amount_range_low=50001, transaction_type="Sale (Full)"),
                   row(3, amount_range_low=1000001)])
        svc.run_daily(datetime(2026, 10, 20, 22, tzinfo=UTC), prices=FakePrices(), fetch=lambda c: {"sic": "3674"},
                      source="congress", job_key=svc.JOB_DAILY_CONGRESS, tickers=lambda: {"AAA": "1"})
        today = date(2026, 10, 20)
        g = lambda name: sorted(x["group"] for x in api.track_record_payload(
            {"source": "congress", "group": name, "horizon": "1"}, today)["groups"])
        self.assertEqual(g("chamber"), ["house", "senate"])
        self.assertEqual(g("party"), ["D", "R"])
        self.assertEqual(g("late"), ["late (> 45 days)", "on time"])
        self.assertEqual(g("amount"), ["$50k–250k", "> $250k", "≤ $15k"])
        with self.assertRaises(api.BadRequest):
            api.track_record_payload({"source": "congress", "group": "role"}, today)
        m = api.markers_payload({"ticker": "AAA", "from": "2026-10-01", "to": "2026-10-31", "kinds": "insider,congress"})
        self.assertEqual([(x["date"], x["kind"], x["side"], x["count"]) for x in m["markers"]],
                         [("2026-10-02", "congress", "buy", 2), ("2026-10-02", "congress", "sell", 1)])


class WatchAndAlertsTest(Case):
    def test_members_and_watch(self):
        self.load([row(1), row(2, filer_id="senate_john_example", filer_name="John Example", chamber="senate",
                              source_id="senate_efd")])
        self.assertEqual([m["id"] for m in congress_api.members_payload({})["members"]],
                         ["house_jane_example", "senate_john_example"])
        self.assertEqual(congress_api.watch_action("http://evil.example", {"members": []})[0], 403)
        self.assertEqual(congress_api.watch_action(None, {"members": ["house_nobody"]})[0], 400)
        self.assertEqual(congress_api.watch_action(None, "x")[0], 400)
        self.assertEqual(congress_api.watch_action(None, {"members": ["senate_john_example"]}),
                         (200, {"watched": ["senate_john_example"]}))
        self.assertTrue(next(m for m in congress_api.members_payload({"q": "john"})["members"])["watched"])

    def test_alert_kinds_are_monitoring_only(self):
        for kind in (al.KIND_MEMBER, al.KIND_HELD):
            self.assertIs(mon_alerts.KINDS[kind]["toast"], False)

    def test_on_new_trades(self):
        trades = self.load([row(1, filer_id="senate_john_example", filer_name="John Example", ticker="BBB",
                                chamber="senate", party="R", state="YY", source_id="senate_efd"), row(2)])
        emitted = []
        emit = lambda kind, title, body, key, **kw: (emitted.append((kind, key, body)), len(emitted))[1]
        n = al.on_new_trades(trades, held={"AAA": ["paper-main"]}, watched={"senate_john_example"}, emit=emit)
        self.assertEqual(n, 2)
        kinds = sorted((k, key) for k, key, _b in emitted)
        self.assertEqual(kinds, [(al.KIND_HELD, "disclosure:disclosure_held_ticker:congress:house_8000002_g1"),
                                 (al.KIND_MEMBER, "disclosure:disclosure_watched_member:congress:house_8000001_g1")])
        body = next(b for k, _key, b in emitted if k == al.KIND_MEMBER)
        self.assertIn("Congress: John Example (senate, R-YY) bought BBB ($1,001 - $15,000)", body)
        self.assertEqual(al.on_new_trades(trades, held={}, watched=set(), emit=lambda *a, **k: 1 / 0), 0)
        self.assertEqual(al.on_new_trades(trades, held={}, watched={"senate_john_example"},
                                          emit=lambda *a, **k: 1 / 0), 0)
        self.assertIn("ZeroDivisionError", al.STATE["last_error"])          # recorded, never raised

    def test_pull_alerts_only_new_rows_and_not_on_the_first_pull(self):
        seen = []
        with mock.patch.object(al, "on_new_trades", side_effect=lambda trades: seen.append([t["id"] for t in trades]) or 0):
            congress.pull(Fetch([row(1)]), datetime(2026, 10, 3, tzinfo=UTC))           # first pull: no alerts
            congress.pull(Fetch([row(1), row(2)], etag='"e2"'), datetime(2026, 10, 3, 6, tzinfo=UTC))
        self.assertEqual(seen, [["congress:house_8000002_g1"]])

    def test_live_form4_hook_alerts_new_trades(self):
        details = {"issuer_ticker": "AAA", "issuer_cik": "123", "owner_name": "Doe Jane", "owner_roles": ["CFO"],
                   "transactions": [{"code": "P", "derivative": False, "date": "2026-10-01", "shares": 1.0,
                                     "price": 2.0}]}
        event = {"id": "insider-0001234567-26-000099", "details": details, "filed_at": "2026-10-05T17:30:00-04:00",
                 "form": "4", "url": "https://www.sec.gov/x"}
        seen = []
        with mock.patch.object(al, "on_new_trades", side_effect=lambda trades: seen.append([t["id"] for t in trades]) or 0):
            svc.on_insider_event(event)
            svc.on_insider_event(event)                                     # repeated: not new any more
        self.assertEqual(seen, [["insider:0001234567-26-000099:P"], []])

    def test_status_carries_alert_state(self):
        self.assertIn("alerts", api.status_payload())


if __name__ == "__main__":
    unittest.main()
