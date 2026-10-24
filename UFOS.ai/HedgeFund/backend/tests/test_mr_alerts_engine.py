"""Market Research MR7 — the alert engine (SPEC §13 "MR7 backend notes"): crossing up / down incl. gaps
and exact touches, no trigger without a previous price (start, reconnect, restart), late trades, direction
filters, cooldown + de-dup, restart persistence (no repeated trigger), alert lines (moved line → new level,
deleted line → switched off with a reason), day move vs the previous close (once per day and side, missing
previous close → waiting with the reason), news alerts (once per id), the WebSocket ``alert`` message + log,
hub demand registration / release, the 30-symbol limit message, market-closed status, crash isolation.
Fake Alpaca transport / fakes only; no network.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_alerts_engine -v
"""
import asyncio
import logging
import unittest
from datetime import datetime, timedelta, timezone

try:
    from mr_data_testkit import TempStore
    from mr_alerts_testkit import NOW, AnyData, Clock, alert_line, engine, run, tr
    from mr_hub_testkit import FAST, FakeAlpaca, FakeData as HubData, Sink, keys, running, trade, wait_for
except ImportError:  # pragma: no cover
    from tests.mr_data_testkit import TempStore
    from tests.mr_alerts_testkit import NOW, AnyData, Clock, alert_line, engine, run, tr
    from tests.mr_hub_testkit import FAST, FakeAlpaca, FakeData as HubData, Sink, keys, running, trade, wait_for

from market_research import alerts as al
from market_research import drawings as dr
from market_research import limits, store
from market_research.alerts import AlertEngine
from market_research.hub.hub import MarketDataHub
from strategy_platform.core.errors import DataSourceUnavailable


def setUpModule():
    logging.getLogger("market_research").setLevel(logging.CRITICAL + 1)


def tearDownModule():
    logging.getLogger("market_research").setLevel(logging.NOTSET)


def new(kind="price_cross", symbol="AAPL", **params):
    defaults = {"price_cross": {"level": 100.0, "direction": "up"}, "day_move": {"pct": 5, "direction": "any"},
                "news": {}}
    p = params or defaults.get(kind, {})
    return al.create(AnyData(), {"symbol": symbol, "kind": kind, "params": p, "cooldown_seconds": 60}, now=NOW)


class Case(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()
        self.clock = Clock()
        self.hub = MarketDataHub(now=self.clock)
        self.out = Sink("alerts")
        self.hub.register(self.out)

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def eng(self, **kw):
        return engine(self.clock, self.hub, **kw)

    def feed(self, eng, *trades):
        for sym, price, sec in trades:
            eng.on_trade("stocks", tr(sym, price, sec))
        run(eng.sync())

    def fired(self):
        return self.out.of("alert")


class CrossingTest(Case):
    def test_up_needs_a_previous_price_and_fires_on_gap_and_touch(self):
        a = new()
        e = self.eng()
        run(e.sync())
        self.feed(e, ("AAPL", 101.0, 1))                 # start above the level: baseline only
        self.assertEqual(self.fired(), [])
        self.feed(e, ("AAPL", 99.0, 2), ("AAPL", 100.0, 3))   # exact touch from below = crossed
        self.assertEqual(len(self.fired()), 1)
        m = self.fired()[0]
        self.assertEqual(set(m), {"type", "alert_id", "symbol", "kind", "title", "message", "ts", "price", "level",
                                  "log_id", "logged"})
        self.assertEqual((m["alert_id"], m["symbol"], m["title"], m["price"], m["level"], m["logged"]),
                         (a["id"], "AAPL", "Research alert · AAPL", 100.0, 100.0, True))
        self.assertEqual(m["message"], "AAPL crossed above 100.00 — last trade 100.00.")
        log = store.alert_log_list(5)
        self.assertEqual((log[0]["id"], log[0]["price"], log[0]["detail"]["side"], log[0]["detail"]["previous_price"]),
                         (m["log_id"], 100.0, "up", 99.0))
        self.assertIsNotNone(store.alert_get(a["id"])["last_triggered_at"])

    def test_gap_down_and_direction_filter(self):
        up = new(level=100.0, direction="up")
        down = new(level=100.0, direction="down")
        e = self.eng()
        run(e.sync())
        self.feed(e, ("AAPL", 105.0, 1), ("AAPL", 95.0, 2))   # gap across the level between two trades
        self.assertEqual([m["alert_id"] for m in self.fired()], [down["id"]])
        self.assertIn("crossed below 100.00", self.fired()[0]["message"])
        self.feed(e, ("AAPL", 100.5, 3))
        self.assertEqual([m["alert_id"] for m in self.fired()], [down["id"], up["id"]])

    def test_late_trade_ignored_and_reconnect_resets_the_baseline(self):
        new()
        e = self.eng()
        run(e.sync())
        self.feed(e, ("AAPL", 99.0, 10), ("AAPL", 101.0, 5))  # older trade: ignored
        self.assertEqual(self.fired(), [])
        sub = next(iter(e.subs.values()))
        sub.deliver({"type": "symbol_status", "symbol": "AAPL", "live": False, "reason": "waiting"})
        self.feed(e, ("AAPL", 101.0, 20))                 # first trade after the gap: baseline
        self.assertEqual(self.fired(), [])

    def test_cooldown_and_dedupe_then_again(self):
        new()
        e = self.eng()
        run(e.sync())
        self.feed(e, ("AAPL", 99.0, 1), ("AAPL", 101.0, 2), ("AAPL", 99.0, 3), ("AAPL", 101.0, 4))
        self.assertEqual(len(self.fired()), 1)
        rule = next(iter(e.rules.values()))
        self.assertEqual(rule.suppressed, 1)
        st = e.status(store.alerts_list()[0])
        self.assertEqual(st["state"], "not_live")          # hub not running → not live (with the reason)
        self.clock.advance(seconds=61)
        self.feed(e, ("AAPL", 99.0, 70), ("AAPL", 101.0, 71))
        self.assertEqual(len(self.fired()), 2)
        self.assertEqual(len(store.alert_log_list(10)), 2)

    def test_restart_does_not_repeat_a_trigger(self):
        new()
        e = self.eng()
        run(e.sync())
        self.feed(e, ("AAPL", 99.0, 1), ("AAPL", 101.0, 2))
        self.assertEqual(len(self.fired()), 1)
        e.running = False                                 # "restart": a new engine on the same store
        self.hub.trade_hook = None
        e2 = self.eng()
        run(e2.sync())
        self.feed(e2, ("AAPL", 101.0, 3))                 # baseline after the restart: never a trigger
        self.feed(e2, ("AAPL", 99.0, 4), ("AAPL", 101.0, 5))   # crossing inside the persisted cooldown
        self.assertEqual(len(self.fired()), 1)
        self.clock.advance(seconds=61)
        self.feed(e2, ("AAPL", 99.0, 80), ("AAPL", 101.0, 81))
        self.assertEqual(len(self.fired()), 2)

    def test_rule_deleted_before_logging_sends_nothing(self):
        a = new()
        e = self.eng()
        run(e.sync())
        e.on_trade("stocks", tr("AAPL", 99.0, 1))
        e.on_trade("stocks", tr("AAPL", 101.0, 2))
        al.delete(a["id"])
        run(e._flush())
        self.assertEqual(self.fired(), [])

    def test_one_broken_rule_does_not_stop_the_others(self):
        new(level=100.0, direction="up")
        b = new(level=100.5, direction="up")
        e = self.eng()
        run(e.sync())
        orig = e._evaluate

        def boom(rule, *a):
            if rule.params["level"] == 100.0:
                raise RuntimeError("x")
            return orig(rule, *a)
        e._evaluate = boom
        self.feed(e, ("AAPL", 99.0, 1), ("AAPL", 101.0, 2))
        self.assertEqual([m["alert_id"] for m in self.fired()], [b["id"]])


class LineTest(Case):
    def test_moved_line_changes_the_level_deleted_line_disables(self):
        line = alert_line(p=100.0)
        a = al.create(AnyData(), {"symbol": "AAPL", "kind": "line_cross", "params": {"drawing_id": line["id"]}}, now=NOW)
        e = self.eng()
        run(e.sync())
        self.assertEqual(e.rules[a["id"]].level, 100.0)
        dr.patch(line["id"], {"points": [{"t": "2026-10-14T14:00:00+00:00", "p": 110.0}]}, now=NOW)
        run(e.sync())
        self.assertEqual(e.rules[a["id"]].level, 110.0)
        self.feed(e, ("AAPL", 99.0, 1), ("AAPL", 101.0, 2))      # crosses the OLD level: nothing
        self.assertEqual(self.fired(), [])
        self.feed(e, ("AAPL", 111.0, 3))
        self.assertEqual(self.fired()[0]["message"], "AAPL crossed the alert line at 110.00 (upward) — last trade 111.00.")
        self.assertEqual(self.hub.demand()["alerts"], 1)
        dr.delete(line["id"])
        run(e.sync())
        row = store.alert_get(a["id"])
        self.assertEqual((row["enabled"], row["disabled_reason"]), (False, al.LINE_DELETED))
        self.assertEqual(e.status(row), dict(e.status(row), state="disabled", reason=al.LINE_DELETED))
        self.assertNotIn(a["id"], e.rules)
        self.assertEqual(self.hub.demand()["alerts"], 0)              # demand released
        self.assertTrue(any(x["action"] == "alert_disabled" for x in store.list_audit()))


class DayMoveTest(Case):
    def test_once_per_day_and_side_vs_previous_close(self):
        a = new("day_move", pct=5, direction="any")
        day = ["2026-10-15"]
        calls = []

        def prev(sym, ac, now):
            calls.append((sym, ac))
            return {"value": 100.0, "feed": "sip_daily", "reason": None}
        e = self.eng(prev=prev, day=lambda ac, now: day[0])
        run(e.sync())
        self.assertEqual(e.day_ctx["AAPL"][1]["value"], 100.0)
        self.feed(e, ("AAPL", 104.0, 1), ("AAPL", 105.5, 2))
        self.assertEqual(len(self.fired()), 1)
        self.assertEqual(self.fired()[0]["message"], "AAPL is up 5.50 % today — last trade 105.50 vs previous close 100.00.")
        self.assertAlmostEqual(self.fired()[0]["level"], 105.0)
        self.clock.advance(seconds=120)                          # cooldown over — still once per day and side
        self.feed(e, ("AAPL", 104.0, 130), ("AAPL", 106.0, 131))
        self.assertEqual(len(self.fired()), 1)
        self.feed(e, ("AAPL", 94.0, 132))                         # the other side fires once
        self.assertEqual(len(self.fired()), 2)
        self.assertIn("down 6.00 %", self.fired()[1]["message"])
        day[0] = "2026-10-16"                                     # next session: previous close re-read
        self.clock.advance(seconds=120)
        run(e.sync())
        self.assertEqual(len(calls), 2)
        self.feed(e, ("AAPL", 104.0, 300), ("AAPL", 106.0, 301))
        self.assertEqual(len(self.fired()), 3)
        self.assertEqual(store.alert_get(a["id"])["state"]["days"][-1], "2026-10-16:up")

    def test_missing_previous_close_waits_with_the_reason(self):
        a = new("day_move", pct=5, direction="up")

        def prev(sym, ac, now):
            raise DataSourceUnavailable("alpaca", "equity_bars_history", "network/TLS error")
        e = self.eng(prev=prev, day=lambda ac, now: "2026-10-15")
        e._live = lambda kind, sym: (True, None)
        run(e.sync())
        st = e.status(store.alert_get(a["id"]))
        self.assertEqual(st["state"], "waiting")
        self.assertIn("Previous close unavailable: alpaca is unavailable", st["reason"])
        self.feed(e, ("AAPL", 100.0, 1), ("AAPL", 200.0, 2))
        self.assertEqual(self.fired(), [])


class NewsTest(Case):
    def test_once_per_id_with_cooldown(self):
        a = new("news")
        e = self.eng()
        run(e.sync())
        sub = e.subs[a["id"]]
        self.assertIn("news:AAPL", sub.topics)
        item = {"id": 7, "headline": "Apple beats", "url": "https://www.benzinga.com/x", "created_at": NOW.isoformat()}
        sub.deliver({"type": "news", "item": item})
        sub.deliver({"type": "news", "item": dict(item, headline="Apple beats (updated)")})   # update: no 2nd
        run(e.sync())
        self.assertEqual([m["message"] for m in self.fired()], ["News for AAPL: Apple beats"])
        sub.deliver({"type": "news", "item": dict(item, id=8)})                       # inside the cooldown
        run(e.sync())
        self.assertEqual(len(self.fired()), 1)
        self.clock.advance(seconds=61)
        sub.deliver({"type": "news", "item": dict(item, id=9, headline="Another")})
        run(e.sync())
        self.assertEqual(len(self.fired()), 2)
        self.assertEqual(store.alert_log_list(1)[0]["detail"]["news_id"], 9)
        self.assertEqual(store.alert_get(a["id"])["state"]["news_ids"], [7, 8, 9])   # 8 handled (suppressed)


class StatusTest(Case):
    def test_market_closed_and_baseline_states(self):
        a = new()
        self.clock.now = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)   # Saturday
        e = self.eng()
        e._live = lambda kind, sym: (True, None)
        run(e.sync())
        st = e.status(store.alert_get(a["id"]))
        self.assertEqual(st["state"], "waiting")
        self.assertEqual(st["reason"], al.BASELINE_REASON + al.CLOSED_NOTE)
        self.feed(e, ("AAPL", 99.0, 1))
        st = e.status(store.alert_get(a["id"]))
        self.assertEqual((st["state"], st["last_price"]["price"], st["level"]), ("armed", 99.0, 100.0))
        e.running = False
        self.assertEqual(e.status(store.alert_get(a["id"]))["state"], "not_running")


class FlushFirstTest(Case):
    """Review M3: a trigger is written + sent while the previous-close fetch hangs."""

    def test_trigger_not_blocked_by_a_hanging_prev_close(self):
        import threading
        import time as _t
        new()                                              # price_cross 100 up
        new("day_move", pct=5, direction="any")            # needs a previous close → fetch hangs
        release = threading.Event()

        def hang(sym, ac, now):
            release.wait(10)
            return {"value": 100.0}
        e = AlertEngine(self.hub, lambda: None, now=self.clock, reload_seconds=0.05, prev_close_fn=hang,
                        day_fn=lambda ac, now: "2026-10-15")

        async def body():
            task = asyncio.ensure_future(e.run())
            try:
                for _ in range(200):
                    if e.rules:
                        break
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.1)                   # the prev-close fetch is hanging now
                e.on_trade("stocks", tr("AAPL", 99.0, 1))
                e.on_trade("stocks", tr("AAPL", 101.0, 2))
                t0 = _t.monotonic()
                while not self.out.of("alert") and _t.monotonic() - t0 < 2:
                    await asyncio.sleep(0.01)
                self.assertEqual(len(self.out.of("alert")), 1)
                self.assertLess(_t.monotonic() - t0, 1.0)
                self.assertEqual(len(store.alert_log_list(5)), 1)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        run(body())


class HubIntegrationTest(unittest.TestCase):
    """Real hub + fake Alpaca: demand, the 30-symbol limit, end-to-end trigger, crash isolation."""

    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def make(self):
        async def ok_probe():
            return True
        self.srv = FakeAlpaca()
        self.hub = MarketDataHub(transport=self.srv, keys=keys, probe=ok_probe, next_probe_in=lambda: 0.0,
                                 data=lambda: HubData(), config=FAST)
        self.eng = AlertEngine(self.hub, lambda: None, reload_seconds=0.05)
        self.out = Sink("alerts", active=False)          # a hidden view: receives alerts, adds no demand
        self.hub.register(self.out)

    def test_demand_limit_and_end_to_end(self):
        self.make()
        syms = ["AAPL"] + [f"Z{i:03d}" for i in range(limits.STOCK_STREAM_SYMBOL_LIMIT)]
        for i, s in enumerate(syms):
            new(symbol=s, level=100.0 + i, direction="up")

        async def body():
            task = asyncio.ensure_future(self.eng.run())
            try:
                self.assertTrue(await wait_for(lambda: self.hub.demand()["alerts"] == len(syms)))
                self.assertTrue(self.hub.demand()["streams_wanted"])
                self.assertTrue(await wait_for(lambda: self.srv.live("stocks") and
                                               len(self.srv.live("stocks")[0].subs) == limits.STOCK_STREAM_SYMBOL_LIMIT))
                rows = {r["symbol"]: r for r in store.alerts_list()}
                last = syms[-1]
                st = self.eng.status(rows[last])
                self.assertEqual(st["state"], "not_live")
                self.assertEqual(st["reason"], f"Stream limit: 30 symbols — {last} is not live." + al.NOT_LIVE_SUFFIX)
                self.assertTrue(await wait_for(lambda: self.eng.status(rows["AAPL"])["live"]))
                conn = self.srv.live("stocks")[0]
                conn.push([trade("AAPL", 99.0, 1, "2026-10-15T14:00:01Z"), trade("AAPL", 100.2, 1, "2026-10-15T14:00:02Z")])
                self.assertTrue(await wait_for(lambda: self.out.of("alert")))
                self.assertEqual(self.out.of("alert")[0]["symbol"], "AAPL")
                # switch every alert off → demand released, streams no longer wanted
                for r in store.alerts_list():
                    al.update(r["id"], {"enabled": False})
                self.eng.poke()
                self.assertTrue(await wait_for(lambda: self.hub.demand()["alerts"] == 0))
                self.assertFalse(self.hub.demand()["streams_wanted"])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        run(running(self.hub, body))

    def test_engine_crash_releases_demand_and_hub_keeps_running(self):
        self.make()
        new()

        async def body():
            task = asyncio.ensure_future(self.eng.run())
            self.assertTrue(await wait_for(lambda: self.hub.demand()["alerts"] == 1))

            def broken():
                raise RuntimeError("store exploded")
            self.eng.store = type("S", (), {"alerts_list": staticmethod(broken),
                                            "StoreUnavailable": store.StoreUnavailable})()
            self.eng.poke()
            res = await asyncio.gather(task, return_exceptions=True)
            self.assertIsInstance(res[0], RuntimeError)        # → the supervisor restarts the task
            self.assertEqual(self.hub.demand()["alerts"], 0)
            self.assertFalse(self.eng.running)
            self.assertTrue(self.hub.running)

            def bad_hook(endpoint, t):
                raise RuntimeError("hook")
            self.hub.trade_hook = bad_hook                       # a failing hook never stops the stream
            sink = Sink("trades:AAPL")
            self.hub.register(sink)
            self.assertTrue(await wait_for(lambda: self.srv.live("stocks")))
            self.srv.live("stocks")[0].push([trade("AAPL", 99.0, 1, "2026-10-15T14:00:01Z")])
            self.assertTrue(await wait_for(lambda: sink.of("trade")))
            self.assertTrue(self.hub.running)
        run(running(self.hub, body))


if __name__ == "__main__":
    unittest.main()
