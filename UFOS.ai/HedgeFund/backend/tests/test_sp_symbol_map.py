"""SPEC §20.10 (user decision 2026-10-18, option (a)): the user's ISIN → US ticker mapping, validated against the
Alpaca asset list (fake), feeding the market-close price path of insights and diagnostics; refusals (unknown ticker,
non-US ISIN, crypto / OTC / inactive, no confirmation, asset list unavailable); delete → unpriced again; routes +
audit. Offline (fake assets, fake prices, stored FX fixings), temp databases."""
import asyncio
import unittest

try:
    from sp_monitor_testkit import MonCase, put_rates
    import tr_synthetic_fixture as fx
except ImportError:  # pragma: no cover
    from tests.sp_monitor_testkit import MonCase, put_rates
    from tests import tr_synthetic_fixture as fx

from strategy_platform.api import routes
from strategy_platform.api.routes import NO_BODY
from strategy_platform.execution.errors import TradingError
from strategy_platform.monitoring import insights, prices
from strategy_platform.monitoring.shadow import service, symbols
from strategy_platform.storage import db

SPGI = "US78409V1044"          # S&P Global (a valid US ISIN)
AAPL = "US0378331005"
ASSETS = [
    {"symbol": "SPGI", "name": "S&P Global Inc.", "asset_class": "us_equity", "exchange": "NYSE", "tradable": True,
     "status": "active"},
    {"symbol": "AAPL", "name": "Apple Inc.", "asset_class": "us_equity", "exchange": "NASDAQ", "tradable": True,
     "status": "active"},
    {"symbol": "BTC/USD", "name": "Bitcoin", "asset_class": "crypto", "exchange": "CRYPTO", "tradable": True,
     "status": "active"},
    {"symbol": "PINKY", "name": "Pink sheet", "asset_class": "us_equity", "exchange": "OTC", "tradable": True,
     "status": "active"},
    {"symbol": "HALT", "name": "Halted", "asset_class": "us_equity", "exchange": "NYSE", "tradable": False,
     "status": "active"},
]
CLOSES = {"SPGI": [("2026-09-30", 480.0), ("2026-10-01", 500.0)]}


def tr_buy(n, isin, qty, price, cur="EUR"):
    return fx.row("2026-03-02T10:00:00.000Z", "TRADING", "BUY", n, ac="STOCK", name="Some AG", sym=isin,
                  shares=str(qty), price=str(price), amount=f"{-qty * price:.2f}", fee="-1.00", cur=cur)


class SymbolMapTest(MonCase):
    def setUp(self):
        super().setUp()
        symbols.set_assets_source(lambda: ASSETS)
        self.addCleanup(symbols.set_assets_source, None)
        prices.set_source(lambda s, a, b: CLOSES[s] if s in CLOSES else (_ for _ in ()).throw(ValueError("unknown")),
                          lambda s, a, b: [])
        put_rates({"2026-09-30": 1.2, "2026-10-01": 1.25})
        self.aid = service.create_account("TR", "EUR", "DE")["id"]
        # SPGI bought in USD (same currency as its US listing), AAPL bought in EUR (TR), a German share in EUR
        rows = [tr_buy(1, SPGI, 2, 400, "USD"), tr_buy(2, "DE0005140008", 10, 20), tr_buy(3, AAPL, 3, 150)]
        CLOSES["AAPL"] = [("2026-09-30", 200.0), ("2026-10-01", 210.0)]
        service.import_file(self.aid, fx.to_csv(rows), "tr.csv", "auto", None, False)

    def pos(self):
        a = insights.portfolio(2026, self.aid)["accounts"][0]
        return {p["key"]: p for p in a["positions"]}

    def call(self, name, query=None, body=NO_BODY, match=None):
        return asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))

    def test_valid_mapping_prices_the_position(self):
        before = self.pos()[SPGI]
        self.assertEqual(before["price_source"], "none")
        self.assertIn("no listing mapped — pick its listing (Shadow Account → Symbol mapping)", before["note"])
        m = symbols.put(SPGI, "spgi", True, "TR has no tickers")
        self.assertEqual((m["symbol"], m["exchange"], m["source"], m["asset_name"], m["price_source"], m["currency"]),
                         ("SPGI", "NYSE", "user_mapping", "S&P Global Inc.", "alpaca_us", "USD"))
        p = self.pos()[SPGI]
        # same currency (USD cost, USD listing): value + P&L; cost 801 USD
        self.assertEqual((p["price_source"], p["price"], p["as_of"], p["value"], p["unrealised"]),
                         ("market", 500.0, "2026-10-01", 1000.0, 199.0))
        self.assertEqual((p["market_symbol"], p["symbol_source"], p["price_provider"], p["listing"]),
                         ("SPGI", "user_mapping", "alpaca_us", None))
        self.assertEqual(p["price_detail"], "market close via mapped symbol SPGI (user mapping)")
        self.assertEqual(p["day_change"], 2 * (500.0 - 480.0))
        self.assertNotIn("fx", p)
        cov = service.diagnose(self.aid)["coverage"]
        self.assertIn({"key": SPGI, "symbol": "SPGI", "source": "user_mapping", "provider": "alpaca_us",
                       "price_source": "market close via mapped symbol SPGI (user mapping)"}, cov["price_sources"])

    def test_fx_revert_other_currency_has_value_but_no_pnl(self):
        symbols.put(AAPL, "AAPL", True, None)
        p = self.pos()[AAPL]                        # bought in EUR at TR, listing in USD
        self.assertEqual((p["price_source"], p["price"], p["value"], p["unrealised"], p["day_change"]),
                         ("market_other_currency", None, None, None, None))
        self.assertEqual(p["listing"], {"currency": "USD", "price": 210.0, "value": 630.0, "as_of": "2026-10-01"})
        self.assertEqual(p["note"], "different currencies — not converted (listing USD, cost EUR)")
        self.assertEqual(p["price_detail"], "market close via mapped symbol AAPL (user mapping) — USD 630.00; "
                                            "different currencies — not converted")
        a = insights.portfolio(2026, self.aid)["accounts"][0]
        self.assertIn("1 position(s) valued only in another currency (no P&L)", a["reasons"])
        # the card's "unpriced" count follows the same rule: a position valued in another currency is not unpriced
        # (live test 2026-10-19: positions - valued counted it)
        su = insights.summary()
        nr = [r for r in a["reasons"] if r.endswith("position(s) without a price source")]
        self.assertEqual(su["unpriced"], int(nr[0].split()[0]) if nr else 0)
        self.assertEqual(len(su["currencies"]) and sum(c["positions"] - c["valued"] for c in su["currencies"]) - su["unpriced"], 1)
        eur = next(x for x in a["allocation"] if x["currency"] == "EUR")
        self.assertEqual(eur["market_value"], None)                          # nothing USD summed into EUR
        ex = {e["key"]: e["reason"] for e in service.diagnose(self.aid)["coverage"]["excluded"]}
        self.assertIn("different currencies — not converted (listing USD, trades EUR)", ex[AAPL])

    def test_unknown_ticker_and_other_refusals(self):
        for sym, code, text in (("NOPE", "symbol_refused", "not in the Alpaca asset list"),
                                ("PINKY", "symbol_refused", "OTC"), ("HALT", "symbol_refused", "not tradable")):
            with self.assertRaises(TradingError) as cm:
                symbols.put(SPGI, sym, True, None)
            self.assertEqual(cm.exception.code, code, sym)
            self.assertIn(text, cm.exception.user_message)
        self.assertEqual(symbols.put(SPGI, "SPGI", False, None)["confirmed_via"], "dialog")   # fix1023: no flag needed
        with self.assertRaises(TradingError) as cm:
            symbols.put("US78409V1045", "SPGI", True, None)              # wrong check digit
        self.assertEqual(cm.exception.code, "invalid_isin")
        symbols.put(SPGI, "SPGI", True, None)
        with self.assertRaises(TradingError) as cm:
            symbols.put(AAPL, "SPGI", True, None)                       # one ticker, one ISIN
        self.assertIn("already mapped", cm.exception.user_message)
        symbols.set_assets_source(lambda: (_ for _ in ()).throw(RuntimeError("no Alpaca keys")))
        with self.assertRaises(TradingError) as cm:
            symbols.put(AAPL, "AAPL", True, None)
        self.assertEqual((cm.exception.status, cm.exception.code), (503, "symbol_check_unavailable"))
        self.assertNotIn(AAPL, symbols.mappings())                      # nothing saved unverified

    def test_non_us_isin_refused_for_market_data(self):
        with self.assertRaises(TradingError) as cm:
            symbols.put("DE0005140008", "AAPL", True, None)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "non_us_isin"))
        p = self.pos()["DE0005140008"]
        self.assertEqual(p["price_source"], "none")
        self.assertIn("no listing mapped — pick its listing", p["note"])           # an EODHD listing can (§20.11)

    def test_delete_mapping_unprices_again(self):
        symbols.put(SPGI, "SPGI", True, None)
        self.assertEqual(self.pos()[SPGI]["price_source"], "market")
        r = symbols.delete(SPGI, "wrong")
        self.assertEqual(r["removed"]["symbol"], "SPGI")
        self.assertEqual(self.pos()[SPGI]["price_source"], "none")
        with self.assertRaises(TradingError) as cm:
            symbols.delete(SPGI, None)
        self.assertEqual(cm.exception.status, 404)
        with db._connect() as conn:                                      # insert-only history
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sh_symbol_maps").fetchone()[0], 2)
            acts = [r[0] for r in conn.execute("SELECT action FROM audit WHERE action LIKE 'shadow_symbol%' "
                                               "ORDER BY id").fetchall()]
        self.assertEqual(acts, ["shadow_symbol_map", "shadow_symbol_unmap"])

    def test_routes(self):
        st, p = self.call("sh_symbol_map_put", body={"isin": "bad", "symbol": "SPGI"})    # fix1023: no confirm flag
        self.assertEqual((st, set(p["errors"])), (400, {"isin"}))
        st, p = self.call("sh_symbol_map_put", body={"isin": SPGI, "symbol": "SPGI", "confirm_us_listing": True,
                                                     "x": 1})
        self.assertEqual((st, set(p["errors"])), (400, {"x"}))
        st, p = self.call("sh_symbol_map_put", body={"isin": SPGI, "symbol": "NOPE", "confirm_us_listing": True})
        self.assertEqual((st, p["code"]), (409, "symbol_refused"))
        st, p = self.call("sh_symbol_map_put", body={"isin": SPGI, "symbol": "SPGI", "confirm_us_listing": True,
                                                     "reason": "checked on the issuer site"})
        self.assertEqual((st, p["mapping"]["symbol"], p["mapping"]["reason"]), (200, "SPGI", "checked on the issuer site"))
        st, p = self.call("sh_symbol_maps")
        self.assertEqual((st, p["scope"], [m["isin"] for m in p["mappings"]]), (200, "global", [SPGI]))
        st, p = self.call("sh_symbol_map_delete", match={"isin": SPGI})
        self.assertEqual((st, p["removed"]["isin"]), (200, SPGI))
        st, p = self.call("sh_symbol_map_delete", match={"isin": SPGI})
        self.assertEqual((st, p["code"]), (404, "not_found"))
        paths = {(m, pth) for m, pth, _n, _b in routes.ROUTES}
        self.assertIn(("DELETE", "/v2/shadow/symbol-mappings/{isin}"), paths)


def T(day, kind, key, qty=None, price=None, fee=0.0, gross=None):
    from strategy_platform.monitoring.shadow import normalize
    t = normalize.make_txn(date_=day, kind=kind, symbol=key, qty=qty, price=price, fee=fee, gross=gross,
                           currency="USD")
    t.update({"fx": 1.0, "gross_b": t["gross"], "fee_b": t["fee"], "tax_b": t["tax"], "net_b": t["net"],
              "price_b": t["price"]})
    return t


class AuditFindingsTest(MonCase):
    """Live audit report 2026-10-18: the disposition coverage rule; one fee number in insights and diagnostics."""

    def test_disposition_not_meaningful_below_the_coverage_rule(self):
        from strategy_platform.monitoring.shadow import diagnostics, report
        keys = [f"K{i}" for i in range(10)]
        tx = [T("2024-01-01", "buy", k, 10, 10) for k in keys] + [T("2024-01-05", "sell", "K0", 10, 12)]
        # 1 realised + 9 paper cases; 8 of them priced → 9 / 10 = 0.9 ≥ 0.8 → numbers
        px = {k: 11.0 for k in keys[1:9]}
        d = diagnostics.disposition(tx, lambda k, day: px.get(k))
        self.assertEqual((d["cases"], d["unpriced_cases"], d["priced_share"]), (10, 1, 0.9))
        self.assertIsNotNone(d["pgr"])
        # exactly at the threshold (8 of 10 priced) is still shown
        px8 = {k: 11.0 for k in keys[1:8]}
        self.assertIn("pgr", diagnostics.disposition(tx, lambda k, day: px8.get(k)))
        # 1 of 10 priced → not meaningful, no percentages at all
        d = diagnostics.disposition(tx, lambda k, day: None)
        self.assertTrue(d["not_meaningful"])
        self.assertNotIn("pgr", d)
        self.assertEqual(d["unavailable"], "not meaningful: 9 of 10 cases unpriced (add symbol mappings or valuation "
                                           "prices)")
        self.assertEqual(diagnostics.MIN_PRICED_SHARE, 0.8)
        self.assertEqual(diagnostics.facts({"disposition": d}), [])           # nothing for the AI commentary either
        h = report._block("Disposition effect", d, [("pgr", "PGR", "pct")])
        self.assertEqual(h, "<h3>Disposition effect</h3><p class=muted>Not meaningful: 9 of 10 cases unpriced (add "
                            "symbol mappings or valuation prices)</p>")

    def test_fees_are_the_same_number_in_insights_and_diagnostics(self):
        prices.set_source(lambda s, a, b: (_ for _ in ()).throw(ValueError("offline")), lambda s, a, b: [])
        aid = service.create_account("fees", "EUR", "DE")["id"]
        rows = [dict(r, fee="-1.00") if r["type"] == "PRIVATE_MARKET_BUY" else r for r in fx.ROWS]
        service.import_file(aid, fx.to_csv(rows), "f.csv", "auto", None, False)
        ins = next(c for c in insights.portfolio(2025, aid)["accounts"][0]["years"]["2025"]["currencies"]
                   if c["currency"] == "EUR")["fees"]
        dia = service.diagnose(aid)["turnover"]["fees"]
        self.assertEqual((ins, round(dia, 2)), (11.0, 11.0))                  # 10 trade fees + the pending order fee


if __name__ == "__main__":
    unittest.main()
