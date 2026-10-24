"""/research/* HTTP routes (SPEC §1.2, §4).

All routes are added to main.py's aiohttp app and therefore sit behind its
``api_auth`` middleware (session token + origin check). Same two layers as
``strategy_platform/api/routes.py`` (whose ``dumps``/``read_body``/
``user_error`` are reused):

- ``ResearchApi``: one ``async`` method per route taking
  ``(query, body, match)`` and returning ``(status, payload)``;
- ``register(app, service)``: adds the aiohttp routes (aiohttp imported lazily).

MR1: ``GET /research/feeds/status``. MR2 (data_api.py): ``/research/assets``, ``/research/market/status``,
``/research/symbols/{symbol}/snapshot|bars|stats``. MR6 (news_api.py): news, logo + thumbnail
proxy answer image bytes (``Binary`` → ``web.Response``). Errors: invalid input → 400
``{error, errors}``; ``UserFacingError`` → 400/503 ``{error, kind}``;
anything else → 500 with a generic message (details only in backend.log).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Mapping, Tuple

from strategy_platform.api.routes import NO_BODY, dumps, read_body, user_error
from strategy_platform.core.errors import UserFacingError

from . import feeds as default_feeds
from .data_api import DATA_ROUTES, DataRoutes
from .workspace_api import WORKSPACE_ROUTES, WorkspaceRoutes
from .drawings_api import DRAWING_ROUTES, DrawingRoutes
from .news_api import NEWS_ROUTES, Binary, NewsRoutes
from .alerts_api import ALERT_ROUTES, AlertRoutes
from .integrations_api import INTEGRATION_ROUTES, IntegrationRoutes

logger = logging.getLogger(__name__)

STATUS_SCHEMA = 1
INTERNAL_ERROR = "internal error in Market Research (see backend.log)"


def _bad(errors: dict, message: str = "invalid input") -> Tuple[int, dict]:
    return 400, {"error": message, "errors": errors}


def _no_query(query: Mapping):
    if query:
        return _bad({str(k)[:40]: "unknown parameter" for k in list(query)[:20]})
    return None


def status_payload(supervisor, feeds=default_feeds, now=None) -> dict:
    """The exact GET /research/feeds/status shape (SPEC §1.2)."""
    s = supervisor.status()
    feed_rows = feeds.feeds_status()
    return {
        "schema": STATUS_SCHEMA,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "research": s["research"],
        "store": s["store"],
        "tasks": s["tasks"],
        "feeds": feed_rows,
        "live_count": sum(1 for f in feed_rows if f.get("state") == "live"),
        "feed_count": len(feed_rows),
        "demand": feeds.demand(),
        "budget": feeds.budget(),
    }


class ResearchApi(DataRoutes, WorkspaceRoutes, DrawingRoutes, NewsRoutes, AlertRoutes, IntegrationRoutes):   # MR2: data_api.py; MR4: workspace_api.py; MR5: drawings_api.py; MR6: news_api.py; MR7: alerts_api.py; MR8: integrations_api.py
    def __init__(self, service):
        self.service = service

    # GET /research/feeds/status
    async def feeds_status(self, query: Mapping, body, match: Mapping):
        bad = _no_query(query)
        if bad:
            return bad
        return 200, status_payload(self.service.supervisor, self.service.feeds)


ROUTES = (
    ("GET", "/research/feeds/status", "feeds_status", False),
) + DATA_ROUTES                                  # MR2: assets, market status, snapshot, bars, stats
ROUTES = ROUTES + WORKSPACE_ROUTES               # MR4: watchlists, quotes, session, recent, palette
ROUTES = ROUTES + DRAWING_ROUTES                 # MR5: drawings, chart settings, indicators
ROUTES = ROUTES + NEWS_ROUTES                    # MR6: news, logo, thumbnail proxy, news settings
ROUTES = ROUTES + ALERT_ROUTES                   # MR7: alerts CRUD + alert log
ROUTES = ROUTES + INTEGRATION_ROUTES             # MR8: portfolio, markers, backtest markers, universe (read-only)


def register(app, service) -> ResearchApi:
    """Adds the /research/* routes to an aiohttp app (behind its middlewares)."""
    from aiohttp import web  # lazy: importable without aiohttp

    api = ResearchApi(service)

    def make(name: str, reads_body: bool):
        handler = getattr(api, name)

        async def route(request):
            body = NO_BODY
            if reads_body:
                try:
                    body = await read_body(request)
                except ValueError:
                    return web.json_response({"error": "invalid JSON body", "errors": {"_": "invalid JSON body"}},
                                             status=400)
            try:
                status, payload = await handler(request.query, body, request.match_info)
            except UserFacingError as ex:
                status, payload = user_error(ex)
            except Exception:
                logger.exception("/research route %s failed", name)
                status, payload = 500, {"error": INTERNAL_ERROR}
            if isinstance(payload, Binary):          # MR6: image bytes (logo, news thumbnail proxy)
                return web.Response(body=payload.data, status=status, content_type=payload.content_type,
                                    headers=payload.headers())
            return web.json_response(payload, status=status, dumps=dumps)

        route.__name__ = f"research_{name}"
        return route

    for method, path, name, reads_body in ROUTES:
        app.router.add_route(method, path, make(name, reads_body))
    return api
