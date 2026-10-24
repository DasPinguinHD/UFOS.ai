"""Shared fakes for the Strategy Platform data-layer tests (no network, no
alpaca-py, no real keys): a fake alpaca-py SDK namespace with scripted
clients, fake HTTP errors, a gate without real sleeping, a fake provider,
and a temp hedgefund.db. Stdlib only. The few tests here check the fakes.

Run from HedgeFund/backend:  python -m unittest tests.test_sp_data_support -v
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from strategy_platform import secrets
from strategy_platform.data import http_gate
from strategy_platform.storage import db as sp_db


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


KEYS = {secrets.target("alpaca-paper-key-id"): "test-key-id",
        secrets.target("alpaca-paper-secret-key"): "test-secret"}


# --- fake HTTP errors (shapes of alpaca.common.exceptions.APIError / requests) -----------------

class FakeAPIError(Exception):
    """Like alpaca.common.exceptions.APIError: status_code + JSON message."""

    def __init__(self, status, message="error", headers=None):
        super().__init__(f'{{"code": {status}0000, "message": "{message}"}}')
        self.status_code = status
        self.message = message
        self.response = SimpleNamespace(status_code=status, headers=dict(headers or {}))


class ReadTimeout(OSError):
    """Same class name as requests.exceptions.ReadTimeout."""


class ConnectionError_(OSError):
    pass


ConnectionError_.__name__ = "ConnectionError"


# --- fake alpaca-py -----------------------------------------------------------------------------

class _Req:
    def __init__(self, **kw):
        self.kw = kw
        self.__dict__.update(kw)


def _req(name):
    return type(name, (_Req,), {})


class FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return None


class Script:
    """Scripted answers per client method: a list of results/exceptions; the
    last entry repeats. Records (method, request, client) calls."""

    def __init__(self):
        self.answers = {}
        self.calls = []
        self.clients = []

    def set(self, method, *answers):
        self.answers[method] = list(answers)

    def answer(self, method, request, client):
        self.calls.append((method, request, client))
        queue = self.answers.get(method)
        if not queue:
            raise AssertionError(f"no scripted answer for {method}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item


def make_client_class(name, script):
    methods = ("get_stock_bars", "get_crypto_bars", "get_stock_latest_quote", "get_crypto_latest_quote",
               "get_stock_latest_trade", "get_crypto_latest_trade", "get_news", "get_corporate_actions")

    def __init__(self, api_key=None, secret_key=None, raw_data=False, **kw):
        self.api_key, self.secret_key, self.raw_data = api_key, secret_key, raw_data
        self._retry = 3
        self._session = FakeSession()
        script.clients.append((name, self))

    ns = {"__init__": __init__}
    for m in methods:
        ns[m] = (lambda m: lambda self, request: script.answer(m, request, self))(m)
    return type(name, (), ns)


def fake_sdk(script):
    return SimpleNamespace(
        Adjustment=SimpleNamespace(ALL="all", SPLIT="split"), DataFeed=SimpleNamespace(IEX="iex", SIP="sip"),
        CorporateActionsType=lambda v: v,
        TimeFrame=lambda amount, unit: ("TF", amount, unit),
        TimeFrameUnit=SimpleNamespace(Day="Day", Hour="Hour", Minute="Min"),
        StockBarsRequest=_req("StockBarsRequest"), CryptoBarsRequest=_req("CryptoBarsRequest"),
        StockLatestQuoteRequest=_req("StockLatestQuoteRequest"), CryptoLatestQuoteRequest=_req("CryptoLatestQuoteRequest"),
        StockLatestTradeRequest=_req("StockLatestTradeRequest"), CryptoLatestTradeRequest=_req("CryptoLatestTradeRequest"),
        NewsRequest=_req("NewsRequest"), CorporateActionsRequest=_req("CorporateActionsRequest"),
        StockHistoricalDataClient=make_client_class("StockHistoricalDataClient", script),
        CryptoHistoricalDataClient=make_client_class("CryptoHistoricalDataClient", script),
        NewsClient=make_client_class("NewsClient", script),
        CorporateActionsClient=make_client_class("CorporateActionsClient", script))


class FakeTime:
    """monotonic + sleep for the gate: sleeping advances the clock."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def fake_gate(source="alpaca", **kw):
    ft = FakeTime()
    gate = http_gate.HttpGate(source, monotonic=ft.monotonic, sleep=ft.sleep, **kw)
    return gate, ft


# --- bars --------------------------------------------------------------------------------------

def equity_row(day, close, *, symbol="SPY", source="alpaca", fetched_at="2026-10-02T21:00:00+00:00", **over):
    """A daily IEX bar dict as the provider returns it (ts = NY midnight, EDT)."""
    from strategy_platform.data import pit
    ts = datetime.fromisoformat(day + "T04:00:00+00:00")
    row = {"symbol": symbol, "timeframe": "1Day", "ts": ts.isoformat(timespec="seconds"),
           "open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1000.0,
           "vwap": close, "trade_count": 10, "source": source,
           "known_at": over.pop("known_at", None) or pit.known_at_iso("equity", "1Day", ts),
           "fetched_at": fetched_at}
    row.update(over)
    return row


def crypto_row(day, close, *, symbol="BTC-USD", source="alpaca", fetched_at="2026-10-02T21:00:00+00:00"):
    ts = datetime.fromisoformat(day + "T00:00:00+00:00")
    return {"symbol": symbol, "timeframe": "1Day", "ts": ts.isoformat(timespec="seconds"),
            "open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 5.0,
            "vwap": close, "trade_count": 100, "source": source,
            "known_at": (ts + timedelta(days=1)).isoformat(timespec="seconds"), "fetched_at": fetched_at}


class FakeBarProvider:
    """Serves bars from ``self.bars[symbol]`` (rows) for registry.fetch-like
    calls; ``fail`` (exception) makes every call raise."""

    def __init__(self):
        self.bars = {}
        self.fail = None
        self.calls = []

    def fetch(self, data_type, op, *args):
        self.calls.append((data_type, op) + args)
        if self.fail is not None:
            raise self.fail
        assert op == "get_bars", op
        symbol, timeframe, start, end = args
        s, e = start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")
        return [dict(r) for r in self.bars.get(symbol, []) if s <= r["ts"] <= e]


class TempDb:
    """hedgefund.db in a temp dir outside the repo (storage.db)."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        sp_db.init(os.path.join(self._tmp.name, "hedgefund.db"))
        return sp_db

    def __exit__(self, *exc):
        sp_db.close()
        self._tmp.cleanup()


class FakesTest(unittest.TestCase):
    def test_script_repeats_last_answer_and_raises(self):
        s = Script()
        s.set("get_news", ValueError("x"), {"news": []})
        with self.assertRaises(ValueError):
            s.answer("get_news", None, None)
        self.assertEqual(s.answer("get_news", None, None), {"news": []})
        self.assertEqual(s.answer("get_news", None, None), {"news": []})

    def test_fake_error_shape(self):
        e = FakeAPIError(429, "too many requests", {"Retry-After": "2"})
        self.assertEqual((e.status_code, e.response.headers["Retry-After"]), (429, "2"))
        self.assertTrue(any("Timeout" in c.__name__ for c in type(ReadTimeout()).__mro__))


if __name__ == "__main__":
    unittest.main()
