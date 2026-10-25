"""INSIDER-RET slice (a) — storage + return engine (contract INSIDER_RET_API.md, accepted 2026-10-24 21:05):
the SIC -> sector ETF map (one editable file, first match wins, every rule reachable, no SPY substitute), the trade
builder (P/S aggregation, amendments not measured, role class, point-in-time clusters), the entry rules of both start
points, horizons / running / settle, ETF window, excess and score signs, the chip and the Track-record statistics,
the Alpaca price adapter (fresh-through rule, errors), and the newsroom.db v4 tables + 2-year retention. Offline."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from newsroom import db, disclosure_db as ddb, disclosure_returns as eng, disclosure_trades as dt, sector_map  # noqa: E402
from strategy_platform.core import calendars  # noqa: E402
from strategy_platform.core.errors import DataSourceUnavailable  # noqa: E402

UTC = timezone.utc


def ts(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class FakePrices:
    """Daily (open, close) per trading day from a linear series; minute bars as given; every call recorded."""

    def __init__(self, daily=None, minutes=None):
        self.daily_data, self.minute_data, self.calls = daily or {}, minutes or {}, []

    def daily(self, symbol, first, last):
        self.calls.append(("daily", symbol, first, last))
        return {d: v for d, v in self.daily_data.get(symbol, {}).items() if first <= d <= last}

    def minutes(self, symbol, start, end):
        self.calls.append(("minutes", symbol, start, end))
        return [(t, p) for t, p in self.minute_data.get(symbol, []) if start <= t < end]


def series(base, step, first=date(2026, 9, 1), last=date(2027, 3, 31), skip=()):
    out = {}
    for k, d in enumerate(calendars.trading_days(first, last)):
        if d in skip:
            continue
        out[d] = (base * (1 + step * k) * 0.999, base * (1 + step * k))
    return out


def trade(**kw):
    t = {"id": "insider:0001-26-000001:P", "source": "insider", "form": "4", "code": "P", "ticker": "AAA",
         "accepted_at": "2026-10-05T14:10:30+00:00", "filing_date": "2026-10-05", "trade_date_first": "2026-10-01",
         "measurable": 1, "not_measurable_reason": None}
    t.update(kw)
    return t


class SectorMapTest(unittest.TestCase):
    def test_lookups_and_no_substitute(self):
        self.assertEqual(sector_map.etf_for(2834), ("XLV", None))
        self.assertEqual(sector_map.etf_for("2810"), ("XLB", None))
        self.assertEqual(sector_map.etf_for(7372), ("XLK", None))
        self.assertEqual(sector_map.etf_for(3674), ("XLK", None))
        self.assertEqual(sector_map.etf_for(6022), ("XLF", None))
        self.assertEqual(sector_map.etf_for(6798), ("XLRE", None))
        self.assertEqual(sector_map.etf_for(1311), ("XLE", None))
        self.assertEqual(sector_map.etf_for(3711), ("XLY", None))
        self.assertEqual(sector_map.etf_for(6770), (None, "no sector ETF for SIC 6770"))
        self.assertEqual(sector_map.etf_for(9999), (None, "no sector ETF for SIC 9999"))
        self.assertEqual(sector_map.etf_for(1100), (None, "no sector ETF for SIC 1100"))     # unmapped: no guess
        self.assertEqual(sector_map.etf_for(None), (None, "SIC code unknown"))
        self.assertNotIn("SPY", json.dumps(sector_map.table()[2]))

    def test_file_valid_and_every_rule_reachable(self):
        rules, source, etfs = sector_map.table()
        self.assertIn("approximation", source)
        self.assertEqual(set(etfs), {"XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"})
        for i, (lo, hi, _etf, label) in enumerate(rules):
            covered = set()
            for plo, phi, _e, _l in rules[:i]:
                covered.update(range(plo, phi + 1))
            self.assertTrue(set(range(lo, hi + 1)) - covered, f"rule {label} is shadowed by earlier rules")
        self.assertIn("approximation", sector_map.CAVEAT)

    def test_validation_errors(self):
        good = {"schema": 1, "source": "x", "etfs": ["XLK"], "rules": [{"sic_from": 1, "sic_to": 2, "etf": "XLK", "label": "a"}]}
        self.assertEqual(sector_map.validate(good), [(1, 2, "XLK", "a")])
        for bad in ({**good, "schema": 2}, {**good, "source": ""},
                    {**good, "rules": [{"sic_from": 3, "sic_to": 2, "etf": "XLK", "label": "a"}]},
                    {**good, "rules": [{"sic_from": 1, "sic_to": 2, "etf": "SPY", "label": "a"}]},
                    {**good, "rules": [{"sic_from": 1, "sic_to": 2, "etf": "XLK", "label": ""}]}):
            with self.assertRaises(sector_map.SectorMapError):
                sector_map.validate(bad)


class TradeBuilderTest(unittest.TestCase):
    DETAILS = {"issuer_name": "Acme Inc", "issuer_ticker": "acme", "issuer_cik": "0000123456",
               "owner_name": "Doe Jane", "owner_cik": "0000999", "owner_roles": ["Director", "Chief Executive Officer"],
               "plan_10b5_1": False, "plan_10b5_1_source": None,
               "transactions": [
                   {"code": "P", "derivative": False, "date": "2026-10-01", "shares": 100.0, "price": 10.0},
                   {"code": "P", "derivative": False, "date": "2026-10-02", "shares": 300.0, "price": 12.0},
                   {"code": "S", "derivative": False, "date": "2026-10-02", "shares": 50.0, "price": None},
                   {"code": "M", "derivative": False, "date": "2026-10-02", "shares": 999.0, "price": 1.0},
                   {"code": "P", "derivative": True, "date": "2026-10-02", "shares": 999.0, "price": 1.0}]}

    def test_aggregation_per_code(self):
        rows = dt.insider_trades("0001234567-26-000042", self.DETAILS, form="4",
                                 accepted_at="2026-10-05T10:10:30-04:00", filing_date="2026-10-05", origin="live",
                                 now="2026-10-05T15:00:00+00:00")
        self.assertEqual([r["code"] for r in rows], ["P", "S"])
        p, s = rows
        self.assertEqual((p["shares"], p["value_usd"], p["avg_price"]), (400.0, 4600.0, 11.5))
        self.assertEqual((p["trade_date_first"], p["trade_date_last"]), ("2026-10-01", "2026-10-02"))
        self.assertEqual(p["accepted_at"], "2026-10-05T14:10:30+00:00")
        self.assertEqual((p["ticker"], p["issuer_cik"], p["role_class"], p["measurable"]), ("ACME", "123456", "CEO", 1))
        self.assertEqual(p["official_url"],
                         "https://www.sec.gov/Archives/edgar/data/123456/000123456726000042/0001234567-26-000042-index.htm")
        self.assertEqual((s["shares"], s["value_usd"], s["avg_price"]), (50.0, None, None))
        self.assertEqual(p["id"], "insider:0001234567-26-000042:P")

    def test_amendment_and_ticker_not_measured(self):
        amend = dt.insider_trades("0001234567-26-000043", self.DETAILS, form="4/A", accepted_at=None,
                                  filing_date="2026-10-06", origin="live")
        self.assertTrue(all(r["measurable"] == 0 and r["not_measurable_reason"] == dt.AMENDMENT_REASON for r in amend))
        none = dt.insider_trades("0001234567-26-000044", {**self.DETAILS, "issuer_ticker": "NONE"}, form="4",
                                 accepted_at=None, filing_date="2026-10-06", origin="live")
        self.assertEqual({r["not_measurable_reason"] for r in none}, {"no ticker"})
        with self.assertRaises(ValueError):
            dt.insider_trades("x", self.DETAILS, form="5", accepted_at=None, filing_date="2026-10-06", origin="live")

    def test_role_class(self):
        self.assertEqual(dt.role_class(["Director", "President & CEO"]), "CEO")
        self.assertEqual(dt.role_class(["EVP, Principal Financial Officer"]), "CFO")
        self.assertEqual(dt.role_class(["Director", "President"]), "Director")
        self.assertEqual(dt.role_class(["10% owner"]), "10% owner")
        self.assertEqual(dt.role_class(["10% owner", "Director"]), "Director")
        self.assertEqual(dt.role_class(["General Counsel"]), "Other")
        self.assertEqual(dt.role_class([]), "Other")

    def test_clusters_point_in_time(self):
        def row(i, owner, day, code="P", filed=None, planned=0, form="4"):
            return {"id": f"t{i}", "source": "insider", "form": form, "code": code, "issuer_cik": "1",
                    "owner_cik": owner, "trade_date_first": day, "plan_10b5_1": planned,
                    "accepted_at": filed or f"{day}T20:00:00+00:00"}
        rows = [row(1, "a", "2026-10-01"), row(2, "b", "2026-10-10"), row(3, "c", "2026-10-30"),
                row(4, "a", "2026-10-11", form="4/A"),
                row(5, "x", "2026-10-01", "S"), row(6, "y", "2026-10-02", "S"), row(7, "z", "2026-10-03", "S", planned=1),
                row(8, "w", "2026-10-04", "S")]
        ids = dt.cluster_ids(rows)
        # buy: t2 sees a + b within 14 days -> cluster; t1 does NOT become one later (no look-ahead); t3 too late
        self.assertEqual(ids, {"t2", "t8"})          # sell: x, y, w (z planned) -> 3 owners at t8


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.prices = FakePrices(
            daily={"AAA": series(10.0, 0.01), "XLK": series(100.0, 0.005)},
            minutes={"AAA": [(ts("2026-10-05T14:10:00Z"), 9.0), (ts("2026-10-05T14:12:00Z"), 10.0)],
                     "XLK": [(ts("2026-10-05T14:11:00Z"), 99.0), (ts("2026-10-05T14:12:00Z"), 100.0)]})
        self.now = ts("2026-10-13T00:00:00Z")

    def rows(self, t, etf="XLK", reason=None, prices=None, now=None):
        out = eng.compute(t, etf, reason, prices or self.prices, now or self.now)
        return {(r["start"], r["horizon"]): r for r in out}

    def test_minute_entry_horizons_and_excess(self):
        r = self.rows(trade())
        f1 = r[("filing", 1)]
        self.assertEqual((f1["entry_basis"], f1["entry_price"], f1["etf_entry_price"]), ("minute", 10.0, 100.0))
        self.assertEqual(f1["entry_ts"], "2026-10-05T14:12:00+00:00")       # the 14:10 bar is the acceptance minute
        d1 = calendars.next_trading_day(date(2026, 10, 5))
        c1 = self.prices.daily_data["AAA"][d1][1]
        x1 = self.prices.daily_data["XLK"][d1][1]
        self.assertEqual((f1["state"], f1["exit_date"]), ("complete", d1.isoformat()))
        self.assertAlmostEqual(f1["r_stock"], c1 / 10.0 - 1)
        self.assertAlmostEqual(f1["excess"], (c1 / 10.0 - 1) - (x1 / 100.0 - 1))
        self.assertAlmostEqual(f1["score"], f1["excess"])
        self.assertEqual(r[("filing", 5)]["state"], "complete")
        self.assertEqual((r[("filing", 20)]["state"], r[("filing", 20)]["sessions_done"]), ("running", 5))
        self.assertEqual(r[("filing", 60)]["sessions_done"], 5)
        t1 = r[("trade_date", 1)]
        self.assertEqual((t1["entry_basis"], t1["entry_price"]), ("close", self.prices.daily_data["AAA"][date(2026, 10, 1)][1]))

    def test_sale_score_reversed(self):
        r = self.rows(trade(code="S"))
        self.assertAlmostEqual(r[("filing", 1)]["score"], -r[("filing", 1)]["excess"])

    def test_entry_rules(self):
        p = self.prices
        e = eng.filing_entry(trade(accepted_at="2026-10-05T21:30:00Z"), p)          # after the close
        self.assertEqual((e.session, e.basis, e.kind), (date(2026, 10, 6), "next_open", "open"))
        e = eng.filing_entry(trade(accepted_at="2026-10-05T12:00:00Z"), p)          # before the open
        self.assertEqual((e.session, e.basis), (date(2026, 10, 5), "next_open"))
        e = eng.filing_entry(trade(accepted_at="2026-10-03T15:00:00Z"), p)          # Saturday
        self.assertEqual((e.session, e.basis), (date(2026, 10, 5), "next_open"))
        e = eng.filing_entry(trade(accepted_at="2026-10-05T19:59:10Z"), p)          # last minute: no bar after it
        self.assertEqual((e.session, e.basis), (date(2026, 10, 6), "next_open_no_trade"))
        e = eng.filing_entry(trade(accepted_at="2026-11-27T18:30:00Z"), p)          # 13:30 EST, early close 13:00
        self.assertEqual((e.session, e.basis), (date(2026, 11, 30), "next_open"))
        self.assertEqual(eng.filing_entry(trade(accepted_at=None), p).reason, "acceptance time unknown")
        e = eng.filing_entry({"source": "congress", "filing_date": "2026-10-09"}, p)  # Friday -> Monday open
        self.assertEqual((e.session, e.basis), (date(2026, 10, 12), "next_open_after_filing_date"))
        e = eng.trade_date_entry(trade(trade_date_first="2026-10-04"))              # Sunday -> Friday close
        self.assertEqual((e.session, e.basis, e.kind), (date(2026, 10, 2), "prev_close_non_trading_day", "close"))

    def test_open_entry_uses_daily_open_for_both(self):
        r = self.rows(trade(accepted_at="2026-10-05T21:30:00Z"))
        f = r[("filing", 1)]
        d = date(2026, 10, 6)
        self.assertEqual((f["entry_price"], f["etf_entry_price"]),
                         (self.prices.daily_data["AAA"][d][0], self.prices.daily_data["XLK"][d][0]))
        self.assertEqual(f["exit_date"], "2026-10-07")

    def test_no_etf_unknown_acceptance_and_not_measurable(self):
        r = self.rows(trade(accepted_at=None), etf=None, reason="no sector ETF for SIC 6770")
        self.assertEqual({r[("filing", h)]["state"] for h in eng.HORIZONS}, {"not_measurable"})
        t1 = r[("trade_date", 1)]
        self.assertEqual((t1["state"], t1["excess"], t1["score"], t1["reason"]),
                         ("complete", None, None, "no sector ETF for SIC 6770"))
        self.assertIsNotNone(t1["r_stock"])
        r = self.rows(trade(measurable=0, not_measurable_reason="no ticker"))
        n_calls = len(self.prices.calls)
        self.assertEqual({x["reason"] for x in r.values()}, {"no ticker"})
        self.rows(trade(measurable=0, not_measurable_reason="no ticker"))
        self.assertEqual(len(self.prices.calls), n_calls)              # an unmeasurable trade fetches nothing

    def test_missing_exit_bar_and_settle(self):
        gap = date(2026, 10, 6)
        prices = FakePrices(daily={"AAA": series(10.0, 0.01, skip={gap}), "XLK": series(100.0, 0.005)},
                            minutes=self.prices.minute_data)
        r = self.rows(trade(), prices=prices)
        self.assertEqual((r[("filing", 1)]["state"], r[("filing", 1)]["reason"]),
                         ("not_measurable", "no price bar for AAA on 2026-10-06"))
        just_after_close = ts("2026-10-06T20:10:00Z")                           # inside the 20-min settle time
        r = self.rows(trade(), now=just_after_close)
        self.assertEqual((r[("filing", 1)]["state"], r[("filing", 1)]["sessions_done"]), ("running", 0))
        future = self.rows(trade(accepted_at="2026-10-12T21:00:00Z"), now=ts("2026-10-12T22:00:00Z"))
        self.assertEqual(future[("filing", 1)]["reason"], "entry not reached yet")

    def test_chip_and_stats(self):
        rows = eng.compute(trade(), "XLK", None, self.prices, self.now)
        chip = eng.chip(rows)
        self.assertEqual((chip["horizon"], chip["state"], chip["etf"]), (5, "complete", "XLK"))
        running = eng.compute(trade(), "XLK", None, self.prices, ts("2026-10-05T18:00:00Z"))
        self.assertEqual((eng.chip(running)["horizon"], eng.chip(running)["state"]), (1, "running"))
        stats = eng.group_stats(rows + [dict(r, trade_id="b") for r in rows], {rows[0]["trade_id"]: "P", "b": "S"}, 5)
        self.assertEqual([(g["group"], g["n"], g["small_sample"]) for g in stats], [("P", 1, True), ("S", 1, True)])
        self.assertEqual(stats[0]["hit_rate"], 1.0 if stats[0]["mean_score"] > 0 else 0.0)
        self.assertIn("approximation", eng.TRACK_RECORD_CAVEAT)


class AlpacaPricesTest(unittest.TestCase):
    def test_daily_fresh_rule_and_errors(self):
        now = ts("2026-10-13T00:00:00Z")
        bars = [{"ts": "2026-10-09T04:00:00Z", "open": 1.0, "close": 2.0},
                {"ts": "2026-10-12T04:00:00Z", "open": 3.0, "close": 4.0}]
        with mock.patch("strategy_platform.data.cache.get_bars", return_value=bars) as gb:
            out = eng.AlpacaPrices(now).daily("AAA", date(2026, 10, 9), date(2026, 10, 12))
        self.assertEqual(out, {date(2026, 10, 9): (1.0, 2.0), date(2026, 10, 12): (3.0, 4.0)})
        kw = gb.call_args.kwargs
        self.assertEqual((kw["adjustment"], kw["require_fresh_through"]),
                         ("all", calendars.last_close_before(now)))     # the 12th has closed: it must be included
        with mock.patch("strategy_platform.data.cache.get_bars", return_value=bars) as gb:
            eng.AlpacaPrices(now).daily("AAA", date(2026, 10, 1), date(2026, 10, 9))
        self.assertIsNone(gb.call_args.kwargs["require_fresh_through"])  # older range: plain history
        with mock.patch("strategy_platform.data.cache.get_bars",
                        side_effect=DataSourceUnavailable("alpaca", "equity_bars", "down")):
            with self.assertRaises(eng.PriceUnavailable):
                eng.AlpacaPrices(now).daily("AAA", date(2026, 10, 9), date(2026, 10, 12))

    def test_minutes(self):
        with mock.patch("strategy_platform.data.registry.fetch",
                        return_value=[{"ts": "2026-10-05T14:12:00Z", "open": 10.0},
                                      {"ts": "2026-10-05T14:11:00Z", "open": 9.5}]) as f:
            out = eng.AlpacaPrices().minutes("AAA", ts("2026-10-05T14:11:00Z"), ts("2026-10-05T20:00:00Z"))
        self.assertEqual(f.call_args.args[:4], ("equity_bars", "get_bars", "AAA", "1Min"))
        self.assertEqual([p for _t, p in out], [9.5, 10.0])


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="disc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(db.close)
        self.assertTrue(db.init(os.path.join(self.tmp, "newsroom.db"), legacy=""))

    def test_tables_upsert_query_returns_jobs_sic(self):
        self.assertTrue({"disclosure_trades", "disclosure_returns", "issuer_sic", "disclosure_jobs"} <= set(db.TABLES))
        rows = dt.insider_trades("0001234567-26-000042", TradeBuilderTest.DETAILS, form="4",
                                 accepted_at="2026-10-05T14:10:30Z", filing_date="2026-10-05", origin="live",
                                 now="2026-10-05T15:00:00+00:00")
        self.assertEqual(ddb.upsert_trades(rows), 2)
        ddb.set_clusters("insider", [rows[0]["id"]])
        again = [dict(r, first_seen="2026-10-06T00:00:00+00:00", updated_at="2026-10-06T00:00:00+00:00") for r in rows]
        ddb.upsert_trades(again)                                       # first_seen and cluster survive an update
        got = ddb.get_trade(rows[0]["id"])
        self.assertEqual((got["first_seen"], got["cluster"], got["updated_at"]),
                         ("2026-10-05T15:00:00+00:00", 1, "2026-10-06T00:00:00+00:00"))
        found, total, total_all = ddb.query_trades("insider", [("code = ?", "P")])
        self.assertEqual((len(found), total, total_all), (1, 1, 2))
        with self.assertRaises(ValueError):
            ddb.upsert_trades([{"id": "x"}])
        prices = FakePrices(daily={"ACME": series(10.0, 0.01), "XLK": series(100.0, 0.005)})
        ret = eng.compute(dict(got), "XLK", None, prices, ts("2026-10-13T00:00:00Z"))
        self.assertEqual(ddb.replace_returns(got["id"], ret), 8)
        self.assertEqual(len(ddb.returns_for([got["id"]])[got["id"]]), 8)
        self.assertEqual(len(ddb.returns_rows("insider", "filing", 1)), 1)
        self.assertEqual([t["id"] for t in ddb.trades_to_compute("insider", "2026-01-01")],
                         sorted([rows[0]["id"], rows[1]["id"]]))          # running rows / no rows yet
        with self.assertRaises(ValueError):
            ddb.replace_returns("other", ret)
        ddb.job_put("returns:daily", "done", {"n": 2})
        self.assertEqual(ddb.job_get("returns:daily")["progress"], {"n": 2})
        self.assertEqual([j["key"] for j in ddb.jobs("returns:")], ["returns:daily"])
        ddb.sic_put("0000123456", "2834", "Pharmaceutical Preparations", ["ACME"], fetched_at="2026-10-01T00:00:00+00:00")
        self.assertEqual(ddb.sic_get(123456)["sic"], 2834)
        self.assertFalse(ddb.sic_stale(123456, now=ts("2026-11-01T00:00:00Z")))
        self.assertTrue(ddb.sic_stale(123456, now=ts("2027-01-15T00:00:00Z")))
        self.assertEqual(ddb.counts("insider")["trades"], 2)

    def test_backfill_rows_pending_acceptance_and_keep_existing(self):
        live = dt.insider_trades("0001234567-26-000070", TradeBuilderTest.DETAILS, form="4",
                                 accepted_at="2026-10-05T14:10:30Z", filing_date="2026-10-05", origin="live")
        ddb.upsert_trades(live)
        filled = dt.insider_trades("0001234567-26-000070", TradeBuilderTest.DETAILS, form="4", accepted_at=None,
                                   filing_date="2026-10-05", origin="backfill")
        pend = dt.insider_trades("0001234567-25-000001", TradeBuilderTest.DETAILS, form="4", accepted_at=None,
                                 filing_date="2025-06-02", origin="backfill")
        self.assertEqual({r["acceptance_state"] for r in pend}, {"pending"})
        self.assertEqual({r["acceptance_state"] for r in live}, {"found"})
        ddb.upsert_trades(filled + pend, keep_existing=True)
        self.assertEqual(ddb.get_trade(live[0]["id"])["accepted_at"], "2026-10-05T14:10:30+00:00")   # live kept
        self.assertEqual(ddb.pending_acceptance(), {"123456": ["0001234567-25-000001"]})
        daily = {t["id"] for t in ddb.trades_to_compute("insider", "2024-10-01")}
        self.assertEqual(daily, {r["id"] for r in live})                 # pending + new backfill rows left out
        self.assertEqual(ddb.trades_to_compute("insider", "2024-10-01", new_backfill=True), [])   # still pending
        self.assertEqual(ddb.set_acceptance({"0001234567-25-000001": "2025-06-02T20:31:00+00:00"}), 2)
        self.assertEqual({t["id"] for t in ddb.trades_to_compute("insider", "2024-10-01", new_backfill=True)},
                         {r["id"] for r in pend})
        self.assertEqual(ddb.pending_acceptance(), {})

    def test_retention_two_years_and_store_off(self):
        old = dt.insider_trades("0001234567-24-000001", TradeBuilderTest.DETAILS, form="4", accepted_at=None,
                                filing_date="2024-10-01", origin="backfill")
        new = dt.insider_trades("0001234567-26-000001", TradeBuilderTest.DETAILS, form="4", accepted_at=None,
                                filing_date="2026-10-01", origin="live")
        ddb.upsert_trades(old + new)
        res = db.prune(now=datetime(2026, 10, 24, 12, tzinfo=UTC))
        self.assertEqual(res["disclosures"], 2)
        self.assertEqual(ddb.counts("insider")["trades"], 2)
        self.assertNotIn("disclosures", db.prune(now=datetime(2026, 10, 24, 12, tzinfo=UTC)))   # shape unchanged
        db.close()
        with self.assertRaises(ddb.StoreUnavailable):
            ddb.get_trade("x")


if __name__ == "__main__":
    unittest.main()
