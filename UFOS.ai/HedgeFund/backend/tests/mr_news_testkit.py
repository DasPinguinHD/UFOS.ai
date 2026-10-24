"""Shared fakes for the MR6 news/logo tests (no network)."""
from datetime import datetime, timedelta, timezone

from market_research.assets import SymbolNotCovered

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
COVERED = {"AAPL": "equity", "MSFT": "equity", "BTC-USD": "crypto"}
PNG_1x1 = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
           + b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89" + b"\x00\x00\x00\x0aIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
           + b"\x00\x00\x00\x00IEND\xaeB`\x82")


def png(w=1, h=1, pad=0):
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + w.to_bytes(4, "big") + h.to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00" + b"\x00" * (4 + pad))


def jpeg(w=10, h=20):
    return (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + b"\xff\xdb\x00\x04\x00\x00" + b"\xff\xc0\x00\x11\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big")
            + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01" + b"\xff\xd9")


def gif(w=3, h=4):
    return b"GIF89a" + w.to_bytes(2, "little") + h.to_bytes(2, "little") + b"\x00\x00\x00;"


def webp(w=16, h=9):
    body = b"VP8 " + (10).to_bytes(4, "little") + b"\x00\x00\x00" + b"\x9d\x01\x2a" + w.to_bytes(2, "little") \
        + h.to_bytes(2, "little")
    return b"RIFF" + (4 + len(body)).to_bytes(4, "little") + b"WEBP" + body


def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def raw_news(nid, created, *, headline=None, symbols=("AAPL",), updated=None, url=None, images=None, summary="s"):
    """An item as AlpacaProvider.news returns it (already whitespace-normalised by the provider)."""
    return {"id": nid, "headline": headline or f"Headline {nid}", "summary": summary, "publisher": "benzinga",
            "url": url if url is not None else f"https://www.benzinga.com/news/{nid}",
            "created_at": iso(created), "updated_at": iso(updated) if updated else iso(created),
            "symbols": list(symbols), "images": images if images is not None else [], "source": "alpaca"}


class FakeSources:
    def __init__(self):
        self.rows = []            # provider dicts
        self.news_calls = []
        self.news_fail = None
        self.logo_calls = []
        self.logo_result = {}     # symbol -> bytes | None | exception
        self.logo_default = None

    def news(self, symbol, end=None, limit=20):
        self.news_calls.append((symbol, end, limit))
        if self.news_fail is not None:
            raise self.news_fail
        from strategy_platform.data.providers.alpaca import news_symbol
        tag = news_symbol(symbol)
        out = [r for r in self.rows if tag in r["symbols"] and (end is None or r["created_at"] <= iso(end))]
        out.sort(key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in out[:limit]]

    def logo(self, symbol):
        self.logo_calls.append(symbol)
        v = self.logo_result.get(symbol, self.logo_default)
        if isinstance(v, BaseException):
            raise v
        return v


class FakeData:
    def __init__(self, now=None):
        self.sources = FakeSources()
        self.clock = [now or NOW]

    def now(self):
        return self.clock[0]

    def advance(self, **kw):
        self.clock[0] = self.clock[0] + timedelta(**kw)

    def asset(self, symbol):
        if symbol not in COVERED:
            raise SymbolNotCovered(f"Not available from Alpaca: {symbol} is not in Alpaca's list of active US equities.")
        return {"symbol": symbol, "asset_class": COVERED[symbol]}


class Q(dict):
    def __init__(self, pairs=()):
        super().__init__()
        self.pairs = list(pairs)
        for k, v in self.pairs:
            self.setdefault(k, v)

    def getall(self, k, *default):
        # aiohttp semantics: a missing key raises KeyError unless a default is given
        vals = [v for kk, v in self.pairs if kk == k]
        if not vals:
            if default:
                return default[0]
            raise KeyError(k)
        return vals


class Mono:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t
