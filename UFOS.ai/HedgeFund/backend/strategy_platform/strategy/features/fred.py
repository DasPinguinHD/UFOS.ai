"""Minimal FRED/ALFRED client for the external features (SPEC §7.8).

Synchronous (features load in worker threads), stdlib ``urllib``; every
request goes through the HTTP gate of source ``fred`` (``data/http_gate``:
rate limit + bounded retries for 429/5xx/timeouts only). The key is read at
every call via ``secrets.require_secret("fred-api-key")`` (SecretMissing
before any request) and masked in every error text (``newsroom.keys.redact``
— the same redaction the newsroom collectors use; aiohttp/urllib errors can
contain the request URL with ``api_key=``).

- ``first_releases(series_id, units, start, end)`` — ALFRED "initial release
  only" observations (``output_type=4``, ``realtime_start=1776-07-04``,
  ``realtime_end=9999-12-31``) -> ``[{date, value, realtime_start}]``; each
  observation's EARLIEST ``realtime_start`` is its known date. Values ``.``
  (no observation) are skipped. Requests are split into observation-date
  windows (``WINDOW_DAYS``) so a request stays below FRED's limit of 2000
  vintage dates per JSON request.
- ``release_dates(release_id)`` — all release dates of a FRED release
  including scheduled future ones (``include_release_dates_with_no_data``).

Failures raise ``DataSourceUnavailable("fred", "macro", reason)`` — no stand-in.
Tests inject ``transport(url, params, timeout) -> (status, body_bytes, headers)``.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Callable, List, Optional

from ... import secrets
from ...core.errors import DataSourceUnavailable
from ...data import http_gate

logger = logging.getLogger(__name__)

API_BASE = "https://api.stlouisfed.org/fred/"
FRED_SECRET = "fred-api-key"
SOURCE = "fred"
DATA_TYPE = "macro"
REQUEST_TIMEOUT_SECONDS = 30
REALTIME_START = "1776-07-04"     # ALFRED: the whole real-time history
REALTIME_END = "9999-12-31"
PAGE_LIMIT = 100000               # FRED maximum observations per request
WINDOW_DAYS = {"daily": 366, "weekly": 3660, "monthly": 7320}


class FredHttpError(Exception):
    def __init__(self, status: int, message: str, retry_after: Optional[float] = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.retry_after = retry_after


def _redact(text: str, key: str) -> str:
    from newsroom.keys import redact
    return redact(text, key)


def urllib_transport(url: str, params: dict, timeout: float):
    """Real HTTP GET -> (status, body bytes, headers dict). Network errors raise."""
    import urllib.error
    import urllib.parse
    import urllib.request
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https host
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read() or b"", dict(ex.headers or {})


def classify(ex: BaseException, data_type: str):
    """Gate classifier: 429/5xx/timeouts are retried, the rest stops at once."""
    if isinstance(ex, FredHttpError):
        if ex.status == 429 or ex.status >= 500:
            return http_gate.Retry(f"HTTP {ex.status}", ex.retry_after)
        if ex.status in (401, 403) or (ex.status == 400 and "api_key" in ex.message.lower()):
            return DataSourceUnavailable(SOURCE, data_type, "FRED rejected the API key — check it in "
                                         "Hedge Fund → Settings → Keys")
        return DataSourceUnavailable(SOURCE, data_type, f"FRED answered HTTP {ex.status}: {ex.message}")
    if isinstance(ex, TimeoutError) or "timed out" in str(ex).lower():
        return http_gate.Retry("timeout")
    return DataSourceUnavailable(SOURCE, data_type, f"request failed ({str(ex)[:200] or type(ex).__name__})")


class FredClient:
    def __init__(self, transport: Optional[Callable] = None, gate: Optional[http_gate.HttpGate] = None):
        self.transport = transport or urllib_transport
        self._gate = gate
        self.requests = 0

    @property
    def gate(self) -> http_gate.HttpGate:
        return self._gate or http_gate.gate_for(SOURCE)

    def _get(self, endpoint: str, params: dict) -> dict:
        key = secrets.require_secret(FRED_SECRET)        # SecretMissing before any request
        full = {**params, "api_key": key, "file_type": "json"}

        def once():
            self.requests += 1
            try:
                status, body, headers = self.transport(API_BASE + endpoint, full, REQUEST_TIMEOUT_SECONDS)
            except Exception as ex:   # noqa: BLE001 — re-raised WITHOUT the key (the URL may be in the text)
                text = _redact(str(ex) or type(ex).__name__, key)
                if isinstance(ex, TimeoutError) or "timed out" in text.lower():
                    raise TimeoutError(text) from None
                raise ConnectionError(text) from None
            if status != 200:
                message = ""
                try:
                    message = str(json.loads(body.decode("utf-8")).get("error_message") or "")
                except (ValueError, AttributeError, UnicodeDecodeError):
                    message = ""
                retry_after = None
                try:
                    retry_after = float((headers or {}).get("Retry-After"))
                except (TypeError, ValueError):
                    retry_after = None
                raise FredHttpError(status, _redact(message or "request failed", key)[:300], retry_after)
            try:
                data = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as ex:
                raise DataSourceUnavailable(SOURCE, DATA_TYPE, "FRED returned malformed JSON") from ex
            if not isinstance(data, dict):
                raise DataSourceUnavailable(SOURCE, DATA_TYPE, "FRED returned unexpected data")
            return data

        return self.gate.call(once, data_type=DATA_TYPE, classify=classify)

    def first_releases(self, series_id: str, units: Optional[str], start: date, end: date,
                       frequency: str = "daily") -> List[dict]:
        """[{date, value, realtime_start}] oldest first, start <= date <= end."""
        out = {}
        step = timedelta(days=WINDOW_DAYS.get(frequency, 366))
        a = start
        while a <= end:
            b = min(end, a + step - timedelta(days=1))
            params = {"series_id": series_id, "observation_start": a.isoformat(), "observation_end": b.isoformat(),
                      "realtime_start": REALTIME_START, "realtime_end": REALTIME_END, "output_type": "4",
                      "sort_order": "asc", "limit": str(PAGE_LIMIT)}
            if units:
                params["units"] = units
            data = self._get("series/observations", params)
            observations = data.get("observations")
            if not isinstance(observations, list):
                raise DataSourceUnavailable(SOURCE, DATA_TYPE, f"FRED returned no observation list for {series_id}")
            for obs in observations:
                if not isinstance(obs, dict):
                    raise DataSourceUnavailable(SOURCE, DATA_TYPE, f"FRED returned a malformed observation of {series_id}")
                raw = obs.get("value")
                if raw == ".":
                    continue      # FRED's marker for "no observation on that date"
                try:
                    d = date.fromisoformat(str(obs["date"])[:10]).isoformat()
                    rt = date.fromisoformat(str(obs["realtime_start"])[:10]).isoformat()
                    v = float(raw)
                except (KeyError, TypeError, ValueError) as ex:
                    raise DataSourceUnavailable(SOURCE, DATA_TYPE,
                                                f"FRED returned a malformed observation of {series_id}") from ex
                if v != v or v in (float("inf"), float("-inf")):
                    raise DataSourceUnavailable(SOURCE, DATA_TYPE, f"FRED returned a non-finite value of {series_id}")
                prev = out.get(d)
                if prev is None or rt < prev["realtime_start"]:      # earliest vintage = first release
                    out[d] = {"date": d, "value": v, "realtime_start": rt}
            a = b + timedelta(days=1)
        return [out[d] for d in sorted(out)]

    def release_dates(self, release_id: int) -> List[str]:
        """Sorted unique ISO dates of a release (past and scheduled)."""
        params = {"release_id": str(int(release_id)), "realtime_start": REALTIME_START,
                  "realtime_end": REALTIME_END, "include_release_dates_with_no_data": "true",
                  "sort_order": "asc", "limit": "10000"}
        data = self._get("release/dates", params)
        rows = data.get("release_dates")
        if not isinstance(rows, list):
            raise DataSourceUnavailable(SOURCE, DATA_TYPE, f"FRED returned no release dates for release {release_id}")
        out = set()
        for row in rows:
            try:
                out.add(date.fromisoformat(str(row["date"])[:10]).isoformat())
            except (KeyError, TypeError, ValueError) as ex:
                raise DataSourceUnavailable(SOURCE, DATA_TYPE,
                                            f"FRED returned a malformed release date for release {release_id}") from ex
        return sorted(out)


_default: Optional[FredClient] = None


def default_client() -> FredClient:
    global _default
    if _default is None:
        _default = FredClient()
    return _default


def set_default_client(client: Optional[FredClient]) -> None:
    """Tests: inject a client with a fake transport (None resets)."""
    global _default
    _default = client
