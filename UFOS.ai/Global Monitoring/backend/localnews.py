"""Regional news lookup for the "click a capital star" feature (/localnews).

2026-09-24: switched from GDELT to Google News' public search RSS feed.
GDELT's IP rate limit meant this lookup never returned a single article in
practice (see news_feeds.py). Google News search RSS needs no API key, is
built for exactly this ("news about <place> from the last few days"), and
aggregates many local outlets per city — which the world-news RSS feeds
alone can't do.

Note: Google News RSS is intended for personal, non-commercial use; that
fits this proof-of-concept. A commercial build would need a licensed news
API instead (see backend/README.md).

Fallback: if Google News fails or returns nothing, the stories already
loaded from the world-news feeds (news_feeds.latest_items) are searched for
the city or country name — zero extra requests.

Caches (keyed by "city|country"): a success for LOCALNEWS_CACHE_SECONDS, a
failure for LOCALNEWS_ERROR_CACHE_SECONDS, so repeated clicks on the same
star don't re-query.
"""
import logging
import re
import time
from urllib.parse import quote_plus, urlparse

import aiohttp

import news_feeds
from config import LOCALNEWS_MAX_RECORDS, LOCALNEWS_CACHE_SECONDS, LOCALNEWS_ERROR_CACHE_SECONDS

logger = logging.getLogger(__name__)

GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

_cache: dict[str, tuple[float, dict]] = {}

# The panel shows LOCALNEWS_MAX_RECORDS; the AI summary ("✦ AI Sum up local
# news", /localnews-summary) gets up to this many from the same cached lookup.
SUMMARY_MAX_RECORDS = 20
_error_cache: dict[str, tuple[float, str]] = {}


def _article(item: dict) -> dict:
    title = item["title"]
    source = item.get("source") or urlparse(item["url"]).netloc
    # Google News titles end in " - <Outlet>"; the outlet is shown separately.
    if source and title.endswith(" - " + source):
        title = title[: -len(" - " + source)]
    published = item.get("published")
    summary = item.get("summary") or ""
    return {
        "title": title,
        "url": item["url"],
        "domain": source,
        "seendate": published.strftime("%Y-%m-%d %H:%M UTC") if published else "",
        # Google News' <description> only repeats the headline; world-feed
        # fallback items carry a real teaser. Only kept if it adds something.
        "summary": summary if summary and not summary.startswith(title[:40]) else "",
    }


def _parse_google_news(body: bytes) -> list:
    """Like news_feeds.parse_feed, but takes the outlet name from each item's
    <source> tag (Google News aggregates many outlets in one feed)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(body)
    items = []
    for it in root.iter("item"):
        src_el = it.find("source")
        parsed = news_feeds.parse_feed(ET.tostring(it), "")  # reuse title/link/date parsing
        if not parsed:
            continue
        entry = parsed[0]
        entry["source"] = (src_el.text or "").strip() if src_el is not None else "Google News"
        items.append(entry)
    return items


async def _google_news(session: aiohttp.ClientSession, city: str, country: str) -> list:
    query = f'"{city}" {country} when:3d' if country else f'"{city}" when:3d'
    url = GOOGLE_NEWS_SEARCH.format(q=quote_plus(query))
    async with session.get(url, headers=news_feeds.HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        body = (await resp.read()).strip()
    return _parse_google_news(body)


def _from_loaded_feeds(city: str, country: str) -> list:
    names = [n for n in (city, country) if n]
    if not names:
        return []
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b", re.IGNORECASE)
    hits = [i for i in news_feeds.latest_items if pattern.search(i["title"] + " " + i["summary"])]
    hits.sort(key=lambda i: i["published"].timestamp() if i["published"] else 0, reverse=True)
    return hits


async def fetch_local_news(session: aiohttp.ClientSession, city: str, country: str,
                           limit: int = LOCALNEWS_MAX_RECORDS) -> dict:
    """Returns {"articles": [...], "notice": str|None}. Raises only if both
    Google News and the loaded-feeds fallback come up empty."""
    cache_key = f"{city.lower()}|{country.lower()}"

    cached = _cache.get(cache_key)
    if cached and (time.time() - cached[0]) < LOCALNEWS_CACHE_SECONDS:
        return {"articles": cached[1]["articles"][:limit], "notice": cached[1]["notice"]}

    notice = None
    items: list = []
    cached_error = _error_cache.get(cache_key)
    if not (cached_error and (time.time() - cached_error[0]) < LOCALNEWS_ERROR_CACHE_SECONDS):
        try:
            items = await _google_news(session, city, country)
        except Exception as ex:
            msg = str(ex) or type(ex).__name__
            logger.warning("Google News lookup failed for %s, %s: %s", city, country, msg)
            _error_cache[cache_key] = (time.time(), msg)
            notice = f"Google News is unavailable right now ({msg})."

    if not items:
        items = _from_loaded_feeds(city, country)
        if items:
            notice = (notice + " " if notice else "") + "Showing matching stories from the world-news feeds instead."

    if not items:
        raise RuntimeError(notice or f"No recent English-language coverage found for {city}.")

    full = {"articles": [_article(i) for i in items[:SUMMARY_MAX_RECORDS]], "notice": notice}
    if notice is None:
        _cache[cache_key] = (time.time(), full)
        _error_cache.pop(cache_key, None)
    return {"articles": full["articles"][:limit], "notice": notice}
