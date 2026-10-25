"""INSIDER-RET slice (c) — the 2-year backfill (contract INSIDER_RET_API.md §5, decisions §11, 2026-10-24): data-set
links read from the SEC page (paths differ per quarter), data-set parsing (Form 4 / 4/A P-S rows only, dates
DD-MON-YYYY, 10b5-1 checkbox + footnote rule equal to the live collector's, co-reporters), acceptance times from the
issuer submissions (recent + older pages), the pilot that stops and asks, gap days from the daily index + .txt, the
price step (outside US market hours only, failing tickers retried later), start / pause / resume / restart, the SEC
limiter at 0.2 s only while EDGAR steps run, live rows never overwritten, and the POST control (403 for web pages,
409 for a wrong state). Offline: every sec.gov / Alpaca call is faked."""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import zipfile
from datetime import date, datetime, timezone
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from newsroom import db, disclosure_db as ddb, disclosure_returns as eng, disclosures_api as api  # noqa: E402
from newsroom import disclosures_backfill as bfm, disclosures_service as svc  # noqa: E402
from newsroom.sec_limiter import LIMITER  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 2, 0, tzinfo=UTC)          # Friday 22:00 New York: outside market hours


def _aiohttp_stub():
    if importlib.util.find_spec("aiohttp") is not None:
        return {}
    stub = types.ModuleType("aiohttp")
    stub.ClientSession, stub.ClientTimeout, stub.TCPConnector = object, (lambda **kw: None), (lambda **kw: None)
    return {"aiohttp": stub}


def tsv(header, rows):
    return "\t".join(header) + "\n" + "".join("\t".join(r) + "\n" for r in rows)


def dataset_zip():
    sub = tsv(["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERNAME", "ISSUERTRADINGSYMBOL",
               "AFF10B5ONE"],
              [["0000111111-26-000001", "03-AUG-2026", "4", "123456", "ACME INC", "ACME", "0"],
               ["0000111111-26-000002", "04-AUG-2026", "4", "123456", "ACME INC", "ACME", "1"],
               ["0000111111-26-000003", "05-AUG-2026", "4/A", "123456", "ACME INC", "ACME", "0"],
               ["0000111111-26-000004", "05-AUG-2026", "5", "123456", "ACME INC", "ACME", "0"],
               ["0000111111-26-000005", "06-AUG-2026", "4", "123456", "ACME INC", "ACME", "0"],
               ["0000111111-24-000006", "01-JUL-2024", "4", "123456", "ACME INC", "ACME", "0"]])
    trans = tsv(["ACCESSION_NUMBER", "TRANS_CODE", "TRANS_DATE", "TRANS_SHARES", "TRANS_PRICEPERSHARE"],
                [["0000111111-26-000001", "P", "31-JUL-2026", "100", "10"],
                 ["0000111111-26-000001", "P", "01-AUG-2026", "100", "12"],
                 ["0000111111-26-000002", "S", "03-AUG-2026", "50", "11"],
                 ["0000111111-26-000003", "P", "31-JUL-2026", "100", "10"],
                 ["0000111111-26-000004", "P", "31-JUL-2026", "100", "10"],
                 ["0000111111-26-000005", "M", "05-AUG-2026", "100", "1"],
                 ["0000111111-24-000006", "P", "28-JUN-2024", "1", "1"]])
    owners = tsv(["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE",
                  "RPTOWNER_TXT"],
                 [["0000111111-26-000001", "999", "Doe Jane", "Director,Officer", "Chief Executive Officer", ""],
                  ["0000111111-26-000001", "998", "Doe Trust", "TenPercentOwner", "", ""],
                  ["0000111111-26-000002", "997", "Roe Rick", "Officer", "CFO", ""],
                  ["0000111111-26-000003", "999", "Doe Jane", "Director", "", ""]])
    notes = tsv(["ACCESSION_NUMBER", "FOOTNOTE_ID", "FOOTNOTE_TXT"],
                [["0000111111-26-000003", "F1", "Effected pursuant to a Rule 10b5-1 trading plan adopted 2026-01-02."],
                 ["0000111111-26-000001", "F1", "These purchases were not made pursuant to a Rule 10b5-1 plan."]])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in (("SUBMISSION.tsv", sub), ("NONDERIV_TRANS.tsv", trans), ("REPORTINGOWNER.tsv", owners),
                           ("FOOTNOTES.tsv", notes)):
            zf.writestr("2026q3_form345/" + name, text)
    return buf.getvalue()


PAGE = ('<a href="https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/'
        '2026q3_form345.zip">2026 Q3</a> <a href="/files/structureddata/data/insider-transactions-data-sets/'
        '2025q4_form345.zip">2025 Q4</a>')
TXT = (b"<SEC-HEADER>\nACCESSION NUMBER: 0000222222-26-000009\nCONFORMED SUBMISSION TYPE: 4\n"
       b"<ACCEPTANCE-DATETIME>20261005163012\n</SEC-HEADER>\n<DOCUMENT>\n<TYPE>4\n<TEXT>\n<XML>\n"
       b"<?xml version=\"1.0\"?>\n<ownershipDocument><issuer><issuerCik>0000555555</issuerCik>"
       b"<issuerName>BETA CORP</issuerName><issuerTradingSymbol>BETA</issuerTradingSymbol></issuer>"
       b"<reportingOwner><reportingOwnerId><rptOwnerCik>0000444</rptOwnerCik><rptOwnerName>Poe Pat</rptOwnerName>"
       b"</reportingOwnerId><reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>"
       b"</reportingOwner><nonDerivativeTable><nonDerivativeTransaction><securityTitle><value>Common</value>"
       b"</securityTitle><transactionDate><value>2026-10-01</value></transactionDate><transactionCoding>"
       b"<transactionCode>P</transactionCode></transactionCoding><transactionAmounts><transactionShares>"
       b"<value>10</value></transactionShares><transactionPricePerShare><value>5</value>"
       b"</transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value>"
       b"</transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction></nonDerivativeTable>"
       b"</ownershipDocument>\n</XML>\n</TEXT>\n</DOCUMENT>\n")
IDX = (b"Form Type   Company Name      CIK         Date Filed  File Name\n"
       b"-----------------------------------------------------------------\n"
       b"4           BETA CORP         555555      20261005    edgar/data/555555/0000222222-26-000009.txt\n"
       b"4           Poe Pat           444         20261005    edgar/data/444/0000222222-26-000009.txt\n"
       b"10-Q        OTHER INC         1           20261005    edgar/data/1/0000000001-26-000001.txt\n")


def submissions(missing=False):
    recent = {"accessionNumber": ["0000111111-26-000001", "0000111111-26-000003"],
              "acceptanceDateTime": ["2026-08-03T16:31:22.000Z", "2026-08-05T21:00:00.000Z"]}
    return {"sic": "2834", "sicDescription": "Pharmaceutical Preparations", "tickers": ["ACME"],
            "filings": {"recent": recent if not missing else {"accessionNumber": [], "acceptanceDateTime": []},
                        "files": [{"name": "CIK0000123456-submissions-001.json", "filingFrom": "2020-01-01",
                                   "filingTo": "2026-08-04"}]}}


OLDER = {"accessionNumber": ["0000111111-26-000002"], "acceptanceDateTime": ["2026-08-04T12:00:00.000Z"]}


def series(base, step, first=date(2024, 9, 1), last=date(2027, 3, 31)):
    return {d: (base * (1 + step * k) * 0.999, base * (1 + step * k))
            for k, d in enumerate(calendars.trading_days(first, last))}


class FakePrices:
    def __init__(self, now=None, fail=()):
        self.fail = set(fail)
        self.data = {"ACME": series(10, 0.001), "BETA": series(5, 0.002), "XLV": series(100, 0.0005),
                     "XLK": series(100, 0.0005)}

    def daily(self, symbol, first, last):
        if symbol in self.fail:
            raise eng.PriceUnavailable(f"{symbol}: no data")
        return {d: v for d, v in self.data.get(symbol, {}).items() if first <= d <= last}

    def minutes(self, symbol, start, end):
        return []


class FakeSec:
    def __init__(self, missing=False, on=None):
        self.missing, self.on, self.calls, self.overridden = missing, on or {}, [], []

    def __call__(self, url):
        self.calls.append(url)
        self.overridden.append(LIMITER.overridden)
        if url in self.on:
            self.on[url]()
        if url == bfm.DATASETS_PAGE:
            return PAGE.encode()
        if url.endswith("2026q3_form345.zip"):
            return dataset_zip()
        if url.endswith("2025q4_form345.zip"):
            raise svc.SecUnavailable(url + ": HTTP 404")
        if url.endswith("CIK0000123456.json"):
            return json.dumps(submissions(self.missing)).encode()
        if url.endswith("CIK0000123456-submissions-001.json"):
            return json.dumps(OLDER if not self.missing else {}).encode()
        if url.endswith("CIK0000555555.json"):
            return json.dumps({"sic": "3674", "tickers": ["BETA"], "filings": {"recent": {}}}).encode()
        if "/daily-index/" in url:
            return IDX if url.endswith("form.20261005.idx") else b""
        if url.endswith("0000222222-26-000009.txt"):
            return TXT
        raise svc.SecUnavailable(url + ": HTTP 404")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="disc-bf-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(db.close)
        self.assertTrue(db.init(os.path.join(self.tmp, "newsroom.db"), legacy=""))
        patcher = mock.patch.dict(sys.modules, _aiohttp_stub())
        patcher.start()
        self.addCleanup(patcher.stop)

    def backfill(self, sec=None, prices=FakePrices, now=NOW):
        bf = bfm.Backfill(fetch=sec or FakeSec(), prices_factory=prices, clock=lambda: now, sleep=lambda s: None)
        return bf

    def run_to_end(self, bf, action="start"):
        ok, msg = bf.control(action)
        self.assertTrue(ok, msg)
        bf._thread.join(timeout=30)
        self.assertFalse(bf.running())
        return bf.state()


class ParsingTest(Case):
    def test_links_quarters_and_dates(self):
        links = bfm.dataset_links(PAGE)
        self.assertEqual(links[(2026, 3)].split("/files/")[1].split("/")[0], "datastandardsinnovation")
        self.assertEqual(links[(2025, 4)], "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/"
                                           "2025q4_form345.zip")
        self.assertEqual(bfm.quarters_for(date(2024, 10, 24), date(2026, 10, 24))[:2], [(2024, 4), (2025, 1)])
        self.assertEqual(len(bfm.quarters_for(date(2024, 10, 24), date(2026, 10, 24))), 9)
        self.assertEqual(bfm._dset_date("03-AUG-2026"), "2026-08-03")
        self.assertIsNone(bfm._dset_date("2026-08-03"))

    def test_parse_dataset(self):
        rows = {r["id"]: r for r in bfm.parse_dataset(dataset_zip(), since="2024-10-24")}
        self.assertEqual(sorted(rows), ["insider:0000111111-26-000001:P", "insider:0000111111-26-000002:S",
                                        "insider:0000111111-26-000003:P"])      # form 5, M-only, old: dropped
        p = rows["insider:0000111111-26-000001:P"]
        self.assertEqual((p["shares"], p["value_usd"], p["trade_date_first"], p["role_class"], p["co_reporters"]),
                         (200.0, 2200.0, "2026-07-31", "CEO", 1))
        self.assertEqual((p["plan_10b5_1"], p["acceptance_state"], p["origin"], p["issuer_name"]),
                         (0, "pending", "backfill", "ACME Inc"))     # negated footnote: not a plan; ticker kept upper
        self.assertEqual(rows["insider:0000111111-26-000002:S"]["plan_source"], "checkbox")
        a = rows["insider:0000111111-26-000003:P"]
        self.assertEqual((a["plan_source"], a["measurable"]), ("footnote", 0))   # amendment: stored, not measured

    def test_footnote_rule_equals_live_collector(self):
        with open(os.path.join(BACKEND, "newsroom", "sec_edgar_collector.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(f're.compile(r"{bfm._10B5_1_RE.pattern}", re.IGNORECASE)', src)
        self.assertIn(f're.compile(r"{bfm._10B5_1_NEGATED_RE.pattern}", re.IGNORECASE)', src)

    def test_acceptance_pages_and_gap_files(self):
        sec = FakeSec()
        found, doc = bfm.issuer_acceptance("0000123456", ["0000111111-26-000001", "0000111111-26-000002", "x"],
                                           "2024-10-24", lambda u: json.loads(sec(u)))
        self.assertEqual(found, {"0000111111-26-000001": "2026-08-03T16:31:22+00:00",
                                 "0000111111-26-000002": "2026-08-04T12:00:00+00:00"})
        self.assertEqual(doc["sic"], "2834")
        self.assertEqual(bfm.daily_index_paths(IDX), [("4", "edgar/data/555555/0000222222-26-000009.txt"),
                                                      ("4", "edgar/data/444/0000222222-26-000009.txt")])
        form, accepted, xml = bfm.parse_submission_txt(TXT)
        self.assertEqual((form, accepted), ("4", "2026-10-05T20:30:12+00:00"))   # 16:30:12 New York (EDT)
        self.assertTrue(xml.startswith(b"<?xml"))


class ControllerTest(Case):
    def test_full_run(self):
        live = svc.dtr.insider_trades("0000111111-26-000001", {"issuer_ticker": "ACME", "issuer_cik": "123456",
                                                               "transactions": [{"code": "P", "derivative": False,
                                                                                 "date": "2026-07-31", "shares": 1.0,
                                                                                 "price": 1.0}]},
                                      form="4", accepted_at="2026-08-03T16:31:22Z", filing_date="2026-08-03",
                                      origin="live")
        live[0]["filing_date"] = "2026-10-20"           # pretend the live store started on 2026-10-20
        ddb.upsert_trades(live)
        sec = FakeSec()
        bf = self.backfill(sec)
        state = self.run_to_end(bf)
        self.assertEqual(state["state"], "complete", state)
        self.assertTrue(all(sec.overridden))             # every EDGAR request ran with the 0.2 s backfill interval
        self.assertFalse(LIMITER.overridden)             # ... and the configured interval is back
        t1 = ddb.get_trade("insider:0000111111-26-000001:P")
        self.assertEqual((t1["origin"], t1["shares"]), ("live", 1.0))           # live row never overwritten
        t2 = ddb.get_trade("insider:0000111111-26-000002:S")
        self.assertEqual((t2["acceptance_state"], t2["accepted_at"]), ("found", "2026-08-04T12:00:00+00:00"))
        gap = ddb.get_trade("insider:0000222222-26-000009:P")
        self.assertEqual((gap["accepted_at"], gap["acceptance_state"], gap["ticker"]),
                         ("2026-10-05T20:30:12+00:00", "found", "BETA"))
        self.assertEqual(ddb.sic_get(555555)["sic"], 3674)
        rets = ddb.returns_for([t2["id"], gap["id"]])
        self.assertEqual(len(rets[t2["id"]]), 8)
        self.assertEqual(next(r for r in rets[gap["id"]] if r["start"] == "filing" and r["horizon"] == 1)["etf"],
                         "XLK")
        st = bfm.status()
        steps = {s["key"]: s for s in st["steps"]}
        self.assertEqual(steps["datasets"]["gaps"][0]["key"], "2025q4")          # recorded, not skipped silently
        self.assertTrue(all(s["state"] == "done" for s in st["steps"]))
        # gap days: 2026-09-30 (quarter-end cutoff) .. the first live day 2026-10-20
        self.assertIn("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260930.idx", sec.calls)
        self.assertIn("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261020.idx", sec.calls)
        self.assertNotIn("https://www.sec.gov/Archives/edgar/daily-index/2026/QTR4/form.20261021.idx", sec.calls)
        ok, msg = bf.control("start")                    # a second run repeats the steps cheaply
        bf._thread.join(timeout=30)
        self.assertEqual(bf.state()["state"], "complete")
        self.assertEqual(sum(1 for c in sec.calls if c.endswith("2026q3_form345.zip")), 1)   # not downloaded again

    def test_pilot_stops_and_asks(self):
        state = self.run_to_end(self.backfill(FakeSec(missing=True)))
        self.assertEqual(state["state"], "needs_decision")
        self.assertIn("acceptance time", state["last_error"])
        self.assertEqual(self.backfill().control("resume")[0], False)

    def test_pause_resume_and_restart(self):
        holder = {}
        sec = FakeSec(on={"https://data.sec.gov/submissions/CIK0000123456.json": lambda: holder["bf"]._pause.set()})
        bf = self.backfill(sec)
        holder["bf"] = bf
        self.assertEqual(self.run_to_end(bf)["state"], "paused")
        sec.on.clear()
        self.assertEqual(self.run_to_end(bf, "resume")["state"], "complete")
        ddb.job_put(bfm.STATE_KEY, "running", {})
        fresh = self.backfill()
        fresh.recover_after_restart()
        self.assertEqual((fresh.state()["state"], fresh.state()["last_error"]),
                         ("paused", "backend restarted — press Resume to continue"))
        self.assertEqual(fresh.control("pause"), (False, "the backfill is paused — nothing to pause"))
        self.assertFalse(fresh.control("explode")[0])

    def test_market_hours_and_failing_ticker(self):
        self.assertTrue(self.backfill(now=datetime(2026, 10, 23, 15, 0, tzinfo=UTC))._market_blocked())   # 11:00 NY
        self.assertFalse(self.backfill(now=datetime(2026, 10, 24, 15, 0, tzinfo=UTC))._market_blocked())  # Saturday
        state = self.run_to_end(self.backfill(prices=lambda now: FakePrices(fail={"BETA"})))
        self.assertEqual(state["state"], "complete")
        steps = {s["key"]: s for s in bfm.status()["steps"]}
        self.assertEqual([g["key"] for g in steps["prices"]["gaps"]], ["BETA"])
        self.assertEqual(ddb.returns_for(["insider:0000222222-26-000009:P"])["insider:0000222222-26-000009:P"], [])


class ApiTest(Case):
    def test_backfill_action(self):
        with mock.patch.object(bfm, "BACKFILL", self.backfill()):
            self.assertEqual(api.backfill_action("http://evil.example", {"action": "start"})[0], 403)
            self.assertEqual(api.backfill_action(None, ["start"])[0], 400)
            self.assertEqual(api.backfill_action(None, {"action": "resume"})[0], 409)
            code, payload = api.backfill_action(None, {"action": "start"})
            self.assertEqual((code, payload["message"]), (200, "running"))
            bfm.BACKFILL._thread.join(timeout=30)
            self.assertEqual(api.status_payload()["backfill"]["state"], "complete")
            self.assertEqual(api.source_state()["backfill"], "complete")


if __name__ == "__main__":
    unittest.main()
