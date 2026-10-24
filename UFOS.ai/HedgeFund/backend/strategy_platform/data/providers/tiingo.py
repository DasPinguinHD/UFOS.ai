"""Tiingo end-of-day prices — daily equity/ETF history BEFORE the seam (SPEC §8.1).

User decisions 2026-10-03/-06: Tiingo is the second data source, used ONLY
before the seam (``data/seam.py``: 2016-01-04) for daily prices AND volume
(data type ``equity_bars_pre2016``) — never as a substitute for Alpaca, which
serves both from the seam on (SIP history). Licence ``personal_use``.
(2026-10-03 … 06 Tiingo also served ``equity_volume`` for all years; replaced
by Alpaca SIP volume, SPEC §8.8.)

Official documentation (checked 2026-10-03):
- https://www.tiingo.com/documentation/end-of-day —
  ``GET https://api.tiingo.com/tiingo/daily/<ticker>/prices?startDate=YYYY-MM-DD&endDate=YYYY-MM-DD``
  returns a JSON list of ``{date, open, high, low, close, volume, adjOpen,
  adjHigh, adjLow, adjClose, adjVolume, divCash, splitFactor}``: raw OHLCV,
  split- and dividend-adjusted OHLCV, the cash distribution with that date as
  ex-date, and the split multiplier (1.0 on days without a split; 2.0 for a
  2-for-1 split, 0.5 for a 1-for-2 reverse split). Auth header
  ``Authorization: Token <key>``.
- https://www.tiingo.com/about/pricing — free "Starter" plan: 50 requests per
  hour, 1,000 requests per day, 500 unique symbols per month, 1 GB per month,
  internal (personal) use only, no display/sharing. The HTTP gate is
  configured at 80 % (40/hour, 800/day, see ``http_gate.DEFAULT_WINDOWS``);
  one request per symbol covers its whole pre-seam history + the overlap.

Security: the key comes only from the Windows Credential Manager
(``secrets.require_secret("tiingo-api-key")``) at every call, travels only in
the Authorization header over TLS with certificate verification
(``ssl.create_default_context()``), and is never logged; every error text is
passed through ``redact`` before it is raised or logged.

Errors: missing key -> ``SecretMissing`` ("Tiingo API key is not set — enter
it in Hedge Fund → Keys & data."); 401/403 -> ``SourceAuthRejected``; 404 ->
``UnknownSymbol`` (an ``InvalidRequest``: Tiingo does not know the ticker);
429 -> ``DataSourceUnavailable`` at once (the allowance is hourly/daily —
retrying in seconds cannot help); 5xx / timeouts -> retried by the gate;
network/TLS and other 4xx / malformed data -> ``DataSourceUnavailable``.
Nothing is skipped or filled in silently.

Pure helpers: ``parse_prices(raw)`` (JSON -> validated rows) and
``build_series(symbol, rows, fetched_at)`` -> ``{"split", "all",
"dividends", "splits"}`` from ONE response (split math below).

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
from __future__ import annotations

import json
import logging
import math
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Dict, List, Optional

from ...core import calendars
from ...core.errors import DataSourceUnavailable, InvalidRequest, SecretMissing, UserFacingError
from ...core.instruments import instrument
from .. import http_gate, pit
from ..http_gate import Retry
from .base import Provider, SourceAuthRejected, iso

logger = logging.getLogger(__name__)

SOURCE = "tiingo"
DATA_TYPE = "equity_bars_pre2016"
DATA_TYPES = (DATA_TYPE,)                  # since 2026-10-06 only before the seam (prices AND volume)
SECRET = "tiingo-api-key"
BASE_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 20 * 1024 * 1024          # ~26 years of daily rows are < 3 MB
MISSING_KEY_MESSAGE = "Tiingo API key is not set — enter it in Hedge Fund → Keys & data."
AUTH_MESSAGE = "Tiingo rejected the API key — check it in Hedge Fund → Keys & data."
CLOSE_NY = time(16, 0)
_PRICE_FIELDS = ("open", "high", "low", "close", "adjOpen", "adjHigh", "adjLow", "adjClose")


class UnknownSymbol(InvalidRequest):
    """Tiingo has no daily prices for this ticker (HTTP 404)."""

    def __init__(self, symbol: str):
        super().__init__(f"Tiingo has no daily prices for {symbol}.")
        self.symbol = symbol


def redact(text, *values) -> str:
    """``text`` with every secret value and any ``Token …`` / ``token=…`` masked."""
    out = str(text)
    for v in values:
        if v:
            out = out.replace(str(v), "***")
    out = re.sub(r"(?i)(token[ =:]+)[^\s&\"',;]+", r"\1***", out)
    return out


def require_key(secrets_module=None) -> str:
    """The Tiingo key from the Credential Manager, else ``SecretMissing`` with
    MISSING_KEY_MESSAGE (SPEC §8.1 wording; the key row is in "Keys & data")."""
    sec = secrets_module
    if sec is None:
        from ... import secrets as sec
    try:
        return sec.require_secret(SECRET)
    except SecretMissing as ex:
        err = SecretMissing(ex.name, ex.label)
        err.user_message = MISSING_KEY_MESSAGE
        err.args = (MISSING_KEY_MESSAGE,)
        raise err from None


def ticker(symbol: str) -> str:
    """Platform equity symbol -> Tiingo ticker (share classes use '-': BRK.B -> BRK-B)."""
    inst = instrument(symbol)
    if inst.asset_class != "equity":
        raise ValueError(f"{inst.symbol}: Tiingo daily prices are used for equities/ETFs only")
    return inst.symbol.replace(".", "-")


def bar_ts(d: date) -> str:
    """Bar start like Alpaca's daily bars: midnight New York of the trading date, in UTC."""
    return iso(datetime.combine(d, time(0, 0), tzinfo=calendars.NY))


def known_at(d: date) -> str:
    """Point-in-time of a Tiingo daily bar. Inside the NYSE calendar years:
    the session close (pit.py). Before them (all stored Tiingo rows): 16:00
    New York of that date — the regular close, and on the rare early-close
    days (13:00) later than the true close, so never earlier than knowable."""
    if d.year >= min(calendars.CALENDAR_YEARS):
        return pit.known_at_iso("equity", "1Day", datetime.combine(d, time(0, 0), tzinfo=calendars.NY))
    return iso(datetime.combine(d, CLOSE_NY, tzinfo=calendars.NY))


def _finite(v, field: str, *, positive: bool = False, allow_zero: bool = True) -> float:
    if v is None or isinstance(v, bool):
        raise ValueError(f"{field} missing")
    f = float(v)
    if not math.isfinite(f):
        raise ValueError(f"{field} not finite")
    if positive and (f < 0 or (f == 0 and not allow_zero)):
        raise ValueError(f"{field} must be > 0" if not allow_zero else f"{field} negative")
    return f


def parse_prices(raw) -> List[dict]:
    """Tiingo JSON list -> rows ``{date, open, high, low, close, volume,
    adj_open, adj_high, adj_low, adj_close, adj_volume, div_cash,
    split_factor}`` (date = ``date`` object), oldest first. Any missing or
    invalid field -> ValueError (the caller turns it into DataSourceUnavailable)."""
    if not isinstance(raw, list):
        raise TypeError(f"expected a JSON list, got {type(raw).__name__}")
    out, seen = [], set()
    for i, it in enumerate(raw):
        if not isinstance(it, dict):
            raise TypeError(f"row {i}: expected an object")
        d = date.fromisoformat(str(it["date"])[:10])
        if d in seen:
            raise ValueError(f"duplicate date {d}")
        seen.add(d)
        row = {"date": d}
        for f in _PRICE_FIELDS:
            row[{"adjOpen": "adj_open", "adjHigh": "adj_high", "adjLow": "adj_low",
                 "adjClose": "adj_close"}.get(f, f)] = _finite(it.get(f), f, positive=True, allow_zero=False)
        row["volume"] = _finite(it.get("volume"), "volume", positive=True)
        row["adj_volume"] = _finite(it.get("adjVolume"), "adjVolume", positive=True)
        row["div_cash"] = _finite(it.get("divCash"), "divCash", positive=True)
        row["split_factor"] = _finite(it.get("splitFactor"), "splitFactor", positive=True, allow_zero=False)
        out.append(row)
    out.sort(key=lambda r: r["date"])
    return out


def action_row(symbol: str, kind: str, ex_date: date, *, cash=None, ratio=None, fetched_at: str) -> dict:
    return {"symbol": symbol, "kind": kind, "ex_date": ex_date.isoformat(), "cash_amount": cash,
            "split_ratio": ratio, "pay_date": None, "source": SOURCE, "known_at": pit.action_known_at_iso(ex_date),
            "fetched_at": fetched_at}


def build_series(symbol: str, rows: List[dict], fetched_at: str) -> Dict[str, list]:
    """From ONE parsed response:

    - ``split``: raw OHLC divided by C(d), volume multiplied by C(d), where
      C(d) = product of ``splitFactor`` of every row dated AFTER d up to the
      last row of the response (a split's factor is reported on its ex-date,
      whose raw prices are already post-split). The response ends at the end
      of the seam overlap, so every row shares ONE split basis; splits after
      that are absorbed by the seam factor (data/seam.py).
    - ``all``: Tiingo's ``adjOpen/adjHigh/adjLow/adjClose/adjVolume``.
    - ``dividends``: rows with ``divCash > 0`` (raw cash per share, ex-date).
    - ``splits``: rows with ``splitFactor != 1`` (forward > 1, reverse < 1).
    Bars carry ``source="tiingo"`` and their ``adjustment``; vwap/trade_count None.
    """
    rows = sorted(rows, key=lambda r: r["date"])
    split_bars: List[dict] = []
    all_bars: List[dict] = []
    dividends: List[dict] = []
    splits: List[dict] = []
    cum = 1.0
    for r in reversed(rows):
        d = r["date"]
        ts, ka = bar_ts(d), known_at(d)
        base = {"symbol": symbol, "timeframe": "1Day", "ts": ts, "vwap": None, "trade_count": None,
                "source": SOURCE, "known_at": ka, "fetched_at": fetched_at}
        split_bars.append(dict(base, open=r["open"] / cum, high=r["high"] / cum, low=r["low"] / cum,
                               close=r["close"] / cum, volume=r["volume"] * cum, adjustment="split"))
        all_bars.append(dict(base, open=r["adj_open"], high=r["adj_high"], low=r["adj_low"], close=r["adj_close"],
                             volume=r["adj_volume"], adjustment="all"))
        if r["div_cash"] > 0:
            dividends.append(action_row(symbol, "cash_dividend", d, cash=r["div_cash"], fetched_at=fetched_at))
        if r["split_factor"] != 1.0:
            kind = "forward_split" if r["split_factor"] > 1.0 else "reverse_split"
            splits.append(action_row(symbol, kind, d, ratio=r["split_factor"], fetched_at=fetched_at))
        cum *= r["split_factor"]            # applies to every earlier row
    for lst in (split_bars, all_bars, dividends, splits):
        lst.reverse()
    return {"split": split_bars, "all": all_bars, "dividends": dividends, "splits": splits}


def _now_fn(clock) -> Callable[[], datetime]:
    if clock is None:
        return lambda: datetime.now(timezone.utc)
    return clock.now_utc if hasattr(clock, "now_utc") else clock


def _http_detail(ex) -> str:
    try:
        body = ex.read(2000)
    except Exception:  # noqa: BLE001 — the body of a foreign error object
        return ""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        if isinstance(data, dict) and isinstance(data.get("detail"), str):
            return " ".join(data["detail"].split())[:200]
    except ValueError:
        pass
    return " ".join(body.decode("utf-8", "replace").split())[:200] if isinstance(body, bytes) else ""


class TiingoProvider(Provider):
    name = SOURCE
    data_types = DATA_TYPES

    def __init__(self, *, gate=None, secrets_module=None, urlopen=None, clock=None,
                 timeout: float = REQUEST_TIMEOUT_SECONDS):
        self._gate_obj = gate
        self._secrets = secrets_module
        self._urlopen = urlopen if urlopen is not None else urllib.request.urlopen
        self._now = _now_fn(clock)
        self.timeout = float(timeout)

    def _gate(self):
        return self._gate_obj if self._gate_obj is not None else http_gate.gate_for(SOURCE)

    def _key(self) -> str:
        return require_key(self._secrets)

    def classify(self, ex: BaseException, symbol: str, key: str, dt: str = DATA_TYPE):
        """Gate classifier: Retry for 5xx / timeouts, else the UserFacingError to raise (redacted)."""
        if isinstance(ex, urllib.error.HTTPError):
            status = ex.code
            detail = redact(_http_detail(ex), key)
            if status in (401, 403):
                return SourceAuthRejected(SOURCE, dt, AUTH_MESSAGE, detail=f"HTTP {status}")
            if status == 404:
                return UnknownSymbol(symbol)
            if status == 429:
                return DataSourceUnavailable(SOURCE, dt, "Tiingo's request allowance is used up (HTTP 429; "
                                             "free plan: 50 requests/hour, 1,000/day) — try again later")
            if 500 <= status <= 599:
                return Retry(f"Tiingo server error (HTTP {status})")
            return DataSourceUnavailable(SOURCE, dt,
                                         f"Tiingo refused the request (HTTP {status}{': ' + detail if detail else ''})")
        reason = getattr(ex, "reason", None)
        if isinstance(ex, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError)):
            return Retry("the request to Tiingo timed out")
        if isinstance(ex, (urllib.error.URLError, ssl.SSLError, OSError)):
            return DataSourceUnavailable(SOURCE, dt,
                                         f"network/TLS error — api.tiingo.com could not be reached "
                                         f"({redact(type(ex).__name__, key)})")
        return DataSourceUnavailable(SOURCE, dt, f"unexpected error ({type(ex).__name__})")

    def daily_prices(self, symbol: str, start, end, *, progress=None, data_type: str = DATA_TYPE) -> List[dict]:
        """Parsed daily rows (see ``parse_prices``) for start <= date <= end (dates).
        ``data_type`` (equity_bars_pre2016 | equity_volume) only names the use in errors."""
        if data_type not in DATA_TYPES:
            raise ValueError(f"unknown Tiingo data type {data_type!r}")
        dt = data_type
        tk = ticker(symbol)
        lo, hi = _as_date(start), _as_date(end)
        if lo > hi:
            raise ValueError("start must not lie after end")
        key = self._key()
        url = BASE_URL.format(ticker=urllib.parse.quote(tk.lower(), safe="-")) + "?" + urllib.parse.urlencode(
            {"startDate": lo.isoformat(), "endDate": hi.isoformat(), "format": "json"})
        context = ssl.create_default_context()       # certificate + hostname verification

        def request():
            req = urllib.request.Request(url, headers={
                "Authorization": f"Token {key}", "Accept": "application/json",
                "Content-Type": "application/json", "User-Agent": "UFOS.ai-HedgeFund/1.0"})
            with self._urlopen(req, timeout=self.timeout, context=context) as resp:
                body = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise DataSourceUnavailable(SOURCE, dt, "the response was larger than expected")
            return body

        try:
            body = self._gate().call(request, data_type=dt, classify=lambda ex, _dt: self.classify(ex, symbol, key, dt),
                                     progress=progress)
        except UserFacingError as ex:
            # defence in depth: no user-facing text may carry the key
            ex.user_message = redact(ex.user_message, key)
            ex.detail = redact(getattr(ex, "detail", ""), key)
            ex.args = tuple(redact(a, key) for a in ex.args)
            raise
        logger.info("tiingo: %s daily prices %s..%s fetched (%d bytes)", tk, lo, hi, len(body))
        try:
            raw = json.loads(body.decode("utf-8"))
            if isinstance(raw, dict) and "detail" in raw:
                raise DataSourceUnavailable(SOURCE, dt,
                                            f"Tiingo answered with an error ({redact(str(raw.get('detail'))[:200], key)})")
            return parse_prices(raw)
        except UserFacingError:
            raise
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as ex:
            raise DataSourceUnavailable(SOURCE, dt,
                                        f"unexpected data from Tiingo ({type(ex).__name__}: "
                                        f"{redact(str(ex), key)[:120]})") from ex


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
