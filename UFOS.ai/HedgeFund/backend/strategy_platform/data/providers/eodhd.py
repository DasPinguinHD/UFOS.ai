"""EODHD end-of-day prices + ISIN search (stage 9d, SPEC §20.11) — stocks / ETFs in the listing's currency.

Official docs read 2026-10-18 (cited in SPEC §20.11):
- EOD: ``GET https://eodhd.com/api/eod/{CODE}.{EXCHANGE}?api_token=…&fmt=json&from=&to=`` →
  ``[{date, open, high, low, close, adjusted_close, volume}]`` (``close`` as traded).
- Search: ``GET https://eodhd.com/api/search/{query}?api_token=…`` — query may be an ISIN →
  ``[{Code, Exchange, Name, Type, Country, Currency, ISIN, isPrimary, previousClose, previousCloseDate}]``.
- Every request costs 1 API call (a 404 too); the free plan has 20 calls per day (reset at midnight GMT), 1 year
  of EOD history, personal use only; exceeded → HTTP 402.

Budget: ``EODHD_DAILY_LIMIT`` calls per UTC day, counted in hedgefund.db ``meta`` BEFORE a request is sent (no
counter → no request). At the limit: ``EodhdLimitReached`` "EODHD daily limit reached (20/20) — next refresh
tomorrow". Cache: EOD history once per listing per UTC day (365 days in one call, ``meta`` ``eodhd/eod/<listing>``);
search results per ISIN for 30 days (``eodhd/search/<ISIN>``). The key (Credential Manager ``eodhd-api-key``)
travels only in the query string to eodhd.com and is redacted from every error and log line.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from ...core.errors import DataSourceUnavailable, SecretMissing, UserFacingError
from .. import http_gate
from ..http_gate import Retry

logger = logging.getLogger(__name__)

SOURCE = "eodhd"
SECRET = "eodhd-api-key"
HOST = "eodhd.com"
EOD_URL = "https://eodhd.com/api/eod/{listing}"
SEARCH_URL = "https://eodhd.com/api/search/{query}"
EODHD_DAILY_LIMIT = 20                     # the free plan (SPEC §20.11)
HISTORY_DAYS = 365                         # the free plan's history
SEARCH_TTL_DAYS = 30
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MISSING_KEY_MESSAGE = "EODHD API key is not set — enter it in Hedge Fund → Settings → Keys."
AUTH_MESSAGE = "EODHD rejected the API key — check it in Hedge Fund → Settings → Keys."
TERMS = "personal use only — no redistribution (EODHD terms)"
LISTING_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-_]{0,19}\.[A-Z0-9]{1,10}$")
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

_lock = threading.Lock()                    # the call counter
_flight_guard = threading.Lock()
_flights: Dict[str, threading.Lock] = {}
_urlopen: Optional[Callable] = None        # tests: a fake urlopen(req, timeout=, context=)
_clock: Optional[Callable[[], datetime]] = None


class EodhdLimitReached(UserFacingError):
    def __init__(self, used: int, limit: int):
        super().__init__(f"EODHD daily limit reached ({used}/{limit}) — next refresh tomorrow")
        self.used, self.limit = used, limit


def set_transport(urlopen: Optional[Callable], clock: Optional[Callable[[], datetime]] = None) -> None:
    """Tests: a fake ``urlopen`` (never the network) and an optional clock."""
    global _urlopen, _clock
    _urlopen, _clock = urlopen, clock


def _now() -> datetime:
    return _clock() if _clock is not None else datetime.now(timezone.utc)


def today() -> str:
    return _now().astimezone(timezone.utc).date().isoformat()


def redact(text, key: Optional[str]) -> str:
    out = str(text)
    if key:
        out = out.replace(key, "***").replace(urllib.parse.quote(key, safe=""), "***")
    return re.sub(r"(?i)(api_token=)[^&\s\"']+", r"\1***", out)


def _flight(key: str) -> threading.Lock:
    """One lock per listing / per ISIN search: parallel first-of-day reads make ONE call (review 2026-10-18)."""
    with _flight_guard:
        lk = _flights.get(key)
        if lk is None:
            lk = _flights[key] = threading.Lock()
        return lk


class EodhdFetchFailed(UserFacingError):
    """Today's fetch of a listing failed; the SAME text is shown until tomorrow or an explicit refresh (1 call)."""

    def __init__(self, reason: str, fetched_at: Optional[str], cached: bool):
        super().__init__(reason)
        self.fetched_at, self.cached = fetched_at, cached


def _db():
    from ...storage import db
    return db


# --- budget ----------------------------------------------------------------------------------------------------

def _calls_key(day: str) -> str:
    return f"eodhd/calls/{day}"


def budget() -> dict:
    day = today()
    used = int(_db().meta_get(_calls_key(day)) or 0)
    left = max(0, EODHD_DAILY_LIMIT - used)
    return {"plan": "free", "day": day, "daily_limit": EODHD_DAILY_LIMIT, "used_today": used, "left_today": left,
            "resets": "00:00 GMT",
            "message": None if left else f"EODHD daily limit reached ({used}/{EODHD_DAILY_LIMIT}) — next refresh "
                                         "tomorrow"}


def _spend() -> None:
    """Counts one call (persisted) or raises EodhdLimitReached — before anything is sent."""
    with _lock:
        day = today()
        used = int(_db().meta_get(_calls_key(day)) or 0)
        if used >= EODHD_DAILY_LIMIT:
            raise EodhdLimitReached(used, EODHD_DAILY_LIMIT)
        _db().meta_set(_calls_key(day), str(used + 1))


# --- transport -------------------------------------------------------------------------------------------------

def _key() -> str:
    from ... import secrets as sec
    try:
        return sec.require_secret(SECRET)
    except SecretMissing as ex:
        err = SecretMissing(ex.name, ex.label)
        err.user_message = MISSING_KEY_MESSAGE
        err.args = (MISSING_KEY_MESSAGE,)
        raise err from None


def _classify(ex: BaseException, data_type: str, key: str, tries: list):
    """No 4xx is retried (every attempt costs a call); a 5xx / timeout at most ONCE (review 2026-10-18)."""
    tries[0] += 1
    if isinstance(ex, urllib.error.HTTPError):
        st = ex.code
        if 500 <= st <= 599:
            if tries[0] <= 1:
                return Retry(f"EODHD HTTP {st}")
            return DataSourceUnavailable(SOURCE, data_type, f"EODHD server error (HTTP {st}) — retried once")
        if st in (401, 403):
            return DataSourceUnavailable(SOURCE, data_type, AUTH_MESSAGE + f" (HTTP {st})")
        if st == 402:
            return DataSourceUnavailable(SOURCE, data_type, "EODHD reports the daily API limit as used up (HTTP 402) — "
                                                            "next refresh tomorrow")
        if st == 404:
            return DataSourceUnavailable(SOURCE, data_type, "EODHD has no data for this listing (HTTP 404)")
        if st == 429:
            return DataSourceUnavailable(SOURCE, data_type, "EODHD's per-minute limit was hit (HTTP 429)")
        return DataSourceUnavailable(SOURCE, data_type, f"EODHD refused the request (HTTP {st})")
    reason = getattr(ex, "reason", None)
    if isinstance(ex, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError)):
        if tries[0] <= 1:
            return Retry("the request to EODHD timed out")
        return DataSourceUnavailable(SOURCE, data_type, "the request to EODHD timed out — retried once")
    if isinstance(ex, (urllib.error.URLError, ssl.SSLError, OSError)):
        return DataSourceUnavailable(SOURCE, data_type, f"network/TLS error — {HOST} could not be reached "
                                                        f"({redact(type(ex).__name__, key)})")
    return DataSourceUnavailable(SOURCE, data_type, f"unexpected error ({type(ex).__name__})")


def _get(url_base: str, params: dict, data_type: str):
    key = _key()
    url = url_base + "?" + urllib.parse.urlencode(dict(params, api_token=key, fmt="json"))
    if urllib.parse.urlparse(url).hostname != HOST:                 # defence in depth: only eodhd.com
        raise DataSourceUnavailable(SOURCE, data_type, "refused: not the EODHD host")
    opener = _urlopen if _urlopen is not None else urllib.request.urlopen
    context = ssl.create_default_context()

    def request():
        _spend()                                                     # every attempt is a call EODHD counts
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "UFOS.ai-HedgeFund/1.0"})
        with opener(req, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise DataSourceUnavailable(SOURCE, data_type, "the response was larger than expected")
        return body
    try:
        tries = [0]
        body = http_gate.gate_for(SOURCE).call(request, data_type=data_type,
                                               classify=lambda ex, dt: _classify(ex, dt, key, tries))
    except UserFacingError as ex:
        ex.user_message = redact(ex.user_message, key)
        ex.detail = redact(getattr(ex, "detail", ""), key)
        ex.args = tuple(redact(a, key) for a in ex.args)
        raise
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as ex:
        raise DataSourceUnavailable(SOURCE, data_type, f"unexpected data from EODHD ({type(ex).__name__})")


# --- search ----------------------------------------------------------------------------------------------------

def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1e12 else None


def _txt(v, n: int) -> Optional[str]:
    if v is None:
        return None
    s = " ".join(str(v).split())
    return s[:n] or None


def parse_search(raw, isin: str) -> List[dict]:
    """EODHD search rows → listing candidates for ``isin`` (rows with another ISIN dropped)."""
    if not isinstance(raw, list):
        raise DataSourceUnavailable(SOURCE, "search", "unexpected search answer (not a list)")
    out, seen = [], set()
    for r in raw:
        if not isinstance(r, dict):
            continue
        code, exch = _txt(r.get("Code"), 20), _txt(r.get("Exchange"), 10)
        if not code or not exch:
            continue
        if (str(r.get("ISIN") or "").strip().upper() or isin) != isin:
            continue
        listing = f"{code}.{exch}".upper()
        if not LISTING_RE.fullmatch(listing) or listing in seen:
            continue
        cur = (_txt(r.get("Currency"), 3) or "").upper() or None
        seen.add(listing)
        out.append({"listing": listing, "code": code.upper(), "exchange": exch.upper(), "name": _txt(r.get("Name"), 200),
                    "type": _txt(r.get("Type"), 40), "country": _txt(r.get("Country"), 40), "currency": cur,
                    "is_primary": bool(r.get("isPrimary")), "previous_close": _num(r.get("previousClose")),
                    "previous_close_date": _txt(r.get("previousCloseDate"), 10)})
    return out


def search_isin(isin: str, refresh: bool = False) -> dict:
    """{isin, candidates, cached, searched_at} — cached per ISIN for SEARCH_TTL_DAYS."""
    isin = (isin or "").strip().upper()
    if not _ISIN_RE.fullmatch(isin):
        raise ValueError("isin: an ISIN (12 characters)")
    ck = f"eodhd/search/{isin}"
    with _flight(ck):
        cached = cached_search(isin)
        if cached is not None and not refresh:
            return dict(cached, cached=True)
        raw = _get(SEARCH_URL.format(query=urllib.parse.quote(isin, safe="")), {"limit": 50}, "search")
        res = {"isin": isin, "candidates": parse_search(raw, isin),
               "searched_at": _now().astimezone(timezone.utc).isoformat(timespec="seconds")}
        _db().meta_set(ck, json.dumps(res))
        return dict(res, cached=False)


def cached_search(isin: str) -> Optional[dict]:
    text = _db().meta_get(f"eodhd/search/{(isin or '').upper()}")
    if not text:
        return None
    try:
        res = json.loads(text)
        at = datetime.fromisoformat(res["searched_at"])
    except (ValueError, KeyError, TypeError):
        return None
    if _now() - at > timedelta(days=SEARCH_TTL_DAYS):
        return None
    return res


# --- end of day ------------------------------------------------------------------------------------------------

def parse_eod(raw) -> List[List]:
    if not isinstance(raw, list):
        raise DataSourceUnavailable(SOURCE, "eod", "unexpected EOD answer (not a list)")
    bars = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        d, c = str(r.get("date") or "")[:10], _num(r.get("close"))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and c is not None and c > 0:
            bars.append([d, c])
    return sorted(bars)


def _today_entries() -> Dict[str, dict]:
    from ...storage import db
    with db._connect() as conn:
        rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'eodhd/eod/%'").fetchall()
    out = {}
    for r in rows:
        try:
            e = json.loads(r[1])
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("fetched") == today():
            out[r[0][len("eodhd/eod/"):]] = e
    return out


def cached_listings_today() -> int:
    return sum(1 for e in _today_entries().values() if e.get("ok", True))


def failures_today() -> List[dict]:
    """Listings whose fetch failed today (cached until tomorrow or a refresh)."""
    return [{"listing": k, "reason": e.get("reason"), "fetched_at": e.get("fetched_at")}
            for k, e in sorted(_today_entries().items()) if e.get("ok") is False]


def closes(listing: str, refresh: bool = False) -> List[List]:
    """[[day, close]] of the last HISTORY_DAYS (as traded, listing currency). ONE fetch per listing per UTC day —
    its outcome (ok or the failure reason) is cached until tomorrow; ``refresh`` fetches again (costs 1 call).
    Parallel callers wait for the one fetch in flight."""
    listing = (listing or "").strip().upper()
    if not LISTING_RE.fullmatch(listing):
        raise ValueError("listing: CODE.EXCHANGE")
    ck = f"eodhd/eod/{listing}"
    with _flight(ck):
        text = _db().meta_get(ck)
        if text and not refresh:
            try:
                c = json.loads(text)
            except ValueError:
                c = {}
            if isinstance(c, dict) and c.get("fetched") == today():
                if c.get("ok") is False:
                    raise EodhdFetchFailed(c.get("reason") or "EODHD fetch failed today", c.get("fetched_at"), True)
                if "bars" in c:
                    return c["bars"]
        t = date.fromisoformat(today())
        at = _now().astimezone(timezone.utc).isoformat(timespec="seconds")
        try:
            raw = _get(EOD_URL.format(listing=urllib.parse.quote(listing, safe=".")),
                       {"from": (t - timedelta(days=HISTORY_DAYS)).isoformat(), "to": t.isoformat(), "period": "d"},
                       "eod")
            bars = parse_eod(raw)
        except (EodhdLimitReached, SecretMissing):
            raise                                       # no call was made: nothing to remember
        except UserFacingError as ex:
            reason = (f"EODHD {listing}: {getattr(ex, 'reason', None) or ex.user_message} — cached until tomorrow "
                      "(refresh costs 1 call)")[:300]
            _db().meta_set(ck, json.dumps({"fetched": today(), "fetched_at": at, "ok": False, "reason": reason}))
            raise EodhdFetchFailed(reason, at, False) from None
        _db().meta_set(ck, json.dumps({"fetched": today(), "fetched_at": at, "ok": True, "bars": bars}))
        logger.info("eodhd: %s — %d daily closes fetched", listing, len(bars))
        return bars
