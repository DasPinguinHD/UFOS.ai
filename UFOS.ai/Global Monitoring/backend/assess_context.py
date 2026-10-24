"""Builds the context the Assess/Sentiment button sends to the model.

2026-09-24: the model kept answering "not enough context" — because it only
ever saw the marker's own headline + one-line teaser (or, for a flight, a
callsign and some telemetry), and the old prompt told it to say so whenever
that was thin. This module gathers what a human analyst would look at next:

1. FULL ARTICLE TEXT for news markers: the article page is fetched and its
   paragraphs extracted (stdlib HTMLParser, no new dependency), capped at
   ARTICLE_MAX_CHARS. Cached per URL. Falls back silently to the teaser if the
   page can't be fetched (paywall, JS-only page, timeout).
2. LOCATION: the country (or "open water near ...") under the marker, from
   geo.nearest_country() — flights and disasters only come with lat/lon.
3. RELATED EVENTS: candidates filtered per category (_RELATED_RULES — e.g.
   a flood gets nearby disasters + news about the same country, never
   flights), newest first, one line each with distance. For markets: the
   other market quotes plus the latest geopolitics headlines as backdrop.
4. STRUCTURED FACTS where the collector provides them (GDACS: type,
   country, alert level, ISO start/end dates).

build_context() returns (context_text, used_labels); used_labels is shown to
the user as "Based on: ..." so it's visible what the assessment rests on.

Stage 10 security audit L3 (2026-10-24): the article URL comes from a news
feed, so the fetch is restricted to PUBLIC internet addresses — a host that
resolves to a private / loopback / link-local / reserved address (the PC
itself, the router, the LAN, cloud metadata 169.254.169.254) is refused. The
check runs on the resolved addresses at connect time (a resolver that drops
non-public results, so a DNS answer can't change between check and connect),
on IP-literal hosts, and on every redirect hop (redirects are followed by hand,
at most ARTICLE_MAX_REDIRECTS). The article cache is an LRU of at most
ARTICLE_CACHE_MAX_ENTRIES entries.
"""
import ipaddress
import logging
import re
import socket
import time
from collections import OrderedDict
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

import news_feeds
from geo import detect_mentioned_country, haversine_km, nearest_country

logger = logging.getLogger(__name__)

ARTICLE_MAX_CHARS = 6000
ARTICLE_CACHE_SECONDS = 3600
ARTICLE_CACHE_MAX_ENTRIES = 200      # LRU bound (each entry ≤ ARTICLE_MAX_CHARS characters)
ARTICLE_MAX_REDIRECTS = 3
RELATED_MAX = 5

# Which other events count as "related", per category of the assessed event:
# {other category: max distance in km}. Plus, for news-type matches, "same
# country by headline" always counts. 2026-09-24: a single 1,500 km radius
# for everything fed a Sicilian flood assessment US military flights and
# unrelated politics — the model then spent sentences dismissing them.
_RELATED_RULES = {
    "disaster":    {"disaster": 1000, "geopolitics_same_country": True},
    "flight":      {"flight": 500, "geopolitics": 800},
    "geopolitics": {"disaster": 500, "flight": 500, "geopolitics_same_country": True},
}

_article_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()


class BlockedAddress(Exception):
    """The article URL points at a non-public address (or is not http/https)."""


def ip_is_public(value) -> bool:
    """True only for a global unicast address (not private, loopback, link-local, multicast, reserved,
    unspecified, shared/CGNAT, site-local; IPv4-mapped IPv6 judged by its IPv4 part)."""
    try:
        ip = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                                       or ip.is_reserved or ip.is_unspecified)


def check_url(url: str) -> str:
    """The URL if it is http(s) with a host that is a name or a PUBLIC IP literal; else BlockedAddress. A name is
    checked again after resolution (PublicOnlyResolver)."""
    try:
        parts = urlsplit(str(url or ""))
        host = parts.hostname or ""
    except ValueError as ex:
        raise BlockedAddress("unparsable URL") from ex
    if parts.scheme not in ("http", "https") or not host:
        raise BlockedAddress("not an http(s) URL with a host")
    if parts.username is not None or parts.password is not None:
        raise BlockedAddress("credentials in the URL")
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        literal = True
    except ValueError:
        literal = False
    if literal and not ip_is_public(host):
        raise BlockedAddress(f"non-public address {host}")
    if not literal and (host.lower() == "localhost" or host.lower().endswith((".localhost", ".local"))):
        raise BlockedAddress(f"local host name {host}")
    return url


class PublicOnlyResolver(AbstractResolver):
    """Resolves like aiohttp's default resolver, then keeps PUBLIC addresses only; none left → OSError (the
    connection is never opened). Used for every connection of the article fetch, redirect hops included."""

    def __init__(self, inner=None):
        self._inner = inner or aiohttp.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        infos = await self._inner.resolve(host, port, family=family)
        public = [i for i in infos if ip_is_public(i.get("host"))]
        if not public:
            raise OSError(f"{host} resolves to non-public addresses only — refused (stage 10 L3)")
        return public

    async def close(self):
        await self._inner.close()


def _cache_get(url: str):
    hit = _article_cache.get(url)
    if hit is None:
        return None
    if time.time() - hit[0] >= ARTICLE_CACHE_SECONDS:
        _article_cache.pop(url, None)
        return None
    _article_cache.move_to_end(url)
    return hit[1]


def _cache_put(url: str, text: str) -> None:
    _article_cache[url] = (time.time(), text)
    _article_cache.move_to_end(url)
    while len(_article_cache) > ARTICLE_CACHE_MAX_ENTRIES:
        _article_cache.popitem(last=False)


def _session_factory():
    """The article fetch's own session: the public-only resolver on every connection (not the shared session)."""
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicOnlyResolver()))


SESSION_FACTORY = _session_factory      # tests: a fake session


class _ParagraphExtractor(HTMLParser):
    """Collects the text of <p> elements, preferring those inside <article>
    or <main>; skips script/style/nav/footer/aside/header/form content."""
    _SKIP = {"script", "style", "nav", "footer", "aside", "header", "form", "noscript", "figure"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth_skip = 0
        self.depth_article = 0
        self.in_p = False
        self.buf = []
        self.paragraphs = []      # (in_article, text)

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.depth_skip += 1
        elif tag in ("article", "main"):
            self.depth_article += 1
        elif tag == "p" and not self.depth_skip:
            self.in_p, self.buf = True, []

    def handle_endtag(self, tag):
        if tag in self._SKIP and self.depth_skip:
            self.depth_skip -= 1
        elif tag in ("article", "main") and self.depth_article:
            self.depth_article -= 1
        elif tag == "p" and self.in_p:
            text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if len(text) >= 40:  # skip captions, bylines, "Share this" etc.
                self.paragraphs.append((self.depth_article > 0, text))
            self.in_p = False

    def handle_data(self, data):
        if self.in_p and not self.depth_skip:
            self.buf.append(data)


def extract_article_text(html_text: str) -> str:
    parser = _ParagraphExtractor()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    in_article = [t for inside, t in parser.paragraphs if inside]
    paragraphs = in_article if len(" ".join(in_article)) >= 300 else [t for _, t in parser.paragraphs]
    text = "\n".join(paragraphs)
    if len(text) < 300:
        # Fallback (2026-09-24, a Guardian article came back empty): the
        # structural parser can lose track on some pages (an unclosed
        # <header>/<form> hides everything after it). A plain regex over all
        # <p> elements is cruder but can't get stuck that way.
        raw_ps = re.findall(r"<p\b[^>]*>(.*?)</p>", html_text, re.IGNORECASE | re.DOTALL)
        cleaned = [news_feeds.clean_text(p) for p in raw_ps]
        cleaned = [p for p in cleaned if len(p) >= 60]
        if len("\n".join(cleaned)) > len(text):
            text = "\n".join(cleaned)
    if len(text) > ARTICLE_MAX_CHARS:
        text = text[:ARTICLE_MAX_CHARS].rsplit(" ", 1)[0] + " …[truncated]"
    return text


async def _get_following_redirects(session, url: str, headers: dict) -> str:
    """GET with redirects followed by hand (each hop checked, at most ARTICLE_MAX_REDIRECTS); the HTML text."""
    current = check_url(url)
    for _hop in range(ARTICLE_MAX_REDIRECTS + 1):
        async with session.get(current, headers=headers, allow_redirects=False,
                               timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    raise BlockedAddress("redirect without a Location")
                current = check_url(urljoin(current, location))
                continue
            resp.raise_for_status()
            raw = await resp.content.read(3_000_000)  # cap: nobody needs more than 3 MB of HTML
            return raw.decode(resp.charset or "utf-8", errors="replace")
    raise BlockedAddress(f"more than {ARTICLE_MAX_REDIRECTS} redirects")


async def fetch_article_text(session: aiohttp.ClientSession, url: str) -> str:
    """The article's paragraph text ("" when unavailable or refused). ``session`` (the shared one) is not used for
    the fetch itself: it runs on its own session with the public-only resolver (stage 10 L3)."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    cached = _cache_get(url)
    if cached is not None:
        return cached
    text = ""
    try:
        headers = dict(news_feeds.HEADERS, Accept="text/html,application/xhtml+xml")
        async with SESSION_FACTORY() as own:
            html_text = await _get_following_redirects(own, url, headers)
        text = extract_article_text(html_text)
        if not text:
            logger.info("Article text empty for %s (HTML %d chars, %d <p> tags) — page may render via JavaScript",
                        url, len(html_text), html_text.lower().count("<p"))
    except BlockedAddress as ex:
        logger.warning("Article fetch refused for %s: %s", url, ex)
    except Exception as ex:
        logger.info("Article text unavailable for %s: %s", url, str(ex) or type(ex).__name__)
    _cache_put(url, text)
    return text


def _age(ts: str) -> str:
    try:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ""
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f}h ago" if hours >= 1 else f"{delta.total_seconds() / 60:.0f}min ago"


_CATEGORY_LABELS = {"geopolitics": "news", "disaster": "disaster", "flight": "flight",
                    "markets": "market"}


def _line(ev: dict, km=None) -> str:
    cat = _CATEGORY_LABELS.get(ev.get("category"), ev.get("category", ""))
    summary = (ev.get("summary") or "")[:160]
    where = f", ~{km:.0f} km away" if km is not None else ""
    return f"- [{cat}{where}, {_age(ev.get('published') or ev.get('timestamp', ''))}] {ev.get('title', '')} — {summary}"


def _event_country(ev: dict):
    if ev.get("category") == "disaster":
        country = ((ev.get("facts") or {}).get("country") or "").strip().lower()
        return country or None
    if ev.get("category") == "geopolitics":
        return detect_mentioned_country(ev.get("title", "")) or detect_mentioned_country(ev.get("summary", ""))
    return None


def related_events(event: dict, all_events: list) -> list:
    """Candidate related events as (event, distance_km_or_None), newest
    first, at most RELATED_MAX — filtered per category by _RELATED_RULES."""
    others = [e for e in all_events if e.get("id") != event.get("id")]
    cat = event.get("category")

    if cat == "markets":
        markets = [(e, None) for e in others if e.get("category") == "markets"]
        news = [(e, None) for e in others if e.get("category") == "geopolitics"][:3]
        return (markets + news)[:RELATED_MAX + 3]

    rules = _RELATED_RULES.get(cat, {})
    country = _event_country(event)
    lat, lon = event.get("lat"), event.get("lon")
    picked = []
    for e in others:
        other_cat = e.get("category")
        km = None
        if None not in (lat, lon, e.get("lat"), e.get("lon")):
            km = haversine_km(lat, lon, e["lat"], e["lon"])
        max_km = rules.get(other_cat)
        near = isinstance(max_km, (int, float)) and km is not None and km <= max_km
        same_country = (other_cat == "geopolitics" and rules.get("geopolitics_same_country")
                        and country is not None and _event_country(e) == country)
        if near or same_country:
            picked.append((e, km))
    picked.sort(key=lambda p: p[0].get("published") or p[0].get("timestamp") or "", reverse=True)
    return picked[:RELATED_MAX]


def _facts_block(event: dict) -> str:
    facts = event.get("facts") or {}
    lines = [f"- {k.replace('_', ' ')}: {v}" for k, v in facts.items() if v]
    return ("Structured facts (dates are ISO yyyy-mm-dd):\n" + "\n".join(lines)) if lines else ""


async def build_context(session: aiohttp.ClientSession, event: dict, all_events: list) -> tuple[str, list]:
    parts, used = [], []

    facts = _facts_block(event)
    if facts:
        parts.append(facts)
        used.append("structured facts")

    lat, lon = event.get("lat"), event.get("lon")
    if lat is not None and lon is not None and event.get("category") in ("flight", "disaster", "geopolitics"):
        where = nearest_country(lat, lon)
        if where:
            parts.append(f"Location: {where} (lat {lat:.1f}, lon {lon:.1f}).")
            used.append("location")

    if event.get("category") == "geopolitics" and event.get("url"):
        article = await fetch_article_text(session, event["url"])
        if article:
            parts.append("Full article text (fetched from the source page):\n" + article)
            used.append("full article text")

    related = related_events(event, all_events)
    if related:
        label = ("Other market quotes and latest news headlines" if event.get("category") == "markets"
                 else "CANDIDATE related events (nearby or same country) — use only if clearly connected, "
                      "otherwise ignore them silently")
        parts.append(label + ":\n" + "\n".join(_line(e, km) for e, km in related))
        used.append(f"{len(related)} candidate related event{'s' if len(related) != 1 else ''}")

    return "\n\n".join(parts), used
