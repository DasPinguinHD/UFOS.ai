"""Stage 9d (SPEC §20.11): price sources in the ORIGINAL currency — EODHD (ISIN search parsing, the user's listing
pick, EOD closes in the listing's currency, the daily call budget incl. exhaustion, the daily cache, the key never in
a response), Kraken public (fixed BTC / ETH / SOL → EUR pairs, the uncommitted frame dropped, public path only),
no source for derivatives / bonds, the shadow conversion label. Fakes only — never the network."""
import asyncio
import io
import json
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

try:
    from sp_monitor_testkit import MonCase
    import tr_synthetic_fixture as fx
except ImportError:  # pragma: no cover
    from tests.sp_monitor_testkit import MonCase
    from tests import tr_synthetic_fixture as fx

from strategy_platform import secrets
from strategy_platform.api import routes
from strategy_platform.api.routes import NO_BODY
from strategy_platform.data import http_gate
from strategy_platform.data.providers import eodhd, kraken_public
from strategy_platform.execution.errors import TradingError
from strategy_platform.monitoring import insights, prices
from strategy_platform.monitoring.shadow import service, symbols
from strategy_platform.storage import db

KEY = "SECRET-EODHD-KEY-123"
SAP = "DE0007164600"
SEARCH = [
    {"Code": "SAP", "Exchange": "XETRA", "Name": "SAP SE", "Type": "Common Stock", "Country": "Germany",
     "Currency": "EUR", "ISIN": SAP, "isPrimary": True, "previousClose": 120.5, "previousCloseDate": "2026-10-01"},
    {"Code": "SAP", "Exchange": "US", "Name": "SAP SE ADR", "Type": "Common Stock", "Country": "USA",
     "Currency": "USD", "ISIN": "US8030542042", "isPrimary": False},                 # another ISIN → dropped
    {"Code": "SAPA", "Exchange": "F", "Name": "SAP SE", "Type": "Common Stock", "Country": "Germany",
     "Currency": "EUR", "ISIN": SAP, "isPrimary": False, "previousClose": "120.1"},
    {"Code": "SAP", "Exchange": "XETRA", "ISIN": SAP, "Currency": "EUR"},              # duplicate → dropped
    "junk", {"Exchange": "XETRA"},
]
EOD = [{"date": "2026-09-30", "open": 1, "high": 1, "low": 1, "close": 118.0, "adjusted_close": 117, "volume": 5},
       {"date": "2026-10-01", "open": 1, "high": 1, "low": 1, "close": 121.0, "adjusted_close": 120, "volume": 5}]


def ts(day):
    return int(datetime.fromisoformat(day + "T00:00:00+00:00").timestamp())


KRAKEN = {"error": [], "result": {"XXBTZEUR": [[ts("2026-09-30"), "1", "1", "1", "50000.0", "1", "1", 1],
                                               [ts("2026-10-01"), "1", "1", "1", "52000.5", "1", "1", 1],
                                               [ts("2026-10-02"), "1", "1", "1", "99999.0", "1", "1", 1]],
                                  "last": ts("2026-10-01")}}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Net:
    def __init__(self):
        self.urls = []
        self.fail = None
        self.fail_eod = None
        self.delay = 0.0
        self.lock = threading.Lock()

    def __call__(self, req, timeout=None, context=None):
        url = req.full_url
        with self.lock:
            self.urls.append(url)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise urllib.error.HTTPError(url, self.fail, f"fail {url}", {}, None)
        if self.fail_eod and "/api/eod/" in url:
            raise urllib.error.HTTPError(url, self.fail_eod, "fail", {}, None)
        if "api.kraken.com" in url:
            return Resp(json.dumps(KRAKEN).encode())
        if "/api/search/" in url:
            return Resp(json.dumps(SEARCH).encode())
        if "/api/eod/" in url:
            return Resp(json.dumps(EOD).encode())
        raise AssertionError(url)


def tr(n, isin, qty, price, ac="STOCK", sym=None, cur="EUR"):
    return fx.row("2026-03-02T10:00:00.000Z", "TRADING", "BUY", n, ac=ac, name="X", sym=sym or isin,
                  shares=str(qty), price=str(price), amount=f"{-qty * price:.2f}", fee="-1.00", cur=cur)


class PriceSourceTest(MonCase):
    def setUp(self):
        super().setUp()
        self.net = Net()
        self.clock = [datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)]
        eodhd.set_transport(self.net, lambda: self.clock[0])
        kraken_public.set_transport(self.net)
        self.addCleanup(eodhd.set_transport, None, None)
        self.addCleanup(kraken_public.set_transport, None)
        secrets.set_store(secrets.MemoryStore({secrets.target("eodhd-api-key"): KEY}))
        self.addCleanup(secrets.set_store, None)
        prices.set_source(lambda s, a, b: (_ for _ in ()).throw(ValueError("offline")), lambda s, a, b: [])
        self.aid = service.create_account("TR", "EUR", "DE")["id"]
        rows = [tr(1, SAP, 10, 100), tr(2, None, 0.1, 40000, "CRYPTO", "BTC"), tr(3, None, 1, 0.5, "CRYPTO", "DOGE"),
                tr(4, "DE000SYNWRT1", 100, 1.5, "DERIVATIVE"), tr(5, "US912828XX00", 1000, 0.98, "BOND")]
        service.import_file(self.aid, fx.to_csv(rows), "tr.csv", "auto", None, False)

    def call(self, name, query=None, body=NO_BODY, match=None):
        return asyncio.run(getattr(routes.V2Api(), name)(query or {}, body, match or {}))

    def pos(self):
        return {p["key"]: p for p in insights.portfolio(2026, self.aid)["accounts"][0]["positions"]}

    def eod_calls(self):
        return sum(1 for u in self.net.urls if "/api/eod/" in u)

    def test_search_parsing(self):
        c = eodhd.parse_search(SEARCH, SAP)
        self.assertEqual([x["listing"] for x in c], ["SAP.XETRA", "SAPA.F"])
        self.assertEqual(c[0], {"listing": "SAP.XETRA", "code": "SAP", "exchange": "XETRA", "name": "SAP SE",
                                "type": "Common Stock", "country": "Germany", "currency": "EUR", "is_primary": True,
                                "previous_close": 120.5, "previous_close_date": "2026-10-01"})
        self.assertEqual(c[1]["previous_close"], 120.1)

    def test_listing_pick_eod_cache_and_budget(self):
        st, p = self.call("sh_listings", {"isin": SAP})
        self.assertEqual((st, p["cached"], [c["listing"] for c in p["candidates"]]), (200, False, ["SAP.XETRA", "SAPA.F"]))
        self.assertEqual(p["budget"]["used_today"], 1)
        self.assertIn(f"https://eodhd.com/api/search/{SAP}?", self.net.urls[0])
        st, p = self.call("sh_listings", {"isin": SAP})
        self.assertEqual((p["cached"], p["budget"]["used_today"], len(self.net.urls)), (True, 1, 1))   # 0 calls
        # no automatic pick: searched, but not mapped → still unpriced
        self.assertEqual(self.pos()[SAP]["price_source"], "none")
        st, p = self.call("sh_symbol_map_put", body={"isin": SAP, "source": "eodhd", "listing": "SAP.US",
                                                     "confirm_listing": True})
        self.assertEqual((st, p["code"]), (409, "listing_refused"))
        # fix1023: confirm_listing is accepted and ignored (no longer required)
        st, p = self.call("sh_symbol_map_put", body={"isin": SAP, "source": "eodhd", "listing": "sap.xetra",
                                                     "reason": "my venue"})              # fix1023: no confirm flag
        self.assertEqual(st, 200, p)
        m = p["mapping"]
        self.assertEqual((m["confirmed_via"], m["confirmed_listing"]), ("dialog", False))      # truthful provenance
        self.assertEqual((m["price_source"], m["listing"], m["currency"], m["exchange"], m["validated_against"],
                          m["confirmed_us_listing"]), ("eodhd", "SAP.XETRA", "EUR", "XETRA", "eodhd_search", False))
        pos = self.pos()[SAP]                     # same currency (EUR cost, EUR listing): value + P&L
        self.assertEqual((pos["price_source"], pos["price"], pos["as_of"], pos["value"], pos["unrealised"]),
                         ("market", 121.0, "2026-10-01", 1210.0, 209.0))
        self.assertEqual((pos["price_detail"], pos["price_provider"], pos["market_symbol"]),
                         ("EODHD close of SAP.XETRA (EUR, your listing)", "eodhd", "SAP.XETRA"))
        self.assertEqual(self.eod_calls(), 1)
        for _ in range(3):                        # card polls + diagnostics: the day's cache, no further call
            self.pos()
            insights.summary()
        service.diagnose(self.aid)
        self.assertEqual(self.eod_calls(), 1)
        self.assertEqual(eodhd.budget()["used_today"], 2)
        eod_url = next(u for u in self.net.urls if "/api/eod/" in u)
        self.assertIn("https://eodhd.com/api/eod/SAP.XETRA?", eod_url)
        self.assertIn("from=2025-10-02", eod_url)
        # the next UTC day: one new call
        self.clock[0] += timedelta(days=1)
        self.pos()
        self.assertEqual(self.eod_calls(), 2)

    def test_budget_exhaustion(self):
        symbols.listings(SAP)
        symbols.put_listing(SAP, "SAP.XETRA", True, None)
        db.meta_set("eodhd/calls/2026-10-02", "20")
        n = sum(1 for u in self.net.urls if "eodhd.com" in u)
        pos = self.pos()[SAP]
        self.assertEqual(pos["note"], "not available (no price source): EODHD daily limit reached (20/20) — next "
                                      "refresh tomorrow")
        self.assertEqual(sum(1 for u in self.net.urls if "eodhd.com" in u), n)     # nothing was sent
        st, p = self.call("sh_price_sources")
        e = next(s for s in p["sources"] if s["id"] == "eodhd")
        self.assertEqual((e["budget"]["used_today"], e["budget"]["left_today"], e["budget"]["message"], e["key_present"]),
                         (20, 0, "EODHD daily limit reached (20/20) — next refresh tomorrow", True))
        st, p = self.call("sh_listings", {"isin": "US0378331005", "refresh": "true"})
        self.assertEqual((st, p["code"]), (409, "eodhd_limit"))
        self.assertEqual(p["error"], "EODHD daily limit reached (20/20) — next refresh tomorrow")
        self.clock[0] += timedelta(days=1)                       # resets with the UTC day
        self.assertEqual(self.pos()[SAP]["price_source"], "market")

    def test_key_never_in_a_response(self):
        self.net.fail = 401
        st, p = self.call("sh_listings", {"isin": SAP})
        self.assertEqual(st, 503)
        self.assertNotIn(KEY, json.dumps(p))
        self.assertIn("EODHD rejected the API key", p["error"])
        st, p = self.call("sh_price_sources")
        self.assertNotIn(KEY, json.dumps(p))
        secrets.set_store(secrets.MemoryStore({}))
        st, p = self.call("sh_listings", {"isin": SAP, "refresh": "true"})
        self.assertEqual((st, p["code"], p["error"]), (424, "key_missing", eodhd.MISSING_KEY_MESSAGE))
        self.assertEqual(eodhd.redact(f"https://eodhd.com/api/eod/X.US?api_token={KEY}&fmt=json", KEY),
                         "https://eodhd.com/api/eod/X.US?api_token=***&fmt=json")

    def test_kraken_eur_pairs_and_no_source_reasons(self):
        p = self.pos()
        btc = p["BTC"]
        self.assertEqual((btc["price_source"], btc["price"], btc["as_of"], btc["price_provider"], btc["symbol_source"]),
                         ("market", 52000.5, "2026-10-01", "kraken_public", "fixed_pair"))    # current frame dropped
        self.assertEqual(btc["price_detail"], "Kraken close of XBT/EUR (public market data)")
        self.assertEqual((btc["value"], btc["unrealised"]), (5200.05, 1199.05))
        self.assertIn("pair=XBTEUR&interval=1440", next(u for u in self.net.urls if "kraken" in u))
        self.assertIn("crypto DOGE — only BTC, ETH, SOL in EUR (Kraken public)", p["DOGE"]["note"])
        self.assertIn("derivative — issuer product without a price source; enter a valuation price",
                      p["DE000SYNWRT1"]["note"])
        self.assertIn("bond — no price source; enter a valuation price", p["US912828XX00"]["note"])
        self.assertEqual(kraken_public.pair_for("eth"), ("ETHEUR", "ETH/EUR"))
        with self.assertRaises(Exception):
            kraken_public.assert_public("https://api.kraken.com/0/private/Balance")
        with self.assertRaises(Exception):
            kraken_public.assert_public("https://futures.kraken.com/0/public/OHLC")
        n = len(self.net.urls)
        self.pos()
        self.assertEqual(len(self.net.urls), n)                 # 15-minute cache
        cov = service.diagnose(self.aid)["coverage"]
        self.assertIn("BTC", {s["key"] for s in cov["price_sources"]})

    def test_shadow_conversion_label(self):
        from strategy_platform.monitoring.shadow import shadow_portfolio
        orig = shadow_portfolio.compare
        shadow_portfolio.compare = lambda *a, **k: {"shadows": [{"label": "SPY", "end_value": 1.0},
                                                                {"label": "x", "unavailable": "no data"}],
                                                    "user": {}, "notice": "n"}
        self.addCleanup(setattr, shadow_portfolio, "compare", orig)
        r = service.shadows(self.aid, [{"type": "buy_and_hold", "symbol": "SPY"}])
        self.assertEqual(r["shadows"][0]["conversion"], "simulated in USD — converted at the FRED rate of each flow date")
        self.assertNotIn("conversion", r["shadows"][1])

    def mapped(self):
        symbols.listings(SAP)
        symbols.put_listing(SAP, "SAP.XETRA", True, None)

    def test_failed_fetch_is_cached_for_the_day_and_refresh_costs_one_call(self):
        self.mapped()
        self.net.fail_eod = 404
        notes = {self.pos()[SAP]["note"] for _ in range(5)}
        insights.summary()
        service.diagnose(self.aid)
        self.assertEqual(self.eod_calls(), 1)                    # 5 polls + summary + diagnostics: ONE call
        self.assertEqual(len(notes), 1)                          # the same text every time
        note = notes.pop()
        self.assertIn("EODHD SAP.XETRA: EODHD has no data for this listing (HTTP 404) — cached until tomorrow "
                      "(refresh costs 1 call)", note)
        st, p = self.call("sh_price_sources")
        e = next(x for x in p["sources"] if x["id"] == "eodhd")
        self.assertEqual([f["listing"] for f in e["failures_today"]], ["SAP.XETRA"])
        self.assertEqual(e["budget"]["used_today"], 2)           # search + the one EOD call
        self.net.fail_eod = None
        st, p = self.call("sh_eodhd_refresh", body={"listing": "SAP.XETRA"})
        self.assertEqual((st, p["ok"], p["closes"], p["cost"], p["budget"]["used_today"]),
                         (200, True, 2, "1 EODHD call", 3))
        self.assertEqual(self.pos()[SAP]["price_source"], "market")
        self.assertEqual(self.eod_calls(), 2)
        st, p = self.call("sh_eodhd_refresh", body={"listing": "NOPE.XETRA"})
        self.assertEqual(st, 404)
        with db._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'shadow_eodhd_refresh'").fetchone()[0]
        self.assertEqual(n, 1)
        self.clock[0] += timedelta(days=1)                       # a new UTC day: one new fetch
        self.net.fail_eod = 404
        self.pos()
        self.assertEqual(self.eod_calls(), 3)

    def test_retries(self):
        self.mapped()
        http_gate.set_gate("eodhd", http_gate.HttpGate("eodhd", sleep=lambda s: None))
        self.addCleanup(http_gate.set_gate, "eodhd", None)
        for code, calls in ((404, 1), (402, 1), (429, 1), (503, 2)):     # 4xx never retried, 5xx once
            self.net.fail_eod = code
            before = self.eod_calls()
            with self.assertRaises(eodhd.EodhdFetchFailed):
                eodhd.closes("SAP.XETRA", refresh=True)
            self.assertEqual(self.eod_calls() - before, calls, code)
        self.assertEqual(eodhd.budget()["used_today"], 1 + 1 + 1 + 1 + 2)

    def test_single_flight_with_threads(self):
        self.mapped()
        self.net.delay = 0.2
        out, errs = [], []

        def read():
            try:
                out.append(len(eodhd.closes("SAP.XETRA")))
            except Exception as ex:  # noqa: BLE001
                errs.append(ex)
        th = [threading.Thread(target=read) for _ in range(4)]
        for t in th:
            t.start()
        for t in th:
            t.join()
        self.assertEqual((errs, out, self.eod_calls()), ([], [2, 2, 2, 2], 1))
        searches = []
        th = [threading.Thread(target=lambda: searches.append(eodhd.search_isin("US0378331005")["cached"]))
              for _ in range(3)]
        for t in th:
            t.start()
        for t in th:
            t.join()
        self.assertEqual((sorted(searches), sum(1 for u in self.net.urls if "/api/search/US0378331005" in u)),
                         ([False, True, True], 1))

    def test_report_price_notes(self):
        from strategy_platform.monitoring.shadow import report
        self.mapped()
        h = report.build(self.aid, "html")["content"]
        self.assertIn("Prices: EODHD (personal use, no redistribution).", h)
        self.assertIn("Prices: Kraken public market data (crypto in EUR).", h)
        empty = service.create_account("E", "EUR", "DE")["id"]
        self.assertEqual(json.loads(report.build(empty, "json")["content"])["price_notes"], [])

    def test_secret_name_and_routes(self):
        self.assertEqual(secrets.SECRETS["eodhd-api-key"], "EODHD API key")
        paths = {(m, p) for m, p, _n, _b in routes.ROUTES}
        self.assertTrue({("GET", "/v2/shadow/price-sources"), ("GET", "/v2/shadow/listings")} <= paths)
        st, p = self.call("sh_listings", {"isin": "nope"})
        self.assertEqual((st, set(p["errors"])), (400, {"isin"}))
        with self.assertRaises(TradingError):
            symbols.put_listing(SAP, "SAP.XETRA", True, None)    # no search yet


if __name__ == "__main__":
    unittest.main()
