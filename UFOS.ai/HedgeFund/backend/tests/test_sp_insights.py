"""Stage 9b (SPEC §20): portfolio insights over the Shadow Account imports — FIFO across a year boundary, partial
sales, brokerage (stocks + ETFs, §20.9) / other / unclassified, dividends with withholding, fees as given, multi-currency WITHOUT conversion,
every price-source path (import valuation, user valuation, market raw close for a US listing, not available),
coverage and concentration, the previous year, no imports, the routes. Offline (fake prices), temp databases."""
import asyncio
import unittest

try:
    from sp_monitor_testkit import MonCase
except ImportError:  # pragma: no cover
    from tests.sp_monitor_testkit import MonCase

from strategy_platform.api import routes
from strategy_platform.api.routes import NO_BODY
from strategy_platform.monitoring import insights, prices
from strategy_platform.monitoring.shadow import service

CSV = ("Date,Type,Symbol,ISIN,Name,Qty,Price,Amount,Fee,Tax,Currency,Class\n"
       "2025-06-02,B,AAA,,AAA AG,10,100,,1,,EUR,share\n"
       "2025-12-15,S,AAA,,AAA AG,4,120,,1,,EUR,share\n"
       "2026-02-02,S,AAA,,AAA AG,4,90,,1,,EUR,share\n"
       "2026-03-02,B,,IE00B4L5Y983,World ETF,5,50,,0,,EUR,etf\n"
       "2026-04-01,S,,IE00B4L5Y983,World ETF,2,60,,0,,EUR,etf\n"
       "2026-04-02,B,XYZ,,Mystery,1,10,,0,,EUR,\n"
       "2026-05-04,S,XYZ,,Mystery,1,12,,0,,EUR,\n"
       "2026-05-05,D,AAA,,AAA AG,,,10,,2.5,EUR,share\n"
       "2026-06-01,F,,,Depot fee,,,5,,,EUR,\n"
       "2026-01-05,B,MSFT,US5949181045,Microsoft,2,300,,1,,USD,share\n"
       "2026-07-01,S,NVDA,US67066G1040,Nvidia,1,100,,0,,USD,share\n"
       "2026-03-03,W,MSFT,US5949181045,Microsoft,,,1.5,,1.5,USD,share\n")
MAPPING = {"delimiter": ",", "decimal": ".", "thousands": "", "date_format": "%Y-%m-%d", "encoding": "auto",
           "columns": {"date": "Date", "type": "Type", "symbol": "Symbol", "isin": "ISIN", "name": "Name",
                       "quantity": "Qty", "price": "Price", "amount": "Amount", "fee": "Fee", "tax": "Tax",
                       "currency": "Currency", "asset_class": "Class"},
           "type_map": {"B": "buy", "S": "sell", "D": "dividend", "F": "fee", "W": "withholding"},
           "default_currency": "EUR", "amount_sign": "positive"}
IBKR = ("ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Description,ISIN,TradeDate,Quantity,TradePrice,"
        "IBCommission,Buy/Sell\n"
        "U1,USD,STK,TSLA,TESLA,US88160R1014,20260102,1,200,0,BUY\n"
        "ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Description,ISIN,ReportDate,Quantity,MarkPrice,PositionValue\n"
        "U1,USD,STK,TSLA,TESLA,US88160R1014,20260930,1,250,250\n")


def by_cur(block, cur):
    return next(c for c in block["currencies"] if c["currency"] == cur)


class InsightsTest(MonCase):
    def setUp(self):
        super().setUp()
        self.a = service.create_account("Bank A", "EUR", "DE")["id"]
        pid = service.save_profile("bank a", MAPPING)["id"]
        r = service.import_file(self.a, CSV.encode(), "a.csv", "generic", pid, False)
        self.assertEqual(r["counts"]["rejected"], 0, r["errors"])
        service.put_valuation(self.a, "2026-09-30", [{"key": "AAA", "price": 95.0}])
        self.b = service.create_account("IBKR", "USD", "other")["id"]
        r = service.import_file(self.b, IBKR.encode(), "flex.csv", "auto", None, False)
        self.assertEqual(r["counts"]["positions"], 1)
        closes = {"MSFT": [("2026-09-29", 400.0), ("2026-09-30", 410.0), ("2026-10-01", 420.0)],
                  "TSLA": [("2026-10-01", 999.0)]}
        prices.set_source(lambda s, a, b: closes[s] if s in closes else (_ for _ in ()).throw(ValueError("unknown")),
                          lambda s, a, b: [])

    def pick(self, d, aid):
        return next(x for x in d["accounts"] if x["account_id"] == aid)

    def test_realised_fifo_buckets_and_years(self):
        d = insights.portfolio()
        self.assertEqual((d["year"], d["previous_year"]), (2026, 2025))       # the test clock: 2026-10-02
        a = self.pick(d, self.a)
        eur = by_cur(a["years"]["2026"], "EUR")
        # §20.9: the AAA share (−41.4) and the World ETF (+20) are both "brokerage"
        self.assertEqual(eur["realised"]["brokerage"], {"gains": 20.0, "losses": -41.4, "net": -21.4, "parts": 2})
        self.assertEqual((eur["realised"]["other"]["net"], eur["realised"]["unclassified"]["net"]), (0.0, 2.0))
        self.assertEqual(eur["realised"]["net"], -19.4)
        self.assertEqual((eur["dividends"], eur["withholding"], eur["other_fees"], eur["trading_fees"], eur["fees"]),
                         (10.0, 2.5, 5.0, 1.0, 6.0))
        prev = by_cur(a["years"]["2025"], "EUR")
        self.assertEqual((prev["realised"]["brokerage"]["net"], prev["trading_fees"]), (78.6, 2.0))
        usd = by_cur(a["years"]["2026"], "USD")                    # never converted / mixed with EUR
        self.assertEqual((usd["realised"]["net"], usd["withholding"], usd["trading_fees"]), (0.0, 1.5, 1.0))
        self.assertEqual([(n["key"], n["qty"]) for n in a["not_used"]], [("US67066G1040", 1.0)])
        yoy = next(r for r in d["yoy"] if r["currency"] == "EUR")
        self.assertEqual(yoy["realised_net"], {"current": -19.4, "previous": 78.6, "delta": -98.0})
        self.assertEqual(a["state"], "incomplete")
        self.assertEqual(len(a["reasons"]), 3)                     # unpriced ETF, unmatched NVDA, unclassified XYZ
        # the previous year on its own
        d25 = insights.portfolio(2025, self.a)
        self.assertEqual((d25["year"], d25["previous_year"]), (2025, 2024))
        self.assertEqual(by_cur(self.pick(d25, self.a)["years"]["2025"], "EUR")["realised"]["net"], 78.6)

    def test_price_paths_coverage_and_allocation(self):
        d = insights.portfolio()
        pos = {p["key"]: p for p in self.pick(d, self.a)["positions"]}
        self.assertEqual(set(pos), {"AAA", "IE00B4L5Y983", "US5949181045"})
        aaa = pos["AAA"]
        self.assertEqual((aaa["price_source"], aaa["valuation_source"], aaa["as_of"], aaa["value"], aaa["unrealised"]),
                         ("valuation", "user", "2026-09-30", 190.0, -10.2))
        etf = pos["IE00B4L5Y983"]
        self.assertEqual((etf["price_source"], etf["value"]), ("none", None))
        self.assertTrue(etf["note"].startswith("not available (no price source)"))
        msft = pos["US5949181045"]
        self.assertEqual((msft["price_source"], msft["price"], msft["as_of"], msft["day_change"], msft["unrealised"]),
                         ("market", 420.0, "2026-10-01", 20.0, 239.0))
        alloc = {x["currency"]: x for x in self.pick(d, self.a)["allocation"]}
        self.assertEqual((alloc["EUR"]["cost_valued"], alloc["EUR"]["cost_total"], alloc["EUR"]["coverage_pct"]),
                         (200.2, 350.2, 57.17))
        self.assertEqual((alloc["EUR"]["market_value"], alloc["EUR"]["concentration"]["top1_pct"]), (190.0, 100.0))
        self.assertEqual((alloc["USD"]["coverage_pct"], alloc["USD"]["day_change"]), (100.0, 20.0))
        # import valuation first (even though market data exists for TSLA)
        tsla = self.pick(d, self.b)["positions"][0]
        self.assertEqual((tsla["price_source"], tsla["valuation_source"], tsla["price"], tsla["as_of"]),
                         ("valuation", "import", 250.0, "2026-09-30"))
        tot = {x["currency"]: x for x in d["totals"]["allocation"]}
        self.assertEqual(tot["USD"]["market_value"], 1090.0)        # 840 MSFT + 250 TSLA, both USD — no FX
        self.assertEqual(d["label"], insights.LABEL)
        self.assertIn("does not produce tax data", d["tax_notice"])

    def test_import_valuation_dropped_with_its_import(self):
        imp = service.imports_of(self.b)[0]["id"]
        self.assertEqual([r["source"] for r in service.valuation_rows(self.b)], ["import"])
        service.remove_import(self.b, imp, "REMOVE IMPORT", "test")
        self.assertEqual(service.valuation_rows(self.b), [])
        self.assertEqual(self.pick(insights.portfolio(), self.b)["positions"], [])

    def test_summary(self):
        s = insights.summary()
        self.assertEqual((s["state"], s["accounts"], s["year"]), ("incomplete", 2, 2026))
        self.assertEqual(s["headline"], "Realised 2026: −19.40 EUR")
        self.assertIn("coverage 57 %", s["detail"])
        self.assertIn("2 depots", s["detail"])
        self.assertEqual(s["unpriced"], 1)                  # the ETF without a price source
        eur = next(c for c in s["currencies"] if c["currency"] == "EUR")
        self.assertEqual((eur["realised_brokerage"], eur["previous"]["realised_net"]), (-21.4, 78.6))
        self.assertEqual(s["as_of"], {"valuation": "2026-09-30", "market": "2026-10-01"})

    def test_routes(self):
        api = routes.V2Api()

        def call(name, q=None):
            return asyncio.run(getattr(api, name)(q or {}, NO_BODY, {}))
        self.assertEqual(call("insights_summary")[0], 200)
        self.assertEqual(call("insights_summary", {"x": "1"})[0], 400)
        st, p = call("insights_portfolio", {"year": "2025", "account": self.a})
        self.assertEqual((st, p["year"], len(p["accounts"])), (200, 2025, 1))
        self.assertEqual(call("insights_portfolio", {"year": "1990"})[0], 400)
        self.assertEqual(call("insights_portfolio", {"account": "sha-000000000000"})[0], 404)
        paths = {r[1] for r in routes.ROUTES}
        self.assertTrue({"/v2/insights/summary", "/v2/insights/portfolio"} <= paths)


class NamedCasesTest(InsightsTest):
    """The review's checklist, one case per test (same synthetic depots as InsightsTest)."""

    def year(self, y, cur="EUR"):
        return by_cur(self.pick(insights.portfolio(), self.a)["years"][str(y)], cur)

    def test_fifo_across_the_year_boundary(self):
        # bought 10 in 2025 (cost 1001), 4 sold in 2025 (+78.6), 4 sold in 2026 from the SAME 2025 lot (−41.4)
        self.assertEqual(self.year(2025)["realised"]["brokerage"]["net"], 78.6)
        self.assertEqual(self.year(2026)["realised"]["brokerage"]["net"], -41.4 + 20.0)       # + the ETF sale

    def test_partial_sale(self):
        pos = {p["key"]: p for p in self.pick(insights.portfolio(), self.a)["positions"]}
        self.assertEqual((pos["AAA"]["qty"], pos["AAA"]["cost"]), (2.0, 200.2))           # 10 − 4 − 4
        self.assertEqual((pos["IE00B4L5Y983"]["qty"], pos["IE00B4L5Y983"]["cost"]), (3.0, 150.0))   # 5 − 2

    def test_brokerage_other_unclassified(self):
        r = self.year(2026)["realised"]
        self.assertEqual((r["brokerage"]["parts"], r["other"]["parts"], r["unclassified"]["parts"]), (2, 0, 1))
        self.assertEqual([b["key"] for b in insights.portfolio()["buckets"]], ["brokerage", "other", "unclassified"])
        self.assertEqual(insights.BUCKET_LABELS["brokerage"], "Brokerage (stocks + ETFs)")
        self.assertEqual([insights._bucket(c) for c in ("share", "etf", "fund", "bond", "crypto", "derivative",
                                                        "other", None)],
                         ["brokerage"] * 3 + ["other"] * 4 + ["unclassified"])

    def test_dividends_with_withholding(self):
        y = self.year(2026)
        self.assertEqual((y["dividends"], y["withholding"]), (10.0, 2.5))
        self.assertEqual(self.year(2026, "USD")["withholding"], 1.5)

    def test_multi_currency_without_conversion(self):
        d = insights.portfolio()
        curs = [c["currency"] for c in d["totals"]["years"]["2026"]["currencies"]]
        self.assertEqual(curs, ["EUR", "USD"])
        self.assertTrue(d["limits"][0].startswith("Original currency everywhere — no FX conversion."))

    def test_previous_year(self):
        self.assertEqual(self.year(2025)["trading_fees"], 2.0)
        self.assertEqual(insights.portfolio(2025)["previous_year"], 2024)


def generic_account(test, label, rows, currency="EUR"):
    aid = service.create_account(label, currency, "other")["id"]
    pid = service.save_profile(f"{label} profile", MAPPING)["id"]
    r = service.import_file(aid, ("Date,Type,Symbol,ISIN,Name,Qty,Price,Amount,Fee,Tax,Currency,Class\n" + rows).encode(),
                            f"{label}.csv", "generic", pid, False)
    test.assertEqual(r["counts"]["rejected"], 0, r["errors"])
    return aid


class ReviewFixesTest(MonCase):
    def test_flex_qty_mismatch_and_positions_without_lots(self):
        flex = ("ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Description,ISIN,TradeDate,Quantity,TradePrice,"
                "IBCommission,Buy/Sell\n"
                "U1,USD,STK,TSLA,TESLA,US88160R1014,20260102,1,200,0,BUY\n"
                "ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Description,ISIN,ReportDate,Quantity,MarkPrice\n"
                "U1,USD,STK,TSLA,TESLA,US88160R1014,20260930,30,50\n"
                "U1,USD,STK,AAPL,APPLE,US0378331005,20260930,5,180\n")
        aid = service.create_account("IBKR", "USD", "other")["id"]
        service.import_file(aid, flex.encode(), "f.csv", "auto", None, False)
        a = insights.portfolio()["accounts"][0]
        pos = {p["key"]: p for p in a["positions"]}
        t = pos["US88160R1014"]
        self.assertEqual((t["price_source"], t["value"], t["broker_qty"], t["qty"]), ("none", None, 30.0, 1.0))
        self.assertIn("broker reports 30, imported lots give 1", t["note"])
        ap = pos["US0378331005"]                                      # never dropped
        self.assertEqual((ap["cost"], ap["broker_qty"], ap["price_source"]), (None, 5.0, "none"))
        self.assertIn("no imported lots", ap["note"])
        self.assertEqual(a["state"], "incomplete")
        self.assertTrue(any("differs from the imported lots" in r for r in a["reasons"]))
        self.assertTrue(any("without imported lots" in r for r in a["reasons"]))
        al = a["allocation"][0]
        self.assertEqual((al["coverage_pct"], al["valued"], al["positions"]), (0.0, 0, 2))
        self.assertIn("USD: nothing valued", insights.summary()["detail"])

    def test_flex_parser_limits(self):
        from strategy_platform.monitoring.shadow import importers
        flex = ("ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Description,ISIN,ReportDate,Quantity,MarkPrice\n"
                "U1,USD,STK,AAA,A,US0000000010,20260930,-5,10\n"             # short → rejected explicitly
                "U1,USD,STK,BBB,B,US0000000020,20260930,100000000000000,10\n"  # beyond the bound
                "U1,USD,STK,CCC,C,US123,20260930,1,10\n"                     # bad ISIN
                "U1,USD,STK,DDD,D,US0000000040,20260930,1,=cmd|' /C calc'!A0\n"  # formula in a number
                "U1,USD,STK,EEE,E,US0000000050,20260930,1e5,10\n"
                "U1,USD,STK,=HYPERLINK(1),F,US0000000060,20260930,2,10\n")   # formula text stays inert data
        r = importers.parse_file(flex.encode())
        msgs = [e["message"] for e in r["errors"]]
        self.assertEqual(len(r["errors"]), 5, r["errors"])
        self.assertIn("short positions are not supported (negative Quantity)", msgs)
        self.assertIn("too large", msgs)
        self.assertIn("not a valid ISIN", msgs)
        self.assertEqual([p["key"] for _n, p in r["positions"]], ["US0000000060"])
        self.assertEqual(r["positions"][0][1]["symbol"], "=HYPERLINK(1)")

    def test_user_valuation_by_currency(self):
        aid = generic_account(self, "two", "2026-01-05,B,SAP,DE0007164600,SAP,1,100,,0,,EUR,share\n"
                                           "2026-01-06,B,SAP,DE0007164600,SAP,1,110,,0,,USD,share\n")
        service.put_valuation(aid, "2026-09-30", [{"key": "DE0007164600", "price": 120.0}])
        pos = insights.portfolio()["accounts"][0]["positions"]
        self.assertTrue(all(p["price_source"] == "none" and "has no currency" in p["note"] for p in pos))
        service.put_valuation(aid, "2026-09-30", [{"key": "DE0007164600", "price": 120.0, "currency": "EUR"}])
        pos = {p["currency"]: p for p in insights.portfolio()["accounts"][0]["positions"]}
        self.assertEqual((pos["EUR"]["price_source"], pos["EUR"]["value"]), ("valuation", 120.0))
        self.assertEqual(pos["USD"]["price_source"], "none")

    def test_crypto_symbol_in_usd_gets_no_market_price(self):
        prices.set_source(lambda s, a, b: [("2026-10-01", 60000.0)], lambda s, a, b: [])
        generic_account(self, "crypto", "2026-01-05,B,BTC,,Bitcoin,1,40000,,0,,USD,\n", "USD")
        p = insights.portfolio()["accounts"][0]["positions"][0]
        self.assertEqual(p["price_source"], "none")
        self.assertIn("no ISIN in the import", p["note"])

    def test_taxes_on_sales_reported_separately(self):
        generic_account(self, "tax", "2026-01-05,B,AAA,,A,10,10,,0,,EUR,share\n"
                                     "2026-02-05,S,AAA,,A,10,20,,0,26.38,EUR,share\n")
        y = by_cur(insights.portfolio()["totals"]["years"]["2026"], "EUR")
        self.assertEqual((y["realised"]["brokerage"]["net"], y["taxes_on_sales"]), (100.0, 26.38))
        self.assertEqual(insights.summary()["currencies"][0]["taxes_on_sales"], 26.38)

    def test_coverage_maths(self):
        aid = generic_account(self, "cov", "2026-01-05,B,AAA,,A,1,300,,0,,EUR,share\n"
                                           "2026-01-05,B,BBB,,B,1,100,,0,,EUR,share\n")
        service.put_valuation(aid, "2026-09-30", [{"key": "AAA", "price": 330.0}])
        al = insights.portfolio()["accounts"][0]["allocation"][0]
        self.assertEqual((al["cost_valued"], al["cost_total"], al["coverage_pct"], al["market_value"],
                          al["unrealised"]), (300.0, 400.0, 75.0, 330.0, 30.0))

    def test_fifty_positions_stay_cheap(self):
        import time as _t
        calls, starts = [], []

        def src(symbol, start, end):
            calls.append(symbol)
            starts.append(start)
            return [("2026-09-30", 10.0), ("2026-10-01", 11.0)]
        prices.set_source(src, lambda s, a, b: [])
        rows = "".join(f"2020-01-0{1 + i % 9},B,S{i},US{i:09d}0,N{i},1,10,,0,,USD,share\n" for i in range(50))
        generic_account(self, "fifty", rows, "USD")
        t0 = _t.monotonic()
        s = insights.summary()
        dt = _t.monotonic() - t0
        self.assertLess(dt, 3.0)
        self.assertEqual(len(calls), 50)                               # one price load per symbol
        self.assertTrue(all(st.isoformat() >= "2026-09-12" for st in starts))   # a ~20-day window, not since 2020
        self.assertEqual(s["currencies"][0]["valued"], 50)


class NoImportsTest(MonCase):
    def test_no_imports(self):
        s = insights.summary()
        self.assertEqual((s["state"], s["currencies"], s["headline"]), ("no_imports", [], "No depot imported yet"))
        service.create_account("empty", "EUR", "DE")
        d = insights.portfolio()
        self.assertEqual(d["state"], "no_imports")
        self.assertEqual(d["accounts"][0]["state"], "no_imports")


if __name__ == "__main__":
    unittest.main()
