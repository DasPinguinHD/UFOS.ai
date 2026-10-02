"""24/7 crypto in the HedgeFund engine (fund/SPEC.md §9, §10, §11): the
one-time watchlist rollout (incl. a fresh install), per-symbol tradability
(a "Run now" outside the US session fills crypto and queues equities), no
scheduled full cycles outside the session, the light risk watch, the
per-class daily trade limit and the /fund/state contract fields. Paper
trading only; offline (fakes, temp DB).

Run from HedgeFund/backend:  python -m unittest tests.test_fund_crypto_engine -v
"""
import asyncio
import os
import tempfile
import unittest
from datetime import timedelta

try:
    from crypto_testkit import BTC, ETH, CryptoCase
    from fund_testkit import FRI, MON, SAT, EngineCase
except ImportError:  # pragma: no cover - run as package
    from tests.crypto_testkit import BTC, ETH, CryptoCase
    from tests.fund_testkit import FRI, MON, SAT, EngineCase

from fund import params as P
from fund import store
from fund.engine import FundEngine
from newsroom import db


class RolloutTest(EngineCase):
    def test_adds_crypto_once_with_audit(self):
        v0 = store.effective_params()[0]
        self.assertEqual(self.engine.crypto_rollout(), v0 + 1)
        row = store.latest_params()
        self.assertEqual(row["actor"], "system")
        self.assertEqual(row["note"], "crypto rollout: added BTC-USD, ETH-USD, SOL-USD")
        self.assertEqual(row["params"]["watchlist"], ["SPY", "QQQ", "BTC-USD", "ETH-USD", "SOL-USD"])
        self.assertIsNotNone(store.get_crypto_rollout())
        audit = [a for a in store.list_audit(20) if a["action"] == "crypto_rollout"]
        self.assertEqual([a["actor"] for a in audit], ["system"])
        # one-time: removed again by the user -> not re-added at the next start
        self.engine.update_params({"watchlist": ["SPY"]})
        self.assertIsNone(self.engine.crypto_rollout())
        self.assertEqual(store.effective_params()[1]["watchlist"], ["SPY"])

    def test_respects_the_25_symbol_cap(self):
        wl = [f"S{i}" for i in range(24)]
        store.save_params(P.merge(None, {"watchlist": wl}), "test")
        self.engine.crypto_rollout()
        self.assertEqual(store.effective_params()[1]["watchlist"], wl + ["BTC-USD"])
        self.assertEqual(store.latest_params()["note"], "crypto rollout: added BTC-USD")

    def test_no_new_version_when_a_crypto_symbol_is_present(self):
        store.save_params(P.merge(None, {"watchlist": ["SPY", "ETH-USD"]}), "test")
        v = store.effective_params()[0]
        self.assertIsNone(self.engine.crypto_rollout())
        self.assertEqual(store.effective_params()[0], v)
        self.assertIsNotNone(store.get_crypto_rollout())

    def test_fresh_install_stores_the_crypto_defaults_as_version_1(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            db.close()
            self.assertTrue(db.init(os.path.join(tmp.name, "fresh.db"), legacy=""))
            eng = FundEngine(self.feed, self.broker, store, clock=lambda: self.now, startup_delay=60)
            self.assertEqual(eng.crypto_rollout(), 1)
            row = store.latest_params()
            self.assertEqual((row["version"], row["actor"]), (1, "system"))
            self.assertIn("BTC-USD", row["note"])
            self.assertEqual(store.effective_params()[1], P.defaults())
            self.assertIn("SOL-USD", store.effective_params()[1]["watchlist"])
            self.assertIsNone(eng.crypto_rollout())                   # once
            self.assertEqual(store.effective_params()[0], 1)
        finally:
            db.close()
            tmp.cleanup()
            self.assertTrue(db.init(os.path.join(self._tmp.name, "n.db"), legacy=""))

    def test_start_runs_the_rollout(self):
        async def go():
            other = FundEngine(self.feed, self.broker, store, clock=lambda: self.now, startup_delay=60)
            other.start()
            await other.stop()
        asyncio.run(go())
        self.assertIn("BTC-USD", store.effective_params()[1]["watchlist"])


class CryptoEngineTest(CryptoCase):
    def test_run_now_outside_the_session_fills_crypto_and_queues_equities(self):
        self.now = SAT
        self.set_headlines()
        self.engine.set_autopilot(True)
        out = self.cycle("manual")
        self.assertIn(out["status"], ("ok", "partial"), out)
        orders = {o["symbol"]: o for o in store.list_orders()}
        self.assertEqual((orders[BTC]["status"], orders[BTC]["qty"]), ("filled", 0.054))   # 3 % x 0.9 of 100k
        self.assertEqual(orders["SPY"]["status"], "pending")
        self.assertEqual(orders["QQQ"]["status"], "pending")
        st = self.engine.state()
        eng = st["engine"]
        self.assertEqual(eng["schedule_mode"], "manual_only")
        self.assertIsNone(eng["next_run_at"])
        self.assertEqual(eng["next_session_run_at"], MON.replace(hour=13, minute=30, second=5).isoformat())
        self.assertEqual(eng["next_risk_watch_at"], (SAT + timedelta(minutes=30)).isoformat())
        self.assertEqual((eng["crypto_risk_watch"], eng["risk_watch_interval_minutes"], eng["interval_minutes"]),
                         (True, 30, 5))
        by = {s["symbol"]: s for s in st["symbols"]}
        self.assertEqual((by[BTC]["asset_class"], by[BTC]["tradable_now"]), ("crypto", True))
        self.assertEqual((by["SPY"]["asset_class"], by["SPY"]["tradable_now"]), ("equity", False))
        pos = {p["symbol"]: p for p in st["positions"]}
        self.assertEqual((pos[BTC]["asset_class"], pos[BTC]["instrument"], pos[BTC]["qty"]), ("crypto", "spot", 0.054))
        self.assertEqual(st["account"]["funding_total"], 0.0)
        self.assertIsNone(st["account"]["crypto_margin_excess"])
        user = self.fake.judge_messages[1]["content"]
        self.assertIn('"asset_class": "crypto"', user)
        self.assertIn('"trading_hours": "24/7"', user)
        self.assertIn("24/7", self.fake.judge_messages[0]["content"])
        # Monday: the queued equity orders fill after the opening grace; session schedule again
        self.now = MON
        self.engine.touch_client()                              # the app is watching (dead-man switch)
        self.cycle()
        self.assertGreater(self.qty("SPY"), 0)
        st = self.engine.state()
        self.assertEqual(st["engine"]["schedule_mode"], "session")
        self.assertEqual(st["engine"]["next_run_at"], (MON + timedelta(minutes=5)).isoformat())
        self.assertIsNone(st["engine"]["next_risk_watch_at"])
        self.assertEqual({s["symbol"]: s["tradable_now"] for s in st["symbols"]}["SPY"], True)

    def test_no_scheduled_full_cycles_outside_the_session(self):
        self.now = SAT

        async def wait_runs(n):
            for _ in range(300):
                await asyncio.sleep(0.01)
                if len(store.list_runs(20)) >= n and not self.engine.cycle_in_progress:
                    return

        async def go():
            self.engine.start()
            await wait_runs(1)
            first = store.list_runs(20)
            # a scheduled full cycle that comes due while the market is closed does not run
            self.engine.next_run_at = self.now - timedelta(seconds=1)
            self.engine._event_obj.set()
            await asyncio.sleep(0.2)
            after = store.list_runs(20)
            next_run = self.engine.next_run_at
            self.engine.request_run("user")                     # "Run now" does
            await wait_runs(2)
            await self.engine.stop()
            return first, after, next_run, store.list_runs(20)

        first, after, next_run, final = asyncio.run(go())
        self.assertEqual(len(first), 1)
        self.assertEqual((first[0]["trigger"], first[0]["judge_mode"]), ("startup", "none"))
        self.assertEqual(len(after), 1)
        self.assertEqual(next_run, MON.replace(hour=13, minute=30, second=5))
        self.assertEqual(final[0]["trigger"], "manual")
        self.assertEqual(self.fake.calls.count("judge"), 1)       # only "Run now" used the LLM

    def test_crypto_trades_do_not_use_the_equity_trade_budget(self):
        self.set_params(max_trades_per_day=1)
        self.now = SAT
        self.set_headlines()
        self.engine.set_autopilot(True)
        self.cycle("manual")
        self.assertEqual(sorted((o["symbol"], o["status"]) for o in store.list_orders()),
                         [(BTC, "filled"), ("SPY", "pending")])              # 1 crypto + 1 equity
        recs = {r["symbol"]: r for r in store.latest_recommendations().values()}
        self.assertEqual(recs["QQQ"]["skip_reason"], "daily trade limit")
        # Sunday: a new UTC day for crypto; the equity budget of Monday is already used (SPY)
        self.now = SAT + timedelta(days=1)
        self.feed.quotes[BTC] = 60000.0
        self.engine.update_params({"crypto_max_position_pct": 6.0})
        self.engine.touch_client()
        self.cycle("manual")
        btc = [o for o in store.list_orders() if o["symbol"] == BTC]
        self.assertEqual(len(btc), 2)
        self.assertEqual(len(store.orders_on_utc_day("2026-10-04", "autopilot")), 1)

    def test_risk_watch_needs_crypto_positions_closed_session_and_the_flag(self):
        self.now = SAT
        self.assertEqual(asyncio.run(self.engine.run_risk_watch())["status"], "not_needed")
        self.order(BTC, "buy", qty=0.1)
        self.now = FRI
        self.assertEqual(asyncio.run(self.engine.run_risk_watch())["reason"], "US session open — full cycles run")
        self.now = SAT
        self.set_params(crypto_risk_watch=False)
        self.assertEqual(asyncio.run(self.engine.run_risk_watch())["status"], "not_needed")
        self.assertEqual(store.list_runs(10), [])

    def test_risk_watch_marks_funds_liquidates_without_agents(self):
        self.broker.ensure_initialized(FRI)
        self.order(BTC, "short", qty=0.5)                       # FRI 14:00
        self.order("SPY", "buy", qty=10)
        self.now = SAT
        self.feed.quotes[BTC] = 61000.0
        pend = store.insert_order(self.now, "QQQ", "buy", 5, "pending", "autopilot")
        out = asyncio.run(self.engine.run_risk_watch())
        self.assertEqual(out["status"], "ok", out)
        run = store.get_run(out["run_id"])
        self.assertEqual((run["run"]["trigger"], run["run"]["judge_mode"]), ("risk-watch", "none"))
        self.assertEqual(run["recommendations"], [])
        self.assertEqual(run["agents"], [])
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(run["run"]["summary"]["risk_watch"])
        self.assertEqual(run["run"]["summary"]["margin_orders"], 1)
        self.assertAlmostEqual(sum(r["amount"] for r in self.ledger("funding")), 0.5 * 61000 * 0.0001 * 3)
        self.assertEqual(self.qty(BTC), 0)                      # liquidated
        self.assertEqual(self.qty("SPY"), 10)                   # equities untouched
        self.assertEqual(store.get_order(pend)["status"], "pending")
        self.assertEqual(store.equity_points()[-1]["ts"], SAT.isoformat())
        self.assertEqual(self.engine.next_risk_watch_at, SAT + timedelta(minutes=30))

    def test_risk_watch_drawdown_stop_liquidates(self):
        self.order(BTC, "buy", qty=1.0)
        self.cycle()                                            # peak ~100k
        self.now = SAT
        self.feed.quotes[BTC] = 40000.0                         # -10k on 100k
        out = asyncio.run(self.engine.run_risk_watch())
        self.assertTrue(store.get_halted()["active"])
        self.assertTrue(store.get_run(out["run_id"])["run"]["summary"]["drawdown_stop"])
        self.assertEqual(self.qty(BTC), 0)
        self.assertIn("drawdown-stop", [o["origin"] for o in store.list_orders() if o["status"] == "filled"])

    def test_effective_from_outside_the_session(self):
        self.now = SAT
        self.cycle()
        # no crypto position: no risk watch will run -> the next full cycle (Monday's open)
        res = self.engine.update_params({"max_position_pct": 6.0})
        self.assertEqual(res["effective_from"], MON.replace(hour=13, minute=30, second=5).isoformat())
        self.order(BTC, "buy", qty=0.1)
        res = self.engine.update_params({"max_position_pct": 7.0})
        self.assertEqual(res["effective_from"], (SAT + timedelta(minutes=30)).isoformat())
        self.set_params(crypto_risk_watch=False)
        res = self.engine.update_params({"max_position_pct": 8.0})
        self.assertEqual(res["effective_from"], MON.replace(hour=13, minute=30, second=5).isoformat())

    def test_manual_order_fractional_validation(self):
        from fund.engine import EngineError
        self.now = SAT
        with self.assertRaises(EngineError) as cm:
            asyncio.run(self.engine.manual_order({"symbol": "SPY", "side": "buy", "qty": 1.5}))
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("fractional", cm.exception.errors["qty"])
        r = asyncio.run(self.engine.manual_order({"symbol": "eth-usd", "side": "buy", "qty": 0.25}))
        self.assertEqual((r["symbol"], r["qty"], r["status"]), (ETH, 0.25, "filled"))


if __name__ == "__main__":
    unittest.main()
