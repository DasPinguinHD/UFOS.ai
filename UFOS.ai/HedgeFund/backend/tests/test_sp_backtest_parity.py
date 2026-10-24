"""Stage 3c acceptance (SPEC §8.3): PARITY — the same engine run on the
in-memory store and on the normal fund.store (temp newsroom DB) gives the
same fills, cash ledger and positions; crypto perp short funding over a
multi-day run; mixed crypto/NYSE alignment; a seam-crossing range uses the
factor-scaled pre-seam (Tiingo) rows of data/seam.py. Offline, fakes only.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_backtest_parity -v
"""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

try:
    from test_sp_backtest_support import DAYS, FakeLoader, series, spec
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_backtest_support import DAYS, FakeLoader, series, spec

from fund import store as fund_store
from strategy_platform.execution.paper_broker import PaperBroker
from newsroom import db as newsroom_db
from strategy_platform.backtest import engine
from strategy_platform.backtest.memstore import MemStore

START = "2024-02-15"
I0 = DAYS.index(date(2024, 2, 15))
LS = dict(mode="long_short", selection={"type": "threshold", "long_above": 100, "short_below": 50})
UTC_DAYS = [date(2024, 1, 1) + timedelta(days=i) for i in range(91)]


def crypto_series(base, days=UTC_DAYS, vol=1e5):
    out = {}
    for i, d in enumerate(days):
        c = base * (1 + 0.002 * ((i * 5) % 7 - 3))
        out[d] = (round(c * 0.999, 2), round(c, 2), vol)
    return out


class FundStoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.assertTrue(newsroom_db.init(os.path.join(self._tmp.name, "n.db"), legacy=""))

    def tearDown(self):
        newsroom_db.close()
        self._tmp.cleanup()

    def assert_parity(self, mem, ref):
        self.assertEqual(len(mem.orders), len(ref.orders))
        for a, b in zip(mem.orders, ref.orders):
            self.assertEqual(set(a), set(b))
            for k in a:
                if isinstance(a[k], float):
                    self.assertAlmostEqual(a[k], b[k], places=6, msg=k)
                else:
                    self.assertEqual(a[k], b[k], k)
        self.assertEqual([(l["id"], l["ts"], l["kind"], l["symbol"], l["note"]) for l in mem.ledger],
                         [(l["id"], l["ts"], l["kind"], l["symbol"], l["note"]) for l in ref.ledger])
        for a, b in zip(mem.ledger, ref.ledger):
            self.assertAlmostEqual(a["amount"], b["amount"], places=6)
        self.assertEqual(mem.positions, ref.positions)
        for a, b in zip(mem.equity, ref.equity):
            self.assertAlmostEqual(a, b, places=6)


class ParityTest(FundStoreCase):
    def test_equity_long_short_with_dividends_and_borrow(self):
        mk = lambda: FakeLoader({"SPY": series(150), "TLT": series(20), "GLD": series(105, drift=-0.002)},
                                dividends={"SPY": [(DAYS[I0 + 10], 1.5)], "TLT": [(DAYS[I0 + 12], 0.07)]})
        s = spec(["SPY", "TLT", "GLD"], **LS)
        mem = engine.run(s, data=mk(), start=START)
        ref = engine.run(s, data=mk(), start=START, _store_factory=lambda: fund_store)
        self.assertGreater(len(mem.fills), 20)
        kinds = {l["kind"] for l in mem.ledger}
        self.assertTrue({"deposit", "fill", "fee", "borrow_fee", "dividend", "dividend_short"} <= kinds)
        self.assert_parity(mem, ref)
        self.assertEqual(len(fund_store.list_orders(limit=1000)), len(mem.orders))   # the normal store holds the run

    def test_crypto_perp_short_parity(self):
        mk = lambda: FakeLoader(crypto={"BTC-USD": crypto_series(40000.0)})
        s = spec(["BTC-USD"], mode="short_only", gross=0.5, rebalance="weekly")
        mem = engine.run(s, data=mk(), start="2024-02-01", benchmark="BTC-USD")
        ref = engine.run(s, data=mk(), start="2024-02-01", benchmark="BTC-USD", _store_factory=lambda: fund_store)
        self.assertTrue([l for l in mem.ledger if l["kind"] == "funding"])
        self.assert_parity(mem, ref)

    def test_memstore_matches_fund_store_on_direct_broker_calls(self):
        """The same hand-driven call sequence on both stores (independent of the engine)."""
        from strategy_platform.core import params as P
        from strategy_platform.core.types import Quote
        params = P.merge(None, {"fees_bps": 10.0})
        prices = {"SPY": 500.0, "XLF": 40.0}
        out = []
        for st in (MemStore(params), fund_store):
            now = {"t": datetime(2024, 3, 4, 14, 40, tzinfo=timezone.utc)}
            b = PaperBroker(st, lambda s: Quote(symbol=s, price=prices[s], ts=now["t"].isoformat()),
                            clock=lambda: now["t"], params_fn=lambda: params,
                            dividends_fn=lambda s, since: [(date(2024, 3, 6), 1.0)] if s == "SPY" else [])
            b.target("SPY", 0.5, "autopilot")
            b.target("XLF", -0.2, "autopilot")
            for day in (5, 6, 7):
                now["t"] = datetime(2024, 3, day, 14, 40, tzinfo=timezone.utc)
                b.daily_accruals()
                b.target("SPY", 0.3, "autopilot")
                b.check_margin()
                b.snapshot_equity()
            out.append((engine._all_rows(st, "orders"), engine._all_rows(st, "ledger"),
                        st.get_positions(include_flat=True)))
        (o1, l1, p1), (o2, l2, p2) = out
        self.assertEqual(o1, o2)
        self.assertEqual([(r["kind"], r["note"]) for r in l1], [(r["kind"], r["note"]) for r in l2])
        self.assertIn("dividend", [r["kind"] for r in l1])
        for a, b in zip(l1, l2):
            self.assertAlmostEqual(a["amount"], b["amount"], places=9)
        self.assertEqual(p1, p2)


class CryptoTest(unittest.TestCase):
    def test_perp_short_funding_over_many_days(self):
        data = crypto_series(40000.0)
        s = spec(["BTC-USD"], mode="short_only", gross=0.5, rebalance="monthly")
        r = engine.run(s, data=FakeLoader(crypto={"BTC-USD": data}), start="2024-02-01", end="2024-02-20",
                       benchmark="BTC-USD")
        self.assertEqual(r.range["calendar"], "utc")
        short = r.fills[0]
        self.assertEqual((short["side"], short["ts"]), ("short", "2024-02-02T00:00:00+00:00"))
        qty = short["qty"]
        funding = [l for l in r.ledger if l["kind"] == "funding"]
        # windows after the short opened (02-02 00:00) through the last close phase (02-20 23:59)
        self.assertEqual(len(funding), 3 * 19 - 1)
        self.assertEqual(len({l["note"][:16] for l in funding}), len(funding))     # one row per window
        for l in funding:
            self.assertGreater(l["amount"], 0)                                  # positive rate: shorts receive
            day = date.fromisoformat(l["ts"][:10])
            mark = data[day][0] if l["ts"].endswith("T00:00:00+00:00") else data[day][1]
            self.assertAlmostEqual(l["amount"], qty * mark * 0.01 / 100, places=6)
        self.assertFalse([l for l in r.ledger if l["kind"] in ("borrow_fee", "dividend_short")])

    def test_crypto_has_no_participation_limit(self):
        tiny = {d: (o, c, 0.0001) for d, (o, c, _v) in crypto_series(40000.0).items()}   # ~$4 daily volume
        loader = FakeLoader({"SPY": series(150)}, volumes={"SPY": {d: 1_000_000.0 for d in DAYS}},
                            crypto={"BTC-USD": tiny})
        r = engine.run(spec(["SPY", "BTC-USD"]), data=loader, start=START)
        btc = [f for f in r.fills if f["symbol"] == "BTC-USD"][0]
        self.assertGreater(btc["notional"], 40_000)                           # full ~50 % target, uncut
        cuts = [w for w in r.warnings if w["code"] == "participation_cut"]
        self.assertEqual([w["symbol"] for w in cuts], ["SPY"])                # equities still limited
        info = [w for w in r.warnings if w["code"] == "crypto_no_participation_limit"]
        self.assertEqual(len(info), 1)
        self.assertIn("user decision 2026-10-03", info[0]["message"])
        self.assertFalse([w for w in r.warnings if w["code"] == "missing_volume"])
        r2 = engine.run(spec(["SPY"]), data=FakeLoader({"SPY": series(150)}), start=START)
        self.assertFalse([w for w in r2.warnings if w["code"] == "crypto_no_participation_limit"])

    def test_mixed_universe_uses_pit_crypto_quotes_on_nyse_sessions(self):
        loader = FakeLoader({"SPY": series(150)}, crypto={"BTC-USD": crypto_series(40000.0)})
        r = engine.run(spec(["SPY", "BTC-USD"]), data=loader, start=START)
        self.assertEqual(r.range["calendar"], "nyse")
        btc = [f for f in r.fills if f["symbol"] == "BTC-USD"][0]
        d1 = DAYS[I0 + 1]
        self.assertEqual(btc["ts"], f"{d1.isoformat()}T14:36:00+00:00")
        # newest price known at 14:36 UTC = the open of that UTC day's bar (00:00 UTC)
        self.assertAlmostEqual(btc["fill_price"], loader.crypto["BTC-USD"][d1][0] * (1 + 20.0 / 2 / 10000))


class SeamCrossingTest(unittest.TestCase):
    """data/seam.py with the stage-3b World fake (2015 weekdays as Tiingo
    rows; the World skips no 2015 holidays, so holiday rows are ignored by the
    NYSE-session panel and the engine)."""

    def setUp(self):
        try:
            import test_sp_data_seam as ts
            from test_sp_data_support import TempDb
        except ImportError:  # pragma: no cover
            from tests import test_sp_data_seam as ts
            from tests.test_sp_data_support import TempDb
        from strategy_platform import secrets
        self.ts = ts
        self._db = TempDb()
        self._db.__enter__()
        self.addCleanup(self._db.__exit__, None, None, None)
        secrets.set_store(secrets.MemoryStore({secrets.target("tiingo-api-key"): ts.KEY}))
        self.addCleanup(secrets.set_store, None)

    def test_pre_seam_rows_are_factor_scaled(self):
        from strategy_platform.data import seam as seam_mod
        from strategy_platform.data.cache import BarCache
        ts = self.ts
        world = ts.World()
        now = datetime(2016, 6, 1, 21, 0, tzinfo=timezone.utc)
        cache = BarCache(fetch=world.fetch, configured_source=ts.src_for, clock=lambda: now)
        svc = seam_mod.Seam(cache=cache, fetch=world.fetch, clock=lambda: now)

        class Loader(engine.DataLoader):
            def volume(self, symbol, start, end=None, *, progress=None):
                days = [d for d in ts.PRE + ts.POST if d >= start and (end is None or d <= end)]
                rows = [{"date": d.isoformat(), "volume": 1e9, "close_split": 1.0, "dollar_volume_split": 1e12,
                         "known_at": "x", "source": "tiingo"} for d in days]
                return rows, {"source": "tiingo", "rows": len(rows), "last": rows[-1]["date"], "warnings": []}

        r = engine.run(spec(["SPY"]), data=Loader(seam=svc), start="2015-09-15", end="2016-03-29")
        self.assertEqual(r.dates[0], "2015-09-15")
        snap = r.data["SPY"]["split"]
        self.assertEqual(snap["sources"], ["tiingo", "alpaca_sip"])
        self.assertTrue(snap["seam"]["ok"])
        self.assertAlmostEqual(snap["seam"]["factor"], 0.5, places=12)
        first = r.fills[0]
        d1 = date(2015, 9, 16)
        self.assertTrue(first["ts"].startswith(d1.isoformat()))
        p = ts.true_price(world.index[d1]) / 2                                # Tiingo raw x factor
        self.assertAlmostEqual(first["fill_price"], p * (1 + 5.0 / 2 / 10000), places=9)
        divs = {l["note"][3:13]: l for l in r.ledger if l["kind"] == "dividend"}
        held = [x for x in r.positions if x["date"] < "2015-10-01"][-1]["positions"]["SPY"]
        self.assertAlmostEqual(divs["2015-10-01"]["amount"], held * 0.25)       # 0.5 raw x factor 0.5
        held = [x for x in r.positions if x["date"] < "2016-03-01"][-1]["positions"]["SPY"]
        self.assertAlmostEqual(divs["2016-03-01"]["amount"], held * 0.15)       # 0.3 ÷ later 2:1 split


if __name__ == "__main__":
    unittest.main()
