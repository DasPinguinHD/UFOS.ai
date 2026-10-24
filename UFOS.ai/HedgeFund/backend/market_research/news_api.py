"""MR6 REST routes (SPEC §4.2, exact JSON in §13 "MR6 backend notes") — mixed into ``api.ResearchApi``:

- ``GET /research/symbols/{symbol}/news?limit=&before=`` (JSON),
- ``GET /research/symbols/{symbol}/logo`` (``image/png`` bytes or 404 JSON),
- ``GET /research/news/images/{key}`` (thumbnail proxy: image bytes or JSON error),
- ``GET /research/news/settings``, ``PUT /research/news/settings {"thumbnails": bool}``.

Binary answers are returned as ``Binary`` payloads; ``api.register`` turns them into a plain
``web.Response`` with ``X-Content-Type-Options: nosniff`` and a restrictive CSP. Unknown query parameters /
body fields → 400. Alpaca calls run on the Research pool inside ``http_gate.research_priority()``; the
thumbnail proxy runs on its own small pool (``executor.run_image``) under its own gate (not the Alpaca budget).

Part of UFOS.ai, a proof-of-concept portfolio project; research only.
"""
from __future__ import annotations

import re

from strategy_platform.api.routes import NO_BODY
from strategy_platform.core.clock import to_utc

from . import limits
from .bars.series import BadParam
from .data_api import _int, _one, _research, _symbol, _unknown
from .executor import PoolBusy, run_blocking, run_image
from .logos import LogosNotOnPlan, NoLogo
from .news import NewsUnavailable, ThumbnailsOff, ThumbnailUnavailable, UnknownImage

KEY_RE = re.compile(r"^[0-9a-f]{64}$")


class Binary:
    """A non-JSON answer (image bytes)."""

    def __init__(self, data: bytes, content_type: str, *, max_age: int = 3600):
        self.data, self.content_type, self.max_age = bytes(data), content_type, int(max_age)

    def headers(self) -> dict:
        return {"Cache-Control": f"private, max-age={self.max_age}", "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox"}


def _bad(errors: dict, message: str = "invalid input"):
    return 400, {"error": message, "errors": errors}


class NewsRoutes:
    """Mixin: needs ``self.service.news`` (``news.NewsService``) and ``self.service.logos`` (``LogoService``)."""

    # GET /research/symbols/{symbol}/news?limit=&before=
    async def symbol_news(self, query, body, match):
        async def go():
            bad = _unknown(query, ("limit", "before"))
            if bad:
                return _bad(bad)
            inst = _symbol(match)
            limit = _int(query, "limit", 1, limits.NEWS_LIMIT_MAX) or limits.NEWS_LIMIT_DEFAULT
            before = _one(query, "before")
            if before is not None:
                if len(before) > 40:
                    raise BadParam("before", "not an ISO-8601 time")
                try:
                    before = to_utc(before)
                except (ValueError, TypeError):
                    raise BadParam("before", "not an ISO-8601 time")
            try:
                return 200, await _research(self.service.news.symbol_news, inst.symbol, limit, before)
            except NewsUnavailable as ex:
                return 503, {"error": ex.user_message, "kind": ex.cause_kind}
        return await self._guard(go)

    # GET /research/symbols/{symbol}/logo
    async def symbol_logo(self, query, body, match):
        async def go():
            if query:
                return _bad(_unknown(query, ()))
            inst = _symbol(match)
            try:
                png = await _research(self.service.logos.logo, inst.symbol)
            except NoLogo as ex:
                return 404, {"error": ex.user_message, "kind": "NoLogo"}
            except LogosNotOnPlan as ex:
                return 404, {"error": ex.user_message, "kind": "LogosNotOnPlan"}
            return 200, Binary(png, "image/png", max_age=86400)
        return await self._guard(go)

    # GET /research/news/images/{key}
    async def news_image(self, query, body, match):
        if query:
            return _bad(_unknown(query, ()))
        key = str(match.get("key", ""))
        if not KEY_RE.match(key):
            return 404, {"error": UnknownImage().user_message, "kind": "UnknownImage"}
        try:
            mime, data = await run_image(self.service.news.image, key)    # own pool (review M2)
        except PoolBusy as ex:
            return 503, {"error": ex.user_message, "kind": "ThumbnailBusy"}
        except ThumbnailsOff as ex:
            return 409, {"error": ex.user_message, "kind": "ThumbnailsOff"}
        except UnknownImage as ex:
            return 404, {"error": ex.user_message, "kind": "UnknownImage"}
        except ThumbnailUnavailable as ex:
            return 503, {"error": ex.user_message, "kind": "ThumbnailUnavailable"}
        return 200, Binary(data, mime, max_age=3600)

    # GET /research/news/settings
    async def news_settings_get(self, query, body, match):
        if query:
            return _bad(_unknown(query, ()))
        return 200, await run_blocking(self.service.news.settings)

    # PUT /research/news/settings {"thumbnails": bool}
    async def news_settings_put(self, query, body, match):
        if query:
            return _bad(_unknown(query, ()))
        if body is NO_BODY or not isinstance(body, dict):
            return _bad({"_": "a JSON object body is required"})
        extra = [k for k in body if k != "thumbnails"]
        if extra:
            return _bad({str(k)[:40]: "unknown field" for k in extra[:20]})
        if not isinstance(body.get("thumbnails"), bool):
            return _bad({"thumbnails": "must be true or false"})
        return 200, await run_blocking(self.service.news.set_thumbnails, body["thumbnails"])


NEWS_ROUTES = (
    ("GET", "/research/symbols/{symbol}/news", "symbol_news", False),
    ("GET", "/research/symbols/{symbol}/logo", "symbol_logo", False),
    ("GET", "/research/news/images/{key}", "news_image", False),
    ("GET", "/research/news/settings", "news_settings_get", False),
    ("PUT", "/research/news/settings", "news_settings_put", True),
)
