"""EUR/USD rates for the Shadow Account conversions (SPEC §19.6–§19.8, §19.15 — the tax export was REMOVED on
2026-10-18 by the user's decision): FRED series ``DEXUSEU`` ("U.S.
Dollars to One Euro", Federal Reserve H.10 noon buying rates in New York) — the one FX source the data layer has.
NOT the ECB euro reference rate (the data layer has no ECB source — the user decides whether this is acceptable).

Loaded only on the user's request (``refresh(year)``) with the existing FRED client (``strategy/features/fred.py``:
key ``fred-api-key``, gate ``fred``) into ``ext_observations`` (series ``DEXUSEU``, source ``fred``); conversions read
ONLY the stored rates, and ONLY the fixing of the exact date (review fix H1, 2026-10-18): a date without a fixing
(US holiday, weekend) or after the last loaded fixing is MISSING (that value is left out with a warning / the
shadow rule is unavailable, with the reason) — no stand-in.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from ..execution.errors import TradingError
from ..storage import db

SERIES = "DEXUSEU"
SOURCE = "fred"
NAME = "U.S. Dollars to One Euro (Federal Reserve H.10, noon buying rates in New York)"
NOTE = ("Not the ECB euro reference rate — the app's data layer has no ECB source. Used only to convert Shadow "
        "Account values between EUR and USD for the behaviour analysis (monitoring only).")
SUPPORTED = ("EUR", "USD")


class FxMissing(Exception):
    def __init__(self, missing: List[str]):
        super().__init__("FX rates missing")
        self.missing = sorted(set(missing))


class FxUnsupported(Exception):
    def __init__(self, currency: str):
        super().__init__(f"no FX source for {currency} in the data layer (FRED {SERIES} covers EUR/USD only)")
        self.currency = currency


def describe() -> dict:
    return {"series": SERIES, "source": SOURCE, "name": NAME, "note": NOTE, "lookup": "exact date only"}



class Rates:
    """USD per EUR by date — stored observations only, EXACT dates (SPEC §19.14 H1: no earlier-fixing lookup)."""

    def __init__(self, rows: Iterable[dict]):
        pts = sorted((r["date"], float(r["value"])) for r in rows if float(r["value"]) > 0)
        self.by_day = dict(pts)
        self.last = pts[-1][0] if pts else None
        self.used: set = set()

    @classmethod
    def load(cls, start: date, end: date) -> "Rates":
        rows = db.query_ext_observations(SERIES, SOURCE, start, end)
        last = db.query_ext_observations(SERIES, SOURCE, end, None)        # fixings after the window: the "last"
        r = cls(rows)
        if last:
            r.last = max(r.last or "", last[-1]["date"])
        return r

    def reason(self, day: str) -> str:
        """Why ``day`` has no rate: after the last loaded fixing, or no fixing in the source on that day."""
        if self.last is None:
            return "no FRED DEXUSEU fixing is loaded at all — refresh the FX rates"
        if day > self.last:
            return f"after the last loaded fixing ({self.last}) — refresh the FX rates"
        return "FRED DEXUSEU has no fixing on that day (e.g. a US holiday or a weekend)"

    def usd_per_eur(self, day: str) -> Optional[float]:
        r = self.by_day.get(day)
        if r is not None:
            self.used.add(day)
        return r

    def rate_to_eur(self, currency: str, day: str) -> Optional[float]:
        """EUR per 1 unit of ``currency`` on ``day`` (1.0 for EUR); None = missing; raises FxUnsupported."""
        c = (currency or "").upper()
        if c == "EUR":
            return 1.0
        if c != "USD":
            raise FxUnsupported(c or "?")
        r = self.usd_per_eur(day)
        return None if r is None else 1.0 / r

    def convert(self, amount: float, frm: str, to: str, day: str, missing: List[str]) -> Optional[float]:
        """``amount`` from ``frm`` to ``to`` (EUR/USD) at ``day``; a missing rate is appended to ``missing``."""
        f, t = (frm or "").upper(), (to or "").upper()
        if f == t:
            return float(amount)
        for c in (f, t):
            if c not in SUPPORTED:
                raise FxUnsupported(c or "?")
        r = self.usd_per_eur(day)
        if r is None:
            missing.append(day)
            return None
        return float(amount) / r if f == "USD" else float(amount) * r


def coverage(year: int) -> dict:
    rows = db.query_ext_observations(SERIES, SOURCE, date(year, 1, 1), date(year, 12, 31))
    fetched = max((r.get("fetched_at") for r in rows), default=None)
    return {"series": SERIES, "year": int(year), "observations": len(rows),
            "first": rows[0]["date"] if rows else None, "last": rows[-1]["date"] if rows else None,
            "fetched_at": fetched}


def refresh(year: int, client=None) -> dict:
    """Downloads DEXUSEU for ``year`` (and the last days of the year before, for the lookup rule) from FRED."""
    from ..core.errors import DataSourceUnavailable, SecretMissing, SecretStoreUnavailable
    from ..strategy.features import fred
    start = date(year, 1, 1)
    end = min(date(year, 12, 31), datetime.now(timezone.utc).date())
    if end < start:
        raise TradingError(409, "future_year", f"{year} has not started yet.")
    before = coverage(year)["observations"]
    try:
        rows = (client or fred.default_client()).first_releases(SERIES, None, start, end, "daily")
    except (SecretMissing, SecretStoreUnavailable) as ex:
        raise TradingError(503, "fx_unavailable", getattr(ex, "user_message", None) or
                           "The FRED key is missing — add it in Hedge Fund → Settings → Keys.")
    except DataSourceUnavailable as ex:
        raise TradingError(503, "fx_unavailable", f"FRED {SERIES} could not be loaded: "
                                                  f"{getattr(ex, 'user_message', None) or ex}")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if rows:
        db.upsert_ext_observations([{**r, "series_id": SERIES, "source": SOURCE, "fetched_at": fetched_at}
                                    for r in rows])
    db.add_audit("user", "fx_refresh", {"series": SERIES, "year": year, "rows": len(rows)})
    out = coverage(year)
    out["added"] = max(0, out["observations"] - before)
    return out
