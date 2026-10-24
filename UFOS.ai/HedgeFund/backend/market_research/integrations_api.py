"""MR8 REST routes (SPEC §4.2, exact JSON in §13 "MR8 backend notes") — mixed into ``api.ResearchApi``.
All READ-ONLY (``integrations.py``); there is no order route in Research.

``GET /research/portfolio``, ``GET /research/symbols/{symbol}/markers?from=&to=&kinds=``,
``GET /research/backtests/{run_id}/markers?symbol=&from=&to=``, ``GET /research/watchlists/{id}/universe``.

Part of UFOS.ai, a proof-of-concept portfolio project; research only, paper trading only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from strategy_platform.core.clock import to_utc
from strategy_platform.core.instruments import instrument

from . import integrations as ig
from . import limits
from . import watchlists as wl
from .bars.series import BadParam
from .data_api import _one, _symbol, _unknown
from .executor import run_blocking


def _bad(errors: dict, message: str = "invalid input"):
    return 400, {"error": message, "errors": errors}


def _time(query, key):
    v = _one(query, key)
    if v is None:
        return None
    if len(v) > 40:
        raise BadParam(key, "not an ISO-8601 time")
    try:
        return to_utc(v)
    except (ValueError, TypeError):
        raise BadParam(key, "not an ISO-8601 time")


def _range(query, *, default: bool):
    start, end = _time(query, "from"), _time(query, "to")
    if default:
        end = end or datetime.now(timezone.utc).replace(microsecond=0)
        start = start or end - timedelta(days=limits.MARKER_DEFAULT_DAYS)
    if start is not None and end is not None:
        if start >= end:
            raise BadParam("from", "must be before to")
        if end - start > timedelta(days=limits.MARKER_RANGE_MAX_DAYS):
            raise BadParam("from", f"the range may span at most {limits.MARKER_RANGE_MAX_DAYS} days")
    return start, end


class IntegrationRoutes:
    async def _ig_guard(self, coro_fn):
        try:
            return await self._guard(coro_fn)
        except ig.IntegrationNotFound as ex:
            return 404, {"error": ex.user_message}
        except ig.IntegrationUnavailable as ex:
            return 503, {"error": ex.user_message, "kind": "IntegrationUnavailable"}
        except wl.WorkspaceError as ex:
            return ex.status, ex.payload

    # GET /research/portfolio
    async def portfolio(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            return 200, await run_blocking(ig.portfolio)
        return await self._ig_guard(go)

    # GET /research/symbols/{symbol}/markers?from=&to=&kinds=insider,calendar
    async def symbol_markers(self, query, body, match):
        async def go():
            bad = _unknown(query, ("from", "to", "kinds"))
            if bad:
                return _bad(bad)
            inst = _symbol(match)
            start, end = _range(query, default=True)
            raw = _one(query, "kinds")
            kinds = list(ig.MARKER_KINDS) if raw is None else [k for k in raw.split(",") if k]
            if not kinds or len(raw or "x") > 40 or any(k not in ig.MARKER_KINDS for k in kinds) or \
                    len(set(kinds)) != len(kinds):
                raise BadParam("kinds", "comma-separated subset of " + ",".join(ig.MARKER_KINDS))
            return 200, await run_blocking(ig.symbol_markers, inst.symbol, start, end, kinds)
        return await self._ig_guard(go)

    # GET /research/backtests/{run_id}/markers?symbol=&from=&to=
    async def backtest_markers(self, query, body, match):
        async def go():
            bad = _unknown(query, ("symbol", "from", "to"))
            if bad:
                return _bad(bad)
            raw = _one(query, "symbol")
            if raw is None:
                raise BadParam("symbol", "required")
            try:
                sym = instrument(raw[:20]).symbol
            except ValueError:
                raise BadParam("symbol", "not a supported symbol (stocks/ETFs like NVDA, BRK.B; crypto like BTC-USD)")
            start, end = _range(query, default=False)
            return 200, await run_blocking(ig.backtest_markers, str(match.get("run_id", "")), sym, start, end)
        return await self._ig_guard(go)

    # GET /research/watchlists/{id}/universe
    async def watchlist_universe(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            lst = await run_blocking(wl.get_list, str(match.get("id", "")))
            return 200, ig.universe(lst)
        return await self._ig_guard(go)


INTEGRATION_ROUTES = (
    ("GET", "/research/portfolio", "portfolio", False),
    ("GET", "/research/symbols/{symbol}/markers", "symbol_markers", False),
    ("GET", "/research/backtests/{run_id}/markers", "backtest_markers", False),
    ("GET", "/research/watchlists/{id}/universe", "watchlist_universe", False),
)
