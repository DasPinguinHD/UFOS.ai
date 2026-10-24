"""Market Research MR2b — the market-data hub (market_research/SPEC.md §7.6, §4.3, §13 MR2b notes):
multiplexing (2 subscribers, 1 connection), lazy start/stop + linger, the 30-symbol stock limit message,
Alpaca 406 / 402 / 409 / missing keys -> unavailable + re-check schedule, reconnect backoff + attempt counter +
probe use and spacing, silence -> ping -> reconnect, live candle throttle + boundaries through the hub,
IEX tail -> SIP replacement (chunked), hub crash isolated from a dummy engine.
Fake Alpaca transport; no network.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_hub -v
"""
import asyncio
import contextvars
import logging
import re
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from market_research import limits
from market_research.hub import hub as hub_mod
from market_research.hub import protocol
from market_research.hub.hub import MarketDataHub, chunk_bars
from market_research.supervisor import ResearchSupervisor, RestartPolicy
from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing
from strategy_platform.data import http_gate
from strategy_platform.data.providers.alpaca import AUTH_MESSAGE

from tests.mr_hub_testkit import FAST, Clock, FakeAlpaca, FakeData, Sink, keys, minute, running, trade, wait_for

UTC = timezone.utc
_LOG = logging.getLogger("market_research")


def setUpModule():
    global _level
    _level = _LOG.level
    _LOG.setLevel(logging.CRITICAL)


def tearDownModule():
    _LOG.setLevel(_level)


def make_hub(srv=None, *, clock=None, data=None, probe=None, next_probe_in=None, config=FAST, keys_fn=keys,
             status_fn=None):
    async def ok_probe():
        return True
    data = data or FakeData()
    return MarketDataHub(transport=srv or FakeAlpaca(), keys=keys_fn, probe=probe or ok_probe,
                         next_probe_in=next_probe_in or (lambda: 0.0), data=lambda: data, config=config,
                         now=clock or Clock(), status_fn=status_fn)


def run(coro):
    return asyncio.run(coro)


class MultiplexTest(unittest.TestCase):
    def test_two_subscribers_one_connection(self):
        srv = FakeAlpaca()
        hub = make_hub(srv)
        a = Sink("trades:AAPL", "candle:AAPL:1m:rth")
        b = Sink("trades:aapl", "trades:MSFT")
        hub.register(a)
        hub.register(b)

        async def body():
            self.assertTrue(await wait_for(lambda: srv.live("stocks") and srv.live("stocks")[0].subs == {"AAPL", "MSFT"}))
            c = srv.live("stocks")[0]
            self.assertEqual(srv.connects, ["stocks"])                     # one connection, no crypto/news
            self.assertEqual(c.sent[0], {"action": "auth", "key": "KEYID", "secret": "SECRET"})
            subs = [m for m in c.sent if m["action"] == "subscribe"]
            self.assertEqual(subs, [{"action": "subscribe", "trades": ["AAPL", "MSFT"], "bars": ["AAPL", "MSFT"]}])
            c.push([trade("AAPL", 100.5, 10, "2026-10-15T14:00:01.123456789Z")])
            self.assertTrue(await wait_for(lambda: a.of("trade") and b.of("trade")))
            self.assertEqual(a.of("trade")[0], {"type": "trade", "symbol": "AAPL", "price": 100.5, "size": 10.0,
                                                "ts": "2026-10-15T14:00:01+00:00", "feed": "iex"})
            c.push([trade("MSFT", 300.0, 1, "2026-10-15T14:00:02Z")])
            self.assertTrue(await wait_for(lambda: b.of("trade", symbol="MSFT")))
            self.assertEqual(a.of("trade", symbol="MSFT"), [])
            row = hub.feeds_status()[0]
            self.assertEqual((row["state"], row["symbols"], row["attempt"]), ("live", 2, 0))
            self.assertEqual(row["message"], "Live · 2 symbols")
            self.assertEqual(row["connected_since"], "2026-10-15T14:00:00+00:00")
            self.assertEqual(hub.demand()["views"], 2)
            hub.unregister(b)                                                # MSFT only wanted by b
            self.assertTrue(await wait_for(lambda: {"action": "unsubscribe", "trades": ["MSFT"], "bars": ["MSFT"]}
                                           in c.sent))
            self.assertEqual(len(srv.connects), 1)
            self.assertEqual(a.of("symbol_status", symbol="AAPL")[-1],
                             {"type": "symbol_status", "symbol": "AAPL", "live": True, "reason": None})
        run(running(hub, body))

    def test_crypto_and_news_endpoints(self):
        srv = FakeAlpaca()
        hub = make_hub(srv)
        a = Sink("trades:BTC-USD", "news:AAPL", "news:BTC-USD")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: srv.live("crypto") and srv.live("crypto")[0].subs == {"BTC/USD"}
                                           and srv.live("news") and srv.live("news")[0].subs == {"AAPL", "BTCUSD"}))
            self.assertNotIn("stocks", srv.connects)
            srv.live("crypto")[0].push([trade("BTC/USD", 60000.0, 0.5, "2026-10-15T14:00:01Z")])
            self.assertTrue(await wait_for(lambda: a.of("trade")))
            self.assertEqual(a.of("trade")[0]["symbol"], "BTC-USD")
            self.assertEqual(a.of("trade")[0]["feed"], "crypto")
            srv.live("news")[0].push([{"T": "n", "id": 7, "headline": "  Big\nnews  ", "summary": "s",
                                       "source": "benzinga", "url": "javascript:alert(1)",
                                       "created_at": "2026-10-15T14:00:00Z", "updated_at": "2026-10-15T14:00:00Z",
                                       "symbols": ["BTCUSD"]}])
            self.assertTrue(await wait_for(lambda: a.of("news")))
            item = a.of("news")[0]["item"]
            self.assertEqual((item["headline"], item["url"], item["publisher"]), ("Big news", None, "benzinga"))
            self.assertEqual(item["images"], [])
        run(running(hub, body))


class LazyTest(unittest.TestCase):
    def test_streams_only_while_visible_then_linger(self):
        srv = FakeAlpaca()
        hub = make_hub(srv)
        a = Sink("trades:AAPL", active=False)
        hub.register(a)

        async def body():
            await asyncio.sleep(0.1)
            self.assertEqual(srv.connects, [])
            self.assertEqual(hub.demand(), {"views": 0, "alerts": 0, "streams_wanted": False,
                                            "note": hub_mod.DEMAND_NOTE})
            self.assertEqual(hub.feeds_status()[0]["state"], "off")
            self.assertTrue(hub.feeds_status()[0]["message"].startswith("Off — live streams run only while"))
            a.active = True
            hub.refresh()
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "live"))
            self.assertEqual(hub.feeds_status()[1]["message"], "Off — no crypto symbol is open.")
            a.active = False
            hub.refresh()
            t0 = time.monotonic()
            await asyncio.sleep(0.05)
            self.assertEqual(hub.conns["stocks"].state, "live")                # lingering
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "off"))
            self.assertGreaterEqual(time.monotonic() - t0, FAST.linger - 0.02)
            self.assertEqual(srv.live("stocks"), [])
            a.active = True                                                    # visible again: new connection
            hub.refresh()
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "live"))
            self.assertEqual(srv.connects, ["stocks", "stocks"])
        run(running(hub, body))

    def test_alert_subscriber_counts_as_demand(self):
        srv = FakeAlpaca()
        hub = make_hub(srv)
        hub.register(Sink("trades:AAPL", kind="alert"))

        async def body():
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "live"))
            self.assertEqual(hub.demand()["alerts"], 1)
            self.assertTrue(hub.demand()["streams_wanted"])
        run(running(hub, body))


class SymbolLimitTest(unittest.TestCase):
    def test_30_symbol_limit_message(self):
        srv = FakeAlpaca()
        hub = make_hub(srv)
        syms = [f"SYM{i:02d}" for i in range(1, 33)]
        a = Sink(*[f"trades:{s}" for s in syms], "trades:BTC-USD")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: srv.live("stocks") and len(srv.live("stocks")[0].subs) == 30))
            self.assertEqual(srv.live("stocks")[0].subs, set(syms[:30]))
            self.assertTrue(await wait_for(lambda: srv.live("crypto")))      # crypto has no limit
            st = {m["symbol"]: m for m in a.of("symbol_status")}
            for s in ("SYM31", "SYM32"):
                self.assertEqual(st[s], {"type": "symbol_status", "symbol": s, "live": False,
                                         "reason": f"Stream limit: 30 symbols — {s} is not live."})
            self.assertTrue(await wait_for(lambda: a.of("symbol_status", symbol="SYM01")[-1]["live"]))
            self.assertEqual(hub.feeds_status()[0]["symbol_limit"], 30)
            del a.topics["trades:SYM01"]                                       # a slot frees up
            hub.refresh()
            self.assertTrue(await wait_for(lambda: "SYM31" in srv.live("stocks")[0].subs))
            self.assertNotIn("SYM01", srv.live("stocks")[0].subs)
            self.assertTrue(await wait_for(lambda: a.of("symbol_status", symbol="SYM31")[-1]["live"]))
            self.assertFalse(a.of("symbol_status", symbol="SYM32")[-1]["live"])
        run(running(hub, body))

    def test_alpaca_405_marks_refused_symbols(self):
        # review L5: 405 rejects the whole request -> refused symbols are off the wire, retried one by one,
        # again when a slot frees and at every demand change
        srv = FakeAlpaca()
        srv.max_symbols = 2
        hub = make_hub(srv)
        a = Sink("trades:AAA", "trades:BBB", "trades:CCC")
        hub.register(a)
        limit_ccc = "Stream limit: Alpaca refused more symbols — CCC is not live."

        async def body():
            self.assertTrue(await wait_for(lambda: a.of("symbol_status", symbol="CCC")
                                           and a.of("symbol_status", symbol="CCC")[-1]["reason"] == limit_ccc
                                           and a.of("symbol_status", symbol="BBB")[-1]["live"]))
            self.assertTrue(a.of("symbol_status", symbol="AAA")[-1]["live"])
            c = srv.live("stocks")[0]
            subs = [m for m in c.sent if m["action"] == "subscribe"]
            self.assertEqual(subs[0]["trades"], ["AAA", "BBB", "CCC"])          # refused as a whole
            self.assertEqual([m["trades"] for m in subs[1:]], [["AAA"], ["BBB"], ["CCC"]])   # one by one
            conn = hub.conns["stocks"]
            self.assertEqual(set(conn.subscribed), {"AAA", "BBB"})               # CCC is not on the wire
            self.assertEqual(hub.feeds_status()[0]["symbols"], 2)
            n = len(c.sent)
            await asyncio.sleep(0.1)
            self.assertEqual(len(c.sent), n)                                    # no retry loop
            # a slot frees (AAA unsubscribed) -> CCC is retried and goes live
            del a.topics["trades:AAA"]
            hub.refresh()
            self.assertTrue(await wait_for(lambda: a.of("symbol_status", symbol="CCC")[-1]["live"]))
            self.assertEqual(set(conn.subscribed), {"BBB", "CCC"})
            self.assertEqual(c.sent[-1], {"action": "subscribe", "trades": ["CCC"], "bars": ["CCC"]})
            # refused again -> retried at a demand change once Alpaca has room
            a.add("trades:DDD")
            hub.refresh()
            self.assertTrue(await wait_for(lambda: a.of("symbol_status", symbol="DDD")
                                           and a.of("symbol_status", symbol="DDD")[-1]["reason"]
                                           == "Stream limit: Alpaca refused more symbols — DDD is not live."))
            n = len(c.sent)
            await asyncio.sleep(0.05)
            self.assertEqual(len(c.sent), n)
            srv.max_symbols = 3
            hub.register(Sink("trades:BBB"))                                    # demand change (views 1 -> 2)
            self.assertTrue(await wait_for(lambda: a.of("symbol_status", symbol="DDD")[-1]["live"]))
            self.assertEqual(set(conn.subscribed), {"BBB", "CCC", "DDD"})
        run(running(hub, body))

    def test_alpaca_405_after_partial_accept(self):
        # unverified variant: Alpaca confirms part of the batch, then sends 405
        srv = FakeAlpaca()
        srv.max_symbols, srv.partial_405 = 2, True
        hub = make_hub(srv)
        a = Sink("trades:AAA", "trades:BBB", "trades:CCC")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: a.of("symbol_status", symbol="CCC")
                                           and a.of("symbol_status", symbol="CCC")[-1]["reason"]
                                           == "Stream limit: Alpaca refused more symbols — CCC is not live."))
            self.assertTrue(a.of("symbol_status", symbol="AAA")[-1]["live"])
            self.assertEqual(set(hub.conns["stocks"].subscribed), {"AAA", "BBB"})
        run(running(hub, body))


class RefusedTest(unittest.TestCase):
    def _unavailable(self, mode, expected):
        srv = FakeAlpaca()
        srv.mode["stocks"] = mode
        hub = make_hub(srv)
        a = Sink("trades:AAPL")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "unavailable"))
            row = hub.feeds_status()[0]
            self.assertEqual((row["state"], row["message"], row["attempt"]), ("unavailable", expected, 0))
            self.assertEqual(a.of("symbol_status")[-1], {"type": "symbol_status", "symbol": "AAPL", "live": False,
                                                         "reason": expected})
            await asyncio.sleep(FAST.recheck_refused * 0.5)
            self.assertEqual(srv.connects, ["stocks"])                        # no fast reconnect loop
            self.assertTrue(await wait_for(lambda: len(srv.connects) == 2, 2))   # slow re-check
            await asyncio.sleep(0.02)
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "unavailable"))
            n = len(srv.connects)
            hub.register(Sink("trades:AAPL"))                                  # demand change -> re-check now
            self.assertTrue(await wait_for(lambda: len(srv.connects) == n + 1, FAST.recheck_refused * 0.8))
        run(running(hub, body))

    def test_406_connection_limit(self):
        self._unavailable("406", protocol.CONNECTION_LIMIT_MESSAGE)
        self.assertEqual(protocol.CONNECTION_LIMIT_MESSAGE, "Live data unavailable: Alpaca allows one stream "
                         "connection per account and another program is using it.")

    def test_402_keys_rejected(self):
        self._unavailable("auth_fail", AUTH_MESSAGE)

    def test_409_plan(self):
        self._unavailable("plan", protocol.PLAN_MESSAGE)

    def test_missing_keys_rechecked_every_60s(self):
        srv = FakeAlpaca()
        calls = []

        def no_keys():
            calls.append(1)
            raise SecretMissing("alpaca-paper-key-id", "Alpaca paper API key ID")
        hub = make_hub(srv, keys_fn=no_keys)
        hub.register(Sink("trades:AAPL"))

        async def body():
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "unavailable"))
            self.assertEqual(hub.feeds_status()[0]["message"],
                             "Alpaca paper API key ID is not set — enter it in Hedge Fund → Settings → Keys.")
            self.assertEqual(srv.connects, [])
            self.assertTrue(await wait_for(lambda: len(calls) >= 2, 2))       # FAST.recheck_missing_keys
        run(running(hub, body))
        self.assertEqual(limits.RECHECK_MISSING_KEYS_SECONDS, 60)
        self.assertEqual(limits.RECHECK_REFUSED_SECONDS, 300)


class ReconnectTest(unittest.TestCase):
    WAIT_RE = re.compile(r"^Waiting for connection \(attempt (\d+)\) — (next try in \d+ s|checking the connection)$")

    def test_backoff_attempts_and_probe_spacing(self):
        srv = FakeAlpaca()
        srv.fail_connect.add("stocks")
        probes, spacing, last = [], 0.2, [None]
        hub = None

        async def probe():
            probes.append((time.monotonic(), hub.conns["stocks"].attempt))
            last[0] = time.monotonic()
            return result[0]

        def next_probe_in():
            return 0.0 if last[0] is None else max(0.0, last[0] + spacing - time.monotonic())
        result = [False]
        hub = make_hub(srv, probe=probe, next_probe_in=next_probe_in)
        a = Sink("trades:AAPL")
        hub.register(a)
        seen = []

        async def body():
            for _ in range(160):
                row = hub.feeds_status()[0]
                if row["state"] == "waiting":
                    self.assertRegex(row["message"], self.WAIT_RE)
                    self.assertEqual(int(self.WAIT_RE.match(row["message"]).group(1)), row["attempt"])
                    seen.append(row["attempt"])
                await asyncio.sleep(0.005)
            self.assertEqual(seen[0], 1)
            self.assertGreaterEqual(max(seen), 3)
            self.assertEqual(seen, sorted(seen))                                # counter only grows
            self.assertTrue(probes)
            self.assertTrue(all(att >= 2 for _t, att in probes))                # never probed on attempt 1
            gaps = [b[0] - a_[0] for a_, b in zip(probes, probes[1:])]
            self.assertTrue(all(g >= spacing - 0.02 for g in gaps), gaps)       # probe spacing respected
            self.assertEqual(srv.connects, ["stocks", "stocks"])                # probe False -> no connect
            st = a.of("symbol_status")[-1]
            self.assertFalse(st["live"])
            self.assertRegex(st["reason"], r"^Stocks stream: waiting for connection \(attempt \d+\)\.$")
            result[0] = True                                                    # network back
            srv.fail_connect.clear()
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "live", 3))
            self.assertEqual(hub.feeds_status()[0]["attempt"], 0)
            self.assertTrue(a.of("symbol_status")[-1]["live"])
        run(running(hub, body))

    def test_probe_user_facing_error_makes_feed_unavailable(self):
        srv = FakeAlpaca()
        srv.fail_connect.add("stocks")

        async def probe():
            raise DataSourceUnavailable("alpaca", "equity_quotes", "keys rejected")
        hub = make_hub(srv, probe=probe)
        hub.register(Sink("trades:AAPL"))

        async def body():
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "unavailable"))
            self.assertIn("keys rejected", hub.feeds_status()[0]["message"])
        run(running(hub, body))

    def test_drop_reconnects_and_stable_connection_resets_backoff(self):
        srv = FakeAlpaca()
        cfg = FAST.__class__(**{**FAST.__dict__, "stable": 0.05})
        hub = make_hub(srv, config=cfg)
        hub.register(Sink("trades:AAPL"))

        async def body():
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "live"))
            hub.conns["stocks"]._failures = 3                   # pretend earlier failures
            await asyncio.sleep(0.08)                            # live ≥ stable
            srv.live("stocks")[0].drop()
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "waiting"))
            self.assertEqual(hub.conns["stocks"].attempt, 1)
            self.assertTrue(await wait_for(lambda: hub.conns["stocks"].state == "live"))
            self.assertEqual(len(srv.connects), 2)
            self.assertEqual(srv.live("stocks")[0].subs, {"AAPL"})          # re-subscribed
        run(running(hub, body))

    def test_silence_ping_then_reconnect(self):
        srv = FakeAlpaca()
        hub = make_hub(srv)
        hub.register(Sink("trades:AAPL"))

        async def body():
            self.assertTrue(await wait_for(lambda: srv.live("stocks")))
            srv.live("stocks")[0].pong = True
            await asyncio.sleep(FAST.silence + 0.1)
            self.assertGreaterEqual(srv.pings, 1)                # pong -> stays connected
            self.assertEqual(len(srv.connects), 1)
            srv.live("stocks")[0].pong = False
            self.assertTrue(await wait_for(lambda: len(srv.connects) == 2, 2))
        run(running(hub, body))

    def test_default_probe_runs_under_research_priority(self):
        seen = {}

        def fake_probe(source):
            seen["source"] = source
            seen["research"] = http_gate.is_research_priority()
            import threading
            seen["thread"] = threading.current_thread().name
            return True
        with mock.patch("strategy_platform.data.network.probe", fake_probe):
            self.assertTrue(run(hub_mod.default_probe()))
        self.assertEqual(seen["source"], "alpaca")
        self.assertTrue(seen["research"])
        self.assertTrue(seen["thread"].startswith("research"))
        self.assertFalse(http_gate.is_research_priority())


class LiveCandleHubTest(unittest.TestCase):
    def test_throttle_and_boundary(self):
        srv = FakeAlpaca()
        clock = Clock(datetime(2026, 10, 15, 14, 0, 0, tzinfo=UTC))      # 10:00 ET, bucket start: empty seed
        hub = make_hub(srv, clock=clock)
        a = Sink("trades:AAPL", "candle:AAPL:1m:rth")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: srv.live("stocks") and srv.live("stocks")[0].subs == {"AAPL"}))
            c = srv.live("stocks")[0]
            t0 = time.monotonic()
            for i in range(60):
                c.push([trade("AAPL", 100 + i * 0.01, 1, f"2026-10-15T14:00:{i % 59 + 1:02d}Z")])
                await asyncio.sleep(0.01)
            elapsed = time.monotonic() - t0
            self.assertTrue(await wait_for(lambda: a.of("candle") and a.of("candle")[-1]["bar"]["v"] == 60.0))
            sent_during = len(a.of("candle"))
            self.assertLessEqual(sent_during, int(elapsed * limits.LIVE_CANDLE_MAX_PER_SECOND) + 2)
            self.assertGreaterEqual(sent_during, 2)
            self.assertLessEqual(len(a.of("trade")), int(elapsed * limits.TRADE_MAX_PER_SECOND) + 2)
            bar = a.of("candle")[-1]["bar"]
            self.assertEqual((bar["t"], bar["feed"], bar["final"], bar["n"]),
                             ("2026-10-15T14:00:00+00:00", "iex_tail", False, 60))
            self.assertEqual(a.of("candle")[-1]["tf"], "1m")
            self.assertEqual(a.of("candle")[-1]["session"], "rth")
            c.push([trade("AAPL", 101.0, 2, "2026-10-15T14:01:00.5Z")])   # next bucket: old one closes first
            self.assertTrue(await wait_for(lambda: a.of("candle")[-1]["bar"]["t"] == "2026-10-15T14:01:00+00:00"))
            ts = [m["bar"]["t"] for m in a.of("candle")]
            last_old = max(i for i, t in enumerate(ts) if t.endswith("14:00:00+00:00"))
            self.assertEqual(a.of("candle")[last_old]["bar"]["v"], 60.0)
            self.assertEqual(a.of("candle")[-1]["bar"]["v"], 2.0)
            # minute bar from the stream replaces the trades of its minute
            c.push([{"T": "b", "S": "AAPL", "o": 101, "h": 102, "l": 100.5, "c": 101.5, "v": 500, "vw": 101.2,
                     "n": 9, "t": "2026-10-15T14:01:00Z"}])
            self.assertTrue(await wait_for(lambda: a.of("candle")[-1]["bar"]["v"] == 500.0))
            self.assertEqual(a.of("candle")[-1]["bar"]["h"], 102)
        run(running(hub, body))

    def test_crypto_bucket_closes_by_time_final_true(self):
        srv = FakeAlpaca()
        clock = Clock(datetime(2026, 10, 15, 14, 0, 0, tzinfo=UTC))
        hub = make_hub(srv, clock=clock)
        a = Sink("candle:BTC-USD:1m:utc")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: srv.live("crypto") and srv.live("crypto")[0].subs == {"BTC/USD"}))
            srv.live("crypto")[0].push([trade("BTC/USD", 60000.0, 0.1, "2026-10-15T14:00:10Z")])
            self.assertTrue(await wait_for(lambda: a.of("candle")))
            self.assertFalse(a.of("candle")[-1]["bar"]["final"])
            clock.now = datetime(2026, 10, 15, 14, 1, 1, tzinfo=UTC)
            self.assertTrue(await wait_for(lambda: a.of("candle")[-1]["bar"]["final"]))
            self.assertEqual(a.of("candle")[-1]["bar"]["feed"], "crypto")
            self.assertEqual(a.of("candle")[-1]["session"], "utc")
        run(running(hub, body))

    def test_late_trade_after_clock_close_sends_a_corrected_final(self):
        # review H1 through the hub: final by the clock, then a trade of that minute a few ms later
        srv = FakeAlpaca()
        clock = Clock(datetime(2026, 10, 15, 14, 0, 0, tzinfo=UTC))
        hub = make_hub(srv, clock=clock)
        a = Sink("candle:BTC-USD:1m:utc")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: srv.live("crypto") and srv.live("crypto")[0].subs == {"BTC/USD"}))
            c = srv.live("crypto")[0]
            c.push([trade("BTC/USD", 100.0, 1.0, "2026-10-15T14:00:10Z")])
            self.assertTrue(await wait_for(lambda: a.of("candle")))
            clock.now = datetime(2026, 10, 15, 14, 1, 0, 5000, tzinfo=UTC)
            self.assertTrue(await wait_for(lambda: a.of("candle")[-1]["bar"]["final"]))
            c.push([trade("BTC/USD", 101.0, 1.0, "2026-10-15T14:00:59.990Z")])
            self.assertTrue(await wait_for(lambda: a.of("candle")[-1]["bar"]["v"] == 2.0))
            await asyncio.sleep(0.05)
            same_t = [m["bar"] for m in a.of("candle") if m["bar"]["t"] == "2026-10-15T14:00:00+00:00"]
            after_final = same_t[[b["final"] for b in same_t].index(True):]
            self.assertTrue(all(b["final"] for b in after_final))              # never a non-final restart
            self.assertEqual((after_final[-1]["o"], after_final[-1]["c"], after_final[-1]["h"],
                              after_final[-1]["v"]), (100.0, 101.0, 101.0, 2.0))
            # beyond the grace window: dropped
            clock.now = datetime(2026, 10, 15, 14, 1, 30, tzinfo=UTC)
            n = len(a.of("candle"))
            c.push([trade("BTC/USD", 1.0, 1.0, "2026-10-15T14:00:59.995Z")])
            await asyncio.sleep(0.05)
            self.assertEqual(len(a.of("candle")), n)
        run(running(hub, body))

    def test_mid_bucket_start_is_seeded_from_rest(self):
        srv = FakeAlpaca()
        clock = Clock(datetime(2026, 10, 15, 14, 3, 30, tzinfo=UTC))
        ms = [minute(datetime(2026, 10, 15, 14, m, tzinfo=UTC), o=100 + m, v=10, feed="iex_tail") for m in range(3)]
        data = FakeData(ms)
        hub = make_hub(srv, clock=clock, data=data)
        a = Sink("candle:AAPL:5m:rth")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: srv.live("stocks")))
            srv.live("stocks")[0].push([trade("AAPL", 105.0, 5, "2026-10-15T14:03:31Z")])
            self.assertTrue(await wait_for(lambda: a.of("candle")))
            bar = a.of("candle")[-1]["bar"]
            self.assertEqual((bar["t"], bar["o"], bar["c"], bar["v"]), ("2026-10-15T14:00:00+00:00", 100, 105.0, 35.0))
            call = [c for c in data.calls if c[0] == "equity_minutes"][0]
            self.assertEqual(call[2:4], (datetime(2026, 10, 15, 14, 0, tzinfo=UTC),
                                         datetime(2026, 10, 15, 14, 3, tzinfo=UTC)))
        run(running(hub, body))

    def test_seed_failure_sends_error_and_no_partial_candle(self):
        srv = FakeAlpaca()
        clock = Clock(datetime(2026, 10, 15, 14, 3, 30, tzinfo=UTC))
        data = FakeData()
        data.fail = DataSourceUnavailable("alpaca", "equity_bars", "network/TLS error")
        hub = make_hub(srv, clock=clock, data=data)
        a = Sink("candle:AAPL:5m:rth")
        hub.register(a)

        async def body():
            self.assertTrue(await wait_for(lambda: a.of("error")))
            err = a.of("error")[0]
            self.assertEqual((err["code"], err["topic"]), ("candle_unavailable", "candle:AAPL:5m:rth"))
            self.assertIn("network/TLS error", err["message"])
            srv.live("stocks")[0].push([trade("AAPL", 105.0, 5, "2026-10-15T14:03:31Z")])
            await asyncio.sleep(0.1)
            self.assertEqual(a.of("candle"), [])                       # never a partial candle
            data.fail = None
            self.assertTrue(await wait_for(lambda: a.of("candle"), 2))   # retried (seed_retry)
        run(running(hub, body))


class SipReplaceTest(unittest.TestCase):
    def test_tail_replaced_by_sip(self):
        clock = Clock(datetime(2026, 10, 15, 15, 0, tzinfo=UTC))            # sip_through 14:45
        ms = [minute(datetime(2026, 10, 15, 14, m, tzinfo=UTC), o=100 + m) for m in range(40, 50)]
        data = FakeData(ms)
        hub = make_hub(clock=clock, data=data)
        a = Sink("candle:AAPL:1m:rth", "candle:AAPL:5m:rth")
        hub.register(a)
        last = hub._sip_through["AAPL"]
        self.assertEqual(last, datetime(2026, 10, 15, 14, 43, tzinfo=UTC))
        new = datetime(2026, 10, 15, 14, 50, tzinfo=UTC)
        run(hub._sip_refresh("AAPL", last, new))
        self.assertEqual(data.calls, [("equity_minutes", "AAPL", datetime(2026, 10, 15, 14, 40, tzinfo=UTC), new,
                                       False)])
        rep = {m["tf"]: m for m in a.of("sip_replace")}
        self.assertEqual([b["t"][11:16] for b in rep["1m"]["bars"]],
                         ["14:43", "14:44", "14:45", "14:46", "14:47", "14:48", "14:49"])
        self.assertTrue(all(b["feed"] == "sip" and b["final"] for b in rep["1m"]["bars"]))
        self.assertEqual([b["t"][11:16] for b in rep["5m"]["bars"]], ["14:40", "14:45"])
        self.assertEqual(rep["5m"]["bars"][0]["o"], 140)
        self.assertEqual((rep["1m"]["part"], rep["1m"]["parts"], rep["1m"]["session"]), (1, 1, "rth"))
        self.assertEqual(hub._sip_through["AAPL"], new)

    def test_sip_replace_is_chunked(self):
        start = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)                   # 04:00 ET
        ms = [minute(start + timedelta(minutes=i)) for i in range(600)]
        clock = Clock(datetime(2026, 10, 15, 18, 20, tzinfo=UTC))
        hub = make_hub(clock=clock, data=FakeData(ms))
        a = Sink("candle:AAPL:1m:eth")
        hub.register(a)
        hub._sip_through["AAPL"] = start
        run(hub._sip_refresh("AAPL", start, start + timedelta(minutes=600)))
        parts = a.of("sip_replace")
        self.assertEqual([(m["part"], m["parts"], len(m["bars"])) for m in parts], [(1, 2, 500), (2, 2, 100)])
        self.assertEqual(parts[1]["bars"][0]["t"], (start + timedelta(minutes=500)).isoformat(timespec="seconds"))
        self.assertEqual(chunk_bars({"type": "x"}, []), [{"type": "x", "bars": [], "part": 1, "parts": 1}])

    def test_sip_failure_keeps_tail_and_reports(self):
        clock = Clock(datetime(2026, 10, 15, 15, 0, tzinfo=UTC))
        data = FakeData()
        data.fail = DataSourceUnavailable("alpaca_sip", "equity_bars_history", "HTTP 403")
        hub = make_hub(clock=clock, data=data)
        a = Sink("candle:AAPL:1m:rth")
        hub.register(a)
        hub.running = True
        hub.conns["stocks"].state = "live"
        last = hub._sip_through["AAPL"]
        run(hub._sip_refresh("AAPL", last, datetime(2026, 10, 15, 14, 50, tzinfo=UTC)))
        self.assertEqual(a.of("sip_replace"), [])
        st = a.of("symbol_status", symbol="AAPL")[-1]
        self.assertTrue(st["live"])
        self.assertTrue(st["reason"].startswith("SIP update failed: alpaca_sip is unavailable"))
        self.assertIn("The IEX live tail stays marked as tail.", st["reason"])
        self.assertEqual(hub._sip_through["AAPL"], last)                 # retried next round

    def test_only_8_symbols_tracked(self):
        hub = make_hub()
        hub.register(Sink(*[f"candle:S{i:02d}:1m:rth" for i in range(12)], "candle:BTC-USD:1m:utc",
                          "candle:ZZZ:1D:rth"))
        self.assertEqual(list(hub._sip_through), [f"S{i:02d}" for i in range(limits.SIP_TAIL_MAX_SYMBOLS)])


class IsolationTest(unittest.TestCase):
    def test_hub_crash_is_isolated_from_the_engine(self):
        policy = RestartPolicy(delays=(0.05,), max_restarts=3, window_seconds=60, healthy_after_seconds=60)
        sup = ResearchSupervisor(policy, store_status=lambda: {"state": "ok", "reason": None,
                                                               "file": "market_research.db", "schema_version": 2})
        srv = FakeAlpaca()
        hub = make_hub(srv)
        hub.register(Sink("trades:AAPL"))
        boom = {"on": True}
        real_flush = hub._flush

        def flush():
            if boom["on"]:
                raise RuntimeError("bug in the hub")
            real_flush()
        hub._flush = flush
        sup.add("market_data_hub", "Live market data", hub.run)
        ticks = [0]

        async def engine():
            while True:
                ticks[0] += 1
                await asyncio.sleep(0.005)

        async def scenario():
            eng = asyncio.ensure_future(engine())
            sup.start()
            ok = await wait_for(lambda: sup.status()["research"]["state"] == "unavailable")
            reason = sup.status()["research"]["reason"]
            rows = hub.feeds_status()
            before = ticks[0]
            await asyncio.sleep(0.05)
            alive = ticks[0] > before
            boom["on"] = False
            healed = await wait_for(lambda: hub.running and hub.conns["stocks"].state == "live", 3)
            await sup.stop(1.0)
            eng.cancel()
            return ok, reason, rows, alive, healed, eng
        ok, reason, rows, alive, healed, _eng = run(scenario())
        self.assertTrue(ok)
        self.assertIn('Research task "market_data_hub" crashed (RuntimeError — see backend.log)', reason)
        self.assertIn("Trading is not affected.", reason)
        self.assertTrue(all(r["state"] == "off" for r in rows))
        self.assertTrue(alive)                                                 # the engine kept running
        self.assertTrue(healed)                                                # restarted by the supervisor
        self.assertTrue(all(c.closed for c in srv.conns))                      # every connection closed


if __name__ == "__main__":
    unittest.main()
