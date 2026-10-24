"""Stage 9 (SPEC §19.6–§19.8): the Shadow Account — importers on synthetic samples of every template + the generic
profile, malicious CSVs (formula injection, huge fields / rows / columns, NUL, size), dedupe on re-import, FIFO with
partial sales, the diagnostics math on known cases, shadow portfolios with exact cash-flow parity, the audit report
(escaped, no script), the API (strict input, multipart, 404/409). Offline: fake prices, temp databases."""
import asyncio
import json
import math
import unittest
from datetime import date, timedelta

try:
    from sp_monitor_testkit import MonCase, put_rates
except ImportError:  # pragma: no cover
    from tests.sp_monitor_testkit import MonCase, put_rates

from strategy_platform.api import routes
from strategy_platform.api.routes import NO_BODY
from strategy_platform.monitoring import prices
from strategy_platform.monitoring.shadow import diagnostics, importers, normalize, report, service, shadow_portfolio

# the real Trade Republic export format (§19.16) — synthetic rows
TR_HEAD = ('"datetime","date","account_type","category","type","asset_class","name","symbol","shares","price","amount",'
           '"fee","tax","currency","original_amount","original_currency","fx_rate","description","transaction_id",'
           '"counterparty_name","counterparty_iban","payment_reference","mcc_code"\n')


def tr_line(dt, cat, typ, name, shares, price, amount, fee, tax, n, ac="STOCK", sym="US0378331005"):
    vals = [dt, dt[:10], "DEFAULT", cat, typ, ac, name, sym, shares, price, amount, fee, tax, "EUR", "", "", "", "",
            f"00000000-0000-4000-8000-{n:012d}", "", "", "", ""]
    return ",".join('"' + v.replace('"', '""') + '"' for v in vals) + "\n"


TR = (TR_HEAD
      + tr_line("2024-03-01T10:00:00.000Z", "TRADING", "SELL", "=cmd|' /C calc'!A0", "-5", "120", "600.00", "-1.00",
                "0", 2)
      + tr_line("2024-01-05T10:00:00.000Z", "TRADING", "BUY", "Apple", "10", "100", "-1000.00", "-1.00", "0", 1)
      + tr_line("2024-04-01T10:00:00.000Z", "CASH", "DIVIDEND", "Apple", "", "", "10.00", "", "-1.50", 3)
      + tr_line("2024-04-02T10:00:00.000Z", "TRADING", "BUY", "x", "1", "1", "=1+2", "0", "0", 4)
      + tr_line("2024-04-03T10:00:00.000Z", "CASH", "LOTTO", "x", "1", "0", "1", "0", "0", 5))
SCALABLE = ("date;time;status;reference;description;assetType;type;isin;shares;price;amount;fee;tax;currency\n"
            "2024-02-01;10:00:00;Executed;R1;iShares Core MSCI World;Security;Buy;IE00B4L5Y983;2;80,50;-161,99;0,99;0;EUR\n"
            "2024-02-02;10:00:00;Cancelled;R2;x;Security;Buy;IE00B4L5Y983;2;80,50;-161,99;0,99;0;EUR\n"
            "2024-03-01;09:00:00;Executed;R3;iShares Core MSCI World;Security;Distribution;IE00B4L5Y983;;;1,20;0;0,30;EUR\n")
IBKR = ("HEADER,TRNT,ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Description,ISIN,TradeDate,Quantity,TradePrice,"
        "IBCommission,Buy/Sell,LevelOfDetail\n"
        "DATA,TRNT,U1,USD,STK,SPY,SPDR,US78462F1030,20240110,10,470.5,-1.0,BUY,EXECUTION\n"
        "DATA,TRNT,U1,USD,STK,SPY,SPDR,US78462F1030,20240110,10,470.5,-1.0,BUY,ORDER\n"
        "DATA,TRNT,U1,USD,CASH,EUR.USD,FX,,20240110,100,1.1,0,BUY,EXECUTION\n")
GENERIC = ("Valuta,Art,WKN,Menge,Kurs,Betrag,Spesen,Waehrung\n"
           "05.01.2024,K,AAPL,10,100.00,1000.00,1.00,USD\n"
           "01.03.2024,V,AAPL,5,120.00,600.00,1.00,USD\n"
           "02.03.2024,X,AAPL,1,1,1,0,USD\n")
MAPPING = {"delimiter": ",", "decimal": ".", "thousands": "", "date_format": "%d.%m.%Y", "encoding": "auto",
           "columns": {"date": "Valuta", "type": "Art", "symbol": "WKN", "quantity": "Menge", "price": "Kurs",
                       "amount": "Betrag", "fee": "Spesen", "currency": "Waehrung"},
           "type_map": {"K": "buy", "V": "sell", "X": "ignore"}, "default_currency": "USD", "amount_sign": "positive"}


class ImporterTest(unittest.TestCase):
    def test_trade_republic(self):
        r = importers.parse_file(TR.encode("utf-8"))
        self.assertEqual(r["detection"]["template"], "trade_republic")
        self.assertEqual(r["counts"], {"rows": 5, "parsed": 3, "rejected": 1, "skipped": 1, "positions": 0})
        self.assertEqual([e["field"] for e in r["errors"]], ["amount"])         # an unknown type is skipped (§19.16)
        buy = r["txns"][0][1]
        self.assertEqual((buy["kind"], buy["qty"], buy["gross"], buy["fee"], buy["net"], buy["price"]),
                         ("buy", 10.0, 1000.0, 1.0, -1001.0, 100.0))
        sell = r["txns"][1][1]
        self.assertEqual((sell["gross"], sell["net"], sell["name"]), (600.0, 599.0, "=cmd|' /C calc'!A0"))
        div = r["txns"][2][1]
        self.assertEqual((div["kind"], div["gross"], div["tax"], div["net"]), ("dividend", 10.0, 1.5, 8.5))
        self.assertNotIn(importers.VERIFY_NOTE, r["warnings"])          # written from a real export (§19.16)

    def test_scalable_cp1252_and_status(self):
        r = importers.parse_file(SCALABLE.replace("Core", "Cöre").encode("cp1252"))
        self.assertEqual((r["detection"]["template"], r["detection"]["encoding"]), ("scalable", "cp1252"))
        self.assertEqual(r["counts"]["skipped"], 1)
        t = r["txns"][0][1]
        self.assertEqual((t["gross"], t["fee"], t["net"], t["name"]), (161.0, 0.99, -161.99, "iShares Cöre MSCI World"))
        d = r["txns"][1][1]
        self.assertEqual((d["kind"], d["gross"], d["tax"]), ("dividend", 1.5, 0.3))

    def test_ibkr_sections_and_detail_levels(self):
        r = importers.parse_file(IBKR.encode())
        self.assertEqual(r["detection"]["template"], "ibkr_flex")
        self.assertEqual(r["counts"]["parsed"], 1)
        self.assertEqual(r["counts"]["skipped"], 2)            # ORDER level + FX trade
        t = r["txns"][0][1]
        self.assertEqual((t["symbol"], t["currency"], t["gross"], t["fee"], t["asset_class"]),
                         ("SPY", "USD", 4705.0, 1.0, "share"))

    def test_generic_profile(self):
        self.assertEqual(importers.check_mapping(MAPPING), {})
        bad = dict(MAPPING, decimal=";", columns={"x": "y"})
        e = importers.check_mapping(bad)
        self.assertIn("mapping.decimal", e)
        self.assertIn("mapping.columns.x", e)
        r = importers.parse_file(GENERIC.encode(), "generic", MAPPING)
        self.assertEqual(r["counts"], {"rows": 3, "parsed": 2, "rejected": 0, "skipped": 1, "positions": 0})
        self.assertEqual(r["txns"][1][1]["net"], 599.0)
        with self.assertRaises(importers.ImportRefused) as cm:
            importers.parse_file(TR.encode(), "generic", MAPPING)
        self.assertEqual(cm.exception.code, "unknown_format")

    def test_unknown_format(self):
        with self.assertRaises(importers.ImportRefused) as cm:
            importers.parse_file(b"a;b;c\n1;2;3\n")
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "unknown_format"))
        self.assertEqual(cm.exception.detection["header"], ["a", "b", "c"])

    def test_malicious_files(self):
        head = TR_HEAD
        one = tr_line("2024-01-05T10:00:00.000Z", "TRADING", "BUY", "x", "1", "1", "-1.00", "0", "0", 1)
        for data, code in ((head.encode() + b"x" * 200000 + b",1\n", "malformed"),
                           (b"\x00" + head.encode(), "binary"),
                           (b"a" * (importers.MAX_BYTES + 1), "too_large"),
                           (b"", "empty"),
                           ((";".join(["c"] * 70) + "\n").encode(), "too_many_columns"),
                           ((head + one + '"1"\n' * (importers.MAX_ROWS + 1)).encode(), "too_many_rows")):
            with self.assertRaises(importers.ImportRefused) as cm:
                importers.parse_file(data)
            self.assertEqual(cm.exception.code, code)
        long_field = head + tr_line("2024-01-05T10:00:00.000Z", "TRADING", "BUY", "A" * 5000, "1", "1", "-1.00", "0",
                                    "0", 1)
        r = importers.parse_file(long_field.encode())
        self.assertEqual(r["errors"][0]["message"], "field longer than 2048 characters")
        for v in ("=SUM(1)", "@x", "1e5", "0x10", "1,2,3", "--1"):
            with self.assertRaises(ValueError):
                importers.parse_number(v, ",", ".")
        self.assertEqual(importers.parse_number("€ 1.234,56", ",", "."), 1234.56)
        self.assertEqual(importers.parse_number("−12,5", ",", "."), -12.5)
        self.assertEqual(normalize.clean_text("a\r\nb\x07c", 10), "abc")


class FifoTest(unittest.TestCase):
    def test_partial_sales_and_unmatched(self):
        b = normalize.Book()
        b.buy("A", "2024-01-01", 10, {"ccy": 1000.0})
        b.buy("A", "2024-02-01", 10, {"ccy": 2000.0})
        parts = b.sell("A", "2024-03-01", 15, {"ccy": 3000.0})
        self.assertEqual([(p["qty"], p["cost"]["ccy"], p["proceeds"]["ccy"], p["open_date"]) for p in parts],
                         [(10, 1000.0, 2000.0, "2024-01-01"), (5, 1000.0, 1000.0, "2024-02-01")])
        self.assertEqual(b.holdings()["A"]["qty"], 5)
        self.assertAlmostEqual(b.holdings()["A"]["amounts"]["ccy"], 1000.0)
        b.sell("A", "2024-04-01", 7, {"ccy": 700.0})
        self.assertEqual([(u["key"], u["qty"]) for u in b.unmatched], [("A", 2)])
        s = normalize.Book(allow_short=True)
        s.sell("B", "2024-01-01", 2, {"ccy": 400.0})
        parts = s.buy("B", "2024-01-05", 2, {"ccy": 300.0})
        self.assertEqual((parts[0]["short"], parts[0]["proceeds"]["ccy"], parts[0]["cost"]["ccy"]), (True, 400.0, 300.0))


def T(day, kind, key, qty=None, price=None, fee=0.0, gross=None, tax=0.0):
    t = normalize.make_txn(date_=day, kind=kind, symbol=key, qty=qty, price=price, fee=fee, gross=gross, tax=tax,
                           currency="USD")
    t.update({"fx": 1.0, "gross_b": t["gross"], "fee_b": t["fee"], "tax_b": t["tax"], "net_b": t["net"],
              "price_b": t["price"]})
    return t


class DiagnosticsMathTest(unittest.TestCase):
    def test_disposition_known_case(self):
        tx = [T("2024-01-01", "buy", "A", 10, 10), T("2024-01-01", "buy", "B", 10, 10),
              T("2024-01-01", "buy", "C", 10, 10), T("2024-01-05", "sell", "A", 10, 12)]
        px = {"B": 8.0, "C": 11.0}
        d = diagnostics.disposition(tx, lambda k, day: px.get(k))
        self.assertEqual((d["realized_gains"], d["paper_gains"], d["paper_losses"], d["realized_losses"]), (1, 1, 1, 0))
        self.assertEqual((d["pgr"], d["plr"], d["de"]), (0.5, 0.0, 0.5))
        d2 = diagnostics.disposition(tx, lambda k, day: None)
        self.assertEqual(d2["unpriced_cases"], 2)

    def test_turnover_holding_concentration_after_losses(self):
        tx = [T("2024-01-01", "buy", "A", 75, 10), T("2024-01-01", "buy", "B", 25, 10, fee=1.0),
              T("2024-01-11", "sell", "B", 25, 8),               # loss, held 10 days
              T("2024-01-12", "buy", "C", 10, 10), T("2024-01-14", "buy", "D", 10, 10),
              T("2024-02-10", "sell", "A", 75, 12)]              # gain, held 40 days
        book = normalize.Book()
        for t in tx:
            diagnostics._book_apply(book, t)
        h = diagnostics.holding(book.closed)
        self.assertEqual((h["closed_parts"], h["winners_median_days"], h["losers_median_days"]), (2, 40, 10))
        c = diagnostics.concentration(tx[:2], "2024-01-01", "2024-01-01")
        self.assertAlmostEqual(c["current"]["top1_pct"], 750 / 1001 * 100)
        a = diagnostics.after_losses(tx, book.closed)
        self.assertEqual((a["loss_sells"], a["gain_sells"], a["buys_after_loss_avg"], a["buys_after_gain_avg"]),
                         (1, 1, 2.0, 0.0))
        t = diagnostics.turnover(tx, "2024-01-01", "2024-02-10", book.closed)
        self.assertEqual((t["trades"], t["buys"], t["sells"], t["round_trips_under_30d"]), (6, 4, 2, 1))
        self.assertAlmostEqual(t["volume"], 750 + 250 + 200 + 100 + 100 + 900)
        self.assertAlmostEqual(t["fees"], 1.0)

    def test_timing(self):
        bench = [((date(2024, 1, 1) + timedelta(days=i)).isoformat(), 100.0 * (1.01 ** i)) for i in range(60)]
        tx = [T("2024-01-03", "buy", "A", 1, 10)]
        t = diagnostics.timing(tx, bench, "SPY")
        self.assertAlmostEqual(t["buy_fwd_avg_pct"], (1.01 ** 20 - 1) * 100)
        self.assertAlmostEqual(t["uncond_fwd_avg_pct"], (1.01 ** 20 - 1) * 100)


def closes(series):
    def fn(symbol, start, end):
        if symbol not in series:
            raise ValueError(f"unknown symbol {symbol}")
        return series[symbol]
    return fn


class ShadowParityTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(prices.set_source, None)
        days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        self.series = {"SPY": list(zip(days, [100.0, 110.0, 121.0, 133.1])),
                       "AGG": list(zip(days, [50.0, 50.0, 55.0, 55.0]))}
        prices.set_source(closes(self.series))
        self.cl = prices.Closes(date(2024, 1, 1), date(2024, 1, 10))

    def tx(self):
        return [T("2024-01-02", "buy", "X", 10, 100), T("2024-01-03", "buy", "X", 5, 100),
                T("2024-01-04", "sell", "X", 3, 100)]

    def run_cmp(self, rules, tx=None, end="2024-01-05", user=None):
        ident = lambda v, d: v      # noqa: E731 — USD base: no FX
        return shadow_portfolio.compare(tx or self.tx(), rules, end, closes=self.cl, strategy_series=lambda s: [],
                                        to_usd=ident, to_base=ident, user_end_value=user, user_message=None)

    def test_buy_and_hold_parity_and_value(self):
        out = self.run_cmp([{"type": "buy_and_hold", "symbol": "SPY"}], user=1500.0)
        s = out["shadows"][0]
        self.assertEqual(s["parity"], {"flows_equal": True, "max_abs_diff": 0.0, "count": 3})
        units = 1000 / 100 + 500 / 110 - 300 / 121
        self.assertAlmostEqual(s["end_value"], units * 133.1, places=6)
        self.assertEqual((out["flows"]["sum_in"], out["flows"]["sum_out"]), (1500.0, 300.0))
        self.assertAlmostEqual(out["user"]["pnl"], 1500.0 - 1200.0)
        self.assertIsNone(s["mwr_annual"])          # 3 days: no annualised root in range (not a stand-in number)
        self.assertAlmostEqual(s["twr"], 0.331, places=6)

    def test_financing_keeps_parity(self):
        tx = [T("2024-01-02", "buy", "X", 1, 100), T("2024-01-04", "sell", "X", 1, 500)]
        s = self.run_cmp([{"type": "buy_and_hold", "symbol": "SPY"}], tx=tx)["shadows"][0]
        self.assertTrue(s["parity"]["flows_equal"])
        self.assertLess(s["cash_min"], 0)
        self.assertAlmostEqual(s["end_value"], -(500 - 121.0))

    def test_monthly_rebalance_and_unavailable(self):
        out = self.run_cmp([{"type": "monthly_rebalance", "weights": {"SPY": 0.6, "AGG": 0.4}},
                            {"type": "buy_and_hold", "symbol": "QQQ"}])
        self.assertTrue(out["shadows"][0]["parity"]["flows_equal"])
        self.assertIn("unavailable", out["shadows"][1])
        self.assertIsNone(shadow_portfolio.check_rule({"type": "monthly_rebalance", "weights": {"SPY": 1.0}}))
        self.assertIn("sum to 1", shadow_portfolio.check_rule({"type": "monthly_rebalance",
                                                               "weights": {"SPY": 0.5}}))
        self.assertIsNotNone(shadow_portfolio.check_rule({"type": "x"}))

    def test_xirr(self):
        r = shadow_portfolio.xirr([("2024-01-01", -100.0), ("2025-01-01", 110.0)])
        self.assertAlmostEqual(r, 0.10, places=3)


class ServiceAndApiTest(MonCase):
    def call(self, name, query=None, body=NO_BODY, match=None):
        api = routes.V2Api()
        return asyncio.run(getattr(api, name)(query or {}, body, match or {}))

    def test_flow_through_the_api(self):
        st, p = self.call("sh_account_create", body={"label": "My TR", "base_currency": "EUR", "broker_country": "DE"})
        self.assertEqual(st, 201, p)
        sid = p["account"]["id"]
        st, p = self.call("sh_account_create", body={"label": "x", "base_currency": "GBP", "broker_country": "DE",
                                                     "extra": 1})
        self.assertEqual((st, set(p["errors"])), (400, {"extra"}))          # unknown fields first
        st, p = self.call("sh_import", {"dry_run": "true"}, {"_csv": TR.encode(), "_filename": "tr.csv"}, {"id": sid})
        self.assertEqual((st, p["import"], p["counts"]["accepted"]), (200, None, 3))
        st, p = self.call("sh_import", {}, {"_csv": TR.encode(), "_filename": "tr.csv"}, {"id": sid})
        self.assertEqual((st, p["import"]["accepted"]), (201, 3))
        st, p = self.call("sh_import", {}, {"_csv": TR.encode(), "_filename": "tr.csv"}, {"id": sid})
        self.assertEqual((st, p["code"]), (409, "duplicate_import"))          # the same file twice
        st, p = self.call("sh_import", {"dry_run": "true"}, {"_csv": TR.encode()}, {"id": sid})
        self.assertTrue(any("imported already" in w for w in p["warnings"]))
        st, p = self.call("sh_import", {}, {"_csv": TR.encode() + b"\n", "_filename": "tr2.csv"}, {"id": sid})
        self.assertEqual((st, p["counts"]["accepted"], p["counts"]["already_known"]), (201, 0, 3))  # tx-id dedupe
        extra_import = p["import"]["id"]
        st, p = self.call("sh_import_remove", body={}, match={"id": sid, "import_id": extra_import})   # fix1023
        self.assertEqual((st, p["removed"]["transactions"]), (200, 0), p)
        self.assertEqual(p["account"]["imports"], 1)
        from strategy_platform.storage import db as _db
        self.assertEqual(_db.list_audit(action="shadow_import_removed")[0]["detail"]["import_id"], extra_import)
        st, p = self.call("sh_import_remove", body={"confirm": "REMOVE IMPORT", "reason": "again"},
                          match={"id": sid, "import_id": extra_import})
        self.assertEqual(st, 404)
        st, p = self.call("sh_import", {}, {"_csv": b"a;b\n1;2\n"}, {"id": sid})
        self.assertEqual((st, p["code"]), (409, "unknown_format"))
        st, p = self.call("sh_import", {}, {"_multipart_error": "too big", "_code": "too_large"}, {"id": sid})
        self.assertEqual((st, p["code"]), (400, "too_large"))
        st, p = self.call("sh_import", {"template": "generic"}, {"_csv": b"x"}, {"id": sid})
        self.assertEqual((st, set(p["errors"])), (400, {"profile_id"}))
        st, p = self.call("sh_account_get", match={"id": "sha-000000000000"})
        self.assertEqual(st, 404)
        st, p = self.call("sh_account_get", match={"id": sid})
        self.assertEqual(p["positions"][0]["qty"], 5.0)
        self.assertAlmostEqual(p["positions"][0]["cost"], 500.5)
        st, p = self.call("sh_transactions", {"limit": "2"}, match={"id": sid})
        self.assertEqual((len(p["transactions"]), p["total"]), (2, 3))
        # valuation + diagnostics + shadows + report
        st, p = self.call("sh_valuation", body={"as_of": "2024-06-28", "prices": [{"key": "US0378331005",
                                                                                   "price": 150.0}]}, match={"id": sid})
        self.assertEqual(st, 200, p)
        prices.set_source(closes({"SPY": [((date(2024, 1, 2) + timedelta(days=i)).isoformat(), 470.0 + i)
                                          for i in range(200)]}))
        put_rates({(date(2024, 1, 1) + timedelta(days=i)).isoformat(): 1.10 for i in range(200)})
        st, p = self.call("sh_diagnostics", match={"id": sid})
        self.assertEqual(st, 200, p)
        self.assertEqual(p["coverage"]["instruments"], 1)
        self.assertEqual((p["coverage"]["priced"], p["coverage"]["excluded"]), (1, []))      # priced by the valuation
        self.assertIn("disposition", p)
        json.dumps(p, allow_nan=False)
        st, p = self.call("sh_shadows", body={"rules": [{"type": "buy_and_hold", "symbol": "SPY"}]}, match={"id": sid})
        self.assertEqual(st, 200, p)
        self.assertTrue(p["user"]["valued"])
        self.assertAlmostEqual(p["user"]["end_value"], 750.0)
        self.assertTrue(p["shadows"][0]["parity"]["flows_equal"], p["shadows"][0])
        st, p = self.call("sh_shadows", body={"rules": []}, match={"id": sid})
        self.assertEqual(st, 400)
        st, p = self.call("sh_report", body={"format": "html"}, match={"id": sid})
        self.assertEqual(st, 200, p)
        html = p["content"]
        self.assertNotIn("<script", html.lower())
        self.assertIn("default-src 'none'", html)
        self.assertIn("UFOS.ai does not produce tax data or tax reports.", html)
        self.assertNotIn("=cmd|' /C", html)                   # escaped (&#x27;)
        st, p = self.call("sh_report", body={"format": "json"}, match={"id": sid})
        self.assertEqual(json.loads(p["content"])["account"]["id"], sid)
        st, p = self.call("sh_classification_put", body={"key": "US0378331005", "asset_class": "share"})
        self.assertEqual(st, 200, p)
        st, p = self.call("sh_templates")
        self.assertEqual({t["id"] for t in p["templates"]}, {"trade_republic", "scalable", "ibkr_flex", "generic"})
        st, p = self.call("sh_profile_create", body={"name": "my bank", "mapping": MAPPING})
        self.assertEqual(st, 201, p)
        self.assertEqual(p["profile"]["version"], 1)

    def test_market_prices_only_for_us_listings_raw_and_split_aware(self):
        """Review H2: raw closes for trade-price comparisons, splits explicit, market data only for US listings in
        USD — everything else excluded per instrument with the reason (never a ticker guess)."""
        acc = service.create_account("IBKR", "USD", "other")["id"]
        flex = ("ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Description,ISIN,TradeDate,Quantity,TradePrice,"
                "IBCommission,Buy/Sell\n"
                "U1,USD,STK,AAA,AAA INC,US0000000AA1,20240102,10,100,0,BUY\n"
                "U1,USD,STK,BBB,BBB INC,US0000000BB2,20240102,10,100,0,BUY\n"
                "U1,EUR,STK,SAP,SAP SE,DE0007164600,20240102,10,100,0,BUY\n"
                "U1,USD,STK,AAA,AAA INC,US0000000AA1,20240108,5,90,0,SELL\n")
        service.import_file(acc, flex.encode(), "f.csv", "auto", None, False)
        days = [(date(2024, 1, 2) + timedelta(days=i)).isoformat() for i in range(10)]
        total = {"AAA": [(d, 50.0) for d in days], "BBB": [(d, 200.0) for d in days]}
        raw_split = {"AAA": [(d, 95.0) for d in days], "BBB": [(d, 100.0) for d in days]}
        state = {"n": 0}

        def src(symbol, start, end):
            # Closes loads the "all" series first for series(), "split" for raw(): this fake serves the split series
            return raw_split[symbol] if symbol in raw_split else (_ for _ in ()).throw(ValueError("unknown"))
        prices.set_source(src, lambda s, a, b: [("2024-01-05", 2.0)] if s == "BBB" else [])
        ctx = service.Context(acc)
        self.assertEqual(ctx.price_b("US0000000AA1", "2024-01-08"), 95.0)       # raw close, no split
        self.assertIsNone(ctx.price_b("US0000000BB2", "2024-01-08"))            # split after the first purchase
        self.assertIn("stock split of BBB on 2024-01-05", ctx.excluded["US0000000BB2"])
        self.assertIsNone(ctx.price_b("DE0007164600", "2024-01-08"))
        self.assertEqual(ctx.excluded["DE0007164600"], "not available: no price source for DE0007164600 — no listing "
                                                        "mapped — pick its listing (Shadow Account → Symbol mapping)")
        self.assertEqual(ctx.closes.raw_on("BBB", "2024-01-04"), 200.0)         # before the 2:1 split: × 2
        d = service.diagnose(acc)
        self.assertEqual({e["key"] for e in d["coverage"]["excluded"]}, {"US0000000BB2", "DE0007164600"})
        self.assertEqual(d["disposition"]["excluded_instruments"], 2)
        self.assertEqual(d["disposition"]["unpriced_cases"], 1)                 # BBB (SAP: no EUR/USD rate → left out)
        _ = (total, state)

    def test_monitoring_routes_strict(self):
        st, p = self.call("mon_alerts", {"limit": "0"})
        self.assertEqual((st, set(p["errors"])), (400, {"limit"}))
        st, p = self.call("mon_alerts_ack", body={"ids": [1], "all": True})
        self.assertEqual(st, 400)
        st, p = self.call("mon_decision", body={"action": "demote", "reason": "because"},
                          match={"strategy_id": "s-1"})
        self.assertEqual((st, p["code"]), (404, "not_found"))          # fix1023: no confirm word needed
        st, p = self.call("mon_report_create", body={"period": "daily", "date": "2026-10-02"})
        self.assertEqual(st, 201, p)
        st, p = self.call("mon_report_get", match={"id": p["report"]["id"]})
        self.assertEqual(st, 200)
        st, p = self.call("mon_overview")
        self.assertEqual(st, 200)
        st, p = self.call("mon_tax_notice")
        self.assertEqual((st, p["tax_export"]), (200, False))
        self.assertTrue(p["notice"].startswith("UFOS.ai does not produce tax data or tax reports."))
        st, p = self.call("sh_fx_get", {"year": "2026"})
        self.assertEqual((st, p["series"]), (200, "DEXUSEU"))
        st, p = self.call("sh_fx_post", body={"year": 1990})
        self.assertEqual(st, 400)
        st, p = self.call("mon_decay_rules")
        self.assertEqual(p["rules"]["RETURN_Z"], 3.0)
        names = {r[2] for r in routes.ROUTES}
        self.assertTrue({"mon_overview", "sh_import", "sh_commentary", "mon_tax_notice", "sh_fx_get"} <= names)
        paths = {r[1] for r in routes.ROUTES}
        self.assertFalse([x for x in paths if x.startswith("/v2/monitoring/tax/")])    # user decision 2026-10-18


_ = math
if __name__ == "__main__":
    unittest.main()
