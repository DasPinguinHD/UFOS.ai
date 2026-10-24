"""Market Research MR5 — the drawing split rule (plan §4.2, SPEC §13 "MR5 backend notes"): prices ÷ split
factor for splits after the drawing's price basis, logged (drawing_rescales + audit), idempotent (repeated
checks, action ids recorded), failures change nothing and are reported; the supervised task runs at start,
after a wake-up, and under research priority on the Research pool.

Run from HedgeFund/backend:  python -m unittest tests.test_mr_drawings_split -v
"""
import asyncio
import time
import logging
import threading
import unittest
from datetime import date

try:
    from mr_data_testkit import TempStore, utc
except ImportError:  # pragma: no cover - run as package
    from tests.mr_data_testkit import TempStore, utc

from market_research import drawings as dr, store
from strategy_platform.core.errors import DataSourceUnavailable, SecretMissing
from strategy_platform.data import http_gate

NOW = utc(2026, 10, 15, 15, 0)


def setUpModule():
    logging.getLogger("market_research").setLevel(logging.CRITICAL + 1)


def tearDownModule():
    logging.getLogger("market_research").setLevel(logging.NOTSET)


def split(sym, ex, old, new, kind=None):
    return {"symbol": sym, "kind": kind or ("forward_split" if new > old else "reverse_split"), "ex_date": ex,
            "rate": None, "old_rate": old, "new_rate": new, "process_date": None, "payable_date": None,
            "special": None, "source": "alpaca"}


class FakeSplits:
    def __init__(self, actions=(), fail=None):
        self.actions = list(actions)
        self.fail = fail
        self.calls = []

    def splits(self, symbols, start, end):
        self.calls.append((list(symbols), start, end, http_gate.is_research_priority(),
                           threading.current_thread().name))
        if self.fail:
            raise self.fail
        return [a for a in self.actions if a["symbol"] in symbols and start.isoformat() <= a["ex_date"] <= end.isoformat()]


def make(symbol="AAPL", tool="segment", as_of="2026-06-01", prices=(400.0, 440.0)):
    d = dr.validate_create({"symbol": symbol, "tool": tool,
                            "points": [{"t": f"2026-05-{20 + i}T14:30:00Z", "p": p} for i, p in enumerate(prices)]})
    d["id"] = dr.new_id()
    d["created_at"] = d["updated_at"] = "2026-06-01T14:00:00+00:00"
    d["price_basis"] = {"as_of": as_of, "factor": 1.0, "rescales": 0}
    assert store.drawing_insert(d, 500) == ("created", d["id"])
    return d["id"]


class SplitRuleTest(unittest.TestCase):
    def setUp(self):
        self._ts = TempStore()
        self._ts.__enter__()

    def tearDown(self):
        self._ts.__exit__(None, None, None)

    def test_forward_split_rescales_once(self):
        a = make()
        old = make(as_of="2026-09-01", prices=(110.0, 120.0))          # drawn after the split: untouched
        src = FakeSplits([split("AAPL", "2026-08-03", 1, 4)])
        r = dr.split_check(src, NOW)
        self.assertEqual((r["state"], r["rescaled"], r["symbols"]), ("ok", 1, 1))
        self.assertEqual(src.calls[0][:3], (["AAPL"], date(2026, 6, 2), date(2026, 10, 15)))
        d = store.drawing_get(a)
        self.assertEqual([p["p"] for p in d["points"]], [100.0, 110.0])
        self.assertEqual(d["price_basis"], {"as_of": "2026-08-03", "factor": 4.0, "rescales": 1})
        self.assertEqual([p["p"] for p in store.drawing_get(old)["points"]], [110.0, 120.0])
        log = store.drawing_rescales_list(a)
        self.assertEqual(len(log), 1)
        self.assertEqual((log[0]["action_key"], log[0]["factor"], log[0]["ex_date"]),
                         ("forward_split:2026-08-03:1:4", 4.0, "2026-08-03"))
        self.assertEqual((log[0]["detail"]["before"], log[0]["detail"]["after"]), ([400.0, 440.0], [100.0, 110.0]))
        self.assertTrue(any(x["action"] == "drawing_rescaled" and x["actor"] == "system" for x in store.list_audit()))
        # idempotent: again (same actions), and even with the basis moved back by hand
        for _ in range(3):
            self.assertEqual(dr.split_check(src, NOW)["rescaled"], 0)
        store.drawing_update(a, {}, "2026-08-04T00:00:00+00:00", basis_as_of="2026-06-01")
        self.assertEqual(dr.split_check(src, NOW)["rescaled"], 0)        # action id already recorded
        self.assertEqual([p["p"] for p in store.drawing_get(a)["points"]], [100.0, 110.0])
        st = dr.split_check_status(dr.symbol_of("AAPL"))
        self.assertEqual((st["state"], st["reason"]), ("ok", None))

    def test_reverse_and_multiple_in_order(self):
        a = make(prices=(2.0, 3.0))
        src = FakeSplits([split("AAPL", "2026-09-10", 1, 2), split("AAPL", "2026-07-01", 10, 1),
                          split("MSFT", "2026-07-01", 1, 3)])
        r = dr.split_check(src, NOW)
        self.assertEqual(r["rescaled"], 2)
        d = store.drawing_get(a)
        self.assertEqual([round(p["p"], 9) for p in d["points"]], [10.0, 15.0])     # ×10 then ÷2
        self.assertAlmostEqual(d["price_basis"]["factor"], 0.2)
        self.assertEqual(d["price_basis"]["as_of"], "2026-09-10")
        self.assertEqual([x["ex_date"] for x in store.drawing_rescales_list(a)], ["2026-07-01", "2026-09-10"])

    def test_failure_changes_nothing(self):
        a = make()
        for ex in (SecretMissing("alpaca-paper-key-id", "Alpaca key id"), DataSourceUnavailable("alpaca", "corporate_actions", "timeout")):
            r = dr.split_check(FakeSplits(fail=ex), NOW)
            self.assertEqual(r["state"], "failed")
            self.assertIn("split check could not read", r["reason"])
            self.assertEqual([p["p"] for p in store.drawing_get(a)["points"]], [400.0, 440.0])
            st = dr.list_drawings("AAPL")["split_check"]
            self.assertEqual(st["state"], "failed")
        self.assertEqual(dr.split_check(FakeSplits([split("AAPL", "2026-08-03", 1, 4)]), NOW)["state"], "ok")

    def test_unusable_future_crypto_and_nothing_to_do(self):
        a = make()
        make(symbol="BTC-USD", as_of=None, prices=(60000.0, 61000.0))
        src = FakeSplits([split("AAPL", "2026-08-03", 0, 4), dict(split("AAPL", "2026-08-04", 1, 2), new_rate=None),
                          split("AAPL", "2026-12-01", 1, 2)])
        r = dr.split_check(src, NOW)
        self.assertEqual((r["state"], r["rescaled"]), ("ok", 0))
        self.assertEqual(len(r["skipped"]), 2)
        self.assertIn("without usable rates", r["reason"])
        self.assertEqual(src.calls[0][0], ["AAPL"])                       # crypto never asked
        self.assertEqual([p["p"] for p in store.drawing_get(a)["points"]], [400.0, 440.0])
        self.assertTrue(any(x["action"] == "split_unusable" for x in store.list_audit()))
        store.drawings_delete_symbol("AAPL")
        make(as_of="2026-10-15")
        src2 = FakeSplits()
        self.assertEqual(dr.split_check(src2, NOW)["state"], "ok")
        self.assertEqual(src2.calls, [])                                  # basis = today: no request

    def test_import_keeps_basis_then_rescales(self):
        f = {"version": 1, "symbol": "AAPL", "drawings": [
            {"tool": "priceLine", "points": [{"t": "2026-05-01T14:30:00Z", "p": 800.0}],
             "price_basis": {"as_of": "2026-05-02"}}]}
        res = dr.import_file(f, "AAPL", now=NOW)
        d = res["drawings"][0]
        self.assertEqual(d["price_basis"]["as_of"], "2026-05-02")
        dr.split_check(FakeSplits([split("AAPL", "2026-08-03", 1, 4)]), NOW)
        self.assertEqual(store.drawing_get(d["id"])["points"][0]["p"], 200.0)

    def test_task_runs_at_start_under_research_priority_and_on_wake(self):
        make()
        src = FakeSplits([split("AAPL", "2026-08-03", 1, 4)])
        sleeps = []

        async def main():
            async def fake_sleep(s):
                sleeps.append(s)
                await asyncio.sleep(3600)

            async def until(cond, seconds=30.0):        # S10-H: a deadline, not a fixed number of 10 ms polls
                end = time.monotonic() + seconds          # (the Research pool thread can be slow under load)
                while not cond() and time.monotonic() < end:
                    await asyncio.sleep(0.01)

            task = asyncio.ensure_future(dr.run(lambda: src, sleep=fake_sleep, now=lambda: NOW))
            await until(lambda: bool(src.calls) and bool(sleeps))       # the first check ran and the task waits
            make(as_of="2026-01-02")
            dr.request_check()
            # S10-H: wait for the rescale itself — the source call comes BEFORE the check writes the drawing
            await until(lambda: len(src.calls) >= 2 and sum(1 for d in store.drawings_list("AAPL")
                                                            if d["price_basis"]["rescales"] == 1) == 2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(main())
        self.assertGreaterEqual(len(src.calls), 2)
        self.assertTrue(src.calls[0][3])                                  # research priority
        self.assertTrue(src.calls[0][4].startswith("research"))           # Research pool thread
        self.assertEqual(sleeps[0], 86400.0)
        self.assertEqual(sum(1 for d in store.drawings_list("AAPL") if d["price_basis"]["rescales"] == 1), 2)

    def test_task_retry_after_failure_and_closed_store(self):
        src = FakeSplits(fail=SecretMissing("alpaca-paper-key-id", "Alpaca key id"))
        make()
        sleeps = []

        async def main():
            async def fake_sleep(s):
                sleeps.append(s)
                if len(sleeps) >= 1:
                    raise asyncio.CancelledError

            try:
                await dr.run(lambda: src, sleep=fake_sleep, now=lambda: NOW)
            except asyncio.CancelledError:
                pass

        asyncio.run(main())
        self.assertEqual(sleeps, [3600.0])


if __name__ == "__main__":
    unittest.main()
