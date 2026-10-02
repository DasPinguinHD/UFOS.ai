"""Shared fixtures for the crypto tests (paper trading only): an EngineCase
with BTC-USD / ETH-USD bars and quotes and a fake LLM that also answers for
the crypto symbols. Stdlib only."""
import json
from datetime import timedelta

try:
    from fund_testkit import EngineCase, FakeLLM, make_bars
except ImportError:  # pragma: no cover - run as package
    from tests.fund_testkit import EngineCase, FakeLLM, make_bars

from fund import llm as llm_mod
from fund import store
from fund.types import OrderRequest

BTC = "BTC-USD"
ETH = "ETH-USD"


class CryptoLLM(FakeLLM):
    """FakeLLM that also answers for the crypto symbols (records the last prompt)."""
    symbols = ["SPY", "QQQ", BTC, ETH]

    async def __call__(self, messages, model, max_tokens, temperature):
        system = messages[0]["content"]
        kind = "sentiment" if "news sentiment" in system else "judge"
        self.calls.append(kind)
        if kind == "judge":
            self.judge_messages = messages
        if kind == "sentiment":
            data = [{"symbol": s, "score": 0.4, "confidence": 0.6, "summary": "Upbeat coverage."} for s in self.symbols]
        else:
            data = [{"symbol": s, "action": self.action, "confidence": self.confidence,
                     "rationale": "Trend and sentiment agree."} for s in self.symbols]
        return json.dumps(data), model, {"cost_usd": self.cost, "prompt_tokens": 100, "completion_tokens": 20}


class CryptoCase(EngineCase):
    watchlist = ["SPY", "QQQ", BTC]
    extra_params = {"crypto_fees_bps": 0.0}

    def setUp(self):
        super().setUp()
        self.fake = CryptoLLM()
        llm_mod.TRANSPORT = self.fake
        self.feed.bars[BTC] = make_bars(50000.0, 400)
        self.feed.quotes[BTC] = 50000.0
        self.feed.bars[ETH] = make_bars(2000.0, 400)
        self.feed.quotes[ETH] = 2000.0

    def set_headlines(self):
        ts = (self.now - timedelta(hours=2)).isoformat(timespec="seconds")
        self.feed.headlines = {s: [{"title": f"{s} rallies on strong data", "publisher": "Wire",
                                    "published_at": ts, "url": "https://example.com/a"}]
                               for s in ("SPY", "QQQ", BTC)}

    def params(self):
        return store.effective_params()[1]

    def set_params(self, **kw):
        store.save_params(dict(self.params(), **kw), "test")

    def order(self, symbol, side, qty=None, notional=None, origin="manual", allow_reduce=False):
        return self.broker.submit(OrderRequest(symbol=symbol, side=side, qty=qty, notional=notional, origin=origin),
                                  self.now, self.params(), allow_reduce=allow_reduce)

    def ledger(self, kind):
        return store.cash_entries(1000, kind=kind)
