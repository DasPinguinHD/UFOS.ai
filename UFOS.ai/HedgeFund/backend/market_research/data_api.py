"""MR2 REST routes of ``/research/*`` (SPEC §4.2) — mixed into ``api.ResearchApi`` (same handler shape
``(query, body, match) -> (status, payload)``, same error wrapper in ``api.register``).

Validation (SPEC §4.1, §9): unknown query parameters → 400; symbols via ``core/instruments`` (→ 400);
``tf`` ∈ 1m 5m 15m 1H 4H 1D 1W (required); ``session`` rth|eth (stocks only — given for crypto → 400);
``before`` ISO-8601 (naive = UTC); ``limit`` integer 1 … MAX_BARS_PER_REQUEST; assets ``q`` 1–32 chars of
letters/digits/space/``.-/``, ``limit`` 1–50. Blocking work runs on the Research pool inside
``http_gate.research_priority()``.

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import re
from typing import Mapping, Optional

from strategy_platform.core.clock import to_utc
from strategy_platform.core.instruments import instrument

from . import limits
from .assets import QUERY_RE, SymbolNotCovered
from .bars import aggregate as agg
from .bars.series import BadParam, max_limit
from .executor import run_blocking

INT_RE = re.compile(r"^[0-9]{1,6}$")


def _bad(errors: dict, message: str = "invalid input"):
    return 400, {"error": message, "errors": errors}


def _unknown(query: Mapping, allowed) -> Optional[dict]:
    extra = [k for k in query if k not in allowed]
    return {str(k)[:40]: "unknown parameter" for k in extra[:20]} if extra else None


def _one(query: Mapping, key: str) -> Optional[str]:
    # aiohttp's MultiDictProxy.getall(key) raises KeyError for a missing key unless a default is
    # given (live test 2026-10-18: every optional parameter -> 500). Always pass the default.
    vals = list(query.getall(key, [])) if hasattr(query, "getall") else ([query[key]] if key in query else [])
    if len(vals) > 1:
        raise BadParam(key, "given more than once")
    return vals[0] if vals else None


def _int(query, key, lo, hi) -> Optional[int]:
    v = _one(query, key)
    if v is None:
        return None
    if not INT_RE.match(v) or not lo <= int(v) <= hi:
        raise BadParam(key, f"must be an integer {lo}–{hi}")
    return int(v)


def _symbol(match: Mapping):
    raw = str(match.get("symbol", ""))
    if len(raw) > 20:
        raise BadParam("symbol", "not a supported symbol")
    try:
        return instrument(raw)
    except ValueError:
        raise BadParam("symbol", "not a supported symbol (stocks/ETFs like NVDA, BRK.B; crypto like BTC-USD)")


async def _research(fn, *args):
    from strategy_platform.data import http_gate
    with http_gate.research_priority():
        return await run_blocking(fn, *args)


class DataRoutes:
    """Mixin: needs ``self.service.data`` (``data.DataService``)."""

    async def _guard(self, coro_fn):
        try:
            return await coro_fn()
        except BadParam as ex:
            return _bad({ex.field: ex.user_message}, ex.user_message if ex.field == "before" else "invalid input")
        except SymbolNotCovered as ex:
            return 404, {"error": ex.user_message, "kind": "SymbolNotCovered"}

    # GET /research/assets?q=&limit=
    async def assets(self, query, body, match):
        async def go():
            bad = _unknown(query, ("q", "limit"))
            if bad:
                return _bad(bad)
            q = _one(query, "q")
            if q is None or not QUERY_RE.match(q) or not q.strip():
                raise BadParam("q", f"1–{limits.ASSET_QUERY_MAX_CHARS} characters: letters, digits, space, . - /")
            limit = _int(query, "limit", 1, limits.ASSET_LIMIT_MAX) or limits.ASSET_LIMIT_DEFAULT
            return 200, await _research(self.service.data.search_assets, q, limit)
        return await self._guard(go)

    # GET /research/market/status
    async def market_status(self, query, body, match):
        if query:
            return _bad(_unknown(query, ()))
        return 200, await run_blocking(self.service.data.market_status)

    # GET /research/symbols/{symbol}/snapshot
    async def symbol_snapshot(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            inst = _symbol(match)
            return 200, await _research(self.service.data.snapshot, inst.symbol)
        return await self._guard(go)

    # GET /research/symbols/{symbol}/stats
    async def symbol_stats(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            inst = _symbol(match)
            return 200, await _research(self.service.data.stats, inst.symbol)
        return await self._guard(go)

    # GET /research/symbols/{symbol}/bars?tf=&session=&before=&limit=
    async def symbol_bars(self, query, body, match):
        async def go():
            bad = _unknown(query, ("tf", "session", "before", "limit"))
            if bad:
                return _bad(bad)
            inst = _symbol(match)
            tf = _one(query, "tf")
            if tf not in agg.TIMEFRAMES:
                raise BadParam("tf", "must be one of " + " ".join(agg.TIMEFRAMES))
            session = _one(query, "session")
            if session is not None:
                if inst.asset_class == "crypto":
                    raise BadParam("session", "applies to stocks/ETFs only (crypto bars are UTC aligned)")
                if session not in agg.SESSIONS:
                    raise BadParam("session", "must be rth or eth")
            before = _one(query, "before")
            if before is not None:
                if len(before) > 40:
                    raise BadParam("before", "not an ISO-8601 time")
                try:
                    before = to_utc(before)
                except (ValueError, TypeError):
                    raise BadParam("before", "not an ISO-8601 time")
            limit = _int(query, "limit", 1, max_limit(tf))
            return 200, await _research(self.service.data.bars, inst.symbol, tf, session, before, limit)
        return await self._guard(go)


DATA_ROUTES = (
    ("GET", "/research/assets", "assets", False),
    ("GET", "/research/market/status", "market_status", False),
    ("GET", "/research/symbols/{symbol}/snapshot", "symbol_snapshot", False),
    ("GET", "/research/symbols/{symbol}/bars", "symbol_bars", False),
    ("GET", "/research/symbols/{symbol}/stats", "symbol_stats", False),
)
