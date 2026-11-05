"""CONGRESS slice (a) — collector + storage + read routes (contract CONGRESS_API.md, decisions §10, 2026-10-24/25):
row mapping (scope House + Senate, OGE dropped, purchase / sale / other types, stock + ETF measurable only, owner
types, late flag, http(s) links only, rejected rows counted), the conditional pull (ETag, 304 = unchanged, errors
keep the rows), staleness after 3 calendar days, newsroom.db v5, and the list / detail payloads with their 400s.
All rows are SYNTHETIC (invented members and amounts) — kadoa data is fetched at runtime only and never committed."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from newsroom import congress, congress_api, db, disclosure_db as ddb, disclosures_api  # noqa: E402

UTC = timezone.utc


def row(i, **kw):
    r = {"id": f"house_9{i:06d}_g1", "filing_id": f"house_9{i:06d}", "filer_id": "house_jane_example",
         "source_id": "house_clerk", "transaction_date": "2026-10-01", "notification_date": "2026-10-05",
         "filing_date": "2026-10-07", "owner": "SP", "ticker": "AAA", "asset_name": "Example Corp Common Stock",
         "asset_type": "ST", "transaction_type": "Purchase", "amount_range_low": 1001, "amount_range_high": 15000,
         "amount_range_label": "$1,001 - $15,000", "comment": None, "is_late": 0, "days_to_file": 6,
         "ingested_at": "2026-10-09 13:31:43", "doc_url": "https://disclosures.example.invalid/ptr/9.pdf",
         "filing_type": "PTR", "filer_name": "Jane Example", "chamber": "house", "party": "D", "state": "XX",
         "office": "XX-01", "ret_since": 4.7, "excess_since": 6.1}
    r.update(kw)
    return r


class FakeFetch:
    def __init__(self, payload, etag='"v1"'):
        self.payload, self.etag, self.calls = payload, etag, []

    def __call__(self, url, etag=None):
        self.calls.append((url, etag))
        if isinstance(self.payload, Exception):
            raise self.payload
        if etag and etag == self.etag:
            return 304, etag, b""
        return 200, self.etag, json.dumps(self.payload).encode()


class MappingTest(unittest.TestCase):
    def test_map_rows(self):
        rows = [row(1), row(2, transaction_type="Sale (Partial)", owner=None, is_late=1, days_to_file=50),
                row(3, transaction_type="Sale (Full)", owner="JT"), row(4, asset_type="OP"),
                row(5, transaction_type="Exchange"), row(6, ticker="--"), row(7, source_id="oge_executive"),
                row(8, id=""), row(9, source_id="senate_efd", chamber=None, doc_url="javascript:alert(1)"),
                "not a row"]
        mapped, skipped = congress.map_rows(rows)
        by = {m["id"]: m for m in mapped}
        p = by["congress:house_9000001_g1"]
        self.assertEqual((p["source"], p["code"], p["measurable"], p["owner_type"], p["owner_cik"], p["owner_name"]),
                         ("congress", "P", 1, "spouse", "house_jane_example", "Jane Example"))
        self.assertEqual((p["amount_low"], p["amount_high"], p["amount_label"], p["is_late"], p["days_to_file"]),
                         (1001.0, 15000.0, "$1,001 - $15,000", 0, 6))
        self.assertEqual((p["trade_date_first"], p["filing_date"], p["accepted_at"]), ("2026-10-01", "2026-10-07", None))
        self.assertNotIn("ret_since", p)                                         # kadoa's returns are ignored
        s = by["congress:house_9000002_g1"]
        self.assertEqual((s["code"], s["owner_type"], s["is_late"]), ("S", "self", 1))
        self.assertEqual(by["congress:house_9000003_g1"]["owner_type"], "joint")
        self.assertEqual(by["congress:house_9000004_g1"]["not_measurable_reason"], "not measurable (asset type OP)")
        x = by["congress:house_9000005_g1"]
        self.assertEqual((x["code"], x["measurable"]), ("X", 0))
        self.assertEqual(by["congress:house_9000006_g1"]["not_measurable_reason"], "no ticker")
        sen = by["congress:house_9000009_g1"]
        self.assertEqual((sen["chamber"], sen["official_url"]), ("senate", None))   # chamber from source, no js: link
        self.assertEqual(dict(skipped), {"out of scope": 1, "missing id / filing_date / filer_id": 1,
                                         "not an object": 1})

    def test_filer_file_rows_take_chamber_from_the_filer(self):
        filer = {"id": "senate_john_example", "full_name": "John Example", "chamber": "senate", "party": "R",
                 "state": "YY", "office": None}
        r = row(1, source_id="senate_efd", filer_id=None, filer_name=None, chamber=None, party=None, state=None)
        mapped, skipped = congress.map_rows([r], filer, origin="backfill")
        m = mapped[0]
        self.assertEqual((m["owner_cik"], m["owner_name"], m["chamber"], m["party"], m["state"], m["origin"]),
                         ("senate_john_example", "John Example", "senate", "R", "YY", "backfill"))

    def test_staleness_calendar_days(self):
        now = datetime(2026, 10, 25, 12, tzinfo=UTC)
        self.assertFalse(congress.stale("2026-10-22 23:00:00", now))     # 3 days: fine (single skipped days normal)
        self.assertTrue(congress.stale("2026-10-21 23:00:00", now))
        self.assertTrue(congress.stale(None, now))


class PullAndApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="congress-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(db.close)
        self.assertTrue(db.init(os.path.join(self.tmp, "newsroom.db"), legacy=""))
        self.now = datetime(2026, 10, 10, 12, tzinfo=UTC)

    def test_schema_v5(self):
        self.assertEqual(db.SCHEMA_VERSION, 5)
        self.assertIn("disclosure_watch", db.TABLES)
        self.assertIn("comment", ddb.TRADE_COLUMNS)

    def test_pull_etag_and_errors(self):
        fetch = FakeFetch([row(1), row(2, transaction_type="Sale (Partial)"), row(3, source_id="oge_executive")])
        p = congress.pull(fetch, self.now)
        self.assertEqual((p["upserted"], p["skipped"], p["etag"], p["updated_through"]),
                         (2, {"out of scope": 1}, '"v1"', "2026-10-09 13:31:43"))
        self.assertEqual(fetch.calls[0], (congress.BASE_URL + "trades.json", None))
        p2 = congress.pull(fetch, self.now)                                    # conditional: 304 -> unchanged
        self.assertTrue(p2["unchanged"])
        self.assertEqual(fetch.calls[1][1], '"v1"')
        self.assertEqual(ddb.counts("congress")["trades"], 2)
        with self.assertRaises(congress.SourceError):
            congress.pull(FakeFetch({"oops": 1}, etag='"v2"'), self.now)
        with self.assertRaises(congress.SourceError):
            congress.pull(FakeFetch(congress.SourceError("https://x: HTTP 503")), self.now)
        self.assertEqual(ddb.counts("congress")["trades"], 2)                 # rows kept
        st = congress.status(datetime(2026, 10, 20, tzinfo=UTC))
        self.assertEqual((st["rows"], st["stale"], st["asset_types"], st["measurable_asset_types"]),
                         (2, True, {"ST": 2}, ["ST", "EF"]))
        self.assertIn("Ethics in Government Act", st["terms"])
        self.assertIn("congress", disclosures_api.status_payload())

    def test_list_and_detail(self):
        congress.pull(FakeFetch([row(1), row(2, transaction_type="Sale (Full)", party="R", chamber="senate",
                                             source_id="senate_efd", is_late=1, comment="Spouse account."),
                                 row(3, asset_type="OP", ticker="BBB")]), self.now)
        today = date(2026, 10, 20)
        out = congress_api.list_payload({}, today)
        self.assertEqual((out["total_all"], out["total_matching"], out["shown"]), (3, 3, 3))
        self.assertTrue(out["source"]["stale"] is False or out["source"]["stale"] is True)
        r = next(x for x in out["rows"] if x["id"] == "congress:house_9000001_g1")
        self.assertEqual((r["member_name"], r["chamber"], r["party"], r["code"], r["amount_label"], r["chip"]),
                         ("Jane Example", "house", "D", "P", "$1,001 - $15,000", None))   # returns come in slice (b)
        self.assertEqual(congress_api.list_payload({"chamber": "senate"}, today)["total_matching"], 1)
        self.assertEqual(congress_api.list_payload({"late": "late"}, today)["total_matching"], 1)
        self.assertEqual(congress_api.list_payload({"party": "R"}, today)["total_matching"], 1)
        self.assertEqual(congress_api.list_payload({"measurable": "yes"}, today)["total_matching"], 2)
        self.assertEqual(congress_api.list_payload({"member": "house_jane_example"}, today)["total_matching"], 3)
        for bad in ({"chamber": "lords"}, {"party": "X"}, {"late": "maybe"}, {"member": "x'; drop"},
                    {"days": "0"}, {"ticker": "A B"}):
            with self.assertRaises(disclosures_api.BadRequest, msg=str(bad)):
                congress_api.list_payload(bad, today)
        t = ddb.get_trade("congress:house_9000002_g1")
        d = congress_api.detail(t, [])
        self.assertEqual((d["comment"], d["asset_type"], d["official_url"][:8]), ("Spouse account.", "ST", "https://"))
        self.assertIn(congress.TERMS, d["notes"])
        self.assertTrue(congress_api.TRADE_ID_RE.match("congress:house_9000002_g1"))
        self.assertFalse(congress_api.TRADE_ID_RE.match("congress:x' OR 1=1"))


if __name__ == "__main__":
    unittest.main()
