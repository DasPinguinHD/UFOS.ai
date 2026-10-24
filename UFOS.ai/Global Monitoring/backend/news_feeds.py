"""World-news RSS feeds — replaces GDELT as the news source (2026-09-24).

Why GDELT was dropped: its DOC 2.0 API rate-limits by IP with a long,
self-extending penalty window. In practice (2026-09-22 .. 09-24) the
/localnews lookup never returned a single article and the geopolitics
collector only got through occasionally — every restart during development
landed inside the penalty and extended it. See backend/README.md.

What replaces it: plain RSS feeds published by large international
newsrooms. No API key, no published rate limit (we poll every
NEWS_POLL_SECONDS, default 10 minutes — far gentler than any feed reader),
and the articles are real reporting with a headline + one-paragraph
description, which is also better input for the Assess button than GDELT's
bare headline.

Feeds are fetched concurrently; one broken or slow feed never affects the
others (each result carries its own ok/error). RSS 2.0, RDF (RSS 1.0) and
Atom are all handled by parse_feed().

latest_items keeps the most recent unfiltered pool (all feeds, all topics),
which /localnews uses as a fallback when the Google News lookup fails.
"""
import asyncio
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp

logger = logging.getLogger(__name__)

# (display name, URL). Chosen for global coverage from different regions'
# newsrooms (UK, Qatar, Germany, France, US, UN), not only US/UK outlets.
FEEDS = [
    ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("The Guardian", "https://www.theguardian.com/world/rss"),
    ("DW", "https://rss.dw.com/xml/rss-en-world"),
    ("France 24", "https://www.france24.com/en/rss"),
    ("UN News", "https://news.un.org/feed/subscribe/en/news/all/rss.xml"),
    ("NPR", "https://feeds.npr.org/1004/rss.xml"),
    ("CBC", "https://www.cbc.ca/webfeed/rss/rss-world"),
    # Economy/markets (2026-09-24): the layer is read with a finance lens,
    # so macro news (rates, tariffs, energy, currencies) belongs here too.
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("The Guardian Business", "https://www.theguardian.com/business/rss"),
    ("DW Business", "https://rss.dw.com/xml/rss-en-bus"),
]

# Some publishers reject aiohttp's default User-Agent.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; UFOS.ai-GlobalMonitoring/1.0; personal proof-of-concept)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
}

_ATOM = "{http://www.w3.org/2005/Atom}"
_RSS1 = "{http://purl.org/rss/1.0/}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_TAG_RE = re.compile(r"<[^>]+>")

latest_items: list = []


def clean_text(text: str) -> str:
    """Strip HTML tags/entities that many feeds put into <description>."""
    if not text:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", text))
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)          # RFC 822 (RSS 2.0)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))  # ISO 8601 (Atom, dc:date)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _child_text(el, *names):
    for name in names:
        found = el.find(name)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def parse_feed(body: bytes, source_name: str) -> list:
    root = ET.fromstring(body)
    items = []

    # RSS 2.0: <rss><channel><item>
    for it in root.iter("item"):
        items.append({
            "title": clean_text(_child_text(it, "title")),
            "url": _child_text(it, "link", "guid"),
            "summary": clean_text(_child_text(it, "description")),
            "published": _parse_date(_child_text(it, "pubDate", _DC + "date")),
            "source": source_name,
        })
    # RDF / RSS 1.0: namespaced <item>
    for it in root.iter(_RSS1 + "item"):
        items.append({
            "title": clean_text(_child_text(it, _RSS1 + "title")),
            "url": _child_text(it, _RSS1 + "link"),
            "summary": clean_text(_child_text(it, _RSS1 + "description")),
            "published": _parse_date(_child_text(it, _DC + "date")),
            "source": source_name,
        })
    # Atom: <feed><entry>
    for it in root.iter(_ATOM + "entry"):
        link_el = it.find(_ATOM + "link")
        items.append({
            "title": clean_text(_child_text(it, _ATOM + "title")),
            "url": link_el.get("href") if link_el is not None else "",
            "summary": clean_text(_child_text(it, _ATOM + "summary", _ATOM + "content")),
            "published": _parse_date(_child_text(it, _ATOM + "updated", _ATOM + "published")),
            "source": source_name,
        })

    return [i for i in items if i["title"] and i["url"]]


async def fetch_feed(session: aiohttp.ClientSession, name: str, url: str) -> tuple:
    """Returns (name, items, error_or_None). Never raises."""
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            body = (await resp.read()).strip()
        return name, parse_feed(body, name), None
    except Exception as ex:
        msg = str(ex) or type(ex).__name__
        logger.warning("News feed %s failed: %s", name, msg)
        return name, [], msg


async def fetch_all(session: aiohttp.ClientSession) -> tuple:
    """Fetches every feed concurrently. Returns (items, results) where
    results is a list of (name, item_count, error_or_None)."""
    global latest_items
    results = await asyncio.gather(*(fetch_feed(session, n, u) for n, u in FEEDS))
    items = [item for _, feed_items, _ in results for item in feed_items]
    if items:
        latest_items = items
    return items, [(name, len(feed_items), err) for name, feed_items, err in results]
