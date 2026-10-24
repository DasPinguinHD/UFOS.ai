"""Kraken PUBLIC market data — daily crypto closes in EUR for the Shadow Account (stage 9d, SPEC §20.11).

``GET https://api.kraken.com/0/public/OHLC?pair=<pair>&interval=1440`` (https://docs.kraken.com/api-reference/
market-data/get-ohlc-data.md): no key; ``{error: [...], result: {<pair key>: [[time, open, high, low, close, vwap,
volume, count], …], last}}``; at most 720 entries; the LAST entry is the current, uncommitted frame and is never
used. Only host ``api.kraken.com`` with path prefix ``/0/public/`` is allowed (checked before every request) — no
private endpoint, no key, nothing to do with trading (the broker live guard is separate and unchanged).

Fixed, unambiguous table (user decision 2026-10-18): TR crypto symbol BTC → XBTEUR, ETH → ETHEUR, SOL → SOLEUR.
Any other coin has no source. Closes are cached in memory for 15 minutes per pair.
"""
from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ...core.errors import DataSourceUnavailable
from .. import http_gate
from ..http_gate import Retry

SOURCE = "kraken_public"
HOST = "api.kraken.com"
PUBLIC_PREFIX = "/0/public/"
OHLC_URL = "https://api.kraken.com/0/public/OHLC"
PAIRS = {"BTC": ("XBTEUR", "XBT/EUR"), "ETH": ("ETHEUR", "ETH/EUR"), "SOL": ("SOLEUR", "SOL/EUR")}
CACHE_SECONDS = 15 * 60
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_urlopen: Optional[Callable] = None
_cache: Dict[str, tuple] = {}
_lock = threading.Lock()


def set_transport(urlopen: Optional[Callable]) -> None:
    """Tests: a fake ``urlopen`` (never the network); also clears the cache."""
    global _urlopen
    _urlopen = urlopen
    _cache.clear()


def pair_for(symbol: Optional[str]):
    """(request pair, display pair) for a TR crypto symbol, or None."""
    return PAIRS.get((symbol or "").strip().upper())


def assert_public(url: str) -> None:
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or p.hostname != HOST or p.port not in (None, 443) or not p.path.startswith(PUBLIC_PREFIX):
        raise DataSourceUnavailable(SOURCE, "crypto_eur_closes", "refused: only Kraken's public market-data path is "
                                                                 "allowed")


def _classify(ex: BaseException, data_type: str):
    if isinstance(ex, urllib.error.HTTPError):
        if ex.code == 429 or 500 <= ex.code <= 599:
            return Retry(f"Kraken HTTP {ex.code}")
        return DataSourceUnavailable(SOURCE, data_type, f"Kraken refused the request (HTTP {ex.code})")
    reason = getattr(ex, "reason", None)
    if isinstance(ex, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError)):
        return Retry("the request to Kraken timed out")
    if isinstance(ex, (urllib.error.URLError, ssl.SSLError, OSError)):
        return DataSourceUnavailable(SOURCE, data_type, f"network/TLS error — {HOST} could not be reached")
    return DataSourceUnavailable(SOURCE, data_type, f"unexpected error ({type(ex).__name__})")


def parse_ohlc(raw) -> List[List]:
    """[[day, close]] of the COMMITTED daily frames (the last, current frame is dropped)."""
    if not isinstance(raw, dict):
        raise DataSourceUnavailable(SOURCE, "crypto_eur_closes", "unexpected answer (not an object)")
    errs = raw.get("error") or []
    if errs:
        raise DataSourceUnavailable(SOURCE, "crypto_eur_closes", "Kraken error: " + "; ".join(str(e)[:80]
                                                                                           for e in errs[:3]))
    res = raw.get("result")
    rows = [v for k, v in (res or {}).items() if k != "last" and isinstance(v, list)] if isinstance(res, dict) else []
    if len(rows) != 1:
        raise DataSourceUnavailable(SOURCE, "crypto_eur_closes", "unexpected answer (one pair expected)")
    out = []
    for e in rows[0][:-1]:
        try:
            day = datetime.fromtimestamp(int(e[0]), tz=timezone.utc).date().isoformat()
            c = float(e[4])
        except (TypeError, ValueError, IndexError, OverflowError, OSError):
            continue
        if c > 0:
            out.append([day, c])
    return sorted(out)


def closes(symbol: str) -> List[List]:
    """Daily EUR closes for a fixed-table symbol (BTC / ETH / SOL)."""
    p = pair_for(symbol)
    if p is None:
        raise ValueError(f"no Kraken EUR pair for {symbol!r}")
    pair = p[0]
    with _lock:
        hit = _cache.get(pair)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    url = OHLC_URL + "?" + urllib.parse.urlencode({"pair": pair, "interval": 1440})
    assert_public(url)
    opener = _urlopen if _urlopen is not None else urllib.request.urlopen
    context = ssl.create_default_context()

    def request():
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "UFOS.ai-HedgeFund/1.0"})
        with opener(req, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise DataSourceUnavailable(SOURCE, "crypto_eur_closes", "the response was larger than expected")
        return body
    body = http_gate.gate_for(SOURCE).call(request, data_type="crypto_eur_closes", classify=_classify)
    try:
        bars = parse_ohlc(json.loads(body.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as ex:
        raise DataSourceUnavailable(SOURCE, "crypto_eur_closes", f"unexpected data ({type(ex).__name__})")
    with _lock:
        _cache[pair] = (time.monotonic(), bars)
    return bars
