"""SPEC §10.1 loading: the limiter never exceeds its concurrency, the HTTP
gate's rate limits hold under parallel load (fake clock), parallel symbol
loading in the backtest engine gives results identical to sequential loading
(deterministic order, same warnings, same first error), corporate-action
chunking (<= 10 symbols, <= 3 years per request) merges to the same rows.
Offline: fakes only.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_network_loading -v
"""
import threading
import time
import unittest
from datetime import date

try:
    from test_sp_backtest_support import DAYS, FakeLoader, series, spec
    from test_sp_data_support import KEYS, Script, fake_gate, fake_sdk
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_backtest_support import DAYS, FakeLoader, series, spec
    from tests.test_sp_data_support import KEYS, Script, fake_gate, fake_sdk

from strategy_platform import secrets
from strategy_platform.backtest import engine
from strategy_platform.core.errors import DataSourceUnavailable
from strategy_platform.data import http_gate, network
from strategy_platform.data.providers import alpaca as A
from strategy_platform.data.providers import tiingo as T
from strategy_platform.data.providers.alpaca import classify_error

START = "2024-02-15"


class LockedClock:
    """Thread-safe fake clock: sleeping advances it (no real waiting). ``lock`` is
    shared with the recorders below so a recorded stamp is the acquisition time."""

    def __init__(self):
        self.lock = threading.RLock()
        self.t = 1000.0

    def monotonic(self):
        with self.lock:
            return self.t

    def sleep(self, s):
        with self.lock:
            self.t += max(float(s), 1e-6)


def max_in_window(stamps, seconds):
    stamps = sorted(stamps)
    best, j = 0, 0
    for i in range(len(stamps)):
        while stamps[i] - stamps[j] >= seconds:
            j += 1
        best = max(best, i - j + 1)
    return best


def run_threads(n, target):
    errors = []

    def wrap():
        try:
            target()
        except BaseException as ex:  # noqa: BLE001 — reported by the test
            errors.append(ex)
    threads = [threading.Thread(target=wrap, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return errors


class LimiterConcurrencyTest(unittest.TestCase):
    def _hammer(self, lim, threads=12, calls=10):
        gate = http_gate.HttpGate("alpaca", rate_per_minute=1e9, capacity=10000, concurrency=lim)
        lock = threading.Lock()
        state = {"active": 0, "peak": 0, "over": 0}

        def fn():
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                if state["active"] > lim.limit:
                    state["over"] += 1
            time.sleep(0.003)
            with lock:
                state["active"] -= 1
            return 1

        errors = run_threads(threads, lambda: [gate.call(fn, data_type="equity_bars", classify=classify_error)
                                               for _ in range(calls)])
        self.assertEqual(errors, [])
        self.assertEqual(lim.in_flight, 0)
        return state

    def test_fixed_limit_never_exceeded(self):
        for fixed in (1, 3):
            with self.subTest(fixed=fixed):
                lim = network.AdaptiveLimiter("alpaca", adaptive=False, fixed=fixed)
                st = self._hammer(lim)
                self.assertLessEqual(st["peak"], fixed)
                self.assertEqual(st["over"], 0)
                self.assertGreaterEqual(st["peak"], 1)

    def test_adaptive_never_exceeds_current_limit(self):
        lim = network.AdaptiveLimiter("alpaca")
        st = self._hammer(lim, threads=12, calls=15)              # 180 successes -> limit grows to 8
        self.assertEqual(st["over"], 0)
        self.assertLessEqual(st["peak"], network.MAX_CONCURRENCY)
        self.assertEqual(lim.limit, network.MAX_CONCURRENCY)


class GateLimitsUnderLoadTest(unittest.TestCase):
    def test_token_bucket_holds_with_8_parallel_callers(self):
        clock = LockedClock()
        lim = network.AdaptiveLimiter("alpaca", adaptive=False, fixed=8)
        gate = http_gate.HttpGate("alpaca", 180, capacity=10, monotonic=clock.monotonic, sleep=clock.sleep,
                                  concurrency=lim)
        stamps = []
        orig = gate.bucket.try_acquire

        def recording():
            with clock.lock:
                wait = orig()
                if wait <= 0:
                    stamps.append(clock.t)
                return wait
        gate.bucket.try_acquire = recording
        errors = run_threads(8, lambda: [gate.call(lambda: 1, data_type="equity_bars", classify=classify_error)
                                         for _ in range(60)])
        self.assertEqual(errors, [])
        self.assertEqual(len(stamps), 480)
        self.assertGreater(stamps[-1] - stamps[0], 60.0)
        self.assertLessEqual(max_in_window(stamps, 60.0), 190)    # 180/min + burst 10
        self.assertLessEqual(lim.peak, 8)

    def test_hourly_window_holds_with_parallel_callers(self):
        clock = LockedClock()
        lim = network.AdaptiveLimiter("tiingo", adaptive=False, fixed=4)
        gate = http_gate.HttpGate("tiingo", 600, capacity=10, windows=((5, 3600.0),), acquire_timeout=4 * 3600,
                                  monotonic=clock.monotonic, sleep=clock.sleep, concurrency=lim)
        stamps = []
        orig = gate.limiter.try_acquire

        def recording():
            with clock.lock:
                wait = orig()
                if wait <= 0:
                    stamps.append(clock.t)
                return wait
        gate.limiter.try_acquire = recording
        errors = run_threads(6, lambda: [gate.call(lambda: 1, data_type="equity_bars_pre2016",
                                                   classify=lambda ex, dt: DataSourceUnavailable("tiingo", dt, "x"))
                                         for _ in range(2)])
        self.assertEqual(errors, [])
        self.assertEqual(len(stamps), 12)
        self.assertLessEqual(max_in_window(stamps, 3600.0), 5)
        self.assertLessEqual(lim.peak, 4)


# --- engine: parallel == sequential ---------------------------------------------------------------

SYMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]


class SlowLoader(FakeLoader):
    """FakeLoader whose LATER symbols answer FASTER (completion order reversed);
    CCC has no Tiingo volume (warning). Tracks concurrent history() calls."""

    def __init__(self, *a, max_workers=None, fail=None, **kw):
        super().__init__(*a, **kw)
        if max_workers is not None:
            self.max_workers = max_workers
        self.fail = fail or {}
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.started = []

    def history(self, symbol, adjustment, start, end=None, *, progress=None):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            if adjustment == "split":
                self.started.append(symbol)
        try:
            order = SYMS.index(symbol) if symbol in SYMS else len(SYMS)
            time.sleep(0.004 * (len(SYMS) + 1 - order))
            if symbol in self.fail:
                delay, err = self.fail[symbol]
                time.sleep(delay)
                raise err
            return super().history(symbol, adjustment, start, end, progress=progress)
        finally:
            with self._lock:
                self.active -= 1

    def volume(self, symbol, start, end=None, *, progress=None):
        if symbol == "CCC":
            raise T.UnknownSymbol(symbol)
        return super().volume(symbol, start, end, progress=progress)


def make_loader(**kw):
    prices = {s: series(40 + 13 * i, drift=0.0005 * (i + 1)) for i, s in enumerate(SYMS)}
    prices["SPY"] = series(400)
    return SlowLoader(prices, dividends={"AAA": [(DAYS[30], 0.5)], "EEE": [(DAYS[40], 0.25)]}, **kw)


SPEC = spec(SYMS, mode="long_short", selection={"type": "threshold", "long_above": 70, "short_below": 60})


class ParallelEngineTest(unittest.TestCase):
    def test_parallel_equals_sequential(self):
        seq_loader = make_loader(max_workers=1)
        par_loader = make_loader(max_workers=4)
        seq = engine.run(SPEC, data=seq_loader, start=START).to_dict()
        par = engine.run(SPEC, data=par_loader, start=START).to_dict()
        self.assertEqual(seq_loader.peak, 1)
        self.assertGreater(par_loader.peak, 1)                    # really ran in parallel
        self.assertEqual(seq, par)
        self.assertIn("no_volume_source", [w["code"] for w in par["warnings"]])
        self.assertTrue(par["fills"])

    def test_default_pool_size_comes_from_network_settings(self):
        network.reset()
        try:
            loader = make_loader()                                # no max_workers attribute
            r = engine.run(SPEC, data=loader, start=START).to_dict()
        finally:
            network.reset()
        self.assertEqual(r, engine.run(SPEC, data=make_loader(max_workers=1), start=START).to_dict())
        self.assertGreater(loader.peak, 1)

    def test_first_error_in_symbol_order_like_sequential(self):
        def fails():
            return {"BBB": (0.15, DataSourceUnavailable("alpaca_sip", "equity_bars_history", "BBB broke")),
                    "DDD": (0.0, DataSourceUnavailable("alpaca_sip", "equity_bars_history", "DDD broke"))}
        for workers in (1, 4):
            with self.subTest(workers=workers):
                loader = make_loader(max_workers=workers, fail=fails())
                with self.assertRaises(DataSourceUnavailable) as cm:
                    engine.run(SPEC, data=loader, start=START)
                self.assertIn("BBB broke", cm.exception.user_message)
        loader = make_loader(max_workers=2, fail=fails())
        with self.assertRaises(DataSourceUnavailable):
            engine.run(SPEC, data=loader, start=START)
        self.assertNotIn("FFF", loader.started)                   # not started after the failure

    def test_cancel_stops_parallel_loading(self):
        with self.assertRaises(engine.BacktestCancelled):
            engine.run(SPEC, data=make_loader(max_workers=4), start=START, cancel=lambda: True)


# --- corporate actions chunking --------------------------------------------------------------------

def master_actions(symbols):
    out = []
    n = 0
    for i, s in enumerate(symbols):
        for year in range(2016, 2027):
            for month in (3, 9):
                n += 1
                out.append(("cash_dividends", {"id": f"d{n}", "symbol": s, "rate": round(0.1 + i * 0.01 + year % 7 * 0.001, 4),
                                               "special": False, "ex_date": f"{year}-{month:02d}-{10 + i % 15:02d}",
                                               "process_date": f"{year}-{month:02d}-28",
                                               "payable_date": f"{year}-{month:02d}-28"}))
        if i % 4 == 0:
            n += 1
            out.append(("forward_splits", {"id": f"s{n}", "symbol": s, "new_rate": 2, "old_rate": 1,
                                           "ex_date": f"{2017 + i % 9}-06-01", "process_date": f"{2017 + i % 9}-06-01"}))
    return out


class ChunkingTest(unittest.TestCase):
    def setUp(self):
        secrets.set_store(secrets.MemoryStore(KEYS))
        self.syms = [f"S{c}{d}" for c in "ABC" for d in "ABCDEFGH"][:23]
        self.master = master_actions(self.syms)
        self.requests = []
        test = self

        class Client:
            def __init__(self, api_key=None, secret_key=None, raw_data=False, **kw):
                self._retry = 3

            def get_corporate_actions(self, request):
                kw = request.kw
                test.requests.append(kw)
                out = {}
                for key, it in test.master:
                    if it["symbol"] in kw["symbols"] and kw["start"].isoformat() <= it["ex_date"] <= \
                            kw["end"].isoformat():
                        out.setdefault(key, []).append(dict(it))
                return out

        sdk = fake_sdk(Script())
        sdk.CorporateActionsClient = Client
        self.gate, _ft = fake_gate()
        self.p = A.AlpacaProvider(gate=self.gate, sdk=sdk)

    def tearDown(self):
        secrets.set_store(None)

    def test_chunked_equals_one_request(self):
        lo, hi = date(2016, 1, 1), date(2026, 10, 9)
        chunked = self.p.corporate_actions(self.syms, lo, hi)
        n_chunked = len(self.requests)
        for kw in self.requests:
            self.assertLessEqual(len(kw["symbols"]), A.CORPORATE_ACTIONS_MAX_SYMBOLS)
            self.assertLessEqual((kw["end"] - kw["start"]).days + 1, A.CORPORATE_ACTIONS_MAX_DAYS)
            self.assertLessEqual((kw["end"] - kw["start"]).days, 3 * 365)
        self.assertEqual(n_chunked, 3 * 4)                        # 23 symbols -> 3 groups; ~10.8 y -> 4 windows
        old = (A.CORPORATE_ACTIONS_MAX_SYMBOLS, A.CORPORATE_ACTIONS_MAX_DAYS)
        A.CORPORATE_ACTIONS_MAX_SYMBOLS, A.CORPORATE_ACTIONS_MAX_DAYS = 10 ** 6, 10 ** 6
        try:
            single = self.p.corporate_actions(self.syms, lo, hi)
        finally:
            A.CORPORATE_ACTIONS_MAX_SYMBOLS, A.CORPORATE_ACTIONS_MAX_DAYS = old
        self.assertEqual(len(self.requests), n_chunked + 1)
        self.assertEqual(chunked, single)
        self.assertEqual(len(chunked), len(self.master))
        self.assertEqual(self.gate.calls, n_chunked + 1)          # every chunk passed the gate

    def test_chunks_partition_the_range(self):
        chunks = A.corporate_action_chunks(["X"] * 25, date(2016, 1, 4), date(2026, 10, 9))
        windows = sorted({w for _g, w in chunks})
        self.assertEqual(windows[0][0], date(2016, 1, 4))
        self.assertEqual(windows[-1][1], date(2026, 10, 9))
        for (a1, b1), (a2, b2) in zip(windows, windows[1:]):
            self.assertEqual((a2 - b1).days, 1)
        self.assertEqual(sorted({len(g) for g, _w in chunks}), [5, 10])
        self.assertEqual(A.corporate_action_chunks(["X"], date(2020, 1, 1), date(2020, 1, 1)),
                         [(["X"], (date(2020, 1, 1), date(2020, 1, 1)))])

    def test_duplicate_ids_across_chunks_once(self):
        dup = ("cash_dividends", dict(self.master[0][1]))
        self.master.append(dup)                                   # the source reports it twice
        rows = self.p.corporate_actions(self.syms[:1], date(2016, 1, 1), date(2026, 10, 9))
        self.assertEqual(len(rows), len([m for m in self.master[:-1] if m[1]["symbol"] == self.syms[0]]))


class LiveFeedPathTest(unittest.TestCase):
    """The live paper engine's calls (latest quote/trade, dividends) through a gate with a limiter."""

    def setUp(self):
        secrets.set_store(secrets.MemoryStore(KEYS))

    def tearDown(self):
        secrets.set_store(None)

    def test_latest_trade_through_limited_gate(self):
        script = Script()
        script.set("get_stock_latest_trade", {"SPY": {"p": 600.5, "s": 10, "t": "2026-10-02T19:59:59Z"}})
        lim = network.AdaptiveLimiter("alpaca", adaptive=False, fixed=1)
        gate, _ft = fake_gate(concurrency=lim)
        p = A.AlpacaProvider(gate=gate, sdk=fake_sdk(script))
        for _ in range(3):
            self.assertEqual(p.latest_trade("SPY")["price"], 600.5)
        self.assertEqual((lim.in_flight, lim.peak, gate.calls), (0, 1, 3))


if __name__ == "__main__":
    unittest.main()
