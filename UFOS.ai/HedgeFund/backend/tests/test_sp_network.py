"""strategy_platform/data/network.py (SPEC §10.1): settings in hedgefund.db
meta, AIMD limiter transitions, fixed mode, connection-error kinds for typical
transport exceptions (requests / urllib3 / aiohttp shapes, socket, ssl), the
``kind == "connection"`` marking by gate + registry, probe spacing. Offline:
fakes, temp DB, no network, no real keys.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_network -v
"""
import socket
import ssl
import threading
import unittest
import urllib.error

try:
    from test_sp_data_support import KEYS, FakeAPIError, Script, TempDb, fake_gate, fake_sdk
except ImportError:  # pragma: no cover - run as package
    from tests.test_sp_data_support import KEYS, FakeAPIError, Script, TempDb, fake_gate, fake_sdk

from strategy_platform import secrets
from strategy_platform.core.errors import DataSourceUnavailable, InvalidRequest
from strategy_platform.data import http_gate, network, registry
from strategy_platform.data.providers import alpaca as A
from strategy_platform.data.providers import tiingo as T
from strategy_platform.data.providers.base import SourceAuthRejected


# --- shapes of third-party exceptions (same class names / bases as the real packages) -------------

class RequestException(OSError):                       # requests.exceptions.RequestException(IOError)
    def __init__(self, *a, response=None, **kw):
        super().__init__(*a)
        self.response = response


class R_ConnectionError(RequestException):
    pass


class R_Timeout(RequestException):
    pass


class ConnectTimeout(R_ConnectionError, R_Timeout):
    pass


class ReadTimeout(R_Timeout):
    pass


class R_SSLError(R_ConnectionError):
    pass


class R_HTTPError(RequestException):
    pass


R_ConnectionError.__name__ = "ConnectionError"
R_Timeout.__name__ = "Timeout"
R_SSLError.__name__ = "SSLError"
R_HTTPError.__name__ = "HTTPError"


class ClientError(Exception):                          # aiohttp
    pass


class ClientConnectionError(ClientError):
    pass


class ClientOSError(ClientConnectionError, OSError):
    pass


class ClientConnectorError(ClientOSError):
    pass


class ServerDisconnectedError(ClientConnectionError):
    pass


class ClientResponseError(ClientError):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class Response:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}


def connection_examples():
    import errno
    return {
        "requests ConnectTimeout": ConnectTimeout("connect timed out"),
        "requests ReadTimeout": ReadTimeout("read timed out"),
        "requests ConnectionError": R_ConnectionError("Max retries exceeded"),
        "requests SSLError": R_SSLError("handshake failure"),
        "socket.gaierror (DNS)": socket.gaierror(11001, "getaddrinfo failed"),
        "socket.timeout": socket.timeout("timed out"),
        "TimeoutError": TimeoutError(),
        "ConnectionResetError": ConnectionResetError(10054, "reset by peer"),
        "ConnectionRefusedError": ConnectionRefusedError(),
        "OSError ENETUNREACH": OSError(errno.ENETUNREACH, "Network is unreachable"),
        "OSError WSAETIMEDOUT": OSError(10060, "timed out"),
        "ssl.SSLError": ssl.SSLError("handshake"),
        "ssl.SSLEOFError": ssl.SSLEOFError(),
        "ssl.SSLCertVerificationError": ssl.SSLCertVerificationError("cert"),
        "URLError(gaierror)": urllib.error.URLError(socket.gaierror(-2, "Name or service not known")),
        "URLError(timeout)": urllib.error.URLError(socket.timeout("timed out")),
        "aiohttp ClientConnectorError": ClientConnectorError("Cannot connect to host"),
        "aiohttp ServerDisconnectedError": ServerDisconnectedError(),
        "wrapped in args": R_ConnectionError(ConnectionResetError("reset")),
    }


def non_connection_examples():
    return {
        "Alpaca APIError 500": FakeAPIError(500),
        "Alpaca APIError 401": FakeAPIError(401),
        "urllib HTTPError 503": urllib.error.HTTPError("https://x", 503, "unavailable", {}, None),
        "requests HTTPError": R_HTTPError("500 Server Error", response=Response(500)),
        "aiohttp ClientResponseError": ClientResponseError(502),
        "ValueError": ValueError("bad json"),
        "KeyError": KeyError("t"),
        "plain OSError": OSError("something"),
        "DataSourceUnavailable unmarked": DataSourceUnavailable("alpaca", "equity_bars", "x"),
    }


class ClassificationTest(unittest.TestCase):
    def test_connection_examples(self):
        for name, ex in connection_examples().items():
            with self.subTest(name):
                self.assertTrue(network.is_connection_error(ex))
                self.assertEqual(network.error_kind(ex), "connection")

    def test_non_connection_examples(self):
        for name, ex in non_connection_examples().items():
            with self.subTest(name):
                self.assertFalse(network.is_connection_error(ex))
                self.assertIsNone(network.error_kind(ex))

    def test_real_requests_exceptions_when_installed(self):
        try:
            import requests
        except ImportError:
            self.skipTest("requests not installed (stdlib-only test environment)")
        E = requests.exceptions
        for ex in (E.ConnectTimeout(), E.ReadTimeout(), E.ConnectionError(), E.SSLError()):
            self.assertTrue(network.is_connection_error(ex), type(ex).__name__)
        resp = requests.models.Response()
        resp.status_code = 500
        self.assertFalse(network.is_connection_error(E.HTTPError(response=resp)))

    def test_marking(self):
        err = DataSourceUnavailable("alpaca", "equity_bars", "x")
        self.assertIsNone(getattr(err, "kind", None))
        self.assertIs(network.mark_connection(err), err)
        self.assertEqual(getattr(err, "kind", None), "connection")
        self.assertTrue(network.is_connection_error(err))
        self.assertEqual(network.error_kind(err), "connection")


class GateMarkingTest(unittest.TestCase):
    """http_gate marks connection-caused DataSourceUnavailable (kind attribute) and feeds the limiter."""

    def setUp(self):
        secrets.set_store(secrets.MemoryStore(dict(KEYS, **{secrets.target("tiingo-api-key"): "tk-123"})))
        self.script = Script()
        self.lim = network.AdaptiveLimiter("alpaca")
        self.gate, self.ft = fake_gate(concurrency=self.lim)
        self.p = A.AlpacaProvider(gate=self.gate, sdk=fake_sdk(self.script))

    def tearDown(self):
        secrets.set_store(None)

    def test_timeouts_exhausted_are_connection_and_halve(self):
        self.script.set("get_stock_latest_trade", ReadTimeout("read timed out"))
        with self.assertRaises(DataSourceUnavailable) as cm:
            self.p.latest_trade("SPY")
        self.assertEqual(getattr(cm.exception, "kind", None), "connection")
        self.assertIn("gave up after 3 attempts", cm.exception.user_message)
        self.assertEqual(self.lim.limit, 1)                       # 4 -> 2 -> 1 -> 1
        self.assertEqual(self.lim.in_flight, 0)

    def test_network_error_at_once_is_connection(self):
        for ex in (R_ConnectionError("x"), socket.gaierror(-2, "dns"), ssl.SSLError("tls"),
                   ClientConnectorError("x")):
            with self.subTest(type(ex).__name__):
                self.script.set("get_stock_latest_trade", ex)
                with self.assertRaises(DataSourceUnavailable) as cm:
                    self.p.latest_trade("SPY")
                self.assertEqual(getattr(cm.exception, "kind", None), "connection")

    def test_http_and_auth_errors_are_not_connection(self):
        for ex, cls in ((FakeAPIError(500), DataSourceUnavailable), (FakeAPIError(429), DataSourceUnavailable),
                        (FakeAPIError(401), SourceAuthRejected), (FakeAPIError(422), DataSourceUnavailable),
                        (ValueError("x"), DataSourceUnavailable)):
            with self.subTest(repr(ex)):
                self.script.set("get_stock_latest_trade", ex)
                with self.assertRaises(cls) as cm:
                    self.p.latest_trade("SPY")
                self.assertIsNone(getattr(cm.exception, "kind", None))
        self.assertEqual(self.lim.limit, 4)                       # other failures change nothing

    def test_tiingo_dns_failure_is_connection(self):
        def opener(req, timeout=None, context=None):
            raise urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
        gate, _ft = fake_gate("tiingo", windows=((40, 3600.0),))
        p = T.TiingoProvider(gate=gate, urlopen=opener)
        with self.assertRaises(DataSourceUnavailable) as cm:
            p.daily_prices("SPY", "2010-01-01", "2010-02-01")
        self.assertEqual(getattr(cm.exception, "kind", None), "connection")
        self.assertNotIn("tk-123", str(cm.exception))

    def test_tiingo_http_429_is_not_connection(self):
        def opener(req, timeout=None, context=None):
            raise urllib.error.HTTPError("https://api.tiingo.com", 429, "Too Many", {}, None)
        gate, _ft = fake_gate("tiingo", windows=((40, 3600.0),))
        p = T.TiingoProvider(gate=gate, urlopen=opener)
        with self.assertRaises(DataSourceUnavailable) as cm:
            p.daily_prices("SPY", "2010-01-01", "2010-02-01")
        self.assertIsNone(getattr(cm.exception, "kind", None))

    def test_registry_marks_unexpected_connection_errors(self):
        class Raw:
            name = "alpaca"
            data_types = A.DATA_TYPES

            def latest_trade(self, symbol):
                raise ConnectionResetError(10054, "reset")      # outside any gate

        with TempDb():
            reg = registry.Registry(providers={"alpaca": Raw})
            with self.assertRaises(DataSourceUnavailable) as cm:
                reg.fetch("equity_quotes", "latest_trade", "SPY")
        self.assertEqual(getattr(cm.exception, "kind", None), "connection")


class LimiterTest(unittest.TestCase):
    def test_aimd_transitions(self):
        lim = network.AdaptiveLimiter("alpaca")
        self.assertEqual(lim.limit, network.START_CONCURRENCY)
        lim.record_connection_error()
        self.assertEqual(lim.limit, 2)
        lim.record_connection_error()
        lim.record_connection_error()
        self.assertEqual(lim.limit, 1)                            # floor 1
        for _ in range(19):
            lim.record_success()
        self.assertEqual(lim.limit, 1)
        lim.record_success()
        self.assertEqual(lim.limit, 2)                            # 20 consecutive -> +1
        for _ in range(19):
            lim.record_success()
        lim.record_connection_error()                             # streak broken and halved
        self.assertEqual(lim.limit, 1)
        for _ in range(20 * 10):
            lim.record_success()
        self.assertEqual(lim.limit, network.MAX_CONCURRENCY)      # capped at 8
        lim.record_failure()                                      # HTTP/auth/data errors: no change
        self.assertEqual(lim.limit, 8)
        lim.record_connection_error()
        self.assertEqual(lim.limit, 4)

    def test_fixed_mode_is_constant(self):
        lim = network.AdaptiveLimiter("tiingo", adaptive=False, fixed=3)
        self.assertEqual(lim.limit, 3)
        for _ in range(5):
            lim.record_connection_error()
        for _ in range(100):
            lim.record_success()
        self.assertEqual(lim.limit, 3)
        lim.configure(True, 3)                                    # adaptive restarts at 4
        self.assertEqual(lim.limit, 4)
        lim.record_connection_error()
        lim.configure(True, 6)                                    # already adaptive: unchanged
        self.assertEqual(lim.limit, 2)
        lim.configure(False, 6)
        self.assertEqual(lim.limit, 6)

    def test_lowered_limit_blocks_new_slots_until_enough_released(self):
        lim = network.AdaptiveLimiter("alpaca")
        for _ in range(4):
            lim.acquire()
        lim.record_connection_error()                             # limit 2, 4 in flight
        got = threading.Event()

        def taker():
            lim.acquire()
            got.set()

        t = threading.Thread(target=taker, daemon=True)
        t.start()
        lim.release()
        lim.release()                                             # 2 in flight = limit
        self.assertFalse(got.wait(0.2))
        lim.release()                                             # 1 in flight -> a slot
        self.assertTrue(got.wait(2.0))
        t.join(2.0)
        self.assertEqual(lim.in_flight, 2)

    def test_waiting_calls_progress_and_can_cancel(self):
        lim = network.AdaptiveLimiter("alpaca", adaptive=False, fixed=1)
        lim.acquire()
        events = []

        class Stop(Exception):
            pass

        def progress(ev):
            events.append(ev)
            raise Stop()

        old = network.SLOT_PROGRESS_INTERVAL_SECONDS
        network.SLOT_PROGRESS_INTERVAL_SECONDS = 0.01
        try:
            with self.assertRaises(Stop):
                lim.acquire(progress)
        finally:
            network.SLOT_PROGRESS_INTERVAL_SECONDS = old
        self.assertEqual(lim.in_flight, 1)
        self.assertNotIn("message", events[0])
        self.assertTrue(events[0]["waiting"])

    def test_invalid_limiter_arguments(self):
        with self.assertRaises(ValueError):
            network.AdaptiveLimiter("alpaca", fixed=9)
        with self.assertRaises(ValueError):
            network.limiter_for("fred")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        network.reset()
        http_gate.set_gate("alpaca", None)
        http_gate.set_gate("tiingo", None)

    def tearDown(self):
        network.reset()
        http_gate.set_gate("alpaca", None)
        http_gate.set_gate("tiingo", None)

    def test_defaults_set_persist_and_apply(self):
        with TempDb() as db:
            s = network.get_settings()
            self.assertEqual(s, {"adaptive": True, "fixed": 4, "current": {"alpaca": 4, "tiingo": 4},
                                 "min": 1, "max": 8})
            s = network.set_settings(False, 2)
            self.assertEqual((s["adaptive"], s["fixed"], s["current"]), (False, 2, {"alpaca": 2, "tiingo": 2}))
            self.assertEqual((db.meta_get("network.adaptive"), db.meta_get("network.fixed")), ("false", "2"))
            self.assertIs(http_gate.gate_for("alpaca").concurrency, network.limiter_for("alpaca"))
            self.assertEqual(network.pool_size(), 2)
            network.reset()                                       # "restart": loaded from meta again
            self.assertEqual(network.limiter_for("tiingo").limit, 4)
            network.ensure_loaded()
            self.assertEqual(network.limiter_for("tiingo").limit, 2)
            self.assertEqual(network.get_settings()["current"], {"alpaca": 2, "tiingo": 2})
            s = network.set_settings(True, 7)
            self.assertEqual((s["adaptive"], s["fixed"], s["current"]), (True, 7, {"alpaca": 4, "tiingo": 4}))
            self.assertEqual(network.pool_size(), network.MAX_CONCURRENCY)

    def test_validation(self):
        with TempDb():
            for adaptive, fixed in ((1, 4), ("true", 4), (True, 0), (True, 9), (True, True), (True, 4.0),
                                    (None, 4), (False, "4")):
                with self.subTest((adaptive, fixed)):
                    with self.assertRaises(network.InvalidNetworkSettings) as cm:
                        network.set_settings(adaptive, fixed)
                    self.assertIsInstance(cm.exception, InvalidRequest)
                    self.assertIsInstance(cm.exception, ValueError)
            self.assertEqual(network.get_settings()["fixed"], 4)  # nothing stored

    def test_invalid_stored_value_get_reports_requests_fail(self):
        with TempDb() as db:
            db.meta_set("network.adaptive", "yes")
            s = network.get_settings()                            # no exception: the UI can show it
            self.assertEqual((s["stored_invalid"], s["adaptive"], s["fixed"]), (True, None, 4))
            self.assertEqual(s["stored"], {"network.adaptive": "yes", "network.fixed": None})
            self.assertIn("Hedge Fund → Keys & data → Network", s["error"])
            self.assertEqual((s["min"], s["max"], set(s["current"])), (1, 8, {"alpaca", "tiingo"}))
            # every data request stops with the clear message (no silent default)
            with self.assertRaises(network.StoredSettingsInvalid) as cm:
                network.limiter_for("alpaca").acquire()
            self.assertIn("Hedge Fund → Keys & data → Network", cm.exception.user_message)
            self.assertEqual(network.limiter_for("alpaca").in_flight, 0)
            with self.assertRaises(network.StoredSettingsInvalid):
                network.pool_size()
            db.meta_set("network.fixed", "12\x07" + "9" * 60)
            s = network.get_settings()
            self.assertEqual(s["fixed"], None)
            self.assertNotIn("\x07", s["stored"]["network.fixed"])
            self.assertLessEqual(len(s["stored"]["network.fixed"]), 41)
            # repair: fixed None cannot keep an invalid stored value; explicit values store and apply
            with self.assertRaises(network.InvalidNetworkSettings):
                network.set_settings(True)
            with self.assertRaises(network.InvalidNetworkSettings):
                network.set_settings("yes", 3)                    # only the NEW input is validated
            s = network.set_settings(False, 3)
            self.assertEqual((s["adaptive"], s["fixed"], s["current"]), (False, 3, {"alpaca": 3, "tiingo": 3}))
            self.assertNotIn("stored_invalid", s)
            network.limiter_for("alpaca").acquire()               # requests work again
            network.limiter_for("alpaca").release()
            self.assertEqual(network.set_settings(True)["fixed"], 3)   # fixed None keeps a valid stored value
            self.assertNotIn("stored_invalid", network.get_settings())

    def test_put_route_repairs_invalid_store(self):
        import asyncio
        from strategy_platform.api import routes

        def call(name, body=routes.NO_BODY):
            return asyncio.run(getattr(routes.V2Api(), name)({}, body, {}))

        with TempDb() as db:
            db.meta_set("network.adaptive", "yes")
            status, s = call("network_settings_get")
            self.assertEqual((status, s["stored_invalid"]), (200, True))
            self.assertIn("Keys & data → Network", s["error"])
            status, s = call("network_settings_put", {"adaptive": True})       # stored fixed (4) is valid
            self.assertEqual((status, s["adaptive"], s["fixed"]), (200, True, 4))
            audit = [a for a in db.list_audit(limit=10) if a["action"] == "network_settings_changed"][0]
            self.assertEqual(audit["detail"]["old"], {"adaptive": None, "fixed": 4, "stored_invalid": True,
                                                      "stored": {"network.adaptive": "yes",
                                                                 "network.fixed": None}})
            self.assertEqual(audit["detail"]["new"], {"adaptive": True, "fixed": 4})
            db.meta_set("network.fixed", "zero")
            status, payload = call("network_settings_put", {"adaptive": False})
            self.assertEqual(status, 400)
            self.assertIn("fixed is required", payload["error"])
            status, s = call("network_settings_put", {"adaptive": False, "fixed": 2})
            self.assertEqual((status, s["fixed"], s["current"]), (200, 2, {"alpaca": 2, "tiingo": 2}))
            self.assertEqual(set(call("network_settings_get")[1]), {"adaptive", "fixed", "current", "min", "max"})

    def test_defaults_until_the_db_is_open(self):
        class Closed:
            def is_open(self):
                return False

            def meta_get(self, key):
                raise AssertionError("must not read a closed DB")

        network.reset(store=Closed())
        self.assertEqual(network.pool_size(), network.MAX_CONCURRENCY)
        self.assertEqual(network.limiter_for("alpaca").limit, 4)


class FakeClock:
    def __init__(self):
        self.t = 5000.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class ProbeTest(unittest.TestCase):
    def make(self, outcome=None):
        self.clock = FakeClock()
        self.fetched = []

        def fetch(source):
            self.fetched.append((source, self.clock.t))
            if outcome is not None:
                raise outcome
        return network.Prober(fetch, monotonic=self.clock.monotonic, sleep=self.clock.sleep)

    def test_spacing_at_least_30_seconds(self):
        p = self.make()
        self.assertEqual(p.next_probe_in("alpaca"), 0.0)
        self.assertTrue(p.probe("alpaca"))
        self.assertEqual(p.next_probe_in("alpaca"), 30.0)
        self.clock.t += 10
        self.assertAlmostEqual(p.next_probe_in("alpaca"), 20.0)
        self.assertTrue(p.probe("alpaca"))                        # slept the remaining 20 s first
        self.assertEqual(self.clock.sleeps, [20.0])
        self.assertEqual(self.fetched[1][1] - self.fetched[0][1], 30.0)
        for _ in range(3):
            p.probe("alpaca")
        times = [t for _s, t in self.fetched]
        self.assertTrue(all(b - a >= 30.0 for a, b in zip(times, times[1:])))

    def test_connection_failure_is_false_other_errors_raise(self):
        err = network.mark_connection(DataSourceUnavailable("alpaca", "equity_quotes", "timed out"))
        self.assertFalse(self.make(err).probe("alpaca"))
        self.assertFalse(self.make(socket.timeout()).probe("alpaca"))
        with self.assertRaises(SourceAuthRejected):
            self.make(SourceAuthRejected("alpaca", "equity_quotes", A.AUTH_MESSAGE)).probe("alpaca")

    def test_tiingo_is_not_probed_but_spaced(self):
        p = self.make()
        self.assertTrue(p.probe("tiingo"))
        self.assertTrue(p.probe("tiingo"))
        self.assertEqual(self.fetched, [])
        self.assertEqual(self.clock.sleeps, [30.0])
        self.assertEqual(p.requests, 0)
        with self.assertRaises(ValueError):
            p.probe("fred")

    def test_sources_are_spaced_independently(self):
        p = self.make()
        p.probe("alpaca")
        p.probe("tiingo")
        self.assertEqual(self.clock.sleeps, [])

    def test_default_probe_uses_latest_spy_trade(self):
        calls = []

        class Reg:
            def fetch(self, dt, op, *args):
                calls.append((dt, op) + args)

        old = registry.default()
        registry.set_default(Reg())
        try:
            network._default_probe_fetch("alpaca")
        finally:
            registry.set_default(old)
        self.assertEqual(calls, [("equity_quotes", "latest_trade", "SPY")])


if __name__ == "__main__":
    unittest.main()
