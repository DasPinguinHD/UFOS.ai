"""Stage 9c (SPEC §19.16 / §20.9): the REAL Trade Republic export format on a SYNTHETIC fixture — every row type,
time sorting, warrant + TILG pairing and a missing partner, staking cost basis, the bond amount basis, dividend
fields, interest, tax refunds, the pending private-market order, privacy (no counterparty / IBAN / name ever stored
or returned), transaction-id dedupe across overlapping exports, and a known FIFO result. Offline, temp databases."""
import asyncio
import json
import unittest

try:
    from sp_monitor_testkit import MonCase
    import tr_synthetic_fixture as fx
except ImportError:  # pragma: no cover
    from tests.sp_monitor_testkit import MonCase
    from tests import tr_synthetic_fixture as fx

from strategy_platform.api import routes
from strategy_platform.api.routes import NO_BODY
from strategy_platform.monitoring import insights, prices
from strategy_platform.monitoring.shadow import importers, normalize, report, service
from strategy_platform.storage import db


def by_cur(block, cur="EUR"):
    return next(c for c in block["currencies"] if c["currency"] == cur)


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.r = importers.parse_file(fx.CSV)
        self.t = [t for _n, t in self.r["txns"]]

    def kinds(self, kind):
        return [t for t in self.t if t["kind"] == kind]

    def test_detection_counts_and_every_type(self):
        r = self.r
        self.assertEqual(r["detection"]["template"], "trade_republic")
        self.assertEqual(r["counts"], {"rows": 23, "parsed": 22, "rejected": 0, "skipped": 1, "positions": 0})
        self.assertEqual(r["skipped"][0]["reason"],
                         "unsupported Trade Republic type CASH/CARD_TRANSACTION — not imported")
        self.assertNotIn(importers.VERIFY_NOTE, r["warnings"])           # written from a real export
        self.assertFalse(importers.TEMPLATES["trade_republic"].verify)
        got = sorted({t["kind"] for t in self.t})
        self.assertEqual(got, sorted(["deposit", "withdrawal", "buy", "sell", "staking", "dividend", "interest",
                                      "tax_refund", "pending_buy", "corporate_action"]))
        ac = {t["key"]: t["asset_class"] for t in self.t if t["key"]}
        self.assertEqual((ac[fx.STOCK], ac[fx.FUND], ac[fx.BOND], ac["SYC"], ac[fx.WRT1], ac[fx.PRIV]),
                         ("share", "fund", "bond", "crypto", "derivative", "other"))

    def test_sorted_by_datetime(self):
        days = [t["date"] for t in self.t]
        self.assertEqual(days, sorted(days))
        self.assertEqual((self.t[0]["kind"], self.t[0]["gross"]), ("deposit", 5000.0))   # line 3 of the file
        self.assertEqual(importers._tr_when("2025-06-20T09:00:00.123Z") < importers._tr_when(
            "2025-06-20T09:00:00.123456Z"), True)
        with self.assertRaises(Exception):
            importers._tr_when("2025-06-20 09:00")

    def test_trades_use_the_cash_amount(self):
        stock = self.kinds("buy")[0]
        self.assertEqual((stock["qty"], stock["gross"], stock["fee"], stock["net"]), (10.0, 1000.0, 1.0, -1001.0))
        bond = next(t for t in self.kinds("buy") if t["key"] == fx.BOND)
        # price is a fraction of the nominal; the amount (incl. accrued interest) is the cost basis, not qty × price
        self.assertEqual((bond["qty"], bond["price"], bond["gross"], bond["net"]), (1000.0, 0.98, 982.0, -983.0))
        crypto = next(t for t in self.kinds("buy") if t["key"] == "SYC")
        self.assertEqual((crypto["symbol"], crypto["isin"]), ("SYC", None))       # the ticker, never the XF id
        sell = next(t for t in self.kinds("sell") if t["key"] == fx.STOCK)
        self.assertEqual((sell["qty"], sell["gross"], sell["fee"], sell["tax"], sell["net"]),
                         (12.0, 1560.0, 1.0, 10.5, 1548.5))

    def test_dividend_interest_tax_refunds(self):
        d = self.kinds("dividend")[0]
        self.assertEqual((d["gross"], d["tax"], round(d["net"], 8), d["original_amount"], d["original_currency"], d["fx_rate"]),
                         (8.4, 2.1, 6.3, 9.0, "USD", 0.9333))
        i = self.kinds("interest")[0]
        self.assertEqual((i["gross"], i["tax"], i["net"], i["name"]), (12.34, 3.08, 12.34 - 3.08, None))
        refunds = sorted((t["tax"], t["key"]) for t in self.kinds("tax_refund"))
        self.assertEqual(refunds, [(2.0, fx.FUND), (5.55, None)])     # refund on a sale + TAX_OPTIMIZATION (amount 0)
        fund_sell = next(t for t in self.kinds("sell") if t["key"] == fx.FUND)
        self.assertEqual((fund_sell["tax"], fund_sell["net"]), (0.0, 119.0))

    def test_warrant_pairing_and_missing_partner(self):
        w = next(t for t in self.kinds("sell") if t["key"] == fx.WRT1 and t["qty"] == 60.0)
        self.assertEqual((w["gross"], w["date"], w["raw_type"], w["ext_id"]),
                         (30.0, "2025-06-20", "WARRANT_EXERCISE+TILG", f"{fx.tid(15)}|{fx.tid(16)}"))
        self.assertFalse([t for t in self.t if t["raw_type"] == "CASH/TILG"])     # consumed by the pair
        ca = self.kinds("corporate_action")
        self.assertEqual([(t["key"], t["qty"], t["net"]) for t in ca], [(fx.WRT2, 10.0, 0.0)])
        self.assertIn("without its TILG", ca[0]["note"])
        # a TILG without its exercise is reported too, never booked as cash
        r = importers.parse_file(fx.to_csv([x for x in fx.ROWS if x["type"] != "WARRANT_EXERCISE"]))
        tilg = [t for _n, t in r["txns"] if t["kind"] == "corporate_action" and t["raw_type"] == "CASH/TILG"]
        self.assertEqual((len(tilg), tilg[0]["net"]), (1, 0.0))

    def test_staking_and_pending(self):
        s = self.kinds("staking")[0]
        self.assertEqual((s["qty"], s["price"], s["gross"], s["net"], s["note"]), (0.01, 21000.0, 210.0, 0.0,
                                                                                  "staking reward"))
        p = self.kinds("pending_buy")[0]
        self.assertEqual((p["gross"], p["net"], p["asset_class"]), (500.0, -500.0, "other"))
        self.assertEqual(p["name"], "Synthetic Private Equity (pay to [removed])")   # IBAN scrubbed from kept text

    def test_transfers_keep_only_date_kind_amount_currency(self):
        tr = self.kinds("deposit") + self.kinds("withdrawal")
        self.assertEqual(len(tr), 3)
        for t in tr:
            self.assertEqual((t["name"], t["key"], t["symbol"], t["isin"], t["note"]), (None, None, None, None, None))
        self.assertEqual(sorted(t["net"] for t in tr), [-300.0, 1000.0, 5000.0])
        blob = json.dumps(self.r, default=str)
        for s in fx.SECRETS:
            self.assertNotIn(s, blob, s)

    def test_scrub_and_bad_rows(self):
        self.assertEqual(importers.scrub_iban("x DE89 3704 0044 0532 0130 00 y GB33BUKB20201555555555"),
                         "x [removed] y [removed]")
        self.assertEqual(importers.scrub_iban("US0378331005 XF000SYC0019"), "US0378331005 XF000SYC0019")  # ISINs
        bad = [dict(fx.ROWS[4], amount="=1+2"), dict(fx.ROWS[5], datetime="yesterday"),
               dict(fx.ROWS[10], price="")]
        r = importers.parse_file(fx.to_csv(bad))
        self.assertEqual(sorted(e["field"] for e in r["errors"]), ["amount", "datetime", "price"])


class ServiceTest(MonCase):
    def setUp(self):
        super().setUp()
        prices.set_source(lambda s, a, b: (_ for _ in ()).throw(ValueError("offline")), lambda s, a, b: [])
        self.acc = service.create_account("TR", "EUR", "DE")["id"]
        self.res = service.import_file(self.acc, fx.CSV, "tr.csv", "auto", None, False)

    def call(self, name, query=None, body=NO_BODY, match=None):
        return asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))

    def test_import_counts(self):
        c = self.res["counts"]
        self.assertEqual((c["accepted"], c["already_known"], c["duplicates"], c["skipped"], c["rejected"]),
                         (22, 0, 0, 1, 0))

    def test_transaction_id_dedupe(self):
        with self.assertRaises(Exception) as cm:                            # the identical file: still refused
            service.import_file(self.acc, fx.CSV, "tr.csv", "auto", None, False)
        self.assertEqual(getattr(cm.exception, "code", None), "duplicate_import")
        newer = fx.ROWS[:6] + [fx.row("2025-12-01T10:00:00.000Z", "TRADING", "BUY", 90, ac="STOCK", name="Synthetic AG",
                                      sym=fx.STOCK, shares="1", price="150", amount="-150.00", fee="-1.00"),
                               fx.row("2025-12-02T10:00:00.000Z", "CASH", "CUSTOMER_INPAYMENT", 91, amount="50.00")]
        r = service.import_file(self.acc, fx.to_csv(newer), "tr-newer.csv", "auto", None, False)
        self.assertEqual((r["counts"]["accepted"], r["counts"]["already_known"]), (2, 6))
        self.assertEqual(len(service.txns(self.acc)), 24)

    def test_late_partner_resolves_the_reason(self):
        a = service.create_account("TR2", "EUR", "DE")["id"]
        first = [x for x in fx.ROWS if x["transaction_id"] != fx.tid(16)]               # TILG missing
        service.import_file(a, fx.to_csv(first), "a.csv", "auto", None, False)
        acc = next(x for x in insights.portfolio(2025, a)["accounts"])
        self.assertEqual(len(acc["unpaired_corporate_actions"]), 2)
        self.assertTrue(any("corporate action" in r and fx.WRT1 in r for r in acc["reasons"]))
        pos = {p["key"]: p for p in acc["positions"]}
        self.assertEqual(pos[fx.WRT1]["qty"], 60.0)                                       # NOT closed silently
        service.import_file(a, fx.CSV, "b.csv", "auto", None, False)                      # the full export
        acc = next(x for x in insights.portfolio(2025, a)["accounts"])
        self.assertEqual([u["key"] for u in acc["unpaired_corporate_actions"]], [fx.WRT2])
        self.assertNotIn(fx.WRT1, {p["key"] for p in acc["positions"]})

    def test_insights_fifo_buckets_taxes(self):
        d = insights.portfolio(2025, self.acc)
        a = d["accounts"][0]
        y = by_cur(a["years"]["2025"])
        r = y["realised"]
        # brokerage: stock 1559 − (1001 + 2/5 × 601) = 317.6; ETF 119 − 100.5 = 18.5
        self.assertEqual((r["brokerage"]["net"], r["brokerage"]["parts"]), (336.1, 3))
        # other: crypto 4999 − 4000.4 = 998.6; call 79 − 60.4 = 18.6; warrant exercise 30 − 90.6 = −60.6
        self.assertEqual((r["other"]["net"], r["other"]["gains"], r["other"]["losses"]), (956.6, 1017.2, -60.6))
        self.assertEqual(r["unclassified"]["parts"], 0)
        self.assertEqual((y["dividends"], y["withholding"], y["interest"], y["taxes_on_sales"], y["tax_refunds"],
                          y["trading_fees"]), (8.4, 5.18, 12.34, 10.5, 7.55, 10.0))
        pos = {p["key"]: p for p in a["positions"]}
        self.assertEqual(set(pos), {fx.STOCK, fx.FUND, fx.BOND, "SYC", fx.WRT2})       # no private fund position
        self.assertEqual((pos[fx.STOCK]["qty"], pos[fx.STOCK]["cost"]), (3.0, 360.6))
        self.assertEqual((pos[fx.BOND]["cost"], pos["SYC"]["qty"], pos["SYC"]["cost"]), (983.0, 0.31, 6210.6))
        self.assertIn("staking", pos["SYC"]["note"])
        self.assertEqual([(p["key"], p["amount"]) for p in a["pending"]], [(fx.PRIV, 500.0)])
        self.assertEqual(a["not_used"], [])                                             # no oversell
        self.assertEqual(d["buckets"][0], {"key": "brokerage", "label": "Brokerage (stocks + ETFs)"})
        s = insights.summary()
        self.assertEqual(s["currencies"][0]["realised_brokerage"], 0.0)                 # the summary is 2026

    def test_privacy_nothing_stored_or_returned(self):
        with db._connect() as conn:
            dump = "\n".join(conn.iterdump())
            cols = [r[1] for r in conn.execute("PRAGMA table_info(sh_transactions)").fetchall()]
        outs = [json.dumps(self.res, default=str), json.dumps(service.transactions_page(self.acc, 100, 0), default=str),
                json.dumps(insights.portfolio(2025), default=str),
                report.build(self.acc, "json")["content"], report.build(self.acc, "html")["content"]]
        st, p = self.call("sh_transactions", {"limit": "100"}, match={"id": self.acc})
        outs.append(json.dumps(p, default=str))
        for s in fx.SECRETS:
            self.assertNotIn(s, dump, f"{s!r} in the database")
            for o in outs:
                self.assertNotIn(s, o, f"{s!r} in a response")
        self.assertFalse([c for c in cols if "counterparty" in c or "iban" in c or "mcc" in c or "reference" in c])


class MigrationTest(unittest.TestCase):
    def test_v19_keeps_rows_and_widens_kinds(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        for version, statements in db.MIGRATIONS:
            if version > 18:
                break
            for sql in statements:
                sql(conn) if callable(sql) else conn.execute(sql)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        conn.execute("INSERT INTO sh_transactions (account_id, import_id, hash, date, kind, currency, gross, fee, tax,"
                     " net) VALUES ('a', 'i', 'h1', '2024-01-01', 'buy', 'EUR', 1, 0, 0, -1)")
        conn.commit()
        self.assertEqual(db.migrate(conn), db.SCHEMA_VERSION)
        row = conn.execute("SELECT hash, kind, ext_id, note, ts, seq FROM sh_transactions").fetchall()
        self.assertEqual(row, [("h1", "buy", None, None, None, None)])
        conn.execute("INSERT INTO sh_transactions (account_id, import_id, hash, date, kind, currency, gross, fee, tax,"
                     " net, ext_id) VALUES ('a', 'i', 'h2', '2024-01-02', 'staking', 'EUR', 1, 0, 0, 0, 'x')")
        with self.assertRaises(sqlite3.IntegrityError):              # one transaction id per account
            conn.execute("INSERT INTO sh_transactions (account_id, import_id, hash, date, kind, currency, gross, fee,"
                         " tax, net, ext_id) VALUES ('a', 'i', 'h3', '2024-01-02', 'buy', 'EUR', 1, 0, 0, 0, 'x')")
        with self.assertRaises(sqlite3.DatabaseError):               # still insert-only (no update)
            conn.execute("UPDATE sh_transactions SET net = 5")
        conn.close()


def tr_buy(n, dt, qty, price, kind="BUY"):
    sign = -1 if kind == "BUY" else 1
    return fx.row(dt, "TRADING", kind, n, ac="STOCK", name="Order AG", sym=fx.STOCK,
                  shares=str(qty if kind == "BUY" else -qty), price=str(price), amount=f"{sign * qty * price:.2f}",
                  fee="0.00")


class ReviewFixTest(MonCase):
    """Stage-9c review fixes (2026-10-18, SPEC §19.16 "Review fixes")."""

    def setUp(self):
        super().setUp()
        prices.set_source(lambda s, a, b: (_ for _ in ()).throw(ValueError("offline")), lambda s, a, b: [])

    def realised(self, aid, year="2025"):
        a = insights.portfolio(int(year), aid)["accounts"][0]
        return by_cur(a["years"][year])["realised"]["brokerage"]["net"]

    def test_same_day_order_follows_the_time_not_the_file_line(self):
        # HIGH: the 15:00 buy (110) is FIRST in the file, the 09:00 buy (100) second; FIFO must sell the 09:00 lot
        rows = [tr_buy(1, "2025-03-03T15:00:00.000Z", 10, 110), tr_buy(2, "2025-03-03T09:00:00.000Z", 10, 100),
                tr_buy(3, "2025-03-04T10:00:00.000Z", 10, 105, "SELL")]
        aid = service.create_account("order", "EUR", "DE")["id"]
        service.import_file(aid, fx.to_csv(rows), "o.csv", "auto", None, False)
        self.assertEqual(self.realised(aid), 50.0)                       # was −50.00 before the fix
        tx = service.txns(aid)
        self.assertEqual([(t["price"], t["ts"][11:19], t["row"]) for t in tx],
                         [(100.0, "09:00:00", 3), (110.0, "15:00:00", 2), (105.0, "10:00:00", 4)])
        pos = insights.portfolio(2025, aid)["accounts"][0]["positions"][0]
        self.assertEqual((pos["qty"], pos["cost"]), (10.0, 1100.0))
        book = normalize.replay(tx)
        self.assertEqual(book.closed[0]["cost"]["ccy"], 1000.0)
        # Scalable: its time column orders the same way
        sc = ("date;time;status;reference;description;assetType;type;isin;shares;price;amount;fee;tax;currency\n"
              "2025-03-03;15:00:00;Executed;R1;Order AG;Stock;Buy;DE000SYN0001;10;110,00;-1100,00;0;0;EUR\n"
              "2025-03-03;09:00:00;Executed;R2;Order AG;Stock;Buy;DE000SYN0001;10;100,00;-1000,00;0;0;EUR\n"
              "2025-03-04;10:00:00;Executed;R3;Order AG;Stock;Sell;DE000SYN0001;10;105,00;1050,00;0;0;EUR\n")
        sid = service.create_account("scalable", "EUR", "DE")["id"]
        service.import_file(sid, sc.encode(), "s.csv", "auto", None, False)
        self.assertEqual(self.realised(sid), 50.0)
        with self.assertRaises(normalize.TxnError):
            normalize.time9("25:00")
        self.assertEqual(normalize.time9("93000"), "09:30:00.000000000")

    def test_deposits_only_report(self):
        aid = service.create_account("cash", "EUR", "DE")["id"]
        service.import_file(aid, fx.to_csv([fx.ROWS[1]]), "d.csv", "auto", None, False)
        h = report.build(aid, "html")["content"]
        self.assertIn("<td>Your portfolio</td><td>—</td><td>—</td><td>—</td><td>—</td>"
                      "<td>not available: no trades imported</td>", h)
        self.assertNotIn("<td>Your portfolio</td><td>0.00</td>", h)

    def test_repeated_transaction_id_in_one_file(self):
        same = fx.ROWS[4]
        changed = dict(fx.ROWS[4], amount="-999.00")
        r = importers.parse_file(fx.to_csv([same, dict(same), changed]))
        self.assertEqual((r["counts"]["parsed"], r["counts"]["skipped"], r["counts"]["rejected"]), (1, 1, 1))
        self.assertEqual(r["skipped"][0]["reason"], "repeated row (same transaction_id and content) — imported once")
        self.assertEqual((r["errors"][0]["field"], r["errors"][0]["message"]),
                         ("transaction_id", "duplicate transaction_id with different content"))

    def test_over_long_ids_are_rejected(self):
        r = importers.parse_file(fx.to_csv([dict(fx.ROWS[4], transaction_id="x" * 121)]))
        self.assertEqual((r["counts"]["parsed"], r["errors"][0]["field"]), (0, "transaction_id"))
        with self.assertRaises(normalize.TxnError):
            normalize.make_txn(date_="2025-01-01", kind="deposit", gross=1, ext_id="y" * 256)

    def test_possible_duplicates_are_skipped_with_a_reason(self):
        aid = service.create_account("dups", "EUR", "DE")["id"]
        service.import_file(aid, fx.CSV, "a.csv", "auto", None, False)
        renamed = [dict(x, transaction_id=x["transaction_id"].replace("-8000-", "-9000-")) for x in fx.ROWS]
        r = service.import_file(aid, fx.to_csv(renamed), "b.csv", "auto", None, False)     # every id changed
        self.assertEqual((r["counts"]["accepted"], r["counts"]["possible_duplicates"]), (0, 22))
        self.assertTrue(any(service.POSSIBLE_DUP in w for w in r["warnings"]))
        self.assertEqual(len(service.txns(aid)), 22)
        # an older import WITHOUT ids, then the TR export of the same purchase
        gid = service.create_account("generic", "EUR", "DE")["id"]
        prof = service.save_profile("g", {"delimiter": ",", "decimal": ".", "thousands": "", "date_format": "%Y-%m-%d",
                                          "encoding": "auto", "columns": {"date": "D", "type": "T", "isin": "I",
                                                                          "quantity": "Q", "price": "P", "fee": "F"},
                                          "type_map": {"B": "buy"}, "default_currency": "EUR",
                                          "amount_sign": "positive"})["id"]
        service.import_file(gid, b"D,T,I,Q,P,F\n2025-01-10,B,DE000SYN0001,10,100,1\n", "g.csv", "generic", prof, False)
        r = service.import_file(gid, fx.to_csv([fx.ROWS[4], fx.ROWS[5]]), "tr.csv", "auto", None, False)
        self.assertEqual((r["counts"]["accepted"], r["counts"]["possible_duplicates"]), (1, 1))
        rep = json.loads(json.dumps(service.imports_of(gid)))
        self.assertEqual(len(rep), 2)

    def test_iban_patterns(self):
        for raw in ("DE89-3704-0044-0532-0130-00", "DE89 370400 440532 013000", "DE89 3704 0044 0532 0130 00",
                    "GB33BUKB20201555555555", "gb33bukb20201555555555", "NO9386011117947"):
            self.assertEqual(importers.scrub_iban(f"pay {raw} now"), "pay [removed] now", raw)
        for keep in ("US0378331005 XF000SYC0019", "Buy trade DE0005140008 Deutsche Bank", "IE00B4L5Y983",
                     "Sell DE000SYN0001 SYNTHETIC AG 2030"):
            self.assertEqual(importers.scrub_iban(keep), keep)

    def test_pending_fee_in_fees(self):
        aid = service.create_account("pending", "EUR", "DE")["id"]
        rows = [dict(fx.ROWS[21], fee="-1.00")]
        service.import_file(aid, fx.to_csv(rows), "p.csv", "auto", None, False)
        a = insights.portfolio(2025, aid)["accounts"][0]
        self.assertEqual((by_cur(a["years"]["2025"])["fees"], a["positions"]), (1.0, []))
        self.assertIn("fee is included in the fees", a["pending"][0]["note"])


class EmptyReportTest(MonCase):
    """9c addendum: an empty account's audit report names why nothing is shown (never an empty heading or 0.00)."""

    def test_empty_account(self):
        prices.set_source(lambda s, a, b: (_ for _ in ()).throw(ValueError("offline")), lambda s, a, b: [])
        acc = service.create_account("Empty", "EUR", "DE")["id"]
        h = report.build(acc, "html")["content"]
        self.assertIn(report.NO_TRADES, h)
        for title in ("Disposition effect", "Trading activity", "Holding periods (days)",
                      "Concentration (current, cost basis)", "Trading after losses", "Timing vs market"):
            self.assertIn(f"<h3>{title}</h3><p class=muted>Not available: no trades imported</p>", h)
        self.assertIn("<td>Your portfolio</td><td>—</td><td>—</td><td>—</td><td>—</td>"
                      "<td>not available: no statements imported</td>", h)
        self.assertNotIn("<td>Your portfolio</td><td>0.00</td>", h)
        j = json.loads(report.build(acc, "json")["content"])
        self.assertEqual((j["diagnostics"]["message"], j["shadows"]["user"]["end_value"],
                          j["shadows"]["user"]["message"]), (report.NO_TRADES, None, "no statements imported"))


if __name__ == "__main__":
    unittest.main()
