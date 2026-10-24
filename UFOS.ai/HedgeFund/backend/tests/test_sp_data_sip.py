"""User decision 2026-10-06: historical daily bars from Alpaca SIP (own
source label ``alpaca_sip``, request end <= now - 15 min, IEX untouched for
the live engine), equity volume = SIP from the seam + Tiingo before it with
a volume seam check, the Tiingo key only for pre-seam loads, the SIP check
in the connection test, the ``many_late_starts`` guard, and the persistent
Tiingo gate counters (kept from 2026-10-05). Fakes, frozen clocks, temp db.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_data_sip -v
"""
import unittest
from datetime import date, datetime, timedelta, timezone

try:
    from test_sp_data_support import KEYS, FakeAPIError, FakeTime, Script, TempDb, fake_gate, fake_sdk, utc
    from test_sp_data_seam import KEY, PRE, World, src_for
    import test_sp_backtest_support as sup
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import KEYS, FakeAPIError, FakeTime, Script, TempDb, fake_gate, fake_sdk, utc
    from tests.test_sp_data_seam import KEY, PRE, World, src_for
    import tests.test_sp_backtest_support as sup

from strategy_platform import secrets
from strategy_platform.backtest import engine
from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing
from strategy_platform.data import http_gate, licences, registry, seam as seam_mod
from strategy_platform.data.cache import BarCache
from strategy_platform.data.providers import alpaca as A
from strategy_platform.storage.db import StorageUnavailable

NOW = utc(2026, 10, 6, 14, 0)


def sip_answer(day="2016-01-04", close=201.0, volume=2.2e8):
    return {"SPY": [{"t": f"{day}T05:00:00Z", "o": close, "h": close, "l": close, "c": close, "v": volume,
                     "n": 10, "vw": close}]}


class SipProviderTest(unittest.TestCase):
    def setUp(self):
        secrets.set_store(secrets.MemoryStore(KEYS))
        self.addCleanup(secrets.set_store, None)
        self.script = Script()
        self.gate, _ = fake_gate()
        self.p = A.AlpacaSipProvider(gate=self.gate, sdk=fake_sdk(self.script), clock=lambda: NOW)

    def test_sip_feed_label_and_15_minute_rule(self):
        self.script.set("get_stock_bars", sip_answer())
        rows = self.p.get_bars("SPY", "1Day", utc(2016, 1, 4), NOW, adjustment="split")
        req = self.script.calls[-1][1]
        self.assertEqual((req.feed, req.adjustment), ("sip", "split"))
        self.assertLessEqual(req.end, NOW - timedelta(minutes=15))           # never newer than now - 15 min
        self.assertEqual((rows[0]["source"], rows[0]["volume"]), ("alpaca_sip", 2.2e8))
        self.assertEqual(self.p.get_bars("SPY", "1Day", NOW - timedelta(minutes=5), NOW), [])
        with self.assertRaises(ValueError):
            self.p.get_bars("BTC-USD", "1Day", utc(2024, 1, 1), NOW)
        self.assertEqual(self.p.data_types, ("equity_bars_history",))

    def test_live_provider_stays_iex(self):
        script = Script()
        script.set("get_stock_bars", sip_answer())
        p = A.AlpacaProvider(gate=self.gate, sdk=fake_sdk(script), clock=lambda: NOW)
        rows = p.get_bars("SPY", "1Day", utc(2016, 1, 4), NOW)
        self.assertEqual((script.calls[-1][1].feed, rows[0]["source"]), ("iex", "alpaca"))

    def test_sip_refusal_is_a_clear_error_never_iex(self):
        self.script.set("get_stock_bars", FakeAPIError(403, "subscription does not permit querying recent SIP data"))
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.p.get_bars("SPY", "1Day", utc(2016, 1, 4), NOW)
        self.assertIn("15 minutes", cm.exception.user_message)
        self.assertIn("no IEX data is used instead", cm.exception.user_message)
        self.assertEqual(len(self.script.calls), 1)


class SipRequestEndTest(unittest.TestCase):
    """Fix 2026-10-06: a 1Day SIP window must end at the close of the last session that closed
    >= 15 min ago — never inside today's session (its daily bar holds the last 15 minutes)."""

    def end(self, *ny):
        from strategy_platform.core import calendars
        now = datetime(*ny, tzinfo=calendars.NY).astimezone(timezone.utc)
        return A.sip_request_end("1Day", now)

    def test_daily_clamp(self):
        mon_close = utc(2026, 10, 5, 20, 0)                                  # Mon 16:00 New York (EDT)
        self.assertEqual(self.end(2026, 10, 6, 15, 9), mon_close)           # during Tuesday's session
        self.assertEqual(self.end(2026, 10, 6, 8, 0), mon_close)            # before the open
        self.assertEqual(self.end(2026, 10, 6, 16, 10), mon_close)          # 10 min after the close
        self.assertEqual(self.end(2026, 10, 6, 16, 20), utc(2026, 10, 6, 20, 0))   # 20 min after the close
        self.assertEqual(self.end(2026, 10, 10, 12, 0), utc(2026, 10, 9, 20, 0))   # Saturday -> Friday
        self.assertEqual(self.end(2026, 11, 26, 18, 0), utc(2026, 11, 25, 21, 0))  # Thanksgiving -> Wed (EST)
        self.assertEqual(self.end(2026, 11, 27, 13, 20), utc(2026, 11, 27, 18, 0))  # early close 13:00

    def test_intraday_keeps_now_minus_15_min(self):
        self.assertEqual(A.sip_request_end("1Hour", NOW), NOW - timedelta(minutes=15))

    def test_provider_never_asks_for_todays_session(self):
        secrets.set_store(secrets.MemoryStore(KEYS))
        self.addCleanup(secrets.set_store, None)
        from strategy_platform.core import calendars
        during = datetime(2026, 10, 6, 15, 9, tzinfo=calendars.NY).astimezone(timezone.utc)
        script = Script()
        script.set("get_stock_bars", sip_answer("2026-10-05"))
        gate, _ = fake_gate()
        p = A.AlpacaSipProvider(gate=gate, sdk=fake_sdk(script), clock=lambda: during)
        p.get_bars("SPY", "1Day", utc(2026, 9, 1), during)
        self.assertEqual(script.calls[-1][1].end, utc(2026, 10, 5, 20, 0))
        self.assertEqual(p.get_bars("SPY", "1Day", utc(2026, 10, 6, 4, 0), during), [])   # only today: no request
        self.assertEqual(len(script.calls), 1)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self._db = TempDb()
        self.db = self._db.__enter__()
        self.addCleanup(self._db.__exit__, None, None, None)

    def test_sources_and_licences(self):
        self.assertEqual(registry.DEFAULT_SOURCES["equity_bars_history"], "alpaca_sip")
        self.assertEqual(registry.DEFAULT_SOURCES["equity_volume"], "alpaca_sip")
        self.assertEqual(registry.DEFAULT_SOURCES["equity_bars"], "alpaca")            # live engine: IEX
        self.assertEqual(licences.licence_for("alpaca_sip")["licence"], "personal_use")
        self.assertIn("15 minutes", licences.licence_for("alpaca_sip")["note"])

    def test_connection_test_has_the_sip_check(self):
        class Live:
            data_types = A.DATA_TYPES

            def get_bars(self, symbol, tf, start, end):
                return [{"ts": "2026-10-05T04:00:00+00:00", "close": 1.0}]

            def latest_quote(self, symbol):
                return {"bid": 1, "ask": 2, "ts": "2026-10-06T14:00:00Z"}

        class Sip:
            data_types = ("equity_bars_history",)
            answer = None

            def get_bars(self, symbol, tf, start, end):
                if isinstance(Sip.answer, BaseException):
                    raise Sip.answer
                return Sip.answer

        reg = registry.Registry(store=self.db, providers={"alpaca": Live, "alpaca_sip": Sip}, clock=lambda: NOW)
        Sip.answer = [{"ts": "2016-01-04T05:00:00+00:00", "close": 201.0, "volume": 2.2e8}]
        out = reg.test_source("alpaca")
        self.assertEqual(out["checks"][-1]["name"], "SIP historical daily bars (SPY, 2016-01-04 and newest)")
        self.assertTrue(out["ok"], out)
        self.assertIn("2016-01-04", out["checks"][-1]["message"])
        self.assertEqual(self.db.data_source_get("equity_bars_history")["status"], "ok")
        Sip.answer = DataSourceUnavailable("alpaca_sip", "equity_bars_history", A.SIP_PLAN_MESSAGE)
        out = reg.test_source("alpaca")
        self.assertFalse(out["ok"])
        self.assertIn("15 minutes", out["checks"][-1]["message"])
        self.assertTrue(all(c["ok"] for c in out["checks"][:-1]))

    def test_cancellation_leaves_the_source_status_alone(self):
        class Prov:
            data_types = ("equity_bars_pre2016",)

            def daily_prices(self, *a, **k):
                raise engine.BacktestCancelled()
        reg = registry.Registry(store=self.db, providers={"tiingo": Prov})
        with self.assertRaises(engine.BacktestCancelled):
            reg.fetch("equity_bars_pre2016", "daily_prices", "SPY", date(2010, 1, 4), date(2016, 3, 30))
        self.assertIsNone(self.db.data_source_get("equity_bars_pre2016")["status"])


class CacheTest(unittest.TestCase):
    def test_iex_and_sip_rows_never_mix(self):
        with TempDb() as db:
            world = World()
            now = utc(2016, 6, 1, 21, 0)
            cache = BarCache(fetch=world.fetch, configured_source=src_for, clock=lambda: now)
            iex = cache.get_bars("SPY", "1Day", utc(2016, 1, 4), utc(2016, 2, 1))
            sip = cache.get_bars("SPY", "1Day", utc(2016, 1, 4), utc(2016, 2, 1), data_type="equity_bars_history")
            self.assertEqual({r["source"] for r in iex}, {"alpaca"})
            self.assertEqual({r["source"] for r in sip}, {"alpaca_sip"})
            self.assertEqual(len(db.query_bars("SPY", "1Day", None, None, "alpaca")), len(iex))
            self.assertEqual(len(db.query_bars("SPY", "1Day", None, None, "alpaca_sip")), len(sip))
            with self.assertRaises(ValueError):
                cache.get_bars("BTC-USD", "1Day", utc(2016, 1, 4), utc(2016, 2, 1), data_type="equity_bars_history")


class VolumeTest(unittest.TestCase):
    def setUp(self):
        self._db = TempDb()
        self.db = self._db.__enter__()
        self.addCleanup(self._db.__exit__, None, None, None)
        secrets.set_store(secrets.MemoryStore({secrets.target("tiingo-api-key"): KEY}))
        self.addCleanup(secrets.set_store, None)
        self.now = utc(2016, 6, 1, 21, 0)

    def svc(self, world):
        cache = BarCache(fetch=world.fetch, configured_source=src_for, clock=lambda: self.now)
        return seam_mod.Seam(cache=cache, fetch=world.fetch, clock=lambda: self.now)

    def test_sip_after_seam_tiingo_before_when_the_volume_check_passes(self):
        rows, info = self.svc(World()).volume("SPY", date(2015, 6, 1), date(2016, 5, 31))
        pre = [r for r in rows if r["date"] < "2016-01-04"]
        post = [r for r in rows if r["date"] >= "2016-01-04"]
        self.assertEqual(len(pre), len(PRE))
        self.assertEqual({r["source"] for r in pre}, {"tiingo"})
        self.assertEqual({r["source"] for r in post}, {"alpaca_sip"})
        self.assertAlmostEqual(pre[-1]["volume"], 2_000_000.0)                  # 1e6 split-basis shares ÷ 0.5
        self.assertAlmostEqual(post[0]["volume"], 2_000_000.0)                  # continuous across the seam
        for r in rows:
            self.assertAlmostEqual(r["dollar_volume_split"], r["volume"] * r["close_split"])
        self.assertTrue(info["seam"]["stats"]["volume"]["ok"])
        self.assertEqual(info["sources"], ["tiingo", "alpaca_sip"])

    def test_volume_mismatch_drops_only_pre_seam_volume(self):
        world = World(alpaca_volume=30_000.0)                                   # e.g. an IEX-like volume level
        svc = self.svc(world)
        rows, info = svc.volume("SPY", date(2015, 6, 1), date(2016, 5, 31))
        self.assertTrue(rows and all(r["source"] == "alpaca_sip" for r in rows))
        w = [x for x in info["warnings"] if x["code"] == "seam_volume_mismatch"]
        self.assertEqual(len(w), 1)
        self.assertIn("median volume ratio", w[0]["message"])
        prices, pinfo = svc.history("SPY", "split", date(2015, 6, 1), date(2016, 5, 31))
        self.assertTrue(any(r["source"] == "tiingo" for r in prices))           # prices still pass their check

    def test_no_tiingo_key_needed_from_the_seam_on(self):
        secrets.set_store(secrets.MemoryStore({}))
        world = World()
        rows, info = self.svc(world).volume("SPY", date(2016, 1, 4), date(2016, 5, 31))
        self.assertEqual(info["sources"], ["alpaca_sip"])
        self.assertFalse(any(c[0] == "equity_bars_pre2016" for c in world.calls))
        with self.assertRaises(SecretMissing):
            self.svc(world).volume("SPY", date(2015, 6, 1), date(2016, 5, 31))
        with self.assertRaises(ValueError):
            self.svc(world).volume("BTC-USD", date(2016, 1, 4))

    def test_short_history_for_a_late_sip_start(self):
        class Young(World):
            def alpaca_bars(self, symbol, start, end, adjustment, source="alpaca_sip"):
                return [r for r in super().alpaca_bars(symbol, start, end, adjustment, source)
                        if r["ts"] >= "2016-03-01"]
        _rows, info = self.svc(Young()).history("SPY", "all", date(2016, 1, 4), date(2016, 5, 31))
        self.assertEqual([w["code"] for w in info["warnings"]], ["short_history"])


class LateStartTest(unittest.TestCase):
    def test_many_late_starts_warning(self):
        late = {d: oc for d, oc in sup.series(50).items() if d >= sup.DAYS[60]}
        loader = sup.FakeLoader({"AAA": late, "BBB": dict(late), "CCC": sup.series(30), "SPY": sup.series(400)})
        old = engine.LATE_START_DAYS
        engine.LATE_START_DAYS = 30
        self.addCleanup(setattr, engine, "LATE_START_DAYS", old)
        r = engine.run(sup.spec(["AAA", "BBB", "CCC"]), data=loader, start=sup.DAYS[50].isoformat())
        w = [x for x in r.warnings if x["code"] == "many_late_starts"]
        self.assertEqual(len(w), 1)
        self.assertEqual(w[0]["symbols"], ["AAA", "BBB"])
        engine.LATE_START_DAYS = 365
        r = engine.run(sup.spec(["AAA", "BBB", "CCC"]), data=loader, start=sup.DAYS[50].isoformat())
        self.assertFalse(any(x["code"] == "many_late_starts" for x in r.warnings))


class GatePersistenceTest(unittest.TestCase):
    def setUp(self):
        self._db = TempDb()
        self.db = self._db.__enter__()
        self.addCleanup(self._db.__exit__, None, None, None)

    def gate(self, ft, wall):
        return http_gate.HttpGate("tiingo", windows=((2, 3600.0), (5, 86400.0)),
                                  persist=http_gate.PersistedStamps("http_gate/tiingo/requests"),
                                  monotonic=ft.monotonic, sleep=ft.sleep, wall=lambda: wall[0] + ft.t)

    def test_restart_keeps_the_windows(self):
        wall = [1_700_000_000.0]
        ft = FakeTime()
        g = self.gate(ft, wall)
        for _ in range(2):
            g.call(lambda: 1, data_type="equity_bars_pre2016", classify=lambda e, d: e)
        self.assertEqual(len(self.db.meta_keys("http_gate/tiingo/")), 1)
        ft2 = FakeTime()                                   # "restart": new monotonic clock, 100 s later
        ft2.t = 5.0
        wall2 = [wall[0] + ft.t + 100.0 - ft2.t]
        g2 = self.gate(ft2, wall2)
        self.assertEqual(g2.limiter.remaining()[0][2], 0)
        events = []
        g2.call(lambda: 1, data_type="equity_bars_pre2016", classify=lambda e, d: e, progress=events.append)
        self.assertGreaterEqual(ft2.t - 5.0, 3600 - 100 - 2)
        self.assertTrue(events and "0 requests left this hour" in events[0]["message"])
        self.assertEqual(g2.limiter.remaining()[1][2], 2)

    def test_unwritable_counters_stop_the_request(self):
        g = self.gate(FakeTime(), [1_700_000_000.0])
        self.db.close()
        sent = []
        with self.assertRaises(StorageUnavailable):
            g.call(lambda: sent.append(1), data_type="equity_bars_pre2016", classify=lambda e, d: e)
        self.assertEqual(sent, [])

    def test_process_gate_is_persisted(self):
        http_gate.set_gate("tiingo", None)
        self.addCleanup(http_gate.set_gate, "tiingo", None)
        self.assertIsInstance(http_gate.gate_for("tiingo").limiter._persist, http_gate.PersistedStamps)


if __name__ == "__main__":
    unittest.main()
