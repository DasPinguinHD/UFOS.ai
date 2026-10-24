"""Stage 7 (SPEC §17.4, §17.5): allocator math (inverse volatility, correlation penalty,
caps, minimum weight, total), capital transfers, netting (internal crosses, legs, the
participation cap), the sub-ledger split rules and the attribution invariant (Σ sources =
account) incl. accruals pro rata by signed quantity. Offline; temp databases."""
import math
import unittest
from datetime import date, timedelta

try:
    from sp_trading_testkit import FRI, TradingCase
except ImportError:  # pragma: no cover
    from tests.sp_trading_testkit import FRI, TradingCase

from strategy_platform.execution import oms
from strategy_platform.portfolio import allocator, ledger, netting
from strategy_platform.storage import db

P = {"alloc_max_weight": 1.0, "alloc_min_weight": 0.0, "alloc_max_total": 1.0, "alloc_lambda": 0.0,
     "alloc_drift_band": 0.05}


def series(n, scale, phase=0, seed=1):
    out = []
    for i in range(n):
        x = math.sin(i * 0.7 + phase) * scale + ((i * seed * 37) % 11 - 5) * scale / 50.0
        out.append(((date(2026, 1, 1) + timedelta(days=i)).isoformat(), x))
    return out


def src(sid, led=None, hold=None):
    return {"source_id": sid, "name": sid, "calendar": "nyse", "ledger_returns": led or [], "holdout_returns": hold}


class AllocatorTest(unittest.TestCase):
    def test_inverse_volatility(self):
        a, b = series(60, 0.01), series(60, 0.02, phase=1.3)
        w = {r["source_id"]: r for r in allocator.weights([src("a", a), src("b", b)], P)}
        va, vb = w["a"]["vol"], w["b"]["vol"]
        self.assertAlmostEqual(w["a"]["weight"] / w["b"]["weight"], vb / va, places=9)
        self.assertAlmostEqual(w["a"]["weight"] + w["b"]["weight"], 1.0, places=12)
        self.assertEqual(w["a"]["vol_source"], "ledger")

    def test_correlation_penalty(self):
        a = series(80, 0.01)
        b = [(d, r * 1.0) for d, r in a]                 # perfectly correlated twin of a
        c = series(80, 0.01, phase=2.0, seed=3)          # same vol, other path
        rows = {r["source_id"]: r for r in allocator.weights([src("a", a), src("b", b), src("c", c)],
                                                              dict(P, alloc_lambda=1.0))}
        self.assertAlmostEqual(rows["a"]["mean_corr"], (1.0 + max(0.0, allocator.corr(
            [x for _d, x in a][-120:], [x for _d, x in c][-120:]))) / 2, places=9)
        self.assertGreater(rows["c"]["weight"], rows["a"]["weight"])
        self.assertAlmostEqual(rows["a"]["penalty"], 1.0 + rows["a"]["mean_corr"], places=12)

    def test_caps_min_weight_total_and_no_estimate(self):
        a, b, c = series(60, 0.005), series(60, 0.02, 1), series(60, 0.03, 2)
        rows = {r["source_id"]: r for r in allocator.weights(
            [src("a", a), src("b", b), src("c", c), src("d", series(5, 0.01))],
            dict(P, alloc_max_weight=0.4, alloc_min_weight=0.2, alloc_max_total=0.9))}
        self.assertTrue(rows["a"]["capped"])
        self.assertAlmostEqual(rows["a"]["weight"], 0.4 * 0.9, places=12)       # excess NOT redistributed
        self.assertEqual(rows["d"]["weight"], 0.0)
        self.assertIn("no volatility estimate", rows["d"]["note"])
        self.assertLessEqual(sum(r["weight"] for r in rows.values()), 0.9 + 1e-12)
        small = [r for r in rows.values() if r["note"] and "below the minimum" in r["note"]]
        self.assertTrue(all(r["weight"] == 0.0 for r in small))

    def test_floor_removing_every_strategy_is_no_allocation(self):
        # review fix L2 (2026-10-18): explicit "no allocation" with a reason, never silent zeros
        three = [src("a", series(60, 0.01)), src("b", series(60, 0.01, 1, 2)), src("c", series(60, 0.01, 2, 3))]
        res = allocator.weights_result(three, dict(P, alloc_min_weight=0.5))
        self.assertEqual(res["status"], "no_allocation")
        self.assertIn("minimum weight 0.5", res["reason"])
        self.assertTrue(all(r["weight"] == 0.0 and "no allocation was made" in r["note"] for r in res["weights"]))
        none = allocator.weights_result([src("a", series(5, 0.01))], P)
        self.assertEqual(none["status"], "no_allocation")
        self.assertIn("no strategy has a volatility estimate", none["reason"])
        ok = allocator.weights_result(three, dict(P, alloc_min_weight=0.05))
        self.assertEqual((ok["status"], ok["reason"]), ("ok", None))
        self.assertEqual(allocator.weights_result([], P)["status"], "ok")      # nothing managed: nothing to say

    def test_holdout_returns_define_young_strategies(self):
        rows = allocator.weights([src("a", series(5, 0.01), series(60, 0.01))], P)
        self.assertEqual(rows[0]["vol_source"], "holdout")
        self.assertAlmostEqual(rows[0]["weight"], 1.0)

    def test_plan_transfers(self):
        moves = allocator.plan_transfers(100000.0, {"a": 40000.0, "b": 40000.0}, {"a": 50000.0, "b": 0.0}, 50000.0, 0.05)
        self.assertEqual(moves, [("a", -10000.0), ("b", 40000.0)])
        moves = allocator.plan_transfers(100000.0, {"a": 41000.0}, {"a": 40000.0}, 60000.0, 0.05)
        self.assertEqual(moves, [])                                              # inside the drift band
        moves = allocator.plan_transfers(100000.0, {"a": 60000.0}, {"a": 0.0}, 30000.0, 0.05)
        self.assertEqual(moves, [("a", 30000.0)])                                # limited by the cash


class NettingTest(unittest.TestCase):
    def test_internal_cross_and_residual(self):
        out = netting.net({"SPY": {"a": 100.0, "b": -30.0}}, {"SPY": 0.0})
        self.assertEqual(out["crosses"], [{"symbol": "SPY", "legs": [("a", 30.0), ("b", -30.0)]}])
        self.assertEqual(out["orders"][0]["legs"], [{"side": "buy", "qty": 70.0, "allocations": [("a", 70.0)]}])

    def test_fully_crossed_means_no_order(self):
        out = netting.net({"SPY": {"a": 40.0, "b": -40.0}}, {"SPY": 10.0})
        self.assertEqual(out["orders"], [])
        self.assertEqual(len(out["crosses"]), 1)

    def test_flip_two_legs_and_reductions_first(self):
        out = netting.net({"SPY": {"a": -80.0}, "QQQ": {"a": -5.0}}, {"SPY": 50.0, "QQQ": 20.0})
        self.assertEqual([o["symbol"] for o in out["orders"]], ["QQQ", "SPY"])   # QQQ reduces -> first
        spy = out["orders"][1]
        self.assertEqual([(l["side"], l["qty"]) for l in spy["legs"]], [("sell", 50.0), ("short", 30.0)])

    def test_participation_cap_scales_allocations(self):
        out = netting.net({"SPY": {"a": 30.0, "b": 20.0}}, {}, {"SPY": 10.0})
        o = out["orders"][0]
        self.assertTrue(o["capped"])
        self.assertEqual(o["legs"][0]["allocations"], [("a", 6.0), ("b", 4.0)])

    def test_split_rules(self):
        self.assertEqual(ledger.split_qty("SPY", 10, [("a", 1), ("b", 1), ("c", 1)]),
                         [("a", 4.0), ("b", 3.0), ("c", 3.0)])
        self.assertAlmostEqual(sum(q for _s, q in ledger.split_qty("BTC-USD", 0.3, [("a", 1), ("b", 2)])), 0.3, places=8)
        parts = ledger.split_amount(-10.0, [("a", 1), ("b", 2)])
        self.assertEqual(math.fsum(v for _s, v in parts), -10.0)


class AttributionTest(TradingCase):
    def assert_invariant(self):
        st = ledger.all_states(self.acc.id)
        cash = math.fsum(v["cash"] for v in st.values())
        self.assertAlmostEqual(cash, self.acc.store.cash_balance(), places=6)
        hold = ledger.holdings(self.acc.id)
        for p in self.acc.store.get_positions():
            self.assertAlmostEqual(sum(q.get(p["symbol"], 0.0) for q in hold.values()), p["qty"], places=8)

    def test_fills_by_allocation_and_accruals_by_signed_qty(self):
        with db._connect() as conn:
            ledger.insert_entries(conn, "paper-main", [
                {"source_id": "_unallocated", "kind": "capital", "amount": -50000.0},
                {"source_id": "s-a", "kind": "capital", "amount": 30000.0},
                {"source_id": "s-b", "kind": "capital", "amount": 20000.0}])
        o, _ = oms.place(self.acc, {"symbol": "SPY", "side": "buy", "qty": 70, "origin": "runtime",
                                    "allocations": [("s-a", 50), ("s-b", 20)]})
        self.assertEqual(o["state"], "filled")
        ledger.attribute(self.acc)
        self.assertEqual(ledger.holdings(self.acc.id), {"s-a": {"SPY": 50.0}, "s-b": {"SPY": 20.0}})
        self.assert_invariant()
        # internal cross: b sells 30 to a (b goes short internally) — the account is untouched
        with db._connect() as conn:
            ledger.insert_entries(conn, "paper-main", [
                {"source_id": "s-a", "kind": "cross", "symbol": "SPY", "qty": 30.0, "price": 500.0, "amount": -15000.0},
                {"source_id": "s-b", "kind": "cross", "symbol": "SPY", "qty": -30.0, "price": 500.0, "amount": 15000.0}])
        # a dividend on the account's 70 shares: per share × signed quantity (a +80, b −10)
        self.acc.store.add_cash_entry(FRI, "dividend", 70 * 1.5, symbol="SPY", note="ex 2026-10-01: 70 x 1.5")
        ledger.attribute(self.acc)
        rows = [r for r in ledger.entries("paper-main") if r["kind"] == "dividend"]
        by = {r["source_id"]: r["amount"] for r in rows}
        self.assertAlmostEqual(by["s-a"], 80 * 1.5, places=9)
        self.assertAlmostEqual(by["s-b"], -10 * 1.5, places=9)
        self.assert_invariant()
        # attributed exactly once
        self.assertEqual(ledger.attribute(self.acc), 0)

    def test_orders_without_allocations_follow_the_holdings(self):
        with db._connect() as conn:
            ledger.insert_entries(conn, "paper-main", [
                {"source_id": "_unallocated", "kind": "capital", "amount": -40000.0},
                {"source_id": "s-a", "kind": "capital", "amount": 40000.0}])
        oms.place(self.acc, {"symbol": "SPY", "side": "buy", "qty": 40, "origin": "runtime",
                             "allocations": [("s-a", 40)]})
        oms.place(self.acc, {"symbol": "SPY", "side": "sell", "qty": 40, "origin": "kill"})
        ledger.attribute(self.acc)
        self.assertEqual(ledger.holdings(self.acc.id), {})
        self.assert_invariant()
        st = ledger.source_state("paper-main", "s-a")
        self.assertLess(st["realized"], 0)                 # two fees + the spread
        self.assertAlmostEqual(st["costs"]["fees"], -math.fsum(r["amount"] for r in ledger.entries("paper-main", "s-a")
                                                               if r["kind"] == "fee"), places=9)


class NoAllocationRebalanceTest(TradingCase):
    def test_no_capital_moves_and_the_reason_is_shown(self):
        from strategy_platform.portfolio import allocation, settings as pf_settings
        fake = [{"source_id": f"s-00000000000{i}", "strategy_id": f"s-00000000000{i}", "version": 1,
                 "name": f"S{i}", "calendar": "nyse", "run_id": f"inc-00000000000{i}",
                 "holdout_returns": series(60, 0.01, i, i + 1)} for i in range(3)]
        real = allocation.managed_sources
        allocation.managed_sources = lambda account: fake
        try:
            cur = pf_settings.values(self.acc.id)
            pf_settings.write(self.acc.id, dict(cur, alloc_min_weight=0.5), "user", "test floor")
            out = allocation.rebalance(self.acc, "initial allocation", "engine")
        finally:
            allocation.managed_sources = real
        a = out["allocation"]
        self.assertEqual(a["status"], "no_allocation")
        self.assertIn("lower the minimum weight", a["status_reason"])
        self.assertEqual(a["transfers"], [])
        self.assertIsNone(a["unallocated_weight"])
        st = ledger.all_states(self.acc.id)
        self.assertEqual(set(st), {"_unallocated"})                 # no capital entry for any strategy


if __name__ == "__main__":
    unittest.main()
