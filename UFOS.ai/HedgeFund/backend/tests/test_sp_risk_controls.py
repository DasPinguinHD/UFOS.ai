"""Stage 7 (SPEC §17.1, §17.6, §17.7): post-trade limits (daily loss event, drawdown
kill incl. the stale-mark guard), auto trading + the ONE heartbeat shared with the old
engine (dead-man switch), the kill switch covering BOTH engines, resume. Temp databases,
fake quotes, a real FundEngine on a temp newsroom.db. Paper only."""
import asyncio
import unittest
from datetime import timedelta

try:
    from sp_trading_testkit import SAT, TradingCase
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import SAT, TradingCase

from strategy_platform.core import params as P
from fund import store as fund_store
from strategy_platform.execution.paper_broker import PaperBroker
from fund.engine import FundEngine
from strategy_platform.data.market_feed import FakeFeed
from strategy_platform.execution import control, oms
from strategy_platform.execution.errors import TradingError
from strategy_platform.storage import db


def actions():
    return [a["action"] for a in db.list_audit(200)]


class PostTradeTest(TradingCase):
    def test_drawdown_kill(self):
        self.manual("SPY", "buy", 90)
        self.rt._housekeeping(self.acc, self.clock())
        self.acc.adapter.snapshot_equity(self.clock(), {})          # peak ≈ 100,000
        control.set_autotrade(True, "user")
        self.quotes.prices["SPY"] = 380.0                           # equity ≈ 89,200: drawdown ≈ 10.8 %
        self.advance(minutes=5)
        try:
            self.rt._housekeeping(self.acc, self.clock())
        except TradingError:
            pass
        h = self.acc.halt()
        self.assertTrue(h["active"])
        self.assertEqual(h["kind"], "drawdown")
        self.assertFalse(control.autotrade()["enabled"])
        self.assertEqual(self.acc.store.get_positions(), [])        # closed (session open)
        kills = [o for o in oms.list_orders("paper-main") if o["origin"] == "drawdown"]
        self.assertEqual(len(kills), 1)
        self.assertIn("post_trade_halt", actions())
        # a halted account takes no new risk; resume rebases the peak, auto trading stays off
        acc = control.resume("paper-main", "checked the loss", "user")
        self.assertFalse(acc.halt()["active"])
        self.assertFalse(control.autotrade()["enabled"])
        self.assertAlmostEqual(acc.store.get_peak_equity(), acc.broker.account({}).equity, places=6)

    def test_drawdown_kill_waits_for_live_crypto_marks(self):
        from strategy_platform.core.types import Quote
        self.assertEqual(self.manual("BTC-USD", "buy", 0.6)["state"], "filled")
        self.acc.adapter.snapshot_equity(self.clock(), {})
        self.quotes.prices["BTC-USD"] = 38000.0          # drawdown ≈ 13 %
        stale_ts = (self.clock() - timedelta(hours=2)).isoformat(timespec="seconds")
        self.quotes_obj = self.quotes
        real = self.quotes.__call__
        from strategy_platform.execution import accounts

        def stale(sym):
            q = real(sym)
            return Quote(symbol=q.symbol, price=q.price, prev_close=q.prev_close, ts=stale_ts, stale=True) if q else q
        accounts.configure(quote_fn=stale, clock=self.clock)
        acc = accounts.get("paper-main")
        with self.assertRaises(TradingError) as cm:
            self.rt._housekeeping(acc, self.clock())
        self.assertEqual(cm.exception.code, "stale_marks")
        self.assertFalse(acc.halt()["active"])

    def test_daily_loss_event_once(self):
        self.manual("SPY", "buy", 90)
        self.quotes.prices["SPY"] = 470.0                           # ≈ −2.7 % (< the 10 % kill)
        self.rt._housekeeping(self.acc, self.clock())
        self.rt._housekeeping(self.acc, self.clock())
        evs = [e for e in self.acc.store.events_after(0, 100) if e["title"].startswith("Daily loss limit")]
        self.assertEqual(len(evs), 2)                               # equity class + crypto class (UTC day)
        self.assertEqual(actions().count("post_trade_halt"), 2)
        self.assertFalse(self.acc.halt()["active"])                 # no kill, only the block


class AutotradeHeartbeatTest(TradingCase):
    def test_on_off_and_heartbeat(self):
        st = control.set_autotrade(True, "user")
        self.assertTrue(st["enabled"])
        self.assertIsNotNone(st["off_at"])
        self.advance(minutes=10)
        self.assertFalse(control.check_heartbeat(self.clock()))
        control.touch(self.clock())
        self.advance(minutes=14)
        self.assertFalse(control.check_heartbeat(self.clock()))
        self.advance(minutes=2)
        self.assertTrue(control.check_heartbeat(self.clock()))
        self.assertFalse(control.autotrade()["enabled"])
        self.assertIn("autotrade_heartbeat_off", actions())

    def test_refused_while_halted_and_forced_off_at_start(self):
        self.acc.set_halt(True, "kill", "x")
        with self.assertRaises(TradingError) as cm:
            control.set_autotrade(True, "user")
        self.assertEqual(cm.exception.code, "halted")
        self.acc.set_halt(False)
        control.set_autotrade(True, "user")
        control.force_off_at_start()
        self.assertFalse(control.autotrade()["enabled"])

    def test_off_cancels_queued_runtime_orders(self):
        self.set_now(SAT)
        ts = self.clock().isoformat(timespec="seconds")
        with self.acc.store.atomic():
            with self.acc.store.client_order_id("rt-queued-0001"):
                self.acc.store.insert_order(ts, "SPY", "buy", 1, "pending", "autopilot")
        with db._connect() as conn:
            oms._insert(conn, {"client_order_id": "rt-queued-0001", "account_id": "paper-main", "created_at": ts,
                               "updated_at": ts, "symbol": "SPY", "side": "buy", "qty": 1.0, "origin": "runtime",
                               "tif": "day", "state": "accepted", "filled_qty": 0.0, "fees": 0.0, "attempts": 1,
                               "allow_reduce": 1, "allocations_json": "[]", "checks_json": "[]"})
        control.set_autotrade(True, "user")
        control.set_autotrade(False, "user")
        self.assertEqual(oms.get("rt-queued-0001")["state"], "cancelled")


class BothEnginesTest(TradingCase):
    """The pinned kill switch and the heartbeat cover the old engine AND the platform."""

    def setUp(self):
        super().setUp()
        self.feed = FakeFeed(bars={}, quotes={"SPY": 500.0, "QQQ": 400.0})
        fund_store.save_params(P.merge(None, {"watchlist": ["SPY", "QQQ"]}), "test")
        self.lbroker = PaperBroker(fund_store, self.feed.quote, clock=self.clock)
        self.engine = FundEngine(self.feed, self.lbroker, fund_store, clock=self.clock, startup_delay=0.0)
        self.engine.platform_kill_fn = control.kill_platform
        control.set_legacy_engine(self.engine)
        self.lbroker.ensure_initialized(self.clock())

    def test_old_kill_switch_also_kills_the_platform(self):
        self.manual("SPY", "buy", 10)
        out = asyncio.run(self.engine.kill("pinned button"))
        self.assertTrue(out["halted"])
        self.assertTrue(out["platform"]["ok"])
        self.assertTrue(self.acc.halt()["active"])
        self.assertEqual(self.acc.store.get_positions(), [])
        self.assertTrue(fund_store.get_halted()["active"])

    def test_v2_kill_also_kills_the_old_engine(self):
        out = control.kill_all("v2 button", "user")
        self.assertTrue(out["platform"]["ok"])
        self.assertTrue(out["legacy"]["ok"])
        self.assertTrue(out["legacy"]["halted"])
        self.assertTrue(fund_store.get_halted()["active"])
        self.assertTrue(self.acc.halt()["active"])
        self.assertIn("platform_kill", actions())

    def test_one_heartbeat(self):
        self.engine.touch_client(self.clock())                       # the old window polls /fund/state
        control.set_autotrade(True, "user")
        self.advance(minutes=14)
        self.engine.touch_client(self.clock())                       # counts for the platform as well
        self.advance(minutes=14)
        self.assertFalse(control.check_heartbeat(self.clock()))
        control.touch(self.clock())                                  # a platform GET touches the old engine
        self.assertEqual(self.engine.last_client_seen, self.clock())

    def test_each_half_reports_its_own_failure(self):
        def boom(reason, actor):
            raise RuntimeError("platform down")
        self.engine.platform_kill_fn = boom
        out = asyncio.run(self.engine.kill("x"))
        self.assertTrue(out["halted"])
        self.assertFalse(out["platform"]["ok"])
        self.assertIn("platform down", out["platform"]["error"])


if __name__ == "__main__":
    unittest.main()
